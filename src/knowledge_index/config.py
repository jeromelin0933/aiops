"""Strict non-secret configuration for the SPEC-014 local workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping

import yaml

from .chunking import ChunkingProfile
from .contracts import (
    ApplicabilityPolicy,
    OpaqueExternalReference,
    OpaqueReferenceType,
    ProviderCapability,
    ProviderInvocationLimits,
    RetrievalProfile,
    RetrievalQueryDisposition,
    RetrievalScoreDirection,
)


class KnowledgeConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GeminiEmbedding2Profile:
    """Offline-selectable embedding commitment; no retrieval or activation claim."""

    version: str = "1.0"
    capability_identity: str = "spec014-google-gemini-embedding-2-capability-v1"
    embedding_profile_identity: str = "spec014-gemini-embedding-2-embedding-v1"
    provider: str = "google"
    model: str = "gemini-embedding-2"
    api_version: str = "v1"
    sdk_baseline: str = "google-genai==2.25.0"
    dimension: int = 768
    document_envelope: str = "title: {approved document_id} | text: {chunk_content}"
    query_envelope: str = "task: search result | query: {canonical_query}"
    normalization: str = "provider-normalized-conditional-v1"
    bounds_policy: str = "verified-token-and-serialized-payload-v1"
    retry_policy: str = "one-sync-attempt-30s-v1"
    normalization_tolerance: float | None = None
    retrieval_calibrated: bool = False
    applicability_calibrated: bool = False


GEMINI_EMBEDDING2_PROFILE = GeminiEmbedding2Profile()


def select_gemini_embedding2_capability(profile_reference: OpaqueExternalReference) -> ProviderCapability:
    """Explicit offline selection; tracked Production config stays on its historical model."""
    profile = GEMINI_EMBEDDING2_PROFILE
    return ProviderCapability(
        profile_reference, profile.capability_identity, profile.provider,
        profile.model, profile.embedding_profile_identity, profile.dimension, True,
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise KnowledgeConfigError(f"{label} must be a string mapping")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise KnowledgeConfigError(f"{label} keys are incomplete or unsupported")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise KnowledgeConfigError(f"{label} must be non-empty text")
    if any(token in label.lower() for token in ("secret", "password", "api_key", "token")):
        raise KnowledgeConfigError("secret configuration is forbidden")
    return value


def _path(value: Any, label: str) -> str:
    text = _text(value, label)
    path = PurePath(text)
    if path.is_absolute() or ".." in path.parts:
        raise KnowledgeConfigError(f"{label} must be repo-relative")
    return text


@dataclass(frozen=True, slots=True)
class KnowledgeIndexConfig:
    version: str
    manifest_path: str
    authority_store_path: str
    capability: ProviderCapability
    limits: ProviderInvocationLimits
    normalization_semantics: str
    chunking: ChunkingProfile
    chroma_path: str
    index_schema_identity: str
    distance_metric: str
    retrieval_profile: RetrievalProfile
    applicability_policy: ApplicabilityPolicy


def load_knowledge_config(path: str | Path) -> KnowledgeIndexConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise KnowledgeConfigError("knowledge config is unreadable") from exc
    root = _mapping(raw, "knowledge config")
    _keys(root, {"version", "manifest", "authority_store", "embedding", "chunking", "index", "retrieval", "applicability"}, "knowledge config")
    if root["version"] != "1.0":
        raise KnowledgeConfigError("unsupported knowledge config version")

    authority = _mapping(root["authority_store"], "authority_store")
    _keys(authority, {"adapter", "path"}, "authority_store")
    if authority["adapter"] != "sqlite3":
        raise KnowledgeConfigError("authority store must be sqlite3")

    embedding = _mapping(root["embedding"], "embedding")
    _keys(embedding, {
        "provider", "model", "profile_reference", "capability_identity",
        "embedding_profile_identity", "dimension", "normalization_semantics",
        "hidden_retries_disabled", "limits",
    }, "embedding")
    gemini = embedding["model"] == GEMINI_EMBEDDING2_PROFILE.model
    if embedding["provider"] != "google" or embedding["model"] not in ("text-embedding-004", GEMINI_EMBEDDING2_PROFILE.model) or embedding["dimension"] != 768 or embedding["hidden_retries_disabled"] is not True:
        raise KnowledgeConfigError("embedding must select an approved Google model, dimension 768, without hidden retry")
    if gemini and (embedding["capability_identity"] != GEMINI_EMBEDDING2_PROFILE.capability_identity
                  or embedding["embedding_profile_identity"] != GEMINI_EMBEDDING2_PROFILE.embedding_profile_identity
                  or embedding["normalization_semantics"] != GEMINI_EMBEDDING2_PROFILE.normalization):
        raise KnowledgeConfigError("Gemini2 capability/profile/normalization selection must be exact")
    limits = _mapping(embedding["limits"], "embedding.limits")
    _keys(limits, {
        "timeout_seconds", "maximum_request_bytes", "maximum_batch_items",
        "maximum_invocations", "maximum_cost_units", "maximum_rate_units",
        "maximum_quota_units", "maximum_resource_units",
    }, "embedding.limits")
    invocation_limits = ProviderInvocationLimits(**limits)
    if ((not gemini and invocation_limits.maximum_invocations != 1)
            or (gemini and (invocation_limits.maximum_invocations > 54
                            or invocation_limits.timeout_seconds != 30))):
        raise KnowledgeConfigError("historical invocation count is one; Gemini2 requires bounded <=54 invocations and 30 seconds")
    profile_reference = OpaqueExternalReference(OpaqueReferenceType.CREDENTIAL_PROFILE, _text(embedding["profile_reference"], "profile_reference"))
    capability = ProviderCapability(
        profile_reference, _text(embedding["capability_identity"], "capability_identity"),
        "google", embedding["model"], _text(embedding["embedding_profile_identity"], "embedding_profile_identity"),
        768, True,
    )

    chunking = _mapping(root["chunking"], "chunking")
    _keys(chunking, {"profile_identity", "maximum_chunk_bytes"}, "chunking")
    chunk_profile = ChunkingProfile(_text(chunking["profile_identity"], "chunking.profile_identity"), chunking["maximum_chunk_bytes"])

    index = _mapping(root["index"], "index")
    _keys(index, {"engine", "schema_identity", "distance_metric", "path"}, "index")
    if index["engine"] != "chromadb":
        raise KnowledgeConfigError("index engine must be chromadb")
    if index["distance_metric"] != "l2":
        raise KnowledgeConfigError("index.distance_metric must be the approved l2 metric")

    retrieval = _mapping(root["retrieval"], "retrieval")
    _keys(retrieval, {
        "profile_identity", "version", "canonicalization_version", "allowed_filter_keys",
        "top_k", "candidate_limit", "score_direction", "score_precision",
        "max_query_bytes", "max_content_bytes_per_result", "max_metadata_bytes_per_result",
        "max_total_payload_bytes",
    }, "retrieval")
    retrieval_profile = RetrievalProfile(
        _text(retrieval["profile_identity"], "retrieval.profile_identity"), retrieval["version"],
        retrieval["canonicalization_version"], capability.embedding_profile_identity,
        capability.provider, capability.model, capability.embedding_dimension,
        "chromadb", index["schema_identity"], retrieval["max_query_bytes"],
        tuple(retrieval["allowed_filter_keys"]), retrieval["top_k"], retrieval["candidate_limit"],
        RetrievalScoreDirection(retrieval["score_direction"]), retrieval["score_precision"],
        retrieval["max_content_bytes_per_result"], retrieval["max_metadata_bytes_per_result"],
        retrieval["max_total_payload_bytes"], RetrievalQueryDisposition.INVALID,
        RetrievalQueryDisposition.INVALID,
    )

    applicability = _mapping(root["applicability"], "applicability")
    _keys(applicability, {"policy_identity", "version", "required_filter_keys", "direct_threshold", "partial_threshold", "contextual_threshold"}, "applicability")
    policy = ApplicabilityPolicy(
        applicability["policy_identity"], applicability["version"],
        tuple(applicability["required_filter_keys"]), applicability["direct_threshold"],
        applicability["partial_threshold"], applicability["contextual_threshold"],
    )
    return KnowledgeIndexConfig(
        "1.0", _path(root["manifest"], "manifest"), _path(authority["path"], "authority_store.path"),
        capability, invocation_limits, embedding["normalization_semantics"], chunk_profile,
        _path(index["path"], "index.path"), index["schema_identity"],
        index["distance_metric"], retrieval_profile, policy,
    )
