import json
from datetime import timezone
from pathlib import Path

from src.event_detection.log.parser import OPTIONAL_FIELDS_DEFAULT, LogParser


FIXTURES = Path(__file__).parent / "fixtures"


def _lines(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def test_parse_valid_json_fills_optional_fields_and_parses_timestamp():
    result = LogParser().parse(_lines("valid_logs.jsonl")[0])

    assert result is not None
    assert result["status_code"] == 200
    assert result["duration_ms"] == 42
    assert result["_parsed_timestamp"].tzinfo == timezone.utc
    assert result["_parsed_timestamp"].microsecond == 123000
    assert all(field in result for field in OPTIONAL_FIELDS_DEFAULT)
    assert result["trace_id"] is None


def test_parse_coerces_numeric_strings_and_supported_timestamp_formats():
    parser = LogParser()
    string_values = parser.parse(_lines("valid_logs.jsonl")[1])
    offset_timestamp = parser.parse(_lines("valid_logs.jsonl")[2])
    optional_numbers = parser.parse(_lines("valid_logs.jsonl")[3])

    assert string_values["status_code"] == 401
    assert string_values["duration_ms"] == 18
    assert offset_timestamp["_parsed_timestamp"].tzinfo == timezone.utc
    assert optional_numbers["memory_usage_pct"] == 98.5
    assert isinstance(optional_numbers["memory_usage_pct"], float)
    assert offset_timestamp["rate_limit_quota"] == 100
    assert isinstance(offset_timestamp["rate_limit_quota"], int)


def test_malformed_fixture_lines_are_rejected():
    parser = LogParser()
    lines = [line for line in _lines("malformed_logs.jsonl") if line]

    assert lines
    assert all(parser.parse(line) is None for line in lines)


def test_non_string_input_is_rejected():
    assert LogParser().parse(None) is None


def test_naive_iso_timestamp_is_normalized_to_utc():
    raw = json.dumps({
        "timestamp": "2026-07-14T10:00:00",
        "level": "INFO",
        "service_name": "auth-service",
        "status_code": 200,
        "duration_ms": 1,
    })

    assert LogParser().parse(raw)["_parsed_timestamp"].tzinfo == timezone.utc
