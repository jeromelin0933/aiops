"""Characterize a capture blocker; PASS does NOT mean first-failure STOP exists."""
from types import SimpleNamespace
import socket

import pytest
import yaml

from alert_correlation.state import RetryDisposition
from runtime_orchestration.config import load_runtime_config, RuntimeConfigError
from runtime_orchestration.contracts import RuntimeWorkKind
from test_runtime_host_e2e import _HostRuntime, _event, NOW


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs): raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


def test_current_config_has_no_zero_retry_setting(tmp_path):
    from pathlib import Path
    raw = yaml.safe_load(Path("configs/runtime_orchestration.yaml").read_text())
    raw.pop("rca")
    raw["retry"] = {"limit": 0, "delays_seconds": []}
    path = tmp_path / "no-retry.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(RuntimeConfigError, match="positive integer"):
        load_runtime_config(path)


def test_retryable_intake_failure_can_continue_to_retry_within_same_cycle(tmp_path, monkeypatch):
    """Real RuntimeCore plus isolated stores, injected exception/work only.

    This proves an outside run() wrapper cannot promise first-failure stop.
    It is not a production failure or a real capture.
    """
    event = _event("EVT-OFFLINE-FAILURE-BOUNDARY", "brute_force_detected", NOW,
                   source_ip="192.0.2.77")
    runtime = _HostRuntime(tmp_path / "isolated-core", [event])
    calls = []
    class InjectedDomainFailure(Exception):
        retry_disposition = RetryDisposition.RETRYABLE
    def fail(_event_id):
        calls.append("initial-failure")
        raise InjectedDomainFailure()
    work = SimpleNamespace(work_kind=RuntimeWorkKind.DOMAIN_OPERATION)
    try:
        runtime.core.startup()
        monkeypatch.setattr(runtime.pending, "process_authoritative_event", fail)
        monkeypatch.setattr(runtime.retry, "enumerate_eligibility",
            lambda: SimpleNamespace(eligible=(work,), next_eligibility=None))
        monkeypatch.setattr(runtime.core._retry_executor, "execute_retry",
                            lambda _work: calls.append("retry-executed"))
        monkeypatch.setattr(runtime.core._pending_scheduler, "poll", lambda **kw: None)
        runtime.core.run_cycle()
        assert calls == ["initial-failure", "retry-executed"]
    finally:
        runtime.close()
