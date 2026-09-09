from datetime import datetime, timezone

import pytest

from alert_correlation.contracts import AnchorStrength, AnchorTransition, CorrelationFamily, DecisionReasonCode, DecisionType
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY, CorrelationPolicy, PolicyRegistry
from alert_correlation.contracts import EvidenceClass
from alert_correlation.state.contracts import CorrelationMutationIntent, TerminalOutcome
from shadow_management.contracts import ShadowDomainError, ShadowDomainErrorCode
from shadow_management.manager import ShadowManager
from shadow_management.sqlite_store import SqliteShadowStore


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


class _IncidentOwners:
    def __init__(self, owned_event_ids: set[str] | None = None) -> None:
        self._owned_event_ids = owned_event_ids or set()

    def event_has_incident_owner(self, event_id: str) -> bool:
        return event_id in self._owned_event_ids


def _intent(
    operation_id: str = "OP-1", event_id: str = "EVT-1"
) -> CorrelationMutationIntent:
    return CorrelationMutationIntent(operation_id, event_id, TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, NOW)


def _manager(tmp_path, owners: _IncidentOwners | None = None) -> tuple[SqliteShadowStore, ShadowManager]:
    store = SqliteShadowStore(tmp_path / "shadow.sqlite")
    manager = ShadowManager(store, DEFAULT_POLICY_REGISTRY, owners or _IncidentOwners(), shadow_id_factory=lambda: "SHADOW-1")
    return store, manager


def _event(event_id: str = "EVT-1", event_type: str = "general_log_anomaly") -> dict[str, object]:
    return {"event_id": event_id, "event_type": event_type, "unprojected": "authoritative Event remains external"}


def test_manager_is_the_legal_authoritative_shadow_creation_boundary(tmp_path) -> None:
    store, manager = _manager(tmp_path)
    result = manager.route_shadow(_intent(), _event(), NOW)
    record = store.get_shadow(result.shadow_id)
    assert result.operation_id == "OP-1"
    assert record is not None
    assert record.event_id == "EVT-1"
    assert record.entered_shadow_at == NOW
    assert record.reason.value == "INSUFFICIENT_OPERATIONAL_IDENTITY"
    assert store.get_operation_result("OP-1") == result
    assert not hasattr(store, "put_shadow")
    assert not hasattr(store, "put_receipt")
    store.close()


@pytest.mark.parametrize(
    ("decision", "outcome", "target", "reason", "anchor"),
    [
        (DecisionType.CREATE_NEW, TerminalOutcome.CREATED_INCIDENT, None, DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, AnchorStrength.STRONG),
        (DecisionType.ATTACH_EXISTING, TerminalOutcome.ATTACHED_TO_INCIDENT, "INC-1", DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE, None),
    ],
)
def test_wrong_domain_intents_are_rejected_without_local_effect(
    tmp_path, decision: DecisionType, outcome: TerminalOutcome, target: str | None,
    reason: DecisionReasonCode, anchor: AnchorStrength | None,
) -> None:
    store, manager = _manager(tmp_path)
    intent = CorrelationMutationIntent("OP-WRONG", "EVT-1", outcome, decision, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, reason, target, None, anchor, AnchorTransition.NONE, NOW)
    with pytest.raises(ShadowDomainError) as error:
        manager.route_shadow(intent, _event(), NOW)
    assert error.value.code is ShadowDomainErrorCode.INVALID_SHADOW_MUTATION
    assert store.get_shadow_by_event_id("EVT-1") is None
    store.close()


@pytest.mark.parametrize(
    "event", [_event("OTHER"), _event(event_type="unregistered"), _event(event_type="general_metrics_anomaly")]
)
def test_event_id_mismatch_unregistered_and_policy_mismatch_cannot_create_shadow(tmp_path, event: dict[str, object]) -> None:
    store, manager = _manager(tmp_path)
    with pytest.raises(ShadowDomainError) as error:
        manager.route_shadow(_intent(), event, NOW)
    assert error.value.code is ShadowDomainErrorCode.INVALID_SHADOW_MUTATION
    assert store.get_shadow_by_event_id("EVT-1") is None
    store.close()


def test_noncurrent_unknown_policy_cannot_create_shadow(tmp_path) -> None:
    store = SqliteShadowStore(tmp_path / "shadow.sqlite")
    historical_policy = PolicyRegistry((CorrelationPolicy("POLICY-GENERAL-LOG-ANOMALY", "1.0", "general_log_anomaly", EvidenceClass.UNKNOWN, CorrelationFamily.UNKNOWN, is_current=False),))
    manager = ShadowManager(store, historical_policy, _IncidentOwners(), shadow_id_factory=lambda: "SHADOW-1")
    with pytest.raises(ShadowDomainError) as error:
        manager.route_shadow(_intent(), _event(), NOW)
    assert error.value.code is ShadowDomainErrorCode.INVALID_SHADOW_MUTATION
    assert store.get_shadow_by_event_id("EVT-1") is None
    store.close()


def test_same_event_different_operation_is_shadow_ownership_conflict(tmp_path) -> None:
    store, manager = _manager(tmp_path)
    manager.route_shadow(_intent(), _event(), NOW)
    with pytest.raises(ShadowDomainError) as error:
        manager.route_shadow(_intent("OP-2"), _event(), NOW)
    assert error.value.code is ShadowDomainErrorCode.SHADOW_EVENT_OWNERSHIP_CONFLICT
    assert error.value.retry_disposition.value == "REPAIR_REQUIRED"
    store.close()


def test_incident_owner_conflict_prevents_shadow_commit(tmp_path) -> None:
    store, manager = _manager(tmp_path, _IncidentOwners({"EVT-1"}))
    with pytest.raises(ShadowDomainError) as error:
        manager.route_shadow(_intent(), _event(), NOW)
    assert error.value.code is ShadowDomainErrorCode.INCIDENT_EVENT_OWNERSHIP_CONFLICT
    assert store.get_shadow_by_event_id("EVT-1") is None
    store.close()


def test_atomic_transaction_rolls_back_shadow_if_receipt_write_fails(tmp_path, monkeypatch) -> None:
    store, manager = _manager(tmp_path)
    def fail_receipt(*_args: object) -> None:
        raise RuntimeError("receipt write failed")
    monkeypatch.setattr(store, "_insert_operation_receipt", fail_receipt)
    with pytest.raises(RuntimeError, match="receipt write failed"):
        manager.route_shadow(_intent(), _event(), NOW)
    assert store.get_shadow_by_event_id("EVT-1") is None
    assert store.get_operation_result("OP-1") is None
    store.close()
