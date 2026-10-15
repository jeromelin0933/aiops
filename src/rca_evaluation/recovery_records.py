"""Provenance-sealed Candidate-F recovery and deviation issuance."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .identity import commitment
from .ledger import LedgerRecord
from .recovery_evaluation import (
    RecoveryBoundary, ResolvedRecoveryObservation, _authoritative,
    evaluate_refresh, evaluate_restart,
)

if TYPE_CHECKING:
    from .sqlite_store import SqliteEvaluationStore

PROTECTED_RECOVERY_KINDS = frozenset({
    "DEVIATION_RESULT", "REFRESH_VERSION_RESULT", "RESTART_RECOVERY_RESULT",
    "RECOVERY_SOURCE_OBSERVATION", "RECOVERY_EVALUATOR_EXECUTION",
})
RECOVERY_EVALUATOR_IDENTITY = "SPEC-017-R2S4-GOVERNED-EVALUATOR-v3"
DEVIATION_EVALUATOR_IDENTITY = "SPEC-017-R2S4-DEVIATION-EVALUATOR-v3"
_RECOVERY_ISSUANCE_AUTHORITY = object()


class ValidatedRecoveryResult:
    """Retired caller-facing carrier; retained only as a fail-closed API."""
    def __new__(cls, *args, **kwargs):
        raise TypeError("caller-created recovery results are not issuable")


class ValidatedDeviationResult:
    """Retired caller-facing carrier; retained only as a fail-closed API."""
    def __new__(cls, *args, **kwargs):
        raise TypeError("caller-created deviation results are not issuable")


def _require_store(store: object) -> "SqliteEvaluationStore":
    from .sqlite_store import SqliteEvaluationStore
    if type(store) is not SqliteEvaluationStore:
        raise TypeError("protected issuance requires the Candidate-F store")
    return store


def _projection(value: ResolvedRecoveryObservation) -> dict[str, object]:
    state = _authoritative(value)
    result = {
        "evaluation_run_id": value.evaluation_run_id,
        "execution_id": value.execution_id,
        "incident_id": value.incident_id, "aggregate_id": value.aggregate_id,
        "runtime_work_id": value.runtime_work_id,
        "clock_source": value.clock_source,
        "current": state.current_version_id, "freshness": state.freshness.value,
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
        "materiality": value.materiality.value if value.materiality else None,
        "runtime_operation_id": value.runtime_operation_id,
        "runtime_terminal": value.runtime_terminal,
        "observed_at_utc": value.observed_at_utc,
        "observed_ns": value.observed_ns,
    }
    if commitment(result) != value.observation_commitment:
        raise ValueError("resolver observation commitment is contradictory")
    return result


def _record_source(store, *, case_id, parent_execution_id, ordinal, observation):
    projection = _projection(observation)
    run = store._run_for(parent_execution_id)
    if observation.execution_id != parent_execution_id:
        raise ValueError("cross-execution source observation rejected")
    if observation.evaluation_run_id != run.record_id:
        raise ValueError("cross-run source observation rejected")
    payload = {
        "kind": "RECOVERY_SOURCE_OBSERVATION", "case_id": case_id,
        "evaluation_run_id": run.record_id, "execution_id": parent_execution_id,
        "source_ordinal": ordinal,
        "source_observation_commitment": observation.observation_commitment,
        "observation_projection": projection,
    }
    record = LedgerRecord.create(
        "observation", [parent_execution_id, "recovery-source", case_id,
                        ordinal, observation.observation_commitment],
        payload, parent_execution_id)
    return store._append_protected_recovery(
        record, authority=_RECOVERY_ISSUANCE_AUTHORITY)


def _record_control_source(store, *, case_id, parent_execution_id, ordinal,
                           projection):
    run = store._run_for(parent_execution_id)
    payload = {
        "kind": "RECOVERY_SOURCE_OBSERVATION", "case_id": case_id,
        "evaluation_run_id": run.record_id, "execution_id": parent_execution_id,
        "source_ordinal": ordinal,
        "source_observation_commitment": commitment(projection),
        "observation_projection": projection,
    }
    record = LedgerRecord.create(
        "observation", [parent_execution_id, "recovery-control-source",
                        case_id, ordinal, commitment(projection)],
        payload, parent_execution_id)
    return store._append_protected_recovery(
        record, authority=_RECOVERY_ISSUANCE_AUTHORITY)


def _seal_execution(store, *, kind, case_id, parent_execution_id,
                    fixture_identity, source_records, result_payload):
    source_ids = [item.record_id for item in source_records]
    source_hashes = [item.semantic_commitment for item in source_records]
    operation = commitment({
        "evaluator": RECOVERY_EVALUATOR_IDENTITY, "kind": kind,
        "case_id": case_id, "execution_id": parent_execution_id,
        "sources": source_ids,
    })
    payload = {
        "kind": "RECOVERY_EVALUATOR_EXECUTION",
        "evaluator_identity": RECOVERY_EVALUATOR_IDENTITY,
        "evaluator_version": "3", "evaluation_operation_identity": operation,
        "case_id": case_id, "execution_id": parent_execution_id,
        "fixture_identity": fixture_identity, "result_kind": kind,
        "source_observation_record_ids": source_ids,
        "source_observation_record_commitments": source_hashes,
        "result_payload": result_payload,
        "result_commitment": commitment(result_payload),
    }
    record = LedgerRecord.create(
        "observation", [parent_execution_id, "recovery-evaluator", case_id,
                        operation], payload, parent_execution_id)
    return store._append_protected_recovery(
        record, authority=_RECOVERY_ISSUANCE_AUTHORITY)


def _issue_from_execution(store, evaluator_execution_id):
    execution = store.get(evaluator_execution_id)
    if execution is None:
        raise ValueError("evaluator execution does not exist")
    provenance = json.loads(execution.semantic_json)
    if (provenance.get("kind") != "RECOVERY_EVALUATOR_EXECUTION"
            or provenance.get("evaluator_identity") != RECOVERY_EVALUATOR_IDENTITY
            or provenance.get("result_commitment")
                != commitment(provenance.get("result_payload"))):
        raise ValueError("recovery evaluator provenance is invalid")
    ids = provenance.get("source_observation_record_ids")
    hashes = provenance.get("source_observation_record_commitments")
    if (not isinstance(ids, list) or not isinstance(hashes, list)
            or len(ids) != len(hashes) or not 2 <= len(ids) <= 8):
        raise ValueError("evaluator source lineage is incomplete")
    observation_hashes = []
    for source_id, expected_hash in zip(ids, hashes):
        source = store.get(source_id)
        if (source is None or source.parent_id != execution.parent_id
                or source.semantic_commitment != expected_hash):
            raise ValueError("source observation is missing or has wrong commitment")
        payload = json.loads(source.semantic_json)
        projection = payload.get("observation_projection")
        if (payload.get("kind") != "RECOVERY_SOURCE_OBSERVATION"
                or payload.get("case_id") != provenance.get("case_id")
                or payload.get("execution_id") != execution.parent_id
                or payload.get("source_observation_commitment")
                    != commitment(projection)):
            raise ValueError("source observation binding is invalid")
        observation_hashes.append(payload["source_observation_commitment"])
    result = dict(provenance["result_payload"])
    if (result.get("kind") != provenance.get("result_kind")
            or result.get("case_id") != provenance.get("case_id")
            or result.get("fixture_identity") != provenance.get("fixture_identity")
            or result.get("public_observation_commitments") != observation_hashes):
        raise ValueError("evaluator result binding is invalid")
    result.update({
        "governed_evaluator_identity": RECOVERY_EVALUATOR_IDENTITY,
        "evaluator_execution_record_id": execution.record_id,
        "evaluator_execution_commitment": execution.semantic_commitment,
        "typed_result_commitment": commitment(provenance["result_payload"]),
    })
    record = LedgerRecord.create(
        "observation", [execution.parent_id, "protected-recovery-result",
                        provenance["case_id"], execution.record_id],
        result, execution.parent_id)
    return store._append_protected_recovery(
        record, authority=_RECOVERY_ISSUANCE_AUTHORITY)


def evaluate_and_issue_refresh_result(store, *, case_id, parent_execution_id,
                                      fixture_identity, before,
                                      after_materiality, final, payload):
    store = _require_store(store)
    evaluation = evaluate_refresh(
        before=before, after_materiality=after_materiality, final=final)
    values = (before, after_materiality, final)
    sources = tuple(_record_source(
        store, case_id=case_id, parent_execution_id=parent_execution_id,
        ordinal=index, observation=value)
        for index, value in enumerate(values, 1))
    canonical = dict(payload)
    canonical.update({
        "kind": "REFRESH_VERSION_RESULT", "case_id": case_id,
        "fixture_identity": fixture_identity, "passed": evaluation.passed,
        "disposition": evaluation.disposition, "details": list(evaluation.details),
        "public_observation_commitments": [v.observation_commitment for v in values],
    })
    execution = _seal_execution(
        store, kind="REFRESH_VERSION_RESULT", case_id=case_id,
        parent_execution_id=parent_execution_id,
        fixture_identity=fixture_identity, source_records=sources,
        result_payload=canonical)
    return _issue_from_execution(store, execution.record_id)


def record_refresh_failure_observation(
    store, *, case_id, parent_execution_id, evidence, candidate_d,
    candidate_a, runtime, materiality_result_id, aggregate_id,
    prior_current_version_id, expected_version_ids, runtime_work_id,
):
    """Capture a governed failure fact only from Candidate B/D/A public reads."""
    store = _require_store(store)
    materiality = evidence.read_materiality_result(materiality_result_id)
    recovery = candidate_d.recovery()
    current = candidate_a.get_current(aggregate_id)
    history = candidate_a.get_version_history(aggregate_id)
    work = runtime.get(runtime_work_id)
    if (materiality is None or materiality.judgement.value != "MATERIAL"):
        raise ValueError("refresh failure observation lacks MATERIAL authority")
    failures = tuple(getattr(getattr(recovery, "value", None), "failures", ()))
    if len(failures) != 1:
        raise ValueError("refresh failure observation lacks one Candidate-D failure")
    failure = failures[0]
    attempt_id = failure.try_identity.attempt_id
    lineage = candidate_a.get_attempt_lineage(attempt_id)
    if (lineage is None or lineage.attempt.lineage.attempt_id != attempt_id
            or any(outcome.result_kind.value == "VALIDATED_RESULT"
                   for outcome in lineage.try_outcomes)):
        raise ValueError("Candidate-D failure lacks authoritative Attempt identity")
    version_ids = tuple(item.version_id for item in history)
    if (current is None or current.version.version_id != prior_current_version_id
            or current.current.freshness.value != "STALE"
            or version_ids != tuple(expected_version_ids)):
        raise ValueError("Current/Version refresh failure state is contradictory")
    operation_id = getattr(work, "operation_id", None) or getattr(
        work, "workflow_operation_id", None)
    if work is None or not operation_id:
        raise ValueError("refresh failure lacks Runtime work identity")
    materiality_projection = {
        "materiality_result_id": materiality.materiality_result_id,
        "baseline_revision_id": materiality.request.baseline_revision_id,
        "candidate_revision_id": materiality.request.candidate_revision_id,
        "rule_version": materiality.request.materiality_rule_version,
        "judgement": materiality.judgement.value,
        "reason_facts": list(materiality.reason_facts),
    }
    failure_projection = {
        "attempt_id": attempt_id,
        "try_ordinal": failure.try_identity.try_ordinal,
        "operation_id": failure.operation_id,
        "disposition": failure.failure_class.value,
        "retry_safety": failure.retry_safety.value,
        "subject_input_commitment": failure.subject_input_commitment,
        "candidate_a_try_outcome_count": len(lineage.try_outcomes),
    }
    current_projection = {
        "aggregate_id": aggregate_id,
        "prior_current_version_id": prior_current_version_id,
        "current_version_id": current.version.version_id,
        "freshness": current.current.freshness.value,
        "version_ids": list(version_ids),
        "publication_operation_ids": [
            item.publication_operation_id for item in history],
    }
    projection = {
        "observation_kind": "REFRESH_FAILURE_DOMAIN_OBSERVATION",
        "materiality": materiality_projection,
        "materiality_commitment": commitment(materiality_projection),
        "runtime_work_id": runtime_work_id,
        "runtime_operation_id": operation_id,
        "refresh_attempted": True,
        "candidate_d_failure": failure_projection,
        "candidate_d_failure_commitment": commitment(failure_projection),
        "current_version": current_projection,
        "current_version_commitment": commitment(current_projection),
        "new_version": False, "new_current": False,
        "prior_current_retained": True, "publication_effect": "NONE",
    }
    return _record_control_source(
        store, case_id=case_id, parent_execution_id=parent_execution_id,
        ordinal=4, projection=projection)


def _validated_refresh_failure_projection(source, *, case_id,
                                          parent_execution_id):
    if source is None or source.parent_id != parent_execution_id:
        raise ValueError("refresh failure source observation is missing")
    payload = json.loads(source.semantic_json)
    projection = payload.get("observation_projection")
    if (payload.get("kind") != "RECOVERY_SOURCE_OBSERVATION"
            or payload.get("case_id") != case_id
            or payload.get("execution_id") != parent_execution_id
            or payload.get("source_observation_commitment")
                != commitment(projection)
            or not isinstance(projection, dict)
            or projection.get("observation_kind")
                != "REFRESH_FAILURE_DOMAIN_OBSERVATION"):
        raise ValueError("refresh failure source observation binding is invalid")
    materiality = projection.get("materiality")
    failure = projection.get("candidate_d_failure")
    current = projection.get("current_version")
    if (not all(isinstance(item, dict)
                for item in (materiality, failure, current))
            or projection.get("materiality_commitment") != commitment(materiality)
            or materiality.get("judgement") != "MATERIAL"
            or projection.get("candidate_d_failure_commitment")
                != commitment(failure)
            or failure.get("disposition") != "MALFORMED_PROVIDER_OUTPUT"
            or projection.get("current_version_commitment")
                != commitment(current)
            or current.get("freshness") != "STALE"
            or current.get("current_version_id")
                != current.get("prior_current_version_id")
            or projection.get("refresh_attempted") is not True
            or projection.get("new_version") is not False
            or projection.get("new_current") is not False
            or projection.get("prior_current_retained") is not True
            or projection.get("publication_effect") != "NONE"):
        raise ValueError("refresh failure domain observation is contradictory")
    return payload, projection


def evaluate_and_issue_refresh_failure_result(
    store, *, case_id, parent_execution_id, fixture_identity,
    failure_observation_record_id, before, after_materiality, final, payload,
):
    """Reuse the refresh evaluator and seal its actual failure-domain source."""
    store = _require_store(store)
    source = store.get(failure_observation_record_id)
    source_payload, domain = _validated_refresh_failure_projection(
        source, case_id=case_id, parent_execution_id=parent_execution_id)
    evaluation = evaluate_refresh(
        before=before, after_materiality=after_materiality, final=final)
    if (not evaluation.passed
            or evaluation.disposition != "REFRESH_FAILED_CURRENT_RETAINED_STALE"
            or after_materiality.materiality.value != "MATERIAL"
            or final.state.current_version_id != before.state.current_version_id
            or final.state.freshness.value != "STALE"
            or domain["current_version"]["current_version_id"]
                != final.state.current_version_id):
        raise ValueError("governed refresh evaluator did not prove failure semantics")
    values = (before, after_materiality, final)
    observation_sources = tuple(_record_source(
        store, case_id=case_id, parent_execution_id=parent_execution_id,
        ordinal=index, observation=value)
        for index, value in enumerate(values, 1))
    canonical = dict(payload)
    canonical.update({
        "kind": "REFRESH_VERSION_RESULT", "case_id": case_id,
        "fixture_identity": fixture_identity, "passed": True,
        "disposition": evaluation.disposition,
        "details": list(evaluation.details), "materiality": "MATERIAL",
        "materiality_result_id": domain["materiality"]["materiality_result_id"],
        "materiality_result_commitment": domain["materiality_commitment"],
        "candidate_d_failure_identity": commitment(domain["candidate_d_failure"]),
        "candidate_d_failure_disposition": domain["candidate_d_failure"]["disposition"],
        "candidate_d_failure_commitment": domain["candidate_d_failure_commitment"],
        "refresh_attempted": True, "new_version": False,
        "new_current": False, "prior_current_retained": True,
        "prior_current_preserved": True,
        "prior_current_freshness": "STALE", "publication_effect": "NONE",
        "public_observation_commitments": [
            value.observation_commitment for value in values]
            + [source_payload["source_observation_commitment"]],
    })
    execution = _seal_execution(
        store, kind="REFRESH_VERSION_RESULT", case_id=case_id,
        parent_execution_id=parent_execution_id,
        fixture_identity=fixture_identity,
        source_records=observation_sources + (source,),
        result_payload=canonical)
    return _issue_from_execution(store, execution.record_id)


def evaluate_and_issue_restart_result(store, *, case_id, parent_execution_id,
                                      fixture_identity, before, after,
                                      expected_operation_id, boundary,
                                      signal_injection_count,
                                      publication_reconciled_before_resume,
                                      payload, recovery_state_ambiguous=False):
    store = _require_store(store)
    evaluation = evaluate_restart(
        before=before, after=after, expected_operation_id=expected_operation_id,
        boundary=boundary, signal_injection_count=signal_injection_count,
        publication_reconciled_before_resume=publication_reconciled_before_resume,
        recovery_state_ambiguous=recovery_state_ambiguous)
    values = (before, after)
    sources = tuple(_record_source(
        store, case_id=case_id, parent_execution_id=parent_execution_id,
        ordinal=index, observation=value)
        for index, value in enumerate(values, 1))
    canonical = dict(payload)
    canonical.update({
        "kind": "RESTART_RECOVERY_RESULT", "case_id": case_id,
        "fixture_identity": fixture_identity, "passed": evaluation.passed,
        "disposition": evaluation.disposition, "details": list(evaluation.details),
        "public_observation_commitments": [v.observation_commitment for v in values],
    })
    execution = _seal_execution(
        store, kind="RESTART_RECOVERY_RESULT", case_id=case_id,
        parent_execution_id=parent_execution_id,
        fixture_identity=fixture_identity, source_records=sources,
        result_payload=canonical)
    return _issue_from_execution(store, execution.record_id)


def evaluate_and_issue_checkpoint_recovery_result(
    store, *, case_id, parent_execution_id, fixture_identity,
    checkpoint, recovery, payload,
):
    """Issue restart truth only from adapter-owned checkpoint/recovery receipts."""
    from .evaluation_controls import (
        is_actual_checkpoint_receipt, is_actual_recovery_receipt,
    )
    store = _require_store(store)
    if (not is_actual_checkpoint_receipt(checkpoint)
            or not is_actual_recovery_receipt(recovery)
            or recovery.boundary_identity != checkpoint.boundary_identity
            or recovery.operation_identity != checkpoint.operation_identity
            or recovery.work_identity != checkpoint.work_identity
            or recovery.checkpoint_commitment
                != checkpoint.durable_facts_commitment
            or recovery.duplicate_effect_detected):
        raise ValueError("restart evaluator provenance is contradictory")
    projections = (
        {"phase": "PRE_CRASH", "boundary": checkpoint.boundary_identity,
         "operation_identity": checkpoint.operation_identity,
         "work_identity": checkpoint.work_identity,
         "facts": checkpoint.durable_facts,
         "facts_commitment": checkpoint.durable_facts_commitment},
        {"phase": "AFTER_RECOVERY", "boundary": recovery.boundary_identity,
         "operation_identity": recovery.operation_identity,
         "work_identity": recovery.work_identity,
         "facts": recovery.terminal_facts,
         "facts_commitment": recovery.terminal_facts_commitment,
         "duplicate_effect_detected": recovery.duplicate_effect_detected},
    )
    sources = tuple(_record_control_source(
        store, case_id=case_id, parent_execution_id=parent_execution_id,
        ordinal=index, projection=value)
        for index, value in enumerate(projections, 1))
    canonical = dict(payload)
    canonical.update({
        "kind": "RESTART_RECOVERY_RESULT", "case_id": case_id,
        "fixture_identity": fixture_identity, "passed": True,
        "disposition": "CONVERGED", "details": [],
        "boundary_identity": checkpoint.boundary_identity,
        "operation_identity": checkpoint.operation_identity,
        "work_identity": checkpoint.work_identity,
        "duplicate_effect_detected": False,
        "public_observation_commitments": [commitment(v) for v in projections],
    })
    execution = _seal_execution(
        store, kind="RESTART_RECOVERY_RESULT", case_id=case_id,
        parent_execution_id=parent_execution_id,
        fixture_identity=fixture_identity, source_records=sources,
        result_payload=canonical)
    return _issue_from_execution(store, execution.record_id)


def issue_deviation_result(store, *, source_evaluation_fact_id,
                           expected_public_observation_class):
    store = _require_store(store)
    source = store.get(source_evaluation_fact_id)
    if source is None:
        raise ValueError("deviation source recovery result is missing")
    source_payload = json.loads(source.semantic_json)
    if (source_payload.get("kind") not in {
            "REFRESH_VERSION_RESULT", "RESTART_RECOVERY_RESULT"}
            or source_payload.get("governed_evaluator_identity")
                != RECOVERY_EVALUATOR_IDENTITY
            or not source_payload.get("evaluator_execution_record_id")):
        raise ValueError("deviation source is not a protected evaluator result")
    accepted = {
        "MATERIAL_STALE_REFRESH": {"REFRESHED_CURRENT_FRESH",
                                    "REFRESH_FAILED_CURRENT_RETAINED_STALE"},
        "MATERIAL_REFRESH_FAILURE_RETAINS_CURRENT": {
            "REFRESH_FAILED_CURRENT_RETAINED_STALE"},
        "NON_MATERIAL_NO_VERSION": {"NON_MATERIAL_NO_SEMANTIC_ACTION"},
        "SAME_OPERATION_RECOVERY": {"CONVERGED"},
    }.get(expected_public_observation_class, set())
    outcome = ("NO_DEVIATION" if source_payload.get("passed") is True
               and source_payload.get("disposition") in accepted else "DEVIATION")
    payload = {
        "kind": "DEVIATION_RESULT", "case_id": source_payload["case_id"],
        "source_evaluation_fact_id": source.record_id,
        "source_evaluation_fact_commitment": source.semantic_commitment,
        "outcome": outcome,
        "expected_public_observation_class": expected_public_observation_class,
        "observed_disposition": source_payload["disposition"],
        "governed_evaluator_identity": DEVIATION_EVALUATOR_IDENTITY,
        "deviation_evaluation_operation_identity": commitment({
            "evaluator": DEVIATION_EVALUATOR_IDENTITY, "source": source.record_id,
            "source_commitment": source.semantic_commitment,
            "expected": expected_public_observation_class}),
    }
    payload["typed_result_commitment"] = commitment(payload)
    record = LedgerRecord.create(
        "observation", [source.parent_id, "protected-deviation-result",
                        source_payload["case_id"], source.record_id],
        payload, source.parent_id)
    return store._append_protected_recovery(
        record, authority=_RECOVERY_ISSUANCE_AUTHORITY)


def issue_recovery_result(store, provenance):
    raise TypeError("recovery results require evaluator-owned execution provenance")


def validated_recovery_result(**_):
    raise TypeError("caller-created recovery evaluations are not issuable")


def validated_deviation_result(**_):
    raise TypeError("caller-created deviation assertions are not issuable")


def is_recovery_issuance_authority(value):
    return value is _RECOVERY_ISSUANCE_AUTHORITY


__all__ = [
    "PROTECTED_RECOVERY_KINDS", "ValidatedRecoveryResult",
    "ValidatedDeviationResult", "evaluate_and_issue_refresh_result",
    "evaluate_and_issue_refresh_failure_result",
    "evaluate_and_issue_restart_result",
    "evaluate_and_issue_checkpoint_recovery_result", "issue_deviation_result",
    "issue_recovery_result", "validated_deviation_result",
    "record_refresh_failure_observation", "validated_recovery_result",
]
