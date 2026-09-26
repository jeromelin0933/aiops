"""Thin local workflow composition for approved SPEC-014 capabilities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .build import KnowledgeBuildService
from .chroma_index_adapter import ChromaIndexAdapter
from .chunking import plan_chunks
from .config import KnowledgeIndexConfig, load_knowledge_config
from .contracts import (
    ActivationOperationKey,
    BuildActivationRequest,
    BuildIdentityInput,
    BuildOperationKey,
    CanonicalKnowledgeQuery,
    OpaqueExternalReference,
    OpaqueReferenceType,
    QueryFilter,
    RetrievalOperationKey,
    RetrievalOperationRequest,
)
from .google_embedding_adapter import GoogleEmbeddingAdapter, create_google_client
from .identity import canonical_serialize
from .manifest import admit_production_manifest
from .retrieval import KnowledgeRetrievalService
from .snapshot import KnowledgeSnapshotService
from .sqlite_store import SqliteKnowledgeStore


ROOT = Path(__file__).resolve().parents[2]


def _print(value: object) -> None:
    print(canonical_serialize(value))


def _admission(config: KnowledgeIndexConfig):
    result = admit_production_manifest(config.manifest_path, repository_root=ROOT)
    if not result.accepted:
        _print(result)
        raise RuntimeError("production manifest admission failed")
    return result


def _raw_manifest(config: KnowledgeIndexConfig) -> dict[str, object]:
    value = json.loads((ROOT / config.manifest_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("production manifest must be a JSON object")
    return value


def _index(config: KnowledgeIndexConfig) -> ChromaIndexAdapter:
    return ChromaIndexAdapter(ROOT / config.chroma_path, index_schema_identity=config.index_schema_identity)


def _store(config: KnowledgeIndexConfig) -> SqliteKnowledgeStore:
    path = ROOT / config.authority_store_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteKnowledgeStore(path)


def _provider(config: KnowledgeIndexConfig, *, live: bool) -> GoogleEmbeddingAdapter:
    client = create_google_client(timeout_seconds=config.limits.timeout_seconds) if live else None
    return GoogleEmbeddingAdapter(client, config.capability)


def _identity_input(config: KnowledgeIndexConfig, admission, chunks) -> BuildIdentityInput:
    assert admission.manifest_commitment is not None
    return BuildIdentityInput(
        admission.manifest_commitment,
        tuple(chunk.chunk_identity for chunk in chunks),
        config.chunking.profile_identity,
        "1.0",
        config.capability.provider,
        config.capability.model,
        config.capability.embedding_profile_identity,
        config.capability.embedding_dimension,
        config.normalization_semantics,
        "chromadb",
        config.index_schema_identity,
        "spec014-knowledge-metadata-v1",
        "spec014-build-contract-v1",
    )


def _parse_filters(values: list[str]) -> tuple[QueryFilter, ...]:
    filters: list[QueryFilter] = []
    for value in values:
        if "=" not in value:
            raise ValueError("filters must use key=value")
        key, item = value.split("=", 1)
        filters.append(QueryFilter(key, item))
    return tuple(sorted(filters))


def _run(args: argparse.Namespace, config: KnowledgeIndexConfig) -> object:
    if args.command == "admit":
        return _admission(config)
    if args.command == "rebuild":
        admission = _admission(config)
        source_root = ROOT / admission.manifest.governed_source_root  # type: ignore[union-attr]
        chunks = plan_chunks(admission, source_root=source_root, profile=config.chunking)
        identity_input = _identity_input(config, admission, chunks)
        with _store(config) as store:
            service = KnowledgeBuildService(store, _provider(config, live=True), _index(config))
            return service.stage(
                operation_key=BuildOperationKey("rebuild-" + admission.manifest_commitment[-24:]),
                raw_manifest=_raw_manifest(config), source_root=source_root, chunks=chunks,
                identity_input=identity_input, limits=config.limits,
                required_capability_identity=config.capability.capability_identity,
            )
    with _store(config) as store:
        index = _index(config)
        build = KnowledgeBuildService(store, _provider(config, live=False), index)
        if args.command == "validate":
            return build.validate(args.build_id, BuildOperationKey("validate-" + args.build_id[-24:]), maximum_probe_results=12)
        if args.command == "inspect":
            return {
                "staged": store.get_staged_build(args.build_id),
                "validation": store.get_build_validation(args.build_id),
                "artifact": index.inspect(args.build_id),
                "activation": store.read_activation(),
                "readiness": store.local_readiness(
                    required_profile_reference=config.capability.profile_reference,
                    required_capability_identity=config.capability.capability_identity,
                ),
            }
        if args.command == "activate":
            return build.activate(BuildActivationRequest(
                args.build_id, ActivationOperationKey(args.operation_key), args.expected_generation,
            ))
        if args.command == "retrieve":
            provider = _provider(config, live=True)
            retrieval = KnowledgeRetrievalService(store, provider, index)
            snapshots = KnowledgeSnapshotService(store, retrieval)
            request = RetrievalOperationRequest(
                RetrievalOperationKey(args.operation_key),
                CanonicalKnowledgeQuery("1.0", "1.0", args.query.strip(), _parse_filters(args.filter)),
                config.retrieval_profile, config.applicability_policy,
                (OpaqueExternalReference(OpaqueReferenceType.CALLER, "team-local-cli"),),
                config.capability.profile_reference, config.capability.capability_identity,
            )
            return snapshots.resolve(request, config.limits)
    raise RuntimeError("unsupported command")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/knowledge_index.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("admit")
    commands.add_parser("rebuild")
    for name in ("validate", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("--build-id", required=True)
    activate = commands.add_parser("activate")
    activate.add_argument("--build-id", required=True)
    activate.add_argument("--operation-key", required=True)
    activate.add_argument("--expected-generation", required=True, type=int)
    retrieve = commands.add_parser("retrieve")
    retrieve.add_argument("--operation-key", required=True)
    retrieve.add_argument("--query", required=True)
    retrieve.add_argument("--filter", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_knowledge_config(ROOT / args.config)
        _print(_run(args, config))
    except Exception as exc:
        print(f"Knowledge workflow failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0
