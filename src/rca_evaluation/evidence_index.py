"""Secret-free evidence index derived solely from an authoritative run snapshot."""

from __future__ import annotations

import json

from .report_schema import (
    EVIDENCE_INDEX_SCHEMA_VERSION, EvidenceEntry, EvidenceIndex,
)
from .sqlite_store import EvaluationRunSnapshot


_SAFE_REFERENCE_FIELDS = frozenset({
    "kind", "scenario_id", "input_identity", "input_commitment",
    "manifest_commitment", "repository_commit", "oracle_content_commitment",
    "fixture_revision", "production_build", "profile_identity",
    "profile_commitment", "gate", "invocation_id", "start_id",
    "upstream_result_commitment", "corpus_index", "knowledge_build_id",
    "knowledge_build_commitment", "index_commitment", "namespace_id",
    "invocation_start_id", "final_scoring_commitment",
    "typed_claims_commitment", "blind_packet_commitment",
})
_SAFE_INVOCATION_PIN_FIELDS = frozenset({
    "provider", "model", "credential_profile_identity", "prompt_identity",
    "prompt_version", "schema_identity", "schema_version",
    "configuration_identity", "configuration_version", "manifest_commitment",
    "release_reference", "embedding_provider", "embedding_model",
    "embedding_profile_identity", "retrieval_profile_identity", "index_engine",
    "index_schema_identity", "retrieval_operation_identity",
    "active_build_identity", "activation_generation",
    "activation_operation_identity", "validated_build_commitment",
    "manifest_identity", "corpus_identity", "corpus_version",
    "embedding_dimension", "artifact_commitment", "lineage_commitment",
    "staged_commitment", "compose_commitment", "repository_commit",
    "project_identity", "workspace_identity", "runtime_service",
    "runtime_entrypoint", "runtime_config", "namespace_identity",
})


def _safe_text(value: object) -> str | None:
    if isinstance(value, (str, int, bool)):
        text = str(value)
        if len(text) <= 512:
            return text
    return None


def _safe_invocation_pins(value: object, prefix: str = "invocation_pins") -> tuple[
        tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    result = []
    for name in sorted(value):
        item = value[name]
        if name == "expected_build_context":
            result.extend(_safe_invocation_pins(item, f"{prefix}.{name}"))
        elif name in _SAFE_INVOCATION_PIN_FIELDS:
            safe = _safe_text(item)
            if safe is not None:
                result.append((f"{prefix}.{name}", safe))
    return tuple(result)


def build_evidence_index(snapshot: EvaluationRunSnapshot) -> EvidenceIndex:
    """Index source records without copying arbitrary or secret-bearing payloads."""
    if not isinstance(snapshot, EvaluationRunSnapshot):
        raise TypeError("authoritative EvaluationRunSnapshot required")
    root_payload: dict[str, object] = {}
    entries = []
    for record in snapshot.records:
        if record.domain == "report":
            continue
        payload = json.loads(record.semantic_json)
        if record.domain == "evaluation_run":
            root_payload = payload
        references = []
        for name in sorted(_SAFE_REFERENCE_FIELDS & payload.keys()):
            safe = _safe_text(payload[name])
            if safe is not None:
                references.append((name, safe))
        references.extend(_safe_invocation_pins(payload.get("invocation_pins")))
        entries.append(EvidenceEntry(
            record.domain, record.record_id, record.parent_id,
            record.semantic_commitment,
            str(payload.get("kind") or record.domain).upper(),
            tuple(references),
        ))
    return EvidenceIndex(
        EVIDENCE_INDEX_SCHEMA_VERSION,
        snapshot.evaluation_run_id,
        snapshot.evaluation_plan_id,
        snapshot.oracle_revision_id,
        _safe_text(root_payload.get("oracle_content_commitment")),
        _safe_text(root_payload.get("repository_commit")),
        _safe_text(root_payload.get("fixture_revision")),
        _safe_text(root_payload.get("manifest_commitment")),
        snapshot.report_source_cut_commitment,
        tuple(entries),
    )
