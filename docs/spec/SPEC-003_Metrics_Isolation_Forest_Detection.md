# SPEC-003：Metrics Isolation Forest Detection

## Software Design Specification v1.1

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-003 |
| Document Name | Metrics Isolation Forest Detection |
| Version | 1.1 |
| Status | Implemented |
| Date | 2026-08-12 |
| Author | 林子豪（PM） |
| Assignee | Tako |
| Branch Metadata | `feature/metrics-iforest` |
| Related PRD | PRD-001 v3.2、PRD-002 current Approved version（v1.3） |
| Related DDS | DDS-001 |
| Related SPEC | SPEC-001、SPEC-002 v1.4、SPEC-004 |

| Version | Date | Change |
|---|---|---|
| 1.1 | 2026-08-12 | 更新 PRD-002 reference、釐清 config-driven request spike classification wording，並將 SPEC-005 integration 未來式更新為已完成的 non-normative validation evidence；Metrics IForest gate 與 fallback contract 不變。 |

> 本文件是 Metrics Isolation Forest Detection 的正式工程契約。Git 操作、Codex CLI 操作方式及完整 AI Coding Agent Prompt 不屬於本 SPEC，由 PM 透過獨立工作文件提供。

---

# 0. 文件目的與規格優先序

本文件定義 **Metrics Isolation Forest Detection** 模組的完整實作規格。

本模組正式 Runtime 以 Prometheus HTTP API 的 `query_range` 作為資料輸入介面，取得 `api_requests_per_sec` 的 QPS 時間序列，將固定時間範圍內的 Samples 聚合成 Window Features，再使用 Isolation Forest 判斷該 Window 是否偏離正常 QPS 行為。

當模型判定 Window 異常後，再由可測試的 Rule Classifier 指派事件語意：

```text
Isolation Forest
判斷「QPS Window 是否異常」
        │
        ├── 正常 → 不建立 Event
        │
        └── 異常
              │
              ▼
       Rule Classifier
       判斷「是否為已知 Request Spike」
              │
              ├── current_qps / baseline_mean >= configured request_spike_ratio
              │      → request_spike_detected
              │
              └── 其他異常
                     → general_metrics_anomaly
```

所有正式 Event 必須符合 **PRD-002 第 5 章 Event Schema**，並透過 SPEC-001 已建立的 `EventStore` 寫入 `events/event_store.jsonl`。

## 0.1 規格優先序

1. PRD-002 current Approved version 第 5 章 Event Schema 與正式 Event Type。
2. 本 SPEC-003 的模組邊界、介面、資料結構與驗收標準。
3. PRD-001 v3.2 的整體架構與產品範圍。
4. SPEC-001、SPEC-002 已完成的共用介面與既有測試。

若文件、既有程式碼或測試之間出現無法同時滿足的衝突，實作者與 AI Coding Agent 必須停止擴大修改並回報 PM，不得自行重新定義需求。

## 0.2 核心決策

1. 使用 Hybrid Pipeline：Isolation Forest 負責異常判定，Rule Classifier 負責已知事件分類。
2. SPEC-003 v1.1 只處理 `api_requests_per_sec`。
3. 正式 Runtime 使用 Prometheus `query_range`，不使用 Instant Query 作為模型推論輸入。
4. 已知 QPS Spike 輸出 `request_spike_detected`。
5. 未知 QPS Window 異常輸出 `general_metrics_anomaly`。
6. `general_metrics_anomaly` 僅代表 QPS Window 未知異常，不代表所有 Prometheus Metrics。
7. Rule Classifier 不得在 Isolation Forest 判定正常時單獨建立 Event。
8. 模型檔由 Training Script 產生，不納入版本控制。
9. 模型不存在時預設 Fail Fast，不得無聲退化為 Rule-only。
10. 單元測試、模型測試及必要驗收不得依賴 Docker、真實 Prometheus、Metrics Generator 或網路。
11. Log／Metrics Generator integration 已由 SPEC-005 v1.2 完成驗證；其結果僅作 non-normative evidence。
12. `db_pool_active_connections` 本階段僅收集與視覺化，不納入 SPEC-003 v1.1。

---

# 1. 系統定位與模組邊界

## 1.1 整體架構位置

```text
Logs → Log Event Detection（SPEC-001）──────────────┐
                                                     ▼
                                             Event Store / Queue
                                                     ▲
Metrics → Metrics Threshold Detection（SPEC-002）───┤
        → Metrics IForest Detection（SPEC-003）─────┘
                                                     │
                                                     ▼
                                             Event Runner（SPEC-004）
                                                     │
                                                     ▼
                                  Alert Correlation / Incident / RCA
```

SPEC-002 與 SPEC-003 是互補且彼此獨立的 Metrics Detection Pipeline：

| Pipeline | 正式範圍 |
|---|---|
| SPEC-002 Metrics Threshold | `api_p95_latency_ms`、`system_memory_usage_pct` 靜態 Threshold |
| SPEC-003 Metrics IForest | `api_requests_per_sec` 動態 QPS Window 異常 |

任一 Pipeline 判定異常時均可獨立建立 Event；同一事故可能同時產生多筆不同來源 Event，後續由 Event Runner 與 Alert Correlation 處理。

## 1.2 本模組負責

- 讀取並驗證 `configs/metrics_iforest.yaml`。
- 使用 Prometheus `query_range` 查詢 QPS 時間序列。
- 解析、清理、排序及去除重複 Samples。
- 建立固定 QPS Window。
- 提取固定順序的 Metrics Window Features。
- 以固定 Baseline Fixture 訓練 Isolation Forest。
- 儲存、載入並驗證 Model Artifact Metadata。
- 對 QPS Window 執行 Isolation Forest 推論。
- 對已判定異常的 Window 分類為已知 Request Spike 或未知 QPS 異常。
- 建立 `request_spike_detected` 或 `general_metrics_anomaly` Event。
- 寫入既有 `EventStore`。
- 執行 Event Cooldown。
- 提供 `run_once()` 供測試、手動執行及 SPEC-004 呼叫。
- 提供可重現的 Training Script、Validation Script、Fixtures 與 Unit Tests。

## 1.3 本模組不負責

- 修改 Log Generator 或 Metrics Generator。
- 修改 Docker Compose、Prometheus 或 Grafana 設定。
- 依賴目前 Generator 完整性才能完成單元測試。
- Memory、Latency 或 DB Pool Event Detection。
- Log Event Detection。
- Event Runner、Alert Correlation、Incident Manager。
- LLM、RAG、RCA、Dashboard 或 Email。
- 新增 PRD-002 未定義的 Event Type。
- 對所有 Prometheus Metrics 提供未知異常偵測。

---

# 2. 前置條件與依賴

## 2.1 前置條件

開始實作前，工作分支內容應包含：

- PRD-001 v3.2。
- PRD-002 current Approved version（v1.3）。
- SPEC-001 已完成成果。
- SPEC-002 v1.4 已完成成果。
- DDS-001 已建立的 Prometheus、Metrics Generator 與 Grafana 基礎。
- `requirements.txt` 已包含 `requests`。

Metrics Generator final integration validation 已由 SPEC-005 v1.2 完成；該 controller 與 Generator 行為不改變本 Detector requirement。

## 2.2 既有共用介面

本模組必須重用：

```python
from src.event_detection.store.event_store import EventStore
```

正式寫入介面：

```python
EventStore.write(event: dict) -> None
```

SPEC-003 不得直接重用 Log 專用 `EventBuilder`，應建立 Metrics IForest 專用 Event Builder。SPEC-003 亦不得修改或依賴 `metrics_threshold.py` 的內部 class。

## 2.3 Runtime Dependencies

| 套件 | 用途 |
|---|---|
| `requests` | 呼叫 Prometheus HTTP API |
| `numpy` | Feature 計算與數值處理 |
| `scikit-learn` | `IsolationForest` |
| `joblib` | Model Artifact 儲存與載入 |
| `PyYAML` | Config 讀取 |

上述套件由 `requirements.txt` 管理。本模組不新增 pandas、prometheus-api-client、statsmodels、TensorFlow 或 PyTorch。

## 2.4 Development Dependencies

測試使用 `pytest`，由 `requirements-dev.txt` 管理。Python Standard Library 可使用：

```text
argparse dataclasses datetime json logging math pathlib statistics
time typing uuid
```

## 2.5 依賴變更規則

- Tako 與 AI Coding Agent 不得自行修改 `requirements.txt` 或 `requirements-dev.txt`。
- 不得自行升級、降級或鎖定套件版本。
- 發現缺少依賴時停止擴大修改並回報 PM。
- 不得改用新的第三方 Prometheus Client。

---

# 3. Input 與 Output Contract

## 3.1 正式 Runtime Input

```text
Prometheus HTTP API
GET /api/v1/query_range
Metric：api_requests_per_sec
```

Prometheus Base URL 預設為 `http://localhost:9090`，可由 Config 覆寫。

## 3.2 開發與必要測試 Input

使用：

- Mock Prometheus Matrix Response。
- SPEC-003 專用 JSON Fixtures。
- 固定 QPS Baseline Windows。
- 固定 Random State。
- `tmp_path` 產生的臨時 Config、Model 與 EventStore。

必要測試不得要求 Docker、真實 Prometheus、Metrics Generator、Log Generator或外部網路。

## 3.3 正式 Output

```text
events/event_store.jsonl
```

寫入方式：

```python
EventStore.write(event)
```

`run_once()` 必須回傳本輪成功寫入的 Event list。本版只有一個 Metric，單輪最多成功建立一筆 Event。

## 3.4 正式 Event Type

只允許：

```text
request_spike_detected
general_metrics_anomaly
```

## 3.5 DB Pool 邊界

`db_pool_active_connections` 本階段可被產生、收集與顯示，但不作為模型輸入、不建立 Model、不輸出 Event，也不列為 SPEC-003 完成驗收條件。

---

# 4. 檔案範圍與 AI Agent 執行邊界

## 4.1 允許新增或修改

```text
configs/metrics_iforest.yaml
src/event_detection/metrics_iforest.py
scripts/train_metrics_model.py
scripts/validate_metrics_iforest.py
tests/test_metrics_iforest.py
tests/fixtures/metrics_iforest/
```

建議 Fixtures：

```text
qps_baseline.json
prometheus_qps_normal.json
prometheus_qps_spike.json
prometheus_qps_general_anomaly.json
prometheus_qps_empty.json
```

## 4.2 Runtime 產生但不得納入版本控制

```text
models/metrics_isolation_forest.pkl
```

若現有 `.gitignore` 未排除 `*.pkl` 或 `models/`，實作者不得自行修改 `.gitignore`，應回報 PM。

## 4.3 可讀取或 import，但不得修改

```text
src/event_detection/store/event_store.py
requirements.txt
requirements-dev.txt
.gitignore
```

## 4.4 禁止修改

```text
docker-compose.yml
docker/
README.md
CONTRIBUTING.md
docs/
src/log_generator/
src/metrics_generator/
src/event_detection/log/
src/event_detection/model/
src/event_detection/event/builder.py
src/event_detection/metrics_threshold.py
src/event_detection/runner.py
tests/test_metrics_threshold.py
```

## 4.5 AI Coding Agent 執行邊界

- 本 SPEC 是正式工程契約。
- AI Coding Agent 不得執行任何 Git 指令。
- 不得修改 Dependency Files 或禁止範圍。
- 不得新增 PRD-002 未定義的 Event Type。
- 不得新增、刪除或重新命名 Event Schema Top-level 欄位。
- 不得以 Rule-only 方式取代 Isolation Forest。
- 不得為通過測試而修改 Generator、SPEC-001 或 SPEC-002。
- 發現規格與既有程式衝突時，停止修改並列出衝突供 PM 判斷。

---

# 5. Config Specification

新增：

```yaml
# configs/metrics_iforest.yaml
prometheus:
  base_url: "http://localhost:9090"
  query_endpoint: "/api/v1/query_range"
  timeout_seconds: 5

metric:
  name: "api_requests_per_sec"

window:
  lookback_seconds: 300
  step_seconds: 15
  min_sample_count: 10

runtime:
  poll_interval_seconds: 15

isolation_forest:
  contamination: 0.05
  n_estimators: 200
  random_state: 42
  max_samples: "auto"

anomaly:
  score_threshold: -0.05
  confidence_medium_score: -0.10
  confidence_high_score: -0.30

classification:
  request_spike_ratio: 3.0
  known_event_type: "request_spike_detected"
  fallback_event_type: "general_metrics_anomaly"

event:
  cooldown_seconds: 60

model:
  path: "models/metrics_isolation_forest.pkl"
  metadata_version: "1.0"
  train_if_missing: false

training:
  baseline_fixture_path: "tests/fixtures/metrics_iforest/qps_baseline.json"
  minimum_window_count: 30

output:
  event_store_path: "events/event_store.jsonl"
```

`3.0` 是目前 Approved configuration／baseline value；正式 algorithm contract 為
`current_qps / baseline_mean >= configured request_spike_ratio`，不得在 Python
hardcode 為不可變常數。SPEC-005 scenario generator 的 4x 值只用於產生驗證輸入，
不是 classification requirement。

## 5.1 Config Loader

```python
class MetricsIForestConfigLoader:
    @staticmethod
    def load(config_path: str | Path) -> dict:
        ...
```

必須以 UTF-8 讀取 YAML；檔案不存在時 raise `FileNotFoundError`；YAML 錯誤保留原始 Exception Context；驗證完成後才回傳 dict。

## 5.2 Config Validation

至少驗證：

- Base URL 非空，Endpoint 固定 `/api/v1/query_range`。
- Timeout、Lookback、Step、Poll Interval 大於 0。
- `metric.name == api_requests_per_sec`。
- `min_sample_count > 1`。
- `lookback_seconds % step_seconds == 0`。
- `expected_sample_count = lookback_seconds / step_seconds + 1`，且 `expected_sample_count >= min_sample_count`。
- `0 < contamination < 0.5`，`n_estimators > 0`。
- `request_spike_ratio > 1.0`。
- Known／Fallback Event Type 分別為兩個正式值。
- `cooldown_seconds >= 0`。
- Model Path、Metadata Version、Baseline Fixture Path 非空。
- `train_if_missing` 為 Boolean。
- `minimum_window_count >= 10`。
- `confidence_high_score < confidence_medium_score < 0`。
- `score_threshold <= 0`。

Config 不合法時在建立 Runtime Loop 前 raise `ValueError`。

---

# 6. Input Data Format

## 6.1 Prometheus Range Query

使用：

```text
GET /api/v1/query_range
```

Query Parameters：

| 參數 | 值或規則 |
|---|---|
| `query` | `api_requests_per_sec` |
| `start` | `end - lookback_seconds` |
| `end` | 本輪執行時 UTC Unix Timestamp |
| `step` | `step_seconds`，預設 15 |

HTTP 呼叫必須使用：

```python
requests.get(
    url,
    params=params,
    timeout=timeout_seconds,
)
```

不得手動拼接未編碼 Query String。

## 6.2 成功 Response

```json
{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [
      {
        "metric": {},
        "values": [
          [1720000000, "10.0"],
          [1720000015, "11.0"],
          [1720000030, "9.5"],
          [1720000045, "12.0"],
          [1720000060, "40.0"]
        ]
      }
    ]
  }
}
```

## 6.3 無資料 Response

```json
{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": []
  }
}
```

無資料時不推論、不輸出 Event、不寫入 EventStore，`run_once()` 回傳 `[]`。

## 6.4 多 Series Response

SPEC-003 v1.1 預期只回傳一個 Series。若超過一個 Series：

- 不得任意取第一個 Series。
- 不得將不同 Label Series 自行相加或平均。
- 記錄 ERROR。
- 本輪不推論，回傳 `[]`。

未來支援多服務 QPS 時，必須另行定義 Label-aware Window、Model Ownership 與 `service_name` Mapping。

## 6.5 Baseline Fixture Format

`qps_baseline.json` 使用：

```json
{
  "metric_name": "api_requests_per_sec",
  "step_seconds": 15,
  "windows": [
    {
      "start_timestamp": 1720000000,
      "values": [10.0, 10.5, 9.8, 11.0, 10.2, 10.7, 9.9, 10.4, 10.1, 10.6, 10.3, 9.7, 10.2, 10.8, 10.0, 10.4, 9.9, 10.5, 10.1, 10.3, 10.2]
    },
    {
      "start_timestamp": 1720000300,
      "values": [11.0, 10.8, 11.2, 10.9, 11.1, 10.7, 11.0, 11.3, 10.8, 11.1, 10.9, 11.2, 10.8, 11.0, 11.1, 10.7, 11.3, 10.9, 11.0, 11.2, 10.8]
    }
  ]
}
```

正式 Fixture 必須：

- Metric Name 正確。
- `step_seconds > 0`。
- 每個 Window 有唯一 Start Timestamp。
- 每個 Baseline Window 必須有 `expected_sample_count = lookback_seconds / step_seconds + 1` 筆有效值。
- 清理後至少產生 `minimum_window_count` 個 Feature Vectors。
- 主要代表正常 QPS，不包含主要 S6 Spike。
- 不在 Runtime 動態使用亂數產生訓練資料。
- 可被所有團隊成員重現。

Timestamp 建立規則：

```python
timestamp = start_timestamp + index * step_seconds
```

## 6.6 Prometheus Fixtures

Fixtures 維持真實 `query_range` Matrix 結構：

| Fixture | 用途 |
|---|---|
| `prometheus_qps_normal.json` | 正常 QPS Window |
| `prometheus_qps_spike.json` | 明顯 Request Spike |
| `prometheus_qps_general_anomaly.json` | 模型異常但最新值不足 Baseline 3 倍 |
| `prometheus_qps_empty.json` | 空 Result |

---

# 7. Data Structures 與 Exceptions

## 7.1 `MetricSample`

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class MetricSample:
    timestamp: float
    value: float
```

## 7.2 `MetricWindow`

```python
@dataclass(frozen=True)
class MetricWindow:
    metric_name: str
    start_timestamp: float
    end_timestamp: float
    samples: list[MetricSample]
```

規則：Samples 依 Timestamp 升冪；Start／End 為第一與最後有效 Sample；Metric 固定為 `api_requests_per_sec`。

## 7.3 `MetricsWindowFeatures`

```python
@dataclass(frozen=True)
class MetricsWindowFeatures:
    current_value: float
    mean_value: float
    std_value: float
    min_value: float
    max_value: float
    median_value: float
    first_value: float
    last_value: float
    max_to_mean_ratio: float
    current_to_mean_ratio: float
    slope: float
    sample_count: int

    def to_list(self) -> list[float]:
        return [
            self.current_value,
            self.mean_value,
            self.std_value,
            self.min_value,
            self.max_value,
            self.median_value,
            self.first_value,
            self.last_value,
            self.max_to_mean_ratio,
            self.current_to_mean_ratio,
            self.slope,
            float(self.sample_count),
        ]

    @staticmethod
    def feature_names() -> list[str]:
        return [
            "current_value",
            "mean_value",
            "std_value",
            "min_value",
            "max_value",
            "median_value",
            "first_value",
            "last_value",
            "max_to_mean_ratio",
            "current_to_mean_ratio",
            "slope",
            "sample_count",
        ]
```

Feature 名稱、數量與順序是 Model Contract；訓練與推論必須完全一致。

## 7.4 `MetricsPredictionResult`

```python
@dataclass(frozen=True)
class MetricsPredictionResult:
    is_anomaly: bool
    model_label: int
    anomaly_score: float
    confidence: float
    features: MetricsWindowFeatures
```

## 7.5 `MetricsClassificationResult`

```python
@dataclass(frozen=True)
class MetricsClassificationResult:
    event_type: str
    severity: str
    classification_reason: str
    baseline_mean: float
    spike_ratio: float | None
    baseline_zero: bool
```

`spike_ratio=None` 只表示 Baseline 為 0 且 Ratio 無法以有限 JSON Number 表示。

## 7.6 Required Exceptions

```python
class MetricsIForestError(Exception):
    pass

class MetricsIForestModelNotFoundError(MetricsIForestError):
    pass

class MetricsIForestModelLoadError(MetricsIForestError):
    pass

class MetricsIForestModelVersionError(MetricsIForestError):
    pass

class MetricsIForestTrainingDataError(MetricsIForestError):
    pass
```

---

# 8. Required Classes 與 Interface

主要 Class 集中於：

```text
src/event_detection/metrics_iforest.py
```

## 8.1 `MetricsIForestConfigLoader`

```python
class MetricsIForestConfigLoader:
    @staticmethod
    def load(config_path: str | Path) -> dict:
        ...
```

責任：讀取、驗證並回傳 Config。

## 8.2 `PrometheusRangeClient`

```python
class PrometheusRangeClient:
    def __init__(self, base_url: str, endpoint: str, timeout_seconds: float):
        ...

    def fetch_samples(
        self,
        metric_name: str,
        start_timestamp: float,
        end_timestamp: float,
        step_seconds: int,
    ) -> list[MetricSample]:
        ...
```

責任：HTTP Query、Response 驗證、單一 Series 解析、無效值過濾、排序與 Timestamp 去重。Recoverable Runtime Error 時記錄 Log 並回傳 `[]`。

## 8.3 `MetricWindowBuilder`

```python
class MetricWindowBuilder:
    def build(
        self,
        metric_name: str,
        samples: list[MetricSample],
        min_sample_count: int,
    ) -> MetricWindow | None:
        ...
```

Sample 不足時回傳 `None`。

## 8.4 `MetricsFeatureExtractor`

```python
class MetricsFeatureExtractor:
    def extract(self, window: MetricWindow) -> MetricsWindowFeatures:
        ...
```

依第 10 章計算固定 Features，不產生 NaN 或 Inf。

## 8.5 `MetricsIForestTrainer`

```python
class MetricsIForestTrainer:
    def train_from_fixture(self, fixture_path: str | Path) -> dict:
        ...

    def save_artifact(self, artifact: dict, model_path: str | Path) -> None:
        ...
```

責任：讀取 Fixture、建立 Feature Matrix、驗證 Window Count、Fit Model、建立並儲存 Artifact。

## 8.6 `MetricsIForestModelLoader`

```python
class MetricsIForestModelLoader:
    def load(self, model_path: str | Path) -> dict:
        ...
```

責任：載入可信本機 Artifact，驗證 Metadata、Metric、Feature Names、Model Interface 與 sklearn Version。

## 8.7 `MetricsIForestPredictor`

```python
class MetricsIForestPredictor:
    def __init__(self, artifact: dict, anomaly_config: dict):
        ...

    def predict(self, features: MetricsWindowFeatures) -> MetricsPredictionResult:
        ...
```

## 8.8 `MetricsIForestClassifier`

```python
class MetricsIForestClassifier:
    def classify(
        self,
        window: MetricWindow,
        prediction: MetricsPredictionResult,
    ) -> MetricsClassificationResult | None:
        ...
```

模型正常時必須回傳 `None`。

## 8.9 `MetricsIForestEventBuilder`

```python
class MetricsIForestEventBuilder:
    def build(
        self,
        window: MetricWindow,
        prediction: MetricsPredictionResult,
        classification: MetricsClassificationResult,
    ) -> dict:
        ...
```

## 8.10 `MetricsIForestCooldownManager`

```python
class MetricsIForestCooldownManager:
    def should_fire(
        self,
        event_type: str,
        metric_name: str,
        now_timestamp: float,
    ) -> bool:
        ...

    def record_fired(
        self,
        event_type: str,
        metric_name: str,
        fired_at: float,
    ) -> None:
        ...
```

## 8.11 `MetricsIForestDetector`

```python
class MetricsIForestDetector:
    def __init__(
        self,
        config_path: str = "configs/metrics_iforest.yaml",
        event_store: EventStore | None = None,
    ):
        ...

    def run_once(self) -> list[dict]:
        ...

    def start(self) -> None:
        ...
```

Detector 整合 Config、Model、Prometheus Client、Window、Features、Prediction、Classification、Cooldown、EventBuilder 與 EventStore。

---

# 9. Sample Parsing 與 Window 建立

## 9.1 Sample Validation

以下資料不得進入模型：

- NaN、Inf、`-Inf`。
- 無法轉為 Float 的 Timestamp 或 Value。
- 缺少 Timestamp／Value、空值。
- QPS Value 小於 0。

無效 Sample 跳過並記錄 DEBUG，不得使整輪崩潰。

## 9.2 排序與去重

1. 依 Timestamp 升冪排序。
2. 同一 Timestamp 多筆有效值時，保留 Response 中最後一筆有效值。
3. 建立新的排序後 List。

## 9.3 最低 Sample 數量

當 `len(valid_samples) < min_sample_count`：不建立 Window、不推論、不輸出 Event，`run_once()` 回傳 `[]`。

## 9.4 Window Boundary

```python
start_timestamp = samples[0].timestamp
end_timestamp = samples[-1].timestamp
```

使用實際有效 Sample 邊界，不使用 Query 的理論 Start／End 取代。

---

# 10. Feature Extraction

## 10.1 Feature Contract

固定 12 個 Features：

```text
current_value
mean_value
std_value
min_value
max_value
median_value
first_value
last_value
max_to_mean_ratio
current_to_mean_ratio
slope
sample_count
```

令：

```python
values = [sample.value for sample in window.samples]
```

則：

```python
current_value = values[-1]
mean_value = numpy.mean(values)
std_value = numpy.std(values, ddof=0)
min_value = min(values)
max_value = max(values)
median_value = numpy.median(values)
first_value = values[0]
last_value = values[-1]
sample_count = len(values)
```

`std_value` 使用母體標準差 `ddof=0`。

## 10.2 Ratio Features

當 `mean_value > 0`：

```python
max_to_mean_ratio = max_value / mean_value
current_to_mean_ratio = current_value / mean_value
```

當 `mean_value == 0`：

```python
max_to_mean_ratio = 0.0
current_to_mean_ratio = 0.0
```

Feature Ratio 的 Mean 包含目前最後一筆；第 13 章的 `baseline_mean` 排除最後一筆，兩者不得混用。

## 10.3 Slope

```python
slope = (last_value - first_value) / max(sample_count - 1, 1)
```

單位為每一筆有效 Sample 的平均 QPS 變化量。

## 10.4 Finite Value Guarantee

回傳前驗證：

- 所有 Float Features 為 Finite Number。
- `sample_count` 為正整數。
- `len(to_list()) == len(feature_names()) == 12`。

不符合時 raise `ValueError`，不得傳入模型。

---

# 11. Isolation Forest Training 與 Model Artifact

## 11.1 Model Responsibility

模型只判斷 QPS Window 是否偏離正常行為，不負責 Event 語意、根因、Incident、Alert 或 RCA。

## 11.2 Training Flow

```text
read qps_baseline.json
→ validate fixture
→ build MetricWindow list
→ extract Features
→ validate minimum feature count
→ build X matrix
→ fit IsolationForest
→ build Artifact
→ save with joblib
```

不得以 S6 Spike Fixture 作為主要正常訓練資料。

## 11.3 Model Parameters

從 Config 讀取：

```yaml
contamination: 0.05
n_estimators: 200
random_state: 42
max_samples: "auto"
```

不得在 Python 邏輯 Hardcode 正式參數。

## 11.4 Artifact Schema

```python
{
    "metadata_version": "1.0",
    "metric_name": "api_requests_per_sec",
    "feature_names": MetricsWindowFeatures.feature_names(),
    "trained_at": "2026-07-26T10:00:00Z",
    "training_window_count": 30,
    "model_params": {
        "contamination": 0.05,
        "n_estimators": 200,
        "random_state": 42,
        "max_samples": "auto",
    },
    "sklearn_version": "<runtime version>",
    "numpy_version": "<runtime version>",
    "model": fitted_isolation_forest,
}
```

使用 `joblib.dump()` 與 `joblib.load()`。

## 11.5 Artifact Validation

載入時必須驗證：

- Artifact 是 Dict。
- Metadata Version 等於 Config。
- Metric Name 正確。
- Feature Names 與順序完全一致。
- Training Window Count 達 Minimum。
- Model 具有 `predict` 與 `decision_function`。
- sklearn Version 與目前 Runtime 完全相同。

NumPy Version 不同時至少記錄 WARNING；若實際 Load／Predict 失敗，raise `MetricsIForestModelLoadError`。

## 11.6 Model File 與安全

預設：

```text
models/metrics_isolation_forest.pkl
```

- Training Script 自動建立 Parent Directory。
- 不納入版本控制。
- Runtime 不需每輪重新訓練。
- 不得載入來源不明的 Pickle。
- 只允許載入本專案 Training Script 在 Config 指定本機路徑產生的 Artifact。

## 11.7 Model Missing：Fail Fast

預設 `train_if_missing: false`。Model 不存在時初始化直接 raise `MetricsIForestModelNotFoundError`，訊息需明確指出應先執行 Training Script。

不得靜默跳過模型、假裝載入成功、只用 3 倍 Rule 建立 Event，或每輪自動重新訓練。

## 11.8 Explicit `train_if_missing`

只有 Config 明確為 `true` 才允許：

```text
model missing
→ read fixed baseline fixture
→ train
→ save
→ reload and validate
→ start prediction
```

不得使用真實 Runtime Window 臨時自我訓練。

---

# 12. Prediction 與 Confidence

## 12.1 Prediction

```python
feature_vector = features.to_list()
model_label = int(model.predict([feature_vector])[0])
anomaly_score = float(model.decision_function([feature_vector])[0])
```

正式異常條件：

```python
model_label == -1 and anomaly_score <= score_threshold
```

Label 與 Score Threshold 必須同時成立。

## 12.2 Confidence

Confidence 為 0～1 的異常強度映射，不是校準機率。使用固定分段線性公式：

```python
def score_to_confidence(score: float) -> float:
    if score >= 0.0:
        return 0.0

    if score >= confidence_medium_score:
        t = score / confidence_medium_score
        return round(0.3 * t, 4)

    if score >= confidence_high_score:
        t = (
            score - confidence_medium_score
        ) / (
            confidence_high_score - confidence_medium_score
        )
        return round(0.3 + 0.5 * t, 4)

    clamped = max(score, -1.0)
    t = (
        clamped - confidence_high_score
    ) / (
        -1.0 - confidence_high_score
    )
    return round(min(0.8 + 0.2 * t, 1.0), 4)
```

不得輸出 NaN、Inf、小於 0 或大於 1。

---

# 13. Hybrid Classification

## 13.1 Classification Gate

只有 `prediction.is_anomaly is True` 才進入 Classifier。模型正常時回傳 `None`，不得建立 Event。

## 13.2 Recent Baseline

```python
baseline_samples = window.samples[:-1]
current_value = window.samples[-1].value
baseline_mean = mean(sample.value for sample in baseline_samples)
```

最後一筆是目前 QPS；前面 Samples 是近期 Baseline。若 Baseline Samples 為空，raise `ValueError`。

## 13.3 Spike Ratio

當 `baseline_mean > 0`：

```python
spike_ratio = current_value / baseline_mean
ratio_for_comparison = spike_ratio
baseline_zero = False
```

當 Baseline 與 Current 都為 0：

```python
spike_ratio = 0.0
ratio_for_comparison = 0.0
baseline_zero = True
```

當 Baseline 為 0、Current 大於 0：

```python
spike_ratio = None
ratio_for_comparison = float("inf")
baseline_zero = True
```

Infinity 只可存在內部分類，不得輸出至 JSON。

## 13.4 Known Request Spike

```python
prediction.is_anomaly is True
and ratio_for_comparison >= request_spike_ratio
```

輸出：

```text
event_type = request_spike_detected
severity = HIGH
classification_reason = current_qps_at_least_3x_recent_baseline
```

上述 `current_qps_at_least_3x_recent_baseline` 是目前 config value `3.0` 的 evidence wording；正式判斷仍為 `current_qps / baseline_mean >= configured request_spike_ratio`。在目前 Approved configuration 下，剛好 3.0 倍必須分類為 Known Request Spike。

## 13.5 Unknown QPS Window Anomaly

```python
prediction.is_anomaly is True
and ratio_for_comparison < request_spike_ratio
```

輸出：

```text
event_type = general_metrics_anomaly
classification_reason = anomalous_qps_window_not_matching_known_request_spike
```

Severity：

```text
confidence >= 0.8 → HIGH
confidence < 0.8  → MEDIUM
```

此 Event 只表示 QPS Window 異常，不得宣稱已確認根因、Rate Limit、DB 問題或所有未知 Metrics。

## 13.6 Rule-only 禁止規則

即使 `current_qps / baseline_mean >= configured request_spike_ratio`，若 Isolation Forest 判定正常，仍不得輸出任何 Event。

---

# 14. Event Schema Mapping

## 14.1 Top-level Schema

每筆 Event Top-level 欄位剛好為：

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

`window_start`、`window_end`、`metric_name`、Model Metadata 等只能放在 `triggered_features`。

## 14.2 固定 Mapping

| 欄位 | 值 |
|---|---|
| `event_source` | `metrics_iforest_detection` |
| `detection_method` | `isolation_forest` |
| `service_name` | `metrics` |
| `trace_id` | `null` |
| `source_ip` | `null` |
| `downstream_service` | `null` |
| `external_service` | `null` |
| `status` | `OPEN` |
| `raw_log_sample` | `[]` |

## 14.3 Event ID 與時間

```text
event_id = EVT-{epoch_milliseconds}-{random4}
```

`detected_at` 使用 Event 建立當下 UTC ISO 8601 並以 `Z` 結尾。

## 14.4 `triggered_features`

必須包含：

```text
metric_name
window_start
window_end
current_value
baseline_mean
spike_ratio
baseline_zero
window_mean
window_std
window_min
window_max
window_median
window_first
window_last
max_to_mean_ratio
current_to_mean_ratio
slope
sample_count
model_label
anomaly_score
model_metadata_version
classification_reason
```

所有 Number 必須有限；`spike_ratio` 只有 Baseline Zero 特殊情況可為 `null`。

## 14.5 Request Spike Event Example

```json
{
  "event_id": "EVT-1785060301234-a3f9",
  "detected_at": "2026-07-26T10:05:01.234Z",
  "event_source": "metrics_iforest_detection",
  "event_type": "request_spike_detected",
  "detection_method": "isolation_forest",
  "severity": "HIGH",
  "confidence": 0.8314,
  "service_name": "metrics",
  "trace_id": null,
  "source_ip": null,
  "downstream_service": null,
  "external_service": null,
  "status": "OPEN",
  "triggered_features": {
    "metric_name": "api_requests_per_sec",
    "window_start": "2026-07-26T10:00:00Z",
    "window_end": "2026-07-26T10:05:00Z",
    "current_value": 42.0,
    "baseline_mean": 11.0,
    "spike_ratio": 3.8182,
    "baseline_zero": false,
    "window_mean": 12.55,
    "window_std": 7.9,
    "window_min": 8.0,
    "window_max": 42.0,
    "window_median": 10.5,
    "window_first": 10.0,
    "window_last": 42.0,
    "max_to_mean_ratio": 3.3466,
    "current_to_mean_ratio": 3.3466,
    "slope": 1.6842,
    "sample_count": 20,
    "model_label": -1,
    "anomaly_score": -0.41,
    "model_metadata_version": "1.0",
    "classification_reason": "current_qps_at_least_3x_recent_baseline"
  },
  "raw_log_sample": []
}
```

## 14.6 General Metrics Anomaly Event Example

```json
{
  "event_id": "EVT-1785060601234-b7c1",
  "detected_at": "2026-07-26T10:10:01.234Z",
  "event_source": "metrics_iforest_detection",
  "event_type": "general_metrics_anomaly",
  "detection_method": "isolation_forest",
  "severity": "MEDIUM",
  "confidence": 0.6,
  "service_name": "metrics",
  "trace_id": null,
  "source_ip": null,
  "downstream_service": null,
  "external_service": null,
  "status": "OPEN",
  "triggered_features": {
    "metric_name": "api_requests_per_sec",
    "window_start": "2026-07-26T10:05:00Z",
    "window_end": "2026-07-26T10:10:00Z",
    "current_value": 21.0,
    "baseline_mean": 10.0,
    "spike_ratio": 2.1,
    "baseline_zero": false,
    "window_mean": 10.55,
    "window_std": 5.0,
    "window_min": 3.0,
    "window_max": 24.0,
    "window_median": 11.0,
    "window_first": 4.0,
    "window_last": 21.0,
    "max_to_mean_ratio": 2.2749,
    "current_to_mean_ratio": 1.9905,
    "slope": 0.8947,
    "sample_count": 20,
    "model_label": -1,
    "anomaly_score": -0.22,
    "model_metadata_version": "1.0",
    "classification_reason": "anomalous_qps_window_not_matching_known_request_spike"
  },
  "raw_log_sample": []
}
```

## 14.7 JSON Safety

Event Builder 必須將 NumPy Scalar 轉為 Python Number，並以 `json.dumps(event, allow_nan=False)` 驗證後才交給 EventStore。

---

# 15. Cooldown

## 15.1 Cooldown Key

```text
(event_type, metric_name)
```

不同 Event Type 的 Cooldown 互相獨立。

## 15.2 行為

Cooldown 內相同 Key 再次觸發：

- 跳過 Event。
- 不寫入 EventStore。
- 不回傳該 Event。
- 不更新 Last Fired Time。

Cooldown 到期後再次異常可重新建立 Event。只有 EventStore 寫入成功後才記錄 Last Fired Time；寫入失敗不得記錄 Cooldown。

## 15.3 Testability

Cooldown 應允許測試傳入指定 `now_timestamp`，Unit Test 不得實際等待 60 秒。

---

# 16. Detector Runtime Flow

## 16.1 Initialization

```text
load config
→ validate config
→ create PrometheusRangeClient
→ create EventStore
→ load model artifact
   ├── exists → validate
   └── missing
         ├── train_if_missing=false → fail fast
         └── train_if_missing=true  → train, save, reload, validate
→ create predictor / classifier / cooldown / builder
```

Config、Model Missing、Metadata 或 Load Error 屬於 Fatal Initialization Error，必須在 Polling Loop 前失敗。

## 16.2 `run_once()`

```text
calculate query start/end
→ fetch QPS samples
→ no samples: return []
→ build MetricWindow
→ insufficient samples: return []
→ extract features
→ predict anomaly
→ normal: return []
→ classify anomalous window
→ classification None: return []
→ cooldown check
→ cooldown active: return []
→ build and validate event
→ EventStore.write(event)
→ record cooldown
→ return [event]
```

## 16.3 `start()`

- 依 `poll_interval_seconds` 重複呼叫 `run_once()`。
- 捕捉單輪 Recoverable Runtime Error，記錄 ERROR 並進入下一輪。
- 不得因一次 Timeout 永久停止。
- `KeyboardInterrupt` 正常中止。

SPEC-004 未完成前，`start()` 只作為本機介面；SPEC-004 完成後由 Event Runner 決定正式呼叫方式。

---

# 17. Error Handling

## 17.1 Fatal Initialization Errors

| 情境 | 行為 |
|---|---|
| Config 不存在 | `FileNotFoundError` |
| YAML 錯誤 | Raise Parse Error |
| Config 欄位不合法 | `ValueError` |
| Metric Name 非 QPS | `ValueError` |
| Event Type 不允許 | `ValueError` |
| Model 不存在且不自動訓練 | `MetricsIForestModelNotFoundError` |
| Model 損壞 | `MetricsIForestModelLoadError` |
| Metadata 不符 | `MetricsIForestModelVersionError` |
| Baseline Fixture 不存在 | `FileNotFoundError` |
| Training Data 不足 | `MetricsIForestTrainingDataError` |

## 17.2 Recoverable Runtime Errors

| 情境 | 行為 |
|---|---|
| Prometheus Connection Error | ERROR，回傳 `[]` |
| Timeout | ERROR，回傳 `[]` |
| HTTP Status 非成功 | ERROR，回傳 `[]` |
| JSON Decode Error | ERROR，回傳 `[]` |
| `status != success` | ERROR，回傳 `[]` |
| `resultType != matrix` | ERROR，回傳 `[]` |
| 空 Result | DEBUG／WARNING，回傳 `[]` |
| 多 Series | ERROR，回傳 `[]` |
| Sample 不足 | WARNING，回傳 `[]` |
| EventStore 寫入失敗 | ERROR，不記錄 Cooldown，回傳 `[]` |

## 17.3 Unexpected Runtime Error

`start()` 可捕捉未預期 Exception、記錄 Stack Trace 並進入下一輪；`run_once()` 不得以過度寬泛的 `except Exception: return []` 隱藏所有程式設計錯誤。

---

# 18. Logging

| Level | 使用情境 |
|---|---|
| DEBUG | Query Range、Sample 清理、正常 Window、Cooldown Skip |
| INFO | Config Loaded、Model Loaded、Training Completed、Detector Started |
| WARNING | Sample 不足、General Anomaly、Explicit Train-if-missing |
| ERROR | Prometheus、Invalid Response、Model、EventStore、Unexpected Runtime Error |

Log 應包含 Metric Name、Window Start／End、Sample Count、Error Type；不得包含密碼、API Key、真實個資、不可信 Pickle 內容或敏感環境變數。

---

# 19. Scripts

## 19.1 Training Script

```text
scripts/train_metrics_model.py
```

必須：

1. 預設讀取 `configs/metrics_iforest.yaml`，可接受 `--config`。
2. 讀取 Baseline Fixture。
3. 使用正式 Feature Extractor。
4. 訓練 Isolation Forest。
5. 儲存 Model Artifact。
6. Reload 並驗證 Artifact。
7. 成功以 Exit Code 0 結束，失敗以非 0 結束。
8. 輸出 Model Path、Training Window Count、Feature Count、Metadata Version。

不得啟動 Docker、Prometheus 或 Generator，也不得複製第二套 Feature Logic。

## 19.2 Validation Script

```text
scripts/validate_metrics_iforest.py
```

使用已儲存 Model 與 Fixtures，不使用真實 Prometheus。必須驗證：

- Artifact 可載入，Metadata 與 Feature Contract 一致。
- Normal Fixture 不建立正式 Event。
- Spike Fixture 分類為 `request_spike_detected`。
- General Anomaly Fixture 分類為 `general_metrics_anomaly`。
- Event Builder 產生合法 15 欄位 Event。
- Event JSON 不含 NaN 或 Inf。

必要驗證失敗時以非 0 Exit Code 結束。若模型行為未達 Fixture 預期，只能調整本 SPEC 允許範圍內的 Baseline Fixture、測試 Fixture 或 Config 參數；不得修改 Generator、PRD 或改為 Rule-only。

---

# 20. Test Specification

## 20.1 Testing Principles

所有必要測試：

- 不依賴 Docker、真實 Prometheus、Generator、網路。
- 不依賴預先存在的正式 Model File。
- 使用固定 Random State。
- 使用 `tmp_path` 存放 Config、Model、EventStore。
- 使用 Mock `requests.get` 或 Fixture Response。
- 可由所有團隊成員與 CI 重現。

## 20.2 Config Tests

- 正常 Config。
- Config 不存在、YAML 錯誤、缺欄位。
- Metric Name、Endpoint、Event Type 錯誤。
- Ratio、Sample Count、Contamination、Confidence Threshold 不合法。
- `train_if_missing` 型別錯誤。

## 20.3 Prometheus Client Tests

- 正常 Matrix、空 Result。
- HTTP Error、Timeout、Connection Error、JSON Error。
- Status、Result Type 錯誤。
- 多 Series、Values 空。
- NaN、Inf、負 QPS、無法解析值。
- Samples 未排序。
- 重複 Timestamp 保留最後有效值。

## 20.4 Window 與 Feature Tests

- Sample 足夠／不足。
- 實際 Sample Boundary。
- Current、Mean、Population Std、Min、Max、Median、First、Last。
- Ratios、Slope、Sample Count。
- Mean Zero 保護。
- Feature Names、順序、長度 12。
- 不產生 NaN／Inf。

## 20.5 Training 與 Artifact Tests

- Baseline Fixture 解析。
- Metric Name 錯誤、Window 數不足。
- Fit、Save、Reload。
- Metadata 完整。
- Feature、Metric、Metadata Version、sklearn Version 不符時失敗。
- 缺 Model 欄位、損壞 Model 時失敗。
- 固定 Random State 可重現。

## 20.6 Prediction Tests

- 正常 Window 不觸發，異常 Window 觸發。
- Label=-1 但 Score 未達 Threshold 不觸發。
- Score 達 Threshold 但 Label 非 -1 不觸發。
- Confidence Boundary、範圍與 Finite Value。
- Feature 維度錯誤時失敗。

可使用 Mock Model 精準測試判定邏輯，另以固定真實 Model 測試 Training／Validation。

## 20.7 Classification Tests

### Case A：Known Spike

IForest 異常且 Ratio 大於 3.0 → `request_spike_detected`、HIGH。

### Case B：Boundary 3.0

IForest 異常且 Ratio 等於 3.0 → `request_spike_detected`、HIGH。

### Case C：Unknown QPS Anomaly

IForest 異常且 Ratio 小於 3.0 → `general_metrics_anomaly`、MEDIUM 或 HIGH。

### Case D：Rule 成立但模型正常

IForest 正常且 Ratio 大於等於 3.0 → 不建立 Event。

### Case E：Baseline Zero、Current Positive

不發生 Division by Zero；內部可用 Infinity 比較；Event `spike_ratio=null`、`baseline_zero=true`；模型異常時分類為 Known Spike。

### Case F：Baseline Zero、Current Zero

Ratio 為 0.0、`baseline_zero=true`；只有模型異常時才可能分類為 General Anomaly。

## 20.8 Event Schema Tests

- Top-level 欄位剛好 15 個。
- Event Source、Detection Method、Status、Service Name 正確。
- `raw_log_sample=[]`，Correlation-specific 欄位為 None。
- 只允許兩個 Event Type。
- 額外資訊只在 `triggered_features`。
- Triggered Features 必要欄位完整。
- `json.dumps(..., allow_nan=False)` 成功。

## 20.9 Cooldown Tests

- 第一筆觸發。
- 同 Key Cooldown 內不重複。
- 不同 Event Type 分別觸發。
- 到期再次觸發。
- Skip 不更新時間。
- EventStore 失敗不記錄 Cooldown。
- 不實際等待時間。

## 20.10 Detector Tests

- Normal 回傳 `[]`。
- Spike／General 各成功寫入一筆。
- Empty、Sample 不足、Cooldown 回傳 `[]`。
- EventStore 使用 `tmp_path`。
- Recoverable Prometheus Error 不寫入 Event。

## 20.11 Model Missing Tests

`train_if_missing=false`：Raise `MetricsIForestModelNotFoundError`。

`train_if_missing=true`：Fixture → Train → Save 到 `tmp_path` → Reload → Detector 初始化成功。

## 20.12 Required Commands

```powershell
python -m pytest tests/test_metrics_iforest.py -q
python -m pytest -q
python scripts/train_metrics_model.py
python scripts/validate_metrics_iforest.py
```

上述不包含任何 Git 操作。

---

# 21. Acceptance Criteria

## 21.1 Scope 與 Input

- [ ] 正式 Metric 只有 `api_requests_per_sec`。
- [ ] Runtime 使用 Prometheus `query_range`。
- [ ] 不使用 Instant Query 作為模型 Window Input。
- [ ] 不修改 Log／Metrics Generator。
- [ ] 不將 DB Pool 納入模型或 Event Detection。
- [ ] 必要測試不依賴 Docker、Prometheus、Generator 或網路。

## 21.2 Window 與 Features

- [ ] Samples 會被驗證、清理、排序及去重。
- [ ] 負 QPS、NaN 與 Inf 不會進入模型。
- [ ] Sample 不足時不執行推論。
- [ ] Feature Contract 固定為 12 個欄位。
- [ ] Training 與 Prediction Feature 順序完全一致。
- [ ] Features 不包含 NaN 或 Inf。

## 21.3 Model Lifecycle

- [ ] 使用 `sklearn.ensemble.IsolationForest`。
- [ ] Model Parameters 由 Config 讀取。
- [ ] Baseline Fixture 可重現。
- [ ] 至少產生 Config 指定的 Minimum Training Windows。
- [ ] Model Artifact 包含完整 Metadata。
- [ ] Loader 驗證 Metadata 與 sklearn Version。
- [ ] Model File 不納入版本控制。
- [ ] Model Missing 預設 Fail Fast。
- [ ] 只有明確 `train_if_missing=true` 才自動從固定 Fixture 訓練。
- [ ] 不允許無聲 Rule-only Fallback。

## 21.4 Prediction 與 Classification

- [ ] Model Label 與 Score Threshold 同時成立才視為異常。
- [ ] Confidence 固定為 0～1，且不宣稱為機率。
- [ ] 採 Hybrid Pipeline。
- [ ] Rule 不得單獨建立 Event。
- [ ] `spike_ratio >= 3.0` 分類為 `request_spike_detected`。
- [ ] 其他模型異常分類為 `general_metrics_anomaly`。
- [ ] `general_metrics_anomaly` 明確限於 QPS Window。
- [ ] Baseline Zero 不產生非法 JSON Number。

## 21.5 Event 與 Cooldown

- [ ] Event Top-level 欄位剛好符合 PRD-002 15 欄位。
- [ ] Event Source 為 `metrics_iforest_detection`。
- [ ] Detection Method 為 `isolation_forest`。
- [ ] `raw_log_sample == []`。
- [ ] Event 可用 `allow_nan=False` 序列化。
- [ ] 只輸出兩個正式 Event Type。
- [ ] Cooldown Key 為 `(event_type, metric_name)`。
- [ ] EventStore 成功後才記錄 Cooldown。
- [ ] EventStore 寫入失敗不視為成功 Event。

## 21.6 Tests 與範圍治理

- [ ] `tests/test_metrics_iforest.py` 全部通過。
- [ ] Training Script 成功。
- [ ] Validation Script 成功。
- [ ] Full Regression 全部通過。
- [ ] 未修改禁止範圍。
- [ ] 未修改 Dependency Files。
- [ ] AI Coding Agent 未執行 Git 指令。
- [ ] 不執行 Alert、Incident、LLM、RAG、RCA、Dashboard 或 Email。

---

# 22. Completed Integration Evidence（Non-normative）

## 22.1 SPEC-005 v1.2 Final Validation

SPEC-005 v1.2 已完成 S6 final validation，evidence flow 為：

```text
baseline samples ready
→ QPS spike satisfies configured request_spike_ratio
→ EventDetectionRunner
→ request_spike_detected
→ EventStore persistence evidence
→ PASS
```

此為已完成的 integration validation evidence，不是 MetricsIForestDetector 的新 requirement；validation controller 行為亦不屬於本 Detector contract。原有正式 gate 保持 `label == -1 AND score <= configured threshold`，未達 configured request spike ratio 的 model anomaly 仍 fallback 為 `general_metrics_anomaly`。

既有模組交付條件以：

- Mock Prometheus Response。
- 固定 Baseline Fixture。
- Training Script。
- Validation Script。
- Unit Tests。
- Full Regression。

為準。

上述既有模組交付條件與 Detector contract 不因 integration evidence 而改變。

## 22.2 已完成的整合範圍

SPEC-005 v1.2 已執行：

```text
Generator Validation
→ Generator Patch（若需要）
→ Prometheus query_range 真實驗證
→ SPEC-002 / SPEC-003 / SPEC-004 整合
→ 六大情境端到端驗收
```

該階段已驗證：

1. 正常 QPS 不輸出 Event。
2. S6 QPS Spike 產生 `request_spike_detected`。
3. 未知 QPS Pattern 產生 `general_metrics_anomaly`。
4. Log `rate_limit_storm` 與 Metrics `request_spike_detected` 可被後續 Correlation 收斂。
5. Event Runner 可獨立呼叫三條 Detection Pipeline。

上述結果是 integration evidence，不將 validation controller 行為升級為 Metrics detector requirement。

---

# 23. 最終交付物

完成後應包含：

```text
configs/metrics_iforest.yaml
src/event_detection/metrics_iforest.py
scripts/train_metrics_model.py
scripts/validate_metrics_iforest.py
tests/test_metrics_iforest.py
tests/fixtures/metrics_iforest/qps_baseline.json
tests/fixtures/metrics_iforest/prometheus_qps_normal.json
tests/fixtures/metrics_iforest/prometheus_qps_spike.json
tests/fixtures/metrics_iforest/prometheus_qps_general_anomaly.json
tests/fixtures/metrics_iforest/prometheus_qps_empty.json
```

Runtime 可產生但不得納入版本控制：

```text
models/metrics_isolation_forest.pkl
events/event_store.jsonl
```

本 SPEC 不包含：

- 完整 AI Coding Agent Prompt。
- Codex CLI 安裝或操作流程。
- Git 指令。
- 比賽答辯與設計理由素材。

上述內容由 PM 透過獨立 Google Doc 與比賽準備文件提供。
