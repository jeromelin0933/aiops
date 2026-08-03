import json
from copy import deepcopy
from pathlib import Path

import joblib
import pytest
import yaml

import src.event_detection.runner as runner_module
from src.event_detection.log.reader import LogReader
from src.event_detection.model.predictor import PredictionResult
from src.event_detection.metrics_iforest import (
    MetricsIForestModelNotFoundError,
    MetricsIForestModelVersionError,
)
from src.event_detection.runner import (
    PIPELINE_ORDER,
    DetectionPipeline,
    EventDetectionRunner,
    EventRunnerConfigLoader,
    EventRunnerCycleResult,
    LogEventDetectionRunner,
    PipelineRunResult,
    PipelineRuntimeState,
    _create_log_pipeline,
    _normalize_runtime_error_message,
    _sanitize_error_message,
    _validate_events_returned,
    main,
)


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(text)


def test_read_new_lines_once_starts_at_eof_for_existing_file(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("existing\n", encoding="utf-8")
    reader = LogReader(path)

    assert reader.read_new_lines_once() == []
    _append(path, "new-1\n\nnew-2\n")
    assert reader.read_new_lines_once() == ["new-1", "new-2"]
    assert reader.read_new_lines_once() == []


def test_read_new_lines_once_reads_file_created_after_start_from_beginning(tmp_path):
    path = tmp_path / "created-later.log"
    reader = LogReader(path)

    assert reader.read_new_lines_once() == []
    path.write_text("first\nsecond\n", encoding="utf-8")

    assert reader.read_new_lines_once() == ["first", "second"]


def test_read_new_lines_once_reads_rotated_file_from_beginning(tmp_path):
    path = tmp_path / "rotating.log"
    rotated_path = tmp_path / "rotating.log.1"
    path.write_text("existing\n", encoding="utf-8")
    reader = LogReader(path)
    assert reader.read_new_lines_once() == []

    path.replace(rotated_path)
    path.write_text("rotated-first\n", encoding="utf-8")

    assert path.stat().st_ino != rotated_path.stat().st_ino
    assert reader.read_new_lines_once() == ["rotated-first"]


def test_read_new_lines_once_resets_offset_after_same_inode_truncation(tmp_path):
    path = tmp_path / "truncated.log"
    path.write_text("long-existing-content\n", encoding="utf-8")
    reader = LogReader(path)
    assert reader.read_new_lines_once() == []
    original_inode = path.stat().st_ino

    path.write_text("new\n", encoding="utf-8")

    assert path.stat().st_ino == original_inode
    assert reader.read_new_lines_once() == ["new"]


def test_read_all_does_not_change_runtime_offset(tmp_path):
    path = tmp_path / "isolated.log"
    path.write_text("existing\n", encoding="utf-8")
    reader = LogReader(path)
    assert reader.read_new_lines_once() == []

    _append(path, "pending\n")
    assert reader.read_all() == ["existing", "pending"]
    assert reader.read_new_lines_once() == ["pending"]


def test_tail_reuses_read_new_lines_once(monkeypatch, tmp_path):
    reader = LogReader(tmp_path / "unused.log", poll_interval_seconds=0)
    batches = iter([[], ["from-single-read"]])
    calls = []

    def fake_read_once():
        calls.append("read")
        return next(batches)

    monkeypatch.setattr(reader, "read_new_lines_once", fake_read_once)
    monkeypatch.setattr("src.event_detection.log.reader.time.sleep", lambda _seconds: None)

    assert next(reader.tail()) == "from-single-read"
    assert calls == ["read", "read"]


def test_read_new_lines_once_does_not_sleep(monkeypatch, tmp_path):
    reader = LogReader(tmp_path / "missing.log")
    monkeypatch.setattr(
        "src.event_detection.log.reader.time.sleep",
        lambda _seconds: pytest.fail("read_new_lines_once must not sleep"),
    )

    assert reader.read_new_lines_once() == []


class FakeReader:
    def __init__(self, *batches):
        self.batches = list(batches)
        self.poll_interval = 0
        self.calls = 0

    def read_new_lines_once(self):
        self.calls += 1
        return self.batches.pop(0) if self.batches else []


class FakePredictor:
    def __init__(self, *, anomaly=True, load_error=None):
        self.anomaly = anomaly
        self.load_error = load_error
        self.load_count = 0
        self.vectors = []

    def load(self):
        self.load_count += 1
        if self.load_error is not None:
            raise self.load_error

    def predict_one(self, vector):
        self.vectors.append(vector)
        return PredictionResult(
            self.anomaly,
            -0.4 if self.anomaly else 0.1,
            0.9 if self.anomaly else 0.0,
            -1 if self.anomaly else 1,
        )


class FakeBuilder:
    def __init__(self, event_types=None):
        self.event_types = iter(event_types or ["general_log_anomaly"])
        self.summaries = []

    def build(self, _prediction, summary):
        self.summaries.append(summary)
        return {"event_type": next(self.event_types)}


class RecordingStore:
    def __init__(self, fail=False, error=None):
        self.fail = fail
        self.error = error
        self.events = []
        self.write_count = 0

    def write(self, event):
        self.write_count += 1
        if self.error is not None:
            raise self.error
        if self.fail:
            raise OSError("write failed")
        self.events.append(event)


def _config(*, min_log_count=1, cooldown_seconds=60):
    return {
        "window": {"window_seconds": 60, "min_log_count": min_log_count},
        "event": {"cooldown_seconds": cooldown_seconds},
        "output": {"model_path": "unused.pkl", "event_store_path": "unused.jsonl"},
        "anomaly": {
            "score_threshold": -0.05,
            "confidence_high_threshold": -0.3,
            "confidence_medium_threshold": -0.1,
        },
    }


def _line(second=0, *, status=500):
    return json.dumps(
        {
            "timestamp": f"2026-01-01T00:00:{second:02d}Z",
            "level": "ERROR" if status >= 400 else "INFO",
            "service_name": "test-service",
            "status_code": status,
            "duration_ms": 10,
        }
    )


def _runner(reader, predictor=None, builder=None, store=None, config=None):
    return LogEventDetectionRunner(
        config=config or _config(),
        reader=reader,
        predictor=predictor or FakePredictor(),
        builder=builder or FakeBuilder(),
        store=store or RecordingStore(),
    )


def test_log_runner_initialize_loads_model_only_once():
    predictor = FakePredictor()
    runner = _runner(FakeReader(), predictor=predictor)

    runner.initialize()
    runner.initialize()

    assert predictor.load_count == 1


def test_log_runner_initialize_propagates_model_load_error():
    predictor = FakePredictor(load_error=RuntimeError("bad model"))
    runner = _runner(FakeReader(), predictor=predictor)

    with pytest.raises(RuntimeError, match="bad model"):
        runner.initialize()
    assert predictor.load_count == 1
    assert runner._initialized is False


def test_log_runner_run_once_returns_empty_without_new_logs():
    predictor = FakePredictor()
    runner = _runner(FakeReader([]), predictor=predictor)

    assert runner.run_once() == []
    assert predictor.load_count == 1
    assert predictor.vectors == []


def test_log_runner_run_once_skips_parser_failure_and_insufficient_window():
    predictor = FakePredictor()
    reader = FakeReader(["not-json", _line(0)])
    runner = _runner(
        reader,
        predictor=predictor,
        config=_config(min_log_count=2),
    )

    assert runner.run_once() == []
    assert predictor.vectors == []


def test_log_runner_run_once_skips_normal_window():
    predictor = FakePredictor(anomaly=False)
    store = RecordingStore()
    runner = _runner(FakeReader([_line(0)]), predictor=predictor, store=store)

    assert runner.run_once() == []
    assert len(predictor.vectors) == 1
    assert store.events == []


def test_log_runner_run_once_returns_only_successfully_written_event():
    store = RecordingStore()
    event = {"event_type": "general_log_anomaly"}
    builder = FakeBuilder([event["event_type"]])
    runner = _runner(FakeReader([_line(0)]), builder=builder, store=store)

    assert runner.run_once() == [event]
    assert store.events == [event]
    assert "general_log_anomaly" in runner._last_fired


def test_log_runner_run_once_applies_cooldown_without_returning_duplicate():
    store = RecordingStore()
    runner = _runner(
        FakeReader([_line(0)], [_line(1)]),
        builder=FakeBuilder(["same-event", "same-event"]),
        store=store,
    )

    assert runner.run_once() == [{"event_type": "same-event"}]
    assert runner.run_once() == []
    assert store.events == [{"event_type": "same-event"}]


@pytest.mark.parametrize(
    "write_error",
    [
        OSError("write failed"),
        PermissionError("permission denied"),
        AttributeError("store bug"),
    ],
)
def test_log_runner_write_failure_propagates_and_does_not_record_cooldown(
    write_error,
):
    runner = _runner(
        FakeReader([_line(0)]), store=RecordingStore(error=write_error)
    )

    with pytest.raises(type(write_error), match=str(write_error)):
        runner.run_once()

    assert runner._last_fired == {}


def test_log_runner_retries_same_event_after_write_failure():
    class FailOnceStore(RecordingStore):
        def write(self, event):
            self.write_count += 1
            if self.write_count == 1:
                raise OSError("temporary write failure")
            self.events.append(event)

    store = FailOnceStore()
    runner = _runner(
        FakeReader([_line(0)], [_line(1)]),
        builder=FakeBuilder(["same-event", "same-event"]),
        store=store,
    )

    with pytest.raises(OSError, match="temporary write failure"):
        runner.run_once()
    assert "same-event" not in runner._last_fired

    assert runner.run_once() == [{"event_type": "same-event"}]
    assert store.events == [{"event_type": "same-event"}]
    assert "same-event" in runner._last_fired


def test_log_runner_success_preserves_event_identity_and_records_cooldown():
    event = {"event_type": "identity-event", "payload": {"value": 1}}

    class IdentityBuilder:
        def build(self, _prediction, _summary):
            return event

    store = RecordingStore()
    runner = _runner(
        FakeReader([_line(0)]), builder=IdentityBuilder(), store=store
    )

    returned = runner.run_once()

    assert returned[0] is event
    assert store.events[0] is event
    assert "identity-event" in runner._last_fired


def test_log_runner_run_once_can_return_multiple_successful_events():
    store = RecordingStore()
    runner = _runner(
        FakeReader([_line(0), _line(1)]),
        builder=FakeBuilder(["event-a", "event-b"]),
        store=store,
    )

    assert runner.run_once() == [
        {"event_type": "event-a"},
        {"event_type": "event-b"},
    ]
    assert store.events == [
        {"event_type": "event-a"},
        {"event_type": "event-b"},
    ]


def test_log_runner_run_once_does_not_reload_model_or_sleep(monkeypatch):
    predictor = FakePredictor()
    runner = _runner(FakeReader([], []), predictor=predictor)
    monkeypatch.setattr(
        "src.event_detection.runner.time.sleep",
        lambda _seconds: pytest.fail("run_once must not sleep"),
    )

    assert runner.run_once() == []
    assert runner.run_once() == []
    assert predictor.load_count == 1


def test_log_runner_start_keeps_standalone_polling_capability(monkeypatch):
    predictor = FakePredictor()
    runner = _runner(FakeReader([]), predictor=predictor)
    run_calls = []

    def fake_run_once():
        run_calls.append("run")
        return []

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    monkeypatch.setattr(
        "src.event_detection.runner.time.sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    runner.start()

    assert predictor.load_count == 1
    assert run_calls == ["run"]


def _event_runner_config(tmp_path, *, enabled=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    enabled = enabled or {name: True for name in PIPELINE_ORDER}
    child_paths = {}
    for name in PIPELINE_ORDER:
        child_path = tmp_path / f"{name}.yaml"
        if enabled[name]:
            child_path.write_text("{}\n", encoding="utf-8")
        child_paths[name] = child_path
    return {
        "runtime": {"tick_seconds": 1.0},
        "pipelines": {
            "log_event_detection": {
                "enabled": enabled["log_event_detection"],
                "config_path": str(child_paths["log_event_detection"]),
                "interval_seconds": 5.0,
            },
            "metrics_threshold_detection": {
                "enabled": enabled["metrics_threshold_detection"],
                "config_path": str(child_paths["metrics_threshold_detection"]),
                "interval_seconds": 15.0,
            },
            "metrics_iforest_detection": {
                "enabled": enabled["metrics_iforest_detection"],
                "config_path": str(child_paths["metrics_iforest_detection"]),
                "interval_seconds": 15.0,
            },
        },
    }


def _write_event_runner_config(tmp_path, config):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "event_runner.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _start_enabled_log_with_child_config(tmp_path, child_config):
    enabled = {
        "log_event_detection": True,
        "metrics_threshold_detection": False,
        "metrics_iforest_detection": False,
    }
    config = _event_runner_config(tmp_path, enabled=enabled)
    child_path = Path(config["pipelines"]["log_event_detection"]["config_path"])
    if isinstance(child_config, str):
        child_path.write_text(child_config, encoding="utf-8")
    else:
        child_path.write_text(
            yaml.safe_dump(child_config, sort_keys=False), encoding="utf-8"
        )
    return EventDetectionRunner(_write_event_runner_config(tmp_path, config))


def _construct_log_runner_with_config(config):
    return LogEventDetectionRunner(
        config=config,
        reader=FakeReader(),
        predictor=FakePredictor(),
        builder=FakeBuilder(),
        store=RecordingStore(),
    )


class FakePipeline:
    def __init__(self, events=None):
        self.events = events or []

    def run_once(self):
        return self.events


def _all_overrides():
    return {name: FakePipeline() for name in PIPELINE_ORDER}


def test_phase_three_data_structures_and_protocol_are_available():
    detector = FakePipeline()
    state = PipelineRuntimeState("log_event_detection", detector, 5.0, 10.0)
    result = PipelineRunResult(
        pipeline_name="log_event_detection",
        status="SUCCESS",
        started_at="start",
        completed_at="end",
        duration_ms=1.0,
        events=[],
        event_count=0,
    )
    cycle = EventRunnerCycleResult(
        mode="FORCED",
        started_at="start",
        completed_at="end",
        pipeline_results=[result],
        total_event_count=0,
        success_count=1,
        failure_count=0,
        skipped_count=0,
    )

    assert DetectionPipeline.__name__ == "DetectionPipeline"
    assert callable(detector.run_once)
    assert state.detector is detector
    assert cycle.pipeline_results == [result]


def test_formal_event_runner_config_loads_fixed_intervals():
    config = EventRunnerConfigLoader.load("configs/event_runner.yaml")

    assert PIPELINE_ORDER == (
        "log_event_detection",
        "metrics_threshold_detection",
        "metrics_iforest_detection",
    )
    assert config["runtime"]["tick_seconds"] == 1.0
    assert [
        config["pipelines"][name]["interval_seconds"] for name in PIPELINE_ORDER
    ] == [5.0, 15.0, 15.0]


def test_event_runner_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        EventRunnerConfigLoader.load(tmp_path / "missing.yaml")


def test_event_runner_config_yaml_error_is_preserved(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("runtime: [\n", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        EventRunnerConfigLoader.load(path)


@pytest.mark.parametrize("missing_section", ["runtime", "pipelines"])
def test_event_runner_config_requires_top_level_sections(tmp_path, missing_section):
    config = _event_runner_config(tmp_path)
    config.pop(missing_section)

    with pytest.raises(ValueError):
        EventRunnerConfigLoader.load(_write_event_runner_config(tmp_path, config))


@pytest.mark.parametrize("tick_seconds", [0, -1, float("nan"), float("inf"), True])
def test_event_runner_config_rejects_invalid_tick(tmp_path, tick_seconds):
    config = _event_runner_config(tmp_path)
    config["runtime"]["tick_seconds"] = tick_seconds

    with pytest.raises(ValueError):
        EventRunnerConfigLoader.load(_write_event_runner_config(tmp_path, config))


def test_event_runner_config_rejects_missing_or_unknown_pipeline(tmp_path):
    missing = _event_runner_config(tmp_path / "missing")
    missing["pipelines"].pop("log_event_detection")
    with pytest.raises(ValueError, match="missing pipeline"):
        EventRunnerConfigLoader.load(
            _write_event_runner_config(tmp_path / "missing", missing)
        )

    unknown = _event_runner_config(tmp_path / "unknown")
    unknown["pipelines"]["unknown_detection"] = {
        "enabled": False,
        "interval_seconds": 1.0,
    }
    with pytest.raises(ValueError, match="unknown pipeline"):
        EventRunnerConfigLoader.load(
            _write_event_runner_config(tmp_path / "unknown", unknown)
        )


def test_event_runner_config_requires_boolean_enabled(tmp_path):
    config = _event_runner_config(tmp_path)
    config["pipelines"]["log_event_detection"]["enabled"] = 1

    with pytest.raises(ValueError, match="enabled must be boolean"):
        EventRunnerConfigLoader.load(_write_event_runner_config(tmp_path, config))


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), False])
def test_event_runner_config_rejects_invalid_interval(tmp_path, interval):
    config = _event_runner_config(tmp_path)
    config["pipelines"]["log_event_detection"]["interval_seconds"] = interval

    with pytest.raises(ValueError):
        EventRunnerConfigLoader.load(_write_event_runner_config(tmp_path, config))


@pytest.mark.parametrize(
    "pipeline_name",
    ["metrics_threshold_detection", "metrics_iforest_detection"],
)
def test_event_runner_config_requires_fifteen_second_metrics_interval(
    tmp_path, pipeline_name
):
    config = _event_runner_config(tmp_path)
    config["pipelines"][pipeline_name]["interval_seconds"] = 14.0

    with pytest.raises(ValueError, match="must be 15.0"):
        EventRunnerConfigLoader.load(_write_event_runner_config(tmp_path, config))


def test_event_runner_config_requires_enabled_child_config_file(tmp_path):
    config = _event_runner_config(tmp_path)
    missing_path = tmp_path / "does-not-exist.yaml"
    config["pipelines"]["log_event_detection"]["config_path"] = str(missing_path)

    with pytest.raises(FileNotFoundError) as error:
        EventRunnerConfigLoader.load(_write_event_runner_config(tmp_path, config))
    assert str(missing_path) in str(error.value)


def test_event_runner_config_rejects_all_disabled(tmp_path):
    config = _event_runner_config(
        tmp_path,
        enabled={name: False for name in PIPELINE_ORDER},
    )

    with pytest.raises(ValueError, match="at least one pipeline"):
        EventRunnerConfigLoader.load(_write_event_runner_config(tmp_path, config))


def test_event_runner_config_rejects_tick_larger_than_smallest_enabled_interval(tmp_path):
    config = _event_runner_config(tmp_path)
    config["runtime"]["tick_seconds"] = 6.0

    with pytest.raises(ValueError, match="minimum enabled interval"):
        EventRunnerConfigLoader.load(_write_event_runner_config(tmp_path, config))


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty-yaml"),
        pytest.param("- item\n", id="list-root"),
        pytest.param("plain-string\n", id="string-root"),
        pytest.param("42\n", id="number-root"),
    ],
)
def test_enabled_log_pipeline_rejects_non_mapping_yaml_root(tmp_path, content):
    with pytest.raises(TypeError, match="log child config root must be a mapping"):
        _start_enabled_log_with_child_config(tmp_path, content)


def test_enabled_log_pipeline_preserves_invalid_yaml_parse_error(tmp_path):
    with pytest.raises(yaml.YAMLError):
        _start_enabled_log_with_child_config(tmp_path, "window: [\n")


@pytest.mark.parametrize(
    ("section", "value"),
    [
        ("log_reader", []),
        ("window", []),
        ("anomaly", "invalid"),
        ("output", 1),
        ("event", []),
        ("feature_extraction", "invalid"),
    ],
)
def test_enabled_log_pipeline_rejects_non_mapping_runtime_sections(
    tmp_path, section, value
):
    config = deepcopy(_config())
    config[section] = value

    with pytest.raises(TypeError, match=section):
        _start_enabled_log_with_child_config(tmp_path, config)


@pytest.mark.parametrize("section", ["window", "anomaly", "output"])
def test_enabled_log_pipeline_requires_runtime_sections(tmp_path, section):
    config = deepcopy(_config())
    config.pop(section)

    with pytest.raises(ValueError, match=section):
        _start_enabled_log_with_child_config(tmp_path, config)


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        pytest.param("log_reader.log_file_path", "", id="empty-log-path"),
        pytest.param("log_reader.log_file_path", "   ", id="blank-log-path"),
        pytest.param("log_reader.poll_interval_seconds", "5", id="poll-string"),
        pytest.param("log_reader.poll_interval_seconds", 0, id="poll-zero"),
        pytest.param("log_reader.poll_interval_seconds", -1, id="poll-negative"),
        pytest.param("log_reader.poll_interval_seconds", True, id="poll-bool"),
        pytest.param("log_reader.poll_interval_seconds", float("nan"), id="poll-nan"),
        pytest.param("log_reader.poll_interval_seconds", float("inf"), id="poll-inf"),
        pytest.param("window.window_seconds", 0, id="window-zero"),
        pytest.param("window.window_seconds", -1, id="window-negative"),
        pytest.param("window.window_seconds", True, id="window-bool"),
        pytest.param("window.window_seconds", float("nan"), id="window-nan"),
        pytest.param("window.window_seconds", float("inf"), id="window-inf"),
        pytest.param("window.min_log_count", 0, id="count-zero"),
        pytest.param("window.min_log_count", -1, id="count-negative"),
        pytest.param("window.min_log_count", 1.5, id="count-float"),
        pytest.param("window.min_log_count", True, id="count-bool"),
        pytest.param("window.min_log_count", "5", id="count-string"),
        pytest.param("event.cooldown_seconds", -1, id="cooldown-negative"),
        pytest.param("event.cooldown_seconds", True, id="cooldown-bool"),
        pytest.param("event.cooldown_seconds", float("nan"), id="cooldown-nan"),
        pytest.param("event.cooldown_seconds", float("inf"), id="cooldown-inf"),
        pytest.param("event.cooldown_seconds", "60", id="cooldown-string"),
    ],
)
def test_enabled_log_pipeline_rejects_invalid_runtime_field_values(
    tmp_path, field_path, value
):
    config = deepcopy(_config())
    section, field = field_path.split(".")
    config.setdefault(section, {})[field] = value

    with pytest.raises((TypeError, ValueError), match=field_path):
        _start_enabled_log_with_child_config(tmp_path, config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("score_threshold", "invalid"),
        ("score_threshold", True),
        ("score_threshold", float("nan")),
        ("score_threshold", float("inf")),
        ("confidence_high_threshold", "invalid"),
        ("confidence_high_threshold", True),
        ("confidence_high_threshold", float("nan")),
        ("confidence_high_threshold", float("inf")),
        ("confidence_medium_threshold", "invalid"),
        ("confidence_medium_threshold", True),
        ("confidence_medium_threshold", float("nan")),
        ("confidence_medium_threshold", float("inf")),
    ],
)
def test_enabled_log_pipeline_rejects_invalid_anomaly_threshold(
    tmp_path, field, value
):
    config = deepcopy(_config())
    config["anomaly"][field] = value

    with pytest.raises((TypeError, ValueError), match=f"anomaly.{field}"):
        _start_enabled_log_with_child_config(tmp_path, config)


@pytest.mark.parametrize(
    ("high_threshold", "medium_threshold"),
    [(-0.1, -0.1), (-1.0, -0.1), (-0.05, -0.1), (-0.3, 0.0)],
)
def test_enabled_log_pipeline_rejects_invalid_confidence_threshold_relationship(
    tmp_path, high_threshold, medium_threshold
):
    config = deepcopy(_config())
    config["anomaly"]["confidence_high_threshold"] = high_threshold
    config["anomaly"]["confidence_medium_threshold"] = medium_threshold

    with pytest.raises(ValueError, match="confidence thresholds"):
        _start_enabled_log_with_child_config(tmp_path, config)


@pytest.mark.parametrize(
    "missing_field",
    ["score_threshold", "confidence_high_threshold", "confidence_medium_threshold"],
)
def test_enabled_log_pipeline_requires_anomaly_thresholds(tmp_path, missing_field):
    config = deepcopy(_config())
    config["anomaly"].pop(missing_field)

    with pytest.raises(ValueError, match=f"anomaly.{missing_field}"):
        _start_enabled_log_with_child_config(tmp_path, config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_path", None),
        ("model_path", ""),
        ("model_path", "   "),
        ("event_store_path", ""),
        ("event_store_path", "   "),
    ],
)
def test_enabled_log_pipeline_rejects_invalid_output_paths(tmp_path, field, value):
    config = deepcopy(_config())
    config["output"][field] = value

    with pytest.raises(ValueError, match=f"output.{field}"):
        _start_enabled_log_with_child_config(tmp_path, config)


def test_enabled_log_pipeline_requires_model_path(tmp_path):
    config = deepcopy(_config())
    config["output"].pop("model_path")

    with pytest.raises(ValueError, match="output.model_path"):
        _start_enabled_log_with_child_config(tmp_path, config)


@pytest.mark.parametrize("field", ["known_services", "known_error_types"])
@pytest.mark.parametrize("value", ["service", ["valid", 1], [None], [{}]])
def test_enabled_log_pipeline_validates_feature_extraction_lists(
    tmp_path, field, value
):
    config = deepcopy(_config())
    config["feature_extraction"] = {field: value}

    with pytest.raises(TypeError, match=f"feature_extraction.{field}"):
        _start_enabled_log_with_child_config(tmp_path, config)


def test_log_child_config_optional_fields_keep_runtime_defaults(monkeypatch):
    captured = {}

    class CapturingEventStore(RecordingStore):
        def __init__(self, path):
            super().__init__()
            captured["event_store_path"] = path

    monkeypatch.setattr(runner_module, "EventStore", CapturingEventStore)
    config = deepcopy(_config())
    config.pop("event")
    config["window"] = {}
    config["output"].pop("event_store_path")
    config["isolation_forest"] = "training-only-section-is-ignored"

    runner = LogEventDetectionRunner(
        config=config,
        predictor=FakePredictor(),
        builder=FakeBuilder(),
    )

    assert runner.reader.path == Path("logs/aiops.json.log")
    assert runner.reader.poll_interval == 5
    assert runner.aggregator.window_seconds == 60
    assert runner.aggregator.min_log_count == 5
    assert runner.cooldown_seconds == 60
    assert captured["event_store_path"] == "events/event_store.jsonl"
    runner.initialize()
    assert runner._initialized is True


def test_log_child_config_accepts_zero_cooldown_and_optional_feature_config():
    config = deepcopy(_config(cooldown_seconds=0))
    config["feature_extraction"] = {
        "known_services": ["auth-service"],
        "known_error_types": ["AuthenticationFailed"],
    }

    runner = _construct_log_runner_with_config(config)

    assert runner.cooldown_seconds == 0


def test_disabled_pipeline_does_not_require_config_or_initialize_factory(tmp_path, monkeypatch):
    enabled = {
        "log_event_detection": True,
        "metrics_threshold_detection": False,
        "metrics_iforest_detection": False,
    }
    config = _event_runner_config(tmp_path, enabled=enabled)
    for name in PIPELINE_ORDER[1:]:
        config["pipelines"][name]["config_path"] = str(tmp_path / f"missing-{name}.yaml")
        monkeypatch.setitem(
            runner_module.DEFAULT_PIPELINE_FACTORIES,
            name,
            lambda _path: pytest.fail("disabled pipeline factory must not run"),
        )
    log_override = FakePipeline()

    runner = EventDetectionRunner(
        _write_event_runner_config(tmp_path, config),
        pipeline_overrides={"log_event_detection": log_override},
        clock=lambda: 10.0,
    )

    assert list(runner.pipeline_states) == ["log_event_detection"]
    assert runner.pipeline_states["log_event_detection"].detector is log_override


def test_pipeline_overrides_prevent_all_default_detector_creation(tmp_path, monkeypatch):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    overrides = _all_overrides()
    for name in PIPELINE_ORDER:
        monkeypatch.setitem(
            runner_module.DEFAULT_PIPELINE_FACTORIES,
            name,
            lambda _path: pytest.fail("overridden default factory must not run"),
        )

    runner = EventDetectionRunner(config_path, pipeline_overrides=overrides, clock=lambda: 7.0)

    assert list(runner.pipeline_states) == list(PIPELINE_ORDER)
    assert [runner.pipeline_states[name].detector for name in PIPELINE_ORDER] == [
        overrides[name] for name in PIPELINE_ORDER
    ]


def test_unknown_pipeline_override_fails_before_detector_creation(tmp_path, monkeypatch):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    for name in PIPELINE_ORDER:
        monkeypatch.setitem(
            runner_module.DEFAULT_PIPELINE_FACTORIES,
            name,
            lambda _path: pytest.fail("factory must not run for invalid overrides"),
        )

    with pytest.raises(ValueError, match="unknown pipeline override"):
        EventDetectionRunner(
            config_path,
            pipeline_overrides={"unknown_detection": FakePipeline()},
        )


def test_default_detector_startup_error_is_propagated(tmp_path, monkeypatch):
    enabled = {
        "log_event_detection": True,
        "metrics_threshold_detection": False,
        "metrics_iforest_detection": False,
    }
    config_path = _write_event_runner_config(
        tmp_path, _event_runner_config(tmp_path, enabled=enabled)
    )

    def fail_factory(_path):
        raise RuntimeError("model initialization failed")

    monkeypatch.setitem(
        runner_module.DEFAULT_PIPELINE_FACTORIES,
        "log_event_detection",
        fail_factory,
    )

    with pytest.raises(RuntimeError, match="model initialization failed"):
        EventDetectionRunner(config_path)


def test_incompatible_log_artifact_fails_unified_runner_startup(
    tmp_path, monkeypatch
):
    enabled = {
        "log_event_detection": True,
        "metrics_threshold_detection": False,
        "metrics_iforest_detection": False,
    }
    config = _event_runner_config(tmp_path, enabled=enabled)
    model_path = tmp_path / "invalid-log-model.pkl"
    joblib.dump({"model": object()}, model_path)
    log_config = _config()
    log_config["output"]["model_path"] = str(model_path)
    Path(config["pipelines"]["log_event_detection"]["config_path"]).write_text(
        yaml.safe_dump(log_config, sort_keys=False), encoding="utf-8"
    )
    created = []

    class CapturingLogRunner(LogEventDetectionRunner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(runner_module, "LogEventDetectionRunner", CapturingLogRunner)

    with pytest.raises(TypeError, match="unsupported model artifact type: dict"):
        EventDetectionRunner(_write_event_runner_config(tmp_path, config))

    assert len(created) == 1
    assert created[0]._initialized is False


def test_disabled_log_pipeline_does_not_load_or_validate_model(tmp_path, monkeypatch):
    enabled = {
        "log_event_detection": False,
        "metrics_threshold_detection": True,
        "metrics_iforest_detection": False,
    }
    config = _event_runner_config(tmp_path, enabled=enabled)
    config["pipelines"]["log_event_detection"]["config_path"] = str(
        tmp_path / "missing-log-config.yaml"
    )
    monkeypatch.setitem(
        runner_module.DEFAULT_PIPELINE_FACTORIES,
        "log_event_detection",
        lambda _path: pytest.fail("disabled log pipeline factory must not run"),
    )
    monkeypatch.setattr(
        "src.event_detection.model.predictor.joblib.load",
        lambda _path: pytest.fail("disabled log pipeline must not load a model"),
    )
    threshold = FakePipeline()

    runner = EventDetectionRunner(
        _write_event_runner_config(tmp_path, config),
        pipeline_overrides={"metrics_threshold_detection": threshold},
    )

    assert list(runner.pipeline_states) == ["metrics_threshold_detection"]
    assert runner.pipeline_states["metrics_threshold_detection"].detector is threshold


@pytest.mark.parametrize(
    "disabled_child_content",
    [
        "window: [\n",
        yaml.safe_dump({"window": [], "anomaly": None, "output": None}),
    ],
)
def test_disabled_log_pipeline_does_not_parse_or_validate_child_config(
    tmp_path, monkeypatch, disabled_child_content
):
    enabled = {
        "log_event_detection": False,
        "metrics_threshold_detection": True,
        "metrics_iforest_detection": False,
    }
    config = _event_runner_config(tmp_path, enabled=enabled)
    disabled_path = tmp_path / "disabled-log.yaml"
    disabled_path.write_text(disabled_child_content, encoding="utf-8")
    config["pipelines"]["log_event_detection"]["config_path"] = str(disabled_path)
    monkeypatch.setitem(
        runner_module.DEFAULT_PIPELINE_FACTORIES,
        "log_event_detection",
        lambda _path: pytest.fail("disabled log pipeline factory must not run"),
    )
    threshold = FakePipeline()

    runner = EventDetectionRunner(
        _write_event_runner_config(tmp_path, config),
        pipeline_overrides={"metrics_threshold_detection": threshold},
    )

    assert list(runner.pipeline_states) == ["metrics_threshold_detection"]


def test_log_default_factory_initializes_model_before_return(monkeypatch):
    created = []

    class FakeLogDetector:
        def __init__(self, config_path):
            self.config_path = config_path
            self.initialize_count = 0
            created.append(self)

        def initialize(self):
            self.initialize_count += 1

        def run_once(self):
            return []

    monkeypatch.setattr(runner_module, "LogEventDetectionRunner", FakeLogDetector)

    detector = _create_log_pipeline("log-config.yaml")

    assert detector is created[0]
    assert detector.config_path == "log-config.yaml"
    assert detector.initialize_count == 1


def test_constructor_sets_same_initial_due_with_single_clock_read(tmp_path):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    clock_calls = []

    def fake_clock():
        clock_calls.append("clock")
        return 123.5

    sleeper = lambda _seconds: None
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides=_all_overrides(),
        clock=fake_clock,
        sleeper=sleeper,
    )

    assert clock_calls == ["clock"]
    assert [
        runner.pipeline_states[name].next_due_monotonic for name in PIPELINE_ORDER
    ] == [123.5, 123.5, 123.5]
    assert runner.clock is fake_clock
    assert runner.sleeper is sleeper
    assert runner._ready is True


@pytest.mark.parametrize(
    "value",
    [[], [{"event_type": "x"}], [{"event_type": "x"}, {}]],
)
def test_return_contract_validation_accepts_list_of_dicts_without_modification(value):
    assert _validate_events_returned("log_event_detection", value) is value


@pytest.mark.parametrize("value", [None, (), "event", {}, ["event"]])
def test_return_contract_validation_rejects_invalid_values(value):
    with pytest.raises(TypeError):
        _validate_events_returned("log_event_detection", value)


class RecordingPipeline:
    def __init__(self, name, call_order, *, returned=None, error=None):
        self.name = name
        self.call_order = call_order
        self.returned = [] if returned is None else returned
        self.error = error
        self.call_count = 0

    def run_once(self):
        self.call_count += 1
        self.call_order.append(self.name)
        if self.error is not None:
            raise self.error
        return self.returned


def _recording_overrides(*, returns=None, errors=None, call_order=None):
    returns = returns or {}
    errors = errors or {}
    call_order = call_order if call_order is not None else []
    return {
        name: RecordingPipeline(
            name,
            call_order,
            returned=returns.get(name, []),
            error=errors.get(name),
        )
        for name in PIPELINE_ORDER
    }


@pytest.mark.parametrize(
    ("raw_message", "secret_values", "preserved_values"),
    [
        ("token=abc123", ["abc123"], ["token=", "[REDACTED]"]),
        ("password: my-secret", ["my-secret"], ["password:"]),
        ('API_KEY="secret-value"', ["secret-value"], ['API_KEY="[REDACTED]"']),
        ("Access_Token=access-value", ["access-value"], ["Access_Token="]),
        ("access-token=hyphen-access", ["hyphen-access"], ["access-token="]),
        ("api-key=hyphen-value", ["hyphen-value"], ["api-key="]),
        ("apikey=compact-value", ["compact-value"], ["apikey="]),
        ("passwd whitespace-value", ["whitespace-value"], ["passwd "]),
        ("secret=credential-value", ["credential-value"], ["secret="]),
        (
            "Authorization: Bearer eyJhbGciOi...",
            ["eyJhbGciOi..."],
            ["Authorization: Bearer [REDACTED]"],
        ),
        (
            "authorization=Basic dXNlcjpwYXNz",
            ["dXNlcjpwYXNz"],
            ["authorization=Basic [REDACTED]"],
        ),
        (
            "Bearer standalone-credential",
            ["standalone-credential"],
            ["Bearer [REDACTED]"],
        ),
        (
            "request failed: /api?token=query-value&timeout=5",
            ["query-value"],
            ["/api?token=[REDACTED]&timeout=5"],
        ),
        (
            "url=https://example.test?api_key=url-secret&status=active",
            ["url-secret"],
            ["https://example.test?api_key=[REDACTED]&status=active"],
        ),
        (
            "token=first-value&password=second-value&timeout=5",
            ["first-value", "second-value"],
            ["token=[REDACTED]", "password=[REDACTED]", "timeout=5"],
        ),
    ],
)
def test_runtime_error_sanitizer_redacts_sensitive_values(
    raw_message, secret_values, preserved_values
):
    sanitized = _sanitize_error_message(raw_message)

    for secret_value in secret_values:
        assert secret_value not in sanitized
    for preserved_value in preserved_values:
        assert preserved_value in sanitized


@pytest.mark.parametrize(
    "message",
    [
        "Prometheus timeout after 5 seconds",
        "status_code=500 metric_name=api_requests_per_sec",
        "model feature dimension mismatch: expected 23, got 10",
        "permission denied while writing event store",
        "connection refused",
        "invalid response format",
    ],
)
def test_runtime_error_sanitizer_preserves_normal_technical_messages(message):
    assert _sanitize_error_message(message) == message


@pytest.mark.parametrize(
    "error",
    [Exception(), RuntimeError(""), ValueError("   ")],
)
def test_empty_runtime_exception_message_falls_back_to_error_type(
    tmp_path, error
):
    call_order = []
    returns = {
        "metrics_threshold_detection": [{"event_type": "threshold"}],
        "metrics_iforest_detection": [{"event_type": "iforest"}],
    }
    runner = EventDetectionRunner(
        _write_event_runner_config(tmp_path, _event_runner_config(tmp_path)),
        pipeline_overrides=_recording_overrides(
            returns=returns,
            errors={"log_event_detection": error},
            call_order=call_order,
        ),
    )

    cycle = runner.run_once()

    failed = cycle.pipeline_results[0]
    assert failed.status == "FAILED"
    assert failed.error_type == type(error).__name__
    assert failed.error_message == type(error).__name__
    assert failed.error_message.strip()
    assert failed.events == []
    assert failed.event_count == 0
    assert call_order == list(PIPELINE_ORDER)
    assert cycle.total_event_count == 2


def test_runtime_failure_log_contains_only_sanitized_message(tmp_path, caplog):
    raw_secret_message = "token=abc123 password=my-secret"
    runner = EventDetectionRunner(
        _write_event_runner_config(tmp_path, _event_runner_config(tmp_path)),
        pipeline_overrides=_recording_overrides(
            errors={"log_event_detection": RuntimeError(raw_secret_message)}
        ),
    )

    with caplog.at_level("ERROR", logger="EventDetectionRunner"):
        cycle = runner.run_once()

    failed = cycle.pipeline_results[0]
    assert failed.error_type == "RuntimeError"
    assert "abc123" not in failed.error_message
    assert "my-secret" not in failed.error_message
    assert "[REDACTED]" in failed.error_message
    assert "abc123" not in caplog.text
    assert "my-secret" not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "pipeline_name=log_event_detection" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "Traceback" not in caplog.text
    assert raw_secret_message not in caplog.text


@pytest.mark.parametrize(
    ("failed_pipeline", "error"),
    [
        ("log_event_detection", RuntimeError("token=log-secret")),
        ("metrics_threshold_detection", Exception()),
        (
            "metrics_iforest_detection",
            ValueError("password=iforest-secret"),
        ),
    ],
)
def test_sanitized_runtime_failure_preserves_pipeline_isolation(
    tmp_path, failed_pipeline, error
):
    call_order = []
    returns = {
        name: [{"event_type": f"{name}-event"}]
        for name in PIPELINE_ORDER
        if name != failed_pipeline
    }
    runner = EventDetectionRunner(
        _write_event_runner_config(tmp_path, _event_runner_config(tmp_path)),
        pipeline_overrides=_recording_overrides(
            returns=returns,
            errors={failed_pipeline: error},
            call_order=call_order,
        ),
    )

    cycle = runner.run_once()

    assert call_order == list(PIPELINE_ORDER)
    results = {result.pipeline_name: result for result in cycle.pipeline_results}
    failed = results[failed_pipeline]
    assert failed.status == "FAILED"
    assert failed.error_type == type(error).__name__
    assert failed.error_message.strip()
    assert failed.events == []
    assert failed.event_count == 0
    assert cycle.total_event_count == 2
    assert cycle.success_count == 2
    assert cycle.failure_count == 1
    assert all(
        results[name].events[0] is returns[name][0]
        for name in returns
    )
    assert "log-secret" not in failed.error_message
    assert "iforest-secret" not in failed.error_message


def test_forced_run_once_executes_all_enabled_pipelines_in_fixed_order(tmp_path):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    call_order = []
    overrides = _recording_overrides(call_order=call_order)
    runner = EventDetectionRunner(config_path, pipeline_overrides=overrides)

    before_due = {
        name: state.next_due_monotonic for name, state in runner.pipeline_states.items()
    }
    cycle = runner.run_once()

    assert call_order == list(PIPELINE_ORDER)
    assert [result.pipeline_name for result in cycle.pipeline_results] == list(PIPELINE_ORDER)
    assert [result.status for result in cycle.pipeline_results] == [
        "SUCCESS", "SUCCESS", "SUCCESS"
    ]
    assert cycle.mode == "FORCED"
    assert cycle.total_event_count == 0
    assert cycle.success_count == 3
    assert cycle.failure_count == 0
    assert cycle.skipped_count == 0
    assert {
        name: state.next_due_monotonic for name, state in runner.pipeline_states.items()
    } == before_due


def test_forced_run_once_only_executes_enabled_pipelines(tmp_path):
    enabled = {
        "log_event_detection": False,
        "metrics_threshold_detection": True,
        "metrics_iforest_detection": False,
    }
    config_path = _write_event_runner_config(
        tmp_path, _event_runner_config(tmp_path, enabled=enabled)
    )
    call_order = []
    threshold = RecordingPipeline("metrics_threshold_detection", call_order)
    disabled_log = RecordingPipeline("log_event_detection", call_order)
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides={
            "log_event_detection": disabled_log,
            "metrics_threshold_detection": threshold,
        },
    )

    cycle = runner.run_once()

    assert call_order == ["metrics_threshold_detection"]
    assert disabled_log.call_count == 0
    assert [result.pipeline_name for result in cycle.pipeline_results] == [
        "metrics_threshold_detection"
    ]
    assert cycle.success_count == 1
    assert cycle.skipped_count == 0


def test_forced_run_once_aggregates_events_and_counts_results(tmp_path):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    returns = {
        "log_event_detection": [{"event_type": "log-a"}, {"event_type": "log-b"}],
        "metrics_threshold_detection": [{"event_type": "threshold"}],
        "metrics_iforest_detection": [],
    }
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides=_recording_overrides(returns=returns),
    )

    cycle = runner.run_once()

    assert [result.event_count for result in cycle.pipeline_results] == [2, 1, 0]
    assert cycle.total_event_count == 3
    assert cycle.success_count == 3
    assert cycle.failure_count == cycle.skipped_count == 0
    assert all(result.duration_ms >= 0 for result in cycle.pipeline_results)
    assert all(result.error_type is None for result in cycle.pipeline_results)
    assert all(result.error_message is None for result in cycle.pipeline_results)
    assert all(result.scheduler_lag_ms is None for result in cycle.pipeline_results)


def test_metrics_threshold_and_iforest_use_or_acceptance_without_deduplication(tmp_path):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    threshold_event = {
        "event_type": "high_memory_detected",
        "service_name": "metrics",
        "confidence": 1.0,
    }
    iforest_event = {
        "event_type": "general_metrics_anomaly",
        "service_name": "metrics",
        "confidence": 0.6,
    }
    returns = {
        "metrics_threshold_detection": [threshold_event],
        "metrics_iforest_detection": [iforest_event],
    }
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides=_recording_overrides(returns=returns),
    )

    cycle = runner.run_once()

    assert cycle.total_event_count == 2
    assert cycle.pipeline_results[1].events == [threshold_event]
    assert cycle.pipeline_results[2].events == [iforest_event]
    assert cycle.pipeline_results[1].events[0] is threshold_event
    assert cycle.pipeline_results[2].events[0] is iforest_event


def test_forced_run_once_does_not_modify_event_dicts(tmp_path):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    event = {
        "event_id": "EVT-1",
        "event_type": "high_memory_detected",
        "severity": "HIGH",
        "confidence": 1.0,
        "status": "OPEN",
        "triggered_features": {"metric_name": "system_memory_usage_pct"},
    }
    original = deepcopy(event)
    returns = {"metrics_threshold_detection": [event]}
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides=_recording_overrides(returns=returns),
    )

    cycle = runner.run_once()

    assert event == original
    assert cycle.pipeline_results[1].events[0] is event
    assert set(event) == set(original)
    assert "runner_id" not in event
    assert "cycle_id" not in event
    assert "pipeline_status" not in event


class WriteRecordingStore:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)


class StoreOwningPipeline:
    def __init__(self, event, store):
        self.event = event
        self.store = store

    def run_once(self):
        self.store.write(self.event)
        return [self.event]


def test_unified_runner_does_not_write_detector_events_again(tmp_path):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    store = WriteRecordingStore()
    events = [{"event_type": name} for name in PIPELINE_ORDER]
    overrides = {
        name: StoreOwningPipeline(event, store)
        for name, event in zip(PIPELINE_ORDER, events)
    }
    runner = EventDetectionRunner(config_path, pipeline_overrides=overrides)

    cycle = runner.run_once()

    assert cycle.total_event_count == 3
    assert store.events == events
    assert len(store.events) == 3


def test_log_store_failure_is_failed_while_later_pipelines_continue(tmp_path):
    call_order = []

    class OrderedReader(FakeReader):
        def read_new_lines_once(self):
            call_order.append("log_event_detection")
            return super().read_new_lines_once()

    store = RecordingStore(
        error=OSError("log store unavailable token=storage-secret")
    )
    log_pipeline = _runner(
        OrderedReader([_line(0)]),
        builder=FakeBuilder(["failed-log-event"]),
        store=store,
    )
    threshold_event = {"event_type": "threshold-event"}
    iforest_event = {"event_type": "iforest-event"}
    overrides = {
        "log_event_detection": log_pipeline,
        "metrics_threshold_detection": RecordingPipeline(
            "metrics_threshold_detection",
            call_order,
            returned=[threshold_event],
        ),
        "metrics_iforest_detection": RecordingPipeline(
            "metrics_iforest_detection",
            call_order,
            returned=[iforest_event],
        ),
    }
    runner = EventDetectionRunner(
        _write_event_runner_config(tmp_path, _event_runner_config(tmp_path)),
        pipeline_overrides=overrides,
    )

    cycle = runner.run_once()

    assert call_order == list(PIPELINE_ORDER)
    failed = cycle.pipeline_results[0]
    assert failed.status == "FAILED"
    assert failed.events == []
    assert failed.event_count == 0
    assert failed.error_type == "OSError"
    assert failed.error_message == "log store unavailable token=[REDACTED]"
    assert "storage-secret" not in failed.error_message
    assert [result.status for result in cycle.pipeline_results[1:]] == [
        "SUCCESS",
        "SUCCESS",
    ]
    assert cycle.total_event_count == 2
    assert cycle.failure_count == 1
    assert cycle.success_count == 2
    assert cycle.pipeline_results[1].events[0] is threshold_event
    assert cycle.pipeline_results[2].events[0] is iforest_event
    assert store.write_count == 1
    assert log_pipeline._last_fired == {}


def test_runtime_exception_is_failed_and_remaining_pipelines_continue(
    tmp_path, caplog
):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    call_order = []
    errors = {"log_event_detection": RuntimeError("temporary log failure")}
    returns = {
        "metrics_threshold_detection": [{"event_type": "threshold"}],
        "metrics_iforest_detection": [{"event_type": "iforest"}],
    }
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides=_recording_overrides(
            returns=returns, errors=errors, call_order=call_order
        ),
    )

    with caplog.at_level("ERROR", logger="EventDetectionRunner"):
        cycle = runner.run_once()

    assert call_order == list(PIPELINE_ORDER)
    failed = cycle.pipeline_results[0]
    assert failed.status == "FAILED"
    assert failed.events == []
    assert failed.event_count == 0
    assert failed.error_type == "RuntimeError"
    assert failed.error_message == "temporary log failure"
    assert failed.duration_ms >= 0
    assert [result.status for result in cycle.pipeline_results[1:]] == [
        "SUCCESS", "SUCCESS"
    ]
    assert cycle.total_event_count == 2
    assert cycle.success_count == 2
    assert cycle.failure_count == 1
    assert cycle.skipped_count == 0
    assert "log_event_detection" in caplog.text


def test_two_runtime_failures_do_not_prevent_third_pipeline_success(tmp_path):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    errors = {
        "log_event_detection": RuntimeError("log failed"),
        "metrics_threshold_detection": ValueError("threshold failed"),
    }
    returns = {"metrics_iforest_detection": [{"event_type": "iforest"}]}
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides=_recording_overrides(returns=returns, errors=errors),
    )

    cycle = runner.run_once()

    assert [result.status for result in cycle.pipeline_results] == [
        "FAILED", "FAILED", "SUCCESS"
    ]
    assert cycle.total_event_count == 1
    assert cycle.success_count == 1
    assert cycle.failure_count == 2


@pytest.mark.parametrize("invalid", [None, (), "events", {}, ["not-a-dict"]])
def test_invalid_return_contract_fails_only_that_pipeline(tmp_path, invalid):
    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    call_order = []
    overrides = _recording_overrides(call_order=call_order)
    overrides["metrics_threshold_detection"].returned = invalid
    runner = EventDetectionRunner(config_path, pipeline_overrides=overrides)

    cycle = runner.run_once()

    assert call_order == list(PIPELINE_ORDER)
    assert [result.status for result in cycle.pipeline_results] == [
        "SUCCESS", "FAILED", "SUCCESS"
    ]
    failed = cycle.pipeline_results[1]
    assert failed.events == []
    assert failed.event_count == 0
    assert failed.error_type == "TypeError"
    assert cycle.success_count == 2
    assert cycle.failure_count == 1


def test_execute_pipeline_calculates_duration_and_optional_scheduler_lag(tmp_path):
    enabled = {
        "log_event_detection": True,
        "metrics_threshold_detection": False,
        "metrics_iforest_detection": False,
    }
    config_path = _write_event_runner_config(
        tmp_path, _event_runner_config(tmp_path, enabled=enabled)
    )
    clock_values = iter([10.0, 12.0, 12.25])
    pipeline = FakePipeline()
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides={"log_event_detection": pipeline},
        clock=lambda: next(clock_values),
    )

    result = runner._execute_pipeline(
        "log_event_detection", pipeline, scheduled_due=11.5
    )

    assert result.status == "SUCCESS"
    assert result.duration_ms == pytest.approx(250.0)
    assert result.scheduler_lag_ms == pytest.approx(500.0)


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ScheduledPipeline:
    def __init__(self, name, clock, call_order, *, durations=None, outcomes=None):
        self.name = name
        self.clock = clock
        self.call_order = call_order
        self.durations = list(durations or [])
        self.outcomes = list(outcomes or [])
        self.call_count = 0

    def run_once(self):
        self.call_count += 1
        self.call_order.append(self.name)
        if self.durations:
            self.clock.advance(self.durations.pop(0))
        outcome = self.outcomes.pop(0) if self.outcomes else []
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _scheduled_runner(tmp_path, clock, pipelines, *, enabled=None, sleeper=None):
    enabled = enabled or {name: name in pipelines for name in PIPELINE_ORDER}
    config_path = _write_event_runner_config(
        tmp_path, _event_runner_config(tmp_path, enabled=enabled)
    )
    return EventDetectionRunner(
        config_path,
        pipeline_overrides=pipelines,
        clock=clock,
        sleeper=sleeper,
    )


def test_default_scheduler_clock_uses_time_monotonic_and_not_wall_clock(
    tmp_path, monkeypatch
):
    clock = FakeClock()

    class ForbiddenDateTime:
        @classmethod
        def now(cls, *_args, **_kwargs):
            pytest.fail("Scheduler must not use datetime.now")

        @classmethod
        def utcnow(cls, *_args, **_kwargs):
            pytest.fail("Scheduler must not use datetime.utcnow")

    def forbidden_time_time():
        pytest.fail("Scheduler must not use time.time")

    monkeypatch.setattr(runner_module.time, "monotonic", clock)
    monkeypatch.setattr(runner_module.time, "time", forbidden_time_time)
    monkeypatch.setattr(runner_module, "datetime", ForbiddenDateTime)
    monkeypatch.setattr(runner_module, "_runner_timestamp", lambda: "wall-clock-only")

    config_path = _write_event_runner_config(tmp_path, _event_runner_config(tmp_path))
    runner = EventDetectionRunner(
        config_path,
        pipeline_overrides=_all_overrides(),
        sleeper=lambda _seconds: None,
    )

    assert runner.clock is clock
    assert clock.calls == 1
    assert {
        state.next_due_monotonic for state in runner.pipeline_states.values()
    } == {0.0}

    initial = runner.run_due_once()
    clock.advance(5.0)
    at_five = runner.run_due_once()

    assert [result.status for result in initial.pipeline_results] == [
        "SUCCESS", "SUCCESS", "SUCCESS"
    ]
    assert [result.status for result in at_five.pipeline_results] == [
        "SUCCESS", "SKIPPED_NOT_DUE", "SKIPPED_NOT_DUE"
    ]
    assert clock.calls > 1


def test_scheduler_initial_due_and_five_ten_fifteen_second_slots(tmp_path):
    clock = FakeClock()
    order = []
    pipelines = {
        name: ScheduledPipeline(name, clock, order) for name in PIPELINE_ORDER
    }
    runner = _scheduled_runner(tmp_path, clock, pipelines)

    initial = runner.run_due_once()
    clock.advance(5)
    at_five = runner.run_due_once()
    clock.advance(5)
    at_ten = runner.run_due_once()
    clock.advance(5)
    at_fifteen = runner.run_due_once()

    assert [result.status for result in initial.pipeline_results] == [
        "SUCCESS", "SUCCESS", "SUCCESS"
    ]
    assert [result.status for result in at_five.pipeline_results] == [
        "SUCCESS", "SKIPPED_NOT_DUE", "SKIPPED_NOT_DUE"
    ]
    assert [result.status for result in at_ten.pipeline_results] == [
        "SUCCESS", "SKIPPED_NOT_DUE", "SKIPPED_NOT_DUE"
    ]
    assert [result.status for result in at_fifteen.pipeline_results] == [
        "SUCCESS", "SUCCESS", "SUCCESS"
    ]
    assert order == [
        "log_event_detection",
        "metrics_threshold_detection",
        "metrics_iforest_detection",
        "log_event_detection",
        "log_event_detection",
        "log_event_detection",
        "metrics_threshold_detection",
        "metrics_iforest_detection",
    ]
    assert at_five.skipped_count == at_ten.skipped_count == 2
    assert all(
        result.duration_ms == 0.0 and result.events == [] and result.event_count == 0
        for result in at_five.pipeline_results
        if result.status == "SKIPPED_NOT_DUE"
    )


def test_scheduler_rechecks_clock_before_each_pipeline(tmp_path):
    clock = FakeClock()
    order = []
    pipelines = {
        "log_event_detection": ScheduledPipeline(
            "log_event_detection", clock, order, durations=[0.0, 1.0]
        ),
        "metrics_threshold_detection": ScheduledPipeline(
            "metrics_threshold_detection", clock, order
        ),
        "metrics_iforest_detection": ScheduledPipeline(
            "metrics_iforest_detection", clock, order
        ),
    }
    runner = _scheduled_runner(tmp_path, clock, pipelines)
    runner.run_due_once()
    order.clear()
    clock.value = 14.0

    cycle = runner.run_due_once()

    assert order == [
        "log_event_detection",
        "metrics_threshold_detection",
        "metrics_iforest_detection",
    ]
    assert [result.status for result in cycle.pipeline_results] == [
        "SUCCESS", "SUCCESS", "SUCCESS"
    ]
    assert cycle.pipeline_results[1].scheduler_lag_ms == pytest.approx(0.0)


def test_scheduler_lag_reflects_sequential_delay_and_is_never_negative(tmp_path):
    clock = FakeClock()
    order = []
    pipelines = {
        "log_event_detection": ScheduledPipeline(
            "log_event_detection", clock, order, durations=[2.0]
        ),
        "metrics_threshold_detection": ScheduledPipeline(
            "metrics_threshold_detection", clock, order
        ),
        "metrics_iforest_detection": ScheduledPipeline(
            "metrics_iforest_detection", clock, order
        ),
    }
    runner = _scheduled_runner(tmp_path, clock, pipelines)

    cycle = runner.run_due_once()

    assert cycle.pipeline_results[0].scheduler_lag_ms == 0.0
    assert cycle.pipeline_results[1].scheduler_lag_ms == pytest.approx(2000.0)
    assert cycle.pipeline_results[2].scheduler_lag_ms == pytest.approx(2000.0)
    assert all(result.scheduler_lag_ms >= 0 for result in cycle.pipeline_results)


def test_runtime_error_advances_schedule_and_retries_at_next_slot(tmp_path):
    clock = FakeClock()
    order = []
    pipeline = ScheduledPipeline(
        "log_event_detection",
        clock,
        order,
        outcomes=[RuntimeError("temporary"), [{"event_type": "recovered"}]],
    )
    runner = _scheduled_runner(
        tmp_path, clock, {"log_event_detection": pipeline}
    )

    failed = runner.run_due_once()
    not_due = runner.run_due_once()
    clock.advance(5)
    retried = runner.run_due_once()

    assert failed.pipeline_results[0].status == "FAILED"
    assert runner.pipeline_states["log_event_detection"].next_due_monotonic == 10.0
    assert not_due.pipeline_results[0].status == "SKIPPED_NOT_DUE"
    assert retried.pipeline_results[0].status == "SUCCESS"
    assert retried.total_event_count == 1
    assert pipeline.call_count == 2


def test_overrun_skips_past_slots_without_burst_catch_up(tmp_path, caplog):
    clock = FakeClock()
    order = []
    pipeline = ScheduledPipeline(
        "log_event_detection", clock, order, durations=[12.0]
    )
    runner = _scheduled_runner(
        tmp_path, clock, {"log_event_detection": pipeline}
    )

    with caplog.at_level("WARNING", logger="EventDetectionRunner"):
        overrun_cycle = runner.run_due_once()
    immediate_cycle = runner.run_due_once()

    assert overrun_cycle.pipeline_results[0].duration_ms == pytest.approx(12000.0)
    assert runner.pipeline_states["log_event_detection"].next_due_monotonic == 15.0
    assert immediate_cycle.pipeline_results[0].status == "SKIPPED_NOT_DUE"
    assert pipeline.call_count == 1
    assert "pipeline overrun" in caplog.text
    assert "interval_seconds=5.000" in caplog.text


def test_next_due_uses_previous_due_instead_of_completion_time(tmp_path):
    clock = FakeClock()
    pipeline = ScheduledPipeline(
        "log_event_detection", clock, [], durations=[1.0, 1.0]
    )
    runner = _scheduled_runner(
        tmp_path, clock, {"log_event_detection": pipeline}
    )

    runner.run_due_once()
    assert runner.pipeline_states["log_event_detection"].next_due_monotonic == 5.0
    clock.value = 5.0
    runner.run_due_once()

    assert clock.value == 6.0
    assert runner.pipeline_states["log_event_detection"].next_due_monotonic == 10.0


def test_due_cycle_logging_contains_pipeline_and_cycle_context(tmp_path, caplog):
    clock = FakeClock()
    pipeline = ScheduledPipeline(
        "log_event_detection",
        clock,
        [],
        outcomes=[[{"event_type": "event"}]],
    )
    runner = _scheduled_runner(
        tmp_path, clock, {"log_event_detection": pipeline}
    )

    with caplog.at_level("DEBUG", logger="EventDetectionRunner"):
        runner.run_due_once()

    assert "pipeline_name=log_event_detection" in caplog.text
    assert "status=SUCCESS" in caplog.text
    assert "event_count=1" in caplog.text
    assert "mode=DUE_ONLY" in caplog.text
    assert "total_event_count=1" in caplog.text


def test_start_repeats_due_cycles_and_uses_configured_tick(tmp_path):
    clock = FakeClock()
    sleep_calls = []
    runner = _scheduled_runner(
        tmp_path,
        clock,
        {"log_event_detection": ScheduledPipeline("log_event_detection", clock, [])},
    )
    cycle_calls = []

    def fake_due_cycle():
        cycle_calls.append("cycle")
        if len(cycle_calls) == 2:
            runner.stop()
        return None

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    runner.run_due_once = fake_due_cycle
    runner.sleeper = fake_sleep

    runner.start()

    assert cycle_calls == ["cycle", "cycle"]
    assert sleep_calls == [1.0]
    assert runner._stop_requested is True


def test_start_can_be_stopped_by_fake_sleeper_without_real_wait(tmp_path):
    clock = FakeClock()
    runner = _scheduled_runner(
        tmp_path,
        clock,
        {"log_event_detection": ScheduledPipeline("log_event_detection", clock, [])},
    )
    cycle_calls = []

    runner.run_due_once = lambda: cycle_calls.append("cycle")

    def stop_in_sleep(seconds):
        assert seconds == 1.0
        runner.stop()

    runner.sleeper = stop_in_sleep
    runner.start()

    assert cycle_calls == ["cycle"]


def test_keyboard_interrupt_requests_graceful_shutdown_without_escape(tmp_path, caplog):
    clock = FakeClock()
    runner = _scheduled_runner(
        tmp_path,
        clock,
        {"log_event_detection": ScheduledPipeline("log_event_detection", clock, [])},
    )
    runner.run_due_once = lambda: (_ for _ in ()).throw(KeyboardInterrupt())

    with caplog.at_level("INFO", logger="EventDetectionRunner"):
        runner.start()

    assert runner._stop_requested is True
    assert "interrupted" in caplog.text
    assert "stopped" in caplog.text


def test_stop_before_start_prevents_cycle_and_sleep(tmp_path):
    clock = FakeClock()
    runner = _scheduled_runner(
        tmp_path,
        clock,
        {"log_event_detection": ScheduledPipeline("log_event_detection", clock, [])},
        sleeper=lambda _seconds: pytest.fail("stopped runner must not sleep"),
    )
    runner.run_due_once = lambda: pytest.fail("stopped runner must not run a cycle")

    runner.stop()
    runner.start()


@pytest.mark.parametrize(
    "startup_error",
    [
        MetricsIForestModelNotFoundError("missing model"),
        MetricsIForestModelVersionError("bad metadata"),
    ],
)
def test_iforest_startup_model_errors_fail_fast(tmp_path, monkeypatch, startup_error):
    enabled = {
        "log_event_detection": False,
        "metrics_threshold_detection": False,
        "metrics_iforest_detection": True,
    }
    config_path = _write_event_runner_config(
        tmp_path, _event_runner_config(tmp_path, enabled=enabled)
    )

    def fail_factory(_path):
        raise startup_error

    monkeypatch.setitem(
        runner_module.DEFAULT_PIPELINE_FACTORIES,
        "metrics_iforest_detection",
        fail_factory,
    )

    with pytest.raises(type(startup_error), match=str(startup_error)):
        EventDetectionRunner(config_path)


def _empty_cli_cycle():
    return EventRunnerCycleResult(
        mode="FORCED",
        started_at="start",
        completed_at="end",
        pipeline_results=[],
        total_event_count=0,
        success_count=0,
        failure_count=0,
        skipped_count=0,
    )


def test_cli_once_returns_zero_and_prints_summary_for_zero_events(monkeypatch, capsys):
    calls = []

    class FakeCLIEventRunner:
        def __init__(self, config_path):
            calls.append(("init", config_path))

        def run_once(self):
            calls.append(("once",))
            return _empty_cli_cycle()

    monkeypatch.setattr(runner_module, "EventDetectionRunner", FakeCLIEventRunner)

    exit_code = main(["--config", "custom.yaml", "--once"])

    assert exit_code == 0
    assert calls == [("init", "custom.yaml"), ("once",)]
    output = capsys.readouterr().out
    assert "mode=FORCED" in output
    assert "events=0" in output


def test_cli_continuous_mode_calls_start_and_returns_zero(monkeypatch):
    calls = []

    class FakeCLIEventRunner:
        def __init__(self, config_path):
            calls.append(("init", config_path))

        def start(self):
            calls.append(("start",))

    monkeypatch.setattr(runner_module, "EventDetectionRunner", FakeCLIEventRunner)

    assert main(["--config", "continuous.yaml"]) == 0
    assert calls == [("init", "continuous.yaml"), ("start",)]


def test_cli_startup_error_returns_nonzero_without_escaping(monkeypatch, caplog):
    class FailingCLIEventRunner:
        def __init__(self, _config_path):
            raise FileNotFoundError("runner config missing")

    monkeypatch.setattr(runner_module, "EventDetectionRunner", FailingCLIEventRunner)

    with caplog.at_level("ERROR", logger="EventDetectionRunner"):
        exit_code = main(["--config", "missing.yaml", "--once"])

    assert exit_code == 1
    assert "runner config missing" in caplog.text
