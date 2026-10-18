"""Evaluation-only trust anchor for PM approval and amendment records.

The trusted caller supplies these records independently of Oracle/ledger input.
No approval can be created by reading production output or the evaluation DB.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType

from .identity import require_identity


PM_AUTHORITY = "林子豪（PM）"


@dataclass(frozen=True, slots=True)
class TrustedOracleApproval:
    reference: str
    authority: str
    oracle_revision_id: str
    semantic_content_commitment: str
    approved_at: str
    parent_revision_id: str | None


@dataclass(frozen=True, slots=True)
class TrustedAmendmentAuthorization:
    reference: str
    authority: str
    original_judgment_id: str
    original_oracle_revision_id: str
    target_oracle_revision_id: str


class TrustedApprovalRegistry:
    """Immutable snapshot of records obtained from an independent PM authority.

    This registry is an injected trust boundary, not a self-registration API.
    Deployments must source its records independently of Candidate-F inputs.
    """

    def __init__(self, oracle_approvals: tuple[TrustedOracleApproval, ...] = (),
                 amendments: tuple[TrustedAmendmentAuthorization, ...] = ()) -> None:
        approvals = {}
        for record in oracle_approvals:
            if type(record) is not TrustedOracleApproval or not record.reference or record.reference in approvals:
                raise ValueError("invalid or duplicate trusted Oracle approval")
            require_identity(record.oracle_revision_id, "oracle_revision")
            if record.parent_revision_id is not None:
                require_identity(record.parent_revision_id, "oracle_revision")
            if record.authority != PM_AUTHORITY or not record.semantic_content_commitment.startswith("sha256:") or not record.approved_at:
                raise ValueError("invalid trusted Oracle approval")
            approvals[record.reference] = record
        authorizations = {}
        for record in amendments:
            if type(record) is not TrustedAmendmentAuthorization or not record.reference or record.reference in authorizations:
                raise ValueError("invalid or duplicate trusted amendment authorization")
            require_identity(record.original_judgment_id, "judgment")
            require_identity(record.original_oracle_revision_id, "oracle_revision")
            require_identity(record.target_oracle_revision_id, "oracle_revision")
            if record.authority != PM_AUTHORITY or record.original_oracle_revision_id == record.target_oracle_revision_id:
                raise ValueError("invalid trusted amendment authorization")
            authorizations[record.reference] = record
        self._approvals = MappingProxyType(approvals)
        self._amendments = MappingProxyType(authorizations)

    def verify_oracle(self, revision: object) -> None:
        record = self._approvals.get(revision.approval_reference)
        if record is None or (record.authority, record.oracle_revision_id,
                              record.semantic_content_commitment, record.approved_at,
                              record.parent_revision_id) != (
                                  revision.approval_authority, revision.oracle_revision_id,
                                  revision.semantic_content_commitment, revision.approved_at,
                                  revision.parent_revision_id):
            raise ValueError("Oracle approval reference is unknown or does not bind exact revision")

    def verify_amendment(self, reference: str, original_judgment_id: str,
                         original_oracle_revision_id: str, target_oracle_revision_id: str) -> None:
        record = self._amendments.get(reference)
        if record is None or (record.authority, record.original_judgment_id,
                              record.original_oracle_revision_id, record.target_oracle_revision_id) != (
                                  PM_AUTHORITY, original_judgment_id,
                                  original_oracle_revision_id, target_oracle_revision_id):
            raise ValueError("amendment authorization is unknown or out of scope")


TRUSTED_ORACLE_APPROVAL_SCHEMA = "spec017.trusted-oracle-approval.v1"


def load_trusted_oracle_approvals(path: str | Path) -> TrustedApprovalRegistry:
    """Load a trust snapshot which is intentionally separate from Oracle content.

    The strict schema contains only approval bindings.  It cannot carry Oracle
    semantics and therefore cannot turn the Oracle artifact into a self-issued
    approval source.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"schema", "approvals"}:
        raise ValueError("trusted Oracle approval input schema is invalid")
    if raw["schema"] != TRUSTED_ORACLE_APPROVAL_SCHEMA:
        raise ValueError("unsupported trusted Oracle approval input")
    if not isinstance(raw["approvals"], list) or not raw["approvals"]:
        raise ValueError("trusted Oracle approval input must not be empty")
    required = {
        "reference", "authority", "oracle_revision_id",
        "semantic_content_commitment", "approved_at", "parent_revision_id",
    }
    records = []
    for item in raw["approvals"]:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("trusted Oracle approval record schema is invalid")
        records.append(TrustedOracleApproval(**item))
    return TrustedApprovalRegistry(tuple(records))
