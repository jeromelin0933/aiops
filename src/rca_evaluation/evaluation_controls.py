"""PM-authorized evaluation-only refresh fixtures and restart controls."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .identity import canonical_text_bytes, canonical_text_commitment, commitment


APPROVAL_REFERENCE = "PM-SPEC017-R2S4-20261013"
CONTROL_SCHEMA = "spec017.r2s4-evaluation-controls.v2"
CONTROL_ARTIFACT_COMMITMENT = (
    "sha256:3fe0a4f5d0c63cbc1723e4f1c5dbbad5c2cdf7a30f3d50c873a3c7e2c0924910"
)
_LOADER_AUTHORITY = object()
_CHECKPOINT_ADAPTER_AUTHORITY = object()


def _sha(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value.startswith("sha256:")
            or len(value) != 71):
        raise ValueError(f"{label} must be a SHA-256 commitment")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a SHA-256 commitment") from exc
    return value


@dataclass(frozen=True, slots=True)
class EvidencePairFixture:
    fixture_identity: str
    scenario_identity: str
    variant: str
    incident_scope: str
    baseline_input_identity: str
    candidate_input_identity: str
    baseline_source: dict[str, object]
    candidate_source: dict[str, object]
    expected_public_observation_class: str
    oracle_scenario_reference: str
    isolation_canary_commitment: str
    baseline_input_commitment: str
    candidate_input_commitment: str
    pair_commitment: str


@dataclass(frozen=True, slots=True)
class RestartControl:
    boundary_identity: str
    fixture_identity: str
    control_reference: str
    exact_control_hook_identity: str
    crash_point: str
    restart_reopen_method: str
    pre_crash_required_facts: tuple[str, ...]
    pre_crash_forbidden_facts: tuple[str, ...]
    expected_terminal_public_facts: tuple[str, ...]
    no_duplicate_assertions: tuple[str, ...]
    control_commitment: str


@dataclass(frozen=True, slots=True)
class EvaluationControlArtifact:
    fixture_set_identity: str
    fixture_set_revision: str
    classification: str
    pm_approval_reference: str
    materiality_rule_version: str
    artifact_commitment: str
    refresh_cases: tuple[EvidencePairFixture, ...]
    restart_controls: tuple[RestartControl, ...]
    _authority: object

    def refresh(self, variant: str) -> EvidencePairFixture:
        matches = tuple(item for item in self.refresh_cases
                        if item.variant == variant)
        if len(matches) != 1:
            raise ValueError("evaluation refresh fixture is absent or ambiguous")
        return matches[0]

    def restart(self, boundary: str) -> RestartControl:
        matches = tuple(item for item in self.restart_controls
                        if item.boundary_identity == boundary)
        if len(matches) != 1:
            raise ValueError("evaluation restart control is absent or ambiguous")
        return matches[0]


def load_evaluation_control_artifact(
    path: str | Path,
) -> EvaluationControlArtifact:
    artifact_path = Path(path)
    raw_bytes = artifact_path.read_bytes()
    actual = canonical_text_commitment(raw_bytes)
    if actual != CONTROL_ARTIFACT_COMMITMENT:
        raise ValueError("evaluation control artifact commitment mismatch")
    raw = json.loads(canonical_text_bytes(raw_bytes))
    if (raw.get("schema_version") != CONTROL_SCHEMA
            or raw.get("classification") != "EVALUATION ONLY"
            or raw.get("pm_approval_reference") != APPROVAL_REFERENCE
            or raw.get("materiality_rule_version")
                != "evidence-materiality-v1"):
        raise ValueError("evaluation control artifact authority is invalid")
    refresh = []
    for name in ("material", "material_failure", "non_material"):
        item = raw["refresh_cases"][name]
        baseline = commitment(item["baseline_source"])
        candidate = commitment(item["candidate_source"])
        if baseline == candidate:
            raise ValueError("evaluation Evidence pair must be semantically distinct")
        refresh.append(EvidencePairFixture(
            **item, baseline_input_commitment=baseline,
            candidate_input_commitment=candidate,
            pair_commitment=commitment(item),
        ))
    controls = []
    for boundary in (
        "EVIDENCE", "KNOWLEDGE", "GENERATION", "ARTIFACT_COMMIT",
        "PUBLICATION",
    ):
        item = raw["restart_controls"][boundary]
        converted = dict(item)
        for field in (
            "pre_crash_required_facts", "pre_crash_forbidden_facts",
            "expected_terminal_public_facts", "no_duplicate_assertions",
        ):
            converted[field] = tuple(converted[field])
            if not converted[field]:
                raise ValueError("restart control fact sets must be non-empty")
        controls.append(RestartControl(
            boundary, **converted, control_commitment=commitment(item)))
    if len({item.exact_control_hook_identity for item in controls}) != 5:
        raise ValueError("restart controls must have distinct exact hook semantics")
    return EvaluationControlArtifact(
        raw["fixture_set_identity"], raw["fixture_set_revision"],
        raw["classification"], raw["pm_approval_reference"],
        raw["materiality_rule_version"], actual, tuple(refresh),
        tuple(controls), _LOADER_AUTHORITY,
    )


@dataclass(frozen=True, slots=True, init=False)
class ActualCheckpointReceipt:
    boundary_identity: str
    exact_control_hook_identity: str
    operation_identity: str
    work_identity: str
    durable_facts: dict[str, object]
    durable_facts_commitment: str
    _authority: object

    @classmethod
    def _issued(cls, boundary_identity, exact_control_hook_identity,
                operation_identity, work_identity, durable_facts):
        value = object.__new__(cls)
        facts = dict(durable_facts)
        for name, item in (
            ("boundary_identity", boundary_identity),
            ("exact_control_hook_identity", exact_control_hook_identity),
            ("operation_identity", operation_identity),
            ("work_identity", work_identity),
            ("durable_facts", facts),
            ("durable_facts_commitment", commitment(facts)),
            ("_authority", _CHECKPOINT_ADAPTER_AUTHORITY),
        ):
            object.__setattr__(value, name, item)
        return value


def is_actual_checkpoint_receipt(value: object) -> bool:
    return (type(value) is ActualCheckpointReceipt
            and value._authority is _CHECKPOINT_ADAPTER_AUTHORITY)


@dataclass(frozen=True, slots=True, init=False)
class ActualRecoveryReceipt:
    boundary_identity: str
    operation_identity: str
    work_identity: str
    checkpoint_commitment: str
    terminal_facts: dict[str, object]
    terminal_facts_commitment: str
    duplicate_effect_detected: bool
    _authority: object

    @classmethod
    def _issued(cls, checkpoint, facts):
        value = object.__new__(cls)
        facts = dict(facts)
        for name, item in (
            ("boundary_identity", checkpoint.boundary_identity),
            ("operation_identity", checkpoint.operation_identity),
            ("work_identity", checkpoint.work_identity),
            ("checkpoint_commitment", checkpoint.durable_facts_commitment),
            ("terminal_facts", facts),
            ("terminal_facts_commitment", commitment(facts)),
            ("duplicate_effect_detected", False),
            ("_authority", _CHECKPOINT_ADAPTER_AUTHORITY),
        ):
            object.__setattr__(value, name, item)
        return value


def is_actual_recovery_receipt(value: object) -> bool:
    return (type(value) is ActualRecoveryReceipt
            and value._authority is _CHECKPOINT_ADAPTER_AUTHORITY)


class ControlledEvaluationInterruption(BaseException):
    """Evaluation-only interruption after a verified real lifecycle effect."""

    def __init__(self, receipt: ActualCheckpointReceipt | None = None) -> None:
        super().__init__()
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class ArmedFailpoint:
    control: RestartControl
    operation_identity: str
    work_identity: str

    def interrupt_at(
        self, *, hook_identity: str, durable_facts_commitment: str,
    ) -> None:
        """Reject label-only controls; formal interruptions require an adapter."""
        del hook_identity, durable_facts_commitment
        raise ValueError("failpoint label without an actual checkpoint is not formal evidence")

    def _interrupt_verified(
        self, receipt: ActualCheckpointReceipt, *, authority: object,
    ) -> None:
        if authority is not _CHECKPOINT_ADAPTER_AUTHORITY:
            raise ValueError("checkpoint adapter authority is invalid")
        if receipt.boundary_identity != self.control.boundary_identity:
            raise ValueError("control was invoked at the wrong boundary")
        hook_identity = receipt.exact_control_hook_identity
        durable_facts_commitment = receipt.durable_facts_commitment
        if hook_identity != self.control.exact_control_hook_identity:
            raise ValueError("control was invoked at the wrong checkpoint")
        _sha(durable_facts_commitment, "durable facts commitment")
        if durable_facts_commitment != commitment(receipt.durable_facts):
            raise ValueError("durable checkpoint facts are contradictory")
        if (receipt.operation_identity != self.operation_identity
                or receipt.work_identity != self.work_identity):
            raise ValueError("checkpoint operation/work binding is contradictory")
        raise ControlledEvaluationInterruption(receipt)


class GovernedCheckpointAdapter:
    """Candidate-F-only wrappers around five existing lifecycle methods.

    Each method first invokes the production delegate and then verifies its
    durable result through the corresponding public read authority.  The
    adapter decides only when to interrupt and never manufactures the result.
    """

    def __init__(self, armed: ArmedFailpoint) -> None:
        if type(armed) is not ArmedFailpoint:
            raise TypeError("checkpoint adapter requires an armed control")
        self._armed = armed

    def _stop(self, facts: dict[str, object]) -> None:
        if not facts or any(value is None for value in facts.values()):
            raise ValueError("actual checkpoint lacks durable facts")
        receipt = ActualCheckpointReceipt._issued(
            self._armed.control.boundary_identity,
            self._armed.control.exact_control_hook_identity,
            self._armed.operation_identity, self._armed.work_identity,
            facts)
        self._armed._interrupt_verified(
            receipt, authority=_CHECKPOINT_ADAPTER_AUTHORITY)

    def capture_evidence(self, service, command):
        if self._armed.control.boundary_identity != "EVIDENCE":
            raise ValueError("Evidence wrapper used for the wrong boundary")
        result = service.capture_evidence(command)
        snapshot_id = getattr(result, "snapshot_id", None)
        revision_id = getattr(result, "revision_id", None)
        outcome = service.read_capture_outcome(command.capture_operation_id)
        snapshot = service.resolve_snapshot(snapshot_id) if snapshot_id else None
        revision = service.resolve_revision(revision_id) if revision_id else None
        if (outcome is None or snapshot is None or revision is None
                or outcome.snapshot_id != snapshot_id
                or snapshot.revision_id != revision_id):
            raise ValueError("Evidence checkpoint is not durably authoritative")
        self._stop({"capture_operation_id": command.capture_operation_id,
                    "snapshot_id": snapshot_id, "revision_id": revision_id})

    def resolve_knowledge(self, service, request, limits):
        if self._armed.control.boundary_identity != "KNOWLEDGE":
            raise ValueError("Knowledge wrapper used for the wrong boundary")
        result = service.resolve(request, limits)
        snapshot = getattr(result, "snapshot", None)
        if snapshot is None:
            raise ValueError("Knowledge checkpoint has no terminal Snapshot")
        stored = service.read_snapshot(snapshot.snapshot_key)
        provenance = service.read_provenance(snapshot.snapshot_key)
        if (getattr(stored, "value", None) != snapshot
                or getattr(provenance, "value", None) is None):
            raise ValueError("Knowledge checkpoint is not durably authoritative")
        self._stop({"retrieval_operation_id": request.operation_key.value,
                    "snapshot_key": snapshot.snapshot_key.value,
                    "provenance_commitment":
                        provenance.value.snapshot_commitment})

    def execute_generation(self, service, *args, **kwargs):
        if self._armed.control.boundary_identity != "GENERATION":
            raise ValueError("Generation wrapper used for the wrong boundary")
        result = service.execute(*args, **kwargs)
        terminal = getattr(result, "result", None) or getattr(result, "failure", None)
        if terminal is None:
            raise ValueError("Candidate-D checkpoint is not terminal")
        recovery = service.store.recovery()
        durable = tuple(getattr(recovery, "value", ()).results) if getattr(recovery, "value", None) else ()
        terminal_id = (getattr(terminal, "validated_result_id", None)
                       or getattr(terminal, "operation_id", None))
        try_identity = (getattr(terminal, "try_identity", None)
                        or getattr(getattr(terminal, "content", None),
                                   "input", None))
        try_identity = getattr(try_identity, "try_identity", try_identity)
        if not durable or not terminal_id:
            raise ValueError("Candidate-D terminal result is not durable")
        self._stop({"candidate_d_terminal_id": terminal_id,
                    "try_identity": str(try_identity)})

    def commit_artifact(self, candidate_a, *args, **kwargs):
        if self._armed.control.boundary_identity != "ARTIFACT_COMMIT":
            raise ValueError("Artifact wrapper used for the wrong boundary")
        version = candidate_a.commit_validated_artifact(*args, **kwargs)
        history = candidate_a.get_version_history(version.aggregate_id)
        lineage = candidate_a.get_attempt_lineage(version.attempt_id)
        if (sum(item.version_id == version.version_id for item in history) != 1
                or lineage is None or not lineage.try_outcomes):
            raise ValueError("Artifact checkpoint is not durably authoritative")
        self._stop({"version_id": version.version_id,
                    "attempt_id": version.attempt_id,
                    "publication_operation_id": version.publication_operation_id})

    def publish_spec008(self, publish, incident_reader, command):
        if self._armed.control.boundary_identity != "PUBLICATION":
            raise ValueError("Publication wrapper used for the wrong boundary")
        publish(command)
        operation_id = command.publication_operation_id
        receipt = incident_reader.get_rca_publication_result(operation_id)
        relationship = incident_reader.get_rca_relationship(command.incident_id)
        if receipt is None or relationship is None:
            raise ValueError("SPEC-008 checkpoint is not durably authoritative")
        self._stop({"publication_operation_id": operation_id,
                    "target_version_id": receipt.target_version_id,
                    "incident_current_version_id": relationship.current_version_id})

    def verify_evidence_recovery(self, checkpoint, service, command):
        self._require_checkpoint(checkpoint, "EVIDENCE")
        result = service.capture_evidence(command)
        facts = {"capture_operation_id": command.capture_operation_id,
                 "snapshot_id": result.snapshot_id,
                 "revision_id": result.revision_id}
        if (facts != checkpoint.durable_facts
                or service.resolve_snapshot(result.snapshot_id) is None
                or service.resolve_revision(result.revision_id) is None):
            raise ValueError("Evidence recovery did not converge")
        return ActualRecoveryReceipt._issued(checkpoint, facts)

    def verify_knowledge_recovery(self, checkpoint, service, request, limits):
        self._require_checkpoint(checkpoint, "KNOWLEDGE")
        result = service.resolve(request, limits)
        snapshot = result.snapshot
        provenance = service.read_provenance(snapshot.snapshot_key)
        facts = {"retrieval_operation_id": request.operation_key.value,
                 "snapshot_key": snapshot.snapshot_key.value,
                 "provenance_commitment": provenance.value.snapshot_commitment}
        if facts != checkpoint.durable_facts:
            raise ValueError("Knowledge recovery did not converge")
        return ActualRecoveryReceipt._issued(checkpoint, facts)

    def verify_generation_recovery(self, checkpoint, candidate_a, d_store,
                                   attempt_id):
        self._require_checkpoint(checkpoint, "GENERATION")
        lineage = candidate_a.get_attempt_lineage(attempt_id)
        recovered = d_store.recovery().value
        terminal_id = checkpoint.durable_facts["candidate_d_terminal_id"]
        if (lineage is None or len(lineage.try_outcomes) != 1
                or sum(item.validated_result_id == terminal_id
                       for item in recovered.results) != 1):
            raise ValueError("Generation recovery did not converge")
        return ActualRecoveryReceipt._issued(checkpoint, {
            "candidate_d_terminal_id": terminal_id,
            "try_outcome_count": 1, "attempt_id": attempt_id})

    def verify_artifact_recovery(self, checkpoint, candidate_a, aggregate_id):
        self._require_checkpoint(checkpoint, "ARTIFACT_COMMIT")
        history = candidate_a.get_version_history(aggregate_id)
        version_id = checkpoint.durable_facts["version_id"]
        if sum(item.version_id == version_id for item in history) != 1:
            raise ValueError("Artifact recovery did not converge")
        return ActualRecoveryReceipt._issued(checkpoint, {
            "version_id": version_id, "version_count": len(history),
            "attempt_id": checkpoint.durable_facts["attempt_id"]})

    def verify_publication_recovery(self, checkpoint, candidate_a, incidents):
        self._require_checkpoint(checkpoint, "PUBLICATION")
        operation = checkpoint.operation_identity
        a_result = candidate_a.get_publication_result(operation)
        i_result = incidents.get_rca_publication_result(operation)
        current = candidate_a.get_current(a_result.target.aggregate_id)
        if (i_result is None or current is None
                or current.version.version_id != a_result.target.target_version_id):
            raise ValueError("Publication recovery did not converge")
        return ActualRecoveryReceipt._issued(checkpoint, {
            "publication_operation_id": operation,
            "target_version_id": a_result.target.target_version_id,
            "incident_current_version_id": i_result.resulting_current_version_id})

    def _require_checkpoint(self, checkpoint, boundary):
        if (not is_actual_checkpoint_receipt(checkpoint)
                or checkpoint.boundary_identity != boundary
                or checkpoint.operation_identity != self._armed.operation_identity
                or checkpoint.work_identity != self._armed.work_identity):
            raise ValueError("recovery receipt lacks its actual checkpoint")


def arm_failpoint(
    artifact: EvaluationControlArtifact, *, boundary: str,
    control_commitment: str, operation_identity: str, work_identity: str,
) -> ArmedFailpoint:
    if (type(artifact) is not EvaluationControlArtifact
            or artifact._authority is not _LOADER_AUTHORITY):
        raise TypeError("failpoint requires the verified evaluation-only artifact")
    control = artifact.restart(boundary)
    if control.control_commitment != control_commitment:
        raise ValueError("wrong boundary control commitment")
    for value, label in ((operation_identity, "operation identity"),
                         (work_identity, "work identity")):
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{label} must be non-empty trimmed text")
    return ArmedFailpoint(control, operation_identity, work_identity)


__all__ = [
    "APPROVAL_REFERENCE", "ActualCheckpointReceipt", "ActualRecoveryReceipt",
    "ArmedFailpoint",
    "ControlledEvaluationInterruption", "GovernedCheckpointAdapter",
    "EvaluationControlArtifact", "EvidencePairFixture", "RestartControl",
    "arm_failpoint", "is_actual_checkpoint_receipt",
    "is_actual_recovery_receipt",
    "load_evaluation_control_artifact",
]
