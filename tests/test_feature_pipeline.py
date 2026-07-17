from pathlib import Path

from src.event_detection.log.encoder import FeatureEncoder
from src.event_detection.log.features import FeatureExtractor
from src.event_detection.log.parser import LogParser
from src.event_detection.model.schema import EncodedFeatureVector, RawFeatures


FIXTURE = Path(__file__).parent / "fixtures" / "valid_logs.jsonl"
CONFIG = {
    "known_services": ["auth-service", "notification-service", "payment-service"],
    "known_error_types": [
        "AuthenticationFailed", "RateLimitExceeded", "OutOfMemoryError"
    ],
}


def _features(index):
    line = FIXTURE.read_text(encoding="utf-8").splitlines()[index]
    return FeatureExtractor().extract_one(LogParser().parse(line))


def test_extracts_401_features_and_raw_values():
    raw = _features(1)

    assert isinstance(raw, RawFeatures)
    assert raw.is_error and raw.is_4xx and raw.is_401
    assert not raw.is_429 and not raw.is_5xx
    assert raw.has_source_ip
    assert raw.raw_source_ip == "192.0.2.10"


def test_extracts_429_and_target_service_features():
    raw = _features(2)

    assert raw.is_429 and raw.is_4xx
    assert raw.has_target_service
    assert raw.raw_target_service == "sms-gateway"
    assert raw.rate_limit_quota == 100


def test_extracts_5xx_oom_and_dependency_features():
    raw = _features(3)

    assert raw.is_5xx and raw.is_oom
    assert raw.has_downstream_service
    assert raw.has_external_service
    assert raw.has_transaction_id
    assert raw.raw_downstream_service == "core-db"
    assert raw.raw_external_service == "bank-api"


def test_encoder_produces_stable_19_dimension_vector():
    encoded = FeatureEncoder(CONFIG).encode(_features(3))

    assert isinstance(encoded, EncodedFeatureVector)
    assert len(encoded.to_list()) == 19
    assert len(encoded.feature_names()) == 19
    assert encoded.feature_names()[0] == "status_code"
    assert encoded.feature_names()[-1] == "is_oom"
    assert encoded.to_list()[0] == encoded.status_code
    assert encoded.to_list()[-1] == encoded.is_oom
    assert encoded.service_name_encoded == 3.0
    assert encoded.error_type_encoded == 3.0
    assert encoded.level_encoded == 3.0


def test_encoder_maps_unknown_categories_and_level_to_defaults():
    raw = RawFeatures(service_name="new-service", error_type="NewError", level="DEBUG")
    encoded = FeatureEncoder(CONFIG).encode(raw)

    assert encoded.service_name_encoded == 0.0
    assert encoded.error_type_encoded == 0.0
    assert encoded.level_encoded == 1.0


def test_extractor_uses_null_fallbacks():
    raw = FeatureExtractor().extract_one({
        "timestamp": "2026-07-14T10:00:00Z",
        "level": None,
        "service_name": None,
        "status_code": 200,
        "duration_ms": 0,
        "error_type": None,
        "memory_usage_pct": None,
        "rate_limit_quota": None,
    })

    assert raw.level == "INFO"
    assert raw.service_name == "unknown"
    assert raw.error_type == "unknown"
    assert raw.memory_usage_pct == 0.0
    assert raw.rate_limit_quota == 0
