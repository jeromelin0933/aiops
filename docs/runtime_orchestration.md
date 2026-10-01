# Runtime Orchestration Launch

The host Runtime entry point is config-driven:

```powershell
python scripts/run_correlation_runtime.py --config configs/runtime_orchestration.yaml
```

The checked-in host config uses `run-until-idle`. The process opens the Event,
correlation state, Incident, Shadow, and Runtime work stores named in the config.
When the `rca:` block is present it also composes Candidate A/B/C/D and SPEC-008
public semantic capabilities in the same process. It runs the existing startup
recovery barrier, including publication-first RCA reconciliation, and only then
dispatches normal intake. SPEC-011 remains the singular Runtime authority;
SPEC-016 does not introduce a second worker, scheduler, Runtime Clock, retry
authority, or recovery framework.

The current `rca:` configuration surface contains repo-relative Candidate-A,
Candidate-B, and Candidate-D store paths; Knowledge, Generation, and Evidence
config references; and local non-secret Loki and Prometheus endpoints:

```yaml
rca:
  a_store_path: var/runtime_orchestration/rca.sqlite3
  b_store_path: var/runtime_orchestration/evidence.sqlite3
  d_store_path: var/runtime_orchestration/generation.sqlite3
  knowledge_config_path: configs/knowledge_index.yaml
  generation_config_path: configs/rca_generation.json
  evidence_config_path: configs/incident_evidence.yaml
  loki_endpoint: http://localhost:3100/loki/api/v1/query_range
  prometheus_endpoint: http://localhost:9090/api/v1/query_range
```

Docker uses the same keys with service-local Loki and Prometheus hostnames.
Paths and endpoints are validated as bounded, local, and non-secret. Provider
clients are acquired lazily at the actual embedding or generation invocation;
credentials are not stored in Runtime work or configuration documentation.

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

Compose mounts three named volumes:

- `runtime-events` at `/app/events` for the authoritative EventStore.
- `runtime-state` at `/app/var/runtime_orchestration` for the independent
  correlation, Incident, Shadow, Runtime-work, Candidate-A, Candidate-B, and
  Candidate-D SQLite files.
- `runtime-knowledge` at `/app/var/knowledge_index` for Candidate-C local
  Knowledge index artifacts.

Candidate-E continuation uses RCA-specific records in the existing D2 Runtime
work database. Those records retain only orchestration continuity: stable root
and operation references, Attempt-bound retry consumption, singleton follow-up
frontier, absolute wake continuity, and publication continuation. Candidate A,
B, C, D, and SPEC-008 remain authoritative for their own business facts.

During startup, RCA recovery classifies publication subjects first from
SPEC-008 receipts/relationships and Candidate-A publication facts. It then
classifies validated D results, typed failures, exhausted or pending retries,
follow-up/post-context work, STALE Current, and initial Incident obligations.
Unreadable or contradictory required authority fails the affected RCA
capability closed; it is not treated as absence and does not trigger guessing or
destructive repair.

Normal cycles dispatch RCA initial, Attempt, and singleton follow-up work only
after startup classification. Same-Try and Next-Try work share the existing
Attempt-bound durable retry framework and Runtime Clock. Post-context work
retains its original absolute wake, publication reconciliation precedes renewed
generation for the same subject, and Runtime completion occurs only after
authoritative publication coherence and follow-up frontier resolution.

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

Historical SPEC-011 correlation Runtime Docker evidence remains PASS. The
Candidate-E Docker path introduced at accepted implementation commit
`eff7eb067cc09974c7679050f5136a7cfdffadaf` was **NOT EXECUTED**. Live Gemini
was also **NOT EXECUTED**. Candidate F and final real-LLM/RAG quality evaluation
remain Pending; Production Ready remains `NO`.
