from dataclasses import FrozenInstanceError

import pytest

from knowledge_index import (
    KnowledgeResolutionOutcome,
    KnowledgeValidationError,
    RetrievalOperationState,
    RetrievalResolution,
    SnapshotFinalizationKey,
    SnapshotSourceStatus,
)


def test_slice_five_contract_vocabularies_are_closed() -> None:
    assert {item.value for item in SnapshotSourceStatus} == {"AVAILABLE", "UNAVAILABLE"}
    assert {item.value for item in RetrievalOperationState} == {
        "FROZEN", "TRANSIENT_UNAVAILABLE", "RETRIEVAL_COMPLETED", "COMPLETED"
    }


def test_snapshot_finalization_key_is_frozen_bounded_and_non_secret() -> None:
    key = SnapshotFinalizationKey("finalize-1")
    with pytest.raises(FrozenInstanceError):
        key.value = "changed"  # type: ignore[misc]
    with pytest.raises(KnowledgeValidationError):
        SnapshotFinalizationKey("api_key")


def test_completed_outcome_requires_snapshot() -> None:
    with pytest.raises(KnowledgeValidationError):
        KnowledgeResolutionOutcome(RetrievalResolution.MATCH)
