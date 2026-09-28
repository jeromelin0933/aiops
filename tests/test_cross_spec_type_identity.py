"""Regression coverage for canonical cross-SPEC Python type identity."""

import ast
from pathlib import Path

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
from rca_shared.claim_types import ClaimCategory as CanonicalClaimCategory
from llm_generation.contracts import ClaimCategory as DClaimCategory
from rca_persistence.contracts import ClaimCategory as AClaimCategory
import rca_persistence.contracts as a_contracts
import rca_persistence.sqlite_store as a_store
import llm_generation.contracts as d_contracts
import rca_shared.claim_types as shared_claim_types


def test_claim_category_has_one_canonical_python_identity() -> None:
    assert DClaimCategory is AClaimCategory is CanonicalClaimCategory
    assert [(item.name, item.value) for item in CanonicalClaimCategory] == [
        ("OBSERVED_FACT", "OBSERVED_FACT"),
        ("ANALYTICAL_INFERENCE", "ANALYTICAL_INFERENCE"),
        ("KNOWLEDGE_BACKED_GUIDANCE", "KNOWLEDGE_BACKED_GUIDANCE"),
        ("MODEL_SUGGESTED_GUIDANCE", "MODEL_SUGGESTED_GUIDANCE"),
    ]


def test_claim_category_is_declared_once_and_a_does_not_import_d() -> None:
    modules = (a_contracts, a_store, d_contracts, shared_claim_types)
    definitions = 0
    for module in modules:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        definitions += sum(isinstance(node, ast.ClassDef) and node.name == "ClaimCategory" for node in tree.body)
        if module in (a_contracts, a_store):
            assert not any(
                isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("llm_generation")
                or isinstance(node, ast.Import) and any(alias.name.startswith("llm_generation") for alias in node.names)
                for node in ast.walk(tree)
            )
    assert definitions == 1


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
