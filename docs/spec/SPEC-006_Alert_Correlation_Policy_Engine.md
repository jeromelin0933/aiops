# SPEC-006 — Alert Correlation Policy Engine

## Software Design Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-006 |
| Document Name | Alert Correlation Policy Engine |
| Version | 1.0 |
| Status | Approved — Implementation Pending |
| Date | 2026-08-30 |
| Requirement Authority | PRD-003 v1.0 Final |
| Upstream Event Contract | PRD-002 v1.5 |
| Implementation Owner（預計） | 富裕 |

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-30 | PRD-003 第一份 Engineering SPEC；定義 deterministic Alert Correlation Policy Engine、versioned Policy Registry、candidate selection、decision 與 structured failure contract。 |

> **SPEC Approved ≠ Implemented。** 本 SPEC 完成後仍須完成 implementation、targeted tests、contract tests 與 full repository regression，通過後才可更新為 `Implemented`。

---

# 0. Authority、目的與核心決策

## 0.1 Authority 與文件優先序

本 SPEC 依下列正式文件制定：

1. `PRD-003 v1.0 Final` 是 Alert Correlation／Incident Management detailed requirement authority。
2. `PRD-002 v1.5` 是 upstream Event Contract authority；SPEC-006 消費其 15-field Runtime Event，不新增、刪除、重新命名或改寫 Event 欄位。
3. `PRD-001` 提供整體平台方向。
4. `SPEC-001`～`SPEC-004` 提供已實作的 Event Detection 與 Runner contract；`SPEC-005` 提供 validation／scenario alignment evidence，但不構成 Runtime correlation answer source。

若 implementation 發現本 SPEC 與最新正式 authority 存在 active contract conflict，衝突範圍必須停止並回報 PM，不得由實作者自行重設計或修改 upstream Event Contract。

## 0.2 核心定位

SPEC-006 定義 **Deterministic Alert Correlation Policy Engine**。給定一筆正式 Runtime Event、目前的 `IncidentCorrelationView[]` 與 `CorrelationEvaluationContext`，Engine 判斷應產生哪一個 correlation decision，或回傳 structured evaluation failure。

本模組只回答：

> **這筆 Event 現在應該去哪裡？**

對相同 Event、Views、Context、Registry 與 Config，必須產生 deterministic equivalent result。

## 0.3 D1～D12 Engineering Decisions

| ID | 正式決策 |
|---|---|
| D1 | Engine 是 pure、deterministic decision layer；不持久化、不做 runtime orchestration、不依賴 wall clock。 |
| D2 | Policy selection 只依 Runtime Event 的 `event_type` 與 exact policy reference；Scenario ID 與 validation answer 不得進入決策。 |
| D3 | `PolicyRegistry` 是 versioned、typed、process-lifetime immutable；bootstrap definition error Fail Fast。 |
| D4 | Policy-owned `IdentityExtractor` 負責 Strong identity 位置與驗證；shared engine pipeline 不堆疊 scenario-specific `if/elif`。 |
| D5 | `NormalizedFingerprint` 是 immutable structured value object，不使用 fragile string concatenation。 |
| D6 | Candidate 共用 lifecycle → window → family → evidence-specific identity gates，並採 staged View validation 與 Fail Closed。 |
| D7 | Strong candidate 採 Tier 1 exact Strong identity，再採 Tier 2 Weak standalone promotion；Tier 1 ambiguity 不得 fallback。 |
| D8 | Known Weak 不依 candidate 自身 Strong／Weak 給 priority；缺乏 identity 時所有 compatible candidates 平等參與 ambiguity。 |
| D9 | Registered `UNKNOWN` 直接 `ROUTE_SHADOW`；unregistered `event_type` 是 `POLICY_NOT_REGISTERED` failure。 |
| D10 | Pending phase 是顯式 evaluation context；到期仍須最後 reevaluation，無法唯一解決才 `CREATE_NEW`。 |
| D11 | Decision 與 evaluation failure 是互斥 result variants；expected domain failure structured return，bootstrap／programming failure不偽裝成 business decision。 |
| D12 | 每個 Decision 攜帶 exact `policy_id`／`policy_version`；historical version 缺失 Fail Closed，不得 fallback 到 latest。 |

---

# 1. 模組責任與邊界

## 1.1 MUST

Engine 必須：

- resolve Event policy；
- classify `STRONG`／`KNOWN_WEAK`／`UNKNOWN`；
- resolve `correlation_family`；
- extract 並 validate Strong identity；
- 建立 normalized fingerprint；
- 過濾 lifecycle eligibility；
- 過濾 Correlation Window；
- 評估 family compatibility；
- 評估 evidence-tier compatibility；
- 判斷 zero／one／multiple candidates；
- 產生 deterministic `CorrelationDecision`；
- 對預期中的 contract／state error 產生 structured `CorrelationEvaluationError`。

## 1.2 MUST NOT

Engine 不得：

- persist Pending 或管理 Pending timer／Pending Grace；
- persist Processed／Dedup 或 Evaluation Failure State；
- 寫入 Correlation State Store；
- create／mutate Incident 或執行 lifecycle transition；
- persist Shadow；
- poll EventStore 或管理 runtime loop；
- retry failure、sleep 或 rebuild View；
- 實作 operator UI 或 RCA；
- 直接讀 YAML／config file；
- mutate Event 或 `IncidentCorrelationView`；
- 使用 Scenario context、Generator state 或 Validator expected answer。

## 1.3 Downstream interface boundary

| SPEC | 保留責任 |
|---|---|
| SPEC-007 | Pending persistence、Pending Grace、Processed／Dedup、Evaluation Failure State、blocked／retryability、restart recovery；保存 exact policy reference。 |
| SPEC-008 | Incident creation、authoritative mutation、evidence attach、severity escalation、`correlation_context` mutation與audit。若需 repair contract，必須經 Incident authority、audit、preserve referential integrity，且不得由 Runtime／AI 自動 destructive repair。 |
| SPEC-009 | Incident lifecycle、Assignment、Resolution Evidence、Review、Recovery verification與recurrence。 |
| SPEC-010 | Shadow／Unclassified persistence、Shadow reason、`review_status`與`policy_version`。 |
| SPEC-011 | Runtime orchestration、Event／Incident View 組裝、failure retry、derived View rebuild、operator-visible blocked correlation、reevaluation與downstream E2E。 |

此處只定義 interface boundary，不指定 SPEC-007～011 的 persistence technology、file layout、DB engine或完整 workflow implementation。PoC 不要求完整 Admin Repair Tool。

---

# 2. Domain Model 與 Main API

## 2.1 Main logical API

```text
AlertCorrelationPolicyEngine.evaluate(
    event,
    incident_views: Sequence[IncidentCorrelationView],
    context: CorrelationEvaluationContext,
) -> CorrelationEvaluationResult
```

Implementation 可依 repository 慣例使用 dataclass、enum、protocol、ABC 或 value object，但本 SPEC 不鎖定 physical file layout。`evaluate` 的輸入集合必須視為 read-only。

## 2.2 Logical types

建議 logical structures：

- `AlertCorrelationPolicyEngine`
- `CorrelationEngineConfig`
- `PolicyRegistry`
- `CorrelationPolicy`
- `IdentityExtractor`
- `IncidentCorrelationView`
- `NormalizedFingerprint`
- `CorrelationEvaluationContext`
- `CorrelationEvaluationResult`
- `CorrelationDecision`
- `CorrelationEvaluationError`

正式 enum／closed set：

```text
EvidenceClass = STRONG | KNOWN_WEAK | UNKNOWN
AnchorStrength = STRONG | WEAK
AnchorTransition = NONE | WEAK_TO_STRONG
EvaluationPhase = INITIAL | PENDING_RECHECK | PENDING_EXPIRED
DecisionType = ATTACH_EXISTING | CREATE_NEW | ENTER_PENDING | ROUTE_SHADOW
```

`anchor_transition` 可採 nullable 或 `NONE`，但 `WEAK_TO_STRONG` 只能搭配 `ATTACH_EXISTING`。

## 2.3 `CorrelationEngineConfig`

最低欄位：

```text
correlation_window_seconds: positive number = 120
```

必須驗證為正數且型別合法；invalid config 必須在 bootstrap Fail Fast，不得 silent fallback。Pending Grace `30s` 屬 SPEC-007，不得放入本 Engine 的時間管理。

`CorrelationEngineConfig` 由未來 SPEC-011／bootstrap 讀取、驗證後 inject。Engine 不直接讀 YAML。PoC 不支援 hot reload；Registry 與 Config 在 controlled startup load 後於 process lifetime immutable。

以下屬 semantic contract，不能由一般 runtime config 覆寫：Evidence Class、Correlation Family、fingerprint identity fields、Strong／Weak precedence、lifecycle eligibility、UNKNOWN routing、forward-only timestamp rule，以及 Strong／Weak decision semantics。

Config 不得包含 `scenario_id`、expected incident／service、Validator expected answer、Generator current scenario等 answer leakage。

## 2.4 `CorrelationEvaluationContext`

最低欄位：

```text
evaluation_phase: EvaluationPhase
policy_id: nullable string
policy_version: nullable string
```

`INITIAL` 通常不指定 historical reference，由 Registry 對 current `event_type` deterministic lookup。`PENDING_RECHECK` 與 `PENDING_EXPIRED` 必須提供 SPEC-007 保存的 exact `policy_id`／`policy_version`；缺少、矛盾或與 Event policy 不一致時回 `INCONSISTENT_CORRELATION_CONTEXT`。exact version 不存在時回 `POLICY_VERSION_UNAVAILABLE`，不得改用 latest。

Engine 不讀 wall clock，也不判斷 Pending 是否到期；`PENDING_EXPIRED` 是 SPEC-007 已完成 timeout 判斷後傳入的事實。

## 2.5 `IncidentCorrelationView`

Engine 不接收完整 Incident。最低 conceptual fields：

```text
incident_id: string
status: OPEN | ASSIGNED | IN_PROGRESS | AWAITING_REVIEW | CLOSED
last_correlated_at: timestamp
correlation_family: CorrelationFamily
anchor_strength: STRONG | WEAK
normalized_fingerprint: nullable NormalizedFingerprint
anchor_event_type: nullable string
```

`anchor_event_type` 只有 matching／validation 真正需要時才使用。View 不得暴露 assignee、reviewer、完整 RCA artifact、Jira／Discord reference、Resolution Evidence、complete audit trail或其他 operation-only fields。Engine 只能讀取 View，不能 mutate authoritative Incident。

## 2.6 `NormalizedFingerprint`

`NormalizedFingerprint` 是 immutable semantic value object，概念包含：

```text
event_type = oom_crash_detected
identity = {
  service_name: payment-api
}
```

`identity` 必須是 structured named fields，具 deterministic equality 與安全 comparison；不得依靠字串 delimiter concatenation。欄位順序不得影響 equality，欄位名稱與值則必須參與 equality。

Weak standalone Incident 可保留已知 `correlation_family`，並使用 `anchor_strength=WEAK`、`normalized_fingerprint=null`、`anchor_event_id=null`，直到 Late Strong-Anchor Promotion。

---

# 3. Policy Registry 與 Policy Matrix

## 3.1 Versioned typed Registry

每個 `CorrelationPolicy` 概念上至少包含：

```text
policy_id
policy_version
event_type
evidence_class
correlation_family
identity_extractor: nullable IdentityExtractor
```

Registry 必須支援：

- 以 `event_type` deterministic resolve current policy；
- 以 `policy_id + policy_version` exact lookup historical policy；
- 可擴充但 runtime read-only／immutable semantics；
- definition 與 lookup 結果不受 iteration order 影響。

新增 Event Type、變更 Evidence Class／Family、修改 identity fields 或 fingerprint semantics，均屬正式 Engineering Contract change，不得視為營運人員可隨意修改的 runtime YAML。

Bootstrap 遇到 duplicate current `event_type`、duplicate `policy_id + policy_version`、malformed definition、缺少必要 extractor或 UNKNOWN 錯配 extractor時，必須 Fail Fast。這些不是單筆 Event evaluation failure。

## 3.2 Correlation Family

PoC v1.0 closed set：

```text
ATTACK_SOURCE
CROSS_SERVICE_LATENCY
MEMORY_OOM
EXTERNAL_DEPENDENCY
DOWNSTREAM_CASCADE
RATE_LIMIT
UNKNOWN
```

Runtime 使用 semantic `correlation_family`，不得使用 `scenario_id`、S1～S6 current scenario、Generator state、Validator expected answer或 Mock runtime current scenario作為 policy key 或 decision source。S1～S6 只用於本文件 traceability 與 validation context。

## 3.3 PoC v1.0 Policy Matrix

Policy ID 命名是本 SPEC 的 stable identifier；所有初始版本為 `1.0`。

| Trace | `policy_id` | `event_type` | Evidence | `correlation_family` | Strong identity／結果 |
|---|---|---|---|---|---|
| S1 | `POLICY-BRUTE-FORCE-DETECTED` | `brute_force_detected` | `STRONG` | `ATTACK_SOURCE` | `source_ip`；fingerprint=`event_type + source_ip` |
| S2 | `POLICY-CROSS-SERVICE-FAILURE` | `cross_service_failure` | `STRONG` | `CROSS_SERVICE_LATENCY` | `trace_id`；fingerprint=`event_type + trace_id` |
| S2 | `POLICY-HIGH-LATENCY-DETECTED` | `high_latency_detected` | `KNOWN_WEAK` | `CROSS_SERVICE_LATENCY` | 無 Strong identity extractor |
| S3 | `POLICY-OOM-CRASH-DETECTED` | `oom_crash_detected` | `STRONG` | `MEMORY_OOM` | `service_name`；fingerprint=`event_type + service_name` |
| S3 | `POLICY-HIGH-MEMORY-DETECTED` | `high_memory_detected` | `KNOWN_WEAK` | `MEMORY_OOM` | 無 Strong identity extractor |
| S4 | `POLICY-EXTERNAL-DEPENDENCY-FAILURE` | `external_dependency_failure` | `STRONG` | `EXTERNAL_DEPENDENCY` | `external_service`；fingerprint=`event_type + external_service` |
| S5 | `POLICY-DOWNSTREAM-CASCADE-FAILURE` | `downstream_cascade_failure` | `STRONG` | `DOWNSTREAM_CASCADE` | `downstream_service`；fingerprint=`event_type + downstream_service` |
| S6 | `POLICY-RATE-LIMIT-STORM` | `rate_limit_storm` | `STRONG` | `RATE_LIMIT` | `triggered_features.target_service`；fingerprint=`event_type + triggered_features.target_service` |
| S6 | `POLICY-REQUEST-SPIKE-DETECTED` | `request_spike_detected` | `KNOWN_WEAK` | `RATE_LIMIT` | 無 Strong identity extractor |
| Unknown | `POLICY-GENERAL-LOG-ANOMALY` | `general_log_anomaly` | `UNKNOWN` | `UNKNOWN` | `ROUTE_SHADOW` |
| Unknown | `POLICY-GENERAL-METRICS-ANOMALY` | `general_metrics_anomaly` | `UNKNOWN` | `UNKNOWN` | `ROUTE_SHADOW` |

S3 明確依賴已修復並完成 E2E validation 的 upstream contract：`oom_crash_detected.service_name` 必須是 actual OOM-origin service。不得重新使用 `unique_services[0]`、scenario metadata或generator state。

S6 必須從 nested `event.triggered_features.target_service` 取值，不得假設存在 top-level `target_service`。

`general_log_anomaly` 與 `general_metrics_anomaly` 是明確註冊的合法 UNKNOWN policies。完全未註冊 Event Type 不等於 UNKNOWN。

## 3.4 `IdentityExtractor`

Policy 擁有「如何取得 identity」的知識。Engine core 只呼叫 typed extractor，不應用大量 event-specific／scenario-specific `if/elif` 硬編欄位位置。

Extractor 可做 contract-safe normalization：

- 接受 contract 指定的 string field；
- trim surrounding whitespace；
- reject missing、null、non-string或 trim 後 empty string。

Extractor 不得做 fuzzy matching、alias guessing、無條件 lowercase、lossy normalization或 identity inference，除非未來正式 Policy Contract 明確允許。

Missing path／null 回 `MISSING_REQUIRED_IDENTITY`；存在但型別錯誤或 normalization 後非法回 `INVALID_IDENTITY_VALUE`。Registered Strong Event 的 identity failure 必須 Fail Closed，不得轉成 `CREATE_NEW`、`ENTER_PENDING` 或 `ROUTE_SHADOW`。Known Weak 本來就沒有 Strong instance identity，並非 error。

---

# 4. Candidate Eligibility 與 Staged Validation

## 4.1 Common gates 與固定順序

Candidate evaluation 共用以下順序：

1. Lifecycle eligibility
2. Correlation Window eligibility
3. Correlation Family compatibility
4. Evidence-specific identity compatibility

順序是 observable contract：先排除 closed／out-of-window View，再對可能影響 uniqueness 的 context 做嚴格 validation。

## 4.2 Lifecycle

Correlation-open：`OPEN`、`ASSIGNED`、`IN_PROGRESS`。

Correlation-closed：`AWAITING_REVIEW`、`CLOSED`。

`AWAITING_REVIEW` 不得參與 normal correlation；`CLOSED` 永不成為 candidate。Engine 不因此改變任何 lifecycle state。

## 4.3 Correlation Window

PoC default `120 seconds`，規則為：

```text
delta = event.detected_at - incident.last_correlated_at
eligible iff 0 <= delta <= correlation_window
```

`0` 與恰好 `120s` 在 default config 下均 eligible；超過 window ineligible。PoC v1.0 採 forward-only，negative delta 必須 ineligible，不能因數值小於 120 而接受。未來 out-of-order telemetry 必須另立 bounded late-arrival policy。

Event envelope 必須有可解析且符合 PRD-002 contract 的 `event_id`、`event_type`、`detected_at` 等 evaluation 必要欄位；無法可靠計算則回 `INVALID_EVENT_ENVELOPE`。

## 4.4 Staged `IncidentCorrelationView` validation

### Stage 1 — Minimal Eligibility Validation

每個 View 先驗證：

- `incident_id` 是合法非空 reference；
- `status` 是已知 lifecycle value；
- `last_correlated_at` 是可與 Event timestamp 安全比較的 timestamp。

若 lifecycle／time eligibility 無法安全判斷，whole evaluation 回 `INVALID_INCIDENT_VIEW`。不得 silently skip。

### Stage 2 — Candidate Context Validation

只有 lifecycle + time eligible 的 View 才驗證：

- `correlation_family` 是合法值；
- `anchor_strength` 是合法值；
- `normalized_fingerprint` 與 anchor strength／incoming evidence 所需 matching contract 一致；
- 必要時驗證 nullable `anchor_event_type`。

Eligible View 的 malformed context 可能改變 candidate uniqueness，必須 whole evaluation Fail Closed。已確定為 `CLOSED` 或 `AWAITING_REVIEW` 且 Stage 1 合法的 View，其無關 fingerprint／context 歷史不完整不得阻塞此次 evaluation。

> 任何可能影響 candidate uniqueness 的 malformed view 不能被偷偷略過；與此次 evaluation 無關的 correlation-closed historical corruption也不應無限阻塞系統。

---

# 5. Evaluation Algorithm

## 5.1 共用前置流程

每次 `evaluate` 必須依序：

1. 驗證 Event envelope 的 evaluation-required contract。
2. 依 `EvaluationPhase` resolve current 或 exact historical policy。
3. 驗證 Context 與 resolved policy 一致性。
4. 若 Evidence=`UNKNOWN`，回 `ROUTE_SHADOW`，不做 operational candidate matching。
5. 若 Evidence=`STRONG`，執行 extractor 並建立 `NormalizedFingerprint`；任何 identity error立即 Failure。
6. 對所有 Views 執行 staged validation 與 common gates。
7. 依 evidence-specific algorithm 計算 candidate；不得使用輸入順序 tie-break。
8. 依 phase 產生唯一 deterministic Decision。

Policy 未註冊時在第 2 步回 `POLICY_NOT_REGISTERED`；不能送 Shadow。

## 5.2 Strong Event candidate tiering

### Tier 1 — Exact Strong Identity Match

從 lifecycle eligible、time eligible、same-family Views 中找：

- `anchor_strength=STRONG`；
- `normalized_fingerprint` 與 incoming fingerprint exact match。

結果：

- exactly 1：`ATTACH_EXISTING`；
- more than 1：`ENTER_PENDING`／`MULTIPLE_COMPATIBLE_CANDIDATES`；
- zero：才進 Tier 2。

Tier 1 若 ambiguous，禁止 fallback 至 Weak candidates。

### Tier 2 — Weak Standalone Promotion Candidate

只有 Tier 1=0 才搜尋 same-family、lifecycle eligible、time eligible、`anchor_strength=WEAK` 且符合 Weak standalone invariant（沒有 Strong fingerprint／anchor）的 View。

結果：

- exactly 1：`ATTACH_EXISTING`，`anchor_transition=WEAK_TO_STRONG`；
- more than 1：`ENTER_PENDING`／`MULTIPLE_COMPATIBLE_CANDIDATES`；
- zero：`CREATE_NEW`。

> **Strong exact identity > Weak contextual compatibility。**

Late Promotion 的關鍵不是 Strong 單純權重較高，而是 incoming Strong evidence 帶入 Weak Incident 原本缺少的 instance-level identity，使 identity certainty 提升。SPEC-008 才負責保留 `incident_id`、attach evidence、更新 anchor／fingerprint／context並audit；SPEC-006 只回傳 transition intent。

## 5.3 Known Weak candidate safety

Known Weak 沒有 instance-level identity，因此其 compatible set 是所有 same-family、lifecycle eligible、time eligible且 Stage 2 合法的 Views；Strong-origin 與 Weak-origin candidates 平等參與，不使用 Strong-candidate priority。

結果：

- exactly 1：`ATTACH_EXISTING`；唯一 Weak standalone Incident亦可 attach；
- zero：`ENTER_PENDING`／`NO_COMPATIBLE_CANDIDATE`；
- more than 1：`ENTER_PENDING`／`MULTIPLE_COMPATIBLE_CANDIDATES`。

> 只有 incoming evidence 本身足以支持更強 identity match 時，才允許 evidence-tier priority。不能只因某個 Incident 自身 evidence 較強，就把缺乏 identity 的 Weak Event 優先 attach 給它。

因此同時存在 Strong-origin 與 Weak-origin compatible candidates 時，結果是 ambiguity，不得猜測。

## 5.4 UNKNOWN

Registered UNKNOWN Event：

- 不進 Known Weak Pending；
- PoC v1.0 不做 operational candidate matching；
- 回 `ROUTE_SHADOW`，建議 reason=`INSUFFICIENT_OPERATIONAL_IDENTITY`。

Shadow 是合法但目前缺乏安全 operational identity 的 learning path，不是 malformed／unsupported input 的垃圾桶。Shadow persistence 屬 SPEC-010。

## 5.5 Evaluation phase 與 Pending expiration

### `INITIAL`

- Strong 依 tiering 立即 attach／create／pending。
- Known Weak 0 candidate 或 ambiguity 時進 Pending。

### `PENDING_RECHECK`

Pending Grace 尚未到期，必須使用當下 Views 完整重算：

- 出現唯一 candidate：`ATTACH_EXISTING`；Strong 依 tiering保留 promotion semantics。
- 仍無唯一 resolution：`ENTER_PENDING`，reason 依當次 0 或 >1 結果。

### `PENDING_EXPIRED`

SPEC-007 已判定 Grace 到期，Engine 仍必須用當下 Views做最後一次完整 reevaluation：

- exactly 1 compatible resolution：`ATTACH_EXISTING`；
- 仍無唯一 resolution：`CREATE_NEW`。

不得因 phase 已到期就無條件 `CREATE_NEW`。Strong 因 multiple exact／promotion candidates Pending，到期後若仍 >1 或已變成 0，建立 Strong standalone Incident；Known Weak unresolved expiry 建立 Weak standalone Incident。

此策略採 temporary over-segmentation，優先避免 false correlation 與 operational suppression。SPEC-006 不計時、不延長 Grace、不保存 candidate snapshot。

---

# 6. Decision 與 Result Contract

## 6.1 `CorrelationDecision`

PoC v1.0 top-level business Decision 只有：

```text
ATTACH_EXISTING
CREATE_NEW
ENTER_PENDING
ROUTE_SHADOW
```

不得新增 `LATE_PROMOTE` 為第五種。Late Promotion 表示 `ATTACH_EXISTING + anchor_transition=WEAK_TO_STRONG`。

Decision minimum conceptual fields：

```text
decision_type
policy_id
policy_version
correlation_family
reason_code
target_incident_id: nullable
normalized_fingerprint: nullable
anchor_strength: nullable
anchor_transition
diagnostics
```

`CorrelationDecision.anchor_strength` 表示此 Decision 若作用於 Incident，預期 resulting／effective Incident anchor strength；它不是 incoming Event evidence class 的單純複製。`ATTACH_EXISTING` 與 `CREATE_NEW` 必須填值；`ENTER_PENDING` 尚無 resulting Incident，`ROUTE_SHADOW` 不建立 Incident，因此兩者為 `null`／not applicable。

`diagnostics` 只可包含 lightweight、non-authoritative資料，例如 `candidate_count`。Candidate list 不得被當成未來 authoritative snapshot；Pending reevaluation 必須使用最新 View 重算。

## 6.2 Decision Reason Code

PoC v1.0 `reason_code` 是 stable、machine-readable closed set：

```text
EXACT_STRONG_IDENTITY_MATCH
UNIQUE_COMPATIBLE_CANDIDATE
WEAK_TO_STRONG_PROMOTION
NO_COMPATIBLE_CANDIDATE
MULTIPLE_COMPATIBLE_CANDIDATES
PENDING_EXPIRED_UNRESOLVED
INSUFFICIENT_OPERATIONAL_IDENTITY
```

| Reason Code | 適用語意 |
|---|---|
| `EXACT_STRONG_IDENTITY_MATCH` | Strong Event 以 Tier 1 exact fingerprint 唯一匹配既有 Strong Incident。 |
| `UNIQUE_COMPATIBLE_CANDIDATE` | Known Weak Event 唯一匹配 compatible Strong或Weak Incident。 |
| `WEAK_TO_STRONG_PROMOTION` | Strong Event 以 Tier 2 唯一匹配 Weak standalone Incident並要求 promotion。 |
| `NO_COMPATIBLE_CANDIDATE` | Strong Event 無 candidate而建立新 Incident，或尚未到期的 evaluation 無 candidate而進／維持 Pending；由 `decision_type` 區分結果。 |
| `MULTIPLE_COMPATIBLE_CANDIDATES` | 當次 evaluation 有多個同 tier／compatible candidates而進／維持 Pending。 |
| `PENDING_EXPIRED_UNRESOLVED` | 最後 reevaluation 後仍無唯一 resolution，建立 standalone Incident。 |
| `INSUFFICIENT_OPERATIONAL_IDENTITY` | Registered UNKNOWN Event 路由 Shadow。 |

Reason Code 必須由相同 decision inputs deterministic 產生；consumer 不得依賴 free-text message parsing。Reason Code 不包含 Scenario ID／S1～S6 answer leakage，不形成額外 Decision hierarchy，也不改變第 5 章演算法。

## 6.3 Decision invariants

| Decision context | 必要 invariant |
|---|---|
| Strong Event attach existing Strong Incident | `target_incident_id` present、`anchor_strength=STRONG`、`anchor_transition=NONE`、reason=`EXACT_STRONG_IDENTITY_MATCH`。 |
| Known Weak Event attach existing Strong Incident | `target_incident_id` present、`anchor_strength=STRONG`、`anchor_transition=NONE`、reason=`UNIQUE_COMPATIBLE_CANDIDATE`；incoming Weak evidence不得使 Incident anchor降級。 |
| Known Weak Event attach existing Weak Incident | `target_incident_id` present、`anchor_strength=WEAK`、`anchor_transition=NONE`、reason=`UNIQUE_COMPATIBLE_CANDIDATE`。 |
| Strong Event Late Promotion至Weak Incident | `target_incident_id` present、`anchor_strength=STRONG`、`anchor_transition=WEAK_TO_STRONG`、reason=`WEAK_TO_STRONG_PROMOTION`。 |
| `CREATE_NEW` Strong | `target_incident_id=null`、`anchor_strength=STRONG`、normalized fingerprint present；一般無 candidate時 reason=`NO_COMPATIBLE_CANDIDATE`，Pending expiry unresolved時 reason=`PENDING_EXPIRED_UNRESOLVED`。 |
| `CREATE_NEW` Weak | `target_incident_id=null`、`anchor_strength=WEAK`、family retained、Strong fingerprint absent、reason=`PENDING_EXPIRED_UNRESOLVED`。`anchor_event_id` 由 SPEC-008 依 PRD-003 Weak-origin semantics決定。 |
| `ENTER_PENDING` | `target_incident_id=null`、`anchor_strength=null`；reason為 `NO_COMPATIBLE_CANDIDATE` 或 `MULTIPLE_COMPATIBLE_CANDIDATES`。 |
| `ROUTE_SHADOW` | Evidence必須是 registered `UNKNOWN`；`target_incident_id=null`、fingerprint absent、`anchor_strength=null`、reason=`INSUFFICIENT_OPERATIONAL_IDENTITY`。 |

所有 Decision 都必須帶 resolved exact `policy_id`／`policy_version`。

## 6.4 `CorrelationEvaluationResult`

Result 是互斥 sum type：

```text
Success(CorrelationDecision)
Failure(CorrelationEvaluationError)
```

Failure 不是第五種 Decision；Result 不得同時含 Decision 與 Error。

---

# 7. Error Handling 與 Fail-Closed Contract

## 7.1 Evaluation error taxonomy

PoC 至少包含：

| Error Code | 使用時機 |
|---|---|
| `INVALID_EVENT_ENVELOPE` | Event 缺 evaluation-required envelope field、timestamp不可解析或違反必要型別。 |
| `POLICY_NOT_REGISTERED` | `event_type` 沒有 current policy。 |
| `POLICY_VERSION_UNAVAILABLE` | Pending recovery要求的 exact historical policy不存在。 |
| `MISSING_REQUIRED_IDENTITY` | Strong required identity path缺少或為 null。 |
| `INVALID_IDENTITY_VALUE` | identity 型別錯誤或 trim 後空值等非法值。 |
| `INVALID_INCIDENT_VIEW` | View 無法通過 staged validation。 |
| `INCONSISTENT_CORRELATION_CONTEXT` | phase／policy reference缺失、矛盾或與 Event policy不一致。 |

Failure diagnostics 採 reference-oriented：

```text
error_code
event_id
event_type
policy_id: nullable
policy_version: nullable
incident_id: nullable
field_path: nullable
message
```

不得 duplicate full Event、full Incident或 raw logs。`message` 應穩定描述原因，但 consumer 不得依賴 message parsing；automation 使用 `error_code` 與 references。

## 7.2 Domain failure 與 bootstrap failure

Expected domain evaluation failures，例如 missing identity、unregistered policy、invalid eligible Incident context與 unavailable historical policy version，回 `CorrelationEvaluationResult.Failure`。

Bootstrap／programming contract failures，例如 invalid `correlation_window_seconds`、duplicate registry definition、malformed registry configuration，必須 startup Fail Fast，不得偽裝成某筆 Event evaluation failure。

## 7.3 Unexpected internal exception

Unexpected `AttributeError`、`TypeError`、internal library bug等 programming error不得被 `catch Exception -> INVALID_EVENT` 掩蓋。它們應 propagate 至 SPEC-011 runtime boundary，由該層負責 structured logging、operator visibility與 safe runtime boundary。

## 7.4 Fail Closed

- Registered Strong 缺 identity：Failure，不 create／pending／shadow／猜測。
- Known Weak 缺 Strong identity：不是 error。
- Unregistered Event Type：`POLICY_NOT_REGISTERED`，不是 Shadow。
- 任何可能影響 uniqueness 的 malformed eligible View：whole evaluation Failure，不 silently skip。
- Historical exact policy unavailable：Failure，不 fallback latest。

SPEC-006 只回 failure，不 persist、retry、repair、quarantine或 sleep。SPEC-007 定義 persisted Failure State、blocked／retryability與 dedup relationship；SPEC-011 定義 retry、View rebuild、operator-visible failure與 reevaluation；SPEC-008 如需 authoritative repair，只能在 authority／audit／referential-integrity constraints下定義，且不得自動 destructive repair。

---

# 8. Determinism、Purity 與 Versioning

## 8.1 Determinism

對 equivalent inputs，`evaluate` 必須回 equivalent result，且不得由以下因素改變：

- View iterable 原始順序；
- process-local wall clock；
- filesystem／YAML狀態；
- EventStore／Incident Store查詢時序；
- random value；
- Scenario／Generator／Validator context。

Engine 不得 mutate輸入、persist、poll、sleep或 retry。若 diagnostics 需要排序，必須採明確 stable rule；但不得以排序結果猜測 target。

## 8.2 Policy version lifecycle

Pending 首次 Decision 攜帶 exact policy reference，SPEC-007 保存。Recovery／reevaluation 必須使用相同 `policy_id + policy_version`。Registry 可同時保留 historical versions，但每個 `event_type` 只能有一個 current mapping。

PoC 不支援 Policy hot reload。Future 可定義 version compatibility、hot reload、controlled migration與 production Registry management；在正式 contract 出現前不得自動 migration。

---

# 9. Acceptance Criteria

## AC-006-A — Policy Resolution

- S1～S6、Known Weak與 explicit UNKNOWN 均 resolve 正確 typed policy。
- Registered UNKNOWN 回 `ROUTE_SHADOW`；unregistered Event回 `POLICY_NOT_REGISTERED`。
- `INITIAL` resolve current policy；Pending phases resolve exact policy version。
- historical version不存在回 `POLICY_VERSION_UNAVAILABLE`，不得 fallback latest。

## AC-006-B — Strong Fingerprint Correctness

- S1～S6 Strong extractor各自使用正式 identity field並產生 deterministic structured fingerprint。
- S3 使用 actual OOM-origin `event.service_name`。
- S6 使用 nested `event.triggered_features.target_service`，不讀假想 top-level field。
- missing、null、empty與 invalid type依 taxonomy回 structured failure。

## AC-006-C — Evidence-tier Candidate Selection

- exact Strong candidate優先於 Weak contextual candidate。
- multiple exact Strong產生 ambiguity，且不得 fallback至 Weak tier。
- Tier 1=0時，unique Weak standalone產生 `ATTACH_EXISTING + WEAK_TO_STRONG`。
- multiple Weak promotion candidates進 Pending；沒有任何 candidate時 Strong `CREATE_NEW`。
- Promotion rationale明確基於 incoming Strong 提供 instance-level identity。
- Strong exact attach、Strong create與Late Promotion分別產生規定的 stable `reason_code`，且 `anchor_strength` 表示 resulting／effective Incident anchor。

## AC-006-D — Known Weak Ambiguity Safety

- Weak candidate selection不依 candidate 自身 anchor Strong／Weak給 priority。
- unique Strong candidate可 attach；unique Weak candidate可 attach。
- Strong + Weak 同時 compatible時進 Pending。
- 0／1／>1 candidate分別符合 Pending／Attach／Pending，ambiguity never guess。
- Known Weak attach Strong Incident時 effective `anchor_strength` 維持 `STRONG`；attach Weak Incident時維持 `WEAK`，兩者皆不得產生 anchor transition。

## AC-006-E — Pending Phase Behavior

- `INITIAL`、`PENDING_RECHECK`、`PENDING_EXPIRED` 均有明確行為。
- Recheck 使用最新 View重算；未唯一且未到期維持 Pending。
- Expiry先做最後 reevaluation；candidate在 expiry前或當下變唯一則 attach。
- Expiry仍無唯一 resolution才 standalone create，Strong／Weak anchor semantics正確。
- Pending decisions的 `anchor_strength=null`；expiry unresolved standalone create使用 `PENDING_EXPIRED_UNRESOLVED`。

## AC-006-F — Lifecycle／Window／Family Compatibility

- `OPEN`、`ASSIGNED`、`IN_PROGRESS` eligible。
- `AWAITING_REVIEW`、`CLOSED` ineligible。
- forward-only window符合 `0 <= delta <= configured window`；驗證 0、120s、超界與 negative delta。
- family mismatch永不成為 candidate。

## AC-006-G — Invalid／Unknown Evidence Safety

- Registered UNKNOWN只回 Shadow；unregistered回 Failure。
- Strong identity錯誤 Fail Closed。
- staged View validation只深入驗證 lifecycle/time eligible context。
- malformed eligible candidate使 whole evaluation Failure；closed historical非相關 corruption不阻塞。
- Shadow decision使用 `INSUFFICIENT_OPERATIONAL_IDENTITY` 且 `anchor_strength=null`。

## AC-006-H — Determinism／Module Boundary／Versioned Policy

- repeated equivalent evaluation產生 equivalent result，且不 mutate Event／Views。
- 無 persistence、store、wall clock、YAML、runtime loop或 Scenario coupling。
- exact policy version semantics成立。
- alternate valid correlation window生效；invalid config bootstrap Fail Fast。

---

# 10. Implementation Test Contract

## 10.1 Required coverage

Implementation Phase MUST 至少涵蓋：

- S1～S6 Policy Registry mapping；
- S1～S6 Strong fingerprint extraction；
- Strong exact candidate priority；
- multiple exact Strong ambiguity與 no fallback；
- unique Weak Late Promotion；
- multiple Weak promotion ambiguity；
- Known Weak + unique Strong candidate；
- Known Weak + unique Weak candidate；
- Known Weak + Strong + Weak candidates → Pending；
- Known Weak 0／1／>1 candidate；
- `INITIAL`／`PENDING_RECHECK`／`PENDING_EXPIRED`；
- expired但 candidate變唯一 → attach；
- `OPEN`／`ASSIGNED`／`IN_PROGRESS`；
- `AWAITING_REVIEW`／`CLOSED`；
- default 120s window的 0、120s與超界 boundary；
- negative delta exclusion；
- family mismatch；
- registered UNKNOWN；
- unregistered Event Type；
- missing Strong identity與 invalid identity；
- staged View validation，包括 closed historical context不阻塞與 eligible malformed Fail Closed；
- historical exact policy version與 unavailable version；
- alternate valid window config；
- invalid config bootstrap failure；
- deterministic repeated evaluation；
- Decision `anchor_strength` resulting／effective semantics與 nullable cases；
- 所有核心 Decision／phase組合的 stable `reason_code` mapping與 closed-set enforcement；
- no Event／View mutation。

建議使用 parameterized tests；不得用固定測試數量取代 contract coverage。

## 10.2 Test layers

### MUST — Targeted unit tests

覆蓋 extractor、fingerprint equality、Registry bootstrap／lookup、common gates、tiering、phase、effective／nullable `anchor_strength`、Decision Reason Code closed set與mapping、decision invariant及 error taxonomy。

### MUST — Contract／integration-lite tests

必須使用 repository 目前真正的 PRD-002 Event model／production Event construction path，至少驗證：

- S3 `oom_crash_detected.service_name`；
- S6 `rate_limit_storm.triggered_features.target_service`。

不得只以 test-specific DTO證明 contract。

### MUST — Full repository regression

```text
python -m pytest -q
```

若 Repository 未來另有正式 equivalent command，使用該 command並記錄結果。

### NOT REQUIRED YET — Docker S1～S6 downstream Correlation E2E

SPEC-007～011 尚未實作，完整 Correlation Runtime E2E 留待後續 integration SPEC；不得將未執行誤報為通過。

---

# 11. Future Extensibility

本版保留但不實作：

- additional evidence tiers；
- topology identity；
- transaction-context identity；
- new correlation families；
- bounded late-arrival telemetry；
- controlled policy evolution與 version compatibility；
- hot reload與 controlled migration；
- production Policy Registry management；
- richer runtime config source；
- production administrative repair tooling。

新增 Policy／`IdentityExtractor` 應透過 Registry extension，盡量不重寫 shared evaluation pipeline。任何新增 semantic contract仍須正式 Engineering Contract change。

---

# 12. Out of Scope

- Correlation State persistence；
- Pending timer與 Pending Grace implementation；
- Processed／Dedup ledger；
- Evaluation Failure persistence；
- Incident creation／mutation；
- Incident lifecycle；
- Shadow persistence；
- RCA implementation；
- runtime orchestration與 EventStore polling；
- operator Dashboard；
- full Admin Repair Tool；
- Docker correlation E2E；
- physical database selection。

---

# 13. Implementation Handoff 與 Governance

本 SPEC implementation 預計由 **富裕** 負責。此 owner 資訊不改變工程契約：implementation 不得依賴特定個人的 local environment、測試習慣或手動 workaround。

Implementation 完成前，文件 Status 維持 `Approved — Implementation Pending`。更新為 `Implemented` 前必須具備：

1. 符合本 SPEC 的 implementation；
2. targeted unit tests通過；
3. 使用真實 PRD-002 Event model的 contract／integration-lite tests通過；
4. full repository regression通過；
5. 由 PM／reviewer確認 implementation evidence。

本 SPEC 不授權 implementation 階段之外的 upstream Event Schema修改、persistence選型或 downstream workflow提前實作。

---

# 14. PM Review Checklist

- [ ] 文件標示 `Version 1.0`、`Approved — Implementation Pending`、`2026-08-30`，並明示 SPEC Approved ≠ Implemented。
- [ ] D1～D12 全部成為 normative engineering decisions。
- [ ] S1～S6 Policy Matrix、Known Weak與 explicit UNKNOWN完整。
- [ ] Runtime不使用 Scenario ID／Generator／Validator answer leakage。
- [ ] Strong exact tier、Weak promotion tier與 no-fallback ambiguity完整。
- [ ] Promotion 明示 incoming Strong提供 instance-level identity。
- [ ] Known Weak不因 candidate本身較Strong而優先 attach。
- [ ] `INITIAL`／`PENDING_RECHECK`／`PENDING_EXPIRED` 與 final reevaluation完整。
- [ ] exact policy version與 config bootstrap contract完整。
- [ ] Decision／Failure分離、Fail Closed與 staged View validation完整。
- [ ] SPEC-007～011界線清楚且未鎖定 persistence technology。
- [ ] AC-006-A～H與 required test layers完整。
- [ ] implementation owner記錄為富裕，但契約保持 implementation-neutral。
