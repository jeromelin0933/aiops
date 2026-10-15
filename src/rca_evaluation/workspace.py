"""Physically isolated, per-execution evaluation workspaces."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from .identity import canonical_bytes, commitment
from .observation import CapturedInputBoundary


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


class ProductionRootKind(str, Enum):
    EVENT = "EVENT"
    INCIDENT = "INCIDENT"
    EVIDENCE = "EVIDENCE"
    KNOWLEDGE = "KNOWLEDGE"
    RCA = "RCA"
    GENERATION = "GENERATION"
    RUNTIME = "RUNTIME"


@dataclass(frozen=True, slots=True)
class ProductionRoot:
    kind: ProductionRootKind
    path: str | Path


class TrustedIsolationPolicy:
    """Complete trusted snapshot of every production-owned persistence root."""

    __slots__ = ("_roots", "authority_reference", "policy_commitment")

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("trusted isolation policy is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, roots: tuple[ProductionRoot, ...], *, authority_reference: str) -> None:
        if not isinstance(authority_reference, str) or not authority_reference.strip():
            raise ValueError("trusted isolation authority reference is required")
        if not isinstance(roots, tuple) or any(type(root) is not ProductionRoot for root in roots):
            raise TypeError("production roots must be an immutable trusted tuple")
        by_kind = {}
        for root in roots:
            if not isinstance(root.kind, ProductionRootKind) or root.kind in by_kind:
                raise ValueError("production root inventory has invalid or duplicate kinds")
            resolved = Path(root.path).resolve()
            by_kind[root.kind] = resolved
        if set(by_kind) != set(ProductionRootKind):
            raise ValueError("production root inventory is empty or incomplete")
        self._roots = MappingProxyType(by_kind)
        self.authority_reference = authority_reference
        self.policy_commitment = commitment({
            "authority_reference": authority_reference,
            "roots": {kind.value: str(by_kind[kind]) for kind in sorted(by_kind, key=lambda item: item.value)},
        })

    @property
    def roots(self) -> tuple[tuple[ProductionRootKind, Path], ...]:
        return tuple(sorted(self._roots.items(), key=lambda item: item[0].value))

    def assert_isolated(self, evaluation_root: Path) -> None:
        resolved = evaluation_root.resolve()
        for kind, production_root in self.roots:
            if _overlaps(resolved, production_root):
                raise ValueError(
                    f"evaluation workspace overlaps trusted {kind.value} production root"
                )


@dataclass(frozen=True, slots=True)
class ExecutionWorkspace:
    path: Path
    execution_id: str
    manifest_commitment: str

    @classmethod
    def create(cls, evaluation_root: str | Path, captured: CapturedInputBoundary,
               *, scenario_id: str, canary_commitment: str,
               isolation_policy: TrustedIsolationPolicy) -> "ExecutionWorkspace":
        if not isinstance(captured, CapturedInputBoundary):
            raise TypeError("captured must be a CapturedInputBoundary")
        root = Path(evaluation_root).resolve()
        if root.name != "rca_evaluation":
            raise ValueError("workspace root must be a dedicated rca_evaluation directory")
        if type(isolation_policy) is not TrustedIsolationPolicy:
            raise TypeError("a trusted complete isolation policy is required")
        isolation_policy.assert_isolated(root)
        for name, value in (("scenario_id", scenario_id), ("canary_commitment", canary_commitment)):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed evaluation value")

        directory_name = hashlib.sha256(captured.execution_id.encode("utf-8")).hexdigest()
        path = root / "workspaces" / directory_name
        manifest = {
            "execution_id": captured.execution_id,
            "event_id": captured.event_id,
            "event_content_commitment": captured.event_content_commitment,
            "scenario_id": scenario_id,
            "canary_commitment": canary_commitment,
            "isolation_policy_commitment": isolation_policy.policy_commitment,
        }
        raw = canonical_bytes(manifest)
        marker = commitment(manifest)
        (root / "workspaces").mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir()
            (path / "manifest.json").write_bytes(raw)
        except FileExistsError:
            try:
                existing = (path / "manifest.json").read_bytes()
            except OSError as exc:
                raise ValueError("existing execution workspace is incomplete") from exc
            if existing != raw:
                raise ValueError("execution workspace identity has contradictory content")
        return cls(path, captured.execution_id, marker)

    def write_once(self, name: str, payload: dict) -> Path:
        if (not isinstance(name, str) or not name or not name.replace("_", "").isalnum()
                or name.startswith("_") or name.endswith("_")):
            raise ValueError("artifact name must be a simple evaluation-local identifier")
        raw = canonical_bytes(payload)
        target = self.path / f"{name}.json"
        try:
            with target.open("xb") as stream:
                stream.write(raw)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise ValueError("workspace artifact replay is contradictory")
        return target

    def read(self, name: str) -> dict:
        target = self.path / f"{name}.json"
        value = json.loads(target.read_bytes())
        if canonical_bytes(value) != target.read_bytes():
            raise ValueError("workspace artifact is not canonical")
        return value
