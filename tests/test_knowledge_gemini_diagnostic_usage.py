"""Only sealed mock transport/keys/usage; no provider evidence is claimed."""

from dataclasses import replace
import json

import httpx
import pytest

import knowledge_index.gemini_diagnostic as diagnostic
import knowledge_index.gemini_diagnostic_controls as controls
from knowledge_index.google_embedding_adapter import _embedding_prompt_tokens
from test_knowledge_gemini_diagnostic_dispatch import campaign, original_request
from test_knowledge_gemini_diagnostic_batching import batched, transport
from test_knowledge_gemini_diagnostic_controls import ledger_rows
from test_knowledge_gemini_embedding2_serialization import offline_sdk


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


def formal_store(monkeypatch, temporary):
    """Exercise exact Live class numeric checks on an ISOLATED fixture DB.

    Native host checks are already tested separately. Bypass only native path
    I/O, NEVER receipt parsing/validation, formal finish() or its state machine.
    """
    live = object.__new__(controls.LiveDiagnosticUsage)
    live._path = temporary._path
    monkeypatch.setattr(controls.LiveDiagnosticUsage, '_connect', controls.TemporaryDiagnosticUsage._connect)
    monkeypatch.setattr(controls.LiveDiagnosticUsage, '_check_reservation_state', lambda *a: None)
    monkeypatch.setattr(diagnostic, 'LiveDiagnosticUsage', lambda: live)
    return live


def query_response(monkeypatch, tmp_path, *, metadata=None, body=None, status=200, bad_vector=False):
    calls = []
    def handle(request):
        assert ledger_rows(tmp_path/'attempts.sqlite')[0][1] == 'IN_FLIGHT'
        calls.append(request)
        vector = [1.0]+[0.0]*767
        if bad_vector: vector.pop()
        value = {'embeddings':[{'values':vector}]}
        if metadata is not None: value['usageMetadata'] = metadata
        return httpx.Response(status, headers={'x-prompt-token-count':'1'},
                              content=body if body is not None else json.dumps(value).encode())
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: httpx.MockTransport(handle))
    return calls


@pytest.mark.parametrize('count', [1, 50])
def test_trusted_response_propagates_and_persists_on_formal_store(campaign, monkeypatch, tmp_path, count):
    prepared, _, _, temporary = campaign()
    live = formal_store(monkeypatch, temporary)
    calls = query_response(monkeypatch, tmp_path, metadata={'promptTokenCount':count, 'unused':'private-response-marker'})
    result = diagnostic.run_live(original_request(prepared, 'query', tmp_path, 1), live_opt_in=True)
    assert result.outcome == 'SUCCESS' and result.usage == str(count) and len(calls) == 1
    assert result.usage_provenance_sha256 and len(result.usage_provenance_sha256) == 64
    row = ledger_rows(tmp_path/'attempts.sqlite')[0]
    assert row[-1] == str(count) and row[1] == 'SUCCESS'
    assert row[3] == prepared.usage_units + 16  # reservation is NOT actual usage/refunded
    data = (tmp_path/'attempts.sqlite').read_bytes()
    assert b'synthetic-dispatch-key' not in data and b'private-response-marker' not in data
    assert type(live) is controls.LiveDiagnosticUsage


@pytest.mark.parametrize('metadata', [None, {}, [], 'untrusted', {'promptTokenCount':None},
    {'promptTokenCount':True}, {'promptTokenCount':False}, {'promptTokenCount':'1'},
    {'promptTokenCount':1.0}, {'promptTokenCount':-1}, {'promptTokenCount':0},
    {'promptTokenCount':2**63}, {'promptTokenCount':float('nan')}, {'promptTokenCount':float('inf')},
    {'prompt_token_count':1}])
def test_missing_invalid_usage_stays_unknown_on_formal_store(campaign, monkeypatch, tmp_path, metadata):
    prepared, _, _, temporary = campaign()
    formal_store(monkeypatch, temporary)
    calls = query_response(monkeypatch, tmp_path, metadata=metadata)
    result = diagnostic.run_live(original_request(prepared, 'query', tmp_path, 1), live_opt_in=True)
    assert result.usage == 'UNKNOWN' and result.usage_provenance_sha256 is None and len(calls) == 1
    assert ledger_rows(tmp_path/'attempts.sqlite')[0][-1] == 'UNKNOWN'
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(original_request(prepared, 'query', tmp_path, 1), live_opt_in=True)
    assert len(calls) == 1


@pytest.mark.parametrize('body', [b'{', b'[]', b'null', b'\xff',
    b'{"usageMetadata":{"promptTokenCount":1,"promptTokenCount":2}}',
    b'{"usageMetadata":{"promptTokenCount":1},"usageMetadata":{"promptTokenCount":2}}',
    b'{"usageMetadata":{"promptTokenCount":NaN}}', b' '*1000001],
    ids=['truncated','array','null','invalid_utf8','duplicate_count','duplicate_metadata','nan','oversized'])
def test_strict_allowlist_parser_rejects_malformed_duplicate_or_oversized(body):
    assert _embedding_prompt_tokens(body) is None


def test_pinned_sdk_still_drops_usage_but_guard_captures_it(campaign, monkeypatch, tmp_path):
    from google.genai import models, types
    assert 'usage_metadata' not in types.EmbedContentResponse.model_fields
    assert 'usageMetadata' not in models._EmbedContentResponse_from_mldev(
        {'embeddings':[], 'usageMetadata':{'promptTokenCount':1}})
    prepared, _, _, temporary = campaign()
    formal_store(monkeypatch, temporary)
    query_response(monkeypatch, tmp_path, metadata={'promptTokenCount':1})
    result = diagnostic.run_live(original_request(prepared, 'query', tmp_path, 1), live_opt_in=True)
    assert result.usage == '1'  # own boundary, no SDK monkeypatch/upgrade


def test_no_trusted_body_access_is_unknown_even_with_usage_header(campaign, monkeypatch, tmp_path):
    prepared, _, _, temporary = campaign()
    formal_store(monkeypatch, temporary)
    query_response(monkeypatch, tmp_path)
    result = diagnostic.run_live(original_request(prepared, 'query', tmp_path, 1), live_opt_in=True)
    assert result.usage == 'UNKNOWN'
    with pytest.raises(controls.DiagnosticDenied): controls._issue_provider_usage_receipt(object())


@pytest.mark.parametrize('fake', [None, 1, '1', {'verified':True,'prompt_tokens':1}, object()])
def test_caller_supplied_usage_cannot_complete_live_attempt(campaign, monkeypatch, tmp_path, fake):
    prepared, binding, _, temporary = campaign()
    live = formal_store(monkeypatch, temporary)
    live.reserve(binding.record, 'held', items=1, usage_units=10, cost_micro_units=1)
    with pytest.raises(controls.DiagnosticDenied): live._finish_provider_usage(binding.record, 'held', 'SUCCESS', fake, 'a'*64)
    with pytest.raises(controls.DiagnosticDenied): live._finish(binding.record, 'held', 'SUCCESS', '1')
    with pytest.raises(TypeError): live.finish(binding.record, 'held', 'SUCCESS', usage=fake)
    with pytest.raises(controls.DiagnosticDenied): controls._ProviderUsageReceipt(prompt_tokens=1, verified=True)
    assert ledger_rows(tmp_path/'attempts.sqlite')[0][1] == 'IN_FLIGHT'


@pytest.mark.parametrize('change', ['attempt', 'source', 'profile', 'input', 'request', 'scalar', 'replay'])
def test_boundary_receipt_cannot_be_rebound_or_replayed(campaign, monkeypatch, tmp_path, change):
    prepared, binding, _, temporary = campaign()
    live = formal_store(monkeypatch, temporary)
    receipts = []
    finish = live._finish_provider_usage
    def capture(r, i, o, receipt, digest):
        receipts.append((r,i,o,receipt,digest))
        return finish(r,i,o,receipt,digest)
    monkeypatch.setattr(live, '_finish_provider_usage', capture)
    query_response(monkeypatch, tmp_path, metadata={'promptTokenCount':1})
    diagnostic.run_live(original_request(prepared, 'query', tmp_path, 1), live_opt_in=True)
    record, request_id, outcome, receipt, digest = receipts[0]
    before = ledger_rows(tmp_path/'attempts.sqlite')
    if change == 'attempt': request_id = 'different'
    elif change == 'source': record = replace(record, source_revision='a'*40)
    elif change == 'profile': record = replace(record, credential_profile_identity='different-profile')
    elif change == 'input': record = replace(record, input_commitment='0'*64)
    elif change == 'request': digest = '0'*64
    with pytest.raises(controls.DiagnosticDenied):
        if change == 'scalar': live._finish(record, request_id, outcome, '2', _provider_receipt=receipt)
        else: finish(record, request_id, outcome, receipt, digest)
    assert ledger_rows(tmp_path/'attempts.sqlite') == before


def batch_usage(monkeypatch, tmp_path, request, *, missing_batch=None, tokens=1):
    calls = transport(monkeypatch, tmp_path, request)
    factory = diagnostic._create_live_transport
    def handle(actual):
        response = factory().handle_request(actual)
        body = json.loads(response.content)
        if len(calls)-1 != missing_batch: body['usageMetadata'] = {'promptTokenCount':tokens}
        return httpx.Response(response.status_code, content=json.dumps(body).encode())
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: httpx.MockTransport(handle))
    return calls


def test_formal_store_valid_metadata_allows_safe_batch_continuation(batched, monkeypatch, tmp_path):
    request, _, _, temporary = batched(known_usage=False)
    formal_store(monkeypatch, temporary)
    calls = batch_usage(monkeypatch, tmp_path, request)
    result = diagnostic.run_live(request, live_opt_in=True)
    rows = ledger_rows(tmp_path/'attempts.sqlite')
    assert result.vector_count == 54 and result.usage == '6' and len(calls) == 6
    assert result.usage_provenance_sha256 and all(r[-2:] == (1,'1') for r in rows)
    with pytest.raises(controls.DiagnosticDenied) as failure:
        diagnostic.run_live(request, live_opt_in=True)
    assert failure.value.code == 'BUDGET_EXHAUSTED' and len(calls) == 6


@pytest.mark.parametrize('missing_batch', [0, 1, 5])
def test_unknown_usage_stops_next_batch_without_refund(batched, monkeypatch, tmp_path, missing_batch):
    request, _, _, temporary = batched(known_usage=False)
    formal_store(monkeypatch, temporary)
    calls = batch_usage(monkeypatch, tmp_path, request, missing_batch=missing_batch)
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.outcome == 'BLOCKED' and result.usage == 'UNKNOWN' and result.vector_count == 0
    assert len(calls) == missing_batch+1
    assert all(row[-2] == 1 for row in ledger_rows(tmp_path/'attempts.sqlite'))


def test_excess_actual_usage_is_persisted_and_blocks_without_refund(batched, monkeypatch, tmp_path):
    request, _, _, temporary = batched(known_usage=False)
    formal_store(monkeypatch, temporary)
    calls = batch_usage(monkeypatch, tmp_path, request, tokens=500000)
    with pytest.raises(controls.DiagnosticDenied, match='exceed reservation'):
        diagnostic.run_live(request, live_opt_in=True)
    row = ledger_rows(tmp_path/'attempts.sqlite')[0]
    assert row[1] == 'UNKNOWN' and row[-2:] == (1,'500000') and len(calls) == 1
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_live(request, live_opt_in=True)
    assert len(calls) == 1


def test_usage_record_failure_and_restart_keep_attempt_fence(batched, monkeypatch, tmp_path):
    request, _, _, temporary = batched(known_usage=False)
    live = formal_store(monkeypatch, temporary)
    calls = batch_usage(monkeypatch, tmp_path, request)
    def fail(*a): raise controls.DiagnosticDenied('durable usage unavailable')
    monkeypatch.setattr(live, '_finish_provider_usage', fail)
    with pytest.raises(controls.DiagnosticDenied, match='durable usage'): diagnostic.run_live(request, live_opt_in=True)
    assert ledger_rows(tmp_path/'attempts.sqlite')[0][1] == 'IN_FLIGHT'
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_live(request, live_opt_in=True)
    assert len(calls) == 1


@pytest.mark.parametrize('bad_vector,status', [(True,200), (False,429)])
def test_valid_usage_cannot_upgrade_invalid_or_failed_response(campaign, monkeypatch, tmp_path, bad_vector, status):
    prepared, _, _, temporary = campaign()
    formal_store(monkeypatch, temporary)
    calls = query_response(monkeypatch, tmp_path, metadata={'promptTokenCount':1}, bad_vector=bad_vector, status=status)
    result = diagnostic.run_live(original_request(prepared,'query',tmp_path,1), live_opt_in=True)
    assert result.outcome != 'SUCCESS' and result.usage == 'UNKNOWN' and len(calls) == 1
    assert ledger_rows(tmp_path/'attempts.sqlite')[0][-1] == 'UNKNOWN'


def test_timeout_before_complete_body_has_no_trusted_usage(batched, monkeypatch, tmp_path):
    request, _, _, temporary = batched(known_usage=False)
    formal_store(monkeypatch, temporary)
    calls = []
    class InterruptedBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"usageMetadata":{"promptTokenCount":1},'
            raise httpx.ReadTimeout('synthetic timeout')
    def handle(actual):
        calls.append(True)
        return httpx.Response(200, stream=InterruptedBody())
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: httpx.MockTransport(handle))
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.outcome == 'TIMEOUT' and result.usage == 'UNKNOWN' and calls == [True]
    assert ledger_rows(tmp_path/'attempts.sqlite')[0][-2:] == (1,'UNKNOWN')


def test_completion_commit_failure_cannot_continue_or_clear_fence(batched, monkeypatch, tmp_path):
    from test_knowledge_gemini_diagnostic_controls import CompletionFaultConnection
    request, _, _, temporary = batched(known_usage=False)
    live = formal_store(monkeypatch, temporary)
    calls = batch_usage(monkeypatch, tmp_path, request)
    finish, connect, events = live._finish_provider_usage, live._connect, []
    def faulty_finish(*args):
        monkeypatch.setattr(live, '_connect', lambda **kw: connect(**kw) if kw.get('read_only')
                            else CompletionFaultConnection(connect(**kw), 'commit_before', events))
        return finish(*args)
    monkeypatch.setattr(live, '_finish_provider_usage', faulty_finish)
    with pytest.raises(controls.DiagnosticDenied, match='UNKNOWN'): diagnostic.run_live(request, live_opt_in=True)
    assert calls and len(calls) == 1 and ledger_rows(tmp_path/'attempts.sqlite')[0][1] == 'IN_FLIGHT'
    assert events.count('commit') == 1


def test_late_receipt_does_not_reconcile_existing_unknown(campaign, monkeypatch, tmp_path):
    prepared, binding, _, temporary = campaign()
    live = formal_store(monkeypatch, temporary)
    finish, receipts = live._finish_provider_usage, []
    def unavailable(r, i, outcome, receipt, digest):
        receipts.append((r,i,outcome,receipt,digest))
        live.finish(r, i, outcome)  # deliberate UNKNOWN fixture
    monkeypatch.setattr(live, '_finish_provider_usage', unavailable)
    query_response(monkeypatch, tmp_path, metadata={'promptTokenCount':1})
    diagnostic.run_live(original_request(prepared,'query',tmp_path,1), live_opt_in=True)
    before = ledger_rows(tmp_path/'attempts.sqlite')
    assert before[0][-1] == 'UNKNOWN'
    with pytest.raises(controls.DiagnosticDenied): finish(*receipts[0])
    assert ledger_rows(tmp_path/'attempts.sqlite') == before
