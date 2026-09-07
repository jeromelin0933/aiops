"""Test-only durable-state seeding for recovery and integrity scenarios.

This deliberately writes serialized records directly.  It is not a production
state-store API and therefore cannot grant normal mutation authority.
"""

from src.alert_correlation.state.contracts import ActivePendingRecord, BlockedCorrelationRecord
from src.alert_correlation.state.sqlite_store import _encode_blocked, _encode_pending, _json


def seed_pending(store, record: ActivePendingRecord) -> None:
    """Seed a pre-existing persisted Pending record for test setup only."""
    store._connection.execute(
        "INSERT INTO correlation_state_pending(event_id, payload) VALUES (?, ?)",
        (record.event_id, _json(_encode_pending(record))),
    )
    store._connection.commit()


def seed_block(store, record: BlockedCorrelationRecord) -> None:
    """Seed a pre-existing persisted Blocked record for test setup only."""
    store._connection.execute(
        "INSERT INTO correlation_state_blocked(event_id, payload) VALUES (?, ?)",
        (record.event_id, _json(_encode_blocked(record))),
    )
    store._connection.commit()
