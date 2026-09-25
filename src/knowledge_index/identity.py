"""Deterministic, type-separated identities for governed Knowledge."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from .contracts import (
    BuildIdentityInput,
    ChunkIdentityInput,
    GovernedManifest,
    IdentityNamespace,
    KnowledgeValidationError,
    _contains_secret_shape,
)


_PREFIXES = {
    IdentityNamespace.MANIFEST: "kmf",
    IdentityNamespace.DOCUMENT: "kdoc",
    IdentityNamespace.DOCUMENT_VERSION: "kver",
    IdentityNamespace.CHUNK: "kchk",
    IdentityNamespace.BUILD: "kbld",
}
_LOGICAL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
def _logical_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _LOGICAL_IDENTIFIER.fullmatch(value):
        raise KnowledgeValidationError(f"{field} must be a bounded canonical identifier")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value) or ".." in value.split("/"):
        raise KnowledgeValidationError(f"{field} cannot be an absolute or traversing path")
    if _contains_secret_shape(value, identity_value=True):
        raise KnowledgeValidationError(f"{field} must be non-secret")
    return value


def _reject_secret_identity_input(value: object, *, field_name: str | None = None) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _reject_secret_identity_input(
                getattr(value, field.name), field_name=field.name
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _contains_secret_shape(key, metadata_key=True):
                raise KnowledgeValidationError("identity input contains a secret-shaped key")
            _reject_secret_identity_input(item, field_name=key)
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            _reject_secret_identity_input(item)
        return
    if isinstance(value, str) and _contains_secret_shape(
        value,
        metadata_key=field_name == "key",
        identity_value=True,
    ):
        raise KnowledgeValidationError("identity input contains secret-shaped material")


def _canonical_value(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise KnowledgeValidationError("canonical mappings require string keys")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise KnowledgeValidationError("canonical values cannot contain non-finite numbers")
        return value
    raise KnowledgeValidationError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_serialize(value: object) -> str:
    """Serialize supported semantic values without environment-dependent inputs."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _identity(namespace: IdentityNamespace, payload: object) -> str:
    if not isinstance(namespace, IdentityNamespace):
        raise KnowledgeValidationError("namespace must be an IdentityNamespace")
    _reject_secret_identity_input(payload)
    envelope = {"namespace": namespace.value, "schema_version": "1.0", "payload": payload}
    digest = hashlib.sha256(canonical_serialize(envelope).encode("utf-8")).hexdigest()
    return f"{_PREFIXES[namespace]}_{digest}"


def _manifest_payload(manifest: GovernedManifest) -> dict[str, object]:
    if not isinstance(manifest, GovernedManifest):
        raise KnowledgeValidationError("manifest must be a GovernedManifest")
    documents = sorted(
        manifest.documents,
        key=lambda item: (item.document_id, item.document_version, item.source_path),
    )
    return {
        "schema_version": manifest.schema_version,
        "canonicalization_version": manifest.canonicalization_version,
        "manifest_id": manifest.manifest_id,
        "corpus_id": manifest.corpus_id,
        "corpus_version": manifest.corpus_version,
        "documents": documents,
    }


def manifest_commitment(manifest: GovernedManifest) -> str:
    return _identity(IdentityNamespace.MANIFEST, _manifest_payload(manifest))


def document_identity(corpus_id: str, logical_document_id: str) -> str:
    corpus_id = _logical_identifier(corpus_id, "corpus_id")
    logical_document_id = _logical_identifier(logical_document_id, "logical_document_id")
    return _identity(
        IdentityNamespace.DOCUMENT,
        {"corpus_id": corpus_id, "logical_document_id": logical_document_id},
    )


def document_version_identity(
    stable_document_identity: str, declared_version: str, content_hash: str
) -> str:
    if not re.fullmatch(r"kdoc_[0-9a-f]{64}", stable_document_identity):
        raise KnowledgeValidationError("stable_document_identity must use the document namespace")
    declared_version = _logical_identifier(declared_version, "declared_version")
    if not isinstance(content_hash, str) or not _CONTENT_HASH.fullmatch(content_hash):
        raise KnowledgeValidationError("content_hash must be a lowercase SHA-256 digest")
    return _identity(
        IdentityNamespace.DOCUMENT_VERSION,
        {
            "document_identity": stable_document_identity,
            "declared_version": declared_version,
            "content_hash": content_hash,
        },
    )


def chunk_identity(identity_input: ChunkIdentityInput) -> str:
    if not isinstance(identity_input, ChunkIdentityInput):
        raise KnowledgeValidationError("identity_input must be a ChunkIdentityInput")
    return _identity(IdentityNamespace.CHUNK, identity_input)


def build_identity(identity_input: BuildIdentityInput) -> str:
    if not isinstance(identity_input, BuildIdentityInput):
        raise KnowledgeValidationError("identity_input must be a BuildIdentityInput")
    return _identity(IdentityNamespace.BUILD, identity_input)
