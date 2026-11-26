"""Exact Stage 2 admission using synthetic publications and the native TEMP ledger.

All source/credential/transport seams are test-only replacements. No formal
ledger, source resolver, protected authority, Chroma client or network is used.
"""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import sqlite3

import httpx
import pytest

from knowledge_index import gemini_diagnostic as d, gemini_diagnostic_controls as c
from test_knowledge_gemini_query_counting import query_counting, counting, offline_sdk
from test_knowledge_query_count_success_review import reviewed, stage2
from test_knowledge_december_fence_policy import composite, seed, rows, blocking
from test_knowledge_gemini_diagnostic_controls import safety_parse


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def query_demo(query_counting, monkeypatch):
    f = query_counting
    grant = replace(f['grant'], project_identity='gen-lang-client-0149178738')
    e4 = replace(f['e4'], authorization_record_sha256=c.authorization_record_digest(grant))
    f['value']['project_identity'] = grant.project_identity
    f['value']['binding']['authorization_record_sha256'] = c.authorization_record_digest(grant)
    f.update(grant=grant, e4=e4, binding=replace(f['binding'], record=grant, sdk_byte_evidence=e4))
    # Produce a genuinely serialized counting observation, using MockTransport.
    f = reviewed.__wrapped__(f)
    seed(f)

    def prepare(name='offline-stage2-one'):
        base = replace(f['target'], campaign_id=name, pm_approval_reference='OFFLINE-GRANT-'+name,
            query_count_success_review=None, december_demo_fence_review=None,
            project_identity=None, credential_profile_revision=None,
            bounds_basis=c.DEMO_QUERY_BOUNDS, cost_basis=c.DEMO_QUERY_COST)
        review = composite(f, base, f['review'])
        target = replace(base, december_demo_fence_review=review,
            project_identity=review.project_identity, credential_profile_revision=review.credential_profile_revision)
        f['target'] = target
        # The existing fixture's pure SDK observer needs this campaign's facts.
        import test_knowledge_query_count_success_review as legacy
        from test_knowledge_gemini_diagnostic_controls import facts
        monkeypatch.setattr(legacy, 'facts', lambda **kw: facts(pm_approval_reference=target.pm_approval_reference, **kw))
        request, value, credentials, calls = stage2(f, monkeypatch)
        value.pop('provider_limits')
        value.update(artifact_revision=c.DEMO_QUERY_SAFETY_REVISION,
            operation='CONTROLLED_PROVIDER_DIAGNOSTIC', project_identity=target.project_identity,
            query_binding=c.query_counting_binding(), outbound_eligibility='APPROVED_SYNTHETIC_NON_SECRET')
        value['pricing_evidence'] = dict(account_reference=review.account_reference,
            price_tier='FREE_TIER_TEXT_V1', currency='USD')
        value['free_tier_evidence'] = dict(
            human_account_confirmation=dict(reference='OFFLINE-ACCOUNT-CONFIRMATION',
                confirmed_by='OFFLINE-HUMAN', confirmed_at='2026-11-25T15:00:00Z',
                billing_status='FREE_TIER_NO_LINKED_PAID_BILLING'),
            data_use_permission=dict(pm_approval_reference=target.pm_approval_reference,
                                     authority_identity=target.spec_lead_identity))
        value['validity'] = dict(not_before='2026-11-25T15:00:00Z', expires_at='2026-12-17T15:59:00Z')
        value['token_evidence']['source']['content_sha256'] = review.counting_success_review.observation_sha256
        return request, value, credentials, calls
    f['prepare_demo'] = prepare
    f['current'] = prepare()
    return f


def test_exact_query_native_admission_and_vector_return(query_demo):
    f = query_demo; request, value, credentials, calls = f['current']
    before = rows(f)
    proof, basis, reserved, hold = d._safety_from_artifact(f['target'], (c.QUERY_COUNTING_ENVELOPE,), safety_parse(value))
    assert proof.count_tokens(c.QUERY_COUNTING_ENVELOPE) == reserved == 300 and hold == 1
    assert basis.price_micro_units_per_million_tokens is None
    assert basis.provider_item_limit is basis.provider_item_token_limit is None
    result = d.embed_query_for_retrieval(request, live_opt_in=True)
    assert len(result.vector) == 768 and len(calls) == len(credentials) == result.invocation_count == 1
    assert all(row in rows(f) for row in before)
    with pytest.raises(c.DiagnosticDenied): d.embed_query_for_retrieval(request, live_opt_in=True)
    assert len(calls) == 1


def test_filters_rejected_before_credential_or_reservation(query_demo):
    from knowledge_index import QueryFilter
    f = query_demo; request, _, credentials, calls = f['current']; before = rows(f)
    changed = replace(request, query=replace(request.query,
        filters=(QueryFilter('component_service_scope', 'not-approved'),)))
    with pytest.raises(c.DiagnosticDenied): d.run_live(changed, live_opt_in=True)
    assert rows(f) == before and not credentials and not calls


@pytest.mark.parametrize('path,value', [
    (('project_identity',), 'other'),
    (('operation',), c.QUERY_COUNTING_OPERATION),
    (('query_binding','generation'), 2),
    (('query_binding','embedding_profile'), 'text-embedding-004'),
    (('binding','source_revision'), 'f'*40),
    (('binding','authorization_record_sha256'), 'f'*64),
    (('binding','cost_basis'), 'legacy-price'),
    (('binding','campaign_id'), 'another-campaign'),
    (('credential_binding','secret_revision'), 'different'),
    (('pricing_evidence','account_reference'), 'another-account'),
    (('free_tier_evidence','human_account_confirmation','billing_status'), 'PAID'),
    (('free_tier_evidence','data_use_permission','pm_approval_reference'), 'wrong-grant'),
    (('token_evidence','source','content_sha256'), 'f'*64),
    (('token_evidence','implementation_sha256'), 'f'*64),
    (('token_evidence','data_sha256'), 'f'*64),
    (('token_evidence','method_id'), 'BYTES_ARE_TOKENS'),
    (('e4_reference','sdk_body_sha256'), 'f'*64),
    (('lifecycle_state',), 'REVOKED'),
])
def test_mismatch_rejected_before_reservation_and_credentials(query_demo, path, value):
    f = query_demo; request, metadata, credentials, calls = f['current']; before = rows(f)
    target = metadata
    for key in path[:-1]: target = target[key]
    target[path[-1]] = value
    with pytest.raises(c.DiagnosticDenied): d.run_live(request, live_opt_in=True)
    assert rows(f) == before and not credentials and not calls


@pytest.mark.parametrize('bound', [None, {}, {'f'*64:300}, {sha256(c.QUERY_COUNTING_ENVELOPE.encode()).hexdigest():8193}])
def test_no_implicit_risk_or_byte_token_proof(query_demo, bound):
    f = query_demo; request, value, credentials, calls = f['current']
    value['accepted_input_token_bounds'] = bound
    with pytest.raises(c.DiagnosticDenied): d.run_live(request, live_opt_in=True)
    assert not credentials and not calls


def test_repeatable_new_campaigns_reuse_one_immutable_count_proof(query_demo):
    f = query_demo; before = rows(f)
    for name in ('offline-stage2-one', 'offline-stage2-two', 'offline-stage2-three'):
        request, value, credentials, calls = f['prepare_demo'](name)
        outcome = d.run_live(request, live_opt_in=True)
        assert outcome.outcome == 'SUCCESS' and len(calls) == 1 and len(credentials) == 1
        assert all(row in rows(f) for row in before)
    assert len(f['calls']) == 1  # the independent original Counting mock only


@pytest.mark.parametrize('fault,expected', [('timeout','TIMEOUT'), ('loss','UNKNOWN'), ('http','FAILED'), ('unknown-usage','SUCCESS')])
def test_faults_keep_real_usage_state_and_block_new_campaign(query_demo, monkeypatch, fault, expected):
    f = query_demo; request, value, credentials, calls = f['current']
    def send(request):
        calls.append(True)
        if fault == 'timeout': raise httpx.ReadTimeout('offline')
        if fault == 'loss': raise httpx.ReadError('offline')
        if fault == 'http': return httpx.Response(503, json={'error': {'code':503,'message':'offline'}})
        return httpx.Response(200, json={'embeddings':[{'values':[1.]+[0.]*767}]})
    monkeypatch.setattr(d, '_create_live_transport', lambda:httpx.MockTransport(send))
    result = d.run_live(request, live_opt_in=True)
    assert result.outcome == expected and len(calls) == 1
    assert result.billing_status == 'INTERNAL_HOLD_NOT_PROVIDER_PRICE_OR_COST_CAP'
    assert result.admission_usage_basis == 'DEMO_FIXED_QUERY_TOKEN_BOUND_CAMPAIGN_LIMITS_V1'
    own = next(row for row in rows(f) if row[0] == f['target'].campaign_id)
    assert own[4:7] == (300,1,'UNKNOWN')
    next_request, _, next_credentials, next_calls = f['prepare_demo']('offline-next')
    with pytest.raises(c.DiagnosticDenied): d.run_live(next_request, live_opt_in=True)
    assert not next_credentials and not next_calls


def test_final_guard_observes_new_unknown_fence(query_demo, monkeypatch):
    f = query_demo; request, _, credentials, calls = f['current']
    resolver = d.resolve_diagnostic_credential
    def changed(*args):
        credential = resolver(*args)
        with sqlite3.connect(str(f['path'])) as db:
            db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('unreviewed','x','UNKNOWN',1,1,1)")
        return credential
    monkeypatch.setattr(d, 'resolve_diagnostic_credential', changed)
    assert d.run_live(request, live_opt_in=True).outcome != 'SUCCESS'
    assert len(credentials) == 1 and not calls


def test_unknown_usage_never_delivers_vector_to_retrieval(query_demo, monkeypatch):
    from knowledge_index.google_embedding_adapter import GoogleEmbeddingAdapter
    f = query_demo; request, _, credentials, calls = f['current']
    def send(request):
        calls.append(True)
        return httpx.Response(200, json={'embeddings':[{'values':[1.]+[0.]*767}]})
    monkeypatch.setattr(d, '_create_live_transport', lambda:httpx.MockTransport(send))
    provider = GoogleEmbeddingAdapter(None, request.capability, controlled_live_opt_in=True)
    result = provider.embed_query(request)
    assert not result.vector and result.failure_detail and len(calls) == len(credentials) == 1
    own = next(row for row in rows(f) if row[0] == f['target'].campaign_id)
    assert own[2] == 'SUCCESS' and own[6] == 'UNKNOWN'  # vector response != trusted usage


@pytest.mark.parametrize('field', ['token_evidence','accepted_input_token_bounds'])
def test_required_token_evidence_cannot_be_omitted(query_demo, field):
    value = deepcopy(query_demo['current'][1]); value.pop(field)
    with pytest.raises(c.DiagnosticDenied): safety_parse(value)
