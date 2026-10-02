"""Focused S1 authority, isolation, and replay checks."""

from dataclasses import FrozenInstanceError, replace
import json
import sqlite3

import pytest

from rca_evaluation.authority import (
    PM_AUTHORITY, TrustedAmendmentAuthorization, TrustedApprovalRegistry,
    TrustedOracleApproval,
)
from rca_evaluation.identity import canonical_bytes, commitment, identity
from rca_evaluation.ledger import LedgerConflict, LedgerRecord
from rca_evaluation.oracle import EvaluationPlan, OracleRevision
from rca_evaluation.sqlite_store import EvaluationStoreIntegrityError, SqliteEvaluationStore


def semantics():
    return {
        "scenarios": {f"S{i}": {f"O{j}": f"approved-{i}-{j}" for j in range(1, 11)} for i in range(1, 7)},
        "cross_scenario_rules": ["approved comparison rule"],
    }


def oracle(content=None, parent=None, reference="PM-017-approval"):
    return OracleRevision.approved(
        oracle_set_id="PM-S1-S6", semantics=semantics() if content is None else content,
        parent_revision_id=parent, created_at="2026-10-02T00:00:00Z",
        approval_authority=PM_AUTHORITY, approval_reference=reference,
        approved_at="2026-10-02T01:00:00Z",
    )


def trusted(*revisions, amendments=()):
    return TrustedApprovalRegistry(tuple(
        TrustedOracleApproval(r.approval_reference, PM_AUTHORITY, r.oracle_revision_id,
                              r.semantic_content_commitment, r.approved_at, r.parent_revision_id)
        for r in revisions
    ), tuple(amendments))


def plan(revision):
    return EvaluationPlan.admit(
        revision, fixture_revision="fixture-r1", production_build="build-a",
        model="model-a", prompt="prompt-hash", schema="schema-hash",
        corpus_index="index-hash", configuration="config-hash", repetitions=5,
    )


def test_canonicalization_commitment_and_immutable_revision():
    assert canonical_bytes({"b": 2, "a": 1}) == canonical_bytes({"a": 1, "b": 2})
    assert commitment({"b": 2, "a": 1}) == commitment({"a": 1, "b": 2})
    assert oracle({"cross_scenario_rules": ["approved comparison rule"], "scenarios": semantics()["scenarios"]}).oracle_revision_id == oracle().oracle_revision_id
    with pytest.raises(ValueError):
        canonical_bytes({"value": 1.0})
    original = oracle()
    with pytest.raises(FrozenInstanceError):
        original.status = "DRAFT"
    changed = semantics()
    changed["scenarios"]["S1"]["O1"] = "new approved rule"
    assert oracle(changed).oracle_revision_id != original.oracle_revision_id
    assert original.semantic_content_commitment != oracle(changed).semantic_content_commitment


def test_approval_version_and_exact_pinning(tmp_path):
    revision = oracle()
    with pytest.raises(ValueError):
        OracleRevision.approved(oracle_set_id="set", semantics=semantics(), created_at="x",
                                approval_authority="OTHER", approval_reference="approval", approved_at="x")
    with pytest.raises(ValueError):
        replace(revision, schema_version="99").validate()
    with pytest.raises(ValueError):
        oracle({"scenarios": {"S1": {}}, "cross_scenario_rules": ["rule"]})
    admitted = plan(revision)
    db_path = tmp_path / "rca_evaluation" / "evaluation.db"
    changed = semantics()
    changed["scenarios"]["S2"]["O2"] = "changed"
    later = oracle(changed, parent=revision.oracle_revision_id, reference="PM-017-later")
    with SqliteEvaluationStore(db_path, approvals=trusted(revision, later)) as store:
        with pytest.raises(ValueError):
            store.admit_plan(admitted)
        store.admit_oracle(revision)
        store.admit_plan(admitted)
        assert store.get_plan(admitted.evaluation_plan_id) == admitted
        store.admit_oracle(later)
        with pytest.raises(ValueError):
            store.admit_plan(replace(admitted, oracle_revision_id=later.oracle_revision_id))


def test_identity_domains_and_append_only_replay(tmp_path):
    ids = {identity(domain, ["same-key"]) for domain in (
        "evaluation_plan", "evaluation_run", "execution", "observation", "judgment", "review", "re_adjudication", "report")}
    assert len(ids) == 8
    revision = oracle()
    admitted = plan(revision)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / "evaluation.db",
                               approvals=trusted(revision)) as store:
        store.admit_oracle(revision)
        store.admit_plan(admitted)
        run = LedgerRecord.create("evaluation_run", [admitted.evaluation_plan_id, "campaign-1"],
                                  {"oracle_revision_id": revision.oracle_revision_id}, admitted.evaluation_plan_id)
        assert store.append(run) == store.append(run)
        execution = LedgerRecord.create("execution", [run.record_id, "S1", 1],
                                        {"scenario_id": "S1", "repetition": 1}, run.record_id)
        store.append(execution)
        observation = LedgerRecord.create("observation", [execution.record_id, "public-1"],
                                          {"public_output_commitment": "sha256:abc"}, execution.record_id)
        store.append(observation)
        store.append(LedgerRecord.create("review", [execution.record_id, "reviewer-1"],
                                         {"reviewer": "reviewer-1", "finding": "bounded"}, execution.record_id))
        store.append(LedgerRecord.create("report", [run.record_id, "report-1"],
                                         {"basis": "original"}, run.record_id))
        with pytest.raises(LedgerConflict):
            store.append(LedgerRecord.create("observation", [execution.record_id, "public-1"],
                                              {"public_output_commitment": "sha256:different"}, execution.record_id))
        with pytest.raises(sqlite3.IntegrityError):
            store._db.execute("UPDATE ledger SET semantic_json='{}' WHERE record_id=?", (observation.record_id,))
        store.readiness_check()


def test_re_adjudication_preserves_original_judgment(tmp_path):
    first = oracle()
    altered = semantics()
    altered["scenarios"]["S1"]["O1"] = "later-approved"
    later = oracle(altered, parent=first.oracle_revision_id, reference="PM-017-later")
    p = plan(first)
    amendment = TrustedAmendmentAuthorization("PM-017-amendment", PM_AUTHORITY,
                                              identity("judgment", [identity("execution", [identity("evaluation_run", [p.evaluation_plan_id, "run"]), "S1", 1]), "original"]),
                                              first.oracle_revision_id, later.oracle_revision_id)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / "evaluation.db",
                               approvals=trusted(first, later, amendments=(amendment,))) as store:
        store.admit_oracle(first)
        store.admit_oracle(later)
        store.admit_plan(p)
        run = store.append(LedgerRecord.create("evaluation_run", [p.evaluation_plan_id, "run"],
                                                {"oracle_revision_id": first.oracle_revision_id}, p.evaluation_plan_id))
        execution = store.append(LedgerRecord.create("execution", [run.record_id, "S1", 1],
                                                      {"scenario_id": "S1", "repetition": 1}, run.record_id))
        original = store.append(LedgerRecord.create("judgment", [execution.record_id, "original"],
                                                     {"kind": "ORIGINAL", "oracle_revision_id": first.oracle_revision_id,
                                                      "result": "MATCH"}, execution.record_id))
        payload = {"run_id": run.record_id, "original_judgment_id": original.record_id,
                   "original_oracle_revision_id": first.oracle_revision_id,
                   "new_oracle_revision_id": later.oracle_revision_id, "reason": "approved correction",
                   "authorization_reference": "PM-017-amendment", "result": "NO_MATCH"}
        with pytest.raises(ValueError):
            store.append(LedgerRecord.create("re_adjudication", [original.record_id, "later"],
                                              {**payload, "authorization_reference": ""}, original.record_id))
        new = store.append(LedgerRecord.create("re_adjudication", [original.record_id, "later"],
                                                payload, original.record_id))
        assert json.loads(store.get(original.record_id).semantic_json)["result"] == "MATCH"
        assert json.loads(store.get(new.record_id).semantic_json)["result"] == "NO_MATCH"
        assert store.get_plan(p.evaluation_plan_id).oracle_revision_id == first.oracle_revision_id


def test_store_isolation_and_integrity(tmp_path):
    production = tmp_path / "production.db"
    sqlite3.connect(production).execute("CREATE TABLE events (id TEXT)").connection.close()
    with pytest.raises(ValueError):
        SqliteEvaluationStore(production)
    eval_path = tmp_path / "rca_evaluation" / "evaluation.db"
    revision = oracle()
    with SqliteEvaluationStore(eval_path, approvals=trusted(revision)) as store:
        store.admit_oracle(revision)
    assert sqlite3.connect(production).execute("SELECT name FROM sqlite_master WHERE name='events'").fetchone()
    with sqlite3.connect(eval_path) as connection:
        connection.execute("DROP TRIGGER oracle_no_update")
    with pytest.raises(EvaluationStoreIntegrityError):
        SqliteEvaluationStore(eval_path)


def test_trusted_oracle_approval_binds_exact_revision_and_commitment(tmp_path):
    approved = oracle()
    with pytest.raises(ValueError, match="invalid trusted Oracle approval"):
        TrustedApprovalRegistry((TrustedOracleApproval(
            approved.approval_reference, "SPEC Lead", approved.oracle_revision_id,
            approved.semantic_content_commitment, approved.approved_at,
            approved.parent_revision_id,
        ),))
    revised_content = semantics()
    revised_content["scenarios"]["S1"]["O1"] = "different"
    changed = oracle(revised_content)
    path = tmp_path / "rca_evaluation" / "approval.db"
    with SqliteEvaluationStore(path, approvals=trusted(approved)) as store:
        store.admit_oracle(approved)
        with pytest.raises(ValueError, match="unknown or does not bind"):
            store.admit_oracle(changed)
        with pytest.raises(ValueError, match="unknown or does not bind"):
            store.admit_oracle(replace(approved, approval_reference="forged"))
        with pytest.raises(ValueError, match="unknown or does not bind"):
            store.admit_oracle(replace(approved, approved_at="2026-10-03T01:00:00Z"))
        with pytest.raises(ValueError, match="unknown or does not bind"):
            store.admit_oracle(replace(approved, parent_revision_id=approved.oracle_revision_id))
        with pytest.raises(ValueError, match="content commitment mismatch"):
            store.admit_oracle(replace(approved, semantic_content_commitment="sha256:" + "0" * 64))
    with pytest.raises(EvaluationStoreIntegrityError):
        SqliteEvaluationStore(path)  # no independent trusted source on restart
    with SqliteEvaluationStore(path, approvals=trusted(approved)) as store:
        assert store.get_oracle(approved.oracle_revision_id) == approved


def test_authorized_re_adjudication_exact_scope_and_lineage(tmp_path):
    first = oracle()
    changed = semantics()
    changed["scenarios"]["S1"]["O1"] = "amended"
    target = oracle(changed, parent=first.oracle_revision_id, reference="PM-target")
    unrelated_content = semantics()
    unrelated_content["scenarios"]["S2"]["O2"] = "unrelated"
    unrelated = oracle(unrelated_content, reference="PM-unrelated")
    changed_again = semantics()
    changed_again["scenarios"]["S1"]["O1"] = "another amendment"
    other_target = oracle(changed_again, parent=first.oracle_revision_id, reference="PM-other")
    p = plan(first)
    run_id = identity("evaluation_run", [p.evaluation_plan_id, "run"])
    execution_id = identity("execution", [run_id, "S1", 1])
    original_id = identity("judgment", [execution_id, "original"])
    auth = TrustedAmendmentAuthorization("PM-amendment", PM_AUTHORITY, original_id,
                                         first.oracle_revision_id, target.oracle_revision_id)
    with pytest.raises(ValueError, match="invalid trusted amendment authorization"):
        TrustedApprovalRegistry(amendments=(TrustedAmendmentAuthorization(
            "SPEC-Lead-amendment", "SPEC Lead", original_id,
            first.oracle_revision_id, target.oracle_revision_id,
        ),))
    wrong_original_revision = TrustedAmendmentAuthorization(
        "PM-wrong-original", PM_AUTHORITY, original_id,
        unrelated.oracle_revision_id, target.oracle_revision_id)
    path = tmp_path / "rca_evaluation" / "amendment.db"
    with SqliteEvaluationStore(path, approvals=trusted(first, target, unrelated, other_target,
                                                       amendments=(auth, wrong_original_revision))) as store:
        for revision in (first, target, unrelated, other_target):
            store.admit_oracle(revision)
        store.admit_plan(p)
        run = store.append(LedgerRecord.create("evaluation_run", [p.evaluation_plan_id, "run"],
                                                {"oracle_revision_id": first.oracle_revision_id}, p.evaluation_plan_id))
        execution = store.append(LedgerRecord.create("execution", [run.record_id, "S1", 1],
                                                      {"scenario_id": "S1", "repetition": 1}, run.record_id))
        original = store.append(LedgerRecord.create("judgment", [execution.record_id, "original"],
                                                     {"kind": "ORIGINAL", "oracle_revision_id": first.oracle_revision_id,
                                                      "result": "MATCH"}, execution.record_id))
        second_execution = store.append(LedgerRecord.create("execution", [run.record_id, "S1", 2],
                                                             {"scenario_id": "S1", "repetition": 2}, run.record_id))
        second_original = store.append(LedgerRecord.create("judgment", [second_execution.record_id, "original"],
                                                            {"kind": "ORIGINAL", "oracle_revision_id": first.oracle_revision_id,
                                                             "result": "MATCH"}, second_execution.record_id))
        base = {"run_id": run.record_id, "original_judgment_id": original.record_id,
                "original_oracle_revision_id": first.oracle_revision_id,
                "new_oracle_revision_id": target.oracle_revision_id,
                "reason": "approved correction", "authorization_reference": auth.reference,
                "result": "NO_MATCH"}

        def attempt(parent, payload, label):
            return store.append(LedgerRecord.create("re_adjudication", [parent, label], payload, parent))

        with pytest.raises(ValueError, match="unknown or out of scope"):
            attempt(original.record_id, {**base, "authorization_reference": "forged"}, "forged")
        with pytest.raises(ValueError, match="unknown or out of scope"):
            attempt(second_original.record_id, {**base, "original_judgment_id": second_original.record_id}, "wrong-judgment")
        with pytest.raises(ValueError, match="unknown or out of scope"):
            attempt(original.record_id, {**base, "new_oracle_revision_id": other_target.oracle_revision_id}, "wrong-target")
        with pytest.raises(ValueError, match="unknown or out of scope"):
            attempt(original.record_id, {**base, "authorization_reference": "PM-wrong-original"}, "wrong-original-revision")
        with pytest.raises(ValueError, match="not an approved descendant"):
            attempt(original.record_id, {**base, "new_oracle_revision_id": unrelated.oracle_revision_id}, "unrelated")
        later = attempt(original.record_id, base, "authorized")
        assert attempt(original.record_id, base, "authorized") == later
        assert json.loads(store.get(original.record_id).semantic_json)["result"] == "MATCH"
        assert json.loads(store.get(later.record_id).semantic_json)["result"] == "NO_MATCH"


def test_one_original_judgment_per_execution_domain_and_sqlite(tmp_path):
    revision = oracle()
    p = plan(revision)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / "original.db",
                               approvals=trusted(revision)) as store:
        store.admit_oracle(revision)
        store.admit_plan(p)
        run = store.append(LedgerRecord.create("evaluation_run", [p.evaluation_plan_id, "run"],
                                                {"oracle_revision_id": revision.oracle_revision_id}, p.evaluation_plan_id))
        execution = store.append(LedgerRecord.create("execution", [run.record_id, "S1", 1],
                                                      {"scenario_id": "S1", "repetition": 1}, run.record_id))
        first = LedgerRecord.create("judgment", [execution.record_id, "original"],
                                    {"kind": "ORIGINAL", "oracle_revision_id": revision.oracle_revision_id,
                                     "result": "MATCH"}, execution.record_id)
        store.append(first)
        assert store.append(first) == first
        with pytest.raises(LedgerConflict):
            store.append(LedgerRecord.create("judgment", [execution.record_id, "original"],
                                              {"kind": "ORIGINAL", "oracle_revision_id": revision.oracle_revision_id,
                                               "result": "NO_MATCH"}, execution.record_id))
        different_id = LedgerRecord.create("judgment", [execution.record_id, "another-original"],
                                           {"kind": "ORIGINAL", "oracle_revision_id": revision.oracle_revision_id,
                                            "result": "MATCH"}, execution.record_id)
        with pytest.raises(LedgerConflict):
            store.append(different_id)
        with pytest.raises(sqlite3.IntegrityError):
            store._db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)", (
                different_id.record_id, different_id.domain, different_id.parent_id,
                different_id.identity_key_json, different_id.semantic_json, different_id.semantic_commitment))
        assert store.get(first.record_id) == first
        store.readiness_check()


def test_same_named_noop_trigger_and_table_drift_fail_readiness(tmp_path):
    path = tmp_path / "rca_evaluation" / "schema.db"
    with SqliteEvaluationStore(path) as store:
        store.readiness_check()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER ledger_no_update")
        connection.execute("CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT 1; END")
    with pytest.raises(EvaluationStoreIntegrityError, match="schema definition changed"):
        SqliteEvaluationStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER ledger_no_update")
        connection.execute("CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'immutable'); END")
        connection.execute("ALTER TABLE plans ADD COLUMN drift TEXT")
    with pytest.raises(EvaluationStoreIntegrityError, match="schema definition changed"):
        SqliteEvaluationStore(path)
