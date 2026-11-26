"""Credential-free contract tests for the approved Gemini adapter."""

from dataclasses import replace
import json
import os
import sys
import socket
from types import SimpleNamespace

import pytest

from llm_generation.contracts import FailureClass
from llm_generation.google_generation_adapter import (
    GoogleGenerationAdapter, create_google_generation_client,
    GEMINI35_MODEL, GEMINI35_PROFILE,
)
from llm_generation.service import (
    ProviderCapability, ProviderFailureKind, ProviderInvocationError,
    ProviderRequest,
)
from rca_persistence.contracts import AdmittedRetryDisposition
from test_llm_generation_phase2 import case, _structured
from test_llm_generation_service import environment


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    # Never discover/read a host credential, even in the SDK factory tests.
    monkeypatch.setattr(os, "environ", {})

    def deny(*args, **kwargs):
        raise AssertionError("offline adapter tests cannot use a socket")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


class SDKError(Exception):
    def __init__(self, code, status, secret="Authorization: Bearer secret-value"):
        super().__init__(secret)
        self.code = code
        self.status = status


class MockModels:
    def __init__(self, output="{}", count=10, error=None, count_error=None):
        self.output = output
        self.count = count
        self.error = error
        self.count_error = count_error
        self.count_calls = []
        self.generate_calls = []

    def count_tokens(self, **kwargs):
        self.count_calls.append(kwargs)
        if self.count_error:
            raise self.count_error
        return SimpleNamespace(total_tokens=self.count)

    def generate_content(self, **kwargs):
        self.generate_calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            text=self.output,
            usage_metadata=SimpleNamespace(
                prompt_token_count=10, candidates_token_count=5,
                total_token_count=15,
            ),
        )


def adapter(config, models):
    pin = config.pin
    capability = ProviderCapability(
        pin.provider, pin.model, pin.profile.identity,
        pin.result_schema.identity, pin.result_schema.version, True,
    )
    return GoogleGenerationAdapter(capability, client=SimpleNamespace(models=models))


def test_success_count_then_one_generation_and_durable_replay(case, tmp_path):
    service, _, grant, resources = environment(case, tmp_path)
    models = MockModels(json.dumps(_structured(case.content, case.config)))
    service.provider = adapter(case.config, models)
    first = service.execute(case.content.input, case.config, grant, resources)
    assert first.result is not None
    assert first.token_preflight_calls == service.provider.token_preflight_calls == 1
    assert first.physical_invocations == service.provider.generation_calls == 1
    assert models.count_calls == [{"model": "gemini-2.5-flash", "contents": models.generate_calls[0]["contents"]}]
    assert models.generate_calls[0]["model"] == "gemini-2.5-flash"
    assert models.generate_calls[0]["config"]["max_output_tokens"] == case.config.bounds.maximum_output_tokens
    replay = service.execute(case.content.input, case.config, grant, resources)
    assert replay.result == first.result
    assert replay.token_preflight_calls == replay.physical_invocations == 0
    assert service.provider.token_preflight_calls == service.provider.generation_calls == 1


def test_count_limit_and_admission_rejection_never_generate(case, tmp_path):
    service, _, grant, resources = environment(case, tmp_path)
    models = MockModels(count=case.config.bounds.maximum_input_tokens + 1)
    service.provider = adapter(case.config, models)
    rejected = service.execute(case.content.input, case.config, grant, resources)
    assert rejected.failure.failure_class is FailureClass.INVOCATION_BOUND
    assert rejected.token_preflight_calls == 1 and rejected.physical_invocations == 0
    assert len(models.count_calls) == 1 and models.generate_calls == []

    second, _, second_grant, second_resources = environment(case, tmp_path / "admission")
    other = MockModels()
    second.provider = adapter(case.config, other)
    source = replace(case.content.input, evidence_projection="untrusted")
    receipt = second.execute(source, case.config, second_grant, second_resources)
    assert receipt.failure.failure_class is FailureClass.INVALID_INPUT
    assert receipt.token_preflight_calls == receipt.physical_invocations == 0
    assert other.count_calls == other.generate_calls == []


@pytest.mark.parametrize("code,status,expected,safety", [
    (408, "DEADLINE_EXCEEDED", FailureClass.PROVIDER_TIMEOUT, AdmittedRetryDisposition.RETRYABLE),
    (503, "UNAVAILABLE", FailureClass.PROVIDER_UNAVAILABLE, AdmittedRetryDisposition.RETRYABLE),
    (429, "RESOURCE_EXHAUSTED", FailureClass.PROVIDER_QUOTA, AdmittedRetryDisposition.RETRYABLE),
    (429, "RATE_LIMIT_EXCEEDED", FailureClass.PROVIDER_QUOTA, AdmittedRetryDisposition.RETRYABLE),
    (507, "RESOURCE_EXHAUSTED", FailureClass.PROVIDER_QUOTA, AdmittedRetryDisposition.RETRYABLE),
    (404, "NOT_FOUND", FailureClass.CAPABILITY_MISMATCH, AdmittedRetryDisposition.NON_RETRYABLE),
])
@pytest.mark.parametrize("stage", ("count", "generation"))
def test_typed_sanitized_sdk_failures(case, tmp_path, code, status, expected, safety, stage):
    service, _, grant, resources = environment(case, tmp_path)
    error = SDKError(code, status)
    models = MockModels(count_error=error if stage == "count" else None,
                        error=error if stage == "generation" else None)
    service.provider = adapter(case.config, models)
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure.failure_class is expected
    assert receipt.failure.retry_safety is safety
    assert receipt.token_preflight_calls == 1
    assert receipt.physical_invocations == (stage == "generation")
    assert len(models.count_calls) == 1
    assert len(models.generate_calls) == (stage == "generation")
    assert "secret-value" not in receipt.failure.safe_detail
    assert b"secret-value" not in service.store.path.read_bytes()


def test_exact_capability_and_timeout_without_fallback(case):
    models = MockModels()
    provider = adapter(case.config, models)
    request = ProviderRequest(
        "google", "gemini-2.5-flash", case.config.pin.profile.identity,
        "prompt", case.config.pin.result_schema.identity,
        case.config.pin.result_schema.version,
        case.config.bounds.timeout_seconds, 1024, 128,
    )
    assert provider.input_token_upper_bound("prompt", request.timeout_seconds) == 10
    with pytest.raises(ProviderInvocationError) as mismatch:
        provider.invoke_once(replace(request, model="fallback-model"))
    assert mismatch.value.kind is ProviderFailureKind.CAPABILITY
    with pytest.raises(ProviderInvocationError):
        provider.invoke_once(replace(request, timeout_seconds=request.timeout_seconds + 1))
    assert provider.generation_calls == 0 and models.generate_calls == []


def test_missing_credential_fails_closed_without_sdk_import(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderInvocationError) as missing:
        create_google_generation_client(10)
    assert missing.value.kind is ProviderFailureKind.UNAVAILABLE
    assert "key" not in str(missing.value).lower()


def test_service_missing_credential_sends_neither_request(case, tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    service, _, grant, resources = environment(case, tmp_path)
    service.provider = GoogleGenerationAdapter(adapter(case.config, MockModels()).capability)
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert receipt.token_preflight_calls == receipt.physical_invocations == 0
    assert service.provider.token_preflight_calls == service.provider.generation_calls == 0


@pytest.mark.parametrize("ambient_mode", ("record", "replay", "auto"))
def test_sdk_client_uses_one_credential_and_disables_ambient_replay(monkeypatch, ambient_mode):
    captured = {}

    class DebugConfig:
        def __init__(self, *, client_mode, replays_directory, replay_id):
            self.client_mode = client_mode
            self.replays_directory = replays_directory
            self.replay_id = replay_id

    class HttpRetryOptions:
        def __init__(self, *, attempts):
            captured["attempts"] = attempts

    class HttpOptions:
        def __init__(self, *, timeout, retry_options):
            captured["timeout"] = timeout
            captured["retry_options"] = retry_options

    def client(*, api_key, http_options, debug_config):
        captured["selected_source"] = "GEMINI_API_KEY"
        captured["http_options"] = http_options
        captured["debug_config"] = debug_config
        assert api_key == "test-only-secret"
        return SimpleNamespace(models=MockModels())

    monkeypatch.setenv("GEMINI_API_KEY", "test-only-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "unselected-test-secret")
    monkeypatch.setenv("GOOGLE_GENAI_CLIENT_MODE", ambient_mode)
    monkeypatch.setenv("GOOGLE_GENAI_REPLAYS_DIRECTORY", "unsafe-ambient-replay-path")
    monkeypatch.setenv("GOOGLE_GENAI_REPLAY_ID", "unsafe-ambient-replay-id")
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(genai=SimpleNamespace(Client=client)))
    monkeypatch.setitem(sys.modules, "google.genai", SimpleNamespace(
        client=SimpleNamespace(DebugConfig=DebugConfig),
        types=SimpleNamespace(HttpRetryOptions=HttpRetryOptions, HttpOptions=HttpOptions),
    ))
    assert create_google_generation_client(12).models is not None
    assert captured["attempts"] == 1
    assert captured["timeout"] == 12_000
    assert captured["selected_source"] == "GEMINI_API_KEY"
    assert captured["debug_config"].client_mode is None
    assert captured["debug_config"].replays_directory is None
    assert captured["debug_config"].replay_id is None
    assert os.environ["GOOGLE_GENAI_CLIENT_MODE"] == ambient_mode
    assert os.environ["GOOGLE_GENAI_REPLAYS_DIRECTORY"] == "unsafe-ambient-replay-path"
    assert os.environ["GOOGLE_GENAI_REPLAY_ID"] == "unsafe-ambient-replay-id"


def test_output_bound_and_malformed_usage_are_typed(case):
    provider = adapter(case.config, MockModels(output="x" * 101))
    request = ProviderRequest(
        "google", "gemini-2.5-flash", case.config.pin.profile.identity,
        "prompt", case.config.pin.result_schema.identity,
        case.config.pin.result_schema.version,
        case.config.bounds.timeout_seconds, 100, 128,
    )
    provider.input_token_upper_bound("prompt", request.timeout_seconds)
    with pytest.raises(ProviderInvocationError) as bound:
        provider.invoke_once(request)
    assert bound.value.kind is ProviderFailureKind.BOUNDS
    assert provider.token_preflight_calls == provider.generation_calls == 1


class Mock35Models(MockModels):
    def __init__(self, *args, model=GEMINI35_MODEL, finish="STOP", usage=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.response_model = model
        self.finish = finish
        self.usage = usage

    def generate_content(self, **kwargs):
        result = super().generate_content(**kwargs)
        result.model_version = self.response_model
        result.candidates = [SimpleNamespace(finish_reason=self.finish)]
        if self.usage is not None:
            result.usage_metadata = SimpleNamespace(**self.usage)
        return result


def capability35():
    return ProviderCapability("google", GEMINI35_MODEL, GEMINI35_PROFILE,
                              "spec015-rca-result-v1", "1", True)


def provider35(models=None):
    return GoogleGenerationAdapter(capability35(), profile_version="1",
                                   client=SimpleNamespace(models=models or Mock35Models()))


def request35(**changes):
    return replace(ProviderRequest("google", GEMINI35_MODEL, GEMINI35_PROFILE,
        "bounded observed facts", "spec015-rca-result-v1", "1", 30, 4096, 128), **changes)


@pytest.mark.parametrize("changes", [
    {"model": "gemini-3.5-flash-lite-preview"}, {"model": "gemini-3.5-flash"},
    {"profile_id": "unknown-profile"}, {"profile_id": GEMINI35_PROFILE + "-extra"},
    {"schema_id": "unknown-schema"}, {"schema_version": "2"},
    {"hidden_retries_disabled": False}, {"provider": "unknown"},
])
def test_gemini35_exact_admission(changes):
    with pytest.raises(ValueError):
        GoogleGenerationAdapter(replace(capability35(), **changes), profile_version="1")


@pytest.mark.parametrize("version", [None, "2", 1, True])
def test_gemini35_requires_exact_profile_version(version):
    with pytest.raises(ValueError):
        GoogleGenerationAdapter(capability35(), profile_version=version)


def test_gemini35_profile_cannot_select_legacy_or_unpinned_sdk(monkeypatch):
    with pytest.raises(ValueError):
        GoogleGenerationAdapter(replace(capability35(), model="gemini-2.5-flash"))
    import llm_generation.google_generation_adapter as module
    monkeypatch.setattr(module, "version", lambda name: "2.26.0")
    with pytest.raises(ValueError):
        provider35()


def test_gemini35_exact_model_and_explicit_wire_configuration():
    models = Mock35Models()
    provider = provider35(models)
    assert provider.input_token_upper_bound("bounded observed facts", 30) == 10
    result = provider.invoke_once(request35())
    assert result.metadata.total_tokens == 15
    assert models.count_calls == [{"model": GEMINI35_MODEL, "contents": "bounded observed facts"}]
    assert len(models.generate_calls) == 1
    call = models.generate_calls[0]
    assert call["model"] == GEMINI35_MODEL
    assert call["config"] == {
        "max_output_tokens": 128, "response_mime_type": "application/json",
        "thinking_config": {"thinking_level": "MINIMAL"},
        "automatic_function_calling": {"disable": True}, "tools": [],
    }
    assert provider.token_preflight_calls == provider.generation_calls == 1


@pytest.mark.parametrize("model", [None, "", 35, "gemini-2.5-flash",
    GEMINI35_MODEL + "-001", "models/" + GEMINI35_MODEL])
def test_gemini35_unknown_response_routing_fails_closed(model):
    models = Mock35Models(model=model)
    provider = provider35(models)
    with pytest.raises(ProviderInvocationError) as rejected:
        provider.invoke_once(request35())
    assert rejected.value.kind is ProviderFailureKind.CAPABILITY
    assert rejected.value.request_sent and len(models.generate_calls) == 1


@pytest.mark.parametrize("model,allowed", [
    (None, True), (GEMINI35_MODEL, True), ("gemini-2.5-flash", False),
    ("", False), (123, False),
])
def test_gemini35_count_response_identity_is_optional_but_conflicts_reject(model, allowed):
    models = Mock35Models()
    original = models.count_tokens
    def count_tokens(**kwargs):
        response = original(**kwargs)
        if model is not None:
            response.model_version = model
        return response
    models.count_tokens = count_tokens
    provider = provider35(models)
    if allowed:
        assert provider.input_token_upper_bound("synthetic public facts", 30) > 0
    else:
        with pytest.raises(ProviderInvocationError) as failure:
            provider.input_token_upper_bound("synthetic public facts", 30)
        assert failure.value.kind is ProviderFailureKind.CAPABILITY
        assert failure.value.request_sent
    assert len(models.count_calls) == provider.token_preflight_calls == 1
    assert models.count_calls[0]["model"] == GEMINI35_MODEL
    assert provider.generation_calls == 0 and models.generate_calls == []


@pytest.mark.parametrize("finish", [None, "MAX_TOKENS", "SAFETY", "OTHER"])
def test_gemini35_incomplete_output_rejected(finish):
    with pytest.raises(ProviderInvocationError) as rejected:
        provider35(Mock35Models(finish=finish)).invoke_once(request35())
    assert rejected.value.kind is ProviderFailureKind.MALFORMED


@pytest.mark.parametrize("usage", [
    {"prompt_token_count": None, "candidates_token_count": 5, "total_token_count": 15},
    {"prompt_token_count": 10, "candidates_token_count": True, "total_token_count": 15},
    {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": None},
    {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 14},
    {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 15, "thoughts_token_count": 2},
    {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 15, "thoughts_token_count": -1},
    {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 16, "tool_use_prompt_token_count": 1},
])
def test_gemini35_unknown_or_inconsistent_usage_rejected(usage):
    with pytest.raises(ProviderInvocationError) as rejected:
        provider35(Mock35Models(usage=usage)).invoke_once(request35())
    assert rejected.value.kind is ProviderFailureKind.MALFORMED


@pytest.mark.parametrize("thoughts", [None, 2])
def test_gemini35_all_post_input_tokens_are_bounded(thoughts):
    usage = {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 17}
    if thoughts is not None:
        usage["thoughts_token_count"] = thoughts
    result = provider35(Mock35Models(usage=usage)).invoke_once(request35())
    assert result.metadata.output_tokens == 7
    with pytest.raises(ProviderInvocationError) as rejected:
        provider35(Mock35Models(usage=usage)).invoke_once(request35(maximum_output_tokens=6))
    assert rejected.value.kind is ProviderFailureKind.BOUNDS


@pytest.mark.parametrize("stage", ["count", "generation"])
@pytest.mark.parametrize("error,kind", [
    (TimeoutError(), ProviderFailureKind.TIMEOUT),
    (SDKError(503, "UNAVAILABLE"), ProviderFailureKind.UNAVAILABLE),
    (SDKError(429, "RESOURCE_EXHAUSTED"), ProviderFailureKind.QUOTA),
])
def test_gemini35_provider_errors_never_retry_or_fallback(stage, error, kind):
    models = Mock35Models(count_error=error if stage == "count" else None,
                          error=error if stage == "generation" else None)
    provider = provider35(models)
    with pytest.raises(ProviderInvocationError) as rejected:
        if stage == "count":
            provider.input_token_upper_bound("bounded observed facts", 30)
        else:
            provider.invoke_once(request35())
    assert rejected.value.kind is kind and rejected.value.request_sent
    assert len(models.count_calls) == (stage == "count")
    assert len(models.generate_calls) == (stage == "generation")


def test_gemini35_wrong_timeout_and_request_pin_are_rejected_before_call():
    models = Mock35Models()
    provider = provider35(models)
    with pytest.raises(ProviderInvocationError):
        provider.input_token_upper_bound("bounded observed facts", 31)
    with pytest.raises(ProviderInvocationError):
        provider.invoke_once(request35(profile_id="other-profile"))
    assert models.count_calls == models.generate_calls == []


@pytest.mark.parametrize("failure_stage", [None, "countTokens", "generateContent"])
def test_gemini35_real_pinned_sdk_mock_transport(monkeypatch, failure_stage):
    import httpx
    from google import genai
    real_client = genai.Client
    requests = []
    created = []

    def transport(request):
        requests.append((request.url.path, json.loads(request.content)))
        if failure_stage and request.url.path.endswith(":" + failure_stage):
            return httpx.Response(503, json={"error": {
                "code": 503, "message": "synthetic unavailable", "status": "UNAVAILABLE"}})
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 10})
        assert request.url.path.endswith(":generateContent")
        return httpx.Response(200, json={
            "modelVersion": GEMINI35_MODEL,
            "candidates": [{"content": {"role": "model", "parts": [{"text": "{}"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5,
                              "thoughtsTokenCount": 2, "totalTokenCount": 17},
        })

    def factory(*, api_key, http_options, debug_config):
        assert api_key == "synthetic-offline-only"
        assert http_options.api_version == "v1beta"
        assert http_options.timeout == 30_000 and http_options.retry_options.attempts == 1
        assert debug_config.client_mode is None
        http_options.client_args = {**(http_options.client_args or {}), "transport": httpx.MockTransport(transport), "trust_env": False}
        http_options.async_client_args = {"transport": httpx.MockTransport(transport), "trust_env": False}
        client = real_client(api_key=api_key, http_options=http_options, debug_config=debug_config)
        created.append(client)
        return client

    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-offline-only")
    monkeypatch.setattr(genai, "Client", factory)
    provider = GoogleGenerationAdapter(capability35(), profile_version="1")
    try:
        if failure_stage:
            with pytest.raises(ProviderInvocationError) as failure:
                provider.input_token_upper_bound("bounded observed facts", 30)
                provider.invoke_once(request35())
            assert failure.value.kind is ProviderFailureKind.UNAVAILABLE
            assert len(requests) == (1 if failure_stage == "countTokens" else 2)
            assert sum(path.endswith(":" + failure_stage) for path, _ in requests) == 1
            return
        assert provider.input_token_upper_bound("bounded observed facts", 30) == 10
        assert provider.invoke_once(request35()).metadata.output_tokens == 7
        assert len(requests) == 2
        assert all("/models/" + GEMINI35_MODEL + ":" in path for path, _ in requests)
        cfg = requests[1][1]["generationConfig"]
        # The pinned SDK forwards ThinkingConfig's protobuf field spelling.
        # Assert the actual wire payload, rather than assuming camelCase.
        assert cfg["thinkingConfig"] == {"thinking_level": "MINIMAL"}
        assert cfg["responseMimeType"] == "application/json"
        assert not {"candidateCount", "temperature", "topP", "topK"} & cfg.keys()
        assert provider.token_preflight_calls == provider.generation_calls == 1
    finally:
        for client in created:
            client.close()


@pytest.mark.parametrize("body,expected", [
    ('{"totalTokens":10}', None),
    ('{"totalTokens":10,"modelVersion":"gemini-3.5-flash-lite"}', None),
    ('{"totalTokens":10,"modelVersion":"EXPLICIT-CONFLICT"}', ProviderFailureKind.CAPABILITY),
    ('{"totalTokens":10,"model_version":"gemini-3.5-flash-lite"}', None),
    ('{"totalTokens":10,"model_version":"EXPLICIT-CONFLICT"}', ProviderFailureKind.CAPABILITY),
    ('{"totalTokens":10,"modelVersion":null}', None),
    ('{"totalTokens":10,"modelVersion":7}', ProviderFailureKind.CAPABILITY),
    ('{"totalTokens":10,"modelVersion":"gemini-3.5-flash-lite","model_version":"OTHER"}', ProviderFailureKind.CAPABILITY),
    ('{"totalTokens":10,"modelVersion":"OTHER","modelVersion":"gemini-3.5-flash-lite"}', ProviderFailureKind.MALFORMED),
    ('not JSON', ProviderFailureKind.MALFORMED),
])
def test_gemini35_factory_observes_count_identity_before_real_sdk_conversion(monkeypatch, capsys, body, expected):
    import httpx
    from google import genai
    real_client = genai.Client
    calls, clients = [], []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, content=body.encode(), headers={"content-type":"application/json"})
    def factory(*, api_key, http_options, debug_config):
        assert http_options.api_version == "v1beta" and http_options.timeout == 30000
        assert http_options.retry_options.attempts == 1
        assert len(http_options.client_args["event_hooks"]["response"]) == 1
        http_options.client_args.update(transport=httpx.MockTransport(transport), trust_env=False)
        client = real_client(api_key=api_key,http_options=http_options,debug_config=debug_config)
        clients.append(client)
        return client
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-offline-only")
    monkeypatch.setattr(genai,"Client",factory)
    provider = GoogleGenerationAdapter(capability35(),profile_version="1")
    try:
        if expected is None:
            assert provider.input_token_upper_bound("synthetic approved facts",30) == 10
        else:
            with pytest.raises(ProviderInvocationError) as rejected:
                provider.input_token_upper_bound("synthetic approved facts",30)
            assert rejected.value.kind is expected and rejected.value.request_sent
        assert len(calls) == provider.token_preflight_calls == 1 and provider.generation_calls == 0
        assert calls[0].method == "POST"
        assert calls[0].url.path == "/v1beta/models/gemini-3.5-flash-lite:countTokens"
        assert set(calls[0].extensions["timeout"].values()) == {30.0}
        assert json.loads(calls[0].content) == {"contents":[{"parts":[{"text":"synthetic approved facts"}],"role":"user"}]}
        assert capsys.readouterr() == ("", "")
    finally:
        for client in clients: client.close()


def test_gemini35_generation_service_preserves_grounding_and_replay(case, tmp_path):
    from dataclasses import asdict
    from llm_generation.config import load_generation_config, parse_generation_config, InvocationBounds
    from llm_generation.contracts import VersionedIdentity, ValidationFact
    from llm_generation.identity import semantic_commitment
    from rca_persistence.contracts import GenerationProvenance
    base = load_generation_config("configs/rca_generation.json")
    raw = {
        "version": base.version, "provider": "google", "model": GEMINI35_MODEL,
        "profile": {"identity": GEMINI35_PROFILE, "version": "1"},
        "prompt": asdict(base.pin.prompt), "result_schema": asdict(base.pin.result_schema),
        "configuration": {"identity": "", "version": "1"}, "bounds": asdict(base.bounds),
        "hidden_retries_disabled": True, "prompt_template": base.prompt_template,
        "result_schema_commitment": base.result_schema_commitment,
    }
    raw["configuration_commitment"] = semantic_commitment((
        raw["version"], raw["provider"], raw["model"], VersionedIdentity(**raw["profile"]),
        base.pin.prompt, base.pin.result_schema, "1", InvocationBounds(**raw["bounds"]),
        True, raw["prompt_template"], raw["result_schema_commitment"],
    ))
    raw["configuration"]["identity"] = "cfg_" + raw["configuration_commitment"]
    case.config = parse_generation_config(raw)
    case.content = replace(case.content, input=replace(case.content.input, pin=case.config.pin),
        validation_facts=(ValidationFact("schema", "1", base.result_schema_commitment),))
    previous = case.attempts.get_attempt_lineage("attempt-1")
    lineage = replace(previous.attempt.lineage, generation_provenance=GenerationProvenance(
        "google", GEMINI35_MODEL, base.pin.prompt.identity,
        case.config.pin.configuration.identity, GEMINI35_PROFILE))
    case.attempts = SimpleNamespace(get_attempt_lineage=lambda _: replace(previous,
        attempt=replace(previous.attempt, lineage=lineage)))
    service, _, grant, resources = environment(case, tmp_path)
    models = Mock35Models(json.dumps(_structured(case.content, case.config)))
    service.provider = provider35(models)
    first = service.execute(case.content.input, case.config, grant, resources)
    assert first.result is not None
    replay = service.execute(case.content.input, case.config, grant, resources)
    assert replay.result == first.result and replay.physical_invocations == 0
    assert len(models.count_calls) == len(models.generate_calls) == 1
    bad = _structured(case.content, case.config)
    bad["summary"] = "A statement absent from claims"
    other, _, grant2, resources2 = environment(case, tmp_path / "ungrounded")
    other.provider = provider35(Mock35Models(json.dumps(bad)))
    rejected = other.execute(case.content.input, case.config, grant2, resources2)
    assert rejected.failure.failure_class is FailureClass.GROUNDING
