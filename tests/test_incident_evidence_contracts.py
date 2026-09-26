from datetime import datetime, timedelta, timezone

import pytest

from incident_evidence import (
    BoundsOmissionFacts,
    CaptureCommand,
    CaptureFailure,
    CaptureTerminalKind,
    CollectionProvenance,
    EvidenceCompleteness,
    EvidenceDomainError,
    EvidenceFailureKind,
    EvidenceSource,
    MaterialityEvaluationKind,
    MaterialityJudgement,
    MaterialityRequest,
    MaterialityResult,
    QueryProvenance,
    RetryDisposition,
    SelectorFact,
    SourceCollectionSummary,
    SourceStatus,
    derive_collection_windows,
    derive_episode,
)
from incident_evidence.time_semantics import LogicalWindow


UTC = timezone.utc


def command(**overrides):
    values = {
        "capture_operation_id": "capture-1",
        "incident_id": "INC-1",
        "snapshot_at": datetime(2026, 9, 21, 2, 5, tzinfo=UTC),
        "capture_contract_version": "spec013-v1",
        "canonicalization_version": "canonical-v1",
        "source_policy_version": "sources-v1",
        "bounds_policy_version": "bounds-v1",
        "config_identity": "config-v1",
    }
    values.update(overrides)
    return CaptureCommand(**values)


def bounds(**overrides):
    values = {
        "bounds_policy_version": "bounds-v1",
        "observed_candidate_count": 2,
        "included_count": 2,
        "omitted_count": 0,
        "omission_reason": None,
        "sampling_applied": False,
        "sampling_policy_identity": None,
        "aggregation_applied": False,
        "aggregation_rule_identity": None,
        "aggregation_lossy": False,
        "dedup_applied": False,
        "dedup_input_count": None,
        "dedup_output_count": None,
        "truncation_applied": False,
        "truncation_reason": None,
        "completeness_impact": EvidenceCompleteness.FULL,
    }
    values.update(overrides)
    return BoundsOmissionFacts(**values)


def test_closed_sets_match_spec_013():
    assert {item.value for item in CaptureTerminalKind} == {"SUCCESS", "FAILURE"}
    assert {item.value for item in EvidenceSource} == {"LOKI", "PROMETHEUS"}
    assert {item.value for item in SourceStatus} == {
        "AVAILABLE",
        "EMPTY",
        "UNAVAILABLE",
        "INVALID",
    }
    assert {item.value for item in EvidenceCompleteness} == {"FULL", "DEGRADED"}
    assert {item.value for item in MaterialityJudgement} == {
        "SAME",
        "NON_MATERIAL",
        "MATERIAL",
        "REPAIR_REQUIRED",
    }
    assert {item.value for item in MaterialityEvaluationKind} == {
        "PAIRWISE",
        "NO_BASELINE",
    }
    assert {item.value for item in RetryDisposition} == {
        "RETRYABLE",
        "NON_RETRYABLE",
        "REPAIR_REQUIRED",
    }
    with pytest.raises(ValueError):
        SourceStatus("UNKNOWN")


def test_capture_command_is_immutable_and_canonicalizes_absolute_time():
    local = timezone(timedelta(hours=8))
    value = command(snapshot_at=datetime(2026, 9, 21, 10, 5, tzinfo=local))

    assert value.snapshot_at == datetime(2026, 9, 21, 2, 5, tzinfo=UTC)
    with pytest.raises((AttributeError, TypeError)):
        value.incident_id = "INC-2"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capture_operation_id", ""),
        ("incident_id", " INC-1"),
        ("capture_contract_version", None),
        ("canonicalization_version", ""),
        ("source_policy_version", "sources-v1 "),
        ("bounds_policy_version", 1),
        ("config_identity", ""),
    ],
)
def test_capture_command_rejects_invalid_identity_fields(field, value):
    with pytest.raises(EvidenceDomainError) as captured:
        command(**{field: value})

    assert captured.value.kind is EvidenceFailureKind.INVALID_CAPTURE_COMMAND
    assert captured.value.retry_disposition is RetryDisposition.NON_RETRYABLE
    assert captured.value.field_path == field


def test_capture_command_rejects_naive_and_out_of_range_time():
    with pytest.raises(EvidenceDomainError) as naive:
        command(snapshot_at=datetime(2026, 9, 21, 2, 5))
    assert naive.value.kind is EvidenceFailureKind.INVALID_CAPTURE_COMMAND

    with pytest.raises(EvidenceDomainError) as out_of_range:
        command(snapshot_at=datetime.min.replace(tzinfo=timezone(timedelta(hours=14))))
    assert out_of_range.value.kind is EvidenceFailureKind.INVALID_CAPTURE_COMMAND


def test_episode_and_default_windows_follow_spec_013_section_8():
    episode = derive_episode(
        [
            datetime(2026, 9, 21, 2, 4, tzinfo=UTC),
            datetime(2026, 9, 21, 2, 0, tzinfo=UTC),
        ]
    )
    windows = derive_collection_windows(
        episode, datetime(2026, 9, 21, 2, 3, tzinfo=UTC)
    )

    assert episode.start == datetime(2026, 9, 21, 2, 0, tzinfo=UTC)
    assert episode.end == datetime(2026, 9, 21, 2, 4, tzinfo=UTC)
    assert windows.logs.start == datetime(2026, 9, 21, 1, 58, tzinfo=UTC)
    assert windows.metrics.start == datetime(2026, 9, 21, 1, 55, tzinfo=UTC)
    assert windows.logs.end == windows.metrics.end == datetime(
        2026, 9, 21, 2, 3, tzinfo=UTC
    )
    assert windows.default_post_context_boundary == datetime(
        2026, 9, 21, 2, 6, tzinfo=UTC
    )


def test_invalid_event_time_uses_required_event_failure_family():
    with pytest.raises(EvidenceDomainError) as captured:
        derive_episode([datetime(2026, 9, 21, 2, 0)])
    assert captured.value.kind is EvidenceFailureKind.INVALID_REQUIRED_EVENT


def test_logical_window_is_closed_and_rejects_reverse_range():
    instant = datetime(2026, 9, 21, tzinfo=UTC)
    assert LogicalWindow(instant, instant).start == instant

    with pytest.raises(EvidenceDomainError) as captured:
        LogicalWindow(instant, instant - timedelta(microseconds=1))
    assert captured.value.kind is EvidenceFailureKind.INVALID_CAPTURE_COMMAND


def test_bounds_require_explicit_omission_and_loss_facts():
    facts = bounds(
        observed_candidate_count=5,
        included_count=3,
        omitted_count=2,
        omission_reason="record-count limit",
        truncation_applied=True,
        truncation_reason="max records reached",
        completeness_impact=EvidenceCompleteness.DEGRADED,
    )
    assert facts.omitted_count == 2

    with pytest.raises(ValueError, match="explicit reason"):
        bounds(observed_candidate_count=3, included_count=2, omitted_count=1)
    with pytest.raises(ValueError, match="unknown omission magnitude"):
        bounds(
            observed_candidate_count=None,
            omitted_count=None,
            omission_reason="source unavailable",
        )
    with pytest.raises(ValueError, match="semantic omission"):
        bounds(
            observed_candidate_count=2,
            included_count=1,
            omitted_count=1,
            omission_reason="dropped",
            completeness_impact=EvidenceCompleteness.FULL,
        )

    lossless_dedup = bounds(
        observed_candidate_count=2,
        included_count=1,
        omitted_count=1,
        omission_reason="canonical duplicate",
        dedup_applied=True,
        dedup_input_count=2,
        dedup_output_count=1,
        completeness_impact=EvidenceCompleteness.FULL,
    )
    assert lossless_dedup.completeness_impact is EvidenceCompleteness.FULL


def test_source_summary_preserves_typed_status_and_provenance():
    window = LogicalWindow(
        datetime(2026, 9, 21, 2, 0, tzinfo=UTC),
        datetime(2026, 9, 21, 2, 1, tzinfo=UTC),
    )
    selector = SelectorFact(
        EvidenceSource.LOKI,
        "service_name",
        "payments",
        "payments",
        "selectors-v1",
    )
    query = QueryProvenance(
        EvidenceSource.LOKI,
        "loki-query-1",
        "logql-v1",
        window,
        (selector,),
        "loki-adapter-v1",
        "bounds-v1",
        "timeout-v1",
    )
    collection = CollectionProvenance(
        EvidenceSource.LOKI,
        "loki-adapter-v1",
        window,
        1,
        True,
        SourceStatus.AVAILABLE,
    )
    summary = SourceCollectionSummary(
        EvidenceSource.LOKI,
        SourceStatus.AVAILABLE,
        2,
        query,
        collection,
        bounds(),
    )
    assert summary.status is SourceStatus.AVAILABLE

    with pytest.raises(ValueError, match="AVAILABLE requires"):
        SourceCollectionSummary(
            EvidenceSource.LOKI,
            SourceStatus.AVAILABLE,
            0,
            query,
            collection,
            bounds(
                observed_candidate_count=0,
                included_count=0,
                omitted_count=0,
            ),
        )
    with pytest.raises(ValueError, match="DEGRADED"):
        bounds(
            observed_candidate_count=2,
            included_count=1,
            omitted_count=1,
            omission_reason="sampled",
            sampling_applied=True,
            sampling_policy_identity="sample-v1",
        )


def test_materiality_contract_has_explicit_pairwise_and_no_baseline_paths_only():
    pairwise = MaterialityRequest(
        MaterialityEvaluationKind.PAIRWISE,
        candidate_revision_id="revision-2",
        materiality_rule_version="rule-v1",
        baseline_revision_id="revision-1",
    )
    result = MaterialityResult(
        "materiality-1", pairwise, MaterialityJudgement.SAME, ("same semantics",)
    )
    assert result.judgement is MaterialityJudgement.SAME

    initial = MaterialityRequest(
        MaterialityEvaluationKind.NO_BASELINE,
        candidate_revision_id="revision-1",
        materiality_rule_version="rule-v1",
    )
    assert MaterialityResult("materiality-2", initial, None, ("initial",)).judgement is None

    with pytest.raises(ValueError, match="must not carry"):
        MaterialityRequest(
            MaterialityEvaluationKind.NO_BASELINE,
            candidate_revision_id="revision-1",
            materiality_rule_version="rule-v1",
            baseline_revision_id="revision-0",
        )
    with pytest.raises(TypeError, match="requires"):
        MaterialityResult("materiality-3", pairwise, None, ("not judged",))


@pytest.mark.parametrize(
    "unsafe_summary",
    [
        "Authorization: Bearer top-secret",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "API key: top-secret",
        "api_key=top-secret",
        "apikey=top-secret",
        "access_token=abc123",
        "refresh_token=abc123",
        "client_secret=abc123",
        "password: hunter2",
        "token=abc123",
        "secret: value",
        "request failed at https://alice:password@example.test/query",
        "request failed at https://token@example.test/path",
    ],
)
def test_capture_failure_rejects_secret_bearing_safe_summary(unsafe_summary):
    with pytest.raises(EvidenceDomainError) as captured:
        CaptureFailure(
            "capture-1",
            EvidenceFailureKind.SOURCE_INVALID,
            RetryDisposition.NON_RETRYABLE,
            unsafe_summary,
        )
    assert captured.value.kind is EvidenceFailureKind.UNSAFE_EVIDENCE_CONTENT
    assert captured.value.field_path == "safe_summary"


def test_capture_failure_accepts_bounded_non_secret_summary():
    failure = CaptureFailure(
        "capture-1",
        EvidenceFailureKind.SOURCE_INVALID,
        RetryDisposition.NON_RETRYABLE,
        "Loki response shape was invalid",
    )
    assert failure.safe_summary == "Loki response shape was invalid"
