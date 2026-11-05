"""Competition capture preparation only: no runtime, source I/O or store creation.

This module does not authorize capture or resolve pending selector contracts.
The per-source request policy uses the existing SPEC-013 extension point.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path, PureWindowsPath

import yaml

from incident_evidence.capture_service import (
    DEFAULT_SOURCE_REQUEST_POLICIES, SourceRequestPolicy,
)
from incident_evidence.contracts import EvidenceSource, SourceAdapterRequest
from knowledge_index.identity import canonical_serialize
from .config import RuntimeConfig, RuntimeAuthorityStoresConfig, RuntimeWorkStoreConfig


AUTHORITY = "PM-AIOPS-COMP-P0-FINAL-DEMO-GATE-20261103"
BASELINE = "8e3faca4b27a81400c26e4964436bde13aa3c643"
PARENT_RUN = "competition-capture-20261104-01"
EXTERNAL_ROOT = PureWindowsPath(
    r"C:\Users\vincent\AppData\Local\AIOps\Handoffs\competition-capture-20261104-01"
)
SCENARIO_SHA = "baa97bce3afe413049c08b726e2d75794019755db358a700ea3990aaa6d97f8e"
INPUT_SHA = {
    "s5": "acf18f78763a5983943bbcf07271f389d17fa54ef75cdd632ca7d6f9fc827f45",
    "s2": "64bb814d49c784126d16c8d3efeff168297ad3fd863e79859fec2f171f04b511",
}
STORE_PATHS = {
    "event": "events/event_store.jsonl",
    "correlation": "stores/correlation.sqlite3",
    "incident": "stores/incidents.sqlite3",
    "shadow": "stores/shadows.sqlite3",
    "work": "stores/runtime_work.sqlite3",
    "evidence": "stores/evidence.sqlite3",
}


def child_binding(case: str) -> dict:
    """Case IDs select controller inputs only, never Event/Incident semantics."""
    if case not in INPUT_SHA:
        raise ValueError("only the exact S5/S2 preparation cases are supported")
    run_id = PARENT_RUN + "-" + case
    root = EXTERNAL_ROOT / run_id
    return {
        "run_id": run_id, "root": str(root),
        "stores": {key: str(root / value) for key, value in STORE_PATHS.items()},
        "log_output": str(root / "observations" / "aiops.json.log"),
        "metric_observation_output": str(root / "observations" / "metrics.jsonl"),
        "raw_metric_sample_count": "NOT AVAILABLE",
        "maximum_seconds_proposed": 600, "maximum_triggers_proposed": 1,
        "maximum_captures_proposed": 1,
    }


def capture_only_runtime(base: RuntimeConfig) -> RuntimeConfig:
    """Existing factory takes the child's external root as project_root.

    This does not construct the application or initialize any stores.
    """
    return replace(
        base, rca=None,
        work_store=RuntimeWorkStoreConfig("sqlite3", STORE_PATHS["work"]),
        authority_stores=RuntimeAuthorityStoresConfig(
            STORE_PATHS["event"], STORE_PATHS["correlation"],
            STORE_PATHS["incident"], STORE_PATHS["shadow"],
        ),
        loop=replace(base.loop, mode="run-until-idle"),
    )


def validate_capture_boundary(config: RuntimeConfig, child: dict) -> None:
    """Reject altered roots, paths or any RCA-enabled configuration."""
    expected = [child_binding(case) for case in INPUT_SHA]
    if child not in expected or config.rca is not None:
        raise ValueError("capture root/run or disabled-provider binding mismatch")
    fields = config.authority_stores
    actual = (fields.event_store_path, fields.correlation_state_path,
              fields.incident_store_path, fields.shadow_store_path, config.work_store.path)
    if (actual != tuple(STORE_PATHS[k] for k in ("event", "correlation", "incident", "shadow", "work"))
            or config.loop.mode != "run-until-idle"):
        raise ValueError("capture store or runtime mode mismatch")


def demo_metric_request_policies() -> dict[EvidenceSource, SourceRequestPolicy]:
    """Latency in milliseconds for BOTH cases, not a shared-dependency metric.

    No selector is removed, injected or relabelled. Series/context approval is
    still required before this proposal may be used in a real capture.
    """
    policies = dict(DEFAULT_SOURCE_REQUEST_POLICIES)
    policies[EvidenceSource.PROMETHEUS] = replace(
        policies[EvidenceSource.PROMETHEUS],
        query_resource="api_p95_latency_ms",
        query_semantic_identity="s5-s2-demo-latency-ms-v1",
        query_resolution_seconds=15.0,
    )
    return policies


def selector_preparation_findings(request: SourceAdapterRequest,
                                  observed_label_names: frozenset[str]) -> tuple[str, ...]:
    """Advisory blockers only; never changes the request or queries a source.

    Labels must eventually come from actual deployment evidence. A synthetic
    label set is suitable only for an isolated compatibility test.
    """
    findings = set()
    by_field: dict[str, set[str]] = {}
    for selector in request.selectors:
        by_field.setdefault(selector.field_name, set()).add(selector.normalized_value)
    if not by_field:
        findings.add("MISSING_BOUNDED_SELECTOR")
    if "multiple" in by_field.get("service_name", set()):
        findings.add("AGGREGATE_SERVICE_RULE_APPROVAL_REQUIRED")
        if "downstream_service" not in by_field:
            findings.add("AGGREGATE_DEPENDENCY_EVIDENCE_MISSING")
    if set(by_field) - observed_label_names:
        findings.add("SELECTOR_LABELS_NOT_AVAILABLE")
    return tuple(sorted(findings))


def prepare_capture(repository: Path, base: RuntimeConfig) -> dict:
    """Read governed inputs/artifact existence only. Never unpickle a model.

    Pending contracts deliberately cannot be enabled with caller flags.
    """
    data = (repository / "configs/scenarios.yaml").read_bytes()
    if sha256(data).hexdigest() != SCENARIO_SHA:
        raise ValueError("approved scenario configuration commitment changed")
    raw = yaml.safe_load(data)
    inputs = {}
    runtime = capture_only_runtime(base)
    for case in INPUT_SHA:
        scoped = {k: raw[k] for k in ("runtime", "log", "metrics", "background_errors")}
        scoped["scenario_input"] = raw["scenarios"][case.upper()]
        digest = sha256(canonical_serialize(scoped).encode()).hexdigest()
        if digest != INPUT_SHA[case]:
            raise ValueError("approved scenario input commitment changed")
        binding = child_binding(case)
        validate_capture_boundary(runtime, binding)
        inputs[case] = {"input_commitment": digest, "binding": binding}
    models = {}
    for name in ("log", "metrics"):
        path = repository / "models" / (name + "_isolation_forest.pkl")
        models[name] = {"path": str(path), "exists": path.is_file(),
                        "sha256": sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
                        "loader_compatibility": "SEE CONTROLLED MODEL BUILD EVIDENCE",
                        "artifact_provenance": "REQUIRES MODEL BUILD EVIDENCE BINDING"}
    policies = {key.value: asdict(value) for key, value in demo_metric_request_policies().items()}
    proposal = {"parent_run": PARENT_RUN, "inputs": inputs,
                "runtime": asdict(runtime), "request_policies": policies}
    return {
        "authority": AUTHORITY, "baseline": BASELINE,
        "source_binding": "PENDING HUMAN CLEAN SOURCE CHECKPOINT",
        "capture_preflight": "BLOCKED", "execution_allowed": False,
        "proposal": proposal,
        "proposal_commitment": sha256(canonical_serialize(proposal).encode()).hexdigest(),
        "commitment_status": "PREPARATION ONLY; NOT EFFECTIVE CAPTURE CONFIG OR APPROVAL",
        "models": models,
        "blockers": [("GOVERNED_MODEL_ARTIFACTS_REQUIRED" if not all(m["exists"] for m in models.values())
                      else "MODEL_BUILD_EVIDENCE_BINDING_REQUIRED"),
                     "OBSERVABILITY_DEPLOYMENT_BINDING_REQUIRED",
                     "AGGREGATE_AND_METRIC_SELECTOR_CONTRACT_APPROVAL_REQUIRED",
                     "CONTROLLER_BOUNDED_IO_REQUIRED",
                     "FINAL_CONTROLLER_CHECKPOINT_AND_AUTHORIZATION_REQUIRED"],
    }
