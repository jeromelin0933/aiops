"""Single-host prototype controls use only synthetic facts and temporary SQLite."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import sqlite3
import subprocess
from threading import Barrier
from types import SimpleNamespace

import pytest

from knowledge_index.gemini_diagnostic_controls import (
    ApprovedDiagnosticInput, DiagnosticAuthorizationRecord, DiagnosticDenied,
    DiagnosticExecutionFacts, FixedHostPaths, LiveDiagnosticUsage, LiveHostBinding,
    OfflineAuthorizationSource, TemporaryDiagnosticUsage, input_commitment,
    load_live_binding, resolve_repository_source, validate_authorization,
    WindowsPathFacts,
)
import knowledge_index.gemini_diagnostic_controls as controls


REVISION = "b" * 40
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def record(*, kind="query", commitment=None, **changes):
    values = dict(
        campaign_id="synthetic-campaign-1", pm_approval_reference="synthetic-pm-campaign-1",
        spec_lead_identity="synthetic-lead", runner_identity="synthetic-runner",
        source_revision=REVISION, provider_identity="google", model_identity="gemini-embedding-2",
        credential_profile_identity="synthetic-credential-profile", purpose="CONTROLLED_PROVIDER_DIAGNOSTIC",
        input_kind=kind, input_scope_identity="synthetic-scope",
        input_commitment=commitment or input_commitment("query", ("build-1",), ("task: search result | query: sample",)),
        maximum_invocations=2, maximum_items=4, maximum_item_tokens=1000,
        maximum_aggregate_tokens=4000, maximum_item_bytes=2000,
        maximum_payload_bytes=10000, maximum_usage_units=4000,
        maximum_cost_micro_units=100000, bounds_basis="SYNTHETIC_OFFLINE_ONLY",
        cost_basis="SYNTHETIC_OFFLINE_ONLY", cost_currency="USD",
        valid_from="2026-10-23T11:00:00+00:00", expires_at="2026-10-23T13:00:00+00:00",
        timeout_seconds=30, attempts=1, authorized_host="synthetic-host",
        single_active_runner=True, api_version="v1", sdk_baseline="google-genai==2.25.0",
        normalization_tolerance=0.01,
    )
    values.update(changes)
    return DiagnosticAuthorizationRecord(**values)


def facts(**changes):
    values = dict(
        source_revision=REVISION, clean_tree=True,
        pm_approval_reference="synthetic-pm-campaign-1", spec_lead_identity="synthetic-lead",
        runner_identity="synthetic-runner",
        host_identity="synthetic-host", credential_profile_identity="synthetic-credential-profile", now=NOW,
    )
    values.update(changes)
    return DiagnosticExecutionFacts(**values)


def e4_host_material(grant, approved):
    semantic = {"requests": [{
        "model": "models/gemini-embedding-2",
        "content": {"role": "user", "parts": [{"text": text}]},
        "outputDimensionality": 768,
    } for text in approved.envelopes]}
    canonical = json.dumps(semantic, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":")).encode()
    return dict(
        schema_version=1, artifact_type="SDK_REQUEST_BYTE_EVIDENCE",
        status="HOST_CONTROLLED_E4_ONLY", method="PINNED_SDK_HTTPX_REQUEST_BODY",
        reference="host-admin-e4-001", valid_from="2000-01-01T00:00:00+00:00",
        valid_until="2099-01-01T00:00:00+00:00", campaign_id=grant.campaign_id,
        pm_approval_reference=grant.pm_approval_reference,
        source_revision=grant.source_revision,
        authorization_record_sha256=controls.authorization_record_digest(grant),
        provider=grant.provider_identity, model=grant.model_identity,
        sdk=grant.sdk_baseline, api=grant.api_version,
        kind=approved.kind, input_scope_identity=approved.scope_identity,
        ordered_identities=list(approved.identities),
        input_commitment=approved.commitment,
        canonical_json_sha256=sha256(canonical).hexdigest(),
        sdk_body_sha256="a" * 64, sdk_body_bytes=256,
        http_method="POST", request_url=(
            "https://generativelanguage.googleapis.com/v1/models/"
            "gemini-embedding-2:batchEmbedContents"),
        output_dimensionality=768, timeout_ms=30_000, attempts=1,
    )


def e4_query_case():
    grant = record(input_scope_identity="build:build-1", runner_identity="S-1-5-21-100")
    envelope = "task: search result | query: sample"
    material = dict(
        schema_version=1, campaign_id=grant.campaign_id,
        source_revision=grant.source_revision, purpose=grant.purpose,
        kind="query", scope_identity=grant.input_scope_identity,
        input_commitment=grant.input_commitment,
        ordered_items=[dict(frozen_build_identity="build-1",
                            canonical_query="sample", envelope=envelope)],
    )
    approved = ApprovedDiagnosticInput.from_json(json.dumps(material).encode())
    return grant, material, approved


def e4_host_fixture(tmp_path, monkeypatch):
    grant, input_material, approved = e4_query_case()
    monkeypatch.setattr(controls, "_programdata_known_folder", lambda: tmp_path)
    paths = controls.fixed_host_paths()
    paths.authority.mkdir(parents=True)
    paths.state.mkdir()
    paths.campaign.write_text(json.dumps(asdict(grant)), encoding="utf-8")
    paths.approved_input.write_text(json.dumps(input_material), encoding="utf-8")
    paths.sdk_request_evidence.write_text(
        json.dumps(e4_host_material(grant, approved)), encoding="utf-8")
    paths.attempts.write_bytes(b"isolated-existing-store-placeholder")
    monkeypatch.setattr(controls, "process_runner_sid", lambda: "S-1-5-21-100")
    monkeypatch.setattr(controls, "_effective_token", _fake_token)

    def host_facts(path, token=None):
        try:
            info = path.lstat()
        except OSError as exc:
            raise DiagnosticDenied("diagnostic host path is unavailable") from exc
        directory = path.is_dir()
        runner = "S-1-5-21-100"
        aces = (("S-1-5-32-544", 0x10000000, 0),
                (runner, 0x00120117 if path in (paths.state, paths.attempts)
                 else 0x00120089, 0))
        rights = 0x3 if path in (paths.state, paths.attempts) else 0x1
        return WindowsPathFacts("S-1-5-32-544", aces, (info.st_dev, info.st_ino),
                                directory, False, rights)

    monkeypatch.setattr(controls, "_windows_path_facts", host_facts)
    monkeypatch.setattr(controls, "_check_existing_usage", lambda path, observed: None)
    return paths, grant, approved, host_facts


def validate(item, actual=None, **changes):
    args = dict(
        live_opt_in=True, kind="query", input_scope_identity=item.input_scope_identity,
        commitment=item.input_commitment,
        item_count=1, item_bytes=(48,), item_tokens=(48,), payload_bytes=190,
        safety_basis="SYNTHETIC_OFFLINE_ONLY", conservative_usage_units=48,
        conservative_cost_micro_units=100,
    )
    args.update(changes)
    return validate_authorization(item, actual or facts(), **args)


@pytest.mark.parametrize("field", tuple(DiagnosticAuthorizationRecord.__dataclass_fields__))
def test_every_mandatory_record_field_is_required(field):
    values = asdict(record())
    del values[field]
    with pytest.raises(DiagnosticDenied, match="incomplete"):
        DiagnosticAuthorizationRecord.from_json(__import__("json").dumps(values).encode())


@pytest.mark.parametrize("field,value", [
    ("pm_approval_reference", "PM-RAG-EMBEDDING-MIGRATION-20261022"),
    ("provider_identity", "other"), ("model_identity", "text-embedding-004"),
    ("purpose", "PRODUCTION"), ("api_version", "v1beta"),
    ("sdk_baseline", "google-genai==0.0.0"), ("timeout_seconds", 31),
    ("attempts", 2), ("single_active_runner", False),
    ("maximum_cost_micro_units", 0), ("bounds_basis", ""),
    ("maximum_cost_micro_units", 2**63),
    ("credential_profile_identity", "api_key=secret"),
])
def test_invalid_record_binding_fails_closed(field, value):
    with pytest.raises(DiagnosticDenied):
        record(**{field: value})


@pytest.mark.parametrize("actual,kwargs", [
    (facts(clean_tree=False), {}), (facts(source_revision="a" * 40), {}),
    (facts(host_identity="other"), {}), (facts(runner_identity="other"), {}),
    (facts(pm_approval_reference="other"), {}), (facts(spec_lead_identity="other"), {}),
    (facts(credential_profile_identity="other"), {}),
    (facts(now=datetime(2026, 10, 23, 14, tzinfo=timezone.utc)), {}),
    (facts(), {"live_opt_in": False}), (facts(), {"commitment": "other"}),
    (facts(), {"input_scope_identity": "other"}),
    (facts(), {"safety_basis": ""}), (facts(), {"item_count": 5}),
    (facts(), {"payload_bytes": 10001}), (facts(), {"conservative_usage_units": 4001}),
    (facts(), {"conservative_cost_micro_units": 100001}),
])
def test_source_opt_in_scope_and_bounds_fail_before_transport(actual, kwargs):
    with pytest.raises(DiagnosticDenied):
        validate(record(), actual, **kwargs)


def test_temporary_usage_rejects_duplicate_restart_unknown_and_exhaustion(tmp_path):
    path = tmp_path / "isolated-attempts.sqlite"
    grant = record(maximum_invocations=1)
    first = TemporaryDiagnosticUsage(path)
    first.reserve(grant, "request-1", items=1, usage_units=100, cost_micro_units=100)
    restarted = TemporaryDiagnosticUsage(path)
    with pytest.raises(DiagnosticDenied):
        restarted.reserve(grant, "request-1", items=1, usage_units=100, cost_micro_units=100)
    with pytest.raises(DiagnosticDenied):
        restarted.reserve(grant, "request-2", items=1, usage_units=100, cost_micro_units=100)
    first.finish(grant, "request-1", "UNKNOWN")
    with pytest.raises(DiagnosticDenied):
        restarted.reserve(grant, "request-2", items=1, usage_units=100, cost_micro_units=100)


def test_two_runners_compete_for_single_execution_lock(tmp_path):
    path = tmp_path / "isolated-concurrent.sqlite"
    grant = record()
    TemporaryDiagnosticUsage(path)
    barrier = Barrier(2)

    def attempt(index):
        ledger = TemporaryDiagnosticUsage(path)
        barrier.wait()
        try:
            ledger.reserve(grant, f"request-{index}", items=1, usage_units=100, cost_micro_units=100)
            return "reserved"
        except DiagnosticDenied:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (1, 2)))
    assert sorted(results) == ["denied", "reserved"]
    assert len(ledger_rows(path)) == 1
    assert ledger_rows(path)[0][1:] == ("IN_FLIGHT", 1, 100, 100, "UNKNOWN")


def test_unavailable_usage_record_fails_closed(tmp_path):
    with pytest.raises(DiagnosticDenied, match="unavailable"):
        TemporaryDiagnosticUsage(tmp_path / "missing-directory" / "attempts.sqlite")


def test_offline_source_reads_exact_record_and_rejects_missing_or_malformed(tmp_path):
    path = tmp_path / "controlled-fixture.json"
    source = OfflineAuthorizationSource(path)
    with pytest.raises(DiagnosticDenied, match="missing"):
        source.load()
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(DiagnosticDenied, match="incomplete"):
        source.load()
    path.write_text(json.dumps(asdict(record())), encoding="utf-8")
    assert source.load() == record()


def test_fixed_known_folder_and_missing_host_fail_closed(tmp_path, monkeypatch):
    known_folder = controls._programdata_known_folder
    if os.name == "nt":
        actual = controls._programdata_known_folder()
        assert actual.is_absolute()
        monkeypatch.setenv("ProgramData", str(tmp_path))
        assert controls._programdata_known_folder() == actual
        assert controls.process_runner_sid().startswith("S-1-")
        assert controls._windows_path_facts(actual).is_directory
    monkeypatch.setattr(controls, "_programdata_known_folder", lambda: tmp_path)
    paths = controls.fixed_host_paths()
    assert paths.campaign == tmp_path / "AIOps/Diagnostic/authority/campaign.json"
    assert paths.approved_input == tmp_path / "AIOps/Diagnostic/authority/approved_input.json"
    assert paths.attempts == tmp_path / "AIOps/Diagnostic/state/attempts.sqlite"
    monkeypatch.setattr(controls, "process_runner_sid", lambda: "S-1-5-21-100")
    with pytest.raises(DiagnosticDenied):
        load_live_binding()
    with pytest.raises(TypeError):
        controls.fixed_host_paths(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(controls, "_programdata_known_folder", known_folder)
        patch.setattr(controls, "os", SimpleNamespace(name="posix"))
        with pytest.raises(DiagnosticDenied, match="Windows"):
            controls._programdata_known_folder()


def _fake_host_facts(path, *, runner="S-1-5-21-100", unsafe=None):
    directory = path.suffix not in (".json", ".sqlite")
    aces = (("S-1-5-32-544", 0x10000000, 0), (runner, 0x00120089, 0))
    if path.name in ("state", "attempts.sqlite"):
        aces = (("S-1-5-32-544", 0x10000000, 0), (runner, 0x00120117, 0))
    if unsafe == "ordinary":
        aces += (("S-1-5-32-545", 0x00000002, 0),)
    if unsafe == "runner_write":
        aces += ((runner, 0x00000002, 0),)
    rights = 0x3 if path.name in ("state", "attempts.sqlite") else 0x1
    if unsafe == "runner_write":
        rights |= 0x2
    return WindowsPathFacts("S-1-5-32-544", aces, (1, abs(hash(path)) or 1),
                            directory, unsafe == "reparse", rights)


@contextmanager
def _fake_token(runner="S-1-5-21-100"):
    yield 1, controls.WindowsTokenFacts(runner, (("S-1-5-32-545", 7),),
                                        (), ("SeChangeNotifyPrivilege",), False)


@pytest.mark.parametrize("target,unsafe", [
    ("root", "ordinary"), ("authority", "runner_write"),
    ("campaign", "ordinary"), ("approved_input", "reparse"),
    ("state", "reparse"), ("attempts", "ordinary"),
])
def test_host_acl_owner_parent_and_reparse_policy_fail_closed(tmp_path, monkeypatch, target, unsafe):
    paths = FixedHostPaths(
        tmp_path / "AIOps/Diagnostic", tmp_path / "AIOps/Diagnostic/authority",
        tmp_path / "AIOps/Diagnostic/authority/campaign.json",
        tmp_path / "AIOps/Diagnostic/authority/approved_input.json",
        tmp_path / "AIOps/Diagnostic/state",
        tmp_path / "AIOps/Diagnostic/state/attempts.sqlite",
    )
    runner = "S-1-5-21-100"
    monkeypatch.setattr(controls, "_effective_token", _fake_token)
    monkeypatch.setattr(controls, "_windows_path_facts", lambda path, token=None: _fake_host_facts(
        path, runner=runner, unsafe=unsafe if path == getattr(paths, target) else None,
    ))
    with pytest.raises(DiagnosticDenied):
        controls.validate_host_paths(paths, runner)


def test_host_policy_rejects_wrong_sid_owner_and_path_shape(tmp_path, monkeypatch):
    paths = controls.FixedHostPaths(
        tmp_path / "AIOps/Diagnostic", tmp_path / "AIOps/Diagnostic/authority",
        tmp_path / "AIOps/Diagnostic/authority/campaign.json",
        tmp_path / "AIOps/Diagnostic/authority/approved_input.json",
        tmp_path / "AIOps/Diagnostic/state",
        tmp_path / "AIOps/Diagnostic/state/attempts.sqlite",
    )
    runner = "S-1-5-21-100"
    monkeypatch.setattr(controls, "_effective_token", _fake_token)
    monkeypatch.setattr(controls, "_windows_path_facts", lambda path, token=None: _fake_host_facts(path))
    assert len(controls.validate_host_paths(paths, runner)) == 10
    with pytest.raises(DiagnosticDenied, match="fixed"):
        controls.validate_host_paths(replace(paths, attempts=tmp_path / "other.sqlite"), runner)
    monkeypatch.setattr(controls, "_windows_path_facts", lambda path, token=None: controls.WindowsPathFacts(
        "S-1-5-21-999", _fake_host_facts(path).allowed_aces,
        (1, 2), path.suffix not in (".json", ".sqlite"), False, 0x1,
    ))
    with pytest.raises(DiagnosticDenied):
        controls.validate_host_paths(paths, runner)


def test_e4_strict_artifact_parser_and_cross_binding():
    grant, _, approved = e4_query_case()
    material = e4_host_material(grant, approved)
    evidence = controls.DiagnosticSdkByteEvidence.from_json(json.dumps(material).encode())
    evidence.matches_record_input(grant, approved)
    assert evidence.authorization_record_sha256 == controls.authorization_record_digest(grant)
    assert evidence.ordered_identities == approved.identities
    assert evidence.status == "HOST_CONTROLLED_E4_ONLY"


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"artifact_type": "OTHER"}, {"status": "SYNTHETIC_OFFLINE_ONLY"},
    {"method": "local-json-mirror"}, {"reference": "api_key=secret"},
    {"valid_from": "2099-01-01T00:00:00+00:00"},
    {"valid_until": "2000-01-01T00:00:00+00:00"},
    {"valid_until": "2099-01-01T00:00:00"},
    {"authorization_record_sha256": "z" * 64},
    {"ordered_identities": []}, {"ordered_identities": ["build-1", "build-1"]},
    {"canonical_json_sha256": None}, {"sdk_body_sha256": "0"},
    {"sdk_body_bytes": 0}, {"sdk_body_bytes": True},
    {"provider": "other"}, {"model": "text-embedding-004"},
    {"sdk": "google-genai==0"}, {"api": "v1beta"},
    {"kind": "other"}, {"http_method": "GET"}, {"request_url": "https://example.invalid"},
    {"output_dimensionality": 1536}, {"timeout_ms": 31_000}, {"attempts": 2},
])
def test_e4_strict_artifact_rejects_invalid_fields(change):
    grant, _, approved = e4_query_case()
    material = e4_host_material(grant, approved) | change
    with pytest.raises(DiagnosticDenied):
        controls.DiagnosticSdkByteEvidence.from_json(json.dumps(material).encode())


def test_e4_strict_artifact_rejects_missing_extra_duplicate_and_malformed():
    grant, _, approved = e4_query_case()
    material = e4_host_material(grant, approved)
    for payload in (
        {key: value for key, value in material.items() if key != "sdk_body_sha256"},
        material | {"api_key": "secret"},
    ):
        with pytest.raises(DiagnosticDenied):
            controls.DiagnosticSdkByteEvidence.from_json(json.dumps(payload).encode())
    duplicate = json.dumps(material).replace('"schema_version": 1',
        '"schema_version": 1, "schema_version": 1', 1)
    with pytest.raises(DiagnosticDenied, match="duplicate"):
        controls.DiagnosticSdkByteEvidence.from_json(duplicate.encode())
    with pytest.raises(DiagnosticDenied):
        controls.DiagnosticSdkByteEvidence.from_json(b"{malformed")


def test_e4_deep_json_decoder_recursion_is_typed_secret_safe_denial():
    deeply_nested = ("[" * 1100 + "0" + "]" * 1100).encode()
    with pytest.raises(DiagnosticDenied) as caught:
        controls.DiagnosticSdkByteEvidence.from_json(deeply_nested)
    assert str(caught.value) == "SDK byte evidence is malformed"
    assert isinstance(caught.value.__cause__, RecursionError)


@pytest.mark.parametrize("change", [
    {"campaign_id": "other"}, {"pm_approval_reference": "other"},
    {"source_revision": "a" * 40}, {"authorization_record_sha256": "0" * 64},
    {"input_scope_identity": "other"}, {"input_commitment": "0" * 64},
    {"ordered_identities": ["different"]}, {"sdk_body_bytes": 10_001},
])
def test_e4_artifact_rejects_record_or_approved_input_mismatch(change):
    grant, _, approved = e4_query_case()
    material = e4_host_material(grant, approved) | change
    evidence = controls.DiagnosticSdkByteEvidence.from_json(json.dumps(material).encode())
    with pytest.raises(DiagnosticDenied, match="does not match"):
        evidence.matches_record_input(grant, approved)


def test_e4_fixed_host_loader_reads_existing_protected_artifact(tmp_path, monkeypatch):
    paths, grant, approved, _ = e4_host_fixture(tmp_path, monkeypatch)
    assert paths.sdk_request_evidence == paths.authority / "sdk_request_evidence.json"
    assert len(controls.validate_host_paths(paths, "S-1-5-21-100")) == 10
    binding = controls.load_live_binding()
    assert binding.record == grant and binding.approved_input == approved
    assert binding.sdk_byte_evidence is not None
    binding.sdk_byte_evidence.matches_record_input(grant, approved)
    with pytest.raises(TypeError):
        controls.fixed_host_paths(tmp_path)


@pytest.mark.parametrize("change", [
    {"campaign_id": "other"}, {"pm_approval_reference": "other"},
    {"source_revision": "a" * 40},
    {"authorization_record_sha256": "0" * 64},
    {"input_scope_identity": "other"}, {"input_commitment": "0" * 64},
    {"ordered_identities": ["other"]},
    {"sdk_body_sha256": "invalid"}, {"sdk_body_bytes": 0},
    {"valid_until": "2000-01-01T00:00:00+00:00"},
])
def test_e4_fixed_host_loader_rejects_invalid_evidence_payload(
    tmp_path, monkeypatch, change,
):
    paths, grant, approved, _ = e4_host_fixture(tmp_path, monkeypatch)
    paths.sdk_request_evidence.write_text(
        json.dumps(e4_host_material(grant, approved) | change), encoding="utf-8")
    with pytest.raises(DiagnosticDenied):
        controls.load_live_binding()


@pytest.mark.parametrize("unsafe", [
    "missing", "ordinary", "runner_write", "owner", "reparse", "identity",
    "parent_write", "caller_path",
])
def test_e4_fixed_host_loader_rejects_untrusted_source(tmp_path, monkeypatch, unsafe):
    paths, _, _, original_facts = e4_host_fixture(tmp_path, monkeypatch)
    if unsafe == "missing":
        paths.sdk_request_evidence.unlink()
    if unsafe == "caller_path":
        other = tmp_path / "caller-evidence.json"
        other.write_bytes(paths.sdk_request_evidence.read_bytes())
        with pytest.raises(TypeError):
            controls.load_live_binding(evidence_path=other)
        return

    def altered(path, token=None):
        facts = original_facts(path, token)
        if path == paths.sdk_request_evidence:
            if unsafe == "ordinary":
                return replace(facts, allowed_aces=facts.allowed_aces
                               + (("S-1-5-32-545", 0x2, 0),))
            if unsafe == "runner_write":
                return replace(facts, effective_rights=facts.effective_rights | 0x2)
            if unsafe == "owner":
                return replace(facts, owner_sid="S-1-5-21-999")
            if unsafe == "reparse":
                return replace(facts, is_reparse=True)
            if unsafe == "identity":
                return replace(facts, identity=(facts.identity[0], facts.identity[1] + 1))
        if path == paths.authority and unsafe == "parent_write":
            return replace(facts, effective_rights=facts.effective_rights | 0x40)
        return facts

    monkeypatch.setattr(controls, "_windows_path_facts", altered)
    with pytest.raises(DiagnosticDenied):
        controls.load_live_binding()


@pytest.mark.parametrize("changes", [
    {"groups": (("S-1-5-32-544", 7),)},
    {"groups": (("S-1-5-32-544", 0x10),)},
    {"groups": (("S-1-5-32-545", 0x10),)},
    {"restricted_sids": ("S-1-1-0",)},
    {"privileges": ("SeRestorePrivilege",)},
    {"privileges": ("SeUnknownPrivilege",)},
    {"groups": (("S-1-5-32-545", 0x10000000),)},
    {"elevated": True},
])
def test_effective_token_rejects_admin_restriction_and_privilege(changes):
    safe = controls.WindowsTokenFacts("S-1-5-21-100", (("S-1-5-32-545", 7),),
                                     (), ("SeChangeNotifyPrivilege",), False)
    with pytest.raises(DiagnosticDenied):
        controls._validate_token_facts(replace(safe, **changes), safe.user_sid)


def test_effective_rights_override_allow_ace_scan(tmp_path):
    path = tmp_path / "campaign.json"
    base = WindowsPathFacts("S-1-5-32-544",
                            (("S-1-5-21-100", 0x00120089, 0),),
                            (1, 2), False, False, 0)
    with pytest.raises(DiagnosticDenied, match="required host access"):
        controls._validate_path_facts(path, base, "S-1-5-21-100", directory=False)
    with pytest.raises(DiagnosticDenied, match="can alter"):
        controls._validate_path_facts(path, replace(base, effective_rights=0x3),
                                      "S-1-5-21-100", directory=False)
    with pytest.raises(DiagnosticDenied, match="generic rights"):
        controls._validate_path_facts(path, replace(
            base, allowed_aces=(("S-1-5-21-100", 0x80000001, 0),),
            effective_rights=0x1), "S-1-5-21-100", directory=False)


def test_protected_directories_reject_effective_file_delete_child(tmp_path):
    runner = "S-1-5-21-100"
    read_only = WindowsPathFacts("S-1-5-32-544",
                                 ((runner, 0x00120089, 0),),
                                 (1, 2), True, False, 0x1)
    for name, rights in (("authority", 0x41), ("authority", 0x40),
                         ("AIOps", 0x41)):
        with pytest.raises(DiagnosticDenied, match="can alter"):
            controls._validate_path_facts(tmp_path / name,
                                          replace(read_only, effective_rights=rights),
                                          runner, directory=True)
    controls._validate_path_facts(tmp_path / "authority", read_only,
                                  runner, directory=True)
    state = replace(read_only, effective_rights=0x3)
    controls._validate_path_facts(tmp_path / "state", state, runner,
                                  directory=True, writable_state=True)
    with pytest.raises(DiagnosticDenied, match="can alter"):
        controls._validate_path_facts(tmp_path / "state",
                                      replace(state, effective_rights=0x43), runner,
                                      directory=True, writable_state=True)


def test_actual_windows_thread_impersonation_is_denied():
    if os.name != "nt":
        pytest.skip("Windows-only API smoke test")
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.ImpersonateSelf.argtypes = [wintypes.DWORD]
    advapi.ImpersonateSelf.restype = wintypes.BOOL
    advapi.RevertToSelf.restype = wintypes.BOOL
    if not advapi.ImpersonateSelf(2):
        pytest.skip("host does not permit self impersonation")
    try:
        with pytest.raises(DiagnosticDenied, match="thread impersonation"):
            controls.process_runner_sid()
    finally:
        assert advapi.RevertToSelf()


def test_actual_windows_accesscheck_honors_deny_ace_and_generic_mapping():
    if os.name != "nt":
        pytest.skip("Windows-only AccessCheck smoke test")
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]

    def rights(sddl):
        descriptor = ctypes.c_void_p()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None,
        ):
            pytest.fail("Windows test SDDL could not be constructed")
        try:
            with controls._effective_token() as (token, _):
                return controls._access_check(descriptor.value, token)
        finally:
            kernel.LocalFree(descriptor)

    allowed = rights("O:SYG:SYD:(A;;0x0012019F;;;WD)")
    denied = rights("O:SYG:SYD:(D;;0x00000002;;;WD)(A;;0x0012019F;;;WD)")
    delete_child = rights("O:SYG:SYD:(A;;0x00000041;;;WD)")
    generic_unmapped = rights("O:SYG:SYD:(A;;GRGW;;;WD)")
    assert allowed & 0x1 and allowed & 0x2
    assert denied & 0x1 and not denied & 0x2
    assert delete_child & 0x1 and delete_child & 0x40
    assert generic_unmapped == 0  # Unsupported ACE form cannot grant required rights.
    with pytest.raises(DiagnosticDenied, match="effective access"):
        rights("O:SYD:(A;;0x0012019F;;;WD)")  # Missing Group SID.


def test_trusted_open_detects_replacement(tmp_path):
    path = tmp_path / "record.json"
    path.write_text("{}", encoding="utf-8")
    info = path.stat()
    facts = WindowsPathFacts("S-1-5-32-544", (), (info.st_dev, info.st_ino + 1), False, False)
    with pytest.raises(DiagnosticDenied, match="changed"):
        controls._read_trusted_file(path, facts)


def test_runner_requires_read_access_and_state_journal_access(tmp_path):
    campaign = tmp_path / "campaign.json"
    state = tmp_path / "state"
    for path, mask, writable in ((campaign, 0x00020000, False),
                                 (state, 0x00000001, True)):
        facts = WindowsPathFacts("S-1-5-32-544",
                                 (("S-1-5-21-100", mask, 0),), (1, 2),
                                 writable, False)
        with pytest.raises(DiagnosticDenied, match="required host access"):
            controls._validate_path_facts(path, facts, "S-1-5-21-100",
                                          directory=writable, writable_state=writable)


def test_authorization_and_input_reject_duplicate_json_keys():
    with pytest.raises(DiagnosticDenied, match="duplicate"):
        DiagnosticAuthorizationRecord.from_json(b'{"campaign_id":"a","campaign_id":"b"}')
    with pytest.raises(DiagnosticDenied, match="duplicate"):
        ApprovedDiagnosticInput.from_json(b'{"kind":"query","kind":"document"}')


def test_query_artifact_binding_and_ordered_envelope_validation():
    item = {"frozen_build_identity": "build-1", "canonical_query": "sample",
            "envelope": "task: search result | query: sample"}
    values = {"schema_version": 1, "campaign_id": "synthetic-campaign-1",
              "source_revision": REVISION, "purpose": "CONTROLLED_PROVIDER_DIAGNOSTIC",
              "kind": "query", "scope_identity": "build:build-1",
              "input_commitment": input_commitment("query", ("build-1",), (item["envelope"],)),
              "ordered_items": [item]}
    approved = ApprovedDiagnosticInput.from_json(json.dumps(values).encode())
    approved.matches_record(record(input_scope_identity="build:build-1"))
    for changed in (
        {**values, "campaign_id": "other"},
        {**values, "ordered_items": [{**item, "envelope": "changed"}]},
        {**values, "ordered_items": [{**item, "api_key": "secret"}]},
    ):
        with pytest.raises(DiagnosticDenied):
            altered = ApprovedDiagnosticInput.from_json(json.dumps(changed).encode())
            altered.matches_record(record(input_scope_identity="build:build-1"))


def test_existing_formal_sqlite_is_read_only_and_missing_fails(tmp_path, monkeypatch):
    path = tmp_path / "attempts.sqlite"
    info = None
    with pytest.raises(DiagnosticDenied, match="unavailable"):
        controls._check_existing_usage(path, WindowsPathFacts("S-1-5-32-544", (), (1, 1), False, False))
    TemporaryDiagnosticUsage(path)
    info = path.stat()
    facts_for_db = WindowsPathFacts("S-1-5-32-544", (), (info.st_dev, info.st_ino), False, False)
    controls._check_existing_usage(path, facts_for_db)
    grant = record(maximum_invocations=1)
    paths = controls.FixedHostPaths(tmp_path, tmp_path, tmp_path, tmp_path, tmp_path, path)
    fake_binding = LiveHostBinding(grant, None, paths, "S-1-5-21-100")
    monkeypatch.setattr(controls, "load_live_binding", lambda: fake_binding)
    monkeypatch.setattr(controls, "_windows_path_facts", lambda _: facts_for_db)
    ledger = LiveDiagnosticUsage()
    ledger.reserve(grant, "request-1", items=1, usage_units=100, cost_micro_units=100)
    with pytest.raises(DiagnosticDenied):
        LiveDiagnosticUsage().reserve(grant, "request-2", items=1, usage_units=100,
                                      cost_micro_units=100)
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_formal_usage_rejects_schema_without_unique_request_identity(tmp_path):
    path = tmp_path / "attempts.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE attempts (campaign_id TEXT, request_id TEXT, outcome TEXT, "
                   "items INTEGER, usage_reserved INTEGER, cost_reserved INTEGER, "
                   "usage_observed TEXT)")
    info = path.stat()
    facts = WindowsPathFacts("S-1-5-32-544", (), (info.st_dev, info.st_ino), False, False)
    with pytest.raises(DiagnosticDenied, match="schema"):
        controls._check_existing_usage(path, facts)


def test_windows_guard_pins_existing_sqlite_path_during_connection(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-only namespace guard")
    path = tmp_path / "attempts.sqlite"
    TemporaryDiagnosticUsage(path)
    info = path.stat()
    with controls._guard_db_path(path, (info.st_dev, info.st_ino)):
        with sqlite3.connect(controls._sqlite_uri(path, read_only=False), uri=True) as db:
            assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            db.execute("INSERT INTO attempts (campaign_id,request_id,outcome,items,"
                       "usage_reserved,cost_reserved) VALUES ('c','r','IN_FLIGHT',1,1,1)")
    assert path.exists()
    # A trusted runner may itself have raw rename authority. If its rename
    # succeeds, the guard must detect the changed path before eligibility.
    try:
        with controls._guard_db_path(path, (info.st_dev, info.st_ino)):
            path.rename(tmp_path / "replacement.sqlite")
    except OSError:
        assert path.exists()
    except DiagnosticDenied as exc:
        if path.exists():
            assert "unavailable" in str(exc)
        else:
            assert "changed" in str(exc)
    else:
        pytest.fail("a replaced formal ledger path went undetected")


@pytest.mark.parametrize("extra_sql", [
    "CREATE TRIGGER erase AFTER INSERT ON attempts BEGIN DELETE FROM attempts; END",
    "CREATE VIEW hidden AS SELECT * FROM attempts",
    "CREATE INDEX extra_idx ON attempts(outcome)",
])
def test_formal_usage_rejects_unexpected_sqlite_objects(tmp_path, extra_sql):
    path = tmp_path / "attempts.sqlite"
    TemporaryDiagnosticUsage(path)
    with sqlite3.connect(path) as db:
        db.execute(extra_sql)
    info = path.stat()
    facts = WindowsPathFacts("S-1-5-32-544", (), (info.st_dev, info.st_ino), False, False)
    with pytest.raises(DiagnosticDenied, match="schema"):
        controls._check_existing_usage(path, facts)


@pytest.mark.parametrize("column", ["items", "usage_reserved", "cost_reserved"])
def test_formal_usage_rejects_negative_existing_budget(tmp_path, column):
    path = tmp_path / "attempts.sqlite"
    TemporaryDiagnosticUsage(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO attempts (campaign_id,request_id,outcome,items,"
                   "usage_reserved,cost_reserved) VALUES ('c','r','SUCCESS',1,1,1)")
        db.execute(f"UPDATE attempts SET {column}=-1")
    info = path.stat()
    facts = WindowsPathFacts("S-1-5-32-544", (), (info.st_dev, info.st_ino), False, False)
    with pytest.raises(DiagnosticDenied):
        controls._check_existing_usage(path, facts)


def test_formal_reservation_rechecks_schema_under_write_lock(tmp_path, monkeypatch):
    path = tmp_path / "attempts.sqlite"
    TemporaryDiagnosticUsage(path)
    info = path.stat()
    facts = WindowsPathFacts("S-1-5-32-544", (), (info.st_dev, info.st_ino), False, False)
    binding = LiveHostBinding(record(), None,
        FixedHostPaths(tmp_path, tmp_path, tmp_path, tmp_path, tmp_path, path),
        "S-1-5-21-100")
    monkeypatch.setattr(controls, "load_live_binding", lambda: binding)
    monkeypatch.setattr(controls, "_windows_path_facts", lambda _: facts)
    usage = LiveDiagnosticUsage()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER erase AFTER INSERT ON attempts BEGIN DELETE FROM attempts; END")
    with pytest.raises(DiagnosticDenied, match="schema"):
        usage.reserve(binding.record, "request-1", items=1,
                      usage_units=100, cost_micro_units=100)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)


def test_formal_usage_rejects_invalid_observed_usage_and_integer_overflow(tmp_path, monkeypatch):
    path = tmp_path / "attempts.sqlite"
    TemporaryDiagnosticUsage(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO attempts (campaign_id,request_id,outcome,items,"
                   "usage_reserved,cost_reserved,usage_observed) "
                   "VALUES ('c','r1','SUCCESS',1,1,1,'NaN')")
    info = path.stat()
    facts = WindowsPathFacts("S-1-5-32-544", (), (info.st_dev, info.st_ino), False, False)
    with pytest.raises(DiagnosticDenied):
        controls._check_existing_usage(path, facts)
    grant = record()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM attempts")
        db.executemany("INSERT INTO attempts (campaign_id,request_id,outcome,items,"
                       "usage_reserved,cost_reserved,usage_observed) "
                       "VALUES (?,?,'SUCCESS',1,?,1,'1')",
                       ((grant.campaign_id, "r1", 2**62),
                        (grant.campaign_id, "r2", 2**62)))
    binding = LiveHostBinding(grant, None,
        FixedHostPaths(tmp_path, tmp_path, tmp_path, tmp_path, tmp_path, path),
        "S-1-5-21-100")
    monkeypatch.setattr(controls, "load_live_binding", lambda: binding)
    monkeypatch.setattr(controls, "_windows_path_facts", lambda _: facts)
    with pytest.raises(DiagnosticDenied, match="usage lock or record"):
        LiveDiagnosticUsage().reserve(binding.record, "r3", items=1,
                                      usage_units=1, cost_micro_units=1)


def test_repository_source_resolver_checks_head_and_untracked_dirty(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, REVISION + "\n" if "rev-parse" in argv else "?? untracked.txt\n",
        )

    monkeypatch.setattr(controls.subprocess, "run", fake_run)
    assert resolve_repository_source(tmp_path) == (REVISION, False)
    assert calls[0][-2:] == ["rev-parse", "HEAD"]
    assert calls[1][-2:] == ["--porcelain=v1", "--untracked-files=all"]


def ledger_rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT request_id,outcome,items,usage_reserved,cost_reserved,"
                          "usage_observed FROM attempts ORDER BY request_id").fetchall()


@pytest.mark.parametrize("observed", ["0", "1", "99", "100", "UNKNOWN"])
def test_s1_atomic_usage_known_zero_unknown_and_no_refund(tmp_path, observed):
    path = tmp_path / "offline.sqlite"
    usage, grant = TemporaryDiagnosticUsage(path), record()
    usage.reserve(grant, "r1", items=1, usage_units=100, cost_micro_units=81)
    usage._finish(grant, "r1", "SUCCESS", observed)  # Explicit synthetic fixture.
    expected = [("r1", "SUCCESS", 1, 100, 81, observed)]
    assert ledger_rows(path) == expected
    with pytest.raises(DiagnosticDenied):
        usage._finish(grant, "r1", "FAILED", "0")
    assert ledger_rows(path) == expected
    restarted = TemporaryDiagnosticUsage(path)
    if observed == "UNKNOWN":
        with pytest.raises(DiagnosticDenied):
            restarted.reserve(grant, "r2", items=1, usage_units=1, cost_micro_units=1)
        assert len(ledger_rows(path)) == 1
    else:
        restarted.reserve(grant, "r2", items=1, usage_units=1, cost_micro_units=1)
        assert len(ledger_rows(path)) == 2


@pytest.mark.parametrize("observed", [None, 0, True, 1.0, "", "00", "01", "-1",
                                      "+1", "1.0", " 1", "1 ", "１", "unknown",
                                      str(2**63), "9" * 1000])
def test_s1_invalid_observed_usage_cannot_update_or_free_budget(tmp_path, observed):
    path = tmp_path / "offline.sqlite"
    usage, grant = TemporaryDiagnosticUsage(path), record()
    usage.reserve(grant, "r", items=1, usage_units=100, cost_micro_units=100)
    before = ledger_rows(path)
    with pytest.raises(DiagnosticDenied):
        usage._finish(grant, "r", "SUCCESS", observed)
    assert ledger_rows(path) == before


@pytest.mark.parametrize("observed", ["101", str(2**63 - 1)])
def test_s1_excess_usage_persists_exact_evidence_and_blocks_restart(tmp_path, observed):
    path = tmp_path / "offline.sqlite"
    usage, grant = TemporaryDiagnosticUsage(path), record()
    usage.reserve(grant, "r1", items=1, usage_units=100, cost_micro_units=91)
    with pytest.raises(DiagnosticDenied, match="evidence retained") as failure:
        usage._finish(grant, "r1", "SUCCESS", observed)
    assert len(str(failure.value)) < 160
    assert ledger_rows(path) == [("r1", "UNKNOWN", 1, 100, 91, observed)]
    with sqlite3.connect(path) as db:
        controls._validate_formal_connection(db)  # Same frozen DDL accepts audit fact.
    with pytest.raises(DiagnosticDenied):
        TemporaryDiagnosticUsage(path).reserve(
            grant, "r2", items=1, usage_units=1, cost_micro_units=1)
    assert len(ledger_rows(path)) == 1


def test_s1_no_public_live_observation_override(tmp_path):
    # Do not initialize the formal host: only test the live port's input boundary.
    usage = object.__new__(LiveDiagnosticUsage)
    with pytest.raises(TypeError):
        usage.finish(record(), "r", "SUCCESS", usage_observed="0")
    with pytest.raises(DiagnosticDenied, match="not applicable"):
        usage._finish(record(), "r", "SUCCESS", "0")


class CompletionFaultConnection:
    """Offline wrapper: inject a failure without replacing real SQLite rows."""

    def __init__(self, db, mode, events):
        self.db, self.mode, self.events = db, mode, events

    def execute(self, sql, *args):
        if sql.startswith("UPDATE"):
            self.events.append("update")
            if self.mode == "update":
                raise sqlite3.OperationalError("synthetic-secret-update")
            if self.mode == "update_after":
                self.db.execute(sql, *args)
                raise sqlite3.OperationalError("synthetic-secret-after-update")
        return self.db.execute(sql, *args)

    def commit(self):
        self.events.append("commit")
        if self.mode in ("commit_after", "ambiguous_after", "commit_after_rollback"):
            self.db.commit()
        raise sqlite3.OperationalError("synthetic-secret-commit")

    def rollback(self):
        self.events.append("rollback")
        if self.mode in ("rollback", "commit_after_rollback"):
            raise sqlite3.OperationalError("synthetic-secret-rollback")
        self.db.rollback()

    def close(self):
        self.events.append("close")
        self.db.close()
        if self.mode == "close":
            raise sqlite3.OperationalError("synthetic-secret-close")


@pytest.mark.parametrize("mode", ["update", "update_after", "commit_before", "commit_after",
                                  "ambiguous_after", "rollback", "commit_after_rollback", "close"])
def test_s1_update_commit_rollback_and_ambiguous_completion(tmp_path, monkeypatch, mode):
    path = tmp_path / "offline.sqlite"
    usage, grant = TemporaryDiagnosticUsage(path), record()
    usage.reserve(grant, "r1", items=1, usage_units=100, cost_micro_units=100)
    connect, events = usage._connect, []

    def fault_connect(*, read_only=False):
        if read_only:
            events.append("read_only")
            assert events[-2] == "close"
            if mode == "ambiguous_after":
                raise sqlite3.OperationalError("synthetic-secret-read")
            return connect(read_only=True)
        return CompletionFaultConnection(connect(), mode, events)

    monkeypatch.setattr(usage, "_connect", fault_connect)
    if mode in ("commit_after", "commit_after_rollback"):
        usage.finish(grant, "r1", "SUCCESS")  # Confirmed by a separate read only handle.
    else:
        with pytest.raises(DiagnosticDenied, match="UNKNOWN") as failure:
            usage.finish(grant, "r1", "SUCCESS")
        assert "synthetic-secret" not in str(failure.value)
    assert events.count("update") == 1
    assert events.count("commit") == (0 if mode in ("update", "update_after") else 1)
    assert events.count("rollback") == events.count("close") == events.count("read_only") == 1
    outcome = "SUCCESS" if mode in ("commit_after", "ambiguous_after", "commit_after_rollback") else "IN_FLIGHT"
    assert ledger_rows(path) == [("r1", outcome, 1, 100, 100, "UNKNOWN")]
    with pytest.raises(DiagnosticDenied):
        TemporaryDiagnosticUsage(path).reserve(
            grant, "r2", items=1, usage_units=1, cost_micro_units=1)
    assert len(ledger_rows(path)) == 1


def test_s1_excess_evidence_survives_completion_write_failure_in_bounded_denial(tmp_path, monkeypatch):
    path = tmp_path / "offline.sqlite"
    usage, grant = TemporaryDiagnosticUsage(path), record()
    usage.reserve(grant, "r1", items=1, usage_units=100, cost_micro_units=100)
    connect, events = usage._connect, []
    monkeypatch.setattr(usage, "_connect", lambda **kwargs: (
        connect(**kwargs) if kwargs.get("read_only")
        else CompletionFaultConnection(connect(), "commit_before", events)))
    with pytest.raises(DiagnosticDenied, match="observed=101; reserved=100") as failure:
        usage._finish(grant, "r1", "SUCCESS", "101")
    assert "synthetic-secret" not in str(failure.value) and len(str(failure.value)) < 200
    assert ledger_rows(path) == [("r1", "IN_FLIGHT", 1, 100, 100, "UNKNOWN")]
    assert events == ["update", "commit", "rollback", "close"]


@pytest.mark.parametrize("field", ["items", "usage_units", "cost_micro_units"])
@pytest.mark.parametrize("value", [0, -1, True, "1", 1.0, 2**63])
def test_s1_invalid_reservation_integers_never_create_rows(tmp_path, field, value):
    path = tmp_path / "offline.sqlite"
    usage = TemporaryDiagnosticUsage(path)
    args = dict(items=1, usage_units=1, cost_micro_units=1)
    args[field] = value
    with pytest.raises(DiagnosticDenied):
        usage.reserve(record(), "r", **args)
    assert ledger_rows(path) == []


@pytest.mark.parametrize("column", ["items", "usage_reserved", "cost_reserved"])
def test_s1_sqlite_sum_overflow_keeps_all_reservations(tmp_path, column):
    path = tmp_path / "offline.sqlite"
    usage, grant = TemporaryDiagnosticUsage(path), record()
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO attempts VALUES (?,?,'SUCCESS',1,1,1,'0')",
                       [(grant.campaign_id, "r1"), (grant.campaign_id, "r2")])
        db.execute(f"UPDATE attempts SET {column}=?", (2**62,))
    before = ledger_rows(path)
    with pytest.raises(DiagnosticDenied, match="usage lock or record"):
        usage.reserve(grant, "r3", items=1, usage_units=1, cost_micro_units=1)
    assert ledger_rows(path) == before


@pytest.mark.parametrize("budget", ["maximum_invocations", "maximum_items",
                                   "maximum_usage_units", "maximum_cost_micro_units"])
def test_s1_usage_and_cost_ceilings_remain_independent(tmp_path, budget):
    path = tmp_path / "offline.sqlite"
    usage = TemporaryDiagnosticUsage(path)
    grant = record(**{budget: 1 if budget in ("maximum_invocations", "maximum_items") else 100})
    usage.reserve(grant, "r1", items=1, usage_units=100, cost_micro_units=100)
    usage._finish(grant, "r1", "SUCCESS", "0")
    before = ledger_rows(path)
    with pytest.raises(DiagnosticDenied, match="budget"):
        usage.reserve(grant, "r2", items=1, usage_units=1, cost_micro_units=1)
    assert ledger_rows(path) == before


def test_s1_concurrent_completion_is_at_most_once(tmp_path):
    path = tmp_path / "offline.sqlite"
    usage, grant = TemporaryDiagnosticUsage(path), record()
    usage.reserve(grant, "r", items=1, usage_units=100, cost_micro_units=100)
    barrier = Barrier(2)

    def complete(_):
        local = TemporaryDiagnosticUsage(path)
        barrier.wait()
        try:
            local._finish(grant, "r", "SUCCESS", "0")
            return "completed"
        except DiagnosticDenied:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(complete, (1, 2))) == ["completed", "denied"]
    assert ledger_rows(path) == [("r", "SUCCESS", 1, 100, 100, "0")]
