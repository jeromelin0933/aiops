"""Build PRD-002 events from anomalous log windows."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from src.event_detection.model.predictor import PredictionResult
from src.event_detection.model.schema import WindowSummary


EVENT_FIELDS = {
    "event_id", "detected_at", "event_source", "event_type",
    "detection_method", "severity", "confidence", "service_name",
    "trace_id", "source_ip", "downstream_service", "external_service",
    "status", "triggered_features", "raw_log_sample",
}

SEVERITY_MAP = {
    "oom_crash_detected": "CRITICAL",
    "downstream_cascade_failure": "CRITICAL",
    "brute_force_detected": "CRITICAL",
    "rate_limit_storm": "HIGH",
    "external_dependency_failure": "HIGH",
    "cross_service_failure": "HIGH",
    "general_log_anomaly": "MEDIUM",
}


class EventBuilder:
    """Classify one anomalous window and create exactly one standard event."""

    def build(
        self, prediction: PredictionResult, summary: WindowSummary
    ) -> Optional[dict]:
        if not prediction.is_anomaly:
            return None

        event_type, context = self._infer_event_type(summary)
        event = {
            "event_id": self._make_event_id(),
            "detected_at": self._now_iso(),
            "event_source": "log_event_detection",
            "event_type": event_type,
            "detection_method": "isolation_forest",
            "severity": SEVERITY_MAP[event_type],
            "confidence": prediction.confidence,
            "service_name": self._pick_service(summary, event_type, context),
            "trace_id": context.get("trace_id"),
            "source_ip": context.get("source_ip"),
            "downstream_service": context.get("downstream_service"),
            "external_service": context.get("external_service"),
            "status": "OPEN",
            "triggered_features": self._triggered_features(
                prediction, summary, event_type, context
            ),
            "raw_log_sample": [
                {key: value for key, value in log.items() if not key.startswith("_")}
                for log in summary.raw_log_sample[:3]
            ],
        }
        assert set(event) == EVENT_FIELDS
        return event

    def _infer_event_type(self, summary: WindowSummary) -> tuple[str, dict]:
        # Fixed priority: S3 -> S5 -> S1 -> S6 -> S4 -> S2 -> fallback.
        if "OutOfMemoryError" in summary.top_error_types:
            return "oom_crash_detected", {}

        downstream = self._first_matching(
            summary.downstream_error_services,
            lambda services: len(set(services)) >= 5,
        )
        if downstream is not None:
            return "downstream_cascade_failure", {"downstream_service": downstream}

        source_ip = self._first_matching(
            summary.source_ip_401_counts, lambda count: count >= 10
        )
        if source_ip is not None:
            return "brute_force_detected", {"source_ip": source_ip}

        target = self._first_matching(
            summary.target_429_counts, lambda count: count >= 20
        )
        if target is not None:
            return "rate_limit_storm", {"target_service": target}

        if summary.external_failure_logs:
            log = summary.external_failure_logs[0]
            return "external_dependency_failure", {
                "external_service": log.get("external_service")
            }

        trace_id = self._first_matching(
            summary.trace_error_services, lambda services: len(set(services)) >= 2
        )
        if trace_id is not None:
            return "cross_service_failure", {
                "trace_id": trace_id,
                "downstream_service": summary.trace_downstreams.get(trace_id),
            }
        return "general_log_anomaly", {}

    @staticmethod
    def _first_matching(values: dict, predicate):
        for key in sorted(values):
            if predicate(values[key]):
                return key
        return None

    @staticmethod
    def _pick_service(summary, event_type, context):
        if event_type == "downstream_cascade_failure":
            return "multiple"
        if event_type == "cross_service_failure":
            services = summary.trace_error_services.get(context.get("trace_id"), [])
            return sorted(services)[0] if services else "unknown"
        return sorted(summary.unique_services)[0] if summary.unique_services else "unknown"

    @staticmethod
    def _triggered_features(prediction, summary, event_type, context):
        features = {
            "anomaly_score": prediction.anomaly_score,
            "window_log_count": summary.total_log_count,
            "error_count": summary.error_count,
            "max_duration_ms": summary.max_duration_ms,
            "mean_duration_ms": summary.mean_duration_ms,
        }
        if event_type == "oom_crash_detected":
            features["max_memory_pct"] = summary.max_memory_pct
        elif event_type == "downstream_cascade_failure":
            downstream = context["downstream_service"]
            services = sorted(set(summary.downstream_error_services[downstream]))
            features.update(common_downstream=downstream,
                            affected_service_count=len(services),
                            affected_services=services)
        elif event_type == "brute_force_detected":
            ip = context["source_ip"]
            features.update(attacker_ip=ip,
                            failed_attempt_count=summary.source_ip_401_counts[ip])
        elif event_type == "rate_limit_storm":
            target = context["target_service"]
            features.update(target_service=target,
                            rate_limit_count=summary.target_429_counts[target])
        elif event_type == "external_dependency_failure":
            features["external_service"] = context["external_service"]
        elif event_type == "cross_service_failure":
            trace_id = context["trace_id"]
            features.update(trace_id=trace_id,
                            affected_services=sorted(set(summary.trace_error_services[trace_id])))
        return features

    @staticmethod
    def _make_event_id() -> str:
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        return f"EVT-{timestamp}-{uuid.uuid4().hex[:4]}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
