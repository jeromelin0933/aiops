from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from rca_evaluation.formal_admission import (
    RepositoryState, final_admit_formal_campaign,
)
from rca_evaluation.identity import canonical_bytes, commitment, identity
from rca_evaluation.ledger import LedgerConflict
from rca_evaluation.preparation import (
    PREPARATION_AUTHORITY, PREPARED_CHILD_MARKER, prepare_formal_campaign,
    recover_preparation, verify_prepared_real_rag_child,
)
from rca_evaluation.readiness import (
    FormalRequiredFactsResolution, ReadinessFactState, RequiredFact,
    RequiredFactsMaterialization,
)
from rca_evaluation.sqlite_store import SqliteEvaluationStore
import rca_evaluation.formal_admission as admission_module
import rca_evaluation.preparation as preparation_module


ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG = ROOT / "configs/rca_evaluation/formal_preregistration_v1.json"
TRUST = ROOT / "configs/rca_evaluation/trust/spec017_oracle_approval_v1.json"
HEAD = "e9cee50ce7904fb822e8032bbe8b05a4a826cf07"
BRANCH = "feature/spec-017-formal-evaluation-readiness"
CAMPAIGN = "formal-campaign-2026-10"
SHA = "sha256:" + "a" * 64


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _config(tmp_path: Path) -> Path:
    raw = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    raw["evaluation_plan"]["production_build"] = HEAD
    raw["evaluation_plan"]["corpus_index"] = "knowledge-build:test-pinned"
    inputs = raw["manifest_inputs"]
    inputs["environment_identity"] = "pytest:python-pinned"
    approved = inputs["approved_spec_commitments"]
    approved["knowledge_document_revisions"] = ["test-doc@1"]
    approved["knowledge_build_identity"] = "knowledge-build:test-pinned"
    approved["docker_image_digest"] = SHA
    approved["docker_namespace_identity"] = "prepared-at-final-read"
    approved["tool_versions"] = {"python": "3.test", "docker": "test"}
    return _write_json(tmp_path / "formal.json", raw)


def _reviewers(tmp_path: Path) -> Path:
    return _write_json(tmp_path / "reviewers.json", {
        "schema": "spec017.reviewer-roster.v1",
        "stage1": {"reviewer_ids": ["a", "b"], "reviewer_kind": "HUMAN",
                   "blind_diagnostic_only": True, "official_verdict_allowed": False,
                   "disagreement": "THIRD_HUMAN_TIEBREAK",
                   "tiebreak_reviewer_id": "c", "tiebreak_reviewer_kind": "HUMAN"},
        "stage2": {"reviewer_ids": ["d", "e"], "reviewer_kind": "HUMAN",
                   "independent": True, "disagreement": "THIRD_HUMAN_TIEBREAK",
                   "tiebreak_reviewer_id": "f", "tiebreak_reviewer_kind": "HUMAN"},
        "machine_hard_failure_override_allowed": False,
        "records_append_only": True,
    })


def _prepare(tmp_path: Path, config: Path | None = None):
    evaluation = tmp_path / "rca_evaluation"
    report = prepare_formal_campaign(
        repository_root=ROOT, preregistration_path=config or _config(tmp_path),
        campaign_key=CAMPAIGN, database_path=evaluation / "evaluation.db",
        evaluation_root=evaluation,
        repository_state=RepositoryState(BRANCH, HEAD, False))
    return report, evaluation


def _resolved() -> FormalRequiredFactsResolution:
    fact = RequiredFact("test", ReadinessFactState.RESOLVED, "verified",
                        "governed test authority", "fresh-read")
    return FormalRequiredFactsResolution(
        RequiredFactsMaterialization.create({"/evaluation_plan/production_build": fact}),
        (fact,))


def _child_path(prepared, evaluation: Path) -> Path:
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        payload = store.get_preparation(prepared.preparation_id)
    return evaluation / payload["real_rag_child"]["relative_path"]


def _assert_fresh_namespace(evaluation: Path) -> None:
    preparation_module._namespace_shape(
        evaluation / "evaluation.db", evaluation)


def test_absent_real_rag_root_is_fresh_and_read_only(tmp_path):
    evaluation = tmp_path / "rca_evaluation"
    evaluation.mkdir()

    _assert_fresh_namespace(evaluation)

    assert not (evaluation / "evaluation.db").exists()
    assert not (evaluation / "real_rag").exists()


def test_empty_real_rag_root_is_fresh_and_read_only(tmp_path):
    evaluation = tmp_path / "rca_evaluation"
    isolated_root = evaluation / "real_rag"
    isolated_root.mkdir(parents=True)
    before = isolated_root.stat()

    _assert_fresh_namespace(evaluation)

    after = isolated_root.stat()
    assert list(isolated_root.iterdir()) == []
    assert not (evaluation / "evaluation.db").exists()
    assert (after.st_mode, after.st_size, after.st_mtime_ns) == (
        before.st_mode, before.st_size, before.st_mtime_ns)


@pytest.mark.parametrize("entry_kind", [
    "unknown_child", "unknown_file", "ownership_marker", "candidate_artifact",
])
def test_nonempty_real_rag_root_without_authority_fails_closed(
    tmp_path, entry_kind,
):
    evaluation = tmp_path / "rca_evaluation"
    isolated_root = evaluation / "real_rag"
    isolated_root.mkdir(parents=True)
    if entry_kind == "unknown_child":
        (isolated_root / "unknown-child").mkdir()
    else:
        name = {
            "unknown_file": "unexpected.txt",
            "ownership_marker": PREPARED_CHILD_MARKER,
            "candidate_artifact": "chroma.sqlite3",
        }[entry_kind]
        (isolated_root / name).write_text("unauthorized", encoding="utf-8")

    with pytest.raises(ValueError, match="stale Real-RAG state"):
        _assert_fresh_namespace(evaluation)


def test_real_rag_namespace_must_be_exact_safe_directory(tmp_path):
    evaluation = tmp_path / "rca_evaluation"
    evaluation.mkdir()
    (evaluation / "real_rag").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="stale Real-RAG state"):
        _assert_fresh_namespace(evaluation)

    unrelated = tmp_path / "not_rca_evaluation"
    unrelated.mkdir()
    with pytest.raises(ValueError, match="dedicated rca_evaluation DB namespace"):
        preparation_module._namespace_shape(
            unrelated / "evaluation.db", unrelated)


def _cli_args(prepared, evaluation: Path, config: Path, reviewers: Path) -> list[str]:
    return [
        "--admission-only", "--preparation-id", prepared.preparation_id,
        "--repository-root", str(ROOT), "--config", str(config),
        "--trusted-oracle-approval", str(TRUST),
        "--reviewer-roster", str(reviewers), "--campaign-key", CAMPAIGN,
        "--database", str(evaluation / "evaluation.db"),
        "--evaluation-root", str(evaluation),
    ]


def test_pristine_prepare_is_durable_idempotent_and_restart_recoverable(tmp_path):
    config = _config(tmp_path)
    first, evaluation = _prepare(tmp_path, config)
    second, _ = _prepare(tmp_path, config)
    assert first == second
    assert first.lifecycle_state == "PREPARED"
    assert first.preparation_commitment == second.preparation_commitment
    with SqliteEvaluationStore(evaluation / "evaluation.db") as restarted:
        recovered = recover_preparation(restarted, first.preparation_id)
        assert recovered == first
        payload = restarted.get_preparation(first.preparation_id)
        assert payload["authority_reference"] == PREPARATION_AUTHORITY
        assert payload["planned_lineage"]["counts_as_formal_execution"] is False
        assert payload["docker_namespace"]["resources_created"] is False
        assert payload["real_rag_child"]["state"] == "ALLOCATED"
        assert verify_prepared_real_rag_child(
            restarted, first.preparation_id, ROOT) == payload["real_rag_child"]
        assert restarted.has_no_formal_execution_facts()


def test_prepare_creates_no_external_resource_denominator_gate_or_scenario_fact(tmp_path):
    report, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        payload = store.get_preparation(report.preparation_id)
        assert payload["effects"] == {
            "formal_execution": False, "external_gates": False,
            "scenario_injection": False, "denominator_consumed": False,
        }
        assert store._db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        assert store._db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
        assert store._db.execute("SELECT COUNT(*) FROM oracle_revisions").fetchone()[0] == 0
    child = evaluation / payload["real_rag_child"]["relative_path"]
    assert set(child.iterdir()) == {child / PREPARED_CHILD_MARKER}


def test_non_pristine_store_and_contradictory_replay_fail_closed(tmp_path):
    report, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        payload = store.get_preparation(report.preparation_id)
        changed = json.loads(json.dumps(payload))
        changed["repository"]["configuration_reference"] = "contradiction.json"
        with pytest.raises(LedgerConflict, match="contradictory preparation"):
            store.prepare(changed)

        malformed = json.loads(json.dumps(payload))
        malformed["lifecycle_state"] = "ADMITTED"
        with pytest.raises(ValueError, match="lifecycle"):
            store.prepare(malformed)


def test_prepared_state_cannot_execute(tmp_path):
    report, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        with pytest.raises(ValueError, match="only ADMITTED"):
            store.require_admitted_preparation(report.preparation_id, SHA)


def test_supported_final_admission_has_no_repository_assertion_seam_and_fresh_reads(
    tmp_path, monkeypatch,
):
    assert "repository_state" not in inspect.signature(
        final_admit_formal_campaign).parameters
    config = _config(tmp_path)
    prepared, evaluation = _prepare(tmp_path, config)
    calls = []

    def current(root):
        calls.append(Path(root).resolve())
        return RepositoryState(BRANCH, "1" * 40, False)

    monkeypatch.setattr(admission_module, "inspect_repository", current)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        with pytest.raises(ValueError, match="stale or contradictory"):
            final_admit_formal_campaign(
                preparation_id=prepared.preparation_id, evaluation_store=store,
                repository_root=ROOT, preregistration_path=config,
                trusted_approval_path=TRUST,
                reviewer_roster_path=_reviewers(tmp_path),
                campaign_key=CAMPAIGN,
                database_path=evaluation / "evaluation.db",
                evaluation_root=evaluation)
    assert calls == [ROOT]


@pytest.mark.parametrize("mutation,match", [
    ("marker_missing", "marker is missing"),
    ("marker_mismatch", "marker mismatch"),
    ("stale_lifecycle", "marker mismatch"),
    ("unexpected_content", "unexpected state"),
    ("duplicate_child", "duplicate or stale"),
])
def test_current_child_contradictions_block(
    tmp_path, mutation, match,
):
    prepared, evaluation = _prepare(tmp_path)
    child = _child_path(prepared, evaluation)
    marker = child / PREPARED_CHILD_MARKER
    if mutation == "marker_missing":
        marker.unlink()
    elif mutation in {"marker_mismatch", "stale_lifecycle"}:
        value = json.loads(marker.read_text(encoding="utf-8"))
        value["child_identity" if mutation == "marker_mismatch"
              else "lifecycle_state"] = "wrong"
        marker.write_text(json.dumps(value), encoding="utf-8")
    elif mutation == "unexpected_content":
        (child / "unexpected-index-file").write_text("not a build", encoding="utf-8")
    else:
        sibling = child.parent / "duplicate-child"
        sibling.mkdir()
        (sibling / PREPARED_CHILD_MARKER).write_text("{}", encoding="utf-8")
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        with pytest.raises(ValueError, match=match):
            verify_prepared_real_rag_child(store, prepared.preparation_id, ROOT)


def test_missing_allocation_and_wrong_durable_owner_block(tmp_path):
    prepared, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        with pytest.raises(ValueError, match="missing"):
            verify_prepared_real_rag_child(
                store, identity("preparation", ["missing"]), ROOT)
        payload = store.get_preparation(prepared.preparation_id)
        payload["real_rag_child"]["ownership"] = identity(
            "preparation", ["wrong-owner"])
        with pytest.raises(ValueError, match="Real-RAG child allocation"):
            store._validate_preparation_payload(payload)


def test_corrupt_durable_child_allocation_fails_store_fresh_read(tmp_path):
    prepared, evaluation = _prepare(tmp_path)
    database = evaluation / "evaluation.db"
    with SqliteEvaluationStore(database) as store:
        payload = store.get_preparation(prepared.preparation_id)
    payload["real_rag_child"]["ownership"] = identity(
        "preparation", ["wrong-owner"])
    import sqlite3
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER preparation_no_update")
    connection.execute(
        "UPDATE preparation_records SET content=?,semantic_commitment=? "
        "WHERE preparation_id=? AND kind='PREPARED'",
        (canonical_bytes(payload), commitment(payload), prepared.preparation_id))
    connection.execute(
        "CREATE TRIGGER preparation_no_update BEFORE UPDATE ON "
        "preparation_records BEGIN SELECT RAISE(ABORT, 'immutable'); END")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="corrupt preparation"):
        SqliteEvaluationStore(database)


def test_production_or_shared_root_overlap_blocks(tmp_path, monkeypatch):
    prepared, evaluation = _prepare(tmp_path)
    child = _child_path(prepared, evaluation)
    documents = iter((
        {"knowledge": {"config_path": "configs/knowledge_index.yaml"}},
        {"index": {"path": str(child)}},
    ))
    import rca_evaluation.preparation as preparation_module
    monkeypatch.setattr(preparation_module.yaml, "safe_load", lambda stream: next(documents))
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        with pytest.raises(ValueError, match="overlaps production/shared"):
            verify_prepared_real_rag_child(store, prepared.preparation_id, ROOT)


def test_cached_preparation_cannot_mask_current_child_contradiction_at_final(
    tmp_path, monkeypatch,
):
    config = _config(tmp_path)
    prepared, evaluation = _prepare(tmp_path, config)
    child = _child_path(prepared, evaluation)
    (child / "unexpected-current-state").write_text("stale", encoding="utf-8")
    monkeypatch.setattr(admission_module, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    monkeypatch.setattr(admission_module, "resolve_formal_required_facts",
                        lambda *args, **kwargs: _resolved())
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        with pytest.raises(ValueError, match="unexpected state"):
            final_admit_formal_campaign(
                preparation_id=prepared.preparation_id, evaluation_store=store,
                repository_root=ROOT, preregistration_path=config,
                trusted_approval_path=TRUST,
                reviewer_roster_path=_reviewers(tmp_path),
                campaign_key=CAMPAIGN,
                database_path=evaluation / "evaluation.db",
                evaluation_root=evaluation)


@pytest.mark.parametrize("mutation,match", [
    ("commitment", "readiness preparation commitment"),
    ("lineage", "planned lineage is invalid"),
    ("effects", "Formal execution effects"),
])
def test_store_rejects_structurally_invalid_preparation(tmp_path, mutation, match):
    prepared, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        payload = store.get_preparation(prepared.preparation_id)
        if mutation == "commitment":
            payload["readiness_preparation"]["commitment"] = SHA
        elif mutation == "lineage":
            payload["planned_lineage"].pop("planned_execution_identity")
        else:
            payload["effects"]["formal_execution"] = True
        with pytest.raises(ValueError, match=match):
            store.prepare(payload)


def test_invalid_prepared_to_admitted_transition_is_rejected(tmp_path):
    prepared, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        source = store.get_preparation(prepared.preparation_id)
        invalid = {
            "schema": "spec017.preparation-admission.v1",
            "lifecycle_state": "ADMITTED",
            "preparation_id": prepared.preparation_id,
            "preparation_commitment": SHA,
            "formal_admission_commitment": commitment({"proof": "one"}),
            "formal_admission_payload": {"proof": "one"},
        }
        assert commitment(source) != SHA
        with pytest.raises(ValueError, match="contradicts preparation"):
            store.admit_preparation(prepared.preparation_id, invalid)


def test_final_admission_fresh_reads_and_appends_only_admitted(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation_module, "validate_prepared_resource_freshness",
                        lambda **_: None)
    config = _config(tmp_path)
    prepared, evaluation = _prepare(tmp_path, config)
    calls = []

    def fresh(*args, **kwargs):
        calls.append(kwargs["preparation_id"])
        return _resolved()

    monkeypatch.setattr(admission_module, "resolve_formal_required_facts", fresh)
    monkeypatch.setattr(admission_module, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        admitted = final_admit_formal_campaign(
            preparation_id=prepared.preparation_id, evaluation_store=store,
            repository_root=ROOT, preregistration_path=config,
            trusted_approval_path=TRUST, reviewer_roster_path=_reviewers(tmp_path),
            campaign_key=CAMPAIGN, database_path=evaluation / "evaluation.db",
            evaluation_root=evaluation)
        assert calls == [prepared.preparation_id]
        assert admitted.preparation_id == prepared.preparation_id
        assert store.require_admitted_preparation(
            prepared.preparation_id, admitted.admission_commitment
        )["lifecycle_state"] == "ADMITTED"
        assert store.has_no_formal_execution_facts()
        replay = final_admit_formal_campaign(
            preparation_id=prepared.preparation_id, evaluation_store=store,
            repository_root=ROOT, preregistration_path=config,
            trusted_approval_path=TRUST, reviewer_roster_path=_reviewers(tmp_path),
            campaign_key=CAMPAIGN, database_path=evaluation / "evaluation.db",
            evaluation_root=evaluation)
        assert replay.admission_commitment == admitted.admission_commitment
        assert calls == [prepared.preparation_id, prepared.preparation_id]


def test_final_admission_blocks_stale_commitment_and_unresolved_fact(tmp_path, monkeypatch):
    config = _config(tmp_path)
    prepared, evaluation = _prepare(tmp_path, config)
    changed = json.loads(config.read_text(encoding="utf-8"))
    changed["evaluation_plan"]["model"] = "drifted"
    drift = _write_json(tmp_path / "drift.json", changed)
    monkeypatch.setattr(admission_module, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        with pytest.raises(ValueError, match="stale or contradictory"):
            final_admit_formal_campaign(
                preparation_id=prepared.preparation_id, evaluation_store=store,
                repository_root=ROOT, preregistration_path=drift,
                trusted_approval_path=TRUST, reviewer_roster_path=_reviewers(tmp_path),
                campaign_key=CAMPAIGN, database_path=evaluation / "evaluation.db",
                evaluation_root=evaluation)


def test_final_admission_re_resolves_current_readiness_on_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation_module, "validate_prepared_resource_freshness",
                        lambda **_: None)
    config = _config(tmp_path)
    prepared, evaluation = _prepare(tmp_path, config)
    monkeypatch.setattr(admission_module, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    unresolved = RequiredFact(
        "real_rag_activation", ReadinessFactState.UNRESOLVED, None,
        "Candidate-C public API", "current activation is absent")
    results = iter((
        _resolved(),
        FormalRequiredFactsResolution(
            RequiredFactsMaterialization.create({"/x": unresolved}),
            (unresolved,)),
    ))
    monkeypatch.setattr(admission_module, "resolve_formal_required_facts",
                        lambda *args, **kwargs: next(results))
    values = dict(
        preparation_id=prepared.preparation_id,
        repository_root=ROOT, preregistration_path=config,
        trusted_approval_path=TRUST, reviewer_roster_path=_reviewers(tmp_path),
        campaign_key=CAMPAIGN, database_path=evaluation / "evaluation.db",
        evaluation_root=evaluation)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        admitted = final_admit_formal_campaign(
            evaluation_store=store, **values)
        assert admitted.preparation_id == prepared.preparation_id
        with pytest.raises(ValueError, match="unresolved required fact"):
            final_admit_formal_campaign(evaluation_store=store, **values)


def test_public_cli_final_admission_is_durable_and_replay_is_exact(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(preparation_module, "validate_prepared_resource_freshness",
                        lambda **_: None)
    config = _config(tmp_path)
    reviewers = _reviewers(tmp_path)
    prepared, evaluation = _prepare(tmp_path, config)
    calls = []

    def fresh(*args, **kwargs):
        calls.append(kwargs["preparation_id"])
        return _resolved()

    monkeypatch.setattr(admission_module, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    monkeypatch.setattr(admission_module, "resolve_formal_required_facts", fresh)
    args = _cli_args(prepared, evaluation, config, reviewers)
    assert admission_module.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["mode"] == "FINAL_FORMAL_ADMISSION"
    assert first["admitted"] is True
    assert first["preparation_id"] == prepared.preparation_id

    assert admission_module.main(args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay == first
    assert calls == [prepared.preparation_id, prepared.preparation_id]

    contradiction = RequiredFact(
        "current_readiness", ReadinessFactState.CONTRADICTORY, None,
        "governed public source", "current fact contradicts admission")
    current = FormalRequiredFactsResolution(
        RequiredFactsMaterialization.create({"/current": contradiction}),
        (contradiction,))
    monkeypatch.setattr(admission_module, "resolve_formal_required_facts",
                        lambda *args, **kwargs: current)
    assert admission_module.main(args) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["admitted"] is False
    assert "contradictory required fact" in blocked["error"]
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        durable = store.require_admitted_preparation(
            prepared.preparation_id, first["admission_commitment"])
        assert durable["formal_admission_payload"]["lifecycle_state"] == "ADMITTED"
        assert store._db.execute(
            "SELECT COUNT(*) FROM preparation_records WHERE kind='ADMITTED'"
        ).fetchone()[0] == 1
        assert store._db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        assert store._db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
        assert store.has_no_formal_execution_facts()


def test_public_cli_missing_preparation_in_existing_store_is_blocked(
    tmp_path, monkeypatch, capsys,
):
    evaluation = tmp_path / "rca_evaluation"
    database = evaluation / "evaluation.db"
    with SqliteEvaluationStore(database):
        pass
    monkeypatch.setattr(admission_module, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    missing = type("Missing", (), {
        "preparation_id": identity("preparation", ["missing"]),
    })()
    code = admission_module.main(_cli_args(
        missing, evaluation, _config(tmp_path), _reviewers(tmp_path)))
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["admitted"] is False
    assert "requires PREPARED" in output["error"]


@pytest.mark.parametrize("failure", ["unresolved", "stale"])
def test_public_cli_current_readiness_or_preparation_drift_is_blocked(
    tmp_path, monkeypatch, capsys, failure,
):
    config = _config(tmp_path)
    prepared, evaluation = _prepare(tmp_path, config)
    monkeypatch.setattr(admission_module, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    if failure == "unresolved":
        fact = RequiredFact(
            "current_readiness", ReadinessFactState.UNRESOLVED, None,
            "governed public source", "current fact is absent")
        unresolved = FormalRequiredFactsResolution(
            RequiredFactsMaterialization.create({"/current": fact}), (fact,))
        monkeypatch.setattr(
            admission_module, "resolve_formal_required_facts",
            lambda *args, **kwargs: unresolved)
    else:
        changed = json.loads(config.read_text(encoding="utf-8"))
        changed["evaluation_plan"]["model"] = "drifted"
        config = _write_json(tmp_path / "drifted-formal.json", changed)
    code = admission_module.main(_cli_args(
        prepared, evaluation, config, _reviewers(tmp_path)))
    output = json.loads(capsys.readouterr().out)
    assert code == 2 and output["admitted"] is False
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        with pytest.raises(ValueError, match="only ADMITTED"):
            store.require_admitted_preparation(prepared.preparation_id, SHA)


def test_public_cli_never_reports_success_when_durable_readback_fails(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(preparation_module, "validate_prepared_resource_freshness",
                        lambda **_: None)
    config = _config(tmp_path)
    prepared, evaluation = _prepare(tmp_path, config)
    monkeypatch.setattr(admission_module, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    monkeypatch.setattr(admission_module, "resolve_formal_required_facts",
                        lambda *args, **kwargs: _resolved())
    monkeypatch.setattr(
        SqliteEvaluationStore, "require_admitted_preparation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("simulated durable read-back failure")))
    code = admission_module.main(_cli_args(
        prepared, evaluation, config, _reviewers(tmp_path)))
    output = json.loads(capsys.readouterr().out)
    assert code == 2 and output["admitted"] is False
    assert "read-back failure" in output["error"]


def test_admission_contradiction_is_append_only_fail_closed(tmp_path):
    prepared, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        source = store.get_preparation(prepared.preparation_id)
        base = {
            "schema": "spec017.preparation-admission.v1",
            "lifecycle_state": "ADMITTED",
            "preparation_id": prepared.preparation_id,
            "preparation_commitment": commitment(source),
            "formal_admission_commitment": commitment({"proof": "one"}),
            "formal_admission_payload": {"proof": "one"},
        }
        store.admit_preparation(prepared.preparation_id, base)
        changed = {
            **base,
            "formal_admission_commitment": commitment({"proof": "two"}),
            "formal_admission_payload": {"proof": "two"},
        }
        with pytest.raises(LedgerConflict, match="contradictory Final Admission"):
            store.admit_preparation(prepared.preparation_id, changed)


def test_unknown_or_partial_schema_fails_closed(tmp_path):
    database = tmp_path / "rca_evaluation" / "evaluation.db"
    database.parent.mkdir(parents=True)
    import sqlite3
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA application_id = 24644119")
    connection.execute("PRAGMA user_version = 99")
    connection.execute("CREATE TABLE unknown_state(value TEXT)")
    connection.close()
    with pytest.raises(RuntimeError, match="unsupported evaluation database"):
        SqliteEvaluationStore(database)


def test_exact_v1_schema_migrates_additively_without_promoting_records(tmp_path):
    import sqlite3
    from rca_evaluation.sqlite_store import APP_ID, _SCHEMA_V1_SQL

    database = tmp_path / "rca_evaluation" / "evaluation.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute(f"PRAGMA application_id = {APP_ID}")
    connection.execute("PRAGMA user_version = 1")
    connection.executescript(_SCHEMA_V1_SQL)
    connection.close()
    with SqliteEvaluationStore(database) as migrated:
        assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert migrated.is_pristine_for_formal_admission()
        assert migrated._db.execute(
            "SELECT COUNT(*) FROM preparation_records").fetchone()[0] == 0
