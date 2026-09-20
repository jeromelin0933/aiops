import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from rca_persistence import (
    AdmitAttemptRequest,
    AdmittedRetryDisposition,
    ArtifactProvenance,
    ArtifactStatementKind,
    AttemptLineage,
    CreateAggregateRequest,
    DiagnosticConclusion,
    EvidenceCompleteness,
    EvidenceReference,
    EvidentialSupport,
    GenerationProvenance,
    GuidanceSource,
    KnowledgeReference,
    LogicalTryIdentity,
    LogicalTryOutcome,
    LogicalTryResultKind,
    PublicationTargetIdentity,
    RcaAction,
    RcaArtifact,
    RcaDomainError,
    RcaErrorCode,
    RcaHypothesis,
    SqliteRcaStore,
    VersionRole,
)


NOW = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
GENERATION = GenerationProvenance("provider", "model", "prompt", "config", "profile")


def artifact(summary: str = "Root cause α") -> RcaArtifact:
    evidence = EvidenceReference("E-1", ArtifactStatementKind.OBSERVED_FACT, "metric spike")
    knowledge = KnowledgeReference("K-1", "corpus", "index", "doc", "v1", "sec", "chunk")
    return RcaArtifact(
        summary,
        "SEV-2",
        DiagnosticConclusion.IDENTIFIED,
        (RcaHypothesis(1, "queue saturation", EvidentialSupport.HIGH, ("E-1",), (), ("K-1",), "aligned"),),
        (RcaAction("drain queue", GuidanceSource.SOP_BACKED, ("K-1",)),),
        (RcaAction("add capacity", GuidanceSource.MODEL_SUGGESTED),),
        ("limited sample",),
        EvidenceCompleteness.FULL,
        False,
        ArtifactProvenance("ES-1", "ER-1", "KS-1", (evidence,), (knowledge,), GENERATION),
    )


def seed(store: SqliteRcaStore, attempt_id: str = "ATT-1", *, successful: bool = True) -> None:
    if store.get_aggregate("AGG-1") is None:
        store.create_or_discover_aggregate(CreateAggregateRequest("OP-AGG", "AGG-1", "INC-1", NOW))
    lineage = AttemptLineage(attempt_id, "AGG-1", "ES-1", "ER-1", "KS-1", GENERATION)
    store.admit_attempt(AdmitAttemptRequest(f"OP-{attempt_id}", lineage, NOW))
    outcome = LogicalTryOutcome(
        LogicalTryIdentity(attempt_id, 1),
        LogicalTryResultKind.VALIDATED_RESULT if successful else LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.NON_RETRYABLE,
        NOW + timedelta(minutes=1),
        validated_result_id=f"VALID-{attempt_id}" if successful else None,
        failure_code=None if successful else "REJECTED",
    )
    store.record_try_outcome(f"OP-TRY-{attempt_id}", outcome)


def target(version: str = "VER-1", publication: str = "PUB-1") -> PublicationTargetIdentity:
    return PublicationTargetIdentity(publication, "AGG-1", "INC-1", version, None)


def commit(store: SqliteRcaStore, operation: str = "OP-COMMIT-1", version: str = "VER-1", publication: str = "PUB-1"):
    return store.commit_validated_artifact(operation, "ATT-1", artifact(), target(version, publication), NOW)


def test_atomic_happy_path_and_lossless_provenance_reads(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        version = commit(store)
        assert version.version_number == 1
        assert version.role is VersionRole.COMMITTED_UNPUBLISHED
        assert store.get_version("VER-1") == version
        assert store.get_version_history("AGG-1") == (version,)
        assert store.get_artifact("VER-1") == artifact()
        assert store.get_artifact_provenance("VER-1") == artifact().provenance
        assert version.artifact.summary == "Root cause α"


def test_failed_attempt_and_lineage_contradiction_allocate_nothing(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store, successful=False)
        with pytest.raises(RcaDomainError) as failed:
            commit(store)
        assert failed.value.code is RcaErrorCode.SEMANTIC_CONFLICT
        assert store.get_version_history("AGG-1") == ()

        wrong = replace(artifact(), provenance=replace(artifact().provenance, evidence_revision_id="ER-X"))
        with pytest.raises(RcaDomainError) as lineage:
            store.commit_validated_artifact("OP-COMMIT-X", "ATT-1", wrong, target(), NOW)
        assert lineage.value.code in {RcaErrorCode.SEMANTIC_CONFLICT, RcaErrorCode.IDENTITY_LINEAGE_CONFLICT}
        assert store.get_version_history("AGG-1") == ()


def test_rollback_does_not_leave_authority_or_consume_number(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        with sqlite3.connect(database) as connection:
            connection.execute(
                """CREATE TRIGGER reject_commit BEFORE INSERT ON rca_operation_receipts
                   WHEN NEW.command_kind='COMMIT_ARTIFACT' BEGIN SELECT RAISE(ABORT, 'fault'); END"""
            )
        with pytest.raises(RcaDomainError):
            commit(store)
        assert store.get_version("VER-1") is None
        assert store.get_publication_result("PUB-1") is None
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TRIGGER reject_commit")
        assert commit(store).version_number == 1


def test_continuous_numbering_and_committed_numbers_are_not_reused(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        first = commit(store)
        seed(store, "ATT-2")
        second = store.commit_validated_artifact("OP-COMMIT-2", "ATT-2", artifact(), target("VER-2", "PUB-2"), NOW)
        assert (first.version_number, second.version_number) == (1, 2)
        assert tuple(item.version_number for item in store.get_version_history("AGG-1")) == (1, 2)


def test_corrupt_artifact_or_dangling_attempt_fails_closed(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
    with sqlite3.connect(database) as connection:
        encoded = connection.execute(
            "SELECT semantic_identity FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'"
        ).fetchone()[0]
        payload = json.loads(encoded)
        payload["artifact"]["evaluation_ground_truth"] = "forbidden"
        connection.execute(
            "UPDATE rca_operation_receipts SET semantic_identity=? WHERE command_kind='COMMIT_ARTIFACT'",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
    with pytest.raises(RcaDomainError) as corrupt:
        SqliteRcaStore(database)
    assert corrupt.value.code is RcaErrorCode.INTEGRITY_CORRUPTION

    dangling = tmp_path / "dangling.db"
    with SqliteRcaStore(dangling) as store:
        seed(store)
        commit(store)
    with sqlite3.connect(dangling) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM rca_attempts WHERE attempt_id='ATT-1'")
    with pytest.raises(RcaDomainError) as missing:
        SqliteRcaStore(dangling)
    assert missing.value.code is RcaErrorCode.INTEGRITY_CORRUPTION


def test_production_shape_has_no_secret_or_evaluation_ground_truth_fields(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
    with sqlite3.connect(database) as connection:
        payload = json.loads(connection.execute(
            "SELECT semantic_identity FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'"
        ).fetchone()[0])
    encoded_keys = set(payload) | set(payload["artifact"]) | set(payload["artifact"]["provenance"]["generation"])
    assert not encoded_keys & {"credential_secret", "api_key", "scenario_id", "evaluation_run_id", "expected_root_cause", "ground_truth"}
