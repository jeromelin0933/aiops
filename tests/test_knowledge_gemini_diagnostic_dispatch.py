"""Exercise the production diagnostic entry with ONLY fake authority and I/O.

Token bounds/prices/approvals here are synthetic fixtures, not live evidence.
The offline_sdk fixture blocks sockets, DNS, ADC and real HTTP transports.
"""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from hashlib import sha256
import json
import logging
from pathlib import Path

import httpx
import pytest

import knowledge_index.gemini_diagnostic as diagnostic
import knowledge_index.gemini_diagnostic_controls as controls
from test_knowledge_gemini_diagnostic import _e4_case, _host_e4_observation
from test_knowledge_gemini_diagnostic_controls import facts, ledger_rows, safety_case, safety_parse
from test_knowledge_gemini_embedding2_serialization import offline_sdk


@pytest.fixture
def campaign(offline_sdk, tmp_path, monkeypatch):
    def make(kind="query", count=1, record_changes=None):
        original, approved = _e4_case(tmp_path, kind, count)
        record = replace(original.record, bounds_basis="fixture-approved-bounds",
            cost_basis="fixture-approved-price", valid_from="2000-01-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00")
        # Real SDK serialization is observed over the sealed offline transport
        # before the target request; no hash is learned from its outbound body.
        prepared = diagnostic._preflight(load_record=lambda: record,
            resolve_facts=lambda *_: facts(credential_profile_identity=record.credential_profile_identity),
            safety=lambda r, texts: (diagnostic._offline_proof(r),
                diagnostic._offline_safety_basis(r, texts),
                sum(len(t.encode()) for t in texts), 100),
            request=original_request(original, kind, tmp_path, count),
            live_opt_in=True, offline_harness=True)
        evidence, _ = _host_e4_observation(prepared, approved)
        if record_changes:
            record = replace(record, **record_changes)
            evidence = replace(evidence, authorization_record_sha256=controls.authorization_record_digest(record))
        value = safety_case()[3]
        value["binding"].update(campaign_id=record.campaign_id,
            authorization_record_sha256=controls.authorization_record_digest(record),
            source_revision=record.source_revision, kind=kind,
            input_scope_identity=record.input_scope_identity, input_commitment=record.input_commitment,
            bounds_basis=record.bounds_basis, cost_basis=record.cost_basis)
        value["credential_binding"].update(profile_identity=record.credential_profile_identity)
        value["e4_reference"].update(reference=evidence.reference,
            sdk_body_sha256=evidence.sdk_body_sha256, sdk_body_bytes=evidence.sdk_body_bytes,
            canonical_json_sha256=evidence.canonical_json_sha256)
        bounds = {sha256(text.encode()).hexdigest(): len(text.encode()) + 16 for text in prepared.texts}
        value["accepted_input_token_bounds"] = bounds
        value["token_evidence"].update(method_id="APPROVED_FIXED_INPUT_UPPER_BOUNDS_V1",
            implementation_sha256=sha256(Path(controls.__file__).read_bytes()).hexdigest(),
            data_sha256=sha256(json.dumps(bounds, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        artifact = safety_parse(value)
        binding = controls.LiveHostBinding(record, approved, None, record.runner_identity, evidence)
        monkeypatch.setattr(diagnostic, "load_live_binding", lambda: binding)
        monkeypatch.setattr(diagnostic, "load_live_safety_artifact", lambda: artifact)
        monkeypatch.setattr(diagnostic, "resolve_repository_source", lambda _: (record.source_revision, True))
        monkeypatch.setattr(diagnostic.socket, "gethostname", lambda: record.authorized_host)
        usage = controls.TemporaryDiagnosticUsage(tmp_path / "attempts.sqlite")
        monkeypatch.setattr(diagnostic, "LiveDiagnosticUsage", lambda: usage)
        monkeypatch.setitem(controls.os.environ, "GEMINI_CREDENTIAL_PROFILE", record.credential_profile_identity)
        monkeypatch.setitem(controls.os.environ, "GEMINI_CREDENTIAL_REVISION", "r1")
        monkeypatch.setitem(controls.os.environ, "GEMINI_API_KEY", "synthetic-dispatch-key")
        return prepared, binding, artifact, usage
    return make


def original_request(prepared, kind, tmp_path, count):
    # Reuse deterministic request fixtures without supplying a fake provider to
    # the production runner. Public run_live takes the normal typed request.
    from test_knowledge_gemini_diagnostic import request_and_record
    return request_and_record(tmp_path, kind=kind, count=count)[0]


def mock_transport(monkeypatch, tmp_path, *, status=200, mode="ok"):
    calls = []
    def handle(request):
        rows = ledger_rows(tmp_path / "attempts.sqlite")
        assert rows[0][1] == "IN_FLIGHT"  # committed before physical dispatch
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic-dispatch-key", request=request)
        if mode == "network":
            raise httpx.ConnectError("synthetic-dispatch-key", request=request)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": status,
                "status": "RESOURCE_EXHAUSTED", "message": "synthetic-dispatch-key"}})
        n = len(json.loads(request.content)["requests"])
        if mode == "count":
            n -= 1
        vectors = [[1.0] + [0.0] * 767 for _ in range(n)]
        if mode == "dimension":
            vectors[0].pop()
        if mode == "normalization":
            vectors[0][0] = 2.0
        body = {} if mode == "malformed" else {"embeddings": [{"values": v} for v in vectors]}
        return httpx.Response(200, json=body)
    monkeypatch.setattr(diagnostic, "_create_live_transport", lambda: httpx.MockTransport(handle))
    return calls


@pytest.mark.parametrize("kind,count", [("query", 1), ("document", 54)])
def test_controlled_entry_dispatches_once_after_persisted_attempt(campaign, monkeypatch, tmp_path, kind, count):
    prepared, binding, artifact, usage = campaign(kind, count)
    calls = mock_transport(monkeypatch, tmp_path)
    request = original_request(prepared, kind, tmp_path, count)
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.outcome == "SUCCESS" and result.vector_count == count
    assert result.category == "OBSERVED_MAPPING_REVIEW_REQUIRED"  # not Provider PASS
    assert result.provider_attempts == len(calls) == 1
    assert ledger_rows(tmp_path / "attempts.sqlite")[0][1] == "SUCCESS"
    assert ledger_rows(tmp_path / "attempts.sqlite")[0][-1] == "UNKNOWN"
    with pytest.raises(controls.DiagnosticDenied, match="human review"):
        diagnostic.run_live(request, live_opt_in=True)
    assert len(calls) == 1  # no auto-resend or budget refund


@pytest.mark.parametrize("mode,status,outcome", [
    ("timeout", 200, "TIMEOUT"), ("network", 200, "UNKNOWN"),
    ("ok", 429, "FAILED"), ("ok", 401, "FAILED"),
    ("count", 200, "FAILED"), ("dimension", 200, "FAILED"),
    ("normalization", 200, "FAILED"), ("malformed", 200, "FAILED"),
])
def test_failures_are_secret_safe_and_never_resend(campaign, monkeypatch, tmp_path, caplog, mode, status, outcome):
    prepared, _, _, _ = campaign()
    calls = mock_transport(monkeypatch, tmp_path, status=status, mode=mode)
    caplog.set_level(logging.DEBUG)
    result = diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert result.outcome == outcome
    assert result.provider_attempts == len(calls) == 1
    if status == 429:
        assert result.category == "provider_quota"
    if mode in ("count", "dimension", "normalization", "malformed"):
        assert result.category == "INVALID_PROVIDER_RESPONSE"
    assert "synthetic-dispatch-key" not in repr(result) + caplog.text
    assert "synthetic-dispatch-key".encode() not in (tmp_path / "attempts.sqlite").read_bytes()
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["dirty", "source", "host", "profile", "model", "input", "expired", "missing_key", "bad_key", "revision", "token_bound"])
def test_rejection_before_transport(campaign, monkeypatch, tmp_path, change):
    prepared, binding, artifact, _ = campaign()
    request = original_request(prepared, "query", tmp_path, 1)
    calls = mock_transport(monkeypatch, tmp_path)
    if change in ("dirty", "source"):
        monkeypatch.setattr(diagnostic, "resolve_repository_source", lambda _: (
            "a" * 40 if change == "source" else binding.record.source_revision, change != "dirty"))
    elif change == "host":
        monkeypatch.setattr(diagnostic.socket, "gethostname", lambda: "wrong-host")
    elif change == "profile":
        request = replace(request, capability=replace(request.capability,
            profile_reference=replace(request.capability.profile_reference, value="different-profile")))
    elif change == "model":
        request = replace(request, capability=replace(request.capability, model="wrong-model"))
    elif change == "input":
        request = replace(request, query=replace(request.query, text="changed input"))
    elif change == "expired":
        monkeypatch.setattr(diagnostic, "load_live_binding", lambda: replace(binding,
            record=replace(binding.record, valid_from="1999-01-01T00:00:00Z", expires_at="2000-01-01T00:00:00Z")))
    elif change == "missing_key":
        monkeypatch.delitem(controls.os.environ, "GEMINI_API_KEY")
    elif change == "bad_key":
        monkeypatch.setitem(controls.os.environ, "GEMINI_API_KEY", "bad\nsynthetic-dispatch-key")
    elif change == "revision":
        monkeypatch.setitem(controls.os.environ, "GEMINI_CREDENTIAL_REVISION", "wrong")
    else:
        monkeypatch.setattr(diagnostic, "load_live_binding", lambda: replace(binding,
            record=replace(binding.record, maximum_item_tokens=1)))
    with pytest.raises(controls.DiagnosticDenied) as caught:
        diagnostic.run_live(request, live_opt_in=True)
    assert "synthetic-dispatch-key" not in str(caught.value)
    assert calls == []
    rows = ledger_rows(tmp_path / "attempts.sqlite")
    if change in ("missing_key", "bad_key", "revision"):
        assert len(rows) == 1 and rows[0][1] == "FAILED" and rows[0][-1] == "UNKNOWN"
        assert rows[0][3] > 0 and rows[0][4] > 0
    else:
        assert rows == []


def test_default_denial_never_loads_authority_or_credentials(offline_sdk, monkeypatch):
    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: pytest.fail("authority must not be accessed"))
    with pytest.raises(controls.DiagnosticDenied, match="opt-in"):
        diagnostic.run_live()


@pytest.mark.parametrize("limits", [
    {"maximum_item_tokens": 1}, {"maximum_payload_bytes": 1},
    {"maximum_usage_units": 1}, {"maximum_cost_micro_units": 1},
])
def test_consistently_bound_limits_reject_before_credential_access(campaign, monkeypatch, tmp_path, limits):
    prepared, _, _, _ = campaign(record_changes=limits)
    calls = mock_transport(monkeypatch, tmp_path)
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", lambda *_: pytest.fail("credential accessed"))
    # Synthetic per-token price is raised for a meaningful cost-cap failure.
    if "maximum_cost_micro_units" in limits:
        original_safety = diagnostic._live_safety
        def safety(record, texts):
            proof, basis, usage, cost = original_safety(record, texts)
            return proof, replace(basis, price_micro_units_per_million_tokens=1_000_000), usage, usage
        monkeypatch.setattr(diagnostic, "_live_safety", safety)
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert calls == [] and ledger_rows(tmp_path / "attempts.sqlite") == []


def test_concurrent_runner_cannot_dispatch_while_first_is_in_flight(campaign, monkeypatch, tmp_path):
    prepared, _, _, _ = campaign()
    request = original_request(prepared, "query", tmp_path, 1)
    entered, release = Event(), Event()
    calls = []
    credential_calls = []
    resolve = diagnostic.resolve_diagnostic_credential
    def credential(*args):
        credential_calls.append(True)
        return resolve(*args)
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", credential)
    def handle(actual):
        calls.append(actual)
        entered.set()
        assert release.wait(10)
        return httpx.Response(200, json={"embeddings": [{"values": [1.0] + [0.0] * 767}]})
    monkeypatch.setattr(diagnostic, "_create_live_transport", lambda: httpx.MockTransport(handle))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(diagnostic.run_live, request, live_opt_in=True)
        try:
            assert entered.wait(10)
            with pytest.raises(controls.DiagnosticDenied):
                diagnostic.run_live(request, live_opt_in=True)
        finally:
            release.set()
        assert first.result(timeout=10).provider_attempts == 1
    assert len(calls) == len(credential_calls) == 1


def test_crash_retains_inflight_budget_and_blocks_resend(campaign, monkeypatch, tmp_path):
    prepared, _, _, _ = campaign()
    request = original_request(prepared, "query", tmp_path, 1)
    calls = []
    def crash(actual):
        calls.append(actual)
        raise KeyboardInterrupt()
    monkeypatch.setattr(diagnostic, "_create_live_transport", lambda: httpx.MockTransport(crash))
    with pytest.raises(KeyboardInterrupt):
        diagnostic.run_live(request, live_opt_in=True)
    assert ledger_rows(tmp_path / "attempts.sqlite")[0][1] == "IN_FLIGHT"
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(request, live_opt_in=True)
    assert len(calls) == 1


def test_source_drift_after_reservation_denies_physical_send(campaign, monkeypatch, tmp_path):
    prepared, binding, _, _ = campaign()
    calls = mock_transport(monkeypatch, tmp_path)
    reserve = diagnostic._reserve
    def drift(*args):
        reserve(*args)
        monkeypatch.setattr(diagnostic, "resolve_repository_source", lambda _: (binding.record.source_revision, False))
    monkeypatch.setattr(diagnostic, "_reserve", drift)
    result = diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert result.outcome == "FAILED" and result.provider_attempts == 0 and calls == []
    assert ledger_rows(tmp_path / "attempts.sqlite")[0][-1] == "UNKNOWN"


@pytest.mark.parametrize("change", ["method", "implementation", "data", "missing", "extra", "zero"])
def test_token_bound_metadata_cannot_stand_in_for_accepted_exact_inputs(campaign, monkeypatch, tmp_path, change):
    prepared, _, artifact, _ = campaign()
    value = json.loads(json.dumps(dict(artifact.fields), default=dict))
    bounds = value["accepted_input_token_bounds"]
    digest = next(iter(bounds))
    if change == "method":
        value["token_evidence"]["method_id"] = "unaccepted-method"
    elif change == "implementation":
        value["token_evidence"]["implementation_sha256"] = "0" * 64
    elif change == "data":
        bounds[digest] += 1
    elif change == "missing":
        del value["accepted_input_token_bounds"]
    elif change == "zero":
        bounds[digest] = 0
    else:
        bounds["0" * 64] = 20
        value["token_evidence"]["data_sha256"] = sha256(json.dumps(
            bounds, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    calls = mock_transport(monkeypatch, tmp_path)
    def load():
        return safety_parse(value)
    monkeypatch.setattr(diagnostic, "load_live_safety_artifact", load)
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", lambda *_: pytest.fail("credential accessed"))
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert calls == [] and ledger_rows(tmp_path / "attempts.sqlite") == []


def test_campaign_budget_exhaustion_after_known_mock_usage(campaign, monkeypatch, tmp_path):
    prepared, _, _, usage = campaign(record_changes={"maximum_invocations": 1})
    calls = mock_transport(monkeypatch, tmp_path)
    # Numeric usage is allowed ONLY by the existing temporary offline store;
    # real usage remains UNKNOWN and requires human review, never auto-refund.
    monkeypatch.setattr(usage, "finish", lambda r, key, outcome: usage._finish(r, key, outcome, "1"))
    request = original_request(prepared, "query", tmp_path, 1)
    assert diagnostic.run_live(request, live_opt_in=True).outcome == "SUCCESS"
    with pytest.raises(controls.DiagnosticDenied) as caught:
        diagnostic.run_live(request, live_opt_in=True)
    assert caught.value.code == "BUDGET_EXHAUSTED" and len(calls) == 1


@pytest.mark.parametrize("state,expected_code", [
    ("SUCCESS", "BUDGET_EXHAUSTED"), ("UNKNOWN", "RUNNER_CONFLICT"),
    ("TIMEOUT", "RUNNER_CONFLICT"), ("IN_FLIGHT", "RUNNER_CONFLICT"),
])
def test_major01_ineligible_campaign_denied_before_credential_discovery(
    campaign, monkeypatch, tmp_path, state, expected_code,
):
    # Reproduces MAJOR-01 (original Independent Audit: BLOCK). Known SUCCESS
    # exhausts the invocation budget; other states independently fence runners.
    prepared, binding, _, usage = campaign(record_changes={"maximum_invocations": 1})
    usage.reserve(binding.record, "prior-attempt", items=1, usage_units=1, cost_micro_units=1)
    if state != "IN_FLIGHT":
        usage._finish(binding.record, "prior-attempt", state, "1" if state == "SUCCESS" else "UNKNOWN")
    before = ledger_rows(tmp_path / "attempts.sqlite")
    calls = dict(credential=0, transport=0)
    def credential(*args):
        calls["credential"] += 1
        pytest.fail("ineligible campaign reached credential discovery")
    def transport():
        calls["transport"] += 1
        pytest.fail("ineligible campaign reached provider transport")
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", credential)
    monkeypatch.setattr(diagnostic, "_create_live_transport", transport)
    with pytest.raises(controls.DiagnosticDenied) as caught:
        diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert caught.value.code == expected_code
    assert calls == dict(credential=0, transport=0)
    assert ledger_rows(tmp_path / "attempts.sqlite") == before


@pytest.mark.parametrize("failure", ["unavailable", "invalid", "unexpected"])
def test_major01_credential_failure_retains_reservation_and_typed_failure(
    campaign, monkeypatch, tmp_path, caplog, failure,
):
    prepared, _, _, _ = campaign()
    calls = dict(credential=0, transport=0)
    reserved_rows = []
    secret = "synthetic-major01-must-not-leak"
    code = "CREDENTIAL_INVALID" if failure == "invalid" else "CREDENTIAL_UNAVAILABLE"
    def credential(*args):
        calls["credential"] += 1
        row = ledger_rows(tmp_path / "attempts.sqlite")[0]
        assert row[1] == "IN_FLIGHT" and row[3] > 0 and row[4] > 0
        reserved_rows.append(row)
        if failure == "unexpected":
            raise RuntimeError(secret)
        raise controls.DiagnosticDenied("synthetic credential resolution denied", code=code)
    def transport():
        calls["transport"] += 1
        pytest.fail("credential failure reached provider transport")
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", credential)
    monkeypatch.setattr(diagnostic, "_create_live_transport", transport)
    request = original_request(prepared, "query", tmp_path, 1)
    with pytest.raises(controls.DiagnosticDenied) as caught:
        diagnostic.run_live(request, live_opt_in=True)
    assert caught.value.code == code and secret not in str(caught.value) + caplog.text
    rows = ledger_rows(tmp_path / "attempts.sqlite")
    assert len(rows) == 1 and rows[0][1] == "FAILED" and rows[0][-1] == "UNKNOWN"
    assert rows[0][0] == reserved_rows[0][0] and rows[0][2:5] == reserved_rows[0][2:5]
    assert secret.encode() not in (tmp_path / "attempts.sqlite").read_bytes()
    with pytest.raises(controls.DiagnosticDenied) as rejected:
        diagnostic.run_live(request, live_opt_in=True)
    assert rejected.value.code == "RUNNER_CONFLICT"
    assert calls == dict(credential=1, transport=0)
    assert ledger_rows(tmp_path / "attempts.sqlite") == rows


def test_major01_valid_flow_orders_atomic_reservation_before_credential_and_dispatch(
    campaign, monkeypatch, tmp_path,
):
    prepared, _, _, _ = campaign()
    order = []
    reserve, credential = diagnostic._reserve, diagnostic.resolve_diagnostic_credential
    def reserve_once(*args):
        reserve(*args)
        order.append("reserved")
    def resolve(*args):
        assert ledger_rows(tmp_path / "attempts.sqlite")[0][1] == "IN_FLIGHT"
        order.append("credential")
        return credential(*args)
    calls = mock_transport(monkeypatch, tmp_path)
    factory = diagnostic._create_live_transport
    def transport():
        order.append("transport")
        return factory()
    monkeypatch.setattr(diagnostic, "_reserve", reserve_once)
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", resolve)
    monkeypatch.setattr(diagnostic, "_create_live_transport", transport)
    result = diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert result.outcome == "SUCCESS" and len(calls) == 1
    assert order == ["reserved", "credential", "transport"]
