import copy
import hashlib
import json
from pathlib import Path

from knowledge_index import AdmissionFailureCode, admit_manifest, admit_production_manifest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs" / "knowledge_manifest.json"
SOURCES = ROOT / "docs" / "knowledge"


def raw_manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def codes(result):
    return {item.code for item in result.findings}


def copy_sources(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for source in SOURCES.iterdir():
        if source.is_file():
            (destination / source.name).write_bytes(source.read_bytes())


def admit_with_first_content(tmp_path: Path, content: bytes):
    copy_sources(tmp_path)
    raw = raw_manifest()
    target = tmp_path / raw["documents"][0]["source_path"]
    target.write_bytes(content)
    raw["documents"][0]["expected_content_hash"] = hashlib.sha256(content).hexdigest()
    return admit_manifest(
        raw, source_root=tmp_path,
        candidate_sources=sorted(path.name for path in tmp_path.iterdir()),
    )


def test_approved_production_release_and_git_blob_bytes_admit() -> None:
    result = admit_production_manifest("configs/knowledge_manifest.json", repository_root=ROOT)
    assert result.accepted
    assert result.manifest.schema_version == "1.1"
    assert result.manifest.release_status == "APPROVED"
    assert len(result.admitted_documents) == 6


def test_release_gate_and_exact_six_fail_closed() -> None:
    raw = raw_manifest()
    raw["release_status"] = "DRAFT"
    assert AdmissionFailureCode.RELEASE_NOT_APPROVED in codes(admit_manifest(raw, source_root=SOURCES))
    raw = raw_manifest()
    raw["documents"].pop()
    assert AdmissionFailureCode.RELEASE_MEMBERSHIP_INVALID in codes(admit_manifest(raw, source_root=SOURCES))


def test_raw_byte_hash_mismatch_is_not_normalized(tmp_path: Path) -> None:
    copy_sources(tmp_path)
    target = tmp_path / raw_manifest()["documents"][0]["source_path"]
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n", 1))
    result = admit_manifest(raw_manifest(), source_root=tmp_path, candidate_sources=sorted(p.name for p in tmp_path.iterdir()))
    assert AdmissionFailureCode.CONTENT_HASH_MISMATCH in codes(result)


def test_production_contentfulness_requires_body_inside_an_h2_boundary(tmp_path: Path) -> None:
    assert admit_with_first_content(tmp_path / "meaningful", b"## Procedure\nrun step one\n").accepted
    for name, content in (
        ("h2-only", b"## Procedure\n"),
        ("whitespace", b"## Procedure\n \t\n"),
        ("multiple-empty", b"## One\n\n## Two\n### Subheading\n"),
    ):
        result = admit_with_first_content(tmp_path / name, content)
        assert AdmissionFailureCode.CONTENT_NOT_MEANINGFUL in codes(result)


def test_contentfulness_is_deterministic_for_identical_bytes(tmp_path: Path) -> None:
    content = b"## Procedure\n\n### Detail\nperform the governed action\n"
    first = admit_with_first_content(tmp_path / "first", content)
    second = admit_with_first_content(tmp_path / "second", content)
    assert first.accepted is second.accepted is True
    assert first.findings == second.findings


def test_extra_missing_duplicate_and_unapproved_sources_fail_closed(tmp_path: Path) -> None:
    copy_sources(tmp_path)
    raw = raw_manifest()
    (tmp_path / "extra.md").write_text("## Extra\nnot governed", encoding="utf-8")
    result = admit_manifest(raw, source_root=tmp_path, candidate_sources=sorted(p.name for p in tmp_path.iterdir()))
    assert AdmissionFailureCode.SOURCE_NOT_LISTED in codes(result)
    (tmp_path / "extra.md").unlink()
    (tmp_path / raw["documents"][0]["source_path"]).unlink()
    assert AdmissionFailureCode.SOURCE_MISSING in codes(admit_manifest(raw, source_root=tmp_path))
    duplicate = raw_manifest()
    duplicate["documents"][1] = copy.deepcopy(duplicate["documents"][0])
    assert AdmissionFailureCode.DUPLICATE_ENTRY in codes(admit_manifest(duplicate, source_root=SOURCES))
    unapproved = raw_manifest()
    unapproved["documents"][0]["approval_state"] = "UNAPPROVED"
    assert AdmissionFailureCode.GOVERNANCE_METADATA_INVALID in codes(admit_manifest(unapproved, source_root=SOURCES))


def test_metadata_contentfulness_ground_truth_and_path_escape_fail_closed(tmp_path: Path) -> None:
    copy_sources(tmp_path)
    raw = raw_manifest()
    raw["documents"][0]["metadata"].pop("operational_domain")
    assert AdmissionFailureCode.GOVERNANCE_METADATA_INVALID in codes(admit_manifest(raw, source_root=tmp_path))
    raw = raw_manifest()
    raw["documents"][0]["metadata"]["problem_failure_class"] = "ground_truth.expected_answer"
    assert AdmissionFailureCode.SOURCE_CLASS_FORBIDDEN in codes(admit_manifest(raw, source_root=tmp_path))
    raw = raw_manifest()
    target = tmp_path / raw["documents"][0]["source_path"]
    target.write_text("no canonical section", encoding="utf-8")
    raw["documents"][0]["expected_content_hash"] = hashlib.sha256(target.read_bytes()).hexdigest()
    assert AdmissionFailureCode.CONTENT_NOT_MEANINGFUL in codes(admit_manifest(raw, source_root=tmp_path))
    raw = raw_manifest()
    raw["documents"][0]["source_path"] = "../escape.md"
    assert AdmissionFailureCode.GOVERNANCE_METADATA_INVALID in codes(admit_manifest(raw, source_root=tmp_path))
