"""Strict, bounded structured-output decoding without provider authority."""

from __future__ import annotations

from dataclasses import fields
import json
import re
from typing import Any

from rca_persistence.contracts import DiagnosticConclusion, EvidenceCompleteness, EvidentialSupport, GuidanceSource
from rca_shared.claim_types import ClaimCategory

from .config import GenerationConfig
from .contracts import (
    CausalAssertion, CausalRelation, Claim, EvidenceReference, GenerationInput, Guidance, Hypothesis,
    InvocationMetadata, KnowledgeReference, ResultContent, ValidationFact, safe_text,
)


class StructuredOutputError(ValueError):
    """Malformed, unknown, unsafe or incompatible result structure."""


_FORBIDDEN_CONTEXT = re.compile(
    r"(?i)(scenario[_ -]?id|\bS[1-6]\b|generator[_ -]?expected|validator[_ -]?expected|base64\s*:)"
)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise StructuredOutputError("structured output has duplicate fields")
        value[key] = item
    return value


def _object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise StructuredOutputError(f"{label} has missing or unsupported fields")
    return value


def _items(value: object, maximum: int, label: str) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise StructuredOutputError(f"{label} must be a bounded array")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    return tuple(safe_text(item, label, 160, identifier=True) for item in _items(value, 64, label))


def _secret_tree(value: object, maximum_text_bytes: int) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            safe_text(key, "structured field", 160)
            _secret_tree(item, maximum_text_bytes)
    elif isinstance(value, list):
        for item in value:
            _secret_tree(item, maximum_text_bytes)
    elif isinstance(value, str):
        safe_text(value, "structured value", maximum_text_bytes)
        if _FORBIDDEN_CONTEXT.search(value):
            raise StructuredOutputError("structured output contains forbidden evaluation context")
    elif value is not None and type(value) not in (bool, int):
        raise StructuredOutputError("structured output contains an unsupported scalar")


def parse_structured_output(
    raw: str | bytes | dict[str, object],
    source: GenerationInput,
    config: GenerationConfig,
    invocation: InvocationMetadata,
) -> ResultContent:
    """Decode an exact schema. The caller supplies input and accounting authority."""

    if not isinstance(source, GenerationInput) or not isinstance(config, GenerationConfig) or not isinstance(invocation, InvocationMetadata):
        raise TypeError("pinned input, configuration and sanitized invocation facts are required")
    try:
        if isinstance(raw, bytes):
            if len(raw) > config.bounds.maximum_output_bytes:
                raise StructuredOutputError("structured output exceeds its bound")
            raw = raw.decode("utf-8", errors="strict")
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > config.bounds.maximum_output_bytes:
                raise StructuredOutputError("structured output exceeds its bound")
            value = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(StructuredOutputError("non-finite scalar")))
        elif isinstance(raw, dict):
            value = raw
            if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > config.bounds.maximum_output_bytes:
                raise StructuredOutputError("structured output exceeds its bound")
        else:
            raise StructuredOutputError("structured output must be JSON or an object")
        _secret_tree(value, config.bounds.maximum_text_bytes)
        data = _object(value, {
            "schema_identity", "schema_version", "contract_version", "summary", "summary_claim_ids",
            "severity_assessment", "severity_claim_ids", "diagnostic_conclusion", "hypotheses",
            "remediation", "prevention", "limitations", "evidence_completeness", "knowledge_gap",
            "evidence_references", "knowledge_references", "claims",
        }, "result")
        expected_contract = "2" if config.version == "2.0" else "1"
        if (data["schema_identity"], data["schema_version"], data["contract_version"]) != (
            source.pin.result_schema.identity, source.pin.result_schema.version, expected_contract
        ) or source.pin != config.pin:
            raise StructuredOutputError("result schema or pinned configuration is incompatible")

        def decoded(kind: type, item: object, label: str) -> dict[str, Any]:
            return _object(item, {field.name for field in fields(kind)}, label)

        evidence = tuple(EvidenceReference(**decoded(EvidenceReference, item, "Evidence reference")) for item in _items(data["evidence_references"], 64, "Evidence references"))
        knowledge = tuple(KnowledgeReference(**decoded(KnowledgeReference, item, "Knowledge reference")) for item in _items(data["knowledge_references"], 64, "Knowledge references"))
        claims: list[Claim] = []
        for raw_claim in _items(data["claims"], config.bounds.maximum_claims, "claims"):
            claim_keys = {field.name for field in fields(Claim)}
            if expected_contract == "1":
                claim_keys.remove("causal_assertion")
            item = _object(raw_claim, claim_keys, "claim")
            assertion = None
            if expected_contract == "2" and item["causal_assertion"] is not None:
                structured = decoded(CausalAssertion, item["causal_assertion"], "causal assertion")
                assertion = CausalAssertion(
                    _strings(structured["cause_claim_ids"], "cause_claim_ids"),
                    _strings(structured["effect_claim_ids"], "effect_claim_ids"),
                    CausalRelation(structured["relation"]),
                )
            claims.append(Claim(
                item["claim_id"], ClaimCategory(item["category"]), item["text"],
                _strings(item["supporting_evidence_ids"], "supporting_evidence_ids"),
                _strings(item["contradicting_evidence_ids"], "contradicting_evidence_ids"),
                _strings(item["knowledge_reference_ids"], "knowledge_reference_ids"),
                None if item["evidential_support"] is None else EvidentialSupport(item["evidential_support"]),
                assertion,
            ))
        hypotheses: list[Hypothesis] = []
        for raw_hypothesis in _items(data["hypotheses"], config.bounds.maximum_hypotheses, "hypotheses"):
            item = decoded(Hypothesis, raw_hypothesis, "hypothesis")
            hypotheses.append(Hypothesis(
                item["rank"], item["statement"], EvidentialSupport(item["evidential_support"]),
                _strings(item["supporting_evidence_ids"], "supporting_evidence_ids"),
                _strings(item["contradicting_evidence_ids"], "contradicting_evidence_ids"),
                _strings(item["knowledge_reference_ids"], "knowledge_reference_ids"),
                item["reasoning_summary"], _strings(item["claim_ids"], "claim_ids"),
            ))

        def guidance(name: str) -> tuple[Guidance, ...]:
            values: list[Guidance] = []
            for raw_guidance in _items(data[name], 64, name):
                item = decoded(Guidance, raw_guidance, name)
                values.append(Guidance(
                    item["description"], GuidanceSource(item["source"]),
                    _strings(item["knowledge_reference_ids"], "knowledge_reference_ids"),
                    _strings(item["claim_ids"], "claim_ids"),
                ))
            return tuple(values)

        return ResultContent(
            source, expected_contract, data["summary"], _strings(data["summary_claim_ids"], "summary_claim_ids"),
            data["severity_assessment"], _strings(data["severity_claim_ids"], "severity_claim_ids"),
            DiagnosticConclusion(data["diagnostic_conclusion"]), tuple(hypotheses),
            guidance("remediation"), guidance("prevention"),
            tuple(safe_text(item, "limitation") for item in _items(data["limitations"], 64, "limitations")),
            EvidenceCompleteness(data["evidence_completeness"]), data["knowledge_gap"],
            evidence, knowledge, tuple(claims),
            (ValidationFact("schema", source.pin.result_schema.version, config.result_schema_commitment),),
            invocation,
        )
    except (UnicodeError, TypeError, ValueError, KeyError, OverflowError) as exc:
        if isinstance(exc, StructuredOutputError):
            raise
        raise StructuredOutputError("structured output is malformed, unsafe or incompatible") from exc
