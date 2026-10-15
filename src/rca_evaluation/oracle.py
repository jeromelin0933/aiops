"""Immutable Oracle revisions and two-stage evaluation admission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .identity import canonical_bytes, commitment, identity, require_identity
from .authority import PM_AUTHORITY


SCENARIOS = tuple(f"S{i}" for i in range(1, 7))
ORACLE_FIELDS = tuple(f"O{i}" for i in range(1, 11))
SCHEMA_VERSION = "1"
OFFICIAL_ORACLE_ARTIFACT_SCHEMA = "spec017.oracle-artifact.v1"
APPROVED_SPEC_COMMIT = "7996d39242d9aeabbfc481532a89afa530a12088"
APPROVED_SPEC_DOCUMENT = "docs/spec/SPEC-017_RCA_Evaluation_E2E_Validation.md"
APPROVED_SPEC_SECTION = "§7"


def _reference(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _validate_semantics(content: object) -> None:
    if not isinstance(content, dict) or set(content) != {"scenarios", "cross_scenario_rules"}:
        raise ValueError("Oracle must contain scenarios and cross_scenario_rules")
    scenarios = content["scenarios"]
    if not isinstance(scenarios, dict) or set(scenarios) != set(SCENARIOS):
        raise ValueError("Oracle must contain exactly S1 through S6")
    for scenario in scenarios.values():
        if not isinstance(scenario, dict) or set(scenario) != set(ORACLE_FIELDS):
            raise ValueError("every scenario must contain exactly O1 through O10")
        if any(value is None or value == "" for value in scenario.values()):
            raise ValueError("Oracle fields must have content")
    rules = content["cross_scenario_rules"]
    if not isinstance(rules, (dict, list)) or not rules:
        raise ValueError("cross-scenario rules must have content")


@dataclass(frozen=True, slots=True)
class OracleRevision:
    oracle_set_id: str
    oracle_revision_id: str
    parent_revision_id: str | None
    schema_version: str
    semantic_content_commitment: str
    semantic_json: bytes
    created_at: str
    status: str
    approval_authority: str
    approval_reference: str
    approved_at: str

    @classmethod
    def approved(cls, *, oracle_set_id: str, semantics: dict, created_at: str,
                 approval_authority: str, approval_reference: str, approved_at: str,
                 parent_revision_id: str | None = None) -> "OracleRevision":
        _validate_semantics(semantics)
        _reference(oracle_set_id, "oracle_set_id")
        if parent_revision_id is not None:
            require_identity(parent_revision_id, "oracle_revision")
        for name, value in (("created_at", created_at), ("approved_at", approved_at),
                            ("approval_reference", approval_reference)):
            _reference(value, name)
        if approval_authority != PM_AUTHORITY:
            raise ValueError("Oracle approval authority must be PM")
        frozen = canonical_bytes(semantics)
        digest = commitment([SCHEMA_VERSION, json.loads(frozen)])
        revision_id = identity("oracle_revision", [oracle_set_id, SCHEMA_VERSION, digest])
        result = cls(oracle_set_id, revision_id, parent_revision_id, SCHEMA_VERSION,
                     digest, frozen, created_at, "APPROVED", approval_authority,
                     approval_reference, approved_at)
        result.validate()
        return result

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.status != "APPROVED":
            raise ValueError("unsupported or unapproved Oracle revision")
        _reference(self.oracle_set_id, "oracle_set_id")
        if self.approval_authority != PM_AUTHORITY:
            raise ValueError("Oracle approval authority must be PM")
        for name in ("created_at", "approved_at", "approval_reference"):
            _reference(getattr(self, name), name)
        if self.parent_revision_id is not None:
            require_identity(self.parent_revision_id, "oracle_revision")
        semantics = json.loads(self.semantic_json)
        _validate_semantics(semantics)
        if canonical_bytes(semantics) != self.semantic_json:
            raise ValueError("noncanonical Oracle content")
        digest = commitment([self.schema_version, semantics])
        if digest != self.semantic_content_commitment or self.oracle_revision_id != identity(
                "oracle_revision", [self.oracle_set_id, self.schema_version, digest]):
            raise ValueError("Oracle content commitment mismatch")


@dataclass(frozen=True, slots=True)
class OfficialOracleArtifact:
    """Versioned evaluation-only materialization of Approved SPEC-017 §7."""

    artifact_schema: str
    artifact_revision: str
    source_document: str
    source_section: str
    approved_commit: str
    artifact_commitment: str
    revision: OracleRevision


def load_official_oracle(path: str | Path) -> OfficialOracleArtifact:
    """Load the official sidecar and derive the existing S1 OracleRevision.

    The JSON file is data, never a production import.  Its commitment covers
    metadata and semantics; Oracle admission remains owned by the existing
    ``SqliteEvaluationStore`` flow.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "artifact_schema", "artifact_revision", "oracle_set_id", "source",
        "approval", "semantics",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("official Oracle artifact schema is invalid")
    source = raw["source"]
    approval = raw["approval"]
    if source != {
        "document": APPROVED_SPEC_DOCUMENT,
        "section": APPROVED_SPEC_SECTION,
        "approved_commit": APPROVED_SPEC_COMMIT,
    }:
        raise ValueError("official Oracle source does not identify Approved SPEC-017 §7")
    if raw["artifact_schema"] != OFFICIAL_ORACLE_ARTIFACT_SCHEMA:
        raise ValueError("unsupported official Oracle artifact schema")
    if raw["artifact_revision"] != "spec017-s1-s6-v1":
        raise ValueError("unsupported official Oracle artifact revision")
    if not isinstance(approval, dict) or set(approval) != {
        "authority", "reference", "approved_at", "created_at",
    }:
        raise ValueError("official Oracle approval metadata is invalid")
    revision = OracleRevision.approved(
        oracle_set_id=raw["oracle_set_id"], semantics=raw["semantics"],
        created_at=approval["created_at"],
        approval_authority=approval["authority"],
        approval_reference=approval["reference"],
        approved_at=approval["approved_at"],
    )
    return OfficialOracleArtifact(
        raw["artifact_schema"], raw["artifact_revision"], source["document"],
        source["section"], source["approved_commit"], commitment(raw), revision,
    )


@dataclass(frozen=True, slots=True)
class EvaluationPlan:
    evaluation_plan_id: str
    oracle_revision_id: str
    oracle_content_commitment: str
    oracle_approval_reference: str
    fixture_revision: str
    production_build: str
    model: str
    prompt: str
    schema: str
    corpus_index: str
    configuration: str
    repetitions: int

    @classmethod
    def admit(cls, oracle: OracleRevision, *, fixture_revision: str, production_build: str,
              model: str, prompt: str, schema: str, corpus_index: str,
              configuration: str, repetitions: int) -> "EvaluationPlan":
        oracle.validate()
        fields = (fixture_revision, production_build, model, prompt, schema, corpus_index, configuration)
        for name, value in zip(("fixture_revision", "production_build", "model", "prompt", "schema", "corpus_index", "configuration"), fields):
            _reference(value, name)
        if type(repetitions) is not int or repetitions <= 0:
            raise ValueError("repetitions must be a positive integer")
        key = [oracle.oracle_revision_id, oracle.semantic_content_commitment,
               oracle.approval_reference, *fields, repetitions]
        return cls(identity("evaluation_plan", key), oracle.oracle_revision_id,
                   oracle.semantic_content_commitment, oracle.approval_reference,
                   *fields, repetitions)

    def validate(self, oracle: OracleRevision) -> None:
        expected = self.admit(oracle, fixture_revision=self.fixture_revision,
                              production_build=self.production_build, model=self.model,
                              prompt=self.prompt, schema=self.schema, corpus_index=self.corpus_index,
                              configuration=self.configuration, repetitions=self.repetitions)
        if self != expected:
            raise ValueError("plan does not pin exact admitted Oracle and inputs")
