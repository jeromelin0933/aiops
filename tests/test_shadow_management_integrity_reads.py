from datetime import datetime, timezone

import pytest

from alert_correlation.contracts import AnchorTransition, CorrelationFamily, DecisionReasonCode, DecisionType
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY
from alert_correlation.state.contracts import CorrelationMutationIntent, TerminalOutcome
from shadow_management.contracts import ShadowDomainErrorCode, ShadowReason, ShadowReviewStatus
from shadow_management.manager import ShadowManager
from shadow_management.sqlite_store import ShadowStoreIntegrityError, SqliteShadowStore
from tests._shadow_store_testkit import (
    corrupt_receipt_result_timestamp,
    corrupt_receipt_event_id,
    insert_second_receipt_for_shadow,
    remove_receipt_for_shadow,
)


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


class _NoIncidentOwner:
    def event_has_incident_owner(self, _event_id: str) -> bool:
        return False


def _manager(database) -> tuple[SqliteShadowStore, ShadowManager]:
    store = SqliteShadowStore(database)
    return store, ShadowManager(store, DEFAULT_POLICY_REGISTRY, _NoIncidentOwner())


def _intent(operation_id: str, event_id: str) -> CorrelationMutationIntent:
    return CorrelationMutationIntent(operation_id, event_id, TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, NOW)


def _event(event_id: str) -> dict[str, object]:
    return {"event_id": event_id, "event_type": "general_log_anomaly"}


def test_enumerate_reads_are_deterministic_and_filterable(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    store, manager = _manager(database)
    first = manager.route_shadow(_intent("OP-2", "EVT-2"), _event("EVT-2"), NOW)
    second = manager.route_shadow(_intent("OP-1", "EVT-1"), _event("EVT-1"), NOW)
    expected = tuple(sorted((first.shadow_id, second.shadow_id)))
    assert tuple(record.shadow_id for record in store.enumerate_shadows()) == expected
    assert store.enumerate_shadows(reason=ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY) == store.enumerate_shadows()
    assert store.enumerate_shadows(review_status=ShadowReviewStatus.UNREVIEWED) == store.enumerate_shadows()
    assert store.enumerate_shadows() == store.enumerate_shadows()
    store.close()


def test_receipt_and_ownership_contradictions_fail_closed(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    store, manager = _manager(database)
    result = manager.route_shadow(_intent("OP-1", "EVT-1"), _event("EVT-1"), NOW)
    store.close()
    insert_second_receipt_for_shadow(database, result.shadow_id)
    with SqliteShadowStore(database) as reopened:
        with pytest.raises(ShadowStoreIntegrityError) as error:
            reopened.readiness_check()
    assert error.value.code is ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD


def test_single_shadow_read_cannot_hide_missing_or_contradictory_receipt(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    store, manager = _manager(database)
    result = manager.route_shadow(_intent("OP-1", "EVT-1"), _event("EVT-1"), NOW)
    store.close()
    remove_receipt_for_shadow(database, result.shadow_id)
    with SqliteShadowStore(database) as reopened:
        with pytest.raises(ShadowStoreIntegrityError) as error:
            reopened.get_shadow(result.shadow_id)
    assert error.value.code is ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD

    database = tmp_path / "receipt-owner.sqlite"
    store, manager = _manager(database)
    result = manager.route_shadow(_intent("OP-1", "EVT-1"), _event("EVT-1"), NOW)
    store.close()
    corrupt_receipt_event_id(database, "OP-1", "EVT-OTHER")
    with SqliteShadowStore(database) as reopened:
        with pytest.raises(ShadowStoreIntegrityError) as error:
            reopened.get_shadow(result.shadow_id)
    assert error.value.code is ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD

    database = tmp_path / "receipt.sqlite"
    store, manager = _manager(database)
    manager.route_shadow(_intent("OP-1", "EVT-1"), _event("EVT-1"), NOW)
    store.close()
    corrupt_receipt_result_timestamp(database, "OP-1")
    with SqliteShadowStore(database) as reopened:
        with pytest.raises(ShadowStoreIntegrityError) as error:
            reopened.get_operation_result("OP-1")
    assert error.value.code is ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD


def test_whole_store_unreadable_fails_fast(tmp_path) -> None:
    database = tmp_path / "not-a-sqlite-file"
    database.write_bytes(b"not a sqlite database")
    with pytest.raises(ShadowStoreIntegrityError) as error:
        SqliteShadowStore(database)
    assert error.value.code is ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE
