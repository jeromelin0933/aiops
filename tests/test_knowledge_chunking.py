from pathlib import Path

from knowledge_index import ChunkingProfile, admit_production_manifest, plan_chunks


ROOT = Path(__file__).resolve().parents[1]


def test_production_h2_chunk_plan_is_stable_bounded_and_complete() -> None:
    admission = admit_production_manifest("configs/knowledge_manifest.json", repository_root=ROOT)
    profile = ChunkingProfile("test-h2-v1", 1024)
    first = plan_chunks(admission, source_root=ROOT / "docs" / "knowledge", profile=profile)
    second = plan_chunks(admission, source_root=ROOT / "docs" / "knowledge", profile=profile)
    assert first == second and first
    assert tuple(item.ordinal for item in first) == tuple(range(len(first)))
    assert len({item.chunk_identity for item in first}) == len(first)
    assert all(item.section_identity.startswith("h2-") for item in first)
    assert all(len(item.content.encode("utf-8")) <= 1024 for item in first)
    assert {item.document_identity for item in first} == {
        item.document_identity for item in admission.admitted_documents
    }


def test_smaller_bound_changes_chunk_identity_without_provider_state() -> None:
    admission = admit_production_manifest("configs/knowledge_manifest.json", repository_root=ROOT)
    large = plan_chunks(admission, source_root=ROOT / "docs" / "knowledge", profile=ChunkingProfile("large-v1", 4096))
    small = plan_chunks(admission, source_root=ROOT / "docs" / "knowledge", profile=ChunkingProfile("small-v1", 512))
    assert tuple(item.chunk_identity for item in large) != tuple(item.chunk_identity for item in small)
    assert len(small) >= len(large)
