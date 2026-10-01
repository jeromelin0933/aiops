"""S6 host integration checks on the existing Runtime startup/worker boundary."""

from datetime import datetime, timezone
from dataclasses import asdict, replace
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from incident_evidence.contracts import EvidenceReadiness, EvidenceRecoveryFacts
from knowledge_index.contracts import KnowledgeLocalReadiness
from llm_generation.contracts import LocalReadStatus
from rca_persistence.contracts import CreateAggregateRequest, RecoveryCandidateKind
from runtime_orchestration.clock import RuntimeClock
from runtime_orchestration.rca_host import RcaRecoveryKind, RcaRuntimeHost
from runtime_orchestration.rca_runtime_actions import RcaRuntimeActions
from runtime_orchestration.rca_publication import PublicationRuntimeDisposition
from runtime_orchestration.telemetry import NullRuntimeTelemetry, StdlibRuntimeTelemetry, RuntimeTelemetryEvent
from runtime_orchestration.worker import RuntimeCycleResult
from runtime_orchestration.cli import (
    _ClosableGenerationProvider, build_runtime_application,
)
from runtime_orchestration.config import load_runtime_config
from runtime_orchestration.worker import RuntimeWorkerMode
from runtime_orchestration.rca_continuation import SqliteRcaContinuationStore, rca_root_id
from incident_evidence import (
    LokiRangeAdapter, PrometheusRangeAdapter, SourceStatus,
    SqliteEvidenceStore,
)
from rca_persistence import SqliteRcaStore
from llm_generation.sqlite_store import CandidateDStore
from llm_generation.service import ProviderCapability, ProviderFailureKind, ProviderInvocationError
from runtime_orchestration.contracts import RuntimeWorkKind, RuntimeWorkStatus
from runtime_orchestration.identity import runtime_work_id
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore
from test_incident_evidence_capture import (
    FakeAdapter, capture_command, capture_service,
)
from test_llm_generation_phase2 import _config as generation_config
from test_runtime_host_e2e import _HostRuntime, _event
from test_spec_016_followup_refresh import env
from knowledge_index import (
    ActivationOperationKey, BuildActivationRequest, BuildIdentityInput,
    BuildOperationKey, ChromaIndexAdapter, GoogleEmbeddingAdapter,
    KnowledgeBuildService, SqliteKnowledgeStore,
    admit_production_manifest, load_knowledge_config, plan_chunks,
)


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _Incidents:
    def __init__(self, ids=()): self.ids = ids
    def validate_readiness(self): return None
    def list_correlation_views(self):
        return tuple(SimpleNamespace(incident_id=x) for x in self.ids)
    def get_incident(self, incident_id):
        return SimpleNamespace(incident_id=incident_id) if incident_id in self.ids else None


class _A:
    def __init__(self, candidates=(), aggregates=None):
        self.candidates = candidates
        self.aggregates = aggregates or {}
    def validate_local_readiness(self): return None
    def enumerate_recovery_candidates(self): return self.candidates
    def get_aggregate(self, aggregate_id): return self.aggregates.get(aggregate_id)
    def get_aggregate_by_incident(self, incident_id):
        return next((item for item in self.aggregates.values()
                     if item.incident_id == incident_id), None)
    def get_current(self, aggregate_id): return None
    def get_version_history(self, aggregate_id): return ()


class _B:
    def validate_local_readiness(self): return EvidenceReadiness.READY
    def enumerate_recovery_facts(self): return EvidenceRecoveryFacts((), (), (), ())


class _C:
    def __init__(self, status=KnowledgeLocalReadiness.READY): self.status = status
    def local_readiness(self):
        return SimpleNamespace(status=self.status)


class _D:
    def local_readiness(self): return LocalReadStatus.FOUND
    def recovery(self):
        return SimpleNamespace(status=LocalReadStatus.FOUND,
                               value=SimpleNamespace(results=(), failures=()))


class _D2:
    def enumerate_all(self):
        return SimpleNamespace(records=(), isolated_corruptions=())
    def get(self, root):
        return None


class _Actions:
    def __init__(self): self.calls = []
    def reconcile_publication(self, operation_id, observed_at):
        self.calls.append(("publication", operation_id))
    def advance(self, subject, observed_at):
        self.calls.append(("advance", subject.kind))
        return True


def _host(*, incidents=None, a=None, b=None, c=None, d=None, d2=None,
          actions=None, runtime_work=None):
    actions = actions or _Actions()
    return RcaRuntimeHost(incidents=incidents or _Incidents(), candidate_a=a or _A(),
        candidate_b=b or _B(), candidate_c=c or _C(), candidate_d=d or _D(),
        continuations=d2 or _D2(), actions=actions,
        clock=RuntimeClock(lambda: NOW, lambda: 0.0, lambda _: None),
        telemetry=NullRuntimeTelemetry(), runtime_work_store=runtime_work), actions


def _snapshot():
    return SimpleNamespace(runtime_work=SimpleNamespace(records=(), isolated_corruptions=()))


@pytest.mark.parametrize("coverage_kind", ["incomplete", "complete", "contradictory"])
def test_concrete_follow_up_dispatch_passes_coverage_to_real_orchestrator(
    env, monkeypatch, coverage_kind,
):
    from copy import deepcopy
    from datetime import timedelta
    from runtime_orchestration.rca_host import RcaRecoverySubject
    from test_spec_016_followup_refresh import INCIDENT, _completed_post_context, _post

    member = _post(env.baseline)
    env.flow.admit(INCIDENT, member)
    coverage = env.baseline
    if coverage_kind != "incomplete":
        coverage = _completed_post_context(env)
        env.time.now += timedelta(minutes=2)
    if coverage_kind == "contradictory":
        original_read = env.b.resolve_snapshot

        def contradictory_read(snapshot_id):
            snapshot = original_read(snapshot_id)
            if snapshot_id == coverage.snapshot_id:
                content = deepcopy(snapshot.snapshot_content)
                content["post_context"]["effective_window_ends"]["LOKI"] = "2026-09-21T12:01:00Z"
                return replace(snapshot, snapshot_content=content)
            return snapshot

        monkeypatch.setattr(env.b, "resolve_snapshot", contradictory_read)

    observations = []
    original_recheck = env.flow._recheck

    def observe_recheck(incident_id, post_context_snapshots):
        observations.append((incident_id, dict(post_context_snapshots)))
        return original_recheck(incident_id, post_context_snapshots)

    monkeypatch.setattr(env.flow, "_recheck", observe_recheck)
    actions = RcaRuntimeActions(
        incidents=env.incidents, candidate_a=env.a, candidate_b=env.b.store,
        candidate_c=object(), capture_service=object(), continuations=env.d2,
        initial=object(), execution=object(), follow_up=env.flow,
        publication=object(), evidence_policy=object(),
        evidence_config_identity="config", knowledge_config=object(),
        generation_config=object(), retry_limit=4,
    )
    # Limit discovery to this test's admitted post-context coverage; A/B/D2
    # reads and the concrete public recheck remain real production methods.
    monkeypatch.setattr(actions, "_snapshots", lambda incident_id: (
        env.b.store.resolve_snapshot(coverage.snapshot_id),))
    subject = RcaRecoverySubject(INCIDENT, RcaRecoveryKind.FOLLOW_UP_PENDING)
    if coverage_kind == "contradictory":
        with pytest.raises(ValueError, match="full follow-up frontier requires repair"):
            actions.advance(subject, env.time.now)
    else:
        assert actions.advance(subject, env.time.now) is (coverage_kind == "complete")
    assert observations == [(INCIDENT, {member: coverage.snapshot_id})]
    state = env.d2.get(env.root)
    assert state.follow_up_complete is (coverage_kind == "complete")
    assert state.unresolved_frontier == (() if coverage_kind == "complete" else (member,))
    assert env.d2.get_follow_up_work(env.root).status is RuntimeWorkStatus.OUTSTANDING


def test_rca_dispatch_cannot_cross_startup_barrier():
    host, _ = _host()
    with pytest.raises(RuntimeError, match="startup recovery barrier"):
        host.run_cycle(should_stop=lambda: False)
    host.recover(_snapshot())
    assert host.run_cycle(should_stop=lambda: False).acquired_work == 0


def test_publication_reconciles_during_startup_before_other_rca_work():
    candidate = SimpleNamespace(kind=RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
        aggregate_id="agg-1", publication_operation_id="pub-1")
    a = _A((candidate,), {"agg-1": SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")})
    host, actions = _host(incidents=_Incidents(("INC-1",)), a=a)
    host.recover(_snapshot())
    assert actions.calls == [("publication", "pub-1")]
    assert any(item.kind is RcaRecoveryKind.PUBLICATION for item in host.subjects)


def test_publication_recovery_reclassifies_before_initial_dispatch():
    candidate = SimpleNamespace(kind=RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
        aggregate_id="agg-1", publication_operation_id="pub-1")
    a = _A((candidate,), {"agg-1": SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")})
    class Reconcile(_Actions):
        def reconcile_publication(self, operation_id, observed_at):
            super().reconcile_publication(operation_id, observed_at)
            a.candidates = ()
    host, actions = _host(incidents=_Incidents(("INC-1",)), a=a, actions=Reconcile())
    host.recover(_snapshot())
    assert actions.calls == [("publication", "pub-1")]
    assert all(item.kind is not RcaRecoveryKind.PUBLICATION for item in host.subjects)
    host.run_cycle(should_stop=lambda: False)
    assert actions.calls[-1] == ("advance", RcaRecoveryKind.INITIAL_INCIDENT)


def test_missing_d2_keeps_publication_reconciliation_before_fail_closed():
    target = SimpleNamespace(publication_operation_id="pub-1", incident_id="INC-1")
    receipt = SimpleNamespace(target=target)
    calls = []
    class A:
        def get_publication_result(self, op):
            return receipt if op == "pub-1" else None
    class Publication:
        def inspect(self, selected):
            calls.append("inspect")
            return SimpleNamespace(classification=SimpleNamespace(
                value="A_COMMITTED_008_INCOMPLETE")), None
        def reconcile(self, selected, observed_at):
            calls.append("reconcile")
            return SimpleNamespace(
                disposition=PublicationRuntimeDisposition.RUNTIME_BOOKKEEPING_LOST)
    class D2:
        def get(self, root): return None
    actions = RcaRuntimeActions(incidents=object(), candidate_a=A(),
        candidate_b=object(), candidate_c=object(), capture_service=object(),
        continuations=D2(), initial=object(), execution=object(),
        follow_up=object(), publication=Publication(),
        evidence_policy=object(), evidence_config_identity="config",
        knowledge_config=object(), generation_config=object(), retry_limit=4)
    with pytest.raises(ValueError, match="publication reconciliation is not coherent"):
        actions.reconcile_publication("pub-1", NOW)
    assert calls == ["inspect", "reconcile"]


@pytest.mark.parametrize("disposition,expected", [
    (None, RcaRecoveryKind.VALIDATED_D_RESULT),
    ("RETRYABLE", RcaRecoveryKind.RETRYABLE_TRY_FAILURE),
    ("NON_RETRYABLE", RcaRecoveryKind.NON_RETRYABLE_TRY_FAILURE),
    ("REPAIR_REQUIRED", RcaRecoveryKind.REPAIR_REQUIRED_TRY_FAILURE),
])
def test_d_result_and_typed_failure_recovery_classes(disposition, expected):
    a = _A(aggregates={"agg-1": SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")})
    lineage = SimpleNamespace(attempt=SimpleNamespace(
        lineage=SimpleNamespace(aggregate_id="agg-1")), try_outcomes=())
    a.get_attempt_lineage = lambda attempt_id: lineage if attempt_id == "try-1" else None
    fact = SimpleNamespace(operation_id="d-op-1",
        try_identity=SimpleNamespace(attempt_id="try-1"),
        validated_result_id="result-1",
        retry_safety=SimpleNamespace(value=disposition))
    class D(_D):
        def recovery(self):
            return SimpleNamespace(status=LocalReadStatus.FOUND,
                value=SimpleNamespace(results=(fact,) if disposition is None else (),
                                      failures=() if disposition is None else (fact,)))
    host, _ = _host(incidents=_Incidents(("INC-1",)), a=a, d=D())
    host.recover(_snapshot())
    assert expected in {item.kind for item in host.subjects}


def test_follow_up_and_post_context_recovery_restores_absolute_wake():
    from runtime_orchestration.rca_continuation import FollowUpRequirement
    member = FollowUpRequirement("POST_CONTEXT", "boundary:snapshot-1")
    work = SimpleNamespace(work_id="work-1", incident_id="INC-1",
                           status=RuntimeWorkStatus.OUTSTANDING,
                           work_kind=RuntimeWorkKind.RCA_FOLLOW_UP,
                           next_retry_at=None)
    state = SimpleNamespace(incident_id="INC-1", root_id=rca_root_id("INC-1"),
        unresolved_frontier=(member,), admitted_frontier=(member,),
        next_eligibility_at=NOW.replace(day=2), stage="FOLLOW_UP")
    class D2(_D2):
        def enumerate_all(self):
            return SimpleNamespace(records=(state,), isolated_corruptions=())
        def get(self, root):
            return state if root == state.root_id else None
        def get_follow_up_work(self, root):
            assert root == state.root_id
            return work
    host, actions = _host(incidents=_Incidents(("INC-1",)), d2=D2())
    host.recover(SimpleNamespace(runtime_work=SimpleNamespace(
        records=(work,), isolated_corruptions=())))
    assert {item.kind for item in host.subjects} == {
        RcaRecoveryKind.FOLLOW_UP_PENDING, RcaRecoveryKind.POST_CONTEXT_PENDING}
    result = host.run_cycle(should_stop=lambda: False)
    assert result.next_eligibility == state.next_eligibility_at
    assert actions.calls == []


@pytest.mark.parametrize("candidate_kind,expected", [
    (RecoveryCandidateKind.STALE_CURRENT, RcaRecoveryKind.STALE_CURRENT),
    (RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION,
     RcaRecoveryKind.RETRY_PENDING),
])
def test_a_recovery_candidates_are_classified(candidate_kind, expected):
    candidate = SimpleNamespace(kind=candidate_kind, aggregate_id="agg-1",
                                attempt_id="attempt-1")
    aggregate = SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")
    work = SimpleNamespace(work_id="initial", work_kind=RuntimeWorkKind.RCA_INITIAL,
                           status=RuntimeWorkStatus.OUTSTANDING,
                           incident_id="INC-1", next_retry_at=None)
    state = SimpleNamespace(incident_id="INC-1", root_id=rca_root_id("INC-1"),
        unresolved_frontier=(), admitted_frontier=(), next_eligibility_at=None,
        stage="INITIAL")
    class D2(_D2):
        def enumerate_all(self):
            return SimpleNamespace(records=(state,), isolated_corruptions=())
        def get(self, root): return state if root == state.root_id else None
        def get_follow_up_work(self, root): return work
    host, _ = _host(incidents=_Incidents(("INC-1",)),
                    a=_A((candidate,), {"agg-1": aggregate}), d2=D2())
    host.recover(SimpleNamespace(runtime_work=SimpleNamespace(
        records=(work,), isolated_corruptions=())))
    assert expected in {item.kind for item in host.subjects}


def test_exhausted_attempt_budget_is_classified_without_dispatch():
    initial = SimpleNamespace(work_id="initial", work_kind=RuntimeWorkKind.RCA_INITIAL,
                              status=RuntimeWorkStatus.OUTSTANDING,
                              incident_id="INC-1", next_retry_at=None)
    exhausted = SimpleNamespace(work_id="attempt-work",
        work_kind=RuntimeWorkKind.RCA_ATTEMPT,
        status=RuntimeWorkStatus.EXHAUSTED, incident_id="INC-1",
        operation_id="attempt-1", stage="EXHAUSTED",
        source_retry_disposition="RETRYABLE", next_retry_at=None)
    state = SimpleNamespace(incident_id="INC-1", root_id=rca_root_id("INC-1"),
        unresolved_frontier=(), admitted_frontier=(), next_eligibility_at=None,
        stage="EXHAUSTED")
    class D2(_D2):
        def enumerate_all(self):
            return SimpleNamespace(records=(state,), isolated_corruptions=())
        def get(self, root): return state if root == state.root_id else None
        def get_follow_up_work(self, root): return initial
    failure = SimpleNamespace(
        try_identity=SimpleNamespace(attempt_id="attempt-1"),
        retry_safety=SimpleNamespace(value="RETRYABLE"), operation_id="d-failure")
    class D(_D):
        def recovery(self):
            return SimpleNamespace(status=LocalReadStatus.FOUND,
                value=SimpleNamespace(results=(), failures=(failure,)))
    a = _A(aggregates={"agg-1": SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")})
    a.get_attempt_lineage = lambda attempt_id: SimpleNamespace(
        attempt=SimpleNamespace(lineage=SimpleNamespace(aggregate_id="agg-1")),
        try_outcomes=(SimpleNamespace(identity=failure.try_identity),))
    host, actions = _host(incidents=_Incidents(("INC-1",)), a=a, d=D(), d2=D2(),
        c=_C(KnowledgeLocalReadiness.NOT_INITIALIZED))
    host.recover(SimpleNamespace(runtime_work=SimpleNamespace(
        records=(initial, exhausted), isolated_corruptions=())))
    assert RcaRecoveryKind.RETRY_BUDGET_EXHAUSTED in {
        item.kind for item in host.subjects}
    # The real executor reconciles an exhausted domain outcome without a D invocation.
    assert exhausted.next_retry_at is None


@pytest.mark.parametrize("replacement", [
    "https://example.com/query", "http://user:secret@localhost:3100/query",
    "http://localhost:3100/query?api_key=secret",
])
def test_rca_config_rejects_remote_or_credential_bearing_endpoint(tmp_path, replacement):
    import yaml
    root = tmp_path / "strict"
    config_path = _cli_config(root)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["rca"]["loki_endpoint"] = replacement
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_runtime_config(config_path)


def test_repo_local_and_docker_runtime_configs_enable_one_rca_host():
    repository = Path(__file__).resolve().parents[1]
    local = load_runtime_config(repository / "configs" / "runtime_orchestration.yaml")
    docker = load_runtime_config(repository / "configs" /
                                 "runtime_orchestration.docker.yaml")
    assert local.rca is not None and docker.rca is not None
    assert local.retry_limit == docker.retry_limit == 4
    assert local.retry_delays_seconds == docker.retry_delays_seconds
    assert local.rca.generation_config_path == docker.rca.generation_config_path


def test_unreadable_b_enumeration_fails_closed_before_rca_dispatch():
    class BrokenB(_B):
        def enumerate_recovery_facts(self): raise OSError("unreadable")
    host, actions = _host(b=BrokenB())
    host.recover(_snapshot())
    assert actions.calls == []
    assert host.subjects[0].kind is RcaRecoveryKind.REPAIR_REQUIRED
    assert host.run_cycle(should_stop=lambda: False).acquired_work == 0


def test_reliably_absent_knowledge_build_is_distinct_from_corruption():
    host, actions = _host(incidents=_Incidents(("INC-1",)),
                    c=_C(KnowledgeLocalReadiness.NOT_INITIALIZED))
    host.recover(_snapshot())
    assert tuple(item.kind for item in host.subjects) == (
        RcaRecoveryKind.KNOWLEDGE_NOT_READY,)
    assert host.run_cycle(should_stop=lambda: False).acquired_work == 0
    assert actions.calls == []


def test_missing_d2_does_not_hide_initial_incident_obligation():
    host, actions = _host(incidents=_Incidents(("INC-1",)))
    host.recover(_snapshot())
    assert len(host.subjects) == 1
    assert host.subjects[0].kind is RcaRecoveryKind.INITIAL_INCIDENT
    result = host.run_cycle(should_stop=lambda: False)
    assert result.acquired_work == result.completed_safe_boundaries == 1
    assert actions.calls == [("advance", RcaRecoveryKind.INITIAL_INCIDENT)]


def test_missing_d2_with_existing_aggregate_reconstructs_same_initial_root():
    aggregate = SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")
    host, _ = _host(incidents=_Incidents(("INC-1",)),
                    a=_A(aggregates={aggregate.aggregate_id: aggregate}))
    host.recover(_snapshot())
    assert tuple(item.kind for item in host.subjects) == (RcaRecoveryKind.INITIAL_INCIDENT,)


def test_missing_d2_after_b_effect_fails_closed_without_replacing_capture_basis():
    aggregate = SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")
    class B(_B):
        def enumerate_recovery_facts(self):
            return EvidenceRecoveryFacts((),
                (SimpleNamespace(incident_id="INC-1"),), (), ())
    host, actions = _host(incidents=_Incidents(("INC-1",)), b=B(),
        a=_A(aggregates={aggregate.aggregate_id: aggregate}))
    host.recover(_snapshot())
    assert tuple(item.kind for item in host.subjects) == (RcaRecoveryKind.REPAIR_REQUIRED,)
    assert host.run_cycle(should_stop=lambda: False).acquired_work == 0
    assert actions.calls == []


def test_missing_d2_b_effect_remains_repair_when_c_not_initialized():
    aggregate = SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")
    facts = EvidenceRecoveryFacts((),
        (SimpleNamespace(incident_id="INC-1"),), (), ())
    class B(_B):
        def enumerate_recovery_facts(self): return facts
    incidents = _Incidents(("INC-1",))
    a = _A(aggregates={aggregate.aggregate_id: aggregate})
    d = _D()
    host, actions = _host(incidents=incidents, a=a, b=B(), d=d,
        c=_C(KnowledgeLocalReadiness.NOT_INITIALIZED))
    classified = host._classify(("INC-1",), (), facts, d.recovery().value,
                                 (), _snapshot())
    assert tuple(item.kind for item in classified) == (RcaRecoveryKind.REPAIR_REQUIRED,)
    with pytest.raises(RuntimeError, match="startup recovery barrier"):
        host.run_cycle(should_stop=lambda: False)
    host.recover(_snapshot())
    assert tuple(item.kind for item in host.subjects) == (RcaRecoveryKind.REPAIR_REQUIRED,)
    assert host.run_cycle(should_stop=lambda: False).acquired_work == 0
    assert actions.calls == []


def test_publication_still_reconciles_first_without_downgrading_same_subject_repair():
    candidate = SimpleNamespace(kind=RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
        aggregate_id="agg-1", publication_operation_id="pub-1")
    aggregate = SimpleNamespace(aggregate_id="agg-1", incident_id="INC-1")
    class B(_B):
        def enumerate_recovery_facts(self):
            return EvidenceRecoveryFacts((),
                (SimpleNamespace(incident_id="INC-1"),), (), ())
    host, actions = _host(incidents=_Incidents(("INC-1",)),
        a=_A((candidate,), {"agg-1": aggregate}), b=B(),
        c=_C(KnowledgeLocalReadiness.NOT_INITIALIZED))
    host.recover(_snapshot())
    assert actions.calls == [("publication", "pub-1")]
    assert RcaRecoveryKind.REPAIR_REQUIRED in {item.kind for item in host.subjects}
    assert RcaRecoveryKind.KNOWLEDGE_NOT_READY not in {
        item.kind for item in host.subjects}
    assert host.run_cycle(should_stop=lambda: False).acquired_work == 0
    assert actions.calls == [("publication", "pub-1")]


def test_missing_d2_b_effect_repair_survives_store_reopen_and_dispatch_guard(tmp_path):
    a_path = tmp_path / "rca.sqlite3"
    b_path = tmp_path / "evidence.sqlite3"
    work_path = tmp_path / "runtime.sqlite3"
    with SqliteRcaStore(a_path) as candidate_a:
        candidate_a.create_or_discover_aggregate(
            CreateAggregateRequest("discover-op-1", "agg-1", "INC-1", NOW))
    with SqliteEvidenceStore(b_path) as candidate_b:
        service, *_ = capture_service(tmp_path, store=candidate_b)
        service.capture_evidence(capture_command("capture-op-1"))
        assert len(candidate_b.enumerate_recovery_facts().snapshots) == 1
    for _ in range(2):
        with (SqliteRcaStore(a_path) as candidate_a,
              SqliteEvidenceStore(b_path) as candidate_b,
              SqliteRuntimeWorkStore(work_path) as work,
              SqliteRcaContinuationStore(work_path) as d2):
            host, actions = _host(incidents=_Incidents(("INC-1",)),
                a=candidate_a, b=candidate_b,
                c=_C(KnowledgeLocalReadiness.NOT_INITIALIZED),
                d2=d2, runtime_work=work)
            host.recover(SimpleNamespace(runtime_work=work.enumerate_all()))
            assert tuple(item.kind for item in host.subjects) == (
                RcaRecoveryKind.REPAIR_REQUIRED,)
            assert host.run_cycle(should_stop=lambda: False).acquired_work == 0
            assert actions.calls == []
            assert d2.enumerate_all().records == ()
            assert len(candidate_b.enumerate_recovery_facts().snapshots) == 1


def test_no_progress_action_does_not_falsely_report_completed_work():
    class NoProgress(_Actions):
        def advance(self, subject, observed_at):
            self.calls.append(("advance", subject.kind))
            return False
    host, actions = _host(incidents=_Incidents(("INC-1",)), actions=NoProgress())
    host.recover(_snapshot())
    result = host.run_cycle(should_stop=lambda: False)
    assert result.acquired_work == result.completed_safe_boundaries == 0
    assert actions.calls == [("advance", RcaRecoveryKind.INITIAL_INCIDENT)]


def test_existing_worker_dispatches_rca_after_recovery_and_honors_stop(tmp_path):
    class Port:
        def __init__(self): self.recovered = False; self.cycles = 0
        def recover(self, snapshot):
            assert snapshot.runtime_work.records == ()
            self.recovered = True
        def run_cycle(self, *, should_stop):
            assert self.recovered and not should_stop()
            self.cycles += 1
            return RuntimeCycleResult(1, 1) if self.cycles == 1 else RuntimeCycleResult(0, 0)
    port = Port()
    runtime = _HostRuntime(tmp_path / "runtime", [], rca=port)
    try:
        runtime.run_until_idle()
        assert port.cycles >= 2
        runtime.worker.stop_controller.request_stop()
        assert runtime.core.run_cycle() == RuntimeCycleResult(0, 0)
        assert port.cycles >= 2
    finally:
        runtime.close()


def test_rca_telemetry_is_bounded_and_filters_credential_shapes(caplog):
    telemetry = StdlibRuntimeTelemetry(logging.getLogger("spec016.s6.telemetry"))
    with caplog.at_level(logging.INFO, logger="spec016.s6.telemetry"):
        telemetry.emit(RuntimeTelemetryEvent.RECOVERY, observed_at=NOW,
                       stage="RCA_STARTUP", root_id="root-1",
                       recovery_classification="api_key=should-not-appear")
    assert "should-not-appear" not in caplog.text
    assert "[REDACTED]" in caplog.text


def _cli_config(root: Path) -> Path:
    repository = Path(__file__).resolve().parents[1]
    (root / "configs").mkdir(parents=True)
    for name in ("incident_evidence.yaml", "knowledge_index.yaml"):
        (root / "configs" / name).write_text(
            (repository / "configs" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    generation = generation_config()
    data = {
        "version": generation.version, "provider": generation.pin.provider,
        "model": generation.pin.model,
        "profile": asdict(generation.pin.profile),
        "prompt": asdict(generation.pin.prompt),
        "result_schema": asdict(generation.pin.result_schema),
        "configuration": asdict(generation.pin.configuration),
        "bounds": asdict(generation.bounds),
        "hidden_retries_disabled": True,
        "prompt_template": generation.prompt_template,
        "result_schema_commitment": generation.result_schema_commitment,
        "configuration_commitment": generation.configuration_commitment,
    }
    (root / "configs" / "generation.json").write_text(
        json.dumps(data), encoding="utf-8")
    text = (repository / "configs" / "runtime_orchestration.yaml").read_text(
        encoding="utf-8")
    for old, new in (
        ("events/event_store.jsonl", "runtime/events.jsonl"),
        ("var/runtime_orchestration/correlation_state.sqlite3", "runtime/state.sqlite3"),
        ("var/runtime_orchestration/incidents.sqlite3", "runtime/incidents.sqlite3"),
        ("var/runtime_orchestration/shadows.sqlite3", "runtime/shadows.sqlite3"),
        ("var/runtime_orchestration/runtime_work.sqlite3", "runtime/runtime.sqlite3"),
    ):
        text = text.replace(old, new)
    text += """
rca:
  a_store_path: runtime/a.sqlite3
  b_store_path: runtime/b.sqlite3
  d_store_path: runtime/d.sqlite3
  knowledge_config_path: configs/knowledge_index.yaml
  generation_config_path: configs/generation.json
  evidence_config_path: configs/incident_evidence.yaml
  loki_endpoint: http://localhost:3100/loki/api/v1/query_range
  prometheus_endpoint: http://localhost:9090/api/v1/query_range
"""
    path = root / "configs" / "runtime.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _seed_active_knowledge(root: Path, embedding_client, chroma_client) -> None:
    repository = Path(__file__).resolve().parents[1]
    config = load_knowledge_config(root / "configs" / "knowledge_index.yaml")
    admission = admit_production_manifest(
        "configs/knowledge_manifest.json", repository_root=repository)
    chunks = plan_chunks(
        admission, source_root=repository / "docs" / "knowledge",
        profile=config.chunking)
    identity = BuildIdentityInput(
        admission.manifest_commitment,
        tuple(item.chunk_identity for item in chunks),
        config.chunking.profile_identity, "1.0",
        config.capability.provider, config.capability.model,
        config.capability.embedding_profile_identity,
        config.capability.embedding_dimension,
        config.normalization_semantics,
        "chromadb", config.index_schema_identity,
        "spec014-knowledge-metadata-v1",
        "spec014-build-contract-v2",
    )

    path = root / config.authority_store_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteKnowledgeStore(path) as store:
        build = KnowledgeBuildService(
            store,
            GoogleEmbeddingAdapter(embedding_client, config.capability),
            ChromaIndexAdapter(root / config.chroma_path,
                               index_schema_identity=config.index_schema_identity,
                               client=chroma_client))
        staged = build.stage(
            operation_key=BuildOperationKey("s6-cli-stage"),
            raw_manifest=json.loads(
                (repository / config.manifest_path).read_text(encoding="utf-8")),
            source_root=repository / "docs" / "knowledge",
            chunks=chunks, identity_input=identity, limits=config.limits,
            required_capability_identity=config.capability.capability_identity,
        )
        assert staged.record is not None
        validation = build.validate(
            staged.record.build_identity,
            BuildOperationKey("s6-cli-validate"), maximum_probe_results=12)
        assert not validation.findings
        build.activate(BuildActivationRequest(
            staged.record.build_identity, ActivationOperationKey("s6-cli-activate"), 0))


def test_actual_cli_composes_public_rca_stores_and_closes_clients(tmp_path, monkeypatch):
    root = tmp_path / "cli"
    seeded = _HostRuntime(root / "runtime", [])
    seeded.close()
    config_path = _cli_config(root)
    clients = []

    class Client:
        def __init__(self): self.closed = False; clients.append(self)
        def close(self): self.closed = True

    fake_chroma = SimpleNamespace(PersistentClient=lambda **kwargs: Client())
    fake_settings = SimpleNamespace(Settings=lambda **kwargs: object())
    monkeypatch.setitem(sys.modules, "chromadb", fake_chroma)
    monkeypatch.setitem(sys.modules, "chromadb.config", fake_settings)
    monkeypatch.setattr("runtime_orchestration.cli.create_google_client",
                        lambda **kwargs: Client())
    monkeypatch.setattr("runtime_orchestration.cli.create_google_generation_client",
                        lambda *args: Client())
    app = build_runtime_application(
        load_runtime_config(config_path), project_root=root,
        config_path=config_path)
    try:
        assert app.worker._core._rca is not None
        app.run(RuntimeWorkerMode.RUN_UNTIL_IDLE)
        assert app.worker._core._rca.subjects == ()
    finally:
        app.close()
    # An idle host has no reason to acquire provider credentials.
    assert len(clients) == 1 and clients[0].closed


def test_cli_missing_active_knowledge_build_preserves_platform_dispatch(
    tmp_path, monkeypatch
):
    root = tmp_path / "cli-no-active-build"
    event = _event("EVT-RCA-C-NOT-READY", "brute_force_detected",
                   datetime(2026, 9, 15, tzinfo=timezone.utc),
                   source_ip="203.0.113.82")
    event["detection_method"] = "isolation_forest"
    seeded = _HostRuntime(root / "runtime", [event])
    seeded.close()
    config_path = _cli_config(root)
    chroma = SimpleNamespace(close=lambda: None)
    monkeypatch.setitem(sys.modules, "chromadb",
                        SimpleNamespace(PersistentClient=lambda **kwargs: chroma))
    monkeypatch.setitem(sys.modules, "chromadb.config",
                        SimpleNamespace(Settings=lambda **kwargs: object()))
    monkeypatch.setattr("runtime_orchestration.cli.create_google_client",
                        lambda **kwargs: pytest.fail("no C provider call before Active build"))
    monkeypatch.setattr("runtime_orchestration.cli.create_google_generation_client",
                        lambda *args: pytest.fail("no D provider call before Active build"))
    app = build_runtime_application(load_runtime_config(config_path),
        project_root=root, config_path=config_path)
    try:
        app.run(RuntimeWorkerMode.RUN_UNTIL_IDLE)
        host = app.worker._core._rca
        assert RcaRecoveryKind.KNOWLEDGE_NOT_READY in {
            subject.kind for subject in host.subjects}
    finally:
        app.close()
    with SqliteRcaStore(root / "runtime" / "a.sqlite3") as a:
        assert a.enumerate_recovery_candidates() == ()


def test_actual_cli_dispatches_initial_rca_through_public_a_b_c_ports(
    tmp_path, monkeypatch
):
    root = tmp_path / "cli-dispatch"
    admitted_event = _event("EVT-RCA-CLI", "brute_force_detected",
                            datetime(2026, 9, 15, tzinfo=timezone.utc),
                            source_ip="203.0.113.81")
    admitted_event["detection_method"] = "isolation_forest"
    seeded = _HostRuntime(root / "runtime", [admitted_event])
    try:
        seeded.run_until_idle()
        incident_ids = tuple(item.incident_id
                             for item in seeded.incidents.list_correlation_views())
        assert len(incident_ids) == 1
    finally:
        seeded.close()
    config_path = _cli_config(root)
    clients = []

    class Collection:
        def __init__(self, metadata):
            self.metadata = metadata
            self.ids = []
            self.embeddings = []
            self.metadatas = []
        def add(self, *, ids, embeddings, metadatas):
            self.ids = ids
            self.embeddings = embeddings
            self.metadatas = metadatas
        def get(self, **kwargs):
            return {"ids": self.ids, "embeddings": self.embeddings,
                    "metadatas": self.metadatas}
        def query(self, **kwargs):
            count = kwargs["n_results"]
            return {"ids": [self.ids[:count]],
                    "distances": [[0.1] * count],
                    "metadatas": [self.metadatas[:count]]}

    class ChromaClient:
        def __init__(self):
            self.closed = False
            self.collections = {}
            clients.append(self)
        def create_collection(self, name, *, metadata, embedding_function):
            collection = Collection(metadata)
            self.collections[name] = collection
            return collection
        def get_collection(self, name, *, embedding_function):
            return self.collections[name]
        def close(self): self.closed = True

    class EmbeddingClient:
        def __init__(self):
            self.closed = False
            self.models = self
            clients.append(self)
        def embed_content(self, **kwargs):
            return SimpleNamespace(embeddings=[
                SimpleNamespace(values=[0.1] * 768)
                for _ in kwargs["contents"]])
        def close(self): self.closed = True

    class GenerationClient:
        def __init__(self):
            self.closed = False
            self.models = self
            clients.append(self)
        def count_tokens(self, **kwargs):
            return SimpleNamespace(total_tokens=10)
        def generate_content(self, **kwargs):
            raise TimeoutError("deterministic provider timeout")
        def close(self): self.closed = True

    chroma_client = ChromaClient()
    embedding_client = EmbeddingClient()
    generation_client = GenerationClient()
    _seed_active_knowledge(root, embedding_client, chroma_client)

    monkeypatch.setitem(sys.modules, "chromadb",
                        SimpleNamespace(PersistentClient=lambda **kwargs: chroma_client))
    monkeypatch.setitem(sys.modules, "chromadb.config",
                        SimpleNamespace(Settings=lambda **kwargs: object()))
    monkeypatch.setattr("runtime_orchestration.cli.create_google_client",
                        lambda **kwargs: embedding_client)
    monkeypatch.setattr("runtime_orchestration.cli.create_google_generation_client",
                        lambda *args: generation_client)
    monkeypatch.setattr(LokiRangeAdapter, "collect",
                        lambda self, request: FakeAdapter(SourceStatus.EMPTY).collect(request))
    monkeypatch.setattr(PrometheusRangeAdapter, "collect",
                        lambda self, request: FakeAdapter(SourceStatus.EMPTY).collect(request))
    monkeypatch.setattr("runtime_orchestration.cli.RuntimeClock.system",
        lambda: RuntimeClock(lambda: NOW, lambda: 0.0, lambda _: None))
    app = build_runtime_application(
        load_runtime_config(config_path), project_root=root,
        config_path=config_path)
    try:
        app.run(RuntimeWorkerMode.RUN_UNTIL_IDLE)
    finally:
        app.close()
    with SqliteRcaStore(root / "runtime" / "a.sqlite3") as candidate_a:
        aggregate = candidate_a.get_aggregate_by_incident(incident_ids[0])
        assert aggregate is not None
        candidates = candidate_a.enumerate_recovery_candidates()
        assert len(candidates) == 1 and candidates[0].attempt_id is not None
    with SqliteEvidenceStore(root / "runtime" / "b.sqlite3") as candidate_b:
        facts = candidate_b.enumerate_recovery_facts()
        assert len(facts.snapshots) == 1
        assert facts.snapshots[0].incident_id == incident_ids[0]
    with SqliteRcaContinuationStore(root / "runtime" / "runtime.sqlite3") as d2:
        state = d2.get(rca_root_id(incident_ids[0]))
        assert state is not None and state.capture_basis is not None
    d_recovery = CandidateDStore(root / "runtime" / "d.sqlite3").recovery()
    assert len(d_recovery.value.failures) == 1
    assert d_recovery.value.failures[0].try_identity.attempt_id == candidates[0].attempt_id
    with SqliteRuntimeWorkStore(root / "runtime" / "runtime.sqlite3") as work:
        attempt_work = work.get(runtime_work_id(
            RuntimeWorkKind.RCA_ATTEMPT, rca_root_id(incident_ids[0]),
            candidates[0].attempt_id))
        assert attempt_work.status is RuntimeWorkStatus.OUTSTANDING
        assert attempt_work.next_retry_at is not None
    restarted = build_runtime_application(
        load_runtime_config(config_path), project_root=root,
        config_path=config_path)
    try:
        restarted.run(RuntimeWorkerMode.RUN_UNTIL_IDLE)
    finally:
        restarted.close()
    with SqliteRuntimeWorkStore(root / "runtime" / "runtime.sqlite3") as work:
        assert work.get(attempt_work.work_id) == attempt_work
    assert len(CandidateDStore(root / "runtime" / "d.sqlite3").recovery().value.failures) == 1
    assert all(client.closed for client in clients)


def test_cli_startup_failure_closes_already_opened_rca_authorities(
    tmp_path, monkeypatch
):
    root = tmp_path / "cli-failure"
    config_path = _cli_config(root)
    closed = []
    for name in ("SqliteRcaStore", "SqliteEvidenceStore", "SqliteKnowledgeStore"):
        cls = getattr(__import__("runtime_orchestration.cli", fromlist=[name]), name)
        original = cls.close
        def record(self, *, _name=name, _original=original):
            closed.append(_name)
            return _original(self)
        monkeypatch.setattr(cls, "close", record)
    def fail_initialize(self):
        raise RuntimeError("deterministic D startup failure")
    monkeypatch.setattr("runtime_orchestration.cli.CandidateDStore.initialize",
                        fail_initialize)
    with pytest.raises(RuntimeError, match="deterministic D startup failure"):
        build_runtime_application(
            load_runtime_config(config_path), project_root=root,
            config_path=config_path)
    assert closed == ["SqliteKnowledgeStore", "SqliteEvidenceStore", "SqliteRcaStore"]


def test_generation_client_acquisition_preserves_d_no_request_safety(monkeypatch):
    def unavailable(_timeout):
        raise ProviderInvocationError(ProviderFailureKind.UNAVAILABLE)
    monkeypatch.setattr("runtime_orchestration.cli.create_google_generation_client",
                        unavailable)
    provider = _ClosableGenerationProvider(ProviderCapability(
        "google", "gemini-2.5-flash", "profile", "schema", "1", True), 30)
    with pytest.raises(ProviderInvocationError) as caught:
        provider.input_token_upper_bound("prompt", 30)
    assert caught.value.request_sent is False
    provider.close()
