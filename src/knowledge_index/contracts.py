"""Immutable contracts for the SPEC-014 governed Knowledge foundation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Protocol, runtime_checkable


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


# Slice 2 durable contracts deliberately describe Candidate-C persistence facts only.
class KnowledgeReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class KnowledgeLocalReadiness(str, Enum):
    READY = "READY"
    NOT_INITIALIZED = "NOT_INITIALIZED"
    UNAVAILABLE = "UNAVAILABLE"
    MISMATCH = "MISMATCH"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class RetentionSubjectKind(str, Enum):
    BUILD = "BUILD"
    SNAPSHOT = "SNAPSHOT"
    CONTENT = "CONTENT"


class RetentionObligationKind(str, Enum):
    PUBLISHED_RCA = "PUBLISHED_RCA"
    MATERIAL_FAILED_ATTEMPT = "MATERIAL_FAILED_ATTEMPT"
    OUTSTANDING_OPERATION = "OUTSTANDING_OPERATION"
    RECONCILIATION_RECOVERY = "RECONCILIATION_RECOVERY"


class RetentionHoldStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


def _durable_key(value: object, field: str) -> str:
    result = _identifier(value, field)
    if _contains_secret_shape(result, identity_value=True):
        raise KnowledgeValidationError(f"{field} must be non-secret")
    return result


@dataclass(frozen=True, slots=True)
class ActivationOperationKey:
    value: str

    def __post_init__(self) -> None:
        _durable_key(self.value, "activation operation key")


@dataclass(frozen=True, slots=True)
class RetrievalOperationKey:
    value: str

    def __post_init__(self) -> None:
        _durable_key(self.value, "retrieval operation key")


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshotKey:
    value: str

    def __post_init__(self) -> None:
        _durable_key(self.value, "Knowledge Snapshot key")


@dataclass(frozen=True, slots=True)
class RetentionHoldKey:
    value: str

    def __post_init__(self) -> None:
        _durable_key(self.value, "retention hold key")


@dataclass(frozen=True, slots=True)
class BuildLineageRecord:
    build_identity: str
    manifest_commitment: str
    lineage_commitment: str
    record_version: int = 1

    def __post_init__(self) -> None:
        _typed_identity(self.build_identity, "kbld", "build_identity")
        _typed_identity(self.manifest_commitment, "kmf", "manifest_commitment")
        _hash(self.lineage_commitment, "lineage_commitment")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported build lineage record version")


@dataclass(frozen=True, slots=True)
class ActivationAuthorityRecord:
    generation: int
    operation_key: ActivationOperationKey
    active_build_identity: str
    validated_build_commitment: str
    result_commitment: str
    record_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise KnowledgeValidationError("activation generation must be a positive integer")
        if not isinstance(self.operation_key, ActivationOperationKey):
            raise KnowledgeValidationError("operation_key must be an ActivationOperationKey")
        _typed_identity(self.active_build_identity, "kbld", "active_build_identity")
        _hash(self.validated_build_commitment, "validated_build_commitment")
        _hash(self.result_commitment, "result_commitment")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported activation record version")


@dataclass(frozen=True, slots=True)
class OperationEnvelope:
    operation_key: RetrievalOperationKey
    semantic_commitment: str
    frozen_build_identity: str | None = None
    snapshot_key: KnowledgeSnapshotKey | None = None
    completed: bool = False
    revision: int = 1
    record_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.operation_key, RetrievalOperationKey):
            raise KnowledgeValidationError("operation_key must be a RetrievalOperationKey")
        _hash(self.semantic_commitment, "semantic_commitment")
        if self.frozen_build_identity is not None:
            _typed_identity(self.frozen_build_identity, "kbld", "frozen_build_identity")
        if self.snapshot_key is not None and not isinstance(self.snapshot_key, KnowledgeSnapshotKey):
            raise KnowledgeValidationError("snapshot_key must be a KnowledgeSnapshotKey")
        if not isinstance(self.completed, bool):
            raise KnowledgeValidationError("completed must be boolean")
        if self.completed != (self.snapshot_key is not None):
            raise KnowledgeValidationError("completed operation and snapshot_key must agree")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise KnowledgeValidationError("operation revision must be positive")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported operation record version")


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshotEnvelope:
    snapshot_key: KnowledgeSnapshotKey
    operation_key: RetrievalOperationKey
    frozen_build_identity: str
    snapshot_commitment: str
    lineage_commitment: str
    record_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_key, KnowledgeSnapshotKey):
            raise KnowledgeValidationError("snapshot_key must be a KnowledgeSnapshotKey")
        if not isinstance(self.operation_key, RetrievalOperationKey):
            raise KnowledgeValidationError("operation_key must be a RetrievalOperationKey")
        _typed_identity(self.frozen_build_identity, "kbld", "frozen_build_identity")
        _hash(self.snapshot_commitment, "snapshot_commitment")
        _hash(self.lineage_commitment, "lineage_commitment")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported Snapshot envelope record version")


@dataclass(frozen=True, slots=True)
class RetentionHoldRecord:
    hold_key: RetentionHoldKey
    subject_kind: RetentionSubjectKind
    subject_identity: str
    owner: OpaqueExternalReference
    obligation_kind: RetentionObligationKind
    semantic_commitment: str
    status: RetentionHoldStatus = RetentionHoldStatus.ACTIVE
    revision: int = 1
    record_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.hold_key, RetentionHoldKey):
            raise KnowledgeValidationError("hold_key must be a RetentionHoldKey")
        if not isinstance(self.subject_kind, RetentionSubjectKind):
            raise KnowledgeValidationError("subject_kind must be a RetentionSubjectKind")
        _durable_key(self.subject_identity, "retention subject identity")
        if self.subject_kind is RetentionSubjectKind.BUILD:
            _typed_identity(self.subject_identity, "kbld", "retention build identity")
        if not isinstance(self.owner, OpaqueExternalReference):
            raise KnowledgeValidationError("owner must be an OpaqueExternalReference")
        if not isinstance(self.obligation_kind, RetentionObligationKind):
            raise KnowledgeValidationError("obligation_kind must be a RetentionObligationKind")
        _hash(self.semantic_commitment, "semantic_commitment")
        if not isinstance(self.status, RetentionHoldStatus):
            raise KnowledgeValidationError("status must be a RetentionHoldStatus")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise KnowledgeValidationError("retention revision must be positive")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported retention record version")


@dataclass(frozen=True, slots=True)
class KnowledgeCorruptionFinding:
    code: str
    record_kind: str
    record_key: str
    detail: str

    def __post_init__(self) -> None:
        for field in ("code", "record_kind", "record_key"):
            _durable_key(getattr(self, field), field)
        if (
            not isinstance(self.detail, str)
            or not self.detail
            or self.detail != self.detail.strip()
            or len(self.detail) > 256
            or _contains_secret_shape(self.detail)
        ):
            raise KnowledgeValidationError("corruption detail must be bounded and non-secret")


@dataclass(frozen=True, slots=True)
class KnowledgeReadResult:
    status: KnowledgeReadStatus
    value: object | None = None
    findings: tuple[KnowledgeCorruptionFinding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, KnowledgeReadStatus):
            raise KnowledgeValidationError("status must be a KnowledgeReadStatus")
        if self.status is KnowledgeReadStatus.FOUND and self.value is None:
            raise KnowledgeValidationError("FOUND requires a value")
        if self.status is not KnowledgeReadStatus.FOUND and self.value is not None:
            raise KnowledgeValidationError("non-FOUND result cannot contain a value")
        if any(not isinstance(item, KnowledgeCorruptionFinding) for item in self.findings):
            raise KnowledgeValidationError("findings must contain KnowledgeCorruptionFinding values")


@dataclass(frozen=True, slots=True)
class KnowledgeReadinessFact:
    status: KnowledgeLocalReadiness
    active_build_identity: str | None = None
    generation: int | None = None
    findings: tuple[KnowledgeCorruptionFinding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, KnowledgeLocalReadiness):
            raise KnowledgeValidationError("status must be a KnowledgeLocalReadiness")
        if self.active_build_identity is not None:
            _typed_identity(self.active_build_identity, "kbld", "active_build_identity")
        if self.generation is not None and (
            isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1
        ):
            raise KnowledgeValidationError("generation must be positive")
        if self.status is KnowledgeLocalReadiness.READY and (
            self.active_build_identity is None or self.generation is None or self.findings
        ):
            raise KnowledgeValidationError("READY requires exact active authority without findings")


@runtime_checkable
class KnowledgePersistence(Protocol):
    """Public Candidate-C persistence boundary, without workflow policy."""

    def get_build_lineage(self, build_identity: str) -> KnowledgeReadResult: ...

    def read_activation(self) -> KnowledgeReadResult: ...

    def local_readiness(self) -> KnowledgeReadinessFact: ...
