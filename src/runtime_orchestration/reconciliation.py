"""Phase 3 INITIAL correlation and terminal cross-store reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, Sequence

from alert_correlation import (
    AlertCorrelationPolicyEngine,
    CorrelationDecision,
    CorrelationEvaluationContext,
    CorrelationEvaluationFailure,
    CorrelationEvaluationSuccess,
    DecisionType,
    EvaluationPhase,
    IncidentCorrelationView,
)
from alert_correlation.state import (
    CorrelationMutationIntent,
    ProcessedCorrelationRecord,
    ProcessingClaim,
    ResolvedCorrelationState,
    ResolvedState,
    TerminalOutcome,
)
from incident_management import (
    IncidentDomainError,
    IncidentMutationRequest,
    IncidentOperationResult,
    IncidentRecord,
)
from shadow_management import ShadowDomainError, ShadowMutationResult, ShadowRecord

from .clock import RuntimeClock
from .identity import runtime_operation_id
from .intake import DurableEventIntake
from .orchestrator import RuntimeLifecycleState
from .telemetry import NullRuntimeTelemetry, RuntimeTelemetry, RuntimeTelemetryEvent


class CorrelationStateMutationPort(Protocol):
    def acquire_claim(self, event_id: str) -> ProcessingClaim | None: ...
    def release_claim(self, claim: ProcessingClaim) -> None: ...
    def resolve(self, event_id: str) -> ResolvedCorrelationState: ...
    def begin_intent(
        self, intent: CorrelationMutationIntent, claim: ProcessingClaim
    ) -> CorrelationMutationIntent: ...
    def finalize_processed(
        self, record: ProcessedCorrelationRecord, claim: ProcessingClaim | None = None
    ) -> ProcessedCorrelationRecord: ...


class IncidentTerminalReadPort(Protocol):
    def list_correlation_views(self) -> tuple[IncidentCorrelationView, ...]: ...
    def event_has_incident_owner(self, event_id: str) -> bool: ...
    def get_operation_result(self, operation_id: str) -> IncidentOperationResult | None: ...
    def get_incident(self, incident_id: str) -> IncidentRecord | None: ...


class ShadowTerminalReadPort(Protocol):
    def get_shadow_by_event_id(self, event_id: str) -> ShadowRecord | None: ...
    def get_operation_result(self, operation_id: str) -> ShadowMutationResult | None: ...


class IncidentMutationPort(Protocol):
    def apply_correlation_mutation(
        self, request: IncidentMutationRequest
    ) -> IncidentOperationResult: ...


class ShadowMutationPort(Protocol):
    def route_shadow(
        self,
        intent: CorrelationMutationIntent,
        event: Mapping[str, object],
        now: object,
    ) -> ShadowMutationResult: ...


class RuntimeReadinessPort(Protocol):
    @property
    def state(self) -> RuntimeLifecycleState: ...


class CreatedIncidentObligationPort(Protocol):
    def handle_processed_created_incident(
        self, processed: ProcessedCorrelationRecord
    ) -> object: ...


class TerminalFailureRecorderPort(Protocol):
    def record_terminal_failure(
        self,
        intent: CorrelationMutationIntent,
        error: object,
        *,
        source_domain: str,
    ) -> object: ...


class InitialExecutionStatus(str, Enum):
    PROCESSED = "PROCESSED"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"


@dataclass(frozen=True, slots=True)
class InitialExecutionResult:
    event_id: str
    status: InitialExecutionStatus
    processed: ProcessedCorrelationRecord
    decision: CorrelationDecision | None
    reconciled_existing_receipt: bool


class RuntimeInitialProcessingError(RuntimeError):
    """The Event cannot safely advance through the Phase 3 protocol."""


class RuntimeConcurrentClaimError(RuntimeInitialProcessingError):
    pass


class RuntimePolicyEvaluationError(RuntimeInitialProcessingError):
    def __init__(self, failure: CorrelationEvaluationFailure) -> None:
        super().__init__("SPEC-006 returned a typed evaluation failure")
        self.failure = failure


class RuntimeNonTerminalDecisionError(RuntimeInitialProcessingError):
    pass


class RuntimeClaimAuthorityError(RuntimeInitialProcessingError):
    pass


class RuntimeOwnershipIntegrityError(RuntimeInitialProcessingError):
    pass


class InitialCorrelationOrchestrator:
    """Execute only INITIAL terminal decisions through public domain APIs.

    Event input is rediscovered through authoritative Event intake for every
    fresh or recovery attempt.  Unresolved Intents are reconciled without
    policy reevaluation and all uncertain failures retain their active claim.
    """

    def __init__(
        self,
        *,
        event_intake: DurableEventIntake,
        state_store: CorrelationStateMutationPort,
        policy_engine: AlertCorrelationPolicyEngine,
        incident_store: IncidentTerminalReadPort,
        incident_manager: IncidentMutationPort,
        shadow_store: ShadowTerminalReadPort,
        shadow_manager: ShadowMutationPort,
        clock: RuntimeClock,
        readiness: RuntimeReadinessPort,
        post_create_obligation: CreatedIncidentObligationPort | None = None,
        terminal_failure_recorder: TerminalFailureRecorderPort | None = None,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        if not isinstance(event_intake, DurableEventIntake):
            raise TypeError("event_intake must be DurableEventIntake")
        if not isinstance(policy_engine, AlertCorrelationPolicyEngine):
            raise TypeError("policy_engine must be the real SPEC-006 engine")
        for owner, methods in (
            (state_store, ("acquire_claim", "release_claim", "resolve", "begin_intent", "finalize_processed")),
            (incident_store, ("list_correlation_views", "event_has_incident_owner", "get_operation_result", "get_incident")),
            (incident_manager, ("apply_correlation_mutation",)),
            (shadow_store, ("get_shadow_by_event_id", "get_operation_result")),
            (shadow_manager, ("route_shadow",)),
        ):
            if any(not callable(getattr(owner, method, None)) for method in methods):
                raise TypeError("Runtime dependency does not provide its required public API")
        if not isinstance(clock, RuntimeClock):
            raise TypeError("clock must be RuntimeClock")
        if not isinstance(getattr(readiness, "state", None), RuntimeLifecycleState):
            raise TypeError("readiness must expose RuntimeLifecycleState")
        if post_create_obligation is not None and not callable(
            getattr(post_create_obligation, "handle_processed_created_incident", None)
        ):
            raise TypeError("post_create_obligation lacks its Runtime public API")
        if terminal_failure_recorder is not None and not callable(
            getattr(terminal_failure_recorder, "record_terminal_failure", None)
        ):
            raise TypeError("terminal_failure_recorder lacks its Runtime public API")
        self._event_intake = event_intake
        self._state_store = state_store
        self._policy_engine = policy_engine
        self._incident_store = incident_store
        self._incident_manager = incident_manager
        self._shadow_store = shadow_store
        self._shadow_manager = shadow_manager
        self._clock = clock
        self._readiness = readiness
        self._post_create_obligation = post_create_obligation
        self._terminal_failure_recorder = terminal_failure_recorder
        self._telemetry = telemetry or NullRuntimeTelemetry()

    def process_authoritative_event(self, event_id: str) -> InitialExecutionResult:
        """Acquire a claim and execute or resume one authoritative Event."""
        self._require_ready()
        event = self._authoritative_event(event_id)
        claim = self._state_store.acquire_claim(event_id)
        if claim is None:
            self._emit_claim(event_id, "REFUSED")
            raise RuntimeConcurrentClaimError(
                f"Event {event_id} already has an active processing claim"
            )
        self._emit_claim(event_id, "ACQUIRED")

        resolved = self._state_store.resolve(event_id)
        if resolved.processed is not None:
            self._verify_processed_ownership(resolved.processed)
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            return InitialExecutionResult(
                event_id,
                InitialExecutionStatus.ALREADY_PROCESSED,
                resolved.processed,
                None,
                True,
            )
        if resolved.intent is not None:
            return self._execute_intent(event, resolved.intent, claim, decision=None)
        if resolved.state is not ResolvedState.UNSEEN or resolved.claim != claim:
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            raise RuntimeInitialProcessingError(
                f"Event {event_id} is not eligible for fresh INITIAL evaluation"
            )

        views = self._fresh_incident_views()
        evaluation = self._policy_engine.evaluate(
            event,
            views,
            CorrelationEvaluationContext(EvaluationPhase.INITIAL),
        )
        if isinstance(evaluation, CorrelationEvaluationFailure):
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            raise RuntimePolicyEvaluationError(evaluation)
        if not isinstance(evaluation, CorrelationEvaluationSuccess):
            raise RuntimeInitialProcessingError("SPEC-006 returned an unsupported result")
        decision = evaluation.decision
        if decision.decision_type not in {
            DecisionType.ATTACH_EXISTING,
            DecisionType.CREATE_NEW,
            DecisionType.ROUTE_SHADOW,
        }:
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            raise RuntimeNonTerminalDecisionError(
                "Phase 3 executes terminal INITIAL Decisions only"
            )

        self._require_fresh_unseen_claim(event_id, claim)
        intent = CorrelationMutationIntent.from_decision(
            operation_id=runtime_operation_id("TERMINAL_CORRELATION", event_id),
            event_id=event_id,
            decision=decision,
            created_at=self._clock.now(),
        )
        durable_intent = self._state_store.begin_intent(intent, claim)
        return self._execute_intent(event, durable_intent, claim, decision=decision)

    def resume_authoritative_intent(
        self, event_id: str, claim: ProcessingClaim, *, attempt: int = 1
    ) -> InitialExecutionResult:
        """Resume one durable Intent with its valid or formally reclaimed claim."""
        self._require_recovery_or_ready()
        event = self._authoritative_event(event_id)
        resolved = self._state_store.resolve(event_id)
        if resolved.processed is not None:
            self._verify_processed_ownership(resolved.processed)
            if resolved.claim == claim:
                self._state_store.release_claim(claim)
                self._emit_claim(event_id, "RELEASED")
            return InitialExecutionResult(
                event_id,
                InitialExecutionStatus.ALREADY_PROCESSED,
                resolved.processed,
                None,
                True,
            )
        if resolved.intent is None:
            raise RuntimeInitialProcessingError("no durable Intent exists to reconcile")
        self._require_claim(resolved, claim, resolved.intent)
        return self._execute_intent(
            event, resolved.intent, claim, decision=None, attempt=attempt
        )

    def reconcile_authoritative_intent_receipt(
        self, event_id: str, claim: ProcessingClaim
    ) -> InitialExecutionResult | None:
        """Finalize a committed same-operation receipt without issuing a mutation."""
        self._require_recovery_or_ready()
        event = self._authoritative_event(event_id)
        resolved = self._state_store.resolve(event_id)
        if resolved.processed is not None:
            return self.resume_authoritative_intent(event_id, claim)
        if resolved.intent is None:
            raise RuntimeInitialProcessingError("no durable Intent exists to reconcile")
        self._require_claim(resolved, claim, resolved.intent)
        intent = resolved.intent
        if intent.decision_type in {DecisionType.ATTACH_EXISTING, DecisionType.CREATE_NEW}:
            receipt = self._incident_store.get_operation_result(intent.operation_id)
        elif intent.decision_type is DecisionType.ROUTE_SHADOW:
            receipt = self._shadow_store.get_operation_result(intent.operation_id)
        else:
            raise RuntimeNonTerminalDecisionError(
                "durable Intent contains a non-terminal Decision"
            )
        if receipt is None:
            return None
        return self._execute_intent(event, intent, claim, decision=None)

    def execute_authoritative_terminal_decision(
        self,
        event_id: str,
        decision: CorrelationDecision,
        claim: ProcessingClaim,
    ) -> InitialExecutionResult:
        """Start the Phase 3 protocol for a terminal decision made elsewhere.

        Pending reevaluation uses this public Runtime composition boundary so
        terminal decisions do not grow a second mutation implementation.
        """
        self._require_recovery_or_ready()
        if not isinstance(decision, CorrelationDecision):
            raise TypeError("decision must be a real SPEC-006 CorrelationDecision")
        if decision.decision_type not in {
            DecisionType.ATTACH_EXISTING,
            DecisionType.CREATE_NEW,
            DecisionType.ROUTE_SHADOW,
        }:
            raise RuntimeNonTerminalDecisionError(
                "only terminal Decisions may enter the terminal protocol"
            )
        event = self._authoritative_event(event_id)
        resolved = self._state_store.resolve(event_id)
        if (
            resolved.claim != claim
            or resolved.processed is not None
            or resolved.intent is not None
        ):
            raise RuntimeClaimAuthorityError(
                "current State is not eligible to begin a terminal Intent"
            )
        if resolved.pending is not None and (
            decision.policy_id,
            decision.policy_version,
        ) != (resolved.pending.policy_id, resolved.pending.policy_version):
            raise RuntimeInitialProcessingError(
                "terminal Pending Decision changed historical policy identity"
            )
        intent = CorrelationMutationIntent.from_decision(
            operation_id=runtime_operation_id("TERMINAL_CORRELATION", event_id),
            event_id=event_id,
            decision=decision,
            created_at=self._clock.now(),
        )
        durable_intent = self._state_store.begin_intent(intent, claim)
        return self._execute_intent(event, durable_intent, claim, decision=decision)

    def _require_ready(self) -> None:
        if self._readiness.state is not RuntimeLifecycleState.READY:
            raise RuntimeInitialProcessingError(
                "INITIAL execution requires a completed Startup Recovery Barrier"
            )

    def _require_recovery_or_ready(self) -> None:
        if self._readiness.state not in {
            RuntimeLifecycleState.RECOVERY,
            RuntimeLifecycleState.READY,
        }:
            raise RuntimeInitialProcessingError(
                "Intent recovery requires the Startup Recovery Barrier"
            )

    def _emit_claim(self, event_id: str, result: str) -> None:
        self._telemetry.emit(
            RuntimeTelemetryEvent.WORK_UPDATED,
            observed_at=self._clock.now(),
            event_id=event_id,
            stage="CLAIM",
            claim_result=result,
        )

    def _authoritative_event(self, event_id: str) -> Mapping[str, object]:
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
            raise ValueError("event_id must be a non-empty, trimmed string")
        snapshot = self._event_intake.scan_authoritative()
        event = snapshot.event_by_id.get(event_id)
        if event is None:
            raise RuntimeInitialProcessingError(
                f"Event {event_id} is absent from authoritative EventStore"
            )
        return event

    def _fresh_incident_views(self) -> Sequence[IncidentCorrelationView]:
        views = self._incident_store.list_correlation_views()
        if not isinstance(views, tuple) or any(
            not isinstance(view, IncidentCorrelationView) for view in views
        ):
            raise RuntimeInitialProcessingError(
                "SPEC-008 returned malformed IncidentCorrelationViews"
            )
        return views

    def _require_fresh_unseen_claim(
        self, event_id: str, claim: ProcessingClaim
    ) -> None:
        resolved = self._state_store.resolve(event_id)
        if resolved.state is not ResolvedState.UNSEEN or resolved.claim != claim:
            raise RuntimeClaimAuthorityError(
                "State changed or processing claim was lost before Intent"
            )

    @staticmethod
    def _require_claim(
        resolved: ResolvedCorrelationState,
        claim: ProcessingClaim,
        intent: CorrelationMutationIntent,
    ) -> None:
        if resolved.claim != claim or resolved.intent != intent:
            raise RuntimeClaimAuthorityError(
                "current claim or durable Intent authority does not match execution"
            )

    def _execute_intent(
        self,
        event: Mapping[str, object],
        intent: CorrelationMutationIntent,
        claim: ProcessingClaim,
        *,
        decision: CorrelationDecision | None,
        attempt: int = 1,
    ) -> InitialExecutionResult:
        self._require_claim(self._state_store.resolve(intent.event_id), claim, intent)
        try:
            if intent.decision_type in {
                DecisionType.ATTACH_EXISTING,
                DecisionType.CREATE_NEW,
            }:
                processed, replayed = self._execute_incident(
                    event, intent, claim, attempt=attempt
                )
            elif intent.decision_type is DecisionType.ROUTE_SHADOW:
                processed, replayed = self._execute_shadow(
                    event, intent, claim, attempt=attempt
                )
            else:
                raise RuntimeNonTerminalDecisionError(
                    "durable Intent contains a non-terminal Decision"
                )
        except IncidentDomainError as exc:
            if self._terminal_failure_recorder is not None:
                self._terminal_failure_recorder.record_terminal_failure(
                    intent, exc, source_domain="SPEC-008"
                )
            raise
        except ShadowDomainError as exc:
            if self._terminal_failure_recorder is not None:
                self._terminal_failure_recorder.record_terminal_failure(
                    intent, exc, source_domain="SPEC-010"
                )
            raise
        self._state_store.release_claim(claim)
        self._emit_claim(intent.event_id, "RELEASED")
        self._telemetry.emit(
            RuntimeTelemetryEvent.RECONCILIATION,
            observed_at=self._clock.now(),
            event_id=intent.event_id,
            stage="TERMINAL_RECONCILIATION",
            decision=intent.decision_type,
            policy_id=intent.policy_id,
            policy_version=intent.policy_version,
            operation_id=intent.operation_id,
            incident_id=processed.incident_id,
            shadow_id=processed.shadow_ref,
            reconciliation_result="RECEIPT_REPLAYED" if replayed else "DOMAIN_CONFIRMED",
        )
        result = InitialExecutionResult(
            intent.event_id,
            InitialExecutionStatus.PROCESSED,
            processed,
            decision,
            replayed,
        )
        if (
            processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
            and self._post_create_obligation is not None
        ):
            self._post_create_obligation.handle_processed_created_incident(processed)
        return result

    def _execute_incident(
        self,
        event: Mapping[str, object],
        intent: CorrelationMutationIntent,
        claim: ProcessingClaim,
        *,
        attempt: int = 1,
    ) -> tuple[ProcessedCorrelationRecord, bool]:
        self._require_no_shadow_owner(intent.event_id)
        result = self._incident_store.get_operation_result(intent.operation_id)
        replayed = result is not None
        if result is None:
            self._require_claim(self._state_store.resolve(intent.event_id), claim, intent)
            self._emit_domain_attempt(intent, "SPEC-008", attempt)
            returned = self._incident_manager.apply_correlation_mutation(
                IncidentMutationRequest(intent, event, self._clock.now())
            )
            result = self._incident_store.get_operation_result(intent.operation_id)
            if result is None or returned != result:
                raise RuntimeOwnershipIntegrityError(
                    "SPEC-008 success has no matching authoritative operation result"
                )
        self._verify_incident_result(intent, result)
        self._require_no_shadow_owner(intent.event_id)
        self._require_claim(self._state_store.resolve(intent.event_id), claim, intent)
        processed = ProcessedCorrelationRecord(
            event_id=intent.event_id,
            terminal_outcome=intent.intended_terminal_outcome,
            resolved_at=result.completed_at,
            incident_id=result.incident_id,
            shadow_ref=None,
            policy_id=intent.policy_id,
            policy_version=intent.policy_version,
        )
        return self._state_store.finalize_processed(processed, claim), replayed

    def _execute_shadow(
        self,
        event: Mapping[str, object],
        intent: CorrelationMutationIntent,
        claim: ProcessingClaim,
        *,
        attempt: int = 1,
    ) -> tuple[ProcessedCorrelationRecord, bool]:
        self._require_no_incident_owner(intent.event_id)
        result = self._shadow_store.get_operation_result(intent.operation_id)
        replayed = result is not None
        if result is None:
            self._require_claim(self._state_store.resolve(intent.event_id), claim, intent)
            self._emit_domain_attempt(intent, "SPEC-010", attempt)
            returned = self._shadow_manager.route_shadow(
                intent, event, self._clock.now()
            )
            result = self._shadow_store.get_operation_result(intent.operation_id)
            if result is None or returned != result:
                raise RuntimeOwnershipIntegrityError(
                    "SPEC-010 success has no matching authoritative operation result"
                )
        self._verify_shadow_result(intent, result)
        self._require_no_incident_owner(intent.event_id)
        self._require_claim(self._state_store.resolve(intent.event_id), claim, intent)
        processed = ProcessedCorrelationRecord(
            event_id=intent.event_id,
            terminal_outcome=TerminalOutcome.SHADOWED,
            resolved_at=result.entered_shadow_at,
            incident_id=None,
            shadow_ref=result.shadow_id,
            policy_id=intent.policy_id,
            policy_version=intent.policy_version,
        )
        return self._state_store.finalize_processed(processed, claim), replayed

    def _emit_domain_attempt(
        self, intent: CorrelationMutationIntent, source_domain: str, attempt: int
    ) -> None:
        self._telemetry.emit(
            RuntimeTelemetryEvent.WORK_OBSERVED,
            observed_at=self._clock.now(),
            event_id=intent.event_id,
            stage="DOMAIN_ATTEMPT",
            decision=intent.decision_type,
            policy_id=intent.policy_id,
            policy_version=intent.policy_version,
            operation_id=intent.operation_id,
            source_domain=source_domain,
            attempt=attempt,
        )

    def _verify_incident_result(
        self, intent: CorrelationMutationIntent, result: IncidentOperationResult
    ) -> None:
        if (
            not isinstance(result, IncidentOperationResult)
            or result.operation_id != intent.operation_id
            or result.event_id != intent.event_id
            or result.mutation_kind is not intent.decision_type
        ):
            raise RuntimeOwnershipIntegrityError(
                "SPEC-008 operation result contradicts durable Intent"
            )
        incident = self._incident_store.get_incident(result.incident_id)
        has_owner = self._incident_store.event_has_incident_owner(intent.event_id)
        if (
            not isinstance(incident, IncidentRecord)
            or intent.event_id not in incident.event_ids
            or has_owner is not True
        ):
            raise RuntimeOwnershipIntegrityError(
                "SPEC-008 result is not backed by authoritative Event ownership"
            )

    def _verify_shadow_result(
        self, intent: CorrelationMutationIntent, result: ShadowMutationResult
    ) -> None:
        if (
            not isinstance(result, ShadowMutationResult)
            or result.operation_id != intent.operation_id
        ):
            raise RuntimeOwnershipIntegrityError(
                "SPEC-010 operation result contradicts durable Intent"
            )
        shadow = self._shadow_store.get_shadow_by_event_id(intent.event_id)
        if not isinstance(shadow, ShadowRecord) or shadow.shadow_id != result.shadow_id:
            raise RuntimeOwnershipIntegrityError(
                "SPEC-010 result is not backed by authoritative Event ownership"
            )

    def _require_no_shadow_owner(self, event_id: str) -> None:
        shadow = self._shadow_store.get_shadow_by_event_id(event_id)
        if shadow is not None:
            raise RuntimeOwnershipIntegrityError(
                "Event has contradictory or opposite Shadow ownership"
            )

    def _require_no_incident_owner(self, event_id: str) -> None:
        has_owner = self._incident_store.event_has_incident_owner(event_id)
        if not isinstance(has_owner, bool):
            raise RuntimeOwnershipIntegrityError(
                "SPEC-008 ownership read did not return authoritative boolean evidence"
            )
        if has_owner:
            raise RuntimeOwnershipIntegrityError(
                "Event has contradictory or opposite Incident ownership"
            )

    def _verify_processed_ownership(
        self, processed: ProcessedCorrelationRecord
    ) -> None:
        shadow = self._shadow_store.get_shadow_by_event_id(processed.event_id)
        incident_owner = self._incident_store.event_has_incident_owner(
            processed.event_id
        )
        if not isinstance(incident_owner, bool):
            raise RuntimeOwnershipIntegrityError(
                "SPEC-008 ownership read did not return authoritative boolean evidence"
            )
        if processed.terminal_outcome is TerminalOutcome.SHADOWED:
            if (
                incident_owner
                or not isinstance(shadow, ShadowRecord)
                or shadow.shadow_id != processed.shadow_ref
            ):
                raise RuntimeOwnershipIntegrityError(
                    "Processed Shadow outcome contradicts domain ownership"
                )
            return
        incident = (
            self._incident_store.get_incident(processed.incident_id)
            if processed.incident_id is not None
            else None
        )
        if (
            shadow is not None
            or not incident_owner
            or not isinstance(incident, IncidentRecord)
            or processed.event_id not in incident.event_ids
        ):
            raise RuntimeOwnershipIntegrityError(
                "Processed Incident outcome contradicts domain ownership"
            )
