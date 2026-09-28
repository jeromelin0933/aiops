"""Independent Candidate-D SQLite authority with fail-closed semantic reads."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import json
import os
import re
from pathlib import Path
import sqlite3
from typing import Generic, TypeVar

from knowledge_index.contracts import RetrievalResolution
from rca_persistence.contracts import (
    AdmittedRetryDisposition, DiagnosticConclusion, EvidenceCompleteness,
    EvidentialSupport, GuidanceSource, LogicalTryIdentity,
)
from rca_shared.claim_types import ClaimCategory
from .contracts import (
    CausalAssertion, CausalRelation, CausalSupportProof, Claim, EvidenceReference,
    FailureClass, GenerationFailure, GenerationInput, GenerationPin, Guidance,
    Hypothesis, InvocationMetadata, KnowledgeReference, LocalReadStatus,
    RecoveryFact, ResultContent, ValidatedGenerationResult, ValidationFact,
    VersionedIdentity,
)
from .identity import semantic_commitment
from .config import load_generation_config, GenerationConfigError

T = TypeVar("T")
TYPES = {cls.__name__: cls for cls in (
    CausalAssertion, CausalSupportProof, Claim, EvidenceReference,
    GenerationFailure, GenerationInput, GenerationPin, Guidance, Hypothesis,
    InvocationMetadata, KnowledgeReference, LogicalTryIdentity, ResultContent,
    ValidationFact, VersionedIdentity,
)}
ENUMS = {cls.__name__: cls for cls in (
    AdmittedRetryDisposition, CausalRelation, ClaimCategory,
    DiagnosticConclusion, EvidenceCompleteness, EvidentialSupport,
    FailureClass, GuidanceSource, RetrievalResolution,
)}
DDL = ("CREATE TABLE authority (identity TEXT PRIMARY KEY, "
       "subject_identity TEXT NOT NULL, result_subject TEXT UNIQUE, "
       "kind TEXT NOT NULL CHECK(kind IN ('result','failure')), "
       "result_id TEXT UNIQUE, commitment TEXT NOT NULL, payload TEXT NOT NULL, "
       "CHECK((kind='result' AND result_id IS NOT NULL AND result_subject=subject_identity) OR "
       "(kind='failure' AND result_id IS NULL AND result_subject IS NULL)))")


def encode(value: object) -> object:
    if isinstance(value, Enum):
        return {"enum": type(value).__name__, "value": value.value}
    if is_dataclass(value) and not isinstance(value, type):
        return {"type": type(value).__name__, "fields": {
            field.name: encode(getattr(value, field.name)) for field in fields(value)
        }}
    if isinstance(value, tuple):
        return {"tuple": [encode(item) for item in value]}
    if type(value) in (str, int, bool) or value is None:
        return value
    raise TypeError("unsupported durable value")


def decode(value: object) -> object:
    if isinstance(value, dict):
        if set(value) == {"tuple"} and isinstance(value["tuple"], list):
            return tuple(decode(item) for item in value["tuple"])
        if set(value) == {"enum", "value"} and value["enum"] in ENUMS:
            return ENUMS[value["enum"]](value["value"])
        if set(value) == {"type", "fields"} and value["type"] in TYPES:
            cls = TYPES[value["type"]]
            raw = value["fields"]
            if not isinstance(raw, dict) or set(raw) != {field.name for field in fields(cls)}:
                raise ValueError("durable field set invalid")
            return cls(**{key: decode(item) for key, item in raw.items()})
        raise ValueError("unknown durable tag")
    if type(value) in (str, int, bool) or value is None:
        return value
    raise ValueError("unsupported durable value")


def payload(value: object) -> str:
    return json.dumps(encode(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(operation_id: str, try_identity: LogicalTryIdentity) -> str:
    return json.dumps([operation_id, try_identity.attempt_id, try_identity.try_ordinal],
                      separators=(",", ":"))


def subject_identity(try_identity: LogicalTryIdentity) -> str:
    return json.dumps([try_identity.attempt_id, try_identity.try_ordinal],
                      separators=(",", ":"))


def _subject_equivalent(existing: ValidatedGenerationResult,
                        candidate: ValidatedGenerationResult) -> bool:
    """operation_id is execution identity, not result-content authority scope."""
    old_source = existing.content.input
    normalized = replace(
        candidate.content,
        input=replace(candidate.content.input, operation_id=old_source.operation_id),
    )
    return normalized == existing.content


@dataclass(frozen=True, slots=True)
class StoreRead(Generic[T]):
    status: LocalReadStatus
    value: T | None = None


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    results: tuple[RecoveryFact, ...]
    failures: tuple[GenerationFailure, ...]


class CandidateDStore:
    def __init__(self, path: str | Path, *, resource_config_path: str | Path | None = None):
        self.path = Path(path)
        self.resource_config_path = Path(resource_config_path) if resource_config_path is not None else None

    @classmethod
    def from_environment(cls, *, resource_config_path: str | Path | None = None) -> "CandidateDStore":
        path = os.environ.get("CANDIDATE_D_STORE_PATH")
        if not path:
            raise ValueError("CANDIDATE_D_STORE_PATH must identify an independent local store")
        return cls(path, resource_config_path=resource_config_path)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def initialize(self) -> LocalReadStatus:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                # An existing path is authority, even when it is empty or truncated.
                # Only a successfully claimed new path may receive a schema.
                if not self.path.is_file() or self.path.stat().st_size == 0:
                    return LocalReadStatus.REPAIR_REQUIRED
                return self.readiness()
            os.close(descriptor)
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(DDL)
                db.execute("PRAGMA user_version=2")
                db.commit()
            return self.readiness()
        except (OSError, sqlite3.Error):
            return LocalReadStatus.UNAVAILABLE

    def _inspect(self, db: sqlite3.Connection) -> RecoverySnapshot:
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("unrecognized schema version")
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='authority'"
        ).fetchone()
        if schema is None or schema[0] != DDL:
            raise ValueError("unrecognized authority schema")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"authority"}:
            raise ValueError("unexpected schema objects")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity failure")
        results, failures = [], []
        ids: set[str] = set()
        result_subjects: set[str] = set()
        for key, subject, result_subject, kind, result_id, commitment, raw in db.execute(
            "SELECT identity,subject_identity,result_subject,kind,result_id,commitment,payload "
            "FROM authority ORDER BY identity"
        ):
            value = decode(json.loads(raw))
            if kind == "result":
                if not isinstance(value, ResultContent):
                    raise ValueError("invalid result payload")
                checked = ValidatedGenerationResult(result_id, commitment, value)
                expected_subject = subject_identity(value.input.try_identity)
                if (key != identity(value.input.operation_id, value.input.try_identity)
                    or subject != expected_subject or result_subject != expected_subject
                    or result_id in ids or result_subject in result_subjects):
                    raise ValueError("result identity contradiction")
                ids.add(result_id)
                result_subjects.add(result_subject)
                results.append(RecoveryFact(
                    checked.validated_result_id, value.input.try_identity,
                    value.input.operation_id, commitment,
                    value.input.evidence_snapshot_id, value.input.evidence_revision_id,
                    value.input.knowledge_snapshot_id, value.input.pin, True,
                ))
            elif kind == "failure":
                if (not isinstance(value, GenerationFailure) or result_id is not None
                    or result_subject is not None
                    or key != identity(value.operation_id, value.try_identity)
                    or subject != subject_identity(value.try_identity)
                    or commitment != semantic_commitment(value)):
                    raise ValueError("failure identity contradiction")
                failures.append(value)
            else:
                raise ValueError("unknown authority kind")
        return RecoverySnapshot(tuple(results), tuple(failures))

    def recovery(self) -> StoreRead[RecoverySnapshot]:
        if not self.path.is_file():
            return StoreRead(LocalReadStatus.UNAVAILABLE)
        try:
            with self._connect() as db:
                return StoreRead(LocalReadStatus.FOUND, self._inspect(db))
        except (sqlite3.Error, OSError):
            return StoreRead(LocalReadStatus.UNAVAILABLE)
        except (ValueError, TypeError, KeyError):
            return StoreRead(LocalReadStatus.REPAIR_REQUIRED)

    def readiness(self) -> LocalReadStatus:
        if not self.path.is_file():
            return LocalReadStatus.UNAVAILABLE
        if self.resource_config_path is not None:
            try:
                load_generation_config(self.resource_config_path)
            except GenerationConfigError:
                return LocalReadStatus.REPAIR_REQUIRED
        return self.recovery().status

    def local_readiness(self) -> LocalReadStatus:
        """Full D-local readiness requires a resolvable versioned generation resource."""
        if self.resource_config_path is None:
            return LocalReadStatus.INVALID
        return self.readiness()

    def _read(self, column: str, key: str, kind: str) -> StoreRead:
        if not self.path.is_file():
            return StoreRead(LocalReadStatus.UNAVAILABLE)
        try:
            with self._connect() as db:
                self._inspect(db)
                row = db.execute(
                    f"SELECT kind,result_id,commitment,payload FROM authority WHERE {column}=?",
                    (key,),
                ).fetchone()
                if row is None or row[0] != kind:
                    return StoreRead(LocalReadStatus.NOT_FOUND)
                value = decode(json.loads(row[3]))
                if kind == "result":
                    value = ValidatedGenerationResult(row[1], row[2], value)
                return StoreRead(LocalReadStatus.FOUND, value)
        except sqlite3.IntegrityError:
            return StoreRead(LocalReadStatus.REPAIR_REQUIRED)
        except (sqlite3.Error, OSError):
            return StoreRead(LocalReadStatus.UNAVAILABLE)
        except (ValueError, TypeError, KeyError):
            return StoreRead(LocalReadStatus.REPAIR_REQUIRED)

    def result(self, result_id: str) -> StoreRead[ValidatedGenerationResult]:
        read = self._read("result_id", result_id, "result")
        if (read.status is LocalReadStatus.NOT_FOUND and
            (not isinstance(result_id, str) or
             re.fullmatch(r"dvr_[0-9a-f]{64}", result_id) is None)):
            return StoreRead(LocalReadStatus.INVALID)
        return read

    def replay(self, source: GenerationInput) -> StoreRead[ValidatedGenerationResult]:
        return self._read("identity", identity(source.operation_id, source.try_identity), "result")

    def result_for_try(self, try_identity: LogicalTryIdentity) -> StoreRead[ValidatedGenerationResult]:
        return self._read("result_subject", subject_identity(try_identity), "result")

    def failure(self, operation_id: str, try_identity: LogicalTryIdentity) -> StoreRead[GenerationFailure]:
        return self._read("identity", identity(operation_id, try_identity), "failure")

    def failures_for_try(self, try_identity: LogicalTryIdentity) -> StoreRead[tuple[GenerationFailure, ...]]:
        if not self.path.is_file():
            return StoreRead(LocalReadStatus.UNAVAILABLE)
        try:
            with self._connect() as db:
                self._inspect(db)
                rows = db.execute(
                    "SELECT payload FROM authority WHERE subject_identity=? AND kind='failure' "
                    "ORDER BY identity",
                    (subject_identity(try_identity),),
                ).fetchall()
                values = tuple(decode(json.loads(row[0])) for row in rows)
                if any(not isinstance(value, GenerationFailure) for value in values):
                    raise ValueError("invalid failure payload")
                return StoreRead(
                    LocalReadStatus.FOUND if values else LocalReadStatus.NOT_FOUND,
                    values if values else None,
                )
        except sqlite3.IntegrityError:
            return StoreRead(LocalReadStatus.REPAIR_REQUIRED)
        except (sqlite3.Error, OSError):
            return StoreRead(LocalReadStatus.UNAVAILABLE)
        except (ValueError, TypeError, KeyError):
            return StoreRead(LocalReadStatus.REPAIR_REQUIRED)

    def _commit(self, key: str, subject: str, kind: str, result_id: str | None,
                commitment: str, raw: str) -> StoreRead:
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._inspect(db)
                old = db.execute(
                    "SELECT kind,result_id,commitment,payload FROM authority WHERE identity=?",
                    (key,),
                ).fetchone()
                if old is None:
                    if kind == "result":
                        authoritative = db.execute(
                            "SELECT result_id,commitment,payload FROM authority "
                            "WHERE result_subject=?",
                            (subject,),
                        ).fetchone()
                        if authoritative is not None:
                            existing = ValidatedGenerationResult(
                                authoritative[0], authoritative[1],
                                decode(json.loads(authoritative[2])),
                            )
                            candidate = ValidatedGenerationResult(
                                result_id, commitment, decode(json.loads(raw)),
                            )
                            if not _subject_equivalent(existing, candidate):
                                return StoreRead(LocalReadStatus.REPAIR_REQUIRED)
                            return StoreRead(LocalReadStatus.FOUND, existing)
                    db.execute("INSERT INTO authority VALUES (?,?,?,?,?,?,?)",
                               (key, subject, subject if kind == "result" else None,
                                kind, result_id, commitment, raw))
                elif old != (kind, result_id, commitment, raw):
                    return StoreRead(LocalReadStatus.REPAIR_REQUIRED)
                db.commit()
            return self._read("identity", key, kind)
        except sqlite3.IntegrityError:
            return StoreRead(LocalReadStatus.REPAIR_REQUIRED)
        except (sqlite3.Error, OSError):
            return StoreRead(LocalReadStatus.UNAVAILABLE)
        except (ValueError, TypeError, KeyError):
            return StoreRead(LocalReadStatus.REPAIR_REQUIRED)

    def commit_result(self, value: ValidatedGenerationResult) -> StoreRead[ValidatedGenerationResult]:
        if not isinstance(value, ValidatedGenerationResult):
            raise TypeError("validated result required")
        source = value.content.input
        return self._commit(identity(source.operation_id, source.try_identity),
                            subject_identity(source.try_identity), "result",
                            value.validated_result_id, value.semantic_commitment,
                            payload(value.content))

    def commit_failure(self, value: GenerationFailure) -> StoreRead[GenerationFailure]:
        if not isinstance(value, GenerationFailure):
            raise TypeError("typed failure required")
        return self._commit(identity(value.operation_id, value.try_identity),
                            subject_identity(value.try_identity), "failure",
                            None, semantic_commitment(value), payload(value))
