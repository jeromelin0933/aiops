"""Fail-closed security checks before Knowledge can cross a provider boundary."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .contracts import (
    AdmissionFailureCode,
    AdmissionFailureFact,
    MetadataItem,
    SourceClassification,
    _contains_secret_shape,
)


_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:access[_-]?token|api[_-]?key|authorization|credential|password|passwd|secret|token)\s*(?:=|:)\s*\S+"
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
    r"|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
)
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_REDACTED = "[REDACTED]"
_FORBIDDEN_LOCATOR_PART = re.compile(
    r"(?i)^(?:scenarios?|ground[_-]?truth|fixtures?|validator(?:[_-]?output)?|evaluation(?:[_-]?truth)?|"
    r"generated[_-]?rca|rca[_-]?output|shadows?|unreviewed[_-]?incidents?)$"
)


def sanitize_failure_detail(message: object, *, maximum_length: int = 256) -> str:
    """Return bounded diagnostic text with common secret shapes removed."""
    text = message if isinstance(message, str) else type(message).__name__
    text = _SENSITIVE_VALUE.sub(_REDACTED, text)
    text = _CONTROL_CHARACTER.sub("?", text).strip()
    if not text:
        text = "validation failure"
    return text[:maximum_length]


def validate_source_classification(
    classification: SourceClassification,
) -> AdmissionFailureFact | None:
    if classification is SourceClassification.APPROVED_OPERATIONAL_KNOWLEDGE:
        return None
    return AdmissionFailureFact(
        AdmissionFailureCode.SOURCE_CLASS_FORBIDDEN,
        "source_classification",
        "source classification is not admissible for production Knowledge",
    )


def validate_source_locator(source_path: str) -> AdmissionFailureFact | None:
    """Reject locators that identify evaluation or foreign-authority roots."""
    normalized_parts = tuple(part for part in source_path.replace("\\", "/").split("/") if part)
    if any(_FORBIDDEN_LOCATOR_PART.fullmatch(part.rsplit(".", 1)[0]) for part in normalized_parts):
        return AdmissionFailureFact(
            AdmissionFailureCode.SOURCE_CLASS_FORBIDDEN,
            "source_path",
            "source locator belongs to a forbidden production Knowledge class",
        )
    return None


def validate_metadata_security(
    metadata: Iterable[MetadataItem],
) -> AdmissionFailureFact | None:
    for item in metadata:
        if _contains_secret_shape(item.key, metadata_key=True) or _contains_secret_shape(
            item.value
        ):
            return AdmissionFailureFact(
                AdmissionFailureCode.SECRET_METADATA,
                "metadata",
                "metadata contains a forbidden secret-shaped field",
            )
    return None


def preflight_outbound_content(
    content: str, *, maximum_utf8_bytes: int = 1_000_000
) -> AdmissionFailureFact | None:
    if not isinstance(content, str):
        return AdmissionFailureFact(
            AdmissionFailureCode.OUTBOUND_CONTENT_UNSAFE,
            "content",
            "source content is not valid UTF-8 text",
        )
    if len(content.encode("utf-8")) > maximum_utf8_bytes:
        return AdmissionFailureFact(
            AdmissionFailureCode.OUTBOUND_CONTENT_UNSAFE,
            "content",
            "source content exceeds the bounded outbound size",
        )
    if _CONTROL_CHARACTER.search(content) or _contains_secret_shape(content):
        return AdmissionFailureFact(
            AdmissionFailureCode.OUTBOUND_CONTENT_UNSAFE,
            "content",
            "source content failed outbound security preflight",
        )
    return None
