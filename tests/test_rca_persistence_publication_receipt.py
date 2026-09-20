import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from rca_persistence import (
    PublicationDisposition,
    RcaDomainError,
    RcaErrorCode,
    RecoveryCandidateKind,
    SqliteRcaStore,
    VersionRole,
)

from test_rca_persistence_version_artifact import NOW, artifact, commit, seed, target


def test_a_side_receipt_is_local_only_and_discoverable(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        version = commit(store)
        receipt = store.get_publication_result("PUB-1")
        assert receipt is not None
        assert receipt.disposition is PublicationDisposition.A_SIDE_COMMITTED
        assert receipt.resulting_current_version_id is None
        assert version.role is VersionRole.COMMITTED_UNPUBLISHED
        candidates = store.enumerate_recovery_candidates()
        assert len(candidates) == 1
        assert candidates[0].kind is RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION
        assert candidates[0].version_id == "VER-1"
        assert candidates[0].publication_operation_id == "PUB-1"
        assert not hasattr(store, "complete_authorized_publication")
        assert not hasattr(store, "apply_authorized_freshness")


def test_same_command_and_response_loss_replay_return_original_identities(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        original = commit(store)
        replay = store.commit_validated_artifact(
            "OP-COMMIT-1", "ATT-1", artifact(), target(), NOW + timedelta(hours=1)
        )
        assert replay == original
    with SqliteRcaStore(database) as reopened:
        assert commit(reopened) == original
        assert reopened.get_publication_result("PUB-1").target == target()
        assert reopened.enumerate_recovery_candidates()[0].version_id == original.version_id


def test_command_or_publication_contradiction_fails_closed(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        commit(store)
        with pytest.raises(RcaDomainError) as command:
            store.commit_validated_artifact(
                "OP-COMMIT-1", "ATT-1", replace(artifact(), summary="different"), target(), NOW
            )
        assert command.value.code is RcaErrorCode.RECEIPT_REPLAY_CONFLICT
        with pytest.raises(RcaDomainError) as publication:
            store.commit_validated_artifact(
                "OP-COMMIT-2", "ATT-1", artifact(), target("VER-X", "PUB-1"), NOW
            )
        assert publication.value.code is RcaErrorCode.RECEIPT_REPLAY_CONFLICT
        assert tuple(item.version_id for item in store.get_version_history("AGG-1")) == ("VER-1",)


def test_receipt_corruption_is_not_absence(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
    with sqlite3.connect(database) as connection:
        encoded = connection.execute(
            "SELECT semantic_identity FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'"
        ).fetchone()[0]
        payload = json.loads(encoded)
        payload["publication_target"]["incident_id"] = "INC-X"
        connection.execute(
            "UPDATE rca_operation_receipts SET semantic_identity=? WHERE command_kind='COMMIT_ARTIFACT'",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    assert raised.value.code is not RcaErrorCode.NOT_FOUND


def test_artifact_commit_does_not_mutate_incident_or_current_state(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        before = store.get_aggregate("AGG-1")
        version = commit(store)
        assert store.get_aggregate("AGG-1") == before
        assert version.role is VersionRole.COMMITTED_UNPUBLISHED
        assert "get_current" not in SqliteRcaStore.__dict__
