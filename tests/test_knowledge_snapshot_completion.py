from dataclasses import replace

import pytest

from knowledge_index import (
    RawRetrievalBatch,
    RawRetrievalCandidate,
    RetrievalApplicability,
    KnowledgeReadStatus,
    RetrievalResolution,
    build_snapshot,
    resolve_retrieval,
)
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import request
from _knowledge_snapshot_testkit import environment, expanded_staged
from knowledge_index.snapshot import validate_snapshot_against_authority


def test_match_completes_with_exactly_one_snapshot(tmp_path) -> None:
    store, staged, provider, index, service = environment(tmp_path)
    outcome = service.resolve(request(), limits())
    assert outcome.resolution is RetrievalResolution.MATCH
    assert outcome.snapshot is not None
    assert outcome.snapshot.frozen_build_identity == staged.build_identity
    assert len(outcome.snapshot.chunks) == 1
    operation = store.get_retrieval_operation_read(request().operation_key)
    assert operation.status is KnowledgeReadStatus.FOUND
    assert operation.value.snapshot_key == outcome.snapshot.snapshot_key
    assert service.resolve(request(), limits()).snapshot == outcome.snapshot
    assert provider.calls == 1
    assert len(index.query_calls) == 1
    store.close()


def test_no_match_completes_with_knowledge_gap_snapshot(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path, candidate_score=0.1)
    outcome = service.resolve(request(), limits())
    assert outcome.resolution is RetrievalResolution.NO_MATCH
    assert outcome.snapshot is not None
    assert outcome.snapshot.knowledge_gap is True
    assert outcome.snapshot.chunks == ()
    assert all(item.applicability.value == "NONE" for item in outcome.snapshot.evaluations)
    store.close()


def _snapshot_validation_fixture(tmp_path):
    store, staged, _, _, _ = environment(tmp_path)
    frozen = store.freeze_retrieval_operation(request())
    staged = expanded_staged(staged)
    batch = RawRetrievalBatch(
        staged.build_identity,
        staged.artifact.artifact_commitment,
        tuple(
            RawRetrievalCandidate(chunk.chunk_identity, score, chunk.metadata_commitment)
            for chunk, score in zip(staged.chunks, (0.95, 0.8))
        ),
    )
    result = resolve_retrieval(frozen, staged, batch)
    return store, frozen, staged, result


def test_snapshot_revalidation_rejects_wrong_score(tmp_path) -> None:
    store, frozen, staged, result = _snapshot_validation_fixture(tmp_path)
    try:
        wrong = replace(
            result,
            candidates=(replace(result.candidates[0], score=0.1), *result.candidates[1:]),
            evaluations=(replace(result.evaluations[0], score=0.1), *result.evaluations[1:]),
        )
        with pytest.raises(ValueError, match="re-derive"):
            validate_snapshot_against_authority(build_snapshot(frozen, staged, wrong), frozen, staged)
    finally:
        store.close()


def test_snapshot_revalidation_rejects_wrong_ordering(tmp_path) -> None:
    store, frozen, staged, result = _snapshot_validation_fixture(tmp_path)
    try:
        wrong = replace(
            result,
            candidates=tuple(reversed(result.candidates)),
            evaluations=tuple(reversed(result.evaluations)),
        )
        with pytest.raises(ValueError, match="re-derive"):
            validate_snapshot_against_authority(build_snapshot(frozen, staged, wrong), frozen, staged)
    finally:
        store.close()


def test_snapshot_revalidation_rejects_wrong_applicability(tmp_path) -> None:
    store, frozen, staged, result = _snapshot_validation_fixture(tmp_path)
    try:
        wrong = replace(
            result,
            candidates=(
                replace(result.candidates[0], applicability=RetrievalApplicability.PARTIAL),
                *result.candidates[1:],
            ),
            evaluations=(
                replace(result.evaluations[0], applicability=RetrievalApplicability.PARTIAL),
                *result.evaluations[1:],
            ),
        )
        with pytest.raises(ValueError, match="re-derive"):
            validate_snapshot_against_authority(build_snapshot(frozen, staged, wrong), frozen, staged)
    finally:
        store.close()


def test_snapshot_revalidation_rejects_wrong_rule_provenance(tmp_path) -> None:
    store, frozen, staged, result = _snapshot_validation_fixture(tmp_path)
    try:
        first = result.evaluations[0]
        wrong_rules = (replace(first.rule_facts[0], matched=False), *first.rule_facts[1:])
        wrong = replace(
            result,
            evaluations=(replace(first, rule_facts=wrong_rules), *result.evaluations[1:]),
        )
        with pytest.raises(ValueError, match="re-derive"):
            validate_snapshot_against_authority(build_snapshot(frozen, staged, wrong), frozen, staged)
    finally:
        store.close()
