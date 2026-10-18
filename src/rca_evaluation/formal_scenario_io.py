"""Governed Slice 4 injection and public-observation adapters.

The module contains no Docker control, scheduler, retry loop, Oracle, grader,
or production write API.  It composes the existing Scenario Runtime trigger,
EventStore authoritative read, and Candidate A/B/C/D public projections.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Mapping, Protocol

import yaml

from incident_management.contracts import IncidentStatus
from llm_generation.contracts import FailureClass, LocalReadStatus
from rca_persistence.contracts import (
    GenerationLifecycle, LogicalTryResultKind,
)
from scenario_runtime.schema import ScenarioId, ScenarioPhase, ScenarioTriggerResult

from .campaign import (
    AuthoritativeOutcomeStatus, CampaignPlan, FailureCase,
    KnowledgeDisposition, PlannedExecution, UpstreamOutcome,
)
from .fixtures import require_verified_fixture_resolver
from .formal_compose import resolve_formal_compose_topology
from .formal_fixture_sources import FormalFixtureSourceArtifact
from .identity import canonical_text_commitment, commitment, require_identity
from .latency import ExternalObservation, ObservationClock
from .ledger import LedgerRecord
from .observation import (
    CapturedInputBoundary, ObservationDetail, ObservationOutcome,
    PublicObservationResolver,
)
from .oracle import SCENARIOS
from .sqlite_store import SqliteEvaluationStore


FORMAL_INJECTOR_CONTRACT = "spec017.formal-scenario-injector.v1"
FORMAL_OBSERVER_CONTRACT = "spec017.formal-public-observer.v1"
FORMAL_POLLING_INTERVAL_MS = 1_000
FORMAL_TIMEOUT_MS = 300_000

_PRODUCTION_EVENT_TYPES = {
    "S1": "brute_force_detected",
    "S2": "cross_service_failure",
    "S3": "oom_crash_detected",
    "S4": "external_dependency_failure",
    "S5": "downstream_cascade_failure",
    "S6": "rate_limit_storm",
}
_FORBIDDEN_PRODUCTION_KEYS = frozenset({
    "oracle", "oracle_answer", "ground_truth", "ground_truth_label",
    "expected_root_cause", "expected_rca", "expected_causal_class",
    "scoring_predicate", "reviewer_answer", "review_verdict",
    "expected_success", "expected_failure", "expected_disposition",
})


def _contains_ground_truth(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).strip().lower().replace("-", "_")
                in _FORBIDDEN_PRODUCTION_KEYS
            or _contains_ground_truth(item)
            for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(_contains_ground_truth(item) for item in value)
    return False


class ActualExecutionState(str, Enum):
    NOT_EXECUTED = "NOT_EXECUTED"
    UNRESOLVED = "UNRESOLVED"


class InjectionLifecycleState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    INJECTION_AUTHORIZED = "INJECTION_AUTHORIZED"
    SIGNAL_SENT = "SIGNAL_SENT"
    OBSERVING = "OBSERVING"
    TERMINAL = "TERMINAL"


class FormalObservationDisposition(str, Enum):
    OBSERVED = "OBSERVED"
    FAILED = "FAILED"
    CENSORED = "CENSORED"
    INVALID = "INVALID"


class ObservationReasonCode(str, Enum):
    EVENT_BINDING_MISSING = "EVENT_BINDING_MISSING"
    EVENT_BINDING_CONFLICT = "EVENT_BINDING_CONFLICT"
    INCIDENT_LINEAGE_AMBIGUOUS = "INCIDENT_LINEAGE_AMBIGUOUS"
    RCA_LINEAGE_AMBIGUOUS = "RCA_LINEAGE_AMBIGUOUS"
    PUBLIC_STATE_CONTRADICTION = "PUBLIC_STATE_CONTRADICTION"
    READ_INTEGRITY_FAILURE = "READ_INTEGRITY_FAILURE"
    TERMINAL_PRODUCTION_FAILURE = "TERMINAL_PRODUCTION_FAILURE"
    TIMEOUT_REACHED = "TIMEOUT_REACHED"


@dataclass(frozen=True, slots=True)
class FormalObservationPollingContract:
    adapter_version: str = FORMAL_OBSERVER_CONTRACT
    polling_interval_ms: int = FORMAL_POLLING_INTERVAL_MS
    timeout_ms: int = FORMAL_TIMEOUT_MS
    clock_authority: str = "Candidate-F ObservationClock"
    actual_observation: ActualExecutionState = ActualExecutionState.UNRESOLVED

    def validate(self) -> None:
        if (self.adapter_version != FORMAL_OBSERVER_CONTRACT
                or self.polling_interval_ms != FORMAL_POLLING_INTERVAL_MS
                or self.timeout_ms != FORMAL_TIMEOUT_MS
                or self.clock_authority != "Candidate-F ObservationClock"
                or self.actual_observation is not ActualExecutionState.UNRESOLVED):
            raise ValueError("Formal observation polling contract is contradictory")


class AmbiguousInjectionState(RuntimeError):
    """A durable authorization exists, so reinjection could duplicate effects."""


class FormalEventPublicReads(Protocol):
    def read_all_authoritative(self) -> list[dict]: ...


@dataclass(frozen=True, slots=True)
class AuthoritativeEventBinding:
    execution_identity: str
    operation_identity: str
    event_identity: str
    event_content_commitment: str
    event_isolation_commitment: str

    def validate(self) -> None:
        require_identity(self.execution_identity, "execution")
        if (not isinstance(self.operation_identity, str)
                or not self.operation_identity.startswith("injection:")
                or not isinstance(self.event_identity, str)
                or not self.event_identity.strip()
                or not self.event_content_commitment.startswith("sha256:")
                or not self.event_isolation_commitment.startswith("sha256:")):
            raise ValueError("authoritative Event binding is malformed")


class FormalEventOwnershipResolver(Protocol):
    """Read-only external proof over an execution-owned EventStore."""

    def resolve(self, operation_identity: str) -> AuthoritativeEventBinding | None: ...


class ScenarioTriggerPort(Protocol):
    def trigger(
        self, scenario_id: ScenarioId | str, *, requested_at: float | None = None,
    ) -> ScenarioTriggerResult: ...


class GenerationRecoveryPublicReads(Protocol):
    def recovery(self) -> object: ...


class RcaLineagePublicReads(Protocol):
    def get_aggregate_by_incident(self, incident_id: str) -> object | None: ...
    def get_attempt_lineage(self, attempt_id: str) -> object | None: ...


@dataclass(frozen=True, slots=True)
class FormalScenarioInput:
    preparation_id: str
    evaluation_run_identity: str
    planned_run_identity: str
    execution_identity: str
    planned_execution_identity: str
    scenario_id: str
    repetition: int
    slot_ordinal: int
    official_source_reference: str
    source_commitment: str
    input_identity: str
    input_commitment: str
    injector_contract: str
    production_event_type: str
    compose_project_identity: str
    event_store_volume_identity: str
    event_isolation_commitment: str
    actual_injection: ActualExecutionState
    injector_identity: str

    def material(self, *, include_identity: bool = True) -> dict[str, object]:
        result = {
            name: (value.value if isinstance(value, Enum) else value)
            for name, value in asdict(self).items()
            if name != "injector_identity"
        }
        if include_identity:
            result["injector_identity"] = self.injector_identity
        return result

    def validate(self) -> None:
        require_identity(self.preparation_id, "preparation")
        require_identity(self.evaluation_run_identity, "evaluation_run")
        require_identity(self.planned_run_identity, "planned_run")
        require_identity(self.execution_identity, "execution")
        require_identity(self.planned_execution_identity, "planned_execution")
        if (self.scenario_id not in SCENARIOS
                or type(self.repetition) is not int or not 1 <= self.repetition <= 5
                or self.slot_ordinal != (self.repetition - 1) * 6
                    + SCENARIOS.index(self.scenario_id) + 1
                or self.official_source_reference
                    != f"configs/scenarios.yaml#scenarios.{self.scenario_id}"
                or self.injector_contract != FORMAL_INJECTOR_CONTRACT
                or self.production_event_type
                    != _PRODUCTION_EVENT_TYPES[self.scenario_id]
                or not isinstance(self.event_store_volume_identity, str)
                or not self.event_store_volume_identity.startswith(
                    self.compose_project_identity + "_")
                or self.event_isolation_commitment != commitment({
                    "preparation_id": self.preparation_id,
                    "planned_execution_identity":
                        self.planned_execution_identity,
                    "compose_project_identity": self.compose_project_identity,
                    "event_store_volume_identity":
                        self.event_store_volume_identity,
                    "ownership": "EXECUTION_OWNED_EVENT_STORE",
                })
                or self.actual_injection is not ActualExecutionState.NOT_EXECUTED
                or not self.source_commitment.startswith("sha256:")
                or not self.input_commitment.startswith("sha256:")
                or self.injector_identity != "injector:" + commitment(
                    self.material(include_identity=False))[7:]
                or _contains_ground_truth(self.material())):
            raise ValueError("Formal scenario input contract is contradictory")


@dataclass(frozen=True, slots=True)
class FormalScenarioRegistry:
    preparation_id: str
    entries: tuple[FormalScenarioInput, ...]
    registry_commitment: str

    def material(self, *, include_commitment: bool = True) -> dict[str, object]:
        result = {
            "preparation_id": self.preparation_id,
            "entries": [item.material() for item in self.entries],
        }
        if include_commitment:
            result["registry_commitment"] = self.registry_commitment
        return result

    def validate(self) -> None:
        require_identity(self.preparation_id, "preparation")
        for entry in self.entries:
            entry.validate()
        expected = tuple((scenario, repetition)
                         for repetition in range(1, 6)
                         for scenario in SCENARIOS)
        observed = tuple((item.scenario_id, item.repetition)
                         for item in self.entries)
        if (observed != expected or len(self.entries) != 30
                or len({item.execution_identity for item in self.entries}) != 30
                or len({item.planned_execution_identity
                        for item in self.entries}) != 30
                or commitment(self.material(include_commitment=False))
                    != self.registry_commitment
                or _contains_ground_truth(self.material())):
            raise ValueError("Formal scenario registry is incomplete or contradictory")

    def for_execution(self, execution_id: str) -> FormalScenarioInput:
        require_identity(execution_id, "execution")
        matches = tuple(item for item in self.entries
                        if item.execution_identity == execution_id)
        if len(matches) != 1:
            raise ValueError("execution has no exact Formal scenario input")
        return matches[0]


@dataclass(frozen=True, slots=True)
class ControlledInjectionSource:
    fixture_identity: str
    scenario_id: str
    input_commitment: str
    production_input: dict[str, object]
    injector_contract: str = FORMAL_INJECTOR_CONTRACT

    def __post_init__(self) -> None:
        if (self.scenario_id not in SCENARIOS
                or not self.input_commitment.startswith("sha256:")
                or self.injector_contract != FORMAL_INJECTOR_CONTRACT
                or _contains_ground_truth(self.production_input)):
            raise ValueError("controlled injection source leaks evaluation truth")


def project_r2_controlled_injection_sources(
    artifact: FormalFixtureSourceArtifact,
) -> tuple[ControlledInjectionSource, ...]:
    """Project only R2 production controls; evaluation_truth is unreachable."""
    if type(artifact) is not FormalFixtureSourceArtifact:
        raise TypeError("official R2 fixture source artifact is required")
    values = []
    for case in artifact.cases:
        production = case.production_payload()
        scenario = production.get("scenario_trigger")
        values.append(ControlledInjectionSource(
            case.fixture_identity, str(scenario), case.input_commitment,
            production))
    return tuple(values)


def resolve_formal_scenario_registry(
    store: SqliteEvaluationStore, preparation_id: str,
    campaign: CampaignPlan, repository_root: str | Path,
) -> FormalScenarioRegistry:
    """Fresh-read official source/config and bind all 30 fixed nominal slots."""
    if type(store) is not SqliteEvaluationStore:
        raise TypeError("Formal scenario registry requires SqliteEvaluationStore")
    if type(campaign) is not CampaignPlan:
        raise TypeError("Formal scenario registry requires CampaignPlan")
    require_identity(preparation_id, "preparation")
    if (campaign.repetitions != 5 or len(campaign.executions) != 30
            or any(item.is_replacement for item in campaign.executions)):
        raise ValueError("Formal scenario registry requires 30 unreplaced nominal slots")
    root = Path(repository_root).resolve()
    prepared = store.get_preparation(preparation_id)
    if prepared is None:
        raise ValueError("Formal scenario registry requires PREPARED state")
    topology = resolve_formal_compose_topology(
        store, preparation_id, root)
    resolver = require_verified_fixture_resolver(campaign.fixture_resolver)
    nominal = {item.scenario_id: item for item in resolver.entries
               if item.variant == "nominal"}
    if set(nominal) != set(SCENARIOS):
        raise ValueError("official nominal fixture inventory is incomplete")
    source_path = root / "configs/scenarios.yaml"
    source_bytes = source_path.read_bytes()
    source_commitment = canonical_text_commitment(source_bytes)
    source = yaml.safe_load(source_bytes)
    scenarios = source.get("scenarios") if isinstance(source, Mapping) else None
    if not isinstance(scenarios, Mapping) or set(scenarios) != set(SCENARIOS):
        raise ValueError("official scenario configuration is invalid")
    lineage = prepared.get("planned_lineage")
    if not isinstance(lineage, Mapping):
        raise ValueError("prepared planned lineage is unavailable")
    planned_run = lineage.get("planned_run_identity")
    require_identity(planned_run, "planned_run")
    envelopes = {(item.scenario_id, item.repetition): item
                 for item in topology.envelopes}
    entries = []
    for ordinal, execution in enumerate(campaign.executions, 1):
        fixture = nominal.get(execution.scenario_id)
        envelope = envelopes.get((execution.scenario_id, execution.repetition))
        scenario_material = scenarios.get(execution.scenario_id)
        if (fixture is None or envelope is None
                or fixture.source_commitment != source_commitment
                or fixture.source_reference
                    != f"configs/scenarios.yaml#scenarios.{execution.scenario_id}"
                or fixture.input_identity != execution.input_identity
                or fixture.input_commitment != execution.input_commitment
                or not isinstance(scenario_material, Mapping)):
            raise ValueError("official scenario fixture commitment drifted")
        event_mounts = tuple(
            item for item in envelope.volume_mounts
            if item.role == "runtime-events" and item.target == "/app/events")
        if len(event_mounts) != 1:
            raise ValueError("execution-owned EventStore volume is unavailable")
        event_volume = event_mounts[0].volume_identity
        event_isolation = commitment({
            "preparation_id": preparation_id,
            "planned_execution_identity": envelope.planned_execution_identity,
            "compose_project_identity": envelope.compose_project_identity,
            "event_store_volume_identity": event_volume,
            "ownership": "EXECUTION_OWNED_EVENT_STORE",
        })
        values = dict(
            preparation_id=preparation_id,
            evaluation_run_identity=campaign.run_id,
            planned_run_identity=planned_run,
            execution_identity=execution.execution_id,
            planned_execution_identity=envelope.planned_execution_identity,
            scenario_id=execution.scenario_id,
            repetition=execution.repetition,
            slot_ordinal=ordinal,
            official_source_reference=fixture.source_reference,
            source_commitment=source_commitment,
            input_identity=execution.input_identity,
            input_commitment=execution.input_commitment,
            injector_contract=FORMAL_INJECTOR_CONTRACT,
            production_event_type=_PRODUCTION_EVENT_TYPES[execution.scenario_id],
            compose_project_identity=envelope.compose_project_identity,
            event_store_volume_identity=event_volume,
            event_isolation_commitment=event_isolation,
            actual_injection=ActualExecutionState.NOT_EXECUTED,
        )
        entry = FormalScenarioInput(
            **values, injector_identity="injector:" + commitment({
                **values, "actual_injection": values["actual_injection"].value,
            })[7:])
        entry.validate()
        entries.append(entry)
    provisional = FormalScenarioRegistry(
        preparation_id, tuple(entries), "sha256:" + "0" * 64)
    result = FormalScenarioRegistry(
        preparation_id, tuple(entries),
        commitment(provisional.material(include_commitment=False)))
    result.validate()
    return result


def _record_id(execution_id: str, label: str) -> str:
    return LedgerRecord.create(
        "observation", [execution_id, label], {}, execution_id).record_id


def injection_lifecycle_state(
    store: SqliteEvaluationStore, execution_id: str,
) -> InjectionLifecycleState:
    require_identity(execution_id, "execution")
    if (store.get(_record_id(execution_id, "final")) is not None
            or store.get(_record_id(
                execution_id, "formal-public-observation-terminal")) is not None):
        return InjectionLifecycleState.TERMINAL
    if store.get(_record_id(execution_id, "formal-observation-started")) is not None:
        return InjectionLifecycleState.OBSERVING
    if store.get(_record_id(execution_id, "formal-signal-sent")) is not None:
        return InjectionLifecycleState.SIGNAL_SENT
    if store.get(_record_id(execution_id, "formal-injection-authorized")) is not None:
        return InjectionLifecycleState.INJECTION_AUTHORIZED
    return InjectionLifecycleState.NOT_STARTED


def _require_formal_execution_lineage(
    store: SqliteEvaluationStore, plan: FormalScenarioInput,
) -> LedgerRecord:
    """Fresh-read the ADMITTED Formal run and exact execution slot."""
    store.require_formal_run(plan.evaluation_run_identity)
    execution = store.get(plan.execution_identity)
    if execution is None or execution.domain != "execution":
        raise ValueError("durable execution slot is absent")
    payload = json.loads(execution.semantic_json)
    if (execution.parent_id != plan.evaluation_run_identity
            or payload.get("scenario_id") != plan.scenario_id
            or payload.get("repetition") != plan.repetition
            or payload.get("input_identity") != plan.input_identity
            or payload.get("input_commitment") != plan.input_commitment):
        raise ValueError("durable Formal execution lineage is contradictory")
    return execution


@dataclass(frozen=True, slots=True)
class InjectionReceipt:
    execution_identity: str
    injector_identity: str
    operation_identity: str
    scenario_id: str
    trigger_count: int
    event_baseline_ids: tuple[str, ...]
    event_isolation_commitment: str
    event_binding_state: str
    sent_record_id: str


class FormalScenarioInjector:
    """Calls only Scenario Runtime.trigger after durable Candidate-F authority."""

    def __init__(
        self, *, store: SqliteEvaluationStore, registry: FormalScenarioRegistry,
        trigger: ScenarioTriggerPort, events: FormalEventPublicReads,
    ) -> None:
        if type(store) is not SqliteEvaluationStore:
            raise TypeError("Formal injector requires SqliteEvaluationStore")
        registry.validate()
        if not callable(getattr(trigger, "trigger", None)):
            raise TypeError("existing Scenario Runtime trigger port is required")
        if not callable(getattr(events, "read_all_authoritative", None)):
            raise TypeError("authoritative EventStore read is required")
        self._store = store
        self._registry = registry
        self._trigger = trigger
        self._events = events

    def inject(self, execution: PlannedExecution) -> InjectionReceipt:
        if type(execution) is not PlannedExecution or execution.is_replacement:
            raise TypeError("Formal injector requires one unreplaced PlannedExecution")
        plan = self._registry.for_execution(execution.execution_id)
        if (execution.scenario_id != plan.scenario_id
                or execution.repetition != plan.repetition
                or execution.input_identity != plan.input_identity
                or execution.input_commitment != plan.input_commitment):
            raise ValueError("execution contradicts governed scenario input")
        _require_formal_execution_lineage(self._store, plan)
        signal = self._store.get(_record_id(
            execution.execution_id, "signal-started"))
        if signal is None:
            raise ValueError("durable signal_started_at must precede injection")
        signal_payload = json.loads(signal.semantic_json)
        if (signal.parent_id != execution.execution_id
                or signal_payload.get("kind") != "SIGNAL_STARTED"
                or not signal_payload.get("signal_started_at")
                or type(signal_payload.get("signal_started_ns")) is not int
                or signal_payload.get("polling_interval_ms")
                    != FORMAL_POLLING_INTERVAL_MS
                or signal_payload.get("cutoff_ms") != FORMAL_TIMEOUT_MS):
            raise ValueError("durable signal start boundary is contradictory")

        sent_id = _record_id(execution.execution_id, "formal-signal-sent")
        existing_sent = self._store.get(sent_id)
        if existing_sent is not None:
            return self._decode_receipt(existing_sent, plan)
        authorized_id = _record_id(
            execution.execution_id, "formal-injection-authorized")
        if self._store.get(authorized_id) is not None:
            raise AmbiguousInjectionState(
                "injection may have taken effect; blind replay is forbidden")

        baseline = self._event_ids(self._events.read_all_authoritative())
        operation = "injection:" + commitment({
            "execution_identity": execution.execution_id,
            "injector_identity": plan.injector_identity,
            "signal_record_id": signal.record_id,
        })[7:]
        authorization = LedgerRecord.create(
            "observation", [execution.execution_id,
                            "formal-injection-authorized"],
            {"kind": "FORMAL_INJECTION_AUTHORIZED",
             "execution_identity": execution.execution_id,
             "injector_identity": plan.injector_identity,
             "operation_identity": operation,
             "scenario_id": plan.scenario_id,
             "signal_record_id": signal.record_id,
             "signal_started_at": signal_payload["signal_started_at"],
             "event_baseline_ids": list(baseline),
             "event_baseline_commitment": commitment(list(baseline)),
             "event_store_volume_identity":
                plan.event_store_volume_identity,
             "event_isolation_commitment":
                plan.event_isolation_commitment},
            execution.execution_id)
        self._store.append(authorization)

        outcome = self._trigger.trigger(ScenarioId(plan.scenario_id))
        if (type(outcome) is not ScenarioTriggerResult or not outcome.accepted
                or outcome.snapshot.phase is not ScenarioPhase.INJECTING
                or outcome.snapshot.active_scenario is not ScenarioId(plan.scenario_id)
                or type(outcome.snapshot.trigger_count) is not int
                or outcome.snapshot.trigger_count < 1):
            raise ValueError("Scenario Runtime rejected or contradicted injection")
        sent = LedgerRecord.create(
            "observation", [execution.execution_id, "formal-signal-sent"],
            {"kind": "FORMAL_SIGNAL_SENT",
             "execution_identity": execution.execution_id,
             "injector_identity": plan.injector_identity,
             "operation_identity": operation,
             "scenario_id": plan.scenario_id,
             "trigger_count": outcome.snapshot.trigger_count,
             "event_baseline_ids": list(baseline),
             "event_baseline_commitment": commitment(list(baseline)),
             "event_isolation_commitment": plan.event_isolation_commitment,
             "event_binding_state": "PENDING_AUTHORITATIVE_EVENT"},
            execution.execution_id)
        persisted = self._store.append(sent)
        return self._decode_receipt(persisted, plan)

    @staticmethod
    def _event_ids(events: object) -> tuple[str, ...]:
        if not isinstance(events, list):
            raise ValueError("authoritative EventStore read returned invalid type")
        values = []
        for event in events:
            event_id = event.get("event_id") if isinstance(event, dict) else None
            if not isinstance(event_id, str) or not event_id.strip():
                raise ValueError("authoritative EventStore contains invalid Event")
            values.append(event_id)
        if len(values) != len(set(values)):
            raise ValueError("authoritative EventStore contains duplicate identities")
        return tuple(sorted(values))

    @staticmethod
    def _decode_receipt(
        record: LedgerRecord, plan: FormalScenarioInput,
    ) -> InjectionReceipt:
        payload = json.loads(record.semantic_json)
        baseline = payload.get("event_baseline_ids")
        if (record.parent_id != plan.execution_identity
                or payload.get("kind") != "FORMAL_SIGNAL_SENT"
                or payload.get("execution_identity") != plan.execution_identity
                or payload.get("injector_identity") != plan.injector_identity
                or payload.get("scenario_id") != plan.scenario_id
                or payload.get("event_isolation_commitment")
                    != plan.event_isolation_commitment
                or payload.get("event_binding_state")
                    != "PENDING_AUTHORITATIVE_EVENT"
                or not isinstance(baseline, list)
                or commitment(baseline)
                    != payload.get("event_baseline_commitment")):
            raise ValueError("durable signal-sent receipt is contradictory")
        return InjectionReceipt(
            plan.execution_identity, plan.injector_identity,
            payload["operation_identity"], plan.scenario_id,
            payload["trigger_count"], tuple(baseline),
            plan.event_isolation_commitment,
            payload["event_binding_state"], record.record_id)


class CandidateDTerminalFailureResolver:
    """Resolve terminal generation failure from existing D/A public reads."""

    def __init__(self, generation: GenerationRecoveryPublicReads,
                 rca: RcaLineagePublicReads) -> None:
        if not callable(getattr(generation, "recovery", None)):
            raise TypeError("Candidate-D public recovery read is required")
        if (not callable(getattr(rca, "get_aggregate_by_incident", None))
                or not callable(getattr(rca, "get_attempt_lineage", None))):
            raise TypeError("Candidate-A public lineage reads are required")
        self._generation = generation
        self._rca = rca

    def resolve(self, incident_id: str) -> UpstreamOutcome | None:
        aggregate = self._rca.get_aggregate_by_incident(incident_id)
        if aggregate is None:
            return None
        recovery = self._generation.recovery()
        status = getattr(recovery, "status", None)
        if status in {LocalReadStatus.UNAVAILABLE, LocalReadStatus.NOT_FOUND}:
            return None
        if status is not LocalReadStatus.FOUND:
            raise ValueError("Candidate-D public recovery state is invalid")
        snapshot = getattr(recovery, "value", None)
        failures = getattr(snapshot, "failures", None)
        if not isinstance(failures, tuple):
            raise ValueError("Candidate-D public failure projection is invalid")
        matches = []
        for failure in failures:
            lineage = self._rca.get_attempt_lineage(
                failure.try_identity.attempt_id)
            attempt = getattr(lineage, "attempt", None)
            outcomes = getattr(lineage, "try_outcomes", None)
            if (attempt is None or not isinstance(outcomes, tuple)
                    or attempt.lineage.aggregate_id != aggregate.aggregate_id):
                continue
            if (attempt.lifecycle is GenerationLifecycle.FAILED and outcomes
                    and outcomes[-1].result_kind is LogicalTryResultKind.FAILURE
                    and outcomes[-1].identity == failure.try_identity
                    and outcomes[-1].failure_code == failure.failure_class.value):
                matches.append(failure)
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("terminal Candidate-D failure lineage is ambiguous")
        failure = matches[0]
        failure_case = {
            FailureClass.PROVIDER_TIMEOUT: FailureCase.LLM_TIMEOUT_OR_FAILURE,
            FailureClass.PROVIDER_UNAVAILABLE: FailureCase.LLM_TIMEOUT_OR_FAILURE,
            FailureClass.PROVIDER_QUOTA: FailureCase.LLM_TIMEOUT_OR_FAILURE,
            FailureClass.INVOCATION_BOUND: FailureCase.RETRY_EXHAUSTION,
            FailureClass.MALFORMED_PROVIDER_OUTPUT:
                FailureCase.SCHEMA_OR_VALIDATION_FAILURE,
            FailureClass.STRUCTURED_PARSE:
                FailureCase.SCHEMA_OR_VALIDATION_FAILURE,
            FailureClass.RESULT_SCHEMA:
                FailureCase.SCHEMA_OR_VALIDATION_FAILURE,
        }.get(failure.failure_class, FailureCase.AUTHORITATIVE_NON_SUCCESS)
        return UpstreamOutcome(
            AuthoritativeOutcomeStatus.TERMINAL_FAILURE, failure_case,
            "CANDIDATE_D_PUBLIC_FAILURE", KnowledgeDisposition.MATCH)


class FormalPublicObserver:
    """One execution's EventStore-to-published-RCA public adapter."""

    def __init__(
        self, *, store: SqliteEvaluationStore, plan: FormalScenarioInput,
        events: FormalEventPublicReads, lineage: PublicObservationResolver,
        event_ownership: FormalEventOwnershipResolver,
        terminal_failures: CandidateDTerminalFailureResolver,
        clock: ObservationClock,
    ) -> None:
        if type(store) is not SqliteEvaluationStore:
            raise TypeError("Formal observer requires SqliteEvaluationStore")
        plan.validate()
        if type(lineage) is not PublicObservationResolver:
            raise TypeError("existing PublicObservationResolver is required")
        if type(terminal_failures) is not CandidateDTerminalFailureResolver:
            raise TypeError("typed Candidate-D failure resolver is required")
        if not callable(getattr(events, "read_all_authoritative", None)):
            raise TypeError("authoritative EventStore read is required")
        if not callable(getattr(event_ownership, "resolve", None)):
            raise TypeError("authoritative execution-to-Event resolver is required")
        for method in ("utc_now", "monotonic_ns", "wait_ms"):
            if not callable(getattr(clock, method, None)):
                raise TypeError("existing Candidate-F ObservationClock is required")
        self._store = store
        self._plan = plan
        self._events = events
        self._event_ownership = event_ownership
        self._lineage = lineage
        self._terminal_failures = terminal_failures
        self._clock = clock

    def observe(self, execution_id: str) -> ExternalObservation:
        if execution_id != self._plan.execution_identity:
            raise ValueError("observer execution lineage is contradictory")
        _require_formal_execution_lineage(self._store, self._plan)
        sent = self._store.get(_record_id(execution_id, "formal-signal-sent"))
        if sent is None:
            raise ValueError("observer requires durable signal-sent receipt")
        sent_payload = json.loads(sent.semantic_json)
        baseline = sent_payload.get("event_baseline_ids")
        operation_identity = sent_payload.get("operation_identity")
        if (sent_payload.get("injector_identity") != self._plan.injector_identity
                or sent_payload.get("scenario_id") != self._plan.scenario_id
                or not isinstance(operation_identity, str)
                or sent_payload.get("event_isolation_commitment")
                    != self._plan.event_isolation_commitment
                or not isinstance(baseline, list)
                or commitment(baseline)
                    != sent_payload.get("event_baseline_commitment")):
            raise ValueError("observer injection lineage is contradictory")
        observed_at = self._ensure_observation_started()
        events = self._events.read_all_authoritative()
        FormalScenarioInjector._event_ids(events)
        binding = self._event_ownership.resolve(operation_identity)
        if binding is None:
            raise ValueError(ObservationReasonCode.EVENT_BINDING_MISSING.value)
        if type(binding) is not AuthoritativeEventBinding:
            raise ValueError(ObservationReasonCode.EVENT_BINDING_CONFLICT.value)
        binding.validate()
        if (binding.execution_identity != execution_id
                or binding.operation_identity != operation_identity
                or binding.event_isolation_commitment
                    != self._plan.event_isolation_commitment
                or binding.event_identity in baseline):
            raise ValueError(ObservationReasonCode.EVENT_BINDING_CONFLICT.value)
        event_id = binding.event_identity
        matches = [item for item in events if item.get("event_id") == event_id]
        if len(matches) != 1:
            raise ValueError(ObservationReasonCode.EVENT_BINDING_CONFLICT.value)
        event = matches[0]
        if (commitment(event) != binding.event_content_commitment
                or event.get("event_type") != self._plan.production_event_type):
            raise ValueError(ObservationReasonCode.EVENT_BINDING_CONFLICT.value)
        self._persist_event_binding(binding)
        captured = CapturedInputBoundary(
            execution_id, event_id, binding.event_content_commitment)
        incident = self._lineage.resolve_incident(captured)
        if incident.outcome is ObservationOutcome.SYSTEM_FAILURE:
            raise OSError("Incident public read is unavailable")
        if incident.detail is ObservationDetail.MISSING:
            return ExternalObservation(
                observed_at, False, False, False, upstream=UpstreamOutcome(),
                event_id=event_id)
        if (incident.outcome is not ObservationOutcome.RESOLVED
                or incident.observation is None):
            raise ValueError("Incident public lineage is invalid or ambiguous")
        incident_endpoint = incident.observation
        incident_at, incident_ns = self._capture_endpoint(
            "incident", incident_endpoint.incident_id,
            incident_endpoint.membership_commitment)
        resolution = self._lineage.resolve(captured)
        if resolution.outcome is ObservationOutcome.SYSTEM_FAILURE:
            raise OSError("RCA public read is unavailable")
        if resolution.outcome is ObservationOutcome.RESOLVED:
            public = resolution.observation
            rca_at, rca_ns = self._capture_endpoint(
                "rca", public.current_version_id,
                public.lineage_commitment)
            return ExternalObservation(
                observed_at_utc=observed_at,
                event_has_unique_terminal_incident_owner=True,
                incident_durable_readable=True,
                incident_membership_coherent=True,
                artifact_durable=True, version_publication_successful=True,
                target_version_id=public.current_version_id,
                candidate_a_current_version_id=public.current_version_id,
                incident_rca_version_id=public.current_version_id,
                upstream=UpstreamOutcome(AuthoritativeOutcomeStatus.SUCCESS),
                event_id=event_id, incident_id=public.incident_id,
                current_version_id=public.current_version_id,
                public_lineage_commitment=public.lineage_commitment,
                incident_observed_at=incident_at,
                incident_observed_ns=incident_ns,
                rca_observed_at=rca_at, rca_observed_ns=rca_ns)
        if resolution.detail is not ObservationDetail.MISSING:
            raise ValueError("RCA public lineage is invalid or contradictory")
        failure = self._terminal_failures.resolve(incident_endpoint.incident_id)
        return ExternalObservation(
            observed_at_utc=observed_at,
            event_has_unique_terminal_incident_owner=True,
            incident_durable_readable=True,
            incident_membership_coherent=True,
            upstream=failure or UpstreamOutcome(),
            event_id=event_id, incident_id=incident_endpoint.incident_id,
            public_lineage_commitment=incident_endpoint.membership_commitment,
            incident_observed_at=incident_at,
            incident_observed_ns=incident_ns)

    def _persist_event_binding(
        self, binding: AuthoritativeEventBinding,
    ) -> LedgerRecord:
        record = LedgerRecord.create(
            "observation", [self._plan.execution_identity,
                            "formal-event-binding"],
            {"kind": "FORMAL_EVENT_BINDING",
             "execution_identity": binding.execution_identity,
             "operation_identity": binding.operation_identity,
             "event_identity": binding.event_identity,
             "event_content_commitment": binding.event_content_commitment,
             "event_isolation_commitment":
                binding.event_isolation_commitment},
            self._plan.execution_identity)
        return self._store.append(record)

    def _capture_endpoint(
        self, endpoint_kind: str, endpoint_identity: str,
        lineage_commitment: str,
    ) -> tuple[str, int]:
        if endpoint_kind not in {"incident", "rca"}:
            raise ValueError("unsupported Formal endpoint kind")
        label = f"formal-{endpoint_kind}-endpoint"
        record_id = _record_id(self._plan.execution_identity, label)
        existing = self._store.get(record_id)
        if existing is not None:
            payload = json.loads(existing.semantic_json)
            if (payload.get("kind") != f"FORMAL_{endpoint_kind.upper()}_ENDPOINT"
                    or payload.get("endpoint_identity") != endpoint_identity
                    or payload.get("lineage_commitment") != lineage_commitment
                    or not isinstance(payload.get("observed_at"), str)
                    or type(payload.get("observed_ns")) is not int):
                raise ValueError(
                    ObservationReasonCode.PUBLIC_STATE_CONTRADICTION.value)
            return payload["observed_at"], payload["observed_ns"]
        observed_at = self._clock.utc_now()
        observed_ns = self._clock.monotonic_ns()
        self._store.append(LedgerRecord.create(
            "observation", [self._plan.execution_identity, label],
            {"kind": f"FORMAL_{endpoint_kind.upper()}_ENDPOINT",
             "endpoint_identity": endpoint_identity,
             "lineage_commitment": lineage_commitment,
             "observed_at": observed_at, "observed_ns": observed_ns},
            self._plan.execution_identity))
        return observed_at, observed_ns

    def _ensure_observation_started(self) -> str:
        record_id = _record_id(
            self._plan.execution_identity, "formal-observation-started")
        existing = self._store.get(record_id)
        if existing is not None:
            payload = json.loads(existing.semantic_json)
            if (payload.get("kind") != "FORMAL_OBSERVATION_STARTED"
                    or payload.get("execution_identity")
                        != self._plan.execution_identity
                    or payload.get("injector_identity")
                        != self._plan.injector_identity
                    or payload.get("polling_interval_ms")
                        != FORMAL_POLLING_INTERVAL_MS
                    or payload.get("timeout_ms") != FORMAL_TIMEOUT_MS):
                raise ValueError("durable observation-start record is contradictory")
            return payload["observation_started_at"]
        now = self._clock.utc_now()
        self._store.append(LedgerRecord.create(
            "observation", [self._plan.execution_identity,
                            "formal-observation-started"],
            {"kind": "FORMAL_OBSERVATION_STARTED",
             "execution_identity": self._plan.execution_identity,
             "injector_identity": self._plan.injector_identity,
             "observation_started_at": now,
             "observation_started_ns": self._clock.monotonic_ns(),
             "polling_interval_ms": FORMAL_POLLING_INTERVAL_MS,
             "timeout_ms": FORMAL_TIMEOUT_MS},
            self._plan.execution_identity))
        return now


class FormalPublicObserverRouter:
    """Routes by exact execution identity; never guesses by service or time."""

    def __init__(self, registry: FormalScenarioRegistry,
                 observers: Mapping[str, FormalPublicObserver]) -> None:
        registry.validate()
        expected = {item.execution_identity for item in registry.entries}
        if set(observers) != expected or any(
                type(item) is not FormalPublicObserver
                for item in observers.values()):
            raise ValueError("observer routing must cover exact Formal executions")
        self._observers = dict(observers)

    def observe(self, execution_id: str) -> ExternalObservation:
        try:
            observer = self._observers[execution_id]
        except KeyError as exc:
            raise ValueError("unknown Formal observation execution") from exc
        return observer.observe(execution_id)


@dataclass(frozen=True, slots=True)
class FormalObservationFact:
    execution_identity: str
    scenario_id: str
    signal_started_at: str
    signal_started_ns: int
    observation_started_at: str
    observation_started_ns: int
    observation_ended_at: str
    observation_ended_ns: int
    event_identity: str | None
    incident_identity: str | None
    rca_version_identity: str | None
    public_lineage_commitment: str | None
    incident_observed_at: str | None
    rca_observed_at: str | None
    disposition: FormalObservationDisposition
    terminal_production_failure: bool
    incident_elapsed_ms: int | None
    rca_elapsed_ms: int | None
    source_provenance: str
    reason_code: ObservationReasonCode | None
    observation_commitment: str

    def material(self, *, include_commitment: bool = True) -> dict[str, object]:
        result = {
            name: (value.value if isinstance(value, Enum) else value)
            for name, value in asdict(self).items()
            if name != "observation_commitment"
        }
        if include_commitment:
            result["observation_commitment"] = self.observation_commitment
        return result

    @classmethod
    def _derive(
        cls, *, execution_identity: str, scenario_id: str,
        signal_started_at: str, signal_started_ns: int,
        observation_started_at: str, observation_started_ns: int,
        observation_ended_at: str, observation_ended_ns: int,
        endpoint: ExternalObservation | None,
        reason_code: ObservationReasonCode | None = None,
    ) -> "FormalObservationFact":
        require_identity(execution_identity, "execution")
        if scenario_id not in SCENARIOS:
            raise ValueError("observation scenario is unsupported")
        if (type(signal_started_ns) is not int
                or type(observation_started_ns) is not int
                or type(observation_ended_ns) is not int
                or observation_started_ns < signal_started_ns
                or observation_ended_ns < observation_started_ns):
            raise ValueError("observation monotonic boundary is negative or invalid")
        event_id = endpoint.event_id if endpoint is not None else None
        incident_id = endpoint.incident_id if endpoint is not None else None
        version_id = endpoint.current_version_id if endpoint is not None else None
        lineage = (endpoint.public_lineage_commitment
                   if endpoint is not None else None)
        actionable = endpoint is not None and endpoint.actionable_incident
        usable = endpoint is not None and endpoint.usable_rca
        terminal_failure = bool(
            endpoint is not None and endpoint.upstream.terminal_failure)
        incident_observed_at = (
            endpoint.incident_observed_at if endpoint is not None else None)
        incident_observed_ns = (
            endpoint.incident_observed_ns if endpoint is not None else None)
        rca_observed_at = (
            endpoint.rca_observed_at if endpoint is not None else None)
        rca_observed_ns = (
            endpoint.rca_observed_ns if endpoint is not None else None)
        if (actionable != (isinstance(incident_observed_at, str)
                           and type(incident_observed_ns) is int)
                or usable != (isinstance(rca_observed_at, str)
                              and type(rca_observed_ns) is int)):
            raise ValueError("latency endpoint timestamps must match public endpoints")
        if ((incident_observed_ns is not None
             and (incident_observed_ns < signal_started_ns
                  or incident_observed_ns > observation_ended_ns))
                or (rca_observed_ns is not None
                    and (rca_observed_ns < signal_started_ns
                         or rca_observed_ns > observation_ended_ns))):
            raise ValueError(
                "latency endpoint must fall inside the durable observation window")
        incident_elapsed = ((incident_observed_ns - signal_started_ns) // 1_000_000
                            if incident_observed_ns is not None else None)
        rca_elapsed = ((rca_observed_ns - signal_started_ns) // 1_000_000
                       if rca_observed_ns is not None else None)
        invalid_reasons = {
            ObservationReasonCode.EVENT_BINDING_MISSING,
            ObservationReasonCode.EVENT_BINDING_CONFLICT,
            ObservationReasonCode.INCIDENT_LINEAGE_AMBIGUOUS,
            ObservationReasonCode.RCA_LINEAGE_AMBIGUOUS,
            ObservationReasonCode.PUBLIC_STATE_CONTRADICTION,
            ObservationReasonCode.READ_INTEGRITY_FAILURE,
        }
        elapsed_ms = (observation_ended_ns - signal_started_ns) // 1_000_000
        if terminal_failure:
            if reason_code not in {
                    None, ObservationReasonCode.TERMINAL_PRODUCTION_FAILURE}:
                raise ValueError("terminal failure reason is contradictory")
            disposition = FormalObservationDisposition.FAILED
            reason_code = ObservationReasonCode.TERMINAL_PRODUCTION_FAILURE
        elif reason_code in invalid_reasons:
            if usable:
                raise ValueError("usable endpoint cannot be marked INVALID")
            disposition = FormalObservationDisposition.INVALID
        elif usable:
            if reason_code is not None or rca_elapsed is None:
                raise ValueError("observed endpoint reason is contradictory")
            if rca_elapsed > FORMAL_TIMEOUT_MS:
                raise ValueError("usable RCA was first observed after the cutoff")
            disposition = FormalObservationDisposition.OBSERVED
        elif elapsed_ms >= FORMAL_TIMEOUT_MS:
            if reason_code not in {None, ObservationReasonCode.TIMEOUT_REACHED}:
                raise ValueError("timeout reason is contradictory")
            disposition = FormalObservationDisposition.CENSORED
            reason_code = ObservationReasonCode.TIMEOUT_REACHED
        else:
            raise ValueError("non-terminal observation cannot be materialized")
        values = dict(
            execution_identity=execution_identity, scenario_id=scenario_id,
            signal_started_at=signal_started_at,
            signal_started_ns=signal_started_ns,
            observation_started_at=observation_started_at,
            observation_started_ns=observation_started_ns,
            observation_ended_at=observation_ended_at,
            observation_ended_ns=observation_ended_ns,
            event_identity=event_id, incident_identity=incident_id,
            rca_version_identity=version_id,
            public_lineage_commitment=lineage,
            incident_observed_at=incident_observed_at,
            rca_observed_at=rca_observed_at, disposition=disposition,
            terminal_production_failure=terminal_failure,
            incident_elapsed_ms=incident_elapsed, rca_elapsed_ms=rca_elapsed,
            source_provenance=FORMAL_OBSERVER_CONTRACT,
            reason_code=reason_code)
        provisional = cls(
            **values, observation_commitment="sha256:" + "0" * 64)
        result = cls(
            **values, observation_commitment=commitment(
                provisional.material(include_commitment=False)))
        if _contains_ground_truth(result.material()):
            raise ValueError("production observation contains Ground Truth")
        return result


class FormalObservationCommand:
    """Authoritative terminal materialization; owns clock and disposition."""

    def __init__(
        self, *, store: SqliteEvaluationStore, plan: FormalScenarioInput,
        clock: ObservationClock,
    ) -> None:
        if type(store) is not SqliteEvaluationStore:
            raise TypeError("Formal observation command requires Candidate-F store")
        plan.validate()
        for method in ("utc_now", "monotonic_ns"):
            if not callable(getattr(clock, method, None)):
                raise TypeError("Candidate-F observation clock is required")
        self._store = store
        self._plan = plan
        self._clock = clock

    def finalize(
        self, endpoint: ExternalObservation | None = None, *,
        reason_code: ObservationReasonCode | None = None,
    ) -> LedgerRecord:
        _require_formal_execution_lineage(self._store, self._plan)
        signal = self._store.get(_record_id(
            self._plan.execution_identity, "signal-started"))
        started = self._store.get(_record_id(
            self._plan.execution_identity, "formal-observation-started"))
        if signal is None or started is None:
            raise ValueError("Formal observation boundary is not durable")
        signal_payload = json.loads(signal.semantic_json)
        started_payload = json.loads(started.semantic_json)
        terminal_id = _record_id(
            self._plan.execution_identity,
            "formal-public-observation-terminal")
        existing = self._store.get(terminal_id)
        if existing is not None:
            payload = json.loads(existing.semantic_json)
            requested_disposition = (
                FormalObservationDisposition.FAILED
                if endpoint is not None and endpoint.upstream.terminal_failure
                else FormalObservationDisposition.INVALID
                if reason_code in {
                    ObservationReasonCode.EVENT_BINDING_MISSING,
                    ObservationReasonCode.EVENT_BINDING_CONFLICT,
                    ObservationReasonCode.INCIDENT_LINEAGE_AMBIGUOUS,
                    ObservationReasonCode.RCA_LINEAGE_AMBIGUOUS,
                    ObservationReasonCode.PUBLIC_STATE_CONTRADICTION,
                    ObservationReasonCode.READ_INTEGRITY_FAILURE,
                }
                else FormalObservationDisposition.OBSERVED
                if endpoint is not None and endpoint.usable_rca
                else FormalObservationDisposition.CENSORED)
            if ((requested_disposition is FormalObservationDisposition.FAILED
                 and reason_code not in {
                    None, ObservationReasonCode.TERMINAL_PRODUCTION_FAILURE})
                    or (requested_disposition
                        is FormalObservationDisposition.OBSERVED
                        and reason_code is not None)
                    or (requested_disposition
                        is FormalObservationDisposition.CENSORED
                        and reason_code not in {
                            None, ObservationReasonCode.TIMEOUT_REACHED})):
                raise ValueError("contradictory terminal observation replay")
            if (payload.get("kind") != "FORMAL_PUBLIC_OBSERVATION"
                    or payload.get("execution_identity")
                        != self._plan.execution_identity
                    or payload.get("disposition")
                        != requested_disposition.value
                    or payload.get("event_identity")
                        != (endpoint.event_id if endpoint else None)
                    or payload.get("incident_identity")
                        != (endpoint.incident_id if endpoint else None)
                    or payload.get("rca_version_identity")
                        != (endpoint.current_version_id if endpoint else None)
                    or (requested_disposition
                        is FormalObservationDisposition.INVALID
                        and payload.get("reason_code") != reason_code.value)):
                raise ValueError("contradictory terminal observation replay")
            return existing
        fact = FormalObservationFact._derive(
            execution_identity=self._plan.execution_identity,
            scenario_id=self._plan.scenario_id,
            signal_started_at=signal_payload["signal_started_at"],
            signal_started_ns=signal_payload["signal_started_ns"],
            observation_started_at=started_payload["observation_started_at"],
            observation_started_ns=started_payload["observation_started_ns"],
            observation_ended_at=self._clock.utc_now(),
            observation_ended_ns=self._clock.monotonic_ns(),
            endpoint=endpoint, reason_code=reason_code)
        return record_formal_observation(self._store, fact)


def record_formal_observation(
    store: SqliteEvaluationStore, fact: FormalObservationFact,
) -> LedgerRecord:
    """Append one idempotent terminal projection to the sole Candidate-F store."""
    if type(store) is not SqliteEvaluationStore:
        raise TypeError("formal observation requires SqliteEvaluationStore")
    if type(fact) is not FormalObservationFact:
        raise TypeError("typed FormalObservationFact is required")
    invalid_reasons = {
        ObservationReasonCode.EVENT_BINDING_MISSING,
        ObservationReasonCode.EVENT_BINDING_CONFLICT,
        ObservationReasonCode.INCIDENT_LINEAGE_AMBIGUOUS,
        ObservationReasonCode.RCA_LINEAGE_AMBIGUOUS,
        ObservationReasonCode.PUBLIC_STATE_CONTRADICTION,
        ObservationReasonCode.READ_INTEGRITY_FAILURE,
    }
    elapsed_ms = (
        fact.observation_ended_ns - fact.signal_started_ns) // 1_000_000
    disposition_valid = (
        (fact.disposition is FormalObservationDisposition.OBSERVED
         and fact.rca_version_identity is not None
         and fact.rca_observed_at is not None
         and fact.rca_elapsed_ms is not None
         and fact.rca_elapsed_ms <= FORMAL_TIMEOUT_MS
         and not fact.terminal_production_failure
         and fact.reason_code is None)
        or (fact.disposition is FormalObservationDisposition.FAILED
            and fact.terminal_production_failure
            and fact.reason_code
                is ObservationReasonCode.TERMINAL_PRODUCTION_FAILURE)
        or (fact.disposition is FormalObservationDisposition.CENSORED
            and not fact.terminal_production_failure
            and fact.rca_version_identity is None
            and fact.rca_elapsed_ms is None
            and elapsed_ms >= FORMAL_TIMEOUT_MS
            and fact.reason_code is ObservationReasonCode.TIMEOUT_REACHED)
        or (fact.disposition is FormalObservationDisposition.INVALID
            and not fact.terminal_production_failure
            and fact.reason_code in invalid_reasons)
    )
    scalar_schema_valid = (
        type(fact.terminal_production_failure) is bool
        and isinstance(fact.disposition, FormalObservationDisposition)
        and (fact.reason_code is None
             or isinstance(fact.reason_code, ObservationReasonCode))
        and all(isinstance(value, str) and value.strip()
                for value in (
                    fact.signal_started_at, fact.observation_started_at,
                    fact.observation_ended_at))
        and all(value is None or (
                    isinstance(value, str) and value.strip())
                for value in (
                    fact.event_identity, fact.incident_identity,
                    fact.rca_version_identity, fact.incident_observed_at,
                    fact.rca_observed_at))
        and (fact.public_lineage_commitment is None
             or (isinstance(fact.public_lineage_commitment, str)
                 and fact.public_lineage_commitment.startswith("sha256:")))
    )
    if (fact.source_provenance != FORMAL_OBSERVER_CONTRACT
            or not scalar_schema_valid
            or not disposition_valid
            or commitment(fact.material(include_commitment=False))
                != fact.observation_commitment
            or _contains_ground_truth(fact.material())):
        raise ValueError("Formal observation commitment is contradictory")
    execution = store.get(fact.execution_identity)
    signal = store.get(_record_id(fact.execution_identity, "signal-started"))
    started = store.get(_record_id(
        fact.execution_identity, "formal-observation-started"))
    if (execution is None or execution.domain != "execution" or signal is None
            or started is None):
        raise ValueError("Formal observation lacks durable execution/signal lineage")
    store.require_formal_run(execution.parent_id)
    signal_payload = json.loads(signal.semantic_json)
    started_payload = json.loads(started.semantic_json)
    execution_payload = json.loads(execution.semantic_json)
    if (signal_payload.get("signal_started_at") != fact.signal_started_at
            or signal_payload.get("signal_started_ns") != fact.signal_started_ns
            or execution_payload.get("scenario_id") != fact.scenario_id
            or started_payload.get("kind") != "FORMAL_OBSERVATION_STARTED"
            or started_payload.get("observation_started_at")
                != fact.observation_started_at
            or started_payload.get("observation_started_ns")
                != fact.observation_started_ns
            or started_payload.get("polling_interval_ms")
                != FORMAL_POLLING_INTERVAL_MS
            or started_payload.get("timeout_ms") != FORMAL_TIMEOUT_MS):
        raise ValueError("Formal observation signal boundary is contradictory")
    binding = store.get(_record_id(
        fact.execution_identity, "formal-event-binding"))
    if fact.event_identity is not None:
        if binding is None:
            raise ValueError("Formal observation lacks authoritative Event binding")
        binding_payload = json.loads(binding.semantic_json)
        if (binding_payload.get("kind") != "FORMAL_EVENT_BINDING"
                or binding_payload.get("event_identity")
                    != fact.event_identity):
            raise ValueError("Formal observation Event binding is contradictory")
    for kind, endpoint_identity, observed_at, elapsed in (
        ("incident", fact.incident_identity, fact.incident_observed_at,
         fact.incident_elapsed_ms),
        ("rca", fact.rca_version_identity, fact.rca_observed_at,
         fact.rca_elapsed_ms),
    ):
        endpoint_record = store.get(_record_id(
            fact.execution_identity, f"formal-{kind}-endpoint"))
        if endpoint_identity is None:
            if observed_at is not None or elapsed is not None:
                raise ValueError("Formal endpoint timing is fabricated")
            continue
        if endpoint_record is None:
            raise ValueError("Formal endpoint timing lacks observer authority")
        endpoint_payload = json.loads(endpoint_record.semantic_json)
        expected_lineage = (
            fact.public_lineage_commitment
            if kind == "rca" or fact.rca_version_identity is None else None)
        if (endpoint_payload.get("endpoint_identity") != endpoint_identity
                or endpoint_payload.get("observed_at") != observed_at
                or type(endpoint_payload.get("observed_ns")) is not int
                or (endpoint_payload["observed_ns"] - fact.signal_started_ns)
                    // 1_000_000 != elapsed
                or (expected_lineage is not None
                    and endpoint_payload.get("lineage_commitment")
                        != expected_lineage)):
            raise ValueError("Formal endpoint timing is contradictory")
    record = LedgerRecord.create(
        "observation", [fact.execution_identity,
                        "formal-public-observation-terminal"],
        {"kind": "FORMAL_PUBLIC_OBSERVATION", **fact.material()},
        fact.execution_identity)
    return store.append(record)
