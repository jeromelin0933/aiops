"""Complete, committed classification of production-sensitive source roots."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .identity import commitment
from .isolation import IsolationViolation, _FORBIDDEN_NAMES, _normalized


class SensitiveSurface(str, Enum):
    GENERATION_PROMPT = "GENERATION_PROMPT"
    EVIDENCE_QUERY = "EVIDENCE_QUERY"
    KNOWLEDGE_QUERY = "KNOWLEDGE_QUERY"
    CANDIDATE_D_INPUT = "CANDIDATE_D_INPUT"
    CANDIDATE_E_PAYLOAD = "CANDIDATE_E_PAYLOAD"
    PRODUCTION_PERSISTENCE = "PRODUCTION_PERSISTENCE"


class SourceClassification(str, Enum):
    APPROVED = "APPROVED"
    REVIEWED_EXCLUSION = "REVIEWED_EXCLUSION"


@dataclass(frozen=True, slots=True)
class SensitiveInventoryEntry:
    relative_path: str
    classification: SourceClassification
    source_commitment: str
    allowed_dependencies: tuple[str, ...]
    allowed_fields: tuple[str, ...]
    allowed_defensive_terms: tuple[str, ...]
    surfaces: tuple[SensitiveSurface, ...] = ()
    entry_points: tuple[str, ...] = ()
    exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SensitiveInventoryAudit:
    scanned_paths: tuple[str, ...]
    inventory_commitment: str


_GOVERNED_ROOTS = (
    "src/llm_generation",
    "src/incident_evidence",
    "src/knowledge_index",
    "src/runtime_orchestration",
    "src/rca_persistence",
    "src/incident_management",
    "src/event_detection",
    "src/alert_correlation",
    "src/shadow_management",
    "src/rca_integration",
)
_MANIFEST_SHA256 = "c2d4152aeeb2180307e4460a071e6f10d091e99edc92cb6c31b2d39268fbd722"
_MANIFEST_PATH = Path(__file__).with_name("production_sensitive_inventory.json")


def _load_manifest() -> tuple[dict, tuple[SensitiveInventoryEntry, ...]]:
    try:
        raw = _MANIFEST_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _MANIFEST_SHA256:
            raise IsolationViolation("production-sensitive classification manifest commitment changed")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IsolationViolation("production-sensitive classification manifest is unavailable") from exc
    if data.get("schema_version") != 1 or tuple(data.get("governed_roots", ())) != _GOVERNED_ROOTS:
        raise IsolationViolation("production-sensitive classification manifest is invalid")
    entries = []
    try:
        for item in data["files"]:
            classification = SourceClassification(item["classification"])
            entry = SensitiveInventoryEntry(
                item["path"], classification, item["source_commitment"],
                tuple(item["allowed_dependencies"]), tuple(item["allowed_fields"]),
                tuple(item["allowed_defensive_terms"]),
                tuple(SensitiveSurface(value) for value in item.get("surfaces", ())),
                tuple(item.get("entry_points", ())), item.get("exclusion_reason"),
            )
            if classification is SourceClassification.APPROVED:
                if not entry.surfaces or not entry.entry_points or entry.exclusion_reason is not None:
                    raise ValueError("approved entry is incomplete")
            elif not entry.exclusion_reason or entry.surfaces or entry.entry_points:
                raise ValueError("reviewed exclusion is incomplete")
            entries.append(entry)
    except (KeyError, TypeError, ValueError) as exc:
        raise IsolationViolation("production-sensitive classification entry is invalid") from exc
    paths = [entry.relative_path for entry in entries]
    if len(paths) != len(set(paths)):
        raise IsolationViolation("production-sensitive classification paths are not unique")
    return data, tuple(entries)


_MANIFEST, PRODUCTION_SENSITIVE_INVENTORY = _load_manifest()
REVIEWED_EXCLUSIONS = tuple(
    entry for entry in PRODUCTION_SENSITIVE_INVENTORY
    if entry.classification is SourceClassification.REVIEWED_EXCLUSION
)


def _source_surfaces(tree: ast.AST, package: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    dependencies: set[str] = set()
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            dependencies.add(package if node.level else node.module.split(".")[0])
        if isinstance(node, ast.Dict):
            fields.update(
                key.value for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
        elif isinstance(node, ast.keyword) and node.arg:
            fields.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            fields.add(node.target.id)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and ("CREATE TABLE" in node.value or "CREATE INDEX" in node.value
                   or "CREATE UNIQUE INDEX" in node.value)):
            fields.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value))
    return tuple(sorted(dependencies)), tuple(sorted(fields))


def _source_values(tree: ast.AST) -> tuple[str, ...]:
    values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            values.append(node.id)
        elif isinstance(node, ast.Attribute):
            values.append(node.attr)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            values.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
    return tuple(values)


def validate_production_sensitive_inventory(
    repository_root: str | Path,
    inventory: tuple[SensitiveInventoryEntry, ...] = PRODUCTION_SENSITIVE_INVENTORY,
) -> SensitiveInventoryAudit:
    """Enumerate every governed module and enforce its committed classification."""
    if tuple(inventory) != PRODUCTION_SENSITIVE_INVENTORY:
        raise IsolationViolation("production-sensitive inventory cannot be reduced or replaced")
    root = Path(repository_root).resolve()
    expected = {entry.relative_path for entry in PRODUCTION_SENSITIVE_INVENTORY}
    discovered: set[str] = set()
    for relative_root in _GOVERNED_ROOTS:
        directory = root / relative_root
        if not directory.is_dir():
            raise IsolationViolation(f"sensitive source root is unresolved: {relative_root}")
        discovered.update(path.relative_to(root).as_posix() for path in directory.rglob("*.py"))
    unknown = discovered - expected
    missing = expected - discovered
    if unknown:
        raise IsolationViolation(f"unclassified production source: {sorted(unknown)[0]}")
    if missing:
        raise IsolationViolation(f"inventory source is missing: {sorted(missing)[0]}")

    for entry in PRODUCTION_SENSITIVE_INVENTORY:
        path = (root / entry.relative_path).resolve()
        if root not in path.parents or not path.is_file():
            raise IsolationViolation(f"inventory path is unresolved: {entry.relative_path}")
        try:
            source = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise IsolationViolation(f"inventory source cannot be parsed: {entry.relative_path}") from exc
        package = Path(entry.relative_path).parts[1]
        dependencies, fields = _source_surfaces(tree, package)
        if dependencies != entry.allowed_dependencies:
            raise IsolationViolation(f"non-allowlisted dependency in production source: {entry.relative_path}")
        if fields != entry.allowed_fields:
            raise IsolationViolation(f"non-allowlisted production-bound field: {entry.relative_path}")
        definitions = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if set(entry.entry_points) - definitions:
            raise IsolationViolation(f"approved entry point is unresolved: {entry.relative_path}")
        defensive = set(entry.allowed_defensive_terms)
        for value in _source_values(tree):
            normalized = _normalized(value)
            hits = {name for name in _FORBIDDEN_NAMES if name in normalized}
            if not hits <= defensive:
                raise IsolationViolation(f"forbidden evaluation semantics in production source: {entry.relative_path}")
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            elif isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            if any(module == "rca_evaluation" or module.startswith("rca_evaluation.") for module in modules):
                raise IsolationViolation(f"production source imports evaluation code: {entry.relative_path}")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != entry.source_commitment:
            label = "reviewed exclusion" if entry.classification is SourceClassification.REVIEWED_EXCLUSION else "approved source"
            raise IsolationViolation(f"{label} commitment changed: {entry.relative_path}")

    return SensitiveInventoryAudit(tuple(sorted(discovered)), commitment(_MANIFEST))
