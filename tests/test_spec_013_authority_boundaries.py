from __future__ import annotations

from dataclasses import fields

from incident_evidence import (
    CandidateBAuthorityHandoff,
    MaterialityEvaluationKind,
    MaterialityRequest,
    SqliteEvidenceStore,
    build_candidate_b_handoff,
    compare_materiality,
)

from _incident_evidence_store_testkit import success


def test_handoff_contains_only_candidate_b_authoritative_references(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "handoff.sqlite")
    outcome = store.commit_success(success())
    materiality = compare_materiality(
        store,
        MaterialityRequest(
            MaterialityEvaluationKind.NO_BASELINE,
            outcome.revision_id,
            "evidence-materiality-v1",
        ),
    )
    handoff = build_candidate_b_handoff(
        store,
        outcome.snapshot_id,
        materiality_result_id=materiality.materiality_result_id,
    )
    assert isinstance(handoff, CandidateBAuthorityHandoff)
    names = {field.name for field in fields(handoff)}
    assert names == {
        "snapshot_id",
        "revision_id",
        "completeness",
        "provenance_references",
        "materiality_result_id",
        "materiality_judgement",
    }
    forbidden = {"current", "attempt", "aggregate", "schedule", "retry", "rca"}
    assert not any(any(word in name.lower() for word in forbidden) for name in names)
    assert handoff.provenance_references


def test_materiality_request_requires_explicit_caller_baseline():
    names = {field.name for field in fields(MaterialityRequest)}
    assert names == {
        "evaluation_kind",
        "candidate_revision_id",
        "materiality_rule_version",
        "baseline_revision_id",
    }
