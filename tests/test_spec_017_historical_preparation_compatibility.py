from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from rca_evaluation.formal_admission import (
    RepositoryState, _final_admit_formal_campaign,
)
from rca_evaluation.identity import canonical_bytes, commitment, identity
from rca_evaluation.preparation import _build_payload
from rca_evaluation.sqlite_store import (
    APP_ID, EvaluationStoreIntegrityError, SqliteEvaluationStore,
    _PREPARATION_SCHEMA_V2_SQL, _SCHEMA_V1_SQL,
)
from tests.test_spec_017_r4_slice0_preparation import (
    BRANCH, CAMPAIGN, HEAD, ROOT, SHA, TRUST, _config, _prepare,
)


def _legacy_v2_payload(tmp_path: Path, database: Path) -> dict[str, object]:
    config = json.loads(_config(tmp_path).read_text(encoding="utf-8"))
    payload = _build_payload(
        repository_root=ROOT, config=config, campaign_key=CAMPAIGN,
        repository_branch=BRANCH, repository_commit=HEAD,
        database_path=database,
    )
    payload.pop("resource_facts")
    docker = payload["docker_namespace"]
    for name in (
        "resource_state", "resource_commitment", "topology_commitment",
        "actual_ownership_commitment",
    ):
        docker.pop(name)
    child = payload["real_rag_child"]
    for name in (
        "resource_state", "corpus_identity", "index_identity",
        "build_identity", "activation_identity", "artifacts",
        "resource_commitment",
    ):
        child.pop(name)
    return payload


def _write_v2_database(
    tmp_path: Path, *, mutate=None,
) -> tuple[Path, dict[str, object], bytes, str]:
    database = tmp_path / "legacy" / "rca_evaluation" / "evaluation.db"
    database.parent.mkdir(parents=True)
    payload = _legacy_v2_payload(tmp_path, database)
    if mutate is not None:
        mutate(payload)
    content = canonical_bytes(payload)
    semantic_commitment = commitment(payload)
    preparation_id = payload["preparation_id"]
    record_id = identity("preparation", [preparation_id, "PREPARED"])
    connection = sqlite3.connect(database)
    connection.execute(f"PRAGMA application_id = {APP_ID}")
    connection.execute("PRAGMA user_version = 2")
    connection.executescript(_SCHEMA_V1_SQL + _PREPARATION_SCHEMA_V2_SQL)
    connection.execute(
        "INSERT INTO preparation_records VALUES (?,?,?,?,?)",
        (record_id, preparation_id, "PREPARED", content, semantic_commitment),
    )
    connection.commit()
    connection.close()
    return database, payload, content, semantic_commitment


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v2_prepared_reload_projects_none_without_mutating_history(tmp_path):
    database, historical, content, semantic_commitment = _write_v2_database(tmp_path)
    before_file = _file_sha256(database)

    with SqliteEvaluationStore(database) as store:
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == 2
        loaded = store.get_preparation(historical["preparation_id"])
        assert loaded is not None
        assert loaded["resource_facts"] is None
        assert loaded["preparation_id"] == historical["preparation_id"]
        assert loaded["repository"] == historical["repository"]
        row = store._db.execute(
            "SELECT kind,content,semantic_commitment FROM preparation_records"
        ).fetchone()
        assert row == ("PREPARED", content, semantic_commitment)
        assert "resource_facts" not in json.loads(row[1])
        with pytest.raises(ValueError, match="only ADMITTED"):
            store.require_admitted_preparation(historical["preparation_id"], SHA)

    assert _file_sha256(database) == before_file


def test_v2_open_is_read_only_for_new_preparation_lifecycle_writes(tmp_path):
    database, historical, _, _ = _write_v2_database(tmp_path)
    before_file = _file_sha256(database)
    with SqliteEvaluationStore(database) as store:
        projected = store.get_preparation(historical["preparation_id"])
        with pytest.raises(EvaluationStoreIntegrityError, match="read-only"):
            store.prepare(projected)
    assert _file_sha256(database) == before_file


def test_v2_historical_read_does_not_bypass_source_freshness(tmp_path):
    database, historical, _, _ = _write_v2_database(tmp_path)
    config = _config(tmp_path)
    with SqliteEvaluationStore(database) as store:
        with pytest.raises(ValueError, match="stale or contradictory"):
            _final_admit_formal_campaign(
                preparation_id=historical["preparation_id"],
                evaluation_store=store, repository_root=ROOT,
                preregistration_path=config, trusted_approval_path=TRUST,
                reviewer_roster_path=tmp_path / "unused-reviewers.json",
                campaign_key=CAMPAIGN, database_path=database,
                evaluation_root=database.parent,
                repository_state=RepositoryState(BRANCH, "1" * 40, False),
            )


@pytest.mark.parametrize("mutation", [
    lambda payload: payload.update({"unknown_legacy_field": True}),
    lambda payload: payload["repository"].update({"commit": "malformed"}),
    lambda payload: payload.update({"lifecycle_state": "ADMITTED"}),
])
def test_v2_decoder_rejects_non_exact_or_malformed_payload(tmp_path, mutation):
    database, _, _, _ = _write_v2_database(tmp_path, mutate=mutation)
    with pytest.raises(EvaluationStoreIntegrityError, match="corrupt preparation"):
        SqliteEvaluationStore(database)


def test_current_decoder_still_requires_current_exact_root_and_nested_schema(tmp_path):
    report, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        current = store.get_preparation(report.preparation_id)
        missing = copy.deepcopy(current)
        missing.pop("resource_facts")
        with pytest.raises(ValueError, match="schema is invalid"):
            store._validate_preparation_payload(missing)

        legacy_nested = copy.deepcopy(current)
        legacy_nested["docker_namespace"].pop("resource_state")
        with pytest.raises(ValueError, match="Docker namespace is invalid"):
            store._validate_preparation_payload(legacy_nested)
