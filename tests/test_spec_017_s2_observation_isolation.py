"""Focused S2 public observation mapping and isolation tests."""

from __future__ import annotations

import base64
import inspect
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from incident_management.contracts import IncidentStatus
from knowledge_index.contracts import KnowledgeReadResult, KnowledgeReadStatus
from llm_generation.contracts import LocalReadStatus
from rca_persistence.contracts import LogicalTryIdentity, LogicalTryResultKind

import rca_evaluation.observation as observation_module
from rca_evaluation.authority import PM_AUTHORITY, TrustedApprovalRegistry, TrustedOracleApproval
from rca_evaluation.identity import commitment, identity
from rca_evaluation.isolation import (
    ActualOutboundPins,
    AuthoritativeOutboundResolver,
    GroundTruthIsolationGuard,
    IsolationViolation,
    OutboundBasis,
    OutboundChannel,
    SanitizedOutboundManifest,
    assert_no_production_dependency_on_evaluation,
)
from rca_evaluation.inventory import (
    PRODUCTION_SENSITIVE_INVENTORY,
    SourceClassification,
    SensitiveSurface,
    validate_production_sensitive_inventory,
)
from rca_evaluation.ledger import LedgerRecord
from rca_evaluation.observation import (
    CapturedInputBoundary,
    ObservationDetail,
    ObservationOutcome,
    PublicObservationResolver,
)
from rca_evaluation.oracle import EvaluationPlan, OracleRevision
from rca_evaluation.sqlite_store import SqliteEvaluationStore
from rca_evaluation.workspace import (
    ExecutionWorkspace,
    ProductionRoot,
    ProductionRootKind,
    TrustedIsolationPolicy,
)


def captured(label="one"):
    return CapturedInputBoundary(
        identity("execution", ["test", label]), "EVT-1", commitment({"event_id": "EVT-1"}),
    )


def isolation_policy(tmp_path, **overrides):
    roots = {
        kind: tmp_path / "production" / f"{kind.value.lower()}.db"
        for kind in ProductionRootKind
    }
    roots.update({ProductionRootKind[name]: value for name, value in overrides.items()})
    return TrustedIsolationPolicy(
        tuple(ProductionRoot(kind, roots[kind]) for kind in ProductionRootKind),
        authority_reference="SPEC-017-production-root-inventory",
    )


class IncidentReads:
    def __init__(self):
        self.owner = True
        self.records = {
            status: () for status in IncidentStatus
        }
        self.incident = SimpleNamespace(incident_id="INC-1", event_ids=("EVT-1",))
        self.records[IncidentStatus.OPEN] = (self.incident,)
        self.relationship = SimpleNamespace(incident_id="INC-1", current_version_id="VER-1")

    @property
    def connection(self):
        raise AssertionError("private connection access is forbidden")

    def event_has_incident_owner(self, event_id):
        assert event_id == "EVT-1"
        return self.owner

    def list_incidents_by_workflow_status(self, status):
        return self.records[status]

    def get_incident(self, incident_id):
        return self.incident if incident_id == "INC-1" else None

    def get_rca_relationship(self, incident_id):
        return self.relationship if incident_id == "INC-1" else None


class RcaReads:
    def __init__(self):
        try_identity = LogicalTryIdentity("ATT-1", 1)
        lineage = SimpleNamespace(
            attempt_id="ATT-1", aggregate_id="AGG-1",
            evidence_snapshot_id="ES-1", evidence_revision_id="ER-1",
            knowledge_snapshot_id="KS-1",
        )
        self.outcome = SimpleNamespace(
            identity=try_identity, result_kind=LogicalTryResultKind.VALIDATED_RESULT,
            validated_result_id="DVR-1",
        )
        self.lineage_read = SimpleNamespace(
            attempt=SimpleNamespace(lineage=lineage), try_outcomes=(self.outcome,),
        )
        self.aggregate = SimpleNamespace(aggregate_id="AGG-1", incident_id="INC-1")
        self.version = SimpleNamespace(
            version_id="VER-1", aggregate_id="AGG-1", attempt_id="ATT-1",
        )
        self.current = SimpleNamespace(
            current=SimpleNamespace(aggregate_id="AGG-1"), version=self.version,
        )
        self.provenance = SimpleNamespace(
            evidence_snapshot_id="ES-1", evidence_revision_id="ER-1",
            knowledge_snapshot_id="KS-1",
        )

    def get_aggregate_by_incident(self, incident_id):
        return self.aggregate if incident_id == "INC-1" else None

    def get_current(self, aggregate_id):
        return self.current if aggregate_id == "AGG-1" else None

    def get_version_history(self, aggregate_id):
        return (self.version,) if aggregate_id == "AGG-1" else ()

    def get_artifact_provenance(self, version_id):
        return self.provenance if version_id == "VER-1" else None

    def get_attempt_lineage(self, attempt_id):
        return self.lineage_read if attempt_id == "ATT-1" else None


class EvidenceReads:
    def __init__(self):
        self.snapshot_content = '{"evidence":"ES-1"}'

    def resolve_snapshot(self, snapshot_id):
        if snapshot_id != "ES-1":
            return None
        return SimpleNamespace(
            snapshot_id="ES-1", revision_id="ER-1", incident_id="INC-1",
            canonical_snapshot_content=self.snapshot_content,
        )

    def resolve_revision(self, revision_id):
        if revision_id != "ER-1":
            return None
        return SimpleNamespace(
            revision_id="ER-1", incident_id="INC-1",
            integrity_identity="spec013-integrity-ER-1",
        )


class KnowledgeReads:
    def __init__(self):
        self.snapshot_commitment = commitment({"knowledge": "KS-1"})

    def read_snapshot(self, key):
        assert key.value == "KS-1"
        return KnowledgeReadResult(KnowledgeReadStatus.FOUND, SimpleNamespace(
            snapshot_key=key, snapshot_commitment=self.snapshot_commitment,
        ))

    def read_provenance(self, key):
        assert key.value == "KS-1"
        return KnowledgeReadResult(KnowledgeReadStatus.FOUND, SimpleNamespace(
            snapshot_identity=key, snapshot_commitment=self.snapshot_commitment,
        ))


class GenerationReads:
    def __init__(self, rca):
        source = SimpleNamespace(
            try_identity=rca.outcome.identity, evidence_snapshot_id="ES-1",
            evidence_revision_id="ER-1", knowledge_snapshot_id="KS-1",
        )
        self.value = SimpleNamespace(
            validated_result_id="DVR-1",
            content=SimpleNamespace(input=source),
        )

    def result(self, result_id):
        if result_id != "DVR-1":
            return SimpleNamespace(status=LocalReadStatus.NOT_FOUND, value=None)
        return SimpleNamespace(status=LocalReadStatus.FOUND, value=self.value)


def full_environment():
    incidents = IncidentReads()
    rca = RcaReads()
    evidence = EvidenceReads()
    knowledge = KnowledgeReads()
    resolver = PublicObservationResolver(
        incidents, rca, evidence, knowledge, GenerationReads(rca),
    )
    return incidents, rca, evidence, knowledge, resolver


def environment():
    incidents, rca, _, _, resolver = full_environment()
    return incidents, rca, resolver


def evaluation_semantics():
    return {
        "scenarios": {
            f"S{i}": {f"O{j}": f"approved-{i}-{j}" for j in range(1, 11)}
            for i in range(1, 7)
        },
        "cross_scenario_rules": ["approved comparison rule"],
    }


def add_execution(store, admitted_plan, label):
    run = store.append(LedgerRecord.create(
        "evaluation_run", [admitted_plan.evaluation_plan_id, label],
        {"oracle_revision_id": admitted_plan.oracle_revision_id},
        admitted_plan.evaluation_plan_id,
    ))
    execution = store.append(LedgerRecord.create(
        "execution", [run.record_id, "S1", 1],
        {"scenario_id": "S1", "repetition": 1}, run.record_id,
    ))
    return run, execution


@pytest.fixture
def outbound_context(tmp_path):
    revision = OracleRevision.approved(
        oracle_set_id="PM-S1-S6", semantics=evaluation_semantics(),
        created_at="2026-10-02T00:00:00Z", approval_authority=PM_AUTHORITY,
        approval_reference="PM-017-S2", approved_at="2026-10-02T01:00:00Z",
    )
    approvals = TrustedApprovalRegistry((TrustedOracleApproval(
        revision.approval_reference, PM_AUTHORITY, revision.oracle_revision_id,
        revision.semantic_content_commitment, revision.approved_at,
        revision.parent_revision_id,
    ),))
    store = SqliteEvaluationStore(
        tmp_path / "authority" / "rca_evaluation" / "evaluation.db",
        approvals=approvals,
    )
    store.admit_oracle(revision)
    admitted_plan = EvaluationPlan.admit(
        revision, fixture_revision="fixture-r1", production_build="build-a",
        model="provider/model/profile-v1", prompt="prompt-v1", schema="schema-v1",
        corpus_index="corpus-v1", configuration="config-v1", repetitions=2,
    )
    store.admit_plan(admitted_plan)
    run, execution = add_execution(store, admitted_plan, "run-primary")
    incidents, rca, evidence, knowledge, observations = full_environment()
    authority = AuthoritativeOutboundResolver(store, observations)
    guard = GroundTruthIsolationGuard(
        authority,
        protected_values=("sha256:oracle-secret",),
        canary_commitments=("sha256:secret-canary",),
    )
    context = SimpleNamespace(
        revision=revision, store=store, plan=admitted_plan, run=run,
        execution=execution,
        captured=CapturedInputBoundary(
            execution.record_id, "EVT-1", commitment({"event_id": "EVT-1"}),
        ),
        incidents=incidents, rca=rca, evidence=evidence, knowledge=knowledge,
        observations=observations, authority=authority, guard=guard,
    )
    try:
        yield context
    finally:
        store.close()


def test_public_only_unique_event_to_full_rca_lineage():
    _, _, resolver = environment()
    result = resolver.resolve(captured())
    assert result.outcome is ObservationOutcome.RESOLVED
    assert result.detail is ObservationDetail.COMPLETE
    assert result.observation.event_id == "EVT-1"
    assert result.observation.incident_id == "INC-1"
    assert result.observation.aggregate_id == "AGG-1"
    assert result.observation.current_version_id == "VER-1"
    assert result.observation.attempt_id == "ATT-1"
    assert result.observation.try_ordinal == 1
    assert result.observation.validated_result_id == "DVR-1"
    assert result.observation.evidence_snapshot_id == "ES-1"
    assert result.observation.evidence_revision_id == "ER-1"
    assert result.observation.knowledge_snapshot_id == "KS-1"

    source = inspect.getsource(observation_module)
    assert "sqlite" not in source.lower()
    assert "timestamp" not in source.lower()
    assert "scenario_id" not in source
    assert not any(token in source for token in ("._db", "._connection", ".execute("))


def test_ambiguous_event_mapping_fails_closed_without_order_inference():
    incidents, _, resolver = environment()
    incidents.records[IncidentStatus.ASSIGNED] = (
        SimpleNamespace(incident_id="INC-2", event_ids=("EVT-1",)),
    )
    result = resolver.resolve(captured())
    assert result.outcome is ObservationOutcome.INVALID_OBSERVATION
    assert result.detail is ObservationDetail.CONTRADICTORY
    assert result.observation is None


def test_missing_unavailable_corrupt_and_contradictory_are_distinct():
    incidents, _, resolver = environment()
    incidents.owner = False
    incidents.records[IncidentStatus.OPEN] = ()
    assert resolver.resolve(captured()).detail is ObservationDetail.MISSING

    incidents, _, resolver = environment()
    incidents.event_has_incident_owner = lambda _: (_ for _ in ()).throw(TimeoutError())
    unavailable = resolver.resolve(captured())
    assert unavailable.outcome is ObservationOutcome.SYSTEM_FAILURE
    assert unavailable.detail is ObservationDetail.UNAVAILABLE

    incidents, _, resolver = environment()
    incidents.owner = "yes"
    corrupt = resolver.resolve(captured())
    assert corrupt.outcome is ObservationOutcome.INVALID_OBSERVATION
    assert corrupt.detail is ObservationDetail.CORRUPT

    incidents, _, resolver = environment()
    incidents.records[IncidentStatus.OPEN] = ()
    contradictory = resolver.resolve(captured())
    assert contradictory.outcome is ObservationOutcome.INVALID_OBSERVATION
    assert contradictory.detail is ObservationDetail.CONTRADICTORY


def test_per_execution_workspace_is_evaluation_only_and_append_once(tmp_path):
    production = tmp_path / "production" / "events.jsonl"
    production.parent.mkdir()
    production.write_text("production-authority\n", encoding="utf-8")
    root = tmp_path / "rca_evaluation"
    first = ExecutionWorkspace.create(
        root, captured("first"), scenario_id="S1", canary_commitment="sha256:canary-1",
        isolation_policy=isolation_policy(tmp_path),
    )
    second = ExecutionWorkspace.create(
        root, captured("second"), scenario_id="S2", canary_commitment="sha256:canary-2",
        isolation_policy=isolation_policy(tmp_path),
    )
    assert first.path != second.path
    assert first.path.parent == second.path.parent == root / "workspaces"
    assert first.write_once("observation", {"status": "captured"}) == first.write_once(
        "observation", {"status": "captured"},
    )
    with pytest.raises(ValueError, match="contradictory"):
        first.write_once("observation", {"status": "different"})
    with pytest.raises(ValueError, match="overlaps"):
        overlapping = isolation_policy(
            tmp_path,
            **{ProductionRootKind.RUNTIME.value: tmp_path / "nested"},
        )
        ExecutionWorkspace.create(
            tmp_path / "nested" / "rca_evaluation", captured("third"), scenario_id="S3",
            canary_commitment="sha256:canary-3",
            isolation_policy=overlapping,
        )
    assert production.read_text(encoding="utf-8") == "production-authority\n"


@pytest.mark.parametrize("payload", [
    {"scenarioId": "S1"},
    {"metadata": base64.b64encode(b'{"ground_truth":"answer"}').decode("ascii")},
    {"metadata": "65787065637465645f726f6f745f6361757365"},
    {"metadata": "eval:evaluation_run:" + "a" * 64},
    {"metadata": "sha256:secret-canary"},
])
def test_runtime_outbound_guard_rejects_alias_encoded_identity_and_canary(
    payload, outbound_context,
):
    with pytest.raises(IsolationViolation):
        outbound_context.guard.commit_outbound(
            outbound_context.plan.evaluation_plan_id, outbound_context.captured,
            OutboundChannel.GENERATION_PROMPT, payload,
        )


def test_valid_authoritative_basis_is_resolved_and_reverified(outbound_context):
    context = outbound_context
    payload = {"event_id": "EVT-1", "evidence_snapshot_id": "ES-1"}
    manifests = tuple(
        context.guard.admit_outbound(
            context.plan.evaluation_plan_id, context.captured, channel, payload,
        )
        for channel in OutboundChannel
    )
    assert {item.basis.channel for item in manifests} == set(OutboundChannel)
    assert {item.outbound_payload_commitment for item in manifests} == {commitment(payload)}
    basis = manifests[0].basis
    assert basis.evaluation_plan_id == context.plan.evaluation_plan_id
    assert basis.evaluation_run_id == context.run.record_id
    assert basis.execution_id == context.execution.record_id
    assert basis.oracle_revision_id == context.revision.oracle_revision_id
    assert basis.production_prompt_identity == context.plan.prompt
    assert basis.provider_identity == context.plan.model
    assert basis.evidence_snapshot_id == "ES-1"
    assert basis.knowledge_snapshot_id == "KS-1"
    verified = context.guard.verify_before_invocation(
        manifests[0], context.captured, ActualOutboundPins.from_admitted(manifests[0]), payload,
    )
    assert verified.canonical_payload == b'{"event_id":"EVT-1","evidence_snapshot_id":"ES-1"}'


def test_unadmitted_plan_and_execution_from_different_plan_fail_closed(outbound_context):
    context = outbound_context
    forged_plan = identity("evaluation_plan", ["forged"])
    with pytest.raises(IsolationViolation, match="not admitted"):
        context.guard.admit_outbound(
            forged_plan, context.captured, OutboundChannel.GENERATION_PROMPT, {},
        )
    other_plan = EvaluationPlan.admit(
        context.revision, fixture_revision="fixture-r1", production_build="build-a",
        model="provider/model/profile-v1", prompt="prompt-other", schema="schema-v1",
        corpus_index="corpus-v1", configuration="config-v1", repetitions=2,
    )
    context.store.admit_plan(other_plan)
    _, other_execution = add_execution(context.store, other_plan, "run-other")
    other_captured = CapturedInputBoundary(
        other_execution.record_id, "EVT-1", commitment({"event_id": "EVT-1"}),
    )
    with pytest.raises(IsolationViolation, match="does not belong"):
        context.guard.admit_outbound(
            context.plan.evaluation_plan_id, other_captured,
            OutboundChannel.GENERATION_PROMPT, {},
        )


@pytest.mark.parametrize("drift", [
    "channel", "prompt", "evidence", "knowledge", "config", "provider", "payload",
])
def test_authority_pin_or_payload_drift_fails_before_invocation(drift, outbound_context):
    context = outbound_context
    payload = {"prompt": "approved", "evidence_snapshot_id": "ES-1"}
    admitted = context.guard.admit_outbound(
        context.plan.evaluation_plan_id, context.captured,
        OutboundChannel.GENERATION_PROMPT, payload,
    )
    actual_payload = dict(payload)
    actual_pins = ActualOutboundPins.from_admitted(admitted)
    if drift == "channel":
        actual_pins = replace(actual_pins, channel=OutboundChannel.EVIDENCE_QUERY)
    elif drift == "prompt":
        actual_pins = replace(actual_pins, production_prompt_identity="prompt-forged")
    elif drift == "evidence":
        actual_pins = replace(
            actual_pins, evidence_snapshot_id="ES-UNRELATED",
            evidence_snapshot_commitment=commitment({"evidence": "unrelated"}),
        )
    elif drift == "knowledge":
        actual_pins = replace(
            actual_pins, knowledge_snapshot_id="KS-UNRELATED",
            knowledge_snapshot_commitment=commitment({"knowledge": "unrelated"}),
        )
    elif drift == "config":
        actual_pins = replace(
            actual_pins, non_secret_config_commitment=commitment({"configuration": "forged"}),
        )
    elif drift == "provider":
        actual_pins = replace(actual_pins, provider_identity="provider/forged")
    else:
        actual_payload["prompt"] = "mutated-after-admission"
    invocations = []
    with pytest.raises(IsolationViolation, match="drifted"):
        verified = context.guard.verify_before_invocation(
            admitted, context.captured, actual_pins, actual_payload,
        )
        invocations.append(verified.canonical_payload)
    assert invocations == []


def test_caller_self_consistent_basis_has_no_admission_path(outbound_context):
    context = outbound_context
    authorized = context.authority.resolve(
        context.plan.evaluation_plan_id, context.captured,
        OutboundChannel.GENERATION_PROMPT,
    )
    unauthorized = replace(
        authorized, evidence_snapshot_id="ES-UNRELATED",
        evidence_snapshot_commitment=commitment({"evidence": "unrelated"}),
    )
    assert isinstance(unauthorized, OutboundBasis)
    with pytest.raises(TypeError):
        context.guard.admit_outbound(unauthorized, {"self_consistent": True})


@pytest.mark.parametrize("lineage", ["evidence", "knowledge"])
def test_unrelated_public_snapshot_fails_closed(lineage, outbound_context):
    context = outbound_context
    source = context.observations._generation.value.content.input
    if lineage == "evidence":
        context.rca.lineage_read.attempt.lineage.evidence_snapshot_id = "ES-UNRELATED"
        context.rca.provenance.evidence_snapshot_id = "ES-UNRELATED"
        source.evidence_snapshot_id = "ES-UNRELATED"
    else:
        context.rca.lineage_read.attempt.lineage.knowledge_snapshot_id = "KS-UNRELATED"
        context.rca.provenance.knowledge_snapshot_id = "KS-UNRELATED"
        source.knowledge_snapshot_id = "KS-UNRELATED"
    with pytest.raises(IsolationViolation, match="did not resolve uniquely"):
        context.guard.admit_outbound(
            context.plan.evaluation_plan_id, context.captured,
            OutboundChannel.GENERATION_PROMPT, {},
        )


def test_authority_change_between_admission_and_invocation_fails_closed(outbound_context):
    context = outbound_context
    payload = {"event_id": "EVT-1"}
    admitted = context.guard.admit_outbound(
        context.plan.evaluation_plan_id, context.captured,
        OutboundChannel.GENERATION_PROMPT, payload,
    )
    context.evidence.snapshot_content = '{"evidence":"changed-authority"}'
    with pytest.raises(IsolationViolation, match="drifted"):
        context.guard.verify_before_invocation(
            admitted, context.captured, ActualOutboundPins.from_admitted(admitted), payload,
        )


def test_production_dependency_direction_guard(tmp_path):
    production = tmp_path / "production_src"
    production.mkdir()
    (production / "good.py").write_text("import json\n", encoding="utf-8")
    assert_no_production_dependency_on_evaluation(production)
    (production / "bad.py").write_text("from rca_evaluation import observation\n", encoding="utf-8")
    with pytest.raises(IsolationViolation):
        assert_no_production_dependency_on_evaluation(production)

    assert_no_production_dependency_on_evaluation(Path("src"))


def test_actual_repository_sensitive_inventory_is_complete_and_scanned():
    audit = validate_production_sensitive_inventory(Path("."))
    assert set(audit.scanned_paths) == {
        entry.relative_path for entry in PRODUCTION_SENSITIVE_INVENTORY
    }
    assert {surface for entry in PRODUCTION_SENSITIVE_INVENTORY for surface in entry.surfaces} == set(SensitiveSurface)
    assert any(
        entry.relative_path.endswith("/__init__.py")
        for entry in PRODUCTION_SENSITIVE_INVENTORY
    )
    assert any(
        entry.classification is SourceClassification.REVIEWED_EXCLUSION
        for entry in PRODUCTION_SENSITIVE_INVENTORY
    )
    assert audit.inventory_commitment.startswith("sha256:")


def test_inventory_omission_and_unresolved_repository_fail_closed(tmp_path):
    with pytest.raises(IsolationViolation, match="cannot be reduced"):
        validate_production_sensitive_inventory(Path("."), PRODUCTION_SENSITIVE_INVENTORY[:-1])
    with pytest.raises(IsolationViolation, match="unresolved"):
        validate_production_sensitive_inventory(tmp_path)


def test_unknown_and_unclassified_new_modules_fail_closed(tmp_path):
    repository = tmp_path / "repository"
    shutil.copytree(Path("src"), repository / "src")
    for name, content in (
        ("unknown_name.py", "VALUE = 1\n"),
        ("totally_benign_module.py", "def helper():\n    return None\n"),
    ):
        unclassified = repository / "src" / "llm_generation" / name
        unclassified.write_text(content, encoding="utf-8")
        with pytest.raises(IsolationViolation, match="unclassified"):
            validate_production_sensitive_inventory(repository)
        unclassified.unlink()


def test_inventory_enforces_init_import_field_exclusion_and_forbidden_terms(tmp_path):
    repository = tmp_path / "repository"
    shutil.copytree(Path("src"), repository / "src")

    governed_init = repository / "src" / "llm_generation" / "__init__.py"
    original = governed_init.read_text(encoding="utf-8")
    governed_init.write_text(original + "\n# drift\n", encoding="utf-8")
    with pytest.raises(IsolationViolation, match="reviewed exclusion commitment changed"):
        validate_production_sensitive_inventory(repository)
    governed_init.write_text(original, encoding="utf-8")

    sensitive = repository / "src" / "llm_generation" / "projection.py"
    original = sensitive.read_text(encoding="utf-8")
    sensitive.write_text(original + "\nimport fractions\n", encoding="utf-8")
    with pytest.raises(IsolationViolation, match="non-allowlisted dependency"):
        validate_production_sensitive_inventory(repository)
    sensitive.write_text(original + "\nNEW_FIELDS = {'unapproved_production_field': 'x'}\n", encoding="utf-8")
    with pytest.raises(IsolationViolation, match="non-allowlisted production-bound field"):
        validate_production_sensitive_inventory(repository)
    sensitive.write_text(original + "\nexpected_root_cause = 'forbidden'\n", encoding="utf-8")
    with pytest.raises(IsolationViolation, match="forbidden evaluation semantics"):
        validate_production_sensitive_inventory(repository)
    sensitive.write_text(original, encoding="utf-8")

    exclusion = repository / "src" / "llm_generation" / "identity.py"
    original = exclusion.read_text(encoding="utf-8")
    exclusion.write_text(original + "\n# reviewed exclusion drift\n", encoding="utf-8")
    with pytest.raises(IsolationViolation, match="reviewed exclusion commitment changed"):
        validate_production_sensitive_inventory(repository)


def test_missing_classified_source_fails_closed(tmp_path):
    repository = tmp_path / "repository"
    shutil.copytree(Path("src"), repository / "src")
    (repository / "src" / "llm_generation" / "projection.py").unlink()
    with pytest.raises(IsolationViolation, match="inventory source is missing"):
        validate_production_sensitive_inventory(repository)


def test_workspace_requires_complete_trusted_isolation_policy(tmp_path):
    root = tmp_path / "rca_evaluation"
    with pytest.raises(TypeError, match="isolation_policy"):
        ExecutionWorkspace.create(
            root, captured("omitted"), scenario_id="S1",
            canary_commitment="sha256:canary",
        )
    with pytest.raises(ValueError, match="empty or incomplete"):
        TrustedIsolationPolicy((), authority_reference="trusted-empty")
    incomplete = tuple(
        ProductionRoot(kind, tmp_path / "production" / kind.value)
        for kind in ProductionRootKind if kind is not ProductionRootKind.RUNTIME
    )
    with pytest.raises(ValueError, match="empty or incomplete"):
        TrustedIsolationPolicy(incomplete, authority_reference="trusted-incomplete")


def test_workspace_rejects_both_overlap_directions_and_accepts_isolation(tmp_path):
    production_parent = tmp_path / "production"
    under_production = production_parent / "rca_evaluation"
    parent_policy = isolation_policy(
        tmp_path, **{ProductionRootKind.EVENT.value: production_parent},
    )
    with pytest.raises(ValueError, match="overlaps"):
        ExecutionWorkspace.create(
            under_production, captured("under-production"), scenario_id="S1",
            canary_commitment="sha256:canary", isolation_policy=parent_policy,
        )

    evaluation_root = tmp_path / "isolated" / "rca_evaluation"
    child_policy = isolation_policy(
        tmp_path, **{ProductionRootKind.RUNTIME.value: evaluation_root / "runtime.db"},
    )
    with pytest.raises(ValueError, match="overlaps"):
        ExecutionWorkspace.create(
            evaluation_root, captured("production-under"), scenario_id="S1",
            canary_commitment="sha256:canary", isolation_policy=child_policy,
        )

    valid = ExecutionWorkspace.create(
        evaluation_root, captured("valid-isolated"), scenario_id="S1",
        canary_commitment="sha256:canary", isolation_policy=isolation_policy(tmp_path),
    )
    assert valid.path.is_dir()
