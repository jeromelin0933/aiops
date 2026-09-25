import hashlib
from pathlib import Path

from knowledge_index import (
    ArtifactTrust,
    BuildChunk,
    BuildDocumentProvenance,
    BuildFailureCode,
    BuildFailureFact,
    BuildIdentityInput,
    ChunkIdentityInput,
    EmbeddingVector,
    IndexArtifactFacts,
    IndexEntryFact,
    IndexProbeFacts,
    OpaqueExternalReference,
    OpaqueReferenceType,
    ProviderCapability,
    ProviderEmbeddingRequest,
    ProviderEmbeddingResult,
    ProviderInvocationLimits,
    RetrySafetyDisposition,
    chunk_identity,
    derive_chunk_metadata_commitment,
)
from knowledge_index.manifest import admit_manifest


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def limits(**changes) -> ProviderInvocationLimits:
    values = dict(
        timeout_seconds=2.0,
        maximum_request_bytes=10_000,
        maximum_batch_items=16,
        maximum_invocations=1,
        maximum_cost_units=100,
        maximum_rate_units=100,
        maximum_quota_units=100,
        maximum_resource_units=100,
    )
    values.update(changes)
    return ProviderInvocationLimits(**values)


def capability(**changes) -> ProviderCapability:
    values = dict(
        profile_reference=OpaqueExternalReference(
            OpaqueReferenceType.CREDENTIAL_PROFILE, "profile-approved-1"
        ),
        capability_identity="embedding-capability-v1",
        provider="google",
        model="text-embedding-004",
        embedding_profile_identity="embedding-profile-v1",
        embedding_dimension=3,
        hidden_retries_disabled=True,
    )
    values.update(changes)
    return ProviderCapability(**values)


def manifest_and_plan(
    root: Path,
    *,
    content: str = "approved operational procedure",
    name: str = "sop.txt",
    manifest_id: str = "manifest-1",
    profile: ProviderCapability | None = None,
    index_schema: str = "index-schema-v1",
) -> tuple[dict[str, object], tuple[BuildChunk, ...], BuildIdentityInput]:
    selected = profile or capability()
    (root / name).write_text(content, encoding="utf-8")
    content_digest = digest(content)
    raw: dict[str, object] = {
        "schema_version": "1.0",
        "canonicalization_version": "1.0",
        "manifest_id": manifest_id,
        "corpus_id": "operations",
        "corpus_version": manifest_id,
        "documents": [{
            "document_id": f"doc-{manifest_id}",
            "document_version": "v1",
            "source_path": name,
            "expected_content_hash": content_digest,
            "status": "ACTIVE",
            "source_classification": "APPROVED_OPERATIONAL_KNOWLEDGE",
            "approval_reference": "approval-1",
            "outbound_eligible": True,
            "content_type": "text/plain; charset=utf-8",
            "metadata": {"service": "payments"},
        }],
    }
    admitted = admit_manifest(raw, source_root=root)
    assert admitted.accepted and admitted.manifest_commitment
    document = admitted.admitted_documents[0]
    governed_document = admitted.manifest.documents[0]
    provenance = BuildDocumentProvenance(
        document.document_identity,
        document.document_version_identity,
        document.source_path,
        document.content_hash,
        governed_document.approval_reference,
        governed_document.source_classification,
        governed_document.outbound_eligible,
        governed_document.content_type,
        governed_document.metadata,
    )
    chunk_input = ChunkIdentityInput(
        document.document_identity,
        document.document_version_identity,
        "section-1",
        0,
        content_digest,
        "chunk-profile-v1",
    )
    metadata_commitment = derive_chunk_metadata_commitment(
        admitted.manifest_commitment,
        provenance,
        chunk_identity=chunk_identity(chunk_input),
        section_identity="section-1",
        ordinal=0,
        content_hash=content_digest,
    )
    chunk = BuildChunk(
        chunk_identity(chunk_input),
        document.document_identity,
        document.document_version_identity,
        "section-1",
        0,
        content,
        content_digest,
        metadata_commitment,
    )
    identity_input = BuildIdentityInput(
        admitted.manifest_commitment,
        (chunk.chunk_identity,),
        "chunk-profile-v1",
        "1.0",
        selected.provider,
        selected.model,
        selected.embedding_profile_identity,
        selected.embedding_dimension,
        "l2-normalized-v1",
        "chroma",
        index_schema,
        "metadata-schema-v1",
        "build-contract-v1",
    )
    return raw, (chunk,), identity_input


class DeterministicProvider:
    def __init__(
        self,
        selected: ProviderCapability | None = None,
        *,
        failure: BuildFailureFact | None = None,
        raises: Exception | None = None,
        invocation_count: int = 1,
        cost_units: int = 1,
        rate_units: int = 1,
        quota_units: int = 1,
        resource_units: int = 1,
        capability_error: Exception | None = None,
    ) -> None:
        self.selected = selected or capability()
        self.failure = failure
        self.raises = raises
        self.result_invocation_count = invocation_count
        self.cost_units = cost_units
        self.rate_units = rate_units
        self.quota_units = quota_units
        self.resource_units = resource_units
        self.capability_error = capability_error
        self.calls = 0
        self.requests: list[ProviderEmbeddingRequest] = []

    def capability(self) -> ProviderCapability:
        if self.capability_error is not None:
            raise self.capability_error
        return self.selected

    def embed(self, request: ProviderEmbeddingRequest) -> ProviderEmbeddingResult:
        self.calls += 1
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if self.failure is not None:
            return ProviderEmbeddingResult(
                self.selected, (), self.result_invocation_count, self.cost_units,
                self.rate_units, self.quota_units, self.resource_units, self.failure
            )
        vectors = tuple(
            EmbeddingVector(
                chunk.chunk_identity,
                tuple(float(index + 1) for index in range(self.selected.embedding_dimension)),
            )
            for chunk in request.chunks
        )
        return ProviderEmbeddingResult(
            self.selected, vectors, self.result_invocation_count, self.cost_units,
            self.rate_units, self.quota_units, self.resource_units
        )


class DeterministicIndex:
    def __init__(
        self,
        *,
        trust: ArtifactTrust = ArtifactTrust.APPROVED,
        stage_error: Exception | None = None,
    ) -> None:
        self.trust = trust
        self.stage_error = stage_error
        self.artifacts: dict[str, IndexArtifactFacts] = {}
        self.stage_calls = 0
        self.inspect_calls: list[str] = []
        self.probe_calls: list[tuple[str, int]] = []

    def stage(
        self,
        build_identity: str,
        request: ProviderEmbeddingRequest,
        result: ProviderEmbeddingResult,
    ) -> IndexArtifactFacts:
        self.stage_calls += 1
        if self.stage_error is not None:
            raise self.stage_error
        entries = tuple(
            IndexEntryFact(
                chunk.chunk_identity,
                chunk.metadata_commitment,
                request.capability.embedding_dimension,
            )
            for chunk in request.chunks
        )
        artifact = IndexArtifactFacts(
            build_identity,
            digest("artifact:" + build_identity),
            self.trust,
            request.capability.provider,
            request.capability.model,
            request.capability.embedding_profile_identity,
            request.capability.embedding_dimension,
            "chroma",
            "index-schema-v1",
            entries,
            True,
        )
        existing = self.artifacts.get(build_identity)
        if existing is not None and existing != artifact:
            raise RuntimeError("contradictory immutable artifact")
        self.artifacts[build_identity] = artifact
        return artifact

    def inspect(self, build_identity: str) -> IndexArtifactFacts | None:
        self.inspect_calls.append(build_identity)
        return self.artifacts.get(build_identity)

    def probe(self, build_identity: str, *, maximum_results: int) -> IndexProbeFacts:
        self.probe_calls.append((build_identity, maximum_results))
        artifact = self.artifacts.get(build_identity)
        return IndexProbeFacts(
            build_identity,
            artifact is not None and artifact.integrity_ok,
            min(len(artifact.entries), maximum_results) if artifact is not None else 0,
            maximum_results,
        )


def provider_failure(code: BuildFailureCode = BuildFailureCode.PROVIDER_UNAVAILABLE) -> BuildFailureFact:
    return BuildFailureFact(
        code,
        "provider",
        "bounded provider failure",
        RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED,
    )
