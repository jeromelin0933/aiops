from dataclasses import replace

from knowledge_index import (
    ApplicabilityRuleIdentity,
    DocumentStatus,
    RetrievalApplicability,
    RetrievalEvaluationDisposition,
    RetrievalRejectionReason,
    RawRetrievalBatch,
    RawRetrievalCandidate,
    RetrievalResolution,
    SqliteKnowledgeStore,
    build_snapshot,
    canonical_serialize,
    derive_sop_backed_eligibility,
    resolve_retrieval,
)
from _knowledge_retrieval_testkit import candidates_for, profile, request, stage_activate
from _knowledge_snapshot_testkit import expanded_staged


def test_match_and_no_match_are_deterministic(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        frozen = store.freeze_retrieval_operation(request())
        match = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment,
            candidates_for(staged, 0.95),
        ))
        no_match = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment,
            candidates_for(staged, 0.1),
        ))
        assert match.resolution is RetrievalResolution.MATCH
        assert match.evaluations and match.evaluations[0].included is True
        assert no_match.resolution is RetrievalResolution.NO_MATCH
        assert no_match.knowledge_gap is True
        assert no_match.evaluations and no_match.evaluations[0].included is False


def test_frozen_governance_controls_sop_authority_and_other_is_contextual(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        legacy = staged.document_provenance[0]
        sop = replace(
            legacy, knowledge_type="SOP", guidance_authority="SOP_BACKED_ELIGIBLE",
            document_status=DocumentStatus.ACTIVE, approval_state="APPROVED",
            production_eligible=True,
        )
        runbook = replace(sop, knowledge_type="RUNBOOK")
        other = replace(
            sop, knowledge_type="OTHER_APPROVED_OPERATIONAL_REFERENCE",
            guidance_authority="CONTEXTUAL_ONLY",
        )
        assert derive_sop_backed_eligibility(sop, RetrievalApplicability.DIRECT)
        assert derive_sop_backed_eligibility(runbook, RetrievalApplicability.PARTIAL)
        assert not derive_sop_backed_eligibility(other, RetrievalApplicability.DIRECT)
        assert not derive_sop_backed_eligibility(legacy, RetrievalApplicability.DIRECT)
        assert not derive_sop_backed_eligibility(sop, RetrievalApplicability.NONE)

        frozen = store.freeze_retrieval_operation(request())
        contextual_stage = replace(staged, document_provenance=(other,))
        result = resolve_retrieval(frozen, contextual_stage, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment,
            candidates_for(staged, 1.0),
        ))
        assert result.resolution is RetrievalResolution.MATCH
        assert result.candidates[0].applicability is RetrievalApplicability.CONTEXTUAL
        assert not derive_sop_backed_eligibility(
            other, result.candidates[0].applicability
        )


def test_orphan_candidate_is_repair_required_not_no_match(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        frozen = store.freeze_retrieval_operation(request())
        orphan = replace(candidates_for(staged)[0], chunk_identity="kchk_" + "f" * 64)
        result = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment, (orphan,)
        ))
        assert result.resolution is RetrievalResolution.REPAIR_REQUIRED


def test_applicable_candidate_over_total_payload_is_invalid_not_no_match(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        selected = profile(max_content_bytes_per_result=1, max_total_payload_bytes=1)
        frozen = store.freeze_retrieval_operation(request(retrieval_profile=selected))
        result = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment,
            candidates_for(staged, 0.95),
        ))
        assert result.resolution is RetrievalResolution.INVALID
        assert result.resolution is not RetrievalResolution.NO_MATCH
        assert result.knowledge_gap is False


def test_applicable_content_is_deterministically_truncated_with_provenance(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        selected = profile(max_content_bytes_per_result=5)
        frozen = store.freeze_retrieval_operation(request(retrieval_profile=selected))
        result = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment,
            candidates_for(staged, 0.95),
        ))
        assert result.resolution is RetrievalResolution.MATCH
        assert len(result.candidates[0].content.encode("utf-8")) <= 5
        assert result.candidates[0].content_truncated is True


def test_applicable_metadata_over_bound_is_invalid_not_no_match(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        selected = profile(max_metadata_bytes_per_result=1)
        frozen = store.freeze_retrieval_operation(request(retrieval_profile=selected))
        result = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment,
            candidates_for(staged, 0.95),
        ))
        assert result.resolution is RetrievalResolution.INVALID
        assert result.resolution is not RetrievalResolution.NO_MATCH
        assert result.knowledge_gap is False


def _two_candidates(staged, first=0.95, second=0.8):
    return tuple(
        RawRetrievalCandidate(chunk.chunk_identity, score, chunk.metadata_commitment)
        for chunk, score in zip(staged.chunks, (first, second))
    )


def test_evaluation_preserves_filter_rule_and_rejection_provenance(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        frozen = store.freeze_retrieval_operation(request())
        result = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment,
            candidates_for(staged, 0.1),
        ))
        fact = result.evaluations[0]
        snapshot_fact = build_snapshot(frozen, staged, result).evaluations[0]
        assert snapshot_fact == fact
        assert fact.query_predicates == request().query.filters
        assert tuple(item.key for item in fact.metadata_predicates) == ("service",)
        assert tuple(item.rule_identity for item in fact.rule_facts) == tuple(ApplicabilityRuleIdentity)
        assert fact.disposition is RetrievalEvaluationDisposition.REJECTED
        assert fact.rejection_reason is RetrievalRejectionReason.SCORE_OUTSIDE_POLICY


def test_top_k_exclusion_has_explicit_reason_and_native_order_is_irrelevant(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        staged = expanded_staged(staged)
        selected = profile(top_k=1)
        frozen = store.freeze_retrieval_operation(request(retrieval_profile=selected))
        candidates = _two_candidates(staged)
        forward = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment, candidates,
        ))
        reverse = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment, tuple(reversed(candidates)),
        ))
        assert forward == reverse
        excluded = forward.evaluations[1]
        assert build_snapshot(frozen, staged, forward).evaluations[1] == excluded
        assert excluded.disposition is RetrievalEvaluationDisposition.TOP_K_EXCLUDED
        assert excluded.rejection_reason is RetrievalRejectionReason.TOP_K_LIMIT


def test_payload_exclusion_is_explicit_and_final_result_obeys_bound(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        staged = expanded_staged(staged)
        frozen = store.freeze_retrieval_operation(request())
        candidates = _two_candidates(staged)
        selected_result = None
        selected_bound = None
        for bound in range(1024, 8193):
            selected_profile = profile(max_total_payload_bytes=bound)
            candidate_frozen = replace(
                frozen,
                request=replace(frozen.request, retrieval_profile=selected_profile),
            )
            result = resolve_retrieval(candidate_frozen, staged, RawRetrievalBatch(
                staged.build_identity, staged.artifact.artifact_commitment, candidates,
            ))
            if result.resolution is RetrievalResolution.MATCH and result.payload_truncated:
                selected_result = result
                selected_bound = bound
                break
        assert selected_result is not None and selected_bound is not None
        excluded = next(item for item in selected_result.evaluations if not item.included)
        assert build_snapshot(candidate_frozen, staged, selected_result).evaluations == selected_result.evaluations
        assert excluded.disposition is RetrievalEvaluationDisposition.PAYLOAD_EXCLUDED
        assert excluded.rejection_reason is RetrievalRejectionReason.PAYLOAD_LIMIT
        assert len(canonical_serialize(selected_result).encode("utf-8")) <= selected_bound
