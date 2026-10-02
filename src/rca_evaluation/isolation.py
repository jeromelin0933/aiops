"""Ground Truth leakage and production dependency direction guards."""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import json
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .identity import commitment, require_identity
from .observation import (
    CapturedInputBoundary,
    ObservationOutcome,
    PublicObservationResolver,
)
from .sqlite_store import SqliteEvaluationStore

_FORBIDDEN_NAMES = frozenset({
    "scenarioid", "evaluationrunid", "expectedcausalclass",
    "expectedrootcause", "expectedanswer", "acceptedalternative",
    "forbiddenoverclaim", "oraclerevisionid", "oracleanswer",
    "groundtruth", "evaluationtruth", "readjudicationresult",
})


class IsolationViolation(ValueError):
    pass


class OutboundChannel(str, Enum):
    GENERATION_PROMPT = "GENERATION_PROMPT"
    EVIDENCE_QUERY = "EVIDENCE_QUERY"
    KNOWLEDGE_QUERY = "KNOWLEDGE_QUERY"
    PRODUCTION_RUNTIME = "PRODUCTION_RUNTIME"
    PRODUCTION_PERSISTENCE = "PRODUCTION_PERSISTENCE"


def _reference(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed value")
    return value


def _commitment(value: object, field: str) -> str:
    value = _reference(value, field)
    if len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError(f"{field} must be a SHA-256 commitment")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be a SHA-256 commitment") from exc
    return value


@dataclass(frozen=True, slots=True)
class OutboundBasis:
    channel: OutboundChannel
    evaluation_plan_id: str
    evaluation_run_id: str
    execution_id: str
    oracle_revision_id: str
    oracle_content_commitment: str
    production_prompt_identity: str
    production_prompt_commitment: str
    evidence_snapshot_id: str
    evidence_snapshot_commitment: str
    knowledge_snapshot_id: str
    knowledge_snapshot_commitment: str
    provider_identity: str
    non_secret_config_commitment: str

    def __post_init__(self) -> None:
        if not isinstance(self.channel, OutboundChannel):
            raise TypeError("channel must be an OutboundChannel")
        require_identity(self.evaluation_plan_id, "evaluation_plan")
        require_identity(self.evaluation_run_id, "evaluation_run")
        require_identity(self.execution_id, "execution")
        require_identity(self.oracle_revision_id, "oracle_revision")
        for name in (
            "production_prompt_identity", "evidence_snapshot_id",
            "knowledge_snapshot_id", "provider_identity",
        ):
            _reference(getattr(self, name), name)
        for name in (
            "production_prompt_commitment", "evidence_snapshot_commitment",
            "knowledge_snapshot_commitment", "non_secret_config_commitment",
            "oracle_content_commitment",
        ):
            _commitment(getattr(self, name), name)

    def material(self) -> dict:
        return {
            "channel": self.channel.value,
            "evaluation_plan_id": self.evaluation_plan_id,
            "evaluation_run_id": self.evaluation_run_id,
            "execution_id": self.execution_id,
            "oracle_revision_id": self.oracle_revision_id,
            "oracle_content_commitment": self.oracle_content_commitment,
            "production_prompt_identity": self.production_prompt_identity,
            "production_prompt_commitment": self.production_prompt_commitment,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "evidence_snapshot_commitment": self.evidence_snapshot_commitment,
            "knowledge_snapshot_id": self.knowledge_snapshot_id,
            "knowledge_snapshot_commitment": self.knowledge_snapshot_commitment,
            "provider_identity": self.provider_identity,
            "non_secret_config_commitment": self.non_secret_config_commitment,
        }


@dataclass(frozen=True, slots=True)
class SanitizedOutboundManifest:
    basis: OutboundBasis
    outbound_payload_commitment: str
    manifest_commitment: str


@dataclass(frozen=True, slots=True)
class VerifiedOutbound:
    manifest: SanitizedOutboundManifest
    canonical_payload: bytes


@dataclass(frozen=True, slots=True)
class ActualOutboundPins:
    """Values presented by the production adapter immediately before invocation."""

    channel: OutboundChannel
    production_prompt_identity: str
    production_prompt_commitment: str
    evidence_snapshot_id: str
    evidence_snapshot_commitment: str
    knowledge_snapshot_id: str
    knowledge_snapshot_commitment: str
    provider_identity: str
    non_secret_config_commitment: str

    @classmethod
    def from_admitted(cls, admitted: SanitizedOutboundManifest) -> "ActualOutboundPins":
        if not isinstance(admitted, SanitizedOutboundManifest):
            raise TypeError("admitted must be a SanitizedOutboundManifest")
        basis = admitted.basis
        return cls(
            basis.channel, basis.production_prompt_identity,
            basis.production_prompt_commitment, basis.evidence_snapshot_id,
            basis.evidence_snapshot_commitment, basis.knowledge_snapshot_id,
            basis.knowledge_snapshot_commitment, basis.provider_identity,
            basis.non_secret_config_commitment,
        )

    def apply_to(self, authoritative: OutboundBasis) -> OutboundBasis:
        if not isinstance(self.channel, OutboundChannel):
            raise TypeError("actual channel must be an OutboundChannel")
        return OutboundBasis(
            channel=self.channel,
            evaluation_plan_id=authoritative.evaluation_plan_id,
            evaluation_run_id=authoritative.evaluation_run_id,
            execution_id=authoritative.execution_id,
            oracle_revision_id=authoritative.oracle_revision_id,
            oracle_content_commitment=authoritative.oracle_content_commitment,
            production_prompt_identity=self.production_prompt_identity,
            production_prompt_commitment=self.production_prompt_commitment,
            evidence_snapshot_id=self.evidence_snapshot_id,
            evidence_snapshot_commitment=self.evidence_snapshot_commitment,
            knowledge_snapshot_id=self.knowledge_snapshot_id,
            knowledge_snapshot_commitment=self.knowledge_snapshot_commitment,
            provider_identity=self.provider_identity,
            non_secret_config_commitment=self.non_secret_config_commitment,
        )


class AuthoritativeOutboundResolver:
    """Resolve outbound pins only from admitted S1 facts and S2 public lineage."""

    def __init__(self, evaluation_store: SqliteEvaluationStore,
                 observations: PublicObservationResolver) -> None:
        if type(evaluation_store) is not SqliteEvaluationStore:
            raise TypeError("evaluation_store must be the S1 evaluation authority")
        if type(observations) is not PublicObservationResolver:
            raise TypeError("observations must be the S2 public observation resolver")
        self._store = evaluation_store
        self._observations = observations

    def resolve(self, evaluation_plan_id: str, captured: CapturedInputBoundary,
                channel: OutboundChannel) -> OutboundBasis:
        require_identity(evaluation_plan_id, "evaluation_plan")
        if not isinstance(captured, CapturedInputBoundary):
            raise TypeError("captured must be a CapturedInputBoundary")
        if not isinstance(channel, OutboundChannel):
            raise TypeError("channel must be an OutboundChannel")
        self._store.readiness_check()
        plan = self._store.get_plan(evaluation_plan_id)
        if plan is None:
            raise IsolationViolation("outbound plan is not admitted")
        execution = self._store.get(captured.execution_id)
        if execution is None or execution.domain != "execution":
            raise IsolationViolation("outbound execution is not admitted")
        run = self._store.get(execution.parent_id)
        if run is None or run.domain != "evaluation_run" or run.parent_id != plan.evaluation_plan_id:
            raise IsolationViolation("execution does not belong to the admitted plan")
        run_payload = json.loads(run.semantic_json)
        if run_payload.get("oracle_revision_id") != plan.oracle_revision_id:
            raise IsolationViolation("evaluation run Oracle pin disagrees with admitted plan")
        execution_payload = json.loads(execution.semantic_json)
        if (execution_payload.get("scenario_id") not in {f"S{i}" for i in range(1, 7)}
                or not isinstance(execution_payload.get("repetition"), int)):
            raise IsolationViolation("admitted execution facts are invalid")
        resolved = self._observations.resolve(captured)
        if resolved.outcome is not ObservationOutcome.RESOLVED or resolved.observation is None:
            raise IsolationViolation("public production lineage did not resolve uniquely")
        observation = resolved.observation
        if observation.execution_id != execution.record_id:
            raise IsolationViolation("public observation belongs to another execution")
        return OutboundBasis(
            channel=channel,
            evaluation_plan_id=plan.evaluation_plan_id,
            evaluation_run_id=run.record_id,
            execution_id=execution.record_id,
            oracle_revision_id=plan.oracle_revision_id,
            oracle_content_commitment=plan.oracle_content_commitment,
            production_prompt_identity=plan.prompt,
            production_prompt_commitment=commitment({
                "prompt": plan.prompt,
                "schema": plan.schema,
            }),
            evidence_snapshot_id=observation.evidence_snapshot_id,
            evidence_snapshot_commitment=observation.evidence_commitment,
            knowledge_snapshot_id=observation.knowledge_snapshot_id,
            knowledge_snapshot_commitment=observation.knowledge_commitment,
            provider_identity=plan.model,
            non_secret_config_commitment=commitment({
                "configuration": plan.configuration,
                "corpus_index": plan.corpus_index,
                "fixture_revision": plan.fixture_revision,
                "production_build": plan.production_build,
                "schema": plan.schema,
            }),
        )


def _outbound_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IsolationViolation("outbound payload must be finite canonical JSON") from exc


def _decoded_variants(value: str) -> tuple[str, ...]:
    pending = [value]
    found: list[str] = []
    while pending and len(found) < 12:
        current = pending.pop(0)
        if current in found:
            continue
        found.append(current)
        unquoted = urllib.parse.unquote(current)
        if unquoted != current:
            pending.append(unquoted)
        compact = "".join(current.split())
        if len(compact) >= 8:
            if len(compact) % 2 == 0:
                try:
                    decoded = bytes.fromhex(compact).decode("utf-8")
                    pending.append(decoded)
                except (ValueError, UnicodeDecodeError):
                    pass
            try:
                padded = compact + "=" * (-len(compact) % 4)
                decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
                pending.append(decoded)
            except (binascii.Error, ValueError, UnicodeDecodeError):
                pass
    return tuple(found)


def _normalized(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _walk_text(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_text(item)
    elif isinstance(value, str):
        yield value


class GroundTruthIsolationGuard:
    def __init__(self, authority: AuthoritativeOutboundResolver, *,
                 protected_values: tuple[str, ...] = (),
                 canary_commitments: tuple[str, ...] = ()) -> None:
        if type(authority) is not AuthoritativeOutboundResolver:
            raise TypeError("an authoritative outbound resolver is required")
        values = tuple(protected_values) + tuple(canary_commitments)
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("protected values and canaries must be non-empty strings")
        self._authority = authority
        self._protected = frozenset(values)

    def assert_safe(self, payload: object) -> None:
        _outbound_bytes(payload)
        for raw in _walk_text(payload):
            for value in _decoded_variants(raw):
                normalized = _normalized(value)
                if any(name in normalized for name in _FORBIDDEN_NAMES):
                    raise IsolationViolation("outbound payload contains a forbidden evaluation field")
                if "eval:" in value.lower():
                    raise IsolationViolation("outbound payload contains an evaluation identity")
                if any(secret in value for secret in self._protected):
                    raise IsolationViolation("outbound payload contains protected Oracle or canary material")

    def _manifest(self, basis: OutboundBasis, payload: object) -> SanitizedOutboundManifest:
        if not isinstance(basis, OutboundBasis):
            raise TypeError("basis must be an OutboundBasis")
        production_basis = basis.material().copy()
        for evaluation_pin in (
            "evaluation_plan_id", "evaluation_run_id", "execution_id",
            "oracle_revision_id", "oracle_content_commitment",
        ):
            del production_basis[evaluation_pin]
        self.assert_safe(production_basis)
        self.assert_safe(payload)
        digest = hashlib.sha256(_outbound_bytes(payload)).hexdigest()
        payload_commitment = f"sha256:{digest}"
        material = {**basis.material(), "outbound_payload_commitment": payload_commitment}
        return SanitizedOutboundManifest(basis, payload_commitment, commitment(material))

    def admit_outbound(
        self, evaluation_plan_id: str, captured: CapturedInputBoundary,
        channel: OutboundChannel, payload: object,
    ) -> SanitizedOutboundManifest:
        """Admit an evaluation-side manifest; this does not invoke production."""
        basis = self._authority.resolve(evaluation_plan_id, captured, channel)
        return self._manifest(basis, payload)

    def commit_outbound(
        self, evaluation_plan_id: str, captured: CapturedInputBoundary,
        channel: OutboundChannel, payload: object,
    ) -> SanitizedOutboundManifest:
        """Compatibility name with mandatory approved basis binding."""
        return self.admit_outbound(evaluation_plan_id, captured, channel, payload)

    def verify_before_invocation(
        self, admitted: SanitizedOutboundManifest, captured: CapturedInputBoundary,
        actual_pins: ActualOutboundPins, actual_payload: object,
    ) -> VerifiedOutbound:
        """Rebuild from actual input and return only exact canonical invocation bytes."""
        if not isinstance(admitted, SanitizedOutboundManifest):
            raise TypeError("admitted must be a SanitizedOutboundManifest")
        if not isinstance(actual_pins, ActualOutboundPins):
            raise TypeError("actual_pins must come from the production invocation boundary")
        authoritative_basis = self._authority.resolve(
            admitted.basis.evaluation_plan_id, captured, actual_pins.channel,
        )
        authoritative = self._manifest(authoritative_basis, actual_payload)
        if authoritative != admitted:
            raise IsolationViolation("authoritative outbound basis drifted from admitted commitment")
        actual_basis = actual_pins.apply_to(authoritative_basis)
        actual = self._manifest(actual_basis, actual_payload)
        if actual != admitted:
            raise IsolationViolation("actual outbound manifest drifted from admitted commitment")
        return VerifiedOutbound(actual, _outbound_bytes(actual_payload))


def assert_no_production_dependency_on_evaluation(source_root: str | Path) -> None:
    """Fail if any production Python module imports Candidate-F."""
    root = Path(source_root).resolve()
    for path in root.rglob("*.py"):
        if "rca_evaluation" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise IsolationViolation(f"cannot inspect production dependency: {path}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and (
                    node.module == "rca_evaluation" or node.module.startswith("rca_evaluation.")):
                raise IsolationViolation(f"production imports evaluation package: {path}")
            if isinstance(node, ast.Import) and any(
                    alias.name == "rca_evaluation" or alias.name.startswith("rca_evaluation.")
                    for alias in node.names):
                raise IsolationViolation(f"production imports evaluation package: {path}")
