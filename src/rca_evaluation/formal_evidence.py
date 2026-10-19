"""R4 Slice 6 evidence composition and the sole Formal execution entry guard.

This module never executes Docker, providers, scenarios, reviews, or a campaign.
It fresh-reads existing authorities and builds immutable Candidate-F projections.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Protocol

from .evidence_index import build_evidence_index
from .identity import commitment, identity, require_identity
from .ledger import LedgerRecord
from .manifest import ReproducibilityManifest, verify_run_manifest_binding
from .report_schema import FormalReport
from .reporting import build_formal_report
from .readiness import (
    resolve_formal_required_facts, resolve_materialized_formal_config,
)
from .sqlite_store import EvaluationLedgerReader, SqliteEvaluationStore


FORMAL_EVIDENCE_SCHEMA = "SPEC-017-R4-FORMAL-EVIDENCE-v1"
FORMAL_EXECUTION_ENTRY_SCHEMA = "SPEC-017-R4-FORMAL-EXECUTION-ENTRY-v1"
FORMAL_GATE_CONTRACT_VERSION = "SPEC-017-R4-GATE-EVIDENCE-v1"
FORMAL_GATE_RECEIPT_KIND = "FORMAL_GATE_RECEIPT"
_FORMAL_GATES = (
    "REAL_LLM", "REAL_RAG", "DOCKER_RCA_E2E", "SCENARIO_INJECTION",
    "RESTART_RECOVERY", "PUBLIC_OBSERVATION", "EVALUATOR_SCORING",
    "HUMAN_REVIEW",
)


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith(
            "sha256:"):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


class GateEvidenceState(str, Enum):
    NOT_EXECUTED = "NOT_EXECUTED"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INVALID = "INVALID"


class ClosureStage(str, Enum):
    ENGINEERING_COMPLETE = "ENGINEERING_COMPLETE"
    EVALUATION_READY = "EVALUATION_READY"
    EVALUATION_EXECUTED = "EVALUATION_EXECUTED"
    PRODUCT_ACCEPTANCE_COMPLETE = "PRODUCT_ACCEPTANCE_COMPLETE"


@dataclass(frozen=True, slots=True)
class FormalGateEvidence:
    gate: str
    gate_contract_version: str
    evaluation_run_id: str
    state: GateEvidenceState
    source_record_ids: tuple[str, ...]
    source_record_commitments: tuple[tuple[str, str], ...]
    durable_receipt_id: str | None
    durable_receipt_commitment: str | None
    source_commitment: str

    def validate(self) -> None:
        material = {
            "gate": self.gate,
            "gate_contract_version": self.gate_contract_version,
            "evaluation_run_id": self.evaluation_run_id,
            "state": self.state.value,
            "source_record_ids": list(self.source_record_ids),
            "source_record_commitments": [list(item) for item in
                                           self.source_record_commitments],
            "durable_receipt_id": self.durable_receipt_id,
            "durable_receipt_commitment": self.durable_receipt_commitment,
        }
        if (not self.gate or not self.gate_contract_version
                or not self.evaluation_run_id.startswith("eval:evaluation_run:")
                or tuple(item[0] for item in self.source_record_commitments)
                    != self.source_record_ids
                or (self.state is GateEvidenceState.PASS and (
                    not self.source_record_ids
                    or self.durable_receipt_id is None
                    or self.durable_receipt_commitment is None))
                or commitment(material) != self.source_commitment):
            raise ValueError("Formal gate evidence lineage is invalid")


@dataclass(frozen=True, slots=True)
class ManifestFreshnessVerification:
    current: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManifestCurrentState:
    """Bounded, secret-free result returned by a current-state authority."""

    repository_commit: str
    repository_dirty: bool
    raw_template_commitment: str
    evaluation_config_commitment: str
    oracle_revision_id: str
    oracle_content_commitment: str
    fixture_revision: str
    provider_profile_references: tuple[str, ...]
    rag_profile_references: tuple[str, ...]
    environment_identity: str
    docker_compose_commitment: str
    docker_config_commitment: str
    topology_commitment: str
    interruption_matrix_identity: str
    blockers: tuple[str, ...]
    state_commitment: str

    @classmethod
    def create(cls, **values: object) -> "ManifestCurrentState":
        material = dict(values)
        result = cls(**material, state_commitment=commitment(material))
        result.validate()
        return result

    def validate(self) -> None:
        material = asdict(self)
        claimed = material.pop("state_commitment")
        if commitment(material) != claimed:
            raise ValueError("current manifest authority result is contradictory")


class ManifestCurrentStateResolver(Protocol):
    def resolve(
        self, reader: EvaluationLedgerReader, evaluation_run_id: str,
        manifest: ReproducibilityManifest,
    ) -> ManifestCurrentState: ...


class RepositoryManifestCurrentStateResolver:
    """Fresh-read repository/config authorities without external execution."""

    def __init__(
        self, *, evaluation_store: SqliteEvaluationStore,
        repository_root: str | Path, preregistration_path: str | Path,
        candidate_c_authority: object | None = None,
        runtime_authority: object | None = None,
        docker_state_inspector: object | None = None,
    ) -> None:
        if type(evaluation_store) is not SqliteEvaluationStore:
            raise TypeError("Candidate-F SqliteEvaluationStore required")
        self._store = evaluation_store
        self._root = Path(repository_root).resolve()
        self._config_path = Path(preregistration_path).resolve()
        self._candidate_c_authority = candidate_c_authority
        self._runtime_authority = runtime_authority
        self._docker_state_inspector = docker_state_inspector

    def resolve(
        self, reader: EvaluationLedgerReader, evaluation_run_id: str,
        manifest: ReproducibilityManifest,
    ) -> ManifestCurrentState:
        from .evaluation_controls import (
            CONTROL_ARTIFACT_COMMITMENT, load_evaluation_control_artifact,
        )
        from .fixtures import load_fixture_manifest
        from .formal_admission import inspect_repository
        from .formal_compose import resolve_formal_compose_topology
        from .formal_docker_lifecycle import CONTROL_ARTIFACT_PATH
        from .oracle import load_official_oracle
        from .runtime_image_readiness import RuntimeImageReadinessResolver
        import yaml

        if type(reader) is not EvaluationLedgerReader:
            raise TypeError("store-issued EvaluationLedgerReader required")
        snapshot = reader.read_run_snapshot(evaluation_run_id)
        root_record = next(item for item in snapshot.records
                           if item.record_id == evaluation_run_id)
        run = json.loads(root_record.semantic_json)
        admission = run.get("formal_admission")
        preparation_id = (admission.get("preparation_id")
                          if isinstance(admission, dict) else None)
        if not isinstance(preparation_id, str):
            raise ValueError("durable preparation lineage is unavailable")

        raw_config = json.loads(self._config_path.read_text(encoding="utf-8"))
        prepared = self._store.get_preparation(preparation_id)
        if prepared is None:
            raise ValueError("durable preparation lineage is unavailable")
        raw_template_commitment = commitment(raw_config)
        if raw_template_commitment != prepared.get("preregistration_identity"):
            raise ValueError("current preregistration template drifted")
        resolved_config = resolve_materialized_formal_config(
            raw_config, repository_root=self._root,
            candidate_c_authority=self._candidate_c_authority,
            runtime_authority=self._runtime_authority,
            docker_state_inspector=self._docker_state_inspector,
            evaluation_store=self._store,
            preparation_id=preparation_id,
            _required_facts_resolver=resolve_formal_required_facts,
        )
        config = dict(resolved_config.materialized_config)
        repository = inspect_repository(self._root)
        oracle = load_official_oracle(
            (self._root / config["oracle_artifact"]).resolve())
        fixtures = load_fixture_manifest(
            (self._root / config["fixture_manifest"]).resolve(),
            oracle=oracle, repository_root=self._root)
        llm = yaml.safe_load((self._root /
            "configs/rca_evaluation/real_llm.yaml").read_text(encoding="utf-8"))
        rag = yaml.safe_load((self._root /
            "configs/rca_evaluation/real_rag.yaml").read_text(encoding="utf-8"))
        runtime_source = RuntimeImageReadinessResolver(self._root)._source()
        topology = resolve_formal_compose_topology(
            self._store, preparation_id, self._root)
        interruption = load_evaluation_control_artifact(
            self._root / CONTROL_ARTIFACT_PATH)
        if interruption.artifact_commitment != CONTROL_ARTIFACT_COMMITMENT:
            raise ValueError("current interruption matrix authority drifted")
        approved = config["manifest_inputs"]["approved_spec_commitments"]
        blockers = []
        if runtime_source.compose_commitment != approved.get("docker_compose_digest"):
            blockers.append("CURRENT_DOCKER_COMPOSE_DRIFT")
        if runtime_source.build_context_identity != approved.get("docker_config_digest"):
            blockers.append("CURRENT_DOCKER_CONFIG_DRIFT")
        return ManifestCurrentState.create(
            repository_commit=repository.commit,
            repository_dirty=repository.dirty,
            raw_template_commitment=raw_template_commitment,
            evaluation_config_commitment=(
                resolved_config.materialized_config_commitment),
            oracle_revision_id=oracle.revision.oracle_revision_id,
            oracle_content_commitment=oracle.revision.semantic_content_commitment,
            fixture_revision=fixtures.fixture_revision,
            provider_profile_references=(llm["profile_identity"],),
            rag_profile_references=(rag["profile_identity"],),
            environment_identity=config["manifest_inputs"]["environment_identity"],
            docker_compose_commitment=runtime_source.compose_commitment,
            docker_config_commitment=runtime_source.build_context_identity,
            topology_commitment=topology.topology_commitment,
            interruption_matrix_identity=interruption.artifact_commitment,
            blockers=tuple(blockers),
        )


@dataclass(frozen=True, slots=True)
class FormalReproducibilityManifest:
    """Secret-free closure manifest; the admitted manifest remains authoritative."""

    schema: str
    evaluation_run_id: str
    admitted_manifest_commitment: str
    evidence_index_commitment: str
    source_cut_commitment: str
    repository_commit: str
    oracle_revision_id: str
    oracle_content_commitment: str
    fixture_revision: str
    reviewer_protocol_commitment: str
    execution_order_commitment: str
    provider_profile_commitment: str
    rag_profile_commitment: str
    closure_manifest_commitment: str

    def validate(self) -> None:
        material = asdict(self)
        claimed = material.pop("closure_manifest_commitment")
        if (self.schema != FORMAL_EVIDENCE_SCHEMA
                or commitment(material) != claimed):
            raise ValueError("Formal reproducibility closure manifest is invalid")


@dataclass(frozen=True, slots=True)
class FormalExecutionEntryAuthorization:
    schema: str
    preparation_id: str
    evaluation_run_id: str
    admission_commitment: str
    manifest_commitment: str
    required_facts_commitment: str
    repository_commit: str
    replay_state: str
    authorization_commitment: str

    def validate(self) -> None:
        material = asdict(self)
        claimed = material.pop("authorization_commitment")
        if (self.schema != FORMAL_EXECUTION_ENTRY_SCHEMA
                or commitment(material) != claimed):
            raise ValueError("Formal execution entry authorization is invalid")


@dataclass(frozen=True, slots=True)
class FinalReadinessProjection:
    schema: str
    evaluation_run_id: str
    evidence_index_commitment: str
    manifest_commitment: str
    nominal_slot_count: int
    reconstructed_slot_count: int
    disposition_counts: tuple[tuple[str, int], ...]
    gates: tuple[FormalGateEvidence, ...]
    engineering_capability_ready: bool
    actual_external_facts_ready: bool
    closure_stage: ClosureStage | None
    blockers: tuple[str, ...]
    source_cut_commitment: str
    projection_commitment: str

    def validate(self) -> None:
        for gate in self.gates:
            gate.validate()
            if gate.evaluation_run_id != self.evaluation_run_id:
                raise ValueError("gate evidence belongs to another evaluation run")
        material = asdict(self)
        material["closure_stage"] = (
            self.closure_stage.value if self.closure_stage is not None else None)
        material["gates"] = [{
            **asdict(item), "state": item.state.value,
        } for item in self.gates]
        claimed = material.pop("projection_commitment")
        if (self.schema != FORMAL_EVIDENCE_SCHEMA
                or commitment(material) != claimed):
            raise ValueError("Final readiness projection is invalid")


def authorize_formal_execution(
    *, admission_report: object, evaluation_store: SqliteEvaluationStore,
    preregistration_path: str | Path, repository_root: str | Path,
    execution_root: str | Path, execution_workspace: str | Path,
    candidate_c_authority: object | None = None,
    runtime_authority: object | None = None,
    docker_state_inspector: object | None = None,
    container_real_rag_inspector: object | None = None,
) -> FormalExecutionEntryAuthorization:
    """Fresh-read every supported authority before the first execution effect."""
    from .formal_admission import FormalAdmissionReport, inspect_repository

    if type(admission_report) is not FormalAdmissionReport:
        raise TypeError("durable Final Formal Admission report required")
    if type(evaluation_store) is not SqliteEvaluationStore:
        raise TypeError("Candidate-F SqliteEvaluationStore required")
    admission_report.validate()
    if admission_report.preparation_id is None:
        raise ValueError("PREPARED/preflight state cannot authorize execution")
    root = Path(repository_root).resolve()
    execution = Path(execution_root).resolve()
    workspace = Path(execution_workspace).resolve()
    if (root != Path(admission_report.repository_root)
            or execution != Path(admission_report.evaluation_root)
            or workspace != execution
            or evaluation_store.path != Path(admission_report.database_path)
            or evaluation_store.path.parent != execution):
        raise ValueError("Formal execution namespace contradicts Final Admission")
    evaluation_store.require_admitted_preparation(
        admission_report.preparation_id, admission_report.admission_commitment)

    existing_run = evaluation_store.get(admission_report.campaign.run_id)
    replay_state = "NEW_EXECUTION"
    if existing_run is None:
        if not evaluation_store.has_no_formal_execution_facts():
            raise ValueError("foreign Formal execution facts already exist")
    else:
        evaluation_store.require_formal_run(admission_report.campaign.run_id)
        existing_payload = json.loads(existing_run.semantic_json)
        if (existing_payload.get("formal_admission_commitment") !=
                admission_report.admission_commitment
                or existing_payload.get("manifest_commitment") !=
                    admission_report.manifest.manifest_commitment):
            raise ValueError("existing Formal execution contradicts admission")
        replay_state = "EXACT_RECOVERY_CONTINUATION"

    config = json.loads(Path(preregistration_path).resolve().read_text(
        encoding="utf-8"))
    current_raw_commitment = commitment(config)
    prepared = evaluation_store.get_preparation(admission_report.preparation_id)
    if (prepared is None
            or current_raw_commitment != prepared.get("preregistration_identity")
            or current_raw_commitment != admission_report.preregistration_commitment):
        raise ValueError("pre-registration drift detected at execution entry")
    repository = inspect_repository(root)
    expected_branch = admission_report.manifest.approved_spec_commitments[
        "repository_branch"]
    if (repository.dirty or repository.commit != admission_report.manifest.repository_commit
            or repository.branch != expected_branch):
        raise ValueError("repository drift detected at execution entry")

    from .preparation import validate_prepared_resource_freshness
    validate_prepared_resource_freshness(
        store=evaluation_store,
        preparation_id=admission_report.preparation_id,
        repository_root=root, runtime_authority=runtime_authority,
        docker_state_inspector=docker_state_inspector,
        candidate_c_authority=candidate_c_authority,
        container_real_rag_inspector=container_real_rag_inspector)

    fresh = resolve_materialized_formal_config(
        config, repository_root=root,
        candidate_c_authority=candidate_c_authority,
        runtime_authority=runtime_authority,
        docker_state_inspector=docker_state_inspector,
        container_real_rag_inspector=container_real_rag_inspector,
        evaluation_store=evaluation_store,
        preparation_id=admission_report.preparation_id,
        _required_facts_resolver=resolve_formal_required_facts,
    )
    if (fresh.materialized_config_commitment !=
            admission_report.materialized_config_commitment
            or fresh.materialized_config_commitment !=
                admission_report.manifest.evaluation_config_commitment):
        raise ValueError("materialized Formal configuration drift detected")
    admission_report.manifest.validate_formal_executable(
        admission_report.plan, admission_report.campaign,
        admission_report.reviewer_protocol.sealed_payload)
    if len(admission_report.campaign.executions) != 30:
        raise ValueError("Formal execution entry requires all 30 nominal slots")

    material = {
        "schema": FORMAL_EXECUTION_ENTRY_SCHEMA,
        "preparation_id": admission_report.preparation_id,
        "evaluation_run_id": admission_report.campaign.run_id,
        "admission_commitment": admission_report.admission_commitment,
        "manifest_commitment": admission_report.manifest.manifest_commitment,
        "required_facts_commitment":
            fresh.required_facts_commitment,
        "repository_commit": repository.commit,
        "replay_state": replay_state,
    }
    result = FormalExecutionEntryAuthorization(
        **material, authorization_commitment=commitment(material))
    result.validate()
    return result


def verify_manifest_freshness(
    reader: EvaluationLedgerReader, evaluation_run_id: str,
    manifest: ReproducibilityManifest, *,
    current_state_resolver: ManifestCurrentStateResolver | None = None,
) -> ManifestFreshnessVerification:
    """Verify the supplied manifest against a fresh authoritative run cut."""
    if type(reader) is not EvaluationLedgerReader:
        raise TypeError("store-issued EvaluationLedgerReader required")
    if type(manifest) is not ReproducibilityManifest:
        raise TypeError("ReproducibilityManifest required")
    blockers: list[str] = []
    try:
        manifest.validate()
    except (TypeError, ValueError):
        blockers.append("MANIFEST_INTEGRITY_CONTRADICTION")
    snapshot = reader.read_run_snapshot(evaluation_run_id)
    root = next(record for record in snapshot.records
                if record.record_id == evaluation_run_id)
    payload = json.loads(root.semantic_json)
    durable_admission = payload.get("formal_admission")
    expected_topology = (durable_admission.get(
        "expected_topology_commitment")
        if isinstance(durable_admission, dict) else None)
    expected_interruption = (durable_admission.get(
        "expected_interruption_matrix_identity")
        if isinstance(durable_admission, dict) else None)
    expected_raw_template = (durable_admission.get(
        "preregistration_commitment")
        if isinstance(durable_admission, dict) else None)
    expected_materialized_config = (durable_admission.get(
        "materialized_config_commitment")
        if isinstance(durable_admission, dict) else None)
    if not _is_sha256(expected_topology):
        blockers.append("EXPECTED_TOPOLOGY_COMMITMENT_UNAVAILABLE")
    if not _is_sha256(expected_interruption):
        blockers.append("EXPECTED_INTERRUPTION_MATRIX_IDENTITY_UNAVAILABLE")
    if not _is_sha256(expected_raw_template):
        blockers.append("EXPECTED_RAW_TEMPLATE_COMMITMENT_UNAVAILABLE")
    if not _is_sha256(expected_materialized_config):
        blockers.append("EXPECTED_MATERIALIZED_CONFIG_COMMITMENT_UNAVAILABLE")
    elif expected_materialized_config != manifest.evaluation_config_commitment:
        blockers.append("EXPECTED_MATERIALIZED_CONFIG_CONTRADICTION")
    execution_order = tuple(payload.get("execution_order") or ())
    execution_payloads = {
        record.record_id: json.loads(record.semantic_json)
        for record in snapshot.records if record.domain == "execution"
        and record.record_id in set(execution_order)
    }
    executions = tuple((execution_id, execution_payloads[execution_id])
                       for execution_id in execution_order
                       if execution_id in execution_payloads)
    verification = verify_run_manifest_binding(
        payload, evaluation_run_id=evaluation_run_id,
        evaluation_plan_id=snapshot.evaluation_plan_id,
        oracle_revision_id=snapshot.oracle_revision_id,
        executions=executions)
    blockers.extend(verification.blockers)
    if payload.get("manifest_commitment") != manifest.manifest_commitment:
        blockers.append("MANIFEST_COMMITMENT_DRIFT")
    if payload.get("sealed_manifest") != json.loads(manifest.canonical_json):
        blockers.append("MANIFEST_CONTENT_DRIFT")
    index = build_evidence_index(snapshot)
    for actual, expected, label in (
        (index.repository_identity, manifest.repository_commit, "SOURCE"),
        (index.oracle_content_commitment, manifest.oracle_content_commitment,
         "ORACLE"),
        (index.fixture_revision, manifest.fixture_revision, "FIXTURE"),
    ):
        if actual != expected:
            blockers.append(f"{label}_DRIFT")
    if current_state_resolver is None:
        blockers.append("CURRENT_STATE_AUTHORITY_UNAVAILABLE")
    else:
        try:
            current = current_state_resolver.resolve(
                reader, evaluation_run_id, manifest)
            if type(current) is not ManifestCurrentState:
                raise TypeError("typed current manifest state required")
            current.validate()
            blockers.extend(current.blockers)
            comparisons = (
                (current.repository_commit, manifest.repository_commit,
                 "CURRENT_REPOSITORY_REVISION_DRIFT"),
                (current.repository_dirty, manifest.repository_dirty,
                 "CURRENT_REPOSITORY_DIRTY_STATE_DRIFT"),
                (current.raw_template_commitment, expected_raw_template,
                 "CURRENT_RAW_TEMPLATE_DRIFT"),
                (current.evaluation_config_commitment,
                 expected_materialized_config,
                 "CURRENT_CONFIG_DRIFT"),
                (current.oracle_revision_id, manifest.oracle_revision_id,
                 "CURRENT_ORACLE_REVISION_DRIFT"),
                (current.oracle_content_commitment,
                 manifest.oracle_content_commitment,
                 "CURRENT_ORACLE_CONTENT_DRIFT"),
                (current.fixture_revision, manifest.fixture_revision,
                 "CURRENT_FIXTURE_DRIFT"),
                (current.provider_profile_references,
                 manifest.provider_profile_references,
                 "CURRENT_LLM_PROFILE_DRIFT"),
                (current.rag_profile_references,
                 manifest.rag_profile_references,
                 "CURRENT_RAG_PROFILE_DRIFT"),
                (current.environment_identity, manifest.environment_identity,
                 "CURRENT_DOCKER_TOPOLOGY_DRIFT"),
                (current.docker_compose_commitment,
                 manifest.approved_spec_commitments.get(
                     "docker_compose_digest"),
                 "CURRENT_DOCKER_COMPOSE_DRIFT"),
                (current.docker_config_commitment,
                 manifest.approved_spec_commitments.get("docker_config_digest"),
                 "CURRENT_DOCKER_CONFIG_DRIFT"),
            )
            for actual, expected, label in comparisons:
                if actual != expected:
                    blockers.append(label)
            if not _is_sha256(current.topology_commitment):
                blockers.append("CURRENT_TOPOLOGY_COMMITMENT_MALFORMED")
            elif current.topology_commitment != expected_topology:
                blockers.append("CURRENT_DOCKER_TOPOLOGY_DRIFT")
            if not _is_sha256(current.interruption_matrix_identity):
                blockers.append("CURRENT_INTERRUPTION_MATRIX_IDENTITY_MALFORMED")
            elif current.interruption_matrix_identity != expected_interruption:
                blockers.append("CURRENT_INTERRUPTION_MATRIX_DRIFT")
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError,
                ValueError):
            blockers.append("CURRENT_STATE_AUTHORITY_UNRESOLVED")
    return ManifestFreshnessVerification(
        not blockers, tuple(sorted(set(blockers))))


def build_reproducibility_closure_manifest(
    reader: EvaluationLedgerReader, evaluation_run_id: str,
    manifest: ReproducibilityManifest, *,
    current_state_resolver: ManifestCurrentStateResolver | None = None,
) -> FormalReproducibilityManifest:
    """Bind the pre-execution manifest to the current immutable evidence cut."""
    freshness = verify_manifest_freshness(
        reader, evaluation_run_id, manifest,
        current_state_resolver=current_state_resolver)
    if not freshness.current:
        raise ValueError("stale reproducibility manifest: " +
                         ", ".join(freshness.blockers))
    report = build_formal_report(reader, evaluation_run_id)
    material = {
        "schema": FORMAL_EVIDENCE_SCHEMA,
        "evaluation_run_id": evaluation_run_id,
        "admitted_manifest_commitment": manifest.manifest_commitment,
        "evidence_index_commitment":
            report.evidence_index.evidence_index_commitment,
        "source_cut_commitment": report.source_cut_commitment,
        "repository_commit": manifest.repository_commit,
        "oracle_revision_id": manifest.oracle_revision_id,
        "oracle_content_commitment": manifest.oracle_content_commitment,
        "fixture_revision": manifest.fixture_revision,
        "reviewer_protocol_commitment": commitment(manifest.reviewer_protocol),
        "execution_order_commitment": commitment(list(manifest.execution_ids)),
        "provider_profile_commitment": commitment(
            list(manifest.provider_profile_references)),
        "rag_profile_commitment": commitment(
            list(manifest.rag_profile_references)),
    }
    result = FormalReproducibilityManifest(
        **material, closure_manifest_commitment=commitment(material))
    result.validate()
    return result


def _expected_gate_material(
    report: FormalReport,
) -> dict[str, tuple[GateEvidenceState, tuple[str, ...]]]:
    gates: dict[str, tuple[GateEvidenceState, tuple[str, ...]]] = {}
    for item in report.real_gates:
        state = {
            "NOT_EXECUTED": GateEvidenceState.NOT_EXECUTED,
            "EXECUTED_PASS": GateEvidenceState.PASS,
            "EXECUTED_FAIL": GateEvidenceState.FAIL,
            "INVALID_EXECUTION": GateEvidenceState.INVALID,
            "EXECUTION_STARTED_INCOMPLETE": GateEvidenceState.BLOCKED,
        }.get(item.state, GateEvidenceState.INVALID)
        ids = (*item.authorization_ids, *item.invocation_start_ids,
               *item.terminal_receipt_ids, *item.docker_namespace_ids,
               *item.docker_cleanup_ids)
        gates[item.gate] = (state, tuple(ids))

    rows = report.executions
    observed = sum(item.disposition is not None for item in rows)
    scored = sum(item.root_cause_success is not None
                 and item.unsupported_claim_denominator is not None
                 and item.conclusion_calibration_valid is not None
                 for item in rows)
    reviewed = len(report.original_judgments)
    category_by_parent: dict[str, set[str]] = {}
    ids_by_parent: dict[str, list[str]] = {}
    for entry in report.evidence_index.entries:
        if entry.parent_id is not None:
            category_by_parent.setdefault(entry.parent_id, set()).add(entry.category)
            ids_by_parent.setdefault(entry.parent_id, []).append(entry.record_id)
    nominal_ids = tuple(item.execution_id for item in rows
                        if item.replacement_for_execution_id is None)
    injection_ids = tuple(
        entry.record_id for entry in report.evidence_index.entries
        if entry.parent_id in set(nominal_ids)
        and entry.category in {"SIGNAL", "EVENT_BINDING"})
    observation_ids = tuple(
        entry.record_id for entry in report.evidence_index.entries
        if entry.parent_id in set(nominal_ids)
        and entry.category in {
            "INCIDENT_OBSERVATION", "RCA_OBSERVATION", "LATENCY"})
    scoring_ids = tuple(
        entry.record_id for entry in report.evidence_index.entries
        if entry.category == "SCORING")
    injection_complete = bool(nominal_ids) and len(nominal_ids) == 30 and all(
        {"SIGNAL", "EVENT_BINDING"} <= category_by_parent.get(execution_id, set())
        for execution_id in nominal_ids)
    injection_started = any(
        "SIGNAL" in category_by_parent.get(execution_id, set())
        for execution_id in nominal_ids)
    derived = (
        ("SCENARIO_INJECTION", GateEvidenceState.NOT_EXECUTED
         if not injection_started else GateEvidenceState.PASS
         if injection_complete else GateEvidenceState.BLOCKED, injection_ids),
        ("RESTART_RECOVERY", GateEvidenceState.PASS
         if report.restart_recovery_results.complete
         and report.restart_recovery_results.passed else GateEvidenceState.FAIL
         if report.restart_recovery_results.complete else
         GateEvidenceState.NOT_EXECUTED,
         report.restart_recovery_results.evidence_record_ids),
        ("PUBLIC_OBSERVATION", GateEvidenceState.NOT_EXECUTED
         if observed == 0 else GateEvidenceState.PASS
         if observed == len(rows) == 30 else GateEvidenceState.BLOCKED, ()),
        ("EVALUATOR_SCORING", GateEvidenceState.NOT_EXECUTED
         if scored == 0 else GateEvidenceState.PASS
         if scored == len(rows) == 30 else GateEvidenceState.BLOCKED, ()),
        ("HUMAN_REVIEW", GateEvidenceState.NOT_EXECUTED
         if reviewed == 0 else GateEvidenceState.PASS
         if reviewed == len(rows) == 30 else GateEvidenceState.BLOCKED,
         tuple(item.judgment_id for item in report.original_judgments)),
    )
    derived = tuple(
        (name, state,
         observation_ids if name == "PUBLIC_OBSERVATION" else
         scoring_ids if name == "EVALUATOR_SCORING" else ids)
        for name, state, ids in derived)
    gates.update((name, (state, tuple(ids))) for name, state, ids in derived)
    return gates


def _receipt_id(evaluation_run_id: str, gate: str) -> str:
    return identity("report", [evaluation_run_id, FORMAL_GATE_RECEIPT_KIND, gate])


def _gate_projection(
    *, gate: str, evaluation_run_id: str, state: GateEvidenceState,
    source_records: tuple[LedgerRecord, ...], receipt: LedgerRecord | None,
) -> FormalGateEvidence:
    lineage = tuple((item.record_id, item.semantic_commitment)
                    for item in source_records)
    material = {
        "gate": gate,
        "gate_contract_version": FORMAL_GATE_CONTRACT_VERSION,
        "evaluation_run_id": evaluation_run_id,
        "state": state.value,
        "source_record_ids": [item[0] for item in lineage],
        "source_record_commitments": [list(item) for item in lineage],
        "durable_receipt_id": receipt.record_id if receipt is not None else None,
        "durable_receipt_commitment": (
            receipt.semantic_commitment if receipt is not None else None),
    }
    result = FormalGateEvidence(
        gate, FORMAL_GATE_CONTRACT_VERSION, evaluation_run_id, state,
        tuple(item[0] for item in lineage), lineage,
        receipt.record_id if receipt is not None else None,
        receipt.semantic_commitment if receipt is not None else None,
        commitment(material))
    result.validate()
    return result


def _read_durable_gate_receipt(
    reader: EvaluationLedgerReader, evaluation_run_id: str, gate: str,
    expected_state: GateEvidenceState, expected_source_ids: tuple[str, ...],
) -> FormalGateEvidence | None:
    receipt = reader.get(_receipt_id(evaluation_run_id, gate))
    if receipt is None:
        return None
    if receipt.domain != "report" or receipt.parent_id != evaluation_run_id:
        raise ValueError("durable gate receipt authority is invalid")
    payload = json.loads(receipt.semantic_json)
    required = {
        "kind", "schema", "gate", "gate_contract_version",
        "evaluation_run_id", "execution_ids", "authoritative_sources",
        "state", "operation_identity", "lineage_commitment",
    }
    if (set(payload) != required
            or payload["kind"] != FORMAL_GATE_RECEIPT_KIND
            or payload["schema"] != FORMAL_EVIDENCE_SCHEMA
            or payload["gate"] != gate
            or payload["gate_contract_version"] != FORMAL_GATE_CONTRACT_VERSION
            or payload["evaluation_run_id"] != evaluation_run_id
            or payload["state"] != expected_state.value
            or payload["operation_identity"] != commitment({
                "evaluation_run_id": evaluation_run_id,
                "gate": gate,
                "gate_contract_version": FORMAL_GATE_CONTRACT_VERSION,
            })):
        raise ValueError("durable gate receipt lineage is contradictory")
    sources = payload["authoritative_sources"]
    if not isinstance(sources, list) or any(
            not isinstance(item, dict)
            or set(item) != {"record_id", "domain", "commitment"}
            for item in sources):
        raise ValueError("durable gate source lineage is malformed")
    snapshot_ids = {item.record_id for item in
                    reader.read_run_snapshot(evaluation_run_id).records}
    records = []
    for item in sources:
        source = reader.get(item["record_id"])
        if (source is None or source.domain != item["domain"]
                or source.semantic_commitment != item["commitment"]
                or source.record_id not in snapshot_ids):
            raise ValueError("durable gate source is absent or contradictory")
        records.append(source)
    source_ids = tuple(item.record_id for item in records)
    execution_ids = sorted({
        execution_id for source in records
        for execution_id in _source_execution_ids(source)
    })
    if (source_ids != expected_source_ids
            or payload["execution_ids"] != execution_ids
            or payload["lineage_commitment"] != commitment(sources)
            or (expected_state is GateEvidenceState.PASS and not records)):
        raise ValueError("durable gate PASS lacks authoritative source lineage")
    return _gate_projection(
        gate=gate, evaluation_run_id=evaluation_run_id,
        state=expected_state, source_records=tuple(records), receipt=receipt)


def _source_execution_ids(record: LedgerRecord) -> tuple[str, ...]:
    values = []
    if record.parent_id is not None and record.parent_id.startswith(
            "eval:execution:"):
        values.append(record.parent_id)
    payload = json.loads(record.semantic_json)
    execution_id = payload.get("execution_id")
    if isinstance(execution_id, str):
        require_identity(execution_id, "execution")
        values.append(execution_id)
    return tuple(sorted(set(values)))


def append_durable_gate_receipt(
    store: SqliteEvaluationStore, evaluation_run_id: str, gate: str,
) -> FormalGateEvidence:
    """Derive, append, and read back one authoritative Candidate-F gate receipt."""
    if type(store) is not SqliteEvaluationStore:
        raise TypeError("Candidate-F SqliteEvaluationStore required")
    if gate not in _FORMAL_GATES:
        raise ValueError("unknown Formal gate")
    store.require_formal_run(evaluation_run_id)
    reader = store.reader()
    report = build_formal_report(reader, evaluation_run_id)
    state, source_ids = _expected_gate_material(report)[gate]
    if state is GateEvidenceState.PASS and not source_ids:
        raise ValueError("Formal gate PASS requires authoritative source evidence")
    records = []
    for record_id in source_ids:
        source = reader.get(record_id)
        if source is None:
            raise ValueError("authoritative gate source record is unavailable")
        records.append(source)
    sources = [{"record_id": item.record_id, "domain": item.domain,
                "commitment": item.semantic_commitment} for item in records]
    execution_ids = sorted({
        execution_id for item in records
        for execution_id in _source_execution_ids(item)
    })
    payload = {
        "kind": FORMAL_GATE_RECEIPT_KIND,
        "schema": FORMAL_EVIDENCE_SCHEMA,
        "gate": gate,
        "gate_contract_version": FORMAL_GATE_CONTRACT_VERSION,
        "evaluation_run_id": evaluation_run_id,
        "execution_ids": execution_ids,
        "authoritative_sources": sources,
        "state": state.value,
        "operation_identity": commitment({
            "evaluation_run_id": evaluation_run_id,
            "gate": gate,
            "gate_contract_version": FORMAL_GATE_CONTRACT_VERSION,
        }),
        "lineage_commitment": commitment(sources),
    }
    store.append(LedgerRecord.create(
        "report", [evaluation_run_id, FORMAL_GATE_RECEIPT_KIND, gate],
        payload, evaluation_run_id))
    result = _read_durable_gate_receipt(
        store.reader(), evaluation_run_id, gate, state, source_ids)
    if result is None:
        raise RuntimeError("durable gate receipt read-back failed")
    return result


def _gate_evidence(
    reader: EvaluationLedgerReader, report: FormalReport,
) -> tuple[FormalGateEvidence, ...]:
    expected = _expected_gate_material(report)
    gates = []
    for gate in _FORMAL_GATES:
        state, source_ids = expected[gate]
        durable = _read_durable_gate_receipt(
            reader, report.evaluation_run_id, gate, state, source_ids)
        if durable is not None:
            gates.append(durable)
            continue
        records = tuple(reader.get(item) for item in source_ids)
        if any(item is None for item in records):
            raise ValueError("gate projection source record is unavailable")
        # A derived PASS is not closure authority until Candidate-F durably
        # appends and reads back its source-bound receipt.
        projection_state = (GateEvidenceState.BLOCKED
                            if state is GateEvidenceState.PASS else state)
        gates.append(_gate_projection(
            gate=gate, evaluation_run_id=report.evaluation_run_id,
            state=projection_state,
            source_records=tuple(records), receipt=None))
    return tuple(gates)


def build_final_readiness_projection(
    reader: EvaluationLedgerReader, evaluation_run_id: str,
    manifest: ReproducibilityManifest, *,
    current_state_resolver: ManifestCurrentStateResolver | None = None,
) -> FinalReadinessProjection:
    """Rebuild closure state from Candidate-F; no projection is an authority."""
    if type(reader) is not EvaluationLedgerReader:
        raise TypeError("store-issued EvaluationLedgerReader required")
    report = build_formal_report(reader, evaluation_run_id)
    freshness = verify_manifest_freshness(
        reader, evaluation_run_id, manifest,
        current_state_resolver=current_state_resolver)
    snapshot = reader.read_run_snapshot(evaluation_run_id)
    root = next(json.loads(record.semantic_json) for record in snapshot.records
                if record.record_id == evaluation_run_id)
    admitted = tuple(root.get("execution_order") or ())
    rows = {item.execution_id: item for item in report.executions}
    formal_rows = tuple(rows[item] for item in admitted if item in rows)
    counts: dict[str, int] = {}
    for row in formal_rows:
        value = row.disposition or "NOT_EXECUTED"
        counts[value] = counts.get(value, 0) + 1
    blockers = list(freshness.blockers)
    if len(admitted) != 30 or len(formal_rows) != 30 or len(set(admitted)) != 30:
        blockers.append("NOMINAL_30_SLOT_RECONSTRUCTION_INCOMPLETE")
    if any(row.replacement_for_execution_id is not None
           for row in report.executions):
        blockers.append("NOMINAL_SLOT_REPLACEMENT_CONTRADICTION")
    gates = _gate_evidence(reader, report)
    engineering_ready = (
        freshness.current and report.readiness.engineering_implementation_complete
        and len(admitted) == len(formal_rows) == 30
        and len(set(admitted)) == 30
        and not any(row.replacement_for_execution_id is not None
                    for row in report.executions))
    if not engineering_ready:
        blockers.append("ENGINEERING_CAPABILITY_INCOMPLETE")
    external_ready = (
        report.readiness.evaluation_ready
        and all(item.state is GateEvidenceState.PASS for item in gates))
    stage = None
    if engineering_ready:
        stage = ClosureStage.ENGINEERING_COMPLETE
    if external_ready:
        stage = ClosureStage.EVALUATION_READY
    if external_ready and report.readiness.evaluation_executed:
        stage = ClosureStage.EVALUATION_EXECUTED
    material = {
        "schema": FORMAL_EVIDENCE_SCHEMA,
        "evaluation_run_id": evaluation_run_id,
        "evidence_index_commitment":
            report.evidence_index.evidence_index_commitment,
        "manifest_commitment": manifest.manifest_commitment,
        "nominal_slot_count": 30,
        "reconstructed_slot_count": len(formal_rows),
        "disposition_counts": tuple(sorted(counts.items())),
        "gates": [{**asdict(item), "state": item.state.value}
                  for item in gates],
        "engineering_capability_ready": engineering_ready,
        "actual_external_facts_ready": external_ready,
        "closure_stage": stage.value if stage is not None else None,
        "blockers": tuple(sorted(set(blockers))),
        "source_cut_commitment": report.source_cut_commitment,
    }
    result = FinalReadinessProjection(
        FORMAL_EVIDENCE_SCHEMA, evaluation_run_id,
        report.evidence_index.evidence_index_commitment,
        manifest.manifest_commitment, 30, len(formal_rows),
        tuple(sorted(counts.items())), gates, engineering_ready,
        external_ready, stage, tuple(sorted(set(blockers))),
        report.source_cut_commitment, commitment(material))
    result.validate()
    return result


__all__ = [
    "ClosureStage", "FinalReadinessProjection", "FormalExecutionEntryAuthorization",
    "FormalGateEvidence", "FormalReproducibilityManifest", "GateEvidenceState",
    "ManifestCurrentState", "ManifestCurrentStateResolver",
    "RepositoryManifestCurrentStateResolver", "ManifestFreshnessVerification",
    "append_durable_gate_receipt", "authorize_formal_execution",
    "build_final_readiness_projection", "build_reproducibility_closure_manifest",
    "verify_manifest_freshness",
]
