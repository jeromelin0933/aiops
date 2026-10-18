from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rca_evaluation.authority import load_trusted_oracle_approvals
from llm_generation.contracts import (
    FailureClass, GenerationFailure, LocalReadStatus,
)
from rca_persistence.contracts import (
    AdmittedRetryDisposition, GenerationLifecycle, LogicalTryIdentity,
    LogicalTryResultKind,
)
from scenario_runtime.schema import (
    ScenarioId, ScenarioPhase, ScenarioRuntimeSnapshot, ScenarioTriggerResult,
)

from rca_evaluation.campaign import PlannedExecution
from rca_evaluation.formal_scenario_io import (
    FORMAL_OBSERVER_CONTRACT, FORMAL_POLLING_INTERVAL_MS,
    FORMAL_TIMEOUT_MS, ActualExecutionState, AmbiguousInjectionState,
    AuthoritativeEventBinding,
    CandidateDTerminalFailureResolver,
    FormalObservationCommand, FormalObservationFact,
    FormalObservationPollingContract,
    FormalPublicObserver, FormalScenarioInjector,
    InjectionLifecycleState, injection_lifecycle_state,
    ObservationReasonCode,
    project_r2_controlled_injection_sources,
    resolve_formal_scenario_registry,
)
from rca_evaluation.formal_fixture_sources import (
    load_formal_fixture_source_artifact,
)
from rca_evaluation.identity import commitment, identity
from rca_evaluation.ledger import LedgerRecord
from rca_evaluation.observation import PublicObservationResolver
from rca_evaluation.oracle import load_official_oracle
from rca_evaluation.preparation import prepare_formal_campaign
from rca_evaluation.runner import CampaignRunner
from rca_evaluation.sqlite_store import SqliteEvaluationStore
from tests.test_spec_017_r1_formal_admission import _admit
from tests.test_spec_017_s2_observation_isolation import full_environment


ROOT = Path(__file__).resolve().parents[1]
TRUST = ROOT / "configs/rca_evaluation/trust/spec017_oracle_approval_v1.json"
R2_SOURCE = ROOT / "configs/rca_evaluation/fixtures/spec017_r2_missing_formal_fixtures_v1.json"
GROUND_TRUTH_TERMS = (
    "oracle", "ground_truth", "expected_root_cause", "expected_rca",
    "scoring_predicate", "review_verdict", "reviewer_answer",
)


class Clock:
    def __init__(self):
        self.ns = 10_000_000_000
        self.waits = []

    def monotonic_ns(self):
        return self.ns

    def utc_now(self):
        return "2026-10-16T00:00:10Z"

    def wait_ms(self, milliseconds):
        self.waits.append(milliseconds)
        self.ns += milliseconds * 1_000_000


class Events:
    def __init__(self, values=()):
        self.values = list(values)

    @property
    def connection(self):
        raise AssertionError("private EventStore connection is forbidden")

    def read_all_authoritative(self):
        return list(self.values)


class Ownership:
    def __init__(self, plan, events, *, event_id="EVT-1", missing=False,
                 isolation_commitment=None):
        self.plan = plan
        self.events = events
        self.event_id = event_id
        self.missing = missing
        self.isolation_commitment = (
            isolation_commitment or plan.event_isolation_commitment)

    def resolve(self, operation_identity):
        if self.missing:
            return None
        matches = [item for item in self.events.values
                   if item.get("event_id") == self.event_id]
        event_commitment = (commitment(matches[0]) if len(matches) == 1
                            else commitment({"missing": self.event_id}))
        return AuthoritativeEventBinding(
            self.plan.execution_identity, operation_identity, self.event_id,
            event_commitment, self.isolation_commitment)


class Trigger:
    def __init__(self, store=None, execution_id=None, *, fail=False):
        self.calls = []
        self.store = store
        self.execution_id = execution_id
        self.fail = fail

    def trigger(self, scenario_id, *, requested_at=None):
        self.calls.append(scenario_id)
        if self.store is not None:
            record = self.store.get(LedgerRecord.create(
                "observation", [self.execution_id,
                                "formal-injection-authorized"], {},
                self.execution_id).record_id)
            assert record is not None
        if self.fail:
            raise RuntimeError("synthetic crash after authorization")
        return ScenarioTriggerResult(
            True, "accepted", ScenarioRuntimeSnapshot(
                ScenarioPhase.INJECTING, ScenarioId(scenario_id), 1.0, 2.0,
                len(self.calls), False))


class NoFailureReads:
    def recovery(self):
        return SimpleNamespace(status=LocalReadStatus.UNAVAILABLE, value=None)


class Unused:
    def __getattr__(self, name):
        raise AssertionError(f"test setup must not call {name}")


def _prepared_registry(tmp_path):
    report = _admit(tmp_path)
    prepared = prepare_formal_campaign(
        repository_root=ROOT,
        preregistration_path=tmp_path / "formal.json",
        campaign_key=report.campaign.campaign_key,
        database_path=report.database_path,
        evaluation_root=report.evaluation_root,
        repository_state=SimpleNamespace(
            branch="feature/spec-017-formal-evaluation-readiness",
            commit="e9cee50ce7904fb822e8032bbe8b05a4a826cf07",
            dirty=False))
    store = SqliteEvaluationStore(
        report.database_path, approvals=load_trusted_oracle_approvals(TRUST))
    admission_payload = report.admission_payload()
    store.admit_preparation(prepared.preparation_id, {
        "schema": "spec017.preparation-admission.v1",
        "lifecycle_state": "ADMITTED",
        "preparation_id": prepared.preparation_id,
        "preparation_commitment": prepared.preparation_commitment,
        "formal_admission_commitment": report.admission_commitment,
        "formal_admission_payload": admission_payload,
    })
    store.admit_oracle(report.oracle_revision)
    store.admit_plan(report.plan)
    runner = CampaignRunner(
        ledger=store, clock=Unused(), observer=Unused(), injector=Unused(),
        grader=lambda _: True)
    store._append_formal_run(runner._create_run_record(
        admitted_plan=report.plan, campaign=report.campaign,
        manifest=report.manifest, execution_mode="FORMAL",
        formal_admission_commitment=report.admission_commitment,
        formal_admission_payload=admission_payload))
    registry = resolve_formal_scenario_registry(
        store, prepared.preparation_id, report.campaign, ROOT)
    return report, prepared, store, registry


def _admit_execution(store, report, execution):
    store.append(LedgerRecord.create(
        "execution", [report.campaign.run_id, execution.scenario_id,
                      execution.repetition],
        {"scenario_id": execution.scenario_id,
         "repetition": execution.repetition,
         "input_identity": execution.input_identity,
         "input_commitment": execution.input_commitment},
        report.campaign.run_id))


def _signal(store, execution, clock):
    return store.append(LedgerRecord.create(
        "observation", [execution.execution_id, "signal-started"],
        {"kind": "SIGNAL_STARTED",
         "signal_started_at": "2026-10-16T00:00:00Z",
         "signal_started_ns": clock.monotonic_ns(),
         "polling_interval_ms": FORMAL_POLLING_INTERVAL_MS,
         "cutoff_ms": FORMAL_TIMEOUT_MS}, execution.execution_id))


def _injected(tmp_path):
    report, prepared, store, registry = _prepared_registry(tmp_path)
    execution = report.campaign.executions[0]
    _admit_execution(store, report, execution)
    clock = Clock()
    _signal(store, execution, clock)
    events = Events()
    trigger = Trigger(store, execution.execution_id)
    injector = FormalScenarioInjector(
        store=store, registry=registry, trigger=trigger, events=events)
    receipt = injector.inject(execution)
    return report, prepared, store, registry, execution, clock, events, trigger, receipt


def _observer(store, registry, execution, clock, events, *, environment=None,
              ownership=None):
    incidents, rca, _, _, lineage = full_environment()
    if environment == "incident-only":
        rca.current = None
        rca.get_current = lambda _: None
        incidents.relationship = SimpleNamespace(
            incident_id="INC-1", current_version_id=None)
    elif environment == "event-only":
        incidents.owner = False
        incidents.records = {status: () for status in IncidentStatus}
    failures = CandidateDTerminalFailureResolver(NoFailureReads(), rca)
    plan = registry.for_execution(execution.execution_id)
    return (FormalPublicObserver(
        store=store, plan=plan,
        events=events, lineage=lineage, terminal_failures=failures,
        event_ownership=ownership or Ownership(plan, events),
        clock=clock), incidents, rca)


# Imported late to keep the test's production imports visually separate.
from incident_management.contracts import IncidentStatus  # noqa: E402


def test_registry_has_exact_s1_s6_repetition_major_governed_inputs(tmp_path):
    report, _, store, registry = _prepared_registry(tmp_path)
    try:
        assert len(registry.entries) == 30
        assert [(item.scenario_id, item.repetition) for item in registry.entries] == [
            (f"S{scenario}", repetition)
            for repetition in range(1, 6) for scenario in range(1, 7)]
        assert {item.scenario_id for item in registry.entries} == {
            "S1", "S2", "S3", "S4", "S5", "S6"}
        assert all(item.official_source_reference ==
                   f"configs/scenarios.yaml#scenarios.{item.scenario_id}"
                   for item in registry.entries)
        assert all(item.actual_injection is ActualExecutionState.NOT_EXECUTED
                   for item in registry.entries)
        assert all(not execution.is_replacement
                   for execution in report.campaign.executions)
        with pytest.raises(ValueError, match="does not authorize replacement"):
            report.campaign.replacement_for(
                report.campaign.executions[0].execution_id)
    finally:
        store.close()


def test_registry_replay_is_stable_and_slots_have_distinct_lineage(tmp_path):
    report, prepared, store, first = _prepared_registry(tmp_path)
    try:
        second = resolve_formal_scenario_registry(
            store, prepared.preparation_id, report.campaign, ROOT)
        assert second == first
        assert first.entries[0].injector_identity == second.entries[0].injector_identity
        assert first.entries[0].planned_execution_identity != (
            first.entries[1].planned_execution_identity)
        assert first.entries[0].execution_identity != first.entries[1].execution_identity
    finally:
        store.close()


def test_unsupported_scenario_and_wrong_fixture_commitment_fail_closed(tmp_path):
    report, prepared, store, _ = _prepared_registry(tmp_path)
    try:
        first = report.campaign.executions[0]
        changed = replace(
            first, input_commitment="sha256:" + "f" * 64)
        campaign = replace(
            report.campaign,
            executions=(changed, *report.campaign.executions[1:]))
        with pytest.raises(ValueError, match="fixture commitment drifted"):
            resolve_formal_scenario_registry(
                store, prepared.preparation_id, campaign, ROOT)
        with pytest.raises(ValueError):
            ScenarioId("S7")
    finally:
        store.close()


def test_registry_and_r2_controlled_sources_contain_no_ground_truth(tmp_path):
    report, _, store, registry = _prepared_registry(tmp_path)
    try:
        raw = json.dumps(registry.material(), sort_keys=True).lower()
        assert not any(term in raw for term in GROUND_TRUTH_TERMS)
        oracle = load_official_oracle(
            ROOT / "configs/rca_evaluation/oracle/spec017_s1_s6_v1.json")
        artifact = load_formal_fixture_source_artifact(R2_SOURCE, oracle=oracle)
        controls = project_r2_controlled_injection_sources(artifact)
        assert {item.fixture_identity for item in controls} == {
            "spec017:S1:degraded:v1", "spec017:S2:failure:v1",
            "spec017:S6:contradiction:v1"}
        serialized = json.dumps([item.production_input for item in controls]).lower()
        assert not any(term in serialized for term in GROUND_TRUTH_TERMS)
    finally:
        store.close()


def test_signal_start_is_required_and_durable_authorization_precedes_effect(tmp_path):
    report, _, store, registry = _prepared_registry(tmp_path)
    execution = report.campaign.executions[0]
    try:
        _admit_execution(store, report, execution)
        events = Events()
        trigger = Trigger(store, execution.execution_id)
        injector = FormalScenarioInjector(
            store=store, registry=registry, trigger=trigger, events=events)
        with pytest.raises(ValueError, match="signal_started_at"):
            injector.inject(execution)
        assert trigger.calls == []
        clock = Clock()
        _signal(store, execution, clock)
        receipt = injector.inject(execution)
        assert trigger.calls == [ScenarioId.S1]
        assert receipt.scenario_id == "S1"
        assert injection_lifecycle_state(
            store, execution.execution_id) is InjectionLifecycleState.SIGNAL_SENT
    finally:
        store.close()


def test_equivalent_replay_and_post_authorization_ambiguity_guard(tmp_path):
    report, _, store, registry, execution, clock, events, trigger, first = _injected(tmp_path)
    try:
        injector = FormalScenarioInjector(
            store=store, registry=registry, trigger=trigger, events=events)
        second = injector.inject(execution)
        assert second == first
        assert trigger.calls == [ScenarioId.S1]
    finally:
        store.close()

    report, _, store, registry = _prepared_registry(tmp_path / "ambiguous")
    execution = report.campaign.executions[0]
    try:
        _admit_execution(store, report, execution)
        _signal(store, execution, Clock())
        crashing = FormalScenarioInjector(
            store=store, registry=registry,
            trigger=Trigger(fail=True), events=Events())
        with pytest.raises(RuntimeError, match="synthetic crash"):
            crashing.inject(execution)
        assert injection_lifecycle_state(
            store, execution.execution_id
        ) is InjectionLifecycleState.INJECTION_AUTHORIZED
        with pytest.raises(AmbiguousInjectionState, match="blind replay"):
            crashing.inject(execution)
    finally:
        store.close()


def test_injector_has_no_incident_rca_or_candidate_f_result_capability():
    source = inspect.getsource(FormalScenarioInjector)
    assert ".trigger(" in source
    assert "create_incident" not in source
    assert "write_incident" not in source
    assert "publish" not in source
    assert "RCA" not in source
    assert "formal-public-observation-terminal" not in source


def test_event_alone_is_not_incident_and_unpublished_rca_is_not_usable(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        observer, _, _ = _observer(
            store, registry, execution, clock, events, environment="event-only")
        event_only = observer.observe(execution.execution_id)
        assert event_only.event_id == "EVT-1"
        assert not event_only.actionable_incident
        assert not event_only.usable_rca

        observer, _, _ = _observer(
            store, registry, execution, clock, events,
            environment="incident-only")
        incident_only = observer.observe(execution.execution_id)
        assert incident_only.actionable_incident
        assert incident_only.incident_id == "INC-1"
        assert not incident_only.usable_rca
        assert incident_only.current_version_id is None
    finally:
        store.close()


def test_public_published_rca_endpoint_is_detected_with_exact_lineage(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        observer, _, _ = _observer(store, registry, execution, clock, events)
        observed = observer.observe(execution.execution_id)
        assert observed.actionable_incident
        assert observed.usable_rca
        assert observed.event_id == "EVT-1"
        assert observed.incident_id == "INC-1"
        assert observed.current_version_id == "VER-1"
        assert observed.public_lineage_commitment.startswith("sha256:")
        assert injection_lifecycle_state(
            store, execution.execution_id) is InjectionLifecycleState.OBSERVING
    finally:
        store.close()


def test_ambiguous_event_or_incident_lineage_fails_closed(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [
            {"event_id": "EVT-1", "event_type": "brute_force_detected"},
            {"event_id": "EVT-2", "event_type": "brute_force_detected"},
        ]
        observer, _, _ = _observer(store, registry, execution, clock, events)
        exact = observer.observe(execution.execution_id)
        assert exact.event_id == "EVT-1"
        assert exact.usable_rca

        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        observer, incidents, _ = _observer(
            store, registry, execution, clock, events)
        incidents.records[IncidentStatus.ASSIGNED] = (
            SimpleNamespace(incident_id="INC-2", event_ids=("EVT-1",)),)
        with pytest.raises(ValueError, match="invalid or ambiguous"):
            observer.observe(execution.execution_id)
    finally:
        store.close()


def test_foreign_singleton_even_with_valid_public_lineage_is_rejected(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        # EVT-1 has a complete valid Incident/RCA public lineage in the test
        # environment, but the injection authority binds another Event.
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        plan = registry.for_execution(execution.execution_id)
        observer, _, _ = _observer(
            store, registry, execution, clock, events,
            ownership=Ownership(plan, events, event_id="EVT-OWNED"))
        with pytest.raises(ValueError, match="EVENT_BINDING_CONFLICT"):
            observer.observe(execution.execution_id)
        assert store.get(LedgerRecord.create(
            "observation", [execution.execution_id, "formal-event-binding"],
            {}, execution.execution_id).record_id) is None
    finally:
        store.close()


def test_missing_or_conflicting_event_ownership_fails_closed(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        plan = registry.for_execution(execution.execution_id)
        missing, _, _ = _observer(
            store, registry, execution, clock, events,
            ownership=Ownership(plan, events, missing=True))
        with pytest.raises(ValueError, match="EVENT_BINDING_MISSING"):
            missing.observe(execution.execution_id)
        conflicting, _, _ = _observer(
            store, registry, execution, clock, events,
            ownership=Ownership(
                plan, events, isolation_commitment="sha256:" + "f" * 64))
        with pytest.raises(ValueError, match="EVENT_BINDING_CONFLICT"):
            conflicting.observe(execution.execution_id)
    finally:
        store.close()


def test_event_binding_replay_is_exact_and_outside_event_schema(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        event = {"event_id": "EVT-1", "event_type": "brute_force_detected"}
        events.values = [event]
        observer, _, _ = _observer(store, registry, execution, clock, events)
        assert observer.observe(execution.execution_id).event_id == "EVT-1"
        first_payload, first = _payload(
            store, execution.execution_id, "formal-event-binding")
        assert observer.observe(execution.execution_id).event_id == "EVT-1"
        second_payload, second = _payload(
            store, execution.execution_id, "formal-event-binding")
        assert second == first
        assert second_payload == first_payload
        assert set(event) == {"event_id", "event_type"}
        assert "execution_identity" not in event
    finally:
        store.close()


def test_raw_generation_result_without_publication_never_counts_as_rca(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        observer, _, rca = _observer(
            store, registry, execution, clock, events,
            environment="incident-only")
        # full_environment still has a validated Candidate-D result, but Current
        # publication was removed above.
        assert rca.get_current("AGG-1") is None
        observed = observer.observe(execution.execution_id)
        assert observed.actionable_incident
        assert not observed.usable_rca
        assert observed.current_version_id is None
    finally:
        store.close()


def test_typed_candidate_d_public_failure_maps_to_failed(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        incidents, rca, _, _, lineage = full_environment()
        rca.current = None
        rca.get_current = lambda _: None
        incidents.relationship = SimpleNamespace(
            incident_id="INC-1", current_version_id=None)
        try_id = LogicalTryIdentity("ATT-1", 1)
        failure = GenerationFailure(
            try_id, "operation-1", FailureClass.PROVIDER_TIMEOUT,
            AdmittedRetryDisposition.NON_RETRYABLE, "provider timeout")
        rca.lineage_read.attempt = SimpleNamespace(
            lineage=rca.lineage_read.attempt.lineage,
            lifecycle=GenerationLifecycle.FAILED)
        rca.lineage_read.try_outcomes = (SimpleNamespace(
            identity=try_id, result_kind=LogicalTryResultKind.FAILURE,
            failure_code=FailureClass.PROVIDER_TIMEOUT.value),)
        generation = SimpleNamespace(recovery=lambda: SimpleNamespace(
            status=LocalReadStatus.FOUND,
            value=SimpleNamespace(failures=(failure,))))
        observer = FormalPublicObserver(
            store=store, plan=registry.for_execution(execution.execution_id),
            events=events, lineage=lineage,
            event_ownership=Ownership(
                registry.for_execution(execution.execution_id), events),
            terminal_failures=CandidateDTerminalFailureResolver(generation, rca),
            clock=clock)
        observed = observer.observe(execution.execution_id)
        assert observed.upstream.terminal_failure
        assert observed.upstream.failure_case.value == "LLM_TIMEOUT_OR_FAILURE"
        assert not observed.usable_rca
        record = FormalObservationCommand(
            store=store,
            plan=registry.for_execution(execution.execution_id),
            clock=clock).finalize(observed)
        assert json.loads(record.semantic_json)["disposition"] == "FAILED"
        with pytest.raises(ValueError, match="contradictory terminal"):
            FormalObservationCommand(
                store=store,
                plan=registry.for_execution(execution.execution_id),
                clock=clock).finalize(
                    observed, reason_code=ObservationReasonCode.TIMEOUT_REACHED)
    finally:
        store.close()


def test_polling_contract_is_frozen_and_adapter_does_not_sleep(tmp_path):
    contract = FormalObservationPollingContract()
    contract.validate()
    assert contract.polling_interval_ms == 1_000
    assert contract.timeout_ms == 300_000
    assert contract.adapter_version == FORMAL_OBSERVER_CONTRACT
    assert contract.actual_observation is ActualExecutionState.UNRESOLVED
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        observer, _, _ = _observer(store, registry, execution, clock, events)
        observer.observe(execution.execution_id)
        assert clock.waits == []
        source = inspect.getsource(__import__(
            "rca_evaluation.formal_scenario_io", fromlist=["*"]))
        assert "time.sleep" not in source
        assert "run_forever" not in source
    finally:
        store.close()


def _payload(store, execution_id, label):
    record = store.get(LedgerRecord.create(
        "observation", [execution_id, label], {}, execution_id).record_id)
    assert record is not None
    return json.loads(record.semantic_json), record


def test_observer_owns_first_incident_and_rca_endpoint_times_and_replay(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        clock.ns = 11_000_000_000
        incident_observer, _, _ = _observer(
            store, registry, execution, clock, events,
            environment="incident-only")
        incident_only = incident_observer.observe(execution.execution_id)
        assert incident_only.incident_observed_ns == 11_000_000_000
        assert incident_only.rca_observed_ns is None

        clock.ns = 13_000_000_000
        observer, _, _ = _observer(store, registry, execution, clock, events)
        endpoint = observer.observe(execution.execution_id)
        assert endpoint.incident_observed_ns == 11_000_000_000
        assert endpoint.rca_observed_ns == 13_000_000_000
        command = FormalObservationCommand(
            store=store,
            plan=registry.for_execution(execution.execution_id), clock=clock)
        first = command.finalize(endpoint)
        material = json.loads(first.semantic_json)
        assert material["disposition"] == "OBSERVED"
        assert material["incident_elapsed_ms"] == 1_000
        assert material["rca_elapsed_ms"] == 3_000

        clock.ns = 20_000_000_000
        replayed_endpoint = observer.observe(execution.execution_id)
        assert replayed_endpoint.incident_observed_ns == 11_000_000_000
        assert replayed_endpoint.rca_observed_ns == 13_000_000_000
        assert command.finalize(replayed_endpoint) == first
        with pytest.raises(ValueError, match="contradictory terminal"):
            command.finalize(
                replayed_endpoint,
                reason_code=ObservationReasonCode.PUBLIC_STATE_CONTRADICTION)
    finally:
        store.close()


def test_caller_endpoint_times_and_negative_latency_cannot_be_authority(tmp_path):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        observer, _, _ = _observer(store, registry, execution, clock, events)
        endpoint = observer.observe(execution.execution_id)
        forged = replace(endpoint, incident_observed_ns=1,
                         rca_observed_ns=2)
        command = FormalObservationCommand(
            store=store,
            plan=registry.for_execution(execution.execution_id), clock=clock)
        with pytest.raises(ValueError, match="latency endpoint|timing is contradictory"):
            command.finalize(forged)
        forged_lineage = replace(
            endpoint,
            public_lineage_commitment="sha256:" + "f" * 64)
        with pytest.raises(ValueError, match="timing is contradictory"):
            command.finalize(forged_lineage)
        with pytest.raises(ValueError, match="negative or invalid"):
            FormalObservationFact._derive(
                execution_identity=execution.execution_id, scenario_id="S1",
                signal_started_at="2026-10-16T00:00:00Z",
                signal_started_ns=10, observation_started_at="later",
                observation_started_ns=9, observation_ended_at="later",
                observation_ended_ns=11, endpoint=None,
                reason_code=ObservationReasonCode.TIMEOUT_REACHED)
    finally:
        store.close()


@pytest.mark.parametrize("elapsed_ms", [299_999, 300_000, 310_000])
def test_censoring_is_derived_only_at_or_after_exact_cutoff(tmp_path, elapsed_ms):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        observer, _, _ = _observer(
            store, registry, execution, clock, events,
            environment="event-only")
        unresolved = observer.observe(execution.execution_id)
        clock.ns = 10_000_000_000 + elapsed_ms * 1_000_000
        command = FormalObservationCommand(
            store=store,
            plan=registry.for_execution(execution.execution_id), clock=clock)
        if elapsed_ms < 300_000:
            with pytest.raises(ValueError, match="non-terminal"):
                command.finalize(unresolved)
        else:
            record = command.finalize(unresolved)
            payload = json.loads(record.semantic_json)
            assert payload["disposition"] == "CENSORED"
            assert payload["reason_code"] == "TIMEOUT_REACHED"
            assert payload["rca_elapsed_ms"] is None
    finally:
        store.close()


def test_production_side_payloads_and_typed_observation_are_ground_truth_free(
    tmp_path, caplog,
):
    _, _, store, registry, execution, clock, events, _, _ = _injected(tmp_path)
    try:
        synthetic = "expected_root_cause=SPEC017_SHOULD_NEVER_PERSIST"
        events.values = [{"event_id": "EVT-1",
                          "event_type": "brute_force_detected"}]
        observer, _, _ = _observer(
            store, registry, execution, clock, events,
            environment="event-only")
        unresolved = observer.observe(execution.execution_id)
        command = FormalObservationCommand(
            store=store,
            plan=registry.for_execution(execution.execution_id), clock=clock)
        with pytest.raises(ValueError):
            command.finalize(unresolved, reason_code=synthetic)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            command.finalize(  # type: ignore[call-arg]
                unresolved, diagnostics={"expected_root_cause": synthetic})
        terminal = command.finalize(
            unresolved,
            reason_code=ObservationReasonCode.EVENT_BINDING_CONFLICT)
        terminal_payload = json.loads(terminal.semantic_json)
        assert terminal_payload["disposition"] == "INVALID"
        assert terminal_payload["reason_code"] == "EVENT_BINDING_CONFLICT"
        material = json.dumps(registry.material(), sort_keys=True).lower()
        sent = store.get(LedgerRecord.create(
            "observation", [execution.execution_id, "formal-signal-sent"], {},
            execution.execution_id).record_id)
        material += sent.semantic_json.decode("utf-8").lower()
        material += (ROOT / "docker-compose.formal.yml").read_text(
            encoding="utf-8").lower()
        material += (ROOT / "configs/runtime_orchestration.docker.yaml").read_text(
            encoding="utf-8").lower()
        assert not any(term in material for term in GROUND_TRUTH_TERMS)
        assert synthetic not in material
        assert synthetic.encode() not in store.path.read_bytes()
        assert synthetic not in caplog.text
        assert synthetic not in terminal.semantic_json.decode("utf-8")
        assert synthetic not in terminal.semantic_commitment
    finally:
        store.close()
