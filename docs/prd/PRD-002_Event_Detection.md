# PRD-002：Event Detection 子系統需求文件

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | PRD-002 |
| Document Name | Event Detection |
| Version | 1.0 |
| Status | Draft |
| Date | 2026-07-13 |
| Author | 林子豪（PM） |
| Related Documents | ADR-001、PRD-001、DDS-001 |

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

- 必須能感知六大劇本對應的 Metrics 異常（詳見第 4 章）
- **不得直接輸出 Alert 或觸發 Email**
- **不得依賴 LLM 判斷**
- 輸出格式必須符合本文件第 5 章定義的 Event Schema

### FR-03 兩條 Pipeline 互相獨立

Log Event Detection 與 Metrics Event Detection 必須是兩個**彼此獨立的偵測流程**。

- 單一偵測流程發生錯誤，不得影響另一條流程的運作
- 兩者可以各自先完成、各自測試，最後再整合

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
| 觸發條件 | `api_p95_latency_ms` 超過 3000ms |
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
| 觸發條件 | `system_memory_usage_pct` 超過 90% |
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

同時需要 Metrics Detection 補強：

| 項目 | 規格 |
|---|---|
| 觸發條件 | `api_requests_per_sec` 短時間內暴增超過基準值 3 倍 |
| Event Type | `request_spike_detected` |
| Detection Method | Isolation Forest |

---

## 5. Event Schema（各模組共用契約）

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
| `event_source` | string | `log_event_detection` 或 `metrics_event_detection` |
| `event_type` | string | 對應第 4 章各劇本的 Event Type |
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
| `raw_log_sample` | array | 最多 3 筆觸發此 Event 的原始 Log |

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

| 子模組 | Git Branch | 可修改目錄 |
|---|---|---|
| Log Event Detection | `feature/event-detection` | `src/event_detection/log/`、`configs/`、`tests/` |
| Metrics Threshold Detection | `feature/event-detection` | `src/event_detection/metrics/threshold/`、`configs/`、`tests/` |
| Metrics Isolation Forest | `feature/event-detection` | `src/event_detection/metrics/isolation_forest/`、`configs/`、`tests/` |
| Event Store 寫入 | `feature/event-detection` | `src/event_detection/store/`、`events/` |

> 本階段統一使用現有的 `feature/event-detection` branch，不另開新 branch，避免整合複雜度。

### 8.2 允許 / 不允許修改的範圍

**可以自由修改：**
- `src/event_detection/`（新目錄，本階段建立）
- `configs/`（新增設定檔）
- `tests/`（新增測試）
- `events/`（新目錄，存放 event_store.jsonl）

**需先與 PM 討論才能動：**
- Event Schema（本文件第 5 章）
- 共用設定檔名稱或格式

**不得自行修改：**
- `docker/`、`docker-compose.yml`
- `src/log_generator/`、`src/metrics_generator/`（DDS-001 已完成，不動）
- `docker/grafana/dashboard.json`
- `docker/prometheus/prometheus.yml`
- `docker/promtail/promtail-config.yml`
- ADR、SDD、PRD 文件

### 8.3 資料夾結構（本階段新增部分）

```
src/
└── event_detection/          ← 本階段新建
    ├── __init__.py
    ├── log/
    │   ├── __init__.py
    │   └── log_detector.py   ← Log 異常偵測邏輯
    ├── metrics/
    │   ├── __init__.py
    │   ├── threshold/
    │   │   ├── __init__.py
    │   │   └── threshold_detector.py
    │   └── isolation_forest/
    │       ├── __init__.py
    │       └── if_detector.py
    ├── store/
    │   ├── __init__.py
    │   └── event_store.py    ← Event 寫入 events/event_store.jsonl
    └── runner.py             ← 主執行入口，整合兩條 Pipeline

events/                       ← 本階段新建
└── event_store.jsonl         ← 所有 Event 寫入這裡（加入 .gitignore）

configs/
└── event_detection.yml       ← 偵測參數設定（Threshold 值、偵測間隔等）
```

---

## 9. 成功標準（驗收條件）

本階段完成的判斷標準如下，**全部通過才算完成**：

| 編號 | 驗收項目 |
|---|---|
| AC-01 | 觸發 S1（50 筆 401），`event_store.jsonl` 中出現且僅出現 1 筆 `brute_force_detected` Event |
| AC-02 | 觸發 S2（同 trace_id 三層 Log），出現 1 筆 `cross_service_failure` Event，含正確 trace_id |
| AC-03 | 觸發 S3（OOM），出現 `oom_crash_detected` Event；Metrics 超過 90% 時出現 `high_memory_detected` Event |
| AC-04 | 觸發 S4（外部 API 逾時），出現 1 筆 `external_dependency_failure` Event，含 external_service 資訊 |
| AC-05 | 觸發 S5（50 筆跨服務 Log），出現 1 筆 `downstream_cascade_failure` Event，`triggered_features.common_downstream=core-db` |
| AC-06 | 觸發 S6（55 筆 429），出現 1 筆 `rate_limit_storm` Event（不是 55 個） |
| AC-07 | 所有 Event 的 Schema 符合第 5 章定義，欄位完整，型別正確 |
| AC-08 | Log Detection 與 Metrics Detection 各自獨立，其中一條關閉不影響另一條 |
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
