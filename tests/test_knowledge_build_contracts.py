from dataclasses import FrozenInstanceError, replace

import pytest

from knowledge_index import (
    ArtifactTrust,
    BuildFailureCode,
    BuildOperationKey,
    BuildStageState,
    BuildValidationState,
    EmbeddingProviderPort,
    KnowledgeValidationError,
    ProviderInvocationLimits,
    RetrySafetyDisposition,
    StagedIndexPort,
    build_identity,
)
from _knowledge_build_testkit import (
    DeterministicIndex, DeterministicProvider, capability, manifest_and_plan,
)


def test_slice_three_vocabularies_are_closed() -> None:
    assert {item.value for item in BuildStageState} == {"STAGED"}
    assert {item.value for item in BuildValidationState} == {"VALIDATED", "FAILED"}
    assert {item.value for item in ArtifactTrust} == {"APPROVED", "TEST_ONLY"}
    assert {item.value for item in RetrySafetyDisposition} == {
        "SAME_OPERATION_ONLY", "DO_NOT_RETRY", "EXTERNAL_AUTHORIZATION_REQUIRED"
    }
    assert BuildFailureCode.ACTIVATION_INELIGIBLE.value == "ACTIVATION_INELIGIBLE"


def test_build_operation_key_is_frozen_bounded_and_non_secret() -> None:
    key = BuildOperationKey("build-op-1")
    with pytest.raises(FrozenInstanceError):
        key.value = "changed"
    for value in ("", "api_key", "token"):
        with pytest.raises(KnowledgeValidationError):
            BuildOperationKey(value)


def test_invocation_limits_are_positive_and_bounded_contract_values() -> None:
    limits = ProviderInvocationLimits(1.0, 100, 2, 1, 10, 10, 10, 10)
    assert limits.maximum_invocations == 1
    with pytest.raises(KnowledgeValidationError):
        ProviderInvocationLimits(0, 100, 2, 1, 10, 10, 10, 10)


def test_provider_capability_requires_non_secret_opaque_profile() -> None:
    assert capability().profile_reference.value == "profile-approved-1"
    with pytest.raises(KnowledgeValidationError):
        capability(profile_reference="raw-secret")


def test_test_adapters_implement_candidate_c_owned_ports() -> None:
    assert isinstance(DeterministicProvider(), EmbeddingProviderPort)
    assert isinstance(DeterministicIndex(), StagedIndexPort)


def test_build_contract_v2_changes_new_identity_without_changing_v1(tmp_path) -> None:
    _, _, legacy = manifest_and_plan(tmp_path)
    legacy_identity = build_identity(legacy)
    governed = replace(legacy, build_contract_version="spec014-build-contract-v2")
    assert legacy_identity == "kbld_9c8a8c6a1a0f3dec9a90f48bcadabd8f53dcd7301547000bdbc83d2fb684e79c"
    assert build_identity(governed) != legacy_identity
