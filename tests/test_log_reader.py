from pathlib import Path
from unittest.mock import patch

from src.event_detection.log.reader import LogReader


FIXTURE = Path(__file__).parent / "fixtures" / "reader_logs.jsonl"


def test_read_all_returns_only_nonblank_lines():
    assert LogReader(str(FIXTURE)).read_all() == ['{"id":1}', '{"id":2}']


def test_read_all_returns_empty_for_missing_file(tmp_path):
    assert LogReader(str(tmp_path / "missing.jsonl")).read_all() == []


def test_read_all_does_not_change_tail_offset(tmp_path):
    path = tmp_path / "logs.jsonl"
    path.write_text("first\n", encoding="utf-8")
    reader = LogReader(str(path))

    assert reader.read_all() == ["first"]
    assert reader._offset == 0


def test_tail_skips_existing_content_and_yields_appended_line(tmp_path):
    path = tmp_path / "logs.jsonl"
    path.write_text("existing\n", encoding="utf-8")
    reader = LogReader(str(path), poll_interval_seconds=0)
    stream = reader.tail()

    def append_during_first_sleep(_interval):
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write("\nappended\n")

    with patch("src.event_detection.log.reader.time.sleep", side_effect=append_during_first_sleep):
        assert next(stream) == "appended"


def test_tail_reads_recreated_or_truncated_file_from_start(tmp_path):
    path = tmp_path / "logs.jsonl"
    path.write_text("existing-content\n", encoding="utf-8")
    reader = LogReader(str(path), poll_interval_seconds=0)
    stream = reader.tail()

    def truncate_during_first_sleep(_interval):
        path.write_text("new\n", encoding="utf-8")

    with patch("src.event_detection.log.reader.time.sleep", side_effect=truncate_during_first_sleep):
        assert next(stream) == "new"
