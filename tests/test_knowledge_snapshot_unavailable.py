import pytest

from knowledge_index import (
    KnowledgeValidationError,
    RetrievalFailureCode,
    RetrievalFailureFact,
    KnowledgeReadStatus,
    RetrievalOperationState,
    RetrievalResolution,
    SnapshotSourceStatus,
    RetrySafetyDisposition,
    TerminalUnavailableRequest,
)
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import request
from _knowledge_snapshot_testkit import environment, terminal_request


def test_transient_unavailable_records_recovery_without_snapshot(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path, provider_error=RuntimeError("offline"))
    outcome = service.resolve(request(), limits())
    assert outcome.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE
    assert outcome.snapshot is None
    read = store.get_retrieval_operation_read(request().operation_key)
    assert read.status is KnowledgeReadStatus.FOUND
    assert read.value.state is RetrievalOperationState.TRANSIENT_UNAVAILABLE
    assert read.value.snapshot_key is None
    store.close()


def test_authoritative_terminal_unavailable_creates_one_snapshot(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path, provider_error=RuntimeError("offline"))
    service.resolve(request(), limits())
    finalized = service.finalize_unavailable(terminal_request())
    assert finalized.snapshot is not None
    assert finalized.snapshot.source_status is SnapshotSourceStatus.UNAVAILABLE
    assert finalized.snapshot.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE
    assert finalized.snapshot.chunks == ()
    assert service.finalize_unavailable(terminal_request()).snapshot == finalized.snapshot
    store.close()


def test_contradictory_terminal_finalization_fails_closed(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path)
    service.resolve(request(), limits())
    with pytest.raises(ValueError):
        service.finalize_unavailable(terminal_request())
    store.close()


def test_invalid_integrity_fact_cannot_be_finalized_as_unavailable() -> None:
    baseline = terminal_request()
    with pytest.raises(KnowledgeValidationError):
        TerminalUnavailableRequest(
            baseline.finalization_key,
            baseline.operation_key,
            baseline.authority_reference,
            (RetrievalFailureFact(
                RetrievalFailureCode.FROZEN_BUILD_INVALID,
                "authority",
                "frozen lineage is invalid",
                RetrySafetyDisposition.DO_NOT_RETRY,
            ),),
        )
