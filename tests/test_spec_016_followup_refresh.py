"""S4: one durable typed frontier and public-authority material refresh."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest

from incident_evidence import (
    CaptureSuccess, EvidenceRevision, EvidenceSnapshot, MaterialityJudgement, SqliteEvidenceStore,
    compare_materiality,
)
from incident_management.contracts import IncidentStatus
from knowledge_index.contracts import KnowledgeReadStatus, RetrievalResolution
from rca_persistence import (
    AdmitAttemptRequest, AdmittedRetryDisposition, AttemptLineage,
    CreateAggregateRequest, CurrentFreshness, CurrentRca, GenerationProvenance,
    LogicalTryIdentity, LogicalTryOutcome, LogicalTryResultKind,
    PublicationDisposition, PublicationResult, PublicationTargetIdentity,
    SqliteRcaStore,
)
from runtime_orchestration.clock import RuntimeClock
from runtime_orchestration.rca_continuation import (
    ContradictoryRcaContinuationError, FollowUpRequirement,
    RcaContinuation, RcaContinuationConcurrencyError,
    SqliteRcaContinuationStore, rca_root_id,
)
from runtime_orchestration.rca_followup import FollowUpDisposition, RcaFollowUpOrchestrator
from runtime_orchestration.contracts import RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus
from runtime_orchestration.identity import runtime_work_id
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore

from _incident_evidence_store_testkit import success
from test_rca_persistence_version_artifact import artifact


NOW = datetime(2026, 9, 21, 12, 1, tzinfo=timezone.utc)
INCIDENT = "INC-1"
AGGREGATE = "aggregate:1"
VERSION = "version:1"
GEN = GenerationProvenance("provider", "model", "prompt", "config", "profile")
KNOWLEDGE_1 = "ksnp_" + "1" * 64
KNOWLEDGE_2 = "ksnp_" + "2" * 64
KNOWLEDGE_3 = "ksnp_" + "3" * 64


@dataclass
class _Incident:
    incident_id: str = INCIDENT
    status: IncidentStatus = IncidentStatus.OPEN
    event_ids: tuple[str, ...] = ("event-1",)


class _Incidents:
    def __init__(self):
        self.subject = _Incident()
    def validate_readiness(self): pass
    def list_correlation_views(self): return (SimpleNamespace(incident_id=INCIDENT),)
    def get_incident(self, incident_id): return self.subject
    def get_rca_relationship(self, incident_id):
        return SimpleNamespace(incident_id=INCIDENT, current_version_id=VERSION)


class _B:
    def __init__(self, path):
        self.store = SqliteEvidenceStore(path)
        self.requests = []
    def resolve_revision(self, revision_id): return self.store.resolve_revision(revision_id)
    def resolve_snapshot(self, snapshot_id): return self.store.resolve_snapshot(snapshot_id)
    def compare_materiality(self, request):
        self.requests.append(request)
        return compare_materiality(self.store, request)
    def close(self): self.store.close()


class _C:
    def read_snapshot(self, key):
        return SimpleNamespace(status=KnowledgeReadStatus.FOUND,
                               value=SimpleNamespace(snapshot_key=key,
                                                     resolution=RetrievalResolution.NO_MATCH))


class _Time:
    def __init__(self): self.now = NOW
    def clock(self): return RuntimeClock(lambda: self.now, lambda: 0.0, lambda _: None)


@pytest.fixture
def env(tmp_path):
    b_path = tmp_path / "b.sqlite3"
    b = _B(b_path)
    baseline = b.store.commit_success(success("capture-base", incident_id=INCIDENT,
                                              evidence="baseline"))
    material_a = b.store.commit_success(success("capture-a", incident_id=INCIDENT,
                                                evidence="material-a"))
    material_b = b.store.commit_success(success("capture-b", incident_id=INCIDENT,
                                                evidence="material-b"))
    a_path = tmp_path / "a.sqlite3"
    a = SqliteRcaStore(a_path)
    a.create_or_discover_aggregate(CreateAggregateRequest("OP-AGG", AGGREGATE, INCIDENT, NOW))
    initial = AttemptLineage("attempt:1", AGGREGATE, baseline.snapshot_id,
                             baseline.revision_id, KNOWLEDGE_1, GEN)
    a.admit_attempt(AdmitAttemptRequest("OP-attempt:1", initial, NOW))
    a.record_try_outcome("OP-TRY-1", LogicalTryOutcome(
        LogicalTryIdentity("attempt:1", 1), LogicalTryResultKind.VALIDATED_RESULT,
        AdmittedRetryDisposition.NON_RETRYABLE, NOW, validated_result_id="VALID-1"))
    base_artifact = artifact()
    adapted = replace(base_artifact, provenance=replace(base_artifact.provenance,
        evidence_snapshot_id=baseline.snapshot_id,
        evidence_revision_id=baseline.revision_id,
        knowledge_snapshot_id=KNOWLEDGE_1))
    target = PublicationTargetIdentity("PUB-1", AGGREGATE, INCIDENT, VERSION, None)
    a.commit_validated_artifact("OP-COMMIT-1", "attempt:1", adapted, target, NOW)
    a.complete_authorized_publication(PublicationResult(target,
        PublicationDisposition.APPLIED, NOW, VERSION))
    d2_path = tmp_path / "d2.sqlite3"
    d2 = SqliteRcaContinuationStore(d2_path)
    root = rca_root_id(INCIDENT)
    d2.create(RcaContinuation(root, runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root),
        INCIDENT, "EXECUTION", "EXECUTION", 4, NOW, NOW, NOW,
        aggregate_id=AGGREGATE, attempt_id="attempt:1"))
    with SqliteRuntimeWorkStore(d2_path) as work:
        work.create(RuntimeWorkRecord(
            runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root), RuntimeWorkKind.RCA_INITIAL,
            "EVT-1", "EXECUTION", "EXECUTION", 0, 4,
            RuntimeWorkStatus.OUTSTANDING, NOW, NOW, NOW,
            incident_id=INCIDENT, operation_id=root))
    incidents = _Incidents()
    time = _Time()
    coordinator = RcaFollowUpOrchestrator(incidents=incidents, candidate_a=a,
        candidate_b=b, candidate_c=_C(), continuations=d2, clock=time.clock())
    try:
        yield SimpleNamespace(a=a, a_path=a_path, b=b, b_path=b_path, d2=d2,
            d2_path=d2_path, incidents=incidents, time=time, flow=coordinator,
            baseline=baseline, material_a=material_a, material_b=material_b, root=root)
    finally:
        d2.close(); a.close(); b.close()


def _revision(outcome):
    return FollowUpRequirement("MATERIAL_EVIDENCE", "revision:" + outcome.revision_id)


def _post(outcome):
    return FollowUpRequirement("POST_CONTEXT", "boundary:" + outcome.snapshot_id)


def _completed_post_context(env):
    proposal = success("capture-post-complete", incident_id=INCIDENT, evidence="baseline")
    semantic = deepcopy(proposal.revision.semantic_content)
    content = deepcopy(proposal.snapshot.snapshot_content)
    end = "2026-09-21T12:02:00Z"
    for source in ("LOKI", "PROMETHEUS"):
        semantic["collection_boundaries"][source]["end"] = end
        content["windows"][source]["end"] = end
        content["provenance"][source]["query"]["logical_window"]["end"] = end
        content["provenance"][source]["collection"]["logical_window"]["end"] = end
        content["post_context"]["effective_window_ends"][source] = end
        content["post_context"]["reached_upper_boundary"][source] = True
    content["semantic_evidence"] = semantic
    content["post_context"]["snapshot_at"] = "2026-09-21T12:03:00Z"
    command = replace(proposal.command, snapshot_at=NOW + timedelta(minutes=2))
    revision = EvidenceRevision.from_content(
        incident_id=INCIDENT,
        canonicalization_version=proposal.revision.canonicalization_version,
        semantic_content=semantic,
    )
    snapshot = EvidenceSnapshot.from_content(command, revision_id=revision.revision_id,
        completeness=proposal.snapshot.completeness, source_statuses=proposal.snapshot.source_statuses,
        snapshot_content=content)
    return env.b.store.commit_success(CaptureSuccess(command, snapshot, revision))


def _advanced_post_context(env, operation_id, *, complete):
    proposal = success(operation_id, incident_id=INCIDENT, evidence="baseline")
    semantic = deepcopy(proposal.revision.semantic_content)
    content = deepcopy(proposal.snapshot.snapshot_content)
    boundary = "2026-09-21T12:02:30Z"
    end = boundary if complete else "2026-09-21T12:02:00Z"
    content["incident_projection"]["event_ids"] = ["event-1", "event-2"]
    content["event_projections"].append({"event_id": "event-2"})
    content["episode"]["end"] = "2026-09-21T12:00:30Z"
    content["post_context"]["episode_end"] = "2026-09-21T12:00:30Z"
    content["post_context"]["default_boundary"] = boundary
    for source in ("LOKI", "PROMETHEUS"):
        semantic["collection_boundaries"][source]["end"] = end
        content["windows"][source]["end"] = end
        content["provenance"][source]["query"]["logical_window"]["end"] = end
        content["provenance"][source]["collection"]["logical_window"]["end"] = end
        content["post_context"]["effective_window_ends"][source] = end
        content["post_context"]["reached_upper_boundary"][source] = complete
    content["semantic_evidence"] = semantic
    at = NOW + timedelta(minutes=2) if complete else NOW + timedelta(minutes=1)
    content["post_context"]["snapshot_at"] = at.isoformat().replace("+00:00", "Z")
    command = replace(proposal.command, snapshot_at=at)
    revision = EvidenceRevision.from_content(
        incident_id=INCIDENT,
        canonicalization_version=proposal.revision.canonicalization_version,
        semantic_content=semantic,
    )
    snapshot = EvidenceSnapshot.from_content(command, revision_id=revision.revision_id,
        completeness=proposal.snapshot.completeness, source_statuses=proposal.snapshot.source_statuses,
        snapshot_content=content)
    return env.b.store.commit_success(CaptureSuccess(command, snapshot, revision))


def test_new_event_advances_post_context_without_losing_older_member(env):
    old = _post(env.baseline)
    assert env.flow.admit(INCIDENT, old).disposition is FollowUpDisposition.OUTSTANDING
    old_wake = env.d2.get(env.root).next_eligibility_at
    env.incidents.subject.event_ids = ("event-1", "event-2")
    early = _advanced_post_context(env, "capture-boundary-b1-early", complete=False)
    newer = _post(early)
    assert env.flow.admit(INCIDENT, newer).disposition is FollowUpDisposition.OUTSTANDING
    state = env.d2.get(env.root)
    assert state.unresolved_frontier == (old, newer)
    assert state.follow_up_wake_at == old_wake
    assert state.next_eligibility_at == old_wake + timedelta(seconds=30)
    assert env.flow.recheck(INCIDENT, post_context_snapshots={old: early.snapshot_id}).disposition is FollowUpDisposition.OUTSTANDING
    assert env.d2.get(env.root).unresolved_frontier == (old, newer)
    reopened = SqliteRcaContinuationStore(env.d2_path)
    try:
        assert reopened.get(env.root).next_eligibility_at == old_wake + timedelta(seconds=30)
        assert reopened.get(env.root).admitted_frontier == (old, newer)
    finally:
        reopened.close()
    assert env.flow.admit(INCIDENT, newer).continuation.admitted_frontier == (old, newer)
    covered = _advanced_post_context(env, "capture-boundary-b1-covered", complete=True)
    result = env.flow.recheck(INCIDENT, post_context_snapshots={old: covered.snapshot_id,
                                                                 newer: covered.snapshot_id})
    assert result.disposition is FollowUpDisposition.COMPLETE
    assert result.continuation.unresolved_frontier == ()
    assert result.continuation.admitted_frontier == (old, newer)


def test_new_event_blocks_old_post_context_completion_before_new_admission(env):
    old = _post(env.baseline)
    env.flow.admit(INCIDENT, old)
    covered_old = _completed_post_context(env)
    env.incidents.subject.event_ids = ("event-1", "event-2")
    result = env.flow.recheck(INCIDENT, post_context_snapshots={old: covered_old.snapshot_id})
    assert result.disposition is FollowUpDisposition.OUTSTANDING
    assert old in result.continuation.unresolved_frontier


def test_same_evidence_does_not_discard_advanced_post_context(env):
    old = _post(env.baseline)
    env.flow.admit(INCIDENT, old)
    env.incidents.subject.event_ids = ("event-1", "event-2")
    early = _advanced_post_context(env, "capture-b1-same-evidence", complete=False)
    newer = _post(early)
    same = _revision(env.baseline)
    env.flow.admit(INCIDENT, newer)
    env.flow.admit(INCIDENT, same)
    result = env.flow.recheck(INCIDENT)
    assert result.disposition is FollowUpDisposition.OUTSTANDING
    assert same not in result.continuation.unresolved_frontier
    assert set(result.continuation.unresolved_frontier) == {old, newer}
    assert env.a.get_version_history(AGGREGATE)[-1].version_id == VERSION


def test_contradictory_advanced_window_fails_closed_and_preserves_members(env):
    old = _post(env.baseline)
    env.flow.admit(INCIDENT, old)
    env.incidents.subject.event_ids = ("event-1", "event-2")
    advanced = _advanced_post_context(env, "capture-b1-contradictory", complete=True)
    class ContradictoryB:
        def __getattr__(self, name): return getattr(env.b, name)
        def resolve_snapshot(self, snapshot_id):
            if snapshot_id != advanced.snapshot_id:
                return env.b.resolve_snapshot(snapshot_id)
            snapshot = env.b.resolve_snapshot(snapshot_id)
            content = deepcopy(snapshot.snapshot_content)
            content["post_context"]["effective_window_ends"]["LOKI"] = "2026-09-21T12:01:00Z"
            return EvidenceSnapshot.from_content(advanced.command, revision_id=advanced.revision_id,
                completeness=snapshot.completeness, source_statuses=snapshot.source_statuses,
                snapshot_content=content)
    flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
        candidate_b=ContradictoryB(), candidate_c=_C(), continuations=env.d2,
        clock=env.time.clock())
    assert flow.recheck(INCIDENT, post_context_snapshots={old: advanced.snapshot_id}).disposition is FollowUpDisposition.REPAIR_REQUIRED
    assert old in env.d2.get(env.root).unresolved_frontier


def test_multi_type_coalescing_and_restart_preserve_each_member_and_wake(env):
    members = (_revision(env.material_a), _revision(env.material_b), _post(env.baseline))
    for member in members:
        assert env.flow.admit(INCIDENT, member).disposition is FollowUpDisposition.OUTSTANDING
    original = env.d2.get(env.root)
    assert set(original.unresolved_frontier) == set(members)
    assert original.follow_up_root_id == env.flow.follow_up_root(INCIDENT)
    assert original.next_eligibility_at == NOW + timedelta(minutes=1)
    reopened = SqliteRcaContinuationStore(env.d2_path)
    try:
        assert reopened.get(env.root).unresolved_frontier == original.unresolved_frontier
        assert reopened.get(env.root).next_eligibility_at == original.next_eligibility_at
        env.time.now += timedelta(minutes=5)
        assert reopened.get(env.root).next_eligibility_at < env.time.now
        assert reopened.get(env.root).stage == "EXECUTION"  # Initial RCA was not delayed.
    finally:
        reopened.close()


def test_post_context_requires_fresh_b_coverage_and_full_frontier_recheck(env):
    post = _post(env.baseline)
    material = _revision(env.material_a)
    env.flow.admit(INCIDENT, post)
    env.flow.admit(INCIDENT, material)
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING
    completed = _completed_post_context(env)
    result = env.flow.recheck(INCIDENT, post_context_snapshots={post: completed.snapshot_id})
    assert result.disposition is FollowUpDisposition.OUTSTANDING
    assert result.continuation.unresolved_frontier == (material,)
    assert not result.continuation.follow_up_complete
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING


def test_post_context_completion_requires_b_coverage_and_survives_reopen(env):
    post = _post(env.baseline)
    env.flow.admit(INCIDENT, post)
    original_wake = env.d2.get(env.root).next_eligibility_at
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING
    completed = _completed_post_context(env)
    result = env.flow.recheck(INCIDENT, post_context_snapshots={post: completed.snapshot_id})
    assert result.disposition is FollowUpDisposition.COMPLETE
    assert result.continuation.unresolved_frontier == ()
    assert result.continuation.follow_up_complete
    assert result.continuation.next_eligibility_at is None
    assert result.continuation.follow_up_wake_at == original_wake
    reopened = SqliteRcaContinuationStore(env.d2_path)
    reopened_a = SqliteRcaStore(env.a_path)
    reopened_b = _B(env.b_path)
    try:
        assert reopened.get(env.root).follow_up_complete
        assert reopened.get(env.root).follow_up_root_id == env.flow.follow_up_root(INCIDENT)
        env.time.now += timedelta(minutes=5)
        restarted = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=reopened_a,
            candidate_b=reopened_b, candidate_c=_C(), continuations=reopened,
            clock=env.time.clock())
        replay = restarted.recheck(INCIDENT)
        assert replay.disposition is FollowUpDisposition.COMPLETE
        assert replay.continuation.frontier_resolutions[0].coverage_snapshot_id == completed.snapshot_id
        assert replay.continuation.follow_up_wake_at == original_wake
        assert replay.continuation.follow_up_root_id == env.flow.follow_up_root(INCIDENT)
    finally:
        reopened.close(); reopened_b.close(); reopened_a.close()


def test_due_post_context_recheck_retains_original_absolute_wake(env):
    post = _post(env.baseline)
    env.flow.admit(INCIDENT, post)
    original_wake = env.d2.get(env.root).next_eligibility_at
    env.time.now += timedelta(minutes=10)
    reopened = SqliteRcaContinuationStore(env.d2_path)
    reopened_b = _B(env.b_path)
    try:
        flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
            candidate_b=reopened_b, candidate_c=_C(), continuations=reopened,
            clock=env.time.clock())
        result = flow.recheck(INCIDENT)
        assert result.disposition is FollowUpDisposition.OUTSTANDING
        assert result.continuation.unresolved_frontier == (post,)
        assert result.continuation.next_eligibility_at == original_wake
        assert result.continuation.follow_up_wake_at == original_wake
        assert original_wake < env.time.now
    finally:
        reopened.close(); reopened_b.close()


def test_unreadable_post_context_authority_keeps_member_and_wake(env):
    post = _post(env.baseline)
    env.flow.admit(INCIDENT, post)
    original = env.d2.get(env.root)
    class UnreadableB:
        def __getattr__(self, name): return getattr(env.b, name)
        def resolve_snapshot(self, snapshot_id): return None
    flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
        candidate_b=UnreadableB(), candidate_c=_C(), continuations=env.d2,
        clock=env.time.clock())
    assert flow.recheck(INCIDENT).disposition is FollowUpDisposition.REPAIR_REQUIRED
    assert env.d2.get(env.root) == original


def test_unreadable_durable_post_context_coverage_proof_fails_closed(env):
    post = _post(env.baseline)
    env.flow.admit(INCIDENT, post)
    completed = _completed_post_context(env)
    assert env.flow.recheck(INCIDENT,
        post_context_snapshots={post: completed.snapshot_id}).disposition is FollowUpDisposition.COMPLETE
    original = env.d2.get(env.root)
    class UnreadableB:
        def __getattr__(self, name): return getattr(env.b, name)
        def resolve_snapshot(self, snapshot_id):
            return None if snapshot_id == completed.snapshot_id else env.b.resolve_snapshot(snapshot_id)
    reopened = SqliteRcaContinuationStore(env.d2_path)
    try:
        flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
            candidate_b=UnreadableB(), candidate_c=_C(), continuations=reopened,
            clock=env.time.clock())
        assert flow.recheck(INCIDENT).disposition is FollowUpDisposition.REPAIR_REQUIRED
        assert reopened.get(env.root) == original
    finally:
        reopened.close()


def test_concurrent_distinct_revisions_and_post_context_cas_lose_nothing(env):
    members = (_revision(env.material_a), _revision(env.material_b), _post(env.baseline))
    barrier = Barrier(3)
    def admit(member):
        a = SqliteRcaStore(env.a_path)
        b = _B(env.b_path)
        d2 = SqliteRcaContinuationStore(env.d2_path)
        try:
            flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=a,
                candidate_b=b, candidate_c=_C(), continuations=d2, clock=env.time.clock())
            barrier.wait(timeout=10)
            return flow.admit(INCIDENT, member).disposition
        finally:
            d2.close(); b.close(); a.close()
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert set(pool.map(admit, members)) == {FollowUpDisposition.OUTSTANDING}
    assert set(env.d2.get(env.root).unresolved_frontier) == set(members)


def test_generic_d2_update_cannot_discard_an_unresolved_member(env):
    member = _revision(env.material_a)
    env.flow.admit(INCIDENT, member)
    current = env.d2.get(env.root)
    with pytest.raises(ContradictoryRcaContinuationError):
        env.d2.update(replace(current, unresolved_frontier=()), expected_revision=current.revision)
    assert env.d2.get(env.root).unresolved_frontier == (member,)


def test_completion_cas_rechecks_a_concurrently_admitted_member(env):
    covered = _revision(env.baseline)
    arriving = _revision(env.material_a)
    env.flow.admit(INCIDENT, covered)
    class RacingD2:
        fired = False
        def __getattr__(self, name): return getattr(env.d2, name)
        def resolve_follow_up(self, record, *, expected_revision):
            if not self.fired:
                self.fired = True
                assert env.flow.admit(INCIDENT, arriving).disposition is FollowUpDisposition.OUTSTANDING
            return env.d2.resolve_follow_up(record, expected_revision=expected_revision)
    flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
        candidate_b=env.b, candidate_c=_C(), continuations=RacingD2(),
        clock=env.time.clock())
    checked = flow.recheck(INCIDENT)
    assert checked.disposition is FollowUpDisposition.OUTSTANDING
    assert checked.continuation.unresolved_frontier == (arriving,)
    assert not checked.continuation.follow_up_complete


def test_completion_cas_conflict_rereads_changed_current_basis(env):
    covered = _revision(env.baseline)
    first = _revision(env.material_a)
    env.flow.admit(INCIDENT, covered)
    env.flow.admit(INCIDENT, first)
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                             env.material_a.revision_id, KNOWLEDGE_2, GEN)
    class RacingD2:
        fired = False
        def __getattr__(self, name): return getattr(env.d2, name)
        def resolve_follow_up(self, record, *, expected_revision):
            if not self.fired:
                self.fired = True
                assert env.flow.refresh(INCIDENT, first,
                    evidence_snapshot_id=env.material_a.snapshot_id,
                    knowledge_snapshot_id=KNOWLEDGE_2,
                    lineage=lineage).disposition is FollowUpDisposition.ATTEMPT_ADMITTED
            return env.d2.resolve_follow_up(record, expected_revision=expected_revision)
    flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
        candidate_b=env.b, candidate_c=_C(), continuations=RacingD2(),
        clock=env.time.clock())
    checked = flow.recheck(INCIDENT)
    assert checked.disposition is FollowUpDisposition.OUTSTANDING
    assert set(checked.continuation.unresolved_frontier) >= {covered, first}
    assert [(r.baseline_revision_id, r.candidate_revision_id) for r in env.b.requests] == [
        (env.baseline.revision_id, env.baseline.revision_id),
        (env.baseline.revision_id, env.material_a.revision_id),
        (env.baseline.revision_id, env.material_a.revision_id),
        (env.material_a.revision_id, env.baseline.revision_id),
        (env.material_a.revision_id, env.material_a.revision_id),
        (env.baseline.revision_id, env.material_a.revision_id),
    ]
    assert env.a.get_attempt_lineage("attempt:2") is not None
    assert len(env.a.get_version_history(AGGREGATE)) == 1


def test_each_revision_receives_direct_current_baseline_pairwise_judgement(env):
    members = (_revision(env.material_a), _revision(env.material_b))
    for member in members: env.flow.admit(INCIDENT, member)
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING
    assert {(r.baseline_revision_id, r.candidate_revision_id) for r in env.b.requests} == {
        (env.baseline.revision_id, env.material_a.revision_id),
        (env.baseline.revision_id, env.material_b.revision_id),
    }
    assert len(env.d2.get(env.root).unresolved_frontier) == 2


def test_same_and_non_material_suppress_attempt_and_version(env):
    same = _revision(env.baseline)
    env.flow.admit(INCIDENT, same)
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.baseline.snapshot_id,
                             env.baseline.revision_id, KNOWLEDGE_2, GEN)
    result = env.flow.refresh(INCIDENT, same, evidence_snapshot_id=env.baseline.snapshot_id,
                              knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage)
    assert result.disposition is FollowUpDisposition.SUPPRESSED
    assert result.materiality.judgement is MaterialityJudgement.SAME
    assert env.a.get_attempt_lineage("attempt:2") is None
    assert len(env.a.get_version_history(AGGREGATE)) == 1
    assert env.a.get_current(AGGREGATE).current.freshness is CurrentFreshness.FRESH
    assert env.d2.get(env.root).unresolved_frontier == ()


def test_distinct_non_material_revision_is_directly_suppressed(env):
    proposal = success("capture-non-material", incident_id=INCIDENT, evidence="baseline")
    semantic = deepcopy(proposal.revision.semantic_content)
    content = deepcopy(proposal.snapshot.snapshot_content)
    earlier = "2026-09-21T11:57:00Z"
    for source in ("LOKI", "PROMETHEUS"):
        semantic["collection_boundaries"][source]["start"] = earlier
        content["windows"][source]["start"] = earlier
        content["provenance"][source]["query"]["logical_window"]["start"] = earlier
        content["provenance"][source]["collection"]["logical_window"]["start"] = earlier
    content["semantic_evidence"] = semantic
    revision = EvidenceRevision.from_content(
        incident_id=INCIDENT,
        canonicalization_version=proposal.revision.canonicalization_version,
        semantic_content=semantic,
    )
    snapshot = EvidenceSnapshot.from_content(proposal.command, revision_id=revision.revision_id,
        completeness=proposal.snapshot.completeness, source_statuses=proposal.snapshot.source_statuses,
        snapshot_content=content)
    outcome = env.b.store.commit_success(CaptureSuccess(proposal.command, snapshot, revision))
    assert outcome.revision_id != env.baseline.revision_id
    member = _revision(outcome)
    env.flow.admit(INCIDENT, member)
    lineage = AttemptLineage("attempt:2", AGGREGATE, outcome.snapshot_id,
                             outcome.revision_id, KNOWLEDGE_2, GEN)
    result = env.flow.refresh(INCIDENT, member, evidence_snapshot_id=outcome.snapshot_id,
                              knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage)
    assert result.disposition is FollowUpDisposition.SUPPRESSED
    assert result.materiality.judgement is MaterialityJudgement.NON_MATERIAL
    assert env.a.get_attempt_lineage("attempt:2") is None
    assert len(env.a.get_version_history(AGGREGATE)) == 1
    assert env.d2.get(env.root).follow_up_complete


def test_material_stale_freeze_then_attempt_and_last_known_good(env):
    member = _revision(env.material_a)
    env.flow.admit(INCIDENT, member)
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                             env.material_a.revision_id, KNOWLEDGE_2, GEN)
    result = env.flow.refresh(INCIDENT, member,
        evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage)
    assert result.disposition is FollowUpDisposition.ATTEMPT_ADMITTED
    assert result.materiality.judgement is MaterialityJudgement.MATERIAL
    assert env.a.get_current(AGGREGATE).current.freshness is CurrentFreshness.STALE
    assert env.a.get_current(AGGREGATE).version.version_id == VERSION
    assert env.a.get_current(AGGREGATE).artifact == env.a.get_artifact(VERSION)
    assert env.a.get_attempt_lineage("attempt:2").attempt.lineage == lineage
    assert env.d2.get(env.root).refresh_basis.evidence_revision_id == env.material_a.revision_id
    assert FollowUpRequirement("STALE_REFRESH", "version:" + VERSION) in env.d2.get(env.root).unresolved_frontier
    assert len(env.a.get_version_history(AGGREGATE)) == 1


def test_simultaneous_material_revisions_keep_second_member_and_compare_directly(env):
    first = _revision(env.material_a)
    second = _revision(env.material_b)
    env.flow.admit(INCIDENT, first)
    env.flow.admit(INCIDENT, second)
    first_lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                                   env.material_a.revision_id, KNOWLEDGE_2, GEN)
    assert env.flow.refresh(INCIDENT, first, evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2, lineage=first_lineage).disposition is FollowUpDisposition.ATTEMPT_ADMITTED
    second_lineage = AttemptLineage("attempt:3", AGGREGATE, env.material_b.snapshot_id,
                                    env.material_b.revision_id, KNOWLEDGE_3, GEN)
    pending = env.flow.refresh(INCIDENT, second, evidence_snapshot_id=env.material_b.snapshot_id,
                               knowledge_snapshot_id=KNOWLEDGE_3, lineage=second_lineage)
    assert pending.disposition is FollowUpDisposition.WAITING
    assert pending.materiality.request.baseline_revision_id == env.material_a.revision_id
    assert pending.materiality.request.candidate_revision_id == env.material_b.revision_id
    assert [(r.baseline_revision_id, r.candidate_revision_id) for r in env.b.requests] == [
        (env.baseline.revision_id, env.material_a.revision_id),
        (env.material_a.revision_id, env.material_b.revision_id),
    ]
    assert env.a.get_attempt_lineage("attempt:3") is None
    assert len(env.a.get_version_history(AGGREGATE)) == 1
    assert {item.reference for item in env.d2.get(env.root).unresolved_frontier if
            item.requirement_type == "MATERIAL_EVIDENCE"} == {first.reference, second.reference}


def test_current_changes_during_pairwise_then_retries_second_against_new_basis(env):
    first, second = _revision(env.material_a), _revision(env.material_b)
    env.flow.admit(INCIDENT, first)
    env.flow.admit(INCIDENT, second)
    first_lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                                   env.material_a.revision_id, KNOWLEDGE_2, GEN)
    second_lineage = AttemptLineage("attempt:3", AGGREGATE, env.material_b.snapshot_id,
                                    env.material_b.revision_id, KNOWLEDGE_3, GEN)
    class RacingB:
        fired = False
        def __getattr__(self, name): return getattr(env.b, name)
        def compare_materiality(self, request):
            if request.candidate_revision_id == env.material_b.revision_id and not self.fired:
                self.fired = True
                assert env.flow.refresh(INCIDENT, first,
                    evidence_snapshot_id=env.material_a.snapshot_id,
                    knowledge_snapshot_id=KNOWLEDGE_2,
                    lineage=first_lineage).disposition is FollowUpDisposition.ATTEMPT_ADMITTED
            return env.b.compare_materiality(request)
    flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
        candidate_b=RacingB(), candidate_c=_C(), continuations=env.d2,
        clock=env.time.clock())
    result = flow.refresh(INCIDENT, second, evidence_snapshot_id=env.material_b.snapshot_id,
                          knowledge_snapshot_id=KNOWLEDGE_3, lineage=second_lineage)
    assert result.disposition is FollowUpDisposition.WAITING
    assert [(r.baseline_revision_id, r.candidate_revision_id) for r in env.b.requests] == [
        (env.baseline.revision_id, env.material_a.revision_id),
        (env.baseline.revision_id, env.material_b.revision_id),
        (env.material_a.revision_id, env.material_b.revision_id),
    ]
    assert set(env.d2.get(env.root).unresolved_frontier) >= {first, second}
    assert env.a.get_attempt_lineage("attempt:3") is None
    assert len(env.a.get_version_history(AGGREGATE)) == 1


def test_restart_keeps_both_revisions_and_compares_second_against_stale_current(env):
    first, second = _revision(env.material_a), _revision(env.material_b)
    env.flow.admit(INCIDENT, first)
    env.flow.admit(INCIDENT, second)
    first_lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                                   env.material_a.revision_id, KNOWLEDGE_2, GEN)
    assert env.flow.refresh(INCIDENT, first, evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2,
        lineage=first_lineage).disposition is FollowUpDisposition.ATTEMPT_ADMITTED
    reopened_a = SqliteRcaStore(env.a_path)
    reopened_b = _B(env.b_path)
    reopened_d2 = SqliteRcaContinuationStore(env.d2_path)
    try:
        restarted = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=reopened_a,
            candidate_b=reopened_b, candidate_c=_C(), continuations=reopened_d2,
            clock=env.time.clock())
        assert set(reopened_d2.get(env.root).unresolved_frontier) >= {first, second}
        second_lineage = AttemptLineage("attempt:3", AGGREGATE, env.material_b.snapshot_id,
                                        env.material_b.revision_id, KNOWLEDGE_3, GEN)
        pending = restarted.refresh(INCIDENT, second,
            evidence_snapshot_id=env.material_b.snapshot_id,
            knowledge_snapshot_id=KNOWLEDGE_3, lineage=second_lineage)
        assert pending.disposition is FollowUpDisposition.WAITING
        assert [(r.baseline_revision_id, r.candidate_revision_id) for r in reopened_b.requests] == [
            (env.material_a.revision_id, env.material_b.revision_id),
        ]
        assert restarted.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING
        assert set(reopened_d2.get(env.root).unresolved_frontier) >= {first, second}
        assert reopened_a.get_attempt_lineage("attempt:3") is None
        assert len(reopened_a.get_version_history(AGGREGATE)) == 1
    finally:
        reopened_d2.close(); reopened_b.close(); reopened_a.close()


def test_stale_read_and_basis_freeze_precede_attempt_admission(env):
    member = _revision(env.material_a)
    env.flow.admit(INCIDENT, member)
    order = []
    class A:
        def __getattr__(self, name): return getattr(env.a, name)
        def apply_authorized_freshness(self, fact):
            order.append("stale")
            return env.a.apply_authorized_freshness(fact)
        def get_current(self, aggregate_id):
            current = env.a.get_current(aggregate_id)
            if current.current.freshness is CurrentFreshness.STALE:
                order.append("read-stale")
            return current
        def admit_attempt(self, request):
            assert env.d2.get(env.root).refresh_basis is not None
            order.append("admit")
            return env.a.admit_attempt(request)
    class B:
        def __getattr__(self, name): return getattr(env.b, name)
        def resolve_snapshot(self, snapshot_id):
            order.append("evidence")
            return env.b.resolve_snapshot(snapshot_id)
    class C:
        def read_snapshot(self, key):
            order.append("knowledge")
            return _C().read_snapshot(key)
    flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=A(),
        candidate_b=B(), candidate_c=C(), continuations=env.d2, clock=env.time.clock())
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                             env.material_a.revision_id, KNOWLEDGE_2, GEN)
    assert flow.refresh(INCIDENT, member, evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage).disposition is FollowUpDisposition.ATTEMPT_ADMITTED
    assert order.index("stale") < order.index("read-stale") < order.index("evidence")
    assert order.index("evidence") < order.index("knowledge") < order.index("admit")


def test_refresh_reopen_reuses_frozen_basis_and_one_attempt(env):
    member = _revision(env.material_a)
    env.flow.admit(INCIDENT, member)
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                             env.material_a.revision_id, KNOWLEDGE_2, GEN)
    first = env.flow.refresh(INCIDENT, member, evidence_snapshot_id=env.material_a.snapshot_id,
                             knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage)
    assert first.disposition is FollowUpDisposition.ATTEMPT_ADMITTED
    reopened_a = SqliteRcaStore(env.a_path)
    reopened_b = _B(env.b_path)
    reopened_d2 = SqliteRcaContinuationStore(env.d2_path)
    try:
        restarted = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=reopened_a,
            candidate_b=reopened_b, candidate_c=_C(), continuations=reopened_d2,
            clock=env.time.clock())
        second = restarted.refresh(INCIDENT, member,
            evidence_snapshot_id=env.material_a.snapshot_id,
            knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage)
        assert second.disposition is FollowUpDisposition.ATTEMPT_ADMITTED
        assert second.continuation.refresh_basis == first.continuation.refresh_basis
        assert reopened_a.get_attempt_lineage("attempt:2").attempt.lineage == lineage
        assert reopened_a.get_current(AGGREGATE).version.version_id == VERSION
        assert len(reopened_a.get_version_history(AGGREGATE)) == 1
    finally:
        reopened_d2.close(); reopened_b.close(); reopened_a.close()
    replay = env.flow.refresh(INCIDENT, member,
        evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage)
    assert replay.disposition is FollowUpDisposition.ATTEMPT_ADMITTED
    assert len(env.a.get_version_history(AGGREGATE)) == 1


def test_knowledge_unavailable_after_stale_keeps_last_known_good_and_replays(env):
    member = _revision(env.material_a)
    env.flow.admit(INCIDENT, member)
    class UnavailableC:
        def read_snapshot(self, key):
            return SimpleNamespace(status=KnowledgeReadStatus.UNAVAILABLE)
    blocked = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
        candidate_b=env.b, candidate_c=UnavailableC(), continuations=env.d2,
        clock=env.time.clock())
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                             env.material_a.revision_id, KNOWLEDGE_2, GEN)
    result = blocked.refresh(INCIDENT, member, evidence_snapshot_id=env.material_a.snapshot_id,
                             knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage)
    assert result.disposition is FollowUpDisposition.UNAVAILABLE
    assert env.a.get_current(AGGREGATE).current.freshness is CurrentFreshness.STALE
    assert env.a.get_current(AGGREGATE).version.version_id == VERSION
    assert env.a.get_attempt_lineage("attempt:2") is None
    assert env.d2.get(env.root).refresh_basis is None
    assert env.flow.refresh(INCIDENT, member, evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage).disposition is FollowUpDisposition.ATTEMPT_ADMITTED


@pytest.mark.parametrize("status", [IncidentStatus.AWAITING_REVIEW, IncidentStatus.CLOSED])
def test_lifecycle_rejects_new_automatic_refresh_but_preserves_known_work(env, status):
    member = _revision(env.material_a)
    env.flow.admit(INCIDENT, member)
    env.incidents.subject.status = status
    assert env.flow.admit(INCIDENT, _revision(env.material_b)).disposition is FollowUpDisposition.REPAIR_REQUIRED
    assert env.flow.admit(INCIDENT, member).disposition is FollowUpDisposition.OUTSTANDING
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                             env.material_a.revision_id, KNOWLEDGE_2, GEN)
    result = env.flow.refresh(INCIDENT, member, evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage)
    if status is IncidentStatus.CLOSED:
        assert result.disposition is FollowUpDisposition.REPAIR_REQUIRED
        assert env.a.get_current(AGGREGATE).current.freshness is CurrentFreshness.FRESH
    else:
        assert result.disposition is FollowUpDisposition.ATTEMPT_ADMITTED
        assert env.a.get_current(AGGREGATE).current.freshness is CurrentFreshness.STALE


@pytest.mark.parametrize("status", [IncidentStatus.ASSIGNED, IncidentStatus.IN_PROGRESS])
def test_active_lifecycle_admits_material_refresh(env, status):
    env.incidents.subject.status = status
    member = _revision(env.material_a)
    assert env.flow.admit(INCIDENT, member).disposition is FollowUpDisposition.OUTSTANDING
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                             env.material_a.revision_id, KNOWLEDGE_2, GEN)
    assert env.flow.refresh(INCIDENT, member, evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage).disposition is FollowUpDisposition.ATTEMPT_ADMITTED


def test_full_frontier_guard_and_parallel_root_rejection(env):
    first = _revision(env.material_a)
    env.flow.admit(INCIDENT, first)
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING
    assert env.d2.get(env.root).unresolved_frontier == (first,)
    state = env.d2.get(env.root)
    with pytest.raises(ContradictoryRcaContinuationError):
        env.d2.update(replace(state, follow_up_root_id="followup:parallel"),
                      expected_revision=state.revision)
    assert env.d2.get(env.root).follow_up_root_id == env.flow.follow_up_root(INCIDENT)


def test_preexisting_parallel_follow_up_root_fails_closed(env):
    state = env.d2.get(env.root)
    env.d2.update(replace(state, follow_up_root_id="followup:parallel"),
                  expected_revision=state.revision)
    member = _revision(env.material_a)
    assert env.flow.admit(INCIDENT, member).disposition is FollowUpDisposition.REPAIR_REQUIRED
    assert env.d2.get(env.root).unresolved_frontier == ()


def test_contradictory_stale_frontier_fails_closed(env):
    member = _revision(env.material_a)
    env.flow.admit(INCIDENT, member)
    lineage = AttemptLineage("attempt:2", AGGREGATE, env.material_a.snapshot_id,
                             env.material_a.revision_id, KNOWLEDGE_2, GEN)
    assert env.flow.refresh(INCIDENT, member, evidence_snapshot_id=env.material_a.snapshot_id,
        knowledge_snapshot_id=KNOWLEDGE_2, lineage=lineage).disposition is FollowUpDisposition.ATTEMPT_ADMITTED
    contradictory = FollowUpRequirement("STALE_REFRESH", "version:version:other")
    assert env.flow.admit(INCIDENT, contradictory).disposition is FollowUpDisposition.REPAIR_REQUIRED
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.OUTSTANDING


def test_preexisting_contradictory_frontier_is_not_completed(env):
    member = _revision(env.material_a)
    env.flow.admit(INCIDENT, member)
    state = env.d2.get(env.root)
    first = FollowUpRequirement("STALE_REFRESH", "version:version:one")
    second = FollowUpRequirement("STALE_REFRESH", "version:version:two")
    with pytest.raises(ContradictoryRcaContinuationError, match="atomic D2"):
        env.d2.update(replace(state, unresolved_frontier=state.unresolved_frontier + (first, second),
                              admitted_frontier=state.admitted_frontier + (first, second)),
                      expected_revision=state.revision)
    with sqlite3.connect(env.d2_path) as connection:
        row = connection.execute("SELECT record_json FROM rca_runtime_continuations WHERE root_id=?",
                                 (env.root,)).fetchone()
        payload = json.loads(row[0])
        members = [{"requirement_type": item.requirement_type, "reference": item.reference}
                   for item in (first, second)]
        payload["unresolved_frontier"].extend(members)
        payload["admitted_frontier"].extend(members)
        payload["revision"] = state.revision + 1
        connection.execute(
            "UPDATE rca_runtime_continuations SET record_json=?, revision=? WHERE root_id=?",
            (json.dumps(payload), payload["revision"], env.root))
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.REPAIR_REQUIRED
    assert len(env.d2.get(env.root).unresolved_frontier) == 3


def _change_same_version_current(env):
    env.a.apply_authorized_freshness(CurrentRca(
        AGGREGATE, VERSION, CurrentFreshness.STALE, env.material_b.revision_id))


@pytest.mark.parametrize("moment", ["before_cas", "after_cas", "crash_after_cas"])
def test_resolution_proof_survives_current_change_and_restart(env, moment):
    member = _revision(env.baseline)
    env.flow.admit(INCIDENT, member)

    class RacingD2:
        fired = False
        def __getattr__(self, name): return getattr(env.d2, name)
        def resolve_follow_up(self, record, *, expected_revision):
            if not self.fired and moment == "before_cas":
                self.fired = True
                _change_same_version_current(env)
            saved = env.d2.resolve_follow_up(record, expected_revision=expected_revision)
            if not self.fired and moment in ("after_cas", "crash_after_cas"):
                self.fired = True
                assert saved.frontier_resolutions[0].state == "PROVISIONAL"
                assert saved.admitted_frontier == (member,)
                _change_same_version_current(env)
                if moment == "crash_after_cas":
                    raise KeyboardInterrupt("simulated crash after D2 commit")
            return saved

    flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
        candidate_b=env.b, candidate_c=_C(), continuations=RacingD2(),
        clock=env.time.clock())
    if moment == "crash_after_cas":
        with pytest.raises(KeyboardInterrupt):
            flow.recheck(INCIDENT)
        reopened = SqliteRcaContinuationStore(env.d2_path)
        try:
            persisted = reopened.get(env.root)
            assert persisted.admitted_frontier == (member,)
            assert persisted.frontier_resolutions[0].state == "PROVISIONAL"
            flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
                candidate_b=env.b, candidate_c=_C(), continuations=reopened,
                clock=env.time.clock())
            checked = flow.recheck(INCIDENT)
        finally:
            reopened.close()
    else:
        checked = flow.recheck(INCIDENT)
    assert checked.disposition is FollowUpDisposition.OUTSTANDING
    persisted = env.d2.get(env.root)
    assert persisted.admitted_frontier == (member,)
    assert persisted.unresolved_frontier == (member,)
    assert not persisted.follow_up_complete
    assert (env.material_b.revision_id, env.baseline.revision_id) in [
        (request.baseline_revision_id, request.candidate_revision_id)
        for request in env.b.requests]
    assert len(env.a.get_version_history(AGGREGATE)) == 1


def test_unchanged_current_revalidates_terminal_proof_after_reopen(env):
    member = _revision(env.baseline)
    env.flow.admit(INCIDENT, member)
    completed = env.flow.recheck(INCIDENT)
    assert completed.disposition is FollowUpDisposition.COMPLETE
    assert completed.continuation.admitted_frontier == (member,)
    assert completed.continuation.frontier_resolutions[0].state == "VALIDATED"
    revision = completed.continuation.revision
    reopened = SqliteRcaContinuationStore(env.d2_path)
    try:
        flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
            candidate_b=env.b, candidate_c=_C(), continuations=reopened,
            clock=env.time.clock())
        assert flow.recheck(INCIDENT).disposition is FollowUpDisposition.COMPLETE
        assert reopened.get(env.root).revision == revision
    finally:
        reopened.close()
    assert len(env.a.get_version_history(AGGREGATE)) == 1


def test_terminal_bookkeeping_is_reopened_when_current_changes(env):
    member = _revision(env.baseline)
    env.flow.admit(INCIDENT, member)
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.COMPLETE
    assert env.d2.get(env.root).unresolved_frontier == ()
    _change_same_version_current(env)
    reopened = SqliteRcaContinuationStore(env.d2_path)
    try:
        flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
            candidate_b=env.b, candidate_c=_C(), continuations=reopened,
            clock=env.time.clock())
        result = flow.recheck(INCIDENT)
        assert result.disposition is FollowUpDisposition.OUTSTANDING
        assert result.continuation.unresolved_frontier == (member,)
        assert not result.continuation.follow_up_complete
    finally:
        reopened.close()


def test_s1_frontier_is_adopted_without_losing_member_or_early_resolution(env):
    member = _revision(env.material_a)
    state = env.d2.get(env.root)
    legacy = env.d2.update(replace(state, unresolved_frontier=(member,)),
                           expected_revision=state.revision)
    assert legacy.admitted_frontier == (member,)
    assert legacy.follow_up_root_id is None
    with sqlite3.connect(env.d2_path) as connection:
        row = connection.execute("SELECT record_json FROM rca_runtime_continuations WHERE root_id=?",
                                 (env.root,)).fetchone()
        payload = json.loads(row[0])
        payload.pop("admitted_frontier")
        payload.pop("frontier_resolutions")
        connection.execute("UPDATE rca_runtime_continuations SET record_json=? WHERE root_id=?",
                           (json.dumps(payload), env.root))
    reopened = SqliteRcaContinuationStore(env.d2_path)
    try:
        restored = reopened.get(env.root)
        assert restored.unresolved_frontier == restored.admitted_frontier == (member,)
        flow = RcaFollowUpOrchestrator(incidents=env.incidents, candidate_a=env.a,
            candidate_b=env.b, candidate_c=_C(), continuations=reopened,
            clock=env.time.clock())
        result = flow.recheck(INCIDENT)
        assert result.disposition is FollowUpDisposition.OUTSTANDING
        assert result.continuation.follow_up_root_id == flow.follow_up_root(INCIDENT)
        assert result.continuation.unresolved_frontier == (member,)
        assert result.continuation.admitted_frontier == (member,)
    finally:
        reopened.close()


def test_explicit_s4_missing_admission_and_legacy_terminal_without_ledger_fail_closed(env):
    member = _revision(env.material_a)
    state = env.d2.get(env.root)
    with pytest.raises(ValueError, match="admitted frontier"):
        replace(state, follow_up_root_id=env.flow.follow_up_root(INCIDENT),
                unresolved_frontier=(member,), admitted_frontier=())
    env.flow.admit(INCIDENT, _revision(env.baseline))
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.COMPLETE
    with sqlite3.connect(env.d2_path) as connection:
        row = connection.execute("SELECT record_json FROM rca_runtime_continuations WHERE root_id=?",
                                 (env.root,)).fetchone()
        payload = json.loads(row[0])
        payload.pop("admitted_frontier")
        payload.pop("frontier_resolutions")
        connection.execute("UPDATE rca_runtime_continuations SET record_json=? WHERE root_id=?",
                           (json.dumps(payload), env.root))
    reopened = SqliteRcaContinuationStore(env.d2_path)
    try:
        with pytest.raises(Exception, match="legacy completed frontier"):
            reopened.get(env.root)
    finally:
        reopened.close()


def _atomic_complete(env):
    checked = env.flow.recheck(INCIDENT)
    assert checked.disposition is FollowUpDisposition.COMPLETE
    work = env.d2.get_follow_up_work(env.root)
    return env.d2.complete_rca_follow_up_if_frontier_unchanged(
        env.root, expected_work_revision=work.revision,
        expected_frontier_revision=checked.continuation.revision,
        observed_at=NOW + timedelta(minutes=1))


def _outstanding_followups(path):
    with SqliteRuntimeWorkStore(path) as store:
        return tuple(row for row in store.enumerate_outstanding().records
                     if row.work_kind is RuntimeWorkKind.RCA_FOLLOW_UP)


def test_atomic_admission_wins_before_conditional_completion(env):
    checked = env.flow.recheck(INCIDENT)
    old_work = env.d2.get_follow_up_work(env.root)
    member = _revision(env.material_a)
    assert env.flow.admit(INCIDENT, member).disposition is FollowUpDisposition.OUTSTANDING
    with pytest.raises(RcaContinuationConcurrencyError):
        env.d2.complete_rca_follow_up_if_frontier_unchanged(
            env.root, expected_work_revision=old_work.revision,
            expected_frontier_revision=checked.continuation.revision,
            observed_at=NOW + timedelta(minutes=1))
    assert env.d2.get_follow_up_work(env.root).status is RuntimeWorkStatus.OUTSTANDING
    assert env.d2.get(env.root).unresolved_frontier == (member,)


def test_atomic_completion_wins_then_admission_creates_one_recoverable_work(env):
    old = _atomic_complete(env)
    assert old.status is RuntimeWorkStatus.COMPLETED
    member = _revision(env.material_a)
    first = env.flow.admit(INCIDENT, member)
    assert first.disposition is FollowUpDisposition.OUTSTANDING
    assert first.continuation.follow_up_root_id == env.flow.follow_up_root(INCIDENT)
    new_id = first.continuation.follow_up_work_id
    assert new_id == runtime_work_id(
        RuntimeWorkKind.RCA_FOLLOW_UP, first.continuation.follow_up_root_id,
        str(first.continuation.follow_up_work_activation_revision))
    assert env.flow.admit(INCIDENT, member).continuation.follow_up_work_id == new_id
    with SqliteRuntimeWorkStore(env.d2_path) as store:
        assert store.get(old.work_id) == old
    assert tuple(row.work_id for row in _outstanding_followups(env.d2_path)) == (new_id,)
    with SqliteRcaContinuationStore(env.d2_path) as reopened:
        recovered = tuple(row for row in reopened.enumerate_all().records
                          if row.incident_id == INCIDENT)
        assert len(recovered) == 1
        assert recovered[0].unresolved_frontier == (member,)
        assert reopened.get_follow_up_work(recovered[0].root_id).work_id == new_id


def test_simultaneous_admission_and_completion_serialize_without_orphan(env):
    checked = env.flow.recheck(INCIDENT)
    work = env.d2.get_follow_up_work(env.root)
    member = _revision(env.material_a)
    candidate = replace(checked.continuation,
                        follow_up_root_id=env.flow.follow_up_root(INCIDENT),
                        admitted_frontier=(member,), unresolved_frontier=(member,),
                        follow_up_complete=False)
    barrier = Barrier(2)
    def admit():
        with SqliteRcaContinuationStore(env.d2_path) as store:
            barrier.wait(timeout=10)
            try:
                store.admit_follow_up(candidate, expected_revision=checked.continuation.revision)
                return "ADMITTED"
            except RcaContinuationConcurrencyError:
                return "CONFLICT"
    def complete():
        with SqliteRcaContinuationStore(env.d2_path) as store:
            barrier.wait(timeout=10)
            try:
                store.complete_rca_follow_up_if_frontier_unchanged(
                    env.root, expected_work_revision=work.revision,
                    expected_frontier_revision=checked.continuation.revision,
                    observed_at=NOW + timedelta(minutes=1))
                return "COMPLETED"
            except RcaContinuationConcurrencyError:
                return "CONFLICT"
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(admit)
        c = pool.submit(complete)
        outcomes = a.result(timeout=15), c.result(timeout=15)
    assert outcomes in (("ADMITTED", "CONFLICT"), ("ADMITTED", "COMPLETED"))
    assert env.d2.get(env.root).unresolved_frontier == (member,)
    assert env.d2.get_follow_up_work(env.root).status is RuntimeWorkStatus.OUTSTANDING
    assert len(_outstanding_followups(env.d2_path)) <= 1


def test_conditional_completion_rolls_back_on_crash_inside_transaction(env):
    checked = env.flow.recheck(INCIDENT)
    work = env.d2.get_follow_up_work(env.root)
    original_write = env.d2._write
    @contextmanager
    def crash_before_commit():
        with original_write():
            yield
            raise KeyboardInterrupt("simulated crash before commit")
    env.d2._write = crash_before_commit
    try:
        with pytest.raises(KeyboardInterrupt):
            env.d2.complete_rca_follow_up_if_frontier_unchanged(
                env.root, expected_work_revision=work.revision,
                expected_frontier_revision=checked.continuation.revision,
                observed_at=NOW + timedelta(minutes=1))
    finally:
        env.d2._write = original_write
    with SqliteRcaContinuationStore(env.d2_path) as reopened:
        assert reopened.get_follow_up_work(env.root) == work
        assert reopened.get(env.root).revision == checked.continuation.revision


def test_crash_after_atomic_admission_reopens_same_member_and_work(env):
    _atomic_complete(env)
    member = _revision(env.material_a)
    admitted = env.flow.admit(INCIDENT, member).continuation
    with SqliteRcaContinuationStore(env.d2_path) as reopened:
        assert reopened.get(env.root).unresolved_frontier == (member,)
        assert reopened.get_follow_up_work(env.root).work_id == admitted.follow_up_work_id
        assert reopened.get_follow_up_work(env.root).status is RuntimeWorkStatus.OUTSTANDING
    assert len(_outstanding_followups(env.d2_path)) == 1


def test_legacy_completed_work_with_unresolved_member_fails_closed(env):
    old = _atomic_complete(env)
    member = _revision(env.material_a)
    state = env.d2.get(env.root)
    with pytest.raises(ContradictoryRcaContinuationError, match="terminal work"):
        env.d2.update(replace(state, unresolved_frontier=(member,),
                              admitted_frontier=(member,)),
                      expected_revision=state.revision)
    with sqlite3.connect(env.d2_path) as connection:
        row = connection.execute(
            "SELECT record_json FROM rca_runtime_continuations WHERE root_id=?",
            (env.root,)).fetchone()
        payload = json.loads(row[0])
        payload["revision"] = state.revision + 1
        payload["follow_up_root_id"] = env.flow.follow_up_root(INCIDENT)
        payload["unresolved_frontier"] = [{"requirement_type": member.requirement_type,
                                           "reference": member.reference}]
        payload["admitted_frontier"] = payload["unresolved_frontier"]
        payload["follow_up_complete"] = False
        connection.execute(
            "UPDATE rca_runtime_continuations SET record_json=?, revision=? WHERE root_id=?",
            (json.dumps(payload), payload["revision"], env.root))
    with pytest.raises(ContradictoryRcaContinuationError, match="completed work"):
        env.d2.get_follow_up_work(env.root)
    assert env.flow.recheck(INCIDENT).disposition is FollowUpDisposition.REPAIR_REQUIRED
    assert env.flow.admit(INCIDENT, _revision(env.material_b)).disposition is FollowUpDisposition.REPAIR_REQUIRED
    assert env.d2.get(env.root).unresolved_frontier == (member,)
    with SqliteRuntimeWorkStore(env.d2_path) as store:
        assert store.get(old.work_id) == old
    assert _outstanding_followups(env.d2_path) == ()


def test_legacy_completed_work_with_provisional_proof_fails_closed(env):
    member = _revision(env.baseline)
    env.flow.admit(INCIDENT, member)
    old = _atomic_complete(env)
    with sqlite3.connect(env.d2_path) as connection:
        row = connection.execute(
            "SELECT record_json FROM rca_runtime_continuations WHERE root_id=?",
            (env.root,)).fetchone()
        payload = json.loads(row[0])
        payload["revision"] += 1
        payload["frontier_resolutions"][0]["state"] = "PROVISIONAL"
        connection.execute(
            "UPDATE rca_runtime_continuations SET record_json=?, revision=? WHERE root_id=?",
            (json.dumps(payload), payload["revision"], env.root))
    with SqliteRcaContinuationStore(env.d2_path) as reopened:
        with pytest.raises(ContradictoryRcaContinuationError, match="provisional"):
            reopened.get_follow_up_work(env.root)
        assert reopened.get(env.root).admitted_frontier == (member,)
    with SqliteRuntimeWorkStore(env.d2_path) as store:
        assert store.get(old.work_id) == old


def test_two_members_after_terminal_completion_share_one_outstanding_work(env):
    _atomic_complete(env)
    first = _revision(env.material_a)
    second = _revision(env.material_b)
    barrier = Barrier(2)
    def admit(member):
        with SqliteRcaContinuationStore(env.d2_path) as d2:
            a = SqliteRcaStore(env.a_path)
            b = _B(env.b_path)
            try:
                flow = RcaFollowUpOrchestrator(
                    incidents=env.incidents, candidate_a=a, candidate_b=b,
                    candidate_c=_C(), continuations=d2, clock=env.time.clock())
                barrier.wait(timeout=10)
                return flow.admit(INCIDENT, member)
            finally:
                a.close(); b.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(admit, first), pool.submit(admit, second))
        a, b = (future.result(timeout=15) for future in futures)
    assert a.disposition is b.disposition is FollowUpDisposition.OUTSTANDING
    assert a.continuation.follow_up_work_id == b.continuation.follow_up_work_id
    assert set(env.d2.get(env.root).unresolved_frontier) == {first, second}
    assert len(_outstanding_followups(env.d2_path)) == 1


def test_resolution_admission_completion_race_requires_new_full_frontier(env):
    covered = _revision(env.baseline)
    arriving = _revision(env.material_a)
    env.flow.admit(INCIDENT, covered)
    checked = env.flow.recheck(INCIDENT)
    assert checked.disposition is FollowUpDisposition.COMPLETE
    work = env.d2.get_follow_up_work(env.root)
    env.flow.admit(INCIDENT, arriving)
    with pytest.raises(RcaContinuationConcurrencyError):
        env.d2.complete_rca_follow_up_if_frontier_unchanged(
            env.root, expected_work_revision=work.revision,
            expected_frontier_revision=checked.continuation.revision,
            observed_at=NOW + timedelta(minutes=1))
    assert env.d2.get(env.root).unresolved_frontier == (arriving,)


def test_authoritative_proof_invalidation_after_completion_reactivates_same_root(env):
    member = _revision(env.baseline)
    env.flow.admit(INCIDENT, member)
    old = _atomic_complete(env)
    _change_same_version_current(env)
    result = env.flow.recheck(INCIDENT)
    assert result.disposition is FollowUpDisposition.OUTSTANDING
    assert result.continuation.unresolved_frontier == (member,)
    assert result.continuation.follow_up_root_id == env.flow.follow_up_root(INCIDENT)
    assert env.d2.get_follow_up_work(env.root).status is RuntimeWorkStatus.OUTSTANDING
    assert env.d2.get_follow_up_work(env.root).work_id != old.work_id
    assert len(_outstanding_followups(env.d2_path)) == 1