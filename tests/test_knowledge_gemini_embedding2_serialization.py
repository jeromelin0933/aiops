"""Offline SDK probes only; no provider acceptance, normalization or latency claim.

Exercise the public SDK through HTTPX MockTransport, not a mocked serializer.
Synthetic response markers prove local parsing order, never provider chunk mapping.
No production adapter, credential profile, corpus or durable store is used.
"""

from importlib.metadata import version
from hashlib import sha256
import json
import os
import socket
from types import SimpleNamespace

import httpx
import pytest


MODEL = "gemini-embedding-2"
URL = "https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:batchEmbedContents"


@pytest.fixture
def offline_sdk(monkeypatch):
    # Replace the environment mapping without reading/copying credential values.
    # Do this before importing/constructing the SDK; explicit fake key is used.
    monkeypatch.setattr(os, "environ", {})

    blocked_calls = []

    def blocked(*args, **kwargs):
        blocked_calls.append("network-or-credential-discovery")
        raise AssertionError("Offline probe forbids network or credential discovery")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)

    import google.auth

    monkeypatch.setattr(google.auth, "default", blocked)
    monkeypatch.setattr(google.auth, "load_credentials_from_file", blocked)
    from google import genai
    from google.genai import errors, types

    assert version("google-genai") == "2.25.0", "SDK pin changed; review required"
    clients = []

    def make_client(*, status=200, transport_error=None):
        captured = []

        def handle(request):
            captured.append(request)
            if transport_error is not None:
                raise transport_error("synthetic transport failure", request=request)
            if status != 200:
                return httpx.Response(status, json={"error": {
                    "code": status, "message": "synthetic failure",
                    "status": "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE",
                }})
            count = len(json.loads(request.content)["requests"])
            return httpx.Response(200, json={"embeddings": [
                {"values": [float(i + 1)] + [0.0] * 767} for i in range(count)
            ]})

        transport = httpx.MockTransport(handle)
        sync = httpx.Client(transport=transport, trust_env=False, follow_redirects=False)
        # Async requests are outside this synchronous contract and fail closed.
        async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(blocked), trust_env=False,
        )
        client = genai.Client(
            vertexai=False, api_key="synthetic-offline-not-a-credential",
            http_options=types.HttpOptions(
                api_version="v1", timeout=30_000,
                retry_options=types.HttpRetryOptions(attempts=1),
                httpx_client=sync, httpx_async_client=async_client,
            ),
        )
        clients.append((client, sync, async_client))
        return client, captured

    yield SimpleNamespace(make_client=make_client, types=types, errors=errors,
                         blocked_calls=blocked_calls)
    for client, sync, async_client in reversed(clients):
        client.close()
        sync.close()
        # Unused MockTransport has no asynchronous I/O to drain. Drive its close
        # directly: a Windows event loop would create a forbidden socketpair.
        closing = async_client.aclose()
        try:
            with pytest.raises(StopIteration):
                closing.send(None)
        finally:
            closing.close()
        assert async_client.is_closed


def document_inputs():
    # Unique ordered identities, six synthetic titles, Unicode and JSON escaping.
    return [
        f'title: synthetic-document-{i // 9:02d} | text: chunk-{i:03d} 維運 "quoted"\\path\nline'
        for i in range(54)
    ]


def contents(types, texts):
    return [types.Content(role="user", parts=[types.Part(text=text)]) for text in texts]


def test_54_contents_use_real_sdk_serialization_and_preserve_order(offline_sdk):
    client, captured = offline_sdk.make_client()
    texts = document_inputs()
    result = client.models.embed_content(
        model=MODEL, contents=contents(offline_sdk.types, texts),
        config=offline_sdk.types.EmbedContentConfig(output_dimensionality=768),
    )
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.extensions["timeout"] == {
        "connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0,
    }  # Configuration propagation only, not a 30-second wall-clock guarantee.
    body = json.loads(request.content)
    assert set(body) == {"requests"}
    assert len(body["requests"]) == 54
    for item, text in zip(body["requests"], texts, strict=True):
        assert set(item) == {"model", "content", "outputDimensionality"}
        assert item["model"] == "models/" + MODEL
        assert item["outputDimensionality"] == 768
        assert item["content"] == {"role": "user", "parts": [{"text": text}]}
    # Exact key assertions also exclude taskType/autoTruncate and config leakage.
    assert len(result.embeddings) == 54
    assert [entry.values[0] for entry in result.embeddings] == list(range(1, 55))
    assert all(len(entry.values) == 768 for entry in result.embeddings)
    # Measure actual serialized bytes including envelopes, escaping and metadata.
    text_bytes = sum(len(text.encode("utf-8")) for text in texts)
    assert 54 <= 128
    assert text_bytes < len(request.content) < 1_000_000
    assert int(request.headers["content-length"]) == len(request.content)
    assert b'\\n' in request.content and b'\\"quoted\\"' in request.content
    # No token count or provider item/payload limit is inferred from these bytes.
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":")).encode("utf-8")
    assert sha256(request.content).hexdigest() != sha256(canonical).hexdigest()
    assert b"synthetic-offline-not-a-credential" not in request.content


def test_list_of_strings_is_one_item_with_54_parts_negative_case(offline_sdk):
    client, captured = offline_sdk.make_client()
    texts = document_inputs()
    client.models.embed_content(
        model=MODEL, contents=texts,
        config=offline_sdk.types.EmbedContentConfig(output_dimensionality=768),
    )
    assert len(captured) == 1
    items = json.loads(captured[0].content)["requests"]
    assert len(items) == 1  # Unsuitable for the approved 54-item contract.
    assert items[0]["content"]["parts"] == [{"text": text} for text in texts]


def test_query_envelope_uses_same_model_dimension_and_omitted_fields(offline_sdk):
    client, captured = offline_sdk.make_client()
    text = "task: search result | query: synthetic database timeout"
    client.models.embed_content(
        model=MODEL, contents=contents(offline_sdk.types, [text]),
        config=offline_sdk.types.EmbedContentConfig(output_dimensionality=768),
    )
    assert len(captured) == 1
    assert str(captured[0].url) == URL
    assert json.loads(captured[0].content) == {"requests": [{
        "model": "models/" + MODEL, "outputDimensionality": 768,
        "content": {"role": "user", "parts": [{"text": text}]},
    }]}


@pytest.mark.parametrize("auto_truncate", [False, True])
def test_auto_truncate_is_local_serialization_failure_before_transport(
    offline_sdk, auto_truncate,
):
    client, captured = offline_sdk.make_client()
    with pytest.raises(ValueError, match="auto_truncate parameter is only supported"):
        client.models.embed_content(
            model=MODEL, contents=contents(offline_sdk.types, ["synthetic"]),
            config=offline_sdk.types.EmbedContentConfig(
                output_dimensionality=768, auto_truncate=auto_truncate,
            ),
        )
    assert captured == []  # Not a provider outage or HTTP rejection.


@pytest.mark.parametrize("status", [429, 503])
def test_retryable_mock_http_error_has_exactly_one_transport_attempt(offline_sdk, status):
    client, captured = offline_sdk.make_client(status=status)
    with pytest.raises(offline_sdk.errors.APIError) as caught:
        client.models.embed_content(
            model=MODEL, contents=contents(offline_sdk.types, ["synthetic"]),
            config=offline_sdk.types.EmbedContentConfig(output_dimensionality=768),
        )
    assert caught.value.code == status
    assert len(captured) == 1  # Mock HTTP error, not an observed provider failure.


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_mock_transport_failure_is_distinct_from_local_serialization(offline_sdk, error):
    client, captured = offline_sdk.make_client(transport_error=error)
    with pytest.raises(error, match="synthetic transport failure"):
        client.models.embed_content(
            model=MODEL, contents=contents(offline_sdk.types, ["synthetic"]),
            config=offline_sdk.types.EmbedContentConfig(output_dimensionality=768),
        )
    assert len(captured) == 1


def test_network_and_credential_fallback_guards_are_active(offline_sdk):
    import google.auth

    with pytest.raises(AssertionError, match="forbids network"):
        socket.create_connection(("offline.invalid", 443))
    with pytest.raises(AssertionError, match="forbids network"):
        with httpx.Client(trust_env=False) as fallback:
            fallback.get("https://offline.invalid")
    with pytest.raises(AssertionError, match="credential discovery"):
        google.auth.default()


@pytest.mark.parametrize("stage,failures", [
    ("sync", ()), ("async", ()), ("async", ("sync",)),
    ("sdk", ()), ("sdk", ("sync",)), ("sdk", ("async",)),
    ("sdk", ("sync", "async")), ("sdk", ("suspend",)),
])
def test_s2_partial_constructor_cleanup_attempts_and_primary_failure(
    offline_sdk, monkeypatch, stage, failures,
):
    from google import genai
    from knowledge_index import google_embedding_adapter as adapter

    events = []

    class Sync:
        def __init__(self, **kwargs):
            events.append("sync acquire")
            if stage == "sync":
                raise RuntimeError("synthetic-secret-sync-constructor")

        def close(self):
            events.append("sync close")
            if "sync" in failures:
                raise RuntimeError("synthetic-secret-sync-cleanup")

    class Suspend:
        def __await__(self):
            yield None

    class Async:
        def __init__(self, **kwargs):
            events.append("async acquire")
            if stage == "async":
                raise RuntimeError("synthetic-secret-async-constructor")

        async def aclose(self):
            events.append("async close")
            if "async" in failures:
                raise RuntimeError("synthetic-secret-async-cleanup")
            if "suspend" in failures:
                await Suspend()

    def constructor(**kwargs):
        events.append("sdk construct")
        raise RuntimeError("synthetic-secret-SDK-constructor")

    monkeypatch.setattr(httpx, "Client", Sync)
    monkeypatch.setattr(httpx, "AsyncClient", Async)
    # Acquisition doubles are deliberately not real HTTPX clients. Isolate the
    # constructor-failure fixture from Pydantic's real-client field validation.
    monkeypatch.setattr(offline_sdk.types, "HttpOptions", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(genai, "Client", constructor)
    with pytest.raises(ValueError) as failure:
        adapter._create_gemini_offline_mock_client(
            lambda *_: pytest.fail("constructor failure cannot delegate"))
    detail = str(failure.value)
    assert "synthetic-secret" not in detail and failure.value.__suppress_context__
    expected_stage = {"sync": "sync acquisition", "async": "async acquisition",
                      "sdk": "SDK constructor"}[stage]
    assert expected_stage in detail  # Primary failure never replaced by cleanup error.
    assert events.count("sdk construct") == int(stage == "sdk")
    assert events.count("sync close") == int(stage != "sync")
    assert events.count("async close") == int(stage == "sdk")
    for cleanup in ("sync", "async"):
        if cleanup in failures or (cleanup == "async" and "suspend" in failures):
            assert cleanup in detail


def test_s2_partial_cleanup_all_owners_and_no_second_attempt(offline_sdk):
    from knowledge_index.google_embedding_adapter import _close_offline_resources

    events = []

    def close(name):
        events.append(name)
        raise RuntimeError("synthetic-secret-cleanup")

    async def aclose():
        close("async")

    failures = _close_offline_resources(
        SimpleNamespace(close=lambda: close("sdk")),
        SimpleNamespace(close=lambda: close("sync")), SimpleNamespace(aclose=aclose))
    assert events == ["sdk", "sync", "async"]
    assert failures == ("sdk", "sync", "async")
