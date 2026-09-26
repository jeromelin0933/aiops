"""Pure deterministic validation for immutable staged Knowledge builds."""

from __future__ import annotations

import hashlib

from .contracts import (
    ArtifactTrust,
    BuildDocumentProvenance,
    BuildManifestProvenance,
    BuildFailureCode,
    BuildIdentityInput,
    BuildOperationKey,
    BuildValidationFinding,
    BuildValidationState,
    IndexArtifactFacts,
    IndexProbeFacts,
    OpaqueExternalReference,
    StagedBuildRecord,
)
from .identity import canonical_serialize


def _commitment(namespace: str, value: object) -> str:
    envelope = {"namespace": namespace, "version": "1.0", "value": value}
    return hashlib.sha256(canonical_serialize(envelope).encode("utf-8")).hexdigest()


def derive_build_operation_commitment(
    operation_key: BuildOperationKey,
    build_identity: str,
    profile_reference: OpaqueExternalReference,
    capability_identity: str,
) -> str:
    return _commitment("KNOWLEDGE_BUILD_OPERATION", {
        "operation_key": operation_key,
        "build_identity": build_identity,
        "profile_reference": profile_reference,
        "capability_identity": capability_identity,
    })


def derive_chunk_metadata_commitment(
    manifest_commitment: str,
    provenance: BuildDocumentProvenance,
    *,
    chunk_identity: str,
    section_identity: str,
    ordinal: int,
    content_hash: str,
) -> str:
    document: object = provenance
    if not provenance.has_governance_authority:
        # Preserve the exact pre-patch commitment input for legacy builds.
        document = {
            "document_identity": provenance.document_identity,
            "document_version_identity": provenance.document_version_identity,
            "source_path": provenance.source_path,
            "content_hash": provenance.content_hash,
            "approval_reference": provenance.approval_reference,
            "source_classification": provenance.source_classification,
            "outbound_eligible": provenance.outbound_eligible,
            "content_type": provenance.content_type,
            "metadata": provenance.metadata,
        }
    return _commitment("KNOWLEDGE_CHUNK_PROVENANCE", {
        "manifest_commitment": manifest_commitment,
        "document": document,
        "chunk_identity": chunk_identity,
        "section_identity": section_identity,
        "ordinal": ordinal,
        "content_hash": content_hash,
    })


def derive_build_lineage_commitment(
    build_identity: str,
    manifest_commitment: str,
    chunks: tuple[object, ...],
    profile_reference: OpaqueExternalReference,
    capability_identity: str,
    manifest_provenance: BuildManifestProvenance | None = None,
) -> str:
    value = {
        "build_identity": build_identity,
        "manifest_commitment": manifest_commitment,
        "chunks": chunks,
        "profile_reference": profile_reference,
        "capability_identity": capability_identity,
    }
    if manifest_provenance is not None:
        value["manifest_provenance"] = manifest_provenance
    return _commitment("KNOWLEDGE_BUILD_LINEAGE", value)


def derive_staged_build_commitment(
    lineage_commitment: str,
    artifact: IndexArtifactFacts,
    identity_input: BuildIdentityInput,
) -> str:
    return _commitment("KNOWLEDGE_STAGED_BUILD", {
        "lineage_commitment": lineage_commitment,
        "artifact": artifact,
        "identity_input": identity_input,
    })


def derive_build_validation_commitment(
    build_identity: str,
    operation_key: BuildOperationKey,
    staged_commitment: str,
    state: BuildValidationState,
    findings: tuple[BuildValidationFinding, ...],
) -> str:
    return _commitment("KNOWLEDGE_BUILD_VALIDATION", {
        "build_identity": build_identity,
        "operation_key": operation_key,
        "staged_commitment": staged_commitment,
        "state": state,
        "findings": findings,
    })


def _finding(code: BuildFailureCode, field: str, detail: str) -> BuildValidationFinding:
    return BuildValidationFinding(code, field, detail)


def validate_staged_build(
    staged: StagedBuildRecord,
    artifact: IndexArtifactFacts | None,
    probe: IndexProbeFacts | None,
) -> tuple[BuildValidationFinding, ...]:
    """Return canonical findings without mutating store, artifact, or activation."""
    if not isinstance(staged, StagedBuildRecord):
        raise TypeError("staged must be a StagedBuildRecord")
    findings: list[BuildValidationFinding] = []
    chunks = staged.chunks
    chunk_ids = tuple(chunk.chunk_identity for chunk in chunks)
    provenance_by_version = {
        item.document_version_identity: item for item in staged.document_provenance
    }

    if staged.document_count != len({chunk.document_identity for chunk in chunks}):
        findings.append(_finding(
            BuildFailureCode.CARDINALITY_MISMATCH,
            "document_count",
            "declared document count does not match staged chunk provenance",
        ))
    if staged.document_count != len(staged.document_provenance):
        findings.append(_finding(
            BuildFailureCode.CARDINALITY_MISMATCH,
            "document_provenance",
            "admitted document provenance cardinality does not match the staged build",
        ))
    if len(set(chunk_ids)) != len(chunk_ids):
        findings.append(_finding(
            BuildFailureCode.DUPLICATE_CHUNK,
            "chunks",
            "staged build contains duplicate Chunk Identity values",
        ))
    if chunk_ids != staged.build_input.ordered_chunk_identities:
        findings.append(_finding(
            BuildFailureCode.CARDINALITY_MISMATCH,
            "ordered_chunk_identities",
            "ordered staged chunks do not match Build Identity inputs",
        ))
    if any(not chunk.metadata_commitment for chunk in chunks):
        findings.append(_finding(
            BuildFailureCode.METADATA_INVALID,
            "metadata",
            "required chunk metadata provenance is missing",
        ))
    for chunk in chunks:
        provenance = provenance_by_version.get(chunk.document_version_identity)
        if (
            provenance is None
            or provenance.document_identity != chunk.document_identity
            or chunk.metadata_commitment
            != derive_chunk_metadata_commitment(
                staged.manifest_commitment,
                provenance,
                chunk_identity=chunk.chunk_identity,
                section_identity=chunk.section_identity,
                ordinal=chunk.ordinal,
                content_hash=chunk.content_hash,
            )
        ):
            findings.append(_finding(
                BuildFailureCode.METADATA_INVALID,
                "chunk_provenance",
                "chunk metadata does not resolve to admitted manifest and source provenance",
            ))

    if artifact is None:
        findings.append(_finding(
            BuildFailureCode.ARTIFACT_MISSING,
            "artifact",
            "exact staged index artifact is missing",
        ))
    else:
        if artifact != staged.artifact:
            findings.append(_finding(
                BuildFailureCode.ARTIFACT_MISMATCH,
                "artifact",
                "inspected artifact does not match immutable staged provenance",
            ))
        build_input = staged.build_input
        compatibility = (
            artifact.build_identity == staged.build_identity
            and artifact.provider == build_input.embedding_provider
            and artifact.model == build_input.embedding_model
            and artifact.embedding_profile_identity == build_input.embedding_profile_identity
            and artifact.embedding_dimension == build_input.embedding_dimension
            and artifact.index_engine == build_input.index_engine
            and artifact.index_schema_identity == build_input.index_schema_identity
        )
        if not compatibility:
            findings.append(_finding(
                BuildFailureCode.EMBEDDING_MISMATCH,
                "compatibility",
                "artifact compatibility does not match immutable Build Identity inputs",
            ))
        if artifact.artifact_trust is not ArtifactTrust.APPROVED:
            findings.append(_finding(
                BuildFailureCode.ACTIVATION_INELIGIBLE,
                "artifact_trust",
                "artifact is not approved for production activation",
            ))
        if not artifact.integrity_ok:
            findings.append(_finding(
                BuildFailureCode.INDEX_INTEGRITY_FAILED,
                "artifact_integrity",
                "staged index artifact failed integrity inspection",
            ))

        entry_ids = tuple(entry.chunk_identity for entry in artifact.entries)
        if len(set(entry_ids)) != len(entry_ids):
            findings.append(_finding(
                BuildFailureCode.DUPLICATE_CHUNK,
                "index_entries",
                "index contains duplicate Chunk Identity entries",
            ))
        missing = set(chunk_ids) - set(entry_ids)
        orphan = set(entry_ids) - set(chunk_ids)
        if missing:
            findings.append(_finding(
                BuildFailureCode.MISSING_CHUNK,
                "index_entries",
                "index is missing required staged chunks",
            ))
        if orphan:
            findings.append(_finding(
                BuildFailureCode.ORPHAN_CHUNK,
                "index_entries",
                "index contains chunks outside the staged build",
            ))
        chunk_metadata = {chunk.chunk_identity: chunk.metadata_commitment for chunk in chunks}
        if any(
            entry.chunk_identity in chunk_metadata
            and entry.metadata_commitment != chunk_metadata[entry.chunk_identity]
            for entry in artifact.entries
        ):
            findings.append(_finding(
                BuildFailureCode.METADATA_INVALID,
                "index_metadata",
                "index metadata cannot resolve to staged chunk provenance",
            ))
        if any(
            entry.embedding_dimension != build_input.embedding_dimension
            for entry in artifact.entries
        ):
            findings.append(_finding(
                BuildFailureCode.EMBEDDING_MISMATCH,
                "entry_dimension",
                "index entry embedding dimension is incompatible",
            ))

    if probe is None or not probe.succeeded or probe.build_identity != staged.build_identity:
        findings.append(_finding(
            BuildFailureCode.PROBE_FAILED,
            "probe",
            "bounded staged-index probe failed",
        ))

    if (
        derive_build_lineage_commitment(
            staged.build_identity,
            staged.manifest_commitment,
            staged.chunks,
            staged.profile_reference,
            staged.capability_identity,
            staged.manifest_provenance,
        ) != staged.lineage_commitment
        or derive_staged_build_commitment(
            staged.lineage_commitment, staged.artifact, staged.build_input
        ) != staged.staged_commitment
    ):
        findings.append(_finding(
            BuildFailureCode.INDEX_INTEGRITY_FAILED,
            "build_commitment",
            "staged build semantic commitments do not match immutable provenance",
        ))

    return tuple(sorted(set(findings)))
