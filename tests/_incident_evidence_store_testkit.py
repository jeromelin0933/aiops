"""Fixtures and controlled corruption helpers for Candidate-B store tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from incident_evidence import (
    BoundsOmissionFacts,
    CaptureCommand,
    CaptureSuccess,
    CollectionProvenance,
    EvidenceCompleteness,
    EvidenceRevision,
    EvidenceSnapshot,
    EvidenceSource,
    LogicalWindow,
    QueryProvenance,
    SelectorFact,
    SqliteEvidenceStore,
    SourceStatus,
    canonical_json,
)


def command(operation_id: str = "capture-1", *, incident_id: str = "incident-1") -> CaptureCommand:
    return CaptureCommand(
        operation_id,
        incident_id,
        datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
        "capture-v1",
        "canonical-v1",
        "sources-v1",
        "bounds-v1",
        "config-v1",
    )


def success(
    operation_id: str = "capture-1",
    *,
    incident_id: str = "incident-1",
    evidence: str = "same evidence",
    completeness: EvidenceCompleteness = EvidenceCompleteness.FULL,
    loki_bounds: BoundsOmissionFacts | None = None,
) -> CaptureSuccess:
    cmd = command(operation_id, incident_id=incident_id)
    window = LogicalWindow(
        datetime(2026, 9, 21, 11, 58, tzinfo=timezone.utc),
        datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
    )
    source_statuses = (
        (EvidenceSource.LOKI, SourceStatus.AVAILABLE),
        (EvidenceSource.PROMETHEUS, SourceStatus.EMPTY),
    )
    loki_bounds = loki_bounds or bounds_facts(
        observed_candidate_count=1, included_count=1, omitted_count=0
    )
    prometheus_bounds = bounds_facts(
        observed_candidate_count=0, included_count=0, omitted_count=0
    )
    source_bounds = {
        "LOKI": loki_bounds,
        "PROMETHEUS": prometheus_bounds,
    }
    windows = {"LOKI": window, "PROMETHEUS": window}
    provenance = {}
    for source, status in source_statuses:
        selector = SelectorFact(
            source, "service_name", "payments", "payments", "selectors-v1"
        )
        query = QueryProvenance(
            source,
            f"{source.value.lower()}-query-v1",
            "query-v1",
            window,
            (selector,),
            f"{source.value.lower()}-adapter-v1",
            "bounds-v1",
            "request-budget-v1",
        )
        collection = CollectionProvenance(
            source,
            f"{source.value.lower()}-adapter-v1",
            window,
            1,
            True,
            status,
        )
        provenance[source.value] = {
            "query": query,
            "collection": collection,
            "validation_findings": [],
            "safe_failure_kind": None,
        }
    normalized_evidence = {
        "LOKI": [
            {"record_id": f"log-{index}", "message": evidence}
            for index in range(loki_bounds.included_count)
        ],
        "PROMETHEUS": [],
    }
    canonical_bounds = json.loads(canonical_json(source_bounds))
    canonical_windows = json.loads(canonical_json(windows))
    semantic = {
        "incident_context": {"severity": "critical"},
        "events": [{"event_id": "event-1", "value": evidence}],
        "normalized_evidence": normalized_evidence,
        "source_statuses": {"LOKI": "AVAILABLE", "PROMETHEUS": "EMPTY"},
        "completeness": completeness.value,
        "omission": canonical_bounds,
        "collection_boundaries": canonical_windows,
    }
    revision = EvidenceRevision.from_content(
        incident_id=incident_id,
        canonicalization_version=cmd.canonicalization_version,
        semantic_content=semantic,
    )
    snapshot = EvidenceSnapshot.from_content(
        cmd,
        revision_id=revision.revision_id,
        completeness=completeness,
        source_statuses=source_statuses,
        snapshot_content={
            "incident_projection": {"incident_id": incident_id, "severity": "critical"},
            "event_projections": [{"event_id": "event-1"}],
            "episode": {"start": "2026-09-21T11:58:00Z", "end": "2026-09-21T12:00:00Z"},
            "windows": windows,
            "semantic_evidence": semantic,
            "provenance": provenance,
            "bounds": source_bounds,
            "post_context": {
                "episode_end": "2026-09-21T12:00:00Z",
                "default_boundary": "2026-09-21T12:02:00Z",
                "effective_window_ends": {"LOKI": "2026-09-21T12:00:00Z", "PROMETHEUS": "2026-09-21T12:00:00Z"},
                "snapshot_at": "2026-09-21T12:00:00Z",
                "reached_upper_boundary": {"LOKI": False, "PROMETHEUS": False},
            },
        },
    )
    return CaptureSuccess(cmd, snapshot, revision)


def bounds_facts(**overrides: object) -> BoundsOmissionFacts:
    values = {
        "bounds_policy_version": "bounds-v1",
        "observed_candidate_count": 1,
        "included_count": 1,
        "omitted_count": 0,
        "omission_reason": None,
        "sampling_applied": False,
        "sampling_policy_identity": None,
        "aggregation_applied": False,
        "aggregation_rule_identity": None,
        "aggregation_lossy": False,
        "dedup_applied": False,
        "dedup_input_count": None,
        "dedup_output_count": None,
        "truncation_applied": False,
        "truncation_reason": None,
        "completeness_impact": EvidenceCompleteness.FULL,
    }
    values.update(overrides)
    return BoundsOmissionFacts(**values)


def execute_sql(path: Path, statement: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def create_old_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE evidence_store_metadata(metadata_key TEXT PRIMARY KEY, metadata_value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO evidence_store_metadata VALUES (?, ?)",
            (("store_domain", "SPEC-013-CANDIDATE-B-EVIDENCE"), ("schema_version", "0")),
        )
        connection.commit()
    finally:
        connection.close()


def create_weakened_check_schema(path: Path) -> None:
    """Create same-shape/schema-version DDL with one weakened CHECK expression."""
    _create_schema_variant(
        path,
        lambda statement: statement.replace(
            "completeness IN ('FULL','DEGRADED')",
            "completeness IN ('FULL','DEGRADED','UNSAFE')",
        ),
    )


def create_check_literal_case_schema(path: Path) -> None:
    _create_schema_variant(
        path,
        lambda statement: statement.replace("'FULL'", "'full'"),
    )


def create_check_literal_whitespace_schema(path: Path) -> None:
    _create_schema_variant(
        path,
        lambda statement: statement.replace("'FULL'", "'FU LL'"),
    )


def create_schema_with_trigger(path: Path) -> None:
    _create_schema_variant(
        path,
        extra_statement="""CREATE TRIGGER unexpected_capture_trigger
            BEFORE INSERT ON capture_results
            BEGIN SELECT RAISE(ABORT, 'unexpected trigger'); END""",
    )


def create_schema_with_view(path: Path) -> None:
    _create_schema_variant(
        path,
        extra_statement="CREATE VIEW unexpected_capture_view AS SELECT * FROM capture_results",
    )


def create_schema_with_sqlitex_trigger(path: Path) -> None:
    _create_schema_variant(
        path,
        extra_statement="""CREATE TRIGGER sqliteXtrigger
            BEFORE INSERT ON capture_results
            BEGIN SELECT RAISE(ABORT, 'unexpected trigger'); END""",
    )


def create_schema_with_sqlitex_view(path: Path) -> None:
    _create_schema_variant(
        path,
        extra_statement="CREATE VIEW sqliteXview AS SELECT * FROM capture_results",
    )


def create_schema_with_sqlitex_table(path: Path) -> None:
    _create_schema_variant(path, extra_statement="CREATE TABLE sqliteXtable(value TEXT)")


def _create_schema_variant(
    path: Path,
    transform=lambda statement: statement,
    *,
    extra_statement: str | None = None,
) -> None:
    connection = sqlite3.connect(path)
    try:
        statements = [transform(statement) for statement in SqliteEvidenceStore._schema_statements()]
        for statement in statements:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO evidence_store_metadata VALUES (?, ?)",
            (("store_domain", "SPEC-013-CANDIDATE-B-EVIDENCE"), ("schema_version", "1")),
        )
        if extra_statement is not None:
            connection.execute(extra_statement)
        connection.commit()
    finally:
        connection.close()
