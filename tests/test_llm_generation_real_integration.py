"""Explicitly authorized, non-default Gemini adapter smoke test."""

import os
import socket

import pytest

from llm_generation.google_generation_adapter import GoogleGenerationAdapter
from llm_generation.service import ProviderCapability, ProviderRequest


_AUTHORIZED = (
    os.environ.get("RUN_SPEC015_REAL_INTEGRATION") == "1"
    and os.environ.get("SPEC015_REAL_INTEGRATION_AUTHORIZED") == "1"
    and bool(os.environ.get("GEMINI_API_KEY"))
)
pytestmark = pytest.mark.skipif(not _AUTHORIZED, reason="live Gemini requires explicit flag, authorization and credential")


def test_live_gemini_adapter_only_when_explicitly_authorized():
    try:
        socket.getaddrinfo("generativelanguage.googleapis.com", 443)
    except OSError:
        pytest.skip("Gemini network unavailable")
    provider = GoogleGenerationAdapter(ProviderCapability(
        "google", "gemini-2.5-flash", "spec015-live-profile",
        "spec015-live-schema", "1", True,
    ))
    prompt = 'Return JSON containing only {"ok": true}.'
    count = provider.input_token_upper_bound(prompt, 20)
    assert 0 < count <= 1024
    response = provider.invoke_once(ProviderRequest(
        "google", "gemini-2.5-flash", "spec015-live-profile",
        prompt, "spec015-live-schema", "1", 20, 4096, 128,
    ))
    assert provider.token_preflight_calls == provider.generation_calls == 1
    assert response.metadata.invocation_count == 1
    assert isinstance(response.structured_output, str)
