"""SPEC-016 S1 continuation facts persisted in the existing Runtime D2 DB."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .clock import canonical_utc, format_utc, parse_utc
from .contracts import RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus
from .identity import runtime_work_id
from .sqlite_work_store import (
    SqliteRuntimeWorkStore, RuntimeWorkConcurrencyError, _decode_record as _decode_work,
    _encode_record as _encode_work, _validate_transition as _validate_work_transition,
)

_MAX_TEXT, _MAX_FRONTIER, _MAX_BOUNDARIES, _SCHEMA_VERSION = 512, 64, 32, 1
_RUNTIME_TABLES = frozenset({"runtime_work_store_metadata", "runtime_work_records"})
_TABLES = _RUNTIME_TABLES | frozenset({"rca_continuation_store_metadata", "rca_runtime_continuations"})
_METADATA_COLUMNS = (("singleton", "INTEGER", 1, 1), ("schema_version", "INTEGER", 1, 0))
_RECORD_COLUMNS = (("root_id", "TEXT", 1, 1), ("record_json", "TEXT", 1, 0), ("revision", "INTEGER", 1, 0))
_REFERENCE = re.compile(
    r"(?:INC-[A-Za-z0-9._-]+|rtw-[A-Za-z0-9._-]+|rtw_[a-f0-9]{64}|rto_[a-f0-9]{64}|rcar_[a-f0-9]{64}|rcao_[a-f0-9]{64}|"
    r"SPEC-\d{3}/v\d+|(?:canon|policy|bounds)/v\d+|[a-z][a-z0-9_-]*-v\d+|"
    r"[a-z][a-z0-9_-]*:[a-f0-9]{64}|collection:[a-z][a-z0-9_-]*|"
    r"(?:revision|boundary|version|aggregate|attempt|retry|publication|followup|materiality):[A-Za-z0-9._-]+)"
)
_CODES = frozenset({
    "INITIAL", "CAPTURE", "FREEZE_CAPTURE_BASIS", "MATERIALITY", "RETRIEVAL", "ATTEMPT",
    "EXECUTION", "ARTIFACT", "PUBLICATION", "FOLLOW_UP", "NONE", "EVIDENCE_CAPTURE",
    "KNOWLEDGE_RETRIEVAL", "D_EXECUTION", "ARTIFACT_COMMIT", "AGGREGATE_IDENTITY",
    "AGGREGATE_DISCOVERY", "ATTEMPT_ADMISSION",
})
_SOURCE_DOMAINS = frozenset({"CANDIDATE_A", "CANDIDATE_B", "CANDIDATE_C", "CANDIDATE_D", "SPEC_008", "SPEC_011"})
_SOURCE_DISPOSITIONS = frozenset({"RETRYABLE", "NON_RETRYABLE", "REPAIR_REQUIRED", "APPLIED", "PENDING", "EXHAUSTED", "SUPERSEDED", "OBSOLETE"})
_OBSERVATION_CODES = frozenset({"OBSERVED", "RECONCILED", "RETRY_SCHEDULED", "RECOVERY_REQUIRED", "RECEIPT_FOUND"})


def _text(value: object, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{field} must be a bounded, non-empty, trimmed string")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} contains control characters")
    return value


def _reference(value: object, field: str, *, optional: bool = False) -> str | None:
    text = _text(value, field, optional=optional)
    if text is not None and _REFERENCE.fullmatch(text) is None:
        raise ValueError(f"{field} must be an allow-listed continuation reference")
    return text


def _code(value: object, field: str, vocabulary: frozenset[str], *, optional: bool = False) -> str | None:
    text = _text(value, field, optional=optional)
    if text is not None and text not in vocabulary:
        raise RcaRepairRequiredError(f"REPAIR_REQUIRED: {field} is not an allow-listed code")
    return text


def _identity(prefix: str, domain: str, *parts: str) -> str:
    _text(domain, "identity domain")
    if not parts:
        raise ValueError("identity requires stable semantic inputs")
    encoded = ["spec-016/e1/v1", f"{len(domain)}:{domain}"]
    for part in parts:
        _text(part, "identity input")
        encoded.append(f"{len(part)}:{part}")
    return f"{prefix}_{hashlib.sha256('|'.join(encoded).encode()).hexdigest()}"


def rca_root_id(incident_id: str) -> str:
    _reference(incident_id, "incident_id")
    return _identity("rcar", "INCIDENT_RCA_ROOT", incident_id)


def rca_child_operation_id(root_id: str, subject: str, purpose: str) -> str:
    _reference(root_id, "root_id"); _text(subject, "subject"); _code(purpose, "purpose", _CODES)
    return _identity("rcao", f"CHILD:{purpose}", root_id, subject, purpose)


@dataclass(frozen=True, slots=True)
class CaptureCommandBasis:
    incident_id: str; root_id: str; purpose: str; snapshot_at: datetime
    capture_contract_version: str; canonicalization_version: str; source_policy_version: str
    configuration_identity: str; collection_boundary_references: tuple[str, ...]
    # Older S1 continuations did not retain this component separately.  It is
    # optional only for backward decoding; S2 refuses to construct a B command
    # without it.
    bounds_policy_version: str | None = None

    def __post_init__(self) -> None:
        _reference(self.incident_id, "incident_id"); _reference(self.root_id, "root_id")
        if self.root_id != rca_root_id(self.incident_id):
            raise ValueError("root_id contradicts authoritative incident_id")
        _code(self.purpose, "purpose", _CODES)
        for field in ("capture_contract_version", "canonicalization_version", "source_policy_version", "configuration_identity"):
            _reference(getattr(self, field), field)
        _reference(self.bounds_policy_version, "bounds_policy_version", optional=True)
        object.__setattr__(self, "snapshot_at", canonical_utc(self.snapshot_at, field="snapshot_at"))
        refs = tuple(self.collection_boundary_references)
        if not refs or len(refs) > _MAX_BOUNDARIES or len(refs) != len(set(refs)):
            raise ValueError("collection_boundary_references must be distinct and bounded")
        for ref in refs:
            _reference(ref, "collection_boundary_reference")
            if not ref.startswith("collection:"):
                raise ValueError("collection_boundary_reference must be a collection reference")
        object.__setattr__(self, "collection_boundary_references", refs)

    @property
    def capture_operation_id(self) -> str:
        subject = hashlib.sha256(json.dumps(_encode_basis(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return rca_child_operation_id(self.root_id, subject, "EVIDENCE_CAPTURE")


def derive_capture_operation_id(basis: CaptureCommandBasis) -> str:
    if not isinstance(basis, CaptureCommandBasis): raise TypeError("basis must be CaptureCommandBasis")
    return basis.capture_operation_id


@dataclass(frozen=True, slots=True)
class FollowUpRequirement:
    requirement_type: str; reference: str
    def __post_init__(self) -> None:
        prefixes = {"MATERIAL_EVIDENCE": "revision:", "POST_CONTEXT": "boundary:", "STALE_REFRESH": "version:"}
        if self.requirement_type not in prefixes:
            raise RcaRepairRequiredError("REPAIR_REQUIRED: requirement_type is not allow-listed")
        _text(self.reference, "reference")
        if not self.reference.startswith(prefixes[self.requirement_type]):
            raise RcaRepairRequiredError("REPAIR_REQUIRED: requirement reference contradicts requirement_type")
        subject = self.reference[len(prefixes[self.requirement_type]):]
        if not subject or (self.requirement_type == "MATERIAL_EVIDENCE" and
                           re.fullmatch(r"spec013-evidence-revision:[a-f0-9]{64}", subject) is None and
                           _REFERENCE.fullmatch(self.reference) is None) or (
                           self.requirement_type == "POST_CONTEXT" and
                           re.fullmatch(r"spec013-evidence-snapshot:[a-f0-9]{64}", subject) is None and
                           _REFERENCE.fullmatch(self.reference) is None) or (
                           self.requirement_type == "STALE_REFRESH" and
                           _REFERENCE.fullmatch(subject) is None and _REFERENCE.fullmatch(self.reference) is None):
            raise RcaRepairRequiredError("REPAIR_REQUIRED: requirement subject is not an allow-listed reference")


@dataclass(frozen=True, slots=True)
class FollowUpResolution:
    member: FollowUpRequirement
    current_version_id: str
    freshness: str
    material_evidence_revision_basis: str
    attempt_id: str
    artifact_revision_id: str
    publication_id: str
    baseline_revision_id: str | None
    candidate_revision_id: str | None
    materiality_rule_version: str | None
    materiality_result_id: str | None
    judgement: str | None
    coverage_snapshot_id: str | None
    state: str

    def __post_init__(self) -> None:
        if not isinstance(self.member, FollowUpRequirement): raise TypeError("resolution member is invalid")
        for name in ("current_version_id", "material_evidence_revision_basis", "attempt_id",
                     "artifact_revision_id", "publication_id"):
            _text(getattr(self, name), name)
        for name in ("baseline_revision_id", "candidate_revision_id", "materiality_rule_version",
                     "materiality_result_id", "judgement", "coverage_snapshot_id"):
            _text(getattr(self, name), name, optional=True)
        if self.freshness not in ("FRESH", "STALE") or self.state not in ("PROVISIONAL", "VALIDATED", "INVALIDATED"):
            raise ValueError("invalid follow-up resolution state")
        if self.member.requirement_type == "MATERIAL_EVIDENCE" and (
            self.candidate_revision_id != self.member.reference[len("revision:"):] or
            None in (self.baseline_revision_id, self.materiality_rule_version,
                     self.materiality_result_id, self.judgement)):
            raise ValueError("material resolution lacks exact B request and result")


@dataclass(frozen=True, slots=True)
class RefreshBasis:
    """D2 references frozen before Candidate-A refresh Attempt admission."""
    requirement: FollowUpRequirement
    baseline_version_id: str
    evidence_snapshot_id: str
    evidence_revision_id: str
    knowledge_snapshot_id: str
    materiality_result_id: str
    attempt_id: str
    lineage_commitment: str

    def __post_init__(self) -> None:
        if self.requirement.requirement_type != "MATERIAL_EVIDENCE":
            raise ValueError("refresh basis requires an Evidence Revision member")
        if self.requirement.reference != "revision:" + self.evidence_revision_id:
            raise ValueError("refresh basis contradicts Evidence Revision member")
        _reference(self.baseline_version_id, "baseline_version_id")
        _reference(self.attempt_id, "attempt_id")
        for field, pattern in (
            ("evidence_snapshot_id", r"spec013-evidence-snapshot:[a-f0-9]{64}"),
            ("evidence_revision_id", r"spec013-evidence-revision:[a-f0-9]{64}"),
            ("knowledge_snapshot_id", r"ksnp_[a-f0-9]{64}"),
            ("materiality_result_id", r"spec013-materiality-result:[a-f0-9]{64}"),
        ):
            if re.fullmatch(pattern, _text(getattr(self, field), field)) is None:
                raise ValueError(f"{field} is not a bounded public authority reference")
        if re.fullmatch(r"[a-f0-9]{64}", self.lineage_commitment) is None:
            raise ValueError("lineage_commitment must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class RcaContinuation:
    root_id: str; runtime_work_id: str; incident_id: str; stage: str; next_action: str; retry_limit: int
    created_at: datetime; updated_at: datetime; observed_at: datetime
    aggregate_id: str | None = None; attempt_id: str | None = None; try_ordinal: int | None = None
    capture_operation_id: str | None = None; retrieval_operation_id: str | None = None
    execution_operation_id: str | None = None; artifact_commit_operation_id: str | None = None
    publication_operation_id: str | None = None; retry_continuation_reference: str | None = None
    next_eligibility_at: datetime | None = None; follow_up_root_id: str | None = None
    unresolved_frontier: tuple[FollowUpRequirement, ...] = (); publication_continuation_reference: str | None = None
    source_domain: str | None = None; source_disposition: str | None = None; observation_code: str | None = None
    capture_basis: CaptureCommandBasis | None = None; revision: int = 0
    materiality_result_id: str | None = None
    refresh_basis: RefreshBasis | None = None
    follow_up_complete: bool = False
    admitted_frontier: tuple[FollowUpRequirement, ...] | None = None
    frontier_resolutions: tuple[FollowUpResolution, ...] = ()
    follow_up_wake_at: datetime | None = None
    follow_up_work_id: str | None = None
    follow_up_work_activation_revision: int | None = None
    follow_up_capture_bases: tuple[CaptureCommandBasis, ...] = ()
    follow_up_capture_snapshots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("root_id", "runtime_work_id", "incident_id"): _reference(getattr(self, field), field)
        _code(self.stage, "stage", _CODES); _code(self.next_action, "next_action", _CODES)
        if self.root_id != rca_root_id(self.incident_id): raise ValueError("root_id contradicts authoritative incident_id")
        for field in ("aggregate_id", "attempt_id", "capture_operation_id", "retrieval_operation_id", "execution_operation_id", "artifact_commit_operation_id", "publication_operation_id", "retry_continuation_reference", "follow_up_root_id", "follow_up_work_id", "publication_continuation_reference", "materiality_result_id"):
            _reference(getattr(self, field), field, optional=True)
        _code(self.source_domain, "source_domain", _SOURCE_DOMAINS, optional=True)
        _code(self.source_disposition, "source_disposition", _SOURCE_DISPOSITIONS, optional=True)
        _code(self.observation_code, "observation_code", _OBSERVATION_CODES, optional=True)
        if isinstance(self.retry_limit, bool) or not isinstance(self.retry_limit, int) or self.retry_limit < 1: raise ValueError("retry_limit must be positive")
        if self.try_ordinal is not None and (isinstance(self.try_ordinal, bool) or not isinstance(self.try_ordinal, int) or self.try_ordinal < 1): raise ValueError("try_ordinal must be a positive integer")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0: raise ValueError("revision must be non-negative")
        if self.follow_up_work_activation_revision is not None and (
            isinstance(self.follow_up_work_activation_revision, bool) or
            not isinstance(self.follow_up_work_activation_revision, int) or
            self.follow_up_work_activation_revision < 1):
            raise ValueError("follow-up work activation revision must be positive")
        if (self.follow_up_work_id is None) != (self.follow_up_work_activation_revision is None):
            raise ValueError("follow-up work identity and activation revision must be paired")
        for field in ("created_at", "updated_at", "observed_at", "next_eligibility_at", "follow_up_wake_at"):
            value = getattr(self, field)
            if value is not None: object.__setattr__(self, field, canonical_utc(value, field=field))
        if self.updated_at < self.created_at or self.observed_at < self.created_at: raise ValueError("continuation timestamps cannot precede creation")
        frontier = tuple(self.unresolved_frontier)
        if len(frontier) > _MAX_FRONTIER or len(frontier) != len(set(frontier)): raise ValueError("unresolved_frontier must be distinct and bounded")
        if not all(isinstance(item, FollowUpRequirement) for item in frontier): raise TypeError("unresolved_frontier must contain FollowUpRequirement")
        object.__setattr__(self, "unresolved_frontier", frontier)
        # S1 callers and records predate the admitted-member ledger. Their
        # unresolved frontier is the exact durable admission set at adoption.
        # Before a follow-up root exists, S1 may append more typed members via
        # dataclasses.replace; retain both the old ledger and those members.
        admitted = (tuple(frontier) if self.admitted_frontier is None else
                    tuple(self.admitted_frontier))
        proofs = tuple(self.frontier_resolutions)
        if (self.follow_up_root_id is None and not proofs and not self.follow_up_complete):
            admitted = admitted + tuple(item for item in frontier if item not in admitted)
        if (len(admitted) > _MAX_FRONTIER or len(admitted) != len(set(admitted)) or
            not all(isinstance(item, FollowUpRequirement) for item in admitted) or
            not set(frontier).issubset(admitted)):
            raise ValueError("admitted frontier must retain every unresolved member")
        if (len(proofs) > _MAX_FRONTIER or len({item.member for item in proofs}) != len(proofs) or
            not all(isinstance(item, FollowUpResolution) and item.member in admitted for item in proofs)):
            raise ValueError("frontier resolutions must identify admitted members")
        object.__setattr__(self, "admitted_frontier", admitted)
        object.__setattr__(self, "frontier_resolutions", proofs)
        if (self.follow_up_wake_at is None and self.next_eligibility_at is not None
            and any(item.requirement_type == "POST_CONTEXT" for item in admitted)):
            object.__setattr__(self, "follow_up_wake_at", self.next_eligibility_at)
        if self.capture_basis is not None:
            if self.capture_basis.root_id != self.root_id or self.capture_basis.incident_id != self.incident_id: raise ValueError("capture_basis contradicts continuation root")
            if self.capture_operation_id != self.capture_basis.capture_operation_id: raise ValueError("capture basis and capture_operation_id contradict")
        captures = tuple(self.follow_up_capture_bases)
        snapshots = tuple(self.follow_up_capture_snapshots)
        if (not all(isinstance(basis, CaptureCommandBasis) for basis in captures)
            or len(captures) - len(snapshots) not in (0, 1)
            or len({basis.capture_operation_id for basis in captures}) != len(captures)):
            raise ValueError("follow-up capture continuity is incomplete or duplicated")
        for basis in captures:
            if (not isinstance(basis, CaptureCommandBasis) or basis.root_id != self.root_id
                or basis.incident_id != self.incident_id or basis.purpose != "FOLLOW_UP"
                or basis.bounds_policy_version is None):
                raise ValueError("follow-up capture basis contradicts the RCA subject")
        for snapshot in snapshots:
            if re.fullmatch(r"spec013-evidence-snapshot:[a-f0-9]{64}", snapshot) is None:
                raise ValueError("follow-up capture lacks a public B Snapshot reference")
        object.__setattr__(self, "follow_up_capture_bases", captures)
        object.__setattr__(self, "follow_up_capture_snapshots", snapshots)
        if self.refresh_basis is not None and not isinstance(self.refresh_basis, RefreshBasis):
            raise TypeError("refresh_basis must be RefreshBasis")
        if not isinstance(self.follow_up_complete, bool) or (self.follow_up_complete and frontier):
            raise ValueError("completed follow-up must have an empty frontier")


@dataclass(frozen=True, slots=True)
class RcaContinuationCorruption:
    record_key: str; error_code: str; detail: str
@dataclass(frozen=True, slots=True)
class RcaContinuationEnumeration:
    records: tuple[RcaContinuation, ...]; isolated_corruptions: tuple[RcaContinuationCorruption, ...]
class RcaContinuationStoreError(RuntimeError): pass
class RcaRepairRequiredError(RcaContinuationStoreError): pass
class RcaContinuationStoreIntegrityError(RcaRepairRequiredError): pass
class RcaContinuationRecordCorruptError(RcaRepairRequiredError): pass
class RcaContinuationConcurrencyError(RcaContinuationStoreError): pass
class RcaContinuationNotFoundError(RcaContinuationStoreError): pass
class ContradictoryRcaContinuationError(RcaRepairRequiredError): pass


class SqliteRcaContinuationStore:
    """RCA extension tables in the existing SPEC-011 Runtime Work SQLite D2."""
    def __init__(self, database_path: str | Path) -> None:
        self._path, self._closed = str(database_path), False
        if self._path == ":memory:": raise ValueError("S1 continuation requires a durable Runtime Work database path")
        try:
            runtime = SqliteRuntimeWorkStore(self._path); runtime.close()
            self._connection = sqlite3.connect(self._path, isolation_level=None); self._connection.row_factory = sqlite3.Row
            self._initialize()
        except (sqlite3.Error, OSError) as exc:
            self._close_on_error(); raise RcaContinuationStoreIntegrityError("RCA continuation store cannot be opened reliably") from exc
        except BaseException:
            self._close_on_error(); raise
    def close(self) -> None:
        if not self._closed: self._connection.close(); self._closed = True
    def __enter__(self) -> "SqliteRcaContinuationStore":
        self._open()
        return self
    def __exit__(self, *_: object) -> None:
        self.close()
    @property
    def database_path(self) -> str: return self._path
    def create(self, record: RcaContinuation) -> RcaContinuation:
        self._open()
        if not isinstance(record, RcaContinuation) or record.revision != 0: raise ValueError("new continuation must have revision 0")
        try:
            with self._write():
                row = self._row(record.root_id)
                if row is not None:
                    existing = self._decode_or_raise(row)
                    if _stable_identity(existing) != _stable_identity(record): raise ContradictoryRcaContinuationError("root has contradictory stable identity")
                    if replace(existing, revision=0) == record: return existing
                    raise ContradictoryRcaContinuationError("root already has different continuation state")
                persisted = replace(record, revision=1)
                self._connection.execute("INSERT INTO rca_runtime_continuations(root_id, record_json, revision) VALUES (?, ?, ?)", (persisted.root_id, _encode_record(persisted), persisted.revision))
                return persisted
        except sqlite3.Error as exc: raise RcaContinuationStoreIntegrityError("RCA continuation create failed") from exc
    def get(self, root_id: str) -> RcaContinuation | None:
        self._open(); _text(root_id, "root_id")
        try:
            with self._read_snapshot():
                self._require_integrity(); row = self._row(root_id)
                return None if row is None else self._decode_or_raise(row)
        except sqlite3.Error as exc: raise RcaContinuationStoreIntegrityError("RCA continuation lookup is unreadable") from exc
    def enumerate_all(self) -> RcaContinuationEnumeration:
        self._open()
        try:
            with self._read_snapshot():
                self._require_integrity(); valid, corrupt = [], []
                for row in self._connection.execute("SELECT rowid, * FROM rca_runtime_continuations ORDER BY root_id, rowid").fetchall():
                    try: valid.append(self._decode_or_raise(row))
                    except RcaContinuationRecordCorruptError as exc: corrupt.append(RcaContinuationCorruption(row["root_id"], "MALFORMED_RCA_CONTINUATION", str(exc)))
                return RcaContinuationEnumeration(tuple(valid), tuple(corrupt))
        except sqlite3.Error as exc: raise RcaContinuationStoreIntegrityError("RCA continuation enumeration is unreadable") from exc
    def update(self, record: RcaContinuation, *, expected_revision: int) -> RcaContinuation:
        return self._update(record, expected_revision=expected_revision, allow_capture_freeze=False)
    def advance_publication(self, record: RcaContinuation, *, expected_revision: int,
                            expected_work: RuntimeWorkRecord,
                            replace_resolved_publication: bool = False) -> RcaContinuation:
        """Bind an A-proven intent without crossing a concurrent Runtime fence."""
        if (record.stage != "PUBLICATION" or record.next_action != "PUBLICATION"
            or record.publication_operation_id is None):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: incomplete publication continuation")
        return self._update(record, expected_revision=expected_revision,
                            allow_capture_freeze=False, expected_work=expected_work,
                            allow_publication_advance=replace_resolved_publication)
    def complete_publication_budget(self, root_id: str, budget: RuntimeWorkRecord, *,
                                    expected_work: RuntimeWorkRecord,
                                    expected_revision: int, observed_at: datetime) -> RuntimeWorkRecord:
        """Finish existing generation bookkeeping under the same D2 fence."""
        self._open()
        with self._write():
            self._require_integrity()
            row = self._row(root_id)
            if row is None: raise RcaContinuationNotFoundError(root_id)
            state = self._decode_or_raise(row)
            if state.revision != expected_revision:
                raise RcaContinuationConcurrencyError("RCA publication continuation changed")
            if (expected_work.status is not RuntimeWorkStatus.OUTSTANDING or
                self._active_work(state) != expected_work):
                raise RuntimeWorkConcurrencyError("RCA publication lost Runtime fencing authority")
            budget_row = self._connection.execute(
                "SELECT rowid, * FROM runtime_work_records WHERE work_id=?", (budget.work_id,)).fetchone()
            if budget_row is None or _decode_work(budget_row) != budget:
                raise RuntimeWorkConcurrencyError("RCA generation budget changed")
            if (state.stage != "PUBLICATION" or state.publication_operation_id is None
                or budget.work_kind is not RuntimeWorkKind.RCA_ATTEMPT
                or budget.workflow_operation_id != root_id or budget.incident_id != state.incident_id
                or budget.status is not RuntimeWorkStatus.OUTSTANDING):
                raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: invalid publication budget")
            observed = canonical_utc(observed_at, field="observed_at")
            completed = replace(budget, status=RuntimeWorkStatus.COMPLETED,
                next_action="NONE", next_retry_at=None, updated_at=observed,
                observed_at=observed, revision=budget.revision + 1)
            _validate_work_transition(budget, completed)
            values = _encode_work(completed)
            cursor = self._connection.execute(
                "UPDATE runtime_work_records SET status=:status, next_action=:next_action, "
                "next_retry_at=:next_retry_at, updated_at=:updated_at, "
                "observed_at=:observed_at, revision=:revision "
                "WHERE work_id=:work_id AND revision=:expected_revision",
                {**values, "expected_revision": budget.revision})
            if cursor.rowcount != 1:
                raise RuntimeWorkConcurrencyError("RCA generation budget compare-and-swap failed")
            return completed
    def resolve_follow_up(self, record: RcaContinuation, *, expected_revision: int) -> RcaContinuation:
        """CAS for a coordinator that has fresh-read every frontier member."""
        return self._update(record, expected_revision=expected_revision,
                            allow_capture_freeze=False, allow_frontier_resolution=True)
    def admit_follow_up(self, record: RcaContinuation, *, expected_revision: int) -> RcaContinuation:
        """Admit a member and its Runtime work continuity in one D2 commit."""
        self._open()
        try:
            with self._write():
                self._require_integrity()
                row = self._row(record.root_id)
                if row is None: raise RcaContinuationNotFoundError(record.root_id)
                old = self._decode_or_raise(row)
                if old.revision != expected_revision:
                    raise RcaContinuationConcurrencyError("RCA continuation revision changed")
                if (record.follow_up_work_id != old.follow_up_work_id or
                    record.follow_up_work_activation_revision != old.follow_up_work_activation_revision or
                    record.revision not in (0, expected_revision) or
                    record.follow_up_complete or
                    not (set(record.admitted_frontier) - set(old.admitted_frontier)).issubset(
                        record.unresolved_frontier)):
                    raise ContradictoryRcaContinuationError("invalid atomic RCA admission candidate")
                if not set(old.admitted_frontier) < set(record.admitted_frontier):
                    raise ContradictoryRcaContinuationError("admission must add a new member")
                work = self._active_work(old)
                if work.status is RuntimeWorkStatus.COMPLETED:
                    if old.unresolved_frontier or (old.admitted_frontier and not old.follow_up_complete):
                        raise ContradictoryRcaContinuationError(
                            "REPAIR_REQUIRED: completed work has unresolved frontier")
                    follow_root = record.follow_up_root_id
                    if follow_root is None or follow_root != old.follow_up_root_id and old.follow_up_root_id is not None:
                        raise ContradictoryRcaContinuationError("follow-up root changed")
                    new_id = runtime_work_id(RuntimeWorkKind.RCA_FOLLOW_UP, follow_root,
                                             str(expected_revision + 1))
                    created = max(record.updated_at, record.observed_at, work.updated_at)
                    new_work = RuntimeWorkRecord(
                        new_id, RuntimeWorkKind.RCA_FOLLOW_UP, work.event_id,
                        "FOLLOW_UP", "FOLLOW_UP", 0, work.retry_limit,
                        RuntimeWorkStatus.OUTSTANDING, created, created, created,
                        incident_id=old.incident_id, operation_id=follow_root)
                    self._insert_work(new_work)
                    record = replace(record, follow_up_work_id=new_id,
                                     follow_up_work_activation_revision=expected_revision + 1)
                elif work.status is not RuntimeWorkStatus.OUTSTANDING:
                    raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: RCA work is terminal")
                _validate_update(old, record, allow_capture_freeze=False,
                                 allow_follow_up_work_rotation=True)
                persisted = replace(record, revision=expected_revision + 1)
                self._connection.execute(
                    "UPDATE rca_runtime_continuations SET record_json=?, revision=? WHERE root_id=? AND revision=?",
                    (_encode_record(persisted), persisted.revision, persisted.root_id, expected_revision))
                return persisted
        except sqlite3.Error as exc:
            raise RcaContinuationStoreIntegrityError("atomic RCA admission failed") from exc
    def complete_rca_follow_up_if_frontier_unchanged(
        self, root_id: str, *, expected_work_revision: int,
        expected_frontier_revision: int, observed_at: datetime,
    ) -> RuntimeWorkRecord:
        """Conditionally complete the bound RCA work against one frontier snapshot."""
        self._open()
        observed = canonical_utc(observed_at, field="observed_at")
        try:
            with self._write():
                self._require_integrity()
                row = self._row(root_id)
                if row is None: raise RcaContinuationNotFoundError(root_id)
                state = self._decode_or_raise(row)
                if state.revision != expected_frontier_revision:
                    raise RcaContinuationConcurrencyError("RCA frontier revision changed")
                work = self._active_work(state)
                if work.revision != expected_work_revision:
                    raise RuntimeWorkConcurrencyError("RCA work revision changed")
                if not _frontier_completion_safe(state):
                    raise ContradictoryRcaContinuationError(
                        "REPAIR_REQUIRED: full RCA frontier is not completion-safe")
                if work.status is RuntimeWorkStatus.COMPLETED:
                    return work
                if work.status is not RuntimeWorkStatus.OUTSTANDING or observed < work.updated_at:
                    raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: RCA work cannot complete")
                completed = replace(work, status=RuntimeWorkStatus.COMPLETED,
                                    next_action="NONE", next_retry_at=None,
                                    updated_at=observed, observed_at=observed,
                                    revision=work.revision + 1)
                _validate_work_transition(work, completed)
                values = _encode_work(completed)
                cursor = self._connection.execute(
                    "UPDATE runtime_work_records SET status=:status, next_action=:next_action, "
                    "next_retry_at=:next_retry_at, updated_at=:updated_at, "
                    "observed_at=:observed_at, revision=:revision "
                    "WHERE work_id=:work_id AND revision=:expected_revision",
                    {**values, "expected_revision": work.revision})
                if cursor.rowcount != 1:
                    raise RuntimeWorkConcurrencyError("RCA work compare-and-swap failed")
                return completed
        except sqlite3.Error as exc:
            raise RcaContinuationStoreIntegrityError("atomic RCA completion failed") from exc
    def get_follow_up_work(self, root_id: str) -> RuntimeWorkRecord:
        """Classify the current RCA work binding for restart and replay."""
        self._open()
        with self._read_snapshot():
            self._require_integrity()
            row = self._row(root_id)
            if row is None: raise RcaContinuationNotFoundError(root_id)
            return self._active_work(self._decode_or_raise(row))
    def _active_work(self, state: RcaContinuation) -> RuntimeWorkRecord:
        initial_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, state.root_id)
        if state.runtime_work_id != initial_id:
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: initial work binding changed")
        if (state.follow_up_root_id is not None and state.follow_up_root_id !=
            rca_child_operation_id(state.root_id, state.incident_id, "FOLLOW_UP")):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: follow-up root is not stable")
        initial_row = self._connection.execute(
            "SELECT rowid, * FROM runtime_work_records WHERE work_id=?", (initial_id,)).fetchone()
        if initial_row is None:
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: initial RCA work is absent")
        try: initial = _decode_work(initial_row)
        except (TypeError, ValueError, KeyError) as exc:
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: initial RCA work is corrupt") from exc
        if (initial.work_kind is not RuntimeWorkKind.RCA_INITIAL or
            initial.incident_id != state.incident_id or initial.operation_id != state.root_id or
            initial.retry_limit != state.retry_limit or initial.created_at != state.created_at):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: initial RCA work contradicts root")
        work_id = state.follow_up_work_id or initial_id
        if state.follow_up_work_id is not None and (
            state.follow_up_root_id is None or
            state.follow_up_work_id != runtime_work_id(
                RuntimeWorkKind.RCA_FOLLOW_UP, state.follow_up_root_id,
                str(state.follow_up_work_activation_revision)) or
            state.follow_up_work_activation_revision > state.revision):
            raise ContradictoryRcaContinuationError(
                "REPAIR_REQUIRED: follow-up work activation is not reconstructible")
        row = (initial_row if work_id == initial_id else self._connection.execute(
            "SELECT rowid, * FROM runtime_work_records WHERE work_id=?", (work_id,)).fetchone())
        if row is None:
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: bound RCA work is absent")
        try: work = _decode_work(row)
        except (TypeError, ValueError, KeyError) as exc:
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: bound RCA work is corrupt") from exc
        expected_kind = RuntimeWorkKind.RCA_FOLLOW_UP if state.follow_up_work_id else RuntimeWorkKind.RCA_INITIAL
        expected_operation = state.follow_up_root_id if state.follow_up_work_id else state.root_id
        if (work.work_kind is not expected_kind or work.incident_id != state.incident_id or
            work.operation_id != expected_operation or work.retry_limit != state.retry_limit or
            work.event_id != initial.event_id or
            (state.follow_up_work_id and initial.status is not RuntimeWorkStatus.COMPLETED)):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: RCA work binding contradicts root")
        outstanding = self._connection.execute(
            "SELECT rowid, * FROM runtime_work_records WHERE work_kind=? AND incident_id=? AND status=?",
            (RuntimeWorkKind.RCA_FOLLOW_UP.value, state.incident_id,
             RuntimeWorkStatus.OUTSTANDING.value)).fetchall()
        if len(outstanding) > 1 or (outstanding and outstanding[0]["work_id"] != work_id):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: parallel follow-up work")
        if work.status is RuntimeWorkStatus.COMPLETED and not _frontier_completion_safe(state):
            raise ContradictoryRcaContinuationError(
                "REPAIR_REQUIRED: completed work has unresolved or provisional frontier")
        return work
    def _insert_work(self, work: RuntimeWorkRecord) -> None:
        values = _encode_work(replace(work, revision=1))
        columns = tuple(values)
        self._connection.execute(
            f"INSERT INTO runtime_work_records ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + name for name in columns)})", values)
    def rotate_refresh_basis(self, record: RcaContinuation, *, expected_revision: int) -> RcaContinuation:
        """Replace a resolved refresh reference after a later A Current baseline is read."""
        return self._update(record, expected_revision=expected_revision,
                            allow_capture_freeze=False, allow_basis_rotation=True)
    def freeze_capture_basis(self, root_id: str, basis: CaptureCommandBasis, *, observed_at: datetime, expected_revision: int | None = None,
                             follow_up: bool = False) -> RcaContinuation:
        self._open()
        if not isinstance(basis, CaptureCommandBasis): raise TypeError("basis must be CaptureCommandBasis")
        current = self.get(root_id)
        if current is None: raise RcaRepairRequiredError("REPAIR_REQUIRED: continuation absence does not prove no RCA obligation")
        if follow_up:
            if basis.purpose != "FOLLOW_UP" or basis.bounds_policy_version is None:
                raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: incomplete follow-up capture basis")
            if len(current.follow_up_capture_bases) > len(current.follow_up_capture_snapshots):
                if current.follow_up_capture_bases[-1] == basis: return current
                raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: pending follow-up capture is already frozen")
            if basis in current.follow_up_capture_bases:
                return current
            if expected_revision is not None and current.revision != expected_revision:
                raise RcaContinuationConcurrencyError("RCA continuation revision changed")
            observed = canonical_utc(observed_at, field="observed_at")
            return self._update(replace(current,
                follow_up_root_id=rca_child_operation_id(root_id, current.incident_id, "FOLLOW_UP"),
                follow_up_capture_bases=current.follow_up_capture_bases + (basis,),
                updated_at=observed, observed_at=observed), expected_revision=current.revision,
                allow_capture_freeze=False, allow_follow_up_capture=True,
                allow_frontier_resolution=True)
        if current.capture_basis is not None:
            if current.capture_basis == basis: return current
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: capture basis is already frozen and differs")
        if expected_revision is not None and current.revision != expected_revision: raise RcaContinuationConcurrencyError("RCA continuation revision changed")
        observed = canonical_utc(observed_at, field="observed_at")
        if observed < current.updated_at: raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: capture observation moved backwards")
        return self._update(replace(current, capture_basis=basis, capture_operation_id=derive_capture_operation_id(basis), updated_at=observed, observed_at=observed), expected_revision=current.revision, allow_capture_freeze=True)
    def require_capture_command_basis(self, root_id: str, *, follow_up: bool = False) -> CaptureCommandBasis:
        current = self.get(root_id)
        if follow_up:
            if current is None or len(current.follow_up_capture_bases) != len(current.follow_up_capture_snapshots) + 1:
                raise RcaRepairRequiredError("REPAIR_REQUIRED: pending frozen follow-up Capture Command Basis is absent")
            return current.follow_up_capture_bases[-1]
        if current is None or current.capture_basis is None or current.capture_operation_id is None: raise RcaRepairRequiredError("REPAIR_REQUIRED: complete frozen Capture Command Basis is absent")
        if current.capture_operation_id != derive_capture_operation_id(current.capture_basis): raise RcaRepairRequiredError("REPAIR_REQUIRED: frozen Capture Command Basis contradicts capture identity")
        return current.capture_basis
    def resolve_follow_up_capture(self, root_id: str, basis: CaptureCommandBasis,
                                  snapshot_id: str, *, observed_at: datetime,
                                  expected_revision: int) -> RcaContinuation:
        """Retain the frozen command after its B Snapshot enters the E3 frontier."""
        current = self.get(root_id)
        if current is None or current.revision != expected_revision:
            raise RcaContinuationConcurrencyError("RCA continuation revision changed")
        if (len(current.follow_up_capture_bases) != len(current.follow_up_capture_snapshots) + 1
            or current.follow_up_capture_bases[-1] != basis):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: pending capture basis changed")
        observed = canonical_utc(observed_at, field="observed_at")
        return self._update(replace(current,
            follow_up_capture_snapshots=current.follow_up_capture_snapshots + (snapshot_id,),
            updated_at=observed, observed_at=observed), expected_revision=expected_revision,
            allow_capture_freeze=False, allow_follow_up_capture=True)
    def _update(self, record: RcaContinuation, *, expected_revision: int, allow_capture_freeze: bool,
                allow_frontier_resolution: bool = False,
                allow_basis_rotation: bool = False,
                allow_follow_up_capture: bool = False,
                expected_work: RuntimeWorkRecord | None = None,
                allow_publication_advance: bool = False) -> RcaContinuation:
        self._open()
        if not isinstance(record, RcaContinuation): raise TypeError("record must be RcaContinuation")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1: raise ValueError("expected_revision must be positive")
        if record.revision not in (0, expected_revision): raise RcaContinuationConcurrencyError("candidate revision does not match expectation")
        try:
            with self._write():
                self._require_integrity(); row = self._row(record.root_id)
                if row is None: raise RcaContinuationNotFoundError(record.root_id)
                old = self._decode_or_raise(row)
                if old.revision != expected_revision: raise RcaContinuationConcurrencyError("RCA continuation revision changed")
                if expected_work is not None and (
                    expected_work.status is not RuntimeWorkStatus.OUTSTANDING or
                    self._active_work(old) != expected_work):
                    raise RuntimeWorkConcurrencyError("RCA publication lost Runtime fencing authority")
                if (record.follow_up_work_id != old.follow_up_work_id or
                    record.follow_up_work_activation_revision != old.follow_up_work_activation_revision):
                    raise ContradictoryRcaContinuationError(
                        "REPAIR_REQUIRED: follow-up work binding requires store-owned transition")
                added = set(record.admitted_frontier) - set(old.admitted_frontier)
                if added and old.follow_up_root_id is not None:
                    raise ContradictoryRcaContinuationError(
                        "REPAIR_REQUIRED: rooted frontier admission requires atomic D2 operation")
                if added and old.follow_up_root_id is None:
                    work_row = self._connection.execute(
                        "SELECT status FROM runtime_work_records WHERE work_id=?",
                        (old.runtime_work_id,)).fetchone()
                    if work_row is not None and work_row["status"] != RuntimeWorkStatus.OUTSTANDING.value:
                        raise ContradictoryRcaContinuationError(
                            "REPAIR_REQUIRED: terminal work cannot receive legacy frontier admission")
                if old.follow_up_root_id is not None or allow_follow_up_capture:
                    active = self._active_work(old)
                    if active.status is RuntimeWorkStatus.COMPLETED and not _frontier_completion_safe(record):
                        if not allow_frontier_resolution or added:
                            raise ContradictoryRcaContinuationError(
                                "REPAIR_REQUIRED: terminal work cannot acquire unresolved frontier")
                        activation = expected_revision + 1
                        follow_root = record.follow_up_root_id
                        if follow_root is None:
                            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: follow-up capture lacks singleton root")
                        new_id = runtime_work_id(RuntimeWorkKind.RCA_FOLLOW_UP,
                                                 follow_root, str(activation))
                        created = max(record.updated_at, record.observed_at, active.updated_at)
                        self._insert_work(RuntimeWorkRecord(
                            new_id, RuntimeWorkKind.RCA_FOLLOW_UP, active.event_id,
                            "FOLLOW_UP", "FOLLOW_UP", 0, active.retry_limit,
                            RuntimeWorkStatus.OUTSTANDING, created, created, created,
                            incident_id=old.incident_id, operation_id=follow_root))
                        record = replace(record, follow_up_work_id=new_id,
                                         follow_up_work_activation_revision=activation)
                _validate_update(old, record, allow_capture_freeze=allow_capture_freeze,
                                  allow_frontier_resolution=allow_frontier_resolution,
                                  allow_basis_rotation=allow_basis_rotation,
                                  allow_follow_up_capture=allow_follow_up_capture,
                                  allow_publication_advance=allow_publication_advance,
                                  allow_follow_up_work_rotation=allow_frontier_resolution)
                if replace(old, revision=0) == replace(record, revision=0): return old
                persisted = replace(record, revision=expected_revision + 1)
                cursor = self._connection.execute("UPDATE rca_runtime_continuations SET record_json=?, revision=? WHERE root_id=? AND revision=?", (_encode_record(persisted), persisted.revision, persisted.root_id, expected_revision))
                if cursor.rowcount != 1: raise RcaContinuationConcurrencyError("RCA continuation compare-and-swap failed")
                return persisted
        except sqlite3.Error as exc: raise RcaContinuationStoreIntegrityError("RCA continuation update failed") from exc
    def _initialize(self) -> None:
        # Concurrent openers must classify the schema after acquiring the
        # same D2 write lock, rather than act on a stale pre-lock absence read.
        with self._write():
            names = self._table_names()
            if names == _RUNTIME_TABLES:
                self._connection.execute("CREATE TABLE rca_continuation_store_metadata (singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton=1), schema_version INTEGER NOT NULL)")
                self._connection.execute("INSERT INTO rca_continuation_store_metadata VALUES(1,1)")
                self._connection.execute("CREATE TABLE rca_runtime_continuations (root_id TEXT PRIMARY KEY NOT NULL, record_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1))")
            elif names != _TABLES:
                raise RcaContinuationStoreIntegrityError("database is not the existing Runtime D2 with a valid RCA extension")
            self._validate_schema()
    def _validate_schema(self) -> None:
        self._require_integrity()
        if self._column_shape("rca_continuation_store_metadata") != _METADATA_COLUMNS: raise RcaContinuationStoreIntegrityError("malformed RCA continuation metadata schema")
        if self._column_shape("rca_runtime_continuations") != _RECORD_COLUMNS: raise RcaContinuationStoreIntegrityError("malformed RCA continuation record schema")
        rows = self._connection.execute("SELECT singleton, schema_version FROM rca_continuation_store_metadata").fetchall()
        if len(rows) != 1 or rows[0]["singleton"] != 1 or rows[0]["schema_version"] != _SCHEMA_VERSION: raise RcaContinuationStoreIntegrityError("unsupported or malformed RCA continuation schema")
    def _require_integrity(self) -> None:
        rows = self._connection.execute("PRAGMA quick_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok": raise RcaContinuationStoreIntegrityError("SQLite integrity check failed")
    def _table_names(self) -> frozenset[str]: return frozenset(row[0] for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    def _column_shape(self, table: str) -> tuple[tuple[str, str, int, int], ...]: return tuple((row[1], row[2].upper(), row[3], row[5]) for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall())
    def _row(self, root_id: str): return self._connection.execute("SELECT rowid, * FROM rca_runtime_continuations WHERE root_id=?", (root_id,)).fetchone()
    def _decode_or_raise(self, row: sqlite3.Row) -> RcaContinuation:
        try:
            value = _decode_record(row["record_json"], row["revision"])
            if value.root_id != row["root_id"]: raise ValueError("row key contradicts payload root_id")
            return value
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc: raise RcaContinuationRecordCorruptError(f"REPAIR_REQUIRED: corrupt RCA continuation {row['root_id']}: {exc}") from exc
    def _open(self) -> None:
        if self._closed: raise RcaContinuationStoreError("RCA continuation store is closed")
    def _close_on_error(self) -> None:
        if (connection := getattr(self, "_connection", None)) is not None: connection.close()
        self._closed = True
    @contextmanager
    def _write(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try: yield
        except BaseException: self._connection.rollback(); raise
        else: self._connection.commit()
    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        self._connection.execute("BEGIN")
        try: yield
        except BaseException: self._connection.rollback(); raise
        else: self._connection.commit()


def _stable_identity(value: RcaContinuation) -> tuple[object, ...]: return (value.root_id, value.runtime_work_id, value.incident_id, value.retry_limit, value.created_at)
def _frontier_completion_safe(value: RcaContinuation) -> bool:
    return (len(value.follow_up_capture_bases) == len(value.follow_up_capture_snapshots) and
            not value.unresolved_frontier and
            (not value.admitted_frontier or value.follow_up_complete) and
            {proof.member for proof in value.frontier_resolutions} == set(value.admitted_frontier) and
            all(proof.state == "VALIDATED" for proof in value.frontier_resolutions))
def _operation_identities(value: RcaContinuation) -> tuple[object, ...]: return (value.capture_operation_id, value.retrieval_operation_id, value.execution_operation_id, value.artifact_commit_operation_id, value.publication_operation_id, value.follow_up_root_id)
def _validate_update(old: RcaContinuation, new: RcaContinuation, *, allow_capture_freeze: bool,
                      allow_frontier_resolution: bool = False,
                      allow_basis_rotation: bool = False,
                      allow_follow_up_work_rotation: bool = False,
                      allow_follow_up_capture: bool = False,
                      allow_publication_advance: bool = False) -> None:
    if _stable_identity(old) != _stable_identity(new): raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: stable RCA identity is immutable")
    if new.updated_at < old.updated_at or new.observed_at < old.observed_at: raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: continuation time cannot move backwards")
    if (old.follow_up_capture_bases != new.follow_up_capture_bases or
        old.follow_up_capture_snapshots != new.follow_up_capture_snapshots):
        if (not allow_follow_up_capture or
            new.follow_up_capture_bases[:len(old.follow_up_capture_bases)] != old.follow_up_capture_bases or
            new.follow_up_capture_snapshots[:len(old.follow_up_capture_snapshots)] != old.follow_up_capture_snapshots or
            (len(new.follow_up_capture_bases) - len(old.follow_up_capture_bases),
             len(new.follow_up_capture_snapshots) - len(old.follow_up_capture_snapshots)) not in ((1, 0), (0, 1))):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: frozen follow-up capture history was rewritten")
    if old.capture_basis is not None:
        if old.capture_basis != new.capture_basis or old.capture_operation_id != new.capture_operation_id: raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: frozen Capture Command Basis was rewritten")
    elif old.capture_basis != new.capture_basis:
        if not allow_capture_freeze or new.capture_basis is None or new.capture_operation_id != derive_capture_operation_id(new.capture_basis): raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: Capture Command Basis may only be set by freeze")
    for name, old_value, new_value in zip(
        ("retrieval", "execution", "artifact", "publication", "follow_up"),
        _operation_identities(old)[1:], _operation_identities(new)[1:]):
        if old_value is not None and old_value != new_value:
            if name == "publication" and allow_publication_advance:
                continue
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: operation identity was rewritten")
    if old.materiality_result_id is not None and old.materiality_result_id != new.materiality_result_id:
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: Materiality authority reference was rewritten")
    if old.follow_up_work_id != new.follow_up_work_id and not allow_follow_up_work_rotation:
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: follow-up work binding requires atomic admission")
    if (old.follow_up_work_activation_revision != new.follow_up_work_activation_revision
        and not allow_follow_up_work_rotation):
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: follow-up activation requires atomic admission")
    if (old.follow_up_wake_at is not None and
        (new.follow_up_wake_at is None or new.follow_up_wake_at > old.follow_up_wake_at)):
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: original follow-up wake was lost or delayed")
    if not set(old.admitted_frontier).issubset(new.admitted_frontier):
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: admitted frontier member was discarded")
    if not set(new.unresolved_frontier).issubset(new.admitted_frontier):
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: unresolved member lacks durable admission")
    if any(member not in {proof.member for proof in new.frontier_resolutions}
           for member in set(old.unresolved_frontier) - set(new.unresolved_frontier)):
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: frontier member was removed without proof")
    if not allow_frontier_resolution and not set(old.unresolved_frontier).issubset(new.unresolved_frontier):
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: unresolved frontier member was discarded")
    if new.follow_up_complete and not old.follow_up_complete and not allow_frontier_resolution:
        raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: follow-up completion requires full-frontier resolution")
    if old.refresh_basis is not None and old.refresh_basis != new.refresh_basis:
        if (not allow_basis_rotation or new.refresh_basis is None
            or old.refresh_basis.requirement in old.unresolved_frontier
            or old.refresh_basis.baseline_version_id == new.refresh_basis.baseline_version_id):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: frozen refresh basis was rewritten")
def _encode_basis(value: CaptureCommandBasis) -> dict[str, object]: return {"incident_id": value.incident_id, "root_id": value.root_id, "purpose": value.purpose, "snapshot_at": format_utc(value.snapshot_at), "capture_contract_version": value.capture_contract_version, "canonicalization_version": value.canonicalization_version, "source_policy_version": value.source_policy_version, "configuration_identity": value.configuration_identity, "collection_boundary_references": list(value.collection_boundary_references), "bounds_policy_version": value.bounds_policy_version}
def _encode_record(value: RcaContinuation) -> str:
    data = {name: getattr(value, name) for name in value.__dataclass_fields__}
    data["follow_up_capture_contract_version"] = 1
    for name in ("created_at", "updated_at", "observed_at", "next_eligibility_at", "follow_up_wake_at"): data[name] = None if data[name] is None else format_utc(data[name])
    data["capture_basis"] = None if value.capture_basis is None else _encode_basis(value.capture_basis)
    data["follow_up_capture_bases"] = [_encode_basis(basis) for basis in value.follow_up_capture_bases]
    data["refresh_basis"] = None if value.refresh_basis is None else {
        **{name: getattr(value.refresh_basis, name) for name in value.refresh_basis.__dataclass_fields__
           if name != "requirement"},
        "requirement": {"requirement_type": value.refresh_basis.requirement.requirement_type,
                        "reference": value.refresh_basis.requirement.reference},
    }
    data["unresolved_frontier"] = [{"requirement_type": item.requirement_type, "reference": item.reference} for item in value.unresolved_frontier]
    data["admitted_frontier"] = [{"requirement_type": item.requirement_type, "reference": item.reference} for item in value.admitted_frontier]
    data["frontier_resolutions"] = [{**{name: getattr(item, name) for name in item.__dataclass_fields__ if name != "member"},
        "member": {"requirement_type": item.member.requirement_type, "reference": item.member.reference}}
        for item in value.frontier_resolutions]
    return json.dumps(data, sort_keys=True, separators=(",", ":"))
def _decode_record(payload: str, revision: int) -> RcaContinuation:
    data = json.loads(payload)
    if not isinstance(data, dict) or data.pop("revision", None) != revision: raise ValueError("persisted continuation revision contradicts row revision")
    capture_version = data.pop("follow_up_capture_contract_version", None)
    if capture_version not in (None, 1) or (capture_version == 1 and not {
        "follow_up_capture_bases", "follow_up_capture_snapshots"}.issubset(data)):
        raise ValueError("follow-up capture continuity is missing or unsupported")
    if ("follow_up_capture_bases" in data) != ("follow_up_capture_snapshots" in data):
        raise ValueError("follow-up capture continuity is partially missing")
    if ("admitted_frontier" not in data and data.get("follow_up_root_id") is not None
        and data.get("follow_up_complete")):
        raise ValueError("legacy completed frontier has no reconstructable admitted members")
    for name in ("created_at", "updated_at", "observed_at"): data[name] = parse_utc(data[name], field=name)
    if data["next_eligibility_at"] is not None: data["next_eligibility_at"] = parse_utc(data["next_eligibility_at"], field="next_eligibility_at")
    if data.get("follow_up_wake_at") is not None:
        data["follow_up_wake_at"] = parse_utc(data["follow_up_wake_at"], field="follow_up_wake_at")
    data["unresolved_frontier"] = tuple(FollowUpRequirement(**item) for item in data["unresolved_frontier"])
    data["admitted_frontier"] = (None if "admitted_frontier" not in data else
        tuple(FollowUpRequirement(**item) for item in data["admitted_frontier"]))
    data["frontier_resolutions"] = tuple(FollowUpResolution(**{**item, "member": FollowUpRequirement(**item["member"])})
        for item in data.get("frontier_resolutions", ()))
    if data["capture_basis"] is not None:
        basis = dict(data["capture_basis"]); basis["snapshot_at"] = parse_utc(basis["snapshot_at"], field="snapshot_at"); basis["collection_boundary_references"] = tuple(basis["collection_boundary_references"]); data["capture_basis"] = CaptureCommandBasis(**basis)
    captures = []
    for raw in data.get("follow_up_capture_bases", ()):
        basis = dict(raw)
        basis["snapshot_at"] = parse_utc(basis["snapshot_at"], field="snapshot_at")
        basis["collection_boundary_references"] = tuple(basis["collection_boundary_references"])
        captures.append(CaptureCommandBasis(**basis))
    data["follow_up_capture_bases"] = tuple(captures)
    data["follow_up_capture_snapshots"] = tuple(data.get("follow_up_capture_snapshots", ()))
    refresh = data.get("refresh_basis")
    if refresh is not None:
        refresh = dict(refresh); refresh["requirement"] = FollowUpRequirement(**refresh["requirement"])
        data["refresh_basis"] = RefreshBasis(**refresh)
    data["revision"] = revision
    return RcaContinuation(**data)
