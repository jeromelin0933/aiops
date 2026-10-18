from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

from rca_evaluation.authority import load_trusted_oracle_approvals
from rca_evaluation import formal_admission as subject
from rca_evaluation.formal_admission import (
    FORMAL_ORDER, FORMAL_POLLING_MS, FORMAL_REPETITIONS, FORMAL_SEED,
    FORMAL_TIMEOUT_MS, RepositoryState, ReviewerProtocol,
    admit_formal_campaign, final_admit_formal_campaign, load_reviewer_protocol,
)
from rca_evaluation.identity import commitment
from rca_evaluation.ledger import LedgerConflict, LedgerRecord
from rca_evaluation.oracle import load_official_oracle
from rca_evaluation.reporting import append_report_commitment, build_formal_report
from rca_evaluation.readiness import (
    FormalRequiredFactsResolution, ReadinessFactState, RequiredFact,
    RequiredFactsMaterialization,
)
from rca_evaluation.preparation import prepare_formal_campaign
from rca_evaluation.runner import CampaignRunner
from rca_evaluation.sqlite_store import SqliteEvaluationStore


ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG = ROOT / "configs/rca_evaluation/formal_preregistration_v1.json"
TRUST = ROOT / "configs/rca_evaluation/trust/spec017_oracle_approval_v1.json"
HEAD = "e9cee50ce7904fb822e8032bbe8b05a4a826cf07"
BRANCH = "feature/spec-017-formal-evaluation-readiness"
SHA = "sha256:" + "a" * 64


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _config(tmp_path: Path, **control_overrides: object) -> Path:
    raw = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    raw["evaluation_plan"]["production_build"] = HEAD
    raw["evaluation_plan"]["corpus_index"] = "knowledge-build:test-pinned"
    inputs = raw["manifest_inputs"]
    inputs["environment_identity"] = "pytest:python-pinned"
    extension = inputs["approved_spec_commitments"]
    extension["knowledge_document_revisions"] = ["test-doc@1"]
    extension["knowledge_build_identity"] = "knowledge-build:test-pinned"
    extension["docker_image_digest"] = SHA
    extension["docker_namespace_identity"] = "spec017-test-isolated"
    extension["tool_versions"] = {"python": "3.test", "docker": "test"}
    raw["campaign_controls"].update(control_overrides)
    return _write_json(tmp_path / "formal.json", raw)


def _reviewers(tmp_path: Path, *, stage1=("human-a", "human-b"),
               stage1_tiebreak="human-c",
               stage2=("human-d", "human-e"), tiebreak="human-f",
               stage1_official=False) -> Path:
    return _write_json(tmp_path / "reviewers.json", {
        "schema": "spec017.reviewer-roster.v1",
        "stage1": {"reviewer_ids": list(stage1),
                   "reviewer_kind": "HUMAN",
                   "blind_diagnostic_only": True,
                   "official_verdict_allowed": stage1_official,
                   "disagreement": "THIRD_HUMAN_TIEBREAK",
                   "tiebreak_reviewer_id": stage1_tiebreak,
                   "tiebreak_reviewer_kind": "HUMAN"},
        "stage2": {"reviewer_ids": list(stage2), "reviewer_kind": "HUMAN",
                   "independent": True,
                   "disagreement": "THIRD_HUMAN_TIEBREAK",
                   "tiebreak_reviewer_id": tiebreak,
                   "tiebreak_reviewer_kind": "HUMAN"},
        "machine_hard_failure_override_allowed": False,
        "records_append_only": True,
    })


def _admit(tmp_path: Path, **overrides: object):
    root = tmp_path / "rca_evaluation"
    values = dict(
        repository_root=ROOT,
        trusted_approval_path=TRUST,
        campaign_key="formal-campaign-2026-10", database_path=root / "evaluation.db",
        evaluation_root=root,
        repository_state=RepositoryState(BRANCH, HEAD, False),
    )
    if "preregistration_path" not in overrides:
        values["preregistration_path"] = _config(tmp_path)
    if "reviewer_roster_path" not in overrides:
        values["reviewer_roster_path"] = _reviewers(tmp_path)
    values.update(overrides)
    return admit_formal_campaign(**values)


class _Unused:
    def __getattr__(self, name):
        raise AssertionError(f"formal validation must not call {name}")


def _formal_runner(report, monkeypatch, *, required_facts_resolver=None):
    monkeypatch.setattr(
        subject, "inspect_repository",
        lambda root: RepositoryState(BRANCH, HEAD, False))
    config = Path(report.evaluation_root).parent / "formal.json"
    reviewers = Path(report.evaluation_root).parent / "reviewers.json"
    prepared = prepare_formal_campaign(
        repository_root=ROOT, preregistration_path=config,
        campaign_key=report.campaign.campaign_key,
        database_path=report.database_path, evaluation_root=report.evaluation_root,
        repository_state=RepositoryState(BRANCH, HEAD, False))
    store = SqliteEvaluationStore(
        report.database_path, approvals=load_trusted_oracle_approvals(TRUST))
    fact = RequiredFact("test readiness", ReadinessFactState.RESOLVED,
                        "verified", "governed test authority", "fresh-read")
    resolution = FormalRequiredFactsResolution(
        RequiredFactsMaterialization.create({
            "/evaluation_plan/production_build": fact}), (fact,))
    monkeypatch.setattr(
        subject, "resolve_formal_required_facts",
        required_facts_resolver or (lambda *args, **kwargs: resolution))
    report = final_admit_formal_campaign(
        preparation_id=prepared.preparation_id, evaluation_store=store,
        repository_root=ROOT, preregistration_path=config,
        trusted_approval_path=TRUST, reviewer_roster_path=reviewers,
        campaign_key=report.campaign.campaign_key,
        database_path=report.database_path, evaluation_root=report.evaluation_root)
    runner = CampaignRunner(
        ledger=store, clock=_Unused(), observer=_Unused(), injector=_Unused(),
        grader=lambda _: True)
    return report, store, runner


def _validate(runner, report, **overrides):
    values = {
        "admission_report": report,
        "admitted_plan": report.plan,
        "campaign": report.campaign,
        "manifest": report.manifest,
        "execution_root": report.evaluation_root,
        "execution_workspace": report.evaluation_root,
        "repository_root": report.repository_root,
    }
    values.update(overrides)
    return runner.validate_formal_execution(**values)


def _issue_formal_root(store, runner, report):
    """Non-executing harness: validate and issue only the authoritative run root."""
    _validate(runner, report)
    store.admit_oracle(report.oracle_revision)
    store.admit_plan(report.plan)
    record = runner._create_run_record(
        admitted_plan=report.plan, campaign=report.campaign,
        manifest=report.manifest, execution_mode="FORMAL",
        formal_admission_commitment=report.admission_commitment,
        formal_admission_payload=report.admission_payload(),
    )
    return store._append_formal_run(record)


def test_exact_formal_controls_order_denominator_and_manifest_are_sealed(tmp_path):
    report = _admit(tmp_path)
    campaign = report.campaign
    assert campaign.repetitions == FORMAL_REPETITIONS == 5
    assert len(campaign.executions) == 30
    assert [(item.scenario_id, item.repetition) for item in campaign.executions] == [
        (f"S{scenario}", repetition)
        for repetition in range(1, 6) for scenario in range(1, 7)
    ]
    assert FORMAL_ORDER == "REPETITION_MAJOR"
    assert campaign.admission.random_seed == report.manifest.random_seed == FORMAL_SEED == 17017
    assert campaign.admission.polling_interval_ms == report.manifest.polling_interval_ms == FORMAL_POLLING_MS == 1_000
    assert campaign.admission.timeout_ms == report.manifest.timeout_ms == FORMAL_TIMEOUT_MS == 300_000
    assert all(not item.is_replacement for item in campaign.executions)
    with pytest.raises(ValueError, match="does not authorize replacement"):
        campaign.replacement_for(campaign.executions[0].execution_id)
    assert report.denominator["nominal"].startswith("30 pre-registered slots")
    assert len(set(report.denominator.values())) == 5
    assert report.manifest.evaluation_config_commitment == report.materialized_config_commitment
    assert report.preregistration_commitment == commitment(
        json.loads((tmp_path / "formal.json").read_text(encoding="utf-8")))
    assert report.manifest.campaign_key == "formal-campaign-2026-10"
    assert report.manifest.execution_order_strategy == FORMAL_ORDER
    assert report.manifest.early_stop_allowed is False
    assert report.manifest.replacement_policy == "NONE"
    assert report.manifest.reviewer_protocol == report.reviewer_protocol.sealed_payload
    report.manifest.validate()


def test_campaign_key_required_frozen_and_identities_are_deterministic(tmp_path):
    first = _admit(tmp_path)
    second = _admit(tmp_path / "second")
    assert first.campaign.run_id == second.campaign.run_id
    assert [item.execution_id for item in first.campaign.executions] == [
        item.execution_id for item in second.campaign.executions]
    with pytest.raises(FrozenInstanceError):
        first.campaign.campaign_key = "changed"
    with pytest.raises(ValueError, match="campaign_key"):
        _admit(tmp_path / "empty", campaign_key="")
    changed = _admit(tmp_path / "changed", campaign_key="another-explicit-key")
    assert changed.campaign.run_id != first.campaign.run_id
    assert changed.admission_commitment != first.admission_commitment


@pytest.mark.parametrize("field,value", [
    ("repetitions", 4), ("order", "SCENARIO_MAJOR"),
    ("bootstrap_seed", 1), ("polling_interval_seconds", 2),
    ("per_execution_timeout_seconds", 299), ("early_stop", True),
    ("replacement", "RETRY"),
])
def test_contradictory_preregistration_replay_fails_closed(tmp_path, field, value):
    with pytest.raises(ValueError, match="frozen delegation"):
        _admit(tmp_path, preregistration_path=_config(tmp_path, **{field: value}))


def test_missing_preregistration_is_rejected(tmp_path):
    with pytest.raises(OSError):
        _admit(tmp_path, preregistration_path=tmp_path / "missing.json")


@pytest.mark.parametrize("state", [
    RepositoryState("wrong-branch", HEAD, False),
    RepositoryState(BRANCH, HEAD, True),
    RepositoryState(BRANCH, "not-a-commit", False),
])
def test_repository_authority_or_dirty_state_is_rejected(tmp_path, state):
    with pytest.raises(ValueError, match="repository"):
        _admit(tmp_path, repository_state=state)


def test_fresh_database_workspace_collision_is_rejected(tmp_path):
    evaluation_root = tmp_path / "rca_evaluation"
    evaluation_root.mkdir()
    with pytest.raises(ValueError, match="fresh DB/workspace"):
        _admit(tmp_path, evaluation_root=evaluation_root,
               database_path=evaluation_root / "evaluation.db")


def test_trusted_oracle_approval_missing_invalid_or_not_independent_is_rejected(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(OSError):
        _admit(tmp_path, trusted_approval_path=missing)
    invalid = _write_json(tmp_path / "invalid.json", {
        "schema": "spec017.trusted-oracle-approval.v1", "approvals": []})
    with pytest.raises(ValueError, match="must not be empty"):
        _admit(tmp_path / "invalid-case", trusted_approval_path=invalid)
    config = _config(tmp_path / "same")
    with pytest.raises(ValueError, match="independent files"):
        _admit(tmp_path / "same", preregistration_path=config,
               trusted_approval_path=config)


def test_reviewer_identities_protocol_and_official_verdict_are_fail_closed(tmp_path):
    with pytest.raises(OSError):
        _admit(tmp_path, reviewer_roster_path=tmp_path / "missing-reviewers.json")
    duplicate = _reviewers(tmp_path / "dup", stage1=("same", "same"),
                           stage2=("same", "other"))
    with pytest.raises(ValueError, match="distinct"):
        load_reviewer_protocol(duplicate)
    official = _reviewers(tmp_path / "official", stage1_official=True)
    with pytest.raises(ValueError, match="frozen formal protocol"):
        load_reviewer_protocol(official)
    protocol = load_reviewer_protocol(_reviewers(tmp_path / "valid"))
    assert len(protocol.stage1_reviewer_ids) == 2
    assert protocol.stage1_tiebreak_reviewer_id not in protocol.stage1_reviewer_ids
    assert len(protocol.stage2_reviewer_ids) == 2
    assert protocol.tiebreak_reviewer_id not in protocol.stage2_reviewer_ids
    assert protocol.stage1_blind_diagnostic_only
    assert not protocol.stage1_official_verdict_allowed
    assert not protocol.machine_hard_failure_override_allowed
    assert protocol.records_append_only


def test_reviewer_identity_changes_change_admission_lineage_commitment(tmp_path):
    first = _admit(tmp_path / "one")
    other_roster = _reviewers(
        tmp_path / "two", stage1=("human-x", "human-y"),
        stage1_tiebreak="human-z", stage2=("human-u", "human-v"),
        tiebreak="human-t")
    second = _admit(tmp_path / "three", reviewer_roster_path=other_roster)
    assert first.manifest.manifest_commitment != second.manifest.manifest_commitment
    assert first.manifest.reviewer_protocol != second.manifest.reviewer_protocol
    assert first.admission_commitment != second.admission_commitment


def test_same_campaign_identity_with_changed_admission_fails_durable_replay(tmp_path):
    first = _admit(tmp_path / "one")
    other = _reviewers(
        tmp_path / "other", stage1=("human-x", "human-y"),
        stage1_tiebreak="human-z", stage2=("human-u", "human-v"),
        tiebreak="human-t")
    changed = _admit(tmp_path / "two", reviewer_roster_path=other)
    assert first.campaign.run_id == changed.campaign.run_id
    oracle = load_official_oracle(
        ROOT / "configs/rca_evaluation/oracle/spec017_s1_s6_v1.json").revision
    with SqliteEvaluationStore(
            tmp_path / "store" / "rca_evaluation" / "evaluation.db",
            approvals=load_trusted_oracle_approvals(TRUST)) as store:
        store.admit_oracle(oracle)
        store.admit_plan(first.plan)
        key = [first.plan.evaluation_plan_id, first.campaign.campaign_key]
        store.append(LedgerRecord.create(
            "evaluation_run", key,
            {"oracle_revision_id": first.plan.oracle_revision_id,
             "admission_commitment": first.admission_commitment},
            first.plan.evaluation_plan_id))
        with pytest.raises(LedgerConflict, match="contradictory replay"):
            store.append(LedgerRecord.create(
                "evaluation_run", key,
                {"oracle_revision_id": changed.plan.oracle_revision_id,
                 "admission_commitment": changed.admission_commitment},
                changed.plan.evaluation_plan_id))


def test_repository_template_refuses_unfrozen_execution_inputs(tmp_path):
    root = tmp_path / "rca_evaluation"
    with pytest.raises(ValueError, match="must be frozen"):
        admit_formal_campaign(
            repository_root=ROOT, preregistration_path=BASE_CONFIG,
            trusted_approval_path=TRUST,
            reviewer_roster_path=_reviewers(tmp_path), campaign_key="campaign",
            database_path=root / "evaluation.db", evaluation_root=root,
            repository_state=RepositoryState(BRANCH, HEAD, False),
        )


def test_caller_materialization_cannot_admit_formal_campaign(tmp_path):
    root = tmp_path / "rca_evaluation"
    minted = RequiredFactsMaterialization.create({
        "/evaluation_plan/production_build": RequiredFact(
            "production_build", ReadinessFactState.RESOLVED, HEAD,
            "caller", "caller-minted resolved value")})
    with pytest.raises(ValueError, match="not Formal admission authority"):
        admit_formal_campaign(
            repository_root=ROOT, preregistration_path=BASE_CONFIG,
            trusted_approval_path=TRUST,
            reviewer_roster_path=_reviewers(tmp_path), campaign_key="campaign",
            database_path=root / "evaluation.db", evaluation_root=root,
            repository_state=RepositoryState(BRANCH, HEAD, False),
            readiness_materialization=minted,
        )


def test_admission_only_cli_with_public_template_remains_fail_closed(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(subject, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    evaluation_root = tmp_path / "fresh" / "rca_evaluation"
    code = subject.main([
        "--admission-only", "--preparation-id", "preparation:" + "a" * 64,
        "--repository-root", str(ROOT),
        "--config", str(BASE_CONFIG),
        "--trusted-oracle-approval", str(TRUST),
        "--reviewer-roster", str(_reviewers(tmp_path)),
        "--campaign-key", "formal-campaign-2026-10",
        "--database", str(evaluation_root / "evaluation.db"),
        "--evaluation-root", str(evaluation_root),
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 2 and output["admitted"] is False
    assert output["execution_performed"] is False
    assert output["side_effects"] == "NONE"
    assert not evaluation_root.exists()


def test_in_memory_preflight_never_masquerades_as_durable_admission(tmp_path):
    report = _admit(tmp_path)
    payload = report.public_payload()
    assert payload["mode"] == "PREFLIGHT_ONLY"
    assert payload["preflight_passed"] is True
    assert payload["admitted"] is False
    assert report.preparation_id is None


def test_admission_only_cli_does_not_implicitly_prepare(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subject, "inspect_repository",
                        lambda root: RepositoryState(BRANCH, HEAD, False))
    evaluation_root = tmp_path / "fresh" / "rca_evaluation"
    code = subject.main([
        "--admission-only", "--preparation-id", "preparation:" + "a" * 64,
        "--repository-root", str(ROOT),
        "--config", str(_config(tmp_path)),
        "--trusted-oracle-approval", str(TRUST),
        "--reviewer-roster", str(_reviewers(tmp_path)),
        "--campaign-key", "formal-campaign-2026-10",
        "--database", str(evaluation_root / "evaluation.db"),
        "--evaluation-root", str(evaluation_root),
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 2 and output["admitted"] is False
    assert output["execution_performed"] is False and output["side_effects"] == "NONE"
    assert not evaluation_root.exists()


def test_public_report_preserves_ground_truth_and_secret_isolation(tmp_path):
    payload = json.dumps(_admit(tmp_path).public_payload(), sort_keys=True).lower()
    for forbidden in ("semantics", "ground_truth", "oracleanswer", "gemini_api_key",
                      "credential", "password", "secret"):
        assert forbidden not in payload


def test_direct_protocol_requires_two_independent_reviewers_and_tiebreak():
    with pytest.raises(ValueError):
        ReviewerProtocol(("s1",), "s1-tie", ("s2", "s3"), "s4")
    with pytest.raises(ValueError):
        ReviewerProtocol(
            ("s1", "s2"), "s1-tie", ("s3", "s4"), "s5",
            stage2_independent=False)
    with pytest.raises(ValueError, match="distinct"):
        ReviewerProtocol(("s1", "s2"), "s1", ("s3", "s4"), "s5")


def test_formal_execution_requires_report_and_exact_governance_objects(tmp_path, monkeypatch):
    report = _admit(tmp_path)
    report, store, runner = _formal_runner(report, monkeypatch)
    try:
        _validate(runner, report)
        assert not store.is_pristine_for_formal_admission()
        assert store.has_no_formal_execution_facts()
        with pytest.raises(TypeError, match="FormalAdmissionReport"):
            _validate(runner, report, admission_report=None)
        with pytest.raises(ValueError, match="plan/run/campaign/manifest"):
            _validate(runner, report, admitted_plan=replace(
                report.plan, repetitions=4))
        with pytest.raises(ValueError, match="plan/run/campaign/manifest"):
            _validate(runner, report, campaign=replace(
                report.campaign, campaign_key="other"))
        with pytest.raises(ValueError, match="plan/run/campaign/manifest"):
            _validate(runner, report, manifest=replace(
                report.manifest, campaign_key="other"))
    finally:
        store.close()


def test_formal_execution_binds_root_database_workspace_and_freshness(tmp_path, monkeypatch):
    report = _admit(tmp_path)
    report, store, runner = _formal_runner(report, monkeypatch)
    try:
        with pytest.raises(ValueError, match="evaluation root"):
            _validate(runner, report, execution_root=tmp_path / "wrong")
        with pytest.raises(ValueError, match="workspace"):
            _validate(runner, report, execution_workspace=tmp_path / "wrong")
        with pytest.raises(ValueError, match="repository root"):
            _validate(runner, report, repository_root=tmp_path)
        monkeypatch.setattr(
            subject, "inspect_repository",
            lambda root: RepositoryState(BRANCH, HEAD, True))
        with pytest.raises(ValueError, match="baseline.*pristine"):
            _validate(runner, report)
        assert not store.is_pristine_for_formal_admission()
        assert store.has_no_formal_execution_facts()
        monkeypatch.setattr(
            subject, "inspect_repository",
            lambda root: RepositoryState(BRANCH, HEAD, False))
        (Path(report.evaluation_root) / "collision.txt").write_text("collision")
        with pytest.raises(ValueError, match="collision"):
            _validate(runner, report)
    finally:
        store.close()

    other = _admit(tmp_path / "other")
    other, other_store, other_runner = _formal_runner(other, monkeypatch)
    try:
        with pytest.raises(ValueError, match="admitted DB"):
            _validate(other_runner, report,
                      execution_root=report.evaluation_root,
                      execution_workspace=report.evaluation_root)
    finally:
        other_store.close()


def test_legacy_or_default_manifest_is_readable_but_not_formal_executable(tmp_path):
    report = _admit(tmp_path)
    legacy_like = replace(
        report.manifest, campaign_key=None, reviewer_protocol_json=b"{}")
    assert legacy_like.campaign_key is None
    assert legacy_like.reviewer_protocol == {}
    with pytest.raises(ValueError):
        legacy_like.validate_formal_executable(
            report.plan, report.campaign,
            report.reviewer_protocol.sealed_payload)


def test_admission_only_report_itself_does_not_create_workspace(tmp_path):
    report = _admit(tmp_path)
    assert not Path(report.evaluation_root).exists()
    report.validate()
    assert not Path(report.evaluation_root).exists()


def test_generic_supported_path_cannot_mint_or_resume_formal(tmp_path, monkeypatch):
    report = _admit(tmp_path)
    report, store, runner = _formal_runner(report, monkeypatch)
    try:
        unsupported_formal_record = runner._create_run_record(
            admitted_plan=report.plan, campaign=report.campaign,
            manifest=report.manifest, execution_mode="FORMAL",
            formal_admission_commitment=SHA,
            formal_admission_payload=report.admission_payload())
        with pytest.raises(ValueError, match="protected"):
            store.append(unsupported_formal_record)
        store.admit_oracle(report.oracle_revision)
        store.admit_plan(report.plan)
        generic = runner._create_run_record(
            admitted_plan=report.plan, campaign=report.campaign,
            manifest=report.manifest, execution_mode="GENERIC")
        store.append(generic)
        assert json.loads(store.get(generic.record_id).semantic_json)[
            "execution_mode"] == "GENERIC"
        with pytest.raises(ValueError, match="FORMAL admission provenance"):
            store.require_formal_run(generic.record_id)
        with pytest.raises(ValueError, match="FORMAL admission provenance"):
            runner._run_admitted(
                admitted_plan=report.plan, campaign=report.campaign,
                manifest=report.manifest, warmup=lambda: None,
                run_already_admitted=True)
    finally:
        store.close()


def test_store_revalidates_exact_formal_binding_and_replay(tmp_path, monkeypatch):
    report = _admit(tmp_path)
    report, store, runner = _formal_runner(report, monkeypatch)
    try:
        run = _issue_formal_root(store, runner, report)
        assert store.require_formal_run(run.record_id) == run
        assert store._append_formal_run(run) == run

        payload = json.loads(run.semantic_json)
        payload["formal_admission_commitment"] = SHA
        wrong_commitment = LedgerRecord.create(
            "evaluation_run", [report.plan.evaluation_plan_id,
                               report.campaign.campaign_key], payload,
            report.plan.evaluation_plan_id)
        with pytest.raises(ValueError, match="binding"):
            store._append_formal_run(wrong_commitment)

        payload = json.loads(run.semantic_json)
        payload["formal_admission"]["manifest_commitment"] = SHA
        mismatch = LedgerRecord.create(
            "evaluation_run", [report.plan.evaluation_plan_id,
                               report.campaign.campaign_key], payload,
            report.plan.evaluation_plan_id)
        with pytest.raises(ValueError, match="binding"):
            store._append_formal_run(mismatch)

        legacy_manifest = replace(
            report.manifest, campaign_key=None, reviewer_protocol_json=b"{}")
        legacy_attempt = runner._create_run_record(
            admitted_plan=report.plan, campaign=report.campaign,
            manifest=legacy_manifest, execution_mode="FORMAL",
            formal_admission_commitment=report.admission_commitment,
            formal_admission_payload=report.admission_payload())
        with pytest.raises(ValueError, match="binding"):
            store._append_formal_run(legacy_attempt)
    finally:
        store.close()


def test_internal_resume_requires_existing_run_manifest_and_campaign_truth(
    tmp_path, monkeypatch,
):
    report = _admit(tmp_path)
    report, store, runner = _formal_runner(report, monkeypatch)
    try:
        _issue_formal_root(store, runner, report)
        contradictory = replace(report.manifest, timeout_ms=299_000)
        with pytest.raises(ValueError, match="manifest"):
            runner._run_admitted(
                admitted_plan=report.plan, campaign=report.campaign,
                manifest=contradictory, warmup=lambda: None,
                run_already_admitted=True)
        snapshot = store.reader().read_run_snapshot(report.campaign.run_id)
        assert not any(record.domain == "execution" for record in snapshot.records)
    finally:
        store.close()


def test_generic_run_is_immutable_and_cannot_be_promoted(tmp_path, monkeypatch):
    report = _admit(tmp_path)
    report, store, runner = _formal_runner(report, monkeypatch)
    try:
        _validate(runner, report)
        store.admit_oracle(report.oracle_revision)
        store.admit_plan(report.plan)
        generic = runner._create_run_record(
            admitted_plan=report.plan, campaign=report.campaign,
            manifest=report.manifest, execution_mode="GENERIC")
        store.append(generic)
        formal = runner._create_run_record(
            admitted_plan=report.plan, campaign=report.campaign,
            manifest=report.manifest, execution_mode="FORMAL",
            formal_admission_commitment=report.admission_commitment,
            formal_admission_payload=report.admission_payload())
        with pytest.raises(LedgerConflict, match="contradictory replay"):
            store._append_formal_run(formal)
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["GENERIC", None, "UNKNOWN", "LEGACY"])
def test_nonformal_classifications_cannot_mint_formal_report(
    tmp_path, monkeypatch, mode,
):
    report = _admit(tmp_path)
    report, store, runner = _formal_runner(report, monkeypatch)
    try:
        store.admit_oracle(report.oracle_revision)
        store.admit_plan(report.plan)
        generic = runner._create_run_record(
            admitted_plan=report.plan, campaign=report.campaign,
            manifest=report.manifest, execution_mode="GENERIC")
        payload = json.loads(generic.semantic_json)
        if mode is None:
            payload.pop("execution_mode")
        else:
            payload["execution_mode"] = mode
        record = LedgerRecord.create(
            "evaluation_run", [report.plan.evaluation_plan_id,
                               report.campaign.campaign_key], payload,
            report.plan.evaluation_plan_id)
        store.append(record)
        projection = build_formal_report(store.reader(), record.record_id)
        assert not projection.readiness.formal_evaluation_pass
        assert "FORMAL_RUN_NOT_ELIGIBLE" in projection.readiness.blockers
        with pytest.raises(ValueError, match="FORMAL admission provenance"):
            append_report_commitment(
                store, projection, generated_at="2026-10-15T00:00:00Z")
    finally:
        store.close()


def test_properly_admitted_formal_root_is_report_eligible_without_execution(
    tmp_path, monkeypatch,
):
    report = _admit(tmp_path)
    report, store, runner = _formal_runner(report, monkeypatch)
    try:
        run = _issue_formal_root(store, runner, report)
        projection = build_formal_report(store.reader(), run.record_id)
        commitment_record = append_report_commitment(
            store, projection, generated_at="2026-10-15T00:00:00Z")
        assert commitment_record.parent_id == run.record_id
    finally:
        store.close()
