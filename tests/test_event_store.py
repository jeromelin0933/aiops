from src.event_detection.store.event_store import EventStore


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
