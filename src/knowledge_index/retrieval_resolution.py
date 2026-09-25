"""Pure, bounded, deterministic retrieval resolution."""

from __future__ import annotations

import hashlib

from .contracts import (
    ApplicabilityPolicy,
    FrozenRetrievalOperation,
    OrderedRetrievalCandidate,
    RawRetrievalBatch,
    RetrievalApplicability,
    RetrievalFailureCode,
    RetrievalFailureFact,
    RetrievalOperationRequest,
    RetrievalResolution,
    RetrievalResult,
    RetrievalScoreDirection,
    RetrySafetyDisposition,
    StagedBuildRecord,
)
from .identity import canonical_serialize


def derive_retrieval_operation_commitment(request: RetrievalOperationRequest) -> str:
    envelope = {
        "namespace": "KNOWLEDGE_RETRIEVAL_OPERATION",
        "version": "1.0",
        "request": request,
    }
    return hashlib.sha256(canonical_serialize(envelope).encode("utf-8")).hexdigest()


def _failure(
    frozen: FrozenRetrievalOperation,
    code: RetrievalFailureCode,
    field: str,
    detail: str,
    resolution: RetrievalResolution = RetrievalResolution.INVALID,
) -> RetrievalResult:
    return RetrievalResult(
        frozen.request.operation_key,
        frozen.frozen_build_identity,
        resolution,
        failures=(RetrievalFailureFact(
            code, field, detail, RetrySafetyDisposition.DO_NOT_RETRY
        ),),
    )


def _applicability(
    score: float,
    metadata: dict[str, str],
    query_filters: dict[str, str],
    policy: ApplicabilityPolicy,
    direction: RetrievalScoreDirection,
) -> RetrievalApplicability:
    # A score is never sufficient: every approved predicate must be present in
    # both the query and governed document metadata and must agree exactly.
    if any(
        key not in query_filters or metadata.get(key) != query_filters[key]
        for key in policy.required_filter_keys
    ):
        return RetrievalApplicability.NONE
    if direction is RetrievalScoreDirection.HIGHER_IS_BETTER:
        if score >= policy.direct_threshold:
            return RetrievalApplicability.DIRECT
        if score >= policy.partial_threshold:
            return RetrievalApplicability.PARTIAL
        if score >= policy.contextual_threshold:
            return RetrievalApplicability.CONTEXTUAL
    else:
        if score <= policy.direct_threshold:
            return RetrievalApplicability.DIRECT
        if score <= policy.partial_threshold:
            return RetrievalApplicability.PARTIAL
        if score <= policy.contextual_threshold:
            return RetrievalApplicability.CONTEXTUAL
    return RetrievalApplicability.NONE


def _truncate_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value, False
    return encoded[:maximum].decode("utf-8", errors="ignore"), True


def resolve_retrieval(
    frozen: FrozenRetrievalOperation,
    staged: StagedBuildRecord,
    batch: RawRetrievalBatch,
) -> RetrievalResult:
    """Resolve raw candidates without reading authority or performing I/O."""
    profile = frozen.request.retrieval_profile
    policy = frozen.request.applicability_policy
    if (
        staged.build_identity != frozen.frozen_build_identity
        or staged.staged_commitment != frozen.staged_commitment
        or staged.lineage_commitment != frozen.lineage_commitment
        or staged.artifact.artifact_commitment != frozen.artifact_commitment
        or batch.build_identity != frozen.frozen_build_identity
        or batch.artifact_commitment != frozen.artifact_commitment
    ):
        return _failure(
            frozen, RetrievalFailureCode.FROZEN_BUILD_INVALID, "frozen_build",
            "retrieval facts do not match the frozen build boundary",
            RetrievalResolution.REPAIR_REQUIRED,
        )
    if len(batch.candidates) > profile.candidate_limit:
        return _failure(
            frozen, RetrievalFailureCode.CANDIDATE_INVALID, "candidate_limit",
            "index returned more candidates than authorized",
        )
    chunks = {item.chunk_identity: item for item in staged.chunks}
    provenance = {
        item.document_version_identity: item for item in staged.document_provenance
    }
    entries = {item.chunk_identity: item for item in staged.artifact.entries}
    seen: set[str] = set()
    canonical: list[tuple[float, str, object, object]] = []
    for item in batch.candidates:
        chunk = chunks.get(item.chunk_identity)
        entry = entries.get(item.chunk_identity)
        if item.chunk_identity in seen:
            return _failure(
                frozen, RetrievalFailureCode.CANDIDATE_INVALID, "candidate",
                "index returned a duplicate candidate",
            )
        seen.add(item.chunk_identity)
        if chunk is None or entry is None or (
            item.metadata_commitment != chunk.metadata_commitment
            or item.metadata_commitment != entry.metadata_commitment
        ):
            return _failure(
                frozen, RetrievalFailureCode.CANDIDATE_INVALID, "candidate",
                "candidate does not resolve to exact frozen chunk provenance",
                RetrievalResolution.REPAIR_REQUIRED,
            )
        document = provenance.get(chunk.document_version_identity)
        if document is None or document.document_identity != chunk.document_identity:
            return _failure(
                frozen, RetrievalFailureCode.CANDIDATE_INVALID, "provenance",
                "candidate document provenance is missing",
                RetrievalResolution.REPAIR_REQUIRED,
            )
        score = round(float(item.score), profile.score_precision)
        canonical.append((score, item.chunk_identity, chunk, document))
    reverse = profile.score_direction is RetrievalScoreDirection.HIGHER_IS_BETTER
    canonical.sort(key=lambda value: value[1])
    canonical.sort(key=lambda value: value[0], reverse=reverse)

    filters = {item.key: item.value for item in frozen.request.query.filters}
    output: list[OrderedRetrievalCandidate] = []
    payload_truncated = False
    for score, _, chunk, document in canonical[:profile.top_k]:
        metadata = {item.key: item.value for item in document.metadata}
        applicability = _applicability(
            score, metadata, filters, policy, profile.score_direction
        )
        if applicability is RetrievalApplicability.NONE:
            continue
        metadata_size = len(canonical_serialize(document.metadata).encode("utf-8"))
        if metadata_size > profile.max_metadata_bytes_per_result:
            return _failure(
                frozen, RetrievalFailureCode.CANDIDATE_INVALID, "metadata",
                "candidate metadata exceeds the authorized bound",
            )
        content, content_truncated = _truncate_utf8(
            chunk.content, profile.max_content_bytes_per_result
        )
        candidate = OrderedRetrievalCandidate(
            chunk.chunk_identity,
            chunk.document_identity,
            chunk.document_version_identity,
            score,
            applicability,
            content,
            document.metadata,
            content_truncated,
        )
        tentative = RetrievalResult(
            frozen.request.operation_key,
            frozen.frozen_build_identity,
            RetrievalResolution.MATCH,
            tuple((*output, candidate)),
        )
        total = len(canonical_serialize(tentative).encode("utf-8"))
        if total > profile.max_total_payload_bytes:
            if not output:
                return _failure(
                    frozen, RetrievalFailureCode.CANDIDATE_INVALID, "total_payload",
                    "applicable result cannot be represented within the authorized payload bound",
                )
            payload_truncated = True
            break
        output.append(candidate)
    if output:
        return RetrievalResult(
            frozen.request.operation_key,
            frozen.frozen_build_identity,
            RetrievalResolution.MATCH,
            tuple(output),
            payload_truncated=payload_truncated,
        )
    return RetrievalResult(
        frozen.request.operation_key,
        frozen.frozen_build_identity,
        RetrievalResolution.NO_MATCH,
        knowledge_gap=True,
        payload_truncated=payload_truncated,
    )
