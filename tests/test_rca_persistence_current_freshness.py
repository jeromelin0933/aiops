from dataclasses import replace

import pytest

from rca_persistence import (
    CurrentFreshness,
    CurrentRca,
    PublicationDisposition,
    PublicationResult,
    PublicationTargetIdentity,
    RcaDomainError,
    RcaErrorCode,
    SqliteRcaStore,
    VersionRole,
)

from test_rca_persistence_version_artifact import NOW, artifact, commit, seed, target


def _result(target_identity, disposition, current=None):
    return PublicationResult(target_identity, disposition, NOW, current)


def test_only_applied_authority_promotes_and_replacement_preserves_history(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        commit(store)
        applied = _result(target(), PublicationDisposition.APPLIED, "VER-1")
        assert store.complete_authorized_publication(applied) == applied
        assert store.complete_authorized_publication(applied) == applied

        seed(store, "ATT-2")
        second_target = PublicationTargetIdentity("PUB-2", "AGG-1", "INC-1", "VER-2", "VER-1")
        store.commit_validated_artifact("OP-COMMIT-2", "ATT-2", artifact(), second_target, NOW)
        second = _result(second_target, PublicationDisposition.APPLIED, "VER-2")
        store.complete_authorized_publication(second)

        current = store.get_current("AGG-1")
        assert current is not None
        assert current.version.version_id == "VER-2"
        assert current.current.freshness is CurrentFreshness.FRESH
        history = store.get_version_history("AGG-1")
        assert tuple(item.role for item in history) == (
            VersionRole.HISTORICAL,
            VersionRole.CURRENT,
        )


@pytest.mark.parametrize(
    "disposition",
    [
        PublicationDisposition.PRECONDITION_SUPERSEDED,
        PublicationDisposition.TARGET_ALREADY_CURRENT_CONFLICT,
        PublicationDisposition.REPAIR_REQUIRED,
    ],
)
def test_non_success_result_never_guesses_or_overwrites_current(tmp_path, disposition) -> None:
    with SqliteRcaStore(tmp_path / f"{disposition.value}.db") as store:
        seed(store)
        commit(store)
        store.complete_authorized_publication(
            _result(target(), PublicationDisposition.APPLIED, "VER-1")
        )
        seed(store, "ATT-2")
        second_target = PublicationTargetIdentity("PUB-2", "AGG-1", "INC-1", "VER-2", "VER-X")
        store.commit_validated_artifact("OP-COMMIT-2", "ATT-2", artifact(), second_target, NOW)
        store.complete_authorized_publication(_result(second_target, disposition, "VER-1"))
        assert store.get_current("AGG-1").version.version_id == "VER-1"
        assert store.get_version("VER-2").role is VersionRole.COMMITTED_UNPUBLISHED


def test_authorized_material_evidence_marks_stale_and_failed_refresh_keeps_lkg(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        commit(store)
        store.complete_authorized_publication(
            _result(target(), PublicationDisposition.APPLIED, "VER-1")
        )
        stale = CurrentRca("AGG-1", "VER-1", CurrentFreshness.STALE, "ER-MATERIAL-2")
        assert store.apply_authorized_freshness(stale) == stale
        assert store.apply_authorized_freshness(stale) == stale

        seed(store, "ATT-2")
        failed_target = PublicationTargetIdentity("PUB-2", "AGG-1", "INC-1", "VER-2", "VER-1")
        store.commit_validated_artifact("OP-COMMIT-2", "ATT-2", artifact(), failed_target, NOW)
        store.complete_authorized_publication(
            _result(failed_target, PublicationDisposition.REPAIR_REQUIRED, "VER-1")
        )
        readable = store.get_current("AGG-1")
        assert readable is not None
        assert readable.current == stale
        assert readable.artifact == artifact()

        with pytest.raises(RcaDomainError) as raised:
            store.apply_authorized_freshness(replace(stale, material_evidence_revision_basis="ER-OLDER"))
        assert raised.value.code is RcaErrorCode.SEMANTIC_CONFLICT


def test_revision_difference_alone_does_not_infer_materiality(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        commit(store)
        store.complete_authorized_publication(
            _result(target(), PublicationDisposition.APPLIED, "VER-1")
        )
        assert store.get_current("AGG-1").current.freshness is CurrentFreshness.FRESH


def test_stale_basis_and_multiple_transitions_survive_replacement_and_restart(tmp_path) -> None:
    database = tmp_path / "lineage.db"
    first_stale = CurrentRca(
        "AGG-1", "VER-1", CurrentFreshness.STALE, "ER-MATERIAL-2"
    )
    second_stale = CurrentRca(
        "AGG-1", "VER-2", CurrentFreshness.STALE, "ER-MATERIAL-3"
    )
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
        store.complete_authorized_publication(
            _result(target(), PublicationDisposition.APPLIED, "VER-1")
        )
        store.apply_authorized_freshness(first_stale)

        seed(store, "ATT-2")
        second_target = PublicationTargetIdentity(
            "PUB-2", "AGG-1", "INC-1", "VER-2", "VER-1"
        )
        store.commit_validated_artifact(
            "OP-COMMIT-2", "ATT-2", artifact(), second_target, NOW
        )
        store.complete_authorized_publication(
            _result(second_target, PublicationDisposition.APPLIED, "VER-2")
        )
        store.apply_authorized_freshness(second_stale)
        expected = (
            CurrentRca("AGG-1", "VER-1", CurrentFreshness.FRESH, "ER-1"),
            first_stale,
            CurrentRca("AGG-1", "VER-2", CurrentFreshness.FRESH, "ER-1"),
            second_stale,
        )
        assert store.get_freshness_lineage("AGG-1") == expected
        assert store.get_version("VER-1").role is VersionRole.HISTORICAL

    with SqliteRcaStore(database) as reopened:
        assert reopened.get_freshness_lineage("AGG-1") == expected
        assert reopened.get_current("AGG-1").current == second_stale
