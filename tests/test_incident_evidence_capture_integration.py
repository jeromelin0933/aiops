import pytest

from incident_evidence import (
    CaptureFinalizationHandoff,
    CaptureTerminalKind,
    EvidenceSource,
    SourceStatus,
    SqliteEvidenceStore,
)
from test_incident_evidence_capture import capture_command, capture_service


class InjectOnce:
    def __init__(self, point):
        self.point = point
        self.triggered = False

    def __call__(self, point):
        if point == self.point and not self.triggered:
            self.triggered = True
            raise RuntimeError(point)


@pytest.mark.parametrize("point", ["before_transaction", "during_transaction"])
def test_precommit_fault_has_no_terminal_authority_or_snapshot(tmp_path, point):
    store = SqliteEvidenceStore(tmp_path / f"{point}.sqlite", fault_injector=InjectOnce(point))
    service, *_ = capture_service(tmp_path, store=store)
    with pytest.raises(RuntimeError, match=point):
        service.capture_evidence(capture_command())
    assert service.read_capture_outcome("CAP-1") is None
    assert store.enumerate_recovery_facts().snapshots == ()


def test_response_loss_after_commit_replays_then_survives_reopen(tmp_path):
    path = tmp_path / "response-loss.sqlite"
    store = SqliteEvidenceStore(path, fault_injector=InjectOnce("after_commit"))
    service, *_ = capture_service(tmp_path, store=store)
    command = capture_command()
    with pytest.raises(RuntimeError, match="after_commit"):
        service.capture_evidence(command)
    committed = service.read_capture_outcome("CAP-1")
    assert committed.terminal_kind is CaptureTerminalKind.SUCCESS
    assert service.capture_evidence(command) == committed
    store.close()

    reopened = SqliteEvidenceStore(path)
    assert reopened.read_capture_outcome("CAP-1") == committed
    assert reopened.resolve_snapshot(committed.snapshot_id).revision_id == committed.revision_id
    assert reopened.resolve_revision(committed.revision_id) is not None


def test_failure_response_loss_replays_same_failure_without_snapshot(tmp_path):
    path = tmp_path / "failure-response-loss.sqlite"
    store = SqliteEvidenceStore(path, fault_injector=InjectOnce("after_commit"))
    statuses = {EvidenceSource.LOKI: SourceStatus.UNAVAILABLE, EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE}
    service, *_ = capture_service(tmp_path, statuses, store=store)
    command = capture_command()
    handoff = CaptureFinalizationHandoff("CAP-1", "runtime-exhaustion-1", (EvidenceSource.LOKI,))
    with pytest.raises(RuntimeError, match="after_commit"):
        service.capture_evidence(command, finalization=handoff)
    committed = service.read_capture_outcome("CAP-1")
    assert committed.terminal_kind is CaptureTerminalKind.FAILURE
    assert service.capture_evidence(command) == committed
    assert store.enumerate_recovery_facts().snapshots == ()
    store.close()
    reopened = SqliteEvidenceStore(path)
    assert reopened.read_capture_outcome("CAP-1") == committed
    assert reopened.enumerate_recovery_facts().snapshots == ()


@pytest.mark.parametrize("point", ["before_transaction", "during_transaction"])
def test_failure_precommit_fault_rolls_back_all_authority(tmp_path, point):
    path = tmp_path / f"failure-{point}.sqlite"
    store = SqliteEvidenceStore(path, fault_injector=InjectOnce(point))
    statuses = {EvidenceSource.LOKI: SourceStatus.UNAVAILABLE, EvidenceSource.PROMETHEUS: SourceStatus.AVAILABLE}
    service, *_ = capture_service(tmp_path, statuses, store=store)
    handoff = CaptureFinalizationHandoff("CAP-1", "runtime-exhaustion-1", (EvidenceSource.LOKI,))
    with pytest.raises(RuntimeError, match=point):
        service.capture_evidence(capture_command(), finalization=handoff)
    assert service.read_capture_outcome("CAP-1") is None
    assert store.enumerate_recovery_facts().snapshots == ()
