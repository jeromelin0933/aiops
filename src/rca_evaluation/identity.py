"""Domain-separated, deterministic evaluation identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


DOMAINS = frozenset({
    "oracle_revision", "evaluation_plan", "evaluation_run", "execution",
    "observation", "judgment", "review", "re_adjudication", "report",
})


def canonical_bytes(value: object) -> bytes:
    """Strict UTF-8 JSON: no floats, coercion, duplicate keys, or object fallback."""
    def check(item: object) -> None:
        if item is None or type(item) in (str, int, bool):
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                check(child)
            return
        if isinstance(item, Mapping) and all(type(k) is str for k in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("semantic content must contain only JSON values without floats")

    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def commitment(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def identity(domain: str, semantic_key: object) -> str:
    if domain not in DOMAINS:
        raise ValueError("unknown evaluation identity domain")
    digest = hashlib.sha256(canonical_bytes(["SPEC-017", domain, semantic_key])).hexdigest()
    return f"eval:{domain}:{digest}"


def require_identity(value: str, domain: str) -> str:
    if not isinstance(value, str) or not value.startswith(f"eval:{domain}:") or len(value) != len(f"eval:{domain}:") + 64:
        raise ValueError(f"expected {domain} identity")
    try:
        int(value.rsplit(":", 1)[1], 16)
    except ValueError as exc:
        raise ValueError(f"expected {domain} identity") from exc
    return value
