from dataclasses import replace

import pytest

from incident_evidence import (
    CaptureTerminalKind,
    DEFAULT_SOURCE_REQUEST_POLICIES,
    EvidenceDomainError,
    EvidenceFailureKind,
    EvidenceSource,
    SourceAdmissionPolicy,
)
from test_incident_evidence_capture import capture_command, capture_service


def test_equivalent_terminal_replay_short_circuits_before_all_upstream_io(tmp_path):
    service, incident_reader, event_reader, adapters = capture_service(tmp_path)
    command = capture_command()
    first = service.capture_evidence(command)
    counts = (incident_reader.calls, event_reader.calls, tuple(a.calls for a in adapters.values()))
    second = service.capture_evidence(command)
    assert second == first
    assert (incident_reader.calls, event_reader.calls, tuple(a.calls for a in adapters.values())) == counts


def test_contradictory_replay_fails_closed_before_upstream_io(tmp_path):
    service, incident_reader, event_reader, adapters = capture_service(tmp_path)
    service.capture_evidence(capture_command())
    counts = (incident_reader.calls, event_reader.calls, tuple(a.calls for a in adapters.values()))
    contradictory = replace(capture_command(), incident_id="INC-DIFFERENT")
    with pytest.raises(EvidenceDomainError) as caught:
        service.capture_evidence(contradictory)
    assert caught.value.kind is EvidenceFailureKind.CONTRADICTORY_REPLAY
    assert (incident_reader.calls, event_reader.calls, tuple(a.calls for a in adapters.values())) == counts


def test_changed_request_policy_under_same_command_identity_fails_closed(tmp_path):
    changed = dict(DEFAULT_SOURCE_REQUEST_POLICIES)
    changed[EvidenceSource.LOKI] = replace(
        changed[EvidenceSource.LOKI], query_resource="different-stream-semantics"
    )
    service, incident_reader, event_reader, adapters = capture_service(
        tmp_path, request_policies=changed
    )
    outcome = service.capture_evidence(capture_command())
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure.kind is EvidenceFailureKind.INVALID_CAPTURE_COMMAND
    assert incident_reader.calls == event_reader.calls == 0
    assert all(adapter.calls == 0 for adapter in adapters.values())


def test_changed_admission_policy_under_same_command_identity_fails_closed(tmp_path):
    changed = SourceAdmissionPolicy(
        degraded_unavailable_sources=frozenset({EvidenceSource.LOKI})
    )
    service, incident_reader, event_reader, adapters = capture_service(
        tmp_path, admission_policy=changed
    )
    outcome = service.capture_evidence(capture_command())
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure.kind is EvidenceFailureKind.INVALID_CAPTURE_COMMAND
    assert incident_reader.calls == event_reader.calls == 0
    assert all(adapter.calls == 0 for adapter in adapters.values())


def test_terminal_replay_still_short_circuits_changed_effective_policy(tmp_path):
    original_service, *_ = capture_service(tmp_path)
    command = capture_command()
    original = original_service.capture_evidence(command)
    changed = SourceAdmissionPolicy(
        degraded_unavailable_sources=frozenset({EvidenceSource.LOKI})
    )
    replay_service, incident_reader, event_reader, adapters = capture_service(
        tmp_path,
        store=original_service._store,
        admission_policy=changed,
    )
    assert replay_service.capture_evidence(command) == original
    assert incident_reader.calls == event_reader.calls == 0
    assert all(adapter.calls == 0 for adapter in adapters.values())
