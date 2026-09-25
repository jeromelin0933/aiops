"""Pure, bounded, deterministic retrieval resolution."""

from __future__ import annotations

import hashlib

from .contracts import (
    ApplicabilityRuleFact,
    ApplicabilityRuleIdentity,
    ApplicabilityPolicy,
    FrozenRetrievalOperation,
    MetadataItem,
    OrderedRetrievalCandidate,
    RawRetrievalBatch,
    QueryFilter,
    RetrievalApplicability,
    RetrievalEvaluationDisposition,
    RetrievalFailureCode,
    RetrievalFailureFact,
    RetrievalEvaluationFact,
    RetrievalOperationRequest,
    RetrievalResolution,
    RetrievalResult,
    RetrievalRejectionReason,
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


def derive_applicability_provenance(
    score: float,
    metadata: tuple[MetadataItem, ...],
    query_filters: tuple[QueryFilter, ...],
    policy: ApplicabilityPolicy,
    direction: RetrievalScoreDirection,
) -> tuple[
    RetrievalApplicability,
    tuple[QueryFilter, ...],
    tuple[MetadataItem, ...],
    tuple[ApplicabilityRuleFact, ...],
]:
    """Derive the exact bounded inputs and rule outcomes used by Slice 4."""
    query_by_key = {item.key: item for item in query_filters}
    metadata_by_key = {item.key: item for item in metadata}
    required_query = tuple(
        query_by_key[key] for key in policy.required_filter_keys if key in query_by_key
    )
    required_metadata = tuple(
        metadata_by_key[key] for key in policy.required_filter_keys if key in metadata_by_key
    )
    filters_match = all(
        key in query_by_key
        and key in metadata_by_key
        and metadata_by_key[key].value == query_by_key[key].value
        for key in policy.required_filter_keys
    )
    if direction is RetrievalScoreDirection.HIGHER_IS_BETTER:
        threshold_matches = (
            score >= policy.direct_threshold,
            score >= policy.partial_threshold,
            score >= policy.contextual_threshold,
        )
    else:
        threshold_matches = (
            score <= policy.direct_threshold,
            score <= policy.partial_threshold,
            score <= policy.contextual_threshold,
        )
    rules = (
        ApplicabilityRuleFact(ApplicabilityRuleIdentity.REQUIRED_FILTERS, filters_match),
        ApplicabilityRuleFact(ApplicabilityRuleIdentity.DIRECT_THRESHOLD, threshold_matches[0]),
        ApplicabilityRuleFact(ApplicabilityRuleIdentity.PARTIAL_THRESHOLD, threshold_matches[1]),
        ApplicabilityRuleFact(ApplicabilityRuleIdentity.CONTEXTUAL_THRESHOLD, threshold_matches[2]),
    )
    applicability = _applicability(
        score,
        {item.key: item.value for item in metadata},
        {item.key: item.value for item in query_filters},
        policy,
        direction,
    )
    return applicability, required_query, required_metadata, rules


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

    evaluation_inputs: list[dict[str, object]] = []
    applicable_candidates: list[OrderedRetrievalCandidate] = []
    for rank, (score, _, chunk, document) in enumerate(canonical, start=1):
        applicability, query_predicates, metadata_predicates, rules = derive_applicability_provenance(
            score,
            document.metadata,
            frozen.request.query.filters,
            policy,
            profile.score_direction,
        )
        content, content_truncated = _truncate_utf8(
            chunk.content, profile.max_content_bytes_per_result
        )
        within_top_k = rank <= profile.top_k
        if within_top_k and applicability is not RetrievalApplicability.NONE:
            metadata_size = len(canonical_serialize(document.metadata).encode("utf-8"))
            if metadata_size > profile.max_metadata_bytes_per_result:
                return _failure(
                    frozen, RetrievalFailureCode.CANDIDATE_INVALID, "metadata",
                    "candidate metadata exceeds the authorized bound",
                )
            applicable_candidates.append(OrderedRetrievalCandidate(
                chunk.chunk_identity,
                chunk.document_identity,
                chunk.document_version_identity,
                score,
                applicability,
                content,
                document.metadata,
                content_truncated,
            ))
        evaluation_inputs.append({
            "chunk_identity": chunk.chunk_identity,
            "score": score,
            "applicability": applicability,
            "content_truncated": content_truncated,
            "canonical_rank": rank,
            "policy_identity": policy.policy_identity,
            "policy_version": policy.version,
            "query_predicates": query_predicates,
            "metadata_predicates": metadata_predicates,
            "rule_facts": rules,
            "within_top_k": within_top_k,
        })

    def evaluations_for(included_count: int) -> tuple[RetrievalEvaluationFact, ...]:
        included_chunks = {
            item.chunk_identity for item in applicable_candidates[:included_count]
        }
        payload_excluded_chunks = {
            item.chunk_identity for item in applicable_candidates[included_count:]
        }
        facts: list[RetrievalEvaluationFact] = []
        for values in evaluation_inputs:
            applicability = values["applicability"]
            assert isinstance(applicability, RetrievalApplicability)
            chunk_identity = values["chunk_identity"]
            assert isinstance(chunk_identity, str)
            rules = values["rule_facts"]
            assert isinstance(rules, tuple)
            if not values["within_top_k"]:
                disposition = RetrievalEvaluationDisposition.TOP_K_EXCLUDED
                reason = RetrievalRejectionReason.TOP_K_LIMIT
            elif chunk_identity in included_chunks:
                disposition = RetrievalEvaluationDisposition.INCLUDED
                reason = RetrievalRejectionReason.NONE
            elif chunk_identity in payload_excluded_chunks:
                disposition = RetrievalEvaluationDisposition.PAYLOAD_EXCLUDED
                reason = RetrievalRejectionReason.PAYLOAD_LIMIT
            else:
                disposition = RetrievalEvaluationDisposition.REJECTED
                reason = (
                    RetrievalRejectionReason.REQUIRED_FILTER_MISMATCH
                    if not rules[0].matched
                    else RetrievalRejectionReason.SCORE_OUTSIDE_POLICY
                )
            facts.append(RetrievalEvaluationFact(
                chunk_identity=chunk_identity,
                score=values["score"],
                applicability=applicability,
                included=disposition is RetrievalEvaluationDisposition.INCLUDED,
                content_truncated=values["content_truncated"],
                canonical_rank=values["canonical_rank"],
                policy_identity=values["policy_identity"],
                policy_version=values["policy_version"],
                query_predicates=values["query_predicates"],
                metadata_predicates=values["metadata_predicates"],
                rule_facts=rules,
                disposition=disposition,
                rejection_reason=reason,
            ))
        return tuple(facts)

    if not applicable_candidates:
        result = RetrievalResult(
            frozen.request.operation_key,
            frozen.frozen_build_identity,
            RetrievalResolution.NO_MATCH,
            knowledge_gap=True,
            evaluations=evaluations_for(0),
        )
        if len(canonical_serialize(result).encode("utf-8")) > profile.max_total_payload_bytes:
            return _failure(
                frozen, RetrievalFailureCode.CANDIDATE_INVALID, "total_payload",
                "evaluation provenance cannot be represented within the authorized payload bound",
            )
        return result

    selected: RetrievalResult | None = None
    for included_count in range(1, len(applicable_candidates) + 1):
        result = RetrievalResult(
            frozen.request.operation_key,
            frozen.frozen_build_identity,
            RetrievalResolution.MATCH,
            tuple(applicable_candidates[:included_count]),
            payload_truncated=included_count < len(applicable_candidates),
            evaluations=evaluations_for(included_count),
        )
        if len(canonical_serialize(result).encode("utf-8")) > profile.max_total_payload_bytes:
            break
        selected = result
    if selected is None:
        return _failure(
            frozen, RetrievalFailureCode.CANDIDATE_INVALID, "total_payload",
            "applicable result and provenance cannot be represented within the authorized payload bound",
        )
    return selected
