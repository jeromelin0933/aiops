"""Opt-in Docker smoke/E2E; kept separate from deterministic host regression."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_RUNTIME_DOCKER_E2E") != "1",
    reason="set RUN_RUNTIME_DOCKER_E2E=1 for Docker integration evidence",
)

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
PROBE = ROOT / "docker" / "runtime" / "e2e_probe.py"


def _run(project: str, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )


def _probe(project: str, command: str) -> dict:
    mounted = f"{PROBE}:/tmp/runtime_e2e_probe.py:ro"
    result = _run(
        project,
        "run",
        "--rm",
        "--no-deps",
        "--volume",
        mounted,
        "runtime",
        "python",
        "/tmp/runtime_e2e_probe.py",
        command,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _complete(snapshot: dict) -> bool:
    states = snapshot["states"]
    return (
        states["DOCKER-A-CREATE"]["processed"] == "CREATED_INCIDENT"
        and states["DOCKER-B-ATTACH"]["processed"] == "ATTACHED_TO_INCIDENT"
        and states["DOCKER-C-PENDING"]["processed"] == "CREATED_INCIDENT"
        and states["DOCKER-D-SHADOW"]["processed"] == "SHADOWED"
    )


def _wait_for_complete(project: str, timeout: float = 50.0) -> dict:
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = _probe(project, "snapshot")
        if _complete(latest):
            return latest
        time.sleep(1.0)
    pytest.fail(f"Docker Runtime did not converge: {latest}")


def _assert_integrity(snapshot: dict) -> None:
    states = snapshot["states"]
    assert states["DOCKER-A-CREATE"]["incident_id"] == states["DOCKER-B-ATTACH"][
        "incident_id"
    ]
    assert snapshot["shadow_exists"] is True
    assert snapshot["shadow_has_incident_owner"] is False
    assert snapshot["work_corruptions"] == 0
    assert len(snapshot["incidents"]) == 2
    assert all(item["status"] == "ASSIGNED" for item in snapshot["incidents"])
    assert all(item["workflow_audits"] == 1 for item in snapshot["incidents"])
    assert len(snapshot["work"]) == 2
    assert all(item["status"] == "COMPLETED" for item in snapshot["work"])


def test_docker_runtime_four_path_restart_and_graceful_stop() -> None:
    project = f"spec011e2e{uuid.uuid4().hex[:10]}"
    try:
        _run(project, "build", "runtime", timeout=300)
        assert _probe(project, "seed") == {"seeded": 4}
        _run(project, "up", "-d", "runtime")
        before_restart = _wait_for_complete(project)
        _assert_integrity(before_restart)

        _run(project, "stop", "-t", "30", "runtime", timeout=60)
        stopped_logs = _run(project, "logs", "--no-color", "runtime").stdout
        assert '"runtime_event":"READY"' in stopped_logs
        assert '"stage":"DRAINED"' in stopped_logs

        _run(project, "start", "runtime")
        after_restart = _wait_for_complete(project, timeout=20)
        _assert_integrity(after_restart)
        assert after_restart == before_restart
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(COMPOSE),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
