"""Basic event-time Log Event Detection runner."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.event_detection.event.builder import EventBuilder
from src.event_detection.log.encoder import FeatureEncoder
from src.event_detection.log.features import FeatureExtractor
from src.event_detection.log.parser import LogParser
from src.event_detection.log.reader import LogReader
from src.event_detection.log.window import WindowFeatureAggregator
from src.event_detection.model.predictor import AnomalyPredictor
from src.event_detection.model.schema import WindowSummary
from src.event_detection.store.event_store import EventStore


def load_config(path: str = "configs/event_detection.yml") -> dict:
    import yaml

    with Path(path).open(encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


class LogEventDetectionRunner:
    """Coordinate parsing, event-time window inference, and persistence."""

    def __init__(self, config_path="configs/event_detection.yml", *, config=None,
                 reader=None, predictor=None, store=None):
        cfg = config if config is not None else load_config(config_path)
        self.config = cfg
        reader_cfg = cfg.get("log_reader", {})
        self.reader = reader or LogReader(
            reader_cfg.get("log_file_path", "logs/aiops.json.log"),
            reader_cfg.get("poll_interval_seconds", 5),
        )
        self.parser = LogParser()
        self.extractor = FeatureExtractor()
        self.encoder = FeatureEncoder(cfg.get("feature_extraction", {}))
        self.aggregator = WindowFeatureAggregator(**cfg["window"])
        self.predictor = predictor or AnomalyPredictor(cfg)
        self.builder = EventBuilder()
        self.store = store or EventStore(
            cfg.get("output", {}).get("event_store_path", "events/event_store.jsonl")
        )
        self.cooldown_seconds = float(cfg.get("event", {}).get("cooldown_seconds", 60))
        self._entries = []
        self._latest_event_time = None
        self._last_fired = {}

    def process_line(self, raw_line: str):
        """Process one line; return a persisted Event or ``None``."""
        entry = self.parser.parse(raw_line)
        if entry is None:
            return None

        # Keep Phase 1 components connected even though model inference is window-level.
        raw_features = self.extractor.extract_one(entry)
        self.encoder.encode(raw_features)
        event_time = entry["_parsed_timestamp"]
        if self._latest_event_time is None or event_time > self._latest_event_time:
            self._latest_event_time = event_time
        self._entries.append(entry)
        self._prune_window()

        if not self.aggregator.has_enough(self._entries):
            return None
        vector = self.aggregator.aggregate(
            self._entries, window_end=self._latest_event_time
        )
        prediction = self.predictor.predict_one(vector)
        if not prediction.is_anomaly:
            return None

        summary = self._compute_summary(self._entries)
        event = self.builder.build(prediction, summary)
        if event is None or self._in_cooldown(event["event_type"]):
            return None
        self.store.write(event)
        self._last_fired[event["event_type"]] = self._latest_event_time
        return event

    def start(self) -> None:
        """Load the model and process tailed lines until interrupted."""
        self.predictor.load()
        for raw_line in self.reader.tail():
            self.process_line(raw_line)

    def _prune_window(self):
        cutoff = self._latest_event_time - timedelta(seconds=self.aggregator.window_seconds)
        self._entries = [
            entry for entry in self._entries
            if cutoff <= entry["_parsed_timestamp"] <= self._latest_event_time
        ]

    def _in_cooldown(self, event_type):
        last = self._last_fired.get(event_type)
        return last is not None and (
            self._latest_event_time - last
        ).total_seconds() < self.cooldown_seconds

    @staticmethod
    def _compute_summary(logs):
        error_logs = [log for log in logs if str(log.get("level", "")).upper() == "ERROR"]
        warn_logs = [log for log in logs if str(log.get("level", "")).upper() == "WARN"]
        durations = [float(log.get("duration_ms") or 0) for log in logs]
        memories = [float(log.get("memory_usage_pct") or 0) for log in logs]
        ip_401 = Counter()
        target_429 = Counter()
        trace_services = defaultdict(set)
        trace_downstreams = {}
        downstream_services = defaultdict(set)
        external_failures = []
        for log in logs:
            status = int(log.get("status_code", 0))
            if status == 401 and log.get("source_ip"):
                ip_401[log["source_ip"]] += 1
            if status == 429 and log.get("target_service"):
                target_429[log["target_service"]] += 1
            if log in error_logs:
                trace_id = log.get("trace_id")
                if trace_id and log.get("service_name"):
                    trace_services[trace_id].add(log["service_name"])
                    if log.get("downstream_service"):
                        trace_downstreams.setdefault(trace_id, log["downstream_service"])
                downstream = log.get("downstream_service")
                if downstream and log.get("service_name"):
                    downstream_services[downstream].add(log["service_name"])
            if log.get("external_service") and status >= 500:
                external_failures.append(log)

        timestamps = [log["_parsed_timestamp"] for log in logs]
        samples = error_logs[:3] if error_logs else logs[:3]
        return WindowSummary(
            window_start=min(timestamps).isoformat().replace("+00:00", "Z"),
            window_end=max(timestamps).isoformat().replace("+00:00", "Z"),
            total_log_count=len(logs), error_count=len(error_logs), warn_count=len(warn_logs),
            unique_services=sorted({log.get("service_name") for log in logs if log.get("service_name")}),
            top_error_types=[name for name, _ in Counter(
                log.get("error_type") for log in logs if log.get("error_type")
            ).most_common(5)],
            max_duration_ms=max(durations, default=0),
            mean_duration_ms=sum(durations) / len(durations) if durations else 0,
            max_memory_pct=max(memories, default=0),
            source_ip_401_counts=dict(ip_401),
            trace_error_services={key: sorted(value) for key, value in trace_services.items()},
            trace_downstreams=trace_downstreams,
            downstream_error_services={key: sorted(value) for key, value in downstream_services.items()},
            target_429_counts=dict(target_429), external_failure_logs=external_failures,
            raw_log_sample=[{key: value for key, value in log.items() if not key.startswith("_")}
                            for log in samples],
        )


if __name__ == "__main__":
    LogEventDetectionRunner().start()
