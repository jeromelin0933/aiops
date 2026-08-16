# AIOps Incident-driven Platform

Last reviewed against current governance baseline: 2026-08-17

本 repository 目前已實作 Mock Data generation、Observability foundation、Event Detection、Event Detection Runner，以及 Scenario runtime / validation。PRD-001 v3.3 的 downstream product direction 尚包含 Alert Correlation、Incident lifecycle、Jira integration、Discord / ChatOps、RAG / LLM RCA、complete Dashboard workflow、Email fallback / escalation、human review 與 complete closed loop；這些能力尚未由 implementation evidence 證明完成，PRD-001 整體狀態仍為「執行中」。

## Current implementation

- Mock log and metrics generation
- Prometheus / Loki / Promtail / Grafana observability foundation
- Log Event Detection
- Metrics Threshold Detection
- Metrics Isolation Forest Detection
- EventDetectionRunner
- Scenario runtime and Demo / E2E integration validation controller

Not yet implemented / downstream platform scope：Alert Correlation、Incident lifecycle / Incident Manager、Jira integration、Discord / ChatOps query、RAG / LLM RCA、complete Dashboard workflow、Email fallback / escalation、human review，以及 complete closed loop。請勿把 product direction 解讀為這些模組目前可運行；RAG framework 目前仍為 future architecture / TBD。

## Current repository layout

以下項目均已於 2026-08-12 靜態確認存在：

```text
configs/scenarios.yaml

src/scenario_runtime/
src/log_generator/
src/metrics_generator/
src/event_detection/

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

Repository 不提供／不提交這些 runtime artifacts；artifact 未提交不等同 defect。若本機已有 Metrics model，可直接使用，不需每次執行都重新訓練。需建立 artifact 時，repository 確實提供 `scripts/train_log_model.py` 與 `scripts/train_metrics_model.py`；Log model 的正式行為與訓練邊界請依 SPEC-001 v2.2。本文不複製 Isolation Forest contract。

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

治理優先序為 PRD-002 → SPEC-001 / SPEC-002 / SPEC-003 / SPEC-004 → PRD-001 → DDS / README。

| Document | Role |
|---|---|
| PRD-001 v3.3 | 執行中的整體產品需求 |
| PRD-002 v1.3 | Approved；正式 Event Detection requirement |
| SPEC-001 v2.2 | Implemented；Log Event Detection contract |
| SPEC-002 v1.4 | Implemented；Metrics Threshold Detection contract |
| SPEC-003 v1.1 | Implemented；Metrics Isolation Forest Detection contract |
| SPEC-004 v1.1 | Implemented；Event Detection Runner contract |
| SPEC-005 v1.2 | Implemented — PASS WITH KNOWN LIMITATIONS；implementation / validation evidence，不是 detector authoritative contract |
| DDS-001 v1.2 | Repository-level Mock Data / Observability design reference |

PRD-002 與 SPEC-001～SPEC-004 提供正式 Event Detection contract；DDS / README 不重新定義其 schema、threshold、semantics、ownership、generator behavior 或 model parameters。

SDD、ADR-001 與 PM team instructions 是由 Google Drive 管理的 external governance documents。Repository 不建立其 mirror，本 README 也不推測其版本或內容。

## Current limitations

- Grafana datasource provisioning 與 dashboard import 尚未自動化。
- Model artifacts 是 local runtime prerequisites。
- SPEC-005 validation evidence 不證明 PRD-001 v3.3 downstream workflow / integrations 已完成，包括 Alert Correlation、Incident lifecycle、Jira、Discord / ChatOps、RAG / LLM RCA、complete Dashboard workflow、Email fallback / escalation、human review 與 complete closed loop。
- Demo / E2E validation controller 與其 validation-specific behavior 不構成 production architecture requirement。
