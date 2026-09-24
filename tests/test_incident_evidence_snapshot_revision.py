from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from incident_evidence import (
    CaptureTerminalKind,
    EvidenceRevision,
    MATERIALITY_RULE_V1,
    MaterialityEvaluationKind,
    MaterialityJudgement,
    MaterialityRequest,
    canonical_json,
    compare_materiality,
)
from incident_management import IncidentStatus
from test_incident_evidence_capture import capture_command, capture_service
from test_incident_evidence_trusted_core import incident


UTC = timezone.utc


def test_snapshot_is_immutable_and_revision_is_reused_for_same_semantics(tmp_path):
    service, *_ = capture_service(tmp_path)
    first = service.capture_evidence(capture_command("CAP-1"))
    second = service.capture_evidence(capture_command("CAP-2"))
    assert first.terminal_kind is CaptureTerminalKind.SUCCESS
    assert first.snapshot_id != second.snapshot_id
    assert first.revision_id == second.revision_id
    snapshot = service.resolve_snapshot(first.snapshot_id)
    with pytest.raises(FrozenInstanceError):
        snapshot.revision_id = "replacement"


def test_semantic_evidence_change_creates_a_different_revision(tmp_path):
    first_service, *_ = capture_service(tmp_path, marker="1")
    first = first_service.capture_evidence(capture_command("CAP-1"))
    second_service, *_ = capture_service(tmp_path, store=first_service._store, marker="2")
    second = second_service.capture_evidence(capture_command("CAP-2"))
    assert first.revision_id != second.revision_id
    materiality = compare_materiality(
        first_service._store,
        MaterialityRequest(
            MaterialityEvaluationKind.PAIRWISE,
            second.revision_id,
            MATERIALITY_RULE_V1,
            first.revision_id,
        ),
    )
    assert materiality.judgement is MaterialityJudgement.MATERIAL


def test_workflow_only_incident_change_preserves_revision_and_is_not_material(tmp_path):
    service, incident_reader, *_ = capture_service(tmp_path)
    first = service.capture_evidence(capture_command("CAP-1"))
    incident_reader.value = incident(
        status=IncidentStatus.ASSIGNED,
        updated_at=datetime(2026, 9, 22, 1, 3, tzinfo=UTC),
    )
    second = service.capture_evidence(capture_command("CAP-2"))

    assert first.snapshot_id != second.snapshot_id
    assert first.revision_id == second.revision_id
    assert service.resolve_snapshot(first.snapshot_id).snapshot_content[
        "incident_projection"
    ]["status"] == "OPEN"
    assert service.resolve_snapshot(second.snapshot_id).snapshot_content[
        "incident_projection"
    ]["status"] == "ASSIGNED"

    materiality = compare_materiality(
        service._store,
        MaterialityRequest(
            MaterialityEvaluationKind.PAIRWISE,
            second.revision_id,
            MATERIALITY_RULE_V1,
            first.revision_id,
        ),
    )
    assert materiality.judgement is MaterialityJudgement.SAME


def test_revision_identity_cannot_alias_different_semantics(tmp_path):
    service, *_ = capture_service(tmp_path)
    outcome = service.capture_evidence(capture_command())
    revision = service.resolve_revision(outcome.revision_id)
    changed = revision.semantic_content
    changed["completeness"] = "DEGRADED"
    with pytest.raises(ValueError, match="revision_id contradicts"):
        EvidenceRevision(
            revision.revision_id,
            revision.incident_id,
            revision.canonicalization_version,
            canonical_json(changed),
            revision.integrity_identity,
        )
