"""Query-count readiness boundaries; no new live purpose is authorized here.

The existing document campaign cannot be repurposed as Query counting. These
tests preserve that fence while the separate controls/governance scope is
reviewed. SDK conversion evidence is serialization only, not live admission.
"""

from dataclasses import replace
from hashlib import sha256
import json

import pytest

import knowledge_index.gemini_diagnostic as diagnostic
import knowledge_index.gemini_diagnostic_controls as controls
from knowledge_index.contracts import (
    ApplicabilityPolicy, KnowledgeValidationError, RetrievalApplicability,
    RetrievalScoreDirection,
)
from knowledge_index.retrieval_resolution import _applicability
from test_knowledge_gemini_diagnostic_controls import record
from test_knowledge_gemini_diagnostic_counting import counting  # noqa: F401
from test_knowledge_gemini_embedding2_serialization import offline_sdk  # noqa: F401


PROPOSED_OPERATION = "S2_QUERY_COUNTING_OBSERVATION_V1"
QUERY = (
    'Observed incident event facts (incident-evidence-query-v1): '
    '{"additional_distinct_event_facts":0,"observed_event_facts":'
    '[{"event_source":"log_event_detection","event_type":"cross_service_failure",'
    '"service_name":"api-gateway","severity":"HIGH"}]}'
)
ENVELOPE = "task: search result | query: " + QUERY
SCOPES = tuple(sorted((
    "component_service_scope", "dependency_role_scope",
    "operational_domain", "problem_failure_class",
)))


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("operation", [
    "S2_QUERY_COUNTING_OBSERVATION_V2", "COUNTING_QUERY_OBSERVATION", "COUNTING_OBSERVATION@query-v1",
])
def test_unapproved_query_purpose_denied_before_host_source_or_credentials(monkeypatch, operation):
    touched = []

    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("No host, Git source guard, credential or transport access")

    for name in ("load_live_binding", "load_live_safety_artifact", "resolve_repository_source",
                 "resolve_diagnostic_credential", "LiveDiagnosticUsage", "_create_live_transport"):
        monkeypatch.setattr(diagnostic, name, forbidden)
    with pytest.raises(controls.DiagnosticDenied) as exc:
        diagnostic.run_live(operation=operation, live_opt_in=True)
    assert exc.value.code == "OPERATION_MISMATCH" and touched == []


def test_query_counting_purpose_requires_exact_scope_not_runner_alias():
    with pytest.raises(controls.DiagnosticDenied, match="scope"):
        record(purpose=PROPOSED_OPERATION)


def test_query_request_cannot_reinterpret_historical_document_operation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Document counting must not dispatch with caller query input")

    monkeypatch.setattr(diagnostic, "run_counting_observation", forbidden)
    with pytest.raises(controls.DiagnosticDenied) as exc:
        diagnostic.run_live(object(), operation=controls.COUNTING_OPERATION, live_opt_in=True)
    assert exc.value.code == "INPUT_MISMATCH"


@pytest.mark.parametrize("changes", [
    {"input_kind": "query"},
    {"input_commitment": sha256(ENVELOPE.encode()).hexdigest()},
    {"maximum_item_bytes": len(ENVELOPE.encode())},
])
def test_document_counting_dto_retains_exact_input_bounds(counting, changes):
    with pytest.raises(controls.DiagnosticDenied) as exc:
        replace(counting["grant"], **changes)
    assert exc.value.code == "INPUT_MISMATCH"
    assert not counting["credentials"] and not counting["rows"]() and not counting["calls"]


def test_query_count_route_is_not_current_host_e4_admission(counting):
    candidate = replace(counting["e4"], kind="query")
    with pytest.raises(controls.DiagnosticDenied):
        candidate.matches_record_input(counting["grant"], counting["approved"])
    assert not counting["credentials"] and not counting["rows"]() and not counting["calls"]


def test_exact_query_sdk_serialization_does_not_grant_document_admission(counting, monkeypatch):
    contents, body, route = diagnostic._prepare_counting_sdk_request(ENVELOPE)
    assert route == {"model": "models/gemini-embedding-2"}
    assert len(contents) == 1 and len(contents[0].parts) == 1
    assert json.loads(body) == {"contents": [{"parts": [{"text": ENVELOPE}], "role": "user"}]}
    assert sha256(ENVELOPE.encode()).hexdigest() == "592c472d99ccc7f9f38e4402857a8e2893b08c5210babd9e2b0480ab8644f339"
    assert sha256(body).hexdigest() == "c6f53df959cf856ac59ad6f851079a32b691bc2869f81a6ebb87f344bb6ab52f"

    def forbidden(*args, **kwargs):
        raise AssertionError("Serialization cannot authorize source/credential/transport")

    monkeypatch.setattr(diagnostic, "resolve_repository_source", forbidden)
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", forbidden)
    with pytest.raises(controls.DiagnosticDenied) as exc:
        diagnostic._serialize_counting(ENVELOPE)
    assert exc.value.code == "PAYLOAD_MISMATCH"
    assert not counting["credentials"] and not counting["rows"]() and not counting["calls"]


def test_query_approved_input_rejected_before_source_guard(counting, monkeypatch):
    approved = replace(counting["approved"], kind="query", envelopes=(ENVELOPE,))
    binding = replace(counting["binding"], approved_input=approved)

    def forbidden(*args, **kwargs):
        raise AssertionError("Query/document mismatch must precede Git source guard")

    monkeypatch.setattr(diagnostic, "resolve_repository_source", forbidden)
    with pytest.raises(controls.DiagnosticDenied) as exc:
        diagnostic._prepare_counting(binding, None)
    assert exc.value.code == "INPUT_MISMATCH"
    assert not counting["rows"]() and not counting["credentials"] and not counting["calls"]


@pytest.mark.parametrize("score", [0.0, 0.3, 100.0])
def test_missing_scope_precedes_score_but_does_not_calibrate_policy(score):
    # Synthetic unit policy only: not a Gemini2 calibration or Live profile.
    policy = ApplicabilityPolicy("offline-query-count-scope-test", "1", SCOPES, 0., 0., 0.)
    metadata = {name: "synthetic" for name in SCOPES}
    assert _applicability(score, metadata, {}, policy,
                          RetrievalScoreDirection.LOWER_IS_BETTER) is RetrievalApplicability.NONE


def test_unresolved_thresholds_are_not_loadable_approved_policy():
    with pytest.raises(KnowledgeValidationError, match="finite"):
        ApplicabilityPolicy("unapproved-december-candidate", "1", SCOPES, None, None, None)
