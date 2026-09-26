"""Canonical, fail-closed governed manifest admission for SPEC-014 Slice 1."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
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
_ROOT_KEYS_V11 = frozenset({
    "schema_identity", "schema_version", "canonicalization_version", "manifest_id",
    "corpus_id", "corpus_version", "release_id", "release_version",
    "canonical_release_reference", "release_status", "release_approver_role",
    "release_approval_reference", "governed_source_root", "source_revision",
    "content_hash_algorithm", "metadata_schema_identity", "metadata_schema_version",
    "metadata_vocabulary", "documents",
})
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
_DOCUMENT_KEYS_V11 = frozenset({
    "document_id", "document_version", "source_path", "committed_source_reference",
    "expected_content_hash", "content_type", "approval_state", "status",
    "production_eligible", "approval_reference", "source_classification",
    "security_classification", "outbound_eligible", "outbound_scope",
    "knowledge_type", "guidance_authority", "metadata",
})
_METADATA_VOCABULARY_KEYS = frozenset({
    "multi_value_order", "multi_value_separator", "required_document_metadata_keys",
    "required_exact_match_filter_keys", "required_filter_failure_disposition",
    "unsupported_constraint_disposition", "no_applicable_candidate_resolution",
})
_REQUIRED_DOCUMENT_METADATA = frozenset({
    "metadata_schema_identity", "metadata_schema_version", "operational_domain",
    "component_service_scope", "dependency_role_scope", "problem_failure_class",
    "observable_signal_symptom_classes", "applicability_constraints",
    "canonical_section_identity_scheme",
})
_REQUIRED_DOCUMENT_METADATA_ORDER = [
    "metadata_schema_identity", "metadata_schema_version", "operational_domain",
    "component_service_scope", "dependency_role_scope", "problem_failure_class",
    "observable_signal_symptom_classes", "applicability_constraints",
    "canonical_section_identity_scheme",
]
_PRODUCTION_DOCUMENTS = frozenset({
    "credential_abuse_brute_force_response.md",
    "database_slow_query_api_timeout_diagnosis.md",
    "high_memory_oom_service_crash_handling.md",
    "external_api_timeout_outage_handling.md",
    "shared_database_network_dependency_interruption.md",
    "http_429_rate_limit_qps_spike_handling.md",
})
_H2 = re.compile(r"(?m)^## [^\r\n]+\s*$")
_MARKDOWN_HEADING_LINE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")


def _has_meaningful_h2_body(content: str) -> bool:
    """Require a non-empty, non-heading line inside one canonical H2 boundary."""
    matches = tuple(_H2.finditer(content))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end():end]
        if any(
            line.strip() and _MARKDOWN_HEADING_LINE.match(line) is None
            for line in body.splitlines()
        ):
            return True
    return False


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


def _parse_document_v11(value: object, source_revision: str) -> ManifestDocument:
    if not isinstance(value, Mapping):
        raise KnowledgeValidationError("document entry must be a mapping")
    shape = _shape_findings(value, _DOCUMENT_KEYS_V11, "document")
    if shape:
        raise KnowledgeValidationError(shape[0].detail)
    metadata = _parse_metadata(value["metadata"])
    metadata_by_key = {item.key: item.value for item in metadata}
    if set(metadata_by_key) != _REQUIRED_DOCUMENT_METADATA:
        raise KnowledgeValidationError("document governance metadata is incomplete")
    for key in ("observable_signal_symptom_classes", "applicability_constraints"):
        parts = metadata_by_key[key].split("|")
        if parts != sorted(set(parts)) or any(not part for part in parts):
            raise KnowledgeValidationError("multi-value metadata is not canonical")
    if (
        metadata_by_key["metadata_schema_identity"] != "spec014-knowledge-metadata"
        or metadata_by_key["metadata_schema_version"] != "1.0"
        or metadata_by_key["canonical_section_identity_scheme"] != "markdown_h2_heading_v1"
    ):
        raise KnowledgeValidationError("document metadata schema is incompatible")
    try:
        document = ManifestDocument(
            document_id=value["document_id"],  # type: ignore[arg-type]
            document_version=value["document_version"],  # type: ignore[arg-type]
            source_path=value["source_path"],  # type: ignore[arg-type]
            expected_content_hash=value["expected_content_hash"],  # type: ignore[arg-type]
            status=DocumentStatus(value["status"]),
            source_classification=SourceClassification(value["source_classification"]),
            approval_reference=value["approval_reference"],  # type: ignore[arg-type]
            outbound_eligible=value["outbound_eligible"],  # type: ignore[arg-type]
            content_type=ContentType(value["content_type"]),
            metadata=metadata,
            approval_state=value["approval_state"],  # type: ignore[arg-type]
            production_eligible=value["production_eligible"],  # type: ignore[arg-type]
            committed_source_reference=value["committed_source_reference"],  # type: ignore[arg-type]
            security_classification=value["security_classification"],  # type: ignore[arg-type]
            outbound_scope=value["outbound_scope"],  # type: ignore[arg-type]
            knowledge_type=value["knowledge_type"],  # type: ignore[arg-type]
            guidance_authority=value["guidance_authority"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise KnowledgeValidationError("document entry contains an invalid field") from exc
    expected_reference = f"{source_revision}:docs/knowledge/{document.source_path}"
    if document.committed_source_reference != expected_reference:
        raise KnowledgeValidationError("committed source reference is inconsistent")
    authorized_guidance = (
        document.knowledge_type in {"SOP", "RUNBOOK"}
        and document.guidance_authority == "SOP_BACKED_ELIGIBLE"
    ) or (
        document.knowledge_type == "OTHER_APPROVED_OPERATIONAL_REFERENCE"
        and document.guidance_authority == "CONTEXTUAL_ONLY"
    )
    if (
        document.approval_state != "APPROVED" or document.status is not DocumentStatus.ACTIVE
        or not document.production_eligible
        or document.source_classification is not SourceClassification.APPROVED_OPERATIONAL_KNOWLEDGE
        or document.security_classification != "NON_SECRET_SYNTHETIC_MOCK_OPERATIONAL_KNOWLEDGE"
        or not document.outbound_eligible
        or document.outbound_scope != "SPEC014_APPROVED_EMBEDDING_PROVIDER_ONLY"
        or not authorized_guidance
    ):
        raise KnowledgeValidationError("document governance decision is not production eligible")
    return document


def _metadata_vocabulary(value: object) -> tuple[MetadataItem, ...]:
    if not isinstance(value, Mapping) or set(value) != _METADATA_VOCABULARY_KEYS:
        raise KnowledgeValidationError("metadata vocabulary shape is invalid")
    if (
        value["multi_value_order"] != "LEXICOGRAPHIC_ASCENDING_UNIQUE"
        or value["multi_value_separator"] != "|"
        or value["required_filter_failure_disposition"] != "NOT_APPLICABLE"
        or value["unsupported_constraint_disposition"] != "NOT_APPLICABLE"
        or value["no_applicable_candidate_resolution"] != "NO_MATCH"
        or value["required_document_metadata_keys"] != _REQUIRED_DOCUMENT_METADATA_ORDER
        or value["required_exact_match_filter_keys"] != [
            "component_service_scope", "dependency_role_scope",
            "operational_domain", "problem_failure_class",
        ]
    ):
        raise KnowledgeValidationError("metadata vocabulary contract is incompatible")
    return tuple(MetadataItem(key, json.dumps(value[key], ensure_ascii=False, separators=(",", ":"))) for key in sorted(value))


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
    schema_version = raw_manifest.get("schema_version")
    expected_root = _ROOT_KEYS_V11 if schema_version == "1.1" else _ROOT_KEYS
    shape = _shape_findings(raw_manifest, expected_root, "manifest")
    if shape:
        return _rejected(shape)
    if schema_version not in {"1.0", "1.1"} or raw_manifest.get("canonicalization_version") != "1.0":
        return _rejected((_fact(AdmissionFailureCode.UNSUPPORTED_SCHEMA, "manifest.schema_version", "manifest schema or canonicalization version is unsupported"),))
    raw_documents = raw_manifest.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        return _rejected((_fact(AdmissionFailureCode.INVALID_MANIFEST, "manifest.documents", "documents must be a non-empty list"),))

    findings: list[AdmissionFailureFact] = []
    documents: list[ManifestDocument] = []
    source_revision = raw_manifest.get("source_revision", "")
    for index, raw_document in enumerate(raw_documents):
        if isinstance(raw_document, Mapping):
            expected_document = _DOCUMENT_KEYS_V11 if schema_version == "1.1" else _DOCUMENT_KEYS
            entry_shape = _shape_findings(raw_document, expected_document, f"document.{index}")
            findings.extend(entry_shape)
            if entry_shape:
                continue
        try:
            documents.append(
                _parse_document_v11(raw_document, source_revision)
                if schema_version == "1.1"
                else _parse_document(raw_document)
            )
        except KnowledgeValidationError:
            code = (
                AdmissionFailureCode.GOVERNANCE_METADATA_INVALID
                if schema_version == "1.1"
                else AdmissionFailureCode.INVALID_MANIFEST
            )
            findings.append(_fact(code, f"document.{index}", "document entry is invalid"))

    try:
        common = dict(
            schema_version=raw_manifest["schema_version"],
            canonicalization_version=raw_manifest["canonicalization_version"],
            manifest_id=raw_manifest["manifest_id"],
            corpus_id=raw_manifest["corpus_id"],
            corpus_version=raw_manifest["corpus_version"],
            documents=tuple(documents),
        )
        if schema_version == "1.1":
            manifest = GovernedManifest(
                **common,
                schema_identity=raw_manifest["schema_identity"],
                release_id=raw_manifest["release_id"],
                release_version=raw_manifest["release_version"],
                canonical_release_reference=raw_manifest["canonical_release_reference"],
                release_status=raw_manifest["release_status"],
                release_approver_role=raw_manifest["release_approver_role"],
                release_approval_reference=raw_manifest["release_approval_reference"],
                governed_source_root=raw_manifest["governed_source_root"],
                source_revision=raw_manifest["source_revision"],
                content_hash_algorithm=raw_manifest["content_hash_algorithm"],
                metadata_schema_identity=raw_manifest["metadata_schema_identity"],
                metadata_schema_version=raw_manifest["metadata_schema_version"],
                metadata_vocabulary=_metadata_vocabulary(raw_manifest["metadata_vocabulary"]),
            )
        else:
            manifest = GovernedManifest(**common)
    except (KnowledgeValidationError, TypeError, ValueError):
        return _rejected((*findings, _fact(AdmissionFailureCode.INVALID_MANIFEST, "manifest", "manifest identity fields are invalid")))

    if schema_version == "1.1":
        release_valid = (
            manifest.release_id == "spec014-production-knowledge-release"
            and manifest.release_version == "1.0"
            and manifest.canonical_release_reference == "spec014-production-knowledge-release@1.0"
            and manifest.release_status == "APPROVED"
            and manifest.release_approval_reference == "SPEC014-CORPUS-RELEASE-V1-APPROVAL-20260926"
            and manifest.governed_source_root == "docs/knowledge/"
            and manifest.content_hash_algorithm == "SHA-256"
            and manifest.metadata_schema_identity == "spec014-knowledge-metadata"
            and manifest.metadata_schema_version == "1.0"
        )
        if not release_valid:
            findings.append(_fact(AdmissionFailureCode.RELEASE_NOT_APPROVED, "manifest.release", "production release is not approved or compatible"))
        paths = tuple(document.source_path for document in documents)
        if len(paths) != 6 or frozenset(paths) != _PRODUCTION_DOCUMENTS:
            findings.append(_fact(AdmissionFailureCode.RELEASE_MEMBERSHIP_INVALID, "manifest.documents", "production release membership is not the approved exact-six set"))

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
        if schema_version == "1.1" and not _has_meaningful_h2_body(content):
            findings.append(_fact(AdmissionFailureCode.CONTENT_NOT_MEANINGFUL, "document.content", "governed source has no canonical Knowledge section"))
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


def admit_production_manifest(
    manifest_path: str | Path,
    *,
    repository_root: str | Path,
) -> ManifestAdmissionResult:
    """Load the approved release, derive its governed root, and verify Git blob bytes."""
    root = Path(repository_root).resolve(strict=True)
    path = Path(manifest_path)
    if not path.is_absolute():
        path = root / path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _rejected((_fact(AdmissionFailureCode.INVALID_MANIFEST, "manifest", "production manifest is unreadable"),))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != "1.1":
        return _rejected((_fact(AdmissionFailureCode.UNSUPPORTED_SCHEMA, "manifest.schema_version", "production manifest must use schema 1.1"),))
    governed = raw.get("governed_source_root")
    if governed != "docs/knowledge/":
        return _rejected((_fact(AdmissionFailureCode.UNSAFE_SOURCE_PATH, "manifest.governed_source_root", "production source root is not approved"),))
    source_root = root / "docs" / "knowledge"
    try:
        candidates = tuple(sorted(item.name for item in source_root.iterdir() if item.is_file()))
    except OSError:
        candidates = ()
    result = admit_manifest(raw, source_root=source_root, candidate_sources=candidates)
    if not result.accepted:
        return result
    revision = raw.get("source_revision")
    assert isinstance(revision, str)
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"], cwd=root,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for document in result.manifest.documents:  # type: ignore[union-attr]
            relative = f"docs/knowledge/{document.source_path}"
            blob = subprocess.run(
                ["git", "show", f"{revision}:{relative}"], cwd=root,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout
            working = (source_root / document.source_path).read_bytes()
            if blob != working or hashlib.sha256(blob).hexdigest() != document.expected_content_hash:
                raise ValueError("source blob mismatch")
    except (OSError, subprocess.CalledProcessError, ValueError):
        return _rejected((_fact(AdmissionFailureCode.SOURCE_REVISION_MISMATCH, "manifest.source_revision", "source revision does not resolve to the approved raw bytes"),))
    return result
