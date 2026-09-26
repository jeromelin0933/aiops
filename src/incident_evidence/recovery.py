"""Candidate-B local recovery, readiness, and reference-only handoff seams."""

from __future__ import annotations

from .contracts import (
    CandidateBAuthorityHandoff,
    EvidenceRecoveryFacts,
    EvidenceReadiness,
)
from .errors import EvidenceDomainError, EvidenceFailureKind
from .sqlite_store import SqliteEvidenceStore


def enumerate_recovery_facts(store: SqliteEvidenceStore) -> EvidenceRecoveryFacts:
    """Enumerate B-owned truth only; performs no scheduling or recovery work."""

    if not isinstance(store, SqliteEvidenceStore):
        raise TypeError("store must be a SqliteEvidenceStore")
    return store.enumerate_recovery_facts()


def validate_local_readiness(store: SqliteEvidenceStore) -> EvidenceReadiness:
    """Validate only the Candidate-B local store/authority boundary."""

    if not isinstance(store, SqliteEvidenceStore):
        raise TypeError("store must be a SqliteEvidenceStore")
    # Complete enumeration is part of readiness, not merely physical SQLite health.
    store.enumerate_recovery_facts()
    return EvidenceReadiness.READY


def build_candidate_b_handoff(
    store: SqliteEvidenceStore,
    snapshot_id: str,
    *,
    materiality_result_id: str | None = None,
) -> CandidateBAuthorityHandoff:
    """Build a reference-only DTO without selecting or mutating Candidate-A Current."""

    if not isinstance(store, SqliteEvidenceStore):
        raise TypeError("store must be a SqliteEvidenceStore")
    snapshot = store.resolve_snapshot(snapshot_id)
    if snapshot is None:
        raise EvidenceDomainError(
            EvidenceFailureKind.DANGLING_EVIDENCE_REFERENCE,
            "required Evidence Snapshot is missing",
            field_path="snapshot_id",
        )
    content = snapshot.snapshot_content
    provenance = content["provenance"]
    references = tuple(
        provenance[source]["query"]["query_semantic_identity"]
        for source in sorted(provenance)
    )

    result = None
    if materiality_result_id is not None:
        result = store.read_materiality_result(materiality_result_id)
        if result is None:
            raise EvidenceDomainError(
                EvidenceFailureKind.DANGLING_EVIDENCE_REFERENCE,
                "required Materiality Result is missing",
                field_path="materiality_result_id",
            )
        if result.request.candidate_revision_id != snapshot.revision_id:
            raise EvidenceDomainError(
                EvidenceFailureKind.MATERIALITY_REPAIR_REQUIRED,
                "Materiality Result does not describe the handoff Revision",
            )

    return CandidateBAuthorityHandoff(
        snapshot.snapshot_id,
        snapshot.revision_id,
        snapshot.completeness,
        references,
        result.materiality_result_id if result is not None else None,
        result.judgement if result is not None else None,
    )


__all__ = [
    "build_candidate_b_handoff",
    "enumerate_recovery_facts",
    "validate_local_readiness",
]
