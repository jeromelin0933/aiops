# SPEC-001：Log Event Detection
## Software Design Specification v2.0（ML Pipeline 版）

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-001 v2.0 |
| Document Name | Log Event Detection — ML Pipeline |
| Status | Ready for Implementation |
| Date | 2026-07-14 |
| Related PRD | PRD-001、PRD-002 |
| Related DDS | DDS-001 |
| Implements | PRD-002 FR-01、FR-03、FR-04、FR-05、FR-06 |
| Target | AI Coding Agent（Claude Code / Cursor / Copilot） |

---

## 0. 設計說明

### 0.1 與上一版的本質差異

上一版（v1.0）把六個 Scenario 直接硬編碼為六條 Detection Rule，屬於 Rule-Based System。

**本版（v2.0）改為 Machine Learning Pipeline。**

六個 Scenario 的角色完全改變：

| | v1.0 | v2.0 |
|---|---|---|
| S1–S6 | 偵測規則，寫死在程式裡 | 驗證資料集（Validation Dataset） |
| 偵測方式 | Pattern matching | Isolation Forest 學習正常分佈 |
| 新異常類型 | 需要改程式新增 Rule | 只需重訓模型 |
| 統計依據 | 無（人工設定閾值） | 有（模型學習到的正常基準） |

### 0.2 整體 Pipeline

```
logs/aiops.json.log
        │
        ▼
  [ Log Reader ]        tail 模式，持續讀取新行
        │
        ▼
  [ Log Parser ]        JSON 解析 + Schema 驗證 + Timestamp 標準化
        │
        ▼
[ Feature Extractor ]   每筆 Log → RawFeatures dataclass
        │
        ▼
[ Feature Encoder ]     類別/布林特徵數值化 → EncodedFeatureVector（19 維）
        │
        ▼
 [ Window Buffer ]      累積視窗內所有 EncodedFeatureVector + 原始 Log
        │ （每 poll 週期）
        ▼
[ Anomaly Predictor ]   Isolation Forest predict() + decision_function()
        │
        ▼
 [ Event Builder ]      組裝 PRD-002 Event Schema
        │
        ▼
  [ Event Store ]       寫入 events/event_store.jsonl
```

---

## 1. 檔案結構

```
專案根目錄/
│
├── src/
│   └── event_detection/            ← 本 SPEC 全部新建於此
│       ├── __init__.py
│       ├── runner.py               ← 主執行入口，整合所有模組
│       │
│       ├── log/
│       │   ├── __init__.py
│       │   ├── reader.py           ← LogReader：tail 模式讀取 Log
│       │   ├── parser.py           ← LogParser：JSON 解析與 Schema 驗證
│       │   ├── features.py         ← FeatureExtractor：抽取原始特徵
│       │   └── encoder.py          ← FeatureEncoder：特徵數值化
│       │
│       ├── model/
│       │   ├── __init__.py
│       │   ├── schema.py           ← RawFeatures / EncodedFeatureVector / WindowSummary dataclass
│       │   ├── trainer.py          ← ModelTrainer：訓練並儲存 Isolation Forest
│       │   └── predictor.py        ← AnomalyPredictor：載入模型，執行推論
│       │
│       ├── event/
│       │   ├── __init__.py
│       │   └── builder.py          ← EventBuilder：組裝 Event dict
│       │
│       └── store/
│           ├── __init__.py
│           └── event_store.py      ← EventStore：寫入 event_store.jsonl
│
├── scripts/
│   ├── train_log_model.py          ← 一次性訓練腳本（新增）
│   └── validate_log_detection.py  ← Scenario 驗收腳本（新增）
│
├── models/                         ← 新建目錄，加入 .gitignore
│   └── log_isolation_forest.pkl   ← 訓練後自動產生
│
├── events/                         ← 新建目錄，加入 .gitignore
│   └── event_store.jsonl
│
└── configs/
    └── event_detection.yml         ← 新增設定檔

不得修改（DDS-001 完成物）：
  src/log_generator/log_generator.py
  src/metrics_generator/metrics_generator.py
  docker-compose.yml
  docker/prometheus/prometheus.yml
  docker/grafana/dashboard.json
  docker/promtail/promtail-config.yml
```

---

## 2. Config

```yaml
# configs/event_detection.yml

log_reader:
  log_file_path: "logs/aiops.json.log"
  poll_interval_seconds: 5
  encoding: "utf-8"

feature_extraction:
  # Label Encoding 的合法值清單
  # 必須涵蓋 log_generator.py 產生的所有 service_name
  # index 0 保留給 unknown，清單從 index 1 開始對應
  known_services:
    - "auth-service"
    - "payment-service"
    - "order-service"
    - "member-service"
    - "api-gateway"
    - "notification-service"
    - "database-service"
    - "remittance-service"
    - "report-service"

  # 必須涵蓋 log_generator.py 產生的所有 error_type
  known_error_types:
    - "AuthenticationFailed"
    - "AccountLocked"
    - "SlowQuery"
    - "UpstreamTimeout"
    - "GatewayTimeout"
    - "OutOfMemoryError"
    - "HighMemoryUsage"
    - "BadGateway"
    - "ExternalServiceTimeout"
    - "TransactionFailed"
    - "ConnectionRefused"
    - "ServiceUnavailable"
    - "RateLimitExceeded"
    - "InternalError"
    - "BadRequest"

window:
  window_seconds: 60       # 滑動視窗長度（秒）
  min_log_count: 5         # 視窗內至少幾筆 Log 才執行推論

isolation_forest:
  contamination: 0.05      # 預期約 5% 的資料為異常
  n_estimators: 200
  random_state: 42
  max_samples: "auto"

anomaly:
  # decision_function 分數低於此值才建立 Event（避免噪音誤報）
  score_threshold: -0.05
  # Confidence 分級邊界（詳見 predictor.py）
  confidence_high_threshold: -0.3
  confidence_medium_threshold: -0.1

event:
  cooldown_seconds: 60     # 同 event_type 在 N 秒內不重複觸發

output:
  event_store_path: "events/event_store.jsonl"
  model_path: "models/log_isolation_forest.pkl"
```

---

## 3. model/schema.py — 資料結構定義

```python
# src/event_detection/model/schema.py
#
# 職責：定義本模組所有跨層傳遞的資料結構。
# 所有模組透過此 dataclass 溝通，不使用裸 dict 或 list。
# 此檔案不含任何業務邏輯，只有資料結構。

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class RawFeatures:
    """
    從單一 Log 條目抽取的原始特徵（尚未數值化）。
    由 FeatureExtractor.extract_one() 產生。
    """

    # ── 數值欄位（可直接使用，無需 Encoding）──────────────────
    status_code:       int   = 200
    duration_ms:       int   = 0
    memory_usage_pct:  float = 0.0
    rate_limit_quota:  int   = 0

    # ── 類別欄位（需要 Label Encoding）────────────────────────
    service_name:      str   = "unknown"
    error_type:        str   = "unknown"   # null → "unknown"

    # ── level（固定 Encoding：INFO=1, WARN=2, ERROR=3）────────
    level:             str   = "INFO"

    # ── Boolean 欄位（null 判斷 → 0/1）───────────────────────
    has_source_ip:          bool = False   # source_ip 不為 null
    has_downstream_service: bool = False   # downstream_service 不為 null
    has_external_service:   bool = False   # external_service 不為 null
    has_transaction_id:     bool = False   # transaction_id 不為 null
    has_target_service:     bool = False   # target_service 不為 null

    # ── 衍生 Boolean 特徵（從數值欄位計算）───────────────────
    is_error: bool = False   # level == "ERROR"
    is_warn:  bool = False   # level == "WARN"
    is_5xx:   bool = False   # status_code >= 500
    is_4xx:   bool = False   # 400 <= status_code < 500
    is_401:   bool = False   # status_code == 401（暴力攻擊指標）
    is_429:   bool = False   # status_code == 429（Rate Limit 指標）
    is_oom:   bool = False   # error_type == "OutOfMemoryError"

    # ── 原始字串欄位（不進 Feature Vector，供 EventBuilder 使用）──
    raw_source_ip:          Optional[str] = None
    raw_downstream_service: Optional[str] = None
    raw_external_service:   Optional[str] = None
    raw_trace_id:           Optional[str] = None
    raw_transaction_id:     Optional[str] = None
    raw_target_service:     Optional[str] = None
    raw_timestamp:          Optional[str] = None


@dataclass
class EncodedFeatureVector:
    """
    數值化後的特徵向量（19 維），直接傳給 Isolation Forest。
    欄位順序固定，訓練與推論必須完全一致。

    維度說明：
      [0]  status_code            數值
      [1]  duration_ms            數值
      [2]  memory_usage_pct       數值
      [3]  rate_limit_quota       數值
      [4]  service_name_encoded   Label Encoding
      [5]  error_type_encoded     Label Encoding
      [6]  level_encoded          固定 Encoding
      [7]  has_source_ip          Boolean → 0/1
      [8]  has_downstream_service Boolean → 0/1
      [9]  has_external_service   Boolean → 0/1
      [10] has_transaction_id     Boolean → 0/1
      [11] has_target_service     Boolean → 0/1
      [12] is_error               Boolean → 0/1
      [13] is_warn                Boolean → 0/1
      [14] is_5xx                 Boolean → 0/1
      [15] is_4xx                 Boolean → 0/1
      [16] is_401                 Boolean → 0/1
      [17] is_429                 Boolean → 0/1
      [18] is_oom                 Boolean → 0/1
    """

    status_code:            float = 0.0
    duration_ms:            float = 0.0
    memory_usage_pct:       float = 0.0
    rate_limit_quota:       float = 0.0
    service_name_encoded:   float = 0.0
    error_type_encoded:     float = 0.0
    level_encoded:          float = 1.0   # 預設 INFO=1
    has_source_ip:          float = 0.0
    has_downstream_service: float = 0.0
    has_external_service:   float = 0.0
    has_transaction_id:     float = 0.0
    has_target_service:     float = 0.0
    is_error:               float = 0.0
    is_warn:                float = 0.0
    is_5xx:                 float = 0.0
    is_4xx:                 float = 0.0
    is_401:                 float = 0.0
    is_429:                 float = 0.0
    is_oom:                 float = 0.0

    def to_list(self) -> list:
        """
        以固定順序回傳 float list。
        此順序在訓練與推論時必須完全一致，不得更動。
        """
        return [
            self.status_code, self.duration_ms,
            self.memory_usage_pct, self.rate_limit_quota,
            self.service_name_encoded, self.error_type_encoded, self.level_encoded,
            self.has_source_ip, self.has_downstream_service,
            self.has_external_service, self.has_transaction_id, self.has_target_service,
            self.is_error, self.is_warn, self.is_5xx,
            self.is_4xx, self.is_401, self.is_429, self.is_oom,
        ]

    @staticmethod
    def feature_names() -> list:
        """回傳與 to_list() 對應的特徵名稱（供模型分析使用）。"""
        return [
            "status_code", "duration_ms",
            "memory_usage_pct", "rate_limit_quota",
            "service_name_encoded", "error_type_encoded", "level_encoded",
            "has_source_ip", "has_downstream_service",
            "has_external_service", "has_transaction_id", "has_target_service",
            "is_error", "is_warn", "is_5xx",
            "is_4xx", "is_401", "is_429", "is_oom",
        ]


@dataclass
class WindowSummary:
    """
    一個時間視窗的彙總資訊。
    由 runner.py 的 WindowBuffer.compute_summary() 產生，
    傳給 EventBuilder 組裝 triggered_features 和 raw_log_sample。
    不進 Feature Vector，只作為 Metadata。
    """
    window_start:                     str   = ""
    window_end:                       str   = ""
    total_log_count:                  int   = 0
    error_count:                      int   = 0
    warn_count:                       int   = 0
    unique_services:                  list  = field(default_factory=list)
    unique_trace_ids:                 list  = field(default_factory=list)
    top_error_types:                  list  = field(default_factory=list)
    max_duration_ms:                  float = 0.0
    mean_duration_ms:                 float = 0.0
    max_memory_pct:                   float = 0.0
    top_source_ip:                    Optional[str] = None
    top_source_ip_count:              int   = 0
    top_downstream:                   Optional[str] = None
    top_downstream_count:             int   = 0
    top_target_service:               Optional[str] = None
    affected_services_for_downstream: list  = field(default_factory=list)
    cross_service_trace_ids:          list  = field(default_factory=list)
    raw_log_sample:                   list  = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
```

---

## 4. log/reader.py — LogReader

```python
# src/event_detection/log/reader.py
#
# 職責：
#   以 tail 模式持續監看 logs/aiops.json.log，
#   讀取自上次位置之後新增的原始文字行，
#   以 generator 形式逐行 yield 給呼叫端。
#
# 每個 class / function 的職責：
#   LogReader.__init__()   初始化路徑與 poll 間隔，設定 offset=0
#   LogReader.tail()       無限 Generator，yield 新行；處理 inode 輪替
#   LogReader.read_all()   一次性讀取全部行（供訓練腳本使用）

import time
from pathlib import Path


class LogReader:
    """
    持續 tail logs/aiops.json.log 的讀取器。

    避免重複讀取的機制：
      記錄 file offset（上次讀到的 byte 位置）。
      每次 poll 用 f.seek(offset) 跳過已讀取的部分，
      只讀取 offset 之後新增的內容。

    Log 輪替偵測：
      比較每次讀取前後的 inode 號碼。
      若 inode 改變，代表原檔案被刪除並重建（log rotation），
      此時重設 offset=0 從頭讀取新檔案。
    """

    def __init__(self, log_file_path: str, poll_interval_seconds: int = 5):
        """
        Args:
            log_file_path:          Log 檔案路徑（相對或絕對）
            poll_interval_seconds:  每次 poll 的等待秒數
        """
        self.path          = Path(log_file_path)
        self.poll_interval = poll_interval_seconds
        self._offset: int  = 0
        self._inode:  int  = -1

    def tail(self):
        """
        無限 Generator，逐行 yield 新增的原始 Log 字串。

        行為規範：
          - 首次啟動：將 offset 定位到檔案末尾，只處理未來新增的行
          - 若 Log 檔案不存在：每隔 poll_interval 秒重試，直到出現為止
          - 若 inode 改變（Log 輪替）：重設 offset=0，從頭讀取
          - 空白行直接跳過，不 yield
          - 每 poll_interval 秒執行一次讀取

        Yields:
            str: 已 strip() 的原始文字行（不含換行符）
        """
        # 初次啟動：定位到末尾，只讀未來的新行
        if self.path.exists():
            stat          = self.path.stat()
            self._offset  = stat.st_size
            self._inode   = stat.st_ino

        while True:
            if not self.path.exists():
                time.sleep(self.poll_interval)
                continue

            current_stat = self.path.stat()
            # inode 改變 → 檔案被輪替，重設
            if current_stat.st_ino != self._inode:
                self._offset = 0
                self._inode  = current_stat.st_ino

            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                for raw_line in f:
                    stripped = raw_line.strip()
                    if stripped:
                        yield stripped
                self._offset = f.tell()

            time.sleep(self.poll_interval)

    def read_all(self) -> list:
        """
        一次性讀取 Log 檔案全部內容（供 train_log_model.py 使用）。
        不改變 offset 狀態，不影響 tail() 的運作。

        Returns:
            list[str]: 所有非空白行
        """
        if not self.path.exists():
            return []
        raw = self.path.read_text(encoding="utf-8", errors="replace")
        return [l.strip() for l in raw.splitlines() if l.strip()]
```

---

## 5. log/parser.py — LogParser

```python
# src/event_detection/log/parser.py
#
# 職責：
#   接收原始 JSON 字串，解析並驗證 Schema，
#   回傳標準化後的 dict 或 None（解析失敗時靜默丟棄）。
#
# 每個 function 的職責：
#   LogParser.parse()            主流程：JSON解析→Schema驗證→型別轉換→Timestamp解析
#   LogParser._validate()        確認 REQUIRED_FIELDS 存在
#   LogParser._coerce_types()    強制轉換數值欄位型別
#   LogParser._fill_optional()   補全缺失的可選欄位（填 None）
#   LogParser._parse_timestamp() 解析 ISO8601 字串為 datetime

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("LogParser")

REQUIRED_FIELDS = {"timestamp", "level", "service_name", "status_code", "duration_ms"}

OPTIONAL_FIELDS_DEFAULT = {
    "trace_id": None, "error_type": None, "error_message": None,
    "source_ip": None, "user_id": None, "downstream_service": None,
    "external_service": None, "transaction_id": None,
    "memory_usage_pct": None, "target_service": None, "rate_limit_quota": None,
}


class LogParser:
    """
    JSON Log 解析器與 Schema 驗證器。

    設計原則：
      所有解析失敗靜默處理（不拋例外），回傳 None。
      確保單一壞掉的 Log 不中斷整個 Pipeline。
    """

    def parse(self, raw_line: str) -> Optional[dict]:
        """
        解析單一 Log 字串。

        處理流程：
          1. json.loads() 解析原始字串
          2. 確認型別為 dict
          3. _validate() 必要欄位檢查
          4. _fill_optional() 補全可選欄位
          5. _coerce_types() 數值型別強制轉換
          6. _parse_timestamp() 解析 timestamp

        Args:
            raw_line: LogReader yield 的原始字串

        Returns:
            dict: 解析完成，包含所有欄位 + "_parsed_timestamp" key
            None: 任一步驟失敗
        """
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError as e:
            logger.debug(f"JSON decode error: {e} | {raw_line[:80]}")
            return None

        if not isinstance(entry, dict):
            return None

        if not self._validate(entry):
            return None

        self._fill_optional(entry)

        if not self._coerce_types(entry):
            return None

        parsed_ts = self._parse_timestamp(entry.get("timestamp", ""))
        if parsed_ts is None:
            logger.debug(f"Cannot parse timestamp: {entry.get('timestamp')}")
            return None

        entry["_parsed_timestamp"] = parsed_ts
        return entry

    def _validate(self, entry: dict) -> bool:
        """
        確認所有 REQUIRED_FIELDS 存在於 entry。

        Args:
            entry: 已 JSON 解析的 dict

        Returns:
            True: 所有必要欄位存在
            False: 有缺失欄位（不 raise，直接回傳 False）
        """
        for field in REQUIRED_FIELDS:
            if field not in entry:
                logger.debug(f"Missing field '{field}'")
                return False
        return True

    def _fill_optional(self, entry: dict) -> None:
        """
        將 OPTIONAL_FIELDS_DEFAULT 中缺失的欄位補上 None。
        直接修改傳入的 dict（in-place）。
        """
        for field, default in OPTIONAL_FIELDS_DEFAULT.items():
            if field not in entry:
                entry[field] = default

    def _coerce_types(self, entry: dict) -> bool:
        """
        強制轉換數值欄位的型別，處理 Log Generator 可能輸出字串數字的情況。

        轉換規則：
          status_code     → int
          duration_ms     → int
          memory_usage_pct→ float（若不為 None）
          rate_limit_quota→ int（若不為 None）

        Returns:
            True: 轉換成功
            False: 轉換失敗（不可轉換的值）
        """
        try:
            entry["status_code"] = int(entry["status_code"])
            entry["duration_ms"] = int(entry["duration_ms"])
            if entry.get("memory_usage_pct") is not None:
                entry["memory_usage_pct"] = float(entry["memory_usage_pct"])
            if entry.get("rate_limit_quota") is not None:
                entry["rate_limit_quota"] = int(entry["rate_limit_quota"])
            return True
        except (ValueError, TypeError) as e:
            logger.debug(f"Type coercion error: {e}")
            return False

    def _parse_timestamp(self, ts_str: str) -> Optional[datetime]:
        """
        解析 ISO8601 UTC 時間字串為 timezone-aware datetime（UTC）。

        支援格式：
          "2026-07-14T10:00:00.123Z"
          "2026-07-14T10:00:00Z"
          "2026-07-14T10:00:00+00:00"

        Returns:
            datetime: UTC aware
            None: 無法解析
        """
        if not ts_str:
            return None
        try:
            ts = ts_str.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
```

---

## 6. log/features.py — FeatureExtractor

```python
# src/event_detection/log/features.py
#
# 職責：
#   接收 LogParser 回傳的 dict，抽取 RawFeatures。
#   只做「抽取」，不做數值化（Encoding 在 encoder.py）。
#
# 每個 function 的職責：
#   FeatureExtractor.extract_one()   主函式，從 dict 產生 RawFeatures

from src.event_detection.model.schema import RawFeatures


class FeatureExtractor:
    """
    從已解析的 Log dict 抽取原始特徵（RawFeatures）。

    特徵分類說明：

    ① 數值特徵（直接使用原始數值）：
       status_code      int    HTTP 回應碼，正常期集中在 200-204
       duration_ms      int    請求耗時，異常時此值顯著偏高
       memory_usage_pct float  記憶體使用率，OOM 前此值接近 100
       rate_limit_quota int    Rate Limit 每秒額度，S6 劇本觸發時此值不為 0

    ② 類別特徵（需 Label Encoding）：
       service_name     str    來源服務名稱，用 known_services 清單 encode
       error_type       str    錯誤類型，null → "unknown"，用 known_error_types encode

    ③ Level 特徵（固定 Encoding）：
       level            str    INFO=1, WARN=2, ERROR=3
                               用固定映射而非 Label Encoding，保留數值大小語意

    ④ Boolean 特徵（null 判斷 → 0/1）：
       has_source_ip          source_ip 不為 null（S1 暴力攻擊時此值=1）
       has_downstream_service downstream_service 不為 null（S2、S5 關鍵）
       has_external_service   external_service 不為 null（S4 關鍵）
       has_transaction_id     transaction_id 不為 null
       has_target_service     target_service 不為 null（S6 關鍵）

    ⑤ 衍生 Boolean 特徵（從數值計算）：
       is_error   level=="ERROR"                提高 ERROR 的統計權重
       is_warn    level=="WARN"
       is_5xx     status_code>=500              伺服器錯誤指標
       is_4xx     400<=status_code<500          用戶端錯誤指標
       is_401     status_code==401              登入失敗（S1 暴力攻擊特徵）
       is_429     status_code==429              Rate Limit（S6 特徵）
       is_oom     error_type=="OutOfMemoryError" OOM（S3 特徵）
    """

    def extract_one(self, entry: dict) -> RawFeatures:
        """
        從單一 Log dict 抽取 RawFeatures。

        Args:
            entry: LogParser.parse() 回傳的 dict（已保證 Schema 正確）

        Returns:
            RawFeatures dataclass

        Null 處理規則：
          - 數值欄位若為 None → fallback 到 0 或 0.0
          - 類別欄位若為 None → "unknown"
          - level 若為 None 或空字串 → "INFO"
          - Boolean 欄位：欄位值為 None → has_xxx = False
        """
        sc   = int(entry.get("status_code", 200))
        dur  = int(entry.get("duration_ms", 0))
        mem  = float(entry.get("memory_usage_pct") or 0.0)
        rq   = int(entry.get("rate_limit_quota") or 0)
        svc  = str(entry.get("service_name") or "unknown")
        et   = str(entry.get("error_type")   or "unknown")
        lvl  = str(entry.get("level")        or "INFO").upper()

        src_ip = entry.get("source_ip")
        ds     = entry.get("downstream_service")
        ext    = entry.get("external_service")
        txn    = entry.get("transaction_id")
        tgt    = entry.get("target_service")
        tid    = entry.get("trace_id")

        return RawFeatures(
            status_code      = sc,
            duration_ms      = dur,
            memory_usage_pct = mem,
            rate_limit_quota = rq,
            service_name     = svc,
            error_type       = et,
            level            = lvl,

            has_source_ip          = src_ip is not None,
            has_downstream_service = ds     is not None,
            has_external_service   = ext    is not None,
            has_transaction_id     = txn    is not None,
            has_target_service     = tgt    is not None,

            is_error = (lvl == "ERROR"),
            is_warn  = (lvl == "WARN"),
            is_5xx   = (sc >= 500),
            is_4xx   = (400 <= sc < 500),
            is_401   = (sc == 401),
            is_429   = (sc == 429),
            is_oom   = (et == "OutOfMemoryError"),

            raw_source_ip          = src_ip,
            raw_downstream_service = ds,
            raw_external_service   = ext,
            raw_trace_id           = tid,
            raw_transaction_id     = txn,
            raw_target_service     = tgt,
            raw_timestamp          = entry.get("timestamp"),
        )
```

---

## 7. log/encoder.py — FeatureEncoder

```python
# src/event_detection/log/encoder.py
#
# 職責：
#   接收 RawFeatures，將類別欄位數值化，
#   回傳 EncodedFeatureVector（可直接傳給 numpy / Isolation Forest）。
#
# 每個 function 的職責：
#   FeatureEncoder.__init__()   從 config 建立 Label Encoding 映射 dict
#   FeatureEncoder.encode()     RawFeatures → EncodedFeatureVector

from src.event_detection.model.schema import RawFeatures, EncodedFeatureVector


class FeatureEncoder:
    """
    將 RawFeatures 數值化為 EncodedFeatureVector（19 維）。

    Encoding 策略：

    Label Encoding（service_name / error_type）：
      以 config 的 known_xxx 清單的 list index + 1 作為數值。
      index 0 保留給「未知值或 null」（對應 "unknown" 或清單外的值）。

      範例（known_services = ["auth-service", "payment-service", ...]）：
        "auth-service"    → 1.0
        "payment-service" → 2.0
        "unknown"         → 0.0
        任何不在清單的值   → 0.0

    Level Encoding（固定映射，不用 config）：
      "INFO"  → 1.0
      "WARN"  → 2.0
      "ERROR" → 3.0
      保留大小語意（ERROR 最嚴重）。

    Boolean Encoding：
      True  → 1.0
      False → 0.0

    數值特徵（直接 float 轉型，不做標準化）：
      Isolation Forest 對 feature scale 不敏感，
      不需要 StandardScaler 或 MinMaxScaler。
    """

    LEVEL_MAP = {"INFO": 1.0, "WARN": 2.0, "ERROR": 3.0}

    def __init__(self, config: dict):
        """
        Args:
            config: event_detection.yml 的 feature_extraction 段落

        建立兩個 Label Encoding 映射 dict：
          _service_map:    service_name → float
          _error_type_map: error_type   → float
        """
        known_svcs  = config.get("known_services",    [])
        known_errs  = config.get("known_error_types", [])
        self._service_map    = {s: float(i + 1) for i, s in enumerate(known_svcs)}
        self._error_type_map = {e: float(i + 1) for i, e in enumerate(known_errs)}

    def encode(self, raw: RawFeatures) -> EncodedFeatureVector:
        """
        將 RawFeatures 轉換為 EncodedFeatureVector。

        Args:
            raw: FeatureExtractor.extract_one() 回傳的 RawFeatures

        Returns:
            EncodedFeatureVector，呼叫 .to_list() 得到 19 維 float list

        未知類別值的處理：
          任何不在映射 dict 中的值 → 0.0（unknown）
          訓練與推論時行為一致，不拋例外。
        """
        return EncodedFeatureVector(
            status_code            = float(raw.status_code),
            duration_ms            = float(raw.duration_ms),
            memory_usage_pct       = float(raw.memory_usage_pct),
            rate_limit_quota       = float(raw.rate_limit_quota),
            service_name_encoded   = self._service_map.get(raw.service_name, 0.0),
            error_type_encoded     = self._error_type_map.get(raw.error_type, 0.0),
            level_encoded          = self.LEVEL_MAP.get(raw.level, 1.0),
            has_source_ip          = 1.0 if raw.has_source_ip          else 0.0,
            has_downstream_service = 1.0 if raw.has_downstream_service else 0.0,
            has_external_service   = 1.0 if raw.has_external_service   else 0.0,
            has_transaction_id     = 1.0 if raw.has_transaction_id     else 0.0,
            has_target_service     = 1.0 if raw.has_target_service     else 0.0,
            is_error               = 1.0 if raw.is_error               else 0.0,
            is_warn                = 1.0 if raw.is_warn                else 0.0,
            is_5xx                 = 1.0 if raw.is_5xx                 else 0.0,
            is_4xx                 = 1.0 if raw.is_4xx                 else 0.0,
            is_401                 = 1.0 if raw.is_401                 else 0.0,
            is_429                 = 1.0 if raw.is_429                 else 0.0,
            is_oom                 = 1.0 if raw.is_oom                 else 0.0,
        )
```

---

## 8. model/trainer.py — ModelTrainer

```python
# src/event_detection/model/trainer.py
#
# 職責：
#   接收一批 EncodedFeatureVector（正常流量期間收集），
#   訓練 Isolation Forest，儲存模型到 models/log_isolation_forest.pkl。
#   此模組只在 scripts/train_log_model.py 中呼叫，不在推論 Pipeline 中執行。
#
# 每個 function 的職責：
#   ModelTrainer.__init__()   從 config 讀取超參數與儲存路徑
#   ModelTrainer.train()      訓練模型，印出分數分佈，儲存 pkl

import numpy as np
import joblib
from pathlib import Path
from sklearn.ensemble import IsolationForest

from src.event_detection.model.schema import EncodedFeatureVector


class ModelTrainer:
    """
    Isolation Forest 訓練器。

    訓練資料要求：
      - 必須是「純正常流量」的 Log，不能混入 Scenario 異常資料
      - 至少 100 筆，建議 500–2000 筆（取決於 Log Generator 運行時長）
      - 訓練腳本（train_log_model.py）需等 Log Generator 正常運行 10 分鐘後才執行

    超參數說明：
      contamination:
        Isolation Forest 假設訓練資料中有多少比例是異常。
        由於訓練資料是純正常流量，設為 0.05（5%）是保守估計。
        此值影響 decision_function 的 threshold，過高會導致正常資料被誤判。

      n_estimators:
        決策樹數量，200 棵在效能與準確度間取得平衡。
        設太少（<50）容易不穩定，設太多（>500）訓練時間增加但提升有限。

      random_state:
        固定為 42，確保每次訓練結果可重現。

      max_samples:
        "auto" 代表 min(256, n_samples)，適合中小型訓練集。
    """

    def __init__(self, config: dict):
        """
        Args:
            config: event_detection.yml 完整 dict
        """
        if_cfg             = config["isolation_forest"]
        self.contamination = if_cfg.get("contamination", 0.05)
        self.n_estimators  = if_cfg.get("n_estimators",  200)
        self.random_state  = if_cfg.get("random_state",  42)
        self.max_samples   = if_cfg.get("max_samples",   "auto")
        self.model_path    = Path(config["output"]["model_path"])

    def train(self, vectors: list) -> IsolationForest:
        """
        訓練 Isolation Forest 並儲存至 model_path。

        Args:
            vectors: list[EncodedFeatureVector]，純正常流量，至少 100 筆

        Returns:
            訓練好的 IsolationForest instance

        Raises:
            ValueError: vectors 數量 < 50 時

        訓練後會印出：
          - 樣本數
          - 訓練集的 decision_function 分數分佈（mean / min / max）
          - 模型儲存路徑
        """
        if len(vectors) < 50:
            raise ValueError(
                f"訓練資料不足（{len(vectors)} 筆）。"
                f"至少需要 50 筆，建議 500 筆以上。"
            )

        X = np.array([v.to_list() for v in vectors])

        model = IsolationForest(
            n_estimators  = self.n_estimators,
            contamination = self.contamination,
            max_samples   = self.max_samples,
            random_state  = self.random_state,
            n_jobs        = -1,
        )
        model.fit(X)

        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, self.model_path)

        scores = model.decision_function(X)
        print(f"[ModelTrainer] 訓練完成 | 樣本數：{len(X)}")
        print(f"  decision_function 分數分佈：")
        print(f"    mean={scores.mean():.4f}, min={scores.min():.4f}, max={scores.max():.4f}")
        print(f"  模型儲存至：{self.model_path}")
        return model
```

---

## 9. model/predictor.py — AnomalyPredictor

```python
# src/event_detection/model/predictor.py
#
# 職責：
#   載入已訓練的 Isolation Forest，
#   對 EncodedFeatureVector 進行推論，
#   回傳 anomaly_score 與 confidence。
#
# 每個 function 的職責：
#   AnomalyPredictor.__init__()         從 config 讀取 threshold 與路徑
#   AnomalyPredictor.load()             從 pkl 載入模型
#   AnomalyPredictor.predict_one()      單筆推論，回傳 PredictionResult
#   AnomalyPredictor.predict_batch()    批次推論（供驗收腳本）
#   AnomalyPredictor._score_to_confidence() 分數轉 confidence

import numpy as np
import joblib
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from sklearn.ensemble import IsolationForest

from src.event_detection.model.schema import EncodedFeatureVector


@dataclass
class PredictionResult:
    """
    單次推論結果。
    由 AnomalyPredictor.predict_one() 回傳，傳給 EventBuilder。
    """
    is_anomaly:    bool    # True = Isolation Forest 判定為異常且分數低於 threshold
    anomaly_score: float   # decision_function() 原始值，越負越異常，正常約 > 0
    confidence:    float   # 0.0–1.0，越高越確信是異常
    label:         int     # -1 = 異常，1 = 正常（predict() 原始回傳值）


class AnomalyPredictor:
    """
    Isolation Forest 推論器。

    decision_function() 回傳值說明：
      > 0.0           離決策邊界遠，偏正常
      ≈ 0.0           邊界附近，不確定
      < 0.0           偏異常
      < -0.1（預設值） 中等異常，confidence = medium
      < -0.3（預設值） 高確信異常，confidence = high

    is_anomaly 的判定條件（兩者同時成立）：
      1. predict() 回傳 -1
      2. decision_function() 分數 < score_threshold（預設 -0.05）
      第 2 個條件是額外保護，防止 predict() 邊界誤判。
    """

    def __init__(self, config: dict):
        """
        Args:
            config: event_detection.yml 完整 dict
        """
        self.model_path       = Path(config["output"]["model_path"])
        anomaly_cfg           = config["anomaly"]
        self.score_threshold  = anomaly_cfg.get("score_threshold",          -0.05)
        self.conf_high        = anomaly_cfg.get("confidence_high_threshold", -0.3)
        self.conf_medium      = anomaly_cfg.get("confidence_medium_threshold",-0.1)
        self._model: Optional[IsolationForest] = None

    def load(self) -> None:
        """
        從 model_path 載入已訓練的模型。

        Raises:
            FileNotFoundError: pkl 不存在，需先執行 train_log_model.py
        """
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"找不到模型：{self.model_path}\n"
                f"請先執行：python scripts/train_log_model.py"
            )
        self._model = joblib.load(self.model_path)
        print(f"[AnomalyPredictor] 模型載入：{self.model_path}")

    def predict_one(self, vector: EncodedFeatureVector) -> PredictionResult:
        """
        對單一 EncodedFeatureVector 進行推論。

        推論步驟：
          1. 呼叫 model.decision_function(X) → 取得 score
          2. 呼叫 model.predict(X)           → 取得 label（-1 或 1）
          3. is_anomaly = (label == -1) AND (score < score_threshold)
          4. _score_to_confidence(score)     → 取得 confidence

        Args:
            vector: FeatureEncoder.encode() 回傳的 EncodedFeatureVector

        Returns:
            PredictionResult

        Raises:
            RuntimeError: 未先呼叫 load()
        """
        if self._model is None:
            raise RuntimeError("請先呼叫 load()")

        X     = np.array([vector.to_list()])
        score = float(self._model.decision_function(X)[0])
        label = int(self._model.predict(X)[0])

        return PredictionResult(
            is_anomaly    = (label == -1 and score < self.score_threshold),
            anomaly_score = score,
            confidence    = self._score_to_confidence(score),
            label         = label,
        )

    def predict_batch(self, vectors: list) -> list:
        """
        批次推論（供 validate_log_detection.py 使用）。

        Args:
            vectors: list[EncodedFeatureVector]

        Returns:
            list[PredictionResult]，與輸入順序對應
        """
        if not vectors or self._model is None:
            return []
        X      = np.array([v.to_list() for v in vectors])
        scores = self._model.decision_function(X)
        labels = self._model.predict(X)
        return [
            PredictionResult(
                is_anomaly    = (int(l) == -1 and float(s) < self.score_threshold),
                anomaly_score = float(s),
                confidence    = self._score_to_confidence(float(s)),
                label         = int(l),
            )
            for s, l in zip(scores, labels)
        ]

    def _score_to_confidence(self, score: float) -> float:
        """
        將 decision_function 原始分數轉換為 confidence（0.0–1.0）。

        映射規則（分段線性）：
          score >= 0.0                 → 0.0（明顯正常）
          conf_medium <= score < 0.0  → 0.0–0.3（低確信）
          conf_high <= score < conf_medium → 0.3–0.8（中等確信）
          score < conf_high           → 0.8–1.0（高確信，夾在 -1.0）

        Args:
            score: decision_function() 回傳值

        Returns:
            float: 0.0–1.0，四捨五入至小數點後 3 位
        """
        if score >= 0.0:
            return 0.0
        if score >= self.conf_medium:              # -0.1 ~ 0.0
            t = score / self.conf_medium
            return round(0.3 * t, 3)
        if score >= self.conf_high:                # -0.3 ~ -0.1
            t = (score - self.conf_medium) / (self.conf_high - self.conf_medium)
            return round(0.3 + 0.5 * t, 3)
        clamped = max(score, -1.0)                 # < -0.3
        t = (clamped - self.conf_high) / (-1.0 - self.conf_high)
        return round(min(0.8 + 0.2 * t, 1.0), 3)
```

---

## 10. event/builder.py — EventBuilder

```python
# src/event_detection/event/builder.py
#
# 職責：
#   當 AnomalyPredictor 判定異常時，
#   根據 PredictionResult + WindowSummary，
#   組裝符合 PRD-002 Event Schema 的 Event dict。
#
# 每個 function 的職責：
#   EventBuilder.build()              主函式，回傳 Event dict 或 None
#   EventBuilder._infer_event_type()  根據 WindowSummary 推斷 event_type
#   EventBuilder._build_triggered_features()  組裝 triggered_features
#   EventBuilder._make_event_id()     產生唯一 event_id
#   EventBuilder._now_iso()           取得當前 UTC 時間字串

import uuid
from datetime import datetime, timezone
from typing import Optional

from src.event_detection.model.predictor import PredictionResult
from src.event_detection.model.schema    import WindowSummary


# event_type → severity 映射
SEVERITY_MAP = {
    "oom_crash_detected":           "CRITICAL",
    "external_dependency_failure":  "HIGH",
    "downstream_cascade_failure":   "CRITICAL",
    "brute_force_detected":         "CRITICAL",
    "rate_limit_storm":             "HIGH",
    "cross_service_failure":        "HIGH",
    "anomaly_detected":             "MEDIUM",
}


class EventBuilder:
    """
    組裝標準化 Event dict（PRD-002 第 5 章 Schema）。

    event_type 推斷邏輯（優先順序由高到低）：
      1. oom_crash_detected          top_error_types 含 OutOfMemoryError
      2. external_dependency_failure raw_log_sample 有 external_service 不為 null
      3. downstream_cascade_failure  affected_services_for_downstream >= 3 個不同服務
      4. brute_force_detected        top_source_ip_count >= 10
      5. rate_limit_storm            raw_log_sample 有 429 + target_service 不為 null
      6. cross_service_failure       cross_service_trace_ids 不為空
      7. anomaly_detected            以上皆不符合（Fallback）
    """

    def build(
        self,
        prediction: PredictionResult,
        summary: WindowSummary,
    ) -> Optional[dict]:
        """
        組裝 Event dict。

        Args:
            prediction: AnomalyPredictor.predict_one() 回傳的 PredictionResult
            summary:    WindowBuffer.compute_summary() 回傳的 WindowSummary

        Returns:
            dict: 符合 PRD-002 Event Schema 的 Event
            None: prediction.is_anomaly == False 時（正常情況）

        Event Schema（固定，不得修改欄位名稱）：
          event_id, detected_at, event_source, event_type,
          detection_method, severity, confidence, service_name,
          trace_id, source_ip, downstream_service, external_service,
          status, triggered_features, raw_log_sample
        """
        if not prediction.is_anomaly:
            return None

        event_type = self._infer_event_type(summary)
        severity   = SEVERITY_MAP.get(event_type, "MEDIUM")

        return {
            "event_id":           self._make_event_id(),
            "detected_at":        self._now_iso(),
            "event_source":       "log_event_detection",
            "event_type":         event_type,
            "detection_method":   "isolation_forest",
            "severity":           severity,
            "confidence":         prediction.confidence,
            "service_name":       self._pick_service(summary, event_type),
            "trace_id":           self._pick_trace_id(summary, event_type),
            "source_ip":          self._pick_source_ip(summary, event_type),
            "downstream_service": self._pick_downstream(summary, event_type),
            "external_service":   self._pick_external(summary, event_type),
            "status":             "OPEN",
            "triggered_features": self._build_triggered_features(
                summary, prediction, event_type
            ),
            "raw_log_sample": [
                {k: v for k, v in log.items() if not k.startswith("_")}
                for log in summary.raw_log_sample[:3]
            ],
        }

    def _infer_event_type(self, s: WindowSummary) -> str:
        """
        按優先順序判斷 event_type。
        只取第一個符合的，避免多重匹配。
        """
        if "OutOfMemoryError" in (s.top_error_types or []):
            return "oom_crash_detected"

        if any(log.get("external_service") for log in s.raw_log_sample):
            return "external_dependency_failure"

        if len(s.affected_services_for_downstream) >= 3:
            return "downstream_cascade_failure"

        if s.top_source_ip_count >= 10:
            return "brute_force_detected"

        if any(
            log.get("status_code") == 429 and log.get("target_service")
            for log in s.raw_log_sample
        ):
            return "rate_limit_storm"

        if s.cross_service_trace_ids:
            return "cross_service_failure"

        return "anomaly_detected"

    def _pick_service(self, s: WindowSummary, event_type: str) -> str:
        if event_type == "downstream_cascade_failure":
            return "multiple"
        return s.unique_services[0] if s.unique_services else "unknown"

    def _pick_trace_id(self, s: WindowSummary, event_type: str) -> Optional[str]:
        if event_type == "cross_service_failure" and s.cross_service_trace_ids:
            return s.cross_service_trace_ids[0]
        return None

    def _pick_source_ip(self, s: WindowSummary, event_type: str) -> Optional[str]:
        return s.top_source_ip if event_type == "brute_force_detected" else None

    def _pick_downstream(self, s: WindowSummary, event_type: str) -> Optional[str]:
        if event_type in ("downstream_cascade_failure", "cross_service_failure"):
            return s.top_downstream
        return None

    def _pick_external(self, s: WindowSummary, event_type: str) -> Optional[str]:
        if event_type == "external_dependency_failure":
            for log in s.raw_log_sample:
                ext = log.get("external_service")
                if ext:
                    return ext
        return None

    def _build_triggered_features(
        self,
        s: WindowSummary,
        pred: PredictionResult,
        event_type: str,
    ) -> dict:
        """
        根據 event_type 組裝 triggered_features。
        供 Alert Correlation Engine 進行事件關聯分析使用。
        """
        base = {
            "anomaly_score":    pred.anomaly_score,
            "window_log_count": s.total_log_count,
            "error_count":      s.error_count,
            "mean_duration_ms": s.mean_duration_ms,
            "max_duration_ms":  s.max_duration_ms,
        }
        if event_type == "brute_force_detected":
            base.update({"attacker_ip": s.top_source_ip,
                          "failed_attempt_count": s.top_source_ip_count})
        elif event_type == "downstream_cascade_failure":
            base.update({"common_downstream": s.top_downstream,
                          "affected_service_count": len(s.affected_services_for_downstream),
                          "affected_services": s.affected_services_for_downstream})
        elif event_type == "cross_service_failure":
            base.update({"trace_id": s.cross_service_trace_ids[0] if s.cross_service_trace_ids else None,
                          "affected_services": s.unique_services})
        elif event_type == "rate_limit_storm":
            for log in s.raw_log_sample:
                if log.get("target_service"):
                    base.update({"target_service": log["target_service"],
                                  "rate_limit_quota": log.get("rate_limit_quota")})
                    break
        elif event_type == "oom_crash_detected":
            base["max_memory_pct"] = s.max_memory_pct
        return base

    @staticmethod
    def _make_event_id() -> str:
        ts  = int(datetime.now(timezone.utc).timestamp() * 1000)
        rnd = uuid.uuid4().hex[:4]
        return f"EVT-{ts}-{rnd}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
```

---

## 11. store/event_store.py — EventStore

```python
# src/event_detection/store/event_store.py
#
# 職責：
#   將 Event dict 以 JSONL 格式寫入 events/event_store.jsonl。
#   確保目錄存在，提供讀取所有 Event 的介面（供驗收腳本）。
#
# 每個 function 的職責：
#   EventStore.__init__()  確認目錄存在，設定路徑
#   EventStore.write()     單筆 Event 寫入（append 模式）
#   EventStore.read_all()  讀取全部 Event（供 validate 腳本）

import json
from pathlib import Path


class EventStore:
    """
    Event 持久化寫入器。

    輸出格式：JSONL（每行一筆完整 JSON，UTF-8，無 BOM）
    寫入模式：append（不覆蓋既有紀錄）
    """

    def __init__(self, store_path: str = "events/event_store.jsonl"):
        self.path = Path(store_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict) -> None:
        """
        Append 一筆 Event 至 JSONL 檔案。

        Args:
            event: 符合 PRD-002 Schema 的 Event dict
        """
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_all(self) -> list:
        """
        讀取所有已寫入的 Event。
        JSON 解析失敗的行靜默跳過。

        Returns:
            list[dict]：所有有效 Event
        """
        if not self.path.exists():
            return []
        results = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return results
```

---

## 12. runner.py — 主執行入口

```python
# src/event_detection/runner.py
#
# 職責：
#   整合所有模組，執行完整的 Log Event Detection Pipeline。
#   維護 WindowBuffer（滑動視窗），
#   每個 poll 週期計算 WindowSummary 並執行 Isolation Forest 推論。
#
# 每個 class / function 的職責：
#   WindowBuffer.add()             加入 Log + 清理過期 Log
#   WindowBuffer.has_enough()      判斷視窗資料是否足夠推論
#   WindowBuffer.to_list()         回傳當前視窗所有 Log
#   WindowBuffer.compute_summary() 計算 WindowSummary（供 EventBuilder）
#   LogEventDetectionRunner.__init__()  初始化所有模組
#   LogEventDetectionRunner.start()     啟動推論迴圈（blocking）

import time
import yaml
import logging
from collections import deque, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.event_detection.log.reader    import LogReader
from src.event_detection.log.parser    import LogParser
from src.event_detection.log.features  import FeatureExtractor
from src.event_detection.log.encoder   import FeatureEncoder
from src.event_detection.model.predictor import AnomalyPredictor
from src.event_detection.model.schema  import WindowSummary
from src.event_detection.event.builder import EventBuilder
from src.event_detection.store.event_store import EventStore

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("Runner")


def load_config(path: str = "configs/event_detection.yml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class WindowBuffer:
    """
    滑動視窗緩衝區。

    儲存過去 window_seconds 秒的已解析 Log dict。
    同時提供 compute_summary() 計算視窗的彙總統計。
    """

    def __init__(self, window_seconds: int = 60, min_log_count: int = 5):
        self.window_seconds = window_seconds
        self.min_log_count  = min_log_count
        self._buf: deque    = deque()

    def add(self, log_entry: dict) -> None:
        """加入一筆 Log 並清除超過視窗的舊資料。"""
        self._buf.append(log_entry)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.window_seconds)
        while self._buf and self._buf[0].get("_parsed_timestamp", cutoff) < cutoff:
            self._buf.popleft()

    def has_enough(self) -> bool:
        """視窗內 Log 數量 >= min_log_count 才回傳 True。"""
        return len(self._buf) >= self.min_log_count

    def to_list(self) -> list:
        return list(self._buf)

    def compute_summary(self) -> WindowSummary:
        """
        計算當前視窗的彙總統計。
        此結果傳給 EventBuilder 組裝 triggered_features 與 raw_log_sample。

        計算項目：
          - error_count / warn_count
          - unique_services：視窗內出現的所有 service_name
          - top_error_types：出現最多次的 error_type（前 5 名）
          - max/mean duration_ms
          - max_memory_pct
          - top_source_ip / top_source_ip_count：出現最多次的 source_ip
          - top_downstream / affected_services_for_downstream：
              最多服務指向的 downstream_service，及指向它的不同服務清單
          - cross_service_trace_ids：
              同一 trace_id 跨越 >= 2 個 service_name 的 trace_id 清單
          - raw_log_sample：前 3 筆 ERROR Log（無 ERROR 則取前 3 筆任意 Log）
        """
        logs = self.to_list()
        if not logs:
            return WindowSummary()

        error_logs  = [l for l in logs if l.get("level") == "ERROR"]
        warn_logs   = [l for l in logs if l.get("level") == "WARN"]
        durations   = [l.get("duration_ms", 0) for l in logs]
        error_types = [l["error_type"] for l in logs if l.get("error_type")]
        memory_vals = [l["memory_usage_pct"] for l in logs if l.get("memory_usage_pct")]

        ip_ctr = Counter(l["source_ip"] for l in logs if l.get("source_ip"))
        top_ip, top_ip_cnt = ip_ctr.most_common(1)[0] if ip_ctr else (None, 0)

        ds_errs = [l for l in error_logs if l.get("downstream_service")]
        ds_ctr  = Counter(l["downstream_service"] for l in ds_errs)
        top_ds, top_ds_cnt = ds_ctr.most_common(1)[0] if ds_ctr else (None, 0)

        affected_for_ds = list(set(
            l["service_name"] for l in ds_errs
            if l.get("downstream_service") == top_ds
        )) if top_ds else []

        trace_svc: dict = {}
        for l in logs:
            tid = l.get("trace_id")
            svc = l.get("service_name")
            if tid and svc:
                trace_svc.setdefault(tid, set()).add(svc)
        cross_tids = [tid for tid, svcs in trace_svc.items() if len(svcs) >= 2]

        sample = error_logs[:3] if error_logs else logs[:3]

        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        return WindowSummary(
            window_start                    = logs[0].get("timestamp", ""),
            window_end                      = ts_now,
            total_log_count                 = len(logs),
            error_count                     = len(error_logs),
            warn_count                      = len(warn_logs),
            unique_services                 = list(set(l.get("service_name","") for l in logs)),
            unique_trace_ids                = list(set(l.get("trace_id","") for l in logs if l.get("trace_id"))),
            top_error_types                 = [t for t,_ in Counter(error_types).most_common(5)],
            max_duration_ms                 = float(max(durations)) if durations else 0.0,
            mean_duration_ms                = float(sum(durations)/len(durations)) if durations else 0.0,
            max_memory_pct                  = float(max(memory_vals)) if memory_vals else 0.0,
            top_source_ip                   = top_ip,
            top_source_ip_count             = top_ip_cnt,
            top_downstream                  = top_ds,
            top_downstream_count            = top_ds_cnt,
            affected_services_for_downstream= affected_for_ds,
            cross_service_trace_ids         = cross_tids,
            raw_log_sample                  = [{k:v for k,v in s.items() if not k.startswith("_")} for s in sample],
        )


class LogEventDetectionRunner:
    """
    Log Event Detection 主執行器。

    冷卻期機制：
      _last_fired[event_type] 記錄上次觸發時間。
      同 event_type 在 cooldown_seconds 內不重複建立 Event。
    """

    def __init__(self, config_path: str = "configs/event_detection.yml"):
        cfg            = load_config(config_path)
        self.config    = cfg
        self.reader    = LogReader(cfg["log_reader"]["log_file_path"],
                                    cfg["log_reader"]["poll_interval_seconds"])
        self.parser    = LogParser()
        self.extractor = FeatureExtractor()
        self.encoder   = FeatureEncoder(cfg["feature_extraction"])
        self.predictor = AnomalyPredictor(cfg)
        self.window    = WindowBuffer(cfg["window"]["window_seconds"],
                                       cfg["window"]["min_log_count"])
        self.builder   = EventBuilder()
        self.store     = EventStore(cfg["output"]["event_store_path"])
        self.cooldown  = cfg["event"]["cooldown_seconds"]
        self._last_fired: dict = {}

    def start(self) -> None:
        """
        啟動推論迴圈（blocking，Ctrl+C 停止）。

        每收到一行新 Log：
          1. parse → 解析失敗則跳過
          2. extract + encode → 取得 EncodedFeatureVector
          3. 加入 WindowBuffer
          4. 視窗不足 min_log_count → 跳過推論
          5. predictor.predict_one() → 取得 PredictionResult
          6. is_anomaly=False → 跳過
          7. compute_summary() → 取得 WindowSummary
          8. builder.build() → 取得 Event dict
          9. 冷卻期檢查 → 在冷卻期內則跳過
          10. store.write() → 寫入 event_store.jsonl
        """
        logger.info("Log Event Detection 啟動")
        self.predictor.load()

        for raw_line in self.reader.tail():
            entry = self.parser.parse(raw_line)
            if entry is None:
                continue

            raw  = self.extractor.extract_one(entry)
            enc  = self.encoder.encode(raw)
            self.window.add(entry)

            if not self.window.has_enough():
                continue

            result = self.predictor.predict_one(enc)
            if not result.is_anomaly:
                continue

            summary = self.window.compute_summary()
            event   = self.builder.build(result, summary)
            if event is None:
                continue

            et  = event["event_type"]
            now = datetime.now(timezone.utc)
            if et in self._last_fired:
                elapsed = (now - self._last_fired[et]).total_seconds()
                if elapsed < self.cooldown:
                    continue

            self.store.write(event)
            self._last_fired[et] = now
            logger.warning(
                f"✅ EVENT | {et} | severity={event['severity']} "
                f"| confidence={event['confidence']} | id={event['event_id']}"
            )
```

---

## 13. scripts/train_log_model.py

```python
# scripts/train_log_model.py
#
# 使用說明：
#   1. 執行 Log Generator 以正常模式跑 10 分鐘（不觸發任何 Scenario）
#   2. 執行：python scripts/train_log_model.py
#   3. 確認 models/log_isolation_forest.pkl 產生

import sys
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.event_detection.log.reader    import LogReader
from src.event_detection.log.parser    import LogParser
from src.event_detection.log.features  import FeatureExtractor
from src.event_detection.log.encoder   import FeatureEncoder
from src.event_detection.model.trainer import ModelTrainer


def main():
    with open("configs/event_detection.yml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    print("請確認：Log Generator 已以正常模式執行 10+ 分鐘")
    input("按 Enter 開始訓練...")

    reader    = LogReader(config["log_reader"]["log_file_path"])
    parser    = LogParser()
    extractor = FeatureExtractor()
    encoder   = FeatureEncoder(config["feature_extraction"])
    trainer   = ModelTrainer(config)

    all_lines = reader.read_all()
    print(f"讀取 {len(all_lines)} 行 Log...")

    vectors, errors = [], 0
    for line in all_lines:
        entry = parser.parse(line)
        if entry is None:
            errors += 1
            continue
        vectors.append(encoder.encode(extractor.extract_one(entry)))

    print(f"有效特徵向量：{len(vectors)} 筆，解析失敗：{errors} 筆")
    trainer.train(vectors)
    print("訓練完成。執行推論：python -m src.event_detection.runner")


if __name__ == "__main__":
    main()
```

---

## 14. Validation — 六大 Scenario 驗證

### 14.1 驗證說明

六個 Scenario 是**驗證資料集**，用來確認 Isolation Forest 訓練完成後能正確偵測各類異常。

驗證流程：
1. 訓練完成（`models/log_isolation_forest.pkl` 已存在）
2. 啟動 Runner（Terminal 1）
3. 依序觸發各 Scenario（Terminal 2）
4. 確認 `events/event_store.jsonl` 出現對應 Event

| Scenario | 偵測目標異常特徵 | 預期 event_type |
|---|---|---|
| S1 密碼爆破 | 大量 is_401=1.0，同 source_ip | `brute_force_detected` |
| S2 DB 慢查詢 | duration_ms 極高，跨 service 同 trace_id | `cross_service_failure` |
| S3 OOM | is_oom=1.0，memory_usage_pct 接近 100 | `oom_crash_detected` |
| S4 外部 API 斷線 | has_external_service=1.0，is_5xx=1.0 | `external_dependency_failure` |
| S5 DB 瞬斷 | has_downstream_service=1.0，多服務同 downstream | `downstream_cascade_failure` |
| S6 Rate Limit | is_429=1.0，has_target_service=1.0 | `rate_limit_storm` |

### 14.2 驗收腳本

```python
# scripts/validate_log_detection.py

import json, time, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

STORE = Path("events/event_store.jsonl")

REQUIRED_SCHEMA_FIELDS = [
    "event_id", "detected_at", "event_source", "event_type",
    "detection_method", "severity", "confidence", "service_name",
    "trace_id", "source_ip", "downstream_service", "external_service",
    "status", "triggered_features", "raw_log_sample",
]

SCENARIO_EXPECTED = {
    "S1": "brute_force_detected",
    "S2": "cross_service_failure",
    "S3": "oom_crash_detected",
    "S4": "external_dependency_failure",
    "S5": "downstream_cascade_failure",
    "S6": "rate_limit_storm",
}


def read_events():
    if not STORE.exists():
        return []
    result = []
    for line in STORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return result


def wait_for_new_event(expected_type: str, known_ids: set, timeout=120) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        for e in read_events():
            if e.get("event_id") not in known_ids and e.get("event_type") == expected_type:
                return e
    return None


def check_schema(event: dict) -> list:
    return [f for f in REQUIRED_SCHEMA_FIELDS if f not in event]


def main():
    print("確認 Runner 已在另一個 Terminal 啟動後按 Enter...")
    input()

    all_pass = True
    for scenario, expected in SCENARIO_EXPECTED.items():
        print(f"\n--- {scenario}: 預期 {expected} ---")
        print(f"請觸發 {scenario}，然後按 Enter...")
        input()

        known_ids = {e["event_id"] for e in read_events()}
        print(f"等待 Event（最多 120 秒）...")
        event = wait_for_new_event(expected, known_ids, timeout=120)

        if not event:
            print(f"❌ FAIL：未在 120 秒內偵測到 {expected}")
            all_pass = False
            continue

        missing = check_schema(event)
        if missing:
            print(f"❌ FAIL：Schema 缺少欄位 {missing}")
            all_pass = False
            continue

        if event.get("event_source") != "log_event_detection":
            print(f"❌ FAIL：event_source 錯誤")
            all_pass = False
            continue

        if event.get("detection_method") != "isolation_forest":
            print(f"❌ FAIL：detection_method 必須為 isolation_forest")
            all_pass = False
            continue

        print(f"✅ PASS | severity={event['severity']} | confidence={event['confidence']}")
      