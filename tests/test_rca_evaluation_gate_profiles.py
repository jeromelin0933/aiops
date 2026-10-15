"""Deterministic S5 tests over the single Candidate-F evaluation ledger."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
import yaml

from knowledge_index import (
    ActivationOperationKey, BuildManifestProvenance, ChromaIndexAdapter,
    DocumentStatus, GoogleEmbeddingAdapter, KnowledgeLocalReadiness,
    KnowledgeProvenanceProjection, KnowledgeRetrievalService,
    KnowledgeReadinessFact, KnowledgeReadResult, KnowledgeReadStatus,
    KnowledgeSnapshotService, RetrievalOperationKey, RetrievalResolution,
    SqliteKnowledgeStore, load_knowledge_config,
)
from llm_generation.contracts import (
    GenerationPin, LocalReadStatus, ValidatedGenerationResult,
    VersionedIdentity,
)
from llm_generation.google_generation_adapter import GoogleGenerationAdapter
from llm_generation.service import ProviderCapability
from llm_generation.sqlite_store import CandidateDStore
from rca_evaluation.authority import (
    PM_AUTHORITY, TrustedApprovalRegistry, TrustedOracleApproval,
)
from rca_evaluation.docker_harness import (
    DockerLifecyclePhase, DockerReadinessFacts, allocate_namespace,
    cleanup_targets, evaluate_docker_harness, lifecycle_command,
    record_cleanup_outcome,
)
from rca_evaluation.gate_records import (
    UPSTREAM_ONLY_PROVENANCE, read_docker_cleanup, read_docker_namespace,
    read_gate_authorization, read_gate_execution_receipt,
    record_gate_authorization, record_gate_execution_receipt,
)
from rca_evaluation.gates import (
    AuthorizationRequest, GateExecutionStatus, GateKind, RealLlmFacts,
    RealRagBuildContext, RealRagFacts, assert_secret_free, capture_gate_evidence,
    evaluate_real_llm, evaluate_real_rag, load_docker_gate_profile,
    load_real_llm_profile, load_real_rag_profile,
)
from rca_evaluation.identity import commitment, identity
from rca_evaluation.ledger import LedgerConflict, LedgerRecord
from rca_evaluation.oracle import EvaluationPlan, OracleRevision
from rca_evaluation.sqlite_store import SqliteEvaluationStore
from rca_evaluation.real_gate_provenance import (
    ActualInvocationReceipt, DockerProbeEvidence, actual_invocation_status,
    execute_docker_invocation,
    execute_real_llm_invocation, execute_real_rag_invocation,
    read_actual_invocation_receipt, _real_rag_invocation_pins,
    _record_start, _validate_docker_terminal_binding,
    _validate_real_rag_binding,
)
from runtime_orchestration.contracts import (
    RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus,
)
from runtime_orchestration.identity import runtime_work_id
from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import (
    QueryProvider, RetrievalIndex, candidates_for,
    request as knowledge_request, stage_activate,
)
from _knowledge_snapshot_testkit import environment as knowledge_environment
from test_llm_generation_phase2 import case


ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "configs" / "rca_evaluation"
REPO_COMMIT = "4109019b61b181824bcec8a32efd31c55e0bfe91"
OTHER_RUN = identity("evaluation_run", {"test": "other"})
OTHER_EXECUTION = identity("execution", {"test": "other", "order": 1})
NOW = "2026-10-04T01:00:00Z"
LATER = "2026-10-04T01:00:01Z"

_ORACLE = OracleRevision.approved(
    oracle_set_id="PM-S1-S6",
    semantics={
        "scenarios": {
            f"S{i}": {f"O{j}": f"approved-{i}-{j}" for j in range(1, 11)}
            for i in range(1, 7)},
        "cross_scenario_rules": ["approved disambiguation"]},
    created_at=NOW, approval_authority=PM_AUTHORITY,
    approval_reference="PM-017-S5-R1", approved_at=NOW)
_PLAN = EvaluationPlan.admit(
    _ORACLE, fixture_revision="fixtures-r1", production_build="build-4109019",
    model="gate-model", prompt="prompt-v1", schema="schema-v1",
    corpus_index="index-v1", configuration=commitment({"s5": "r1"}),
    repetitions=1)
_RUN = LedgerRecord.create(
    "evaluation_run", [_PLAN.evaluation_plan_id, "s5-single-authority-run"],
    {"oracle_revision_id": _ORACLE.oracle_revision_id},
    parent_id=_PLAN.evaluation_plan_id)
RUN_ID = _RUN.record_id
_EXECUTION = LedgerRecord.create(
    "execution", [RUN_ID, "S1", 1],
    {"scenario_id": "S1", "repetition": 1,
     "replacement_for_execution_id": None}, parent_id=RUN_ID)
EXECUTION_ID = _EXECUTION.record_id
_RAG_AUTHORITIES = {}


class _ExactCandidateCAuthority:
    def __init__(self, inner, staged):
        profile = _profile(GateKind.REAL_RAG)
        values = profile.values()
        config = load_knowledge_config(ROOT / values["config_path"])
        manifest = BuildManifestProvenance(
            "spec017-test-manifest", "spec014-manifest-schema",
            "1.0", "1.0", values["manifest_commitment"],
            "spec017-test-corpus", "1.0",
        )
        build_input = replace(
            staged.build_input,
            manifest_commitment=values["manifest_commitment"],
            embedding_provider=values["embedding_provider"],
            embedding_model=values["embedding_model"],
            embedding_profile_identity=values["embedding_profile_identity"],
            embedding_dimension=config.capability.embedding_dimension,
            index_engine=values["index_engine"],
            index_schema_identity=values["index_schema_identity"],
        )
        artifact = replace(
            staged.artifact,
            provider=values["embedding_provider"],
            model=values["embedding_model"],
            embedding_profile_identity=values["embedding_profile_identity"],
            embedding_dimension=config.capability.embedding_dimension,
            index_engine=values["index_engine"],
            index_schema_identity=values["index_schema_identity"],
        )
        documents = tuple(replace(
            item,
            knowledge_type="SOP",
            guidance_authority="SOP_BACKED_ELIGIBLE",
            document_status=DocumentStatus.ACTIVE,
            approval_state="APPROVED",
            production_eligible=True,
        ) for item in staged.document_provenance)
        self.staged = replace(
            staged,
            manifest_commitment=values["manifest_commitment"],
            build_input=build_input,
            artifact=artifact,
            document_provenance=documents,
            manifest_provenance=manifest,
        )
        self.activation = inner.read_activation().value
        self.reads = []

    def read_activation(self):
        self.reads.append("activation")
        return KnowledgeReadResult(KnowledgeReadStatus.FOUND, self.activation)

    def local_readiness(self, **_):
        self.reads.append("readiness")
        return KnowledgeReadinessFact(
            KnowledgeLocalReadiness.READY,
            self.activation.active_build_identity,
            self.activation.generation,
        )

    def get_staged_build(self, build_identity):
        self.reads.append("staged")
        if build_identity != self.activation.active_build_identity:
            return KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
        return KnowledgeReadResult(KnowledgeReadStatus.FOUND, self.staged)


def _approvals():
    return TrustedApprovalRegistry((TrustedOracleApproval(
        _ORACLE.approval_reference, PM_AUTHORITY, _ORACLE.oracle_revision_id,
        _ORACLE.semantic_content_commitment, _ORACLE.approved_at,
        _ORACLE.parent_revision_id),))


def _open_store(path):
    return SqliteEvaluationStore(path, approvals=_approvals())


@pytest.fixture
def authority(tmp_path):
    path = tmp_path / "rca_evaluation" / "evaluation.db"
    store = _open_store(path)
    store.admit_oracle(_ORACLE)
    store.admit_plan(_PLAN)
    store.append(_RUN)
    store.append(_EXECUTION)
    candidate_root = tmp_path / "candidate-c-readiness"
    candidate_root.mkdir()
    candidate_c, staged, _, _, _ = knowledge_environment(candidate_root)
    _RAG_AUTHORITIES[store] = _ExactCandidateCAuthority(candidate_c, staged)
    yield store, path
    try:
        store.close()
        candidate_c.close()
        _RAG_AUTHORITIES.pop(store, None)
    except Exception:
        pass


def _profile(gate):
    loader, name = {
        GateKind.REAL_LLM: (load_real_llm_profile, "real_llm.yaml"),
        GateKind.REAL_RAG: (load_real_rag_profile, "real_rag.yaml"),
        GateKind.DOCKER_RCA_E2E: (load_docker_gate_profile, "docker_e2e.yaml"),
    }[gate]
    return loader(PROFILES / name)


def _issue(store, profile, gate, *, run_id=RUN_ID):
    return record_gate_authorization(
        store, evaluation_run_id=run_id, gate=gate.value,
        profile_identity=profile.profile_identity,
        profile_commitment=profile.profile_commitment,
        authorization_source="SPEC-017-F2-4",
        authorizing_actor="SPEC Lead", issued_at=NOW)


def _request(record=None, *, credential=True):
    return AuthorizationRequest(
        record.authorization_id if record is not None else None, credential)


def _rag_facts(authorization, **overrides):
    values = dict(
        authorization=authorization, authority_state="ACTIVE",
        active_build_identity="kbld_" + "1" * 64,
        chroma_index_available=True, public_retrieval_available=True,
        actual_manifest_commitment="kmf_1f98f62afda12b1c49dda11c67be0249bec9bf3c788e00f22fbe61f50cfb41ee",
        actual_embedding_profile_identity="spec014-google-document-query-v1",
        actual_index_schema_identity="spec014-chroma-cosine-v1",
        chroma_path="var/rca_evaluation/real_rag/run-1/chroma",
        real_embedding_and_index=True)
    values.update(overrides)
    return RealRagFacts(**values)


def _ready_llm(store, *, run_id=RUN_ID, execution_id=EXECUTION_ID,
               profile=None):
    profile = profile or _profile(GateKind.REAL_LLM)
    authorization = _issue(
        store, profile, GateKind.REAL_LLM, run_id=run_id)
    return evaluate_real_llm(
        profile, RealLlmFacts(_request(authorization), True),
        repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=run_id, execution_id=execution_id)


def _ready_rag(store, *, overrides=None, profile=None, root=ROOT):
    profile = profile or _profile(GateKind.REAL_RAG)
    authorization = _issue(store, profile, GateKind.REAL_RAG)
    candidate_c = _RAG_AUTHORITIES[store]
    merged = {
        "candidate_c_authority": candidate_c,
        "active_build_identity": candidate_c.activation.active_build_identity,
    }
    merged.update(overrides or {})
    return evaluate_real_rag(
        profile, _rag_facts(_request(authorization), **merged),
        repository_root=root, authority=store.reader(),
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID)


def _docker_context(store, *, nonce="nonce-1"):
    profile = _profile(GateKind.DOCKER_RCA_E2E)
    authorization = _issue(store, profile, GateKind.DOCKER_RCA_E2E)
    namespace = allocate_namespace(
        store, profile, evaluation_run_id=RUN_ID,
        execution_id=EXECUTION_ID, issuance_nonce=nonce, issued_at=NOW)
    facts = DockerReadinessFacts(
        _request(authorization, credential=False), True, True, True,
        namespace.namespace_id)
    return profile, namespace, facts


def _valid_docker_probe_snapshot():
    states = {
        "DOCKER-A-CREATE": {
            "processed": "CREATED_INCIDENT", "incident_id": "INC-1",
            "pending": False},
        "DOCKER-B-ATTACH": {
            "processed": "ATTACHED_TO_INCIDENT", "incident_id": "INC-1",
            "pending": False},
        "DOCKER-C-PENDING": {
            "processed": "CREATED_INCIDENT", "incident_id": "INC-2",
            "pending": False},
        "DOCKER-D-SHADOW": {
            "processed": "SHADOWED", "incident_id": None,
            "pending": False},
    }
    return {
        "states": states,
        "incidents": [
            {"incident_id": "INC-1", "status": "ASSIGNED",
             "assignee": "engineer-a", "events": ["DOCKER-A-CREATE"],
             "workflow_audits": 1},
            {"incident_id": "INC-2", "status": "ASSIGNED",
             "assignee": "engineer-b", "events": ["DOCKER-C-PENDING"],
             "workflow_audits": 1},
        ],
        "shadow_exists": True,
        "shadow_has_incident_owner": False,
        "work": [
            {"work_id": "WORK-1", "status": "COMPLETED",
             "attempt_count": 0},
            {"work_id": "WORK-2", "status": "COMPLETED",
             "attempt_count": 0},
        ],
        "work_corruptions": 0,
        "rca": {
            "a_candidates": 0, "b_readiness": "READY",
            "b_integrity": "VALID", "c_readiness": "NOT_INITIALIZED",
            "d_readiness": "FOUND", "d_recovery": "FOUND",
            "d2_roots": 0, "d2_corruptions": 0,
        },
    }


def _iso_after(value, seconds):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z")


def _docker_semantic_proof(store, *, disposition="COMPLETED"):
    profile, namespace, facts = _docker_context(store)
    ready = evaluate_docker_harness(
        profile, facts, repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID)
    values = profile.values()
    pins = {
        "compose_commitment": "sha256:" + "1" * 64,
        "repository_commit": REPO_COMMIT,
        "project_identity": namespace.project_name,
        "volume_identities": list(namespace.volume_identities),
        "workspace_identity": namespace.workspace_root,
        "runtime_service": values["runtime_service"],
        "runtime_entrypoint": values["runtime_entrypoint"],
        "runtime_config": values["runtime_config"],
        "namespace_identity": namespace.namespace_id,
    }
    start = _record_start(store, ready, pins)
    probe_at = _iso_after(start.started_at, 1)
    cleanup_at = _iso_after(start.started_at, 2)
    terminal_at = _iso_after(start.started_at, 3)
    observation = _valid_docker_probe_snapshot()
    probe = DockerProbeEvidence(
        RUN_ID, EXECUTION_ID, start.start_id, start.invocation_id,
        namespace.namespace_id, namespace.project_name,
        values["runtime_service"], "container-1", "sha256:" + "a" * 64,
        namespace.volume_identities,
        "sha256:" + "2" * 64, probe_at, observation,
        commitment(observation),
    )
    cleanup = record_cleanup_outcome(
        store, issuance_id=namespace.namespace_id,
        completed_at=cleanup_at, disposition=disposition,
        invocation_start_id=start.start_id,
        invocation_id=start.invocation_id,
        invocation_started_at=start.started_at,
    )
    return profile, namespace, ready, start, probe, cleanup, terminal_at


def _foreign_docker_cleanup(store, profile, ready):
    execution = _append_execution(store)
    namespace = allocate_namespace(
        store, profile, evaluation_run_id=RUN_ID,
        execution_id=execution.record_id, issuance_nonce="foreign-cleanup",
        issued_at=NOW)
    foreign_ready = replace(ready, execution_id=execution.record_id)
    values = profile.values()
    start = _record_start(store, foreign_ready, {
        "compose_commitment": "sha256:" + "1" * 64,
        "repository_commit": REPO_COMMIT,
        "project_identity": namespace.project_name,
        "volume_identities": list(namespace.volume_identities),
        "workspace_identity": namespace.workspace_root,
        "runtime_service": values["runtime_service"],
        "runtime_entrypoint": values["runtime_entrypoint"],
        "runtime_config": values["runtime_config"],
        "namespace_identity": namespace.namespace_id,
    })
    return record_cleanup_outcome(
        store, issuance_id=namespace.namespace_id,
        completed_at=_iso_after(start.started_at, 2),
        disposition="COMPLETED", invocation_start_id=start.start_id,
        invocation_id=start.invocation_id,
        invocation_started_at=start.started_at)


def _receipt(store, ready, *, run_id=RUN_ID, execution_id=EXECUTION_ID,
             provenance=None, upstream=None):
    upstream = upstream or {
        "reference": "candidate-d-result-1",
        "commitment": "sha256:" + "1" * 64,
        "typed_outcome": "VALIDATED_GENERATION_RESULT",
        "provenance": UPSTREAM_ONLY_PROVENANCE[ready.gate.value],
    }
    return record_gate_execution_receipt(
        store, evaluation_run_id=run_id, execution_id=execution_id,
        gate=ready.gate.value, profile_identity=ready.profile_identity,
        profile_commitment=ready.profile_commitment,
        harness_invocation_identity="r1-observation-only",
        external_started_at=NOW, external_ended_at=LATER,
        upstream_result_reference=upstream["reference"],
        upstream_result_commitment=upstream["commitment"],
        authoritative_typed_outcome=upstream["typed_outcome"],
        sanitized_provenance=(provenance or
                              upstream["provenance"]))


def _forged_actual_receipt(store, ready, *, pins, provenance,
                           provenance_kind, invocation_id="invocation-1"):
    start_payload = {
        "evaluation_run_id": RUN_ID, "execution_id": EXECUTION_ID,
        "gate": ready.gate.value, "profile_identity": ready.profile_identity,
        "profile_commitment": ready.profile_commitment,
        "invocation_id": invocation_id, "started_at": NOW,
        "invocation_pins": pins,
    }
    start = store.append(LedgerRecord.create(
        "gate_invocation_start",
        [EXECUTION_ID, ready.gate.value, ready.profile_commitment, invocation_id],
        start_payload, EXECUTION_ID))
    terminal_payload = {
        "start_id": start.record_id, **start_payload, "ended_at": LATER,
        "status": GateExecutionStatus.EXECUTED_PASS.value,
        "provenance_kind": provenance_kind,
        "upstream_result_reference": "upstream-result",
        "upstream_result_commitment": "sha256:" + "6" * 64,
        "authoritative_typed_outcome": "SUCCESS",
        "sanitized_provenance": provenance,
    }
    return store.append(LedgerRecord.create(
        "gate_invocation_receipt", [start.record_id, "terminal"],
        terminal_payload, start.record_id))


def _internal_invalid_llm_start(store, ready, *, invocation_id="invocation-1"):
    profile = _profile(GateKind.REAL_LLM)
    pins = {
        "provider": profile.provider, "model": profile.model,
        "credential_profile_identity": profile.generation_profile_identity,
        "prompt_identity": profile.prompt_identity, "prompt_version": "1",
        "schema_identity": profile.result_schema_identity, "schema_version": "2",
        "configuration_identity": profile.configuration_identity,
        "configuration_version": "2.0",
    }
    return store._record_actual_execution_start(
        evaluation_run_id=ready.evaluation_run_id,
        execution_id=ready.execution_id,
        gate=ready.gate.value, profile_identity=ready.profile_identity,
        profile_commitment=ready.profile_commitment,
        invocation_id=invocation_id, started_at=NOW, invocation_pins=pins,
    )


def _append_execution(store, *, run_id=RUN_ID, scenario_id="S2"):
    return store.append(LedgerRecord.create(
        "execution", [run_id, scenario_id, 1],
        {"scenario_id": scenario_id, "repetition": 1,
         "replacement_for_execution_id": None},
        parent_id=run_id,
    ))


def _append_run_and_execution(store, *, marker="invocation-binding-run"):
    run = store.append(LedgerRecord.create(
        "evaluation_run", [_PLAN.evaluation_plan_id, marker],
        {"oracle_revision_id": _ORACLE.oracle_revision_id},
        parent_id=_PLAN.evaluation_plan_id,
    ))
    return run, _append_execution(
        store, run_id=run.record_id, scenario_id="S1")


def _internal_invalid_llm_terminal(store, start, **overrides):
    values = dict(
        start_id=start.record_id,
        status=GateExecutionStatus.INVALID_EXECUTION.value,
        ended_at=LATER,
        provenance_kind="LIVE_GEMINI_INVOCATION",
        upstream_result_reference="no-provider-result",
        upstream_result_commitment="sha256:" + "9" * 64,
        authoritative_typed_outcome="NO_ACTUAL_GENERATION_INVOCATION",
        sanitized_provenance={
            "adapter": "llm_generation.google_generation_adapter.GoogleGenerationAdapter",
            "provider": _profile(GateKind.REAL_LLM).provider,
            "model": _profile(GateKind.REAL_LLM).model,
            "physical_invocations": 0,
        },
    )
    values.update(overrides)
    return store._record_actual_execution_terminal(**values)


def _candidate_d_public_result(tmp_path, case):
    store = CandidateDStore(tmp_path / "candidate-d.sqlite")
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    assert store.commit_result(result).value == result
    assert store.result(result.validated_result_id).value == result
    return {
        "reference": result.validated_result_id,
        "commitment": "sha256:" + result.semantic_commitment,
        "typed_outcome": "VALIDATED_GENERATION_RESULT",
        "provenance": UPSTREAM_ONLY_PROVENANCE[GateKind.REAL_LLM.value],
    }


def _candidate_c_public_result(tmp_path, resolution):
    score = 0.95 if resolution is RetrievalResolution.MATCH else 0.1
    authority_root = tmp_path / f"candidate-c-{resolution.value.lower()}"
    authority_root.mkdir(parents=True)
    store, _, _, _, service = knowledge_environment(
        authority_root,
        candidate_score=score,
    )
    outcome = service.resolve(knowledge_request(), limits())
    assert outcome.resolution is resolution
    assert outcome.snapshot is not None
    persisted = store.get_snapshot(outcome.snapshot.snapshot_key)
    assert persisted.status is KnowledgeReadStatus.FOUND
    assert persisted.value == outcome.snapshot
    store.close()
    return {
        "reference": outcome.snapshot.snapshot_key.value,
        "commitment": "sha256:" + outcome.snapshot.snapshot_commitment,
        "typed_outcome": resolution.value,
        "provenance": UPSTREAM_ONLY_PROVENANCE[GateKind.REAL_RAG.value],
    }


def _rag_binding_case(tmp_path, resolution=RetrievalResolution.MATCH):
    root = tmp_path / f"rag-binding-{resolution.value.lower()}"
    root.mkdir()
    score = 0.95 if resolution is RetrievalResolution.MATCH else 0.1
    store, staged, _, _, service = knowledge_environment(
        root, candidate_score=score)
    request = knowledge_request()
    outcome = service.resolve(request, limits())
    operation = service.read_operation(request.operation_key)
    assert operation.status is KnowledgeReadStatus.FOUND
    frozen = operation.value.frozen
    snapshot = outcome.snapshot
    assert snapshot is not None
    public = _rag_projection(snapshot, staged, frozen)
    expected = RealRagBuildContext(
        public.frozen_build_identity, public.activation_generation,
        public.activation_operation_identity.value,
        public.validation_commitment, public.manifest_identity,
        public.manifest_commitment, public.corpus_identity,
        public.corpus_version, public.embedding_provider,
        public.embedding_model, public.embedding_profile_identity,
        public.embedding_dimension, public.index_engine,
        public.index_schema_identity, public.artifact_commitment,
        public.lineage_commitment, public.staged_commitment,
    )
    store.close()
    return expected, frozen, public, request


def _rag_projection(snapshot, staged, frozen):
    return KnowledgeProvenanceProjection(
        snapshot.snapshot_key, snapshot.snapshot_commitment,
        snapshot.schema_version, snapshot.resolution, snapshot.source_status,
        snapshot.operation_key, snapshot.activation_generation,
        snapshot.activation_operation_key, snapshot.frozen_build_identity,
        snapshot.lineage_commitment, snapshot.staged_commitment,
        snapshot.validation_commitment, snapshot.artifact_commitment,
        "manifest-binding", "manifest-schema-binding", "1.0", "1.0",
        snapshot.manifest_commitment, "corpus-binding", "1.0",
        snapshot.retrieval_profile_identity,
        snapshot.retrieval_profile_version,
        snapshot.applicability_policy_identity,
        snapshot.applicability_policy_version,
        staged.build_input.embedding_provider,
        staged.build_input.embedding_model,
        staged.build_input.embedding_profile_identity,
        staged.build_input.embedding_dimension,
        staged.build_input.index_engine,
        staged.build_input.index_schema_identity,
        snapshot.query_commitment, frozen.request.query.filters, (),
        snapshot.failures,
    )


def _spec011_host_runtime_result(tmp_path):
    store = SqliteRuntimeWorkStore(tmp_path / "spec011-host.sqlite")
    observed = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
    work_id = runtime_work_id(RuntimeWorkKind.AUTO_ASSIGN, "EVT-S5", "INC-S5")
    created = store.create(RuntimeWorkRecord(
        work_id=work_id, work_kind=RuntimeWorkKind.AUTO_ASSIGN,
        event_id="EVT-S5", incident_id="INC-S5", stage="POST_CORRELATION",
        next_action="AUTO_ASSIGN", workflow_operation_id="WFO-S5",
        attempt_count=0, retry_limit=1, next_retry_at=None,
        status=RuntimeWorkStatus.OUTSTANDING, created_at=observed,
        updated_at=observed, observed_at=observed,
    ))
    terminal = store.complete(
        created.work_id, observed_at=observed, expected_revision=created.revision)
    assert terminal.status is RuntimeWorkStatus.COMPLETED
    assert store.get(work_id) == terminal
    store.close()
    return {
        "reference": terminal.work_id,
        "commitment": commitment({
            "runtime_work_id": terminal.work_id,
            "status": terminal.status.value,
            "revision": terminal.revision,
            "observed_at": terminal.observed_at.isoformat(),
        }),
        "typed_outcome": terminal.status.value,
        "provenance": UPSTREAM_ONLY_PROVENANCE[GateKind.DOCKER_RCA_E2E.value],
    }


def _ready_gate(store, gate):
    if gate is GateKind.REAL_LLM:
        return _ready_llm(store)
    if gate is GateKind.REAL_RAG:
        return _ready_rag(store)
    profile, _, facts = _docker_context(store)
    return evaluate_docker_harness(
        profile, facts, repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID)


def test_default_off_and_missing_authorization_remains_not_authorized(authority):
    store, _ = authority
    profile = _profile(GateKind.REAL_LLM)
    result = evaluate_real_llm(
        profile, RealLlmFacts(_request(), True), repository_root=ROOT,
        authority=store.reader(), evaluation_run_id=RUN_ID,
        execution_id=EXECUTION_ID)
    assert not profile.default_enabled
    assert result.status is GateExecutionStatus.NOT_AUTHORIZED


def test_authorization_is_single_ledger_record_and_reloads(authority):
    store, path = authority
    profile = _profile(GateKind.REAL_LLM)
    authorization = _issue(store, profile, GateKind.REAL_LLM)
    assert read_gate_authorization(
        store.reader(), authorization.authorization_id) == authorization
    store.close()
    restarted = _open_store(path)
    try:
        assert read_gate_authorization(
            restarted.reader(), authorization.authorization_id) == authorization
    finally:
        restarted.close()


def test_authorization_cross_gate_profile_run_and_commitment_rejected(authority):
    store, _ = authority
    llm = _profile(GateKind.REAL_LLM)
    rag = _profile(GateKind.REAL_RAG)
    authorization = _issue(store, llm, GateKind.REAL_LLM)
    request = _request(authorization)
    assert evaluate_real_rag(
        rag, _rag_facts(request), repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID,
        execution_id=EXECUTION_ID).status is GateExecutionStatus.NOT_AUTHORIZED
    altered = replace(llm, profile_commitment="sha256:" + "0" * 64)
    assert evaluate_real_llm(
        altered, RealLlmFacts(request, True), repository_root=ROOT,
        authority=store.reader(), evaluation_run_id=RUN_ID,
        execution_id=EXECUTION_ID).status is GateExecutionStatus.NOT_AUTHORIZED
    assert evaluate_real_llm(
        llm, RealLlmFacts(request, True), repository_root=ROOT,
        authority=store.reader(), evaluation_run_id=OTHER_RUN,
        execution_id=EXECUTION_ID).status is GateExecutionStatus.NOT_AUTHORIZED


def test_authorization_replay_idempotent_and_contradiction_fails_closed(authority):
    store, _ = authority
    profile = _profile(GateKind.REAL_LLM)
    first = _issue(store, profile, GateKind.REAL_LLM)
    assert _issue(store, profile, GateKind.REAL_LLM) == first
    with pytest.raises(LedgerConflict):
        record_gate_authorization(
            store, evaluation_run_id=RUN_ID, gate=GateKind.REAL_LLM.value,
            profile_identity=profile.profile_identity,
            profile_commitment=profile.profile_commitment,
            authorization_source="different-source",
            authorizing_actor="different actor", issued_at=LATER)


def test_candidate_d_store_result_alone_cannot_form_real_llm_pass(
    authority, tmp_path, case,
):
    store, _ = authority
    ready = _ready_llm(store)
    upstream = _candidate_d_public_result(tmp_path, case)
    receipt = _receipt(store, ready, upstream=upstream)
    assert read_gate_execution_receipt(store.reader(), receipt.receipt_id) == receipt
    evidence = capture_gate_evidence(
        ready, repository_commit=REPO_COMMIT,
        environment_identity="pytest", references={"bounded": "reference"},
        execution_receipt_id=receipt.receipt_id, authority=store.reader())
    assert evidence.status is GateExecutionStatus.READY_NOT_EXECUTED
    payload = json.loads(evidence.canonical_json)
    assert payload["execution_receipt"]["upstream_result_reference"] == \
        upstream["reference"]


@pytest.mark.parametrize(
    "resolution", (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH),
)
def test_candidate_c_store_result_alone_cannot_form_real_rag_pass(
    authority, tmp_path, resolution,
):
    store, _ = authority
    ready = _ready_rag(store)
    upstream = _candidate_c_public_result(tmp_path, resolution)
    receipt = _receipt(store, ready, upstream=upstream)
    evidence = capture_gate_evidence(
        ready, repository_commit=REPO_COMMIT,
        environment_identity="pytest", references={"bounded": "reference"},
        execution_receipt_id=receipt.receipt_id, authority=store.reader())
    assert evidence.status is GateExecutionStatus.READY_NOT_EXECUTED
    assert json.loads(evidence.canonical_json)["execution_receipt"][
        "authoritative_typed_outcome"
    ] == resolution.value


def test_spec011_host_result_alone_cannot_form_docker_pass(authority, tmp_path):
    store, _ = authority
    ready = _ready_gate(store, GateKind.DOCKER_RCA_E2E)
    upstream = _spec011_host_runtime_result(tmp_path)
    receipt = _receipt(store, ready, upstream=upstream)
    evidence = capture_gate_evidence(
        ready, repository_commit=REPO_COMMIT,
        environment_identity="pytest", references={"bounded": "reference"},
        execution_receipt_id=receipt.receipt_id, authority=store.reader())
    assert evidence.status is GateExecutionStatus.READY_NOT_EXECUTED
    assert json.loads(evidence.canonical_json)["execution_receipt"][
        "sanitized_provenance"
    ] == "SPEC011_PUBLIC_RUNTIME"


@pytest.mark.parametrize(("source", "target_gate"), (
    ("candidate-d", GateKind.REAL_RAG),
    ("candidate-d", GateKind.DOCKER_RCA_E2E),
    ("candidate-c", GateKind.REAL_LLM),
    ("candidate-c", GateKind.DOCKER_RCA_E2E),
    ("spec011-host", GateKind.REAL_LLM),
    ("spec011-host", GateKind.REAL_RAG),
))
def test_upstream_public_result_cannot_cross_gate(
    authority, tmp_path, case, source, target_gate,
):
    store, _ = authority
    upstream = {
        "candidate-d": lambda: _candidate_d_public_result(tmp_path, case),
        "candidate-c": lambda: _candidate_c_public_result(
            tmp_path, RetrievalResolution.MATCH),
        "spec011-host": lambda: _spec011_host_runtime_result(tmp_path),
    }[source]()
    ready = _ready_gate(store, target_gate)
    with pytest.raises(ValueError, match="upstream public provenance only"):
        _receipt(store, ready, upstream=upstream)


@pytest.mark.parametrize(("run_id", "execution_id"), (
    (OTHER_RUN, EXECUTION_ID), (RUN_ID, OTHER_EXECUTION),
))
def test_receipt_wrong_run_or_execution_rejected(authority, run_id, execution_id):
    store, _ = authority
    ready = _ready_llm(store)
    with pytest.raises(ValueError):
        _receipt(store, ready, run_id=run_id, execution_id=execution_id)


def test_receipt_wrong_profile_rejected_by_evidence(authority):
    store, _ = authority
    ready = _ready_llm(store)
    wrong = replace(ready, profile_commitment="sha256:" + "0" * 64)
    receipt = _receipt(store, ready)
    with pytest.raises(ValueError, match="exact run/execution/gate/profile"):
        capture_gate_evidence(
            wrong, repository_commit=REPO_COMMIT,
            environment_identity="pytest", references={"bounded": "reference"},
            execution_receipt_id=receipt.receipt_id, authority=store.reader())


def test_real_or_fake_provenance_cannot_be_minted_in_r1(authority):
    store, _ = authority
    ready = _ready_llm(store)
    for provenance in ("REAL_PROVIDER", "FAKE_PROVIDER", "caller-selected"):
        with pytest.raises(ValueError, match="upstream public provenance only"):
            _receipt(store, ready, provenance=provenance)


def test_ready_not_executed_cannot_become_pass_without_receipt(authority):
    store, _ = authority
    ready = _ready_llm(store)
    with pytest.raises(ValueError, match="absent"):
        capture_gate_evidence(
            ready, repository_commit=REPO_COMMIT,
            environment_identity="pytest", references={"bounded": "reference"},
            execution_receipt_id=identity(
                "gate_execution_receipt", ["missing"]), authority=store.reader())
    evidence = capture_gate_evidence(
        ready, repository_commit=REPO_COMMIT,
        environment_identity="pytest", references={"bounded": "reference"})
    assert evidence.status is GateExecutionStatus.READY_NOT_EXECUTED


def test_upstream_only_receipt_cannot_be_relabelled_actual(authority):
    store, _ = authority
    ready = _ready_llm(store)
    receipt = _receipt(store, ready)
    with pytest.raises(ValueError, match="gate_invocation_receipt"):
        capture_gate_evidence(
            ready, repository_commit=REPO_COMMIT,
            environment_identity="pytest", references={"bounded": "reference"},
            actual_invocation_receipt_id=receipt.receipt_id,
            authority=store.reader())


def test_manual_actual_receipt_value_cannot_mint_real_pass(authority):
    store, _ = authority
    ready = _ready_llm(store)
    manual = ActualInvocationReceipt(
        "eval:gate_invocation_receipt:" + "1" * 64,
        "sha256:" + "2" * 64,
        "eval:gate_invocation_start:" + "3" * 64,
        RUN_ID, EXECUTION_ID, GateKind.REAL_LLM.value,
        ready.profile_identity, ready.profile_commitment, "caller-invocation",
        NOW, LATER, GateExecutionStatus.EXECUTED_PASS,
        "LIVE_GEMINI_INVOCATION", {}, "candidate-d-result",
        "sha256:" + "4" * 64, "VALIDATED_GENERATION_RESULT", {},
    )
    assert read_actual_invocation_receipt(
        store.reader(), manual.receipt_id) is None
    with pytest.raises(ValueError, match="absent"):
        capture_gate_evidence(
            ready, repository_commit=REPO_COMMIT,
            environment_identity="pytest", references={"bounded": "reference"},
            actual_invocation_receipt_id=manual.receipt_id,
            authority=store.reader())


def test_real_llm_harness_rejects_non_google_or_store_only_service(authority):
    store, _ = authority
    ready = _ready_llm(store)
    with pytest.raises(ValueError, match="existing Google generation adapter"):
        execute_real_llm_invocation(
            store, ready, _profile(GateKind.REAL_LLM),
            SimpleNamespace(provider=object()), None, None, None, None)


def test_real_llm_harness_rejects_wrong_profile_before_invocation(authority):
    store, _ = authority
    ready = _ready_llm(store)
    wrong = replace(
        _profile(GateKind.REAL_LLM),
        profile_commitment="sha256:" + "0" * 64,
    )
    with pytest.raises(ValueError, match="exact authorized READY gate"):
        execute_real_llm_invocation(
            store, ready, wrong, SimpleNamespace(provider=object()),
            None, None, None, None)


@pytest.mark.parametrize("field", ("provider", "model", "prompt_identity", "schema_identity"))
def test_real_llm_actual_receipt_wrong_pin_rejected(authority, field):
    store, _ = authority
    ready = _ready_llm(store)
    profile = _profile(GateKind.REAL_LLM)
    values = {
        "provider": profile.provider, "model": profile.model,
        "profile": VersionedIdentity(profile.generation_profile_identity, "1"),
        "prompt": VersionedIdentity(profile.prompt_identity, "1"),
        "result_schema": VersionedIdentity(profile.result_schema_identity, "2"),
        "configuration": VersionedIdentity(profile.configuration_identity, "2.0"),
    }
    if field in {"provider", "model"}:
        values[field] = "wrong-pin"
    elif field == "prompt_identity":
        values["prompt"] = VersionedIdentity("wrong-pin", "1")
    else:
        values["result_schema"] = VersionedIdentity("wrong-pin", "2")
    pin = GenerationPin(**values)
    adapter = GoogleGenerationAdapter(ProviderCapability(
        profile.provider, profile.model, profile.generation_profile_identity,
        profile.result_schema_identity, "2", True))
    with pytest.raises(ValueError, match="does not match the admitted gate profile"):
        execute_real_llm_invocation(
            store, ready, profile, SimpleNamespace(provider=adapter),
            SimpleNamespace(pin=pin), SimpleNamespace(pin=pin), None, None)


def test_real_rag_harness_rejects_fake_adapters(authority):
    store, _ = authority
    ready = _ready_rag(store)
    with pytest.raises(ValueError, match="existing Google/Chroma adapters"):
        execute_real_rag_invocation(
            store, ready, _profile(GateKind.REAL_RAG),
            knowledge_store=object(), provider=object(), index=object(),
            request=object(), limits=object(), repository_root=ROOT)


def test_real_rag_readiness_fresh_reads_and_start_pins_exact_build(authority):
    store, _ = authority
    ready = _ready_rag(store)
    candidate_c = _RAG_AUTHORITIES[store]

    assert ready.status is GateExecutionStatus.READY_NOT_EXECUTED
    assert ready.real_rag_build_context is not None
    assert candidate_c.reads[-3:] == ["activation", "readiness", "staged"]
    context = ready.real_rag_build_context.pins()
    profile = _profile(GateKind.REAL_RAG)
    config = load_knowledge_config(ROOT / "configs" / "knowledge_index.yaml")
    request = replace(
        knowledge_request(), retrieval_profile=config.retrieval_profile,
        applicability_policy=config.applicability_policy,
        profile_reference=config.capability.profile_reference,
        capability_identity=config.capability.capability_identity,
    )
    pins = _real_rag_invocation_pins(
        profile, request,
        ROOT / "var" / "rca_evaluation" / "real_rag" / "run-1" / "chroma",
        ready.real_rag_build_context,
    )
    start = _record_start(store, ready, pins)
    assert start.invocation_pins["expected_build_context"] == context
    assert start.invocation_pins[
        "retrieval_operation_identity"] == request.operation_key.value


@pytest.mark.parametrize(
    "resolution", (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH),
)
def test_real_rag_exact_frozen_build_binding_is_semantically_valid(
    tmp_path, resolution,
):
    expected, frozen, public, request = _rag_binding_case(
        tmp_path, resolution)

    assert _validate_real_rag_binding(
        expected, frozen, public, request) == expected.pins()


def test_real_rag_readiness_b1_frozen_b2_rejected(tmp_path):
    expected, frozen, public, request = _rag_binding_case(tmp_path)
    frozen_b2 = replace(
        frozen, frozen_build_identity="kbld_" + "2" * 64)

    with pytest.raises(ValueError, match="readiness build"):
        _validate_real_rag_binding(expected, frozen_b2, public, request)


def test_real_rag_wrong_but_valid_build_b2_rejected(tmp_path):
    root = tmp_path / "wrong-but-valid-build"
    root.mkdir()
    store = SqliteKnowledgeStore(root / "knowledge.sqlite3")
    staged_b1, index = stage_activate(
        store, root / "sources-b1", name="b1")
    activation_b1 = store.read_activation().value
    staged_b2, _ = stage_activate(
        store, root / "sources-b2", name="b2",
        expected_generation=1, index=index)
    assert staged_b1.build_identity != staged_b2.build_identity
    assert store.read_activation().value.active_build_identity == \
        staged_b2.build_identity
    request = knowledge_request("retrieve-valid-b2")
    retrieval = KnowledgeRetrievalService(
        store, QueryProvider(), RetrievalIndex(
            index.artifacts, candidates_for(staged_b2)))
    service = KnowledgeSnapshotService(store, retrieval)
    outcome = service.resolve(request, limits())
    operation = service.read_operation(request.operation_key)
    frozen_b2 = operation.value.frozen
    public_b2 = _rag_projection(outcome.snapshot, staged_b2, frozen_b2)
    expected_b1 = RealRagBuildContext(
        staged_b1.build_identity, activation_b1.generation,
        activation_b1.operation_key.value,
        activation_b1.validated_build_commitment,
        public_b2.manifest_identity, staged_b1.manifest_commitment,
        public_b2.corpus_identity, public_b2.corpus_version,
        staged_b1.build_input.embedding_provider,
        staged_b1.build_input.embedding_model,
        staged_b1.build_input.embedding_profile_identity,
        staged_b1.build_input.embedding_dimension,
        staged_b1.build_input.index_engine,
        staged_b1.build_input.index_schema_identity,
        staged_b1.artifact.artifact_commitment,
        staged_b1.lineage_commitment, staged_b1.staged_commitment,
    )
    try:
        with pytest.raises(ValueError, match="readiness build"):
            _validate_real_rag_binding(
                expected_b1, frozen_b2, public_b2, request)
    finally:
        store.close()


def test_real_rag_activation_generation_mismatch_rejected(tmp_path):
    expected, frozen, public, request = _rag_binding_case(tmp_path)
    generation = expected.activation_generation + 1

    with pytest.raises(ValueError, match="readiness build"):
        _validate_real_rag_binding(
            expected, replace(frozen, activation_generation=generation),
            replace(public, activation_generation=generation), request)


def test_real_rag_activation_operation_mismatch_rejected(tmp_path):
    expected, frozen, public, request = _rag_binding_case(tmp_path)
    operation = ActivationOperationKey("activate-other-valid-build")

    with pytest.raises(ValueError, match="readiness build"):
        _validate_real_rag_binding(
            expected, replace(frozen, activation_operation_key=operation),
            replace(public, activation_operation_identity=operation), request)


def test_real_rag_retrieval_operation_identity_mismatch_rejected(tmp_path):
    expected, frozen, public, request = _rag_binding_case(tmp_path)

    with pytest.raises(ValueError, match="retrieval identity"):
        _validate_real_rag_binding(
            expected, frozen,
            replace(public, retrieval_operation_identity=RetrievalOperationKey(
                "retrieve-other-valid-operation")),
            request)


@pytest.mark.parametrize("field", ("manifest_identity", "manifest_commitment",
                                   "corpus_identity", "corpus_version"))
def test_real_rag_manifest_and_corpus_mismatch_rejected(tmp_path, field):
    expected, frozen, public, request = _rag_binding_case(tmp_path)
    value = ("kmf_" + "3" * 64
             if field == "manifest_commitment" else "different-valid-value")

    with pytest.raises(ValueError, match="public provenance"):
        _validate_real_rag_binding(
            expected, frozen, replace(public, **{field: value}), request)


@pytest.mark.parametrize("field", (
    "provider", "model", "embedding_profile_identity", "embedding_dimension",
))
def test_real_rag_embedding_pin_mismatch_rejected(tmp_path, field):
    expected, frozen, public, request = _rag_binding_case(tmp_path)
    value = (expected.embedding_dimension + 1
             if field == "embedding_dimension" else "different-valid-value")
    profile = replace(
        frozen.request.retrieval_profile, **{field: value})
    changed_frozen = replace(
        frozen, request=replace(frozen.request, retrieval_profile=profile))
    public_field = "embedding_" + field if field in {"provider", "model"} else field

    with pytest.raises(ValueError, match="readiness build"):
        _validate_real_rag_binding(
            expected, changed_frozen,
            replace(public, **{public_field: value}), request)


@pytest.mark.parametrize("field", (
    "validation_commitment", "staged_commitment", "artifact_commitment",
    "lineage_commitment",
))
def test_real_rag_index_and_build_commitment_mismatch_rejected(
    tmp_path, field,
):
    expected, frozen, public, request = _rag_binding_case(tmp_path)
    value = "4" * 64

    with pytest.raises(ValueError, match="readiness build"):
        _validate_real_rag_binding(
            expected, replace(frozen, **{field: value}),
            replace(public, **{field: value}), request)


def test_real_rag_harness_rejects_fake_clients_behind_real_adapter_types(authority):
    store, _ = authority
    ready = _ready_rag(store)
    provider = object.__new__(GoogleEmbeddingAdapter)
    object.__setattr__(provider, "_client", object())
    index = object.__new__(ChromaIndexAdapter)
    object.__setattr__(index, "_client", object())
    object.__setattr__(index, "storage_path",
                      ROOT / "var" / "rca_evaluation" / "real_rag" / "run-1")
    config = load_knowledge_config(ROOT / "configs" / "knowledge_index.yaml")
    request = replace(
        knowledge_request(), retrieval_profile=config.retrieval_profile,
        applicability_policy=config.applicability_policy,
        profile_reference=config.capability.profile_reference,
        capability_identity=config.capability.capability_identity,
    )
    with pytest.raises(ValueError, match="injected/fake RAG clients"):
        execute_real_rag_invocation(
            store, ready, _profile(GateKind.REAL_RAG),
            knowledge_store=object(), provider=provider, index=index,
            request=request, limits=object(), repository_root=ROOT)


def test_real_rag_caller_cannot_mint_stale_or_unbound_result_receipt(authority):
    store, _ = authority
    ready = _ready_rag(store)
    profile = _profile(GateKind.REAL_RAG)
    values = profile.values()
    pins = {
        "manifest_commitment": values["manifest_commitment"],
        "release_reference": values["release_reference"],
        "embedding_provider": values["embedding_provider"],
        "embedding_model": values["embedding_model"],
        "embedding_profile_identity": values["embedding_profile_identity"],
        "retrieval_profile_identity": values["retrieval_profile_identity"],
        "index_engine": values["index_engine"],
        "index_schema_identity": values["index_schema_identity"],
        "chroma_reference": "var/rca_evaluation/real_rag/run-1/chroma",
    }
    with pytest.raises(ValueError, match="protected"):
        _forged_actual_receipt(
            store, ready, pins=pins,
            provenance={
                "embedding_adapter": "knowledge_index.google_embedding_adapter.GoogleEmbeddingAdapter",
                "index_adapter": "knowledge_index.chroma_index_adapter.ChromaIndexAdapter",
                "embedding_calls": 0, "index_query_calls": 0,
                "frozen_build_identity": "unavailable",
            }, provenance_kind="GOVERNED_REAL_RAG_INVOCATION")


def test_docker_harness_rejects_injected_runner_without_execution(authority):
    store, _ = authority
    profile, namespace, facts = _docker_context(store)
    ready = evaluate_docker_harness(
        profile, facts, repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID)
    called = False

    def fake_runner(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not execute")

    with pytest.raises(ValueError, match="injected command runners"):
        execute_docker_invocation(
            store, ready, profile, namespace_id=namespace.namespace_id,
            repository_root=ROOT, repository_commit=REPO_COMMIT,
            command_runner=fake_runner)
    assert called is False


def test_docker_caller_cannot_mint_wrong_project_or_container_receipt(authority):
    store, _ = authority
    profile, namespace, facts = _docker_context(store)
    ready = evaluate_docker_harness(
        profile, facts, repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID)
    pins = {
        "compose_commitment": "sha256:" + "7" * 64,
        "repository_commit": REPO_COMMIT,
        "project_identity": namespace.project_name,
        "volume_identities": list(namespace.volume_identities),
        "workspace_identity": namespace.workspace_root,
        "runtime_service": "runtime",
        "runtime_entrypoint": "scripts/run_correlation_runtime.py",
        "runtime_config": "configs/runtime_orchestration.docker.yaml",
    }
    with pytest.raises(ValueError, match="protected"):
        _forged_actual_receipt(
            store, ready, pins=pins,
            provenance={
                "project_identity": "foreign-project",
                "container_identity": "unavailable", "service_identity": "runtime",
                "image_identity": "unavailable",
                "volume_identities": list(namespace.volume_identities),
                "probe_commitment": "sha256:" + "8" * 64,
            }, provenance_kind="ISOLATED_DOCKER_INVOCATION")


def test_docker_same_context_probe_and_cleanup_semantic_validation(authority):
    store, _ = authority
    profile, _, ready, start, probe, cleanup, terminal_at = \
        _docker_semantic_proof(store)

    assert _validate_docker_terminal_binding(
        store.reader(), ready, profile, start, probe,
        cleanup.cleanup_id, terminal_at) == cleanup


def test_docker_missing_probe_rejected(authority):
    store, _ = authority
    profile, _, ready, start, _, cleanup, terminal_at = \
        _docker_semantic_proof(store)

    with pytest.raises(ValueError, match="harness-issued probe"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start, None,
            cleanup.cleanup_id, terminal_at)


def test_docker_failed_probe_rejected(authority):
    store, _ = authority
    profile, _, ready, start, probe, cleanup, terminal_at = \
        _docker_semantic_proof(store)
    observation = json.loads(json.dumps(probe.observation))
    observation["work"][0]["status"] = "FAILED"
    failed = replace(
        probe, observation=observation,
        probe_commitment=commitment(observation))

    with pytest.raises(ValueError, match="Incident/Runtime"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start, failed,
            cleanup.cleanup_id, terminal_at)


def test_docker_malformed_probe_rejected(authority):
    store, _ = authority
    profile, _, ready, start, probe, cleanup, terminal_at = \
        _docker_semantic_proof(store)
    observation = {"work": probe.observation["work"]}
    malformed = replace(
        probe, observation=observation,
        probe_commitment=commitment(observation))

    with pytest.raises(ValueError, match="malformed or incomplete"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start, malformed,
            cleanup.cleanup_id, terminal_at)


@pytest.mark.parametrize("field", ("namespace_id", "execution_id",
                                   "invocation_id"))
def test_docker_foreign_probe_context_rejected(authority, field):
    store, _ = authority
    profile, _, ready, start, probe, cleanup, terminal_at = \
        _docker_semantic_proof(store)
    value = {
        "namespace_id": identity("docker_namespace", ["foreign"]),
        "execution_id": OTHER_EXECUTION,
        "invocation_id": "foreign-invocation",
    }[field]

    with pytest.raises(ValueError, match="exact invocation context|namespace"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start,
            replace(probe, **{field: value}), cleanup.cleanup_id,
            terminal_at)


def test_docker_stale_probe_before_invocation_rejected(authority):
    store, _ = authority
    profile, _, ready, start, probe, cleanup, terminal_at = \
        _docker_semantic_proof(store)
    stale = replace(probe, observed_at=_iso_after(start.started_at, -1))

    with pytest.raises(ValueError, match="timestamps"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start, stale,
            cleanup.cleanup_id, terminal_at)


def test_docker_missing_cleanup_rejected(authority):
    store, _ = authority
    profile, namespace, ready, start, probe, _, terminal_at = \
        _docker_semantic_proof(store)
    missing = identity("docker_cleanup", [namespace.namespace_id, "missing"])

    with pytest.raises(ValueError, match="durable namespace and cleanup"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start, probe, missing,
            terminal_at)


def test_docker_incomplete_cleanup_disposition_rejected(authority):
    store, _ = authority
    profile, _, ready, start, probe, cleanup, terminal_at = \
        _docker_semantic_proof(store, disposition="FAILED")

    with pytest.raises(ValueError, match="completed invocation"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start, probe,
            cleanup.cleanup_id, terminal_at)


def test_docker_foreign_or_wrong_namespace_cleanup_rejected(authority):
    store, _ = authority
    profile, _, ready, start, probe, _, terminal_at = \
        _docker_semantic_proof(store)
    foreign = _foreign_docker_cleanup(store, profile, ready)

    with pytest.raises(ValueError, match="completed invocation"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start, probe,
            foreign.cleanup_id, terminal_at)


def test_docker_stale_cleanup_from_previous_invocation_rejected(authority):
    store, _ = authority
    profile, namespace, ready, _, probe, cleanup, terminal_at = \
        _docker_semantic_proof(store)
    values = profile.values()
    second = _record_start(store, ready, {
        "compose_commitment": "sha256:" + "1" * 64,
        "repository_commit": REPO_COMMIT,
        "project_identity": namespace.project_name,
        "volume_identities": list(namespace.volume_identities),
        "workspace_identity": namespace.workspace_root,
        "runtime_service": values["runtime_service"],
        "runtime_entrypoint": values["runtime_entrypoint"],
        "runtime_config": values["runtime_config"],
        "namespace_identity": namespace.namespace_id,
    })
    second_probe = replace(
        probe, invocation_start_id=second.start_id,
        invocation_id=second.invocation_id,
        observed_at=_iso_after(second.started_at, 1))

    with pytest.raises(ValueError, match="completed invocation"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, second, second_probe,
            cleanup.cleanup_id, _iso_after(second.started_at, 3))


def test_docker_terminal_before_cleanup_rejected(authority):
    store, _ = authority
    profile, _, ready, start, probe, cleanup, _ = \
        _docker_semantic_proof(store)

    with pytest.raises(ValueError, match="timestamps"):
        _validate_docker_terminal_binding(
            store.reader(), ready, profile, start, probe,
            cleanup.cleanup_id, _iso_after(start.started_at, 1))


def test_docker_cleanup_before_invocation_start_rejected(authority):
    store, _ = authority
    profile, namespace, facts = _docker_context(store)
    ready = evaluate_docker_harness(
        profile, facts, repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID)
    values = profile.values()
    start = _record_start(store, ready, {
        "compose_commitment": "sha256:" + "1" * 64,
        "repository_commit": REPO_COMMIT,
        "project_identity": namespace.project_name,
        "volume_identities": list(namespace.volume_identities),
        "workspace_identity": namespace.workspace_root,
        "runtime_service": values["runtime_service"],
        "runtime_entrypoint": values["runtime_entrypoint"],
        "runtime_config": values["runtime_config"],
        "namespace_identity": namespace.namespace_id,
    })

    with pytest.raises(ValueError, match="precedes its invocation start"):
        record_cleanup_outcome(
            store, issuance_id=namespace.namespace_id,
            completed_at=_iso_after(start.started_at, -1),
            disposition="COMPLETED", invocation_start_id=start.start_id,
            invocation_id=start.invocation_id,
            invocation_started_at=start.started_at)


def test_actual_invocation_id_cross_execution_reuse_rejected(authority):
    store, _ = authority
    invocation_id = "INV-001"
    first = _internal_invalid_llm_start(
        store, _ready_llm(store), invocation_id=invocation_id)
    second_execution = _append_execution(store)
    second_ready = _ready_llm(
        store, execution_id=second_execution.record_id)

    with pytest.raises(LedgerConflict, match="different run/execution/gate/profile"):
        _internal_invalid_llm_start(
            store, second_ready, invocation_id=invocation_id)

    assert actual_invocation_status(
        store.reader(), first.record_id) is GateExecutionStatus.EXECUTION_STARTED


def test_actual_invocation_id_cross_run_reuse_rejected(authority):
    store, _ = authority
    invocation_id = "INV-001"
    first = _internal_invalid_llm_start(
        store, _ready_llm(store), invocation_id=invocation_id)
    second_run, second_execution = _append_run_and_execution(store)
    second_ready = _ready_llm(
        store, run_id=second_run.record_id,
        execution_id=second_execution.record_id)

    with pytest.raises(LedgerConflict, match="different run/execution/gate/profile"):
        _internal_invalid_llm_start(
            store, second_ready, invocation_id=invocation_id)

    assert actual_invocation_status(
        store.reader(), first.record_id) is GateExecutionStatus.EXECUTION_STARTED


def test_actual_invocation_id_cross_gate_reuse_rejected(authority):
    store, _ = authority
    invocation_id = "INV-001"
    first = _internal_invalid_llm_start(
        store, _ready_llm(store), invocation_id=invocation_id)

    with pytest.raises(LedgerConflict, match="different run/execution/gate/profile"):
        _internal_invalid_llm_start(
            store, _ready_rag(store), invocation_id=invocation_id)

    assert actual_invocation_status(
        store.reader(), first.record_id) is GateExecutionStatus.EXECUTION_STARTED


def test_actual_invocation_id_cross_profile_reuse_rejected(authority):
    store, _ = authority
    invocation_id = "INV-001"
    first = _internal_invalid_llm_start(
        store, _ready_llm(store), invocation_id=invocation_id)
    second_profile = replace(
        _profile(GateKind.REAL_LLM),
        profile_identity="spec017-real-llm-profile-p2",
        profile_commitment="sha256:" + "2" * 64,
    )

    with pytest.raises(LedgerConflict, match="different run/execution/gate/profile"):
        _internal_invalid_llm_start(
            store, _ready_llm(store, profile=second_profile),
            invocation_id=invocation_id)

    assert actual_invocation_status(
        store.reader(), first.record_id) is GateExecutionStatus.EXECUTION_STARTED


def test_actual_invocation_start_equivalent_replay_preserved(authority):
    store, _ = authority
    ready = _ready_llm(store)
    first = _internal_invalid_llm_start(
        store, ready, invocation_id="INV-001")
    replay = _internal_invalid_llm_start(
        store, ready, invocation_id="INV-001")

    assert replay == first


def test_actual_terminal_same_invocation_wrong_execution_rejected(authority):
    store, _ = authority
    start = _internal_invalid_llm_start(
        store, _ready_llm(store), invocation_id="INV-001")
    second_execution = _append_execution(store)
    start_payload = json.loads(start.semantic_json)
    terminal_payload = {
        "start_id": start.record_id, **start_payload,
        "execution_id": second_execution.record_id,
        "ended_at": LATER,
        "status": GateExecutionStatus.INVALID_EXECUTION.value,
        "provenance_kind": "LIVE_GEMINI_INVOCATION",
        "upstream_result_reference": "no-provider-result",
        "upstream_result_commitment": "sha256:" + "9" * 64,
        "authoritative_typed_outcome": "NO_ACTUAL_GENERATION_INVOCATION",
        "sanitized_provenance": {},
    }
    terminal = LedgerRecord.create(
        "gate_invocation_receipt", [start.record_id, "terminal"],
        terminal_payload, start.record_id)

    with pytest.raises(ValueError, match="contradicts its durable start"):
        store._append_validated(terminal)


def test_terminal_receipt_wrong_invocation_rejected_by_single_ledger(authority):
    store, _ = authority
    ready = _ready_llm(store)
    start = _internal_invalid_llm_start(
        store, ready, invocation_id="invocation-original")
    assert actual_invocation_status(
        store.reader(), start.record_id) is GateExecutionStatus.EXECUTION_STARTED
    start_payload = json.loads(start.semantic_json)
    terminal = {
        "start_id": start.record_id, **start_payload,
        "invocation_id": "invocation-foreign", "ended_at": LATER,
        "status": GateExecutionStatus.EXECUTED_PASS.value,
        "provenance_kind": "LIVE_GEMINI_INVOCATION",
        "upstream_result_reference": "result",
        "upstream_result_commitment": "sha256:" + "9" * 64,
        "authoritative_typed_outcome": "SUCCESS",
        "sanitized_provenance": {},
    }
    with pytest.raises(ValueError, match="protected"):
        store.append(LedgerRecord.create(
            "gate_invocation_receipt", [start.record_id, "terminal"],
            terminal, start.record_id))


def test_generic_append_rejects_actual_invocation_start(authority):
    store, _ = authority
    ready = _ready_llm(store)
    payload = {
        "evaluation_run_id": RUN_ID, "execution_id": EXECUTION_ID,
        "gate": ready.gate.value, "profile_identity": ready.profile_identity,
        "profile_commitment": ready.profile_commitment,
        "invocation_id": "caller-start", "started_at": NOW,
        "invocation_pins": {},
    }
    with pytest.raises(ValueError, match="protected"):
        store.append(LedgerRecord.create(
            "gate_invocation_start",
            [EXECUTION_ID, ready.gate.value, ready.profile_commitment,
             "caller-start"], payload, EXECUTION_ID))


def test_generic_append_rejects_actual_invocation_terminal(authority):
    store, _ = authority
    ready = _ready_llm(store)
    start = _internal_invalid_llm_start(store, ready)
    payload = {
        "start_id": start.record_id, **json.loads(start.semantic_json),
        "ended_at": LATER,
        "status": GateExecutionStatus.EXECUTED_PASS.value,
        "provenance_kind": "LIVE_GEMINI_INVOCATION",
        "upstream_result_reference": "caller-result",
        "upstream_result_commitment": "sha256:" + "8" * 64,
        "authoritative_typed_outcome": "VALIDATED_GENERATION_RESULT",
        "sanitized_provenance": {},
    }
    with pytest.raises(ValueError, match="protected"):
        store.append(LedgerRecord.create(
            "gate_invocation_receipt", [start.record_id, "terminal"],
            payload, start.record_id))


def test_terminal_without_actual_invocation_start_is_rejected(authority):
    store, _ = authority
    missing = identity("gate_invocation_start", [EXECUTION_ID, "missing"])
    with pytest.raises(ValueError, match="matching start"):
        store._record_actual_execution_terminal(
            start_id=missing,
            status=GateExecutionStatus.EXECUTED_PASS.value,
            ended_at=LATER, provenance_kind="LIVE_GEMINI_INVOCATION",
            upstream_result_reference="result",
            upstream_result_commitment="sha256:" + "7" * 64,
            authoritative_typed_outcome="VALIDATED_GENERATION_RESULT",
            sanitized_provenance={},
        )


def test_actual_terminal_rejects_wrong_result_facts(authority):
    store, _ = authority
    start = _internal_invalid_llm_start(store, _ready_llm(store))
    with pytest.raises(ValueError, match="upstream result commitment"):
        _internal_invalid_llm_terminal(
            store, start, upstream_result_commitment="wrong-result")
    assert actual_invocation_status(
        store.reader(), start.record_id) is GateExecutionStatus.EXECUTION_STARTED


def test_actual_terminal_contradiction_rejected_and_replay_preserved(authority):
    store, _ = authority
    start = _internal_invalid_llm_start(store, _ready_llm(store))
    first = _internal_invalid_llm_terminal(store, start)
    replay = _internal_invalid_llm_terminal(store, start)
    assert replay == first
    with pytest.raises(LedgerConflict, match="contradictory replay"):
        _internal_invalid_llm_terminal(
            store, start, authoritative_typed_outcome="DIFFERENT_OUTCOME")


def test_governed_issuance_primitive_records_only_invalid_test_outcome(authority):
    store, _ = authority
    ready = _ready_llm(store)
    start = _internal_invalid_llm_start(store, ready)
    record = _internal_invalid_llm_terminal(store, start)
    receipt = read_actual_invocation_receipt(store.reader(), record.record_id)
    assert receipt is not None
    assert receipt.status is GateExecutionStatus.INVALID_EXECUTION
    evidence = capture_gate_evidence(
        ready, repository_commit=REPO_COMMIT,
        environment_identity="pytest", references={"bounded": "reference"},
        actual_invocation_receipt_id=receipt.receipt_id,
        authority=store.reader())
    assert evidence.status is GateExecutionStatus.INVALID_EXECUTION


@pytest.mark.parametrize("gate", tuple(GateKind))
def test_missing_actual_invocation_receipt_never_promotes_ready_gate(authority, gate):
    store, _ = authority
    ready = _ready_gate(store, gate)
    with pytest.raises(ValueError, match="absent"):
        capture_gate_evidence(
            ready, repository_commit=REPO_COMMIT,
            environment_identity="pytest", references={"bounded": "reference"},
            actual_invocation_receipt_id=identity(
                "gate_invocation_receipt", [ready.execution_id, gate.value, "missing"]),
            authority=store.reader())
    evidence = capture_gate_evidence(
        ready, repository_commit=REPO_COMMIT,
        environment_identity="pytest", references={"bounded": "reference"})
    assert evidence.status is GateExecutionStatus.READY_NOT_EXECUTED


def test_docker_namespace_cleanup_and_restart_use_single_ledger(authority):
    store, path = authority
    profile, namespace, _ = _docker_context(store)
    assert cleanup_targets(
        store.reader(), profile, issuance_id=namespace.namespace_id,
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID,
        compose_project_name=namespace.project_name,
        volume_identities=namespace.volume_identities,
        workspace_root=namespace.workspace_root) == (
            namespace.project_name, namespace.volume_identities)
    cleanup = record_cleanup_outcome(
        store, issuance_id=namespace.namespace_id,
        completed_at=LATER, disposition="CLEANUP_SUCCEEDED")
    store.close()
    restarted = _open_store(path)
    try:
        assert read_docker_namespace(
            restarted.reader(), namespace.namespace_id) == namespace
        assert read_docker_cleanup(restarted.reader(), cleanup.cleanup_id) == cleanup
        assert lifecycle_command(
            restarted.reader(), profile, issuance_id=namespace.namespace_id,
            evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID,
            phase=DockerLifecyclePhase.CLEANUP)[-3:] == (
                "down", "--volumes", "--remove-orphans")
    finally:
        restarted.close()


def test_docker_namespace_replay_is_idempotent_and_reallocation_fails(authority):
    store, _ = authority
    profile, first, _ = _docker_context(store)
    assert allocate_namespace(
        store, profile, evaluation_run_id=RUN_ID,
        execution_id=EXECUTION_ID, issuance_nonce="nonce-1",
        issued_at=NOW) == first
    with pytest.raises(LedgerConflict):
        allocate_namespace(
            store, profile, evaluation_run_id=RUN_ID,
            execution_id=EXECUTION_ID, issuance_nonce="nonce-2",
            issued_at=NOW)


@pytest.mark.parametrize("change", ("project", "volume", "run", "execution", "profile"))
def test_docker_cleanup_rejects_foreign_scope(authority, change):
    store, _ = authority
    profile, namespace, _ = _docker_context(store)
    values = dict(
        authority=store.reader(), profile=profile,
        issuance_id=namespace.namespace_id,
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID,
        compose_project_name=namespace.project_name,
        volume_identities=namespace.volume_identities,
        workspace_root=namespace.workspace_root)
    if change == "project":
        values["compose_project_name"] = "foreign-project"
    elif change == "volume":
        values["volume_identities"] += ("foreign-volume",)
    elif change == "run":
        values["evaluation_run_id"] = OTHER_RUN
    elif change == "execution":
        values["execution_id"] = OTHER_EXECUTION
    else:
        values["profile"] = replace(
            profile, profile_commitment="sha256:" + "0" * 64)
    with pytest.raises(ValueError):
        cleanup_targets(**values)


def test_docker_readiness_uses_exact_durable_namespace(authority):
    store, _ = authority
    profile, namespace, facts = _docker_context(store)
    result = evaluate_docker_harness(
        profile, facts, repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID)
    assert result.status is GateExecutionStatus.READY_NOT_EXECUTED
    missing = replace(facts, issuance_id=identity("docker_namespace", ["missing"]))
    assert evaluate_docker_harness(
        profile, missing, repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID,
        execution_id=EXECUTION_ID).status is GateExecutionStatus.NOT_READY
    assert namespace.project_name.startswith("spec017eval-")


@pytest.mark.parametrize("overrides", (
    {"authority_state": None, "active_build_identity": None},
    {"chroma_index_available": False},
    {"actual_embedding_profile_identity": "wrong-profile"},
    {"chroma_path": "var/knowledge_index/chroma"},
    {"chroma_path": "var/rca_evaluation/real_rag/../knowledge_index/chroma"},
))
def test_rag_isolation_and_pins_preserved(authority, overrides):
    store, _ = authority
    assert _ready_rag(store, overrides=overrides).status is GateExecutionStatus.NOT_READY


@pytest.mark.parametrize(("field", "bad"), (
    ("embedding_adapter", "fake.Adapter"),
    ("embedding_profile_identity", "wrong-profile"),
))
def test_rag_profile_integrity_preserved(authority, tmp_path, field, bad):
    store, _ = authority
    raw = yaml.safe_load((PROFILES / "real_rag.yaml").read_text(encoding="utf-8"))
    raw["knowledge"][field] = bad
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    profile = load_real_rag_profile(path)
    assert _ready_rag(store, profile=profile).status is GateExecutionStatus.NOT_READY


def test_rag_symlink_escape_preserved(authority, tmp_path, monkeypatch):
    store, _ = authority
    (tmp_path / "configs").mkdir(); (tmp_path / "docs").mkdir()
    shutil.copy(ROOT / "configs" / "knowledge_index.yaml", tmp_path / "configs")
    shutil.copy(ROOT / "configs" / "knowledge_manifest.json", tmp_path / "configs")
    shutil.copytree(ROOT / "docs" / "knowledge", tmp_path / "docs" / "knowledge")
    isolated = tmp_path / "var" / "rca_evaluation" / "real_rag"
    outside = tmp_path / "outside"
    isolated.mkdir(parents=True); outside.mkdir()
    link = isolated / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if str(path).startswith(str(link)):
                tail = Path(str(path)[len(str(link)):].lstrip("\\/"))
                return original(outside / tail, *args, **kwargs)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    assert _ready_rag(
        store, root=tmp_path,
        overrides={"chroma_path": str(link / "chroma")}
    ).status is GateExecutionStatus.NOT_READY


@pytest.mark.parametrize("secret", (
    {"client_secret": "x"}, {"x-api-key": "x"},
    {"authorization": "x"}, {"token": "x"},
    {"opaque_reference": "Bearer abc.def"},
    {"opaque_reference": "eyJabcdefgh.eyJijklmnop.qrstuvwxyz"},
    {"opaque_reference": "AKIAIOSFODNN7EXAMPLE"},
    {"opaque_reference": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----"},
))
def test_secret_safety_preserved(secret):
    with pytest.raises(ValueError, match="secret"):
        assert_secret_free(secret)


def test_secret_free_profiles_and_gate_independence_preserved(authority):
    store, _ = authority
    for name in ("real_llm.yaml", "real_rag.yaml", "docker_e2e.yaml"):
        assert_secret_free(yaml.safe_load((PROFILES / name).read_text(encoding="utf-8")))
    llm = _ready_llm(store)
    rag = _ready_rag(store, overrides={"chroma_index_available": False})
    profile, _, facts = _docker_context(store)
    docker = evaluate_docker_harness(
        profile, facts, repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=RUN_ID, execution_id=EXECUTION_ID)
    assert llm.status is GateExecutionStatus.READY_NOT_EXECUTED
    assert rag.status is GateExecutionStatus.NOT_READY
    assert docker.status is GateExecutionStatus.READY_NOT_EXECUTED
