# AIOps 智慧維運平台
## 產品需求文件（PRD）v3.2

> **文件狀態**：執行中
> **最後更新**：2026-08-12
> **適用對象**：東吳大學資管系專題四人組
> **對應文件**：SDD v0.1、ADR-001
| Version | Date | Change |
|---|---|---|
| 3.1 | 2026-07-26 | 釐清 Metrics Threshold 與 Isolation Forest 雙軌分工；SPEC-003 v1.0 限定 QPS；新增未知 QPS Event 說明；DB Pool 定義為僅觀測與未來擴充。 |
| 3.2 | 2026-08-12 | Post-SPEC-005 Cross-Document Governance Batch 2B：依 PRD-002 v1.3 與 SPEC-001～004 正式契約同步 S1、S3、S6、模組二驗收與 G3 里程碑；補充 SPEC-005 v1.2 非規範性驗證證據及相關文件。整體平台狀態維持執行中。 |

---

## 1. 專案定位

### 1.1 一句話定義

建立一套 AIOps Prototype，能在金融系統發生異常時，自動完成「異常偵測 → 告警收斂 → Incident 管理 → AI 根因分析 → 視覺化呈現 → Email 通知」的完整閉環流程，大幅降低 MTTR。

### 1.2 目標用途

- 資服盃產學合作組參賽（截止：2026/11/07）
- 系上專題發表（預計：2026/11 中旬）
- 取得富邦金控驗證同意書（企業背書）

### 1.3 Demo 形式

影片錄製（非 Live Demo），展示六大劇本的完整自動化閉環流程。

---

## 2. 核心架構

### 2.1 Incident-driven Architecture

```
Logs ─────────────────┐
                      ▼
               Log Event Detection
                      │
                      ▼
               Event Queue ◀──────── Metrics Event Detection
                      │                        ▲
                      ▼                        │
           Alert Correlation Engine    Metrics Collection
                      │
                      ▼
             Incident Manager
                      │
               ┌──────┴──────┐
               ▼             ▼
          RAG 知識庫     LLM 推論
               └──────┬──────┘
                      ▼
                  RCA Report
                      │
               ┌──────┴──────┐
               ▼             ▼
           Dashboard    Email Alert
```

### 2.2 架構決策依據

詳見 `docs/ADR-001.md`。

### 2.3 資安邊界

> ⚠️ **重要**：以下規範為資安紅線，實作時嚴格遵守，不得繞過。

| 資安項目 | 規範 |
|---|---|
| API Key 管理 | 一律存放於 `.env`，禁止 hardcode 進程式碼或 commit 進 Git |
| `.gitignore` | `.env`、`*.pkl`（模型檔）、`vector_store/`（向量庫）必須列入 |
| Log 去識別化 | Mock Data 中不得出現真實個資格式（如身分證字號、信用卡號） |
| LLM 傳輸 | 送給 Gemini API 的內容僅限模擬資料，禁止夾帶任何敏感字串 |
| ChromaDB | 本地儲存，不對外開放 port |
| Dashboard | 本地 localhost，不部署至公網 |

---

## 3. 六大模擬劇本

這六個劇本是整個系統的核心展示內容，所有模組的設計都要能支援這些場景。

### 劇本一：單點爆破與帳號鎖定（垂直收斂）

| 項目 | 說明 |
|---|---|
| 場景描述 | 駭客針對單一帳號暴力嘗試密碼，觸發系統保護機制 |
| 正式偵測契約 | 同一 `source_ip` 在 60 秒內出現 ≥ 10 筆 `status_code=401`，分類為 `brute_force_detected`；≥ 10 是 Detector classification threshold |
| Approved Demo／E2E Input | 同一 `source_ip` 在 60 秒內 exactly 50 筆 `status_code=401`；exactly 50 是核准的驗證輸入，不是 Detector 門檻，且不包含額外 account-lock Log |
| 收斂邏輯 | 相同 IP + 極短時間窗 → 壓縮為 1 個安全威脅 Incident |
| 展示價值 | AI 能識別「高頻同源」特徵；產品層級上可支援後續帳號保護機制，但 account-lock Log 不是本劇本的正式 Event Detection input |
| 涉及欄位 | `source_ip`、`user_id`、`status_code`、`timestamp` |

### 劇本二：資料庫卡頓引發 API 雪崩（跨服務收斂）⭐ 最經典

| 項目 | 說明 |
|---|---|
| 場景描述 | DB 慢查詢 → AP 層卡死 → Gateway 回傳 504 給用戶 |
| 產生資料 | 帶有**相同 trace_id** 的三層錯誤 Log（DB / AP / Gateway） |
| 收斂邏輯 | 透過 `trace_id` 關聯三筆跨服務 Log → 1 個 Incident |
| 展示價值 | 精準定位「Gateway 504 的根因是底層 DB 慢查詢」 |
| 涉及欄位 | `trace_id`、`service_name`、`downstream_service`、`duration_ms` |

### 劇本三：記憶體耗盡（OOM）導致服務崩潰（資源與應用收斂）

| 項目 | 說明 |
|---|---|
| 場景描述 | AP 記憶體滿載 → OOM 崩潰；前端出現 502 Bad Gateway 是可能的 downstream／user-visible symptom |
| 正式偵測契約 | Log 中的 `OOM`／`OutOfMemoryError` 證據產生 `oom_crash_detected`；`system_memory_usage_pct >= 90%` 產生 `high_memory_detected` |
| Validation Input／Evidence | SPEC-005 Demo 使用 `memory=95%` 作為驗證輸入；95 不是永久門檻。502 不是 required detector trigger、required generated Log 或 required acceptance condition |
| 收斂邏輯 | 基礎設施警告 + 應用崩潰 → 1 個 Incident |
| 展示價值 | AI 能看穿 502 的本質是記憶體洩漏，而非網路問題 |
| 涉及欄位 | `memory_usage_pct`（Metrics）、`error_type`、`status_code` |

### 劇本四：第三方銀行 API 斷線（外部依賴收斂）

| 項目 | 說明 |
|---|---|
| 場景描述 | 外部銀行閘道 API 無回應 → 內部交易失敗（500） |
| 產生資料 | 外部連線逾時（30s） + 內部匯款交易失敗 Log |
| 收斂邏輯 | `external_service` 欄位標記為外部依賴 → 獨立 Incident 分類 |
| 展示價值 | 金融業高優先場景：立刻釐清「不是我們的錯，是外部服務掛了」 |
| 涉及欄位 | `external_service`、`timeout_ms`、`transaction_id` |

### 劇本五：共用資料庫網路瞬斷（微服務連鎖陣亡風暴）

| 項目 | 說明 |
|---|---|
| 場景描述 | 核心 DB 網路斷線 → 所有微服務同時噴出連線拒絕 → Gateway 全面 503 |
| 產生資料 | 短時間內數十筆、帶有**不同 trace_id**、**不同 service_name** 的 Log |
| 收斂邏輯 | 共同的 `downstream_service=core-db` → 收斂為 1 個 Incident |
| 展示價值 | 將「告警風暴」完美收斂，一眼看出核心資料庫是單點故障源 |
| 涉及欄位 | `downstream_service`、`error_code`（connection refused）、多個 `service_name` |

### 劇本六：API 額度打爆（429 Rate Limit 告警風暴）

| 項目 | 說明 |
|---|---|
| 場景描述 | 同一 `target_service` 發生 HTTP 429 storm，並伴隨 QPS 異常 |
| Log Detector 契約 | 同一 `target_service` 在 60 秒內出現 ≥ 20 筆 `status_code=429`，分類為 `rate_limit_storm`；≥ 20 是 Detector classification threshold |
| Approved Demo／E2E Input | 同一 `target_service` 在 60 秒內 exactly 55 筆 `status_code=429`；exactly 55 是核准的驗證輸入，不是 Detector 門檻 |
| Metrics IForest 契約 | Isolation Forest 判定 QPS Window 異常，且 `current_qps / baseline_mean >= configured request_spike_ratio`，分類為 `request_spike_detected`；ratio 維持 config-driven，3.0 與 SPEC-005 的 4x scenario generation value 均不是不可變 algorithm requirement |
| Event Detection 預期證據 | `rate_limit_storm` + `request_spike_detected` |
| 收斂邏輯 | 時間窗冷卻期機制 → 靜默折疊，只觸發 1 次 LLM 分析 |
| 展示價值 | 展示「高頻同質性垃圾告警的靜默與折疊」，保護 LLM API 成本 |
| 涉及欄位 | `status_code=429`、`target_service`、`rate_limit_quota` |

---

## 4. 功能需求（對應 SDD G1–G7）

### G1 資料模擬層

| 項目 | 說明 |
|---|---|
| Log 模擬器 | 產生 JSON 格式 AP Log，支援六大劇本的異常注入 |
| Metrics 模擬器 | 產生 p95 Latency、Error Rate、Memory Usage、DB Pool 等指標 |
| Trace ID 機制 | 跨服務 Log 需帶有相同 `trace_id`（劇本二、五使用） |
| 外部服務標記 | `external_service` 欄位標記外部依賴（劇本四使用） |
| 劇本觸發介面 | 提供簡單的觸發方式（命令列參數或 API）可切換不同劇本 |

> `db_pool_active_connections` 雖由 Metrics Generator 產生，
> 並可由 Prometheus 收集及於 Grafana 顯示，
> 但本階段僅作為觀測與未來擴充指標，
> 不納入 Metrics Threshold Detection 或
> Metrics Isolation Forest Detection v1.1 的正式 Event Detection 範圍。

**Log Schema 必要欄位：**

```json
{
  "timestamp":          "2026-07-11T10:00:00.000Z",
  "level":              "ERROR",
  "service_name":       "payment-service",
  "trace_id":           "abc123def456",
  "status_code":        500,
  "duration_ms":        3500,
  "error_type":         "ConnectionTimeout",
  "error_message":      "DB connection refused",
  "source_ip":          "192.168.1.100",
  "user_id":            "user_mock_001",
  "downstream_service": "core-db",
  "external_service":   null,
  "transaction_id":     "txn_mock_789",
  "memory_usage_pct":   null,
  "target_service":     null,
  "rate_limit_quota":   null
}
```

### G2 觀測層

| 項目 | 說明 |
|---|---|
| Log 收集 | Promtail → Grafana Loki |
| Metrics 收集 | Python Exporter → Prometheus（port 8000，15 秒抓取） |
| 視覺化 | Grafana Dashboard（各劇本指標折線圖） |
| 部署方式 | Docker Compose 一鍵啟動 |

### G3 異常偵測層

| 項目 | 說明 |
|---|---|
| Log 異常偵測 | 以 Isolation Forest 判斷 Log Window 是否異常，再由 Rule Classifier 分類已知情境；無法分類時輸出 `general_log_anomaly`。 |
| Metrics 雙軌原則 | Metrics Threshold Detection 與 Metrics Isolation Forest Detection 為互補且彼此獨立的雙軌機制；任一 Pipeline 判定異常，即可各自產生標準化 Event。 |
| Metrics Threshold Detection | 對 `api_p95_latency_ms` 與 `system_memory_usage_pct` 執行靜態門檻判斷。 |
| Metrics Isolation Forest Detection | SPEC-003 v1.1 對 `api_requests_per_sec` 建立 QPS 動態基準，輸出 `request_spike_detected` 或 `general_metrics_anomaly`。 |
| DB Pool | `db_pool_active_connections` 本階段僅收集與視覺化，不納入正式 Event Detection。 |
| 偵測週期 | Event Runner 整合後，由各 Detection Pipeline 依設定週期執行。 |
| 輸出格式 | 所有 Detection Pipeline 輸出符合 PRD-002 第 5 章的 Event。 |
| G3 里程碑 | Event Detection implementation／validation completed：SPEC-001～004 Implemented；SPEC-005 v1.2 validation completed，結果為 PASS WITH KNOWN LIMITATIONS。此里程碑完成不代表 AIOps Platform 整體完成。 |
> Threshold 與 Isolation Forest 不互相取代或排斥。
> 同一事故可能同時產生不同來源的 Metrics Event，
> 後續再由 Event Runner 與 Alert Correlation Engine 進行整理與收斂。

### G4 告警收斂層

| 項目 | 說明 |
|---|---|
| 收斂規則一 | 相同 `source_ip` + 時間窗 60s 內 → 垂直收斂（劇本一） |
| 收斂規則二 | 相同 `trace_id` 跨服務 → 橫向收斂（劇本二） |
| 收斂規則三 | 相同 `downstream_service` 多來源 → 根因收斂（劇本五） |
| 收斂規則四 | 相同 `status_code` + 時間窗冷卻 → 靜默折疊（劇本六） |
| 冷卻期機制 | 首筆觸發 LLM 分析後，60 秒內同類 Event 不重複觸發 |
| 輸出格式 | 產生 Incident，寫入 Incident Manager |

### G5 Incident 管理層

| 項目 | 說明 |
|---|---|
| Incident 建立 | 由 Alert Correlation 輸出觸發 |
| 狀態管理 | Open → Analyzing → Resolved |
| 嚴重度 | Critical / High / Medium / Low（由收斂規則決定） |
| 持久化 | 寫入 `incidents/incident_store.jsonl` |

**Incident Schema：**

```json
{
  "incident_id":       "INC-1720000000-abc123",
  "created_at":        "2026-07-11T10:00:00Z",
  "status":            "Open",
  "severity":          "critical",
  "scenario":          "scenario_5_db_network_failure",
  "affected_services": ["payment-service", "member-service", "order-service"],
  "root_service":      "core-db",
  "correlation_rule":  "downstream_service",
  "event_count":       47,
  "events":            [],
  "rca_result":        null
}
```

### G6 RAG + LLM 分析層

| 項目 | 說明 |
|---|---|
| 知識庫來源 | 自建六大劇本對應的 SOP 文件（各 1–2 頁 Markdown） |
| 向量化 | Google text-embedding-004 |
| 向量資料庫 | ChromaDB（本地，不對外開放） |
| LLM | Gemini 2.5 Flash API |
| 輸入 | Incident + 相關 Log 片段 + RAG 召回 SOP |
| 輸出 | 結構化 RCA JSON |
| 保護機制 | 冷卻期限制呼叫頻率，防止 Free Tier 429 錯誤 |

**RCA 輸出 Schema：**

```json
{
  "report_id":              "RCA-INC-1720000000",
  "incident_id":            "INC-1720000000-abc123",
  "generated_at":           "2026-07-11T10:00:30Z",
  "incident_summary":       "核心資料庫網路斷線導致所有微服務連線拒絕",
  "severity_assessment":    "critical",
  "estimated_mttr_minutes": 20,
  "root_causes": [
    {
      "rank":          1,
      "confidence":    "high",
      "hypothesis":    "core-db 網路層發生瞬斷",
      "evidence":      "47 筆來自不同服務的 connection refused 均指向 downstream_service=core-db",
      "sop_reference": "SOP-05"
    }
  ],
  "remediation_steps": [
    {
      "order":           1,
      "priority":        "immediate",
      "action":          "確認 core-db 網路連線狀態與 VPC 路由表",
      "expected_effect": "確認是否為網路層故障或資料庫本身問題"
    }
  ],
  "prevention_measures": [
    "為 core-db 建立多可用區備援",
    "設定 DB 連線池重試機制與 Circuit Breaker"
  ]
}
```

### G7 Dashboard 展示層

| 項目 | 說明 |
|---|---|
| 框架 | FastAPI + Jinja2 |
| 樣式 | Tailwind CSS（Apple 極簡風格） |
| 頁面一 | Incident 列表（嚴重度、劇本類型、狀態、時間） |
| 頁面二 | 單一 Incident 詳情（根因分析、修復步驟、相關 Log、Metrics 圖表） |
| 頁面三 | Grafana 嵌入（Logs + Metrics 即時視覺化） |
| 更新機制 | 每 10 秒 polling 自動刷新 |

### G8 Email 告警通知

| 項目 | 說明 |
|---|---|
| 觸發條件 | Incident severity = Critical 時自動發送 |
| 發送方式 | Gmail SMTP（App Password） |
| 收件人 | 設定於 `.env`，不 hardcode |
| 郵件內容 | Incident ID、嚴重度、摘要、Dashboard 連結 |
| 資安規範 | Gmail 帳號與 App Password 存於 `.env`，禁止 commit |

---

## 5. 非功能需求

| 項目 | 規範 |
|---|---|
| 零額外費用 | 所有工具使用免費方案或開源工具 |
| 個人筆電可跑 | 無 GPU 需求，Docker Compose 本地部署 |
| Mock Fallback | Gemini API 失敗時顯示預設 RCA，Dashboard 不中斷 |
| 彈性架構 | 各模組以標準 JSON 介面溝通，未來可替換任一元件 |
| API Key 安全 | 全部存於 `.env`，`.gitignore` 列入 |
| LLM 輸入清潔 | 送出前過濾敏感字串 Pattern（身分證、信用卡等正規表示式） |

---

## 6. 系統限制（Out of Scope）

| 項目 | 原因 |
|---|---|
| 真實企業資料 | 未取得授權，使用 Mock Data |
| 正式生產環境部署 | PoC 階段不需要 |
| 高可用性（HA）架構 | 超出 Prototype 範疇 |
| 即時串流（Kafka / Flink） | 列為 v2.0 演進方向 |
| 地端 LLM 部署 | 筆電無 GPU，使用 API |
| ELK Stack | 資源過重，列為 v2.0 演進方向 |
| 真實 Trace 系統（Jaeger） | PoC 以 trace_id 欄位模擬，非真實分散式追蹤 |

---

## 7. 模組介面定義

> ⚠️ 以下 Schema 一旦確定，所有人必須遵守。修改需在群組討論，PM 確認後才能變更。

### 7.1 Event Schema（異常偵測輸出）

Event Schema 是 Event Detection、Event Runner、Alert Correlation Engine 與 Incident Manager 之間的共用資料契約。

為避免不同文件維護多份 Schema 造成欄位不一致，本專案規定：

> **Event Schema 的唯一正式定義以 `PRD-002：Event Detection` 第 5 章為準。**

PRD-001 僅描述 Event 在整體系統中的角色，不重複定義完整欄位。

所有後續 SPEC 文件，包括：

- SPEC-001 Log Event Detection
- SPEC-002 Metrics Threshold Detection
- SPEC-003 Metrics Isolation Forest Detection
- SPEC-004 Event Runner

皆必須遵守 PRD-002 第 5 章定義的 Event Schema。

任何模組不得自行新增、刪除或重新命名 Event Schema 欄位。若需調整 Schema，必須先更新 PRD-002，並經 PM 確認後，才可修改對應 SPEC 與程式碼。

### 7.2 Incident Schema（收斂輸出）

（詳見 G5 章節）

### 7.3 RCA Report Schema（LLM 輸出）

（詳見 G6 章節）

---

## 8. 團隊分工

### 8.1 模組 Owner

| 模組 | Owner | 全員協作內容 |
|---|---|---|
| G1 資料模擬 + G2 觀測堆疊 | 成員 A（夜羽） | 一起跑通環境、驗證六大劇本資料 |
| G3 異常偵測 + G4 告警收斂 + G5 Incident | 成員 B | 一起驗證收斂規則、測試各劇本 |
| G6 RAG + LLM | 成員 C | 一起整理 SOP 文件、測試 Prompt 品質 |
| G7 Dashboard + G8 Email + Demo 影片 | 成員 D | 一起給 UI 回饋、撰寫 Demo 腳本 |
| 文件 + 對外溝通 + 資安審查 | 你（PM） | 維護 SDD、ADR、PRD，對接富邦，資安紅線把關 |

### 8.2 資安審查責任

你（PM）在每次模組完成後，執行以下清單：

- [ ] `.env` 有無被 commit（`git log --all` 搜尋）
- [ ] 程式碼中有無 hardcode API Key 或密碼
- [ ] Mock Data 有無出現個資格式字串
- [ ] `vector_store/` 和 `*.pkl` 有無在 `.gitignore`
- [ ] 送給 Gemini API 的 prompt 有無夾帶敏感字串

### 8.3 Git 工作流程

```
main        ← 只有 PM 可以 merge，發表前才更新
  └── develop ← 每週五整合
        ├── feature/data-simulator
        ├── feature/anomaly-detector
        ├── feature/incident-manager
        ├── feature/rag-llm
        └── feature/dashboard
```

---

## 9. 專案時程

| 週次 | 時間 | 里程碑 |
|---|---|---|
| W1–W2 | 7月 第1–2週 | 環境建置、GitHub 建立、Schema 確認、資安規範公告 |
| W3–W4 | 7月 第3–4週 | 模組一完成：六大劇本 Mock Data 能跑，Grafana 能看到 |
| W5–W7 | 8月 第1–3週 | 模組二完成：六大劇本能正確偵測並收斂為 Incident |
| W8–W10 | 8月第4–9月第2週 | 模組三完成：LLM 能針對各劇本產出有意義的 RCA |
| W11–W12 | 9月 第3–4週 | 模組四完成：Dashboard 完整呈現 + Email 通知正常 |
| W13 | 10月 第1週 | 全流程整合測試 + 富邦 Demo + 請求驗證書 |
| W14–W16 | 10月 第2–4週 | 精修 + 六大劇本 Demo 影片錄製 |
| W17 | 11月 第1週 | 資服盃提交（11/7）+ 系上發表準備 |

---

## 10. 驗收標準

### 模組一（資料 + 觀測）
- [ ] `docker compose up` 一指令啟動所有服務
- [ ] 能觸發六大劇本，各劇本的 Log 特徵在 Grafana Loki 中可見
- [ ] Prometheus 能看到各劇本對應的 Metrics 異常

### 模組二（Event Detection + 後續收斂與 Incident）

#### Event Detection acceptance
- [ ] 劇本一：使用 exactly 50 筆核准 Demo／E2E 401 輸入產生 `brute_force_detected`；正式分類門檻維持同一 `source_ip`、60 秒內 ≥ 10 筆 401
- [ ] 劇本二：產生 `cross_service_failure` + `high_latency_detected`
- [ ] 劇本三：產生 `oom_crash_detected` + `high_memory_detected`
- [ ] 劇本四：產生 `external_dependency_failure`
- [ ] 劇本五：產生 `downstream_cascade_failure`
- [ ] 劇本六：使用 exactly 55 筆核准 Demo／E2E 429 輸入產生 `rate_limit_storm` + `request_spike_detected`；正式 Log 分類門檻維持同一 `target_service`、60 秒內 ≥ 20 筆 429，QPS ratio 維持 config-driven

#### 後續 Incident／Correlation／RCA／LLM pipeline acceptance
- [ ] 各劇本的 Event 由後續 Correlation／Incident pipeline 正確收斂為對應 Incident
- [ ] RCA／LLM 行為依模組三驗收；不得取代上述 Event Detection acceptance

> Event Detection milestone 已完成 implementation／validation，不表示 Incident、Correlation、RCA 或 LLM 已完成驗收。

### 模組三（RAG + LLM）
- [ ] 各劇本 RCA 報告有正確的根因假說（對應各劇本場景）
- [ ] `sop_reference` 能正確引用對應劇本的 SOP
- [ ] RCA JSON 結構符合 Schema

### 模組四（Dashboard + Email）
- [ ] Incident 列表正確顯示六大劇本各自的嚴重度
- [ ] 詳情頁能看到 RCA 報告、修復步驟、相關 Log
- [ ] Critical Incident 觸發時 Gmail 收到通知

### 整合驗收
- [ ] 六大劇本各自走完完整閉環，全程無人工介入
- [ ] Demo 影片剪輯完成（六大劇本各一段）
- [ ] 資安清單全部通過

---

## 11. 技術選型

| 用途 | 工具 | 版本 |
|---|---|---|
| 容器化 | Docker Compose | 最新穩定版 |
| Metrics 收集 | Prometheus | v2.51 |
| Log 收集 | Grafana Loki + Promtail | v2.9 |
| 視覺化 | Grafana | v10.4 |
| Log 異常偵測 | scikit-learn Isolation Forest | 1.4.x |
| Metrics 異常偵測 | Threshold + Isolation Forest | 1.4.x |
| 向量資料庫 | ChromaDB | 最新穩定版 |
| LLM | Gemini 2.5 Flash API | google-genai 1.16.x |
| Embedding | Google text-embedding-004 | — |
| 後端 | FastAPI + Uvicorn | 0.115.x |
| 前端 | Jinja2 + Tailwind CSS CDN | — |
| Email | Gmail SMTP | smtplib（Python 標準庫） |
| 主要語言 | Python | 3.11 |

---

## 12. 規劃演進方向（v2.0）

- ELK Stack 替換 Loki（更強的全文搜尋）
- Kafka 串流架構（毫秒級延遲）
- 真實 Trace 系統（Jaeger / Zipkin）
- 地端 LLM（企業資安合規）
- 預測性告警（Predictive Alert）
- 自動修復（Auto Remediation）
- 多租戶架構（Multi-tenant）

---

## 13. 相關文件與驗證證據

正式需求與實作契約依治理優先序參照：PRD-002 v1.3、SPEC-001 v2.2、SPEC-002 v1.4、SPEC-003 v1.1、SPEC-004 v1.1。

SPEC-005 v1.2 僅作 Implementation／Validation Evidence（non-normative）：Phase 5 Observability PASS、Phase 6 S1–S6 PASS、E2E Exit Code 0、Phase 7 PASS WITH KNOWN LIMITATIONS、Blocking Defects 0。這些結果不新增、取代或放寬正式 product requirement，也不將 observed E2E values 升級為永久門檻。

---

*本文件隨專案進度持續更新。重大設計決策請另見 `docs/ADR-001.md`。*
