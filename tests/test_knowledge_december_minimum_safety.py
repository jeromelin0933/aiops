"""Stage 1 safety simplification: temporary ledger, synthetic approvals, sealed I/O.

No test supplies a production source guard, credential or authority publication.
"""
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from knowledge_index import gemini_diagnostic as d, gemini_diagnostic_controls as c
from test_knowledge_gemini_query_counting import query_counting, counting, offline_sdk
from test_knowledge_december_fence_policy import composite, seed, rows, blocking
from test_knowledge_gemini_diagnostic_controls import safety_parse


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def minimum(query_counting, monkeypatch):
    f = query_counting
    grant = replace(f['grant'], campaign_id='dec17-s2-query-count-stage1-20261125-01',
        project_identity='gen-lang-client-0149178738',
        bounds_basis=c.DEMO_COUNTING_BOUNDS, cost_basis=c.DEMO_COUNTING_COST)
    value = f['value']
    value['artifact_revision'] = c.DEMO_COUNTING_SAFETY_REVISION
    value['project_identity'] = grant.project_identity
    value.pop('provider_limits')
    account = value['pricing_evidence']['account_reference']
    value['pricing_evidence'] = dict(account_reference=account, price_tier='FREE_TIER_TEXT_V1', currency='USD')
    value['free_tier_evidence'] = dict(
        human_account_confirmation=dict(reference='OFFLINE-HUMAN-ACCOUNT-NOT-AUTHORITY',
            confirmed_by='OFFLINE-HUMAN', confirmed_at='2026-11-25T15:00:00Z',
            billing_status='FREE_TIER_NO_LINKED_PAID_BILLING'),
        data_use_permission=dict(pm_approval_reference=grant.pm_approval_reference,
                                 authority_identity=grant.spec_lead_identity))
    value['validity'] = dict(not_before='2026-11-25T15:00:00Z',
                             expires_at='2026-12-17T15:59:00Z')
    f['grant'] = grant
    grant = replace(grant, december_demo_fence_review=composite(f, grant))
    approved = replace(f['approved'], campaign_id=grant.campaign_id)
    e4 = replace(f['e4'], campaign_id=grant.campaign_id,
                 authorization_record_sha256=c.authorization_record_digest(grant))
    value['binding'].update(campaign_id=grant.campaign_id, bounds_basis=grant.bounds_basis,
        cost_basis=grant.cost_basis, authorization_record_sha256=c.authorization_record_digest(grant))
    f.update(grant=grant, approved=approved, e4=e4,
             binding=replace(f['binding'], record=grant, approved_input=approved, sdk_byte_evidence=e4))
    monkeypatch.setattr(d, 'resolve_diagnostic_credential', c.resolve_diagnostic_credential)
    return f


def test_exact_stage1_with_history_reserves_before_one_mock_send_and_retains_holds(minimum, monkeypatch):
    f = minimum
    seed(f)
    before = rows(f)
    assert blocking(f, f['grant']) == 0
    def send(request):
        assert next(row for row in rows(f) if row[0] == f['grant'].campaign_id)[2] == 'IN_FLIGHT'
        assert request.content == f['body'] and str(request.url) == c.COUNTING_URL
        assert request.extensions['timeout'] == dict(connect=30., read=30., write=30., pool=30.)
        f['calls'].append(request)
        return httpx.Response(200, json={'totalTokens':128})
    monkeypatch.setattr(d, '_create_live_transport', lambda: httpx.MockTransport(send))
    result = d.run_query_counting_observation(live_opt_in=True)
    assert result.outcome == 'SUCCESS' and result.observed_token_count == 128 and result.usage == 'UNKNOWN'
    assert len(f['calls']) == 1 and all(row in rows(f) for row in before)
    assert next(row for row in rows(f) if row[0] == f['grant'].campaign_id)[4:7] == (1, 1, 'UNKNOWN')
    assert blocking(f, f['grant']) == 1
    with pytest.raises(c.DiagnosticDenied):
        d.run_query_counting_observation(live_opt_in=True)
    assert len(f['calls']) == 1


@pytest.mark.parametrize('path,value', [
    (('operation',), c.COUNTING_OPERATION),
    (('project_identity',), 'another-project'),
    (('binding','campaign_id'), 'other-campaign'),
    (('binding','model'), 'text-embedding-004'),
    (('binding','source_revision'), 'a'*40),
    (('binding','authorization_record_sha256'), 'a'*64),
    (('binding','bounds_basis'), 'counting-minimum-v1'),
    (('binding','cost_basis'), 'counting-free-v1'),
    (('query_binding','generation'), 2),
    (('query_binding','envelope_sha256'), 'a'*64),
    (('e4_reference','sdk_body_sha256'), 'a'*64),
    (('e4_reference','canonical_json_sha256'), 'a'*64),
    (('credential_binding','secret_revision'), 'other'),
    (('free_tier_evidence','data_use_permission','pm_approval_reference'), 'other'),
    (('free_tier_evidence','data_use_permission','authority_identity'), 'other'),
    (('free_tier_evidence','human_account_confirmation','billing_status'), 'PAID'),
    (('free_tier_evidence','human_account_confirmation','confirmed_at'), '2099-01-01T00:00:00+00:00'),
    (('validity','expires_at'), '2099-01-01T00:00:00+00:00'),
    (('lifecycle_state',), 'REVOKED'),
])
def test_drift_rejected_before_reservation_or_credential(minimum, path, value):
    f = minimum
    target = f['value']
    for key in path[:-1]: target = target[key]
    target[path[-1]] = value
    with pytest.raises(c.DiagnosticDenied):
        d.run_query_counting_observation(live_opt_in=True)
    assert not f['rows']() and not f['calls'] and not f['credentials']


@pytest.mark.parametrize('extra', ['provider_limits', 'token_evidence', 'accepted_input_token_bounds',
                                  'sequential_batch_plan', 'full54_bounded_risk'])
def test_no_token_price_or_embedding_authority_inheritance(minimum, extra):
    value = deepcopy(minimum['value'])
    value[extra] = {}
    with pytest.raises(c.DiagnosticDenied): safety_parse(value)


@pytest.mark.parametrize('fault,expected', [('timeout','TIMEOUT'), ('loss','UNKNOWN'), ('http','UNKNOWN')])
def test_error_keeps_unknown_usage_hold_and_duplicate_fence(minimum, monkeypatch, fault, expected):
    f = minimum
    seed(f)
    def send(request):
        f['calls'].append(request)
        if fault == 'timeout': raise httpx.ReadTimeout('offline')
        if fault == 'loss': raise httpx.ReadError('offline')
        return httpx.Response(503, json={'error': {'code':503,'message':'offline'}})
    monkeypatch.setattr(d, '_create_live_transport', lambda: httpx.MockTransport(send))
    result = d.run_query_counting_observation(live_opt_in=True)
    assert result.outcome == expected
    assert next(row for row in rows(f) if row[0] == f['grant'].campaign_id)[4:7] == (1,1,'UNKNOWN')
    with pytest.raises(c.DiagnosticDenied): d.run_query_counting_observation(live_opt_in=True)
    assert len(f['calls']) == 1


def test_final_safety_revalidation_stops_before_transport(minimum, monkeypatch):
    f = minimum
    seed(f)
    resolver = d.resolve_diagnostic_credential
    def changed(*args):
        credential = resolver(*args)
        f['value']['lifecycle_state'] = 'REVOKED'
        return credential
    monkeypatch.setattr(d, 'resolve_diagnostic_credential', changed)
    result = d.run_query_counting_observation(live_opt_in=True)
    assert result.outcome != 'SUCCESS' and not f['calls']


def test_no_implicit_profile_downgrade(minimum):
    value = deepcopy(minimum['value'])
    value['artifact_revision'] = 'legacy'
    with pytest.raises(c.DiagnosticDenied): safety_parse(value)
