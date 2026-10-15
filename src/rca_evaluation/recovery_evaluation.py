"""Read-only refresh and restart/replay assertions over upstream facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from incident_evidence.contracts import MaterialityJudgement
from incident_management.contracts import IncidentRcaPublicationDisposition
from rca_persistence.contracts import (
    CurrentFreshness, LogicalTryResultKind, PublicationDisposition,
)
from runtime_orchestration.contracts import RuntimeWorkStatus

from .identity import commitment, require_identity
from .latency import (
    LatencyDisposition, LatencyKind, LatencyMeasurement,
)


class Materiality(str, Enum):
    MATERIAL = "MATERIAL"
    NON_MATERIAL = "NON_MATERIAL"


class Freshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"


class RecoveryBoundary(str, Enum):
    EVIDENCE = "EVIDENCE"
    KNOWLEDGE = "KNOWLEDGE"
    GENERATION = "GENERATION"
    ARTIFACT_COMMIT = "ARTIFACT_COMMIT"
    PUBLICATION = "PUBLICATION"


@dataclass(frozen=True, slots=True)
class RcaPublicState:
    current_version_id: str
    freshness: Freshness
    attempt_ids: tuple[str, ...]
    try_outcome_ids: tuple[str, ...]
    version_ids: tuple[str, ...]
    publication_ids: tuple[str, ...]
    frontier_identity: str
    frontier_ordinal: int = 0
    authorized_try_ids: tuple[str, ...] = ()
    failed_try_ids: tuple[str, ...] = ()
    try_attempt_lineage: tuple[tuple[str, str], ...] = ()
    version_attempt_lineage: tuple[tuple[str, str], ...] = ()
    publication_version_lineage: tuple[tuple[str, str], ...] = ()
    authoritative_version_history: tuple[str, ...] = ()
    semantic_effects: tuple[tuple[str, str, str], ...] = ()


class RcaRecoveryReads(Protocol):
    def get_current(self, aggregate_id: str) -> object | None: ...
    def get_version_history(self, aggregate_id: str) -> tuple[object, ...]: ...
    def get_freshness_lineage(self, aggregate_id: str) -> tuple[object, ...]: ...
    def get_attempt_lineage(self, attempt_id: str) -> object | None: ...
    def get_publication_result(self, publication_operation_id: str) -> object | None: ...


class IncidentRecoveryReads(Protocol):
    def get_rca_relationship(self, incident_id: str) -> object | None: ...
    def get_rca_publication_result(self, publication_operation_id: str) -> object | None: ...


class EvidenceRecoveryReads(Protocol):
    def read_materiality_result(self, materiality_result_id: str) -> object | None: ...


class RuntimeRecoveryReads(Protocol):
    def get(self, work_id: str) -> object | None: ...


class RecoveryObservationClock(Protocol):
    def monotonic_ns(self) -> int: ...
    def utc_now(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RecoveryStateReference:
    evaluation_run_id: str
    execution_id: str
    incident_id: str
    aggregate_id: str
    attempt_ids: tuple[str, ...] = ()
    materiality_result_id: str | None = None
    runtime_work_id: str | None = None


_RESOLVER_AUTHORITY = object()


@dataclass(frozen=True, slots=True, init=False)
class ResolvedRecoveryObservation:
    evaluation_run_id: str
    execution_id: str
    incident_id: str
    aggregate_id: str
    runtime_work_id: str | None
    clock_source: str
    state: RcaPublicState
    materiality: Materiality | None
    runtime_operation_id: str | None
    runtime_terminal: bool
    observed_at_utc: str
    observed_ns: int
    observation_commitment: str
    _authority: object

    @classmethod
    def _resolved(
        cls, reference: RecoveryStateReference, clock_source: str,
        state: RcaPublicState, materiality: Materiality | None,
        runtime_operation_id: str | None, runtime_terminal: bool,
        observed_at_utc: str, observed_ns: int,
    ) -> "ResolvedRecoveryObservation":
        value = object.__new__(cls)
        facts = {
            "state": state, "materiality": materiality.value if materiality else None,
            "runtime_operation_id": runtime_operation_id,
            "runtime_terminal": runtime_terminal,
            "observed_at_utc": observed_at_utc, "observed_ns": observed_ns,
        }
        for name, item in (
            ("evaluation_run_id", reference.evaluation_run_id),
            ("execution_id", reference.execution_id),
            ("incident_id", reference.incident_id),
            ("aggregate_id", reference.aggregate_id),
            ("runtime_work_id", reference.runtime_work_id),
            ("clock_source", clock_source),
            ("state", state), ("materiality", materiality),
            ("runtime_operation_id", runtime_operation_id),
            ("runtime_terminal", runtime_terminal),
            ("observed_at_utc", observed_at_utc), ("observed_ns", observed_ns),
            ("observation_commitment", commitment({
                "evaluation_run_id": reference.evaluation_run_id,
                "execution_id": reference.execution_id,
                "incident_id": reference.incident_id,
                "aggregate_id": reference.aggregate_id,
                "runtime_work_id": reference.runtime_work_id,
                "clock_source": clock_source,
                "current": state.current_version_id,
                "freshness": state.freshness.value,
                "attempts": list(state.attempt_ids), "tries": list(state.try_outcome_ids),
                "versions": list(state.version_ids),
                "publications": list(state.publication_ids),
                "frontier": state.frontier_identity,
                "frontier_ordinal": state.frontier_ordinal,
                "authorized_tries": list(state.authorized_try_ids),
                "failed_tries": list(state.failed_try_ids),
                "try_attempt_lineage": [list(item) for item in state.try_attempt_lineage],
                "version_attempt_lineage": [list(item) for item in state.version_attempt_lineage],
                "publication_version_lineage": [list(item) for item in state.publication_version_lineage],
                "authoritative_version_history": list(state.authoritative_version_history),
                "semantic_effects": [list(item) for item in state.semantic_effects],
                "materiality": facts["materiality"],
                "runtime_operation_id": runtime_operation_id,
                "runtime_terminal": runtime_terminal,
                "observed_at_utc": observed_at_utc, "observed_ns": observed_ns,
            })), ("_authority", _RESOLVER_AUTHORITY),
        ):
            object.__setattr__(value, name, item)
        return value


class RecoveryPublicStateResolver:
    """Compose refresh/restart facts only from existing public read APIs."""

    def __init__(
        self, rca: RcaRecoveryReads, incidents: IncidentRecoveryReads,
        evidence: EvidenceRecoveryReads, runtime: RuntimeRecoveryReads,
        clock: RecoveryObservationClock, *, clock_source: str,
    ) -> None:
        self._rca = rca
        self._incidents = incidents
        self._evidence = evidence
        self._runtime = runtime
        self._clock = clock
        if not isinstance(clock_source, str) or not clock_source.strip():
            raise ValueError("clock_source must be explicit")
        self._clock_source = clock_source

    def resolve(self, reference: RecoveryStateReference) -> ResolvedRecoveryObservation:
        if not isinstance(reference, RecoveryStateReference):
            raise TypeError("resolver accepts only identity/reference input")
        if not all(isinstance(value, str) and value.strip() for value in (
                reference.evaluation_run_id, reference.execution_id,
                reference.incident_id, reference.aggregate_id)):
            raise ValueError("recovery context identities must be explicit")
        require_identity(reference.evaluation_run_id, "evaluation_run")
        require_identity(reference.execution_id, "execution")
        current = self._rca.get_current(reference.aggregate_id)
        if current is None:
            raise ValueError("authoritative Current is missing")
        current_fact = getattr(current, "current", None)
        version = getattr(current, "version", None)
        if (current_fact is None or version is None
                or current_fact.aggregate_id != reference.aggregate_id
                or version.aggregate_id != reference.aggregate_id
                or current_fact.current_version_id != version.version_id):
            raise ValueError("Current/Version public facts are contradictory")
        freshness = {
            CurrentFreshness.FRESH: Freshness.FRESH,
            CurrentFreshness.STALE: Freshness.STALE,
        }.get(current_fact.freshness)
        if freshness is None:
            raise ValueError("Current freshness is invalid")
        relationship = self._incidents.get_rca_relationship(reference.incident_id)
        if (relationship is None or relationship.incident_id != reference.incident_id
                or relationship.current_version_id != version.version_id):
            raise ValueError("Incident relationship contradicts Candidate-A Current")
        history = self._rca.get_version_history(reference.aggregate_id)
        if (not isinstance(history, tuple)
                or len({item.version_id for item in history}) != len(history)
                or sum(item.version_id == version.version_id for item in history) != 1):
            raise ValueError("authoritative Version history is missing or contradictory")
        freshness_lineage = self._rca.get_freshness_lineage(reference.aggregate_id)
        if (not isinstance(freshness_lineage, tuple) or not freshness_lineage
                or freshness_lineage[-1] != current_fact):
            raise ValueError("freshness lineage does not end at authoritative Current")

        attempt_ids = tuple(dict.fromkeys(
            tuple(item.attempt_id for item in history) + reference.attempt_ids))
        try_ids: list[str] = []
        authorized: list[str] = []
        failed: list[str] = []
        try_lineage: list[tuple[str, str]] = []
        for attempt_id in attempt_ids:
            read = self._rca.get_attempt_lineage(attempt_id)
            if read is None or read.attempt.lineage.attempt_id != attempt_id:
                raise ValueError("Attempt lineage reference is missing or contradictory")
            for outcome in read.try_outcomes:
                try_id = f"{outcome.identity.attempt_id}:{outcome.identity.try_ordinal}"
                try_ids.append(try_id); authorized.append(try_id)
                try_lineage.append((try_id, attempt_id))
                if outcome.result_kind is not LogicalTryResultKind.VALIDATED_RESULT:
                    failed.append(try_id)

        publication_ids: list[str] = []
        publication_lineage: list[tuple[str, str]] = []
        for item in history:
            publication = self._rca.get_publication_result(item.publication_operation_id)
            incident_publication = self._incidents.get_rca_publication_result(
                item.publication_operation_id)
            if publication is None:
                raise ValueError("Candidate-A publication result is missing")
            if (publication.target.target_version_id != item.version_id
                    or publication.target.aggregate_id != reference.aggregate_id):
                raise ValueError("publication lineage is contradictory")
            is_current = item.version_id == version.version_id
            if is_current and (
                publication.disposition is not PublicationDisposition.APPLIED
                or incident_publication is None
                or incident_publication.disposition
                    is not IncidentRcaPublicationDisposition.APPLIED
                or incident_publication.target_version_id != item.version_id
                or incident_publication.incident_id != reference.incident_id
                or publication.resulting_current_version_id != item.version_id
                or incident_publication.resulting_current_version_id != item.version_id
            ):
                raise ValueError("Current lacks coherent successful publication")
            if publication.disposition is PublicationDisposition.APPLIED:
                if (incident_publication is None
                        or incident_publication.target_version_id != item.version_id
                        or incident_publication.incident_id != reference.incident_id):
                    raise ValueError("applied publication lacks coherent Incident result")
                publication_ids.append(item.publication_operation_id)
                publication_lineage.append((item.publication_operation_id, item.version_id))
            elif (publication.disposition is PublicationDisposition.A_SIDE_COMMITTED
                  and incident_publication is not None):
                raise ValueError("A-side-only publication fabricated an Incident result")

        materiality = None
        if reference.materiality_result_id is not None:
            result = self._evidence.read_materiality_result(reference.materiality_result_id)
            if result is None or result.materiality_result_id != reference.materiality_result_id:
                raise ValueError("Materiality result is missing")
            materiality = {
                MaterialityJudgement.MATERIAL: Materiality.MATERIAL,
                MaterialityJudgement.NON_MATERIAL: Materiality.NON_MATERIAL,
                MaterialityJudgement.SAME: Materiality.NON_MATERIAL,
            }.get(result.judgement)
            if materiality is None:
                raise ValueError("Materiality authority is repair-required or incomplete")
            request = result.request
            if materiality is Materiality.MATERIAL:
                if (freshness is not Freshness.STALE
                        or request.candidate_revision_id
                            != current_fact.material_evidence_revision_basis
                        or len(freshness_lineage) < 2
                        or request.baseline_revision_id
                            != freshness_lineage[-2].material_evidence_revision_basis):
                    raise ValueError(
                        "Materiality result contradicts Current freshness basis")
            elif (request.baseline_revision_id
                  != current_fact.material_evidence_revision_basis):
                raise ValueError("NON_MATERIAL result contradicts Current basis")

        runtime_operation_id = None
        runtime_terminal = False
        if reference.runtime_work_id is not None:
            work = self._runtime.get(reference.runtime_work_id)
            if work is None or work.work_id != reference.runtime_work_id:
                raise ValueError("Runtime work reference is missing")
            runtime_operation_id = work.operation_id or work.workflow_operation_id
            if runtime_operation_id is None:
                raise ValueError("Runtime work lacks an operation identity")
            runtime_terminal = work.status in {
                RuntimeWorkStatus.COMPLETED, RuntimeWorkStatus.EXHAUSTED,
                RuntimeWorkStatus.FAILED_CLOSED,
            }

        state = RcaPublicState(
            version.version_id, freshness, attempt_ids, tuple(try_ids),
            tuple(item.version_id for item in history), tuple(publication_ids),
            commitment({
                "current": version.version_id,
                "freshness": freshness.value,
                "attempts": list(attempt_ids), "tries": try_ids,
                "versions": [item.version_id for item in history],
                "publications": publication_ids,
            }),
            len(try_ids) + len(history) + len(publication_ids),
            tuple(authorized), tuple(failed), tuple(try_lineage),
            tuple((item.version_id, item.attempt_id) for item in history),
            tuple(publication_lineage), tuple(item.version_id for item in history),
            tuple(
                [(RecoveryBoundary.GENERATION.value, item, runtime_operation_id or item)
                 for item in try_ids]
                + [(RecoveryBoundary.ARTIFACT_COMMIT.value, item.version_id,
                    runtime_operation_id or item.version_id) for item in history]
                + [(RecoveryBoundary.PUBLICATION.value, item,
                    runtime_operation_id or item) for item in publication_ids]
            ),
        )
        return ResolvedRecoveryObservation._resolved(
            reference, self._clock_source, state, materiality,
            runtime_operation_id, runtime_terminal,
            self._clock.utc_now(), self._clock.monotonic_ns(),
        )


# Approved R2-facing name; this alias does not introduce another resolver.
RcaPublicStateResolver = RecoveryPublicStateResolver


def _authoritative(value: ResolvedRecoveryObservation) -> RcaPublicState:
    if (type(value) is not ResolvedRecoveryObservation
            or value._authority is not _RESOLVER_AUTHORITY):
        raise TypeError("refresh/restart evaluation requires resolver-owned public facts")
    return value.state


def _require_same_context(
    *observations: ResolvedRecoveryObservation, require_operation: bool = False,
) -> None:
    if not observations:
        raise ValueError("recovery observations are required")
    for item in observations:
        _authoritative(item)
    contexts = {
        (
            item.evaluation_run_id, item.execution_id, item.incident_id,
            item.aggregate_id, item.clock_source,
        )
        for item in observations
    }
    if len(contexts) != 1:
        raise ValueError("recovery observations belong to different evaluation contexts")
    if require_operation:
        operation_contexts = {
            (item.runtime_work_id, item.runtime_operation_id) for item in observations
        }
        if len(operation_contexts) != 1 or None in next(iter(operation_contexts)):
            raise ValueError("restart observations do not bind the same operation/work")
    timestamps = tuple(item.observed_ns for item in observations)
    if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
        raise ValueError("recovery observations are not monotonic")


@dataclass(frozen=True, slots=True)
class RefreshEvaluation:
    passed: bool
    disposition: str
    details: tuple[str, ...] = ()


def _one_new(before: tuple[str, ...], after: tuple[str, ...]) -> tuple[str, ...]:
    if not set(before).issubset(after) or len(after) != len(set(after)):
        return ()
    return tuple(item for item in after if item not in before)


def _evaluate_refresh_states(
    *, before: RcaPublicState, after_materiality: RcaPublicState,
    final: RcaPublicState, materiality: Materiality, refresh_succeeded: bool,
) -> RefreshEvaluation:
    findings: list[str] = []
    if before.current_version_id not in before.version_ids:
        findings.append("existing v1 Current is absent from Version history")
    if before.freshness is not Freshness.FRESH:
        findings.append("v1 was not initially CURRENT/FRESH")
    if materiality is Materiality.NON_MATERIAL:
        if after_materiality != before or final != before:
            findings.append("NON_MATERIAL changed semantic state or frontier")
        return RefreshEvaluation(not findings, "NON_MATERIAL_NO_SEMANTIC_ACTION", tuple(findings))

    if after_materiality.current_version_id != before.current_version_id:
        findings.append("Material Evidence changed Current before refresh publication")
    if after_materiality.freshness is not Freshness.STALE:
        findings.append("Material Evidence did not expose v1 STALE")
    for label in ("attempt_ids", "try_outcome_ids", "version_ids", "publication_ids"):
        if getattr(after_materiality, label) != getattr(before, label):
            findings.append(f"Material Evidence created {label} before refresh")
    if refresh_succeeded:
        new_attempts = _one_new(before.attempt_ids, final.attempt_ids)
        new_tries = _one_new(before.try_outcome_ids, final.try_outcome_ids)
        new_versions = _one_new(before.version_ids, final.version_ids)
        new_publications = _one_new(before.publication_ids, final.publication_ids)
        if not all(len(items) == 1 for items in (
                new_attempts, new_tries, new_versions, new_publications)):
            findings.append("material refresh did not add exactly one Attempt/Try/Version/publication")
        else:
            attempt_id, try_id = new_attempts[0], new_tries[0]
            version_id, publication_id = new_versions[0], new_publications[0]
            if (try_id, attempt_id) not in final.try_attempt_lineage:
                findings.append("new Try does not belong to the new Attempt")
            if try_id not in final.authorized_try_ids:
                findings.append("new Try lacks authorized lineage")
            if (version_id, attempt_id) not in final.version_attempt_lineage:
                findings.append("new Version does not belong to the new Attempt")
            if (publication_id, version_id) not in final.publication_version_lineage:
                findings.append("publication does not target the new Version")
            if final.current_version_id != version_id:
                findings.append("Current does not point to the published Version")
            if version_id not in final.authoritative_version_history:
                findings.append("new Version is absent from authoritative history")
        if final.freshness is not Freshness.FRESH:
            findings.append("successful refresh did not finish CURRENT/FRESH")
        if final.frontier_ordinal <= after_materiality.frontier_ordinal:
            findings.append("successful refresh frontier did not advance")
        disposition = "REFRESHED_CURRENT_FRESH"
    else:
        if final.current_version_id != before.current_version_id:
            findings.append("failed refresh did not retain last-known-good Current")
        if final.freshness is not Freshness.STALE:
            findings.append("failed refresh fabricated FRESH state")
        if final.version_ids != before.version_ids or final.publication_ids != before.publication_ids:
            findings.append("failed refresh fabricated a successful Version/publication")
        new_attempts = _one_new(before.attempt_ids, final.attempt_ids)
        new_tries = _one_new(before.try_outcome_ids, final.try_outcome_ids)
        if new_attempts or new_tries:
            if len(new_attempts) != 1 or len(new_tries) != 1:
                findings.append("failed refresh Attempt/Try facts are inconsistent")
            else:
                try_id, attempt_id = new_tries[0], new_attempts[0]
                if ((try_id, attempt_id) not in final.try_attempt_lineage
                        or try_id not in final.failed_try_ids
                        or try_id not in final.authorized_try_ids):
                    findings.append("failed refresh Try lineage/outcome is inconsistent")
                if final.frontier_ordinal != after_materiality.frontier_ordinal + 1:
                    findings.append("failed refresh frontier does not match failed Try")
        elif final.frontier_ordinal != after_materiality.frontier_ordinal:
            findings.append("failed refresh frontier advanced without a typed Try outcome")
        disposition = "REFRESH_FAILED_CURRENT_RETAINED_STALE"
    return RefreshEvaluation(not findings, disposition, tuple(findings))


def evaluate_refresh(
    *, before: ResolvedRecoveryObservation,
    after_materiality: ResolvedRecoveryObservation,
    final: ResolvedRecoveryObservation,
) -> RefreshEvaluation:
    _require_same_context(
        before, after_materiality, final, require_operation=True)
    before_state = _authoritative(before)
    materiality_state = _authoritative(after_materiality)
    final_state = _authoritative(final)
    materiality = after_materiality.materiality
    if materiality is None:
        return RefreshEvaluation(False, "INVALID_PUBLIC_OBSERVATION", (
            "Materiality result was not resolved from Candidate-B public authority",
        ))
    return _evaluate_refresh_states(
        before=before_state, after_materiality=materiality_state, final=final_state,
        materiality=materiality,
        refresh_succeeded=(
            final_state.current_version_id != before_state.current_version_id
            and final_state.freshness is Freshness.FRESH
        ),
    )


@dataclass(frozen=True, slots=True)
class RestartEvaluation:
    passed: bool
    disposition: str
    details: tuple[str, ...] = ()


def verify_report_case_payload(kind: str, payload: object) -> tuple[str, ...]:
    """Validate the canonical report projection of already-evaluated S4 facts."""
    if not isinstance(payload, dict):
        return ("RECOVERY_REPORT_FACT_NOT_OBJECT",)
    findings = []
    common = {"case_id", "disposition", "before_state", "resulting_state",
              "duplicate_effect_detected", "latency_disposition"}
    if any(payload.get(name) is None for name in common):
        findings.append("RECOVERY_REPORT_FACT_INCOMPLETE")
    if type(payload.get("duplicate_effect_detected")) is not bool:
        findings.append("RECOVERY_DUPLICATE_OUTCOME_INVALID")
    if kind == "REFRESH_VERSION_RESULT":
        if payload.get("disposition") not in {
                "NON_MATERIAL_NO_SEMANTIC_ACTION", "REFRESHED_CURRENT_FRESH",
                "REFRESH_FAILED_CURRENT_RETAINED_STALE"}:
            findings.append("REFRESH_DISPOSITION_INVALID")
        for name in ("trigger_basis", "freshness_transition",
                     "prior_current_preserved"):
            if payload.get(name) is None:
                findings.append(f"REFRESH_{name.upper()}_MISSING")
        if type(payload.get("prior_current_preserved")) is not bool:
            findings.append("REFRESH_PRIOR_CURRENT_OUTCOME_INVALID")
        if (payload.get("disposition") == "REFRESHED_CURRENT_FRESH"
                and (payload.get("freshness_transition") != "FRESH_TO_STALE_TO_FRESH"
                     or payload.get("duplicate_effect_detected") is not False)):
            findings.append("REFRESH_SUCCESS_SEMANTICS_INVALID")
        if (payload.get("disposition") == "REFRESH_FAILED_CURRENT_RETAINED_STALE"
                and payload.get("prior_current_preserved") is not True):
            findings.append("FAILED_REFRESH_DID_NOT_PRESERVE_CURRENT")
    elif kind == "RESTART_RECOVERY_RESULT":
        if payload.get("disposition") not in {
                "CONVERGED", "FAILED", "RECOVERY_AMBIGUOUS"}:
            findings.append("RESTART_DISPOSITION_INVALID")
        for name in ("recovered_identity", "publication_continuity"):
            if payload.get(name) is None:
                findings.append(f"RESTART_{name.upper()}_MISSING")
        if (payload.get("disposition") == "CONVERGED"
                and payload.get("duplicate_effect_detected") is not False):
            findings.append("RESTART_DUPLICATE_EFFECT_INVALID")
    else:
        findings.append("RECOVERY_REPORT_KIND_INVALID")
    return tuple(sorted(set(findings)))


def _evaluate_restart_states(
    *, before: RcaPublicState, after: RcaPublicState,
    expected_operation_id: str, completed_operation_id: str | None,
    boundary: RecoveryBoundary, signal_injection_count: int,
    publication_reconciled_before_resume: bool,
    recovery_state_ambiguous: bool = False,
) -> RestartEvaluation:
    if recovery_state_ambiguous:
        return RestartEvaluation(False, "RECOVERY_AMBIGUOUS", (
            "ambiguous restart state failed closed without signal replay",
        ))
    findings: list[str] = []
    if completed_operation_id != expected_operation_id:
        findings.append("same operation identity did not converge")
    if signal_injection_count != 1:
        findings.append("scenario signal was duplicated")
    if not publication_reconciled_before_resume:
        findings.append("publication was not reconciled before rerun")
    before_effects = {item[1]: item for item in before.semantic_effects}
    after_effects = {item[1]: item for item in after.semantic_effects}
    if len(before_effects) != len(before.semantic_effects) or len(after_effects) != len(after.semantic_effects):
        findings.append("duplicate semantic effect identity observed")
    if any(after_effects.get(identity) != effect for identity, effect in before_effects.items()):
        findings.append("pre-restart frontier identity/effect was not preserved")
    additions = tuple(effect for identity, effect in after_effects.items()
                      if identity not in before_effects)
    if len(additions) > 1:
        findings.append("restart produced more than one planned semantic effect")
    elif additions:
        effect = additions[0]
        if effect[0] != boundary.value or effect[2] != expected_operation_id:
            findings.append("frontier transition is not authorized for the crash boundary/operation")
    if after.frontier_ordinal != before.frontier_ordinal + len(additions):
        findings.append("frontier did not preserve or advance exactly with the authorized effect")
    deltas = {
        "Attempt": _one_new(before.attempt_ids, after.attempt_ids),
        "Try outcome": _one_new(before.try_outcome_ids, after.try_outcome_ids),
        "Version": _one_new(before.version_ids, after.version_ids),
        "publication": _one_new(before.publication_ids, after.publication_ids),
    }
    expected_delta = {
        RecoveryBoundary.GENERATION: "Try outcome",
        RecoveryBoundary.ARTIFACT_COMMIT: "Version",
        RecoveryBoundary.PUBLICATION: "publication",
    }.get(boundary)
    if deltas["Attempt"]:
        findings.append("restart created a second Attempt")
    for label in ("Try outcome", "Version", "publication"):
        allowed = label == expected_delta
        if (allowed and additions and (len(deltas[label]) != 1
                                       or deltas[label][0] != additions[0][1])):
            findings.append(f"{boundary.value} completion did not converge one {label} identity")
        if not allowed and deltas[label]:
            findings.append(f"restart created an unauthorized {label}")
    for label, values in (
        ("Attempt", after.attempt_ids), ("Try outcome", after.try_outcome_ids),
        ("Version", after.version_ids), ("publication", after.publication_ids),
    ):
        if len(values) != len(set(values)):
            findings.append(f"duplicate {label} identity/effect")
    for label, old, new in (
        ("Attempt", before.attempt_ids, after.attempt_ids),
        ("Try outcome", before.try_outcome_ids, after.try_outcome_ids),
        ("Version", before.version_ids, after.version_ids),
        ("publication", before.publication_ids, after.publication_ids),
    ):
        if not set(old).issubset(new):
            findings.append(f"pre-restart {label} identity was not preserved")
    return RestartEvaluation(not findings, "CONVERGED" if not findings else "FAILED", tuple(findings))


def evaluate_restart(
    *, before: ResolvedRecoveryObservation, after: ResolvedRecoveryObservation,
    expected_operation_id: str, boundary: RecoveryBoundary,
    signal_injection_count: int, publication_reconciled_before_resume: bool,
    recovery_state_ambiguous: bool = False,
) -> RestartEvaluation:
    _require_same_context(before, after, require_operation=True)
    if (before.runtime_operation_id != expected_operation_id
            or after.runtime_operation_id != expected_operation_id):
        return RestartEvaluation(False, "FAILED", (
            "expected operation is not bound to both restart observations",
        ))
    before_state = _authoritative(before)
    after_state = _authoritative(after)
    return _evaluate_restart_states(
        before=before_state, after=after_state,
        expected_operation_id=expected_operation_id,
        completed_operation_id=after.runtime_operation_id, boundary=boundary,
        signal_injection_count=signal_injection_count,
        publication_reconciled_before_resume=publication_reconciled_before_resume,
        recovery_state_ambiguous=(recovery_state_ambiguous or not after.runtime_terminal),
    )


def refresh_latency_measurement(
    start: ResolvedRecoveryObservation, end: ResolvedRecoveryObservation, *,
    disposition: LatencyDisposition, polling_interval_ms: int, cutoff_ms: int,
    clock_source: str,
) -> LatencyMeasurement:
    _require_same_context(start, end, require_operation=True)
    if start.clock_source != clock_source:
        raise ValueError("refresh latency clock source contradicts observation context")
    duration = None
    ended_at = None
    if disposition is LatencyDisposition.OBSERVED_COMPLETION:
        if (start.materiality is not Materiality.MATERIAL
                or start.state.freshness is not Freshness.STALE
                or end.state.freshness is not Freshness.FRESH
                or end.state.current_version_id == start.state.current_version_id):
            raise ValueError("refresh latency endpoints are not authoritative STALE→new FRESH")
        duration = (end.observed_ns - start.observed_ns) // 1_000_000
        ended_at = end.observed_at_utc
    return LatencyMeasurement(
        LatencyKind.REFRESH, disposition,
        "MATERIAL_EVIDENCE_PUBLIC_STALE_OBSERVED",
        "NEW_CURRENT_FRESH_PUBLICATION_OBSERVED", clock_source,
        polling_interval_ms, cutoff_ms, start.observed_at_utc, ended_at, duration,
    )


def restart_latency_measurement(
    start: ResolvedRecoveryObservation, end: ResolvedRecoveryObservation, *,
    expected_operation_id: str, disposition: LatencyDisposition,
    polling_interval_ms: int, cutoff_ms: int, clock_source: str,
) -> LatencyMeasurement:
    _require_same_context(start, end, require_operation=True)
    if start.clock_source != clock_source:
        raise ValueError("restart latency clock source contradicts observation context")
    if start.runtime_operation_id != expected_operation_id:
        raise ValueError("restart latency start lacks expected operation identity")
    duration = None
    ended_at = None
    if disposition is LatencyDisposition.OBSERVED_COMPLETION:
        if (not end.runtime_terminal
                or end.runtime_operation_id != expected_operation_id):
            raise ValueError("restart latency endpoint lacks same-operation convergence")
        duration = (end.observed_ns - start.observed_ns) // 1_000_000
        ended_at = end.observed_at_utc
    return LatencyMeasurement(
        LatencyKind.RESTART_RECOVERY, disposition,
        "RESTART_RECOVERY_OBSERVATION_STARTED",
        "SAME_OPERATION_TERMINAL_CONVERGENCE_OBSERVED", clock_source,
        polling_interval_ms, cutoff_ms, start.observed_at_utc, ended_at, duration,
    )
