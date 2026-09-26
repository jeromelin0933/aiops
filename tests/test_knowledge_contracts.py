from dataclasses import FrozenInstanceError

import pytest

from knowledge_index import (
    AdmissionFailureCode,
    AdmissionFailureFact,
    DocumentStatus,
    IdentityNamespace,
    KnowledgeValidationError,
    MetadataItem,
    OpaqueExternalReference,
    OpaqueReferenceType,
    SourceClassification,
)


def test_slice_one_contracts_are_frozen_and_vocabularies_are_closed() -> None:
    item = MetadataItem("service", "identity")
    with pytest.raises(FrozenInstanceError):
        item.value = "other"  # type: ignore[misc]
    assert set(DocumentStatus) == {
        DocumentStatus.ACTIVE,
        DocumentStatus.RETIRED,
        DocumentStatus.REVOKED,
        DocumentStatus.UNAPPROVED,
    }
    assert set(IdentityNamespace) == {
        IdentityNamespace.MANIFEST,
        IdentityNamespace.DOCUMENT,
        IdentityNamespace.DOCUMENT_VERSION,
        IdentityNamespace.CHUNK,
        IdentityNamespace.BUILD,
    }
    assert SourceClassification.APPROVED_OPERATIONAL_KNOWLEDGE.value == "APPROVED_OPERATIONAL_KNOWLEDGE"


def test_failure_facts_are_structured_bounded_and_secret_free() -> None:
    fact = AdmissionFailureFact(
        AdmissionFailureCode.SOURCE_MISSING,
        "document.source_path",
        "governed source is missing",
    )
    assert fact.code is AdmissionFailureCode.SOURCE_MISSING
    with pytest.raises(KnowledgeValidationError):
        AdmissionFailureFact(
            AdmissionFailureCode.SECRET_METADATA,
            "metadata",
            "api_key=do-not-disclose",
        )
    with pytest.raises(KnowledgeValidationError):
        AdmissionFailureFact(
            AdmissionFailureCode.INVALID_MANIFEST,
            "manifest",
            "x" * 257,
        )


def test_opaque_references_are_type_separated_without_foreign_payload_parsing() -> None:
    evidence = OpaqueExternalReference(OpaqueReferenceType.EVIDENCE_SNAPSHOT, "EVS-42")
    attempt = OpaqueExternalReference(OpaqueReferenceType.RCA_ATTEMPT, "EVS-42")
    assert evidence != attempt
    assert evidence.value == "EVS-42"
    with pytest.raises(KnowledgeValidationError):
        OpaqueExternalReference("EVIDENCE_SNAPSHOT", "EVS-42")  # type: ignore[arg-type]
    with pytest.raises(KnowledgeValidationError):
        OpaqueExternalReference(OpaqueReferenceType.CALLER, "")
    with pytest.raises(KnowledgeValidationError):
        OpaqueExternalReference(OpaqueReferenceType.CALLER, "x" * 513)


def test_opaque_reference_rejects_secret_shaped_values() -> None:
    for value in ("api_key:secret", "token=secret", "credential:secret"):
        with pytest.raises(KnowledgeValidationError):
            OpaqueExternalReference(OpaqueReferenceType.CREDENTIAL_PROFILE, value)


def test_safe_non_secret_opaque_profile_reference_remains_valid() -> None:
    reference = OpaqueExternalReference(
        OpaqueReferenceType.CREDENTIAL_PROFILE, "PROFILE-EMBEDDING-1"
    )
    assert reference.value == "PROFILE-EMBEDDING-1"
