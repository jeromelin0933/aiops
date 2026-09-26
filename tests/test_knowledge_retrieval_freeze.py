from knowledge_index import (
    KnowledgeBuildService,
    KnowledgeReadStatus,
    SqliteKnowledgeStore,
)
from _knowledge_retrieval_testkit import request, stage_activate
from _knowledge_build_testkit import DeterministicIndex, DeterministicProvider


def test_freeze_captures_active_once_and_replay_keeps_old_build(tmp_path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        one, index = stage_activate(store, tmp_path / "one", "one")
        frozen = store.freeze_retrieval_operation(request())
        two, _ = stage_activate(
            store, tmp_path / "two", "two", expected_generation=1, index=index
        )
        replay = store.freeze_retrieval_operation(request())
        assert replay == frozen
        assert replay.frozen_build_identity == one.build_identity
        assert replay.frozen_build_identity != two.build_identity
        fresh = store.freeze_retrieval_operation(request("retrieve-2"))
        assert fresh.frozen_build_identity == two.build_identity
    finally:
        store.close()


def test_frozen_operation_is_exactly_readable(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SqliteKnowledgeStore(path) as store:
        stage_activate(store, tmp_path / "one", "one")
        frozen = store.freeze_retrieval_operation(request())
    with SqliteKnowledgeStore(path) as reopened:
        result = reopened.get_frozen_retrieval_operation(request().operation_key)
        assert result.status is KnowledgeReadStatus.FOUND
        assert result.value == frozen


def test_two_connection_activation_freeze_race_has_one_consistent_cut(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from knowledge_index import (
        ActivationOperationKey, BuildActivationRequest, BuildOperationKey,
    )
    from _knowledge_build_testkit import limits, manifest_and_plan

    path = tmp_path / "knowledge.sqlite3"
    index = DeterministicIndex()
    with SqliteKnowledgeStore(path) as store:
        first, _ = stage_activate(store, tmp_path / "one", "one", index=index)
        service = KnowledgeBuildService(store, DeterministicProvider(), index)
        root = tmp_path / "two"
        root.mkdir()
        raw, chunks, identity_input = manifest_and_plan(
            root, content="approved operational procedure two", manifest_id="manifest-two"
        )
        second = service.stage(
            operation_key=BuildOperationKey("stage-two"), raw_manifest=raw,
            source_root=root, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        ).record
        assert second is not None
        service.validate(second.build_identity, BuildOperationKey("validate-two"), maximum_probe_results=3)
    barrier = Barrier(2)

    def activate():
        with SqliteKnowledgeStore(path) as store:
            barrier.wait()
            KnowledgeBuildService(store, DeterministicProvider(), index).activate(
                BuildActivationRequest(second.build_identity, ActivationOperationKey("activate-two"), 1)
            )

    def freeze():
        with SqliteKnowledgeStore(path) as store:
            barrier.wait()
            return store.freeze_retrieval_operation(request("race-operation"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        activation_future = pool.submit(activate)
        freeze_future = pool.submit(freeze)
        frozen = freeze_future.result()
        activation_future.result()
    expected = first.build_identity if frozen.activation_generation == 1 else second.build_identity
    assert frozen.activation_generation in (1, 2)
    assert frozen.frozen_build_identity == expected
