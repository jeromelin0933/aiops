"""Fail-closed SPEC-017 preflight and durable Final Formal Admission CLI.

This module constructs existing Candidate-F plans and manifests.  It never
invokes a provider, builds RAG, starts Docker, injects scenarios, or executes a
campaign.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Mapping, Sequence

from .authority import load_trusted_oracle_approvals
from .campaign import CampaignAdmission, CampaignPlan
from .fixtures import FixtureAdmissionRequest, load_fixture_manifest
from .identity import canonical_bytes, commitment
from .manifest import ReproducibilityManifest
from .oracle import EvaluationPlan, OracleRevision, SCENARIOS, load_official_oracle
from .readiness import (
    RagChildAllocation, RequiredFactsMaterialization,
    resolve_formal_required_facts, resolve_materialized_formal_config,
)
from .runtime_image_readiness import RuntimeImageReadinessResolver
from .sqlite_store import SqliteEvaluationStore


FORMAL_SCHEMA = "spec017.formal-preregistration.v1"
REVIEWER_SCHEMA = "spec017.reviewer-roster.v1"
FORMAL_REPETITIONS = 5
FORMAL_SEED = 17017
FORMAL_POLLING_MS = 1_000
FORMAL_TIMEOUT_MS = 300_000
FORMAL_ORDER = "REPETITION_MAJOR"
FORMAL_REPLACEMENT = "NONE"
FORMAL_AUTHORITIES = frozenset({
    "PRD-004", "SPEC-017-v1.0", "PM-SPEC017-R2S4-20261013",
    "PM-SPEC017-EOL-COMMITMENT-20261015",
})


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    if value == "REQUIRED_BEFORE_EXECUTION":
        raise ValueError(f"{name} must be frozen before executable admission")
    return value


def _strict_object(value: object, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} schema is invalid")
    return value


def _assert_no_unfrozen(value: object) -> None:
    if value == "REQUIRED_BEFORE_EXECUTION":
        raise ValueError("all manifest inputs must be frozen before executable admission")
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_unfrozen(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_unfrozen(item)


@dataclass(frozen=True, slots=True)
class ReviewerProtocol:
    stage1_reviewer_ids: tuple[str, str]
    stage1_tiebreak_reviewer_id: str
    stage2_reviewer_ids: tuple[str, str]
    tiebreak_reviewer_id: str
    stage1_blind_diagnostic_only: bool = True
    stage1_official_verdict_allowed: bool = False
    stage2_independent: bool = True
    machine_hard_failure_override_allowed: bool = False
    records_append_only: bool = True

    def __post_init__(self) -> None:
        if len(self.stage1_reviewer_ids) != 2:
            raise ValueError("exactly two Stage-1 reviewer identities are required")
        identities = (*self.stage1_reviewer_ids, self.stage1_tiebreak_reviewer_id,
                      *self.stage2_reviewer_ids,
                      self.tiebreak_reviewer_id)
        for value in identities:
            _text(value, "reviewer identity")
        if len(set(identities)) != len(identities):
            raise ValueError("reviewer identities must be distinct")
        if len(self.stage2_reviewer_ids) != 2:
            raise ValueError("exactly two Stage-2 reviewer identities are required")
        if (not self.stage1_blind_diagnostic_only
                or self.stage1_official_verdict_allowed
                or not self.stage2_independent
                or self.machine_hard_failure_override_allowed
                or not self.records_append_only):
            raise ValueError("reviewer protocol does not match the frozen formal protocol")

    @property
    def sealed_payload(self) -> dict[str, object]:
        return {
            "stage1": {
                "reviewer_ids": list(self.stage1_reviewer_ids),
                "reviewer_kind": "HUMAN",
                "blind_diagnostic_only": True,
                "official_verdict_allowed": False,
                "disagreement": "THIRD_HUMAN_TIEBREAK",
                "tiebreak_reviewer_id": self.stage1_tiebreak_reviewer_id,
                "tiebreak_reviewer_kind": "HUMAN",
            },
            "stage2": {
                "reviewer_ids": list(self.stage2_reviewer_ids),
                "reviewer_kind": "HUMAN",
                "independent": True,
                "disagreement": "THIRD_HUMAN_TIEBREAK",
                "tiebreak_reviewer_id": self.tiebreak_reviewer_id,
                "tiebreak_reviewer_kind": "HUMAN",
            },
            "machine_hard_failure_override_allowed": False,
            "records_append_only": True,
        }


def load_reviewer_protocol(path: str | Path) -> ReviewerProtocol:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _strict_object(raw, {"schema", "stage1", "stage2",
                         "machine_hard_failure_override_allowed",
                         "records_append_only"}, "reviewer roster")
    if raw["schema"] != REVIEWER_SCHEMA:
        raise ValueError("unsupported reviewer roster")
    stage1 = _strict_object(raw["stage1"], {
        "reviewer_ids", "reviewer_kind", "blind_diagnostic_only",
        "official_verdict_allowed", "disagreement", "tiebreak_reviewer_id",
        "tiebreak_reviewer_kind",
    }, "Stage-1 reviewer")
    stage2 = _strict_object(raw["stage2"], {
        "reviewer_ids", "reviewer_kind", "independent", "disagreement",
        "tiebreak_reviewer_id", "tiebreak_reviewer_kind",
    }, "Stage-2 reviewer")
    if (stage1["reviewer_kind"] != "HUMAN"
            or stage1["tiebreak_reviewer_kind"] != "HUMAN"
            or stage1["disagreement"] != "THIRD_HUMAN_TIEBREAK"
            or stage2["reviewer_kind"] != "HUMAN"
            or stage2["tiebreak_reviewer_kind"] != "HUMAN"
            or stage2["disagreement"] != "THIRD_HUMAN_TIEBREAK"):
        raise ValueError("Stage-2 disagreement must use a third human tie-break")
    if (not isinstance(stage1["reviewer_ids"], list)
            or len(stage1["reviewer_ids"]) != 2
            or not isinstance(stage2["reviewer_ids"], list)
            or len(stage2["reviewer_ids"]) != 2):
        raise ValueError("reviewer identity lists are invalid")
    for name, value in (
        ("stage1 blind diagnostic", stage1["blind_diagnostic_only"]),
        ("stage1 official verdict", stage1["official_verdict_allowed"]),
        ("stage2 independence", stage2["independent"]),
        ("machine override", raw["machine_hard_failure_override_allowed"]),
        ("append-only records", raw["records_append_only"]),
    ):
        if type(value) is not bool:
            raise ValueError(f"{name} must be boolean")
    return ReviewerProtocol(
        tuple(stage1["reviewer_ids"]), stage1["tiebreak_reviewer_id"],
        tuple(stage2["reviewer_ids"]),
        stage2["tiebreak_reviewer_id"],
        stage1["blind_diagnostic_only"], stage1["official_verdict_allowed"],
        stage2["independent"], raw["machine_hard_failure_override_allowed"],
        raw["records_append_only"],
    )


@dataclass(frozen=True, slots=True)
class RepositoryState:
    branch: str
    commit: str
    dirty: bool


def inspect_repository(root: str | Path) -> RepositoryState:
    def git(*args: str) -> str:
        result = subprocess.run(
            ("git", *args), cwd=Path(root), check=True, capture_output=True,
            text=True, encoding="utf-8",
        )
        return result.stdout.strip()
    return RepositoryState(git("branch", "--show-current"), git("rev-parse", "HEAD"),
                           bool(git("status", "--short")))


@dataclass(frozen=True, slots=True)
class FormalAdmissionReport:
    oracle_revision: OracleRevision
    plan: EvaluationPlan
    campaign: CampaignPlan
    manifest: ReproducibilityManifest
    reviewer_protocol: ReviewerProtocol
    preregistration_commitment: str
    admission_commitment: str
    denominator: Mapping[str, object]
    repository_root: str
    evaluation_root: str
    database_path: str
    preparation_id: str | None = None
    preparation_commitment: str | None = None
    expected_topology_commitment: str | None = None
    expected_interruption_matrix_identity: str | None = None
    materialized_config_commitment: str | None = None

    def admission_payload(self) -> dict[str, object]:
        """Return the deterministic governance and lineage material for this admission."""
        payload = {
            "schema": "spec017.formal-admission.v1",
            "campaign_key": self.campaign.campaign_key,
            "evaluation_run_id": self.campaign.run_id,
            "evaluation_plan_id": self.plan.evaluation_plan_id,
            "manifest_commitment": self.manifest.manifest_commitment,
            "preregistration_commitment": self.preregistration_commitment,
            "materialized_config_commitment": self.materialized_config_commitment,
            "reviewer_protocol": self.reviewer_protocol.sealed_payload,
            "reviewer_protocol_commitment": commitment(
                self.reviewer_protocol.sealed_payload),
            "controls": {
                "repetitions": FORMAL_REPETITIONS, "order": FORMAL_ORDER,
                "bootstrap_seed": FORMAL_SEED, "polling_interval_seconds": 1,
                "per_execution_timeout_seconds": 300, "early_stop": False,
                "replacement": FORMAL_REPLACEMENT,
            },
            "execution_order": [item.execution_id for item in self.campaign.executions],
            "denominator_policy": dict(self.denominator),
            "repository_root": self.repository_root,
            "database_path": self.database_path,
            "evaluation_root": self.evaluation_root,
            "execution_workspace": self.evaluation_root,
        }
        if self.preparation_id is not None:
            payload["preparation_id"] = self.preparation_id
            payload["preparation_commitment"] = self.preparation_commitment
            payload["expected_topology_commitment"] = (
                self.expected_topology_commitment)
            payload["expected_interruption_matrix_identity"] = (
                self.expected_interruption_matrix_identity)
            payload["lifecycle_state"] = "ADMITTED"
        return payload

    def validate(self) -> None:
        """Validate this typed governance result and its deterministic lineage."""
        for label, value in (
            ("raw preregistration commitment", self.preregistration_commitment),
            ("materialized config commitment",
             self.materialized_config_commitment),
        ):
            if (not isinstance(value, str) or len(value) != 71
                    or not value.startswith("sha256:")):
                raise ValueError(f"{label} is not durably frozen")
            try:
                int(value[7:], 16)
            except ValueError as exc:
                raise ValueError(f"{label} is malformed") from exc
        if (self.manifest.evaluation_config_commitment !=
                self.materialized_config_commitment):
            raise ValueError("manifest is not bound to the materialized config")
        if self.preparation_id is not None:
            for label, value in (
                ("expected topology commitment",
                 self.expected_topology_commitment),
                ("expected interruption matrix identity",
                 self.expected_interruption_matrix_identity),
            ):
                if (not isinstance(value, str) or len(value) != 71
                        or not value.startswith("sha256:")):
                    raise ValueError(f"{label} is not durably frozen")
                try:
                    int(value[7:], 16)
                except ValueError as exc:
                    raise ValueError(f"{label} is malformed") from exc
        if commitment(self.admission_payload()) != self.admission_commitment:
            raise ValueError("FormalAdmissionReport is internally inconsistent")
        self.oracle_revision.validate()
        self.plan.validate(self.oracle_revision)
        self.campaign.validate(self.plan)
        self.manifest.validate_formal_executable(
            self.plan, self.campaign, self.reviewer_protocol.sealed_payload)

    def durable_admission_payload(self) -> dict[str, object]:
        """Return the sole durable representation of this ADMITTED transition."""
        if self.preparation_id is None or self.preparation_commitment is None:
            raise ValueError("preflight report has no durable Final Admission")
        return {
            "schema": "spec017.preparation-admission.v1",
            "lifecycle_state": "ADMITTED",
            "preparation_id": self.preparation_id,
            "preparation_commitment": self.preparation_commitment,
            "formal_admission_commitment": self.admission_commitment,
            "formal_admission_payload": self.admission_payload(),
        }

    def public_payload(self) -> dict[str, object]:
        durable_admission = self.preparation_id is not None
        payload = {
            "mode": ("FINAL_FORMAL_ADMISSION" if durable_admission
                     else "PREFLIGHT_ONLY"),
            "admitted": durable_admission,
            "campaign_key": self.campaign.campaign_key,
            "evaluation_run_id": self.campaign.run_id,
            "evaluation_plan_id": self.campaign.evaluation_plan_id,
            "manifest_commitment": self.manifest.manifest_commitment,
            "preregistration_commitment": self.preregistration_commitment,
            "materialized_config_commitment": self.materialized_config_commitment,
            "admission_commitment": self.admission_commitment,
            "fixed_n": self.campaign.repetitions,
            "nominal_slot_count": len(self.campaign.executions),
            "execution_order": [
                {"ordinal": index, "scenario_id": item.scenario_id,
                 "repetition": item.repetition, "execution_id": item.execution_id}
                for index, item in enumerate(self.campaign.executions, 1)
            ],
            "denominator": dict(self.denominator),
            "execution_performed": False,
            "side_effects": "NONE",
        }
        if durable_admission:
            payload["preparation_id"] = self.preparation_id
        else:
            payload["preflight_passed"] = True
        return payload


def _load_preregistration(path: str | Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema", "authority_references", "repository", "oracle_artifact",
        "fixture_manifest", "evaluation_plan", "campaign_controls",
        "manifest_inputs", "denominator_policy",
    }
    _strict_object(raw, required, "formal pre-registration")
    if raw["schema"] != FORMAL_SCHEMA:
        raise ValueError("unsupported formal pre-registration")
    authorities = raw["authority_references"]
    if (not isinstance(authorities, list)
            or len(authorities) != len(set(authorities))
            or set(authorities) != FORMAL_AUTHORITIES):
        raise ValueError("formal authority references do not match the delegated authority")
    controls = _strict_object(raw["campaign_controls"], {
        "repetitions", "order", "bootstrap_seed", "polling_interval_seconds",
        "per_execution_timeout_seconds", "early_stop", "replacement",
    }, "campaign controls")
    expected = {
        "repetitions": FORMAL_REPETITIONS, "order": FORMAL_ORDER,
        "bootstrap_seed": FORMAL_SEED, "polling_interval_seconds": 1,
        "per_execution_timeout_seconds": 300, "early_stop": False,
        "replacement": FORMAL_REPLACEMENT,
    }
    for name in ("repetitions", "bootstrap_seed", "polling_interval_seconds",
                 "per_execution_timeout_seconds"):
        if type(controls[name]) is not int:
            raise ValueError(f"{name} must be an integer")
    if type(controls["early_stop"]) is not bool:
        raise ValueError("early_stop must be boolean")
    if controls != expected:
        raise ValueError("formal campaign controls differ from the frozen delegation")
    return raw


def _assert_fresh_namespace(database_path: Path, evaluation_root: Path) -> None:
    database = database_path.resolve()
    root = evaluation_root.resolve()
    if root.name != "rca_evaluation":
        raise ValueError("evaluation root must be a dedicated rca_evaluation directory")
    if database.parent != root or database.suffix != ".db":
        raise ValueError("evaluation database must be a .db directly under evaluation root")
    if root.exists() or database.exists():
        raise ValueError("formal admission requires a fresh DB/workspace namespace")


def admit_formal_campaign(
    *, repository_root: str | Path, preregistration_path: str | Path,
    trusted_approval_path: str | Path, reviewer_roster_path: str | Path,
    campaign_key: str, database_path: str | Path, evaluation_root: str | Path,
    repository_state: RepositoryState | None = None,
    readiness_materialization: RequiredFactsMaterialization | None = None,
    candidate_c_authority: object | None = None,
    runtime_authority: object | None = None,
    docker_state_inspector: object | None = None,
    allocation_reference: RagChildAllocation | None = None,
    evaluation_store: SqliteEvaluationStore | None = None,
    allocation_evaluation_run_id: str | None = None,
    allocation_execution_id: str | None = None,
    preparation_id: str | None = None,
    _allow_prepared_namespace: bool = False,
) -> FormalAdmissionReport:
    """Validate formal admission in memory; perform no campaign/store writes."""
    root = Path(repository_root).resolve()
    prereg_path = Path(preregistration_path).resolve()
    trust_path = Path(trusted_approval_path).resolve()
    reviewer_path = Path(reviewer_roster_path).resolve()
    _text(campaign_key, "campaign_key")
    if trust_path == prereg_path or reviewer_path in {trust_path, prereg_path}:
        raise ValueError("trust, reviewer, and pre-registration inputs must be independent files")
    if not _allow_prepared_namespace:
        _assert_fresh_namespace(Path(database_path), Path(evaluation_root))
    config = _load_preregistration(prereg_path)
    raw_preregistration_commitment = commitment(config)
    if readiness_materialization is not None:
        raise ValueError(
            "caller RequiredFactsMaterialization is not Formal admission authority")
    if ("REQUIRED_BEFORE_EXECUTION" in json.dumps(config, sort_keys=True)
            or preparation_id is not None):
        try:
            resolved_config = resolve_materialized_formal_config(
            config, repository_root=root,
            candidate_c_authority=candidate_c_authority,
            runtime_authority=runtime_authority,
            docker_state_inspector=docker_state_inspector,
            allocation_reference=allocation_reference,
            evaluation_store=evaluation_store,
            evaluation_run_id=allocation_evaluation_run_id,
            execution_id=allocation_execution_id,
            preparation_id=preparation_id,
            _required_facts_resolver=resolve_formal_required_facts,
        )
        except ValueError as exc:
            raise ValueError(
                f"all manifest inputs must be frozen before executable admission: {exc}"
            ) from exc
        config = dict(resolved_config.materialized_config)
    _assert_no_unfrozen(config)
    materialized_config_commitment = commitment(config)
    repo = repository_state or inspect_repository(root)
    repository = _strict_object(config["repository"], {"required_branch"}, "repository")
    if repo.branch != repository["required_branch"] or repo.dirty:
        raise ValueError("repository branch/cleanliness admission failed")
    if len(repo.commit) != 40 or any(ch not in "0123456789abcdef" for ch in repo.commit.lower()):
        raise ValueError("repository commit is invalid")

    oracle_path = (root / config["oracle_artifact"]).resolve()
    fixture_path = (root / config["fixture_manifest"]).resolve()
    if trust_path in {oracle_path, fixture_path}:
        raise ValueError("TrustedOracleApproval must be independent of Oracle/fixture inputs")
    oracle_artifact = load_official_oracle(oracle_path)
    approvals = load_trusted_oracle_approvals(trust_path)
    approvals.verify_oracle(oracle_artifact.revision)
    reviewers = load_reviewer_protocol(reviewer_path)
    resolver = load_fixture_manifest(fixture_path, oracle=oracle_artifact,
                                     repository_root=root)

    plan_input = _strict_object(config["evaluation_plan"], {
        "production_build", "model", "prompt", "result_schema",
        "corpus_index", "configuration_reference",
    }, "evaluation plan")
    if plan_input["production_build"] != repo.commit:
        raise ValueError("production_build must equal the admitted repository commit")
    plan = EvaluationPlan.admit(
        oracle_artifact.revision, fixture_revision=resolver.fixture_revision,
        production_build=_text(plan_input["production_build"], "production_build"),
        model=_text(plan_input["model"], "model"),
        prompt=_text(plan_input["prompt"], "prompt"),
        schema=_text(plan_input["result_schema"], "result_schema"),
        corpus_index=_text(plan_input["corpus_index"], "corpus_index"),
        configuration=_text(plan_input["configuration_reference"],
                            "configuration_reference"),
        repetitions=FORMAL_REPETITIONS,
    )
    requests = {}
    for entry in resolver.entries:
        if entry.variant == "nominal":
            requests[entry.scenario_id] = FixtureAdmissionRequest(
                entry.scenario_id, entry.variant, entry.input_identity,
                entry.input_commitment, entry.oracle_scenario_reference)
    prereg_commitment = raw_preregistration_commitment
    for name in ("provider_profile_references", "rag_profile_references"):
        values = config["manifest_inputs"].get(name)
        if not isinstance(values, list) or not values:
            raise ValueError(f"{name} must be a non-empty list")
    admission = CampaignAdmission(
        repository_commit=repo.commit, repository_dirty=False,
        evaluation_config_commitment=materialized_config_commitment,
        provider_profile_references=tuple(config["manifest_inputs"]["provider_profile_references"]),
        rag_profile_references=tuple(config["manifest_inputs"]["rag_profile_references"]),
        environment_identity=_text(config["manifest_inputs"]["environment_identity"], "environment_identity"),
        evaluator_rubric_version=_text(config["manifest_inputs"]["evaluator_rubric_version"], "evaluator_rubric_version"),
        polling_interval_ms=FORMAL_POLLING_MS, timeout_ms=FORMAL_TIMEOUT_MS,
        clock_source=_text(config["manifest_inputs"]["clock_source"], "clock_source"),
        clock_synchronization_uncertainty_ms=config["manifest_inputs"]["clock_synchronization_uncertainty_ms"],
        random_seed=FORMAL_SEED, deterministic_order_commitment=None,
    )
    campaign = CampaignPlan.fixed_nominal(
        plan, campaign_key=campaign_key, fixture_resolver=resolver,
        fixture_requests=requests, admission=admission,
    )
    order = tuple((item.scenario_id, item.repetition) for item in campaign.executions)
    expected_order = tuple((scenario, repetition)
                           for repetition in range(1, 6) for scenario in SCENARIOS)
    if order != expected_order or len(campaign.executions) != 30:
        raise ValueError("formal nominal order/slot count is not repetition-major 30")

    manifest_inputs = config["manifest_inputs"]
    approved = dict(manifest_inputs["approved_spec_commitments"])
    approved["required_isolation_case_ids"] = [item.execution_id for item in campaign.executions]
    approved["required_deviation_case_ids"] = [item.execution_id for item in campaign.executions]
    approved["required_refresh_case_ids"] = list(campaign.required_refresh_case_ids)
    approved["required_restart_case_ids"] = list(campaign.required_restart_case_ids)
    manifest = ReproducibilityManifest.seal(
        run_id=campaign.run_id,
        execution_ids=tuple(item.execution_id for item in campaign.executions),
        scenario_inputs=tuple((item.scenario_id, item.input_identity, item.input_commitment)
                              for item in campaign.executions),
        random_seed=FORMAL_SEED, deterministic_order_commitment=None,
        evaluation_plan_id=plan.evaluation_plan_id,
        oracle_revision_id=plan.oracle_revision_id,
        oracle_content_commitment=plan.oracle_content_commitment,
        fixture_revision=plan.fixture_revision, production_build=plan.production_build,
        model=plan.model, prompt=plan.prompt, result_schema=plan.schema,
        corpus_index=plan.corpus_index,
        evaluation_config_reference=plan.configuration,
        evaluation_config=config, repository_commit=repo.commit,
        repository_dirty=False,
        provider_profile_references=admission.provider_profile_references,
        rag_profile_references=admission.rag_profile_references,
        environment_identity=admission.environment_identity,
        evaluator_rubric_version=admission.evaluator_rubric_version,
        polling_interval_ms=FORMAL_POLLING_MS, timeout_ms=FORMAL_TIMEOUT_MS,
        clock_source=admission.clock_source,
        clock_synchronization_uncertainty_ms=admission.clock_synchronization_uncertainty_ms,
        approved_spec_commitments=approved,
        campaign_key=campaign_key, execution_order_strategy=FORMAL_ORDER,
        early_stop_allowed=False, replacement_policy=FORMAL_REPLACEMENT,
        reviewer_protocol=reviewers.sealed_payload,
    )
    manifest.validate_admission(plan, campaign)
    denominator = _strict_object(config["denominator_policy"], {
        "nominal", "degraded", "failure", "refresh", "restart",
    }, "denominator policy")
    repository_path = str(root)
    database = str(Path(database_path).resolve())
    evaluation = str(Path(evaluation_root).resolve())
    provisional = FormalAdmissionReport(
        oracle_artifact.revision, plan, campaign, manifest, reviewers,
        prereg_commitment, "sha256:" + "0" * 64, denominator,
        repository_path, evaluation, database,
        materialized_config_commitment=materialized_config_commitment)
    lineage_payload = provisional.admission_payload()
    return FormalAdmissionReport(
        oracle_artifact.revision, plan, campaign, manifest, reviewers,
        prereg_commitment, commitment(lineage_payload), denominator,
        repository_path, evaluation, database,
        materialized_config_commitment=materialized_config_commitment)


def final_admit_formal_campaign(
    *, preparation_id: str, evaluation_store: SqliteEvaluationStore,
    repository_root: str | Path, preregistration_path: str | Path,
    trusted_approval_path: str | Path, reviewer_roster_path: str | Path,
    campaign_key: str, database_path: str | Path, evaluation_root: str | Path,
    candidate_c_authority: object | None = None,
    runtime_authority: object | None = None,
    docker_state_inspector: object | None = None,
) -> FormalAdmissionReport:
    """Supported Final Admission; repository truth is always fresh-read."""
    return _final_admit_formal_campaign(
        preparation_id=preparation_id, evaluation_store=evaluation_store,
        repository_root=repository_root,
        preregistration_path=preregistration_path,
        trusted_approval_path=trusted_approval_path,
        reviewer_roster_path=reviewer_roster_path,
        campaign_key=campaign_key, database_path=database_path,
        evaluation_root=evaluation_root,
        candidate_c_authority=candidate_c_authority,
        runtime_authority=runtime_authority,
        docker_state_inspector=docker_state_inspector,
        repository_state=None)


def _final_admit_formal_campaign(
    *, preparation_id: str, evaluation_store: SqliteEvaluationStore,
    repository_root: str | Path, preregistration_path: str | Path,
    trusted_approval_path: str | Path, reviewer_roster_path: str | Path,
    campaign_key: str, database_path: str | Path, evaluation_root: str | Path,
    repository_state: RepositoryState | None = None,
    candidate_c_authority: object | None = None,
    runtime_authority: object | None = None,
    docker_state_inspector: object | None = None,
) -> FormalAdmissionReport:
    """Internal implementation; repository_state exists only as a test seam."""
    from .preparation import _build_payload, verify_prepared_real_rag_child

    if type(evaluation_store) is not SqliteEvaluationStore:
        raise TypeError("Final Admission requires SqliteEvaluationStore authority")
    database = Path(database_path).resolve()
    evaluation = Path(evaluation_root).resolve()
    root = Path(repository_root).resolve()
    if evaluation_store.path != database or database.parent != evaluation:
        raise ValueError("Final Admission store/namespace mismatch")
    prepared = evaluation_store.get_preparation(preparation_id)
    if prepared is None:
        raise ValueError("Final Admission requires PREPARED state")
    config = _load_preregistration(preregistration_path)
    state = repository_state or inspect_repository(root)
    expected = _build_payload(
        repository_root=root, config=config, campaign_key=campaign_key,
        repository_branch=state.branch, repository_commit=state.commit,
        database_path=database)
    if prepared != expected:
        raise ValueError("stale or contradictory preparation facts")
    verify_prepared_real_rag_child(evaluation_store, preparation_id, root)
    if not evaluation_store.has_no_formal_execution_facts():
        raise ValueError("unexpected Formal execution or denominator fact")
    allowed = {database.name, database.name + "-journal",
               database.name + "-shm", database.name + "-wal", "real_rag"}
    if (not evaluation.is_dir()
            or any(item.name not in allowed for item in evaluation.iterdir())):
        raise ValueError("prepared namespace ownership/isolation mismatch")

    from .evaluation_controls import CONTROL_ARTIFACT_COMMITMENT
    from .formal_compose import resolve_formal_compose_topology
    from .formal_docker_lifecycle import approved_interruption_matrix

    expected_topology = resolve_formal_compose_topology(
        evaluation_store, preparation_id, root).topology_commitment
    approved_interruption_matrix(root)
    expected_interruption_matrix = CONTROL_ARTIFACT_COMMITMENT

    provisional = admit_formal_campaign(
        repository_root=root, preregistration_path=preregistration_path,
        trusted_approval_path=trusted_approval_path,
        reviewer_roster_path=reviewer_roster_path,
        campaign_key=campaign_key, database_path=database,
        evaluation_root=evaluation, repository_state=state,
        candidate_c_authority=candidate_c_authority,
        runtime_authority=runtime_authority,
        docker_state_inspector=docker_state_inspector,
        evaluation_store=evaluation_store,
        preparation_id=preparation_id, _allow_prepared_namespace=True,
    )
    with_preparation = FormalAdmissionReport(
        provisional.oracle_revision, provisional.plan, provisional.campaign,
        provisional.manifest, provisional.reviewer_protocol,
        provisional.preregistration_commitment, "sha256:" + "0" * 64,
        provisional.denominator, provisional.repository_root,
        provisional.evaluation_root, provisional.database_path,
        preparation_id, commitment(prepared), expected_topology,
        expected_interruption_matrix, provisional.materialized_config_commitment,
    )
    report = FormalAdmissionReport(
        with_preparation.oracle_revision, with_preparation.plan,
        with_preparation.campaign, with_preparation.manifest,
        with_preparation.reviewer_protocol,
        with_preparation.preregistration_commitment,
        commitment(with_preparation.admission_payload()),
        with_preparation.denominator, with_preparation.repository_root,
        with_preparation.evaluation_root, with_preparation.database_path,
        preparation_id, commitment(prepared), expected_topology,
        expected_interruption_matrix,
        with_preparation.materialized_config_commitment,
    )
    evaluation_store.admit_preparation(
        preparation_id, report.durable_admission_payload())
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-only", action="store_true", required=True)
    parser.add_argument("--preparation-id", required=True)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--trusted-oracle-approval", type=Path, required=True)
    parser.add_argument("--reviewer-roster", type=Path, required=True)
    parser.add_argument("--campaign-key", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--evaluation-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database = args.database.resolve()
        evaluation = args.evaluation_root.resolve()
        if not database.is_file() or not evaluation.is_dir():
            raise ValueError(
                "Final Admission requires an existing Candidate-F store")
        approvals = load_trusted_oracle_approvals(
            args.trusted_oracle_approval)
        with SqliteEvaluationStore(database, approvals=approvals) as store:
            report = final_admit_formal_campaign(
                preparation_id=args.preparation_id,
                evaluation_store=store,
                repository_root=args.repository_root,
                preregistration_path=args.config,
                trusted_approval_path=args.trusted_oracle_approval,
                reviewer_roster_path=args.reviewer_roster,
                campaign_key=args.campaign_key, database_path=database,
                evaluation_root=evaluation,
                runtime_authority=RuntimeImageReadinessResolver(
                    args.repository_root),
            )
            durable = store.require_admitted_preparation(
                args.preparation_id, report.admission_commitment)
            if durable != report.durable_admission_payload():
                raise ValueError(
                    "durable Final Admission read-back is contradictory")
    except (OSError, ValueError, TypeError, RuntimeError,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"mode": "FINAL_FORMAL_ADMISSION", "admitted": False,
                          "error": str(exc), "execution_performed": False,
                          "side_effects": "NONE"}, ensure_ascii=False, sort_keys=True))
        return 2
    print(canonical_bytes(report.public_payload()).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
