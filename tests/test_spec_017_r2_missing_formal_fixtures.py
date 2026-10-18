from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import random

import pytest
import rca_evaluation.formal_fixture_sources as formal_sources

from incident_evidence import (
    EvidenceCompleteness, EvidenceSource, LokiLogRecord, LokiSourceResult,
    SourceStatus,
)
from llm_generation.contracts import FailureClass
from llm_generation.service import ProviderFailureKind, ProviderInvocationError
from rca_evaluation.fixtures import FixtureAdmissionRequest, load_fixture_manifest
from rca_evaluation.formal_fixture_sources import (
    CASE_IDENTITIES, SOURCE_ARTIFACT_COMMITMENT,
    load_formal_fixture_source_artifact,
)
from rca_evaluation.identity import (
    canonical_text_bytes, canonical_text_commitment, commitment,
)
from rca_evaluation.oracle import load_official_oracle
from rca_persistence.contracts import AdmittedRetryDisposition
from scenario_runtime.config import ScenarioConfigLoader
from src.scenario_runtime.schema import ScenarioId
from log_generator.log_generator import LogGenerator
from test_incident_evidence_capture import FakeAdapter, capture_command, capture_service
from test_llm_generation_phase2 import case
from test_llm_generation_service import environment


ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / "configs/rca_evaluation/oracle/spec017_s1_s6_v1.json"
MANIFEST_PATH = ROOT / "configs/rca_evaluation/fixtures/spec017_s1_s6_fixture_manifest_v1.json"
SOURCE_PATH = ROOT / "configs/rca_evaluation/fixtures/spec017_r2_missing_formal_fixtures_v1.json"


def _official():
    oracle = load_official_oracle(ORACLE_PATH)
    sources = load_formal_fixture_source_artifact(SOURCE_PATH, oracle=oracle)
    resolver = load_fixture_manifest(
        MANIFEST_PATH, oracle=oracle, repository_root=ROOT)
    return oracle, sources, resolver


def test_exact_three_sources_bind_oracle_and_pass_strict_fixture_admission():
    oracle, sources, resolver = _official()
    assert tuple(case.fixture_identity for case in sources.cases) == CASE_IDENTITIES
    assert sources.oracle_revision_id == oracle.revision.oracle_revision_id
    assert sources.oracle_content_commitment == oracle.revision.semantic_content_commitment
    for source in sources.cases:
        entry = next(item for item in resolver.entries
                     if item.fixture_identity == source.fixture_identity)
        admitted = resolver.admit(FixtureAdmissionRequest(
            entry.scenario_id, entry.variant, entry.input_identity,
            entry.input_commitment, entry.oracle_scenario_reference,
            fixture_identity=entry.fixture_identity,
            fixture_revision=entry.fixture_revision,
        ))
        assert admitted.fixture_identity == source.fixture_identity
        assert admitted.source_fixture_revision == "v1"
        assert admitted.input_commitment == source.input_commitment
        assert admitted.oracle_revision_id == oracle.revision.oracle_revision_id


@pytest.mark.parametrize("field,value", [
    ("fixture_identity", "spec017:S6:contradiction:v2"),
    ("fixture_revision", "v2"),
    ("input_commitment", "sha256:" + "0" * 64),
    ("oracle_scenario_reference", "spec017-s1-s6-v1:S5"),
])
def test_r2_admission_rejects_wrong_identity_revision_commitment_or_oracle(field, value):
    _, _, resolver = _official()
    entry = next(item for item in resolver.entries
                 if item.fixture_identity == "spec017:S6:contradiction:v1")
    request = {
        "scenario_id": entry.scenario_id,
        "variant": entry.variant,
        "input_identity": entry.input_identity,
        "input_commitment": entry.input_commitment,
        "oracle_scenario_reference": entry.oracle_scenario_reference,
        "fixture_identity": entry.fixture_identity,
        "fixture_revision": entry.fixture_revision,
    }
    request[field] = value
    with pytest.raises(ValueError, match="do not match"):
        resolver.admit(FixtureAdmissionRequest(**request))


def test_r2_admission_requires_identity_and_revision_and_duplicate_is_rejected(tmp_path):
    oracle, _, resolver = _official()
    entry = next(item for item in resolver.entries if item.variant == "degraded")
    with pytest.raises(ValueError, match="exact identity and revision"):
        resolver.admit(FixtureAdmissionRequest(
            entry.scenario_id, entry.variant, entry.input_identity,
            entry.input_commitment, entry.oracle_scenario_reference,
        ))
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    raw["entries"].append(dict(raw["entries"][0]))
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_fixture_manifest(duplicate, oracle=oracle, repository_root=ROOT)


def test_canonical_text_commitment_eol_equivalence_and_text_change(tmp_path):
    raw = SOURCE_PATH.read_bytes()
    assert canonical_text_commitment(raw) == SOURCE_ARTIFACT_COMMITMENT
    crlf = canonical_text_bytes(raw).replace(b"\n", b"\r\n")
    path = tmp_path / "crlf.json"
    path.write_bytes(crlf)
    oracle = load_official_oracle(ORACLE_PATH)
    assert load_formal_fixture_source_artifact(
        path, oracle=oracle).artifact_commitment == SOURCE_ARTIFACT_COMMITMENT
    changed = canonical_text_bytes(raw).replace(b'"included_count": 20',
                                                b'"included_count": 19')
    assert canonical_text_commitment(changed) != SOURCE_ARTIFACT_COMMITMENT
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="commitment mismatch"):
        load_formal_fixture_source_artifact(path, oracle=oracle)
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        load_formal_fixture_source_artifact(path, oracle=oracle)


def test_fixture_sidecar_cannot_rewrite_oracle_answer_authority(tmp_path, monkeypatch):
    raw = canonical_text_bytes(SOURCE_PATH.read_bytes()).replace(
        b'"expected_causal_class": "CREDENTIAL_ABUSE_BRUTE_FORCE"',
        b'"expected_causal_class": "REQUEST_SURGE_RATE_LIMIT"', 1,
    )
    path = tmp_path / "oracle-rewrite.json"
    path.write_bytes(raw)
    monkeypatch.setattr(
        formal_sources, "SOURCE_ARTIFACT_COMMITMENT",
        canonical_text_commitment(raw),
    )
    with pytest.raises(ValueError, match="contradicts the Oracle"):
        load_formal_fixture_source_artifact(
            path, oracle=load_official_oracle(ORACLE_PATH))


def test_production_projection_excludes_all_evaluation_truth():
    _, sources, _ = _official()
    forbidden = {
        "expected_causal_class", "expected_evidence", "forbidden_overclaim",
        "accepted_alternative", "expected_knowledge_behavior", "ground_truth",
        "ground_truth_label", "oracle_predicates", "denominator",
        "expected_disposition", "expected_failure_class",
    }
    for case_source in sources.cases:
        production = case_source.production_payload()
        encoded = json.dumps(production, ensure_ascii=False, sort_keys=True)
        assert not forbidden.intersection(production)
        assert all(name not in encoded for name in forbidden)
        assert commitment(production) == case_source.input_commitment
        assert case_source.evaluation_truth["ground_truth_label"] not in encoded


class _BoundedCompleteLokiAdapter(FakeAdapter):
    def __init__(self, records, condition):
        super().__init__(SourceStatus.AVAILABLE)
        self._records = records
        self._condition = condition

    def collect(self, request):
        base = super().collect(request)
        included = int(self._condition["included_count"])
        records = tuple(
            LokiLogRecord(
                base.records[0].timestamp,
                base.records[0].timestamp_ns + index,
                base.records[0].labels,
                json.dumps(record, separators=(",", ":")),
            )
            for index, record in enumerate(self._records[:included])
        )
        bounds = replace(
            base.summary.bounds,
            observed_candidate_count=int(self._condition["observed_candidate_count"]),
            included_count=included,
            omitted_count=int(self._condition["omitted_count"]),
            omission_reason=self._condition["omission_reason"],
            sampling_applied=True,
            sampling_policy_identity=self._condition["sampling_policy_identity"],
            completeness_impact=EvidenceCompleteness.DEGRADED,
        )
        return LokiSourceResult(
            replace(base.summary, record_count=included, bounds=bounds), records)


def test_s1_degraded_uses_generated_auth_signal_and_candidate_b_typed_degradation(tmp_path):
    _, sources, _ = _official()
    fixture = sources.resolve("spec017:S1:degraded:v1")
    production = fixture.production_payload()
    config = ScenarioConfigLoader.load(ROOT / "configs/scenarios.yaml")
    records = LogGenerator(config).scenario_records(ScenarioId.S1, random.Random(42))
    assert len(records) == production["signal"]["unauthorized_count"] == 50
    assert len({item["source_ip"] for item in records}) == 1
    assert len({item["user_id"] for item in records}) == 1
    condition = production["evidence_adapter_condition"]
    adapters = {
        EvidenceSource.LOKI: _BoundedCompleteLokiAdapter(records, condition),
        EvidenceSource.PROMETHEUS: FakeAdapter(SourceStatus.AVAILABLE),
    }
    service, *_ = capture_service(tmp_path, adapters=adapters)
    outcome = service.capture_evidence(capture_command("SPEC017-S1-DEGRADED"))
    snapshot = service.resolve_snapshot(outcome.snapshot_id)
    assert snapshot.completeness is EvidenceCompleteness.DEGRADED
    bounds = snapshot.snapshot_content["bounds"]["LOKI"]
    assert (bounds["observed_candidate_count"], bounds["included_count"],
            bounds["omitted_count"]) == (50, 20, 30)
    assert fixture.evaluation_truth["denominator"] == "DEGRADED"


def test_s2_failure_uses_candidate_d_provider_seam_and_typed_failure(case, tmp_path):
    _, sources, _ = _official()
    fixture = sources.resolve("spec017:S2:failure:v1")
    injection = fixture.production_input["failure_injection"]
    assert injection == {
        "port": "llm_generation.ports.GenerationProvider",
        "method": "invoke_once", "exception": "ProviderInvocationError",
        "kind": "UNAVAILABLE", "request_sent": True,
    }
    service, fake, grant, resources = environment(
        case, tmp_path,
        ProviderInvocationError(ProviderFailureKind[injection["kind"]]),
    )
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert receipt.failure.retry_safety is AdmittedRetryDisposition.RETRYABLE
    assert receipt.physical_invocations == len(fake.calls) == 1
    assert fixture.evaluation_truth["denominator"] == "FAILURE"
    assert fixture.evaluation_truth["expected_disposition"] == "SYSTEM_FAILURE_RETAINED"
    preregistration = json.loads((
        ROOT / "configs/rca_evaluation/formal_preregistration_v1.json"
    ).read_text(encoding="utf-8"))
    assert preregistration["campaign_controls"]["replacement"] == "NONE"
    assert preregistration["campaign_controls"]["early_stop"] is False


def test_s6_contradiction_is_legitimate_evidence_while_truth_stays_evaluation_side():
    _, sources, _ = _official()
    fixture = sources.resolve("spec017:S6:contradiction:v1")
    production = fixture.production_payload()
    post = production["post_context_evidence"]
    assert post["loki"] == {
        "status_code": 429, "error_type": "RateLimitExceeded",
        "target_service": "sms-gateway", "rate_limit_quota": 100,
        "record_count": 55,
    }
    assert post["prometheus"]["value"] == post["prometheus"]["established_baseline"]
    assert production["baseline_signal"]["spike_requests_per_sec"] > post["prometheus"]["value"]
    encoded = json.dumps(production, sort_keys=True)
    assert "S6_QPS_NORMAL_WHILE_429_PERSISTS" not in encoded
    assert fixture.evaluation_truth["ground_truth_label"] == "S6_QPS_NORMAL_WHILE_429_PERSISTS"
    assert fixture.evaluation_truth["denominator"] == "REFRESH"
    assert fixture.evaluation_truth["expected_disposition"] == (
        "MATERIAL_CONTRADICTION_REQUIRES_REFRESH")
