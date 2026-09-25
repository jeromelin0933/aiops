from dataclasses import fields
import hashlib
import socket
import time

import pytest

from knowledge_index import (
    BuildIdentityInput,
    ChunkIdentityInput,
    ContentType,
    DocumentStatus,
    GovernedManifest,
    ManifestDocument,
    MetadataItem,
    SourceClassification,
    build_identity,
    canonical_serialize,
    chunk_identity,
    document_identity,
    document_version_identity,
    manifest_commitment,
    KnowledgeValidationError,
)


HASH_A = hashlib.sha256(b"alpha").hexdigest()
HASH_B = hashlib.sha256(b"beta").hexdigest()


def _document(name: str, version: str, digest: str, *, metadata: tuple[MetadataItem, ...] = ()) -> ManifestDocument:
    return ManifestDocument(
        name,
        version,
        f"sop/{name}.txt",
        digest,
        DocumentStatus.ACTIVE,
        SourceClassification.APPROVED_OPERATIONAL_KNOWLEDGE,
        "GOV-1",
        True,
        ContentType.TEXT_UTF8,
        metadata,
    )


def _manifest(documents: tuple[ManifestDocument, ...]) -> GovernedManifest:
    return GovernedManifest("1.0", "1.0", "MANIFEST-1", "CORPUS-1", "1.0", documents)


def test_canonical_serialization_is_mapping_order_independent_and_sequence_sensitive() -> None:
    assert canonical_serialize({"b": 2, "a": {"y": 2, "x": 1}}) == canonical_serialize(
        {"a": {"x": 1, "y": 2}, "b": 2}
    )
    assert canonical_serialize(["a", "b"]) != canonical_serialize(["b", "a"])
    with pytest.raises(Exception):
        canonical_serialize({"unsupported"})


def test_manifest_commitment_is_semantically_order_independent_but_change_sensitive() -> None:
    first = _document("DOC-A", "1", HASH_A, metadata=(MetadataItem("owner", "ops"),))
    second = _document("DOC-B", "1", HASH_B)
    assert manifest_commitment(_manifest((first, second))) == manifest_commitment(_manifest((second, first)))
    assert manifest_commitment(_manifest((first, second))) != manifest_commitment(
        GovernedManifest("1.0", "1.0", "MANIFEST-1", "CORPUS-1", "2.0", (first, second))
    )


def test_document_version_chunk_and_build_identities_are_deterministic_and_separate() -> None:
    document = document_identity("CORPUS-1", "DOC-A")
    version = document_version_identity(document, "1", HASH_A)
    chunk_input = ChunkIdentityInput(document, version, "section-1", 0, HASH_A, "chunk-v1")
    chunk = chunk_identity(chunk_input)
    build_input = BuildIdentityInput(
        "kmf_" + "a" * 64,
        (chunk,),
        "chunk-v1",
        "1.0",
        "google",
        "text-embedding-004",
        "embedding-v1",
        768,
        "none",
        "chromadb",
        "chroma-schema-v1",
        "knowledge-metadata-v1",
        "build-v1",
    )
    build = build_identity(build_input)
    assert document.startswith("kdoc_")
    assert version.startswith("kver_")
    assert chunk.startswith("kchk_")
    assert build.startswith("kbld_")
    assert len({document, version, chunk, build}) == 4
    assert chunk_identity(chunk_input) == chunk
    assert build_identity(build_input) == build
    changed = BuildIdentityInput(
        build_input.manifest_commitment,
        build_input.ordered_chunk_identities,
        build_input.chunking_profile_identity,
        build_input.canonicalization_version,
        build_input.embedding_provider,
        "different-model",
        build_input.embedding_profile_identity,
        build_input.embedding_dimension,
        build_input.normalization_semantics,
        build_input.index_engine,
        build_input.index_schema_identity,
        build_input.metadata_schema_identity,
        build_input.build_contract_version,
    )
    assert build_identity(changed) != build


def test_identity_has_no_environment_or_secret_inputs(tmp_path, monkeypatch) -> None:
    manifest = _manifest((_document("DOC-A", "1", HASH_A),))
    before = manifest_commitment(manifest)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "different-host")
    monkeypatch.setattr(time, "time", lambda: 9999999999.0)
    assert manifest_commitment(manifest) == before
    build_fields = {field.name for field in fields(BuildIdentityInput)}
    assert not {"secret", "credential", "api_key", "absolute_path", "hostname", "timestamp"} & build_fields


def test_identity_inputs_preserve_order_only_where_spec_requires_it() -> None:
    document = document_identity("CORPUS-1", "DOC-A")
    version = document_version_identity(document, "1", HASH_A)
    one = chunk_identity(ChunkIdentityInput(document, version, "section", 0, HASH_A, "chunk-v1"))
    two = chunk_identity(ChunkIdentityInput(document, version, "section", 1, HASH_B, "chunk-v1"))
    common = dict(
        manifest_commitment="kmf_" + "a" * 64,
        chunking_profile_identity="chunk-v1",
        canonicalization_version="1.0",
        embedding_provider="google",
        embedding_model="text-embedding-004",
        embedding_profile_identity="embedding-v1",
        embedding_dimension=768,
        normalization_semantics="none",
        index_engine="chromadb",
        index_schema_identity="chroma-schema-v1",
        metadata_schema_identity="knowledge-metadata-v1",
        build_contract_version="build-v1",
    )
    assert build_identity(BuildIdentityInput(ordered_chunk_identities=(one, two), **common)) != build_identity(
        BuildIdentityInput(ordered_chunk_identities=(two, one), **common)
    )


def test_identity_boundaries_reject_paths_secrets_and_wrong_namespaces() -> None:
    with pytest.raises(KnowledgeValidationError):
        document_identity("CORPUS-1", "C:/private/document")
    with pytest.raises(KnowledgeValidationError):
        document_identity("CORPUS-1", "token=secret")
    with pytest.raises(KnowledgeValidationError):
        document_version_identity("kbld_" + "a" * 64, "1", HASH_A)
    with pytest.raises(KnowledgeValidationError):
        ChunkIdentityInput("kbld_" + "a" * 64, "kver_" + "b" * 64, "section", 0, HASH_A, "chunk-v1")


@pytest.mark.parametrize(
    "metadata",
    [
        (MetadataItem("api_key", "synthetic-secret-a"),),
        (MetadataItem("owner", "token:synthetic-secret-b"),),
    ],
)
def test_direct_manifest_commitment_rejects_secret_shaped_metadata(metadata) -> None:
    manifest = _manifest((_document("DOC-A", "1", HASH_A, metadata=metadata),))
    with pytest.raises(KnowledgeValidationError, match="secret-shaped"):
        manifest_commitment(manifest)


@pytest.mark.parametrize(
    "secret_profile",
    ["token:synthetic-secret-a", "api_key:synthetic-secret-b"],
)
def test_direct_build_identity_rejects_every_secret_value(secret_profile: str) -> None:
    document = document_identity("CORPUS-1", "DOC-A")
    version = document_version_identity(document, "1", HASH_A)
    chunk = chunk_identity(ChunkIdentityInput(document, version, "section", 0, HASH_A, "chunk-v1"))
    identity_input = BuildIdentityInput(
        "kmf_" + "a" * 64,
        (chunk,),
        "chunk-v1",
        "1.0",
        "google",
        "text-embedding-004",
        secret_profile,
        768,
        "none",
        "chromadb",
        "chroma-schema-v1",
        "knowledge-metadata-v1",
        "build-v1",
    )
    with pytest.raises(KnowledgeValidationError, match="secret-shaped"):
        build_identity(identity_input)


def test_safe_manifest_and_build_identity_inputs_remain_deterministic() -> None:
    manifest = _manifest(
        (_document("DOC-A", "1", HASH_A, metadata=(MetadataItem("owner", "ops"),)),)
    )
    commitment = manifest_commitment(manifest)
    assert manifest_commitment(manifest) == commitment
    document = document_identity("CORPUS-1", "DOC-A")
    version = document_version_identity(document, "1", HASH_A)
    chunk = chunk_identity(ChunkIdentityInput(document, version, "section", 0, HASH_A, "chunk-v1"))
    identity_input = BuildIdentityInput(
        commitment,
        (chunk,),
        "chunk-v1",
        "1.0",
        "google",
        "text-embedding-004",
        "embedding-profile-v1",
        768,
        "none",
        "chromadb",
        "chroma-schema-v1",
        "knowledge-metadata-v1",
        "build-v1",
    )
    assert build_identity(identity_input) == build_identity(identity_input)


@pytest.mark.parametrize(
    "identity_input",
    [
        lambda document, version: ChunkIdentityInput(
            document, version, "token:synthetic-secret", 0, HASH_A, "chunk-v1"
        ),
        lambda document, version: ChunkIdentityInput(
            document, version, "section", 0, HASH_A, "api_key"
        ),
    ],
)
def test_other_public_chunk_identity_paths_reject_secret_shapes(identity_input) -> None:
    document = document_identity("CORPUS-1", "DOC-A")
    version = document_version_identity(document, "1", HASH_A)
    with pytest.raises(KnowledgeValidationError, match="secret-shaped"):
        chunk_identity(identity_input(document, version))
