"""Fixed evaluation-only policy for SPEC-017 blind-packet leakage isolation."""

from __future__ import annotations

from dataclasses import dataclass

from .identity import commitment


@dataclass(frozen=True, slots=True)
class TrustedLeakagePolicy:
    policy_identity: str
    required_canaries: tuple[str, ...]
    normalization_version: str

    def __post_init__(self) -> None:
        if (not isinstance(self.policy_identity, str) or not self.policy_identity
                or self.normalization_version != "UNICODE-NFKC-CASEFOLD-ALNUM-v1"):
            raise ValueError("trusted leakage policy identity or normalization is invalid")
        if (not isinstance(self.required_canaries, tuple) or not self.required_canaries
                or any(not isinstance(item, str) or not item for item in self.required_canaries)
                or len(set(self.required_canaries)) != len(self.required_canaries)):
            raise ValueError("trusted leakage policy requires a non-empty unique canary set")

    def material(self) -> dict:
        return {
            "policy_identity": self.policy_identity,
            "required_canaries": list(self.required_canaries),
            "normalization_version": self.normalization_version,
        }

    @property
    def policy_commitment(self) -> str:
        return commitment(self.material())


TRUSTED_LEAKAGE_POLICY = TrustedLeakagePolicy(
    "SPEC-017-S3-BLIND-ISOLATION-v1",
    ("GT-CANARY-017", "ORACLE-CANARY-017"),
    "UNICODE-NFKC-CASEFOLD-ALNUM-v1",
)
