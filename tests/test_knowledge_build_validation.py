from dataclasses import replace

import pytest

from knowledge_index import (
    ArtifactTrust,
    BuildFailureCode,
    BuildOperationKey,
    BuildValidationState,
    DocumentStatus,
    IndexEntryFact,
    IndexProbeFacts,
    KnowledgeBuildService,
    KnowledgeReadStatus,
    SqliteKnowledgeStore,
    validate_staged_build,
    derive_chunk_metadata_commitment,
)
from _knowledge_build_testkit import DeterministicIndex, DeterministicProvider, digest, limits, manifest_and_plan


def _stage(tmp_path, *, index=None):
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    provider = DeterministicProvider()
    index = index or DeterministicIndex()
    service = KnowledgeBuildService(store, provider, index)
    staged = service.stage(
        operation_key=BuildOperationKey("stage-1"), raw_manifest=raw,
        source_root=tmp_path, chunks=chunks, identity_input=identity_input,
        limits=limits(), required_capability_identity="embedding-capability-v1",
    ).record
    assert staged is not None
    return store, index, service, staged


def test_valid_staged_build_has_no_deterministic_findings(tmp_path) -> None:
    store, index, _service, staged = _stage(tmp_path)
    try:
        probe = index.probe(staged.build_identity, maximum_results=3)
        assert validate_staged_build(staged, index.inspect(staged.build_identity), probe) == ()
    finally:
        store.close()


def test_chunk_commitment_binds_frozen_knowledge_type_and_guidance_authority(tmp_path) -> None:
    store, _, _, staged = _stage(tmp_path)
    try:
        legacy = staged.document_provenance[0]
        sop = replace(
            legacy, knowledge_type="SOP", guidance_authority="SOP_BACKED_ELIGIBLE",
            document_status=DocumentStatus.ACTIVE, approval_state="APPROVED",
            production_eligible=True,
        )
        runbook = replace(sop, knowledge_type="RUNBOOK")
        chunk = staged.chunks[0]
        values = {
            derive_chunk_metadata_commitment(
                staged.manifest_commitment, document,
                chunk_identity=chunk.chunk_identity,
                section_identity=chunk.section_identity, ordinal=chunk.ordinal,
                content_hash=chunk.content_hash,
            )
            for document in (legacy, sop, runbook)
        }
        assert len(values) == 3
    finally:
        store.close()


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("missing", BuildFailureCode.MISSING_CHUNK),
        ("orphan", BuildFailureCode.ORPHAN_CHUNK),
        ("duplicate", BuildFailureCode.DUPLICATE_CHUNK),
        ("metadata", BuildFailureCode.METADATA_INVALID),
        ("dimension", BuildFailureCode.EMBEDDING_MISMATCH),
        ("integrity", BuildFailureCode.INDEX_INTEGRITY_FAILED),
        ("test_only", BuildFailureCode.ACTIVATION_INELIGIBLE),
    ],
)
def test_validator_rejects_incomplete_or_incompatible_index(tmp_path, mutation, expected) -> None:
    store, index, _service, staged = _stage(tmp_path)
    try:
        artifact = index.artifacts[staged.build_identity]
        entry = artifact.entries[0]
        if mutation == "missing":
            artifact = replace(artifact, entries=())
        elif mutation == "orphan":
            orphan = replace(entry, chunk_identity=f"kchk_{'f' * 64}")
            artifact = replace(artifact, entries=artifact.entries + (orphan,))
        elif mutation == "duplicate":
            artifact = replace(artifact, entries=artifact.entries + (entry,))
        elif mutation == "metadata":
            artifact = replace(artifact, entries=(replace(entry, metadata_commitment=digest("wrong")),))
        elif mutation == "dimension":
            artifact = replace(artifact, entries=(replace(entry, embedding_dimension=4),))
        elif mutation == "integrity":
            artifact = replace(artifact, integrity_ok=False)
        else:
            artifact = replace(artifact, artifact_trust=ArtifactTrust.TEST_ONLY)
        probe = IndexProbeFacts(staged.build_identity, True, 1, 3)
        findings = validate_staged_build(staged, artifact, probe)
        assert expected in {finding.code for finding in findings}
    finally:
        store.close()


def test_failed_bounded_probe_prevents_validation(tmp_path) -> None:
    store, index, _service, staged = _stage(tmp_path)
    try:
        failed = IndexProbeFacts(staged.build_identity, False, 0, 2)
        findings = validate_staged_build(staged, index.inspect(staged.build_identity), failed)
        assert BuildFailureCode.PROBE_FAILED in {finding.code for finding in findings}
    finally:
        store.close()


def test_declared_document_cardinality_mismatch_fails_validation(tmp_path) -> None:
    store, index, _service, staged = _stage(tmp_path)
    try:
        inconsistent = replace(staged, document_count=staged.document_count + 1)
        findings = validate_staged_build(
            inconsistent,
            index.inspect(staged.build_identity),
            index.probe(staged.build_identity, maximum_results=2),
        )
        assert BuildFailureCode.CARDINALITY_MISMATCH in {
            finding.code for finding in findings
        }
    finally:
        store.close()


def test_arbitrary_matching_metadata_commitment_cannot_pass_validation(tmp_path) -> None:
    store, index, _service, staged = _stage(tmp_path)
    try:
        arbitrary = digest("arbitrary-but-valid-shaped-metadata")
        bad_chunk = replace(staged.chunks[0], metadata_commitment=arbitrary)
        bad_entry = replace(staged.artifact.entries[0], metadata_commitment=arbitrary)
        bad_artifact = replace(staged.artifact, entries=(bad_entry,))
        inconsistent = replace(staged, chunks=(bad_chunk,), artifact=bad_artifact)
        findings = validate_staged_build(
            inconsistent,
            bad_artifact,
            IndexProbeFacts(staged.build_identity, True, 1, 2),
        )
        assert BuildFailureCode.METADATA_INVALID in {
            finding.code for finding in findings
        }
    finally:
        store.close()


def test_validation_success_is_durable_but_does_not_activate(tmp_path) -> None:
    store, _index, service, staged = _stage(tmp_path)
    try:
        validation = service.validate(
            staged.build_identity, BuildOperationKey("validate-1"), maximum_probe_results=3
        )
        assert validation.state is BuildValidationState.VALIDATED
        assert store.get_build_validation(staged.build_identity).value == validation
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
    finally:
        store.close()


def test_validation_failure_is_immutable_and_preserves_staged_build(tmp_path) -> None:
    store, index, service, staged = _stage(tmp_path)
    try:
        index.artifacts[staged.build_identity] = replace(staged.artifact, integrity_ok=False)
        validation = service.validate(
            staged.build_identity, BuildOperationKey("validate-failed"), maximum_probe_results=2
        )
        assert validation.state is BuildValidationState.FAILED
        assert store.get_staged_build(staged.build_identity).value == staged
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
        assert service.validate(
            staged.build_identity, BuildOperationKey("validate-failed"), maximum_probe_results=2
        ) == validation
    finally:
        store.close()
