"""SPEC-016 S3 public A/D composition and shared SPEC-011 D2 retry budget."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest

from llm_generation.facade import CandidateDHandoffFacade
from llm_generation.service import GenerationService
from llm_generation.contracts import FailureClass, GenerationFailure, LocalReadStatus
from llm_generation.service import (ExecutionAuthorization, ProviderFailureKind,
    ProviderInvocationError, subject_input_commitment)
from rca_persistence.contracts import (AdmitAttemptRequest, AdmittedRetryDisposition,
    CreateAggregateRequest, LogicalTryIdentity, LogicalTryOutcome,
    LogicalTryResultKind, VersionRole)
from rca_persistence.sqlite_store import SqliteRcaStore
from runtime_orchestration.clock import RuntimeClock
from runtime_orchestration.contracts import RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus
from runtime_orchestration.identity import runtime_operation_id, runtime_work_id
from runtime_orchestration.rca_continuation import rca_root_id
from runtime_orchestration.rca_execution import (RcaAttemptExecutor, RcaExecutionDisposition,
    RcaExecutionRequest)
from runtime_orchestration.retry import DurableRetryController
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore

from test_llm_generation_phase2 import case
from test_llm_generation_service import environment


NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


class _Time:
    def __init__(self): self.value = NOW
    def clock(self): return RuntimeClock(lambda: self.value, lambda: 0.0, lambda _: None)
    def advance(self, seconds): self.value += timedelta(seconds=seconds)


@pytest.fixture
def flow(case, tmp_path):
    a = SqliteRcaStore(tmp_path / "a.sqlite3")
    lineage = case.attempts.get_attempt_lineage("attempt-1").attempt.lineage
    a.create_or_discover_aggregate(CreateAggregateRequest(
        "aggregate-setup", lineage.aggregate_id, "INC-1", NOW))
    a.admit_attempt(AdmitAttemptRequest("attempt-setup", lineage, NOW))
    case.attempts = a
    service, provider, _, resources = environment(case, tmp_path / "d")
    facade = CandidateDHandoffFacade(service.store, a, case.evidence, case.knowledge)
    work = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    root = rca_root_id("INC-1")
    work.create(RuntimeWorkRecord(
        runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root), RuntimeWorkKind.RCA_INITIAL,
        "EVT-1", "INITIAL", "INITIAL", 0, 4, RuntimeWorkStatus.OUTSTANDING,
        NOW, NOW, NOW, incident_id="INC-1", operation_id=root))
    time = _Time()
    retry = DurableRetryController(work_store=work, retry_delays_seconds=(1, 2, 4, 8),
                                   clock=time.clock())
    executor = RcaAttemptExecutor(candidate_a=a, candidate_d=service,
        d_store=service.store, handoff=facade, work_store=work, retry=retry,
        clock=time.clock())
    request = RcaExecutionRequest("INC-1", "attempt-1", case.content.input,
                                  case.config, resources)
    try:
        yield case, a, service, provider, work, time, retry, executor, request
    finally:
        work.close(); a.close()


def test_success_handoff_commits_one_unpublished_version_without_retry_slot(flow):
    case, a, service, provider, work, _, _, executor, request = flow
    result = executor.step(request)
    assert result.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
    assert result.retry_slots_used == 0 and len(provider.calls) == 1
    assert result.version.role is VersionRole.COMMITTED_UNPUBLISHED
    assert a.get_attempt_lineage("attempt-1").try_outcomes[0].validated_result_id
    assert a.get_artifact(result.version.version_id) == result.version.artifact
    assert a.get_current("aggregate-1") is None
    assert work.get(runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT,
        rca_root_id("INC-1"), "attempt-1")).status is RuntimeWorkStatus.COMPLETED
    replay = executor.step(request)
    assert replay.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
    assert replay.version == result.version and len(provider.calls) == 1


def test_retry_slot_waits_absolute_time_and_is_consumed_before_second_invocation(flow):
    _, a, service, provider, work, time, _, executor, request = flow
    provider.outcome = ProviderInvocationError(ProviderFailureKind.TIMEOUT)
    first = executor.step(request)
    assert first.disposition is RcaExecutionDisposition.RETRY_PENDING
    assert first.retry_slots_used == 0 and first.next_eligibility_at == NOW + timedelta(seconds=1)
    assert len(provider.calls) == 1
    assert executor.step(request).disposition is RcaExecutionDisposition.RETRY_PENDING
    assert len(provider.calls) == 1
    time.advance(1)
    second = executor.step(request)
    assert second.disposition is RcaExecutionDisposition.RETRY_PENDING
    assert second.retry_slots_used == 1 and len(provider.calls) == 2
    budget = work.get(runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT,
                                      rca_root_id("INC-1"), "attempt-1"))
    assert budget.attempt_count == 1
    assert a.get_attempt_lineage("attempt-1").try_outcomes == ()


def _seed_durable_failure(flow, *, safety=AdmittedRetryDisposition.RETRYABLE,
                          same_try=False):
    _, _, service, _, _, _, retry, executor, request = flow
    root = rca_root_id("INC-1")
    budget = retry.establish_rca_attempt_budget(root_id=root, attempt_id="attempt-1",
        incident_id="INC-1", event_id="EVT-1")
    retry.mark_rca_initial_invoking(budget)
    source = replace(request.source, operation_id=runtime_operation_id(
        "RCA_D_EXECUTION", root, "attempt-1", "1", "0"))
    failure = GenerationFailure(LogicalTryIdentity("attempt-1", 1), source.operation_id,
        FailureClass.PROVIDER_TIMEOUT if safety is AdmittedRetryDisposition.RETRYABLE
        else FailureClass.CAPABILITY_MISMATCH if safety is AdmittedRetryDisposition.NON_RETRYABLE
        else FailureClass.IDENTITY_CONTRADICTION,
        safety, "typed failure", subject_input_commitment(source), same_try)
    assert service.store.commit_failure(failure).status is LocalReadStatus.FOUND
    return failure


def test_unsafe_same_try_records_a_failure_before_next_try_and_consumes_one_slot(flow):
    _, a, _, provider, work, time, _, executor, request = flow
    _seed_durable_failure(flow, same_try=False)
    pending = executor.step(request)
    assert pending.disposition is RcaExecutionDisposition.RETRY_PENDING
    assert len(a.get_attempt_lineage("attempt-1").try_outcomes) == 1
    assert provider.calls == []
    time.advance(1)
    result = executor.step(request)
    assert result.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
    assert result.retry_slots_used == 1 and len(provider.calls) == 1
    outcomes = a.get_attempt_lineage("attempt-1").try_outcomes
    assert [item.identity.try_ordinal for item in outcomes] == [1, 2]
    assert outcomes[0].retry_disposition is AdmittedRetryDisposition.RETRYABLE
    assert work.get(runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT,
        rca_root_id("INC-1"), "attempt-1")).attempt_count == 1


def test_four_shared_slots_exhaust_without_fifth_retry_or_new_try(flow):
    _, a, _, provider, work, time, _, executor, request = flow
    provider.outcome = ProviderInvocationError(ProviderFailureKind.TIMEOUT)
    assert executor.step(request).retry_slots_used == 0
    for slot, delay in enumerate((1, 2, 4, 8), start=1):
        time.advance(delay)
        result = executor.step(request)
        assert result.retry_slots_used == slot
    assert result.disposition is RcaExecutionDisposition.EXHAUSTED
    assert result.source_disposition is AdmittedRetryDisposition.RETRYABLE
    assert len(provider.calls) == 5
    assert executor.step(request).disposition is RcaExecutionDisposition.EXHAUSTED
    assert len(provider.calls) == 5
    assert [item.identity.try_ordinal for item in a.get_attempt_lineage("attempt-1").try_outcomes] == [1]


def test_reopen_preserves_absolute_eligibility_and_consumed_count(flow, tmp_path):
    case, a, service, provider, work, time, _, executor, request = flow
    provider.outcome = ProviderInvocationError(ProviderFailureKind.TIMEOUT)
    first = executor.step(request)
    assert first.next_eligibility_at == NOW + timedelta(seconds=1)
    work.close()
    reopened = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    retry = DurableRetryController(work_store=reopened, retry_delays_seconds=(1, 2, 4, 8),
                                   clock=time.clock())
    restarted = RcaAttemptExecutor(candidate_a=a, candidate_d=service,
        d_store=service.store, handoff=CandidateDHandoffFacade(service.store, a,
        case.evidence, case.knowledge), work_store=reopened, retry=retry, clock=time.clock())
    assert restarted.step(request).next_eligibility_at == first.next_eligibility_at
    time.advance(1)
    assert restarted.step(request).retry_slots_used == 1
    assert reopened.get(runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT,
        rca_root_id("INC-1"), "attempt-1")).attempt_count == 1
    reopened.close()


def test_distinct_attempts_have_distinct_d2_retry_budget_subjects(flow):
    _, _, _, _, work, _, retry, _, _ = flow
    root = rca_root_id("INC-1")
    first = retry.establish_rca_attempt_budget(root_id=root, attempt_id="attempt-1",
        incident_id="INC-1", event_id="EVT-1")
    retry.mark_rca_initial_invoking(first)
    second = retry.establish_rca_attempt_budget(root_id=root, attempt_id="attempt-2",
        incident_id="INC-1", event_id="EVT-1")
    assert first.work_id != second.work_id
    assert second.attempt_count == 0 and second.workflow_operation_id == root
    assert len([item for item in work.enumerate_all().records
                if item.work_kind is RuntimeWorkKind.RCA_ATTEMPT]) == 2
    assert all(item.work_kind is not RuntimeWorkKind.RCA_ATTEMPT
               for item in retry.enumerate_eligibility().eligible)


def test_non_retryable_failure_reconciles_a_and_stops(flow):
    _, a, _, provider, _, _, _, executor, request = flow
    _seed_durable_failure(flow, safety=AdmittedRetryDisposition.NON_RETRYABLE)
    result = executor.step(request)
    assert result.disposition is RcaExecutionDisposition.NON_RETRYABLE
    assert result.source_disposition is AdmittedRetryDisposition.NON_RETRYABLE
    assert len(a.get_attempt_lineage("attempt-1").try_outcomes) == 1
    replay = executor.step(request)
    assert replay.disposition is RcaExecutionDisposition.NON_RETRYABLE
    assert replay.source_disposition is AdmittedRetryDisposition.NON_RETRYABLE
    assert provider.calls == []


def test_authoritative_repair_required_try_remains_terminal_after_restart(flow, tmp_path):
    case, a, service, provider, work, time, _, executor, request = flow
    failure = _seed_durable_failure(flow, safety=AdmittedRetryDisposition.REPAIR_REQUIRED)
    identity = LogicalTryIdentity("attempt-1", 1)
    authoritative = a.record_try_outcome("terminal-repair-outcome", LogicalTryOutcome(
        identity, LogicalTryResultKind.FAILURE, AdmittedRetryDisposition.REPAIR_REQUIRED,
        time.value, failure_code=failure.failure_class.value))
    budget_id = runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT, rca_root_id("INC-1"), "attempt-1")
    before = work.get(budget_id)
    result = executor.step(request)
    assert result.disposition is RcaExecutionDisposition.REPAIR_REQUIRED
    assert result.source_disposition is AdmittedRetryDisposition.REPAIR_REQUIRED
    assert result.retry_slots_used == 0
    assert a.get_attempt_lineage("attempt-1").try_outcomes == (authoritative,)
    assert work.get(budget_id) == before and provider.calls == []
    assert a.get_version_history("aggregate-1") == ()

    a.close(); work.close()
    reopened_a = SqliteRcaStore(tmp_path / "a.sqlite3")
    reopened_work = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    try:
        reopened_service = GenerationService(service.store, provider, reopened_a,
            case.evidence, case.knowledge)
        reopened = RcaAttemptExecutor(candidate_a=reopened_a, candidate_d=reopened_service,
            d_store=service.store, handoff=CandidateDHandoffFacade(service.store,
            reopened_a, case.evidence, case.knowledge), work_store=reopened_work,
            retry=DurableRetryController(work_store=reopened_work,
            retry_delays_seconds=(1, 2, 4, 8), clock=time.clock()), clock=time.clock())
        replay = reopened.step(request)
        assert replay.disposition is RcaExecutionDisposition.REPAIR_REQUIRED
        assert replay.source_disposition is AdmittedRetryDisposition.REPAIR_REQUIRED
        assert replay.retry_slots_used == 0
        assert reopened_a.get_attempt_lineage("attempt-1").try_outcomes == (authoritative,)
        assert reopened_work.get(budget_id) == before and provider.calls == []
        assert reopened_a.get_version_history("aggregate-1") == ()
    finally:
        reopened_work.close(); reopened_a.close()


def test_repair_required_failure_never_invokes_or_advances_try(flow):
    _, a, _, provider, _, _, _, executor, request = flow
    _seed_durable_failure(flow, safety=AdmittedRetryDisposition.REPAIR_REQUIRED)
    result = executor.step(request)
    assert result.disposition is RcaExecutionDisposition.REPAIR_REQUIRED
    assert a.get_attempt_lineage("attempt-1").try_outcomes == ()
    assert provider.calls == []


def test_durable_d_result_recovers_a_outcome_and_artifact_without_reinvocation(flow):
    case, a, service, provider, _, _, retry, executor, request = flow
    root = rca_root_id("INC-1")
    budget = retry.establish_rca_attempt_budget(root_id=root, attempt_id="attempt-1",
        incident_id="INC-1", event_id="EVT-1")
    retry.mark_rca_initial_invoking(budget)
    source = replace(request.source, operation_id=runtime_operation_id(
        "RCA_D_EXECUTION", root, "attempt-1", "1", "0"))
    grant = ExecutionAuthorization(source.operation_id, source.try_identity, 0, True)
    durable = service.execute(source, request.config, grant, request.resources)
    assert durable.result is not None and len(provider.calls) == 1
    recovered = executor.step(request)
    assert recovered.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
    assert recovered.retry_slots_used == 0 and len(provider.calls) == 1
    assert a.get_version_history("aggregate-1") == (recovered.version,)


def test_response_loss_after_a_artifact_commit_replays_same_version(flow):
    case, a, service, provider, work, time, retry, _, request = flow
    class LostResponse:
        lost = False
        def __getattr__(self, name): return getattr(a, name)
        def commit_validated_artifact(self, *args):
            version = a.commit_validated_artifact(*args)
            if not self.lost:
                self.lost = True
                raise OSError("response lost after durable A commit")
            return version
    proxy = LostResponse()
    facade = CandidateDHandoffFacade(service.store, a, case.evidence, case.knowledge)
    executor = RcaAttemptExecutor(candidate_a=proxy, candidate_d=service,
        d_store=service.store, handoff=facade, work_store=work, retry=retry,
        clock=time.clock())
    assert executor.step(request).disposition is RcaExecutionDisposition.UNAVAILABLE
    versions = a.get_version_history("aggregate-1")
    assert len(versions) == 1 and len(provider.calls) == 1
    replay = executor.step(request)
    assert replay.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
    assert replay.version == versions[0] and a.get_version_history("aggregate-1") == versions
    assert len(provider.calls) == 1


def test_newer_current_marks_attempt_obsolete_before_budget_or_provider(flow):
    case, a, service, provider, work, time, retry, _, request = flow
    class NewerCurrent:
        def __getattr__(self, name): return getattr(a, name)
        def get_current(self, aggregate_id):
            return SimpleNamespace(current=SimpleNamespace(freshness=SimpleNamespace(value="FRESH")))
    executor = RcaAttemptExecutor(candidate_a=NewerCurrent(), candidate_d=service,
        d_store=service.store, handoff=CandidateDHandoffFacade(service.store, a,
        case.evidence, case.knowledge), work_store=work, retry=retry,
        clock=time.clock())
    assert executor.step(request).disposition is RcaExecutionDisposition.OBSOLETE
    assert provider.calls == []
    assert not any(item.work_kind is RuntimeWorkKind.RCA_ATTEMPT
                   for item in work.enumerate_all().records)


def test_next_try_then_same_try_share_one_counter_and_absolute_schedule(flow):
    _, a, _, provider, work, time, _, executor, request = flow
    _seed_durable_failure(flow, same_try=False)
    assert executor.step(request).disposition is RcaExecutionDisposition.RETRY_PENDING
    provider.outcome = ProviderInvocationError(ProviderFailureKind.TIMEOUT)
    time.advance(1)
    next_try = executor.step(request)
    assert next_try.retry_slots_used == 1
    assert len(a.get_attempt_lineage("attempt-1").try_outcomes) == 1
    assert next_try.next_eligibility_at == NOW + timedelta(seconds=3)
    time.advance(2)
    same_try = executor.step(request)
    assert same_try.retry_slots_used == 2
    assert len(a.get_attempt_lineage("attempt-1").try_outcomes) == 1
    assert len(provider.calls) == 2
    assert work.get(runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT,
        rca_root_id("INC-1"), "attempt-1")).attempt_count == 2


def test_a_outcome_permanently_forbids_same_try_reinvocation(flow):
    _, a, _, provider, _, time, _, executor, request = flow
    provider.outcome = ProviderInvocationError(ProviderFailureKind.TIMEOUT)
    assert executor.step(request).disposition is RcaExecutionDisposition.RETRY_PENDING
    a.record_try_outcome("record-prior-retryable", LogicalTryOutcome(
        LogicalTryIdentity("attempt-1", 1), LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.RETRYABLE, time.value,
        failure_code="PROVIDER_TIMEOUT"))
    time.advance(1)
    result = executor.step(request)
    assert result.retry_slots_used == 1
    assert [item.identity.try_ordinal for item in a.get_attempt_lineage("attempt-1").try_outcomes] == [1]
    assert len(provider.calls) == 2
    assert executor.step(request).retry_slots_used == 1


def test_missing_attempt_budget_with_durable_d_failure_fails_closed(flow):
    _, a, service, provider, _, _, _, executor, request = flow
    root = rca_root_id("INC-1")
    source = replace(request.source, operation_id=runtime_operation_id(
        "RCA_D_EXECUTION", root, "attempt-1", "1", "0"))
    failure = GenerationFailure(source.try_identity, source.operation_id,
        FailureClass.PROVIDER_TIMEOUT, AdmittedRetryDisposition.RETRYABLE,
        "durable timeout", subject_input_commitment(source), True)
    assert service.store.commit_failure(failure).status is LocalReadStatus.FOUND
    assert executor.step(request).disposition is RcaExecutionDisposition.REPAIR_REQUIRED
    assert provider.calls == [] and a.get_attempt_lineage("attempt-1").try_outcomes == ()


def test_concurrent_same_try_has_one_consumed_slot_and_one_d_effect(flow, tmp_path):
    case, a, service, provider, work, time, _, executor, request = flow
    provider.outcome = ProviderInvocationError(ProviderFailureKind.TIMEOUT)
    assert executor.step(request).disposition is RcaExecutionDisposition.RETRY_PENDING
    time.advance(1)
    barrier = Barrier(2)
    def run(_):
        local_a = SqliteRcaStore(tmp_path / "a.sqlite3")
        local_work = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
        local_retry = DurableRetryController(work_store=local_work,
            retry_delays_seconds=(1, 2, 4, 8), clock=time.clock())
        local_service = GenerationService(service.store, provider, local_a,
            case.evidence, case.knowledge)
        local_executor = RcaAttemptExecutor(candidate_a=local_a, candidate_d=local_service,
            d_store=service.store, handoff=CandidateDHandoffFacade(service.store,
            local_a, case.evidence, case.knowledge), work_store=local_work,
            retry=local_retry, clock=time.clock())
        try:
            barrier.wait(timeout=10)
            return local_executor.step(request)
        finally:
            local_work.close(); local_a.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert len(provider.calls) == 2  # Initial invocation plus one Same-Try retry.
    assert work.get(runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT,
        rca_root_id("INC-1"), "attempt-1")).attempt_count == 1
    assert all(item.disposition in (RcaExecutionDisposition.RETRY_PENDING,
                                    RcaExecutionDisposition.REPAIR_REQUIRED) for item in results)
