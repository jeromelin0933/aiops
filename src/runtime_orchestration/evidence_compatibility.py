"""Read-only D3 compatibility inspection, not a SPEC-013 wire-contract extension.

No result here is a SelectorFact OR, a GLOBAL Evidence projection, an approval,
or a source collection result. Production request construction remains pending
the exact SPEC-013 amendment. No source request or store initialization occurs.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from incident_evidence.contracts import CaptureCommand, EvidenceSource, SelectorFact, SourceAdapterRequest
from incident_evidence.errors import EvidenceDomainError, EvidenceFailureKind
from incident_evidence.security import build_selector_fact
from incident_evidence.trusted_core import admit_capture_plan
from incident_evidence.identity import canonical_json


MEMBERSHIP_POLICY_CANDIDATE = "spec013-demo-membership-service-selector-v1"
MAX_DISTINCT_SERVICES = 8
TRACE_PROFILE_CANDIDATE = "loki-demo-structured-trace-v1"
TRACE_QUERY_VERSION_CANDIDATE = "logql-json-trace-v1"


def _commit(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _invalid(message: str, field: str) -> EvidenceDomainError:
    return EvidenceDomainError(EvidenceFailureKind.SOURCE_INVALID, message, field_path=field)


@dataclass(frozen=True)
class MembershipInspection:
    """Inspection facts only. Event references use commitments, not invented revisions."""
    incident_id: str
    incident_content_commitment: str
    event_references: tuple[tuple[str, str], ...]
    service_facts: tuple[SelectorFact, ...]
    dependency_fact: SelectorFact
    policy_candidate: str = MEMBERSHIP_POLICY_CANDIDATE
    request_status: str = "PENDING SPEC-013 BOUNDED OR REQUEST CONTRACT"


def inspect_incident_membership(command: CaptureCommand, incident_reader, event_reader, policy) -> MembershipInspection:
    """Reuse public trusted-core admission and its consistency/identity checks.

    No inference from raw_log_sample, detector labels, scenario or generator
    internals. Every required member must independently provide concrete service
    and dependency facts. Incomplete aggregate coverage is not quietly dropped.
    """
    plan = admit_capture_plan(command, incident_reader, event_reader, policy)
    services = set()
    dependencies = set()
    references = []
    for event in plan.events:
        values = dict(event.selector_values)
        service, dependency = values.get("service_name"), values.get("downstream_service")
        if not service or service == "multiple":
            raise _invalid("membership lacks a concrete service identity", "service_name")
        if not dependency or dependency == "multiple":
            raise _invalid("membership lacks a concrete dependency identity", "downstream_service")
        services.add(service)
        dependencies.add(dependency)
        references.append((event.event_id, _commit(event)))
        if len(services) > MAX_DISTINCT_SERVICES:
            raise _invalid("membership exceeds eight distinct services", "service_name")
    if not services or len(dependencies) != 1:
        raise _invalid("dependency facts are missing or conflicting", "downstream_service")
    facts = tuple(build_selector_fact(
        EvidenceSource.LOKI, "service_name", service,
        selector_policy_version=policy.selector_policy_version,
        allowlist=policy.selector_allowlist,
    ) for service in sorted(services))
    dependency = build_selector_fact(
        EvidenceSource.LOKI, "downstream_service", next(iter(dependencies)),
        selector_policy_version=policy.selector_policy_version,
        allowlist=policy.selector_allowlist,
    )
    return MembershipInspection(plan.incident.incident_id, _commit(plan.incident),
                                tuple(sorted(references)), facts, dependency)


def inspect_structured_trace(request: SourceAdapterRequest, selector_allowlist) -> tuple[str, ...]:
    """Validate candidate facts but never certify parser coverage from empty data."""
    if (request.source is not EvidenceSource.LOKI or request.query_resource != "streams"
            or request.query_semantic_identity != TRACE_PROFILE_CANDIDATE
            or request.query_version != TRACE_QUERY_VERSION_CANDIDATE):
        raise _invalid("incompatible structured trace profile", "query_semantic_identity")
    traces, concrete = [], False
    for selector in request.selectors:
        checked = build_selector_fact(
            request.source, selector.field_name, selector.normalized_value,
            selector_policy_version=selector.selector_policy_version, allowlist=selector_allowlist)
        if checked != selector:
            raise _invalid("noncanonical selector", selector.field_name)
        if selector.field_name == "trace_id":
            traces.append(selector.normalized_value)
        if selector.field_name in {"service_name", "downstream_service"}:
            if selector.normalized_value == "multiple":
                raise _invalid("aggregate marker is not a concrete selector", selector.field_name)
            concrete = True
    if len(traces) != 1 or not concrete:
        raise _invalid("one trace and a concrete bounded identity are required", "selectors")
    return ("PENDING PARSER ERROR COVERAGE AND QUERY PROVENANCE CONTRACT",)


def inspect_global_latency(request: SourceAdapterRequest) -> dict:
    """A readiness view retaining all original selectors; never strips a request."""
    if (request.source is not EvidenceSource.PROMETHEUS
            or request.query_resource != "api_p95_latency_ms"
            or request.query_resolution_seconds != 15.0):
        raise _invalid("global latency resource/resolution mismatch", "query_resource")
    return {
        "status": "PENDING SPEC-013 GLOBAL REQUEST AND PROJECTION CONTRACT",
        "scope_candidate": "GLOBAL", "unit": "ms", "metric": request.query_resource,
        "step_seconds": request.query_resolution_seconds,
        "original_query_provenance": request.query_provenance,
        "original_selectors": request.selectors,
        "aggregation": "NONE; UNCHANGED ORIGINAL SERIES",
        "strong_evidence_authority": False,
        "source_status": "NOT COLLECTED", "completeness": "NOT OBSERVED",
    }
