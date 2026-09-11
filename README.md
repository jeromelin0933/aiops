# AIOps Incident-driven Platform

Last reviewed against current governance baseline: 2026-09-11

本repository目前已實作Mock Data generation、Observability foundation、Event Detection、Event Detection Runner、Scenario runtime／validation，以及SPEC-006～010各自核准範圍內的Policy Engine、Correlation State、Incident Core、Lifecycle／Human Workflow與Shadow／Unclassified Store。PRD-003 v1.0是Alert Correlation／Incident Management Final Requirements authority，但**SPEC-006～010 individually Implemented ≠ 完整Alert Correlation Runtime完成**；SPEC-011 Runtime Orchestration與full downstream E2E仍待後續工作，PRD-001 v3.4整體狀態維持「執行中」。

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

Current PoC implementation uses Python stdlib `sqlite3` for the Correlation State Store. This is an implementation detail，不是platform、production database或future implementation requirement。

Not yet implemented / downstream platform scope：SPEC-011 Runtime Orchestration、complete Runtime／Docker E2E、RCA／RAG workflow、Jira／Discord／ChatOps／Dashboard／Email等external operational adapters／integrations、Knowledge workflow、automatic remediation，以及complete closed loop。請勿將SPEC-006～010各自完成解讀為完整Alert Correlation Runtime、完整平台或production-ready狀態；RAG framework仍為future architecture／TBD。

```text
Logs / Metrics
→ Event Detection                ✅ implemented
→ EventStore                     ✅ implemented
→ SPEC-006 Policy Engine         ✅ implemented
→ SPEC-007 Correlation State     ✅ implemented
→ SPEC-008 Incident Core         ✅ implemented
→ SPEC-009 Lifecycle / Workflow  ✅ implemented
→ SPEC-010 Shadow Store          ✅ implemented
→ SPEC-011 Runtime / E2E         pending
→ RCA / integrations             future downstream implementation
```

## Current repository layout

以下項目已於 2026-09-11 implementation baseline確認存在：

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

scripts/run_mock_runtime.py
scripts/validate_scenarios.py
scripts/train_log_model.py
scripts/train_metrics_model.py

docker/prometheus/
docker/promtail/
docker/grafana/
```

## Quick start and verified commands

在已取得 repository 並進入專案目錄後，啟動目前 Compose 定義的 observability services：

```bash
docker compose up -d
```

此命令不是「啟動完整平台／所有服務」。目前 Compose 啟動 Prometheus、Loki、Promtail 與 Grafana。Scenario runtime 是另一個程序；以下參數已由 script 原始碼靜態確認：

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

Repository 不提供／不提交這些 runtime artifacts；artifact 未提交不等同 defect。若本機已有 Metrics model，可直接使用，不需每次執行都重新訓練。需建立 artifact 時，repository 確實提供 `scripts/train_log_model.py` 與 `scripts/train_metrics_model.py`；Log model 的正式行為與訓練邊界請依 SPEC-001 v2.3。本文不複製 Isolation Forest contract。

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

治理依domain分工：PRD-002與SPEC-001～004治理Event Detection；PRD-003 v1.0 Final治理Alert Correlation／Incident Management detailed requirements；PRD-001治理overall platform direction；DDS-001治理repository-level Mock Data／Observability reference；README只提供入口與索引。SPEC-005提供implementation／validation evidence，不是detector authority。

| Document | Role |
|---|---|
| PRD-001 v3.4 | 執行中的overall platform requirement |
| PRD-002 v1.5 | Approved；Event Detection authoritative PRD |
| PRD-003 v1.0 | Final Requirements；Alert Correlation／Incident Management requirement authority；不以PRD狀態表示implementation完成 |
| SPEC-001 v2.3 | Implemented；Log Event Detection contract |
| SPEC-002 v1.4 | Implemented；Metrics Threshold Detection contract |
| SPEC-003 v1.1 | Implemented；Metrics Isolation Forest Detection contract |
| SPEC-004 v1.1 | Implemented；Event Detection Runner contract |
| SPEC-005 v1.3 | Implemented；S3 Identity Revalidation PASS；implementation／validation evidence，不是detector authority |
| SPEC-006 v1.0 | Implemented；Deterministic Alert Correlation Policy Engine contract |
| SPEC-007 v1.0 | Implemented；Correlation State Store／Pending Recovery contract |
| SPEC-008 v1.1 | Implemented；Incident Store／Incident Manager Core contract |
| SPEC-009 v1.0 | Implemented；Lifecycle／Human Workflow contract |
| SPEC-010 v1.0 | Implemented；Shadow／Unclassified Store contract |
| SPEC-011 | Pending；Runtime orchestration與full downstream E2E |
| DDS-001 v1.3 | Repository-level Mock Data／Observability reference |

PRD-002與SPEC-001～SPEC-004提供正式Event Detection contract；PRD-003 v1.0 Final提供Alert Correlation／Incident Management detailed requirements。DDS／README不重新定義其schema、threshold、semantics、ownership、generator behavior、model parameters或correlation policy。

SDD、ADR-001 與 PM team instructions 是由 Google Drive 管理的 external governance documents。Repository 不建立其 mirror，本 README 也不推測其版本或內容。

## Current limitations

- Grafana datasource provisioning 與 dashboard import 尚未自動化。
- Model artifacts 是 local runtime prerequisites。
- SPEC-006～010已依各自核准scope完成，但完整Alert Correlation Runtime尚未完成；SPEC-011 Runtime orchestration及complete Runtime／Docker E2E仍為pending。
- SPEC-006～010的個別implementation evidence不證明PRD-001 v3.4整體平台或PRD-003 v1.0所有downstream integrations已完成；RCA／RAG、Jira、Discord／ChatOps、complete Dashboard workflow、Email fallback／escalation、Knowledge workflow、automatic remediation與complete closed loop仍未完成。
- Demo / E2E validation controller 與其 validation-specific behavior 不構成 production architecture requirement。
