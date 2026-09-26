from dataclasses import replace

from knowledge_index import (
    ActivationOperationKey,
    ApplicabilityPolicy,
    BuildActivationRequest,
    BuildOperationKey,
    CanonicalKnowledgeQuery,
    KnowledgeBuildService,
    OpaqueExternalReference,
    OpaqueReferenceType,
    ProviderCapability,
    QueryEmbeddingResult,
    QueryFilter,
    RawRetrievalBatch,
    RawRetrievalCandidate,
    RetrievalOperationKey,
    RetrievalOperationRequest,
    RetrievalProfile,
    RetrievalQueryDisposition,
    RetrievalScoreDirection,
)
from _knowledge_build_testkit import (
    DeterministicIndex,
    DeterministicProvider,
    capability,
    limits,
    manifest_and_plan,
)


def profile(**changes):
    values = dict(
        profile_identity="retrieval-profile-v1",
        version="v1",
        canonicalization_version="1.0",
        embedding_profile_identity="embedding-profile-v1",
        provider="google",
        model="text-embedding-004",
        embedding_dimension=3,
        index_engine="chroma",
        index_schema_identity="index-schema-v1",
        max_query_bytes=1024,
        allowed_filter_keys=("service",),
        top_k=3,
        candidate_limit=8,
        score_direction=RetrievalScoreDirection.HIGHER_IS_BETTER,
        score_precision=6,
        max_content_bytes_per_result=4096,
        max_metadata_bytes_per_result=1024,
        max_total_payload_bytes=8192,
        empty_query_disposition=RetrievalQueryDisposition.INVALID,
        unsupported_query_disposition=RetrievalQueryDisposition.INVALID,
    )
    values.update(changes)
    return RetrievalProfile(**values)


def policy(**changes):
    values = dict(
        policy_identity="applicability-v1",
        version="v1",
        required_filter_keys=("service",),
        direct_threshold=0.9,
        partial_threshold=0.7,
        contextual_threshold=0.5,
    )
    values.update(changes)
    return ApplicabilityPolicy(**values)


def request(name="retrieve-1", **changes):
    values = dict(
        operation_key=RetrievalOperationKey(name),
        query=CanonicalKnowledgeQuery(
            "1.0", "1.0", "payments procedure", (QueryFilter("service", "payments"),)
        ),
        retrieval_profile=profile(),
        applicability_policy=policy(),
        external_references=(
            OpaqueExternalReference(OpaqueReferenceType.CALLER, "caller-1"),
        ),
        profile_reference=capability().profile_reference,
        capability_identity=capability().capability_identity,
    )
    values.update(changes)
    return RetrievalOperationRequest(**values)


class QueryProvider:
    def __init__(self, selected: ProviderCapability | None = None, *, error=None, failure=None):
        self.selected = selected or capability()
        self.error = error
        self.failure = failure
        self.calls = 0
        self.requests = []

    def capability(self):
        return self.selected

    def embed_query(self, value):
        self.calls += 1
        self.requests.append(value)
        if self.error:
            raise self.error
        if self.failure:
            return QueryEmbeddingResult(self.selected, (), 1, self.failure)
        return QueryEmbeddingResult(self.selected, (1.0, 2.0, 3.0), 1)


class RetrievalIndex:
    def __init__(self, artifacts, candidates=(), *, inspect_error=None, query_error=None):
        self.artifacts = artifacts
        self.candidates = tuple(candidates)
        self.inspect_error = inspect_error
        self.query_error = query_error
        self.inspect_calls = []
        self.query_calls = []

    def inspect(self, build_identity):
        self.inspect_calls.append(build_identity)
        if self.inspect_error:
            raise self.inspect_error
        return self.artifacts.get(build_identity)

    def query(self, build_identity, vector, *, candidate_limit):
        self.query_calls.append((build_identity, vector, candidate_limit))
        if self.query_error:
            raise self.query_error
        artifact = self.artifacts[build_identity]
        return RawRetrievalBatch(build_identity, artifact.artifact_commitment, self.candidates)


def stage_activate(store, root, name="one", *, expected_generation=0, index=None):
    root.mkdir(exist_ok=True)
    selected_index = index or DeterministicIndex()
    build_service = KnowledgeBuildService(store, DeterministicProvider(), selected_index)
    raw, chunks, identity_input = manifest_and_plan(
        root, content=f"approved operational procedure {name}", manifest_id=f"manifest-{name}"
    )
    staged = build_service.stage(
        operation_key=BuildOperationKey(f"stage-{name}"), raw_manifest=raw,
        source_root=root, chunks=chunks, identity_input=identity_input,
        limits=limits(), required_capability_identity="embedding-capability-v1",
    ).record
    assert staged is not None
    build_service.validate(
        staged.build_identity, BuildOperationKey(f"validate-{name}"), maximum_probe_results=3
    )
    build_service.activate(BuildActivationRequest(
        staged.build_identity, ActivationOperationKey(f"activate-{name}"), expected_generation
    ))
    return staged, selected_index


def candidates_for(staged, *scores):
    if not scores:
        scores = (0.95,)
    return tuple(
        RawRetrievalCandidate(chunk.chunk_identity, score, chunk.metadata_commitment)
        for chunk, score in zip(staged.chunks, scores)
    )
