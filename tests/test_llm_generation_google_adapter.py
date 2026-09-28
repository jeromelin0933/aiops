"""Credential-free contract tests for the approved Gemini adapter."""

from dataclasses import replace
import json
import os
import sys
from types import SimpleNamespace

import pytest

from llm_generation.contracts import FailureClass
from llm_generation.google_generation_adapter import (
    GoogleGenerationAdapter, create_google_generation_client,
)
from llm_generation.service import (
    ProviderCapability, ProviderFailureKind, ProviderInvocationError,
    ProviderRequest,
)
from rca_persistence.contracts import AdmittedRetryDisposition
from test_llm_generation_phase2 import case, _structured
from test_llm_generation_service import environment


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
