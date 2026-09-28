"""Gemini 2.5 Flash boundary; SDK and credentials never enter D contracts."""

from __future__ import annotations

import os
from time import monotonic
from typing import Any

from .contracts import InvocationMetadata
from .service import (
    ProviderCapability, ProviderFailureKind, ProviderInvocationError,
    ProviderRequest, ProviderResponse,
)


APPROVED_PROVIDER = "google"
APPROVED_MODEL = "gemini-2.5-flash"


def create_google_generation_client(timeout_seconds: int) -> Any:
    """Use one named environment credential; never select or rotate keys."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise ProviderInvocationError(ProviderFailureKind.CAPABILITY)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ProviderInvocationError(ProviderFailureKind.UNAVAILABLE)
    try:
        from google import genai
        from google.genai import client as genai_client, types
        return genai.Client(
            api_key=api_key,
            debug_config=genai_client.DebugConfig(
                client_mode=None,
                replays_directory=None,
                replay_id=None,
            ),
            http_options=types.HttpOptions(
                timeout=timeout_seconds * 1000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
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

    def __init__(self, capability: ProviderCapability, *, client: Any = None) -> None:
        if (not isinstance(capability, ProviderCapability)
            or capability.provider != APPROVED_PROVIDER
            or capability.model != APPROVED_MODEL
            or capability.hidden_retries_disabled is not True):
            raise ValueError("approved Google generation capability required")
        self.capability = capability
        self._client = client
        self._timeout_seconds: int | None = None
        self.token_preflight_calls = 0
        self.generation_calls = 0

    def _client_for(self, timeout_seconds: int) -> Any:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
            raise ProviderInvocationError(ProviderFailureKind.CAPABILITY)
        if self._timeout_seconds is not None and timeout_seconds != self._timeout_seconds:
            raise ProviderInvocationError(ProviderFailureKind.CAPABILITY)
        if self._client is None:
            self._client = create_google_generation_client(timeout_seconds)
        self._timeout_seconds = timeout_seconds
        return self._client

    def input_token_upper_bound(self, prompt: str, timeout_seconds: int) -> int | None:
        client = self._client_for(timeout_seconds)
        self.token_preflight_calls += 1
        try:
            response = client.models.count_tokens(model=APPROVED_MODEL, contents=prompt)
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
        self.generation_calls += 1
        started = monotonic()
        try:
            response = client.models.generate_content(
                model=APPROVED_MODEL,
                contents=request.prompt,
                config={
                    "candidate_count": 1,
                    "max_output_tokens": request.maximum_output_tokens,
                    "response_mime_type": "application/json",
                },
            )
        except Exception as exc:
            raise ProviderInvocationError(_provider_kind(exc), request_sent=True) from None
        duration_ms = min(600_000, max(0, int((monotonic() - started) * 1000)))
        usage = getattr(response, "usage_metadata", None)
        input_count = getattr(usage, "prompt_token_count", None)
        output_count = getattr(usage, "candidates_token_count", None)
        total_count = getattr(usage, "total_token_count", None)
        if (any(type(value) is not int or value < 0 for value in (
                input_count, output_count, total_count))
            or total_count < input_count + output_count):
            raise ProviderInvocationError(ProviderFailureKind.MALFORMED,
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
