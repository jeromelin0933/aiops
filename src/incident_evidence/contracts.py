"""Immutable Candidate-B semantic contracts for SPEC-013 Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
from typing import Any

from .errors import (
    EvidenceDomainError,
    EvidenceFailureKind,
    RetryDisposition,
    invalid_capture_command,
)
from .time_semantics import CollectionWindows, Episode, LogicalWindow, canonical_utc


class CaptureTerminalKind(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class EvidenceSource(str, Enum):
    LOKI = "LOKI"
    PROMETHEUS = "PROMETHEUS"


class SourceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class EvidenceCompleteness(str, Enum):
    FULL = "FULL"
    DEGRADED = "DEGRADED"


class MaterialityJudgement(str, Enum):
    SAME = "SAME"
    NON_MATERIAL = "NON_MATERIAL"
    MATERIAL = "MATERIAL"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class MaterialityEvaluationKind(str, Enum):
    PAIRWISE = "PAIRWISE"
    NO_BASELINE = "NO_BASELINE"


@dataclass(frozen=True, slots=True)
class IncidentCaptureProjection:
    """Normalized SPEC-008 facts consumed by trusted-core admission."""

    incident_id: str
    event_ids: tuple[str, ...]
    status: str
    severity: str
    created_at: datetime
    updated_at: datetime
    last_correlated_at: datetime
    anchor_event_id: str | None
    correlation_family: str
    anchor_strength: str
    anchor_event_type: str | None
    normalized_fingerprint: object | None
    anchor_policy_id: str | None
    anchor_policy_version: str | None
    promoted_from_weak: bool


@dataclass(frozen=True, slots=True)
class TrustedEvent:
    """Validated, capture-relevant projection of one authoritative Event."""

    event_id: str
    detected_at: datetime
    event_source: str
    event_type: str
    severity: str
    selector_values: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PlannedSelector:
    """An escaped selector together with the authoritative Event it came from."""

    selector: SelectorFact
    source_event_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.selector, SelectorFact):
            raise TypeError("selector must be a SelectorFact")
        _reference(self.source_event_id, "source_event_id")


@dataclass(frozen=True, slots=True)
class CapturePlan:
    """Deterministic output of trusted-core admission; contains no source I/O."""

    command: CaptureCommand
    incident: IncidentCaptureProjection
    events: tuple[TrustedEvent, ...]
    episode: Episode
    windows: CollectionWindows
    selectors: tuple[PlannedSelector, ...]
    selector_policy_version: str
    config_identity: str
    capture_contract_version: str
    canonicalization_version: str
    source_policy_version: str
    bounds_policy_version: str
    logs_reached_post_context_boundary: bool
    metrics_reached_post_context_boundary: bool

    def __post_init__(self) -> None:
        if not isinstance(self.command, CaptureCommand):
            raise TypeError("command must be a CaptureCommand")
        if not isinstance(self.incident, IncidentCaptureProjection):
            raise TypeError("incident must be an IncidentCaptureProjection")
        if not isinstance(self.episode, Episode):
            raise TypeError("episode must be an Episode")
        if not isinstance(self.windows, CollectionWindows):
            raise TypeError("windows must be CollectionWindows")
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "selectors", tuple(self.selectors))
        if any(not isinstance(item, TrustedEvent) for item in self.events):
            raise TypeError("events must contain TrustedEvent values")
        if any(not isinstance(item, PlannedSelector) for item in self.selectors):
            raise TypeError("selectors must contain PlannedSelector values")
        for field in (
            "selector_policy_version", "config_identity", "capture_contract_version",
            "canonicalization_version", "source_policy_version", "bounds_policy_version",
        ):
            object.__setattr__(self, field, _reference(getattr(self, field), field))
        if not isinstance(self.logs_reached_post_context_boundary, bool) or not isinstance(
            self.metrics_reached_post_context_boundary, bool
        ):
            raise TypeError("post-context boundary facts must be bool values")


class EvidenceReadiness(str, Enum):
    READY = "READY"


class EvidenceIntegrityStatus(str, Enum):
    VALID = "VALID"


MAX_FAILURE_PROVENANCE_ENTRIES = 16
MAX_FAILURE_PROVENANCE_UTF8_BYTES = 4096


def _reference(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise invalid_capture_command(
            f"{field} must be a non-empty, trimmed identity", field_path=field
        )
    return value


def _bounded_text(value: object, field: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        raise ValueError(f"{field} must be non-empty, trimmed and at most {maximum} characters")
    return value


def _count(value: object, field: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _validate_safe_persisted_text(value: object, *, path: str) -> None:
    """Apply safe-text checks recursively at authoritative JSON boundaries."""
    from .security import validate_safe_text

    if isinstance(value, dict):
        for key, item in value.items():
            _validate_safe_persisted_text(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_safe_persisted_text(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        validate_safe_text(value, field_path=path)


def _require_object_keys(
    value: object, required: set[str], *, field: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or not required.issubset(value):
        missing = sorted(required - set(value)) if isinstance(value, dict) else sorted(required)
        raise ValueError(f"{field} is missing required facts: {', '.join(missing)}")
    return value


def _require_exact_object_keys(
    value: object, required: set[str], *, field: str
) -> dict[str, Any]:
    result = _require_object_keys(value, required, field=field)
    extras = sorted(set(result) - required)
    if extras:
        raise ValueError(f"{field} has unsupported facts: {', '.join(extras)}")
    return result


def _persisted_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an absolute timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an absolute timestamp") from exc


def _decode_window(value: object, *, field: str) -> LogicalWindow:
    data = _require_exact_object_keys(value, {"start", "end"}, field=field)
    return LogicalWindow(
        _persisted_datetime(data["start"], field=f"{field}.start"),
        _persisted_datetime(data["end"], field=f"{field}.end"),
    )


def _decode_query_provenance(value: object, *, source: EvidenceSource) -> QueryProvenance:
    data = _require_exact_object_keys(
        value,
        {
            "source", "query_semantic_identity", "query_version", "logical_window",
            "selectors", "adapter_contract_version", "bounds_policy_identity",
            "request_budget_identity",
        },
        field=f"Snapshot provenance.{source.value}.query",
    )
    if not isinstance(data["selectors"], list):
        raise ValueError("persisted query selectors must be an ordered list")
    selectors = []
    for index, raw in enumerate(data["selectors"]):
        selector = _require_exact_object_keys(
            raw,
            {
                "source", "field_name", "normalized_value", "escaped_value",
                "selector_policy_version",
            },
            field=f"Snapshot provenance.{source.value}.query.selectors[{index}]",
        )
        selectors.append(
            SelectorFact(
                EvidenceSource(selector["source"]),
                selector["field_name"],
                selector["normalized_value"],
                selector["escaped_value"],
                selector["selector_policy_version"],
            )
        )
    return QueryProvenance(
        EvidenceSource(data["source"]),
        data["query_semantic_identity"],
        data["query_version"],
        _decode_window(data["logical_window"], field=f"Snapshot provenance.{source.value}.query.logical_window"),
        tuple(selectors),
        data["adapter_contract_version"],
        data["bounds_policy_identity"],
        data["request_budget_identity"],
    )


def _decode_collection_provenance(
    value: object, *, source: EvidenceSource
) -> CollectionProvenance:
    data = _require_exact_object_keys(
        value,
        {
            "source", "adapter_contract_version", "logical_window", "page_count",
            "continuation_complete", "response_validation_status",
        },
        field=f"Snapshot provenance.{source.value}.collection",
    )
    return CollectionProvenance(
        EvidenceSource(data["source"]),
        data["adapter_contract_version"],
        _decode_window(data["logical_window"], field=f"Snapshot provenance.{source.value}.collection.logical_window"),
        data["page_count"],
        data["continuation_complete"],
        SourceStatus(data["response_validation_status"]),
    )


def _decode_bounds(value: object, *, source: EvidenceSource) -> BoundsOmissionFacts:
    fields = {
        "bounds_policy_version", "observed_candidate_count", "included_count",
        "omitted_count", "omission_reason", "sampling_applied",
        "sampling_policy_identity", "aggregation_applied", "aggregation_rule_identity",
        "aggregation_lossy", "dedup_applied", "dedup_input_count",
        "dedup_output_count", "truncation_applied", "truncation_reason",
        "completeness_impact",
    }
    data = _require_exact_object_keys(
        value, fields, field=f"Snapshot bounds.{source.value}"
    )
    return BoundsOmissionFacts(
        data["bounds_policy_version"], data["observed_candidate_count"],
        data["included_count"], data["omitted_count"], data["omission_reason"],
        data["sampling_applied"], data["sampling_policy_identity"],
        data["aggregation_applied"], data["aggregation_rule_identity"],
        data["aggregation_lossy"], data["dedup_applied"], data["dedup_input_count"],
        data["dedup_output_count"], data["truncation_applied"],
        data["truncation_reason"], EvidenceCompleteness(data["completeness_impact"]),
    )


@dataclass(frozen=True, slots=True)
class CaptureCommand:
    capture_operation_id: str
    incident_id: str
    snapshot_at: datetime
    capture_contract_version: str
    canonicalization_version: str
    source_policy_version: str
    bounds_policy_version: str
    config_identity: str

    def __post_init__(self) -> None:
        for field in (
            "capture_operation_id",
            "incident_id",
            "capture_contract_version",
            "canonicalization_version",
            "source_policy_version",
            "bounds_policy_version",
            "config_identity",
        ):
            object.__setattr__(self, field, _reference(getattr(self, field), field))
        object.__setattr__(
            self,
            "snapshot_at",
            canonical_utc(self.snapshot_at, field="snapshot_at"),
        )


@dataclass(frozen=True, slots=True)
class SelectorFact:
    source: EvidenceSource
    field_name: str
    normalized_value: str
    escaped_value: str
    selector_policy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise TypeError("source must be an EvidenceSource")
        for field in (
            "field_name",
            "normalized_value",
            "escaped_value",
            "selector_policy_version",
        ):
            object.__setattr__(self, field, _bounded_text(getattr(self, field), field))


@dataclass(frozen=True, slots=True)
class QueryProvenance:
    source: EvidenceSource
    query_semantic_identity: str
    query_version: str
    logical_window: LogicalWindow
    selectors: tuple[SelectorFact, ...]
    adapter_contract_version: str
    bounds_policy_identity: str
    request_budget_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise TypeError("source must be an EvidenceSource")
        if not isinstance(self.logical_window, LogicalWindow):
            raise TypeError("logical_window must be a LogicalWindow")
        for field in (
            "query_semantic_identity",
            "query_version",
            "adapter_contract_version",
            "bounds_policy_identity",
            "request_budget_identity",
        ):
            object.__setattr__(self, field, _bounded_text(getattr(self, field), field))
        try:
            selectors = tuple(self.selectors)
        except TypeError as exc:
            raise TypeError("selectors must be iterable") from exc
        if any(
            not isinstance(selector, SelectorFact) or selector.source is not self.source
            for selector in selectors
        ):
            raise ValueError("selectors must be SelectorFacts for the provenance source")
        if len({selector.field_name for selector in selectors}) != len(selectors):
            raise ValueError("selector fields must be unique")
        object.__setattr__(self, "selectors", selectors)


@dataclass(frozen=True, slots=True)
class CollectionProvenance:
    source: EvidenceSource
    adapter_contract_version: str
    logical_window: LogicalWindow
    page_count: int | None
    continuation_complete: bool
    response_validation_status: SourceStatus

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise TypeError("source must be an EvidenceSource")
        object.__setattr__(
            self,
            "adapter_contract_version",
            _bounded_text(self.adapter_contract_version, "adapter_contract_version"),
        )
        if not isinstance(self.logical_window, LogicalWindow):
            raise TypeError("logical_window must be a LogicalWindow")
        _count(self.page_count, "page_count", nullable=True)
        if not isinstance(self.continuation_complete, bool):
            raise TypeError("continuation_complete must be a bool")
        if not isinstance(self.response_validation_status, SourceStatus):
            raise TypeError("response_validation_status must be a SourceStatus")
        if not self.continuation_complete and self.response_validation_status in {
            SourceStatus.AVAILABLE,
            SourceStatus.EMPTY,
        }:
            raise ValueError("valid source status requires complete continuation")


@dataclass(frozen=True, slots=True)
class BoundsOmissionFacts:
    bounds_policy_version: str
    observed_candidate_count: int | None
    included_count: int
    omitted_count: int | None
    omission_reason: str | None
    sampling_applied: bool
    sampling_policy_identity: str | None
    aggregation_applied: bool
    aggregation_rule_identity: str | None
    aggregation_lossy: bool
    dedup_applied: bool
    dedup_input_count: int | None
    dedup_output_count: int | None
    truncation_applied: bool
    truncation_reason: str | None
    completeness_impact: EvidenceCompleteness

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bounds_policy_version",
            _bounded_text(self.bounds_policy_version, "bounds_policy_version"),
        )
        observed = _count(
            self.observed_candidate_count,
            "observed_candidate_count",
            nullable=True,
        )
        included = _count(self.included_count, "included_count")
        omitted = _count(self.omitted_count, "omitted_count", nullable=True)
        if not all(
            isinstance(value, bool)
            for value in (
                self.sampling_applied,
                self.aggregation_applied,
                self.aggregation_lossy,
                self.dedup_applied,
                self.truncation_applied,
            )
        ):
            raise TypeError("bounds operation flags must be bool values")
        if not isinstance(self.completeness_impact, EvidenceCompleteness):
            raise TypeError("completeness_impact must be EvidenceCompleteness")
        if self.omission_reason is not None:
            _bounded_text(self.omission_reason, "omission_reason")

        if observed is None:
            if omitted is not None or self.omission_reason is None:
                raise ValueError(
                    "unknown observed count requires UNKNOWN omitted count and an explicit reason"
                )
            if self.completeness_impact is not EvidenceCompleteness.DEGRADED:
                raise ValueError("unknown omission magnitude must be DEGRADED")
        else:
            if omitted is None or observed != included + omitted:
                raise ValueError(
                    "known observed count must equal included_count + omitted_count"
                )
            if omitted and self.omission_reason is None:
                raise ValueError("omitted evidence requires an explicit reason")
            if not omitted and self.omission_reason is not None:
                raise ValueError("omission_reason requires omitted evidence")

        self._require_rule(
            self.sampling_applied,
            self.sampling_policy_identity,
            "sampling_policy_identity",
        )
        self._require_rule(
            self.aggregation_applied,
            self.aggregation_rule_identity,
            "aggregation_rule_identity",
        )
        self._require_rule(
            self.truncation_applied,
            self.truncation_reason,
            "truncation_reason",
        )
        if self.aggregation_lossy and not self.aggregation_applied:
            raise ValueError("lossy aggregation requires aggregation_applied")

        dedup_input = _count(
            self.dedup_input_count, "dedup_input_count", nullable=True
        )
        dedup_output = _count(
            self.dedup_output_count, "dedup_output_count", nullable=True
        )
        if self.dedup_applied:
            if dedup_input is None or dedup_output is None or dedup_output > dedup_input:
                raise ValueError("dedup requires valid input/output counts")
        elif dedup_input is not None or dedup_output is not None:
            raise ValueError("dedup counts require dedup_applied")

        lossless_omission_proven = (
            self.dedup_applied
            and dedup_input is not None
            and dedup_output is not None
            and omitted is not None
            and dedup_input - dedup_output == omitted
        ) or (self.aggregation_applied and not self.aggregation_lossy)
        if (
            omitted is not None
            and omitted > 0
            and not lossless_omission_proven
            and self.completeness_impact is not EvidenceCompleteness.DEGRADED
        ):
            raise ValueError(
                "semantic omission without proven lossless normalization must be DEGRADED"
            )

        if (
            self.sampling_applied
            or self.aggregation_lossy
            or self.truncation_applied
        ) and self.completeness_impact is not EvidenceCompleteness.DEGRADED:
            raise ValueError("lossy bounds operations must be DEGRADED")

    @staticmethod
    def _require_rule(applied: bool, value: str | None, field: str) -> None:
        if applied:
            _bounded_text(value, field)
        elif value is not None:
            raise ValueError(f"{field} requires its operation to be applied")


@dataclass(frozen=True, slots=True)
class SourceCollectionSummary:
    source: EvidenceSource
    status: SourceStatus
    record_count: int
    query_provenance: QueryProvenance
    collection_provenance: CollectionProvenance
    bounds: BoundsOmissionFacts
    validation_findings: tuple[str, ...] = ()
    safe_failure_kind: EvidenceFailureKind | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise TypeError("source must be an EvidenceSource")
        if not isinstance(self.status, SourceStatus):
            raise TypeError("status must be a SourceStatus")
        count = _count(self.record_count, "record_count")
        if self.query_provenance.source is not self.source:
            raise ValueError("query provenance source must match collection source")
        if self.collection_provenance.source is not self.source:
            raise ValueError("collection provenance source must match collection source")
        if self.collection_provenance.response_validation_status is not self.status:
            raise ValueError("collection validation status must match source status")
        if count != self.bounds.included_count:
            raise ValueError("record_count must match bounds included_count")
        if self.status is SourceStatus.AVAILABLE and count == 0:
            raise ValueError("AVAILABLE requires one or more records")
        if self.status is SourceStatus.EMPTY and count != 0:
            raise ValueError("EMPTY requires zero records")
        if self.status in {SourceStatus.UNAVAILABLE, SourceStatus.INVALID} and count != 0:
            raise ValueError("unavailable or invalid payload cannot be admitted")
        findings = tuple(
            _bounded_text(value, "validation_finding")
            for value in self.validation_findings
        )
        object.__setattr__(self, "validation_findings", findings)
        if self.safe_failure_kind is not None and not isinstance(
            self.safe_failure_kind, EvidenceFailureKind
        ):
            raise TypeError("safe_failure_kind must be an EvidenceFailureKind")
        if self.status in {SourceStatus.AVAILABLE, SourceStatus.EMPTY}:
            if self.safe_failure_kind is not None:
                raise ValueError("valid source status cannot carry a failure kind")
        elif self.safe_failure_kind is None:
            raise ValueError("failed source status requires a safe failure kind")


@dataclass(frozen=True, slots=True)
class CaptureFailure:
    capture_operation_id: str
    kind: EvidenceFailureKind
    retry_disposition: RetryDisposition
    safe_summary: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capture_operation_id",
            _reference(self.capture_operation_id, "capture_operation_id"),
        )
        if not isinstance(self.kind, EvidenceFailureKind):
            raise TypeError("kind must be an EvidenceFailureKind")
        if not isinstance(self.retry_disposition, RetryDisposition):
            raise TypeError("retry_disposition must be a RetryDisposition")
        from .security import validate_safe_text

        validate_safe_text(self.safe_summary, field_path="safe_summary")
        object.__setattr__(
            self, "safe_summary", _bounded_text(self.safe_summary, "safe_summary")
        )


@dataclass(frozen=True, slots=True)
class MaterialityRequest:
    evaluation_kind: MaterialityEvaluationKind
    candidate_revision_id: str
    materiality_rule_version: str
    baseline_revision_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_kind, MaterialityEvaluationKind):
            raise TypeError("evaluation_kind must be a MaterialityEvaluationKind")
        for field in ("candidate_revision_id", "materiality_rule_version"):
            object.__setattr__(self, field, _reference(getattr(self, field), field))
        if self.evaluation_kind is MaterialityEvaluationKind.PAIRWISE:
            object.__setattr__(
                self,
                "baseline_revision_id",
                _reference(self.baseline_revision_id, "baseline_revision_id"),
            )
        elif self.baseline_revision_id is not None:
            raise ValueError("NO_BASELINE must not carry a baseline Revision")


@dataclass(frozen=True, slots=True)
class MaterialityResult:
    materiality_result_id: str
    request: MaterialityRequest
    judgement: MaterialityJudgement | None
    reason_facts: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "materiality_result_id",
            _reference(self.materiality_result_id, "materiality_result_id"),
        )
        if not isinstance(self.request, MaterialityRequest):
            raise TypeError("request must be a MaterialityRequest")
        if self.request.evaluation_kind is MaterialityEvaluationKind.PAIRWISE:
            if not isinstance(self.judgement, MaterialityJudgement):
                raise TypeError("PAIRWISE result requires a MaterialityJudgement")
        elif self.judgement is not None:
            raise ValueError("NO_BASELINE result must not fabricate a pairwise judgement")
        facts = tuple(_bounded_text(value, "reason_fact") for value in self.reason_facts)
        object.__setattr__(self, "reason_facts", facts)


@dataclass(frozen=True, slots=True)
class EvidenceRevision:
    """Incident-scoped canonical evidence identity persisted by Candidate B."""

    revision_id: str
    incident_id: str
    canonicalization_version: str
    canonical_semantic_content: str
    integrity_identity: str

    def __post_init__(self) -> None:
        from .identity import canonical_json, semantic_identity

        for field in ("revision_id", "incident_id", "canonicalization_version"):
            object.__setattr__(self, field, _reference(getattr(self, field), field))
        if not isinstance(self.canonical_semantic_content, str):
            raise TypeError("canonical_semantic_content must be canonical JSON")
        try:
            decoded = json.loads(self.canonical_semantic_content)
        except (TypeError, ValueError) as exc:
            raise ValueError("canonical_semantic_content must be valid JSON") from exc
        if canonical_json(decoded) != self.canonical_semantic_content:
            raise ValueError("canonical_semantic_content is not canonical JSON")
        from .security import validate_safe_provenance
        validate_safe_provenance(decoded, path="revision.semantic_content")
        _validate_safe_persisted_text(decoded, path="revision.semantic_content")
        semantic = _require_object_keys(
            decoded,
            {
                "incident_context",
                "events",
                "normalized_evidence",
                "source_statuses",
                "completeness",
                "omission",
                "collection_boundaries",
            },
            field="Revision semantic content",
        )
        if not isinstance(semantic["incident_context"], dict):
            raise ValueError("Revision incident_context must be an object")
        if not isinstance(semantic["events"], list) or not semantic["events"]:
            raise ValueError("Revision events must be a non-empty ordered list")
        for field in ("normalized_evidence", "source_statuses", "omission", "collection_boundaries"):
            if not isinstance(semantic[field], dict):
                raise ValueError(f"Revision {field} must be an object")
        if semantic["completeness"] not in {item.value for item in EvidenceCompleteness}:
            raise ValueError("Revision completeness is invalid")
        projection = {
            "incident_id": self.incident_id,
            "canonicalization_version": self.canonicalization_version,
            "semantic_content": decoded,
        }
        expected_revision_id = semantic_identity("spec013-evidence-revision", projection)
        expected_integrity = semantic_identity("spec013-revision-integrity", projection)
        if self.revision_id != expected_revision_id:
            raise ValueError("revision_id contradicts canonical semantic evidence")
        if self.integrity_identity != expected_integrity:
            raise ValueError("Revision integrity identity is invalid")

    @classmethod
    def from_content(
        cls,
        *,
        incident_id: str,
        canonicalization_version: str,
        semantic_content: object,
    ) -> "EvidenceRevision":
        from .identity import canonical_json, semantic_identity

        incident_id = _reference(incident_id, "incident_id")
        canonicalization_version = _reference(
            canonicalization_version, "canonicalization_version"
        )
        content = canonical_json(semantic_content)
        projection = {
            "incident_id": incident_id,
            "canonicalization_version": canonicalization_version,
            "semantic_content": json.loads(content),
        }
        return cls(
            semantic_identity("spec013-evidence-revision", projection),
            incident_id,
            canonicalization_version,
            content,
            semantic_identity("spec013-revision-integrity", projection),
        )

    @property
    def semantic_content(self) -> Any:
        return json.loads(self.canonical_semantic_content)


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """Immutable operational capture record bound to exactly one Revision."""

    snapshot_id: str
    capture_operation_id: str
    incident_id: str
    snapshot_at: datetime
    capture_contract_version: str
    canonicalization_version: str
    source_policy_version: str
    bounds_policy_version: str
    config_identity: str
    revision_id: str
    completeness: EvidenceCompleteness
    source_statuses: tuple[tuple[EvidenceSource, SourceStatus], ...]
    canonical_snapshot_content: str

    def __post_init__(self) -> None:
        from .identity import canonical_json, semantic_identity

        for field in (
            "snapshot_id",
            "capture_operation_id",
            "incident_id",
            "capture_contract_version",
            "canonicalization_version",
            "source_policy_version",
            "bounds_policy_version",
            "config_identity",
            "revision_id",
        ):
            object.__setattr__(self, field, _reference(getattr(self, field), field))
        object.__setattr__(self, "snapshot_at", canonical_utc(self.snapshot_at, field="snapshot_at"))
        if not isinstance(self.completeness, EvidenceCompleteness):
            raise TypeError("completeness must be an EvidenceCompleteness")
        statuses = tuple(self.source_statuses)
        if not statuses:
            raise ValueError("source_statuses must not be empty")
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], EvidenceSource)
            or not isinstance(item[1], SourceStatus)
            for item in statuses
        ):
            raise TypeError("source_statuses must contain EvidenceSource/SourceStatus pairs")
        if len({source for source, _ in statuses}) != len(statuses):
            raise ValueError("source_statuses must be unique by source")
        statuses = tuple(sorted(statuses, key=lambda item: item[0].value))
        object.__setattr__(self, "source_statuses", statuses)
        if self.completeness is EvidenceCompleteness.FULL and any(
            status not in {SourceStatus.AVAILABLE, SourceStatus.EMPTY}
            for _, status in statuses
        ):
            raise ValueError("FULL Snapshot cannot contain an unavailable or invalid source")
        if not isinstance(self.canonical_snapshot_content, str):
            raise TypeError("canonical_snapshot_content must be canonical JSON")
        try:
            content = json.loads(self.canonical_snapshot_content)
        except (TypeError, ValueError) as exc:
            raise ValueError("canonical_snapshot_content must be valid JSON") from exc
        if not isinstance(content, dict) or not content:
            raise ValueError("Snapshot content must be a non-empty object")
        if canonical_json(content) != self.canonical_snapshot_content:
            raise ValueError("canonical_snapshot_content is not canonical JSON")
        from .security import validate_safe_provenance
        validate_safe_provenance(content, path="snapshot.content")
        _validate_safe_persisted_text(content, path="snapshot.content")
        snapshot_content = _require_object_keys(
            content,
            {
                "incident_projection",
                "event_projections",
                "episode",
                "windows",
                "semantic_evidence",
                "provenance",
                "bounds",
                "post_context",
            },
            field="Snapshot content",
        )
        if not isinstance(snapshot_content["incident_projection"], dict) or not snapshot_content["incident_projection"]:
            raise ValueError("Snapshot incident_projection must be a non-empty object")
        if not isinstance(snapshot_content["event_projections"], list) or not snapshot_content["event_projections"]:
            raise ValueError("Snapshot event_projections must be a non-empty ordered list")
        _require_object_keys(snapshot_content["episode"], {"start", "end"}, field="Snapshot episode")
        source_names = {source.value for source, _ in statuses}
        source_maps = {
            field: _require_exact_object_keys(
                snapshot_content[field], source_names, field=f"Snapshot {field}"
            )
            for field in ("windows", "provenance", "bounds")
        }
        summaries = []
        for source, status in statuses:
            provenance = _require_exact_object_keys(
                source_maps["provenance"][source.value],
                {"query", "collection", "validation_findings", "safe_failure_kind"},
                field=f"Snapshot provenance.{source.value}",
            )
            if not isinstance(provenance["validation_findings"], list):
                raise ValueError("persisted validation_findings must be an ordered list")
            query = _decode_query_provenance(provenance["query"], source=source)
            collection = _decode_collection_provenance(
                provenance["collection"], source=source
            )
            bounds = _decode_bounds(source_maps["bounds"][source.value], source=source)
            window = _decode_window(
                source_maps["windows"][source.value],
                field=f"Snapshot windows.{source.value}",
            )
            if query.source is not source or collection.source is not source:
                raise ValueError("persisted provenance source identity is contradictory")
            if query.logical_window != window or collection.logical_window != window:
                raise ValueError("persisted provenance logical windows are contradictory")
            if bounds.bounds_policy_version != self.bounds_policy_version:
                raise ValueError("persisted bounds policy contradicts CaptureCommand")
            safe_failure = provenance["safe_failure_kind"]
            summary = SourceCollectionSummary(
                source,
                status,
                bounds.included_count,
                query,
                collection,
                bounds,
                tuple(provenance["validation_findings"]),
                EvidenceFailureKind(safe_failure) if safe_failure is not None else None,
            )
            summaries.append(summary)
        if self.completeness is EvidenceCompleteness.FULL:
            if any(
                summary.status in {SourceStatus.UNAVAILABLE, SourceStatus.INVALID}
                or summary.bounds.completeness_impact is EvidenceCompleteness.DEGRADED
                for summary in summaries
            ):
                raise ValueError("FULL Snapshot has completeness-affecting source or bounds facts")
        elif not any(
            summary.status in {SourceStatus.UNAVAILABLE, SourceStatus.INVALID}
            or summary.bounds.completeness_impact is EvidenceCompleteness.DEGRADED
            for summary in summaries
        ):
            raise ValueError("DEGRADED Snapshot requires a persisted degradation basis")
        _require_object_keys(
            snapshot_content["post_context"],
            {
                "episode_end",
                "default_boundary",
                "effective_window_ends",
                "snapshot_at",
                "reached_upper_boundary",
            },
            field="Snapshot post_context",
        )
        if not isinstance(snapshot_content["semantic_evidence"], dict):
            raise ValueError("Snapshot semantic_evidence must be an object")
        normalized = snapshot_content["semantic_evidence"].get("normalized_evidence")
        normalized = _require_exact_object_keys(
            normalized, source_names, field="Snapshot semantic_evidence.normalized_evidence"
        )
        for summary in summaries:
            records = normalized[summary.source.value]
            if not isinstance(records, list):
                raise ValueError("normalized source evidence must be an ordered list")
            if len(records) != summary.record_count:
                raise ValueError("normalized evidence count contradicts persisted bounds")
        expected = semantic_identity(
            "spec013-evidence-snapshot",
            {"capture_operation_id": self.capture_operation_id},
        )
        if self.snapshot_id != expected:
            raise ValueError("snapshot_id contradicts capture operation identity")

    @classmethod
    def from_content(
        cls,
        command: CaptureCommand,
        *,
        revision_id: str,
        completeness: EvidenceCompleteness,
        source_statuses: tuple[tuple[EvidenceSource, SourceStatus], ...],
        snapshot_content: object,
    ) -> "EvidenceSnapshot":
        from .identity import canonical_json, semantic_identity

        if not isinstance(command, CaptureCommand):
            raise TypeError("command must be a CaptureCommand")
        return cls(
            semantic_identity(
                "spec013-evidence-snapshot",
                {"capture_operation_id": command.capture_operation_id},
            ),
            command.capture_operation_id,
            command.incident_id,
            command.snapshot_at,
            command.capture_contract_version,
            command.canonicalization_version,
            command.source_policy_version,
            command.bounds_policy_version,
            command.config_identity,
            revision_id,
            completeness,
            source_statuses,
            canonical_json(snapshot_content),
        )

    @property
    def snapshot_content(self) -> dict[str, Any]:
        return json.loads(self.canonical_snapshot_content)


@dataclass(frozen=True, slots=True)
class CaptureSuccess:
    command: CaptureCommand
    snapshot: EvidenceSnapshot
    revision: EvidenceRevision

    def __post_init__(self) -> None:
        if not isinstance(self.command, CaptureCommand):
            raise TypeError("command must be a CaptureCommand")
        if not isinstance(self.snapshot, EvidenceSnapshot):
            raise TypeError("snapshot must be an EvidenceSnapshot")
        if not isinstance(self.revision, EvidenceRevision):
            raise TypeError("revision must be an EvidenceRevision")
        if self.snapshot.capture_operation_id != self.command.capture_operation_id:
            raise ValueError("Snapshot does not belong to CaptureCommand")
        if self.snapshot.incident_id != self.command.incident_id:
            raise ValueError("Snapshot Incident contradicts CaptureCommand")
        for field in (
            "snapshot_at",
            "capture_contract_version",
            "canonicalization_version",
            "source_policy_version",
            "bounds_policy_version",
            "config_identity",
        ):
            if getattr(self.snapshot, field) != getattr(self.command, field):
                raise ValueError(f"Snapshot {field} contradicts CaptureCommand")
        if self.snapshot.revision_id != self.revision.revision_id:
            raise ValueError("Snapshot Revision binding is contradictory")
        if self.revision.incident_id != self.command.incident_id:
            raise ValueError("Revision Incident contradicts CaptureCommand")
        if self.revision.canonicalization_version != self.command.canonicalization_version:
            raise ValueError("Revision canonicalization contradicts CaptureCommand")
        semantic = self.revision.semantic_content
        expected_statuses = {
            source.value: status.value for source, status in self.snapshot.source_statuses
        }
        if semantic["source_statuses"] != expected_statuses:
            raise ValueError("Revision source statuses contradict Snapshot")
        if semantic["completeness"] != self.snapshot.completeness.value:
            raise ValueError("Revision completeness contradicts Snapshot")
        if self.snapshot.snapshot_content["semantic_evidence"] != semantic:
            raise ValueError("Snapshot semantic evidence contradicts Revision")
        content = self.snapshot.snapshot_content
        if semantic["omission"] != content["bounds"]:
            raise ValueError("Revision omission facts contradict Snapshot bounds")
        if semantic["collection_boundaries"] != content["windows"]:
            raise ValueError("Revision collection boundaries contradict Snapshot windows")


@dataclass(frozen=True, slots=True)
class CaptureTerminalOutcome:
    capture_operation_id: str
    command_semantic_identity: str
    terminal_kind: CaptureTerminalKind
    snapshot_id: str | None = None
    revision_id: str | None = None
    failure: CaptureFailure | None = None
    safe_provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "capture_operation_id", _reference(self.capture_operation_id, "capture_operation_id"))
        object.__setattr__(self, "command_semantic_identity", _reference(self.command_semantic_identity, "command_semantic_identity"))
        if not isinstance(self.terminal_kind, CaptureTerminalKind):
            raise TypeError("terminal_kind must be a CaptureTerminalKind")
        from .security import validate_safe_text
        provenance = tuple(_bounded_text(item, "safe_provenance") for item in self.safe_provenance)
        if len(provenance) > MAX_FAILURE_PROVENANCE_ENTRIES:
            raise ValueError(
                f"safe_provenance must contain at most {MAX_FAILURE_PROVENANCE_ENTRIES} entries"
            )
        from .identity import canonical_json
        if len(canonical_json(provenance).encode("utf-8")) > MAX_FAILURE_PROVENANCE_UTF8_BYTES:
            raise ValueError(
                f"safe_provenance must be at most {MAX_FAILURE_PROVENANCE_UTF8_BYTES} UTF-8 bytes"
            )
        for index, item in enumerate(provenance):
            validate_safe_text(item, field_path=f"safe_provenance[{index}]")
        object.__setattr__(self, "safe_provenance", provenance)
        if self.terminal_kind is CaptureTerminalKind.SUCCESS:
            _reference(self.snapshot_id, "snapshot_id")
            _reference(self.revision_id, "revision_id")
            if self.failure is not None or provenance:
                raise ValueError("SUCCESS cannot carry failure authority")
        else:
            if self.snapshot_id is not None or self.revision_id is not None:
                raise ValueError("FAILURE cannot carry Snapshot/Revision authority")
            if not isinstance(self.failure, CaptureFailure):
                raise TypeError("FAILURE requires CaptureFailure")
            if self.failure.capture_operation_id != self.capture_operation_id:
                raise ValueError("CaptureFailure operation identity is contradictory")


@dataclass(frozen=True, slots=True)
class EvidenceRecoveryFacts:
    capture_outcomes: tuple[CaptureTerminalOutcome, ...]
    snapshots: tuple[EvidenceSnapshot, ...]
    revisions: tuple[EvidenceRevision, ...]
    materiality_results: tuple[MaterialityResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "capture_outcomes", tuple(self.capture_outcomes))
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "revisions", tuple(self.revisions))
        object.__setattr__(self, "materiality_results", tuple(self.materiality_results))


__all__ = [
    "BoundsOmissionFacts",
    "CaptureCommand",
    "CaptureFailure",
    "CaptureTerminalKind",
    "CollectionProvenance",
    "EvidenceCompleteness",
    "EvidenceIntegrityStatus",
    "MAX_FAILURE_PROVENANCE_ENTRIES",
    "MAX_FAILURE_PROVENANCE_UTF8_BYTES",
    "EvidenceReadiness",
    "EvidenceRecoveryFacts",
    "EvidenceRevision",
    "EvidenceSnapshot",
    "EvidenceSource",
    "MaterialityEvaluationKind",
    "MaterialityJudgement",
    "MaterialityRequest",
    "MaterialityResult",
    "QueryProvenance",
    "SelectorFact",
    "SourceCollectionSummary",
    "SourceStatus",
    "CaptureSuccess",
    "CaptureTerminalOutcome",
]
