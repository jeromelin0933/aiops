from pathlib import Path

import knowledge_index
from knowledge_index import OpaqueExternalReference, OpaqueReferenceType


PACKAGE = Path("src/knowledge_index")


def _production_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE.glob("*.py"))
    )


def test_public_api_contains_only_approved_candidate_c_capabilities() -> None:
    exported = set(knowledge_index.__all__)
    forbidden_fragments = {
        "runtime",
        "scheduler",
        "credentialregistry",
    }
    assert all(
        fragment not in name.lower().replace("_", "")
        for name in exported
        for fragment in forbidden_fragments
    )


def test_candidate_c_store_does_not_import_forbidden_domain_or_private_helpers() -> None:
    source = _production_source()
    forbidden_imports = (
        "runtime_orchestration",
        "incident_management",
        "incident_evidence",
        "rca_persistence",
        "rca_integration",
        "event_detection.runner",
        "_stable_identity",
        "_sanitize_error_message",
        "chromadb",
        "google.",
    )
    for forbidden in forbidden_imports:
        assert forbidden not in source


def test_candidate_c_defines_no_foreign_authority_or_future_placeholder() -> None:
    source = _production_source().lower()
    forbidden_definitions = (
        "class evidencesnapshot",
        "class evidencerevision",
        "class rcaaggregate",
        "class rcaattempt",
        "class incident",
        "class retrieval:",
        "class scheduler",
        "class credentialregistry",
    )
    for forbidden in forbidden_definitions:
        assert forbidden not in source


def test_slice_two_store_is_candidate_c_owned_and_stdlib_only() -> None:
    source = (PACKAGE / "sqlite_store.py").read_text(encoding="utf-8")
    assert "class SqliteKnowledgeStore" in source
    assert "import sqlite3" in source
    assert "runtime_orchestration" not in source
    assert "incident_evidence" not in source
    assert "rca_" not in source
    assert "google." not in source
    assert "chromadb" not in source


def test_slice_four_does_not_define_forbidden_next_slice_workflow() -> None:
    source = _production_source().lower()
    forbidden = (
        "class googleembedding",
        "class chroma",
        "class buildlifecycle",
        "class scheduler",
        "class runtimeclock",
    )
    for definition in forbidden:
        assert definition not in source


def test_opaque_references_retain_only_discriminator_and_value() -> None:
    reference = OpaqueExternalReference(OpaqueReferenceType.EVIDENCE_SNAPSHOT, "opaque-123")
    assert tuple(reference.__dataclass_fields__) == ("reference_type", "value")
    assert reference.value == "opaque-123"
    assert not hasattr(reference, "evidence")
    assert not hasattr(reference, "payload")
    assert not hasattr(reference, "rca")


def test_slice_four_retrieval_modules_do_not_publish_snapshot_or_foreign_authority() -> None:
    retrieval_source = "\n".join(
        (PACKAGE / name).read_text(encoding="utf-8")
        for name in ("retrieval.py", "retrieval_ports.py", "retrieval_resolution.py")
    ).lower()
    for forbidden in (
        "knowledgesnapshot", "publish_snapshot", "chromadb", "google.",
        "runtime_orchestration", "credentialregistry", "incident_mutation",
        "rcaaggregate", "evidencesnapshot",
    ):
        assert forbidden not in retrieval_source


def test_slice_five_snapshot_module_owns_no_runtime_or_foreign_authority() -> None:
    source = (PACKAGE / "snapshot.py").read_text(encoding="utf-8").lower()
    for forbidden in (
        "runtime_orchestration", "scheduler", "runtimeclock", "retry_budget",
        "credentialregistry", "chromadb", "google.", "incident_mutation",
        "rcaaggregate", "evidencesnapshot", "startup recovery",
    ):
        assert forbidden not in source
