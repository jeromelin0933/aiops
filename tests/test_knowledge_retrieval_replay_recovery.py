from dataclasses import replace

import pytest

from knowledge_index import (
    KnowledgeStoreConflictError,
    SqliteKnowledgeStore,
)
from _knowledge_retrieval_testkit import profile, request, stage_activate


def test_equivalent_replay_survives_restart(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SqliteKnowledgeStore(path) as store:
        stage_activate(store, tmp_path / "one", "one")
        expected = store.freeze_retrieval_operation(request())
    with SqliteKnowledgeStore(path) as store:
        assert store.freeze_retrieval_operation(request()) == expected


def test_contradictory_query_and_profile_replay_fail_closed(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        stage_activate(store, tmp_path / "one", "one")
        store.freeze_retrieval_operation(request())
        with pytest.raises(KnowledgeStoreConflictError):
            store.freeze_retrieval_operation(request(query=replace(request().query, text="other")))
        with pytest.raises(KnowledgeStoreConflictError):
            store.freeze_retrieval_operation(request(retrieval_profile=profile(top_k=1)))
        with pytest.raises(KnowledgeStoreConflictError):
            store.freeze_retrieval_operation(request(
                applicability_policy=replace(request().applicability_policy, contextual_threshold=0.4)
            ))
        with pytest.raises(KnowledgeStoreConflictError):
            store.freeze_retrieval_operation(request(external_references=()))


def test_frozen_target_never_falls_back_to_new_active(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        first, index = stage_activate(store, tmp_path / "one", "one")
        frozen = store.freeze_retrieval_operation(request())
        second, _ = stage_activate(store, tmp_path / "two", "two", expected_generation=1, index=index)
        assert frozen.frozen_build_identity == first.build_identity
        assert store.get_frozen_retrieval_operation(request().operation_key).value.frozen_build_identity != second.build_identity
