"""Phase 5 composition of trusted-core admission, source capture, and persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Mapping, Protocol

from .adapters import LokiSourceResult, PrometheusSourceResult
from .contracts import (
    CaptureCommand,
    CaptureFailure,
    CaptureFinalizationHandoff,
    CaptureSuccess,
    CaptureTerminalOutcome,
    EvidenceCompleteness,
    EvidenceRevision,
    EvidenceSnapshot,
    EvidenceSource,
    NonTerminalInvocationFailure,
    SourceAdapterRequest,
    SourceCollectionSummary,
    SourceStatus,
)
from .errors import (
    EvidenceDomainError,
    EvidenceFailureKind,
    RetryDisposition,
)
from .identity import canonical_json, capture_command_semantic_identity, semantic_identity
from .policy import EvidencePolicy, SourceAdmissionPolicy
from .sqlite_store import SqliteEvidenceStore
from .trusted_core import AuthoritativeEventReader, IncidentReader, admit_capture_plan


class SourceAdapter(Protocol):
    def collect(self, request: SourceAdapterRequest) -> object: ...


@dataclass(frozen=True, slots=True)
class SourceRequestPolicy:
    """Non-secret, versioned request facts used to construct adapter requests."""

    query_resource: str
    query_semantic_identity: str
    query_version: str
    adapter_contract_version: str
    request_budget_identity: str
    timeout_seconds: float
    query_resolution_seconds: float | None = None


DEFAULT_SOURCE_REQUEST_POLICIES: Mapping[EvidenceSource, SourceRequestPolicy] = {
    EvidenceSource.LOKI: SourceRequestPolicy(
        "streams", "loki-stream-query-v1", "logql-v1", "loki-adapter-v1",
        "source-request-budget-v1", 2.0,
    ),
    EvidenceSource.PROMETHEUS: SourceRequestPolicy(
        "up", "prometheus-range-query-v1", "promql-v1", "prometheus-adapter-v1",
        "source-request-budget-v1", 2.0, 15.0,
    ),
}


def effective_capture_config_identity(
    policy: EvidencePolicy,
    admission_policy: SourceAdmissionPolicy,
    request_policies: Mapping[EvidenceSource, SourceRequestPolicy],
) -> str:
    """Bind every effective evidence-affecting policy to CaptureCommand config identity."""
    if not isinstance(policy, EvidencePolicy):
        raise TypeError("policy must be an EvidencePolicy")
    if not isinstance(admission_policy, SourceAdmissionPolicy):
        raise TypeError("admission_policy must be a SourceAdmissionPolicy")
    requests = dict(request_policies)
    if set(requests) != set(admission_policy.configured_sources):
        raise ValueError("request policies must exactly match configured sources")
    projection = {
        "base_policy": {
            "config_version": policy.config_version,
            "config_identity": policy.config_identity,
            "capture_contract_version": policy.capture_contract_version,
            "canonicalization_version": policy.canonicalization_version,
            "source_policy_version": policy.source_policy_version,
            "bounds_policy_version": policy.bounds_policy_version,
            "selector_policy_version": policy.selector_policy_version,
            "redaction_policy_version": policy.redaction_policy_version,
            "windows": policy.windows,
            "selector_allowlist": {
                source.value: values for source, values in policy.selector_allowlist.items()
            },
            "bounds": policy.bounds,
        },
        "admission_policy": admission_policy,
        "request_policies": {
            source.value: requests[source]
            for source in sorted(requests, key=lambda item: item.value)
        },
    }
    return semantic_identity("spec013-effective-capture-config", projection)


class EvidenceCaptureService:
    """Stateless Phase 5 semantic service; all replay authority lives in the store."""

    def __init__(
        self,
        *,
        store: SqliteEvidenceStore,
        incident_reader: IncidentReader,
        event_reader: AuthoritativeEventReader,
        policy: EvidencePolicy,
        adapters: Mapping[EvidenceSource, SourceAdapter],
        admission_policy: SourceAdmissionPolicy | None = None,
        request_policies: Mapping[EvidenceSource, SourceRequestPolicy] | None = None,
    ) -> None:
        if not isinstance(store, SqliteEvidenceStore):
            raise TypeError("store must be a SqliteEvidenceStore")
        if not isinstance(policy, EvidencePolicy):
            raise TypeError("policy must be an EvidencePolicy")
        self._store = store
        self._incident_reader = incident_reader
        self._event_reader = event_reader
        self._policy = policy
        self._admission = admission_policy or SourceAdmissionPolicy()
        self._adapters = dict(adapters)
        self._request_policies = dict(request_policies or DEFAULT_SOURCE_REQUEST_POLICIES)
        expected = self._admission.configured_sources
        if set(self._adapters) != set(expected):
            raise ValueError("adapters must exactly match configured sources")
        if set(self._request_policies) != set(expected):
            raise ValueError("request policies must exactly match configured sources")
        self._effective_config_identity = effective_capture_config_identity(
            policy, self._admission, self._request_policies
        )
        self._effective_policy = replace(
            policy, config_identity=self._effective_config_identity
        )

    def capture_evidence(
        self,
        command: CaptureCommand,
        *,
        finalization: CaptureFinalizationHandoff | None = None,
    ) -> CaptureTerminalOutcome | NonTerminalInvocationFailure:
        if not isinstance(command, CaptureCommand):
            raise TypeError("command must be a CaptureCommand")

        # Normative first action: no policy, trusted-core, or source access precedes this read.
        existing = self._store.read_capture_outcome(command.capture_operation_id)
        command_identity = capture_command_semantic_identity(command)
        if existing is not None:
            if existing.command_semantic_identity != command_identity:
                raise EvidenceDomainError(
                    EvidenceFailureKind.CONTRADICTORY_REPLAY,
                    "capture_operation_id was reused with different command semantics",
                )
            return existing

        self._validate_finalization(command, finalization)
        if command.config_identity != self._effective_config_identity:
            return self._handle_domain_failure(
                command,
                EvidenceDomainError(
                    EvidenceFailureKind.INVALID_CAPTURE_COMMAND,
                    "CaptureCommand config identity does not match effective capture policy",
                    field_path="config_identity",
                ),
                finalization,
            )
        try:
            plan = admit_capture_plan(
                command, self._incident_reader, self._event_reader, self._effective_policy
            )
        except EvidenceDomainError as exc:
            return self._handle_domain_failure(command, exc, finalization)

        results: dict[EvidenceSource, LokiSourceResult | PrometheusSourceResult] = {}
        requests: dict[EvidenceSource, SourceAdapterRequest] = {}
        try:
            for source in sorted(self._admission.configured_sources, key=lambda item: item.value):
                request = self._source_request(plan, source)
                requests[source] = request
                result = self._adapters[source].collect(request)
                if source is EvidenceSource.LOKI and not isinstance(result, LokiSourceResult):
                    raise EvidenceDomainError(
                        EvidenceFailureKind.SOURCE_INVALID,
                        "Loki adapter returned an invalid result contract",
                    )
                if source is EvidenceSource.PROMETHEUS and not isinstance(result, PrometheusSourceResult):
                    raise EvidenceDomainError(
                        EvidenceFailureKind.SOURCE_INVALID,
                        "Prometheus adapter returned an invalid result contract",
                    )
                results[source] = result
        except EvidenceDomainError as exc:
            return self._handle_domain_failure(command, exc, finalization)
        except OSError:
            return self._handle_domain_failure(
                command,
                EvidenceDomainError(
                    EvidenceFailureKind.SOURCE_UNAVAILABLE,
                    "source adapter invocation was unavailable",
                ),
                finalization,
            )
        except (TypeError, ValueError) as exc:
            return self._handle_domain_failure(
                command,
                EvidenceDomainError(
                    EvidenceFailureKind.SOURCE_INVALID,
                    "source adapter contract validation failed",
                ),
                finalization,
            )

        summaries = {source: result.summary for source, result in results.items()}
        admission = self._classify_sources(command, summaries, requests, finalization)
        if isinstance(admission, (CaptureTerminalOutcome, NonTerminalInvocationFailure)):
            return admission
        completeness = admission

        try:
            revision_content = self._revision_content(plan, results, completeness)
            revision = EvidenceRevision.from_content(
                incident_id=command.incident_id,
                canonicalization_version=command.canonicalization_version,
                semantic_content=revision_content,
            )
            snapshot_content = self._snapshot_content(
                plan, results, revision_content, finalization
            )
            if len(canonical_json(snapshot_content).encode("utf-8")) > self._policy.bounds.max_total_bytes:
                raise EvidenceDomainError(
                    EvidenceFailureKind.SOURCE_INVALID,
                    "normalized Snapshot exceeds the configured total evidence bound",
                )
            snapshot = EvidenceSnapshot.from_content(
                command,
                revision_id=revision.revision_id,
                completeness=completeness,
                source_statuses=tuple(
                    (source, summaries[source].status)
                    for source in sorted(summaries, key=lambda item: item.value)
                ),
                snapshot_content=snapshot_content,
            )
            return self._store.commit_success(CaptureSuccess(command, snapshot, revision))
        except EvidenceDomainError as exc:
            return self._handle_domain_failure(command, exc, finalization)
        except (TypeError, ValueError):
            return self._commit_failure(
                command,
                EvidenceFailureKind.UNSAFE_EVIDENCE_CONTENT,
                RetryDisposition.NON_RETRYABLE,
                "captured evidence could not be safely normalized",
            )

    def read_capture_outcome(self, capture_operation_id: str) -> CaptureTerminalOutcome | None:
        return self._store.read_capture_outcome(capture_operation_id)

    def resolve_snapshot(self, snapshot_id: str) -> EvidenceSnapshot | None:
        return self._store.resolve_snapshot(snapshot_id)

    def resolve_revision(self, revision_id: str) -> EvidenceRevision | None:
        return self._store.resolve_revision(revision_id)

    @staticmethod
    def _validate_finalization(
        command: CaptureCommand, finalization: CaptureFinalizationHandoff | None
    ) -> None:
        if finalization is None:
            return
        if not isinstance(finalization, CaptureFinalizationHandoff):
            raise TypeError("finalization must be a CaptureFinalizationHandoff")
        if finalization.capture_operation_id != command.capture_operation_id:
            raise EvidenceDomainError(
                EvidenceFailureKind.CONTRADICTORY_REPLAY,
                "finalization handoff belongs to a different capture operation",
            )

    def _source_request(self, plan: object, source: EvidenceSource) -> SourceAdapterRequest:
        request_policy = self._request_policies[source]
        selectors = tuple(
            item.selector for item in plan.selectors if item.selector.source is source
        )
        window = plan.windows.logs if source is EvidenceSource.LOKI else plan.windows.metrics
        return SourceAdapterRequest(
            source=source,
            incident_id=plan.command.incident_id,
            logical_window=window,
            selectors=selectors,
            query_resource=request_policy.query_resource,
            query_semantic_identity=request_policy.query_semantic_identity,
            query_version=request_policy.query_version,
            adapter_contract_version=request_policy.adapter_contract_version,
            bounds_policy_identity=plan.bounds_policy_version,
            request_budget_identity=request_policy.request_budget_identity,
            timeout_seconds=request_policy.timeout_seconds,
            max_records=self._policy.bounds.max_records_per_source,
            max_content_bytes=self._policy.bounds.max_content_bytes,
            max_labels_per_record=self._policy.bounds.max_labels_per_record,
            max_label_value_characters=self._policy.bounds.max_label_value_characters,
            query_resolution_seconds=request_policy.query_resolution_seconds,
        )

    def _classify_sources(
        self,
        command: CaptureCommand,
        summaries: Mapping[EvidenceSource, SourceCollectionSummary],
        requests: Mapping[EvidenceSource, SourceAdapterRequest],
        finalization: CaptureFinalizationHandoff | None,
    ) -> EvidenceCompleteness | CaptureTerminalOutcome | NonTerminalInvocationFailure:
        unavailable = frozenset(
            source for source, summary in summaries.items()
            if summary.status is SourceStatus.UNAVAILABLE
        )
        invalid = frozenset(
            source for source, summary in summaries.items()
            if summary.status is SourceStatus.INVALID
        )
        valid = frozenset(summaries) - unavailable - invalid

        invalid_allowed = invalid <= self._admission.degraded_invalid_sources and all(
            self._invalid_is_safely_isolated(summaries[source], requests[source])
            for source in invalid
        )
        if invalid and not invalid_allowed:
            return self._commit_failure(
                command,
                EvidenceFailureKind.SOURCE_INVALID,
                RetryDisposition.NON_RETRYABLE,
                "source evidence did not satisfy terminal admission policy",
                tuple(f"{source.value}:INVALID" for source in sorted(invalid, key=lambda item: item.value)),
            )
        if unavailable and finalization is None:
            return self._non_terminal(
                command,
                EvidenceFailureKind.SOURCE_UNAVAILABLE,
                "one or more evidence sources are unavailable",
                tuple(f"{source.value}:UNAVAILABLE" for source in sorted(unavailable, key=lambda item: item.value)),
            )
        if finalization is not None and frozenset(finalization.exhausted_sources) != unavailable:
            raise EvidenceDomainError(
                EvidenceFailureKind.CONTRADICTORY_REPLAY,
                "finalization exhausted-source facts contradict collection results",
            )
        remaining_trustworthy = bool(valid) or not self._admission.require_remaining_valid_source
        unavailable_allowed = unavailable <= self._admission.degraded_unavailable_sources
        if (unavailable and not unavailable_allowed) or not remaining_trustworthy:
            kind = EvidenceFailureKind.SOURCE_INVALID if invalid else EvidenceFailureKind.SOURCE_UNAVAILABLE
            disposition = RetryDisposition.NON_RETRYABLE if invalid else RetryDisposition.RETRYABLE
            return self._commit_failure(
                command, kind, disposition,
                "source evidence did not satisfy terminal admission policy",
                tuple(
                    f"{source.value}:{summaries[source].status.value}"
                    for source in sorted(unavailable | invalid, key=lambda item: item.value)
                ),
            )
        degraded_bounds = any(
            summary.bounds.completeness_impact is EvidenceCompleteness.DEGRADED
            for summary in summaries.values()
        )
        if unavailable or invalid or degraded_bounds:
            return EvidenceCompleteness.DEGRADED
        return EvidenceCompleteness.FULL

    @staticmethod
    def _invalid_is_safely_isolated(
        summary: SourceCollectionSummary,
        request: SourceAdapterRequest,
    ) -> bool:
        """Prove isolation only from Phase 4 typed facts; text findings grant no authority."""
        bounds = summary.bounds
        collection = summary.collection_provenance
        return (
            summary.status is SourceStatus.INVALID
            and summary.safe_failure_kind is EvidenceFailureKind.SOURCE_INVALID
            and bool(summary.validation_findings)
            and summary.record_count == 0
            and summary.query_provenance == request.query_provenance
            and collection.adapter_contract_version == request.adapter_contract_version
            and collection.logical_window == request.logical_window
            and collection.continuation_complete
            and collection.page_count is not None
            and collection.page_count >= 1
            and bounds.bounds_policy_version == request.bounds_policy_identity
            and bounds.observed_candidate_count == 0
            and bounds.included_count == 0
            and bounds.omitted_count == 0
            and bounds.omission_reason is None
            and not bounds.sampling_applied
            and not bounds.aggregation_applied
            and not bounds.aggregation_lossy
            and not bounds.dedup_applied
            and not bounds.truncation_applied
            and bounds.completeness_impact is EvidenceCompleteness.DEGRADED
        )

    def _handle_domain_failure(
        self,
        command: CaptureCommand,
        error: EvidenceDomainError,
        finalization: CaptureFinalizationHandoff | None,
    ) -> CaptureTerminalOutcome | NonTerminalInvocationFailure:
        if error.retry_disposition is RetryDisposition.RETRYABLE and finalization is None:
            return self._non_terminal(command, error.kind, str(error))
        return self._commit_failure(
            command, error.kind, error.retry_disposition, str(error)
        )

    @staticmethod
    def _non_terminal(
        command: CaptureCommand,
        kind: EvidenceFailureKind,
        summary: str,
        provenance: tuple[str, ...] = (),
    ) -> NonTerminalInvocationFailure:
        return NonTerminalInvocationFailure(
            capture_command_semantic_identity(command),
            CaptureFailure(command.capture_operation_id, kind, RetryDisposition.RETRYABLE, summary),
            provenance,
        )

    def _commit_failure(
        self,
        command: CaptureCommand,
        kind: EvidenceFailureKind,
        disposition: RetryDisposition,
        summary: str,
        provenance: tuple[str, ...] = (),
    ) -> CaptureTerminalOutcome:
        failure = CaptureFailure(command.capture_operation_id, kind, disposition, summary)
        return self._store.commit_failure(command, failure, safe_provenance=provenance)

    def _revision_content(
        self, plan: object,
        results: Mapping[EvidenceSource, LokiSourceResult | PrometheusSourceResult],
        completeness: EvidenceCompleteness,
    ) -> dict[str, object]:
        summaries = {source: result.summary for source, result in results.items()}
        windows = {source.value: summaries[source].query_provenance.logical_window for source in summaries}
        bounds = {source.value: summaries[source].bounds for source in summaries}
        return {
            "incident_context": asdict(plan.incident),
            "events": [asdict(event) for event in plan.events],
            "normalized_evidence": self._normalized_evidence(results),
            "source_statuses": {source.value: summaries[source].status.value for source in summaries},
            "completeness": completeness.value,
            "omission": bounds,
            "collection_boundaries": windows,
            "semantic_versions": {
                "canonicalization": plan.canonicalization_version,
                "source_policy": plan.source_policy_version,
                "bounds_policy": plan.bounds_policy_version,
                "selector_policy": plan.selector_policy_version,
                "redaction_policy": self._policy.redaction_policy_version,
                "config_identity": plan.config_identity,
            },
        }

    def _snapshot_content(
        self, plan: object,
        results: Mapping[EvidenceSource, LokiSourceResult | PrometheusSourceResult],
        revision_content: dict[str, object],
        finalization: CaptureFinalizationHandoff | None,
    ) -> dict[str, object]:
        summaries = {source: result.summary for source, result in results.items()}
        windows = {source.value: summaries[source].query_provenance.logical_window for source in summaries}
        content: dict[str, object] = {
            "incident_projection": asdict(plan.incident),
            "event_projections": [asdict(event) for event in plan.events],
            "episode": asdict(plan.episode),
            "windows": windows,
            "semantic_evidence": revision_content,
            "provenance": {
                source.value: {
                    "query": summary.query_provenance,
                    "collection": summary.collection_provenance,
                    "validation_findings": list(summary.validation_findings),
                    "safe_failure_kind": (
                        summary.safe_failure_kind.value if summary.safe_failure_kind else None
                    ),
                }
                for source, summary in summaries.items()
            },
            "bounds": {source.value: summary.bounds for source, summary in summaries.items()},
            "post_context": {
                "episode_end": plan.episode.end,
                "default_boundary": plan.windows.default_post_context_boundary,
                "effective_window_ends": {
                    EvidenceSource.LOKI.value: plan.windows.logs.end,
                    EvidenceSource.PROMETHEUS.value: plan.windows.metrics.end,
                },
                "snapshot_at": plan.command.snapshot_at,
                "reached_upper_boundary": {
                    EvidenceSource.LOKI.value: plan.logs_reached_post_context_boundary,
                    EvidenceSource.PROMETHEUS.value: plan.metrics_reached_post_context_boundary,
                },
            },
        }
        if finalization is not None:
            content["caller_finalization"] = {
                "authority_reference": finalization.authority_reference,
                "exhausted_sources": [source.value for source in finalization.exhausted_sources],
            }
        return content

    def _normalized_evidence(
        self, results: Mapping[EvidenceSource, LokiSourceResult | PrometheusSourceResult]
    ) -> dict[str, list[dict[str, object]]]:
        normalized: dict[str, list[dict[str, object]]] = {}
        for source, result in results.items():
            records: list[dict[str, object]] = []
            if isinstance(result, LokiSourceResult):
                for record in result.records:
                    semantic = {
                        "source": source.value,
                        "observed_at": record.timestamp,
                        "timestamp_ns": record.timestamp_ns,
                        "labels": dict(record.labels),
                        "message": record.line,
                        "redaction_policy_version": self._policy.redaction_policy_version,
                    }
                    records.append({
                        "record_semantic_identity": semantic_identity("spec013-log-record", semantic),
                        **semantic,
                    })
            else:
                for record in result.records:
                    semantic = {
                        "source": source.value,
                        "observed_at": record.timestamp,
                        "timestamp_seconds": record.timestamp_seconds,
                        "labels": dict(record.labels),
                        "value": record.value_text,
                        "redaction_policy_version": self._policy.redaction_policy_version,
                    }
                    records.append({
                        "record_semantic_identity": semantic_identity("spec013-metric-record", semantic),
                        **semantic,
                    })
            normalized[source.value] = records
        return normalized


__all__ = [
    "DEFAULT_SOURCE_REQUEST_POLICIES",
    "EvidenceCaptureService",
    "SourceAdapter",
    "SourceRequestPolicy",
    "effective_capture_config_identity",
]
