# AIOps 智慧維運平台
## 產品需求文件（PRD）v3.4

> **文件狀態**：執行中
> **最後更新**：2026-08-29
> **適用對象**：東吳大學資管系專題四人組
> **對應文件**：SDD v0.1、ADR-001（均由 Google Drive 管理）

| Version | Date | Change |
|---|---|---|
| 3.1 | 2026-07-26 | 釐清 Metrics Threshold 與 Isolation Forest 雙軌分工；SPEC-003 v1.0 限定 QPS；新增未知 QPS Event 說明；DB Pool 定義為僅觀測與未來擴充。 |
| 3.2 | 2026-08-12 | Post-SPEC-005 Cross-Document Governance Batch 2B：依 PRD-002 v1.3 與 SPEC-001～004 正式契約同步 S1、S3、S6、模組二驗收與 G3 里程碑；補充 SPEC-005 v1.2 非規範性驗證證據及相關文件。整體平台狀態維持執行中。 |
| 3.3 | 2026-08-16 | 擴充 downstream Incident Lifecycle 產品方向；調和 Dashboard、Jira、Discord、Email 角色；加入 human-in-the-loop workflow、retention 與 knowledge feedback 方向，並修正過時治理與部署文字。Event Detection authoritative contract 不變。 |
| 3.4 | 2026-08-29 | Post-PRD-003 backward governance reconciliation：對齊 correlation authority、Incident lifecycle eligibility、RCA persistence boundary、external interface authority、retention／reset wording與 current document references；不宣稱 downstream implementation 完成。 |

---

## 1. 專案定位

### 1.1 一句話定義

建立一套 AIOps Prototype，自動完成 Detection → Correlation → Incident creation → Assignment → RCA generation → technical presentation／operational delivery，並透過 Dashboard、Jira、Discord 與 conditional Email 支援人機協作的 Incident handling closed loop；remediation、review 與 closure 保留 human-in-the-loop，以降低 MTTR 並提升處理可追溯性。

### 1.2 目標用途

- 資服盃產學合作組參賽（截止：2026/11/07）
- 系上專題發表（預計：2026/11 中旬）
- 取得富邦金控驗證同意書（企業背書）

### 1.3 Demo 形式

影片錄製（非 Live Demo），保留六大劇本，展示 automated technical pipeline 與 human-in-the-loop operational workflow；不宣稱完整 Incident lifecycle 全程無人工介入。

---

## 2. 核心架構

### 2.1 Incident-driven Architecture

```text
Logs / Metrics
      │
      ▼
Event Detection ──→ Events ──→ Alert Correlation
                                      │
                                      ▼
                         Incident Manager / Incident Record
                            │        │        │        │
                            ▼        ▼        ▼        ▼
                       Dashboard    Jira   Discord   LLM + RAG
                                                       │
                                                       ▼
                                                      RCA
                                                       │
                                                       ▼
                                      update same Incident context
                                          │        │        │
                                          ▼        ▼        ▼
                                     Dashboard    Jira   Discord

Email = Fallback / Escalation only
```

Incident Manager／Incident Record 是平台內 Incident lifecycle 與 persistence authority。Dashboard、Jira、Discord、Email 是不同的 view／operational interface／adapter；Jira 不是 Incident source of truth，Dashboard 也不是資料儲存或 workflow gate。RCA 回寫同一 Incident context，不只存在於 Dashboard。

### 2.2 ADR／SDD 治理

- SDD 與 ADR-001 由 Google Drive 管理，repository 不建立 mirror。
- ADR-001 是 historical architecture decision record；PRD-001 v3.3 不 retroactively rewrite ADR-001。
- 未來若發生重大架構改變，應建立新 ADR，不覆寫歷史決策。
- 本文件不宣稱外部 SDD 已同步 v3.3 的功能編號或內容。

### 2.3 資安邊界

> ⚠️ **重要**：以下規範為資安紅線，實作時嚴格遵守，不得繞過。

| 資安項目 | 規範 |
|---|---|
| Credentials | Jira token／credentials、Discord bot token、Discord webhook URL、Email credentials、LLM API credentials 一律不得 hardcode、不得 commit，採 environment／config-based secret handling；本 PRD 不指定新的 secret-management product。 |
| `.gitignore` | `.env`、`*.pkl`（模型檔）、`vector_store/`（向量庫）必須列入。 |
| Log 去識別化 | Mock Data 中不得出現真實個資格式（如身分證字號、信用卡號）。 |
| LLM 傳輸 | 送給 LLM API 的內容僅限模擬資料，禁止夾帶任何敏感字串。 |
| ChromaDB | 本地儲存，不對外開放 port。 |
| Dashboard | 本地 localhost，不部署至公網。 |

---

## 3. 六大模擬劇本

這六個劇本是系統的核心展示內容。各劇本的 Event Detection threshold、Event Type、approved Demo／E2E input 與 detector semantics 維持 frozen；共同 downstream 方向為 Events → Correlation → one Incident → Assignment → RCA → operational workflow → review／closure。

### 劇本一：單點爆破與帳號鎖定（垂直收斂）

| 項目 | 說明 |
|---|---|
| 場景描述 | 駭客針對單一帳號暴力嘗試密碼，觸發系統保護機制。 |
| 正式偵測契約 | 同一 `source_ip` 在 60 秒內出現 ≥ 10 筆 `status_code=401`，分類為 `brute_force_detected`；≥ 10 是 Detector classification threshold。 |
| Approved Demo／E2E Input | 同一 `source_ip` 在 60 秒內 exactly 50 筆 `status_code=401`；exactly 50 是核准的驗證輸入，不是 Detector 門檻，且不包含額外 account-lock Log。 |
| 收斂邏輯 | 相同 IP + 極短時間窗 → 壓縮為 1 個安全威脅 Incident。 |
| 展示價值 | AI 能識別「高頻同源」特徵；產品層級上可支援後續帳號保護機制，但 account-lock Log 不是本劇本的正式 Event Detection input。 |
| 涉及欄位 | `source_ip`、`user_id`、`status_code`、`timestamp`。 |

### 劇本二：資料庫卡頓引發 API 雪崩（跨服務收斂）⭐ 最經典

| 項目 | 說明 |
|---|---|
| 場景描述 | DB 慢查詢 → AP 層卡死 → Gateway 回傳 504 給用戶。 |
| 產生資料 | 帶有**相同 trace_id** 的三層錯誤 Log（DB／AP／Gateway）。 |
| 收斂邏輯 | 透過 `trace_id` 關聯三筆跨服務 Log → 1 個 Incident。 |
| 展示價值 | 精準定位「Gateway 504 的根因是底層 DB 慢查詢」。 |
| 涉及欄位 | `trace_id`、`service_name`、`downstream_service`、`duration_ms`。 |

### 劇本三：記憶體耗盡（OOM）導致服務崩潰（資源與應用收斂）

| 項目 | 說明 |
|---|---|
| 場景描述 | AP 記憶體滿載 → OOM 崩潰；前端出現 502 Bad Gateway 是可能的 downstream／user-visible symptom。 |
| 正式偵測契約 | Log 中的 `OOM`／`OutOfMemoryError` 證據產生 `oom_crash_detected`；`system_memory_usage_pct >= 90%` 產生 `high_memory_detected`。 |
| Validation Input／Evidence | SPEC-005 Demo 使用 `memory=95%` 作為驗證輸入；95 不是永久門檻。502 不是 required detector trigger、required generated Log 或 required acceptance condition。 |
| 收斂邏輯 | 基礎設施警告 + 應用崩潰 → 1 個 Incident。 |
| 展示價值 | AI 能看穿 502 的本質是記憶體洩漏，而非網路問題。 |
| 涉及欄位 | `memory_usage_pct`（Metrics）、`error_type`、`status_code`。 |

### 劇本四：第三方銀行 API 斷線（外部依賴收斂）

| 項目 | 說明 |
|---|---|
| 場景描述 | 外部銀行閘道 API 無回應 → 內部交易失敗（500）。 |
| 產生資料 | 外部連線逾時（30s）+ 內部匯款交易失敗 Log。 |
| 收斂邏輯 | `external_service` 欄位標記為外部依賴 → 獨立 Incident 分類。 |
| 展示價值 | 金融業高優先場景：立刻釐清「不是我們的錯，是外部服務掛了」。 |
| 涉及欄位 | `external_service`、`timeout_ms`、`transaction_id`。 |

### 劇本五：共用資料庫網路瞬斷（微服務連鎖陣亡風暴）

| 項目 | 說明 |
|---|---|
| 場景描述 | 核心 DB 網路斷線 → 所有微服務同時噴出連線拒絕 → Gateway 全面 503。 |
| 產生資料 | 短時間內數十筆、帶有**不同 trace_id**、**不同 service_name**，並共同指向 `downstream_service=core-db` 的 Log。 |
| 收斂邏輯 | 共同的 `downstream_service=core-db` → 收斂為 1 個 Incident。 |
| 展示價值 | 將「告警風暴」完美收斂，一眼看出核心資料庫是單點故障源。 |
| 涉及欄位 | `downstream_service`、`error_code`（connection refused）、多個 `service_name`。 |

### 劇本六：API 額度打爆（429 Rate Limit 告警風暴）

| 項目 | 說明 |
|---|---|
| 場景描述 | 同一 `target_service` 發生 HTTP 429 storm，並伴隨 QPS 異常。 |
| Log Detector 契約 | 同一 `target_service` 在 60 秒內出現 ≥ 20 筆 `status_code=429`，分類為 `rate_limit_storm`；≥ 20 是 Detector classification threshold。 |
| Approved Demo／E2E Input | 同一 `target_service` 在 60 秒內 exactly 55 筆 `status_code=429`；exactly 55 是核准的驗證輸入，不是 Detector 門檻。 |
| Metrics IForest 契約 | Isolation Forest 判定 QPS Window 異常，且 `current_qps / baseline_mean >= configured request_spike_ratio`，分類為 `request_spike_detected`；ratio 維持 config-driven，3.0 與 SPEC-005 的 4x scenario generation value 均不是不可變 algorithm requirement。 |
| Event Detection 預期證據 | `rate_limit_storm` + `request_spike_detected`。 |
| 收斂邏輯 | downstream correlation／RCA invocation suppression 將高頻同質 Event 靜默折疊，避免重複分析；不重新定義 detector cooldown。 |
| 展示價值 | 展示「高頻同質性垃圾告警的靜默與折疊」，保護 LLM API 成本。 |
| 涉及欄位 | `status_code=429`、`target_service`、`rate_limit_quota`。 |

---

## 4. 功能需求（G1–G8）

### G1 資料模擬層

| 項目 | 說明 |
|---|---|
| Log 模擬器 | 產生 JSON 格式 AP Log，支援六大劇本的異常注入。 |
| Metrics 模擬器 | 產生 p95 Latency、Error Rate、Memory Usage、DB Pool 等指標。 |
| Trace ID 機制 | 劇本二跨服務 Log 使用相同 `trace_id`；劇本五使用不同 `trace_id`、不同 `service_name`，以共同 `downstream_service` 表達共用下游故障。 |
| 外部服務標記 | `external_service` 欄位標記外部依賴（劇本四使用）。 |
| 劇本觸發介面 | 提供簡單觸發方式，可切換不同劇本；具體介面留實作規格。 |

`db_pool_active_connections` 雖由 Metrics Generator 產生，並可由 Prometheus 收集及於 Grafana 顯示，但本階段僅作為觀測與未來擴充指標，不納入 Metrics Threshold Detection 或 Metrics Isolation Forest Detection v1.1 的正式 Event Detection 範圍。

### G2 觀測層

| 項目 | 說明 |
|---|---|
| Log 收集 | Promtail → Grafana Loki。 |
| Metrics 收集 | Python Exporter → Prometheus（port 8000，15 秒抓取）。 |
| 視覺化 | Grafana Dashboard（各劇本指標折線圖）。 |
| 部署現況 | Docker Compose 啟動 observability services；Scenario runtime 為另一程序，Grafana datasource／dashboard 尚需人工 setup。完整平台 one-command startup 尚未完成。 |

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
| G3 里程碑 | Event Detection implementation／validation completed：SPEC-001～004 Implemented；SPEC-005 v1.3 Implemented，S3 Identity Revalidation PASS。此里程碑完成不代表 AIOps Platform 整體完成。 |

Threshold 與 Isolation Forest 不互相取代或排斥。同一事故可能同時產生不同來源的 Metrics Event，後續再由 Event Runner 與 Alert Correlation 進行整理與收斂。

### G4 Alert Correlation

EventStore／Events → Alert Correlation → correlated Incident creation trigger → Incident Manager。核心產品價值是將多筆相關 Event 收斂成一件 Incident。

| 產品需求 | 說明 |
|---|---|
| 六大劇本支援 | 支援 evidence-driven Strong／Known Weak／Shadow policy；不得以 scenario、generator 或 validator expected answer 作 runtime correlation decision。 |
| 概念邊界 | Detector Cooldown、Correlation Window、Pending Grace 與 RCA invocation protection／suppression 是不同概念。任何舊「60 秒內同類 Event 不重複觸發 LLM」敘述僅表示 RCA invocation protection intent，不是 Correlation Window，也不是 detector cooldown。 |
| 詳細權威 | `PRD-003 v1.0 Final` 是 Correlation Window、Strong／Weak policies、Pending、Shadow、fingerprints、Incident creation、ownership／dedup／recovery等 detailed requirements 的 authoritative source；後續 Engineering SPEC 負責 implementation contract。PRD Final 不代表 implementation complete。 |

### G5 Incident Management

#### G5.1 Incident Manager and Incident Record Authority

Incident Manager／Incident Record 是平台內 Incident lifecycle 與 persistence authority，接收 correlated Incident creation trigger，維持事件關聯、狀態、指派、RCA、resolution、review、external references 與 audit trail。外部介面不得各自成為另一份 authoritative Incident。

#### G5.2 Incident Lifecycle

`OPEN → ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED`

RCA processing status 不屬於 Incident lifecycle state，兩者必須分離。

Lifecycle view 的 Active 與 correlation eligibility 也必須分離。依 `PRD-003 v1.0 Final`，只有 `OPEN`、`ASSIGNED`、`IN_PROGRESS` 是 correlation-open；`AWAITING_REVIEW` 雖仍可顯示於 Active operational view，但已 correlation-closed；`CLOSED` 是 correlation-closed／terminal。

`ASSIGNED`／`IN_PROGRESS` 可繼續接受 compatible evidence並進行 evidence enrichment或 severity escalation，但不得 reset lifecycle、assignee、reviewer、替換 Incident identity、重新啟動 assignment workflow或 silent overwrite既有RCA。Material evidence所需的RCA refresh／supersede／versioning或等價auditable update機制，留後續RCA PRD／SPEC定義。

#### G5.3 Assignment Policy

PoC 採單一 Operations Team：Supervisor、Engineer A、Engineer B、Engineer C。新 Incident 自動以 Round Robin 方向 A → B → C → A 指派，並保留 manual override。本節只定義產品行為，不固定演算法或 index implementation。

#### G5.4 Engineer／Reviewer Workflow

- Engineer 接受指派後，將 Incident 由 `ASSIGNED` 推進至 `IN_PROGRESS`。
- 處理後留下 resolution evidence：Actual Action、Resolution Note、SOP Followed?、Additional Note，再推進至 `AWAITING_REVIEW`。
- Supervisor／Reviewer 檢查 Incident evidence、RCA、resolution evidence 與 recovery state；確認後推進至 `CLOSED`。
- 此流程是 lightweight workflow，不代表已完成 Enterprise ITSM。

#### G5.5 Resolution Evidence

Resolution evidence 是 Incident Record 的產品級資料，用於說明實際處置、處置結果與 SOP 遵循狀況，並支援 reviewer closure 與後續稽核；詳細欄位契約留 PRD-003。

#### G5.6 Knowledge Improvement Candidate

若 Engineer actual remediation 與 RCA／current SOP 存在 deviation，平台建立 Knowledge Improvement Candidate，交由 Human／Supervisor Review。PoC 不自動修改 SOP 或 Knowledge Base、不自動 RAG ingestion、不自動更新 Vector DB，也不自動 retraining；上述能力列入 Future Work。

#### G5.7 Active／History

Dashboard／operational view可將 `OPEN` 至 `AWAITING_REVIEW` 稱為 Active Incident，並將 `CLOSED` 顯示於 History；但 **Active Incident ≠ correlation-open Incident**。Normal correlation只接受`OPEN`、`ASSIGNED`、`IN_PROGRESS`，`AWAITING_REVIEW`與`CLOSED`均correlation-closed。Closed不等於Delete，Dashboard不得成為workflow gate。

#### G5.8 Lifecycle／Retention／Reset-Cleanup Governance

- Lifecycle 是 Incident operational workflow；Retention 是資料保留／archive policy；Reset／Cleanup 是受控 development、testing或maintenance operation，三者不得混用。
- PoC default：Event、Incident、Correlation Processed State、Shadow與相關RCA relationship/history不因時間經過或Incident `CLOSED`自動刪除。
- Production retention／archive policy未來可依compliance、storage cost、audit與learning requirements設定；具體期限與physical mechanism留後續SPEC。
- Destructive reset／cleanup不屬normal runtime，不得由validator自動執行、不得因test failure自動執行，AI coding agent也不得自行執行。事前必須列出affected stores／data並取得PM／team authorization；scoped cleanup須維護referential integrity。

#### G5.9 External Interface Synchronization

Incident Manager 追蹤 Jira、Discord 與 Email delivery／synchronization reference。外部動作需具可追溯性；同步失敗不得破壞 platform Incident authority，並應形成可觀察、可重試或可升級的 failure boundary。詳細 payload、retry 與 conflict policy 留後續 PRD／SPEC。

#### G5.10 Preliminary Product Data Shape

Incident 的 concept-level data needs 包含 identity、timestamps、lifecycle status、severity、assignee、reviewer、correlated Events、RCA relationship／current state、resolution evidence、Knowledge Improvement Candidate 與 external interface references。Detailed authoritative Incident Schema與correlation／persistence contract由`PRD-003 v1.0 Final`定義；本節preliminary shape不是authoritative schema。

既有 `incidents/incident_store.jsonl` 僅可視為目前 Prototype／proposed persistence direction，不是不可變 storage contract；本文件不定義 database class 或 store implementation。

### G6 RAG + LLM／RCA

| 項目 | 說明 |
|---|---|
| 知識來源 | 六大劇本對應 SOP，供 RAG 召回。 |
| 輸入概念 | Incident、相關 Logs／Metrics evidence 與召回的 SOP。 |
| 輸出概念 | Incident summary、severity assessment、ranked root-cause hypotheses、evidence、SOP reference、remediation steps 與 prevention measures。此為產品輸出概念，不是不可變 schema。 |
| Cardinality | Processing 期間 `1 Incident : 0..1 RCA`；successful RCA completion 後 `1 Incident : 1 RCA`。 |
| Processing state | `PENDING／GENERATING`、`COMPLETED`、`FAILED`；這些不是 Incident lifecycle states。 |
| Persistence | Incident只保存RCA relationship／current state，例如`rca_status`、`rca_ref`；完整RCA Artifact未來可由獨立RCA persistence authority保存。PRD-001不鎖定physical persistence，Incident不得duplicate完整RCA Artifact；Dashboard只是View，Jira／Discord只接收摘要、reference或presentation。 |
| Failure／Fallback | LLM 失敗時 RCA processing 標記 `FAILED`，不把 mock／fallback RCA 當成 successful generated RCA。Demo 可使用 availability／presentation fallback 維持畫面可用，但它不是 successful RCA evidence，也不自動關閉 Incident。 |

### G7 Dashboard — Technical View／Team Visibility

Dashboard 支援 Active Incidents、Incident History 與 Incident Detail。Detail 可包含 severity、lifecycle status、assignee、reviewer、Jira reference／status、Discord War Room reference、correlated Events、Logs／Metrics technical evidence、RCA、resolution 與 Knowledge Improvement Candidate。

Dashboard 是 technical view／team visibility，不是 persistence authority 或 workflow gate；Supervisor 不需先登入 Dashboard 才能完成指派或其他 lifecycle action。

### G8 Operational Integrations and Notification

#### G8.1 Jira — Incident Work Management

- 產品方向為 `1 Incident → 1 Jira Ticket`。
- Jira 承載 Assignee、Work status、Engineer resolution、Awaiting Review、Reviewer／Supervisor closure 與 accountability。
- Jira 不是 Incident source of truth；平台 Incident Manager／Incident Record 保持 authority。
- Jira只可作human workflow interface／closure intent source。任何closure intent都必須由Incident Manager驗證lifecycle guard、required reviewer approval與recovery verification，再authoritatively執行`AWAITING_REVIEW → CLOSED`；完成後才同步Jira representation、Dashboard Active → History與Discord closing workflow。外部介面不得直接覆寫Incident authority。
- Jira Free 現階段以 Engineer／Reviewer workflow responsibility 為妥協，不宣稱 Enterprise RBAC 或 issue-level isolation。
- REST API、webhook payload、auth flow 與 retry 留後續 PRD／SPEC。

#### G8.2 Discord — Primary Notification／War Room／ChatOps

```text
#incident-alerts
    └── one thread per Incident

#aiops-query
    └── optional global query
```

- Discord 是 primary operational notification、War Room 與 ChatOps interface。
- 每件 Incident 只建立一個主要 collaboration context，不為每件 Incident 建永久 channel。
- Incident created 時發送 notification 並建立 Incident thread；RCA generating 與 RCA completed 均更新同一 thread，不把 RCA 當第二次完整告警重複轟炸。
- Incident-scoped controlled natural-language query 可包含「為什麼判斷 DB 是 root cause？」、「顯示最近 10 分鐘 latency evidence」、「RCA 建議的 SOP 要如何執行？」。
- Future query data source 可包含 Incident、RCA、Prometheus、Loki、SOP／RAG Knowledge。受控 query／tool routing 下，LLM 不得直接任意執行 PromQL 或 Log query。
- `#aiops-query` 是 optional global query interface。

#### G8.3 Discord Closure

Incident `CLOSED` 後，在同一 Incident thread 發布 Closing Summary，接著 Lock／Archive；預設不立即Delete。後續archive／retention依Retention policy治理；任何destructive cleanup仍須遵守G5.8的獨立authorization與referential-integrity規則。

#### G8.4 Email — Fallback／Escalation

Email 只作 Fallback／Escalation，不是 every Incident primary notification。產品級觸發例包括 Critical Incident 長時間未 acknowledged、Discord／Jira delivery failure、Awaiting Review timeout；exact timeout 與 exact retry count 留後續 PRD／SPEC。

---

## 5. 非功能需求

| 項目 | 規範 |
|---|---|
| 零額外費用 | 所有工具使用免費方案或開源工具。 |
| 個人筆電可跑 | 無 GPU 需求；PoC 可在個人筆電執行。 |
| 模組化／Adapter replaceability | 模組以穩定產品邊界溝通，Dashboard、Jira、Discord、Email adapter 可替換，不改變 Incident authority。 |
| Credential safety | 所有 credentials 不 hardcode、不 commit，採 environment／config-based handling。 |
| Sensitive-data protection | LLM 輸入前過濾敏感資料；Mock Data 不含真實個資。 |
| Lifecycle consistency | 平台 lifecycle 狀態與外部介面呈現需可對照，RCA processing state 與 Incident lifecycle 分離。 |
| Synchronization traceability | 外部同步需留下 reference、狀態或 audit evidence。 |
| Human action auditability | assignment override、resolution evidence、review 與 closure 應可追溯。 |
| Configurable retention | Incident、RCA 與相關資料的 retention policy 可設定。 |
| Graceful integration failure boundary | Jira、Discord、Email 或 LLM failure 不得偽造成功結果或破壞 Incident Record；fallback 只維持 availability／presentation。 |

---

## 6. 系統限制（Out of Scope）

PoC 不包含：

- 真實企業資料與正式生產環境部署
- 高可用性（HA）架構、即時串流（Kafka／Flink）、真實 Trace 系統（Jaeger）
- Enterprise-grade RBAC、full ITSM platform、real enterprise on-call scheduling
- workload-aware production assignment、skill-based assignment、service ownership routing、cross-team routing
- automatic remediation
- automatic SOP modification、automatic Knowledge Base modification
- automatic RAG ingestion、automatic Vector DB update、automatic retraining
- production-grade retention enforcement

產品方向可保留 adapter 與 future integration，但本文件不宣稱上述能力已完成。

---

## 7. 模組介面定義

### 7.1 Event Schema（異常偵測輸出）

Event Schema 是 Event Detection、Event Runner、Alert Correlation 與 Incident Manager 之間的共用資料契約。

> **Event Schema 的唯一 authoritative definition 是 `PRD-002：Event Detection` 第 5 章。**

PRD-001 僅描述 Event 在整體系統中的產品角色，不重複定義完整欄位。SPEC-001 Log Event Detection、SPEC-002 Metrics Threshold Detection、SPEC-003 Metrics Isolation Forest Detection 與 SPEC-004 Event Runner 均遵守 PRD-002 第 5 章。任何模組不得由 PRD-001 v3.4 自行新增、刪除或重新命名 Event Schema 欄位。

SPEC-003／SPEC-004／SPEC-005 中的 PRD-001 v3.2 references 是 historical implementation／reconciliation baseline；本輪不修改這些文件，也不改變 frozen Event Detection contract。

### 7.2 Incident Schema（收斂輸出）

PRD-001 只描述 Incident 的產品角色與 conceptual data needs。Detailed authoritative Incident Schema、correlation contract與logical persistence roles由`PRD-003 v1.0 Final`定義；synchronization implementation contract留後續Engineering SPEC。G5的preliminary product data shape不具authoritative schema效力。

### 7.3 RCA

- Processing 期間：`1 Incident : 0..1 RCA`。
- Successful RCA completion 後：`1 Incident : 1 RCA`。
- RCA processing state 與 Incident lifecycle state 分離。
- Incident只保存RCA relationship／current state；完整RCA Artifact可由future independent persistence authority保存。Detailed artifact schema、refresh／supersede／versioning與implementation留後續RCA PRD／SPEC。

---

## 8. 團隊分工

### 8.1 模組 Owner

| 模組 | Owner | 全員協作內容 |
|---|---|---|
| G1 資料模擬 + G2 觀測堆疊 | 成員 A（夜羽） | 一起跑通環境、驗證六大劇本資料。 |
| G3 異常偵測 + G4 告警收斂 + G5 Incident | 成員 B | 一起驗證收斂規則、測試各劇本。 |
| G6 RAG + LLM | 成員 C | 一起整理 SOP 文件、測試 Prompt 品質。 |
| G7 Dashboard + 既有 Demo 影片工作 | 成員 D | 一起給 UI 回饋、撰寫 Demo 腳本。 |
| 文件 + 對外溝通 + 資安審查 | 你（PM） | 維護需求與治理文件、對接富邦、資安紅線把關。 |
| Jira／Discord／Incident workflow／Reviewer workflow | TBD／role-based ownership | PM 後續核准 owner；本版不自行指派人名。 |

### 8.2 資安審查責任

你（PM）在每次模組完成後，執行以下清單：

- [ ] `.env` 有無被 commit（`git log --all` 搜尋）
- [ ] 程式碼中有無 hardcode Jira credentials、Discord token／webhook URL、Email credentials、LLM／API credentials 或密碼
- [ ] Mock Data 有無出現個資格式字串
- [ ] `vector_store/` 和 `*.pkl` 有無在 `.gitignore`
- [ ] 送給 LLM API 的 prompt 有無夾帶敏感字串

### 8.3 Git 工作流程

```text
main       = stable milestone branch
develop    = current integration baseline
feature/*  = individual feature development
```

PM 負責 integration review、merge／integration decision，以及將 stable milestone promotion to `main`。

---

## 9. 專案狀態與 Milestone-based Roadmap

本節以 Actual Status 與 milestone 管理，不新增或移動既有專案日期。

| 階段 | 狀態 | 內容 |
|---|---|---|
| Completed | 已完成 | Mock Data／Observability foundation。 |
| Completed | 已完成 | Event Detection。 |
| Completed | 已完成 | Event Runner。 |
| Completed | 已完成 | Scenario／E2E validation。 |
| Next | Requirements Final／Implementation Pending | Alert Correlation（PRD-003 v1.0 Final；Engineering SPEC待建立）。 |
| Next | Requirements Final／Implementation Pending | Incident Manager／lifecycle（PRD-003 v1.0 Final；Engineering SPEC待建立）。 |
| Planned downstream | 規劃中 | RAG／LLM RCA、Dashboard、Jira、Discord／ChatOps、Email fallback／escalation、full integration／Demo。 |

Event Detection 與 Event Runner 的完成不代表 Alert Correlation、Incident、RCA 或整體平台已完成。既有產品目標維持資服盃截止 2026/11/07 與系上專題發表預計 2026/11 中旬。

---

## 10. 驗收標準

### 10.1 Mock Data／Observability Foundation

- [x] 六大劇本的 Log／Metrics foundation 可供驗證。
- [x] Observability services 可由 Docker Compose 啟動。
- [ ] Scenario runtime、Grafana datasource／dashboard setup 與完整平台 startup 尚需整合；不得以目前 Compose 宣稱 one-command 啟動所有服務。

### 10.2 Event Detection Acceptance（已完成且 governance-reconciled）

- [x] 劇本一：使用 exactly 50 筆核准 Demo／E2E 401 輸入產生 `brute_force_detected`；正式分類門檻維持同一 `source_ip`、60 秒內 ≥ 10 筆 401。
- [x] 劇本二：產生 `cross_service_failure` + `high_latency_detected`。
- [x] 劇本三：產生 `oom_crash_detected` + `high_memory_detected`。
- [x] 劇本四：產生 `external_dependency_failure`。
- [x] 劇本五：產生 `downstream_cascade_failure`。
- [x] 劇本六：使用 exactly 55 筆核准 Demo／E2E 429 輸入產生 `rate_limit_storm` + `request_spike_detected`；正式 Log 分類門檻維持同一 `target_service`、60 秒內 ≥ 20 筆 429，QPS ratio 維持 config-driven。

### 10.3 Downstream Correlation／Incident Acceptance

- [ ] Relevant Events 收斂成 one expected Incident，並由 Incident Manager 建立 authoritative Incident Record。
- [ ] Incident 依 `OPEN → ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED` 推進。
- [ ] 新 Incident 依 PoC Round Robin 自動指派，並可 manual override。
- [ ] 每件 Incident 對應一張 Jira Ticket，且 Jira 不成為 Incident authority。
- [ ] 每件 Incident 在 Discord 使用 single-thread collaboration context。
- [ ] RCA processing state 與 Incident lifecycle 分離，successful completion 符合一件 Incident 一份 RCA。
- [ ] Dashboard 正確呈現 Active／History 與 Incident Detail，但不成為 workflow gate。
- [ ] Engineer 留下 Actual Action、Resolution Note、SOP Followed?、Additional Note。
- [ ] Reviewer 檢查 evidence、RCA、resolution 與 recovery state 後完成 closure。
- [ ] Closure 同步反映 platform Incident、Jira、Dashboard History 與 Discord Closing Summary／Lock／Archive。
- [ ] 若 actual remediation 偏離 RCA／current SOP，建立 Knowledge Improvement Candidate 供人工審查。
- [ ] Email 只在 fallback／escalation 情境使用。

### 10.4 Integration Acceptance

- [ ] Automated：Detection → Correlation → Incident creation → Assignment → RCA → Dashboard／Jira／Discord delivery。
- [ ] Human-in-the-loop：remediation、resolution note、SOP deviation、review、closure。
- [ ] Demo 影片完成並涵蓋六大劇本。
- [ ] 資安審查清單全部通過。

---

## 11. 技術選型

| 用途 | 工具 | 版本／定位 |
|---|---|---|
| 容器化 | Docker Compose | 既有 observability service orchestration；完整平台啟動尚待整合。 |
| Metrics 收集 | Prometheus | v2.51 |
| Log 收集 | Grafana Loki + Promtail | v2.9 |
| 視覺化 | Grafana | v10.4 |
| Log 異常偵測 | scikit-learn Isolation Forest | 1.4.x |
| Metrics 異常偵測 | Threshold + Isolation Forest | 1.4.x |
| 向量資料庫 | ChromaDB | implementation detail deferred；不改變既有 dependency contract。 |
| LLM | Gemini 2.5 Flash API | google-genai 1.16.x |
| Embedding | Google text-embedding-004 | — |
| 後端 | FastAPI + Uvicorn | 0.115.x |
| 前端 | Jinja2 + Tailwind CSS CDN | — |
| Incident Work Management | Jira Cloud | integration detail deferred。 |
| Operational Notification／War Room／ChatOps | Discord | integration detail deferred。 |
| Fallback／Escalation | Gmail SMTP | smtplib（Python 標準庫）。 |
| 主要語言 | Python | 3.11 |

本 PRD 不自行指定新的 SDK、REST version、webhook implementation 或未知 package version。

---

## 12. Future Work

- ELK Stack、Kafka 串流架構、真實 Trace 系統、地端 LLM、Predictive Alert、多租戶架構
- Enterprise RBAC 與 issue-level isolation
- enterprise on-call scheduling、workload-aware assignment、skill-based assignment
- service ownership routing、cross-team routing／recommendation
- severity-based escalation policy
- production retention policy 與 production-grade enforcement
- Knowledge Base approval workflow
- approved SOP → RAG ingestion；經核准後的 Knowledge Base／Vector DB 更新與 retraining governance
- automatic remediation（須另行治理與核准）
- Microsoft Teams adapter
- enterprise ITSM integration

以上均為未來方向，不是目前已實作能力。

---

## 13. 相關文件與驗證證據

正式文件依domain分工：PRD-001 v3.4為執行中的overall platform direction；PRD-002 v1.5為Approved Event Detection authority；SPEC-001 v2.3、SPEC-002 v1.4、SPEC-003 v1.1、SPEC-004 v1.1為Implemented engineering contracts；PRD-003 v1.0是Final Alert Correlation／Incident Management detailed requirements，但implementation仍pending。

SPEC-005 v1.3為Implemented implementation／validation evidence（non-normative），S3 Identity Revalidation PASS；它不取代PRD-002或SPEC-001～004 detector authority，也不將observed E2E values升級為永久門檻。

DDS-001 v1.3是repository-level Mock Data／Observability reference；README只提供project entry point與governance index。

PRD-001 v3.4只做Post-PRD-003 backward governance reconciliation，不retroactively modify frozen Event Detection contracts。SPEC-003／SPEC-004／SPEC-005中的舊PRD references維持historical implementation／reconciliation baseline。

SDD 與 ADR-001 由 Google Drive 管理，repository 不建立 mirror。ADR-001 保持 historical architecture decision record；未來重大架構改變應建立新 ADR。

---

*本文件隨專案進度持續更新。SDD 與 ADR-001 由 Google Drive 管理，不在 repository 建立 mirror。*
