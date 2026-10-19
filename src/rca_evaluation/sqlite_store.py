"""Independent, append-only SQLite authority for Candidate-F evaluation records."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath

from .authority import TrustedApprovalRegistry
from .identity import canonical_bytes, commitment, identity, require_identity
from .ledger import LedgerConflict, LedgerRecord
from .oracle import EvaluationPlan, OracleRevision


APP_ID = 0x017E0A17
SCHEMA_VERSION = 3
RUN_SNAPSHOT_SCHEMA = "SPEC-017-RUN-LEDGER-SNAPSHOT-v1"
RUN_SNAPSHOT_ORDERING = "DEPTH_DOMAIN_RECORD_ID-v1"
REPORT_SOURCE_CUT_POLICY = "EXCLUDE_REPORT_PROJECTIONS-v1"
DEFAULT_RUN_SNAPSHOT_MAX_RECORDS = 10_000
DEFAULT_RUN_SNAPSHOT_MAX_CANONICAL_BYTES = 16 * 1024 * 1024
_DOMAIN_ORDER = (
    "evaluation_run", "execution", "observation", "review", "judgment",
    "re_adjudication", "gate_authorization", "gate_execution_receipt",
    "gate_invocation_start", "gate_invocation_receipt", "rag_child_allocation",
    "docker_namespace",
    "docker_cleanup", "report",
)
_PROTECTED_R3_DOMAINS = frozenset({
    "gate_invocation_start", "gate_invocation_receipt",
    "rag_child_allocation",
})
_ACTUAL_GATES = frozenset({"REAL_LLM", "REAL_RAG", "DOCKER_RCA_E2E"})
_ACTUAL_TERMINAL_STATUSES = frozenset({
    "EXECUTED_PASS", "EXECUTED_FAIL", "INVALID_EXECUTION",
})
_ACTUAL_PROVENANCE_KINDS = {
    "REAL_LLM": "LIVE_GEMINI_INVOCATION",
    "REAL_RAG": "GOVERNED_REAL_RAG_INVOCATION",
    "DOCKER_RCA_E2E": "ISOLATED_DOCKER_INVOCATION",
}
_ACTUAL_INVOCATION_BINDING_FIELDS = (
    "evaluation_run_id", "execution_id", "gate", "profile_identity",
    "profile_commitment",
)
_SCHEMA_V1_SQL = """
    CREATE TABLE oracle_revisions (
        revision_id TEXT PRIMARY KEY, content BLOB NOT NULL);
    CREATE TABLE plans (
        plan_id TEXT PRIMARY KEY, content BLOB NOT NULL);
    CREATE TABLE ledger (
        record_id TEXT PRIMARY KEY, domain TEXT NOT NULL,
        parent_id TEXT, identity_key_json BLOB NOT NULL, semantic_json BLOB NOT NULL,
        semantic_commitment TEXT NOT NULL);
    CREATE UNIQUE INDEX one_original_judgment_per_execution ON ledger(parent_id)
        WHERE domain='judgment' AND json_extract(semantic_json, '$.kind')='ORIGINAL';
    CREATE TRIGGER oracle_no_update BEFORE UPDATE ON oracle_revisions BEGIN SELECT RAISE(ABORT, 'immutable'); END;
    CREATE TRIGGER oracle_no_delete BEFORE DELETE ON oracle_revisions BEGIN SELECT RAISE(ABORT, 'immutable'); END;
    CREATE TRIGGER plan_no_update BEFORE UPDATE ON plans BEGIN SELECT RAISE(ABORT, 'immutable'); END;
    CREATE TRIGGER plan_no_delete BEFORE DELETE ON plans BEGIN SELECT RAISE(ABORT, 'immutable'); END;
    CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'immutable'); END;
    CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'immutable'); END;
"""
_PREPARATION_SCHEMA_SQL = """
    CREATE TABLE preparation_records (
        record_id TEXT PRIMARY KEY, preparation_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('PREPARED','PREPARED_RESOURCES','ADMITTED')),
        content BLOB NOT NULL, semantic_commitment TEXT NOT NULL,
        UNIQUE(preparation_id, kind));
    CREATE TRIGGER preparation_no_update BEFORE UPDATE ON preparation_records BEGIN SELECT RAISE(ABORT, 'immutable'); END;
    CREATE TRIGGER preparation_no_delete BEFORE DELETE ON preparation_records BEGIN SELECT RAISE(ABORT, 'immutable'); END;
"""
_SCHEMA_SQL = _SCHEMA_V1_SQL + _PREPARATION_SCHEMA_SQL

_PREPARATION_SCHEMA_V2_SQL = _PREPARATION_SCHEMA_SQL.replace(
    "'PREPARED','PREPARED_RESOURCES','ADMITTED'", "'PREPARED','ADMITTED'")


def _schema_definition(db: sqlite3.Connection) -> tuple[tuple[str, str, str, str | None], ...]:
    return tuple(db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE type IN ('table','index','trigger') ORDER BY type,name"
    ))


def _expected_schema_definition() -> tuple[tuple[str, str, str, str | None], ...]:
    with sqlite3.connect(":memory:") as pristine:
        pristine.executescript(_SCHEMA_SQL)
        return _schema_definition(pristine)


def _expected_v1_schema_definition() -> tuple[tuple[str, str, str, str | None], ...]:
    with sqlite3.connect(":memory:") as pristine:
        pristine.executescript(_SCHEMA_V1_SQL)
        return _schema_definition(pristine)


def _expected_v2_schema_definition() -> tuple[tuple[str, str, str, str | None], ...]:
    with sqlite3.connect(":memory:") as pristine:
        pristine.executescript(_SCHEMA_V1_SQL + _PREPARATION_SCHEMA_V2_SQL)
        return _schema_definition(pristine)


class EvaluationStoreIntegrityError(RuntimeError):
    """Existing evaluation state cannot be trusted."""


class EvaluationRunSnapshotError(RuntimeError):
    """A complete, bounded, authoritative run snapshot cannot be returned."""


@dataclass(frozen=True, slots=True)
class EvaluationRunSnapshot:
    """Immutable coherent cut issued by the Candidate-F ledger authority."""

    schema_version: str
    ordering_policy: str
    report_source_cut_policy: str
    evaluation_run_id: str
    evaluation_plan_id: str
    oracle_revision_id: str
    records: tuple[LedgerRecord, ...]
    report_source_record_count: int
    report_source_cut_commitment: str
    canonical_size_bytes: int
    integrity_status: str


_READER_SEAL = object()


class EvaluationLedgerReader:
    """Narrow read/reference access for normal evaluation execution paths."""

    __slots__ = ("__store",)

    def __init__(self, store: "SqliteEvaluationStore", *, _seal: object) -> None:
        if _seal is not _READER_SEAL:
            raise TypeError("evaluation ledger reader is store-issued")
        self.__store = store

    def get(self, record_id: str) -> LedgerRecord | None:
        return self.__store.get(record_id)

    def readiness_check(self) -> None:
        self.__store.readiness_check()

    def read_run_snapshot(
        self, evaluation_run_id: str, *,
        max_records: int = DEFAULT_RUN_SNAPSHOT_MAX_RECORDS,
        max_canonical_bytes: int = DEFAULT_RUN_SNAPSHOT_MAX_CANONICAL_BYTES,
    ) -> EvaluationRunSnapshot:
        """Read one complete run cut without exposing storage or mutation access."""
        return self.__store._read_run_snapshot(
            evaluation_run_id, max_records=max_records,
            max_canonical_bytes=max_canonical_bytes,
        )


class SqliteEvaluationStore:
    def __init__(self, database_path: str | Path, *, approvals: TrustedApprovalRegistry | None = None) -> None:
        path = Path(database_path).resolve()
        if path.parent.name != "rca_evaluation" or path.suffix != ".db":
            raise ValueError("evaluation database must reside in a dedicated rca_evaluation directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        if approvals is not None and type(approvals) is not TrustedApprovalRegistry:
            raise TypeError("approvals must be a trusted evaluation approval registry")
        self._approvals = approvals or TrustedApprovalRegistry()
        self._db = sqlite3.connect(str(path), isolation_level=None)
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._initialize()
            self.readiness_check()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "SqliteEvaluationStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def reader(self) -> EvaluationLedgerReader:
        """Return a read-only capability without append/admission methods."""
        self.readiness_check()
        return EvaluationLedgerReader(self, _seal=_READER_SEAL)

    def is_pristine_for_formal_admission(self) -> bool:
        """Legacy R1 predicate: absolutely no durable facts of any kind."""
        self.readiness_check()
        return all(self._db.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in ("oracle_revisions", "plans", "ledger",
                          "preparation_records"))

    def has_no_formal_execution_facts(self) -> bool:
        """Preparation records are not Oracle, plan, run, execution, or gate facts."""
        self.readiness_check()
        return all(self._db.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in ("oracle_revisions", "plans", "ledger"))

    def _read_run_snapshot(
        self, evaluation_run_id: str, *, max_records: int,
        max_canonical_bytes: int,
    ) -> EvaluationRunSnapshot:
        """Build a validated transitive run cut in one SQLite read transaction."""
        require_identity(evaluation_run_id, "evaluation_run")
        for value, label in (
            (max_records, "max_records"),
            (max_canonical_bytes, "max_canonical_bytes"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if self._db.in_transaction:
            raise EvaluationRunSnapshotError(
                "run snapshot requires an authority-owned read transaction")

        self._db.execute("BEGIN")
        try:
            # Integrity and enumeration share this transaction, so a concurrent
            # append can be observed only wholly before or wholly after this cut.
            self.readiness_check()
            root = self.get(evaluation_run_id)
            if root is None:
                raise EvaluationRunSnapshotError("evaluation run does not exist")
            if root.domain != "evaluation_run":
                raise EvaluationRunSnapshotError(
                    "snapshot root is not an evaluation run")
            root_payload = json.loads(root.semantic_json)
            evaluation_plan_id = require_identity(
                root.parent_id, "evaluation_plan")
            oracle_revision_id = require_identity(
                root_payload.get("oracle_revision_id"), "oracle_revision")

            domain_rank = "CASE tree.domain " + " ".join(
                f"WHEN '{domain}' THEN {rank}"
                for rank, domain in enumerate(_DOMAIN_ORDER)
            ) + f" ELSE {len(_DOMAIN_ORDER)} END"
            cursor = self._db.execute(f"""
                WITH RECURSIVE tree(
                    record_id, domain, parent_id, identity_key_json,
                    semantic_json, semantic_commitment, depth
                ) AS (
                    SELECT record_id, domain, parent_id, identity_key_json,
                           semantic_json, semantic_commitment, 0
                    FROM ledger WHERE record_id=?
                    UNION ALL
                    SELECT child.record_id, child.domain, child.parent_id,
                           child.identity_key_json, child.semantic_json,
                           child.semantic_commitment, tree.depth + 1
                    FROM ledger AS child
                    JOIN tree ON child.parent_id=tree.record_id
                )
                SELECT domain, record_id, parent_id, identity_key_json,
                       semantic_json, semantic_commitment, depth
                FROM tree
                ORDER BY depth, {domain_rank}, record_id
            """, (evaluation_run_id,))
            rows = cursor.fetchmany(max_records + 1)
            if len(rows) > max_records:
                raise EvaluationRunSnapshotError(
                    "run snapshot record bound exceeded")

            records: list[LedgerRecord] = []
            serialized_records: list[dict[str, object]] = []
            source_records: list[dict[str, object]] = []
            for domain, record_id, parent_id, key_json, semantic_json, semantic_hash, depth in rows:
                record = LedgerRecord(
                    domain, record_id, parent_id, key_json, semantic_json,
                    semantic_hash,
                )
                try:
                    record.validate()
                    self._validate_links(record)
                except (ValueError, TypeError) as exc:
                    raise EvaluationStoreIntegrityError(
                        "run snapshot contains an invalid ledger record") from exc
                records.append(record)
                serialized_records.append({
                    "depth": depth, "domain": domain, "record_id": record_id,
                    "parent_id": parent_id,
                    "identity_key": json.loads(key_json),
                    "semantic": json.loads(semantic_json),
                    "semantic_commitment": semantic_hash,
                })
                if domain != "report":
                    source_records.append({
                        "domain": domain, "record_id": record_id,
                        "parent_id": parent_id,
                        "semantic_commitment": semantic_hash,
                    })

            snapshot_material = {
                "schema_version": RUN_SNAPSHOT_SCHEMA,
                "ordering_policy": RUN_SNAPSHOT_ORDERING,
                "report_source_cut_policy": REPORT_SOURCE_CUT_POLICY,
                "evaluation_run_id": evaluation_run_id,
                "evaluation_plan_id": evaluation_plan_id,
                "oracle_revision_id": oracle_revision_id,
                "records": serialized_records,
            }
            canonical_size = len(canonical_bytes(snapshot_material))
            if canonical_size > max_canonical_bytes:
                raise EvaluationRunSnapshotError(
                    "run snapshot canonical byte bound exceeded")
            source_cut = commitment({
                "schema_version": RUN_SNAPSHOT_SCHEMA,
                "ordering_policy": RUN_SNAPSHOT_ORDERING,
                "source_cut_policy": REPORT_SOURCE_CUT_POLICY,
                "evaluation_run_id": evaluation_run_id,
                "evaluation_plan_id": evaluation_plan_id,
                "oracle_revision_id": oracle_revision_id,
                "records": source_records,
            })
            snapshot = EvaluationRunSnapshot(
                RUN_SNAPSHOT_SCHEMA, RUN_SNAPSHOT_ORDERING,
                REPORT_SOURCE_CUT_POLICY, evaluation_run_id,
                evaluation_plan_id, oracle_revision_id, tuple(records),
                len(source_records), source_cut, canonical_size, "READY",
            )
            self._db.execute("COMMIT")
            return snapshot
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _initialize(self) -> None:
        app_id = self._db.execute("PRAGMA application_id").fetchone()[0]
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        tables = self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if app_id == 0 and version == 0 and not tables:
            self._db.execute(f"PRAGMA application_id = {APP_ID}")
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.executescript(_SCHEMA_SQL)
            self._schema_version = SCHEMA_VERSION
        elif app_id == APP_ID and version == 1:
            # The sole supported migration is additive.  Validate the complete
            # old schema before changing it; partial/unknown migrations fail.
            if _schema_definition(self._db) != _expected_v1_schema_definition():
                raise EvaluationStoreIntegrityError(
                    "legacy evaluation schema is not the exact supported v1")
            try:
                self._db.executescript(
                    "BEGIN IMMEDIATE;" + _PREPARATION_SCHEMA_SQL
                    + f"PRAGMA user_version = {SCHEMA_VERSION};COMMIT;")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            self._schema_version = SCHEMA_VERSION
        elif app_id == APP_ID and version == 2:
            if _schema_definition(self._db) != _expected_v2_schema_definition():
                raise EvaluationStoreIntegrityError(
                    "legacy evaluation schema is not the exact supported v2")
            # v2 preparation rows are immutable historical evidence.  The v3
            # CHECK expansion is needed only for new PREPARED_RESOURCES rows,
            # so opening a v2 database must not rebuild its table or change its
            # physical schema version.
            self._schema_version = 2
        elif app_id != APP_ID or version != SCHEMA_VERSION:
            raise EvaluationStoreIntegrityError("foreign or unsupported evaluation database")
        else:
            self._schema_version = SCHEMA_VERSION

    def prepare(self, payload: dict[str, object]) -> dict[str, object]:
        """Atomically cross the first durable-mutation boundary.

        Only an exact replay may coexist with a preparation record.  Any
        Oracle/plan/ledger fact means the namespace was not pristine at the
        authorized boundary and is rejected without repair or deletion.
        """
        if self._schema_version != SCHEMA_VERSION:
            raise EvaluationStoreIntegrityError(
                "legacy evaluation database is read-only")
        preparation_id = self._validate_preparation_payload(payload)
        content = canonical_bytes(payload)
        semantic_hash = commitment(payload)
        record_id = identity("preparation", [preparation_id, "PREPARED"])
        self._db.execute("BEGIN IMMEDIATE")
        try:
            if any(self._db.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                   for table in ("oracle_revisions", "plans", "ledger")):
                raise ValueError(
                    "first preparation mutation requires a pristine formal store")
            rows = self._db.execute(
                "SELECT record_id,preparation_id,kind,content,semantic_commitment "
                "FROM preparation_records WHERE kind='PREPARED'").fetchall()
            expected = (record_id, preparation_id, "PREPARED", content, semantic_hash)
            if not rows:
                self._db.execute(
                    "INSERT INTO preparation_records VALUES (?,?,?,?,?)", expected)
            elif len(rows) != 1 or tuple(rows[0]) != expected:
                raise LedgerConflict("contradictory preparation replay")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return dict(payload)

    def _record_prepared_resources(
        self, preparation_id: str, payload: dict[str, object],
    ) -> dict[str, object]:
        """Append an exact PREPARED_RESOURCES transition, never execution facts."""
        if self._schema_version != SCHEMA_VERSION:
            raise EvaluationStoreIntegrityError(
                "legacy evaluation database is read-only")
        initial_row = self._db.execute(
            "SELECT content FROM preparation_records "
            "WHERE preparation_id=? AND kind='PREPARED'", (preparation_id,)).fetchone()
        if initial_row is None:
            raise ValueError("identity-only PREPARED state is required")
        if self._validate_preparation_payload(payload) != preparation_id:
            raise ValueError("prepared resource transition identity mismatch")
        if payload["docker_namespace"].get("resource_state") != "PREPARED_RESOURCES":
            raise ValueError("prepared resource transition requires PREPARED_RESOURCES")
        from .preparation import identity_only_preparation_payload
        initial = json.loads(initial_row[0])
        if identity_only_preparation_payload(payload) != initial:
            raise ValueError("prepared resource transition changes immutable identity")
        if not self.has_no_formal_execution_facts():
            raise ValueError("Formal execution facts exist during preparation")
        content = canonical_bytes(payload)
        semantic_hash = commitment(payload)
        record_id = identity(
            "preparation", [preparation_id, "PREPARED_RESOURCES"])
        expected = (record_id, preparation_id, "PREPARED_RESOURCES", content,
                    semantic_hash)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT record_id,preparation_id,kind,content,semantic_commitment "
                "FROM preparation_records WHERE preparation_id=? "
                "AND kind='PREPARED_RESOURCES'", (preparation_id,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO preparation_records VALUES (?,?,?,?,?)", expected)
            elif tuple(row) != expected:
                raise LedgerConflict("contradictory prepared resource replay")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return dict(payload)

    def _validate_preparation_payload(self, payload: object) -> str:
        """Validate store-local structure without duplicating domain discovery."""
        return self._validate_preparation_payload_version(payload, legacy_v2=False)

    def _validate_legacy_v2_preparation_payload(self, payload: object) -> str:
        """Validate the one exact historical v2 PREPARED payload shape."""
        legacy_fields = {
            "schema", "authority_reference", "lifecycle_state",
            "preparation_id", "preregistration_identity",
            "campaign_identity", "campaign_key", "repository",
            "planned_lineage", "docker_namespace", "real_rag_child",
            "readiness_preparation", "effects",
        }
        if type(payload) is not dict or set(payload) != legacy_fields:
            raise ValueError("legacy v2 preparation payload schema is invalid")
        projected = dict(payload)
        projected["resource_facts"] = None
        return self._validate_preparation_payload_version(
            projected, legacy_v2=True)

    def _validate_preparation_payload_version(
        self, payload: object, *, legacy_v2: bool,
    ) -> str:
        """Validate an exact persisted version; projection is never stored."""
        expected_fields = {
            "schema", "authority_reference", "lifecycle_state",
            "preparation_id", "preregistration_identity",
            "campaign_identity", "campaign_key", "repository",
            "planned_lineage", "docker_namespace", "real_rag_child",
            "readiness_preparation", "resource_facts", "effects",
        }
        if type(payload) is not dict or set(payload) != expected_fields:
            raise ValueError("preparation payload schema is invalid")
        preparation_id = require_identity(payload.get("preparation_id"), "preparation")
        if (payload.get("schema") != "spec017.preparation.v1"
                or payload.get("authority_reference") !=
                    "PM-SPEC017-R4-PREADMISSION-20261016"
                or payload.get("lifecycle_state") != "PREPARED"):
            raise ValueError("preparation lifecycle contract is invalid")
        preregistration = self._require_sha256(
            payload.get("preregistration_identity"), "preregistration identity")
        campaign_key = payload.get("campaign_key")
        if (not isinstance(campaign_key, str) or not campaign_key
                or campaign_key != campaign_key.strip()
                or payload.get("campaign_identity") !=
                    commitment({"campaign_key": campaign_key})):
            raise ValueError("preparation campaign binding is invalid")

        repository = payload.get("repository")
        lineage = payload.get("planned_lineage")
        docker = payload.get("docker_namespace")
        child = payload.get("real_rag_child")
        readiness = payload.get("readiness_preparation")
        effects = payload.get("effects")
        if (not isinstance(repository, dict) or set(repository) != {
                "root", "branch", "commit", "configuration_reference",
                "configuration_commitment"}
                or repository.get("configuration_commitment") != preregistration
                or any(not isinstance(repository.get(name), str)
                       or not repository[name]
                       or repository[name] != repository[name].strip()
                       for name in (
                           "root", "branch", "commit", "configuration_reference"))):
            raise ValueError("preparation repository binding is invalid")
        repository_commit = repository["commit"]
        if (len(repository_commit) != 40
                or any(character not in "0123456789abcdefABCDEF"
                       for character in repository_commit)):
            raise ValueError("preparation repository commit is invalid")
        expected_preparation_id = identity("preparation", [
            preregistration, campaign_key, repository["branch"],
            repository["commit"], str(self.path),
        ])
        if preparation_id != expected_preparation_id:
            raise ValueError("preparation identity binding is invalid")
        if (not isinstance(lineage, dict) or set(lineage) != {
                "planned_run_identity", "planned_execution_identity",
                "counts_as_formal_execution"}
                or lineage.get("counts_as_formal_execution") is not False):
            raise ValueError("preparation planned lineage is invalid")
        require_identity(lineage.get("planned_run_identity"), "planned_run")
        require_identity(lineage.get("planned_execution_identity"),
                         "planned_execution")
        expected_run = identity("planned_run", [preparation_id, campaign_key])
        expected_execution = identity(
            "planned_execution", [expected_run, "READINESS_TARGET"])
        if (lineage["planned_run_identity"] != expected_run
                or lineage["planned_execution_identity"] != expected_execution):
            raise ValueError("preparation planned lineage binding is invalid")
        legacy_docker_fields = {
            "namespace_identity", "project_name", "ownership", "state",
            "resources_created"}
        prepared_docker_fields = legacy_docker_fields | {
            "resource_state", "resource_commitment", "topology_commitment",
            "actual_ownership_commitment"}
        allowed_docker_fields = ({frozenset(legacy_docker_fields)} if legacy_v2
                                 else {frozenset(prepared_docker_fields)})
        if (not isinstance(docker, dict) or set(docker) not in allowed_docker_fields
                or docker.get("ownership") != preparation_id
                or not isinstance(docker.get("project_name"), str)
                or not docker["project_name"]):
            raise ValueError("prepared Docker namespace is invalid")
        resource_state = ("IDENTITY_ONLY" if legacy_v2
                          else docker.get("resource_state"))
        if resource_state not in {"IDENTITY_ONLY", "PREPARING", "PREPARED_RESOURCES"}:
            raise ValueError("prepared Docker resource state is invalid")
        if resource_state == "IDENTITY_ONLY":
            if (docker.get("state") != "PREPARED_IDENTITY_ONLY"
                    or docker.get("resources_created") is not False
                    or any(docker.get(name) is not None for name in (
                        "resource_commitment", "topology_commitment",
                        "actual_ownership_commitment"))):
                raise ValueError("prepared Docker identity-only state is contradictory")
        elif resource_state == "PREPARING":
            if docker.get("resources_created") is not False:
                raise ValueError("preparing Docker resources are contradictory")
        else:
            if (docker.get("state") != "PREPARED_RESOURCES"
                    or docker.get("resources_created") is not True
                    or any(not isinstance(docker.get(name), str)
                           for name in ("resource_commitment", "topology_commitment",
                                        "actual_ownership_commitment"))):
                raise ValueError("prepared Docker resources are contradictory")
        require_identity(docker.get("namespace_identity"), "docker_namespace")
        expected_namespace = identity("docker_namespace", [
            preparation_id, expected_run, "FORMAL_FUTURE_TARGET"])
        if (docker["namespace_identity"] != expected_namespace
                or docker["project_name"] !=
                    "spec017-" + expected_namespace.rsplit(":", 1)[1][:20]):
            raise ValueError("prepared Docker namespace binding is invalid")
        legacy_child_fields = {
                "child_identity", "relative_path", "profile_identity",
                "ownership", "state", "ownership_commitment"}
        prepared_child_fields = legacy_child_fields | {
            "resource_state", "corpus_identity", "index_identity",
            "build_identity", "activation_identity", "artifacts",
            "resource_commitment"}
        allowed_child_fields = ({frozenset(legacy_child_fields)} if legacy_v2
                                else {frozenset(prepared_child_fields)})
        if (not isinstance(child, dict) or set(child) not in allowed_child_fields
                or child.get("ownership") != preparation_id
                or child.get("state") != "ALLOCATED"
                or not isinstance(child.get("child_identity"), str)
                or not child["child_identity"].startswith("ragchild_")
                or not isinstance(child.get("profile_identity"), str)
                or not child["profile_identity"]):
            raise ValueError("prepared Real-RAG child allocation is invalid")
        child_resource_state = ("IDENTITY_ONLY" if legacy_v2
                                else child.get("resource_state"))
        if child_resource_state != resource_state:
            raise ValueError("prepared Docker/Real-RAG resource states contradict")
        resource_names = ("corpus_identity", "index_identity", "build_identity",
                          "activation_identity", "resource_commitment")
        if child_resource_state == "IDENTITY_ONLY":
            if (any(child.get(name) is not None for name in resource_names)
                    or child.get("artifacts", []) != []):
                raise ValueError("prepared Real-RAG identity-only state is contradictory")
        elif child_resource_state == "PREPARED_RESOURCES":
            artifacts = child.get("artifacts")
            if (any(not isinstance(child.get(name), str) or not child[name]
                    for name in resource_names)
                    or not isinstance(artifacts, list) or not artifacts
                    or len(artifacts) > 128):
                raise ValueError("prepared Real-RAG resource lineage is invalid")
            seen: set[str] = set()
            for artifact in artifacts:
                if (not isinstance(artifact, dict)
                        or set(artifact) != {"relative_path", "digest"}
                        or not isinstance(artifact["relative_path"], str)
                        or PurePosixPath(artifact["relative_path"]).is_absolute()
                        or ".." in PurePosixPath(artifact["relative_path"]).parts
                        or artifact["relative_path"] in seen
                        or not isinstance(artifact["digest"], str)
                        or len(artifact["digest"]) != 71
                        or not artifact["digest"].startswith("sha256:")):
                    raise ValueError("prepared Real-RAG artifact manifest is invalid")
                seen.add(artifact["relative_path"])
        relative = child.get("relative_path")
        if (not isinstance(relative, str) or not relative
                or PurePosixPath(relative).is_absolute()
                or ".." in PurePosixPath(relative).parts):
            raise ValueError("prepared Real-RAG child path is invalid")
        self._require_sha256(
            child.get("ownership_commitment"), "child ownership commitment")
        expected_child = "ragchild_" + commitment({
            "preparation": preparation_id,
            "planned_run": expected_run,
            "planned_execution": expected_execution,
            "profile": child["profile_identity"],
        })[7:]
        expected_relative = f"real_rag/{expected_child}"
        owner = {
            "schema": "spec017.prepared-real-rag-child-ownership.v1",
            "preparation_id": preparation_id,
            "planned_run_identity": expected_run,
            "planned_execution_identity": expected_execution,
            "profile_identity": child["profile_identity"],
            "child_identity": expected_child,
            "relative_path": expected_relative,
            "lifecycle_state": "ALLOCATED",
        }
        if (child["child_identity"] != expected_child
                or child["relative_path"] != expected_relative
                or child["ownership_commitment"] != commitment(owner)):
            raise ValueError("prepared Real-RAG child identity binding is invalid")
        if (not isinstance(readiness, dict) or set(readiness) != {
                "state", "material", "commitment"}
                or readiness.get("state") not in {
                    "PREPARED_NOT_VALIDATED_FOR_ADMISSION",
                    "PREPARED_RESOURCES_NOT_VALIDATED_FOR_ADMISSION"}
                or not isinstance(readiness.get("material"), dict)
                or commitment(readiness["material"]) != readiness.get("commitment")):
            raise ValueError("readiness preparation commitment is invalid")
        material = readiness["material"]
        identity_material_fields = {
                "provider_profiles", "rag_profiles", "repository_commit",
                "configuration_commitment", "docker_namespace_identity",
                "real_rag_child_identity"}
        resource_material_fields = identity_material_fields | {
            "runtime_image_identity", "service_health_commitment",
            "mount_commitment", "docker_resource_commitment",
            "real_rag_resource_commitment"}
        if (set(material) not in {
                frozenset(identity_material_fields), frozenset(resource_material_fields)}
                or not isinstance(material.get("provider_profiles"), list)
                or not material["provider_profiles"]
                or not isinstance(material.get("rag_profiles"), list)
                or not material["rag_profiles"]
                or material.get("repository_commit") != repository.get("commit")
                or material.get("configuration_commitment") != preregistration
                or material.get("docker_namespace_identity") !=
                    docker.get("namespace_identity")
                or material.get("real_rag_child_identity") !=
                    child.get("child_identity")
                or child.get("profile_identity") not in
                    material.get("rag_profiles", [])):
            raise ValueError("readiness preparation material is contradictory")
        if resource_state == "PREPARED_RESOURCES":
            if (set(material) != resource_material_fields
                    or readiness.get("state") !=
                        "PREPARED_RESOURCES_NOT_VALIDATED_FOR_ADMISSION"
                    or material.get("docker_resource_commitment") !=
                        docker.get("resource_commitment")
                    or material.get("real_rag_resource_commitment") !=
                        child.get("resource_commitment")):
                raise ValueError("prepared resource commitments are contradictory")
            docker_resource_material = {
                "namespace_identity": docker["namespace_identity"],
                "project_name": docker["project_name"],
                "ownership": docker["ownership"],
                "topology_commitment": docker["topology_commitment"],
                "actual_ownership_commitment":
                    docker["actual_ownership_commitment"],
                "runtime_image_identity": material["runtime_image_identity"],
                "service_health_commitment":
                    material["service_health_commitment"],
            }
            child_resource_material = {
                "child_identity": child["child_identity"],
                "ownership": child["ownership"],
                "corpus_identity": child["corpus_identity"],
                "index_identity": child["index_identity"],
                "build_identity": child["build_identity"],
                "activation_identity": child["activation_identity"],
                "artifacts": child["artifacts"],
                "mount_commitment": material["mount_commitment"],
            }
            if (docker["resource_commitment"] !=
                    commitment(docker_resource_material)
                    or child["resource_commitment"] !=
                    commitment(child_resource_material)):
                raise ValueError("prepared resource commitment is invalid")
        elif set(material) != identity_material_fields:
            raise ValueError("identity-only readiness contains resource facts")
        resource_facts = payload.get("resource_facts")
        if resource_state == "IDENTITY_ONLY":
            if resource_facts is not None:
                raise ValueError("identity-only preparation contains resource facts")
        elif resource_state == "PREPARED_RESOURCES":
            from .preparation import (
                PreparationResourceDomain, PreparationResourceFact,
                PreparationResourceFacts,
            )
            if (not isinstance(resource_facts, dict)
                    or set(resource_facts) != {"facts", "facts_commitment"}
                    or not isinstance(resource_facts.get("facts"), list)
                    or len(resource_facts["facts"]) !=
                        len(PreparationResourceDomain)):
                raise ValueError("prepared resource fact schema is invalid")
            typed = []
            exact_fields = {
                "domain", "preparation_id", "planned_run_identity",
                "expected_target_identity", "actual_observed_identity",
                "authority_profile", "semantic_identity_commitment",
                "observed_at", "fact_commitment",
            }
            for raw in resource_facts["facts"]:
                if not isinstance(raw, dict) or set(raw) != exact_fields:
                    raise ValueError("prepared resource fact field is not allowlisted")
                try:
                    typed.append(PreparationResourceFact(
                        PreparationResourceDomain(raw["domain"]),
                        raw["preparation_id"], raw["planned_run_identity"],
                        raw["expected_target_identity"],
                        raw["actual_observed_identity"], raw["authority_profile"],
                        raw["semantic_identity_commitment"],
                        raw["observed_at"], raw["fact_commitment"]))
                except (TypeError, ValueError) as exc:
                    raise ValueError("prepared resource fact is invalid") from exc
            typed_facts = PreparationResourceFacts(
                tuple(typed), resource_facts["facts_commitment"])
            typed_facts.validate()
            by_domain = {item.domain: item for item in typed_facts.facts}
            if (by_domain[PreparationResourceDomain.RUNTIME_IMAGE]
                    .actual_observed_identity !=
                    material["runtime_image_identity"]
                    or by_domain[PreparationResourceDomain.DOCKER_TOPOLOGY]
                        .actual_observed_identity !=
                        docker["topology_commitment"]
                    or by_domain[PreparationResourceDomain.DOCKER_OWNERSHIP]
                        .actual_observed_identity !=
                        docker["actual_ownership_commitment"]
                    or by_domain[PreparationResourceDomain.DOCKER_HEALTH]
                        .actual_observed_identity !=
                        material["service_health_commitment"]
                    or by_domain[PreparationResourceDomain.REAL_RAG_BUILD]
                        .actual_observed_identity !=
                        child["build_identity"]
                    or by_domain[PreparationResourceDomain.REAL_RAG_BUILD]
                        .actual_observed_identity !=
                        child["index_identity"]
                    or by_domain[PreparationResourceDomain.REAL_RAG_BUILD]
                        .expected_target_identity !=
                        child["corpus_identity"]
                    or by_domain[PreparationResourceDomain.REAL_RAG_ACTIVATION]
                        .actual_observed_identity !=
                        child["activation_identity"]):
                raise ValueError("prepared resource facts contradict projections")
        if effects != {
                "formal_execution": False, "external_gates": False,
                "scenario_injection": False, "denominator_consumed": False}:
            raise ValueError("PREPARED cannot contain Formal execution effects")
        return preparation_id

    def get_preparation(self, preparation_id: str) -> dict[str, object] | None:
        require_identity(preparation_id, "preparation")
        row = self._db.execute(
            "SELECT content,semantic_commitment FROM preparation_records "
            "WHERE preparation_id=? AND kind IN ('PREPARED_RESOURCES','PREPARED') "
            "ORDER BY CASE kind WHEN 'PREPARED_RESOURCES' THEN 0 ELSE 1 END",
            (preparation_id,)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            if (canonical_bytes(payload) != row[0]
                    or commitment(payload) != row[1]):
                raise ValueError("preparation row commitment mismatch")
            if self._schema_version == 2:
                if (self._validate_legacy_v2_preparation_payload(payload)
                        != preparation_id):
                    raise ValueError("legacy preparation identity mismatch")
                projected = dict(payload)
                projected["resource_facts"] = None
                return projected
            if self._validate_preparation_payload(payload) != preparation_id:
                raise ValueError("preparation identity mismatch")
            return payload
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EvaluationStoreIntegrityError("corrupt preparation record") from exc

    def admit_preparation(
        self, preparation_id: str, payload: dict[str, object],
    ) -> dict[str, object]:
        """Append the deterministic ADMITTED transition after fresh validation."""
        if self._schema_version != SCHEMA_VERSION:
            raise EvaluationStoreIntegrityError(
                "legacy evaluation database is read-only")
        prepared = self.get_preparation(preparation_id)
        if prepared is None:
            raise ValueError("PREPARED state is required before Final Admission")
        expected_fields = {
            "schema", "lifecycle_state", "preparation_id",
            "preparation_commitment", "formal_admission_commitment",
            "formal_admission_payload",
        }
        if (type(payload) is not dict or set(payload) != expected_fields
                or payload.get("schema") != "spec017.preparation-admission.v1"
                or payload.get("lifecycle_state") != "ADMITTED"
                or payload.get("preparation_id") != preparation_id
                or payload.get("preparation_commitment") != commitment(prepared)
                or not isinstance(payload.get("formal_admission_payload"), dict)
                or commitment(payload["formal_admission_payload"]) !=
                    payload.get("formal_admission_commitment")):
            raise ValueError("Final Admission payload contradicts preparation")
        if not self.has_no_formal_execution_facts():
            raise ValueError("Formal execution facts exist before Final Admission")
        content = canonical_bytes(payload)
        semantic_hash = commitment(payload)
        record_id = identity("formal_admission", [preparation_id])
        expected = (record_id, preparation_id, "ADMITTED", content, semantic_hash)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT record_id,preparation_id,kind,content,semantic_commitment "
                "FROM preparation_records WHERE preparation_id=? AND kind='ADMITTED'",
                (preparation_id,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO preparation_records VALUES (?,?,?,?,?)", expected)
            elif tuple(row) != expected:
                raise LedgerConflict("contradictory Final Admission replay")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return dict(payload)

    def require_admitted_preparation(
        self, preparation_id: str, admission_commitment: str,
    ) -> dict[str, object]:
        """Fresh-read the exact durable admission used by an execution guard."""
        require_identity(preparation_id, "preparation")
        row = self._db.execute(
            "SELECT content,semantic_commitment FROM preparation_records "
            "WHERE preparation_id=? AND kind='ADMITTED'", (preparation_id,)).fetchone()
        if row is None:
            raise ValueError("only ADMITTED preparation can execute")
        payload = json.loads(row[0])
        if (canonical_bytes(payload) != row[0] or commitment(payload) != row[1]
                or payload.get("formal_admission_commitment") != admission_commitment):
            raise EvaluationStoreIntegrityError("durable Final Admission is contradictory")
        return payload

    def _insert_once(self, table: str, id_column: str, record_id: str, content: bytes) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._db.execute(f"SELECT content FROM {table} WHERE {id_column}=?", (record_id,)).fetchone()
            if existing is None:
                self._db.execute(f"INSERT INTO {table} VALUES (?, ?)", (record_id, content))
            elif existing[0] != content:
                raise LedgerConflict(f"contradictory replay for {record_id}")
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def admit_oracle(self, revision: OracleRevision) -> OracleRevision:
        revision.validate()
        self._approvals.verify_oracle(revision)
        if revision.parent_revision_id is not None:
            if revision.parent_revision_id == revision.oracle_revision_id or self.get_oracle(revision.parent_revision_id) is None:
                raise ValueError("parent Oracle revision must already be admitted")
        data = asdict(revision)
        data["semantic_json"] = revision.semantic_json.decode("utf-8")
        self._insert_once("oracle_revisions", "revision_id", revision.oracle_revision_id, canonical_bytes(data))
        return revision

    def get_oracle(self, revision_id: str) -> OracleRevision | None:
        require_identity(revision_id, "oracle_revision")
        row = self._db.execute("SELECT content FROM oracle_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
            data["semantic_json"] = data["semantic_json"].encode("utf-8")
            result = OracleRevision(**data)
            result.validate()
            self._approvals.verify_oracle(result)
            if result.oracle_revision_id != revision_id:
                raise ValueError("Oracle row identity mismatch")
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise EvaluationStoreIntegrityError("invalid Oracle revision row") from exc

    def admit_plan(self, plan: EvaluationPlan) -> EvaluationPlan:
        oracle = self.get_oracle(plan.oracle_revision_id)
        if oracle is None:
            raise ValueError("exact Oracle revision is not admitted")
        plan.validate(oracle)
        self._insert_once("plans", "plan_id", plan.evaluation_plan_id, canonical_bytes(asdict(plan)))
        return plan

    def get_plan(self, plan_id: str) -> EvaluationPlan | None:
        require_identity(plan_id, "evaluation_plan")
        row = self._db.execute("SELECT content FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        try:
            plan = EvaluationPlan(**json.loads(row[0]))
            oracle = self.get_oracle(plan.oracle_revision_id)
            if oracle is None:
                raise ValueError("plan references missing Oracle")
            plan.validate(oracle)
            if plan.evaluation_plan_id != plan_id:
                raise ValueError("plan row identity mismatch")
            return plan
        except (ValueError, TypeError, KeyError) as exc:
            raise EvaluationStoreIntegrityError("invalid evaluation plan row") from exc

    def append(self, record: LedgerRecord) -> LedgerRecord:
        """Append an evaluation fact; callers never receive production write access."""
        if record.domain == "evaluation_run":
            payload = json.loads(record.semantic_json)
            if (payload.get("execution_mode") == "FORMAL"
                    or payload.get("formal_admission_commitment") is not None
                    or payload.get("formal_admission") is not None):
                raise ValueError(
                    "formal evaluation run is protected; use the admitted formal entrypoint")
        if record.domain == "judgment":
            raise ValueError(
                "formal ORIGINAL judgment is protected; the governed adjudicator must issue it"
            )
        if record.domain in _PROTECTED_R3_DOMAINS:
            raise ValueError(
                f"{record.domain} is protected; actual invocation evidence "
                "must be issued by the governed harness"
            )
        if record.domain == "observation":
            from .recovery_records import PROTECTED_RECOVERY_KINDS
            try:
                kind = json.loads(record.semantic_json).get("kind")
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("observation payload is invalid") from exc
            if kind in PROTECTED_RECOVERY_KINDS:
                raise ValueError(
                    f"{kind} is protected; a governed typed evaluator must issue it"
                )
            if kind == "UNSUPPORTED_CLAIM_RESULT":
                raise ValueError(
                    "UNSUPPORTED_CLAIM_RESULT is protected; the governed adjudicator must issue it"
                )
        return self._append_validated(record)

    def _append_rag_child_allocation(self, record: LedgerRecord) -> LedgerRecord:
        """Persist a structurally valid Candidate-F isolated-child allocation."""
        payload = json.loads(record.semantic_json)
        if (record.domain != "rag_child_allocation"
                or payload.get("kind") != "REAL_RAG_CHILD_ALLOCATION"
                or payload.get("lifecycle_state") != "ALLOCATED"):
            raise ValueError("RAG child allocation payload is invalid")
        return self._append_validated(record)

    def _append_formal_run(self, record: LedgerRecord) -> LedgerRecord:
        """Append a structurally validated FORMAL root from the supported entrypoint."""
        self._validate_formal_run_record(record)
        result = self._append_validated(record)
        self.require_formal_run(result.record_id)
        return result

    def _validate_formal_run_record(self, record: LedgerRecord) -> None:
        """Check durable FORMAL classification, lineage, and frozen controls."""
        record.validate()
        payload = json.loads(record.semantic_json)
        commitment_value = payload.get("formal_admission_commitment")
        if (record.domain != "evaluation_run"
                or payload.get("execution_mode") != "FORMAL"
                or not isinstance(commitment_value, str)
                or len(commitment_value) != 71
                or not commitment_value.startswith("sha256:")):
            raise ValueError("formal run root lacks an exact admission binding")
        admission = payload.get("formal_admission")
        exact = {
            "evaluation_plan_id": record.parent_id,
            "evaluation_run_id": record.record_id,
            "campaign_key": payload.get("campaign_key"),
            "manifest_commitment": payload.get("manifest_commitment"),
            "evaluation_root": str(self.path.parent),
            "execution_workspace": str(self.path.parent),
            "database_path": str(self.path),
        }
        controls = admission.get("controls") if isinstance(admission, dict) else None
        expected_controls = {
            "repetitions": 5, "order": "REPETITION_MAJOR",
            "bootstrap_seed": 17017, "polling_interval_seconds": 1,
            "per_execution_timeout_seconds": 300, "early_stop": False,
            "replacement": "NONE",
        }
        order = payload.get("execution_order")
        sealed_manifest = payload.get("sealed_manifest")
        expected_scenarios = [
            f"S{scenario}" for _repetition in range(1, 6)
            for scenario in range(1, 7)]
        scenario_inputs = (sealed_manifest.get("scenario_inputs")
                           if isinstance(sealed_manifest, dict) else None)
        if (not isinstance(admission, dict)
                or commitment(admission) != commitment_value
                or any(admission.get(name) != value for name, value in exact.items())
                or admission.get("schema") != "spec017.formal-admission.v1"
                or controls != expected_controls
                or payload.get("fixed_n") != 5
                or admission.get("execution_order") != order
                or not isinstance(order, list) or len(order) != 30
                or len(set(order)) != 30
                or not isinstance(sealed_manifest, dict)
                or commitment(sealed_manifest)
                    != payload.get("manifest_commitment")
                or sealed_manifest.get("run_id") != record.record_id
                or sealed_manifest.get("evaluation_plan_id") != record.parent_id
                or sealed_manifest.get("execution_ids") != order
                or not isinstance(scenario_inputs, list)
                or [item[0] for item in scenario_inputs
                    if isinstance(item, list) and item] != expected_scenarios
                or len(scenario_inputs) != 30
                or sealed_manifest.get("random_seed") != 17017
                or sealed_manifest.get("polling_interval_ms") != 1_000
                or sealed_manifest.get("timeout_ms") != 300_000
                or sealed_manifest.get("execution_order_strategy")
                    != "REPETITION_MAJOR"
                or sealed_manifest.get("early_stop_allowed") is not False
                or sealed_manifest.get("replacement_policy") != "NONE"
                or payload.get("repository_commit")
                    != payload.get("production_build")
                or payload.get("repository_dirty") is not False
                or payload.get("reviewer_protocol")
                    != admission.get("reviewer_protocol")
                or commitment(payload.get("reviewer_protocol"))
                    != admission.get("reviewer_protocol_commitment")):
            raise ValueError("formal run/admission/manifest binding is contradictory")

    def require_formal_run(self, evaluation_run_id: str) -> LedgerRecord:
        """Return a run only when its durable FORMAL lineage is coherent."""
        run = self.get(evaluation_run_id)
        if run is None or run.domain != "evaluation_run":
            raise ValueError("formal evaluation run does not exist")
        try:
            self._validate_formal_run_record(run)
        except (TypeError, ValueError) as exc:
            raise ValueError("run lacks coherent FORMAL admission provenance") from exc
        return run

    def _append_protected_adjudication(
        self, record: LedgerRecord, *, authority: object,
    ) -> LedgerRecord:
        """Append S3 formal truth only after store-level provenance validation."""
        from .adjudication import (
            ADJUDICATION_EVALUATOR_IDENTITY, ADJUDICATION_EVALUATOR_VERSION,
            is_adjudication_issuance_authority,
        )
        if not is_adjudication_issuance_authority(authority):
            raise ValueError("protected adjudication authority is invalid")
        payload = json.loads(record.semantic_json)
        if record.domain == "observation":
            if payload.get("kind") != "UNSUPPORTED_CLAIM_RESULT":
                raise ValueError("protected claim result kind is invalid")
        elif record.domain == "judgment":
            if payload.get("kind") != "ORIGINAL":
                raise ValueError("protected judgment kind is invalid")
        else:
            raise ValueError("protected adjudication record domain is invalid")
        run = self._run_for(record.parent_id)
        plan = self.get_plan(run.parent_id)
        execution = self.get(record.parent_id)
        execution_payload = json.loads(execution.semantic_json)
        if (payload.get("governed_adjudicator_identity")
                != ADJUDICATION_EVALUATOR_IDENTITY
                or payload.get("adjudicator_version")
                    != ADJUDICATION_EVALUATOR_VERSION
                or payload.get("evaluation_run_id") != run.record_id
                or payload.get("execution_id") != record.parent_id
                or payload.get("scenario_id", payload.get("oracle_scenario_id"))
                    != execution_payload.get("scenario_id")
                or payload.get("oracle_revision_id") != plan.oracle_revision_id):
            raise ValueError("protected adjudication run/execution/Oracle binding is invalid")
        ids = payload.get("source_record_ids")
        hashes = payload.get("source_record_commitments")
        if (not isinstance(ids, list) or not isinstance(hashes, list)
                or len(ids) != len(hashes) or len(ids) < 3
                or len(set(ids)) != len(ids)):
            raise ValueError("protected adjudication source lineage is incomplete")
        sources = []
        for source_id, source_hash in zip(ids, hashes):
            source = self.get(source_id)
            if (source is None or source.parent_id != record.parent_id
                    or source.semantic_commitment != source_hash):
                raise ValueError("protected adjudication source is missing or mismatched")
            sources.append(source)
        if record.domain == "observation":
            findings = payload.get("claim_adjudications")
            result = payload.get("unsupported_claim_result")
            claim_ids = payload.get("production_claim_identities")
            claim_hashes = payload.get("production_claim_commitments")
            review_ids = payload.get("claim_review_ids")
            if (not isinstance(findings, list) or not isinstance(result, dict)
                    or not isinstance(claim_ids, list)
                    or not isinstance(claim_hashes, list)
                    or len(claim_ids) != len(claim_hashes)
                    or len(set(claim_ids)) != len(claim_ids)
                    or not isinstance(review_ids, list) or len(review_ids) not in {2, 3}
                    or not set(review_ids).issubset(ids)):
                raise ValueError("protected claim adjudication projection is invalid")
            pairs = []
            for item in findings:
                if (not isinstance(item, dict)
                        or item.get("production_claim_id") not in claim_ids
                        or not isinstance(item.get("atomic_commitment"), str)
                        or item.get("disposition") not in {
                            "SUPPORTED", "UNSUPPORTED", "CONTRADICTED"}):
                    raise ValueError("protected claim disposition is invalid")
                pairs.append((item["production_claim_id"], item["atomic_commitment"]))
            if len(set(pairs)) != len(pairs):
                raise ValueError("protected claim dispositions are duplicated")
            numerator = sum(item["disposition"] in {
                "UNSUPPORTED", "CONTRADICTED"} for item in findings)
            denominator = len(findings)
            contradicted = sum(item["disposition"] == "CONTRADICTED"
                               for item in findings)
            expected_result = {
                "status": ("SCORED" if denominator else "NO_ELIGIBLE_CLAIMS"),
                "numerator": numerator, "denominator": denominator,
                "contradicted_count": contradicted,
                "rate_fraction": (None if not denominator else
                                  [numerator, denominator]),
            }
            if result != expected_result:
                raise ValueError("protected UCR numerator/denominator is not deterministic")
            for review_id in review_ids:
                review = self.get(review_id)
                review_payload = json.loads(review.semantic_json) if review else {}
                if (review is None or review.domain != "review"
                        or review_payload.get("stage") != "CLAIM_ADJUDICATION"
                        or review_payload.get("input_commitment")
                            != payload.get("input_commitment")):
                    raise ValueError("protected claim review lineage is invalid")
            review_payloads = [json.loads(self.get(item).semantic_json)
                               for item in review_ids]
            consensus_keys = [item.get("claim_facts_commitment")
                              for item in review_payloads]
            if (len(consensus_keys) == 2 and consensus_keys[0] == consensus_keys[1]):
                selected_review = review_payloads[0]
            elif (len(consensus_keys) == 3 and consensus_keys[0] != consensus_keys[1]
                    and consensus_keys[2] in set(consensus_keys[:2])):
                selected_review = review_payloads[2]
            else:
                raise ValueError("protected claim reviewer consensus is invalid")
            evaluated_commitment = commitment({
                "status": result["status"], "numerator": numerator,
                "denominator": denominator,
                "contradicted_count": contradicted,
                "rate_fraction": result["rate_fraction"],
                "findings": findings, "integrity_errors": [],
            })
            if (selected_review.get("claim_facts_commitment")
                    != payload.get("claim_facts_commitment")
                    or selected_review.get("result_commitment")
                    != evaluated_commitment):
                raise ValueError("protected claim evaluator result lacks reviewer consensus")
            result_material = {
                "claim_adjudications": findings, "ucr": result,
                "claim_facts_commitment": payload.get("claim_facts_commitment"),
            }
            if payload.get("governed_result_commitment") != commitment(result_material):
                raise ValueError("protected claim/UCR result commitment is invalid")
        else:
            scoring = payload.get("root_cause_predicates")
            claim = self.get(payload.get("claim_result_record_id", ""))
            claim_payload = json.loads(claim.semantic_json) if claim else {}
            stage2_ids = payload.get("stage2_review_ids")
            if (not isinstance(stage2_ids, list) or len(stage2_ids) not in {2, 3}
                    or not set(stage2_ids).issubset(ids)):
                raise ValueError("protected judgment reviewer lineage is invalid")
            stage2_payloads = []
            for review_id in stage2_ids:
                review = self.get(review_id)
                review_payload = json.loads(review.semantic_json) if review else {}
                if (review is None or review.domain != "review"
                        or review_payload.get("stage") != "STAGE2"
                        or review_payload.get("oracle_revision_id")
                            != payload.get("oracle_revision_id")
                        or review_payload.get("oracle_scenario_id")
                            != payload.get("oracle_scenario_id")
                        or review_payload.get("oracle_scenario_commitment")
                            != payload.get("oracle_scenario_commitment")):
                    raise ValueError("protected judgment Stage 2 source is invalid")
                stage2_payloads.append(review_payload)
            consensus_keys = [commitment(item.get("top_hypothesis"))
                              for item in stage2_payloads]
            if (len(consensus_keys) == 2 and consensus_keys[0] == consensus_keys[1]):
                selected_stage2 = stage2_payloads[0]
            elif (len(consensus_keys) == 3 and consensus_keys[0] != consensus_keys[1]
                    and consensus_keys[2] in set(consensus_keys[:2])):
                selected_stage2 = stage2_payloads[2]
            else:
                raise ValueError("protected judgment reviewer consensus is invalid")
            if (not isinstance(scoring, dict)
                    or payload.get("final_scoring_commitment") != commitment(scoring)
                    or claim is None or claim.parent_id != record.parent_id
                    or claim.record_id not in ids
                    or claim.semantic_commitment
                        != payload.get("claim_result_record_commitment")
                    or claim_payload.get("kind") != "UNSUPPORTED_CLAIM_RESULT"
                    or payload.get("unsupported_claim_result")
                        != claim_payload.get("unsupported_claim_result")
                    or payload.get("predicate_scoring_basis_commitment")
                        != commitment({
                            "root_cause": scoring,
                            "unsupported_claim_result":
                                payload.get("unsupported_claim_result"),
                        })
                    or bool(selected_stage2.get("root_cause_success"))
                        != bool(scoring.get("root_cause_identification_success"))
                    or payload.get("result") != (
                        "PASS" if scoring.get("root_cause_identification_success")
                        else "FAIL")):
                raise ValueError("protected judgment scoring/claim binding is invalid")
            material = dict(payload)
            sealed = material.pop("judgment_commitment", None)
            if sealed != commitment(material):
                raise ValueError("protected judgment commitment is invalid")
        return self._append_validated(record)

    def _append_protected_recovery(
        self, record: LedgerRecord, *, authority: object,
    ) -> LedgerRecord:
        """Append through the sole R2/S4 evaluator-owned issuance capability."""
        from .recovery_records import (
            PROTECTED_RECOVERY_KINDS, is_recovery_issuance_authority,
        )
        if not is_recovery_issuance_authority(authority):
            raise ValueError("protected recovery issuance authority is invalid")
        if record.domain != "observation":
            raise ValueError("protected recovery fact must be an observation")
        payload = json.loads(record.semantic_json)
        kind = payload.get("kind")
        if kind not in PROTECTED_RECOVERY_KINDS:
            raise ValueError("protected recovery fact is not evaluator-issued")
        run = self._run_for(record.parent_id)
        if kind == "RECOVERY_SOURCE_OBSERVATION":
            projection = payload.get("observation_projection")
            if (payload.get("evaluation_run_id") != run.record_id
                    or payload.get("execution_id") != record.parent_id
                    or not isinstance(payload.get("case_id"), str)
                    or type(payload.get("source_ordinal")) is not int
                    or payload.get("source_observation_commitment")
                        != commitment(projection)):
                raise ValueError("protected source observation binding is invalid")
        elif kind == "RECOVERY_EVALUATOR_EXECUTION":
            ids = payload.get("source_observation_record_ids")
            hashes = payload.get("source_observation_record_commitments")
            if (payload.get("evaluator_identity")
                    != "SPEC-017-R2S4-GOVERNED-EVALUATOR-v3"
                    or payload.get("execution_id") != record.parent_id
                    or not isinstance(ids, list) or not isinstance(hashes, list)
                    or len(ids) != len(hashes) or not 2 <= len(ids) <= 8
                    or payload.get("result_commitment")
                        != commitment(payload.get("result_payload"))):
                raise ValueError("protected evaluator execution binding is invalid")
            for source_id, source_hash in zip(ids, hashes):
                source = self.get(source_id)
                if (source is None or source.parent_id != record.parent_id
                        or source.semantic_commitment != source_hash):
                    raise ValueError("protected evaluator source is missing")
                source_payload = json.loads(source.semantic_json)
                if (source_payload.get("kind") != "RECOVERY_SOURCE_OBSERVATION"
                        or source_payload.get("case_id") != payload.get("case_id")):
                    raise ValueError("protected evaluator source lineage is invalid")
        elif kind in {"REFRESH_VERSION_RESULT", "RESTART_RECOVERY_RESULT"}:
            evaluator = self.get(payload.get("evaluator_execution_record_id", ""))
            if (payload.get("governed_evaluator_identity")
                    != "SPEC-017-R2S4-GOVERNED-EVALUATOR-v3"
                    or evaluator is None or evaluator.parent_id != record.parent_id
                    or evaluator.semantic_commitment
                        != payload.get("evaluator_execution_commitment")
                    or not payload.get("typed_result_commitment")):
                raise ValueError("protected recovery result provenance is invalid")
        elif kind == "DEVIATION_RESULT":
            source = self.get(payload.get("source_evaluation_fact_id", ""))
            if (payload.get("governed_evaluator_identity")
                    != "SPEC-017-R2S4-DEVIATION-EVALUATOR-v3"
                    or source is None or source.parent_id != record.parent_id
                    or source.semantic_commitment
                        != payload.get("source_evaluation_fact_commitment")
                    or not payload.get("typed_result_commitment")):
                raise ValueError("protected deviation provenance is invalid")
        return self._append_validated(record)

    def _append_validated(self, record: LedgerRecord) -> LedgerRecord:
        record.validate()
        self._validate_links(record)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            if record.domain == "judgment":
                existing_original = self._db.execute(
                    "SELECT record_id FROM ledger WHERE domain='judgment' AND parent_id=? "
                    "AND json_extract(semantic_json, '$.kind')='ORIGINAL'",
                    (record.parent_id,),
                ).fetchone()
                if existing_original is not None and existing_original[0] != record.record_id:
                    raise LedgerConflict("execution already has an ORIGINAL judgment")
            if record.domain == "gate_invocation_start":
                payload = json.loads(record.semantic_json)
                invocation_id = payload["invocation_id"]
                binding = tuple(payload[field] for field in
                                _ACTUAL_INVOCATION_BINDING_FIELDS)
                existing_starts = self._db.execute(
                    "SELECT record_id,semantic_json FROM ledger "
                    "WHERE domain='gate_invocation_start' "
                    "AND json_extract(semantic_json, '$.invocation_id')=?",
                    (invocation_id,),
                ).fetchall()
                for existing_id, existing_json in existing_starts:
                    existing_payload = json.loads(existing_json)
                    existing_binding = tuple(
                        existing_payload.get(field)
                        for field in _ACTUAL_INVOCATION_BINDING_FIELDS
                    )
                    if existing_binding != binding:
                        raise LedgerConflict(
                            "actual invocation identity is already bound to a "
                            "different run/execution/gate/profile"
                        )
                    if (existing_id != record.record_id
                            or existing_json != record.semantic_json):
                        raise LedgerConflict(
                            f"contradictory replay for invocation {invocation_id}"
                        )
            row = self._db.execute("SELECT domain,parent_id,identity_key_json,semantic_json,semantic_commitment FROM ledger WHERE record_id=?",
                                   (record.record_id,)).fetchone()
            expected = (record.domain, record.parent_id, record.identity_key_json, record.semantic_json, record.semantic_commitment)
            if row is None:
                self._db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)", (record.record_id, *expected))
            elif tuple(row) != expected:
                raise LedgerConflict(f"contradictory replay for {record.record_id}")
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return record

    def _record_actual_execution_start(
        self, *, evaluation_run_id: str, execution_id: str, gate: str,
        profile_identity: str, profile_commitment: str, invocation_id: str,
        started_at: str, invocation_pins: dict[str, object],
    ) -> LedgerRecord:
        """Issue a governed actual-execution start from verified harness facts."""
        if gate not in _ACTUAL_GATES:
            raise ValueError("actual invocation start has unsupported gate")
        for value, label in (
            (profile_identity, "profile identity"),
            (invocation_id, "invocation identity"),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{label} must be non-empty trimmed text")
        self._require_sha256(profile_commitment, "profile commitment")
        self._parse_timestamp(started_at, "started_at")
        if type(invocation_pins) is not dict:
            raise ValueError("invocation pins must be a dictionary")

        execution = self.get(execution_id)
        if (execution is None or execution.domain != "execution"
                or execution.parent_id != evaluation_run_id):
            raise ValueError("actual invocation start requires its exact execution run")
        authorization_id = identity(
            "gate_authorization",
            [evaluation_run_id, gate, profile_identity, profile_commitment],
        )
        authorization = self.get(authorization_id)
        if authorization is None or authorization.domain != "gate_authorization":
            raise ValueError("actual invocation start requires exact gate authorization")

        payload = {
            "evaluation_run_id": evaluation_run_id,
            "execution_id": execution_id,
            "gate": gate,
            "profile_identity": profile_identity,
            "profile_commitment": profile_commitment,
            "invocation_id": invocation_id,
            "started_at": started_at,
            "invocation_pins": invocation_pins,
        }
        return self._append_validated(LedgerRecord.create(
            "gate_invocation_start",
            [execution_id, gate, profile_commitment, invocation_id],
            payload, execution_id,
        ))

    def _record_actual_execution_terminal(
        self, *, start_id: str, status: str, ended_at: str,
        provenance_kind: str, upstream_result_reference: str,
        upstream_result_commitment: str, authoritative_typed_outcome: str,
        sanitized_provenance: dict[str, object],
    ) -> LedgerRecord:
        """Issue the sole terminal transition for a durable governed start."""
        require_identity(start_id, "gate_invocation_start")
        start = self.get(start_id)
        if start is None or start.domain != "gate_invocation_start":
            raise ValueError("actual invocation terminal requires a matching start")
        start_payload = json.loads(start.semantic_json)
        gate = start_payload.get("gate")
        if status not in _ACTUAL_TERMINAL_STATUSES:
            raise ValueError("actual invocation terminal has invalid lifecycle status")
        if provenance_kind != _ACTUAL_PROVENANCE_KINDS.get(gate):
            raise ValueError("actual invocation terminal has wrong gate provenance")
        ended = self._parse_timestamp(ended_at, "ended_at")
        if ended < self._parse_timestamp(start_payload.get("started_at"), "started_at"):
            raise ValueError("actual invocation end precedes its durable start")
        for value, label in (
            (upstream_result_reference, "upstream result reference"),
            (authoritative_typed_outcome, "authoritative typed outcome"),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{label} must be non-empty trimmed text")
        self._require_sha256(upstream_result_commitment,
                             "upstream result commitment")
        if type(sanitized_provenance) is not dict:
            raise ValueError("sanitized provenance must be a dictionary")

        payload = {
            "start_id": start_id,
            "evaluation_run_id": start_payload["evaluation_run_id"],
            "execution_id": start_payload["execution_id"],
            "gate": start_payload["gate"],
            "profile_identity": start_payload["profile_identity"],
            "profile_commitment": start_payload["profile_commitment"],
            "invocation_id": start_payload["invocation_id"],
            "started_at": start_payload["started_at"],
            "ended_at": ended_at,
            "status": status,
            "provenance_kind": provenance_kind,
            "invocation_pins": start_payload["invocation_pins"],
            "upstream_result_reference": upstream_result_reference,
            "upstream_result_commitment": upstream_result_commitment,
            "authoritative_typed_outcome": authoritative_typed_outcome,
            "sanitized_provenance": sanitized_provenance,
        }
        return self._append_validated(LedgerRecord.create(
            "gate_invocation_receipt", [start_id, "terminal"], payload,
            start_id,
        ))

    @staticmethod
    def _parse_timestamp(value: object, label: str) -> datetime:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{label} must be non-empty timestamp text")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{label} must include a timezone")
        return parsed

    @staticmethod
    def _require_sha256(value: object, label: str) -> str:
        if (not isinstance(value, str) or not value.startswith("sha256:")
                or len(value) != 71):
            raise ValueError(f"{label} must be a SHA-256 commitment")
        try:
            int(value[7:], 16)
        except ValueError as exc:
            raise ValueError(f"{label} must be a SHA-256 commitment") from exc
        return value

    def _validate_links(self, record: LedgerRecord) -> None:
        payload = json.loads(record.semantic_json)
        key = json.loads(record.identity_key_json)
        if not isinstance(key, list) or len(key) < 2 or key[0] != record.parent_id:
            raise ValueError("identity key must include exact parent identity")
        required_parent = {
            "evaluation_run": "evaluation_plan", "execution": "evaluation_run",
            "observation": "execution", "judgment": "execution", "review": "execution",
            "re_adjudication": "judgment", "report": "evaluation_run",
            "gate_authorization": "evaluation_run",
            "gate_execution_receipt": "execution",
            "docker_namespace": "execution",
            "docker_cleanup": "docker_namespace",
            "gate_invocation_start": "execution",
            "gate_invocation_receipt": "gate_invocation_start",
            "rag_child_allocation": "execution",
        }[record.domain]
        require_identity(record.parent_id, required_parent)
        if required_parent == "evaluation_plan":
            if self.get_plan(record.parent_id) is None:
                raise ValueError("run plan is not admitted")
        elif self.get(record.parent_id) is None:
            raise ValueError("ledger parent does not exist")
        if record.domain == "evaluation_run":
            plan = self.get_plan(record.parent_id)
            if payload.get("oracle_revision_id") != plan.oracle_revision_id:
                raise ValueError("run must pin plan Oracle revision")
        if record.domain == "execution":
            run = self.get(record.parent_id)
            plan = self.get_plan(run.parent_id)
            original_shape = len(key) == 3
            replacement_shape = (
                len(key) == 5 and key[3] == "replacement"
                and isinstance(key[4], str)
            )
            if (not (original_shape or replacement_shape)
                    or key[1] not in {f"S{i}" for i in range(1, 7)}
                    or type(key[2]) is not int or not 1 <= key[2] <= plan.repetitions):
                raise ValueError("execution identity must bind scenario and admitted repetition")
            if payload.get("scenario_id") != key[1] or payload.get("repetition") != key[2]:
                raise ValueError("execution payload does not match identity")
            if replacement_shape:
                original_id = require_identity(key[4], "execution")
                original = self.get(original_id)
                if (original is None or original.domain != "execution"
                        or original.parent_id != record.parent_id):
                    raise ValueError("replacement must reference an existing execution in the run")
                original_key = json.loads(original.identity_key_json)
                if (len(original_key) != 3
                        or original_key[1:3] != key[1:3]
                        or payload.get("replacement_for_execution_id") != original_id
                        or payload.get("planned_slot_execution_id") != original_id):
                    raise ValueError("replacement lineage does not match the original planned slot")
            elif payload.get("replacement_for_execution_id") is not None:
                raise ValueError("original execution cannot claim replacement lineage")
        if record.domain == "gate_authorization":
            if (len(key) != 4
                    or key != [record.parent_id, payload.get("gate"),
                               payload.get("profile_identity"),
                               payload.get("profile_commitment")]
                    or payload.get("evaluation_run_id") != record.parent_id):
                raise ValueError("gate authorization does not bind exact run/gate/profile")
        if record.domain == "gate_execution_receipt":
            execution = self.get(record.parent_id)
            if (len(key) != 3
                    or key != [record.parent_id, payload.get("gate"),
                               payload.get("profile_commitment")]
                    or payload.get("execution_id") != record.parent_id
                    or payload.get("evaluation_run_id") != execution.parent_id):
                raise ValueError("gate receipt does not bind exact run/execution/gate/profile")
        if record.domain == "gate_invocation_start":
            execution = self.get(record.parent_id)
            if (len(key) != 4
                    or key != [record.parent_id, payload.get("gate"),
                               payload.get("profile_commitment"),
                               payload.get("invocation_id")]
                    or payload.get("execution_id") != record.parent_id
                    or payload.get("evaluation_run_id") != execution.parent_id):
                raise ValueError("gate invocation does not bind exact run/execution/gate/profile")
        if record.domain == "gate_invocation_receipt":
            start = self.get(record.parent_id)
            start_payload = json.loads(start.semantic_json)
            if (len(key) != 2 or key != [record.parent_id, "terminal"]
                    or payload.get("start_id") != record.parent_id
                    or payload.get("invocation_id") != start_payload.get("invocation_id")
                    or payload.get("evaluation_run_id") != start_payload.get("evaluation_run_id")
                    or payload.get("execution_id") != start_payload.get("execution_id")
                    or payload.get("gate") != start_payload.get("gate")
                    or payload.get("profile_identity") != start_payload.get("profile_identity")
                    or payload.get("profile_commitment") != start_payload.get("profile_commitment")
                    or payload.get("started_at") != start_payload.get("started_at")
                    or payload.get("invocation_pins") != start_payload.get("invocation_pins")):
                raise ValueError("terminal invocation receipt contradicts its durable start")
        if record.domain == "docker_namespace":
            execution = self.get(record.parent_id)
            if (len(key) != 2
                    or key != [record.parent_id, payload.get("profile_commitment")]
                    or payload.get("execution_id") != record.parent_id
                    or payload.get("evaluation_run_id") != execution.parent_id):
                raise ValueError("Docker namespace does not bind exact run/execution/profile")
        if record.domain == "docker_cleanup":
            namespace = self.get(record.parent_id)
            namespace_payload = json.loads(namespace.semantic_json)
            if (len(key) != 2 or key != [record.parent_id, "cleanup"]
                    or payload.get("namespace_id") != record.parent_id
                    or payload.get("project_name") !=
                        namespace_payload.get("project_name")
                    or payload.get("volume_identities") !=
                        namespace_payload.get("volume_identities")
                    or payload.get("workspace_root") !=
                        namespace_payload.get("workspace_root")):
                raise ValueError("Docker cleanup does not bind exact namespace target")
            invocation_fields = (
                payload.get("invocation_start_id"),
                payload.get("invocation_id"),
                payload.get("invocation_started_at"),
            )
            if any(value is not None for value in invocation_fields):
                if any(value is None for value in invocation_fields):
                    raise ValueError("Docker cleanup invocation binding is incomplete")
                start = self.get(payload["invocation_start_id"])
                if start is None or start.domain != "gate_invocation_start":
                    raise ValueError("Docker cleanup invocation start is missing")
                start_payload = json.loads(start.semantic_json)
                if (start_payload.get("gate") != "DOCKER_RCA_E2E"
                        or start_payload.get("evaluation_run_id") !=
                            namespace_payload.get("evaluation_run_id")
                        or start_payload.get("execution_id") !=
                            namespace_payload.get("execution_id")
                        or start_payload.get("profile_identity") !=
                            namespace_payload.get("profile_identity")
                        or start_payload.get("profile_commitment") !=
                            namespace_payload.get("profile_commitment")
                        or start_payload.get("invocation_id") !=
                            payload.get("invocation_id")
                        or start_payload.get("started_at") !=
                            payload.get("invocation_started_at")
                        or self._parse_timestamp(
                            payload.get("completed_at"), "completed_at") <
                            self._parse_timestamp(
                                payload.get("invocation_started_at"),
                                "invocation_started_at")):
                    raise ValueError(
                        "Docker cleanup does not bind exact invocation context")
        if record.domain == "judgment":
            if payload.get("kind") != "ORIGINAL":
                raise ValueError("original judgment kind required")
            run = self._run_for(record.parent_id)
            plan = self.get_plan(run.parent_id)
            if payload.get("oracle_revision_id") != plan.oracle_revision_id:
                raise ValueError("original judgment must use pinned Oracle")
        if record.domain == "re_adjudication":
            original = self.get(record.parent_id)
            if json.loads(original.semantic_json).get("kind") != "ORIGINAL":
                raise ValueError("re-adjudication must reference original judgment")
            run = self._run_for(original.parent_id)
            plan = self.get_plan(run.parent_id)
            if payload.get("run_id") != run.record_id or payload.get("original_judgment_id") != original.record_id:
                raise ValueError("re-adjudication lineage mismatch")
            if payload.get("original_oracle_revision_id") != plan.oracle_revision_id:
                raise ValueError("original Oracle revision mismatch")
            new_id = require_identity(payload.get("new_oracle_revision_id"), "oracle_revision")
            target = self.get_oracle(new_id)
            if new_id == plan.oracle_revision_id or target is None:
                raise ValueError("new approved Oracle revision required")
            seen = set()
            cursor = target
            while cursor.oracle_revision_id != plan.oracle_revision_id:
                if cursor.oracle_revision_id in seen or cursor.parent_revision_id is None:
                    raise ValueError("target Oracle is not an approved descendant of original")
                seen.add(cursor.oracle_revision_id)
                cursor = self.get_oracle(cursor.parent_revision_id)
                if cursor is None:
                    raise ValueError("Oracle amendment lineage is incomplete")
            for field in ("reason", "authorization_reference", "result"):
                if not isinstance(payload.get(field), str) or not payload[field].strip():
                    raise ValueError(f"{field} required for authorized re-adjudication")
            self._approvals.verify_amendment(payload["authorization_reference"], original.record_id,
                                             plan.oracle_revision_id, new_id)

    def _run_for(self, execution_id: str) -> LedgerRecord:
        execution = self.get(execution_id)
        if execution is None or execution.domain != "execution":
            raise ValueError("execution does not exist")
        run = self.get(execution.parent_id)
        if run is None or run.domain != "evaluation_run":
            raise ValueError("run does not exist")
        return run

    def get(self, record_id: str) -> LedgerRecord | None:
        row = self._db.execute("SELECT domain,parent_id,identity_key_json,semantic_json,semantic_commitment FROM ledger WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            return None
        result = LedgerRecord(row[0], record_id, row[1], row[2], row[3], row[4])
        try:
            result.validate()
            self._validate_links(result)
        except EvaluationStoreIntegrityError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise EvaluationStoreIntegrityError("invalid ledger record") from exc
        return result

    def readiness_check(self) -> None:
        try:
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise EvaluationStoreIntegrityError("SQLite integrity failure")
            if (self._db.execute("PRAGMA application_id").fetchone()[0] != APP_ID
                    or self._db.execute("PRAGMA user_version").fetchone()[0]
                        != self._schema_version):
                raise EvaluationStoreIntegrityError("evaluation schema identity changed")
            expected_schema = (_expected_v2_schema_definition()
                               if self._schema_version == 2
                               else _expected_schema_definition())
            if _schema_definition(self._db) != expected_schema:
                raise EvaluationStoreIntegrityError("evaluation schema definition changed")
            for revision_id, raw in self._db.execute("SELECT revision_id,content FROM oracle_revisions"):
                if canonical_bytes(json.loads(raw)) != raw:
                    raise EvaluationStoreIntegrityError("noncanonical Oracle row")
                self.get_oracle(revision_id)
            for plan_id, raw in self._db.execute("SELECT plan_id,content FROM plans"):
                if canonical_bytes(json.loads(raw)) != raw:
                    raise EvaluationStoreIntegrityError("noncanonical plan row")
                self.get_plan(plan_id)
            preparation_ids = {
                row[0] for row in self._db.execute(
                    "SELECT DISTINCT preparation_id FROM preparation_records")
            }
            for preparation_id in preparation_ids:
                prepared = self.get_preparation(preparation_id)
                if prepared is None:
                    raise EvaluationStoreIntegrityError(
                        "admission exists without preparation")
                rows = self._db.execute(
                    "SELECT record_id,kind,content,semantic_commitment "
                    "FROM preparation_records WHERE preparation_id=?",
                    (preparation_id,)).fetchall()
                for record_id, kind, raw, semantic_hash in rows:
                    payload = json.loads(raw)
                    expected_domain = (
                        "preparation" if kind == "PREPARED"
                        else "preparation"
                        if kind == "PREPARED_RESOURCES"
                        else "formal_admission")
                    expected_key = (
                        [preparation_id, kind]
                        if kind in {"PREPARED", "PREPARED_RESOURCES"}
                        else [preparation_id])
                    if (record_id != identity(expected_domain, expected_key)
                            or canonical_bytes(payload) != raw
                            or commitment(payload) != semantic_hash):
                        raise EvaluationStoreIntegrityError(
                            "preparation lifecycle row is invalid")
                    if kind == "PREPARED_RESOURCES":
                        try:
                            if (self._validate_preparation_payload(payload)
                                    != preparation_id
                                    or payload["docker_namespace"].get(
                                        "resource_state") != "PREPARED_RESOURCES"):
                                raise ValueError
                        except (KeyError, TypeError, ValueError) as exc:
                            raise EvaluationStoreIntegrityError(
                                "prepared resource row is invalid") from exc
                    if kind == "ADMITTED" and (
                            payload.get("schema") !=
                                "spec017.preparation-admission.v1"
                            or payload.get("lifecycle_state") != "ADMITTED"
                            or payload.get("preparation_id") != preparation_id
                            or payload.get("preparation_commitment") !=
                                commitment(prepared)
                            or not isinstance(
                                payload.get("formal_admission_payload"), dict)
                            or commitment(payload["formal_admission_payload"]) !=
                                payload.get("formal_admission_commitment")):
                        raise EvaluationStoreIntegrityError(
                            "Final Admission row is invalid")
            invocation_bindings: dict[str, tuple[object, ...]] = {}
            for (record_id,) in self._db.execute("SELECT record_id FROM ledger"):
                record = self.get(record_id)
                if record.domain == "gate_invocation_start":
                    payload = json.loads(record.semantic_json)
                    invocation_id = payload["invocation_id"]
                    binding = tuple(
                        payload.get(field)
                        for field in _ACTUAL_INVOCATION_BINDING_FIELDS
                    )
                    existing = invocation_bindings.setdefault(
                        invocation_id, binding)
                    if existing != binding:
                        raise EvaluationStoreIntegrityError(
                            "actual invocation identity has conflicting bindings"
                        )
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise EvaluationStoreIntegrityError("evaluation store readiness failed") from exc
