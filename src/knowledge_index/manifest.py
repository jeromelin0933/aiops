"""Canonical, fail-closed governed manifest admission for SPEC-014 Slice 1."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import (
    AdmittedDocument,
    AdmissionFailureCode,
    AdmissionFailureFact,
    ContentType,
    DocumentStatus,
    GovernedManifest,
    KnowledgeValidationError,
    ManifestAdmissionResult,
    ManifestDocument,
    MetadataItem,
    SourceClassification,
)
from .identity import document_identity, document_version_identity, manifest_commitment
from .security import (
    preflight_outbound_content,
    validate_metadata_security,
    validate_source_classification,
    validate_source_locator,
)


_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "canonicalization_version",
        "manifest_id",
        "corpus_id",
        "corpus_version",
        "documents",
    }
)
_DOCUMENT_KEYS = frozenset(
    {
        "document_id",
        "document_version",
        "source_path",
        "expected_content_hash",
        "status",
        "source_classification",
        "approval_reference",
        "outbound_eligible",
        "content_type",
        "metadata",
    }
)


def _fact(code: AdmissionFailureCode, field: str, detail: str) -> AdmissionFailureFact:
    return AdmissionFailureFact(code, field, detail)


def _shape_findings(value: Mapping[str, object], expected: frozenset[str], label: str) -> list[AdmissionFailureFact]:
    findings: list[AdmissionFailureFact] = []
    string_keys = {key for key in value if isinstance(key, str)}
    if len(string_keys) != len(value):
        findings.append(
            _fact(
                AdmissionFailureCode.UNSUPPORTED_FIELD,
                f"{label}.unsupported_key_type",
                "field name must be a string",
            )
        )
    for key in sorted(expected - string_keys):
        findings.append(_fact(AdmissionFailureCode.MISSING_FIELD, f"{label}.{key}", "required field is missing"))
    for index, _key in enumerate(sorted(string_keys - expected)):
        findings.append(
            _fact(
                AdmissionFailureCode.UNSUPPORTED_FIELD,
                f"{label}.unsupported_field_{index}",
                "field is not supported by this schema",
            )
        )
    return findings


def _parse_metadata(value: object) -> tuple[MetadataItem, ...]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise KnowledgeValidationError("metadata must be a string mapping")
    items = tuple(sorted(MetadataItem(key, item) for key, item in value.items()))
    return items


def _parse_document(value: object) -> ManifestDocument:
    if not isinstance(value, Mapping):
        raise KnowledgeValidationError("document entry must be a mapping")
    shape = _shape_findings(value, _DOCUMENT_KEYS, "document")
    if shape:
        raise KnowledgeValidationError(shape[0].detail)
    try:
        return ManifestDocument(
            document_id=value["document_id"],  # type: ignore[arg-type]
            document_version=value["document_version"],  # type: ignore[arg-type]
            source_path=value["source_path"],  # type: ignore[arg-type]
            expected_content_hash=value["expected_content_hash"],  # type: ignore[arg-type]
            status=DocumentStatus(value["status"]),
            source_classification=SourceClassification(value["source_classification"]),
            approval_reference=value["approval_reference"],  # type: ignore[arg-type]
            outbound_eligible=value["outbound_eligible"],  # type: ignore[arg-type]
            content_type=ContentType(value["content_type"]),
            metadata=_parse_metadata(value["metadata"]),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise KnowledgeValidationError("document entry contains an invalid field") from exc


def _safe_relative_source(source_path: str, source_root: Path) -> Path | None:
    path = PurePosixPath(source_path)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        return None
    try:
        candidate = (source_root / Path(*path.parts)).resolve(strict=False)
        candidate.relative_to(source_root)
    except (OSError, ValueError):
        return None
    return candidate


def _rejected(findings: Iterable[AdmissionFailureFact]) -> ManifestAdmissionResult:
    ordered = tuple(sorted(set(findings), key=lambda item: (item.code.value, item.field, item.detail)))
    return ManifestAdmissionResult(False, None, None, (), ordered)


def admit_manifest(
    raw_manifest: Mapping[str, object],
    *,
    source_root: str | Path,
    candidate_sources: Iterable[str] | None = None,
) -> ManifestAdmissionResult:
    """Validate the entire proposed corpus and admit all documents or none."""
    if not isinstance(raw_manifest, Mapping) or any(not isinstance(key, str) for key in raw_manifest):
        return _rejected((_fact(AdmissionFailureCode.INVALID_MANIFEST, "manifest", "manifest must be a string-keyed mapping"),))
    shape = _shape_findings(raw_manifest, _ROOT_KEYS, "manifest")
    if shape:
        return _rejected(shape)
    if raw_manifest.get("schema_version") != "1.0" or raw_manifest.get("canonicalization_version") != "1.0":
        return _rejected((_fact(AdmissionFailureCode.UNSUPPORTED_SCHEMA, "manifest.schema_version", "manifest schema or canonicalization version is unsupported"),))
    raw_documents = raw_manifest.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        return _rejected((_fact(AdmissionFailureCode.INVALID_MANIFEST, "manifest.documents", "documents must be a non-empty list"),))

    findings: list[AdmissionFailureFact] = []
    documents: list[ManifestDocument] = []
    for index, raw_document in enumerate(raw_documents):
        if isinstance(raw_document, Mapping):
            entry_shape = _shape_findings(raw_document, _DOCUMENT_KEYS, f"document.{index}")
            findings.extend(entry_shape)
            if entry_shape:
                continue
        try:
            documents.append(_parse_document(raw_document))
        except KnowledgeValidationError:
            findings.append(_fact(AdmissionFailureCode.INVALID_MANIFEST, f"document.{index}", "document entry is invalid"))

    try:
        manifest = GovernedManifest(
            schema_version=raw_manifest["schema_version"],  # type: ignore[arg-type]
            canonicalization_version=raw_manifest["canonicalization_version"],  # type: ignore[arg-type]
            manifest_id=raw_manifest["manifest_id"],  # type: ignore[arg-type]
            corpus_id=raw_manifest["corpus_id"],  # type: ignore[arg-type]
            corpus_version=raw_manifest["corpus_version"],  # type: ignore[arg-type]
            documents=tuple(documents),
        )
    except (KnowledgeValidationError, TypeError, ValueError):
        return _rejected((*findings, _fact(AdmissionFailureCode.INVALID_MANIFEST, "manifest", "manifest identity fields are invalid")))

    by_identity: dict[tuple[str, str], ManifestDocument] = {}
    by_source: dict[str, ManifestDocument] = {}
    for document in documents:
        key = (document.document_id, document.document_version)
        existing = by_identity.get(key)
        if existing is not None:
            code = AdmissionFailureCode.DUPLICATE_ENTRY if existing == document else AdmissionFailureCode.CONTRADICTORY_ENTRY
            findings.append(_fact(code, "document.identity", "document identity and version are repeated"))
        else:
            by_identity[key] = document
        if document.source_path in by_source:
            findings.append(_fact(AdmissionFailureCode.DUPLICATE_ENTRY, "document.source_path", "source path is assigned more than once"))
        else:
            by_source[document.source_path] = document

    declared_sources = frozenset(document.source_path for document in documents)
    if candidate_sources is not None:
        try:
            candidates = tuple(candidate_sources)
        except TypeError:
            candidates = ()
            findings.append(_fact(AdmissionFailureCode.SOURCE_NOT_LISTED, "candidate_sources", "candidate source set is invalid"))
        for candidate in sorted(value for value in candidates if isinstance(value, str)):
            if candidate not in declared_sources:
                findings.append(_fact(AdmissionFailureCode.SOURCE_NOT_LISTED, "candidate_sources", "candidate source is not governed by the manifest"))
        if any(not isinstance(value, str) for value in candidates):
            findings.append(_fact(AdmissionFailureCode.SOURCE_NOT_LISTED, "candidate_sources", "candidate source set contains an invalid value"))

    root = Path(source_root)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return _rejected((*findings, _fact(AdmissionFailureCode.SOURCE_UNREADABLE, "source_root", "approved source boundary is unavailable")))
    if not resolved_root.is_dir():
        return _rejected((*findings, _fact(AdmissionFailureCode.SOURCE_UNREADABLE, "source_root", "approved source boundary is not a directory")))

    admitted: list[AdmittedDocument] = []
    for document in sorted(documents, key=lambda item: (item.document_id, item.document_version, item.source_path)):
        before = len(findings)
        if document.status is not DocumentStatus.ACTIVE:
            findings.append(_fact(AdmissionFailureCode.DOCUMENT_NOT_ACTIVE, "document.status", "document is not active and approved for admission"))
        source_finding = validate_source_classification(document.source_classification)
        if source_finding is not None:
            findings.append(source_finding)
        locator_finding = validate_source_locator(document.source_path)
        if locator_finding is not None:
            findings.append(locator_finding)
        if not document.outbound_eligible:
            findings.append(_fact(AdmissionFailureCode.SOURCE_CLASS_FORBIDDEN, "document.outbound_eligible", "document is not eligible for outbound processing"))
        metadata_finding = validate_metadata_security(document.metadata)
        if metadata_finding is not None:
            findings.append(metadata_finding)
        if document.content_type is not ContentType.TEXT_UTF8:
            findings.append(_fact(AdmissionFailureCode.CONTENT_TYPE_UNSUPPORTED, "document.content_type", "content type is unsupported"))

        source = _safe_relative_source(document.source_path, resolved_root)
        if source is None:
            findings.append(_fact(AdmissionFailureCode.UNSAFE_SOURCE_PATH, "document.source_path", "source path is outside the approved boundary"))
            continue
        if not source.exists():
            findings.append(_fact(AdmissionFailureCode.SOURCE_MISSING, "document.source_path", "governed source is missing"))
            continue
        if not source.is_file():
            findings.append(_fact(AdmissionFailureCode.SOURCE_UNREADABLE, "document.source_path", "governed source is not a readable file"))
            continue
        try:
            content_bytes = source.read_bytes()
            content = content_bytes.decode("utf-8")
        except (OSError, UnicodeError):
            findings.append(_fact(AdmissionFailureCode.SOURCE_UNREADABLE, "document.source_path", "governed source cannot be read as UTF-8"))
            continue
        actual_hash = hashlib.sha256(content_bytes).hexdigest()
        if actual_hash != document.expected_content_hash:
            findings.append(_fact(AdmissionFailureCode.CONTENT_HASH_MISMATCH, "document.expected_content_hash", "governed source content does not match its commitment"))
        outbound_finding = preflight_outbound_content(content)
        if outbound_finding is not None:
            findings.append(outbound_finding)
        if len(findings) == before:
            stable_document = document_identity(manifest.corpus_id, document.document_id)
            stable_version = document_version_identity(
                stable_document, document.document_version, actual_hash
            )
            admitted.append(AdmittedDocument(stable_document, stable_version, document.source_path, actual_hash))

    if findings:
        return _rejected(findings)
    return ManifestAdmissionResult(
        True,
        manifest,
        manifest_commitment(manifest),
        tuple(admitted),
        (),
    )
