"""Controlled malformed/version state seeding for SPEC-010 SQLite adapter tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def set_schema_version(database: Path, version: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE shadow_store_metadata SET metadata_value = ? WHERE metadata_key = ?",
            (version, "shadow_store_schema_version"),
        )


def insert_malformed_shadow(database: Path, shadow_id: str, event_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO shadow_records(
                   shadow_id, event_id, entered_shadow_at, reason, review_status, policy_id, policy_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (shadow_id, event_id, "not-a-timestamp", "NOT_A_REASON", "UNREVIEWED", "POLICY", "1"),
        )


def remove_receipt_shadow_referent(database: Path, shadow_id: str) -> None:
    """Seed a corrupt local receipt reference without exposing a runtime delete API."""
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM shadow_records WHERE shadow_id = ?", (shadow_id,))


def insert_second_receipt_for_shadow(database: Path, shadow_id: str) -> None:
    with sqlite3.connect(database) as connection:
        payload, entered_at = connection.execute(
            "SELECT semantic_identity, entered_shadow_at FROM operation_receipts LIMIT 1"
        ).fetchone()
        connection.execute(
            """INSERT INTO operation_receipts(operation_id, semantic_identity, shadow_id, entered_shadow_at)
               VALUES (?, ?, ?, ?)""",
            ("OP-CONTRADICTORY", payload.replace("OP-1", "OP-CONTRADICTORY"), shadow_id, entered_at),
        )


def corrupt_receipt_result_timestamp(database: Path, operation_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE operation_receipts SET entered_shadow_at = ? WHERE operation_id = ?",
            ("2026-09-10T00:00:00+00:00", operation_id),
        )


def remove_receipt_for_shadow(database: Path, shadow_id: str) -> None:
    """Seed an isolated missing-receipt contradiction without a runtime API."""
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM operation_receipts WHERE shadow_id = ?", (shadow_id,))


def corrupt_receipt_event_id(database: Path, operation_id: str, event_id: str) -> None:
    """Change only receipt identity data to prove record/receipt cross-checking."""
    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT semantic_identity FROM operation_receipts WHERE operation_id = ?", (operation_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE operation_receipts SET semantic_identity = ? WHERE operation_id = ?",
            (payload.replace('"event_id":"EVT-1"', f'"event_id":"{event_id}"', 1), operation_id),
        )
