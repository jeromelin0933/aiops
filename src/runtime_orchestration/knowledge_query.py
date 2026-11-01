"""Bounded public Evidence facts -> Candidate-C query; no SOP lookup or inference."""
from __future__ import annotations

from dataclasses import replace
import json
import re

from incident_evidence import EvidenceSnapshot
from knowledge_index import (CanonicalKnowledgeQuery, KnowledgeReadStatus,
    OpaqueExternalReference, OpaqueReferenceType, RetrievalOperationRequest)


MAPPING_VERSION = "incident-evidence-query-v1"
# Existing SPEC-013 admitted detector vocabulary, not a fault/root-cause map.
_EVENT_TYPES = {
    "log_event_detection": frozenset(("oom_crash_detected", "downstream_cascade_failure",
        "brute_force_detected", "rate_limit_storm", "external_dependency_failure",
        "cross_service_failure", "general_log_anomaly")),
    "metrics_threshold_detection": frozenset(("high_memory_detected", "high_latency_detected")),
    "metrics_iforest_detection": frozenset(("request_spike_detected", "general_metrics_anomaly")),
}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_EXCLUDED = re.compile(r"(?i)(scenario|oracle|ground.?truth|expected|validator|secret|password|api.?key|bearer)")


def query_from_evidence(incident_id: str, snapshot: EvidenceSnapshot) -> CanonicalKnowledgeQuery:
    """Use only captured Event observations. Unsupported scope filters stay absent.

    The four applicability scopes are not fields of TrustedEvent. No approved
    event-to-scope crosswalk is provisioned; service_name is NOT a component
    scope and detector labels are NOT proven failure causes.
    """
    if (not isinstance(snapshot, EvidenceSnapshot) or snapshot.incident_id != incident_id
            or snapshot.snapshot_content['incident_projection'].get('incident_id') != incident_id):
        raise ValueError("query requires the exact public Incident Evidence Snapshot")
    facts = set()
    for event in snapshot.snapshot_content['event_projections']:
        if not isinstance(event, dict):
            raise ValueError("public Event projection is malformed")
        source, kind = event.get('event_source'), event.get('event_type')
        if (not isinstance(source, str) or not isinstance(kind, str)
                or kind not in _EVENT_TYPES.get(source, ())):
            continue
        # Only this explicit field allowlist enters the query. No raw logs,
        # triggered_features, event/scenario IDs, messages or oracle fields.
        severity = event.get('severity')
        severity = severity if severity in ('LOW','MEDIUM','HIGH','CRITICAL') else ''
        selectors = event.get('selector_values', [])
        if not isinstance(selectors, (list, tuple)):
            selectors = []
        services = sorted({pair[1] for pair in selectors
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == 'service_name'
            and isinstance(pair[1], str) and _NAME.fullmatch(pair[1]) and not _EXCLUDED.search(pair[1])})
        # Conflicting selector values are not resolved by guessing one service.
        service = services[0] if len(services) == 1 else ''
        facts.add((source, kind, severity, service))
    ordered = sorted(facts)
    payload = {'observed_event_facts': [dict(zip(
        ('event_source','event_type','severity','service_name'), fact)) for fact in ordered[:8]],
        'additional_distinct_event_facts': max(0, len(ordered)-8)}
    text = f"Observed incident event facts ({MAPPING_VERSION}): " + json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return CanonicalKnowledgeQuery('1.0', '1.0', text, ())


def bind_evidence_query(request: RetrievalOperationRequest, incident_id: str,
                        snapshot: EvidenceSnapshot, prior: object) -> RetrievalOperationRequest:
    """Preserve exact frozen operations, including legacy Incident-ID queries.

    No C state is written here. A new operation uses the mapped query and public
    evidence/version references; arbitrary frozen contradictions fail closed.
    """
    query = query_from_evidence(incident_id, snapshot)
    references = tuple(sorted(set(request.external_references + (
        OpaqueExternalReference(OpaqueReferenceType.CALLER, MAPPING_VERSION),
        OpaqueExternalReference(OpaqueReferenceType.EVIDENCE_SNAPSHOT, snapshot.snapshot_id),
    )), key=lambda ref:(ref.reference_type.value, ref.value)))
    mapped = replace(request, query=query, external_references=references)
    if prior.status is KnowledgeReadStatus.UNAVAILABLE:
        raise OSError("public Knowledge operation is unavailable")
    if prior.status is KnowledgeReadStatus.FOUND:
        frozen = prior.value.frozen.request
        if (frozen == request and request.query == CanonicalKnowledgeQuery('1.0','1.0',incident_id)):
            return request  # Replay old authority; never remap an already frozen operation.
        if frozen != mapped:
            raise ValueError("mapped query contradicts the frozen Knowledge operation")
    elif prior.status is not KnowledgeReadStatus.NOT_FOUND:
        raise ValueError("public Knowledge operation requires repair")
    return mapped
