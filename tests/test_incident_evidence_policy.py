from pathlib import Path

import pytest
import yaml

from incident_evidence import (
    EvidenceDomainError,
    EvidenceFailureKind,
    EvidencePolicyConfigError,
    EvidenceSource,
    build_selector_fact,
    ensure_no_ground_truth_fields,
    load_evidence_policy,
    redact_sensitive_mapping,
    sanitize_provenance,
    validate_safe_provenance,
)


CONFIG_PATH = Path("configs/incident_evidence.yaml")


def load_raw():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def write_config(tmp_path, value):
    path = tmp_path / "incident_evidence.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_checked_in_policy_is_versioned_and_matches_spec_defaults():
    policy = load_evidence_policy(CONFIG_PATH)

    assert policy.config_version == "1.0"
    assert policy.windows.logs_pre_seconds == 120
    assert policy.windows.metrics_pre_seconds == 300
    assert policy.windows.post_seconds == 120
    assert policy.config_identity
    assert policy.capture_contract_version
    assert policy.canonicalization_version
    assert policy.source_policy_version
    assert policy.bounds_policy_version
    assert policy.selector_policy_version
    assert policy.redaction_policy_version
    assert policy.materiality_rule_versions == ("evidence-materiality-v1",)


def test_policy_rejects_unknown_keys_versions_and_ground_truth(tmp_path):
    unknown = load_raw()
    unknown["runtime_retry_budget"] = 4
    with pytest.raises(EvidencePolicyConfigError, match="unsupported"):
        load_evidence_policy(write_config(tmp_path, unknown))

    wrong_version = load_raw()
    wrong_version["config_version"] = "2.0"
    with pytest.raises(EvidencePolicyConfigError, match="unsupported"):
        load_evidence_policy(write_config(tmp_path, wrong_version))

    ground_truth = load_raw()
    ground_truth["selector_allowlist"]["LOKI"].append("scenario_id")
    with pytest.raises(EvidencePolicyConfigError, match="forbidden"):
        load_evidence_policy(write_config(tmp_path, ground_truth))

    secret_selector = load_raw()
    secret_selector["selector_allowlist"]["LOKI"].append("authorization")
    with pytest.raises(EvidencePolicyConfigError, match="secret-bearing"):
        load_evidence_policy(write_config(tmp_path, secret_selector))


def test_selector_requires_allowlist_and_escapes_injection_before_query_use():
    policy = load_evidence_policy(CONFIG_PATH)
    raw = 'payments"} |= "secret\\tail\nnext'
    selector = build_selector_fact(
        EvidenceSource.LOKI,
        "service_name",
        raw,
        selector_policy_version=policy.selector_policy_version,
        allowlist=policy.selector_allowlist,
    )

    assert selector.normalized_value == raw
    assert selector.escaped_value == 'payments\\"} |= \\"secret\\\\tail\\nnext'
    assert selector.escaped_value != raw

    with pytest.raises(EvidenceDomainError) as blocked:
        build_selector_fact(
            EvidenceSource.PROMETHEUS,
            "scenario_id",
            "S1",
            selector_policy_version=policy.selector_policy_version,
            allowlist=policy.selector_allowlist,
        )
    assert blocked.value.kind is EvidenceFailureKind.UNSAFE_EVIDENCE_CONTENT


def test_ground_truth_fields_are_rejected_recursively():
    with pytest.raises(EvidenceDomainError) as captured:
        ensure_no_ground_truth_fields(
            {"safe": [{"validator_expected_answer": "database"}]}
        )
    assert captured.value.kind is EvidenceFailureKind.UNSAFE_EVIDENCE_CONTENT


def test_secret_values_are_excluded_from_provenance_and_redacted_for_safe_logs():
    raw = {
        "source": "LOKI",
        "Authorization": "Bearer top-secret",
        "endpoint": "https://example.test/query?api_key=abc&status=active",
        "nested": {"token": "secret", "page": 2},
    }

    sanitized = sanitize_provenance(raw)
    assert "Authorization" not in sanitized
    assert "api_key" not in sanitized["endpoint"]
    assert "status=active" in sanitized["endpoint"]
    assert "token" not in sanitized["nested"]
    validate_safe_provenance(sanitized)

    redacted = redact_sensitive_mapping(raw)
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert "abc" not in redacted["endpoint"]

    with pytest.raises(EvidenceDomainError) as captured:
        validate_safe_provenance(raw)
    assert captured.value.kind is EvidenceFailureKind.UNSAFE_EVIDENCE_CONTENT


@pytest.mark.parametrize(
    ("credential_url", "forbidden_values"),
    [
        (
            "https://alice:password@example.test/query",
            ("alice", "password"),
        ),
        ("https://token@example.test/path", ("token",)),
    ],
)
def test_url_userinfo_credentials_are_removed_and_raw_provenance_is_rejected(
    credential_url, forbidden_values
):
    sanitized = sanitize_provenance({"endpoint": credential_url})

    assert sanitized["endpoint"].startswith("https://example.test/")
    assert all(value not in sanitized["endpoint"] for value in forbidden_values)
    validate_safe_provenance(sanitized)

    with pytest.raises(EvidenceDomainError) as captured:
        validate_safe_provenance({"endpoint": credential_url})
    assert captured.value.kind is EvidenceFailureKind.UNSAFE_EVIDENCE_CONTENT

    redacted = redact_sensitive_mapping({"endpoint": credential_url})
    assert all(value not in redacted["endpoint"] for value in forbidden_values)
