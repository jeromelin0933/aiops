"""Independent, append-only SQLite authority for Candidate-F evaluation records."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .authority import TrustedApprovalRegistry
from .identity import canonical_bytes, require_identity
from .ledger import LedgerConflict, LedgerRecord
from .oracle import EvaluationPlan, OracleRevision


APP_ID = 0x017E0A17
SCHEMA_VERSION = 1
_SCHEMA_SQL = """
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


def _schema_definition(db: sqlite3.Connection) -> tuple[tuple[str, str, str, str | None], ...]:
    return tuple(db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE type IN ('table','index','trigger') ORDER BY type,name"
    ))


def _expected_schema_definition() -> tuple[tuple[str, str, str, str | None], ...]:
    with sqlite3.connect(":memory:") as pristine:
        pristine.executescript(_SCHEMA_SQL)
        return _schema_definition(pristine)


class EvaluationStoreIntegrityError(RuntimeError):
    """Existing evaluation state cannot be trusted."""


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

    def _initialize(self) -> None:
        app_id = self._db.execute("PRAGMA application_id").fetchone()[0]
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        tables = self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if app_id == 0 and version == 0 and not tables:
            self._db.execute(f"PRAGMA application_id = {APP_ID}")
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.executescript(_SCHEMA_SQL)
        elif app_id != APP_ID or version != SCHEMA_VERSION:
            raise EvaluationStoreIntegrityError("foreign or unsupported evaluation database")

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

    def _validate_links(self, record: LedgerRecord) -> None:
        payload = json.loads(record.semantic_json)
        key = json.loads(record.identity_key_json)
        if not isinstance(key, list) or len(key) < 2 or key[0] != record.parent_id:
            raise ValueError("identity key must include exact parent identity")
        required_parent = {
            "evaluation_run": "evaluation_plan", "execution": "evaluation_run",
            "observation": "execution", "judgment": "execution", "review": "execution",
            "re_adjudication": "judgment", "report": "evaluation_run",
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
            if len(key) != 3 or key[1] not in {f"S{i}" for i in range(1, 7)} or type(key[2]) is not int or not 1 <= key[2] <= plan.repetitions:
                raise ValueError("execution identity must bind scenario and admitted repetition")
            if payload.get("scenario_id") != key[1] or payload.get("repetition") != key[2]:
                raise ValueError("execution payload does not match identity")
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
        except ValueError as exc:
            raise EvaluationStoreIntegrityError("invalid ledger record") from exc
        return result

    def readiness_check(self) -> None:
        try:
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise EvaluationStoreIntegrityError("SQLite integrity failure")
            if self._db.execute("PRAGMA application_id").fetchone()[0] != APP_ID or self._db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise EvaluationStoreIntegrityError("evaluation schema identity changed")
            if _schema_definition(self._db) != _expected_schema_definition():
                raise EvaluationStoreIntegrityError("evaluation schema definition changed")
            for revision_id, raw in self._db.execute("SELECT revision_id,content FROM oracle_revisions"):
                if canonical_bytes(json.loads(raw)) != raw:
                    raise EvaluationStoreIntegrityError("noncanonical Oracle row")
                self.get_oracle(revision_id)
            for plan_id, raw in self._db.execute("SELECT plan_id,content FROM plans"):
                if canonical_bytes(json.loads(raw)) != raw:
                    raise EvaluationStoreIntegrityError("noncanonical plan row")
                self.get_plan(plan_id)
            for (record_id,) in self._db.execute("SELECT record_id FROM ledger"):
                record = self.get(record_id)
                self._validate_links(record)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise EvaluationStoreIntegrityError("evaluation store readiness failed") from exc
