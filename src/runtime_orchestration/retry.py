"""D8 bounded retry timing over domain-owned retry dispositions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
import math
from typing import Protocol

from alert_correlation.state import RetryDisposition
from alert_correlation.state import CorrelationMutationIntent

from .clock import RuntimeClock, canonical_utc
from .contracts import (
    RuntimeWorkKind,
    RuntimeWorkEnumeration,
    RuntimeWorkRecord,
    RuntimeWorkStatus,
    RuntimeWorkStore,
)
from .identity import runtime_work_id
from .sqlite_work_store import ContradictoryRuntimeWorkError
from .telemetry import NullRuntimeTelemetry, RuntimeTelemetry, RuntimeTelemetryEvent


class DomainRetryFailure(Protocol):
    code: Enum
    retry_disposition: RetryDisposition


@dataclass(frozen=True, slots=True)
class RetryEligibility:
    eligible: tuple[RuntimeWorkRecord, ...]
    next_eligibility: datetime | None


class RuntimeRetryIntegrityError(RuntimeError):
    pass


class DurableRetryController:
    """Persist timing while preserving the source domain's exact taxonomy."""

    def __init__(
        self,
        *,
        work_store: RuntimeWorkStore,
        retry_delays_seconds: tuple[float, ...],
        clock: RuntimeClock,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        if not retry_delays_seconds or any(
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or delay <= 0
            for delay in retry_delays_seconds
        ):
            raise ValueError("retry delays must be positive finite numbers")
        if not isinstance(clock, RuntimeClock):
            raise TypeError("clock must be RuntimeClock")
        if any(
            not callable(getattr(work_store, method, None))
            for method in ("enumerate_all", "update")
        ):
            raise TypeError("work_store lacks its Runtime public API")
        self._work_store = work_store
        self._delays = tuple(float(delay) for delay in retry_delays_seconds)
        self._clock = clock
        self._telemetry = telemetry or NullRuntimeTelemetry()

    @property
    def automatic_retry_limit(self) -> int:
        return len(self._delays)

    @property
    def domain_attempt_limit(self) -> int:
        return self.automatic_retry_limit + 1

    def establish_rca_attempt_budget(
        self, *, root_id: str, attempt_id: str, incident_id: str, event_id: str,
    ) -> RuntimeWorkRecord:
        """Use the existing D2 work authority for one Attempt's shared slots."""
        work_id = runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT, root_id, attempt_id)
        work = self._work_store.get(work_id)
        if work is None:
            now = self._clock.now()
            proposal = RuntimeWorkRecord(
                work_id, RuntimeWorkKind.RCA_ATTEMPT, event_id, "INITIAL", "EXECUTE_TRY",
                0, self.automatic_retry_limit, RuntimeWorkStatus.OUTSTANDING,
                now, now, now, incident_id=incident_id, operation_id=attempt_id,
                workflow_operation_id=root_id,
            )
            try:
                work = self._work_store.create(proposal)
            except ContradictoryRuntimeWorkError:
                # A competing initial executor may have committed this subject.
                work = self._work_store.get(work_id)
                if work is None:
                    raise
        if (work.work_id != work_id or work.work_kind is not RuntimeWorkKind.RCA_ATTEMPT
            or work.incident_id != incident_id or work.event_id != event_id
            or work.operation_id != attempt_id or work.workflow_operation_id != root_id
            or work.retry_limit != self.automatic_retry_limit):
            raise RuntimeRetryIntegrityError("Attempt retry budget binding contradicts D2")
        return work

    def mark_rca_initial_invoking(self, work: RuntimeWorkRecord) -> RuntimeWorkRecord:
        if (work.work_kind is not RuntimeWorkKind.RCA_ATTEMPT or work.stage != "INITIAL"
            or work.attempt_count != 0 or work.status is not RuntimeWorkStatus.OUTSTANDING):
            raise RuntimeRetryIntegrityError("initial physical invocation is not eligible")
        now = self._clock.now()
        return self._work_store.update(replace(work, stage="INITIAL_INVOKING",
            updated_at=now, observed_at=now), expected_revision=work.revision)

    def schedule_rca_failure(self, work: RuntimeWorkRecord, error: DomainRetryFailure) -> RuntimeWorkRecord:
        disposition = getattr(error, "retry_safety", None)
        code = getattr(error, "failure_class", None)
        if (work.work_kind is not RuntimeWorkKind.RCA_ATTEMPT
            or work.stage not in {"INITIAL_INVOKING", "RETRY_INVOKING"}
            or work.status is not RuntimeWorkStatus.OUTSTANDING
            or not isinstance(disposition, Enum) or disposition.value not in
                {"RETRYABLE", "NON_RETRYABLE", "REPAIR_REQUIRED"}
            or not isinstance(code, Enum)):
            raise RuntimeRetryIntegrityError("invalid Candidate-D retry scheduling authority")
        now = self._clock.now()
        retryable = disposition.value == "RETRYABLE"
        exhausted = retryable and work.attempt_count == work.retry_limit
        status = (RuntimeWorkStatus.EXHAUSTED if exhausted else
                  RuntimeWorkStatus.OUTSTANDING if retryable else RuntimeWorkStatus.FAILED_CLOSED)
        due = (now + timedelta(seconds=self._delays[work.attempt_count])
               if retryable and not exhausted else None)
        return self._work_store.update(replace(work,
            stage="EXHAUSTED" if exhausted else "RETRY_PENDING" if retryable else "FAILED_CLOSED",
            next_action="RETRY" if retryable and not exhausted else "NONE",
            next_retry_at=due, last_attempt_at=now,
            source_domain="CANDIDATE_D", source_error_code=code.value,
            source_retry_disposition=disposition.value, status=status,
            updated_at=now, observed_at=now), expected_revision=work.revision)

    def consume_rca_retry_slot(self, work: RuntimeWorkRecord) -> RuntimeWorkRecord:
        now = self._clock.now()
        if (work.work_kind is not RuntimeWorkKind.RCA_ATTEMPT
            or work.stage != "RETRY_PENDING" or work.status is not RuntimeWorkStatus.OUTSTANDING
            or work.next_retry_at is None or now < work.next_retry_at
            or work.attempt_count >= work.retry_limit
            or work.source_retry_disposition != "RETRYABLE"):
            raise RuntimeRetryIntegrityError("Attempt retry slot is not eligible")
        return self._work_store.update(replace(work, stage="RETRY_INVOKING",
            next_action="EXECUTE_TRY", attempt_count=work.attempt_count + 1,
            next_retry_at=None, updated_at=now, observed_at=now),
            expected_revision=work.revision)

    def record_domain_failure(
        self,
        work: RuntimeWorkRecord,
        error: DomainRetryFailure,
        *,
        source_domain: str,
    ) -> RuntimeWorkRecord:
        disposition = getattr(error, "retry_disposition", None)
        code = getattr(error, "code", None)
        if not isinstance(disposition, RetryDisposition):
            raise RuntimeRetryIntegrityError(
                "domain failure lacks an authoritative RetryDisposition"
            )
        if not isinstance(code, Enum) or not isinstance(code.value, str):
            raise RuntimeRetryIntegrityError("domain failure lacks a closed error code")
        if work.status is not RuntimeWorkStatus.OUTSTANDING:
            raise RuntimeRetryIntegrityError("only outstanding work may record failure")
        if work.retry_limit != self.domain_attempt_limit:
            raise RuntimeRetryIntegrityError(
                "durable attempt ceiling contradicts configured retry budget"
            )

        failed_attempts = work.attempt_count + 1
        if failed_attempts > work.retry_limit:
            raise RuntimeRetryIntegrityError("durable retry budget was exceeded")
        now = self._clock.now()
        if disposition is RetryDisposition.RETRYABLE:
            if failed_attempts < work.retry_limit:
                status = RuntimeWorkStatus.OUTSTANDING
                stage = "RETRY_PENDING"
                next_retry = now + timedelta(
                    seconds=self._delays[failed_attempts - 1]
                )
            else:
                status = RuntimeWorkStatus.EXHAUSTED
                stage = "EXHAUSTED"
                next_retry = None
        else:
            status = RuntimeWorkStatus.FAILED_CLOSED
            stage = "FAILED_CLOSED"
            next_retry = None

        updated = self._work_store.update(
            replace(
                work,
                stage=stage,
                attempt_count=failed_attempts,
                next_retry_at=next_retry,
                last_attempt_at=now,
                source_domain=source_domain,
                source_error_code=code.value,
                source_retry_disposition=disposition.value,
                status=status,
                updated_at=now,
                observed_at=now,
            ),
            expected_revision=work.revision,
        )
        telemetry_event = (
            RuntimeTelemetryEvent.RETRY_EXHAUSTED
            if status is RuntimeWorkStatus.EXHAUSTED
            else RuntimeTelemetryEvent.RETRY_SCHEDULED
            if status is RuntimeWorkStatus.OUTSTANDING
            else RuntimeTelemetryEvent.WORK_UPDATED
        )
        self._telemetry.emit(
            telemetry_event,
            observed_at=now,
            work_id=updated.work_id,
            event_id=updated.event_id,
            stage=updated.stage,
            operation_id=updated.operation_id,
            workflow_operation_id=updated.workflow_operation_id,
            incident_id=updated.incident_id,
            source_domain=source_domain,
            source_error_code=code.value,
            retry_disposition=disposition,
            attempt=failed_attempts,
            next_eligibility=next_retry,
            status=status,
        )
        return updated

    def enumerate_eligibility(self, *, now: datetime | None = None) -> RetryEligibility:
        absolute_now = self._clock.now() if now is None else canonical_utc(now)
        enumeration = self._work_store.enumerate_all()
        if not isinstance(enumeration, RuntimeWorkEnumeration):
            raise RuntimeRetryIntegrityError("D2 returned an invalid enumeration")
        if enumeration.isolated_corruptions:
            raise RuntimeRetryIntegrityError(
                "corrupt retry continuity cannot be safely enumerated"
            )
        eligible: list[RuntimeWorkRecord] = []
        future: list[datetime] = []
        for work in enumeration.records:
            if work.work_kind is RuntimeWorkKind.RCA_ATTEMPT:
                # S3 steps use this same D2 work record, but RCA dispatch is
                # not wired into the existing worker until a later slice.
                continue
            if work.status is not RuntimeWorkStatus.OUTSTANDING:
                continue
            if work.source_retry_disposition not in (None, RetryDisposition.RETRYABLE.value):
                raise RuntimeRetryIntegrityError(
                    "automatic work contradicts its domain retry disposition"
                )
            if work.next_retry_at is None or work.next_retry_at <= absolute_now:
                eligible.append(work)
            else:
                future.append(work.next_retry_at)
        return RetryEligibility(
            tuple(sorted(eligible, key=lambda item: item.work_id)),
            min(future) if future else None,
        )


class TerminalFailureRecorder:
    """Attach D2 retry continuity to the authoritative durable Intent."""

    def __init__(
        self,
        *,
        work_store: RuntimeWorkStore,
        retry: DurableRetryController,
        clock: RuntimeClock,
    ) -> None:
        self._work_store = work_store
        self._retry = retry
        self._clock = clock

    def record_terminal_failure(
        self,
        intent: CorrelationMutationIntent,
        error: DomainRetryFailure,
        *,
        source_domain: str,
    ) -> RuntimeWorkRecord:
        if not isinstance(intent, CorrelationMutationIntent):
            raise TypeError("intent must be the authoritative SPEC-007 type")
        work_id = runtime_work_id(
            RuntimeWorkKind.DOMAIN_OPERATION, intent.event_id, intent.operation_id
        )
        work = self._work_store.get(work_id)
        if work is None:
            now = self._clock.now()
            work = self._work_store.create(
                RuntimeWorkRecord(
                    work_id=work_id,
                    work_kind=RuntimeWorkKind.DOMAIN_OPERATION,
                    event_id=intent.event_id,
                    operation_id=intent.operation_id,
                    stage="DOMAIN_ATTEMPT",
                    next_action="RESUME_CORRELATION_INTENT",
                    attempt_count=0,
                    retry_limit=self._retry.domain_attempt_limit,
                    status=RuntimeWorkStatus.OUTSTANDING,
                    created_at=now,
                    updated_at=now,
                    observed_at=now,
                )
            )
        if (
            work.work_kind is not RuntimeWorkKind.DOMAIN_OPERATION
            or work.event_id != intent.event_id
            or work.operation_id != intent.operation_id
        ):
            raise RuntimeRetryIntegrityError(
                "terminal retry work contradicts authoritative Intent"
            )
        return self._retry.record_domain_failure(
            work, error, source_domain=source_domain
        )
