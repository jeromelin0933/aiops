from dataclasses import replace
import json
from pathlib import Path

import pytest

import rca_evaluation.formal_evidence as subject
from rca_evaluation.evidence_index import (
    EvidenceCategory, append_evidence_index_commitment, build_evidence_index,
    evidence_index_commitment_is_current,
)
from rca_evaluation.formal_evidence import (
    ClosureStage, FormalGateEvidence, GateEvidenceState, ManifestCurrentState,
    RepositoryManifestCurrentStateResolver,
    append_durable_gate_receipt, authorize_formal_execution,
    build_final_readiness_projection, build_reproducibility_closure_manifest,
    verify_manifest_freshness,
)
from rca_evaluation.identity import commitment
from rca_evaluation.ledger import LedgerConflict, LedgerRecord
from rca_evaluation.readiness import (
    FormalRequiredFactsResolution, ReadinessFactState, RequiredFact,
    RequiredFactsMaterialization,
)
from rca_evaluation.reporting import build_formal_report
from tests.test_spec_017_r1_formal_admission import (
    _admit, _formal_runner, _issue_formal_root, _validate,
)


ROOT = Path(__file__).resolve().parents[1]
SECRET = "SPEC017_TEST_SECRET_DO_NOT_PERSIST"
GT = "expected_root_cause=SPEC017_SHOULD_NEVER_PERSIST"
HEAD = "e9cee50ce7904fb822e8032bbe8b05a4a826cf07"
BASE_CONFIG = ROOT / "configs/rca_evaluation/formal_preregistration_v1.json"


class _CurrentResolver:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def resolve(self, reader, evaluation_run_id, manifest):
        self.calls += 1
        return self.value


def _current(report, **changes):
    manifest = report.manifest
    approved = manifest.approved_spec_commitments
    values = {
        "repository_commit": manifest.repository_commit,
        "repository_dirty": manifest.repository_dirty,
        "raw_template_commitment": report.preregistration_commitment,
        "evaluation_config_commitment": manifest.evaluation_config_commitment,
        "oracle_revision_id": manifest.oracle_revision_id,
        "oracle_content_commitment": manifest.oracle_content_commitment,
        "fixture_revision": manifest.fixture_revision,
        "provider_profile_references": manifest.provider_profile_references,
        "rag_profile_references": manifest.rag_profile_references,
        "environment_identity": manifest.environment_identity,
        "docker_compose_commitment": approved["docker_compose_digest"],
        "docker_config_commitment": approved["docker_config_digest"],
        "topology_commitment": report.expected_topology_commitment,
        "interruption_matrix_identity":
            report.expected_interruption_matrix_identity,
        "blockers": (),
    }
    values.update(changes)
    return ManifestCurrentState.create(**values)


def _admitted_store(tmp_path, monkeypatch, *, execution_count=30):
    report, store, runner = _formal_runner(_admit(tmp_path), monkeypatch)
    _issue_formal_root(store, runner, report)
    executions = []
    if execution_count:
        for item in report.campaign.executions[:execution_count]:
            executions.append(store.append(LedgerRecord.create(
                "execution", [report.campaign.run_id, item.scenario_id,
                              item.repetition], {
                    "scenario_id": item.scenario_id,
                    "repetition": item.repetition,
                    "input_identity": item.input_identity,
                    "input_commitment": item.input_commitment,
                    "case": item.case.value,
                    "manifest_commitment": report.manifest.manifest_commitment,
                    "planned_slot_execution_id": item.execution_id,
                }, report.campaign.run_id)))
    return report, store, tuple(executions)


def _resolved_cut():
    fact = RequiredFact(
        "fresh execution readiness", ReadinessFactState.RESOLVED,
        "verified", "governed test resolver", "fresh-read")
    return FormalRequiredFactsResolution(
        RequiredFactsMaterialization.create({
            "/evaluation_plan/production_build": fact}), (fact,))


def _materializing_resolver(drift: dict[str, object]):
    values = {
        "/evaluation_plan/production_build": HEAD,
        "/evaluation_plan/corpus_index": "knowledge-build:materialized",
        "/manifest_inputs/environment_identity": "pytest:materialized",
        "/manifest_inputs/approved_spec_commitments/knowledge_document_revisions":
            ["test-doc@materialized"],
        "/manifest_inputs/approved_spec_commitments/knowledge_build_identity":
            "knowledge-build:materialized",
        "/manifest_inputs/approved_spec_commitments/docker_image_digest":
            "sha256:" + "a" * 64,
        "/manifest_inputs/approved_spec_commitments/docker_namespace_identity":
            "spec017-preflight-namespace",
        "/manifest_inputs/approved_spec_commitments/tool_versions/python":
            "3.test",
        "/manifest_inputs/approved_spec_commitments/tool_versions/docker":
            "docker-test",
    }

    def resolve(template, **kwargs):
        current = dict(values)
        store = kwargs.get("evaluation_store")
        preparation_id = kwargs.get("preparation_id")
        if store is not None and preparation_id is not None:
            prepared = store.get_preparation(preparation_id)
            if prepared is not None:
                current[
                    "/manifest_inputs/approved_spec_commitments/"
                    "docker_namespace_identity"
                ] = prepared["docker_namespace"]["namespace_identity"]
        current.update(drift)
        facts = {
            path: RequiredFact(
                path.rsplit("/", 1)[-1], ReadinessFactState.RESOLVED,
                value, "governed test current authority", "fresh-read")
            for path, value in current.items()
        }
        return FormalRequiredFactsResolution(
            RequiredFactsMaterialization.create(facts), tuple(facts.values()))

    return resolve


def test_raw_template_and_materialized_config_use_two_stage_fresh_authority(
    tmp_path, monkeypatch,
):
    raw = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    config_path = tmp_path / "formal.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    drift: dict[str, object] = {}
    resolver = _materializing_resolver(drift)
    monkeypatch.setattr(
        "rca_evaluation.formal_admission.resolve_formal_required_facts",
        resolver)
    preflight = _admit(tmp_path, preregistration_path=config_path)
    report, store, runner = _formal_runner(
        preflight, monkeypatch, required_facts_resolver=resolver)
    monkeypatch.setattr(subject, "resolve_formal_required_facts", resolver)
    try:
        assert report.preregistration_commitment == commitment(raw)
        assert report.materialized_config_commitment == (
            report.manifest.evaluation_config_commitment)
        assert report.preregistration_commitment != (
            report.materialized_config_commitment)

        authorization = authorize_formal_execution(
            admission_report=report, evaluation_store=store,
            preregistration_path=config_path, repository_root=ROOT,
            execution_root=report.evaluation_root,
            execution_workspace=report.evaluation_root)
        authorization.validate()

        _issue_formal_root(store, runner, report)
        for item in report.campaign.executions:
            store.append(LedgerRecord.create(
                "execution", [report.campaign.run_id, item.scenario_id,
                              item.repetition], {
                    "scenario_id": item.scenario_id,
                    "repetition": item.repetition,
                    "input_identity": item.input_identity,
                    "input_commitment": item.input_commitment,
                    "case": item.case.value,
                    "manifest_commitment": report.manifest.manifest_commitment,
                    "planned_slot_execution_id": item.execution_id,
                }, report.campaign.run_id))
        current_resolver = RepositoryManifestCurrentStateResolver(
            evaluation_store=store, repository_root=ROOT,
            preregistration_path=config_path)
        current = current_resolver.resolve(
            store.reader(), report.campaign.run_id, report.manifest)
        assert current.raw_template_commitment == report.preregistration_commitment
        assert current.evaluation_config_commitment == (
            report.materialized_config_commitment)
        freshness = verify_manifest_freshness(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=current_resolver)
        assert freshness.current

        drift["/evaluation_plan/corpus_index"] = "knowledge-build:drift"
        with pytest.raises(ValueError, match="materialized Formal configuration drift"):
            authorize_formal_execution(
                admission_report=report, evaluation_store=store,
                preregistration_path=config_path, repository_root=ROOT,
                execution_root=report.evaluation_root,
                execution_workspace=report.evaluation_root)
        stale = verify_manifest_freshness(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=current_resolver)
        assert not stale.current
        assert "CURRENT_CONFIG_DRIFT" in stale.blockers

        drift.clear()
        changed = dict(raw)
        changed["denominator_policy"] = dict(raw["denominator_policy"])
        changed["denominator_policy"]["nominal"] += "; drift"
        config_path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValueError, match="pre-registration drift"):
            authorize_formal_execution(
                admission_report=report, evaluation_store=store,
                preregistration_path=config_path, repository_root=ROOT,
                execution_root=report.evaluation_root,
                execution_workspace=report.evaluation_root)
        raw_stale = verify_manifest_freshness(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=current_resolver)
        assert not raw_stale.current
        assert "CURRENT_STATE_AUTHORITY_UNRESOLVED" in raw_stale.blockers

        config_path.write_text(json.dumps(raw), encoding="utf-8")

        def unavailable(*args, **kwargs):
            raise RuntimeError("current required-fact authority unavailable")

        monkeypatch.setattr(subject, "resolve_formal_required_facts", unavailable)
        with pytest.raises(RuntimeError, match="authority unavailable"):
            authorize_formal_execution(
                admission_report=report, evaluation_store=store,
                preregistration_path=config_path, repository_root=ROOT,
                execution_root=report.evaluation_root,
                execution_workspace=report.evaluation_root)
        unresolved = verify_manifest_freshness(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=current_resolver)
        assert not unresolved.current
        assert "CURRENT_STATE_AUTHORITY_UNRESOLVED" in unresolved.blockers

        with pytest.raises(TypeError, match="unexpected keyword"):
            authorize_formal_execution(
                admission_report=report, evaluation_store=store,
                preregistration_path=config_path, repository_root=ROOT,
                execution_root=report.evaluation_root,
                execution_workspace=report.evaluation_root,
                expected_materialized_commitment=(
                    report.materialized_config_commitment))
    finally:
        store.close()


def test_supported_execution_entry_requires_durable_admission_and_fresh_facts(
    tmp_path, monkeypatch,
):
    report, store, runner = _formal_runner(_admit(tmp_path), monkeypatch)
    monkeypatch.setattr(subject, "resolve_formal_required_facts",
                        lambda *args, **kwargs: _resolved_cut())
    try:
        result = authorize_formal_execution(
            admission_report=report, evaluation_store=store,
            preregistration_path=tmp_path / "formal.json",
            repository_root=ROOT, execution_root=report.evaluation_root,
            execution_workspace=report.evaluation_root)
        result.validate()
        assert result.replay_state == "NEW_EXECUTION"
        assert result.evaluation_run_id == report.campaign.run_id

        _issue_formal_root(store, runner, report)
        recovered = authorize_formal_execution(
            admission_report=report, evaluation_store=store,
            preregistration_path=tmp_path / "formal.json",
            repository_root=ROOT, execution_root=report.evaluation_root,
            execution_workspace=report.evaluation_root)
        assert recovered.replay_state == "EXACT_RECOVERY_CONTINUATION"
        assert recovered.authorization_commitment != result.authorization_commitment
        _validate(runner, report)

        unresolved = RequiredFact(
            "actual external fact", ReadinessFactState.UNRESOLVED, None,
            "fresh authority", "not executed")
        monkeypatch.setattr(subject, "resolve_formal_required_facts",
            lambda *args, **kwargs: FormalRequiredFactsResolution(
                RequiredFactsMaterialization.create({
                    "/evaluation_plan/production_build": unresolved}),
                (unresolved,)))
        with pytest.raises(ValueError, match="unresolved required fact"):
            authorize_formal_execution(
                admission_report=report, evaluation_store=store,
                preregistration_path=tmp_path / "formal.json",
                repository_root=ROOT, execution_root=report.evaluation_root,
                execution_workspace=report.evaluation_root)
    finally:
        store.close()


def test_evidence_index_is_typed_secret_free_and_exact_execution_scoped(
    tmp_path, monkeypatch,
):
    report, store, executions = _admitted_store(tmp_path, monkeypatch)
    try:
        store.append(LedgerRecord.create(
            "observation", [executions[0].record_id, "diagnostic"], {
                "kind": "INCIDENT_OBSERVATION", "credential": SECRET,
                "evaluator_payload": {"ground_truth": GT},
                "input_identity": "safe-public-reference",
            }, executions[0].record_id))
        index = build_evidence_index(
            store.reader().read_run_snapshot(report.campaign.run_id))
        assert len([item for item in index.entries
                    if item.domain == "execution"]) == 30
        assert {item.category for item in index.entries} >= {
            EvidenceCategory.PREREGISTRATION.value,
            EvidenceCategory.INPUT.value,
            EvidenceCategory.INCIDENT_OBSERVATION.value,
        }
        serialized = repr(index)
        assert SECRET not in serialized and GT not in serialized
        assert all(item.record_id.startswith("eval:") for item in index.entries)
    finally:
        store.close()


def test_evidence_index_commitment_append_replay_conflict_and_recovery(
    tmp_path, monkeypatch,
):
    report, store, executions = _admitted_store(tmp_path, monkeypatch)
    try:
        index = build_evidence_index(
            store.reader().read_run_snapshot(report.campaign.run_id))
        first = append_evidence_index_commitment(store, index)
        replay = append_evidence_index_commitment(store, index)
        assert replay == first
        assert evidence_index_commitment_is_current(store.reader(), first.record_id)

        conflicting = LedgerRecord.create(
            "report", [report.campaign.run_id, "EVIDENCE_INDEX",
                       index.source_cut_commitment], {
                "kind": "EVIDENCE_INDEX_COMMITMENT", "contradiction": True,
            }, report.campaign.run_id)
        assert conflicting.record_id == first.record_id
        with pytest.raises(LedgerConflict):
            store.append(conflicting)

        # Crash model: a source append happened after the old index receipt.
        store.append(LedgerRecord.create(
            "observation", [executions[0].record_id, "late-source"],
            {"kind": "READ_INTEGRITY_FAILURE"}, executions[0].record_id))
        assert not evidence_index_commitment_is_current(
            store.reader(), first.record_id)
        current = build_evidence_index(
            store.reader().read_run_snapshot(report.campaign.run_id))
        second = append_evidence_index_commitment(store, current)
        assert second.record_id != first.record_id
        assert append_evidence_index_commitment(store, current) == second
    finally:
        store.close()


def test_manifest_freshness_detects_source_oracle_fixture_and_profile_drift(
    tmp_path, monkeypatch,
):
    report, store, _ = _admitted_store(tmp_path, monkeypatch)
    try:
        resolver = _CurrentResolver(_current(report))
        current = verify_manifest_freshness(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=resolver)
        assert current.current
        assert resolver.calls == 1
        mutations = (
            replace(report.manifest, repository_commit="f" * 40),
            replace(report.manifest,
                    oracle_content_commitment="sha256:" + "f" * 64),
            replace(report.manifest, fixture_revision="fixture-drift"),
            replace(report.manifest,
                    provider_profile_references=("profile-drift",)),
            replace(report.manifest,
                    rag_profile_references=("rag-profile-drift",)),
            replace(report.manifest, environment_identity="topology-drift"),
        )
        for stale in mutations:
            result = verify_manifest_freshness(
                store.reader(), report.campaign.run_id, stale,
                current_state_resolver=_CurrentResolver(
                    _current(report)))
            assert not result.current
            assert result.blockers
    finally:
        store.close()


def test_reproducibility_closure_manifest_is_deterministic_and_secret_free(
    tmp_path, monkeypatch,
):
    report, store, executions = _admitted_store(tmp_path, monkeypatch)
    try:
        store.append(LedgerRecord.create(
            "observation", [executions[0].record_id, "secret-diagnostic"], {
                "kind": "PROVIDER_DIAGNOSTIC", "credential": SECRET,
                "ground_truth": GT,
            }, executions[0].record_id))
        first = build_reproducibility_closure_manifest(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        second = build_reproducibility_closure_manifest(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        assert first == second
        first.validate()
        assert SECRET not in repr(first) and GT not in repr(first)
        assert first.evidence_index_commitment.startswith("sha256:")
        assert first.reviewer_protocol_commitment == commitment(
            report.manifest.reviewer_protocol)
    finally:
        store.close()


def test_final_projection_reconstructs_all_slots_without_fabricating_results(
    tmp_path, monkeypatch,
):
    report, store, _ = _admitted_store(tmp_path, monkeypatch)
    try:
        first = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        second = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        assert first == second
        first.validate()
        assert first.nominal_slot_count == first.reconstructed_slot_count == 30
        assert first.disposition_counts == (("NOT_EXECUTED", 30),)
        assert first.engineering_capability_ready
        assert not first.actual_external_facts_ready
        assert first.closure_stage is ClosureStage.ENGINEERING_COMPLETE
        assert all(item.state is GateEvidenceState.NOT_EXECUTED
                   for item in first.gates)

        formal = build_formal_report(store.reader(), report.campaign.run_id)
        assert formal.root_cause_metric.missing_count == 30
        assert formal.unsupported_claim_metric.missing_count == 30
        assert formal.conclusion_calibration_metric.missing_count == 30
        assert not formal.readiness.evaluation_executed
        assert not formal.readiness.product_acceptance_complete
    finally:
        store.close()


def test_gate_lineage_version_run_replay_serialization_and_conflict(
    tmp_path, monkeypatch,
):
    report, store, _ = _admitted_store(tmp_path, monkeypatch)
    try:
        first = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        replay = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        assert replay.gates == first.gates
        assert replay.projection_commitment == first.projection_commitment
        for gate in first.gates:
            gate.validate()
            assert gate.gate_contract_version == "SPEC-017-R4-GATE-EVIDENCE-v1"
            assert gate.evaluation_run_id == report.campaign.run_id
            encoded = json.dumps({
                "gate": gate.gate,
                "gate_contract_version": gate.gate_contract_version,
                "evaluation_run_id": gate.evaluation_run_id,
                "state": gate.state.value,
                "source_record_ids": list(gate.source_record_ids),
                    "source_record_commitments": [list(item) for item in
                                                   gate.source_record_commitments],
                    "durable_receipt_id": gate.durable_receipt_id,
                    "durable_receipt_commitment":
                        gate.durable_receipt_commitment,
                "source_commitment": gate.source_commitment,
            }, sort_keys=True)
            decoded = json.loads(encoded)
            assert decoded["gate_contract_version"] == gate.gate_contract_version
            assert decoded["evaluation_run_id"] == gate.evaluation_run_id

        target = first.gates[0]
        with pytest.raises(ValueError, match="lineage"):
            replace(target, gate_contract_version="different-version").validate()
        with pytest.raises(ValueError, match="lineage"):
            replace(target, evaluation_run_id=(
                "eval:evaluation_run:" + "f" * 64)).validate()
        with pytest.raises(ValueError, match="another evaluation run"):
            replace(
                first,
                gates=(replace(
                    target,
                    evaluation_run_id="eval:evaluation_run:" + "f" * 64,
                    source_commitment=commitment({
                        "gate": target.gate,
                        "gate_contract_version": target.gate_contract_version,
                        "evaluation_run_id": "eval:evaluation_run:" + "f" * 64,
                        "state": target.state.value,
                        "source_record_ids": list(target.source_record_ids),
                        "source_record_commitments": [list(item) for item in
                            target.source_record_commitments],
                        "durable_receipt_id": target.durable_receipt_id,
                        "durable_receipt_commitment":
                            target.durable_receipt_commitment,
                    })), *first.gates[1:]),
            ).validate()
    finally:
        store.close()


def test_durable_gate_receipt_requires_authoritative_sources_and_replays(
    tmp_path, monkeypatch,
):
    report, store, executions = _admitted_store(tmp_path, monkeypatch)
    try:
        for execution in executions:
            store.append(LedgerRecord.create(
                "observation", [execution.record_id, "signal"],
                {"kind": "SIGNAL_STARTED"}, execution.record_id))
            store.append(LedgerRecord.create(
                "observation", [execution.record_id, "event-binding"],
                {"kind": "AUTHORITATIVE_EVENT_BINDING"}, execution.record_id))

        first = append_durable_gate_receipt(
            store, report.campaign.run_id, "SCENARIO_INJECTION")
        replay = append_durable_gate_receipt(
            store, report.campaign.run_id, "SCENARIO_INJECTION")
        assert first == replay
        assert first.state is GateEvidenceState.PASS
        assert first.gate_contract_version == "SPEC-017-R4-GATE-EVIDENCE-v1"
        assert first.evaluation_run_id == report.campaign.run_id
        assert len(first.source_record_ids) == 60
        assert first.durable_receipt_id is not None

        durable = store.get(first.durable_receipt_id)
        payload = json.loads(durable.semantic_json)
        assert payload["gate_contract_version"] == first.gate_contract_version
        assert payload["evaluation_run_id"] == report.campaign.run_id
        assert len(payload["authoritative_sources"]) == 60
        assert len(payload["execution_ids"]) == 30
        indexed = build_evidence_index(
            store.reader().read_run_snapshot(report.campaign.run_id))
        assert any(item.record_id == first.durable_receipt_id
                   and item.category == EvidenceCategory.GATE.value
                   for item in indexed.entries)

        projection = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        projected = next(item for item in projection.gates
                         if item.gate == "SCENARIO_INJECTION")
        assert projected == first

        payload["gate_contract_version"] = "different-version"
        conflict = LedgerRecord.create(
            "report", [report.campaign.run_id, "FORMAL_GATE_RECEIPT",
                       "SCENARIO_INJECTION"], payload, report.campaign.run_id)
        with pytest.raises(LedgerConflict):
            store.append(conflict)
    finally:
        store.close()


def test_source_less_pass_cannot_become_gate_authority_even_if_recommitted():
    material = {
        "gate": "REAL_LLM",
        "gate_contract_version": "SPEC-017-R4-GATE-EVIDENCE-v1",
        "evaluation_run_id": "eval:evaluation_run:" + "a" * 64,
        "state": "PASS",
        "source_record_ids": [],
        "source_record_commitments": [],
        "durable_receipt_id": None,
        "durable_receipt_commitment": None,
    }
    fabricated = FormalGateEvidence(
        material["gate"], material["gate_contract_version"],
        material["evaluation_run_id"], GateEvidenceState.PASS, (), (), None,
        None, commitment(material))
    with pytest.raises(ValueError, match="lineage"):
        fabricated.validate()


def test_source_less_durable_pass_is_rejected_by_supported_projection(
    tmp_path, monkeypatch,
):
    report, store, _ = _admitted_store(tmp_path, monkeypatch)
    try:
        run_id = report.campaign.run_id
        sources = []
        payload = {
            "kind": "FORMAL_GATE_RECEIPT",
            "schema": "SPEC-017-R4-FORMAL-EVIDENCE-v1",
            "gate": "REAL_LLM",
            "gate_contract_version": "SPEC-017-R4-GATE-EVIDENCE-v1",
            "evaluation_run_id": run_id,
            "execution_ids": [],
            "authoritative_sources": sources,
            "state": "PASS",
            "operation_identity": commitment({
                "evaluation_run_id": run_id, "gate": "REAL_LLM",
                "gate_contract_version": "SPEC-017-R4-GATE-EVIDENCE-v1",
            }),
            "lineage_commitment": commitment(sources),
        }
        store.append(LedgerRecord.create(
            "report", [run_id, "FORMAL_GATE_RECEIPT", "REAL_LLM"],
            payload, run_id))
        with pytest.raises(ValueError, match="receipt lineage|source lineage"):
            build_final_readiness_projection(
                store.reader(), run_id, report.manifest,
                current_state_resolver=_CurrentResolver(
                    _current(report)))
    finally:
        store.close()


def test_manifest_current_authorities_are_required_and_each_drift_blocks(
    tmp_path, monkeypatch,
):
    report, store, _ = _admitted_store(tmp_path, monkeypatch)
    try:
        unavailable = verify_manifest_freshness(
            store.reader(), report.campaign.run_id, report.manifest)
        assert not unavailable.current
        assert "CURRENT_STATE_AUTHORITY_UNAVAILABLE" in unavailable.blockers

        exact = verify_manifest_freshness(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        assert exact.current
        durable_admission = json.loads(store.get(
            report.campaign.run_id).semantic_json)["formal_admission"]
        assert durable_admission["expected_topology_commitment"] == (
            report.expected_topology_commitment)
        assert durable_admission["expected_interruption_matrix_identity"] == (
            report.expected_interruption_matrix_identity)

        drifts = (
            ({"repository_commit": "f" * 40},
             "CURRENT_REPOSITORY_REVISION_DRIFT"),
            ({"raw_template_commitment": "sha256:" + "c" * 64},
             "CURRENT_RAW_TEMPLATE_DRIFT"),
            ({"evaluation_config_commitment": "sha256:" + "f" * 64},
             "CURRENT_CONFIG_DRIFT"),
            ({"environment_identity": "topology-drift"},
             "CURRENT_DOCKER_TOPOLOGY_DRIFT"),
            ({"provider_profile_references": ("llm-drift",)},
             "CURRENT_LLM_PROFILE_DRIFT"),
            ({"rag_profile_references": ("rag-drift",)},
             "CURRENT_RAG_PROFILE_DRIFT"),
            ({"oracle_revision_id": "eval:oracle_revision:" + "f" * 64},
             "CURRENT_ORACLE_REVISION_DRIFT"),
            ({"fixture_revision": "fixture-drift"},
             "CURRENT_FIXTURE_DRIFT"),
            ({"topology_commitment": "sha256:" + "d" * 64},
             "CURRENT_DOCKER_TOPOLOGY_DRIFT"),
            ({"interruption_matrix_identity": "sha256:" + "e" * 64},
             "CURRENT_INTERRUPTION_MATRIX_DRIFT"),
            ({"topology_commitment": "not-a-topology-commitment"},
             "CURRENT_TOPOLOGY_COMMITMENT_MALFORMED"),
            ({"interruption_matrix_identity": "not-an-interruption-identity"},
             "CURRENT_INTERRUPTION_MATRIX_IDENTITY_MALFORMED"),
        )
        for changes, expected_blocker in drifts:
            resolver = _CurrentResolver(_current(report, **changes))
            result = verify_manifest_freshness(
                store.reader(), report.campaign.run_id, report.manifest,
                current_state_resolver=resolver)
            assert resolver.calls == 1
            assert not result.current
            assert expected_blocker in result.blockers

        topology_drift = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(
                report, topology_commitment="sha256:" + "d" * 64)))
        assert not topology_drift.engineering_capability_ready
        assert topology_drift.closure_stage is None
        assert "CURRENT_DOCKER_TOPOLOGY_DRIFT" in topology_drift.blockers

        interruption_drift = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(
                report, interruption_matrix_identity="sha256:" + "e" * 64)))
        assert not interruption_drift.engineering_capability_ready
        assert interruption_drift.closure_stage is None
        assert "CURRENT_INTERRUPTION_MATRIX_DRIFT" in (
            interruption_drift.blockers)

        for field in (
            "expected_topology_commitment",
            "expected_interruption_matrix_identity",
            "materialized_config_commitment",
        ):
            missing = replace(report, **{field: None})
            with pytest.raises(ValueError, match="not durably frozen"):
                missing.validate()
    finally:
        store.close()


def test_failed_censored_invalid_are_retained_without_replacement_or_double_count(
    tmp_path, monkeypatch,
):
    report, store, executions = _admitted_store(tmp_path, monkeypatch)
    dispositions = (
        "OBSERVED_COMPLETION", "TERMINAL_PRODUCTION_FAILURE",
        "RIGHT_CENSORED_UNFINISHED", "INVALID_EVALUATION",
    )
    try:
        for position, execution in enumerate(executions):
            value = dispositions[position % len(dispositions)]
            store.append(LedgerRecord.create(
                "observation", [execution.record_id, "final"], {
                    "disposition": value,
                    "metric_disposition": ("PASS" if value ==
                        "OBSERVED_COMPLETION" else "INVALID_EVALUATION"),
                    "knowledge": "MATCH", "failure_case": "NONE",
                    "diagnostic_stage": "DONE",
                }, execution.record_id))
        projection = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, report.manifest,
            current_state_resolver=_CurrentResolver(_current(report)))
        counts = dict(projection.disposition_counts)
        assert sum(counts.values()) == 30
        assert counts["TERMINAL_PRODUCTION_FAILURE"] > 0
        assert counts["RIGHT_CENSORED_UNFINISHED"] > 0
        assert counts["INVALID_EVALUATION"] > 0
        assert projection.reconstructed_slot_count == 30
        assert all(item.replacement_for_execution_id is None
                   for item in build_formal_report(
                       store.reader(), report.campaign.run_id).executions)
    finally:
        store.close()


def test_incomplete_or_stale_evidence_cannot_jump_closure_stage(
    tmp_path, monkeypatch,
):
    report, store, _ = _admitted_store(
        tmp_path, monkeypatch, execution_count=29)
    try:
        stale = replace(report.manifest, fixture_revision="stale")
        projection = build_final_readiness_projection(
            store.reader(), report.campaign.run_id, stale,
            current_state_resolver=_CurrentResolver(_current(report)))
        assert projection.reconstructed_slot_count == 29
        assert "NOMINAL_30_SLOT_RECONSTRUCTION_INCOMPLETE" in projection.blockers
        assert not projection.engineering_capability_ready
        assert projection.closure_stage is None
        assert not projection.actual_external_facts_ready
        assert all(item.state is not GateEvidenceState.PASS
                   for item in projection.gates)
    finally:
        store.close()
