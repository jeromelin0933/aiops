"""Historical Google adapter and gated gemini-embedding-2 compatibility path."""

from __future__ import annotations

import math
import json
import os
from dataclasses import dataclass, replace
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from types import MappingProxyType, SimpleNamespace
from threading import Lock
from typing import Any, Callable

import httpx

from .config import GEMINI_EMBEDDING2_PROFILE
from .gemini_diagnostic_controls import DiagnosticDenied
from .identity import document_identity

from .contracts import (
    BuildFailureCode,
    BuildFailureFact,
    EmbeddingVector,
    KnowledgeValidationError,
    ProviderCapability,
    ProviderEmbeddingRequest,
    ProviderEmbeddingResult,
    QueryEmbeddingRequest,
    QueryEmbeddingResult,
    RetrySafetyDisposition,
)


APPROVED_PROVIDER = "google"
APPROVED_MODEL = "text-embedding-004"
APPROVED_DIMENSION = 768
GEMINI_MODEL = GEMINI_EMBEDDING2_PROFILE.model


def _embedding_prompt_tokens(body: bytes) -> int | None:
    """Allowlisted scalar from the existing guarded response, before SDK loss.

    This parser alone establishes NO provider/attempt authority. Missing,
    duplicate, malformed and implausible usage remains UNKNOWN. Every approved
    envelope is nonempty, so an explicit zero is not adopted as actual usage.
    Never retain/return the raw response, other metadata, or header guesses.
    """
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate response field")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("invalid JSON constant")
    try:
        if type(body) is not bytes or not 0 < len(body) <= 1_000_000:
            return None
        value = json.loads(body.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
        metadata = value.get("usageMetadata") if type(value) is dict else None
        count = metadata.get("promptTokenCount") if type(metadata) is dict else None
        return count if type(count) is int and 0 < count <= 2**63 - 1 else None
    except (ValueError, UnicodeError, TypeError, RecursionError, OverflowError):
        return None


@dataclass(frozen=True, slots=True)
class GeminiEmbedding2Verification:
    """Offline technical inputs only; never authorizes a provider invocation.

    Synthetic counters, serializers, bounds, and tolerances are suitable for
    isolated contract tests, not for live transport or provider PASS claims.
    """

    count_tokens: Callable[[str], int]
    serialize_payload: Callable[[tuple[Any, ...], Any], bytes]
    max_item_tokens: int
    max_aggregate_tokens: int
    max_provider_items: int
    max_item_bytes: int
    max_payload_bytes: int
    normalization_tolerance: float
    evidence_identity: str

    def __post_init__(self) -> None:
        if not callable(self.count_tokens) or not callable(self.serialize_payload):
            raise ValueError("model-compatible token and SDK payload proofs are required")
        for name in (
            "max_item_tokens", "max_aggregate_tokens", "max_provider_items",
            "max_item_bytes", "max_payload_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("verified provider bounds must be positive integers")
        if (
            type(self.normalization_tolerance) is not float
            or not math.isfinite(self.normalization_tolerance)
            or not 0 < self.normalization_tolerance < 1
            or not self.evidence_identity
        ):
            raise ValueError("offline normalization test inputs must be finite and labeled")


class _GeminiEmbedding2OfflineClient:
    """Closed in-memory SDK stand-in for adapter tests; owns no transport."""

    __slots__ = ("_response", "_error", "calls")

    def __init__(self, *, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def embed_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if self._response is not None:
            return self._response
        return SimpleNamespace(embeddings=[
            SimpleNamespace(values=[1.0] + [0.0] * 767)
            for _ in kwargs["contents"]
        ])


class _ProviderFailureKind(Enum):
    EXHAUSTED = "EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


class _ProviderResponseContractError(ValueError):
    """A bounded local interpretation failure after a successful invocation."""


class _GeminiLocalFailure(ValueError):
    def __init__(self, code: BuildFailureCode, field: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.field = field
        self.detail = detail


def create_google_client(*, timeout_seconds: float, model: str = APPROVED_MODEL) -> Any:
    """Create an SDK client using only Google's environment/ADC boundary."""
    if model == GEMINI_MODEL:
        raise ValueError("live Gemini client creation requires separate diagnostic authority")
    if model != APPROVED_MODEL:
        raise ValueError("unsupported embedding model")
    from google import genai
    from google.genai import types

    retry = types.HttpRetryOptions(attempts=1)
    options = types.HttpOptions(timeout=int(timeout_seconds * 1000), retry_options=retry)
    return genai.Client(http_options=options)


def _gemini_http_options(*, timeout_seconds: float) -> Any:
    """Pure SDK configuration proof; never constructs a credential-bearing client."""
    _require_pinned_gemini_sdk()
    if timeout_seconds != 30:
        raise ValueError("gemini-embedding-2 requires the approved 30-second timeout")
    from google.genai import types

    return types.HttpOptions(
        api_version="v1", timeout=30_000,
        retry_options=types.HttpRetryOptions(attempts=1),
    )


def _create_diagnostic_live_sdk_client(api_key: str, sync: Any, async_client: Any) -> Any:
    """SDK ownership stays in this adapter; runner owns authorization/attempts.

    No configurable endpoint, retry policy, ADC, replay or alternate client is
    admitted. Construction alone grants no campaign/provider PASS authority.
    """
    if (type(sync) is not _DiagnosticHttpxClient
            or type(async_client) is not httpx.AsyncClient
            or type(async_client._transport) is not _DiagnosticAsyncDenyTransport):
        raise DiagnosticDenied("controlled diagnostic clients are required")
    from google import genai
    from google.genai.client import DebugConfig
    options = _gemini_http_options(timeout_seconds=30)
    options.base_url = "https://generativelanguage.googleapis.com"
    options.httpx_client, options.httpx_async_client = sync, async_client
    try:
        return genai.Client(vertexai=False, api_key=api_key,
            debug_config=DebugConfig(client_mode=None, replays_directory=None, replay_id=None),
            http_options=options)
    except Exception:
        raise DiagnosticDenied("diagnostic SDK construction failed") from None


def _close_offline_resources(sdk: Any = None, sync: Any = None,
                             async_client: Any = None) -> tuple[str, ...]:
    """Attempt each acquired owner's cleanup once, without masking its peers.

    Only unused, closed MockTransport resources may use the synchronous async
    close drive. Suspension is a failure, never evidence of completed cleanup.
    """
    failed = []
    for name, client in (("sdk", sdk), ("sync", sync)):
        if client is not None:
            try:
                client.close()
            except BaseException:
                failed.append(name)
    if async_client is not None:
        closing = None
        async_failed = False
        try:
            closing = async_client.aclose()
            try:
                closing.send(None)
            except StopIteration:
                pass
            else:
                async_failed = True
        except BaseException:
            async_failed = True
        finally:
            if closing is not None:
                try:
                    closing.close()
                except BaseException:
                    async_failed = True
        if async_failed:
            failed.append("async")
    return tuple(failed)


def _create_gemini_offline_mock_client(handler: Callable[[Any], Any]) -> tuple[Any, Any, Any]:
    """SDK-bearing test transport stays inside the approved Google adapter port.

    This helper accepts only an HTTPX MockTransport handler, uses an explicit
    synthetic key, and cannot discover credentials or select a live transport.
    """
    _require_pinned_gemini_sdk()
    import httpx
    from google import genai
    from google.genai import types

    def blocked(_: Any) -> Any:
        raise ValueError("asynchronous diagnostic transport is forbidden")

    sync = async_client = None
    stage = "sync acquisition"
    try:
        sync = httpx.Client(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False,
        )
        stage = "async acquisition"
        async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(blocked), trust_env=False,
        )
        stage = "SDK constructor"
        client = genai.Client(
            vertexai=False, api_key="synthetic-offline-not-a-credential",
            http_options=types.HttpOptions(
                api_version="v1", timeout=30_000,
                retry_options=types.HttpRetryOptions(attempts=1),
                httpx_client=sync, httpx_async_client=async_client,
            ),
        )
    except BaseException:
        failed = _close_offline_resources(sync=sync, async_client=async_client)
        detail = ",".join(failed) if failed else "none"
        raise ValueError(f"offline construction failed at {stage}; cleanup failures: {detail}") from None
    return client, sync, async_client


class _SealedDiagnosticFields:
    """Ordinary Python mutation protection within the trusted runner boundary.

    Not an isolation boundary against arbitrary code/object.__setattr__. No
    caller receives a mutable alias to the final headers, timeout or body.
    """

    _sealed = False

    def __setattr__(self, name: str, value: Any) -> None:
        if self._sealed:
            raise DiagnosticDenied("sealed diagnostic request mutation denied")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if self._sealed:
            raise DiagnosticDenied("sealed diagnostic request mutation denied")
        object.__delattr__(self, name)

    def __getattribute__(self, name: str) -> Any:
        value = object.__getattribute__(self, name)
        return MappingProxyType(value) if name == "__dict__" else value


class _DiagnosticHeaders(_SealedDiagnosticFields, httpx.Headers):
    def __init__(self, headers: httpx.Headers) -> None:
        super().__init__(headers.raw, encoding=headers.encoding)
        self._list = tuple(self._list)
        self._sealed = True

    def _deny(self, *args: Any, **kwargs: Any) -> Any:
        raise DiagnosticDenied("sealed diagnostic headers mutation denied")

    __setitem__ = __delitem__ = update = setdefault = pop = popitem = clear = _deny


class _DiagnosticURL(_SealedDiagnosticFields, httpx.URL):
    def __init__(self, url: httpx.URL) -> None:
        super().__init__(url)
        self._sealed = True


@dataclass(frozen=True, slots=True)
class _DiagnosticBody(httpx.SyncByteStream):
    data: bytes

    def __iter__(self):
        yield self.data


class _DiagnosticFinalRequest(_SealedDiagnosticFields, httpx.Request):
    def __init__(self, built: httpx.Request) -> None:
        # This is the final object returned by build_request(), BEFORE any
        # evidence check. Preserve actual SDK serialization; never serialize a
        # mirror or construct a substitute after guard verification.
        if type(built.content) is not bytes or set(built.extensions) != {"timeout"}:
            raise DiagnosticDenied("diagnostic SDK request buffering is invalid")
        super().__init__(built.method, built.url, headers=built.headers,
                         content=built.content, extensions=built.extensions)
        self.url = _DiagnosticURL(self.url)
        self.headers = _DiagnosticHeaders(self.headers)
        self.extensions = MappingProxyType({
            "timeout": MappingProxyType(dict(built.extensions["timeout"])),
        })
        self.stream = _DiagnosticBody(self.content)
        self._sealed = True


class _DiagnosticHttpxClient(httpx.Client):
    def __init__(self, guard: "_DiagnosticGuardedMockTransport") -> None:
        super().__init__(transport=guard, trust_env=False, follow_redirects=False,
                         max_redirects=0, auth=None)
        self._final_request: _DiagnosticFinalRequest | None = None

    def build_request(self, *args: Any, **kwargs: Any) -> httpx.Request:
        if self._final_request is not None:
            raise DiagnosticDenied("second diagnostic request construction denied")
        final = _DiagnosticFinalRequest(super().build_request(*args, **kwargs))
        self._final_request = final
        return final

    def _check_closed_policy(self) -> None:
        if (self.trust_env or self.follow_redirects or self.max_redirects != 0
                or self._auth is not None or self._mounts
                or any(self.event_hooks.values())):
            raise DiagnosticDenied("diagnostic HTTPX policy mutation denied")

    def send(self, request: httpx.Request, *, stream: bool = False,
             auth: Any = None, follow_redirects: Any = False) -> httpx.Response:
        self._check_closed_policy()
        if (request is not self._final_request or type(request) is not _DiagnosticFinalRequest
                or stream is not False or auth is not None or follow_redirects is not False):
            raise DiagnosticDenied("diagnostic request substitution or send override denied")
        return super().send(request, stream=False, auth=None, follow_redirects=False)


class _DiagnosticAsyncDenyTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise DiagnosticDenied("asynchronous diagnostic dispatch denied")


@dataclass(frozen=True, slots=True)
class _DiagnosticMockResponse:
    status: int = 200
    transport_error: str | None = None
    count: int | None = None
    mode: str = "ordered"

    def __post_init__(self) -> None:
        if (type(self.status) is not int or self.status not in (200, 302, 400, 401, 403, 408, 429, 500, 503, 504, 507)
                or self.transport_error not in (None, "timeout", "connection")
                or (self.count is not None and (type(self.count) is not int or not 0 <= self.count <= 128))
                or self.mode not in ("ordered", "reverse", "wrong_dimension", "unnormalized", "malformed")):
            raise DiagnosticDenied("synthetic diagnostic response fixture is invalid")


class _DiagnosticClosedMockTransport(_SealedDiagnosticFields, httpx.MockTransport):
    def __init__(self, response: _DiagnosticMockResponse) -> None:
        observations = []

        def handle(request: httpx.Request) -> httpx.Response:
            observations.append((request, request.content, b"".join(request.stream)))
            if response.transport_error == "timeout":
                raise httpx.ReadTimeout("synthetic offline timeout", request=request)
            if response.transport_error == "connection":
                raise httpx.ConnectError("synthetic offline connection failure", request=request)
            if response.status == 302:
                return httpx.Response(302, headers={"location": "https://offline.invalid/denied"})
            if response.status != 200:
                return httpx.Response(response.status, json={"error": {
                    "code": response.status, "message": "synthetic offline failure",
                }})
            count = len(json.loads(request.content)["requests"]) if response.count is None else response.count
            order = range(count - 1, -1, -1) if response.mode == "reverse" else range(count)
            vectors = [[math.cos(i * 0.01), math.sin(i * 0.01), *([0.0] * 766)] for i in order]
            if vectors and response.mode == "wrong_dimension":
                vectors[0].pop()
            if vectors and response.mode == "unnormalized":
                vectors[0][0] = 2.0
            if response.mode == "malformed":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"embeddings": [{"values": v} for v in vectors]})

        super().__init__(handle)
        self.observations = observations
        self._sealed = True


class _DiagnosticGuardedMockTransport(httpx.BaseTransport):
    """A one-shot, synchronous mock guard, never a runner lock or authority."""

    def __init__(self, response: _DiagnosticMockResponse) -> None:
        if type(response) is not _DiagnosticMockResponse:
            raise DiagnosticDenied("fixed synthetic response fixture is required")
        self._delegate = _DiagnosticClosedMockTransport(response)
        self._lock = Lock()
        self._entered = False
        self._closed = False
        self._handle: _DiagnosticOfflineSdkHandle | None = None
        self.delegations = 0
        self.guarded_request: httpx.Request | None = None
        self.delegated_request: httpx.Request | None = None
        self.delegated_body: bytes | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not self._lock.acquire(blocking=False):
            raise DiagnosticDenied("concurrent or reentrant diagnostic send denied")
        try:
            if self._entered or self._closed:
                raise DiagnosticDenied("second or closed diagnostic send denied")
            self._entered = True  # Consume before ALL validation; never re-arm.
            self.guarded_request = request
            if self._handle is None:
                raise DiagnosticDenied("diagnostic SDK construction is incomplete")
            self._handle._check_policy()
            if (type(request) is not _DiagnosticFinalRequest
                    or request is not self._handle._sync._final_request
                    or type(request.stream) is not _DiagnosticBody
                    or request.stream.data != request.content
                    or b"".join(request.stream) != request.content
                    or type(self._delegate) is not _DiagnosticClosedMockTransport
                    or request.headers.get("content-length") != str(len(request.content))
                    or request.headers.get("host") != "generativelanguage.googleapis.com"
                    or request.headers.get("x-goog-api-key") != "synthetic-offline-not-a-credential"
                    or request.headers.get("x-server-timeout") != "30"
                    or "authorization" in request.headers or "proxy-authorization" in request.headers
                    or "transfer-encoding" in request.headers
                    or request.url.query or request.url.fragment or request.url.userinfo):
                raise DiagnosticDenied("diagnostic final request identity or stream mismatch")
            self._verify_request(request)
            # No callback, await, mutation, copy, or alternate transport here.
            self.delegations += 1
            self.delegated_request = request
            self.delegated_body = request.content
            response = self._delegate.handle_request(request)
            if 300 <= response.status_code < 400:
                response.close()
                raise DiagnosticDenied("diagnostic redirect denied")
            return response
        finally:
            self._lock.release()

    def _verify_request(self, request: httpx.Request) -> None:
        raise DiagnosticDenied("diagnostic verification is unavailable")

    def close(self) -> None:
        self._closed = True
        self._delegate.close()


class _DiagnosticOfflineSdkHandle:
    def __init__(self, sdk: Any, sync: _DiagnosticHttpxClient, async_client: httpx.AsyncClient,
                 guard: _DiagnosticGuardedMockTransport) -> None:
        self._sdk, self._sync, self._async, self._guard = sdk, sync, async_client, guard
        self._invoked = False
        self._closed = False
        self._invoke_lock = Lock()
        self._close_lock = Lock()
        self.cleanup_failures: tuple[str, ...] = ()

    def _check_policy(self) -> None:
        try:
            self._check_policy_facts()
        except Exception:
            raise DiagnosticDenied("diagnostic SDK policy mutation denied") from None

    def _check_policy_facts(self) -> None:
        self._sync._check_closed_policy()
        api = self._sdk._api_client
        options = api._http_options
        config = self._guard._prepared.config
        if (self._closed or api.vertexai is not False
                or self._sdk.models._api_client is not api
                or type(self._guard).handle_request is not _DiagnosticGuardedMockTransport.handle_request
                or getattr(self._guard.handle_request, "__func__", None) is not _DiagnosticGuardedMockTransport.handle_request
                or type(self._guard._delegate) is not _DiagnosticClosedMockTransport
                or self._guard._handle is not self
                or api.api_key != "synthetic-offline-not-a-credential"
                or options.api_version != "v1" or options.timeout != 30_000
                or options.retry_options.attempts != 1
                or options.headers.get("x-goog-api-key") != "synthetic-offline-not-a-credential"
                or any(k.lower() in ("authorization", "proxy-authorization") for k in options.headers)
                or any(k.lower() == "x-server-timeout" and v != "30" for k, v in options.headers.items())
                or api._retry.stop.max_attempt_number != 1
                or options.base_url.rstrip("/") != "https://generativelanguage.googleapis.com"
                or api._httpx_client is not self._sync or options.httpx_client is not self._sync
                or api._async_httpx_client is not self._async
                or options.httpx_async_client is not self._async
                or self._sync._transport is not self._guard
                or type(self._async._transport) is not _DiagnosticAsyncDenyTransport
                or self._async.trust_env or self._async.follow_redirects
                or self._async._mounts or self._async._auth is not None
                or any(self._async.event_hooks.values())
                or self._sdk._debug_config.client_mode is not None
                or self._sdk._debug_config.replays_directory is not None
                or self._sdk._debug_config.replay_id is not None
                or config.http_options is not None or config.output_dimensionality != 768
                or config.task_type is not None or config.auto_truncate is not None):
            raise DiagnosticDenied("diagnostic SDK policy mutation denied")

    def embed_prepared(self) -> Any:
        if not self._invoke_lock.acquire(blocking=False):
            raise DiagnosticDenied("concurrent diagnostic invocation denied")
        try:
            if self._invoked:
                raise DiagnosticDenied("second diagnostic invocation denied")
            self._invoked = True
            self._check_policy()
            prepared = self._guard._prepared
            return self._sdk.models.embed_content(
                model=GEMINI_MODEL,
                contents=[content.model_copy(deep=True) for content in prepared.contents],
                config=prepared.config.model_copy(deep=True),
            )
        finally:
            self._invoke_lock.release()

    def close(self) -> None:
        if not self._close_lock.acquire(blocking=False):
            raise DiagnosticDenied("diagnostic cleanup is already active")
        try:
            if not self._closed:
                self._closed = True
                self.cleanup_failures = _close_offline_resources(self._sdk, self._sync, self._async)
            if self.cleanup_failures:
                raise DiagnosticDenied("diagnostic SDK cleanup failed; reservation must be retained")
        finally:
            self._close_lock.release()


def _create_guarded_diagnostic_offline_sdk_client(
    guard: _DiagnosticGuardedMockTransport,
) -> _DiagnosticOfflineSdkHandle:
    """Own SDK construction for a closed diagnostic guard, not live authority.

    No caller client, credentials, SDK factory, options or transport is accepted.
    The subclass supplies only diagnostic verification; physical dispatch remains
    the fixed one-shot implementation and the sealed, synthetic MockTransport.
    """
    if (not isinstance(guard, _DiagnosticGuardedMockTransport)
            or type(guard) is _DiagnosticGuardedMockTransport
            or type(guard).handle_request is not _DiagnosticGuardedMockTransport.handle_request
            or getattr(guard.handle_request, "__func__", None) is not _DiagnosticGuardedMockTransport.handle_request
            or type(guard).close is not _DiagnosticGuardedMockTransport.close
            or getattr(guard.close, "__func__", None) is not _DiagnosticGuardedMockTransport.close
            or type(guard._delegate) is not _DiagnosticClosedMockTransport
            or guard._entered or guard._closed or guard._handle is not None or guard.delegations):
        raise DiagnosticDenied("closed unused diagnostic guard is required")
    # Pytest adds its current-test marker after fixture setup. No SDK/auth/env
    # key is admitted and no ambient value is read to establish this boundary.
    if type(os.environ) is not dict or set(os.environ) - {"PYTEST_CURRENT_TEST"}:
        raise DiagnosticDenied("isolated offline environment is required")
    if version("httpx") != "0.28.1":
        raise DiagnosticDenied("reviewed diagnostic HTTPX baseline is required")
    options = _gemini_http_options(timeout_seconds=30)
    prepared = guard._prepared
    if (prepared.safety_decision.evidence_status != "SYNTHETIC_OFFLINE_ONLY"
            or guard._evidence.status != "SYNTHETIC_OFFLINE_ONLY"
            or prepared.config.http_options is not None
            or prepared.config.output_dimensionality != 768
            or prepared.config.task_type is not None or prepared.config.auto_truncate is not None):
        raise DiagnosticDenied("synthetic diagnostic factory inputs are invalid")
    # Copy mutable SDK models before ownership transfer, never expected hashes
    # from the unverified outbound request. The supplied E4 artifact stays fixed.
    prepared = replace(prepared, contents=tuple(c.model_copy(deep=True) for c in prepared.contents),
                       config=prepared.config.model_copy(deep=True))
    from google import genai
    from google.genai.client import DebugConfig

    guard._prepared = prepared
    sync = async_client = sdk = None
    stage = "sync acquisition"
    try:
        sync = _DiagnosticHttpxClient(guard)
        stage = "async acquisition"
        async_client = httpx.AsyncClient(transport=_DiagnosticAsyncDenyTransport(),
                                         trust_env=False, follow_redirects=False, max_redirects=0)
        options.httpx_client, options.httpx_async_client = sync, async_client
        stage = "SDK constructor"
        sdk = genai.Client(vertexai=False, api_key="synthetic-offline-not-a-credential",
                           debug_config=DebugConfig(client_mode=None, replays_directory=None,
                                                    replay_id=None), http_options=options)
        handle = _DiagnosticOfflineSdkHandle(sdk, sync, async_client, guard)
        guard._handle = handle
        handle._check_policy()
        return handle
    except BaseException:
        failed = _close_offline_resources(sdk, sync, async_client)
        detail = ",".join(failed) if failed else "none"
        raise DiagnosticDenied(
            f"offline construction failed at {stage}; cleanup failures: {detail}"
        ) from None


def _require_pinned_gemini_sdk() -> None:
    try:
        installed = version("google-genai")
    except PackageNotFoundError as exc:
        raise ValueError("pinned Gemini SDK is unavailable") from exc
    if installed != "2.25.0":
        raise ValueError("Gemini SDK baseline requires google-genai==2.25.0")


def _config(task_type: str) -> Any:
    try:
        from google.genai import types
        return types.EmbedContentConfig(task_type=task_type, auto_truncate=False)
    except ImportError:
        return {"task_type": task_type, "auto_truncate": False}


def _typed_attribute(exc: Exception, name: str, expected_type: type) -> Any | None:
    """Read only allowlisted scalar facts; hostile descriptors fail closed."""
    try:
        value = getattr(exc, name, None)
    except Exception:
        return None
    return value if type(value) is expected_type else None


def _provider_failure_kind(exc: Exception) -> _ProviderFailureKind:
    code = _typed_attribute(exc, "code", int)
    status_code = _typed_attribute(exc, "status_code", int)
    status = _typed_attribute(exc, "status", str)
    http_status = code if code is not None else status_code
    if http_status in (429, 507) or status == "RESOURCE_EXHAUSTED":
        return _ProviderFailureKind.EXHAUSTED
    if http_status in (408, 504) or isinstance(exc, TimeoutError) or type(exc).__name__ in {
        "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    }:
        return _ProviderFailureKind.TIMEOUT
    return _ProviderFailureKind.UNAVAILABLE


def _values(response: Any, *, strict_numbers: bool = False) -> tuple[tuple[float, ...], ...]:
    try:
        embeddings = getattr(response, "embeddings")
    except AttributeError as exc:
        raise _ProviderResponseContractError("missing embeddings") from exc
    if embeddings is None:
        raise _ProviderResponseContractError("missing embeddings")
    try:
        items = tuple(embeddings)
    except (TypeError, ValueError) as exc:
        raise _ProviderResponseContractError("malformed embeddings") from exc

    vectors = []
    for item in items:
        try:
            raw_values = getattr(item, "values")
        except AttributeError as exc:
            raise _ProviderResponseContractError("missing embedding values") from exc
        if raw_values is None:
            raise _ProviderResponseContractError("missing embedding values")
        try:
            values = tuple(raw_values)
        except (TypeError, ValueError) as exc:
            raise _ProviderResponseContractError("malformed embedding values") from exc
        converted = []
        for value in values:
            if isinstance(value, bool) or (strict_numbers and type(value) not in (int, float)):
                raise _ProviderResponseContractError("invalid embedding number")
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise _ProviderResponseContractError("invalid embedding number") from exc
            if not math.isfinite(number):
                raise _ProviderResponseContractError("non-finite embedding number")
            converted.append(number)
        vectors.append(tuple(converted))
    return tuple(vectors)


def _validated_vectors(
    response: Any, expected_cardinality: int, *, normalization_tolerance: float | None = None,
    strict_numbers: bool = False,
) -> tuple[tuple[float, ...], ...]:
    vectors = _values(response, strict_numbers=strict_numbers)
    if len(vectors) != expected_cardinality or any(
        len(vector) != APPROVED_DIMENSION for vector in vectors
    ):
        raise _ProviderResponseContractError("incompatible embedding shape")
    if normalization_tolerance is not None:
        for vector in vectors:
            norm = math.hypot(*vector)
            if not math.isfinite(norm) or abs(norm - 1.0) > normalization_tolerance:
                raise _ProviderResponseContractError("incompatible embedding normalization")
    return vectors


class GoogleEmbeddingAdapter:
    """Implements both Candidate-C embedding ports without retry authority."""

    def __init__(
        self, client: Any, capability: ProviderCapability, *,
        gemini_verification: GeminiEmbedding2Verification | None = None,
    ) -> None:
        gemini = capability.model == GEMINI_MODEL
        if (
            capability.provider != APPROVED_PROVIDER
            or (capability.model != APPROVED_MODEL and not gemini)
            or capability.embedding_dimension != APPROVED_DIMENSION
            or not capability.hidden_retries_disabled
            or (gemini and (
                capability.capability_identity != GEMINI_EMBEDDING2_PROFILE.capability_identity
                or capability.embedding_profile_identity != GEMINI_EMBEDDING2_PROFILE.embedding_profile_identity
            ))
        ):
            raise ValueError("Google embedding capability is not the approved fixed baseline")
        if gemini:
            _require_pinned_gemini_sdk()
        self._client = client
        self._capability = capability
        self._gemini_verification = gemini_verification

    def capability(self) -> ProviderCapability:
        return self._capability

    @staticmethod
    def _failure(
        code: BuildFailureCode, detail: str, retry: RetrySafetyDisposition,
        field: str = "provider",
    ) -> BuildFailureFact:
        return BuildFailureFact(code, field, detail, retry)

    def _gemini_prepare(
        self, texts: tuple[str, ...], limits: Any,
    ) -> tuple[tuple[Any, ...], Any]:
        proof = self._gemini_verification
        if proof is None:
            raise _GeminiLocalFailure(
                BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED, "provider_bounds",
                "model token, payload, and normalization evidence is not verified",
            )
        if (
            limits.timeout_seconds != 30 or limits.maximum_invocations != 1
            or len(texts) > limits.maximum_batch_items
            or any(len(texts) > getattr(limits, name) for name in (
                "maximum_cost_units", "maximum_rate_units", "maximum_quota_units",
                "maximum_resource_units",
            ))
        ):
            raise _GeminiLocalFailure(
                BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED, "provider_bounds",
                "request exceeds the approved invocation policy",
            )
        try:
            from google.genai import types
            contents = tuple(
                types.Content(role="user", parts=[types.Part(text=value)])
                for value in texts
            )
            config = types.EmbedContentConfig(output_dimensionality=APPROVED_DIMENSION)
            counts = tuple(proof.count_tokens(value) for value in texts)
            payload = proof.serialize_payload(contents, config)
            expected = {"requests": [{
                "model": "models/" + GEMINI_MODEL,
                "content": {"role": "user", "parts": [{"text": value}]},
                "outputDimensionality": APPROVED_DIMENSION,
            } for value in texts]}
            if type(payload) is not bytes or json.loads(payload) != expected:
                raise ValueError("serialized payload differs from the pinned SDK contract")
            # Escaped, pretty JSON is a conservative bound for the pinned
            # request shape; the synthetic SDK transport test checks it.
            conservative_payload_bytes = len(
                json.dumps(expected, ensure_ascii=True, indent=2).encode("utf-8")
            )
        except Exception as exc:
            raise _GeminiLocalFailure(
                BuildFailureCode.PROVIDER_CONTRACT_INVALID, "local_serialization",
                "local SDK serialization or token proof is invalid",
            ) from exc
        if (
            any(type(count) is not int or count <= 0 or count > proof.max_item_tokens for count in counts)
            or sum(counts) > proof.max_aggregate_tokens
            or len(texts) > proof.max_provider_items
            or any(len(value.encode("utf-8")) > proof.max_item_bytes for value in texts)
            or sum(len(value.encode("utf-8")) for value in texts) > min(
                proof.max_payload_bytes, limits.maximum_request_bytes,
            )
            or max(len(payload), conservative_payload_bytes) > min(
                proof.max_payload_bytes, limits.maximum_request_bytes,
            )
        ):
            raise _GeminiLocalFailure(
                BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED, "provider_bounds",
                "request exceeds verified item, token, or serialized payload bounds",
            )
        return contents, config

    def _gemini_documents(self, request: ProviderEmbeddingRequest) -> ProviderEmbeddingResult:
        def failed(code: BuildFailureCode, field: str, detail: str, calls: int = 0) -> ProviderEmbeddingResult:
            return ProviderEmbeddingResult(
                self._capability, (), calls, 0, 0, 0, 0,
                self._failure(code, detail, RetrySafetyDisposition.DO_NOT_RETRY, field),
            )

        if request.capability != self._capability:
            return failed(BuildFailureCode.PROFILE_MISMATCH, "capability", "provider capability continuity failed")
        if type(self._client) is not _GeminiEmbedding2OfflineClient:
            return failed(
                BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_authorization",
                "live Gemini invocation requires separate diagnostic authority",
            )
        document_ids = dict(request.approved_document_ids)
        if (
            not request.approved_corpus_id
            or set(document_ids) != {chunk.document_identity for chunk in request.chunks}
            or any(
                document_identity(request.approved_corpus_id, approved_id) != identity
                for identity, approved_id in document_ids.items()
            )
        ):
            return failed(BuildFailureCode.PROFILE_MISMATCH, "approved_document_ids", "approved document ID mapping is missing or inconsistent")
        texts = tuple(
            f"title: {document_ids[chunk.document_identity]} | text: {chunk.content}"
            for chunk in request.chunks
        )
        try:
            contents, config = self._gemini_prepare(texts, request.limits)
        except _GeminiLocalFailure as exc:
            return failed(exc.code, exc.field, exc.detail)
        try:
            response = self._client.embed_content(
                model=GEMINI_MODEL, contents=list(contents), config=config,
            )
        except Exception as exc:
            code, field, detail, retry = self._gemini_invocation_failure(exc)
            return ProviderEmbeddingResult(
                self._capability, (), 1, 0, 0, 0, 0,
                self._failure(code, detail, retry, field),
            )
        try:
            vectors = _validated_vectors(
                response, len(request.chunks),
                normalization_tolerance=self._gemini_verification.normalization_tolerance,
                strict_numbers=True,
            )
            embeddings = tuple(
                EmbeddingVector(chunk.chunk_identity, vector)
                for chunk, vector in zip(request.chunks, vectors, strict=True)
            )
        except Exception:
            return failed(BuildFailureCode.PROVIDER_CONTRACT_INVALID, "response_contract", "provider returned incompatible embedding facts", 1)
        units = len(embeddings)
        return ProviderEmbeddingResult(self._capability, embeddings, 1, units, units, units, units)

    @staticmethod
    def _gemini_invocation_failure(
        exc: Exception, *, query: bool = False,
    ) -> tuple[BuildFailureCode, str, str, RetrySafetyDisposition]:
        from google.genai import errors
        if isinstance(exc, errors.APIError):
            kind = _provider_failure_kind(exc)
            if query:
                code = _typed_attribute(exc, "code", int)
                status = code if code is not None else _typed_attribute(exc, "status_code", int)
                if status == 400:
                    return (BuildFailureCode.PROVIDER_CONTRACT_INVALID, "provider_request", "provider rejected the embedding request contract", RetrySafetyDisposition.DO_NOT_RETRY)
                if status in (401, 403):
                    return (BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_authorization", "provider authentication or authorization failed", RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED)
                if status in (408, 504):
                    return (BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_timeout", "provider request timed out", RetrySafetyDisposition.SAME_OPERATION_ONLY)
                if status == 429:
                    return (BuildFailureCode.PROVIDER_EXHAUSTED, "provider_rate_limit", "provider rate limit exhausted", RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED)
                if status in (500, 503):
                    return (BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_service", "provider service is unavailable", RetrySafetyDisposition.SAME_OPERATION_ONLY)
                if kind is _ProviderFailureKind.TIMEOUT:
                    return (BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_timeout", "provider request timed out", RetrySafetyDisposition.SAME_OPERATION_ONLY)
                if kind is _ProviderFailureKind.EXHAUSTED:
                    return (BuildFailureCode.PROVIDER_EXHAUSTED, "provider_resource", "provider quota or resource exhausted", RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED)
            if kind is _ProviderFailureKind.EXHAUSTED:
                return (BuildFailureCode.PROVIDER_EXHAUSTED, "provider_error", "provider quota or resource exhausted", RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED)
            return (BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_error", "provider rejected the embedding request", RetrySafetyDisposition.DO_NOT_RETRY)
        if isinstance(exc, (ValueError, TypeError)):
            return (BuildFailureCode.PROVIDER_CONTRACT_INVALID, "local_serialization", "local SDK request serialization failed", RetrySafetyDisposition.DO_NOT_RETRY)
        if query and _provider_failure_kind(exc) is _ProviderFailureKind.TIMEOUT:
            return (BuildFailureCode.PROVIDER_UNAVAILABLE, "transport_timeout", "embedding transport timed out", RetrySafetyDisposition.SAME_OPERATION_ONLY)
        return (BuildFailureCode.PROVIDER_UNAVAILABLE, "transport", "embedding transport failed or timed out", RetrySafetyDisposition.SAME_OPERATION_ONLY)

    def embed(self, request: ProviderEmbeddingRequest) -> ProviderEmbeddingResult:
        if self._capability.model == GEMINI_MODEL:
            return self._gemini_documents(request)
        size = sum(len(chunk.content.encode("utf-8")) for chunk in request.chunks)
        projected_units = len(request.chunks)
        if (
            size > request.limits.maximum_request_bytes
            or projected_units > request.limits.maximum_batch_items
            or projected_units > request.limits.maximum_cost_units
            or projected_units > request.limits.maximum_rate_units
            or projected_units > request.limits.maximum_quota_units
            or projected_units > request.limits.maximum_resource_units
        ):
            return ProviderEmbeddingResult(
                self._capability, (), 0, 0, 0, 0, 0,
                self._failure(BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED, "provider request exceeds approved bounds", RetrySafetyDisposition.DO_NOT_RETRY),
            )
        if request.capability != self._capability or request.limits.maximum_invocations < 1:
            return ProviderEmbeddingResult(
                self._capability, (), 0, 0, 0, 0, 0,
                self._failure(BuildFailureCode.PROFILE_MISMATCH, "provider capability continuity failed", RetrySafetyDisposition.DO_NOT_RETRY),
            )
        config = _config("RETRIEVAL_DOCUMENT")
        try:
            response = self._client.models.embed_content(
                model=APPROVED_MODEL,
                contents=[chunk.content for chunk in request.chunks],
                config=config,
            )
        except Exception as exc:
            kind = _provider_failure_kind(exc)
            exhausted = kind is _ProviderFailureKind.EXHAUSTED
            detail = (
                "provider quota or resource exhausted" if exhausted
                else "provider invocation timed out" if kind is _ProviderFailureKind.TIMEOUT
                else "provider invocation unavailable"
            )
            return ProviderEmbeddingResult(
                self._capability, (), 1, 0, 0, 0, 0,
                self._failure(
                    BuildFailureCode.PROVIDER_EXHAUSTED if exhausted else BuildFailureCode.PROVIDER_UNAVAILABLE,
                    detail,
                    RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED if exhausted else RetrySafetyDisposition.SAME_OPERATION_ONLY,
                ),
            )
        try:
            vectors = _validated_vectors(response, len(request.chunks))
            embeddings = tuple(
                EmbeddingVector(chunk.chunk_identity, vector)
                for chunk, vector in zip(request.chunks, vectors)
            )
        except (_ProviderResponseContractError, KnowledgeValidationError):
            return ProviderEmbeddingResult(
                self._capability, (), 1, 0, 0, 0, 0,
                self._failure(BuildFailureCode.PROVIDER_CONTRACT_INVALID, "provider returned incompatible embedding facts", RetrySafetyDisposition.DO_NOT_RETRY),
            )
        units = len(embeddings)
        return ProviderEmbeddingResult(self._capability, embeddings, 1, units, units, units, units)

    def _gemini_query(self, request: QueryEmbeddingRequest) -> QueryEmbeddingResult:
        def failed(
            code: BuildFailureCode, field: str, detail: str, calls: int = 0,
            retry: RetrySafetyDisposition = RetrySafetyDisposition.DO_NOT_RETRY,
        ) -> QueryEmbeddingResult:
            return QueryEmbeddingResult(
                self._capability, (), calls, detail,
                failure=self._failure(code, detail, retry, field),
            )

        if request.capability != self._capability:
            return failed(BuildFailureCode.PROFILE_MISMATCH, "capability", "query capability mismatch")
        if type(self._client) is not _GeminiEmbedding2OfflineClient:
            return failed(
                BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_authorization",
                "live Gemini invocation is unauthorized", retry=RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED,
            )
        text = f"task: search result | query: {request.query.text}"
        try:
            contents, config = self._gemini_prepare((text,), request.limits)
        except _GeminiLocalFailure as exc:
            return failed(exc.code, exc.field, f"{exc.field}: {exc.detail}")
        try:
            response = self._client.embed_content(
                model=GEMINI_MODEL, contents=list(contents), config=config,
            )
        except Exception as exc:
            code, field, detail, retry = self._gemini_invocation_failure(exc, query=True)
            # A "provider_authorization:" prefix resembles a secret-bearing
            # header to the approved failure-detail validator.
            bounded_detail = detail if field == "provider_authorization" else f"{field}: {detail}"
            return failed(code, field, bounded_detail, 1, retry)
        try:
            vectors = _validated_vectors(
                response, 1,
                normalization_tolerance=self._gemini_verification.normalization_tolerance,
                strict_numbers=True,
            )
        except Exception:
            return failed(BuildFailureCode.PROVIDER_CONTRACT_INVALID, "response_contract", "response_contract: provider returned incompatible query embedding", 1)
        return QueryEmbeddingResult(self._capability, vectors[0], 1, None, 1, 1, 1, 1)

    def embed_query(self, request: QueryEmbeddingRequest) -> QueryEmbeddingResult:
        if self._capability.model == GEMINI_MODEL:
            return self._gemini_query(request)
        size = len(request.query.text.encode("utf-8"))
        if (
            request.capability != self._capability
            or size > request.limits.maximum_request_bytes
            or request.limits.maximum_batch_items < 1
            or request.limits.maximum_invocations < 1
        ):
            return QueryEmbeddingResult(self._capability, (), 0, "query embedding request violates approved bounds")
        config = _config("RETRIEVAL_QUERY")
        try:
            response = self._client.models.embed_content(
                model=APPROVED_MODEL,
                contents=[request.query.text],
                config=config,
            )
        except Exception:
            return QueryEmbeddingResult(self._capability, (), 1, "query embedding provider is unavailable")
        try:
            vectors = _validated_vectors(response, 1)
        except _ProviderResponseContractError:
            return QueryEmbeddingResult(self._capability, (), 1, "query embedding response is incompatible")
        return QueryEmbeddingResult(self._capability, vectors[0], 1, None, 1, 1, 1, 1)
