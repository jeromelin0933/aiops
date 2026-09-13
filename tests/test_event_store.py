import pytest

from src.event_detection.store.event_store import (
    EventStore,
    EventStoreReadIntegrityError,
)


def test_appends_jsonl_and_reads_valid_objects(tmp_path):
    path = tmp_path / "nested" / "events.jsonl"
    store = EventStore(path)
    store.write({"id": 1, "message": "異常"})
    store.write({"id": 2})
    assert store.read_all() == [{"id": 1, "message": "異常"}, {"id": 2}]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_read_all_skips_blank_invalid_and_non_object_json(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('\nnot-json\n[1, 2]\n{"valid": true}\n', encoding="utf-8")
    assert EventStore(path).read_all() == [{"valid": True}]


def test_missing_store_reads_as_empty(tmp_path):
    assert EventStore(tmp_path / "new" / "events.jsonl").read_all() == []


def test_authoritative_read_of_missing_store_is_reliably_empty(tmp_path):
    assert EventStore(tmp_path / "new" / "events.jsonl").read_all_authoritative() == []


def test_authoritative_read_ignores_representation_blank_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('\n  \n{"event_id": "EVT-1"}\n\t\n', encoding="utf-8")

    assert EventStore(path).read_all_authoritative() == [{"event_id": "EVT-1"}]


def test_authoritative_read_returns_all_events_without_mutating_payload_or_store(tmp_path):
    path = tmp_path / "events.jsonl"
    events = [
        {"event_id": "EVT-1", "nested": {"values": [1, 2]}, "nullable": None},
        {"event_id": "EVT-2", "message": "異常"},
    ]
    store = EventStore(path)
    for event in events:
        store.write(event)
    original_bytes = path.read_bytes()

    assert store.read_all_authoritative() == events
    assert path.read_bytes() == original_bytes


def test_authoritative_read_fails_closed_on_malformed_json(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('not-json\n', encoding="utf-8")

    with pytest.raises(EventStoreReadIntegrityError) as captured:
        EventStore(path).read_all_authoritative()

    assert captured.value.line_number == 1


def test_authoritative_read_fails_closed_on_non_object_json(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('[1, 2]\n', encoding="utf-8")

    with pytest.raises(EventStoreReadIntegrityError) as captured:
        EventStore(path).read_all_authoritative()

    assert captured.value.line_number == 1


def test_authoritative_read_wraps_undecodable_representation_as_integrity_failure(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'\xff\xfe\x00')

    with pytest.raises(EventStoreReadIntegrityError) as captured:
        EventStore(path).read_all_authoritative()

    assert captured.value.line_number is None


@pytest.mark.parametrize("corrupt_record", ["not-json", "[1, 2]"])
def test_authoritative_read_never_returns_partial_success_or_repairs_mixed_store(
    tmp_path, corrupt_record
):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event_id": "EVT-1"}\n'
        f"{corrupt_record}\n"
        '{"event_id": "EVT-2"}\n',
        encoding="utf-8",
    )
    original_bytes = path.read_bytes()

    with pytest.raises(EventStoreReadIntegrityError):
        EventStore(path).read_all_authoritative()

    assert path.read_bytes() == original_bytes


def test_authoritative_read_is_stable_after_reopen(tmp_path):
    path = tmp_path / "events.jsonl"
    original = EventStore(path)
    original.write({"event_id": "EVT-1", "status": "OPEN"})
    original.write({"event_id": "EVT-2", "status": "OPEN"})

    first = original.read_all_authoritative()
    reopened = EventStore(path)

    assert reopened.read_all_authoritative() == first
