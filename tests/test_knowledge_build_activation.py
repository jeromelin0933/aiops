from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from knowledge_index import (
    ArtifactTrust,
    BuildActivationRequest,
    BuildOperationKey,
    KnowledgeBuildError,
    KnowledgeBuildService,
    KnowledgeStoreConcurrencyError,
    KnowledgeStoreConflictError,
    SqliteKnowledgeStore,
)
from _knowledge_build_testkit import DeterministicIndex, DeterministicProvider, limits, manifest_and_plan


def _stage_validate(service, root, name: str):
    root.mkdir(exist_ok=True)
    raw, chunks, identity_input = manifest_and_plan(
        root, content=f"approved procedure {name}", manifest_id=f"manifest-{name}"
    )
    staged = service.stage(
        operation_key=BuildOperationKey(f"stage-{name}"), raw_manifest=raw,
        source_root=root, chunks=chunks, identity_input=identity_input,
        limits=limits(), required_capability_identity="embedding-capability-v1",
    ).record
    assert staged is not None
    validation = service.validate(
        staged.build_identity, BuildOperationKey(f"validate-{name}"), maximum_probe_results=3
    )
    return staged, validation


def test_only_validated_build_can_be_explicitly_activated(tmp_path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    provider, index = DeterministicProvider(), DeterministicIndex()
    service = KnowledgeBuildService(store, provider, index)
    try:
        root = tmp_path / "source"
        root.mkdir()
        raw, chunks, identity_input = manifest_and_plan(root)
        staged = service.stage(
            operation_key=BuildOperationKey("stage-1"), raw_manifest=raw,
            source_root=root, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        ).record
        assert staged is not None
        request = BuildActivationRequest(staged.build_identity, __import__("knowledge_index").ActivationOperationKey("activate-1"), 0)
        with pytest.raises(KnowledgeBuildError):
            service.activate(request)
        service.validate(staged.build_identity, BuildOperationKey("validate-1"), maximum_probe_results=3)
        result = service.activate(request)
        assert result.authority.active_build_identity == staged.build_identity
        assert service.activate(request) == result
    finally:
        store.close()


def test_contradictory_activation_operation_is_rejected(tmp_path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    index = DeterministicIndex()
    service = KnowledgeBuildService(store, DeterministicProvider(), index)
    try:
        one, _ = _stage_validate(service, tmp_path / "one", "one")
        two, _ = _stage_validate(service, tmp_path / "two", "two")
        from knowledge_index import ActivationOperationKey
        service.activate(BuildActivationRequest(one.build_identity, ActivationOperationKey("activate-same"), 0))
        with pytest.raises(KnowledgeStoreConflictError):
            service.activate(BuildActivationRequest(two.build_identity, ActivationOperationKey("activate-same"), 0))
    finally:
        store.close()


def test_concurrent_activation_has_one_generation_winner(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    index = DeterministicIndex()
    with SqliteKnowledgeStore(path) as store:
        service = KnowledgeBuildService(store, DeterministicProvider(), index)
        one, _ = _stage_validate(service, tmp_path / "one", "one")
        two, _ = _stage_validate(service, tmp_path / "two", "two")
    barrier = Barrier(2)

    def activate(candidate, suffix):
        from knowledge_index import ActivationOperationKey
        with SqliteKnowledgeStore(path) as store:
            service = KnowledgeBuildService(store, DeterministicProvider(), index)
            barrier.wait()
            try:
                service.activate(BuildActivationRequest(candidate.build_identity, ActivationOperationKey(f"activate-{suffix}"), 0))
                return "success"
            except KnowledgeStoreConcurrencyError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda pair: activate(*pair), ((one, "one"), (two, "two"))))
    assert sorted(outcomes) == ["conflict", "success"]


def test_failed_validation_and_activation_preserve_lkg(tmp_path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    index = DeterministicIndex()
    service = KnowledgeBuildService(store, DeterministicProvider(), index)
    from knowledge_index import ActivationOperationKey
    try:
        old, _ = _stage_validate(service, tmp_path / "old", "old")
        service.activate(BuildActivationRequest(old.build_identity, ActivationOperationKey("activate-old"), 0))

        new_root = tmp_path / "new"
        new_root.mkdir()
        raw, chunks, identity_input = manifest_and_plan(new_root, content="new procedure", manifest_id="manifest-new")
        staged = service.stage(
            operation_key=BuildOperationKey("stage-new"), raw_manifest=raw,
            source_root=new_root, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        ).record
        assert staged is not None
        index.artifacts[staged.build_identity] = __import__("dataclasses").replace(
            staged.artifact, integrity_ok=False
        )
        service.validate(staged.build_identity, BuildOperationKey("validate-new"), maximum_probe_results=2)
        with pytest.raises(KnowledgeBuildError):
            service.activate(BuildActivationRequest(staged.build_identity, ActivationOperationKey("activate-new"), 1))
        assert store.read_activation().value.active_build_identity == old.build_identity
    finally:
        store.close()


def test_test_only_artifact_never_becomes_active(tmp_path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    index = DeterministicIndex(trust=ArtifactTrust.TEST_ONLY)
    service = KnowledgeBuildService(store, DeterministicProvider(), index)
    from knowledge_index import ActivationOperationKey
    try:
        staged, validation = _stage_validate(service, tmp_path / "test-only", "test-only")
        assert validation.findings
        with pytest.raises(KnowledgeBuildError):
            service.activate(BuildActivationRequest(staged.build_identity, ActivationOperationKey("activate-test"), 0))
        assert store.read_activation().value is None
    finally:
        store.close()
