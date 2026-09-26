from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from incident_evidence import (
    BoundsOmissionFacts,
    CaptureCommand,
    CaptureTerminalKind,
    CollectionProvenance,
    EvidenceCaptureService,
    EvidenceCompleteness,
    EvidenceFailureKind,
    EvidenceSource,
    LokiLogRecord,
    LokiSourceResult,
    PrometheusSampleRecord,
    PrometheusSourceResult,
    SourceCollectionSummary,
    SourceStatus,
    SqliteEvidenceStore,
    SourceAdmissionPolicy,
    DEFAULT_SOURCE_REQUEST_POLICIES,
    effective_capture_config_identity,
    load_evidence_policy,
)

from test_incident_evidence_trusted_core import event, incident


UTC = timezone.utc


class StaticIncidentReader:
    def __init__(self, value=None):
        self.value = value if value is not None else incident()
        self.calls = 0

    def get_incident(self, _incident_id):
        self.calls += 1
        return self.value


class StaticEventReader:
    def __init__(self, values=None):
        self.values = values if values is not None else [event()]
        self.calls = 0

    def read_all_authoritative(self):
        self.calls += 1
        return deepcopy(self.values)


class FakeAdapter:
    def __init__(self, status, *, marker="same"):
        self.status = status
        self.marker = marker
        self.calls = 0

    def collect(self, request):
        self.calls += 1
        count = 1 if self.status is SourceStatus.AVAILABLE else 0
        failed = self.status in {SourceStatus.UNAVAILABLE, SourceStatus.INVALID}
        bounds = BoundsOmissionFacts(
            request.bounds_policy_identity,
            None if failed else count,
            count,
            None if failed else 0,
            "source result excluded" if failed else None,
            False, None, False, None, False, False, None, None, False, None,
            EvidenceCompleteness.DEGRADED if failed else EvidenceCompleteness.FULL,
        )
        summary = SourceCollectionSummary(
            request.source,
            self.status,
            count,
            request.query_provenance,
            CollectionProvenance(
                request.source,
                request.adapter_contract_version,
                request.logical_window,
                None if failed else 1,
                not failed,
                self.status,
            ),
            bounds,
            ("isolated source failure",) if failed else (),
            (
                EvidenceFailureKind.SOURCE_UNAVAILABLE
                if self.status is SourceStatus.UNAVAILABLE
                else EvidenceFailureKind.SOURCE_INVALID if self.status is SourceStatus.INVALID else None
            ),
        )
        timestamp = request.logical_window.start
        if request.source is EvidenceSource.LOKI:
            records = () if not count else (
                LokiLogRecord(timestamp, int(timestamp.timestamp() * 1_000_000_000), (), self.marker),
            )
            return LokiSourceResult(summary, records)
        records = () if not count else (
            PrometheusSampleRecord(timestamp, str(int(timestamp.timestamp())), (), 1.0, self.marker),
        )
        return PrometheusSourceResult(summary, records)


def capture_command(
    operation_id="CAP-1",
    *,
    snapshot_at=None,
    policy=None,
    admission_policy=None,
    request_policies=None,
):
    policy = policy or load_evidence_policy("configs/incident_evidence.yaml")
    admission_policy = admission_policy or SourceAdmissionPolicy()
    request_policies = request_policies or DEFAULT_SOURCE_REQUEST_POLICIES
    return CaptureCommand(
        operation_id,
        "INC-1",
        snapshot_at or datetime(2026, 9, 22, 1, 4, tzinfo=UTC),
        policy.capture_contract_version,
        policy.canonicalization_version,
        policy.source_policy_version,
        policy.bounds_policy_version,
        effective_capture_config_identity(policy, admission_policy, request_policies),
    )


def capture_service(
    tmp_path,
    statuses=None,
    *,
    store=None,
    admission_policy=None,
    request_policies=None,
    policy=None,
    adapters=None,
    marker="same",
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    statuses = statuses or {
        EvidenceSource.LOKI: SourceStatus.AVAILABLE,
        EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE,
    }
    incident_reader = StaticIncidentReader()
    event_reader = StaticEventReader()
    adapters = adapters or {
        source: FakeAdapter(status, marker=marker) for source, status in statuses.items()
    }
    policy = policy or load_evidence_policy("configs/incident_evidence.yaml")
    service = EvidenceCaptureService(
        store=store or SqliteEvidenceStore(tmp_path / "evidence.sqlite"),
        incident_reader=incident_reader,
        event_reader=event_reader,
        policy=policy,
        adapters=adapters,
        admission_policy=admission_policy,
        request_policies=request_policies,
    )
    return service, incident_reader, event_reader, adapters


def test_full_success_composes_trusted_core_sources_and_complete_readback(tmp_path):
    service, incident_reader, event_reader, adapters = capture_service(tmp_path)
    outcome = service.capture_evidence(capture_command())

    assert outcome.terminal_kind is CaptureTerminalKind.SUCCESS
    snapshot = service.resolve_snapshot(outcome.snapshot_id)
    revision = service.resolve_revision(outcome.revision_id)
    assert snapshot.completeness is EvidenceCompleteness.FULL
    assert snapshot.snapshot_content["semantic_evidence"] == revision.semantic_content
    assert incident_reader.calls == 2 and event_reader.calls == 1
    assert all(adapter.calls == 1 for adapter in adapters.values())


def test_empty_sources_are_full_success_not_failure(tmp_path):
    statuses = {source: SourceStatus.EMPTY for source in EvidenceSource}
    service, *_ = capture_service(tmp_path, statuses)
    outcome = service.capture_evidence(capture_command())
    assert outcome.terminal_kind is CaptureTerminalKind.SUCCESS
    assert service.resolve_snapshot(outcome.snapshot_id).completeness is EvidenceCompleteness.FULL
