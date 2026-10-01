"""S7 process-memory loss across the public initial RCA authorities."""

import pytest
from datetime import timedelta

from event_detection.store.event_store import EventStore
from incident_evidence import (
    EvidenceCaptureService, EvidenceSource, SourceStatus, SqliteEvidenceStore,
    load_evidence_policy,
)
from knowledge_index import (
    KnowledgeRetrievalService, KnowledgeSnapshotService, SqliteKnowledgeStore,
)
from rca_persistence import SqliteRcaStore
from runtime_orchestration.rca_continuation import SqliteRcaContinuationStore, rca_root_id
from runtime_orchestration.rca_initial import InitialRcaDisposition, InitialRcaOrchestrator

from test_incident_evidence_capture import FakeAdapter
from test_spec_016_initial_composition import NOW, _public_services, _run
from _knowledge_retrieval_testkit import QueryProvider, RetrievalIndex, candidates_for, request, limits


class _Crash(BaseException):
    pass


class _CrashAfterEffect:
    def __init__(self, delegate, operation):
        self._delegate = delegate
        self._operation = operation

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def capture_evidence(self, command):
        result = self._delegate.capture_evidence(command)
        if self._operation == "capture":
            raise _Crash
        return result

    def resolve(self, request, limits):
        result = self._delegate.resolve(request, limits)
        if self._operation == "knowledge":
            raise _Crash
        return result

    def admit_attempt(self, request):
        result = self._delegate.admit_attempt(request)
        if self._operation == "attempt":
            raise _Crash
        return result


@pytest.mark.parametrize("crash_after", ("capture", "knowledge", "attempt"))
def test_reopened_authorities_replay_frozen_initial_identity_without_second_effect(
    tmp_path, crash_after,
):
    retrieval_context = []
    incidents, _, b_store, capture, adapters, a, c_store, knowledge, provider = (
        _public_services(tmp_path, retrieval_context=retrieval_context)
    )
    staged, index = retrieval_context[0]
    d2_path = tmp_path / "runtime.sqlite3"
    d2 = SqliteRcaContinuationStore(d2_path)
    try:
        with pytest.raises(_Crash):
            _run(InitialRcaOrchestrator(
                incidents=incidents,
                candidate_a=_CrashAfterEffect(a, crash_after),
                candidate_b=_CrashAfterEffect(capture, crash_after),
                candidate_c=_CrashAfterEffect(knowledge, crash_after),
                continuations=d2,
            ), capture)
        frozen = d2.get(rca_root_id("INC-S2"))
        assert frozen.capture_basis.snapshot_at == NOW
        first_provider_calls = provider.calls
        first_source_calls = tuple(adapter.calls for adapter in adapters.values())
    finally:
        d2.close(); a.close(); b_store.close(); c_store.close(); incidents.close()

    completed_identity = None
    for replay_index in range(3):
        reopened_incidents = __import__("incident_management").SqliteIncidentStore(
            str(tmp_path / "incidents.sqlite3"))
        reopened_a = SqliteRcaStore(tmp_path / "rca.sqlite3")
        reopened_b_store = SqliteEvidenceStore(tmp_path / "evidence.sqlite3")
        no_second_source = {
            source: FakeAdapter(SourceStatus.AVAILABLE, marker="replay")
            for source in EvidenceSource
        }
        reopened_capture = EvidenceCaptureService(
            store=reopened_b_store, incident_reader=reopened_incidents,
            event_reader=EventStore(str(tmp_path / "events.jsonl")),
            policy=load_evidence_policy("configs/incident_evidence.yaml"),
            adapters=no_second_source,
        )
        reopened_c_store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
        first_knowledge_resolution = crash_after == "capture" and replay_index == 0
        replay_provider = (QueryProvider() if first_knowledge_resolution else
                           QueryProvider(error=AssertionError("duplicate provider invocation")))
        reopened_knowledge = KnowledgeSnapshotService(
            reopened_c_store,
            KnowledgeRetrievalService(
                reopened_c_store,
                replay_provider,
                (RetrievalIndex(index.artifacts, candidates_for(staged, 0.1))
                 if first_knowledge_resolution else
                 RetrievalIndex({}, query_error=AssertionError("duplicate index query"))),
            ),
        )
        reopened_d2 = SqliteRcaContinuationStore(d2_path)
        try:
            replay = _run(InitialRcaOrchestrator(
                incidents=reopened_incidents, candidate_a=reopened_a,
                candidate_b=reopened_capture, candidate_c=reopened_knowledge,
                continuations=reopened_d2,
            ), reopened_capture)
            assert replay.disposition is InitialRcaDisposition.READY_FOR_TRY
            state = reopened_d2.get(rca_root_id("INC-S2"))
            assert state.capture_basis.snapshot_at == NOW
            identity = (state.capture_operation_id, state.retrieval_operation_id,
                        state.attempt_id)
            assert identity[0] == frozen.capture_operation_id
            if frozen.retrieval_operation_id is not None:
                assert identity[1] == frozen.retrieval_operation_id
            if frozen.attempt_id is not None:
                assert identity[2] == frozen.attempt_id
            if completed_identity is not None:
                assert identity == completed_identity
            completed_identity = identity
            assert len(reopened_a.enumerate_recovery_candidates()) == 1
            assert len(reopened_b_store.enumerate_recovery_facts().snapshots) == 1
            assert tuple(adapter.calls for adapter in no_second_source.values()) == (0, 0)
            if first_knowledge_resolution:
                assert replay_provider.calls == 1
        finally:
            reopened_d2.close(); reopened_c_store.close()
            reopened_b_store.close(); reopened_a.close(); reopened_incidents.close()
    assert first_provider_calls <= 1
    assert all(calls <= 1 for calls in first_source_calls)


# These helpers reopen public authorities, never retain a D result or A view.
def _execution_setup(path, provider_transform=None):
    from dataclasses import replace
    from types import SimpleNamespace
    from test_llm_generation_phase2 import case as generation_case
    from test_llm_generation_service import environment
    from _knowledge_retrieval_testkit import request, limits
    from _incident_evidence_store_testkit import success
    from rca_persistence import CreateAggregateRequest, AdmitAttemptRequest, LogicalTryIdentity
    from runtime_orchestration.contracts import RuntimeWorkRecord, RuntimeWorkKind, RuntimeWorkStatus
    from runtime_orchestration.identity import runtime_work_id
    from runtime_orchestration.rca_continuation import RcaContinuation
    from test_spec_016_attempt_retry import NOW as execution_time
    path.mkdir(parents=True, exist_ok=True)
    generator = generation_case.__wrapped__(path)
    data = next(generator)
    lineage = data.attempts.get_attempt_lineage("attempt-1").attempt.lineage
    generator.close()  # close the original Knowledge authority before wiring
    c_store, c, snapshot = _execution_knowledge(path)
    lineage = replace(lineage, knowledge_snapshot_id=snapshot.snapshot_key.value,
                      aggregate_id="aggregate:execution", attempt_id="attempt:execution")
    data.content = replace(data.content, input=replace(data.content.input,
        knowledge_snapshot_id=snapshot.snapshot_key.value, knowledge_resolution=snapshot.resolution,
        try_identity=LogicalTryIdentity(lineage.attempt_id, 1)))
    a = SqliteRcaStore(path / "a.sqlite3")
    a.create_or_discover_aggregate(CreateAggregateRequest("aggregate-setup", lineage.aggregate_id, "INC-1", execution_time))
    a.admit_attempt(AdmitAttemptRequest("attempt-setup", lineage, execution_time))
    b = SqliteEvidenceStore(path / "b.sqlite3")
    b.commit_success(success())
    data.attempts, data.evidence, data.knowledge = a, b, c
    service, provider, _, resources = environment(data, path / "d")
    if provider_transform is not None:
        service.provider = provider_transform(provider)
    from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore
    work = SqliteRuntimeWorkStore(path / "runtime.sqlite3")
    root = rca_root_id("INC-1")
    work_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
    work.create(RuntimeWorkRecord(work_id, RuntimeWorkKind.RCA_INITIAL, "EVT-1", "INITIAL", "INITIAL", 0, 4,
        RuntimeWorkStatus.OUTSTANDING, execution_time, execution_time, execution_time,
        incident_id="INC-1", operation_id=root))
    d2 = SqliteRcaContinuationStore(path / "runtime.sqlite3")
    d2.create(RcaContinuation(root, work_id, "INC-1", "EXECUTION", "EXECUTION", 4,
        execution_time, execution_time, execution_time,
        aggregate_id=lineage.aggregate_id, attempt_id=lineage.attempt_id))
    return _execution_instances(path, a, b, c_store, c, work, d2, service, provider)


def _execution_knowledge(path):
    from pathlib import Path
    import json
    from knowledge_index import (admit_production_manifest, plan_chunks, load_knowledge_config,
        BuildIdentityInput, KnowledgeBuildService, BuildOperationKey, BuildActivationRequest,
        ActivationOperationKey)
    from _knowledge_build_testkit import DeterministicProvider, DeterministicIndex, capability, limits
    from _knowledge_retrieval_testkit import request
    repository = Path(__file__).resolve().parents[1]
    config = load_knowledge_config(repository / "configs/knowledge_index.yaml")
    admission = admit_production_manifest("configs/knowledge_manifest.json", repository_root=repository)
    chunks = plan_chunks(admission, source_root=repository / "docs/knowledge", profile=config.chunking)
    cap = capability()
    identity = BuildIdentityInput(admission.manifest_commitment, tuple(x.chunk_identity for x in chunks),
        config.chunking.profile_identity, "1.0", cap.provider, cap.model, cap.embedding_profile_identity,
        cap.embedding_dimension, config.normalization_semantics, "chroma", "index-schema-v1",
        "spec014-knowledge-metadata-v1", "spec014-build-contract-v2")
    store = SqliteKnowledgeStore(path / "c.sqlite3")
    index = DeterministicIndex()
    build = KnowledgeBuildService(store, DeterministicProvider(), index)
    staged = build.stage(operation_key=BuildOperationKey("s7-build"),
        raw_manifest=json.loads((repository / "configs/knowledge_manifest.json").read_text(encoding="utf-8")),
        source_root=repository / "docs/knowledge", chunks=chunks, identity_input=identity,
        limits=limits(maximum_request_bytes=1000000, maximum_batch_items=1000),
        required_capability_identity=cap.capability_identity)
    assert staged.record is not None
    validation = build.validate(staged.record.build_identity, BuildOperationKey("s7-validate"), maximum_probe_results=100)
    assert not validation.findings
    build.activate(BuildActivationRequest(staged.record.build_identity, ActivationOperationKey("s7-activate"), 0))
    service = KnowledgeSnapshotService(store, KnowledgeRetrievalService(store, QueryProvider(),
        RetrievalIndex(index.artifacts, candidates_for(staged.record, 0.1))))
    snapshot = service.resolve(request(), limits()).snapshot
    assert snapshot is not None
    assert service.read_provenance(snapshot.snapshot_key).value is not None
    return store, service, snapshot


def _execution_instances(path, a=None, b=None, c_store=None, c=None, work=None, d2=None, service=None, provider=None):
    from types import SimpleNamespace
    from llm_generation.config import load_generation_config
    from llm_generation.contracts import GenerationInput, LocalReadStatus
    from llm_generation.service import GenerationService, AvailableResources, canonical_evidence_projection, canonical_knowledge_projection
    from llm_generation.sqlite_store import CandidateDStore
    from llm_generation.facade import CandidateDHandoffFacade
    from knowledge_index import KnowledgeSnapshotKey
    from rca_persistence import LogicalTryIdentity
    from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore
    from runtime_orchestration.retry import DurableRetryController
    from runtime_orchestration.rca_execution import RcaAttemptExecutor, RcaExecutionRequest
    from test_spec_016_attempt_retry import _Time
    a = a or SqliteRcaStore(path / "a.sqlite3")
    b = b or SqliteEvidenceStore(path / "b.sqlite3")
    c_store = c_store or SqliteKnowledgeStore(path / "c.sqlite3")
    c = c or KnowledgeSnapshotService(c_store, KnowledgeRetrievalService(c_store,
        QueryProvider(error=AssertionError("unexpected Knowledge query")), RetrievalIndex({})))
    work = work or SqliteRuntimeWorkStore(path / "runtime.sqlite3")
    d2 = d2 or SqliteRcaContinuationStore(path / "runtime.sqlite3")
    config = load_generation_config(path / "d" / "generation.json")
    class NoProvider:
        calls = 0
        def __getattr__(self, name):
            raise AssertionError("recovery must not access generation provider")
    provider = provider or NoProvider()
    d = CandidateDStore(path / "d" / "candidate_d.sqlite", resource_config_path=path / "d" / "generation.json")
    assert d.local_readiness() is LocalReadStatus.FOUND
    service = service or GenerationService(d, provider, a, b, c)
    time = _Time()
    # Reopened process starts at the crash instant, never before the last
    # durable observation. This is only the deterministic test Clock input.
    time.value = max(row.updated_at for row in work.enumerate_all().records)
    retry = DurableRetryController(work_store=work, retry_delays_seconds=(1, 2, 4, 8), clock=time.clock())
    executor = RcaAttemptExecutor(candidate_a=a, candidate_d=service, d_store=d,
        handoff=CandidateDHandoffFacade(d, a, b, c), work_store=work, retry=retry,
        clock=time.clock(), continuations=d2)
    lineage = a.get_attempt_lineage("attempt:execution").attempt.lineage
    snapshot = b.resolve_snapshot(lineage.evidence_snapshot_id)
    key = KnowledgeSnapshotKey(lineage.knowledge_snapshot_id)
    knowledge = c.read_snapshot(key).value
    provenance = c.read_provenance(key).value
    source = GenerationInput(LogicalTryIdentity(lineage.attempt_id, 1), "operation-config",
        lineage.evidence_snapshot_id, lineage.evidence_revision_id, lineage.knowledge_snapshot_id,
        knowledge.resolution, config.pin, canonical_evidence_projection(snapshot),
        canonical_knowledge_projection(knowledge, provenance))
    request = RcaExecutionRequest("INC-1", lineage.attempt_id, source, config, AvailableResources(1, 1, 1, 1))
    return SimpleNamespace(a=a, b=b, c_store=c_store, c=c, d=d, work=work, d2=d2,
        service=service, provider=provider, executor=executor, request=request, time=time, retry=retry)


def _close_execution(env):
    env.d2.close(); env.work.close(); env.a.close(); env.b.close(); env.c_store.close()
    # CandidateDStore has per-operation connections; no retained connection.


@pytest.mark.parametrize("crash_point", ("d_result", "try_outcome", "artifact"))
def test_d_to_a_handoff_after_total_authority_reopen(tmp_path, crash_point):
    from runtime_orchestration.rca_execution import RcaExecutionDisposition
    from runtime_orchestration.identity import runtime_work_id
    from runtime_orchestration.contracts import RuntimeWorkKind
    from llm_generation.contracts import LocalReadStatus
    from llm_generation.service import ProviderInvocationError, ProviderFailureKind
    from llm_generation.facade import CandidateDHandoffFacade
    env = _execution_setup(tmp_path)
    successful_response = env.provider.outcome
    env.provider.outcome = ProviderInvocationError(ProviderFailureKind.TIMEOUT)
    pending = env.executor.step(env.request)
    assert pending.disposition is RcaExecutionDisposition.RETRY_PENDING
    original_eligibility = pending.next_eligibility_at
    env.time.advance(1)
    assert env.time.value == original_eligibility
    env.provider.outcome = successful_response
    original_execute = env.service.execute
    original_try = env.a.record_try_outcome
    original_artifact = env.a.commit_validated_artifact
    def execute(*args, **kwargs):
        result = original_execute(*args, **kwargs)
        if crash_point == "d_result":
            assert result.result is not None
            raise _Crash
        return result
    def record_try(*args, **kwargs):
        result = original_try(*args, **kwargs)
        if crash_point == "try_outcome":
            assert env.a.get_version_history("aggregate:execution") == ()
            raise _Crash
        return result
    def artifact(*args, **kwargs):
        result = original_artifact(*args, **kwargs)
        if crash_point == "artifact":
            raise _Crash
        return result
    env.service.execute = execute
    env.a.record_try_outcome = record_try
    env.a.commit_validated_artifact = artifact
    try:
        with pytest.raises(_Crash):
            env.executor.step(env.request)
        assert len(env.provider.calls) == 2  # one timeout and one authorized retry
        before_lineage = env.a.get_attempt_lineage("attempt:execution").attempt.lineage
        budget_id = runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT, rca_root_id("INC-1"), "attempt:execution")
        budget = env.work.get(budget_id)
        assert budget.attempt_count == 1
        assert budget.last_attempt_at == original_eligibility - timedelta(seconds=1)
        durable_result_id = env.d.recovery().value.results[0].validated_result_id
        assert len(env.a.get_attempt_lineage("attempt:execution").try_outcomes) == (0 if crash_point == "d_result" else 1)
        assert len(env.a.get_version_history("aggregate:execution")) == (1 if crash_point == "artifact" else 0)
    finally:
        _close_execution(env)
    del env  # discard executor, services, connection objects and result views
    versions = []
    for _ in range(3):
        fresh = _execution_instances(tmp_path)
        try:
            recovered = fresh.executor.step(fresh.request)
            assert recovered.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
            view = fresh.a.get_attempt_lineage("attempt:execution")
            assert view.attempt.lineage == before_lineage
            assert len(view.try_outcomes) == 1
            assert view.try_outcomes[0].validated_result_id == durable_result_id
            history = fresh.a.get_version_history("aggregate:execution")
            assert len(history) == 1
            projection = CandidateDHandoffFacade(fresh.d, fresh.a, fresh.b, fresh.c).resolve(durable_result_id)
            assert projection.status is LocalReadStatus.FOUND
            assert history[0].artifact == projection.artifact
            versions.append((history[0].version_id, history[0].publication_operation_id))
            current_budget = fresh.work.get(budget_id)
            assert (current_budget.attempt_count, current_budget.last_attempt_at, current_budget.next_retry_at) == (
                budget.attempt_count, budget.last_attempt_at, budget.next_retry_at)
            assert fresh.provider.calls == 0
        finally:
            _close_execution(fresh)
    assert versions[0] == versions[1] == versions[2]


def _s2_publication_setup(path, *, early_snapshot=False):
    """Real S2 admission; only source, index and provider adapters are fake."""
    from dataclasses import replace
    from types import SimpleNamespace
    from test_llm_generation_phase2 import _config
    from test_spec_016_initial_composition import _request
    from _knowledge_retrieval_testkit import request, limits
    from runtime_orchestration.rca_initial import InitialRcaRequest
    from runtime_orchestration.rca_continuation import rca_child_operation_id
    from runtime_orchestration.contracts import RuntimeWorkKind
    from runtime_orchestration.identity import runtime_work_id
    from rca_persistence import GenerationProvenance
    from knowledge_index import RetrievalOperationKey
    services = _public_services(path, event_id="EVT-PUBLICATION", source_marker="publication",
                               incident_id="INC-PUBLICATION")
    incidents, events, b, capture, adapters, a, c_store, c, provider = services
    c_store.close()
    c_store, c, _ = _execution_knowledge(path)
    services = (incidents, events, b, capture, adapters, a, c_store, c, provider)
    d2 = SqliteRcaContinuationStore(path / "runtime.sqlite3")
    config = _config()
    pin = config.pin
    basis, command, _ = _request(capture)
    basis = replace(basis, incident_id="INC-PUBLICATION", root_id=rca_root_id("INC-PUBLICATION"))
    command = replace(command, incident_id=basis.incident_id,
                      capture_operation_id=basis.capture_operation_id)
    if early_snapshot:
        basis = replace(basis, snapshot_at=NOW - timedelta(minutes=9))
        command = replace(command, capture_operation_id=basis.capture_operation_id,
                          snapshot_at=basis.snapshot_at)
    root = basis.root_id
    retrieval = replace(request(), operation_key=RetrievalOperationKey(
        rca_child_operation_id(root, basis.capture_operation_id, "KNOWLEDGE_RETRIEVAL")))
    initial = InitialRcaOrchestrator(incidents=incidents, candidate_a=a,
        candidate_b=capture, candidate_c=c, continuations=d2)
    result = initial.run(InitialRcaRequest("INC-PUBLICATION",
        runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root), 4, basis, NOW, command,
        retrieval, limits(), None, None, attempt_id="attempt:publication",
        generation_provenance=GenerationProvenance(pin.provider, pin.model,
            pin.prompt.identity, pin.configuration.identity, pin.profile.identity)))
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert result.continuation.stage == "EXECUTION"
    assert result.continuation.publication_operation_id is None
    return _s2_publication_instances(path, services=services, d2=d2, config=config)


def _s2_publication_instances(path, *, services=None, d2=None, config=None):
    from types import SimpleNamespace
    from dataclasses import replace
    from incident_management import SqliteIncidentStore, IncidentManager
    from knowledge_index import KnowledgeSnapshotKey
    from llm_generation.config import load_generation_config
    from llm_generation.contracts import (GenerationInput, Claim, EvidenceReference,
        Hypothesis, ResultContent, ValidationFact, InvocationMetadata)
    from llm_generation.validation import evidence_fact_commitment, observed_fact_text
    from llm_generation.service import GenerationService
    from llm_generation.sqlite_store import CandidateDStore
    from llm_generation.facade import CandidateDHandoffFacade
    from rca_persistence import LogicalTryIdentity, DiagnosticConclusion, EvidentialSupport, EvidenceCompleteness
    from rca_shared.claim_types import ClaimCategory
    from runtime_orchestration.rca_execution import RcaAttemptExecutor
    from runtime_orchestration.rca_followup import RcaFollowUpOrchestrator
    from runtime_orchestration.rca_publication import RcaPublicationOrchestrator
    from runtime_orchestration.rca_runtime_actions import RcaRuntimeActions
    from runtime_orchestration.rca_host import RcaRuntimeHost
    from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore
    from runtime_orchestration.retry import DurableRetryController
    from runtime_orchestration.telemetry import NullRuntimeTelemetry
    from rca_integration import RcaPublicationCoordinator
    from test_spec_016_attempt_retry import _Time
    from test_llm_generation_service import environment
    if services is None:
        incidents = SqliteIncidentStore(str(path / "incidents.sqlite3"))
        events = EventStore(str(path / "events.jsonl"))
        b, a = SqliteEvidenceStore(path / "evidence.sqlite3"), SqliteRcaStore(path / "rca.sqlite3")
        c_store = SqliteKnowledgeStore(path / "c.sqlite3")
        c = KnowledgeSnapshotService(c_store, KnowledgeRetrievalService(c_store,
            QueryProvider(error=AssertionError("recovery must not query Knowledge")), RetrievalIndex({})))
        adapters = {source: FakeAdapter(SourceStatus.AVAILABLE, marker="publication")
                    for source in EvidenceSource}
        capture = EvidenceCaptureService(store=b, incident_reader=incidents,
            event_reader=events, policy=load_evidence_policy("configs/incident_evidence.yaml"),
            adapters=adapters)
    else:
        incidents, events, b, capture, adapters, a, c_store, c, _ = services
    work = SqliteRuntimeWorkStore(path / "runtime.sqlite3")
    d2 = d2 or SqliteRcaContinuationStore(path / "runtime.sqlite3")
    time = _Time()
    time.value = max(row.updated_at for row in work.enumerate_all().records)
    retry = DurableRetryController(work_store=work, retry_delays_seconds=(1, 2, 4, 8), clock=time.clock())
    if config is not None:
        from llm_generation.service import canonical_evidence_projection, canonical_knowledge_projection
        lineage = a.get_attempt_lineage("attempt:publication").attempt.lineage
        evidence = b.resolve_snapshot(lineage.evidence_snapshot_id)
        key = KnowledgeSnapshotKey(lineage.knowledge_snapshot_id)
        knowledge, provenance = c.read_snapshot(key).value, c.read_provenance(key).value
        source = GenerationInput(LogicalTryIdentity(lineage.attempt_id, 1), "setup-generation",
            lineage.evidence_snapshot_id, lineage.evidence_revision_id, lineage.knowledge_snapshot_id,
            knowledge.resolution, config.pin, canonical_evidence_projection(evidence),
            canonical_knowledge_projection(knowledge, provenance))
        fact = evidence.snapshot_content["semantic_evidence"]["normalized_evidence"]["LOKI"][0]
        text = observed_fact_text(fact)
        content = ResultContent(source, "1", text, ("c1",), text, ("c1",),
            DiagnosticConclusion.INCONCLUSIVE,
            (Hypothesis(1, text, EvidentialSupport.LOW, ("e1",), (), (), text, ("c1",)),),
            (), (), ("Uncertain",), EvidenceCompleteness(evidence.completeness.value), True,
            (EvidenceReference("e1", evidence.snapshot_id, "1",
                "/semantic_evidence/normalized_evidence/LOKI/0", evidence_fact_commitment(fact)),),
            (), (Claim("c1", ClaimCategory.OBSERVED_FACT, text, ("e1",)),),
            (ValidationFact("schema", "1", config.result_schema_commitment),),
            InvocationMetadata(1, 10, 5, 15, 100))
        service, provider, _, _ = environment(SimpleNamespace(config=config, content=content,
            attempts=a, evidence=b, knowledge=c), path / "d")
        d = service.store
    else:
        config = load_generation_config(path / "d" / "generation.json")
        class NoProvider:
            calls = ()
            def __getattr__(self, name):
                raise AssertionError("recovery must not invoke the generation provider")
        provider = NoProvider()
        d = CandidateDStore(path / "d" / "candidate_d.sqlite", resource_config_path=path / "d" / "generation.json")
        service = GenerationService(d, provider, a, b, c)
    executor = RcaAttemptExecutor(candidate_a=a, candidate_d=service, d_store=d,
        handoff=CandidateDHandoffFacade(d, a, b, c), work_store=work, retry=retry,
        clock=time.clock(), continuations=d2)
    follow = RcaFollowUpOrchestrator(incidents=incidents, candidate_a=a, candidate_b=capture,
        candidate_c=c, continuations=d2, clock=time.clock())
    mutations = []
    manager = IncidentManager(incidents)
    mutate = manager.publish_rca_current
    def publish(command):
        mutations.append(command.publication_operation_id)
        return mutate(command)
    manager.publish_rca_current = publish
    coordinator = RcaPublicationCoordinator(a, manager, incidents)
    publication = RcaPublicationOrchestrator(coordinator, d2, work, follow, incidents, candidate_a=a)
    retrieval = request()
    knowledge_config = SimpleNamespace(retrieval_profile=retrieval.retrieval_profile,
        applicability_policy=retrieval.applicability_policy, limits=limits(),
        capability=SimpleNamespace(profile_reference=retrieval.profile_reference,
                                   capability_identity=retrieval.capability_identity))
    actions = RcaRuntimeActions(incidents=incidents, candidate_a=a, candidate_b=b,
        candidate_c=c, capture_service=capture, continuations=d2, initial=InitialRcaOrchestrator(
            incidents=incidents, candidate_a=a, candidate_b=capture, candidate_c=c, continuations=d2),
        execution=executor, follow_up=follow, publication=publication,
        evidence_policy=load_evidence_policy("configs/incident_evidence.yaml"),
        evidence_config_identity=d2.get(rca_root_id("INC-PUBLICATION")).capture_basis.configuration_identity,
        knowledge_config=knowledge_config, generation_config=config, retry_limit=4)
    host = RcaRuntimeHost(incidents=incidents, candidate_a=a, candidate_b=b,
        candidate_c=c_store, candidate_d=d, continuations=d2, actions=actions,
        clock=time.clock(), telemetry=NullRuntimeTelemetry(), runtime_work_store=work)
    return SimpleNamespace(a=a, b=b, c_store=c_store, c=c, incidents=incidents,
        d=d, work=work, d2=d2, service=service, provider=provider, executor=executor,
        follow=follow, publication=publication, actions=actions, host=host,
        coordinator=coordinator, mutations=mutations, adapters=adapters, time=time)


def _close_s2_publication(env):
    env.d2.close(); env.work.close(); env.c_store.close(); env.b.close(); env.a.close(); env.incidents.close()


def _advance_s2_execution(env):
    from runtime_orchestration.rca_host import RcaRecoverySubject, RcaRecoveryKind
    return env.actions.advance(RcaRecoverySubject("INC-PUBLICATION", RcaRecoveryKind.RETRY_PENDING,
        attempt_id="attempt:publication"), env.time.value)


def test_real_s2_production_execution_advances_exact_intent_and_completes_publication(tmp_path, monkeypatch):
    from runtime_orchestration.rca_publication import PublicationRuntimeDisposition
    from runtime_orchestration.contracts import RuntimeWorkStatus
    env = _s2_publication_setup(tmp_path)
    try:
        original = env.d2.advance_publication
        observed = []
        def verify(record, **kwargs):
            receipt = env.a.get_publication_result(record.publication_operation_id)
            assert receipt is not None
            assert env.a.get_artifact(receipt.target.target_version_id) is not None
            assert env.incidents.get_rca_publication_result(record.publication_operation_id) is None
            assert env.d2.get(record.root_id).stage == "EXECUTION"
            observed.append(receipt.target)
            return original(record, **kwargs)
        monkeypatch.setattr(env.d2, "advance_publication", verify)
        assert _advance_s2_execution(env)
        assert len(observed) == 1 and len(env.provider.calls) == 1
        state = env.d2.get(rca_root_id("INC-PUBLICATION"))
        assert state.stage == "PUBLICATION" and state.publication_operation_id == observed[0].publication_operation_id
        assert env.d2.get_follow_up_work(state.root_id).status is RuntimeWorkStatus.OUTSTANDING
        result = env.actions.reconcile_publication(state.publication_operation_id, env.time.value)
        assert result.disposition is PublicationRuntimeDisposition.COMPLETE
        assert result.work.status is RuntimeWorkStatus.COMPLETED
        assert env.mutations == [state.publication_operation_id]
        assert env.a.get_publication_result(state.publication_operation_id).target == observed[0]
        env.actions.reconcile_publication(state.publication_operation_id, env.time.value)
        assert len(env.mutations) == 1 and len(env.provider.calls) == 1
    finally:
        _close_s2_publication(env)


@pytest.mark.parametrize("business_state", ("a_committed", "008_applied", "both_applied"))
@pytest.mark.parametrize("advancement", ("before", "after"))
def test_publication_handoff_crash_full_reopen_recovers_before_dispatch(tmp_path, monkeypatch, business_state, advancement):
    from types import SimpleNamespace
    from incident_management import IncidentManager, IncidentRcaPublicationRequest
    from runtime_orchestration.contracts import RuntimeWorkStatus, RuntimeWorkKind
    from runtime_orchestration.rca_host import RcaRecoveryKind
    env = _s2_publication_setup(tmp_path)
    try:
        advance = env.d2.advance_publication
        def interrupt(*args, **kwargs):
            if advancement == "after": advance(*args, **kwargs)
            raise _Crash
        monkeypatch.setattr(env.d2, "advance_publication", interrupt)
        with pytest.raises(_Crash): _advance_s2_execution(env)
        state = env.d2.get(rca_root_id("INC-PUBLICATION"))
        assert state.stage == ("EXECUTION" if advancement == "before" else "PUBLICATION")
        assert (state.publication_operation_id is None) == (advancement == "before")
        versions = env.a.get_version_history(state.aggregate_id)
        assert len(versions) == 1 and len(env.provider.calls) == 1
        target = env.a.get_publication_result(versions[0].publication_operation_id).target
        if business_state == "008_applied":
            IncidentManager(env.incidents).publish_rca_current(IncidentRcaPublicationRequest(
                target.publication_operation_id, target.incident_id, target.target_version_id,
                target.expected_current_version_id, env.time.value))
        elif business_state == "both_applied":
            env.coordinator.reconcile(target.publication_operation_id, env.time.value, target=target)
        frozen_basis = state.capture_basis
    finally:
        _close_s2_publication(env)
    del env, versions, state
    for restart in range(3):
        fresh = _s2_publication_instances(tmp_path)
        try:
            monkeypatch.setattr(fresh.a, "commit_validated_artifact",
                lambda *args, **kwargs: pytest.fail("recovery must not recommit Artifact"))
            # Publication reconciliation occurs inside recovery; any renewed
            # provider/initial dispatch before it is a test failure.
            monkeypatch.setattr(fresh.actions, "advance",
                lambda *args, **kwargs: pytest.fail("coherent publication must recover before dispatch"))
            fresh.host.recover(SimpleNamespace(runtime_work=fresh.work.enumerate_all()))
            assert not any(item.kind is RcaRecoveryKind.REPAIR_REQUIRED for item in fresh.host.subjects)
            fresh.host.run_cycle(should_stop=lambda: False)
            latest = fresh.d2.get(rca_root_id("INC-PUBLICATION"))
            assert latest.stage == "PUBLICATION" and latest.publication_operation_id == target.publication_operation_id
            assert latest.capture_basis == frozen_basis
            assert fresh.d2.get_follow_up_work(latest.root_id).status is RuntimeWorkStatus.COMPLETED
            assert fresh.a.get_publication_result(target.publication_operation_id).target == target
            assert fresh.incidents.get_rca_publication_result(target.publication_operation_id).disposition.value == "APPLIED"
            assert len(fresh.a.get_version_history(target.aggregate_id)) == 1
            assert len(fresh.d.recovery().value.results) == 1 and len(fresh.provider.calls) == 0
            assert all(adapter.calls == 0 for adapter in fresh.adapters.values())
            assert fresh.mutations == ([target.publication_operation_id]
                if business_state == "a_committed" and restart == 0 else [])
            budgets = tuple(record for record in fresh.work.enumerate_all().records
                            if record.work_kind is RuntimeWorkKind.RCA_ATTEMPT)
            assert len(budgets) == 1 and budgets[0].status is RuntimeWorkStatus.COMPLETED
            assert budgets[0].attempt_count == 0
        finally:
            _close_s2_publication(fresh)


@pytest.mark.parametrize("contradiction", ("publication", "attempt", "aggregate", "target"))
def test_execution_publication_binding_contradiction_remains_repair_required(tmp_path, monkeypatch, contradiction):
    from dataclasses import replace
    from runtime_orchestration.rca_continuation import rca_child_operation_id
    from runtime_orchestration.rca_publication import PublicationRuntimeDisposition
    env = _s2_publication_setup(tmp_path)
    try:
        monkeypatch.setattr(env.d2, "advance_publication", lambda *args, **kwargs: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash): _advance_s2_execution(env)
        state = env.d2.get(rca_root_id("INC-PUBLICATION"))
        version = env.a.get_version_history(state.aggregate_id)[0]
        target = env.a.get_publication_result(version.publication_operation_id).target
        wrong = rca_child_operation_id(state.root_id, "contradictory-target", "PUBLICATION")
        changes = {"publication": {"publication_operation_id": wrong},
                   "attempt": {"attempt_id": "attempt:contradiction"},
                   "aggregate": {"aggregate_id": "aggregate:contradiction"}}
        if contradiction == "target":
            target = replace(target, expected_current_version_id="version:contradiction")
        else:
            env.d2.update(replace(state, **changes[contradiction]), expected_revision=state.revision)
        result = env.publication.reconcile(target, env.time.value)
        assert result.disposition is PublicationRuntimeDisposition.REPAIR_REQUIRED
        assert env.d2.get(state.root_id).publication_operation_id == (wrong if contradiction == "publication" else None)
        assert env.mutations == []
        assert env.d2.get_follow_up_work(state.root_id).status.value == "OUTSTANDING"
    finally:
        _close_s2_publication(env)


@pytest.mark.parametrize("race", ("frontier", "fence"))
def test_publication_advancement_cas_preserves_frontier_and_runtime_fence(tmp_path, monkeypatch, race):
    from dataclasses import replace
    from runtime_orchestration.rca_continuation import FollowUpRequirement
    env = _s2_publication_setup(tmp_path)
    try:
        root = rca_root_id("INC-PUBLICATION")
        initial = env.d2.get(root)
        snapshot = env.b.resolve_snapshot(env.a.get_attempt_lineage(initial.attempt_id).attempt.lineage.evidence_snapshot_id)
        member = FollowUpRequirement("POST_CONTEXT", "boundary:" + snapshot.snapshot_id)
        advance = env.d2.advance_publication
        calls = []
        def raced(record, **kwargs):
            calls.append(record.publication_operation_id)
            if len(calls) == 1:
                if race == "frontier":
                    assert env.follow.admit("INC-PUBLICATION", member).disposition.value == "OUTSTANDING"
                else:
                    work = env.d2.get_follow_up_work(root)
                    env.work.update(replace(work, next_action="RCA_RECOVERY_FENCE"), expected_revision=work.revision)
            return advance(record, **kwargs)
        monkeypatch.setattr(env.d2, "advance_publication", raced)
        if race == "frontier":
            assert _advance_s2_execution(env)
            latest = env.d2.get(root)
            assert latest.stage == "PUBLICATION" and member in latest.unresolved_frontier
            assert latest.admitted_frontier == (member,)
            assert len(calls) == 2 and calls[0] == calls[1]
        else:
            with pytest.raises(ValueError, match="execution requires repair"):
                _advance_s2_execution(env)
            latest = env.d2.get(root)
            assert latest.stage == "EXECUTION" and latest.publication_operation_id is None
            assert len(env.a.get_version_history(initial.aggregate_id)) == 1
        assert len(env.provider.calls) == 1 and env.mutations == []
        assert env.d2.get_follow_up_work(root).status.value == "OUTSTANDING"
    finally:
        _close_s2_publication(env)


def test_publication_continuation_never_advances_before_durable_a_artifact(tmp_path, monkeypatch):
    env = _s2_publication_setup(tmp_path)
    try:
        monkeypatch.setattr(env.a, "commit_validated_artifact",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("before A commit")))
        monkeypatch.setattr(env.d2, "advance_publication",
            lambda *args, **kwargs: pytest.fail("D2 cannot advance before durable A handoff"))
        with pytest.raises(ValueError, match="execution requires repair"):
            _advance_s2_execution(env)
        state = env.d2.get(rca_root_id("INC-PUBLICATION"))
        assert state.stage == "EXECUTION" and state.publication_operation_id is None
        assert env.a.get_version_history(state.aggregate_id) == ()
        assert env.d2.get_follow_up_work(state.root_id).status.value == "OUTSTANDING"
        assert env.mutations == []
    finally:
        _close_s2_publication(env)


def test_publication_completion_waits_for_real_authoritative_post_context_frontier(tmp_path):
    from runtime_orchestration.rca_publication import PublicationRuntimeDisposition
    env = _s2_publication_setup(tmp_path, early_snapshot=True)
    try:
        assert _advance_s2_execution(env)
        state = env.d2.get(rca_root_id("INC-PUBLICATION"))
        result = env.actions.reconcile_publication(state.publication_operation_id, env.time.value)
        assert result.disposition is PublicationRuntimeDisposition.OUTSTANDING
        state = env.d2.get(state.root_id)
        assert any(member.requirement_type == "POST_CONTEXT" for member in state.unresolved_frontier)
        assert env.a.get_publication_result(state.publication_operation_id).disposition.value == "APPLIED"
        assert env.d2.get_follow_up_work(state.root_id).status.value == "OUTSTANDING"
        # Complete B's boundary using the real capture path and real recheck;
        # suppress no frontier members or proofs in the fixture.
        env.actions._advance_follow_up("INC-PUBLICATION", env.time.value)
        result = env.actions.reconcile_publication(state.publication_operation_id, env.time.value)
        assert result.disposition is PublicationRuntimeDisposition.COMPLETE
        assert result.continuation.unresolved_frontier == ()
        assert all(proof.state == "VALIDATED" for proof in result.continuation.frontier_resolutions)
        assert len(env.mutations) == 1 and len(env.provider.calls) == 1
    finally:
        _close_s2_publication(env)


def test_real_material_refresh_advances_only_from_resolved_publication_predecessor(tmp_path):
    from dataclasses import replace
    from copy import deepcopy
    import json
    from alert_correlation import (AlertCorrelationPolicyEngine, CorrelationEvaluationContext,
        CorrelationEvaluationSuccess, EvaluationPhase)
    from alert_correlation.state import CorrelationMutationIntent
    from incident_management import IncidentManager, IncidentMutationRequest
    from runtime_orchestration.rca_host import RcaRecoveryKind, RcaRecoverySubject
    from test_spec_016_initial_composition import _event
    env = _s2_publication_setup(tmp_path)
    try:
        assert _advance_s2_execution(env)
        first = env.d2.get(rca_root_id("INC-PUBLICATION"))
        assert env.actions.reconcile_publication(first.publication_operation_id, env.time.value).disposition.value == "COMPLETE"
        event = {**_event(), "event_id": "EVT-PUBLICATION-NEW", "detected_at": "2026-09-29T11:51:00Z"}
        EventStore(str(tmp_path / "events.jsonl")).write(event)
        decision = AlertCorrelationPolicyEngine().evaluate(event,
            env.incidents.list_correlation_views(), CorrelationEvaluationContext(EvaluationPhase.INITIAL))
        assert isinstance(decision, CorrelationEvaluationSuccess)
        IncidentManager(env.incidents).apply_correlation_mutation(IncidentMutationRequest(
            CorrelationMutationIntent.from_decision(operation_id="append-publication-event",
                event_id=event["event_id"], decision=decision.decision, created_at=env.time.value),
            event, env.time.value))
        invoke = env.provider.invoke_once
        def grounded_response(request):
            response = invoke(request)
            output = deepcopy(response.structured_output)
            evidence = json.loads(json.loads(request.prompt)["evidence"])
            for reference in output["evidence_references"]:
                reference["evidence_snapshot_id"] = evidence["snapshot_id"]
            return replace(response, structured_output=output)
        env.provider.invoke_once = grounded_response
        for _ in range(6):
            state = env.d2.get(first.root_id)
            env.actions.advance(RcaRecoverySubject("INC-PUBLICATION", RcaRecoveryKind.FOLLOW_UP_PENDING), env.time.value)
            state = env.d2.get(first.root_id)
            if state.publication_operation_id != first.publication_operation_id:
                result = env.actions.reconcile_publication(state.publication_operation_id, env.time.value)
                if result.disposition.value == "COMPLETE": break
        assert result.disposition.value == "COMPLETE"
        target = env.a.get_publication_result(state.publication_operation_id).target
        prior = env.a.get_publication_result(first.publication_operation_id).target
        assert target.publication_operation_id != prior.publication_operation_id
        assert target.expected_current_version_id == prior.target_version_id
        assert len(env.a.get_version_history(first.aggregate_id)) == 2
        assert len(env.provider.calls) == 2 and len(env.mutations) == 2
        assert result.continuation.unresolved_frontier == ()
        assert env.d2.get_follow_up_work(first.root_id).status.value == "COMPLETED"
    finally:
        _close_s2_publication(env)
