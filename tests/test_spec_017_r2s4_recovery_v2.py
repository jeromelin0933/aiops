"""PM-SPEC017-R2S4-20261013 governed fixture/control regressions."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from incident_evidence import (
    BoundsOmissionFacts, EvidenceSource, MaterialityEvaluationKind,
    MaterialityJudgement, MaterialityRequest, SourceStatus,
    SqliteEvidenceStore,
)
from incident_management import IncidentManager, SqliteIncidentStore
from rca_integration import RcaPublicationCoordinator
from rca_persistence import (
    AdmitAttemptRequest, AdmittedRetryDisposition, AttemptLineage,
    CreateAggregateRequest, CurrentFreshness, GenerationProvenance,
    LogicalTryIdentity, LogicalTryOutcome, LogicalTryResultKind,
    PublicationTargetIdentity, SqliteRcaStore,
)
from runtime_orchestration.contracts import (
    RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus,
)
from runtime_orchestration.identity import runtime_work_id
from runtime_orchestration.rca_host import RcaRecoveryKind, RcaRecoverySubject
from runtime_orchestration.rca_continuation import (
    FollowUpRequirement, RcaContinuation, SqliteRcaContinuationStore,
    rca_root_id,
)
from runtime_orchestration.rca_followup import (
    FollowUpDisposition, RcaFollowUpOrchestrator,
)
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore
from rca_evaluation.evaluation_controls import (
    ControlledEvaluationInterruption, GovernedCheckpointAdapter, arm_failpoint,
    load_evaluation_control_artifact,
)
from rca_evaluation.identity import canonical_text_bytes, commitment
from rca_evaluation.ledger import LedgerRecord
from rca_evaluation.recovery_records import (
    ValidatedDeviationResult, ValidatedRecoveryResult,
    evaluate_and_issue_checkpoint_recovery_result,
    evaluate_and_issue_refresh_failure_result,
    evaluate_and_issue_refresh_result, issue_deviation_result,
    issue_recovery_result,
    record_refresh_failure_observation,
    validated_deviation_result, validated_recovery_result,
)
from rca_evaluation.recovery_evaluation import (
    RecoveryStateReference, RcaPublicStateResolver, evaluate_refresh,
)
from rca_evaluation.identity import identity

from test_incident_evidence_capture import (
    FakeAdapter, capture_command, capture_service,
)
from test_incident_manager import _create_request
from test_rca_persistence_version_artifact import artifact
from test_spec_016_crash_restart import _execution_knowledge
from test_spec_016_crash_restart import (
    _close_execution, _close_s2_publication, _execution_instances,
    _execution_setup, _s2_publication_instances, _s2_publication_setup,
)
from test_spec_016_followup_refresh import NOW, _Time
from test_spec_017_s4_campaign_recovery import (
    OFFICIAL_FIXTURES, _Clock, _campaign, _manifest, _oracle, _plan, _runner,
    _store,
)
from rca_evaluation.fixtures import FixtureAdmissionRequest
from rca_evaluation.runner import (
    RecoveryControlRequest, RecoveryInterruptionReceipt,
)


ARTIFACT_PATH = Path(
    "configs/rca_evaluation/fixtures/spec017_r2s4_evaluation_controls_v2.json"
)


@pytest.mark.parametrize("crlf", (False, True))
def test_evaluation_control_artifact_is_portable_across_controlled_eol(
    tmp_path, crlf,
):
    content = canonical_text_bytes(ARTIFACT_PATH.read_bytes())
    if crlf:
        content = content.replace(b"\n", b"\r\n")
    candidate = tmp_path / "controls.json"
    candidate.write_bytes(content)
    artifact = load_evaluation_control_artifact(candidate)
    assert artifact.pm_approval_reference == "PM-SPEC017-R2S4-20261013"


def test_evaluation_control_artifact_content_mutation_fails_closed(tmp_path):
    content = canonical_text_bytes(ARTIFACT_PATH.read_bytes()).replace(
        b'"fixture_set_revision": "v2"',
        b'"fixture_set_revision": "v3"',
    )
    candidate = tmp_path / "controls.json"
    candidate.write_bytes(content)
    with pytest.raises(ValueError, match="commitment mismatch"):
        load_evaluation_control_artifact(candidate)


class _OmissionAdapter(FakeAdapter):
    def __init__(self, marker: str, *, observed: int, omitted: int):
        super().__init__(SourceStatus.AVAILABLE, marker=marker)
        self._observed = observed
        self._omitted = omitted

    def collect(self, request):
        result = super().collect(request)
        bounds = replace(
            result.summary.bounds,
            observed_candidate_count=self._observed,
            omitted_count=self._omitted,
            omission_reason=("evaluation source bound" if self._omitted else None),
            dedup_applied=bool(self._omitted),
            dedup_input_count=(self._observed if self._omitted else None),
            dedup_output_count=(1 if self._omitted else None),
        )
        return replace(result, summary=replace(result.summary, bounds=bounds))


def _capture_pair(tmp_path, fixture, *, omission=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SqliteEvidenceStore(tmp_path / "evidence.sqlite")
    baseline_source = fixture.baseline_source
    candidate_source = fixture.candidate_source
    if omission:
        baseline_adapters = {
            source: _OmissionAdapter(
                str(baseline_source["loki_marker"]),
                observed=int(baseline_source["observed_candidate_count"]),
                omitted=int(baseline_source["omitted_candidate_count"]),
            ) for source in EvidenceSource
        }
        candidate_adapters = {
            source: _OmissionAdapter(
                str(candidate_source["loki_marker"]),
                observed=int(candidate_source["observed_candidate_count"]),
                omitted=int(candidate_source["omitted_candidate_count"]),
            ) for source in EvidenceSource
        }
    else:
        baseline_adapters = {
            source: FakeAdapter(
                SourceStatus.AVAILABLE,
                marker=str(baseline_source[
                    "loki_marker" if source is EvidenceSource.LOKI
                    else "prometheus_value"]),
            ) for source in EvidenceSource
        }
        candidate_adapters = {
            source: FakeAdapter(
                SourceStatus.AVAILABLE,
                marker=str(candidate_source[
                    "loki_marker" if source is EvidenceSource.LOKI
                    else "prometheus_value"]),
            ) for source in EvidenceSource
        }
    baseline_service, *_ = capture_service(
        tmp_path / "baseline", store=store, adapters=baseline_adapters)
    baseline = baseline_service.capture_evidence(
        capture_command("SPEC017-S2-BASELINE"))
    candidate_service, *_ = capture_service(
        tmp_path / "candidate", store=store, adapters=candidate_adapters)
    candidate = candidate_service.capture_evidence(
        capture_command("SPEC017-S2-CANDIDATE"))
    assert getattr(baseline, "revision_id", None), baseline
    assert getattr(candidate, "revision_id", None), candidate
    request = MaterialityRequest(
        MaterialityEvaluationKind.PAIRWISE, candidate.revision_id,
        "evidence-materiality-v1", baseline.revision_id,
    )
    return store, baseline, candidate, candidate_service.compare_materiality(request)


def test_material_fixture_captures_two_authoritative_revisions_and_judges_material(
    tmp_path,
):
    artifact = load_evaluation_control_artifact(ARTIFACT_PATH)
    fixture = artifact.refresh("material_refresh")
    store, baseline, candidate, result = _capture_pair(tmp_path, fixture)
    try:
        assert artifact.classification == "EVALUATION ONLY"
        assert fixture.scenario_identity == "S2"
        assert baseline.revision_id != candidate.revision_id
        assert store.resolve_snapshot(baseline.snapshot_id) is not None
        assert store.resolve_snapshot(candidate.snapshot_id) is not None
        assert result.judgement is MaterialityJudgement.MATERIAL
        assert "normalized_evidence" in result.reason_facts[0]
    finally:
        store.close()


def test_nominal_s3_relabel_is_not_an_admitted_material_fixture():
    nominal = next(item for item in OFFICIAL_FIXTURES.entries
                   if item.scenario_id == "S3" and item.variant == "nominal")
    with pytest.raises(ValueError, match="absent or ambiguous"):
        OFFICIAL_FIXTURES.admit(FixtureAdmissionRequest(
            "S3", "material_refresh", nominal.input_identity,
            nominal.input_commitment, nominal.oracle_scenario_reference))


def test_same_semantic_capture_pair_cannot_become_material_or_non_material(tmp_path):
    artifact = load_evaluation_control_artifact(ARTIFACT_PATH)
    fixture = artifact.refresh("material_refresh")
    same_fixture = replace(
        fixture, candidate_source=dict(fixture.baseline_source))
    store, baseline, candidate, result = _capture_pair(tmp_path, same_fixture)
    try:
        assert baseline.revision_id == candidate.revision_id
        assert result.judgement is MaterialityJudgement.SAME
        assert result.judgement not in {
            MaterialityJudgement.MATERIAL,
            MaterialityJudgement.NON_MATERIAL,
        }
    finally:
        store.close()


def test_non_material_fixture_is_distinct_not_same_and_judges_non_material(
    tmp_path,
):
    artifact = load_evaluation_control_artifact(ARTIFACT_PATH)
    fixture = artifact.refresh("non_material")
    store, baseline, candidate, result = _capture_pair(
        tmp_path, fixture, omission=True)
    try:
        assert baseline.revision_id != candidate.revision_id
        assert result.judgement is MaterialityJudgement.NON_MATERIAL
        assert result.judgement is not MaterialityJudgement.SAME
        assert result.reason_facts == (
            "only collection boundary or omission facts differ",)
    finally:
        store.close()


def _build_refresh_world(tmp_path, fixture, *, omission=False):
    evidence, baseline, candidate, judgment = _capture_pair(
        tmp_path / "candidate-b", fixture, omission=omission)
    incident = SqliteIncidentStore(str(tmp_path / "incident.sqlite"))
    manager = IncidentManager(incident, incident_id_factory=lambda: "INC-1")
    manager.apply_correlation_mutation(_create_request())
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    knowledge_store, knowledge, knowledge_snapshot = _execution_knowledge(
        knowledge_root)
    knowledge_id = knowledge_snapshot.snapshot_key.value
    candidate_a = SqliteRcaStore(tmp_path / "candidate-a.sqlite")
    aggregate = "aggregate:eval-s2"
    candidate_a.create_or_discover_aggregate(
        CreateAggregateRequest("OP-AGG-EVAL", aggregate, "INC-1", NOW))
    generation = GenerationProvenance(
        "provider", "model", "prompt", "config", "profile")
    initial_lineage = AttemptLineage(
        "attempt:eval-1", aggregate, baseline.snapshot_id, baseline.revision_id,
        knowledge_id, generation)
    candidate_a.admit_attempt(AdmitAttemptRequest(
        "OP-ATT-EVAL-1", initial_lineage, NOW))
    candidate_a.record_try_outcome("OP-TRY-EVAL-1", LogicalTryOutcome(
        LogicalTryIdentity("attempt:eval-1", 1),
        LogicalTryResultKind.VALIDATED_RESULT,
        AdmittedRetryDisposition.NON_RETRYABLE, NOW,
        validated_result_id="candidate-d:validated:initial"))
    initial_artifact = artifact("Database latency baseline")
    initial_artifact = replace(
        initial_artifact,
        provenance=replace(
            initial_artifact.provenance,
            evidence_snapshot_id=baseline.snapshot_id,
            evidence_revision_id=baseline.revision_id,
            knowledge_snapshot_id=knowledge_id,
        ),
    )
    candidate_a.commit_validated_artifact(
        "OP-COMMIT-EVAL-1", "attempt:eval-1", initial_artifact,
        PublicationTargetIdentity(
            "PUB-EVAL-1", aggregate, "INC-1", "version:eval-1", None),
        NOW)
    publisher = RcaPublicationCoordinator(candidate_a, manager, incident)
    publisher.publish("PUB-EVAL-1", NOW + timedelta(seconds=1))
    runtime_path = tmp_path / "runtime.sqlite"
    root = rca_root_id("INC-1")
    work_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
    continuation = SqliteRcaContinuationStore(runtime_path)
    continuation.create(RcaContinuation(
        root, work_id, "INC-1", "EXECUTION", "EXECUTION", 4,
        NOW, NOW, NOW, aggregate_id=aggregate, attempt_id="attempt:eval-1"))
    with SqliteRuntimeWorkStore(runtime_path) as work:
        work.create(RuntimeWorkRecord(
            work_id, RuntimeWorkKind.RCA_INITIAL, "EVT-EVAL", "EXECUTION",
            "EXECUTION", 0, 4, RuntimeWorkStatus.OUTSTANDING,
            NOW, NOW, NOW, incident_id="INC-1", operation_id=root))
    flow = RcaFollowUpOrchestrator(
        incidents=incident, candidate_a=candidate_a, candidate_b=type(
            "CandidateBPublic", (), {
                "resolve_revision": lambda self, value: evidence.resolve_revision(value),
                "resolve_snapshot": lambda self, value: evidence.resolve_snapshot(value),
                "compare_materiality": lambda self, value: __import__(
                    "incident_evidence").compare_materiality(evidence, value),
            })(),
        candidate_c=knowledge, continuations=continuation,
        clock=_Time().clock())
    return (evidence, baseline, candidate, judgment, incident, knowledge_store,
            candidate_a, continuation, publisher, flow, knowledge_id, aggregate)


def test_material_test_manually_bypassing_runtime_cannot_qualify_formal_pass(
    tmp_path,
):
    fixture = load_evaluation_control_artifact(ARTIFACT_PATH).refresh(
        "material_refresh")
    world = _build_refresh_world(tmp_path, fixture)
    (evidence, baseline, candidate, judgment, incident, knowledge_store,
     candidate_a, continuation, publisher, flow, knowledge_id, aggregate) = world
    runtime = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite")
    try:
        assert judgment.judgement is MaterialityJudgement.MATERIAL
        oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
        evaluation_store = _store(tmp_path / "evaluation", oracle, plan)
        _runner(evaluation_store, _Clock()).run(
            admitted_plan=plan, campaign=campaign,
            manifest=_manifest(plan, campaign))
        case = next(item for item in campaign.recovery_cases
                    if item.fixture.variant == "material_refresh")
        clock = _Clock()
        resolver = RcaPublicStateResolver(
            candidate_a, incident, evidence, runtime, clock,
            clock_source=campaign.admission.clock_source)
        reference = lambda attempts, materiality=None: RecoveryStateReference(
            campaign.run_id, case.parent_execution_id, "INC-1", aggregate,
            attempts, materiality_result_id=materiality,
            runtime_work_id=runtime_work_id(
                RuntimeWorkKind.RCA_INITIAL, rca_root_id("INC-1")))
        before = resolver.resolve(reference(("attempt:eval-1",)))
        member = FollowUpRequirement(
            "MATERIAL_EVIDENCE", "revision:" + candidate.revision_id)
        assert flow.admit("INC-1", member).disposition is FollowUpDisposition.OUTSTANDING
        lineage = AttemptLineage(
            "attempt:eval-2", aggregate, candidate.snapshot_id,
            candidate.revision_id, knowledge_id,
            GenerationProvenance(
                "provider", "model", "prompt", "config", "profile"))
        class _CrashAfterStale:
            def __getattr__(self, name):
                return getattr(candidate_a, name)
            def apply_authorized_freshness(self, value):
                result = candidate_a.apply_authorized_freshness(value)
                raise ControlledEvaluationInterruption()
        interrupted_flow = RcaFollowUpOrchestrator(
            incidents=incident, candidate_a=_CrashAfterStale(),
            candidate_b=flow._b, candidate_c=flow._c,
            continuations=continuation, clock=_Time().clock())
        with pytest.raises(ControlledEvaluationInterruption):
            interrupted_flow.refresh(
                "INC-1", member, evidence_snapshot_id=candidate.snapshot_id,
                knowledge_snapshot_id=knowledge_id, lineage=lineage)
        clock.ms += 5
        middle = resolver.resolve(reference(
            ("attempt:eval-1",), judgment.materiality_result_id))
        admitted = flow.refresh(
            "INC-1", member, evidence_snapshot_id=candidate.snapshot_id,
            knowledge_snapshot_id=knowledge_id, lineage=lineage)
        assert admitted.disposition is FollowUpDisposition.ATTEMPT_ADMITTED
        stale = candidate_a.get_current(aggregate)
        assert stale.current.freshness is CurrentFreshness.STALE
        assert stale.version.version_id == "version:eval-1"
        candidate_a.record_try_outcome("OP-TRY-EVAL-2", LogicalTryOutcome(
            LogicalTryIdentity("attempt:eval-2", 1),
            LogicalTryResultKind.VALIDATED_RESULT,
            AdmittedRetryDisposition.NON_RETRYABLE, NOW + timedelta(seconds=2),
            validated_result_id="candidate-d:validated:refresh"))
        refreshed_artifact = artifact("Database pool exhaustion caused cascade")
        refreshed_artifact = replace(
            refreshed_artifact,
            provenance=replace(
                refreshed_artifact.provenance,
                evidence_snapshot_id=candidate.snapshot_id,
                evidence_revision_id=candidate.revision_id,
                knowledge_snapshot_id=knowledge_id,
            ),
        )
        candidate_a.commit_validated_artifact(
            "OP-COMMIT-EVAL-2", "attempt:eval-2", refreshed_artifact,
            PublicationTargetIdentity(
                "PUB-EVAL-2", aggregate, "INC-1", "version:eval-2",
                "version:eval-1"), NOW + timedelta(seconds=3))
        publisher.publish("PUB-EVAL-2", NOW + timedelta(seconds=4))
        current = candidate_a.get_current(aggregate)
        assert current.version.version_id == "version:eval-2"
        assert current.current.freshness is CurrentFreshness.FRESH
        assert {item.version_id for item in
                candidate_a.get_version_history(aggregate)} == {
                    "version:eval-1", "version:eval-2"}
        clock.ms += 5
        final = resolver.resolve(reference(
            ("attempt:eval-1", "attempt:eval-2")))
        evaluated = evaluate_refresh(
            before=before, after_materiality=middle, final=final)
        assert evaluated.passed
        observations = (
            before.observation_commitment, middle.observation_commitment,
            final.observation_commitment)
        payload = {
            "kind": "REFRESH_VERSION_RESULT", "case_id": case.case_id,
            "fixture_identity": case.fixture.fixture_identity,
            "variant_identity": case.fixture.variant,
            "disposition": evaluated.disposition,
            "passed": evaluated.passed, "details": list(evaluated.details),
            "before_state": "version:eval-1/CURRENT/FRESH",
            "trigger_basis": "MATERIAL_EVIDENCE",
            "resulting_state": "version:eval-2/CURRENT/FRESH",
            "freshness_transition": "FRESH_TO_STALE_TO_FRESH",
            "prior_current_preserved": True,
            "duplicate_effect_detected": False,
            "latency_disposition": "OBSERVED_COMPLETION", "latency_ms": 5,
            "public_observation_commitments": list(observations),
            "baseline_input_identity": case.fixture.baseline_input_identity,
            "baseline_input_commitment": case.fixture.baseline_input_commitment,
            "candidate_input_identity": case.fixture.candidate_input_identity,
            "candidate_input_commitment": case.fixture.candidate_input_commitment,
            "materiality_rule_version": case.fixture.materiality_rule_version,
        }
        with pytest.raises(TypeError, match="caller-created"):
            validated_recovery_result(
                evaluation=evaluated, kind="REFRESH_VERSION_RESULT",
                case_id=case.case_id,
                parent_execution_id=case.parent_execution_id,
                fixture_identity=case.fixture.fixture_identity,
                source_observation_commitments=observations, payload=payload)
        evaluation_store.close()
    finally:
        runtime.close()
        continuation.close(); candidate_a.close(); knowledge_store.close()
        incident.close(); evidence.close()


def test_material_fixture_runs_spec016_spec011_generation_and_publication(tmp_path):
    """Formal positive: Candidate-F injects input and observes Runtime only."""
    from copy import deepcopy
    import json
    from alert_correlation import (
        AlertCorrelationPolicyEngine, CorrelationEvaluationContext,
        CorrelationEvaluationSuccess, EvaluationPhase,
    )
    from alert_correlation.state import CorrelationMutationIntent
    from event_detection.store.event_store import EventStore
    from incident_management import IncidentMutationRequest
    from llm_generation.validation import evidence_fact_commitment, observed_fact_text
    from runtime_orchestration.rca_host import RcaRecoveryKind, RcaRecoverySubject
    from test_spec_016_initial_composition import _event

    fixture = load_evaluation_control_artifact(ARTIFACT_PATH).refresh(
        "material_refresh")
    env = _s2_publication_setup(tmp_path / "runtime", source_markers={
        EvidenceSource.LOKI: fixture.baseline_source["loki_marker"],
        EvidenceSource.PROMETHEUS: fixture.baseline_source["prometheus_value"],
    })
    evaluation_store = None
    try:
        assert env.actions.advance(RcaRecoverySubject(
            "INC-PUBLICATION", RcaRecoveryKind.RETRY_PENDING,
            attempt_id="attempt:publication"), env.time.value)
        first = env.d2.get(rca_root_id("INC-PUBLICATION"))
        assert env.actions.reconcile_publication(
            first.publication_operation_id, env.time.value).disposition.value == "COMPLETE"

        oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
        evaluation_store = _store(tmp_path / "evaluation", oracle, plan)
        _runner(evaluation_store, _Clock()).run(
            admitted_plan=plan, campaign=campaign,
            manifest=_manifest(plan, campaign))
        case = next(item for item in campaign.recovery_cases
                    if item.fixture.variant == "material_refresh")
        clock = _Clock()
        resolver = RcaPublicStateResolver(
            env.a, env.incidents, env.b, env.work, clock,
            clock_source=campaign.admission.clock_source)
        work_id = runtime_work_id(
            RuntimeWorkKind.RCA_INITIAL, rca_root_id("INC-PUBLICATION"))
        aggregate = first.aggregate_id
        def observe(materiality=None):
            return resolver.resolve(RecoveryStateReference(
                campaign.run_id, case.parent_execution_id,
                "INC-PUBLICATION", aggregate,
                materiality_result_id=materiality,
                runtime_work_id=work_id))
        before = observe()

        for source, marker in {
            EvidenceSource.LOKI: fixture.candidate_source["loki_marker"],
            EvidenceSource.PROMETHEUS: fixture.candidate_source["prometheus_value"],
        }.items():
            env.adapters[source].marker = marker
        event = {**_event(), "event_id": "EVT-PUBLICATION-NEW",
                 "detected_at": "2026-09-29T11:51:00Z"}
        EventStore(str(tmp_path / "runtime" / "events.jsonl")).write(event)
        decision = AlertCorrelationPolicyEngine().evaluate(
            event, env.incidents.list_correlation_views(),
            CorrelationEvaluationContext(EvaluationPhase.INITIAL))
        assert isinstance(decision, CorrelationEvaluationSuccess)
        IncidentManager(env.incidents).apply_correlation_mutation(
            IncidentMutationRequest(CorrelationMutationIntent.from_decision(
                operation_id="append-spec017-material-event",
                event_id=event["event_id"], decision=decision.decision,
                created_at=env.time.value), event, env.time.value))

        invoke = env.provider.invoke_once
        def grounded_response(request):
            response = invoke(request)
            output = deepcopy(response.structured_output)
            evidence = json.loads(json.loads(request.prompt)["evidence"])
            fact = evidence["semantic_evidence"]["normalized_evidence"][
                "PROMETHEUS"][0]
            new_text = observed_fact_text(fact)
            old_text = output["summary"]
            def replace_text(value):
                if isinstance(value, dict):
                    return {key: replace_text(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [replace_text(item) for item in value]
                return new_text if value == old_text else value
            output = replace_text(output)
            for reference in output["evidence_references"]:
                reference["evidence_snapshot_id"] = evidence["snapshot_id"]
                reference["canonical_path"] = (
                    "/semantic_evidence/normalized_evidence/PROMETHEUS/0")
                reference["canonical_fact_commitment"] = evidence_fact_commitment(fact)
            return replace(response, structured_output=output)
        env.provider.invoke_once = grounded_response

        middle_holder = []
        material_holder = []
        apply_freshness = env.a.apply_authorized_freshness
        def observe_stale(value):
            result = apply_freshness(value)
            material_now = tuple(item for item in
                env.b.enumerate_recovery_facts().materiality_results
                if item.judgement is MaterialityJudgement.MATERIAL)
            assert len(material_now) == 1
            material_holder.append(material_now[0])
            clock.ms += 5
            middle_holder.append(observe(
                material_now[0].materiality_result_id))
            return result
        env.a.apply_authorized_freshness = observe_stale
        assert env.actions.advance(RcaRecoverySubject(
            "INC-PUBLICATION", RcaRecoveryKind.FOLLOW_UP_PENDING),
            env.time.value)
        env.a.apply_authorized_freshness = apply_freshness
        material, = material_holder
        assert material.request.baseline_revision_id == (
            before.state.authoritative_version_history and
            env.a.get_attempt_lineage(first.attempt_id).attempt.lineage.evidence_revision_id)
        middle, = middle_holder
        assert middle.state.freshness is not before.state.freshness
        assert env.d.recovery().value.failures == (), tuple(
            (item.failure_class, item.safe_detail)
            for item in env.d.recovery().value.failures)
        state = env.d2.get(first.root_id)
        assert state.publication_operation_id != first.publication_operation_id
        assert env.actions.reconcile_publication(
            state.publication_operation_id,
            env.time.value).disposition.value == "COMPLETE"
        current = env.a.get_current(aggregate)
        assert current.version.version_id != before.state.current_version_id
        assert current.current.freshness is CurrentFreshness.FRESH
        clock.ms += 5
        final = observe()
        payload = {
            "variant_identity": case.fixture.variant,
            "before_state": f"{before.state.current_version_id}/CURRENT/FRESH",
            "trigger_basis": "MATERIAL_EVIDENCE",
            "resulting_state": f"{final.state.current_version_id}/CURRENT/FRESH",
            "freshness_transition": "FRESH_TO_STALE_TO_FRESH",
            "prior_current_preserved": True,
            "duplicate_effect_detected": False,
            "latency_disposition": "OBSERVED_COMPLETION", "latency_ms": 5,
            "baseline_input_identity": case.fixture.baseline_input_identity,
            "baseline_input_commitment": case.fixture.baseline_input_commitment,
            "candidate_input_identity": case.fixture.candidate_input_identity,
            "candidate_input_commitment": case.fixture.candidate_input_commitment,
            "materiality_rule_version": case.fixture.materiality_rule_version,
        }
        fact = evaluate_and_issue_refresh_result(
            evaluation_store, case_id=case.case_id,
            parent_execution_id=case.parent_execution_id,
            fixture_identity=case.fixture.fixture_identity,
            before=before, after_materiality=middle, final=final,
            payload=payload)
        deviation = issue_deviation_result(
            evaluation_store, source_evaluation_fact_id=fact.record_id,
            expected_public_observation_class="MATERIAL_STALE_REFRESH")
        assert json.loads(deviation.semantic_json)["outcome"] == "NO_DEVIATION"
        records = evaluation_store.reader().read_run_snapshot(
            campaign.run_id).records
        kinds = [json.loads(item.semantic_json).get("kind")
                 for item in records if item.domain == "observation"]
        assert kinds.count("RECOVERY_SOURCE_OBSERVATION") == 3
        assert kinds.count("RECOVERY_EVALUATOR_EXECUTION") == 1
        assert len(env.a.get_version_history(aggregate)) == 2
        assert len(env.provider.calls) == 2 and len(env.mutations) == 2
    finally:
        if evaluation_store is not None:
            evaluation_store.close()
        _close_s2_publication(env)


def test_material_failure_runs_governed_candidate_d_typed_failure(tmp_path):
    import json

    from alert_correlation import (
        AlertCorrelationPolicyEngine, CorrelationEvaluationContext,
        CorrelationEvaluationSuccess, EvaluationPhase,
    )
    from alert_correlation.state import CorrelationMutationIntent
    from event_detection.store.event_store import EventStore
    from incident_management import IncidentMutationRequest
    from llm_generation.service import ProviderFailureKind, ProviderInvocationError
    from test_spec_016_initial_composition import _event

    fixture = load_evaluation_control_artifact(ARTIFACT_PATH).refresh(
        "refresh_failure")
    env = _s2_publication_setup(tmp_path / "runtime", source_markers={
        EvidenceSource.LOKI: fixture.baseline_source["loki_marker"],
        EvidenceSource.PROMETHEUS: fixture.baseline_source["prometheus_value"],
    })
    evaluation_store = None
    try:
        assert env.actions.advance(RcaRecoverySubject(
            "INC-PUBLICATION", RcaRecoveryKind.RETRY_PENDING,
            attempt_id="attempt:publication"), env.time.value)
        initial = env.d2.get(rca_root_id("INC-PUBLICATION"))
        env.actions.reconcile_publication(
            initial.publication_operation_id, env.time.value)
        baseline = env.a.get_current(initial.aggregate_id)
        baseline_version_ids = tuple(item.version_id for item in
                                     env.a.get_version_history(initial.aggregate_id))
        artifact = load_evaluation_control_artifact(ARTIFACT_PATH)
        assert artifact.pm_approval_reference == "PM-SPEC017-R2S4-20261013"
        oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
        evaluation_store = _store(tmp_path / "evaluation", oracle, plan)
        _runner(evaluation_store, _Clock()).run(
            admitted_plan=plan, campaign=campaign,
            manifest=_manifest(plan, campaign))
        case = next(item for item in campaign.recovery_cases
                    if item.fixture.variant == "refresh_failure")
        assert case.case_id in campaign.required_refresh_case_ids
        assert case.fixture.fixture_identity == fixture.fixture_identity
        clock = _Clock()
        resolver = RcaPublicStateResolver(
            env.a, env.incidents, env.b, env.work, clock,
            clock_source=campaign.admission.clock_source)
        work_id = runtime_work_id(
            RuntimeWorkKind.RCA_INITIAL, rca_root_id("INC-PUBLICATION"))
        def observe(materiality=None, attempts=()):
            return resolver.resolve(RecoveryStateReference(
                campaign.run_id, case.parent_execution_id,
                "INC-PUBLICATION", initial.aggregate_id, attempts,
                materiality_result_id=materiality,
                runtime_work_id=work_id))
        before = observe()
        for source, marker in {
            EvidenceSource.LOKI: fixture.candidate_source["loki_marker"],
            EvidenceSource.PROMETHEUS: fixture.candidate_source["prometheus_value"],
        }.items():
            env.adapters[source].marker = marker
        event = {**_event(), "event_id": "EVT-SPEC017-FAILURE",
                 "detected_at": "2026-09-29T11:51:00Z"}
        EventStore(str(tmp_path / "runtime" / "events.jsonl")).write(event)
        decision = AlertCorrelationPolicyEngine().evaluate(
            event, env.incidents.list_correlation_views(),
            CorrelationEvaluationContext(EvaluationPhase.INITIAL))
        assert isinstance(decision, CorrelationEvaluationSuccess)
        IncidentManager(env.incidents).apply_correlation_mutation(
            IncidentMutationRequest(CorrelationMutationIntent.from_decision(
                operation_id="append-spec017-failure-event",
                event_id=event["event_id"], decision=decision.decision,
                created_at=env.time.value), event, env.time.value))
        env.provider.outcome = ProviderInvocationError(
            ProviderFailureKind.MALFORMED, request_sent=True)
        middle_holder = []
        material_holder = []
        apply_freshness = env.a.apply_authorized_freshness
        def observe_stale(value):
            result = apply_freshness(value)
            material_now = tuple(item for item in
                env.b.enumerate_recovery_facts().materiality_results
                if item.judgement is MaterialityJudgement.MATERIAL)
            assert len(material_now) == 1
            material_holder.append(material_now[0])
            clock.ms += 5
            middle_holder.append(observe(
                materiality=material_now[0].materiality_result_id))
            return result
        env.a.apply_authorized_freshness = observe_stale
        assert env.actions.advance(RcaRecoverySubject(
            "INC-PUBLICATION", RcaRecoveryKind.FOLLOW_UP_PENDING),
            env.time.value)
        env.a.apply_authorized_freshness = apply_freshness
        material, = material_holder
        middle, = middle_holder
        failures = env.d.recovery().value.failures
        current = env.a.get_current(initial.aggregate_id)
        assert failures
        assert failures[-1].failure_class.value == "MALFORMED_PROVIDER_OUTPUT"
        assert current.version.version_id == baseline.version.version_id
        assert current.current.freshness is CurrentFreshness.STALE
        assert len(env.a.get_version_history(initial.aggregate_id)) == 1
        failure = failures[-1]
        clock.ms += 5
        final = observe()
        domain_source = record_refresh_failure_observation(
            evaluation_store, case_id=case.case_id,
            parent_execution_id=case.parent_execution_id,
            evidence=env.b, candidate_d=env.d, candidate_a=env.a,
            runtime=env.work,
            materiality_result_id=material.materiality_result_id,
            aggregate_id=initial.aggregate_id,
            prior_current_version_id=baseline.version.version_id,
            expected_version_ids=baseline_version_ids,
            runtime_work_id=work_id)
        fact = evaluate_and_issue_refresh_failure_result(
            evaluation_store, case_id=case.case_id,
            parent_execution_id=case.parent_execution_id,
            fixture_identity=case.fixture.fixture_identity,
            failure_observation_record_id=domain_source.record_id,
            before=before, after_materiality=middle, final=final,
            payload={
                "variant_identity": case.fixture.variant,
                "before_state": f"{baseline.version.version_id}/CURRENT/FRESH",
                "trigger_basis": "MATERIAL_EVIDENCE",
                "resulting_state": f"{current.version.version_id}/CURRENT/STALE",
                "freshness_transition": "FRESH_TO_STALE_TO_STALE",
                "duplicate_effect_detected": False,
                "latency_disposition": "FAILURE", "latency_ms": 5,
                "baseline_input_identity": case.fixture.baseline_input_identity,
                "baseline_input_commitment": case.fixture.baseline_input_commitment,
                "candidate_input_identity": case.fixture.candidate_input_identity,
                "candidate_input_commitment": case.fixture.candidate_input_commitment,
                "materiality_rule_version": case.fixture.materiality_rule_version,
            })
        deviation = issue_deviation_result(
            evaluation_store, source_evaluation_fact_id=fact.record_id,
            expected_public_observation_class=
                "MATERIAL_REFRESH_FAILURE_RETAINS_CURRENT")
        issued = json.loads(evaluation_store.get(fact.record_id).semantic_json)
        assert issued["kind"] == "REFRESH_VERSION_RESULT"
        assert issued["disposition"] == "REFRESH_FAILED_CURRENT_RETAINED_STALE"
        assert issued["candidate_d_failure_disposition"] == (
            "MALFORMED_PROVIDER_OUTPUT")
        assert issued["refresh_attempted"] is True
        assert issued["new_version"] is False
        assert issued["new_current"] is False
        assert issued["prior_current_preserved"] is True
        assert issued["prior_current_freshness"] == "STALE"
        assert issued["publication_effect"] == "NONE"
        assert issued["public_observation_commitments"][-1] == (
            json.loads(domain_source.semantic_json)[
                "source_observation_commitment"])
        provenance = json.loads(evaluation_store.get(
            issued["evaluator_execution_record_id"]).semantic_json)
        assert provenance["source_observation_record_ids"][-1] == (
            domain_source.record_id)
        source_projection = json.loads(domain_source.semantic_json)[
            "observation_projection"]
        assert source_projection["materiality"]["materiality_result_id"] == (
            material.materiality_result_id)
        assert source_projection["candidate_d_failure"]["attempt_id"] == (
            failure.try_identity.attempt_id)
        assert source_projection["candidate_d_failure"]["try_ordinal"] == (
            failure.try_identity.try_ordinal)
        assert source_projection["current_version"]["version_ids"] == list(
            baseline_version_ids)
        assert json.loads(deviation.semantic_json)["outcome"] == "NO_DEVIATION"
    finally:
        if evaluation_store is not None:
            evaluation_store.close()
        _close_s2_publication(env)


def test_non_material_fixture_suppresses_attempt_try_version_and_publication(
    tmp_path,
):
    fixture = load_evaluation_control_artifact(ARTIFACT_PATH).refresh(
        "non_material")
    world = _build_refresh_world(tmp_path, fixture, omission=True)
    (evidence, baseline, candidate, judgment, incident, knowledge_store,
     candidate_a, continuation, _, flow, knowledge_id, aggregate) = world
    try:
        assert judgment.judgement is MaterialityJudgement.NON_MATERIAL
        member = FollowUpRequirement(
            "MATERIAL_EVIDENCE", "revision:" + candidate.revision_id)
        flow.admit("INC-1", member)
        result = flow.refresh(
            "INC-1", member, evidence_snapshot_id=candidate.snapshot_id,
            knowledge_snapshot_id=knowledge_id,
            lineage=AttemptLineage(
                "attempt:non-material", aggregate, candidate.snapshot_id,
                candidate.revision_id, knowledge_id,
                GenerationProvenance(
                    "provider", "model", "prompt", "config", "profile")))
        assert result.disposition is FollowUpDisposition.SUPPRESSED
        assert candidate_a.get_attempt_lineage("attempt:non-material") is None
        assert tuple(item.version_id for item in
                     candidate_a.get_version_history(aggregate)) == (
                         "version:eval-1",)
        current = candidate_a.get_current(aggregate)
        assert current.version.version_id == "version:eval-1"
        assert current.current.freshness is CurrentFreshness.FRESH
        assert incident.get_rca_publication_result("PUB-EVAL-2") is None
    finally:
        continuation.close(); candidate_a.close(); knowledge_store.close()
        incident.close(); evidence.close()


@pytest.mark.parametrize("boundary", (
    "EVIDENCE", "KNOWLEDGE", "GENERATION", "ARTIFACT_COMMIT",
    "PUBLICATION",
))
def test_each_restart_boundary_has_a_distinct_governed_checkpoint(boundary):
    artifact = load_evaluation_control_artifact(ARTIFACT_PATH)
    control = artifact.restart(boundary)
    armed = arm_failpoint(
        artifact, boundary=boundary,
        control_commitment=control.control_commitment,
        operation_identity=f"operation:{boundary}",
        work_identity=f"work:{boundary}",
    )
    with pytest.raises(ValueError, match="without an actual checkpoint"):
        armed.interrupt_at(
            hook_identity="candidate-f:wrong-checkpoint:v2",
            durable_facts_commitment=commitment({"durable": boundary}),
        )
    with pytest.raises(ValueError, match="without an actual checkpoint"):
        armed.interrupt_at(
            hook_identity=control.exact_control_hook_identity,
            durable_facts_commitment=commitment({"durable": boundary}),
        )


def _checkpoint(boundary, operation, work):
    artifact = load_evaluation_control_artifact(ARTIFACT_PATH)
    control = artifact.restart(boundary)
    return GovernedCheckpointAdapter(arm_failpoint(
        artifact, boundary=boundary,
        control_commitment=control.control_commitment,
        operation_identity=operation, work_identity=work))


def _issue_actual_restart(tmp_path, boundary, checkpoint, recovery):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
    store = _store(tmp_path, oracle, plan)
    _runner(store, _Clock()).run(
        admitted_plan=plan, campaign=campaign,
        manifest=_manifest(plan, campaign))
    case = next(item for item in campaign.recovery_cases
                if item.fixture.recovery_boundary == boundary)
    fact = evaluate_and_issue_checkpoint_recovery_result(
        store, case_id=case.case_id,
        parent_execution_id=case.parent_execution_id,
        fixture_identity=case.fixture.fixture_identity,
        checkpoint=checkpoint, recovery=recovery,
        payload={
            "control_commitment": case.fixture.control_commitment,
            "exact_control_hook_identity": case.fixture.control_reference,
            "restart_reopen_method": "close/reopen durable authorities",
            "before_state": "DURABLE_PRE_CRASH",
            "resulting_state": "AUTHORITATIVE_RECOVERED",
            "recovered_identity": recovery.operation_identity,
            "publication_continuity": True,
            "latency_disposition": "OBSERVED_COMPLETION",
        })
    issue_deviation_result(
        store, source_evaluation_fact_id=fact.record_id,
        expected_public_observation_class="SAME_OPERATION_RECOVERY")
    store.close()


def test_evidence_actual_checkpoint_crash_reopen_and_replay(tmp_path):
    command = capture_command("SPEC017-EVIDENCE-RESTART")
    service, _, _, adapters = capture_service(tmp_path / "first")
    adapter = _checkpoint("EVIDENCE", command.capture_operation_id,
                          "work:evidence")
    with pytest.raises(ControlledEvaluationInterruption) as crashed:
        adapter.capture_evidence(service, command)
    receipt = crashed.value.receipt
    assert receipt is not None and receipt.boundary_identity == "EVIDENCE"
    expected = dict(receipt.durable_facts)
    service._store.close()
    reopened_store = SqliteEvidenceStore(tmp_path / "first" / "evidence.sqlite")
    no_second = {source: FakeAdapter(
        SourceStatus.AVAILABLE, marker="must-not-run") for source in EvidenceSource}
    reopened, *_ = capture_service(
        tmp_path / "reopened", store=reopened_store, adapters=no_second)
    try:
        replay = reopened.capture_evidence(command)
        assert replay.snapshot_id == expected["snapshot_id"]
        assert replay.revision_id == expected["revision_id"]
        assert all(item.calls == 0 for item in no_second.values())
        assert all(item.calls == 1 for item in adapters.values())
        recovery = adapter.verify_evidence_recovery(
            receipt, reopened, command)
        _issue_actual_restart(
            tmp_path / "evaluation", "EVIDENCE", receipt, recovery)
    finally:
        reopened_store.close()


def test_knowledge_actual_checkpoint_crash_reopen_and_replay(tmp_path):
    from knowledge_index import (
        KnowledgeRetrievalService, KnowledgeSnapshotService, SqliteKnowledgeStore,
    )
    from _knowledge_retrieval_testkit import QueryProvider, RetrievalIndex, limits, request
    (tmp_path / "knowledge").mkdir()
    store, service, snapshot = _execution_knowledge(tmp_path / "knowledge")
    request_value = request()
    adapter = _checkpoint("KNOWLEDGE", request_value.operation_key.value,
                          "work:knowledge")
    with pytest.raises(ControlledEvaluationInterruption) as crashed:
        adapter.resolve_knowledge(service, request_value, limits())
    receipt = crashed.value.receipt
    assert receipt is not None
    store.close()
    reopened_store = SqliteKnowledgeStore(tmp_path / "knowledge" / "c.sqlite3")
    reopened = KnowledgeSnapshotService(reopened_store, KnowledgeRetrievalService(
        reopened_store,
        QueryProvider(error=AssertionError("duplicate Knowledge provider call")),
        RetrievalIndex({}, query_error=AssertionError("duplicate Knowledge query"))))
    try:
        replay = reopened.resolve(request_value, limits())
        assert replay.snapshot.snapshot_key == snapshot.snapshot_key
        assert reopened.read_provenance(snapshot.snapshot_key).value is not None
        recovery = adapter.verify_knowledge_recovery(
            receipt, reopened, request_value, limits())
        _issue_actual_restart(
            tmp_path / "evaluation", "KNOWLEDGE", receipt, recovery)
    finally:
        reopened_store.close()


def test_generation_actual_checkpoint_crash_reopen_and_resume(tmp_path):
    env = _execution_setup(tmp_path / "generation")
    operation = env.request.source.operation_id
    adapter = _checkpoint("GENERATION", operation,
                          str(env.request.source.try_identity))
    execute = env.service.execute
    env.service.execute = lambda *args, **kwargs: adapter.execute_generation(
        type("Delegate", (), {"execute": staticmethod(execute),
                              "store": env.d})(), *args, **kwargs)
    try:
        with pytest.raises(ControlledEvaluationInterruption) as crashed:
            env.executor.step(env.request)
        checkpoint = crashed.value.receipt
        assert checkpoint.boundary_identity == "GENERATION"
        assert len(env.a.get_attempt_lineage(
            env.request.attempt_id).try_outcomes) == 0
    finally:
        _close_execution(env)
    fresh = _execution_instances(tmp_path / "generation")
    try:
        result = fresh.executor.step(fresh.request)
        assert result.disposition.value == "ARTIFACT_COMMITTED"
        assert len(fresh.a.get_attempt_lineage(
            fresh.request.attempt_id).try_outcomes) == 1
        assert len(fresh.a.get_version_history("aggregate:execution")) == 1
        assert fresh.provider.calls == 0
        recovery = adapter.verify_generation_recovery(
            checkpoint, fresh.a, fresh.d, fresh.request.attempt_id)
        _issue_actual_restart(
            tmp_path / "evaluation", "GENERATION", checkpoint, recovery)
    finally:
        _close_execution(fresh)


def test_artifact_actual_checkpoint_crash_reopen_and_idempotent_commit(tmp_path):
    env = _execution_setup(tmp_path / "artifact")
    adapter = _checkpoint("ARTIFACT_COMMIT", env.request.source.operation_id,
                          str(env.request.source.try_identity))
    commit = env.a.commit_validated_artifact
    env.a.commit_validated_artifact = lambda *args, **kwargs: (
        adapter.commit_artifact(
            type("CandidateA", (), {
                "commit_validated_artifact": staticmethod(commit),
                "get_version_history": staticmethod(env.a.get_version_history),
                "get_attempt_lineage": staticmethod(env.a.get_attempt_lineage),
            })(), *args, **kwargs))
    try:
        with pytest.raises(ControlledEvaluationInterruption) as crashed:
            env.executor.step(env.request)
        checkpoint = crashed.value.receipt
        assert len(env.a.get_version_history("aggregate:execution")) == 1
    finally:
        _close_execution(env)
    fresh = _execution_instances(tmp_path / "artifact")
    try:
        fresh.executor.step(fresh.request)
        fresh.executor.step(fresh.request)
        assert len(fresh.a.get_version_history("aggregate:execution")) == 1
        assert fresh.provider.calls == 0
        recovery = adapter.verify_artifact_recovery(
            checkpoint, fresh.a, "aggregate:execution")
        _issue_actual_restart(
            tmp_path / "evaluation", "ARTIFACT_COMMIT", checkpoint, recovery)
    finally:
        _close_execution(fresh)


def test_publication_actual_checkpoint_crash_reopen_and_reconcile(tmp_path):
    env = _s2_publication_setup(tmp_path / "publication")
    try:
        assert env.actions.advance(RcaRecoverySubject(
            "INC-PUBLICATION", RcaRecoveryKind.RETRY_PENDING,
            attempt_id="attempt:publication"), env.time.value)
        state = env.d2.get(rca_root_id("INC-PUBLICATION"))
        operation = state.publication_operation_id
        adapter = _checkpoint("PUBLICATION", operation, state.runtime_work_id)
        publish = env.coordinator._incident_mutations.publish_rca_current
        env.coordinator._incident_mutations.publish_rca_current = (
            lambda command: adapter.publish_spec008(
                publish, env.incidents, command))
        with pytest.raises(ControlledEvaluationInterruption) as crashed:
            env.actions.reconcile_publication(operation, env.time.value)
        checkpoint = crashed.value.receipt
        assert checkpoint.durable_facts[
            "publication_operation_id"] == operation
        assert env.incidents.get_rca_publication_result(operation) is not None
        assert env.a.get_publication_result(operation).disposition.value == (
            "A_SIDE_COMMITTED")
    finally:
        _close_s2_publication(env)
    fresh = _s2_publication_instances(tmp_path / "publication")
    try:
        result = fresh.actions.reconcile_publication(operation, fresh.time.value)
        assert result.disposition.value == "COMPLETE"
        fresh.actions.reconcile_publication(operation, fresh.time.value)
        target = fresh.a.get_publication_result(operation).target
        assert fresh.a.get_current(target.aggregate_id).version.version_id == (
            target.target_version_id)
        recovery = adapter.verify_publication_recovery(
            checkpoint, fresh.a, fresh.incidents)
        _issue_actual_restart(
            tmp_path / "evaluation", "PUBLICATION", checkpoint, recovery)
    finally:
        _close_s2_publication(fresh)


def test_restart_control_rejects_wrong_commitment_and_clean_reset_is_not_recovery():
    artifact = load_evaluation_control_artifact(ARTIFACT_PATH)
    with pytest.raises(ValueError, match="wrong boundary control commitment"):
        arm_failpoint(
            artifact, boundary="EVIDENCE",
            control_commitment="sha256:" + "0" * 64,
            operation_identity="operation:evidence",
            work_identity="work:evidence",
        )
    control = artifact.restart("EVIDENCE")
    request = RecoveryControlRequest(
        "case", "execution", "input", "sha256:" + "1" * 64,
        "EVIDENCE", control.exact_control_hook_identity,
        control.control_commitment)
    with pytest.raises(ValueError, match="actual checkpoint proof"):
        RecoveryInterruptionReceipt(
            "case", "EVIDENCE", control.exact_control_hook_identity,
            control.control_commitment, "operation", "work", 1).validate(
                request)
    with pytest.raises(ValueError, match="clean reset"):
        RecoveryInterruptionReceipt(
            "case", "EVIDENCE", control.exact_control_hook_identity,
            control.control_commitment, "operation", "work", 1,
            clean_reset=True).validate(request)


@pytest.mark.parametrize("kind", (
    "DEVIATION_RESULT", "REFRESH_VERSION_RESULT", "RESTART_RECOVERY_RESULT",
))
def test_generic_append_cannot_mint_protected_recovery_truth(tmp_path, kind):
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
    with _store(tmp_path, oracle, plan) as store:
        _runner(store, _Clock()).run(
            admitted_plan=plan, campaign=campaign,
            manifest=_manifest(plan, campaign))
        execution = campaign.executions[0]
        with pytest.raises(ValueError, match="protected"):
            store.append(LedgerRecord.create(
                "observation", [execution.execution_id, "caller-minted", kind],
                {"kind": kind}, execution.execution_id))


def test_caller_cannot_construct_protected_typed_recovery_or_deviation_result():
    with pytest.raises(TypeError):
        ValidatedRecoveryResult(
            "REFRESH_VERSION_RESULT", "caller", "caller", "caller",
            "caller", (), {})
    with pytest.raises(TypeError):
        ValidatedDeviationResult(
            "caller", "caller", "caller", "sha256:" + "0" * 64,
            "NO_DEVIATION", "caller", "caller")


def test_caller_created_evaluator_outputs_cannot_issue():
    from rca_evaluation.recovery_evaluation import RefreshEvaluation, RestartEvaluation
    for evaluation in (
        RefreshEvaluation(True, "REFRESHED_CURRENT_FRESH"),
        RestartEvaluation(True, "CONVERGED"),
    ):
        with pytest.raises(TypeError, match="caller-created"):
            validated_recovery_result(evaluation=evaluation)


def test_protected_issuer_rejects_missing_wrong_and_cross_bound_sources(tmp_path):
    import rca_evaluation.recovery_records as protected
    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
    with _store(tmp_path, oracle, plan) as store:
        _runner(store, _Clock()).run(
            admitted_plan=plan, campaign=campaign,
            manifest=_manifest(plan, campaign))
        first, second = campaign.executions[:2]
        projection = {"source": "negative-only"}
        def source_record(parent, case, observed_commitment=None,
                          evaluation_run_id=None):
            payload = {
                "kind": "RECOVERY_SOURCE_OBSERVATION", "case_id": case,
                "evaluation_run_id": evaluation_run_id or campaign.run_id,
                "execution_id": parent, "source_ordinal": 1,
                "source_observation_commitment": (
                    observed_commitment or commitment(projection)),
                "observation_projection": projection,
            }
            return LedgerRecord.create(
                "observation", [parent, "negative-source", case,
                                payload["source_observation_commitment"]],
                payload, parent)
        with pytest.raises(ValueError, match="source observation binding"):
            store._append_protected_recovery(
                source_record(first.execution_id, "case:wrong-commitment",
                              "sha256:" + "0" * 64),
                authority=protected._RECOVERY_ISSUANCE_AUTHORITY)
        with pytest.raises(ValueError, match="source observation binding"):
            store._append_protected_recovery(
                source_record(first.execution_id, "case:cross-run",
                              evaluation_run_id="eval:evaluation_run:" + "0" * 64),
                authority=protected._RECOVERY_ISSUANCE_AUTHORITY)
        source = store._append_protected_recovery(
            source_record(first.execution_id, "case:one"),
            authority=protected._RECOVERY_ISSUANCE_AUTHORITY)
        result_payload = {"kind": "RESTART_RECOVERY_RESULT",
                          "case_id": "case:two",
                          "fixture_identity": "fixture",
                          "public_observation_commitments": []}
        evaluator_payload = {
            "kind": "RECOVERY_EVALUATOR_EXECUTION",
            "evaluator_identity": "SPEC-017-R2S4-GOVERNED-EVALUATOR-v3",
            "evaluator_version": "3", "evaluation_operation_identity": "op",
            "case_id": "case:two", "execution_id": second.execution_id,
            "fixture_identity": "fixture",
            "result_kind": "RESTART_RECOVERY_RESULT",
            "source_observation_record_ids": [source.record_id, "missing"],
            "source_observation_record_commitments": [
                source.semantic_commitment, "sha256:" + "0" * 64],
            "result_payload": result_payload,
            "result_commitment": commitment(result_payload),
        }
        evaluator = LedgerRecord.create(
            "observation", [second.execution_id, "negative-evaluator"],
            evaluator_payload, second.execution_id)
        with pytest.raises(ValueError, match="source is missing"):
            store._append_protected_recovery(
                evaluator, authority=protected._RECOVERY_ISSUANCE_AUTHORITY)
        with pytest.raises(ValueError, match="does not exist"):
            protected._issue_from_execution(store, "observation:" + "0" * 64)


def test_refresh_failure_issuer_rejects_missing_corrupt_and_cross_execution_sources(
    tmp_path,
):
    """Raw protected insertion is confined to fail-closed corruption probes."""
    import rca_evaluation.recovery_records as protected

    oracle = _oracle(); plan = _plan(oracle); campaign = _campaign(plan)
    with _store(tmp_path, oracle, plan) as store:
        _runner(store, _Clock()).run(
            admitted_plan=plan, campaign=campaign,
            manifest=_manifest(plan, campaign))
        case = next(item for item in campaign.recovery_cases
                    if item.fixture.variant == "refresh_failure")
        other = next(item for item in campaign.executions
                     if item.execution_id != case.parent_execution_id)
        materiality = {
            "materiality_result_id": "materiality:failure-negative",
            "baseline_revision_id": "revision:baseline",
            "candidate_revision_id": "revision:candidate",
            "rule_version": "materiality-rule:v1",
            "judgement": "MATERIAL", "reason_facts": ["negative-probe"],
        }
        failure = {
            "attempt_id": "attempt:failure", "try_ordinal": 1,
            "operation_id": "operation:failure",
            "disposition": "MALFORMED_PROVIDER_OUTPUT",
            "retry_safety": "RETRYABLE",
            "subject_input_commitment": "sha256:" + "1" * 64,
            "candidate_a_try_outcome_count": 0,
        }
        current = {
            "aggregate_id": "aggregate:failure",
            "prior_current_version_id": "version:prior",
            "current_version_id": "version:prior", "freshness": "STALE",
            "version_ids": ["version:prior"],
            "publication_operation_ids": ["publication:prior"],
        }
        base = {
            "observation_kind": "REFRESH_FAILURE_DOMAIN_OBSERVATION",
            "materiality": materiality,
            "materiality_commitment": commitment(materiality),
            "runtime_work_id": "work:failure",
            "runtime_operation_id": "operation:runtime",
            "refresh_attempted": True,
            "candidate_d_failure": failure,
            "candidate_d_failure_commitment": commitment(failure),
            "current_version": current,
            "current_version_commitment": commitment(current),
            "new_version": False, "new_current": False,
            "prior_current_retained": True, "publication_effect": "NONE",
        }

        def insert_corrupt(projection, *, parent=None, label):
            parent = parent or case.parent_execution_id
            payload = {
                "kind": "RECOVERY_SOURCE_OBSERVATION",
                "case_id": case.case_id,
                "evaluation_run_id": campaign.run_id,
                "execution_id": parent, "source_ordinal": 4,
                "source_observation_commitment": commitment(projection),
                "observation_projection": projection,
            }
            record = LedgerRecord.create(
                "observation", [parent, "negative-refresh-failure", label],
                payload, parent)
            return store._append_protected_recovery(
                record, authority=protected._RECOVERY_ISSUANCE_AUTHORITY)

        def attempt(source_id):
            return evaluate_and_issue_refresh_failure_result(
                store, case_id=case.case_id,
                parent_execution_id=case.parent_execution_id,
                fixture_identity=case.fixture.fixture_identity,
                failure_observation_record_id=source_id,
                before=None, after_materiality=None, final=None, payload={})

        with pytest.raises(ValueError, match="source observation is missing"):
            attempt("eval:observation:" + "0" * 64)

        wrong_failure = dict(base)
        wrong_failure["candidate_d_failure_commitment"] = "sha256:" + "0" * 64
        bad_failure = insert_corrupt(
            wrong_failure, label="wrong-candidate-d-commitment")
        with pytest.raises(ValueError, match="domain observation is contradictory"):
            attempt(bad_failure.record_id)

        wrong_current_value = dict(current, current_version_id="version:forged")
        wrong_current = dict(
            base, current_version=wrong_current_value,
            current_version_commitment=commitment(wrong_current_value))
        bad_current = insert_corrupt(
            wrong_current, label="wrong-current-observation")
        with pytest.raises(ValueError, match="domain observation is contradictory"):
            attempt(bad_current.record_id)

        cross = insert_corrupt(
            base, parent=other.execution_id, label="cross-execution")
        with pytest.raises(ValueError, match="source observation is missing"):
            attempt(cross.record_id)
