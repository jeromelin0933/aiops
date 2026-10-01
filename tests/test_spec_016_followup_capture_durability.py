"""E1-C1 crash evidence through concrete Runtime dispatch and public A/B/D2."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from alert_correlation import (
    AlertCorrelationPolicyEngine, CorrelationEvaluationContext,
    CorrelationEvaluationSuccess, EvaluationPhase,
)
from alert_correlation.state import CorrelationMutationIntent
from event_detection.store.event_store import EventStore
from incident_evidence import (
    EvidenceCaptureService, EvidenceSource, SourceStatus, SqliteEvidenceStore,
    load_evidence_policy, effective_capture_config_identity,
    SourceAdmissionPolicy, DEFAULT_SOURCE_REQUEST_POLICIES,
)
from incident_management import IncidentManager, IncidentMutationRequest, SqliteIncidentStore
from rca_integration import RcaPublicationCoordinator
from rca_persistence import (
    AdmittedRetryDisposition, LogicalTryIdentity, LogicalTryOutcome,
    LogicalTryResultKind, PublicationTargetIdentity, SqliteRcaStore,
)
from runtime_orchestration.clock import RuntimeClock
from runtime_orchestration.rca_continuation import (
    ContradictoryRcaContinuationError, RcaContinuationRecordCorruptError,
    SqliteRcaContinuationStore, rca_root_id,
)
from runtime_orchestration.rca_initial import InitialRcaOrchestrator
from runtime_orchestration.rca_followup import RcaFollowUpOrchestrator, FollowUpDisposition
from runtime_orchestration.rca_publication import advance_publication_continuation
from runtime_orchestration.rca_host import RcaRecoveryKind, RcaRecoverySubject, RcaRuntimeHost
from runtime_orchestration.rca_runtime_actions import RcaRuntimeActions
from runtime_orchestration.telemetry import NullRuntimeTelemetry
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore
from runtime_orchestration.contracts import RuntimeWorkStatus

from test_incident_evidence_capture import FakeAdapter
from test_rca_persistence_version_artifact import artifact
from test_spec_016_initial_composition import _public_services, _run, _event, NOW


INCIDENT = "INC-S2"


class Crash(BaseException):
    pass


def _open(root):
    incidents = SqliteIncidentStore(str(root / "incidents.sqlite3"))
    a = SqliteRcaStore(root / "rca.sqlite3")
    b = SqliteEvidenceStore(root / "evidence.sqlite3")
    d2 = SqliteRcaContinuationStore(root / "runtime.sqlite3")
    policy = load_evidence_policy("configs/incident_evidence.yaml")
    adapters = {source: FakeAdapter(SourceStatus.AVAILABLE, marker="s2")
                for source in EvidenceSource}
    capture = EvidenceCaptureService(store=b, incident_reader=incidents,
        event_reader=EventStore(str(root / "events.jsonl")), policy=policy, adapters=adapters)
    clock = RuntimeClock(lambda: NOW, lambda: 0.0, lambda _: None)
    flow = RcaFollowUpOrchestrator(incidents=incidents, candidate_a=a,
        candidate_b=capture, candidate_c=object(), continuations=d2, clock=clock)
    actions = RcaRuntimeActions(incidents=incidents, candidate_a=a, candidate_b=b,
        candidate_c=object(), capture_service=capture, continuations=d2,
        initial=object(), execution=object(), follow_up=flow, publication=object(),
        evidence_policy=policy, evidence_config_identity=effective_capture_config_identity(
            policy, SourceAdmissionPolicy(), DEFAULT_SOURCE_REQUEST_POLICIES),
        knowledge_config=object(), generation_config=object(), retry_limit=3)
    # Keep these tests at the capture boundary; MATERIAL leaves its real E3
    # member outstanding until Knowledge is available.
    actions._knowledge_for_snapshot = lambda *args: None
    return SimpleNamespace(root=root, incidents=incidents, a=a, b=b, d2=d2,
        capture=capture, adapters=adapters, flow=flow, actions=actions, clock=clock)


def _close(env):
    env.d2.close(); env.b.close(); env.a.close(); env.incidents.close()


@pytest.fixture
def durable(tmp_path, request):
    incidents, _, b, capture, _, a, c, knowledge, _ = _public_services(tmp_path)
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    try:
        initial = _run(InitialRcaOrchestrator(incidents=incidents, candidate_a=a,
            candidate_b=capture, candidate_c=knowledge, continuations=d2), capture,
            basis_override=lambda basis: replace(basis, snapshot_at=NOW - timedelta(minutes=getattr(request, "param", 9))))
        lineage = initial.attempt.lineage
        a.record_try_outcome("capture-test-try", LogicalTryOutcome(
            LogicalTryIdentity(lineage.attempt_id, 1), LogicalTryResultKind.VALIDATED_RESULT,
            AdmittedRetryDisposition.NON_RETRYABLE, NOW, validated_result_id="capture-test-result"))
        base = artifact()
        value = replace(base, provenance=replace(base.provenance,
            evidence_snapshot_id=lineage.evidence_snapshot_id,
            evidence_revision_id=lineage.evidence_revision_id,
            knowledge_snapshot_id=lineage.knowledge_snapshot_id,
            generation=lineage.generation_provenance))
        target = PublicationTargetIdentity("publication:capture-test", lineage.aggregate_id,
            INCIDENT, "version:capture-test", None)
        a.commit_validated_artifact("capture-test-artifact", lineage.attempt_id, value, target, NOW)
        # Capture-boundary fixtures retain the same E handoff that the
        # production executor now durably advances before publication.
        advance_publication_continuation(a, d2, target, NOW)
        RcaPublicationCoordinator(a, IncidentManager(incidents), incidents).publish(
            target.publication_operation_id, NOW)
    finally:
        d2.close(); c.close(); a.close(); b.close(); incidents.close()
    env = _open(tmp_path)
    try:
        yield env
    finally:
        _close(env)


def _append(env, event_id):
    event = {**_event(), "event_id": event_id, "detected_at": "2026-09-29T11:51:00Z"}
    EventStore(str(env.root / "events.jsonl")).write(event)
    evaluated = AlertCorrelationPolicyEngine().evaluate(event,
        env.incidents.list_correlation_views(), CorrelationEvaluationContext(EvaluationPhase.INITIAL))
    assert isinstance(evaluated, CorrelationEvaluationSuccess)
    IncidentManager(env.incidents).apply_correlation_mutation(IncidentMutationRequest(
        CorrelationMutationIntent.from_decision(operation_id="append-" + event_id,
            event_id=event_id, decision=evaluated.decision, created_at=NOW), event, NOW))
    assert event_id in env.incidents.get_incident(INCIDENT).event_ids


def _dispatch(env):
    incident = env.incidents.get_incident(INCIDENT)
    observed = incident.last_correlated_at if len(incident.event_ids) > 1 else NOW
    return env.actions.advance(RcaRecoverySubject(INCIDENT,
        RcaRecoveryKind.FOLLOW_UP_PENDING), observed)


@pytest.mark.parametrize("path", ("new_event", "post_context"))
def test_real_dispatch_freezes_complete_basis_before_b(durable, monkeypatch, path):
    env = durable
    if path == "new_event": _append(env, "EVT-CAPTURE-2")
    original = env.capture.capture_evidence
    observed = []
    def verify(command):
        with SqliteRcaContinuationStore(env.root / "runtime.sqlite3") as reopened:
            basis = reopened.require_capture_command_basis(rca_root_id(INCIDENT), follow_up=True)
            assert basis.capture_operation_id == command.capture_operation_id
            assert basis.snapshot_at == command.snapshot_at
            assert basis.bounds_policy_version == command.bounds_policy_version
            assert basis.configuration_identity == command.config_identity
            assert basis.collection_boundary_references
            observed.append(basis)
        return original(command)
    monkeypatch.setattr(env.capture, "capture_evidence", verify)
    _dispatch(env)
    assert len(observed) == 1
    assert observed[0].snapshot_at == NOW - timedelta(minutes=9 if path == "new_event" else 8)
    state = env.d2.get(rca_root_id(INCIDENT))
    assert state.follow_up_capture_bases == tuple(observed)
    assert len(state.follow_up_capture_snapshots) == 1
    assert state.admitted_frontier
    if path == "new_event": assert state.unresolved_frontier


@pytest.mark.parametrize("path", ("new_event", "post_context"))
def test_freeze_crash_reopen_reuses_original_command_with_newer_incident(durable, monkeypatch, path):
    env = durable
    if path == "new_event": _append(env, "EVT-CAPTURE-2")
    def crash_before_b(command):
        assert env.d2.require_capture_command_basis(rca_root_id(INCIDENT), follow_up=True).capture_operation_id == command.capture_operation_id
        raise Crash
    monkeypatch.setattr(env.capture, "capture_evidence", crash_before_b)
    with pytest.raises(Crash): _dispatch(env)
    basis = env.d2.require_capture_command_basis(rca_root_id(INCIDENT), follow_up=True)
    assert all(adapter.calls == 0 for adapter in env.adapters.values())
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING
    _append(env, "EVT-CAPTURE-NEWER")
    root = env.root
    _close(env)
    reopened = _open(root)
    try:
        seen = []
        capture = reopened.capture.capture_evidence
        def observe(command):
            seen.append(command)
            return capture(command)
        monkeypatch.setattr(reopened.capture, "capture_evidence", observe)
        assert _dispatch(reopened)
        assert len(seen) == 1
        assert seen[0].capture_operation_id == basis.capture_operation_id
        assert seen[0].snapshot_at == basis.snapshot_at
        state = reopened.d2.get(rca_root_id(INCIDENT))
        assert state.follow_up_capture_bases == (basis,)
        assert len(state.follow_up_capture_snapshots) == 1
        assert state.unresolved_frontier
    finally:
        _close(reopened)


@pytest.mark.parametrize("interruption", ("b_response", "frontier_admission", "capture_resolution"))
def test_b_commit_reopens_same_receipt_without_second_capture(durable, monkeypatch, interruption):
    env = durable
    _append(env, "EVT-CAPTURE-2")
    if interruption == "b_response":
        original = env.capture.capture_evidence
        def crash(command):
            original(command)
            raise Crash
        monkeypatch.setattr(env.capture, "capture_evidence", crash)
    elif interruption == "frontier_admission":
        original = env.actions._admit_snapshot_requirements
        def crash_admission(incident_id, snapshot):
            state = env.d2.get(rca_root_id(INCIDENT))
            if state.follow_up_capture_bases and snapshot.capture_operation_id == state.follow_up_capture_bases[-1].capture_operation_id:
                raise Crash
            return original(incident_id, snapshot)
        monkeypatch.setattr(env.actions, "_admit_snapshot_requirements", crash_admission)
    else:
        monkeypatch.setattr(env.d2, "resolve_follow_up_capture",
            lambda *args, **kwargs: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash): _dispatch(env)
    basis = env.d2.require_capture_command_basis(rca_root_id(INCIDENT), follow_up=True)
    receipt = env.b.read_capture_outcome(basis.capture_operation_id)
    assert receipt is not None
    count = len(env.b.enumerate_recovery_facts().snapshots)
    _append(env, "EVT-CAPTURE-NEWER")
    root = env.root
    _close(env)
    reopened = _open(root)
    try:
        monkeypatch.setattr(reopened.capture, "capture_evidence",
            lambda *args: pytest.fail("committed capture must resolve its public receipt"))
        assert _dispatch(reopened)
        state = reopened.d2.get(rca_root_id(INCIDENT))
        assert state.follow_up_capture_bases == (basis,)
        assert state.follow_up_capture_snapshots == (receipt.snapshot_id,)
        assert len(reopened.b.enumerate_recovery_facts().snapshots) == count
        assert all(adapter.calls == 0 for adapter in reopened.adapters.values())
        assert any(member.reference == "revision:" + receipt.revision_id
                   for member in state.admitted_frontier)
    finally:
        _close(reopened)


def test_newer_event_admits_new_capture_and_frontier_without_rewriting_old(durable):
    env = durable
    _append(env, "EVT-CAPTURE-2")
    _dispatch(env)
    first = env.d2.get(rca_root_id(INCIDENT))
    _append(env, "EVT-CAPTURE-3")
    _dispatch(env)
    second = env.d2.get(rca_root_id(INCIDENT))
    assert second.follow_up_capture_bases[:1] == first.follow_up_capture_bases
    assert second.follow_up_capture_snapshots[:1] == first.follow_up_capture_snapshots
    assert len(second.follow_up_capture_bases) == 2
    assert second.follow_up_capture_bases[0].capture_operation_id != second.follow_up_capture_bases[1].capture_operation_id
    assert set(first.admitted_frontier).issubset(second.admitted_frontier)
    assert second.follow_up_root_id == first.follow_up_root_id


def test_pending_basis_blocks_completion_and_generic_rewrite(durable, monkeypatch):
    env = durable
    _append(env, "EVT-CAPTURE-2")
    monkeypatch.setattr(env.capture, "capture_evidence", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash): _dispatch(env)
    state = env.d2.get(rca_root_id(INCIDENT))
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING
    with pytest.raises(ContradictoryRcaContinuationError):
        env.d2.update(replace(state, follow_up_capture_bases=()), expected_revision=state.revision)
    work = env.d2.get_follow_up_work(state.root_id)
    with pytest.raises(ContradictoryRcaContinuationError):
        env.d2.complete_rca_follow_up_if_frontier_unchanged(state.root_id,
            expected_frontier_revision=state.revision, expected_work_revision=work.revision,
            observed_at=NOW)
    assert env.d2.get_follow_up_work(state.root_id).status is RuntimeWorkStatus.OUTSTANDING


@pytest.mark.parametrize("corruption", ("partial", "missing_both", "different_incident", "missing_bounds"))
def test_corrupt_required_basis_fails_closed_before_b(durable, monkeypatch, corruption):
    env = durable
    _append(env, "EVT-CAPTURE-2")
    monkeypatch.setattr(env.capture, "capture_evidence", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash): _dispatch(env)
    with sqlite3.connect(env.root / "runtime.sqlite3") as connection:
        payload = json.loads(connection.execute("SELECT record_json FROM rca_runtime_continuations").fetchone()[0])
        if corruption == "partial": del payload["follow_up_capture_bases"]
        elif corruption == "missing_both":
            del payload["follow_up_capture_bases"]
            del payload["follow_up_capture_snapshots"]
        elif corruption == "different_incident": payload["follow_up_capture_bases"][0]["incident_id"] = "INC-OTHER"
        else: payload["follow_up_capture_bases"][0]["bounds_policy_version"] = None
        connection.execute("UPDATE rca_runtime_continuations SET record_json=?", (json.dumps(payload),))
    with pytest.raises(RcaContinuationRecordCorruptError): _dispatch(env)
    assert all(adapter.calls == 0 for adapter in env.adapters.values())


def test_startup_classifies_pending_capture_before_normal_dispatch(durable, monkeypatch):
    from test_spec_016_runtime_host import _C, _D
    env = durable
    _append(env, "EVT-CAPTURE-2")
    monkeypatch.setattr(env.capture, "capture_evidence", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash): _dispatch(env)
    root = env.root
    basis = env.d2.require_capture_command_basis(rca_root_id(INCIDENT), follow_up=True)
    _close(env)
    reopened = _open(root)
    try:
        with SqliteRuntimeWorkStore(root / "runtime.sqlite3") as work:
            host = RcaRuntimeHost(incidents=reopened.incidents, candidate_a=reopened.a,
                candidate_b=reopened.b, candidate_c=_C(), candidate_d=_D(),
                continuations=reopened.d2, actions=reopened.actions,
                clock=reopened.clock, telemetry=NullRuntimeTelemetry(), runtime_work_store=work)
            host.recover(SimpleNamespace(runtime_work=work.enumerate_all()))
            assert any(subject.kind is RcaRecoveryKind.FOLLOW_UP_PENDING and
                       subject.stage == "CAPTURE" for subject in host.subjects)
            calls = []
            original = reopened.capture.capture_evidence
            def stop_after_capture(command):
                calls.append(command)
                result = original(command)
                stopped[0] = True
                return result
            stopped = [False]
            monkeypatch.setattr(reopened.capture, "capture_evidence", stop_after_capture)
            assert host.run_cycle(should_stop=lambda: stopped[0]).acquired_work == 1
            assert len(calls) == 1 and calls[0].capture_operation_id == basis.capture_operation_id
    finally:
        _close(reopened)


@pytest.mark.parametrize("committed", (False, True))
def test_config_change_never_replaces_frozen_command(durable, monkeypatch, committed):
    env = durable
    _append(env, "EVT-CAPTURE-2")
    original = env.capture.capture_evidence
    def interrupt(command):
        if committed: original(command)
        raise Crash
    monkeypatch.setattr(env.capture, "capture_evidence", interrupt)
    with pytest.raises(Crash): _dispatch(env)
    basis = env.d2.require_capture_command_basis(rca_root_id(INCIDENT), follow_up=True)
    env.actions._evidence_config_identity = "changed-config-v1"
    monkeypatch.setattr(env.capture, "capture_evidence", lambda *args: pytest.fail("changed config cannot invoke"))
    if committed:
        assert _dispatch(env)
    else:
        with pytest.raises(ValueError, match="configuration is unavailable"): _dispatch(env)
    assert env.d2.get(rca_root_id(INCIDENT)).follow_up_capture_bases == (basis,)


@pytest.mark.parametrize("durable", (0,), indirect=True)
def test_new_capture_reactivates_completed_initial_work_before_b(durable, monkeypatch):
    env = durable
    root = rca_root_id(INCIDENT)
    state = env.d2.get(root)
    work = env.d2.get_follow_up_work(root)
    env.d2.complete_rca_follow_up_if_frontier_unchanged(root,
        expected_frontier_revision=state.revision, expected_work_revision=work.revision,
        observed_at=NOW)
    assert state.follow_up_root_id is None
    _append(env, "EVT-CAPTURE-2")
    original = env.capture.capture_evidence
    def verify_active(command):
        with SqliteRcaContinuationStore(env.root / "runtime.sqlite3") as reopened:
            active = reopened.get_follow_up_work(root)
            assert active.status is RuntimeWorkStatus.OUTSTANDING
            assert active.work_id != state.runtime_work_id
            assert reopened.require_capture_command_basis(root, follow_up=True).capture_operation_id == command.capture_operation_id
        return original(command)
    monkeypatch.setattr(env.capture, "capture_evidence", verify_active)
    _dispatch(env)
    assert len(env.d2.get(root).follow_up_capture_snapshots) == 1


def test_existing_d2_extension_initialization_serializes_concurrent_openers(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with SqliteRuntimeWorkStore(path):
        pass
    barrier = Barrier(2)
    class ConcurrentStore(SqliteRcaContinuationStore):
        def _initialize(self):
            barrier.wait(timeout=10)
            return super()._initialize()
    def open_store(_):
        with ConcurrentStore(path) as store:
            return store.enumerate_all()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = tuple(workers.map(open_store, range(2)))
    assert all(not result.records and not result.isolated_corruptions for result in results)
    with sqlite3.connect(path) as connection:
        assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "runtime_work_store_metadata", "runtime_work_records",
            "rca_continuation_store_metadata", "rca_runtime_continuations"}
