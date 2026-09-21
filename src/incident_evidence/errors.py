"""Typed SPEC-013 Candidate-B failure contracts."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping


class RetryDisposition(str, Enum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class EvidenceFailureKind(str, Enum):
    INVALID_CAPTURE_COMMAND = "INVALID_CAPTURE_COMMAND"
    INCIDENT_NOT_FOUND = "INCIDENT_NOT_FOUND"
    MALFORMED_INCIDENT_STATE = "MALFORMED_INCIDENT_STATE"
    INCIDENT_CHANGED_DURING_CAPTURE = "INCIDENT_CHANGED_DURING_CAPTURE"
    REFERENCED_EVENT_NOT_FOUND = "REFERENCED_EVENT_NOT_FOUND"
    DUPLICATE_EVENT_ID = "DUPLICATE_EVENT_ID"
    CONTRADICTORY_EVENT_IDENTITY = "CONTRADICTORY_EVENT_IDENTITY"
    INVALID_REQUIRED_EVENT = "INVALID_REQUIRED_EVENT"
    AUTHORITATIVE_EVENT_ENUMERATION_FAILURE = (
        "AUTHORITATIVE_EVENT_ENUMERATION_FAILURE"
    )
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_INVALID = "SOURCE_INVALID"
    UNSAFE_EVIDENCE_CONTENT = "UNSAFE_EVIDENCE_CONTENT"
    CAPTURE_OPERATION_CONFLICT = "CAPTURE_OPERATION_CONFLICT"
    CONTRADICTORY_REPLAY = "CONTRADICTORY_REPLAY"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    UNSUPPORTED_EVIDENCE_SCHEMA = "UNSUPPORTED_EVIDENCE_SCHEMA"
    EVIDENCE_STORE_INTEGRITY_FAILURE = "EVIDENCE_STORE_INTEGRITY_FAILURE"
    DANGLING_EVIDENCE_REFERENCE = "DANGLING_EVIDENCE_REFERENCE"
    UNSUPPORTED_MATERIALITY_RULE = "UNSUPPORTED_MATERIALITY_RULE"
    MATERIALITY_REPAIR_REQUIRED = "MATERIALITY_REPAIR_REQUIRED"
    TRANSIENT_EVIDENCE_STORE_FAILURE = "TRANSIENT_EVIDENCE_STORE_FAILURE"


DEFAULT_RETRY_DISPOSITIONS: Mapping[EvidenceFailureKind, RetryDisposition] = (
    MappingProxyType(
        {
            EvidenceFailureKind.INVALID_CAPTURE_COMMAND: RetryDisposition.NON_RETRYABLE,
            EvidenceFailureKind.INCIDENT_NOT_FOUND: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.MALFORMED_INCIDENT_STATE: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.INCIDENT_CHANGED_DURING_CAPTURE: RetryDisposition.RETRYABLE,
            EvidenceFailureKind.REFERENCED_EVENT_NOT_FOUND: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.DUPLICATE_EVENT_ID: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.CONTRADICTORY_EVENT_IDENTITY: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.INVALID_REQUIRED_EVENT: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.AUTHORITATIVE_EVENT_ENUMERATION_FAILURE: RetryDisposition.RETRYABLE,
            EvidenceFailureKind.SOURCE_UNAVAILABLE: RetryDisposition.RETRYABLE,
            EvidenceFailureKind.SOURCE_INVALID: RetryDisposition.NON_RETRYABLE,
            EvidenceFailureKind.UNSAFE_EVIDENCE_CONTENT: RetryDisposition.NON_RETRYABLE,
            EvidenceFailureKind.CAPTURE_OPERATION_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.CONTRADICTORY_REPLAY: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.MIGRATION_REQUIRED: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.EVIDENCE_STORE_INTEGRITY_FAILURE: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.DANGLING_EVIDENCE_REFERENCE: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.UNSUPPORTED_MATERIALITY_RULE: RetryDisposition.NON_RETRYABLE,
            EvidenceFailureKind.MATERIALITY_REPAIR_REQUIRED: RetryDisposition.REPAIR_REQUIRED,
            EvidenceFailureKind.TRANSIENT_EVIDENCE_STORE_FAILURE: RetryDisposition.RETRYABLE,
        }
    )
)


class EvidenceDomainError(ValueError):
    """A typed, bounded Candidate-B domain failure."""

    def __init__(
        self,
        kind: EvidenceFailureKind,
        message: str,
        *,
        retry_disposition: RetryDisposition | None = None,
        field_path: str | None = None,
    ) -> None:
        if not isinstance(kind, EvidenceFailureKind):
            raise TypeError("kind must be an EvidenceFailureKind")
        if not isinstance(message, str) or not message:
            raise TypeError("message must be a non-empty string")
        if retry_disposition is not None and not isinstance(
            retry_disposition, RetryDisposition
        ):
            raise TypeError("retry_disposition must be a RetryDisposition")
        super().__init__(message)
        self.kind = kind
        self.retry_disposition = (
            retry_disposition or DEFAULT_RETRY_DISPOSITIONS[kind]
        )
        self.field_path = field_path


def invalid_capture_command(message: str, *, field_path: str) -> EvidenceDomainError:
    return EvidenceDomainError(
        EvidenceFailureKind.INVALID_CAPTURE_COMMAND,
        message,
        field_path=field_path,
    )


__all__ = [
    "DEFAULT_RETRY_DISPOSITIONS",
    "EvidenceDomainError",
    "EvidenceFailureKind",
    "RetryDisposition",
    "invalid_capture_command",
]
