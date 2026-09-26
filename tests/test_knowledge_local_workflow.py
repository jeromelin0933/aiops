import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("chromadb")

from knowledge_index import (
    ActivationOperationKey, BuildActivationRequest, BuildIdentityInput, BuildOperationKey,
    CanonicalKnowledgeQuery, KnowledgeBuildService, KnowledgeLocalReadiness,
    KnowledgeRetrievalService, KnowledgeSnapshotService, OpaqueExternalReference,
    OpaqueReferenceType, QueryFilter, RetrievalOperationKey, RetrievalOperationRequest,
    SqliteKnowledgeStore, admit_production_manifest, load_knowledge_config, plan_chunks,
)
from knowledge_index.chroma_index_adapter import ChromaIndexAdapter
from knowledge_index.cli import build_parser
from knowledge_index.google_embedding_adapter import GoogleEmbeddingAdapter


ROOT = Path(__file__).resolve().parents[1]


class FakeModels:
    def embed_content(self, **kwargs):
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * 768) for _ in kwargs["contents"]])


def test_fake_provider_team_local_workflow_closes_all_candidate_c_steps(tmp_path: Path) -> None:
    config = load_knowledge_config(ROOT / "configs" / "knowledge_index.yaml")
    admission = admit_production_manifest("configs/knowledge_manifest.json", repository_root=ROOT)
    chunks = plan_chunks(admission, source_root=ROOT / "docs" / "knowledge", profile=config.chunking)
    identity = BuildIdentityInput(
        admission.manifest_commitment, tuple(item.chunk_identity for item in chunks),
        config.chunking.profile_identity, "1.0", "google", "text-embedding-004",
        config.capability.embedding_profile_identity, 768, config.normalization_semantics,
        "chromadb", config.index_schema_identity, "spec014-knowledge-metadata-v1",
        "spec014-build-contract-v1",
    )
    raw = json.loads((ROOT / config.manifest_path).read_text(encoding="utf-8"))
    provider = GoogleEmbeddingAdapter(SimpleNamespace(models=FakeModels()), config.capability)
    index = ChromaIndexAdapter(tmp_path / "chroma", index_schema_identity=config.index_schema_identity)
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        builds = KnowledgeBuildService(store, provider, index)
        staged = builds.stage(
            operation_key=BuildOperationKey("fake-local-rebuild"), raw_manifest=raw,
            source_root=ROOT / "docs" / "knowledge", chunks=chunks, identity_input=identity,
            limits=config.limits, required_capability_identity=config.capability.capability_identity,
        )
        assert staged.record is not None
        inspected = index.inspect(staged.record.build_identity)
        assert inspected is not None
        assert inspected.build_identity == staged.record.build_identity
        validation = builds.validate(staged.record.build_identity, BuildOperationKey("fake-local-validate"), maximum_probe_results=12)
        assert not validation.findings
        activation = builds.activate(BuildActivationRequest(staged.record.build_identity, ActivationOperationKey("fake-local-activate"), 0))
        assert activation.authority.active_build_identity == staged.record.build_identity
        assert store.local_readiness(
            required_profile_reference=config.capability.profile_reference,
            required_capability_identity=config.capability.capability_identity,
        ).status is KnowledgeLocalReadiness.READY
        metadata = {item.key: item.value for item in admission.manifest.documents[0].metadata}
        filters = tuple(QueryFilter(key, metadata[key]) for key in config.applicability_policy.required_filter_keys)
        request = RetrievalOperationRequest(
            RetrievalOperationKey("fake-local-retrieve"),
            CanonicalKnowledgeQuery("1.0", "1.0", "credential abuse response", filters),
            config.retrieval_profile, config.applicability_policy,
            (OpaqueExternalReference(OpaqueReferenceType.CALLER, "test-caller"),),
            config.capability.profile_reference, config.capability.capability_identity,
        )
        outcome = KnowledgeSnapshotService(
            store, KnowledgeRetrievalService(store, provider, index)
        ).resolve(request, config.limits)
        assert outcome.snapshot is not None
        assert outcome.snapshot.frozen_build_identity == staged.record.build_identity


def test_thin_cli_exposes_only_the_frozen_team_local_workflow_commands() -> None:
    parser = build_parser()
    commands = {
        parser.parse_args(["admit"]).command,
        parser.parse_args(["rebuild"]).command,
        parser.parse_args(["validate", "--build-id", "kbld_" + "0" * 64]).command,
        parser.parse_args(["inspect", "--build-id", "kbld_" + "0" * 64]).command,
        parser.parse_args([
            "activate", "--build-id", "kbld_" + "0" * 64,
            "--operation-key", "activate-local", "--expected-generation", "0",
        ]).command,
        parser.parse_args([
            "retrieve", "--operation-key", "retrieve-local", "--query", "timeout",
        ]).command,
    }
    assert commands == {"admit", "rebuild", "validate", "inspect", "activate", "retrieve"}
