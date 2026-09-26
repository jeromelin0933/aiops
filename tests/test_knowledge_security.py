import hashlib
from pathlib import Path

import pytest

from knowledge_index import AdmissionFailureCode, admit_manifest, sanitize_failure_detail


def _manifest(root: Path, *, classification: str = "APPROVED_OPERATIONAL_KNOWLEDGE", metadata=None, content: str = "approved procedure"):
    source = root / "source.txt"
    source.write_bytes(content.encode("utf-8"))
    return {
        "schema_version": "1.0",
        "canonicalization_version": "1.0",
        "manifest_id": "MANIFEST-1",
        "corpus_id": "CORPUS-1",
        "corpus_version": "1.0",
        "documents": [
            {
                "document_id": "DOC-1",
                "document_version": "1.0",
                "source_path": "source.txt",
                "expected_content_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                "status": "ACTIVE",
                "source_classification": classification,
                "approval_reference": "GOV-1",
                "outbound_eligible": True,
                "content_type": "text/plain; charset=utf-8",
                "metadata": metadata or {"owner": "operations"},
            }
        ],
    }


@pytest.mark.parametrize(
    "classification",
    [
        "SCENARIO",
        "GROUND_TRUTH",
        "VALIDATOR_OUTPUT",
        "EVALUATION_TRUTH",
        "GENERATED_RCA",
        "SHADOW",
        "UNREVIEWED_INCIDENT",
        "UNSAFE_UNAPPROVED",
    ],
)
def test_foreign_or_evaluation_source_classes_cannot_enter_production_knowledge(
    tmp_path: Path, classification: str
) -> None:
    result = admit_manifest(_manifest(tmp_path, classification=classification), source_root=tmp_path)
    assert not result.accepted
    assert AdmissionFailureCode.SOURCE_CLASS_FORBIDDEN in {item.code for item in result.findings}


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_key": "not-allowed"},
        {"authorization": "not-allowed"},
        {"owner": "Bearer abc.def.ghi"},
        {"owner": "password=not-allowed"},
        {"owner": "token=not-allowed"},
    ],
)
def test_secret_shaped_metadata_is_rejected_without_disclosure(tmp_path: Path, metadata) -> None:
    secret_values = tuple(metadata.values())
    result = admit_manifest(_manifest(tmp_path, metadata=metadata), source_root=tmp_path)
    assert AdmissionFailureCode.SECRET_METADATA in {item.code for item in result.findings}
    assert result.manifest is None
    diagnostic = repr(result.findings)
    for secret in secret_values:
        assert secret not in diagnostic


@pytest.mark.parametrize(
    "content",
    [
        "Authorization: Bearer top-secret-token",
        "api_key=top-secret-token",
        "-----BEGIN PRIVATE KEY-----\nabc",
        "approved\x00procedure",
    ],
)
def test_unsafe_outbound_content_is_rejected_without_echoing_content(tmp_path: Path, content: str) -> None:
    result = admit_manifest(_manifest(tmp_path, content=content), source_root=tmp_path)
    assert AdmissionFailureCode.OUTBOUND_CONTENT_UNSAFE in {item.code for item in result.findings}
    assert "top-secret-token" not in repr(result.findings)
    assert content not in repr(result.findings)


def test_benign_operational_prose_with_unassigned_security_words_is_allowed(tmp_path: Path) -> None:
    content = (
        "Operational prerequisites:\n"
        "authorization:\nBased on verified evidence and existing authorization, continue safely.\n"
        "credential\ntoken\npassword\n"
    )
    result = admit_manifest(_manifest(tmp_path, content=content), source_root=tmp_path)
    assert result.accepted


@pytest.mark.parametrize(
    "content",
    [
        "authorization: Bearer synthetic-secret-value",
        "api_key: synthetic-secret-value",
        "token=synthetic-secret-value",
        "password: synthetic-secret-value",
    ],
)
def test_actual_synthetic_secret_assignments_still_fail_closed(tmp_path: Path, content: str) -> None:
    result = admit_manifest(_manifest(tmp_path, content=content), source_root=tmp_path)
    assert not result.accepted
    assert AdmissionFailureCode.OUTBOUND_CONTENT_UNSAFE in {item.code for item in result.findings}
    assert "synthetic-secret-value" not in repr(result.findings)


@pytest.mark.parametrize(
    "content",
    [
        "api_key:\nsynthetic-secret-value",
        "token:\n synthetic-secret-value",
        "password:\nsynthetic-secret-value",
        "access_token:\nsynthetic-secret-value",
        "api_key:\r\nsynthetic-secret-value",
        "token:\r\n synthetic-secret-value",
        "authorization:\nBearer synthetic-secret-value",
    ],
)
def test_multiline_synthetic_secret_assignments_fail_closed(tmp_path: Path, content: str) -> None:
    result = admit_manifest(_manifest(tmp_path, content=content), source_root=tmp_path)
    assert not result.accepted
    assert AdmissionFailureCode.OUTBOUND_CONTENT_UNSAFE in {item.code for item in result.findings}
    assert "synthetic-secret-value" not in repr(result.findings)


def test_sanitized_failure_representation_redacts_and_bounds_secrets() -> None:
    value = sanitize_failure_detail("provider failed api_key=top-secret-token " + "x" * 500)
    assert "top-secret-token" not in value
    assert "[REDACTED]" in value
    assert len(value) <= 256


def test_admission_secret_rule_remains_aligned_with_identity_rule(tmp_path: Path) -> None:
    result = admit_manifest(
        _manifest(tmp_path, metadata={"api_key": "synthetic-secret"}),
        source_root=tmp_path,
    )
    assert not result.accepted
    assert AdmissionFailureCode.SECRET_METADATA in {item.code for item in result.findings}


@pytest.mark.parametrize(
    "source_path",
    [
        "scenarios/source.txt",
        "fixtures/source.txt",
        "ground_truth/source.txt",
        "validator_output.txt",
        "generated_rca/source.txt",
        "shadow/source.txt",
        "unreviewed_incidents/source.txt",
    ],
)
def test_forbidden_locator_cannot_be_admitted_by_mislabeling_classification(
    tmp_path: Path, source_path: str
) -> None:
    content = "synthetic foreign authority content"
    source = tmp_path / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    raw = _manifest(tmp_path)
    raw["documents"][0]["source_path"] = source_path
    raw["documents"][0]["expected_content_hash"] = hashlib.sha256(content.encode()).hexdigest()
    result = admit_manifest(raw, source_root=tmp_path)
    assert AdmissionFailureCode.SOURCE_CLASS_FORBIDDEN in {item.code for item in result.findings}
