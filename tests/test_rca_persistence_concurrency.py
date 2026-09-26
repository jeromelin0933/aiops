from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from rca_persistence import (
    CurrentFreshness,
    CurrentRca,
    PublicationDisposition,
    PublicationResult,
    RcaDomainError,
    RcaErrorCode,
    SqliteRcaStore,
)

from test_rca_persistence_version_artifact import NOW, artifact, seed, target


def _run_commit(database, barrier, operation, version, publication, attempt="ATT-1"):
    with SqliteRcaStore(database) as store:
        barrier.wait()
        try:
            return store.commit_validated_artifact(
                operation, attempt, artifact(), target(version, publication), NOW
            )
        except RcaDomainError as exc:
            return exc.code


def test_concurrent_allocations_are_unique_continuous_and_gapless(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        seed(store, "ATT-2")
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_run_commit, database, barrier, "OP-C1", "VER-1", "PUB-1", "ATT-1"),
            executor.submit(_run_commit, database, barrier, "OP-C2", "VER-2", "PUB-2", "ATT-2"),
        ]
        results = [future.result() for future in futures]
    assert sorted(item.version_number for item in results) == [1, 2]
    with SqliteRcaStore(database) as store:
        history = store.get_version_history("AGG-1")
        assert tuple(item.version_number for item in history) == (1, 2)
        assert len({item.version_id for item in history}) == 2


def test_equivalent_operation_race_has_one_version_and_receipt(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: _run_commit(database, barrier, "OP-C1", "VER-1", "PUB-1"),
                range(2),
            )
        )
    assert results[0] == results[1]
    with SqliteRcaStore(database) as store:
        assert len(store.get_version_history("AGG-1")) == 1
        assert store.get_publication_result("PUB-1") is not None


def test_contradictory_publication_operation_race_fails_one_closed(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_run_commit, database, barrier, "OP-C1", "VER-1", "PUB-1"),
            executor.submit(_run_commit, database, barrier, "OP-C2", "VER-2", "PUB-1"),
        ]
        results = [future.result() for future in futures]
    assert sum(hasattr(item, "version_id") for item in results) == 1
    assert results.count(RcaErrorCode.RECEIPT_REPLAY_CONFLICT) == 1
    with SqliteRcaStore(database) as store:
        history = store.get_version_history("AGG-1")
        assert len(history) == 1
        assert history[0].version_number == 1


def test_concurrent_current_promotion_has_at_most_one_authorized_winner(tmp_path) -> None:
    database = tmp_path / "promote.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        seed(store, "ATT-2")
        first = store.commit_validated_artifact(
            "OP-C1", "ATT-1", artifact(), target("VER-1", "PUB-1"), NOW
        )
        second = store.commit_validated_artifact(
            "OP-C2", "ATT-2", artifact(), target("VER-2", "PUB-2"), NOW
        )
    barrier = Barrier(2)

    def promote(version):
        with SqliteRcaStore(database) as store:
            barrier.wait()
            result = PublicationResult(
                target(version.version_id, version.publication_operation_id),
                PublicationDisposition.APPLIED,
                NOW,
                version.version_id,
            )
            try:
                return store.complete_authorized_publication(result)
            except RcaDomainError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(promote, [first, second]))
    assert sum(isinstance(item, PublicationResult) for item in results) == 1
    assert results.count(RcaErrorCode.PUBLICATION_EVIDENCE_INCONSISTENCY) == 1
    with SqliteRcaStore(database) as store:
        assert store.get_current("AGG-1").version.version_id in {"VER-1", "VER-2"}


def test_concurrent_equivalent_completion_converges(tmp_path) -> None:
    database = tmp_path / "equivalent-promotion.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        commit = store.commit_validated_artifact(
            "OP-C1", "ATT-1", artifact(), target("VER-1", "PUB-1"), NOW
        )
    result = PublicationResult(
        target(commit.version_id, commit.publication_operation_id),
        PublicationDisposition.APPLIED,
        NOW,
        commit.version_id,
    )
    barrier = Barrier(2)

    def complete(_):
        with SqliteRcaStore(database) as store:
            barrier.wait()
            return store.complete_authorized_publication(result)

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(complete, range(2))) == [result, result]


def test_concurrent_freshness_updates_cannot_erase_authoritative_basis(tmp_path) -> None:
    database = tmp_path / "freshness.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        version = store.commit_validated_artifact(
            "OP-C1", "ATT-1", artifact(), target("VER-1", "PUB-1"), NOW
        )
        store.complete_authorized_publication(
            PublicationResult(target(), PublicationDisposition.APPLIED, NOW, version.version_id)
        )
    barrier = Barrier(2)

    def stale(basis):
        with SqliteRcaStore(database) as store:
            barrier.wait()
            try:
                return store.apply_authorized_freshness(
                    CurrentRca("AGG-1", "VER-1", CurrentFreshness.STALE, basis)
                )
            except RcaDomainError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(stale, ["ER-MATERIAL-A", "ER-MATERIAL-B"]))
    assert sum(isinstance(item, CurrentRca) for item in results) == 1
    assert results.count(RcaErrorCode.SEMANTIC_CONFLICT) == 1
    with SqliteRcaStore(database) as store:
        assert store.get_current("AGG-1").current.material_evidence_revision_basis in {
            "ER-MATERIAL-A", "ER-MATERIAL-B"
        }
