from dataclasses import replace

from knowledge_index import (
    ActivationAuthorityRecord,
    ActivationOperationKey,
    BuildActivationRequest,
    BuildOperationKey,
    KnowledgeBuildService,
    KnowledgeLocalReadiness,
    SqliteKnowledgeStore,
)
from _knowledge_build_testkit import (
    DeterministicIndex,
    DeterministicProvider,
    digest,
    limits,
    manifest_and_plan,
)


def _stage_validate(service, root, suffix):
    root.mkdir(exist_ok=True)
    raw, chunks, identity_input = manifest_and_plan(
        root, content=f"procedure {suffix}", manifest_id=f"manifest-{suffix}"
    )
    staged = service.stage(
        operation_key=BuildOperationKey(f"stage-{suffix}"), raw_manifest=raw,
        source_root=root, chunks=chunks, identity_input=identity_input,
        limits=limits(), required_capability_identity="embedding-capability-v1",
    ).record
    assert staged is not None
    service.validate(staged.build_identity, BuildOperationKey(f"validate-{suffix}"), maximum_probe_results=3)
    return staged


def test_restart_resolves_exact_active_build_not_newer_validated_build(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    index = DeterministicIndex()
    with SqliteKnowledgeStore(path) as store:
        service = KnowledgeBuildService(store, DeterministicProvider(), index)
        active = _stage_validate(service, tmp_path / "active", "active")
        service.activate(BuildActivationRequest(active.build_identity, ActivationOperationKey("activate-1"), 0))
        newer = _stage_validate(service, tmp_path / "newer", "newer")

    index.inspect_calls.clear()
    with SqliteKnowledgeStore(path) as reopened:
        readiness = KnowledgeBuildService(reopened, DeterministicProvider(), index).recover_active()
        assert readiness.status is KnowledgeLocalReadiness.READY
        assert readiness.active_build_identity == active.build_identity
        assert readiness.active_build_identity != newer.build_identity
        assert index.inspect_calls == [active.build_identity]


def test_missing_exact_active_artifact_is_repair_required_without_fallback(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    index = DeterministicIndex()
    with SqliteKnowledgeStore(path) as store:
        service = KnowledgeBuildService(store, DeterministicProvider(), index)
        active = _stage_validate(service, tmp_path / "active", "active")
        service.activate(BuildActivationRequest(active.build_identity, ActivationOperationKey("activate-1"), 0))
        newer = _stage_validate(service, tmp_path / "newer", "newer")
    del index.artifacts[active.build_identity]
    with SqliteKnowledgeStore(path) as reopened:
        readiness = KnowledgeBuildService(reopened, DeterministicProvider(), index).recover_active()
        assert readiness.status is KnowledgeLocalReadiness.REPAIR_REQUIRED
        assert newer.build_identity in index.artifacts


def test_corrupt_active_artifact_is_repair_required(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    index = DeterministicIndex()
    with SqliteKnowledgeStore(path) as store:
        service = KnowledgeBuildService(store, DeterministicProvider(), index)
        active = _stage_validate(service, tmp_path / "active", "active")
        service.activate(BuildActivationRequest(active.build_identity, ActivationOperationKey("activate-1"), 0))
    index.artifacts[active.build_identity] = replace(active.artifact, integrity_ok=False)
    with SqliteKnowledgeStore(path) as reopened:
        assert KnowledgeBuildService(reopened, DeterministicProvider(), index).recover_active().status is KnowledgeLocalReadiness.REPAIR_REQUIRED


def test_active_build_without_validation_is_repair_required(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    index = DeterministicIndex()
    with SqliteKnowledgeStore(path) as store:
        service = KnowledgeBuildService(store, DeterministicProvider(), index)
        root = tmp_path / "source"
        root.mkdir()
        raw, chunks, identity_input = manifest_and_plan(root)
        staged = service.stage(
            operation_key=BuildOperationKey("stage-without-validation"),
            raw_manifest=raw,
            source_root=root,
            chunks=chunks,
            identity_input=identity_input,
            limits=limits(),
            required_capability_identity="embedding-capability-v1",
        ).record
        assert staged is not None
        store.commit_activation(
            ActivationAuthorityRecord(
                1,
                ActivationOperationKey("primitive-activation"),
                staged.build_identity,
                digest("missing-validation"),
                digest("activation-result"),
            ),
            expected_generation=0,
        )
    with SqliteKnowledgeStore(path) as reopened:
        readiness = KnowledgeBuildService(
            reopened, DeterministicProvider(), index
        ).recover_active()
        assert readiness.status is KnowledgeLocalReadiness.REPAIR_REQUIRED


def test_valid_stage_and_validation_records_survive_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    index = DeterministicIndex()
    with SqliteKnowledgeStore(path) as store:
        staged = _stage_validate(
            KnowledgeBuildService(store, DeterministicProvider(), index),
            tmp_path / "source",
            "reopen",
        )
    with SqliteKnowledgeStore(path) as reopened:
        assert reopened.get_staged_build(staged.build_identity).value == staged
        assert reopened.get_build_validation(staged.build_identity).value.build_identity == staged.build_identity


def test_legitimate_empty_store_recovery_is_not_initialized(tmp_path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        service = KnowledgeBuildService(store, DeterministicProvider(), DeterministicIndex())
        assert service.recover_active().status is KnowledgeLocalReadiness.NOT_INITIALIZED
    finally:
        store.close()
