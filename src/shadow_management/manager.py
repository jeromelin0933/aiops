"""The sole authoritative Phase-3 ROUTE_SHADOW mutation boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import uuid4

from alert_correlation.state.contracts import CorrelationMutationIntent
from src.incident_management import IncidentDomainError
from src.incident_management.contracts import IncidentErrorCode

from .contracts import (
    ExactPolicyLookup,
    IncidentOwnershipLookup,
    ShadowDomainError,
    ShadowDomainErrorCode,
    ShadowMutationRequest,
    ShadowMutationResult,
    ShadowOperationReceipt,
    ShadowReason,
    ShadowRecord,
    ShadowReviewStatus,
    receipt_semantic_identity,
    validate_legal_shadow_mutation,
    validate_shadow_creation_reason,
)
from .sqlite_store import SqliteShadowStore


ShadowIdFactory = Callable[[], str]


class ShadowManager:
    """Create one legal Shadow record under local transactional authority only."""

    def __init__(
        self,
        store: SqliteShadowStore,
        policy_lookup: ExactPolicyLookup,
        incident_ownership_lookup: IncidentOwnershipLookup,
        *,
        shadow_id_factory: ShadowIdFactory | None = None,
    ) -> None:
        if not isinstance(store, SqliteShadowStore):
            raise TypeError("store must be a SqliteShadowStore")
        if not callable(getattr(policy_lookup, "resolve_exact", None)):
            raise TypeError("policy_lookup must provide resolve_exact")
        if not callable(getattr(incident_ownership_lookup, "event_has_incident_owner", None)):
            raise TypeError("incident_ownership_lookup must provide event_has_incident_owner")
        if shadow_id_factory is not None and not callable(shadow_id_factory):
            raise TypeError("shadow_id_factory must be callable")
        self._store = store
        self._policy_lookup = policy_lookup
        self._incident_ownership_lookup = incident_ownership_lookup
        self._shadow_id_factory = shadow_id_factory or _new_shadow_id

    def route_shadow(
        self,
        intent: CorrelationMutationIntent,
        event: Mapping[str, object],
        now: datetime,
    ) -> ShadowMutationResult:
        """Atomically persist the local Shadow, Event ownership, and receipt.

        This method neither evaluates correlation policy nor mutates Incident
        state.  Incident ownership evidence is read before the local commit and
        is explicitly not part of a distributed transaction.
        """
        request = ShadowMutationRequest(intent, event, now)
        semantic_identity = receipt_semantic_identity(request)

        with self._store._write_transaction():
            existing_receipt = self._store._get_operation_receipt_locked(
                intent.operation_id
            )
            if existing_receipt is not None:
                if existing_receipt.semantic_identity == semantic_identity:
                    return existing_receipt.result
                raise ShadowDomainError(
                    ShadowDomainErrorCode.MUTATION_RECEIPT_CONFLICT,
                    "operation_id contradicts its durable Shadow receipt identity",
                )

            validate_legal_shadow_mutation(request, self._policy_lookup)
            existing_shadow = self._store._get_shadow_by_event_id_locked(intent.event_id)
            if existing_shadow is not None:
                existing_receipt = self._store._get_operation_receipt_for_shadow_locked(
                    existing_shadow.shadow_id
                )
                if existing_receipt is None:
                    raise ShadowDomainError(
                        ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD,
                        "Shadow Event ownership has no matching operation receipt",
                    )
                raise ShadowDomainError(
                    ShadowDomainErrorCode.SHADOW_EVENT_OWNERSHIP_CONFLICT,
                    "event_id already has Shadow-local ownership from another operation",
                )

            # Incident evidence is only needed for the first local ownership.
            # An existing Shadow owner is already a local terminal conflict.
            self._require_no_incident_owner(intent.event_id)

            reason = ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY
            validate_shadow_creation_reason(reason)
            record = ShadowRecord(
                self._shadow_id_factory(),
                intent.event_id,
                request.now,
                reason,
                ShadowReviewStatus.UNREVIEWED,
                intent.policy_id,
                intent.policy_version,
            )
            result = ShadowMutationResult(
                intent.operation_id, record.shadow_id, record.entered_shadow_at
            )
            receipt = ShadowOperationReceipt(semantic_identity, result)
            self._store._insert_shadow_record(record)
            self._store._insert_operation_receipt(receipt)
            return result

    def _require_no_incident_owner(self, event_id: str) -> None:
        try:
            has_owner = self._incident_ownership_lookup.event_has_incident_owner(event_id)
        except IncidentDomainError as exc:
            # SPEC-008 is the authoritative semantic reader.  Its failures
            # cannot be interpreted as clean absence: map them into the
            # frozen Shadow retry vocabulary while preserving fail-closed
            # behaviour before any local transaction writes occur.
            code = (
                ShadowDomainErrorCode.TRANSIENT_SHADOW_STORE_FAILURE
                if exc.code is IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE
                else ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE
            )
            raise ShadowDomainError(
                code,
                "Incident ownership evidence cannot be safely interpreted",
            ) from exc
        except Exception as exc:
            raise ShadowDomainError(
                ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
                "Incident ownership evidence is unavailable",
            ) from exc
        if not isinstance(has_owner, bool):
            raise ShadowDomainError(
                ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
                "Incident ownership evidence must be boolean",
            )
        if has_owner:
            raise ShadowDomainError(
                ShadowDomainErrorCode.INCIDENT_EVENT_OWNERSHIP_CONFLICT,
                "event_id already has Incident ownership",
            )


def _new_shadow_id() -> str:
    return f"SHADOW-{uuid4().hex}"
