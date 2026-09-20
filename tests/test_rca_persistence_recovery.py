import pytest

from rca_persistence import (
    AdmittedRetryDisposition,
    CurrentFreshness,
    CurrentRca,
    LogicalTryIdentity,
    LogicalTryOutcome,
    LogicalTryResultKind,
    PublicationDisposition,
    PublicationResult,
    PublicationTargetIdentity,
    RecoveryCandidateKind,
    SqliteRcaStore,
)

from test_rca_persistence_replay_recovery import _create_database
from test_rca_persistence_version_artifact import NOW, artifact, commit, seed, target


def test_recovery_enumeration_is_complete_deterministic_and_restart_stable(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
        assert [item.kind for item in store.enumerate_recovery_candidates()] == [
            RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION
        ]
        store.complete_authorized_publication(
            PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1")
        )
        store.apply_authorized_freshness(
            CurrentRca("AGG-1", "VER-1", CurrentFreshness.STALE, "ER-2")
        )
        expected = store.enumerate_recovery_candidates()
        assert [item.kind for item in expected] == [RecoveryCandidateKind.STALE_CURRENT]

    for _ in range(3):
        with SqliteRcaStore(database) as reopened:
            assert reopened.enumerate_recovery_candidates() == expected
            assert reopened.get_current("AGG-1").version.version_id == "VER-1"
            assert reopened.complete_authorized_publication(
                PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1")
            ).resulting_current_version_id == "VER-1"


def test_retryable_terminal_try_is_enumerated_without_allocating_identity(tmp_path) -> None:
    database = tmp_path / "retry.db"
    _create_database(database)
    outcome = LogicalTryOutcome(
        LogicalTryIdentity("ATT-1", 1),
        LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.RETRYABLE,
        NOW,
        failure_code="TIMEOUT",
    )
    with SqliteRcaStore(database) as store:
        store.record_try_outcome("OP-TRY-1", outcome)
        first = store.enumerate_recovery_candidates()
        second = store.enumerate_recovery_candidates()
        assert first == second
        assert first[0].kind is RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION
        assert first[0].attempt_id == "ATT-1"


@pytest.mark.parametrize(
    "disposition",
    [
        PublicationDisposition.PRECONDITION_SUPERSEDED,
        PublicationDisposition.TARGET_ALREADY_CURRENT_CONFLICT,
        PublicationDisposition.REPAIR_REQUIRED,
    ],
)
def test_non_success_publication_remains_unresolved_recovery_evidence(
    tmp_path, disposition
) -> None:
    database = tmp_path / f"{disposition.value}.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
        store.complete_authorized_publication(
            PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1")
        )
        seed(store, "ATT-2")
        unresolved_target = PublicationTargetIdentity(
            "PUB-2", "AGG-1", "INC-1", "VER-2", "VER-X"
        )
        store.commit_validated_artifact(
            "OP-COMMIT-2", "ATT-2", artifact(), unresolved_target, NOW
        )
        store.complete_authorized_publication(
            PublicationResult(unresolved_target, disposition, NOW, "VER-1")
        )
        expected = store.enumerate_recovery_candidates()
        assert len(expected) == 1
        assert expected[0].kind is RecoveryCandidateKind.UNRESOLVED_PUBLICATION
        assert expected[0].version_id == "VER-2"
        assert expected[0].publication_operation_id == "PUB-2"
        assert store.enumerate_recovery_candidates() == expected

    with SqliteRcaStore(database) as reopened:
        assert reopened.enumerate_recovery_candidates() == expected
