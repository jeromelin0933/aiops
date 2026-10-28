"""Mock-only plans/usage/keys. No fixture size or source is live evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
from pathlib import Path
from threading import Event

import httpx
import pytest

import knowledge_index.gemini_diagnostic as diagnostic
import knowledge_index.gemini_diagnostic_controls as controls
from knowledge_index.contracts import ChunkIdentityInput
from knowledge_index.identity import chunk_identity, document_identity, document_version_identity
from test_knowledge_gemini_diagnostic import request_and_record, approved_input, _host_e4_observation
from test_knowledge_gemini_diagnostic_controls import facts, safety_case, safety_parse, ledger_rows
from test_knowledge_gemini_diagnostic_free_tier import free_evidence
from test_knowledge_gemini_embedding2_serialization import offline_sdk


@pytest.fixture(autouse=True)
def sealed_network(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


def thaw(value):
    if hasattr(value, "items"):
        return {k: thaw(v) for k, v in value.items()}
    if type(value) is tuple:
        return [thaw(v) for v in value]
    return value


def wire_e4(evidence):
    value = asdict(evidence)
    for key in ("valid_from", "valid_until"):
        value[key] = value[key].isoformat()
    return value


@pytest.fixture
def batched(tmp_path, monkeypatch):
    def make(*, known_usage=True, paid=False, batch_size=9, changes=None):
        # Six SYNTHETIC documents. Real corpus/manifest/build are never mutated.
        request, record, _, _ = request_and_record(tmp_path, count=54)
        chunks, documents = [], []
        for i, c in enumerate(request.chunks):
            name = 'synthetic-sop-' + str(i // 9)
            doc = document_identity(request.approved_corpus_id, name)
            ver = document_version_identity(doc, '1.0', sha256(name.encode()).hexdigest())
            identity = chunk_identity(ChunkIdentityInput(doc, ver, c.section_identity,
                i % 9, c.content_hash, 'chunk-profile-v1'))
            chunks.append(replace(c, document_identity=doc, document_version_identity=ver,
                                  chunk_identity=identity, ordinal=i % 9))
            if (doc, name) not in documents:
                documents.append((doc, name))
        documents.sort()
        request = replace(request, chunks=tuple(chunks), approved_document_ids=tuple(documents))
        _, ids, texts = diagnostic._envelopes(request)
        record = replace(record, input_commitment=controls.input_commitment('document', ids, texts),
            maximum_items=54, maximum_invocations=54, maximum_item_tokens=4096,
            maximum_aggregate_tokens=30000, maximum_usage_units=30000, maximum_payload_bytes=1000000,
            bounds_basis='fixture-bounds', cost_basis='fixture-price',
            valid_from='2000-01-01T00:00:00+00:00', expires_at='2099-01-01T00:00:00+00:00')
        ranges = [dict(ordinal=n, start=start, end=min(start+batch_size, 54),
            input_commitment=controls.input_commitment('document', ids[start:start+batch_size], texts[start:start+batch_size]))
            for n, start in enumerate(range(0, 54, batch_size))]
        plan = dict(schema_version=1, total_inputs=54, input_commitment=record.input_commitment,
            chunk_metadata_commitment=diagnostic._chunk_metadata_commitment(request), build_identity=request.build_identity,
            model='gemini-embedding-2', api='v1', embedding_profile_identity=request.capability.embedding_profile_identity,
            maximum_batches=len(ranges), maximum_batch_items=batch_size, maximum_batch_tokens=10000,
            maximum_batch_payload_bytes=100000,
            quota=dict(rpm=100, tpm=30000, rpd=1000, request_units_per_invocation=batch_size,
                       source=dict(safety_case()[3]['pricing_evidence']['source'])), batches=ranges)
        record = replace(record, bounds_basis='SEQUENTIAL_BATCH_PLAN_V1:'+controls.DiagnosticBatchPlan.from_mapping(plan).commitment)
        if changes:
            record = replace(record, **changes)
        approved = approved_input(request, record)

        def e4(actual, grant):
            prepared = diagnostic._preflight(load_record=lambda: grant,
                resolve_facts=lambda *_: facts(credential_profile_identity=grant.credential_profile_identity),
                safety=lambda r, ts: (diagnostic._offline_proof(r), diagnostic._offline_safety_basis(r, ts),
                                      sum(len(t.encode()) for t in ts), 100),
                request=actual, live_opt_in=True, offline_harness=True)
            return _host_e4_observation(prepared, approved_input(actual, grant))[0]
        full_e4 = e4(request, record)
        children = []
        for row in ranges:
            start, end = row['start'], row['end']
            sub_chunks = request.chunks[start:end]
            sub_docs = {c.document_identity for c in sub_chunks}
            child = replace(request, chunks=sub_chunks,
                approved_document_ids=tuple(pair for pair in documents if pair[0] in sub_docs))
            children.append(wire_e4(e4(child, replace(record, input_commitment=row['input_commitment']))))
        value = safety_case()[3]
        value['binding'].update(campaign_id=record.campaign_id, source_revision=record.source_revision,
            authorization_record_sha256=controls.authorization_record_digest(record), kind='document',
            input_scope_identity=record.input_scope_identity, input_commitment=record.input_commitment,
            bounds_basis=record.bounds_basis, cost_basis=record.cost_basis)
        value['credential_binding']['profile_identity'] = record.credential_profile_identity
        value['e4_reference'].update(reference=full_e4.reference, sdk_body_sha256=full_e4.sdk_body_sha256,
            canonical_json_sha256=full_e4.canonical_json_sha256, sdk_body_bytes=full_e4.sdk_body_bytes)
        bounds = {sha256(t.encode()).hexdigest(): len(t.encode())+16 for t in texts}
        value['accepted_input_token_bounds'] = bounds
        value['token_evidence'].update(method_id='APPROVED_FIXED_INPUT_UPPER_BOUNDS_V1',
            implementation_sha256=sha256(Path(controls.__file__).read_bytes()).hexdigest(),
            data_sha256=sha256(json.dumps(bounds, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        value['sequential_batch_plan'], value['batch_sdk_evidence'] = plan, children
        value['provider_limits'].update(item_limit=batch_size, aggregate_token_limit=10000, payload_byte_limit=100000)
        if not paid:
            free_evidence(value, record)
        # Never return caller evidence through run_live; replace fixed host I/O
        # seams with isolated synthetic fixtures, as existing dispatch tests do.
        binding = controls.LiveHostBinding(record, approved, None, record.runner_identity, full_e4)
        monkeypatch.setattr(diagnostic, 'load_live_binding', lambda: binding)
        monkeypatch.setattr(diagnostic, 'load_live_safety_artifact', lambda: safety_parse(value))
        monkeypatch.setattr(diagnostic, 'resolve_repository_source', lambda _: (record.source_revision, True))
        monkeypatch.setattr(diagnostic.socket, 'gethostname', lambda: record.authorized_host)
        monkeypatch.setitem(controls.os.environ, 'GEMINI_CREDENTIAL_PROFILE', record.credential_profile_identity)
        monkeypatch.setitem(controls.os.environ, 'GEMINI_CREDENTIAL_REVISION', 'r1')
        monkeypatch.setitem(controls.os.environ, 'GEMINI_API_KEY', 'synthetic-batch-key')
        usage = controls.TemporaryDiagnosticUsage(tmp_path/'attempts.sqlite')
        monkeypatch.setattr(diagnostic, 'LiveDiagnosticUsage', lambda: usage)
        if known_usage:
            def fixture_finish(r, request_id, outcome):
                n = int(request_id.rsplit('.', 1)[1])
                start, end = ranges[n]['start'], ranges[n]['end']
                usage._finish(r, request_id, outcome,
                    str(sum(bounds[sha256(t.encode()).hexdigest()] for t in texts[start:end]))
                    if outcome == 'SUCCESS' else 'UNKNOWN')
            monkeypatch.setattr(usage, 'finish', fixture_finish)
        return request, binding, value, usage
    return make


def transport(monkeypatch, tmp_path, request, *, fail_batch=None, mode=None):
    calls = []
    _, _, texts = diagnostic._envelopes(request)
    global_index = {text: i for i, text in enumerate(texts)}
    def handle(actual):
        n = len(calls)
        body = json.loads(actual.content)
        rows = ledger_rows(tmp_path/'attempts.sqlite')
        assert sum(row[1] == 'IN_FLIGHT' for row in rows) == 1
        assert actual.extensions['timeout'] == dict(connect=30.0, read=30.0, write=30.0, pool=30.0)
        assert str(actual.url) == controls._SAFETY_URL
        assert actual.headers['x-goog-api-key'] == 'synthetic-batch-key'
        assert body.keys() == {'requests'}
        for item in body['requests']:
            assert item.keys() == {'model', 'content', 'outputDimensionality'}
            assert item['model'] == 'models/gemini-embedding-2' and item['outputDimensionality'] == 768
            assert item['content']['role'] == 'user' and len(item['content']['parts']) == 1
        calls.append(body)
        if n == fail_batch:
            if mode == 'crash':
                raise KeyboardInterrupt('synthetic crash')
            if mode == 'timeout':
                raise httpx.ReadTimeout('synthetic-batch-key', request=actual)
            if mode == 'unknown':
                raise httpx.ConnectError('synthetic-batch-key', request=actual)
            if mode in ('429', '500'):
                return httpx.Response(int(mode), json={'error': {'code': int(mode), 'status': 'RESOURCE_EXHAUSTED', 'message': 'synthetic'}})
        vectors = []
        for item in body['requests']:
            i = global_index[item['content']['parts'][0]['text']]
            vectors.append([math.cos(i*.01), math.sin(i*.01)]+[0.0]*766)
        if n == fail_batch:
            if mode == 'count': vectors.pop()
            elif mode == 'dimension': vectors[0].pop()
            elif mode == 'nan': vectors[0][0] = float('nan')
            elif mode == 'infinity': vectors[0][0] = float('inf')
            elif mode == 'zero': vectors[0] = [0.0]*768
            elif mode == 'normalization': vectors[0][0] = 2.0
            elif mode == 'shape': return httpx.Response(200, json={})
        return httpx.Response(200, content=json.dumps({'embeddings': [{'values': v} for v in vectors]}).encode())
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: httpx.MockTransport(handle))
    return calls


@pytest.mark.parametrize('size', [1, 7, 9, 18, 54])
def test_fixed_plan_global_order_and_real_sdk_serialization(batched, monkeypatch, tmp_path, size):
    request, binding, value, _ = batched(batch_size=size)
    calls = transport(monkeypatch, tmp_path, request)
    credential_calls = []
    resolve = diagnostic.resolve_diagnostic_credential
    def credential(*args):
        assert any(r[1] == 'IN_FLIGHT' for r in ledger_rows(tmp_path/'attempts.sqlite'))
        credential_calls.append(True)
        return resolve(*args)
    monkeypatch.setattr(diagnostic, 'resolve_diagnostic_credential', credential)
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.outcome == 'SUCCESS' and result.vector_count == 54
    assert result.category == 'BATCHED_OBSERVATION_MAPPING_REVIEW_REQUIRED'
    assert result.ordered_identities == tuple(c.chunk_identity for c in request.chunks)
    assert len(calls) == len(credential_calls) == len(value['sequential_batch_plan']['batches'])
    actual = [item['content']['parts'][0]['text'] for call in calls for item in call['requests']]
    assert actual == list(diagnostic._envelopes(request)[2])
    assert all(r[-2] == 1 for r in ledger_rows(tmp_path/'attempts.sqlite'))
    assert sum(r[2] for r in ledger_rows(tmp_path/'attempts.sqlite')) == 54
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(request, live_opt_in=True)  # no cross-run resume
    assert len(calls) == len(credential_calls)


@pytest.mark.parametrize('mutation', ['empty', 'overlap', 'gap', 'ordinal', 'empty_batch', 'missing', 'count', 'model', 'api', 'zero_limit'])
def test_invalid_plans_rejected_before_credentials(batched, monkeypatch, mutation):
    request, _, value, _ = batched()
    plan = value['sequential_batch_plan']
    if mutation == 'empty': plan['batches'] = []
    elif mutation == 'overlap': plan['batches'][1]['start'] -= 1
    elif mutation == 'gap': plan['batches'][1]['start'] += 1
    elif mutation == 'ordinal': plan['batches'][1]['ordinal'] = 0
    elif mutation == 'empty_batch': plan['batches'][0]['end'] = 0
    elif mutation == 'missing': plan['batches'].pop()
    elif mutation == 'count': plan['maximum_batches'] = 1
    elif mutation == 'model': plan['model'] = 'text-embedding-004'
    elif mutation == 'api': plan['api'] = 'v1beta'
    else: plan['maximum_batch_tokens'] = 0
    monkeypatch.setattr(diagnostic, 'resolve_diagnostic_credential', lambda *_: pytest.fail('secret before rejected plan'))
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(request, live_opt_in=True)


@pytest.mark.parametrize('mode', ['timeout', 'unknown', '429', '500', 'count', 'dimension', 'nan', 'infinity', 'zero', 'normalization', 'shape'])
def test_batch_failure_stops_without_retry_or_partial_success(batched, monkeypatch, tmp_path, mode):
    request, _, _, _ = batched()
    calls = transport(monkeypatch, tmp_path, request, fail_batch=1, mode=mode)
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.outcome in ('FAILED', 'TIMEOUT', 'UNKNOWN') and result.vector_count == 0
    assert result.failure_code is not None and len(calls) == 2
    before = ledger_rows(tmp_path/'attempts.sqlite')
    assert len(before) == 2 and all(row[-2] == 1 for row in before)
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(request, live_opt_in=True)
    assert ledger_rows(tmp_path/'attempts.sqlite') == before and len(calls) == 2


def test_success_with_unknown_usage_stops_after_first_batch(batched, monkeypatch, tmp_path):
    request, _, _, _ = batched(known_usage=False)
    calls = transport(monkeypatch, tmp_path, request)
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.outcome == 'BLOCKED' and result.category == 'UNKNOWN_USAGE'
    assert result.vector_count == 0 and result.usage == 'UNKNOWN' and len(calls) == 1
    assert ledger_rows(tmp_path/'attempts.sqlite')[0][-2:] == (1, 'UNKNOWN')


def test_crash_restart_keeps_in_flight_fence(batched, monkeypatch, tmp_path):
    request, _, _, _ = batched()
    calls = transport(monkeypatch, tmp_path, request, fail_batch=0, mode='crash')
    with pytest.raises(KeyboardInterrupt): diagnostic.run_live(request, live_opt_in=True)
    assert ledger_rows(tmp_path/'attempts.sqlite')[0][1] == 'IN_FLIGHT'
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_live(request, live_opt_in=True)
    assert len(calls) == 1


def rebind_plan(binding, value, monkeypatch, **record_changes):
    """Synthetic PM re-approval fixture only; production has no such API."""
    plan = controls.DiagnosticBatchPlan.from_mapping(value['sequential_batch_plan'])
    record = replace(binding.record, bounds_basis='SEQUENTIAL_BATCH_PLAN_V1:'+plan.commitment, **record_changes)
    e4 = replace(binding.sdk_byte_evidence, authorization_record_sha256=controls.authorization_record_digest(record))
    binding = replace(binding, record=record, sdk_byte_evidence=e4)
    value['binding'].update(bounds_basis=record.bounds_basis, authorization_record_sha256=controls.authorization_record_digest(record))
    for batch, evidence in zip(value['sequential_batch_plan']['batches'], value['batch_sdk_evidence']):
        evidence['authorization_record_sha256'] = controls.authorization_record_digest(
            replace(record, input_commitment=batch['input_commitment']))
    monkeypatch.setattr(diagnostic, 'load_live_binding', lambda: binding)
    return binding


@pytest.mark.parametrize('mode', ['item_tokens', 'batch_tokens', 'payload', 'provider_limit',
    'missing_tokens', 'missing_plan', 'batch_e4', 'profile', 'version', 'order', 'dirty',
    'quota_tokens', 'quota_requests', 'invocations', 'cost', 'usage'])
def test_safety_denials_never_resolve_credentials_or_send(batched, monkeypatch, tmp_path, mode):
    request, binding, value, _ = batched()
    plan = value['sequential_batch_plan']
    if mode == 'item_tokens': value['provider_limits']['per_item_token_limit'] = 1
    elif mode == 'batch_tokens': plan['maximum_batch_tokens'] = 1
    elif mode == 'payload': plan['maximum_batch_payload_bytes'] = 1
    elif mode == 'provider_limit': del value['provider_limits']['source']
    elif mode == 'missing_tokens': del value['accepted_input_token_bounds']
    elif mode == 'missing_plan':
        del value['sequential_batch_plan']
        del value['batch_sdk_evidence']
    elif mode == 'batch_e4': value['batch_sdk_evidence'][1]['canonical_json_sha256'] = '0'*64
    elif mode == 'profile': plan['embedding_profile_identity'] = 'different-profile'
    elif mode == 'version':
        c = request.chunks[0]
        request = replace(request, chunks=(replace(c, document_version_identity='kver_'+'a'*64),)+request.chunks[1:])
    elif mode == 'order': request = replace(request, chunks=tuple(reversed(request.chunks)))
    elif mode == 'dirty': monkeypatch.setattr(diagnostic, 'resolve_repository_source', lambda _: (binding.record.source_revision, False))
    elif mode == 'quota_tokens':
        plan['quota']['tpm'] = value['free_tier_evidence']['quota_evidence']['tpm'] = 1
    elif mode == 'quota_requests': plan['quota']['request_units_per_invocation'] = 100
    updates = dict(invocations={'maximum_invocations': 1}, cost={'maximum_cost_micro_units': 1}, usage={'maximum_usage_units': 1})
    if mode != 'missing_plan':
        rebind_plan(binding, value, monkeypatch, **updates.get(mode, {}))
    calls, credentials = [], []
    monkeypatch.setattr(diagnostic, 'resolve_diagnostic_credential', lambda *_: credentials.append(True))
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: calls.append(True))
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_live(request, live_opt_in=True)
    assert not calls and not credentials and not ledger_rows(tmp_path/'attempts.sqlite')


@pytest.mark.parametrize('state', ['exhausted', 'UNKNOWN', 'TIMEOUT', 'IN_FLIGHT'])
def test_atomic_campaign_capacity_and_runner_fences_precede_credentials(batched, monkeypatch, tmp_path, state):
    request, binding, _, usage = batched()
    usage.reserve(binding.record, 'previous', items=1, usage_units=1, cost_micro_units=1)
    if state == 'exhausted': usage._finish(binding.record, 'previous', 'SUCCESS', '1')
    elif state != 'IN_FLIGHT': usage._finish(binding.record, 'previous', state, 'UNKNOWN')
    before = ledger_rows(tmp_path/'attempts.sqlite')
    monkeypatch.setattr(diagnostic, 'resolve_diagnostic_credential', lambda *_: pytest.fail('credential before capacity/fence'))
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: pytest.fail('send before capacity/fence'))
    with pytest.raises(controls.DiagnosticDenied) as failure: diagnostic.run_live(request, live_opt_in=True)
    assert failure.value.code == ('BUDGET_EXHAUSTED' if state == 'exhausted' else 'RUNNER_CONFLICT')
    assert ledger_rows(tmp_path/'attempts.sqlite') == before


def test_credential_failure_retains_attempt_hold_and_unknown(batched, monkeypatch, tmp_path):
    request, _, _, _ = batched()
    def denied(*args):
        assert ledger_rows(tmp_path/'attempts.sqlite')[0][1] == 'IN_FLIGHT'
        raise controls.DiagnosticDenied('synthetic failure', code='CREDENTIAL_PROFILE_MISMATCH')
    monkeypatch.setattr(diagnostic, 'resolve_diagnostic_credential', denied)
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: pytest.fail('credential failure sent provider'))
    with pytest.raises(controls.DiagnosticDenied) as failure: diagnostic.run_live(request, live_opt_in=True)
    assert failure.value.code == 'CREDENTIAL_PROFILE_MISMATCH'
    row = ledger_rows(tmp_path/'attempts.sqlite')[0]
    assert row[1] == 'FAILED' and row[-2:] == (1, 'UNKNOWN')
    assert b'synthetic-batch-key' not in (tmp_path/'attempts.sqlite').read_bytes()


def test_single_campaign_runner_and_no_parallel_batch_dispatch(batched, monkeypatch, tmp_path):
    request, _, _, _ = batched()
    entered, release = Event(), Event()
    calls = transport(monkeypatch, tmp_path, request)
    factory = diagnostic._create_live_transport
    def handle(actual):
        if not calls:
            entered.set()
            assert release.wait(10)
        return factory().handle_request(actual)
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: httpx.MockTransport(handle))
    # Capture the original factory: it closes over the real fixture handler.
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(diagnostic.run_live, request, live_opt_in=True)
        try:
            assert entered.wait(10)
            with pytest.raises(controls.DiagnosticDenied) as failure:
                diagnostic.run_live(request, live_opt_in=True)
            assert failure.value.code == 'RUNNER_CONFLICT' and calls == []
        finally:
            release.set()
        assert first.result(timeout=20).vector_count == 54
    assert len(calls) == 6


@pytest.mark.parametrize('mode', ['missing', 'duplicate', 'unexpected', 'order', 'dimension', 'nan', 'infinity', 'zero', 'norm'])
def test_final_validation_rejects_missing_corrupt_or_reordered_vectors(batched, mode):
    request, _, _, _ = batched()
    vectors = [(c.chunk_identity, (1.0,)+(0.0,)*767) for c in request.chunks]
    if mode == 'missing': vectors.pop()
    elif mode == 'duplicate': vectors[1] = vectors[0]
    elif mode == 'unexpected': vectors[0] = ('unexpected', vectors[0][1])
    elif mode == 'order': vectors.reverse()
    else:
        v = dict(dimension=(1.0,), nan=(float('nan'),)+(0.0,)*767,
                 infinity=(float('inf'),)+(0.0,)*767, zero=(0.0,)*768, norm=(2.0,)+(0.0,)*767)[mode]
        vectors[0] = (vectors[0][0], v)
    with pytest.raises(controls.DiagnosticDenied, match='54-to-54'):
        diagnostic._validate_global_batch_vectors(request, vectors, .01)


def test_paid_batching_preserves_existing_price_behavior(batched, monkeypatch, tmp_path):
    request, _, _, _ = batched(paid=True)
    calls = transport(monkeypatch, tmp_path, request)
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.vector_count == 54 and len(calls) == 6
    assert all(r[-2] > 0 for r in ledger_rows(tmp_path/'attempts.sqlite'))


@pytest.mark.parametrize('override', ['batch_plan', 'batch_size', 'usage', 'authorization', 'transport'])
def test_no_caller_plan_or_live_default(override):
    with pytest.raises(TypeError): diagnostic.run_live(**{override: 9}, live_opt_in=True)
    with pytest.raises(controls.DiagnosticDenied, match='opt-in'): diagnostic.run_live()


@pytest.mark.parametrize('mode', ['missing_authorization', 'no_opt_in', 'expired', 'plan_changed', 'source_changed'])
def test_campaign_authority_cannot_be_replaced_or_extended(batched, monkeypatch, tmp_path, mode):
    request, binding, value, _ = batched()
    if mode == 'missing_authorization':
        def denied(): raise controls.DiagnosticDenied('missing authorization')
        monkeypatch.setattr(diagnostic, 'load_live_binding', denied)
    elif mode == 'expired':
        rebind_plan(binding, value, monkeypatch, expires_at='2001-01-01T00:00:00+00:00')
    elif mode == 'plan_changed':
        # No matching PM record update: a caller cannot retarget a signed-off plan.
        value['sequential_batch_plan']['maximum_batch_tokens'] += 1
    elif mode == 'source_changed':
        monkeypatch.setattr(diagnostic, 'resolve_repository_source', lambda _: ('a'*40, True))
    monkeypatch.setattr(diagnostic, 'resolve_diagnostic_credential', lambda *_: pytest.fail('unauthorized credential discovery'))
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: pytest.fail('unauthorized send'))
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_live(request, live_opt_in=mode != 'no_opt_in')
    assert not ledger_rows(tmp_path/'attempts.sqlite')


@pytest.mark.parametrize('mode', ['raw_sdk_bytes', 'changed_source_after_credential'])
def test_final_guard_blocks_before_physical_send_and_retains_attempt(batched, monkeypatch, tmp_path, mode):
    request, _, value, _ = batched()
    if mode == 'raw_sdk_bytes': value['batch_sdk_evidence'][0]['sdk_body_sha256'] = '0'*64
    real_resolve = diagnostic.resolve_diagnostic_credential
    credential_calls = []
    def credential(*args):
        assert ledger_rows(tmp_path/'attempts.sqlite')[0][1] == 'IN_FLIGHT'
        credential_calls.append(True)
        result = real_resolve(*args)
        if mode == 'changed_source_after_credential':
            monkeypatch.setattr(diagnostic, 'resolve_repository_source', lambda _: ('a'*40, True))
        return result
    monkeypatch.setattr(diagnostic, 'resolve_diagnostic_credential', credential)
    monkeypatch.setattr(diagnostic, '_create_live_transport', lambda: pytest.fail('final guard bypass'))
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.outcome == 'FAILED' and result.provider_attempts == 0 and result.vector_count == 0
    assert credential_calls == [True] and ledger_rows(tmp_path/'attempts.sqlite')[0][-2:] == (1, 'UNKNOWN')
