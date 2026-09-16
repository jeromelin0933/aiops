# SPEC-005：Mock Data Generator Validation and Scenario Alignment

## Software Design Specification v1.3

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-005 |
| Document Name | Mock Data Generator Validation and Scenario Alignment |
| 中文名稱 | 模擬資料產生器驗證與六大情境對齊 |
| Version | 1.3 |
| Status | Implemented |
| Date | 2026-08-29 |
| Author | 林子豪（PM） |
| Assignee | 夜雨 |
| Branch Metadata | `feature/mock-data-validation` |
| Related PRD | PRD-001 v3.2、PRD-002 v1.4 |
| Related DDS | DDS-001 v1.1 |
| Related SPEC | SPEC-001 v2.3、SPEC-002 v1.4、SPEC-003 v1.1、SPEC-004 v1.1 |
| Implements | SPEC-001 Phase 4、SPEC-003 Deferred Integration、SPEC-004 Deferred Follow-up |
| Target | Developer／AI Coding Agent |

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-05 | 建立 Generator Audit、Scenario Runtime、Log／Metrics Generator 對齊、Prometheus／Grafana 驗證、SPEC-001～004 真實整合與六大情境 E2E Gate；明確定義持續 Baseline、有限 Scenario Injection、Recovery、同 Runtime 重複觸發、受控隨機性、Docker 修改核准與 Model Artifact 邊界。 |
| 1.1 | 2026-08-05 | 對齊 PRD-002 v1.2；確認 S2 Latency 與 S3 Memory Threshold 邊界為大於或等於，並將上游文字修訂狀態更新為已完成。 |
| 1.2 | 2026-08-12 | 記錄 Phase 5／6 implementation completion evidence、Phase 7 final read-only audit、Log IForest calibration、runner priming、EventStore evidence isolation 與已知限制；不變更既有 normative contract。 |
| 1.3 | 2026-08-29 | S3 identity contract follow-up closure：完成 Validator readiness alignment、S3 OOM-origin identity Runtime revalidation、S1～S6 E2E regression 與 final automated regression。既有 2026-08-12 Phase 6 S3 PASS 歷史紀錄維持不變。 |

> 本文件是 SPEC-005 的正式工程契約。Git 操作、Codex 安裝方式、虛擬環境建立指令及完整 AI Coding Agent Prompt 不屬於本 SPEC，由 PM 透過獨立工作指示提供。

---

# 0. 文件目的、規格優先序與核心決策

## 0.1 文件目的

本文件定義 **Mock Data Generator Validation and Scenario Alignment** 的完整實作規格。

DDS-001 已建立：

- Log Generator。
- Metrics Generator。
- Prometheus。
- Loki。
- Promtail。
- Grafana Dashboard。
- Docker Compose。

SPEC-001～004 已建立：

- Log Event Detection。
- Metrics Threshold Detection。
- Metrics Isolation Forest Detection。
- Event Detection Runner。

但既有 Generator 尚未以目前最新的 PRD／SPEC 契約完成正式驗證。

本 SPEC 的目的不是重新設計 Detector，而是完成：

```text
既有 Generator Read-only Audit
→ Scenario Gap Matrix
→ Log／Metrics Generator Patch
→ 持續式 Mock Data Runtime
→ Prometheus／Loki／Grafana Validation
→ SPEC-001～004 真實整合
→ S1～S6 End-to-End Gate
```

完成後，系統應能在真實本機環境中持續產生正常資料，依使用者指令注入有限時間的六大情境，恢復正常資料，並由 Event Detection Runner 建立符合 PRD-002 的 Event。

## 0.2 上游文件使用原則

DDS-001 是較早期的 Mock Data 與 Observability Foundation 設計紀錄，可用於理解既有架構、元件與原始意圖；若 DDS-001 與後續文件不一致，不得單獨以 DDS-001 決定目前正式行為。

本 SPEC 的上游依據優先順序如下：

1. PRD-002 v1.3 的六大情境、Event Type、Event Schema 與 NFR。
2. SPEC-001～004 的正式 Detector／Runner Contract。
3. PRD-001 v3.2 的產品定位、Demo 範圍與資安邊界。
4. 本 SPEC-005 的 Generator Runtime、Validation 與 E2E 實作契約。
5. DDS-001 v1.1 的既有資料來源與 Observability Foundation。
6. 現有 Generator 程式碼與舊操作流程。

若文件、程式碼或測試無法同時滿足：

```text
停止擴大修改
→ 保存可重現證據
→ 列出衝突文件與實際行為
→ 回報 PM
```

不得自行放寬 PRD-002、修改 Detector Algorithm、降低驗收標準或新增正式 Event Type。

## 0.3 核心決策

1. SPEC-005 只驗證與補強 Mock Data／Scenario／Observability／E2E，不重新實作 SPEC-001～004。
2. Generator 必須對齊 PRD-002 與 SPEC-001～004；不得為了配合不完整 Generator 回頭修改穩定 Detection Contract。
3. Generator 只負責產生輸入資料，不得直接輸出 Event、Alert、Incident 或偵測答案。
4. 正式 Scenario Runtime 生命週期固定為：

```text
啟動
→ 持續正常 Baseline
→ 注入有限時間 Scenario
→ Recovery
→ 回到正常 Baseline
→ 持續運作直到使用者關閉
```

5. 同一個 Runtime 內可依序觸發不同 Scenario，不需要每次重啟 Log／Metrics Generator。
6. Scenario Injection 期間不得接受另一個 Scenario；Recovery 完成前不得開始下一個 Scenario。
7. 正常 Baseline 可有固定 Seed 的有限數值抖動，但不得預設產生不可控制的隨機 Error。
8. v1.0 的背景 Error Injection 預設關閉；若既有 Generator 會隨機產生 Error，必須改為可停用且固定 Seed。
9. S2／S3 Metrics 異常必須維持足夠時間，跨過至少一次 Prometheus Scrape 與一次 Metrics Detection Poll，並保留安全緩衝。
10. S6 必須先建立足夠的正常 QPS Samples，再注入至少達近期 Baseline 3.0 倍的 QPS Spike。
11. `db_pool_active_connections` 僅收集與視覺化，不建立 Event，也不是六大情境通過條件。
12. Event Detection 的統一 Runtime 已由 SPEC-004 `EventDetectionRunner` 提供；SPEC-005 不建立完整 AIOps 平台總開關。
13. Mock Data Runtime 與 Event Detection Runtime 是兩個不同責任：

```text
MockDataRuntime              → 產生 Logs／Metrics
EventDetectionRunner         → 執行三條 Detection Pipeline
Alert Correlation 之後模組   → 不屬於本 SPEC
```

14. `scripts/validate_scenarios.py` 是驗收控制器，不是 Production Application Runner。
15. Docker／Prometheus／Loki／Grafana 預設只使用與驗證，不修改設定。
16. 只有 Read-only Audit 證明基礎設施設定阻擋正式 Contract，且取得 PM 明確核准後，才可修改 Docker／Observability 設定。
17. Model Artifact 不屬於 Unit Test 前置條件；真實 Event Runner／E2E 才需要本機模型。
18. `models/*.pkl`、`events/*.jsonl`、`logs/*.log` 與驗收輸出皆為 Runtime Artifact，不得提交 Git。
19. 必要 Unit／Contract Tests 不依賴 Docker、Prometheus、正式 Model、外部網路或真實長時間等待。
20. 真實 Prometheus／Grafana／Event Runner E2E 為獨立 Integration Gate，不得綁入一般 `python -m pytest -q`。

## 0.4 與 SPEC-001～004 的關係

### SPEC-001

SPEC-001 已完成 Hybrid Log Event Detection，並將真實 Generator 驗證延後至 Phase 4。

本 SPEC 應讓 Generator Output 對齊：

- Log Parser。
- Feature Extractor。
- Window Aggregator。
- Isolation Forest Input。
- Rule-based Event Classification。

不得修改 Log Model Feature、Classifier 優先順序、EventBuilder 或 EventStore。

### SPEC-002

SPEC-002 正式處理：

| Metric | Trigger | Event Type |
|---|---:|---|
| `api_p95_latency_ms` | `>= 3000.0` | `high_latency_detected` |
| `system_memory_usage_pct` | `>= 90.0` | `high_memory_detected` |

Generator 必須讓異常值維持足夠久，使 Prometheus Instant Query 與 15 秒 Detection Poll 有機會讀取。

### SPEC-003

SPEC-003 v1.1 只處理：

```text
api_requests_per_sec
```

正式輸出：

- `request_spike_detected`。
- `general_metrics_anomaly`。

Generator 不得將 Memory、Latency 或 DB Pool 餵入 Metrics IForest，也不得增加 Label 造成多 Series Response。

### SPEC-004

SPEC-004 已提供：

- `EventDetectionRunner.run_once()`。
- `EventDetectionRunner.run_due_once()`。
- `EventDetectionRunner.start()`。
- `EventDetectionRunner.stop()`。
- Startup Fail Fast。
- Runtime Error Isolation。
- Log → Threshold → IForest 固定順序。

SPEC-005 E2E 優先使用 `run_once()` 執行受控驗收，不修改 Scheduler。

## 0.5 已知上游對齊事項與文件修訂邊界

本 SPEC 不自行修改 PRD、DDS、SPEC、ADR 或 SDD。

### 0.5.1 PRD-002 Threshold 文字對齊已完成

PRD-002 已於 v1.2 完成 S2／S3 Threshold 邊界修訂，正式文字為：

```text
api_p95_latency_ms 達到或超過 3000ms
system_memory_usage_pct 達到或超過 90%
```

上述描述與 SPEC-002 v1.4 的正式比較契約一致：

```text
current_value >= threshold
```

實作者應以 PRD-002 v1.3 與 SPEC-002 v1.4 為準，不得將比較條件改回單純大於。

### 0.5.2 不阻擋 SPEC-005、但不得由實作者修改的 Detector 差異

現有 SPEC-001 的已知分類條件部分比 PRD-002 Demo Input 門檻寬鬆，例如：

- S4 EventBuilder 主要依 `external_service` 判斷；Generator 仍必須依 PRD-002 產生 `status_code >= 500`。
- S5 EventBuilder 可在較少服務數時分類；Generator 仍必須產生至少 5 個不同 `service_name`。
- S6 EventBuilder 在模型異常 Gate 後依 429／Target 判斷；Generator 仍必須產生至少 20 筆、正式 Demo 預設 55 筆 429。

上述差異不允許夜雨修改 Detector。SPEC-005 以較嚴格的 PRD-002 Generator Input 完成驗收；若差異造成 E2E 語意問題，保存證據並回報 PM，另立 SPEC-001 Revision／Integration Fix。

### 0.5.3 Audit 發現新衝突時

若 Audit 發現：

- 上游正式條件互相矛盾。
- Generator 已符合上游契約但 Detector 仍無法通過。
- 必須改 Event Schema／Event Type／Threshold／Model Feature 才能通過。

必須停止並回報 PM，由 PM 先完成上游文件 Revision 或另立 Integration Fix。

---

# 1. 系統定位與模組邊界

## 1.1 整體架構位置

```text
                         ┌──────────────────────────────┐
                         │ MockDataRuntime（SPEC-005）  │
                         │                              │
使用者選擇 S1～S6 ─────▶│ Scenario State Machine       │
                         │        │             │       │
                         │        ▼             ▼       │
                         │ Log Generator   Metrics Gen. │
                         └────────┬─────────────┬───────┘
                                  │             │
                                  ▼             ▼
                       logs/aiops.json.log   Exporter :8000
                                  │             │
                                  ▼             ▼
                           Loki／Promtail   Prometheus
                                  │             │
                                  └──────┬──────┘
                                         ▼
                              EventDetectionRunner
                              Log → Threshold → IForest
                                         │
                                         ▼
                              events/event_store.jsonl
                                         │
                                         ▼
                         Alert Correlation（下一階段）
```

## 1.2 本模組負責

- 盤點既有 Log／Metrics Generator 實際行為。
- 建立 Scenario Gap Matrix。
- 建立統一 Scenario Runtime State Machine。
- 提供持續正常 Baseline。
- 提供 S1～S6 有限時間異常注入。
- 提供 Recovery 與重新進入 Baseline。
- 允許同一 Runtime 依序觸發多個 Scenario。
- 讓 Log Generator 對齊正式 Log Schema 與 Scenario Pattern。
- 讓 Metrics Generator 對齊四個正式 Metric 名稱與數值型別。
- 提供固定 Random Seed 與可重現行為。
- 禁止不可控制的背景 Error。
- 驗證 Prometheus Exporter、Target、Instant Query 與 Range Query。
- 驗證 Loki／Promtail 可讀取新 Log。
- 驗證 Grafana 可看到現有 Logs／Metrics。
- 透過 SPEC-004 Event Runner 完成六大情境 E2E。
- 驗證 EventStore 新增 Event 的 Type、Source、Method、Schema 與關鍵欄位。
- 提供必要 Unit／Contract Tests。
- 提供獨立 Manual／Optional Integration Validation Script。
- 保持既有 Generator 直接啟動方式相容，或提供清楚的 Migration Entry Point。

## 1.3 本模組不負責

- 修改 Log Isolation Forest Feature 或 Model Parameter。
- 修改 Metrics Threshold 數值或比較方式。
- 修改 Metrics IForest Feature、Model、Classifier 或 Metadata Contract。
- 修改 Event Runner 排程、錯誤策略或 Pipeline 順序。
- 新增、刪除或重新命名 Event Schema 欄位。
- 新增 PRD-002 未定義的正式 Event Type。
- Cross-Pipeline Deduplication。
- Alert Correlation。
- Incident Manager。
- LLM／RAG／RCA。
- Email Notification。
- 新版正式 Dashboard Business Logic。
- Production Deployment。
- Kafka、正式 Message Queue、HA 或 Distributed Runtime。
- 啟動或關閉整個 AIOps 平台的 Production Orchestrator。
- 保證捕捉所有短於 Scrape／Polling Interval 的瞬時 Spike。
- 產生真實金融個資、真實公司內部資訊或真實憑證。

## 1.4 MockDataRuntime 與 EventDetectionRunner 邊界

`MockDataRuntime`：

- 只產生 Logs／Metrics。
- 管理 Scenario Phase。
- 不 import EventBuilder。
- 不寫 EventStore。
- 不判斷 Event Type。
- 不知道 Detection 是否成功。

`EventDetectionRunner`：

- 只執行三條 Detector。
- 不控制 Generator Scenario。
- 不修改 Generator State。
- 不啟動 Docker。
- 不建立 Incident。

`validate_scenarios.py`：

- 可同時呼叫 MockDataRuntime 與 EventDetectionRunner，作為測試控制器。
- 不得成為 Detector 的正式 Runtime Dependency。
- 不得把 `scenario_id` 注入 Event Schema。

Implementation Evidence：`validate_scenarios.py` 是 Demo／E2E integration controller，不是 Detector、production master runtime 或 EventStore writer；不自行建立 Event，亦不使用 `EventBuilder` 建立 Event。Event ownership 維持在 Detector／`EventDetectionRunner` pipeline。

---

# 2. 前置條件與執行環境

## 2.1 Branch 與 Baseline

正式實作分支：

```text
feature/mock-data-validation
```

開始修改前，該分支內容必須源自最新 `develop`，並包含：

- SPEC-001 完整成果。
- SPEC-002 完整成果。
- SPEC-003 完整成果。
- SPEC-004 完整成果。
- 全專案 Baseline Regression Tests 通過。

Git 建立、切換、合併、提交與推送不屬於本 SPEC，由 PM／成員人工操作。

## 2.2 Python Dependencies

本 SPEC 優先重用既有：

- Python Standard Library。
- `PyYAML`。
- `prometheus-client`。
- `requests`。
- 專案既有依賴。

不得自行修改：

```text
requirements.txt
requirements-dev.txt
```

若發現缺少必要依賴：

```text
停止新增第三方套件
→ 說明缺少的能力
→ 回報 PM
```

## 2.3 Docker／Observability Prerequisites

真實 Integration Validation 前，既有 DDS-001 環境應可啟動：

- Prometheus。
- Loki。
- Promtail。
- Grafana。

本 SPEC 不新增 Docker Container。

## 2.4 Model Artifact Prerequisites

以下工作不要求正式 Model Artifact：

- Generator Audit。
- Config Tests。
- Generator Unit Tests。
- Scenario State Machine Tests。
- Schema／Contract Tests。
- Mock Prometheus Tests。

以下工作需要本機模型：

- 真實 `EventDetectionRunner` Startup。
- 六大情境 E2E。
- Manual Integration Validation。

必要路徑：

```text
models/log_isolation_forest.pkl
models/metrics_isolation_forest.pkl
```

模型不存在時不得由 SPEC-005 Runtime 無聲自動訓練，也不得跳過 Enabled Pipeline。

應先使用既有 Training Script 建立模型。模型檔不得提交版本控制。

Implementation Evidence：上述兩個 prerequisite path 均已驗證；Controller 在缺少任一模型時 fail fast，不自動 train，也不 disable detector pipeline。模型 artifact 是執行 prerequisite，不是 SPEC-005 source deliverable。Log training artifact 與 threshold calibration 定義參照 SPEC-001 v2.2。

## 2.5 Runtime Artifact

可能產生：

```text
logs/aiops.json.log
events/event_store.jsonl
models/*.pkl
reports/spec005/*
```

上述皆不屬於正式 Source Deliverable，不得加入 Git Staging。

---

# 3. Implementation Phases

## 3.1 Phase 1：Read-only Current State Audit

本階段不得修改程式碼。

必須確認：

- 實際 Generator 檔案與入口。
- Log Generator 是否持續執行。
- Metrics Exporter 是否持續更新。
- S1～S6 目前如何選擇。
- Scenario 結束後是否恢復正常。
- 是否需重啟才可選下一個 Scenario。
- 是否存在不可控制 Random Error。
- Log 實際 Schema。
- 四個 Metric 是否存在且為單一 Series。
- Exporter Port。
- Prometheus Scrape Interval。
- Loki／Promtail Log Path。
- Grafana Data Source 與 Query。
- 現有 Scenario 與正式 PRD／SPEC 的 Gap。
- 既有 Unit Tests 與 Runtime Artifacts。

Audit 必須輸出 Scenario Gap Matrix，至少包含：

| Scenario | Existing Log | Existing Metrics | Required Contract | Gap | Proposed Patch |
|---|---|---|---|---|---|
| S1 |  |  |  |  |  |
| S2 |  |  |  |  |  |
| S3 |  |  |  |  |  |
| S4 |  |  |  |  |  |
| S5 |  |  |  |  |  |
| S6 |  |  |  |  |  |

若需要修改 Conditional Scope，Phase 1 結束後必須先取得 PM 核准。

## 3.2 Phase 2：Scenario Runtime Foundation

本階段建立：

- `configs/scenarios.yaml`。
- Scenario Config Loader／Validator。
- Scenario ID 與 Phase Data Structure。
- MockDataRuntime State Machine。
- Baseline／Injection／Recovery Lifecycle。
- Manual Trigger Contract。
- Fixed Seed／Controlled Jitter。
- Background Error Default-off Contract。
- 對應 Unit Tests。

本階段不需要 Docker 或 Model。

## 3.3 Phase 3：Generator Alignment

本階段修改：

- Log Generator。
- Metrics Generator。
- 舊 Entry Point Adapter。
- 統一 Mock Runtime CLI。
- S1～S6 Pattern。
- Recovery 行為。
- 對應 Generator Tests。

不得修改 `src/event_detection/`。

## 3.4 Phase 4：Contract Tests 與 Regression

本階段完成：

- Log Schema Contract Tests。
- Metrics Name／Type／Range Tests。
- Scenario Isolation Tests。
- Fixed Seed Tests。
- Repeated Trigger Tests。
- Full Regression。

必要測試不得依賴 Docker、Prometheus、正式 Model 或實際等待 60 秒。

## 3.5 Phase 5：Observability Integration

本階段才使用既有 Docker／Prometheus／Loki／Grafana：

- Exporter Endpoint。
- Prometheus Target。
- Instant Query。
- Range Query。
- Loki Log Ingestion。
- Grafana Existing Dashboard。

若失敗原因是基礎設施設定，先回報 PM，不直接修改 Docker。

## 3.6 Phase 6：Six Scenario E2E Gate

本階段要求：

- 本機 Model Artifact 已存在。
- EventDetectionRunner 可正常 Startup。
- 六大 Scenario 依序驗收。
- 只讀取本輪新增的 EventStore Evidence。
- Recovery 完成後才進入下一個 Scenario。
- 產生 ScenarioValidationResult。

### 3.7 Phase 5／6 Implementation Result

以下是已完成執行的 completion evidence，不新增或改寫永久 Requirement／Contract：

| Phase | Result |
|---|---|
| Phase 5 | PASS |
| Phase 6 — S1 | PASS |
| Phase 6 — S2 | PASS |
| Phase 6 — S3 | PASS |
| Phase 6 — S4 | PASS |
| Phase 6 — S5 | PASS |
| Phase 6 — S6 | PASS |
| E2E Exit Code | 0 |

上述 `Phase 6 — S3 PASS` 發生於本次 S3 identity contract strengthening 之前，因此只能證明當時的 S3 Event Type／Severity／Metrics acceptance；不構成 `oom_crash_detected.service_name == actual OOM-origin service` 的新驗證證據。新 identity contract 必須待 SPEC-001 v2.3 implementation 完成後另行 revalidate；該follow-up revalidation現已完成，closure evidence見13.8，且不回溯改寫本歷史結果。

### 3.8 Phase 7：Final Read-only Audit

Phase 7 final read-only audit 結果為 `PASS WITH KNOWN LIMITATIONS`，Blocking Defects 為 `0`。

此結果表示 engineering completion；external／business approval 是不同治理狀態，不由本 engineering evidence 自動宣告。Final engineering state 為：

```text
Implemented — PASS WITH KNOWN LIMITATIONS
```

---

# 4. File Structure 與模組設計

## 4.1 Required／Recommended Structure

```text
專案根目錄/
│
├── configs/
│   └── scenarios.yaml
│
├── src/
│   ├── scenario_runtime/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── schema.py
│   │   └── runtime.py
│   │
│   ├── log_generator/
│   │   └── log_generator.py
│   │
│   └── metrics_generator/
│       └── metrics_generator.py
│
├── scripts/
│   ├── run_mock_runtime.py
│   └── validate_scenarios.py
│
└── tests/
    ├── test_scenario_runtime.py
    ├── test_log_generator.py
    ├── test_metrics_generator.py
    ├── test_scenario_alignment.py
    └── fixtures/
        └── scenarios/
```

若 Audit 證明既有檔案名稱或目錄不同，可以在不擴大責任邊界下適配；不得自行建立平行的第二套 Generator 而保留舊實作無人使用。

## 4.2 模組職責

### `scenario_runtime/config.py`

- 讀取 UTF-8 YAML。
- 驗證 Config Root 與必要欄位。
- 驗證 Scenario ID。
- 驗證 Duration／Count／Range。
- 驗證 Baseline 不跨 Threshold。
- 驗證 Scenario 值滿足正式 Contract。
- 驗證 Recovery 足以隔離 Window／Cooldown。
- 不修改其他 Config。

### `scenario_runtime/schema.py`

定義：

- `ScenarioId`。
- `ScenarioPhase`。
- `ScenarioCommand`。
- `ScenarioRuntimeSnapshot`。
- `ScenarioValidationResult`。

### `scenario_runtime/runtime.py`

- 持有目前 Phase。
- 持有目前 Scenario。
- 管理 Injection 起訖時間。
- 管理 Recovery 起訖時間。
- 呼叫 Log／Metrics Generator Adapter。
- 拒絕不合法 Trigger。
- 支援 Graceful Stop。
- 不做 Event Detection。

### `run_mock_runtime.py`

- 啟動持續 Mock Data Runtime。
- 提供人類操作介面。
- 可選擇 S1～S6。
- 可查看目前狀態。
- 可正常停止。
- 不啟動 Docker。
- 不啟動 EventDetectionRunner。

### `validate_scenarios.py`

- 檢查 E2E 前置條件。
- 可觸發指定 Scenario。
- 可呼叫 EventDetectionRunner `run_once()`。
- 記錄 EventStore Offset。
- 只驗證本輪新增 Event。
- 輸出 ScenarioValidationResult。
- 不修改 Detector Contract。

---

# 5. Config Specification

## 5.1 Config Path

新增：

```text
configs/scenarios.yaml
```

## 5.2 建議正式內容

```yaml
version: "1.0"

runtime:
  tick_seconds: 1.0
  random_seed: 42
  recovery_seconds: 60
  allow_trigger_during_injection: false
  allow_trigger_during_recovery: false

log:
  output_path: "logs/aiops.json.log"
  baseline_interval_seconds: 1.0
  baseline_records_per_tick: 1

metrics:
  exporter_port: 8000
  update_interval_seconds: 1.0
  baseline:
    system_memory_usage_pct: 55.0
    api_p95_latency_ms: 250.0
    api_requests_per_sec: 10.0
    db_pool_active_connections: 8.0
  jitter:
    enabled: true
    memory_max_delta: 2.0
    latency_max_delta: 50.0
    qps_max_delta: 1.5
    db_pool_max_delta: 2.0

background_errors:
  enabled: false

scenarios:
  S1:
    duration_seconds: 5
    unauthorized_count: 50
    source_ip: "192.0.2.10"
    user_id: "user_mock_001"

  S2:
    duration_seconds: 45
    trace_id_prefix: "trace-s2"
    api_p95_latency_ms: 4500.0
    downstream_service: "core-db"

  S3:
    duration_seconds: 45
    system_memory_usage_pct: 95.0

  S4:
    duration_seconds: 10
    external_service: "external-bank-gateway"
    status_code: 500

  S5:
    duration_seconds: 10
    affected_service_count: 5
    downstream_service: "core-db"
    error_type: "ConnectionRefused"

  S6:
    duration_seconds: 45
    rate_limit_log_count: 55
    target_service: "sms-gateway"
    qps_spike_multiplier: 4.0

validation:
  prometheus_scrape_interval_seconds: 15
  metrics_detection_poll_seconds: 15
  safety_margin_seconds: 15
  require_qps_warmup: true
```

實作者可在 Audit 後增加必要欄位，但不得：

- 將 Threshold 值複製成另一套可獨立修改的正式 Contract。
- 在 Config 中加入 `expected_event_type` 給 Generator 使用。
- 讓 Generator 讀取 EventStore 決定下一筆資料。
- 讓 Generator 依 Detector 結果自動調整輸入直到通過。

## 5.3 Config Validation

至少驗證：

- Root 為 Mapping。
- `version` 非空。
- `tick_seconds > 0`。
- `random_seed` 為 Integer。
- `recovery_seconds >= 60`。
- `allow_trigger_during_injection == false`。
- `allow_trigger_during_recovery == false`。
- Log Output Path 非空。
- Exporter Port 為 1～65535。
- Metrics Update Interval 大於 0。
- Baseline Metrics 皆為有限數值。
- QPS／DB Pool 不得為負數。
- Memory Baseline 在 0～100。
- Baseline Memory 小於正式 Threshold。
- Baseline Latency 小於正式 Threshold。
- Jitter 不得使正常值跨過正式 Threshold。
- `background_errors.enabled` 預設且正式為 `false`。
- Scenario Key 剛好包含 S1～S6。
- 所有 Duration 大於 0。
- S1 Unauthorized Count `>= 10`，正式 Demo 預設 50。
- S2 Latency `>= configs/thresholds.yaml` 的正式值。
- S3 Memory `>= configs/thresholds.yaml` 的正式值。
- S4 Status Code `>= 500`。
- S5 Affected Service Count `>= 5`。
- S6 Rate Limit Log Count `>= 20`，正式 Demo 預設 55。
- S6 QPS Multiplier `>= configs/metrics_iforest.yaml` 的 `request_spike_ratio`。
- S2／S3 Duration 至少為：

```text
scrape_interval
+ metrics_detection_poll
+ safety_margin
```

- Recovery 至少涵蓋正式 Log Window 與 Cooldown。

Config 不合法時，在 Generator／Exporter 啟動前 Raise `ValueError`。

## 5.4 Cross-Config Read-only Validation

SPEC-005 可唯讀解析：

```text
configs/event_detection.yml
configs/thresholds.yaml
configs/metrics_iforest.yaml
configs/event_runner.yaml
```

用途只限：

- 驗證 Scenario 不低於正式 Trigger。
- 驗證 Recovery 不短於 Window／Cooldown。
- 驗證 QPS Warm-up Sample Requirement。
- 驗證 Metrics Poll Interval。

不得由 SPEC-005 自動改寫上述檔案。

---

# 6. Scenario Runtime Data Structures

## 6.1 `ScenarioId`

```python
from enum import Enum

class ScenarioId(str, Enum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
```

不得增加 `S0`、`S7` 或以自由字串取代正式 ID。

## 6.2 `ScenarioPhase`

```python
class ScenarioPhase(str, Enum):
    STOPPED = "STOPPED"
    BASELINE = "BASELINE"
    INJECTING = "INJECTING"
    RECOVERY = "RECOVERY"
```

## 6.3 `ScenarioCommand`

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ScenarioCommand:
    scenario_id: ScenarioId
    requested_at: float
```

此物件只存在控制層，不得寫入 Log／Prometheus Metric／Event Schema。

## 6.4 `ScenarioRuntimeSnapshot`

```python
@dataclass(frozen=True)
class ScenarioRuntimeSnapshot:
    phase: ScenarioPhase
    active_scenario: ScenarioId | None
    phase_started_at: float
    phase_ends_at: float | None
    trigger_count: int
    stop_requested: bool
```

## 6.5 `ScenarioValidationResult`

```python
@dataclass(frozen=True)
class ScenarioValidationResult:
    scenario_id: str
    started_at: str
    completed_at: str
    expected_event_types: list[str]
    actual_event_types: list[str]
    missing_event_types: list[str]
    unexpected_event_types: list[str]
    event_schema_valid: bool
    generator_evidence_valid: bool
    prometheus_evidence_valid: bool
    runner_success: bool
    passed: bool
    failure_reason: str | None
```

Validation Result 是驗收輸出，不得回傳給 Generator 作為資料生成決策。

---

# 7. Runtime State Machine

## 7.1 Startup

```text
load scenarios.yaml
→ validate config
→ initialize fixed random generator
→ initialize Log Generator
→ initialize Metrics Generator／Exporter
→ phase = BASELINE
→ continuously generate normal data
```

Runtime 啟動後不得自動觸發 S1～S6。

## 7.2 Baseline Phase

Baseline 期間：

- Log 持續寫入正常資料。
- Metrics 持續更新正常值。
- 可接受一個 Scenario Trigger。
- 不可產生不可控制 Error。
- 不讀取 EventStore。

收到合法 Trigger：

```text
BASELINE
→ active_scenario = requested scenario
→ phase = INJECTING
→ set injection end time
```

## 7.3 Injection Phase

Injection 期間：

- 依 Scenario Contract 產生異常資料。
- 其他非相關訊號維持 Baseline。
- 拒絕新 Scenario Trigger。
- 不能以 User Input 延長成無限異常。
- Duration 到期後必須自動進入 Recovery。

## 7.4 Recovery Phase

```text
INJECTING completed
→ phase = RECOVERY
→ all generators return to normal baseline values
→ wait recovery_seconds
→ phase = BASELINE
```

Recovery 目的：

- 讓前一個 Scenario 離開 60 秒 Log Window。
- 讓 Cooldown 到期。
- 讓 Metrics 回復正常。
- 避免下一個 Scenario 被前一輪資料污染。

Recovery 期間拒絕新 Trigger。

## 7.5 Repeated Trigger

同一 Runtime 應支援：

```text
BASELINE
→ S2
→ RECOVERY
→ BASELINE
→ S5
→ RECOVERY
→ BASELINE
→ S1
```

不得要求重啟程式、重新啟動 Exporter 或刪除 Log 才能切換下一個 Scenario。

## 7.6 Stop

收到 `Ctrl+C` 或明確 Stop Command：

- 設定停止旗標。
- 停止接受 Trigger。
- 完成本輪安全寫入。
- 正常離開 Loop。
- 不刪除 Log／Event／Model。
- 不顯示未處理 Traceback。

---

# 8. Randomness、Baseline 與資料安全

## 8.1 Fixed Seed

所有 Runtime Randomness 必須使用 Config 指定 Seed。

不得在各 Function 內重複建立未指定 Seed 的全域 Random Generator。

相同 Config 與 Seed 應產生：

- 相同數量級。
- 相同 Scenario 關聯。
- 相同正常數值範圍。
- 相同服務／IP／User 選擇序列。

不要求 UUID／Timestamp 位元完全相同，但關聯語意必須可重現。

## 8.2 正常 Baseline

正常 Baseline 可以包含：

- `INFO` Log。
- 少量正常 `WARN`，前提是不形成正式異常 Pattern。
- 2xx Status。
- 合理 Latency。
- 合理 Memory。
- 穩定 QPS 加小幅抖動。
- 合理 DB Pool 值。

正常 Baseline 不得包含：

- `OutOfMemoryError`。
- 同 IP 大量 401。
- 同 Target 大量 429。
- 多服務共同 `ConnectionRefused`。
- `external_service` + 5xx。
- 跨服務同 Trace ERROR Pattern。
- Memory／Latency Threshold Crossing。
- QPS 至少 3 倍 Spike。

## 8.3 Background Error

v1.0：

```yaml
background_errors:
  enabled: false
```

若既有 Generator 目前預設產生 Random Error：

- 必須改為預設關閉。
- 必須可由 Config 控制。
- 必須使用固定 Seed。
- E2E Validation 時必須關閉。

本 SPEC 不要求實作額外 Background Error Profile。

## 8.4 Mock Data Security

不得產生：

- 真實姓名。
- 真實身分證字號。
- 真實信用卡號。
- 真實金融帳號。
- 真實電話／Email。
- 真實 API Key／Token／Password。
- 真實公司 Host／Domain／Internal IP。

建議使用文件用途保留位址，例如：

```text
192.0.2.0/24
198.51.100.0/24
203.0.113.0/24
```

User／Transaction：

```text
user_mock_001
TXN-MOCK-0001
```

---

# 9. Log Generator Contract

## 9.1 Output

正式 Output：

```text
logs/aiops.json.log
```

格式：

```text
UTF-8 JSON Lines
一行一個 JSON Object
```

寫入必須：

- Append。
- 每筆完整一行。
- `json.dumps(..., allow_nan=False)` 可序列化。
- 不輸出 Python repr。
- 不輸出多行 Stack Trace 破壞 JSONL。

## 9.2 Required Schema

每筆 Log 至少具有可解析的固定 Schema；不適用欄位使用 `null` 或正式 Parser 接受的 Default，不得隨 Scenario 任意改欄位名稱。

```text
timestamp
level
service_name
trace_id
status_code
duration_ms
error_type
error_message
source_ip
user_id
transaction_id
downstream_service
external_service
target_service
memory_usage_pct
rate_limit_quota
```

正式行為以現有 Log Parser／FeatureExtractor 可接受型別為準。

### 型別原則

| 欄位 | 型別／規則 |
|---|---|
| `timestamp` | ISO8601 UTC String |
| `level` | `INFO`／`WARN`／`ERROR` |
| `service_name` | Non-empty String |
| `trace_id` | String or null |
| `status_code` | Integer |
| `duration_ms` | Finite Number，`>= 0` |
| `error_type` | String or null |
| `error_message` | String or null |
| `source_ip` | Mock String or null |
| `user_id` | Mock String or null |
| `transaction_id` | Mock String or null |
| `downstream_service` | String or null |
| `external_service` | String or null |
| `target_service` | String or null |
| `memory_usage_pct` | Finite Number，0～100 |
| `rate_limit_quota` | Finite Number，`>= 0` |

不得將以下控制資訊寫進正式 Log：

```text
scenario_id
expected_event_type
should_trigger
is_anomaly
expected_severity
classifier_result
```

## 9.3 Baseline Log Pattern

正常 Log 建議：

- `level=INFO`。
- `status_code=200`。
- `duration_ms` 在正常範圍。
- Error 欄位為 null。
- Service 可在已知服務間輪替。
- Trace ID 可存在，但不得形成多服務 ERROR Pattern。
- Source IP／User 可為 Mock 值。

## 9.4 S1 — Brute Force

必須在 60 秒內產生：

- 同一 `source_ip`。
- 同一或可追蹤 `user_id`。
- 至少 10 筆 `status_code=401`。
- 正式 Demo 預設 50 筆 401。
- 可補 1 筆 Account Locked Log，但不得取代 401 數量。

不得混入：

- `external_service`。
- OOM。
- 429。
- 多服務共同 Downstream Cascade。

預期 Log Event：

```text
brute_force_detected
```

## 9.5 S2 — DB Slow Query Cascade

必須產生至少 3 筆：

- 相同 `trace_id`。
- 不同 `service_name`。
- `level=ERROR`。
- `downstream_service=core-db`。
- 合理跨層順序，例如 DB → AP → Gateway。
- AP／Gateway 可包含 504。
- `duration_ms` 顯著升高。

同步 Metrics：

```text
api_p95_latency_ms >= 3000.0
```

並維持足夠時間跨過 Scrape、Poll 與 Safety Margin。

預期 Event：

```text
cross_service_failure
high_latency_detected
```

## 9.6 S3 — OOM

Log 必須至少包含：

```text
error_type = OutOfMemoryError
level = ERROR
service_name = 合法且非空的 OOM-origin service 名稱
```

可補：

- Memory Warning。
- Service Crash。
- 502 Bad Gateway。

同步 Metrics：

```text
system_memory_usage_pct >= 90.0
```

並維持足夠時間跨過 Scrape、Poll 與 Safety Margin。

預期 Event：

```text
oom_crash_detected
high_memory_detected
```

## 9.7 S4 — External API Failure

Log 必須包含：

- `external_service` 非 null。
- `status_code >= 500`。
- `transaction_id` 非 null。
- `duration_ms` 表示 Timeout／Failure。
- Error Message 不包含真實外部機構憑證或 URL Secret。

預期 Event：

```text
external_dependency_failure
```

S4 不要求正式 Metrics Event。

## 9.8 S5 — DB Network Cascade

必須在 60 秒內產生：

- 至少 5 個不同 `service_name`。
- 指向同一 `downstream_service=core-db`。
- `level=ERROR`。
- `error_type=ConnectionRefused`。
- 不同 Trace ID，表示多個獨立請求共同失敗。
- 正式 Demo 可產生數十筆 Log。

預期 Event：

```text
downstream_cascade_failure
```

S5 不依賴 DB Pool Event。

## 9.9 S6 — Rate Limit Storm

必須在 60 秒內產生：

- 同一 `target_service`。
- 至少 20 筆 `status_code=429`。
- 正式 Demo 預設 55 筆。
- `rate_limit_quota` 有效。
- 不得把 `request_spike_detected` 寫進 Log。

同步 Metrics：

- 先完成 QPS Warm-up。
- 當前 QPS 至少達近期 Baseline 3.0 倍。
- Spike Window 必須由 IForest 判定異常後，才由 Detector 輸出正式 Event。

預期 Event：

```text
rate_limit_storm
request_spike_detected
```

---

# 10. Metrics Generator Contract

## 10.1 Exporter

預設：

```text
Port 8000
```

Exporter 應持續存在，不因單一 Scenario 結束而停止或重新綁定 Port。

同一 Runtime 不得重複啟動兩個 Exporter。

## 10.2 正式 Metrics

```text
system_memory_usage_pct
api_p95_latency_ms
api_requests_per_sec
db_pool_active_connections
```

要求：

- Metric Name 完全一致。
- Value 為有限數值。
- 不輸出 NaN／Inf。
- QPS／DB Pool 不為負數。
- Memory 在 0～100。
- `api_requests_per_sec` v1.0 必須維持單一 Series。
- 不得加入 service Label 造成 SPEC-003 多 Series Error。

## 10.3 Baseline Metrics

Baseline 應穩定且不觸發正式 Event：

| Metric | 建議 Baseline |
|---|---:|
| `system_memory_usage_pct` | 約 55，有限抖動 |
| `api_p95_latency_ms` | 約 250，有限抖動 |
| `api_requests_per_sec` | 約 10，有限抖動 |
| `db_pool_active_connections` | 約 8，有限抖動 |

Jitter 必須經 Config Validation 證明不會跨越 Threshold 或形成 3 倍 QPS Spike。

## 10.4 Scenario Mapping

| Scenario | Memory | Latency | QPS | DB Pool |
|---|---|---|---|---|
| S1 | Baseline | Baseline | Baseline | Baseline |
| S2 | Baseline | `>= 3000`，持續 | Baseline | 僅觀測，不作驗收 |
| S3 | `>= 90`，持續 | Baseline | Baseline | 僅觀測，不作驗收 |
| S4 | Baseline | Baseline | Baseline | Baseline |
| S5 | Baseline | Baseline | Baseline | 可維持 Baseline；不建立 Event |
| S6 | Baseline | Baseline | Warm-up 後 `>= 3x` Baseline | Baseline |

不得因 DDS-001 的「可擴充」描述，自行為 S4／S5 新增 Metrics Event。

## 10.5 S2／S3 Hold Duration

Minimum：

```text
prometheus_scrape_interval_seconds
+ metrics_detection_poll_seconds
+ safety_margin_seconds
```

預設：

```text
15 + 15 + 15 = 45 秒
```

這是 E2E 測試資料設計，不代表 SPEC-002 已解決所有瞬時 Spike。

## 10.6 S6 Warm-up

S6 Trigger 前，Validation 必須確認 Prometheus `query_range` 已回傳至少：

```text
configs/metrics_iforest.yaml
window.min_sample_count
```

筆有效 QPS Samples。

不得只依固定 Sleep 猜測 Warm-up 完成。

建議流程：

```text
Baseline QPS running
→ query_range
→ clean／sort／deduplicate sample count
→ sample count sufficient
→ inject QPS spike
```

若在可接受 Timeout 內仍不足，Scenario 應標記 Environment／Warm-up Failure，不得直接注入並假裝通過。

## 10.7 Recovery

Scenario 結束後所有 Metrics 必須回到 Baseline Range。

不得：

- 永久維持異常值。
- 在 Recovery 產生反向極端值。
- 重設 Exporter Server。
- 改變 Metric Name。
- 清除 Prometheus 歷史資料。

---

# 11. Manual Runtime CLI

## 11.1 Entry Point

```powershell
python scripts/run_mock_runtime.py --config configs/scenarios.yaml
```

## 11.2 Required Commands

CLI 至少支援：

```text
1 / S1
2 / S2
3 / S3
4 / S4
5 / S5
6 / S6
status
quit
```

顯示：

- Current Phase。
- Active Scenario。
- Remaining Injection／Recovery Time。
- Exporter Port。
- Log Output Path。
- Trigger Accepted／Rejected Reason。

## 11.3 Non-blocking Baseline Requirement

等待人類輸入時，Baseline 必須持續產生。

建議實作：

- Runtime Main Loop 是唯一 Generator State Writer。
- Console Input 可由 Standard Library `threading` + `queue.Queue` 提供 Command。
- Input Thread 只讀取命令並放入 Queue，不直接修改 Generator State。

不得因 `input()` Blocking 讓 Log／Metrics 停止更新。

## 11.4 Single Scenario Mode

建議支援：

```powershell
python scripts/run_mock_runtime.py `
  --config configs/scenarios.yaml `
  --scenario S3 `
  --exit-after-recovery
```

用途：

- 人工重現。
- CI 外的 Integration。
- Demo 準備。

## 11.5 Runtime Scope

此 CLI 不負責：

- `docker compose up`。
- 啟動 EventDetectionRunner。
- 啟動 Alert Correlation。
- 建立 Incident。
- 呼叫 LLM。

---

# 12. Observability Validation

## 12.1 Metrics Exporter

至少驗證：

```text
http://localhost:8000/metrics
```

可看到四個正式 Metric。

## 12.2 Prometheus Target

Prometheus Target 必須為 UP。

至少記錄：

- Target URL。
- Last Scrape。
- Scrape Health。
- Error Message（若失敗）。

## 12.3 Instant Query

必須能查詢：

```text
api_p95_latency_ms
system_memory_usage_pct
```

驗證：

- Baseline 值。
- S2／S3 異常值。
- Recovery 後正常值。

## 12.4 Range Query

必須能查詢：

```text
api_requests_per_sec
```

驗證：

- Matrix Response。
- 單一 Series。
- Sample Count 足夠。
- S6 前有 Baseline。
- S6 後有 Spike Sample。

## 12.5 Loki／Promtail

至少確認：

- `logs/aiops.json.log` 被 Promtail 讀取。
- Loki 可查詢到新 Log。
- Scenario Log 可被辨識。
- 不需修改 Detector 才能看到資料。

## 12.6 Grafana

人工驗證至少確認：

- Prometheus Data Source 正常。
- Loki Data Source 正常。
- Existing Dashboard 可顯示 Logs／Metrics。
- S2 Latency、S3 Memory、S6 QPS 有可觀察變化。

Grafana 美編、正式 Dashboard 重構與 Incident UI 不屬於本 SPEC。

## 12.7 Docker／Observability Modification Gate

若發現：

- Prometheus Scrape Target 設定錯誤。
- Exporter Port 不一致。
- Promtail Path 無法讀取正式 Log。
- Grafana Data Source Provisioning 錯誤。

必須先提供：

```text
現象
重現步驟
實際 Config
預期 Contract
最小修改建議
受影響檔案
```

取得 PM 核准後才可修改 Conditional Scope。

---

# 13. Event Detection Integration

## 13.1 Prerequisite Check

E2E 開始前，必須確認：

- `models/log_isolation_forest.pkl` 存在。
- `models/metrics_isolation_forest.pkl` 存在。
- Runner Config 存在。
- Prometheus 可連線。
- Log Output Path 可寫入。
- EventStore Parent Directory 可用。
- Event Store 現有 Offset 可記錄。

缺少 Model 時：

```text
Fail Fast
→ 清楚指出既有 Training Script
→ 不自動修改 Config
→ 不將 Pipeline Disable 以繞過
```

## 13.2 Event Runner Usage

受控 E2E 優先使用：

```python
runner.run_once()
```

在正式 scenario detection cycle呼叫 `EventDetectionRunner.run_once()` 前，Scenario Validator必須使用 bounded、data-driven readiness polling；polling沿用正式 validation timeout／poll interval，timeout後明確 FAIL，不得以固定 sleep或無限等待判斷 readiness。

Log Detector checkpoint的 minimum Log count必須從 Event Runner所引用的正式 Log Detector config之 `window.min_log_count` 取得，Validator不得 hardcode。對S3，除本輪 boundary後有效 Log count達到該值外，還必須確認本輪 Runtime evidence已存在至少一筆 `error_type == "OutOfMemoryError"` 且 `service_name`為合法非空字串的Log。若Scenario已完成Recovery而required checkpoint從未成立，validation必須失敗，不得降低標準。

上述evidence只可用於readiness判斷與Event產生後的validation comparison；不得注入Detector、`WindowSummary`、`EventBuilder`或Runtime Event，也不得以validator-only expected service取代實際Runtime Log evidence。本alignment不修改Production Runner scheduler、`run_due_once()`或`start()`。

Readiness alignment automated evidence：

- Targeted Validator tests：`29 passed`。
- SPEC-005 automated set：`71 passed`。
- Final repository regression：`429 passed, 0 failed`。

### Pre-scenario Runner Priming（Implementation Evidence）

Runtime／runner 啟動後、任何 scenario evidence 產生前，Controller 先呼叫一次 `runner.run_once()`，用以建立 `LogReader` initial EOF／offset state。Priming pipeline failure 必須使 validation fail。

Priming 不是 scenario；priming event 不得計入任何 scenario evidence。

原因：

- 強制執行所有 Enabled Pipeline。
- 不依賴 Scheduler Due Time。
- 適合每個 Scenario 的驗收 Checkpoint。
- 不修改 `next_due`。

不得：

- 再次寫入 EventStore。
- 修改 Runner Result 內 Event。
- 在 Generator 內 import Runner。

## 13.3 EventStore Evidence Isolation

每個 Scenario 開始前記錄：

- File Byte Offset，或
- Existing Line Count。

驗收只讀取本輪新增內容。

不得以刪除整個 EventStore 作為唯一隔離方式。

Runtime Artifact 可於人工測試前備份／清理，但正式 Validation Script 必須能只分析 Append 後的新增 Event。

Implementation Evidence：Controller 在 scenario 前記錄 EventStore byte boundary，僅讀取該 boundary 後 append 的 events。Runner return events 不是唯一成功依據；必須確認 runner-produced event 已存在於 EventStore。Controller 不寫 EventStore，append-only ownership 維持於 Detector／`EventDetectionRunner` pipeline。

## 13.4 Expected Event Matrix

| Scenario | Required Log Event | Required Metrics Event | Total Required |
|---|---|---|---:|
| S1 | `brute_force_detected` | 無 | 1 |
| S2 | `cross_service_failure` | `high_latency_detected` | 2 |
| S3 | `oom_crash_detected` | `high_memory_detected` | 2 |
| S4 | `external_dependency_failure` | 無 | 1 |
| S5 | `downstream_cascade_failure` | 無 | 1 |
| S6 | `rate_limit_storm` | `request_spike_detected` | 2 |

Metrics Threshold 與 Metrics IForest 採 OR 接納；同一事故的多來源 Event 全部保留。

## 13.5 Known Scenario Acceptance

Known Scenario 必須出現正式 Expected Event Type。

以下不可取代 Expected Event：

```text
general_log_anomaly
general_metrics_anomaly
```

若已知 Scenario 只產生 General Fallback：

- Scenario FAIL。
- 保存 Input Window 與 Event Evidence。
- 判斷 Generator Gap 或 Contract Defect。
- 不自行改 Classifier Rule。

## 13.6 Event Schema Validation

每筆新增 Event Top-level 欄位必須剛好為：

```text
event_id
detected_at
event_source
event_type
detection_method
severity
confidence
service_name
trace_id
source_ip
downstream_service
external_service
status
triggered_features
raw_log_sample
```

不得因 E2E 增加：

```text
scenario_id
validation_id
expected_event_type
passed
```

Validation Metadata 只存在 ScenarioValidationResult。

## 13.7 Scenario-specific Event Evidence

### S1

- `event_source=log_event_detection`。
- `event_type=brute_force_detected`。
- `source_ip` 非 null。

### S2

- `cross_service_failure.trace_id` 非 null。
- `cross_service_failure.downstream_service` 非 null。
- `high_latency_detected.event_source=metrics_threshold_detection`。
- `high_latency_detected.detection_method=threshold`。

### S3

- `oom_crash_detected.severity=CRITICAL`。
- `oom_crash_detected.service_name` 必須等於本輪 S3 注入的 `OutOfMemoryError` Log 的 `service_name`。
- `high_memory_detected.event_source=metrics_threshold_detection`。

Validator 可以基於本輪 test input／evidence 驗證 expected identity，但不得將 `scenario_id`、`expected_event_type` 或 validator-only metadata 注入 Detector、Event Schema 或 Runtime Correlation。EventStore evidence isolation 繼續使用既有 byte boundary／append 後新增內容；本次驗證不要求清空 EventStore。

## 13.8 S3 Identity Revalidation（PASS）

正式 Runtime command：

```powershell
python scripts/validate_scenarios.py --config configs/scenarios.yaml --runner-config configs/event_runner.yaml --scenario S3
```

Result：`PASS`。

本輪 Runtime evidence：

- New Logs：`63`。
- OOM Logs：`1`。
- Actual OOM-origin service：`payment-api`。
- Persisted Event Types：`oom_crash_detected`、`high_memory_detected`。

Persisted OOM Event：

```text
event_type = oom_crash_detected
service_name = payment-api
severity = CRITICAL
event_source = log_event_detection
detection_method = isolation_forest
```

Identity validation：

```text
persisted oom_crash_detected.service_name
== actual OutOfMemoryError Log.service_name
Result = PASS
```

Runner output／EventStore consistency：`PASS`。Metrics `high_memory_detected` contract：`PASS`。`payment-api`只代表本次Runtime evidence，不是產品固定service或validator expected answer。

本次follow-up不改寫2026-08-12 `Phase 6 — S3 PASS`；該歷史結果仍只證明當時的Event Type／Severity／Metrics acceptance，本節才是strengthened identity contract的正式revalidation evidence。

### 13.9 S1～S6 Follow-up Runtime Regression Evidence

正式command：

```powershell
python scripts/validate_scenarios.py --config configs/scenarios.yaml --runner-config configs/event_runner.yaml --all
```

結果：

- S1 PASS — `brute_force_detected`。
- S2 PASS — `cross_service_failure` + `high_latency_detected`。
- S3 PASS — `oom_crash_detected` + `high_memory_detected`。
- S4 PASS — `external_dependency_failure`。
- S5 PASS — `downstream_cascade_failure`。
- S6 PASS — `rate_limit_storm` + `request_spike_detected`。

本次S3 repair未造成其他Scenario detection contract regression。額外 `general_log_anomaly`／`general_metrics_anomaly` evidence可以保留，但不得取代required Scenario Events。

### 13.10 Retained Runtime Evidence

本次E2E使用EventStore／Log byte-boundary validation，不要求EventStore為空，亦未執行reset或cleanup。Runtime evidence保留於：

- `logs/aiops.json.log`。
- `events/event_store.jsonl`。

Local Model artifacts：

- `models/log_isolation_forest.pkl`。
- `models/metrics_isolation_forest.pkl`。

上述皆為local Runtime Artifacts，不宣稱已提交Repository。

Closure確認：Event Schema仍為15個top-level fields；Log `WindowFeatureVector`仍為23維且順序不變；Metrics IForest contract、Model、Threshold、classifier priority、`high_memory_detected` semantics及S1／S2／S4／S5／S6 Event Detection contract均未改變。PRD-003未因此次Closure修改或標記Final。

### S4

- `external_dependency_failure.external_service` 非 null。

### S5

- `downstream_cascade_failure.downstream_service=core-db` 或正式 Config 值。

### S6

- `rate_limit_storm.event_source=log_event_detection`。
- `request_spike_detected.event_source=metrics_iforest_detection`。
- `request_spike_detected.detection_method=isolation_forest`。
- `triggered_features` 保留 Baseline、Current QPS、Spike Ratio、Window／Score 資訊。

#### S6 Final Flow（Implementation Evidence）

```text
baseline samples ready
→ trigger S6
→ exact 55 HTTP 429（same target_service）
→ Prometheus observes current_qps
   >= baseline_mean × configured request_spike_ratio
→ EventDetectionRunner.run_once()
→ rate_limit_storm
→ request_spike_detected
→ recovery
→ PASS
```

`request_spike_ratio` 保持 config-driven。Baseline samples readiness 與 formal spike readiness 均以資料判定；允許 bounded polling with timeout／polling interval，禁止以固定 sleep 秒數作為「已 ready」的判斷依據。

---

# 14. Scenario Validation Script

## 14.1 Entry Point

```powershell
python scripts/validate_scenarios.py `
  --config configs/scenarios.yaml `
  --runner-config configs/event_runner.yaml
```

可選：

```powershell
--scenarios S1,S2,S3
--report-path reports/spec005/result.json
```

## 14.2 Validation Flow

```text
check prerequisites
→ start／attach MockDataRuntime
→ ensure BASELINE
→ ensure Prometheus ready
→ ensure QPS warm-up when required
→ record EventStore offset
→ trigger scenario
→ collect generator evidence
→ wait until required detector checkpoint
→ EventDetectionRunner.run_once()
→ read newly appended events
→ validate schema／types／sources
→ wait Recovery
→ verify BASELINE restored
→ produce ScenarioValidationResult
```

## 14.3 Timeout

所有等待必須有 Timeout：

- Exporter Ready Timeout。
- Prometheus Target Ready Timeout。
- QPS Warm-up Timeout。
- Scenario Injection Timeout。
- Recovery Timeout。
- Event Appearance Timeout。

Timeout 必須標記 FAIL，不得無限等待。

## 14.4 Exit Code

| Exit Code | 意義 |
|---:|---|
| 0 | 所有指定 Scenario 通過 |
| 1 | 至少一個 Scenario 驗收失敗 |
| 2 | Environment／Prerequisite／Startup Failure |

## 14.5 Report Safety

Report 不得包含：

- API Key。
- Password。
- Token。
- 完整環境變數。
- 真實個資。
- Pickle Binary。

可包含：

- Scenario ID。
- Expected／Actual Event Type。
- Sanitized Error Type／Message。
- Metric Evidence。
- Event ID。
- Timestamp。

---

# 15. Error Handling

## 15.1 Fatal Configuration Errors

| 情境 | 行為 |
|---|---|
| Scenario Config 不存在 | `FileNotFoundError` |
| YAML 錯誤 | Raise Parse Error |
| Root 非 Mapping | `ValueError` |
| 缺少 S1～S6 | `ValueError` |
| Duration／Count 不合法 | `ValueError` |
| Baseline 可能跨 Threshold | `ValueError` |
| Background Error 預設開啟 | `ValueError` |
| Exporter Port 不合法 | `ValueError` |
| Cross-config Contract 不一致 | `ValueError` 或 Domain Error |

Fatal Error 必須發生在 Runtime Loop 與 Exporter 啟動前。

## 15.2 Trigger Rejection

| 情境 | 行為 |
|---|---|
| Runtime 未在 BASELINE | Reject，保持原 Phase |
| Unknown Scenario | Reject／`ValueError` |
| Stop 已要求 | Reject |
| 重複 Trigger 同一 Scenario | Reject，不重設 Timer |

Reject 不得造成 Generator Crash。

## 15.3 Generator Runtime Error

- 單筆 Log Serialize Error：記錄 ERROR，不寫入破損行。
- 無效 Metric Value：拒絕更新，維持最後合法值或 Baseline。
- Exporter Bind Failure：Startup Fail Fast。
- Log Path Permission Error：Startup Fail Fast。
- Scenario Adapter Unexpected Error：Runtime 標記失敗並停止受控 Runtime，不得偽裝 Recovery 成功。

## 15.4 Environment Failure

| 情境 | Validation Result |
|---|---|
| Prometheus Unavailable | Environment Failure |
| Target Down | Environment Failure |
| Loki Unavailable | Observability Failure |
| Model Missing | Prerequisite Failure |
| Runner Config Error | Startup Failure |
| EventStore 無法讀寫 | Environment Failure |

## 15.5 Contract Defect

符合以下條件時標記 Contract Defect：

```text
Generator Output 符合 PRD／SPEC
+ Prometheus／Log Evidence 正確
+ Detector 仍無法依正式 Contract 建立 Expected Event
```

處理：

- 不修改 Detector。
- 不改 Threshold。
- 不增加 Scenario 強度至不合理數值。
- 保存最小重現資料。
- 回報 PM 另立 Integration Fix／SPEC Revision。

---

# 16. Logging

## 16.1 Logger

建議：

```python
logging.getLogger("MockDataRuntime")
logging.getLogger("LogGenerator")
logging.getLogger("MetricsGenerator")
logging.getLogger("ScenarioValidator")
```

不得在 Import 時重複設定全域 `logging.basicConfig()`。

## 16.2 Log Level

| Level | 使用情境 |
|---|---|
| DEBUG | Baseline Tick、Jitter、Command Queue Empty |
| INFO | Runtime Started／Stopped、Scenario Accepted、Phase Change、Recovery Complete |
| WARNING | Trigger Rejected、Warm-up 接近 Timeout、Observability Partial Failure |
| ERROR | Generator Failure、Exporter Failure、Validation Failure |

## 16.3 Required Context

Runtime Log 至少包含：

- Phase。
- Scenario ID（控制層 Log 可包含）。
- Phase Start／End。
- Trigger Accepted／Rejected。
- Generator Status。
- Sanitized Error Type／Message。

不得逐筆完整印出大量 401／429 或敏感欄位。

---

# 17. Testing Strategy

## 17.1 Testing Layers

```text
Unit Tests
→ Generator Contract Tests
→ Scenario Alignment Tests
→ Full Regression
→ Optional Observability Integration
→ Six Scenario E2E Gate
```

## 17.2 Unit Test Principles

必要 Unit Tests：

- 不依賴 Docker。
- 不依賴 Prometheus。
- 不依賴 Loki／Grafana。
- 不依賴正式 Model。
- 不依賴外部網路。
- 不實際等待 45／60／300 秒。
- 使用 Fake Clock／Injected Sleeper。
- 使用 `tmp_path`。
- 使用固定 Random Seed。
- 不寫正式 Log／EventStore。

## 17.3 Config Tests

至少包含：

- Valid Config。
- Missing Root Section。
- Missing Scenario。
- Unknown Scenario。
- Invalid Duration。
- Invalid Port。
- Baseline Cross Threshold。
- Jitter Cross Threshold。
- Background Error Enabled。
- S1 Count Too Low。
- S2 Latency Too Low。
- S3 Memory Too Low。
- S4 Status Below 500。
- S5 Service Count Too Low。
- S6 Log Count Too Low。
- S6 Multiplier Too Low。
- Recovery Too Short。

## 17.4 State Machine Tests

至少包含：

- Startup 進入 BASELINE。
- Baseline 持續 Tick。
- Trigger S1 進入 INJECTING。
- Injection 到期進入 RECOVERY。
- Recovery 到期回 BASELINE。
- 同 Runtime 再 Trigger S2。
- Injection 期間 Reject Trigger。
- Recovery 期間 Reject Trigger。
- Stop 正常結束。
- Fake Clock 不真實 Sleep。

## 17.5 Log Generator Tests

至少包含：

- JSONL 每行合法。
- Schema 欄位固定。
- `allow_nan=False`。
- Baseline 不形成 S1～S6 Pattern。
- S1 同 IP 401 數量。
- S2 同 Trace 跨服務。
- S3 OOM。
- S4 External Service + 5xx。
- S5 至少五服務共同 Downstream。
- S6 至少 20／預設 55 筆 429。
- 無 `scenario_id`／`expected_event_type` 洩漏。
- Mock PII Safety。
- Fixed Seed Reproducibility。

## 17.6 Metrics Generator Tests

至少包含：

- 四個 Metric Name。
- Value 有限。
- QPS 單一 Series Contract。
- Baseline 不跨 Threshold。
- Jitter 不跨 Threshold。
- S2 Latency 值與 Duration。
- S3 Memory 值與 Duration。
- S6 Baseline + Spike Multiplier。
- Recovery 回到正常範圍。
- DB Pool 不建立 Event 語意。

## 17.7 Scenario Alignment Tests

可直接將 Generator Output 餵入正式 Parser／Feature Layer，但不得修改 Detector：

- Log Parser 接受全部 Scenario Log。
- FeatureExtractor 不因缺欄位失敗。
- S1～S6 關鍵欄位存在。
- Metrics 名稱與 Config 一致。
- S2／S3 Duration Derived Rule 正確。
- S6 Warm-up Gate 不足時拒絕注入或標記未就緒。

## 17.8 Regression Commands

必要：

```powershell
python -m pytest tests/test_scenario_runtime.py -q
python -m pytest tests/test_log_generator.py -q
python -m pytest tests/test_metrics_generator.py -q
python -m pytest tests/test_scenario_alignment.py -q
python -m pytest -q
```

實際測試檔若因既有 Repository 結構需合併，可調整檔名，但驗收範圍不得減少。

## 17.9 Optional Integration Commands

```powershell
python scripts/run_mock_runtime.py --config configs/scenarios.yaml
```

```powershell
python scripts/validate_scenarios.py `
  --config configs/scenarios.yaml `
  --runner-config configs/event_runner.yaml
```

Optional Integration 不得成為一般 Unit Test Gate。

---

# 18. File Scope 與治理

## 18.1 允許新增或修改

```text
configs/scenarios.yaml
src/scenario_runtime/
src/log_generator/
src/metrics_generator/
scripts/run_mock_runtime.py
scripts/validate_scenarios.py
tests/test_scenario_runtime.py
tests/test_log_generator.py
tests/test_metrics_generator.py
tests/test_scenario_alignment.py
tests/fixtures/scenarios/
```

若既有 Generator Tests 使用不同檔名，可以在同責任範圍內修改。

## 18.2 可讀取／Import，但不得修改

```text
src/event_detection/
configs/event_detection.yml
configs/thresholds.yaml
configs/metrics_iforest.yaml
configs/event_runner.yaml
requirements.txt
requirements-dev.txt
.gitignore
README.md
CONTRIBUTING.md
PRD／DDS／SPEC／ADR／SDD
```

## 18.3 Conditional Scope — 必須先取得 PM 核准

```text
docker-compose.yml
docker/prometheus/
docker/loki/
docker/promtail/
docker/grafana/
docker/grafana/dashboard.json
```

核准條件：

- Read-only Audit 已完成。
- 有可重現阻斷證據。
- 修改是最小範圍。
- 不改正式 Detection Contract。
- PM 明確同意受影響檔案。

## 18.4 禁止修改

未經 PM 另立 Revision，不得修改：

- Event Schema。
- Event Type。
- Threshold。
- Log／Metrics IForest Feature。
- Model Parameter。
- Event Classifier Rule。
- Event Runner Scheduler。
- Alert／Incident／RAG／Dashboard Business Logic。
- Dependency Files。

## 18.5 Runtime Artifact 不得提交

```text
logs/*.log
events/*.jsonl
models/*.pkl
reports/spec005/*
.venv/
__pycache__/
.pytest_cache/
```

## 18.6 AI Coding Agent Boundary

- 不得執行任何 Git 指令。
- 不得自行切換 Branch。
- 不得 Commit／Push／Merge／Rebase。
- 不得修改 Conditional／Forbidden Scope。
- 發現衝突時停止並回報 PM。
- 不得為通過 E2E 而降低驗收標準。
- 不得在 Generator 中寫入 Expected Event Answer。

---

# 19. Acceptance Criteria

> Completion annotation：僅有 Phase 5、Phase 6 與 Phase 7 final evidence 直接支援的結果可標記 PASS。下列未勾選 checklist 項目不得僅因 E2E Exit Code `0` 自動推定為通過；其原始 normative contract 保留不變。Phase completion summary 見 3.7、3.8。

## 19.1 Audit

- [ ] 已完成 Read-only Audit。
- [ ] 已確認實際 Generator Entry Point。
- [ ] 已確認現有 Scenario 選擇方式。
- [ ] 已確認 Scenario 結束後行為。
- [ ] 已確認 Random Error 行為。
- [ ] 已確認 Exporter Port 與 Prometheus Scrape Interval。
- [ ] 已完成 Scenario Gap Matrix。
- [ ] Conditional Scope 未經核准未修改。

## 19.2 Runtime Lifecycle

- [ ] Startup 後持續產生正常 Baseline。
- [ ] Scenario 只注入有限時間。
- [ ] Scenario 結束自動進 Recovery。
- [ ] Recovery 後自動回 Baseline。
- [ ] Runtime 持續到使用者關閉。
- [ ] 同 Runtime 可再次觸發不同 Scenario。
- [ ] Injection／Recovery 期間 Reject 新 Trigger。
- [ ] 不需要每個 Scenario 重啟 Generator。
- [ ] `Ctrl+C` Graceful Stop。

## 19.3 Randomness／Security

- [ ] Fixed Seed 可重現。
- [ ] 正常 Jitter 不跨 Threshold。
- [ ] Background Error 預設關閉。
- [ ] E2E 不受不可控制 Random Error 影響。
- [ ] 無真實個資／憑證／內部資訊。
- [ ] Log／Report 不洩漏敏感資料。

## 19.4 Log Generator

- [ ] JSONL 合法。
- [ ] Schema 固定。
- [ ] Baseline 不形成正式異常 Pattern。
- [ ] S1 產生至少 10、預設 50 筆同 IP 401。
- [ ] S2 產生相同 Trace 跨服務 ERROR。
- [ ] S3 產生 OutOfMemoryError。
- [ ] S4 產生 External Service + 5xx。
- [ ] S5 產生至少 5 個不同 Service 指向相同 Downstream。
- [ ] S6 產生至少 20、預設 55 筆同 Target 429。
- [ ] Generator Output 不包含 Expected Event Answer。

## 19.5 Metrics Generator

- [ ] 四個 Metric 正確暴露。
- [ ] QPS 為單一 Series。
- [ ] Baseline Value 合法且穩定。
- [ ] S2 Latency 達正式 Threshold 並維持足夠時間。
- [ ] S3 Memory 達正式 Threshold 並維持足夠時間。
- [ ] S6 QPS Warm-up 完成後才 Spike。
- [ ] S6 Spike 至少達正式 Ratio。
- [ ] Recovery 回到 Baseline。
- [ ] DB Pool 未被誤納入 Event Detection。

## 19.6 Observability

- [ ] Exporter `/metrics` 可讀。
- [ ] Prometheus Target UP。
- [ ] Memory／Latency Instant Query 正常。
- [ ] QPS Range Query 正常。
- [ ] QPS Sample Count 足夠。
- [ ] Loki 可看到新 Log。
- [ ] Grafana Existing Dashboard 可觀察資料。
- [ ] 未經核准未修改 Docker／Grafana。

## 19.7 Event Detection Integration

- [ ] Model Artifact 只在真實 E2E 前要求。
- [ ] Model Missing 時 Fail Fast。
- [ ] EventDetectionRunner 可 Startup。
- [ ] S1 Expected Event 通過。
- [ ] S2 兩筆 Expected Event 通過。
- [ ] S3 兩筆 Expected Event 通過。
- [x] SPEC-001 v2.3 S3 identity revalidation 通過：`oom_crash_detected.service_name == OutOfMemoryError Log.service_name`。
- [ ] S4 Expected Event 通過。
- [ ] S5 Expected Event 通過。
- [ ] S6 兩筆 Expected Event 通過。
- [ ] Event Schema 剛好 15 欄位。
- [ ] Event Source／Method 正確。
- [ ] Known Scenario 未以 General Fallback 取代。
- [ ] Event Runner 未重複寫入 EventStore。

## 19.8 Tests／Governance

- [ ] SPEC-005 Unit Tests 全部通過。
- [ ] Full Regression 全部通過。
- [ ] 必要 Tests 不依賴 Docker／Model／網路／真實 Sleep。
- [ ] 未修改 Event Detection Algorithm。
- [ ] 未修改 Dependency Files。
- [ ] 未新增 Event Type／Schema 欄位。
- [ ] AI Coding Agent 未執行 Git。
- [ ] Runtime Artifact 未準備提交。

---

# 20. Final Deliverables

完成後 Branch 應包含：

```text
configs/scenarios.yaml
src/scenario_runtime/
修正後 src/log_generator/
修正後 src/metrics_generator/
scripts/run_mock_runtime.py
scripts/validate_scenarios.py
SPEC-005 對應 Tests／Fixtures
```

PM 回報必須包含：

1. Current State Audit Summary。
2. Scenario Gap Matrix。
3. 實際修改檔案清單。
4. Runtime Lifecycle 實作說明。
5. 六大 Scenario Input Evidence。
6. Prometheus／Loki／Grafana 驗證結果。
7. 六大 Scenario E2E Result。
8. Unit／Regression Test Result。
9. Conditional Scope 是否修改。
10. Runtime Artifact 檢查。
11. Known Limitations。
12. Contract Defect（若有）。

不得交付：

```text
models/*.pkl
events/event_store.jsonl
logs/*.log
.venv/
真實憑證
未核准 Docker 修改
Detection Algorithm 修改
```

---

# 21. Known Limitations

## 21.1 非完整平台總開關

SPEC-005 的 MockDataRuntime 只負責 Generator。

完整本機流程仍可能需要分別：

```text
啟動 Docker／Observability
啟動 MockDataRuntime
啟動或呼叫 EventDetectionRunner
```

Alert Correlation、Incident、RCA 尚未納入，因此本 SPEC 不宣稱具備整個 AIOps 閉環的一鍵啟動。

## 21.2 Real-time E2E Duration

S2／S3 需跨過 Scrape／Poll；S6 需 QPS Warm-up；Scenario 間需 Recovery。

因此真實 E2E 可能需要數分鐘，不能以縮短正式 Interval、修改 Threshold 或跳過 Warm-up 來假裝通過。

## 21.3 Instant Query Limitation

SPEC-002 使用 Instant Query，短於 Scrape／Poll 的 Spike 可能漏失。

SPEC-005 以延長異常持續時間建立可重現 Demo，不代表已解決所有瞬時異常偵測問題。

## 21.4 QPS Model Scope

Metrics IForest v1.0 只處理 `api_requests_per_sec`。

`general_metrics_anomaly` 只表示未知 QPS Window 異常，不表示所有 Metrics 已具泛化能力。

## 21.5 Background Error

v1.0 預設不產生 Random Error。

此設計優先確保：

- E2E 可重現。
- Scenario 不互相污染。
- Demo 結果穩定。

若未來需要更真實的 Background Error Profile，應另立需求與驗收，不應直接打開不可控制亂數。

## 21.6 JSONL Event Store

`events/event_store.jsonl` 是 Prototype Store，不具備 Consumer Offset、ACK、Retry Queue、Partition 或 Exactly-once Delivery。

SPEC-005 以 File Offset／Line Count 區隔本輪 Evidence，不代表已完成正式 Queue Semantics。

## 21.7 Docker Auto-management

Validation Script 不自動執行 `docker compose up/down`，避免在未確認環境下修改或停止其他服務。

## 21.8 Log IForest Calibration／Live-normal False-positive Uncertainty

SPEC-005 Attempt #5 曾觀察到 baseline-like runtime window 輸出：

```text
event_type = general_log_anomaly
anomaly_score = -0.02050324516956037
window_log_count = 51
```

此 finding 表示 calibration／live-normal false-positive uncertainty，而非證明該 window 代表完整 production-normal distribution。核准 deterministic fixture 的 representativeness 有限，live-normal false-positive rate 尚未完整量化；fixture 的 `0/50` false positives 不等於 production false-positive rate 為 0，亦不證明 threshold 已完整泛化。

`general_log_anomaly` fallback contract 仍然有效。歷史 `score_threshold = -0.05` 的 coverage 為 `0/6`，目前正式部署值 `score_threshold = -0.01` 的 coverage 為 `6/6`，且核准 deterministic fixture 的 false positives 為 `0/50`。這些 evidence 仍受上述 live-normal false-positive uncertainty 與 representativeness limitation 限制；本 limitation 不構成靜默回退 `score_threshold` 至歷史值 `-0.05` 的依據。

以下僅為 current deployed contract summary／cross-document reference：目前正式部署的 `score_threshold = -0.01`，predictor 必須同時通過 double gate：

```text
label == -1
AND
score < configured score_threshold
```

Log Event Detection 的 authoritative detector contract 仍由 SPEC-001 v2.2 定義。SPEC-005 不重新定義 predictor semantics，且不得改為 label-only detection。

---

# 22. PM Review Checklist

## 22.1 Scope

- [ ] 修改是否只在 Allowed Scope？
- [ ] Docker／Grafana 修改是否有 PM 核准？
- [ ] 是否未修改 `src/event_detection/`？
- [ ] 是否未修改 Dependency Files？
- [ ] 是否未修改 PRD／SPEC／ADR／SDD？

## 22.2 Lifecycle

- [ ] 是否持續 Baseline？
- [ ] Scenario 是否有限時間？
- [ ] 是否有 Recovery？
- [ ] 是否回到 Baseline 並持續運作？
- [ ] 是否同 Runtime 可再觸發？
- [ ] 是否不需重啟？

## 22.3 Data Contract

- [ ] Log Schema 是否對齊 Parser？
- [ ] Metric Name 是否完全一致？
- [ ] QPS 是否單一 Series？
- [ ] 是否無 Answer Leakage？
- [ ] 是否無真實個資／憑證？
- [ ] Background Error 是否關閉？

## 22.4 Scenario Contract

- [ ] S1 Count／IP／401 是否正確？
- [ ] S2 Trace／Service／Latency 是否正確？
- [ ] S3 OOM／Memory 是否正確？
- [ ] S4 External／5xx 是否正確？
- [ ] S5 Service Diversity／Downstream 是否正確？
- [ ] S6 429／Target／QPS Warm-up／Spike 是否正確？

## 22.5 Integration

- [ ] Prometheus Target 是否 UP？
- [ ] Instant／Range Query 是否有證據？
- [ ] Loki／Grafana 是否有證據？
- [ ] Model Missing 是否 Fail Fast？
- [ ] 六大 Expected Event 是否完整？
- [ ] Event Schema 是否完整？
- [ ] 是否無 General Fallback 取代 Known Event？

## 22.6 Tests／Artifacts

- [ ] SPEC-005 Tests 是否通過？
- [ ] Full Regression 是否通過？
- [ ] Runtime Artifact 是否未 Staged？
- [ ] 是否無實際等待綁入 Unit Tests？
- [ ] 是否無 AI Agent Git 操作？

---

# 23. Traceability Matrix

| SPEC-005 Requirement | 上游依據 | 驗證方式 |
|---|---|---|
| 六大 Scenario 全覆蓋 | PRD-002 FR-06 | S1～S6 E2E |
| S1 同 IP 401 | PRD-002 S1 | Log Contract Test／E2E |
| S2 跨服務 Trace | PRD-002 S2 | Log Contract Test／E2E |
| S2 Latency | PRD-002 S2、SPEC-002 | Prometheus Instant Query／E2E |
| S3 OOM／Memory | PRD-002 S3、SPEC-002 | Log＋Metrics E2E |
| S3 OOM-origin service identity | PRD-002 v1.4 S3、SPEC-001 v2.3 | 本輪 Log／Event evidence revalidation PASS（見13.8） |
| S4 External Failure | PRD-002 S4 | Log Contract Test／E2E |
| S5 Downstream Cascade | PRD-002 S5 | Service Diversity Test／E2E |
| S6 429 Storm | PRD-002 S6 | Log Contract Test／E2E |
| S6 QPS Spike | PRD-002 S6、SPEC-003 | Query Range／IForest E2E |
| DB Pool 僅觀測 | PRD-002 4.9、SPEC-002／003 | No Event Test |
| Event 15 欄位 | PRD-002 第 5 章 | Event Schema Validator |
| Event Runner 統一執行 | SPEC-004 | `run_once()` Integration |
| S2／S3 延長異常 | SPEC-004 Deferred Integration | Duration／Prometheus Evidence |
| Generator 對齊 Detector | SPEC-001 Phase 4 | Contract Tests |
| 不可控制 Error 關閉 | PM Decision／SPEC-005 | Fixed Seed／No Background Error Test |
| 同 Runtime 重複 Scenario | PM Decision／SPEC-005 | State Machine Re-trigger Test |
| Docker 修改需核准 | PRD-002 Out of Scope | File Scope Audit |
| Model 只在 E2E 必要 | SPEC-004 Manual Runtime Prerequisite | Unit／E2E Split |
| Log IForest score threshold calibration | SPEC-001 v2.2 | Predictor double gate＋SPEC-005 Phase 6 calibration evidence |
| Log training artifact prerequisite | SPEC-001 v2.2 training flow | Model prerequisite fail-fast check |

---

# 24. Definition of Done

## 24.1 Engineering Completion Result

Engineering DoD 已完成，結果為：

```text
PASS WITH KNOWN LIMITATIONS
```

此結論不宣告 external／business approval。

SPEC-005 只有在以下全部成立時才算完成：

```text
Read-only Audit 完成
+ Scenario Gap Matrix 完成
+ Scenario Config／State Machine 完成
+ 持續 Baseline 完成
+ S1～S6 有限 Injection 完成
+ Recovery／Repeated Trigger 完成
+ 不可控制 Random Error 關閉
+ Log Generator Contract 完成
+ Metrics Generator Contract 完成
+ Prometheus／Loki／Grafana 驗證完成
+ Model Prerequisite 行為正確
+ EventDetectionRunner 真實整合完成
+ 六大 Scenario E2E 全部通過
+ SPEC-005 Tests 通過
+ Full Regression 通過
+ File Scope Audit 通過
+ Runtime Artifact Audit 通過
+ 內部 PM 工程／文件審查完成
```

上述審查項目僅表示本專案內部 engineering/document governance review 已完成，用以確認 engineering completion；不代表 external stakeholder acceptance、business approval 或合作企業正式驗收，也不應因此將 SPEC-005 `Status` 改為 `Approved`。

SPEC-005 完成代表：

> Mock Data、Observability 與 Event Detection Layer 已具備可重現的六大情境端到端展示基礎。

SPEC-005 完成不代表：

- Alert Correlation 已完成。
- Incident Manager 已完成。
- LLM／RAG RCA 已完成。
- Dashboard／Email 已完成。
- 整個 AIOps 平台已有 Production 一鍵啟動。

