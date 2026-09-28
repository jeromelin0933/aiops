"""Canonical Python identity for the SPEC-015 claim category value."""

from enum import Enum


class ClaimCategory(str, Enum):
    OBSERVED_FACT = "OBSERVED_FACT"
    ANALYTICAL_INFERENCE = "ANALYTICAL_INFERENCE"
    KNOWLEDGE_BACKED_GUIDANCE = "KNOWLEDGE_BACKED_GUIDANCE"
    MODEL_SUGGESTED_GUIDANCE = "MODEL_SUGGESTED_GUIDANCE"
