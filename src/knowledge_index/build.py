"""Fresh-admission immutable build, validation, and explicit activation service."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path

from .build_ports import EmbeddingProviderPort, StagedIndexPort
from .build_validation import validate_staged_build
from .build_validation import (
    derive_build_lineage_commitment,
    derive_build_operation_commitment,
    derive_build_validation_commitment,
    derive_chunk_metadata_commitment,
    derive_staged_build_commitment,
)
from .contracts import (
    ActivationAuthorityRecord,
    ArtifactTrust,
    BuildActivationRequest,
    BuildActivationResult,
    BuildChunk,
    BuildDocumentProvenance,
    BuildFailureCode,
    BuildFailureFact,
    BuildIdentityInput,
    BuildManifestProvenance,
    BuildOperationKey,
    BuildOperationClaim,
    BuildStageResult,
    BuildValidationRecord,
    BuildValidationState,
    ChunkIdentityInput,
    KnowledgeCorruptionFinding,
    KnowledgeLocalReadiness,
    KnowledgePersistence,
    KnowledgeReadinessFact,
    KnowledgeReadStatus,
    ProviderEmbeddingRequest,
    ProviderInvocationLimits,
    RetrySafetyDisposition,
    StagedBuildRecord,
)
from .identity import build_identity, canonical_serialize, chunk_identity
from .manifest import admit_manifest
from .security import preflight_outbound_content, sanitize_failure_detail


class KnowledgeBuildError(RuntimeError):
    def __init__(self, failure: BuildFailureFact) -> None:
        super().__init__(failure.detail)
        self.failure = failure


def _digest(namespace: str, value: object) -> str:
    envelope = {"namespace": namespace, "version": "1.0", "value": value}
    return hashlib.sha256(canonical_serialize(envelope).encode("utf-8")).hexdigest()


def _failure(
    code: BuildFailureCode,
    field: str,
    detail: object,
    retry: RetrySafetyDisposition = RetrySafetyDisposition.DO_NOT_RETRY,
) -> BuildFailureFact:
    return BuildFailureFact(code, field, sanitize_failure_detail(detail), retry)


class KnowledgeBuildService:
    """Candidate-C semantic layer; Runtime and credentials remain external."""

    def __init__(
        self,
        store: KnowledgePersistence,
        provider: EmbeddingProviderPort,
        index: StagedIndexPort,
    ) -> None:
        required_store_methods = (
            "claim_build_operation", "create_staged_build", "get_staged_build", "create_build_validation",
            "get_build_validation", "commit_activation", "read_activation",
        )
        if any(not callable(getattr(store, method, None)) for method in required_store_methods):
            raise TypeError("store does not implement the Candidate-C build persistence boundary")
        if not isinstance(provider, EmbeddingProviderPort):
            raise TypeError("provider must implement EmbeddingProviderPort")
        if not isinstance(index, StagedIndexPort):
            raise TypeError("index must implement StagedIndexPort")
        self._store = store
        self._provider = provider
        self._index = index

    def stage(
        self,
        *,
        operation_key: BuildOperationKey,
        raw_manifest: Mapping[str, object],
        source_root: str | Path,
        chunks: tuple[BuildChunk, ...],
        identity_input: BuildIdentityInput,
        limits: ProviderInvocationLimits,
        required_capability_identity: str,
    ) -> BuildStageResult:
        admission = admit_manifest(raw_manifest, source_root=source_root)
        if not admission.accepted or admission.manifest_commitment is None:
            return BuildStageResult(None, (_failure(
                BuildFailureCode.ADMISSION_REJECTED,
                "manifest",
                "governed manifest admission failed before provider invocation",
            ),))

        assert admission.manifest is not None
        governed_format = admission.manifest.schema_version == "1.1"
        expected_contract = (
            "spec014-build-contract-v2" if governed_format
            else "build-contract-v1"
        )
        if identity_input.build_contract_version != expected_contract:
            return BuildStageResult(None, (_failure(
                BuildFailureCode.INVALID_CHUNK_PLAN,
                "build_contract_version",
                "build contract version is incompatible with admitted provenance format",
            ),))
        manifest_documents = {
            document.source_path: document for document in admission.manifest.documents
        }
        document_provenance = tuple(sorted((
            BuildDocumentProvenance(
                admitted.document_identity,
                admitted.document_version_identity,
                admitted.source_path,
                admitted.content_hash,
                manifest_documents[admitted.source_path].approval_reference,
                manifest_documents[admitted.source_path].source_classification,
                manifest_documents[admitted.source_path].outbound_eligible,
                manifest_documents[admitted.source_path].content_type,
                manifest_documents[admitted.source_path].metadata,
                manifest_documents[admitted.source_path].knowledge_type if governed_format else "",
                manifest_documents[admitted.source_path].guidance_authority if governed_format else "",
                manifest_documents[admitted.source_path].status if governed_format else None,
                manifest_documents[admitted.source_path].approval_state if governed_format else "",
                manifest_documents[admitted.source_path].production_eligible if governed_format else None,
            )
            for admitted in admission.admitted_documents
        ), key=lambda item: (item.document_identity, item.document_version_identity)))
        plan_failure = self._validate_stage_plan(
            admission.manifest_commitment, admission.admitted_documents, document_provenance,
            Path(source_root), chunks, identity_input,
        )
        if plan_failure is not None:
            return BuildStageResult(None, (plan_failure,))

        build_id = build_identity(identity_input)
        try:
            capability = self._provider.capability()
        except Exception as exc:
            return BuildStageResult(None, (_failure(
                BuildFailureCode.PROVIDER_UNAVAILABLE,
                "provider_capability",
                exc,
                RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED,
            ),))
        capability_matches = (
            capability.capability_identity == required_capability_identity
            and capability.provider == identity_input.embedding_provider
            and capability.model == identity_input.embedding_model
            and capability.embedding_profile_identity == identity_input.embedding_profile_identity
            and capability.embedding_dimension == identity_input.embedding_dimension
            and capability.profile_reference.reference_type.value == "CREDENTIAL_PROFILE"
        )
        if not capability_matches or not capability.hidden_retries_disabled:
            return BuildStageResult(None, (_failure(
                BuildFailureCode.PROFILE_MISMATCH,
                "provider_capability",
                "provider capability or hidden-retry policy does not match the build",
            ),))

        claim = BuildOperationClaim(
            operation_key,
            derive_build_operation_commitment(
                operation_key, build_id, capability.profile_reference,
                capability.capability_identity,
            ),
            build_id,
            capability.profile_reference,
            capability.capability_identity,
        )
        try:
            self._store.claim_build_operation(claim)  # type: ignore[attr-defined]
        except Exception as exc:
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.INDEX_CONFLICT,
                "build_operation",
                exc,
                RetrySafetyDisposition.SAME_OPERATION_ONLY,
            )) from exc

        existing_result = self._store.get_staged_build(build_id)  # type: ignore[attr-defined]
        if existing_result.status is KnowledgeReadStatus.FOUND:
            existing = existing_result.value
            assert isinstance(existing, StagedBuildRecord)
            if (
                existing.operation_key == operation_key
                and existing.build_input == identity_input
                and existing.chunks == chunks
                and existing.profile_reference == capability.profile_reference
                and existing.capability_identity == capability.capability_identity
            ):
                return BuildStageResult(existing)
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.INDEX_CONFLICT,
                "build_identity",
                "existing immutable staged build contradicts this operation",
            ))
        if existing_result.status not in (KnowledgeReadStatus.NOT_FOUND,):
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.REPAIR_REQUIRED,
                "staged_build",
                "staged build authority cannot be safely interpreted",
            ))

        request_size = sum(len(chunk.content.encode("utf-8")) for chunk in chunks)
        if (
            request_size > limits.maximum_request_bytes
            or len(chunks) > limits.maximum_batch_items
            or limits.maximum_invocations < 1
        ):
            return BuildStageResult(None, (_failure(
                BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED,
                "provider_limits",
                "provider request exceeds an approved invocation bound",
            ),))

        request = ProviderEmbeddingRequest(build_id, capability, chunks, limits)
        try:
            provider_result = self._provider.embed(request)
        except Exception as exc:
            return BuildStageResult(None, (_failure(
                BuildFailureCode.PROVIDER_UNAVAILABLE,
                "provider",
                exc,
                RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED,
            ),))
        if provider_result.failure is not None:
            return BuildStageResult(None, (provider_result.failure,))
        if (
            provider_result.capability != capability
            or provider_result.invocation_count != 1
            or provider_result.invocation_count > limits.maximum_invocations
            or provider_result.cost_units > limits.maximum_cost_units
            or provider_result.rate_units > limits.maximum_rate_units
            or provider_result.quota_units > limits.maximum_quota_units
            or provider_result.resource_units > limits.maximum_resource_units
            or tuple(item.chunk_identity for item in provider_result.embeddings)
            != tuple(chunk.chunk_identity for chunk in chunks)
            or any(len(item.values) != identity_input.embedding_dimension for item in provider_result.embeddings)
        ):
            return BuildStageResult(None, (_failure(
                BuildFailureCode.PROVIDER_CONTRACT_INVALID,
                "provider_result",
                "provider result violates identity, dimension, invocation, or cost bounds",
            ),))

        try:
            artifact = self._index.stage(build_id, request, provider_result)
        except Exception as exc:
            return BuildStageResult(None, (_failure(
                BuildFailureCode.INDEX_UNAVAILABLE,
                "index",
                exc,
                RetrySafetyDisposition.SAME_OPERATION_ONLY,
            ),))
        if (
            artifact.build_identity != build_id
            or artifact.provider != identity_input.embedding_provider
            or artifact.model != identity_input.embedding_model
            or artifact.embedding_profile_identity != identity_input.embedding_profile_identity
            or artifact.embedding_dimension != identity_input.embedding_dimension
            or artifact.index_engine != identity_input.index_engine
            or artifact.index_schema_identity != identity_input.index_schema_identity
        ):
            return BuildStageResult(None, (_failure(
                BuildFailureCode.INDEX_CONFLICT,
                "artifact",
                "staged artifact compatibility contradicts the declared build",
            ),))

        manifest_provenance = BuildManifestProvenance(
            admission.manifest.manifest_id,
            admission.manifest.schema_identity,
            admission.manifest.schema_version,
            admission.manifest.canonicalization_version,
            admission.manifest_commitment,
            admission.manifest.corpus_id,
            admission.manifest.corpus_version,
        ) if governed_format else None
        lineage_commitment = derive_build_lineage_commitment(
            build_id, admission.manifest_commitment, chunks,
            capability.profile_reference, capability.capability_identity,
            manifest_provenance,
        )
        staged_commitment = derive_staged_build_commitment(
            lineage_commitment, artifact, identity_input
        )
        record = StagedBuildRecord(
            build_id, operation_key, admission.manifest_commitment, lineage_commitment,
            staged_commitment, identity_input, capability.profile_reference,
            capability.capability_identity, len(admission.admitted_documents),
            document_provenance, chunks, artifact,
            manifest_provenance=manifest_provenance,
        )
        try:
            durable = self._store.create_staged_build(record)  # type: ignore[attr-defined]
        except Exception as exc:
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.REPAIR_REQUIRED,
                "staged_build",
                exc,
                RetrySafetyDisposition.SAME_OPERATION_ONLY,
            )) from exc
        return BuildStageResult(durable)

    def validate(
        self,
        build_identity_value: str,
        operation_key: BuildOperationKey,
        *,
        maximum_probe_results: int,
    ) -> BuildValidationRecord:
        staged_result = self._store.get_staged_build(build_identity_value)  # type: ignore[attr-defined]
        if staged_result.status is not KnowledgeReadStatus.FOUND:
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.BUILD_NOT_STAGED, "build_identity",
                "exact staged build is not available",
            ))
        staged = staged_result.value
        assert isinstance(staged, StagedBuildRecord)
        try:
            artifact = self._index.inspect(build_identity_value)
            probe = (
                self._index.probe(build_identity_value, maximum_results=maximum_probe_results)
                if artifact is not None else None
            )
        except Exception:
            artifact = None
            probe = None
        findings = validate_staged_build(staged, artifact, probe)
        state = BuildValidationState.VALIDATED if not findings else BuildValidationState.FAILED
        validation_commitment = derive_build_validation_commitment(
            build_identity_value, operation_key, staged.staged_commitment, state, findings
        )
        record = BuildValidationRecord(
            build_identity_value, operation_key, staged.staged_commitment,
            validation_commitment, state, findings,
        )
        return self._store.create_build_validation(record)  # type: ignore[attr-defined]

    def activate(self, request: BuildActivationRequest) -> BuildActivationResult:
        staged_result = self._store.get_staged_build(request.build_identity)  # type: ignore[attr-defined]
        validation_result = self._store.get_build_validation(request.build_identity)  # type: ignore[attr-defined]
        if staged_result.status is not KnowledgeReadStatus.FOUND:
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.BUILD_NOT_STAGED, "build_identity", "build is not staged",
            ))
        if validation_result.status is not KnowledgeReadStatus.FOUND:
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.BUILD_NOT_VALIDATED, "build_identity", "build has no validation",
            ))
        staged = staged_result.value
        validation = validation_result.value
        assert isinstance(staged, StagedBuildRecord)
        assert isinstance(validation, BuildValidationRecord)
        if (
            validation.state is not BuildValidationState.VALIDATED
            or validation.findings
            or validation.staged_commitment != staged.staged_commitment
        ):
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.ACTIVATION_INELIGIBLE,
                "validation",
                "build did not pass immutable validation",
            ))
        try:
            artifact = self._index.inspect(request.build_identity)
        except Exception as exc:
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.INDEX_UNAVAILABLE, "artifact", exc,
                RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED,
            )) from exc
        if (
            artifact != staged.artifact
            or artifact is None
            or artifact.artifact_trust is not ArtifactTrust.APPROVED
            or not artifact.integrity_ok
        ):
            raise KnowledgeBuildError(_failure(
                BuildFailureCode.ACTIVATION_INELIGIBLE,
                "artifact",
                "exact staged artifact is not activation eligible",
            ))
        generation = request.expected_generation + 1
        result_commitment = _digest("KNOWLEDGE_ACTIVATION_RESULT", {
            "build_identity": request.build_identity,
            "operation_key": request.operation_key,
            "generation": generation,
            "validation_commitment": validation.validation_commitment,
        })
        authority = ActivationAuthorityRecord(
            generation, request.operation_key, request.build_identity,
            validation.validation_commitment, result_commitment,
        )
        durable = self._store.commit_activation(
            authority, expected_generation=request.expected_generation  # type: ignore[attr-defined]
        )
        return BuildActivationResult(
            durable, staged.staged_commitment, validation.validation_commitment
        )

    def recover_active(self) -> KnowledgeReadinessFact:
        activation_result = self._store.read_activation()
        if activation_result.status is KnowledgeReadStatus.NOT_FOUND:
            return KnowledgeReadinessFact(KnowledgeLocalReadiness.NOT_INITIALIZED)
        if activation_result.status is KnowledgeReadStatus.UNAVAILABLE:
            return KnowledgeReadinessFact(
                KnowledgeLocalReadiness.UNAVAILABLE,
                findings=activation_result.findings,
            )
        if activation_result.status is not KnowledgeReadStatus.FOUND:
            return KnowledgeReadinessFact(
                KnowledgeLocalReadiness.REPAIR_REQUIRED,
                findings=activation_result.findings,
            )
        authority = activation_result.value
        assert isinstance(authority, ActivationAuthorityRecord)
        staged_result = self._store.get_staged_build(authority.active_build_identity)  # type: ignore[attr-defined]
        validation_result = self._store.get_build_validation(authority.active_build_identity)  # type: ignore[attr-defined]
        if (
            staged_result.status is not KnowledgeReadStatus.FOUND
            or validation_result.status is not KnowledgeReadStatus.FOUND
        ):
            return self._repair_readiness("active build stage or validation is missing")
        staged = staged_result.value
        validation = validation_result.value
        assert isinstance(staged, StagedBuildRecord)
        assert isinstance(validation, BuildValidationRecord)
        try:
            artifact = self._index.inspect(authority.active_build_identity)
        except Exception:
            return KnowledgeReadinessFact(KnowledgeLocalReadiness.UNAVAILABLE)
        if (
            validation.state is not BuildValidationState.VALIDATED
            or validation.validation_commitment != authority.validated_build_commitment
            or validation.staged_commitment != staged.staged_commitment
            or artifact != staged.artifact
            or artifact is None
            or not artifact.integrity_ok
            or artifact.artifact_trust is not ArtifactTrust.APPROVED
        ):
            return self._repair_readiness("active build lineage or artifact is inconsistent")
        return KnowledgeReadinessFact(
            KnowledgeLocalReadiness.READY,
            active_build_identity=authority.active_build_identity,
            generation=authority.generation,
        )

    @staticmethod
    def _repair_readiness(detail: str) -> KnowledgeReadinessFact:
        finding = KnowledgeCorruptionFinding(
            "BUILD_RECOVERY_INVALID", "build_recovery", "active", detail
        )
        return KnowledgeReadinessFact(
            KnowledgeLocalReadiness.REPAIR_REQUIRED, findings=(finding,)
        )

    @staticmethod
    def _validate_stage_plan(
        manifest_commitment_value: str,
        admitted_documents: tuple[object, ...],
        document_provenance: tuple[BuildDocumentProvenance, ...],
        source_root: Path,
        chunks: tuple[BuildChunk, ...],
        identity_input: BuildIdentityInput,
    ) -> BuildFailureFact | None:
        if not isinstance(chunks, tuple) or not chunks:
            return _failure(BuildFailureCode.INVALID_CHUNK_PLAN, "chunks", "chunk plan is empty")
        if identity_input.manifest_commitment != manifest_commitment_value:
            return _failure(BuildFailureCode.INVALID_CHUNK_PLAN, "manifest_commitment", "Build Identity manifest commitment does not match admission")
        chunk_ids = tuple(chunk.chunk_identity for chunk in chunks)
        if chunk_ids != identity_input.ordered_chunk_identities or len(set(chunk_ids)) != len(chunk_ids):
            return _failure(BuildFailureCode.INVALID_CHUNK_PLAN, "chunks", "ordered chunk identities are missing, duplicate, or contradictory")
        admitted_by_version = {
            getattr(item, "document_version_identity"): item for item in admitted_documents
        }
        provenance_by_version = {
            item.document_version_identity: item for item in document_provenance
        }
        covered: set[str] = set()
        for chunk in chunks:
            expected = chunk_identity(ChunkIdentityInput(
                chunk.document_identity, chunk.document_version_identity,
                chunk.section_identity, chunk.ordinal, chunk.content_hash,
                identity_input.chunking_profile_identity,
            ))
            admitted = admitted_by_version.get(chunk.document_version_identity)
            provenance = provenance_by_version.get(chunk.document_version_identity)
            if expected != chunk.chunk_identity or admitted is None or getattr(admitted, "document_identity") != chunk.document_identity:
                return _failure(BuildFailureCode.INVALID_CHUNK_PLAN, "chunk_identity", "chunk identity or admitted document provenance is invalid")
            if (
                provenance is None
                or provenance.document_identity != chunk.document_identity
                or chunk.metadata_commitment
                != derive_chunk_metadata_commitment(
                    manifest_commitment_value,
                    provenance,
                    chunk_identity=chunk.chunk_identity,
                    section_identity=chunk.section_identity,
                    ordinal=chunk.ordinal,
                    content_hash=chunk.content_hash,
                )
            ):
                return _failure(
                    BuildFailureCode.METADATA_INVALID,
                    "chunk_provenance",
                    "chunk metadata does not resolve to admitted manifest and source provenance",
                )
            unsafe = preflight_outbound_content(chunk.content)
            if unsafe is not None:
                return _failure(BuildFailureCode.INVALID_CHUNK_PLAN, "chunk_content", unsafe.detail)
            try:
                source_content = (source_root / getattr(admitted, "source_path")).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return _failure(BuildFailureCode.INVALID_CHUNK_PLAN, "source", "admitted source cannot be re-read for staging")
            if chunk.content not in source_content:
                return _failure(BuildFailureCode.INVALID_CHUNK_PLAN, "chunk_content", "chunk content does not belong to its admitted source")
            covered.add(chunk.document_version_identity)
        if covered != set(admitted_by_version):
            return _failure(BuildFailureCode.CARDINALITY_MISMATCH, "documents", "not every admitted document has staged chunk provenance")
        return None
