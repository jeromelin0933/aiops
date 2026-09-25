from dataclasses import FrozenInstanceError

import pytest

from knowledge_index import (
    ActivationOperationKey,
    KnowledgeSnapshotKey,
    KnowledgeValidationError,
    RetrievalOperationKey,
    RetrievalProfile,
    RawRetrievalCandidate,
    QueryFilter,
    RetrievalQueryDisposition,
)
from _knowledge_retrieval_testkit import profile, request


def test_retrieval_contracts_are_frozen_and_identity_is_separate() -> None:
    value = request()
    with pytest.raises(FrozenInstanceError):
        value.capability_identity = "different"
    assert RetrievalOperationKey("same") != ActivationOperationKey("same")
    assert RetrievalOperationKey("same") != KnowledgeSnapshotKey("same")


def test_query_profile_and_policy_are_bounded() -> None:
    with pytest.raises(KnowledgeValidationError):
        profile(top_k=9, candidate_limit=8)
    with pytest.raises(KnowledgeValidationError):
        __import__("dataclasses").replace(request().query, text="x" * 16385)


def test_profile_is_versioned_and_closed() -> None:
    value = profile()
    assert isinstance(value, RetrievalProfile)
    assert value.version == "v1"
    assert value.empty_query_disposition is RetrievalQueryDisposition.INVALID
    assert value.unsupported_query_disposition is RetrievalQueryDisposition.INVALID
    assert {item.value for item in RetrievalQueryDisposition} == {"INVALID"}


def test_secret_shaped_retrieval_inputs_fail_closed() -> None:
    with pytest.raises(KnowledgeValidationError):
        request(capability_identity="token:synthetic-secret")
    with pytest.raises(KnowledgeValidationError):
        QueryFilter("api_key", "synthetic")


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_candidate_score_fails_at_contract_boundary(score) -> None:
    with pytest.raises(KnowledgeValidationError):
        RawRetrievalCandidate("kchk_" + "a" * 64, score, "b" * 64)
