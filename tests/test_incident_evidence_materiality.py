from __future__ import annotations

from dataclasses import replace
from copy import deepcopy

import pytest

from incident_evidence import (
    CaptureSuccess,
    EvidenceDomainError,
    EvidenceFailureKind,
    EvidenceRevision,
    EvidenceSnapshot,
    MaterialityEvaluationKind,
    MaterialityJudgement,
    MaterialityRequest,
    SqliteEvidenceStore,
    compare_materiality,
)

from _incident_evidence_store_testkit import success


RULE = "evidence-materiality-v1"


def _commit(store, operation, evidence, *, incident_id="incident-1"):
    return store.commit_success(
        success(operation, incident_id=incident_id, evidence=evidence)
    )


def _pair(baseline, candidate, rule=RULE):
    return MaterialityRequest(
        MaterialityEvaluationKind.PAIRWISE,
        candidate,
        rule,
        baseline,
    )


def test_explicit_no_baseline_is_not_null_pairwise(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "no-baseline.sqlite")
    revision_id = _commit(store, "capture-a", "A").revision_id
    request = MaterialityRequest(
        MaterialityEvaluationKind.NO_BASELINE, revision_id, RULE
    )
    result = compare_materiality(store, request)
    assert result.request.evaluation_kind is MaterialityEvaluationKind.NO_BASELINE
    assert result.request.baseline_revision_id is None
    assert result.judgement is None
    assert "NO_BASELINE" in result.reason_facts[0]


def test_revision_id_difference_does_not_itself_determine_materiality(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "different.sqlite")
    baseline = _commit(store, "capture-a", "same fact").revision_id
    # The incident context is immutable semantic input. A second operation with
    # identical semantic evidence reuses the same Revision, proving ID is derived
    # from facts rather than capture order.
    same = _commit(store, "capture-b", "same fact").revision_id
    changed = _commit(store, "capture-c", "changed fact").revision_id
    assert same == baseline
    assert changed != baseline
    assert compare_materiality(store, _pair(baseline, same)).judgement is MaterialityJudgement.SAME
    assert compare_materiality(store, _pair(baseline, changed)).judgement is MaterialityJudgement.MATERIAL


def test_different_revision_ids_can_be_non_material_under_v1(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "non-material.sqlite")
    original = success("capture-a", evidence="same fact")
    baseline = store.commit_success(original).revision_id

    candidate_base = success("capture-b", evidence="same fact")
    semantic = deepcopy(candidate_base.revision.semantic_content)
    snapshot_content = deepcopy(candidate_base.snapshot.snapshot_content)
    earlier_start = "2026-09-21T11:57:00Z"
    for source in ("LOKI", "PROMETHEUS"):
        semantic["collection_boundaries"][source]["start"] = earlier_start
        snapshot_content["windows"][source]["start"] = earlier_start
        snapshot_content["provenance"][source]["query"]["logical_window"]["start"] = earlier_start
        snapshot_content["provenance"][source]["collection"]["logical_window"]["start"] = earlier_start
    snapshot_content["semantic_evidence"] = semantic
    revision = EvidenceRevision.from_content(
        incident_id=candidate_base.revision.incident_id,
        canonicalization_version=candidate_base.revision.canonicalization_version,
        semantic_content=semantic,
    )
    snapshot = EvidenceSnapshot.from_content(
        candidate_base.command,
        revision_id=revision.revision_id,
        completeness=candidate_base.snapshot.completeness,
        source_statuses=candidate_base.snapshot.source_statuses,
        snapshot_content=snapshot_content,
    )
    candidate = store.commit_success(
        CaptureSuccess(candidate_base.command, snapshot, revision)
    ).revision_id

    assert candidate != baseline
    result = compare_materiality(store, _pair(baseline, candidate))
    assert result.judgement is MaterialityJudgement.NON_MATERIAL


def test_direct_a_to_c_is_evaluated_without_transitive_inference(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "direct.sqlite")
    a = _commit(store, "capture-a", "A").revision_id
    b = _commit(store, "capture-b", "B").revision_id
    c = _commit(store, "capture-c", "C").revision_id
    ab = compare_materiality(store, _pair(a, b))
    bc = compare_materiality(store, _pair(b, c))
    ac = compare_materiality(store, _pair(a, c))
    assert all(item.judgement is MaterialityJudgement.MATERIAL for item in (ab, bc, ac))
    assert ac.request.baseline_revision_id == a
    assert ac.request.candidate_revision_id == c
    assert ac.materiality_result_id not in {ab.materiality_result_id, bc.materiality_result_id}


def test_missing_cross_incident_and_unsupported_rule_are_typed(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "invalid.sqlite")
    first = _commit(store, "capture-a", "A", incident_id="incident-a").revision_id
    second = _commit(store, "capture-b", "B", incident_id="incident-b").revision_id
    cases = (
        (_pair("missing", first), EvidenceFailureKind.DANGLING_EVIDENCE_REFERENCE),
        (_pair(first, second), EvidenceFailureKind.MATERIALITY_REPAIR_REQUIRED),
        (_pair(first, first, "unknown-rule"), EvidenceFailureKind.UNSUPPORTED_MATERIALITY_RULE),
    )
    for request, kind in cases:
        with pytest.raises(EvidenceDomainError) as caught:
            compare_materiality(store, request)
        assert caught.value.kind is kind


def test_canonicalization_mismatch_is_durable_repair_required(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "canonicalization.sqlite")
    baseline_success = success("capture-a", evidence="A")
    baseline = store.commit_success(baseline_success).revision_id
    original = success("capture-b", evidence="B")
    changed_command = replace(original.command, canonicalization_version="canonical-v2")
    changed_revision = EvidenceRevision.from_content(
        incident_id=original.revision.incident_id,
        canonicalization_version="canonical-v2",
        semantic_content=original.revision.semantic_content,
    )
    changed_snapshot = EvidenceSnapshot.from_content(
        changed_command,
        revision_id=changed_revision.revision_id,
        completeness=original.snapshot.completeness,
        source_statuses=original.snapshot.source_statuses,
        snapshot_content=original.snapshot.snapshot_content,
    )
    candidate = store.commit_success(
        CaptureSuccess(changed_command, changed_snapshot, changed_revision)
    ).revision_id
    result = compare_materiality(store, _pair(baseline, candidate))
    assert result.judgement is MaterialityJudgement.REPAIR_REQUIRED
    assert store.read_materiality_result(result.materiality_result_id) == result
