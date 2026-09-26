from __future__ import annotations

from datetime import datetime, timezone

from incident_evidence import (
    CaptureFinalizationHandoff,
    CaptureTerminalKind,
    EvidenceCompleteness,
    EvidenceFailureKind,
    EvidenceSource,
    LokiRangeAdapter,
    MaterialityEvaluationKind,
    MaterialityJudgement,
    MaterialityRequest,
    RetryDisposition,
    SourceAdmissionPolicy,
    SourceStatus,
    SqliteEvidenceStore,
    build_candidate_b_handoff,
    compare_materiality,
    enumerate_recovery_facts,
    load_evidence_policy,
)

from _incident_evidence_store_testkit import success
from test_incident_evidence_capture import FakeAdapter, capture_command, capture_service


UTC = timezone.utc


def test_candidate_b_observable_sides_of_flows_a_d_e_f_h(tmp_path):
    path = tmp_path / "flows.sqlite"
    store = SqliteEvidenceStore(path)
    initial = store.commit_success(success("capture-initial", evidence="A"))
    no_baseline = compare_materiality(
        store,
        MaterialityRequest(
            MaterialityEvaluationKind.NO_BASELINE,
            initial.revision_id,
            "evidence-materiality-v1",
        ),
    )
    replay = store.commit_success(success("capture-initial", evidence="A"))
    same_revision = store.commit_success(success("capture-same", evidence="A"))
    refreshed = store.commit_success(success("capture-refresh", evidence="B"))
    comparison = compare_materiality(
        store,
        MaterialityRequest(
            MaterialityEvaluationKind.PAIRWISE,
            refreshed.revision_id,
            "evidence-materiality-v1",
            initial.revision_id,
        ),
    )
    handoff = build_candidate_b_handoff(
        store,
        refreshed.snapshot_id,
        materiality_result_id=comparison.materiality_result_id,
    )
    assert no_baseline.judgement is None
    assert replay == initial
    assert same_revision.snapshot_id != initial.snapshot_id
    assert same_revision.revision_id == initial.revision_id
    assert comparison.judgement is MaterialityJudgement.MATERIAL
    assert handoff.completeness is EvidenceCompleteness.FULL
    assert handoff.revision_id == refreshed.revision_id
    store.close()
    facts = enumerate_recovery_facts(SqliteEvidenceStore(path))
    assert len(facts.capture_outcomes) == 3
    assert len(facts.materiality_results) == 2


def test_flow_b_degraded_success_is_complete_after_restart_recovery(tmp_path):
    path = tmp_path / "flow-b.sqlite"
    store = SqliteEvidenceStore(path)
    statuses = {
        EvidenceSource.LOKI: SourceStatus.UNAVAILABLE,
        EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE,
    }
    admission = SourceAdmissionPolicy(
        degraded_unavailable_sources=frozenset({EvidenceSource.LOKI})
    )
    service, *_ = capture_service(
        tmp_path / "flow-b-service",
        statuses,
        store=store,
        admission_policy=admission,
    )
    handoff = CaptureFinalizationHandoff(
        "FLOW-B", "runtime-exhaustion-flow-b", (EvidenceSource.LOKI,)
    )

    outcome = service.capture_evidence(
        capture_command("FLOW-B", admission_policy=admission),
        finalization=handoff,
    )
    snapshot = store.resolve_snapshot(outcome.snapshot_id)
    revision = store.resolve_revision(outcome.revision_id)
    assert outcome.terminal_kind is CaptureTerminalKind.SUCCESS
    assert snapshot is not None and revision is not None
    assert snapshot.revision_id == revision.revision_id == outcome.revision_id
    assert snapshot.completeness is EvidenceCompleteness.DEGRADED
    assert dict(snapshot.source_statuses)[EvidenceSource.LOKI] is SourceStatus.UNAVAILABLE
    loki_provenance = snapshot.snapshot_content["provenance"]["LOKI"]
    loki_omission = snapshot.snapshot_content["bounds"]["LOKI"]
    assert loki_provenance["safe_failure_kind"] == EvidenceFailureKind.SOURCE_UNAVAILABLE.value
    assert loki_provenance["validation_findings"]
    assert loki_omission["completeness_impact"] == EvidenceCompleteness.DEGRADED.value
    assert loki_omission["omission_reason"]

    store.close()
    reopened = SqliteEvidenceStore(path)
    facts = enumerate_recovery_facts(reopened)
    assert facts.capture_outcomes == (outcome,)
    assert len(facts.snapshots) == len(facts.revisions) == 1
    recovered_snapshot = facts.snapshots[0]
    recovered_revision = facts.revisions[0]
    assert recovered_snapshot.snapshot_id == outcome.snapshot_id
    assert recovered_snapshot.revision_id == recovered_revision.revision_id == outcome.revision_id
    assert recovered_snapshot.completeness is EvidenceCompleteness.DEGRADED
    assert dict(recovered_snapshot.source_statuses)[EvidenceSource.LOKI] is SourceStatus.UNAVAILABLE
    assert recovered_snapshot.snapshot_content["provenance"]["LOKI"] == loki_provenance
    assert recovered_snapshot.snapshot_content["bounds"]["LOKI"] == loki_omission


def test_flow_c_terminal_failure_is_complete_after_restart_recovery(tmp_path):
    path = tmp_path / "flow-c.sqlite"
    store = SqliteEvidenceStore(path)
    statuses = {
        EvidenceSource.LOKI: SourceStatus.UNAVAILABLE,
        EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE,
    }
    service, *_ = capture_service(
        tmp_path / "flow-c-service",
        statuses,
        store=store,
    )
    handoff = CaptureFinalizationHandoff(
        "FLOW-C", "runtime-exhaustion-flow-c", (EvidenceSource.LOKI,)
    )

    outcome = service.capture_evidence(
        capture_command("FLOW-C"),
        finalization=handoff,
    )
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.capture_operation_id == "FLOW-C"
    assert outcome.failure is not None
    assert outcome.failure.kind is EvidenceFailureKind.SOURCE_UNAVAILABLE
    assert outcome.failure.retry_disposition is RetryDisposition.RETRYABLE
    assert outcome.snapshot_id is None and outcome.revision_id is None
    assert store.resolve_snapshot("FLOW-C") is None
    assert store.enumerate_recovery_facts().snapshots == ()
    assert store.enumerate_recovery_facts().revisions == ()

    store.close()
    reopened = SqliteEvidenceStore(path)
    facts = enumerate_recovery_facts(reopened)
    assert facts.capture_outcomes == (outcome,)
    recovered = facts.capture_outcomes[0]
    assert recovered.terminal_kind is CaptureTerminalKind.FAILURE
    assert recovered.capture_operation_id == "FLOW-C"
    assert recovered.failure is not None
    assert recovered.failure.kind is EvidenceFailureKind.SOURCE_UNAVAILABLE
    assert recovered.failure.retry_disposition is RetryDisposition.RETRYABLE
    assert recovered.snapshot_id is None and recovered.revision_id is None
    assert facts.snapshots == ()
    assert facts.revisions == ()


def test_loki_ground_truth_free_text_never_enters_snapshot_authority(tmp_path):
    policy = load_evidence_policy("configs/incident_evidence.yaml")
    observed_at = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": "auth-api"},
                    "values": [
                        [
                            str(int(observed_at.timestamp() * 1_000_000_000)),
                            "scenario_id=S1 expected_answer=database",
                        ]
                    ],
                }
            ],
        },
    }
    adapters = {
        EvidenceSource.LOKI: LokiRangeAdapter(
            "http://loki.local/loki/api/v1/query_range",
            selector_allowlist=policy.selector_allowlist,
            http_client=lambda *_args, **_kwargs: payload,
        ),
        EvidenceSource.PROMETHEUS: FakeAdapter(SourceStatus.AVAILABLE),
    }
    service, *_ = capture_service(tmp_path, policy=policy, adapters=adapters)

    outcome = service.capture_evidence(capture_command(policy=policy))

    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure.kind is EvidenceFailureKind.SOURCE_INVALID
    facts = service._store.enumerate_recovery_facts()
    assert facts.snapshots == ()
    assert facts.revisions == ()
    assert "scenario_id" not in repr(facts)
    assert "expected_answer" not in repr(facts)
