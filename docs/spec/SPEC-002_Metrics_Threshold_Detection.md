# SPEC-002：Metrics Threshold Detection

## Software Design Specification v1.2（Aligned with PRD-001 / PRD-002 / SPEC-001 / DDS-001）

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-002 |
| Document Name | Metrics Threshold Detection |
| Version | 1.3 |
| Status | Implemented |
| Date | 2026-07-26 |
| Author | 林子豪（PM） |
| Assignee | 富裕 |
| Branch | `feature/metrics-threshold` |
| Related PRD | PRD-001、PRD-002 |
| Related DDS | DDS-001 |
| Related SPEC | SPEC-001（Log Event Detection）、SPEC-003（Metrics Isolation Forest Detection）、SPEC-004（Event Runner） |

| Version | Date | Change |
|---|---|---|
| 1.3 | 2026-07-26 | 文件對齊 PRD-001／PRD-002；明確限定 SPEC-003 v1.0 的未知 Metrics 異常為 QPS Window；將 DB Pool 定義為僅收集與視覺化；移除 Git 操作、Agent Prompt 與 Implementation Notes。無程式碼變更。 |
---

## 0. 文件目的與設計原則

本文件定義 **Metrics Threshold Detection** 模組的完整實作規格。

本模組負責從 DDS-001 已建立的 Prometheus HTTP API 讀取 Metrics，透過靜態閾值判斷是否發生 Metrics 異常，並輸出符合 **PRD-002 第 5 章 Event Schema** 的標準化 Event。

本文件是工程規格，不是概念說明。實作者應以本文件定義的
檔案範圍、資料結構、介面、測試案例與驗收標準為準。

Git 操作與 AI Coding Agent Prompt工作指示不屬於本 SPEC，
統一由 PM 透過獨立工作文件提供。

### 0.1 核心設計原則

1. **PRD-002 第 5 章是 Event Schema 唯一正式契約**  
   本 SPEC 不重新定義 Event Schema，只描述 Metrics Threshold Detection 如何填入欄位。

2. **DDS-001 是資料來源與觀測基礎**  
   Prometheus、Metrics Generator、Docker Compose、Grafana 皆由 DDS-001 提供，本 SPEC 不修改這些基礎設施。

3. **SPEC-001 已完成 Log Event Detection 基礎模組**  
   本 SPEC 可重用 `EventStore`，但不得直接重用 Log 專用的 `EventBuilder`。

4. **Threshold Detection 只負責靜態閾值**

   `api_requests_per_sec` 的動態基準、Request Spike 與未知 QPS Window 異常，
   屬於 SPEC-003 Metrics Isolation Forest Detection。

   SPEC-003 v1.0 的未知 Metrics 異常能力僅限於
   `api_requests_per_sec`，不代表所有 Prometheus Metrics
   均具備未知異常偵測能力。

5. **六大情境是 Demo Validation Set，不是資料輸入上限**  
   本模組可讀取 Prometheus 中任何 config 啟用的 Metric，但正式輸出的 `event_type` 必須已被 PRD-002 定義。若要新增正式 `event_type`，必須先更新 PRD-002。

6. **DB Pool 本階段僅收集與視覺化**

   `db_pool_active_connections` 可由 Metrics Generator 產生、
   Prometheus 收集並於 Grafana 顯示，但不納入 SPEC-002 或
   SPEC-003 v1.0 的正式 Event Detection 範圍。
---

## 1. 前置條件（Prerequisites）

開始本 SPEC 前，`develop` 必須已包含以下成果：

### 1.1 DDS-001 已完成

DDS-001 已提供：

- Log Generator
- Metrics Generator
- Prometheus
- Loki
- Promtail
- Grafana Dashboard
- Docker Compose

本 SPEC 僅依賴 DDS-001 的 Metrics Generator 與 Prometheus，不修改 DDS-001 產物。

### 1.2 SPEC-001 已完成並合併至 develop

SPEC-001 已完成 Log Event Detection，並提供以下可重用基礎：

- `src/event_detection/store/event_store.py`
- `src/event_detection/store/__init__.py`
- `events/` 已加入 `.gitignore`
- PRD-002 Event Schema 對齊策略

注意：

- 本專案目前沒有 `event_schema.py`，不得新增或假設存在此檔案。
- `src/event_detection/event/builder.py` 是 Log Event Detection 專用 EventBuilder，不得直接拿來建立 Metrics Threshold Event。
- 本 SPEC 應在 `metrics_threshold.py` 中自行實作 Metrics 專用的 Event 組裝邏輯。

---

## 2. 模組目標

Metrics Threshold Detection 的目標是：

1. 從 Prometheus HTTP API 定期讀取 Metrics。
2. 對 PRD-002 明確定義的 Metrics Threshold 條件執行靜態閾值判斷。
3. 當閾值被觸發時，產生符合 PRD-002 Event Schema 的 Metrics Event。
4. 將 Event 透過 SPEC-001 已實作的 `EventStore` append 寫入 `events/event_store.jsonl`。
5. 不直接產生 Alert、不寄送 Email、不呼叫 LLM、不進行 RCA。

---

## 3. 系統定位

本模組位於 Event Detection Layer 的 Metrics Threshold 分支。

```text
Logs
  │
  ▼
Log Event Detection（SPEC-001）
  │
  ├──────────────┐
                 ▼
              Event Store / Event Queue
                 ▲
  ┌──────────────┘
  │
Metrics
  │
  ├── Metrics Threshold Detection（SPEC-002，本文件）
  │
  └── Metrics Isolation Forest Detection（SPEC-003）
```

### 3.1 本模組負責

- Prometheus Instant Query
- Metrics 數值解析
- 靜態閾值判斷
- 冷卻期管理
- Metrics Threshold Event 組裝
- 寫入 EventStore
- 單元測試與 mock Prometheus response 測試

### 3.2 本模組不負責

- 修改 Metrics Generator
- 修改 Log Generator
- 修改 Docker / Prometheus / Grafana 設定
- Log Event Detection
- Metrics Isolation Forest Detection
- Event Runner 整合三條 detection pipeline
- Alert Correlation
- Incident Manager
- LLM / RAG
- Dashboard / Email
- 新增 PRD-002 未定義的正式 event_type

---

## 4. Input 設計

### 4.1 Prometheus HTTP API

資料來源為 DDS-001 已建立的 Prometheus。

Prometheus 由 DDS-001 的 Metrics Generator 暴露 exporter 資料，並提供 HTTP API 給本模組查詢。

| 項目 | 規格 |
|---|---|
| Base URL | `http://localhost:9090`，可由 config 覆寫 |
| API Endpoint | `GET /api/v1/query` |
| Query Type | Instant Query |
| Timeout | 預設 5 秒，可由 config 覆寫 |

### 4.1.1 與 DDS-001 的關係

本模組不重新定義 Metrics Generator、Prometheus、Docker Compose 或 Grafana。

上述基礎設施已由 DDS-001 完成。本 SPEC 僅依賴 DDS-001 已提供的 Prometheus HTTP API 讀取 Metrics，並將異常 Metrics 轉換為符合 PRD-002 Event Schema 的 Event。

若 DDS-001 的 Metrics 名稱、Exporter Port、Prometheus 設定或啟動流程發生變更，應先更新 DDS-001，再同步更新本 SPEC 的 Input 定義。

### 4.2 DDS-001 已定義 Metrics

DDS-001 目前定義以下 Metrics：

| Metric | 說明 | 本 SPEC 處理方式 |
|---|---|---|
| `system_memory_usage_pct` | Memory 使用率 | 正式 Threshold Event：`high_memory_detected` |
| `api_p95_latency_ms` | API p95 Latency | 正式 Threshold Event：`high_latency_detected` |
| `api_requests_per_sec` | QPS | 不輸出 Threshold Event；交由 SPEC-003 處理 Request Spike 與未知 QPS Window 異常 |
| `db_pool_active_connections` | DB Pool Active Connections | 僅收集與視覺化；不納入 SPEC-002 或 SPEC-003正式 Event Detection |

### 4.3 Prometheus Instant Query 格式

Request：

```text
GET http://localhost:9090/api/v1/query?query=<metric_name>
```

成功且有資料：

```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {
        "metric": {},
        "value": [1720000000.0, "92.5"]
      }
    ]
  }
}
```

成功但無資料：

```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": []
  }
}
```

### 4.4 Metrics 測試資料策略

本 SPEC 不共用 SPEC-001 的 Log fixtures。

原因：SPEC-001 fixtures 是 Log Event Detection 的暫時性測試資料，資料型態為 JSON Lines Logs；本模組輸入是 Prometheus HTTP API response 或 MetricValue dataclass，兩者測試資料型態不同。

本 SPEC 採兩層測試資料策略：

#### 4.4.1 單元測試

單元測試不得依賴 Docker、Prometheus 或 Metrics Generator。

測試方式可使用：

- `unittest.mock.patch("requests.get")`
- 直接建立 `MetricValue`
- `tmp_path` 建立臨時 EventStore
- 本 SPEC 專用 JSON fixture

建議可新增：

```text
tests/fixtures/metrics_threshold/prometheus_memory_high.json
tests/fixtures/metrics_threshold/prometheus_latency_high.json
tests/fixtures/metrics_threshold/prometheus_empty_result.json
tests/fixtures/metrics_threshold/prometheus_nan_result.json
```

上述 fixture 不是必要，但若實作者認為有助於維護測試，可新增。

#### 4.4.2 整合測試

整合測試可依賴 DDS-001 的 Docker Compose、Metrics Generator 與 Prometheus。

整合測試只作為人工驗證或 optional test，不得成為 `python -m pytest -q` 的必要條件。

---

## 5. 正式 Threshold Event 範圍

本 SPEC 只輸出 PRD-002 已明確定義為 Threshold 的 Metrics Event。

| Scenario | Metric | Trigger | Event Type | Detection Method | Severity |
|---|---|---|---|---|---|
| S2 DB 慢查詢補強 | `api_p95_latency_ms` | `>= 3000.0` | `high_latency_detected` | `threshold` | HIGH |
| S3 OOM 補強 | `system_memory_usage_pct` | `>= 90.0` | `high_memory_detected` | `threshold` | HIGH |

### 5.1 Threshold Comparison Rule

所有啟用且 `threshold_type` 為 `upper` 的 Threshold Rule，統一使用大於或等於比較：

```python
current_value >= threshold
邊界行為定義如下：

current_value < threshold：不觸發 Event。
current_value == threshold：觸發 Event。
current_value > threshold：觸發 Event。

因此：

system_memory_usage_pct == 90.0 時，必須觸發 high_memory_detected。
api_p95_latency_ms == 3000.0 時，必須觸發 high_latency_detected。

實作者不得將比較條件改為單純的 current_value > threshold。
```
### 5.2 不在本 SPEC 正式輸出範圍的 Metrics

### 5.2 不在本 SPEC 正式輸出範圍的 Metrics

| Metric | 原因 | 處理方式 |
|---|---|---|
| `api_requests_per_sec` | 屬於動態基準與 QPS Window 異常偵測 | 交由 SPEC-003 v1.0；可輸出 `request_spike_detected` 或 `general_metrics_anomaly` |
| `db_pool_active_connections` | PRD-002 v1.1 定義為本階段僅收集與視覺化 | 維持 disabled；不由 SPEC-002 或 SPEC-003 v1.0 輸出正式 Event |

### 5.3 六大情境與非六情境資料處理精神

六大情境是本階段 Demo Validation Set，不代表 Prometheus 只能存在六大情境相關 Metrics。

本模組可以接受 config 中啟用的非六情境 Metrics，也可以在未來擴充更多 metric query。

然而，由於本 SPEC 是靜態閾值偵測，
正式輸出的 `event_type` 必須已被 PRD-002 定義。
不得在未更新 PRD-002 的情況下自行新增正式 Event Type。

不同類型異常的處理方式如下：

1. 已定義 Memory／Latency Threshold：由 SPEC-002 輸出正式 Event。
2. QPS 動態異常：由 SPEC-003 v1.0 處理。
3. 未知 QPS Window 異常：由 SPEC-003 v1.0 輸出 `general_metrics_anomaly`。
4. DB Pool：本階段僅收集與視覺化，不輸出 Event。
5. 其他未定義 Metrics：可記錄 Debug Log 或提出 PRD 更新需求，
   但不得直接輸出未定義的正式 Event。

`general_metrics_anomaly` 僅適用於 `api_requests_per_sec`，
不得作為所有未知 Metrics 的通用 fallback。

未知或非正式 Metrics 異常的處理方式：

1. 可在 debug log 中記錄。
2. 可保留於未來 SPEC-003 Metrics IForest 設計。
3. 可提出 PRD-002 更新需求。
4. 不得直接輸出未定義的正式 Event。

---

## 6. Output 設計

### 6.1 Event Schema

本模組輸出的 Event 必須完全符合 PRD-002 第 5 章定義的 15 個 top-level 欄位。

不得新增 top-level 欄位，例如：

- `scenario_id`
- `metric_group`
- `window_start`
- `window_end`
- `root_cause`
- `alert_id`
- `incident_id`

若需要補充資訊，應放入 `triggered_features`。

### 6.2 Metrics Threshold Event 範例

```json
{
  "event_id": "EVT-1720000001234-a3f9",
  "detected_at": "2026-07-17T10:00:01.234Z",
  "event_source": "metrics_threshold_detection",
  "event_type": "high_memory_detected",
  "detection_method": "threshold",
  "severity": "HIGH",
  "confidence": 1.0,
  "service_name": "metrics",
  "trace_id": null,
  "source_ip": null,
  "downstream_service": null,
  "external_service": null,
  "status": "OPEN",
  "triggered_features": {
    "metric_name": "system_memory_usage_pct",
    "current_value": 92.5,
    "threshold_value": 90.0,
    "threshold_type": "upper",
    "exceeded_by": 2.5
  },
  "raw_log_sample": []
}
```

### 6.3 固定欄位值

| 欄位 | 固定值 / 規則 | 說明 |
|---|---|---|
| `event_source` | `metrics_threshold_detection` | 與 SPEC-003 `metrics_iforest_detection` 區分 |
| `detection_method` | `threshold` | 靜態閾值判斷 |
| `confidence` | `1.0` | 閾值判斷為確定性規則 |
| `service_name` | `metrics` | Metrics Event 非單一 AP service |
| `trace_id` | `null` | Metrics 無 trace |
| `source_ip` | `null` | Metrics 無 client IP |
| `downstream_service` | `null` | 本 SPEC 不推論下游根因 |
| `external_service` | `null` | 本 SPEC 不推論外部依賴 |
| `status` | `OPEN` | 新事件預設狀態 |
| `raw_log_sample` | `[]` | Metrics Event 無原始 Log |

### 6.4 `triggered_features` 欄位

每個 Metrics Threshold Event 的 `triggered_features` 必須包含：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `metric_name` | string | 觸發閾值的 Prometheus metric name |
| `current_value` | float | 查詢到的目前數值 |
| `threshold_value` | float | 被觸發的閾值 |
| `threshold_type` | string | 固定為 `upper` |
| `exceeded_by` | float | `current_value - threshold_value` |

可選欄位：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `query_timestamp` | string | Prometheus value[0] timestamp，若有解析可放入 |
| `prometheus_base_url` | string | Debug 用，不建議預設輸出 |

---

## 7. Threshold Config 設計

### 7.1 Config 路徑

```text
configs/thresholds.yaml
```

所有閾值必須集中於此，不得 hardcode 在 Python 程式碼中。

### 7.2 `configs/thresholds.yaml` 建議內容

```yaml
# configs/thresholds.yaml
# Metrics Threshold Detection 設定

prometheus:
  base_url: "http://localhost:9090"
  query_timeout_seconds: 5

polling:
  interval_seconds: 15

cooldown:
  seconds: 60

output:
  event_store_path: "events/event_store.jsonl"

metrics:
  system_memory_usage_pct:
    enabled: true
    threshold_type: "upper"
    threshold: 90.0
    event_type: "high_memory_detected"
    severity: "HIGH"

  api_p95_latency_ms:
    enabled: true
    threshold_type: "upper"
    threshold: 3000.0
    event_type: "high_latency_detected"
    severity: "HIGH"

  api_requests_per_sec:
  enabled: false
  note: "QPS dynamic baseline, request_spike_detected, and general_metrics_anomaly belong to SPEC-003 v1.0."

db_pool_active_connections:
  enabled: false
  note: "Observation and visualization only in v1.0. No formal Event Detection output."
```

### 7.3 Config 驗證規則

`ConfigLoader` 應至少驗證：

1. `prometheus.base_url` 存在。
2. `prometheus.query_timeout_seconds` 為正整數或正浮點數。
3. `polling.interval_seconds` 為正整數或正浮點數。
4. `cooldown.seconds` 為大於等於 0 的數字。
5. `output.event_store_path` 存在。
6. `metrics` 至少有一個 enabled metric。
7. 每個 enabled metric 必須包含：
   - `threshold_type`
   - `threshold`
   - `event_type`
   - `severity`
8. `threshold_type` 本版只接受 `upper`。
9. `severity` 必須是 `CRITICAL`、`HIGH`、`MEDIUM`、`LOW` 之一。
10. 本版正式允許的 `event_type` 僅限：
    - `high_memory_detected`
    - `high_latency_detected`

若 config 中啟用 PRD-002 未定義的正式 `event_type`，應在啟動時 raise `ValueError`，避免產生不符合 PRD 的 Event。

---

## 8. Event Flow

```text
[Scheduler / Manual run_once]
        │
        ▼
[MetricsFetcher.fetch_all]
        │
        ├─ Query system_memory_usage_pct
        ├─ Query api_p95_latency_ms
        └─ Skip disabled metrics
        │
        ▼
[ThresholdEvaluator.evaluate_all]
        │
        ├─ value >= threshold → ThresholdResult
        └─ value < threshold  → None
        │
        ▼
[CooldownManager.filter]
        │
        ├─ same event_type within cooldown → skip
        └─ not in cooldown → pass
        │
        ▼
[MetricsThresholdEventBuilder.build]
        │
        ▼
[EventStore.write]
        │
        ▼
events/event_store.jsonl
```

---

## 9. Folder Structure

### 9.1 允許新增或修改

```text
configs/
└── thresholds.yaml

src/
└── event_detection/
    └── metrics_threshold.py

tests/
├── test_metrics_threshold.py
└── fixtures/
    └── metrics_threshold/                 # optional
        ├── prometheus_memory_high.json
        ├── prometheus_latency_high.json
        ├── prometheus_empty_result.json
        └── prometheus_nan_result.json
```

### 9.2 可重用但不得修改

```text
src/event_detection/store/event_store.py
src/event_detection/store/__init__.py
```

### 9.3 禁止修改

```text
docker-compose.yml
docker/
README.md
CONTRIBUTING.md
src/log_generator/
src/metrics_generator/
src/event_detection/log/
src/event_detection/model/
src/event_detection/event/builder.py
src/event_detection/runner.py
docs/prd/
docs/spec/SPEC-001_Log_Event_Detection.md
```

若實作時發現必須修改禁止區域，應停止實作並通知 PM，不得自行擴大範圍。

---

## 10. Git Branch 與 PM 流程

### 10.1 開發分支

本 SPEC 使用既有分支：

```text
feature/metrics-threshold
```

### 10.2 開始前同步 develop

由 PM 先確認 `develop` 已包含 SPEC-001 成果，並同步到 `feature/metrics-threshold`。

建議 PM 執行：

```powershell
git checkout develop
git pull origin develop

git checkout feature/metrics-threshold
git pull origin feature/metrics-threshold
git merge --no-ff develop -m "merge: sync develop into metrics threshold branch"
python -m pytest -q
git push origin feature/metrics-threshold
```

### 10.3 實作者開發流程

實作者只在 `feature/metrics-threshold` 開發。

```powershell
git checkout feature/metrics-threshold
git pull origin feature/metrics-threshold
```

完成後：

```powershell
python -m pytest -q
git status
git add configs/thresholds.yaml src/event_detection/metrics_threshold.py tests/test_metrics_threshold.py tests/fixtures/metrics_threshold
git commit -m "feat: implement metrics threshold detection"
git push origin feature/metrics-threshold
```

### 10.4 禁止事項

實作者不得：

- merge 回 `develop`
- merge 到 `main`
- 自行建立 Pull Request 到 `develop`
- 修改其他 feature branch
- 使用 `git add .` 加入未確認檔案
- commit `events/event_store.jsonl`
- commit `.venv/`
- commit `.pkl` model file
- commit Docker 或 Dashboard 修改

`develop` 分支統一由 PM review 後 merge。

---

## 11. Interface 設計

本 SPEC 建議以單一檔案實作：

```text
src/event_detection/metrics_threshold.py
```

此檔案包含以下 class / dataclass：

```text
ConfigLoader
MetricValue
MetricsFetcher
ThresholdResult
ThresholdEvaluator
CooldownManager
MetricsThresholdEventBuilder
MetricsThresholdDetector
```

### 11.1 `MetricValue`

```python
@dataclass
class MetricValue:
    name: str
    value: float | None
    queried_at: str
    query_timestamp: float | None = None
    error: str | None = None

    @property
    def is_available(self) -> bool:
        return self.value is not None and self.error is None
```

### 11.2 `ThresholdResult`

```python
@dataclass
class ThresholdResult:
    metric_name: str
    current_value: float
    threshold_value: float
    threshold_type: str
    exceeded_by: float
    event_type: str
    severity: str
    queried_at: str
    query_timestamp: float | None = None
```

### 11.3 `ConfigLoader`

職責：

- 讀取 `configs/thresholds.yaml`
- 驗證必要欄位
- 阻止 PRD-002 未定義的正式 `event_type`
- 回傳 config dict

行為：

- 設定檔不存在：raise `FileNotFoundError`
- YAML 格式錯誤：raise `yaml.YAMLError`
- 缺少必要欄位：raise `ValueError`
- 啟用未允許 event_type：raise `ValueError`

### 11.4 `MetricsFetcher`

職責：

- 呼叫 Prometheus Instant Query
- 解析 response
- 回傳 `MetricValue`
- 不因單一 query 失敗中斷整體流程

行為規範：

1. 對每個 metric 使用 `GET /api/v1/query`。
2. 若 `result` 為空，回傳 `MetricValue(value=None, error=None)`。
3. 若 value 是 `NaN`、`Inf`、`-Inf`、`+Inf`，視為 unavailable。
4. 若 request timeout，回傳 `MetricValue(value=None, error="...")`。
5. 若 connection error，回傳 `MetricValue(value=None, error="...")`。
6. 若 JSON 格式錯誤，回傳 `MetricValue(value=None, error="...")`。
7. 若 Prometheus 回傳多筆 result，本版取第一筆。

### 11.5 `ThresholdEvaluator`

職責：

- 對 available metric value 執行 threshold 比對
- 只處理 enabled metric
- 回傳 `ThresholdResult` 或 `None`

比對邏輯：

```text
if metric_value.is_available is False:
    return None

if metric config enabled is False:
    return None

if threshold_type == "upper" and current_value >= threshold:
    return ThresholdResult

return None
```

### 11.6 `CooldownManager`

職責：

- 防止同一 `event_type` 在短時間內重複寫入
- 以 `event_type` 作為冷卻期 key

行為：

- 首次觸發：允許
- 同一 `event_type` 在 `cooldown.seconds` 內再次觸發：跳過
- 不同 `event_type` 互不影響

### 11.7 `MetricsThresholdEventBuilder`

職責：

- 將 `ThresholdResult` 轉換為 PRD-002 Event Schema
- 不依賴 SPEC-001 Log EventBuilder
- 不新增 top-level 欄位

輸出固定欄位：

```python
{
    "event_source": "metrics_threshold_detection",
    "detection_method": "threshold",
    "confidence": 1.0,
    "service_name": "metrics",
    "trace_id": None,
    "source_ip": None,
    "downstream_service": None,
    "external_service": None,
    "status": "OPEN",
    "raw_log_sample": [],
}
```

### 11.8 `MetricsThresholdDetector`

職責：

- 組裝完整流程
- 提供 `run_once()` 給測試與手動執行
- 提供 `start()` 給 polling 執行

流程：

```text
fetch_all
→ evaluate_all
→ cooldown filter
→ build event
→ EventStore.write
→ record cooldown
→ return fired events
```

`run_once()` 必須回傳本次產生的 events list。

---

## 12. Error Handling

### 12.1 錯誤處理原則

本模組採用靜默降級策略：單一 Metric 查詢失敗不得中斷整體流程。

| 錯誤情境 | 處理方式 |
|---|---|
| Prometheus 連線失敗 | 回傳 `MetricValue(error=...)`，跳過該 Metric |
| Prometheus timeout | 回傳 `MetricValue(error=...)`，跳過該 Metric |
| Prometheus 無資料 | `value=None`，不觸發 |
| Metric value 為 NaN / Inf | `value=None`，不觸發 |
| Config 不存在 | 啟動時 raise `FileNotFoundError` |
| Config 格式錯誤 | 啟動時 raise error |
| Config event_type 未被允許 | 啟動時 raise `ValueError` |
| EventStore 寫入失敗 | log error，`run_once()` 不應造成整體測試 crash；可視實作回傳已成功 events |
| 未知錯誤 | `start()` 捕捉並 log，下一輪繼續 |

### 12.2 Log Level 規範

| Level | 使用情境 |
|---|---|
| DEBUG | 每次 query 結果、正常無事件 |
| INFO | 啟動、設定載入成功 |
| WARNING | Event 觸發 |
| ERROR | Prometheus 失敗、EventStore 失敗、未知錯誤 |

---

## 13. 測試規格

### 13.1 必要測試檔

```text
tests/test_metrics_threshold.py
```

可選 fixture：

```text
tests/fixtures/metrics_threshold/
```

### 13.2 必測項目

#### ConfigLoader

1. 成功讀取合法 config。
2. config 不存在時 raise `FileNotFoundError`。
3. 缺少必要欄位時 raise `ValueError`。
4. 啟用未允許 event_type 時 raise `ValueError`。
5. disabled metric 不需要 event_type / threshold。

#### MetricsFetcher

1. Prometheus 成功回傳 float。
2. Prometheus result 為空時 value 為 None。
3. Prometheus value 為 `NaN` 時 value 為 None。
4. Prometheus timeout 不拋例外。
5. Prometheus connection error 不拋例外。
6. Prometheus JSON 格式異常不拋例外。

#### ThresholdEvaluator

1. Memory 90.0 觸發 `high_memory_detected`。
2. Memory 89.9 不觸發。
3. Latency 3000.0 觸發 `high_latency_detected`。
4. Latency 2999.9 不觸發。
5. unavailable metric 不觸發。
6. disabled metric 不觸發。
7. `exceeded_by` 計算正確。

#### CooldownManager

1. 首次觸發不在 cooldown。
2. 觸發後同一 event_type 進入 cooldown。
3. 不同 event_type cooldown 獨立。
4. cooldown seconds 到期後可再次觸發。

#### MetricsThresholdEventBuilder

1. Event top-level 欄位剛好等於 PRD-002 15 欄位。
2. `event_source == "metrics_threshold_detection"`。
3. `detection_method == "threshold"`。
4. `confidence == 1.0`。
5. `raw_log_sample == []`。
6. `triggered_features` 包含 `metric_name`、`current_value`、`threshold_value`、`threshold_type`、`exceeded_by`。
7. 不出現 `scenario_id`、`window_start`、`window_end` 等額外 top-level 欄位。

#### MetricsThresholdDetector

1. `run_once()` 正常情況回傳空 list。
2. `run_once()` memory high 時寫入 1 個 event。
3. `run_once()` latency high 時寫入 1 個 event。
4. 同一 event_type 在 cooldown 內不重複寫入。
5. 測試使用 `tmp_path` 作為 EventStore path，不寫入真實 `events/event_store.jsonl`。
6. 單元測試不得需要 Docker / Prometheus 實際運行。

### 13.3 測試指令

完整測試：

```powershell
python -m pytest -q
```

本 SPEC 測試：

```powershell
python -m pytest tests/test_metrics_threshold.py -q
```

---

## 14. 驗收標準

提交前必須符合：

1. `python -m pytest -q` 全部通過。
2. 沒有新增或修改禁止範圍檔案。
3. 沒有 hardcode threshold 數字在 evaluator 邏輯中。
4. Event Schema top-level 欄位剛好為 PRD-002 第 5 章定義的 15 欄位。
5. 只輸出 PRD-002 允許的 Threshold event_type：
   - `high_memory_detected`
   - `high_latency_detected`
6. `api_requests_per_sec` 不輸出 Threshold Event，
   其動態異常由 SPEC-003 負責。
7. `db_pool_active_connections` 僅作為收集與視覺化指標，
   必須維持 disabled，且不得由 SPEC-002 輸出 Threshold Event。
8. 測試不依賴真實 Prometheus。
9. EventStore 測試使用 `tmp_path`。
10. 無 `.venv/`、`events/event_store.jsonl`、`.pkl`、Docker、Dashboard 修改被 commit。

---

## 15. 人工整合測試（Optional）

人工整合測試可在本機 Docker 環境執行，不列入 CI 必要條件。

### 15.1 啟動 DDS-001 環境

```powershell
docker compose up -d
docker compose ps
```

確認以下服務為 Up：

```text
aiops-prometheus
aiops-loki
aiops-promtail
aiops-grafana
```

### 15.2 啟動 Metrics Generator

```powershell
python src/metrics_generator/metrics_generator.py
```

### 15.3 查詢 Prometheus

```powershell
curl "http://localhost:9090/api/v1/query?query=system_memory_usage_pct"
```

### 15.4 執行單次 Threshold Detection

```powershell
python -c "from src.event_detection.metrics_threshold import MetricsThresholdDetector; d=MetricsThresholdDetector('configs/thresholds.yaml'); print(d.run_once())"
```

正常情況下可能回傳空 list。若透過 Metrics Generator 觸發 S2 或 S3，應產生對應 Event。

---

## 16. Codex 實作前分析 Prompt

請先只分析，不要修改檔案。

```text
你正在實作 SPEC-002 Metrics Threshold Detection。

請先閱讀：
- docs/prd/PRD-001_AIOps_Platform.md
- docs/prd/PRD-002_Event_Detection.md
- docs/spec/SPEC-001_Log_Event_Detection.md
- docs/spec/SPEC-002_Metrics_Threshold_Detection.md
- DDS-001 相關文件（若存在 docs/dds/DDS-001.md）

請回覆：
1. 你會新增或修改哪些檔案？
2. 哪些既有檔案會被重用但不修改？
3. 你是否會修改 Docker、Generator、README、docs、Log Detection、Model、Runner？
4. Event Schema 15 個 top-level 欄位是什麼？
5. 本 SPEC 允許輸出的 event_type 有哪些？
6. api_requests_per_sec 與 db_pool_active_connections 是否會輸出 Threshold Event？為什麼？
7. 單元測試會如何避免依賴 Prometheus？
8. EventStore 測試會如何避免寫入真實 events/event_store.jsonl？

在 PM 確認前，不要修改任何檔案。
```

---

## 17. Codex 實作 Prompt

確認分析正確後，才使用以下 prompt。

```text
請依照 docs/spec/SPEC-002_Metrics_Threshold_Detection.md 實作 Metrics Threshold Detection。

允許新增或修改：
- configs/thresholds.yaml
- src/event_detection/metrics_threshold.py
- tests/test_metrics_threshold.py
- tests/fixtures/metrics_threshold/（optional）

可 import 但不得修改：
- src/event_detection/store/event_store.py

禁止修改：
- docker-compose.yml
- docker/
- README.md
- CONTRIBUTING.md
- docs/
- src/log_generator/
- src/metrics_generator/
- src/event_detection/log/
- src/event_detection/model/
- src/event_detection/event/builder.py
- src/event_detection/runner.py

實作要求：
1. 從 configs/thresholds.yaml 讀取 threshold，不得 hardcode。
2. 只允許輸出 high_memory_detected 與 high_latency_detected。
3. api_requests_per_sec 必須 disabled，不輸出 Threshold Event。
4. db_pool_active_connections 必須 disabled，不輸出 Event。
5. Event Schema top-level 欄位必須剛好符合 PRD-002 第 5 章 15 欄位。
6. event_source 必須是 metrics_threshold_detection。
7. detection_method 必須是 threshold。
8. 使用 EventStore 寫入 Event，但測試必須使用 tmp_path。
9. 單元測試不得依賴 Docker 或真實 Prometheus。
10. python -m pytest -q 必須通過。
11. 所有 upper threshold 比較必須使用 `current_value >= threshold`；數值等於 threshold 時必須觸發 Event，並加入等於門檻值的邊界測試。

完成後請回報：
- 新增或修改檔案
- 測試結果
- 是否有超出範圍
- 是否有任何未通過測試

不要執行 git add、git commit、git push。
```

---

## 18. Implementation Notes

### 18.1 為什麼不直接重用 SPEC-001 EventBuilder？

SPEC-001 的 `EventBuilder` 是為 Log Window、PredictionResult、WindowSummary 與 raw log sample 設計。

Metrics Threshold Detection 的輸入是 `ThresholdResult`，資料型態不同。若強行共用 Log EventBuilder，會造成介面混亂，並提高未來 SPEC-004 整合風險。

因此本 SPEC 僅重用通用的 `EventStore`，並在 `metrics_threshold.py` 中建立 Metrics 專用 Event Builder。

### 18.2 為什麼 `api_requests_per_sec` 不在本 SPEC 輸出？

PRD-002 將 S6 的 Metrics 補強定義為 `api_requests_per_sec` 短時間暴增超過基準值 3 倍，Detection Method 是 Isolation Forest。

這是動態基準與異常分布問題，不是靜態 threshold 問題，因此交由 SPEC-003。

### 18.3 為什麼 DB Pool 預設 disabled？

DDS-001 有提供 `db_pool_active_connections`，但 PRD-002 尚未定義正式 Threshold Event Type。

為避免 SPEC-002 擅自擴充產品需求，本版保留 config entry 但 disabled。若 PM 後續希望 DB Pool 也輸出正式 Event，應先更新 PRD-002 第 4 章與第 5 章，再更新本 SPEC。

---

## 19. 最終交付物

實作完成後，`feature/metrics-threshold` 應包含：

```text
configs/thresholds.yaml
src/event_detection/metrics_threshold.py
tests/test_metrics_threshold.py
```

可選：

```text
tests/fixtures/metrics_threshold/
```

不得包含：

```text
events/event_store.jsonl
.venv/
models/*.pkl
Docker 修改
Generator 修改
Dashboard 修改
README 修改
docs 修改（除 PM 事先安排）
```

---

## 20. PM Review Checklist

PM review 時檢查：

- [ ] 是否只修改允許檔案？
- [ ] `python -m pytest -q` 是否通過？
- [ ] `configs/thresholds.yaml` 是否只啟用 memory / latency？
- [ ] `api_requests_per_sec` 是否 disabled？
- [ ] `api_requests_per_sec` 是否 disabled，且沒有輸出 Threshold Event？
- [ ] `db_pool_active_connections` 是否維持 disabled，並明確定位為僅收集與視覺化？
- [ ] 是否沒有將 `general_metrics_anomaly` 實作在 SPEC-002？
- [ ] 是否沒有新增 PRD-002 未定義 event_type？
- [ ] Event Schema 是否剛好 15 個 top-level fields？
- [ ] `event_source` 是否為 `metrics_threshold_detection`？
- [ ] `detection_method` 是否為 `threshold`？
- [ ] 測試是否使用 mock Prometheus？
- [ ] EventStore 測試是否使用 `tmp_path`？
- [ ] 是否沒有修改 Docker / Generator / README / docs / runner？
- [ ] Threshold 比較是否使用 `current_value >= threshold`？
- [ ] Memory 90.0 與 Latency 3000.0 是否都會觸發 Event？
