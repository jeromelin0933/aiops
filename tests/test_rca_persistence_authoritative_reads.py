from rca_persistence import (
    CurrentFreshness,
    CurrentRca,
    PublicationDisposition,
    PublicationResult,
    SqliteRcaStore,
)

from test_rca_persistence_version_artifact import NOW, artifact, commit, seed, target


def test_current_read_is_one_coherent_authoritative_snapshot(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        commit(store)
        result = PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1")
        store.complete_authorized_publication(result)

        read = store.get_current("AGG-1")
        assert read is not None
        assert read.current.current_version_id == read.version.version_id == "VER-1"
        assert read.version.artifact == read.artifact == artifact()
        assert read.attempt_lineage.attempt.lineage.attempt_id == read.version.attempt_id
        assert read.publication_result == result
        assert store.get_publication_result("PUB-1") == result
        assert store.get_freshness_lineage("AGG-1") == (
            CurrentRca("AGG-1", "VER-1", CurrentFreshness.FRESH, "ER-1"),
        )


def test_legitimate_absence_remains_distinct_from_integrity_failure(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        assert store.get_current("AGG-MISSING") is None
        assert store.get_version("VER-MISSING") is None
        assert store.get_publication_result("PUB-MISSING") is None


def test_never_published_aggregate_has_legal_current_absence(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        assert store.get_aggregate("AGG-1") is not None
        assert store.get_current("AGG-1") is None
        assert store.get_freshness_lineage("AGG-1") == ()
