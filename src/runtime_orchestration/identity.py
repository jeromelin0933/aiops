"""Deterministic Runtime work and operation identities."""

from __future__ import annotations

import hashlib
from enum import Enum


def _stable_identity(prefix: str, namespace: str, references: tuple[object, ...]) -> str:
    if not isinstance(namespace, str) or not namespace or namespace != namespace.strip():
        raise ValueError("identity namespace must be a non-empty, trimmed string")
    if not references:
        raise ValueError("at least one stable reference is required")
    encoded: list[str] = [f"{len(namespace)}:{namespace}"]
    for reference in references:
        if isinstance(reference, Enum):
            reference = reference.value
        if not isinstance(reference, str) or not reference or reference != reference.strip():
            raise ValueError("identity references must be non-empty, trimmed strings")
        encoded.append(f"{len(reference)}:{reference}")
    digest = hashlib.sha256("|".join(encoded).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def runtime_work_id(work_kind: object, *stable_references: object) -> str:
    return _stable_identity("rtw", str(getattr(work_kind, "value", work_kind)), stable_references)


def runtime_operation_id(operation_scope: str, *stable_references: object) -> str:
    return _stable_identity("rto", operation_scope, stable_references)


def automatic_assignment_operation_id(incident_id: str, automation_actor: str) -> str:
    """Stable workflow identity for one Incident's initial automatic assignment."""
    return _stable_identity("wfo", "AUTO_ASSIGN", (incident_id, automation_actor))
