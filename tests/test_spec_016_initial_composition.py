"""SPEC-016 S2 real public-service composition and durable convergence evidence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import inspect
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from alert_correlation import AlertCorrelationPolicyEngine, CorrelationEvaluationContext, CorrelationEvaluationSuccess, EvaluationPhase
from alert_correlation.state import CorrelationMutationIntent
from event_detection.store.event_store import EventStore
from incident_evidence import (CaptureCommand, EvidenceCaptureService, EvidenceSource,
    MaterialityEvaluationKind, MaterialityRequest, SqliteEvidenceStore,
    SourceStatus, capture_command_semantic_identity, effective_capture_config_identity, load_evidence_policy)
from incident_management import IncidentManager, IncidentMutationRequest, SqliteIncidentStore
from knowledge_index import (KnowledgeReadResult, KnowledgeReadStatus, KnowledgeRetrievalService,
    KnowledgeSnapshotService, RetrievalOperationState, RetrievalOperationKey, SqliteKnowledgeStore)
from rca_persistence import (AdmitAttemptRequest, AttemptLineage, CreateAggregateRequest,
    GenerationLifecycle, GenerationProvenance, RcaDomainError, SqliteRcaStore)
from runtime_orchestration.rca_continuation import (CaptureCommandBasis,
    RcaContinuationConcurrencyError, SqliteRcaContinuationStore, rca_child_operation_id, rca_root_id)
from runtime_orchestration.rca_initial import (InitialRcaDisposition, InitialRcaOrchestrator,
    InitialRcaRequest, derive_attempt_admission_operation_id)
from runtime_orchestration.contracts import RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus
from runtime_orchestration.identity import runtime_work_id
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore

from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import QueryProvider, RetrievalIndex, candidates_for, request, stage_activate
from test_incident_evidence_capture import FakeAdapter


UTC = timezone.utc
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _event():
    return {"event_id": "EVT-S2", "detected_at": "2026-09-29T11:50:00Z",
            "event_source": "log_event_detection", "event_type": "brute_force_detected",
            "detection_method": "isolation_forest", "severity": "CRITICAL", "confidence": 0.99,
            "service_name": "auth-api", "trace_id": None, "source_ip": "203.0.113.10",
            "downstream_service": None, "external_service": None, "status": "OPEN",
            "triggered_features": {}, "raw_log_sample": []}


def _public_services(tmp_path, *, knowledge_store_type=SqliteKnowledgeStore, retrieval_context=None,
                     event_id="EVT-S2", source_marker="s2", incident_id="INC-S2"):
    """Build actual A/B/C stores and public services; only source/provider ports are deterministic test adapters."""
    event = _event()
    event["event_id"] = event_id
    evaluated = AlertCorrelationPolicyEngine().evaluate(event, (), CorrelationEvaluationContext(EvaluationPhase.INITIAL))
    assert isinstance(evaluated, CorrelationEvaluationSuccess)
    events = EventStore(str(tmp_path / "events.jsonl")); events.write(event)
    incidents = SqliteIncidentStore(str(tmp_path / "incidents.sqlite3"))
    IncidentManager(incidents, incident_id_factory=lambda: incident_id).apply_correlation_mutation(
        IncidentMutationRequest(CorrelationMutationIntent.from_decision(
            operation_id="OP-INC-S2", event_id=event["event_id"], decision=evaluated.decision,
            created_at=NOW), event, NOW))
    policy = load_evidence_policy("configs/incident_evidence.yaml")
    adapters = {source: FakeAdapter(SourceStatus.AVAILABLE, marker=source_marker) for source in EvidenceSource}
    evidence_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
    evidence = EvidenceCaptureService(store=evidence_store, incident_reader=incidents, event_reader=events,
                                      policy=policy, adapters=adapters)
    rca = SqliteRcaStore(tmp_path / "rca.sqlite3")
    knowledge_store = knowledge_store_type(tmp_path / "knowledge.sqlite3")
    staged, index = stage_activate(knowledge_store, tmp_path / "knowledge-sources")
    if retrieval_context is not None:
        retrieval_context.append((staged, index))
    provider = QueryProvider()
    knowledge = KnowledgeSnapshotService(knowledge_store, KnowledgeRetrievalService(
        knowledge_store, provider, RetrievalIndex(index.artifacts, candidates_for(staged, 0.1))))
    return incidents, events, evidence_store, evidence, adapters, rca, knowledge_store, knowledge, provider


def _request(evidence, *, attempt_id="attempt:S2"):
    root = rca_root_id("INC-S2")
    policy = load_evidence_policy("configs/incident_evidence.yaml")
    config = effective_capture_config_identity(policy, __import__("incident_evidence").SourceAdmissionPolicy(),
                                                __import__("incident_evidence").DEFAULT_SOURCE_REQUEST_POLICIES)
    basis = CaptureCommandBasis("INC-S2", root, "INITIAL", NOW, policy.capture_contract_version,
        policy.canonicalization_version, policy.source_policy_version, config,
        ("collection:logs", "collection:metrics"), policy.bounds_policy_version)
    command = CaptureCommand(basis.capture_operation_id, "INC-S2", NOW, policy.capture_contract_version,
        policy.canonicalization_version, policy.source_policy_version, policy.bounds_policy_version, config)
    aggregate_id = rca_child_operation_id(root, "INC-S2", "AGGREGATE_IDENTITY")
    # Candidate-B owns the IDs; this helper freezes only request identity.  The
    # test binds immutable A lineage after a public capture has returned them.
    return basis, command, aggregate_id


def _run(orchestrator, evidence, *, attempt_id="attempt:S2", work_id=None, basis_override=None):
    basis, command, aggregate_id = _request(evidence, attempt_id=attempt_id)
    if basis_override is not None:
        basis = basis_override(basis)
        command = replace(command, capture_operation_id=basis.capture_operation_id,
                          snapshot_at=basis.snapshot_at)
    retrieval = replace(request(), operation_key=__import__("knowledge_index").RetrievalOperationKey(
        rca_child_operation_id(rca_root_id("INC-S2"), basis.capture_operation_id, "KNOWLEDGE_RETRIEVAL")))
    # E supplies only stable A intent/provenance. B and C return the immutable
    # lineage facts in order; E never fabricates Snapshot or Revision IDs.
    work_id = work_id or runtime_work_id(RuntimeWorkKind.RCA_INITIAL, rca_root_id("INC-S2"))
    return orchestrator.run(InitialRcaRequest("INC-S2", work_id, 3, basis, NOW, command, retrieval,
        limits(), None, None, attempt_id=attempt_id,
        generation_provenance=GenerationProvenance("provider-s2", "model-s2", "prompt-s2", "config-s2", "profile-s2")))


def test_real_public_a_b_c_composition_consumes_no_baseline_and_stops_before_d(tmp_path):
    incidents, _, _, evidence, adapters, rca, _, knowledge, provider = _public_services(tmp_path)
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    orchestrator = InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
        candidate_c=knowledge, continuations=d2)
    # Initial discovery must admit an Incident before assignment exists.
    assert incidents.get_incident("INC-S2").assignee is None
    result = _run(orchestrator, evidence)
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert result.aggregate is not None and result.aggregate.incident_id == "INC-S2"
    assert result.evidence is not None and result.evidence.snapshot_id is not None
    assert result.evidence.revision_id is not None
    snapshot = evidence.resolve_snapshot(result.evidence.snapshot_id)
    revision = evidence.resolve_revision(result.evidence.revision_id)
    assert snapshot is not None and snapshot.capture_operation_id == result.evidence.capture_operation_id
    assert snapshot.revision_id == result.evidence.revision_id
    assert revision is not None and revision.revision_id == snapshot.revision_id
    assert snapshot.incident_id == revision.incident_id == "INC-S2"
    assert result.materiality is not None and result.materiality.request.evaluation_kind is MaterialityEvaluationKind.NO_BASELINE
    assert result.materiality.request.baseline_revision_id is None
    assert result.materiality.request.candidate_revision_id == revision.revision_id
    assert result.materiality.judgement is None and "NO_BASELINE" in result.materiality.reason_facts[0]
    assert evidence.compare_materiality(result.materiality.request) == result.materiality
    assert result.knowledge is not None and result.knowledge.snapshot is not None
    knowledge_read = knowledge.read_snapshot(result.knowledge.snapshot.snapshot_key)
    assert knowledge_read.status is KnowledgeReadStatus.FOUND
    assert knowledge_read.value == result.knowledge.snapshot
    assert result.knowledge.snapshot.resolution is result.knowledge.resolution
    assert result.attempt is not None and result.attempt.lifecycle is GenerationLifecycle.PENDING
    assert result.attempt.latest_try_ordinal is None
    assert result.attempt.lineage.aggregate_id == result.aggregate.aggregate_id
    assert result.attempt.lineage.evidence_snapshot_id == snapshot.snapshot_id
    assert result.attempt.lineage.evidence_revision_id == revision.revision_id
    assert result.attempt.lineage.knowledge_snapshot_id == result.knowledge.snapshot.snapshot_key.value
    assert rca.get_aggregate_by_incident("INC-S2") == rca.get_aggregate(result.aggregate.aggregate_id) == result.aggregate
    assert rca.get_attempt_lineage("attempt:S2").attempt == result.attempt
    # Public replay proves the same one Aggregate and one admitted Attempt.
    replay = _run(orchestrator, evidence)
    assert replay.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert replay.aggregate == result.aggregate and replay.attempt == result.attempt
    assert replay.evidence == result.evidence and replay.materiality == result.materiality
    assert replay.knowledge.snapshot == result.knowledge.snapshot
    assert all(adapter.calls == 1 for adapter in adapters.values())
    assert provider.calls == 1
    assert rca.get_version_history(result.aggregate.aggregate_id) == ()
    assert rca.get_current(result.aggregate.aggregate_id) is None
    assert incidents.get_rca_relationship("INC-S2").current_version_id is None
    assert d2.get(rca_root_id("INC-S2")).execution_operation_id is None
    import runtime_orchestration.rca_initial as initial_module
    source = inspect.getsource(initial_module)
    assert "SqliteIncidentStore" not in source and "SqliteEvidenceStore" not in source
    assert "SqliteKnowledgeStore" not in source and "_transaction" not in source


def test_existing_valid_d2_rca_work_authorizes_s2_without_replacement(tmp_path):
    incidents, _, _, evidence, _, rca, _, knowledge, _ = _public_services(tmp_path)
    root = rca_root_id("INC-S2")
    work_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
    d2_path = tmp_path / "runtime.sqlite3"
    with SqliteRuntimeWorkStore(d2_path) as work_store:
        original = work_store.create(RuntimeWorkRecord(
            work_id, RuntimeWorkKind.RCA_INITIAL, "EVT-S2", "INITIAL", "INITIAL", 0, 3,
            RuntimeWorkStatus.OUTSTANDING, NOW, NOW, NOW, incident_id="INC-S2", operation_id=root))
    d2 = SqliteRcaContinuationStore(d2_path)
    result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca,
        candidate_b=evidence, candidate_c=knowledge, continuations=d2), evidence)
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert result.continuation.runtime_work_id == work_id
    with SqliteRuntimeWorkStore(d2_path) as work_store:
        assert work_store.enumerate_all().records == (original,)


def test_arbitrary_caller_work_id_cannot_authorize_domain_effects(tmp_path):
    incidents, _, _, evidence, adapters, rca, _, knowledge, provider = _public_services(tmp_path)
    d2_path = tmp_path / "runtime.sqlite3"
    d2 = SqliteRcaContinuationStore(d2_path)
    result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca,
        candidate_b=evidence, candidate_c=knowledge, continuations=d2), evidence,
        work_id="rtw-arbitrary")
    assert result.disposition is InitialRcaDisposition.REPAIR_REQUIRED
    assert rca.get_aggregate_by_incident("INC-S2") is None
    assert all(adapter.calls == 0 for adapter in adapters.values()) and provider.calls == 0
    with SqliteRuntimeWorkStore(d2_path) as work_store:
        assert work_store.enumerate_all().records == ()


def test_existing_d2_work_and_caller_identity_mismatch_fails_closed(tmp_path):
    incidents, _, _, evidence, adapters, rca, _, knowledge, provider = _public_services(tmp_path)
    root = rca_root_id("INC-S2")
    work_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
    d2_path = tmp_path / "runtime.sqlite3"
    with SqliteRuntimeWorkStore(d2_path) as work_store:
        original = work_store.create(RuntimeWorkRecord(
            work_id, RuntimeWorkKind.RCA_INITIAL, "EVT-S2", "INITIAL", "INITIAL", 0, 3,
            RuntimeWorkStatus.OUTSTANDING, NOW, NOW, NOW, incident_id="INC-S2", operation_id=root))
    d2 = SqliteRcaContinuationStore(d2_path)
    result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca,
        candidate_b=evidence, candidate_c=knowledge, continuations=d2), evidence,
        work_id="rtw-other")
    assert result.disposition is InitialRcaDisposition.REPAIR_REQUIRED
    assert rca.get_aggregate_by_incident("INC-S2") is None
    assert all(adapter.calls == 0 for adapter in adapters.values()) and provider.calls == 0
    with SqliteRuntimeWorkStore(d2_path) as work_store:
        assert work_store.enumerate_all().records == (original,)


def test_missing_d2_work_reconstructs_one_work_from_incident_and_a(tmp_path):
    incidents, _, _, evidence, adapters, rca, _, knowledge, provider = _public_services(tmp_path)
    first_d2 = SqliteRcaContinuationStore(tmp_path / "runtime-first.sqlite3")
    first = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca,
        candidate_b=evidence, candidate_c=knowledge, continuations=first_d2), evidence)
    assert first.disposition is InitialRcaDisposition.READY_FOR_TRY
    first_d2.close()
    recovered_path = tmp_path / "runtime-recovered.sqlite3"
    recovered_d2 = SqliteRcaContinuationStore(recovered_path)
    recovered = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca,
        candidate_b=evidence, candidate_c=knowledge, continuations=recovered_d2), evidence,
        attempt_id="attempt:replacement-process-local")
    assert recovered.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert recovered.aggregate == first.aggregate and recovered.attempt == first.attempt
    assert recovered.continuation.root_id == first.continuation.root_id
    assert recovered.continuation.runtime_work_id == first.continuation.runtime_work_id
    with SqliteRuntimeWorkStore(recovered_path) as work_store:
        assert tuple(work.work_id for work in work_store.enumerate_all().records) == (first.continuation.runtime_work_id,)
    assert all(adapter.calls == 1 for adapter in adapters.values()) and provider.calls == 1


def test_reopened_d2_reuses_same_durable_work_and_domain_effects(tmp_path):
    incidents, _, _, evidence, adapters, rca, _, knowledge, provider = _public_services(tmp_path)
    d2_path = tmp_path / "runtime.sqlite3"
    d2 = SqliteRcaContinuationStore(d2_path)
    first = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca,
        candidate_b=evidence, candidate_c=knowledge, continuations=d2), evidence)
    assert first.disposition is InitialRcaDisposition.READY_FOR_TRY
    with SqliteRuntimeWorkStore(d2_path) as work_store:
        original = work_store.get(first.continuation.runtime_work_id)
    d2.close()
    reopened = SqliteRcaContinuationStore(d2_path)
    replay = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca,
        candidate_b=evidence, candidate_c=knowledge, continuations=reopened), evidence)
    assert replay.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert replay.continuation.runtime_work_id == first.continuation.runtime_work_id
    assert replay.aggregate == first.aggregate and replay.attempt == first.attempt
    with SqliteRuntimeWorkStore(d2_path) as work_store:
        assert work_store.enumerate_all().records == (original,)
    assert all(adapter.calls == 1 for adapter in adapters.values()) and provider.calls == 1


def test_concurrent_initial_discovery_converges_to_one_durable_a_b_c_attempt(tmp_path):
    incidents, _, _, evidence, adapters, rca, knowledge_store, knowledge, provider = _public_services(tmp_path)
    # Establish B/C immutable public terminal facts before two D2 workers race
    # for A discovery/admission.  This is an explicit barrier, never timing.
    seed_d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    seed = InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence, candidate_c=knowledge, continuations=seed_d2)
    first = _run(seed, evidence)
    assert first.disposition is InitialRcaDisposition.READY_FOR_TRY
    seed_d2.close(); rca.close(); knowledge_store.close()
    barrier = Barrier(2)
    def replay(_):
        local_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
        local_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
        policy = load_evidence_policy("configs/incident_evidence.yaml")
        local_b = EvidenceCaptureService(store=local_b_store, incident_reader=incidents, event_reader=EventStore(str(tmp_path / "events.jsonl")), policy=policy,
            adapters={source: FakeAdapter(SourceStatus.AVAILABLE, marker="must-not-capture") for source in EvidenceSource})
        local_k_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
        local_k = KnowledgeSnapshotService(local_k_store, KnowledgeRetrievalService(local_k_store, QueryProvider(error=AssertionError("no replay provider")), RetrievalIndex({}, query_error=AssertionError("no replay query"))))
        local_d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
        barrier.wait()
        result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=local_a, candidate_b=local_b, candidate_c=local_k, continuations=local_d2), local_b)
        local_d2.close(); local_k_store.close(); local_b_store.close(); local_a.close()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replay, range(2)))
    assert all(item.disposition is InitialRcaDisposition.READY_FOR_TRY for item in results)
    with SqliteRcaStore(tmp_path / "rca.sqlite3") as reopened:
        assert reopened.get_aggregate_by_incident("INC-S2") == first.aggregate
        assert reopened.get_attempt_lineage("attempt:S2").attempt == first.attempt
    assert all(adapter.calls == 1 for adapter in adapters.values()) and provider.calls == 1


@pytest.mark.parametrize("contradictory", [False, True], ids=["equivalent", "contradictory"])
def test_pre_freeze_capture_basis_cas_race_converges_or_fails_closed(tmp_path, contradictory):
    context = []
    incidents, _, evidence_store, _, _, rca, knowledge_store, _, _ = _public_services(
        tmp_path, retrieval_context=context)
    staged, index = context[0]
    evidence_store.close(); rca.close(); knowledge_store.close()
    root = rca_root_id("INC-S2")
    barrier = Barrier(2)
    winner_completed = Event()
    conflicts = []
    effects = []

    class RacingContinuationStore(SqliteRcaContinuationStore):
        def _update(self, record, *, expected_revision, allow_capture_freeze):
            if allow_capture_freeze:
                # Both freeze calls have read the same unfrozen row before CAS.
                barrier.wait(timeout=20)
                if self.executor_index == 1:
                    assert winner_completed.wait(timeout=20)
            try:
                return super()._update(record, expected_revision=expected_revision,
                                       allow_capture_freeze=allow_capture_freeze)
            except RcaContinuationConcurrencyError:
                if allow_capture_freeze:
                    conflicts.append(record.capture_basis)
                raise

    def execute(index_number):
        local_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
        local_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
        policy = load_evidence_policy("configs/incident_evidence.yaml")
        adapters = {source: FakeAdapter(SourceStatus.AVAILABLE, marker="s2") for source in EvidenceSource}
        local_b = EvidenceCaptureService(store=local_b_store, incident_reader=incidents,
            event_reader=EventStore(str(tmp_path / "events.jsonl")), policy=policy, adapters=adapters)
        local_k_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
        provider = QueryProvider()
        local_k = KnowledgeSnapshotService(local_k_store, KnowledgeRetrievalService(
            local_k_store, provider, RetrievalIndex(index.artifacts, candidates_for(staged, 0.1))))
        local_d2 = RacingContinuationStore(tmp_path / "runtime.sqlite3")
        local_d2.executor_index = index_number
        basis_override = (lambda basis: replace(basis, collection_boundary_references=("collection:logs",))) if contradictory and index_number else None
        try:
            result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=local_a,
                candidate_b=local_b, candidate_c=local_k, continuations=local_d2),
                local_b, basis_override=basis_override)
            effects.append((sum(adapter.calls for adapter in adapters.values()), provider.calls))
            return result
        finally:
            if index_number == 0:
                winner_completed.set()
            local_d2.close(); local_k_store.close(); local_b_store.close(); local_a.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    durable_store = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    durable = durable_store.get(root)
    assert durable_store.enumerate_all().records == (durable,)
    durable_store.close()
    assert durable is not None and durable.capture_basis is not None
    assert durable.root_id == root
    assert len(conflicts) == 1  # The loser actually reached and lost freeze CAS.
    assert sum(provider_calls for _, provider_calls in effects) == 1
    assert sum(calls for calls, _ in effects) == len(EvidenceSource)
    with SqliteRcaStore(tmp_path / "rca.sqlite3") as reopened_a:
        aggregate = reopened_a.get_aggregate_by_incident("INC-S2")
        assert aggregate is not None
        attempts = [reopened_a.get_attempt_lineage("attempt:S2")]
        assert len([view for view in attempts if view is not None]) == 1
        assert reopened_a.get_version_history(aggregate.aggregate_id) == ()
    if contradictory:
        assert sorted(item.disposition.value for item in results) == ["READY_FOR_TRY", "REPAIR_REQUIRED"]
        winner = next(item for item in results if item.disposition is InitialRcaDisposition.READY_FOR_TRY)
        assert winner.continuation.capture_basis == durable.capture_basis
        assert winner.continuation.capture_operation_id == durable.capture_operation_id
        assert conflicts[0] != durable.capture_basis
    else:
        assert all(item.disposition is InitialRcaDisposition.READY_FOR_TRY for item in results)
        assert all(item.continuation.capture_basis == durable.capture_basis for item in results)
        assert all(item.continuation.capture_operation_id == durable.capture_operation_id for item in results)
        assert all(item.continuation.capture_basis.snapshot_at == NOW for item in results)
        assert all(item.attempt == results[0].attempt for item in results)
        assert all(item.knowledge.snapshot == results[0].knowledge.snapshot for item in results)


def test_full_a_b_c_d2_close_reopen_replays_all_s2_identities(tmp_path):
    incidents, _, evidence_store, evidence, _, rca, knowledge_store, knowledge, _ = _public_services(tmp_path)
    d2_path = tmp_path / "runtime.sqlite3"; d2 = SqliteRcaContinuationStore(d2_path)
    first = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence, candidate_c=knowledge, continuations=d2), evidence)
    assert first.disposition is InitialRcaDisposition.READY_FOR_TRY
    root = rca_root_id("INC-S2"); before = d2.get(root)
    # Close every durable authority and discard every process-local service.
    d2.close(); rca.close(); evidence_store.close(); knowledge_store.close()
    replay_a = SqliteRcaStore(tmp_path / "rca.sqlite3"); replay_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
    policy = load_evidence_policy("configs/incident_evidence.yaml")
    replay_b = EvidenceCaptureService(store=replay_b_store, incident_reader=incidents, event_reader=EventStore(str(tmp_path / "events.jsonl")), policy=policy,
        adapters={source: FakeAdapter(SourceStatus.AVAILABLE, marker="must-not-capture") for source in EvidenceSource})
    replay_k_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    replay_k = KnowledgeSnapshotService(replay_k_store, KnowledgeRetrievalService(replay_k_store,
        QueryProvider(error=AssertionError("restart must not query provider")), RetrievalIndex({}, query_error=AssertionError("restart must not query index"))))
    replay_d2 = SqliteRcaContinuationStore(d2_path)
    replay = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=replay_a, candidate_b=replay_b, candidate_c=replay_k, continuations=replay_d2), replay_b)
    after = replay_d2.get(root)
    assert replay.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert (after.root_id, after.capture_operation_id, after.retrieval_operation_id, after.attempt_id) == (before.root_id, before.capture_operation_id, before.retrieval_operation_id, before.attempt_id)
    assert replay.aggregate == first.aggregate and replay.attempt == first.attempt
    assert replay.evidence == first.evidence and replay.materiality == first.materiality
    assert replay.knowledge.snapshot == first.knowledge.snapshot
    assert replay.evidence.command_semantic_identity == capture_command_semantic_identity(_request(replay_b)[1])


def test_retrieval_terminal_crash_reopens_same_durable_identity_without_second_effect(tmp_path):
    incidents, _, evidence_store, evidence, _, rca, knowledge_store, knowledge, provider = _public_services(tmp_path)
    root = rca_root_id("INC-S2")
    d2_path = tmp_path / "runtime.sqlite3"
    d2 = SqliteRcaContinuationStore(d2_path)

    class CrashAfterTerminal:
        def read_operation(self, key): return knowledge.read_operation(key)
        def read_snapshot(self, key): return knowledge.read_snapshot(key)
        def resolve(self, request, limits):
            terminal = knowledge.resolve(request, limits)
            assert terminal.snapshot is not None
            raise SimulatedCrash
        def finalize_unavailable(self, request): return knowledge.finalize_unavailable(request)

    class SimulatedCrash(BaseException): pass

    try:
        _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
            candidate_c=CrashAfterTerminal(), continuations=d2), evidence)
        assert False, "expected deterministic crash after Candidate-C terminal effect"
    except SimulatedCrash:
        pass
    before = d2.get(root)
    assert before.retrieval_operation_id == rca_child_operation_id(root, before.capture_operation_id, "KNOWLEDGE_RETRIEVAL")
    assert before.attempt_id == "attempt:S2"
    assert before.materiality_result_id is not None
    assert provider.calls == 1
    d2.close(); evidence_store.close(); knowledge_store.close(); rca.close()

    reopened_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    reopened_knowledge = KnowledgeSnapshotService(reopened_store, KnowledgeRetrievalService(reopened_store,
        QueryProvider(error=AssertionError("duplicate retrieval provider effect")),
        RetrievalIndex({}, query_error=AssertionError("duplicate retrieval index effect"))))
    reopened_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
    reopened_b = EvidenceCaptureService(store=reopened_b_store, incident_reader=incidents,
        event_reader=EventStore(str(tmp_path / "events.jsonl")), policy=load_evidence_policy("configs/incident_evidence.yaml"),
        adapters={source: FakeAdapter(SourceStatus.AVAILABLE, marker="no-second-capture") for source in EvidenceSource})
    reopened_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
    reopened_d2 = SqliteRcaContinuationStore(d2_path)
    result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=reopened_a, candidate_b=reopened_b,
        candidate_c=reopened_knowledge, continuations=reopened_d2), reopened_b,
        attempt_id="attempt:replacement-process-local")
    after = reopened_d2.get(root)
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert result.knowledge.snapshot == reopened_knowledge.read_snapshot(result.knowledge.snapshot.snapshot_key).value
    assert (after.root_id, after.capture_operation_id, after.materiality_result_id,
            after.retrieval_operation_id, after.attempt_id) == (before.root_id, before.capture_operation_id,
            before.materiality_result_id, before.retrieval_operation_id, before.attempt_id)
    assert reopened_a.get_attempt_lineage(before.attempt_id).attempt == result.attempt
    assert provider.calls == 1


def test_completion_without_snapshot_replays_through_public_candidate_c_resolve(tmp_path):
    class CrashAfterCompletion(BaseException): pass

    class CompletionCrashStore(SqliteKnowledgeStore):
        def record_retrieval_completion(self, completion):
            durable = super().record_retrieval_completion(completion)
            assert durable == completion
            raise CrashAfterCompletion

    incidents, _, evidence_store, evidence, adapters, rca, knowledge_store, knowledge, provider = _public_services(
        tmp_path, knowledge_store_type=CompletionCrashStore)
    root = rca_root_id("INC-S2")
    d2_path = tmp_path / "runtime.sqlite3"
    d2 = SqliteRcaContinuationStore(d2_path)
    with pytest.raises(CrashAfterCompletion):
        _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
            candidate_c=knowledge, continuations=d2), evidence)
    before = d2.get(root)
    retrieval_key = __import__("knowledge_index").RetrievalOperationKey(before.retrieval_operation_id)
    incomplete = knowledge.read_operation(retrieval_key)
    assert incomplete.status is KnowledgeReadStatus.FOUND
    assert incomplete.value.state is RetrievalOperationState.RETRIEVAL_COMPLETED
    assert incomplete.value.completion is not None and incomplete.value.snapshot_key is None
    assert rca.get_attempt_lineage(before.attempt_id) is None
    assert provider.calls == 1
    d2.close(); knowledge_store.close(); evidence_store.close(); rca.close()

    reopened_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    reopened_service = KnowledgeSnapshotService(reopened_store, KnowledgeRetrievalService(reopened_store,
        QueryProvider(error=AssertionError("completion replay must not invoke provider")),
        RetrievalIndex({}, query_error=AssertionError("completion replay must not query index"))))

    class PublicResolveSpy:
        def __init__(self): self.calls = 0
        def read_operation(self, key): return reopened_service.read_operation(key)
        def read_snapshot(self, key): return reopened_service.read_snapshot(key)
        def resolve(self, request, limits):
            self.calls += 1
            return reopened_service.resolve(request, limits)
        def finalize_unavailable(self, request): return reopened_service.finalize_unavailable(request)

    spy = PublicResolveSpy()
    reopened_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
    reopened_b = EvidenceCaptureService(store=reopened_b_store, incident_reader=incidents,
        event_reader=EventStore(str(tmp_path / "events.jsonl")), policy=load_evidence_policy("configs/incident_evidence.yaml"),
        adapters={source: FakeAdapter(SourceStatus.AVAILABLE, marker="no-recapture") for source in EvidenceSource})
    reopened_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
    reopened_d2 = SqliteRcaContinuationStore(d2_path)
    orchestrator = InitialRcaOrchestrator(incidents=incidents, candidate_a=reopened_a, candidate_b=reopened_b,
        candidate_c=spy, continuations=reopened_d2)
    result = _run(orchestrator, reopened_b, attempt_id="attempt:replacement-process-local")
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert spy.calls == 1
    completed = reopened_service.read_operation(retrieval_key)
    assert completed.status is KnowledgeReadStatus.FOUND
    assert completed.value.state is RetrievalOperationState.COMPLETED
    assert completed.value.completion == incomplete.value.completion
    assert completed.value.snapshot_key == result.knowledge.snapshot.snapshot_key
    assert result.knowledge.snapshot.operation_key == retrieval_key
    assert result.knowledge.snapshot.lineage_commitment == incomplete.value.completion.lineage_commitment
    assert reopened_service.read_snapshot(completed.value.snapshot_key).value == result.knowledge.snapshot
    assert result.attempt.lineage.attempt_id == before.attempt_id
    assert result.attempt.lineage.knowledge_snapshot_id == completed.value.snapshot_key.value
    assert reopened_a.get_attempt_lineage(before.attempt_id).attempt == result.attempt
    assert reopened_d2.get(root).retrieval_operation_id == before.retrieval_operation_id
    assert all(adapter.calls == 1 for adapter in adapters.values()) and provider.calls == 1
    replay = _run(orchestrator, reopened_b, attempt_id="attempt:another-process-local")
    assert replay.attempt == result.attempt and replay.knowledge.snapshot == result.knowledge.snapshot
    assert spy.calls == 1


def test_frozen_without_completion_restarts_same_public_retrieval_operation(tmp_path):
    class CrashAfterFreeze(BaseException): pass

    class FreezeCrashStore(SqliteKnowledgeStore):
        def freeze_retrieval_operation(self, request):
            frozen = super().freeze_retrieval_operation(request)
            assert frozen.request == request
            raise CrashAfterFreeze

    retrieval_context = []
    incidents, _, evidence_store, evidence, adapters, rca, knowledge_store, knowledge, original_provider = _public_services(
        tmp_path, knowledge_store_type=FreezeCrashStore, retrieval_context=retrieval_context)
    root = rca_root_id("INC-S2")
    d2_path = tmp_path / "runtime.sqlite3"
    d2 = SqliteRcaContinuationStore(d2_path)
    with pytest.raises(CrashAfterFreeze):
        _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
            candidate_c=knowledge, continuations=d2), evidence)
    before = d2.get(root)
    key = RetrievalOperationKey(before.retrieval_operation_id)
    frozen_read = knowledge.read_operation(key)
    assert frozen_read.status is KnowledgeReadStatus.FOUND
    assert frozen_read.value.state is RetrievalOperationState.FROZEN
    assert frozen_read.value.completion is None and frozen_read.value.snapshot_key is None
    assert rca.get_attempt_lineage(before.attempt_id) is None
    assert original_provider.calls == 0
    d2.close(); knowledge_store.close(); evidence_store.close(); rca.close()

    staged, index = retrieval_context[0]
    reopened_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    replay_provider = QueryProvider()
    replay_index = RetrievalIndex(index.artifacts, candidates_for(staged, 0.1))
    reopened_service = KnowledgeSnapshotService(reopened_store, KnowledgeRetrievalService(
        reopened_store, replay_provider, replay_index))

    class PublicResolveSpy:
        def __init__(self): self.calls = 0
        def read_operation(self, key): return reopened_service.read_operation(key)
        def read_snapshot(self, key): return reopened_service.read_snapshot(key)
        def resolve(self, request, limits):
            self.calls += 1
            assert request == frozen_read.value.frozen.request
            return reopened_service.resolve(request, limits)
        def finalize_unavailable(self, request): return reopened_service.finalize_unavailable(request)

    spy = PublicResolveSpy()
    reopened_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
    reopened_b = EvidenceCaptureService(store=reopened_b_store, incident_reader=incidents,
        event_reader=EventStore(str(tmp_path / "events.jsonl")), policy=load_evidence_policy("configs/incident_evidence.yaml"),
        adapters={source: FakeAdapter(SourceStatus.AVAILABLE, marker="no-recapture") for source in EvidenceSource})
    reopened_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
    reopened_d2 = SqliteRcaContinuationStore(d2_path)
    orchestrator = InitialRcaOrchestrator(incidents=incidents, candidate_a=reopened_a, candidate_b=reopened_b,
        candidate_c=spy, continuations=reopened_d2)
    result = _run(orchestrator, reopened_b, attempt_id="attempt:replacement-process-local")
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert spy.calls == 1 and replay_provider.calls == 1
    completed = reopened_service.read_operation(key)
    assert completed.status is KnowledgeReadStatus.FOUND
    assert completed.value.state is RetrievalOperationState.COMPLETED
    assert completed.value.frozen == frozen_read.value.frozen
    assert completed.value.snapshot_key == result.knowledge.snapshot.snapshot_key
    assert result.knowledge.snapshot.operation_key == key
    assert reopened_service.read_snapshot(completed.value.snapshot_key).value == result.knowledge.snapshot
    assert result.attempt.lineage.attempt_id == before.attempt_id
    assert result.attempt.lineage.knowledge_snapshot_id == completed.value.snapshot_key.value
    assert reopened_a.get_attempt_lineage(before.attempt_id).attempt == result.attempt
    assert reopened_d2.get(root).retrieval_operation_id == before.retrieval_operation_id
    assert all(adapter.calls == 1 for adapter in adapters.values())
    replay = _run(orchestrator, reopened_b, attempt_id="attempt:second-replacement")
    assert replay.knowledge.snapshot == result.knowledge.snapshot and replay.attempt == result.attempt
    assert spy.calls == 1 and replay_provider.calls == 1


def test_durable_frozen_request_contradiction_remains_repair_required(tmp_path):
    incidents, _, _, evidence, _, rca, knowledge_store, knowledge, provider = _public_services(tmp_path)
    basis, _, _ = _request(evidence)
    root = rca_root_id("INC-S2")
    key = RetrievalOperationKey(rca_child_operation_id(root, basis.capture_operation_id, "KNOWLEDGE_RETRIEVAL"))
    expected = replace(request(), operation_key=key)
    contradictory = replace(expected, query=replace(expected.query, text="different frozen query"))
    knowledge_store.freeze_retrieval_operation(contradictory)
    knowledge_store.close()
    reopened_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    reopened_service = KnowledgeSnapshotService(reopened_store, KnowledgeRetrievalService(reopened_store,
        QueryProvider(error=AssertionError("contradictory frozen request must not query provider")),
        RetrievalIndex({}, query_error=AssertionError("contradictory frozen request must not query index"))))
    frozen = reopened_service.read_operation(key)
    assert frozen.status is KnowledgeReadStatus.FOUND and frozen.value.state is RetrievalOperationState.FROZEN
    assert frozen.value.frozen.request == contradictory and frozen.value.completion is None
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
        candidate_c=reopened_service, continuations=d2), evidence)
    assert result.disposition is InitialRcaDisposition.REPAIR_REQUIRED
    assert reopened_service.read_operation(key).value.frozen.request == contradictory
    assert rca.get_attempt_lineage("attempt:S2") is None
    assert provider.calls == 0


@pytest.mark.parametrize("public_read, expected", [
    (KnowledgeReadResult(KnowledgeReadStatus.UNAVAILABLE), InitialRcaDisposition.UNAVAILABLE),
    (KnowledgeReadResult(KnowledgeReadStatus.FOUND, object()), InitialRcaDisposition.REPAIR_REQUIRED),
    (KnowledgeReadResult(KnowledgeReadStatus.FOUND, SimpleNamespace(
        frozen=SimpleNamespace(request=object()), snapshot_key=None)), InitialRcaDisposition.REPAIR_REQUIRED),
])
def test_unreadable_malformed_or_contradictory_candidate_c_read_fails_closed(tmp_path, public_read, expected):
    incidents, _, _, evidence, _, rca, _, knowledge, _ = _public_services(tmp_path)

    class BadPublicRead:
        def __init__(self): self.resolve_calls = 0
        def read_operation(self, key): return public_read
        def resolve(self, request, limits):
            self.resolve_calls += 1
            raise AssertionError("invalid public read must not invoke retrieval")

    bad = BadPublicRead()
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
        candidate_c=bad, continuations=d2), evidence)
    assert result.disposition is expected
    assert bad.resolve_calls == 0
    assert rca.get_attempt_lineage("attempt:S2") is None


@pytest.mark.parametrize("replay_result, expected", [
    ("unreadable", InitialRcaDisposition.UNAVAILABLE),
    ("malformed", InitialRcaDisposition.REPAIR_REQUIRED),
    ("contradictory", InitialRcaDisposition.REPAIR_REQUIRED),
])
def test_completion_replay_public_resolve_failure_fails_closed(tmp_path, replay_result, expected):
    class CrashAfterCompletion(BaseException): pass

    class CompletionCrashStore(SqliteKnowledgeStore):
        def record_retrieval_completion(self, completion):
            super().record_retrieval_completion(completion)
            raise CrashAfterCompletion

    incidents, _, _, evidence, _, rca, knowledge_store, knowledge, provider = _public_services(
        tmp_path, knowledge_store_type=CompletionCrashStore)
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    with pytest.raises(CrashAfterCompletion):
        _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
            candidate_c=knowledge, continuations=d2), evidence)
    key = __import__("knowledge_index").RetrievalOperationKey(d2.get(rca_root_id("INC-S2")).retrieval_operation_id)
    assert knowledge.read_operation(key).value.state is RetrievalOperationState.RETRIEVAL_COMPLETED

    class BadReplay:
        def read_operation(self, key): return knowledge.read_operation(key)
        def resolve(self, request, limits):
            if replay_result == "unreadable":
                raise OSError("public Candidate-C replay is unavailable")
            if replay_result == "malformed":
                return object()
            return SimpleNamespace(resolution=__import__("knowledge_index").RetrievalResolution.NO_MATCH,
                snapshot=SimpleNamespace(snapshot_key=object()))

    result = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
        candidate_c=BadReplay(), continuations=d2), evidence)
    assert result.disposition is expected
    assert knowledge.read_operation(key).value.snapshot_key is None
    assert rca.get_attempt_lineage("attempt:S2") is None
    assert provider.calls == 1


def test_existing_capture_receipt_requires_exact_command_semantics(tmp_path):
    incidents, _, _, evidence, adapters, rca, _, knowledge, provider = _public_services(tmp_path)
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    orchestrator = InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
        candidate_c=knowledge, continuations=d2)
    first = _run(orchestrator, evidence)
    assert first.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert first.evidence.command_semantic_identity == capture_command_semantic_identity(_request(evidence)[1])
    equivalent = _run(orchestrator, evidence)
    assert equivalent.evidence == first.evidence
    assert all(adapter.calls == 1 for adapter in adapters.values()) and provider.calls == 1

    class ContradictoryReceipt:
        def __init__(self): self.capture_calls = 0
        def read_capture_outcome(self, operation_id):
            return replace(evidence.read_capture_outcome(operation_id), command_semantic_identity="contradictory-command")
        def capture_evidence(self, command):
            self.capture_calls += 1
            raise AssertionError("contradictory receipt must not invoke capture")
        def resolve_snapshot(self, key): return evidence.resolve_snapshot(key)
        def resolve_revision(self, key): return evidence.resolve_revision(key)
        def compare_materiality(self, request): return evidence.compare_materiality(request)

    conflicting = ContradictoryReceipt()
    rejected = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=conflicting,
        candidate_c=knowledge, continuations=d2), conflicting)
    assert rejected.disposition is InitialRcaDisposition.REPAIR_REQUIRED
    assert conflicting.capture_calls == 0
    assert all(adapter.calls == 1 for adapter in adapters.values()) and provider.calls == 1


def test_exact_attempt_lineage_admission_identity_replays_without_collision(tmp_path):
    incidents, _, _, evidence, _, rca, _, knowledge, _ = _public_services(tmp_path)
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    orchestrator = InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
        candidate_c=knowledge, continuations=d2)
    first = _run(orchestrator, evidence)
    assert first.disposition is InitialRcaDisposition.READY_FOR_TRY
    lineage = first.attempt.lineage
    root = rca_root_id("INC-S2")
    admission_id = derive_attempt_admission_operation_id(root, lineage)
    assert admission_id == derive_attempt_admission_operation_id(root, replace(lineage))
    assert rca.admit_attempt(AdmitAttemptRequest(admission_id, lineage, NOW)) == first.attempt
    assert _run(orchestrator, evidence).attempt == first.attempt
    differences = (
        replace(lineage, evidence_snapshot_id="snapshot:different"),
        replace(lineage, evidence_revision_id="revision:different"),
        replace(lineage, knowledge_snapshot_id="knowledge:different"),
        replace(lineage, generation_provenance=replace(lineage.generation_provenance, model_id="model:different")),
    )
    for changed in differences:
        changed_id = derive_attempt_admission_operation_id(root, changed)
        assert changed_id != admission_id
        with pytest.raises(RcaDomainError):
            rca.admit_attempt(AdmitAttemptRequest(changed_id, changed, NOW))
    assert rca.get_attempt_lineage(lineage.attempt_id).attempt == first.attempt


def test_missing_d2_rediscovers_existing_candidate_a_attempt_and_lineage(tmp_path):
    incidents, _, evidence_store, evidence, adapters, rca, knowledge_store, knowledge, provider = _public_services(tmp_path)
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime-original.sqlite3")
    first = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=rca, candidate_b=evidence,
        candidate_c=knowledge, continuations=d2), evidence)
    assert first.disposition is InitialRcaDisposition.READY_FOR_TRY
    d2.close(); evidence_store.close(); knowledge_store.close(); rca.close()
    reopened_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
    reopened_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
    reopened_adapters = {source: FakeAdapter(SourceStatus.AVAILABLE, marker="no-recapture") for source in EvidenceSource}
    reopened_b = EvidenceCaptureService(store=reopened_b_store, incident_reader=incidents,
        event_reader=EventStore(str(tmp_path / "events.jsonl")), policy=load_evidence_policy("configs/incident_evidence.yaml"),
        adapters=reopened_adapters)
    reopened_k_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    reopened_k = KnowledgeSnapshotService(reopened_k_store, KnowledgeRetrievalService(reopened_k_store,
        QueryProvider(error=AssertionError("no replacement retrieval")),
        RetrievalIndex({}, query_error=AssertionError("no replacement query"))))
    assert reopened_a.get_attempt_lineage(first.attempt.lineage.attempt_id).attempt == first.attempt
    assert reopened_k.read_snapshot(first.knowledge.snapshot.snapshot_key).value == first.knowledge.snapshot
    missing_d2 = SqliteRcaContinuationStore(tmp_path / "runtime-reconstructed.sqlite3")
    assert missing_d2.get(rca_root_id("INC-S2")) is None
    replay = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=reopened_a, candidate_b=reopened_b,
        candidate_c=reopened_k, continuations=missing_d2), reopened_b,
        attempt_id="attempt:replacement-process-local")
    reconstructed = missing_d2.get(rca_root_id("INC-S2"))
    assert replay.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert replay.aggregate == first.aggregate and replay.attempt == first.attempt
    assert replay.evidence == first.evidence and replay.knowledge.snapshot == first.knowledge.snapshot
    assert reconstructed.aggregate_id == first.aggregate.aggregate_id
    assert reconstructed.attempt_id == first.attempt.lineage.attempt_id
    assert reconstructed.retrieval_operation_id == first.knowledge.snapshot.operation_key.value
    assert len(tuple(item for item in reopened_a.enumerate_recovery_candidates()
        if item.attempt_id == first.attempt.lineage.attempt_id)) == 1
    assert all(adapter.calls == 1 for adapter in adapters.values()) and provider.calls == 1
    assert all(adapter.calls == 0 for adapter in reopened_adapters.values())


def test_missing_d2_with_no_candidate_a_attempt_legally_admits_once(tmp_path):
    incidents, _, _, evidence, _, rca, _, knowledge, provider = _public_services(tmp_path)
    root = rca_root_id("INC-S2")
    aggregate_id = rca_child_operation_id(root, "INC-S2", "AGGREGATE_IDENTITY")
    discover_id = rca_child_operation_id(root, aggregate_id, "AGGREGATE_DISCOVERY")
    aggregate = rca.create_or_discover_aggregate(CreateAggregateRequest(discover_id, aggregate_id, "INC-S2", NOW))
    assert rca.get_aggregate_by_incident("INC-S2") == aggregate
    assert rca.enumerate_recovery_candidates() == ()
    rca.close()
    reopened_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
    missing_d2 = SqliteRcaContinuationStore(tmp_path / "runtime-reconstructed.sqlite3")
    first = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=reopened_a, candidate_b=evidence,
        candidate_c=knowledge, continuations=missing_d2), evidence)
    assert first.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert first.aggregate == aggregate and first.attempt.lineage.aggregate_id == aggregate_id
    assert reopened_a.get_attempt_lineage(first.attempt.lineage.attempt_id).attempt == first.attempt
    replay = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=reopened_a, candidate_b=evidence,
        candidate_c=knowledge, continuations=missing_d2), evidence)
    assert replay.attempt == first.attempt and provider.calls == 1
    assert len(tuple(item for item in reopened_a.enumerate_recovery_candidates()
        if item.aggregate_id == aggregate_id and item.attempt_id is not None)) == 1


def test_missing_d2_with_contradictory_candidate_a_attempt_fails_closed(tmp_path):
    incidents, _, _, evidence, _, rca, _, knowledge, _ = _public_services(tmp_path)
    root = rca_root_id("INC-S2")
    aggregate_id = rca_child_operation_id(root, "INC-S2", "AGGREGATE_IDENTITY")
    discover_id = rca_child_operation_id(root, aggregate_id, "AGGREGATE_DISCOVERY")
    rca.create_or_discover_aggregate(CreateAggregateRequest(discover_id, aggregate_id, "INC-S2", NOW))
    wrong = AttemptLineage("attempt:S2", aggregate_id, "snapshot:wrong", "revision:wrong",
        "knowledge:wrong", GenerationProvenance("provider-s2", "model-s2", "prompt-s2", "config-s2", "profile-s2"))
    existing = rca.admit_attempt(AdmitAttemptRequest(derive_attempt_admission_operation_id(root, wrong), wrong, NOW))
    rca.close()
    reopened_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
    missing_d2 = SqliteRcaContinuationStore(tmp_path / "runtime-reconstructed.sqlite3")
    rejected = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=reopened_a, candidate_b=evidence,
        candidate_c=knowledge, continuations=missing_d2), evidence)
    assert rejected.disposition is InitialRcaDisposition.REPAIR_REQUIRED
    assert reopened_a.get_attempt_lineage("attempt:S2").attempt == existing
    assert len(tuple(item for item in reopened_a.enumerate_recovery_candidates()
        if item.aggregate_id == aggregate_id and item.attempt_id is not None)) == 1
    assert missing_d2.get(root).attempt_id == existing.lineage.attempt_id


def test_crash_before_attempt_admission_rederives_exact_lineage_operation_id(tmp_path):
    class CrashBeforeAdmission(BaseException): pass

    incidents, _, evidence_store, evidence, _, rca, knowledge_store, knowledge, provider = _public_services(tmp_path)
    d2_path = tmp_path / "runtime.sqlite3"
    d2 = SqliteRcaContinuationStore(d2_path)

    class CrashAtA:
        def __init__(self): self.request = None
        def __getattr__(self, name): return getattr(rca, name)
        def admit_attempt(self, request):
            self.request = request
            raise CrashBeforeAdmission

    crashing_a = CrashAtA()
    with pytest.raises(CrashBeforeAdmission):
        _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=crashing_a, candidate_b=evidence,
            candidate_c=knowledge, continuations=d2), evidence)
    captured = crashing_a.request
    assert captured is not None
    assert captured.operation_id == derive_attempt_admission_operation_id(rca_root_id("INC-S2"), captured.lineage)
    assert rca.get_attempt_lineage(captured.lineage.attempt_id) is None
    before = d2.get(rca_root_id("INC-S2"))
    assert before.attempt_id == captured.lineage.attempt_id
    d2.close(); evidence_store.close(); knowledge_store.close(); rca.close()

    reopened_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
    reopened_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
    reopened_b = EvidenceCaptureService(store=reopened_b_store, incident_reader=incidents,
        event_reader=EventStore(str(tmp_path / "events.jsonl")), policy=load_evidence_policy("configs/incident_evidence.yaml"),
        adapters={source: FakeAdapter(SourceStatus.AVAILABLE, marker="no-recapture") for source in EvidenceSource})
    reopened_k_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    reopened_k = KnowledgeSnapshotService(reopened_k_store, KnowledgeRetrievalService(reopened_k_store,
        QueryProvider(error=AssertionError("no new retrieval")),
        RetrievalIndex({}, query_error=AssertionError("no new index query"))))
    reopened_d2 = SqliteRcaContinuationStore(d2_path)

    class AdmissionSpy:
        def __init__(self): self.requests = []
        def __getattr__(self, name): return getattr(reopened_a, name)
        def admit_attempt(self, request):
            self.requests.append(request)
            return reopened_a.admit_attempt(request)

    spy = AdmissionSpy()
    orchestrator = InitialRcaOrchestrator(incidents=incidents, candidate_a=spy, candidate_b=reopened_b,
        candidate_c=reopened_k, continuations=reopened_d2)
    result = _run(orchestrator, reopened_b, attempt_id="attempt:replacement-process-local")
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert len(spy.requests) == 1
    assert spy.requests[0].operation_id == captured.operation_id
    assert spy.requests[0].lineage == captured.lineage
    assert result.attempt == reopened_a.get_attempt_lineage(captured.lineage.attempt_id).attempt
    assert reopened_d2.get(rca_root_id("INC-S2")).attempt_id == captured.lineage.attempt_id
    assert _run(orchestrator, reopened_b).attempt == result.attempt
    assert len(spy.requests) == 1 and provider.calls == 1
