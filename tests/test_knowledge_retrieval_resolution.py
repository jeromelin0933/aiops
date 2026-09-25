from dataclasses import replace

from knowledge_index import (
    RawRetrievalBatch,
    RawRetrievalCandidate,
    RetrievalResolution,
    SqliteKnowledgeStore,
    resolve_retrieval,
)
from _knowledge_retrieval_testkit import candidates_for, profile, request, stage_activate


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
        assert no_match.resolution is RetrievalResolution.NO_MATCH
        assert no_match.knowledge_gap is True


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
