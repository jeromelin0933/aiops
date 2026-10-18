"""Focused S6 evidence-index, formal-report, and readiness tests."""

from dataclasses import FrozenInstanceError, replace
import json

import pytest

import rca_evaluation.adjudication as protected_adjudication
from rca_evaluation.adjudication import (
    ADJUDICATION_EVALUATOR_IDENTITY, ADJUDICATION_EVALUATOR_VERSION,
)

from rca_evaluation.authority import (
    PM_AUTHORITY, TrustedAmendmentAuthorization, TrustedApprovalRegistry,
    TrustedOracleApproval,
)
from rca_evaluation.identity import canonical_bytes, commitment, identity
from rca_evaluation.ledger import LedgerRecord
from rca_evaluation.manifest import verify_run_manifest_binding
from rca_evaluation.oracle import EvaluationPlan, OracleRevision
from rca_evaluation.reporting import (
    append_report_commitment, build_formal_report, report_is_current,
)
from rca_evaluation.sqlite_store import SqliteEvaluationStore


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def _semantics(label):
    return {
        "scenarios": {
            f"S{i}": {f"O{j}": f"{label}-{i}-{j}" for j in range(1, 11)}
            for i in range(1, 7)
        },
        "cross_scenario_rules": ["approved rule"],
    }


def _oracle(label="approved", parent=None):
    return OracleRevision.approved(
        oracle_set_id="PM-S1-S6", semantics=_semantics(label),
        parent_revision_id=parent, created_at="2026-10-02T00:00:00Z",
        approval_authority=PM_AUTHORITY,
        approval_reference=f"PM-017-{label}",
        approved_at="2026-10-02T01:00:00Z",
    )


def _plan(revision):
    return EvaluationPlan.admit(
        revision, fixture_revision="fixture-r1", production_build="build-a",
        model="model-a", prompt="prompt-hash", schema="schema-hash",
        corpus_index="index-hash", configuration="config-hash", repetitions=1,
    )


def _approvals(*revisions, amendments=()):
    return TrustedApprovalRegistry(tuple(
        TrustedOracleApproval(
            item.approval_reference, PM_AUTHORITY, item.oracle_revision_id,
            item.semantic_content_commitment, item.approved_at,
            item.parent_revision_id,
        ) for item in revisions
    ), tuple(amendments))


def _run(store, plan, *, missing_reproducibility_field=None, fixed_n=1,
         require_refresh=False, require_restart=False, run_disposition=None):
    run_id = identity("evaluation_run", [plan.evaluation_plan_id, "formal-run"])
    execution_order = [identity("execution", [run_id, f"S{index}", 1])
                       for index in range(1, 7)]
    run_payload = {
            "oracle_revision_id": plan.oracle_revision_id,
            "oracle_content_commitment": plan.oracle_content_commitment,
            "manifest_commitment": SHA_A,
            "fixture_revision": plan.fixture_revision,
            "production_build": plan.production_build,
            "repository_commit": "4109019b61b181824bcec8a32efd31c55e0bfe91",
            "repository_dirty": True,
            "corpus_index": plan.corpus_index,
            "polling_interval_ms": 10,
            "timeout_ms": 100,
            "clock_source": "monotonic",
            "clock_synchronization_uncertainty_ms": 0,
            "repository_branch": "feature/spec-017",
            "oracle_set_identity": "PM-S1-S6",
            "oracle_approval_reference": plan.oracle_approval_reference,
            "fixture_schema_version": "SPEC-017-FIXTURE-v1",
            "fixture_content_commitments": [
                commitment({"scenario": f"S{index}"}) for index in range(1, 7)],
            "random_seed_or_order_commitment": SHA_A,
            "denominator_policy_version": "SPEC-017-8.4-v1",
            "aggregation_policy_version": "SPEC-017-S4-nearest-rank-v2",
            "production_contract_versions": {"RCA": "SPEC-011/016"},
            "model": plan.model, "provider_profile_references": ["provider-v1"],
            "rag_profile_references": ["rag-v1"],
            "prompt": plan.prompt, "result_schema": plan.schema,
            "evaluation_config_commitment": SHA_A,
            "generation_config_commitment": SHA_A,
            "knowledge_corpus_manifest_commitment": SHA_A,
            "knowledge_document_revisions": ["doc-v1"],
            "embedding_model": "embedding-model",
            "knowledge_build_identity": "build-v1",
            "retrieval_profile_identity": "retrieval-v1",
            "applicability_profile_identity": "applicability-v1",
            "docker_compose_digest": SHA_A, "docker_config_digest": SHA_A,
            "docker_image_digest": SHA_A,
            "docker_namespace_identity": "isolated-v1",
            "environment_identity": "test-env-v1",
            "tool_versions": {"python": "3"},
            "evaluator_rubric_version": "rubric-v1",
            "reviewer_protocol_version": "SPEC-017-6.2-v1",
            "adjudication_policy_version": "SPEC-017-2.4-v1",
            "public_observation_adapter_version": "SPEC-017-S2-v1",
            "report_generator_version": "SPEC-017-FORMAL-REPORT-v1",
            "required_isolation_case_ids": execution_order,
            "required_deviation_case_ids": execution_order,
            "required_refresh_case_ids": execution_order[:1] if require_refresh else [],
            "required_restart_case_ids": execution_order[:1] if require_restart else [],
            "fixed_n": fixed_n,
            "execution_order": execution_order,
    }
    extension_names = {
        "repository_branch", "oracle_set_identity", "oracle_approval_reference",
        "fixture_schema_version", "fixture_content_commitments",
        "denominator_policy_version", "production_contract_versions",
        "generation_config_commitment", "knowledge_corpus_manifest_commitment",
        "knowledge_document_revisions", "embedding_model",
        "knowledge_build_identity", "retrieval_profile_identity",
        "applicability_profile_identity", "docker_compose_digest",
        "docker_config_digest", "docker_image_digest", "docker_namespace_identity",
        "tool_versions", "reviewer_protocol_version", "adjudication_policy_version",
        "public_observation_adapter_version", "report_generator_version",
        "required_isolation_case_ids", "required_deviation_case_ids",
        "required_refresh_case_ids", "required_restart_case_ids",
    }
    sealed = {
        "run_id": run_id, "execution_ids": execution_order,
        "scenario_inputs": [[
            f"S{index}", f"fixture:S{index}:nominal",
            commitment({"scenario": f"S{index}"})]
            for index in range(1, 7)],
        "random_seed": None, "deterministic_order_commitment": SHA_A,
        "evaluation_plan_id": plan.evaluation_plan_id,
        "oracle_revision_id": plan.oracle_revision_id,
        "oracle_content_commitment": plan.oracle_content_commitment,
        "fixture_revision": plan.fixture_revision,
        "production_build": plan.production_build,
        "model": plan.model, "prompt": plan.prompt,
        "result_schema": plan.schema, "corpus_index": plan.corpus_index,
        "evaluation_config_reference": plan.configuration,
        "evaluation_config_commitment": SHA_A,
        "repository_commit": run_payload["repository_commit"],
        "repository_dirty": True,
        "provider_profile_references": ["provider-v1"],
        "rag_profile_references": ["rag-v1"],
        "environment_identity": "test-env-v1",
        "evaluator_rubric_version": "rubric-v1",
        "polling_interval_ms": 10, "timeout_ms": 100,
        "clock_source": "monotonic", "clock_synchronization_uncertainty_ms": 0,
        "aggregation_policy_version": "SPEC-017-S4-nearest-rank-v2",
        "approved_spec_commitments": {
            name: run_payload[name] for name in extension_names},
    }
    run_payload["sealed_manifest"] = sealed
    run_payload["manifest_commitment"] = commitment(sealed)
    if run_disposition is not None:
        run_payload["run_disposition"] = run_disposition
    if missing_reproducibility_field:
        run_payload.pop(missing_reproducibility_field)
    run = store.append(LedgerRecord.create(
        "evaluation_run", [plan.evaluation_plan_id, "formal-run"], run_payload,
        plan.evaluation_plan_id,
    ))
    executions = tuple(store.append(LedgerRecord.create(
        "execution", [run.record_id, f"S{index}", 1], {
            "scenario_id": f"S{index}", "repetition": 1,
            "input_identity": f"fixture:S{index}:nominal",
            "input_commitment": commitment({"scenario": f"S{index}"}),
            "case": "NONE", "manifest_commitment": run_payload["manifest_commitment"],
            "planned_slot_execution_id": identity(
                "execution", [run.record_id, f"S{index}", 1]),
        }, run.record_id,
    )) for index in range(1, 7))
    return run, executions


def _complete_sources(store, plan, executions, *, reviews="complete",
                      include_report_components=True):
    dispositions = (
        ("OBSERVED_COMPLETION", "PASS", True, 10),
        ("TERMINAL_PRODUCTION_FAILURE", "SYSTEM_FAILURE", False, None),
        ("RIGHT_CENSORED_UNFINISHED", "TIMEOUT", False, None),
        ("INVALID_EVALUATION", "INVALID_EVALUATION", False, None),
        ("VALID_DEGRADED_COMPLETION", "RCA_FAIL", False, 20),
        ("OBSERVED_COMPLETION", "PASS", True, 30),
    )
    judgments = []
    for position, (execution, (disposition, metric, success, latency)) in enumerate(zip(
            executions, dispositions)):
        payload = {
            "disposition": disposition, "metric_disposition": metric,
            "rca_success": success, "knowledge": "MATCH",
            "failure_case": ("NONE" if disposition in {
                "OBSERVED_COMPLETION", "VALID_DEGRADED_COMPLETION",
                "RIGHT_CENSORED_UNFINISHED", "INVALID_EVALUATION"
            } else "AUTHORITATIVE_NON_SUCCESS"),
            "diagnostic_stage": "DONE", "latency_ms": latency,
        }
        if disposition == "RIGHT_CENSORED_UNFINISHED":
            payload["cutoff_ms"] = 100
        store.append(LedgerRecord.create(
            "observation", [execution.record_id, "final"], payload,
            execution.record_id,
        ))
        if include_report_components:
            store.append(LedgerRecord.create(
                "observation", [execution.record_id, "isolation-result"],
                {"kind": "ISOLATION_RESULT", "result": "PASS"},
                execution.record_id))
        stage1_ids = []
        packet = {}
        facts_commitment = SHA_A
        consensus_commitment = SHA_A
        derived_commitment = SHA_A
        oracle_scenario_commitment = SHA_B
        if reviews in {"stage1", "complete", "disagree"}:
            source = {
                "execution_id": execution.record_id,
                "validated_result_id": f"result-{position}",
                "production_output_commitment": SHA_A,
                "typed_claims_commitment": SHA_B,
                "evidence_snapshot_id": f"evidence-{position}",
                "evidence_commitment": SHA_A,
                "knowledge_snapshot_id": f"knowledge-{position}",
                "knowledge_commitment": SHA_B,
                "machine_facts": {
                    "major_contradiction": {"value": False},
                    "competing_alternatives": {"value": False},
                },
            }
            packet = {
                "schema": "SPEC-017-S3-BLIND-PACKET-v2",
                "captured": {"execution_id": execution.record_id,
                             "event_id": f"event-{position}",
                             "event_content_commitment": SHA_A},
                "source": source, "audit_metadata": [],
                "isolation_policy_identity": "trusted-policy-v1",
                "isolation_policy_commitment": SHA_A,
            }
            packet_record = store.append(LedgerRecord.create(
                "observation", [execution.record_id, "S3_BLIND_PACKET"], {
                    "kind": "BLIND_PACKET", "packet_commitment": commitment(packet),
                    "source_commitment": commitment(source),
                    "isolation_policy_commitment": SHA_A, "packet": packet,
                }, execution.record_id))
            facts = [{
                "kind": kind, "subject_id": "conclusion",
                "determination": "FALSE" if kind in {
                    "MATERIAL_CONTRADICTION", "COMPETING_ALTERNATIVE",
                    "KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY"} else "TRUE",
                "source_identity": (source["knowledge_snapshot_id"]
                                    if kind == "KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY"
                                    else source["validated_result_id"]),
                "source_commitment": (source["knowledge_commitment"]
                                      if kind == "KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY"
                                      else source["production_output_commitment"]),
            } for kind in sorted((
                "OPERATIONAL_CAUSE_GROUNDED", "COMPLETE_CAUSAL_CHAIN",
                "MATERIAL_CONTRADICTION", "COMPETING_ALTERNATIVE",
                "KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY"))]
            facts_commitment = commitment(facts)
            for reviewer in ("reviewer-a", "reviewer-b"):
                review = store.append(LedgerRecord.create(
                    "review", [execution.record_id, "STAGE1", "item", reviewer], {
                        "stage": "STAGE1", "reviewer_id": reviewer,
                        "item_id": "item", "input_commitment": commitment(packet),
                        "blind_packet_record_id": packet_record.record_id,
                        "source_commitment": commitment(source),
                        "reviewed_facts": facts,
                        "reviewed_facts_commitment": facts_commitment,
                        "reason_code": "AGREE", "rationale": "bounded",
                        "reviewed_at": "2026-10-02T02:00:00Z",
                    }, execution.record_id))
                stage1_ids.append(review.record_id)
            consensus_material = {
                "execution_id": execution.record_id, "item_id": "item",
                "blind_packet_record_id": packet_record.record_id,
                "blind_packet_commitment": commitment(packet),
                "source_commitment": commitment(source),
                "reviewed_facts": facts,
                "consensus_facts_commitment": facts_commitment,
                "review_ids": stage1_ids,
            }
            consensus_commitment = commitment(consensus_material)
            consensus_record = store.append(LedgerRecord.create(
                "review", [execution.record_id, "STAGE1_CONSENSUS", "item"], {
                    "stage": "STAGE1_CONSENSUS", **consensus_material,
                    "consensus_commitment": consensus_commitment,
                }, execution.record_id))
        stage2_ids = []
        if reviews in {"complete", "disagree"}:
            stage2_input = commitment({
                "blind_packet_commitment": commitment(packet),
                "stage1_consensus_commitment": consensus_commitment,
                "stage1_consensus_facts_commitment": facts_commitment,
                "derived_root_cause_facts_commitment": derived_commitment,
                "oracle_revision_id": plan.oracle_revision_id,
                "oracle_scenario_id": f"S{position + 1}",
                "oracle_scenario_commitment": oracle_scenario_commitment,
            })
            for reviewer in ("reviewer-c", "reviewer-d"):
                review = store.append(LedgerRecord.create(
                    "review", [execution.record_id, "STAGE2", SHA_A, reviewer], {
                        "stage": "STAGE2", "reviewer_id": reviewer,
                        "input_commitment": stage2_input,
                        "oracle_revision_id": plan.oracle_revision_id,
                        "oracle_scenario_id": f"S{position + 1}",
                        "oracle_scenario_commitment": oracle_scenario_commitment,
                        "production_output_commitment": SHA_A,
                        "typed_claims_commitment": SHA_B,
                        "blind_packet_commitment": commitment(packet),
                        "stage1_consensus_facts_commitment": facts_commitment,
                        "derived_facts_commitment": derived_commitment,
                        "top_hypothesis": {
                            "causal_class": ("OTHER" if reviews == "disagree"
                                             and reviewer == "reviewer-d" else
                                             "CREDENTIAL_ABUSE_BRUTE_FORCE"),
                            "granularity": "operational", "accepted_alternative_id": None},
                        "root_cause_success": success,
                        "reason_code": "AGREE", "rationale": "bounded",
                        "reviewed_at": "2026-10-02T02:01:00Z",
                    }, execution.record_id))
                stage2_ids.append(review.record_id)
        scoring = {
            "root_cause_identification_success": success,
            "conclusion_calibration_valid": disposition not in {
                "INVALID_EVALUATION", "TERMINAL_PRODUCTION_FAILURE"},
        }
        if reviews != "complete":
            continue
        claim_input = commitment({
            "execution_id": execution.record_id,
            "scenario_id": f"S{position + 1}",
            "typed_claims_commitment": SHA_B,
        })
        claim_findings = [
            {"production_claim_id": f"claim-{position}-1",
             "atomic_commitment": commitment({"claim": position, "atom": 1}),
             "disposition": "CONTRADICTED" if position == 0 else "SUPPORTED"},
            {"production_claim_id": f"claim-{position}-2",
             "atomic_commitment": commitment({"claim": position, "atom": 2}),
             "disposition": "SUPPORTED"},
        ]
        ucr = {
            "status": "SCORED", "numerator": 1 if position == 0 else 0,
            "denominator": 2,
            "contradicted_count": 1 if position == 0 else 0,
            "rate_fraction": [1 if position == 0 else 0, 2],
        }
        evaluated_claim_commitment = commitment({
            **ucr, "findings": claim_findings, "integrity_errors": []})
        claim_review_records = []
        for reviewer in ("claim-reviewer-a", "claim-reviewer-b"):
            claim_review_records.append(store.append(LedgerRecord.create(
                "review", [execution.record_id, "CLAIM_ADJUDICATION", reviewer], {
                    "stage": "CLAIM_ADJUDICATION", "reviewer_id": reviewer,
                    "input_commitment": claim_input,
                    "claim_facts_commitment": SHA_A,
                    "result_commitment": evaluated_claim_commitment,
                    "reason_code": "CLAIM_SUPPORT_REVIEW",
                    "rationale": "bounded", "reviewed_at": "2026-10-02T02:30:00Z",
                }, execution.record_id)))
        claim_sources = (packet_record, consensus_record, *claim_review_records)
        claim_payload = {
            "kind": "UNSUPPORTED_CLAIM_RESULT",
            "governed_adjudicator_identity": ADJUDICATION_EVALUATOR_IDENTITY,
            "adjudicator_version": ADJUDICATION_EVALUATOR_VERSION,
            "evaluation_run_id": execution.parent_id,
            "execution_id": execution.record_id,
            "scenario_id": f"S{position + 1}",
            "oracle_revision_id": plan.oracle_revision_id,
            "oracle_scenario_commitment": oracle_scenario_commitment,
            "input_commitment": claim_input,
            "production_claim_identities": [
                f"claim-{position}-1", f"claim-{position}-2"],
            "production_claim_commitments": [SHA_A, SHA_B],
            "claim_adjudications": claim_findings,
            "claim_review_ids": [item.record_id for item in claim_review_records],
            "source_record_ids": [item.record_id for item in claim_sources],
            "source_record_commitments": [
                item.semantic_commitment for item in claim_sources],
            "claim_facts_commitment": SHA_A,
            "unsupported_claim_result": ucr,
            "governed_result_commitment": commitment({
                "claim_adjudications": claim_findings, "ucr": ucr,
                "claim_facts_commitment": SHA_A,
            }),
        }
        claim_result = store._append_protected_adjudication(
            LedgerRecord.create(
                "observation", [execution.record_id,
                                "UNSUPPORTED_CLAIM_RESULT",
                                claim_payload["governed_result_commitment"]],
                claim_payload, execution.record_id),
            authority=protected_adjudication._ADJUDICATION_ISSUANCE_AUTHORITY)
        judgment_payload = {
                "kind": "ORIGINAL",
                "governed_adjudicator_identity": ADJUDICATION_EVALUATOR_IDENTITY,
                "adjudicator_version": ADJUDICATION_EVALUATOR_VERSION,
                "evaluation_run_id": execution.parent_id,
                "execution_id": execution.record_id,
                "oracle_revision_id": plan.oracle_revision_id,
                "result": "PASS" if success else "FAIL",
                "oracle_scenario_id": f"S{position + 1}",
                "oracle_scenario_commitment": oracle_scenario_commitment,
                "production_output_commitment": SHA_A,
                "typed_claims_commitment": SHA_B,
                "blind_packet_commitment": commitment(packet),
                "stage1_consensus_fact_commitment": facts_commitment,
                "stage1_consensus_commitment": consensus_commitment,
                "derived_root_cause_facts_commitment": derived_commitment,
                "final_scoring_commitment": commitment(scoring),
                "stage2_review_ids": stage2_ids,
                "root_cause_predicates": scoring,
                "claim_result_record_id": claim_result.record_id,
                "claim_result_record_commitment": claim_result.semantic_commitment,
                "unsupported_claim_result": ucr,
        }
        judgment_sources = (
            packet_record, consensus_record, claim_result,
            *(store.get(item) for item in stage2_ids))
        judgment_payload["source_record_ids"] = [
            item.record_id for item in judgment_sources]
        judgment_payload["source_record_commitments"] = [
            item.semantic_commitment for item in judgment_sources]
        judgment_payload["predicate_scoring_basis_commitment"] = commitment({
            "root_cause": scoring, "unsupported_claim_result": ucr})
        judgment_payload["judgment_commitment"] = commitment(judgment_payload)
        judgments.append(store._append_protected_adjudication(
            LedgerRecord.create(
                "judgment", [execution.record_id, "original"],
                judgment_payload, execution.record_id),
            authority=protected_adjudication._ADJUDICATION_ISSUANCE_AUTHORITY))
    return tuple(judgments)


@pytest.fixture
def reporting_store(tmp_path):
    revision = _oracle()
    plan = _plan(revision)
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "reporting.db",
        approvals=_approvals(revision),
    ) as store:
        store.admit_oracle(revision)
        store.admit_plan(plan)
        run, executions = _run(store, plan)
        judgments = _complete_sources(store, plan, executions)
        yield store, revision, plan, run, executions, judgments


def test_report_uses_snapshot_membership_and_preserves_raw_counts(reporting_store):
    store, _, _, run, executions, _ = reporting_store
    report = build_formal_report(store.reader(), run.record_id)

    assert tuple(row.execution_id for row in report.executions) == tuple(sorted(
        item.record_id for item in executions))
    assert {row.scenario_id for row in report.executions} == {
        "S1", "S2", "S3", "S4", "S5", "S6"}
    assert report.source_cut_commitment == store.reader().read_run_snapshot(
        run.record_id).report_source_cut_commitment
    assert report.root_cause_metric.rate_fraction == (2, 6)
    assert report.root_cause_metric.macro_primary_rate_fraction == (1, 3)
    assert report.root_cause_metric.micro_supplemental_rate_fraction == (2, 6)
    assert (report.root_cause_metric.failure_count,
            report.root_cause_metric.censor_count,
            report.root_cause_metric.invalid_count) == (1, 1, 1)
    assert report.unsupported_claim_metric.rate_fraction == (1, 12)
    assert report.unsupported_claim_metric.micro_95_interval is not None
    assert report.unsupported_claim_metric.macro_95_interval is not None
    assert report.contradicted_claim_count == 1
    assert tuple(item.scenario_id for item in report.per_scenario) == (
        "S1", "S2", "S3", "S4", "S5", "S6")
    primary, refresh, restart = report.latency
    assert primary.kind == "PRIMARY_FULL_E2E"
    assert (primary.observed_count, primary.failure_count,
            primary.censor_count, primary.invalid_count) == (3, 1, 1, 1)
    assert primary.median_ms_fraction == (40, 2) and primary.p95_ms == 30
    assert primary.start_boundaries == (
        "EVALUATION_SIGNAL_PUBLICLY_OBSERVABLE",)
    assert primary.end_boundaries == (
        "USABLE_CURRENT_RCA_PUBLICLY_OBSERVED",)
    assert primary.polling_intervals_ms == (10,) and primary.cutoffs_ms == (100,)
    assert refresh.kind == "REFRESH" and refresh.observed_count == 0
    assert restart.kind == "RESTART_RECOVERY" and restart.observed_count == 0


def test_manifest_completeness_cannot_mint_recovery_or_no_deviation_truth(
    reporting_store,
):
    store, _, _, run, executions, _ = reporting_store
    report = build_formal_report(store.reader(), run.record_id)

    assert report.knowledge_distribution.outcome_counts == (("MATCH", 6),)
    assert dict(report.degraded_outcome_distribution.outcome_counts) == {
        "INVALID_EVALUATION": 1, "OBSERVED_COMPLETION": 2,
        "RIGHT_CENSORED_UNFINISHED": 1,
        "TERMINAL_PRODUCTION_FAILURE": 1,
        "VALID_DEGRADED_COMPLETION": 1,
    }
    assert report.isolation_results.outcome_counts == (("PASS", 6),)
    assert report.deviation_results.outcome_counts == ()
    assert not report.deviation_results.complete
    assert report.abort_incomplete_results.outcome_counts == (
        ("INVALID_EVALUATION", 1), ("OBSERVED_COMPLETION", 2),
        ("RIGHT_CENSORED_UNFINISHED", 1),
        ("TERMINAL_PRODUCTION_FAILURE", 1),
        ("VALID_DEGRADED_COMPLETION", 1))
    assert report.refresh_version_results.outcome_counts == (
        ("NOT_APPLICABLE", 1),)
    assert report.restart_recovery_results.outcome_counts == (
        ("NOT_APPLICABLE", 1),)
    snapshot_ids = {item.record_id for item in store.reader().read_run_snapshot(
        run.record_id).records}
    for component in (
        report.isolation_results, report.deviation_results,
        report.refresh_version_results, report.restart_recovery_results,
    ):
        assert set(component.evidence_record_ids) <= snapshot_ids


@pytest.mark.parametrize("missing", [
    "oracle_approval_reference", "fixture_content_commitments",
    "production_contract_versions", "docker_image_digest",
    "reviewer_protocol_version", "report_generator_version",
])
def test_missing_each_required_reproducibility_group_blocks_readiness(tmp_path, missing):
    revision = _oracle(); plan = _plan(revision)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / f"missing-{missing}.db",
                               approvals=_approvals(revision)) as store:
        store.admit_oracle(revision); store.admit_plan(plan)
        run, executions = _run(
            store, plan, missing_reproducibility_field=missing)
        _complete_sources(store, plan, executions)
        report = build_formal_report(store.reader(), run.record_id)

    assert not report.readiness.evaluation_ready
    assert not report.readiness.formal_evaluation_pass
    assert f"MANIFEST_{missing.upper()}_RUN_BINDING_MISMATCH" in report.readiness.blockers


def test_sealed_manifest_binds_all_authoritative_run_families(reporting_store):
    store, _, _, run, executions, _ = reporting_store
    snapshot = store.reader().read_run_snapshot(run.record_id)
    root = json.loads(next(item.semantic_json for item in snapshot.records
                           if item.record_id == run.record_id))
    execution_payloads = tuple(
        (execution.record_id, json.loads(next(
            item.semantic_json for item in snapshot.records
            if item.record_id == execution.record_id)))
        for execution in executions)
    result = verify_run_manifest_binding(
        root, evaluation_run_id=run.record_id,
        evaluation_plan_id=snapshot.evaluation_plan_id,
        oracle_revision_id=snapshot.oracle_revision_id,
        executions=execution_payloads)
    # This historical helper intentionally has no execution classification.
    # Its sealed facts remain readable, but it is not Formal-pass eligible.
    assert not result.complete
    assert result.blockers == ("EXECUTION_MODE_NOT_POSITIVELY_FORMAL",)


def test_formal_classification_without_admission_lineage_fails_closed(
    reporting_store,
):
    from rca_evaluation.reporting import _project
    store, _, _, run, _, _ = reporting_store
    snapshot = store.reader().read_run_snapshot(run.record_id)
    root = json.loads(next(item.semantic_json for item in snapshot.records
                           if item.record_id == run.record_id))
    root["execution_mode"] = "FORMAL"
    corrupted = replace(snapshot, records=tuple(
        replace(item, semantic_json=canonical_bytes(root).decode("utf-8"),
                semantic_commitment=commitment(root))
        if item.record_id == run.record_id else item for item in snapshot.records))

    report = _project(corrupted)
    assert not report.readiness.evaluation_ready
    assert not report.readiness.formal_evaluation_pass
    assert "FORMAL_RUN_NOT_ELIGIBLE" in report.readiness.blockers
    assert "MANIFEST_FORMAL_ADMISSION_LINEAGE_INCOHERENT" in report.readiness.blockers


@pytest.mark.parametrize("family,field", [
    ("repository", "repository_commit"),
    ("oracle", "oracle_content_commitment"),
    ("fixture", "fixture_content_commitments"),
    ("provider", "provider_profile_references"),
    ("knowledge", "knowledge_build_identity"),
    ("review", "reviewer_protocol_version"),
    ("docker", "docker_image_digest"),
    ("environment", "environment_identity"),
    ("adapter", "public_observation_adapter_version"),
])
def test_each_reproducibility_family_rejects_unsealed_run_value(
    reporting_store, family, field,
):
    store, _, _, run, _, _ = reporting_store
    snapshot = store.reader().read_run_snapshot(run.record_id)
    root = json.loads(next(item.semantic_json for item in snapshot.records
                           if item.record_id == run.record_id))
    root[field] = SHA_B if field.endswith(("commitment", "digest")) else f"wrong-{family}"
    report_snapshot = replace(snapshot, records=tuple(
        replace(item, semantic_json=canonical_bytes(root).decode("utf-8"),
                semantic_commitment=commitment(root))
        if item.record_id == run.record_id else item for item in snapshot.records))
    from rca_evaluation.reporting import _project
    report = _project(report_snapshot)
    assert not report.readiness.evaluation_ready
    assert any(blocker.startswith("MANIFEST_") for blocker in report.readiness.blockers)


@pytest.mark.parametrize("reviews", ["none", "stage1", "disagree"])
def test_missing_required_review_stage_blocks_formal_pass(tmp_path, reviews):
    revision = _oracle(); plan = _plan(revision)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / f"reviews-{reviews}.db",
                               approvals=_approvals(revision)) as store:
        store.admit_oracle(revision); store.admit_plan(plan)
        run, executions = _run(store, plan)
        _complete_sources(store, plan, executions, reviews=reviews)
        report = build_formal_report(store.reader(), run.record_id)

    assert "REVIEW_INCOMPLETE" in report.readiness.blockers
    assert not report.readiness.evaluation_executed
    assert not report.readiness.formal_evaluation_pass


def test_incomplete_admitted_campaign_blocks_formal_pass(tmp_path):
    revision = _oracle(); plan = _plan(revision)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / "campaign-incomplete.db",
                               approvals=_approvals(revision)) as store:
        store.admit_oracle(revision); store.admit_plan(plan)
        run, executions = _run(store, plan, fixed_n=2)
        _complete_sources(store, plan, executions)
        report = build_formal_report(store.reader(), run.record_id)

    assert "REQUIRED_CAMPAIGN_INCOMPLETE" in report.readiness.blockers
    assert not report.readiness.formal_evaluation_pass


@pytest.mark.parametrize("component", ["refresh", "restart"])
def test_missing_required_13_1_component_blocks_formal_completion(tmp_path, component):
    revision = _oracle(); plan = _plan(revision)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / "component-incomplete.db",
                               approvals=_approvals(revision)) as store:
        store.admit_oracle(revision); store.admit_plan(plan)
        run, executions = _run(
            store, plan, require_refresh=component == "refresh",
            require_restart=component == "restart")
        _complete_sources(
            store, plan, executions, include_report_components=False)
        report = build_formal_report(store.reader(), run.record_id)

    selected = (report.refresh_version_results if component == "refresh"
                else report.restart_recovery_results)
    assert not selected.complete
    assert selected.required_case_ids
    assert "REQUIRED_REPORT_COMPONENT_INCOMPLETE" in report.readiness.blockers
    assert not report.readiness.formal_evaluation_pass


def test_plan_required_refresh_and_restart_cases_require_protected_facts(tmp_path):
    revision = _oracle(); plan = _plan(revision)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / "case-complete.db",
                               approvals=_approvals(revision)) as store:
        store.admit_oracle(revision); store.admit_plan(plan)
        run, executions = _run(
            store, plan, require_refresh=True, require_restart=True)
        _complete_sources(store, plan, executions)
        report = build_formal_report(store.reader(), run.record_id)

    refresh = report.refresh_version_results
    restart = report.restart_recovery_results
    assert not refresh.complete and not restart.complete
    assert refresh.cases == () and restart.cases == ()
    assert "REQUIRED_REPORT_COMPONENT_INCOMPLETE" in report.readiness.blockers


def test_right_censored_is_not_aborted_and_formal_abort_is_distinct(reporting_store):
    store, _, _, run, _, _ = reporting_store
    report = build_formal_report(store.reader(), run.record_id)
    counts = dict(report.abort_incomplete_results.outcome_counts)
    assert counts["RIGHT_CENSORED_UNFINISHED"] == 1
    assert "ABORTED" not in counts


def test_formal_aborted_run_is_reported_separately_from_censor(tmp_path):
    revision = _oracle(); plan = _plan(revision)
    with SqliteEvaluationStore(tmp_path / "rca_evaluation" / "aborted.db",
                               approvals=_approvals(revision)) as store:
        store.admit_oracle(revision); store.admit_plan(plan)
        run, executions = _run(store, plan, run_disposition="ABORTED")
        _complete_sources(store, plan, executions)
        report = build_formal_report(store.reader(), run.record_id)
    counts = dict(report.abort_incomplete_results.outcome_counts)
    assert counts["ABORTED"] == 1
    assert counts["RIGHT_CENSORED_UNFINISHED"] == 1


@pytest.mark.parametrize("missing", [
    "executed", "evidence", "review", "campaign", "judgment_authority",
    "ucr_authority", "isolation", "deviation", "refresh", "restart",
    "external_gates", "root_cause", "unsupported_claims", "calibration",
    "report_source_cut", "closure",
])
def test_false_pass_matrix_rejects_each_single_missing_predicate(missing):
    from rca_evaluation.reporting import _formal_pass_gate
    predicates = {
            "executed": True, "campaign": True, "evidence": True,
            "review": True, "judgment_authority": True,
            "ucr_authority": True,
        "isolation": True, "deviation": True, "refresh": True,
        "restart": True, "external_gates": True, "root_cause": True,
            "unsupported_claims": True, "calibration": True,
            "report_source_cut": True, "closure": True,
    }
    assert _formal_pass_gate(**predicates)
    predicates[missing] = False
    assert not _formal_pass_gate(**predicates)


def test_complete_deviation_is_not_implicitly_successful():
    from rca_evaluation.reporting import _deviation_component, _formal_pass_gate
    execution_id = identity("execution", ["deviation-negative"])
    payload = {
        "kind": "DEVIATION_RESULT", "case_id": "case:deviation",
        "outcome": "DEVIATION", "governed_evaluator_identity": "governed",
    }
    record = LedgerRecord.create(
        "observation", [execution_id, "deviation-negative"], payload,
        execution_id)
    component = _deviation_component(
        ("case:deviation",), ((record, payload),))
    assert component.complete and not component.passed
    predicates = {name: True for name in (
        "executed", "campaign", "evidence", "review", "judgment_authority",
        "ucr_authority", "isolation", "deviation", "refresh", "restart",
        "external_gates", "root_cause", "unsupported_claims", "calibration",
        "report_source_cut", "closure")}
    predicates["deviation"] = component.passed
    assert not _formal_pass_gate(**predicates)


@pytest.mark.parametrize("disposition", ("FAILED", "RECOVERY_AMBIGUOUS"))
def test_complete_restart_non_success_cannot_formal_pass(disposition):
    from rca_evaluation.reporting import _case_component, _formal_pass_gate
    execution_id = identity("execution", ["restart-negative", disposition])
    payload = {
        "kind": "RESTART_RECOVERY_RESULT", "case_id": "case:restart",
        "passed": False, "disposition": disposition,
        "before_state": "before", "resulting_state": "after",
        "recovered_identity": "operation:restart",
        "duplicate_effect_detected": False,
        "publication_continuity": "UNCHANGED",
        "latency_disposition": "FAILURE",
    }
    record = LedgerRecord.create(
        "observation", [execution_id, "restart-negative", disposition],
        payload, execution_id)
    component = _case_component(
        "RESTART_RECOVERY_RESULTS", ("case:restart",), ((record, payload),),
        required_fields=("before_state", "resulting_state", "recovered_identity",
                         "duplicate_effect_detected", "publication_continuity",
                         "latency_disposition"))
    assert component.complete and not component.passed
    predicates = {name: True for name in (
        "executed", "campaign", "evidence", "review", "judgment_authority",
        "ucr_authority", "isolation", "deviation", "refresh", "restart",
        "external_gates", "root_cause", "unsupported_claims", "calibration",
        "report_source_cut", "closure")}
    predicates["restart"] = component.passed
    assert not _formal_pass_gate(**predicates)


def test_caller_ucr_summary_is_not_formal_metric_truth(reporting_store):
    store, _, _, run, executions, _ = reporting_store
    before = build_formal_report(store.reader(), run.record_id)
    store.append(LedgerRecord.create(
        "observation", [executions[0].record_id, "caller-ucr-summary"],
        {"kind": "CALLER_UCR_SUMMARY", "ucr_fraction": [0, 999]},
        executions[0].record_id))
    after = build_formal_report(store.reader(), run.record_id)
    assert after.unsupported_claim_metric == before.unsupported_claim_metric


def test_ready_without_external_execution_and_engineering_stages_are_separate(tmp_path):
    revision = _oracle(); plan = _plan(revision)
    with SqliteEvaluationStore(
            tmp_path / "rca_evaluation" / "ready-not-executed.db",
            approvals=_approvals(revision)) as store:
        store.admit_oracle(revision); store.admit_plan(plan)
        run, executions = _run(
            store, plan, require_refresh=True, require_restart=True)
        _complete_sources(store, plan, executions)
        for gate in ("REAL_LLM", "REAL_RAG", "DOCKER_RCA_E2E"):
            store.append(LedgerRecord.create(
                "gate_authorization", [run.record_id, gate, f"{gate}-profile", SHA_A],
                {"evaluation_run_id": run.record_id, "gate": gate,
                 "profile_identity": f"{gate}-profile",
                 "profile_commitment": SHA_A}, run.record_id))
        report = build_formal_report(store.reader(), run.record_id)
    assert report.readiness.engineering_implementation_complete
    assert report.readiness.engineering_complete
    assert not report.readiness.evaluation_ready
    assert not report.readiness.evaluation_executed
    assert not report.readiness.formal_evaluation_pass
    assert "FORMAL_RUN_NOT_ELIGIBLE" in report.readiness.blockers
    assert all(item.state == "NOT_EXECUTED" for item in report.real_gates)


def test_invalid_protected_judgment_is_an_engineering_blocker(reporting_store):
    from rca_evaluation.reporting import _project
    store, _, _, run, _, judgments = reporting_store
    snapshot = store.reader().read_run_snapshot(run.record_id)
    target = judgments[0]
    payload = json.loads(target.semantic_json)
    payload["governed_adjudicator_identity"] = "caller"
    corrupted = replace(
        target, semantic_json=canonical_bytes(payload).decode("utf-8"),
        semantic_commitment=commitment(payload))
    projected = _project(replace(
        snapshot, records=tuple(
            corrupted if item.record_id == target.record_id else item
            for item in snapshot.records)))
    assert not projected.readiness.engineering_complete
    assert not projected.readiness.evaluation_ready
    assert "PROTECTED_JUDGMENT_AUTHORITY_INVALID" in projected.readiness.blockers


def test_replacement_and_diagnostic_rows_do_not_change_formal_denominator(
    reporting_store,
):
    store, _, _, run, executions, _ = reporting_store
    replacement = store.append(LedgerRecord.create(
        "execution", [run.record_id, "S1", 1, "replacement", executions[0].record_id], {
            "scenario_id": "S1", "repetition": 1,
            "input_identity": "replacement", "input_commitment": SHA_A,
            "planned_slot_execution_id": executions[0].record_id,
            "replacement_for_execution_id": executions[0].record_id,
        }, run.record_id))
    diagnostic = store.append(LedgerRecord.create(
        "execution", [run.record_id, "S2", 1, "replacement", executions[1].record_id], {
            "scenario_id": "S2", "repetition": 1,
            "input_identity": "diagnostic", "input_commitment": SHA_B,
            "planned_slot_execution_id": executions[1].record_id,
            "replacement_for_execution_id": executions[1].record_id,
            "execution_purpose": "DIAGNOSTIC",
        }, run.record_id))

    report = build_formal_report(store.reader(), run.record_id)
    assert {replacement.record_id, diagnostic.record_id} <= {
        row.execution_id for row in report.executions}
    assert report.root_cause_metric.denominator == 6
    assert report.unsupported_claim_metric.denominator == 12
    assert report.conclusion_calibration_metric.denominator == 6


def test_missing_each_real_gate_blocks_formal_pass_without_becoming_failure(
    reporting_store,
):
    store, _, _, run, _, _ = reporting_store
    report = build_formal_report(store.reader(), run.record_id)

    assert not report.readiness.formal_evaluation_pass
    assert {item.gate: item.state for item in report.real_gates} == {
        "REAL_LLM": "NOT_EXECUTED",
        "REAL_RAG": "NOT_EXECUTED",
        "DOCKER_RCA_E2E": "NOT_EXECUTED",
    }
    assert {
        "LIVE_LLM_NOT_EXECUTED", "REAL_RAG_NOT_EXECUTED",
        "DOCKER_FORMAL_E2E_NOT_EXECUTED",
    } <= set(report.readiness.blockers)
    assert not report.readiness.product_acceptance_complete
    assert not report.readiness.production_ready


def test_start_without_terminal_is_projected_as_incomplete(reporting_store):
    store, _, _, run, executions, _ = reporting_store
    store.append(LedgerRecord.create(
        "gate_authorization", [run.record_id, "REAL_LLM", "profile", SHA_A],
        {"evaluation_run_id": run.record_id, "gate": "REAL_LLM",
         "profile_identity": "profile", "profile_commitment": SHA_A},
        run.record_id,
    ))
    store._record_actual_execution_start(
        evaluation_run_id=run.record_id,
        execution_id=executions[0].record_id, gate="REAL_LLM",
        profile_identity="profile", profile_commitment=SHA_A,
        invocation_id="incomplete-invocation",
        started_at="2026-10-02T02:00:00Z", invocation_pins={},
    )

    report = build_formal_report(store.reader(), run.record_id)
    llm = next(item for item in report.real_gates if item.gate == "REAL_LLM")
    assert llm.state == "EXECUTION_STARTED_INCOMPLETE"
    assert not llm.formal_pass
    assert "LIVE_LLM_INCOMPLETE" in report.readiness.blockers


def test_evidence_index_is_secret_free_and_deterministic(reporting_store):
    store, _, _, run, executions, _ = reporting_store
    store.append(LedgerRecord.create(
        "observation", [executions[0].record_id, "secret-shaped"], {
            "kind": "PROVIDER_DIAGNOSTIC", "Authorization": "Bearer secret",
            "credential": "do-not-index", "provider_payload": "unbounded-private",
        }, executions[0].record_id,
    ))
    first = build_formal_report(store.reader(), run.record_id)
    second = build_formal_report(store.reader(), run.record_id)

    assert first == second
    assert first.report_semantic_commitment == second.report_semantic_commitment
    assert (first.evidence_index.evidence_index_commitment ==
            second.evidence_index.evidence_index_commitment)
    material = repr(first.evidence_index)
    assert "Bearer secret" not in material
    assert "do-not-index" not in material
    assert "unbounded-private" not in material


def test_evidence_index_preserves_safe_real_rag_build_and_profile_pins(
    reporting_store,
):
    store, _, _, run, executions, _ = reporting_store
    store.append(LedgerRecord.create(
        "gate_authorization", [run.record_id, "REAL_RAG", "rag-profile", SHA_A],
        {"evaluation_run_id": run.record_id, "gate": "REAL_RAG",
         "profile_identity": "rag-profile", "profile_commitment": SHA_A},
        run.record_id,
    ))
    start = store._record_actual_execution_start(
        evaluation_run_id=run.record_id,
        execution_id=executions[0].record_id, gate="REAL_RAG",
        profile_identity="rag-profile", profile_commitment=SHA_A,
        invocation_id="rag-incomplete", started_at="2026-10-02T02:00:00Z",
        invocation_pins={
            "manifest_commitment": SHA_B,
            "release_reference": "release-1",
            "embedding_provider": "google",
            "embedding_model": "embedding-model",
            "embedding_profile_identity": "embedding-profile",
            "retrieval_profile_identity": "retrieval-profile",
            "index_engine": "chroma",
            "index_schema_identity": "schema-1",
            "chroma_reference": "private-local-path-not-indexed",
            "retrieval_operation_identity": "operation-1",
            "expected_build_context": {
                "active_build_identity": "build-1",
                "validated_build_commitment": SHA_A,
                "manifest_identity": "manifest-1",
                "manifest_commitment": SHA_B,
                "corpus_identity": "corpus-1",
                "corpus_version": "v1",
                "artifact_commitment": SHA_A,
                "lineage_commitment": SHA_B,
                "staged_commitment": SHA_A,
            },
        },
    )
    report = build_formal_report(store.reader(), run.record_id)
    entry = next(item for item in report.evidence_index.entries
                 if item.record_id == start.record_id)
    references = dict(entry.safe_references)

    assert references["profile_identity"] == "rag-profile"
    assert references["profile_commitment"] == SHA_A
    assert references["invocation_pins.manifest_commitment"] == SHA_B
    assert (references[
        "invocation_pins.expected_build_context.active_build_identity"] ==
        "build-1")
    assert "private-local-path-not-indexed" not in repr(entry)


def test_original_and_all_authorized_readjudication_branches_are_preserved(tmp_path):
    first = _oracle("first")
    second = _oracle("second", first.oracle_revision_id)
    third = _oracle("third", first.oracle_revision_id)
    plan = _plan(first)
    run_id = LedgerRecord.create(
        "evaluation_run", [plan.evaluation_plan_id, "formal-run"], {},
        plan.evaluation_plan_id).record_id
    execution_id = LedgerRecord.create(
        "execution", [run_id, "S1", 1], {}, run_id).record_id
    original_id = LedgerRecord.create(
        "judgment", [execution_id, "original"], {}, execution_id).record_id
    amendments = (
        TrustedAmendmentAuthorization(
            "auth-second", PM_AUTHORITY, original_id,
            first.oracle_revision_id, second.oracle_revision_id),
        TrustedAmendmentAuthorization(
            "auth-third", PM_AUTHORITY, original_id,
            first.oracle_revision_id, third.oracle_revision_id),
    )
    with SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "readjudication-report.db",
        approvals=_approvals(first, second, third, amendments=amendments),
    ) as store:
        for revision in (first, second, third):
            store.admit_oracle(revision)
        store.admit_plan(plan)
        run, executions = _run(store, plan)
        judgments = _complete_sources(store, plan, executions)
        original = judgments[0]
        for label, revision, authorization in (
            ("second", second, "auth-second"),
            ("third", third, "auth-third"),
        ):
            store.append(LedgerRecord.create(
                "re_adjudication", [original.record_id, label], {
                    "run_id": run.record_id,
                    "original_judgment_id": original.record_id,
                    "original_oracle_revision_id": first.oracle_revision_id,
                    "new_oracle_revision_id": revision.oracle_revision_id,
                    "reason": "authorized correction",
                    "authorization_reference": authorization,
                    "result": "PASS" if label == "third" else "FAIL",
                }, original.record_id,
            ))

        report = build_formal_report(store.reader(), run.record_id)
        assert any(item.judgment_id == original.record_id
                   for item in report.original_judgments)
        assert len(report.authorized_re_adjudications) == 2
        assert report.re_adjudication_ambiguous


def test_nonformal_projection_cannot_commit_and_ignores_caller_verdict(
    reporting_store,
):
    store, _, _, run, executions, _ = reporting_store
    report = build_formal_report(store.reader(), run.record_id)
    with pytest.raises(ValueError, match="FORMAL admission provenance"):
        append_report_commitment(
            store, report, generated_at="2026-10-02T03:00:00Z")

    store.append(LedgerRecord.create(
        "report", [run.record_id, "caller-crafted"], {
            "formal_evaluation_pass": True,
            "product_acceptance_complete": True,
            "production_ready": True,
        }, run.record_id,
    ))
    ignored = build_formal_report(store.reader(), run.record_id)
    assert not ignored.readiness.formal_evaluation_pass
    assert not ignored.readiness.product_acceptance_complete
    assert not ignored.readiness.production_ready
    assert ignored.source_cut_commitment == report.source_cut_commitment

    forged = replace(
        report, readiness=replace(
            report.readiness, formal_evaluation_pass=True, blockers=()))
    with pytest.raises(ValueError, match="FORMAL admission provenance"):
        append_report_commitment(
            store, forged, generated_at="2026-10-02T03:01:00Z")

    store.append(LedgerRecord.create(
        "observation", [executions[0].record_id, "late-source"],
        {"kind": "LATE_SOURCE"}, executions[0].record_id,
    ))
    assert (build_formal_report(store.reader(), run.record_id).source_cut_commitment
            != report.source_cut_commitment)


def test_generated_projection_is_immutable_and_not_an_authority(reporting_store):
    store, _, _, run, _, _ = reporting_store
    report = build_formal_report(store.reader(), run.record_id)
    with pytest.raises(FrozenInstanceError):
        report.schema_version = "mutated"
    mutated_copy = replace(report, schema_version="LOCAL-NON-AUTHORITY")

    assert mutated_copy != report
    assert build_formal_report(store.reader(), run.record_id) == report
