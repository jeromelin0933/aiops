"""Deterministic semantic commitments for immutable Candidate-D values."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
import re


_COMMITMENT = re.compile(r"^[0-9a-f]{64}$")
_RESULT_ID = re.compile(r"^dvr_[0-9a-f]{64}$")


class IdentityContradiction(ValueError):
    """One stable identity was presented with different semantic content."""


def _plain(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    raise TypeError("semantic content contains an unsupported type")


def semantic_commitment(value: object) -> str:
    payload = json.dumps(_plain(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(("spec015-result-v1\n" + payload).encode("utf-8")).hexdigest()


def validated_result_id(commitment: str) -> str:
    if not isinstance(commitment, str) or not _COMMITMENT.fullmatch(commitment):
        raise ValueError("invalid semantic commitment")
    return "dvr_" + commitment


def require_equivalent_result(existing: object, candidate: object) -> object:
    if not all(hasattr(item, "validated_result_id") and hasattr(item, "semantic_commitment") for item in (existing, candidate)):
        raise TypeError("both values must be validated results")
    if not _RESULT_ID.fullmatch(existing.validated_result_id) or not _RESULT_ID.fullmatch(candidate.validated_result_id):
        raise ValueError("invalid validated result identity")
    if existing.validated_result_id != candidate.validated_result_id or existing != candidate:
        raise IdentityContradiction("validated result identity or content contradicts replay")
    return existing
