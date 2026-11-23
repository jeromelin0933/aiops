"""Versioned Gemini boundaries; SDK and credentials never enter D contracts."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version
from time import monotonic
from typing import Any

from .contracts import InvocationMetadata
from .service import (
    ProviderCapability, ProviderFailureKind, ProviderInvocationError,
    ProviderRequest, ProviderResponse,
)


APPROVED_PROVIDER = "google"
APPROVED_MODEL = "gemini-2.5-flash"
GEMINI35_MODEL = "gemini-3.5-flash-lite"
GEMINI35_PROFILE = "demo-google-gemini35-flash-lite-generation-profile-v1"
GEMINI35_PROFILE_VERSION = "1"
GEMINI35_SDK = "2.25.0"


def create_google_generation_client(
    timeout_seconds: int, *, api_version: str | None = None,
) -> Any:
    """Use one named environment credential; never select or rotate keys."""
    if (type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600
            or api_version not in (None, "v1beta")):
        raise ProviderInvocationError(ProviderFailureKind.CAPABILITY)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ProviderInvocationError(ProviderFailureKind.UNAVAILABLE)
    try:
        from google import genai
        from google.genai import client as genai_client, types
        options = {
            "timeout": timeout_seconds * 1000,
            "retry_options": types.HttpRetryOptions(attempts=1),
        }
        if api_version is not None:
            options["api_version"] = api_version
        return genai.Client(
            api_key=api_key,
            debug_config=genai_client.DebugConfig(
                client_mode=None,
                replays_directory=None,
                replay_id=None,
            ),
            http_options=types.HttpOptions(**options),
        )
    except Exception:
        raise ProviderInvocationError(ProviderFailureKind.UNAVAILABLE) from None


def _provider_kind(exc: Exception) -> ProviderFailureKind:
    """Classify only SDK status and exception type, never message/details."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if type(code) is int:
        if code in (408, 504):
            return ProviderFailureKind.TIMEOUT
        if code == 429:
            return (ProviderFailureKind.QUOTA if status == "RESOURCE_EXHAUSTED"
                    else ProviderFailureKind.RATE)
        if code == 507:
            return ProviderFailureKind.RESOURCE
        if code in (400, 401, 403, 404, 422):
            return ProviderFailureKind.CAPABILITY
        if code in (500, 502, 503):
            return ProviderFailureKind.UNAVAILABLE
    if isinstance(exc, TimeoutError) or type(exc).__name__ in {
        "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    }:
        return ProviderFailureKind.TIMEOUT
    return ProviderFailureKind.UNAVAILABLE


class GoogleGenerationAdapter:
    """One count request and at most one generation request per execution."""

    def __init__(
        self, capability: ProviderCapability, *, client: Any = None,
        profile_version: str | None = None,
    ) -> None:
        if (not isinstance(capability, ProviderCapability)
            or capability.provider != APPROVED_PROVIDER
            or capability.model not in (APPROVED_MODEL, GEMINI35_MODEL)
            or capability.hidden_retries_disabled is not True):
            raise ValueError("approved Google generation capability required")
        self._gemini35 = capability.model == GEMINI35_MODEL
        if self._gemini35:
            if (capability.profile_id != GEMINI35_PROFILE
                    or profile_version != GEMINI35_PROFILE_VERSION
                    or capability.schema_id != "spec015-rca-result-v1"
                    or capability.schema_version != "1"):
                raise ValueError("exact Gemini 3.5 profile/version/schema required")
            try:
                sdk = version("google-genai")
            except PackageNotFoundError:
                sdk = None
            if sdk != GEMINI35_SDK:
                raise ValueError("pinned Gemini 3.5 SDK required")
        elif capability.profile_id == GEMINI35_PROFILE:
            raise ValueError("Gemini 3.5 profile cannot select a legacy model")
        self.capability = capability
        self._client = client
        self._timeout_seconds: int | None = None
        self.token_preflight_calls = 0
        self.generation_calls = 0

    def _client_for(self, timeout_seconds: int) -> Any:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
            raise ProviderInvocationError(ProviderFailureKind.CAPABILITY)
        if self._gemini35 and timeout_seconds != 30:
            raise ProviderInvocationError(ProviderFailureKind.CAPABILITY)
        if self._timeout_seconds is not None and timeout_seconds != self._timeout_seconds:
            raise ProviderInvocationError(ProviderFailureKind.CAPABILITY)
        if self._client is None:
            if self._gemini35:
                self._client = create_google_generation_client(
                    timeout_seconds, api_version="v1beta",
                )
            else:
                self._client = create_google_generation_client(timeout_seconds)
        self._timeout_seconds = timeout_seconds
        return self._client

    def input_token_upper_bound(self, prompt: str, timeout_seconds: int) -> int | None:
        client = self._client_for(timeout_seconds)
        self.token_preflight_calls += 1
        try:
            response = client.models.count_tokens(model=self.capability.model, contents=prompt)
        except Exception as exc:
            raise ProviderInvocationError(_provider_kind(exc), request_sent=True) from None
        count = getattr(response, "total_tokens", None)
        if type(count) is not int or count < 1:
            raise ProviderInvocationError(ProviderFailureKind.MALFORMED,
                                          request_sent=True)
        return count

    def invoke_once(self, request: ProviderRequest) -> ProviderResponse:
        if (not isinstance(request, ProviderRequest)
            or request.provider != self.capability.provider
            or request.model != self.capability.model
            or request.profile_id != self.capability.profile_id
            or request.schema_id != self.capability.schema_id
            or request.schema_version != self.capability.schema_version
            or type(request.maximum_output_tokens) is not int
            or request.maximum_output_tokens < 1
            or type(request.maximum_output_bytes) is not int
            or request.maximum_output_bytes < 1):
            raise ProviderInvocationError(ProviderFailureKind.CAPABILITY)
        client = self._client_for(request.timeout_seconds)
        config = {
            "max_output_tokens": request.maximum_output_tokens,
            "response_mime_type": "application/json",
        }
        if self._gemini35:
            config.update({
                "thinking_config": {"thinking_level": "MINIMAL"},
                "automatic_function_calling": {"disable": True},
                "tools": [],
            })
        else:
            config["candidate_count"] = 1
        self.generation_calls += 1
        started = monotonic()
        try:
            response = client.models.generate_content(
                model=self.capability.model,
                contents=request.prompt,
                config=config,
            )
        except Exception as exc:
            raise ProviderInvocationError(_provider_kind(exc), request_sent=True) from None
        duration_ms = min(600_000, max(0, int((monotonic() - started) * 1000)))
        if self._gemini35:
            try:
                model = getattr(response, "model_version", None)
                candidates = getattr(response, "candidates", None)
                complete = (isinstance(candidates, (list, tuple))
                            and len(candidates) == 1
                            and getattr(candidates[0], "finish_reason", None) == "STOP")
            except Exception:
                model, complete = None, False
            # No prefix/alias inference: an unknown revision requires review.
            if type(model) is not str or model != GEMINI35_MODEL:
                raise ProviderInvocationError(ProviderFailureKind.CAPABILITY,
                                              request_sent=True)
            if not complete:
                raise ProviderInvocationError(ProviderFailureKind.MALFORMED,
                                              request_sent=True)
        usage = getattr(response, "usage_metadata", None)
        input_count = getattr(usage, "prompt_token_count", None)
        output_count = getattr(usage, "candidates_token_count", None)
        total_count = getattr(usage, "total_token_count", None)
        if (any(type(value) is not int or value < 0 for value in (
                input_count, output_count, total_count))
            or total_count < input_count + output_count):
            raise ProviderInvocationError(ProviderFailureKind.MALFORMED,
                                          request_sent=True)
        if self._gemini35:
            thoughts = getattr(usage, "thoughts_token_count", None)
            tools = getattr(usage, "tool_use_prompt_token_count", None)
            if ((thoughts is not None and (type(thoughts) is not int or thoughts < 0
                    or total_count < input_count + output_count + thoughts))
                    or (tools is not None and (type(tools) is not int or tools != 0))):
                raise ProviderInvocationError(ProviderFailureKind.MALFORMED,
                                              request_sent=True)
            # Account for ALL post-input tokens, including thinking, without
            # inventing a zero for an absent optional thoughts breakdown.
            output_count = total_count - input_count
            if output_count > request.maximum_output_tokens:
                raise ProviderInvocationError(ProviderFailureKind.BOUNDS,
                                              request_sent=True)
        try:
            metadata = InvocationMetadata(1, input_count, output_count,
                                          total_count, duration_ms)
            output = response.text
        except Exception:
            raise ProviderInvocationError(ProviderFailureKind.MALFORMED,
                                          request_sent=True) from None
        if not isinstance(output, str):
            raise ProviderInvocationError(ProviderFailureKind.MALFORMED,
                                          request_sent=True)
        if len(output.encode("utf-8")) > request.maximum_output_bytes:
            raise ProviderInvocationError(ProviderFailureKind.BOUNDS,
                                          request_sent=True)
        return ProviderResponse(output, metadata)
