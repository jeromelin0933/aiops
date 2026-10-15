"""Immutable, secret-free reproducibility commitments for an evaluation run."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Mapping

from .identity import canonical_bytes, commitment, require_identity

if TYPE_CHECKING:
    from .campaign import CampaignPlan, PlannedExecution
    from .oracle import EvaluationPlan


# Exact Candidate-F representation of the Approved SPEC-017 §12 commitments
# not already represented by the strongly typed manifest fields below.
REQUIRED_REPRODUCIBILITY_COMMITMENTS = (
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
)


def validate_reproducibility_commitments(value: object) -> dict[str, object]:
    """Validate the bounded §12 extension without normalizing missing facts."""
    if not isinstance(value, Mapping) or set(value) != set(
            REQUIRED_REPRODUCIBILITY_COMMITMENTS):
        raise ValueError("Approved §12 reproducibility commitments are incomplete")
    result = dict(value)
    for name, item in result.items():
        if isinstance(item, str):
            valid = bool(item.strip())
        elif isinstance(item, (list, tuple, dict)):
            # A generic sealed run may be outside the R2/S4 recovery campaign.
            # Formal CampaignPlan admission below requires its derived sets.
            valid = (name in {"required_refresh_case_ids",
                              "required_restart_case_ids"} or bool(item))
        else:
            valid = item is not None
        if not valid:
            raise ValueError(f"Approved §12 commitment is empty: {name}")
    canonical_bytes(result)
    return result


@dataclass(frozen=True, slots=True)
class ManifestBindingVerification:
    complete: bool
    blockers: tuple[str, ...]


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def verify_run_manifest_binding(
    run_payload: Mapping[str, object], *, evaluation_run_id: str,
    evaluation_plan_id: str, oracle_revision_id: str,
    executions: tuple[tuple[str, Mapping[str, object]], ...],
) -> ManifestBindingVerification:
    """Verify a run projection against the canonical sealed manifest contract."""
    blockers: list[str] = []
    sealed = run_payload.get("sealed_manifest")
    manifest_commitment = run_payload.get("manifest_commitment")
    required_base = {
        "run_id", "execution_ids", "scenario_inputs", "random_seed",
        "deterministic_order_commitment", "evaluation_plan_id",
        "oracle_revision_id", "oracle_content_commitment", "fixture_revision",
        "production_build", "model", "prompt", "result_schema", "corpus_index",
        "evaluation_config_reference", "evaluation_config_commitment",
        "repository_commit", "repository_dirty", "provider_profile_references",
        "rag_profile_references", "environment_identity", "evaluator_rubric_version",
        "polling_interval_ms", "timeout_ms", "clock_source",
        "clock_synchronization_uncertainty_ms", "aggregation_policy_version",
        "approved_spec_commitments",
    }
    if not isinstance(sealed, Mapping):
        return ManifestBindingVerification(False, ("SEALED_MANIFEST_MISSING",))
    sealed = dict(sealed)
    if set(sealed) != required_base:
        blockers.append("SEALED_MANIFEST_SCHEMA_INVALID")
    if not _is_sha256(manifest_commitment):
        blockers.append("MANIFEST_COMMITMENT_MALFORMED")
    elif commitment(sealed) != manifest_commitment:
        blockers.append("MANIFEST_COMMITMENT_MISMATCH")
    try:
        extension = validate_reproducibility_commitments(
            sealed.get("approved_spec_commitments"))
    except (TypeError, ValueError):
        extension = {}
        blockers.append("APPROVED_REPRODUCIBILITY_COMMITMENTS_INVALID")
    for name in (
        "oracle_content_commitment", "evaluation_config_commitment",
    ):
        if not _is_sha256(sealed.get(name)):
            blockers.append(f"{name.upper()}_MALFORMED")
    for name, value in extension.items():
        if (name.endswith("_commitment") or name.endswith("_digest")):
            values = value if isinstance(value, list) else [value]
            if any(not _is_sha256(item) for item in values):
                blockers.append(f"{name.upper()}_MALFORMED")
    repository_commit = sealed.get("repository_commit")
    if not isinstance(repository_commit, str) or not (
            len(repository_commit) == 40
            and all(character in "0123456789abcdefABCDEF"
                    for character in repository_commit)):
        blockers.append("REPOSITORY_COMMIT_INVALID")
    expected_scalar = {
        "oracle_revision_id": oracle_revision_id,
        "evaluation_plan_id": evaluation_plan_id,
        "run_id": evaluation_run_id,
    }
    for name, expected in expected_scalar.items():
        if sealed.get(name) != expected:
            blockers.append(f"{name.upper()}_BINDING_MISMATCH")
    root_names = (
        "oracle_content_commitment", "fixture_revision", "production_build",
        "model", "prompt", "result_schema", "corpus_index",
        "evaluation_config_commitment", "repository_commit", "repository_dirty",
        "provider_profile_references", "rag_profile_references",
        "environment_identity", "evaluator_rubric_version", "polling_interval_ms",
        "timeout_ms", "clock_source", "clock_synchronization_uncertainty_ms",
        "aggregation_policy_version",
    )
    for name in root_names:
        sealed_value = sealed.get(name)
        if isinstance(sealed_value, list):
            sealed_value = list(sealed_value)
        if run_payload.get(name) != sealed_value:
            blockers.append(f"{name.upper()}_RUN_BINDING_MISMATCH")
    for name, value in extension.items():
        if run_payload.get(name) != value:
            blockers.append(f"{name.upper()}_RUN_BINDING_MISMATCH")
    expected_order = sealed.get("execution_ids")
    if (not isinstance(expected_order, list)
            or run_payload.get("execution_order") != expected_order
            or tuple(item[0] for item in executions) != tuple(expected_order)):
        blockers.append("EXECUTION_ORDER_BINDING_MISMATCH")
    for name in ("required_isolation_case_ids", "required_deviation_case_ids",
                 "required_refresh_case_ids", "required_restart_case_ids"):
        values = extension.get(name)
        if (not isinstance(values, list)
                or any(not isinstance(item, str) or not item for item in values)
                or len(values) != len(set(values))):
            blockers.append(f"{name.upper()}_INVALID")
    for name in ("required_isolation_case_ids", "required_deviation_case_ids"):
        if set(extension.get(name, ())) != set(expected_order or ()):
            blockers.append(f"{name.upper()}_PLAN_MISMATCH")
    scenario_inputs = sealed.get("scenario_inputs")
    actual_inputs = [
        [payload.get("scenario_id"), payload.get("input_identity"),
         payload.get("input_commitment")]
        for _, payload in executions]
    if scenario_inputs != actual_inputs:
        blockers.append("FIXTURE_INPUT_BINDING_MISMATCH")
    if any(not _is_sha256(item[2]) for item in actual_inputs):
        blockers.append("FIXTURE_INPUT_COMMITMENT_MALFORMED")
    if any(payload.get("manifest_commitment") != manifest_commitment
           for _, payload in executions):
        blockers.append("EXECUTION_MANIFEST_BINDING_MISMATCH")
    random_or_order = (sealed.get("random_seed")
                       if sealed.get("random_seed") is not None
                       else sealed.get("deterministic_order_commitment"))
    if run_payload.get("random_seed_or_order_commitment") != random_or_order:
        blockers.append("EXECUTION_ORDER_SEED_BINDING_MISMATCH")
    fixed_n = run_payload.get("fixed_n")
    scenario_counts = {
        scenario: sum(payload.get("scenario_id") == scenario for _, payload in executions)
        for scenario in (f"S{index}" for index in range(1, 7))}
    if (type(fixed_n) is not int or fixed_n <= 0
            or any(value != fixed_n for value in scenario_counts.values())):
        blockers.append("FIXED_N_BINDING_MISMATCH")
    return ManifestBindingVerification(
        not blockers, tuple(sorted(set(blockers))))


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


@dataclass(frozen=True, slots=True)
class ReproducibilityManifest:
    run_id: str
    execution_ids: tuple[str, ...]
    scenario_inputs: tuple[tuple[str, str, str], ...]
    random_seed: int | None
    deterministic_order_commitment: str | None
    evaluation_plan_id: str
    oracle_revision_id: str
    oracle_content_commitment: str
    fixture_revision: str
    production_build: str
    model: str
    prompt: str
    result_schema: str
    corpus_index: str
    evaluation_config_reference: str
    evaluation_config_commitment: str
    repository_commit: str
    repository_dirty: bool
    provider_profile_references: tuple[str, ...]
    rag_profile_references: tuple[str, ...]
    environment_identity: str
    evaluator_rubric_version: str
    polling_interval_ms: int
    timeout_ms: int
    clock_source: str
    clock_synchronization_uncertainty_ms: int
    aggregation_policy_version: str
    approved_spec_commitments_json: bytes
    canonical_json: bytes
    manifest_commitment: str

    @classmethod
    def seal(
        cls,
        *,
        run_id: str,
        execution_ids: tuple[str, ...],
        scenario_inputs: tuple[tuple[str, str, str], ...],
        random_seed: int | None,
        deterministic_order_commitment: str | None,
        evaluation_plan_id: str,
        oracle_revision_id: str,
        oracle_content_commitment: str,
        fixture_revision: str,
        production_build: str,
        model: str,
        prompt: str,
        result_schema: str,
        corpus_index: str,
        evaluation_config_reference: str,
        evaluation_config: Mapping[str, object],
        repository_commit: str,
        repository_dirty: bool,
        provider_profile_references: tuple[str, ...],
        rag_profile_references: tuple[str, ...],
        environment_identity: str,
        evaluator_rubric_version: str,
        polling_interval_ms: int,
        timeout_ms: int,
        clock_source: str,
        clock_synchronization_uncertainty_ms: int,
        approved_spec_commitments: Mapping[str, object],
        aggregation_policy_version: str = "SPEC-017-S4-nearest-rank-v2",
    ) -> "ReproducibilityManifest":
        require_identity(run_id, "evaluation_run")
        require_identity(evaluation_plan_id, "evaluation_plan")
        require_identity(oracle_revision_id, "oracle_revision")
        if not execution_ids or len(set(execution_ids)) != len(execution_ids):
            raise ValueError("execution_ids must be non-empty and unique")
        for item in execution_ids:
            require_identity(item, "execution")
        if len(scenario_inputs) != len(execution_ids):
            raise ValueError("every execution must have one pinned scenario/input identity")
        for scenario_id, input_id, input_commitment in scenario_inputs:
            if scenario_id not in {f"S{i}" for i in range(1, 7)}:
                raise ValueError("scenario_inputs contains an unsupported scenario")
            _text(input_id, "input identity")
            if (not isinstance(input_commitment, str)
                    or not input_commitment.startswith("sha256:")):
                raise ValueError("input commitment must be a SHA-256 commitment")
        if random_seed is not None and type(random_seed) is not int:
            raise ValueError("random_seed must be an integer or None")
        if random_seed is None:
            _text(deterministic_order_commitment, "deterministic_order_commitment")
        if not oracle_content_commitment.startswith("sha256:"):
            raise ValueError("oracle_content_commitment must be a SHA-256 commitment")
        for name, value in (
            ("fixture_revision", fixture_revision), ("production_build", production_build),
            ("model", model), ("prompt", prompt), ("result_schema", result_schema),
            ("corpus_index", corpus_index),
            ("evaluation_config_reference", evaluation_config_reference),
            ("repository_commit", repository_commit),
            ("environment_identity", environment_identity),
            ("evaluator_rubric_version", evaluator_rubric_version),
            ("clock_source", clock_source),
            ("aggregation_policy_version", aggregation_policy_version),
        ):
            _text(value, name)
        if type(repository_dirty) is not bool:
            raise ValueError("repository_dirty must be boolean")
        for name, values in (
            ("provider_profile_references", provider_profile_references),
            ("rag_profile_references", rag_profile_references),
        ):
            if not isinstance(values, tuple) or not values:
                raise ValueError(f"{name} must be a non-empty tuple")
            for value in values:
                _text(value, name)
        if type(polling_interval_ms) is not int or polling_interval_ms <= 0:
            raise ValueError("polling_interval_ms must be positive")
        if type(timeout_ms) is not int or timeout_ms <= 0 or polling_interval_ms > timeout_ms:
            raise ValueError("timeout must be positive and at least the polling interval")
        if (type(clock_synchronization_uncertainty_ms) is not int
                or clock_synchronization_uncertainty_ms < 0):
            raise ValueError("clock synchronization uncertainty must be non-negative")
        config_commitment = commitment(dict(evaluation_config))
        approved_commitments = validate_reproducibility_commitments(
            approved_spec_commitments)
        payload = {
            "run_id": run_id, "execution_ids": list(execution_ids),
            "scenario_inputs": [list(item) for item in scenario_inputs],
            "random_seed": random_seed,
            "deterministic_order_commitment": deterministic_order_commitment,
            "evaluation_plan_id": evaluation_plan_id,
            "oracle_revision_id": oracle_revision_id,
            "oracle_content_commitment": oracle_content_commitment,
            "fixture_revision": fixture_revision, "production_build": production_build,
            "model": model, "prompt": prompt, "result_schema": result_schema,
            "corpus_index": corpus_index,
            "evaluation_config_reference": evaluation_config_reference,
            "evaluation_config_commitment": config_commitment,
            "repository_commit": repository_commit, "repository_dirty": repository_dirty,
            "provider_profile_references": list(provider_profile_references),
            "rag_profile_references": list(rag_profile_references),
            "environment_identity": environment_identity,
            "evaluator_rubric_version": evaluator_rubric_version,
            "polling_interval_ms": polling_interval_ms, "timeout_ms": timeout_ms,
            "clock_source": clock_source,
            "clock_synchronization_uncertainty_ms": clock_synchronization_uncertainty_ms,
            "aggregation_policy_version": aggregation_policy_version,
            "approved_spec_commitments": approved_commitments,
        }
        frozen = canonical_bytes(payload)
        return cls(
            run_id, execution_ids, scenario_inputs, random_seed,
            deterministic_order_commitment, evaluation_plan_id, oracle_revision_id,
            oracle_content_commitment, fixture_revision, production_build, model,
            prompt, result_schema, corpus_index, evaluation_config_reference,
            config_commitment, repository_commit, repository_dirty,
            provider_profile_references, rag_profile_references, environment_identity,
            evaluator_rubric_version, polling_interval_ms, timeout_ms, clock_source,
            clock_synchronization_uncertainty_ms, aggregation_policy_version,
            canonical_bytes(approved_commitments),
            frozen, commitment(payload),
        )

    def _payload(self) -> dict:
        return {
            "run_id": self.run_id, "execution_ids": list(self.execution_ids),
            "scenario_inputs": [list(item) for item in self.scenario_inputs],
            "random_seed": self.random_seed,
            "deterministic_order_commitment": self.deterministic_order_commitment,
            "evaluation_plan_id": self.evaluation_plan_id,
            "oracle_revision_id": self.oracle_revision_id,
            "oracle_content_commitment": self.oracle_content_commitment,
            "fixture_revision": self.fixture_revision,
            "production_build": self.production_build, "model": self.model,
            "prompt": self.prompt, "result_schema": self.result_schema,
            "corpus_index": self.corpus_index,
            "evaluation_config_reference": self.evaluation_config_reference,
            "evaluation_config_commitment": self.evaluation_config_commitment,
            "repository_commit": self.repository_commit,
            "repository_dirty": self.repository_dirty,
            "provider_profile_references": list(self.provider_profile_references),
            "rag_profile_references": list(self.rag_profile_references),
            "environment_identity": self.environment_identity,
            "evaluator_rubric_version": self.evaluator_rubric_version,
            "polling_interval_ms": self.polling_interval_ms,
            "timeout_ms": self.timeout_ms, "clock_source": self.clock_source,
            "clock_synchronization_uncertainty_ms": self.clock_synchronization_uncertainty_ms,
            "aggregation_policy_version": self.aggregation_policy_version,
            "approved_spec_commitments": self.approved_spec_commitments,
        }

    @property
    def approved_spec_commitments(self) -> dict[str, object]:
        """Return a detached view; immutable canonical bytes remain authoritative."""
        value = json.loads(self.approved_spec_commitments_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise ValueError("Approved §12 commitments are malformed")
        return value

    def validate(self) -> None:
        payload = json.loads(self.canonical_json)
        if canonical_bytes(payload) != self.canonical_json:
            raise ValueError("manifest is not canonical")
        if commitment(payload) != self.manifest_commitment:
            raise ValueError("manifest commitment mismatch")
        if payload != self._payload():
            raise ValueError("manifest fields disagree with canonical content")
        if self.random_seed is None and not self.deterministic_order_commitment:
            raise ValueError("explicit seed or deterministic no-seed order commitment required")
        if not self.clock_source:
            raise ValueError("clock facts are required")

    def validate_admission(self, plan: "EvaluationPlan", campaign: "CampaignPlan",
                           executions: tuple["PlannedExecution", ...] | None = None) -> None:
        self.validate()
        admitted = executions or campaign.executions
        expected_inputs = tuple(
            (item.scenario_id, item.input_identity, item.input_commitment) for item in admitted
        )
        checks = {
            "run identity": self.run_id == campaign.run_id,
            "execution identity/order": self.execution_ids == tuple(item.execution_id for item in admitted),
            "scenario/input identity/commitment": self.scenario_inputs == expected_inputs,
            "evaluation plan identity": self.evaluation_plan_id == plan.evaluation_plan_id == campaign.evaluation_plan_id,
            "Oracle revision": self.oracle_revision_id == plan.oracle_revision_id,
            "Oracle content commitment": self.oracle_content_commitment == plan.oracle_content_commitment,
            "fixture revision": self.fixture_revision == plan.fixture_revision,
            "production build": self.production_build == plan.production_build,
            "model": self.model == plan.model,
            "prompt": self.prompt == plan.prompt,
            "result schema": self.result_schema == plan.schema,
            "corpus/index": self.corpus_index == plan.corpus_index,
            "evaluation config reference": self.evaluation_config_reference == plan.configuration,
            "evaluation config commitment": self.evaluation_config_commitment == campaign.admission.evaluation_config_commitment,
            "repository commit": self.repository_commit == campaign.admission.repository_commit,
            "repository dirty state": self.repository_dirty == campaign.admission.repository_dirty,
            "provider profile references": self.provider_profile_references == campaign.admission.provider_profile_references,
            "RAG profile references": self.rag_profile_references == campaign.admission.rag_profile_references,
            "environment identity": self.environment_identity == campaign.admission.environment_identity,
            "evaluator/rubric": self.evaluator_rubric_version == campaign.admission.evaluator_rubric_version,
            "polling interval": self.polling_interval_ms == campaign.admission.polling_interval_ms,
            "timeout": self.timeout_ms == campaign.admission.timeout_ms,
            "clock source": self.clock_source == campaign.admission.clock_source,
            "clock uncertainty": self.clock_synchronization_uncertainty_ms == campaign.admission.clock_synchronization_uncertainty_ms,
            "random seed": self.random_seed == campaign.admission.random_seed,
            "deterministic order": self.deterministic_order_commitment == campaign.admission.deterministic_order_commitment,
            "required refresh cases": tuple(
                self.approved_spec_commitments["required_refresh_case_ids"]
            ) == campaign.required_refresh_case_ids,
            "required restart cases": tuple(
                self.approved_spec_commitments["required_restart_case_ids"]
            ) == campaign.required_restart_case_ids,
        }
        failed = tuple(name for name, valid in checks.items() if not valid)
        if failed:
            raise ValueError("manifest admission mismatch: " + ", ".join(failed))
