# Runtime Orchestration Launch

The host Runtime entry point is config-driven:

```powershell
python scripts/run_correlation_runtime.py --config configs/runtime_orchestration.yaml
```

The checked-in host config uses `run-until-idle`. The process opens the Event,
correlation state, Incident, Shadow, and Runtime work stores named in the config,
runs the startup recovery barrier, and only then dispatches normal intake.

The same entry point and Runtime core are used by the Compose service:

```powershell
docker compose build runtime
docker compose up -d runtime
docker compose logs runtime
```

The verified Compose service is named `runtime`. It uses
`configs/runtime_orchestration.docker.yaml`, whose loop mode is `continuous`.
There is no Runtime network port or container healthcheck. Readiness is the
Runtime startup barrier and is visible as the structured `READY` log event.

Compose mounts two named volumes:

- `runtime-events` at `/app/events` for the authoritative EventStore.
- `runtime-state` at `/app/var/runtime_orchestration` for the independent
  correlation, Incident, Shadow, and Runtime-work SQLite files.

Graceful stop and restart were verified with:

```powershell
docker compose stop -t 30 runtime
docker compose start runtime
```

On graceful stop, the Runtime emits a structured `DRAINED` event. Restart opens
the same named volumes and begins again at the startup recovery barrier. Docker
integration evidence is opt-in and remains separate from the host regression:

```powershell
$env:RUN_RUNTIME_DOCKER_E2E = "1"
python -m pytest tests/test_runtime_docker_integration.py -q -s
```

The Docker integration test uses an isolated Compose project and removes only
that project's containers and volumes after the test.
