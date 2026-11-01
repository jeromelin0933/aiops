"""Offline public Evidence -> query composition; no SOP-derived scope guesses."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from incident_evidence import EvidenceSnapshot, EvidenceRevision
from knowledge_index import (CanonicalKnowledgeQuery, KnowledgeReadResult, KnowledgeReadStatus,
    OpaqueExternalReference, OpaqueReferenceType, RetrievalResolution)
from runtime_orchestration.knowledge_query import MAPPING_VERSION, query_from_evidence, bind_evidence_query
from runtime_orchestration.rca_initial import InitialRcaOrchestrator, InitialRcaDisposition
from runtime_orchestration.rca_runtime_actions import RcaRuntimeActions
from runtime_orchestration.rca_continuation import SqliteRcaContinuationStore
from _incident_evidence_store_testkit import success
from _knowledge_retrieval_testkit import request
from test_spec_016_initial_composition import _public_services, _run


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    import socket
    import httpx
    from knowledge_index import gemini_diagnostic as diagnostic
    from knowledge_index import gemini_diagnostic_controls as controls
    def forbidden(*a, **kw):pytest.fail('no real network, credential or formal store access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(diagnostic, 'resolve_diagnostic_credential', forbidden)
    monkeypatch.setattr(controls, '_localappdata_known_folder', forbidden)
    monkeypatch.setattr(controls, '_programdata_known_folder', forbidden)


def snapshot(events):
    base = success(incident_id='INC-1')
    content = base.snapshot.snapshot_content
    content['event_projections'] = events
    content['semantic_evidence']['events'] = events
    revision = EvidenceRevision.from_content(incident_id='INC-1',
        canonicalization_version=base.command.canonicalization_version,
        semantic_content=content['semantic_evidence'])
    return EvidenceSnapshot.from_content(base.command, revision_id=revision.revision_id,
        completeness=base.snapshot.completeness, source_statuses=base.snapshot.source_statuses,
        snapshot_content=content)


def event(**changes):
    return dict(event_id='EVT-1', event_source='metrics_threshold_detection',
        event_type='high_latency_detected', severity='HIGH',
        selector_values=[['service_name','payments-api']], **changes)


def test_supported_public_facts_canonical_query_and_provenance():
    evidence = snapshot([event()])
    query = query_from_evidence('INC-1', evidence)
    assert query == query_from_evidence('INC-1', evidence)
    assert query.filters == ()  # No approved event-to-four-scope crosswalk.
    assert query.schema_version == query.canonicalization_version == '1.0'
    assert 'high_latency_detected' in query.text and 'payments-api' in query.text
    assert 'EVT-1' not in query.text and 'INC-1' not in query.text
    before = replace(request(), query=CanonicalKnowledgeQuery('1.0','1.0','INC-1'))
    mapped = bind_evidence_query(before, 'INC-1', evidence,
                                KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND))
    assert mapped.query == query and mapped.operation_key == before.operation_key
    assert OpaqueExternalReference(OpaqueReferenceType.EVIDENCE_SNAPSHOT,evidence.snapshot_id) in mapped.external_references
    assert OpaqueExternalReference(OpaqueReferenceType.CALLER,MAPPING_VERSION) in mapped.external_references
    assert mapped.retrieval_profile == before.retrieval_profile
    assert mapped.applicability_policy == before.applicability_policy


@pytest.mark.parametrize('events', [
    [{'event_id':'EVT-1'}],
    [{'event_source':'unsupported','event_type':'database_failure'}],
    [{'event_source':'metrics_threshold_detection','event_type':['high_latency_detected']}],
])
def test_missing_or_unsupported_observations_never_invent_classification(events):
    query = query_from_evidence('INC-1', snapshot(events))
    assert json.loads(query.text.split(': ',1)[1])['observed_event_facts'] == []
    assert query.filters == ()


def test_partial_ambiguous_and_forbidden_fields_are_not_query_authority():
    value = event()
    value['selector_values'] = [['service_name','one'],['service_name','two']]
    value.update(operational_domain='pretend-approved', document_id='target-document')
    query = query_from_evidence('INC-1',snapshot([value]))
    facts = json.loads(query.text.split(': ',1)[1])['observed_event_facts']
    assert facts[0]['service_name'] == '' and query.filters == ()
    assert all(word not in query.text for word in ('S2-ORACLE','TARGET-SOP','target-document','pretend-approved','Ground Truth'))
    # SPEC-013 itself rejects evaluation-only fields before a public Snapshot
    # can reach the mapper. Do not weaken that boundary to construct a fixture.
    from incident_evidence.errors import EvidenceDomainError
    for key in ('scenario_id','expected_answer','ground_truth','validator_expected_answer'):
        with pytest.raises(EvidenceDomainError):snapshot([dict(value, **{key:'TARGET-SOP'})])
    for services in (None, [['service_name','scenario-oracle']], [['service_name','safe'],['other','ignore']]):
        value['selector_values'] = services
        text = query_from_evidence('INC-1',snapshot([value])).text
        assert 'scenario-oracle' not in text and 'ignore' not in text


def test_no_evidence_or_wrong_incident_stops_before_retrieval():
    for value in (None, snapshot([event()])):
        with pytest.raises(ValueError):query_from_evidence('another-incident', value)


def test_order_deduplication_and_whole_fact_bound():
    events=[]
    for i in range(12):
        value=event();value['selector_values']=[['service_name',f'service-{i:02}']]
        events.append(value)
    query=query_from_evidence('INC-1',snapshot(events+events))
    assert query==query_from_evidence('INC-1',snapshot(list(reversed(events))))
    payload=json.loads(query.text.split(': ',1)[1])
    assert len(payload['observed_event_facts'])==8 and payload['additional_distinct_event_facts']==4


def test_frozen_mapped_and_legacy_operations_are_unchanged_and_contradictions_stop():
    evidence=snapshot([event()])
    legacy=replace(request(),query=CanonicalKnowledgeQuery('1.0','1.0','INC-1'))
    def found(r):return KnowledgeReadResult(KnowledgeReadStatus.FOUND,
        SimpleNamespace(frozen=SimpleNamespace(request=r)))
    assert bind_evidence_query(legacy,'INC-1',evidence,found(legacy))==legacy
    mapped=bind_evidence_query(legacy,'INC-1',evidence,KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND))
    assert bind_evidence_query(legacy,'INC-1',evidence,found(mapped))==mapped
    wrong=replace(mapped,query=CanonicalKnowledgeQuery('1.0','1.0','unrelated query'))
    with pytest.raises(ValueError):bind_evidence_query(legacy,'INC-1',evidence,found(wrong))
    with pytest.raises(OSError):bind_evidence_query(legacy,'INC-1',evidence,KnowledgeReadResult(KnowledgeReadStatus.UNAVAILABLE))


@pytest.mark.parametrize('legacy_first', [False, True])
def test_real_initial_and_followup_share_mapping_public_snapshot_and_replay(tmp_path, legacy_first):
    incidents, events, b, evidence, adapters, a, cstore, knowledge, provider = _public_services(tmp_path)
    d2=SqliteRcaContinuationStore(tmp_path/'runtime.sqlite3')
    observed=[]
    class C:
        def __getattr__(self,name):return getattr(knowledge,name)
        def resolve(self,r,limits):
            observed.append(r)
            return knowledge.resolve(r,limits)
    c=C()
    initial=InitialRcaOrchestrator(incidents=incidents,candidate_a=a,candidate_b=evidence,
        candidate_c=c,continuations=d2,derive_knowledge_query=not legacy_first)
    inputs=[]
    class RuntimeInitial:
        def run(self,r):
            legacy=replace(r.retrieval_request,query=CanonicalKnowledgeQuery('1.0','1.0',r.incident_id),
                external_references=(OpaqueExternalReference(OpaqueReferenceType.CALLER,'spec016-runtime'),))
            r=replace(r,retrieval_request=legacy);inputs.append(r)
            return initial.run(r)
    try:
        result=_run(RuntimeInitial(),evidence)
        assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
        captured=evidence.resolve_snapshot(result.evidence.snapshot_id)
        if not legacy_first:
            assert 'brute_force_detected' in observed[0].query.text
            assert 'auth-api' in observed[0].query.text
            assert observed[0].query==query_from_evidence('INC-S2',captured)
        else:
            initial=InitialRcaOrchestrator(incidents=incidents,candidate_a=a,candidate_b=evidence,
                candidate_c=c,continuations=d2,derive_knowledge_query=True)
            assert observed[0].query.text=='INC-S2'
        assert observed[0].query.filters==()
        # High synthetic similarity does NOT bypass missing required context.
        assert result.knowledge.resolution is RetrievalResolution.NO_MATCH
        actions=object.__new__(RcaRuntimeActions)
        actions._c=c
        actions._knowledge_config=SimpleNamespace(retrieval_profile=observed[0].retrieval_profile,
            applicability_policy=observed[0].applicability_policy,
            capability=SimpleNamespace(profile_reference=observed[0].profile_reference,
                capability_identity=observed[0].capability_identity),limits=inputs[0].retrieval_limits)
        followup=actions._knowledge_for_snapshot('INC-S2',captured)
        assert observed[0]==observed[1] and followup==result.knowledge.snapshot
        assert initial.run(inputs[0]).knowledge.snapshot==followup
        if not legacy_first:
            assert OpaqueExternalReference(OpaqueReferenceType.EVIDENCE_SNAPSHOT,captured.snapshot_id) in observed[0].external_references
        assert knowledge.read_snapshot(followup.snapshot_key).value==followup
        assert followup.external_references == observed[0].external_references
        assert provider.calls == 1
    finally:
        d2.close();incidents.close();b.close();a.close();cstore.close()
