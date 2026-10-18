from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil

import pytest

from rca_evaluation.readiness import (
    ReadinessFactState, materialize_required_before_execution,
    resolve_formal_required_facts,
)
from rca_evaluation.runtime_image_readiness import (
    CommandResult, DependencyReproducibility, RUNTIME_IMAGE_PROFILE,
    RUNTIME_IMAGE_READINESS_SCHEMA, RUNTIME_IMAGE_TAG, RuntimeFactState,
    RuntimeImageReadinessResolver,
)


ROOT = Path(__file__).resolve().parents[1]
HEAD = "e9cee50ce7904fb822e8032bbe8b05a4a826cf07"
SHA = "sha256:" + "a" * 64
PYTHON_BASE_DIGEST = "sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"


class _Commands:
    def __init__(self, *, dirty: bool = True, engine: bool = False,
                 image: object | None = None) -> None:
        self.dirty = dirty
        self.engine = engine
        self.image = image
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command, cwd):
        key = tuple(command)
        self.calls.append(key)
        if key == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, HEAD)
        if key == ("git", "status", "--porcelain"):
            return CommandResult(0, " M source.py" if self.dirty else "")
        if key == ("docker", "--version"):
            return CommandResult(0, "Docker version 27.3.1, build ce12230")
        if key == ("docker", "compose", "version"):
            return CommandResult(0, "Docker Compose version v2.29.7")
        if key == ("docker", "version", "--format", "{{json .Server}}"):
            if not self.engine:
                return CommandResult(1, "", "daemon unavailable")
            return CommandResult(0, json.dumps(
                {"Version": "27.3.1", "ApiVersion": "1.47"}))
        if key == ("docker", "image", "inspect", RUNTIME_IMAGE_TAG):
            return (CommandResult(1, "", "not found") if self.image is None
                    else CommandResult(0, json.dumps(self.image)))
        raise AssertionError(f"unexpected command: {key}")


def _resolver(root: Path = ROOT, **kwargs):
    commands = _Commands(**kwargs)
    return RuntimeImageReadinessResolver(
        root, _command_runner=commands), commands


def _copy_source(tmp_path: Path) -> Path:
    for relative in (
        "docker/runtime/Dockerfile", "docker-compose.yml",
        "configs/runtime_orchestration.docker.yaml",
        "configs/rca_evaluation/docker_e2e.yaml", "requirements.txt",
        "scripts/run_correlation_runtime.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def _template(source) -> dict[str, object]:
    return {
        "manifest_inputs": {
            "approved_spec_commitments": {
                "docker_compose_digest": source.compose_commitment,
                "docker_config_digest": source.build_context_identity,
                "docker_image_digest": "REQUIRED_BEFORE_EXECUTION",
                "docker_namespace_identity": "REQUIRED_BEFORE_EXECUTION",
                "tool_versions": {"docker": "REQUIRED_BEFORE_EXECUTION"},
            },
            "rag_profile_references": [],
            "provider_profile_references": [],
        },
    }


def test_source_commitments_are_deterministic_and_bind_all_relevant_definitions():
    resolver, commands = _resolver()
    first = resolver.formal_readiness()
    second = resolver.formal_readiness()
    assert first == second
    assert first.schema == RUNTIME_IMAGE_READINESS_SCHEMA
    assert first.source.source_definition.state is RuntimeFactState.AVAILABLE
    assert first.source.source_definition.value == "SOURCE_DEFINITION_READY"
    assert first.source.dockerfile_commitment.startswith("sha256:")
    assert first.source.runtime_config_commitment.startswith("sha256:")
    assert first.source.compose_commitment.startswith("sha256:")
    assert first.source.formal_config_commitment.startswith("sha256:")
    assert first.source.dependency_commitment.startswith("sha256:")
    assert first.source.entrypoint_commitment.startswith("sha256:")
    assert first.readiness_commitment == second.readiness_commitment
    assert len(commands.calls) == 12


def test_preregistration_declares_current_compose_and_build_context_commitments():
    resolver, _ = _resolver()
    source = resolver.formal_readiness().source
    preregistration = json.loads((
        ROOT / "configs/rca_evaluation/formal_preregistration_v1.json"
    ).read_text(encoding="utf-8"))
    approved = preregistration["manifest_inputs"]["approved_spec_commitments"]
    assert approved["docker_compose_digest"] == source.compose_commitment
    assert approved["docker_config_digest"] == source.build_context_identity


def test_current_repository_revision_is_fresh_and_dirty_is_not_formal_ready():
    dirty, _ = _resolver(dirty=True)
    dirty_fact = dirty.formal_readiness().source
    assert dirty_fact.repository_revision == HEAD
    assert dirty_fact.repository_dirty is True
    assert dirty_fact.formal_build_source.state is RuntimeFactState.UNRESOLVED

    clean, _ = _resolver(dirty=False)
    clean_fact = clean.formal_readiness().source
    assert clean_fact.formal_build_source.state is RuntimeFactState.AVAILABLE
    assert clean_fact.formal_build_source.value == f"git:{HEAD}"


def test_toolchain_versions_are_typed_and_engine_unavailable_is_unresolved():
    resolver, _ = _resolver(engine=False)
    facts = resolver.formal_readiness()
    assert facts.docker_client.value == "27.3.1"
    assert facts.docker_compose.value == "2.29.7"
    assert facts.docker_engine.state is RuntimeFactState.UNRESOLVED
    assert facts.docker_engine.value is None


def test_engine_version_and_api_are_resolved_only_from_current_server_output():
    resolver, _ = _resolver(engine=True)
    engine = resolver.formal_readiness().docker_engine
    assert engine.state is RuntimeFactState.AVAILABLE
    assert engine.value == {"version": "27.3.1", "api_version": "1.47"}


def test_successful_but_malformed_tool_versions_are_contradictory():
    commands = _Commands(engine=True)

    def malformed(command, cwd):
        if tuple(command) in {
                ("docker", "--version"),
                ("docker", "compose", "version")}:
            return CommandResult(0, "unsupported")
        return commands(command, cwd)

    facts = RuntimeImageReadinessResolver(
        ROOT, _command_runner=malformed).formal_readiness()
    assert facts.docker_client.state is RuntimeFactState.CONTRADICTORY
    assert facts.docker_compose.state is RuntimeFactState.CONTRADICTORY


def test_actual_image_is_unresolved_but_base_image_is_exactly_digest_pinned():
    resolver, _ = _resolver()
    facts = resolver.formal_readiness()
    assert facts.actual_image.state is RuntimeFactState.UNRESOLVED
    assert facts.actual_image.value is None
    assert facts.source.base_image_reference == (
        f"python:3.12-slim@{PYTHON_BASE_DIGEST}")
    assert facts.source.base_image_digest.state is RuntimeFactState.AVAILABLE
    assert facts.source.base_image_digest.value == PYTHON_BASE_DIGEST
    assert facts.source.logical_image_identity == RUNTIME_IMAGE_PROFILE
    assert facts.source.image_tag == RUNTIME_IMAGE_TAG
    assert facts.source.image_tag != facts.actual_image.value


def test_dependency_definition_is_truthfully_partially_pinned():
    resolver, _ = _resolver()
    source = resolver.formal_readiness().source
    assert source.dependency_reproducibility is DependencyReproducibility.PARTIALLY_PINNED


def test_future_image_inspection_requires_content_identity_and_source_labels():
    initial, _ = _resolver(dirty=False, engine=True)
    source = initial.formal_readiness().source
    image = [{
        "Id": SHA,
        "RepoDigests": [f"registry.invalid/runtime@{SHA}"],
        "Config": {"Labels": {
            "org.opencontainers.image.revision": HEAD,
            "io.aiops.runtime.source-commitment": source.source_commitment,
            "io.aiops.runtime.profile": RUNTIME_IMAGE_PROFILE,
        }},
    }]
    resolver, _ = _resolver(dirty=False, engine=True, image=image)
    actual = resolver.formal_readiness().actual_image
    assert actual.state is RuntimeFactState.AVAILABLE
    assert actual.value["image_id"] == SHA
    assert actual.value["image_digest"] == SHA

    image[0]["Config"]["Labels"]["org.opencontainers.image.revision"] = "b" * 40
    contradicted, _ = _resolver(dirty=False, engine=True, image=image)
    assert contradicted.formal_readiness().actual_image.state is RuntimeFactState.CONTRADICTORY


def test_caller_mapping_cannot_fabricate_resolved_runtime_image_fact():
    class CallerAssertion:
        def formal_readiness(self):
            return {"image_digest": SHA, "docker_version": "caller"}

    resolver, _ = _resolver()
    template = _template(resolver.formal_readiness().source)
    resolution = resolve_formal_required_facts(
        template, repository_root=ROOT, runtime_authority=CallerAssertion())
    runtime = next(fact for fact in resolution.prerequisites
                   if fact.name == "runtime_image_readiness")
    assert runtime.state is ReadinessFactState.CONTRADICTORY
    with pytest.raises(ValueError, match="contradictory required fact"):
        resolution.assert_resolved()


def test_required_fact_integration_keeps_unbuilt_image_fail_closed():
    resolver, _ = _resolver(dirty=False, engine=True)
    source = resolver.formal_readiness().source
    resolution = resolve_formal_required_facts(
        _template(source), repository_root=ROOT, runtime_authority=resolver)
    selected = dict(resolution.materialization.facts)
    image = selected[
        "/manifest_inputs/approved_spec_commitments/docker_image_digest"]
    assert image.state is ReadinessFactState.UNRESOLVED
    with pytest.raises(ValueError, match="unresolved required fact"):
        materialize_required_before_execution(
            _template(source), resolution.materialization)


@pytest.mark.parametrize("relative", [
    "docker/runtime/Dockerfile",
    "docker-compose.yml",
    "configs/runtime_orchestration.docker.yaml",
    "requirements.txt",
])
def test_source_and_declared_config_drift_is_contradictory(tmp_path, relative):
    original, _ = _resolver()
    source = original.formal_readiness().source
    copied = _copy_source(tmp_path)
    changed = copied / relative
    changed.write_text(changed.read_text(encoding="utf-8") + "\n# drift\n",
                       encoding="utf-8")
    drifted, _ = _resolver(copied)
    resolution = resolve_formal_required_facts(
        _template(source), repository_root=copied, runtime_authority=drifted)
    drift = next(fact for fact in resolution.prerequisites
                 if fact.name == "runtime_image_declared_configuration")
    assert drift.state is ReadinessFactState.CONTRADICTORY


def test_logical_image_identity_drift_invalidates_typed_source():
    resolver, _ = _resolver()
    source = resolver.formal_readiness().source
    with pytest.raises(ValueError, match="source commitment"):
        replace(source, logical_image_identity="caller-tag").validate()


def test_unknown_readiness_schema_is_fail_closed():
    resolver, _ = _resolver()
    valid = resolver.formal_readiness()

    class UnknownSchema:
        def formal_readiness(self):
            return replace(valid, schema="spec017.runtime-image-readiness.v999")

    resolution = resolve_formal_required_facts(
        _template(valid.source), repository_root=ROOT,
        runtime_authority=UnknownSchema())
    fact = next(item for item in resolution.prerequisites
                if item.name == "runtime_image_readiness")
    assert fact.state is ReadinessFactState.CONTRADICTORY


def test_formal_readiness_is_read_only_and_has_no_runtime_control_commands():
    resolver, commands = _resolver()
    before = {path: path.read_bytes() for path in (
        ROOT / "docker/runtime/Dockerfile", ROOT / "docker-compose.yml",
        ROOT / "configs/runtime_orchestration.docker.yaml",
        ROOT / "configs/rca_evaluation/docker_e2e.yaml",
        ROOT / "requirements.txt", ROOT / "scripts/run_correlation_runtime.py",
    )}
    resolver.formal_readiness()
    after = {path: path.read_bytes() for path in before}
    assert after == before
    flattened = " ".join(" ".join(call) for call in commands.calls)
    for forbidden in (" build", " up", " run", " start", " retry", " recover"):
        assert forbidden not in flattened
    assert commands.calls[-1] == (
        "docker", "image", "inspect", RUNTIME_IMAGE_TAG)


def test_dockerfile_declares_future_source_revision_binding():
    dockerfile = (ROOT / "docker/runtime/Dockerfile").read_text(encoding="utf-8")
    assert "ARG SOURCE_REVISION=UNRESOLVED" in dockerfile
    assert "ARG RUNTIME_IMAGE_SOURCE_COMMITMENT=UNRESOLVED" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "io.aiops.runtime.source-commitment" in dockerfile
    assert f'io.aiops.runtime.profile="{RUNTIME_IMAGE_PROFILE}"' in dockerfile
