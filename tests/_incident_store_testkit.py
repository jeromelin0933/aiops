"""Controlled Phase 2 seeding through SPEC-008 package-private primitives."""

from __future__ import annotations

from collections.abc import Iterable

from src.incident_management import (
    IncidentOperationReceipt,
    IncidentRecord,
    SqliteIncidentStore,
)


def seed_complete_incident(
    store: SqliteIncidentStore,
    record: IncidentRecord,
    receipts: Iterable[IncidentOperationReceipt],
) -> None:
    receipt_by_event = {receipt.result.event_id: receipt for receipt in receipts}
    with store._transaction() as transaction:
        transaction._insert_incident_state(record)
        for ordinal, event_id in enumerate(record.event_ids):
            transaction._insert_event_reference(record.incident_id, event_id, ordinal)
            transaction._insert_operation_receipt(receipt_by_event[event_id])
        for entry in record.audit_trail:
            transaction._insert_audit_entry(entry)


def execute_controlled_sql(
    store: SqliteIncidentStore,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> None:
    with store._transaction() as transaction:
        transaction._connection.execute(statement, parameters)


def seed_incomplete_incident_state(
    store: SqliteIncidentStore,
    record: IncidentRecord,
) -> None:
    with store._transaction() as transaction:
        transaction._insert_incident_state(record)


def seed_then_crash(store: SqliteIncidentStore, record: IncidentRecord) -> None:
    with store._transaction() as transaction:
        transaction._insert_incident_state(record)
        raise RuntimeError("controlled pre-commit crash")
