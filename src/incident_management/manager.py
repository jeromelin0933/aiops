"""Guarded SPEC-008 correlation-driven Incident mutation authority."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable
from uuid import uuid4

from src.alert_correlation import AnchorStrength, AnchorTransition, DecisionType

from .contracts import (
    CORRELATION_OPEN_STATUSES,
    RCA_INITIAL_STATUS,
    IncidentAuditAction,
    IncidentAuditEffect,
    IncidentAuditEntry,
    IncidentCorrelationContext,
    IncidentDomainError,
    IncidentErrorCode,
    IncidentMutationCompletion,
    IncidentMutationRequest,
    IncidentOperationReceipt,
    IncidentOperationResult,
    IncidentRecord,
    OperationReceiptSemanticIdentity,
    IncidentStatus,
)
from .sqlite_store import SqliteIncidentStore, _IncidentStoreTransaction


IncidentIdFactory = Callable[[], str]


def _default_incident_id() -> str:
    return f"INC-{uuid4()}"


def _error(
    code: IncidentErrorCode,
    message: str,
    request: IncidentMutationRequest,
    *,
    incident_id: str | None = None,
) -> IncidentDomainError:
    return IncidentDomainError(
        code,
        message,
        operation_id=request.intent.operation_id,
        event_id=request.intent.event_id,
        incident_id=incident_id,
    )


class IncidentManager:
    """The sole public correlation-driven Incident write entrypoint."""

    def __init__(
        self,
        store: SqliteIncidentStore,
        *,
        incident_id_factory: IncidentIdFactory | None = None,
    ) -> None:
        if not isinstance(store, SqliteIncidentStore):
            raise TypeError("store must be a SqliteIncidentStore")
        if incident_id_factory is not None and not callable(incident_id_factory):
            raise TypeError("incident_id_factory must be callable")
        self._store = store
        self._incident_id_factory = incident_id_factory or _default_incident_id

    def apply_correlation_mutation(
        self, request: IncidentMutationRequest
    ) -> IncidentOperationResult:
        if not isinstance(request, IncidentMutationRequest):
            raise IncidentDomainError(
                IncidentErrorCode.INVALID_INCIDENT_MUTATION,
                "request must be an IncidentMutationRequest",
            )

        with self._store._transaction() as transaction:
            identity = OperationReceiptSemanticIdentity.from_request(request)
            existing_receipt = transaction._get_operation_receipt(
                request.intent.operation_id
            )
            if existing_receipt is not None:
                if existing_receipt.immutable_mutation_identity != identity:
                    raise _error(
                        IncidentErrorCode.MUTATION_RECEIPT_CONFLICT,
                        "operation_id already has a contradictory mutation identity",
                        request,
                        incident_id=existing_receipt.result.incident_id,
                    )
                return existing_receipt.result

            self._validate_event_fingerprint_coherence(request, identity)

            if request.intent.decision_type is DecisionType.CREATE_NEW:
                return self._create_new(transaction, request, identity)
            if request.intent.decision_type is DecisionType.ATTACH_EXISTING:
                return self._attach_existing(transaction, request, identity)
            # IncidentMutationRequest rejects this first; retain defense in depth.
            raise _error(
                IncidentErrorCode.INVALID_INCIDENT_MUTATION,
                "decision does not belong to SPEC-008",
                request,
            )

    @staticmethod
    def _validate_event_fingerprint_coherence(
        request: IncidentMutationRequest,
        identity: OperationReceiptSemanticIdentity,
    ) -> None:
        fingerprint = request.intent.normalized_fingerprint
        if fingerprint is not None and fingerprint.event_type != identity.event_type:
            raise _error(
                IncidentErrorCode.INVALID_INCIDENT_MUTATION,
                "Event type must match normalized fingerprint event type",
                request,
            )

    def _create_new(
        self,
        transaction: _IncidentStoreTransaction,
        request: IncidentMutationRequest,
        identity: OperationReceiptSemanticIdentity,
    ) -> IncidentOperationResult:
        owner = transaction._get_event_owner(identity.event_id)
        if owner is not None:
            raise _error(
                IncidentErrorCode.EVENT_OWNERSHIP_CONFLICT,
                "Event already belongs to an Incident",
                request,
                incident_id=owner,
            )

        incident_id = self._generate_unique_incident_id(transaction, request)
        intent = request.intent
        if intent.anchor_strength is AnchorStrength.STRONG:
            anchor_event_id = identity.event_id
            anchor_event_type = identity.event_type
            fingerprint = intent.normalized_fingerprint
        else:
            anchor_event_id = None
            anchor_event_type = None
            fingerprint = None

        context = IncidentCorrelationContext(
            correlation_family=intent.correlation_family,
            anchor_strength=intent.anchor_strength,
            anchor_event_id=anchor_event_id,
            anchor_event_type=anchor_event_type,
            normalized_fingerprint=fingerprint,
            anchor_policy_id=intent.policy_id,
            anchor_policy_version=intent.policy_version,
            promoted_from_weak=False,
        )
        audit = self._audit_entry(
            request,
            incident_id,
            IncidentAuditAction.INCIDENT_CREATED,
            (IncidentAuditEffect.EVENT_ATTACHED,),
        )
        record = IncidentRecord(
            incident_id=incident_id,
            event_ids=(identity.event_id,),
            anchor_event_id=anchor_event_id,
            status=IncidentStatus.OPEN,
            severity=identity.event_severity,
            created_at=request.now,
            updated_at=request.now,
            last_correlated_at=identity.event_detected_at,
            closed_at=None,
            assignee=None,
            reviewer=None,
            correlation_context=context,
            audit_trail=(audit,),
            rca_status=RCA_INITIAL_STATUS,
            rca_ref=None,
            external_refs=(),
        )
        receipt = self._receipt(request, identity, incident_id)

        transaction._insert_incident_state(record)
        transaction._insert_event_reference(incident_id, identity.event_id, 0)
        transaction._insert_operation_receipt(receipt)
        transaction._insert_audit_entry(audit)
        return receipt.result

    def _attach_existing(
        self,
        transaction: _IncidentStoreTransaction,
        request: IncidentMutationRequest,
        identity: OperationReceiptSemanticIdentity,
    ) -> IncidentOperationResult:
        target_id = request.intent.target_incident_id
        target = transaction._get_incident(target_id)
        owner = transaction._get_event_owner(identity.event_id)

        if target is None:
            raise _error(
                IncidentErrorCode.INCIDENT_NOT_FOUND,
                "target Incident does not exist",
                request,
                incident_id=target_id,
            )
        if target.status not in CORRELATION_OPEN_STATUSES:
            raise _error(
                IncidentErrorCode.INCIDENT_NOT_CORRELATION_OPEN,
                "target Incident is not correlation-open",
                request,
                incident_id=target_id,
            )
        if owner is not None:
            raise _error(
                IncidentErrorCode.EVENT_OWNERSHIP_CONFLICT,
                "Event already belongs to an Incident",
                request,
                incident_id=owner,
            )
        context = target.correlation_context
        if request.intent.correlation_family is not context.correlation_family:
            raise _error(
                IncidentErrorCode.CORRELATION_CONTEXT_CONFLICT,
                "Intent correlation family conflicts with target Incident",
                request,
                incident_id=target_id,
            )
        if identity.event_detected_at < target.last_correlated_at:
            raise _error(
                IncidentErrorCode.STALE_CORRELATION_EVENT_TIME,
                "incoming Event predates the latest correlated Event",
                request,
                incident_id=target_id,
            )
        if request.now < target.updated_at:
            raise _error(
                IncidentErrorCode.STALE_CORRELATION_EVENT_TIME,
                "authoritative mutation time predates target updated_at",
                request,
                incident_id=target_id,
            )

        if request.intent.anchor_transition is AnchorTransition.WEAK_TO_STRONG:
            return self._promote_weak_to_strong(
                transaction, request, identity, target
            )
        if request.intent.anchor_transition is not AnchorTransition.NONE:
            raise _error(
                IncidentErrorCode.INVALID_ANCHOR_PROMOTION,
                "unsupported anchor transition",
                request,
                incident_id=target_id,
            )
        if request.intent.anchor_strength is not context.anchor_strength:
            raise _error(
                IncidentErrorCode.CORRELATION_CONTEXT_CONFLICT,
                "ordinary attach effective anchor strength conflicts with target Incident",
                request,
                incident_id=target_id,
            )
        if (
            request.intent.normalized_fingerprint is not None
            and request.intent.normalized_fingerprint != context.normalized_fingerprint
        ):
            raise _error(
                IncidentErrorCode.CORRELATION_CONTEXT_CONFLICT,
                "ordinary attach fingerprint conflicts with target Incident anchor",
                request,
                incident_id=target_id,
            )

        return self._finish_attach(
            transaction,
            request,
            identity,
            target,
            target.correlation_context,
            (IncidentAuditEffect.EVENT_ATTACHED,),
        )

    def _promote_weak_to_strong(
        self,
        transaction: _IncidentStoreTransaction,
        request: IncidentMutationRequest,
        identity: OperationReceiptSemanticIdentity,
        target: IncidentRecord,
    ) -> IncidentOperationResult:
        context = target.correlation_context
        if (
            context.anchor_strength is not AnchorStrength.WEAK
            or target.anchor_event_id is not None
            or context.anchor_event_id is not None
            or context.anchor_event_type is not None
            or context.normalized_fingerprint is not None
            or context.promoted_from_weak
            or request.intent.anchor_strength is not AnchorStrength.STRONG
            or request.intent.normalized_fingerprint is None
        ):
            raise _error(
                IncidentErrorCode.INVALID_ANCHOR_PROMOTION,
                "target or Intent does not satisfy WEAK_TO_STRONG promotion preconditions",
                request,
                incident_id=target.incident_id,
            )

        promoted_context = IncidentCorrelationContext(
            correlation_family=context.correlation_family,
            anchor_strength=AnchorStrength.STRONG,
            anchor_event_id=identity.event_id,
            anchor_event_type=identity.event_type,
            normalized_fingerprint=request.intent.normalized_fingerprint,
            anchor_policy_id=request.intent.policy_id,
            anchor_policy_version=request.intent.policy_version,
            promoted_from_weak=True,
        )
        return self._finish_attach(
            transaction,
            request,
            identity,
            target,
            promoted_context,
            (
                IncidentAuditEffect.EVENT_ATTACHED,
                IncidentAuditEffect.ANCHOR_PROMOTED,
            ),
        )

    def _finish_attach(
        self,
        transaction: _IncidentStoreTransaction,
        request: IncidentMutationRequest,
        identity: OperationReceiptSemanticIdentity,
        target: IncidentRecord,
        resulting_context: IncidentCorrelationContext,
        base_effects: tuple[IncidentAuditEffect, ...],
    ) -> IncidentOperationResult:
        severity = max(target.severity, identity.event_severity)
        effects = list(base_effects)
        if severity > target.severity:
            effects.append(IncidentAuditEffect.SEVERITY_ESCALATED)
        audit = self._audit_entry(
            request,
            target.incident_id,
            IncidentAuditAction.CORRELATION_ATTACHED,
            tuple(effects),
        )
        updated = replace(
            target,
            event_ids=target.event_ids + (identity.event_id,),
            anchor_event_id=resulting_context.anchor_event_id,
            severity=severity,
            updated_at=request.now,
            last_correlated_at=identity.event_detected_at,
            correlation_context=resulting_context,
            audit_trail=target.audit_trail + (audit,),
        )
        receipt = self._receipt(request, identity, target.incident_id)

        transaction._replace_incident_state(updated)
        transaction._insert_event_reference(
            target.incident_id, identity.event_id, len(target.event_ids)
        )
        transaction._insert_operation_receipt(receipt)
        transaction._insert_audit_entry(audit)
        return receipt.result

    def _generate_unique_incident_id(
        self,
        transaction: _IncidentStoreTransaction,
        request: IncidentMutationRequest,
    ) -> str:
        for _ in range(32):
            incident_id = self._incident_id_factory()
            if (
                not isinstance(incident_id, str)
                or not incident_id
                or incident_id != incident_id.strip()
            ):
                raise _error(
                    IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                    "incident_id_factory returned an invalid Incident reference",
                    request,
                )
            if transaction._get_incident(incident_id) is None:
                return incident_id
        raise _error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            "incident_id_factory could not produce a unique Incident reference",
            request,
        )

    @staticmethod
    def _audit_entry(
        request: IncidentMutationRequest,
        incident_id: str,
        action: IncidentAuditAction,
        effects: tuple[IncidentAuditEffect, ...],
    ) -> IncidentAuditEntry:
        intent = request.intent
        return IncidentAuditEntry(
            operation_id=intent.operation_id,
            event_id=intent.event_id,
            incident_id=incident_id,
            policy_id=intent.policy_id,
            policy_version=intent.policy_version,
            reason_code=intent.reason_code,
            action=action,
            effects=effects,
            occurred_at=request.now,
        )

    @staticmethod
    def _receipt(
        request: IncidentMutationRequest,
        identity: OperationReceiptSemanticIdentity,
        incident_id: str,
    ) -> IncidentOperationReceipt:
        result = IncidentOperationResult(
            operation_id=request.intent.operation_id,
            event_id=request.intent.event_id,
            mutation_kind=request.intent.decision_type,
            incident_id=incident_id,
            completion_result=IncidentMutationCompletion.SUCCEEDED,
            completed_at=request.now,
        )
        return IncidentOperationReceipt(
            result=result,
            immutable_mutation_identity=identity,
        )


__all__ = ["IncidentIdFactory", "IncidentManager"]
