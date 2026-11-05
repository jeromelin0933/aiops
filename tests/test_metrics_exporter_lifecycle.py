"""Owned lifecycle only; real socket cases use ephemeral loopback, no scenario."""
from types import SimpleNamespace
from threading import Event, Thread, current_thread
import json
import socket
from urllib.request import urlopen

import pytest

from src.metrics_generator import metrics_generator as module


def config():
    return SimpleNamespace(raw={"metrics": {"exporter_port": 0}})


def test_legacy_default_start_is_idempotent_and_keeps_default_bind(monkeypatch):
    calls = []
    server = SimpleNamespace(shutdown=lambda:calls.append("shutdown"),
                             server_close=lambda:calls.append("close"))
    serving = SimpleNamespace(join=lambda timeout:calls.append("join"), is_alive=lambda:False)
    def start(port):
        calls.append(port)
        return server, serving
    monkeypatch.setattr(module, "start_http_server", start)
    generator = module.MetricsGenerator(config())
    assert generator.start_exporter() is None
    generator.start_exporter()
    assert calls == [0] and generator.exporter_running
    generator.close()
    generator.close()
    assert calls == [0, "shutdown", "join", "close"]
    assert not generator.exporter_running


@pytest.mark.parametrize("abnormal", [False, True])
def test_owned_loopback_exporter_releases_port_and_threads(abnormal):
    generator = module.MetricsGenerator(config())
    port = None
    try:
        with generator.owned_exporter(address="127.0.0.1", timeout_seconds=3):
            server, serving = generator._exporter_handles
            port = server.server_port
            assert serving.is_alive()
            with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as response:
                assert b"api_p95_latency_ms" in response.read(1024*1024)
            if abnormal:
                raise LookupError("synthetic owner failure")
    except LookupError:
        assert abnormal
    assert not generator.exporter_running
    assert not serving.is_alive() and generator._shutdown_worker is None
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_repeated_isolated_exporter_cycles_have_no_serving_thread_left():
    generator = module.MetricsGenerator(config())
    threads = []
    port = None
    for _ in range(2):
        with generator.owned_exporter(address="127.0.0.1", timeout_seconds=3):
            if port is None:
                port = generator._exporter_handles[0].server_port
                generator._config.raw["metrics"]["exporter_port"] = port
            assert generator._exporter_handles[0].server_port == port
            threads.append(generator._exporter_handles[1])
    assert not generator.exporter_running and all(not t.is_alive() for t in threads)


def test_shutdown_wait_is_not_cancellation_and_retains_handles(monkeypatch):
    entered, release = Event(), Event()
    def shutdown():
        entered.set()
        release.wait()
    closed = []
    handles = (SimpleNamespace(shutdown=shutdown, server_close=lambda:closed.append(True)),
               SimpleNamespace(join=lambda timeout:None, is_alive=lambda:False))
    monkeypatch.setattr(module, "start_http_server", lambda port:handles)
    generator = module.MetricsGenerator(config())
    generator.start_exporter()
    try:
        with pytest.raises(module.ExporterLifecycleError, match="handles retained"):
            generator.stop_exporter(timeout_seconds=0.02)
        assert entered.is_set() and generator.exporter_running
        assert generator._exporter_handles is handles
        with pytest.raises(module.ExporterLifecycleError):generator.start_exporter()
        assert not closed
    finally:
        release.set()
        generator.stop_exporter(timeout_seconds=2)
    assert closed == [True] and not generator.exporter_running


def test_shutdown_error_preserves_ownership_and_never_reports_success(monkeypatch):
    def shutdown():raise OSError("synthetic shutdown failure")
    handles = (SimpleNamespace(shutdown=shutdown, server_close=lambda:None),
               SimpleNamespace(join=lambda timeout:None,is_alive=lambda:False))
    monkeypatch.setattr(module,"start_http_server",lambda port:handles)
    generator=module.MetricsGenerator(config());generator.start_exporter()
    with pytest.raises(module.ExporterLifecycleError,match="cleanup failed"):
        generator.stop_exporter()
    assert generator.exporter_running and generator._exporter_handles is handles
    assert not generator._shutdown_worker.is_alive()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_cleanup_wait_rejected(timeout):
    with pytest.raises(ValueError):module.MetricsGenerator(config()).stop_exporter(timeout_seconds=timeout)


def test_failed_bind_does_not_claim_a_running_exporter(monkeypatch):
    def start(*a, **kw):raise OSError("synthetic bind failure")
    monkeypatch.setattr(module,"start_http_server",start)
    generator=module.MetricsGenerator(config())
    with pytest.raises(OSError):generator.start_exporter()
    assert not generator.exporter_running


def test_stale_stop_cannot_clear_restarted_exporter_generation(monkeypatch, record_testsuite_property):
    """Two overlapping Stops; pause the old caller AFTER its worker has joined.

    No timing sleeps: Events force the second Stop to finish and a same-port
    replacement to start before the old Stop is allowed to finalize ownership.
    """
    old_joined, release_old = Event(), Event()
    trace, errors = [], []

    class ControlledWorker:
        def __init__(self, *args, **kwargs):
            self.thread = Thread(*args, **kwargs)
        def start(self): self.thread.start()
        def is_alive(self): return self.thread.is_alive()
        def join(self, timeout):
            self.thread.join(timeout)
            if current_thread().name == "a01-stale-stop":
                assert not self.thread.is_alive()
                trace.append("old_worker_joined")
                old_joined.set()
                if not release_old.wait(5):
                    raise AssertionError("controlled old Stop was not released")

    monkeypatch.setattr(module, "Thread", ControlledWorker)
    generator = module.MetricsGenerator(config())
    replacement = None
    def stale_stop():
        try:
            generator.stop_exporter(timeout_seconds=3)
            trace.append("old_stop_returned")
        except BaseException as exc:
            errors.append(exc)

    generator.start_exporter(address="127.0.0.1")
    first = generator._exporter_handles
    generator._config.raw["metrics"]["exporter_port"] = first[0].server_port
    caller = Thread(target=stale_stop, name="a01-stale-stop", daemon=True)
    try:
        caller.start()
        assert old_joined.wait(5)
        generator.stop_exporter(timeout_seconds=3)  # Second overlapping Stop.
        trace.append("second_stop_completed")
        assert not generator.exporter_running and not first[1].is_alive()
        generator.start_exporter(address="127.0.0.1")
        replacement = generator._exporter_handles
        assert replacement[0].server_port == first[0].server_port
        trace.append("new_generation_started")
        release_old.set()
        caller.join(5)
        assert not caller.is_alive() and not errors
        assert generator._exporter_handles is replacement
        assert generator.exporter_running and replacement[1].is_alive()
        with urlopen(f"http://127.0.0.1:{replacement[0].server_port}/metrics", timeout=2) as response:
            assert response.status == 200
        generator.stop_exporter(timeout_seconds=3)
        trace.append("new_generation_stopped")
        assert not generator.exporter_running and not replacement[1].is_alive()
        assert trace == ["old_worker_joined", "second_stop_completed", "new_generation_started",
                         "old_stop_returned", "new_generation_stopped"]
        record_testsuite_property("A01_CONTROLLED_INTERLEAVING", json.dumps(trace))
    finally:
        release_old.set()
        caller.join(5)
        generator.stop_exporter(timeout_seconds=3)
        # Red-test cleanup also owns the saved pair if buggy code lost it.
        for pair in (first, replacement):
            if pair is not None and pair[1].is_alive():
                pair[0].shutdown()
                pair[1].join(3)
                pair[0].server_close()
