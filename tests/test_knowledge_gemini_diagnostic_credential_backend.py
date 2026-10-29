"""BV-01 test-only evidence against EXISTING controls and offline SDK seams.

No production resolver/backend or account/ACL mutation is defined here. Native
test helpers are disabled by a source-fixed, currently absent execution scope.
Metadata comparisons prove equality/rejection only. SDK-owner cleanup and mock
interop observations do not prove real credential-buffer/DPAPI cleanup.

Native preparation (NOT OBSERVED): independently authorized Windows hosts,
publisher/runner/unauthorized SIDs, primary-token/service conditions, an exact
disposable fixture root and ACL operations, offline Microsoft API/ABI/flags
review, DATA_BLOB/LocalFree ownership, corruption/cross-host observations and
bounded-buffer cleanup. Machine scope is NOT exclusive to a runner SID.
Actual secret acquisition, backend rotation and credential cleanup are NOT
TESTABLE in this slice: the production resolver/backend does not exist.
"""

import ctypes
import json
import os
import socket
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import knowledge_index.gemini_diagnostic as diagnostic
import knowledge_index.gemini_diagnostic_controls as controls
import knowledge_index.google_embedding_adapter as adapter
from knowledge_index.gemini_diagnostic_controls import DiagnosticDenied
from knowledge_index.gemini_diagnostic_controls import _read_trusted_file as _existing_trusted_read
from test_knowledge_gemini_diagnostic import _e4_case
from test_knowledge_gemini_diagnostic_controls import e4_host_material, safety_case, safety_parse


DUMMY_ERROR = "BV01-DUMMY-NOT-A-CREDENTIAL"


@pytest.fixture
def candidate_a_material():
    """A01 wire examples only: no parser/resolver, grant, or real credential.

    Candidate A strict parsing, credential freshness, mixed-revision acquisition
    and reservation-before-secret-read are NOT TESTABLE UNTIL G4-S2b. Existing
    safety/authorization comparisons below prove only their own source behavior.
    Host ACL/storage/backup acceptance requires separate actual host evidence.
    """
    grant, approved, e4, safety = safety_case()
    metadata = dict(schema_version=1, record_type="DIAGNOSTIC_CREDENTIAL_METADATA",
        backend_id="HOST_CONTROLLED_PROTECTED_FILE_V1",
        profile_identity=grant.credential_profile_identity, secret_revision="r1",
        account_reference=safety["pricing_evidence"]["account_reference"],
        authorized_host=grant.authorized_host, runner_sid=grant.runner_identity,
        validity=dict(not_before="2000-01-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z"),
        lifecycle_state="ACTIVE")
    envelope = dict(metadata=json.loads(json.dumps(metadata)), secret_value=DUMMY_ERROR)
    return metadata, envelope, grant, approved, e4, safety


def test_bv01_candidate_a_fixture_contract_consistency_only(candidate_a_material):
    metadata, envelope, grant, _, _, safety = candidate_a_material
    # Assertions document approved FIXTURES; they are not a surrogate parser.
    assert set(metadata) == {"schema_version", "record_type", "backend_id", "profile_identity",
        "secret_revision", "account_reference", "authorized_host", "runner_sid", "validity", "lifecycle_state"}
    assert type(metadata["schema_version"]) is int and metadata["schema_version"] == 1
    assert metadata["record_type"] == "DIAGNOSTIC_CREDENTIAL_METADATA"
    assert metadata["backend_id"] == "HOST_CONTROLLED_PROTECTED_FILE_V1"
    assert set(metadata["validity"]) == {"not_before", "expires_at"}
    assert metadata["lifecycle_state"] == "ACTIVE"
    assert set(envelope) == {"metadata", "secret_value"}
    assert envelope["metadata"] == metadata and envelope["metadata"] is not metadata
    assert metadata["profile_identity"] == grant.credential_profile_identity
    assert metadata["secret_revision"] == safety["credential_binding"]["secret_revision"]
    assert metadata["authorized_host"] == grant.authorized_host
    assert metadata["runner_sid"] == grant.runner_identity
    assert 1 <= len(envelope["secret_value"]) <= 4096
    assert all(0x21 <= ord(char) <= 0x7e for char in envelope["secret_value"])
    assert len(json.dumps(metadata).encode()) <= 16_384
    assert len(json.dumps(envelope).encode()) <= 65_536
    assert DUMMY_ERROR not in json.dumps(metadata)


@pytest.mark.parametrize("field,value", [
    (None, None), ("profile_identity", "other-profile"), ("profile_identity", None),
    ("secret_revision", "r2"), ("secret_revision", None),
    ("account_reference", "other-account"), ("account_reference", None),
])
def test_bv01_candidate_a_existing_safety_comparison_not_acquisition(
    candidate_a_material, field, value, bv01_isolation,
):
    metadata, envelope, _, _, _, safety = candidate_a_material
    if field is not None:
        envelope["metadata"][field] = value  # Deliberately mixed synthetic publication.
        assert envelope["metadata"] != metadata
    comparison = {key: envelope["metadata"][key]
                  for key in ("profile_identity", "secret_revision", "account_reference")}
    artifact = safety_parse(safety)
    if field is None:
        artifact.matches_credential_metadata(**comparison)
    else:
        with pytest.raises(DiagnosticDenied, match="metadata mismatch") as error:
            artifact.matches_credential_metadata(**comparison)
        assert DUMMY_ERROR not in str(error.value)
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        artifact.require_accepted_evidence()
    assert bv01_isolation["construction"] == 0


@pytest.mark.parametrize("field", [None, "authorized_host", "runner_sid"])
def test_bv01_candidate_a_existing_authorization_binds_host_and_runner(candidate_a_material, field):
    metadata, _, grant, approved, _, _ = candidate_a_material
    if field is not None:
        metadata[field] = "other-host" if field == "authorized_host" else "S-1-5-21-999"
    facts = controls.DiagnosticExecutionFacts(source_revision=grant.source_revision, clean_tree=True,
        pm_approval_reference=grant.pm_approval_reference, spec_lead_identity=grant.spec_lead_identity,
        runner_identity=metadata["runner_sid"], host_identity=metadata["authorized_host"],
        credential_profile_identity=metadata["profile_identity"], now=datetime.now(timezone.utc))
    kwargs = dict(live_opt_in=True, kind=approved.kind, input_scope_identity=approved.scope_identity,
        commitment=approved.commitment, item_count=1, item_bytes=(1,), item_tokens=(1,),
        payload_bytes=256, safety_basis=grant.bounds_basis,
        conservative_usage_units=1, conservative_cost_micro_units=1)
    if field is None:
        controls.validate_authorization(grant, facts, **kwargs)
    else:
        with pytest.raises(DiagnosticDenied, match="unauthorized"):
            controls.validate_authorization(grant, facts, **kwargs)


@pytest.mark.parametrize("change", ["missing_profile", "missing_revision", "expired", "revoked", "fallback"])
def test_bv01_candidate_a_unusable_safety_still_denies_not_credential_parser(candidate_a_material, change):
    safety = candidate_a_material[-1]
    if change.startswith("missing"):
        del safety["credential_binding"]["profile_identity" if change == "missing_profile" else "secret_revision"]
    elif change == "expired":
        safety["validity"]["expires_at"] = "1999-01-01T00:00:00Z"
    elif change == "revoked":
        safety["lifecycle_state"] = "REVOKED"
    else:
        safety["credential_binding"]["fallback"] = "other-profile"
    with pytest.raises(DiagnosticDenied):
        safety_parse(safety)


@pytest.mark.parametrize("filename", ["credential_metadata.json", "credential_secret.json"])
@pytest.mark.parametrize("unsafe", [None, "owner", "reparse", "write", "delete", "acl", "other_reader", "no_read"])
def test_bv01_candidate_a_existing_da_facts_validator_not_actual_acl(tmp_path, filename, unsafe):
    runner = "S-1-5-21-100"
    facts = controls.WindowsPathFacts("S-1-5-32-544", ((runner, 0x00120089, 0),),
                                     (1, 2), False, False, 0x1)
    changes = {
        "owner": dict(owner_sid=runner), "reparse": dict(is_reparse=True),
        "write": dict(effective_rights=0x3), "delete": dict(effective_rights=0x10001),
        "acl": dict(effective_rights=0x40001),
        "other_reader": dict(allowed_aces=(("S-1-1-0", 0x1, 0),)),
        "no_read": dict(effective_rights=0),
    }
    path = tmp_path / filename  # Pure facts validation; NO file/ACL is provisioned.
    if unsafe is None:
        controls._validate_path_facts(path, facts, runner, directory=False)
    else:
        with pytest.raises(DiagnosticDenied):
            controls._validate_path_facts(path, replace(facts, **changes[unsafe]), runner, directory=False)


@pytest.mark.parametrize("case", ["current", "replaced", "missing", "reader_cap"])
def test_bv01_candidate_a_existing_reader_temporary_dummy_only(tmp_path, candidate_a_material, case):
    # Use the original existing reader ONLY on this pytest-owned temporary file.
    # This is not a fixed credential loader or actual Windows trust observation.
    path = tmp_path / "credential_secret.json"
    body = json.dumps(candidate_a_material[1]).encode()
    path.write_bytes(body if case != "reader_cap" else b"x" * 1_000_001)
    info = path.stat()
    identity = (info.st_dev, info.st_ino + int(case == "replaced"))
    facts = controls.WindowsPathFacts("S-1-5-32-544", (), identity, False, False)
    if case == "missing":
        path.unlink()
    if case == "current":
        assert _existing_trusted_read(path, facts) == body
    else:
        with pytest.raises(DiagnosticDenied) as error:
            _existing_trusted_read(path, facts)
        assert DUMMY_ERROR not in str(error.value)


def test_bv01_candidate_a_generic_reader_is_not_candidate_schema_or_size_validation(tmp_path):
    # Positive evidence of the LIMITATION: generic reader accepts invalid JSON
    # above A01's envelope cap. No fabricated Candidate A validator is added.
    path = tmp_path / "credential_secret.json"
    body = b"x" * 65_537
    path.write_bytes(body)
    info = path.stat()
    facts = controls.WindowsPathFacts("S-1-5-32-544", (), (info.st_dev, info.st_ino), False, False)
    assert _existing_trusted_read(path, facts) == body


@pytest.mark.parametrize("override", ["root", "path", "credential_path", "metadata", "secret", "resolver", "backend"])
def test_bv01_candidate_a_existing_live_loader_rejects_caller_override(override):
    with pytest.raises(TypeError):
        controls.load_live_safety_artifact(**{override: DUMMY_ERROR})
    with pytest.raises(DiagnosticDenied, match="opt-in"):
        diagnostic.run_live()
    assert _NATIVE_EXECUTION_SCOPE is None


@pytest.fixture(autouse=True)
def bv01_isolation(monkeypatch, request):
    """Block effects before any test setup; never read/copy ambient secrets."""
    counts = dict(native=0, network=0, discovery=0, host=0, reservation=0,
                  completion=0, delegation=0, subprocess=0, construction=0)

    native_case = request.node.name.startswith("test_bv01_native_")
    if native_case and _NATIVE_EXECUTION_SCOPE is None:
        pytest.skip("NOT EXECUTED: exact native environment/operations authorization pending")
    if native_case:
        # Only a separately reviewed source change can supply this test-local
        # scope. No CLI flag/environment variable/fixture file creates a grant.
        _native_preflight("preflight")

    def forbidden(kind):
        def deny(*args, **kwargs):
            counts[kind] += 1
            pytest.fail("BV-01 forbids " + kind)
        return deny

    monkeypatch.setattr(os, "environ", {})
    for name in (() if native_case else ("WinDLL", "CDLL", "OleDLL", "PyDLL")):
        monkeypatch.setattr(ctypes, name, forbidden("native"))
    class NoNativeLoader:
        def __getattr__(self, name):
            return forbidden("native")()
    for name in (() if native_case else ("windll", "cdll", "oledll", "pydll")):
        monkeypatch.setattr(ctypes, name, NoNativeLoader())
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden("network"))
    monkeypatch.setattr(socket, "create_connection", forbidden("network"))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden("network"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden("network"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden("network"))
    monkeypatch.setattr(subprocess, "run", forbidden("subprocess"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("subprocess"))
    for name in (() if native_case else ("_programdata_known_folder", "process_runner_sid", "_windows_path_facts", "_read_trusted_file")):
        monkeypatch.setattr(controls, name, forbidden("host"))
    monkeypatch.setattr(controls.TemporaryDiagnosticUsage, "reserve", forbidden("reservation"))
    monkeypatch.setattr(controls.TemporaryDiagnosticUsage, "finish", forbidden("completion"))
    monkeypatch.setattr(adapter._DiagnosticClosedMockTransport, "handle_request", forbidden("delegation"))

    import google.auth
    from google import genai
    monkeypatch.setattr(google.auth, "default", forbidden("discovery"))
    monkeypatch.setattr(google.auth, "load_credentials_from_file", forbidden("discovery"))
    constructor = genai.Client
    def fixed_client(*args, **kwargs):
        counts["construction"] += 1
        assert not args and kwargs["api_key"] == "synthetic-offline-not-a-credential"
        assert kwargs["vertexai"] is False
        options = kwargs["http_options"]
        assert type(options.httpx_client) is adapter._DiagnosticHttpxClient
        assert type(options.httpx_async_client._transport) is adapter._DiagnosticAsyncDenyTransport
        assert not options.httpx_client.trust_env and not options.httpx_async_client.trust_env
        return constructor(**kwargs)
    monkeypatch.setattr(genai, "Client", fixed_client)
    yield counts
    assert all(value == 0 for key, value in counts.items() if key != "construction"), counts


def construction_context(tmp_path):
    prepared, approved = _e4_case(tmp_path, kind="query", count=1)
    # Fixed SYNTHETIC metadata sufficient for constructor failure tests only.
    # No observation/delegation, expected-hash self-approval or token proof.
    material = e4_host_material(prepared.record, approved)
    evidence = replace(controls.DiagnosticSdkByteEvidence.from_json(json.dumps(material).encode()),
                       status="SYNTHETIC_OFFLINE_ONLY")
    return prepared, approved, evidence


@pytest.mark.parametrize("field,value", [
    (None, None), ("profile_identity", "other-profile"), ("profile_identity", None),
    ("secret_revision", "r2"), ("secret_revision", None), ("secret_revision", ""),
    ("account_reference", "other-account"), ("account_reference", None),
])
def test_bv01_mock_existing_credential_metadata_comparison_is_not_a_resolver(bv01_isolation, field, value):
    artifact = safety_parse(safety_case()[3])
    expected = dict(artifact.fields["credential_binding"],
                    account_reference=artifact.fields["pricing_evidence"]["account_reference"])
    if field is None:
        artifact.matches_credential_metadata(**expected)
    else:
        expected[field] = value
        with pytest.raises(DiagnosticDenied, match="metadata mismatch"):
            artifact.matches_credential_metadata(**expected)
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        artifact.require_accepted_evidence()
    assert bv01_isolation["construction"] == 0


@pytest.mark.parametrize("change", ["missing_profile", "missing_revision", "revoked", "expired", "fallback"])
def test_bv01_mock_existing_safety_parser_rejects_unusable_metadata(bv01_isolation, change):
    value = safety_case()[3]
    if change.startswith("missing"):
        del value["credential_binding"]["profile_identity" if change == "missing_profile" else "secret_revision"]
    elif change == "revoked":
        value["lifecycle_state"] = "REVOKED"
    elif change == "expired":
        value["validity"]["expires_at"] = "2001-01-01T00:00:00Z"
    else:
        value["credential_binding"]["fallback_profile"] = "other-profile"
    with pytest.raises(DiagnosticDenied) as error:
        safety_parse(value)
    assert DUMMY_ERROR not in str(error.value)
    assert bv01_isolation["construction"] == 0


@pytest.mark.parametrize("override", ["api_key", "credential_resolver", "credential_path", "backend",
    "transport", "base_url", "sdk_factory", "http_options", "source", "usage_observed"])
def test_bv01_mock_existing_factory_and_live_entry_have_no_credential_override(
    bv01_isolation, tmp_path, override,
):
    prepared, approved, evidence = construction_context(tmp_path)
    with pytest.raises(TypeError):
        diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence, **{override: DUMMY_ERROR})
    with pytest.raises(TypeError):
        diagnostic.run_live(live_opt_in=True, **{override: DUMMY_ERROR})
    with pytest.raises(DiagnosticDenied, match="not provisioned"):
        diagnostic._live_safety(prepared.record, prepared.texts)
    assert bv01_isolation["construction"] == 0


@pytest.mark.parametrize("environment", ["opaque_mapping", "dummy_key", "dummy_debug"])
def test_bv01_mock_existing_factory_denies_ambient_source_without_reading_values(
    bv01_isolation, tmp_path, monkeypatch, environment,
):
    prepared, approved, evidence = construction_context(tmp_path)
    reads = []
    class OpaqueEnvironment:
        def __iter__(self):
            reads.append("iterate")
            pytest.fail("ambient mapping must not be inspected")
        def get(self, *args):
            reads.append("value")
            pytest.fail("ambient value must not be read")
    replacement = OpaqueEnvironment() if environment == "opaque_mapping" else {
        "GOOGLE_API_KEY" if environment == "dummy_key" else "GOOGLE_GENAI_CLIENT_MODE": DUMMY_ERROR}
    isolated = os.environ
    monkeypatch.setattr(os, "environ", replacement)
    try:
        with pytest.raises(DiagnosticDenied, match="isolated") as error:
            diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
        assert DUMMY_ERROR not in str(error.value)
    finally:
        monkeypatch.setattr(os, "environ", isolated)  # Pytest bookkeeping needs its isolated mapping.
    assert reads == [] and bv01_isolation["construction"] == 0


@pytest.mark.parametrize("stage,failures", [
    ("sync", ()), ("async", ()), ("async", ("sync",)), ("sdk", ()),
    ("sdk", ("sync",)), ("sdk", ("async",)), ("sdk", ("sync", "async")),
])
def test_bv01_mock_existing_constructor_failure_cleans_partial_sdk_resources_once(
    bv01_isolation, tmp_path, monkeypatch, capsys, stage, failures,
):
    from google import genai
    prepared, approved, evidence = construction_context(tmp_path)
    sync_type, async_type = adapter._DiagnosticHttpxClient, httpx.AsyncClient
    events, acquired = [], []
    def sync(guard):
        events.append("sync acquisition")
        if stage == "sync":
            raise RuntimeError(DUMMY_ERROR)
        resource = sync_type(guard)
        acquired.append(resource)
        real_close = resource.close
        def close():
            events.append("sync cleanup")
            real_close()
            if "sync" in failures:
                raise RuntimeError(DUMMY_ERROR)
        resource.close = close
        return resource
    def async_client(**kwargs):
        events.append("async acquisition")
        if stage == "async":
            raise RuntimeError(DUMMY_ERROR)
        resource = async_type(**kwargs)
        acquired.append(resource)
        real_close = resource.aclose
        async def close():
            events.append("async cleanup")
            await real_close()
            if "async" in failures:
                raise RuntimeError(DUMMY_ERROR)
        resource.aclose = close
        return resource
    def constructor(**kwargs):
        bv01_isolation["construction"] += 1
        events.append("sdk constructor")
        raise RuntimeError(DUMMY_ERROR)
    monkeypatch.setattr(adapter, "_DiagnosticHttpxClient", sync)
    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(genai, "Client", constructor)
    with pytest.raises(DiagnosticDenied) as error:
        diagnostic._create_diagnostic_offline_sdk_client(prepared, approved, evidence)
    output = capsys.readouterr()
    assert DUMMY_ERROR not in str(error.value) + output.out + output.err
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert events.count("sync acquisition") == 1
    assert events.count("async acquisition") == int(stage != "sync")
    assert events.count("sdk constructor") == bv01_isolation["construction"] == int(stage == "sdk")
    assert events.count("sync cleanup") == int(stage != "sync")
    assert events.count("async cleanup") == int(stage == "sdk")
    assert all(resource.is_closed for resource in acquired)


@pytest.mark.parametrize("failure", [None, "sdk", "sync", "async"])
def test_bv01_mock_existing_sdk_handle_cleanup_does_not_retry_or_delegate(
    bv01_isolation, tmp_path, monkeypatch, capsys, failure,
):
    handle = diagnostic._create_diagnostic_offline_sdk_client(*construction_context(tmp_path))
    counts = dict(sdk=0, sync=0, async_=0)
    for resource, name in ((handle._sdk, "sdk"), (handle._sync, "sync")):
        close = resource.close
        def closing(close=close, name=name):
            counts[name] += 1
            close()
            if name == failure:
                raise RuntimeError(DUMMY_ERROR)
        monkeypatch.setattr(resource, "close", closing)
    async_close = handle._async.aclose
    async def closing_async():
        counts["async_"] += 1
        await async_close()
        if failure == "async":
            raise RuntimeError(DUMMY_ERROR)
    monkeypatch.setattr(handle._async, "aclose", closing_async)
    for _ in range(2):
        if failure is None:
            handle.close()
        else:
            with pytest.raises(DiagnosticDenied, match="cleanup failed") as error:
                handle.close()
            assert DUMMY_ERROR not in str(error.value)
    assert counts == dict(sdk=1, sync=1, async_=1)
    assert handle._guard.delegations == 0 and handle._guard._delegate.observations == []
    assert bv01_isolation["construction"] == 1
    output = capsys.readouterr()
    assert DUMMY_ERROR not in output.out + output.err


# Official API references (code preparation only; NOT native observations):
# https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
# https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata
# https://learn.microsoft.com/en-us/windows/win32/api/wincrypt/ns-wincrypt-crypt_integer_blob
# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-localfree
# https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/dpapi.h
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4
_DUMMY_PLAINTEXT = b"BV01-SYNTHETIC-DUMMY-ONLY-NOT-A-CREDENTIAL"
_DUMMY_BLOB_CAP = 1_000_000  # Test-only I/O cap, NOT a production blob/schema bound.


class _DataBlob(ctypes.Structure):
    # DWORD is 32 bits on both Win32 and Win64. Natural pointer alignment; no pack.
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _NativeDenied(ValueError):
    """Bounded test failure; never include plaintext, blob or raw OS exceptions."""


@dataclass(frozen=True)
class _NativeScope:
    """Test-local execution checklist, NOT a credential/PM authority artifact.

    A future SPEC Lead execution approval must be recorded in a reviewed source
    change, including exact operations. Environment/test selection never fills it.
    """
    approval_reference: str
    host: str
    actor_sid: str
    role: str
    fixture_root: str
    expires_at: datetime
    operations: frozenset[str]
    foreign_host: str | None = None
    foreign_blob_sha256: str | None = None


_NATIVE_EXECUTION_SCOPE: _NativeScope | None = None  # NO execution authorized.
_NATIVE_OPERATIONS = frozenset(("preflight", "bind", "protect", "unprotect", "acl_read", "foreign_read"))


def _native_scope(operation):
    scope = _NATIVE_EXECUTION_SCOPE
    if (type(scope) is not _NativeScope
            or any(type(v) is not str or not v or v != v.strip() for v in (
                scope.approval_reference, scope.host, scope.actor_sid, scope.fixture_root))
            or not scope.actor_sid.startswith("S-1-")
            or type(scope.operations) is not frozenset or not scope.operations <= _NATIVE_OPERATIONS
            or operation not in scope.operations or scope.role not in ("publisher", "runner", "unauthorized")
            or type(scope.expires_at) is not datetime or scope.expires_at.tzinfo is None
            or datetime.now(timezone.utc) >= scope.expires_at):
        raise _NativeDenied("native execution scope unavailable")
    return scope


def _native_preflight(operation):
    scope = _native_scope(operation)  # FIRST: no OS/host/env read before scope check.
    try:
        return _native_preflight_facts(scope, operation)
    except _NativeDenied:
        raise
    except Exception:
        raise _NativeDenied("native preflight unavailable") from None


def _native_preflight_facts(scope, operation):
    if _native_scope(operation) is not scope:
        raise _NativeDenied("native scope substitution denied")
    if os.name != "nt" or socket.gethostname() != scope.host:
        raise _NativeDenied("native host mismatch")
    with controls._effective_token() as (_, facts):
        if facts.user_sid != scope.actor_sid:
            raise _NativeDenied("native actor mismatch")
        if scope.role == "publisher":
            if not facts.elevated or not any(sid == "S-1-5-32-544" and flags & 4 for sid, flags in facts.groups):
                raise _NativeDenied("native publisher identity unavailable")
        else:
            controls._validate_token_facts(facts, scope.actor_sid)
    root = Path(scope.fixture_root)
    repo = Path(__file__).resolve().parents[1]
    formal = controls._programdata_known_folder() / "AIOps" / "Diagnostic"
    if (not root.is_absolute() or root == Path(root.anchor) or str(root).startswith("\\\\")
            or root != root.resolve(strict=True) or not root.is_dir()
            or root == repo or repo in root.parents or root == formal or formal in root.parents
            or root == controls.fixed_host_paths().root or controls.fixed_host_paths().root in root.parents):
        raise _NativeDenied("native fixture root unavailable")
    if operation in ("protect", "unprotect", "bind") and scope.role == "unauthorized":
        raise _NativeDenied("native operation not permitted for actor")
    return scope


@dataclass
class _DpapiApi:
    protect: object
    unprotect: object
    free: object
    last_error: object
    wipe: object
    copy: object
    calls: dict = field(default_factory=lambda: dict(protect=0, unprotect=0, free=0, wipe=0))


def _declare_dpapi_abi(crypt32, kernel32):
    """Pure declaration, also inspected with Python call doubles (no DLL load)."""
    pointer = ctypes.POINTER(_DataBlob)
    common = [pointer, ctypes.c_wchar_p, pointer, ctypes.c_void_p, ctypes.c_void_p,
              ctypes.c_uint32, pointer]
    crypt32.CryptProtectData.argtypes = common
    crypt32.CryptUnprotectData.argtypes = [pointer, ctypes.POINTER(ctypes.c_wchar_p), *common[2:]]
    crypt32.CryptProtectData.restype = crypt32.CryptUnprotectData.restype = ctypes.c_int32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p


def _load_native_api():
    _native_preflight("bind")
    try:
        # LOAD_LIBRARY_SEARCH_SYSTEM32: no caller DLL path/search override.
        crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True, winmode=0x800)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
        _declare_dpapi_abi(crypt32, kernel32)
        # SecureZeroMemory is header-inline, not an assumed DLL export. Explicit
        # runtime memset clears only owned buffers; no full Python zeroize claim.
        return _DpapiApi(crypt32.CryptProtectData, crypt32.CryptUnprotectData, kernel32.LocalFree,
                         ctypes.get_last_error, ctypes.memset, ctypes.string_at)
    except Exception:
        raise _NativeDenied("native API binding unavailable") from None


def _dpapi_once(api, operation, payload):
    _native_preflight(operation)
    if (operation not in ("protect", "unprotect") or type(payload) is not bytes
            or not 0 < len(payload) <= _DUMMY_BLOB_CAP
            or (operation == "protect" and payload != _DUMMY_PLAINTEXT)):
        raise _NativeDenied("native dummy input invalid")
    owned_input = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    source = _DataBlob(len(payload), ctypes.cast(owned_input, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()  # NULL/zero before call, including partial-failure cleanup.
    failed_cleanup = False
    try:
        call = api.protect if operation == "protect" else api.unprotect
        flags = _CRYPTPROTECT_UI_FORBIDDEN | (_CRYPTPROTECT_LOCAL_MACHINE if operation == "protect" else 0)
        # No prompt, description, entropy or reserved payload. In particular no
        # LPWSTR output description is requested/allocated by Unprotect.
        api.calls[operation] += 1
        if not call(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(output)):
            code = api.last_error()  # Capture before cleanup can change last error.
            raise _NativeDenied("native call failed; code=" + str(int(code) & 0xFFFFFFFF))
        if not output.pbData or not 0 < output.cbData <= _DUMMY_BLOB_CAP:
            raise _NativeDenied("native output invalid")
        if operation == "protect":
            return api.copy(output.pbData, output.cbData)  # Dummy ciphertext only.
        # Never copy/return plaintext as an immutable Python bytes/string.
        if (output.cbData != len(_DUMMY_PLAINTEXT)
                or any(output.pbData[i] != value for i, value in enumerate(_DUMMY_PLAINTEXT))):
            raise _NativeDenied("native dummy plaintext mismatch")
        return True
    except _NativeDenied:
        raise
    except Exception:
        raise _NativeDenied("native call unavailable") from None
    finally:
        # Cleanup is not re-gated on expiry; already-acquired obligations remain.
        if output.pbData:
            try:
                if output.cbData > _DUMMY_BLOB_CAP:
                    raise _NativeDenied("native output wipe bound unavailable")
                api.calls["wipe"] += 1
                api.wipe(output.pbData, 0, output.cbData)
            except BaseException:
                failed_cleanup = True
            try:
                api.calls["free"] += 1
                if api.free(ctypes.cast(output.pbData, ctypes.c_void_p)):
                    failed_cleanup = True
            except BaseException:
                failed_cleanup = True
        try:
            api.calls["wipe"] += 1
            api.wipe(owned_input, 0, len(owned_input))
        except BaseException:
            failed_cleanup = True
        if failed_cleanup:
            raise _NativeDenied("native cleanup failed; no retry") from None


def _native_fixture_bytes(operation, filename):
    scope = _native_preflight(operation)
    # Only these test-specific fixed names. Never search, provision or read a
    # formal credential file. Runtime fixtures require separate authorization.
    if filename not in ("dummy.blob", "foreign-dummy.blob"):
        raise _NativeDenied("native fixture filename unavailable")
    path = Path(scope.fixture_root) / "authority" / filename
    if operation == "foreign_read" and (not scope.foreign_host or scope.foreign_host == scope.host
            or type(scope.foreign_blob_sha256) is not str or len(scope.foreign_blob_sha256) != 64
            or any(c not in "0123456789abcdef" for c in scope.foreign_blob_sha256)):
        raise _NativeDenied("native foreign fixture evidence unavailable")
    try:
        # Observe all fixture ancestors and opened identity; no provisioning.
        before = {p: p.lstat() for p in (Path(scope.fixture_root), path.parent, path)}
        if any(s.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT for s in before.values()):
            raise _NativeDenied("native fixture reparse denied")
        with path.open("rb") as opened:
            info = os.fstat(opened.fileno())
            expected = before[path]
            if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino) or not stat.S_ISREG(info.st_mode):
                raise _NativeDenied("native fixture opened identity mismatch")
            data = opened.read(_DUMMY_BLOB_CAP + 1)
        for p, info in before.items():
            after = p.lstat()
            if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                raise _NativeDenied("native fixture replacement denied")
        if not 0 < len(data) <= _DUMMY_BLOB_CAP:
            raise _NativeDenied("native fixture size invalid")
        if operation == "foreign_read" and sha256(data).hexdigest() != scope.foreign_blob_sha256:
            raise _NativeDenied("native foreign fixture mismatch")
        return data
    except PermissionError:
        raise _NativeDenied("native fixture permission denied") from None
    except OSError:
        raise _NativeDenied("native fixture unavailable") from None


def test_bv01_native_dummy_roundtrip():
    api = _load_native_api()
    blob = _dpapi_once(api, "protect", _DUMMY_PLAINTEXT)
    if _dpapi_once(api, "unprotect", blob) is not True:
        raise _NativeDenied("native roundtrip not confirmed")
    assert api.calls == dict(protect=1, unprotect=1, free=2, wipe=4)


def test_bv01_native_corrupted_blob_observation(record_property):
    api = _load_native_api()
    blob = _dpapi_once(api, "protect", _DUMMY_PLAINTEXT)
    # Do not assume a particular error code (or that every bit flip fails).
    damaged = bytearray(blob)
    damaged[len(damaged) // 2] ^= 0x80
    try:
        _dpapi_once(api, "unprotect", bytes(damaged))
    except _NativeDenied as error:
        if str(error).startswith("native call failed;") or str(error) == "native dummy plaintext mismatch":
            record_property("bv01_corruption_disposition", str(error))
            record_property("bv01_call_counts", dict(api.calls))
            return
        raise  # Cleanup/binding failures are NOT corruption acceptance evidence.
    pytest.fail("corruption not detected for this sample; backend evidence incomplete")


def _native_runner_acl_read(scope):
    if _native_preflight("acl_read") is not scope:
        raise _NativeDenied("native scope substitution denied")
    root = Path(scope.fixture_root)
    with controls._effective_token() as (token, _):
        for path, directory in ((root, True), (root / "authority", True), (root / "authority" / "dummy.blob", False)):
            facts = controls._windows_path_facts(path, token)
            controls._validate_path_facts(path, facts, scope.actor_sid, directory=directory)
    return _native_fixture_bytes("acl_read", "dummy.blob")


def test_bv01_native_runner_actual_acl_read():
    scope = _native_preflight("acl_read")
    if scope.role != "runner":
        pytest.skip("NOT EXECUTED: runner primary-token context required")
    _native_runner_acl_read(scope)


def test_bv01_native_runner_decrypts_publisher_dummy():
    scope = _native_preflight("acl_read")
    if scope.role != "runner":
        pytest.skip("NOT EXECUTED: separately provisioned runner context required")
    blob = _native_runner_acl_read(scope)
    if _dpapi_once(_load_native_api(), "unprotect", blob) is not True:
        raise _NativeDenied("native runner dummy result unconfirmed")


def test_bv01_native_unauthorized_principal_read_denied():
    scope = _native_preflight("acl_read")
    if scope.role != "unauthorized":
        pytest.skip("NOT EXECUTED: separate unauthorized primary-token context required")
    with pytest.raises(_NativeDenied, match="fixture permission denied"):
        _native_fixture_bytes("acl_read", "dummy.blob")
    # No attempt to decrypt a blob that the principal was denied access to.


def test_bv01_native_wrong_host_dummy_fixture(record_property):
    blob = _native_fixture_bytes("foreign_read", "foreign-dummy.blob")
    api = _load_native_api()
    try:
        _dpapi_once(api, "unprotect", blob)
    except _NativeDenied as error:
        if str(error).startswith("native call failed;") or str(error) == "native dummy plaintext mismatch":
            record_property("bv01_foreign_disposition", str(error))
            record_property("bv01_call_counts", dict(api.calls))
            return
        raise
    pytest.fail("foreign fixture decrypted; machine binding evidence incomplete")


def test_bv01_native_rotation_revocation_replacement_preconditions():
    # Native metadata publication/rotation format is deliberately not invented.
    # The existing mock parser/comparison tests do NOT fill this evidence gap.
    pytest.skip("NOT EXECUTED: exact publication/rotation/revocation/replacement protocol pending")


def _guard_fixture_scope(**changes):
    # Never assigned to the production/test execution constant except isolated
    # monkeypatch tests; this is NOT an approved Windows execution environment.
    values = dict(approval_reference="MOCK-NOT-A-GRANT", host="mock-host", actor_sid="S-1-5-21-100",
                  role="runner", fixture_root="C:\\BV01-UNPROVISIONED", expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
                  operations=frozenset(("preflight", "bind", "protect", "unprotect")))
    return _NativeScope(**(values | changes))


@pytest.mark.parametrize("entry", ["preflight", "bind", "protect", "unprotect", "fixture", "host_facts", "acl_reader"])
def test_bv01_guard_no_scope_denies_before_native_or_host_entry(bv01_isolation, entry):
    assert _NATIVE_EXECUTION_SCOPE is None
    calls = dict(preflight=lambda: _native_preflight("preflight"), bind=_load_native_api,
                 protect=lambda: _dpapi_once(None, "protect", _DUMMY_PLAINTEXT),
                 unprotect=lambda: _dpapi_once(None, "unprotect", b"dummy"),
                 fixture=lambda: _native_fixture_bytes("foreign_read", "foreign-dummy.blob"),
                 host_facts=lambda: _native_preflight_facts(_guard_fixture_scope(), "bind"),
                 acl_reader=lambda: _native_runner_acl_read(_guard_fixture_scope()))
    with pytest.raises(_NativeDenied, match="scope unavailable"):
        calls[entry]()
    assert bv01_isolation["native"] == bv01_isolation["host"] == 0


@pytest.mark.parametrize("name", ["RUN_BV01_NATIVE", "ALLOW_DPAPI", "BV01_APPROVED"])
def test_bv01_guard_environment_and_test_selection_do_not_authorize(bv01_isolation, monkeypatch, name):
    monkeypatch.setenv(name, "1")  # Synthetic mapping only, never ambient credentials.
    with pytest.raises(_NativeDenied, match="scope unavailable"):
        _load_native_api()
    with pytest.raises(TypeError):
        _load_native_api(scope=_guard_fixture_scope())
    assert _NATIVE_EXECUTION_SCOPE is None and bv01_isolation["native"] == 0


@pytest.mark.parametrize("change", [
    {"approval_reference": None}, {"approval_reference": ""}, {"actor_sid": "wrong"},
    {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
    {"expires_at": datetime(2099, 1, 1)}, {"role": "anyone"},
    {"operations": frozenset(("protect", "fallback"))}, {"operations": ("protect",)},
    {"operations": frozenset(("preflight",))},
])
def test_bv01_guard_invalid_expired_or_wrong_operation_scope_denies(bv01_isolation, monkeypatch, change):
    monkeypatch.setattr(sys.modules[__name__], "_NATIVE_EXECUTION_SCOPE", _guard_fixture_scope(**change))
    with pytest.raises(_NativeDenied, match="scope unavailable"):
        _native_preflight("protect")
    assert bv01_isolation["native"] == bv01_isolation["host"] == 0


@pytest.mark.parametrize("mismatch", ["host", "actor", "relative_root"])
def test_bv01_guard_mock_context_mismatch_cannot_reach_dpapi(bv01_isolation, monkeypatch, mismatch):
    module = sys.modules[__name__]
    scope = _guard_fixture_scope(**({"fixture_root": "relative"} if mismatch == "relative_root" else {}))
    monkeypatch.setattr(module, "_NATIVE_EXECUTION_SCOPE", scope)
    monkeypatch.setattr(socket, "gethostname", lambda: "different-host" if mismatch == "host" else scope.host)
    token_entries = []
    @contextmanager
    def token():
        token_entries.append(True)
        yield 0, controls.WindowsTokenFacts("S-1-5-21-999" if mismatch == "actor" else scope.actor_sid,
            (("S-1-5-32-545", 7),), (), (("SeChangeNotifyPrivilege", 2),), False)
    monkeypatch.setattr(controls, "_effective_token", token)
    monkeypatch.setattr(controls, "_programdata_known_folder", lambda: Path("C:/BV01-MOCK-PROGRAMDATA"))
    with pytest.raises(_NativeDenied):
        _load_native_api()
    assert len(token_entries) == int(mismatch != "host") and bv01_isolation["native"] == 0


def test_bv01_guard_abi_declaration_has_no_library_load_or_function_call(bv01_isolation):
    crypt = SimpleNamespace(CryptProtectData=SimpleNamespace(), CryptUnprotectData=SimpleNamespace())
    kernel = SimpleNamespace(LocalFree=SimpleNamespace())
    _declare_dpapi_abi(crypt, kernel)
    pointer = ctypes.POINTER(_DataBlob)
    assert crypt.CryptProtectData.argtypes == [pointer, ctypes.c_wchar_p, pointer, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_uint32, pointer]
    assert crypt.CryptUnprotectData.argtypes[1] == ctypes.POINTER(ctypes.c_wchar_p)
    assert crypt.CryptProtectData.restype is crypt.CryptUnprotectData.restype is ctypes.c_int32
    assert kernel.LocalFree.argtypes == [ctypes.c_void_p] and kernel.LocalFree.restype is ctypes.c_void_p
    alignment = ctypes.alignment(ctypes.c_void_p)
    expected_offset = (4 + alignment - 1) // alignment * alignment
    assert _DataBlob.cbData.offset == 0 and _DataBlob.pbData.offset == expected_offset
    assert ctypes.sizeof(_DataBlob) == expected_offset + ctypes.sizeof(ctypes.c_void_p)
    assert (_CRYPTPROTECT_UI_FORBIDDEN, _CRYPTPROTECT_LOCAL_MACHINE) == (1, 4)
    assert bv01_isolation["native"] == 0


@pytest.mark.parametrize("operation,mode", [
    ("protect", "success"), ("unprotect", "success"), ("unprotect", "plaintext_mismatch"),
    ("protect", "false_no_output"), ("protect", "false_partial_output"),
    ("protect", "exception_partial_output"), ("protect", "copy_failure"),
    ("protect", "wipe_failure"), ("protect", "free_failure"), ("protect", "free_exception"),
    ("protect", "oversized_output"),
])
def test_bv01_guard_mock_interop_flags_failure_and_cleanup_are_not_native_evidence(
    bv01_isolation, monkeypatch, operation, mode, capsys,
):
    # Exercise the actual harness's call/cleanup code with Python call doubles
    # and Python-owned ctypes arrays. NO Win32 allocator or crypto API is used.
    preflights, events, held = [], [], []
    monkeypatch.setattr(sys.modules[__name__], "_native_preflight", lambda op: preflights.append(op))
    def call(source, description, entropy, reserved, prompt, flags, output):
        events.append("call")
        assert description is entropy is reserved is prompt is None
        assert flags == (5 if operation == "protect" else 1)
        incoming = ctypes.cast(source, ctypes.POINTER(_DataBlob)).contents
        assert incoming.cbData > 0 and incoming.pbData
        if mode != "false_no_output":
            data = b"dummy-ciphertext" if operation == "protect" else _DUMMY_PLAINTEXT
            if mode == "plaintext_mismatch":
                data = b"wrong-dummy"
            array = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            held.append(array)
            target = ctypes.cast(output, ctypes.POINTER(_DataBlob)).contents
            target.cbData = _DUMMY_BLOB_CAP + 1 if mode == "oversized_output" else len(data)
            target.pbData = ctypes.cast(array, ctypes.POINTER(ctypes.c_ubyte))
        if mode == "exception_partial_output":
            raise RuntimeError(DUMMY_ERROR)
        return not mode.startswith("false_")
    def wipe(pointer, value, count):
        events.append("wipe")
        assert value == 0 and 0 <= count <= _DUMMY_BLOB_CAP
        if mode == "wipe_failure" and events.count("wipe") == 1:
            raise RuntimeError(DUMMY_ERROR)
        target = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ubyte))
        for i in range(count):
            target[i] = 0
    def free(pointer):
        events.append("free")
        assert pointer.value
        if mode not in ("wipe_failure", "oversized_output"):
            assert all(value == 0 for value in held[0])
        if mode == "free_exception":
            raise RuntimeError(DUMMY_ERROR)
        return 1 if mode == "free_failure" else None
    def copy(pointer, count):
        events.append("copy")
        assert operation == "protect"  # Plaintext must never use this copy path.
        if mode == "copy_failure":
            raise RuntimeError(DUMMY_ERROR)
        return bytes(pointer[i] for i in range(count))
    def last_error():
        events.append("last_error")
        return 13
    api = _DpapiApi(call, call, free, last_error, wipe, copy)
    payload = _DUMMY_PLAINTEXT if operation == "protect" else b"dummy-ciphertext"
    if mode == "success":
        result = _dpapi_once(api, operation, payload)
        assert result == (b"dummy-ciphertext" if operation == "protect" else True)
    else:
        with pytest.raises(_NativeDenied) as error:
            _dpapi_once(api, operation, payload)
        assert DUMMY_ERROR not in str(error.value)
        if mode.startswith("false_"):
            assert str(error.value) == "native call failed; code=13"
    assert preflights == [operation] and events.count("call") == 1
    assert api.calls[operation] == 1 and api.calls["free"] == int(mode != "false_no_output")
    assert events.count("free") == api.calls["free"]
    assert events.count("last_error") == int(mode.startswith("false_"))
    if mode.startswith("false_"):
        assert events.index("last_error") < events.index("wipe")
    assert api.calls["wipe"] == (1 if mode in ("false_no_output", "oversized_output") else 2)
    if "free" in events and mode not in ("wipe_failure", "oversized_output"):
        assert events.index("wipe") < events.index("free") < len(events) - 1
    output = capsys.readouterr()
    assert DUMMY_ERROR not in output.out + output.err and bv01_isolation["native"] == 0
