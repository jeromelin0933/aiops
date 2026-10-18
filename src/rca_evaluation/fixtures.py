"""Evaluation-only fixture manifest admission; never production scenario truth."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

import yaml

from .identity import canonical_text_bytes, canonical_text_commitment, commitment
from .oracle import OfficialOracleArtifact, SCENARIOS


REQUIRED_VARIANTS = frozenset({
    "nominal", "degraded", "failure", "material_refresh", "refresh_failure",
    "non_material",
    "restart_boundary", "contradiction",
})
REQUIRED_VARIANT_INVENTORY = frozenset({
    ("S1", "degraded"),
    ("S2", "failure"),
    ("S2", "material_refresh"),
    ("S2", "refresh_failure"),
    ("S2", "non_material"),
    ("S5", "restart_boundary"),
    ("S6", "contradiction"),
})
REQUIRED_RESTART_BOUNDARIES = (
    "EVIDENCE", "KNOWLEDGE", "GENERATION", "ARTIFACT_COMMIT", "PUBLICATION",
)
OFFICIAL_FIXTURE_MANIFEST_IDENTITY = "spec017-s1-s6-fixtures-v2"
OFFICIAL_FIXTURE_REVISION = "spec017-fixtures-v2"
OFFICIAL_FIXTURE_MANIFEST_COMMITMENT = (
    "sha256:43919ec3b36bcc5a5e67348fa99bf3a5b2c77af3785bcefb9ca6da1ef29a43a0"
)

_VERIFIED_LOADER_PROVENANCE = object()
_FIXTURE_ADMISSION_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class FixtureEntry:
    fixture_identity: str
    fixture_revision: str
    scenario_id: str
    variant: str
    source_reference: str | None
    source_commitment: str | None
    input_identity: str | None
    input_commitment: str | None
    deterministic_order_basis: str
    expected_public_observation_class: str
    oracle_scenario_reference: str
    isolation_canary_commitment: str
    support_status: str
    missing_reason: str | None
    recovery_boundary: str | None = None
    evaluation_classification: str | None = None
    pm_approval_reference: str | None = None
    baseline_input_identity: str | None = None
    baseline_input_commitment: str | None = None
    candidate_input_identity: str | None = None
    candidate_input_commitment: str | None = None
    materiality_rule_version: str | None = None
    control_reference: str | None = None
    control_commitment: str | None = None
    canonical_artifact_commitment: str | None = None


@dataclass(frozen=True, slots=True)
class OfficialFixtureManifest:
    schema_version: str
    manifest_identity: str
    fixture_revision: str
    source_oracle_artifact_revision: str
    source_oracle_revision_id: str
    source_oracle_content_commitment: str
    manifest_commitment: str
    entries: tuple[FixtureEntry, ...]


@dataclass(frozen=True, slots=True)
class FixtureAdmissionRequest:
    scenario_id: str
    variant: str
    input_identity: str
    input_commitment: str
    oracle_scenario_reference: str
    recovery_boundary: str | None = None
    fixture_identity: str | None = None
    fixture_revision: str | None = None


def _manifest_payload(manifest: OfficialFixtureManifest) -> dict:
    return {
        "schema_version": manifest.schema_version,
        "manifest_identity": manifest.manifest_identity,
        "fixture_revision": manifest.fixture_revision,
        "source_oracle_artifact_revision":
            manifest.source_oracle_artifact_revision,
        "source_oracle_revision_id": manifest.source_oracle_revision_id,
        "source_oracle_content_commitment":
            manifest.source_oracle_content_commitment,
        "entries": [
            {name: value for name, value in asdict(item).items()
             if (value is not None or name in {
                 "source_reference", "source_commitment", "input_identity",
                 "input_commitment", "missing_reason",
             })}
            for item in manifest.entries
        ],
    }


def _validate_canonical_official_manifest(
    manifest: OfficialFixtureManifest, oracle: OfficialOracleArtifact,
) -> None:
    if (
        manifest.manifest_identity != OFFICIAL_FIXTURE_MANIFEST_IDENTITY
        or manifest.fixture_revision != OFFICIAL_FIXTURE_REVISION
        or manifest.manifest_commitment
            != OFFICIAL_FIXTURE_MANIFEST_COMMITMENT
        or commitment(_manifest_payload(manifest))
            != OFFICIAL_FIXTURE_MANIFEST_COMMITMENT
        or manifest.source_oracle_artifact_revision
            != oracle.artifact_revision
        or manifest.source_oracle_revision_id
            != oracle.revision.oracle_revision_id
        or manifest.source_oracle_content_commitment
            != oracle.revision.semantic_content_commitment
    ):
        raise ValueError("fixture artifact is not the canonical official manifest")


@dataclass(frozen=True, slots=True, init=False)
class AdmittedFixture:
    """Resolver-owned proof that one exact official fixture is executable."""

    manifest_identity: str
    manifest_commitment: str
    fixture_revision: str
    oracle_revision_id: str
    oracle_content_commitment: str
    fixture_identity: str
    source_fixture_revision: str
    scenario_id: str
    variant: str
    input_identity: str
    input_commitment: str
    oracle_scenario_reference: str
    expected_public_observation_class: str
    recovery_boundary: str | None
    evaluation_classification: str | None
    pm_approval_reference: str | None
    baseline_input_identity: str | None
    baseline_input_commitment: str | None
    candidate_input_identity: str | None
    candidate_input_commitment: str | None
    materiality_rule_version: str | None
    control_reference: str | None
    control_commitment: str | None
    canonical_artifact_commitment: str | None
    _authority: object

    @classmethod
    def _admitted(
        cls, manifest: OfficialFixtureManifest, entry: FixtureEntry,
    ) -> "AdmittedFixture":
        value = object.__new__(cls)
        facts = (
            ("manifest_identity", manifest.manifest_identity),
            ("manifest_commitment", manifest.manifest_commitment),
            ("fixture_revision", manifest.fixture_revision),
            ("oracle_revision_id", manifest.source_oracle_revision_id),
            ("oracle_content_commitment",
             manifest.source_oracle_content_commitment),
            ("fixture_identity", entry.fixture_identity),
            ("source_fixture_revision", entry.fixture_revision),
            ("scenario_id", entry.scenario_id),
            ("variant", entry.variant),
            ("input_identity", entry.input_identity),
            ("input_commitment", entry.input_commitment),
            ("oracle_scenario_reference", entry.oracle_scenario_reference),
            ("expected_public_observation_class",
             entry.expected_public_observation_class),
            ("recovery_boundary", entry.recovery_boundary),
            ("evaluation_classification", entry.evaluation_classification),
            ("pm_approval_reference", entry.pm_approval_reference),
            ("baseline_input_identity", entry.baseline_input_identity),
            ("baseline_input_commitment", entry.baseline_input_commitment),
            ("candidate_input_identity", entry.candidate_input_identity),
            ("candidate_input_commitment", entry.candidate_input_commitment),
            ("materiality_rule_version", entry.materiality_rule_version),
            ("control_reference", entry.control_reference),
            ("control_commitment", entry.control_commitment),
            ("canonical_artifact_commitment",
             entry.canonical_artifact_commitment),
            ("_authority", _FIXTURE_ADMISSION_AUTHORITY),
        )
        for name, item in facts:
            object.__setattr__(value, name, item)
        return value


class VerifiedFixtureResolver:
    """Loader-issued verifier over one canonical official fixture artifact."""

    __slots__ = ("_manifest", "_oracle_revision_id",
                 "_oracle_content_commitment", "_provenance")

    def __init__(
        self, manifest: OfficialFixtureManifest, oracle: OfficialOracleArtifact,
        *, _provenance: object,
    ) -> None:
        if _provenance is not _VERIFIED_LOADER_PROVENANCE:
            raise TypeError("verified fixture resolver is issued only by the official loader")
        _validate_canonical_official_manifest(manifest, oracle)
        self._manifest = manifest
        self._oracle_revision_id = oracle.revision.oracle_revision_id
        self._oracle_content_commitment = (
            oracle.revision.semantic_content_commitment)
        self._provenance = _provenance

    @property
    def manifest(self) -> OfficialFixtureManifest:
        return self._manifest

    @property
    def manifest_identity(self) -> str:
        return self._manifest.manifest_identity

    @property
    def manifest_commitment(self) -> str:
        return self._manifest.manifest_commitment

    @property
    def fixture_revision(self) -> str:
        return self._manifest.fixture_revision

    @property
    def entries(self) -> tuple[FixtureEntry, ...]:
        return self._manifest.entries

    def admit(self, request: FixtureAdmissionRequest) -> AdmittedFixture:
        if self._provenance is not _VERIFIED_LOADER_PROVENANCE:
            raise TypeError("fixture resolver provenance is invalid")
        if type(request) is not FixtureAdmissionRequest:
            raise TypeError("fixture admission requires a non-authoritative request")
        if (
            self._manifest.source_oracle_revision_id
                != self._oracle_revision_id
            or self._manifest.source_oracle_content_commitment
                != self._oracle_content_commitment
        ):
            raise ValueError("verified fixture resolver Oracle binding drifted")
        matches = tuple(
            item for item in self._manifest.entries
            if item.scenario_id == request.scenario_id
            and item.variant == request.variant
            and item.recovery_boundary == request.recovery_boundary
        )
        if len(matches) != 1:
            raise ValueError("fixture variant is absent or ambiguous")
        entry = matches[0]
        if entry.support_status != "SUPPORTED":
            raise ValueError("fixture variant is not supported for formal admission")
        if (
            entry.input_identity != request.input_identity
            or entry.input_commitment != request.input_commitment
            or entry.oracle_scenario_reference
                != request.oracle_scenario_reference
            or (request.fixture_identity is not None
                and entry.fixture_identity != request.fixture_identity)
            or (request.fixture_revision is not None
                and entry.fixture_revision != request.fixture_revision)
        ):
            raise ValueError("fixture admission facts do not match the official manifest")
        if (entry.variant in {"degraded", "failure", "contradiction"}
                and (request.fixture_identity is None
                     or request.fixture_revision is None)):
            raise ValueError("R2 formal fixture admission requires exact identity and revision")
        return AdmittedFixture._admitted(self._manifest, entry)


def require_verified_fixture_resolver(value: object) -> VerifiedFixtureResolver:
    if (
        type(value) is not VerifiedFixtureResolver
        or value._provenance is not _VERIFIED_LOADER_PROVENANCE
    ):
        raise TypeError("formal campaign requires the official fixture resolver")
    return value


def load_fixture_manifest(
    path: str | Path, *, oracle: OfficialOracleArtifact,
    repository_root: str | Path | None = None,
) -> VerifiedFixtureResolver:
    if type(oracle) is not OfficialOracleArtifact:
        raise TypeError("fixture manifest admission requires the official Oracle artifact")
    oracle.revision.validate()
    manifest_path = Path(path)
    manifest_bytes = canonical_text_bytes(manifest_path.read_bytes())
    raw = json.loads(manifest_bytes)
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version", "manifest_identity", "fixture_revision",
        "source_oracle_artifact_revision", "source_oracle_revision_id",
        "source_oracle_content_commitment", "entries",
    }:
        raise ValueError("fixture manifest schema is invalid")
    if raw["schema_version"] != "spec017.fixture-manifest.v2":
        raise ValueError("unsupported fixture manifest schema")
    if (
        raw["source_oracle_artifact_revision"] != oracle.artifact_revision
        or raw["source_oracle_revision_id"]
            != oracle.revision.oracle_revision_id
        or raw["source_oracle_content_commitment"]
            != oracle.revision.semantic_content_commitment
    ):
        raise ValueError("fixture manifest Oracle commitment mismatch")
    entries = tuple(FixtureEntry(**item) for item in raw["entries"])
    if not entries or len({item.fixture_identity for item in entries}) != len(entries):
        raise ValueError("fixture identities must be non-empty and unique")
    if {item.scenario_id for item in entries} != set(SCENARIOS):
        raise ValueError("fixture manifest must reference S1 through S6")
    if not REQUIRED_VARIANTS <= {item.variant for item in entries}:
        raise ValueError("fixture manifest does not cover required variant classes")
    inventory = {(item.scenario_id, item.variant) for item in entries}
    if not REQUIRED_VARIANT_INVENTORY <= inventory:
        raise ValueError("fixture manifest does not cover the approved variant inventory")
    restart_boundaries = tuple(
        item.recovery_boundary for item in entries
        if item.scenario_id == "S5" and item.variant == "restart_boundary"
    )
    if (len(restart_boundaries) != len(REQUIRED_RESTART_BOUNDARIES)
            or set(restart_boundaries) != set(REQUIRED_RESTART_BOUNDARIES)):
        raise ValueError("fixture manifest does not cover every approved restart boundary")
    for item in entries:
        if item.oracle_scenario_reference != (
            f"spec017-s1-s6-v1:{item.scenario_id}"
        ):
            raise ValueError("fixture Oracle scenario reference is invalid")
        if not item.isolation_canary_commitment.startswith("sha256:"):
            raise ValueError("fixture isolation canary commitment is invalid")
        if ((item.variant == "restart_boundary")
                != (item.recovery_boundary in REQUIRED_RESTART_BOUNDARIES)):
            raise ValueError("fixture restart boundary identity is invalid")
        if item.support_status == "SUPPORTED":
            required = (
                item.source_reference, item.source_commitment,
                item.input_identity, item.input_commitment,
            )
            if any(not value for value in required) or item.missing_reason is not None:
                raise ValueError("supported fixture requires complete source/input commitments")
            is_recovery = item.variant in {
                "material_refresh", "refresh_failure", "non_material",
                "restart_boundary"}
            is_r2_formal_source = item.variant in {
                "degraded", "failure", "contradiction"}
            if is_recovery:
                if (item.evaluation_classification != "EVALUATION ONLY"
                        or item.pm_approval_reference
                            != "PM-SPEC017-R2S4-20261013"
                        or item.canonical_artifact_commitment
                            != item.source_commitment):
                    raise ValueError(
                        "recovery fixture lacks evaluation-only PM authority binding")
                if item.variant in {
                    "material_refresh", "refresh_failure", "non_material"}:
                    pair = (
                        item.baseline_input_identity,
                        item.baseline_input_commitment,
                        item.candidate_input_identity,
                        item.candidate_input_commitment,
                        item.materiality_rule_version,
                    )
                    if (any(not value for value in pair)
                            or item.baseline_input_identity
                                == item.candidate_input_identity
                            or item.baseline_input_commitment
                                == item.candidate_input_commitment
                            or item.materiality_rule_version
                                != "evidence-materiality-v1"):
                        raise ValueError(
                            "refresh fixture requires a distinct governed Evidence pair")
                else:
                    if (not item.control_reference
                            or not item.control_commitment):
                        raise ValueError(
                            "restart fixture requires a governed boundary control")
            if is_r2_formal_source:
                if (item.evaluation_classification != "EVALUATION ONLY"
                        or item.pm_approval_reference
                            != "PM Formal Evaluation delegation"
                        or item.canonical_artifact_commitment
                            != item.source_commitment
                        or not item.control_reference
                        or item.control_commitment != item.input_commitment):
                    raise ValueError(
                        "R2 formal fixture lacks evaluation-only authority binding")
            if repository_root is not None:
                source_path, separator, selector = item.source_reference.partition("#")
                if not separator:
                    raise ValueError("fixture source selector is invalid")
                resolved = (Path(repository_root) / source_path).resolve()
                root = Path(repository_root).resolve()
                if resolved != root and root not in resolved.parents:
                    raise ValueError("fixture source escapes repository root")
                content = resolved.read_bytes()
                canonical_content = canonical_text_bytes(content)
                actual_source = canonical_text_commitment(content)
                if actual_source != item.source_commitment:
                    raise ValueError("fixture source commitment mismatch")
                document = yaml.safe_load(canonical_content)
                if is_recovery or is_r2_formal_source:
                    cursor = document
                    for component in selector.split("."):
                        if not isinstance(cursor, dict) or component not in cursor:
                            raise ValueError("fixture source selector is invalid")
                        cursor = cursor[component]
                    scenario = cursor
                else:
                    if selector != f"scenarios.{item.scenario_id}":
                        raise ValueError("fixture source selector is invalid")
                    scenario = document["scenarios"][item.scenario_id]
                canonical = json.dumps(
                    scenario, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                actual_input = "sha256:" + hashlib.sha256(canonical).hexdigest()
                if actual_input != item.input_commitment:
                    raise ValueError("fixture input commitment mismatch")
        elif item.support_status == "MISSING_EXISTING_INPUT_SOURCE":
            if item.missing_reason is None:
                raise ValueError("missing fixture source requires an explicit reason")
        else:
            raise ValueError("fixture support status is invalid")
    manifest = OfficialFixtureManifest(
        raw["schema_version"], raw["manifest_identity"], raw["fixture_revision"],
        raw["source_oracle_artifact_revision"], raw["source_oracle_revision_id"],
        raw["source_oracle_content_commitment"], commitment(raw), entries,
    )
    return VerifiedFixtureResolver(
        manifest, oracle, _provenance=_VERIFIED_LOADER_PROVENANCE)
