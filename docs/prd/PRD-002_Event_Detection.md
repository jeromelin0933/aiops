# PRD-002：Event Detection 子系統需求文件

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | PRD-002 |
| Document Name | Event Detection |
| Version | 1.2 |
| Status | Approved |
| Date | 2026-08-05 |
| Author | 林子豪（PM） |
| Related Documents | ADR-001、PRD-001、DDS-001 |

| Version | Date | Change |
|---|---|---|
| 1.1 | 2026-07-26 | 明確定義 Metrics Threshold 與 Metrics Isolation Forest 雙軌分工；SPEC-003 v1.0 限定 QPS；新增 `general_metrics_anomaly`；定義 DB Pool 為僅觀測指標；補充 S6 Metrics 驗收條件。 |
| 1.2 | 2026-08-06 | 對齊 SPEC-002 v1.3 Threshold 邊界規則；將 S2 Latency 與 S3 Memory 觸發條件明確修正為大於或等於門檻。；經 PM 確認後將文件狀態更新為 Approved。 |
---

## 1. 文件目的

本文件定義 **Event Detection 子系統**的產品需求。

本模組接收來自 DDS-001 已建立的 Logs 與 Metrics 兩條資料來源，透過各自獨立的異常偵測流程，將原始資料轉換為標準化的 **Event**，提供下游 Alert Correlation Engine 進行事件關聯分析。

本文件**僅描述系統需要達成的能力（What）**，不定義演算法細節（How），相關實作設計將於後續 SPEC 文件中定義。

---

## 2. 背景

### 2.1 前一階段成果（DDS-001 已完成）

| 項目 | 狀態 |
|---|---|
| Log Generator（六大劇本） | ✅ 完成 |
| Metrics Generator | ✅ 完成 |
| Prometheus | ✅ 完成 |
| Loki + Promtail | ✅ 完成 |
| Grafana Dashboard | ✅ 完成 |
| Docker Compose | ✅ 完成 |

目前系統已能產生模擬資料並在 Grafana 上觀測，但**尚未具備自動判斷哪些資料代表「異常事件」的能力**。

### 2.2 本階段目標

建立 Event Detection Layer，使系統從「被動觀測」升級為「主動感知」，成為 Incident-driven Architecture 的第一層核心判斷模組。

### 2.3 在整體架構中的位置

```
Logs ──▶ Log Event Detection ──────┐
                                   ▼
                             Event Queue
                                   ▲
Metrics ──▶ Metrics Event Detection┘
                    │
                    ▼
          ┌─── Alert Correlation ←── (下一階段)
          │
          ▼
     Incident Manager
          │
          ▼
      LLM + RAG
```

Event Detection 是 Incident 誕生前的最後一道關口，其輸出品質直接決定後續所有模組的準確性。

---

## 3. 功能需求

### FR-01 Log Event Detection

系統須能自動讀取 `logs/aiops.json.log`，分析其中的 Log 資料，並在偵測到異常時建立 **Log Event**。

- 必須能感知六大劇本（S1–S6）的 Log 異常特徵
- **不得直接輸出 Alert 或觸發 Email**
- **不得依賴 LLM 判斷**
- 輸出格式必須符合本文件第 5 章定義的 Event Schema

### FR-02 Metrics Event Detection

系統須能自動從 Prometheus（`http://localhost:9090`）定期撈取 Metrics，並在偵測到異常時建立 **Metrics Event**。

- 必須能感知六大劇本所需的 Metrics 異常（詳見第 4 章）
- **不得直接輸出 Alert 或觸發 Email**
- **不得依賴 LLM 判斷**
- 輸出格式必須符合本文件第 5 章定義的 Event Schema

本階段 Metrics Event Detection 拆分為兩條互補且彼此獨立的 Pipeline：

#### Metrics Threshold Detection

負責具有明確營運門檻的靜態異常：

- `api_p95_latency_ms` → `high_latency_detected`
- `system_memory_usage_pct` → `high_memory_detected`

#### Metrics Isolation Forest Detection

SPEC-003 v1.0 僅針對：

```text
api_requests_per_sec
建立 QPS 時間視窗與動態基準，並輸出：

已知 Request Spike：request_spike_detected
未知 QPS Window 異常：general_metrics_anomaly

general_metrics_anomaly 的適用範圍僅限 api_requests_per_sec，
不得解讀為系統已涵蓋所有 Prometheus Metrics 的未知異常。

兩條 Metrics Pipeline 應使用不同的 event_source：

metrics_threshold_detection
metrics_iforest_detection

任一 Pipeline 獨立判定異常時，均可建立標準化 Event。
同一事故也可能同時產生不同來源的 Event，後續再交由 Event Runner
與 Alert Correlation Engine 處理，不得在 Event Detection 階段互相排斥或覆蓋。
```

### FR-03 各 Event Detection Pipeline 互相獨立

本階段包含三條彼此獨立的 Event Detection Pipeline：

1. Log Event Detection
2. Metrics Threshold Detection
3. Metrics Isolation Forest Detection

各 Pipeline 必須符合：

- 單一 Pipeline 發生錯誤，不得影響其他 Pipeline 運作
- 各 Pipeline 可獨立開發、啟動與測試
- 各 Pipeline 可獨立建立符合第 5 章的 Event
- 不得要求其中一條 Pipeline 成功觸發後，另一條才可輸出
- 最後由 SPEC-004 Event Runner 統一執行與整合

### FR-04 Event 標準化輸出

所有 Event（無論來自 Log 或 Metrics）必須轉換為**統一的 Event Schema**，讓下游 Alert Correlation Engine 能無差別消費。

### FR-05 每個 Event 必須可追蹤

每個 Event 必須保留完整的來源資訊，包含：

- 是由哪條偵測流程產生的（`event_source`）
- 偵測時間（`detected_at`）
- 使用的偵測方法（`detection_method`）
- 觸發異常的原始特徵值（`triggered_features`）

### FR-06 六大劇本全覆蓋

Event Detection 必須能正確偵測全部六個劇本的異常，不得遺漏任何一個。

---

## 4. 六大劇本偵測需求

### S1 密碼爆破（Log Detection）

| 項目 | 規格 |
|---|---|
| 觸發條件 | 同一 `source_ip` 在 60 秒內出現 ≥ 10 筆 `status_code=401` |
| Event Type | `brute_force_detected` |
| Severity | CRITICAL |
| 關鍵欄位 | `source_ip`、`user_id`、`status_code` |
| 預期行為 | 50 筆 401 → 建立 1 個 Event（不是 50 個） |

### S2 DB 慢查詢引發雪崩（Log Detection）

| 項目 | 規格 |
|---|---|
| 觸發條件 | 同一 `trace_id` 出現跨越多個 `service_name` 的 ERROR Log |
| Event Type | `cross_service_failure` |
| Severity | HIGH |
| 關鍵欄位 | `trace_id`、`downstream_service`、`duration_ms` |
| 預期行為 | 3 筆同 trace_id 的 Log → 建立 1 個 Event，帶有 trace_id 資訊 |

同時需要 Metrics Detection 補強：

| 項目 | 規格 |
|---|---|
| 觸發條件 | `api_p95_latency_ms` 達到或超過 3000ms |
| Event Type | `high_latency_detected` |
| Detection Method | Threshold |

### S3 OOM 崩潰（Log + Metrics Detection）

Log Detection：

| 項目 | 規格 |
|---|---|
| 觸發條件 | Log 中出現 `error_type=OutOfMemoryError` |
| Event Type | `oom_crash_detected` |
| Severity | CRITICAL |

Metrics Detection：

| 項目 | 規格 |
|---|---|
| 觸發條件 | `system_memory_usage_pct` 達到或超過 90% |
| Event Type | `high_memory_detected` |
| Detection Method | Threshold |

### S4 第三方 API 斷線（Log Detection）

| 項目 | 規格 |
|---|---|
| 觸發條件 | Log 中 `external_service` 欄位不為 null 且 `status_code >= 500` |
| Event Type | `external_dependency_failure` |
| Severity | HIGH |
| 關鍵欄位 | `external_service`、`transaction_id`、`duration_ms` |

### S5 DB 網路瞬斷風暴（Log Detection）

| 項目 | 規格 |
|---|---|
| 觸發條件 | 60 秒內 ≥ 5 個不同 `service_name` 出現指向同一 `downstream_service` 的 ERROR |
| Event Type | `downstream_cascade_failure` |
| Severity | CRITICAL |
| 關鍵欄位 | `downstream_service`、`error_type=ConnectionRefused` |
| 預期行為 | 50 筆不同服務的 Log → 建立 1 個 Event，標記 `root_service=core-db` |

### S6 Rate Limit 429 風暴（Log Detection）

| 項目 | 規格 |
|---|---|
| 觸發條件 | 60 秒內同一 `target_service` 出現 ≥ 20 筆 `status_code=429` |
| Event Type | `rate_limit_storm` |
| Severity | HIGH |
| 關鍵欄位 | `target_service`、`rate_limit_quota` |
| 預期行為 | 55 筆 429 → 建立 1 個 Event（冷卻期機制） |

同時需要 Metrics Isolation Forest Detection 補強：
| 項目 | 規格 |
|---|---|
| 適用 Metric | `api_requests_per_sec` |
| 觸發條件 | Isolation Forest 判定 QPS Window 異常，且當前 QPS 達近期基準至少 3.0 倍 |
| Event Type | `request_spike_detected` |
| Event Source | `metrics_iforest_detection` |
| Detection Method | `isolation_forest` |
| Severity | HIGH |
| 預期行為 | 建立 1 筆 `request_spike_detected` Event，並保留 QPS 基準、當前值、Spike Ratio、Window Features 與 Anomaly Score |

Isolation Forest 負責判斷 QPS Window 是否異常；
Rule Classifier 負責將已判定異常且 Spike Ratio 達 3.0 的行為
分類為 `request_spike_detected`。

Rule Classifier 不得在 Isolation Forest 判定正常時單獨建立正式 Event。

### 4.7 未知 Log 異常 fallback

六大劇本是本 Prototype 的 Demo Validation Set，並非系統能力上限。

Log Event Detection 不應只依賴 S1–S6 的固定規則進行偵測，而應具備泛化異常偵測能力。

當 Log Event Detection 判定某個時間視窗明顯偏離正常行為，但該異常視窗無法被分類為 S1–S6 任一已知劇本時，系統仍須建立 Event。

| 項目 | 規格 |
|---|---|
| 觸發條件 | Log Detection 判定某一時間視窗為異常，但無法對應至 S1–S6 |
| Event Type | `general_log_anomaly` |
| Severity | MEDIUM 或 HIGH，由異常分數與特徵決定 |
| Detection Method | `isolation_forest` |
| 預期行為 | 不丟棄未知異常，仍建立 Event 供下游 Alert Correlation 與 LLM RCA 使用 |

此設計確保本系統不是只能辨識六大固定劇本，而是能先偵測未知異常，再對已知劇本提供更具可解釋性的分類。

### 4.8 未知 Metrics 異常 fallback

六大劇本是本 Prototype 的 Demo Validation Set，並非系統能力上限。

Metrics Isolation Forest Detection v1.0 不應只辨識已知的 S6 Request Spike。
當 `api_requests_per_sec` 的時間視窗明顯偏離已學習的正常 QPS 行為，
但無法分類為 `request_spike_detected` 時，系統仍須建立 Event。

| 項目 | 規格 |
|---|---|
| 適用 Metric | `api_requests_per_sec` |
| 觸發條件 | Isolation Forest 判定 QPS Window 異常，但不符合已知 Request Spike 分類條件 |
| Event Type | `general_metrics_anomaly` |
| Event Source | `metrics_iforest_detection` |
| Detection Method | `isolation_forest` |
| Severity | MEDIUM 或 HIGH，由異常分數與特徵決定 |
| 預期行為 | 不丟棄未知 QPS 異常，仍建立 Event 供下游 Alert Correlation 與 Incident Management 使用 |

本功能的未知 Metrics 異常範圍僅限 QPS Window。

本功能不代表以下指標均已具備未知異常偵測能力：

- `api_p95_latency_ms`
- `system_memory_usage_pct`
- `db_pool_active_connections`

若 Isolation Forest 判定 Window 正常，即使單一分類規則成立，
也不得輸出 `general_metrics_anomaly` 或 `request_spike_detected`。

### 4.9 DB Pool 指標範圍

`db_pool_active_connections` 目前僅作為觀測與未來擴充指標。

本階段可由：

- Metrics Generator 產生
- Prometheus 收集
- Grafana 顯示

但不納入：

- Metrics Threshold Detection 正式 Event 輸出
- Metrics Isolation Forest Detection v1.0 模型輸入
- 本階段正式 Event Detection 驗收條件

本階段不定義 DB Pool 專屬 Event Type。

六大情境中的資料庫相關劇本仍可透過以下訊號驗證：

- S2：`api_p95_latency_ms`、相同 `trace_id` 與跨服務 Log
- S5：相同 `downstream_service` 與跨服務 Log

因此六大情境驗收不依賴 DB Pool Event。
若未來要將 DB Pool 納入正式異常偵測，必須先更新 PRD-002，
定義 Metric 語意、觸發條件、Event Type、Severity 與驗收案例。
---

## 5. Event Schema（各模組共用契約）
### 5.0 Schema Ownership

本章為本專案 Event Schema 的唯一正式定義。

所有 Event Detection 相關模組，包括 Log Event Detection、Metrics Threshold Detection、Metrics Isolation Forest Detection 與 Event Runner，皆必須輸出或處理符合本章定義的 Event 物件。

PRD-001 僅引用本章，不另行維護 Event Schema。

任何 SPEC、程式碼或測試案例不得自行新增、刪除或重新命名本章定義的欄位。若未來需要調整 Event Schema，必須先更新本章，並同步修正所有受影響的 SPEC 文件。
> ⚠️ 此 Schema 為所有模組的共用契約。一旦確定，任何修改都必須先與 PM 討論並更新本文件後，才能異動程式碼。

```json
{
  "event_id":           "EVT-1720000001-a3f9",
  "detected_at":        "2026-07-13T10:00:01.234Z",
  "event_source":       "log_event_detection",
  "event_type":         "downstream_cascade_failure",
  "detection_method":   "rule_based",
  "severity":           "CRITICAL",
  "confidence":         0.94,
  "service_name":       "payment-service",
  "trace_id":           "f3a9c1b2e7d8",
  "source_ip":          null,
  "downstream_service": "core-db",
  "external_service":   null,
  "status":             "OPEN",
  "triggered_features": {
    "affected_service_count": 5,
    "error_count_60s":        47,
    "common_downstream":      "core-db"
  },
  "raw_log_sample":     []
}
```

**欄位說明：**

| 欄位 | 型別 | 說明 |
|---|---|---|
| `event_id` | string | `EVT-{timestamp}-{random4}` 格式 |
| `detected_at` | string | ISO8601 UTC，偵測到異常的時間 |
| `event_source` | string | Event 來源模組。允許值：`log_event_detection`、`metrics_threshold_detection`、`metrics_iforest_detection` |
| `event_type` | string | 對應第 4 章已知劇本或 General Anomaly fallback 的正式 Event Type |
| `detection_method` | string | `rule_based`、`threshold`、`isolation_forest` |
| `severity` | string | `CRITICAL`、`HIGH`、`MEDIUM`、`LOW` |
| `confidence` | float | 0.0–1.0，偵測信心度 |
| `service_name` | string | 直接涉及的服務名稱 |
| `trace_id` | string or null | 跨服務追蹤 ID（S2 必填） |
| `source_ip` | string or null | 攻擊來源 IP（S1 必填） |
| `downstream_service` | string or null | 下游根因服務（S2、S5 必填） |
| `external_service` | string or null | 外部依賴服務（S4 必填） |
| `status` | string | `OPEN`（新建立）或 `CLOSED`（已處理） |
| `triggered_features` | object | 觸發此 Event 的特徵值，供 Alert Correlation 參考 |
| `raw_log_sample` | array | Log Event 最多保留 3 筆原始 Log；Metrics Event 固定使用空陣列 `[]` |

---

## 6. 非功能需求

| 項目 | 規格 |
|---|---|
| NFR-01 偵測間隔 | Metrics Detection 每 **15 秒**執行一次（與 Prometheus scrape interval 對齊） |
| NFR-02 Log 讀取 | Log Detection 採 **tail 模式**持續讀取新增的 Log，不重複處理已讀取的紀錄 |
| NFR-03 不依賴 LLM | 所有偵測邏輯必須在本地完成，不得呼叫任何外部 AI API |
| NFR-04 不觸發通知 | Event Detection 不得直接寄送 Email 或推送告警 |
| NFR-05 無額外容器 | 所有偵測邏輯以 Python 程式執行，不新增 Docker 容器 |
| NFR-06 Event 持久化 | 產生的 Event 需寫入 `events/event_store.jsonl`（JSONL 格式） |

---

## 7. 不包含項目（Out of Scope）

本階段明確**不負責**以下項目：

| 項目 | 由哪個階段負責 |
|---|---|
| Alert Correlation（收斂多個 Event 為 Incident） | PRD-003 |
| Incident Manager | PRD-003 |
| LLM RCA | PRD-004 |
| RAG 知識庫 | PRD-004 |
| Email 通知 | PRD-005 |
| Dashboard 更新 | PRD-005 |
| 修改 docker-compose.yml | 需 PM 核准 |
| 修改 Grafana Dashboard | 需 PM 核准 |
| 修改 Prometheus 設定 | 需 PM 核准 |

---

## 8. Git 開發規範

### 8.1 Branch 與負責範圍

Event Detection 階段拆分為多個可獨立開發的 Feature Branch。  
本階段不再使用單一 `feature/event-detection` branch。

各子模組皆使用 PM 已建立好的 Feature Branch 進行開發。完成後由 PM 統一 review、測試與整合回 `develop`。

| 子模組 | Git Branch | 負責人 | 主要修改範圍 |
|---|---|---|---|
| Log Event Detection | `feature/log-event-detection` | 林子豪 | `src/event_detection/log/`、`src/event_detection/model/`、`src/event_detection/event/`、`src/event_detection/store/`、`configs/event_detection.yml`、`tests/` |
| Metrics Threshold Detection | `feature/metrics-threshold` | 富裕 | `src/event_detection/metrics_threshold.py`、`configs/thresholds.yaml`、`tests/` |
| Metrics Isolation Forest Detection | `feature/metrics-iforest` | Tako | `src/event_detection/metrics_iforest.py`、相關模型設定、`tests/` |
| Event Runner | `feature/event-runner` | 待定 | 整合 Log / Metrics Detection 輸出，統一事件執行流程 |

> `feature/event-detection` 不再作為本階段開發分支使用。  
> Event Detection 是階段名稱，不是單一功能分支。

---

### 8.2 組員切換至指定分支

PM 已預先建立本階段所需的 Feature Branch。  
組員開始實作前，不需要自行建立新分支，只需切換到自己負責的分支。

第一次取得遠端分支時，請先執行：

```bash
git fetch origin
```

接著依照自己的負責項目切換分支。

富裕負責 Metrics Threshold Detection：

```bash
git checkout feature/metrics-threshold
```

Tako 負責 Metrics Isolation Forest Detection：

```bash
git checkout feature/metrics-iforest
```

林子豪負責 Log Event Detection：

```bash
git checkout feature/log-event-detection
```

若本機尚未建立該分支，Git 可能需要使用以下格式從遠端分支建立本機追蹤分支：

```bash
git checkout -b feature/<branch-name> origin/feature/<branch-name>
```

例如：

```bash
git checkout -b feature/metrics-threshold origin/feature/metrics-threshold
```

切換完成後，請確認目前所在分支：

```bash
git branch
```

或：

```bash
git status
```

確認畫面顯示目前位於自己的 Feature Branch 後，才可以開始實作。

---

### 8.3 組員完成後流程

組員完成實作後，只需要將自己的 Feature Branch commit 並 push 至 GitHub。  
組員不需要自行 merge 到 `develop`，也不需要自行處理 `develop` 的整合。

基本流程如下：

```bash
git status
git add <changed-files>
git commit -m "feat: implement <module-name>"
git push origin feature/<branch-name>
```

例如富裕完成 Metrics Threshold Detection 後：

```bash
git status
git add src/event_detection/metrics_threshold.py configs/thresholds.yaml tests/
git commit -m "feat: implement metrics threshold detection"
git push origin feature/metrics-threshold
```

完成 push 後，請通知 PM 進行 review。

PM 會負責：

- 拉取組員的 feature branch
- 檢查修改範圍
- 執行測試
- 必要時同步 `develop`
- 解決 merge conflict
- 將通過驗收的功能合併回 `develop`

---

### 8.4 PM 整合規則

`develop` 分支由 PM 統一維護。  
組員不得自行將 Feature Branch merge 到 `develop`。

在任何 Feature Branch 合併回 `develop` 前，PM 會負責執行以下檢查：

1. 確認該分支只修改允許範圍內的檔案。
2. 確認 Event Schema 符合本文件第 5 章。
3. 確認測試可通過。
4. 同步 `develop` 最新內容。
5. 解決可能發生的 merge conflict。
6. 確認合併後 `develop` 可正常執行。

組員只需確保自己的分支能正常執行並成功 push。

---

### 8.5 允許 / 不允許修改的範圍

**可以修改：**

- 自己負責模組對應的 `src/event_detection/` 子檔案
- 自己模組需要的 `configs/` 設定檔
- 自己模組對應的 `tests/` 測試檔

**需先與 PM 討論才能動：**

- Event Schema（本文件第 5 章）
- 共用設定檔名稱或格式
- 共用工具類別
- `events/event_store.jsonl` 的寫入格式
- 其他組員負責的模組

**不得自行修改：**

- `docker/`
- `docker-compose.yml`
- `src/log_generator/`
- `src/metrics_generator/`
- `docker/grafana/dashboard.json`
- `docker/prometheus/prometheus.yml`
- `docker/promtail/promtail-config.yml`
- ADR、SDD、PRD 文件
- README.md
- Event Schema

Event Schema 為所有模組共用契約，唯一正式定義位於本文件第 5 章。  
任何模組不得自行新增、刪除或重新命名 Event Schema 欄位。

---

### 8.6 資料夾結構（本階段新增部分）

本階段預期新增 `src/event_detection/` 作為 Event Detection Layer 的主要程式目錄。

```text
src/
└── event_detection/
    ├── __init__.py
    │
    ├── log/
    │   ├── __init__.py
    │   ├── reader.py
    │   ├── parser.py
    │   ├── features.py
    │   └── encoder.py
    │
    ├── model/
    │   ├── __init__.py
    │   ├── schema.py
    │   ├── trainer.py
    │   └── predictor.py
    │
    ├── event/
    │   ├── __init__.py
    │   └── builder.py
    │
    ├── store/
    │   ├── __init__.py
    │   └── event_store.py
    │
    ├── metrics_threshold.py
    ├── metrics_iforest.py
    └── runner.py

events/
└── event_store.jsonl

configs/
├── event_detection.yml
└── thresholds.yaml
```

| 路徑 | 說明 |
|---|---|
| `src/event_detection/log/` | Log 讀取、解析、特徵抽取與編碼 |
| `src/event_detection/model/` | Log Detection 使用的模型 schema、訓練與推論 |
| `src/event_detection/event/` | Event 組裝與分類 |
| `src/event_detection/store/` | EventStore，負責寫入 `events/event_store.jsonl` |
| `src/event_detection/metrics_threshold.py` | Metrics Threshold Detection |
| `src/event_detection/metrics_iforest.py` | Metrics Isolation Forest Detection |
| `src/event_detection/runner.py` | 後續 Event Runner 或整合入口 |
| `configs/event_detection.yml` | Log Event Detection 設定 |
| `configs/thresholds.yaml` | Metrics Threshold Detection 設定 |

---

### 8.7 Git 操作注意事項

組員實作期間不得執行以下操作：

```bash
git merge develop
git merge main
git push origin develop
git push origin main
git rebase
git reset --hard
git branch -D
```

若遇到 Git 衝突、pull 失敗、push 失敗或分支狀態不確定，請先停止操作並通知 PM。

本專案採用 PM 統一整合策略，避免多人同時處理 `develop` 造成版本混亂。

---

## 9. 成功標準（驗收條件）

本階段完成的判斷標準如下，**全部通過才算完成**：

| 編號 | 驗收項目 |
|---|---|
| AC-01 | 觸發 S1（50 筆 401），`event_store.jsonl` 中出現且僅出現 1 筆 `brute_force_detected` Event |
| AC-02 | 觸發 S2（同 trace_id 三層 Log），出現 1 筆 `cross_service_failure` Event，含正確 trace_id |
| AC-02b | 觸發 S2 Metrics 補強時，`api_p95_latency_ms >= 3000ms` 產生 1 筆 `high_latency_detected` Event |
| AC-03 | 觸發 S3（OOM），出現 `oom_crash_detected` Event；Metrics 達 90% 時出現 `high_memory_detected` Event |
| AC-04 | 觸發 S4（外部 API 逾時），出現 1 筆 `external_dependency_failure` Event，含 external_service 資訊 |
| AC-05 | 觸發 S5（50 筆跨服務 Log），出現 1 筆 `downstream_cascade_failure` Event，`triggered_features.common_downstream=core-db` |
| AC-06 | 觸發 S6（55 筆 429），出現 1 筆 `rate_limit_storm` Event（不是 55 個） |
| AC-06b | QPS Window 被 Isolation Forest 判定異常，且當前 QPS 達近期基準至少 3.0 倍時，產生 1 筆 `request_spike_detected` Event |
| AC-06c | QPS Window 被 Isolation Forest 判定異常，但無法分類為 Request Spike 時，產生 1 筆 `general_metrics_anomaly` Event |
| AC-06d | Isolation Forest 判定正常時，Rule Classifier 不得單獨建立 `request_spike_detected` 或 `general_metrics_anomaly` |
| AC-06e | `general_metrics_anomaly` 的正式範圍僅限 `api_requests_per_sec` |
| AC-06f | `db_pool_active_connections` 本階段只收集與視覺化，不產生正式 Event |
| AC-07 | 所有 Event 的 Schema 符合第 5 章定義，欄位完整，型別正確 |
| AC-08 | Log Detection、Metrics Threshold Detection 與 Metrics Isolation Forest Detection 各自獨立，其中一條關閉或失敗不得影響其他 Pipeline |
| AC-09 | Event 寫入 `events/event_store.jsonl`，格式為 JSONL（每行一筆完整 JSON） |
| AC-10 | `events/` 目錄已加入 `.gitignore` |

---

## 10. 下一階段預告

本 PRD-002 完成後，進入：

```
PRD-003
Alert Correlation & Incident Manager

內容包含：
├── 四條收斂規則（S1–S6 各自的 Correlation Logic）
├── Incident Schema 定義
├── Incident 狀態機（Open → Analyzing → Resolved）
└── 冷卻期機制（防止 LLM 被洗版）
```

對應的 SPEC 文件（實作細節）將在 PRD-003 確認後產出：

| SPEC | 內容 |
|---|---|
| SPEC-001 | Log Event Detection 實作規格 |
| SPEC-002 | Metrics Threshold Detection 實作規格 |
| SPEC-003 | Metrics Isolation Forest Detection 實作規格 |
| SPEC-004 | Event Runner 整合規格 |

---

## 11. 里程碑更新

| Milestone | 狀態 |
|---|---|
| G1 Mock Data | ✅ Completed（DDS-001） |
| G2 Observability Platform | ✅ Completed（DDS-001） |
| G3 Event Detection | 🔄 In Progress（本文件） |
| G4 Alert Correlation | Planned（PRD-003） |
| G5 Incident Manager | Planned（PRD-003） |
| G6 LLM + RAG RCA | Planned（PRD-004） |
| G7 Dashboard Integration | Planned（PRD-005） |
| G8 Email Notification | Planned（PRD-005） |
