"""R2 official Oracle, §8.4 aggregation, latency, and public resolver contracts."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from incident_evidence.contracts import MaterialityJudgement
from incident_management.contracts import IncidentRcaPublicationDisposition
from rca_persistence.contracts import (
    CurrentFreshness, LogicalTryResultKind, PublicationDisposition,
)
from runtime_orchestration.contracts import RuntimeWorkStatus

from rca_evaluation.aggregation import (
    BOOTSTRAP_ALGORITHM, aggregate,
)
from rca_evaluation.authority import (
    PM_AUTHORITY, TrustedApprovalRegistry, TrustedOracleApproval,
)
from rca_evaluation.campaign import (
    ApprovedMetricDisposition, CampaignAdmission, CampaignPlan,
    ExecutionDisposition, ExecutionResult, FailureCase, KnowledgeDisposition,
    PlannedExecution,
)
from rca_evaluation.fixtures import (
    AdmittedFixture, FixtureAdmissionRequest, OfficialFixtureManifest,
    REQUIRED_VARIANTS, REQUIRED_VARIANT_INVENTORY, load_fixture_manifest,
)
from rca_evaluation.identity import commitment, identity
from rca_evaluation.latency import (
    LatencyDisposition, LatencyKind, primary_latency_measurements,
    summarize_latency,
)
from rca_evaluation.oracle import (
    APPROVED_SPEC_COMMIT, APPROVED_SPEC_DOCUMENT, APPROVED_SPEC_SECTION,
    EvaluationPlan, OracleRevision, load_official_oracle,
)
from rca_evaluation.recovery_evaluation import (
    Freshness, Materiality, RcaPublicState, RcaPublicStateResolver,
    RecoveryBoundary, RecoveryStateReference, evaluate_refresh,
    evaluate_restart, refresh_latency_measurement, restart_latency_measurement,
)
from rca_evaluation.sqlite_store import SqliteEvaluationStore


ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / "configs/rca_evaluation/oracle/spec017_s1_s6_v1.json"
FIXTURE_PATH = (
    ROOT / "configs/rca_evaluation/fixtures/"
    "spec017_s1_s6_fixture_manifest_v1.json"
)
OFFICIAL_ORACLE = load_official_oracle(ORACLE_PATH)


def test_official_oracle_is_complete_pinned_deterministic_and_s1_admissible(tmp_path):
    first = load_official_oracle(ORACLE_PATH)
    second = load_official_oracle(ORACLE_PATH)
    semantics = json.loads(first.revision.semantic_json)
    assert set(semantics["scenarios"]) == {f"S{i}" for i in range(1, 7)}
    assert all(set(value) == {f"O{i}" for i in range(1, 11)}
               for value in semantics["scenarios"].values())
    assert (first.source_document, first.source_section, first.approved_commit) == (
        APPROVED_SPEC_DOCUMENT, APPROVED_SPEC_SECTION, APPROVED_SPEC_COMMIT)
    assert first.artifact_commitment == second.artifact_commitment
    assert first.revision == second.revision
    approval = TrustedOracleApproval(
        first.revision.approval_reference, PM_AUTHORITY,
        first.revision.oracle_revision_id,
        first.revision.semantic_content_commitment,
        first.revision.approved_at, first.revision.parent_revision_id,
    )
    store = SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "evaluation.db",
        approvals=TrustedApprovalRegistry((approval,)),
    )
    assert store.admit_oracle(first.revision) == first.revision
    plan = EvaluationPlan.admit(
        first.revision, fixture_revision="spec017-fixtures-v1",
        production_build="4109019", model="deterministic",
        prompt="prompt-v1", schema="schema-v1", corpus_index="index-v1",
        configuration=commitment({"r2": "official"}), repetitions=1,
    )
    assert store.admit_plan(plan) == plan
    store.close()


def test_fixture_manifest_references_existing_inputs_and_admits_required_s4_variants():
    first = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    second = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    assert first.manifest_commitment == second.manifest_commitment
    assert {item.scenario_id for item in first.entries} == {f"S{i}" for i in range(1, 7)}
    assert REQUIRED_VARIANTS <= {item.variant for item in first.entries}
    assert REQUIRED_VARIANT_INVENTORY <= {
        (item.scenario_id, item.variant) for item in first.entries
    }
    nominal = tuple(item for item in first.entries if item.variant == "nominal")
    assert len(nominal) == 6
    assert all(item.support_status == "SUPPORTED" for item in nominal)
    required_s4 = tuple(item for item in first.entries if item.variant in {
        "material_refresh", "non_material", "restart_boundary"})
    assert len(required_s4) == 7
    assert all(item.support_status == "SUPPORTED" and item.source_reference
               and item.input_identity and item.input_commitment
               for item in required_s4)
    assert {item.recovery_boundary for item in required_s4
            if item.variant == "restart_boundary"} == {
                "EVIDENCE", "KNOWLEDGE", "GENERATION",
                "ARTIFACT_COMMIT", "PUBLICATION",
            }
    unavailable = tuple(item for item in first.entries if item.variant in {
        "degraded", "failure", "contradiction"})
    assert unavailable and all(
        item.support_status == "MISSING_EXISTING_INPUT_SOURCE"
        and item.missing_reason for item in unavailable)


def test_formal_fixture_admission_requires_exact_supported_manifest_entry():
    manifest = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    nominal = next(item for item in manifest.entries
                   if item.scenario_id == "S1" and item.variant == "nominal")
    admitted = manifest.admit(FixtureAdmissionRequest(
        "S1", "nominal", nominal.input_identity,
        nominal.input_commitment, nominal.oracle_scenario_reference,
    ))
    assert (admitted.fixture_identity, admitted.manifest_commitment) == (
        nominal.fixture_identity, manifest.manifest_commitment)
    with pytest.raises(ValueError, match="absent"):
        manifest.admit(FixtureAdmissionRequest(
            "S1", "unknown", nominal.input_identity,
            nominal.input_commitment, nominal.oracle_scenario_reference,
        ))
    with pytest.raises(ValueError, match="not supported"):
        manifest.admit(FixtureAdmissionRequest(
            "S6", "contradiction", "caller-input",
            commitment({"caller": True}), "spec017-s1-s6-v1:S6",
        ))
    for field, value in (
        ("input_identity", "caller-input"),
        ("input_commitment", commitment({"caller": True})),
        ("oracle_scenario_reference", "spec017-s1-s6-v1:S2"),
    ):
        request = dict(
            scenario_id="S1", variant="nominal",
            input_identity=nominal.input_identity,
            input_commitment=nominal.input_commitment,
            oracle_scenario_reference=nominal.oracle_scenario_reference,
        )
        request[field] = value
        with pytest.raises(ValueError, match="do not match"):
            manifest.admit(FixtureAdmissionRequest(**request))


def _fixture_requests(manifest):
    entries = {item.scenario_id: item for item in manifest.entries
               if item.variant == "nominal"}
    return {
        scenario: FixtureAdmissionRequest(
            scenario, "nominal", entry.input_identity,
            entry.input_commitment, entry.oracle_scenario_reference,
        )
        for scenario, entry in entries.items()
    }


def _formal_plan(oracle, fixture_revision):
    return EvaluationPlan.admit(
        oracle, fixture_revision=fixture_revision,
        production_build="4109019", model="deterministic",
        prompt="prompt-v1", schema="schema-v1", corpus_index="index-v1",
        configuration=commitment({"r2": "oracle-binding"}), repetitions=1,
    )


def _formal_campaign(plan, resolver, requests):
    return CampaignPlan.fixed_nominal(
        plan, campaign_key="r2-oracle-binding",
        fixture_resolver=resolver, fixture_requests=requests,
        admission=CampaignAdmission(
            repository_commit="4109019b61b181824bcec8a32efd31c55e0bfe91",
            repository_dirty=True,
            evaluation_config_commitment=commitment({"r2": "oracle-binding"}),
            provider_profile_references=("fake-provider:r2",),
            rag_profile_references=("fake-rag:r2",),
            environment_identity="pytest", evaluator_rubric_version="SPEC-017-v1",
            polling_interval_ms=5, timeout_ms=100,
            clock_source="host-monotonic", clock_synchronization_uncertainty_ms=0,
            random_seed=17017,
        ),
    )


def test_formal_campaign_binds_exact_official_oracle_commitment():
    manifest = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    plan = _formal_plan(OFFICIAL_ORACLE.revision, manifest.fixture_revision)
    campaign = _formal_campaign(plan, manifest, _fixture_requests(manifest))
    assert all(
        item.oracle_revision_id == plan.oracle_revision_id
        and item.oracle_content_commitment == plan.oracle_content_commitment
        for item in campaign.fixture_admissions
    )


def test_formal_campaign_rejects_same_shape_foreign_oracle_content():
    manifest = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    semantics = json.loads(OFFICIAL_ORACLE.revision.semantic_json)
    semantics["scenarios"]["S1"]["O10"]["text"] += " foreign"
    foreign = OracleRevision.approved(
        oracle_set_id=OFFICIAL_ORACLE.revision.oracle_set_id,
        semantics=semantics,
        created_at=OFFICIAL_ORACLE.revision.created_at,
        approval_authority=OFFICIAL_ORACLE.revision.approval_authority,
        approval_reference=OFFICIAL_ORACLE.revision.approval_reference,
        approved_at=OFFICIAL_ORACLE.revision.approved_at,
    )
    assert foreign.semantic_content_commitment != (
        OFFICIAL_ORACLE.revision.semantic_content_commitment)
    foreign_plan = _formal_plan(foreign, manifest.fixture_revision)
    with pytest.raises(ValueError, match="Oracle commitment"):
        _formal_campaign(
            foreign_plan, manifest, _fixture_requests(manifest))
    same_source_revision = replace(OFFICIAL_ORACLE, revision=foreign)
    with pytest.raises(ValueError, match="Oracle commitment mismatch"):
        load_fixture_manifest(
            FIXTURE_PATH, oracle=same_source_revision, repository_root=ROOT)


def test_fixture_manifest_oracle_commitment_mutation_is_rejected(tmp_path):
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    original = raw["source_oracle_content_commitment"]
    raw["source_oracle_content_commitment"] = (
        original[:-1] + ("0" if original[-1] != "0" else "1"))
    path = tmp_path / "mutated-fixture-manifest.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="Oracle commitment mismatch"):
        load_fixture_manifest(path, oracle=OFFICIAL_ORACLE, repository_root=ROOT)


def test_caller_fixture_identity_without_admitted_oracle_commitment_is_rejected():
    manifest = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    plan = _formal_plan(OFFICIAL_ORACLE.revision, manifest.fixture_revision)
    valid = _fixture_requests(manifest)
    caller_values = {
        scenario: SimpleNamespace(
            scenario_id=item.scenario_id, variant=item.variant,
            input_identity=item.input_identity,
            input_commitment=item.input_commitment,
            oracle_scenario_reference=item.oracle_scenario_reference,
        )
        for scenario, item in valid.items()
    }
    with pytest.raises(TypeError, match="non-authoritative request"):
        _formal_campaign(plan, manifest, caller_values)


def _caller_manifest_copy(resolver, *, manifest_commitment=None):
    source = resolver.manifest
    return OfficialFixtureManifest(
        source.schema_version, source.manifest_identity, source.fixture_revision,
        source.source_oracle_artifact_revision,
        source.source_oracle_revision_id,
        source.source_oracle_content_commitment,
        manifest_commitment or source.manifest_commitment, source.entries,
    )


def test_caller_built_and_copied_manifest_cannot_mint_formal_admission():
    resolver = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    plan = _formal_plan(OFFICIAL_ORACLE.revision, resolver.fixture_revision)
    requests = _fixture_requests(resolver)
    for caller_manifest in (
        _caller_manifest_copy(resolver),
        replace(resolver.manifest),
    ):
        assert not hasattr(caller_manifest, "admit")
        with pytest.raises(TypeError, match="official fixture resolver"):
            _formal_campaign(plan, caller_manifest, requests)


def test_self_minted_manifest_commitment_does_not_create_official_provenance():
    resolver = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    plan = _formal_plan(OFFICIAL_ORACLE.revision, resolver.fixture_revision)
    caller_manifest = _caller_manifest_copy(
        resolver, manifest_commitment=commitment({
            "caller": "recomputed", "entries": [
                item.fixture_identity for item in resolver.entries],
        }))
    with pytest.raises(TypeError, match="official fixture resolver"):
        _formal_campaign(plan, caller_manifest, _fixture_requests(resolver))


def test_replace_verified_resolver_and_manual_admitted_fixture_are_rejected():
    resolver = load_fixture_manifest(
        FIXTURE_PATH, oracle=OFFICIAL_ORACLE, repository_root=ROOT)
    plan = _formal_plan(OFFICIAL_ORACLE.revision, resolver.fixture_revision)
    requests = _fixture_requests(resolver)
    with pytest.raises(TypeError):
        replace(resolver)

    issued = resolver.admit(requests["S1"])
    manual = object.__new__(AdmittedFixture)
    for field in issued.__dataclass_fields__:
        object.__setattr__(manual, field, getattr(issued, field))
    forged_requests = dict(requests)
    forged_requests["S1"] = manual
    with pytest.raises(TypeError, match="non-authoritative request"):
        _formal_campaign(plan, resolver, forged_requests)


def test_production_modules_do_not_import_official_oracle_or_fixture_artifacts():
    forbidden = (
        "load_official_oracle", "load_fixture_manifest",
        "spec017_s1_s6_v1.json", "spec017_s1_s6_fixture_manifest_v1.json",
    )
    production_roots = tuple(path for path in (ROOT / "src").iterdir()
                             if path.name != "rca_evaluation")
    for root in production_roots:
        for path in root.rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            assert not any(value in content for value in forbidden), path


def _execution(scenario: str, repetition: int) -> PlannedExecution:
    execution_id = identity("execution", ["R2", scenario, repetition])
    return PlannedExecution(
        execution_id, scenario, repetition, f"fixture:{scenario}",
        commitment({"scenario": scenario}),
    )


def _result(
    execution: PlannedExecution, disposition: ExecutionDisposition, *,
    success: bool = False, latency: int | None = None,
    metric: ApprovedMetricDisposition | None = None,
    failure_case: FailureCase = FailureCase.NONE, stage: str = "R2",
) -> ExecutionResult:
    return ExecutionResult(
        execution, disposition, success, KnowledgeDisposition.MATCH,
        failure_case, stage, signal_started_at="2026-10-04T01:00:00Z",
        usable_rca_observed_at=(
            "2026-10-04T01:00:01Z" if latency is not None else None),
        latency_ms=latency,
        cutoff_ms=(100 if disposition is ExecutionDisposition.RIGHT_CENSORED_UNFINISHED
                   else None),
        metric_disposition=metric,
    )


def test_approved_denominator_retains_timeout_failures_invalids_and_macro_slot():
    executions = tuple(_execution(scenario, repetition)
                       for scenario in (f"S{i}" for i in range(1, 7))
                       for repetition in (1, 2))
    results = []
    for item in executions:
        if item.scenario_id == "S1":
            results.append(_result(
                item, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED))
        elif item.scenario_id == "S2" and item.repetition == 1:
            results.append(_result(
                item, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE))
        elif item.scenario_id == "S3" and item.repetition == 1:
            results.append(_result(
                item, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE,
                metric=ApprovedMetricDisposition.INVALID_OUTPUT,
                failure_case=FailureCase.SCHEMA_OR_VALIDATION_FAILURE))
        elif item.scenario_id == "S4" and item.repetition == 1:
            results.append(_result(
                item, ExecutionDisposition.INVALID_EVALUATION,
                metric=ApprovedMetricDisposition.INVALID_OBSERVATION,
                stage="EXTERNAL_OBSERVER"))
        elif item.scenario_id == "S5" and item.repetition == 1:
            results.append(_result(
                item, ExecutionDisposition.INVALID_EVALUATION,
                metric=ApprovedMetricDisposition.ISOLATION_BREACH,
                stage="ISOLATION_BREACH"))
        else:
            results.append(_result(
                item, ExecutionDisposition.OBSERVED_COMPLETION,
                success=True, latency=10))
    summary = aggregate(tuple(results), expected_execution_ids=tuple(
        item.execution_id for item in executions))
    assert summary.planned == summary.quality_eligible == 12
    assert summary.successes == 6
    assert summary.overall_micro_rate == 0.5
    per_scenario = {item.scenario_id: item for item in summary.per_scenario}
    assert per_scenario["S1"].quality_rate == 0
    assert summary.scenario_equal_macro_rate == pytest.approx(
        sum(item.quality_rate for item in per_scenario.values()) / 6)
    counts = dict(summary.disposition_counts)
    assert counts["TIMEOUT"] == 2
    assert counts["SYSTEM_FAILURE"] == 1
    assert counts["INVALID_OUTPUT"] == 1
    assert counts["INVALID_OBSERVATION"] == 1
    assert counts["ISOLATION_BREACH"] == 1


def test_typed_outcome_cannot_be_relabelled_by_caller():
    execution = _execution("S1", 1)
    with pytest.raises(ValueError, match="contradicts"):
        _result(
            execution, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE,
            metric=ApprovedMetricDisposition.RCA_FAIL,
        )
    with pytest.raises(ValueError, match="contradicts"):
        _result(
            execution, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED,
            metric=ApprovedMetricDisposition.SYSTEM_FAILURE,
        )


@pytest.mark.parametrize("original", (
    lambda execution: _result(
        execution, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE,
        metric=ApprovedMetricDisposition.INVALID_OUTPUT,
        failure_case=FailureCase.SCHEMA_OR_VALIDATION_FAILURE,
    ),
    lambda execution: _result(
        execution, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE),
    lambda execution: _result(
        execution, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED),
))
def test_product_invalid_failure_or_timeout_cannot_be_promoted_by_replacement(original):
    execution = _execution("S1", 1)
    replacement_execution = PlannedExecution(
        identity("execution", ["R2", "S1", 1, "replacement"]),
        "S1", 1, execution.input_identity, execution.input_commitment,
        replacement_for_execution_id=execution.execution_id,
    )
    replacement = _result(
        replacement_execution, ExecutionDisposition.OBSERVED_COMPLETION,
        success=True, latency=1,
    )
    with pytest.raises(ValueError, match="does not authorize replacement"):
        aggregate(
            (original(execution), replacement),
            expected_execution_ids=(execution.execution_id,),
        )
    summary = aggregate(
        (original(execution),), expected_execution_ids=(execution.execution_id,))
    assert summary.planned == summary.quality_eligible == 1
    assert summary.successes == 0


def test_bootstrap_is_deterministic_versioned_and_macro_micro_are_separate():
    executions = tuple(_execution(scenario, repetition)
                       for scenario in (f"S{i}" for i in range(1, 7))
                       for repetition in (1, 2))
    results = tuple(_result(
        item, ExecutionDisposition.OBSERVED_COMPLETION,
        success=(item.scenario_id != "S1" and item.repetition == 1), latency=10,
    ) for item in executions)
    first = aggregate(results, expected_execution_ids=tuple(
        item.execution_id for item in executions))
    second = aggregate(results, expected_execution_ids=tuple(
        item.execution_id for item in executions))
    assert first.micro_95_interval == second.micro_95_interval
    assert first.macro_95_interval == second.macro_95_interval
    assert first.bootstrap_algorithm == BOOTSTRAP_ALGORITHM
    assert first.bootstrap_seed == second.bootstrap_seed
    assert first.bootstrap_resamples == second.bootstrap_resamples
    assert "SMALL-N" in first.bootstrap_limitation
    assert first.micro_95_interval is not None and first.macro_95_interval is not None


def test_primary_latency_distribution_excludes_noncompletion_samples():
    executions = tuple(_execution(f"S{i}", 1) for i in range(1, 6))
    results = (
        _result(executions[0], ExecutionDisposition.OBSERVED_COMPLETION,
                success=True, latency=10),
        _result(executions[1], ExecutionDisposition.OBSERVED_COMPLETION,
                success=True, latency=20),
        _result(executions[2], ExecutionDisposition.RIGHT_CENSORED_UNFINISHED),
        _result(executions[3], ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE),
        _result(executions[4], ExecutionDisposition.INVALID_EVALUATION),
    )
    distribution = summarize_latency(
        LatencyKind.PRIMARY_FULL_E2E,
        primary_latency_measurements(
            results, polling_interval_ms=10, cutoff_ms=100,
            clock_source="host-monotonic",
        ),
    )
    assert (distribution.observed_n, distribution.minimum_ms,
            distribution.median_ms, distribution.p95_ms,
            distribution.maximum_ms) == (2, 10, 15.0, 20, 20)
    assert (distribution.timeout_count, distribution.censored_count,
            distribution.failure_count, distribution.invalid_count) == (1, 1, 1, 1)
    assert distribution.start_boundaries == (
        "EVALUATION_SIGNAL_PUBLICLY_OBSERVABLE",)
    assert distribution.polling_intervals_ms == (10,)
    assert distribution.cutoffs_ms == (100,)


class _Clock:
    def __init__(self):
        self.ns = 0

    def utc_now(self):
        return f"2026-10-04T01:00:{self.ns // 1_000_000_000:02d}Z"

    def monotonic_ns(self):
        return self.ns


class _World:
    def __init__(self, incident_id="INC-1", aggregate_id="AGG-1"):
        self.incident_id = incident_id
        self.aggregate_id = aggregate_id
        self.current_id = "v1"
        self.freshness = CurrentFreshness.FRESH
        self.basis = "ER-BASE"
        self.freshness_lineage = [self._current()]
        self.versions = [self._version("v1", "a1", "p1")]
        self.tries = {"a1": [self._try("a1", 1, True)]}
        self.a_publications = {"p1": self._a_publication("v1", "p1", True)}
        self.i_publications = {"p1": self._i_publication("v1", "p1")}
        self.materiality = {}
        self.runtime = {}
        self.relationship_version = "v1"

    def _current(self):
        return SimpleNamespace(
            aggregate_id=self.aggregate_id, current_version_id=self.current_id,
            freshness=self.freshness,
            material_evidence_revision_basis=self.basis,
        )

    def _version(self, version_id, attempt_id, publication_id):
        return SimpleNamespace(
            version_id=version_id, aggregate_id=self.aggregate_id,
            attempt_id=attempt_id, publication_operation_id=publication_id,
        )

    @staticmethod
    def _try(attempt_id, ordinal, success):
        return SimpleNamespace(
            identity=SimpleNamespace(
                attempt_id=attempt_id, try_ordinal=ordinal),
            result_kind=(LogicalTryResultKind.VALIDATED_RESULT if success
                         else LogicalTryResultKind.FAILURE),
        )

    def _a_publication(self, version_id, operation_id, applied):
        return SimpleNamespace(
            target=SimpleNamespace(
                target_version_id=version_id, aggregate_id=self.aggregate_id),
            disposition=(PublicationDisposition.APPLIED if applied
                         else PublicationDisposition.A_SIDE_COMMITTED),
            resulting_current_version_id=version_id if applied else None,
        )

    def _i_publication(self, version_id, operation_id):
        return SimpleNamespace(
            publication_operation_id=operation_id,
            target_version_id=version_id, incident_id=self.incident_id,
            disposition=IncidentRcaPublicationDisposition.APPLIED,
            resulting_current_version_id=version_id,
        )

    def get_current(self, aggregate_id):
        if aggregate_id != self.aggregate_id or self.current_id is None:
            return None
        version = next(item for item in self.versions
                       if item.version_id == self.current_id)
        return SimpleNamespace(current=self._current(), version=version)

    def get_version_history(self, aggregate_id):
        return tuple(self.versions)

    def get_freshness_lineage(self, aggregate_id):
        return tuple(self.freshness_lineage)

    def get_attempt_lineage(self, attempt_id):
        outcomes = self.tries.get(attempt_id)
        if outcomes is None:
            return None
        return SimpleNamespace(
            attempt=SimpleNamespace(
                lineage=SimpleNamespace(attempt_id=attempt_id)),
            try_outcomes=tuple(outcomes),
        )

    def get_publication_result(self, operation_id):
        return self.a_publications.get(operation_id)

    def get_rca_relationship(self, incident_id):
        return SimpleNamespace(
            incident_id=self.incident_id,
            current_version_id=self.relationship_version,
        )

    def get_rca_publication_result(self, operation_id):
        return self.i_publications.get(operation_id)

    def read_materiality_result(self, result_id):
        return self.materiality.get(result_id)

    def get(self, work_id):
        return self.runtime.get(work_id)

    def mark_material(self):
        baseline = self._current()
        self.freshness = CurrentFreshness.STALE
        self.basis = "ER-MAT"
        self.freshness_lineage = [baseline, self._current()]
        request = SimpleNamespace(
            baseline_revision_id="ER-BASE", candidate_revision_id="ER-MAT")
        self.materiality["MAT-1"] = SimpleNamespace(
            materiality_result_id="MAT-1", request=request,
            judgement=MaterialityJudgement.MATERIAL,
        )

    def publish_v2(self):
        self.current_id = "v2"
        self.freshness = CurrentFreshness.FRESH
        self.versions.append(self._version("v2", "a2", "p2"))
        self.tries["a2"] = [self._try("a2", 1, True)]
        self.a_publications["p2"] = self._a_publication("v2", "p2", True)
        self.i_publications["p2"] = self._i_publication("v2", "p2")
        self.relationship_version = "v2"
        self.freshness_lineage.append(self._current())


def _resolver(world, clock, *, clock_source="host-monotonic"):
    return RcaPublicStateResolver(
        world, world, world, world, clock, clock_source=clock_source)


RUN_ID = identity("evaluation_run", ["R2", "recovery"])
EXECUTION_ID = identity("execution", [RUN_ID, "S1", 1])


def _reference(
    *, attempts=("a1",), materiality=None, runtime=None,
    run_id=RUN_ID, execution_id=EXECUTION_ID,
    incident_id="INC-1", aggregate_id="AGG-1",
):
    return RecoveryStateReference(
        run_id, execution_id, incident_id, aggregate_id, attempts,
        materiality_result_id=materiality, runtime_work_id=runtime,
    )


def _refresh_work(world, *, work_id="WORK-REF", operation_id="OP-REF"):
    world.runtime[work_id] = SimpleNamespace(
        work_id=work_id, operation_id=operation_id,
        workflow_operation_id=None, status=RuntimeWorkStatus.OUTSTANDING,
    )


def test_resolver_backed_material_refresh_and_refresh_latency():
    world = _World(); clock = _Clock(); resolver = _resolver(world, clock)
    _refresh_work(world)
    before = resolver.resolve(_reference(runtime="WORK-REF"))
    world.mark_material(); clock.ns = 10_000_000
    stale = resolver.resolve(_reference(materiality="MAT-1", runtime="WORK-REF"))
    world.publish_v2(); clock.ns = 30_000_000
    final = resolver.resolve(_reference(
        attempts=("a1", "a2"), runtime="WORK-REF"))
    assert evaluate_refresh(
        before=before, after_materiality=stale, final=final).passed
    measurement = refresh_latency_measurement(
        stale, final, disposition=LatencyDisposition.OBSERVED_COMPLETION,
        polling_interval_ms=5, cutoff_ms=100, clock_source="host-monotonic")
    distribution = summarize_latency(LatencyKind.REFRESH, (measurement,))
    assert (distribution.observed_n, distribution.minimum_ms,
            distribution.maximum_ms) == (1, 20, 20)


@pytest.mark.parametrize("mismatch", (
    "execution", "incident", "aggregate", "work", "operation",
    "work-operation", "clock",
))
def test_refresh_and_latency_reject_mixed_resolver_context(mismatch):
    world = _World(); clock = _Clock(); resolver = _resolver(world, clock)
    _refresh_work(world)
    before = resolver.resolve(_reference(runtime="WORK-REF"))
    world.mark_material(); clock.ns = 10_000_000
    stale = resolver.resolve(_reference(materiality="MAT-1", runtime="WORK-REF"))
    world.publish_v2(); clock.ns = 20_000_000
    if mismatch == "execution":
        foreign_execution = identity("execution", [RUN_ID, "S1", "foreign"])
        final = resolver.resolve(_reference(
            execution_id=foreign_execution, runtime="WORK-REF"))
        expected_error = "different evaluation contexts"
    elif mismatch in {"incident", "aggregate"}:
        foreign_world = _World(
            incident_id=("INC-2" if mismatch == "incident" else "INC-1"),
            aggregate_id=("AGG-2" if mismatch == "aggregate" else "AGG-1"),
        )
        foreign_world.mark_material(); foreign_world.publish_v2()
        _refresh_work(foreign_world)
        final = _resolver(foreign_world, clock).resolve(_reference(
            incident_id=foreign_world.incident_id,
            aggregate_id=foreign_world.aggregate_id,
            runtime="WORK-REF",
        ))
        expected_error = "different evaluation contexts"
    elif mismatch == "clock":
        final = _resolver(
            world, clock, clock_source="foreign-clock").resolve(
                _reference(runtime="WORK-REF"))
        expected_error = "different evaluation contexts"
    else:
        work_id = "WORK-2" if mismatch in {"work", "work-operation"} else "WORK-REF"
        operation_id = "OP-2" if mismatch in {"operation", "work-operation"} else "OP-REF"
        _refresh_work(world, work_id=work_id, operation_id=operation_id)
        final = resolver.resolve(_reference(runtime=work_id))
        expected_error = "same operation/work"
    with pytest.raises(ValueError, match=expected_error):
        evaluate_refresh(before=before, after_materiality=stale, final=final)
    with pytest.raises(ValueError, match=expected_error):
        refresh_latency_measurement(
            stale, final, disposition=LatencyDisposition.OBSERVED_COMPLETION,
            polling_interval_ms=5, cutoff_ms=100,
            clock_source="host-monotonic",
        )


@pytest.mark.parametrize("defect", (
    "missing-publication", "incident-current-mismatch",
    "publication-incident-mismatch", "materiality-basis-mismatch",
))
def test_public_resolver_fails_closed_on_missing_or_contradictory_facts(defect):
    world = _World(); clock = _Clock(); resolver = _resolver(world, clock)
    materiality = None
    if defect == "missing-publication":
        world.a_publications.clear()
    elif defect == "incident-current-mismatch":
        world.relationship_version = "foreign"
    elif defect == "publication-incident-mismatch":
        world.i_publications["p1"] = world._i_publication("foreign", "p1")
    else:
        world.mark_material()
        world.materiality["MAT-1"].request.candidate_revision_id = "ER-WRONG"
        materiality = "MAT-1"
    with pytest.raises(ValueError):
        resolver.resolve(_reference(materiality=materiality))


def test_caller_built_state_cannot_bypass_public_resolver():
    state = RcaPublicState(
        "v1", Freshness.FRESH, ("a1",), ("a1:1",), ("v1",), ("p1",),
        "caller-frontier",
    )
    with pytest.raises(TypeError, match="resolver-owned public facts"):
        evaluate_refresh(before=state, after_materiality=state, final=state)


@pytest.mark.parametrize("boundary", tuple(RecoveryBoundary))
def test_resolver_backed_restart_same_operation_converges(boundary):
    world = _World(); clock = _Clock(); resolver = _resolver(world, clock)
    world.runtime["WORK-1"] = SimpleNamespace(
        work_id="WORK-1", operation_id="OP-1", workflow_operation_id=None,
        status=RuntimeWorkStatus.OUTSTANDING,
    )
    before = resolver.resolve(_reference(runtime="WORK-1"))

    if boundary is RecoveryBoundary.GENERATION:
        world.tries["a1"].append(world._try("a1", 2, True))
    elif boundary is RecoveryBoundary.ARTIFACT_COMMIT:
        world.versions.append(world._version("v2", "a1", "p2"))
        world.a_publications["p2"] = world._a_publication("v2", "p2", False)
    elif boundary is RecoveryBoundary.PUBLICATION:
        world.versions.append(world._version("v2", "a1", "p2"))
        world.a_publications["p2"] = world._a_publication("v2", "p2", False)
        # Re-resolve the committed frontier as the actual pre-restart boundary.
        before = resolver.resolve(_reference(runtime="WORK-1"))
        world.a_publications["p2"] = world._a_publication("v2", "p2", True)
        world.i_publications["p2"] = world._i_publication("v2", "p2")
        world.current_id = "v2"; world.relationship_version = "v2"
        world.freshness_lineage.append(world._current())
    world.runtime["WORK-1"] = SimpleNamespace(
        work_id="WORK-1", operation_id="OP-1", workflow_operation_id=None,
        status=RuntimeWorkStatus.COMPLETED,
    )
    clock.ns = 25_000_000
    after = resolver.resolve(_reference(runtime="WORK-1"))
    result = evaluate_restart(
        before=before, after=after, expected_operation_id="OP-1",
        boundary=boundary, signal_injection_count=1,
        publication_reconciled_before_resume=True)
    assert result.passed, result.details
    measurement = restart_latency_measurement(
        before, after, expected_operation_id="OP-1",
        disposition=LatencyDisposition.OBSERVED_COMPLETION,
        polling_interval_ms=5, cutoff_ms=100, clock_source="host-monotonic")
    assert measurement.duration_ms == 25


def test_restart_missing_runtime_fact_and_wrong_operation_fail_closed():
    world = _World(); clock = _Clock(); resolver = _resolver(world, clock)
    with pytest.raises(ValueError, match="Runtime work reference is missing"):
        resolver.resolve(_reference(runtime="MISSING"))
    world.runtime["WORK-1"] = SimpleNamespace(
        work_id="WORK-1", operation_id="OTHER", workflow_operation_id=None,
        status=RuntimeWorkStatus.COMPLETED,
    )
    before = resolver.resolve(_reference(runtime="WORK-1"))
    after = resolver.resolve(_reference(runtime="WORK-1"))
    assert not evaluate_restart(
        before=before, after=after, expected_operation_id="OP-1",
        boundary=RecoveryBoundary.EVIDENCE, signal_injection_count=1,
        publication_reconciled_before_resume=True).passed


@pytest.mark.parametrize("mismatch", ("execution", "incident", "aggregate", "operation"))
def test_restart_and_latency_reject_mixed_resolver_context(mismatch):
    world = _World(); clock = _Clock(); resolver = _resolver(world, clock)
    world.runtime["WORK-1"] = SimpleNamespace(
        work_id="WORK-1", operation_id="OP-1", workflow_operation_id=None,
        status=RuntimeWorkStatus.OUTSTANDING,
    )
    before = resolver.resolve(_reference(runtime="WORK-1"))
    clock.ns = 10_000_000
    if mismatch == "operation":
        world.runtime["WORK-2"] = SimpleNamespace(
            work_id="WORK-2", operation_id="OP-2", workflow_operation_id=None,
            status=RuntimeWorkStatus.COMPLETED,
        )
        after = resolver.resolve(_reference(runtime="WORK-2"))
        expected_error = "same operation/work"
    elif mismatch == "execution":
        world.runtime["WORK-1"].status = RuntimeWorkStatus.COMPLETED
        other_execution = identity("execution", [RUN_ID, "S1", "other"])
        after = resolver.resolve(_reference(
            runtime="WORK-1", execution_id=other_execution))
        expected_error = "different evaluation contexts"
    else:
        foreign_world = _World(
            incident_id=("INC-2" if mismatch == "incident" else "INC-1"),
            aggregate_id=("AGG-2" if mismatch == "aggregate" else "AGG-1"),
        )
        foreign_world.runtime["WORK-1"] = SimpleNamespace(
            work_id="WORK-1", operation_id="OP-1", workflow_operation_id=None,
            status=RuntimeWorkStatus.COMPLETED,
        )
        after = _resolver(foreign_world, clock).resolve(_reference(
            runtime="WORK-1", incident_id=foreign_world.incident_id,
            aggregate_id=foreign_world.aggregate_id,
        ))
        expected_error = "different evaluation contexts"
    with pytest.raises(ValueError, match=expected_error):
        evaluate_restart(
            before=before, after=after, expected_operation_id="OP-1",
            boundary=RecoveryBoundary.EVIDENCE, signal_injection_count=1,
            publication_reconciled_before_resume=True,
        )
    with pytest.raises(ValueError, match=expected_error):
        restart_latency_measurement(
            before, after, expected_operation_id="OP-1",
            disposition=LatencyDisposition.OBSERVED_COMPLETION,
            polling_interval_ms=5, cutoff_ms=100,
            clock_source="host-monotonic",
        )


def test_three_latency_distributions_cannot_be_mixed():
    world = _World(); clock = _Clock(); resolver = _resolver(world, clock)
    world.runtime["WORK-1"] = SimpleNamespace(
        work_id="WORK-1", operation_id="OP-1", workflow_operation_id=None,
        status=RuntimeWorkStatus.COMPLETED,
    )
    first = resolver.resolve(_reference(runtime="WORK-1"))
    clock.ns = 10_000_000
    second = resolver.resolve(_reference(runtime="WORK-1"))
    restart = restart_latency_measurement(
        first, second, expected_operation_id="OP-1",
        disposition=LatencyDisposition.OBSERVED_COMPLETION,
        polling_interval_ms=5, cutoff_ms=100, clock_source="host-monotonic")
    with pytest.raises(ValueError, match="cannot be mixed"):
        summarize_latency(LatencyKind.REFRESH, (restart,))
