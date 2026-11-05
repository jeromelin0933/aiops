"""SPEC-013 section 24: explicit demo-v2 contracts, compilation and adapters.

No authority publication, store initialization, retries or network at import.
Controlled publication pins are supplied independently by the Human owner.
The controller execution gate is unrelated and remains blocked.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re

from .contracts import (BoundsOmissionFacts, EvidenceCompleteness, EvidenceSource,
                        SelectorFact, SourceAdapterRequest, SourceStatus)
from .errors import EvidenceDomainError, EvidenceFailureKind
from .identity import canonical_json
from .security import build_selector_fact, validate_safe_provenance, validate_safe_text
from .adapters.loki import LokiRangeAdapter, LokiSourceResult, _summary as log_summary, _epoch_ns
from .adapters.prometheus import PrometheusRangeAdapter, PrometheusSourceResult, _labels as metric_labels


VERSION = "spec013-demo-evidence-v2"
CANONICAL_VERSION = "evidence-canonical-demo-v2"
SOURCE_VERSION = "evidence-sources-demo-v2"
TRACE_PROFILE = "loki-demo-bounded-json-trace-v2"
TRACE_QUERY = "logql-index-plus-client-json-v1"
GLOBAL_PROFILE = "prometheus-demo-global-latency-v1"
SPEC_PATCH_SHA = "40932e399b4bc934d14596f25bac150a669ac1fb10a4b5d6f5bf613cd2be70a8"
EVENT_PROJECTION = "spec013-trusted-event-membership-projection-v1"
INCIDENT_PROJECTION = "spec013-incident-capture-projection-v1"
SERIALIZATION = "spec013-canonical-json-serialization-v1"
_PIN = object()
_LOKI_FIELDS = frozenset({"service_name", "downstream_service", "trace_id", "source_ip",
                          "external_service", "target_service"})


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def invalid(message: str) -> EvidenceDomainError:
    return EvidenceDomainError(EvidenceFailureKind.SOURCE_INVALID, message)


def _hash(value: str, length=64) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None:
        raise ValueError("invalid version binding hash")


def _reference(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or value.startswith("PENDING"):
        raise ValueError("controlled published reference required")
    validate_safe_text(value, field_path="demo.reference")


def _canonical_fact(fact: SelectorFact) -> None:
    if not isinstance(fact, SelectorFact):
        raise ValueError("canonical SelectorFact required")
    checked = build_demo_selector_fact(fact.source, fact.field_name, fact.normalized_value,
        selector_policy_version=fact.selector_policy_version,
        allowlist={EvidenceSource.LOKI: _LOKI_FIELDS})
    if checked != fact:
        raise ValueError("noncanonical selector")


def build_demo_selector_fact(*args, **kwargs) -> SelectorFact:
    """Reuse canonical validation, adding the approved v2 concrete-trace rule."""
    fact = build_selector_fact(*args, **kwargs)
    if fact.field_name == "trace_id" and fact.normalized_value == "multiple":
        raise ValueError("aggregate marker is not a concrete Trace identity")
    return fact


@dataclass(frozen=True)
class DemoPolicyBinding:
    """An immutable pinned policy record, not a Capture Execution Grant."""
    record_json: str
    record_sha256: str
    _pin: object = None

    @classmethod
    def from_published_record(cls, raw: bytes, *, expected_sha256: str,
                              expected_source_revision: str, now: datetime):
        _hash(expected_sha256); _hash(expected_source_revision, 40)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536:
            raise ValueError("bounded policy record required")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("policy publication pin mismatch")
        record = json.loads(raw)
        required = {"version", "decision", "issuer_role", "reference", "scope",
                    "source_revision", "spec_patch_sha256", "valid_from", "expires_at",
                    "loki_endpoint_sha256", "prometheus_endpoint_sha256", "coverage_evidence"}
        if set(record) != required or (record["version"], record["decision"], record["issuer_role"],
            record["scope"], record["source_revision"], record["spec_patch_sha256"]) != (
                VERSION, "APPROVE", "SPEC_LEAD", "COMPETITION_DEMO_ONLY",
                expected_source_revision, SPEC_PATCH_SHA):
            raise ValueError("policy record version/scope/approval mismatch")
        _reference(record["reference"])
        for name in ("loki_endpoint_sha256", "prometheus_endpoint_sha256"):
            _hash(record[name])
        validate_safe_provenance(record, path="demo.policy")
        encoded = canonical_json(record)
        value = cls(encoded, expected_sha256, (_PIN, encoded, expected_sha256))
        value.require_current(now)
        return value

    @property
    def record(self): return json.loads(self.record_json)

    def require_current(self, now=None):
        if self._pin != (_PIN, self.record_json, self.record_sha256):
            raise ValueError("independently pinned policy record required")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("aware validity time required")
        start = datetime.fromisoformat(self.record["valid_from"])
        end = datetime.fromisoformat(self.record["expires_at"])
        if start.tzinfo is None or end.tzinfo is None or not start <= current < end:
            raise ValueError("demo policy is outside validity")

    def projection(self):
        return {"version": VERSION, "scope": "COMPETITION_DEMO_ONLY",
                "reference": self.record["reference"], "record_sha256": self.record_sha256,
                "source_revision": self.record["source_revision"], "spec_patch_sha256": SPEC_PATCH_SHA}


@dataclass(frozen=True)
class MembershipProvenanceV1:
    incident_id: str
    incident_projection_sha256: str
    event_references: tuple[tuple[str, str], ...]
    source_policy_version: str
    source_revision: str
    incident_projection_version: str = INCIDENT_PROJECTION
    event_projection_version: str = EVENT_PROJECTION
    canonical_serialization_version: str = SERIALIZATION
    digest_algorithm: str = "SHA-256/UTF-8/no-salt/no-namespace"

    def __post_init__(self):
        _hash(self.incident_projection_sha256); _hash(self.source_revision, 40)
        _reference(self.incident_id); _reference(self.source_policy_version)
        if (not self.event_references or tuple(sorted(self.event_references)) != self.event_references
                or len({e for e, _ in self.event_references}) != len(self.event_references)):
            raise ValueError("stable distinct event references required")
        for event_id, value in self.event_references:
            _reference(event_id); _hash(value)
        if (self.incident_projection_version, self.event_projection_version,
                self.canonical_serialization_version, self.digest_algorithm) != (
                INCIDENT_PROJECTION, EVENT_PROJECTION, SERIALIZATION, "SHA-256/UTF-8/no-salt/no-namespace"):
            raise ValueError("unknown membership projection version")

    @property
    def provenance_commitment(self): return digest(self)


@dataclass(frozen=True)
class BoundedServiceOrV1:
    service_facts: tuple[SelectorFact, ...]
    dependency_fact: SelectorFact
    membership_provenance: MembershipProvenanceV1

    def __post_init__(self):
        if not isinstance(self.membership_provenance, MembershipProvenanceV1):
            raise ValueError("versioned membership provenance required")
        facts = tuple(sorted(set(self.service_facts), key=lambda f:f.normalized_value))
        if not 1 <= len(facts) <= 8 or len({f.normalized_value for f in facts}) != len(facts):
            raise ValueError("one to eight distinct service identities required")
        for fact in facts + (self.dependency_fact,):
            _canonical_fact(fact)
            if (fact.source is not EvidenceSource.LOKI or fact.normalized_value == "multiple"
                    or any(ord(c)<32 or ord(c)==127 for c in fact.normalized_value)):
                raise ValueError("concrete Loki identity required")
        if any(f.field_name != "service_name" for f in facts) or self.dependency_fact.field_name != "downstream_service":
            raise ValueError("OR requires service and dependency facts")
        if len({f.selector_policy_version for f in facts + (self.dependency_fact,)}) != 1:
            raise ValueError("mixed selector policy versions")
        object.__setattr__(self, "service_facts", facts)

    def query_parts(self):
        # Quote only regex metacharacters, then LogQL string-encode the pattern.
        def literal(value): return "".join("\\"+c if c in r"\.^$|?*+()[]{}" else c for c in value)
        pattern = "^(?:" + "|".join(literal(f.normalized_value) for f in self.service_facts) + ")$"
        return ["service_name=~" + json.dumps(pattern, ensure_ascii=False),
                "downstream_service=" + json.dumps(self.dependency_fact.normalized_value, ensure_ascii=False)]


@dataclass(frozen=True)
class StructuredTraceV2:
    trace_fact: SelectorFact
    parser_policy_identity: str = "bounded-pre-filter-json-v1"
    completeness_basis: str = "BOUNDED_PRE_FILTER_RESPONSE"

    def __post_init__(self):
        _canonical_fact(self.trace_fact)
        if (self.trace_fact.field_name != "trace_id" or self.trace_fact.source is not EvidenceSource.LOKI
                or self.parser_policy_identity != "bounded-pre-filter-json-v1"
                or self.completeness_basis != "BOUNDED_PRE_FILTER_RESPONSE"):
            raise ValueError("incompatible trace/filter representation")


@dataclass(frozen=True)
class DemoEvidenceRequestV2:
    base_request: SourceAdapterRequest
    policy: DemoPolicyBinding
    service_or: BoundedServiceOrV1 | None = None
    trace_filter: StructuredTraceV2 | None = None
    scope: str = "INCIDENT_BOUNDED_LOG"

    def __post_init__(self):
        if not isinstance(self.base_request, SourceAdapterRequest) or not isinstance(self.policy, DemoPolicyBinding):
            raise ValueError("explicit typed demo request and policy required")
        self.policy.require_current()
        base = self.base_request
        if base.source is EvidenceSource.LOKI:
            if self.scope != "INCIDENT_BOUNDED_LOG" or self.service_or is None or base.query_resource != "streams":
                raise ValueError("explicit membership log request required")
            if self.trace_filter is None or base.query_semantic_identity != TRACE_PROFILE or base.query_version != TRACE_QUERY:
                raise ValueError("unknown demo log profile")
            traces = tuple(f for f in base.selectors if f.field_name == "trace_id")
            for trace in traces:_canonical_fact(trace)
            if (bool(traces) != bool(self.trace_filter)
                    or (traces and traces != (self.trace_filter.trace_fact,))):
                raise ValueError("original trace cannot be dropped or replaced")
            if self.service_or.membership_provenance.source_revision != self.policy.record["source_revision"]:
                raise ValueError("membership source binding mismatch")
        elif (self.scope != "GLOBAL" or self.service_or is not None or self.trace_filter is not None
                or base.query_semantic_identity != GLOBAL_PROFILE or base.query_version != "promql-global-context-v1"
                or base.query_resource != "api_p95_latency_ms" or base.query_resolution_seconds != 15.0):
            raise ValueError("explicit GLOBAL latency profile required")

    @property
    def original_selectors(self): return self.base_request.selectors


@dataclass(frozen=True)
class RawFetchCoverageV1:
    raw_admission_cap: int
    requested_limit: int
    raw_response_count: int | None
    observed_raw_lower_bound: int
    backend_limit: int | None
    overflow_sentinel_seen: bool
    continuation_complete: bool | None
    truncation: bool | None
    backend_warnings: tuple[str, ...]
    fetch_completeness: str
    coverage_basis: str
    coverage_evidence: dict | None
    response_commitment: str | None
    response_facts: tuple[tuple[str, object], ...] = ()

    def __post_init__(self):
        if type(self.raw_admission_cap) is not int or self.raw_admission_cap < 1 or self.requested_limit != self.raw_admission_cap+1:
            raise ValueError("raw sentinel limit mismatch")
        if self.fetch_completeness not in {"COMPLETE","UNKNOWN","INCOMPLETE"}:
            raise ValueError("unknown fetch coverage state")
        for value in (self.raw_response_count,self.backend_limit):
            if value is not None and (type(value) is not int or value < 0):raise ValueError("invalid raw count/limit")
        if type(self.observed_raw_lower_bound) is not int or self.observed_raw_lower_bound < 0:
            raise ValueError("invalid observed raw lower bound")
        object.__setattr__(self,"backend_warnings",tuple(self.backend_warnings))
        if self.response_commitment is not None:_hash(self.response_commitment)
        if self.fetch_completeness == "COMPLETE" and not (
                self.raw_response_count is not None and self.raw_response_count <= self.raw_admission_cap
                and self.observed_raw_lower_bound == self.raw_response_count
                and self.backend_limit is not None and self.backend_limit >= self.requested_limit
                and self.continuation_complete is True and self.truncation is False
                and self.overflow_sentinel_seen is False and not self.backend_warnings
                and self.coverage_evidence is not None and self.response_commitment is not None):
            raise ValueError("unsupported COMPLETE coverage")
        if self.fetch_completeness == "COMPLETE":
            evidence=self.coverage_evidence
            _reference(evidence["reference"])
            for key in ("contract_sha256","deployment_config_sha256"):_hash(evidence[key])
            if (self.coverage_basis!="PINNED_ENUMERATION_AND_BELOW_SENTINEL"
                    or evidence.get("backend_limit")!=self.backend_limit
                    or not all(evidence.get(k) is True for k in ("exhaustive_until_limit",
                        "all_streams_included","no_silent_caps","warnings_observable"))):
                raise ValueError("COMPLETE coverage evidence is incompatible")


@dataclass(frozen=True)
class ParsingCoverageV1:
    scanned_count: int
    valid_object_count: int
    invalid_count: int
    missing_trace_count: int
    nonmatching_trace_count: int
    matching_trace_count: int
    filtered_candidate_count: int
    filtered_result_count: int
    covered_response_commitment: str
    parser_policy_identity: str = "bounded-pre-filter-json-v1"

    def __post_init__(self):
        counts=(self.scanned_count,self.valid_object_count,self.invalid_count,self.missing_trace_count,
                self.nonmatching_trace_count,self.matching_trace_count,self.filtered_candidate_count,self.filtered_result_count)
        if any(type(n) is not int or n < 0 for n in counts) or (
                self.scanned_count != self.valid_object_count+self.invalid_count
                or self.valid_object_count != self.missing_trace_count+self.nonmatching_trace_count+self.matching_trace_count
                or self.filtered_candidate_count != self.matching_trace_count
                or self.filtered_result_count > self.matching_trace_count):
            raise ValueError("invalid parsing coverage counts")
        _hash(self.covered_response_commitment)
        if self.parser_policy_identity!="bounded-pre-filter-json-v1":
            raise ValueError("unsupported client JSON parsing policy")


@dataclass(frozen=True)
class QueryProvenanceV2:
    original_selectors: tuple[SelectorFact, ...]
    server_query: str
    client_filter_identity: str
    scope: str
    logical_window: object
    resolution_seconds: float | None
    membership_provenance: MembershipProvenanceV1 | None
    policy_binding: dict
    applied_predicate: dict


@dataclass(frozen=True)
class CollectionProvenanceV2:
    v1_collection_facts: object
    raw_fetch_coverage: RawFetchCoverageV1 | None
    parsing_coverage: ParsingCoverageV1 | None


@dataclass(frozen=True)
class GlobalLatencyContextV1:
    original_labels: tuple[tuple[str, str], ...]
    samples: tuple[object, ...]
    original_selectors: tuple[SelectorFact, ...]
    query_provenance: QueryProvenanceV2
    source_provenance: object
    omission_facts: object
    completeness: EvidenceCompleteness
    resource: str = "api_p95_latency_ms"
    unit: str = "ms"
    scope: str = "GLOBAL"
    step_seconds: float = 15.0
    strength: str = "CONTEXTUAL_ONLY"
    strong_evidence_eligible: bool = False
    applied_selectors: tuple = ()
    nonapplication_reason: str = "GLOBAL_SERIES_NOT_SERVICE_LABELED"
    aggregation_facts: str = "NONE; ORIGINAL SINGLE SERIES"

    def __post_init__(self):
        if (self.resource,self.unit,self.scope,self.step_seconds,self.strength,
                self.strong_evidence_eligible,self.applied_selectors,self.nonapplication_reason) != (
                "api_p95_latency_ms","ms","GLOBAL",15.0,"CONTEXTUAL_ONLY",False,(),
                "GLOBAL_SERIES_NOT_SERVICE_LABELED") or type(self.strong_evidence_eligible) is not bool:
            raise ValueError("GLOBAL context cannot acquire service/strong authority")
        if self.original_labels and dict(self.original_labels).get("__name__") != self.resource:
            raise ValueError("GLOBAL original metric identity does not match resource")
        if self.samples and (not self.original_labels or any(sample.labels != self.original_labels for sample in self.samples)):
            raise ValueError("GLOBAL samples do not preserve original metric labels")

    def require_consumer(self, *, scopes: frozenset, strengths: frozenset):
        if "GLOBAL" not in scopes or "CONTEXTUAL_ONLY" not in strengths:
            raise ValueError("consumer cannot preserve GLOBAL/context-only semantics")
        return self


@dataclass(frozen=True)
class DemoSourceResultV2:
    base_result: LokiSourceResult | PrometheusSourceResult
    query: QueryProvenanceV2
    collection: CollectionProvenanceV2
    global_context: GlobalLatencyContextV1 | None = None

    def projection(self):
        result=json.loads(canonical_json({"query":self.query,"collection":self.collection,
                                          "global_context":self.global_context}))
        if self.query.membership_provenance is not None:
            result["query"]["membership_provenance"]["provenance_commitment"]=self.query.membership_provenance.provenance_commitment
        return result


def membership_from_plan(plan, policy: DemoPolicyBinding) -> BoundedServiceOrV1:
    services = set(); dependencies = set()
    for event in plan.events:
        values = dict(event.selector_values)
        service, dependency = values.get("service_name"), values.get("downstream_service")
        if not service or not dependency or "multiple" in (service, dependency):
            raise invalid("public membership lacks concrete service/dependency")
        services.add(service); dependencies.add(dependency)
    if not 1 <= len(services) <= 8 or len(dependencies) != 1:
        raise invalid("membership is conflicting or exceeds eight services")
    def fact(name, value):
        return build_selector_fact(EvidenceSource.LOKI, name, value,
            selector_policy_version=plan.selector_policy_version,
            allowlist={EvidenceSource.LOKI: _LOKI_FIELDS})
    provenance = MembershipProvenanceV1(plan.incident.incident_id, digest(plan.incident),
        tuple(sorted((event.event_id, digest(event)) for event in plan.events)),
        plan.source_policy_version, policy.record["source_revision"])
    return BoundedServiceOrV1(tuple(fact("service_name", s) for s in sorted(services)),
                             fact("downstream_service", next(iter(dependencies))), provenance)


class LokiDemoV2Adapter(LokiRangeAdapter):
    """One raw index fetch; no server trace filter, paging or hidden retry."""
    def collect(self, request: DemoEvidenceRequestV2) -> DemoSourceResultV2:
        if not isinstance(request, DemoEvidenceRequestV2) or request.base_request.source is not EvidenceSource.LOKI:
            raise ValueError("demo-v2 Loki request required")
        base = request.base_request; request.policy.require_current()
        _canonical_fact(request.trace_filter.trace_fact)
        record = request.policy.record
        if hashlib.sha256(self._endpoint.encode()).hexdigest() != record["loki_endpoint_sha256"]:
            raise invalid("Loki source endpoint binding mismatch")
        parts = request.service_or.query_parts()
        for fact in base.selectors:
            checked = build_demo_selector_fact(base.source, fact.field_name, fact.normalized_value,
                selector_policy_version=fact.selector_policy_version, allowlist=self._selector_allowlist)
            if fact != checked: raise invalid("noncanonical original selector")
            if fact.field_name == "downstream_service" and fact != request.service_or.dependency_fact:
                raise invalid("dependency selector conflicts with membership")
            if fact.field_name not in {"service_name", "downstream_service", "trace_id"}:
                parts.append(fact.field_name + "=" + json.dumps(fact.normalized_value, ensure_ascii=False))
        query_text = "{" + ",".join(parts) + "}"
        query = QueryProvenanceV2(base.selectors, query_text,
            "EXACT_CLIENT_JSON_TRACE_V1" if request.trace_filter else "NO_TRACE_FILTER",
            request.scope, base.logical_window, None, request.service_or.membership_provenance,
            request.policy.projection(), {"service_values":[f.normalized_value for f in request.service_or.service_facts],
                "dependency":request.service_or.dependency_fact.normalized_value,
                "original_service_fact_use":"REPLACED_BY_PUBLIC_MEMBERSHIP_OR",
                "trace_application":"CLIENT_JSON_EXACT" if request.trace_filter else "NONE"})
        payload = self._fetch(base, {"query": query_text, "start": str(_epoch_ns(base.logical_window.start)),
            "end": str(_epoch_ns(base.logical_window.end)), "limit": str(base.max_records+1), "direction": "forward"})
        if isinstance(payload, LokiSourceResult):
            return DemoSourceResultV2(payload, query, CollectionProvenanceV2(payload.summary.collection_provenance, None, None))
        try:
            rows, count = self._normalize(payload, base)
        except (TypeError, ValueError, EvidenceDomainError, OverflowError):
            result = self._failure(base, SourceStatus.INVALID, "invalid raw response", continuation_complete=False)
            return DemoSourceResultV2(result, query, CollectionProvenanceV2(result.summary.collection_provenance, None, None))
        evidence = record["coverage_evidence"]
        limit = evidence.get("backend_limit") if isinstance(evidence, dict) else None
        evidence_ok = False
        if isinstance(evidence, dict):
            try:
                for key in ("contract_sha256", "deployment_config_sha256"):_hash(evidence[key])
                _reference(evidence["reference"])
                label_fields = {"service_name", "downstream_service"} | {
                    fact.field_name for fact in base.selectors if fact.field_name != "trace_id"}
                evidence_ok = (type(limit) is int and limit >= base.max_records+1
                    and label_fields <= set(evidence["indexed_labels"])
                    and all(evidence.get(k) is True for k in ("exhaustive_until_limit", "all_streams_included",
                                                              "no_silent_caps", "warnings_observable")))
            except (KeyError, ValueError, TypeError):pass
        overflow = count > base.max_records
        raw_warnings = payload.get("warnings", ())
        try:
            if not isinstance(raw_warnings,(list,tuple)) or any(not isinstance(x,str) for x in raw_warnings):
                raise ValueError("invalid warning representation")
            for warning in raw_warnings:
                validate_safe_text(warning,field_path="demo.backend_warning")
                if len(warning.encode())>base.max_content_bytes:raise ValueError("warning bound")
            warnings=tuple(raw_warnings)
        except (ValueError,EvidenceDomainError):warnings=("unsafe or invalid backend warning",)
        flags_clear = (not warnings and all(payload.get(key,False) is False for key in ("partial","truncated"))
                       and payload.get("continuation",False) is False and payload.get("nextPageToken",False) is False)
        response_facts=[]
        for key in ("backend_limit","effective_limit","limit"):
            if key in payload:
                value=payload[key]
                response_facts.append((key,value if type(value) is int else "INVALID"))
                if type(value) is not int or value<base.max_records+1:flags_clear=False
        try: response_commitment = digest(payload)
        except (ValueError,TypeError):
            result=self._failure(base,SourceStatus.INVALID,"invalid response commitment",continuation_complete=False)
            return DemoSourceResultV2(result,query,CollectionProvenanceV2(result.summary.collection_provenance,None,None))
        complete = evidence_ok and flags_clear and not overflow
        coverage = RawFetchCoverageV1(base.max_records, base.max_records+1,
            None if overflow else count, count, limit, overflow,
            True if complete else False if overflow else None,
            True if overflow else False if complete else None, warnings,
            "COMPLETE" if complete else "INCOMPLETE" if overflow else "UNKNOWN",
            "PINNED_ENUMERATION_AND_BELOW_SENTINEL" if complete else "NOT_ESTABLISHED",
            evidence, response_commitment, tuple(response_facts))
        if not complete:
            result = self._failure(base, SourceStatus.INVALID, "raw fetch coverage not complete",
                continuation_complete=False, truncation=overflow)
            return DemoSourceResultV2(result, query, CollectionProvenanceV2(result.summary.collection_provenance, coverage, None))
        invalid_count = missing = nonmatch = 0; matches = []
        for row in rows:
            try:
                if dict(row.labels).get("__error__"): raise ValueError("parser error label")
                data = json.loads(row.line)
                if not isinstance(data, dict): raise ValueError("JSON object required")
                trace = data.get("trace_id")
                if "trace_id" in data:
                    build_demo_selector_fact(EvidenceSource.LOKI, "trace_id", trace,
                        selector_policy_version=request.trace_filter.trace_fact.selector_policy_version,
                        allowlist=self._selector_allowlist)
                if request.trace_filter:
                    if trace is None: missing += 1; continue
                    if trace != request.trace_filter.trace_fact.normalized_value: nonmatch += 1; continue
                matches.append(row)
            except (TypeError, ValueError, EvidenceDomainError):invalid_count += 1
        deduped = tuple(sorted(set(matches), key=lambda row:(row.timestamp_ns, row.labels, row.line)))
        parsing = ParsingCoverageV1(count, count-invalid_count, invalid_count, missing, nonmatch,
                                    len(matches), len(matches), len(deduped), response_commitment)
        if invalid_count:
            result = self._failure(base, SourceStatus.INVALID, "client JSON parsing failed")
        else:
            omitted = len(matches)-len(deduped)
            bounds = BoundsOmissionFacts(base.bounds_policy_identity, len(matches),len(deduped),omitted,
                "canonical duplicate" if omitted else None,False,None,False,None,False,bool(omitted),
                len(matches) if omitted else None,len(deduped) if omitted else None,False,None,EvidenceCompleteness.FULL)
            status = SourceStatus.AVAILABLE if deduped else SourceStatus.EMPTY
            result = LokiSourceResult(log_summary(base,status,bounds,(),page_count=1),deduped)
        return DemoSourceResultV2(result,query,CollectionProvenanceV2(result.summary.collection_provenance,coverage,parsing))


class PrometheusDemoV2Adapter(PrometheusRangeAdapter):
    def collect(self, request: DemoEvidenceRequestV2) -> DemoSourceResultV2:
        if not isinstance(request, DemoEvidenceRequestV2) or request.scope != "GLOBAL":
            raise ValueError("explicit GLOBAL demo request required")
        base=request.base_request; request.policy.require_current()
        if hashlib.sha256(self._endpoint.encode()).hexdigest()!=request.policy.record["prometheus_endpoint_sha256"]:
            raise invalid("Prometheus source endpoint binding mismatch")
        for fact in base.selectors:
            checked=build_selector_fact(base.source,fact.field_name,fact.normalized_value,
                selector_policy_version=fact.selector_policy_version,allowlist=self._selector_allowlist)
            if checked!=fact:raise invalid("noncanonical original GLOBAL predicate")
        applied = replace(base, selectors=())
        query=QueryProvenanceV2(base.selectors,"api_p95_latency_ms","NO_CLIENT_FILTER","GLOBAL",
            base.logical_window,15.0,None,request.policy.projection(),
            {"selectors":[],"nonapplication_reason":"GLOBAL_SERIES_NOT_SERVICE_LABELED"})
        payload=self._fetch(applied,{"query":"api_p95_latency_ms","start":str(base.logical_window.start.timestamp()),
            "end":str(base.logical_window.end.timestamp()),"step":"15"})
        labels=()
        if isinstance(payload,PrometheusSourceResult):
            result=replace(payload,summary=replace(payload.summary,query_provenance=base.query_provenance))
        else:
            data=payload.get("data") if isinstance(payload,dict) else None
            matrix=data.get("result") if isinstance(data,dict) else None
            if (not isinstance(matrix,list) or len(matrix)>1 or any(
                    not isinstance(item,dict) or not isinstance(item.get("metric"),dict)
                    or item["metric"].get("__name__") != "api_p95_latency_ms"
                    or set(item["metric"]) & {"service_name","downstream_service"} for item in matrix)
                    or payload.get("warnings") or payload.get("infos")
                    or any(payload.get(flag,False) is not False for flag in ("partial","truncated","continuation"))):
                result=self._failure(base,SourceStatus.INVALID,"GLOBAL series ambiguous, service-labelled or metric identity invalid")
            else:
                # Reuse the complete legacy parser/status/bounds path with the SAME response,
                # through a local transport closure, not a second physical request.
                parsed=PrometheusRangeAdapter(self._endpoint,selector_allowlist=self._selector_allowlist,
                    http_client=lambda *args,**kwargs:payload).collect(applied)
                result=replace(parsed,summary=replace(parsed.summary,query_provenance=base.query_provenance))
                if matrix and result.status in {SourceStatus.AVAILABLE,SourceStatus.EMPTY}:
                    labels=metric_labels(matrix[0]["metric"],applied)
        context=GlobalLatencyContextV1(labels,result.records,base.selectors,query,
            result.summary.collection_provenance,result.summary.bounds,result.summary.bounds.completeness_impact)
        return DemoSourceResultV2(result,query,CollectionProvenanceV2(result.summary.collection_provenance,None,None),context)


def request_for_plan(plan, base: SourceAdapterRequest, binding: DemoPolicyBinding) -> DemoEvidenceRequestV2:
    if base.source is EvidenceSource.LOKI:
        membership=membership_from_plan(plan,binding)
        traces=tuple(f for f in base.selectors if f.field_name=="trace_id")
        trace=StructuredTraceV2(traces[0]) if len(traces)==1 else None
        if trace is None:raise invalid("trusted request has no independent trace identity")
        return DemoEvidenceRequestV2(replace(base,query_semantic_identity=TRACE_PROFILE,
            query_version=TRACE_QUERY,adapter_contract_version="loki-demo-adapter-v2"),binding,membership,trace)
    return DemoEvidenceRequestV2(replace(base,query_resource="api_p95_latency_ms",query_semantic_identity=GLOBAL_PROFILE,
        query_version="promql-global-context-v1",adapter_contract_version="prometheus-demo-adapter-v2",
        query_resolution_seconds=15.0),binding,scope="GLOBAL")


def require_consumer(record, accepted_demo_version=None):
    if accepted_demo_version not in (None,VERSION):raise ValueError("unknown consumer demo version")
    if record is not None and record.canonicalization_version==CANONICAL_VERSION and accepted_demo_version!=VERSION:
        raise EvidenceDomainError(EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
                                  "consumer must explicitly preserve demo-v2 semantics")
    return record


def validate_persisted(content: dict, canonicalization_version: str, *, snapshot: bool):
    """Additive version dispatch; no changes to v1 fields or identity formula."""
    try:
        _validate_persisted(content, canonicalization_version, snapshot=snapshot)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("malformed demo-v2 canonical content") from exc


def _validate_persisted(content: dict, canonicalization_version: str, *, snapshot: bool):
    marker=content.get("demo_v2")
    if marker is None and not canonicalization_version.startswith("evidence-canonical-demo-"):return
    if canonicalization_version!=CANONICAL_VERSION or not isinstance(marker,dict) or marker.get("version")!=VERSION:
        raise ValueError("unknown/incomplete demo evidence version")
    semantic=content["semantic_evidence"] if snapshot else content
    semantic_marker=semantic.get("demo_v2")
    if not isinstance(semantic_marker,dict) or set(semantic_marker)!={"version","global_scope","global_strength"} or (
            semantic_marker["version"],semantic_marker["global_scope"],semantic_marker["global_strength"])!=(
            VERSION,"GLOBAL","CONTEXTUAL_ONLY"):
        raise ValueError("demo Revision lacks context-only semantics")
    for row in semantic["normalized_evidence"].get("PROMETHEUS",[]):
        if (row.get("scope"),row.get("unit"),row.get("resource"),row.get("strength"),
                row.get("strong_evidence_eligible"))!=("GLOBAL","ms","api_p95_latency_ms","CONTEXTUAL_ONLY",False
                ) or type(row.get("strong_evidence_eligible")) is not bool:
            raise ValueError("normalized GLOBAL record lost authority boundary")
        if row["labels"].get("__name__") != "api_p95_latency_ms":
            raise ValueError("normalized GLOBAL metric identity conflicts with resource")
        from .identity import semantic_identity
        if row.get("record_semantic_identity")!=semantic_identity("spec013-global-metric-record-v1",
                {key:value for key,value in row.items() if key!="record_semantic_identity"}):
            raise ValueError("GLOBAL record semantic identity mismatch")
    for row in semantic["normalized_evidence"].get("LOKI",[]):
        trace=json.loads(row["message"]).get("trace_id")
        build_demo_selector_fact(EvidenceSource.LOKI,"trace_id",trace,
            selector_policy_version=semantic["semantic_versions"]["selector_policy"],
            allowlist={EvidenceSource.LOKI:_LOKI_FIELDS})
    if snapshot:
        if set(marker)!={"version","binding","sources"}:
            raise ValueError("demo Snapshot binding is invalid")
        binding=marker["binding"]
        if set(binding)!={"version","scope","reference","record_sha256","source_revision","spec_patch_sha256"} or (
                binding["version"],binding["scope"],binding["spec_patch_sha256"])!=(VERSION,"COMPETITION_DEMO_ONLY",SPEC_PATCH_SHA):
            raise ValueError("demo policy scope/version mismatch")
        _reference(binding["reference"]);_hash(binding["record_sha256"]);_hash(binding["source_revision"],40)
        if set(marker["sources"])!=set(content["provenance"]):raise ValueError("demo source set mismatch")
        for source, metadata in marker["sources"].items():
            if set(metadata)!={"query","collection","global_context"}:raise ValueError("invalid demo source metadata")
            query=metadata["query"]
            if set(query)!={"original_selectors","server_query","client_filter_identity","scope","logical_window",
                           "resolution_seconds","membership_provenance","policy_binding","applied_predicate"}:
                raise ValueError("unknown query provenance fields")
            original=content["provenance"][source]["query"]
            if (query["policy_binding"]!=binding or query["original_selectors"]!=original["selectors"]
                    or query["logical_window"]!=original["logical_window"]):
                raise ValueError("demo original query/approval provenance mismatch")
            collection=metadata["collection"]
            if set(collection)!={"v1_collection_facts","raw_fetch_coverage","parsing_coverage"} or (
                    collection["v1_collection_facts"]!=content["provenance"][source]["collection"]):
                raise ValueError("demo collection provenance mismatch")
            if source=="LOKI":
                if (query["scope"],query["client_filter_identity"],original["query_semantic_identity"],original["query_version"]
                        )!=("INCIDENT_BOUNDED_LOG","EXACT_CLIENT_JSON_TRACE_V1",TRACE_PROFILE,TRACE_QUERY):
                    raise ValueError("unknown log profile")
                traces=[f for f in original["selectors"] if f["field_name"]=="trace_id"]
                if len(traces)!=1:raise ValueError("persisted concrete Trace identity required")
                trace=build_demo_selector_fact(EvidenceSource.LOKI,"trace_id",traces[0]["normalized_value"],
                    selector_policy_version=traces[0]["selector_policy_version"],
                    allowlist={EvidenceSource.LOKI:_LOKI_FIELDS})
                if json.loads(canonical_json(trace))!=traces[0]:raise ValueError("noncanonical persisted Trace")
                provenance=dict(query["membership_provenance"])
                stored_commitment=provenance.pop("provenance_commitment")
                provenance["event_references"]=tuple(tuple(pair) for pair in provenance["event_references"])
                membership=MembershipProvenanceV1(**provenance)
                if (stored_commitment!=membership.provenance_commitment
                        or membership.source_revision!=binding["source_revision"]
                        or membership.incident_id!=content["incident_projection"]["incident_id"]
                        or membership.incident_projection_sha256!=digest(content["incident_projection"])
                        or membership.event_references!=tuple(sorted((e["event_id"],digest(e)) for e in content["event_projections"]))):
                    raise ValueError("membership projection commitment mismatch")
                services=set();dependencies=set()
                for event in content["event_projections"]:
                    facts=dict(event["selector_values"])
                    if not facts.get("service_name") or facts["service_name"]=="multiple" or not facts.get("downstream_service"):
                        raise ValueError("incomplete persisted membership")
                    services.add(facts["service_name"]);dependencies.add(facts["downstream_service"])
                applied=query["applied_predicate"]
                if (set(applied)!={"service_values","dependency","original_service_fact_use","trace_application"}
                        or not 1<=len(services)<=8 or len(dependencies)!=1 or applied["service_values"]!=sorted(services)
                        or applied["dependency"]!=next(iter(dependencies))
                        or applied["original_service_fact_use"]!="REPLACED_BY_PUBLIC_MEMBERSHIP_OR"
                        or applied["trace_application"]!="CLIENT_JSON_EXACT"):
                    raise ValueError("persisted OR does not represent public membership")
                def fact(name,value):
                    return build_selector_fact(EvidenceSource.LOKI,name,value,
                        selector_policy_version=membership.source_policy_version,
                        allowlist={EvidenceSource.LOKI:_LOKI_FIELDS})
                node=BoundedServiceOrV1(tuple(fact("service_name",s) for s in sorted(services)),
                    fact("downstream_service",next(iter(dependencies))),membership)
                parts=node.query_parts()
                for original_fact in original["selectors"]:
                    if original_fact["field_name"] not in {"service_name","downstream_service","trace_id"}:
                        checked=fact(original_fact["field_name"],original_fact["normalized_value"])
                        parts.append(checked.field_name+"="+json.dumps(checked.normalized_value,ensure_ascii=False))
                if query["server_query"]!="{"+",".join(parts)+"}":
                    raise ValueError("persisted bounded query changed")
                raw=collection["raw_fetch_coverage"];parsed=collection["parsing_coverage"]
                status=semantic["source_statuses"][source]
                if raw is not None:raw=RawFetchCoverageV1(**raw)
                if parsed is not None:parsed=ParsingCoverageV1(**parsed)
                if status in {"AVAILABLE","EMPTY"}:
                    if (raw is None or parsed is None or raw.fetch_completeness!="COMPLETE"
                            or parsed.scanned_count!=raw.raw_response_count or parsed.invalid_count!=0
                            or parsed.covered_response_commitment!=raw.response_commitment
                            or parsed.filtered_result_count!=len(semantic["normalized_evidence"][source])):
                        raise ValueError("valid source lacks complete fetch/parsing coverage")
                    traces=[f["normalized_value"] for f in original["selectors"] if f["field_name"]=="trace_id"]
                    if len(traces)!=1:raise ValueError("persisted trace identity missing")
                    for row in semantic["normalized_evidence"][source]:
                        data=json.loads(row["message"])
                        if not isinstance(data,dict) or data.get("trace_id")!=traces[0] or row["labels"].get("__error__"):
                            raise ValueError("persisted log does not satisfy client trace filter")
            if source=="PROMETHEUS":
                context=metadata["global_context"]
                if not isinstance(context,dict) or set(context)!=set(GlobalLatencyContextV1.__dataclass_fields__):
                    raise ValueError("unknown GLOBAL projection fields")
                if (query["scope"]!="GLOBAL" or original["query_semantic_identity"]!=GLOBAL_PROFILE
                        or original["query_version"]!="promql-global-context-v1"
                        or query["server_query"]!="api_p95_latency_ms" or query["resolution_seconds"]!=15.0
                        or query["client_filter_identity"]!="NO_CLIENT_FILTER" or query["membership_provenance"] is not None):
                    raise ValueError("unknown global profile")
                if (context["scope"],context["unit"],context["resource"],context["strength"],
                    context["strong_evidence_eligible"],context["step_seconds"],context["applied_selectors"]) != (
                    "GLOBAL","ms","api_p95_latency_ms","CONTEXTUAL_ONLY",False,15.0,[]):
                    raise ValueError("GLOBAL context semantics were not preserved")
                if (type(context["strong_evidence_eligible"]) is not bool
                        or context["original_selectors"]!=query["original_selectors"]
                        or context["nonapplication_reason"]!="GLOBAL_SERIES_NOT_SERVICE_LABELED"
                        or context["omission_facts"]!=content["bounds"][source]
                        or context["source_provenance"]!=collection["v1_collection_facts"]
                        or context["query_provenance"]!=query
                        or context["aggregation_facts"]!="NONE; ORIGINAL SINGLE SERIES"
                        or context["completeness"]!=content["bounds"][source]["completeness_impact"]
                        or len(context["samples"])!=len(semantic["normalized_evidence"][source])):
                    raise ValueError("GLOBAL provenance/counts mismatch")
                if query["applied_predicate"]!={"selectors":[],"nonapplication_reason":"GLOBAL_SERIES_NOT_SERVICE_LABELED"}:
                    raise ValueError("GLOBAL applied predicates changed")
                if context["original_labels"] and dict(context["original_labels"]).get("__name__")!=context["resource"]:
                    raise ValueError("GLOBAL original labels conflict with metric resource")
                for sample,row in zip(context["samples"],semantic["normalized_evidence"][source]):
                    labels=dict(sample["labels"])
                    if (set(labels)&{"service_name","downstream_service"}
                            or sample["labels"]!=context["original_labels"] or labels!=row["labels"]
                            or sample["value_text"]!=row["value"] or sample["timestamp_seconds"]!=row["timestamp_seconds"]):
                        raise ValueError("GLOBAL original series facts changed")
