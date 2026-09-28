"""Strict, versioned, non-secret Candidate-D resource configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

from .contracts import GenerationPin, VersionedIdentity, safe_text
from .identity import semantic_commitment


class GenerationConfigError(ValueError):
    pass


def _keys(value: object, names: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != names:
        raise GenerationConfigError(f"{label} has missing or unsupported keys")
    return value


def _positive(value: object, label: str, ceiling: int) -> int:
    if type(value) is not int or not 1 <= value <= ceiling:
        raise GenerationConfigError(f"{label} must be a finite positive bounded integer")
    return value


@dataclass(frozen=True, slots=True)
class InvocationBounds:
    maximum_request_bytes: int
    maximum_input_bytes: int
    maximum_output_bytes: int
    maximum_input_tokens: int
    maximum_output_tokens: int
    maximum_total_tokens: int
    timeout_seconds: int
    maximum_invocations: int
    maximum_rate_units: int
    maximum_quota_units: int
    maximum_cost_units: int
    maximum_resource_units: int
    maximum_hypotheses: int
    maximum_claims: int
    maximum_text_bytes: int

    def __post_init__(self) -> None:
        ceilings = {
            "maximum_request_bytes": 1_000_000, "maximum_input_bytes": 1_000_000,
            "maximum_output_bytes": 1_000_000, "maximum_input_tokens": 1_000_000,
            "maximum_output_tokens": 1_000_000, "maximum_total_tokens": 2_000_000,
            "timeout_seconds": 600, "maximum_invocations": 1, "maximum_rate_units": 1_000_000,
            "maximum_quota_units": 1_000_000, "maximum_cost_units": 1_000_000,
            "maximum_resource_units": 1_000_000, "maximum_hypotheses": 64,
            "maximum_claims": 512, "maximum_text_bytes": 32768,
        }
        for name, ceiling in ceilings.items():
            _positive(getattr(self, name), name, ceiling)
        if self.maximum_request_bytes < self.maximum_input_bytes or self.maximum_total_tokens < max(self.maximum_input_tokens, self.maximum_output_tokens):
            raise GenerationConfigError("related bounds contradict each other")


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    version: str
    pin: GenerationPin
    bounds: InvocationBounds
    hidden_retries_disabled: bool
    prompt_template: str
    result_schema_commitment: str
    configuration_commitment: str
    causal_rule_policy_id: str | None = None
    causal_rule_policy_version: str | None = None

    def __post_init__(self) -> None:
        if self.version not in {"1.0", "2.0"} or not isinstance(self.pin, GenerationPin) or not isinstance(self.bounds, InvocationBounds) or self.hidden_retries_disabled is not True:
            raise GenerationConfigError("unsupported generation configuration")
        if self.version == "1.0":
            if self.causal_rule_policy_id is not None or self.causal_rule_policy_version is not None:
                raise GenerationConfigError("legacy configuration cannot claim causal rule authority")
        else:
            if self.pin.result_schema.version != "2":
                raise GenerationConfigError("causal configuration requires result schema version 2")
            safe_text(self.causal_rule_policy_id, "causal_rule_policy_id", 160, identifier=True)
            safe_text(self.causal_rule_policy_version, "causal_rule_policy_version", 160, identifier=True)
        safe_text(self.prompt_template, "prompt_template", 32768)
        if "{" in self.prompt_template or "}" in self.prompt_template:
            raise GenerationConfigError("prompt interpolation syntax is not admitted in Phase 1")
        import re
        if not isinstance(self.result_schema_commitment, str) or not re.fullmatch(r"[0-9a-f]{64}", self.result_schema_commitment):
            raise GenerationConfigError("result schema commitment invalid")
        identity_parts = (self.version, self.pin.provider, self.pin.model, self.pin.profile, self.pin.prompt, self.pin.result_schema, self.pin.configuration.version, self.bounds, self.hidden_retries_disabled, self.prompt_template, self.result_schema_commitment)
        if self.version == "2.0":
            identity_parts += (self.causal_rule_policy_id, self.causal_rule_policy_version)
        expected = semantic_commitment(identity_parts)
        if self.configuration_commitment != expected or self.pin.configuration.identity != "cfg_" + expected:
            raise GenerationConfigError("configuration identity does not commit behavior")


def parse_generation_config(raw: object) -> GenerationConfig:
    if not isinstance(raw, dict):
        raise GenerationConfigError("generation config must be an object")
    names = {"version", "provider", "model", "profile", "prompt", "result_schema", "configuration", "bounds", "hidden_retries_disabled", "prompt_template", "result_schema_commitment", "configuration_commitment"}
    if raw.get("version") == "2.0":
        names |= {"causal_rule_policy_id", "causal_rule_policy_version"}
    root = _keys(raw, names, "generation config")
    try:
        def versioned(name: str) -> VersionedIdentity:
            item = _keys(root[name], {"identity", "version"}, name)
            return VersionedIdentity(**item)
        pin = GenerationPin(root["provider"], root["model"], versioned("profile"), versioned("prompt"), versioned("result_schema"), versioned("configuration"))
        from dataclasses import fields
        bounds = InvocationBounds(**_keys(root["bounds"], {item.name for item in fields(InvocationBounds)}, "bounds"))
        return GenerationConfig(root["version"], pin, bounds, root["hidden_retries_disabled"], root["prompt_template"], root["result_schema_commitment"], root["configuration_commitment"], root.get("causal_rule_policy_id"), root.get("causal_rule_policy_version"))
    except (TypeError, ValueError) as exc:
        raise GenerationConfigError("generation config is invalid, secret-shaped or Ground-Truth-shaped") from exc


def load_generation_config(path: str | Path) -> GenerationConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationConfigError("generation config cannot be read") from exc
    return parse_generation_config(raw)
