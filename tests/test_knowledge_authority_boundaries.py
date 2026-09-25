from pathlib import Path

import knowledge_index
from knowledge_index import OpaqueExternalReference, OpaqueReferenceType


PACKAGE = Path("src/knowledge_index")


def _production_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE.glob("*.py"))
    )


def test_slice_one_public_api_contains_only_foundation_capabilities() -> None:
    exported = set(knowledge_index.__all__)
    forbidden_fragments = {
        "store",
        "sqlite",
        "retrieve",
        "snapshot",
        "activate",
        "provider",
        "runtime",
        "scheduler",
        "credentialregistry",
    }
    assert all(
        fragment not in name.lower().replace("_", "")
        for name in exported
        for fragment in forbidden_fragments
    )


def test_slice_one_does_not_import_forbidden_domain_or_private_helpers() -> None:
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
        "sqlite3",
        "chromadb",
        "google.",
    )
    for forbidden in forbidden_imports:
        assert forbidden not in source


def test_slice_one_defines_no_foreign_authority_or_future_placeholder() -> None:
    source = _production_source().lower()
    forbidden_definitions = (
        "class evidencesnapshot",
        "class evidencerevision",
        "class rcaaggregate",
        "class rcaattempt",
        "class incident",
        "class knowledgesnapshot",
        "class retrieval",
        "class scheduler",
        "class credentialregistry",
        "class sqlite",
    )
    for forbidden in forbidden_definitions:
        assert forbidden not in source


def test_opaque_references_retain_only_discriminator_and_value() -> None:
    reference = OpaqueExternalReference(OpaqueReferenceType.EVIDENCE_SNAPSHOT, "opaque-123")
    assert tuple(reference.__dataclass_fields__) == ("reference_type", "value")
    assert reference.value == "opaque-123"
    assert not hasattr(reference, "evidence")
    assert not hasattr(reference, "payload")
    assert not hasattr(reference, "rca")
