from dataclasses import replace

from knowledge_index import (
    FrozenRetrievalOperation,
    RawRetrievalBatch,
    RetrievalResolution,
    SqliteKnowledgeStore,
    resolve_retrieval,
    BuildChunk,
    IndexEntryFact,
    canonical_serialize,
)
from _knowledge_retrieval_testkit import candidates_for, profile, request, stage_activate


def test_same_frozen_inputs_produce_same_resolution(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        frozen = store.freeze_retrieval_operation(request())
        batch = RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment,
            candidates_for(staged, 0.95123456),
        )
        assert resolve_retrieval(frozen, staged, batch) == resolve_retrieval(frozen, staged, batch)


def test_duplicate_candidate_fails_closed(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        frozen = store.freeze_retrieval_operation(request())
        candidate = candidates_for(staged)[0]
        result = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment, (candidate, candidate)
        ))
        assert result.resolution is RetrievalResolution.INVALID


def test_native_order_does_not_control_equal_score_tie_break(tmp_path) -> None:
    import hashlib

    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, _ = stage_activate(store, tmp_path / "one", "one")
        first = staged.chunks[0]
        content = "second approved operational procedure"
        second = BuildChunk(
            "kchk_" + "0" * 64, first.document_identity,
            first.document_version_identity, "section-2", 1, content,
            hashlib.sha256(content.encode()).hexdigest(), "c" * 64,
        )
        artifact = replace(staged.artifact, entries=staged.artifact.entries + (
            IndexEntryFact(second.chunk_identity, second.metadata_commitment, 3),
        ))
        staged = replace(staged, chunks=(first, second), artifact=artifact)
        candidates = (
            __import__("knowledge_index").RawRetrievalCandidate(first.chunk_identity, 0.9, first.metadata_commitment),
            __import__("knowledge_index").RawRetrievalCandidate(second.chunk_identity, 0.9, second.metadata_commitment),
        )
        probe = store.freeze_retrieval_operation(request("probe-order"))
        baseline = resolve_retrieval(probe, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment, candidates
        ))
        one_candidate = replace(
            baseline, candidates=baseline.candidates[:1], payload_truncated=False
        )
        total_bound = len(canonical_serialize(one_candidate).encode("utf-8"))
        frozen = store.freeze_retrieval_operation(request(
            "bound-order",
            retrieval_profile=profile(max_total_payload_bytes=total_bound),
        ))
        one = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment, candidates
        ))
        two = resolve_retrieval(frozen, staged, RawRetrievalBatch(
            staged.build_identity, staged.artifact.artifact_commitment, tuple(reversed(candidates))
        ))
        assert one == two
        assert one.payload_truncated is True
        assert len(one.candidates) == 1
        assert tuple(item.chunk_identity for item in one.candidates) == tuple(sorted(
            item.chunk_identity for item in one.candidates
        ))
