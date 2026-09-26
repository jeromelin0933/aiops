"""Immutable contracts for the SPEC-014 governed Knowledge foundation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import math
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
    rf"{_SECRET_KEY.pattern}[ \t]*(?:=|:)[ \t]*\S+"
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
    r"|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
)
_MULTILINE_SECRET_SHAPE = re.compile(
    r"(?im)^[ \t]*(?:access[_-]?token|api[_-]?key|authorization|credential|"
    r"password|passwd|secret|token)[ \t]*:[ \t]*\r?\n[ \t]*"
    r"(?:(?:bearer|basic)[ \t]+[A-Za-z0-9._~+/=-]+|"
    r"[A-Za-z0-9][A-Za-z0-9._~+/=-]{7,})[ \t]*\r?$"
)


def _contains_secret_shape(
    value: object, *, metadata_key: bool = False, identity_value: bool = False
) -> bool:
    """Candidate-C-local secret-shape rule; never a shared security authority."""
    if not isinstance(value, str):
        return False
    if _SECRET_SHAPE.search(value) or _MULTILINE_SECRET_SHAPE.search(value):
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
    RELEASE_NOT_APPROVED = "RELEASE_NOT_APPROVED"
    RELEASE_MEMBERSHIP_INVALID = "RELEASE_MEMBERSHIP_INVALID"
    GOVERNANCE_METADATA_INVALID = "GOVERNANCE_METADATA_INVALID"
    SOURCE_REVISION_MISMATCH = "SOURCE_REVISION_MISMATCH"
    CONTENT_NOT_MEANINGFUL = "CONTENT_NOT_MEANINGFUL"


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
    approval_state: str = "APPROVED"
    production_eligible: bool = True
    committed_source_reference: str = ""
    security_classification: str = ""
    outbound_scope: str = ""
    knowledge_type: str = ""
    guidance_authority: str = ""

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
        if self.approval_state not in {"APPROVED", "UNAPPROVED"}:
            raise KnowledgeValidationError("approval_state is invalid")
        if not isinstance(self.production_eligible, bool):
            raise KnowledgeValidationError("production_eligible must be boolean")
        for field in (
            "committed_source_reference", "security_classification", "outbound_scope",
            "knowledge_type", "guidance_authority",
        ):
            value = getattr(self, field)
            if value:
                _identifier(value, field)


@dataclass(frozen=True, slots=True)
class GovernedManifest:
    schema_version: str
    canonicalization_version: str
    manifest_id: str
    corpus_id: str
    corpus_version: str
    documents: tuple[ManifestDocument, ...]
    schema_identity: str = "KNOWLEDGE_MANIFEST"
    release_id: str = ""
    release_version: str = ""
    canonical_release_reference: str = ""
    release_status: str = ""
    release_approver_role: str = ""
    release_approval_reference: str = ""
    governed_source_root: str = ""
    source_revision: str = ""
    content_hash_algorithm: str = ""
    metadata_schema_identity: str = ""
    metadata_schema_version: str = ""
    metadata_vocabulary: tuple[MetadataItem, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version not in {"1.0", "1.1"}:
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
        if self.schema_identity != "KNOWLEDGE_MANIFEST":
            raise KnowledgeValidationError("unsupported manifest schema identity")
        if self.schema_version == "1.1":
            for field in (
                "release_id", "release_version", "canonical_release_reference",
                "release_status", "release_approver_role", "release_approval_reference",
                "content_hash_algorithm", "metadata_schema_identity",
                "metadata_schema_version",
            ):
                value = getattr(self, field)
                if (
                    not isinstance(value, str) or not value or value != value.strip()
                    or len(value) > 512 or _contains_secret_shape(value)
                ):
                    raise KnowledgeValidationError(f"{field} must be bounded and non-secret")
            if (
                not self.governed_source_root
                or self.governed_source_root.startswith(("/", "\\"))
                or ".." in self.governed_source_root.replace("\\", "/").split("/")
            ):
                raise KnowledgeValidationError("governed_source_root is invalid")
            if not re.fullmatch(r"[0-9a-f]{40}", self.source_revision):
                raise KnowledgeValidationError("source_revision must be a full Git object id")
            if not isinstance(self.metadata_vocabulary, tuple) or not self.metadata_vocabulary:
                raise KnowledgeValidationError("metadata_vocabulary is required")


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
class SnapshotFinalizationKey:
    value: str

    def __post_init__(self) -> None:
        _durable_key(self.value, "Snapshot finalization key")


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
    schema_version: str
    resolution: RetrievalResolution
    source_status: SnapshotSourceStatus
    knowledge_gap: bool
    payload_truncated: bool
    activation_generation: int
    activation_operation_key: ActivationOperationKey
    manifest_commitment: str
    validation_commitment: str
    staged_commitment: str
    artifact_commitment: str
    query_commitment: str
    retrieval_profile_identity: str
    retrieval_profile_version: str
    applicability_policy_identity: str
    applicability_policy_version: str
    profile_reference: OpaqueExternalReference
    capability_identity: str
    external_references: tuple[OpaqueExternalReference, ...]
    creation_metadata: tuple[MetadataItem, ...]
    chunks: tuple[KnowledgeSnapshotChunk, ...]
    evaluations: tuple[RetrievalEvaluationFact, ...]
    failures: tuple[RetrievalFailureFact, ...] = ()
    finalization_key: SnapshotFinalizationKey | None = None
    finalization_reference: OpaqueExternalReference | None = None
    record_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_key, KnowledgeSnapshotKey):
            raise KnowledgeValidationError("snapshot_key must be a KnowledgeSnapshotKey")
        if not isinstance(self.operation_key, RetrievalOperationKey):
            raise KnowledgeValidationError("operation_key must be a RetrievalOperationKey")
        _typed_identity(self.frozen_build_identity, "kbld", "frozen_build_identity")
        _hash(self.snapshot_commitment, "snapshot_commitment")
        _hash(self.lineage_commitment, "lineage_commitment")
        if self.schema_version not in {"1.0", "1.1"}:
            raise KnowledgeValidationError("unsupported Knowledge Snapshot schema")
        if self.resolution not in (
            RetrievalResolution.MATCH,
            RetrievalResolution.NO_MATCH,
            RetrievalResolution.RETRIEVAL_UNAVAILABLE,
        ) or not isinstance(self.source_status, SnapshotSourceStatus):
            raise KnowledgeValidationError("Snapshot terminal vocabulary is invalid")
        if not isinstance(self.knowledge_gap, bool):
            raise KnowledgeValidationError("knowledge_gap must be boolean")
        if not isinstance(self.payload_truncated, bool):
            raise KnowledgeValidationError("payload_truncated must be boolean")
        if isinstance(self.activation_generation, bool) or not isinstance(self.activation_generation, int) or self.activation_generation < 1:
            raise KnowledgeValidationError("activation_generation must be positive")
        if not isinstance(self.activation_operation_key, ActivationOperationKey):
            raise KnowledgeValidationError("activation_operation_key is invalid")
        _typed_identity(self.manifest_commitment, "kmf", "manifest_commitment")
        for field in ("validation_commitment", "staged_commitment", "artifact_commitment", "query_commitment"):
            _hash(getattr(self, field), field)
        for field in (
            "retrieval_profile_identity", "retrieval_profile_version",
            "applicability_policy_identity", "applicability_policy_version",
            "capability_identity",
        ):
            _durable_key(getattr(self, field), field)
        if (
            not isinstance(self.profile_reference, OpaqueExternalReference)
            or self.profile_reference.reference_type is not OpaqueReferenceType.CREDENTIAL_PROFILE
        ):
            raise KnowledgeValidationError("Snapshot requires an opaque Credential Profile")
        if (
            not isinstance(self.external_references, tuple)
            or any(not isinstance(item, OpaqueExternalReference) for item in self.external_references)
            or len(self.external_references) > 16
        ):
            raise KnowledgeValidationError("Snapshot external references are invalid")
        if (
            not isinstance(self.creation_metadata, tuple)
            or not self.creation_metadata
            or any(not isinstance(item, MetadataItem) for item in self.creation_metadata)
            or tuple(sorted(self.creation_metadata)) != self.creation_metadata
            or len({item.key for item in self.creation_metadata}) != len(self.creation_metadata)
        ):
            raise KnowledgeValidationError("Snapshot creation metadata must be canonical and non-secret")
        if any(not isinstance(item, KnowledgeSnapshotChunk) for item in self.chunks):
            raise KnowledgeValidationError("Snapshot chunks are invalid")
        if any(not isinstance(item, RetrievalEvaluationFact) for item in self.evaluations):
            raise KnowledgeValidationError("Snapshot evaluations are invalid")
        if any(not isinstance(item, RetrievalFailureFact) for item in self.failures):
            raise KnowledgeValidationError("Snapshot failures are invalid")
        if self.resolution is RetrievalResolution.MATCH:
            if (
                self.source_status is not SnapshotSourceStatus.AVAILABLE or self.knowledge_gap
                or not self.chunks or self.failures or self.finalization_key is not None
                or self.finalization_reference is not None
                or not self.evaluations
                or tuple(item.chunk_identity for item in self.evaluations if item.included)
                != tuple(item.chunk_identity for item in self.chunks)
            ):
                raise KnowledgeValidationError("MATCH Snapshot shape is invalid")
        elif self.resolution is RetrievalResolution.NO_MATCH:
            if (
                self.source_status is not SnapshotSourceStatus.AVAILABLE or not self.knowledge_gap
                or self.chunks or self.failures or self.finalization_key is not None
                or self.finalization_reference is not None
                or self.payload_truncated
                or any(item.included for item in self.evaluations)
            ):
                raise KnowledgeValidationError("NO_MATCH Snapshot shape is invalid")
        else:
            if (
                self.source_status is not SnapshotSourceStatus.UNAVAILABLE
                or self.knowledge_gap or self.payload_truncated or self.chunks or not self.failures
                or self.evaluations
                or not isinstance(self.finalization_key, SnapshotFinalizationKey)
                or not isinstance(self.finalization_reference, OpaqueExternalReference)
            ):
                raise KnowledgeValidationError("unavailable Snapshot shape is invalid")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported Snapshot envelope record version")
        if self.schema_version == "1.1" and any(
            not item.knowledge_type
            or not item.guidance_authority
            or item.sop_backed_eligible is None
            for item in self.chunks
        ):
            raise KnowledgeValidationError("v2 Snapshot chunks require governance facts")


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

    def local_readiness(
        self,
        *,
        required_profile_reference: OpaqueExternalReference | None = None,
        required_capability_identity: str | None = None,
    ) -> KnowledgeReadinessFact: ...

    def claim_build_operation(self, claim: BuildOperationClaim) -> BuildOperationClaim: ...

    def get_build_operation_claim(self, operation_key: BuildOperationKey) -> KnowledgeReadResult: ...

    def create_staged_build(self, record: StagedBuildRecord) -> StagedBuildRecord: ...

    def get_staged_build(self, build_identity: str) -> KnowledgeReadResult: ...

    def create_build_validation(self, record: BuildValidationRecord) -> BuildValidationRecord: ...

    def get_build_validation(self, build_identity: str) -> KnowledgeReadResult: ...

    def commit_activation(
        self, record: ActivationAuthorityRecord, *, expected_generation: int
    ) -> ActivationAuthorityRecord: ...

    def freeze_retrieval_operation(
        self, request: RetrievalOperationRequest
    ) -> FrozenRetrievalOperation: ...

    def get_frozen_retrieval_operation(
        self, key: RetrievalOperationKey
    ) -> KnowledgeReadResult: ...

    def record_retrieval_completion(
        self, fact: DurableRetrievalCompletion
    ) -> DurableRetrievalCompletion: ...

    def get_retrieval_completion(
        self, key: RetrievalOperationKey
    ) -> KnowledgeReadResult: ...

    def complete_operation_with_snapshot(
        self, snapshot: KnowledgeSnapshotEnvelope, *, expected_revision: int
    ) -> OperationEnvelope: ...

    def get_snapshot(self, key: KnowledgeSnapshotKey) -> KnowledgeReadResult: ...

    def record_retrieval_recovery(
        self, fact: RetrievalRecoveryFact
    ) -> RetrievalRecoveryFact: ...

    def get_retrieval_operation_read(
        self, key: RetrievalOperationKey
    ) -> KnowledgeReadResult: ...


# Slice 3 immutable build, validation, and activation contracts.
class BuildStageState(str, Enum):
    STAGED = "STAGED"


class BuildValidationState(str, Enum):
    VALIDATED = "VALIDATED"
    FAILED = "FAILED"


class ArtifactTrust(str, Enum):
    APPROVED = "APPROVED"
    TEST_ONLY = "TEST_ONLY"


class RetrySafetyDisposition(str, Enum):
    SAME_OPERATION_ONLY = "SAME_OPERATION_ONLY"
    DO_NOT_RETRY = "DO_NOT_RETRY"
    EXTERNAL_AUTHORIZATION_REQUIRED = "EXTERNAL_AUTHORIZATION_REQUIRED"


class BuildFailureCode(str, Enum):
    ADMISSION_REJECTED = "ADMISSION_REJECTED"
    INVALID_CHUNK_PLAN = "INVALID_CHUNK_PLAN"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    PROVIDER_BOUNDS_EXCEEDED = "PROVIDER_BOUNDS_EXCEEDED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_EXHAUSTED = "PROVIDER_EXHAUSTED"
    PROVIDER_CONTRACT_INVALID = "PROVIDER_CONTRACT_INVALID"
    INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
    INDEX_CONFLICT = "INDEX_CONFLICT"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
    CARDINALITY_MISMATCH = "CARDINALITY_MISMATCH"
    DUPLICATE_CHUNK = "DUPLICATE_CHUNK"
    MISSING_CHUNK = "MISSING_CHUNK"
    ORPHAN_CHUNK = "ORPHAN_CHUNK"
    METADATA_INVALID = "METADATA_INVALID"
    EMBEDDING_MISMATCH = "EMBEDDING_MISMATCH"
    INDEX_INTEGRITY_FAILED = "INDEX_INTEGRITY_FAILED"
    PROBE_FAILED = "PROBE_FAILED"
    BUILD_NOT_STAGED = "BUILD_NOT_STAGED"
    BUILD_NOT_VALIDATED = "BUILD_NOT_VALIDATED"
    ACTIVATION_INELIGIBLE = "ACTIVATION_INELIGIBLE"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


@dataclass(frozen=True, slots=True)
class BuildOperationKey:
    value: str

    def __post_init__(self) -> None:
        _durable_key(self.value, "build operation key")


@dataclass(frozen=True, slots=True)
class BuildOperationClaim:
    operation_key: BuildOperationKey
    semantic_commitment: str
    build_identity: str
    profile_reference: OpaqueExternalReference
    capability_identity: str
    record_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.operation_key, BuildOperationKey):
            raise KnowledgeValidationError("operation_key must be BuildOperationKey")
        _hash(self.semantic_commitment, "semantic_commitment")
        _typed_identity(self.build_identity, "kbld", "build_identity")
        if (
            not isinstance(self.profile_reference, OpaqueExternalReference)
            or self.profile_reference.reference_type is not OpaqueReferenceType.CREDENTIAL_PROFILE
        ):
            raise KnowledgeValidationError("profile_reference must be an opaque Credential Profile")
        _durable_key(self.capability_identity, "capability_identity")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported build operation claim record version")


@dataclass(frozen=True, slots=True)
class ProviderInvocationLimits:
    timeout_seconds: float
    maximum_request_bytes: int
    maximum_batch_items: int
    maximum_invocations: int
    maximum_cost_units: int
    maximum_rate_units: int
    maximum_quota_units: int
    maximum_resource_units: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise KnowledgeValidationError("timeout_seconds must be finite and positive")
        for field in (
            "maximum_request_bytes", "maximum_batch_items", "maximum_invocations",
            "maximum_cost_units", "maximum_rate_units", "maximum_quota_units",
            "maximum_resource_units",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise KnowledgeValidationError(f"{field} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    profile_reference: OpaqueExternalReference
    capability_identity: str
    provider: str
    model: str
    embedding_profile_identity: str
    embedding_dimension: int
    hidden_retries_disabled: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_reference, OpaqueExternalReference)
            or self.profile_reference.reference_type is not OpaqueReferenceType.CREDENTIAL_PROFILE
        ):
            raise KnowledgeValidationError("provider capability requires an opaque Credential Profile")
        for field in (
            "capability_identity", "provider", "model", "embedding_profile_identity"
        ):
            _durable_key(getattr(self, field), field)
        if (
            isinstance(self.embedding_dimension, bool)
            or not isinstance(self.embedding_dimension, int)
            or self.embedding_dimension <= 0
        ):
            raise KnowledgeValidationError("embedding_dimension must be positive")
        if not isinstance(self.hidden_retries_disabled, bool):
            raise KnowledgeValidationError("hidden_retries_disabled must be boolean")


@dataclass(frozen=True, slots=True)
class BuildChunk:
    chunk_identity: str
    document_identity: str
    document_version_identity: str
    section_identity: str
    ordinal: int
    content: str
    content_hash: str
    metadata_commitment: str

    def __post_init__(self) -> None:
        _typed_identity(self.chunk_identity, "kchk", "chunk_identity")
        _typed_identity(self.document_identity, "kdoc", "document_identity")
        _typed_identity(self.document_version_identity, "kver", "document_version_identity")
        _durable_key(self.section_identity, "section_identity")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise KnowledgeValidationError("ordinal must be non-negative")
        if (
            not isinstance(self.content, str)
            or not self.content
            or len(self.content.encode("utf-8")) > 1_000_000
            or _contains_secret_shape(self.content)
        ):
            raise KnowledgeValidationError("chunk content must be non-empty and bounded")
        _hash(self.content_hash, "content_hash")
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_hash:
            raise KnowledgeValidationError("chunk content hash does not match content")
        _hash(self.metadata_commitment, "metadata_commitment")


@dataclass(frozen=True, slots=True)
class BuildDocumentProvenance:
    document_identity: str
    document_version_identity: str
    source_path: str
    content_hash: str
    approval_reference: str
    source_classification: SourceClassification
    outbound_eligible: bool
    content_type: ContentType
    metadata: tuple[MetadataItem, ...]
    knowledge_type: str = ""
    guidance_authority: str = ""
    document_status: DocumentStatus | None = None
    approval_state: str = ""
    production_eligible: bool | None = None

    def __post_init__(self) -> None:
        _typed_identity(self.document_identity, "kdoc", "document_identity")
        _typed_identity(self.document_version_identity, "kver", "document_version_identity")
        if not isinstance(self.source_path, str) or not self.source_path or len(self.source_path) > 512:
            raise KnowledgeValidationError("source_path must be non-empty and bounded")
        _hash(self.content_hash, "content_hash")
        _identifier(self.approval_reference, "approval_reference")
        if self.source_classification is not SourceClassification.APPROVED_OPERATIONAL_KNOWLEDGE:
            raise KnowledgeValidationError("build provenance requires approved source classification")
        if self.outbound_eligible is not True:
            raise KnowledgeValidationError("build provenance must be outbound eligible")
        if self.content_type is not ContentType.TEXT_UTF8:
            raise KnowledgeValidationError("build provenance requires supported content type")
        if (
            not isinstance(self.metadata, tuple)
            or any(not isinstance(item, MetadataItem) for item in self.metadata)
            or tuple(sorted(self.metadata)) != self.metadata
            or len({item.key for item in self.metadata}) != len(self.metadata)
            or any(
                _contains_secret_shape(item.key, metadata_key=True)
                or _contains_secret_shape(item.value)
                for item in self.metadata
            )
        ):
            raise KnowledgeValidationError(
                "build provenance metadata must be canonical, unique, and non-secret"
            )
        governance = (
            self.knowledge_type,
            self.guidance_authority,
            self.document_status,
            self.approval_state,
            self.production_eligible,
        )
        if any(value not in ("", None) for value in governance):
            if (
                self.knowledge_type not in {
                    "SOP", "RUNBOOK", "OTHER_APPROVED_OPERATIONAL_REFERENCE"
                }
                or self.guidance_authority not in {
                    "SOP_BACKED_ELIGIBLE", "CONTEXTUAL_ONLY"
                }
                or self.document_status is not DocumentStatus.ACTIVE
                or self.approval_state != "APPROVED"
                or self.production_eligible is not True
            ):
                raise KnowledgeValidationError(
                    "build provenance governance facts are incomplete or ineligible"
                )

    @property
    def has_governance_authority(self) -> bool:
        return bool(
            self.knowledge_type
            and self.guidance_authority
            and self.document_status is not None
            and self.approval_state
            and self.production_eligible is not None
        )


@dataclass(frozen=True, slots=True)
class BuildManifestProvenance:
    manifest_identity: str
    manifest_schema_identity: str
    manifest_schema_version: str
    canonicalization_version: str
    manifest_commitment: str
    corpus_identity: str
    corpus_version: str

    def __post_init__(self) -> None:
        for field in (
            "manifest_identity", "manifest_schema_identity", "manifest_schema_version",
            "canonicalization_version", "corpus_identity", "corpus_version",
        ):
            _durable_key(getattr(self, field), field)
        _typed_identity(self.manifest_commitment, "kmf", "manifest_commitment")


@dataclass(frozen=True, slots=True)
class BuildFailureFact:
    code: BuildFailureCode
    field: str
    detail: str
    retry_safety: RetrySafetyDisposition

    def __post_init__(self) -> None:
        if not isinstance(self.code, BuildFailureCode):
            raise KnowledgeValidationError("code must be a BuildFailureCode")
        _durable_key(self.field, "failure field")
        if (
            not isinstance(self.detail, str)
            or not self.detail
            or self.detail != self.detail.strip()
            or len(self.detail) > 256
            or _contains_secret_shape(self.detail)
        ):
            raise KnowledgeValidationError("failure detail must be bounded and non-secret")
        if not isinstance(self.retry_safety, RetrySafetyDisposition):
            raise KnowledgeValidationError("retry_safety must be a RetrySafetyDisposition")


@dataclass(frozen=True, slots=True)
class ProviderEmbeddingRequest:
    build_identity: str
    capability: ProviderCapability
    chunks: tuple[BuildChunk, ...]
    limits: ProviderInvocationLimits

    def __post_init__(self) -> None:
        _typed_identity(self.build_identity, "kbld", "build_identity")
        if not isinstance(self.capability, ProviderCapability):
            raise KnowledgeValidationError("capability must be a ProviderCapability")
        if not isinstance(self.chunks, tuple) or not self.chunks or any(
            not isinstance(chunk, BuildChunk) for chunk in self.chunks
        ):
            raise KnowledgeValidationError("chunks must be a non-empty tuple of BuildChunk")
        if not isinstance(self.limits, ProviderInvocationLimits):
            raise KnowledgeValidationError("limits must be ProviderInvocationLimits")


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    chunk_identity: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        _typed_identity(self.chunk_identity, "kchk", "chunk_identity")
        if not isinstance(self.values, tuple) or not self.values:
            raise KnowledgeValidationError("embedding values must be a non-empty tuple")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in self.values
        ):
            raise KnowledgeValidationError("embedding values must be finite numbers")


@dataclass(frozen=True, slots=True)
class ProviderEmbeddingResult:
    capability: ProviderCapability
    embeddings: tuple[EmbeddingVector, ...]
    invocation_count: int
    cost_units: int
    rate_units: int
    quota_units: int
    resource_units: int
    failure: BuildFailureFact | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, ProviderCapability):
            raise KnowledgeValidationError("capability must be a ProviderCapability")
        if any(not isinstance(item, EmbeddingVector) for item in self.embeddings):
            raise KnowledgeValidationError("embeddings contains an invalid value")
        for field in (
            "invocation_count", "cost_units", "rate_units", "quota_units", "resource_units"
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise KnowledgeValidationError(f"{field} must be non-negative")
        if self.failure is not None and not isinstance(self.failure, BuildFailureFact):
            raise KnowledgeValidationError("failure must be a BuildFailureFact")
        if self.failure is not None and self.embeddings:
            raise KnowledgeValidationError("failed provider result cannot contain embeddings")


@dataclass(frozen=True, slots=True)
class IndexEntryFact:
    chunk_identity: str
    metadata_commitment: str
    embedding_dimension: int

    def __post_init__(self) -> None:
        _typed_identity(self.chunk_identity, "kchk", "chunk_identity")
        _hash(self.metadata_commitment, "metadata_commitment")
        if isinstance(self.embedding_dimension, bool) or not isinstance(self.embedding_dimension, int) or self.embedding_dimension <= 0:
            raise KnowledgeValidationError("embedding_dimension must be positive")


@dataclass(frozen=True, slots=True)
class IndexArtifactFacts:
    build_identity: str
    artifact_commitment: str
    artifact_trust: ArtifactTrust
    provider: str
    model: str
    embedding_profile_identity: str
    embedding_dimension: int
    index_engine: str
    index_schema_identity: str
    entries: tuple[IndexEntryFact, ...]
    integrity_ok: bool

    def __post_init__(self) -> None:
        _typed_identity(self.build_identity, "kbld", "build_identity")
        _hash(self.artifact_commitment, "artifact_commitment")
        if not isinstance(self.artifact_trust, ArtifactTrust):
            raise KnowledgeValidationError("artifact_trust must be ArtifactTrust")
        for field in ("provider", "model", "embedding_profile_identity", "index_engine", "index_schema_identity"):
            _durable_key(getattr(self, field), field)
        if isinstance(self.embedding_dimension, bool) or not isinstance(self.embedding_dimension, int) or self.embedding_dimension <= 0:
            raise KnowledgeValidationError("embedding_dimension must be positive")
        if not isinstance(self.entries, tuple) or any(not isinstance(item, IndexEntryFact) for item in self.entries):
            raise KnowledgeValidationError("entries must be a tuple of IndexEntryFact")
        if not isinstance(self.integrity_ok, bool):
            raise KnowledgeValidationError("integrity_ok must be boolean")


@dataclass(frozen=True, slots=True)
class IndexProbeFacts:
    build_identity: str
    succeeded: bool
    returned_count: int
    inspected_limit: int

    def __post_init__(self) -> None:
        _typed_identity(self.build_identity, "kbld", "build_identity")
        if not isinstance(self.succeeded, bool):
            raise KnowledgeValidationError("succeeded must be boolean")
        for field in ("returned_count", "inspected_limit"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise KnowledgeValidationError(f"{field} must be non-negative")
        if self.returned_count > self.inspected_limit:
            raise KnowledgeValidationError("probe returned_count exceeds inspected_limit")


@dataclass(frozen=True, slots=True)
class StagedBuildRecord:
    build_identity: str
    operation_key: BuildOperationKey
    manifest_commitment: str
    lineage_commitment: str
    staged_commitment: str
    build_input: BuildIdentityInput
    profile_reference: OpaqueExternalReference
    capability_identity: str
    document_count: int
    document_provenance: tuple[BuildDocumentProvenance, ...]
    chunks: tuple[BuildChunk, ...]
    artifact: IndexArtifactFacts
    state: BuildStageState = BuildStageState.STAGED
    record_version: int = 1
    manifest_provenance: BuildManifestProvenance | None = None

    def __post_init__(self) -> None:
        _typed_identity(self.build_identity, "kbld", "build_identity")
        if not isinstance(self.operation_key, BuildOperationKey):
            raise KnowledgeValidationError("operation_key must be BuildOperationKey")
        _typed_identity(self.manifest_commitment, "kmf", "manifest_commitment")
        _hash(self.lineage_commitment, "lineage_commitment")
        _hash(self.staged_commitment, "staged_commitment")
        if not isinstance(self.build_input, BuildIdentityInput):
            raise KnowledgeValidationError("build_input must be BuildIdentityInput")
        if not isinstance(self.profile_reference, OpaqueExternalReference) or self.profile_reference.reference_type is not OpaqueReferenceType.CREDENTIAL_PROFILE:
            raise KnowledgeValidationError("profile_reference must be an opaque Credential Profile")
        _durable_key(self.capability_identity, "capability_identity")
        if isinstance(self.document_count, bool) or not isinstance(self.document_count, int) or self.document_count <= 0:
            raise KnowledgeValidationError("document_count must be positive")
        if (
            not isinstance(self.document_provenance, tuple)
            or not self.document_provenance
            or any(not isinstance(item, BuildDocumentProvenance) for item in self.document_provenance)
            or tuple(sorted(
                self.document_provenance,
                key=lambda item: (item.document_identity, item.document_version_identity),
            )) != self.document_provenance
            or len({item.document_version_identity for item in self.document_provenance})
            != len(self.document_provenance)
        ):
            raise KnowledgeValidationError("document provenance must be complete, unique, and canonical")
        if not isinstance(self.chunks, tuple) or not self.chunks or any(not isinstance(item, BuildChunk) for item in self.chunks):
            raise KnowledgeValidationError("chunks must be a non-empty tuple of BuildChunk")
        if not isinstance(self.artifact, IndexArtifactFacts):
            raise KnowledgeValidationError("artifact must be IndexArtifactFacts")
        if self.artifact.build_identity != self.build_identity:
            raise KnowledgeValidationError("artifact build identity mismatch")
        if self.state is not BuildStageState.STAGED or self.record_version != 1:
            raise KnowledgeValidationError("unsupported staged build state or record version")
        if self.manifest_provenance is not None:
            if (
                not isinstance(self.manifest_provenance, BuildManifestProvenance)
                or self.manifest_provenance.manifest_commitment != self.manifest_commitment
                or any(not item.has_governance_authority for item in self.document_provenance)
            ):
                raise KnowledgeValidationError("v2 staged build requires complete governance provenance")


@dataclass(frozen=True, slots=True, order=True)
class BuildValidationFinding:
    code: BuildFailureCode
    field: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, BuildFailureCode):
            raise KnowledgeValidationError("code must be BuildFailureCode")
        _durable_key(self.field, "validation field")
        if not isinstance(self.detail, str) or not self.detail or len(self.detail) > 256 or _contains_secret_shape(self.detail):
            raise KnowledgeValidationError("validation detail must be bounded and non-secret")


@dataclass(frozen=True, slots=True)
class BuildValidationRecord:
    build_identity: str
    operation_key: BuildOperationKey
    staged_commitment: str
    validation_commitment: str
    state: BuildValidationState
    findings: tuple[BuildValidationFinding, ...]
    record_version: int = 1

    def __post_init__(self) -> None:
        _typed_identity(self.build_identity, "kbld", "build_identity")
        if not isinstance(self.operation_key, BuildOperationKey):
            raise KnowledgeValidationError("operation_key must be BuildOperationKey")
        _hash(self.staged_commitment, "staged_commitment")
        _hash(self.validation_commitment, "validation_commitment")
        if not isinstance(self.state, BuildValidationState):
            raise KnowledgeValidationError("state must be BuildValidationState")
        if any(not isinstance(item, BuildValidationFinding) for item in self.findings):
            raise KnowledgeValidationError("findings must contain BuildValidationFinding")
        if tuple(sorted(set(self.findings))) != self.findings:
            raise KnowledgeValidationError("findings must be unique and canonically sorted")
        if (self.state is BuildValidationState.VALIDATED) != (not self.findings):
            raise KnowledgeValidationError("validation state and findings disagree")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported validation record version")


@dataclass(frozen=True, slots=True)
class BuildStageResult:
    record: StagedBuildRecord | None
    failures: tuple[BuildFailureFact, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(item, BuildFailureFact) for item in self.failures):
            raise KnowledgeValidationError("failures must contain BuildFailureFact")
        if (self.record is None) == (not self.failures):
            raise KnowledgeValidationError("stage result must contain exactly one success or failures")


@dataclass(frozen=True, slots=True)
class BuildActivationRequest:
    build_identity: str
    operation_key: ActivationOperationKey
    expected_generation: int

    def __post_init__(self) -> None:
        _typed_identity(self.build_identity, "kbld", "build_identity")
        if not isinstance(self.operation_key, ActivationOperationKey):
            raise KnowledgeValidationError("operation_key must be ActivationOperationKey")
        if isinstance(self.expected_generation, bool) or not isinstance(self.expected_generation, int) or self.expected_generation < 0:
            raise KnowledgeValidationError("expected_generation must be non-negative")


@dataclass(frozen=True, slots=True)
class BuildActivationResult:
    authority: ActivationAuthorityRecord
    staged_commitment: str
    validation_commitment: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, ActivationAuthorityRecord):
            raise KnowledgeValidationError("authority must be ActivationAuthorityRecord")
        _hash(self.staged_commitment, "staged_commitment")
        _hash(self.validation_commitment, "validation_commitment")


# Slice 4 retrieval contracts.  These values describe Candidate-C retrieval
# semantics only; they deliberately do not publish a Knowledge Snapshot.
class RetrievalScoreDirection(str, Enum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"


class RetrievalQueryDisposition(str, Enum):
    """Frozen v1 handling for non-retrievable query inputs."""

    INVALID = "INVALID"


class RetrievalApplicability(str, Enum):
    DIRECT = "DIRECT"
    PARTIAL = "PARTIAL"
    CONTEXTUAL = "CONTEXTUAL"
    NONE = "NONE"


class RetrievalResolution(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
    INVALID = "INVALID"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class SnapshotSourceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class RetrievalOperationState(str, Enum):
    FROZEN = "FROZEN"
    TRANSIENT_UNAVAILABLE = "TRANSIENT_UNAVAILABLE"
    RETRIEVAL_COMPLETED = "RETRIEVAL_COMPLETED"
    COMPLETED = "COMPLETED"


class RetrievalEvaluationDisposition(str, Enum):
    INCLUDED = "INCLUDED"
    REJECTED = "REJECTED"
    TOP_K_EXCLUDED = "TOP_K_EXCLUDED"
    PAYLOAD_EXCLUDED = "PAYLOAD"


class RetrievalRejectionReason(str, Enum):
    NONE = "NONE"
    REQUIRED_FILTER_MISMATCH = "REQUIRED_FILTER_MISMATCH"
    SCORE_OUTSIDE_POLICY = "SCORE_OUTSIDE_POLICY"
    TOP_K_LIMIT = "TOP_K_LIMIT"
    PAYLOAD_LIMIT = "LIMIT"


class ApplicabilityRuleIdentity(str, Enum):
    REQUIRED_FILTERS = "REQUIRED_FILTERS"
    DIRECT_THRESHOLD = "DIRECT_THRESHOLD"
    PARTIAL_THRESHOLD = "PARTIAL_THRESHOLD"
    CONTEXTUAL_THRESHOLD = "CONTEXTUAL_THRESHOLD"


class RetrievalFailureCode(str, Enum):
    QUERY_INVALID = "QUERY_INVALID"
    REPLAY_CONTRADICTION = "REPLAY_CONTRADICTION"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_INVALID = "PROVIDER_INVALID"
    INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
    INDEX_INVALID = "INDEX_INVALID"
    FROZEN_BUILD_INVALID = "FROZEN_BUILD_INVALID"
    CANDIDATE_INVALID = "CANDIDATE_INVALID"
    COMPATIBILITY_MISMATCH = "COMPATIBILITY_MISMATCH"


@dataclass(frozen=True, slots=True, order=True)
class QueryFilter:
    key: str
    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, str) or not _METADATA_KEY.fullmatch(self.key)
            or _contains_secret_shape(self.key, metadata_key=True)
        ):
            raise KnowledgeValidationError("query filter key is invalid")
        if (
            not isinstance(self.value, str) or not self.value or self.value != self.value.strip()
            or len(self.value.encode("utf-8")) > 512 or _contains_secret_shape(self.value)
        ):
            raise KnowledgeValidationError("query filter value must be bounded and non-secret")


@dataclass(frozen=True, slots=True)
class CanonicalKnowledgeQuery:
    schema_version: str
    canonicalization_version: str
    text: str
    filters: tuple[QueryFilter, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != "1.0" or self.canonicalization_version != "1.0":
            raise KnowledgeValidationError("unsupported query contract version")
        if (
            not isinstance(self.text, str) or self.text != self.text.strip()
            or len(self.text.encode("utf-8")) > 16384 or _contains_secret_shape(self.text)
        ):
            raise KnowledgeValidationError("query text must be canonical, bounded, and non-secret")
        if (
            not isinstance(self.filters, tuple)
            or any(not isinstance(item, QueryFilter) for item in self.filters)
            or len(self.filters) > 32
            or tuple(sorted(self.filters)) != self.filters
            or len({item.key for item in self.filters}) != len(self.filters)
        ):
            raise KnowledgeValidationError("query filters must be unique and canonically sorted")


@dataclass(frozen=True, slots=True)
class RetrievalProfile:
    profile_identity: str
    version: str
    canonicalization_version: str
    embedding_profile_identity: str
    provider: str
    model: str
    embedding_dimension: int
    index_engine: str
    index_schema_identity: str
    max_query_bytes: int
    allowed_filter_keys: tuple[str, ...]
    top_k: int
    candidate_limit: int
    score_direction: RetrievalScoreDirection
    score_precision: int
    max_content_bytes_per_result: int
    max_metadata_bytes_per_result: int
    max_total_payload_bytes: int
    empty_query_disposition: RetrievalQueryDisposition
    unsupported_query_disposition: RetrievalQueryDisposition

    def __post_init__(self) -> None:
        for field in (
            "profile_identity", "version", "canonicalization_version",
            "embedding_profile_identity", "provider", "model", "index_engine",
            "index_schema_identity",
        ):
            _durable_key(getattr(self, field), field)
        if not isinstance(self.allowed_filter_keys, tuple) or len(self.allowed_filter_keys) > 64 or any(
            not isinstance(item, str) or not _METADATA_KEY.fullmatch(item)
            or _contains_secret_shape(item, metadata_key=True)
            for item in self.allowed_filter_keys
        ) or tuple(sorted(set(self.allowed_filter_keys))) != self.allowed_filter_keys:
            raise KnowledgeValidationError("allowed filter keys must be canonical")
        for field in (
            "embedding_dimension", "max_query_bytes", "top_k", "candidate_limit",
            "max_content_bytes_per_result", "max_metadata_bytes_per_result",
            "max_total_payload_bytes",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise KnowledgeValidationError(f"{field} must be positive")
        if self.top_k > self.candidate_limit:
            raise KnowledgeValidationError("top_k cannot exceed candidate_limit")
        if not isinstance(self.score_direction, RetrievalScoreDirection):
            raise KnowledgeValidationError("score_direction is invalid")
        if isinstance(self.score_precision, bool) or not isinstance(self.score_precision, int) or not 0 <= self.score_precision <= 12:
            raise KnowledgeValidationError("score_precision must be between zero and twelve")
        if (
            not isinstance(self.empty_query_disposition, RetrievalQueryDisposition)
            or not isinstance(self.unsupported_query_disposition, RetrievalQueryDisposition)
        ):
            raise KnowledgeValidationError("query dispositions must use the closed retrieval vocabulary")


@dataclass(frozen=True, slots=True)
class ApplicabilityPolicy:
    policy_identity: str
    version: str
    required_filter_keys: tuple[str, ...]
    direct_threshold: float
    partial_threshold: float
    contextual_threshold: float

    def __post_init__(self) -> None:
        _durable_key(self.policy_identity, "policy_identity")
        _durable_key(self.version, "policy version")
        if (
            not isinstance(self.required_filter_keys, tuple) or not self.required_filter_keys
            or len(self.required_filter_keys) > 32
            or tuple(sorted(set(self.required_filter_keys))) != self.required_filter_keys
            or any(
                not _METADATA_KEY.fullmatch(item)
                or _contains_secret_shape(item, metadata_key=True)
                for item in self.required_filter_keys
            )
        ):
            raise KnowledgeValidationError("applicability requires canonical metadata predicates")
        for field in ("direct_threshold", "partial_threshold", "contextual_threshold"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise KnowledgeValidationError(f"{field} must be finite")


@dataclass(frozen=True, slots=True)
class RetrievalOperationRequest:
    operation_key: RetrievalOperationKey
    query: CanonicalKnowledgeQuery
    retrieval_profile: RetrievalProfile
    applicability_policy: ApplicabilityPolicy
    external_references: tuple[OpaqueExternalReference, ...]
    profile_reference: OpaqueExternalReference
    capability_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation_key, RetrievalOperationKey):
            raise KnowledgeValidationError("operation_key must be RetrievalOperationKey")
        if not isinstance(self.query, CanonicalKnowledgeQuery):
            raise KnowledgeValidationError("query is invalid")
        if not isinstance(self.retrieval_profile, RetrievalProfile):
            raise KnowledgeValidationError("retrieval_profile is invalid")
        if not isinstance(self.applicability_policy, ApplicabilityPolicy):
            raise KnowledgeValidationError("applicability_policy is invalid")
        if (
            not isinstance(self.external_references, tuple)
            or any(not isinstance(item, OpaqueExternalReference) for item in self.external_references)
            or len(self.external_references) > 16
            or tuple(sorted(self.external_references, key=lambda item: (item.reference_type.value, item.value))) != self.external_references
            or len(set(self.external_references)) != len(self.external_references)
        ):
            raise KnowledgeValidationError("external references must be bounded and canonical")
        if not isinstance(self.profile_reference, OpaqueExternalReference) or self.profile_reference.reference_type is not OpaqueReferenceType.CREDENTIAL_PROFILE:
            raise KnowledgeValidationError("profile_reference must be an opaque Credential Profile")
        _durable_key(self.capability_identity, "capability_identity")
        policy = self.applicability_policy
        if self.retrieval_profile.score_direction is RetrievalScoreDirection.HIGHER_IS_BETTER:
            ordered = policy.direct_threshold >= policy.partial_threshold >= policy.contextual_threshold
        else:
            ordered = policy.direct_threshold <= policy.partial_threshold <= policy.contextual_threshold
        if not ordered:
            raise KnowledgeValidationError("applicability score bands contradict score direction")


@dataclass(frozen=True, slots=True)
class FrozenRetrievalOperation:
    request: RetrievalOperationRequest
    semantic_commitment: str
    frozen_build_identity: str
    activation_generation: int
    activation_operation_key: ActivationOperationKey
    validation_commitment: str
    staged_commitment: str
    artifact_commitment: str
    lineage_commitment: str
    revision: int = 1
    record_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.request, RetrievalOperationRequest):
            raise KnowledgeValidationError("request is invalid")
        _hash(self.semantic_commitment, "semantic_commitment")
        _typed_identity(self.frozen_build_identity, "kbld", "frozen_build_identity")
        if isinstance(self.activation_generation, bool) or not isinstance(self.activation_generation, int) or self.activation_generation <= 0:
            raise KnowledgeValidationError("activation_generation must be positive")
        if not isinstance(self.activation_operation_key, ActivationOperationKey):
            raise KnowledgeValidationError("activation operation key is invalid")
        for field in ("validation_commitment", "staged_commitment", "artifact_commitment", "lineage_commitment"):
            _hash(getattr(self, field), field)
        if self.revision != 1 or self.record_version != 1:
            raise KnowledgeValidationError("unsupported frozen retrieval record version")


@dataclass(frozen=True, slots=True)
class QueryEmbeddingRequest:
    frozen_build_identity: str
    query: CanonicalKnowledgeQuery
    capability: ProviderCapability
    limits: ProviderInvocationLimits

    def __post_init__(self) -> None:
        _typed_identity(self.frozen_build_identity, "kbld", "frozen_build_identity")
        if not isinstance(self.query, CanonicalKnowledgeQuery) or not isinstance(self.capability, ProviderCapability) or not isinstance(self.limits, ProviderInvocationLimits):
            raise KnowledgeValidationError("query embedding request is invalid")


@dataclass(frozen=True, slots=True)
class QueryEmbeddingResult:
    capability: ProviderCapability
    vector: tuple[float, ...]
    invocation_count: int
    failure_detail: str | None = None
    cost_units: int = 0
    rate_units: int = 0
    quota_units: int = 0
    resource_units: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.capability, ProviderCapability):
            raise KnowledgeValidationError("capability is invalid")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in self.vector):
            raise KnowledgeValidationError("query vector must contain finite numbers")
        if isinstance(self.invocation_count, bool) or not isinstance(self.invocation_count, int) or self.invocation_count < 0:
            raise KnowledgeValidationError("invocation_count must be non-negative")
        for field in ("cost_units", "rate_units", "quota_units", "resource_units"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise KnowledgeValidationError(f"{field} must be non-negative")
        if self.invocation_count > 1:
            raise KnowledgeValidationError("hidden query embedding retries are forbidden")
        if self.failure_detail is None:
            if not self.vector or self.invocation_count != 1:
                raise KnowledgeValidationError("successful query embedding must contain one invocation")
        elif (
            self.vector or not self.failure_detail or self.failure_detail != self.failure_detail.strip()
            or len(self.failure_detail) > 256 or _contains_secret_shape(self.failure_detail)
        ):
            raise KnowledgeValidationError("provider failure must be bounded and non-secret")


@dataclass(frozen=True, slots=True)
class RawRetrievalCandidate:
    chunk_identity: str
    score: float
    metadata_commitment: str

    def __post_init__(self) -> None:
        _typed_identity(self.chunk_identity, "kchk", "chunk_identity")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not math.isfinite(float(self.score)):
            raise KnowledgeValidationError("candidate score must be finite")
        _hash(self.metadata_commitment, "metadata_commitment")


@dataclass(frozen=True, slots=True)
class RawRetrievalBatch:
    build_identity: str
    artifact_commitment: str
    candidates: tuple[RawRetrievalCandidate, ...]

    def __post_init__(self) -> None:
        _typed_identity(self.build_identity, "kbld", "build_identity")
        _hash(self.artifact_commitment, "artifact_commitment")
        if not isinstance(self.candidates, tuple) or any(not isinstance(item, RawRetrievalCandidate) for item in self.candidates):
            raise KnowledgeValidationError("candidates must be RawRetrievalCandidate values")


@dataclass(frozen=True, slots=True)
class OrderedRetrievalCandidate:
    chunk_identity: str
    document_identity: str
    document_version_identity: str
    score: float
    applicability: RetrievalApplicability
    content: str
    metadata: tuple[MetadataItem, ...]
    content_truncated: bool = False

    def __post_init__(self) -> None:
        _typed_identity(self.chunk_identity, "kchk", "chunk_identity")
        _typed_identity(self.document_identity, "kdoc", "document_identity")
        _typed_identity(self.document_version_identity, "kver", "document_version_identity")
        if (
            isinstance(self.score, bool) or not isinstance(self.score, (int, float))
            or not math.isfinite(float(self.score))
            or not isinstance(self.applicability, RetrievalApplicability)
        ):
            raise KnowledgeValidationError("ordered candidate score/applicability is invalid")
        if not isinstance(self.content, str) or not isinstance(self.metadata, tuple) or any(not isinstance(item, MetadataItem) for item in self.metadata):
            raise KnowledgeValidationError("ordered candidate content/metadata is invalid")


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationFact:
    chunk_identity: str
    score: float
    applicability: RetrievalApplicability
    included: bool
    content_truncated: bool
    canonical_rank: int
    policy_identity: str
    policy_version: str
    query_predicates: tuple[QueryFilter, ...]
    metadata_predicates: tuple[MetadataItem, ...]
    rule_facts: tuple[ApplicabilityRuleFact, ...]
    disposition: RetrievalEvaluationDisposition
    rejection_reason: RetrievalRejectionReason

    def __post_init__(self) -> None:
        _typed_identity(self.chunk_identity, "kchk", "chunk_identity")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not math.isfinite(float(self.score)):
            raise KnowledgeValidationError("evaluation score must be finite")
        if not isinstance(self.applicability, RetrievalApplicability):
            raise KnowledgeValidationError("evaluation applicability is invalid")
        if not isinstance(self.included, bool) or not isinstance(self.content_truncated, bool):
            raise KnowledgeValidationError("evaluation flags must be boolean")
        if isinstance(self.canonical_rank, bool) or not isinstance(self.canonical_rank, int) or self.canonical_rank < 1:
            raise KnowledgeValidationError("evaluation canonical rank must be positive")
        _durable_key(self.policy_identity, "evaluation policy identity")
        _durable_key(self.policy_version, "evaluation policy version")
        if (
            not isinstance(self.query_predicates, tuple)
            or any(not isinstance(item, QueryFilter) for item in self.query_predicates)
            or tuple(sorted(self.query_predicates)) != self.query_predicates
            or len({item.key for item in self.query_predicates}) != len(self.query_predicates)
        ):
            raise KnowledgeValidationError("evaluation query predicates must be canonical")
        if (
            not isinstance(self.metadata_predicates, tuple)
            or any(not isinstance(item, MetadataItem) for item in self.metadata_predicates)
            or tuple(sorted(self.metadata_predicates)) != self.metadata_predicates
            or len({item.key for item in self.metadata_predicates}) != len(self.metadata_predicates)
        ):
            raise KnowledgeValidationError("evaluation metadata predicates must be canonical")
        expected_rules = tuple(ApplicabilityRuleIdentity)
        if (
            not isinstance(self.rule_facts, tuple)
            or tuple(item.rule_identity for item in self.rule_facts) != expected_rules
        ):
            raise KnowledgeValidationError("evaluation rule provenance must be complete and canonical")
        if not isinstance(self.disposition, RetrievalEvaluationDisposition) or not isinstance(
            self.rejection_reason, RetrievalRejectionReason
        ):
            raise KnowledgeValidationError("evaluation disposition is invalid")
        if self.included != (self.disposition is RetrievalEvaluationDisposition.INCLUDED):
            raise KnowledgeValidationError("evaluation inclusion and disposition disagree")
        if self.included:
            if self.applicability is RetrievalApplicability.NONE or self.rejection_reason is not RetrievalRejectionReason.NONE:
                raise KnowledgeValidationError("included evaluation must be applicable and unrejected")
        elif self.rejection_reason is RetrievalRejectionReason.NONE:
            raise KnowledgeValidationError("excluded evaluation requires an explicit rejection reason")


@dataclass(frozen=True, slots=True)
class ApplicabilityRuleFact:
    rule_identity: ApplicabilityRuleIdentity
    matched: bool

    def __post_init__(self) -> None:
        if not isinstance(self.rule_identity, ApplicabilityRuleIdentity) or not isinstance(self.matched, bool):
            raise KnowledgeValidationError("applicability rule fact is invalid")


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshotChunk:
    chunk_identity: str
    document_identity: str
    document_version_identity: str
    section_identity: str
    content_commitment: str
    metadata_commitment: str
    score: float
    applicability: RetrievalApplicability
    content: str
    metadata: tuple[MetadataItem, ...]
    content_truncated: bool = False
    knowledge_type: str = ""
    guidance_authority: str = ""
    sop_backed_eligible: bool | None = None

    def __post_init__(self) -> None:
        _typed_identity(self.chunk_identity, "kchk", "chunk_identity")
        _typed_identity(self.document_identity, "kdoc", "document_identity")
        _typed_identity(self.document_version_identity, "kver", "document_version_identity")
        _durable_key(self.section_identity, "section_identity")
        _hash(self.content_commitment, "content_commitment")
        _hash(self.metadata_commitment, "metadata_commitment")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not math.isfinite(float(self.score)):
            raise KnowledgeValidationError("Snapshot chunk score must be finite")
        if self.applicability is RetrievalApplicability.NONE:
            raise KnowledgeValidationError("Snapshot chunk must be applicable")
        if not isinstance(self.content, str) or _contains_secret_shape(self.content):
            raise KnowledgeValidationError("Snapshot content must be non-secret")
        if any(not isinstance(item, MetadataItem) for item in self.metadata):
            raise KnowledgeValidationError("Snapshot metadata is invalid")
        if not isinstance(self.content_truncated, bool):
            raise KnowledgeValidationError("content_truncated must be boolean")
        governance = (
            self.knowledge_type, self.guidance_authority, self.sop_backed_eligible
        )
        if any(value not in ("", None) for value in governance):
            if (
                self.knowledge_type not in {
                    "SOP", "RUNBOOK", "OTHER_APPROVED_OPERATIONAL_REFERENCE"
                }
                or self.guidance_authority not in {
                    "SOP_BACKED_ELIGIBLE", "CONTEXTUAL_ONLY"
                }
                or not isinstance(self.sop_backed_eligible, bool)
            ):
                raise KnowledgeValidationError("Snapshot governance facts are incomplete")


@dataclass(frozen=True, slots=True)
class KnowledgeProvenanceChunk:
    chunk_identity: str
    document_identity: str
    document_version_identity: str
    section_identity: str
    content_commitment: str
    metadata_commitment: str
    knowledge_type: str
    guidance_authority: str
    sop_backed_eligible: bool
    canonical_rank: int
    score: float
    applicability: RetrievalApplicability
    included: bool
    disposition: RetrievalEvaluationDisposition
    rejection_reason: RetrievalRejectionReason
    query_predicates: tuple[QueryFilter, ...]
    metadata_predicates: tuple[MetadataItem, ...]
    rule_facts: tuple[ApplicabilityRuleFact, ...]

    def __post_init__(self) -> None:
        _typed_identity(self.chunk_identity, "kchk", "chunk_identity")
        _typed_identity(self.document_identity, "kdoc", "document_identity")
        _typed_identity(self.document_version_identity, "kver", "document_version_identity")
        _durable_key(self.section_identity, "section_identity")
        _hash(self.content_commitment, "content_commitment")
        _hash(self.metadata_commitment, "metadata_commitment")
        if self.knowledge_type not in {
            "SOP", "RUNBOOK", "OTHER_APPROVED_OPERATIONAL_REFERENCE"
        } or self.guidance_authority not in {
            "SOP_BACKED_ELIGIBLE", "CONTEXTUAL_ONLY"
        }:
            raise KnowledgeValidationError("public provenance governance is invalid")
        if not isinstance(self.sop_backed_eligible, bool):
            raise KnowledgeValidationError("sop_backed_eligible must be boolean")
        if isinstance(self.canonical_rank, bool) or self.canonical_rank < 1:
            raise KnowledgeValidationError("canonical_rank must be positive")


@dataclass(frozen=True, slots=True)
class KnowledgeProvenanceProjection:
    snapshot_identity: KnowledgeSnapshotKey
    snapshot_commitment: str
    snapshot_schema_version: str
    resolution: RetrievalResolution
    source_status: SnapshotSourceStatus
    retrieval_operation_identity: RetrievalOperationKey
    activation_generation: int
    activation_operation_identity: ActivationOperationKey
    frozen_build_identity: str
    lineage_commitment: str
    staged_commitment: str
    validation_commitment: str
    artifact_commitment: str
    manifest_identity: str
    manifest_schema_identity: str
    manifest_schema_version: str
    canonicalization_version: str
    manifest_commitment: str
    corpus_identity: str
    corpus_version: str
    retrieval_profile_identity: str
    retrieval_profile_version: str
    applicability_policy_identity: str
    applicability_policy_version: str
    embedding_provider: str
    embedding_model: str
    embedding_profile_identity: str
    embedding_dimension: int
    index_engine: str
    index_schema_identity: str
    query_commitment: str
    query_filters: tuple[QueryFilter, ...]
    chunks: tuple[KnowledgeProvenanceChunk, ...]
    failures: tuple[RetrievalFailureFact, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_identity, KnowledgeSnapshotKey):
            raise KnowledgeValidationError("snapshot_identity is invalid")
        for field in (
            "snapshot_commitment", "lineage_commitment", "staged_commitment",
            "validation_commitment", "artifact_commitment", "query_commitment",
        ):
            _hash(getattr(self, field), field)
        _typed_identity(self.frozen_build_identity, "kbld", "frozen_build_identity")
        _typed_identity(self.manifest_commitment, "kmf", "manifest_commitment")
        if tuple(item.canonical_rank for item in self.chunks) != tuple(
            sorted(item.canonical_rank for item in self.chunks)
        ):
            raise KnowledgeValidationError("public provenance chunks must be canonically ordered")


@dataclass(frozen=True, slots=True, order=True)
class RetrievalFailureFact:
    code: RetrievalFailureCode
    field: str
    detail: str
    retry_safety: RetrySafetyDisposition

    def __post_init__(self) -> None:
        if not isinstance(self.code, RetrievalFailureCode) or not isinstance(self.retry_safety, RetrySafetyDisposition):
            raise KnowledgeValidationError("retrieval failure vocabulary is invalid")
        _durable_key(self.field, "failure field")
        if not isinstance(self.detail, str) or not self.detail or len(self.detail) > 256 or _contains_secret_shape(self.detail):
            raise KnowledgeValidationError("failure detail must be bounded and non-secret")


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    operation_key: RetrievalOperationKey
    frozen_build_identity: str | None
    resolution: RetrievalResolution
    candidates: tuple[OrderedRetrievalCandidate, ...] = ()
    knowledge_gap: bool = False
    failures: tuple[RetrievalFailureFact, ...] = ()
    payload_truncated: bool = False
    evaluations: tuple[RetrievalEvaluationFact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.operation_key, RetrievalOperationKey) or not isinstance(self.resolution, RetrievalResolution):
            raise KnowledgeValidationError("retrieval result identity/resolution is invalid")
        if self.frozen_build_identity is not None:
            _typed_identity(self.frozen_build_identity, "kbld", "frozen_build_identity")
        if any(not isinstance(item, OrderedRetrievalCandidate) for item in self.candidates) or any(not isinstance(item, RetrievalFailureFact) for item in self.failures):
            raise KnowledgeValidationError("retrieval result facts are invalid")
        if any(not isinstance(item, RetrievalEvaluationFact) for item in self.evaluations):
            raise KnowledgeValidationError("retrieval evaluations are invalid")
        if self.resolution is RetrievalResolution.MATCH:
            if not self.candidates or self.knowledge_gap or self.failures:
                raise KnowledgeValidationError("MATCH requires candidates only")
        elif self.resolution is RetrievalResolution.NO_MATCH:
            if self.candidates or not self.knowledge_gap or self.failures:
                raise KnowledgeValidationError("NO_MATCH requires a knowledge gap only")
        elif self.candidates or self.knowledge_gap or not self.failures:
            raise KnowledgeValidationError("failure resolution requires failures only")
        if self.resolution in (RetrievalResolution.INVALID, RetrievalResolution.REPAIR_REQUIRED, RetrievalResolution.RETRIEVAL_UNAVAILABLE) and self.evaluations:
            raise KnowledgeValidationError("failure result cannot publish evaluation facts")


@dataclass(frozen=True, slots=True)
class DurableRetrievalCompletion:
    """Immutable Candidate-C authority for one bounded completed retrieval."""

    operation_key: RetrievalOperationKey
    frozen_build_identity: str
    frozen_operation_commitment: str
    validation_commitment: str
    staged_commitment: str
    artifact_commitment: str
    lineage_commitment: str
    query_commitment: str
    retrieval_profile_identity: str
    retrieval_profile_version: str
    applicability_policy_identity: str
    applicability_policy_version: str
    raw_batch: RawRetrievalBatch
    resolution: RetrievalResolution
    knowledge_gap: bool
    payload_truncated: bool
    evaluations: tuple[RetrievalEvaluationFact, ...]
    semantic_commitment: str
    record_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.operation_key, RetrievalOperationKey):
            raise KnowledgeValidationError("completion operation key is invalid")
        _typed_identity(self.frozen_build_identity, "kbld", "frozen_build_identity")
        for field in (
            "frozen_operation_commitment", "validation_commitment", "staged_commitment",
            "artifact_commitment", "lineage_commitment", "query_commitment",
            "semantic_commitment",
        ):
            _hash(getattr(self, field), field)
        for field in (
            "retrieval_profile_identity", "retrieval_profile_version",
            "applicability_policy_identity", "applicability_policy_version",
        ):
            _durable_key(getattr(self, field), field)
        if not isinstance(self.raw_batch, RawRetrievalBatch):
            raise KnowledgeValidationError("completion raw batch is invalid")
        if (
            self.raw_batch.build_identity != self.frozen_build_identity
            or self.raw_batch.artifact_commitment != self.artifact_commitment
        ):
            raise KnowledgeValidationError("completion raw batch contradicts frozen authority")
        if self.resolution not in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH):
            raise KnowledgeValidationError("completion must have an available terminal resolution")
        if not isinstance(self.knowledge_gap, bool) or not isinstance(self.payload_truncated, bool):
            raise KnowledgeValidationError("completion flags must be boolean")
        if any(not isinstance(item, RetrievalEvaluationFact) for item in self.evaluations):
            raise KnowledgeValidationError("completion evaluations are invalid")
        raw_ids = tuple(item.chunk_identity for item in self.raw_batch.candidates)
        evaluation_ids = tuple(item.chunk_identity for item in self.evaluations)
        if (
            len(set(raw_ids)) != len(raw_ids)
            or len(set(evaluation_ids)) != len(evaluation_ids)
            or set(raw_ids) != set(evaluation_ids)
            or tuple(item.canonical_rank for item in self.evaluations)
            != tuple(range(1, len(self.evaluations) + 1))
        ):
            raise KnowledgeValidationError(
                "completion candidate completeness and ordering do not re-derive"
            )
        raw_by_id = {item.chunk_identity: item for item in self.raw_batch.candidates}
        if any(raw_by_id[item.chunk_identity].score != item.score for item in self.evaluations):
            raise KnowledgeValidationError("completion evaluation scores contradict raw authority")
        if self.resolution is RetrievalResolution.MATCH:
            if self.knowledge_gap or not any(item.included for item in self.evaluations):
                raise KnowledgeValidationError("MATCH completion requires included knowledge")
        elif (
            not self.knowledge_gap
            or self.payload_truncated
            or any(item.included for item in self.evaluations)
        ):
            raise KnowledgeValidationError("NO_MATCH completion requires complete rejection")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported retrieval completion record version")


@dataclass(frozen=True, slots=True)
class RetrievalRecoveryFact:
    operation_key: RetrievalOperationKey
    semantic_commitment: str
    failures: tuple[RetrievalFailureFact, ...]
    revision: int = 1
    record_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.operation_key, RetrievalOperationKey):
            raise KnowledgeValidationError("recovery operation key is invalid")
        _hash(self.semantic_commitment, "semantic_commitment")
        if not self.failures or any(not isinstance(item, RetrievalFailureFact) for item in self.failures):
            raise KnowledgeValidationError("recovery fact requires bounded failures")
        if any(item.retry_safety is not RetrySafetyDisposition.SAME_OPERATION_ONLY for item in self.failures):
            raise KnowledgeValidationError("recovery facts are only for transient same-operation retry")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise KnowledgeValidationError("recovery revision must be positive")
        if self.record_version != 1:
            raise KnowledgeValidationError("unsupported recovery record version")


@dataclass(frozen=True, slots=True)
class RetrievalOperationRead:
    frozen: FrozenRetrievalOperation
    state: RetrievalOperationState
    recovery: RetrievalRecoveryFact | None = None
    completion: DurableRetrievalCompletion | None = None
    snapshot_key: KnowledgeSnapshotKey | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.frozen, FrozenRetrievalOperation) or not isinstance(self.state, RetrievalOperationState):
            raise KnowledgeValidationError("operation read is invalid")
        if self.recovery is not None and self.recovery.operation_key != self.frozen.request.operation_key:
            raise KnowledgeValidationError("operation recovery key mismatch")
        if self.completion is not None and self.completion.operation_key != self.frozen.request.operation_key:
            raise KnowledgeValidationError("operation completion key mismatch")
        if self.state is RetrievalOperationState.COMPLETED:
            if self.snapshot_key is None:
                raise KnowledgeValidationError("completed operation requires Snapshot")
        elif self.snapshot_key is not None:
            raise KnowledgeValidationError("incomplete operation cannot reference Snapshot")
        if self.state is RetrievalOperationState.RETRIEVAL_COMPLETED and self.completion is None:
            raise KnowledgeValidationError("retrieval-completed operation requires durable authority")
        if self.completion is not None and self.state not in (
            RetrievalOperationState.RETRIEVAL_COMPLETED, RetrievalOperationState.COMPLETED
        ):
            raise KnowledgeValidationError("completion authority contradicts operation state")


@dataclass(frozen=True, slots=True)
class TerminalUnavailableRequest:
    finalization_key: SnapshotFinalizationKey
    operation_key: RetrievalOperationKey
    authority_reference: OpaqueExternalReference
    failures: tuple[RetrievalFailureFact, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.finalization_key, SnapshotFinalizationKey) or not isinstance(self.operation_key, RetrievalOperationKey):
            raise KnowledgeValidationError("terminal finalization identity is invalid")
        if not isinstance(self.authority_reference, OpaqueExternalReference):
            raise KnowledgeValidationError("terminal finalization requires opaque authority reference")
        if not self.failures or any(not isinstance(item, RetrievalFailureFact) for item in self.failures):
            raise KnowledgeValidationError("terminal finalization requires bounded failure facts")
        if any(
            item.code not in (
                RetrievalFailureCode.PROVIDER_UNAVAILABLE,
                RetrievalFailureCode.INDEX_UNAVAILABLE,
            )
            for item in self.failures
        ):
            raise KnowledgeValidationError(
                "invalid or repair-required facts cannot be finalized as unavailable"
            )


@dataclass(frozen=True, slots=True)
class KnowledgeResolutionOutcome:
    resolution: RetrievalResolution
    snapshot: KnowledgeSnapshotEnvelope | None = None
    failures: tuple[RetrievalFailureFact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.resolution, RetrievalResolution):
            raise KnowledgeValidationError("outcome resolution is invalid")
        if self.snapshot is not None and self.snapshot.resolution is not self.resolution:
            raise KnowledgeValidationError("outcome Snapshot resolution mismatch")
        if self.resolution in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH) and self.snapshot is None:
            raise KnowledgeValidationError("completed outcome requires Snapshot")
        if self.failures and any(not isinstance(item, RetrievalFailureFact) for item in self.failures):
            raise KnowledgeValidationError("outcome failures are invalid")
