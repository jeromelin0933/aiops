"""Evaluation-only immutable record contracts and linked identity helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .identity import canonical_bytes, commitment, identity, require_identity


class LedgerConflict(ValueError):
    """One identity was replayed with contradictory semantic content."""


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    domain: str
    record_id: str
    parent_id: str | None
    identity_key_json: bytes
    semantic_json: bytes
    semantic_commitment: str

    @classmethod
    def create(cls, domain: str, key: object, payload: dict,
               parent_id: str | None = None) -> "LedgerRecord":
        if domain not in {"evaluation_run", "execution", "observation", "judgment",
                          "review", "re_adjudication", "report"}:
            raise ValueError("unsupported ledger domain")
        if type(payload) is not dict:
            raise ValueError("ledger payload must be a dictionary")
        frozen = canonical_bytes(payload)
        return cls(domain, identity(domain, key), parent_id, canonical_bytes(key), frozen, commitment(payload))

    def validate(self) -> None:
        require_identity(self.record_id, self.domain)
        if self.parent_id is not None and not self.parent_id.startswith("eval:"):
            raise ValueError("ledger parent must be an evaluation identity")
        import json
        key = json.loads(self.identity_key_json)
        if canonical_bytes(key) != self.identity_key_json or identity(self.domain, key) != self.record_id:
            raise ValueError("ledger identity key mismatch")
        payload = json.loads(self.semantic_json)
        if canonical_bytes(payload) != self.semantic_json or commitment(payload) != self.semantic_commitment:
            raise ValueError("ledger semantic commitment mismatch")
