"""Regression coverage for canonical cross-SPEC Python type identity."""

from alert_correlation import (
    AnchorStrength,
    AnchorTransition,
    CorrelationDecision,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    NormalizedFingerprint,
)
from alert_correlation.state import (
    CorrelationMutationIntent as RuntimeIntent,
    RetryDisposition,
    TerminalOutcome,
)
import incident_management.contracts as incident_contracts
import incident_management.manager as incident_manager
import incident_management.sqlite_store as incident_store
import runtime_orchestration.recovery as runtime_recovery
import shadow_management.contracts as shadow_contracts
import shadow_management.manager as shadow_manager
import shadow_management.sqlite_store as shadow_store


def test_one_canonical_correlation_intent_crosses_all_public_boundaries() -> None:
    spec007_intent = RuntimeIntent
    incident_expected_intent = incident_contracts.CorrelationMutationIntent
    shadow_expected_intent = shadow_contracts.CorrelationMutationIntent

    assert RuntimeIntent is spec007_intent
    assert RuntimeIntent is incident_expected_intent
    assert RuntimeIntent is shadow_expected_intent
    assert incident_expected_intent is shadow_expected_intent


def test_runtime_recovery_reuses_canonical_spec007_state_types() -> None:
    assert runtime_recovery.RetryDisposition is RetryDisposition
    assert runtime_recovery.TerminalOutcome is TerminalOutcome
    assert incident_contracts.RetryDisposition is RetryDisposition
    assert incident_contracts.TerminalOutcome is TerminalOutcome
    assert shadow_contracts.RetryDisposition is RetryDisposition
    assert shadow_contracts.TerminalOutcome is TerminalOutcome


def test_incident_boundary_reuses_canonical_spec006_types() -> None:
    assert incident_contracts.CorrelationDecision is CorrelationDecision
    assert incident_contracts.DecisionType is DecisionType
    assert incident_contracts.DecisionReasonCode is DecisionReasonCode
    assert incident_contracts.CorrelationFamily is CorrelationFamily
    assert incident_contracts.NormalizedFingerprint is NormalizedFingerprint
    assert incident_contracts.AnchorStrength is AnchorStrength
    assert incident_contracts.AnchorTransition is AnchorTransition

    assert incident_manager.DecisionType is DecisionType
    assert incident_manager.AnchorStrength is AnchorStrength
    assert incident_manager.AnchorTransition is AnchorTransition

    assert incident_store.DecisionType is DecisionType
    assert incident_store.DecisionReasonCode is DecisionReasonCode
    assert incident_store.CorrelationFamily is CorrelationFamily
    assert incident_store.NormalizedFingerprint is NormalizedFingerprint
    assert incident_store.AnchorStrength is AnchorStrength
    assert incident_store.AnchorTransition is AnchorTransition


def test_shadow_boundary_reuses_canonical_spec006_types() -> None:
    assert shadow_contracts.DecisionType is DecisionType
    assert shadow_contracts.DecisionReasonCode is DecisionReasonCode
    assert shadow_contracts.CorrelationFamily is CorrelationFamily
    assert shadow_contracts.AnchorTransition is AnchorTransition

    assert shadow_store.DecisionType is DecisionType
    assert shadow_store.DecisionReasonCode is DecisionReasonCode
    assert shadow_store.CorrelationFamily is CorrelationFamily
    assert shadow_store.NormalizedFingerprint is NormalizedFingerprint
    assert shadow_store.AnchorStrength is AnchorStrength
    assert shadow_store.AnchorTransition is AnchorTransition


def test_shadow_incident_guard_uses_canonical_incident_errors() -> None:
    assert shadow_manager.IncidentDomainError is incident_contracts.IncidentDomainError
    assert shadow_manager.IncidentErrorCode is incident_contracts.IncidentErrorCode
