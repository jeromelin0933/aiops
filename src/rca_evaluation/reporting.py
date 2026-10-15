"""Deterministic S6 reporting from the authoritative Candidate-F run snapshot."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from fractions import Fraction
import json
from math import ceil
from statistics import median

from .aggregation import _bootstrap_intervals
from .adjudication import verify_persisted_adjudication
from .evidence_index import build_evidence_index
from .identity import commitment
from .ledger import LedgerRecord
from .manifest import verify_run_manifest_binding
from .oracle import SCENARIOS
from .report_schema import (
    REPORT_SCHEMA_VERSION, READINESS_SCHEMA_VERSION, ExecutionProjection,
    FormalReport, GateProjection, JudgmentProjection, LatencyProjection,
    MetricProjection, ReAdjudicationProjection, ReadinessProjection,
    CaseResultProjection, ReportComponentProjection, ScenarioMetricProjection,
)
from .real_gate_provenance import _receipt as _validate_actual_receipt
from .recovery_evaluation import verify_report_case_payload
from .sqlite_store import (
    EvaluationLedgerReader, EvaluationRunSnapshot, SqliteEvaluationStore,
)


_GATES = ("REAL_LLM", "REAL_RAG", "DOCKER_RCA_E2E")
_TERMINAL_GATE_STATES = frozenset({
    "EXECUTED_PASS", "EXECUTED_FAIL", "INVALID_EXECUTION",
})



def _payload(record: LedgerRecord) -> dict[str, object]:
    value = json.loads(record.semantic_json)
    if type(value) is not dict:
        raise ValueError("ledger semantic payload must be an object")
    return value


def _fraction(numerator: int, denominator: int) -> tuple[int, int] | None:
    return (numerator, denominator) if denominator else None


def _formatted_interval(value: tuple[float, float] | None) -> tuple[str, str] | None:
    if value is None:
        return None
    return tuple(format(item, ".12g") for item in value)  # type: ignore[return-value]


def _latency_projection(kind: str, facts: tuple[dict[str, object], ...], *,
                        expected: int) -> LatencyProjection:
    observed = tuple(
        item["duration_ms"] for item in facts
        if item.get("latency_disposition", item.get("disposition")) ==
            "OBSERVED_COMPLETION"
        and type(item.get("duration_ms")) is int
    )
    ordered = sorted(observed)
    midpoint = None
    if ordered:
        total = ordered[(len(ordered) - 1) // 2] + ordered[len(ordered) // 2]
        midpoint = (total, 2)
    p95 = ordered[ceil(0.95 * len(ordered)) - 1] if ordered else None
    timeout = sum(item.get("latency_disposition", item.get("disposition")) ==
                  "TIMEOUT" for item in facts)
    censored = sum(item.get("latency_disposition", item.get("disposition")) in {
        "TIMEOUT", "RIGHT_CENSORED",
    } for item in facts)
    failed = sum(item.get("latency_disposition", item.get("disposition")) ==
                 "FAILURE" for item in facts)
    invalid = sum(item.get("latency_disposition", item.get("disposition")) ==
                  "INVALID_EVALUATION" for item in facts)
    if not observed:
        estimability = "NOT_ESTIMABLE_NO_OBSERVED_COMPLETIONS"
    elif len(observed) < 20:
        estimability = "OBSERVED_COMPLETIONS_ONLY_SMALL_SAMPLE"
    else:
        estimability = "OBSERVED_COMPLETIONS_ONLY_ESTIMABLE"
    return LatencyProjection(
        kind, len(observed), failed, censored, timeout, invalid,
        max(0, expected - len(facts)), min(observed) if observed else None,
        midpoint, p95, max(observed) if observed else None,
        tuple(sorted({str(item["start_boundary"]) for item in facts
                      if item.get("start_boundary")})),
        tuple(sorted({str(item["end_boundary"]) for item in facts
                      if item.get("end_boundary")})),
        tuple(sorted({int(item["polling_interval_ms"]) for item in facts
                      if type(item.get("polling_interval_ms")) is int})),
        tuple(sorted({int(item["cutoff_ms"]) for item in facts
                      if type(item.get("cutoff_ms")) is int})),
        estimability,
    )


def _primary_latency_fact(
    final: dict[str, object] | None, signal: dict[str, object] | None,
    run: dict[str, object],
) -> dict[str, object] | None:
    if final is None:
        return None
    disposition = final.get("disposition")
    if disposition in {"OBSERVED_COMPLETION", "VALID_DEGRADED_COMPLETION"}:
        latency_disposition = "OBSERVED_COMPLETION"
    elif disposition == "RIGHT_CENSORED_UNFINISHED":
        latency_disposition = "TIMEOUT"
    elif disposition == "TERMINAL_PRODUCTION_FAILURE":
        latency_disposition = "FAILURE"
    else:
        latency_disposition = "INVALID_EVALUATION"
    return {
        "disposition": latency_disposition,
        "duration_ms": final.get("latency_ms"),
        "start_boundary": "EVALUATION_SIGNAL_PUBLICLY_OBSERVABLE",
        "end_boundary": "USABLE_CURRENT_RCA_PUBLICLY_OBSERVED",
        "polling_interval_ms": (
            signal.get("polling_interval_ms") if signal else
            run.get("polling_interval_ms")),
        "cutoff_ms": (
            final.get("cutoff_ms") or
            (signal.get("cutoff_ms") if signal else None) or
            run.get("timeout_ms")),
    }


def _gate_projection(snapshot: EvaluationRunSnapshot, gate: str) -> GateProjection:
    records = tuple((record, _payload(record)) for record in snapshot.records)
    authorizations = tuple(record for record, payload in records
                           if record.domain == "gate_authorization"
                           and payload.get("gate") == gate)
    starts = tuple(record for record, payload in records
                   if record.domain == "gate_invocation_start"
                   and payload.get("gate") == gate)
    start_ids = {record.record_id for record in starts}
    terminals = tuple((record, payload) for record, payload in records
                      if record.domain == "gate_invocation_receipt"
                      and record.parent_id in start_ids)
    for record, payload in terminals:
        _validate_actual_receipt(
            record.record_id, record.semantic_commitment, payload)
    legacy = tuple(record for record, payload in records
                   if record.domain == "gate_execution_receipt"
                   and payload.get("gate") == gate)
    namespaces = tuple(record for record, payload in records
                       if record.domain == "docker_namespace") if gate == "DOCKER_RCA_E2E" else ()
    namespace_ids = {record.record_id for record in namespaces}
    cleanups = tuple((record, payload) for record, payload in records
                     if record.domain == "docker_cleanup"
                     and record.parent_id in namespace_ids) if gate == "DOCKER_RCA_E2E" else ()

    terminal_by_start = {record.parent_id: payload for record, payload in terminals}
    if not starts:
        state = "NOT_EXECUTED"
    elif any(record.record_id not in terminal_by_start for record in starts):
        state = "EXECUTION_STARTED_INCOMPLETE"
    else:
        statuses = {str(payload.get("status")) for _, payload in terminals}
        if "INVALID_EXECUTION" in statuses:
            state = "INVALID_EXECUTION"
        elif "EXECUTED_FAIL" in statuses:
            state = "EXECUTED_FAIL"
        elif statuses == {"EXECUTED_PASS"}:
            state = "EXECUTED_PASS"
        else:
            state = "INVALID_EXECUTION"
    if gate == "DOCKER_RCA_E2E" and state == "EXECUTED_PASS":
        if (not namespaces or not cleanups
                or any(payload.get("disposition") != "COMPLETED"
                       for _, payload in cleanups)):
            state = "INVALID_EXECUTION"
    pins = sorted({
        (str(payload.get("profile_identity")),
         str(payload.get("profile_commitment")))
        for _, payload in records
        if payload.get("gate") == gate
        and payload.get("profile_identity") is not None
        and payload.get("profile_commitment") is not None
    })
    return GateProjection(
        gate, state, state == "EXECUTED_PASS",
        tuple(record.record_id for record in authorizations),
        tuple(record.record_id for record in starts),
        tuple(record.record_id for record, _ in terminals),
        tuple(record.record_id for record in legacy),
        tuple(record.record_id for record in namespaces),
        tuple(record.record_id for record, _ in cleanups), tuple(pins),
    )


def _metric(rows: tuple[ExecutionProjection, ...], name: str,
            selector: str) -> MetricProjection:
    invalid = sum(row.disposition == "INVALID_EVALUATION" for row in rows)
    failed = sum(row.disposition == "TERMINAL_PRODUCTION_FAILURE" for row in rows)
    censored = sum(row.disposition == "RIGHT_CENSORED_UNFINISHED" for row in rows)
    if selector == "root":
        values = tuple(row.root_cause_success for row in rows)
        numerator = sum(value is True for value in values)
        denominator = len(rows)
        missing = sum(value is None for value in values)
    elif selector == "calibration":
        values = tuple(row.conclusion_calibration_valid for row in rows)
        numerator = sum(value is True for value in values)
        denominator = len(rows)
        missing = sum(value is None for value in values)
    else:
        if any(
            row.unsupported_claim_numerator is not None
            and row.unsupported_claim_denominator is not None
            and (row.unsupported_claim_numerator < 0
                 or row.unsupported_claim_denominator < 0
                 or row.unsupported_claim_numerator >
                    row.unsupported_claim_denominator)
            for row in rows
        ):
            raise ValueError("unsupported claim counts are contradictory")
        numerator = sum(row.unsupported_claim_numerator or 0 for row in rows)
        denominator = sum(row.unsupported_claim_denominator or 0 for row in rows)
        missing = sum(row.unsupported_claim_denominator is None for row in rows)
        values = ()
    scenario_rates = []
    for scenario in SCENARIOS:
        scenario_rows = tuple(row for row in rows if row.scenario_id == scenario)
        if selector == "unsupported":
            scenario_numerator = sum(
                row.unsupported_claim_numerator or 0 for row in scenario_rows)
            scenario_denominator = sum(
                row.unsupported_claim_denominator or 0 for row in scenario_rows)
        else:
            attribute = ("root_cause_success" if selector == "root" else
                         "conclusion_calibration_valid")
            scenario_numerator = sum(
                getattr(row, attribute) is True for row in scenario_rows)
            scenario_denominator = len(scenario_rows)
        if scenario_denominator:
            scenario_rates.append(Fraction(scenario_numerator,
                                           scenario_denominator))
    macro_fraction = None
    if scenario_rates:
        macro_value = sum(scenario_rates, Fraction()) / len(scenario_rates)
        macro_fraction = (macro_value.numerator, macro_value.denominator)
    micro_fraction = _fraction(numerator, denominator)
    micro = macro = None
    if rows and not missing:
        if selector == "unsupported":
            scenario_outcomes = tuple(tuple(
                (1,) * int(row.unsupported_claim_numerator or 0)
                + (0,) * (int(row.unsupported_claim_denominator or 0)
                           - int(row.unsupported_claim_numerator or 0))
                for row in rows if row.scenario_id == scenario
            ) for scenario in SCENARIOS)
            scenario_outcomes = tuple(
                tuple(value for group in groups for value in group)
                for groups in scenario_outcomes)
        else:
            scenario_outcomes = tuple(tuple(
                1 if getattr(row, "root_cause_success" if selector == "root"
                             else "conclusion_calibration_valid") else 0
                for row in rows if row.scenario_id == scenario
            ) for scenario in SCENARIOS)
        raw_micro, raw_macro = _bootstrap_intervals(scenario_outcomes)
        micro, macro = _formatted_interval(raw_micro), _formatted_interval(raw_macro)
    return MetricProjection(
        name, numerator, denominator, micro_fraction,
        macro_fraction, micro_fraction,
        invalid, failed, censored, missing, micro, macro,
        ("COMPLETE" if not missing and denominator else
         "NOT_EVALUABLE" if not missing else "INCOMPLETE_EVIDENCE"),
    )


def _distribution(name: str, values: tuple[tuple[str, str], ...], *,
                  complete: bool = True) -> ReportComponentProjection:
    counts: dict[str, int] = {}
    evidence = []
    for record_id, value in values:
        counts[value] = counts.get(value, 0) + 1
        evidence.append(record_id)
    return ReportComponentProjection(
        name, tuple(sorted(counts.items())), tuple(sorted(evidence)), complete,
        complete)


def _case_projection(record: LedgerRecord, payload: dict[str, object]) -> CaseResultProjection:
    return CaseResultProjection(
        str(payload.get("case_id")), str(record.parent_id),
        str(payload.get("disposition")),
        str(payload["before_state"]) if payload.get("before_state") else None,
        str(payload["trigger_basis"]) if payload.get("trigger_basis") else None,
        str(payload["resulting_state"]) if payload.get("resulting_state") else None,
        str(payload["freshness_transition"])
        if payload.get("freshness_transition") else None,
        payload.get("prior_current_preserved")
        if type(payload.get("prior_current_preserved")) is bool else None,
        str(payload["recovered_identity"])
        if payload.get("recovered_identity") else None,
        payload.get("duplicate_effect_detected")
        if type(payload.get("duplicate_effect_detected")) is bool else None,
        str(payload["publication_continuity"])
        if payload.get("publication_continuity") else None,
        str(payload["latency_disposition"])
        if payload.get("latency_disposition") else None,
        record.record_id,
    )


def _case_component(
    name: str, required: tuple[str, ...],
    facts: tuple[tuple[LedgerRecord, dict[str, object]], ...],
    *, required_fields: tuple[str, ...],
) -> ReportComponentProjection:
    cases = tuple(_case_projection(record, payload) for record, payload in facts)
    observed = tuple(item.case_id for item in cases)
    fact_kind = ("REFRESH_VERSION_RESULT" if name == "REFRESH_VERSION_RESULTS"
                 else "RESTART_RECOVERY_RESULT")
    facts_valid = all(
        isinstance(payload.get("case_id"), str) and payload.get("case_id")
        and all(field in payload and payload.get(field) is not None
                for field in required_fields)
        and not verify_report_case_payload(fact_kind, payload)
        for _, payload in facts)
    complete = (facts_valid and len(observed) == len(set(observed))
                and set(observed) >= set(required)
                and all(item.disposition not in {"None", "INVALID", "INCOMPLETE"}
                        for item in cases if item.case_id in set(required)))
    required_payloads = tuple(
        payload for _, payload in facts if payload.get("case_id") in set(required))
    if name == "RESTART_RECOVERY_RESULTS":
        passed = (complete and len(required_payloads) == len(required)
                  and all(payload.get("passed") is True
                          and payload.get("disposition") == "CONVERGED"
                          for payload in required_payloads))
    else:
        passed = (complete and len(required_payloads) == len(required)
                  and all(payload.get("passed") is True
                          for payload in required_payloads))
    counts: dict[str, int] = {}
    for item in cases:
        counts[item.disposition] = counts.get(item.disposition, 0) + 1
    if not required and not cases:
        counts["NOT_APPLICABLE"] = 1
    return ReportComponentProjection(
        name, tuple(sorted(counts.items())),
        tuple(sorted(item.evidence_record_id for item in cases)), complete,
        passed,
        tuple(sorted(required)), tuple(sorted(observed)), cases)


def _deviation_component(
    required: tuple[str, ...],
    facts: tuple[tuple[LedgerRecord, dict[str, object]], ...],
) -> ReportComponentProjection:
    cases = tuple(CaseResultProjection(
        str(payload.get("case_id")), record.parent_id or "",
        str(payload.get("outcome") or "INVALID"), None,
        str(payload.get("governed_evaluator_identity") or "MISSING"),
        None, None, None, None, False, None, None, record.record_id)
        for record, payload in facts)
    observed = {item.case_id for item in cases}
    complete = (
        bool(required) and set(required) == observed
        and len(cases) == len(required)
        and all(item.disposition in {"NO_DEVIATION", "DEVIATION"}
                for item in cases))
    passed = complete and all(
        item.disposition == "NO_DEVIATION" for item in cases)
    return ReportComponentProjection(
        "DEVIATION_RESULTS",
        tuple(sorted({
            value: sum(item.disposition == value for item in cases)
            for value in {item.disposition for item in cases}}.items())),
        tuple(sorted(record.record_id for record, _ in facts)),
        complete, passed, tuple(sorted(required)), tuple(sorted(observed)), cases)


def _formal_pass_gate(**predicates: bool) -> bool:
    """Closed conjunction for the independently projected formal predicates."""
    expected = {
        "executed", "campaign", "evidence", "review", "judgment_authority",
        "ucr_authority", "isolation", "deviation",
        "refresh", "restart", "external_gates", "root_cause",
        "unsupported_claims", "calibration", "report_source_cut", "closure",
    }
    if set(predicates) != expected or any(type(value) is not bool
                                          for value in predicates.values()):
        raise ValueError("formal pass predicates are incomplete")
    return all(predicates.values())


def _project(snapshot: EvaluationRunSnapshot) -> FormalReport:
    records = tuple((record, _payload(record)) for record in snapshot.records)
    root = next(payload for record, payload in records
                if record.record_id == snapshot.evaluation_run_id)
    by_parent: dict[str, list[tuple[LedgerRecord, dict[str, object]]]] = {}
    for record, payload in records:
        if record.parent_id is not None:
            by_parent.setdefault(record.parent_id, []).append((record, payload))
    gates = tuple(_gate_projection(snapshot, gate) for gate in _GATES)
    gate_states = tuple((gate.gate, gate.state) for gate in gates)

    originals = []
    readjudications = []
    executions = []
    original_by_execution: dict[str, tuple[LedgerRecord, dict[str, object]]] = {}
    for record, payload in records:
        if record.domain == "judgment":
            original_by_execution[record.parent_id] = (record, payload)
            originals.append(JudgmentProjection(
                record.record_id, record.parent_id,
                str(payload.get("oracle_revision_id")), str(payload.get("result")),
                record.semantic_commitment,
            ))
        elif record.domain == "re_adjudication":
            readjudications.append(ReAdjudicationProjection(
                record.record_id, record.parent_id,
                str(payload.get("original_oracle_revision_id")),
                str(payload.get("new_oracle_revision_id")),
                str(payload.get("authorization_reference")),
                str(payload.get("result")), record.semantic_commitment,
            ))

    adjudication_verification_by_execution = {
        record.record_id: verify_persisted_adjudication(
            snapshot, record.record_id)
        for record, _ in records if record.domain == "execution"
    }

    latency_facts: dict[str, list[tuple[str, dict[str, object]]]] = {
        "PRIMARY_FULL_E2E": [], "REFRESH": [], "RESTART_RECOVERY": [],
    }
    for record, execution_payload in records:
        if record.domain != "execution":
            continue
        children = by_parent.get(record.record_id, [])
        finals = [(child, payload) for child, payload in children
                  if child.domain == "observation"
                  and json.loads(child.identity_key_json)[1:] == ["final"]]
        if len(finals) > 1:
            raise ValueError("execution has multiple final observation identities")
        final_record, final = finals[0] if finals else (None, None)
        judgment_record, judgment = original_by_execution.get(
            record.record_id, (None, None))
        if not adjudication_verification_by_execution[record.record_id].complete:
            judgment_record, judgment = None, None
        predicates = judgment.get("root_cause_predicates", {}) if judgment else {}
        unsupported = judgment.get("unsupported_claim_result") if judgment else None
        if not isinstance(unsupported, dict):
            unsupported = None
            for _, child_payload in children:
                if child_payload.get("kind") == "UNSUPPORTED_CLAIM_RESULT":
                    unsupported = child_payload
                    break
        signal = next((payload for child, payload in children
                       if child.domain == "observation"
                       and payload.get("kind") == "SIGNAL_STARTED"), None)
        primary = _primary_latency_fact(final, signal, root)
        if primary is not None:
            latency_facts["PRIMARY_FULL_E2E"].append((record.record_id, primary))
        per_execution_latency = {}
        for _, child_payload in children:
            kind = child_payload.get("latency_kind")
            if kind in {"REFRESH", "RESTART_RECOVERY"}:
                latency_facts[kind].append((record.record_id, child_payload))
                per_execution_latency[kind] = str(child_payload.get("disposition"))
        evidence_ids = tuple(child.record_id for child, _ in children)
        executions.append(ExecutionProjection(
            record.record_id, str(execution_payload.get("scenario_id")),
            int(execution_payload.get("repetition")),
            str(execution_payload.get("planned_slot_execution_id") or record.record_id),
            execution_payload.get("replacement_for_execution_id"),
            str(final.get("disposition")) if final else None,
            str(final.get("metric_disposition")) if final else None,
            (bool(predicates.get("root_cause_identification_success"))
             if "root_cause_identification_success" in predicates else
             (bool(final.get("rca_success")) if final and "rca_success" in final else None)),
            (int(unsupported.get("numerator")) if unsupported and
             type(unsupported.get("numerator")) is int else None),
            (int(unsupported.get("denominator")) if unsupported and
             type(unsupported.get("denominator")) is int else None),
            (int(unsupported.get("contradicted_count")) if unsupported and
             type(unsupported.get("contradicted_count")) is int else None),
            (bool(predicates.get("conclusion_calibration_valid"))
             if "conclusion_calibration_valid" in predicates else None),
            str(final.get("knowledge")) if final else None,
            str(final.get("failure_case")) if final else None,
            str(final.get("diagnostic_stage")) if final else None,
            str(primary["disposition"]) if primary else "NOT_OBSERVED",
            per_execution_latency.get("REFRESH", "NOT_OBSERVED"),
            per_execution_latency.get("RESTART_RECOVERY", "NOT_OBSERVED"),
            gate_states, evidence_ids,
        ))
    rows = tuple(executions)
    execution_order = root.get("execution_order")
    admitted_ids = (tuple(execution_order)
                    if isinstance(execution_order, list)
                    and all(isinstance(item, str) for item in execution_order)
                    and len(set(execution_order)) == len(execution_order) else ())
    by_execution_projection = {row.execution_id: row for row in rows}
    formal_rows = tuple(by_execution_projection[item] for item in admitted_ids
                        if item in by_execution_projection)
    formal_id_set = set(admitted_ids)
    root_metric = _metric(formal_rows, "ROOT_CAUSE_IDENTIFICATION_SUCCESS", "root")
    unsupported_metric = _metric(
        formal_rows, "UNSUPPORTED_CLAIM_RATE", "unsupported")
    calibration_metric = _metric(
        formal_rows, "CONCLUSION_CALIBRATION", "calibration")
    scenario_metrics = tuple(ScenarioMetricProjection(
        scenario,
        _metric(tuple(row for row in formal_rows if row.scenario_id == scenario),
                "ROOT_CAUSE_IDENTIFICATION_SUCCESS", "root"),
        _metric(tuple(row for row in formal_rows if row.scenario_id == scenario),
                "UNSUPPORTED_CLAIM_RATE", "unsupported"),
        _metric(tuple(row for row in formal_rows if row.scenario_id == scenario),
                "CONCLUSION_CALIBRATION", "calibration"),
    ) for scenario in SCENARIOS)
    latencies = tuple(_latency_projection(
        kind, tuple(payload for execution_id, payload in latency_facts[kind]
                    if execution_id in formal_id_set), expected=len(formal_rows))
        for kind in ("PRIMARY_FULL_E2E", "REFRESH", "RESTART_RECOVERY"))

    fixed_n = root.get("fixed_n")
    campaign_complete = (
        type(fixed_n) is int and fixed_n > 0
        and len(admitted_ids) == fixed_n * len(SCENARIOS)
        and len(formal_rows) == len(admitted_ids)
        and all(sum(row.scenario_id == scenario for row in formal_rows) == fixed_n
                for scenario in SCENARIOS)
    )
    evidence_index = build_evidence_index(snapshot)
    source_entries = tuple(entry for entry in evidence_index.entries
                           if entry.domain != "evaluation_run")
    input_complete = all(
        any(name == "input_identity" for name, _ in entry.safe_references)
        and any(name == "input_commitment" for name, _ in entry.safe_references)
        for entry in source_entries
        if entry.domain == "execution" and entry.record_id in formal_id_set)
    execution_payload_by_id = {
        record.record_id: payload for record, payload in records
        if record.domain == "execution"}
    manifest_verification = verify_run_manifest_binding(
        root, evaluation_run_id=snapshot.evaluation_run_id,
        evaluation_plan_id=snapshot.evaluation_plan_id,
        oracle_revision_id=snapshot.oracle_revision_id,
        executions=tuple((item, execution_payload_by_id[item]) for item in admitted_ids
                         if item in execution_payload_by_id))
    scoring_complete = all(
        row.root_cause_success is not None
        and row.unsupported_claim_denominator is not None
        and row.conclusion_calibration_valid is not None
        for row in formal_rows)
    latency_evidence_complete = all(
        row.primary_latency_disposition != "NOT_OBSERVED" for row in formal_rows)
    results_complete = bool(formal_rows) and all(
        row.disposition is not None for row in formal_rows)
    review_verifications = tuple(
        adjudication_verification_by_execution[row.execution_id]
        for row in formal_rows)
    review_complete = bool(formal_rows) and all(
        item.complete for item in review_verifications)
    re_adjudication_ambiguous = len(readjudications) > 1
    if re_adjudication_ambiguous:
        review_complete = False
    gate_evidence_complete = all(
        gate.authorization_ids and gate.invocation_start_ids
        and gate.terminal_receipt_ids and gate.profile_pins
        and (gate.gate != "DOCKER_RCA_E2E"
             or (gate.docker_namespace_ids and gate.docker_cleanup_ids))
        for gate in gates)
    evidence_complete = (
        manifest_verification.complete and bool(evidence_index.manifest_commitment)
        and input_complete
        and scoring_complete and latency_evidence_complete
        and review_complete and gate_evidence_complete)

    contradicted_claim_count = sum(
        row.contradicted_claim_count or 0 for row in formal_rows)
    knowledge_distribution = _distribution(
        "KNOWLEDGE_OUTCOME_DISTRIBUTION",
        tuple((row.execution_id, row.knowledge_result or "MISSING")
              for row in formal_rows),
        complete=bool(formal_rows) and all(row.knowledge_result for row in formal_rows))
    degraded_distribution = _distribution(
        "FULL_DEGRADED_FAILURE_DISTRIBUTION",
        tuple((row.execution_id, row.disposition or "INCOMPLETE")
              for row in formal_rows), complete=results_complete)
    isolation_cases = tuple(CaseResultProjection(
        row.execution_id, row.execution_id,
        "PASS" if verification.complete else "INCOMPLETE",
        None, "TRUSTED_BLIND_PACKET_POLICY", None, None, None, None,
        None, None, None, verification.judgment_id or row.execution_id)
        for row, verification in zip(formal_rows, review_verifications))
    required_isolation = tuple(root.get("required_isolation_case_ids") or ())
    isolation_results = ReportComponentProjection(
        "ISOLATION_RESULTS",
        tuple(sorted({value: sum(item.disposition == value for item in isolation_cases)
                      for value in {item.disposition for item in isolation_cases}}.items())),
        tuple(sorted(item.evidence_record_id for item in isolation_cases)),
        (set(required_isolation) == formal_id_set
         and len(isolation_cases) == len(required_isolation)),
        (set(required_isolation) == formal_id_set
         and all(item.disposition == "PASS" for item in isolation_cases)),
        tuple(sorted(required_isolation)),
        tuple(sorted(item.case_id for item in isolation_cases)),
        isolation_cases)
    required_deviation = tuple(root.get("required_deviation_case_ids") or ())
    deviation_facts = tuple(
        (record, payload) for record, payload in records
        if record.domain == "observation"
        and payload.get("kind") == "DEVIATION_RESULT")
    deviation_results = _deviation_component(required_deviation, deviation_facts)
    abort_values = tuple(
        (row.execution_id, row.disposition or "INCOMPLETE")
        for row in formal_rows)
    if root.get("run_disposition") == "ABORTED":
        abort_values += ((snapshot.evaluation_run_id, "ABORTED"),)
    abort_results = _distribution(
        "ABORT_INCOMPLETE_RESULTS", abort_values, complete=campaign_complete)
    refresh_facts = tuple(
        (record, payload)
        for record, payload in records if record.domain == "observation"
        and payload.get("kind") == "REFRESH_VERSION_RESULT")
    required_refresh = tuple(sorted(
        str(item) for item in (root.get("required_refresh_case_ids") or ())))
    refresh_results = _case_component(
        "REFRESH_VERSION_RESULTS", required_refresh, refresh_facts,
        required_fields=("before_state", "trigger_basis", "resulting_state",
                         "freshness_transition", "prior_current_preserved",
                         "duplicate_effect_detected", "latency_disposition"))
    restart_facts = tuple(
        (record, payload)
        for record, payload in records if record.domain == "observation"
        and payload.get("kind") == "RESTART_RECOVERY_RESULT")
    required_restart = tuple(sorted(
        str(item) for item in (root.get("required_restart_case_ids") or ())))
    restart_results = _case_component(
        "RESTART_RECOVERY_RESULTS", required_restart, restart_facts,
        required_fields=("before_state", "resulting_state", "recovered_identity",
                         "duplicate_effect_detected", "publication_continuity",
                         "latency_disposition"))
    report_components_complete = all(component.complete for component in (
        knowledge_distribution, degraded_distribution, isolation_results,
        deviation_results, abort_results, refresh_results, restart_results))
    evidence_complete = evidence_complete and report_components_complete
    gates_executed = all(gate.state in _TERMINAL_GATE_STATES for gate in gates)
    gates_pass = all(gate.formal_pass for gate in gates)
    gate_harness_ready = all(
        bool(gate.authorization_ids) and bool(gate.profile_pins) for gate in gates)
    protected_judgment_authority_valid = all(
        adjudication_verification_by_execution[execution_id].complete
        for execution_id in original_by_execution)
    protected_ucr_authority_valid = all(
        row.unsupported_claim_denominator is not None
        for row in formal_rows if row.execution_id in original_by_execution)
    engineering_implemented = (
        snapshot.integrity_status == "READY"
        and snapshot.schema_version == "SPEC-017-RUN-LEDGER-SNAPSHOT-v1"
        and snapshot.report_source_cut_policy
            == "EXCLUDE_REPORT_PROJECTIONS-v1")
    engineering_complete = (
        engineering_implemented and protected_judgment_authority_valid
        and protected_ucr_authority_valid and not re_adjudication_ambiguous)
    pre_execution_ready = (
        campaign_complete and manifest_verification.complete
        and bool(evidence_index.manifest_commitment) and input_complete
        and gate_harness_ready and bool(required_isolation)
        and bool(required_deviation) and bool(required_refresh)
        and bool(required_restart))
    evaluation_ready = engineering_complete and pre_execution_ready
    evaluation_executed = (
        evaluation_ready and results_complete and review_complete
        and report_components_complete and gate_evidence_complete
        and gates_executed)
    formal_pass = _formal_pass_gate(
        executed=evaluation_executed,
        campaign=campaign_complete, evidence=evidence_complete,
        review=review_complete,
        judgment_authority=protected_judgment_authority_valid,
        ucr_authority=protected_ucr_authority_valid,
        isolation=isolation_results.passed,
        deviation=deviation_results.passed, refresh=refresh_results.passed,
        restart=restart_results.passed,
        external_gates=gates_executed and gates_pass,
        root_cause=(root_metric.numerator == root_metric.denominator
                    and not root_metric.missing_count),
        unsupported_claims=(unsupported_metric.numerator == 0
                            and not unsupported_metric.missing_count),
        calibration=(calibration_metric.numerator == calibration_metric.denominator
                     and not calibration_metric.missing_count),
        report_source_cut=(snapshot.integrity_status == "READY"),
        closure=engineering_complete,
    )
    blockers = []
    if not campaign_complete:
        blockers.append("REQUIRED_CAMPAIGN_INCOMPLETE")
    if not evidence_complete:
        blockers.append("EVIDENCE_INCOMPLETE")
        blockers.extend(f"MANIFEST_{item}"
                        for item in manifest_verification.blockers)
        if not input_complete:
            blockers.append("EXECUTION_LINEAGE_EVIDENCE_INCOMPLETE")
        if not scoring_complete:
            blockers.append("SCORING_EVIDENCE_INCOMPLETE")
        if not latency_evidence_complete:
            blockers.append("LATENCY_EVIDENCE_INCOMPLETE")
        if not review_complete:
            blockers.append("ADJUDICATION_EVIDENCE_INCOMPLETE")
        if not gate_evidence_complete:
            blockers.append("REAL_GATE_EVIDENCE_INCOMPLETE")
    if not results_complete:
        blockers.append("EXECUTION_RESULTS_INCOMPLETE")
    if not review_complete:
        blockers.append("REVIEW_INCOMPLETE")
        blockers.extend(
            f"REVIEW_{blocker}" for result in review_verifications
            for blocker in result.blockers)
    if re_adjudication_ambiguous:
        blockers.append("RE_ADJUDICATION_AMBIGUOUS")
    if not engineering_implemented:
        blockers.append("ENGINEERING_IMPLEMENTATION_INCOMPLETE")
    if not protected_judgment_authority_valid:
        blockers.append("PROTECTED_JUDGMENT_AUTHORITY_INVALID")
    if not protected_ucr_authority_valid:
        blockers.append("PROTECTED_UCR_AUTHORITY_INVALID")
    if not engineering_complete:
        blockers.append("ENGINEERING_CONTRACT_INCOMPLETE")
    if not pre_execution_ready:
        blockers.append("EVALUATION_PRE_EXECUTION_READINESS_INCOMPLETE")
    if not report_components_complete:
        blockers.append("REQUIRED_REPORT_COMPONENT_INCOMPLETE")
        blockers.extend(
            f"{component.name}_INCOMPLETE" for component in (
                knowledge_distribution, degraded_distribution,
                isolation_results, deviation_results, abort_results,
                refresh_results, restart_results) if not component.complete)
    if deviation_results.complete and not deviation_results.passed:
        blockers.append("DEVIATION_RESULTS_NOT_PASSED")
    if refresh_results.complete and not refresh_results.passed:
        blockers.append("REFRESH_VERSION_RESULTS_NOT_PASSED")
    if restart_results.complete and not restart_results.passed:
        blockers.append("RESTART_RECOVERY_RESULTS_NOT_PASSED")
    gate_labels = {
        "REAL_LLM": "LIVE_LLM", "REAL_RAG": "REAL_RAG",
        "DOCKER_RCA_E2E": "DOCKER_FORMAL_E2E",
    }
    for gate in gates:
        if gate.state == "NOT_EXECUTED":
            blockers.append(f"{gate_labels[gate.gate]}_NOT_EXECUTED")
        elif gate.state == "EXECUTION_STARTED_INCOMPLETE":
            blockers.append(f"{gate_labels[gate.gate]}_INCOMPLETE")
        elif not gate.formal_pass:
            blockers.append(f"{gate_labels[gate.gate]}_NOT_PASSED")
    if root_metric.numerator != root_metric.denominator or root_metric.missing_count:
        blockers.append("ROOT_CAUSE_METRIC_NOT_PASSED")
    if unsupported_metric.numerator or unsupported_metric.missing_count:
        blockers.append("UNSUPPORTED_CLAIM_METRIC_NOT_PASSED")
    if (calibration_metric.numerator != calibration_metric.denominator
            or calibration_metric.missing_count):
        blockers.append("CALIBRATION_METRIC_NOT_PASSED")
    blockers.extend((
        "PRODUCT_ACCEPTANCE_NOT_ESTABLISHED",
        "PRODUCTION_READINESS_NOT_ESTABLISHED",
    ))
    readiness = ReadinessProjection(
        READINESS_SCHEMA_VERSION, engineering_implemented,
        engineering_complete, evaluation_ready,
        evaluation_executed, formal_pass, False, False,
        tuple(sorted(set(blockers))), snapshot.report_source_cut_commitment,
    )
    return FormalReport(
        REPORT_SCHEMA_VERSION, snapshot.evaluation_run_id,
        snapshot.evaluation_plan_id, snapshot.oracle_revision_id,
        snapshot.report_source_cut_commitment, evidence_index, rows,
        tuple(originals), tuple(readjudications), re_adjudication_ambiguous,
        root_metric, unsupported_metric, calibration_metric,
        contradicted_claim_count, knowledge_distribution,
        degraded_distribution, isolation_results, deviation_results,
        abort_results, refresh_results, restart_results,
        scenario_metrics, latencies, gates, readiness,
    )


def build_formal_report(
    reader: EvaluationLedgerReader, evaluation_run_id: str,
) -> FormalReport:
    """Build a report using the authoritative run snapshot as its sole input."""
    if type(reader) is not EvaluationLedgerReader:
        raise TypeError("store-issued EvaluationLedgerReader required")
    return _project(reader.read_run_snapshot(evaluation_run_id))


def append_report_commitment(
    store: SqliteEvaluationStore, report: FormalReport, *, generated_at: str,
) -> LedgerRecord:
    """Append one derived report commitment to the same Candidate-F authority."""
    if type(store) is not SqliteEvaluationStore or type(report) is not FormalReport:
        raise TypeError("Candidate-F store and FormalReport required")
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("generated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    current = build_formal_report(store.reader(), report.evaluation_run_id)
    if current.report_semantic_commitment != report.report_semantic_commitment:
        raise ValueError("report is not the current deterministic projection")
    payload = {
        "report_schema_version": report.schema_version,
        "source_cut_commitment": report.source_cut_commitment,
        "evidence_index_commitment": report.evidence_index.evidence_index_commitment,
        "report_semantic_commitment": report.report_semantic_commitment,
        "generated_at": generated_at,
        "readiness_projection_basis": report.readiness.basis_source_cut_commitment,
        "readiness_projection_commitment": commitment(asdict(report.readiness)),
    }
    return store.append(LedgerRecord.create(
        "report", [report.evaluation_run_id, report.schema_version,
                   report.source_cut_commitment], payload,
        report.evaluation_run_id,
    ))


def report_is_current(reader: EvaluationLedgerReader, report_id: str) -> bool:
    if type(reader) is not EvaluationLedgerReader:
        raise TypeError("store-issued EvaluationLedgerReader required")
    record = reader.get(report_id)
    if record is None or record.domain != "report":
        return False
    payload = _payload(record)
    snapshot = reader.read_run_snapshot(record.parent_id)
    current = _project(snapshot)
    return (
        payload.get("report_schema_version") == REPORT_SCHEMA_VERSION
        and payload.get("source_cut_commitment") ==
            snapshot.report_source_cut_commitment
        and payload.get("evidence_index_commitment") ==
            current.evidence_index.evidence_index_commitment
        and payload.get("report_semantic_commitment") ==
            current.report_semantic_commitment
        and payload.get("readiness_projection_basis") ==
            snapshot.report_source_cut_commitment
        and payload.get("readiness_projection_commitment") ==
            commitment(asdict(current.readiness))
    )
