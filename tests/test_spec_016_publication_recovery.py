"""SPEC-016 S5 receipt-first publication and completion evidence."""

from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import inspect

import pytest

from incident_management import IncidentManager, IncidentRcaPublicationRequest
from rca_integration import E4Classification, RcaPublicationCoordinator
from rca_persistence import PublicationDisposition, RcaDomainError
from runtime_orchestration.contracts import RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus
from runtime_orchestration.rca_continuation import (
    FollowUpRequirement, RcaContinuation, SqliteRcaContinuationStore,
    rca_child_operation_id, rca_root_id,
)
from runtime_orchestration.rca_followup import FollowUpDisposition, FollowUpResult
from runtime_orchestration.rca_publication import (
    PublicationRuntimeDisposition, RcaPublicationOrchestrator,
)
from runtime_orchestration.identity import runtime_work_id
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore

from test_rca_host_integration import NOW, _commit_next, _host


def _008_apply(incident_store, target):
    return IncidentManager(incident_store).publish_rca_current(
        IncidentRcaPublicationRequest(target.publication_operation_id,
                                      target.incident_id, target.target_version_id,
                                      target.expected_current_version_id,
                                      NOW + timedelta(minutes=3)))


def test_e4_order_receipt_first_and_response_loss_no_second_mutation(tmp_path):
    _, _, incident, a, _ = _host(tmp_path)
    target = a.get_publication_result("PUB-1").target
    receipt = _008_apply(incident, target)
    calls = []

    class Reads:
        def get_rca_publication_result(self, op):
            calls.append("008 receipt")
            return incident.get_rca_publication_result(op)
        def get_rca_relationship(self, inc):
            calls.append("008 relationship")
            return incident.get_rca_relationship(inc)

    class A:
        def get_publication_result(self, op):
            calls.append("A receipt")
            return a.get_publication_result(op)
        def get_current(self, agg):
            calls.append("A current")
            return a.get_current(agg)
        def get_version_history(self, agg):
            calls.append("A history")
            return a.get_version_history(agg)
        def complete_authorized_publication(self, result):
            return a.complete_authorized_publication(result)

    class NoMutation:
        def publish_rca_current(self, request):
            pytest.fail("APPLIED receipt must prohibit another SPEC-008 mutation")

    coordinator = RcaPublicationCoordinator(A(), NoMutation(), Reads())
    observed = coordinator.inspect("PUB-1", target=target)
    assert calls == ["008 receipt", "008 relationship", "A receipt", "A current", "A history"]
    assert observed.classification is E4Classification.SPEC_008_APPLIED_A_INCOMPLETE
    assert E4Classification.RESPONSE_LOST in observed.recovery_facts
    completed = coordinator.reconcile("PUB-1", NOW + timedelta(minutes=4), target=target)
    assert completed.disposition is PublicationDisposition.APPLIED
    assert completed.recorded_at == receipt.completed_at
    assert coordinator.inspect("PUB-1", target=target).classification is E4Classification.TARGET_ALREADY_CURRENT_EQUIVALENT
    incident.close(); a.close()


def test_a_committed_008_incomplete_then_equivalent_replay(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    target = a.get_publication_result("PUB-1").target
    assert coordinator.inspect("PUB-1", target=target).classification is E4Classification.A_COMMITTED_008_INCOMPLETE
    first = coordinator.reconcile("PUB-1", NOW + timedelta(minutes=3), target=target)
    assert first.disposition is PublicationDisposition.APPLIED
    assert coordinator.reconcile("PUB-1", NOW + timedelta(minutes=4), target=target) == first
    assert len(a.get_version_history("AGG-1")) == 1
    incident.close(); a.close()


def test_superseded_target_keeps_artifact_and_never_promotes(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "PUB-2", "VER-1")
    coordinator.publish("PUB-2", NOW + timedelta(minutes=4))
    _commit_next(a, "ATT-3", "OP-COMMIT-3", "VER-3", "PUB-3", "VER-1")
    stale = coordinator.reconcile("PUB-3", NOW + timedelta(minutes=5))
    assert stale.disposition is PublicationDisposition.PRECONDITION_SUPERSEDED
    assert coordinator.inspect("PUB-3").classification is E4Classification.PRECONDITION_SUPERSEDED
    assert a.get_current("AGG-1").version.version_id == "VER-2"
    assert {v.version_id for v in a.get_version_history("AGG-1")} == {"VER-1", "VER-2", "VER-3"}
    incident.close(); a.close()


def test_target_current_without_same_operation_receipt_fails_closed(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    target = a.get_publication_result("PUB-1").target
    # A different operation makes the exact target Current on SPEC-008.
    IncidentManager(incident).publish_rca_current(
        IncidentRcaPublicationRequest("PUB-OTHER", "INC-1", "VER-1", None,
                                      NOW + timedelta(minutes=3)))
    assert coordinator.inspect("PUB-1", target=target).classification is E4Classification.TARGET_CONFLICT
    with pytest.raises(RcaDomainError):
        coordinator.reconcile("PUB-1", NOW + timedelta(minutes=4), target=target)
    assert a.get_current("AGG-1") is None
    incident.close(); a.close()


def test_incoherent_authority_fails_before_mutation(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    target = a.get_publication_result("PUB-1").target
    _008_apply(incident, target)
    # A read port that contradicts durable A history must not be accepted.
    class BadA:
        def get_publication_result(self, op): return a.get_publication_result(op)
        def get_current(self, agg): return a.get_current(agg)
        def get_version_history(self, agg): return ()

    class NoMutation:
        def publish_rca_current(self, request): pytest.fail("must fail before mutation")

    bad = RcaPublicationCoordinator(BadA(), NoMutation(), incident)
    assert bad.inspect("PUB-1", target=target).classification is E4Classification.INCOHERENT_AUTHORITY
    with pytest.raises(RcaDomainError): bad.reconcile("PUB-1", NOW, target=target)
    incident.close(); a.close()


def test_concurrent_replay_and_publication_race_converge_from_receipts(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    target = a.get_publication_result("PUB-1").target
    _008_apply(incident, target)
    first = coordinator.reconcile("PUB-1", NOW + timedelta(minutes=4), target=target)
    assert coordinator.reconcile("PUB-1", NOW + timedelta(minutes=5), target=target) == first
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "PUB-2", "VER-1")
    _commit_next(a, "ATT-3", "OP-COMMIT-3", "VER-3", "PUB-3", "VER-1")
    winner = coordinator.reconcile("PUB-2", NOW + timedelta(minutes=6))
    loser = coordinator.reconcile("PUB-3", NOW + timedelta(minutes=7))
    assert winner.disposition is PublicationDisposition.APPLIED
    assert loser.disposition is PublicationDisposition.PRECONDITION_SUPERSEDED
    assert incident.get_rca_relationship("INC-1").current_version_id == "VER-2"
    assert coordinator.inspect("PUB-1").classification is E4Classification.APPLIED
    assert coordinator.reconcile("PUB-1", NOW + timedelta(minutes=8)) == first
    incident.close(); a.close()


def test_response_lost_after_spec_008_commit_reopens_without_second_mutation(tmp_path):
    incident_path, a_path, incident, a, _ = _host(tmp_path)
    target = a.get_publication_result("PUB-1").target

    class LostResponse:
        def publish_rca_current(self, request):
            IncidentManager(incident).publish_rca_current(request)
            raise OSError("response lost after durable SPEC-008 commit")

    with pytest.raises(OSError):
        RcaPublicationCoordinator(a, LostResponse(), incident).reconcile(
            "PUB-1", NOW + timedelta(minutes=3), target=target)
    assert a.get_current("AGG-1") is None
    assert incident.get_rca_publication_result("PUB-1") is not None
    incident.close(); a.close()

    from incident_management import SqliteIncidentStore
    from rca_persistence import SqliteRcaStore
    with SqliteIncidentStore(str(incident_path)) as reopened_008:
        with SqliteRcaStore(a_path) as reopened_a:
            class NoMutation:
                def publish_rca_current(self, request):
                    pytest.fail("durable APPLIED receipt prohibits a second mutation")
            coordinator = RcaPublicationCoordinator(reopened_a, NoMutation(), reopened_008)
            result = coordinator.reconcile("PUB-1", NOW + timedelta(minutes=4), target=target)
            assert result.disposition is PublicationDisposition.APPLIED
            assert reopened_a.get_current("AGG-1").version.version_id == "VER-1"


def test_receipt_first_reread_converges_after_concurrent_same_operation_completion(tmp_path):
    _, _, incident, a, winner = _host(tmp_path)
    target = a.get_publication_result("PUB-1").target
    calls = []

    class InterleavedReads:
        first = True
        def get_rca_publication_result(self, operation_id):
            calls.append("008 receipt")
            prior = incident.get_rca_publication_result(operation_id)
            if self.first:
                self.first = False
                assert prior is None
                winner.publish("PUB-1", NOW + timedelta(minutes=3))
            return prior
        def get_rca_relationship(self, incident_id):
            calls.append("008 relationship")
            return incident.get_rca_relationship(incident_id)

    class ReadA:
        def get_publication_result(self, op):
            calls.append("A receipt")
            return a.get_publication_result(op)
        def get_current(self, aggregate_id):
            calls.append("A current")
            return a.get_current(aggregate_id)
        def get_version_history(self, aggregate_id):
            calls.append("A history")
            return a.get_version_history(aggregate_id)
        def complete_authorized_publication(self, result):
            return a.complete_authorized_publication(result)

    class NoMutation:
        def publish_rca_current(self, request):
            pytest.fail("same-operation APPLIED receipt prohibits another mutation")

    replay = RcaPublicationCoordinator(ReadA(), NoMutation(), InterleavedReads())
    result = replay.reconcile("PUB-1", NOW + timedelta(minutes=4), target=target)
    assert result.disposition is PublicationDisposition.APPLIED
    assert calls[:10] == ["008 receipt", "008 relationship", "A receipt", "A current", "A history"] * 2
    assert incident.get_rca_publication_result("PUB-1").disposition.value == "APPLIED"
    assert a.get_current("AGG-1").version.version_id == "VER-1"
    incident.close(); a.close()


def test_actual_concurrent_same_operation_replay_has_one_business_effect(tmp_path):
    incident_path, a_path, incident, a, _ = _host(tmp_path)
    target = a.get_publication_result("PUB-1").target
    incident.close(); a.close()
    barrier = Barrier(2)

    def run():
        from incident_management import SqliteIncidentStore
        from rca_persistence import SqliteRcaStore
        with SqliteIncidentStore(str(incident_path)) as inc:
            with SqliteRcaStore(a_path) as rca:
                class Reads:
                    first = True
                    def get_rca_publication_result(self, operation_id):
                        result = inc.get_rca_publication_result(operation_id)
                        if self.first:
                            self.first = False
                            barrier.wait(timeout=5)
                        return result
                    def get_rca_relationship(self, incident_id):
                        return inc.get_rca_relationship(incident_id)
                return RcaPublicationCoordinator(rca, IncidentManager(inc), Reads()).reconcile(
                    "PUB-1", NOW + timedelta(minutes=3), target=target)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    from incident_management import SqliteIncidentStore
    from rca_persistence import SqliteRcaStore
    with SqliteIncidentStore(str(incident_path)) as inc:
        with SqliteRcaStore(a_path) as rca:
            assert inc.get_rca_publication_result("PUB-1") is not None
            assert len(rca.get_version_history("AGG-1")) == 1


def test_public_port_only_coordinator_has_no_runtime_authority():
    source = inspect.getsource(RcaPublicationCoordinator)
    assert "sqlite" not in source.lower()
    assert "_transaction" not in source
    assert not {"schedule", "retry", "clock", "startup"} & set(RcaPublicationCoordinator.__dict__)


class _FollowUp:
    def __init__(self, store): self.store = store; self.disposition = FollowUpDisposition.COMPLETE; self.calls = 0
    def recheck(self, incident_id):
        self.calls += 1
        return FollowUpResult(self.disposition, self.store.get(rca_root_id(incident_id)))


def _runtime(tmp_path, target, *, create_work=True):
    path = tmp_path / "runtime.db"
    d2 = SqliteRcaContinuationStore(path)
    work = SqliteRuntimeWorkStore(path)
    root = rca_root_id(target.incident_id)
    work_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
    initial = RcaContinuation(root, work_id,
                              target.incident_id, "PUBLICATION", "PUBLICATION", 3,
                              NOW, NOW, NOW,
                              publication_operation_id=target.publication_operation_id)
    d2.create(initial)
    if create_work:
        work.create(RuntimeWorkRecord(work_id, RuntimeWorkKind.RCA_INITIAL,
                                      "EVT-1", "PUBLICATION", "PUBLICATION", 0, 3,
                                      RuntimeWorkStatus.OUTSTANDING, NOW, NOW, NOW,
                                      incident_id=target.incident_id, operation_id=root))
    return d2, work, work_id


def test_completion_last_requires_authoritative_frontier_recheck(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "publication:two", "VER-1")
    target = a.get_publication_result("publication:two").target
    d2, work, work_id = _runtime(tmp_path, target)
    follow = _FollowUp(d2)
    class TrackingWork:
        def get(self, work_id): return work.get(work_id)
        def complete(self, work_id, *, observed_at, expected_revision):
            assert follow.calls > 0
            assert incident.get_rca_publication_result(target.publication_operation_id) is not None
            assert incident.get_rca_relationship("INC-1").current_version_id == "VER-2"
            assert a.get_current("AGG-1").version.version_id == "VER-2"
            return work.complete(work_id, observed_at=observed_at,
                                 expected_revision=expected_revision)
    flow = RcaPublicationOrchestrator(coordinator, d2, TrackingWork(), follow, incident)
    follow.disposition = FollowUpDisposition.OUTSTANDING
    pending = flow.reconcile(target, NOW + timedelta(minutes=4))
    assert pending.disposition is PublicationRuntimeDisposition.OUTSTANDING
    assert work.get(work_id).status is RuntimeWorkStatus.OUTSTANDING
    follow.disposition = FollowUpDisposition.COMPLETE
    done = flow.reconcile(target, NOW + timedelta(minutes=5))
    assert done.disposition is PublicationRuntimeDisposition.COMPLETE
    assert done.work.status is RuntimeWorkStatus.COMPLETED
    assert follow.calls == 2
    d2.close(); work.close(); incident.close(); a.close()


def test_frontier_revision_change_after_recheck_blocks_runtime_completion(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "publication:two", "VER-1")
    target = a.get_publication_result("publication:two").target
    d2, work, work_id = _runtime(tmp_path, target)

    class ConcurrentAdmission:
        def recheck(self, incident_id):
            old = d2.get(rca_root_id(incident_id))
            member = FollowUpRequirement("MATERIAL_EVIDENCE", "revision:7")
            d2.update(replace(old, unresolved_frontier=(member,),
                              updated_at=NOW + timedelta(minutes=4),
                              observed_at=NOW + timedelta(minutes=4)),
                      expected_revision=old.revision)
            return FollowUpResult(FollowUpDisposition.COMPLETE, old)

    result = RcaPublicationOrchestrator(coordinator, d2, work, ConcurrentAdmission(), incident).reconcile(
        target, NOW + timedelta(minutes=5))
    assert result.disposition is PublicationRuntimeDisposition.OUTSTANDING
    assert work.get(work_id).status is RuntimeWorkStatus.OUTSTANDING
    assert d2.get(rca_root_id("INC-1")).unresolved_frontier
    d2.close(); work.close(); incident.close(); a.close()


def test_admission_after_last_frontier_read_blocks_atomic_runtime_completion(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "publication:two", "VER-1")
    target = a.get_publication_result("publication:two").target
    d2, work, work_id = _runtime(tmp_path, target)
    root = rca_root_id(target.incident_id)
    member = FollowUpRequirement("MATERIAL_EVIDENCE", "revision:7")

    class LateAdmission:
        def __getattr__(self, name): return getattr(d2, name)
        def complete_rca_follow_up_if_frontier_unchanged(self, root_id, **kwargs):
            old = d2.get(root_id)
            d2.admit_follow_up(replace(
                old, follow_up_root_id=rca_child_operation_id(
                    root_id, target.incident_id, "FOLLOW_UP"),
                unresolved_frontier=(member,), admitted_frontier=(member,)),
                expected_revision=old.revision)
            return d2.complete_rca_follow_up_if_frontier_unchanged(root_id, **kwargs)

    result = RcaPublicationOrchestrator(
        coordinator, LateAdmission(), work, _FollowUp(d2), incident).reconcile(
            target, NOW + timedelta(minutes=5))
    assert result.disposition is PublicationRuntimeDisposition.OUTSTANDING
    assert work.get(work_id).status is RuntimeWorkStatus.OUTSTANDING
    assert d2.get(root).unresolved_frontier == (member,)
    d2.close(); work.close(); incident.close(); a.close()


@pytest.mark.parametrize("field,bad_value", [
    ("work_kind", RuntimeWorkKind.DOMAIN_OPERATION),
    ("operation_id", "wrong-root"),
    ("event_id", "EVT-WRONG"),
])
def test_contradictory_runtime_work_binding_never_completes(tmp_path, field, bad_value):
    _, _, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "publication:two", "VER-1")
    target = a.get_publication_result("publication:two").target
    d2, work, work_id = _runtime(tmp_path, target, create_work=False)
    row = RuntimeWorkRecord(work_id, RuntimeWorkKind.RCA_INITIAL, "EVT-1",
                            "PUBLICATION", "PUBLICATION", 0, 3,
                            RuntimeWorkStatus.OUTSTANDING, NOW, NOW, NOW,
                            incident_id="INC-1", operation_id=rca_root_id("INC-1"))
    work.create(replace(row, **{field: bad_value}))
    result = RcaPublicationOrchestrator(coordinator, d2, work, _FollowUp(d2), incident).reconcile(
        target, NOW + timedelta(minutes=4))
    assert result.disposition is PublicationRuntimeDisposition.REPAIR_REQUIRED
    assert work.get(work_id).status is RuntimeWorkStatus.OUTSTANDING
    d2.close(); work.close(); incident.close(); a.close()


def test_unreadable_incident_event_binding_blocks_runtime_completion(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "publication:two", "VER-1")
    target = a.get_publication_result("publication:two").target
    d2, work, work_id = _runtime(tmp_path, target)
    result = RcaPublicationOrchestrator(coordinator, d2, work, _FollowUp(d2)).reconcile(
        target, NOW + timedelta(minutes=4))
    assert result.disposition is PublicationRuntimeDisposition.REPAIR_REQUIRED
    assert work.get(work_id).status is RuntimeWorkStatus.OUTSTANDING
    d2.close(); work.close(); incident.close(); a.close()


def test_missing_runtime_work_reconstructs_same_root_after_domain_coherence(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "publication:two", "VER-1")
    target = a.get_publication_result("publication:two").target
    d2, work, work_id = _runtime(tmp_path, target, create_work=False)
    flow = RcaPublicationOrchestrator(coordinator, d2, work, _FollowUp(d2), incident)
    result = flow.reconcile(target, NOW + timedelta(minutes=4))
    assert result.disposition is PublicationRuntimeDisposition.COMPLETE
    assert result.work.work_id == work_id
    assert result.work.status is RuntimeWorkStatus.COMPLETED
    assert len(work.enumerate_all().records) == 1
    d2.close(); work.close(); incident.close(); a.close()


def test_incomplete_d2_publication_reference_reconstructed_without_frontier_loss(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "publication:two", "VER-1")
    target = a.get_publication_result("publication:two").target
    path = tmp_path / "runtime.db"
    d2 = SqliteRcaContinuationStore(path)
    work = SqliteRuntimeWorkStore(path)
    root = rca_root_id("INC-1")
    member = FollowUpRequirement("MATERIAL_EVIDENCE", "revision:7")
    wake = NOW + timedelta(minutes=2)
    d2.create(RcaContinuation(root, runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root),
                              "INC-1", "PUBLICATION", "PUBLICATION", 3,
                              NOW, NOW, NOW, unresolved_frontier=(member,),
                              next_eligibility_at=wake, follow_up_wake_at=wake))
    follow = _FollowUp(d2)
    follow.disposition = FollowUpDisposition.OUTSTANDING
    result = RcaPublicationOrchestrator(coordinator, d2, work, follow).reconcile(
        target, NOW + timedelta(minutes=4))
    assert result.disposition is PublicationRuntimeDisposition.OUTSTANDING
    restored = d2.get(root)
    assert restored.publication_operation_id == target.publication_operation_id
    assert restored.unresolved_frontier == (member,)
    assert restored.admitted_frontier == (member,)
    assert restored.next_eligibility_at == wake
    assert work.enumerate_all().records == ()
    d2.close(); work.close(); incident.close(); a.close()


def test_reopen_partial_publication_and_runtime_continuation_before_completion(tmp_path):
    incident_path, a_path, incident, a, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(a, "ATT-2", "OP-COMMIT-2", "VER-2", "publication:two", "VER-1")
    target = a.get_publication_result("publication:two").target
    d2, work, work_id = _runtime(tmp_path, target)
    _008_apply(incident, target)
    assert a.get_publication_result(target.publication_operation_id).disposition is PublicationDisposition.A_SIDE_COMMITTED
    d2.close(); work.close(); incident.close(); a.close()

    from incident_management import SqliteIncidentStore
    from rca_persistence import SqliteRcaStore
    with SqliteIncidentStore(str(incident_path)) as reopened_008:
        with SqliteRcaStore(a_path) as reopened_a:
            reopened_d2 = SqliteRcaContinuationStore(tmp_path / "runtime.db")
            try:
                with SqliteRuntimeWorkStore(tmp_path / "runtime.db") as reopened_work:
                    class NoMutation:
                        def publish_rca_current(self, request):
                            pytest.fail("reopened APPLIED receipt prohibits second mutation")
                    replay = RcaPublicationCoordinator(reopened_a, NoMutation(), reopened_008)
                    flow = RcaPublicationOrchestrator(replay, reopened_d2, reopened_work,
                                                      _FollowUp(reopened_d2), reopened_008)
                    result = flow.reconcile(target, NOW + timedelta(minutes=4))
                    assert result.disposition is PublicationRuntimeDisposition.COMPLETE
                    assert reopened_work.get(work_id).status is RuntimeWorkStatus.COMPLETED
                    assert reopened_a.get_current("AGG-1").version.version_id == "VER-2"
            finally:
                reopened_d2.close()


def test_missing_d2_never_silently_proves_empty_frontier(tmp_path):
    _, _, incident, a, coordinator = _host(tmp_path)
    d2 = SqliteRcaContinuationStore(tmp_path / "runtime.db")
    work = SqliteRuntimeWorkStore(tmp_path / "runtime.db")
    target = a.get_publication_result("PUB-1").target
    flow = RcaPublicationOrchestrator(coordinator, d2, work, _FollowUp(d2))
    result = flow.reconcile(target, NOW + timedelta(minutes=3))
    assert result.disposition is PublicationRuntimeDisposition.RUNTIME_BOOKKEEPING_LOST
    assert coordinator.inspect("PUB-1", target=target).classification is E4Classification.TARGET_ALREADY_CURRENT_EQUIVALENT
    assert work.enumerate_all().records == ()
    d2.close(); work.close(); incident.close(); a.close()


def _seed_b1_public_authorities(path):
    """Real A/B/008 stores; coherent Current has completed E1 coverage."""
    from test_spec_016_followup_refresh import env, _completed_post_context
    from test_incident_manager import _request, _create_decision, _event
    from incident_management import SqliteIncidentStore
    from rca_persistence import (AdmitAttemptRequest, LogicalTryOutcome,
        LogicalTryIdentity, LogicalTryResultKind, AdmittedRetryDisposition,
        PublicationTargetIdentity)
    generator = env.__wrapped__(path)
    state = next(generator)
    incident = SqliteIncidentStore(str(path / "incident.sqlite3"))
    d2 = SqliteRcaContinuationStore(path / "runtime.sqlite3")
    work = SqliteRuntimeWorkStore(path / "runtime.sqlite3")
    try:
        now = state.time.now
        manager = IncidentManager(incident, incident_id_factory=lambda: "INC-1")
        manager.apply_correlation_mutation(_request(_create_decision(),
            _event("event-1", detected_at="2026-09-21T12:00:00Z"),
            operation_id="OP-B1-CREATE", now=now))
        old = state.a.get_publication_result("PUB-1").target
        manager.publish_rca_current(IncidentRcaPublicationRequest(
            "PUB-1", "INC-1", old.target_version_id, None, now))
        covered = _completed_post_context(state)
        current = state.a.get_current(old.aggregate_id)
        lineage = replace(current.attempt_lineage.attempt.lineage,
            attempt_id="attempt:2", evidence_snapshot_id=covered.snapshot_id,
            evidence_revision_id=covered.revision_id)
        state.a.admit_attempt(AdmitAttemptRequest("OP-B1-ATTEMPT", lineage, now))
        state.a.record_try_outcome("OP-B1-TRY", LogicalTryOutcome(
            LogicalTryIdentity("attempt:2", 1), LogicalTryResultKind.VALIDATED_RESULT,
            AdmittedRetryDisposition.NON_RETRYABLE, now, validated_result_id="VALID-2"))
        artifact = replace(current.artifact, provenance=replace(current.artifact.provenance,
            evidence_snapshot_id=covered.snapshot_id, evidence_revision_id=covered.revision_id))
        target = PublicationTargetIdentity("publication:b1", old.aggregate_id,
            "INC-1", "version:2", old.target_version_id)
        state.a.commit_validated_artifact("OP-B1-ARTIFACT", "attempt:2", artifact, target, now)
        RcaPublicationCoordinator(state.a, manager, incident).reconcile(
            target.publication_operation_id, now + timedelta(minutes=3), target=target)
        work_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, state.root)
        d2.create(RcaContinuation(state.root, work_id, "INC-1", "PUBLICATION",
            "PUBLICATION", 4, now, now, now,
            publication_operation_id=target.publication_operation_id))
        work.create(RuntimeWorkRecord(work_id, RuntimeWorkKind.RCA_INITIAL,
            "event-1", "PUBLICATION", "PUBLICATION", 0, 4,
            RuntimeWorkStatus.OUTSTANDING, now, now, now,
            incident_id="INC-1", operation_id=state.root))
    finally:
        work.close(); d2.close(); incident.close(); generator.close()


def _open_b1_public_authorities(path):
    from types import SimpleNamespace
    from incident_management import SqliteIncidentStore
    from rca_persistence import SqliteRcaStore
    from runtime_orchestration.rca_followup import RcaFollowUpOrchestrator
    from test_spec_016_followup_refresh import _B, _C, _Time
    incident = SqliteIncidentStore(str(path / "incident.sqlite3"))
    a = SqliteRcaStore(path / "a.sqlite3")
    b = _B(path / "b.sqlite3")
    d2 = SqliteRcaContinuationStore(path / "runtime.sqlite3")
    work = SqliteRuntimeWorkStore(path / "runtime.sqlite3")
    manager = IncidentManager(incident)
    time = _Time()
    time.now += timedelta(minutes=3)
    follow = RcaFollowUpOrchestrator(incidents=incident, candidate_a=a,
        candidate_b=b, candidate_c=_C(), continuations=d2, clock=time.clock())
    coordinator = RcaPublicationCoordinator(a, manager, incident)
    target = a.get_publication_result("publication:b1").target
    flow = RcaPublicationOrchestrator(coordinator, d2, work, follow, incident)
    return SimpleNamespace(incident=incident, manager=manager, a=a, b=b,
        d2=d2, work=work, follow=follow, coordinator=coordinator,
        target=target, flow=flow, now=time.now)


def _close_b1_public_authorities(state):
    state.work.close(); state.d2.close(); state.b.close()
    state.a.close(); state.incident.close()


def _attach_b1_event(state):
    from test_incident_manager import _request, _attach_decision, _event
    state.manager.apply_correlation_mutation(_request(_attach_decision(),
        _event("event-2", detected_at="2026-09-21T12:00:30Z"),
        operation_id="OP-B1-ATTACH", now=state.now))


@pytest.mark.parametrize("arrival", ("before_recheck", "during_b_read", "covered"))
@pytest.mark.parametrize("existing_root", (False, True))
def test_b1_empty_frontier_checks_durable_incident_coverage_and_reopen(tmp_path, arrival, existing_root):
    _seed_b1_public_authorities(tmp_path)
    state = _open_b1_public_authorities(tmp_path)
    root = rca_root_id("INC-1")
    try:
        initial = state.d2.get(root)
        assert initial.follow_up_root_id is None
        assert initial.admitted_frontier == initial.unresolved_frontier == ()
        if existing_root:
            current = state.a.get_current(state.target.aggregate_id)
            member = FollowUpRequirement("MATERIAL_EVIDENCE", "revision:" +
                current.attempt_lineage.attempt.lineage.evidence_revision_id)
            assert state.follow.admit("INC-1", member).disposition is FollowUpDisposition.OUTSTANDING
            assert state.follow.recheck("INC-1").disposition is FollowUpDisposition.COMPLETE
            assert state.d2.get(root).unresolved_frontier == ()
        assert state.coordinator.inspect("publication:b1", target=state.target).classification is E4Classification.TARGET_ALREADY_CURRENT_EQUIVALENT
        if arrival == "before_recheck":
            _attach_b1_event(state)
        elif arrival == "during_b_read":
            original_read = state.b.resolve_snapshot
            arrived = False
            def read_then_correlate(snapshot_id):
                nonlocal arrived
                snapshot = original_read(snapshot_id)
                if not arrived:
                    arrived = True
                    _attach_b1_event(state)
                return snapshot
            state.b.resolve_snapshot = read_then_correlate
        result = state.flow.reconcile(state.target, state.now)
        expected = (PublicationRuntimeDisposition.COMPLETE if arrival == "covered"
                    else PublicationRuntimeDisposition.OUTSTANDING)
        assert result.disposition is expected
        work_id = initial.runtime_work_id
        assert state.work.get(work_id).status is (
            RuntimeWorkStatus.COMPLETED if arrival == "covered" else RuntimeWorkStatus.OUTSTANDING)
        # Existing ledger proof may replay SAME; arrival itself supplies no
        # candidate Revision or MATERIAL/STALE/Attempt authorization.
        assert all(request.baseline_revision_id == request.candidate_revision_id
                   for request in state.b.requests)
        assert len(state.a.get_version_history(state.target.aggregate_id)) == 2
    finally:
        _close_b1_public_authorities(state)
    del state  # fresh public services and connections on every replay
    for _ in range(3):
        fresh = _open_b1_public_authorities(tmp_path)
        try:
            assert fresh.flow.reconcile(fresh.target, fresh.now).disposition is expected
            assert fresh.d2.get(root).runtime_work_id == work_id
            assert len(fresh.work.enumerate_all().records) == 1
            assert len(fresh.a.get_version_history(fresh.target.aggregate_id)) == 2
            assert fresh.incident.get_incident("INC-1").event_ids == (
                ("event-1",) if arrival == "covered" else ("event-1", "event-2"))
        finally:
            _close_b1_public_authorities(fresh)


@pytest.mark.parametrize("bad_read", ("unreadable", "malformed", "contradictory"))
def test_b1_empty_frontier_unreadable_or_invalid_coverage_never_completes(tmp_path, bad_read):
    from copy import deepcopy
    _seed_b1_public_authorities(tmp_path)
    state = _open_b1_public_authorities(tmp_path)
    original_read = state.b.resolve_snapshot
    def invalid(snapshot_id):
        if bad_read == "unreadable":
            raise OSError("authority unavailable")
        snapshot = original_read(snapshot_id)
        content = deepcopy(snapshot.snapshot_content)
        if bad_read == "malformed":
            content["event_projections"] = None
        else:
            content["event_projections"] *= 2
        return replace(snapshot, snapshot_content=content)
    state.b.resolve_snapshot = invalid
    try:
        checked = state.follow.recheck("INC-1")
        assert checked.disposition is (
            FollowUpDisposition.UNAVAILABLE if bad_read == "unreadable"
            else FollowUpDisposition.REPAIR_REQUIRED)
        result = state.flow.reconcile(state.target, state.now)
        assert result.disposition is not PublicationRuntimeDisposition.COMPLETE
        work = state.d2.get_follow_up_work(rca_root_id("INC-1"))
        assert state.work.get(work.work_id).status is RuntimeWorkStatus.OUTSTANDING
    finally:
        _close_b1_public_authorities(state)
