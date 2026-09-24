"""Direct, deterministic Candidate-B Materiality evaluation."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import (
    EvidenceRevision,
    MaterialityEvaluationKind,
    MaterialityJudgement,
    MaterialityRequest,
    MaterialityResult,
)
from .errors import EvidenceDomainError, EvidenceFailureKind
from .identity import semantic_identity
from .sqlite_store import SqliteEvidenceStore


MATERIALITY_RULE_V1 = "evidence-materiality-v1"
DEFAULT_MATERIALITY_RULE_VERSIONS = frozenset({MATERIALITY_RULE_V1})

# These facts affect capture lineage and coverage, but not the evidence meaning
# used by the v1 material-evidence decision when every other semantic fact is equal.
_V1_NON_MATERIAL_FIELDS = frozenset({"omission", "collection_boundaries"})


class MaterialityEvaluator:
    """Evaluate only the explicit Revision pair supplied by the caller."""

    def __init__(
        self,
        store: SqliteEvidenceStore,
        *,
        supported_rule_versions: Iterable[str] = DEFAULT_MATERIALITY_RULE_VERSIONS,
    ) -> None:
        if not isinstance(store, SqliteEvidenceStore):
            raise TypeError("store must be a SqliteEvidenceStore")
        versions = frozenset(supported_rule_versions)
        if not versions or any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in versions
        ):
            raise ValueError("supported_rule_versions must contain explicit identities")
        if not versions <= DEFAULT_MATERIALITY_RULE_VERSIONS:
            raise ValueError("supported_rule_versions contains an unimplemented rule")
        self._store = store
        self._supported_rule_versions = versions

    def compare_materiality(self, request: MaterialityRequest) -> MaterialityResult:
        if not isinstance(request, MaterialityRequest):
            raise TypeError("request must be a MaterialityRequest")
        if request.materiality_rule_version not in self._supported_rule_versions:
            raise EvidenceDomainError(
                EvidenceFailureKind.UNSUPPORTED_MATERIALITY_RULE,
                "requested Materiality rule version is unavailable",
                field_path="materiality_rule_version",
            )

        # A persisted answer is authoritative. Reading it also revalidates its
        # Revision lineage and prevents process-memory-dependent replay.
        existing = self._store.read_materiality_result_for_request(request)
        if existing is not None:
            return existing

        candidate = self._require_revision(
            request.candidate_revision_id, field_path="candidate_revision_id"
        )
        if request.evaluation_kind is MaterialityEvaluationKind.NO_BASELINE:
            return self._commit(
                request,
                None,
                ("explicit NO_BASELINE initial evaluation",),
            )

        # MaterialityRequest enforces this for PAIRWISE; retain the explicit
        # assertion here so the evaluator never turns a null into baseline selection.
        if request.baseline_revision_id is None:  # pragma: no cover - contract guard
            raise AssertionError("PAIRWISE request has no baseline Revision")
        baseline = self._require_revision(
            request.baseline_revision_id, field_path="baseline_revision_id"
        )
        if baseline.incident_id != candidate.incident_id:
            raise EvidenceDomainError(
                EvidenceFailureKind.MATERIALITY_REPAIR_REQUIRED,
                "Materiality pair belongs to different Incidents",
            )

        judgement, facts = self._evaluate_v1(baseline, candidate)
        return self._commit(request, judgement, facts)

    evaluate = compare_materiality

    def _require_revision(self, revision_id: str, *, field_path: str) -> EvidenceRevision:
        revision = self._store.resolve_revision(revision_id)
        if revision is None:
            raise EvidenceDomainError(
                EvidenceFailureKind.DANGLING_EVIDENCE_REFERENCE,
                "required Evidence Revision is missing",
                field_path=field_path,
            )
        return revision

    @staticmethod
    def _evaluate_v1(
        baseline: EvidenceRevision, candidate: EvidenceRevision
    ) -> tuple[MaterialityJudgement, tuple[str, ...]]:
        if baseline.canonicalization_version != candidate.canonicalization_version:
            return (
                MaterialityJudgement.REPAIR_REQUIRED,
                ("Revision canonicalization versions are incompatible",),
            )

        baseline_content = baseline.semantic_content
        candidate_content = candidate.semantic_content
        if baseline_content == candidate_content:
            return (
                MaterialityJudgement.SAME,
                ("canonical semantic evidence is identical",),
            )

        changed = tuple(
            sorted(
                key
                for key in set(baseline_content) | set(candidate_content)
                if baseline_content.get(key) != candidate_content.get(key)
            )
        )
        if changed and set(changed) <= _V1_NON_MATERIAL_FIELDS:
            return (
                MaterialityJudgement.NON_MATERIAL,
                ("only collection boundary or omission facts differ",),
            )
        return (
            MaterialityJudgement.MATERIAL,
            ("material semantic fields differ: " + ", ".join(changed),),
        )

    def _commit(
        self,
        request: MaterialityRequest,
        judgement: MaterialityJudgement | None,
        reason_facts: tuple[str, ...],
    ) -> MaterialityResult:
        result = MaterialityResult(
            semantic_identity("spec013-materiality-result", request),
            request,
            judgement,
            reason_facts,
        )
        return self._store.commit_materiality_result(result)


def compare_materiality(
    store: SqliteEvidenceStore,
    request: MaterialityRequest,
    *,
    supported_rule_versions: Iterable[str] = DEFAULT_MATERIALITY_RULE_VERSIONS,
) -> MaterialityResult:
    """Public functional seam for Candidate E composition."""

    return MaterialityEvaluator(
        store, supported_rule_versions=supported_rule_versions
    ).compare_materiality(request)


__all__ = [
    "DEFAULT_MATERIALITY_RULE_VERSIONS",
    "MATERIALITY_RULE_V1",
    "MaterialityEvaluator",
    "compare_materiality",
]
