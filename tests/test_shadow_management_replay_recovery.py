import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from alert_correlation.contracts import AnchorStrength, AnchorTransition, CorrelationFamily, DecisionReasonCode, DecisionType, NormalizedFingerprint
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY
from alert_correlation.state.contracts import CorrelationMutationIntent, TerminalOutcome
from shadow_management.contracts import ShadowDomainError, ShadowDomainErrorCode
from shadow_management.manager import ShadowManager
from shadow_management.sqlite_store import ShadowStoreIntegrityError, SqliteShadowStore
from tests._shadow_store_testkit import remove_receipt_shadow_referent


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


class _NoIncidentOwner:
    def event_has_incident_owner(self, _event_id: str) -> bool:
        return False


def _intent(event_id: str = "EVT-1", *, created_at: datetime = NOW) -> CorrelationMutationIntent:
    return CorrelationMutationIntent("OP-1", event_id, TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, created_at)


def _event(event_id: str = "EVT-1", event_type: str = "general_log_anomaly") -> dict[str, object]:
    return {"event_id": event_id, "event_type": event_type}


def _manager(database, shadow_id: str = "SHADOW-1") -> tuple[SqliteShadowStore, ShadowManager]:
    store = SqliteShadowStore(database)
    return store, ShadowManager(store, DEFAULT_POLICY_REGISTRY, _NoIncidentOwner(), shadow_id_factory=lambda: shadow_id)


def _receipt_count(database) -> int:
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0]


def _shadow_count(database) -> int:
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT COUNT(*) FROM shadow_records").fetchone()[0]


def test_equivalent_same_operation_replay_returns_original_result_and_ignores_retry_now(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    store, manager = _manager(database)
    original = manager.route_shadow(_intent(), _event(), NOW)
    replay = manager.route_shadow(_intent(), _event(), NOW + timedelta(hours=1))
    assert replay == original
    assert replay.entered_shadow_at == NOW
    assert _shadow_count(database) == 1
    assert _receipt_count(database) == 1
    store.close()


@pytest.mark.parametrize(
    "replacement,event",
    [
        (lambda original: replace(original, event_id="EVT-2"), _event("EVT-2")),
        (lambda original: original, _event(event_type="general_metrics_anomaly")),
        (lambda original: CorrelationMutationIntent("OP-1", "EVT-1", TerminalOutcome.CREATED_INCIDENT, DecisionType.CREATE_NEW, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, None, None, AnchorStrength.STRONG, AnchorTransition.NONE, NOW), _event()),
        (lambda original: replace(original, policy_id="POLICY-GENERAL-METRICS-ANOMALY"), _event()),
        (lambda original: replace(original, policy_version="2.0"), _event()),
        (lambda original: replace(original, correlation_family=CorrelationFamily.ATTACK_SOURCE), _event()),
        (lambda original: replace(original, reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE), _event()),
        (lambda original: CorrelationMutationIntent("OP-1", "EVT-1", TerminalOutcome.ATTACHED_TO_INCIDENT, DecisionType.ATTACH_EXISTING, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE, "INC-1", None, None, AnchorTransition.NONE, NOW), _event()),
        (lambda original: replace(original, normalized_fingerprint=NormalizedFingerprint("general_log_anomaly", (("identity", "x"),))), _event()),
        (lambda original: replace(original, anchor_strength=AnchorStrength.STRONG), _event()),
        (lambda original: replace(original, anchor_transition=AnchorTransition.WEAK_TO_STRONG), _event()),
        (lambda original: replace(original, created_at=NOW + timedelta(seconds=1)), _event()),
    ],
)
def test_each_replay_semantic_field_category_conflicts(tmp_path, replacement, event) -> None:
    database = tmp_path / "shadow.sqlite"
    store, manager = _manager(database)
    original = _intent()
    manager.route_shadow(original, _event(), NOW)
    with pytest.raises(ShadowDomainError) as error:
        manager.route_shadow(replacement(original), event, NOW + timedelta(minutes=1))
    assert error.value.code is ShadowDomainErrorCode.MUTATION_RECEIPT_CONFLICT
    assert error.value.retry_disposition.value == "REPAIR_REQUIRED"
    assert _shadow_count(database) == 1
    assert _receipt_count(database) == 1
    store.close()


def test_restart_and_crash_c_style_recovery_return_durable_original_result(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    store, manager = _manager(database)
    original = manager.route_shadow(_intent(), _event(), NOW)
    store.close()  # Simulates local commit before SPEC-007 Processed finalization.
    reopened, recovery_manager = _manager(database, shadow_id="SHADOW-NEW-MUST-NOT-APPEAR")
    assert recovery_manager.route_shadow(_intent(), _event(), NOW + timedelta(days=1)) == original
    assert reopened.get_operation_result("OP-1") == original
    assert reopened.get_shadow("SHADOW-NEW-MUST-NOT-APPEAR") is None
    assert _shadow_count(database) == 1
    assert _receipt_count(database) == 1
    reopened.close()


def test_receipt_with_missing_authoritative_shadow_is_integrity_not_ordinary_not_found(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    store, manager = _manager(database)
    manager.route_shadow(_intent(), _event(), NOW)
    store.close()
    remove_receipt_shadow_referent(database, "SHADOW-1")
    with SqliteShadowStore(database) as reopened:
        assert reopened.get_shadow("SHADOW-1") is None
        with pytest.raises(ShadowStoreIntegrityError) as error:
            reopened.get_operation_result("OP-1")
    assert error.value.code is ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD
