"""Deterministic Candidate-D validation through public A/B/C semantic reads."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json

from incident_evidence.contracts import EvidenceRevision, EvidenceSnapshot
from knowledge_index.contracts import (
    KnowledgeProvenanceProjection, KnowledgeReadStatus, KnowledgeSnapshotEnvelope,
    KnowledgeSnapshotKey, RetrievalApplicability, RetrievalResolution,
)
from rca_persistence.contracts import DiagnosticConclusion, EvidenceCompleteness, EvidentialSupport, GuidanceSource
from rca_shared.claim_types import ClaimCategory

from .config import GenerationConfig
from .contracts import (
    CausalRelation, CausalSupportProof, FailureClass, ResultContent,
    ValidatedGenerationResult, ValidationFact, safe_text,
)
from .identity import semantic_commitment
from .ports import AttemptPublicReader, EvidencePublicReader, KnowledgePublicReader
from .projection import project_artifact
from .schema import _FORBIDDEN_CONTEXT


class GenerationValidationError(ValueError):
    """Safe, typed deterministic validation rejection; never embeds provider text."""

    def __init__(self, failure_class: FailureClass, detail: str) -> None:
        if not isinstance(failure_class, FailureClass):
            raise TypeError("failure_class must be canonical")
        super().__init__(detail)
        self.failure_class = failure_class


@dataclass(frozen=True, slots=True)
class DegradedAuthorizationFact:
    """Caller-supplied governed-flow decision, bound to the exact pinned input."""

    reference: str
    attempt_id: str
    try_ordinal: int
    evidence_snapshot_id: str
    knowledge_snapshot_id: str
    authorized: bool
    trusted_evidence_sufficient: bool

    def __post_init__(self) -> None:
        for name in ("reference", "attempt_id", "evidence_snapshot_id", "knowledge_snapshot_id"):
            safe_text(getattr(self, name), name, 160, identifier=True)
        if type(self.try_ordinal) is not int or self.try_ordinal < 1:
            raise ValueError("degraded authorization Try ordinal is invalid")
        if type(self.authorized) is not bool or type(self.trusted_evidence_sufficient) is not bool:
            raise TypeError("degraded authorization decisions must be explicit booleans")


def _reject(kind: FailureClass, detail: str) -> None:
    raise GenerationValidationError(kind, detail)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _forbidden_context(value: object) -> bool:
    if isinstance(value, str):
        return bool(_FORBIDDEN_CONTEXT.search(value))
    if isinstance(value, dict):
        return any(_forbidden_context(key) or _forbidden_context(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(_forbidden_context(item) for item in value)
    return False


def evidence_fact_commitment(fact: object) -> str:
    """Version 1 commitment over one immutable public Snapshot fact."""

    return hashlib.sha256(("spec015-evidence-fact-v1\n" + _canonical(fact)).encode("utf-8")).hexdigest()


def observed_fact_text(fact: object) -> str:
    """Lossless fact rendering: entity/value/unit/time/polarity/source remain exact."""

    return _canonical(fact)


def _fact_at(snapshot: EvidenceSnapshot, path: str) -> object:
    if not isinstance(path, str) or not path.startswith("/"):
        _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence path is not canonical")
    parts = path[1:].split("/")
    if not parts or any(not part for part in parts):
        _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence path is not canonical")
    decoded: list[str] = []
    for part in parts:
        if "~" in part:
            index = 0
            while index < len(part):
                if part[index] == "~" and (index + 1 >= len(part) or part[index + 1] not in "01"):
                    _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence path escape is invalid")
                index += 2 if part[index] == "~" else 1
            part = part.replace("~1", "/").replace("~0", "~")
        decoded.append(part)
    if decoded[0] not in ("event_projections", "semantic_evidence"):
        _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence path is outside admitted facts")
    current: object = snapshot.snapshot_content
    for part in decoded:
        if isinstance(current, dict):
            if part not in current:
                _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence fact is absent")
            current = current[part]
        elif isinstance(current, list):
            if not part.isascii() or not part.isdecimal() or (len(part) > 1 and part[0] == "0") or int(part) >= len(current):
                _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence array address is invalid")
            current = current[int(part)]
        else:
            _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence path traverses a scalar")
    if isinstance(current, (dict, list)) and not current:
        _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence fact is empty")
    return current


def _presentation(content: ResultContent) -> None:
    claims = {item.claim_id: item for item in content.claims}

    def exact(text: str, ids: tuple[str, ...], field: str) -> None:
        if not ids or text != "; ".join(claims[item].text for item in ids):
            _reject(FailureClass.GROUNDING, f"{field} is not exactly covered by admitted claims")

    exact(content.summary, content.summary_claim_ids, "summary")
    exact(content.severity_assessment, content.severity_claim_ids, "severity")
    factual = {ClaimCategory.OBSERVED_FACT, ClaimCategory.ANALYTICAL_INFERENCE}
    if any(claims[item].category not in factual for item in (*content.summary_claim_ids, *content.severity_claim_ids)):
        _reject(FailureClass.GROUNDING, "factual presentation uses a guidance claim")
    for item in content.hypotheses:
        exact(item.statement, item.claim_ids, "hypothesis")
        exact(item.reasoning_summary, item.claim_ids, "reasoning")
        if any(claims[claim_id].category not in factual for claim_id in item.claim_ids):
            _reject(FailureClass.GROUNDING, "hypothesis uses a guidance claim")
        covered = tuple(claims[claim_id] for claim_id in item.claim_ids)
        if not set(item.supporting_evidence_ids) <= set().union(*(claim.supporting_evidence_ids for claim in covered)):
            _reject(FailureClass.GROUNDING, "hypothesis support exceeds covered claims")
        if not set(item.contradicting_evidence_ids) <= set().union(*(claim.contradicting_evidence_ids for claim in covered)):
            _reject(FailureClass.GROUNDING, "hypothesis contradiction exceeds covered claims")
    for item in (*content.remediation, *content.prevention):
        exact(item.description, item.claim_ids, "guidance")
        expected = ClaimCategory.KNOWLEDGE_BACKED_GUIDANCE if item.source is GuidanceSource.SOP_BACKED else ClaimCategory.MODEL_SUGGESTED_GUIDANCE
        if any(claims[claim_id].category is not expected for claim_id in item.claim_ids):
            _reject(FailureClass.KNOWLEDGE_AUTHORITY, "guidance claim category contradicts source")
        if set(item.knowledge_reference_ids) != set().union(*(claims[claim_id].knowledge_reference_ids for claim_id in item.claim_ids)):
            _reject(FailureClass.KNOWLEDGE_AUTHORITY, "guidance provenance contradicts covered claims")


def _calibration(
    content: ResultContent, config: GenerationConfig, evidence: EvidencePublicReader,
) -> None:
    """Admit identified causality only through exact D-generated proof and coverage."""

    inference = tuple(item for item in content.claims if item.category is ClaimCategory.ANALYTICAL_INFERENCE)
    for claim in inference:
        if any(term in claim.text.lower() for term in (
            "definitely", "proven root cause", "certainly", "必然", "已證實",
        )):
            _reject(FailureClass.GROUNDING, "analytical inference asserts unsupported certainty")
    proofs = {item.inference_claim_id: item for item in content.causal_proofs}
    for proof in content.causal_proofs:
        expected = derive_causal_support_proof(content, config, evidence, proof.inference_claim_id)
        if proof != expected:
            _reject(FailureClass.GROUNDING, "causal proof differs from exact deterministic rule result")
    if content.diagnostic_conclusion is DiagnosticConclusion.IDENTIFIED:
        if content.contract_version != "2" or config.version != "2.0":
            _reject(FailureClass.GROUNDING, "identified causality lacks a supported causal assertion")
        claims = {item.claim_id: item for item in content.claims}
        top = content.hypotheses[0]
        covered_inference = tuple(
            claims[claim_id] for claim_id in top.claim_ids
            if claims[claim_id].category is ClaimCategory.ANALYTICAL_INFERENCE
        )
        if len(covered_inference) != 1 or not top.supporting_evidence_ids:
            _reject(FailureClass.GROUNDING, "identified hypothesis needs one supported causal inference")
        admitted = covered_inference[0]
        assertion = admitted.causal_assertion
        if assertion is None or assertion.relation is not CausalRelation.CAUSES or admitted.claim_id not in proofs:
            _reject(FailureClass.GROUNDING, "identified inference lacks an admitted CAUSES proof")
        if (top.contradicting_evidence_ids or admitted.contradicting_evidence_ids
                or set(top.supporting_evidence_ids) != set(admitted.supporting_evidence_ids)):
            _reject(FailureClass.GROUNDING, "identified hypothesis has contradiction or unsupported coverage")
        cause = claims[assertion.cause_claim_ids[0]]
        effect = claims[assertion.effect_claim_ids[0]]
        if cause.contradicting_evidence_ids or effect.contradicting_evidence_ids:
            _reject(FailureClass.GROUNDING, "causal endpoint has represented contradiction")
        # Presentation is restricted to a deterministic rendering of the exact
        # proved endpoints. Prose is never read to establish causal authority.
        rendered = f"{cause.text} CAUSES {effect.text}"
        if admitted.text != rendered:
            _reject(FailureClass.GROUNDING, "identified inference presentation exceeds proved endpoints")
        presented_ids = set(content.summary_claim_ids + content.severity_claim_ids)
        presented_ids.update(claim_id for hypothesis in content.hypotheses for claim_id in hypothesis.claim_ids)
        for claim_id in presented_ids:
            item = claims[claim_id]
            if item.category is ClaimCategory.ANALYTICAL_INFERENCE and item.claim_id != admitted.claim_id:
                _reject(FailureClass.GROUNDING, "additional unproved analytical presentation is not identified")
    elif content.diagnostic_conclusion is DiagnosticConclusion.MOST_SUPPORTED:
        top = content.hypotheses[0]
        if (not inference or top.evidential_support is EvidentialSupport.LOW
                or (len(content.hypotheses) > 1 and top.evidential_support is content.hypotheses[1].evidential_support)):
            _reject(FailureClass.GROUNDING, "most-supported conclusion lacks a distinct supported hypothesis")


def _evidence(content: ResultContent, reader: EvidencePublicReader) -> tuple[EvidenceSnapshot, EvidenceRevision]:
    source = content.input
    try:
        snapshot = reader.resolve_snapshot(source.evidence_snapshot_id)
        revision = reader.resolve_revision(source.evidence_revision_id)
    except Exception as exc:
        raise GenerationValidationError(FailureClass.LOCAL_INTEGRITY, "Evidence authority is unreadable") from exc
    if not isinstance(snapshot, EvidenceSnapshot) or not isinstance(revision, EvidenceRevision):
        _reject(FailureClass.EVIDENCE_REFERENCE, "exact Evidence Snapshot or Revision is missing")
    if (snapshot.snapshot_id != source.evidence_snapshot_id or snapshot.revision_id != revision.revision_id
            or revision.revision_id != source.evidence_revision_id or snapshot.incident_id != revision.incident_id
            or snapshot.snapshot_content["semantic_evidence"] != revision.semantic_content):
        _reject(FailureClass.LOCAL_INTEGRITY, "Evidence Snapshot and Revision contradict")
    if content.evidence_completeness.value != snapshot.completeness.value:
        _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence completeness contradicts Snapshot")
    facts: dict[str, object] = {}
    for item in content.evidence_references:
        if item.evidence_snapshot_id != snapshot.snapshot_id or item.reference_schema_version != "1":
            _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence reference lineage or schema is unsupported")
        fact = _fact_at(snapshot, item.canonical_path)
        if evidence_fact_commitment(fact) != item.canonical_fact_commitment:
            _reject(FailureClass.EVIDENCE_REFERENCE, "Evidence fact commitment differs")
        facts[item.reference_id] = fact
    for claim in content.claims:
        if claim.category is ClaimCategory.OBSERVED_FACT:
            if claim.text != observed_fact_text(facts[claim.supporting_evidence_ids[0]]):
                _reject(FailureClass.GROUNDING, "Observed Fact rendering differs from immutable Evidence")
        elif claim.category is ClaimCategory.ANALYTICAL_INFERENCE:
            if not isinstance(claim.contradicting_evidence_ids, tuple) or set(claim.supporting_evidence_ids) & set(claim.contradicting_evidence_ids):
                _reject(FailureClass.GROUNDING, "inference support and contradiction structure is invalid")
    return snapshot, revision


CAUSAL_RULE_ID = "oom-downstream-cascade"
CAUSAL_RULE_VERSION = "1"


def _trusted_event_fact(snapshot: EvidenceSnapshot, path: str) -> dict[str, object]:
    if not path.startswith("/semantic_evidence/events/"):
        _reject(FailureClass.GROUNDING, "causal support must use an authoritative Event fact")
    fact = _fact_at(snapshot, path)
    if not isinstance(fact, dict) or set(fact) != {
        "event_id", "detected_at", "event_source", "event_type", "severity", "selector_values",
    }:
        _reject(FailureClass.GROUNDING, "causal Event structure is unsupported")
    if fact not in snapshot.snapshot_content["event_projections"]:
        _reject(FailureClass.GROUNDING, "causal Event differs from Snapshot projection")
    return fact


def _selectors(event: dict[str, object]) -> dict[str, str]:
    raw = event["selector_values"]
    if not isinstance(raw, list) or any(
        not isinstance(pair, list) or len(pair) != 2
        or not all(isinstance(value, str) and value for value in pair)
        for pair in raw
    ):
        _reject(FailureClass.GROUNDING, "causal Event selectors are malformed")
    selectors = dict(raw)
    if len(selectors) != len(raw):
        _reject(FailureClass.GROUNDING, "causal Event selectors conflict")
    return selectors


def _event_time(value: object) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError("not a timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed
    except ValueError:
        _reject(FailureClass.GROUNDING, "causal Event timestamp is invalid")


def derive_causal_support_proof(
    content: ResultContent,
    config: GenerationConfig,
    evidence: EvidencePublicReader,
    inference_claim_id: str,
) -> CausalSupportProof:
    """One bounded rule over public trusted Events; provider text never grants proof."""

    if not isinstance(content, ResultContent) or not isinstance(config, GenerationConfig):
        raise TypeError("typed result and pinned configuration are required")
    if (content.contract_version != "2" or config.version != "2.0"
            or content.input.pin != config.pin
            or config.causal_rule_policy_id != CAUSAL_RULE_ID
            or config.causal_rule_policy_version != CAUSAL_RULE_VERSION):
        _reject(FailureClass.RESULT_SCHEMA, "causal rule policy is not the exact pinned version")
    snapshot, _ = _evidence(content, evidence)
    if content.evidence_completeness is not EvidenceCompleteness.FULL:
        _reject(FailureClass.GROUNDING, "causal rule requires complete Evidence")
    omission = snapshot.snapshot_content["semantic_evidence"]["omission"]
    if not isinstance(omission, dict) or not omission or any(
        not isinstance(item, dict) or item.get("omitted_count") != 0
        or item.get("aggregation_lossy") is not False
        or item.get("truncation_applied") is not False
        for item in omission.values()
    ):
        _reject(FailureClass.GROUNDING, "causal rule cannot ignore omitted Evidence")
    claims = {item.claim_id: item for item in content.claims}
    inference = claims.get(inference_claim_id)
    if inference is None or inference.category is not ClaimCategory.ANALYTICAL_INFERENCE:
        _reject(FailureClass.GROUNDING, "causal proof inference is missing")
    assertion = inference.causal_assertion
    if (assertion is None or assertion.relation is not CausalRelation.CAUSES
            or len(assertion.cause_claim_ids) != 1 or len(assertion.effect_claim_ids) != 1):
        _reject(FailureClass.GROUNDING, "v1 causal rule requires one CAUSES Event pair")
    cause_claim = claims[assertion.cause_claim_ids[0]]
    effect_claim = claims[assertion.effect_claim_ids[0]]
    if (len(cause_claim.supporting_evidence_ids) != 1 or len(effect_claim.supporting_evidence_ids) != 1
            or set(inference.supporting_evidence_ids) != set(
                cause_claim.supporting_evidence_ids + effect_claim.supporting_evidence_ids
            )):
        _reject(FailureClass.GROUNDING, "causal inference support does not equal observed endpoints")
    references = {item.reference_id: item for item in content.evidence_references}
    cause_ref = references[cause_claim.supporting_evidence_ids[0]]
    effect_ref = references[effect_claim.supporting_evidence_ids[0]]
    cause = _trusted_event_fact(snapshot, cause_ref.canonical_path)
    effect = _trusted_event_fact(snapshot, effect_ref.canonical_path)
    cause_selectors = _selectors(cause)
    effect_selectors = _selectors(effect)
    if not (
        cause["event_source"] == effect["event_source"] == "log_event_detection"
        and cause["event_type"] == "oom_crash_detected"
        and effect["event_type"] == "downstream_cascade_failure"
        and cause["severity"] == effect["severity"] == "CRITICAL"
        and isinstance(cause["event_id"], str) and isinstance(effect["event_id"], str)
        and cause["event_id"] != effect["event_id"]
        and cause_selectors.get("service_name")
        and cause_selectors["service_name"] == effect_selectors.get("downstream_service")
        and effect_selectors.get("service_name") == "multiple"
        and _event_time(cause["detected_at"]) < _event_time(effect["detected_at"])
    ):
        _reject(FailureClass.GROUNDING, "structured Event dependency or failure sequence is absent")
    observed_ids = assertion.cause_claim_ids + assertion.effect_claim_ids
    payload = {
        "inference_claim_id": inference_claim_id,
        "assertion": asdict(assertion),
        "causal_rule_id": CAUSAL_RULE_ID,
        "causal_rule_version": CAUSAL_RULE_VERSION,
        "supporting_observed_claim_ids": observed_ids,
        "evidence_snapshot_id": snapshot.snapshot_id,
        "evidence_fact_commitments": (
            cause_ref.canonical_fact_commitment, effect_ref.canonical_fact_commitment,
        ),
    }
    commitment = hashlib.sha256(("spec015-causal-proof-v1\n" + _canonical(payload)).encode("utf-8")).hexdigest()
    return CausalSupportProof(
        "cproof_" + commitment, inference_claim_id, CAUSAL_RULE_ID,
        CAUSAL_RULE_VERSION, observed_ids, snapshot.snapshot_id, commitment,
    )


def _knowledge(content: ResultContent, reader: KnowledgePublicReader, authorization: DegradedAuthorizationFact | None, evidence: EvidenceSnapshot) -> None:
    source = content.input
    try:
        key = KnowledgeSnapshotKey(source.knowledge_snapshot_id)
        snapshot_read = reader.read_snapshot(key)
        provenance_read = reader.read_provenance(key)
    except Exception as exc:
        raise GenerationValidationError(FailureClass.LOCAL_INTEGRITY, "Knowledge authority is unreadable") from exc
    if snapshot_read.status is not KnowledgeReadStatus.FOUND or provenance_read.status is not KnowledgeReadStatus.FOUND:
        _reject(FailureClass.KNOWLEDGE_AUTHORITY, "Knowledge Snapshot or public provenance is unavailable")
    snapshot = snapshot_read.value
    provenance = provenance_read.value
    if not isinstance(snapshot, KnowledgeSnapshotEnvelope) or not isinstance(provenance, KnowledgeProvenanceProjection):
        _reject(FailureClass.KNOWLEDGE_AUTHORITY, "Knowledge public read type is invalid")
    if (snapshot.snapshot_key != key or provenance.snapshot_identity != key
            or snapshot.snapshot_commitment != provenance.snapshot_commitment
            or snapshot.schema_version != provenance.snapshot_schema_version
            or snapshot.resolution is not provenance.resolution
            or snapshot.resolution is not source.knowledge_resolution
            or snapshot.operation_key != provenance.retrieval_operation_identity
            or snapshot.frozen_build_identity != provenance.frozen_build_identity
            or snapshot.lineage_commitment != provenance.lineage_commitment
            or snapshot.staged_commitment != provenance.staged_commitment
            or snapshot.validation_commitment != provenance.validation_commitment
            or snapshot.artifact_commitment != provenance.artifact_commitment
            or snapshot.query_commitment != provenance.query_commitment):
        _reject(FailureClass.KNOWLEDGE_AUTHORITY, "Knowledge lineage or resolution contradicts pin")
    if content.knowledge_gap is not snapshot.knowledge_gap:
        _reject(FailureClass.KNOWLEDGE_AUTHORITY, "Knowledge gap contradicts Snapshot")
    if snapshot.resolution is RetrievalResolution.NO_MATCH:
        if content.knowledge_references or any(item.source is GuidanceSource.SOP_BACKED for item in (*content.remediation, *content.prevention)):
            _reject(FailureClass.KNOWLEDGE_AUTHORITY, "NO_MATCH cannot grant Knowledge provenance or SOP authority")
    elif snapshot.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE:
        if (content.knowledge_references or source.degraded_authorization_reference is None
                or authorization is None or authorization.reference != source.degraded_authorization_reference
                or authorization.attempt_id != source.try_identity.attempt_id
                or authorization.try_ordinal != source.try_identity.try_ordinal
                or authorization.evidence_snapshot_id != source.evidence_snapshot_id
                or authorization.knowledge_snapshot_id != source.knowledge_snapshot_id
                or authorization.authorized is not True or authorization.trusted_evidence_sufficient is not True
                or not evidence.snapshot_content["event_projections"]
                or not any(item.category is ClaimCategory.OBSERVED_FACT for item in content.claims)
                or any(item.source is GuidanceSource.SOP_BACKED for item in (*content.remediation, *content.prevention))):
            _reject(FailureClass.KNOWLEDGE_AUTHORITY, "evidence-only degraded continuation lacks exact governed authorization")
    elif snapshot.resolution is not RetrievalResolution.MATCH:
        _reject(FailureClass.KNOWLEDGE_AUTHORITY, "Knowledge resolution is invalid or repair-required")

    snapshot_chunks = {item.chunk_identity: item for item in snapshot.chunks}
    included = {item.chunk_identity: item for item in provenance.chunks if item.included}
    if len(snapshot_chunks) != len(snapshot.chunks) or set(snapshot_chunks) != set(included):
        _reject(FailureClass.KNOWLEDGE_AUTHORITY, "Knowledge Snapshot and included provenance differ")
    eligible: set[str] = set()
    for item in content.knowledge_references:
        chunk = included.get(item.chunk_id)
        snapshot_chunk = snapshot_chunks.get(item.chunk_id)
        if chunk is None or snapshot_chunk is None or (
            item.knowledge_snapshot_id != key.value
            or item.snapshot_commitment != provenance.snapshot_commitment
            or item.corpus_id != provenance.corpus_identity
            or item.build_id != provenance.frozen_build_identity
            or item.index_id != provenance.index_schema_identity
            or item.document_id != chunk.document_identity
            or item.document_version != chunk.document_version_identity
            or item.section_id != chunk.section_identity
            or item.content_commitment != chunk.content_commitment
            or item.metadata_commitment != chunk.metadata_commitment
            or snapshot_chunk.document_identity != chunk.document_identity
            or snapshot_chunk.document_version_identity != chunk.document_version_identity
            or snapshot_chunk.section_identity != chunk.section_identity
            or snapshot_chunk.content_commitment != chunk.content_commitment
            or snapshot_chunk.metadata_commitment != chunk.metadata_commitment
            or snapshot_chunk.applicability is not chunk.applicability
            or snapshot_chunk.knowledge_type != chunk.knowledge_type
            or snapshot_chunk.guidance_authority != chunk.guidance_authority
            or snapshot_chunk.sop_backed_eligible is not chunk.sop_backed_eligible
        ):
            _reject(FailureClass.KNOWLEDGE_AUTHORITY, "Knowledge reference differs from exact public provenance")
        if (chunk.sop_backed_eligible is True and chunk.guidance_authority == "SOP_BACKED_ELIGIBLE"
                and chunk.knowledge_type in ("SOP", "RUNBOOK")
                and chunk.applicability is not RetrievalApplicability.NONE
                and snapshot_chunk.content_truncated is False):
            eligible.add(item.reference_id)
    for claim in content.claims:
        if claim.category is ClaimCategory.KNOWLEDGE_BACKED_GUIDANCE and not set(claim.knowledge_reference_ids) <= eligible:
            _reject(FailureClass.KNOWLEDGE_AUTHORITY, "Knowledge-backed claim lacks eligible provenance")
    for item in (*content.remediation, *content.prevention):
        if item.source is GuidanceSource.SOP_BACKED and not set(item.knowledge_reference_ids) <= eligible:
            _reject(FailureClass.KNOWLEDGE_AUTHORITY, "SOP_BACKED guidance lacks eligible provenance")


def validate_generation_result(
    content: ResultContent,
    config: GenerationConfig,
    attempts: AttemptPublicReader,
    evidence: EvidencePublicReader,
    knowledge: KnowledgePublicReader,
    *,
    degraded_authorization: DegradedAuthorizationFact | None = None,
) -> ValidatedGenerationResult:
    """Admit only content grounded in the exact public A/B/C authority."""

    if not isinstance(content, ResultContent) or not isinstance(config, GenerationConfig):
        raise TypeError("immutable content and versioned configuration are required")
    if _forbidden_context(asdict(content)):
        _reject(FailureClass.INVALID_INPUT, "result contains forbidden evaluation context")
    source = content.input
    if (source.pin != config.pin or content.contract_version != source.pin.result_schema.version
            or (content.contract_version == "1" and config.version != "1.0")
            or (content.contract_version == "2" and config.version != "2.0")):
        _reject(FailureClass.RESULT_SCHEMA, "result contract or configuration pin differs")
    if len(content.hypotheses) > config.bounds.maximum_hypotheses or len(content.claims) > config.bounds.maximum_claims:
        _reject(FailureClass.RESULT_SCHEMA, "result exceeds versioned collection bounds")
    if (content.invocation_metadata.input_tokens > config.bounds.maximum_input_tokens
            or content.invocation_metadata.output_tokens > config.bounds.maximum_output_tokens
            or content.invocation_metadata.total_tokens > config.bounds.maximum_total_tokens):
        _reject(FailureClass.INVOCATION_BOUND, "invocation exceeds pinned token bound")
    try:
        view = attempts.get_attempt_lineage(source.try_identity.attempt_id)
    except Exception as exc:
        raise GenerationValidationError(FailureClass.LOCAL_INTEGRITY, "Attempt authority is unreadable") from exc
    if view is None:
        _reject(FailureClass.INVALID_INPUT, "pinned Attempt is missing")
    lineage = view.attempt.lineage
    if (lineage.attempt_id != source.try_identity.attempt_id
            or lineage.evidence_snapshot_id != source.evidence_snapshot_id
            or lineage.evidence_revision_id != source.evidence_revision_id
            or lineage.knowledge_snapshot_id != source.knowledge_snapshot_id
            or not source.pin.matches_attempt(lineage.generation_provenance)):
        _reject(FailureClass.IDENTITY_CONTRADICTION, "input does not match immutable Attempt lineage")
    snapshot, _ = _evidence(content, evidence)
    _knowledge(content, knowledge, degraded_authorization, snapshot)
    _presentation(content)
    _calibration(content, config, evidence)
    if content.validation_facts != (ValidationFact("schema", source.pin.result_schema.version, config.result_schema_commitment),):
        _reject(FailureClass.RESULT_SCHEMA, "schema validation fact contradicts pinned schema")
    facts = content.validation_facts + (
        ValidationFact("evidence", "1", semantic_commitment(content.evidence_references)),
        ValidationFact("knowledge", "1", semantic_commitment(content.knowledge_references)),
        ValidationFact("grounding", "1", semantic_commitment(content.claims)),
        ValidationFact("coverage", "1", semantic_commitment((content.summary_claim_ids, content.severity_claim_ids, tuple(item.claim_ids for item in content.hypotheses)))),
    )
    admitted = ValidatedGenerationResult.from_content(replace(content, validation_facts=facts))
    try:
        project_artifact(admitted)
    except (TypeError, ValueError) as exc:
        raise GenerationValidationError(FailureClass.RESULT_SCHEMA, "result cannot be losslessly projected") from exc
    return admitted
