from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
from threading import Barrier, Event

from alert_correlation.contracts import AnchorTransition, CorrelationFamily, DecisionReasonCode, DecisionType
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY
from alert_correlation.state.contracts import CorrelationMutationIntent, TerminalOutcome
from shadow_management.contracts import ShadowDomainError, ShadowDomainErrorCode
from shadow_management.manager import ShadowManager
from shadow_management.sqlite_store import SqliteShadowStore


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


class _NoIncidentOwner:
    def event_has_incident_owner(self, _event_id: str) -> bool:
        return False


def _intent(operation_id: str) -> CorrelationMutationIntent:
    return CorrelationMutationIntent(operation_id, "EVT-1", TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, NOW)


def _worker(database, intent: CorrelationMutationIntent, barrier: Barrier):
    store = SqliteShadowStore(database)
    manager = ShadowManager(store, DEFAULT_POLICY_REGISTRY, _NoIncidentOwner())
    try:
        barrier.wait()
        return manager.route_shadow(intent, {"event_id": "EVT-1", "event_type": "general_log_anomaly"}, NOW)
    except ShadowDomainError as error:
        return error
    finally:
        store.close()


def _race(database, first: CorrelationMutationIntent, second: CorrelationMutationIntent):
    SqliteShadowStore(database).close()
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        return tuple(pool.map(lambda intent: _worker(database, intent, barrier), (first, second)))


def test_same_event_different_operations_have_one_owner_and_loser_fails_closed(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    results = _race(database, _intent("OP-1"), _intent("OP-2"))
    successes = [result for result in results if not isinstance(result, ShadowDomainError)]
    failures = [result for result in results if isinstance(result, ShadowDomainError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is ShadowDomainErrorCode.SHADOW_EVENT_OWNERSHIP_CONFLICT
    with SqliteShadowStore(database) as store:
        assert store.get_shadow_by_event_id("EVT-1") is not None
        loser = "OP-2" if successes[0].operation_id == "OP-1" else "OP-1"
        manager = ShadowManager(store, DEFAULT_POLICY_REGISTRY, _NoIncidentOwner())
        try:
            manager.route_shadow(_intent(loser), {"event_id": "EVT-1", "event_type": "general_log_anomaly"}, NOW)
        except ShadowDomainError as error:
            assert error.code is ShadowDomainErrorCode.SHADOW_EVENT_OWNERSHIP_CONFLICT
        else:  # pragma: no cover - local unique ownership must reject this path.
            raise AssertionError("different operation unexpectedly acquired same Event")


def test_same_operation_race_has_one_receipt_and_stable_equivalent_replay(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    results = _race(database, _intent("OP-1"), _intent("OP-1"))
    successes = [result for result in results if not isinstance(result, ShadowDomainError)]
    assert len(successes) >= 1
    with SqliteShadowStore(database) as store:
        manager = ShadowManager(store, DEFAULT_POLICY_REGISTRY, _NoIncidentOwner())
        replay = manager.route_shadow(_intent("OP-1"), {"event_id": "EVT-1", "event_type": "general_log_anomaly"}, NOW)
        assert replay == store.get_operation_result("OP-1")
        assert store.get_shadow_by_event_id("EVT-1") is not None


def test_busy_lock_is_retryable_only_before_local_mutation_starts(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    SqliteShadowStore(database).close()
    worker_ready = Event()
    release_worker = Event()

    def locked_worker():
        store = SqliteShadowStore(database, busy_timeout_seconds=0)
        manager = ShadowManager(store, DEFAULT_POLICY_REGISTRY, _NoIncidentOwner())
        try:
            worker_ready.set()
            assert release_worker.wait(timeout=5)
            return manager.route_shadow(
                _intent("OP-BUSY"),
                {"event_id": "EVT-1", "event_type": "general_log_anomaly"},
                NOW,
            )
        except ShadowDomainError as error:
            return error
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(locked_worker)
        assert worker_ready.wait(timeout=5)
        with sqlite3.connect(database, isolation_level=None) as lock_connection:
            lock_connection.execute("BEGIN IMMEDIATE")
            release_worker.set()
            result = future.result(timeout=5)
            lock_connection.rollback()

    assert isinstance(result, ShadowDomainError)
    assert result.code is ShadowDomainErrorCode.TRANSIENT_SHADOW_STORE_FAILURE
    assert result.retry_disposition.value == "RETRYABLE"
    with SqliteShadowStore(database) as store:
        assert store.get_shadow_by_event_id("EVT-1") is None
