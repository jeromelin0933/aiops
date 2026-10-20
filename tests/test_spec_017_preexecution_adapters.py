from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import rca_evaluation.preparation_adapters as adapters
import knowledge_index
from knowledge_index import (
    BuildValidationState, KnowledgeLocalReadiness, KnowledgeReadStatus,
    admit_production_manifest, load_knowledge_config,
)
from rca_evaluation.container_wiring import (
    ContainerRealRagInspectionTarget, resolve_formal_container_wiring,
)
from rca_evaluation.external_commands import (
    CommandExecutionError, CommandResult,
)
from rca_evaluation.formal_docker_lifecycle import (
    DockerResourceKind, assess_exact_ownership, project_health,
    resolve_preparation_docker_authority,
)
from rca_evaluation.formal_compose import resolve_formal_compose_topology
from rca_evaluation.preparation import (
    CandidateCPreparationRequest, PREPARED_CHILD_MARKER,
    PreparationResourceDomain, _artifact_manifest, _prepared_resources_payload,
    verify_prepared_real_rag_child,
)
from rca_evaluation.runtime_image_readiness import (
    CommandResult as RuntimeCommandResult, RuntimeImageReadinessResolver,
)
from rca_evaluation.sqlite_store import SqliteEvaluationStore
from tests.test_spec_017_r4_slice0_preparation import ROOT, _prepare
from tests.test_spec_017_preadmission_preparation_authority import _facts


class RecordingExecutor:
    def __init__(self, result=CommandResult(0, "")):
        self.result = result
        self.calls = []

    def execute(self, argv, *, cwd, environment=None, timeout_seconds=30.0):
        self.calls.append((tuple(argv), Path(cwd), environment, timeout_seconds))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _source():
    class Commands:
        def __call__(self, command, cwd):
            key = tuple(command)
            if key == ("git", "rev-parse", "HEAD"):
                return RuntimeCommandResult(
                    0, "97f7372b82676718284af69ff9300a0bec34cb3f")
            if key == ("git", "status", "--porcelain"):
                return RuntimeCommandResult(0, "")
            if key == ("docker", "--version"):
                return RuntimeCommandResult(0, "Docker version 27.3.1")
            if key == ("docker", "compose", "version"):
                return RuntimeCommandResult(0, "Docker Compose version v2.29.7")
            if key == ("docker", "version", "--format", "{{json .Server}}"):
                return RuntimeCommandResult(
                    0, json.dumps({"Version": "27.3.1", "ApiVersion": "1.47"}))
            if key[:3] == ("docker", "image", "inspect"):
                return RuntimeCommandResult(1, "")
            raise AssertionError(key)
    return RuntimeImageReadinessResolver(
        ROOT, _command_runner=Commands()).formal_readiness().source


def test_runtime_build_adapter_uses_exact_argv_and_no_secret(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "DO_NOT_PROPAGATE")
    executor = RecordingExecutor()
    adapters.DockerRuntimeImagePreparationOperations(
        ROOT, executor=executor).build_exact(_source())
    argv, cwd, environment, timeout = executor.calls[0]
    assert argv[:3] == ("docker", "build", "--file")
    assert "docker/runtime/Dockerfile" in argv[3].replace("\\", "/")
    assert argv[-1] == str(ROOT)
    assert "aiops-spec011-runtime:formal" in argv
    assert "97f7372b82676718284af69ff9300a0bec34cb3f" in " ".join(argv)
    assert "DO_NOT_PROPAGATE" not in " ".join(argv)
    assert environment is None and cwd == ROOT and timeout == 900.0


@pytest.mark.parametrize("failure", [
    CommandResult(2, "", "secret provider details"),
    CommandExecutionError("external command timed out"),
])
def test_runtime_build_adapter_fails_closed(failure):
    executor = RecordingExecutor(failure)
    with pytest.raises(CommandExecutionError):
        adapters.DockerRuntimeImagePreparationOperations(
            ROOT, executor=executor).build_exact(_source())


def test_compose_creation_binds_every_exact_project_and_never_defaults(
    tmp_path, monkeypatch,
):
    report, evaluation = _prepare(tmp_path)
    executor = RecordingExecutor()
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        topology = resolve_formal_compose_topology(
            store, report.preparation_id, ROOT)
        authority = resolve_preparation_docker_authority(
            store, report.preparation_id, ROOT)
        monkeypatch.setattr(
            adapters, "materialize_formal_compose_environment",
            lambda *args: {
                "SPEC017_REAL_RAG_CHILD_SOURCE": str(
                    evaluation / "real_rag" / "child"),
                "SPEC017_REAL_RAG_CORPUS_SOURCE": str(ROOT / "docs/knowledge"),
            })
        adapters.ComposePreparationDockerOperations(
            store, report.preparation_id, ROOT,
            executor=executor).create_exact(authority, topology)
    assert len(executor.calls) == 30
    observed_projects = []
    for argv, _, environment, _ in executor.calls:
        assert argv[:2] == ("docker", "compose")
        assert "--file" in argv and str(ROOT / "docker-compose.formal.yml") in argv
        assert "--project-name" in argv and "--no-build" in argv
        project = argv[argv.index("--project-name") + 1]
        assert project and project != "default"
        observed_projects.append(project)
        assert set(environment) == {
            "SPEC017_REAL_RAG_CHILD_SOURCE", "SPEC017_REAL_RAG_CORPUS_SOURCE"}
    assert tuple(observed_projects) == authority.project_names


def test_compose_creation_failure_never_becomes_success(tmp_path, monkeypatch):
    report, evaluation = _prepare(tmp_path)
    executor = RecordingExecutor(CommandResult(7, "", "private failure"))
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        topology = resolve_formal_compose_topology(
            store, report.preparation_id, ROOT)
        authority = resolve_preparation_docker_authority(
            store, report.preparation_id, ROOT)
        monkeypatch.setattr(
            adapters, "materialize_formal_compose_environment",
            lambda *args: {
                "SPEC017_REAL_RAG_CHILD_SOURCE": str(
                    evaluation / "real_rag" / "child"),
                "SPEC017_REAL_RAG_CORPUS_SOURCE": str(ROOT / "docs/knowledge"),
            })
        with pytest.raises(CommandExecutionError, match="non-zero"):
            adapters.ComposePreparationDockerOperations(
                store, report.preparation_id, ROOT,
                executor=executor).create_exact(authority, topology)


class DockerInspectionExecutor:
    def __init__(self, envelope, *, malformed=False, foreign=False,
                 unhealthy=False, wrong_image=False, extra=False,
                 runtime_status="running", wrong_network=False,
                 duplicate_id=False, duplicate_name=False,
                 malformed_mount=False, malformed_health=False):
        self.envelope = envelope
        self.malformed = malformed
        self.foreign = foreign
        self.unhealthy = unhealthy
        self.wrong_image = wrong_image
        self.extra = extra
        self.runtime_status = runtime_status
        self.wrong_network = wrong_network
        self.duplicate_id = duplicate_id
        self.duplicate_name = duplicate_name
        self.malformed_mount = malformed_mount
        self.malformed_health = malformed_health
        self.calls = []
        self.labels = dict(adapters.expected_ownership_labels(envelope))
        if foreign:
            self.labels["com.docker.compose.project"] = "foreign-project"

    def execute(self, argv, *, cwd, environment=None, timeout_seconds=30.0):
        key = tuple(argv)
        self.calls.append(key)
        kind = key[1]
        if key[:3] == ("docker", "image", "inspect"):
            return CommandResult(0, json.dumps([{
                "Id": "sha256:" + "9" * 64}]))
        if key[2] == "ls" and "-q" in key:
            if kind == "container":
                return CommandResult(0, "c1\nc2\nc3" + ("\nc4" if self.extra else ""))
            return CommandResult(0, "n1" if kind == "network" else "\n".join(
                f"v{i}" for i in range(6)))
        if key[2] == "inspect" and len(key) == 4 and key[3] in {
                self.envelope.network_identity,
                *(mount.volume_identity for mount in self.envelope.volume_mounts)}:
            return CommandResult(1, "")
        if key[2] != "inspect":
            raise AssertionError(key)
        if self.malformed:
            return CommandResult(0, "not-json")
        if kind == "container":
            services = ["loki", "prometheus", "runtime"]
            if self.extra:
                services.append("foreign-extra")
            values = []
            for index, service in enumerate(services):
                labels = {**self.labels,
                          "com.docker.compose.service": service,
                          "io.aiops.runtime.profile": self.envelope.runtime_image_profile}
                mounts = []
                if service == "runtime":
                    mounts = [{"Type": "volume", "Name": item.volume_identity,
                               "Destination": item.target}
                              for item in self.envelope.volume_mounts
                              if item.role in {"formal-logs", "runtime-events",
                                               "runtime-knowledge", "runtime-state"}]
                    if self.malformed_mount:
                        mounts.append({"Type": "volume"})
                status = self.runtime_status if service == "runtime" else "running"
                health = ({"Status": "unhealthy" if self.unhealthy
                           and service == "runtime" else "healthy"})
                if self.malformed_health and service == "runtime":
                    health = "healthy"
                values.append({
                    "Id": ("c1" if self.duplicate_id and index == 1
                           else f"c{index + 1}"),
                    "Name": (f"/{self.envelope.compose_project_identity}-loki-1"
                             if self.duplicate_name and index == 1 else
                             f"/{self.envelope.compose_project_identity}-{service}-1"),
                    "Image": "sha256:" + ("0" if self.wrong_image else "9") * 64,
                    "Config": {"Labels": labels, "Image": (
                        "foreign/runtime:wrong" if self.wrong_image and service == "runtime"
                        else {
                        "runtime": "aiops-spec011-runtime:formal",
                        "prometheus": "prom/prometheus:v2.51.0",
                        "loki": "grafana/loki:2.9.0",
                    }.get(service, "foreign:latest"))},
                    "State": {"Status": status, "Health": health},
                    "NetworkSettings": {"Networks": {
                        ("foreign-network" if self.wrong_network and service == "runtime"
                         else self.envelope.network_identity): {}}}, "Mounts": mounts})
        elif kind == "network":
            values = [{"Id": "n1", "Name": self.envelope.network_identity,
                       "Labels": self.labels}]
        else:
            values = [{"Name": mount.volume_identity, "Labels": self.labels}
                      for mount in self.envelope.volume_mounts]
        return CommandResult(0, json.dumps(values))


def _one_envelope(tmp_path):
    report, evaluation = _prepare(tmp_path)
    store = SqliteEvaluationStore(evaluation / "evaluation.db")
    topology = resolve_formal_compose_topology(
        store, report.preparation_id, ROOT)
    return store, topology.envelopes[0]


def test_docker_actual_state_inspector_projects_exact_owned_state(tmp_path):
    store, envelope = _one_envelope(tmp_path)
    try:
        inspected = adapters.DockerCliStateInspector(
            ROOT, executor=DockerInspectionExecutor(envelope),
            clock=lambda: "t1").inspect(envelope)
        inspected.validate()
        assert assess_exact_ownership(envelope, inspected).state.value == "CONTRADICTORY"
        assert project_health(envelope, inspected).operational_state.value == "CONTRADICTORY"
        assert {item.kind for item in inspected.resources} == {
            DockerResourceKind.CONTAINER, DockerResourceKind.NETWORK,
            DockerResourceKind.VOLUME}
        assert replace(inspected, inspected_at="t2").actual_resource_set_commitment == (
            inspected.actual_resource_set_commitment)
    finally:
        store.close()


@pytest.mark.parametrize("options", [
    {"foreign": True}, {"unhealthy": True}, {"extra": True},
])
def test_docker_inspector_observations_fail_existing_semantic_checks(
    tmp_path, options,
):
    store, envelope = _one_envelope(tmp_path)
    try:
        inspector = adapters.DockerCliStateInspector(
            ROOT, executor=DockerInspectionExecutor(envelope, **options))
        inspected = inspector.inspect(envelope)
        ownership = assess_exact_ownership(envelope, inspected)
        health = project_health(envelope, inspected)
        assert (ownership.state.value != "AVAILABLE"
                or health.operational_state.value != "AVAILABLE")
    finally:
        store.close()


def test_docker_inspector_malformed_output_fails_closed(tmp_path):
    store, envelope = _one_envelope(tmp_path)
    try:
        with pytest.raises(ValueError, match="malformed"):
            adapters.DockerCliStateInspector(
                ROOT, executor=DockerInspectionExecutor(
                    envelope, malformed=True)).inspect(envelope)
    finally:
        store.close()


def test_docker_inspector_wrong_image_is_observed_for_semantic_authority(tmp_path):
    store, envelope = _one_envelope(tmp_path)
    try:
        inspected = adapters.DockerCliStateInspector(
            ROOT, executor=DockerInspectionExecutor(
                envelope, wrong_image=True)).inspect(envelope)
        runtime = next(item for item in inspected.resources
                       if item.service == "runtime")
        assert runtime.image_reference == "foreign/runtime:wrong"
        assert assess_exact_ownership(
            envelope, inspected).state.value == "CONTRADICTORY"
    finally:
        store.close()


def test_docker_inspector_missing_resources_stays_unresolved(tmp_path):
    store, envelope = _one_envelope(tmp_path)
    class Missing:
        def execute(self, argv, *, cwd, environment=None, timeout_seconds=30.0):
            if tuple(argv)[:3] == ("docker", "image", "inspect"):
                return CommandResult(0, json.dumps([{
                    "Id": "sha256:" + "9" * 64}]))
            if tuple(argv)[2] == "ls" and "-q" in tuple(argv):
                return CommandResult(0, "")
            return CommandResult(1, "")
    try:
        inspected = adapters.DockerCliStateInspector(
            ROOT, executor=Missing()).inspect(envelope)
        assert not inspected.resources
        assert assess_exact_ownership(envelope, inspected).state.value == "UNRESOLVED"
    finally:
        store.close()


@pytest.mark.parametrize(("status", "expected"), [
    ("running", "RUNNING"), ("created", "PRESENT"), ("exited", "STOPPED"),
])
def test_docker_inspector_enumerates_all_container_states(
    tmp_path, status, expected,
):
    store, envelope = _one_envelope(tmp_path)
    executor = DockerInspectionExecutor(envelope, runtime_status=status)
    try:
        inspected = adapters.DockerCliStateInspector(
            ROOT, executor=executor).inspect(envelope)
        runtime = next(item for item in inspected.resources
                       if item.service == "runtime")
        assert runtime.state.value == expected
        assert runtime.observed_status == status
        container_listings = [call for call in executor.calls
                              if call[:3] == ("docker", "container", "ls")]
        assert container_listings
        assert all("--all" in call for call in container_listings)
    finally:
        store.close()


@pytest.mark.parametrize(("options", "message"), [
    ({"duplicate_id": True}, "duplicate identity"),
    ({"duplicate_name": True}, "duplicate resource identity"),
    ({"malformed_mount": True}, "mount"),
    ({"malformed_health": True}, "health"),
])
def test_docker_inspector_ambiguous_or_malformed_observation_fails_closed(
    tmp_path, options, message,
):
    store, envelope = _one_envelope(tmp_path)
    try:
        with pytest.raises(ValueError, match=message):
            adapters.DockerCliStateInspector(
                ROOT, executor=DockerInspectionExecutor(
                    envelope, **options)).inspect(envelope)
    finally:
        store.close()


def test_docker_inspector_observes_workspace_only_from_filesystem_evidence(
    tmp_path,
):
    store, envelope = _one_envelope(tmp_path)
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copy2(ROOT / "docker-compose.formal.yml",
                 repository / "docker-compose.formal.yml")
    try:
        absent = adapters.DockerCliStateInspector(
            repository, executor=DockerInspectionExecutor(envelope)).inspect(envelope)
        assert all(item.kind is not DockerResourceKind.WORKSPACE
                   for item in absent.resources)
        (repository / envelope.workspace_identity).mkdir(parents=True)
        present = adapters.DockerCliStateInspector(
            repository, executor=DockerInspectionExecutor(envelope)).inspect(envelope)
        workspace = next(item for item in present.resources
                         if item.kind is DockerResourceKind.WORKSPACE)
        assert workspace.state.value == "PRESENT"
        assert workspace.ownership_labels == ()
    finally:
        store.close()


def test_docker_network_mismatch_is_decided_by_existing_authority(tmp_path):
    store, envelope = _one_envelope(tmp_path)
    try:
        inspected = adapters.DockerCliStateInspector(
            ROOT, executor=DockerInspectionExecutor(
                envelope, wrong_network=True)).inspect(envelope)
        runtime = next(item for item in inspected.resources
                       if item.service == "runtime")
        assert runtime.networks == ("foreign-network",)
        assert assess_exact_ownership(
            envelope, inspected).state.value == "CONTRADICTORY"
    finally:
        store.close()


class FakeCandidateService:
    def __init__(self, child, *, validation_ok=True):
        self.child = child
        self.validation_ok = validation_ok
        self.build = "kbld_" + "a" * 64

    def stage(self, **kwargs):
        artifact = self.child / "candidate-c" / "index.bin"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"candidate-c-authoritative-output")
        return SimpleNamespace(
            record=SimpleNamespace(build_identity=self.build), failures=())

    def validate(self, *args, **kwargs):
        return SimpleNamespace(
            state=(BuildValidationState.VALIDATED if self.validation_ok
                   else BuildValidationState.FAILED),
            findings=(() if self.validation_ok else ("invalid",)))

    def activate(self, request):
        return SimpleNamespace(authority=SimpleNamespace(
            active_build_identity=self.build, generation=1))

    def recover_active(self):
        return SimpleNamespace(
            status=KnowledgeLocalReadiness.READY,
            active_build_identity=self.build, generation=1)


class FakeCandidateAuthority:
    def read_activation(self):
        return SimpleNamespace(status=KnowledgeReadStatus.NOT_FOUND, value=None)

    def local_readiness(self, **kwargs):
        return SimpleNamespace(status=KnowledgeLocalReadiness.READY)

    def get_staged_build(self, build_identity):
        return SimpleNamespace(status=KnowledgeReadStatus.FOUND)


def _candidate_request(tmp_path):
    report, evaluation = _prepare(tmp_path)
    store = SqliteEvaluationStore(evaluation / "evaluation.db")
    prepared = store.get_preparation(report.preparation_id)
    wiring = resolve_formal_container_wiring(
        store, report.preparation_id, ROOT, _environment={})
    child = evaluation / prepared["real_rag_child"]["relative_path"]
    manifest = admit_production_manifest(
        ROOT / "configs/knowledge_manifest.json", repository_root=ROOT)
    request = CandidateCPreparationRequest(
        report.preparation_id, report.planned_run_identity,
        prepared["real_rag_child"]["child_identity"], str(child),
        "spec017-real-rag-gate-v1", manifest.manifest_commitment,
        manifest.manifest.corpus_id,
        manifest.manifest.canonical_release_reference,
        execution_mounts=wiring.mounts)
    return store, child, request


def test_candidate_c_bridge_uses_stage_validate_activate_and_exact_children(tmp_path):
    store, child, request = _candidate_request(tmp_path)
    authority = FakeCandidateAuthority()
    config = load_knowledge_config(ROOT / "configs/knowledge_index.yaml")
    service = FakeCandidateService(child)
    adapter = adapters.KnowledgeIndexCandidateCPreparationOperations(
        ROOT, factory=lambda _: (service, authority, config))
    try:
        adapter.prepare_exact(request)
        assert adapter.authority.read_activation().status is KnowledgeReadStatus.NOT_FOUND
        for mount in request.execution_mounts:
            execution = adapters._fs_path(
                child.parent.parent / mount.child_source_reference)
            assert json.loads((execution / PREPARED_CHILD_MARKER).read_text(
                encoding="utf-8")) == mount.expected_marker()
            assert (execution / "index.bin").is_file()
        with pytest.raises(ValueError, match="unexpected state"):
            resolve_formal_container_wiring(
                store, request.preparation_id, ROOT, _environment={})
        artifacts = _artifact_manifest(child)
        assert artifacts == {
            "candidate-c/index.bin":
                "sha256:" + hashlib.sha256(
                    b"candidate-c-authoritative-output").hexdigest()}
        assert resolve_formal_container_wiring(
            store, request.preparation_id, ROOT, _environment={},
            _preparation_artifacts=artifacts).mounts
        prepared = store.get_preparation(request.preparation_id)
        facts = _facts(prepared, overrides={
            PreparationResourceDomain.REAL_RAG_BUILD: service.build})
        payload = _prepared_resources_payload(
            prepared, facts=facts, artifacts=artifacts,
            corpus_identity=next(
                item.expected_target_identity for item in facts.facts
                if item.domain is PreparationResourceDomain.REAL_RAG_BUILD))
        store._record_prepared_resources(request.preparation_id, payload)
        assert verify_prepared_real_rag_child(
            store, request.preparation_id, ROOT)["build_identity"] == service.build
    finally:
        store.close()


def test_candidate_c_validation_failure_never_activates(tmp_path):
    store, child, request = _candidate_request(tmp_path)
    config = load_knowledge_config(ROOT / "configs/knowledge_index.yaml")
    service = FakeCandidateService(child, validation_ok=False)
    try:
        with pytest.raises(RuntimeError, match="validation failed"):
            adapters.KnowledgeIndexCandidateCPreparationOperations(
                ROOT, factory=lambda _: (
                    service, FakeCandidateAuthority(), config)).prepare_exact(request)
    finally:
        store.close()


class ContainerExecutor:
    def __init__(self, mount, project, *, root=ROOT, ids="container-1",
                 child_source=None, corpus_source=None, marker=None, omit_artifact=False,
                 wrong_artifact_hash=False, duplicate_mount=False,
                 malformed_mount=False, wrong_corpus_hash=False):
        self.mount = mount
        self.project = project
        self.root = Path(root)
        self.ids = ids
        self.child_source = child_source
        self.corpus_source = corpus_source
        self.marker = marker
        self.omit_artifact = omit_artifact
        self.wrong_artifact_hash = wrong_artifact_hash
        self.duplicate_mount = duplicate_mount
        self.malformed_mount = malformed_mount
        self.wrong_corpus_hash = wrong_corpus_hash
        self.calls = []

    def execute(self, argv, *, cwd, environment=None, timeout_seconds=30.0):
        key = tuple(argv)
        self.calls.append(key)
        if key[:4] == ("docker", "container", "ls", "-q"):
            return CommandResult(0, self.ids)
        if key[:4] == ("docker", "container", "inspect", "container-1"):
            values = [{
                "Config": {"Labels": {
                    "com.docker.compose.project": self.project,
                    "com.docker.compose.service": "runtime"}},
                "Mounts": [
                    {"Type": "bind", "Destination": self.mount.child_container_target,
                     "Source": (self.child_source or str(
                         self.root / "var/rca_evaluation"
                         / self.mount.child_source_reference)),
                     "RW": True},
                    {"Type": "bind", "Destination": self.mount.corpus_container_target,
                     "Source": (self.corpus_source or str(
                         self.root / self.mount.corpus_source_reference)),
                     "RW": False},
                ],
            }]
            if self.duplicate_mount:
                values[0]["Mounts"].append(dict(values[0]["Mounts"][0]))
            if self.malformed_mount:
                values[0]["Mounts"].append({"Type": "bind"})
            return CommandResult(0, json.dumps(values))
        if key[3:5] == ("cat", f"{self.mount.child_container_target}/{PREPARED_CHILD_MARKER}"):
            if self.marker is False:
                return CommandResult(1, "")
            return CommandResult(0, json.dumps(
                self.mount.expected_marker() if self.marker is None else self.marker))
        if key[3:5] == ("python", "-c"):
            return CommandResult(0, "AVAILABLE")
        if key[3] == "find":
            if key[4] == self.mount.child_container_target:
                child = adapters._fs_path(
                    self.root / "var/rca_evaluation"
                    / self.mount.child_source_reference)
                names = [path.relative_to(child).as_posix()
                         for path in child.rglob("*") if path.is_file()]
                if self.omit_artifact:
                    names = [name for name in names if name == PREPARED_CHILD_MARKER]
                return CommandResult(0, "\n".join(names))
            manifest = admit_production_manifest(
                ROOT / "configs/knowledge_manifest.json", repository_root=ROOT)
            return CommandResult(0, "\n".join(
                item.source_path for item in manifest.manifest.documents))
        if key[3] == "sha256sum":
            name = Path(key[4]).name
            if key[4].startswith(self.mount.child_container_target + "/"):
                relative = key[4][len(self.mount.child_container_target) + 1:]
                child = adapters._fs_path(
                    self.root / "var/rca_evaluation"
                    / self.mount.child_source_reference)
                digest = hashlib.sha256((child / relative).read_bytes()).hexdigest()
                if self.wrong_artifact_hash:
                    digest = "0" * 64
                return CommandResult(0, f"{digest}  {key[4]}")
            manifest = admit_production_manifest(
                ROOT / "configs/knowledge_manifest.json", repository_root=ROOT)
            digest = next(item.expected_content_hash for item in manifest.manifest.documents
                          if item.source_path == name)
            if self.wrong_corpus_hash:
                digest = "0" * 64
            return CommandResult(0, f"{digest}  {key[4]}")
        raise AssertionError(key)


def _mounted_target(tmp_path, monkeypatch):
    store, envelope = _one_envelope(tmp_path)
    wiring = resolve_formal_container_wiring(
        store, envelope.preparation_id, ROOT, _environment={})
    mount = wiring.mounts[0]
    inspection_root = tmp_path / "repository"
    shutil.copytree(ROOT / "configs", inspection_root / "configs")
    shutil.copytree(ROOT / "docs/knowledge", inspection_root / "docs/knowledge")
    host_child = adapters._fs_path(
        inspection_root / "var/rca_evaluation" / mount.child_source_reference)
    host_child.mkdir(parents=True)
    (host_child / PREPARED_CHILD_MARKER).write_text(
        json.dumps(mount.expected_marker()), encoding="utf-8")
    (host_child / "index.bin").write_bytes(b"candidate-c-index")
    allocation = mount.child_source_reference.split("/executions/", 1)[0]
    authoritative = adapters._fs_path(
        inspection_root / "var/rca_evaluation" / allocation / "candidate-c")
    authoritative.mkdir()
    (authoritative / "index.bin").write_bytes(b"candidate-c-index")
    admitted_manifest = admit_production_manifest(
        ROOT / "configs/knowledge_manifest.json", repository_root=ROOT)
    monkeypatch.setattr(
        knowledge_index, "admit_production_manifest",
        lambda *args, **kwargs: admitted_manifest)
    target = ContainerRealRagInspectionTarget(
        envelope.preparation_id, envelope.compose_project_identity, mount)
    return store, envelope, mount, inspection_root, target


def test_container_inspector_reads_actual_target_and_reuses_resolver(
    tmp_path, monkeypatch,
):
    store, envelope, mount, inspection_root, target = _mounted_target(
        tmp_path, monkeypatch)
    try:
        captured = {}
        sentinel = object()
        def resolve(observed_mount, **kwargs):
            captured.update(kwargs)
            assert observed_mount is mount
            marker = (Path(kwargs["container_root"])
                      / mount.child_container_target.lstrip("/")
                      / PREPARED_CHILD_MARKER)
            assert json.loads(marker.read_text(encoding="utf-8")) == mount.expected_marker()
            return sentinel
        monkeypatch.setattr(adapters, "resolve_container_real_rag_readiness", resolve)
        result = adapters.DockerContainerRealRagInspector(
            inspection_root, object(), executor=ContainerExecutor(
                mount, envelope.compose_project_identity, root=inspection_root),
               clock=lambda: "t1").inspect(target)
        assert result is sentinel
        assert captured["project_identity"] == envelope.compose_project_identity
        assert captured["_environment"] == {"GEMINI_API_KEY": "PRESENT"}
    finally:
        store.close()


@pytest.mark.parametrize(("options", "message"), [
    ({"child_source": "C:/unrelated/root/real_rag/same-child"}, "source"),
    ({"omit_artifact": True}, "artifact set"),
    ({"wrong_artifact_hash": True}, "artifact hash"),
    ({"duplicate_mount": True}, "ambiguous"),
    ({"malformed_mount": True}, "malformed"),
])
def test_container_inspector_mount_and_candidate_artifacts_fail_closed(
    tmp_path, monkeypatch, options, message,
):
    store, envelope, mount, root, target = _mounted_target(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match=message):
            adapters.DockerContainerRealRagInspector(
                root, object(), executor=ContainerExecutor(
                    mount, envelope.compose_project_identity, root=root,
                    **options)).inspect(target)
    finally:
        store.close()


def test_container_inspector_binds_execution_bytes_to_candidate_c_source(
    tmp_path, monkeypatch,
):
    store, envelope, mount, root, target = _mounted_target(tmp_path, monkeypatch)
    execution = adapters._fs_path(
        root / "var/rca_evaluation" / mount.child_source_reference)
    (execution / "index.bin").write_bytes(b"different-candidate-c-index")
    try:
        with pytest.raises(ValueError, match="execution artifact hash"):
            adapters.DockerContainerRealRagInspector(
                root, object(), executor=ContainerExecutor(
                    mount, envelope.compose_project_identity,
                    root=root)).inspect(target)
    finally:
        store.close()


def test_container_inspector_rejects_same_suffix_from_different_host_root(
    tmp_path, monkeypatch,
):
    store, envelope, mount, root, target = _mounted_target(tmp_path, monkeypatch)
    wrong = tmp_path / "unrelated" / mount.child_source_reference
    try:
        with pytest.raises(ValueError, match="source"):
            adapters.DockerContainerRealRagInspector(
                root, object(), executor=ContainerExecutor(
                    mount, envelope.compose_project_identity, root=root,
                    child_source=str(wrong))).inspect(target)
    finally:
        store.close()


def test_container_inspector_rejects_wrong_child_or_corpus_source(
    tmp_path, monkeypatch,
):
    store, envelope, mount, root, target = _mounted_target(tmp_path, monkeypatch)
    wrong_child = (root / "var/rca_evaluation/real_rag/wrong-child"
                   / Path(mount.child_source_reference).name)
    wrong_corpus = tmp_path / "other-repository" / mount.corpus_source_reference
    try:
        for options in ({"child_source": str(wrong_child)},
                        {"corpus_source": str(wrong_corpus)}):
            with pytest.raises(ValueError, match="source"):
                adapters.DockerContainerRealRagInspector(
                    root, object(), executor=ContainerExecutor(
                        mount, envelope.compose_project_identity, root=root,
                        **options)).inspect(target)
    finally:
        store.close()


def test_container_inspector_missing_or_wrong_marker_fails_closed(
    tmp_path, monkeypatch,
):
    store, envelope, mount, root, target = _mounted_target(tmp_path, monkeypatch)
    try:
        missing = ContainerExecutor(
            mount, envelope.compose_project_identity, root=root, marker=False)
        with pytest.raises(CommandExecutionError):
            adapters.DockerContainerRealRagInspector(
                root, object(), executor=missing).inspect(target)

        def reject_marker(observed_mount, **kwargs):
            marker = (Path(kwargs["container_root"])
                      / observed_mount.child_container_target.lstrip("/")
                      / PREPARED_CHILD_MARKER)
            if json.loads(marker.read_text(encoding="utf-8")) != (
                    observed_mount.expected_marker()):
                raise ValueError("ownership marker is contradictory")
            return object()
        monkeypatch.setattr(
            adapters, "resolve_container_real_rag_readiness", reject_marker)
        wrong = ContainerExecutor(
            mount, envelope.compose_project_identity, root=root,
            marker={"wrong": "identity"})
        with pytest.raises(ValueError, match="marker"):
            adapters.DockerContainerRealRagInspector(
                root, object(), executor=wrong).inspect(target)
    finally:
        store.close()


def test_container_inspector_corpus_hash_mismatch_is_not_hidden(
    tmp_path, monkeypatch,
):
    store, envelope, mount, root, target = _mounted_target(tmp_path, monkeypatch)
    try:
        def reject_corpus(observed_mount, **kwargs):
            corpus = (Path(kwargs["container_root"])
                      / observed_mount.corpus_container_target.lstrip("/"))
            manifest = admit_production_manifest(
                ROOT / "configs/knowledge_manifest.json", repository_root=ROOT)
            for document in manifest.manifest.documents:
                if hashlib.sha256((corpus / document.source_path).read_bytes()).hexdigest() \
                        != document.expected_content_hash:
                    raise ValueError("corpus hash is contradictory")
            return object()
        monkeypatch.setattr(
            adapters, "resolve_container_real_rag_readiness", reject_corpus)
        with pytest.raises(ValueError, match="corpus hash"):
            adapters.DockerContainerRealRagInspector(
                root, object(), executor=ContainerExecutor(
                    mount, envelope.compose_project_identity, root=root,
                    wrong_corpus_hash=True)).inspect(target)
    finally:
        store.close()


def test_container_inspection_commands_are_read_only(tmp_path, monkeypatch):
    store, envelope, mount, root, target = _mounted_target(tmp_path, monkeypatch)
    executor = ContainerExecutor(
        mount, envelope.compose_project_identity, root=root)
    monkeypatch.setattr(
        adapters, "resolve_container_real_rag_readiness", lambda *args, **kwargs: object())
    try:
        adapters.DockerContainerRealRagInspector(
            root, object(), executor=executor).inspect(target)
        assert all(call[:3] in {
            ("docker", "container", "ls"),
            ("docker", "container", "inspect"),
            ("docker", "exec", "container-1"),
        } for call in executor.calls)
        assert not any(token in {"cp", "chmod", "rm", "mkdir", "touch"}
                       for call in executor.calls for token in call)
    finally:
        store.close()


def test_container_inspector_rejects_missing_or_ambiguous_container(tmp_path):
    store, envelope = _one_envelope(tmp_path)
    try:
        mount = resolve_formal_container_wiring(
            store, envelope.preparation_id, ROOT, _environment={}).mounts[0]
        target = ContainerRealRagInspectionTarget(
            envelope.preparation_id, envelope.compose_project_identity, mount)
        for ids in ("", "one\ntwo"):
            with pytest.raises(ValueError, match="missing or ambiguous"):
                adapters.DockerContainerRealRagInspector(
                    ROOT, object(), executor=ContainerExecutor(
                        mount, envelope.compose_project_identity,
                        ids=ids)).inspect(target)
    finally:
        store.close()


def test_production_composition_is_explicit_and_injects_concrete_adapters(
    tmp_path, monkeypatch,
):
    with pytest.raises(PermissionError, match="explicit"):
        adapters.prepare_production_resources(
            store=object(), preparation_id="unused", repository_root=ROOT,
            explicitly_authorized=False)
    captured = {}
    monkeypatch.setattr(adapters, "prepare_authoritative_resources",
                        lambda **kwargs: captured.update(kwargs) or "facts")
    monkeypatch.setattr(
        adapters.KnowledgeIndexCandidateCPreparationOperations, "close", lambda self: None)
    result = adapters.prepare_production_resources(
        store=object(), preparation_id="prepared", repository_root=ROOT,
        explicitly_authorized=True, executor=RecordingExecutor())
    assert result == "facts"
    assert isinstance(captured["runtime_image_operations"],
                      adapters.DockerRuntimeImagePreparationOperations)
    assert isinstance(captured["docker_operations"],
                      adapters.ComposePreparationDockerOperations)
    assert isinstance(captured["docker_state_inspector"],
                      adapters.DockerCliStateInspector)
    assert isinstance(captured["candidate_c_operations"],
                      adapters.KnowledgeIndexCandidateCPreparationOperations)
    assert isinstance(captured["container_real_rag_inspector"],
                      adapters.DockerContainerRealRagInspector)
