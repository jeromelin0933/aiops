# SPEC-004：Event Detection Runner

## Software Design Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-004 |
| Document Name | Event Detection Runner |
| Version | 1.0 |
| Status | Ready for Implementation |
| Date | 2026-07-31 |
| Author | 林子豪（PM） |
| Assignee | 富裕 |
| Branch Metadata | `feature/event-runner` |
| Related PRD | PRD-001 v3.1、PRD-002 v1.1 |
| Related DDS | DDS-001 |
| Related SPEC | SPEC-001 v2.1、SPEC-002 v1.3、SPEC-003 v1.0 |
| Deferred Follow-up | SPEC-005 Mock Data Generator Validation and Scenario Alignment |

> 本文件是 Event Detection Runner 的正式工程契約。Git 操作、Codex CLI 操作方式及完整 AI Coding Agent Prompt 不屬於本 SPEC，由 PM 透過獨立工作文件提供。

---

# 0. 文件目的與規格優先序

本文件定義 **Event Detection Runner** 的完整實作規格。

SPEC-001、SPEC-002、SPEC-003 已分別提供三條彼此獨立的 Event Detection Pipeline：

```text
1. Log Event Detection
2. Metrics Threshold Detection
3. Metrics Isolation Forest Detection
```

SPEC-004 的責任不是重新實作異常偵測，而是建立一個統一、可測試、可停止且具錯誤隔離能力的執行入口，依固定順序協調三條 Pipeline，並保留每條 Pipeline 原有的 Event 建立、Cooldown 與 EventStore 寫入責任。

正式控制流程：

```text
EventDetectionRunner
        │
        ├── LogEventDetectionRunner.run_once()
        ├── MetricsThresholdDetector.run_once()
        └── MetricsIForestDetector.run_once()
```

正式資料流程：

```text
Log Event Detection ──────────────┐
Metrics Threshold Detection ──────┼──▶ events/event_store.jsonl
Metrics IForest Detection ────────┘              │
                                                  ▼
                                      Alert Correlation（下一階段）
```

## 0.1 規格優先序

1. PRD-002 v1.1 的三條 Pipeline 獨立性、Event Schema、NFR 與驗收要求。
2. 本 SPEC-004 的模組邊界、Runner Contract、Scheduler、錯誤處理與驗收標準。
3. PRD-001 v3.1 的 Incident-driven Architecture 與 Prototype 範圍。
4. SPEC-001 v2.1、SPEC-002 v1.3、SPEC-003 v1.0 已完成的既有介面與 Regression Tests。
5. DDS-001 已建立的 Generator、Prometheus、Loki、Grafana 與 Docker 基礎。

若文件、既有程式碼或測試之間出現無法同時滿足的衝突，實作者與 AI Coding Agent 必須停止擴大修改並回報 PM，不得自行重新定義需求、修改其他 SPEC 的核心邏輯或降低驗收標準。

## 0.2 核心決策

1. SPEC-004 v1.0 採 **單行程、循序式 Orchestration**，不使用 Thread、Process、AsyncIO、Kafka 或外部 Message Queue。
2. 三條 Pipeline 固定依序執行：Log → Metrics Threshold → Metrics IForest。
3. Metrics Threshold 與 Metrics IForest 採 **OR 接納**；任一 Pipeline 獨立產生的 Event 均保留。
4. Event Runner 不做 Cross-Pipeline Deduplication、Correlation 或 Incident 建立。
5. 每個 Detector 仍自行建立 Event、執行 Cooldown 並寫入 EventStore。
6. Event Runner 只彙整 `run_once()` 回傳值，**不得再次呼叫 EventStore.write()**。
7. Event Runner 不新增、刪除、重新命名或改寫 PRD-002 的 15 個 Event Top-level 欄位。
8. Log Detection 必須補上非 Blocking 的 `run_once()`；原有 `start()` 與 `tail()` 能力必須維持相容。
9. SPEC-004 使用 `configs/event_runner.yaml` 作為整合 Runtime 的排程唯一來源；各子模組原有 Poll Interval 僅供其獨立 `start()` 使用。
10. Metrics Threshold 與 Metrics IForest 在整合 Runtime 中固定每 15 秒執行一次，以符合 PRD-002 NFR-01。
11. Scheduler 使用 `time.monotonic()`，不得使用系統日期時間計算執行間隔。
12. Startup／Configuration／Model Initialization Error 採 **Fail Fast**。
13. 單一 Pipeline 的 Runtime Error 採 **Error Isolation**，不得阻止其他 Pipeline 執行，下一個排程週期再重試。
14. Scheduler 不進行積欠週期的 Burst Catch-up；若執行時間超過排程，跳過已錯過的時間點並排到下一個未來週期。
15. Unit Tests 不依賴 Docker、真實 Prometheus、Generator、外部網路、正式 Model File 或實際等待時間。
16. Generator 驗證與修改不屬於本 SPEC，延後至 SPEC-005。
17. v1.0 不宣稱能捕捉所有短於 Prometheus Scrape／Polling 間隔的瞬時 Metrics Spike。

---

# 1. 系統定位與模組邊界

## 1.1 整體架構位置

```text
Logs
  │
  ▼
Log Event Detection（SPEC-001）──────────────┐
                                              │
Metrics                                       │
  ├── Metrics Threshold（SPEC-002）──────────┼──▶ EventStore / Event Queue
  └── Metrics IForest（SPEC-003）────────────┘              │
                                                             ▼
                                               Event Runner（SPEC-004）
                                                             │
                                                             ▼
                                     Alert Correlation / Incident / RCA
```

上圖的 Event Runner 是三條 Detection Pipeline 的 **控制入口**。EventStore 是三條 Pipeline 的 **共同資料輸出**。

Event Runner 不是 EventStore Consumer，也不是 Alert Correlation Engine。

## 1.2 本模組負責

- 讀取並驗證 `configs/event_runner.yaml`。
- 建立固定 Pipeline Registry 與固定執行順序。
- 只初始化 `enabled: true` 的 Pipeline。
- 初始化 Log、Metrics Threshold、Metrics IForest Detector。
- 對 Fatal Startup Error 採 Fail Fast。
- 提供 `run_once()` 強制執行所有啟用 Pipeline 一次。
- 提供 `run_due_once()` 只執行目前到期的 Pipeline。
- 提供 `start()` 持續執行 Scheduler Loop。
- 提供 `stop()` 請求安全停止。
- 使用 `time.monotonic()` 維護各 Pipeline 的 `next_due`。
- 依固定順序循序呼叫各 Detector 的 `run_once()`。
- 隔離單一 Pipeline Runtime Exception。
- 彙整每條 Pipeline 的成功、失敗、Event 數量與執行時間。
- 偵測 Scheduler Lag 與 Pipeline Overrun 並記錄 Warning。
- 保留三條 Pipeline 原有獨立執行能力。
- 將 SPEC-001 的 Log Runtime 拆成可被統一 Runner 呼叫的非 Blocking 單輪介面。
- 提供可重現且不依賴真實基礎設施的 Unit Tests。
- 提供 CLI 的單輪與持續執行入口。

## 1.3 本模組不負責

- 修改 Log Generator 或 Metrics Generator。
- 修改 Docker Compose、Prometheus、Loki、Promtail 或 Grafana。
- 驗證或修正六大情境實際模擬資料。
- 修改任何 Log／Metrics 異常判斷演算法。
- 修改 Threshold、Isolation Forest Feature、Model Parameter 或 Classifier Rule。
- 訓練 Log Model 或 Metrics Model。
- 建立新的 Event Type。
- 建立、修改或補寫 Event。
- 再次寫入 EventStore。
- 跨 Pipeline 去重、事件折疊或告警收斂。
- 建立 Alert 或 Incident。
- 修改 Event 狀態。
- 呼叫 LLM、RAG 或 RCA。
- 更新 Dashboard 或寄送 Email。
- 提供 Production Grade Queue、HA、Auto Restart 或 Distributed Scheduler。
- 保證捕捉短於 Metrics Scrape／Polling 間隔的瞬時 Spike。

## 1.4 OR 接納與 Alert Correlation 邊界

Metrics 雙軌制：

```text
Metrics Threshold Event ─────┐
                              ├── 任一成立均保留
Metrics IForest Event ───────┘
```

例如：

```text
S3：oom_crash_detected + high_memory_detected
S6：rate_limit_storm + request_spike_detected
```

上述多筆 Event 是同一事故的不同證據，不是 SPEC-004 應刪除的重複資料。

Cross-Pipeline Correlation 必須留給後續 Alert Correlation Engine。

---

# 2. 前置條件與依賴

## 2.1 前置條件

開始實作前，`feature/event-runner` 應包含：

- PRD-001 v3.1。
- PRD-002 v1.1。
- SPEC-001 v2.1 已完成成果。
- SPEC-002 v1.3 已完成成果。
- SPEC-003 v1.0 已完成成果。
- DDS-001 基礎設施與既有目錄。
- 全專案 Baseline Regression Tests 通過。

## 2.2 既有正式介面

### Log Event Detection

現有介面：

```python
class LogEventDetectionRunner:
    def start(self) -> None:
        ...
```

本 SPEC 必須補上：

```python
class LogEventDetectionRunner:
    def initialize(self) -> None:
        ...

    def run_once(self) -> list[dict]:
        ...

    def start(self) -> None:
        ...
```

### Metrics Threshold Detection

```python
class MetricsThresholdDetector:
    def run_once(self) -> list[dict]:
        ...

    def start(self) -> None:
        ...
```

### Metrics Isolation Forest Detection

```python
class MetricsIForestDetector:
    def run_once(self) -> list[dict]:
        ...

    def start(self) -> None:
        ...
```

## 2.3 EventStore Ownership

三條 Detector 的 `run_once()` 皆負責：

```text
Detection
→ Event Build
→ Cooldown
→ EventStore.write(event)
→ return successful events
```

SPEC-004 只負責：

```text
call detector.run_once()
→ validate return container
→ aggregate returned events
```

禁止流程：

```text
Detector writes event
→ Event Runner writes same event again   # 禁止
```

## 2.4 Runtime Dependencies

本 SPEC 不新增第三方套件。

使用既有依賴與 Python Standard Library：

```text
argparse dataclasses datetime logging pathlib time typing
PyYAML
```

不得新增 APScheduler、schedule、Celery、Redis、Kafka Client、asyncio framework 或新的 Prometheus Client。

## 2.5 依賴變更規則

- 富裕與 AI Coding Agent 不得自行修改 `requirements.txt` 或 `requirements-dev.txt`。
- 不得自行升級、降級或鎖定套件版本。
- 發現缺少依賴時停止擴大修改並回報 PM。
- 不得為了 Scheduler 引入第三方套件。

---

# 3. Input 與 Output Contract

## 3.1 正式 Runtime Input

SPEC-004 的直接 Input 是三個 Detector 的 `run_once()` 介面，不直接讀取 Logs 或 Prometheus Response。

```text
LogEventDetectionRunner.run_once()       -> list[dict]
MetricsThresholdDetector.run_once()      -> list[dict]
MetricsIForestDetector.run_once()        -> list[dict]
```

底層資料來源仍為：

```text
Log：logs/aiops.json.log
Metrics Threshold：Prometheus Instant Query
Metrics IForest：Prometheus Query Range
```

## 3.2 Development／Unit Test Input

Unit Tests 使用：

- Fake／Stub Detector。
- Fake Clock。
- Fake Sleeper。
- `tmp_path` 建立的 Runner Config。
- `tmp_path` 建立的 Log File。
- 固定 15 欄位 Event Dict。
- Mock Predictor、Parser 或 Reader（需要時）。

必要測試不得要求：

- Docker。
- 真實 Prometheus。
- Generator。
- 外部網路。
- 正式 `.pkl` Model File。
- 真實等待 5 秒或 15 秒。

## 3.3 Detector Return Contract

每個啟用 Detector 的 `run_once()` 必須回傳：

```python
list[dict]
```

合法結果：

```python
[]
[event]
[event_a, event_b]
```

Event Runner 至少驗證：

1. 回傳值必須是 `list`。
2. List 中每個元素必須是 `dict`。

若 Detector 回傳 `None`、Tuple、String 或包含非 Dict 元素，視為該 Pipeline Runtime Contract Error：

- 該 Pipeline 本輪標記 `FAILED`。
- 記錄 ERROR 與 Stack Trace。
- 其他 Pipeline 繼續。
- 不將非法回傳值加入 Cycle Result。

SPEC-004 不重新驗證 15 欄位 Schema，也不修改 Event。Event Schema 正確性由各 Detector 與 Regression Tests 保證。

## 3.4 正式持久化 Output

```text
events/event_store.jsonl
```

此檔案仍由各 Detector 透過既有 EventStore 寫入。

SPEC-004 不持久化 PipelineRunResult 或 CycleResult。

## 3.5 Event Runner Internal Output

`run_once()` 與 `run_due_once()` 回傳：

```python
EventRunnerCycleResult
```

此結果只供：

- Unit Test。
- CLI 摘要。
- Runtime Logging。
- 後續整合驗證。

不得寫入 `events/event_store.jsonl`，也不得當成 Incident 或 Alert。

---

# 4. 檔案範圍與 AI Agent 執行邊界

## 4.1 允許新增或修改

```text
configs/event_runner.yaml
src/event_detection/runner.py
src/event_detection/log/reader.py
tests/test_event_runner.py
```

可選：

```text
tests/fixtures/event_runner/
```

只有在 `tests/test_event_runner.py` 的可讀性確實需要 JSON Fixture 時才可新增。

## 4.2 可讀取或 Import，但不得修改

```text
src/event_detection/metrics_threshold.py
src/event_detection/metrics_iforest.py
src/event_detection/log/parser.py
src/event_detection/log/features.py
src/event_detection/log/encoder.py
src/event_detection/model/
src/event_detection/event/builder.py
src/event_detection/store/event_store.py
configs/event_detection.yml
configs/thresholds.yaml
configs/metrics_iforest.yaml
requirements.txt
requirements-dev.txt
.gitignore
```

## 4.3 絕對禁止修改

```text
docker-compose.yml
docker/
README.md
CONTRIBUTING.md
docs/
src/log_generator/
src/metrics_generator/
scripts/train_log_model.py
scripts/train_metrics_model.py
scripts/validate_log_detection.py
scripts/validate_metrics_iforest.py
tests/test_metrics_threshold.py
tests/test_metrics_iforest.py
PRD / SPEC / ADR / SDD
Alert Correlation
Incident Manager
RAG / LLM / RCA
Dashboard / Email
```

若 Repository 中存在其他 SPEC-001 測試檔，也不得為了讓 SPEC-004 通過而降低、刪除或改寫既有測試。

## 4.4 Runtime 產物不得提交

```text
events/event_store.jsonl
models/*.pkl
logs/*.log
.venv/
__pycache__/
.pytest_cache/
```

## 4.5 AI Coding Agent 執行邊界

- 本 SPEC 是正式工程契約。
- AI Coding Agent 不得執行任何 Git 指令。
- 不得修改 Dependency Files、Generator、Docker 或文件。
- 不得新增 Event Type 或 Event Top-level 欄位。
- 不得將 Runner 實作成 Cross-Pipeline Deduplicator。
- 不得將三條 Pipeline 改成 AND Gate。
- 不得將 Detector 的 EventStore 寫入移到 Event Runner。
- 不得把 Sequential Runner 改成 Thread、Process、AsyncIO 或 Kafka。
- 不得為通過測試而修改 SPEC-001～003 的 Detection Logic。
- 發現規格與既有程式衝突時，停止修改並列出衝突供 PM 判斷。

---

# 5. Config Specification

新增：

```text
configs/event_runner.yaml
```

## 5.1 正式 Config

```yaml
# configs/event_runner.yaml
runtime:
  tick_seconds: 1.0

pipelines:
  log_event_detection:
    enabled: true
    config_path: "configs/event_detection.yml"
    interval_seconds: 5.0

  metrics_threshold_detection:
    enabled: true
    config_path: "configs/thresholds.yaml"
    interval_seconds: 15.0

  metrics_iforest_detection:
    enabled: true
    config_path: "configs/metrics_iforest.yaml"
    interval_seconds: 15.0
```

## 5.2 Schedule Ownership

當使用 SPEC-004 `EventDetectionRunner.start()` 時：

```text
configs/event_runner.yaml
```

是整合 Runtime 的排程唯一來源。

子模組既有 Config 中的 Poll Interval：

```text
configs/event_detection.yml
configs/thresholds.yaml
configs/metrics_iforest.yaml
```

僅供子模組獨立呼叫其 `start()` 時使用，不控制 SPEC-004 Scheduler。

SPEC-004 不得修改上述子模組 Config。

## 5.3 固定 Pipeline Name 與順序

合法 Pipeline Name：

```text
log_event_detection
metrics_threshold_detection
metrics_iforest_detection
```

固定執行順序：

```python
PIPELINE_ORDER = (
    "log_event_detection",
    "metrics_threshold_detection",
    "metrics_iforest_detection",
)
```

順序不可由 YAML 任意改寫。

## 5.4 `EventRunnerConfigLoader`

```python
class EventRunnerConfigLoader:
    @staticmethod
    def load(config_path: str | Path) -> dict:
        ...
```

必須：

1. 以 UTF-8 讀取 YAML。
2. Config 不存在時 raise `FileNotFoundError`。
3. YAML 錯誤保留原始 Exception Context。
4. 驗證完成後才回傳 Dict。
5. 不修改傳入 Config。

## 5.5 Config Validation

至少驗證：

- Top-level 必須包含 `runtime` 與 `pipelines`。
- `runtime.tick_seconds` 為有限正數。
- `pipelines` 必須包含三個固定 Pipeline Key。
- 不允許未知 Pipeline Key。
- 每個 Pipeline 的 `enabled` 必須是 Boolean。
- 每個 Pipeline 的 `interval_seconds` 必須是有限正數。
- 啟用的 Pipeline 必須有非空 `config_path`。
- 啟用的 `config_path` 必須存在且為檔案。
- 至少一條 Pipeline Enabled。
- `tick_seconds <= 所有啟用 Pipeline 的最小 interval_seconds`。
- `metrics_threshold_detection.interval_seconds == 15.0`。
- `metrics_iforest_detection.interval_seconds == 15.0`。

Config 不合法時必須在建立 Runtime Loop 前 raise `ValueError`。

## 5.6 Disabled Pipeline 行為

當某 Pipeline：

```yaml
enabled: false
```

Event Runner 必須：

- 不建立該 Detector。
- 不載入該 Pipeline Model。
- 不驗證該 Pipeline 的 Runtime Model Artifact。
- 不呼叫該 Pipeline。
- 不因該 Pipeline Config／Model 缺失而失敗。

Disabled IForest 不得因 `.pkl` 不存在而阻止其他 Pipeline 啟動。

---

# 6. Internal Data Structures

所有資料結構可放在：

```text
src/event_detection/runner.py
```

不得建立新的共用 Schema File。

## 6.1 `PipelineRunStatus`

```python
PipelineRunStatus = Literal[
    "SUCCESS",
    "FAILED",
    "SKIPPED_NOT_DUE",
]
```

## 6.2 `PipelineRunResult`

```python
@dataclass
class PipelineRunResult:
    pipeline_name: str
    status: PipelineRunStatus
    started_at: str
    completed_at: str
    duration_ms: float
    events: list[dict]
    event_count: int
    error_type: str | None = None
    error_message: str | None = None
    scheduler_lag_ms: float | None = None
```

規則：

- `SUCCESS`：`error_type`、`error_message` 為 None。
- `FAILED`：`events=[]`、`event_count=0`，Error 欄位非空。
- `SKIPPED_NOT_DUE`：`events=[]`、`event_count=0`、`duration_ms=0.0`。
- `duration_ms` 必須大於等於 0。
- `event_count == len(events)`。
- 不得把 Exception Object 放進 Dataclass。
- Error Message 不得包含 API Key、密碼或敏感環境變數。

## 6.3 `EventRunnerCycleResult`

```python
@dataclass
class EventRunnerCycleResult:
    mode: Literal["FORCED", "DUE_ONLY"]
    started_at: str
    completed_at: str
    pipeline_results: list[PipelineRunResult]
    total_event_count: int
    success_count: int
    failure_count: int
    skipped_count: int
```

規則：

```text
total_event_count = 所有 SUCCESS result 的 event_count 總和
success_count = status == SUCCESS
failure_count = status == FAILED
skipped_count = status == SKIPPED_NOT_DUE
```

Pipeline Result 順序必須與 `PIPELINE_ORDER` 相同。

## 6.4 Internal Pipeline Runtime State

可使用：

```python
@dataclass
class PipelineRuntimeState:
    name: str
    detector: DetectionPipeline
    interval_seconds: float
    next_due_monotonic: float
```

此資料只存在記憶體中，不持久化。

---

# 7. Detection Pipeline Contract 與建立方式

## 7.1 Protocol

```python
class DetectionPipeline(Protocol):
    def run_once(self) -> list[dict]:
        ...
```

三條正式 Detector 都必須符合此最小介面。

## 7.2 Default Factory Mapping

```text
log_event_detection
→ LogEventDetectionRunner(config_path)

metrics_threshold_detection
→ MetricsThresholdDetector(config_path)

metrics_iforest_detection
→ MetricsIForestDetector(config_path)
```

Log Detector 建立後必須在 Startup Phase 呼叫：

```python
log_detector.initialize()
```

Metrics IForest 的 Fatal Model Validation 由其 Constructor／Initialization Contract 負責。

## 7.3 Dependency Injection

為了讓 Unit Test 不依賴真實 Config、Prometheus 或 Model，`EventDetectionRunner` 必須允許測試傳入 Pipeline Override：

```python
from collections.abc import Mapping

class EventDetectionRunner:
    def __init__(
        self,
        config_path: str | Path = "configs/event_runner.yaml",
        pipeline_overrides: Mapping[str, DetectionPipeline] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        ...
```

規則：

- Override 只能使用三個固定 Pipeline Name。
- Override 只套用於 Enabled Pipeline。
- 未提供 Override 時建立正式 Detector。
- 提供 Override 時不得額外建立正式 Detector。
- Fake Detector 不得被要求載入 Model 或連線 Prometheus。
- `clock` 預設 `time.monotonic`。
- `sleeper` 預設 `time.sleep`。

## 7.4 Return Contract Validation

Event Runner 內部應提供：

```python
def _validate_events_returned(
    pipeline_name: str,
    value: object,
) -> list[dict]:
    ...
```

不合法時 raise `TypeError`，再由單 Pipeline Wrapper 捕捉並記錄為 `FAILED`。

---

# 8. LogReader 非 Blocking 介面

SPEC-001 的 `LogReader.tail()` 是無限 Generator，不能直接放入 SPEC-004 的單輪循序排程。

本 SPEC 必須新增：

```python
class LogReader:
    def read_new_lines_once(self) -> list[str]:
        ...
```

## 8.1 `read_new_lines_once()` 行為

- 非 Blocking。
- 不呼叫 `time.sleep()`。
- 每次只讀取目前 Offset 後已存在的新行。
- 回傳去除空白後的非空字串 List。
- 更新 Offset。
- 不重複讀取已處理行。
- 保留 UTF-8 與 `errors="replace"`。

## 8.2 首次啟動語意

若 Log File 在 Reader 首次啟動時已存在：

```text
position offset at EOF
→ return []
→ 只處理之後新增的 Log
```

此行為必須與既有 `tail()` 一致。

因此正式 E2E 操作順序必須是：

```text
先啟動 Event Runner
→ 再觸發 Scenario Generator
```

若 Reader 首次啟動時檔案不存在：

```text
return []
```

日後檔案建立時，必須從新檔案開頭讀取，避免漏掉檔案建立後已寫入的第一批資料。

## 8.3 Rotation 與 Truncation

必須處理：

1. inode 改變：視為 Log Rotation，Offset 重設為 0。
2. File Size 小於目前 Offset：視為同 inode Truncation，Offset 重設為 0。
3. File 不存在：回傳 `[]`，不得 raise 或 sleep。

## 8.4 `tail()` 相容性

原有 `tail()` 必須保留，並改為重用 `read_new_lines_once()`：

```python
def tail(self):
    while True:
        for line in self.read_new_lines_once():
            yield line
        time.sleep(self.poll_interval)
```

不得在 `tail()` 與 `read_new_lines_once()` 維護兩套不同 Offset／Rotation Logic。

## 8.5 `read_all()` 相容性

`read_all()` 行為不得改變：

- 一次讀取全部非空行。
- 不改變 Runtime Offset。
- 不影響 `tail()` 或 `read_new_lines_once()`。

---

# 9. LogEventDetectionRunner Refactor

## 9.1 設計目標

將現有 Blocking Runtime 拆成：

```text
initialize()
run_once()
start()
```

不得修改：

- Window-level Feature Contract。
- Isolation Forest Prediction Gate。
- Event Classification Priority。
- Event Schema Mapping。
- Cooldown Key。
- EventStore Ownership。

## 9.2 `initialize()`

```python
class LogEventDetectionRunner:
    def initialize(self) -> None:
        ...
```

必須：

- 載入 Log Isolation Forest Model。
- Idempotent；重複呼叫不重複 Load。
- Model Missing、損壞或不相容時直接 Raise。
- 不讀 Log。
- 不進入 Loop。
- 不建立 Event。

建議內部狀態：

```python
self._initialized: bool = False
```

## 9.3 `_process_raw_line()`

為避免 `run_once()` 與 `start()` 複製 Detection Logic，應抽出：

```python
def _process_raw_line(self, raw_line: str) -> dict | None:
    ...
```

流程必須維持 SPEC-001：

```text
parse
→ extract
→ encode
→ WindowBuffer.add
→ has_enough
→ compute_window_features
→ predictor.predict_one
→ normal: None
→ compute_summary
→ EventBuilder.build
→ cooldown check
→ EventStore.write
→ record cooldown
→ return event
```

只有 EventStore 寫入成功後才回傳 Event 並記錄 Cooldown。

## 9.4 `run_once()`

```python
def run_once(self) -> list[dict]:
    ...
```

流程：

```text
initialize if needed
→ reader.read_new_lines_once()
→ process each raw line
→ collect only successfully written events
→ return events
```

規則：

- 非 Blocking。
- 不 Sleep。
- 沒有新 Log 時回傳 `[]`。
- Parser Skip／正常 Window／Sample 不足／Cooldown 時不加入 Result。
- 同一批次可能回傳 0～多筆 Event。
- 不得清空 WindowBuffer。
- 不得每輪重新載入 Model。

## 9.5 `start()`

Standalone `start()` 必須保留：

```python
def start(self) -> None:
    self.initialize()
    while True:
        self.run_once()
        time.sleep(self.reader.poll_interval)
```

實作可加入 `KeyboardInterrupt` 正常停止，但不得改變獨立執行能力。

---

# 10. EventDetectionRunner Interface

## 10.1 Constructor

```python
class EventDetectionRunner:
    def __init__(
        self,
        config_path: str | Path = "configs/event_runner.yaml",
        pipeline_overrides: Mapping[str, DetectionPipeline] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        ...
```

Constructor 必須完成：

```text
load Runner Config
→ validate Runner Config
→ validate override names
→ initialize enabled pipelines in fixed order
→ create PipelineRuntimeState
→ set each next_due = clock()
→ ready
```

任何 Fatal Initialization Error 必須在 Constructor 完成前傳出。

## 10.2 `run_once()`

```python
def run_once(self) -> EventRunnerCycleResult:
    ...
```

行為：

- 強制執行所有 Enabled Pipeline 一次。
- 忽略 `next_due`。
- 固定循序執行。
- 單 Pipeline Runtime Error 隔離。
- 回傳 `mode="FORCED"`。
- 不修改 Scheduler 的 `next_due`。
- 適合 Unit Test、手動驗證與 SPEC-005 E2E Gate。

## 10.3 `run_due_once()`

```python
def run_due_once(self) -> EventRunnerCycleResult:
    ...
```

行為：

- 固定順序逐一檢查各 Pipeline。
- 每條 Pipeline 檢查前重新取得 `clock()`。
- `now >= next_due` 時執行。
- 未到期回傳 `SKIPPED_NOT_DUE`。
- 回傳 `mode="DUE_ONLY"`。
- 執行完畢後更新該 Pipeline `next_due`。

## 10.4 `start()`

```python
def start(self) -> None:
    ...
```

行為：

```text
log started
→ while stop not requested
     run_due_once()
     sleeper(tick_seconds)
→ graceful stop
```

## 10.5 `stop()`

```python
def stop(self) -> None:
    ...
```

只設定停止旗標：

```python
self._stop_requested = True
```

不得：

- Kill Process。
- Close EventStore。
- 清除 Event。
- 刪除 Model。
- 修改 Pipeline Config。

---

# 11. Sequential Execution Flow

## 11.1 固定順序

```text
1. log_event_detection
2. metrics_threshold_detection
3. metrics_iforest_detection
```

即使前一條失敗，仍必須執行下一條。

## 11.2 Single Pipeline Wrapper

建議介面：

```python
def _execute_pipeline(
    self,
    pipeline_name: str,
    detector: DetectionPipeline,
    scheduled_due: float | None,
) -> PipelineRunResult:
    ...
```

流程：

```text
record wall-clock started_at
record monotonic start
→ detector.run_once()
→ validate returned list[dict]
→ success result

Exception
→ logger.exception(...)
→ failed result

finally
→ calculate duration_ms
```

## 11.3 為何允許 Wrapper 使用 `except Exception`

Detector 的 `run_once()` 不應用過度寬泛 Exception 隱藏程式錯誤；但 Event Runner 的職責是隔離 Pipeline，因此可在**單一 Pipeline 邊界**捕捉 `Exception`。

必要條件：

- 使用 `logger.exception()` 保留 Stack Trace。
- `PipelineRunResult` 明確標記 `FAILED`。
- 不把失敗偽裝成 `SUCCESS + []`。
- 不吞掉 Startup Error；只在 Constructor 完成後的 Runtime Wrapper 使用。

## 11.4 Event 彙整

```text
Log events + Threshold events + IForest events
→ total_event_count
```

不得：

- 依 Event Type 排斥另一條 Pipeline。
- 依 Confidence 只保留最高者。
- 依 Service Name 去重。
- 合併 Triggered Features。
- 重新生成 Event ID。
- 修改 Event Status。

---

# 12. Scheduler Specification

## 12.1 Time Source

排程必須使用：

```python
time.monotonic()
```

Wall Clock 只用於人類可讀的 `started_at`／`completed_at` Logging。

不得用 `datetime.now()` 比較排程 Interval。

## 12.2 Initial Due

Constructor 完成時：

```python
initial_now = clock()
next_due[pipeline] = initial_now
```

因此 `start()` 的第一個 Cycle 會立即執行所有 Enabled Pipeline。

## 12.3 Update Next Due

Pipeline 完成後，以前一個 Scheduled Due 為基準：

```python
next_due = previous_due + interval_seconds
while next_due <= finished_monotonic:
    next_due += interval_seconds
```

此政策代表：

- 維持固定節奏。
- 若執行過久，跳過已錯過的 Slot。
- 不立刻重跑多次補回積欠週期。
- 避免 Catch-up Storm。

## 12.4 Scheduler Lag

```python
scheduler_lag_ms = max(
    0.0,
    (actual_start_monotonic - scheduled_due_monotonic) * 1000,
)
```

只有實際執行的 Pipeline 計算 Lag。

## 12.5 Overrun Warning

若：

```text
duration_seconds >= interval_seconds
```

必須記錄 WARNING，至少包含：

- Pipeline Name。
- Duration。
- Interval。
- Scheduler Lag。

不得因 Overrun 自動啟用 Thread 或改變 Pipeline 順序。

## 12.6 Example Schedule

預設：

```text
Log：5 秒
Threshold：15 秒
IForest：15 秒
```

理想執行：

```text
00s：Log → Threshold → IForest
05s：Log
10s：Log
15s：Log → Threshold → IForest
20s：Log
```

這是循序式執行，不代表 Log Pipeline 持續 Blocking 到完成才永久讓出控制權。

---

# 13. Startup 與 Runtime Lifecycle

## 13.1 Startup Flow

```text
load event_runner.yaml
→ validate config
→ select enabled pipelines
→ initialize Log Detector and load Log Model
→ initialize Threshold Detector
→ initialize IForest Detector and validate Model Artifact
→ initialize scheduler state
→ ready
```

## 13.2 Fail Fast

以下屬於 Fatal Startup Error：

- Runner Config 不存在。
- Runner YAML 格式錯誤。
- Runner Config 欄位不合法。
- Enabled Pipeline Config 不存在。
- 所有 Pipeline Disabled。
- Unknown Pipeline Name。
- Log Model 不存在或 Load 失敗。
- Metrics IForest Model 不存在且 `train_if_missing=false`。
- Metrics IForest Model Metadata／Version 不相容。
- Enabled Detector Constructor 失敗。
- Pipeline Override 名稱不合法。

Fatal Error：

```text
不得進入 start loop
不得將 Runner 標記為 Ready
直接 raise 明確 Exception
```

## 13.3 Recoverable Runtime

進入 Runtime 後：

- Prometheus Timeout。
- 暫時性連線錯誤。
- 本輪無資料。
- 單筆 Log Parse Skip。
- 單 Pipeline Unexpected Exception。

處理：

```text
record current pipeline result
→ continue remaining pipelines
→ retry on next scheduled cycle
```

## 13.4 Graceful Shutdown

`KeyboardInterrupt`：

- 記錄 INFO。
- 設定停止旗標。
- 正常離開 Loop。
- 不顯示未處理 Traceback。
- 不刪除 Runtime Artifact。

---

# 14. Event Semantics 與資料完整性

## 14.1 Event Schema Ownership

Event Runner 不重新定義 Event Schema。

所有 Event 必須由 SPEC-001～003 建立為 PRD-002 的 15 欄位：

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

## 14.2 Event 不可變原則

Event Runner 收到 Event 後不得：

- 新增 `runner_id`。
- 新增 `cycle_id`。
- 新增 `incident_id`。
- 新增 `pipeline_status`。
- 修改 `detected_at`。
- 修改 `severity` 或 `confidence`。
- 修改 `status`。
- 將 Pipeline Runtime 資訊放進 `triggered_features`。

Runner Runtime 資訊只能存在 `PipelineRunResult` 與 Log。

## 14.3 Cross-Pipeline OR Test Case

Fake Threshold：

```text
returns [high_memory_detected]
```

Fake IForest：

```text
returns [general_metrics_anomaly]
```

Expected：

```text
total_event_count == 2
兩筆 Event 內容不變
```

不得因同屬 Metrics 而只保留一筆。

---

# 15. Error Handling

## 15.1 Fatal Initialization Errors

| 情境 | 行為 |
|---|---|
| Runner Config 不存在 | `FileNotFoundError` |
| YAML 錯誤 | Raise Parse Error |
| Config 欄位不合法 | `ValueError` |
| Unknown Pipeline | `ValueError` |
| 所有 Pipeline Disabled | `ValueError` |
| Enabled Config Path 不存在 | `FileNotFoundError` |
| Log Model Load Error | Propagate Original／Domain Error |
| Metrics IForest Model Missing | `MetricsIForestModelNotFoundError` |
| Metrics IForest Metadata Error | `MetricsIForestModelVersionError` |
| Override Name 不合法 | `ValueError` |

## 15.2 Runtime Pipeline Errors

| 情境 | 行為 |
|---|---|
| Detector 回傳 `None` | Pipeline `FAILED`，其他繼續 |
| Detector 回傳非 List | Pipeline `FAILED`，其他繼續 |
| List 包含非 Dict | Pipeline `FAILED`，其他繼續 |
| Detector Unexpected Exception | `logger.exception`，Pipeline `FAILED`，其他繼續 |
| Detector 回傳 `[]` | Pipeline `SUCCESS`，event_count=0 |
| Pipeline Overrun | `WARNING`，正常更新 next_due |
| Scheduler Lag | 記錄 Lag，不額外 Catch-up |

## 15.3 Error Result Safety

Error Result 不得包含：

- 完整環境變數。
- API Key。
- Password。
- Token。
- Pickle 內容。
- 真實個資。

`error_message` 可保留可診斷文字，但 Runtime Log 必須遵守 PRD-001 資安邊界。

---

# 16. Logging 與 Observability

## 16.1 Log Level

| Level | 使用情境 |
|---|---|
| DEBUG | Pipeline Not Due、No Event、Next Due 更新 |
| INFO | Config Loaded、Pipeline Initialized、Runner Started／Stopped、Cycle Summary |
| WARNING | Event Count > 0、Pipeline Overrun、Scheduler Lag 明顯 |
| ERROR | Pipeline Runtime Failure、Invalid Return Contract、Startup Failure Context |

## 16.2 Required Context

Pipeline Log 至少包含：

- `pipeline_name`
- `status`
- `duration_ms`
- `event_count`
- `scheduler_lag_ms`（若適用）
- `error_type`（若失敗）

## 16.3 Cycle Summary

每個 `run_once()`／`run_due_once()` 完成後可記錄：

```text
mode
success_count
failure_count
skipped_count
total_event_count
duration_ms
```

不得逐筆 Log 完整印出敏感內容。

## 16.4 Logger Name

建議：

```python
logger = logging.getLogger("EventDetectionRunner")
```

既有 Log Runner 可使用：

```python
logging.getLogger("LogEventDetectionRunner")
```

不得在 Import 時重複呼叫全域 `logging.basicConfig()` 造成其他模組設定被覆蓋；若既有程式已存在，應以最小修改維持 Regression，並在必要時回報 PM。

---

# 17. CLI 與人工執行

## 17.1 Module Entry Point

`src/event_detection/runner.py` 的 `__main__` 應改為統一 Event Runner CLI。

建議：

```powershell
python -m src.event_detection.runner --config configs/event_runner.yaml
```

持續執行 Scheduler。

## 17.2 Forced Single Cycle

```powershell
python -m src.event_detection.runner --config configs/event_runner.yaml --once
```

行為：

- 建立 Runner。
- Startup Error Fail Fast。
- 強制執行所有 Enabled Pipeline 一次。
- 印出簡潔 Cycle Summary。
- Exit Code 0 表示 Runner 成功完成 Cycle，即使 Event Count 為 0。
- Fatal Startup Error 以非 0 結束。

## 17.3 Manual Runtime Prerequisites

正式 Detector Manual Run 前需具備：

```text
models/log_isolation_forest.pkl
models/metrics_isolation_forest.pkl
```

Metrics Pipeline 若要查詢真實 Prometheus，DDS-001 服務也需啟動。

上述不屬於必要 Unit Test 前置條件。

---

# 18. Test Specification

## 18.1 Testing Principles

所有必要測試：

- 不依賴 Docker。
- 不依賴真實 Prometheus。
- 不依賴 Generator。
- 不依賴外部網路。
- 不依賴正式 Model File。
- 不實際等待 5 秒或 15 秒。
- 使用 Fake Clock／Sleeper。
- 使用 Fake Detector。
- Log File 使用 `tmp_path`。
- 不寫入真實 `events/event_store.jsonl`。
- 不修改既有 SPEC-001～003 Tests。
- 可由所有成員與 CI 重現。

## 18.2 Required Test File

```text
tests/test_event_runner.py
```

## 18.3 Config Tests

- 合法 Config 成功載入。
- Config 不存在。
- YAML 錯誤。
- 缺 `runtime`／`pipelines`。
- `tick_seconds <= 0`。
- Unknown Pipeline Key。
- 缺固定 Pipeline Key。
- `enabled` 非 Boolean。
- `interval_seconds <= 0`、NaN、Inf。
- Enabled Config Path 不存在。
- 所有 Pipeline Disabled。
- Tick 大於最小 Interval。
- Threshold Interval 非 15 秒。
- IForest Interval 非 15 秒。

## 18.4 Initialization Tests

- 只建立 Enabled Pipeline。
- Disabled IForest 不載入 Model。
- Override 不建立正式 Detector。
- Unknown Override Name 失敗。
- 正式 Detector Startup Error 直接傳出。
- Constructor 完成時所有 Enabled Pipeline `next_due` 相同且為當前 Clock。

## 18.5 LogReader Tests

- 首次檔案已存在時定位 EOF 並回傳 `[]`。
- 後續新增行只讀一次。
- 空白行跳過。
- 檔案不存在回傳 `[]`。
- 啟動後才建立檔案時從開頭讀取。
- inode Rotation 從新檔開頭讀取。
- 同 inode Truncation 重設 Offset。
- `read_all()` 不改 Runtime Offset。
- `tail()` 重用 `read_new_lines_once()` 且仍可 Yield。
- `read_new_lines_once()` 不呼叫 Sleep。

## 18.6 LogEventDetectionRunner Tests

- `initialize()` 只 Load Model 一次。
- Model Load Error 傳出。
- 沒有新 Log 回傳 `[]`。
- Parser Skip 回傳 `[]`。
- Window 不足回傳 `[]`。
- 正常 Window 回傳 `[]`。
- 異常 Window 成功寫入並回傳 Event。
- Cooldown 內不重複回傳。
- EventStore 失敗不回傳 Event且不記錄 Cooldown。
- 多行批次可回傳多筆成功 Event。
- `run_once()` 不 Sleep。
- 原有 Window Feature／Classification 測試仍通過。

可使用 Fake Reader、Fake Predictor、Fake Builder 與 Temporary EventStore，必要測試不需正式 Log Model。

## 18.7 Forced `run_once()` Tests

- 三條 Enabled Pipeline 都被執行。
- 執行順序固定。
- Disabled Pipeline 不執行。
- 三條皆回傳空 List，Cycle 成功且 total=0。
- 三條各回傳 Event，正確彙整。
- Threshold 與 IForest 同時回傳 Event，兩筆都保留。
- Event Dict 內容完全不變。
- 統一 `EventDetectionRunner` 不呼叫 EventStore；既有 `LogEventDetectionRunner` 仍由自身負責正式寫入。
- 單 Pipeline 失敗，其他 Pipeline 仍執行。
- 第一條失敗不阻止第二、第三條。
- 兩條失敗，第三條仍可成功。
- Invalid Return Type 標記 Failed。
- Event Count／Success／Failure／Skipped 計算正確。
- `run_once()` 不修改 `next_due`。

## 18.8 Scheduler Tests

使用 Fake Clock，不可真的 Sleep。

- Initial Due：第一輪三條皆執行。
- 5 秒：只有 Log 到期。
- 10 秒：只有 Log 到期。
- 15 秒：三條皆到期。
- 未到期 Pipeline 標記 `SKIPPED_NOT_DUE`。
- 各 Pipeline 檢查前重新讀 Clock。
- Runtime Error 後仍更新至下一個 Schedule Slot。
- Overrun 時跳過過去 Slot，不 Burst Catch-up。
- Pipeline Duration >= Interval 時記錄 Warning。
- Scheduler Lag 計算不為負數。
- Fixed Order 在同時到期時保持一致。

## 18.9 `start()`／`stop()` Tests

- `start()` 重複呼叫 `run_due_once()`。
- 每 Cycle 使用 Config `tick_seconds` 呼叫 Sleeper。
- Fake Sleeper 可觸發 `stop()`，Loop 正常結束。
- `KeyboardInterrupt` 正常停止。
- Stop 後不進入下一輪。
- 不留下未捕捉 Exception。

## 18.10 Fail Fast Tests

- Runner Config Missing。
- Enabled Child Config Missing。
- Log Model Initialization Error。
- IForest Model Missing Error。
- IForest Metadata Error。
- 所有 Pipeline Disabled。

上述錯誤不得被包成 Runtime `PipelineRunResult`；必須在 Runner Startup 直接 Raise。

## 18.11 Regression Tests

必要命令：

```powershell
python -m pytest tests/test_event_runner.py -q
python -m pytest -q
```

全專案既有測試不得失敗。

## 18.12 Optional Manual Integration

不列入必要 CI Gate，可於本機環境執行：

```powershell
python -m src.event_detection.runner --config configs/event_runner.yaml --once
```

持續模式：

```powershell
python -m src.event_detection.runner --config configs/event_runner.yaml
```

正式 E2E 情境驗證延後至 SPEC-005。

---

# 19. Acceptance Criteria

## 19.1 Scope 與 Architecture

- [ ] 建立統一 Event Detection Runner。
- [ ] v1.0 採單行程循序執行。
- [ ] 固定順序為 Log → Threshold → IForest。
- [ ] 未使用 Thread、Process、AsyncIO、Kafka 或第三方 Scheduler。
- [ ] 未修改 Generator、Docker、Prometheus 或 Grafana。
- [ ] 未實作 Alert Correlation、Incident、LLM、RAG、RCA、Dashboard 或 Email。

## 19.2 Pipeline Independence

- [ ] 三條 Pipeline 可獨立 Enabled／Disabled。
- [ ] Disabled Pipeline 不初始化。
- [ ] 單一 Runtime Error 不影響其他 Pipeline。
- [ ] Metrics Threshold 與 IForest 採 OR 接納。
- [ ] 同一事故的多來源 Event 不被 Event Runner 去重。
- [ ] 不要求另一條 Pipeline 同時成功才接納 Event。

## 19.3 Event Ownership

- [ ] Event 由 Detector 建立。
- [ ] Event 由 Detector 寫入 EventStore。
- [ ] Event Runner 不再次寫入 EventStore。
- [ ] Event Runner 不修改 Event。
- [ ] Event Runner 不新增 Top-level 欄位。
- [ ] Event Runner 不建立 Incident。

## 19.4 Log Non-blocking Contract

- [ ] `LogReader.read_new_lines_once()` 完成。
- [ ] `LogEventDetectionRunner.initialize()` 完成且 Idempotent。
- [ ] `LogEventDetectionRunner.run_once()` 完成且非 Blocking。
- [ ] 原有 `tail()`、`read_all()`、`start()` 保持相容。
- [ ] Offset、Rotation、Truncation 不造成重複讀取或永久漏讀。

## 19.5 Scheduler

- [ ] Event Runner Config 是整合排程唯一來源。
- [ ] Metrics 兩條 Pipeline 固定 15 秒。
- [ ] 使用 `time.monotonic()`。
- [ ] Initial Cycle 立即執行 Enabled Pipelines。
- [ ] 未到期 Pipeline Skip。
- [ ] Overrun 不 Burst Catch-up。
- [ ] Scheduler Lag 與 Duration 可觀測。
- [ ] `stop()` 與 KeyboardInterrupt 可安全結束。

## 19.6 Error Strategy

- [ ] Startup Error Fail Fast。
- [ ] Runtime Error Isolation。
- [ ] Failed Pipeline 有明確 Result 與 Log。
- [ ] 不把 Runtime Failure 偽裝成 Success。
- [ ] 下一 Schedule Cycle 可重試。

## 19.7 Tests 與治理

- [ ] `tests/test_event_runner.py` 全部通過。
- [ ] Full Regression 全部通過。
- [ ] 必要測試不依賴 Docker、Prometheus、Generator、網路、正式 Model 或真實 Sleep。
- [ ] 未修改禁止範圍。
- [ ] 未修改 Dependency Files。
- [ ] AI Coding Agent 未執行 Git 指令。
- [ ] Runtime Artifact 未準備提交。

---

# 20. Known Limitations 與 Deferred Integration

## 20.1 Sequential Prototype Trade-off

v1.0 採循序式 Runner，優先確保：

- 可重現。
- 可測試。
- 執行順序明確。
- 錯誤容易定位。
- 避免多執行緒共享狀態。
- 避免 JSONL 併發寫入。

未來面對高吞吐、低延遲或多節點需求，可評估：

```text
Thread / Process
→ Async Runtime
→ Message Queue
→ Kafka / Stream Processing
```

此演進方向屬於 SDD Trade-off 與 Future Work，不屬於 SPEC-004 v1.0。

## 20.2 Metrics Instant Query Limitation

SPEC-002 使用 Prometheus Instant Query。短於 Scrape／Polling 間隔的瞬時 Spike 可能在查詢前恢復，因此不保證被 Threshold Pipeline 捕捉。

SPEC-004 的循序 Scheduler不解決此限制。

SPEC-005 為了建立可重現的 E2E Demo，可讓 S2／S3 Metrics 異常維持足夠時間跨過至少一次 Scrape 與 Detection Poll；此作法是測試資料設計，不代表已解決所有瞬時 Spike。

未來可評估：

```text
Prometheus query_range
PromQL max_over_time / avg_over_time
Recording Rules
Streaming Metrics Processing
```

若正式修改 Threshold 語意，必須先更新 PRD-002 與 SPEC-002，不得由 SPEC-005 偷偷改變 Contract。

## 20.3 JSONL Queue Limitation

`events/event_store.jsonl` 是 Prototype 的持久化 Event Store，不是具備：

- Consumer Offset。
- ACK。
- Retry Queue。
- Partition。
- Exactly-once Delivery。

的正式 Message Queue。

未來若改為 Kafka 或其他 Queue，需另立 ADR／PRD／SPEC。

## 20.4 SPEC-005 Deferred Integration

SPEC-004 完成後，由 PM 另行安排：

```text
六大 Scenario Generator Audit
→ Log / Metrics Generator Patch
→ Prometheus / Grafana Validation
→ SPEC-001 / 002 / 003 / 004 真實整合
→ 六大情境 E2E Gate
→ develop 合併 main
```

SPEC-005 原則：

> Generator 對齊 PRD-002 與 SPEC-001～004，不為了配合不完整 Generator 回頭放寬穩定 Detection Contract。

若發現正式 Contract Defect，必須回報 PM，另以 SPEC Revision／Integration Fix 處理。

## 20.5 不屬於本次驗收

- 真實 Generator 六大情境全通過。
- Grafana Demo 畫面。
- Alert Correlation。
- Incident 建立。
- RCA。
- Dashboard／Email。
- 多執行緒或 Kafka。
- 瞬時 Metrics Spike 完整捕捉。

---

# 21. Final Deliverables

完成後 `feature/event-runner` 應包含：

```text
configs/event_runner.yaml
src/event_detection/runner.py
src/event_detection/log/reader.py
tests/test_event_runner.py
```

可選：

```text
tests/fixtures/event_runner/
```

不得包含：

```text
events/event_store.jsonl
models/*.pkl
logs/*.log
.venv/
Docker 修改
Generator 修改
Dashboard 修改
README 修改
docs 修改（除 PM 事先安排）
```

---

# 22. PM Review Checklist

## 22.1 File Scope

- [ ] 是否只修改允許檔案？
- [ ] 是否沒有修改 Generator、Docker、README、docs 或 Dependency Files？
- [ ] 是否沒有修改 SPEC-002／003 Module？
- [ ] Runtime Artifact 是否未納入？

## 22.2 Log Refactor

- [ ] `read_new_lines_once()` 是否非 Blocking？
- [ ] Offset／Rotation／Truncation 是否正確？
- [ ] `tail()` 是否共用單輪 Reader Logic？
- [ ] `run_once()` 是否回傳成功 Event List？
- [ ] Model 是否只 Load 一次？
- [ ] SPEC-001 Detection Logic 是否未改變？

## 22.3 Orchestration

- [ ] 三條 Pipeline 是否固定順序？
- [ ] Metrics 雙軌是否 OR 接納？
- [ ] 是否沒有 Cross-Pipeline Dedup？
- [ ] 是否沒有再次 EventStore.write？
- [ ] Event 是否保持不變？
- [ ] Disabled Pipeline 是否不初始化？

## 22.4 Scheduler

- [ ] 是否使用 `time.monotonic()`？
- [ ] Metrics Interval 是否固定 15 秒？
- [ ] Initial Due 是否立即執行？
- [ ] Overrun 是否跳過舊 Slot而不 Burst Catch-up？
- [ ] Tests 是否使用 Fake Clock／Sleeper？

## 22.5 Error Strategy

- [ ] Startup Error 是否 Fail Fast？
- [ ] Runtime Error 是否隔離？
- [ ] 失敗是否有明確 Result／Stack Trace？
- [ ] KeyboardInterrupt 是否正常停止？

## 22.6 Test Gate

- [ ] `python -m pytest tests/test_event_runner.py -q` 是否通過？
- [ ] `python -m pytest -q` 是否通過？
- [ ] Baseline Regression 是否無倒退？
- [ ] 必要測試是否不依賴 Docker／Prometheus／Generator／網路／正式 Model？
- [ ] 是否未實際 Sleep 5 或 15 秒？

---

# 23. Traceability Matrix

| SPEC-004 Requirement | 上游依據 | 驗證方式 |
|---|---|---|
| 三條 Pipeline 統一執行 | PRD-002 FR-03 | Initialization／run_once Tests |
| 單 Pipeline 失敗不影響其他 | PRD-002 FR-03、AC-08 | Runtime Error Isolation Tests |
| Metrics 雙軌 OR 接納 | PRD-002 FR-02 | Cross-Pipeline OR Test |
| Event Schema 不修改 | PRD-002 第 5 章 | Event Immutability Test |
| Metrics 15 秒 | PRD-002 NFR-01 | Config／Scheduler Tests |
| Log 不重複讀取 | PRD-002 NFR-02 | LogReader Offset Tests |
| Event 寫入 JSONL | PRD-002 NFR-06 | Existing Detector Regression |
| Fail Fast | SPEC-003 Model Lifecycle、PM 決策 | Startup Failure Tests |
| Generator 延後驗證 | SPEC-001 Phase 4、SPEC-003 Deferred Integration | Scope Audit |
| 不做 Alert／Incident | PRD-002 Out of Scope | File／Behavior Audit |

---

# 24. Definition of Done

SPEC-004 只有在以下全部成立時才算完成：

```text
正式 Config 完成
+ Log 非 Blocking Contract 完成
+ 三條 Pipeline 統一 Runner 完成
+ Sequential Scheduler 完成
+ Startup Fail Fast 完成
+ Runtime Error Isolation 完成
+ OR 接納完成
+ Graceful Shutdown 完成
+ SPEC-004 Tests 全通過
+ Full Regression 全通過
+ File Scope Audit 通過
+ PM Review 通過
```

SPEC-004 完成不等於 Event Detection Layer 已可合併 main。

下一步仍需 SPEC-005 與六大情境 E2E Gate。
