import hashlib
from pathlib import Path

import pytest

from knowledge_index import AdmissionFailureCode, admit_manifest


def _write(root: Path, relative: str, content: str = "approved operational procedure") -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _entry(relative: str, digest: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "document_id": "DOC-1",
        "document_version": "1.0",
        "source_path": relative,
        "expected_content_hash": digest,
        "status": "ACTIVE",
        "source_classification": "APPROVED_OPERATIONAL_KNOWLEDGE",
        "approval_reference": "GOV-1",
        "outbound_eligible": True,
        "content_type": "text/plain; charset=utf-8",
        "metadata": {"service": "identity"},
    }
    value.update(overrides)
    return value


def _manifest(documents: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1.0",
        "canonicalization_version": "1.0",
        "manifest_id": "MANIFEST-1",
        "corpus_id": "CORPUS-1",
        "corpus_version": "1.0",
        "documents": documents,
    }
    value.update(overrides)
    return value


def _codes(result) -> set[AdmissionFailureCode]:
    return {finding.code for finding in result.findings}


def test_valid_manifest_admits_all_documents_deterministically(tmp_path: Path) -> None:
    digest = _write(tmp_path, "sop/login.txt")
    raw = _manifest([_entry("sop/login.txt", digest)])
    first = admit_manifest(raw, source_root=tmp_path, candidate_sources=["sop/login.txt"])
    second = admit_manifest(raw, source_root=tmp_path, candidate_sources=["sop/login.txt"])
    assert first.accepted and first == second
    assert first.manifest_commitment and first.manifest_commitment.startswith("kmf_")
    assert len(first.admitted_documents) == 1
    assert first.findings == ()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"source_path": "sop/missing.txt"}, AdmissionFailureCode.SOURCE_MISSING),
        ({"expected_content_hash": "0" * 64}, AdmissionFailureCode.CONTENT_HASH_MISMATCH),
        ({"status": "RETIRED"}, AdmissionFailureCode.DOCUMENT_NOT_ACTIVE),
        ({"status": "REVOKED"}, AdmissionFailureCode.DOCUMENT_NOT_ACTIVE),
        ({"status": "UNAPPROVED"}, AdmissionFailureCode.DOCUMENT_NOT_ACTIVE),
    ],
)
def test_required_source_failures_reject_the_entire_admission(
    tmp_path: Path, mutation: dict[str, object], expected: AdmissionFailureCode
) -> None:
    digest = _write(tmp_path, "sop/login.txt")
    result = admit_manifest(_manifest([_entry("sop/login.txt", digest, **mutation)]), source_root=tmp_path)
    assert not result.accepted
    assert expected in _codes(result)
    assert result.manifest_commitment is None and result.admitted_documents == ()


def test_duplicate_and_contradictory_document_identities_fail_closed(tmp_path: Path) -> None:
    digest = _write(tmp_path, "sop/login.txt")
    duplicate = _entry("sop/login.txt", digest)
    result = admit_manifest(_manifest([duplicate, dict(duplicate)]), source_root=tmp_path)
    assert AdmissionFailureCode.DUPLICATE_ENTRY in _codes(result)

    other_hash = _write(tmp_path, "sop/other.txt", "different approved procedure")
    contradiction = _entry("sop/other.txt", other_hash)
    result = admit_manifest(_manifest([duplicate, contradiction]), source_root=tmp_path)
    assert AdmissionFailureCode.CONTRADICTORY_ENTRY in _codes(result)


def test_unlisted_candidate_source_is_rejected(tmp_path: Path) -> None:
    digest = _write(tmp_path, "sop/login.txt")
    _write(tmp_path, "sop/unlisted.txt")
    result = admit_manifest(
        _manifest([_entry("sop/login.txt", digest)]),
        source_root=tmp_path,
        candidate_sources=["sop/login.txt", "sop/unlisted.txt"],
    )
    assert AdmissionFailureCode.SOURCE_NOT_LISTED in _codes(result)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": "2.0"},
        {"canonicalization_version": "2.0"},
    ],
)
def test_unsupported_schema_versions_fail_closed(tmp_path: Path, overrides: dict[str, object]) -> None:
    digest = _write(tmp_path, "sop/login.txt")
    result = admit_manifest(_manifest([_entry("sop/login.txt", digest)], **overrides), source_root=tmp_path)
    assert _codes(result) == {AdmissionFailureCode.UNSUPPORTED_SCHEMA}


def test_unknown_root_or_document_fields_fail_closed(tmp_path: Path) -> None:
    digest = _write(tmp_path, "sop/login.txt")
    root = _manifest([_entry("sop/login.txt", digest)], surprise=True)
    assert AdmissionFailureCode.UNSUPPORTED_FIELD in _codes(admit_manifest(root, source_root=tmp_path))
    entry = _entry("sop/login.txt", digest, surprise=True)
    assert AdmissionFailureCode.UNSUPPORTED_FIELD in _codes(
        admit_manifest(_manifest([entry]), source_root=tmp_path)
    )


def test_unknown_field_name_is_not_echoed_in_rejection(tmp_path: Path) -> None:
    digest = _write(tmp_path, "sop/login.txt")
    secret_field = "api_key:must-not-leak"
    raw = _manifest([_entry("sop/login.txt", digest)], **{secret_field: "value"})
    result = admit_manifest(raw, source_root=tmp_path)
    assert not result.accepted and result.manifest is None
    assert secret_field not in repr(result.findings)


@pytest.mark.parametrize("unsafe", ["../outside.txt", "/absolute.txt", "sop/../outside.txt"])
def test_path_traversal_and_absolute_paths_are_rejected(tmp_path: Path, unsafe: str) -> None:
    result = admit_manifest(_manifest([_entry(unsafe, "0" * 64)]), source_root=tmp_path)
    assert AdmissionFailureCode.UNSAFE_SOURCE_PATH in _codes(result)


def test_symlink_escape_is_rejected_when_symlinks_are_available(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available")
    digest = hashlib.sha256(b"outside").hexdigest()
    result = admit_manifest(_manifest([_entry("linked.txt", digest)]), source_root=tmp_path)
    assert AdmissionFailureCode.UNSAFE_SOURCE_PATH in _codes(result)


def test_directory_and_invalid_utf8_are_unreadable_sources(tmp_path: Path) -> None:
    (tmp_path / "directory.txt").mkdir()
    directory = admit_manifest(_manifest([_entry("directory.txt", "0" * 64)]), source_root=tmp_path)
    assert AdmissionFailureCode.SOURCE_UNREADABLE in _codes(directory)
    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"\xff\xfe")
    invalid = admit_manifest(_manifest([_entry("binary.txt", hashlib.sha256(b"\xff\xfe").hexdigest())]), source_root=tmp_path)
    assert AdmissionFailureCode.SOURCE_UNREADABLE in _codes(invalid)
