"""Preparation and existing adapter contracts only; never real capture evidence."""
from dataclasses import replace
import json
from pathlib import Path
import socket
import sqlite3

import pytest
import requests
import yaml

from runtime_orchestration import capture_preparation as p
from runtime_orchestration.config import load_runtime_config
from incident_evidence import EvidenceSource, SourceStatus
from incident_evidence.adapters import LokiRangeAdapter, PrometheusRangeAdapter
from test_incident_evidence_loki_adapter import request as log_request, SELECTOR_ALLOWLIST
from test_incident_evidence_prometheus_adapter import request as metric_request

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("network/provider/production-store boundary forbidden")
    for obj, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                      (socket, "getaddrinfo"), (socket, "create_connection")):
        monkeypatch.setattr(obj, name, deny)
    original = sqlite3.connect
    def connect(database, *args, **kwargs):
        assert Path(database).resolve().is_relative_to(tmp_path)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)


def test_exact_inputs_disjoint_external_paths_and_always_blocked():
    cfg = load_runtime_config(ROOT / "configs/runtime_orchestration.yaml")
    result = p.prepare_capture(ROOT, cfg)
    assert result == p.prepare_capture(ROOT, cfg)
    assert result["capture_preflight"] == "BLOCKED" and not result["execution_allowed"]
    assert result["source_binding"] == "PENDING HUMAN CLEAN SOURCE CHECKPOINT"
    assert result["proposal"]["runtime"]["rca"] is None
    paths = [v for case in p.INPUT_SHA for v in p.child_binding(case)["stores"].values()]
    assert len(paths) == len(set(paths)) == 12
    assert all(str(p.EXTERNAL_ROOT) in path and "knowledge" not in path and "Diagnostic" not in path for path in paths)
    assert result["proposal"]["inputs"]["s5"]["input_commitment"] == p.INPUT_SHA["s5"]


@pytest.mark.parametrize("case", ["S5", "s1", "../s5", "", "s5/../s2"])
def test_unapproved_child_rejected(case):
    with pytest.raises(ValueError): p.child_binding(case)


@pytest.mark.parametrize("change", ["rca", "path", "root", "mode"])
def test_provider_or_store_or_run_binding_mismatch_rejected(change):
    base = load_runtime_config(ROOT / "configs/runtime_orchestration.yaml")
    cfg = p.capture_only_runtime(base); child = p.child_binding("s5")
    if change == "rca": cfg = replace(cfg, rca=base.rca)
    elif change == "path": cfg = replace(cfg, work_store=replace(cfg.work_store, path="var/knowledge_index/knowledge.sqlite3"))
    elif change == "root": child["root"] = r"C:\unapproved"
    else: cfg = replace(cfg, loop=replace(cfg.loop, mode="continuous"))
    with pytest.raises(ValueError): p.validate_capture_boundary(cfg, child)


def test_scenario_hash_mismatch_stops_without_store_creation(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/scenarios.yaml").write_text("version: altered")
    with pytest.raises(ValueError, match="commitment"):
        p.prepare_capture(tmp_path, load_runtime_config(ROOT / "configs/runtime_orchestration.yaml"))
    assert not (tmp_path / "stores").exists()


def test_runtime_rca_disabled_construction_never_constructs_providers(tmp_path, monkeypatch):
    from runtime_orchestration import cli
    def deny(*args, **kwargs): raise AssertionError("RCA/Knowledge/LLM construction forbidden")
    for name in ("_build_rca_host", "GoogleEmbeddingAdapter", "create_google_client",
                 "create_google_generation_client", "SqliteKnowledgeStore", "ChromaIndexAdapter"):
        monkeypatch.setattr(cli, name, deny)
    raw = yaml.safe_load((ROOT / "configs/runtime_orchestration.yaml").read_text())
    raw.pop("rca")
    raw["work_store"]["path"] = p.STORE_PATHS["work"]
    raw["authority_stores"] = dict(event_store_path=p.STORE_PATHS["event"],
        correlation_state_path=p.STORE_PATHS["correlation"],
        incident_store_path=p.STORE_PATHS["incident"], shadow_store_path=p.STORE_PATHS["shadow"])
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    cfg = load_runtime_config(config_path)
    app = cli.build_runtime_application(cfg, project_root=tmp_path, config_path=config_path)
    app.close()  # No startup/run/Scenario invocation.
    assert (tmp_path / p.STORE_PATHS["incident"]).is_file()
    assert not (tmp_path / "knowledge.sqlite3").exists()


def test_metric_policy_is_existing_extension_not_global_default_or_selector_override():
    from incident_evidence import DEFAULT_SOURCE_REQUEST_POLICIES
    policies = p.demo_metric_request_policies()
    selected = policies[EvidenceSource.PROMETHEUS]
    assert selected.query_resource == "api_p95_latency_ms"
    assert selected.query_resolution_seconds == 15 and selected.timeout_seconds == 2
    assert DEFAULT_SOURCE_REQUEST_POLICIES[EvidenceSource.PROMETHEUS].query_resource == "up"
    assert policies[EvidenceSource.LOKI] == DEFAULT_SOURCE_REQUEST_POLICIES[EvidenceSource.LOKI]


@pytest.mark.parametrize("value", [250, 4500])
def test_latency_resource_units_provenance_without_invented_labels(value):
    policy = p.demo_metric_request_policies()[EvidenceSource.PROMETHEUS]
    req = replace(metric_request(), query_resource=policy.query_resource,
        query_semantic_identity=policy.query_semantic_identity, query_resolution_seconds=15)
    observed = []
    payload = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"__name__": "api_p95_latency_ms"},
         "values": [[str(int(req.logical_window.start.timestamp())), str(value)]]}]}}
    def response(*args, **kwargs): observed.append(kwargs["params"]); return payload
    result = PrometheusRangeAdapter("http://synthetic", selector_allowlist=SELECTOR_ALLOWLIST,
                                    http_client=response).collect(req)
    assert result.status is SourceStatus.AVAILABLE
    assert result.records[0].value == value
    assert observed[0]["query"] == 'api_p95_latency_ms{service_name="payments"}'
    assert observed[0]["step"] in (15, 15.0, "15", "15.0")
    assert result.summary.query_provenance == req.query_provenance
    # Fixture is NOT proof that an unlabeled production series matches a label.


@pytest.mark.parametrize("source", ["loki", "prometheus"])
def test_adapter_unavailability_and_empty_are_distinct(source):
    req = log_request() if source == "loki" else metric_request(query_resource="api_p95_latency_ms")
    cls = LokiRangeAdapter if source == "loki" else PrometheusRangeAdapter
    empty = {"status": "success", "data": {"resultType": "streams" if source == "loki" else "matrix", "result": []}}
    result = cls("http://synthetic", selector_allowlist=SELECTOR_ALLOWLIST,
                 http_client=lambda *a, **k: empty).collect(req)
    assert result.status is SourceStatus.EMPTY and not result.records
    def fail(*args, **kwargs): raise requests.ConnectionError("synthetic")
    result = cls("http://synthetic", selector_allowlist=SELECTOR_ALLOWLIST, http_client=fail).collect(req)
    assert result.status is SourceStatus.UNAVAILABLE and not result.records


def test_aggregate_service_and_dependency_are_not_silently_removed():
    from incident_evidence import SelectorFact
    selectors = tuple(SelectorFact(EvidenceSource.LOKI, key, value, value, "selectors-v1")
                      for key, value in (("service_name", "multiple"), ("downstream_service", "core-db")))
    req = replace(log_request(), selectors=selectors)
    adapter = LokiRangeAdapter("http://synthetic", selector_allowlist=SELECTOR_ALLOWLIST)
    assert adapter._query(req) == '{downstream_service="core-db",service_name="multiple"}'
    assert p.selector_preparation_findings(req, frozenset(("service_name", "downstream_service"))) == (
        "AGGREGATE_SERVICE_RULE_APPROVAL_REQUIRED",)
    # This demonstrates the unresolved contract; no aggregate bypass is added.


@pytest.mark.parametrize("shape,expected", [
    ("single", ()),
    ("missing-label", ("SELECTOR_LABELS_NOT_AVAILABLE",)),
    ("missing-dependency", ("AGGREGATE_DEPENDENCY_EVIDENCE_MISSING", "AGGREGATE_SERVICE_RULE_APPROVAL_REQUIRED")),
    ("conflict", ("CONFLICTING_SELECTOR",)),
    ("empty", ("MISSING_BOUNDED_SELECTOR",)),
])
def test_selector_preparation_has_no_implicit_rewrite(shape, expected):
    from incident_evidence import SelectorFact
    req = log_request(); labels = frozenset(("service_name", "downstream_service"))
    if shape == "missing-label": labels = frozenset(("job",))
    elif shape == "missing-dependency":
        req = replace(req, selectors=(replace(req.selectors[0], normalized_value="multiple", escaped_value="multiple"),))
    elif shape == "conflict":
        with pytest.raises(ValueError, match="unique"):
            replace(req, selectors=req.selectors + (SelectorFact(EvidenceSource.LOKI,
                "service_name", "other", "other", "selectors-v1"),))
        return  # Existing SourceAdapterRequest rejects conflict before preparation.
    elif shape == "empty": req = replace(req, selectors=())
    before = req
    assert p.selector_preparation_findings(req, labels) == expected
    assert req == before


def test_metric_multiple_series_is_not_accepted_as_dependency_identity():
    req = metric_request(query_resource="api_p95_latency_ms")
    timestamp = str(int(req.logical_window.start.timestamp()))
    payload = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"instance": label}, "values": [[timestamp, "4500"]]}
        for label in ("a", "b")]}}
    result = PrometheusRangeAdapter("http://synthetic", selector_allowlist=SELECTOR_ALLOWLIST,
                                    http_client=lambda *a, **k: payload).collect(req)
    assert result.status is SourceStatus.AVAILABLE and len(result.records) == 2
    assert all("downstream_service" not in dict(record.labels) for record in result.records)
    assert p.selector_preparation_findings(req, frozenset(("instance",))) == ("SELECTOR_LABELS_NOT_AVAILABLE",)


def test_corrupt_model_metadata_rejected_without_prediction_or_training():
    from src.event_detection.model.predictor import AnomalyPredictor
    from src.event_detection.metrics_iforest import MetricsIForestConfigLoader, MetricsIForestModelLoader, MetricsIForestModelVersionError
    with pytest.raises(TypeError): AnomalyPredictor._validate_model_artifact({})
    cfg = MetricsIForestConfigLoader.load(ROOT / "configs/metrics_iforest.yaml")
    malformed = {key: None for key in ("metadata_version", "metric_name", "feature_names", "trained_at",
        "training_window_count", "model_params", "sklearn_version", "numpy_version", "model")}
    with pytest.raises(MetricsIForestModelVersionError): MetricsIForestModelLoader(cfg)._validate_artifact(malformed)


def test_model_loaders_missing_files_fail_before_deserialization(tmp_path, monkeypatch):
    import joblib
    from src.event_detection.model.predictor import AnomalyPredictor
    from src.event_detection.metrics_iforest import MetricsIForestConfigLoader, MetricsIForestModelLoader, MetricsIForestModelNotFoundError
    def deny(*args, **kwargs): raise AssertionError("unapproved deserialization")
    monkeypatch.setattr(joblib, "load", deny)
    cfg = yaml.safe_load((ROOT / "configs/event_detection.yml").read_text())
    cfg["output"]["model_path"] = str(tmp_path / "missing-log.pkl")
    with pytest.raises(FileNotFoundError): AnomalyPredictor(cfg).load()
    cfg = MetricsIForestConfigLoader.load(ROOT / "configs/metrics_iforest.yaml")
    with pytest.raises(MetricsIForestModelNotFoundError): MetricsIForestModelLoader(cfg).load(tmp_path / "missing-metrics.pkl")


def test_preparation_cli_has_no_execute_or_opt_in_mode(capsys):
    from scripts.prepare_demo_capture import main
    with pytest.raises(SystemExit): main(["--execute"])
    assert main([]) == 2
    output = capsys.readouterr().out
    assert '"execution_allowed": false' in output


def test_custom_metric_policy_preserves_existing_capture_and_public_read_contract(tmp_path):
    from test_incident_evidence_capture import capture_service, capture_command
    from incident_evidence import CaptureTerminalKind
    policies = p.demo_metric_request_policies()
    service, *_ = capture_service(tmp_path, request_policies=policies)
    outcome = service.capture_evidence(capture_command(request_policies=policies))
    assert outcome.terminal_kind is CaptureTerminalKind.SUCCESS
    snapshot = service.resolve_snapshot(outcome.snapshot_id)
    assert snapshot.revision_id == outcome.revision_id
    assert service.resolve_revision(outcome.revision_id) is not None
