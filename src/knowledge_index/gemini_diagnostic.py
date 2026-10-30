"""Diagnostic orchestration with closed offline simulation and guarded live I/O.

Only a fixed in-memory HTTPX MockTransport and an explicit synthetic key are
constructed by the offline path. The live entry loads fixed host authority;
no public caller-supplied authorization, client or transport can select I/O.
Neither path mutates Candidate-C, Runtime, Chroma or Formal Evaluation stores.
"""

from __future__ import annotations

import json
import math
import socket
from threading import Lock
from uuid import uuid4
from hashlib import sha256
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .contracts import (
    BuildFailureCode, ProviderEmbeddingRequest, QueryEmbeddingRequest,
    RetrySafetyDisposition,
)
from .gemini_diagnostic_controls import (
    ApprovedDiagnosticInput, DiagnosticAuthorizationRecord, DiagnosticDenied, DiagnosticExecutionFacts,
    DiagnosticSdkByteEvidence, authorization_record_digest as _authorization_record_digest,
    OfflineAuthorizationSource, TemporaryDiagnosticUsage, input_commitment,
    load_live_binding, reserve_eligible_attempt, resolve_repository_source,
    validate_authorization,
    LiveDiagnosticUsage, load_live_safety_artifact, resolve_diagnostic_credential,
    _issue_provider_usage_receipt,
    COUNTING_OPERATION, COUNTING_CHUNK, COUNTING_INPUT, COUNTING_CORPUS, COUNTING_BODY, COUNTING_URL,
    _timestamp,
    is_single_document_probe, SINGLE_PROBE_RISK_CAP,
)
from .google_embedding_adapter import (
    GEMINI_MODEL, GeminiEmbedding2Verification, GoogleEmbeddingAdapter,
    _GeminiEmbedding2OfflineClient, _create_gemini_offline_mock_client,
    _DiagnosticGuardedMockTransport, _DiagnosticMockResponse,
    _create_guarded_diagnostic_offline_sdk_client, _values,
    _DiagnosticHttpxClient, _DiagnosticAsyncDenyTransport,
    _close_offline_resources, _create_diagnostic_live_sdk_client,
    _validated_vectors,
    _embedding_prompt_tokens,
    _prepare_counting_sdk_request,
)
from .identity import document_identity


@dataclass(frozen=True, slots=True)
class DiagnosticObservation:
    outcome: str
    category: str
    failure_code: BuildFailureCode | None
    failure_field: str | None
    failure_detail: str | None
    retry_safety: RetrySafetyDisposition | None
    provider_attempts: int
    ordered_identities: tuple[str, ...]
    vector_count: int
    usage: str
    cost_estimate_micro_units: int
    campaign_id: str
    source_revision: str
    input_commitment: str
    billing_status: str = "ESTIMATE_NOT_GOOGLE_BILLING"
    usage_provenance_sha256: str | None = None
    admission_usage_basis: str | None = None
    exact_input_token_count: str = "NOT VERIFIED"
    truncation_status: str = "NOT VERIFIED"


@dataclass(frozen=True, slots=True)
class DiagnosticSafetyBasis:
    """Technical evidence for one request; never a live execution grant.

    Production does not accept this object from a caller. A controlled,
    accepted exact-input bound source must be provisioned before live I/O.
    """

    status: str
    reference: str
    campaign_id: str
    source_revision: str
    input_scope_identity: str
    authorization_record_sha256: str
    provider: str
    model: str
    sdk: str
    api: str
    kind: str
    input_commitment: str
    serialized_request_sha256: str
    bounds_basis: str
    cost_basis: str
    token_method: str
    provider_item_limit: int | None
    provider_item_token_limit: int | None
    provider_aggregate_token_limit: int | None
    provider_payload_byte_limit: int | None
    price_tier: str
    price_currency: str
    price_micro_units_per_million_tokens: int | None
    valid_until: datetime
    free_tier_permission_reference: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticSafetyDecision:
    serialized_request_sha256: str
    item_count: int
    item_bytes: tuple[int, ...]
    item_tokens: tuple[int, ...]
    aggregate_tokens: int
    payload_bytes: int
    minimum_cost_micro_units: int
    evidence_status: str
    admission_usage_basis: str = "MODEL_COMPATIBLE_UPPER_BOUND"


def _checked_positive(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise DiagnosticDenied(f"{label} is unavailable or invalid")
    return value


def evaluate_diagnostic_safety(
    record: DiagnosticAuthorizationRecord, basis: DiagnosticSafetyBasis,
    kind: str, identities: tuple[str, ...], texts: tuple[str, ...],
    contents: tuple[Any, ...], config: Any,
    proof: GeminiEmbedding2Verification, reserved_cost_micro_units: int,
    *, allow_synthetic: bool = False,
) -> DiagnosticSafetyDecision:
    """Check actual prepared items against explicitly scoped safety evidence."""
    if not isinstance(basis, DiagnosticSafetyBasis):
        raise DiagnosticDenied("diagnostic safety evidence is missing")
    probe = is_single_document_probe(record) and not allow_synthetic
    if basis.status != ("SYNTHETIC_OFFLINE_ONLY" if allow_synthetic else "VERIFIED"):
        raise DiagnosticDenied("diagnostic safety evidence is not live applicable")
    if not allow_synthetic and proof.evidence_identity == "SYNTHETIC_OFFLINE_ONLY":
        raise DiagnosticDenied("synthetic token counter cannot establish live safety")
    if (
        not basis.reference or basis.campaign_id != record.campaign_id
        or basis.source_revision != record.source_revision
        or basis.input_scope_identity != record.input_scope_identity
        or basis.authorization_record_sha256 != _authorization_record_digest(record)
        or basis.provider != record.provider_identity or basis.model != record.model_identity
        or basis.sdk != record.sdk_baseline or basis.api != record.api_version
        or basis.kind != kind or basis.input_commitment != record.input_commitment
        or basis.bounds_basis != record.bounds_basis or basis.cost_basis != record.cost_basis
        or basis.token_method != ("PM_ACCEPTED_MODEL_LIMIT_RISK_CAP" if probe else
                                  "SYNTHETIC_BYTES_NOT_TOKENS" if allow_synthetic
                                  else "MODEL_COMPATIBLE_UPPER_BOUND")
        or type(basis.valid_until) is not datetime
        or basis.valid_until.tzinfo is None
        or basis.valid_until <= datetime.now(timezone.utc)
    ):
        raise DiagnosticDenied("safety evidence identity, method or validity mismatch")
    if (
        basis.price_currency != "USD" or basis.price_currency != record.cost_currency
        or basis.price_tier not in (("SYNTHETIC_OFFLINE_ONLY",) if allow_synthetic
                                   else ("STANDARD_PAID_TEXT", "FREE_TIER_TEXT_V1"))
    ):
        raise DiagnosticDenied("cost price, account tier or currency is unverified")
    if probe:
        record.__post_init__()
        # These are LOCAL admission caps under PM's bounded-risk exception,
        # not verified input counts, Google aggregate limits or system quotas.
        _, exact = _counting_content()  # Repository-only corpus derivation; no Counting operation.
        if (kind != "document" or identities != (COUNTING_CHUNK,) or texts != (exact,)
                or any(v is not None for v in (basis.provider_item_limit,
                    basis.provider_item_token_limit, basis.provider_aggregate_token_limit,
                    basis.provider_payload_byte_limit))):
            raise DiagnosticDenied("single probe fixed input or risk classification mismatch")
        item_limit, item_token_limit, aggregate_limit = 1, SINGLE_PROBE_RISK_CAP, SINGLE_PROBE_RISK_CAP
        payload_limit = min(record.maximum_payload_bytes, 1_000_000)
    else:
        item_limit = _checked_positive(basis.provider_item_limit, "provider item limit")
        item_token_limit = _checked_positive(basis.provider_item_token_limit, "provider item token limit")
        aggregate_limit = _checked_positive(basis.provider_aggregate_token_limit,
                                            "provider aggregate token limit")
        payload_limit = _checked_positive(basis.provider_payload_byte_limit,
                                          "provider payload limit")
    if basis.price_tier == "FREE_TIER_TEXT_V1":
        if (type(basis.price_micro_units_per_million_tokens) is not int
                or basis.price_micro_units_per_million_tokens != 0
                or basis.free_tier_permission_reference != record.pm_approval_reference):
            raise DiagnosticDenied("Free Tier rate or controlled data-use permission is invalid")
        rate = 0
    else:
        rate = _checked_positive(basis.price_micro_units_per_million_tokens,
                                 "applicable text price")
    _checked_positive(record.maximum_cost_micro_units, "campaign cost limit")
    if len(texts) != len(contents) or not texts or len(texts) > min(
        item_limit, record.maximum_items, 128,
    ):
        raise DiagnosticDenied("provider or approved item bound is exceeded")
    try:
        item_bytes = tuple(len(text.encode("utf-8")) for text in texts)
        item_tokens = tuple(proof.count_tokens(text) for text in texts)
        if probe and item_tokens != (SINGLE_PROBE_RISK_CAP,):
            raise ValueError("probe admission risk cap mismatch")
        payload = _serialized(contents, config)
        requests = json.loads(payload)["requests"]
        if len(requests) != len(texts) or any(
            entry["content"]["parts"] != [{"text": text}]
            or entry["content"]["role"] != "user"
            or entry["model"] != "models/" + GEMINI_MODEL
            or entry["outputDimensionality"] != 768
            for entry, text in zip(requests, texts, strict=True)
        ):
            raise ValueError("serialized request does not match ordered envelopes")
    except Exception as exc:
        raise DiagnosticDenied("serialized diagnostic request is invalid") from exc
    if basis.input_commitment != input_commitment(kind, identities, texts):
        raise DiagnosticDenied("ordered diagnostic input commitment mismatch")
    digest = sha256(_canonical_request(payload)).hexdigest()
    if basis.serialized_request_sha256 != digest:
        raise DiagnosticDenied("serialized request identity differs from safety evidence")
    # SDK key order differs from the mirror. Bound the verified semantic body
    # with a deliberately larger escaped, indented representation.
    bounded_payload_bytes = max(
        len(payload),
        len(json.dumps({"requests": requests}, ensure_ascii=True, indent=2).encode("utf-8")),
    )
    if (
        any(type(n) is not int or not 0 < n <= min(item_token_limit, record.maximum_item_tokens)
            for n in item_tokens)
        or any(not 0 < n <= record.maximum_item_bytes for n in item_bytes)
        or sum(item_bytes) > record.maximum_payload_bytes
        or sum(item_tokens) > min(aggregate_limit, record.maximum_aggregate_tokens)
        or sum(item_tokens) > 2**63 - 1
        or not 0 < bounded_payload_bytes <= min(payload_limit,
                                                record.maximum_payload_bytes, 1_000_000)
    ):
        raise DiagnosticDenied("item, token or serialized payload safety bound is exceeded")
    aggregate = sum(item_tokens)
    conservative_cost = (aggregate * rate + 999_999) // 1_000_000
    if (
        conservative_cost > 2**63 - 1
        or type(reserved_cost_micro_units) is not int
        or reserved_cost_micro_units <= 0
        or reserved_cost_micro_units < conservative_cost
        or reserved_cost_micro_units > record.maximum_cost_micro_units
    ):
        raise DiagnosticDenied("conservative cost reservation is unavailable or exceeded")
    return DiagnosticSafetyDecision(
        digest, len(texts), item_bytes, item_tokens,
        aggregate, bounded_payload_bytes, conservative_cost, basis.status,
        "PM_ACCEPTED_MODEL_LIMIT_RISK_CAP" if probe else
        "SYNTHETIC_OFFLINE_ONLY" if allow_synthetic else "MODEL_COMPATIBLE_UPPER_BOUND",
    )


@dataclass(frozen=True, slots=True)
class _PreparedEligibility:
    """Read-only validated facts, not provider execution authorization."""

    record: DiagnosticAuthorizationRecord
    facts: DiagnosticExecutionFacts
    adapter: GoogleEmbeddingAdapter
    kind: str
    identities: tuple[str, ...]
    texts: tuple[str, ...]
    contents: tuple[Any, ...]
    config: Any
    input_scope_identity: str
    item_bytes: tuple[int, ...]
    item_tokens: tuple[int, ...]
    payload_bytes: int
    safety_decision: DiagnosticSafetyDecision
    safety_basis: str
    usage_units: int
    cost_micro_units: int
    live_opt_in: bool


def _envelopes(
    request: ProviderEmbeddingRequest | QueryEmbeddingRequest,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if isinstance(request, ProviderEmbeddingRequest):
        mapping = dict(request.approved_document_ids)
        if (
            not request.approved_corpus_id
            or set(mapping) != {chunk.document_identity for chunk in request.chunks}
            or any(document_identity(request.approved_corpus_id, approved_id) != identity
                   for identity, approved_id in mapping.items())
        ):
            raise DiagnosticDenied("approved document ID mapping is invalid")
        return (
            "document",
            tuple(chunk.chunk_identity for chunk in request.chunks),
            tuple(f"title: {mapping[chunk.document_identity]} | text: {chunk.content}"
                  for chunk in request.chunks),
        )
    if isinstance(request, QueryEmbeddingRequest):
        return (
            "query", (request.frozen_build_identity,),
            (f"task: search result | query: {request.query.text}",),
        )
    raise DiagnosticDenied("diagnostic request type is invalid")


def _serialized(contents: tuple[Any, ...], config: Any) -> bytes:
    return json.dumps({"requests": [{
        "model": "models/" + GEMINI_MODEL,
        "content": {"role": item.role, "parts": [{"text": part.text} for part in item.parts]},
        "outputDimensionality": config.output_dimensionality,
    } for item in contents]}, ensure_ascii=True).encode("utf-8")


def _canonical_request(payload: bytes) -> bytes:
    return json.dumps(json.loads(payload), sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


def validate_host_sdk_evidence(
    record: DiagnosticAuthorizationRecord, facts: DiagnosticExecutionFacts,
    approved: ApprovedDiagnosticInput, evidence: DiagnosticSdkByteEvidence | None,
    kind: str, identities: tuple[str, ...], texts: tuple[str, ...], scope: str,
) -> None:
    """Bind a fixed Host artifact to source and actual ordered request semantics.

    The raw body digest remains for the future D-C transport guard. This
    read-only check neither supplies E1/E2/E3 proof nor authorizes dispatch.
    """
    if not isinstance(evidence, DiagnosticSdkByteEvidence):
        raise DiagnosticDenied("fixed host SDK byte evidence is unavailable")
    evidence.matches_record_input(record, approved)
    if (not facts.clean_tree or facts.source_revision != evidence.source_revision
            or kind != approved.kind or scope != approved.scope_identity
            or identities != approved.identities or texts != approved.envelopes
            or input_commitment(kind, identities, texts) != evidence.input_commitment):
        raise DiagnosticDenied("actual ordered diagnostic input differs from host SDK evidence")
    expected = {"requests": [{
        "model": "models/" + GEMINI_MODEL,
        "content": {"role": "user", "parts": [{"text": text}]},
        "outputDimensionality": 768,
    } for text in texts]}
    canonical = json.dumps(expected, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":")).encode("utf-8")
    if sha256(canonical).hexdigest() != evidence.canonical_json_sha256:
        raise DiagnosticDenied("actual diagnostic request semantic digest differs from host evidence")


def _validate_sdk_evidence_binding(
    prepared: _PreparedEligibility, approved: ApprovedDiagnosticInput,
    evidence: DiagnosticSdkByteEvidence, *, offline_harness: bool = False,
) -> None:
    """Validate fixed authority metadata before reservation, never actual bytes."""
    if not isinstance(evidence, DiagnosticSdkByteEvidence):
        raise DiagnosticDenied("SDK byte evidence is missing")
    if not offline_harness:
        if (prepared.safety_decision.evidence_status != "VERIFIED"
                or evidence.status != "HOST_CONTROLLED_E4_ONLY"):
            raise DiagnosticDenied("trusted live SDK byte evidence source is not provisioned")
    if evidence.status not in ("SYNTHETIC_OFFLINE_ONLY", "HOST_CONTROLLED_E4_ONLY"):
        raise DiagnosticDenied("SDK byte evidence is not live applicable")
    if evidence.status == "HOST_CONTROLLED_E4_ONLY":
        evidence.matches_record_input(prepared.record, approved)
    if evidence.method != "PINNED_SDK_HTTPX_REQUEST_BODY" or not evidence.reference:
        raise DiagnosticDenied("SDK byte evidence source is invalid")
    if (type(evidence.valid_until) is not datetime or evidence.valid_until.tzinfo is None
            or evidence.valid_until <= datetime.now(timezone.utc)):
        raise DiagnosticDenied("SDK byte evidence has expired")
    approved.matches_record(prepared.record)
    if (
        approved.identities != prepared.identities or approved.envelopes != prepared.texts
        or evidence.campaign_id != prepared.record.campaign_id
        or evidence.source_revision != prepared.record.source_revision
        or evidence.source_revision != prepared.facts.source_revision
        or not prepared.facts.clean_tree
        or evidence.authorization_record_sha256 != _authorization_record_digest(prepared.record)
        or evidence.provider != prepared.record.provider_identity
        or evidence.model != prepared.record.model_identity
        or evidence.sdk != prepared.record.sdk_baseline
        or evidence.api != prepared.record.api_version
        or evidence.kind != prepared.kind
        or evidence.input_scope_identity != prepared.input_scope_identity
        or evidence.ordered_identities != prepared.identities
        or evidence.input_commitment != input_commitment(
            prepared.kind, prepared.identities, prepared.texts)
        or evidence.canonical_json_sha256 != prepared.safety_decision.serialized_request_sha256
        or evidence.http_method != "POST"
        or evidence.request_url != (
            "https://generativelanguage.googleapis.com/v1/models/"
            + GEMINI_MODEL + ":batchEmbedContents")
        or evidence.output_dimensionality != 768
        or evidence.timeout_ms != 30_000 or evidence.attempts != 1
    ):
        raise DiagnosticDenied("SDK byte evidence identity or serialization configuration mismatch")


def compare_sdk_request_bytes(
    prepared: _PreparedEligibility, approved: ApprovedDiagnosticInput,
    evidence: DiagnosticSdkByteEvidence, actual_request: Any,
    *, offline_harness: bool = False,
) -> None:
    """Compare the actual SDK outbound body with an independently scoped observation.

    This is a pre-transport comparison contract. No public live entry supplies
    evidence or calls this function, and synthetic evidence is offline-only.
    """
    _validate_sdk_evidence_binding(prepared, approved, evidence, offline_harness=offline_harness)
    try:
        body = actual_request.content
        timeout = actual_request.extensions["timeout"]
        if (actual_request.method != evidence.http_method
                or str(actual_request.url) != evidence.request_url
                or timeout != {"connect": 30.0, "read": 30.0,
                               "write": 30.0, "pool": 30.0}
                or type(body) is not bytes or not body):
            raise ValueError("SDK request configuration differs")
        parsed = json.loads(body)
        expected = json.loads(_serialized(prepared.contents, prepared.config))
        if parsed != expected or type(parsed) is not dict or set(parsed) != {"requests"}:
            raise ValueError("SDK request differs from prepared ordered input")
        if (len(body) != evidence.sdk_body_bytes
                or sha256(body).hexdigest() != evidence.sdk_body_sha256
                or sha256(_canonical_request(body)).hexdigest() != evidence.canonical_json_sha256
                or len(body) > prepared.payload_bytes
                or len(body) > prepared.record.maximum_payload_bytes):
            raise ValueError("SDK body byte identity or bound differs")
    except Exception as exc:
        raise DiagnosticDenied("actual SDK request bytes do not match approved byte evidence") from exc


def observe_offline_sdk_bytes(
    prepared: _PreparedEligibility, approved: ApprovedDiagnosticInput,
) -> tuple[DiagnosticSdkByteEvidence, Any]:
    """Capture pinned SDK bytes through an isolated MockTransport, without secrets."""
    approved.matches_record(prepared.record)
    if approved.identities != prepared.identities or approved.envelopes != prepared.texts:
        raise DiagnosticDenied("actual request differs from approved diagnostic input")
    import httpx

    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        count = len(json.loads(request.content)["requests"])
        return httpx.Response(200, json={"embeddings": [
            {"values": [1.0] + [0.0] * 767} for _ in range(count)
        ]})

    sdk, sync, async_client = _create_gemini_offline_mock_client(handle)
    try:
        sdk.models.embed_content(model=GEMINI_MODEL, contents=list(prepared.contents),
                                 config=prepared.config)
    finally:
        _cleanup_offline_attempt(sdk, sync, async_client)
    if len(captured) != 1:
        raise DiagnosticDenied("SDK request cardinality is invalid")
    request = captured[0]
    body = request.content
    evidence = DiagnosticSdkByteEvidence(
        "SYNTHETIC_OFFLINE_ONLY", "PINNED_SDK_HTTPX_REQUEST_BODY",
        "isolated-mock-transport", datetime(2099, 1, 1, tzinfo=timezone.utc),
        prepared.record.campaign_id, prepared.record.source_revision,
        _authorization_record_digest(prepared.record), prepared.record.provider_identity,
        prepared.record.model_identity, prepared.record.sdk_baseline,
        prepared.record.api_version, prepared.kind, prepared.input_scope_identity,
        prepared.identities, prepared.record.input_commitment,
        sha256(_canonical_request(body)).hexdigest(), sha256(body).hexdigest(),
        len(body), request.method, str(request.url),
        prepared.config.output_dimensionality, 30_000, 1,
    )
    compare_sdk_request_bytes(prepared, approved, evidence, request, offline_harness=True)
    return evidence, request


class _DiagnosticRequestGuardTransport(_DiagnosticGuardedMockTransport):
    """Consume S1/E4 context; the adapter owns the closed transport mechanics."""

    def __init__(self, prepared: _PreparedEligibility, approved: ApprovedDiagnosticInput,
                 evidence: DiagnosticSdkByteEvidence, response: _DiagnosticMockResponse) -> None:
        self._prepared, self._approved, self._evidence = prepared, approved, evidence
        super().__init__(response)

    def _verify_request(self, request: httpx.Request) -> None:
        try:
            compare_sdk_request_bytes(self._prepared, self._approved, self._evidence,
                                      request, offline_harness=True)
        except Exception:
            raise DiagnosticDenied("diagnostic final request evidence mismatch") from None


def _create_diagnostic_offline_sdk_client(
    prepared: _PreparedEligibility, approved: ApprovedDiagnosticInput,
    evidence: DiagnosticSdkByteEvidence, *, response: _DiagnosticMockResponse | None = None,
) -> Any:
    """Prepare fixed diagnostic context; consume the adapter's closed SDK bridge."""
    if (evidence.status != "SYNTHETIC_OFFLINE_ONLY"
            or prepared.safety_decision.evidence_status != "SYNTHETIC_OFFLINE_ONLY"
            or (response is not None and type(response) is not _DiagnosticMockResponse)):
        raise DiagnosticDenied("synthetic diagnostic factory inputs are invalid")
    approved.matches_record(prepared.record)
    guard = _DiagnosticRequestGuardTransport(prepared, approved, evidence,
                                              response or _DiagnosticMockResponse())
    return _create_guarded_diagnostic_offline_sdk_client(guard)


def _offline_proof(record: DiagnosticAuthorizationRecord) -> GeminiEmbedding2Verification:
    # This byte-based synthetic counter is NEVER a live model token proof.
    return GeminiEmbedding2Verification(
        count_tokens=lambda text: len(text.encode("utf-8")),
        serialize_payload=_serialized,
        max_item_tokens=record.maximum_item_tokens,
        max_aggregate_tokens=record.maximum_aggregate_tokens,
        max_provider_items=record.maximum_items,
        max_item_bytes=record.maximum_item_bytes,
        max_payload_bytes=record.maximum_payload_bytes,
        normalization_tolerance=record.normalization_tolerance,
        evidence_identity="SYNTHETIC_OFFLINE_ONLY",
    )


def _offline_safety_basis(record: DiagnosticAuthorizationRecord,
                          texts: tuple[str, ...]) -> DiagnosticSafetyBasis:
    payload = json.dumps({"requests": [{
        "model": "models/" + GEMINI_MODEL,
        "content": {"role": "user", "parts": [{"text": text}]},
        "outputDimensionality": 768,
    } for text in texts]}, ensure_ascii=True).encode("utf-8")
    return DiagnosticSafetyBasis(
        "SYNTHETIC_OFFLINE_ONLY", "isolated-mock-transport", record.campaign_id,
        record.source_revision, record.input_scope_identity,
        _authorization_record_digest(record),
        record.provider_identity, record.model_identity, record.sdk_baseline,
        record.api_version, record.input_kind, record.input_commitment,
        sha256(_canonical_request(payload)).hexdigest(),
        record.bounds_basis, record.cost_basis, "SYNTHETIC_BYTES_NOT_TOKENS",
        record.maximum_items, record.maximum_item_tokens,
        record.maximum_aggregate_tokens, record.maximum_payload_bytes,
        "SYNTHETIC_OFFLINE_ONLY", record.cost_currency, 1,
        datetime(2099, 1, 1, tzinfo=timezone.utc),
    )


def _preflight(
    *,
    load_record: Callable[[], DiagnosticAuthorizationRecord],
    resolve_facts: Callable[[DiagnosticAuthorizationRecord,
                             ProviderEmbeddingRequest | QueryEmbeddingRequest], DiagnosticExecutionFacts],
    safety: Callable[[DiagnosticAuthorizationRecord, tuple[str, ...]],
                     tuple[GeminiEmbedding2Verification, DiagnosticSafetyBasis, int, int]],
    request: ProviderEmbeddingRequest | QueryEmbeddingRequest,
    live_opt_in: bool,
    offline_harness: bool = False,
    host_byte_binding: Callable[[DiagnosticAuthorizationRecord, DiagnosticExecutionFacts,
                                 str, tuple[str, ...], tuple[str, ...], str], None] | None = None,
) -> _PreparedEligibility:
    """The shared, read-only production/offline pre-transport eligibility core."""
    if live_opt_in is not True:
        raise DiagnosticDenied("explicit diagnostic opt-in is required")
    record = load_record()
    if not isinstance(record, DiagnosticAuthorizationRecord):
        raise DiagnosticDenied("controlled authorization record is unavailable")
    facts = resolve_facts(record, request)
    if not isinstance(facts, DiagnosticExecutionFacts):
        raise DiagnosticDenied("repository or execution facts are unavailable")
    if not facts.clean_tree or facts.source_revision != record.source_revision:
        raise DiagnosticDenied("clean committed source revision does not match authorization", code="SOURCE_MISMATCH")
    if request.capability.model != GEMINI_MODEL or request.capability.provider != "google":
        raise DiagnosticDenied("diagnostic request capability is invalid", code="MODEL_MISMATCH")
    if request.capability.profile_reference.value != record.credential_profile_identity:
        raise DiagnosticDenied("diagnostic credential profile identity mismatch", code="PROFILE_MISMATCH")
    kind, identities, texts = _envelopes(request)
    scope = (
        request.approved_corpus_id if isinstance(request, ProviderEmbeddingRequest)
        else "build:" + request.frozen_build_identity
    )
    if host_byte_binding is not None:
        host_byte_binding(record, facts, kind, identities, texts, scope)
    proof, basis, usage_units, cost_micro_units = safety(record, texts)
    adapter = GoogleEmbeddingAdapter(
        _GeminiEmbedding2OfflineClient(), request.capability, gemini_verification=proof,
    )
    try:
        contents, config = adapter._gemini_prepare(texts, request.limits)
    except Exception as exc:
        raise DiagnosticDenied("diagnostic request serialization or bounds failed") from exc
    decision = evaluate_diagnostic_safety(
        record, basis, kind, identities, texts, contents, config, proof,
        cost_micro_units, allow_synthetic=offline_harness,
    )
    # Equality binds the caller's assertion to this decision; it is not proof.
    # Only evaluate_diagnostic_safety establishes the applicable evidence scope.
    if type(usage_units) is not int or usage_units != decision.aggregate_tokens:
        raise DiagnosticDenied("usage reservation differs from approved input token bound")
    usage_units = decision.aggregate_tokens
    item_bytes, item_tokens, payload_bytes = (
        decision.item_bytes, decision.item_tokens, decision.payload_bytes,
    )
    validate_authorization(
        record, facts, live_opt_in=live_opt_in, kind=kind,
        input_scope_identity=scope,
        commitment=input_commitment(kind, identities, texts), item_count=len(texts),
        item_bytes=item_bytes, item_tokens=item_tokens,
        payload_bytes=payload_bytes, safety_basis=basis.bounds_basis,
        conservative_usage_units=usage_units,
        conservative_cost_micro_units=cost_micro_units,
    )
    return _PreparedEligibility(
        record, facts, adapter, kind, identities, texts, contents, config, scope,
        item_bytes, item_tokens, payload_bytes, decision, basis.bounds_basis, usage_units,
        cost_micro_units, live_opt_in,
    )


def _reserve(prepared: _PreparedEligibility, usage: TemporaryDiagnosticUsage,
             request_id: str, *, campaign_requirement: tuple[int, int, int, int] | None = None) -> None:
    """Persist an attempt only after all read-only checks; never dispatches."""
    if (type(prepared.usage_units) is not int
            or prepared.usage_units != prepared.safety_decision.aggregate_tokens):
        raise DiagnosticDenied("usage reservation differs from approved input token bound")
    reserve_eligible_attempt(
        prepared.record, prepared.facts, usage, request_id=request_id,
        live_opt_in=prepared.live_opt_in, kind=prepared.kind,
        input_scope_identity=prepared.input_scope_identity,
        commitment=input_commitment(prepared.kind, prepared.identities, prepared.texts),
        item_bytes=prepared.item_bytes, item_tokens=prepared.item_tokens,
        payload_bytes=prepared.payload_bytes, safety_basis=prepared.safety_basis,
        conservative_usage_units=prepared.usage_units,
        conservative_cost_micro_units=prepared.cost_micro_units,
        **({} if campaign_requirement is None else {"campaign_requirement": campaign_requirement}),
    )


def run_offline_simulation(
    source: OfflineAuthorizationSource,
    facts: DiagnosticExecutionFacts,
    request: ProviderEmbeddingRequest | QueryEmbeddingRequest,
    usage: TemporaryDiagnosticUsage,
    *,
    source_resolver: Callable[[], tuple[str, bool]],
    request_id: str,
    live_opt_in: bool,
    conservative_usage_units: int,
    conservative_cost_micro_units: int,
    approved_input: ApprovedDiagnosticInput | None = None,
    expected_sdk_evidence: DiagnosticSdkByteEvidence | None = None,
    mock_http_status: int = 200,
    mock_response_count: int | None = None,
    mock_transport_error: str | None = None,
    mock_response_mode: str = "ordered",
) -> DiagnosticObservation:
    """Run one offline E2E attempt with independently fixed synthetic E4 data.

    Missing evidence denies before reservation. Fixture observation is a separate
    operation; this entry never captures or approves its own expected bytes.
    """
    if type(source) is not OfflineAuthorizationSource:
        raise DiagnosticDenied("offline authorization source is unavailable")
    if not isinstance(facts, DiagnosticExecutionFacts) or type(usage) is not TemporaryDiagnosticUsage:
        raise DiagnosticDenied("diagnostic execution facts or usage record is unavailable")
    if (type(approved_input) is not ApprovedDiagnosticInput
            or type(expected_sdk_evidence) is not DiagnosticSdkByteEvidence
            or expected_sdk_evidence.status != "SYNTHETIC_OFFLINE_ONLY"):
        raise DiagnosticDenied("fixed synthetic offline input and SDK evidence are required")
    # Closed finite data fixtures only. No callbacks, client or transport injection.
    response = _DiagnosticMockResponse(mock_http_status, mock_transport_error,
                                       mock_response_count, mock_response_mode)
    if not callable(source_resolver):
        raise DiagnosticDenied("offline source resolver is unavailable")
    def offline_facts(_: DiagnosticAuthorizationRecord, __: Any) -> DiagnosticExecutionFacts:
        try:
            head, clean = source_resolver()
        except Exception as exc:
            raise DiagnosticDenied("offline source resolver failed") from exc
        if type(head) is not str or type(clean) is not bool:
            raise DiagnosticDenied("offline source facts are invalid")
        return replace(facts, source_revision=head, clean_tree=clean)

    def offline_safety(record: DiagnosticAuthorizationRecord, texts: tuple[str, ...]) -> tuple[
        GeminiEmbedding2Verification, DiagnosticSafetyBasis, int, int,
    ]:
        if record.bounds_basis != "SYNTHETIC_OFFLINE_ONLY" or record.cost_basis != "SYNTHETIC_OFFLINE_ONLY":
            raise DiagnosticDenied("offline simulation requires test-only safety bases")
        return (_offline_proof(record), _offline_safety_basis(record, texts),
                conservative_usage_units, conservative_cost_micro_units)

    prepared = _preflight(
        load_record=source.load, resolve_facts=offline_facts, safety=offline_safety,
        request=request, live_opt_in=live_opt_in, offline_harness=True,
    )
    _validate_sdk_evidence_binding(prepared, approved_input, expected_sdk_evidence,
                                   offline_harness=True)
    if (type(expected_sdk_evidence.schema_version) is not int
            or expected_sdk_evidence.schema_version != 1
            or expected_sdk_evidence.artifact_type != "SDK_REQUEST_BYTE_EVIDENCE"
            or any(type(getattr(expected_sdk_evidence, field)) is not int
                   for field in ("output_dimensionality", "timeout_ms", "attempts"))
            or (expected_sdk_evidence.valid_from is not None and (
                type(expected_sdk_evidence.valid_from) is not datetime
                or expected_sdk_evidence.valid_from.tzinfo is None
                or expected_sdk_evidence.valid_from >= datetime.now(timezone.utc)))
            or type(expected_sdk_evidence.sdk_body_bytes) is not int
            or not 0 < expected_sdk_evidence.sdk_body_bytes <= prepared.payload_bytes
            or type(expected_sdk_evidence.sdk_body_sha256) is not str
            or len(expected_sdk_evidence.sdk_body_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sdk_evidence.sdk_body_sha256)):
        raise DiagnosticDenied("fixed SDK raw byte evidence is invalid")
    # Re-run the same authority/safety checks immediately before reservation.
    # This is not a second evaluator or a new authority.
    refreshed = _preflight(
        load_record=source.load, resolve_facts=offline_facts, safety=offline_safety,
        request=request, live_opt_in=live_opt_in, offline_harness=True,
    )
    if refreshed.record != prepared.record or refreshed.safety_decision != prepared.safety_decision:
        raise DiagnosticDenied("offline authorization or safety changed before reservation")
    _validate_sdk_evidence_binding(refreshed, approved_input, expected_sdk_evidence,
                                   offline_harness=True)
    _reserve(refreshed, usage, request_id)
    return _run_offline_e2e(refreshed, usage, request_id, approved_input,
                            expected_sdk_evidence, response)


def _run_offline_e2e(
    prepared: _PreparedEligibility, usage: TemporaryDiagnosticUsage, request_id: str,
    approved: ApprovedDiagnosticInput, evidence: DiagnosticSdkByteEvidence,
    response: _DiagnosticMockResponse,
) -> DiagnosticObservation:
    """Shared source orchestration after durable reservation; no legacy fallback.

    Constructor/cleanup/crash leave IN_FLIGHT/UNKNOWN. Completion ambiguity is
    handled only by the existing ledger. Never repeat a send or completion.
    """
    try:
        handle = _create_diagnostic_offline_sdk_client(prepared, approved, evidence,
                                                       response=response)
    except Exception:
        raise DiagnosticDenied("offline client construction failed; reservation retained") from None
    try:
        observation = _offline_response(prepared, handle)
    finally:
        handle.close()
    usage.finish(prepared.record, request_id, observation.outcome)
    return observation


def _cleanup_offline_attempt(sdk: Any, sync: Any, async_client: Any) -> None:
    failed = False
    for client in (sdk, sync):
        try:
            client.close()
        except Exception:
            failed = True
    closing = None
    try:
        closing = async_client.aclose()
        try:
            closing.send(None)
        except StopIteration:
            pass
        else:
            # The isolated MockTransport must close synchronously. A suspension
            # cannot be treated as evidence that required cleanup completed.
            failed = True
    except Exception:
        failed = True
    finally:
        if closing is not None:
            try:
                closing.close()
            except Exception:
                failed = True
    if failed:
        raise DiagnosticDenied("offline cleanup failed; reservation retained") from None


def _offline_response(prepared: _PreparedEligibility, handle: Any) -> DiagnosticObservation:
    record, adapter = prepared.record, prepared.adapter
    identities, texts = prepared.identities, prepared.texts
    conservative_cost_micro_units = prepared.cost_micro_units
    try:
        response = handle.embed_prepared()
    except Exception as exc:
        code, field, detail, retry = adapter._gemini_invocation_failure(exc, query=True)
        outcome = "TIMEOUT" if "timeout" in field else "UNKNOWN" if field == "transport" else "FAILED"
        return DiagnosticObservation(
            outcome, field, code, field, detail, retry, handle._guard.delegations, identities, 0, "UNKNOWN",
            conservative_cost_micro_units, record.campaign_id, record.source_revision,
            record.input_commitment,
        )
    try:
        vectors = _values(response, strict_numbers=True)
        if len(vectors) != len(texts):
            failure_field = "response_cardinality"
        elif any(len(vector) != 768 for vector in vectors):
            failure_field = "response_dimension"
        elif any(abs(math.hypot(*vector) - 1.0) > record.normalization_tolerance
                 for vector in vectors):
            failure_field = "response_normalization"
        elif any(
            abs(vector[0] - math.cos(i * 0.01)) > 1e-6
            or abs(vector[1] - math.sin(i * 0.01)) > 1e-6
            for i, vector in enumerate(vectors)
        ):
            # Synthetic marker proof only; never provider order acceptance.
            failure_field = "response_order"
        else:
            failure_field = None
    except Exception:
        vectors = ()
        failure_field = "response_contract"
    if failure_field is not None:
        return DiagnosticObservation(
            "FAILED", failure_field, BuildFailureCode.PROVIDER_CONTRACT_INVALID,
            failure_field, "provider returned incompatible embedding facts",
            RetrySafetyDisposition.DO_NOT_RETRY, handle._guard.delegations, identities, 0, "UNKNOWN",
            conservative_cost_micro_units, record.campaign_id, record.source_revision,
            record.input_commitment,
        )
    return DiagnosticObservation(
        "SUCCESS", "success", None, None, None, None, handle._guard.delegations, identities,
        len(vectors), "UNKNOWN", conservative_cost_micro_units,
        record.campaign_id, record.source_revision, record.input_commitment,
    )


def _live_safety(
    record: DiagnosticAuthorizationRecord, texts: tuple[str, ...],
) -> tuple[GeminiEmbedding2Verification, DiagnosticSafetyBasis, int, int]:
    if (not isinstance(record, DiagnosticAuthorizationRecord)
            or record.bounds_basis == "SYNTHETIC_OFFLINE_ONLY" or record.cost_basis == "SYNTHETIC_OFFLINE_ONLY"):
        raise DiagnosticDenied("approved live provider safety basis is not provisioned")
    artifact = load_live_safety_artifact()
    if "sequential_batch_plan" in artifact.fields:
        raise DiagnosticDenied("fixed batch plan requires sequential campaign dispatch")
    return _safety_from_artifact(record, texts, artifact)


def _safety_from_artifact(record, texts, artifact):
    """Shared accepted-evidence evaluation; caller DTOs never enter run_live."""
    if not isinstance(record, DiagnosticAuthorizationRecord):
        raise DiagnosticDenied("approved live provider safety basis is not provisioned")
    if record.bounds_basis == "SYNTHETIC_OFFLINE_ONLY" or record.cost_basis == "SYNTHETIC_OFFLINE_ONLY":
        raise DiagnosticDenied("approved live provider safety basis is not provisioned")
    value = artifact._validated_fields()
    if value["binding"]["authorization_record_sha256"] != _authorization_record_digest(record):
        raise DiagnosticDenied("safety authorization changed")
    if is_single_document_probe(record):
        artifact.require_single_probe(record)
        permission = artifact.require_free_tier_permission(record)
        # Internal count callback supplies only the named admission risk cap.
        # It MUST NOT be published as model-compatible counting or actual usage.
        proof = GeminiEmbedding2Verification(
            count_tokens=lambda _: SINGLE_PROBE_RISK_CAP, serialize_payload=_serialized,
            max_item_tokens=SINGLE_PROBE_RISK_CAP, max_aggregate_tokens=SINGLE_PROBE_RISK_CAP,
            max_provider_items=1, max_item_bytes=record.maximum_item_bytes,
            max_payload_bytes=min(record.maximum_payload_bytes, 1_000_000),
            normalization_tolerance=record.normalization_tolerance,
            evidence_identity=value["artifact_revision"])
        basis = DiagnosticSafetyBasis("VERIFIED", value["artifact_revision"], record.campaign_id,
            record.source_revision, record.input_scope_identity, _authorization_record_digest(record),
            record.provider_identity, record.model_identity, record.sdk_baseline, record.api_version,
            "document", record.input_commitment, value["e4_reference"]["canonical_json_sha256"],
            record.bounds_basis, record.cost_basis, "PM_ACCEPTED_MODEL_LIMIT_RISK_CAP",
            None, None, None, None, "FREE_TIER_TEXT_V1", "USD", 0,
            datetime.fromisoformat(value["validity"]["expires_at"].replace("Z", "+00:00")), permission)
        return proof, basis, SINGLE_PROBE_RISK_CAP, 1
    bounds = artifact.require_accepted_evidence()
    free_permission = artifact.require_free_tier_permission(record)
    digests = tuple(sha256(text.encode("utf-8")).hexdigest() for text in texts)
    if set(bounds) != set(digests):
        raise DiagnosticDenied("accepted token bounds do not cover exact enveloped inputs")
    limits, pricing = value["provider_limits"], value["pricing_evidence"]
    total = sum(bounds[digest] for digest in digests)
    # Preserve positive, non-refundable attempt reservations in the existing
    # store. One micro-USD is a conservative budget hold, NOT a unit price,
    # known token usage or Google billing charge (the Free Tier rate is zero).
    cost = max(1, (total * pricing["rate_micro_units_per_million_tokens"] + 999_999) // 1_000_000)
    proof = GeminiEmbedding2Verification(
        count_tokens=lambda text: bounds[sha256(text.encode("utf-8")).hexdigest()],
        serialize_payload=_serialized,
        max_item_tokens=min(limits["per_item_token_limit"], record.maximum_item_tokens),
        max_aggregate_tokens=min(limits["aggregate_token_limit"], record.maximum_aggregate_tokens),
        max_provider_items=min(limits["item_limit"], record.maximum_items),
        max_item_bytes=record.maximum_item_bytes,
        max_payload_bytes=min(limits["payload_byte_limit"], record.maximum_payload_bytes),
        normalization_tolerance=record.normalization_tolerance,
        evidence_identity=value["artifact_revision"],
    )
    basis = DiagnosticSafetyBasis(
        "VERIFIED", value["artifact_revision"], record.campaign_id, record.source_revision,
        record.input_scope_identity, _authorization_record_digest(record),
        record.provider_identity, record.model_identity, record.sdk_baseline, record.api_version,
        record.input_kind, record.input_commitment, value["e4_reference"]["canonical_json_sha256"],
        record.bounds_basis, record.cost_basis, "MODEL_COMPATIBLE_UPPER_BOUND",
        limits["item_limit"], limits["per_item_token_limit"], limits["aggregate_token_limit"],
        limits["payload_byte_limit"], pricing["price_tier"], pricing["currency"],
        pricing["rate_micro_units_per_million_tokens"],
        datetime.fromisoformat(value["validity"]["expires_at"].replace("Z", "+00:00")),
        free_permission,
    )
    return proof, basis, total, cost


def _create_live_transport() -> httpx.BaseTransport:
    """Internal construction seam; public runner accepts no transport override."""
    return httpx.HTTPTransport(retries=0, trust_env=False)


class _DiagnosticLiveTransport(httpx.BaseTransport):
    """One synchronous send after final source/authority/body verification."""

    def __init__(self, prepared, approved, evidence, credential, revalidate, *, attempt_id=None) -> None:
        self._prepared, self._approved, self._evidence = prepared, approved, evidence
        self._credential, self._revalidate = credential, revalidate
        self._lock, self._entered = Lock(), False
        self.delegations = 0
        self._delegate = None
        self._prompt_tokens = None
        self._usage_response_sha256 = None
        self._attempt_id = attempt_id
        self._response_validated = False
        self._counting_tokens = None
        self._counting_response_sha256 = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not self._lock.acquire(blocking=False):
            raise DiagnosticDenied("diagnostic runner lock conflict", code="RUNNER_CONFLICT")
        try:
            if self._entered:
                raise DiagnosticDenied("diagnostic resend denied", code="RUNNER_CONFLICT")
            self._entered = True
            self._revalidate()
            if self._prepared.record.purpose == COUNTING_OPERATION:
                _compare_counting_bytes(self._prepared, self._evidence, request)
            else:
                compare_sdk_request_bytes(self._prepared, self._approved, self._evidence, request)
            if (request.headers.get("x-goog-api-key") != self._credential.value
                    or request.headers.get("x-server-timeout") != "30"
                    or request.headers.get("content-length") != str(len(request.content))
                    or any(k in request.headers for k in ("authorization", "proxy-authorization", "transfer-encoding"))):
                raise DiagnosticDenied("diagnostic request credential or header mismatch")
            self._delegate = _create_live_transport()
            self.delegations += 1
            response = self._delegate.handle_request(request)
            try:
                # Bound the provider body before the SDK decodes it. Never log
                # headers, provider messages, request objects or raw responses.
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise DiagnosticDenied("diagnostic provider response exceeds bound", code="INVALID_PROVIDER_RESPONSE")
                # Observe only the already guarded physical response. The
                # pinned SDK drops this field; no SDK patch/raw-body storage.
                if 200 <= response.status_code < 300:
                    if self._prepared.record.purpose == COUNTING_OPERATION:
                        self._counting_tokens = _counting_token_count(bytes(body))
                        self._counting_response_sha256 = sha256(body).hexdigest()
                    else:
                        self._prompt_tokens = _embedding_prompt_tokens(bytes(body))
                        if self._prompt_tokens is not None:
                            self._usage_response_sha256 = sha256(body).hexdigest()
                return httpx.Response(response.status_code, headers=response.headers,
                                      content=bytes(body), request=request)
            finally:
                response.close()
        finally:
            self._lock.release()

    def close(self) -> None:
        if self._delegate is not None:
            self._delegate.close()


def _dispatch_live(prepared, approved, evidence, credential, revalidate, *,
                   _vectors=None, _attempt_id=None, _usage_sink=None) -> DiagnosticObservation:
    guard = _DiagnosticLiveTransport(prepared, approved, evidence, credential, revalidate, attempt_id=_attempt_id)
    sdk = sync = async_client = None
    try:
        sync = _DiagnosticHttpxClient(guard)
        async_client = httpx.AsyncClient(transport=_DiagnosticAsyncDenyTransport(),
                                         trust_env=False, follow_redirects=False, max_redirects=0)
        sdk = _create_diagnostic_live_sdk_client(credential.value, sync, async_client)
        response = sdk.models.embed_content(model=GEMINI_MODEL,
            contents=[c.model_copy(deep=True) for c in prepared.contents],
            config=prepared.config.model_copy(deep=True))
        try:
            vectors = _validated_vectors(response, len(prepared.texts), strict_numbers=True,
                normalization_tolerance=prepared.record.normalization_tolerance)
        except Exception:
            raise DiagnosticDenied("invalid diagnostic provider response", code="INVALID_PROVIDER_RESPONSE") from None
        if (len(vectors) != len(prepared.texts) or any(len(v) != 768 or
                abs(math.hypot(*v) - 1.0) > prepared.record.normalization_tolerance for v in vectors)):
            raise DiagnosticDenied("invalid diagnostic provider vectors", code="INVALID_PROVIDER_RESPONSE")
        if _vectors is not None:
            _vectors.extend(zip(prepared.identities, vectors, strict=True))
        guard._response_validated = True
        receipt = None
        if (_attempt_id is not None and type(_usage_sink) is list
                and guard.delegations == 1 and guard._prompt_tokens is not None):
            receipt = _issue_provider_usage_receipt(guard)
            _usage_sink.append(receipt)
        # Positional vectors are observed, not a real mapping/order acceptance
        # proof. Never apply synthetic cosine markers to real provider output.
        return DiagnosticObservation("SUCCESS", "OBSERVED_MAPPING_REVIEW_REQUIRED", None, None,
            None, None, guard.delegations, prepared.identities, len(vectors),
            str(receipt.prompt_tokens) if receipt is not None else "UNKNOWN",
            prepared.cost_micro_units, prepared.record.campaign_id, prepared.record.source_revision,
            prepared.record.input_commitment,
            usage_provenance_sha256=receipt.provenance() if receipt is not None else None)
    except Exception as exc:
        code, field, detail, retry = prepared.adapter._gemini_invocation_failure(exc, query=True)
        if field == "provider_rate_limit" and getattr(exc, "status", None) == "RESOURCE_EXHAUSTED":
            field = "provider_quota"
        if isinstance(exc, DiagnosticDenied):
            field, detail = exc.code, "diagnostic local guard or response rejected"
        outcome = "TIMEOUT" if "timeout" in field.lower() else "UNKNOWN" if field == "transport" else "FAILED"
        return DiagnosticObservation(outcome, field, code, field, detail, retry,
            guard.delegations, prepared.identities, 0, "UNKNOWN", prepared.cost_micro_units,
            prepared.record.campaign_id, prepared.record.source_revision, prepared.record.input_commitment)
    finally:
        if _close_offline_resources(sdk, sync, async_client):
            raise DiagnosticDenied("diagnostic cleanup failed; attempt retained for human review") from None


_SEQUENTIAL_RUNNER_LOCK = Lock()


def _record_live_observation(usage, prepared, request_id, observation, receipts, evidence):
    """Only a validated physical response can complete formal numeric usage."""
    if observation.outcome == "SUCCESS" and len(receipts) == 1:
        receipt = receipts[0]
        usage._finish_provider_usage(prepared.record, request_id, observation.outcome,
                                     receipt, evidence.sdk_body_sha256)
        return observation
    usage.finish(prepared.record, request_id, observation.outcome)
    return replace(observation, usage="UNKNOWN", usage_provenance_sha256=None)


def _chunk_metadata_commitment(request: ProviderEmbeddingRequest) -> str:
    """Bind versions, local ordinals and build identity without exporting text."""
    return sha256(json.dumps([(c.chunk_identity, c.document_identity, c.document_version_identity,
        c.section_identity, c.ordinal, c.content_hash, c.metadata_commitment) for c in request.chunks],
        ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _prepare_sequential_batches(request, binding, artifact, resolve_facts):
    """Project only approved contiguous slices; projections are NOT new grants.

    PM's existing bounds_basis binds the entire immutable plan digest. Child
    records retain campaign/source/profile/budgets and change input commitment
    only. E4 for EVERY child is independently prepared in the protected file.
    """
    record, approved = binding.record, binding.approved_input
    if not isinstance(request, ProviderEmbeddingRequest):
        raise DiagnosticDenied("sequential diagnostic requires approved document input")
    kind, identities, texts = _envelopes(request)
    if (identities != approved.identities or texts != approved.envelopes
            or len(identities) != 54 or len(set(identities)) != 54
            or len({c.document_identity for c in request.chunks}) != 6):
        raise DiagnosticDenied("sequential diagnostic requires six sources and 54 unique ordered chunks", code="INPUT_MISMATCH")
    facts = resolve_facts(record, request)
    artifact.matches_record_input(record, approved, binding.sdk_byte_evidence, facts.source_revision)
    plan = artifact.batch_plan()
    plan.matches_record_input(record, approved)
    fields = plan.fields
    if (fields["embedding_profile_identity"] != request.capability.embedding_profile_identity
            or fields["build_identity"] != request.build_identity
            or fields["chunk_metadata_commitment"] != _chunk_metadata_commitment(request)):
        raise DiagnosticDenied("batch build, chunk version or embedding profile mismatch", code="PROFILE_MISMATCH")
    proof, basis, _, _ = _safety_from_artifact(record, texts, artifact)
    # Enforce the intersection of accepted provider limits and approved plan.
    proof = replace(proof,
        max_provider_items=min(proof.max_provider_items, fields["maximum_batch_items"]),
        max_aggregate_tokens=min(proof.max_aggregate_tokens, fields["maximum_batch_tokens"]),
        max_payload_bytes=min(proof.max_payload_bytes, fields["maximum_batch_payload_bytes"]))
    evidence = artifact.batch_sdk_evidence()
    prepared = []
    for ordinal, (start, end, commitment) in enumerate(plan.ranges):
        child_record = replace(record, input_commitment=commitment)
        child_input = replace(approved, identities=identities[start:end],
                              envelopes=texts[start:end], commitment=commitment)
        chunks = request.chunks[start:end]
        documents = {c.document_identity for c in chunks}
        child_request = replace(request, chunks=chunks,
            approved_document_ids=tuple(pair for pair in request.approved_document_ids if pair[0] in documents))
        child_e4 = evidence[ordinal]
        child_e4.matches_record_input(child_record, child_input)
        child_proof = proof
        child_basis = replace(basis, input_commitment=commitment,
            authorization_record_sha256=_authorization_record_digest(child_record),
            serialized_request_sha256=child_e4.canonical_json_sha256,
            provider_item_limit=proof.max_provider_items,
            provider_aggregate_token_limit=proof.max_aggregate_tokens,
            provider_payload_byte_limit=proof.max_payload_bytes)
        tokens = sum(proof.count_tokens(t) for t in texts[start:end])
        cost = max(1, (tokens * basis.price_micro_units_per_million_tokens + 999_999) // 1_000_000)
        item = _preflight(load_record=lambda r=child_record: r, resolve_facts=resolve_facts,
            safety=lambda r, t, p=child_proof, b=child_basis, u=tokens, c=cost: (p, b, u, c),
            request=child_request, live_opt_in=True,
            host_byte_binding=lambda r, f, k, ids, ts, s, a=child_input, e=child_e4:
                validate_host_sdk_evidence(r, f, a, e, k, ids, ts, s))
        _validate_sdk_evidence_binding(item, child_input, child_e4)
        prepared.append((item, child_input, child_e4))
    total_tokens = sum(item.usage_units for item, _, _ in prepared)
    quota = fields["quota"]
    # Conservative admission: ALL planned requests fit one declared quota
    # window. Accounting units require reviewed source evidence; never infer
    # that one HTTP send consumes one RPM. No clock/scheduler/quota domain.
    if (len(prepared) > record.maximum_invocations
            or sum(len(item.texts) for item, _, _ in prepared) > record.maximum_items
            or total_tokens > record.maximum_usage_units or total_tokens > quota["tpm"]
            or len(prepared) * quota["request_units_per_invocation"] > min(quota["rpm"], quota["rpd"])
            or sum(item.cost_micro_units for item, _, _ in prepared) > record.maximum_cost_micro_units):
        raise DiagnosticDenied("batch campaign token, invocation, quota or cost bound exceeded", code="INPUT_BOUNDS_EXCEEDED")
    return plan, tuple(prepared)


def _validate_global_batch_vectors(request, collected, tolerance):
    expected = tuple(c.chunk_identity for c in request.chunks)
    if (len(collected) != 54 or len(expected) != 54 or len(set(expected)) != 54
            or tuple(identity for identity, _ in collected) != expected
            or any(len(vector) != 768 or any(type(n) not in (int, float) or not math.isfinite(n) for n in vector)
                   or not math.isfinite(math.hypot(*vector)) or math.hypot(*vector) == 0
                   or abs(math.hypot(*vector) - 1.0) > tolerance for _, vector in collected)):
        raise DiagnosticDenied("global 54-to-54 diagnostic validation failed", code="INVALID_PROVIDER_RESPONSE")


def _run_sequential_campaign(request, binding, artifact, resolve_facts):
    if not _SEQUENTIAL_RUNNER_LOCK.acquire(blocking=False):
        raise DiagnosticDenied("sequential diagnostic runner conflict", code="RUNNER_CONFLICT")
    try:
        plan, batches = _prepare_sequential_batches(request, binding, artifact, resolve_facts)
        usage, collected, observations = LiveDiagnosticUsage(), [], []
        for ordinal, (prepared, approved, evidence) in enumerate(batches):
            def refresh():
                if load_live_binding() != binding or load_live_safety_artifact().fields != artifact.fields:
                    raise DiagnosticDenied("batch source, plan or campaign authority changed")
                current = replace(prepared, facts=resolve_facts(prepared.record, request))
                validate_authorization(current.record, current.facts, live_opt_in=True,
                    kind=current.kind, input_scope_identity=current.input_scope_identity,
                    commitment=input_commitment(current.kind, current.identities, current.texts),
                    item_count=len(current.texts), item_bytes=current.item_bytes,
                    item_tokens=current.item_tokens, payload_bytes=current.payload_bytes,
                    safety_basis=current.safety_basis, conservative_usage_units=current.usage_units,
                    conservative_cost_micro_units=current.cost_micro_units)
                return current

            prepared = refresh()
            remaining = batches[ordinal:]
            required = (len(remaining), sum(len(p.texts) for p, _, _ in remaining),
                        sum(p.usage_units for p, _, _ in remaining), sum(p.cost_micro_units for p, _, _ in remaining))
            # Stable IDs deny duplicate/restart of a successful slice; this
            # entry always starts at ordinal zero and has no resume/cache API.
            request_id = "batch." + plan.commitment + "." + str(ordinal)
            _reserve(prepared, usage, request_id, campaign_requirement=required)
            try:
                credential = resolve_diagnostic_credential(binding.record, artifact)
            except DiagnosticDenied:
                usage.finish(prepared.record, request_id, "FAILED")
                raise
            except Exception:
                usage.finish(prepared.record, request_id, "FAILED")
                raise DiagnosticDenied("batch credential resolution failed; attempt retained",
                                       code="CREDENTIAL_UNAVAILABLE") from None
            receipts = []
            observation = _dispatch_live(prepared, approved, evidence, credential, refresh,
                _vectors=collected, _attempt_id=request_id, _usage_sink=receipts)
            observation = _record_live_observation(usage, prepared, request_id, observation, receipts, evidence)
            observations.append(observation)
            if observation.outcome != "SUCCESS":
                return replace(observation, vector_count=0,
                    provider_attempts=sum(o.provider_attempts for o in observations),
                    ordered_identities=binding.approved_input.identities,
                    input_commitment=binding.record.input_commitment,
                    cost_estimate_micro_units=sum(o.cost_estimate_micro_units for o in observations))
            # Durable validated provider usage must exist before continuation.
            # Missing metadata keeps the existing UNKNOWN/adjudication fence.
            state = usage._completion_state(prepared.record, request_id)
            if (state is None or state[0] != "SUCCESS" or state[1] == "UNKNOWN"
                    or not state[1].isdigit() or int(state[1]) > prepared.usage_units):
                return replace(observation, outcome="BLOCKED", category="UNKNOWN_USAGE",
                    failure_code=BuildFailureCode.PROVIDER_UNAVAILABLE,
                    failure_field="UNKNOWN_USAGE", failure_detail="batch usage requires human adjudication",
                    retry_safety=RetrySafetyDisposition.DO_NOT_RETRY, vector_count=0,
                    provider_attempts=sum(o.provider_attempts for o in observations),
                    ordered_identities=binding.approved_input.identities,
                    cost_estimate_micro_units=sum(o.cost_estimate_micro_units for o in observations),
                    input_commitment=binding.record.input_commitment)
        _validate_global_batch_vectors(request, collected, binding.record.normalization_tolerance)
        return replace(observations[-1], category="BATCHED_OBSERVATION_MAPPING_REVIEW_REQUIRED",
            ordered_identities=tuple(identity for identity, _ in collected), vector_count=54,
            provider_attempts=sum(o.provider_attempts for o in observations),
            cost_estimate_micro_units=sum(o.cost_estimate_micro_units for o in observations),
            usage=str(sum(int(o.usage) for o in observations))
                if all(o.usage.isdigit() for o in observations) else "UNKNOWN",
            usage_provenance_sha256=sha256(json.dumps(
                [o.usage_provenance_sha256 for o in observations], separators=(",", ":")).encode()).hexdigest()
                if all(o.usage_provenance_sha256 is not None for o in observations) else None,
            input_commitment=binding.record.input_commitment)
    finally:
        _SEQUENTIAL_RUNNER_LOCK.release()


def run_live(
    request: ProviderEmbeddingRequest | QueryEmbeddingRequest | None = None,
    *, live_opt_in: bool = False, operation: str = "CONTROLLED_PROVIDER_DIAGNOSTIC",
) -> DiagnosticObservation:
    """Load protected campaign facts; reserve once before controlled dispatch.

    This public entry accepts no source, resolver, safety proof, ledger, client,
    or transport override. Missing host/PM/safety facts deny before credentials.
    """
    if operation == COUNTING_OPERATION:
        if request is not None:
            raise DiagnosticDenied("counting takes only its fixed repository input", code="INPUT_MISMATCH")
        return run_counting_observation(live_opt_in=live_opt_in)
    if operation != "CONTROLLED_PROVIDER_DIAGNOSTIC":
        raise DiagnosticDenied("diagnostic operation is invalid", code="OPERATION_MISMATCH")
    if live_opt_in is not True:
        raise DiagnosticDenied("explicit live diagnostic opt-in is required")
    binding = load_live_binding()
    record = binding.record
    if record.purpose != "CONTROLLED_PROVIDER_DIAGNOSTIC":
        raise DiagnosticDenied("counting cannot dispatch embedding", code="OPERATION_MISMATCH")
    if not isinstance(request, (ProviderEmbeddingRequest, QueryEmbeddingRequest)):
        raise DiagnosticDenied("authorized diagnostic input is not available")
    kind, identities, envelopes = _envelopes(request)
    scope = (request.approved_corpus_id if kind == "document"
             else "build:" + request.frozen_build_identity)
    approved = binding.approved_input
    if (
        kind != approved.kind or scope != approved.scope_identity
        or identities != approved.identities or envelopes != approved.envelopes
        or input_commitment(kind, identities, envelopes) != approved.commitment
    ):
        raise DiagnosticDenied("actual request differs from host-approved diagnostic input", code="INPUT_MISMATCH")

    def live_facts(approved: DiagnosticAuthorizationRecord, actual: Any) -> DiagnosticExecutionFacts:
        head, clean = resolve_repository_source(Path(__file__).resolve().parents[2])
        host = socket.gethostname()
        if host != approved.authorized_host:
            raise DiagnosticDenied("diagnostic Windows host identity does not match authorization", code="HOST_MISMATCH")
        return DiagnosticExecutionFacts(
            source_revision=head, clean_tree=clean,
            pm_approval_reference=approved.pm_approval_reference,
            spec_lead_identity=approved.spec_lead_identity,
            runner_identity=binding.runner_sid, host_identity=host,
            credential_profile_identity=actual.capability.profile_reference.value,
            now=datetime.now(timezone.utc),
        )

    if record.bounds_basis.startswith("SEQUENTIAL_BATCH_PLAN_V1:"):
        artifact = load_live_safety_artifact()
        return _run_sequential_campaign(request, binding, artifact, live_facts)

    def prepare():
        return _preflight(
            load_record=lambda: record, resolve_facts=live_facts,
            safety=_live_safety, request=request, live_opt_in=live_opt_in,
            host_byte_binding=lambda approved_record, facts, actual_kind, actual_ids,
                                     actual_texts, actual_scope: validate_host_sdk_evidence(
                approved_record, facts, approved, binding.sdk_byte_evidence,
                actual_kind, actual_ids, actual_texts, actual_scope,
            ),
        )

    prepared = prepare()
    if prepared.kind == "document" and len(prepared.identities) != (1 if is_single_document_probe(record) else 54):
        raise DiagnosticDenied("diagnostic document request requires exactly 54 chunks")
    artifact = load_live_safety_artifact()
    artifact.matches_record_input(record, approved, binding.sdk_byte_evidence, prepared.facts.source_revision)
    if is_single_document_probe(record):
        artifact.require_single_probe(record, approved, binding.sdk_byte_evidence)
    else:
        artifact.require_accepted_evidence()
    usage = LiveDiagnosticUsage()
    refreshed = prepare()
    if (prepared.record != refreshed.record or prepared.safety_decision != refreshed.safety_decision):
        raise DiagnosticDenied("diagnostic authority or safety changed before attempt")
    _validate_sdk_evidence_binding(refreshed, approved, binding.sdk_byte_evidence)
    request_id = uuid4().hex
    # MAJOR-01: existing SQLite eligibility + reservation is one atomic gate.
    # Cumulative budget and active/unknown runner fences must reject BEFORE
    # credential discovery, not through a separate racy read-only check.
    _reserve(refreshed, usage, request_id)
    try:
        credential = resolve_diagnostic_credential(record, artifact)
    except DiagnosticDenied:
        # The committed attempt/budget is never refunded or retried. Unknown
        # usage keeps the campaign fenced for human review even without I/O.
        usage.finish(record, request_id, "FAILED")
        raise
    except Exception:
        usage.finish(record, request_id, "FAILED")
        raise DiagnosticDenied("diagnostic credential resolution failed", code="CREDENTIAL_UNAVAILABLE") from None

    def revalidate():
        current = load_live_binding()
        if current != binding:
            raise DiagnosticDenied("diagnostic host authorization changed before transport")
        latest = prepare()
        if latest.record != refreshed.record or latest.safety_decision != refreshed.safety_decision:
            raise DiagnosticDenied("diagnostic source or safety changed before transport")
        if load_live_safety_artifact().fields != artifact.fields:
            raise DiagnosticDenied("diagnostic safety metadata changed before transport")

    receipts = []
    observation = _dispatch_live(refreshed, approved, binding.sdk_byte_evidence, credential, revalidate,
        _attempt_id=request_id, _usage_sink=receipts)
    observation = _record_live_observation(usage, refreshed, request_id, observation, receipts, binding.sdk_byte_evidence)
    if is_single_document_probe(record):
        observation = replace(observation,
            category="PROBE_OBSERVATION_ONLY" if observation.outcome == "SUCCESS" else observation.category,
            admission_usage_basis="PM_ACCEPTED_MODEL_LIMIT_RISK_CAP",
            billing_status="INTERNAL_HOLD_NOT_GOOGLE_BILLING_OR_WORST_COST",
            truncation_status="TRUNCATION VERIFICATION LIMITATION")
    return observation


@dataclass(frozen=True, slots=True)
class CountingObservation:
    """Counting result/evidence, NEVER embedding usage or compatibility PASS."""
    outcome: str
    code: str
    observed_token_count: int | None
    provider_attempts: int
    evidence: Any
    usage: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class _PreparedCounting:
    record: DiagnosticAuthorizationRecord
    facts: DiagnosticExecutionFacts
    contents: tuple[Any, ...]
    body: bytes
    cost_micro_units: int = 1


def _counting_content():
    """Read approved sources and derive chunks; no build/claim/store mutation."""
    from .manifest import admit_production_manifest
    from .chunking import plan_chunks
    from .config import load_knowledge_config
    root = Path(__file__).resolve().parents[2]
    try:
        config = load_knowledge_config(root / "configs/knowledge_index.yaml")
        admission = admit_production_manifest("configs/knowledge_manifest.json", repository_root=root)
        chunks = plan_chunks(admission, source_root=root / "docs/knowledge", profile=config.chunking)
        mapping = {document_identity(admission.manifest.corpus_id, d.document_id): d.document_id
                   for d in admission.manifest.documents}
        identities = tuple(c.chunk_identity for c in chunks)
        texts = tuple(f"title: {mapping[c.document_identity]} | text: {c.content}" for c in chunks)
        if (len(chunks) != 54 or len(set(identities)) != 54 or len(mapping) != 6
                or input_commitment("document", identities, texts) != COUNTING_CORPUS
                or identities[52] != COUNTING_CHUNK
                or mapping[chunks[52].document_identity] != "ops.shared-database-network-dependency-interruption"
                or next(d for d in admission.manifest.documents
                        if d.document_id == mapping[chunks[52].document_identity]).document_version != "1.0"):
            raise ValueError("candidate differs")
        return admission.manifest.corpus_id, texts[52]
    except Exception:
        raise DiagnosticDenied("approved counting corpus or chunk mismatch", code="INPUT_MISMATCH") from None


def _serialize_counting(envelope):
    try:
        contents, body, route = _prepare_counting_sdk_request(envelope)
    except DiagnosticDenied:
        raise
    except Exception:
        raise DiagnosticDenied("counting SDK serialization unavailable", code="PAYLOAD_MISMATCH") from None
    if (route != {"model": "models/" + GEMINI_MODEL} or len(envelope.encode()) != 528
            or len(body) != 591 or sha256(body).hexdigest() != COUNTING_BODY):
        raise DiagnosticDenied("counting SDK payload differs from approved candidate", code="PAYLOAD_MISMATCH")
    return contents, body


def _prepare_counting(binding, artifact):
    record, approved, e4 = binding.record, binding.approved_input, binding.sdk_byte_evidence
    # Revalidate DTOs as well as fixed-host parsed records.
    record.__post_init__()
    if record.purpose != COUNTING_OPERATION:
        raise DiagnosticDenied("counting authorization operation mismatch", code="OPERATION_MISMATCH")
    scope, envelope = _counting_content()
    if (approved.kind != "document" or approved.scope_identity != scope
            or record.input_scope_identity != scope or approved.identities != (COUNTING_CHUNK,)
            or approved.envelopes != (envelope,)
            or input_commitment("document", approved.identities, approved.envelopes) != COUNTING_INPUT):
        raise DiagnosticDenied("counting approved input mismatch", code="INPUT_MISMATCH")
    contents, body = _serialize_counting(envelope)
    revision, clean = resolve_repository_source(Path(__file__).resolve().parents[2])
    now = datetime.now(timezone.utc)
    if not clean or revision != record.source_revision:
        raise DiagnosticDenied("counting source is dirty or unapproved", code="SOURCE_MISMATCH")
    if (binding.runner_sid != record.runner_identity or socket.gethostname() != record.authorized_host):
        raise DiagnosticDenied("counting host or runner mismatch", code="AUTHORIZATION_DENIED")
    if not _timestamp(record.valid_from) <= now < _timestamp(record.expires_at):
        raise DiagnosticDenied("counting authorization expired", code="AUTHORIZATION_EXPIRED")
    artifact.matches_record_input(record, approved, e4, revision)
    if (e4.sdk_body_sha256 != sha256(body).hexdigest()
            or e4.canonical_json_sha256 != sha256(_canonical_request(body)).hexdigest()
            or e4.sdk_body_bytes != len(body) or e4.output_dimensionality is not None):
        raise DiagnosticDenied("counting E4 payload mismatch", code="PAYLOAD_MISMATCH")
    facts = DiagnosticExecutionFacts(revision, clean, record.pm_approval_reference,
        record.spec_lead_identity, binding.runner_sid, record.authorized_host,
        record.credential_profile_identity, now)
    return _PreparedCounting(record, facts, contents, body)


def _compare_counting_bytes(prepared, evidence, request):
    if (prepared.record.purpose != COUNTING_OPERATION or request.method != "POST"
            or str(request.url) != COUNTING_URL or request.content != prepared.body
            or sha256(request.content).hexdigest() != COUNTING_BODY
            or evidence.sdk_body_sha256 != COUNTING_BODY
            or request.extensions.get("timeout") != dict(connect=30.0, read=30.0, write=30.0, pool=30.0)):
        raise DiagnosticDenied("actual counting request mismatch", code="PAYLOAD_MISMATCH")


def _counting_token_count(body):
    from .gemini_diagnostic_controls import _safety_unique
    try:
        if type(body) is not bytes or not 0 < len(body) <= 1_000_000:
            raise ValueError("size")
        def invalid_constant(_):
            raise ValueError("invalid JSON constant")
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_safety_unique,
                           parse_constant=invalid_constant)
        count = value.get("totalTokens") if type(value) is dict else None
        if type(count) is not int or not 0 <= count <= 2**63 - 1:
            raise ValueError("count")
        return count
    except Exception:
        raise DiagnosticDenied("counting response is invalid", code="INVALID_PROVIDER_RESPONSE") from None


def _dispatch_counting(prepared, binding, credential, revalidate, request_id):
    """Same controlled SDK/client/physical guard as embedding, one operation."""
    guard = _DiagnosticLiveTransport(prepared, binding.approved_input, binding.sdk_byte_evidence,
                                     credential, revalidate, attempt_id=request_id)
    sdk = sync = async_client = None
    outcome, code = "UNKNOWN", "UNKNOWN"
    try:
        sync = _DiagnosticHttpxClient(guard)
        async_client = httpx.AsyncClient(transport=_DiagnosticAsyncDenyTransport(),
                                         trust_env=False, follow_redirects=False, max_redirects=0)
        sdk = _create_diagnostic_live_sdk_client(credential.value, sync, async_client)
        api, opts = sdk._api_client, sdk._api_client._http_options
        if (api.vertexai is not False or opts.api_version != "v1" or opts.timeout != 30_000
                or opts.retry_options.attempts != 1 or api._retry.stop.max_attempt_number != 1
                or api._httpx_client is not sync or api._async_httpx_client is not async_client
                or opts.base_url.rstrip("/") != "https://generativelanguage.googleapis.com"):
            raise DiagnosticDenied("counting SDK policy mismatch", code="AUTHORIZATION_DENIED")
        response = sdk.models.count_tokens(model=GEMINI_MODEL,
            contents=[c.model_copy(deep=True) for c in prepared.contents])
        if (guard.delegations != 1 or guard._counting_tokens is None
                or type(response.total_tokens) is not int or response.total_tokens != guard._counting_tokens):
            raise DiagnosticDenied("counting response boundary mismatch", code="INVALID_PROVIDER_RESPONSE")
        guard._response_validated = True
        outcome, code = "SUCCESS", "COUNTING_RESULT_OBTAINED"
    except Exception as exc:
        if isinstance(exc, DiagnosticDenied):
            outcome, code = "FAILED", exc.code
        elif isinstance(exc, httpx.TimeoutException):
            outcome, code = "TIMEOUT", "TRANSPORT_TIMEOUT"
        elif getattr(exc, "code", None) == 429:
            outcome, code = "FAILED", "RATE_LIMIT_QUOTA"
        elif getattr(exc, "code", None) == 404:
            outcome, code = "FAILED", "UNSUPPORTED_MODEL"
        elif getattr(exc, "code", None) in (400, 405):
            outcome, code = "FAILED", "UNSUPPORTED_OPERATION"
        else:
            outcome, code = "UNKNOWN", "TRANSPORT_FAILURE"
    finally:
        if _close_offline_resources(sdk, sync, async_client):
            outcome, code = "UNKNOWN", "UNKNOWN"
    return outcome, code, guard


def run_counting_observation(*, live_opt_in=False):
    """Fixed host authority only. Tests replace internal I/O with sealed mocks."""
    from types import MappingProxyType
    if live_opt_in is not True:
        raise DiagnosticDenied("explicit counting live opt-in is required")
    if not _SEQUENTIAL_RUNNER_LOCK.acquire(blocking=False):
        raise DiagnosticDenied("diagnostic runner conflict", code="RUNNER_CONFLICT")
    try:
        binding, artifact = load_live_binding(), load_live_safety_artifact()
        prepared = _prepare_counting(binding, artifact)
        record = prepared.record
        usage = LiveDiagnosticUsage()
        request_id = "counting." + COUNTING_BODY
        # No token estimates: usage_reserved=1 means an operation unit, NOT a token.
        usage.reserve(record, request_id, items=1, usage_units=1, cost_micro_units=1)
        try:
            credential = resolve_diagnostic_credential(record, artifact)
        except Exception as exc:
            code = exc.code if isinstance(exc, DiagnosticDenied) else "CREDENTIAL_UNAVAILABLE"
            usage._finish_counting(record, request_id, "FAILED", code)
            raise DiagnosticDenied("counting credential unavailable", code=code) from None
        def revalidate():
            current, safety = load_live_binding(), load_live_safety_artifact()
            if current != binding or safety.fields != artifact.fields:
                raise DiagnosticDenied("counting authority changed before transport")
            latest = _prepare_counting(current, safety)
            if latest.body != prepared.body or latest.record != record:
                raise DiagnosticDenied("counting final safety mismatch")
        outcome, code, guard = _dispatch_counting(prepared, binding, credential, revalidate, request_id)
        evidence = usage._finish_counting(record, request_id, outcome, code, guard)
        return CountingObservation(outcome, code, evidence["observed_token_count"],
                                   guard.delegations, MappingProxyType(evidence))
    finally:
        _SEQUENTIAL_RUNNER_LOCK.release()


def main() -> int:
    """Secret-safe CLI default: never probes credentials or Google."""
    try:
        run_live()
    except DiagnosticDenied as exc:
        print(json.dumps({"outcome": "DENIED", "code": exc.code, "detail": str(exc), "provider_attempts": 0}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
