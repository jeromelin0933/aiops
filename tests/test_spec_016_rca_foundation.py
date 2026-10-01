"""Narrow SPEC-016 E1 / section 5.1 continuation foundation evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from runtime_orchestration.rca_continuation import (
    CaptureCommandBasis, ContradictoryRcaContinuationError, FollowUpRequirement,
    RcaContinuation, RcaContinuationConcurrencyError, RcaRepairRequiredError, SqliteRcaContinuationStore,
    rca_child_operation_id, rca_root_id,
)
from runtime_orchestration import SqliteRuntimeWorkStore


NOW = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)


def _basis(incident="INC-1", snapshot=NOW):
    return CaptureCommandBasis(incident, rca_root_id(incident), "INITIAL", snapshot,
        "SPEC-013/v1", "canon/v1", "policy/v1", "bounds/v1", ("collection:logs", "collection:metrics"))


def _record(incident="INC-1"):
    return RcaContinuation(
        rca_root_id(incident), "rtw-reference-1", incident, "CAPTURE", "FREEZE_CAPTURE_BASIS", 3,
        NOW, NOW, NOW, unresolved_frontier=(FollowUpRequirement("MATERIAL_EVIDENCE", "revision:7"),),
    )


def test_root_and_child_identities_are_stable_domain_separated_and_exclude_runtime_inputs():
    root = rca_root_id("INC-1")
    assert root == rca_root_id("INC-1")
    assert root != rca_root_id("INC-2")
    identities = {rca_child_operation_id(root, "subject", purpose) for purpose in (
        "EVIDENCE_CAPTURE", "KNOWLEDGE_RETRIEVAL", "D_EXECUTION", "ARTIFACT_COMMIT", "PUBLICATION",
    )}
    assert len(identities) == 5
    # The API admits no wall-clock, pid, retry, or worker argument as identity input.
    assert rca_child_operation_id(root, "subject", "D_EXECUTION") == rca_child_operation_id(root, "subject", "D_EXECUTION")


def test_capture_basis_is_durable_and_same_basis_reuses_original_snapshot_and_id(tmp_path):
    path = tmp_path / "rca-d2.sqlite3"
    store = SqliteRcaContinuationStore(path)
    created = store.create(_record())
    with pytest.raises(RcaRepairRequiredError, match="REPAIR_REQUIRED"):
        store.require_capture_command_basis(created.root_id)
    frozen = store.freeze_capture_basis(created.root_id, _basis(), observed_at=NOW + timedelta(seconds=1))
    capture_id = frozen.capture_operation_id
    store.close()
    reopened = SqliteRcaContinuationStore(path)
    restored = reopened.get(created.root_id)
    assert restored is not None
    assert restored.capture_basis.snapshot_at == NOW
    assert restored.capture_operation_id == capture_id
    assert reopened.freeze_capture_basis(restored.root_id, _basis(), observed_at=NOW + timedelta(days=1)).capture_operation_id == capture_id


def test_changed_or_incomplete_capture_basis_fails_closed(tmp_path):
    store = SqliteRcaContinuationStore(tmp_path / "rca-d2.sqlite3")
    created = store.create(_record())
    store.freeze_capture_basis(created.root_id, _basis(), observed_at=NOW)
    with pytest.raises(ContradictoryRcaContinuationError):
        store.freeze_capture_basis(created.root_id, _basis(snapshot=NOW + timedelta(seconds=1)), observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="collection_boundary"):
        CaptureCommandBasis("INC-1", rca_root_id("INC-1"), "INITIAL", NOW,
                            "SPEC-013/v1", "canon/v1", "policy/v1", "bounds/v1", ())


def test_rca_d2_cas_frontier_round_trip_and_concurrent_update_fail_closed(tmp_path):
    path = tmp_path / "rca-d2.sqlite3"
    store = SqliteRcaContinuationStore(path)
    created = store.create(_record())
    changed = store.update(replace(created, unresolved_frontier=(
        FollowUpRequirement("MATERIAL_EVIDENCE", "revision:7"),
        FollowUpRequirement("POST_CONTEXT", "boundary:8"),
        FollowUpRequirement("STALE_REFRESH", "version:3"),
    ), updated_at=NOW + timedelta(seconds=1), observed_at=NOW + timedelta(seconds=1)), expected_revision=created.revision)
    assert {item.requirement_type for item in changed.unresolved_frontier} == {"MATERIAL_EVIDENCE", "POST_CONTEXT", "STALE_REFRESH"}
    with pytest.raises(RcaContinuationConcurrencyError):
        store.update(replace(created, updated_at=NOW + timedelta(seconds=2), observed_at=NOW + timedelta(seconds=2)), expected_revision=created.revision)
    store.close()
    assert len(SqliteRcaContinuationStore(path).enumerate_all().records[0].unresolved_frontier) == 3


def test_generic_update_cannot_rewrite_frozen_basis_and_cannot_rewrite_set_operation_identity(tmp_path):
    store = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    created = store.create(_record())
    frozen = store.freeze_capture_basis(created.root_id, _basis(), observed_at=NOW)
    changed_at = NOW + timedelta(seconds=1)
    for field, value, replacement in (
        ("retrieval_operation_id", rca_child_operation_id(frozen.root_id, "subject", "KNOWLEDGE_RETRIEVAL"), rca_child_operation_id(frozen.root_id, "other", "KNOWLEDGE_RETRIEVAL")),
        ("execution_operation_id", rca_child_operation_id(frozen.root_id, "subject", "D_EXECUTION"), rca_child_operation_id(frozen.root_id, "other", "D_EXECUTION")),
        ("artifact_commit_operation_id", rca_child_operation_id(frozen.root_id, "subject", "ARTIFACT_COMMIT"), rca_child_operation_id(frozen.root_id, "other", "ARTIFACT_COMMIT")),
        ("publication_operation_id", rca_child_operation_id(frozen.root_id, "subject", "PUBLICATION"), rca_child_operation_id(frozen.root_id, "other", "PUBLICATION")),
        ("follow_up_root_id", "followup:one", "followup:two"),
    ):
        # Later S2 stages may retain an identity once; they must never replace it.
        advanced = store.update(replace(frozen, **{field: value}, updated_at=changed_at,
                                        observed_at=changed_at), expected_revision=frozen.revision)
        with pytest.raises(ContradictoryRcaContinuationError, match="REPAIR_REQUIRED"):
            store.update(replace(advanced, **{field: replacement}, updated_at=changed_at,
                                 observed_at=changed_at), expected_revision=advanced.revision)
        frozen = advanced
    changed_basis = _basis(snapshot=NOW + timedelta(seconds=1))
    with pytest.raises(ContradictoryRcaContinuationError, match="REPAIR_REQUIRED"):
        store.update(replace(frozen, capture_basis=changed_basis,
                             capture_operation_id=changed_basis.capture_operation_id,
                             updated_at=changed_at, observed_at=changed_at),
                     expected_revision=frozen.revision)
    with pytest.raises(ValueError):
        replace(frozen, capture_operation_id="other-capture")
    direct = SqliteRcaContinuationStore(tmp_path / "direct.sqlite3")
    initial = direct.create(_record())
    with pytest.raises(ContradictoryRcaContinuationError, match="only be set by freeze"):
        direct.update(replace(initial, capture_basis=_basis(),
                              capture_operation_id=_basis().capture_operation_id),
                      expected_revision=initial.revision)


def test_extension_uses_existing_runtime_d2_without_reinterpreting_runtime_rows(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    rca = SqliteRcaContinuationStore(path)
    rca.create(_record())
    rca.close()
    # The existing D2 adapter remains the owner/decoder of Runtime Work rows.
    runtime = SqliteRuntimeWorkStore(path)
    assert runtime.enumerate_all().records == ()
    assert runtime.enumerate_all().isolated_corruptions == ()
    runtime.close()


def test_malformed_json_and_row_payload_key_contradiction_are_isolated_fail_closed(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRcaContinuationStore(path)
    created = store.create(_record())
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE rca_runtime_continuations SET record_json=? WHERE root_id=?", ("{bad", created.root_id))
    connection.commit()
    connection.close()
    reopened = SqliteRcaContinuationStore(path)
    with pytest.raises(Exception, match="REPAIR_REQUIRED"):
        reopened.get(created.root_id)
    assert reopened.enumerate_all().isolated_corruptions[0].record_key == created.root_id
    reopened.close()

    store = SqliteRcaContinuationStore(tmp_path / "other.sqlite3")
    created = store.create(_record())
    store.close()
    connection = sqlite3.connect(tmp_path / "other.sqlite3")
    payload = connection.execute("SELECT record_json FROM rca_runtime_continuations").fetchone()[0]
    contradictory = payload.replace(created.root_id, rca_root_id("INC-2")).replace("INC-1", "INC-2")
    connection.execute("UPDATE rca_runtime_continuations SET record_json=?", (contradictory,))
    connection.commit()
    connection.close()
    with pytest.raises(Exception, match="row key contradicts"):
        SqliteRcaContinuationStore(tmp_path / "other.sqlite3").get(created.root_id)


def test_schema_shape_and_stale_reopened_connection_fail_closed(tmp_path):
    malformed = tmp_path / "malformed.sqlite3"
    SqliteRuntimeWorkStore(malformed).close()
    connection = sqlite3.connect(malformed)
    connection.execute("CREATE TABLE rca_continuation_store_metadata(singleton INTEGER PRIMARY KEY, schema_version INTEGER)")
    connection.execute("CREATE TABLE rca_runtime_continuations(root_id TEXT PRIMARY KEY, record_json TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(Exception, match="schema"):
        SqliteRcaContinuationStore(malformed)

    path = tmp_path / "cas.sqlite3"
    first = SqliteRcaContinuationStore(path)
    created = first.create(_record())
    second = SqliteRcaContinuationStore(path)
    advanced = first.update(replace(created, updated_at=NOW + timedelta(seconds=1),
                                    observed_at=NOW + timedelta(seconds=1)), expected_revision=created.revision)
    with pytest.raises(RcaContinuationConcurrencyError):
        second.update(replace(created, updated_at=NOW + timedelta(seconds=2),
                              observed_at=NOW + timedelta(seconds=2)), expected_revision=created.revision)
    assert advanced.revision == 2


@pytest.mark.parametrize("unsafe", (
    "scenario_id=S1", "ground_truth=database", "expected_answer=database",
    "raw_provider=response body", "exception_body=stack trace", "api_key=secret",
))
def test_d2_rejects_ground_truth_secrets_and_raw_provider_or_exception_content(unsafe):
    with pytest.raises(RcaRepairRequiredError, match="REPAIR_REQUIRED"):
        RcaContinuation(rca_root_id("INC-1"), "rtw-reference-1", "INC-1", "CAPTURE",
                        "FREEZE_CAPTURE_BASIS", 3, NOW, NOW, NOW,
                        observation_code=unsafe)


def test_allow_list_codes_and_references_round_trip_without_payload_storage(tmp_path):
    store = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    created = store.create(RcaContinuation(
        rca_root_id("INC-1"), "rtw-reference-1", "INC-1", "CAPTURE",
        "FREEZE_CAPTURE_BASIS", 3, NOW, NOW, NOW,
        source_domain="CANDIDATE_B", source_disposition="PENDING",
        observation_code="OBSERVED", unresolved_frontier=(
            FollowUpRequirement("MATERIAL_EVIDENCE", "revision:7"),
            FollowUpRequirement("POST_CONTEXT", "boundary:8"),
            FollowUpRequirement("STALE_REFRESH", "version:3"),
        ),
    ))
    store.close()
    restored = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3").get(created.root_id)
    assert restored.source_disposition == "PENDING"
    assert restored.observation_code == "OBSERVED"
    assert {item.reference for item in restored.unresolved_frontier} == {"revision:7", "boundary:8", "version:3"}


@pytest.mark.parametrize("requirement, reference", (
    ("MATERIAL_EVIDENCE", "arbitrary response body"),
    ("MATERIAL_EVIDENCE", '{"result":"database"}'),
    ("POST_CONTEXT", "revision:7"),
    ("UNKNOWN", "revision:7"),
))
def test_frontier_requires_typed_reference_only(requirement, reference):
    with pytest.raises((ValueError, RcaRepairRequiredError)):
        FollowUpRequirement(requirement, reference)


@pytest.mark.parametrize("field, value", (
    ("source_disposition", "token=abc"), ("source_disposition", "password=abc"),
    ("source_disposition", "response body"), ("source_disposition", "Traceback details"),
    ("source_disposition", '{"result":"database"}'), ("source_disposition", "scenario_id=S1"),
    ("observation_code", "expected answer"), ("observation_code", "unknown arbitrary text"),
))
def test_source_fields_accept_only_finite_non_secret_codes(field, value):
    with pytest.raises(RcaRepairRequiredError, match="REPAIR_REQUIRED"):
        RcaContinuation(rca_root_id("INC-1"), "rtw-reference-1", "INC-1", "CAPTURE",
                        "FREEZE_CAPTURE_BASIS", 3, NOW, NOW, NOW, **{field: value})


def test_every_continuation_read_runs_sqlite_quick_check(tmp_path):
    store = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    created = store.create(_record())
    actual, statements = store._connection, []  # S1 durability guard is intentionally observable here.

    class Spy:
        def execute(self, statement, *args):
            statements.append(statement)
            return actual.execute(statement, *args)

        def commit(self):
            return actual.commit()

        def rollback(self):
            return actual.rollback()

    store._connection = Spy()
    assert store.get(created.root_id) is not None
    store._connection = actual
    assert "PRAGMA quick_check" in statements
