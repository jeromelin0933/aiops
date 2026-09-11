"""Guarded SPEC-008 correlation-driven Incident mutation authority."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable
from uuid import uuid4

from src.alert_correlation import AnchorStrength, AnchorTransition, DecisionType

from .contracts import (
    AssignmentPolicyConfig,
    AssignmentSelectionMode,
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
    WorkflowAction,
    WorkflowAuditEffect,
    WorkflowAuditEntry,
    WorkflowCompletion,
    WorkflowDomainError,
    WorkflowErrorCode,
    WorkflowMutationRequest,
    WorkflowOperationReceipt,
    WorkflowOperationResult,
    WorkflowReceiptSemanticIdentity,
    ResolutionSubmission,
    ReviewAttempt,
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

    def auto_assign_incident(self, request: WorkflowMutationRequest, policy: AssignmentPolicyConfig) -> WorkflowOperationResult:
        return self._assign(request, policy, automatic=True)

    def assign_incident(self, request: WorkflowMutationRequest, policy: AssignmentPolicyConfig) -> WorkflowOperationResult:
        return self._assign(request, policy, automatic=False)

    def reassign_incident(self, request: WorkflowMutationRequest, policy: AssignmentPolicyConfig) -> WorkflowOperationResult:
        self._require_workflow_request(request, WorkflowAction.REASSIGN)
        self._require_policy(policy)
        with self._store._transaction() as transaction:
            replay = self._workflow_replay(transaction, request)
            if replay is not None:
                return replay
            incident = self._fresh_workflow_incident(transaction, request)
            if incident.status is IncidentStatus.CLOSED:
                self._workflow_fail(WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN, "closed Incident cannot be reassigned", request)
            if incident.status not in {IncidentStatus.ASSIGNED, IncidentStatus.IN_PROGRESS}:
                self._workflow_fail(WorkflowErrorCode.ASSIGNMENT_NOT_ALLOWED, "Incident cannot be reassigned in its current status", request)
            if request.target_assignee not in policy.engineers:
                self._workflow_fail(WorkflowErrorCode.INVALID_ASSIGNMENT_TARGET, "target assignee is not configured", request)
            updated = replace(incident, assignee=request.target_assignee, updated_at=request.now)
            return self._persist_workflow(transaction, request, updated, None, (WorkflowAuditEffect.ASSIGNEE_SET,))

    def start_work(self, request: WorkflowMutationRequest) -> WorkflowOperationResult:
        self._require_workflow_request(request, WorkflowAction.START_WORK)
        with self._store._transaction() as transaction:
            replay = self._workflow_replay(transaction, request)
            if replay is not None:
                return replay
            incident = self._fresh_workflow_incident(transaction, request)
            if incident.status is IncidentStatus.CLOSED:
                self._workflow_fail(WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN, "closed Incident cannot start work", request)
            if incident.status is not IncidentStatus.ASSIGNED:
                self._workflow_fail(WorkflowErrorCode.INVALID_LIFECYCLE_TRANSITION, "START_WORK requires ASSIGNED Incident", request)
            if request.actor != incident.assignee:
                self._workflow_fail(WorkflowErrorCode.WORKFLOW_ACTOR_MISMATCH, "only current assignee may start work", request)
            updated = replace(incident, status=IncidentStatus.IN_PROGRESS, updated_at=request.now)
            return self._persist_workflow(transaction, request, updated, None, (WorkflowAuditEffect.STATUS_CHANGED,))

    def submit_resolution(self, request: WorkflowMutationRequest) -> WorkflowOperationResult:
        self._require_workflow_request(request, WorkflowAction.SUBMIT_RESOLUTION)
        with self._store._transaction() as transaction:
            replay = self._workflow_replay(transaction, request)
            if replay is not None:
                return replay
            incident = self._fresh_workflow_incident(transaction, request)
            if incident.status is IncidentStatus.CLOSED:
                self._workflow_fail(WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN, "closed Incident cannot accept Resolution", request)
            if incident.status not in {IncidentStatus.IN_PROGRESS, IncidentStatus.AWAITING_REVIEW}:
                self._workflow_fail(WorkflowErrorCode.INVALID_LIFECYCLE_TRANSITION, "Resolution requires IN_PROGRESS or AWAITING_REVIEW Incident", request)
            if request.actor != incident.assignee:
                self._workflow_fail(WorkflowErrorCode.WORKFLOW_ACTOR_MISMATCH, "only current assignee may submit Resolution", request)
            history = transaction._list_resolution_submissions(incident.incident_id)
            submission = ResolutionSubmission(f"RES-{uuid4()}", incident.incident_id, len(history) + 1,
                request.resolution.actual_action, request.resolution.resolution_note, request.resolution.sop_followed,
                request.resolution.additional_note, request.resolution.deviation_reason, request.actor, request.now)
            updated = replace(incident, status=IncidentStatus.AWAITING_REVIEW, updated_at=request.now)
            effects = (WorkflowAuditEffect.RESOLUTION_SUBMITTED,)
            if incident.status is not updated.status:
                effects += (WorkflowAuditEffect.STATUS_CHANGED,)
            transaction._insert_resolution_submission(submission)
            return self._persist_workflow(transaction, request, updated, None, effects, submission.resolution_submission_id)

    def review_incident(self, request: WorkflowMutationRequest) -> WorkflowOperationResult:
        self._require_workflow_request(request, WorkflowAction.REVIEW_ATTEMPT)
        with self._store._transaction() as transaction:
            replay = self._workflow_replay(transaction, request)
            if replay is not None:
                return replay
            incident = self._fresh_workflow_incident(transaction, request)
            if incident.status is IncidentStatus.CLOSED:
                self._workflow_fail(WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN, "closed Incident cannot be reviewed", request)
            if incident.status is not IncidentStatus.AWAITING_REVIEW:
                self._workflow_fail(WorkflowErrorCode.INVALID_LIFECYCLE_TRANSITION, "review requires AWAITING_REVIEW Incident", request)
            if request.actor != incident.reviewer:
                self._workflow_fail(WorkflowErrorCode.WORKFLOW_ACTOR_MISMATCH, "only current reviewer may review", request)
            history = transaction._list_resolution_submissions(incident.incident_id)
            if not history or request.review.target_resolution_revision != history[-1].revision:
                self._workflow_fail(WorkflowErrorCode.STALE_RESOLUTION_REVISION, "review target is not latest Resolution revision", request)
            attempt = ReviewAttempt(f"REV-{uuid4()}", incident.incident_id, request.review.target_resolution_revision,
                request.actor, request.review.review_approved, request.review.review_note,
                request.review.recovery_verified, request.review.recovery_note, request.now)
            closes = request.review.review_approved and request.review.recovery_verified
            updated = replace(incident, status=IncidentStatus.CLOSED if closes else incident.status,
                closed_at=request.now if closes else incident.closed_at, updated_at=request.now)
            effects = (WorkflowAuditEffect.REVIEW_RECORDED,)
            if request.review.recovery_verified:
                effects += (WorkflowAuditEffect.RECOVERY_VERIFIED,)
            if closes:
                effects += (WorkflowAuditEffect.STATUS_CHANGED, WorkflowAuditEffect.INCIDENT_CLOSED)
            transaction._insert_review_attempt(attempt)
            return self._persist_workflow(transaction, request, updated, None, effects, attempt.review_attempt_id)

    def _assign(self, request: WorkflowMutationRequest, policy: AssignmentPolicyConfig, *, automatic: bool) -> WorkflowOperationResult:
        expected_action = WorkflowAction.AUTO_ASSIGN if automatic else WorkflowAction.MANUAL_ASSIGN
        self._require_workflow_request(request, expected_action)
        self._require_policy(policy)
        with self._store._transaction() as transaction:
            replay = self._workflow_replay(transaction, request)
            if replay is not None:
                return replay
            incident = self._fresh_workflow_incident(transaction, request)
            if incident.status is IncidentStatus.CLOSED:
                self._workflow_fail(WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN, "closed Incident cannot be assigned", request)
            if incident.status is not IncidentStatus.OPEN or incident.assignee is not None:
                self._workflow_fail(WorkflowErrorCode.ASSIGNMENT_NOT_ALLOWED, "initial assignment requires unassigned OPEN Incident", request)
            if automatic:
                state = transaction._get_assignment_state(policy.policy_id, policy.policy_version)
                if state is None:
                    engineers, reviewer, cursor = policy.engineers, policy.default_reviewer, 0
                else:
                    engineers, reviewer, cursor = state
                    if engineers != policy.engineers or reviewer != policy.default_reviewer:
                        self._workflow_fail(WorkflowErrorCode.INCIDENT_WORKFLOW_INTEGRITY_FAILURE, "durable assignment policy state contradicts configured policy", request)
                assignee = engineers[cursor]
                transaction._upsert_assignment_state(policy.policy_id, policy.policy_version, engineers, reviewer, (cursor + 1) % len(engineers))
                mode = AssignmentSelectionMode.AUTO
            else:
                if request.target_assignee not in policy.engineers:
                    self._workflow_fail(WorkflowErrorCode.INVALID_ASSIGNMENT_TARGET, "target assignee is not configured", request)
                assignee, reviewer, mode = request.target_assignee, policy.default_reviewer, AssignmentSelectionMode.MANUAL
            updated = replace(incident, status=IncidentStatus.ASSIGNED, assignee=assignee, reviewer=reviewer, updated_at=request.now)
            provenance = (policy.policy_id, policy.policy_version, assignee, reviewer, mode)
            return self._persist_workflow(transaction, request, updated, provenance,
                (WorkflowAuditEffect.ASSIGNEE_SET, WorkflowAuditEffect.REVIEWER_BOUND, WorkflowAuditEffect.STATUS_CHANGED))

    @staticmethod
    def _require_policy(policy: AssignmentPolicyConfig) -> None:
        if not isinstance(policy, AssignmentPolicyConfig):
            raise WorkflowDomainError(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "policy must be an AssignmentPolicyConfig")

    @staticmethod
    def _require_workflow_request(request: WorkflowMutationRequest, action: WorkflowAction) -> None:
        if not isinstance(request, WorkflowMutationRequest) or request.action is not action:
            raise WorkflowDomainError(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "request action does not match workflow capability")

    @staticmethod
    def _workflow_fail(code: WorkflowErrorCode, message: str, request: WorkflowMutationRequest) -> None:
        raise WorkflowDomainError(code, message, workflow_operation_id=request.workflow_operation_id, incident_id=request.incident_id)

    def _workflow_replay(self, transaction: _IncidentStoreTransaction, request: WorkflowMutationRequest) -> WorkflowOperationResult | None:
        identity = WorkflowReceiptSemanticIdentity.from_request(request)
        receipt = transaction._get_workflow_receipt(request.workflow_operation_id)
        if receipt is None:
            return None
        if receipt.immutable_workflow_identity != identity:
            self._workflow_fail(WorkflowErrorCode.WORKFLOW_RECEIPT_CONFLICT, "workflow operation has contradictory identity", request)
        return receipt.result

    def _fresh_workflow_incident(self, transaction: _IncidentStoreTransaction, request: WorkflowMutationRequest) -> IncidentRecord:
        incident = transaction._get_incident(request.incident_id)
        if incident is None:
            self._workflow_fail(WorkflowErrorCode.INCIDENT_NOT_FOUND, "Incident does not exist", request)
        if request.now < incident.updated_at:
            self._workflow_fail(WorkflowErrorCode.WORKFLOW_TIME_REGRESSION, "workflow time predates Incident updated_at", request)
        return incident

    def _persist_workflow(self, transaction: _IncidentStoreTransaction, request: WorkflowMutationRequest, updated: IncidentRecord,
                          provenance: tuple[str, str, str, str, AssignmentSelectionMode] | None,
                          effects: tuple[WorkflowAuditEffect, ...], result_reference: str | None = None) -> WorkflowOperationResult:
        policy_id = policy_version = selected = reviewer = mode = None
        if provenance is not None:
            policy_id, policy_version, selected, reviewer, mode = provenance
        result = WorkflowOperationResult(request.workflow_operation_id, updated.incident_id, request.action,
            WorkflowCompletion.SUCCEEDED, updated.status, result_reference, request.now, policy_id, policy_version, selected, reviewer, mode)
        receipt = WorkflowOperationReceipt(result, WorkflowReceiptSemanticIdentity.from_request(request))
        audit = WorkflowAuditEntry(f"WFA-{uuid4()}", request.workflow_operation_id, updated.incident_id, request.actor,
            request.action, request.now, transaction._get_incident(updated.incident_id).status, updated.status, effects,
            result_reference, policy_id if request.action is WorkflowAction.AUTO_ASSIGN else None,
            policy_version if request.action is WorkflowAction.AUTO_ASSIGN else None)
        transaction._replace_incident_state(updated)
        transaction._insert_workflow_receipt(receipt)
        transaction._insert_workflow_audit(audit)
        return result

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
