"""Immutable Candidate-B semantic contracts for SPEC-013 Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .errors import (
    EvidenceDomainError,
    EvidenceFailureKind,
    RetryDisposition,
    invalid_capture_command,
)
from .time_semantics import LogicalWindow, canonical_utc


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


__all__ = [
    "BoundsOmissionFacts",
    "CaptureCommand",
    "CaptureFailure",
    "CaptureTerminalKind",
    "CollectionProvenance",
    "EvidenceCompleteness",
    "EvidenceSource",
    "MaterialityEvaluationKind",
    "MaterialityJudgement",
    "MaterialityRequest",
    "MaterialityResult",
    "QueryProvenance",
    "SelectorFact",
    "SourceCollectionSummary",
    "SourceStatus",
]
