"""Only synthetic offline records, HTTPX MockTransport and temporary SQLite."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from hashlib import sha256
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx

import pytest

from knowledge_index import CanonicalKnowledgeQuery, QueryEmbeddingRequest
from knowledge_index.gemini_diagnostic import (
    _envelopes, main, run_live, run_offline_simulation,
)
import knowledge_index.gemini_diagnostic as diagnostic
import knowledge_index.google_embedding_adapter as adapter
from knowledge_index.gemini_diagnostic_controls import (
    ApprovedDiagnosticInput, DiagnosticDenied, LiveHostBinding,
    OfflineAuthorizationSource, TemporaryDiagnosticUsage,
    input_commitment,
)
from test_knowledge_gemini_diagnostic_controls import facts, record, ledger_rows
from test_knowledge_gemini_embedding2_adapter import fixture_request
from test_knowledge_gemini_embedding2_serialization import offline_sdk


def _s2_fixed_case(tmp_path, kind="document", count=2):
    prepared, approved = _e4_case(tmp_path, kind, count)
    # Independent observation completes BEFORE construction of the target guard.
    evidence, observation = diagnostic.observe_offline_sdk_bytes(prepared, approved)
    return prepared, approved, evidence, observation


@pytest.mark.parametrize("mutation", ["arbitrary_transport", "arbitrary_delegate", "entry_override",
                                      "instance_entry", "close_override", "used_guard"])
def test_r01_adapter_bridge_rejects_uncontrolled_transport_handoff(
    offline_sdk, tmp_path, monkeypatch, mutation,
):
    from google import genai

    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    response = adapter._DiagnosticMockResponse()
    guard = diagnostic._DiagnosticRequestGuardTransport(prepared, approved, evidence, response)
    if mutation == "arbitrary_transport":
        guard = httpx.MockTransport(lambda *_: pytest.fail("uncontrolled delegate"))
    elif mutation == "arbitrary_delegate":
        guard._delegate = httpx.MockTransport(lambda *_: pytest.fail("uncontrolled delegate"))
    elif mutation == "entry_override":
        class Override(diagnostic._DiagnosticRequestGuardTransport):
            def handle_request(self, request):
                pytest.fail("uncontrolled entry")
        guard = Override(prepared, approved, evidence, response)
    elif mutation == "instance_entry":
        guard.handle_request = lambda *_: pytest.fail("uncontrolled entry")
    elif mutation == "close_override":
        guard.close = lambda: pytest.fail("uncontrolled cleanup")
    else:
        guard._entered = True
    constructed = []

    def constructor(**kwargs):
        constructed.append(True)
        pytest.fail("invalid handoff must not construct SDK")

    monkeypatch.setattr(genai, "Client", constructor)
    with pytest.raises(DiagnosticDenied, match="closed unused"):
        adapter._create_guarded_diagnostic_offline_sdk_client(guard)
    assert constructed == [] and offline_sdk.blocked_calls == []


@pytest.mark.parametrize("override", ["api_key", "credential_resolver", "httpx_client", "transport",
                                     "base_url", "sdk_factory", "http_options", "safety_source"])
def test_r01_adapter_bridge_has_no_general_sdk_construction_surface(override):
    with pytest.raises(TypeError):
        adapter._create_guarded_diagnostic_offline_sdk_client(object(), **{override: object()})


@pytest.mark.parametrize("kind,count", [("document", 54), ("query", 1)])
def test_s2_real_sdk_final_identity_and_exact_delegated_bytes(offline_sdk, tmp_path, kind, count):
    prepared, approved, evidence, observation = _s2_fixed_case(tmp_path, kind, count)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    try:
        assert handle._guard.delegations == 0
        result = handle.embed_prepared()
        guard = handle._guard
        final = handle._sync._final_request
        assert final is guard.guarded_request is guard.delegated_request
        actual_request, actual_content, actual_stream = guard._delegate.observations[0]
        assert final is actual_request
        assert actual_content == actual_stream == guard.delegated_body
        assert final is not observation
        assert guard.delegated_body == final.content == b"".join(final.stream)
        assert sha256(guard.delegated_body).hexdigest() == evidence.sdk_body_sha256
        assert len(guard.delegated_body) == evidence.sdk_body_bytes
        assert sha256(diagnostic._canonical_request(final.content)).hexdigest() == evidence.canonical_json_sha256
        assert len(result.embeddings) == count and guard.delegations == 1
        with pytest.raises(DiagnosticDenied):
            handle.embed_prepared()
        with pytest.raises(DiagnosticDenied):
            guard.handle_request(final)
        assert guard.delegations == 1
    finally:
        handle.close()
    assert handle.cleanup_failures == () and handle._sync.is_closed and handle._async.is_closed


@pytest.mark.parametrize("mutation", ["raw_bytes", "ordered", "envelope", "dimension", "task_type",
    "auto_truncate", "method", "route", "host", "query", "fragment", "timeout", "length"])
def test_s2_actual_sdk_request_mutation_has_zero_delegation(
    offline_sdk, tmp_path, monkeypatch, mutation,
):
    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    real_build = httpx.Client.build_request

    def build(self, *args, **kwargs):
        built = real_build(self, *args, **kwargs)
        body = json.loads(built.content)
        if mutation == "raw_bytes":
            data = json.dumps(body, sort_keys=True, indent=2).encode()
            assert diagnostic._canonical_request(data) == diagnostic._canonical_request(built.content)
        elif mutation in ("ordered", "envelope", "dimension", "task_type", "auto_truncate"):
            if mutation == "ordered":
                body["requests"].reverse()
            elif mutation == "envelope":
                body["requests"][0]["content"]["parts"][0]["text"] += " mutation"
            else:
                key = {"dimension": "outputDimensionality", "task_type": "taskType",
                       "auto_truncate": "autoTruncate"}[mutation]
                body["requests"][0][key] = 42
            data = json.dumps(body).encode()
        else:
            data = built.content
        if data != built.content:
            built = httpx.Request(built.method, built.url, content=data,
                                  headers={**dict(built.headers), "content-length": str(len(data))},
                                  extensions=built.extensions)
        if mutation == "method":
            built.method = "GET"
        elif mutation == "route":
            built.url = built.url.copy_with(path="/v1/models/other:batchEmbedContents")
        elif mutation == "host":
            built.url = built.url.copy_with(host="offline.invalid")
        elif mutation == "query":
            built.url = built.url.copy_with(query=b"override=1")
        elif mutation == "fragment":
            built.url = built.url.copy_with(fragment="override")
        elif mutation == "timeout":
            built.extensions["timeout"]["read"] = 31.0
        elif mutation == "length":
            built.headers["content-length"] = "1"
        return built

    monkeypatch.setattr(httpx.Client, "build_request", build)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    try:
        with pytest.raises(DiagnosticDenied):
            handle.embed_prepared()
        guard = handle._guard
        assert guard._entered and guard.delegations == 0
        assert guard._delegate.observations == []
        assert guard._evidence is evidence and evidence.sdk_body_sha256 != "0" * 64
        with pytest.raises(DiagnosticDenied):
            guard.handle_request(handle._sync._final_request)
        assert guard.delegations == 0
    finally:
        handle.close()


@pytest.mark.parametrize("field,value", [
    ("sdk_body_sha256", "0" * 64), ("sdk_body_bytes", 1),
    ("canonical_json_sha256", "0" * 64), ("input_commitment", "0" * 64),
    ("source_revision", "a" * 40), ("authorization_record_sha256", "0" * 64),
    ("ordered_identities", ("wrong",)), ("provider", "other"), ("model", "other"),
    ("sdk", "other"), ("api", "v1beta"), ("attempts", 2), ("timeout_ms", 31_000),
    ("output_dimensionality", 769), ("reference", ""),
    ("valid_until", datetime(2000, 1, 1, tzinfo=timezone.utc)),
])
def test_s2_independent_e4_mismatch_is_not_self_approved(offline_sdk, tmp_path, field, value):
    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    fixed_wrong = replace(evidence, **{field: value})
    before = asdict(fixed_wrong)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, fixed_wrong)
    try:
        with pytest.raises(DiagnosticDenied):
            handle.embed_prepared()
        assert handle._guard.delegations == 0 and handle._guard._delegate.observations == []
        assert asdict(handle._guard._evidence) == before
    finally:
        handle.close()


@pytest.mark.parametrize("mutation", ["method", "url", "headers", "header_update", "header_list",
    "extensions", "timeout", "content", "stream", "stream_data", "url_internal", "dict", "delete"])
def test_s2_sealed_final_request_mutation_denied_before_send(offline_sdk, tmp_path, mutation):
    prepared, approved, evidence, observed = _s2_fixed_case(tmp_path)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    try:
        final = handle._sync.build_request(observed.method, observed.url, content=observed.content,
                                            headers=observed.headers, timeout=30.0)
        actions = {
            "method": lambda: setattr(final, "method", "GET"),
            "url": lambda: setattr(final, "url", httpx.URL("https://offline.invalid")),
            "headers": lambda: final.headers.__setitem__("host", "offline.invalid"),
            "header_update": lambda: final.headers.update({"host": "offline.invalid"}),
            "header_list": lambda: setattr(final.headers, "_list", []),
            "extensions": lambda: setattr(final, "extensions", {}),
            "timeout": lambda: final.extensions["timeout"].__setitem__("read", 99),
            "content": lambda: setattr(final, "_content", b"mutation"),
            "stream": lambda: setattr(final, "stream", httpx.ByteStream(b"mutation")),
            "stream_data": lambda: setattr(final.stream, "data", b"mutation"),
            "url_internal": lambda: setattr(final.url, "_uri", httpx.URL("https://offline.invalid")._uri),
            "dict": lambda: final.__dict__.__setitem__("method", "GET"),
            "delete": lambda: delattr(final, "_content"),
        }
        with pytest.raises((DiagnosticDenied, AttributeError, TypeError)):
            actions[mutation]()
        assert handle._guard.delegations == 0 and handle._guard._delegate.observations == []
        assert final.content == observed.content
    finally:
        handle.close()


@pytest.mark.parametrize("mutation", ["stream", "cached_content", "substitution"])
def test_s2_unexpected_stream_or_request_identity_denied(offline_sdk, tmp_path, mutation):
    prepared, approved, evidence, observed = _s2_fixed_case(tmp_path)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    try:
        final = handle._sync.build_request(observed.method, observed.url, content=observed.content,
                                            headers=observed.headers, timeout=30.0)
        if mutation == "stream":
            # Deliberate test-only reflection; normal setters are separately blocked.
            object.__setattr__(final, "stream", httpx.ByteStream(b"unverified bytes"))
        elif mutation == "cached_content":
            object.__setattr__(final, "_content", b"unverified bytes")
        else:
            final = adapter._DiagnosticFinalRequest(observed)
        with pytest.raises(DiagnosticDenied):
            handle._guard.handle_request(final)
        assert handle._guard.delegations == 0 and handle._guard._delegate.observations == []
    finally:
        handle.close()


@pytest.mark.parametrize("mutation", ["retry", "retry_engine", "timeout", "api", "backend",
    "base_url", "sync_fallback", "async_fallback", "redirect", "hooks", "mounts", "per_request",
    "credential", "server_timeout"])
def test_s2_sdk_policy_override_rejected_without_delegation(offline_sdk, tmp_path, mutation):
    from tenacity import stop_after_attempt

    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    try:
        api = handle._sdk._api_client
        if mutation == "retry":
            api._http_options.retry_options.attempts = 2
        elif mutation == "retry_engine":
            api._retry.stop = stop_after_attempt(2)
        elif mutation == "timeout":
            api._http_options.timeout = 31_000
        elif mutation == "api":
            api._http_options.api_version = "v1beta"
        elif mutation == "backend":
            api.vertexai = True
        elif mutation == "base_url":
            api._http_options.base_url = "https://offline.invalid"
        elif mutation == "sync_fallback":
            api._httpx_client = object()
        elif mutation == "async_fallback":
            api._async_httpx_client = object()
        elif mutation == "redirect":
            handle._sync.follow_redirects = True
        elif mutation == "hooks":
            handle._sync.event_hooks["request"].append(lambda *_: pytest.fail("hook must not run"))
        elif mutation == "mounts":
            handle._sync._mounts[object()] = object()
        elif mutation == "credential":
            api._http_options.headers["x-goog-api-key"] = "synthetic-secret-override"
        elif mutation == "server_timeout":
            api._http_options.headers["X-Server-Timeout"] = "31"
        else:
            handle._guard._prepared.config.http_options = offline_sdk.types.HttpOptions(
                retry_options=offline_sdk.types.HttpRetryOptions(attempts=2))
        with pytest.raises(DiagnosticDenied):
            handle.embed_prepared()
        assert handle._guard.delegations == 0 and handle._guard._delegate.observations == []
        # Restore only test-injected fake mount, so ordinary close stays valid.
        if mutation == "mounts":
            handle._sync._mounts.clear()
    finally:
        handle.close()


@pytest.mark.parametrize("status,error", [(302, None), (429, None), (503, None),
                                         (200, "timeout"), (200, "connection")])
def test_s2_redirect_retryable_response_and_transport_failure_never_send_twice(
    offline_sdk, tmp_path, status, error,
):
    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence,
        response=adapter._DiagnosticMockResponse(status, error))
    try:
        with pytest.raises((DiagnosticDenied, offline_sdk.errors.APIError, httpx.TransportError)):
            handle.embed_prepared()
        assert handle._guard.delegations == len(handle._guard._delegate.observations) == 1
        with pytest.raises(DiagnosticDenied):
            handle.embed_prepared()
        with pytest.raises(DiagnosticDenied):
            handle._guard.handle_request(handle._sync._final_request)
        assert handle._guard.delegations == len(handle._guard._delegate.observations) == 1
    finally:
        handle.close()


def test_s2_reentrant_and_concurrent_guard_entry_cannot_delegate_twice(offline_sdk, tmp_path, monkeypatch):
    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    entered, release = Event(), Event()
    real = adapter._DiagnosticClosedMockTransport.handle_request
    calls = []

    def underlying(self, request):
        calls.append(request)
        with pytest.raises(DiagnosticDenied, match="reentrant"):
            handle._guard.handle_request(request)
        entered.set()
        assert release.wait(5)
        return real(self, request)

    monkeypatch.setattr(adapter._DiagnosticClosedMockTransport, "handle_request", underlying)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(handle.embed_prepared)
            try:
                assert entered.wait(5)
                with pytest.raises(DiagnosticDenied, match="concurrent"):
                    handle._guard.handle_request(handle._sync._final_request)
                assert handle._guard.delegations == len(calls) == 1
            finally:
                release.set()
            assert len(first.result(timeout=5).embeddings) == 2
        assert len(handle._guard._delegate.observations) == 1
    finally:
        handle.close()


def test_s2_post_guard_mutation_is_blocked_on_the_actual_delegated_object(
    offline_sdk, tmp_path, monkeypatch,
):
    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    real = adapter._DiagnosticClosedMockTransport.handle_request
    checked = []

    def underlying(self, request):
        assert request is handle._guard.guarded_request is handle._sync._final_request
        assert handle._guard.delegations == 1  # Guard has completed, actual bytes remain sealed.
        for mutate in (
            lambda: setattr(request, "_content", b"mutation"),
            lambda: setattr(request, "stream", httpx.ByteStream(b"mutation")),
            lambda: request.headers.__setitem__("host", "offline.invalid"),
            lambda: setattr(request.url, "_uri", httpx.URL("https://offline.invalid")._uri),
            lambda: request.extensions["timeout"].__setitem__("read", 99),
        ):
            with pytest.raises((DiagnosticDenied, AttributeError, TypeError)):
                mutate()
            checked.append("blocked")
        assert sha256(b"".join(request.stream)).hexdigest() == evidence.sdk_body_sha256
        return real(self, request)

    monkeypatch.setattr(adapter._DiagnosticClosedMockTransport, "handle_request", underlying)
    try:
        handle.embed_prepared()
        assert checked == ["blocked"] * 5
        actual, content, stream = handle._guard._delegate.observations[0]
        assert actual is handle._sync._final_request and content == stream
        assert handle._guard.delegations == len(handle._guard._delegate.observations) == 1
    finally:
        handle.close()


@pytest.mark.parametrize("stage,failures", [
    ("sync", ()), ("async", ()), ("async", ("sync",)), ("sdk", ()),
    ("sdk", ("sync",)), ("sdk", ("async",)), ("sdk", ("sync", "async")),
])
def test_s2_new_factory_partial_acquisition_and_combined_cleanup(
    offline_sdk, tmp_path, monkeypatch, stage, failures,
):
    from google import genai

    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    sync_class, async_class = adapter._DiagnosticHttpxClient, httpx.AsyncClient
    events, acquired = [], []

    def sync(guard):
        events.append("sync acquire")
        if stage == "sync":
            raise RuntimeError("synthetic-secret-acquisition")
        client = sync_class(guard)
        acquired.append(client)
        real_close = client.close

        def close():
            events.append("sync close")
            real_close()
            if "sync" in failures:
                raise RuntimeError("synthetic-secret-cleanup")

        client.close = close
        return client

    def async_client(**kwargs):
        events.append("async acquire")
        if stage == "async":
            raise RuntimeError("synthetic-secret-acquisition")
        client = async_class(**kwargs)
        acquired.append(client)
        real_close = client.aclose

        async def close():
            events.append("async close")
            await real_close()
            if "async" in failures:
                raise RuntimeError("synthetic-secret-cleanup")

        client.aclose = close
        return client

    def constructor(**kwargs):
        events.append("sdk construct")
        raise RuntimeError("synthetic-secret-constructor")

    monkeypatch.setattr(adapter, "_DiagnosticHttpxClient", sync)
    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(genai, "Client", constructor)
    with pytest.raises(DiagnosticDenied) as failure:
        diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    assert "synthetic-secret" not in str(failure.value)
    assert {"sync": "sync acquisition", "async": "async acquisition", "sdk": "SDK constructor"}[stage] in str(failure.value)
    assert events.count("sync close") == int(stage != "sync")
    assert events.count("async close") == int(stage == "sdk")
    assert events.count("sdk construct") == int(stage == "sdk")
    assert all(c.is_closed for c in acquired)
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("override", ["api_key", "credential_resolver", "transport", "base_url",
    "sdk_factory", "http_options", "authorization", "safety_source"])
def test_s2_factory_has_no_live_override_surface(offline_sdk, tmp_path, override):
    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    with pytest.raises(TypeError):
        diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence,
                                                         **{override: object()})
    with pytest.raises(TypeError):
        run_live(live_opt_in=True, **{override: object()})
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("environment", ["ambient", "credential-key", "sdk-debug"])
def test_s2_environment_is_denied_without_reading_credentials_or_constructing_sdk(
    offline_sdk, tmp_path, monkeypatch, environment,
):
    from google import genai

    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    calls = []

    class Ambient:
        def __iter__(self):
            pytest.fail("ambient environment must not be inspected")

        def get(self, *args):
            pytest.fail("ambient credential value must not be read")

    value = Ambient() if environment == "ambient" else {
        "GOOGLE_API_KEY" if environment == "credential-key" else "GOOGLE_GENAI_CLIENT_MODE":
        "synthetic-secret-environment"}

    def constructor(**kwargs):
        calls.append("constructor")
        pytest.fail("environment denied before SDK construction")

    isolated_environment = adapter.os.environ
    monkeypatch.setattr(adapter.os, "environ", value)
    monkeypatch.setattr(genai, "Client", constructor)
    try:
        with pytest.raises(DiagnosticDenied, match="isolated") as failure:
            diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
        assert "synthetic-secret" not in str(failure.value)
    finally:
        # Pytest writes its own marker before fixture teardown; restore its
        # isolated mapping before it reaches that bookkeeping operation.
        monkeypatch.setattr(adapter.os, "environ", isolated_environment)
    assert calls == [] and offline_sdk.blocked_calls == []


@pytest.mark.parametrize("package", ["google-genai", "httpx"])
def test_s2_package_baseline_mismatch_never_constructs(offline_sdk, tmp_path, monkeypatch, package):
    from google import genai
    from knowledge_index import google_embedding_adapter as adapter

    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    real_version = adapter.version
    monkeypatch.setattr(adapter, "version", lambda name: "unreviewed" if name == package else real_version(name))
    monkeypatch.setattr(genai, "Client", lambda **kwargs: pytest.fail("pin mismatch cannot construct"))
    with pytest.raises(ValueError):
        diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    assert offline_sdk.blocked_calls == []


def test_s2_async_dispatch_denied_and_no_network_or_credentials(offline_sdk, tmp_path):
    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    try:
        pending = handle._async.post("https://offline.invalid/denied", content=b"synthetic")
        try:
            with pytest.raises(DiagnosticDenied, match="asynchronous"):
                pending.send(None)
        finally:
            pending.close()
        assert handle._guard.delegations == 0 and handle._guard._delegate.observations == []
        assert offline_sdk.blocked_calls == []
    finally:
        handle.close()


@pytest.mark.parametrize("status", ["VERIFIED", "HOST_CONTROLLED_E4_ONLY"])
def test_s2_no_synthetic_to_live_proof_upgrade(offline_sdk, tmp_path, status):
    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    with pytest.raises(DiagnosticDenied, match="synthetic"):
        diagnostic._create_diagnostic_offline_sdk_client(prepared, approved,
                                                         replace(evidence, status=status))
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        diagnostic._live_safety(prepared.record, prepared.texts)


@pytest.mark.parametrize("fault", [None, "guard", "timeout", "cleanup", "constructor"])
def test_s2_test_only_composition_preserves_s1_reservation_and_completion_order(
    offline_sdk, tmp_path, monkeypatch, fault,
):
    from google import genai

    prepared, approved, evidence, _ = _s2_fixed_case(tmp_path)
    path = tmp_path / "s2-isolated.sqlite"
    usage = TemporaryDiagnosticUsage(path)
    events = []
    real_connect = usage._connect

    def connect(**kwargs):
        db = real_connect(**kwargs)
        db.set_trace_callback(lambda sql: events.append(
            "commit" if sql == "COMMIT" else "update" if sql.startswith("UPDATE") else "sql"))
        return db

    monkeypatch.setattr(usage, "_connect", connect)
    diagnostic._reserve(prepared, usage, "s2-attempt")
    assert ledger_rows(path) == [("s2-attempt", "IN_FLIGHT", len(prepared.texts),
                                  prepared.usage_units, prepared.cost_micro_units, "UNKNOWN")]
    assert events.count("commit") == 1
    real_constructor = genai.Client

    def constructor(**kwargs):
        events.append("constructor")
        assert ledger_rows(path)[0][1] == "IN_FLIGHT"
        if fault == "constructor":
            raise RuntimeError("synthetic-secret-constructor")
        return real_constructor(**kwargs)

    monkeypatch.setattr(genai, "Client", constructor)
    if fault == "constructor":
        with pytest.raises(DiagnosticDenied):
            diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
        assert events.count("update") == 0
        assert ledger_rows(path)[0][1] == "IN_FLIGHT"
    else:
        if fault == "guard":
            evidence = replace(evidence, sdk_body_sha256="0" * 64)
        handle = diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence,
            response=adapter._DiagnosticMockResponse(transport_error="timeout" if fault == "timeout" else None))
        sdk_close, sync_close, async_close = handle._sdk.close, handle._sync.close, handle._async.aclose

        def close(call, name):
            events.append(name)
            assert ledger_rows(path)[0][1] == "IN_FLIGHT"
            call()
            if fault == "cleanup" and name == "sync close":
                raise RuntimeError("synthetic-secret-cleanup")

        async def aclose():
            events.append("async close")
            assert ledger_rows(path)[0][1] == "IN_FLIGHT"
            await async_close()

        monkeypatch.setattr(handle._sdk, "close", lambda: close(sdk_close, "sdk close"))
        monkeypatch.setattr(handle._sync, "close", lambda: close(sync_close, "sync close"))
        monkeypatch.setattr(handle._async, "aclose", aclose)
        if fault in ("guard", "timeout"):
            with pytest.raises((DiagnosticDenied, httpx.ReadTimeout)):
                handle.embed_prepared()
        else:
            assert len(handle.embed_prepared().embeddings) == len(prepared.texts)
        assert handle._guard.delegations == (0 if fault == "guard" else 1)
        assert len(handle._guard._delegate.observations) == handle._guard.delegations
        if fault == "cleanup":
            with pytest.raises(DiagnosticDenied):
                handle.close()
            assert events.count("update") == 0
            assert ledger_rows(path)[0][1] == "IN_FLIGHT"
        else:
            handle.close()
            outcome = {None: "SUCCESS", "guard": "FAILED", "timeout": "TIMEOUT"}[fault]
            usage.finish(prepared.record, "s2-attempt", outcome)
            assert ledger_rows(path)[0][1] == outcome
            assert events.count("update") == 1 and events.count("commit") == 2
            assert events.index("async close") < events.index("update")
        if fault == "cleanup":
            with pytest.raises(DiagnosticDenied):
                handle.close()  # Previous failure cannot become a false cleanup PASS.
        else:
            handle.close()
        assert [events.count(n) for n in ("sdk close", "sync close", "async close")] == [1, 1, 1]
    rows = ledger_rows(path)
    assert len(rows) == 1 and rows[0][3:] == (prepared.usage_units, prepared.cost_micro_units, "UNKNOWN")
    assert events.count("constructor") == 1
    assert events.index("commit") < events.index("constructor")
    with pytest.raises(DiagnosticDenied):
        TemporaryDiagnosticUsage(path).reserve(prepared.record, "s2-next", items=1,
                                              usage_units=1, cost_micro_units=1)
    assert ledger_rows(path) == rows and offline_sdk.blocked_calls == []


@pytest.mark.parametrize("fault", [None, "constructor", "sdk_close", "sync_close",
                                   "async_close", "completion", "timeout", "connection",
                                   "response_loss", "crash", "response_contract"])
def test_s1_attempt_counts_durable_rows_and_cleanup_before_completion(
    offline_sdk, tmp_path, monkeypatch, fault,
):
    request, grant, _, texts = request_and_record(tmp_path)
    path = tmp_path / "isolated-usage.sqlite"
    events = []
    real_factory = diagnostic._create_diagnostic_offline_sdk_client
    real_reserve, real_finish = TemporaryDiagnosticUsage.reserve, TemporaryDiagnosticUsage.finish
    real_connect = TemporaryDiagnosticUsage._connect

    def connect(self, **kwargs):
        db = real_connect(self, **kwargs)

        def trace(sql):
            if sql == "COMMIT":
                events.append("completion_commit" if "completion" in events else "reservation_commit")
            elif sql.startswith("UPDATE"):
                events.append("completion_update")

        db.set_trace_callback(trace)
        return db

    class SyntheticCrash(BaseException):
        pass

    def reserve(self, *args, **kwargs):
        events.append("reserve")
        return real_reserve(self, *args, **kwargs)

    def finish(self, *args, **kwargs):
        events.append("completion")
        assert events[-4:-1] == ["sdk_close", "sync_close", "async_close"]
        assert ledger_rows(path)[0][1] == "IN_FLIGHT"
        if fault == "completion":
            raise DiagnosticDenied("completion unavailable; reservation retained")
        return real_finish(self, *args, **kwargs)

    def factory(*args, **kwargs):
        events.append("constructor")
        assert ledger_rows(path)[0][1] == "IN_FLIGHT"
        if fault == "constructor":
            raise RuntimeError("synthetic-secret-constructor")
        owned = real_factory(*args, **kwargs)
        sdk, sync, async_client = owned._sdk, owned._sync, owned._async
        real_embed = owned.embed_prepared
        real_sdk_close, real_sync_close, real_async_close = sdk.close, sync.close, async_client.aclose

        def delegate():
            events.append("delegation")
            response = real_embed()
            if fault == "response_loss":
                raise httpx.ConnectError("synthetic-secret-response-loss")
            if fault == "crash":
                raise SyntheticCrash()
            if fault == "response_contract":
                return object()
            return response

        def close(client, stage):
            events.append(stage)
            assert ledger_rows(path)[0][1:] == (
                "IN_FLIGHT", 1, sum(len(t.encode("utf-8")) for t in texts), 100, "UNKNOWN")
            (real_sdk_close if stage == "sdk_close" else real_sync_close)()
            if fault == stage:
                raise RuntimeError("synthetic-secret-cleanup")

        async def aclose():
            events.append("async_close")
            await real_async_close()
            if fault == "async_close":
                raise RuntimeError("synthetic-secret-async-cleanup")

        monkeypatch.setattr(owned, "embed_prepared", delegate)
        monkeypatch.setattr(sdk, "close", lambda: close(sdk, "sdk_close"))
        monkeypatch.setattr(sync, "close", lambda: close(sync, "sync_close"))
        monkeypatch.setattr(async_client, "aclose", aclose)
        return owned

    monkeypatch.setattr(TemporaryDiagnosticUsage, "reserve", reserve)
    monkeypatch.setattr(TemporaryDiagnosticUsage, "finish", finish)
    monkeypatch.setattr(TemporaryDiagnosticUsage, "_connect", connect)
    monkeypatch.setattr(diagnostic, "_create_diagnostic_offline_sdk_client", factory)
    args = {"mock_transport_error": fault} if fault in ("timeout", "connection") else {}
    if fault == "crash":
        with pytest.raises(SyntheticCrash):
            execute(tmp_path, request, grant, **args)
    elif fault in ("constructor", "sdk_close", "sync_close", "async_close", "completion"):
        with pytest.raises(DiagnosticDenied) as failure:
            execute(tmp_path, request, grant, **args)
        assert "synthetic-secret" not in str(failure.value)
    else:
        result = execute(tmp_path, request, grant, **args)
        expected = {None: "SUCCESS", "timeout": "TIMEOUT", "connection": "UNKNOWN",
                    "response_loss": "UNKNOWN", "response_contract": "FAILED"}[fault]
        assert result.outcome == expected and result.usage == "UNKNOWN"
        assert result.provider_attempts == 1
    completed = fault in (None, "timeout", "connection", "response_loss", "response_contract")
    assert events.count("reserve") == events.count("constructor") == 1
    assert events.count("delegation") == (0 if fault == "constructor" else 1)
    assert events.count("completion") == int(completed or fault == "completion")
    assert events.count("reservation_commit") == 1
    assert events.index("reservation_commit") < events.index("constructor")
    assert events.count("completion_commit") == events.count("completion_update") == int(completed)
    if completed:
        assert events.index("async_close") < events.index("completion_update") < events.index("completion_commit")
    if fault != "constructor":
        for stage in ("sdk_close", "sync_close", "async_close"):
            assert events.count(stage) == 1
    rows = ledger_rows(path)
    assert len(rows) == 1 and rows[0][3:] == (
        sum(len(t.encode("utf-8")) for t in texts), 100, "UNKNOWN")
    if not completed:
        assert rows[0][1] == "IN_FLIGHT"
    else:
        assert rows[0][1] == expected
    before = list(events)
    # Restart/next identity must stop before constructor, delegation or completion.
    with pytest.raises(DiagnosticDenied):
        execute(tmp_path, request, grant, request_id="synthetic-request-2")
    assert events[len(before):] == ["reserve"]
    assert ledger_rows(path) == rows


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", 2**63, 1, 100])
def test_s1_caller_usage_must_equal_decision_before_reservation(
    offline_sdk, tmp_path, monkeypatch, value,
):
    request, grant, _, texts = request_and_record(tmp_path)
    assert value != sum(len(t.encode("utf-8")) for t in texts)
    counts = {"reserve": 0, "construct": 0}

    def forbidden(stage):
        def call(*args, **kwargs):
            counts[stage] += 1
            pytest.fail("denied input reached a mutation")
        return call

    monkeypatch.setattr(TemporaryDiagnosticUsage, "reserve", forbidden("reserve"))
    monkeypatch.setattr(diagnostic, "_create_diagnostic_offline_sdk_client", forbidden("construct"))
    with pytest.raises(DiagnosticDenied):
        execute(tmp_path, request, grant, conservative_usage_units=value)
    assert counts == {"reserve": 0, "construct": 0}
    assert ledger_rows(tmp_path / "isolated-usage.sqlite") == []


def test_s1_revalidation_detects_source_change_before_reservation(
    offline_sdk, tmp_path, monkeypatch,
):
    request, grant, _, _ = request_and_record(tmp_path)
    calls = []

    def resolver():
        calls.append("resolve")
        return (grant.source_revision, len(calls) == 1)

    monkeypatch.setattr(diagnostic, "_create_diagnostic_offline_sdk_client",
                        lambda *_: pytest.fail("source changed before reservation"))
    with pytest.raises(DiagnosticDenied, match="source revision"):
        execute(tmp_path, request, grant, source_resolver=resolver)
    assert calls == ["resolve", "resolve"]
    assert ledger_rows(tmp_path / "isolated-usage.sqlite") == []


def test_s1_equal_synthetic_value_never_establishes_live_proof(offline_sdk, tmp_path):
    request, grant, _, texts = request_and_record(tmp_path)
    units = sum(len(t.encode("utf-8")) for t in texts)
    with pytest.raises(DiagnosticDenied, match="not live applicable"):
        diagnostic._preflight(
            load_record=lambda: grant,
            resolve_facts=lambda *_: facts(
                credential_profile_identity=request.capability.profile_reference.value),
            safety=lambda record, texts: (diagnostic._offline_proof(record),
                diagnostic._offline_safety_basis(record, texts), units, 100),
            request=request, live_opt_in=True,
        )
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        diagnostic._live_safety(grant, texts)
    for override in ("usage_observed", "conservative_usage_units", "usage", "safety",
                     "client", "transport", "source"):
        with pytest.raises(TypeError):
            run_live(request, live_opt_in=True, **{override: units})


@pytest.mark.parametrize("mode", ["commit_before", "commit_after"])
def test_s1_reservation_commit_error_never_constructs_or_delegates(
    offline_sdk, tmp_path, monkeypatch, mode,
):
    from test_knowledge_gemini_diagnostic_controls import CompletionFaultConnection

    request, grant, _, texts = request_and_record(tmp_path)
    path = tmp_path / "isolated-usage.sqlite"
    connect, events = TemporaryDiagnosticUsage._connect, []
    monkeypatch.setattr(TemporaryDiagnosticUsage, "_connect", lambda self, **kwargs:
        CompletionFaultConnection(connect(self, **kwargs), mode, events))
    monkeypatch.setattr(diagnostic, "_create_diagnostic_offline_sdk_client",
                        lambda *_: pytest.fail("ambiguous reservation cannot construct"))
    monkeypatch.setattr(TemporaryDiagnosticUsage, "finish",
                        lambda *_: pytest.fail("no attempt can complete"))
    with pytest.raises(DiagnosticDenied, match="usage lock or record") as failure:
        execute(tmp_path, request, grant)
    assert "synthetic-secret" not in str(failure.value)
    assert events.count("commit") == 1 and events.count("close") == 2
    expected = [] if mode == "commit_before" else [(
        "synthetic-request-1", "IN_FLIGHT", 1,
        sum(len(t.encode("utf-8")) for t in texts), 100, "UNKNOWN")]
    assert ledger_rows(path) == expected


def request_and_record(tmp_path, *, kind="document", count=1):
    document = fixture_request(tmp_path, count=count)
    request = document if kind == "document" else QueryEmbeddingRequest(
        document.build_identity,
        CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"),
        document.capability, document.limits,
    )
    actual_kind, identities, texts = _envelopes(request)
    scope = (
        request.approved_corpus_id if kind == "document"
        else "build:" + request.frozen_build_identity
    )
    grant = record(
        kind=actual_kind, commitment=input_commitment(actual_kind, identities, texts),
        input_scope_identity=scope, maximum_items=max(4, count),
        credential_profile_identity=request.capability.profile_reference.value,
    )
    return request, grant, identities, texts


def approved_input(request, grant):
    kind, identities, envelopes = _envelopes(request)
    if kind == "document":
        mapping = dict(request.approved_document_ids)
        items = [{
            "chunk_identity": chunk.chunk_identity,
            "document_identity": chunk.document_identity,
            "approved_document_id": mapping[chunk.document_identity],
            "chunk_content": chunk.content,
            "envelope": envelope,
        } for chunk, envelope in zip(request.chunks, envelopes, strict=True)]
    else:
        items = [{"frozen_build_identity": request.frozen_build_identity,
                  "canonical_query": request.query.text, "envelope": envelopes[0]}]
    material = {
        "schema_version": 1, "campaign_id": grant.campaign_id,
        "source_revision": grant.source_revision, "purpose": grant.purpose,
        "kind": kind, "scope_identity": grant.input_scope_identity,
        "input_commitment": input_commitment(kind, identities, envelopes),
        "ordered_items": items,
    }
    return ApprovedDiagnosticInput.from_json(json.dumps(material).encode())


def synthetic_host_evidence(request, grant):
    """Host-custody fixture only; raw digest is not a live provider proof."""
    kind, identities, texts = _envelopes(request)
    return diagnostic.DiagnosticSdkByteEvidence(
        "HOST_CONTROLLED_E4_ONLY", "PINNED_SDK_HTTPX_REQUEST_BODY",
        "host-admin-e4-001", datetime(2099, 1, 1, tzinfo=timezone.utc),
        grant.campaign_id, grant.source_revision,
        diagnostic._authorization_record_digest(grant), grant.provider_identity,
        grant.model_identity, grant.sdk_baseline, grant.api_version, kind,
        grant.input_scope_identity, identities, grant.input_commitment,
        diagnostic._offline_safety_basis(grant, texts).serialized_request_sha256,
        "a" * 64, 100, "POST",
        "https://generativelanguage.googleapis.com/v1/models/"
        "gemini-embedding-2:batchEmbedContents", 768, 30_000, 1,
        pm_approval_reference=grant.pm_approval_reference,
        valid_from=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )


def test_approved_document_artifact_rejects_mapping_and_reordering(tmp_path):
    request, grant, _, _ = request_and_record(tmp_path, count=2)
    kind, identities, envelopes = _envelopes(request)
    mapping = dict(request.approved_document_ids)
    items = [{"chunk_identity": chunk.chunk_identity,
              "document_identity": chunk.document_identity,
              "approved_document_id": mapping[chunk.document_identity],
              "chunk_content": chunk.content, "envelope": envelope}
             for chunk, envelope in zip(request.chunks, envelopes, strict=True)]
    material = {"schema_version": 1, "campaign_id": grant.campaign_id,
                "source_revision": grant.source_revision, "purpose": grant.purpose,
                "kind": kind, "scope_identity": grant.input_scope_identity,
                "input_commitment": input_commitment(kind, identities, envelopes),
                "ordered_items": items}
    for changed_items in (
        [{**items[0], "approved_document_id": "unapproved-id"}, items[1]],
        list(reversed(items)),
        [{**items[0], "chunk_content": "altered"}, items[1]],
    ):
        with pytest.raises(DiagnosticDenied):
            ApprovedDiagnosticInput.from_json(json.dumps(
                {**material, "ordered_items": changed_items}).encode())


# Independent fixture generation is outside target orchestration and counters.
_fixture_preflight = diagnostic._preflight


def fixed_offline_evidence(request, grant):
    prepared = _fixture_preflight(
        load_record=lambda: grant,
        resolve_facts=lambda *_: facts(credential_profile_identity=request.capability.profile_reference.value),
        safety=lambda record, texts: (diagnostic._offline_proof(record),
            diagnostic._offline_safety_basis(record, texts),
            sum(len(t.encode("utf-8")) for t in texts), 100),
        request=request, live_opt_in=True, offline_harness=True,
    )
    approved = approved_input(request, grant)
    evidence, _ = diagnostic.observe_offline_sdk_bytes(prepared, approved)
    return approved, evidence


def s3_case(tmp_path, *, kind="document", count=2):
    request, grant, _, _ = request_and_record(tmp_path, kind=kind, count=count)
    if count == 54:
        grant = replace(grant, maximum_aggregate_tokens=100_000,
                        maximum_payload_bytes=100_000, maximum_usage_units=100_000)
    approved, evidence = fixed_offline_evidence(request, grant)
    source_path = tmp_path / "s3-fixed-source.json"
    source_path.write_text(json.dumps(asdict(grant)), encoding="utf-8")
    path = tmp_path / "s3-offline.sqlite"
    usage = TemporaryDiagnosticUsage(path)
    args = dict(source_resolver=lambda: (grant.source_revision, True),
                request_id="s3-first", live_opt_in=True,
                conservative_usage_units=sum(len(t.encode("utf-8")) for t in approved.envelopes),
                conservative_cost_micro_units=100, approved_input=approved,
                expected_sdk_evidence=evidence)

    def run(**changes):
        return run_offline_simulation(OfflineAuthorizationSource(source_path),
            facts(credential_profile_identity=request.capability.profile_reference.value),
            request, case.usage, **{**args, **changes})

    case = SimpleNamespace(run=run, path=path, usage=usage, grant=grant,
                           request=request, approved=approved, evidence=evidence,
                           source_path=source_path, args=args)
    return case


def s3_probe(monkeypatch, case):
    """Observe the actual shared entry, SQLite commits and owned resources."""
    events, handles = [], []
    reserve, finish = TemporaryDiagnosticUsage.reserve, TemporaryDiagnosticUsage.finish
    factory, connect = diagnostic._create_diagnostic_offline_sdk_client, TemporaryDiagnosticUsage._connect
    delegate = adapter._DiagnosticClosedMockTransport.handle_request

    def reserve_call(self, *args, **kwargs):
        events.append("reserve")
        return reserve(self, *args, **kwargs)

    def finish_call(self, *args, **kwargs):
        events.append("finish")
        assert events.count("sdk cleanup") == events.count("sync cleanup") == events.count("async cleanup") == 1
        assert ledger_rows(case.path)[0][1] == "IN_FLIGHT"
        return finish(self, *args, **kwargs)

    def connect_call(self, **kwargs):
        db = connect(self, **kwargs)
        def trace(sql):
            if sql == "COMMIT":
                events.append("commit")
            if sql.startswith("UPDATE"):
                events.append("update")
        db.set_trace_callback(trace)
        return db

    def factory_call(*args, **kwargs):
        events.append("constructor")
        assert events.count("commit") == 1
        assert ledger_rows(case.path)[0][1] == "IN_FLIGHT"
        handle = factory(*args, **kwargs)
        handles.append(handle)
        for resource, name in ((handle._sdk, "sdk"), (handle._sync, "sync")):
            close = resource.close
            def closing(close=close, name=name):
                events.append(name + " cleanup")
                assert ledger_rows(case.path)[0][1] == "IN_FLIGHT"
                close()
            monkeypatch.setattr(resource, "close", closing)
        close_async = handle._async.aclose
        async def aclose():
            events.append("async cleanup")
            assert ledger_rows(case.path)[0][1] == "IN_FLIGHT"
            await close_async()
        monkeypatch.setattr(handle._async, "aclose", aclose)
        return handle

    def delegation(self, request):
        events.append("delegate")
        assert ledger_rows(case.path)[0][1] == "IN_FLIGHT"
        return delegate(self, request)

    monkeypatch.setattr(TemporaryDiagnosticUsage, "reserve", reserve_call)
    monkeypatch.setattr(TemporaryDiagnosticUsage, "finish", finish_call)
    monkeypatch.setattr(TemporaryDiagnosticUsage, "_connect", connect_call)
    monkeypatch.setattr(diagnostic, "_create_diagnostic_offline_sdk_client", factory_call)
    monkeypatch.setattr(adapter._DiagnosticClosedMockTransport, "handle_request", delegation)
    # Evidence preparation must never occur inside the target entry.
    monkeypatch.setattr(diagnostic, "observe_offline_sdk_bytes", lambda *_:
                        pytest.fail("target attempt cannot approve its own evidence"))
    monkeypatch.setattr(diagnostic, "_create_gemini_offline_mock_client", lambda *_:
                        pytest.fail("legacy dispatch fallback is forbidden"))
    return events, handles


@pytest.mark.parametrize("kind,count", [("document", 54), ("query", 1)])
def test_s3_shared_entry_identity_counts_and_restart(offline_sdk, tmp_path, monkeypatch, kind, count):
    case = s3_case(tmp_path, kind=kind, count=count)
    finish = TemporaryDiagnosticUsage.finish
    events, handles = s3_probe(monkeypatch, case)
    result = case.run()
    assert result.outcome == "SUCCESS" and result.vector_count == count and result.usage == "UNKNOWN"
    assert result.provider_attempts == 1 and result.ordered_identities == case.approved.identities
    handle = handles[0]
    guard = handle._guard
    request, body, stream = guard._delegate.observations[0]
    assert request is handle._sync._final_request is guard.guarded_request is guard.delegated_request
    assert body == stream == guard.delegated_body == request.content
    assert sha256(body).hexdigest() == case.evidence.sdk_body_sha256 and len(body) == case.evidence.sdk_body_bytes
    assert guard.delegations == 1 and handle._closed and not handle.cleanup_failures
    assert events == ["reserve", "commit", "constructor", "delegate", "sdk cleanup", "sync cleanup",
                      "async cleanup", "finish", "update", "commit"]
    rows = ledger_rows(case.path)
    assert rows == [("s3-first", "SUCCESS", count, case.args["conservative_usage_units"], 100, "UNKNOWN")]
    case.usage = TemporaryDiagnosticUsage(case.path)
    for request_id in ("s3-first", "s3-next"):
        with pytest.raises(DiagnosticDenied):
            case.run(request_id=request_id)
    with pytest.raises(DiagnosticDenied):
        handle.embed_prepared()
    with pytest.raises(DiagnosticDenied):
        finish(case.usage, case.grant, "s3-first", "SUCCESS")
    assert ledger_rows(case.path) == rows and len(handles) == events.count("delegate") == 1
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("field,value", [
    ("status", "VERIFIED"), ("valid_until", datetime(2000, 1, 1, tzinfo=timezone.utc)),
    ("canonical_json_sha256", "0" * 64), ("authorization_record_sha256", "0" * 64),
    ("input_commitment", "0" * 64), ("sdk_body_bytes", 0), ("sdk_body_sha256", "invalid"),
    ("ordered_identities", ("wrong",)), ("http_method", "GET"), ("attempts", 2),
    ("timeout_ms", 31_000), ("api", "v1beta"), ("request_url", "https://offline.invalid/"),
    ("schema_version", True), ("artifact_type", "other"), ("attempts", True),
    ("valid_from", datetime(2099, 1, 1, tzinfo=timezone.utc)),
])
def test_s3_bad_fixed_evidence_has_no_reservation(offline_sdk, tmp_path, monkeypatch, field, value):
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    with pytest.raises(DiagnosticDenied):
        case.run(expected_sdk_evidence=replace(case.evidence, **{field: value}))
    assert events == handles == [] and ledger_rows(case.path) == []
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("change", [
    {"approved_input": None}, {"expected_sdk_evidence": None}, {"live_opt_in": False},
    {"conservative_usage_units": True}, {"conservative_cost_micro_units": 0},
    {"mock_transport_error": "other"}, {"mock_response_count": 129},
])
def test_s3_invalid_entry_cannot_reserve_or_fallback(offline_sdk, tmp_path, monkeypatch, change):
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    with pytest.raises(DiagnosticDenied):
        case.run(**change)
    assert events == handles == [] and ledger_rows(case.path) == []


@pytest.mark.parametrize("changes,outcome,field", [
    ({"mock_http_status": 302}, "FAILED", "local_serialization"),
    ({"mock_http_status": 429}, "FAILED", "provider_rate_limit"),
    ({"mock_http_status": 503}, "FAILED", "provider_service"),
    ({"mock_transport_error": "timeout"}, "TIMEOUT", "transport_timeout"),
    ({"mock_transport_error": "connection"}, "UNKNOWN", "transport"),
    ({"mock_response_count": 1}, "FAILED", "response_cardinality"),
    ({"mock_response_mode": "reverse"}, "FAILED", "response_order"),
    ({"mock_response_mode": "wrong_dimension"}, "FAILED", "response_dimension"),
    ({"mock_response_mode": "unnormalized"}, "FAILED", "response_normalization"),
    ({"mock_response_mode": "malformed"}, "FAILED", "response_contract"),
])
def test_s3_response_failure_matrix_has_one_send_no_refund(offline_sdk, tmp_path, monkeypatch, changes, outcome, field):
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    result = case.run(**changes)
    assert result.outcome == outcome and result.failure_field == field and result.usage == "UNKNOWN"
    assert result.provider_attempts == events.count("delegate") == 1
    assert events.count("reserve") == events.count("constructor") == events.count("finish") == 1
    assert events.count("commit") == 2 and events.count("update") == 1
    assert events.index("async cleanup") < events.index("update")
    rows = ledger_rows(case.path)
    assert rows == [("s3-first", outcome, 2, case.args["conservative_usage_units"], 100, "UNKNOWN")]
    with pytest.raises(DiagnosticDenied):
        case.run(request_id="s3-next")
    assert len(handles) == 1 and handles[0]._guard.delegations == 1 and ledger_rows(case.path) == rows
    assert "synthetic" not in result.failure_detail and offline_sdk.blocked_calls == []


@pytest.mark.parametrize("fault", ["raw_digest", "body_mutation", "policy", "response_loss", "cleanup"])
def test_s3_after_reservation_faults_preserve_e4_and_cleanup(offline_sdk, tmp_path, monkeypatch, fault):
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    expected_d = 0 if fault in ("raw_digest", "body_mutation", "policy") else 1
    if fault == "body_mutation":
        build = httpx.Client.build_request
        def mutated(self, *args, **kwargs):
            original = build(self, *args, **kwargs)
            body = json.dumps(json.loads(original.content), indent=2).encode()
            return httpx.Request(original.method, original.url, content=body,
                headers={**dict(original.headers), "content-length": str(len(body))}, extensions=original.extensions)
        monkeypatch.setattr(httpx.Client, "build_request", mutated)
    factory = diagnostic._create_diagnostic_offline_sdk_client
    def altered(*args, **kwargs):
        handle = factory(*args, **kwargs)
        if fault == "policy":
            handle._sdk._api_client._http_options.retry_options.attempts = 2
        if fault == "response_loss":
            invoke = handle.embed_prepared
            def loss():
                invoke()
                raise httpx.ConnectError("synthetic-secret-response-loss")
            monkeypatch.setattr(handle, "embed_prepared", loss)
        if fault == "cleanup":
            close = handle._sync.close
            def fail():
                close()
                raise RuntimeError("synthetic-secret-cleanup")
            monkeypatch.setattr(handle._sync, "close", fail)
        return handle
    monkeypatch.setattr(diagnostic, "_create_diagnostic_offline_sdk_client", altered)
    kwargs = {"expected_sdk_evidence": replace(case.evidence, sdk_body_sha256="0" * 64)} if fault == "raw_digest" else {}
    if fault == "cleanup":
        with pytest.raises(DiagnosticDenied) as failure:
            case.run(**kwargs)
        assert "synthetic-secret" not in str(failure.value)
    else:
        result = case.run(**kwargs)
        assert result.outcome == ("UNKNOWN" if fault == "response_loss" else "FAILED")
    handle = handles[0]
    assert handle._guard.delegations == events.count("delegate") == expected_d
    assert handle._guard._evidence.sdk_body_sha256 == kwargs.get("expected_sdk_evidence", case.evidence).sdk_body_sha256
    assert [events.count(n + " cleanup") for n in ("sdk", "sync", "async")] == [1, 1, 1]
    assert events.count("finish") == int(fault != "cleanup")
    rows = ledger_rows(case.path)
    assert rows[0][1] == ("IN_FLIGHT" if fault == "cleanup" else result.outcome)
    assert rows[0][3:] == (case.args["conservative_usage_units"], 100, "UNKNOWN")
    with pytest.raises(DiagnosticDenied):
        case.run(request_id="s3-next")
    assert ledger_rows(case.path) == rows and len(handles) == 1 and offline_sdk.blocked_calls == []


@pytest.mark.parametrize("stage,fail_cleanup", [
    ("sync", False), ("async", False), ("async", True), ("sdk", False), ("sdk", True),
])
def test_s3_partial_construction_has_independent_cleanup(offline_sdk, tmp_path, monkeypatch, stage, fail_cleanup):
    from google import genai
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    cleanup, acquired = [], []
    sync_type, async_type = adapter._DiagnosticHttpxClient, httpx.AsyncClient

    def sync(*args, **kwargs):
        if stage == "sync":
            raise RuntimeError("synthetic-secret-sync-acquisition")
        resource = sync_type(*args, **kwargs)
        acquired.append(resource)
        close = resource.close
        def closing():
            cleanup.append("sync")
            close()
            if fail_cleanup:
                raise RuntimeError("synthetic-secret-sync-close")
        monkeypatch.setattr(resource, "close", closing)
        return resource

    def async_client(*args, **kwargs):
        if stage == "async":
            raise RuntimeError("synthetic-secret-async-acquisition")
        resource = async_type(*args, **kwargs)
        acquired.append(resource)
        close = resource.aclose
        async def closing():
            cleanup.append("async")
            await close()
            if fail_cleanup:
                raise RuntimeError("synthetic-secret-async-close")
        monkeypatch.setattr(resource, "aclose", closing)
        return resource

    def sdk(**kwargs):
        events.append("sdk constructor")
        raise RuntimeError("synthetic-secret-sdk-constructor")

    monkeypatch.setattr(adapter, "_DiagnosticHttpxClient", sync)
    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(genai, "Client", sdk)
    with pytest.raises(DiagnosticDenied) as failure:
        case.run()
    assert "synthetic-secret" not in str(failure.value)
    assert cleanup == {"sync": [], "async": ["sync"], "sdk": ["sync", "async"]}[stage]
    assert all(r.is_closed for r in acquired)
    assert events.count("reserve") == events.count("constructor") == 1
    assert events.count("sdk constructor") == int(stage == "sdk")
    assert events.count("delegate") == events.count("finish") == 0 and handles == []
    assert ledger_rows(case.path) == [("s3-first", "IN_FLIGHT", 2, case.args["conservative_usage_units"], 100, "UNKNOWN")]
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("mode", ["update", "update_after", "commit_before", "commit_after",
                                  "ambiguous_after", "rollback", "commit_after_rollback", "close"])
def test_s3_atomic_completion_faults_never_repeat_send_or_finish(offline_sdk, tmp_path, monkeypatch, mode):
    import sqlite3
    from test_knowledge_gemini_diagnostic_controls import CompletionFaultConnection
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    connect, finish = case.usage._connect, TemporaryDiagnosticUsage.finish
    faults = []
    completing = False

    def fault_connect(*, read_only=False):
        if read_only:
            faults.append("read_only")
            assert faults[-2] == "close"
            if mode == "ambiguous_after":
                raise sqlite3.OperationalError("synthetic-secret-readback")
            return connect(read_only=True)
        db = connect()
        return CompletionFaultConnection(db, mode, faults) if completing else db

    def finish_call(self, *args):
        nonlocal completing
        completing = True
        return finish(self, *args)

    monkeypatch.setattr(case.usage, "_connect", fault_connect)
    monkeypatch.setattr(TemporaryDiagnosticUsage, "finish", finish_call)
    if mode in ("commit_after", "commit_after_rollback"):
        assert case.run().outcome == "SUCCESS"
    else:
        with pytest.raises(DiagnosticDenied, match="UNKNOWN") as failure:
            case.run()
        assert "synthetic-secret" not in str(failure.value)
    assert events.count("reserve") == events.count("constructor") == events.count("delegate") == events.count("finish") == 1
    assert faults.count("update") == faults.count("read_only") == faults.count("close") == 1
    assert faults.count("commit") == int(mode not in ("update", "update_after"))
    assert events.index("async cleanup") < events.index("finish")
    rows = ledger_rows(case.path)
    outcome = "SUCCESS" if mode in ("commit_after", "ambiguous_after", "commit_after_rollback") else "IN_FLIGHT"
    assert rows == [("s3-first", outcome, 2, case.args["conservative_usage_units"], 100, "UNKNOWN")]
    # Fresh object/connection, no completion replay. Unknown durable result blocks.
    with pytest.raises(DiagnosticDenied):
        TemporaryDiagnosticUsage(case.path).reserve(case.grant, "restart", items=1, usage_units=1, cost_micro_units=1)
    assert len(handles) == handles[0]._guard.delegations == 1 and ledger_rows(case.path) == rows
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("same_id", [False, True])
def test_s3_concurrent_full_entries_have_at_most_one_reservation_and_send(offline_sdk, tmp_path, monkeypatch, same_id):
    from threading import Barrier
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    reserve = TemporaryDiagnosticUsage.reserve
    barrier = Barrier(2)
    def synchronized(self, *args, **kwargs):
        barrier.wait(timeout=10)
        return reserve(self, *args, **kwargs)
    monkeypatch.setattr(TemporaryDiagnosticUsage, "reserve", synchronized)
    def run(index):
        try:
            return case.run(request_id="same" if same_id else f"parallel-{index}").outcome
        except DiagnosticDenied:
            return "DENIED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (1, 2)))
    assert sorted(results) == ["DENIED", "SUCCESS"]
    assert len(ledger_rows(case.path)) == len(handles) == events.count("constructor") == events.count("delegate") == events.count("finish") == 1
    assert events.count("reserve") == 2 and events.count("commit") == 2
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("condition", ["IN_FLIGHT", "TIMEOUT", "UNKNOWN", "SUCCESS_UNKNOWN", "cost", "usage", "sum_overflow"])
def test_s3_existing_state_or_budget_denies_before_factory(offline_sdk, tmp_path, monkeypatch, condition):
    import sqlite3
    case = s3_case(tmp_path)
    # Explicit synthetic durable fixtures, never provider observations.
    outcome = condition if condition in ("IN_FLIGHT", "TIMEOUT", "UNKNOWN") else "SUCCESS"
    observed = "UNKNOWN" if condition in ("IN_FLIGHT", "TIMEOUT", "UNKNOWN", "SUCCESS_UNKNOWN") else "0"
    usage_units = case.grant.maximum_usage_units if condition == "usage" else 1
    cost = case.grant.maximum_cost_micro_units if condition == "cost" else 1
    with sqlite3.connect(case.path) as db:
        for i in range(2 if condition == "sum_overflow" else 1):
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?)", (case.grant.campaign_id,
                f"existing-{i}", outcome, 1, 2**62 if condition == "sum_overflow" else usage_units, cost, observed))
    rows = ledger_rows(case.path)
    events, handles = s3_probe(monkeypatch, case)
    with pytest.raises(DiagnosticDenied):
        case.run()
    assert events == ["reserve"] and handles == [] and ledger_rows(case.path) == rows
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("fault", ["missing_safety", "stale_safety", "source_changed", "authorization_changed", "input_changed"])
def test_s3_revalidation_failure_has_no_durable_side_effect(offline_sdk, tmp_path, monkeypatch, fault):
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    args = {}
    if fault in ("missing_safety", "stale_safety"):
        basis = diagnostic._offline_safety_basis
        monkeypatch.setattr(diagnostic, "_offline_safety_basis", lambda *a:
            None if fault == "missing_safety" else replace(basis(*a), valid_until=datetime(2000, 1, 1, tzinfo=timezone.utc)))
    elif fault == "source_changed":
        resolves = []
        def source():
            resolves.append(True)
            return case.grant.source_revision, len(resolves) == 1
        args["source_resolver"] = source
    elif fault == "authorization_changed":
        load = OfflineAuthorizationSource.load
        loads = []
        def changed(self):
            loads.append(True)
            value = load(self)
            return value if len(loads) == 1 else replace(value, maximum_cost_micro_units=value.maximum_cost_micro_units + 1)
        monkeypatch.setattr(OfflineAuthorizationSource, "load", changed)
    else:
        args["approved_input"] = replace(case.approved, envelopes=("changed",) + case.approved.envelopes[1:])
    with pytest.raises(DiagnosticDenied):
        case.run(**args)
    assert events == handles == [] and ledger_rows(case.path) == []


def test_s3_reentrant_concurrent_and_second_guard_entry_never_resends(offline_sdk, tmp_path, monkeypatch):
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    verify = diagnostic.compare_sdk_request_bytes
    refused = []
    def compare(*args, **kwargs):
        verify(*args, **kwargs)
        request = args[3]
        guard = handles[0]._guard
        def repeat():
            with pytest.raises(DiagnosticDenied):
                guard.handle_request(request)
            refused.append(True)
        repeat()  # Same-thread recursion during the first verification.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(repeat).result(timeout=10)
        assert guard.delegations == 0
    monkeypatch.setattr(diagnostic, "compare_sdk_request_bytes", compare)
    assert case.run().outcome == "SUCCESS"
    handle = handles[0]
    with pytest.raises(DiagnosticDenied):
        handle._guard.handle_request(handle._sync._final_request)
    assert len(refused) == 2 and handle._guard.delegations == events.count("delegate") == 1
    assert events.count("finish") == 1 and ledger_rows(case.path)[0][1] == "SUCCESS"
    assert offline_sdk.blocked_calls == []


def test_s3_caller_response_loss_after_commit_does_not_replay(offline_sdk, tmp_path, monkeypatch):
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    def caller_loses_response():
        case.run()
        raise ConnectionError("synthetic-caller-response-loss")
    with pytest.raises(ConnectionError):
        caller_loses_response()
    rows = ledger_rows(case.path)
    assert rows[0][1] == "SUCCESS" and rows[0][-1] == "UNKNOWN"
    for request_id in ("s3-first", "s3-restart"):
        with pytest.raises(DiagnosticDenied):
            case.run(request_id=request_id)
    assert len(handles) == events.count("delegate") == events.count("finish") == 1
    assert ledger_rows(case.path) == rows and offline_sdk.blocked_calls == []


def test_s3_live_ledger_subclass_and_live_overrides_are_denied(offline_sdk, tmp_path, monkeypatch):
    from knowledge_index.gemini_diagnostic_controls import LiveDiagnosticUsage
    case = s3_case(tmp_path)
    events, handles = s3_probe(monkeypatch, case)
    # No formal host constructor or mutation: just an uninitialized subclass
    # object, rejected by exact port type before any ledger access.
    live = object.__new__(LiveDiagnosticUsage)
    with pytest.raises(DiagnosticDenied):
        run_offline_simulation(OfflineAuthorizationSource(case.source_path), facts(), case.request, live, **case.args)
    for key in ("sdk_factory", "transport", "api_key", "usage_observed", "base_url"):
        with pytest.raises(TypeError):
            case.run(**{key: object()})
    assert events == handles == [] and ledger_rows(case.path) == [] and offline_sdk.blocked_calls == []


@pytest.mark.parametrize("phase", ["reserved", "delegated", "cleaned"])
def test_s3_hard_process_crash_restart_never_redispatches(offline_sdk, tmp_path, monkeypatch, phase):
    import subprocess
    import sys
    from pathlib import Path
    # Real process exit skips finally blocks. Only temporary fixture SQLite is
    # shared; stdout is test evidence, never a new durable dispatch authority.
    script = r'''
import json, os, sys
from pathlib import Path
import pytest
import test_knowledge_gemini_diagnostic as test
from test_knowledge_gemini_embedding2_serialization import offline_sdk
mp = pytest.MonkeyPatch()
fixture = offline_sdk.__wrapped__(mp)
probe = next(fixture)
case = test.s3_case(Path(sys.argv[1]))
events, handles = test.s3_probe(mp, case)
phase = sys.argv[2]
factory = test.diagnostic._create_diagnostic_offline_sdk_client
def die():
    assert probe.blocked_calls == []
    print(json.dumps({"events": events, "delegations": sum(h._guard.delegations for h in handles)}), flush=True)
    os._exit(77)
def construct(*a, **kw):
    if phase == "reserved":
        die()
    h = factory(*a, **kw)
    if phase == "delegated":
        invoke = h.embed_prepared
        def call():
            invoke()
            die()
        mp.setattr(h, "embed_prepared", call)
    if phase == "cleaned":
        close = h.close
        def cleanup():
            close()
            die()
        mp.setattr(h, "close", cleanup)
    return h
mp.setattr(test.diagnostic, "_create_diagnostic_offline_sdk_client", construct)
case.run()
raise AssertionError("crash checkpoint was not reached")
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-B", "-c", script, str(tmp_path), phase],
        cwd=root, env={"SYSTEMROOT": "C:\\Windows",
                       "PYTHONPATH": str(root / "src") + ";" + str(root / "tests"),
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                       "RUN_SPEC014_REAL_INTEGRATION": "0", "RUN_SPEC015_REAL_INTEGRATION": "0",
                       "RUN_SPEC017_REAL_RAG": "0"}, capture_output=True, text=True, timeout=45)
    assert result.returncode == 77, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["events"].count("reserve") == evidence["events"].count("commit") == 1
    assert evidence["events"].count("finish") == 0
    assert evidence["delegations"] == int(phase != "reserved")
    assert evidence["events"].count("constructor") == int(phase != "reserved")
    assert [evidence["events"].count(n + " cleanup") for n in ("sdk", "sync", "async")] == [int(phase == "cleaned")] * 3
    # Restart constructs a fresh offline port and validates fresh fixed evidence;
    # it cannot infer the crashed process's delegation count from the ledger.
    case = s3_case(tmp_path)
    rows = ledger_rows(case.path)
    assert rows == [("s3-first", "IN_FLIGHT", 2, case.args["conservative_usage_units"], 100, "UNKNOWN")]
    events, handles = s3_probe(monkeypatch, case)
    for request_id in ("s3-first", "s3-restart"):
        with pytest.raises(DiagnosticDenied):
            case.run(request_id=request_id)
    assert events == ["reserve", "reserve"] and handles == [] and ledger_rows(case.path) == rows
    assert offline_sdk.blocked_calls == []


def execute(tmp_path, request, grant, **changes):
    actual_facts = changes.pop(
        "actual_facts",
        facts(credential_profile_identity=request.capability.profile_reference.value),
    )
    source_resolver = changes.pop(
        "source_resolver",
        lambda: (actual_facts.source_revision, actual_facts.clean_tree),
    )
    source_path = tmp_path / "synthetic-host-provisioned-record.json"
    if grant is not None:
        source_path.write_text(json.dumps(asdict(grant)), encoding="utf-8")
    args = dict(
        request_id="synthetic-request-1", live_opt_in=True,
        conservative_usage_units=sum(len(t.encode("utf-8")) for t in _envelopes(request)[2]),
        conservative_cost_micro_units=100,
    )
    if grant is not None:
        fixed_input, evidence = fixed_offline_evidence(request, grant)
        args.update(approved_input=fixed_input, expected_sdk_evidence=evidence)
    args.update(changes)
    return run_offline_simulation(
        OfflineAuthorizationSource(source_path), actual_facts, request,
        TemporaryDiagnosticUsage(tmp_path / "isolated-usage.sqlite"),
        source_resolver=source_resolver, **args,
    )


@pytest.mark.parametrize("kind", ["document", "query"])
def test_valid_sdk_mocktransport_is_one_attempt_and_usage_unknown(offline_sdk, tmp_path, kind):
    request, grant, identities, texts = request_and_record(tmp_path, kind=kind)
    observed = execute(tmp_path, request, grant)
    assert observed.outcome == "SUCCESS"
    assert observed.provider_attempts == 1 and observed.vector_count == len(identities)
    assert observed.ordered_identities == identities
    assert observed.usage == "UNKNOWN" and observed.billing_status == "ESTIMATE_NOT_GOOGLE_BILLING"
    assert observed.campaign_id == grant.campaign_id
    assert observed.source_revision == grant.source_revision
    assert observed.input_commitment == grant.input_commitment
    assert texts[0].startswith("title: doc-manifest-1 | text: " if kind == "document"
                              else "task: search result | query: ")
    with pytest.raises(DiagnosticDenied):
        execute(tmp_path, request, grant)


@pytest.mark.parametrize("change", [
    {"live_opt_in": False}, {"conservative_usage_units": 4001},
    {"conservative_cost_micro_units": 100001},
])
def test_pre_transport_denial_never_reserves_or_calls_sdk(offline_sdk, tmp_path, change):
    request, grant, _, _ = request_and_record(tmp_path)
    with pytest.raises(DiagnosticDenied):
        execute(tmp_path, request, grant, **change)
    # Same request identity can still reserve: the denied attempt never reached transport.
    assert execute(tmp_path, request, grant).provider_attempts == 1


@pytest.mark.parametrize("grant_change,actual_facts", [
    (None, facts()),
    ("original", facts(clean_tree=False)),
    ("original", facts(source_revision="a" * 40)),
    ("original", facts(credential_profile_identity="wrong-profile")),
])
def test_missing_record_dirty_source_and_profile_denied_before_client(
    offline_sdk, tmp_path, grant_change, actual_facts,
):
    request, grant, _, _ = request_and_record(tmp_path)
    selected = None if grant_change is None else grant
    with pytest.raises(DiagnosticDenied):
        execute(tmp_path, request, selected, actual_facts=actual_facts)
    assert execute(tmp_path, request, grant).provider_attempts == 1


@pytest.mark.parametrize("source_resolver", [
    lambda: ("a" * 40, True),
    lambda: ("b" * 40, False),
    lambda: (None, True),
])
def test_offline_source_resolver_controls_head_and_clean_gate(
    offline_sdk, tmp_path, source_resolver,
):
    request, grant, _, _ = request_and_record(tmp_path)
    with pytest.raises(DiagnosticDenied):
        execute(tmp_path, request, grant, source_resolver=source_resolver)
    assert execute(tmp_path, request, grant).provider_attempts == 1


def test_wrong_order_or_envelope_commitment_denied_before_transport(offline_sdk, tmp_path):
    request, grant, _, _ = request_and_record(tmp_path, count=2)
    wrong = replace(grant, input_commitment="a" * 64)
    with pytest.raises(DiagnosticDenied, match="commitment"):
        execute(tmp_path, request, wrong)
    assert execute(tmp_path, request, grant).provider_attempts == 1


@pytest.mark.parametrize("status,field", [
    (400, "provider_request"), (401, "provider_authorization"),
    (403, "provider_authorization"), (408, "provider_timeout"),
    (429, "provider_rate_limit"), (500, "provider_service"),
    (503, "provider_service"), (504, "provider_timeout"),
    (507, "provider_resource"),
])
def test_document_diagnostic_http_failures_are_typed_without_retry_or_secret(
    offline_sdk, tmp_path, status, field,
):
    request, grant, _, _ = request_and_record(tmp_path)
    result = execute(tmp_path, request, grant, mock_http_status=status)
    assert result.outcome in ("FAILED", "TIMEOUT")
    assert result.failure_field == field and result.failure_code is not None
    assert result.provider_attempts == 1 and result.usage == "UNKNOWN"
    assert result.failure_detail and "synthetic offline failure" not in result.failure_detail
    assert "synthetic-offline-not-a-credential" not in result.failure_detail
    assert "Authorization" not in result.failure_detail


def test_response_cardinality_mismatch_is_bounded_failure(offline_sdk, tmp_path):
    request, grant, _, _ = request_and_record(tmp_path, count=2)
    result = execute(tmp_path, request, grant, mock_response_count=1)
    assert result.outcome == "FAILED" and result.failure_field == "response_cardinality"
    assert result.provider_attempts == 1 and result.vector_count == 0


@pytest.mark.parametrize("mode,field", [
    ("wrong_dimension", "response_dimension"),
    ("unnormalized", "response_normalization"),
    ("reverse", "response_order"),
])
def test_response_shape_normalization_and_synthetic_order_fail_closed(
    offline_sdk, tmp_path, mode, field,
):
    request, grant, _, _ = request_and_record(tmp_path, count=2)
    result = execute(tmp_path, request, grant, mock_response_mode=mode)
    assert result.outcome == "FAILED" and result.failure_field == field
    assert result.provider_attempts == 1 and result.vector_count == 0


@pytest.mark.parametrize("kind,transport_error,field", [
    ("document", "timeout", "transport_timeout"),
    ("document", "connection", "transport"),
    ("query", "timeout", "transport_timeout"),
    ("query", "connection", "transport"),
])
def test_transport_failures_are_single_attempt_and_secret_safe(
    offline_sdk, tmp_path, kind, transport_error, field,
):
    request, grant, _, _ = request_and_record(tmp_path, kind=kind)
    result = execute(tmp_path, request, grant, mock_transport_error=transport_error)
    assert result.failure_field == field and result.provider_attempts == 1
    assert result.outcome in ("UNKNOWN", "TIMEOUT")
    assert "synthetic offline" not in result.failure_detail


def test_live_entry_and_cli_remain_denied_without_credential_discovery(
    offline_sdk, capsys, monkeypatch,
):
    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: (_ for _ in ()).throw(
        DiagnosticDenied("live diagnostic host controls are not provisioned")))
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        run_live(live_opt_in=True)
    with pytest.raises(TypeError):
        run_live(live_opt_in=True, authorization_path="caller-controlled.json")
    assert main() == 2
    output = capsys.readouterr().out
    assert '"provider_attempts": 0' in output and '"outcome": "DENIED"' in output


def test_offline_shared_core_reserves_before_mocktransport(offline_sdk, tmp_path, monkeypatch):
    request, grant, _, _ = request_and_record(tmp_path)
    order = []
    real_preflight = diagnostic._preflight
    real_reserve = diagnostic._reserve
    real_mock = diagnostic._create_diagnostic_offline_sdk_client

    def preflight(**kwargs):
        order.append("preflight")
        return real_preflight(**kwargs)

    def reserve(*args):
        order.append("reserve")
        return real_reserve(*args)

    def mock_client(*args, **kwargs):
        order.append("mock_client")
        return real_mock(*args, **kwargs)

    monkeypatch.setattr(diagnostic, "_preflight", preflight)
    monkeypatch.setattr(diagnostic, "_reserve", reserve)
    monkeypatch.setattr(diagnostic, "_create_diagnostic_offline_sdk_client", mock_client)
    assert execute(tmp_path, request, grant).provider_attempts == 1
    assert order == ["preflight", "preflight", "reserve", "mock_client"]


def test_run_live_reaches_shared_preflight_but_never_reserves_or_dispatches(
    offline_sdk, tmp_path, monkeypatch,
):
    request, grant, _, _ = request_and_record(tmp_path)
    grant = replace(
        grant, valid_from="2000-01-01T00:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    order = []
    real_preflight = diagnostic._preflight

    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: LiveHostBinding(
        grant, approved_input(request, grant), None, grant.runner_identity,
        synthetic_host_evidence(request, grant)))
    monkeypatch.setattr(diagnostic, "resolve_repository_source", lambda _: (grant.source_revision, True))
    monkeypatch.setattr(diagnostic.socket, "gethostname", lambda: grant.authorized_host)
    monkeypatch.setattr(diagnostic, "_live_safety", lambda record, texts: (
        replace(diagnostic._offline_proof(record), evidence_identity="test-only-future-proof"), replace(
            diagnostic._offline_safety_basis(record, texts),
            status="VERIFIED", token_method="MODEL_COMPATIBLE_UPPER_BOUND",
            price_tier="STANDARD_PAID_TEXT",
        ), sum(len(t.encode("utf-8")) for t in texts), 100,
    ))

    def preflight(**kwargs):
        order.append("preflight")
        return real_preflight(**kwargs)

    monkeypatch.setattr(diagnostic, "_preflight", preflight)
    monkeypatch.setattr(diagnostic, "_reserve", lambda *args: pytest.fail("live budget must not be reserved"))
    monkeypatch.setattr(
        diagnostic, "_create_gemini_offline_mock_client",
        lambda *args: pytest.fail("live dispatch must not construct a client"),
    )
    with pytest.raises(DiagnosticDenied, match="dispatch remains unauthorized"):
        run_live(request, live_opt_in=True)
    assert order == ["preflight"]


def test_run_live_rejects_wrong_actual_host_before_safety(offline_sdk, tmp_path, monkeypatch):
    request, grant, _, _ = request_and_record(tmp_path)
    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: LiveHostBinding(
        grant, approved_input(request, grant), None, grant.runner_identity))
    monkeypatch.setattr(diagnostic, "resolve_repository_source", lambda _: (grant.source_revision, True))
    monkeypatch.setattr(diagnostic.socket, "gethostname", lambda: "unapproved-host")
    monkeypatch.setattr(diagnostic, "_live_safety", lambda *_: pytest.fail(
        "wrong host must not reach safety or transport"))
    with pytest.raises(DiagnosticDenied, match="host identity"):
        run_live(request, live_opt_in=True)


@pytest.mark.parametrize("kind", ["document", "query"])
def test_live_artifact_binds_actual_request_before_safety(
    offline_sdk, tmp_path, monkeypatch, kind,
):
    request, grant, _, _ = request_and_record(tmp_path, kind=kind, count=2)
    artifact = approved_input(request, grant)
    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: LiveHostBinding(
        grant, artifact, None, grant.runner_identity))
    monkeypatch.setattr(diagnostic, "_live_safety", lambda *_: pytest.fail(
        "mismatched host input must not reach safety or transport"))
    monkeypatch.setattr(diagnostic, "_reserve", lambda *_: pytest.fail(
        "mismatched host input must not reserve"))
    altered_envelopes = (tuple(reversed(artifact.envelopes)) if kind == "document"
                         else (artifact.envelopes[0] + " changed",))
    altered = replace(artifact, envelopes=altered_envelopes)
    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: LiveHostBinding(
        grant, altered, None, grant.runner_identity))
    with pytest.raises(DiagnosticDenied, match="host-approved"):
        run_live(request, live_opt_in=True)


def _prepared_safety_case(tmp_path, kind="document", count=2):
    request, grant, identities, texts = request_and_record(tmp_path, kind=kind, count=count)
    proof = diagnostic._offline_proof(grant)
    adapter = diagnostic.GoogleEmbeddingAdapter(
        diagnostic._GeminiEmbedding2OfflineClient(), request.capability,
        gemini_verification=proof,
    )
    contents, config = adapter._gemini_prepare(texts, request.limits)
    basis = diagnostic._offline_safety_basis(grant, texts)
    return request, grant, identities, texts, contents, config, proof, basis


def _e4_case(tmp_path, kind="document", count=2):
    request, grant, _, _ = request_and_record(tmp_path, kind=kind, count=count)
    if count == 54:
        grant = replace(grant, maximum_aggregate_tokens=100_000,
                        maximum_payload_bytes=100_000, maximum_usage_units=100_000)
    prepared = diagnostic._preflight(
        load_record=lambda: grant,
        resolve_facts=lambda *_: facts(
            credential_profile_identity=request.capability.profile_reference.value),
        safety=lambda record, texts: (
            diagnostic._offline_proof(record),
            diagnostic._offline_safety_basis(record, texts), sum(len(t.encode("utf-8")) for t in texts), 100,
        ),
        request=request, live_opt_in=True, offline_harness=True,
    )
    return prepared, approved_input(request, grant)


def _host_e4_observation(prepared, approved):
    observed, request = diagnostic.observe_offline_sdk_bytes(prepared, approved)
    host = replace(observed, status="HOST_CONTROLLED_E4_ONLY",
                   reference="host-admin-e4-001",
                   pm_approval_reference=prepared.record.pm_approval_reference,
                   valid_from=datetime(2000, 1, 1, tzinfo=timezone.utc))
    return host, request


@pytest.mark.parametrize("kind,count", [("document", 54), ("query", 1)])
def test_host_e4_record_input_sdk_cross_binding_offline(offline_sdk, tmp_path, kind, count):
    prepared, approved = _e4_case(tmp_path, kind, count)
    host, request = _host_e4_observation(prepared, approved)
    diagnostic.validate_host_sdk_evidence(
        prepared.record, prepared.facts, approved, host, prepared.kind,
        prepared.identities, prepared.texts, prepared.input_scope_identity,
    )
    diagnostic.compare_sdk_request_bytes(prepared, approved, host, request,
                                         offline_harness=True)
    assert len(json.loads(request.content)["requests"]) == count
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        diagnostic.compare_sdk_request_bytes(prepared, approved, host, request)


@pytest.mark.parametrize("kind", ["document", "query"])
def test_host_e4_binding_runs_inside_shared_preflight(offline_sdk, tmp_path, kind):
    prepared, approved = _e4_case(tmp_path, kind)
    host, _ = _host_e4_observation(prepared, approved)
    request, _, _, _ = request_and_record(tmp_path, kind=kind, count=2)

    def preflight(evidence):
        return diagnostic._preflight(
            load_record=lambda: prepared.record,
            resolve_facts=lambda *_: prepared.facts,
            safety=lambda record, texts: (
                diagnostic._offline_proof(record),
                diagnostic._offline_safety_basis(record, texts), sum(len(t.encode("utf-8")) for t in texts), 100,
            ),
            request=request, live_opt_in=True, offline_harness=True,
            host_byte_binding=lambda record, facts, actual_kind, identities,
                                     texts, scope: diagnostic.validate_host_sdk_evidence(
                record, facts, approved, evidence, actual_kind, identities, texts, scope,
            ),
        )

    assert preflight(host).identities == prepared.identities
    with pytest.raises(DiagnosticDenied):
        preflight(replace(host, authorization_record_sha256="0" * 64))


@pytest.mark.parametrize("change", [
    {"campaign_id": "other"}, {"pm_approval_reference": "other"},
    {"source_revision": "a" * 40},
    {"authorization_record_sha256": "0" * 64},
    {"kind": "query"}, {"input_scope_identity": "other"},
    {"input_commitment": "0" * 64}, {"ordered_identities": ("other",)},
    {"provider": "other"}, {"model": "text-embedding-004"},
    {"sdk": "google-genai==0"}, {"api": "v1beta"},
    {"canonical_json_sha256": "0" * 64},
    {"valid_until": datetime(2000, 1, 1, tzinfo=timezone.utc)},
])
def test_host_e4_cross_binding_rejects_stale_or_wrong_identity(
    offline_sdk, tmp_path, change,
):
    prepared, approved = _e4_case(tmp_path)
    host, _ = _host_e4_observation(prepared, approved)
    with pytest.raises(DiagnosticDenied):
        diagnostic.validate_host_sdk_evidence(
            prepared.record, prepared.facts, approved, replace(host, **change),
            prepared.kind, prepared.identities, prepared.texts,
            prepared.input_scope_identity,
        )


def test_host_e4_raw_digest_length_order_and_execution_source_denied(offline_sdk, tmp_path):
    prepared, approved = _e4_case(tmp_path)
    host, request = _host_e4_observation(prepared, approved)
    for changed in (replace(host, sdk_body_sha256="0" * 64),
                    replace(host, sdk_body_bytes=host.sdk_body_bytes + 1)):
        with pytest.raises(DiagnosticDenied, match="actual SDK request bytes"):
            diagnostic.compare_sdk_request_bytes(prepared, approved, changed, request,
                                                 offline_harness=True)
    for actual_facts, identities, texts in (
        (replace(prepared.facts, source_revision="a" * 40),
         prepared.identities, prepared.texts),
        (replace(prepared.facts, clean_tree=False), prepared.identities, prepared.texts),
        (prepared.facts, tuple(reversed(prepared.identities)), prepared.texts),
        (prepared.facts, prepared.identities, (prepared.texts[0] + " changed",)
         + prepared.texts[1:]),
    ):
        with pytest.raises(DiagnosticDenied):
            diagnostic.validate_host_sdk_evidence(
                prepared.record, actual_facts, approved, host, prepared.kind,
                identities, texts, prepared.input_scope_identity,
            )


def test_live_entry_denies_synthetic_e4_and_unknown_e1_e2_e3_before_reservation(
    offline_sdk, tmp_path, monkeypatch,
):
    request, grant, _, _ = request_and_record(tmp_path)
    grant = replace(grant, valid_from="2000-01-01T00:00:00+00:00",
                    expires_at="2099-01-01T00:00:00+00:00")
    host = synthetic_host_evidence(request, grant)
    approved = approved_input(request, grant)
    monkeypatch.setattr(diagnostic, "resolve_repository_source",
                        lambda _: (grant.source_revision, True))
    monkeypatch.setattr(diagnostic.socket, "gethostname",
                        lambda: grant.authorized_host)
    monkeypatch.setattr(diagnostic, "_reserve", lambda *_: pytest.fail(
        "no formal budget reservation is authorized"))
    monkeypatch.setattr(diagnostic, "_create_gemini_offline_mock_client",
                        lambda *_: pytest.fail("no provider client is authorized"))
    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: LiveHostBinding(
        grant, approved, None, grant.runner_identity,
        replace(host, status="SYNTHETIC_OFFLINE_ONLY")))
    with pytest.raises(DiagnosticDenied):
        run_live(request, live_opt_in=True)
    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: LiveHostBinding(
        grant, approved, None, grant.runner_identity, host))
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        run_live(request, live_opt_in=True)


@pytest.mark.parametrize("kind,count", [("document", 54), ("query", 1)])
def test_e4_actual_sdk_bytes_bind_approved_input_and_canonical_digest(
    offline_sdk, tmp_path, kind, count,
):
    prepared, approved = _e4_case(tmp_path, kind, count)
    evidence, request = diagnostic.observe_offline_sdk_bytes(prepared, approved)
    assert evidence.sdk_body_bytes == len(request.content)
    assert evidence.sdk_body_sha256 == sha256(request.content).hexdigest()
    assert evidence.canonical_json_sha256 == sha256(
        diagnostic._canonical_request(request.content)).hexdigest()
    assert evidence.canonical_json_sha256 == prepared.safety_decision.serialized_request_sha256
    assert len(json.loads(request.content)["requests"]) == count
    assert evidence.ordered_identities == prepared.identities
    assert evidence.input_commitment == prepared.record.input_commitment
    assert evidence.source_revision == prepared.record.source_revision
    assert evidence.authorization_record_sha256 == diagnostic._authorization_record_digest(
        prepared.record)
    assert evidence.status == "SYNTHETIC_OFFLINE_ONLY"
    assert evidence.output_dimensionality == 768
    diagnostic.compare_sdk_request_bytes(prepared, approved, evidence, request,
                                         offline_harness=True)
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        diagnostic.compare_sdk_request_bytes(prepared, approved, evidence, request)
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        diagnostic.compare_sdk_request_bytes(prepared, approved,
            replace(evidence, status="VERIFIED"), request)


def test_e4_semantically_equal_but_different_raw_bytes_are_denied(offline_sdk, tmp_path):
    prepared, approved = _e4_case(tmp_path)
    evidence, request = diagnostic.observe_offline_sdk_bytes(prepared, approved)
    changed = json.dumps(json.loads(request.content), sort_keys=True,
                         indent=2, ensure_ascii=True).encode()
    assert changed != request.content
    assert diagnostic._canonical_request(changed) == diagnostic._canonical_request(
        request.content)
    altered = httpx.Request(request.method, request.url, content=changed,
                            extensions=request.extensions)
    with pytest.raises(DiagnosticDenied, match="actual SDK request bytes"):
        diagnostic.compare_sdk_request_bytes(prepared, approved, evidence, altered,
                                             offline_harness=True)


@pytest.mark.parametrize("change", [
    {"sdk_body_sha256": "0" * 64}, {"sdk_body_bytes": 1},
    {"campaign_id": "other"}, {"source_revision": "a" * 40},
    {"authorization_record_sha256": "0" * 64},
    {"input_scope_identity": "other"}, {"input_commitment": "0" * 64},
    {"ordered_identities": ("other",)},
    {"canonical_json_sha256": "0" * 64},
    {"provider": "other"}, {"model": "other"}, {"sdk": "google-genai==0"},
    {"api": "v1beta"}, {"kind": "query"}, {"output_dimensionality": 1536},
    {"timeout_ms": 31000}, {"attempts": 2}, {"http_method": "GET"},
    {"reference": ""}, {"method": "local-json-mirror"},
    {"valid_until": datetime(2000, 1, 1, tzinfo=timezone.utc)},
])
def test_e4_missing_or_mismatched_evidence_denies(offline_sdk, tmp_path, change):
    prepared, approved = _e4_case(tmp_path)
    evidence, request = diagnostic.observe_offline_sdk_bytes(prepared, approved)
    with pytest.raises(DiagnosticDenied, match="missing"):
        diagnostic.compare_sdk_request_bytes(prepared, approved, None, request,
                                             offline_harness=True)
    with pytest.raises(DiagnosticDenied):
        diagnostic.compare_sdk_request_bytes(prepared, approved,
            replace(evidence, **change), request, offline_harness=True)


@pytest.mark.parametrize("kind", ["document", "query"])
def test_e4_mutated_envelope_or_order_denies(offline_sdk, tmp_path, kind):
    prepared, approved = _e4_case(tmp_path, kind, 2)
    evidence, request = diagnostic.observe_offline_sdk_bytes(prepared, approved)
    items = json.loads(request.content)["requests"]
    items[0]["content"]["parts"][0]["text"] += " altered \u2603\\n"
    changed = json.dumps({"requests": items}).encode()
    altered = httpx.Request(request.method, request.url, content=changed,
                            extensions=request.extensions)
    with pytest.raises(DiagnosticDenied):
        diagnostic.compare_sdk_request_bytes(prepared, approved, evidence, altered,
                                             offline_harness=True)
    if kind == "document":
        reordered = list(reversed(json.loads(request.content)["requests"]))
        swapped = httpx.Request(request.method, request.url,
            content=json.dumps({"requests": reordered}).encode(),
            extensions=request.extensions)
        with pytest.raises(DiagnosticDenied):
            diagnostic.compare_sdk_request_bytes(prepared, approved, evidence, swapped,
                                                 offline_harness=True)
    with pytest.raises(DiagnosticDenied):
        diagnostic.compare_sdk_request_bytes(prepared,
            replace(approved, envelopes=tuple(reversed(approved.envelopes))
                    if kind == "document" else ("wrong",)),
            evidence, request, offline_harness=True)


@pytest.mark.parametrize("kind", ["document", "query"])
def test_actual_prepared_request_matches_synthetic_safety_and_sdk_transport(
    offline_sdk, tmp_path, kind,
):
    request, grant, identities, texts, contents, config, proof, basis = (
        _prepared_safety_case(tmp_path, kind)
    )
    decision = diagnostic.evaluate_diagnostic_safety(
        grant, basis, kind, identities, texts, contents, config, proof, 100,
        allow_synthetic=True,
    )
    assert decision.item_count == len(contents) == len(texts)
    assert decision.serialized_request_sha256 == basis.serialized_request_sha256
    assert decision.evidence_status == "SYNTHETIC_OFFLINE_ONLY"
    client, captured = offline_sdk.make_client()
    client.models.embed_content(model=diagnostic.GEMINI_MODEL,
                                contents=list(contents), config=config)
    assert len(captured) == 1
    actual = captured[0].content
    assert diagnostic._canonical_request(actual) == diagnostic._canonical_request(
        diagnostic._serialized(contents, config))
    assert len(actual) <= decision.payload_bytes
    with pytest.raises(DiagnosticDenied, match="not live applicable"):
        diagnostic.evaluate_diagnostic_safety(
            grant, basis, kind, identities, texts, contents, config, proof, 100,
        )
    with pytest.raises(DiagnosticDenied, match="synthetic token counter"):
        diagnostic.evaluate_diagnostic_safety(
            grant, replace(basis, status="VERIFIED",
                           token_method="MODEL_COMPATIBLE_UPPER_BOUND",
                           price_tier="STANDARD_PAID_TEXT"), kind, identities,
            texts, contents, config, proof, 100,
        )
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        diagnostic._live_safety(grant, texts)


@pytest.mark.parametrize("change", [
    {"status": "VERIFIED"},
    {"reference": ""},
    {"campaign_id": "other"},
    {"source_revision": ""},
    {"source_revision": "not-a-commit"},
    {"source_revision": "a" * 40},
    {"input_scope_identity": "other-scope"},
    {"authorization_record_sha256": "a" * 64},
    {"provider": "other"},
    {"model": "text-embedding-004"},
    {"sdk": "google-genai==other"},
    {"api": "v1beta"},
    {"kind": "query"},
    {"input_commitment": "a" * 64},
    {"serialized_request_sha256": "a" * 64},
    {"bounds_basis": "other"},
    {"cost_basis": "other"},
    {"token_method": "legacy-tokenizer"},
    {"provider_item_limit": None},
    {"provider_item_limit": 1},
    {"provider_item_token_limit": None},
    {"provider_item_token_limit": 1},
    {"provider_aggregate_token_limit": None},
    {"provider_aggregate_token_limit": 1},
    {"provider_payload_byte_limit": None},
    {"provider_payload_byte_limit": 1},
    {"price_tier": "FREE"},
    {"price_currency": "EUR"},
    {"price_micro_units_per_million_tokens": None},
    {"price_micro_units_per_million_tokens": 2**63},
    {"valid_until": datetime(2000, 1, 1, tzinfo=timezone.utc)},
])
def test_safety_basis_missing_mismatched_or_exceeded_denies(
    offline_sdk, tmp_path, change,
):
    _, grant, identities, texts, contents, config, proof, basis = (
        _prepared_safety_case(tmp_path)
    )
    with pytest.raises(DiagnosticDenied):
        diagnostic.evaluate_diagnostic_safety(
            grant, replace(basis, **change), "document", identities, texts,
            contents, config, proof, 100, allow_synthetic=True,
        )


def test_conservative_integer_cost_rounding_ceiling_and_overflow(offline_sdk, tmp_path):
    _, grant, identities, texts, contents, config, proof, basis = (
        _prepared_safety_case(tmp_path)
    )
    tokens = sum(len(text.encode("utf-8")) for text in texts)
    rate = 200_000
    expected = (tokens * rate + 999_999) // 1_000_000
    priced = replace(basis, price_micro_units_per_million_tokens=rate)
    assert diagnostic.evaluate_diagnostic_safety(
        grant, priced, "document", identities, texts, contents, config, proof,
        expected, allow_synthetic=True,
    ).minimum_cost_micro_units == expected
    with pytest.raises(DiagnosticDenied, match="cost"):
        diagnostic.evaluate_diagnostic_safety(
            grant, priced, "document", identities, texts, contents, config, proof,
            expected - 1, allow_synthetic=True,
        )
    with pytest.raises(DiagnosticDenied, match="cost"):
        diagnostic.evaluate_diagnostic_safety(
            grant, replace(basis, price_micro_units_per_million_tokens=2**63-1),
            "document", identities, texts, contents, config, proof,
            grant.maximum_cost_micro_units, allow_synthetic=True,
        )


def test_safety_rejects_changed_content_invalid_counter_and_currency(
    offline_sdk, tmp_path,
):
    _, grant, identities, texts, contents, config, proof, basis = (
        _prepared_safety_case(tmp_path)
    )
    changed = list(contents)
    changed[0] = offline_sdk.types.Content(
        role="user", parts=[offline_sdk.types.Part(text=texts[0] + " 漢\n\\\"")],
    )
    with pytest.raises(DiagnosticDenied, match="serialized"):
        diagnostic.evaluate_diagnostic_safety(
            grant, basis, "document", identities, texts, tuple(changed), config,
            proof, 100, allow_synthetic=True,
        )
    with pytest.raises(DiagnosticDenied, match="token"):
        diagnostic.evaluate_diagnostic_safety(
            grant, basis, "document", identities, texts, contents, config,
            replace(proof, count_tokens=lambda _: True), 100, allow_synthetic=True,
        )
    euro_record = replace(grant, cost_currency="EUR")
    with pytest.raises(DiagnosticDenied, match="currency"):
        diagnostic.evaluate_diagnostic_safety(
            euro_record,
            diagnostic._offline_safety_basis(euro_record, texts),
            "document", identities, texts,
            contents, config, proof, 100, allow_synthetic=True,
        )


def test_54_independent_prepared_contents_have_ordered_safety_commitment(
    offline_sdk, tmp_path,
):
    request, grant, identities, texts = request_and_record(tmp_path, count=54)
    grant = replace(grant, maximum_aggregate_tokens=100_000,
                    maximum_payload_bytes=1_000_000, maximum_usage_units=100_000)
    proof = diagnostic._offline_proof(grant)
    adapter = diagnostic.GoogleEmbeddingAdapter(
        diagnostic._GeminiEmbedding2OfflineClient(), request.capability,
        gemini_verification=proof,
    )
    contents, config = adapter._gemini_prepare(texts, request.limits)
    basis = diagnostic._offline_safety_basis(grant, texts)
    decision = diagnostic.evaluate_diagnostic_safety(
        grant, basis, "document", identities, texts, contents, config, proof,
        100, allow_synthetic=True,
    )
    assert decision.item_count == 54 and len(decision.item_tokens) == 54
    assert len({id(item) for item in contents}) == 54
    with pytest.raises(DiagnosticDenied, match="commitment"):
        diagnostic.evaluate_diagnostic_safety(
            grant, basis, "document", tuple(reversed(identities)), texts,
            contents, config, proof, 100, allow_synthetic=True,
        )


def test_full_preflight_rejects_stale_safety_basis_with_matching_campaign(
    offline_sdk, tmp_path,
):
    request, current, _, texts = request_and_record(tmp_path)
    stale = replace(current, source_revision="a" * 40)
    stale_basis = diagnostic._offline_safety_basis(stale, texts)
    assert stale.campaign_id == current.campaign_id
    assert stale.input_commitment == current.input_commitment
    actual = facts(credential_profile_identity=request.capability.profile_reference.value)
    assert actual.source_revision == current.source_revision
    stale_actual = replace(actual, source_revision=stale.source_revision)
    accepted = diagnostic._preflight(
        load_record=lambda: stale,
        resolve_facts=lambda _record, _request: stale_actual,
        safety=lambda record, _texts: (
            diagnostic._offline_proof(record), stale_basis, sum(len(t.encode("utf-8")) for t in texts), 100,
        ),
        request=request, live_opt_in=True, offline_harness=True,
    )
    assert accepted.safety_decision.evidence_status == "SYNTHETIC_OFFLINE_ONLY"
    assert accepted.facts.source_revision == stale_basis.source_revision
    with pytest.raises(DiagnosticDenied, match="safety evidence"):
        diagnostic._preflight(
            load_record=lambda: current,
            resolve_facts=lambda _record, _request: actual,
            safety=lambda record, _texts: (
                diagnostic._offline_proof(record), stale_basis, sum(len(t.encode("utf-8")) for t in texts), 100,
            ),
            request=request, live_opt_in=True, offline_harness=True,
        )
    current_basis = diagnostic._offline_safety_basis(current, texts)
    changed_record = replace(current, maximum_cost_micro_units=current.maximum_cost_micro_units + 1)
    with pytest.raises(DiagnosticDenied, match="safety evidence"):
        diagnostic._preflight(
            load_record=lambda: changed_record,
            resolve_facts=lambda _record, _request: actual,
            safety=lambda record, _texts: (
                diagnostic._offline_proof(record), current_basis, sum(len(t.encode("utf-8")) for t in texts), 100,
            ),
            request=request, live_opt_in=True, offline_harness=True,
        )
    with pytest.raises(DiagnosticDenied, match="safety evidence"):
        diagnostic._preflight(
            load_record=lambda: current,
            resolve_facts=lambda _record, _request: actual,
            safety=lambda record, _texts: (
                diagnostic._offline_proof(record),
                replace(diagnostic._offline_safety_basis(record, texts),
                        input_scope_identity="other-scope"), sum(len(t.encode("utf-8")) for t in texts), 100,
            ),
            request=request, live_opt_in=True, offline_harness=True,
        )
