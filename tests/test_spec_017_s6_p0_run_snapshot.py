"""S6-P0 authoritative, coherent run-ledger snapshot tests."""

from dataclasses import FrozenInstanceError
import sqlite3
import threading

import pytest

import rca_evaluation.adjudication as protected_adjudication
from rca_evaluation.adjudication import (
    ADJUDICATION_EVALUATOR_IDENTITY, ADJUDICATION_EVALUATOR_VERSION,
)

from rca_evaluation.authority import (
    PM_AUTHORITY, TrustedAmendmentAuthorization, TrustedApprovalRegistry,
    TrustedOracleApproval,
)
from rca_evaluation.identity import canonical_bytes, commitment, identity
from rca_evaluation.ledger import LedgerRecord
from rca_evaluation.oracle import EvaluationPlan, OracleRevision
from rca_evaluation.sqlite_store import (
    EvaluationRunSnapshotError, EvaluationStoreIntegrityError,
    SqliteEvaluationStore,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def _semantics(label: str = "approved") -> dict:
    return {
        "scenarios": {
            f"S{i}": {f"O{j}": f"{label}-{i}-{j}" for j in range(1, 11)}
            for i in range(1, 7)
        },
        "cross_scenario_rules": ["approved comparison rule"],
    }


def _oracle(label: str = "approved", *, parent: str | None = None) -> OracleRevision:
    return OracleRevision.approved(
        oracle_set_id="PM-S1-S6", semantics=_semantics(label),
        parent_revision_id=parent, created_at="2026-10-02T00:00:00Z",
        approval_authority=PM_AUTHORITY,
        approval_reference=f"PM-017-{label}",
        approved_at="2026-10-02T01:00:00Z",
    )


def _plan(revision: OracleRevision) -> EvaluationPlan:
    return EvaluationPlan.admit(
        revision, fixture_revision="fixture-r1", production_build="build-a",
        model="model-a", prompt="prompt-hash", schema="schema-hash",
        corpus_index="index-hash", configuration="config-hash", repetitions=5,
    )


def _approvals(*revisions: OracleRevision, amendments=()) -> TrustedApprovalRegistry:
    return TrustedApprovalRegistry(tuple(
        TrustedOracleApproval(
            item.approval_reference, PM_AUTHORITY, item.oracle_revision_id,
            item.semantic_content_commitment, item.approved_at,
            item.parent_revision_id,
        ) for item in revisions
    ), tuple(amendments))


def _run(store: SqliteEvaluationStore, plan: EvaluationPlan, label: str):
    run = store.append(LedgerRecord.create(
        "evaluation_run", [plan.evaluation_plan_id, label],
        {"oracle_revision_id": plan.oracle_revision_id}, plan.evaluation_plan_id,
    ))
    execution = store.append(LedgerRecord.create(
        "execution", [run.record_id, "S1", 1],
        {"scenario_id": "S1", "repetition": 1}, run.record_id,
    ))
    return run, execution


def _protected_original(store, plan, execution):
    source = store.append(LedgerRecord.create(
        "observation", [execution.record_id, "protected-source"],
        {"kind": "PUBLIC_OBSERVATION"}, execution.record_id))
    input_commitment = commitment({"execution": execution.record_id})
    findings = [{
        "production_claim_id": "claim-1",
        "atomic_commitment": SHA_A, "disposition": "SUPPORTED"}]
    ucr = {"status": "SCORED", "numerator": 0, "denominator": 1,
           "contradicted_count": 0, "rate_fraction": [0, 1]}
    evaluated = commitment({
        **ucr, "findings": findings, "integrity_errors": []})
    reviews = tuple(store.append(LedgerRecord.create(
        "review", [execution.record_id, "CLAIM_ADJUDICATION", reviewer], {
            "stage": "CLAIM_ADJUDICATION", "reviewer_id": reviewer,
            "input_commitment": input_commitment,
            "claim_facts_commitment": SHA_A, "result_commitment": evaluated,
        }, execution.record_id)) for reviewer in ("reviewer-a", "reviewer-b"))
    claim_sources = (source, *reviews)
    claim_payload = {
        "kind": "UNSUPPORTED_CLAIM_RESULT",
        "governed_adjudicator_identity": ADJUDICATION_EVALUATOR_IDENTITY,
        "adjudicator_version": ADJUDICATION_EVALUATOR_VERSION,
        "evaluation_run_id": execution.parent_id,
        "execution_id": execution.record_id, "scenario_id": "S1",
        "oracle_revision_id": plan.oracle_revision_id,
        "oracle_scenario_commitment": SHA_B,
        "input_commitment": input_commitment,
        "production_claim_identities": ["claim-1"],
        "production_claim_commitments": [SHA_A],
        "claim_adjudications": findings,
        "claim_review_ids": [item.record_id for item in reviews],
        "source_record_ids": [item.record_id for item in claim_sources],
        "source_record_commitments": [
            item.semantic_commitment for item in claim_sources],
        "claim_facts_commitment": SHA_A,
        "unsupported_claim_result": ucr,
        "governed_result_commitment": commitment({
            "claim_adjudications": findings, "ucr": ucr,
            "claim_facts_commitment": SHA_A}),
    }
    claim = store._append_protected_adjudication(LedgerRecord.create(
        "observation", [execution.record_id, "UNSUPPORTED_CLAIM_RESULT",
                        claim_payload["governed_result_commitment"]],
        claim_payload, execution.record_id),
        authority=protected_adjudication._ADJUDICATION_ISSUANCE_AUTHORITY)
    scoring = {"root_cause_identification_success": True}
    stage2 = tuple(store.append(LedgerRecord.create(
        "review", [execution.record_id, "STAGE2", reviewer], {
            "stage": "STAGE2", "reviewer_id": reviewer,
            "oracle_revision_id": plan.oracle_revision_id,
            "oracle_scenario_id": "S1", "oracle_scenario_commitment": SHA_B,
            "top_hypothesis": {"causal_class": "OTHER",
                               "granularity": "operational",
                               "accepted_alternative_id": None},
            "root_cause_success": True,
        }, execution.record_id)) for reviewer in ("stage2-a", "stage2-b"))
    judgment_sources = (source, claim, *reviews, *stage2)
    payload = {
        "kind": "ORIGINAL",
        "governed_adjudicator_identity": ADJUDICATION_EVALUATOR_IDENTITY,
        "adjudicator_version": ADJUDICATION_EVALUATOR_VERSION,
        "evaluation_run_id": execution.parent_id,
        "execution_id": execution.record_id,
        "oracle_revision_id": plan.oracle_revision_id,
        "oracle_scenario_id": "S1", "oracle_scenario_commitment": SHA_B,
        "result": "PASS",
        "root_cause_predicates": scoring,
        "stage2_review_ids": [item.record_id for item in stage2],
        "final_scoring_commitment": commitment(scoring),
        "claim_result_record_id": claim.record_id,
        "claim_result_record_commitment": claim.semantic_commitment,
        "unsupported_claim_result": ucr,
        "source_record_ids": [item.record_id for item in judgment_sources],
        "source_record_commitments": [
            item.semantic_commitment for item in judgment_sources],
        "predicate_scoring_basis_commitment": commitment({
            "root_cause": scoring, "unsupported_claim_result": ucr}),
    }
    payload["judgment_commitment"] = commitment(payload)
    return store._append_protected_adjudication(LedgerRecord.create(
        "judgment", [execution.record_id, "original"], payload,
        execution.record_id),
        authority=protected_adjudication._ADJUDICATION_ISSUANCE_AUTHORITY)


def _insert_raw(store: SqliteEvaluationStore, record: LedgerRecord) -> None:
    store._db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)", (
        record.record_id, record.domain, record.parent_id,
        record.identity_key_json, record.semantic_json,
        record.semantic_commitment,
    ))


def _assert_corruption_fails_all_authoritative_reads(
    reader, run_id: str, record_id: str,
) -> None:
    with pytest.raises(EvaluationStoreIntegrityError):
        reader.get(record_id)
    with pytest.raises(EvaluationStoreIntegrityError):
        reader.readiness_check()
    with pytest.raises(EvaluationStoreIntegrityError):
        reader.read_run_snapshot(run_id)


def test_snapshot_complete_closure_excludes_other_run_and_preserves_nested_gate_facts(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    path = tmp_path / "rca_evaluation" / "snapshot.db"
    with SqliteEvaluationStore(path, approvals=_approvals(revision)) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, execution = _run(store, plan, "run-a")
        other_run, other_execution = _run(store, plan, "run-b")
        observation = store.append(LedgerRecord.create(
            "observation", [execution.record_id, "public"],
            {"kind": "PUBLIC_OBSERVATION"}, execution.record_id,
        ))
        review = store.append(LedgerRecord.create(
            "review", [execution.record_id, "reviewer"],
            {"reviewer": "reviewer", "finding": "bounded"}, execution.record_id,
        ))
        judgment = _protected_original(store, plan, execution)
        profile = "gemini-profile"
        authorization = store.append(LedgerRecord.create(
            "gate_authorization", [run.record_id, "REAL_LLM", profile, SHA_A],
            {"evaluation_run_id": run.record_id, "gate": "REAL_LLM",
             "profile_identity": profile, "profile_commitment": SHA_A},
            run.record_id,
        ))
        start = store._record_actual_execution_start(
            evaluation_run_id=run.record_id, execution_id=execution.record_id,
            gate="REAL_LLM", profile_identity=profile,
            profile_commitment=SHA_A, invocation_id="invocation-1",
            started_at="2026-10-02T02:00:00Z", invocation_pins={},
        )
        terminal = store._record_actual_execution_terminal(
            start_id=start.record_id, status="EXECUTED_FAIL",
            ended_at="2026-10-02T02:00:01Z",
            provenance_kind="LIVE_GEMINI_INVOCATION",
            upstream_result_reference="result-1",
            upstream_result_commitment=SHA_B,
            authoritative_typed_outcome="FAILURE", sanitized_provenance={},
        )
        namespace = store.append(LedgerRecord.create(
            "docker_namespace", [execution.record_id, SHA_A],
            {"evaluation_run_id": run.record_id,
             "execution_id": execution.record_id,
             "profile_identity": "docker-profile", "profile_commitment": SHA_A,
             "project_name": "spec017-project", "volume_identities": ["volume-a"],
             "workspace_root": "/workspace/spec017"}, execution.record_id,
        ))
        cleanup = store.append(LedgerRecord.create(
            "docker_cleanup", [namespace.record_id, "cleanup"],
            {"namespace_id": namespace.record_id, "project_name": "spec017-project",
             "volume_identities": ["volume-a"],
             "workspace_root": "/workspace/spec017",
             "completed_at": "2026-10-02T02:00:02Z",
             "disposition": "COMPLETED"}, namespace.record_id,
        ))
        report = store.append(LedgerRecord.create(
            "report", [run.record_id, "existing"], {"projection": "old"},
            run.record_id,
        ))
        other = store.append(LedgerRecord.create(
            "observation", [other_execution.record_id, "other"],
            {"kind": "OTHER_RUN"}, other_execution.record_id,
        ))

        reader = store.reader()
        snapshot = reader.read_run_snapshot(run.record_id)
        ids = tuple(item.record_id for item in snapshot.records)
        assert {
            run.record_id, execution.record_id, observation.record_id,
            review.record_id, judgment.record_id, authorization.record_id,
            start.record_id, terminal.record_id, namespace.record_id,
            cleanup.record_id, report.record_id,
        } <= set(ids)
        assert len(ids) == 17
        assert other_run.record_id not in ids
        assert other.record_id not in ids
        assert ids == tuple(item.record_id for item in
                            reader.read_run_snapshot(run.record_id).records)
        assert snapshot.report_source_record_count == len(snapshot.records) - 1
        assert snapshot.integrity_status == "READY"


def test_snapshot_contains_all_authorized_re_adjudications(tmp_path):
    first = _oracle("first")
    second = _oracle("second", parent=first.oracle_revision_id)
    third = _oracle("third", parent=second.oracle_revision_id)
    plan = _plan(first)
    run_id = identity("evaluation_run", [plan.evaluation_plan_id, "run"])
    execution_id = identity("execution", [run_id, "S1", 1])
    original_id = identity("judgment", [execution_id, "original"])
    amendments = (
        TrustedAmendmentAuthorization(
            "PM-auth-second", PM_AUTHORITY, original_id,
            first.oracle_revision_id, second.oracle_revision_id),
        TrustedAmendmentAuthorization(
            "PM-auth-third", PM_AUTHORITY, original_id,
            first.oracle_revision_id, third.oracle_revision_id),
    )
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "readjudication.db",
        approvals=_approvals(first, second, third, amendments=amendments),
    ) as store:
        for revision in (first, second, third):
            store.admit_oracle(revision)
        store.admit_plan(plan)
        run, execution = _run(store, plan, "run")
        original = _protected_original(store, plan, execution)
        readjudications = []
        for label, target, authorization, result in (
            ("second", second, "PM-auth-second", "FAIL"),
            ("third", third, "PM-auth-third", "PASS"),
        ):
            readjudications.append(store.append(LedgerRecord.create(
                "re_adjudication", [original.record_id, label],
                {"run_id": run.record_id,
                 "original_judgment_id": original.record_id,
                 "original_oracle_revision_id": first.oracle_revision_id,
                 "new_oracle_revision_id": target.oracle_revision_id,
                 "reason": f"authorized {label}",
                 "authorization_reference": authorization, "result": result},
                original.record_id,
            )))
        snapshot = store.reader().read_run_snapshot(run.record_id)
        assert {item.record_id for item in snapshot.records} >= {
            original.record_id, *(item.record_id for item in readjudications),
        }


def test_source_cut_changes_for_source_fact_but_not_report_projection(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "cut.db",
        approvals=_approvals(revision),
    ) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, execution = _run(store, plan, "run")
        reader = store.reader()
        first = reader.read_run_snapshot(run.record_id)
        same = reader.read_run_snapshot(run.record_id)
        assert first.report_source_cut_commitment == same.report_source_cut_commitment
        assert first.records == same.records

        store.append(LedgerRecord.create(
            "observation", [execution.record_id, "new-source"],
            {"kind": "NEW_SOURCE"}, execution.record_id,
        ))
        with_source = reader.read_run_snapshot(run.record_id)
        assert with_source.report_source_cut_commitment != first.report_source_cut_commitment

        store.append(LedgerRecord.create(
            "report", [run.record_id, "projection"],
            {"source_cut": with_source.report_source_cut_commitment}, run.record_id,
        ))
        with_report = reader.read_run_snapshot(run.record_id)
        assert len(with_report.records) == len(with_source.records) + 1
        assert (with_report.report_source_cut_commitment ==
                with_source.report_source_cut_commitment)


def test_snapshot_root_bounds_and_read_only_contract(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "bounds.db",
        approvals=_approvals(revision),
    ) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, execution = _run(store, plan, "run")
        reader = store.reader()
        with pytest.raises(EvaluationRunSnapshotError, match="does not exist"):
            reader.read_run_snapshot(identity(
                "evaluation_run", [plan.evaluation_plan_id, "unknown"]))
        with pytest.raises(ValueError, match="evaluation_run"):
            reader.read_run_snapshot(execution.record_id)
        with pytest.raises(EvaluationRunSnapshotError, match="record bound"):
            reader.read_run_snapshot(run.record_id, max_records=1)
        with pytest.raises(EvaluationRunSnapshotError, match="byte bound"):
            reader.read_run_snapshot(run.record_id, max_canonical_bytes=1)
        snapshot = reader.read_run_snapshot(run.record_id)
        with pytest.raises(FrozenInstanceError):
            snapshot.integrity_status = "CHANGED"
        for mutation in ("append", "update", "delete", "_db"):
            assert not hasattr(reader, mutation)


def test_snapshot_fails_closed_on_dangling_record(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    path = tmp_path / "rca_evaluation" / "corrupt.db"
    with SqliteEvaluationStore(path, approvals=_approvals(revision)) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, _ = _run(store, plan, "run")
        dangling_parent = identity("execution", [run.record_id, "S2", 1])
        malformed = LedgerRecord.create(
            "observation", [dangling_parent, "dangling"],
            {"kind": "DANGLING"}, dangling_parent,
        )
        store._db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)", (
            malformed.record_id, malformed.domain, malformed.parent_id,
            malformed.identity_key_json, malformed.semantic_json,
            malformed.semantic_commitment,
        ))
        with pytest.raises(EvaluationStoreIntegrityError):
            store.reader()


@pytest.mark.parametrize("domain,parent_name", (
    ("observation", "execution"),
    ("report", "run"),
))
def test_authoritative_reads_reject_non_object_semantic_payload(
    tmp_path, domain, parent_name,
):
    revision = _oracle()
    plan = _plan(revision)
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / f"non-object-{domain}.db",
        approvals=_approvals(revision),
    ) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, execution = _run(store, plan, "run")
        reader = store.reader()
        parent_id = execution.record_id if parent_name == "execution" else run.record_id
        key = [parent_id, "malformed-list"]
        malformed = LedgerRecord(
            domain, identity(domain, key), parent_id, canonical_bytes(key),
            canonical_bytes([]), commitment([]),
        )
        _insert_raw(store, malformed)

        _assert_corruption_fails_all_authoritative_reads(
            reader, run.record_id, malformed.record_id)


def test_authoritative_reads_reject_existing_wrong_parent_domain(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "wrong-parent-domain.db",
        approvals=_approvals(revision),
    ) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, _ = _run(store, plan, "run")
        reader = store.reader()
        key = [run.record_id, "observation-cannot-parent-to-run"]
        malformed = LedgerRecord(
            "observation", identity("observation", key), run.record_id,
            canonical_bytes(key), canonical_bytes({"kind": "CORRUPT"}),
            commitment({"kind": "CORRUPT"}),
        )
        _insert_raw(store, malformed)

        _assert_corruption_fails_all_authoritative_reads(
            reader, run.record_id, malformed.record_id)


def test_authoritative_reads_reject_malformed_semantic_identity_structure(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "malformed-identity.db",
        approvals=_approvals(revision),
    ) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, _ = _run(store, plan, "run")
        reader = store.reader()
        key = [run.record_id, "S1", "not-an-integer-repetition"]
        payload = {"scenario_id": "S1", "repetition": 1}
        malformed = LedgerRecord(
            "execution", identity("execution", key), run.record_id,
            canonical_bytes(key), canonical_bytes(payload), commitment(payload),
        )
        _insert_raw(store, malformed)

        _assert_corruption_fails_all_authoritative_reads(
            reader, run.record_id, malformed.record_id)


def test_authoritative_reads_reject_semantic_commitment_contradiction(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "semantic-contradiction.db",
        approvals=_approvals(revision),
    ) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, execution = _run(store, plan, "run")
        reader = store.reader()
        key = [execution.record_id, "contradictory-commitment"]
        malformed = LedgerRecord(
            "observation", identity("observation", key), execution.record_id,
            canonical_bytes(key), canonical_bytes({"kind": "PAYLOAD_A"}),
            commitment({"kind": "PAYLOAD_B"}),
        )
        _insert_raw(store, malformed)

        _assert_corruption_fails_all_authoritative_reads(
            reader, run.record_id, malformed.record_id)


def test_transitive_traversal_run_lineage_contradiction_fails_without_partial_snapshot(
    tmp_path,
):
    revision = _oracle()
    plan = _plan(revision)
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "traversal-contradiction.db",
        approvals=_approvals(revision),
    ) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, execution = _run(store, plan, "run-a")
        other_run, _ = _run(store, plan, "run-b")
        valid = store.append(LedgerRecord.create(
            "observation", [execution.record_id, "valid-prefix"],
            {"kind": "VALID"}, execution.record_id,
        ))
        reader = store.reader()
        key = [execution.record_id, "REAL_LLM", SHA_A]
        payload = {
            "evaluation_run_id": other_run.record_id,
            "execution_id": execution.record_id,
            "gate": "REAL_LLM",
            "profile_commitment": SHA_A,
        }
        malformed = LedgerRecord(
            "gate_execution_receipt",
            identity("gate_execution_receipt", key), execution.record_id,
            canonical_bytes(key), canonical_bytes(payload), commitment(payload),
        )
        _insert_raw(store, malformed)

        with pytest.raises(EvaluationStoreIntegrityError):
            reader.get(malformed.record_id)
        with pytest.raises(EvaluationStoreIntegrityError):
            reader.readiness_check()
        with pytest.raises(EvaluationStoreIntegrityError):
            reader.read_run_snapshot(run.record_id)
        assert valid.record_id != malformed.record_id


def test_canonical_order_is_independent_of_sibling_physical_insertion_order(
    tmp_path,
):
    revision = _oracle()
    plan = _plan(revision)

    def build(path, insertion_order):
        with SqliteEvaluationStore(path, approvals=_approvals(revision)) as store:
            store.admit_oracle(revision)
            store.admit_plan(plan)
            run, execution = _run(store, plan, "same-logical-run")
            siblings = {
                "observation-x": LedgerRecord.create(
                    "observation", [execution.record_id, "x"],
                    {"kind": "X"}, execution.record_id),
                "observation-y": LedgerRecord.create(
                    "observation", [execution.record_id, "y"],
                    {"kind": "Y"}, execution.record_id),
                "review": LedgerRecord.create(
                    "review", [execution.record_id, "reviewer"],
                    {"reviewer": "reviewer", "finding": "valid"},
                    execution.record_id),
            }
            for name in insertion_order:
                store.append(siblings[name])
            snapshot = store.reader().read_run_snapshot(run.record_id)
            expected = (
                run.record_id,
                execution.record_id,
                *sorted((siblings["observation-x"].record_id,
                         siblings["observation-y"].record_id)),
                siblings["review"].record_id,
            )
            actual = tuple(item.record_id for item in snapshot.records)
            assert actual == expected
            return actual, snapshot.report_source_cut_commitment

    first = build(
        tmp_path / "a" / "rca_evaluation" / "ordering.db",
        ("observation-x", "review", "observation-y"),
    )
    second = build(
        tmp_path / "b" / "rca_evaluation" / "ordering.db",
        ("observation-y", "observation-x", "review"),
    )
    assert first == second


def test_snapshot_concurrent_append_is_a_coherent_before_or_after_cut(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    approvals = _approvals(revision)
    path = tmp_path / "rca_evaluation" / "concurrent.db"
    with SqliteEvaluationStore(path, approvals=approvals) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, execution = _run(store, plan, "run")
        appended = tuple(LedgerRecord.create(
            "observation", [execution.record_id, f"concurrent-{index}"],
            {"kind": "CONCURRENT", "index": index}, execution.record_id,
        ) for index in (1, 2))
        reader = store.reader()
        reader_has_cut = threading.Event()
        writer_inserted = threading.Event()
        original_readiness = store.readiness_check

        def synchronized_readiness():
            original_readiness()
            reader_has_cut.set()
            assert writer_inserted.wait(timeout=5)

        store.readiness_check = synchronized_readiness

        def append_concurrently():
            assert reader_has_cut.wait(timeout=5)
            connection = sqlite3.connect(path, isolation_level=None)
            connection.execute("PRAGMA busy_timeout = 5000")
            try:
                connection.execute("BEGIN IMMEDIATE")
                for item in appended:
                    connection.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)", (
                        item.record_id, item.domain, item.parent_id,
                        item.identity_key_json, item.semantic_json,
                        item.semantic_commitment,
                    ))
                writer_inserted.set()
                connection.execute("COMMIT")
            finally:
                connection.close()

        thread = threading.Thread(target=append_concurrently)
        thread.start()
        before = reader.read_run_snapshot(run.record_id)
        thread.join(timeout=5)
        assert not thread.is_alive()
        store.readiness_check = original_readiness
        after = store.reader().read_run_snapshot(run.record_id)
        before_ids = {item.record_id for item in before.records}
        after_ids = {item.record_id for item in after.records}
        assert before_ids.isdisjoint(item.record_id for item in appended)
        assert {item.record_id for item in appended} <= after_ids
        assert before.report_source_cut_commitment != after.report_source_cut_commitment
