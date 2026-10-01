"""S7 serialized admission, completion, and process-local restart races."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from rca_persistence import SqliteRcaStore
from runtime_orchestration.contracts import RuntimeWorkKind, RuntimeWorkStatus
from runtime_orchestration.rca_continuation import (
    RcaContinuationConcurrencyError, SqliteRcaContinuationStore,
)
from runtime_orchestration.rca_followup import FollowUpDisposition, RcaFollowUpOrchestrator
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore

from test_spec_016_followup_refresh import (
    INCIDENT, NOW, _B, _C, _post, _revision, env,
)


def _reopened_flow(state):
    d2 = SqliteRcaContinuationStore(state.d2_path)
    a = SqliteRcaStore(state.a_path)
    b = _B(state.b_path)
    flow = RcaFollowUpOrchestrator(
        incidents=state.incidents, candidate_a=a, candidate_b=b,
        candidate_c=_C(), continuations=d2, clock=state.time.clock(),
    )
    return flow, d2, a, b


def test_two_reopened_executors_merge_distinct_frontier_types_under_one_root(env):
    members = (_revision(env.material_a), _post(env.baseline))
    start = Barrier(2)

    def admit(member):
        flow, d2, a, b = _reopened_flow(env)
        try:
            start.wait(timeout=10)
            return flow.admit(INCIDENT, member)
        finally:
            d2.close(); a.close(); b.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(admit, members[0])
        future_b = pool.submit(admit, members[1])
        results = (future_a.result(timeout=15), future_b.result(timeout=15))
    assert all(item.disposition is FollowUpDisposition.OUTSTANDING for item in results)
    assert {item.continuation.follow_up_root_id for item in results} == {
        env.flow.follow_up_root(INCIDENT)}
    with SqliteRcaContinuationStore(env.d2_path) as reopened:
        assert set(reopened.get(env.root).unresolved_frontier) == set(members)
        assert reopened.get_follow_up_work(env.root).status is RuntimeWorkStatus.OUTSTANDING
    with SqliteRuntimeWorkStore(env.d2_path) as work:
        outstanding = tuple(row for row in work.enumerate_outstanding().records
                            if row.work_kind in (RuntimeWorkKind.RCA_INITIAL,
                                                 RuntimeWorkKind.RCA_FOLLOW_UP))
        assert len(outstanding) == 1


def test_stale_completion_from_prior_process_cannot_erase_new_admission(env):
    checked = env.flow.recheck(INCIDENT)
    old_work = env.d2.get_follow_up_work(env.root)
    member = _revision(env.material_a)
    flow, reopened, a, b = _reopened_flow(env)
    try:
        admitted = flow.admit(INCIDENT, member)
        assert admitted.disposition is FollowUpDisposition.OUTSTANDING
    finally:
        reopened.close(); a.close(); b.close()
    with pytest.raises(RcaContinuationConcurrencyError):
        env.d2.complete_rca_follow_up_if_frontier_unchanged(
            env.root, expected_work_revision=old_work.revision,
            expected_frontier_revision=checked.continuation.revision,
            observed_at=NOW + timedelta(minutes=1),
        )
    with SqliteRcaContinuationStore(env.d2_path) as after_restart:
        assert after_restart.get(env.root).unresolved_frontier == (member,)
        assert after_restart.get_follow_up_work(env.root).status is RuntimeWorkStatus.OUTSTANDING


def test_restart_fences_active_provider_before_a_handoff_and_recovers_durable_result(tmp_path):
    from threading import Event
    from runtime_orchestration.rca_execution import RcaExecutionDisposition
    from runtime_orchestration.identity import runtime_work_id
    from runtime_orchestration.rca_continuation import rca_root_id
    from test_spec_016_crash_restart import _execution_setup, _execution_instances, _close_execution
    from test_spec_016_runtime_host import _host, _Incidents
    from types import SimpleNamespace
    entered, release = Event(), Event()
    class PausedProvider:
        def __init__(self, delegate): self.delegate = delegate
        def __getattr__(self, name): return getattr(self.delegate, name)
        def invoke_once(self, request):
            entered.set()
            assert release.wait(timeout=15)
            return self.delegate.invoke_once(request)
    old_holder = []
    def execute_old():
        old = _execution_setup(tmp_path, PausedProvider)
        old_holder.append(old)
        try:
            return old.executor.step(old.request)
        finally:
            _close_execution(old)
    root_work_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, rca_root_id("INC-1"))
    fresh = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(execute_old)
        try:
            assert entered.wait(timeout=10)
            fresh = _execution_instances(tmp_path)
            old_authority = fresh.work.get(root_work_id)
            class Actions:
                def reconcile_publication(self, *args):
                    pytest.fail("no publication is admitted yet")
                def advance(self, subject, observed_at):
                    result = fresh.executor.step(fresh.request)
                    return result.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
            host, _ = _host(incidents=_Incidents(("INC-1",)), a=fresh.a,
                b=fresh.b, c=fresh.c_store, d=fresh.d, d2=fresh.d2,
                runtime_work=fresh.work, actions=Actions())
            host.recover(SimpleNamespace(runtime_work=fresh.work.enumerate_all()))
            host.run_cycle(should_stop=lambda: False)
            assert fresh.work.get(root_work_id).revision > old_authority.revision
            release.set()
            stale = running.result(timeout=10)
            assert stale.disposition is RcaExecutionDisposition.REPAIR_REQUIRED
            # The already authorized in-flight D operation may persist its result.
            assert len(fresh.d.recovery().value.results) == 1
            assert fresh.a.get_attempt_lineage("attempt:execution").try_outcomes == ()
            assert fresh.a.get_version_history("aggregate:execution") == ()
            assert fresh.work.get(root_work_id).status is RuntimeWorkStatus.OUTSTANDING
            budget = tuple(row for row in fresh.work.enumerate_all().records
                           if row.work_kind is RuntimeWorkKind.RCA_ATTEMPT)[0]
            assert budget.status is RuntimeWorkStatus.OUTSTANDING
            assert len(old_holder[0].provider.calls) == 1
        finally:
            release.set()
            running.result(timeout=10)
            if fresh is not None: _close_execution(fresh)
    del old_holder, fresh, host
    restarted = _execution_instances(tmp_path)
    try:
        result = restarted.executor.step(restarted.request)
        assert result.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
        assert restarted.provider.calls == 0
        assert len(restarted.a.get_attempt_lineage("attempt:execution").try_outcomes) == 1
        assert len(restarted.a.get_version_history("aggregate:execution")) == 1
        restored = tuple(row for row in restarted.work.enumerate_all().records
                         if row.work_kind is RuntimeWorkKind.RCA_ATTEMPT)[0]
        assert (restored.attempt_count, restored.last_attempt_at, restored.next_retry_at) == (
            budget.attempt_count, budget.last_attempt_at, budget.next_retry_at)
    finally:
        _close_execution(restarted)
