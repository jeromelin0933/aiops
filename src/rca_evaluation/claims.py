"""Complete UCR evaluation derived from SPEC-015 authoritative typed claims."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from llm_generation.contracts import Claim
from rca_shared.claim_types import ClaimCategory

from .identity import commitment


class ClaimDisposition(str, Enum):
    SUPPORTED = "SUPPORTED"
    GROUNDED = "SUPPORTED"  # Compatibility alias for the initial S3 slice name.
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ClaimEvaluationStatus(str, Enum):
    SCORED = "SCORED"
    NO_ELIGIBLE_CLAIMS = "NO_ELIGIBLE_CLAIMS"
    INVALID_OUTPUT = "INVALID_OUTPUT"


def production_claim_commitment(claim: Claim) -> str:
    if type(claim) is not Claim:
        raise TypeError("SPEC-015 Claim authority is required")
    causal = None
    if claim.causal_assertion is not None:
        causal = {
            "cause_claim_ids": list(claim.causal_assertion.cause_claim_ids),
            "effect_claim_ids": list(claim.causal_assertion.effect_claim_ids),
            "relation": claim.causal_assertion.relation.value,
        }
    return commitment({
        "claim_id": claim.claim_id,
        "category": claim.category.value,
        "text": claim.text,
        "supporting_evidence_ids": list(claim.supporting_evidence_ids),
        "contradicting_evidence_ids": list(claim.contradicting_evidence_ids),
        "knowledge_reference_ids": list(claim.knowledge_reference_ids),
        "evidential_support": (
            claim.evidential_support.value if claim.evidential_support is not None else None
        ),
        "causal_assertion": causal,
    })


@dataclass(frozen=True, slots=True)
class GovernedClaimDecomposition:
    production_claim_id: str
    production_claim_commitment: str
    atomic_commitments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.production_claim_id or not self.production_claim_commitment:
            raise ValueError("claim decomposition requires exact production claim authority")
        if (not self.atomic_commitments
                or any(not isinstance(value, str) or not value for value in self.atomic_commitments)
                or len(set(self.atomic_commitments)) != len(self.atomic_commitments)):
            raise ValueError("governed decomposition must contain unique atomic commitments")


@dataclass(frozen=True, slots=True)
class AtomicClaimFinding:
    production_claim_id: str
    atomic_commitment: str
    disposition: ClaimDisposition

    def __post_init__(self) -> None:
        if not self.production_claim_id or not self.atomic_commitment:
            raise ValueError("atomic finding requires exact claim lineage")
        if not isinstance(self.disposition, ClaimDisposition):
            raise TypeError("claim disposition must be closed")


@dataclass(frozen=True, slots=True)
class UnsupportedClaimResult:
    status: ClaimEvaluationStatus
    numerator: int
    denominator: int
    contradicted_count: int
    rate: float | None
    unique_findings: tuple[AtomicClaimFinding, ...]
    integrity_errors: tuple[str, ...] = ()

    @property
    def result_commitment(self) -> str:
        return commitment({
            "status": self.status.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "contradicted_count": self.contradicted_count,
            "rate_fraction": None if self.rate is None else [self.numerator, self.denominator],
            "findings": [{
                "production_claim_id": item.production_claim_id,
                "atomic_commitment": item.atomic_commitment,
                "disposition": item.disposition.value,
            } for item in self.unique_findings],
            "integrity_errors": list(self.integrity_errors),
        })


_ELIGIBLE_CATEGORIES = frozenset({
    ClaimCategory.OBSERVED_FACT,
    ClaimCategory.ANALYTICAL_INFERENCE,
})
_GUIDANCE_CATEGORIES = frozenset({
    ClaimCategory.KNOWLEDGE_BACKED_GUIDANCE,
    ClaimCategory.MODEL_SUGGESTED_GUIDANCE,
})


def _invalid(*errors: str) -> UnsupportedClaimResult:
    return UnsupportedClaimResult(
        ClaimEvaluationStatus.INVALID_OUTPUT, 0, 0, 0, None, (), tuple(sorted(set(errors))),
    )


def evaluate_unsupported_claims(
    production_claims: tuple[Claim, ...],
    decomposition: tuple[GovernedClaimDecomposition, ...],
    production_factual_assertion_commitments: tuple[str, ...],
    atomic_findings: tuple[AtomicClaimFinding, ...],
) -> UnsupportedClaimResult:
    """Require a complete, exact disposition for every authoritative eligible atomic unit."""
    if any(type(item) is not Claim for item in production_claims):
        raise TypeError("production_claims must be exact SPEC-015 Claim values")
    claims = {item.claim_id: item for item in production_claims}
    if len(claims) != len(production_claims):
        return _invalid("DUPLICATE_PRODUCTION_CLAIM_ID")
    decompositions = {item.production_claim_id: item for item in decomposition}
    if len(decompositions) != len(decomposition):
        return _invalid("DUPLICATE_DECOMPOSITION")
    if set(decompositions) != set(claims):
        return _invalid("INCOMPLETE_CLAIM_DECOMPOSITION")

    eligible_pairs: set[tuple[str, str]] = set()
    ineligible_pairs: set[tuple[str, str]] = set()
    eligible_semantics: set[str] = set()
    for claim_id, claim in claims.items():
        item = decompositions[claim_id]
        if item.production_claim_commitment != production_claim_commitment(claim):
            return _invalid("PRODUCTION_CLAIM_COMMITMENT_MISMATCH")
        if claim.category in _ELIGIBLE_CATEGORIES:
            for atomic in item.atomic_commitments:
                eligible_pairs.add((claim_id, atomic))
                eligible_semantics.add(atomic)
        elif claim.category not in _GUIDANCE_CATEGORIES:
            return _invalid("UNSUPPORTED_TYPED_CLAIM_CATEGORY")
        else:
            for atomic in item.atomic_commitments:
                ineligible_pairs.add((claim_id, atomic))

    factual_assertions = set(production_factual_assertion_commitments)
    if len(factual_assertions) != len(production_factual_assertion_commitments):
        return _invalid("DUPLICATE_PRODUCTION_ASSERTION")
    if factual_assertions != eligible_semantics:
        return _invalid("TYPED_CLAIM_COVERAGE_MISMATCH")

    findings: dict[tuple[str, str], AtomicClaimFinding] = {}
    for finding in atomic_findings:
        key = (finding.production_claim_id, finding.atomic_commitment)
        if key in ineligible_pairs:
            if finding.disposition is not ClaimDisposition.NOT_APPLICABLE:
                return _invalid("INELIGIBLE_ATOM_MUST_BE_NOT_APPLICABLE")
        elif key not in eligible_pairs:
            return _invalid("UNRELATED_ATOMIC_FINDING")
        elif finding.disposition is ClaimDisposition.NOT_APPLICABLE:
            return _invalid("ELIGIBLE_ATOM_CANNOT_BE_NOT_APPLICABLE")
        if key in findings:
            return _invalid("DUPLICATE_ATOMIC_FINDING")
        findings[key] = finding
    if set(findings) != eligible_pairs | ineligible_pairs:
        return _invalid("MISSING_ATOMIC_FINDING")

    by_semantic: dict[str, AtomicClaimFinding] = {}
    for key in sorted(eligible_pairs):
        finding = findings[key]
        existing = by_semantic.get(finding.atomic_commitment)
        if existing is not None and existing.disposition is not finding.disposition:
            return _invalid("CONTRADICTORY_DUPLICATE_SEMANTIC_UNIT")
        by_semantic.setdefault(finding.atomic_commitment, finding)
    unique = tuple(by_semantic[key] for key in sorted(by_semantic))
    eligible = unique
    if not eligible:
        return UnsupportedClaimResult(
            ClaimEvaluationStatus.NO_ELIGIBLE_CLAIMS, 0, 0, 0, None, unique,
        )
    contradicted = sum(item.disposition is ClaimDisposition.CONTRADICTED for item in eligible)
    numerator = sum(
        item.disposition in {ClaimDisposition.UNSUPPORTED, ClaimDisposition.CONTRADICTED}
        for item in eligible
    )
    denominator = len(eligible)
    return UnsupportedClaimResult(
        ClaimEvaluationStatus.SCORED, numerator, denominator, contradicted,
        numerator / denominator, unique,
    )
