from dataclasses import replace

import pytest

from incident_evidence import (
    CaptureFinalizationHandoff,
    CaptureTerminalKind,
    EvidenceCompleteness,
    EvidenceDomainError,
    EvidenceFailureKind,
    EvidenceSource,
    NonTerminalInvocationFailure,
    SourceAdmissionPolicy,
    BoundsOmissionFacts,
    CollectionProvenance,
    LokiSourceResult,
    SourceCollectionSummary,
    SourceRequestPolicy,
    SourceStatus,
)
from test_incident_evidence_capture import capture_command, capture_service
from test_incident_evidence_capture import StaticEventReader, StaticIncidentReader, FakeAdapter
from incident_evidence import (
    EvidenceCaptureService,
    LokiRangeAdapter,
    SqliteEvidenceStore,
    load_evidence_policy,
)
from test_incident_evidence_trusted_core import event


def test_unavailable_is_retryable_non_terminal_without_snapshot(tmp_path):
    statuses = {EvidenceSource.LOKI: SourceStatus.UNAVAILABLE, EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE}
    service, *_ = capture_service(tmp_path, statuses)
    result = service.capture_evidence(capture_command())
    assert isinstance(result, NonTerminalInvocationFailure)
    assert result.failure.kind is EvidenceFailureKind.SOURCE_UNAVAILABLE
    assert service.read_capture_outcome("CAP-1") is None
    assert service._store.enumerate_recovery_facts().snapshots == ()


def test_authorized_exhaustion_can_commit_policy_permitted_degraded_success(tmp_path):
    statuses = {EvidenceSource.LOKI: SourceStatus.UNAVAILABLE, EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE}
    policy = SourceAdmissionPolicy(degraded_unavailable_sources=frozenset({EvidenceSource.LOKI}))
    service, *_ = capture_service(tmp_path, statuses, admission_policy=policy)
    handoff = CaptureFinalizationHandoff("CAP-1", "runtime-exhaustion-1", (EvidenceSource.LOKI,))
    outcome = service.capture_evidence(
        capture_command(admission_policy=policy), finalization=handoff
    )
    assert outcome.terminal_kind is CaptureTerminalKind.SUCCESS
    assert service.resolve_snapshot(outcome.snapshot_id).completeness is EvidenceCompleteness.DEGRADED


def test_authorized_exhaustion_commits_failure_when_policy_forbids_degradation(tmp_path):
    statuses = {EvidenceSource.LOKI: SourceStatus.UNAVAILABLE, EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE}
    service, *_ = capture_service(tmp_path, statuses)
    handoff = CaptureFinalizationHandoff("CAP-1", "runtime-exhaustion-1", (EvidenceSource.LOKI,))
    outcome = service.capture_evidence(capture_command(), finalization=handoff)
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure.kind is EvidenceFailureKind.SOURCE_UNAVAILABLE
    assert service._store.enumerate_recovery_facts().snapshots == ()


def test_isolated_invalid_requires_explicit_policy(tmp_path):
    statuses = {EvidenceSource.LOKI: SourceStatus.INVALID, EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE}
    denied, *_ = capture_service(tmp_path / "denied", statuses)
    assert denied.capture_evidence(capture_command()).terminal_kind is CaptureTerminalKind.FAILURE
    allowed_policy = SourceAdmissionPolicy(degraded_invalid_sources=frozenset({EvidenceSource.LOKI}))
    class SafelyIsolatedInvalid:
        def collect(self, request):
            bounds = BoundsOmissionFacts(
                request.bounds_policy_identity,
                0, 0, 0, None,
                False, None, False, None, False, False, None, None, False, None,
                EvidenceCompleteness.DEGRADED,
            )
            summary = SourceCollectionSummary(
                request.source,
                SourceStatus.INVALID,
                0,
                request.query_provenance,
                CollectionProvenance(
                    request.source,
                    request.adapter_contract_version,
                    request.logical_window,
                    1,
                    True,
                    SourceStatus.INVALID,
                ),
                bounds,
                ("optional source rejected without payload",),
                EvidenceFailureKind.SOURCE_INVALID,
            )
            return LokiSourceResult(summary, ())

    adapters = {
        EvidenceSource.LOKI: SafelyIsolatedInvalid(),
        EvidenceSource.PROMETHEUS: FakeAdapter(SourceStatus.AVAILABLE),
    }
    allowed, *_ = capture_service(
        tmp_path / "allowed",
        admission_policy=allowed_policy,
        adapters=adapters,
    )
    outcome = allowed.capture_evidence(
        capture_command(admission_policy=allowed_policy), finalization=None
    )
    assert outcome.terminal_kind is CaptureTerminalKind.SUCCESS
    assert allowed.resolve_snapshot(outcome.snapshot_id).completeness is EvidenceCompleteness.DEGRADED


def test_trusted_core_contradiction_is_terminal_failure_never_degraded(tmp_path):
    service = EvidenceCaptureService(
        store=SqliteEvidenceStore(tmp_path / "trusted.sqlite"),
        incident_reader=StaticIncidentReader(),
        event_reader=StaticEventReader([event(), event()]),
        policy=load_evidence_policy("configs/incident_evidence.yaml"),
        adapters={source: FakeAdapter(SourceStatus.AVAILABLE) for source in EvidenceSource},
        admission_policy=SourceAdmissionPolicy(
            degraded_unavailable_sources=frozenset(EvidenceSource),
            degraded_invalid_sources=frozenset(EvidenceSource),
        ),
    )
    admission = SourceAdmissionPolicy(
        degraded_unavailable_sources=frozenset(EvidenceSource),
        degraded_invalid_sources=frozenset(EvidenceSource),
    )
    outcome = service.capture_evidence(capture_command(admission_policy=admission))
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure.kind is EvidenceFailureKind.DUPLICATE_EVENT_ID
    assert service._store.enumerate_recovery_facts().snapshots == ()


def _real_loki_service(tmp_path, payload, *, policy=None):
    policy = policy or load_evidence_policy("configs/incident_evidence.yaml")
    admission = SourceAdmissionPolicy(
        degraded_invalid_sources=frozenset({EvidenceSource.LOKI})
    )
    adapters = {
        EvidenceSource.LOKI: LokiRangeAdapter(
            "http://loki.local/loki/api/v1/query_range",
            selector_allowlist=policy.selector_allowlist,
            http_client=lambda *_args, **_kwargs: payload,
        ),
        EvidenceSource.PROMETHEUS: FakeAdapter(SourceStatus.AVAILABLE),
    }
    service, *_ = capture_service(
        tmp_path,
        policy=policy,
        admission_policy=admission,
        adapters=adapters,
    )
    command = capture_command(policy=policy, admission_policy=admission)
    return service, command


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "success", "data": {"resultType": "vector", "result": []}},
        {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [{"stream": {}, "values": [["0", "outside"]]}],
            },
        },
        {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [{
                    "stream": {},
                    "values": [["1790038680000000000", "authorization=secret"]],
                }],
            },
        },
    ],
    ids=["malformed", "out-of-window", "unsafe-content"],
)
def test_real_adapter_untrusted_invalid_is_terminal_failure(tmp_path, payload):
    service, command = _real_loki_service(tmp_path, payload)
    outcome = service.capture_evidence(command)
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure.kind is EvidenceFailureKind.SOURCE_INVALID
    assert service._store.enumerate_recovery_facts().snapshots == ()


def test_real_adapter_incomplete_truncation_is_terminal_failure(tmp_path):
    base = load_evidence_policy("configs/incident_evidence.yaml")
    policy = replace(base, bounds=replace(base.bounds, max_records_per_source=1))
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{
                "stream": {},
                "values": [
                    ["1790038680000000000", "one"],
                    ["1790038681000000000", "two"],
                ],
            }],
        },
    }
    service, command = _real_loki_service(tmp_path, payload, policy=policy)
    outcome = service.capture_evidence(command)
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure.kind is EvidenceFailureKind.SOURCE_INVALID
    assert service._store.enumerate_recovery_facts().snapshots == ()


def test_real_adapter_unsafe_query_is_terminal_failure(tmp_path):
    request_policies = {
        EvidenceSource.LOKI: SourceRequestPolicy(
            "not-streams",
            "unsafe-query-test-v1",
            "logql-v1",
            "loki-adapter-v1",
            "source-request-budget-v1",
            2.0,
        ),
        EvidenceSource.PROMETHEUS: SourceRequestPolicy(
            "up",
            "prometheus-range-query-v1",
            "promql-v1",
            "prometheus-adapter-v1",
            "source-request-budget-v1",
            2.0,
            15.0,
        ),
    }
    policy = load_evidence_policy("configs/incident_evidence.yaml")
    admission = SourceAdmissionPolicy(
        degraded_invalid_sources=frozenset({EvidenceSource.LOKI})
    )
    adapters = {
        EvidenceSource.LOKI: LokiRangeAdapter(
            "http://loki.local/loki/api/v1/query_range",
            selector_allowlist=policy.selector_allowlist,
            http_client=lambda *_args, **_kwargs: pytest.fail("unsafe query must not be sent"),
        ),
        EvidenceSource.PROMETHEUS: FakeAdapter(SourceStatus.AVAILABLE),
    }
    service, *_ = capture_service(
        tmp_path,
        policy=policy,
        admission_policy=admission,
        request_policies=request_policies,
        adapters=adapters,
    )
    command = capture_command(
        policy=policy,
        admission_policy=admission,
        request_policies=request_policies,
    )
    outcome = service.capture_evidence(command)
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure.kind is EvidenceFailureKind.SOURCE_INVALID
