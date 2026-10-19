from __future__ import annotations

import copy
from dataclasses import fields, replace
import hashlib
import inspect
from pathlib import Path

import pytest

import rca_evaluation.formal_admission as formal_admission_module
import rca_evaluation.preparation as preparation_module
from rca_evaluation.formal_evidence import authorize_formal_execution
from rca_evaluation.identity import commitment
from rca_evaluation.preparation import (
    CandidateCPreparationRequest, PreparationResourceDomain,
    PreparationResourceFact, PreparationResourceFacts,
    _prepared_resources_payload, prepare_authoritative_resources,
    prepared_resource_semantic_identity,
    validate_prepared_resource_freshness,
)
from rca_evaluation.runner import CampaignRunner
from rca_evaluation.runtime_image_readiness import PreparedRuntimeImage
from rca_evaluation.sqlite_store import SqliteEvaluationStore
from tests.test_spec_017_r4_slice0_preparation import ROOT, _prepare


def _sha(char: str) -> str:
    return "sha256:" + char * 64


def _facts(prepared: dict[str, object], overrides=None,
           semantic_overrides=None,
           observed_at="2026-10-19T00:00:00Z") -> PreparationResourceFacts:
    overrides = overrides or {}
    semantic_overrides = semantic_overrides or {}
    preparation_id = prepared["preparation_id"]
    planned_run = prepared["planned_lineage"]["planned_run_identity"]
    namespace = prepared["docker_namespace"]["namespace_identity"]
    observed = {
        PreparationResourceDomain.RUNTIME_IMAGE: _sha("a"),
        PreparationResourceDomain.DOCKER_TOPOLOGY: _sha("b"),
        PreparationResourceDomain.DOCKER_OWNERSHIP: _sha("c"),
        PreparationResourceDomain.DOCKER_HEALTH: _sha("d"),
        PreparationResourceDomain.DOCKER_STALE_STATE: _sha("6"),
        PreparationResourceDomain.DOCKER_ACTUAL_RESOURCES: _sha("7"),
        PreparationResourceDomain.REAL_RAG_BUILD: "kbld_" + "e" * 64,
        PreparationResourceDomain.REAL_RAG_ACTIVATION: _sha("f"),
        PreparationResourceDomain.REAL_RAG_CHILD_MOUNT: _sha("1"),
        PreparationResourceDomain.REAL_RAG_CORPUS_MOUNT: _sha("2"),
    } | overrides
    expected = {
        PreparationResourceDomain.RUNTIME_IMAGE: "spec011-runtime-formal-v1",
        PreparationResourceDomain.DOCKER_TOPOLOGY: namespace,
        PreparationResourceDomain.DOCKER_OWNERSHIP: _sha("b"),
        PreparationResourceDomain.DOCKER_HEALTH: _sha("b"),
        PreparationResourceDomain.DOCKER_STALE_STATE: _sha("b"),
        PreparationResourceDomain.DOCKER_ACTUAL_RESOURCES: _sha("b"),
        PreparationResourceDomain.REAL_RAG_BUILD: _sha("3"),
        PreparationResourceDomain.REAL_RAG_ACTIVATION: observed[
            PreparationResourceDomain.REAL_RAG_BUILD],
        PreparationResourceDomain.REAL_RAG_CHILD_MOUNT: _sha("4"),
        PreparationResourceDomain.REAL_RAG_CORPUS_MOUNT: _sha("5"),
    }
    return PreparationResourceFacts.create(tuple(
        PreparationResourceFact.create(
            domain, preparation_id, planned_run, expected[domain], observed[domain],
            observed_at,
            semantic_identity_commitment=semantic_overrides.get(domain))
        for domain in PreparationResourceDomain
    ))


def _persisted(tmp_path: Path):
    report, evaluation = _prepare(tmp_path)
    store = SqliteEvaluationStore(evaluation / "evaluation.db")
    prepared = store.get_preparation(report.preparation_id)
    target = evaluation / prepared["real_rag_child"]["relative_path"]
    artifact = target / "candidate-c" / "index.bin"
    artifact.parent.mkdir()
    artifact.write_bytes(b"authoritative-candidate-c")
    artifacts = {"candidate-c/index.bin":
                 "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()}
    facts = _facts(prepared)
    payload = _prepared_resources_payload(
        prepared, facts=facts, artifacts=artifacts,
        corpus_identity=_sha("3"))
    store._record_prepared_resources(report.preparation_id, payload)
    return report, evaluation, store, facts, payload


def test_supported_preparation_api_has_no_caller_minted_fact_parameters():
    parameters = set(inspect.signature(prepare_authoritative_resources).parameters)
    forbidden = {
        "runtime_image_identity", "docker_ownership_commitment", "health",
        "corpus_index", "knowledge_build_identity", "real_rag_build",
        "real_rag_activation", "mount_commitment", "metadata", "payload",
    }
    assert parameters.isdisjoint(forbidden)
    assert not hasattr(SqliteEvaluationStore, "record_prepared_resources")


def test_fake_authoritative_adapters_drive_typed_deterministic_preparation(
    tmp_path, monkeypatch,
):
    report, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        prepared = store.get_preparation(report.preparation_id)
        facts = _facts(prepared)
        image_material = {
            "repository_revision": "a" * 40,
            "source_commitment": _sha("6"),
            "dockerfile_commitment": _sha("7"),
            "base_image_digest": _sha("8"),
            "dependency_commitment": _sha("5"),
            "runtime_profile": "spec011-runtime-formal-v1",
            "image_id": _sha("9"), "image_digest": _sha("0"),
        }
        image = PreparedRuntimeImage(
            "a" * 40, _sha("6"), _sha("7"), _sha("8"), _sha("5"),
            "spec011-runtime-formal-v1", _sha("9"), _sha("0"),
            commitment(image_material))
        facts = _facts(
            prepared, {PreparationResourceDomain.RUNTIME_IMAGE: image.image_digest},
            {PreparationResourceDomain.RUNTIME_IMAGE:
                image.preparation_commitment})

        class Runtime:
            def prepare_image(self, operations):
                operations.calls.append("image")
                return image

        class ImageOperations:
            def __init__(self): self.calls = []

        class DockerOperations:
            def __init__(self): self.calls = []
            def create_exact(self, authority, topology):
                self.calls.append((authority.preparation_id,
                                   topology.topology_commitment))

        class CandidateOperations:
            def __init__(self): self.calls = []
            def prepare_exact(self, request: CandidateCPreparationRequest):
                self.calls.append(request.child_identity)
                artifact = Path(request.child_path) / "candidate-c" / "index.bin"
                artifact.parent.mkdir()
                artifact.write_bytes(b"adapter-output")

        monkeypatch.setattr(
            preparation_module, "observe_authoritative_preparation_facts",
            lambda **_: facts)
        image_ops, docker_ops, candidate_ops = (
            ImageOperations(), DockerOperations(), CandidateOperations())
        result = prepare_authoritative_resources(
            store=store, preparation_id=report.preparation_id,
            repository_root=ROOT, runtime_authority=Runtime(),
            runtime_image_operations=image_ops, docker_operations=docker_ops,
            docker_state_inspector=object(),
            candidate_c_operations=candidate_ops,
            candidate_c_authority=object(),
            container_real_rag_inspector=object())
        assert result == facts
        assert image_ops.calls == ["image"]
        assert len(docker_ops.calls) == 1 and len(candidate_ops.calls) == 1
        assert store.get_preparation(report.preparation_id)[
            "resource_facts"]["facts_commitment"] == facts.facts_commitment


@pytest.mark.parametrize("domain", [
    PreparationResourceDomain.RUNTIME_IMAGE,
    PreparationResourceDomain.DOCKER_TOPOLOGY,
    PreparationResourceDomain.DOCKER_OWNERSHIP,
    PreparationResourceDomain.DOCKER_HEALTH,
    PreparationResourceDomain.DOCKER_STALE_STATE,
    PreparationResourceDomain.DOCKER_ACTUAL_RESOURCES,
    PreparationResourceDomain.REAL_RAG_BUILD,
    PreparationResourceDomain.REAL_RAG_ACTIVATION,
    PreparationResourceDomain.REAL_RAG_CHILD_MOUNT,
    PreparationResourceDomain.REAL_RAG_CORPUS_MOUNT,
])
def test_fresh_domain_mismatch_fails_closed(tmp_path, monkeypatch, domain):
    report, _, store, _, payload = _persisted(tmp_path)
    try:
        changed = ("kbld_" + "9" * 64
                   if domain is PreparationResourceDomain.REAL_RAG_BUILD
                   else _sha("9"))
        fresh = _facts(payload, {domain: changed})
        monkeypatch.setattr(
            preparation_module, "observe_authoritative_preparation_facts",
            lambda **_: fresh)
        with pytest.raises(ValueError, match="contradict"):
            validate_prepared_resource_freshness(
                store=store, preparation_id=report.preparation_id,
                repository_root=ROOT, runtime_authority=object(),
                docker_state_inspector=object(), candidate_c_authority=object(),
                container_real_rag_inspector=object())
    finally:
        store.close()


def test_semantic_identity_ignores_observation_time():
    prepared = {
        "preparation_id": "eval:preparation:" + "a" * 64,
        "planned_lineage": {
            "planned_run_identity": "eval:planned_run:" + "b" * 64},
        "docker_namespace": {
            "namespace_identity": "eval:docker_namespace:" + "c" * 64},
    }
    first = _facts(prepared, observed_at="2026-10-19T00:00:00Z")
    second = _facts(
        prepared,
        observed_at="2026-10-19T00:01:00Z")
    assert first.facts_commitment != second.facts_commitment
    assert prepared_resource_semantic_identity(first) == (
        prepared_resource_semantic_identity(second))


def test_fresh_revalidation_passes_with_new_timestamp(tmp_path, monkeypatch):
    report, _, store, _, payload = _persisted(tmp_path)
    try:
        fresh = _facts(
            payload,
            observed_at="2026-10-19T00:05:00Z")
        monkeypatch.setattr(
            preparation_module, "observe_authoritative_preparation_facts",
            lambda **_: fresh)
        assert validate_prepared_resource_freshness(
            store=store, preparation_id=report.preparation_id,
            repository_root=ROOT, runtime_authority=object(),
            docker_state_inspector=object(), candidate_c_authority=object(),
            container_real_rag_inspector=object()) == fresh
    finally:
        store.close()


@pytest.mark.parametrize("field", [
    "repository_revision", "source_commitment", "dockerfile_commitment",
    "base_image_digest", "dependency_commitment", "runtime_profile",
    "image_id", "image_digest",
])
def test_runtime_semantic_identity_fails_closed_for_every_bound_field(field):
    prepared = {
        "preparation_id": "eval:preparation:" + "a" * 64,
        "planned_lineage": {
            "planned_run_identity": "eval:planned_run:" + "b" * 64},
        "docker_namespace": {
            "namespace_identity": "eval:docker_namespace:" + "c" * 64},
    }
    base = {
        "repository_revision": "a" * 40,
        "source_commitment": _sha("1"),
        "dockerfile_commitment": _sha("2"),
        "base_image_digest": _sha("3"),
        "dependency_commitment": _sha("4"),
        "runtime_profile": "spec011-runtime-formal-v1",
        "image_id": _sha("5"),
        "image_digest": _sha("6"),
    }
    changed = dict(base)
    changed[field] = (
        "b" * 40 if field == "repository_revision"
        else "spec011-runtime-formal-v2" if field == "runtime_profile"
        else _sha("9"))
    base_facts = _facts(
        prepared,
        semantic_overrides={PreparationResourceDomain.RUNTIME_IMAGE:
                            commitment(base)})
    changed_facts = _facts(
        prepared,
        overrides=({PreparationResourceDomain.RUNTIME_IMAGE: _sha("9")}
                   if field in {"image_id", "image_digest"} else None),
        semantic_overrides={PreparationResourceDomain.RUNTIME_IMAGE:
                            commitment(changed)})
    assert prepared_resource_semantic_identity(base_facts) != (
        prepared_resource_semantic_identity(changed_facts))


def test_container_result_is_not_a_supported_authority_and_execution_revalidates():
    assert "container_real_rag_inspector" in inspect.signature(
        authorize_formal_execution).parameters
    assert "container_real_rag_inspector" in inspect.signature(
        CampaignRunner.run_formal).parameters
    assert "container_real_rag_readiness" not in inspect.signature(
        authorize_formal_execution).parameters
    assert "validate_prepared_resource_freshness(" in inspect.getsource(
        formal_admission_module._final_admit_formal_campaign)
    assert "validate_prepared_resource_freshness(" in inspect.getsource(
        authorize_formal_execution)


def test_positive_persistence_schema_rejects_generic_or_credential_fields(tmp_path):
    report, _, store, _, payload = _persisted(tmp_path)
    try:
        for name in ("metadata", "ground_truth", "token", "authorization_header"):
            changed = {**payload, name: "arbitrary-value"}
            with pytest.raises(ValueError, match="schema"):
                store._validate_preparation_payload(changed)
        typed_fields = {item.name for item in fields(PreparationResourceFact)}
        assert typed_fields.isdisjoint({
            "metadata", "notes", "payload", "ground_truth", "credential_value",
            "token", "authorization_header", "provider_error_body", "url",
            "source_reference"})
        assert "source_reference" not in inspect.signature(
            PreparationResourceFact.create).parameters
        fact = _facts(payload).facts[0]
        assert "source_reference" not in fact.material()
        with pytest.raises(TypeError):
            replace(_facts(payload).facts[0], metadata="free text")
        with pytest.raises(TypeError):
            PreparationResourceFact.create(
                fact.domain, fact.preparation_id, fact.planned_run_identity,
                fact.expected_target_identity, fact.actual_observed_identity,
                fact.observed_at, source_reference="ground-truth:answer")
        for source_reference in (
                "ground-truth:answer",
                "https://user:secret@example.invalid/resource?token=opaque",
                "Authorization: Bearer opaque-token",
                "provider raw arbitrary text"):
            changed = copy.deepcopy(payload)
            changed["resource_facts"]["facts"][0][
                "source_reference"] = source_reference
            with pytest.raises(ValueError, match="allowlisted"):
                store._validate_preparation_payload(changed)
        with pytest.raises(ValueError, match="only ADMITTED"):
            store.require_admitted_preparation(report.preparation_id, _sha("a"))
    finally:
        store.close()


def test_new_exact_schema_persists_reloads_and_validates_commitments(tmp_path):
    _, _, store, facts, payload = _persisted(tmp_path)
    try:
        facts.validate()
        for fact in facts.facts:
            fact.validate()
            assert commitment(fact.material(include_commitment=False)) == (
                fact.fact_commitment)
        assert commitment([fact.material() for fact in facts.facts]) == (
            facts.facts_commitment)

        preparation_id = store._validate_preparation_payload(payload)
        reloaded = store.get_preparation(preparation_id)
        assert reloaded is not None
        assert reloaded["resource_facts"] == facts.material()
    finally:
        store.close()
