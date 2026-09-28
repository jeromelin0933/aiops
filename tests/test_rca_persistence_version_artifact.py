import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from rca_persistence import (
    AdmitAttemptRequest,
    AdmittedRetryDisposition,
    ArtifactProvenance,
    ArtifactClaim,
    ArtifactClaimSemantics,
    ArtifactCausalAssertion,
    ArtifactCausalSupportProof,
    ArtifactEvidenceFact,
    ArtifactKnowledgeFact,
    TypedArtifactProvenanceRead,
    ClaimCategory,
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
    PublicationDisposition,
    PublicationResult,
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


def typed_artifact() -> RcaArtifact:
    base = artifact()
    evidence = ArtifactEvidenceFact("E-1", "ES-1", "evidence-v1", "/metrics/queue", "a" * 64)
    knowledge = ArtifactKnowledgeFact(
        "K-1", "KS-1", "b" * 64, "corpus", "build", "index", "doc",
        "v1", "sec", "chunk", "c" * 64, "d" * 64,
    )
    claims = (
        ArtifactClaim("C-2", ClaimCategory.ANALYTICAL_INFERENCE, "queue saturation", ("E-1",), (), ("K-1",), EvidentialSupport.HIGH),
        ArtifactClaim("C-1", ClaimCategory.OBSERVED_FACT, "metric spike", ("E-1",), (), ()),
    )
    semantics = ArtifactClaimSemantics(
        "1", claims, (evidence,), (knowledge,), ("C-1", "C-2"), ("C-2",),
        (("C-2",),), (("C-2",),), (("C-1",),),
    )
    return replace(base, claim_semantics=semantics)


def causal_typed_artifact() -> RcaArtifact:
    base = typed_artifact()
    typed = base.claim_semantics
    additional_evidence = ArtifactEvidenceFact("E-2", "ES-1", "evidence-v1", "/events/downstream", "e" * 64)
    claims = (
        replace(typed.claims[0], supporting_evidence_ids=("E-1", "E-2")),
        typed.claims[1],
        ArtifactClaim("C-3", ClaimCategory.OBSERVED_FACT, "downstream failure", ("E-2",), (), ()),
    )
    assertion = ArtifactCausalAssertion("C-2", ("C-1",), ("C-3",), "CAUSES")
    proof = ArtifactCausalSupportProof(
        "cproof-1", "C-2", "oom-downstream-cascade", "1",
        ("C-1", "C-3"), "ES-1", "f" * 64,
    )
    return replace(base, claim_semantics=replace(
        typed, schema_version="2", claims=claims,
        evidence_references=typed.evidence_references + (additional_evidence,),
        causal_assertions=(assertion,), causal_proofs=(proof,),
    ))


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


def test_typed_claims_round_trip_replay_and_authoritative_reads(tmp_path) -> None:
    database = tmp_path / "typed.db"
    expected = typed_artifact()
    with SqliteRcaStore(database) as store:
        seed(store)
        first = store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", expected, target(), NOW)
        assert store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", expected, target(), NOW) == first
        assert store.get_artifact("VER-1") == expected
        assert store.get_version_history("AGG-1") == (first,)
    with SqliteRcaStore(database) as store:
        assert store.get_version("VER-1").artifact == expected
        assert store.get_artifact("VER-1").claim_semantics == expected.claim_semantics
        assert tuple(c.claim_id for c in store.get_artifact("VER-1").claim_semantics.claims) == ("C-2", "C-1")
        assert store.get_artifact("VER-1").claim_semantics.claims[0].category is ClaimCategory.ANALYTICAL_INFERENCE
        assert store.get_artifact("VER-1").claim_semantics.claims[0].contradicting_evidence_ids == ()
        assert store.get_artifact("VER-1").claim_semantics.evidence_references[0].canonical_fact_commitment == "a" * 64
        assert store.get_artifact("VER-1").claim_semantics.knowledge_references[0].metadata_commitment == "d" * 64
        store.complete_authorized_publication(PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1"))
        assert store.get_current("AGG-1").artifact == expected
        assert store.get_version_history("AGG-1")[0].artifact == expected
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'").fetchone()[0] == 1


def test_causal_semantics_round_trip_replay_and_public_reads(tmp_path) -> None:
    database = tmp_path / "causal.db"
    expected = causal_typed_artifact()
    with SqliteRcaStore(database) as store:
        seed(store)
        version = store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", expected, target(), NOW)
        assert store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", expected, target(), NOW) == version
    with SqliteRcaStore(database) as store:
        direct = store.get_artifact("VER-1")
        assert direct == expected
        assert direct.claim_semantics.causal_assertions == expected.claim_semantics.causal_assertions
        assert direct.claim_semantics.causal_proofs == expected.claim_semantics.causal_proofs
        assert store.get_version("VER-1").artifact == expected
        assert store.get_version_history("AGG-1") == (version,)
        store.complete_authorized_publication(PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1"))
        assert store.get_current("AGG-1").artifact == expected
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'").fetchone()[0] == 1


def test_legacy_typed_encoding_keeps_causal_semantics_unavailable() -> None:
    from rca_persistence.sqlite_store import _decode_artifact_object, _encode_artifact
    historical = json.loads(_encode_artifact(typed_artifact()))
    typed = historical["claim_semantics"]
    assert typed["schema_version"] == "1"
    assert "causal_assertions" not in typed
    assert "causal_proofs" not in typed
    assert _decode_artifact_object(historical).claim_semantics.schema_version == "1"


@pytest.mark.parametrize("field", ["relation", "cause", "effect", "rule_id", "rule_version", "proof_commitment"])
def test_causal_semantic_difference_conflicts_on_replay(tmp_path, field) -> None:
    original = causal_typed_artifact()
    typed = original.claim_semantics
    assertion = typed.causal_assertions[0]
    proof = typed.causal_proofs[0]
    if field == "relation":
        changed = replace(typed, causal_assertions=(replace(assertion, relation="CONTRIBUTES_TO"),))
    elif field == "cause":
        changed = replace(typed, causal_assertions=(replace(assertion, cause_claim_ids=("C-3",), effect_claim_ids=("C-1",)),),
                          causal_proofs=(replace(proof, supporting_observed_claim_ids=("C-3", "C-1")),))
    elif field == "effect":
        changed = replace(typed, causal_assertions=(replace(assertion, effect_claim_ids=("C-1",), cause_claim_ids=("C-3",)),),
                          causal_proofs=(replace(proof, supporting_observed_claim_ids=("C-3", "C-1")),))
    elif field == "rule_id":
        changed = replace(typed, causal_proofs=(replace(proof, causal_rule_id="other-rule"),))
    elif field == "rule_version":
        changed = replace(typed, causal_proofs=(replace(proof, causal_rule_version="2"),))
    else:
        changed = replace(typed, causal_proofs=(replace(proof, proof_commitment="a" * 64),))
    altered = replace(original, claim_semantics=changed)
    from rca_persistence.sqlite_store import _encode_artifact
    assert _encode_artifact(original) != _encode_artifact(altered)
    with SqliteRcaStore(tmp_path / f"causal-{field}.db") as store:
        seed(store)
        store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", original, target(), NOW)
        with pytest.raises(RcaDomainError) as conflict:
            store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", altered, target(), NOW)
        assert conflict.value.code is RcaErrorCode.RECEIPT_REPLAY_CONFLICT


@pytest.mark.parametrize("field", ["relation", "rule_version", "proof_commitment"])
def test_persisted_causal_tamper_fails_authoritative_read(tmp_path, field) -> None:
    database = tmp_path / f"causal-tamper-{field}.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", causal_typed_artifact(), target(), NOW)
    with sqlite3.connect(database) as connection:
        encoded = connection.execute("SELECT semantic_identity FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'").fetchone()[0]
        payload = json.loads(encoded)
        typed = payload["artifact"]["claim_semantics"]
        if field == "relation":
            typed["causal_assertions"][0]["relation"] = "CONTRIBUTES_TO"
        elif field == "rule_version":
            typed["causal_proofs"][0]["causal_rule_version"] = "2"
        else:
            typed["causal_proofs"][0]["proof_commitment"] = "a" * 64
        connection.execute("UPDATE rca_operation_receipts SET semantic_identity=? WHERE command_kind='COMMIT_ARTIFACT'", (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        ))
    with pytest.raises(RcaDomainError) as corrupt:
        SqliteRcaStore(database)
    assert corrupt.value.code is RcaErrorCode.INTEGRITY_CORRUPTION


def test_typed_only_public_provenance_survives_restart(tmp_path) -> None:
    database = tmp_path / "typed-provenance.db"
    value = typed_artifact()
    value = replace(value, provenance=replace(value.provenance, evidence_references=(), knowledge_references=()))
    with SqliteRcaStore(database) as store:
        seed(store)
        store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", value, target(), NOW)
        direct = store.get_artifact("VER-1")
        public = store.get_artifact_provenance("VER-1")
        assert isinstance(public, TypedArtifactProvenanceRead)
        assert public.evidence_references == direct.claim_semantics.evidence_references
        assert public.knowledge_references == direct.claim_semantics.knowledge_references
        assert public.evidence_snapshot_id == direct.provenance.evidence_snapshot_id
        assert public.knowledge_snapshot_id == direct.provenance.knowledge_snapshot_id
    with SqliteRcaStore(database) as store:
        assert store.get_artifact_provenance("VER-1") == public
        assert store.get_artifact_provenance("VER-1").evidence_references
        assert store.get_artifact_provenance("VER-1").knowledge_references


def _changed_typed_artifact(field: str) -> RcaArtifact:
    value = typed_artifact()
    semantic = value.claim_semantics
    first = semantic.claims[0]
    if field == "order":
        semantic = replace(semantic, claims=semantic.claims[::-1])
    elif field == "category":
        semantic = replace(semantic, claims=(replace(first, category=ClaimCategory.MODEL_SUGGESTED_GUIDANCE), semantic.claims[1]))
    elif field == "evidence":
        semantic = replace(semantic, evidence_references=(replace(semantic.evidence_references[0], canonical_fact_commitment="e" * 64),))
    elif field == "knowledge":
        semantic = replace(semantic, knowledge_references=(replace(semantic.knowledge_references[0], content_commitment="e" * 64),))
    elif field == "support":
        semantic = replace(semantic, claims=(replace(first, supporting_evidence_ids=()), semantic.claims[1]))
    elif field == "contradiction":
        semantic = replace(semantic, claims=(replace(first, contradicting_evidence_ids=("E-1",)), semantic.claims[1]))
    elif field == "summary":
        semantic = replace(semantic, summary_claim_ids=("C-2",))
    elif field == "severity":
        semantic = replace(semantic, severity_claim_ids=("C-1",))
    return replace(value, claim_semantics=semantic)


@pytest.mark.parametrize("field", ["order", "category", "evidence", "knowledge", "support", "contradiction", "summary", "severity"])
def test_typed_semantic_difference_changes_commitment_and_replay_conflicts(tmp_path, field) -> None:
    from rca_persistence.sqlite_store import _encode_artifact

    original = typed_artifact()
    altered = _changed_typed_artifact(field)
    assert _encode_artifact(altered) != _encode_artifact(original)
    with SqliteRcaStore(tmp_path / f"{field}.db") as store:
        seed(store)
        store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", original, target(), NOW)
        with pytest.raises(RcaDomainError) as conflict:
            store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", altered, target(), NOW)
        assert conflict.value.code is RcaErrorCode.RECEIPT_REPLAY_CONFLICT
        assert store.get_artifact("VER-1") == original


def test_typed_claim_structure_rejects_unresolved_coverage_and_references() -> None:
    semantic = typed_artifact().claim_semantics
    with pytest.raises(RcaDomainError):
        replace(semantic, summary_claim_ids=("missing",))
    with pytest.raises(RcaDomainError):
        replace(semantic, claims=(replace(semantic.claims[0], supporting_evidence_ids=("missing",)), semantic.claims[1]))
    with pytest.raises(RcaDomainError):
        replace(semantic, claims=(semantic.claims[0], semantic.claims[0]))


def test_typed_refs_never_fall_back_to_legacy_and_overlap_must_be_consistent() -> None:
    value = typed_artifact()
    semantic = value.claim_semantics
    assert value.provenance.knowledge_references[0].reference_id == semantic.knowledge_references[0].reference_id

    # A typed claim may not resolve its support from the legacy-only E-1 record.
    with pytest.raises(RcaDomainError):
        replace(semantic, evidence_references=())

    claims_without_evidence = tuple(replace(item, supporting_evidence_ids=()) for item in semantic.claims)
    empty_typed_evidence = replace(semantic, claims=claims_without_evidence, evidence_references=())
    with pytest.raises(RcaDomainError):
        replace(value, claim_semantics=empty_typed_evidence)

    claims_without_knowledge = tuple(replace(item, knowledge_reference_ids=()) for item in semantic.claims)
    empty_typed_knowledge = replace(semantic, claims=claims_without_knowledge, knowledge_references=())
    with pytest.raises(RcaDomainError):
        replace(value, claim_semantics=empty_typed_knowledge)

    # An old reference cannot add an identity absent from typed admitted refs.
    with pytest.raises(RcaDomainError):
        replace(value, provenance=replace(value.provenance, evidence_references=(EvidenceReference("E-extra", ArtifactStatementKind.OBSERVED_FACT, "extra"),)))

    # Knowledge fields shared by both representations must agree for the same ID.
    contradictory = replace(semantic, knowledge_references=(replace(semantic.knowledge_references[0], index_id="other-index"),))
    with pytest.raises(RcaDomainError):
        replace(value, claim_semantics=contradictory)

    # Typed canonical Evidence facts cannot duplicate one identity with two commitments.
    with pytest.raises(RcaDomainError):
        replace(semantic, evidence_references=(semantic.evidence_references[0], replace(semantic.evidence_references[0], canonical_fact_commitment="e" * 64)))


def test_typed_claim_tamper_and_legacy_shape(tmp_path) -> None:
    database = tmp_path / "tamper.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", typed_artifact(), target(), NOW)
    with sqlite3.connect(database) as connection:
        encoded = connection.execute("SELECT semantic_identity FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'").fetchone()[0]
        payload = json.loads(encoded)
        payload["artifact"]["claim_semantics"]["summary_claim_ids"] = ["C-1"]
        connection.execute("UPDATE rca_operation_receipts SET semantic_identity=? WHERE command_kind='COMMIT_ARTIFACT'", (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),))
    with pytest.raises(RcaDomainError) as tampered:
        SqliteRcaStore(database)
    assert tampered.value.code is RcaErrorCode.INTEGRITY_CORRUPTION

    legacy_database = tmp_path / "legacy.db"
    with SqliteRcaStore(legacy_database) as store:
        seed(store)
        store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", artifact(), target(), NOW)
    with sqlite3.connect(legacy_database) as connection:
        historical = connection.execute("SELECT semantic_identity FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'").fetchone()[0]
        assert "claim_semantics" not in json.loads(historical)["artifact"]
    with SqliteRcaStore(legacy_database) as store:
        assert store.get_artifact("VER-1").claim_semantics is None
    with sqlite3.connect(legacy_database) as connection:
        assert connection.execute("SELECT semantic_identity FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'").fetchone()[0] == historical


@pytest.mark.parametrize("field", ["category", "evidence", "knowledge", "support", "contradiction"])
def test_persisted_typed_semantic_tamper_fails_closed(tmp_path, field) -> None:
    database = tmp_path / f"tamper-{field}.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        store.commit_validated_artifact("OP-COMMIT-1", "ATT-1", typed_artifact(), target(), NOW)
    with sqlite3.connect(database) as connection:
        encoded = connection.execute("SELECT semantic_identity FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'").fetchone()[0]
        payload = json.loads(encoded)
        typed = payload["artifact"]["claim_semantics"]
        if field == "category":
            typed["claims"][0]["category"] = "MODEL_SUGGESTED_GUIDANCE"
        elif field == "evidence":
            typed["evidence_references"][0]["canonical_fact_commitment"] = "e" * 64
        elif field == "knowledge":
            typed["knowledge_references"][0]["content_commitment"] = "e" * 64
        elif field == "support":
            typed["claims"][0]["supporting_evidence_ids"] = []
        else:
            typed["claims"][0]["contradicting_evidence_ids"] = ["E-1"]
        connection.execute("UPDATE rca_operation_receipts SET semantic_identity=? WHERE command_kind='COMMIT_ARTIFACT'", (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),))
    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION


def test_candidate_d_structured_content_fits_a_artifact_without_loss() -> None:
    from dataclasses import asdict
    from test_llm_generation_phase1 import content

    admitted = content()
    semantics = ArtifactClaimSemantics(
        "1",
        tuple(ArtifactClaim(**{key: value for key, value in asdict(item).items() if key != "causal_assertion"}) for item in admitted.claims),
        tuple(ArtifactEvidenceFact(**asdict(item)) for item in admitted.evidence_references),
        tuple(ArtifactKnowledgeFact(**asdict(item)) for item in admitted.knowledge_references),
        admitted.summary_claim_ids,
        admitted.severity_claim_ids,
        tuple(item.claim_ids for item in admitted.hypotheses),
        tuple(item.claim_ids for item in admitted.remediation),
        tuple(item.claim_ids for item in admitted.prevention),
    )
    value = RcaArtifact(
        admitted.summary, admitted.severity_assessment, admitted.diagnostic_conclusion,
        tuple(RcaHypothesis(item.rank, item.statement, item.evidential_support,
                            item.supporting_evidence_ids, item.contradicting_evidence_ids,
                            item.knowledge_reference_ids, item.reasoning_summary)
              for item in admitted.hypotheses),
        tuple(RcaAction(item.description, item.source, item.knowledge_reference_ids) for item in admitted.remediation),
        tuple(RcaAction(item.description, item.source, item.knowledge_reference_ids) for item in admitted.prevention),
        admitted.limitations, admitted.evidence_completeness, admitted.knowledge_gap,
        ArtifactProvenance(
            admitted.input.evidence_snapshot_id, admitted.input.evidence_revision_id,
            admitted.input.knowledge_snapshot_id, (), (),
            GenerationProvenance(
                admitted.input.pin.provider, admitted.input.pin.model,
                admitted.input.pin.prompt.identity, admitted.input.pin.configuration.identity,
                admitted.input.pin.profile.identity,
            ),
        ), semantics,
    )
    assert tuple(asdict(item) for item in value.claim_semantics.claims) == tuple(
        {key: field for key, field in asdict(item).items() if key != "causal_assertion"}
        for item in admitted.claims
    )
    assert tuple(asdict(item) for item in value.claim_semantics.evidence_references) == tuple(asdict(item) for item in admitted.evidence_references)
    assert tuple(asdict(item) for item in value.claim_semantics.knowledge_references) == tuple(asdict(item) for item in admitted.knowledge_references)
    assert value.claim_semantics.summary_claim_ids == admitted.summary_claim_ids
    assert value.claim_semantics.severity_claim_ids == admitted.severity_claim_ids
