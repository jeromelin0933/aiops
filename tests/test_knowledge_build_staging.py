from dataclasses import replace

import pytest

from knowledge_index import (
    BuildOperationKey,
    KnowledgeBuildError,
    KnowledgeBuildService,
    KnowledgeReadStatus,
    OpaqueExternalReference,
    OpaqueReferenceType,
    SqliteKnowledgeStore,
    build_identity,
)
from _knowledge_build_testkit import (
    DeterministicIndex,
    DeterministicProvider,
    capability,
    limits,
    manifest_and_plan,
    provider_failure,
)


def _service(tmp_path, *, provider=None, index=None):
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    provider = provider or DeterministicProvider()
    index = index or DeterministicIndex()
    return store, provider, index, KnowledgeBuildService(store, provider, index)


def test_fresh_admission_precedes_provider_and_index(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    raw["documents"][0]["source_classification"] = "GROUND_TRUTH"
    store, provider, index, service = _service(tmp_path)
    try:
        result = service.stage(
            operation_key=BuildOperationKey("stage-1"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        assert result.record is None
        assert provider.calls == 0
        assert index.stage_calls == 0
    finally:
        store.close()


@pytest.mark.parametrize("unsafe", ["secret token:synthetic-secret", "changed content"])
def test_unsafe_or_changed_source_makes_zero_provider_calls(tmp_path, unsafe: str) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    (tmp_path / "sop.txt").write_text(unsafe, encoding="utf-8")
    store, provider, index, service = _service(tmp_path)
    try:
        result = service.stage(
            operation_key=BuildOperationKey("stage-1"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        assert result.failures and provider.calls == 0 and index.stage_calls == 0
    finally:
        store.close()


def test_same_stage_operation_replays_without_provider_or_index_reinvocation(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    store, provider, index, service = _service(tmp_path)
    try:
        kwargs = dict(
            operation_key=BuildOperationKey("stage-1"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        first = service.stage(**kwargs)
        second = service.stage(**kwargs)
        assert first == second
        assert provider.calls == 1
        assert index.stage_calls == 1
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
    finally:
        store.close()


def test_contradictory_stage_operation_is_rejected(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    store, provider, index, service = _service(tmp_path)
    try:
        service.stage(
            operation_key=BuildOperationKey("stage-1"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        with pytest.raises(KnowledgeBuildError):
            service.stage(
                operation_key=BuildOperationKey("stage-other"), raw_manifest=raw,
                source_root=tmp_path, chunks=chunks, identity_input=identity_input,
                limits=limits(), required_capability_identity="embedding-capability-v1",
            )
    finally:
        store.close()


def test_same_operation_different_build_is_rejected_before_external_calls(tmp_path) -> None:
    store, provider, index, service = _service(tmp_path)
    try:
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        first_root.mkdir()
        second_root.mkdir()
        first = manifest_and_plan(first_root, content="procedure one", manifest_id="one")
        second = manifest_and_plan(second_root, content="procedure two", manifest_id="two")
        operation = BuildOperationKey("stable-operation")
        assert service.stage(
            operation_key=operation, raw_manifest=first[0], source_root=first_root,
            chunks=first[1], identity_input=first[2], limits=limits(),
            required_capability_identity="embedding-capability-v1",
        ).record is not None
        calls_before = provider.calls
        index_calls_before = index.stage_calls
        with pytest.raises(KnowledgeBuildError):
            service.stage(
                operation_key=operation, raw_manifest=second[0], source_root=second_root,
                chunks=second[1], identity_input=second[2], limits=limits(),
                required_capability_identity="embedding-capability-v1",
            )
        assert provider.calls == calls_before
        assert index.stage_calls == index_calls_before
    finally:
        store.close()


def test_interrupted_operation_preserves_profile_continuity(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    operation = BuildOperationKey("interrupted-operation")
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        interrupted = DeterministicProvider(raises=RuntimeError("provider interrupted"))
        index = DeterministicIndex()
        result = KnowledgeBuildService(store, interrupted, index).stage(
            operation_key=operation, raw_manifest=raw, source_root=tmp_path,
            chunks=chunks, identity_input=identity_input, limits=limits(),
            required_capability_identity="embedding-capability-v1",
        )
        assert result.failures and interrupted.calls == 1
        store.close()

        store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
        changed = replace(
            capability(),
            profile_reference=OpaqueExternalReference(
                OpaqueReferenceType.CREDENTIAL_PROFILE, "profile-approved-2"
            ),
        )
        switched = DeterministicProvider(changed)
        with pytest.raises(KnowledgeBuildError):
            KnowledgeBuildService(store, switched, index).stage(
                operation_key=operation, raw_manifest=raw, source_root=tmp_path,
                chunks=chunks, identity_input=identity_input, limits=limits(),
                required_capability_identity="embedding-capability-v1",
            )
        assert switched.calls == 0 and index.stage_calls == 0
    finally:
        store.close()


def test_interrupted_operation_allows_same_profile_continuation(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    operation = BuildOperationKey("same-profile-continuation")
    path = tmp_path / "knowledge.sqlite3"
    index = DeterministicIndex()
    with SqliteKnowledgeStore(path) as store:
        interrupted = DeterministicProvider(raises=RuntimeError("provider interrupted"))
        result = KnowledgeBuildService(store, interrupted, index).stage(
            operation_key=operation, raw_manifest=raw, source_root=tmp_path,
            chunks=chunks, identity_input=identity_input, limits=limits(),
            required_capability_identity="embedding-capability-v1",
        )
        assert result.failures
    with SqliteKnowledgeStore(path) as reopened:
        provider = DeterministicProvider()
        result = KnowledgeBuildService(reopened, provider, index).stage(
            operation_key=operation, raw_manifest=raw, source_root=tmp_path,
            chunks=chunks, identity_input=identity_input, limits=limits(),
            required_capability_identity="embedding-capability-v1",
        )
        assert result.record is not None
        assert provider.calls == 1 and index.stage_calls == 1


def test_material_compatibility_change_changes_build_identity(tmp_path) -> None:
    raw, chunks, original = manifest_and_plan(tmp_path)
    changed = replace(original, embedding_model="text-embedding-new")
    assert build_identity(original) != build_identity(changed)


def test_provider_or_index_failure_does_not_stage_or_activate(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    provider = DeterministicProvider(failure=provider_failure())
    store, provider, index, service = _service(tmp_path, provider=provider)
    try:
        result = service.stage(
            operation_key=BuildOperationKey("stage-1"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        assert result.failures
        assert store.get_staged_build(build_identity(identity_input)).status is KnowledgeReadStatus.NOT_FOUND
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
    finally:
        store.close()

    other_root = tmp_path / "index-failure"
    other_root.mkdir()
    raw, chunks, identity_input = manifest_and_plan(other_root)
    store = SqliteKnowledgeStore(other_root / "knowledge.sqlite3")
    provider = DeterministicProvider()
    index = DeterministicIndex(stage_error=RuntimeError("index unavailable"))
    try:
        result = KnowledgeBuildService(store, provider, index).stage(
            operation_key=BuildOperationKey("stage-2"), raw_manifest=raw,
            source_root=other_root, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        assert result.failures
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
    finally:
        store.close()
