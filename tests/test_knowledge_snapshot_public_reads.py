from knowledge_index import (
    KnowledgeLocalReadiness,
    KnowledgeReadStatus,
    KnowledgeSnapshotKey,
    RetrievalOperationKey,
    RetrievalOperationState,
)
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import request
from _knowledge_snapshot_testkit import environment


def test_public_operation_snapshot_and_provenance_reads_are_typed(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path)
    snapshot = service.resolve(request(), limits()).snapshot
    assert snapshot is not None
    operation = service.read_operation(request().operation_key)
    assert operation.status is KnowledgeReadStatus.FOUND
    assert operation.value.state is RetrievalOperationState.COMPLETED
    assert service.read_snapshot(snapshot.snapshot_key).value == snapshot
    legacy = service.read_provenance(snapshot.snapshot_key)
    assert legacy.status is KnowledgeReadStatus.REPAIR_REQUIRED
    assert service.read_snapshot(snapshot.snapshot_key).value == snapshot
    assert service.read_operation(RetrievalOperationKey("missing")).status is KnowledgeReadStatus.NOT_FOUND
    assert service.read_provenance(KnowledgeSnapshotKey("missing")).status is KnowledgeReadStatus.NOT_FOUND
    assert store.local_readiness().status is KnowledgeLocalReadiness.READY
    store.close()
    assert service.read_provenance(snapshot.snapshot_key).status is KnowledgeReadStatus.UNAVAILABLE


def test_invalid_public_read_key_type_has_real_typed_path(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path)
    try:
        assert service.read_snapshot(request().operation_key).status is KnowledgeReadStatus.INVALID
        assert service.read_provenance(request().operation_key).status is KnowledgeReadStatus.INVALID
        assert service.read_operation(KnowledgeSnapshotKey("wrong-key-type")).status is KnowledgeReadStatus.INVALID
    finally:
        store.close()


def test_local_readiness_reports_authoritative_compatibility_mismatch(tmp_path) -> None:
    store, _, _, _, _ = environment(tmp_path)
    try:
        readiness = store.local_readiness(required_capability_identity="different-capability-v1")
        assert readiness.status is KnowledgeLocalReadiness.MISMATCH
        assert readiness.findings[0].code == "COMPATIBILITY_MISMATCH"
        assert store.local_readiness(
            required_capability_identity="embedding-capability-v1"
        ).status is KnowledgeLocalReadiness.READY
    finally:
        store.close()
