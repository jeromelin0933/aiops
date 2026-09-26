from concurrent.futures import ThreadPoolExecutor

from dataclasses import replace

from incident_evidence import EvidenceDomainError, EvidenceFailureKind, EvidenceSource, SourceStatus
from test_incident_evidence_capture import capture_command, capture_service


def test_equivalent_same_operation_converges_and_different_operations_share_revision(tmp_path):
    service, *_ = capture_service(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        same = list(pool.map(service.capture_evidence, [capture_command(), capture_command()]))
    assert same[0] == same[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        different = list(pool.map(service.capture_evidence, [capture_command("CAP-2"), capture_command("CAP-3")]))
    assert different[0].snapshot_id != different[1].snapshot_id
    assert different[0].revision_id == different[1].revision_id


def test_concurrent_contradictory_same_operation_has_one_authority_and_loser_fails_closed(tmp_path):
    service, *_ = capture_service(tmp_path)
    first = capture_command()
    contradictory = replace(first, snapshot_at=first.snapshot_at.replace(minute=5))

    def invoke(command):
        try:
            return service.capture_evidence(command)
        except EvidenceDomainError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, [first, contradictory]))
    outcomes = [item for item in results if not isinstance(item, EvidenceDomainError)]
    failures = [item for item in results if isinstance(item, EvidenceDomainError)]
    assert len(outcomes) == 1 and len(failures) == 1
    assert failures[0].kind is EvidenceFailureKind.CONTRADICTORY_REPLAY
    assert service.read_capture_outcome("CAP-1") == outcomes[0]
