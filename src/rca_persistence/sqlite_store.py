"""Independent SQLite authority for SPEC-012 Candidate-A facts.

The store owns local Aggregate, Attempt, Try, immutable Version/Artifact, and
A-side publication-receipt truth.  It deliberately does not mutate Incident,
promote Current, or claim that a local receipt is full publication.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from math import isfinite
from pathlib import Path
from threading import RLock

from .contracts import (
    AdmitAttemptRequest,
    AdmittedRetryDisposition,
    ArtifactProvenance,
    ArtifactStatementKind,
    AttemptLineage,
    AttemptLineageRead,
    CreateAggregateRequest,
    DiagnosticConclusion,
    EvidenceCompleteness,
    EvidenceReference,
    EvidentialSupport,
    GenerationAttempt,
    GenerationLifecycle,
    GenerationProvenance,
    GuidanceSource,
    KnowledgeReference,
    LogicalTryIdentity,
    LogicalTryOutcome,
    LogicalTryResultKind,
    PublicationDisposition,
    PublicationResult,
    PublicationTargetIdentity,
    RcaAction,
    RcaAggregate,
    RcaArtifact,
    RcaDomainError,
    RcaErrorCode,
    RcaHypothesis,
    RcaVersion,
    RecoveryCandidate,
    RecoveryCandidateKind,
    VersionRole,
    require_equivalent_attempt_lineage,
)


SCHEMA_VERSION = "4"
_RECOGNIZED_OLDER_SCHEMA_VERSIONS = frozenset({"1", "2", "3"})
_METADATA_KEY = "rca_store_schema_version"
_TABLES = frozenset(
    {"rca_store_metadata", "rca_aggregates", "rca_attempts", "rca_operation_receipts"}
)


class RcaStoreIntegrityError(RcaDomainError):
    """The persisted Candidate-A authority cannot safely be interpreted."""

    def __init__(self, code: RcaErrorCode, message: str) -> None:
        if code not in {
            RcaErrorCode.INTEGRITY_CORRUPTION,
            RcaErrorCode.SCHEMA_INCOMPATIBILITY,
        }:
            raise TypeError("RCA store integrity errors require an integrity/schema code")
        super().__init__(code, message)


class SqliteRcaStore:
    """Configurable-path Candidate-A SQLite authority through Phase 4."""

    def __init__(
        self,
        database_path: str | Path = "rca_store.db",
        *,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(busy_timeout_seconds, bool) or not isinstance(
            busy_timeout_seconds, (int, float)
        ):
            raise TypeError("busy_timeout_seconds must be a finite non-negative number")
        timeout = float(busy_timeout_seconds)
        if not isfinite(timeout) or timeout < 0:
            raise ValueError("busy_timeout_seconds must be a finite non-negative number")
        if not isinstance(database_path, (str, Path)):
            raise TypeError("database_path must be a string or Path")

        self._path = str(database_path)
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        try:
            self._connection = sqlite3.connect(
                self._path,
                isolation_level=None,
                timeout=timeout,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
            self._initialize_or_recognize_schema()
            self.validate_local_readiness()
        except RcaDomainError:
            self._close_after_failed_initialization()
            raise
        except sqlite3.Error as exc:
            self._close_after_failed_initialization()
            raise _sqlite_error(exc, "RCA Store cannot initialize local authority") from exc

    def _close_after_failed_initialization(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RcaDomainError(
                RcaErrorCode.LOCAL_READINESS_FAILURE, "RCA Store is closed"
            )
        return self._connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> "SqliteRcaStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_or_discover_aggregate(
        self, request: CreateAggregateRequest
    ) -> RcaAggregate:
        if not isinstance(request, CreateAggregateRequest):
            raise TypeError("request must be CreateAggregateRequest")
        try:
            with self._write_transaction():
                replay = self._read_receipt(request.operation_id)
                if replay is not None:
                    self._require_receipt_identity(replay, "CREATE_AGGREGATE", request)
                    aggregate = self._read_aggregate(request.aggregate_id)
                    if aggregate is None:
                        self._corruption("Aggregate receipt references a missing Aggregate")
                    return aggregate

                by_id = self._read_aggregate(request.aggregate_id)
                by_incident = self._read_aggregate_by_incident(request.incident_id)
                if by_id is not None or by_incident is not None:
                    if by_id != RcaAggregate(request.aggregate_id, request.incident_id) or by_incident != by_id:
                        raise RcaDomainError(
                            RcaErrorCode.IDENTITY_LINEAGE_CONFLICT,
                            "Aggregate identity contradicts immutable Incident binding",
                            operation_id=request.operation_id,
                            aggregate_id=request.aggregate_id,
                        )
                    aggregate = by_id
                else:
                    aggregate = RcaAggregate(request.aggregate_id, request.incident_id)
                    self._db.execute(
                        "INSERT INTO rca_aggregates(aggregate_id, incident_id, created_at) VALUES (?, ?, ?)",
                        (aggregate.aggregate_id, aggregate.incident_id, _timestamp(request.authoritative_now)),
                    )
                self._insert_receipt("CREATE_AGGREGATE", request, aggregate.aggregate_id)
                return aggregate
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot create or discover Aggregate") from exc

    def admit_attempt(self, request: AdmitAttemptRequest) -> GenerationAttempt:
        if not isinstance(request, AdmitAttemptRequest):
            raise TypeError("request must be AdmitAttemptRequest")
        try:
            with self._write_transaction():
                replay = self._read_receipt(request.operation_id)
                if replay is not None:
                    self._require_receipt_identity(replay, "ADMIT_ATTEMPT", request)
                    view = self._read_attempt_lineage(request.lineage.attempt_id)
                    if view is None:
                        self._corruption("Attempt receipt references a missing Attempt")
                    return view.attempt

                if self._read_aggregate(request.lineage.aggregate_id) is None:
                    raise RcaDomainError(
                        RcaErrorCode.INVALID_REFERENCE,
                        "Attempt references an unknown Aggregate",
                        operation_id=request.operation_id,
                        aggregate_id=request.lineage.aggregate_id,
                        attempt_id=request.lineage.attempt_id,
                    )
                view = self._read_attempt_lineage(request.lineage.attempt_id)
                if view is not None:
                    require_equivalent_attempt_lineage(view.attempt.lineage, request.lineage)
                    attempt = view.attempt
                else:
                    lineage = request.lineage
                    provenance = lineage.generation_provenance
                    attempt = GenerationAttempt(lineage, GenerationLifecycle.PENDING)
                    self._db.execute(
                        """INSERT INTO rca_attempts(
                               attempt_id, aggregate_id, evidence_snapshot_id,
                               evidence_revision_id, knowledge_snapshot_id,
                               provider_id, model_id, prompt_id, configuration_id,
                               credential_profile_id, lifecycle, latest_try_ordinal, admitted_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                        (
                            lineage.attempt_id,
                            lineage.aggregate_id,
                            lineage.evidence_snapshot_id,
                            lineage.evidence_revision_id,
                            lineage.knowledge_snapshot_id,
                            provenance.provider_id,
                            provenance.model_id,
                            provenance.prompt_id,
                            provenance.configuration_id,
                            provenance.credential_profile_id,
                            GenerationLifecycle.PENDING.value,
                            _timestamp(request.authoritative_now),
                        ),
                    )
                self._insert_receipt("ADMIT_ATTEMPT", request, request.lineage.attempt_id)
                return attempt
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot admit Attempt") from exc

    def record_try_outcome(
        self, operation_id: str, outcome: LogicalTryOutcome
    ) -> LogicalTryOutcome:
        _reference(operation_id, "operation_id")
        if not isinstance(outcome, LogicalTryOutcome):
            raise TypeError("outcome must be LogicalTryOutcome")
        try:
            with self._write_transaction():
                replay = self._read_receipt(operation_id)
                if replay is not None:
                    self._require_try_receipt_identity(replay, operation_id, outcome)
                    stored = self._read_try_outcome(outcome.identity)
                    if stored is None:
                        self._corruption("Try receipt references a missing authoritative outcome")
                    return stored

                view = self._read_attempt_lineage(outcome.identity.attempt_id)
                if view is None:
                    raise RcaDomainError(
                        RcaErrorCode.INVALID_REFERENCE,
                        "Try outcome references an unknown admitted Attempt",
                        operation_id=operation_id,
                        attempt_id=outcome.identity.attempt_id,
                    )

                stored = self._find_try_outcome(view, outcome.identity.try_ordinal)
                if stored is not None:
                    if stored != outcome:
                        raise RcaDomainError(
                            RcaErrorCode.IDENTITY_LINEAGE_CONFLICT,
                            "Logical Try identity resolves to a contradictory outcome",
                            operation_id=operation_id,
                            attempt_id=outcome.identity.attempt_id,
                        )
                    return stored

                expected_ordinal = len(view.try_outcomes) + 1
                if outcome.identity.try_ordinal != expected_ordinal:
                    raise RcaDomainError(
                        RcaErrorCode.SEMANTIC_CONFLICT,
                        "Logical Try ordinal cannot skip an undisposed predecessor",
                        operation_id=operation_id,
                        attempt_id=outcome.identity.attempt_id,
                    )
                if view.try_outcomes and (
                    view.try_outcomes[-1].retry_disposition
                    is not AdmittedRetryDisposition.RETRYABLE
                ):
                    raise RcaDomainError(
                        RcaErrorCode.SEMANTIC_CONFLICT,
                        "latest admitted Try disposition does not permit another ordinal",
                        operation_id=operation_id,
                        attempt_id=outcome.identity.attempt_id,
                    )
                if view.attempt.lifecycle is GenerationLifecycle.COMPLETED:
                    raise RcaDomainError(
                        RcaErrorCode.SEMANTIC_CONFLICT,
                        "a completed Attempt cannot admit another Logical Try outcome",
                        operation_id=operation_id,
                        attempt_id=outcome.identity.attempt_id,
                    )

                self._insert_try_receipt(operation_id, outcome)
                lifecycle = (
                    GenerationLifecycle.COMPLETED
                    if outcome.result_kind is LogicalTryResultKind.VALIDATED_RESULT
                    else GenerationLifecycle.FAILED
                )
                self._db.execute(
                    """UPDATE rca_attempts
                          SET lifecycle = ?, latest_try_ordinal = ?
                        WHERE attempt_id = ?""",
                    (lifecycle.value, outcome.identity.try_ordinal, outcome.identity.attempt_id),
                )
                return outcome
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot record Logical Try outcome") from exc

    def commit_validated_artifact(
        self,
        operation_id: str,
        attempt_id: str,
        artifact: RcaArtifact,
        publication_target: PublicationTargetIdentity,
        authoritative_now,
    ) -> RcaVersion:
        """Atomically allocate a Version and persist its Artifact and A-side receipt.

        ``target_version_id`` is the caller's opaque proposed Version identity.  It
        becomes allocated only if this transaction commits.  The authoritative
        timestamp is evidence, not part of replay identity.
        """

        _reference(operation_id, "operation_id")
        _reference(attempt_id, "attempt_id")
        if not isinstance(artifact, RcaArtifact):
            raise TypeError("artifact must be RcaArtifact")
        if not isinstance(publication_target, PublicationTargetIdentity):
            raise TypeError("publication_target must be PublicationTargetIdentity")
        occurred_at = _timestamp(authoritative_now)
        version_id = publication_target.target_version_id
        try:
            with self._write_transaction():
                replay = self._read_receipt(operation_id)
                if replay is not None:
                    self._require_commit_receipt_identity(
                        replay, artifact, publication_target, version_id, operation_id, attempt_id
                    )
                    version = self._read_version(version_id)
                    if version is None:
                        self._corruption("Artifact commit receipt references a missing Version")
                    return version

                existing_publication = self._read_publication_result(
                    publication_target.publication_operation_id
                )
                if existing_publication is not None:
                    if existing_publication.target != publication_target:
                        raise RcaDomainError(
                            RcaErrorCode.RECEIPT_REPLAY_CONFLICT,
                            "publication_operation_id resolves to a contradictory target",
                            operation_id=publication_target.publication_operation_id,
                            attempt_id=attempt_id,
                            version_id=version_id,
                        )
                    version = self._read_version(version_id)
                    if version is None:
                        self._corruption("A-side publication receipt references a missing Version")
                    if version.attempt_id != attempt_id or version.artifact != artifact:
                        raise RcaDomainError(
                            RcaErrorCode.RECEIPT_REPLAY_CONFLICT,
                            "equivalent publication identity contradicts immutable Version semantics",
                            operation_id=publication_target.publication_operation_id,
                            attempt_id=attempt_id,
                            version_id=version_id,
                        )
                    return version

                view = self._read_attempt_lineage(attempt_id)
                if view is None:
                    raise RcaDomainError(
                        RcaErrorCode.INVALID_REFERENCE,
                        "Artifact commit references an unknown Attempt",
                        operation_id=operation_id,
                        attempt_id=attempt_id,
                        version_id=version_id,
                    )
                aggregate = self._read_aggregate(view.attempt.lineage.aggregate_id)
                if aggregate is None:
                    self._corruption("Attempt references a missing Aggregate")
                if view.attempt.lifecycle is not GenerationLifecycle.COMPLETED or not view.try_outcomes:
                    raise RcaDomainError(
                        RcaErrorCode.SEMANTIC_CONFLICT,
                        "only an Attempt with a validated successful Try may allocate a Version",
                        operation_id=operation_id,
                        attempt_id=attempt_id,
                        version_id=version_id,
                    )
                latest = view.try_outcomes[-1]
                if latest.result_kind is not LogicalTryResultKind.VALIDATED_RESULT:
                    self._corruption("COMPLETED Attempt lacks a validated successful Try")
                lineage = view.attempt.lineage
                if publication_target.aggregate_id != lineage.aggregate_id or (
                    publication_target.incident_id != aggregate.incident_id
                ):
                    raise RcaDomainError(
                        RcaErrorCode.IDENTITY_LINEAGE_CONFLICT,
                        "publication target contradicts fresh Aggregate/Incident lineage",
                        operation_id=operation_id,
                        aggregate_id=lineage.aggregate_id,
                        attempt_id=attempt_id,
                        version_id=version_id,
                    )
                if not _artifact_matches_lineage(artifact, lineage):
                    raise RcaDomainError(
                        RcaErrorCode.IDENTITY_LINEAGE_CONFLICT,
                        "Artifact provenance contradicts the successful Attempt lineage",
                        operation_id=operation_id,
                        aggregate_id=lineage.aggregate_id,
                        attempt_id=attempt_id,
                        version_id=version_id,
                    )
                existing_version = self._read_version(version_id)
                if existing_version is not None:
                    raise RcaDomainError(
                        RcaErrorCode.IDENTITY_LINEAGE_CONFLICT,
                        "version_id is already allocated to another immutable Version",
                        operation_id=operation_id,
                        aggregate_id=lineage.aggregate_id,
                        attempt_id=attempt_id,
                        version_id=version_id,
                    )

                versions = self._read_all_versions()
                next_number = 1 + max(
                    (item.version_number for item in versions if item.aggregate_id == lineage.aggregate_id),
                    default=0,
                )
                semantic_identity = _commit_semantic_identity(
                    operation_id, attempt_id, artifact, publication_target, next_number
                )
                self._db.execute(
                    """INSERT INTO rca_operation_receipts(
                           operation_id, command_kind, semantic_identity,
                           result_identity, occurred_at
                       ) VALUES (?, 'COMMIT_ARTIFACT', ?, ?, ?)""",
                    (operation_id, semantic_identity, version_id, occurred_at),
                )
                version = self._read_version(version_id)
                if version is None:
                    self._corruption("atomic Artifact commit did not produce a readable Version")
                return version
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot atomically commit validated Artifact") from exc

    def get_version(self, version_id: str) -> RcaVersion | None:
        _reference(version_id, "version_id")
        try:
            with self._read_snapshot():
                version = self._read_version(version_id)
                if version is not None:
                    self._validate_version_authority(version)
                return version
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read Version") from exc

    def get_version_history(self, aggregate_id: str) -> tuple[RcaVersion, ...]:
        _reference(aggregate_id, "aggregate_id")
        try:
            with self._read_snapshot():
                if self._read_aggregate(aggregate_id) is None:
                    return ()
                versions = tuple(
                    item for item in self._read_all_versions() if item.aggregate_id == aggregate_id
                )
                versions = tuple(sorted(versions, key=lambda item: item.version_number))
                for version in versions:
                    self._validate_version_authority(version)
                return versions
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read Version history") from exc

    def get_artifact(self, version_id: str) -> RcaArtifact | None:
        version = self.get_version(version_id)
        return None if version is None else version.artifact

    def get_artifact_provenance(self, version_id: str) -> ArtifactProvenance | None:
        artifact = self.get_artifact(version_id)
        return None if artifact is None else artifact.provenance

    def get_publication_result(
        self, publication_operation_id: str
    ) -> PublicationResult | None:
        _reference(publication_operation_id, "publication_operation_id")
        try:
            with self._read_snapshot():
                result = self._read_publication_result(publication_operation_id)
                if result is not None:
                    self._validate_publication_result(result)
                return result
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read A-side publication receipt") from exc

    def enumerate_recovery_candidates(self) -> tuple[RecoveryCandidate, ...]:
        try:
            with self._read_snapshot():
                candidates = tuple(
                    RecoveryCandidate(
                        RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
                        version.aggregate_id,
                        attempt_id=version.attempt_id,
                        version_id=version.version_id,
                        publication_operation_id=version.publication_operation_id,
                    )
                    for version in sorted(
                        self._read_all_versions(),
                        key=lambda item: (item.aggregate_id, item.version_number),
                    )
                )
                for candidate in candidates:
                    version = self._read_version(candidate.version_id)  # type: ignore[arg-type]
                    if version is None:
                        self._corruption("recovery enumeration found a missing Version")
                    self._validate_version_authority(version)
                return candidates
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot enumerate recovery authority") from exc

    def get_aggregate(self, aggregate_id: str) -> RcaAggregate | None:
        _reference(aggregate_id, "aggregate_id")
        try:
            with self._read_snapshot():
                aggregate = self._read_aggregate(aggregate_id)
                if aggregate is not None:
                    self._require_record_receipt("CREATE_AGGREGATE", aggregate_id)
                return aggregate
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read Aggregate") from exc

    def get_aggregate_by_incident(self, incident_id: str) -> RcaAggregate | None:
        _reference(incident_id, "incident_id")
        try:
            with self._read_snapshot():
                aggregate = self._read_aggregate_by_incident(incident_id)
                if aggregate is not None:
                    self._require_record_receipt("CREATE_AGGREGATE", aggregate.aggregate_id)
                return aggregate
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read Incident Aggregate binding") from exc

    def get_attempt_lineage(self, attempt_id: str) -> AttemptLineageRead | None:
        _reference(attempt_id, "attempt_id")
        try:
            with self._read_snapshot():
                view = self._read_attempt_lineage(attempt_id)
                if view is not None:
                    self._require_record_receipt("ADMIT_ATTEMPT", attempt_id)
                return view
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read Attempt lineage") from exc

    def validate_local_readiness(self) -> None:
        """Validate all local authority through Phase 3 without skipping bad records."""
        try:
            with self._read_snapshot():
                self._validate_authority_locked()
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot validate local readiness") from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise _sqlite_error(exc, "RCA Store cannot begin write transaction") from exc
            try:
                # Every mutation gates on the same fresh, transaction-local
                # authority validation as readiness.  This prevents a command
                # from extending a store whose unrelated records are already
                # contradictory or whose complete enumeration is unreliable.
                self._validate_authority_locked()
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                try:
                    self._db.commit()
                except sqlite3.Error as exc:
                    self._db.rollback()
                    raise RcaStoreIntegrityError(
                        RcaErrorCode.INTEGRITY_CORRUPTION,
                        "RCA Store cannot confirm local transaction commit",
                    ) from exc

    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        with self._lock:
            try:
                self._db.execute("BEGIN")
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def _initialize_or_recognize_schema(self) -> None:
        tables = self._user_tables()
        if not tables:
            initialized = False
            with self._write_transaction_without_schema_gate():
                # A concurrent initializer may have committed while this
                # connection waited for the SQLite write lock.  Re-read only
                # after BEGIN IMMEDIATE owns serialization.
                if self._user_tables():
                    pass
                else:
                    schema = """
                    CREATE TABLE rca_store_metadata (
                        metadata_key TEXT PRIMARY KEY NOT NULL,
                        metadata_value TEXT NOT NULL
                    );
                    CREATE TABLE rca_aggregates (
                        aggregate_id TEXT PRIMARY KEY NOT NULL,
                        incident_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE rca_attempts (
                        attempt_id TEXT PRIMARY KEY NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        evidence_snapshot_id TEXT NOT NULL,
                        evidence_revision_id TEXT NOT NULL,
                        knowledge_snapshot_id TEXT NOT NULL,
                        provider_id TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        prompt_id TEXT NOT NULL,
                        configuration_id TEXT NOT NULL,
                        credential_profile_id TEXT NOT NULL,
                        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('PENDING','GENERATING','COMPLETED','FAILED')),
                        latest_try_ordinal INTEGER,
                        admitted_at TEXT NOT NULL,
                        FOREIGN KEY(aggregate_id) REFERENCES rca_aggregates(aggregate_id),
                        CHECK(latest_try_ordinal IS NULL OR latest_try_ordinal > 0)
                    );
                    CREATE TABLE rca_operation_receipts (
                        operation_id TEXT PRIMARY KEY NOT NULL,
                        command_kind TEXT NOT NULL CHECK(command_kind IN ('CREATE_AGGREGATE','ADMIT_ATTEMPT','RECORD_TRY','COMMIT_ARTIFACT')),
                        semantic_identity TEXT NOT NULL,
                        result_identity TEXT NOT NULL,
                        occurred_at TEXT NOT NULL
                    );
                    CREATE INDEX rca_attempts_by_aggregate
                        ON rca_attempts(aggregate_id, attempt_id);
                    CREATE INDEX rca_receipts_by_result
                        ON rca_operation_receipts(command_kind, result_identity);
                    CREATE UNIQUE INDEX rca_one_outcome_per_logical_try
                        ON rca_operation_receipts(result_identity)
                        WHERE command_kind = 'RECORD_TRY';
                    CREATE UNIQUE INDEX rca_one_commit_receipt_per_version
                        ON rca_operation_receipts(result_identity)
                        WHERE command_kind = 'COMMIT_ARTIFACT';
                    """
                    for statement in schema.split(";"):
                        if statement.strip():
                            self._db.execute(statement)
                    self._db.execute(
                        "INSERT INTO rca_store_metadata(metadata_key, metadata_value) VALUES (?, ?)",
                        (_METADATA_KEY, SCHEMA_VERSION),
                    )
                    initialized = True
            if initialized:
                return
            tables = self._user_tables()

        if "rca_store_metadata" not in tables:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "existing database has no Candidate-A schema version authority",
            )
        try:
            rows = self._db.execute(
                "SELECT metadata_key, metadata_value FROM rca_store_metadata"
            ).fetchall()
        except sqlite3.Error as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A schema metadata is malformed",
            ) from exc
        if len(rows) != 1 or rows[0][0] != _METADATA_KEY or not isinstance(rows[0][1], str):
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A schema metadata is malformed",
            )
        version = rows[0][1]
        if version in _RECOGNIZED_OLDER_SCHEMA_VERSIONS:
            raise RcaDomainError(
                RcaErrorCode.MIGRATION_REQUIRED,
                f"Candidate-A schema version {version} requires governed migration",
            )
        if version != SCHEMA_VERSION:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A schema version is unknown or unsafe",
            )
        self._validate_current_schema()

    @contextmanager
    def _write_transaction_without_schema_gate(self) -> Iterator[None]:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
        except BaseException:
            self._db.rollback()
            raise
        else:
            self._db.commit()

    def _validate_current_schema(self) -> None:
        if self._user_tables() != _TABLES:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A database contains missing or unexpected authority tables",
            )
        expected_columns = {
            "rca_store_metadata": {"metadata_key", "metadata_value"},
            "rca_aggregates": {"aggregate_id", "incident_id", "created_at"},
            "rca_attempts": {
                "attempt_id", "aggregate_id", "evidence_snapshot_id",
                "evidence_revision_id", "knowledge_snapshot_id", "provider_id",
                "model_id", "prompt_id", "configuration_id", "credential_profile_id",
                "lifecycle", "latest_try_ordinal", "admitted_at",
            },
            "rca_operation_receipts": {
                "operation_id", "command_kind", "semantic_identity",
                "result_identity", "occurred_at",
            },
        }
        for table, expected in expected_columns.items():
            actual = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise RcaStoreIntegrityError(
                    RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                    f"Candidate-A table {table} has an unsupported schema shape",
                )
        for table, column in (
            ("rca_store_metadata", "metadata_key"),
            ("rca_aggregates", "aggregate_id"),
            ("rca_aggregates", "incident_id"),
            ("rca_attempts", "attempt_id"),
            ("rca_operation_receipts", "operation_id"),
        ):
            if not _has_unique_column(self._db, table, column):
                raise RcaStoreIntegrityError(
                    RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                    f"Candidate-A table {table} lacks unique {column} authority",
                )
        metadata = self._db.execute(
            "SELECT metadata_key, metadata_value FROM rca_store_metadata"
        ).fetchall()
        if metadata != [(_METADATA_KEY, SCHEMA_VERSION)]:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A current schema metadata is contradictory",
            )
        foreign_keys = self._db.execute("PRAGMA foreign_key_list(rca_attempts)").fetchall()
        if len(foreign_keys) != 1 or (
            foreign_keys[0][2], foreign_keys[0][3], foreign_keys[0][4]
        ) != ("rca_aggregates", "aggregate_id", "aggregate_id"):
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A Attempt table lacks its local Aggregate reference",
            )
        if not _has_unique_partial_try_identity(self._db):
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A schema lacks unique Logical Try identity authority",
            )
        if not _has_unique_partial_commit_identity(self._db):
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A schema lacks unique immutable Version identity authority",
            )

    def _validate_authority_locked(self) -> None:
        self._validate_current_schema()
        integrity = self._db.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            self._corruption("SQLite integrity_check failed")
        if self._db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            self._corruption("SQLite foreign_key_check found dangling authority")

        aggregate_ids = [
            row[0]
            for row in self._db.execute(
                "SELECT aggregate_id FROM rca_aggregates ORDER BY aggregate_id"
            )
        ]
        attempt_ids = [
            row[0]
            for row in self._db.execute(
                "SELECT attempt_id FROM rca_attempts ORDER BY attempt_id"
            )
        ]
        for aggregate_id in aggregate_ids:
            if self._read_aggregate(aggregate_id) is None:
                self._corruption("Aggregate disappeared during readiness snapshot")
            self._require_record_receipt("CREATE_AGGREGATE", aggregate_id)
        for attempt_id in attempt_ids:
            if self._read_attempt_lineage(attempt_id) is None:
                self._corruption("Attempt disappeared during readiness snapshot")
            self._require_record_receipt("ADMIT_ATTEMPT", attempt_id)
        versions = sorted(
            self._read_all_versions(), key=lambda item: (item.aggregate_id, item.version_number)
        )
        expected_by_aggregate: dict[str, int] = {}
        publication_ids: set[str] = set()
        for version in versions:
            expected = expected_by_aggregate.get(version.aggregate_id, 0) + 1
            if version.version_number != expected:
                self._corruption("committed Version numbers are not continuous")
            expected_by_aggregate[version.aggregate_id] = expected
            if version.publication_operation_id in publication_ids:
                self._corruption("publication_operation_id targets multiple Versions")
            publication_ids.add(version.publication_operation_id)
            self._validate_version_authority(version)
        for row in self._db.execute(
            """SELECT result_identity FROM rca_operation_receipts
                 WHERE command_kind = 'RECORD_TRY' ORDER BY result_identity"""
        ):
            self._require_record_receipt("RECORD_TRY", row[0])
        for version in versions:
            self._require_record_receipt("COMMIT_ARTIFACT", version.version_id)
        for row in self._db.execute(
            "SELECT operation_id FROM rca_operation_receipts ORDER BY operation_id"
        ):
            self._validate_receipt(self._read_receipt(row[0]))

    def _user_tables(self) -> frozenset[str]:
        return frozenset(
            row[0]
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )

    def _read_aggregate(self, aggregate_id: str) -> RcaAggregate | None:
        row = self._db.execute(
            "SELECT aggregate_id, incident_id, created_at FROM rca_aggregates WHERE aggregate_id = ?",
            (aggregate_id,),
        ).fetchone()
        return None if row is None else self._decode_aggregate(row)

    def _read_aggregate_by_incident(self, incident_id: str) -> RcaAggregate | None:
        rows = self._db.execute(
            "SELECT aggregate_id, incident_id, created_at FROM rca_aggregates WHERE incident_id = ?",
            (incident_id,),
        ).fetchall()
        if len(rows) > 1:
            self._corruption("one Incident resolves to multiple RCA Aggregates")
        return None if not rows else self._decode_aggregate(rows[0])

    def _decode_aggregate(self, row: sqlite3.Row | tuple[object, ...]) -> RcaAggregate:
        try:
            aggregate = RcaAggregate(row[0], row[1])  # type: ignore[arg-type]
            _parse_timestamp(row[2])
            return aggregate
        except (IndexError, TypeError, ValueError, RcaDomainError) as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.INTEGRITY_CORRUPTION, "malformed persisted RCA Aggregate"
            ) from exc

    def _read_attempt_lineage(self, attempt_id: str) -> AttemptLineageRead | None:
        row = self._db.execute(
            """SELECT attempt_id, aggregate_id, evidence_snapshot_id, evidence_revision_id,
                      knowledge_snapshot_id, provider_id, model_id, prompt_id,
                      configuration_id, credential_profile_id, lifecycle,
                      latest_try_ordinal, admitted_at
                 FROM rca_attempts WHERE attempt_id = ?""",
            (attempt_id,),
        ).fetchone()
        if row is None:
            if self._read_try_outcomes(attempt_id):
                self._corruption("Logical Try history references a missing Attempt")
            return None
        try:
            if self._read_aggregate(row[1]) is None:
                raise ValueError("Attempt references missing Aggregate")
            provenance = GenerationProvenance(row[5], row[6], row[7], row[8], row[9])
            lineage = AttemptLineage(row[0], row[1], row[2], row[3], row[4], provenance)
            attempt = GenerationAttempt(lineage, GenerationLifecycle(row[10]), row[11])
            _parse_timestamp(row[12])
            outcomes = self._read_try_outcomes(row[0])
            return AttemptLineageRead(attempt, outcomes)
        except (IndexError, TypeError, ValueError, RcaDomainError) as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.INTEGRITY_CORRUPTION, "malformed or contradictory persisted Attempt"
            ) from exc

    def _read_version(self, version_id: str) -> RcaVersion | None:
        rows = self._db.execute(
            """SELECT operation_id, semantic_identity, result_identity, occurred_at
                 FROM rca_operation_receipts
                WHERE command_kind='COMMIT_ARTIFACT' AND result_identity=?""",
            (version_id,),
        ).fetchall()
        if len(rows) > 1:
            self._corruption("version_id resolves to multiple Artifact commit receipts")
        return None if not rows else self._decode_version_receipt(tuple(rows[0]))

    def _read_all_versions(self) -> tuple[RcaVersion, ...]:
        return tuple(
            self._decode_version_receipt(tuple(row))
            for row in self._db.execute(
                """SELECT operation_id, semantic_identity, result_identity, occurred_at
                     FROM rca_operation_receipts
                    WHERE command_kind='COMMIT_ARTIFACT' ORDER BY result_identity"""
            )
        )

    def _decode_version_receipt(self, row: tuple[object, ...]) -> RcaVersion:
        try:
            operation_id, encoded, result_identity, occurred_at = row
            _reference(operation_id, "operation_id")
            _reference(result_identity, "result_identity")
            _parse_timestamp(occurred_at)
            payload = json.loads(encoded)  # type: ignore[arg-type]
            data = _expect_object(
                payload,
                {"operation_id", "attempt_id", "artifact", "publication_target", "version_number"},
                "Artifact commit receipt",
            )
            if data["operation_id"] != operation_id:
                raise ValueError("Artifact commit operation identity mismatch")
            target = _publication_target_from_object(data["publication_target"])
            if target.target_version_id != result_identity:
                raise ValueError("Artifact commit result identity mismatch")
            return RcaVersion(
                target.target_version_id,
                target.aggregate_id,
                data["version_number"],  # type: ignore[arg-type]
                data["attempt_id"],  # type: ignore[arg-type]
                _decode_artifact_object(data["artifact"]),
                target.publication_operation_id,
                VersionRole.COMMITTED_UNPUBLISHED,
            )
        except RcaStoreIntegrityError:
            raise
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError, RcaDomainError) as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.INTEGRITY_CORRUPTION,
                "malformed persisted immutable Version/Artifact",
            ) from exc

    def _read_publication_result(
        self, publication_operation_id: str
    ) -> PublicationResult | None:
        matches: list[PublicationResult] = []
        for row in self._db.execute(
            """SELECT operation_id, semantic_identity, result_identity, occurred_at
                 FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'"""
        ):
            version = self._decode_version_receipt(tuple(row))
            if version.publication_operation_id == publication_operation_id:
                payload = json.loads(row[1])
                target = _publication_target_from_object(payload["publication_target"])
                matches.append(
                    PublicationResult(
                        target,
                        PublicationDisposition.A_SIDE_COMMITTED,
                        _parse_timestamp(row[3]),
                    )
                )
        if len(matches) > 1:
            self._corruption("publication_operation_id resolves to multiple receipts")
        return None if not matches else matches[0]

    def _validate_version_authority(self, version: RcaVersion) -> None:
        aggregate = self._read_aggregate(version.aggregate_id)
        view = self._read_attempt_lineage(version.attempt_id)
        result = self._read_publication_result(version.publication_operation_id)
        if aggregate is None or view is None or result is None:
            self._corruption("Version has dangling Aggregate, Attempt, or publication authority")
        if view.attempt.lineage.aggregate_id != version.aggregate_id:
            self._corruption("Version and Attempt belong to different Aggregates")
        if view.attempt.lifecycle is not GenerationLifecycle.COMPLETED:
            self._corruption("Version references a non-successful Attempt")
        if not _artifact_matches_lineage(version.artifact, view.attempt.lineage):
            self._corruption("Version Artifact provenance contradicts Attempt lineage")
        if result.target != PublicationTargetIdentity(
            version.publication_operation_id,
            version.aggregate_id,
            aggregate.incident_id,
            version.version_id,
            result.target.expected_current_version_id,
        ):
            self._corruption("Version and A-side publication receipt contradict")
        self._validate_publication_result(result)

    def _validate_publication_result(self, result: PublicationResult) -> None:
        if result.disposition is not PublicationDisposition.A_SIDE_COMMITTED:
            self._corruption("Phase-4 receipt claims a non-local publication result")
        version = self._read_version(result.target.target_version_id)
        aggregate = self._read_aggregate(result.target.aggregate_id)
        if version is None or aggregate is None:
            self._corruption("A-side receipt has a dangling Version or Aggregate")
        if (
            version.publication_operation_id != result.target.publication_operation_id
            or version.aggregate_id != result.target.aggregate_id
            or aggregate.incident_id != result.target.incident_id
        ):
            self._corruption("A-side receipt contradicts immutable publication target")

    def _require_commit_receipt_identity(
        self,
        receipt: tuple[object, ...],
        artifact: RcaArtifact,
        target: PublicationTargetIdentity,
        version_id: str,
        operation_id: str,
        attempt_id: str,
    ) -> None:
        self._validate_receipt(receipt)
        if receipt[1] != "COMMIT_ARTIFACT":
            raise RcaDomainError(
                RcaErrorCode.RECEIPT_REPLAY_CONFLICT,
                "operation_id resolves to a different Candidate-A command",
                operation_id=operation_id,
                attempt_id=attempt_id,
                version_id=version_id,
            )
        try:
            payload = json.loads(receipt[2])  # type: ignore[arg-type]
            version_number = payload["version_number"]
            semantic_identity = _commit_semantic_identity(
                operation_id, attempt_id, artifact, target, version_number
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            self._corruption("Artifact commit receipt is malformed")
        if (
            receipt[2] != semantic_identity
            or receipt[3] != version_id
        ):
            raise RcaDomainError(
                RcaErrorCode.RECEIPT_REPLAY_CONFLICT,
                "operation_id resolves to a contradictory Artifact commit",
                operation_id=operation_id,
                attempt_id=attempt_id,
                version_id=version_id,
            )

    def _insert_receipt(
        self,
        command_kind: str,
        request: CreateAggregateRequest | AdmitAttemptRequest,
        result_identity: str,
    ) -> None:
        self._db.execute(
            """INSERT INTO rca_operation_receipts(
                   operation_id, command_kind, semantic_identity, result_identity, occurred_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                request.operation_id,
                command_kind,
                _semantic_identity(command_kind, request),
                result_identity,
                _timestamp(request.authoritative_now),
            ),
        )

    def _insert_try_receipt(self, operation_id: str, outcome: LogicalTryOutcome) -> None:
        self._db.execute(
            """INSERT INTO rca_operation_receipts(
                   operation_id, command_kind, semantic_identity, result_identity, occurred_at
               ) VALUES (?, 'RECORD_TRY', ?, ?, ?)""",
            (
                operation_id,
                _try_semantic_identity(operation_id, outcome),
                _try_result_identity(outcome.identity),
                _timestamp(outcome.occurred_at),
            ),
        )

    def _read_try_outcomes(self, attempt_id: str) -> tuple[LogicalTryOutcome, ...]:
        outcomes: list[LogicalTryOutcome] = []
        for row in self._db.execute(
            """SELECT operation_id, semantic_identity, result_identity, occurred_at
                 FROM rca_operation_receipts
                WHERE command_kind = 'RECORD_TRY'"""
        ):
            outcome = _try_outcome_from_receipt(tuple(row))
            if outcome.identity.attempt_id == attempt_id:
                outcomes.append(outcome)
        outcomes.sort(key=lambda item: item.identity.try_ordinal)
        return tuple(outcomes)

    def _read_try_outcome(self, identity: LogicalTryIdentity) -> LogicalTryOutcome | None:
        row = self._db.execute(
            """SELECT operation_id, semantic_identity, result_identity, occurred_at
                 FROM rca_operation_receipts
                WHERE command_kind = 'RECORD_TRY' AND result_identity = ?""",
            (_try_result_identity(identity),),
        ).fetchone()
        return None if row is None else _try_outcome_from_receipt(tuple(row))

    @staticmethod
    def _find_try_outcome(
        view: AttemptLineageRead, ordinal: int
    ) -> LogicalTryOutcome | None:
        return next(
            (item for item in view.try_outcomes if item.identity.try_ordinal == ordinal),
            None,
        )

    def _read_receipt(self, operation_id: str) -> tuple[object, ...] | None:
        row = self._db.execute(
            """SELECT operation_id, command_kind, semantic_identity, result_identity, occurred_at
                 FROM rca_operation_receipts WHERE operation_id = ?""",
            (operation_id,),
        ).fetchone()
        return None if row is None else tuple(row)

    def _require_receipt_identity(
        self,
        receipt: tuple[object, ...],
        command_kind: str,
        request: CreateAggregateRequest | AdmitAttemptRequest,
    ) -> None:
        self._validate_receipt(receipt)
        expected_result = (
            request.aggregate_id
            if isinstance(request, CreateAggregateRequest)
            else request.lineage.attempt_id
        )
        if (
            receipt[1] != command_kind
            or receipt[2] != _semantic_identity(command_kind, request)
            or receipt[3] != expected_result
        ):
            raise RcaDomainError(
                RcaErrorCode.RECEIPT_REPLAY_CONFLICT,
                "operation_id resolves to a contradictory Candidate-A command",
                operation_id=request.operation_id,
            )

    def _require_try_receipt_identity(
        self,
        receipt: tuple[object, ...],
        operation_id: str,
        outcome: LogicalTryOutcome,
    ) -> None:
        self._validate_receipt(receipt)
        if (
            receipt[1] != "RECORD_TRY"
            or receipt[2] != _try_semantic_identity(operation_id, outcome)
            or receipt[3] != _try_result_identity(outcome.identity)
        ):
            raise RcaDomainError(
                RcaErrorCode.RECEIPT_REPLAY_CONFLICT,
                "operation_id resolves to a contradictory Candidate-A command",
                operation_id=operation_id,
                attempt_id=outcome.identity.attempt_id,
            )

    def _require_record_receipt(self, command_kind: str, result_identity: str) -> None:
        rows = self._db.execute(
            """SELECT operation_id, command_kind, semantic_identity, result_identity, occurred_at
                 FROM rca_operation_receipts
                WHERE command_kind = ? AND result_identity = ?
                ORDER BY operation_id""",
            (command_kind, result_identity),
        ).fetchall()
        if not rows:
            self._corruption("authoritative RCA record has no admission receipt")
        for row in rows:
            self._validate_receipt(tuple(row))

    def _validate_receipt(self, receipt: tuple[object, ...] | None) -> None:
        if receipt is None:
            self._corruption("missing RCA operation receipt")
        try:
            operation_id, kind, encoded, result_identity, occurred_at = receipt
            _reference(operation_id, "operation_id")
            _reference(result_identity, "result_identity")
            _parse_timestamp(occurred_at)
            payload = json.loads(encoded)  # type: ignore[arg-type]
            if not isinstance(payload, dict) or payload.get("operation_id") != operation_id:
                raise ValueError("receipt identity does not match operation_id")
            if kind == "CREATE_AGGREGATE":
                request = CreateAggregateRequest(
                    payload["operation_id"], payload["aggregate_id"], payload["incident_id"],
                    _parse_timestamp(occurred_at),
                )
                aggregate = self._read_aggregate(result_identity)  # type: ignore[arg-type]
                if aggregate != RcaAggregate(request.aggregate_id, request.incident_id):
                    raise ValueError("Aggregate receipt contradicts its result")
            elif kind == "ADMIT_ATTEMPT":
                request = _attempt_request_from_payload(payload, _parse_timestamp(occurred_at))
                view = self._read_attempt_lineage(result_identity)  # type: ignore[arg-type]
                if view is None or view.attempt.lineage != request.lineage:
                    raise ValueError("Attempt receipt contradicts its result")
            elif kind == "RECORD_TRY":
                outcome = _try_outcome_from_receipt(
                    (operation_id, encoded, result_identity, occurred_at)
                )
                view = self._read_attempt_lineage(outcome.identity.attempt_id)
                if view is None or self._find_try_outcome(
                    view, outcome.identity.try_ordinal
                ) != outcome:
                    raise ValueError("Try receipt contradicts its authoritative outcome")
            elif kind == "COMMIT_ARTIFACT":
                if not isinstance(payload, dict):
                    raise ValueError("Artifact receipt has no semantic identity")
                version = self._read_version(result_identity)  # type: ignore[arg-type]
                if version is None:
                    raise ValueError("Artifact receipt references missing Version")
                expected = _commit_semantic_identity(
                    operation_id,  # type: ignore[arg-type]
                    payload["attempt_id"],  # type: ignore[arg-type]
                    _decode_artifact_object(payload["artifact"]),
                    _publication_target_from_object(payload["publication_target"]),
                    payload["version_number"],  # type: ignore[arg-type]
                )
                if encoded != expected or version.attempt_id != payload["attempt_id"]:
                    raise ValueError("Artifact receipt contradicts immutable Version")
            else:
                raise ValueError("unknown receipt command kind")
        except RcaStoreIntegrityError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, RcaDomainError) as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.INTEGRITY_CORRUPTION,
                "malformed or contradictory RCA operation receipt",
            ) from exc

    @staticmethod
    def _corruption(message: str) -> None:
        raise RcaStoreIntegrityError(RcaErrorCode.INTEGRITY_CORRUPTION, message)


def _reference(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character in value for character in ("\n", "\r", "\x00"))
    ):
        raise ValueError(f"{field_name} must be a non-empty single-line reference")
    return value


def _timestamp(value: object) -> str:
    from datetime import datetime

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat()


def _parse_timestamp(value: object):
    from datetime import datetime

    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must retain timezone")
    return parsed


def _semantic_identity(
    command_kind: str, request: CreateAggregateRequest | AdmitAttemptRequest
) -> str:
    if command_kind == "CREATE_AGGREGATE" and isinstance(request, CreateAggregateRequest):
        payload: dict[str, object] = {
            "operation_id": request.operation_id,
            "aggregate_id": request.aggregate_id,
            "incident_id": request.incident_id,
        }
    elif command_kind == "ADMIT_ATTEMPT" and isinstance(request, AdmitAttemptRequest):
        payload = {"operation_id": request.operation_id, "lineage": asdict(request.lineage)}
    else:
        raise TypeError("command kind and request type do not match")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _attempt_request_from_payload(payload: dict[str, object], occurred_at) -> AdmitAttemptRequest:
    lineage_data = payload.get("lineage")
    if not isinstance(lineage_data, dict):
        raise ValueError("Attempt receipt has no lineage object")
    provenance_data = lineage_data.get("generation_provenance")
    if not isinstance(provenance_data, dict):
        raise ValueError("Attempt receipt has no generation provenance")
    provenance = GenerationProvenance(
        provenance_data["provider_id"], provenance_data["model_id"],
        provenance_data["prompt_id"], provenance_data["configuration_id"],
        provenance_data["credential_profile_id"],
    )
    lineage = AttemptLineage(
        lineage_data["attempt_id"], lineage_data["aggregate_id"],
        lineage_data["evidence_snapshot_id"], lineage_data["evidence_revision_id"],
        lineage_data["knowledge_snapshot_id"], provenance,
    )
    return AdmitAttemptRequest(payload["operation_id"], lineage, occurred_at)  # type: ignore[arg-type]


def _try_result_identity(identity: LogicalTryIdentity) -> str:
    return json.dumps(
        [identity.attempt_id, identity.try_ordinal], separators=(",", ":")
    )


def _try_semantic_identity(operation_id: str, outcome: LogicalTryOutcome) -> str:
    payload = {
        "operation_id": operation_id,
        "outcome": {
            "identity": {
                "attempt_id": outcome.identity.attempt_id,
                "try_ordinal": outcome.identity.try_ordinal,
            },
            "result_kind": outcome.result_kind.value,
            "retry_disposition": outcome.retry_disposition.value,
            "occurred_at": _timestamp(outcome.occurred_at),
            "validated_result_id": outcome.validated_result_id,
            "failure_code": outcome.failure_code,
            "safe_failure_message": outcome.safe_failure_message,
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _try_outcome_from_receipt(receipt: tuple[object, ...]) -> LogicalTryOutcome:
    try:
        operation_id, encoded, result_identity, occurred_at = receipt
        _reference(operation_id, "operation_id")
        _reference(result_identity, "result_identity")
        receipt_time = _parse_timestamp(occurred_at)
        payload = json.loads(encoded)  # type: ignore[arg-type]
        if not isinstance(payload, dict) or payload.get("operation_id") != operation_id:
            raise ValueError("Try receipt identity does not match operation_id")
        outcome_data = payload.get("outcome")
        if not isinstance(outcome_data, dict):
            raise ValueError("Try receipt has no outcome object")
        identity_data = outcome_data.get("identity")
        if not isinstance(identity_data, dict):
            raise ValueError("Try receipt has no identity object")
        outcome = LogicalTryOutcome(
            LogicalTryIdentity(
                identity_data["attempt_id"], identity_data["try_ordinal"]  # type: ignore[arg-type]
            ),
            LogicalTryResultKind(outcome_data["result_kind"]),
            AdmittedRetryDisposition(outcome_data["retry_disposition"]),
            _parse_timestamp(outcome_data["occurred_at"]),
            validated_result_id=outcome_data.get("validated_result_id"),  # type: ignore[arg-type]
            failure_code=outcome_data.get("failure_code"),  # type: ignore[arg-type]
            safe_failure_message=outcome_data.get("safe_failure_message"),  # type: ignore[arg-type]
        )
        if outcome.occurred_at != receipt_time:
            raise ValueError("Try outcome time contradicts its receipt")
        if _try_result_identity(outcome.identity) != result_identity:
            raise ValueError("Try receipt result identity is contradictory")
        return outcome
    except RcaDomainError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RcaStoreIntegrityError(
            RcaErrorCode.INTEGRITY_CORRUPTION,
            "malformed or contradictory Logical Try outcome",
        ) from exc


def _artifact_matches_lineage(artifact: RcaArtifact, lineage: AttemptLineage) -> bool:
    provenance = artifact.provenance
    return (
        provenance.evidence_snapshot_id == lineage.evidence_snapshot_id
        and provenance.evidence_revision_id == lineage.evidence_revision_id
        and provenance.knowledge_snapshot_id == lineage.knowledge_snapshot_id
        and provenance.generation == lineage.generation_provenance
    )


def _encode_artifact(artifact: RcaArtifact) -> str:
    return json.dumps(
        asdict(artifact),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _expect_object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} has unsupported or missing fields")
    return value


def _decode_artifact_object(value: object) -> RcaArtifact:
    data = _expect_object(
        value,
        {
            "summary", "severity_assessment", "diagnostic_conclusion", "hypotheses",
            "remediation_actions", "prevention_actions", "limitations",
            "evidence_completeness", "knowledge_gap", "provenance",
        },
        "Artifact",
    )
    provenance_data = _expect_object(
        data["provenance"],
        {
            "evidence_snapshot_id", "evidence_revision_id", "knowledge_snapshot_id",
            "evidence_references", "knowledge_references", "generation",
        },
        "Artifact provenance",
    )
    generation_data = _expect_object(
        provenance_data["generation"],
        {"provider_id", "model_id", "prompt_id", "configuration_id", "credential_profile_id"},
        "generation provenance",
    )
    generation = GenerationProvenance(**generation_data)  # type: ignore[arg-type]
    evidence_items = tuple(
        _expect_object(
            item, {"reference_id", "statement_kind", "description"}, "evidence reference"
        )
        for item in _object_list(provenance_data["evidence_references"], "evidence references")
    )
    evidence = tuple(
        EvidenceReference(
            item["reference_id"],  # type: ignore[arg-type]
            ArtifactStatementKind(item["statement_kind"]),
            item["description"],  # type: ignore[arg-type]
        )
        for item in evidence_items
    )
    knowledge = tuple(
        KnowledgeReference(
            **_expect_object(
                item,
                {"reference_id", "corpus_id", "index_id", "document_id", "document_version", "section_id", "chunk_id"},
                "knowledge reference",
            )
        )
        for item in _object_list(provenance_data["knowledge_references"], "knowledge references")
    )
    provenance = ArtifactProvenance(
        provenance_data["evidence_snapshot_id"],  # type: ignore[arg-type]
        provenance_data["evidence_revision_id"],  # type: ignore[arg-type]
        provenance_data["knowledge_snapshot_id"],  # type: ignore[arg-type]
        evidence,
        knowledge,
        generation,
    )
    hypotheses = tuple(
        RcaHypothesis(
            item["rank"],  # type: ignore[arg-type]
            item["statement"],  # type: ignore[arg-type]
            EvidentialSupport(item["evidential_support"]),
            tuple(_string_list(item["supporting_evidence_ids"], "supporting evidence")),
            tuple(_string_list(item["contradicting_evidence_ids"], "contradicting evidence")),
            tuple(_string_list(item["knowledge_reference_ids"], "hypothesis knowledge")),
            item["reasoning_summary"],  # type: ignore[arg-type]
        )
        for item in (
            _expect_object(
                raw,
                {"rank", "statement", "evidential_support", "supporting_evidence_ids", "contradicting_evidence_ids", "knowledge_reference_ids", "reasoning_summary"},
                "hypothesis",
            )
            for raw in _object_list(data["hypotheses"], "hypotheses")
        )
    )

    def decode_actions(raw: object, label: str) -> tuple[RcaAction, ...]:
        actions: list[RcaAction] = []
        for value in _object_list(raw, label):
            item = _expect_object(
                value, {"description", "source", "knowledge_reference_ids"}, label
            )
            actions.append(
                RcaAction(
                    item["description"],  # type: ignore[arg-type]
                    GuidanceSource(item["source"]),
                    tuple(_string_list(item["knowledge_reference_ids"], label)),
                )
            )
        return tuple(actions)

    return RcaArtifact(
        data["summary"],  # type: ignore[arg-type]
        data["severity_assessment"],  # type: ignore[arg-type]
        DiagnosticConclusion(data["diagnostic_conclusion"]),
        hypotheses,
        decode_actions(data["remediation_actions"], "remediation actions"),
        decode_actions(data["prevention_actions"], "prevention actions"),
        tuple(_string_list(data["limitations"], "limitations")),
        EvidenceCompleteness(data["evidence_completeness"]),
        data["knowledge_gap"],  # type: ignore[arg-type]
        provenance,
    )


def _object_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string array")
    return value


def _publication_target_object(target: PublicationTargetIdentity) -> dict[str, object]:
    return {
        "publication_operation_id": target.publication_operation_id,
        "aggregate_id": target.aggregate_id,
        "incident_id": target.incident_id,
        "target_version_id": target.target_version_id,
        "expected_current_version_id": target.expected_current_version_id,
    }


def _publication_target_from_object(value: object) -> PublicationTargetIdentity:
    data = _expect_object(
        value,
        {"publication_operation_id", "aggregate_id", "incident_id", "target_version_id", "expected_current_version_id"},
        "publication target",
    )
    return PublicationTargetIdentity(**data)  # type: ignore[arg-type]


def _commit_semantic_identity(
    operation_id: str,
    attempt_id: str,
    artifact: RcaArtifact,
    target: PublicationTargetIdentity,
    version_number: int,
) -> str:
    return json.dumps(
        {
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "artifact": json.loads(_encode_artifact(artifact)),
            "publication_target": _publication_target_object(target),
            "version_number": version_number,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _has_unique_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    for index in connection.execute(f"PRAGMA index_list({table})"):
        if not index[2]:
            continue
        columns = [row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")]
        if columns == [column]:
            return True
    return False


def _has_unique_partial_try_identity(connection: sqlite3.Connection) -> bool:
    for index in connection.execute("PRAGMA index_list(rca_operation_receipts)"):
        if not index[2] or not index[4]:
            continue
        columns = [row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")]
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index[1],)
        ).fetchone()
        if (
            columns == ["result_identity"]
            and definition is not None
            and isinstance(definition[0], str)
            and "WHERE command_kind = 'RECORD_TRY'" in definition[0]
        ):
            return True
    return False


def _has_unique_partial_commit_identity(connection: sqlite3.Connection) -> bool:
    for index in connection.execute("PRAGMA index_list(rca_operation_receipts)"):
        if not index[2] or not index[4]:
            continue
        columns = [row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")]
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index[1],)
        ).fetchone()
        if (
            columns == ["result_identity"]
            and definition is not None
            and isinstance(definition[0], str)
            and "WHERE command_kind = 'COMMIT_ARTIFACT'" in definition[0]
        ):
            return True
    return False


def _sqlite_error(exc: sqlite3.Error, context: str) -> RcaDomainError:
    message = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message
        or "busy" in message
        or "unable to open" in message
        or "readonly" in message
        or "disk i/o" in message
        or "database or disk is full" in message
    ):
        return RcaDomainError(RcaErrorCode.LOCAL_READINESS_FAILURE, f"{context}: {exc}")
    return RcaStoreIntegrityError(RcaErrorCode.INTEGRITY_CORRUPTION, context)
