from datetime import datetime, timedelta, timezone

import pytest

from incident_evidence import (
    CaptureCommand,
    canonical_json,
    canonical_unordered_items,
    capture_command_semantic_identity,
    semantic_identity,
)


def command(snapshot_at, **overrides):
    values = {
        "capture_operation_id": "capture-1",
        "incident_id": "INC-1",
        "snapshot_at": snapshot_at,
        "capture_contract_version": "spec013-v1",
        "canonicalization_version": "canonical-v1",
        "source_policy_version": "sources-v1",
        "bounds_policy_version": "bounds-v1",
        "config_identity": "config-v1",
    }
    values.update(overrides)
    return CaptureCommand(**values)


def test_mapping_and_set_representation_order_do_not_change_identity():
    left = {"outer": {"b": 2, "a": 1}, "tags": {"z", "a"}}
    right = {"tags": {"a", "z"}, "outer": {"a": 1, "b": 2}}

    assert canonical_json(left) == canonical_json(right)
    assert semantic_identity("test-domain", left) == semantic_identity(
        "test-domain", right
    )


def test_physical_row_order_can_be_explicitly_normalized_without_guessing_order():
    first = [{"id": "b", "value": 2}, {"id": "a", "value": 1}]
    second = list(reversed(first))

    assert canonical_unordered_items(first) == canonical_unordered_items(second)
    assert semantic_identity(
        "unordered-evidence", canonical_unordered_items(first)
    ) == semantic_identity("unordered-evidence", canonical_unordered_items(second))


def test_ordered_sequences_remain_semantically_ordered():
    assert semantic_identity("ordered", ["a", "b"]) != semantic_identity(
        "ordered", ["b", "a"]
    )


def test_equivalent_timezone_offsets_have_same_capture_command_identity():
    utc = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)
    taipei = datetime(
        2026, 9, 21, 10, 0, tzinfo=timezone(timedelta(hours=8))
    )

    assert capture_command_semantic_identity(command(utc)) == (
        capture_command_semantic_identity(command(taipei))
    )


@pytest.mark.parametrize(
    "field",
    [
        "capture_operation_id",
        "incident_id",
        "capture_contract_version",
        "canonicalization_version",
        "source_policy_version",
        "bounds_policy_version",
        "config_identity",
    ],
)
def test_every_evidence_affecting_command_field_changes_identity(field):
    base = command(datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc))
    changed = command(
        datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc),
        **{field: f"changed-{field}"},
    )

    assert capture_command_semantic_identity(base) != capture_command_semantic_identity(
        changed
    )


def test_snapshot_at_absolute_instant_changes_command_identity():
    base = command(datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc))
    changed = command(datetime(2026, 9, 21, 2, 0, 1, tzinfo=timezone.utc))
    assert capture_command_semantic_identity(base) != capture_command_semantic_identity(
        changed
    )


def test_transport_noise_is_not_part_of_capture_command_semantics():
    value = command(datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc))
    envelope_a = {"command": value, "trace_id": "trace-a", "latency_ms": 3}
    envelope_b = {
        "command": value,
        "trace_id": "trace-b",
        "latency_ms": 900,
        "process_retry_count": 4,
    }

    assert capture_command_semantic_identity(envelope_a["command"]) == (
        capture_command_semantic_identity(envelope_b["command"])
    )


def test_identity_is_domain_separated_and_rejects_non_finite_values():
    assert semantic_identity("revision", {"x": 1}) != semantic_identity(
        "snapshot", {"x": 1}
    )
    with pytest.raises(ValueError, match="finite"):
        canonical_json({"value": float("nan")})

