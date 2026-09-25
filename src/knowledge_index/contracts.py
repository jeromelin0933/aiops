"""Immutable contracts for the SPEC-014 governed Knowledge foundation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_SECRET_KEY = re.compile(
    r"(?i)(?:access[_-]?token|api[_-]?key|authorization|credential|password|passwd|secret|token)"
)
_SECRET_SHAPE = re.compile(
    rf"{_SECRET_KEY.pattern}\s*(?:=|:)"
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
    r"|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
)


def _contains_secret_shape(
    value: object, *, metadata_key: bool = False, identity_value: bool = False
) -> bool:
    """Candidate-C-local secret-shape rule; never a shared security authority."""
    if not isinstance(value, str):
        return False
    if _SECRET_SHAPE.search(value):
        return True
    if metadata_key and _SECRET_KEY.search(value):
        return True
    return identity_value and _SECRET_KEY.fullmatch(value) is not None


class KnowledgeValidationError(ValueError):
    """A malformed Candidate-C contract boundary."""


class IdentityNamespace(str, Enum):
    MANIFEST = "KNOWLEDGE_MANIFEST"
    DOCUMENT = "KNOWLEDGE_DOCUMENT"
    DOCUMENT_VERSION = "KNOWLEDGE_DOCUMENT_VERSION"
    CHUNK = "KNOWLEDGE_CHUNK"
    BUILD = "KNOWLEDGE_BUILD"


class DocumentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    REVOKED = "REVOKED"
    UNAPPROVED = "UNAPPROVED"


class SourceClassification(str, Enum):
    APPROVED_OPERATIONAL_KNOWLEDGE = "APPROVED_OPERATIONAL_KNOWLEDGE"
    SCENARIO = "SCENARIO"
    GROUND_TRUTH = "GROUND_TRUTH"
    VALIDATOR_OUTPUT = "VALIDATOR_OUTPUT"
    EVALUATION_TRUTH = "EVALUATION_TRUTH"
    GENERATED_RCA = "GENERATED_RCA"
    SHADOW = "SHADOW"
    UNREVIEWED_INCIDENT = "UNREVIEWED_INCIDENT"
    UNSAFE_UNAPPROVED = "UNSAFE_UNAPPROVED"


class ContentType(str, Enum):
    TEXT_UTF8 = "text/plain; charset=utf-8"


class OpaqueReferenceType(str, Enum):
    CALLER = "CALLER"
    RCA_ATTEMPT = "RCA_ATTEMPT"
    EVIDENCE_SNAPSHOT = "EVIDENCE_SNAPSHOT"
    CREDENTIAL_PROFILE = "CREDENTIAL_PROFILE"


class AdmissionFailureCode(str, Enum):
    INVALID_MANIFEST = "INVALID_MANIFEST"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    UNSUPPORTED_FIELD = "UNSUPPORTED_FIELD"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_IDENTITY = "INVALID_IDENTITY"
    DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
    CONTRADICTORY_ENTRY = "CONTRADICTORY_ENTRY"
    UNSAFE_SOURCE_PATH = "UNSAFE_SOURCE_PATH"
    SOURCE_NOT_LISTED = "SOURCE_NOT_LISTED"
    SOURCE_MISSING = "SOURCE_MISSING"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
    DOCUMENT_NOT_ACTIVE = "DOCUMENT_NOT_ACTIVE"
    SOURCE_CLASS_FORBIDDEN = "SOURCE_CLASS_FORBIDDEN"
    CONTENT_TYPE_UNSUPPORTED = "CONTENT_TYPE_UNSUPPORTED"
    SECRET_METADATA = "SECRET_METADATA"
    OUTBOUND_CONTENT_UNSAFE = "OUTBOUND_CONTENT_UNSAFE"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise KnowledgeValidationError(f"{field} must be a bounded canonical identifier")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise KnowledgeValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _typed_identity(value: object, prefix: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"{prefix}_[0-9a-f]{{64}}", value):
        raise KnowledgeValidationError(f"{field} must use the {prefix} identity namespace")
    return value


@dataclass(frozen=True, slots=True, order=True)
class MetadataItem:
    key: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not _METADATA_KEY.fullmatch(self.key):
            raise KnowledgeValidationError("metadata key must use the canonical bounded vocabulary")
        if (
            not isinstance(self.value, str)
            or not self.value
            or self.value != self.value.strip()
            or len(self.value) > 512
        ):
            raise KnowledgeValidationError("metadata value must be non-empty, trimmed, and bounded")


@dataclass(frozen=True, slots=True)
class OpaqueExternalReference:
    reference_type: OpaqueReferenceType
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference_type, OpaqueReferenceType):
            raise KnowledgeValidationError("reference_type must be an OpaqueReferenceType")
        if not isinstance(self.value, str) or not _OPAQUE_VALUE.fullmatch(self.value):
            raise KnowledgeValidationError("opaque reference must be non-empty, canonical, and bounded")
        if _contains_secret_shape(self.value):
            raise KnowledgeValidationError("opaque reference must be non-secret")


@dataclass(frozen=True, slots=True, order=True)
class AdmissionFailureFact:
    code: AdmissionFailureCode
    field: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, AdmissionFailureCode):
            raise KnowledgeValidationError("failure code must be an AdmissionFailureCode")
        _identifier(self.field, "failure field")
        if (
            not isinstance(self.detail, str)
            or not self.detail
            or self.detail != self.detail.strip()
            or len(self.detail) > 256
            or _contains_secret_shape(self.detail)
        ):
            raise KnowledgeValidationError("failure detail must be bounded, non-empty, and non-secret")


@dataclass(frozen=True, slots=True)
class ManifestDocument:
    document_id: str
    document_version: str
    source_path: str
    expected_content_hash: str
    status: DocumentStatus
    source_classification: SourceClassification
    approval_reference: str
    outbound_eligible: bool
    content_type: ContentType
    metadata: tuple[MetadataItem, ...]

    def __post_init__(self) -> None:
        _identifier(self.document_id, "document_id")
        _identifier(self.document_version, "document_version")
        if not isinstance(self.source_path, str) or not self.source_path or len(self.source_path) > 512:
            raise KnowledgeValidationError("source_path must be a non-empty bounded string")
        _hash(self.expected_content_hash, "expected_content_hash")
        if not isinstance(self.status, DocumentStatus):
            raise KnowledgeValidationError("status must be a DocumentStatus")
        if not isinstance(self.source_classification, SourceClassification):
            raise KnowledgeValidationError("source_classification must be a SourceClassification")
        _identifier(self.approval_reference, "approval_reference")
        if not isinstance(self.outbound_eligible, bool):
            raise KnowledgeValidationError("outbound_eligible must be boolean")
        if not isinstance(self.content_type, ContentType):
            raise KnowledgeValidationError("content_type must be a ContentType")
        if not isinstance(self.metadata, tuple) or any(
            not isinstance(item, MetadataItem) for item in self.metadata
        ):
            raise KnowledgeValidationError("metadata must be a tuple of MetadataItem")
        if tuple(sorted(self.metadata)) != self.metadata or len(set(self.metadata)) != len(self.metadata):
            raise KnowledgeValidationError("metadata must be uniquely key-sorted")
        if len({item.key for item in self.metadata}) != len(self.metadata):
            raise KnowledgeValidationError("metadata keys must be unique")


@dataclass(frozen=True, slots=True)
class GovernedManifest:
    schema_version: str
    canonicalization_version: str
    manifest_id: str
    corpus_id: str
    corpus_version: str
    documents: tuple[ManifestDocument, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise KnowledgeValidationError("unsupported manifest schema version")
        if self.canonicalization_version != "1.0":
            raise KnowledgeValidationError("unsupported canonicalization version")
        _identifier(self.manifest_id, "manifest_id")
        _identifier(self.corpus_id, "corpus_id")
        _identifier(self.corpus_version, "corpus_version")
        if not isinstance(self.documents, tuple) or not self.documents:
            raise KnowledgeValidationError("manifest documents must be a non-empty tuple")
        if any(not isinstance(document, ManifestDocument) for document in self.documents):
            raise KnowledgeValidationError("manifest documents must contain ManifestDocument values")


@dataclass(frozen=True, slots=True)
class ChunkIdentityInput:
    document_identity: str
    document_version_identity: str
    section_identity: str
    ordinal: int
    content_hash: str
    chunking_profile_identity: str

    def __post_init__(self) -> None:
        _typed_identity(self.document_identity, "kdoc", "document_identity")
        _typed_identity(self.document_version_identity, "kver", "document_version_identity")
        _identifier(self.section_identity, "section_identity")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise KnowledgeValidationError("ordinal must be a non-negative integer")
        _hash(self.content_hash, "content_hash")
        _identifier(self.chunking_profile_identity, "chunking_profile_identity")


@dataclass(frozen=True, slots=True)
class BuildIdentityInput:
    manifest_commitment: str
    ordered_chunk_identities: tuple[str, ...]
    chunking_profile_identity: str
    canonicalization_version: str
    embedding_provider: str
    embedding_model: str
    embedding_profile_identity: str
    embedding_dimension: int
    normalization_semantics: str
    index_engine: str
    index_schema_identity: str
    metadata_schema_identity: str
    build_contract_version: str

    def __post_init__(self) -> None:
        _typed_identity(self.manifest_commitment, "kmf", "manifest_commitment")
        if not isinstance(self.ordered_chunk_identities, tuple) or not self.ordered_chunk_identities:
            raise KnowledgeValidationError("ordered_chunk_identities must be a non-empty tuple")
        for identity in self.ordered_chunk_identities:
            _typed_identity(identity, "kchk", "ordered_chunk_identity")
        if len(set(self.ordered_chunk_identities)) != len(self.ordered_chunk_identities):
            raise KnowledgeValidationError("ordered_chunk_identities must be unique")
        for field in (
            "chunking_profile_identity",
            "canonicalization_version",
            "embedding_provider",
            "embedding_model",
            "embedding_profile_identity",
            "normalization_semantics",
            "index_engine",
            "index_schema_identity",
            "metadata_schema_identity",
            "build_contract_version",
        ):
            _identifier(getattr(self, field), field)
        if (
            isinstance(self.embedding_dimension, bool)
            or not isinstance(self.embedding_dimension, int)
            or self.embedding_dimension <= 0
        ):
            raise KnowledgeValidationError("embedding_dimension must be a positive integer")


@dataclass(frozen=True, slots=True)
class AdmittedDocument:
    document_identity: str
    document_version_identity: str
    source_path: str
    content_hash: str

    def __post_init__(self) -> None:
        _identifier(self.document_identity, "document_identity")
        _identifier(self.document_version_identity, "document_version_identity")
        if not isinstance(self.source_path, str) or not self.source_path or len(self.source_path) > 512:
            raise KnowledgeValidationError("source_path must be non-empty and bounded")
        _hash(self.content_hash, "content_hash")


@dataclass(frozen=True, slots=True)
class ManifestAdmissionResult:
    accepted: bool
    manifest: GovernedManifest | None
    manifest_commitment: str | None
    admitted_documents: tuple[AdmittedDocument, ...]
    findings: tuple[AdmissionFailureFact, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise KnowledgeValidationError("accepted must be boolean")
        if any(not isinstance(item, AdmittedDocument) for item in self.admitted_documents):
            raise KnowledgeValidationError("admitted_documents contains an invalid value")
        if any(not isinstance(item, AdmissionFailureFact) for item in self.findings):
            raise KnowledgeValidationError("findings contains an invalid value")
        if self.accepted:
            if self.manifest is None or self.manifest_commitment is None or self.findings:
                raise KnowledgeValidationError("accepted admission requires manifest and no findings")
            _identifier(self.manifest_commitment, "manifest_commitment")
        elif self.manifest_commitment is not None or self.admitted_documents:
            raise KnowledgeValidationError("rejected admission cannot publish a commitment or documents")
