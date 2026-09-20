from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from rca_persistence import RcaDomainError, RcaErrorCode, SqliteRcaStore

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
