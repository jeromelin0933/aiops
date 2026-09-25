from dataclasses import replace
import sqlite3

import pytest

from knowledge_index import (
    KnowledgeReadStatus,
    KnowledgeStoreConflictError,
    KnowledgeStoreIntegrityError,
    RawRetrievalBatch,
    RawRetrievalCandidate,
    RetrievalEvaluationDisposition,
    RetrievalOperationState,
    RetrievalRejectionReason,
    RetrievalResolution,
    SqliteKnowledgeStore,
    build_retrieval_completion,
    build_snapshot,
    derive_retrieval_completion_commitment,
    derive_snapshot_commitment,
    derive_snapshot_key,
    resolve_retrieval,
)
from knowledge_index.snapshot import (
    validate_retrieval_completion_authority,
    validate_snapshot_against_authority,
)
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import profile, request
from _knowledge_snapshot_testkit import environment, expanded_staged


def _resign_snapshot(snapshot, **changes):
    changed = replace(snapshot, **changes)
    commitment = derive_snapshot_commitment(changed)
    return replace(
        changed,
        snapshot_commitment=commitment,
        snapshot_key=derive_snapshot_key(commitment),
    )


def _two_candidate_authority(tmp_path, *, selected_profile=None, scores=(0.95, 0.8)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store, staged, _, _, _ = environment(tmp_path)
    selected_request = request(
        retrieval_profile=selected_profile or request().retrieval_profile
    )
    frozen = store.freeze_retrieval_operation(selected_request)
    staged = expanded_staged(staged)
    raw = RawRetrievalBatch(
        staged.build_identity,
        staged.artifact.artifact_commitment,
        tuple(
            RawRetrievalCandidate(chunk.chunk_identity, score, chunk.metadata_commitment)
            for chunk, score in zip(staged.chunks, scores)
        ),
    )
    result = resolve_retrieval(frozen, staged, raw)
    completion = build_retrieval_completion(frozen, staged, result)
    snapshot = build_snapshot(frozen, staged, completion)
    return store, frozen, staged, completion, snapshot


def _coherent_snapshot_for_raw(frozen, staged, candidates):
    raw = RawRetrievalBatch(
        staged.build_identity,
        staged.artifact.artifact_commitment,
        tuple(candidates),
    )
    result = resolve_retrieval(frozen, staged, raw)
    assert result.resolution in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH)
    completion = build_retrieval_completion(frozen, staged, result)
    snapshot = build_snapshot(frozen, staged, completion)
    validate_retrieval_completion_authority(completion, frozen, staged)
    validate_snapshot_against_authority(snapshot, frozen, staged, completion)
    return completion, snapshot


def _persist_completion_without_snapshot(tmp_path, *, candidate_score=0.95):
    store, _, provider, index, service = environment(
        tmp_path, candidate_score=candidate_score
    )
    result = service._retrieval.retrieve(request(), limits())
    assert result.resolution in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH)
    snapshot = service._snapshot_for_result(request().operation_key, result)
    completion = store.get_retrieval_completion(request().operation_key)
    assert completion.status is KnowledgeReadStatus.FOUND
    return store, provider, index, completion.value, snapshot


def test_durable_completion_saves_the_complete_bounded_candidate_set(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path)
    outcome = service.resolve(request(), limits())
    assert outcome.snapshot is not None
    read = store.get_retrieval_operation_read(request().operation_key)
    assert read.status is KnowledgeReadStatus.FOUND
    assert read.value.state is RetrievalOperationState.COMPLETED
    completion = read.value.completion
    assert completion is not None
    assert tuple(item.chunk_identity for item in completion.raw_batch.candidates) == tuple(
        item.chunk_identity for item in completion.evaluations
    )
    assert len(completion.raw_batch.candidates) == 1
    store.close()


def test_equivalent_completion_replay_returns_same_durable_fact(tmp_path) -> None:
    store, _, _, completion, _ = _persist_completion_without_snapshot(tmp_path)
    assert store.record_retrieval_completion(completion) == completion
    assert store.get_retrieval_operation_read(
        request().operation_key
    ).value.state is RetrievalOperationState.RETRIEVAL_COMPLETED
    store.close()


def test_changed_raw_score_for_same_operation_fails_closed(tmp_path) -> None:
    store, _, _, completion, _ = _persist_completion_without_snapshot(tmp_path)
    raw_candidate = completion.raw_batch.candidates[0]
    evaluation = completion.evaluations[0]
    changed = replace(
        completion,
        raw_batch=replace(
            completion.raw_batch,
            candidates=(replace(raw_candidate, score=0.96),),
        ),
        evaluations=(replace(evaluation, score=0.96),),
        semantic_commitment="0" * 64,
    )
    changed = replace(
        changed,
        semantic_commitment=derive_retrieval_completion_commitment(changed),
    )
    with pytest.raises(KnowledgeStoreConflictError, match="contradictory"):
        store.record_retrieval_completion(changed)
    store.close()


def test_omitted_candidate_for_same_operation_fails_closed(tmp_path) -> None:
    store, _, _, completion, _ = _persist_completion_without_snapshot(
        tmp_path, candidate_score=0.1
    )
    changed = replace(
        completion,
        raw_batch=replace(completion.raw_batch, candidates=()),
        evaluations=(),
        semantic_commitment="0" * 64,
    )
    changed = replace(
        changed,
        semantic_commitment=derive_retrieval_completion_commitment(changed),
    )
    with pytest.raises(KnowledgeStoreConflictError, match="contradictory"):
        store.record_retrieval_completion(changed)
    store.close()


def test_coherent_added_candidate_fails_against_completion_authority(tmp_path) -> None:
    store, frozen, staged, _, _ = _two_candidate_authority(tmp_path)
    first, second = staged.chunks
    completion, snapshot = _coherent_snapshot_for_raw(
        frozen,
        staged,
        (RawRetrievalCandidate(first.chunk_identity, 0.95, first.metadata_commitment),),
    )
    forged_completion, forged_snapshot = _coherent_snapshot_for_raw(
        frozen,
        staged,
        (
            RawRetrievalCandidate(first.chunk_identity, 0.95, first.metadata_commitment),
            RawRetrievalCandidate(second.chunk_identity, 0.8, second.metadata_commitment),
        ),
    )
    assert len(completion.raw_batch.candidates) == len(snapshot.evaluations) == 1
    assert len(forged_completion.raw_batch.candidates) == len(forged_snapshot.evaluations) == 2
    assert forged_snapshot.evaluations[1].canonical_rank == 2
    assert forged_snapshot.evaluations[1].included
    with pytest.raises(ValueError, match="re-derive"):
        validate_snapshot_against_authority(
            forged_snapshot, frozen, staged, completion
        )
    store.close()


def test_snapshot_coherent_score_tamper_fails_against_completion(tmp_path) -> None:
    store, frozen, staged, completion, snapshot = _two_candidate_authority(tmp_path)
    forged_completion, forged_snapshot = _coherent_snapshot_for_raw(
        frozen,
        staged,
        tuple(
            RawRetrievalCandidate(
                candidate.chunk_identity,
                0.96 if index == 0 else candidate.score,
                candidate.metadata_commitment,
            )
            for index, candidate in enumerate(completion.raw_batch.candidates)
        ),
    )
    assert forged_completion.evaluations[0].score == forged_snapshot.chunks[0].score == 0.96
    assert forged_snapshot.evaluations[0].applicability is snapshot.evaluations[0].applicability
    assert forged_snapshot.evaluations[0].rule_facts == snapshot.evaluations[0].rule_facts
    with pytest.raises(ValueError, match="re-derive"):
        validate_snapshot_against_authority(
            forged_snapshot, frozen, staged, completion
        )
    store.close()


def test_snapshot_coherent_ordering_tamper_fails_against_completion(tmp_path) -> None:
    store, frozen, staged, completion, snapshot = _two_candidate_authority(tmp_path)
    first, second = staged.chunks
    _, forged_snapshot = _coherent_snapshot_for_raw(
        frozen,
        staged,
        (
            RawRetrievalCandidate(second.chunk_identity, 0.96, second.metadata_commitment),
            RawRetrievalCandidate(first.chunk_identity, 0.79, first.metadata_commitment),
        ),
    )
    assert tuple(item.chunk_identity for item in forged_snapshot.chunks) == tuple(
        reversed(tuple(item.chunk_identity for item in snapshot.chunks))
    )
    assert tuple(item.canonical_rank for item in forged_snapshot.evaluations) == (1, 2)
    assert tuple(item.chunk_identity for item in forged_snapshot.evaluations) == tuple(
        item.chunk_identity for item in forged_snapshot.chunks
    )
    with pytest.raises(ValueError, match="re-derive"):
        validate_snapshot_against_authority(
            forged_snapshot, frozen, staged, completion
        )
    store.close()


def test_snapshot_coherent_applicability_and_rule_tamper_fails(tmp_path) -> None:
    store, frozen, staged, completion, snapshot = _two_candidate_authority(
        tmp_path, scores=(0.95, 0.4)
    )
    first, second = staged.chunks
    _, forged_snapshot = _coherent_snapshot_for_raw(
        frozen,
        staged,
        (
            RawRetrievalCandidate(first.chunk_identity, 0.8, first.metadata_commitment),
            RawRetrievalCandidate(second.chunk_identity, 0.4, second.metadata_commitment),
        ),
    )
    original = snapshot.evaluations[0]
    forged = forged_snapshot.evaluations[0]
    assert original.applicability.value == "DIRECT"
    assert forged.applicability.value == "PARTIAL"
    assert tuple(item.matched for item in original.rule_facts) == (True, True, True, True)
    assert tuple(item.matched for item in forged.rule_facts) == (True, False, True, True)
    assert forged.query_predicates == original.query_predicates
    assert forged.metadata_predicates == original.metadata_predicates
    assert forged_snapshot.chunks[0].applicability is forged.applicability
    with pytest.raises(ValueError, match="re-derive"):
        validate_snapshot_against_authority(
            forged_snapshot, frozen, staged, completion
        )
    store.close()


def test_snapshot_candidate_omission_and_rerank_fails(tmp_path) -> None:
    store, frozen, staged, completion, snapshot = _two_candidate_authority(tmp_path)
    assert len(snapshot.chunks) == 2
    kept_evaluation = replace(snapshot.evaluations[1], canonical_rank=1)
    tampered = _resign_snapshot(
        snapshot,
        evaluations=(kept_evaluation,),
        chunks=(snapshot.chunks[1],),
    )
    with pytest.raises(ValueError, match="completion|re-derive"):
        validate_snapshot_against_authority(tampered, frozen, staged, completion)
    store.close()


def test_no_match_covers_every_durable_candidate(tmp_path) -> None:
    store, frozen, staged, completion, snapshot = _two_candidate_authority(
        tmp_path, scores=(0.1, 0.2)
    )
    assert snapshot.resolution is RetrievalResolution.NO_MATCH
    assert len(completion.raw_batch.candidates) == len(snapshot.evaluations) == 2
    tampered = _resign_snapshot(snapshot, evaluations=snapshot.evaluations[:1])
    with pytest.raises(ValueError, match="completion|re-derive"):
        validate_snapshot_against_authority(tampered, frozen, staged, completion)
    store.close()


def test_top_k_exclusion_provenance_revalidates_from_completion(tmp_path) -> None:
    store, frozen, staged, completion, snapshot = _two_candidate_authority(
        tmp_path, selected_profile=profile(top_k=1)
    )
    excluded = completion.evaluations[1]
    assert excluded.disposition is RetrievalEvaluationDisposition.TOP_K_EXCLUDED
    assert excluded.rejection_reason is RetrievalRejectionReason.TOP_K_LIMIT
    validate_retrieval_completion_authority(completion, frozen, staged)
    validate_snapshot_against_authority(snapshot, frozen, staged, completion)
    store.close()


def test_payload_exclusion_provenance_revalidates_from_completion(tmp_path) -> None:
    store, staged, _, _, _ = environment(tmp_path)
    frozen = store.freeze_retrieval_operation(request())
    staged = expanded_staged(staged)
    raw = RawRetrievalBatch(
        staged.build_identity,
        staged.artifact.artifact_commitment,
        tuple(
            RawRetrievalCandidate(chunk.chunk_identity, score, chunk.metadata_commitment)
            for chunk, score in zip(staged.chunks, (0.95, 0.8))
        ),
    )
    selected = None
    for bound in range(1024, 8193):
        candidate_frozen = replace(
            frozen,
            request=replace(
                frozen.request,
                retrieval_profile=profile(max_total_payload_bytes=bound),
            ),
        )
        result = resolve_retrieval(candidate_frozen, staged, raw)
        if result.resolution is RetrievalResolution.MATCH and result.payload_truncated:
            completion = build_retrieval_completion(candidate_frozen, staged, result)
            snapshot = build_snapshot(candidate_frozen, staged, completion)
            selected = (candidate_frozen, completion, snapshot)
            break
    assert selected is not None
    frozen, completion, snapshot = selected
    excluded = next(item for item in completion.evaluations if not item.included)
    assert excluded.disposition is RetrievalEvaluationDisposition.PAYLOAD_EXCLUDED
    assert excluded.rejection_reason is RetrievalRejectionReason.PAYLOAD_LIMIT
    validate_snapshot_against_authority(snapshot, frozen, staged, completion)
    store.close()


def test_response_loss_after_completion_reuses_fact_without_requery(tmp_path, monkeypatch) -> None:
    store, _, provider, index, service = environment(tmp_path)
    original = store.complete_operation_with_snapshot
    monkeypatch.setattr(
        store,
        "complete_operation_with_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("response lost")),
    )
    with pytest.raises(RuntimeError, match="response lost"):
        service.resolve(request(), limits())
    completion = store.get_retrieval_completion(request().operation_key)
    assert completion.status is KnowledgeReadStatus.FOUND
    monkeypatch.setattr(store, "complete_operation_with_snapshot", original)
    monkeypatch.setattr(
        store,
        "read_activation",
        lambda: (_ for _ in ()).throw(AssertionError("must not read current Active")),
    )
    replay = service.resolve(request(), limits())
    assert replay.snapshot is not None
    assert provider.calls == 1
    assert len(index.query_calls) == 1
    store.close()


def test_strict_reopen_rederives_completion_commitment(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    store, _, _, _, service = environment(tmp_path)
    service.resolve(request(), limits())
    store.close()
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE retrieval_completion_facts SET semantic_commitment = ?",
        ("f" * 64,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_missing_completion_for_available_snapshot_requires_repair(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    store, _, _, _, service = environment(tmp_path)
    service.resolve(request(), limits())
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM retrieval_completion_facts")
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)
