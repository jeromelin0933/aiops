"""Secret-free evidence index derived solely from an authoritative run snapshot."""

from __future__ import annotations

from collections import Counter
from enum import Enum
import json

from .ledger import LedgerRecord
from .report_schema import (
    EVIDENCE_INDEX_SCHEMA_VERSION, EvidenceEntry, EvidenceIndex,
)
from .sqlite_store import (
    EvaluationLedgerReader, EvaluationRunSnapshot, SqliteEvaluationStore,
)


class EvidenceCategory(str, Enum):
    PREREGISTRATION = "PREREGISTRATION"
    INPUT = "INPUT"
    DOCKER_ENVIRONMENT = "DOCKER_ENVIRONMENT"
    SIGNAL = "SIGNAL"
    EVENT_BINDING = "EVENT_BINDING"
    INCIDENT_OBSERVATION = "INCIDENT_OBSERVATION"
    RCA_OBSERVATION = "RCA_OBSERVATION"
    LATENCY = "LATENCY"
    RESTART = "RESTART"
    GATE = "GATE"
    REVIEW = "REVIEW"
    SCORING = "SCORING"
    REPORT = "REPORT"


_SAFE_REFERENCE_FIELDS = frozenset({
    "kind", "scenario_id", "input_identity", "input_commitment",
    "manifest_commitment", "repository_commit", "oracle_content_commitment",
    "fixture_revision", "production_build", "profile_identity",
    "profile_commitment", "gate", "invocation_id", "start_id",
    "upstream_result_commitment", "corpus_index", "knowledge_build_id",
    "knowledge_build_commitment", "index_commitment", "namespace_id",
    "invocation_start_id", "final_scoring_commitment",
    "typed_claims_commitment", "blind_packet_commitment",
    "gate_contract_version", "evaluation_run_id", "lineage_commitment",
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


def _category(domain: str, payload: dict[str, object]) -> EvidenceCategory:
    kind = str(payload.get("kind") or "").upper()
    if domain == "evaluation_run":
        return EvidenceCategory.PREREGISTRATION
    if domain in {"execution", "rag_child_allocation"}:
        return EvidenceCategory.INPUT
    if domain in {"docker_namespace", "docker_cleanup"}:
        return (EvidenceCategory.RESTART if domain == "docker_cleanup"
                else EvidenceCategory.DOCKER_ENVIRONMENT)
    if domain.startswith("gate_"):
        return EvidenceCategory.GATE
    if domain == "review":
        return EvidenceCategory.REVIEW
    if domain in {"judgment", "re_adjudication"}:
        return EvidenceCategory.SCORING
    if domain == "report" and kind == "FORMAL_GATE_RECEIPT":
        return EvidenceCategory.GATE
    if domain == "report":
        return EvidenceCategory.REPORT
    if kind == "SIGNAL_STARTED":
        return EvidenceCategory.SIGNAL
    if "EVENT_BINDING" in kind:
        return EvidenceCategory.EVENT_BINDING
    if "INCIDENT" in kind:
        return EvidenceCategory.INCIDENT_OBSERVATION
    if "RCA" in kind or kind in {"FORMAL_OBSERVATION", "FINAL"}:
        return EvidenceCategory.RCA_OBSERVATION
    if "LATENCY" in kind or payload.get("latency_kind") is not None:
        return EvidenceCategory.LATENCY
    if "RESTART" in kind or "RECOVERY" in kind or "REFRESH" in kind:
        return EvidenceCategory.RESTART
    if ("SCORE" in kind or "CLAIM" in kind or "DEVIATION" in kind
            or "ISOLATION" in kind):
        return EvidenceCategory.SCORING
    return EvidenceCategory.RCA_OBSERVATION


def build_evidence_index(snapshot: EvaluationRunSnapshot) -> EvidenceIndex:
    """Index source records without copying arbitrary or secret-bearing payloads."""
    if not isinstance(snapshot, EvaluationRunSnapshot):
        raise TypeError("authoritative EvaluationRunSnapshot required")
    root_payload: dict[str, object] = {}
    entries = []
    for record in snapshot.records:
        payload = json.loads(record.semantic_json)
        if (record.domain == "report"
                and payload.get("kind") != "FORMAL_GATE_RECEIPT"):
            continue
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
            _category(record.domain, payload).value,
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


def append_evidence_index_commitment(
    store: SqliteEvaluationStore, index: EvidenceIndex,
) -> LedgerRecord:
    """Durably commit one immutable index cut in the sole Candidate-F ledger."""
    if type(store) is not SqliteEvaluationStore or type(index) is not EvidenceIndex:
        raise TypeError("Candidate-F store and EvidenceIndex required")
    store.require_formal_run(index.evaluation_run_id)
    current = build_evidence_index(
        store.reader().read_run_snapshot(index.evaluation_run_id))
    if current != index:
        raise ValueError("evidence index is not the current authoritative cut")
    counts = Counter(item.category for item in index.entries)
    payload = {
        "kind": "EVIDENCE_INDEX_COMMITMENT",
        "schema_version": index.schema_version,
        "evaluation_run_id": index.evaluation_run_id,
        "source_cut_commitment": index.source_cut_commitment,
        "evidence_index_commitment": index.evidence_index_commitment,
        "entry_count": len(index.entries),
        "category_counts": dict(sorted(counts.items())),
        "provenance": "Candidate-F authoritative run snapshot",
    }
    return store.append(LedgerRecord.create(
        "report", [index.evaluation_run_id, "EVIDENCE_INDEX",
                   index.source_cut_commitment], payload,
        index.evaluation_run_id,
    ))


def evidence_index_commitment_is_current(
    reader: EvaluationLedgerReader, receipt_id: str,
) -> bool:
    """Fresh-read verification; a report projection is never its own authority."""
    if type(reader) is not EvaluationLedgerReader:
        raise TypeError("store-issued EvaluationLedgerReader required")
    receipt = reader.get(receipt_id)
    if receipt is None or receipt.domain != "report":
        return False
    payload = json.loads(receipt.semantic_json)
    if payload.get("kind") != "EVIDENCE_INDEX_COMMITMENT":
        return False
    run_id = payload.get("evaluation_run_id")
    if not isinstance(run_id, str):
        return False
    current = build_evidence_index(reader.read_run_snapshot(run_id))
    return (
        payload.get("schema_version") == current.schema_version
        and payload.get("source_cut_commitment") == current.source_cut_commitment
        and payload.get("evidence_index_commitment") ==
            current.evidence_index_commitment
        and payload.get("entry_count") == len(current.entries)
        and payload.get("category_counts") == dict(sorted(Counter(
            item.category for item in current.entries).items()))
    )
