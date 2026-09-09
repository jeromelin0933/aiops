from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from alert_correlation.state.contracts import RetryDisposition
from shadow_management.contracts import (
    SHADOW_ERROR_RETRY_DISPOSITIONS,
    ShadowDomainErrorCode,
    ShadowDomainValidationError,
    ShadowReason,
    ShadowRecord,
    ShadowReviewStatus,
    retry_disposition_for_shadow_error,
    validate_shadow_creation_reason,
)


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def test_shadow_record_is_seven_field_frozen_domain_contract() -> None:
    record = ShadowRecord("SHADOW-1", "EVT-1", NOW, ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY, ShadowReviewStatus.UNREVIEWED, "POLICY-GENERAL-LOG-ANOMALY", "1.0")
    assert tuple(record.__dataclass_fields__) == ("shadow_id", "event_id", "entered_shadow_at", "reason", "review_status", "policy_id", "policy_version")
    with pytest.raises(FrozenInstanceError):
        record.shadow_id = "SHADOW-2"  # type: ignore[misc]


@pytest.mark.parametrize("kwargs", [
    {"shadow_id": "", "event_id": "EVT-1", "entered_shadow_at": NOW, "reason": ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY, "review_status": ShadowReviewStatus.UNREVIEWED, "policy_id": "P", "policy_version": "1"},
    {"shadow_id": "S", "event_id": "EVT-1", "entered_shadow_at": datetime(2026, 9, 9), "reason": ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY, "review_status": ShadowReviewStatus.UNREVIEWED, "policy_id": "P", "policy_version": "1"},
])
def test_shadow_record_rejects_invalid_required_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ShadowDomainValidationError):
        ShadowRecord(**kwargs)  # type: ignore[arg-type]


def test_review_status_is_unreviewed_only() -> None:
    assert list(ShadowReviewStatus) == [ShadowReviewStatus.UNREVIEWED]
    with pytest.raises(ShadowDomainValidationError):
        ShadowRecord("S", "E", NOW, ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY, "REVIEWED", "P", "1")  # type: ignore[arg-type]


def test_reason_vocabulary_and_current_creation_gate_are_separate() -> None:
    assert set(ShadowReason) == {
        ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY,
        ShadowReason.NO_COMPATIBLE_INCIDENT,
        ShadowReason.MULTIPLE_COMPATIBLE_INCIDENTS,
    }
    validate_shadow_creation_reason(ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY)
    for reason in (ShadowReason.NO_COMPATIBLE_INCIDENT, ShadowReason.MULTIPLE_COMPATIBLE_INCIDENTS):
        with pytest.raises(ShadowDomainValidationError):
            validate_shadow_creation_reason(reason)


def test_all_nine_error_dispositions_are_closed_and_exact() -> None:
    expected = {
        ShadowDomainErrorCode.INVALID_SHADOW_MUTATION: RetryDisposition.NON_RETRYABLE,
        ShadowDomainErrorCode.SHADOW_NOT_FOUND: RetryDisposition.NON_RETRYABLE,
        ShadowDomainErrorCode.SHADOW_EVENT_OWNERSHIP_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.INCIDENT_EVENT_OWNERSHIP_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.MUTATION_RECEIPT_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.TRANSIENT_SHADOW_STORE_FAILURE: RetryDisposition.RETRYABLE,
    }
    assert dict(SHADOW_ERROR_RETRY_DISPOSITIONS) == expected
    assert {code: retry_disposition_for_shadow_error(code) for code in ShadowDomainErrorCode} == expected
    with pytest.raises(TypeError):
        retry_disposition_for_shadow_error("INVALID_SHADOW_MUTATION")  # type: ignore[arg-type]
