"""Selector safety, redaction and Ground-Truth isolation boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contracts import EvidenceSource, SelectorFact
from .errors import EvidenceDomainError, EvidenceFailureKind


FORBIDDEN_PRODUCTION_FIELDS = frozenset(
    {
        "scenario_id",
        "generator_state",
        "generator_current_state",
        "validator_expected_answer",
        "expected_answer",
        "expected_root_cause",
        "expected_rca_cause",
        "expected_rca_class",
        "expected_event_type",
        "ground_truth",
        "evaluation_ground_truth",
        "evaluation_run_id",
        "answer_mapping",
    }
)

SENSITIVE_FIELD_NAMES = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "passwd",
        "secret",
        "cookie",
        "set_cookie",
        "client_secret",
    }
)

_LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sensitive_name_expression(name: str) -> str:
    """Map one normalized policy name to common free-text spellings."""
    return r"[\s_-]*".join(re.escape(part) for part in name.split("_"))


_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(
        _sensitive_name_expression(name)
        for name in sorted(SENSITIVE_FIELD_NAMES, key=len, reverse=True)
    )
    + r")(?![A-Za-z0-9])\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_SECRET_TEXT_PATTERNS = (
    _SENSITIVE_ASSIGNMENT,
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
)
_GROUND_TRUTH_TEXT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(
        _sensitive_name_expression(name)
        for name in sorted(FORBIDDEN_PRODUCTION_FIELDS, key=len, reverse=True)
    )
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def _normalized_key(value: object) -> str:
    return str(value).strip().lower().replace("-", "_")


def _unsafe(message: str, *, field_path: str | None = None) -> EvidenceDomainError:
    return EvidenceDomainError(
        EvidenceFailureKind.UNSAFE_EVIDENCE_CONTENT,
        message,
        field_path=field_path,
    )


def ensure_no_ground_truth_fields(value: object, *, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in FORBIDDEN_PRODUCTION_FIELDS:
                raise _unsafe(
                    f"forbidden production field: {normalized}",
                    field_path=f"{path}.{key}",
                )
            ensure_no_ground_truth_fields(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            ensure_no_ground_truth_fields(item, path=f"{path}[{index}]")


def _sensitive_query_key(key: str) -> bool:
    return _normalized_key(key) in SENSITIVE_FIELD_NAMES


def sanitize_url(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("URL must be a string")
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        netloc = netloc.rsplit("@", 1)[-1]
    filtered = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not _sensitive_query_key(key)
    ]
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, urlencode(filtered), "")
    )


def validate_safe_text(value: object, *, field_path: str) -> None:
    """Reject obvious secrets at authoritative bounded-text boundaries."""
    if not isinstance(value, str):
        raise TypeError(f"{field_path} must be a string")
    if _GROUND_TRUTH_TEXT_PATTERN.search(value):
        raise _unsafe(
            "Ground Truth markers are forbidden in production evidence text",
            field_path=field_path,
        )
    if any(pattern.search(value) for pattern in _SECRET_TEXT_PATTERNS):
        raise _unsafe(
            "secret-bearing content is forbidden in safe text",
            field_path=field_path,
        )
    for match in _URL_IN_TEXT.finditer(value):
        url = match.group(0).rstrip(".,;)")
        if sanitize_url(url) != url:
            raise _unsafe(
                "credential-bearing URLs are forbidden in safe text",
                field_path=field_path,
            )


def sanitize_provenance(value: object) -> object:
    """Return provenance with forbidden secrets removed, never merely hidden."""
    ensure_no_ground_truth_fields(value)
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_provenance(item)
            for key, item in value.items()
            if _normalized_key(key) not in SENSITIVE_FIELD_NAMES
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return tuple(sanitize_provenance(item) for item in value)
    if isinstance(value, str) and "://" in value:
        return sanitize_url(value)
    return value


def validate_safe_provenance(value: object, *, path: str = "root") -> None:
    ensure_no_ground_truth_fields(value, path=path)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _normalized_key(key) in SENSITIVE_FIELD_NAMES:
                raise _unsafe(
                    "secret-bearing fields are forbidden in provenance",
                    field_path=f"{path}.{key}",
                )
            validate_safe_provenance(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            validate_safe_provenance(item, path=f"{path}[{index}]")
    elif isinstance(value, str) and "://" in value and sanitize_url(value) != value:
        raise _unsafe(
            "secret-bearing URLs are forbidden in provenance", field_path=path
        )


def redact_sensitive_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Create a bounded-safe log/failure view; not evidence provenance."""
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if _normalized_key(key) in SENSITIVE_FIELD_NAMES:
            redacted[str(key)] = "[REDACTED]"
        elif isinstance(item, Mapping):
            redacted[str(key)] = redact_sensitive_mapping(item)
        elif isinstance(item, str) and "://" in item:
            redacted[str(key)] = sanitize_url(item)
        else:
            redacted[str(key)] = item
    return redacted


def _escape_loki(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_prometheus(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def build_selector_fact(
    source: EvidenceSource,
    field_name: str,
    value: str,
    *,
    selector_policy_version: str,
    allowlist: Mapping[EvidenceSource, frozenset[str]],
) -> SelectorFact:
    if not isinstance(source, EvidenceSource):
        raise TypeError("source must be an EvidenceSource")
    if not isinstance(field_name, str) or not _LABEL_NAME.fullmatch(field_name):
        raise _unsafe("selector field name is invalid", field_path="field_name")
    if field_name not in allowlist.get(source, frozenset()):
        raise _unsafe(
            "selector field is not allowed by the active policy",
            field_path="field_name",
        )
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _CONTROL_CHARACTERS.search(value)
    ):
        raise _unsafe("selector value is invalid", field_path=field_name)
    if _normalized_key(field_name) in SENSITIVE_FIELD_NAMES:
        raise _unsafe("secret-bearing selectors are forbidden", field_path=field_name)
    escape = _escape_loki if source is EvidenceSource.LOKI else _escape_prometheus
    return SelectorFact(
        source=source,
        field_name=field_name,
        normalized_value=value,
        escaped_value=escape(value),
        selector_policy_version=selector_policy_version,
    )


__all__ = [
    "FORBIDDEN_PRODUCTION_FIELDS",
    "SENSITIVE_FIELD_NAMES",
    "build_selector_fact",
    "ensure_no_ground_truth_fields",
    "redact_sensitive_mapping",
    "sanitize_provenance",
    "sanitize_url",
    "validate_safe_text",
    "validate_safe_provenance",
]
