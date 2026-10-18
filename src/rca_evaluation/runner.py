"""Caller-driven, finite evaluation sequencing over existing Runtime semantics."""

from __future__ import annotations

from dataclasses import asdict
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .campaign import (
    ApprovedMetricDisposition, AuthoritativeOutcomeStatus, CampaignPlan,
    ExecutionDisposition, ExecutionResult, FailureCase, KnowledgeDisposition,
    PlannedExecution, PlannedRecoveryCase,
)
from .latency import ExternalObservation, ExternalObserver, LatencyTrace, ObservationClock
from .ledger import LedgerRecord
from .identity import commitment
from .manifest import ReproducibilityManifest
from .oracle import EvaluationPlan
from .latency import LatencyDisposition
from .recovery_evaluation import (
    Freshness, Materiality, RecoveryBoundary, ResolvedRecoveryObservation,
    evaluate_refresh, evaluate_restart, refresh_latency_measurement,
    restart_latency_measurement,
)
from .recovery_records import (
    evaluate_and_issue_refresh_result, evaluate_and_issue_restart_result,
    issue_deviation_result,
)
from .sqlite_store import SqliteEvaluationStore
from .evaluation_controls import (
    ActualCheckpointReceipt, is_actual_checkpoint_receipt,
)


class EvaluationLedger(Protocol):
    def append(self, record: LedgerRecord) -> LedgerRecord: ...
    def get(self, record_id: str) -> LedgerRecord | None: ...


class SignalInjector(Protocol):
    def inject(self, execution: PlannedExecution) -> None: ...


@dataclass(frozen=True, slots=True)
class RecoveryControlRequest:
    """Production-safe control projection; excludes Oracle/expected outcomes."""

    case_id: str
    parent_execution_id: str
    input_identity: str
    input_commitment: str
    recovery_boundary: str | None
    control_reference: str | None
    control_commitment: str | None


@dataclass(frozen=True, slots=True)
class RecoveryInterruptionReceipt:
    case_id: str
    boundary_identity: str
    exact_control_hook_identity: str
    control_commitment: str
    operation_identity: str
    work_identity: str
    interruption_count: int
    clean_reset: bool = False
    checkpoint_receipt: ActualCheckpointReceipt | None = None

    @classmethod
    def from_checkpoint(
        cls, *, case_id: str, control_commitment: str,
        checkpoint: ActualCheckpointReceipt,
    ) -> "RecoveryInterruptionReceipt":
        if not is_actual_checkpoint_receipt(checkpoint):
            raise TypeError("restart receipt requires an adapter-issued checkpoint")
        return cls(
            case_id, checkpoint.boundary_identity,
            checkpoint.exact_control_hook_identity, control_commitment,
            checkpoint.operation_identity, checkpoint.work_identity, 1,
            checkpoint_receipt=checkpoint)

    def validate(self, request: RecoveryControlRequest) -> None:
        if type(request) is not RecoveryControlRequest:
            raise TypeError("interruption receipt requires its control request")
        if (self.case_id != request.case_id
                or self.boundary_identity != request.recovery_boundary
                or self.exact_control_hook_identity != request.control_reference
                or self.control_commitment != request.control_commitment
                or self.interruption_count != 1):
            raise ValueError("restart interruption receipt is contradictory")
        if self.clean_reset:
            raise ValueError("clean reset cannot masquerade as restart recovery")
        proof = self.checkpoint_receipt
        if (not is_actual_checkpoint_receipt(proof)
                or proof.boundary_identity != self.boundary_identity
                or proof.exact_control_hook_identity
                    != self.exact_control_hook_identity
                or proof.operation_identity != self.operation_identity
                or proof.work_identity != self.work_identity
                or proof.durable_facts_commitment
                    != commitment(proof.durable_facts)):
            raise ValueError("restart receipt lacks an actual checkpoint proof")
        for value in (self.operation_identity, self.work_identity):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("restart interruption identity is missing")


@dataclass(frozen=True, slots=True)
class RecoveryRestartReceipt:
    case_id: str
    boundary_identity: str
    operation_identity: str
    work_identity: str
    restart_reopen_method: str


class RecoveryCaseController(Protocol):
    """Evaluation control only; production retry/recovery remains SPEC-011-owned."""

    def inject(
        self, request: RecoveryControlRequest,
    ) -> RecoveryInterruptionReceipt | None: ...
    def restart(
        self, request: RecoveryControlRequest,
    ) -> RecoveryRestartReceipt: ...
    def reconcile_publication(self, request: RecoveryControlRequest) -> bool: ...


class RecoveryCaseObserver(Protocol):
    """Return only resolver-issued observations from existing public reads."""

    def observe_refresh(
        self, case: PlannedRecoveryCase,
    ) -> tuple[ResolvedRecoveryObservation, ResolvedRecoveryObservation,
               ResolvedRecoveryObservation]: ...

    def observe_restart(
        self, case: PlannedRecoveryCase, *, phase: str,
    ) -> ResolvedRecoveryObservation: ...


@dataclass(frozen=True, slots=True)
class DeviationEvaluation:
    outcome: str
    expected_public_observation_class: str
    observed_disposition: str


def evaluate_deviation(
    *, expected_public_observation_class: str,
    observed_disposition: str, evaluation_passed: bool,
) -> DeviationEvaluation:
    """Form a typed deviation outcome from an evaluator result, never absence."""
    accepted = {
        "MATERIAL_STALE_REFRESH": {
            "REFRESHED_CURRENT_FRESH", "REFRESH_FAILED_CURRENT_RETAINED_STALE",
        },
        "NON_MATERIAL_NO_VERSION": {"NON_MATERIAL_NO_SEMANTIC_ACTION"},
        "MATERIAL_REFRESH_FAILURE_RETAINS_CURRENT": {
            "REFRESH_FAILED_CURRENT_RETAINED_STALE"},
        "SAME_OPERATION_RECOVERY": {"CONVERGED"},
    }.get(expected_public_observation_class, set())
    outcome = ("NO_DEVIATION" if evaluation_passed
               and observed_disposition in accepted else "DEVIATION")
    return DeviationEvaluation(
        outcome, expected_public_observation_class, observed_disposition)


def _state_label(observation: ResolvedRecoveryObservation) -> str:
    state = observation.state
    return f"{state.current_version_id}/CURRENT/{state.freshness.value}"


class RecoveryEvaluationRunner:
    """Run admitted S4 cases and append evaluator-derived facts to the same ledger."""

    def __init__(
        self, *, ledger: EvaluationLedger, observer: RecoveryCaseObserver,
        controller: RecoveryCaseController, polling_interval_ms: int,
        cutoff_ms: int, clock_source: str,
    ) -> None:
        if type(ledger) is not SqliteEvaluationStore:
            raise TypeError(
                "formal recovery execution requires the Candidate-F durable store")
        self._ledger = ledger
        self._observer = observer
        self._controller = controller
        self._polling_interval_ms = polling_interval_ms
        self._cutoff_ms = cutoff_ms
        self._clock_source = clock_source

    def run(
        self, *, admitted_plan: EvaluationPlan, campaign: CampaignPlan,
    ) -> tuple[LedgerRecord, ...]:
        campaign.validate(admitted_plan)
        if not campaign.required_refresh_case_ids:
            raise ValueError("required refresh case set is empty")
        if not campaign.required_restart_case_ids:
            raise ValueError("required restart case set is empty")
        records = []
        for case in campaign.recovery_cases:
            if self._ledger.get(case.parent_execution_id) is None:
                raise ValueError("recovery case requires its admitted execution record")
            records.append(self._run_one(case))
        return tuple(records)

    def _run_one(self, case: PlannedRecoveryCase) -> LedgerRecord:
        control = RecoveryControlRequest(
            case.case_id, case.parent_execution_id,
            case.fixture.input_identity, case.fixture.input_commitment,
            case.fixture.recovery_boundary,
            case.fixture.control_reference, case.fixture.control_commitment,
        )
        interruption = self._controller.inject(control)
        if case.kind == "REFRESH":
            before, after_materiality, final = self._observer.observe_refresh(case)
            evaluation = evaluate_refresh(
                before=before, after_materiality=after_materiality, final=final)
            latency_disposition = "NOT_APPLICABLE_NON_MATERIAL"
            latency_ms = None
            if after_materiality.materiality is Materiality.MATERIAL:
                disposition = (LatencyDisposition.OBSERVED_COMPLETION
                               if final.state.freshness is Freshness.FRESH
                               and final.state.current_version_id
                                   != before.state.current_version_id
                               else LatencyDisposition.FAILURE)
                measurement = refresh_latency_measurement(
                    after_materiality, final, disposition=disposition,
                    polling_interval_ms=self._polling_interval_ms,
                    cutoff_ms=self._cutoff_ms, clock_source=self._clock_source)
                latency_disposition = measurement.disposition.value
                latency_ms = measurement.duration_ms
            payload = {
                "kind": "REFRESH_VERSION_RESULT", "case_id": case.case_id,
                "fixture_identity": case.fixture.fixture_identity,
                "variant_identity": case.fixture.variant,
                "disposition": evaluation.disposition,
                "passed": evaluation.passed, "details": list(evaluation.details),
                "before_state": _state_label(before),
                "trigger_basis": ({
                    Materiality.MATERIAL: "MATERIAL_EVIDENCE",
                    Materiality.NON_MATERIAL: "NON_MATERIAL_EVIDENCE",
                }.get(after_materiality.materiality, "UNRESOLVED")),
                "resulting_state": _state_label(final),
                "freshness_transition": (
                    f"{before.state.freshness.value}_TO_"
                    f"{after_materiality.state.freshness.value}_TO_"
                    f"{final.state.freshness.value}"),
                "prior_current_preserved": (
                    evaluation.disposition != "REFRESH_FAILED_CURRENT_RETAINED_STALE"
                    or final.state.current_version_id == before.state.current_version_id),
                "duplicate_effect_detected": not evaluation.passed and any(
                    "duplicate" in item.lower() for item in evaluation.details),
                "latency_disposition": latency_disposition,
                "latency_ms": latency_ms,
                "public_observation_commitments": [
                    before.observation_commitment,
                    after_materiality.observation_commitment,
                    final.observation_commitment,
                ],
                "baseline_input_identity": case.fixture.baseline_input_identity,
                "baseline_input_commitment":
                    case.fixture.baseline_input_commitment,
                "candidate_input_identity": case.fixture.candidate_input_identity,
                "candidate_input_commitment":
                    case.fixture.candidate_input_commitment,
                "materiality_rule_version":
                    case.fixture.materiality_rule_version,
            }
        else:
            if type(interruption) is not RecoveryInterruptionReceipt:
                raise ValueError("restart case lacks a governed interruption receipt")
            interruption.validate(control)
            before = self._observer.observe_restart(case, phase="BEFORE_RESTART")
            restarted = self._controller.restart(control)
            if (type(restarted) is not RecoveryRestartReceipt
                    or restarted.case_id != case.case_id
                    or restarted.boundary_identity
                        != case.fixture.recovery_boundary
                    or restarted.operation_identity
                        != interruption.operation_identity
                    or restarted.work_identity != interruption.work_identity
                    or not restarted.restart_reopen_method):
                raise ValueError("restart/reopen receipt is contradictory")
            publication_reconciled = self._controller.reconcile_publication(control)
            if type(publication_reconciled) is not bool:
                raise ValueError("publication reconciliation receipt is invalid")
            after = self._observer.observe_restart(case, phase="AFTER_RECOVERY")
            operation_id = before.runtime_operation_id
            if (operation_id is None
                    or operation_id != interruption.operation_identity):
                raise ValueError("restart observation lacks authoritative operation identity")
            boundary = RecoveryBoundary(case.fixture.recovery_boundary)
            evaluation = evaluate_restart(
                before=before, after=after, expected_operation_id=operation_id,
                boundary=boundary,
                signal_injection_count=interruption.interruption_count,
                publication_reconciled_before_resume=publication_reconciled)
            disposition = (LatencyDisposition.OBSERVED_COMPLETION
                           if evaluation.passed else LatencyDisposition.FAILURE)
            measurement = restart_latency_measurement(
                before, after, expected_operation_id=operation_id,
                disposition=disposition,
                polling_interval_ms=self._polling_interval_ms,
                cutoff_ms=self._cutoff_ms, clock_source=self._clock_source)
            payload = {
                "kind": "RESTART_RECOVERY_RESULT", "case_id": case.case_id,
                "fixture_identity": case.fixture.fixture_identity,
                "variant_identity": case.fixture.variant,
                "boundary_identity": boundary.value,
                "disposition": evaluation.disposition,
                "passed": evaluation.passed, "details": list(evaluation.details),
                "before_state": _state_label(before),
                "resulting_state": _state_label(after),
                "recovered_identity": after.runtime_operation_id,
                "publication_continuity": (
                    set(before.state.publication_ids).issubset(
                        after.state.publication_ids)
                    and before.runtime_operation_id == after.runtime_operation_id),
                "duplicate_effect_detected": not evaluation.passed and any(
                    "duplicate" in item.lower() for item in evaluation.details),
                "latency_disposition": measurement.disposition.value,
                "latency_ms": measurement.duration_ms,
                "public_observation_commitments": [
                    before.observation_commitment, after.observation_commitment],
                "control_commitment": interruption.control_commitment,
                "exact_control_hook_identity":
                    interruption.exact_control_hook_identity,
                "operation_identity": interruption.operation_identity,
                "work_identity": interruption.work_identity,
                "restart_reopen_method": restarted.restart_reopen_method,
            }
        if case.kind == "REFRESH":
            fact = evaluate_and_issue_refresh_result(
                self._ledger, case_id=case.case_id,
                parent_execution_id=case.parent_execution_id,
                fixture_identity=case.fixture.fixture_identity,
                before=before, after_materiality=after_materiality,
                final=final, payload=payload)
        else:
            fact = evaluate_and_issue_restart_result(
                self._ledger, case_id=case.case_id,
                parent_execution_id=case.parent_execution_id,
                fixture_identity=case.fixture.fixture_identity,
                before=before, after=after,
                expected_operation_id=operation_id, boundary=boundary,
                signal_injection_count=interruption.interruption_count,
                publication_reconciled_before_resume=publication_reconciled,
                payload=payload)
        issue_deviation_result(
            self._ledger, source_evaluation_fact_id=fact.record_id,
            expected_public_observation_class=
                case.fixture.expected_public_observation_class)
        return fact


class CampaignRunner:
    """Arrange bounded executions; never schedule, retry, or recover production work.

    ``run`` is synchronous and caller-driven.  The supplied injector performs one
    scenario action.  All subsequent progress comes only from external reads.
    """

    def __init__(
        self,
        *,
        ledger: EvaluationLedger,
        clock: ObservationClock,
        observer: ExternalObserver,
        injector: SignalInjector,
        grader: Callable[[ExternalObservation], bool],
        publication_reconciler: Callable[[str], None] | None = None,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._observer = observer
        self._injector = injector
        self._grader = grader
        self._publication_reconciler = publication_reconciler or (lambda _: None)

    def run(
        self,
        *,
        admitted_plan: EvaluationPlan,
        campaign: CampaignPlan,
        manifest: ReproducibilityManifest,
        warmup: Callable[[], None] = lambda: None,
    ) -> tuple[ExecutionResult, ...]:
        """Run a generic/internal campaign; this can never be relabelled FORMAL."""
        campaign.validate(admitted_plan)
        manifest.validate_admission(admitted_plan, campaign)
        return self._run_admitted(
            admitted_plan=admitted_plan, campaign=campaign, manifest=manifest,
            warmup=warmup)

    def validate_formal_execution(
        self, *, admission_report: object,
        admitted_plan: EvaluationPlan, campaign: CampaignPlan,
        manifest: ReproducibilityManifest, execution_root: str | Path,
        execution_workspace: str | Path, repository_root: str | Path,
    ) -> None:
        """Side-effect-free validation shared by the sole formal entrypoint."""
        from .formal_admission import FormalAdmissionReport, inspect_repository

        if type(admission_report) is not FormalAdmissionReport:
            raise TypeError("Formal execution requires FormalAdmissionReport")
        if type(self._ledger) is not SqliteEvaluationStore:
            raise TypeError("Formal execution requires the Candidate-F durable store")
        admission_report.validate()
        if (admitted_plan != admission_report.plan
                or campaign != admission_report.campaign
                or manifest != admission_report.manifest):
            raise ValueError("formal plan/run/campaign/manifest admission mismatch")
        execution = Path(execution_root).resolve()
        workspace = Path(execution_workspace).resolve()
        repository = Path(repository_root).resolve()
        expected_root = Path(admission_report.evaluation_root)
        if execution != expected_root:
            raise ValueError("admitted evaluation root does not match execution root")
        if workspace != expected_root:
            raise ValueError("admitted workspace does not match execution workspace")
        if repository != Path(admission_report.repository_root):
            raise ValueError("admitted repository root does not match execution root")
        if self._ledger.path != Path(admission_report.database_path):
            raise ValueError("admitted DB does not match execution store")
        if self._ledger.path.parent != execution:
            raise ValueError("execution store is outside the admitted evaluation root")
        allowed = {self._ledger.path.name,
                   self._ledger.path.name + "-journal",
                   self._ledger.path.name + "-shm",
                   self._ledger.path.name + "-wal", "real_rag"}
        if (not execution.is_dir()
                or any(item.name not in allowed for item in execution.iterdir())):
            raise ValueError("formal fresh DB/workspace collision detected")
        if admission_report.preparation_id is None:
            raise ValueError("PREPARED is not executable; durable ADMITTED state required")
        self._ledger.require_admitted_preparation(
            admission_report.preparation_id,
            admission_report.admission_commitment)
        if not self._ledger.has_no_formal_execution_facts():
            existing = self._ledger.get(campaign.run_id)
            if existing is None:
                raise ValueError("foreign Formal execution facts already exist")
            self._ledger.require_formal_run(campaign.run_id)
            existing_payload = json.loads(existing.semantic_json)
            if (existing_payload.get("formal_admission_commitment") !=
                    admission_report.admission_commitment
                    or existing_payload.get("manifest_commitment") !=
                        manifest.manifest_commitment):
                raise ValueError("existing Formal execution contradicts admission")
        manifest.validate_formal_executable(
            admitted_plan, campaign,
            admission_report.reviewer_protocol.sealed_payload)
        state = inspect_repository(repository)
        expected_branch = manifest.approved_spec_commitments["repository_branch"]
        if (state.dirty or state.commit != manifest.repository_commit
                or state.branch != expected_branch):
            raise ValueError("formal execution repository baseline is not exact and pristine")

    def run_formal(
        self, *, admission_report: object,
        admitted_plan: EvaluationPlan, campaign: CampaignPlan,
        manifest: ReproducibilityManifest, execution_root: str | Path,
        execution_workspace: str | Path, repository_root: str | Path,
        preregistration_path: str | Path,
        candidate_c_authority: object | None = None,
        runtime_authority: object | None = None,
        docker_state_inspector: object | None = None,
        warmup: Callable[[], None] = lambda: None,
    ) -> tuple[ExecutionResult, ...]:
        """The sole official Formal Evaluation execution entrypoint."""
        from .formal_evidence import authorize_formal_execution

        # Fresh resolution happens inside the supported entrypoint immediately
        # before any Oracle, plan, run, denominator, signal, or provider fact.
        authorize_formal_execution(
            admission_report=admission_report,
            evaluation_store=self._ledger,
            preregistration_path=preregistration_path,
            repository_root=repository_root,
            execution_root=execution_root,
            execution_workspace=execution_workspace,
            candidate_c_authority=candidate_c_authority,
            runtime_authority=runtime_authority,
            docker_state_inspector=docker_state_inspector,
        )
        self.validate_formal_execution(
            admission_report=admission_report,
            admitted_plan=admitted_plan, campaign=campaign, manifest=manifest,
            execution_root=execution_root,
            execution_workspace=execution_workspace,
            repository_root=repository_root)
        # Validation above is complete before the first durable formal fact.
        self._ledger.admit_oracle(admission_report.oracle_revision)
        self._ledger.admit_plan(admitted_plan)
        run_record = self._create_run_record(
            admitted_plan=admitted_plan, campaign=campaign, manifest=manifest,
            execution_mode="FORMAL",
            formal_admission_commitment=admission_report.admission_commitment,
            formal_admission_payload=admission_report.admission_payload(),
        )
        self._ledger._append_formal_run(run_record)
        return self._run_admitted(
            admitted_plan=admitted_plan, campaign=campaign, manifest=manifest,
            warmup=warmup, run_already_admitted=True)

    def _run_admitted(
        self, *, admitted_plan: EvaluationPlan, campaign: CampaignPlan,
        manifest: ReproducibilityManifest, warmup: Callable[[], None],
        run_already_admitted: bool = False,
    ) -> tuple[ExecutionResult, ...]:
        """Execute admitted slots; the resume flag relies on existing ledger truth."""
        campaign.validate(admitted_plan)
        manifest.validate_admission(admitted_plan, campaign)
        if not run_already_admitted:
            run_record = self._create_run_record(
                admitted_plan=admitted_plan, campaign=campaign,
                manifest=manifest, execution_mode="GENERIC")
            self._ledger.append(run_record)
        else:
            run_record = self._ledger.get(campaign.run_id)
            if run_record is None:
                raise ValueError("validated formal run root was not issued")
            self._ledger.require_formal_run(campaign.run_id)
            payload = json.loads(run_record.semantic_json)
            if (run_record.parent_id != admitted_plan.evaluation_plan_id
                    or payload.get("campaign_key") != campaign.campaign_key
                    or payload.get("manifest_commitment")
                        != manifest.manifest_commitment
                    or payload.get("execution_order")
                        != [item.execution_id for item in campaign.executions]
                    or payload.get("fixed_n") != campaign.repetitions):
                raise ValueError("existing formal run conflicts with the admitted plan")
        # Admit the complete denominator before any signal is sent.
        for item in campaign.executions:
            self._ledger.append(LedgerRecord.create(
                "execution", [campaign.run_id, item.scenario_id, item.repetition],
                {
                    "scenario_id": item.scenario_id,
                    "repetition": item.repetition,
                    "input_identity": item.input_identity,
                    "input_commitment": item.input_commitment,
                    "case": item.case.value,
                    "manifest_commitment": manifest.manifest_commitment,
                    "planned_slot_execution_id": item.planned_slot_execution_id,
                }, campaign.run_id,
            ))
        warmup()
        # There is deliberately no result-dependent break or extension here.
        return tuple(self._execute(item, manifest) for item in campaign.executions)

    @staticmethod
    def _create_run_record(
        *, admitted_plan: EvaluationPlan, campaign: CampaignPlan,
        manifest: ReproducibilityManifest, execution_mode: str,
        formal_admission_commitment: str | None = None,
        formal_admission_payload: dict[str, object] | None = None,
    ) -> LedgerRecord:
        if execution_mode not in {"GENERIC", "FORMAL"}:
            raise ValueError("execution mode is invalid")
        if ((execution_mode == "FORMAL")
                != (formal_admission_commitment is not None
                    and formal_admission_payload is not None)):
            raise ValueError("formal run construction requires exact admission provenance")
        run_record = LedgerRecord.create(
            "evaluation_run", [admitted_plan.evaluation_plan_id, campaign.campaign_key],
            {
                "oracle_revision_id": admitted_plan.oracle_revision_id,
                "oracle_content_commitment": manifest.oracle_content_commitment,
                "manifest_commitment": manifest.manifest_commitment,
                "sealed_manifest": json.loads(manifest.canonical_json),
                "fixture_revision": manifest.fixture_revision,
                "production_build": manifest.production_build,
                "repository_commit": manifest.repository_commit,
                "repository_dirty": manifest.repository_dirty,
                "provider_profile_references": list(
                    manifest.provider_profile_references),
                "rag_profile_references": list(manifest.rag_profile_references),
                "corpus_index": manifest.corpus_index,
                "evaluation_config_commitment":
                    manifest.evaluation_config_commitment,
                "environment_identity": manifest.environment_identity,
                "evaluator_rubric_version": manifest.evaluator_rubric_version,
                "polling_interval_ms": manifest.polling_interval_ms,
                "timeout_ms": manifest.timeout_ms,
                "clock_source": manifest.clock_source,
                "aggregation_policy_version":
                    manifest.aggregation_policy_version,
                "campaign_key": manifest.campaign_key,
                "execution_order_strategy": manifest.execution_order_strategy,
                "early_stop_allowed": manifest.early_stop_allowed,
                "replacement_policy": manifest.replacement_policy,
                "reviewer_protocol": manifest.reviewer_protocol,
                "execution_mode": execution_mode,
                "formal_admission_commitment": formal_admission_commitment,
                "formal_admission": formal_admission_payload,
                "random_seed_or_order_commitment": (
                    manifest.random_seed if manifest.random_seed is not None
                    else manifest.deterministic_order_commitment),
                "model": manifest.model,
                "prompt": manifest.prompt,
                "result_schema": manifest.result_schema,
                "clock_synchronization_uncertainty_ms":
                    manifest.clock_synchronization_uncertainty_ms,
                **manifest.approved_spec_commitments,
                "execution_order": list(manifest.execution_ids),
                "fixed_n": admitted_plan.repetitions,
            }, admitted_plan.evaluation_plan_id,
        )
        if run_record.record_id != campaign.run_id:
            raise ValueError("campaign run identity is inconsistent")
        return run_record

    def run_replacement(
        self,
        *,
        admitted_plan: EvaluationPlan,
        campaign: CampaignPlan,
        invalid_execution_id: str,
        manifest: ReproducibilityManifest,
        warmup: Callable[[], None] = lambda: None,
    ) -> ExecutionResult:
        """Append one independently identified replacement for an invalid slot."""
        campaign.validate(admitted_plan)
        replacement = campaign.replacement_for(invalid_execution_id)
        manifest.validate_admission(admitted_plan, campaign, (replacement,))
        original_final = self._ledger.get(self._record_id(
            next(item for item in campaign.executions
                 if item.execution_id == invalid_execution_id), "final"))
        if original_final is None:
            raise ValueError("replacement requires an existing invalid final record")
        original_payload = json.loads(original_final.semantic_json)
        if original_payload.get("disposition") != ExecutionDisposition.INVALID_EVALUATION.value:
            raise ValueError("only INVALID_EVALUATION may receive a replacement")
        self._ledger.append(LedgerRecord.create(
            "execution", [campaign.run_id, replacement.scenario_id,
                          replacement.repetition, "replacement", invalid_execution_id],
            {
                "scenario_id": replacement.scenario_id,
                "repetition": replacement.repetition,
                "input_identity": replacement.input_identity,
                "input_commitment": replacement.input_commitment,
                "case": replacement.case.value,
                "manifest_commitment": manifest.manifest_commitment,
                "planned_slot_execution_id": invalid_execution_id,
                "replacement_for_execution_id": invalid_execution_id,
            }, campaign.run_id,
        ))
        warmup()
        return self._execute(replacement, manifest)

    def _execute(self, execution: PlannedExecution,
                 manifest: ReproducibilityManifest) -> ExecutionResult:
        final_id = self._record_id(execution, "final")
        existing_final = self._ledger.get(final_id)
        if existing_final is not None:
            return self._decode_result(execution, existing_final)

        signal_id = self._record_id(execution, "signal-started")
        signal_record = self._ledger.get(signal_id)
        if signal_record is None:
            signal_ns = self._clock.monotonic_ns()
            signal_utc = self._clock.utc_now()
            # Durable external start precedes the one and only injection action.
            self._ledger.append(LedgerRecord.create(
                "observation", [execution.execution_id, "signal-started"],
                {
                    "kind": "SIGNAL_STARTED",
                    "signal_started_at": signal_utc,
                    "signal_started_ns": signal_ns,
                    "polling_interval_ms": manifest.polling_interval_ms,
                    "cutoff_ms": manifest.timeout_ms,
                }, execution.execution_id,
            ))
            try:
                self._injector.inject(execution)
            except Exception as exc:
                return self._finish(execution, ExecutionResult(
                    execution, ExecutionDisposition.INVALID_EVALUATION, False,
                    KnowledgeDisposition.MATCH, execution.case, "SIGNAL_INJECTION",
                    signal_started_at=signal_utc,
                    detail=f"evaluation injection failed: {type(exc).__name__}",
                ))
        else:
            payload = json.loads(signal_record.semantic_json)
            signal_ns = payload["signal_started_ns"]
            signal_utc = payload["signal_started_at"]
            # Restart first asks existing publication reconciliation to converge;
            # the evaluation runner itself performs no production mutation.
            try:
                self._publication_reconciler(execution.execution_id)
                first = self._observer.observe(execution.execution_id)
            except Exception as exc:
                return self._finish(execution, ExecutionResult(
                    execution, ExecutionDisposition.INVALID_EVALUATION, False,
                    KnowledgeDisposition.MATCH, execution.case, "RECOVERY_RECONCILIATION",
                    signal_started_at=signal_utc,
                    detail=f"recovery observation failed closed: {type(exc).__name__}",
                ))
            if not (first.actionable_incident or first.usable_rca
                    or first.upstream.terminal_failure):
                return self._finish(execution, ExecutionResult(
                    execution, ExecutionDisposition.INVALID_EVALUATION, False,
                    first.upstream.knowledge, first.upstream.failure_case,
                    "RECOVERY_AMBIGUOUS", signal_started_at=signal_utc,
                    detail="durable signal boundary exists but production state is ambiguous; signal not replayed",
                ))
            return self._poll(execution, manifest, signal_ns, signal_utc, first)
        return self._poll(execution, manifest, signal_ns, signal_utc)

    def _poll(self, execution: PlannedExecution, manifest: ReproducibilityManifest,
              signal_ns: int, signal_utc: str,
              first: ExternalObservation | None = None) -> ExecutionResult:
        actionable_at = None
        actionable_ns = None
        pending = first
        while True:
            elapsed_ms = max(0, (self._clock.monotonic_ns() - signal_ns) // 1_000_000)
            if elapsed_ms > manifest.timeout_ms:
                trace = LatencyTrace(signal_utc, signal_ns, manifest.polling_interval_ms,
                                     manifest.timeout_ms, actionable_at, actionable_ns)
                return self._finish(execution, ExecutionResult(
                    execution, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED, False,
                    KnowledgeDisposition.MATCH, execution.case, "CUTOFF",
                    signal_started_at=trace.signal_started_at,
                    actionable_incident_observed_at=trace.actionable_incident_observed_at,
                    cutoff_ms=manifest.timeout_ms,
                    detail="unfinished at disclosed cutoff",
                ))
            try:
                observed = pending or self._observer.observe(execution.execution_id)
                pending = None
            except Exception as exc:
                return self._finish(execution, ExecutionResult(
                    execution, ExecutionDisposition.INVALID_EVALUATION, False,
                    KnowledgeDisposition.MATCH, execution.case, "EXTERNAL_OBSERVER",
                    signal_started_at=signal_utc,
                    detail=f"evaluation observer failed: {type(exc).__name__}",
                ))
            observed_ns = self._clock.monotonic_ns()
            observed_elapsed_ms = max(0, (observed_ns - signal_ns) // 1_000_000)
            if observed_elapsed_ms > manifest.timeout_ms:
                return self._finish(execution, ExecutionResult(
                    execution, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED, False,
                    observed.upstream.knowledge, observed.upstream.failure_case, "CUTOFF",
                    signal_started_at=signal_utc,
                    actionable_incident_observed_at=actionable_at,
                    cutoff_ms=manifest.timeout_ms,
                    detail="first external confirmation occurred after the disclosed cutoff",
                ))
            if observed.actionable_incident and actionable_at is None:
                actionable_at = observed.observed_at_utc
                actionable_ns = observed_ns
            # Typed authoritative non-success and integrity outcomes always take
            # precedence over an apparently usable public endpoint.
            if (observed.upstream.terminal_failure
                    or observed.upstream.knowledge in {
                        KnowledgeDisposition.INVALID,
                        KnowledgeDisposition.REPAIR_REQUIRED,
                    }
                    or (observed.usable_rca
                        and observed.upstream.status is not AuthoritativeOutcomeStatus.SUCCESS)):
                return self._finish(execution, ExecutionResult(
                    execution, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE, False,
                    observed.upstream.knowledge, observed.upstream.failure_case,
                    observed.upstream.failure_stage or "AUTHORITATIVE_NON_SUCCESS",
                    signal_started_at=signal_utc,
                    actionable_incident_observed_at=actionable_at,
                    detail="authoritative typed non-success observed",
                ))
            if observed.usable_rca:
                trace = LatencyTrace(
                    signal_utc, signal_ns, manifest.polling_interval_ms, manifest.timeout_ms,
                    actionable_at, actionable_ns, observed.observed_at_utc, observed_ns,
                )
                degraded = observed.upstream.valid_degraded_completion
                return self._finish(execution, ExecutionResult(
                    execution,
                    (ExecutionDisposition.VALID_DEGRADED_COMPLETION if degraded else
                     ExecutionDisposition.OBSERVED_COMPLETION),
                    bool(self._grader(observed)), observed.upstream.knowledge,
                    observed.upstream.failure_case,
                    observed.upstream.failure_stage or "USABLE_RCA",
                    signal_started_at=trace.signal_started_at,
                    actionable_incident_observed_at=trace.actionable_incident_observed_at,
                    usable_rca_observed_at=trace.usable_rca_observed_at,
                    latency_ms=trace.latency_ms,
                ))
            remaining = manifest.timeout_ms - elapsed_ms
            if remaining <= 0:
                # The cutoff is censoring, never a fabricated completion time.
                trace = LatencyTrace(signal_utc, signal_ns, manifest.polling_interval_ms,
                                     manifest.timeout_ms, actionable_at, actionable_ns)
                return self._finish(execution, ExecutionResult(
                    execution, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED, False,
                    observed.upstream.knowledge, observed.upstream.failure_case, "CUTOFF",
                    signal_started_at=trace.signal_started_at,
                    actionable_incident_observed_at=trace.actionable_incident_observed_at,
                    cutoff_ms=manifest.timeout_ms,
                    detail="unfinished at disclosed cutoff",
                ))
            self._clock.wait_ms(min(manifest.polling_interval_ms, remaining))

    def _finish(self, execution: PlannedExecution, result: ExecutionResult) -> ExecutionResult:
        payload = asdict(result)
        payload.pop("execution")
        payload["disposition"] = result.disposition.value
        payload["knowledge"] = result.knowledge.value
        payload["failure_case"] = result.failure_case.value
        payload["metric_disposition"] = result.metric_disposition.value
        self._ledger.append(LedgerRecord.create(
            "observation", [execution.execution_id, "final"], payload,
            execution.execution_id,
        ))
        return result

    @staticmethod
    def _record_id(execution: PlannedExecution, label: str) -> str:
        return LedgerRecord.create(
            "observation", [execution.execution_id, label], {}, execution.execution_id,
        ).record_id

    @staticmethod
    def _decode_result(execution: PlannedExecution, record: LedgerRecord) -> ExecutionResult:
        payload = json.loads(record.semantic_json)
        payload["disposition"] = ExecutionDisposition(payload["disposition"])
        payload["knowledge"] = KnowledgeDisposition(payload["knowledge"])
        payload["failure_case"] = FailureCase(payload["failure_case"])
        payload["metric_disposition"] = ApprovedMetricDisposition(
            payload["metric_disposition"])
        return ExecutionResult(execution=execution, **payload)
