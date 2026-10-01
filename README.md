# AIOps Incident-driven Platform

Last reviewed against current governance baseline: 2026-10-02

本repository目前已實作Mock Data generation、Observability foundation、Event Detection、Event Detection Runner、Scenario runtime／validation、SPEC-006～011核准範圍，以及PRD-004 Candidate A～E：RCA persistence／publication、Incident Evidence Snapshot／Materiality、Knowledge Corpus／Index／Retrieval／Snapshot、LLM Generation／Validation與RCA Orchestration／Publication／Recovery。PRD-001 v3.5仍為overall platform authority，PRD-003 v1.1是Alert Correlation／Incident Management authority，PRD-004 v1.0維持`Approved`產品需求權威。

## Current implementation

- Mock log and metrics generation
- Prometheus / Loki / Promtail / Grafana observability foundation
- Log Event Detection
- Metrics Threshold Detection
- Metrics Isolation Forest Detection
- EventDetectionRunner
- Scenario runtime and Demo / E2E integration validation controller
- SPEC-006 deterministic Alert Correlation Policy Engine
- SPEC-007 Correlation State Store / Pending Recovery
- SPEC-008 Incident Store / Incident Manager Core
- SPEC-009 Lifecycle / Human Workflow
- SPEC-010 Shadow / Unclassified Store
- SPEC-011 independent Runtime Orchestration Worker、host CLI與Docker Compose runtime service
- SPEC-012 RCA Artifact persistence、Current／History／Freshness、public reads與SPEC-008 publication coordination
- SPEC-013 Incident Evidence Store、trusted-core capture、Loki／Prometheus adapters、Snapshot／Revision與Materiality
- SPEC-014 approved six-document Knowledge Corpus、governed manifest、Google embedding／Chroma adapters、build／validate／activate／retrieve CLI、Knowledge Snapshot與public provenance
- SPEC-015 Gemini provider boundary、structured generation、validation and grounding、durable Candidate-D result、Same-Try safety and replay、Candidate-A lossless Artifact projection support
- SPEC-016 Candidate-E initial／refresh／post-context orchestration、Attempt-scoped retry continuity、publication-first recovery及Runtime completion-last，於既有SPEC-011 singular Runtime內執行

Current PoC implementation uses Python stdlib `sqlite3` for the Correlation State Store及independent D2 Runtime Work Store。Candidate-E RCA continuation tables位於同一D2 database，只保存orchestration continuity，不是Event、Pending、Processed、Intent、Incident、Shadow、Workflow、Evidence、Knowledge、Generation result或RCA Artifact authority；SQLite是current implementation reality，不是platform或production database requirement。

SPEC-014 Candidate C核准範圍目前為`Implemented`。AC-014-X未執行，closure disposition為`NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE`；這不是PASS，且沒有four-member evidence。Adapter存在及default regression PASS均不能冒充live Google provider或real-RAG PASS。

Candidate D／SPEC-015與Candidate E／SPEC-016核准範圍目前為`Implemented`。SPEC-016在既有SPEC-011 singular Runtime Worker、D2、Runtime Clock、retry framework、Startup Recovery Barrier及controlled drain內執行；沒有第二套Runtime或recovery authority。仍Pending的PRD-004 scope包括Candidate F及final real-LLM／RAG quality evaluation；repository亦非Production Ready。Docker Candidate-E execution與Live Gemini均為`NOT EXECUTED`。

SPEC-014 local setup、credential boundary、commands及generated artifact規則見 [`docs/knowledge_index.md`](docs/knowledge_index.md)。Default `python -m pytest -q`不需要live Google credential；real provider validation必須explicit opt-in。

Current implemented capabilities：

```text
Logs / Metrics
→ Event Detection                ✅ implemented
→ EventStore                     ✅ implemented

SPEC-006 Policy Engine            ✅ individually implemented
SPEC-007 Correlation State        ✅ individually implemented
SPEC-008 Incident Core            ✅ individually implemented
SPEC-009 Lifecycle / Workflow     ✅ individually implemented
SPEC-010 Shadow Store             ✅ individually implemented
SPEC-011 Runtime Worker           ✅ implemented and verified
Host Runtime E2E                  ✅ verified
Docker Runtime E2E (historical correlation scope) ✅ verified
SPEC-012～015 Candidate A～D      ✅ implemented
SPEC-016 Candidate E             ✅ implemented inside SPEC-011 Runtime
Candidate-E Docker execution     ⚪ NOT EXECUTED
Candidate F / final evaluation   ⏳ pending
```

Current Runtime Logical Flow（SPEC-011 v1.2）：

```text
Event Detection
      │
      ▼
Durable EventStore
      │
      ▼
SPEC-011 Runtime Orchestration Worker
      ├── SPEC-006 Policy Engine
      ├── SPEC-007 Correlation State
      ├── SPEC-008 Incident Management
      ├── SPEC-009 Lifecycle / Workflow
      ├── SPEC-010 Shadow / Unclassified
      └── SPEC-016 Candidate-E orchestration
            └── public A / B / C / D / SPEC-008 capabilities
```

Event Detection與Runtime是independent process boundaries；Runtime不屬於`EventDetectionRunner`。Runtime從durable authoritative EventStore intake，負責coordination、startup recovery、Pending／AUTO_ASSIGN orchestration與bounded retry；各Domain保留business authority及side effects。Engine decides. State remembers. Runtime orchestrates. Domain stores own the side effects.

Startup不提供合法READY bypass：

```text
STARTING
→ authoritative discovery
→ RECOVERY
→ unresolved Intent reconciliation
→ expired Pending handling
→ retry restoration
→ AUTO_ASSIGN reconstruction / reconciliation
→ RCA publication-first classification / reconciliation
→ RCA obligation / retry / follow-up restoration
→ READY
→ normal intake
```

## Current repository layout

以下項目已於 2026-09-16 implementation baseline確認存在：

```text
configs/scenarios.yaml

src/scenario_runtime/
src/log_generator/
src/metrics_generator/
src/event_detection/
src/alert_correlation/            # SPEC-006 policy engine
src/alert_correlation/state/      # state contracts, SQLite PoC adapter,
                                  # pending state service, recovery service
src/incident_management/          # SPEC-008 incident core and SPEC-009 lifecycle workflow
src/shadow_management/            # SPEC-010 shadow / unclassified store
src/runtime_orchestration/         # SPEC-011 independent Runtime core and D2 SQLite store
src/runtime_orchestration/rca_*.py # SPEC-016 protocol inside the existing Runtime
src/rca_persistence/               # Candidate A authority
src/incident_evidence/              # Candidate B authority
src/knowledge_index/                # Candidate C authority
src/llm_generation/                 # Candidate D authority

scripts/run_mock_runtime.py
scripts/run_correlation_runtime.py
scripts/validate_scenarios.py
scripts/train_log_model.py
scripts/train_metrics_model.py

configs/runtime_orchestration.yaml
configs/runtime_orchestration.docker.yaml
configs/incident_evidence.yaml
configs/knowledge_index.yaml
configs/rca_generation.json

docker/prometheus/
docker/promtail/
docker/grafana/
docker/runtime/Dockerfile
```

## Quick start and verified commands

Host Runtime以checked-in `run-until-idle` config啟動：

```bash
python scripts/run_correlation_runtime.py --config configs/runtime_orchestration.yaml
```

Compose中的`runtime` service使用相同Runtime core及`continuous` Docker config；下列命令會同時啟動Runtime與既有observability services：

```bash
docker compose up -d
```

Runtime service使用`runtime-events`、`runtime-state`與`runtime-knowledge` named volumes，支援controlled drain、graceful stop及restart continuity。其READY是startup recovery barrier完成後的structured telemetry event，不是network healthcheck。Historical SPEC-011 correlation Runtime Docker E2E已獨立驗證；Candidate-E Docker execution為`NOT EXECUTED`。詳細啟動與opt-in Docker E2E命令見`docs/runtime_orchestration.md`。此Compose topology仍不是「完整平台／所有服務」或production-ready deployment。

Scenario runtime 是另一個程序；以下參數已由 script 原始碼靜態確認：

```bash
python scripts/run_mock_runtime.py --config configs/scenarios.yaml
python scripts/run_mock_runtime.py --config configs/scenarios.yaml --scenario S2 --exit-after-recovery
```

`scripts/validate_scenarios.py` 是 Demo / E2E integration validation controller，不是 production master runtime。其 CLI 可選擇單一 scenario 或明確執行全部 scenarios：

```bash
python scripts/validate_scenarios.py --scenario S2
python scripts/validate_scenarios.py --all
```

Validation 會依賴所需 local services、configuration 與 model artifacts；執行前應先確認 prerequisites。本段僅記錄已存在的 CLI，不把 validation-specific behavior 升格為 production requirement。

## Model artifact prerequisites

執行 Event Detection / E2E 前，應確認本機存在所需 runtime artifacts：

```text
models/log_isolation_forest.pkl
models/metrics_isolation_forest.pkl
```

Repository 不提供／不提交這些 runtime artifacts；artifact 未提交不等同 defect。若本機已有 Metrics model，可直接使用，不需每次執行都重新訓練。需建立 artifact 時，repository 確實提供 `scripts/train_log_model.py` 與 `scripts/train_metrics_model.py`；Log model 的正式行為與訓練邊界請依 SPEC-001 v2.4。本文不複製 Isolation Forest contract。

## Observability deployment notes

### Metrics

```text
Metrics exporter on host: :8000
Prometheus container target: host.docker.internal:8000
```

四個現行 Gauge 不使用 dynamic scenario、service 或 detector labels；`api_requests_per_sec` 目前是 single series。這是 implementation / deployment reality，不是 detector normative contract。

### Logs

```text
Host log:           logs/aiops.json.log
Compose mount:      ./logs:/var/log/aiops
Promtail reads:     /var/log/aiops/*.log
Loki push endpoint: http://loki:3100/loki/api/v1/push
```

Host-relative path 與 container path 如上分列；Compose mount 將 host logs 提供給 Promtail。

### Grafana

Grafana container 可由 Compose 啟動並映射至 `http://localhost:3000`，但 service started 不等於 dashboard ready。目前 Prometheus datasource 與 Loki datasource 需人工建立，`docker/grafana/dashboard.json` 需人工 import 並選擇 datasource。Repository 尚未提供完整 datasource / dashboard auto provisioning。

目前 Compose / implementation 不提供 `http://localhost:8080` AIOps Dashboard。

## Runtime artifacts and Git hygiene

下列 runtime / local artifacts 不應提交：

```text
models/*.pkl
events/
logs/*.log
logs/*.txt
reports/spec005/
.pytest_cache/
.pytest-runtime-basetemp/
__pycache__/
.venv/
```

上述項目以及其他已由 repository ignore policy 明確管理的 runtime / temporary artifacts 應留在本機；不泛稱所有名稱含 `temp` 的檔案都會被 ignore。

## Authoritative documents / governance

治理依domain分工：PRD-002與SPEC-001～004治理Event Detection；PRD-003 v1.1治理Alert Correlation／Incident Management；PRD-004 v1.0治理RCA產品需求，SPEC-012～016治理Candidate A～E semantics；SPEC-011維持唯一Runtime orchestration、scheduling、Clock／retry與startup recovery authority，SPEC-016在該framework內實作Candidate-E protocol。PRD-001 v3.5治理overall platform direction；DDS-001是supporting repository-level reference；README只提供入口與索引。

| Document | Role |
|---|---|
| PRD-001 v3.5 | 執行中的overall platform requirement |
| PRD-002 v1.5 | Approved；Event Detection authoritative PRD |
| PRD-003 v1.1 | Final Requirements；Alert Correlation／Incident Management requirement authority；不以PRD狀態表示implementation完成 |
| PRD-004 v1.0 | Approved；Evidence-Grounded RCA product requirement authority；產品版本未因implementation closure升版 |
| SPEC-001 v2.4 | Implemented；Log Event Detection contract；包含authoritative Event enumeration/read-integrity capability |
| SPEC-002 v1.4 | Implemented；Metrics Threshold Detection contract |
| SPEC-003 v1.1 | Implemented；Metrics Isolation Forest Detection contract |
| SPEC-004 v1.1 | Implemented；Event Detection Runner contract |
| SPEC-005 v1.3 | Implemented；S3 Identity Revalidation PASS；implementation／validation evidence，不是detector authority |
| SPEC-006 v1.0 | Implemented；Deterministic Alert Correlation Policy Engine contract |
| SPEC-007 v1.0 | Implemented；Correlation State Store／Pending Recovery contract |
| SPEC-008 v1.3 | Implemented；Incident Core及additive RCA relationship／publication integration |
| SPEC-009 v1.0 | Implemented；Lifecycle／Human Workflow contract |
| SPEC-010 v1.0 | Implemented；Shadow／Unclassified Store contract |
| SPEC-011 v1.2 | Implemented；Existing Scope and RCA Additive Runtime Boundary；singular Runtime authority |
| SPEC-012 v1.1 | Implemented；RCA Artifact／Persistence及publication-side truth |
| SPEC-013 v1.1 | Implemented；Incident Evidence Collection／Snapshot／Materiality |
| SPEC-014 v1.2 | Implemented；Knowledge Corpus／Index／Retrieval／Snapshot；AC-014-X PM-directed skip，不是PASS |
| SPEC-015 v1.1 | Implemented；LLM Generation／Validation、durable result、Same-Try safety／replay；Live Gemini未執行 |
| SPEC-016 v1.1 | Implemented；Candidate-E RCA Orchestration／Publication／Recovery；Docker Candidate-E與Live Gemini未執行 |
| DDS-001 v1.8 | Supporting repository-level implementation architecture reference |

PRD-002與SPEC-001～SPEC-004提供正式Event Detection contract；PRD-003 v1.1提供Alert Correlation／Incident Management requirements；PRD-004 v1.0與SPEC-012～016提供RCA Candidate A～E authority。SPEC-011仍是singular Runtime authority。DDS／README不建立新的normative authority。

SDD、ADR-001 與 PM team instructions 是由 Google Drive 管理的 external governance documents。Repository 不建立其 mirror，本 README 也不推測其版本或內容。

## Current limitations

- Grafana datasource provisioning 與 dashboard import 尚未自動化。
- Model artifacts 是 local runtime prerequisites。
- SPEC-006～011已依各自核准scope完成；SPEC-011的Host與Docker Runtime E2E已驗證，但目前Runtime仍是single-process／single-node PoC，沒有HA、distributed coordination、Kafka／broker、exactly-once transport infrastructure或cross-store 2PC。
- Candidate A～E已實作；Candidate F、final real-LLM／RAG quality evaluation、Docker Candidate-E execution、Live Gemini、operational adapters、Jira、Discord／ChatOps、complete Dashboard workflow、Email fallback／escalation、automatic remediation、production hardening與complete closed loop仍未完成或未執行；因此不得宣稱Production Ready。
- Demo / E2E validation controller 與其 validation-specific behavior 不構成 production architecture requirement。
