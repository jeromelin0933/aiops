"""Deterministic, provider-independent H2 chunk planning for SPEC-014."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from pathlib import Path

from .build_validation import derive_chunk_metadata_commitment
from .contracts import (
    BuildChunk,
    BuildDocumentProvenance,
    ChunkIdentityInput,
    ManifestAdmissionResult,
    KnowledgeValidationError,
)
from .identity import chunk_identity


_H2 = re.compile(r"(?m)^## ([^\r\n]+)\s*$")


@dataclass(frozen=True, slots=True)
class ChunkingProfile:
    profile_identity: str
    maximum_chunk_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.profile_identity, str) or not self.profile_identity:
            raise KnowledgeValidationError("chunking profile identity is required")
        if isinstance(self.maximum_chunk_bytes, bool) or not isinstance(self.maximum_chunk_bytes, int) or self.maximum_chunk_bytes < 256:
            raise KnowledgeValidationError("maximum_chunk_bytes must be at least 256")


def _bounded_parts(text: str, maximum_bytes: int) -> tuple[str, ...]:
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-8")) <= maximum_bytes:
            parts.append(remaining)
            break
        end = 0
        used = 0
        for index, character in enumerate(remaining):
            width = len(character.encode("utf-8"))
            if used + width > maximum_bytes:
                break
            used += width
            end = index + 1
        if end == 0:
            raise KnowledgeValidationError("chunking bound cannot encode one character")
        preferred = max(remaining.rfind("\n\n", 0, end), remaining.rfind("\n", 0, end))
        if preferred >= maximum_bytes // 4:
            end = preferred + (2 if remaining[preferred:preferred + 2] == "\n\n" else 1)
        parts.append(remaining[:end])
        remaining = remaining[end:]
    return tuple(part for part in parts if part)


def plan_chunks(
    admission: ManifestAdmissionResult,
    *,
    source_root: str | Path,
    profile: ChunkingProfile,
) -> tuple[BuildChunk, ...]:
    if not admission.accepted or admission.manifest is None or admission.manifest_commitment is None:
        raise KnowledgeValidationError("accepted manifest admission is required")
    root = Path(source_root).resolve(strict=True)
    governed = {item.source_path: item for item in admission.manifest.documents}
    planned: list[BuildChunk] = []
    ordinal = 0
    for admitted in admission.admitted_documents:
        document = governed[admitted.source_path]
        text = (root / admitted.source_path).read_bytes().decode("utf-8")
        matches = tuple(_H2.finditer(text))
        if not matches:
            raise KnowledgeValidationError("production document has no canonical H2 section")
        provenance = BuildDocumentProvenance(
            admitted.document_identity,
            admitted.document_version_identity,
            admitted.source_path,
            admitted.content_hash,
            document.approval_reference,
            document.source_classification,
            document.outbound_eligible,
            document.content_type,
            document.metadata,
            document.knowledge_type,
            document.guidance_authority,
            document.status,
            document.approval_state,
            document.production_eligible,
        )
        for section_index, match in enumerate(matches):
            end = matches[section_index + 1].start() if section_index + 1 < len(matches) else len(text)
            section = text[match.start():end]
            section_identity = "h2-" + hashlib.sha256(match.group(1).strip().encode("utf-8")).hexdigest()[:24]
            for part in _bounded_parts(section, profile.maximum_chunk_bytes):
                content_hash = hashlib.sha256(part.encode("utf-8")).hexdigest()
                identity_input = ChunkIdentityInput(
                    admitted.document_identity,
                    admitted.document_version_identity,
                    section_identity,
                    ordinal,
                    content_hash,
                    profile.profile_identity,
                )
                identity = chunk_identity(identity_input)
                metadata_commitment = derive_chunk_metadata_commitment(
                    admission.manifest_commitment,
                    provenance,
                    chunk_identity=identity,
                    section_identity=section_identity,
                    ordinal=ordinal,
                    content_hash=content_hash,
                )
                planned.append(BuildChunk(
                    identity, admitted.document_identity, admitted.document_version_identity,
                    section_identity, ordinal, part, content_hash, metadata_commitment,
                ))
                ordinal += 1
    return tuple(planned)
