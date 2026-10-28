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
)
from .google_embedding_adapter import (
    GEMINI_MODEL, GeminiEmbedding2Verification, GoogleEmbeddingAdapter,
    _GeminiEmbedding2OfflineClient, _create_gemini_offline_mock_client,
    _DiagnosticGuardedMockTransport, _DiagnosticMockResponse,
    _create_guarded_diagnostic_offline_sdk_client, _values,
    _DiagnosticHttpxClient, _DiagnosticAsyncDenyTransport,
    _close_offline_resources, _create_diagnostic_live_sdk_client,
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
        or basis.token_method != ("SYNTHETIC_BYTES_NOT_TOKENS" if allow_synthetic
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
             request_id: str) -> None:
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
    if not isinstance(record, DiagnosticAuthorizationRecord):
        raise DiagnosticDenied("approved live provider safety basis is not provisioned")
    if record.bounds_basis == "SYNTHETIC_OFFLINE_ONLY" or record.cost_basis == "SYNTHETIC_OFFLINE_ONLY":
        raise DiagnosticDenied("approved live provider safety basis is not provisioned")
    artifact = load_live_safety_artifact()
    value = artifact._validated_fields()
    if value["binding"]["authorization_record_sha256"] != _authorization_record_digest(record):
        raise DiagnosticDenied("safety authorization changed")
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

    def __init__(self, prepared, approved, evidence, credential, revalidate) -> None:
        self._prepared, self._approved, self._evidence = prepared, approved, evidence
        self._credential, self._revalidate = credential, revalidate
        self._lock, self._entered = Lock(), False
        self.delegations = 0
        self._delegate = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not self._lock.acquire(blocking=False):
            raise DiagnosticDenied("diagnostic runner lock conflict", code="RUNNER_CONFLICT")
        try:
            if self._entered:
                raise DiagnosticDenied("diagnostic resend denied", code="RUNNER_CONFLICT")
            self._entered = True
            self._revalidate()
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
                return httpx.Response(response.status_code, headers=response.headers,
                                      content=bytes(body), request=request)
            finally:
                response.close()
        finally:
            self._lock.release()

    def close(self) -> None:
        if self._delegate is not None:
            self._delegate.close()


def _dispatch_live(prepared, approved, evidence, credential, revalidate) -> DiagnosticObservation:
    guard = _DiagnosticLiveTransport(prepared, approved, evidence, credential, revalidate)
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
            vectors = _values(response, strict_numbers=True)
        except Exception:
            raise DiagnosticDenied("invalid diagnostic provider response", code="INVALID_PROVIDER_RESPONSE") from None
        if (len(vectors) != len(prepared.texts) or any(len(v) != 768 or
                abs(math.hypot(*v) - 1.0) > prepared.record.normalization_tolerance for v in vectors)):
            raise DiagnosticDenied("invalid diagnostic provider vectors", code="INVALID_PROVIDER_RESPONSE")
        # Positional vectors are observed, not a real mapping/order acceptance
        # proof. Never apply synthetic cosine markers to real provider output.
        return DiagnosticObservation("SUCCESS", "OBSERVED_MAPPING_REVIEW_REQUIRED", None, None,
            None, None, guard.delegations, prepared.identities, len(vectors), "UNKNOWN",
            prepared.cost_micro_units, prepared.record.campaign_id, prepared.record.source_revision,
            prepared.record.input_commitment)
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


def run_live(
    request: ProviderEmbeddingRequest | QueryEmbeddingRequest | None = None,
    *, live_opt_in: bool = False,
) -> DiagnosticObservation:
    """Load protected campaign facts; reserve once before controlled dispatch.

    This public entry accepts no source, resolver, safety proof, ledger, client,
    or transport override. Missing host/PM/safety facts deny before credentials.
    """
    if live_opt_in is not True:
        raise DiagnosticDenied("explicit live diagnostic opt-in is required")
    binding = load_live_binding()
    record = binding.record
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
    if prepared.kind == "document" and len(prepared.identities) != 54:
        raise DiagnosticDenied("diagnostic document request requires exactly 54 chunks")
    artifact = load_live_safety_artifact()
    artifact.matches_record_input(record, approved, binding.sdk_byte_evidence, prepared.facts.source_revision)
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

    observation = _dispatch_live(refreshed, approved, binding.sdk_byte_evidence, credential, revalidate)
    usage.finish(record, request_id, observation.outcome)
    return observation


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
