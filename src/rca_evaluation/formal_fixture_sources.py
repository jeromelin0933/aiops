"""Evaluation-only source/ground-truth split for the three R2 formal fixtures."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .identity import canonical_text_bytes, canonical_text_commitment, commitment
from .oracle import OfficialOracleArtifact


SOURCE_SCHEMA = "spec017.r2-missing-formal-fixtures.v1"
SOURCE_ARTIFACT_COMMITMENT = (
    "sha256:51d2f72fe0a665bc9c6edf31d2444c0f13313b6a971e0121a6fe83a0d566a5f8"
)
CASE_KEYS = ("s1_degraded", "s2_failure", "s6_contradiction")
CASE_IDENTITIES = (
    "spec017:S1:degraded:v1",
    "spec017:S2:failure:v1",
    "spec017:S6:contradiction:v1",
)
_EXPECTED_CASES = {
    "s1_degraded": ("S1", "CREDENTIAL_ABUSE_BRUTE_FORCE", "DEGRADED"),
    "s2_failure": ("S2", "DATABASE_LATENCY_CASCADE", "FAILURE"),
    "s6_contradiction": ("S6", "REQUEST_SURGE_RATE_LIMIT", "REFRESH"),
}
_GROUND_TRUTH_FIELDS = frozenset({
    "expected_causal_class", "expected_evidence", "forbidden_overclaim",
    "forbidden_overclaim_authority", "accepted_alternative",
    "accepted_alternative_authority", "expected_knowledge_behavior",
    "ground_truth", "ground_truth_label", "oracle_predicates", "denominator",
    "expected_disposition", "expected_failure_class",
    "expected_retry_disposition",
})


def _contains_ground_truth(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).strip().lower().replace("-", "_") in _GROUND_TRUTH_FIELDS
            or _contains_ground_truth(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_ground_truth(item) for item in value)
    return False


@dataclass(frozen=True, slots=True)
class FormalFixtureSource:
    fixture_identity: str
    fixture_revision: str
    oracle_scenario_reference: str
    source_commitment: str
    production_input: dict[str, object]
    input_commitment: str
    evaluation_truth: dict[str, object]

    def production_payload(self) -> dict[str, object]:
        """Return only injection controls; evaluation truth has no production path."""
        return json.loads(json.dumps(self.production_input, ensure_ascii=False))


@dataclass(frozen=True, slots=True)
class FormalFixtureSourceArtifact:
    artifact_commitment: str
    oracle_revision_id: str
    oracle_content_commitment: str
    cases: tuple[FormalFixtureSource, ...]

    def resolve(self, fixture_identity: str) -> FormalFixtureSource:
        matches = tuple(case for case in self.cases
                        if case.fixture_identity == fixture_identity)
        if len(matches) != 1:
            raise ValueError("formal fixture source is absent or ambiguous")
        return matches[0]


def load_formal_fixture_source_artifact(
    path: str | Path, *, oracle: OfficialOracleArtifact,
) -> FormalFixtureSourceArtifact:
    raw_bytes = Path(path).read_bytes()
    actual = canonical_text_commitment(raw_bytes)
    if actual != SOURCE_ARTIFACT_COMMITMENT:
        raise ValueError("formal fixture source artifact commitment mismatch")
    raw = json.loads(canonical_text_bytes(raw_bytes))
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version", "fixture_set_identity", "fixture_set_revision",
        "classification", "authority_references", "oracle_binding", "cases",
    }:
        raise ValueError("formal fixture source artifact schema is invalid")
    if (raw["schema_version"] != SOURCE_SCHEMA
            or raw["fixture_set_identity"]
                != "spec017-r2-missing-formal-fixtures"
            or raw["fixture_set_revision"] != "v1"
            or raw["classification"] != "EVALUATION ONLY"
            or raw["authority_references"] != [
                "SPEC-017-v1.0", "PM Formal Evaluation delegation",
                "PM-SPEC017-EOL-COMMITMENT-20261015",
            ]):
        raise ValueError("formal fixture source authority is invalid")
    binding = raw["oracle_binding"]
    if binding != {
        "artifact_revision": oracle.artifact_revision,
        "oracle_revision_id": oracle.revision.oracle_revision_id,
        "semantic_content_commitment":
            oracle.revision.semantic_content_commitment,
    }:
        raise ValueError("formal fixture source Oracle binding mismatch")
    if not isinstance(raw["cases"], dict) or tuple(raw["cases"]) != CASE_KEYS:
        raise ValueError("formal fixture source inventory is invalid")
    oracle_semantics = json.loads(oracle.revision.semantic_json)["scenarios"]
    cases = []
    for key, expected_identity in zip(CASE_KEYS, CASE_IDENTITIES):
        item = raw["cases"][key]
        if not isinstance(item, dict) or set(item) != {
            "fixture_identity", "fixture_revision", "oracle_scenario_reference",
            "production_input", "evaluation_truth",
        }:
            raise ValueError("formal fixture case schema is invalid")
        scenario, causal_class, denominator = _EXPECTED_CASES[key]
        truth = item["evaluation_truth"]
        production = item["production_input"]
        if (item["fixture_identity"] != expected_identity
                or item["fixture_revision"] != "v1"
                or item["oracle_scenario_reference"]
                    != f"{oracle.artifact_revision}:{scenario}"):
            raise ValueError("formal fixture case identity or Oracle reference is invalid")
        if not isinstance(production, dict) or _contains_ground_truth(production):
            raise ValueError("formal fixture production input leaks Ground Truth")
        if (not isinstance(truth, dict)
                or truth.get("expected_causal_class") != causal_class
                or truth.get("expected_causal_class")
                    != oracle_semantics[scenario]["O1"]["causal_class"]
                or truth.get("denominator") != denominator
                or not truth.get("oracle_predicates")
                or truth.get("forbidden_overclaim_authority")
                    != f"{oracle.artifact_revision}:{scenario}:O4"
                or truth.get("accepted_alternative_authority")
                    != f"{oracle.artifact_revision}:{scenario}:O3"):
            raise ValueError("formal fixture evaluation truth contradicts the Oracle")
        cases.append(FormalFixtureSource(
            item["fixture_identity"], item["fixture_revision"],
            item["oracle_scenario_reference"], actual,
            production, commitment(production), truth,
        ))
    return FormalFixtureSourceArtifact(
        actual, oracle.revision.oracle_revision_id,
        oracle.revision.semantic_content_commitment, tuple(cases),
    )
