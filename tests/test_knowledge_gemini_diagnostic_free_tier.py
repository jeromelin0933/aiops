"""Synthetic Free Tier evidence only; all network/DNS/ADC paths are sealed."""

from dataclasses import replace
import pytest

import knowledge_index.gemini_diagnostic as diagnostic
import knowledge_index.gemini_diagnostic_controls as controls
from test_knowledge_gemini_diagnostic_controls import safety_case, safety_parse, ledger_rows
from test_knowledge_gemini_diagnostic_dispatch import campaign, mock_transport, original_request
from test_knowledge_gemini_embedding2_serialization import offline_sdk


def free_evidence(value, record):
    value["pricing_evidence"].update(price_tier="FREE_TIER_TEXT_V1",
                                     rate_micro_units_per_million_tokens=0)
    value["free_tier_evidence"] = dict(
        pricing_identity="FREE_TIER_TEXT_V1",
        account_context=dict(account_reference=value["pricing_evidence"]["account_reference"],
                             **value["credential_binding"], project_tier="FREE"),
        quota_evidence=dict(rpm=100, tpm=30000, rpd=1000,
                            source=dict(value["pricing_evidence"]["source"])),
        data_use_permission=dict(pm_approval_reference=record.pm_approval_reference,
                                 authority_identity=record.spec_lead_identity,
                                 source=dict(value["pricing_evidence"]["source"])),
    )
    return value


def configure_free(campaign, monkeypatch, *, kind="query", count=1, record_changes=None):
    prepared, binding, artifact, usage = campaign(kind, count, record_changes)
    # Thaw existing synthetic metadata, retaining every source/input/E4 binding.
    def thaw(value):
        if hasattr(value, "items"):
            return {key: thaw(child) for key, child in value.items()}
        return value
    value = free_evidence(thaw(artifact.fields), binding.record)
    monkeypatch.setattr(diagnostic, "load_live_safety_artifact", lambda: safety_parse(value))
    return prepared, binding, value, usage


def test_free_zero_price_preserves_positive_reservation_and_unknown_usage(
    campaign, monkeypatch, tmp_path, offline_sdk,
):
    prepared, binding, _, _ = configure_free(campaign, monkeypatch)
    proof, basis, tokens, cost = diagnostic._live_safety(binding.record, prepared.texts)
    decision = diagnostic.evaluate_diagnostic_safety(binding.record, basis, prepared.kind,
        prepared.identities, prepared.texts, prepared.contents, prepared.config, proof, cost)
    assert decision.minimum_cost_micro_units == 0
    assert tokens > 0 and cost == 1  # conservative hold, not a unit price
    calls = mock_transport(monkeypatch, tmp_path)
    result = diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert result.outcome == "SUCCESS" and len(calls) == 1
    assert result.usage == "UNKNOWN"
    row = ledger_rows(tmp_path / "attempts.sqlite")[0]
    assert row[-1] == "UNKNOWN" and row[-2] == 1
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(original_request(prepared, "query", tmp_path, 1), live_opt_in=True)
    assert len(calls) == 1 and offline_sdk.blocked_calls == []


@pytest.mark.parametrize("field", ["maximum_cost_micro_units", "maximum_invocations",
    "maximum_items", "maximum_item_tokens", "maximum_aggregate_tokens",
    "maximum_payload_bytes", "maximum_usage_units"])
@pytest.mark.parametrize("invalid", [0, -1, True, None, 2**63])
def test_free_campaign_bounds_remain_positive_finite(field, invalid, offline_sdk):
    record = safety_case()[0]
    with pytest.raises(controls.DiagnosticDenied):
        replace(record, **{field: invalid})


@pytest.mark.parametrize("mutation", ["pricing", "quota", "profile", "permission", "tier",
    "identity", "account", "revision", "rate", "boolean_rate", "expired_quota", "paid_zero",
    "paid_with_free", "extra_approved_flag"])
def test_free_schema_rejects_missing_or_inconsistent_evidence(mutation, offline_sdk):
    record, _, _, value = safety_case()
    free_evidence(value, record)
    free = value["free_tier_evidence"]
    if mutation == "pricing":
        del value["pricing_evidence"]
    elif mutation in ("quota", "permission"):
        del free[{"quota": "quota_evidence", "permission": "data_use_permission"}[mutation]]
    elif mutation in ("profile", "account", "revision", "tier"):
        key = dict(profile="profile_identity", account="account_reference",
                   revision="secret_revision", tier="project_tier")[mutation]
        free["account_context"][key] = "different"
    elif mutation == "identity":
        free["pricing_identity"] = "STANDARD_PAID_TEXT"
    elif mutation in ("rate", "boolean_rate"):
        value["pricing_evidence"]["rate_micro_units_per_million_tokens"] = (True if mutation == "boolean_rate" else 1)
    elif mutation == "expired_quota":
        free["quota_evidence"]["source"]["expires_at"] = "2000-01-01T00:00:00Z"
    elif mutation in ("paid_zero", "paid_with_free"):
        value["pricing_evidence"]["price_tier"] = "STANDARD_PAID_TEXT"
        if mutation == "paid_zero":
            del value["free_tier_evidence"]
        else:
            value["pricing_evidence"]["rate_micro_units_per_million_tokens"] = 1000
    else:
        free["data_use_permission"]["approved"] = True
    with pytest.raises(controls.DiagnosticDenied):
        safety_parse(value)


@pytest.mark.parametrize("mutation", ["no_opt_in", "missing_authorization", "approval", "authority",
    "token", "payload", "usage", "items", "quota_not_budget"])
def test_free_rejection_precedes_credential_and_dispatch(
    campaign, monkeypatch, tmp_path, mutation, offline_sdk,
):
    changes = {"maximum_usage_units": 1} if mutation in ("usage", "quota_not_budget") else None
    prepared, binding, value, _ = configure_free(campaign, monkeypatch,
        kind="document" if mutation == "items" else "query",
        count=54 if mutation == "items" else 1, record_changes=changes)
    def forbidden(*args):
        pytest.fail("denied Free Tier request reached credentials/transport")
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", forbidden)
    monkeypatch.setattr(diagnostic, "_create_live_transport", forbidden)
    if mutation == "missing_authorization":
        monkeypatch.setattr(diagnostic, "load_live_binding", lambda: (_ for _ in ()).throw(
            controls.DiagnosticDenied("authorization unavailable")))
    elif mutation in ("approval", "authority"):
        key = "pm_approval_reference" if mutation == "approval" else "authority_identity"
        value["free_tier_evidence"]["data_use_permission"][key] = "unapproved"
    elif mutation in ("token", "payload", "items"):
        key = dict(token="aggregate_token_limit", payload="payload_byte_limit", items="item_limit")[mutation]
        value["provider_limits"][key] = 1
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(original_request(prepared, binding.approved_input.kind, tmp_path,
            len(binding.approved_input.identities)), live_opt_in=mutation != "no_opt_in")
    assert ledger_rows(tmp_path / "attempts.sqlite") == []
    assert offline_sdk.blocked_calls == []


@pytest.mark.parametrize("mutation", ["rate", "permission", "tier", "reservation", "item_tokens"])
def test_free_evaluator_rechecks_rate_permission_and_bounds(campaign, monkeypatch, mutation):
    prepared, binding, _, _ = configure_free(campaign, monkeypatch)
    proof, basis, _, cost = diagnostic._live_safety(binding.record, prepared.texts)
    if mutation == "rate":
        basis = replace(basis, price_micro_units_per_million_tokens=1)
    elif mutation == "permission":
        basis = replace(basis, free_tier_permission_reference=None)
    elif mutation == "tier":
        basis = replace(basis, price_tier="STANDARD_PAID_TEXT")
    elif mutation == "reservation":
        cost = 0
    else:
        basis = replace(basis, provider_item_token_limit=1)
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.evaluate_diagnostic_safety(binding.record, basis, prepared.kind,
            prepared.identities, prepared.texts, prepared.contents, prepared.config, proof, cost)


@pytest.mark.parametrize("mode,outcome", [("timeout", "TIMEOUT"), ("network", "UNKNOWN")])
def test_free_ambiguous_attempt_retains_all_budgets(campaign, monkeypatch, tmp_path, mode, outcome):
    prepared, _, _, _ = configure_free(campaign, monkeypatch)
    calls = mock_transport(monkeypatch, tmp_path, mode=mode)
    request = original_request(prepared, "query", tmp_path, 1)
    result = diagnostic.run_live(request, live_opt_in=True)
    assert result.outcome == outcome and result.usage == "UNKNOWN"
    before = ledger_rows(tmp_path / "attempts.sqlite")
    assert before[0][-1] == "UNKNOWN" and before[0][-2] == 1
    with pytest.raises(controls.DiagnosticDenied):
        diagnostic.run_live(request, live_opt_in=True)
    assert ledger_rows(tmp_path / "attempts.sqlite") == before and len(calls) == 1
