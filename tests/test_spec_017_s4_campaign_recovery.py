"""Focused S4 bounded campaign, aggregation, refresh, and recovery tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rca_evaluation.aggregation import aggregate, nearest_rank
from rca_evaluation.authority import PM_AUTHORITY, TrustedApprovalRegistry, TrustedOracleApproval
from rca_evaluation.campaign import (
    AuthoritativeOutcomeStatus, CampaignAdmission, CampaignPlan, EvidenceCompleteness,
    ExecutionDisposition, ExecutionResult, FailureCase, KnowledgeDisposition,
    PlannedExecution, UpstreamOutcome,
)
from rca_evaluation.identity import canonical_text_bytes, commitment, identity
from rca_evaluation.fixtures import (
    OFFICIAL_FIXTURE_MANIFEST_COMMITMENT, FixtureAdmissionRequest,
    load_fixture_manifest,
)
from rca_evaluation.latency import ExternalObservation
from incident_evidence.contracts import MaterialityJudgement
from rca_persistence.contracts import (
    CurrentFreshness, LogicalTryResultKind, PublicationDisposition,
)
from runtime_orchestration.contracts import RuntimeWorkStatus
from rca_evaluation.manifest import ReproducibilityManifest
from rca_evaluation.oracle import EvaluationPlan, load_official_oracle
from rca_evaluation.recovery_evaluation import (
    Freshness, Materiality, RcaPublicState, RcaPublicStateResolver,
    RecoveryBoundary, RecoveryStateReference,
    _evaluate_refresh_states, _evaluate_restart_states,
)
from rca_evaluation.runner import CampaignRunner, RecoveryEvaluationRunner
from rca_evaluation.sqlite_store import SqliteEvaluationStore


CONFIG = {"gate": "DETERMINISTIC_CONTRACT", "provider_calls": "fake-only"}
ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_ORACLE = load_official_oracle(
    ROOT / "configs/rca_evaluation/oracle/spec017_s1_s6_v1.json")
OFFICIAL_FIXTURES = load_fixture_manifest(
    ROOT / "configs/rca_evaluation/fixtures/spec017_s1_s6_fixture_manifest_v1.json",
    oracle=OFFICIAL_ORACLE,
    repository_root=ROOT,
)


def _portable_fixture_repository(root, *, crlf=False, mutate_scenarios=False):
    scenario = canonical_text_bytes((ROOT / "configs/scenarios.yaml").read_bytes())
    controls = canonical_text_bytes((
        ROOT / "configs/rca_evaluation/fixtures/spec017_r2s4_evaluation_controls_v2.json"
    ).read_bytes())
    r2_sources = canonical_text_bytes((
        ROOT / "configs/rca_evaluation/fixtures/spec017_r2_missing_formal_fixtures_v1.json"
    ).read_bytes())
    if mutate_scenarios:
        scenario = scenario.replace(b"random_seed: 42", b"random_seed: 43")
    if crlf:
        scenario = scenario.replace(b"\n", b"\r\n")
        controls = controls.replace(b"\n", b"\r\n")
        r2_sources = r2_sources.replace(b"\n", b"\r\n")
    (root / "configs/rca_evaluation/fixtures").mkdir(parents=True, exist_ok=True)
    (root / "configs/scenarios.yaml").write_bytes(scenario)
    (root / "configs/rca_evaluation/fixtures/spec017_r2s4_evaluation_controls_v2.json").write_bytes(
        controls)
    (root / "configs/rca_evaluation/fixtures/spec017_r2_missing_formal_fixtures_v1.json").write_bytes(
        r2_sources)


def _oracle():
    return OFFICIAL_ORACLE.revision


def _plan(oracle, n=1, **overrides):
    values = dict(
        fixture_revision=OFFICIAL_FIXTURES.fixture_revision,
        production_build="build-4109019",
        model="fake-model", prompt="prompt-sha", schema="schema-sha",
        corpus_index="index-sha", configuration=commitment(CONFIG), repetitions=n,
    )
    values.update(overrides)
    return EvaluationPlan.admit(oracle, **values)


def _admission(**overrides):
    values = dict(
        repository_commit="4109019b61b181824bcec8a32efd31c55e0bfe91",
        repository_dirty=False,
        evaluation_config_commitment=commitment(CONFIG),
        provider_profile_references=("fake-provider-profile:r1",),
        rag_profile_references=("fake-rag-profile:r1",),
        environment_identity="pytest-host:python",
        evaluator_rubric_version="SPEC-017-v1.0-S4",
        polling_interval_ms=10, timeout_ms=30,
        clock_source="host-monotonic+utc", clock_synchronization_uncertainty_ms=0,
        random_seed=1704, deterministic_order_commitment=None,
    )
    values.update(overrides)
    return CampaignAdmission(**values)


def _fixture_requests():
    entries = {item.scenario_id: item for item in OFFICIAL_FIXTURES.entries
               if item.variant == "nominal"}
    return {
        scenario: FixtureAdmissionRequest(
            scenario, "nominal", entry.input_identity,
            entry.input_commitment, entry.oracle_scenario_reference,
        )
        for scenario, entry in entries.items()
    }


def _campaign(plan, *, order=None, admission=None):
    return CampaignPlan.fixed_nominal(
        plan, campaign_key="campaign-s4-unit",
        fixture_resolver=OFFICIAL_FIXTURES,
        fixture_requests=_fixture_requests(),
        admission=admission or _admission(), order=order,
    )


def _manifest(plan, campaign, *, executions=None, config=None, **overrides):
    admitted = executions or campaign.executions
    values = dict(
        run_id=campaign.run_id,
        execution_ids=tuple(item.execution_id for item in admitted),
        scenario_inputs=tuple((item.scenario_id, item.input_identity, item.input_commitment)
                              for item in admitted),
        random_seed=campaign.admission.random_seed,
        deterministic_order_commitment=campaign.admission.deterministic_order_commitment,
        evaluation_plan_id=plan.evaluation_plan_id,
        oracle_revision_id=plan.oracle_revision_id,
        oracle_content_commitment=plan.oracle_content_commitment,
        fixture_revision=plan.fixture_revision, production_build=plan.production_build,
        model=plan.model, prompt=plan.prompt, result_schema=plan.schema,
        corpus_index=plan.corpus_index, evaluation_config_reference=plan.configuration,
        evaluation_config=CONFIG if config is None else config,
        repository_commit=campaign.admission.repository_commit, repository_dirty=False,
        provider_profile_references=campaign.admission.provider_profile_references,
        rag_profile_references=campaign.admission.rag_profile_references,
        environment_identity=campaign.admission.environment_identity,
        evaluator_rubric_version=campaign.admission.evaluator_rubric_version,
        polling_interval_ms=campaign.admission.polling_interval_ms,
        timeout_ms=campaign.admission.timeout_ms,
        clock_source=campaign.admission.clock_source,
        clock_synchronization_uncertainty_ms=campaign.admission.clock_synchronization_uncertainty_ms,
        approved_spec_commitments={
            "repository_branch": "feature/spec-017",
            "oracle_set_identity": "PM-S1-S6",
            "oracle_approval_reference": plan.oracle_approval_reference,
            "fixture_schema_version": "SPEC-017-FIXTURE-v1",
            "fixture_content_commitments": [item.input_commitment for item in admitted],
            "denominator_policy_version": "SPEC-017-8.4-v1",
            "production_contract_versions": {"RCA": "SPEC-011/016"},
            "generation_config_commitment": commitment(CONFIG if config is None else config),
            "knowledge_corpus_manifest_commitment": "sha256:" + "c" * 64,
            "knowledge_document_revisions": ["fixture-doc-v1"],
            "embedding_model": "text-embedding-004",
            "knowledge_build_identity": "knowledge-build-v1",
            "retrieval_profile_identity": "retrieval-profile-v1",
            "applicability_profile_identity": "applicability-profile-v1",
            "docker_compose_digest": "sha256:" + "d" * 64,
            "docker_config_digest": "sha256:" + "e" * 64,
            "docker_image_digest": "sha256:" + "f" * 64,
            "docker_namespace_identity": "evaluation-isolated-v1",
            "tool_versions": {"python": "3"},
            "reviewer_protocol_version": "SPEC-017-6.2-v1",
            "adjudication_policy_version": "SPEC-017-2.4-v1",
            "public_observation_adapter_version": "SPEC-017-S2-v1",
            "report_generator_version": "SPEC-017-FORMAL-REPORT-v1",
            "required_isolation_case_ids": [item.execution_id for item in admitted],
            "required_deviation_case_ids": [item.execution_id for item in admitted],
            "required_refresh_case_ids": list(
                campaign.required_refresh_case_ids),
            "required_restart_case_ids": list(
                campaign.required_restart_case_ids),
        },
    )
    values.update(overrides)
    return ReproducibilityManifest.seal(**values)


def _trusted(oracle):
    return TrustedApprovalRegistry((TrustedOracleApproval(
        oracle.approval_reference, PM_AUTHORITY, oracle.oracle_revision_id,
        oracle.semantic_content_commitment, oracle.approved_at, oracle.parent_revision_id,
    ),))


def _store(tmp_path, oracle, plan):
    store = SqliteEvaluationStore(tmp_path / "rca_evaluation" / "evaluation.db",
                                  approvals=_trusted(oracle))
    store.admit_oracle(oracle); store.admit_plan(plan)
    return store


class _Clock:
    def __init__(self): self.ms = 0; self.waits = []
    def monotonic_ns(self): return self.ms * 1_000_000
    def utc_now(self):
        return (datetime(2026, 10, 3, tzinfo=timezone.utc) +
                timedelta(milliseconds=self.ms)).isoformat().replace("+00:00", "Z")
    def wait_ms(self, milliseconds): self.waits.append(milliseconds); self.ms += milliseconds


def _upstream_success(**overrides):
    values = {"status": AuthoritativeOutcomeStatus.SUCCESS}
    values.update(overrides)
    return UpstreamOutcome(**values)


def _observation(clock, *, actionable=True, usable=True, upstream=None):
    return ExternalObservation(
        clock.utc_now(), actionable, actionable, actionable,
        artifact_durable=usable, version_publication_successful=usable,
        target_version_id="VER-1" if usable else None,
        candidate_a_current_version_id="VER-1" if usable else None,
        incident_rca_version_id="VER-1" if usable else None,
        upstream=upstream or _upstream_success(),
    )


class _Observer:
    def __init__(self, clock, factory=None):
        self.clock = clock; self.calls = []
        self.factory = factory or (lambda execution_id, call: _observation(clock))
    def observe(self, execution_id):
        self.calls.append(execution_id)
        return self.factory(execution_id, len(self.calls))


class _Injector:
    def __init__(self, action=None): self.calls = []; self.action = action
    def inject(self, execution):
        self.calls.append(execution.execution_id)
        if self.action: self.action(execution)


def _runner(store, clock, observer=None, injector=None, reconcile=None):
    return CampaignRunner(
        ledger=store, clock=clock, observer=observer or _Observer(clock),
        injector=injector or _Injector(), grader=lambda _: True,
        publication_reconciler=reconcile,
    )


def _result(execution, disposition, *, latency=None, success=False, stage="DONE"):
    return ExecutionResult(
        execution, disposition, success, KnowledgeDisposition.MATCH, FailureCase.NONE,
        stage, latency_ms=latency,
        cutoff_ms=100 if disposition is ExecutionDisposition.RIGHT_CENSORED_UNFINISHED else None,
    )


def test_fixed_n_order_is_precommitted_and_runner_never_early_stops(tmp_path):
    oracle = _oracle(); plan = _plan(oracle, n=2)
    order = tuple((scenario, repetition) for scenario in reversed(tuple(f"S{i}" for i in range(1, 7)))
                  for repetition in (2, 1))
    campaign = _campaign(plan, order=order); manifest = _manifest(plan, campaign)
    assert tuple((item.scenario_id, item.repetition) for item in campaign.executions) == order
    clock = _Clock(); injector = _Injector()
    failure_id = campaign.executions[0].execution_id
    observer = _Observer(clock, lambda eid, _: _observation(
        clock, usable=eid != failure_id,
        upstream=(UpstreamOutcome(
            status=AuthoritativeOutcomeStatus.TERMINAL_FAILURE,
            failure_case=FailureCase.LLM_TIMEOUT_OR_FAILURE,
        ) if eid == failure_id else _upstream_success())))
    with _store(tmp_path, oracle, plan) as store:
        results = _runner(store, clock, observer, injector).run(
            admitted_plan=plan, campaign=campaign, manifest=manifest)
    assert len(results) == 12 and injector.calls == list(manifest.execution_ids)
    assert results[0].disposition is ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE


def test_aggregation_macro_micro_and_invalid_quality_denominator():
    campaign = _campaign(_plan(_oracle(), n=2))
    results = []
    for item in campaign.executions:
        if item.scenario_id == "S1" and item.repetition == 1:
            results.append(_result(item, ExecutionDisposition.INVALID_EVALUATION))
        elif item.scenario_id == "S2":
            results.append(_result(item, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE))
        else:
            results.append(_result(item, ExecutionDisposition.OBSERVED_COMPLETION,
                                   latency=10, success=True))
    summary = aggregate(tuple(results), expected_execution_ids=tuple(
        item.execution_id for item in campaign.executions))
    assert summary.planned == 12 and summary.quality_eligible == 12 and summary.invalid == 1
    assert summary.overall_micro_rate == pytest.approx(9 / 12)
    assert summary.scenario_equal_macro_rate == pytest.approx((0.5 + 0 + 4) / 6)
    assert {row.scenario_id: row.quality_rate for row in summary.per_scenario}["S2"] == 0


def test_latency_aggregation_uses_observed_completions_only():
    campaign = _campaign(_plan(_oracle()))
    dispositions = (
        ExecutionDisposition.OBSERVED_COMPLETION,
        ExecutionDisposition.OBSERVED_COMPLETION,
        ExecutionDisposition.RIGHT_CENSORED_UNFINISHED,
        ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE,
        ExecutionDisposition.INVALID_EVALUATION,
        ExecutionDisposition.RIGHT_CENSORED_UNFINISHED,
    )
    results = tuple(_result(item, disposition, latency=(10, 20)[index] if index < 2 else None,
                            success=index < 2)
                    for index, (item, disposition) in enumerate(zip(campaign.executions, dispositions)))
    summary = aggregate(results, expected_execution_ids=tuple(
        item.execution_id for item in campaign.executions))
    assert summary.observed_latency_count == 2
    assert summary.median_latency_ms == 15 and summary.p95_latency_ms == 20
    assert "SMALL-SAMPLE LIMITATION" in summary.latency_estimability
    assert nearest_rank((1, 2, 3, 4, 100), 95) == 100


def test_latency_no_observed_sample_is_not_estimable():
    campaign = _campaign(_plan(_oracle()))
    results = tuple(_result(item, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED)
                    for item in campaign.executions)
    summary = aggregate(results, expected_execution_ids=tuple(
        item.execution_id for item in campaign.executions))
    assert summary.median_latency_ms is None and summary.p95_latency_ms is None
    assert summary.latency_estimability == "NOT ESTIMABLE — NO OBSERVED COMPLETIONS"


def test_invalid_evaluation_remains_original_slot_and_replacement_is_not_authorized(tmp_path):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
    manifest = _manifest(plan, campaign); clock = _Clock()
    invalid_id = campaign.executions[0].execution_id
    injector = _Injector(lambda execution: (_ for _ in ()).throw(
        RuntimeError("evaluation harness failed")) if execution.execution_id == invalid_id else None)
    with _store(tmp_path, oracle, plan) as store:
        runner = _runner(store, clock, injector=injector)
        original = runner.run(admitted_plan=plan, campaign=campaign, manifest=manifest)
        with pytest.raises(ValueError, match="does not authorize replacement"):
            campaign.replacement_for(invalid_id)
        invalid_final = store.get(identity("observation", [invalid_id, "final"]))
        assert invalid_final is not None
    summary = aggregate(original, expected_execution_ids=tuple(
        item.execution_id for item in campaign.executions))
    assert (summary.planned, summary.quality_eligible, summary.invalid,
            summary.replacement_executions) == (6, 6, 1, 0)


def test_extra_completed_rerun_cannot_masquerade_as_replacement():
    campaign = _campaign(_plan(_oracle()))
    original = tuple(_result(item, ExecutionDisposition.OBSERVED_COMPLETION,
                             latency=1, success=True) for item in campaign.executions)
    first = campaign.executions[0]
    extra = PlannedExecution(
        identity("execution", [campaign.run_id, "extra", 1]), first.scenario_id,
        first.repetition, first.input_identity, first.input_commitment,
    )
    with pytest.raises(ValueError, match="masquerade"):
        aggregate(original + (_result(extra, ExecutionDisposition.OBSERVED_COMPLETION,
                                      latency=1, success=True),),
                  expected_execution_ids=tuple(item.execution_id for item in campaign.executions))


def test_actionable_incident_requires_each_of_three_conditions():
    base = dict(observed_at_utc="now", event_has_unique_terminal_incident_owner=True,
                incident_durable_readable=True, incident_membership_coherent=True)
    assert ExternalObservation(**base).actionable_incident
    for field in (
        "event_has_unique_terminal_incident_owner", "incident_durable_readable",
        "incident_membership_coherent",
    ):
        assert not ExternalObservation(**{**base, field: False}).actionable_incident


def test_usable_rca_requires_matching_current_version_and_incident_relationship():
    base = dict(
        observed_at_utc="now", event_has_unique_terminal_incident_owner=True,
        incident_durable_readable=True, incident_membership_coherent=True,
        artifact_durable=True, version_publication_successful=True,
        target_version_id="v2", candidate_a_current_version_id="v2",
        incident_rca_version_id="v2", upstream=_upstream_success(),
    )
    assert ExternalObservation(**base).usable_rca
    for field in ("candidate_a_current_version_id", "incident_rca_version_id"):
        assert not ExternalObservation(**{**base, field: "v1"}).usable_rca
    assert not ExternalObservation(**{**base, "version_publication_successful": False}).usable_rca


@pytest.mark.parametrize("knowledge", (
    KnowledgeDisposition.INVALID, KnowledgeDisposition.REPAIR_REQUIRED,
))
def test_knowledge_integrity_precedes_apparent_success(tmp_path, knowledge):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan); clock = _Clock()
    observer = _Observer(clock, lambda *_: _observation(
        clock, upstream=_upstream_success(knowledge=knowledge)))
    with _store(tmp_path, oracle, plan) as store:
        results = _runner(store, clock, observer=observer).run(
            admitted_plan=plan, campaign=campaign, manifest=_manifest(plan, campaign))
    assert all(item.disposition is ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE
               for item in results)


@pytest.mark.parametrize("case", (
    FailureCase.LLM_TIMEOUT_OR_FAILURE, FailureCase.RETRY_EXHAUSTION,
    FailureCase.SCHEMA_OR_VALIDATION_FAILURE,
))
def test_typed_generation_failures_precede_apparent_usable_endpoint(tmp_path, case):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan); clock = _Clock()
    outcome = UpstreamOutcome(status=AuthoritativeOutcomeStatus.TERMINAL_FAILURE,
                              failure_case=case, failure_stage="GENERATION")
    with _store(tmp_path, oracle, plan) as store:
        results = _runner(store, clock, observer=_Observer(
            clock, lambda *_: _observation(clock, upstream=outcome))).run(
                admitted_plan=plan, campaign=campaign, manifest=_manifest(plan, campaign))
    assert all(item.disposition is ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE
               for item in results)


@pytest.mark.parametrize("case", (
    FailureCase.LOKI_UNAVAILABLE, FailureCase.PROMETHEUS_UNAVAILABLE,
))
def test_degraded_is_derived_from_typed_source_omission(tmp_path, case):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan); clock = _Clock()
    outcome = _upstream_success(
        failure_case=case, evidence_completeness=EvidenceCompleteness.DEGRADED,
        omission_facts=(case.value,),
    )
    with _store(tmp_path, oracle, plan) as store:
        results = _runner(store, clock, observer=_Observer(
            clock, lambda *_: _observation(clock, upstream=outcome))).run(
                admitted_plan=plan, campaign=campaign, manifest=_manifest(plan, campaign))
    assert all(item.disposition is ExecutionDisposition.VALID_DEGRADED_COMPLETION
               for item in results)
    with pytest.raises(TypeError):
        UpstreamOutcome(degraded=True)  # type: ignore[call-arg]


def test_matching_no_match_and_retrieval_unavailable_semantics_are_allowed():
    no_match = _upstream_success(
        failure_case=FailureCase.KNOWLEDGE_NO_MATCH,
        knowledge=KnowledgeDisposition.NO_MATCH,
    )
    unavailable = _upstream_success(
        failure_case=FailureCase.KNOWLEDGE_RETRIEVAL_UNAVAILABLE,
        knowledge=KnowledgeDisposition.RETRIEVAL_UNAVAILABLE,
        evidence_completeness=EvidenceCompleteness.DEGRADED,
        omission_facts=("retrieval unavailable",),
    )
    assert not no_match.terminal_failure and not no_match.valid_degraded_completion
    assert unavailable.valid_degraded_completion


@pytest.mark.parametrize(("knowledge", "case"), (
    (KnowledgeDisposition.RETRIEVAL_UNAVAILABLE, FailureCase.KNOWLEDGE_NO_MATCH),
    (KnowledgeDisposition.NO_MATCH, FailureCase.KNOWLEDGE_RETRIEVAL_UNAVAILABLE),
))
def test_knowledge_disposition_failure_case_mismatch_fails_closed_before_classification(
    knowledge, case,
):
    with pytest.raises(ValueError, match="semantically inconsistent"):
        _upstream_success(
            knowledge=knowledge, failure_case=case,
            evidence_completeness=EvidenceCompleteness.DEGRADED,
            omission_facts=("must never reach usable/degraded classification",),
        )


def test_runner_preserves_no_match_and_unavailable_classifications(tmp_path):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan); clock = _Clock()
    first_id = campaign.executions[0].execution_id
    no_match = _upstream_success(
        failure_case=FailureCase.KNOWLEDGE_NO_MATCH,
        knowledge=KnowledgeDisposition.NO_MATCH,
    )
    unavailable = _upstream_success(
        failure_case=FailureCase.KNOWLEDGE_RETRIEVAL_UNAVAILABLE,
        knowledge=KnowledgeDisposition.RETRIEVAL_UNAVAILABLE,
        evidence_completeness=EvidenceCompleteness.DEGRADED,
        omission_facts=("retrieval unavailable",),
    )
    observer = _Observer(clock, lambda execution_id, _: _observation(
        clock, upstream=no_match if execution_id == first_id else unavailable))
    with _store(tmp_path, oracle, plan) as store:
        results = _runner(store, clock, observer=observer).run(
            admitted_plan=plan, campaign=campaign, manifest=_manifest(plan, campaign))
    assert results[0].knowledge is KnowledgeDisposition.NO_MATCH
    assert results[0].disposition is ExecutionDisposition.OBSERVED_COMPLETION
    assert all(item.knowledge is KnowledgeDisposition.RETRIEVAL_UNAVAILABLE
               and item.disposition is ExecutionDisposition.VALID_DEGRADED_COMPLETION
               for item in results[1:])


def _state(*, current="v1", freshness=Freshness.FRESH,
           attempts=("a1",), tries=("t1",), versions=("v1",), publications=("p1",),
           frontier=1, authorized=("t1",), failed=(),
           try_lineage=(("t1", "a1"),), version_lineage=(("v1", "a1"),),
           publication_lineage=(("p1", "v1"),), history=("v1",), effects=()):
    return RcaPublicState(
        current, freshness, attempts, tries, versions, publications,
        f"frontier-{frontier}", frontier, authorized, failed, try_lineage,
        version_lineage, publication_lineage, history, effects,
    )


def test_material_refresh_validates_complete_authorized_transition():
    v1 = _state(); stale = replace(v1, freshness=Freshness.STALE)
    v2 = _state(
        current="v2", attempts=("a1", "a2"), tries=("t1", "t2"),
        versions=("v1", "v2"), publications=("p1", "p2"), frontier=2,
        authorized=("t1", "t2"), try_lineage=(("t1", "a1"), ("t2", "a2")),
        version_lineage=(("v1", "a1"), ("v2", "a2")),
        publication_lineage=(("p1", "v1"), ("p2", "v2")), history=("v1", "v2"),
    )
    result = _evaluate_refresh_states(
        before=v1, after_materiality=stale, final=v2,
        materiality=Materiality.MATERIAL, refresh_succeeded=True)
    assert result.passed
    assert not _evaluate_refresh_states(
        before=v1, after_materiality=stale,
        final=replace(v2, publication_version_lineage=(("p1", "v1"),)),
        materiality=Materiality.MATERIAL, refresh_succeeded=True).passed


def test_failed_refresh_retains_stale_current_and_consistent_failed_try():
    v1 = _state(); stale = replace(v1, freshness=Freshness.STALE)
    failed = _state(
        freshness=Freshness.STALE, attempts=("a1", "a2"), tries=("t1", "t2"),
        frontier=2, authorized=("t1", "t2"), failed=("t2",),
        try_lineage=(("t1", "a1"), ("t2", "a2")),
    )
    result = _evaluate_refresh_states(
        before=v1, after_materiality=stale, final=failed,
        materiality=Materiality.MATERIAL, refresh_succeeded=False)
    assert result.passed and result.disposition == "REFRESH_FAILED_CURRENT_RETAINED_STALE"


def test_non_material_forbids_try_publication_and_frontier_advance():
    v1 = _state()
    assert _evaluate_refresh_states(
        before=v1, after_materiality=v1, final=v1,
        materiality=Materiality.NON_MATERIAL, refresh_succeeded=False).passed
    changed = replace(v1, try_outcome_ids=("t1", "t2"), publication_ids=("p1", "p2"),
                      frontier_ordinal=2, frontier_identity="frontier-2")
    assert not _evaluate_refresh_states(
        before=v1, after_materiality=v1, final=changed,
        materiality=Materiality.NON_MATERIAL, refresh_succeeded=False).passed


@pytest.mark.parametrize("boundary", tuple(RecoveryBoundary))
def test_restart_boundaries_allow_one_same_operation_completion(boundary):
    operation = "operation:1"
    before = _state(frontier=0, effects=())
    effect_identity = f"effect:{boundary.value}"
    after = replace(
        before, frontier_ordinal=1, frontier_identity="frontier-1",
        semantic_effects=((boundary.value, effect_identity, operation),),
        try_outcome_ids=(before.try_outcome_ids + (effect_identity,)
                         if boundary is RecoveryBoundary.GENERATION else before.try_outcome_ids),
        version_ids=(before.version_ids + (effect_identity,)
                     if boundary is RecoveryBoundary.ARTIFACT_COMMIT else before.version_ids),
        publication_ids=(before.publication_ids + (effect_identity,)
                         if boundary is RecoveryBoundary.PUBLICATION else before.publication_ids),
    )
    result = _evaluate_restart_states(
        before=before, after=after, expected_operation_id=operation,
        completed_operation_id=operation, boundary=boundary, signal_injection_count=1,
        publication_reconciled_before_resume=True,
    )
    assert result.passed


def test_restart_duplicate_effect_or_identity_fails_closed():
    operation = "operation:1"; before = _state(frontier=0, effects=())
    duplicate = replace(
        before, frontier_ordinal=2, frontier_identity="frontier-2",
        try_outcome_ids=("t1", "t1"),
        semantic_effects=(("GENERATION", "effect:1", operation),
                          ("GENERATION", "effect:2", operation)),
    )
    result = _evaluate_restart_states(
        before=before, after=duplicate, expected_operation_id=operation,
        completed_operation_id=operation, boundary=RecoveryBoundary.GENERATION,
        signal_injection_count=1, publication_reconciled_before_resume=True)
    assert not result.passed
    ambiguous = _evaluate_restart_states(
        before=before, after=before, expected_operation_id=operation,
        completed_operation_id=operation, boundary=RecoveryBoundary.EVIDENCE,
        signal_injection_count=1, publication_reconciled_before_resume=True,
        recovery_state_ambiguous=True)
    assert not ambiguous.passed and ambiguous.disposition == "RECOVERY_AMBIGUOUS"


class _Crash(BaseException): pass


def test_runner_restart_reconciles_publication_first_and_never_reinjects(tmp_path):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan); clock = _Clock()
    events = []
    class CrashOnce(_Injector):
        def inject(self, execution):
            self.calls.append(execution.execution_id)
            if len(self.calls) == 1: raise _Crash()
    injector = CrashOnce()
    with _store(tmp_path, oracle, plan) as store:
        runner = _runner(store, clock, injector=injector,
                         reconcile=lambda eid: events.append("reconcile:" + eid))
        with pytest.raises(_Crash):
            runner.run(admitted_plan=plan, campaign=campaign, manifest=_manifest(plan, campaign))
        results = runner.run(admitted_plan=plan, campaign=campaign,
                             manifest=_manifest(plan, campaign))
    assert injector.calls.count(campaign.executions[0].execution_id) == 1
    assert events == ["reconcile:" + campaign.executions[0].execution_id]
    assert results[0].disposition is ExecutionDisposition.OBSERVED_COMPLETION


@pytest.mark.parametrize("mismatch", (
    "scenario_input", "oracle_content", "config", "clock", "plan",
))
def test_manifest_admission_fails_closed_on_behavioral_pin_mismatch(mismatch):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
    if mismatch == "scenario_input":
        inputs = list(tuple((item.scenario_id, item.input_identity, item.input_commitment)
                            for item in campaign.executions))
        inputs[0] = ("S2", inputs[0][1], inputs[0][2])
        manifest = _manifest(plan, campaign, scenario_inputs=tuple(inputs))
    elif mismatch == "oracle_content":
        manifest = _manifest(plan, campaign, oracle_content_commitment="sha256:" + "0" * 64)
    elif mismatch == "config":
        manifest = _manifest(plan, campaign, config={"gate": "different"})
    elif mismatch == "clock":
        manifest = _manifest(plan, campaign, clock_source="different-clock")
    else:
        other = _plan(oracle, fixture_revision="fixtures-r2")
        manifest = _manifest(plan, campaign)
        with pytest.raises(ValueError, match="mismatch"):
            manifest.validate_admission(other, campaign)
        return
    with pytest.raises(ValueError, match="mismatch"):
        manifest.validate_admission(plan, campaign)


def test_manifest_requires_explicit_seed_or_deterministic_no_seed_and_clock_facts():
    oracle = _oracle(); plan = _plan(oracle)
    with pytest.raises(ValueError, match="deterministic_order_commitment"):
        _admission(random_seed=None, deterministic_order_commitment=None)
    deterministic = _admission(
        random_seed=None, deterministic_order_commitment=commitment({"order": "lexical"}))
    campaign = _campaign(plan, admission=deterministic)
    _manifest(plan, campaign).validate_admission(plan, campaign)
    with pytest.raises(ValueError, match="clock_source"):
        _admission(clock_source="")


class _SyntheticEvaluatorUnitWorld:
    """Retired synthetic world; cannot qualify as formal positive evidence."""

    def __init__(self, suffix):
        raise TypeError("synthetic restart world is negative-test-only")
        self.incident_id = f"INC-{suffix}"
        self.aggregate_id = f"AGG-{suffix}"
        self.current_id = "v1"
        self.freshness = CurrentFreshness.FRESH
        self.basis = "ER-BASE"
        self.versions = [self._version("v1", "a1", "p1")]
        self.tries = {"a1": [self._try("a1", 1, True)]}
        self.a_publications = {"p1": self._a_publication("v1", True)}
        self.i_publications = {"p1": self._i_publication("v1")}
        self.freshness_lineage = [self._current()]
        self.materiality = {}
        self.runtime = {}
        self.relationship_version = "v1"

    def _current(self):
        return SimpleNamespace(
            aggregate_id=self.aggregate_id,
            current_version_id=self.current_id, freshness=self.freshness,
            material_evidence_revision_basis=self.basis)

    def _version(self, version_id, attempt_id, publication_id):
        return SimpleNamespace(
            version_id=version_id, aggregate_id=self.aggregate_id,
            attempt_id=attempt_id,
            publication_operation_id=publication_id)

    @staticmethod
    def _try(attempt_id, ordinal, success):
        return SimpleNamespace(
            identity=SimpleNamespace(
                attempt_id=attempt_id, try_ordinal=ordinal),
            result_kind=(LogicalTryResultKind.VALIDATED_RESULT if success
                         else LogicalTryResultKind.FAILURE))

    def _a_publication(self, version_id, applied):
        return SimpleNamespace(
            target=SimpleNamespace(
                target_version_id=version_id,
                aggregate_id=self.aggregate_id),
            disposition=(PublicationDisposition.APPLIED if applied
                         else PublicationDisposition.A_SIDE_COMMITTED),
            resulting_current_version_id=version_id if applied else None)

    def _i_publication(self, version_id):
        from incident_management.contracts import (
            IncidentRcaPublicationDisposition,
        )
        return SimpleNamespace(
            publication_operation_id=f"p{version_id[-1]}",
            target_version_id=version_id, incident_id=self.incident_id,
            disposition=IncidentRcaPublicationDisposition.APPLIED,
            resulting_current_version_id=version_id)

    def get_current(self, aggregate_id):
        if aggregate_id != self.aggregate_id:
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
            try_outcomes=tuple(outcomes))

    def get_publication_result(self, operation_id):
        return self.a_publications.get(operation_id)

    def get_rca_relationship(self, incident_id):
        return SimpleNamespace(
            incident_id=self.incident_id,
            current_version_id=self.relationship_version)

    def get_rca_publication_result(self, operation_id):
        return self.i_publications.get(operation_id)

    def read_materiality_result(self, result_id):
        return self.materiality.get(result_id)

    def get(self, work_id):
        return self.runtime.get(work_id)


class _SyntheticEvaluatorUnitHarness:
    def __init__(self, campaign, clock, *, fail_material_refresh=False):
        raise TypeError("synthetic restart harness cannot execute formal cases")
        self.campaign = campaign
        self.clock = clock
        self.worlds = {}
        self.resolvers = {}
        self.injected = []
        self.restarted = []
        self.reconciled = []
        self.fail_material_refresh = fail_material_refresh
        self.cases = {item.case_id: item for item in campaign.recovery_cases}
        for case in campaign.recovery_cases:
            world = _SyntheticEvaluatorUnitWorld(case.fixture.fixture_identity)
            work = f"WORK:{case.fixture.fixture_identity}"
            operation = f"OP:{case.fixture.fixture_identity}"
            world.runtime[work] = SimpleNamespace(
                work_id=work, operation_id=operation,
                workflow_operation_id=None,
                status=RuntimeWorkStatus.OUTSTANDING)
            self.worlds[case.case_id] = (world, work)
            self.resolvers[case.case_id] = RcaPublicStateResolver(
                world, world, world, world, clock,
                clock_source=campaign.admission.clock_source)

    def _reference(self, case, *, attempts=("a1",), materiality=None):
        world, work = self.worlds[case.case_id]
        return RecoveryStateReference(
            self.campaign.run_id, case.parent_execution_id,
            world.incident_id, world.aggregate_id, attempts,
            materiality_result_id=materiality, runtime_work_id=work)

    def inject(self, request):
        assert not hasattr(request, "scenario_id")
        assert not hasattr(request, "expected_public_observation_class")
        assert not hasattr(request, "oracle_scenario_reference")
        case = self.cases[request.case_id]
        self.injected.append(request.case_id)
        if case.fixture.recovery_boundary == "PUBLICATION":
            world, _ = self.worlds[case.case_id]
            world.versions.append(world._version("v2", "a1", "p2"))
            world.a_publications["p2"] = world._a_publication("v2", False)

    def restart(self, request):
        case = self.cases[request.case_id]
        self.restarted.append(request.case_id)
        world, work = self.worlds[case.case_id]
        boundary = case.fixture.recovery_boundary
        if boundary == "GENERATION":
            world.tries["a1"].append(world._try("a1", 2, True))
        elif boundary == "ARTIFACT_COMMIT":
            world.versions.append(world._version("v2", "a1", "p2"))
            world.a_publications["p2"] = world._a_publication("v2", False)
        elif boundary == "PUBLICATION":
            world.a_publications["p2"] = world._a_publication("v2", True)
            world.i_publications["p2"] = world._i_publication("v2")
            world.current_id = world.relationship_version = "v2"
            world.freshness_lineage.append(world._current())
        world.runtime[work].status = RuntimeWorkStatus.COMPLETED
        self.clock.ms += 5

    def reconcile_publication(self, request):
        self.reconciled.append(request.case_id)

    def observe_refresh(self, case):
        world, _ = self.worlds[case.case_id]
        resolver = self.resolvers[case.case_id]
        before = resolver.resolve(self._reference(case))
        request = SimpleNamespace(
            baseline_revision_id="ER-BASE", candidate_revision_id="ER-BASE")
        if case.fixture.variant == "material_refresh":
            baseline = world._current()
            world.freshness = CurrentFreshness.STALE
            world.basis = "ER-MAT"
            world.freshness_lineage = [baseline, world._current()]
            request.candidate_revision_id = "ER-MAT"
            judgement = MaterialityJudgement.MATERIAL
        else:
            judgement = MaterialityJudgement.NON_MATERIAL
        world.materiality["MAT-1"] = SimpleNamespace(
            materiality_result_id="MAT-1", request=request,
            judgement=judgement)
        self.clock.ms += 5
        middle = resolver.resolve(self._reference(case, materiality="MAT-1"))
        if (judgement is MaterialityJudgement.MATERIAL
                and self.fail_material_refresh):
            world.tries["a2"] = [world._try("a2", 1, False)]
            attempts = ("a1", "a2")
        elif judgement is MaterialityJudgement.MATERIAL:
            world.current_id = world.relationship_version = "v2"
            world.freshness = CurrentFreshness.FRESH
            world.versions.append(world._version("v2", "a2", "p2"))
            world.tries["a2"] = [world._try("a2", 1, True)]
            world.a_publications["p2"] = world._a_publication("v2", True)
            world.i_publications["p2"] = world._i_publication("v2")
            world.freshness_lineage.append(world._current())
            attempts = ("a1", "a2")
        else:
            attempts = ("a1",)
        world.runtime[self.worlds[case.case_id][1]].status = (
            RuntimeWorkStatus.COMPLETED)
        self.clock.ms += 5
        final = resolver.resolve(self._reference(case, attempts=attempts))
        return before, middle, final

    def observe_restart(self, case, *, phase):
        return self.resolvers[case.case_id].resolve(self._reference(case))


def test_synthetic_restart_world_cannot_qualify_as_formal_positive_evidence():
    with pytest.raises(TypeError, match="negative-test-only"):
        _SyntheticEvaluatorUnitWorld("forged")


def test_required_fixture_admission_and_campaign_coverage_fail_closed():
    plan = _plan(_oracle())
    campaign = _campaign(plan)
    assert len(campaign.required_refresh_case_ids) == 3
    assert len(campaign.required_restart_case_ids) == 5
    assert {item.fixture.recovery_boundary for item in campaign.recovery_cases
            if item.kind == "RESTART"} == {item.value for item in RecoveryBoundary}
    required = tuple(item for item in OFFICIAL_FIXTURES.entries
                     if item.variant in {
                         "material_refresh", "refresh_failure", "non_material",
                         "restart_boundary"})
    for entry in required:
        admitted = OFFICIAL_FIXTURES.admit(FixtureAdmissionRequest(
            entry.scenario_id, entry.variant, entry.input_identity,
            entry.input_commitment, entry.oracle_scenario_reference,
            entry.recovery_boundary))
        assert admitted.fixture_identity == entry.fixture_identity
    entry = next(item for item in required
                 if item.variant == "material_refresh")
    with pytest.raises(ValueError, match="do not match"):
        OFFICIAL_FIXTURES.admit(FixtureAdmissionRequest(
            entry.scenario_id, entry.variant, entry.input_identity,
            "sha256:" + "0" * 64, entry.oracle_scenario_reference))
    with pytest.raises(ValueError, match="required refresh case set is empty"):
        replace(campaign, recovery_cases=tuple(
            item for item in campaign.recovery_cases
            if item.kind == "RESTART")).required_refresh_case_ids
    with pytest.raises(ValueError, match="required restart case set is empty"):
        replace(campaign, recovery_cases=tuple(
            item for item in campaign.recovery_cases
            if item.kind == "REFRESH")).required_restart_case_ids


@pytest.mark.parametrize("crlf", (False, True))
def test_official_fixture_sources_are_portable_across_controlled_eol(tmp_path, crlf):
    _portable_fixture_repository(tmp_path, crlf=crlf)
    loaded = load_fixture_manifest(
        ROOT / "configs/rca_evaluation/fixtures/spec017_s1_s6_fixture_manifest_v1.json",
        oracle=OFFICIAL_ORACLE, repository_root=tmp_path)
    assert loaded.manifest_commitment == OFFICIAL_FIXTURE_MANIFEST_COMMITMENT


def test_fixture_source_content_change_and_old_crlf_commitment_fail_closed(tmp_path):
    _portable_fixture_repository(tmp_path, mutate_scenarios=True)
    with pytest.raises(ValueError, match="source commitment mismatch"):
        load_fixture_manifest(
            ROOT / "configs/rca_evaluation/fixtures/spec017_s1_s6_fixture_manifest_v1.json",
            oracle=OFFICIAL_ORACLE, repository_root=tmp_path)

    _portable_fixture_repository(tmp_path)
    raw = json.loads((
        ROOT / "configs/rca_evaluation/fixtures/spec017_s1_s6_fixture_manifest_v1.json"
    ).read_bytes())
    for entry in raw["entries"]:
        if entry["source_reference"] and entry["source_reference"].startswith(
            "configs/scenarios.yaml#"
        ):
            entry["source_commitment"] = (
                "sha256:baa97bce3afe413049c08b726e2d75794019755db358a700ea3990aaa6d97f8e"
            )
    altered = tmp_path / "old-crlf-manifest.json"
    altered.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="source commitment mismatch"):
        load_fixture_manifest(
            altered, oracle=OFFICIAL_ORACLE, repository_root=tmp_path)


def test_official_fixture_manifest_reconciles_canonical_source_commitment():
    path = ROOT / "configs/rca_evaluation/fixtures/spec017_s1_s6_fixture_manifest_v1.json"
    raw = json.loads(path.read_bytes())
    assert commitment(raw) == OFFICIAL_FIXTURE_MANIFEST_COMMITMENT
    nominal = [item for item in raw["entries"] if item["variant"] == "nominal"]
    assert {item["source_commitment"] for item in nominal} == {
        "sha256:48bbb51aa2595972707101b95ae13b1e9707140ebc56ea0b8da6a9e331826494"
    }
