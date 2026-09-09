# SPEC-008 — Incident Store & Incident Manager Core

## Software Design Specification v1.1

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-008 |
| Document Name | Incident Store & Incident Manager Core |
| Version | 1.1 |
| Status | Approved — Implementation Pending |
| Date | 2026-09-09 |
| Requirement Authority | PRD-003 v1.0 Final |
| Upstream Event Contract | PRD-002 v1.5 |
| Upstream Correlation Contract | SPEC-006 v1.0 Implemented |
| Upstream Correlation State Contract | SPEC-007 v1.0 Implemented |
| Implementation Owner | 富裕 |

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-09-08 | Initial Draft；定義 authoritative Incident persistence、correlation-driven create／attach、Event ownership、idempotent operation replay、Late Strong-Anchor Promotion、audit、read boundary、SQLite PoC persistence與 recovery handoff工程契約。 |
| 1.0 | 2026-09-08 | PM Review #1、Revision Round #1與PM Review #2完成；D1～D12、008-R1、008-R2、typed error contract、RCA initial state、replay／crash consistency、Acceptance Criteria與cross-SPEC boundaries完成final review。Status更新為 Approved — Implementation Pending；Engineering Contract frozen for implementation。 |
| 1.1 | 2026-09-09 | Narrow additive read-capability refinement：新增public、read-only semantic capability，用於判斷Event是否已有authoritative Incident ownership。不改Incident ownership authority、mutation semantics、persistence topology、PRD requirements或既有D1～D12 behavior。Status維持 Approved — Implementation Pending。 |

> **Implementation Status Honesty：Approved ≠ Implemented。** SPEC-008 v1.1已完成本次PM-authorized narrow contract refinement，Status為`Approved — Implementation Pending`。既有v1.0 implementation evidence已完成Phase 1～6與read-only audit，但v1.1新增的public ownership read capability尚待implementation及verification；本次approval不表示SPEC-008已標為Implemented，也不表示SPEC-009 lifecycle、SPEC-010 Shadow、SPEC-011 Runtime／E2E或完整Alert Correlation Runtime已完成。

---

# 0. Authority、Purpose 與 D1～D12 Decisions

## 0.1 Authority hierarchy

本 SPEC 依下列 authority 制定：

1. `PRD-003 v1.0 Final`是Alert Correlation／Incident Management requirement authority。
2. `PRD-002 v1.5`是15-field immutable Runtime Event與EventStore authority；SPEC-008只消費authoritative Event evidence，不得修改或重新定義Event。
3. `SPEC-006 v1.0 Implemented`是`CorrelationDecision`、`NormalizedFingerprint`、family、anchor與reason semantics authority。
4. `SPEC-007 v1.0 Implemented — 2026-09-07`是`CorrelationMutationIntent`、Pending／Processed／Blocked／Claim與recovery handoff authority。
5. SPEC-009、SPEC-010、SPEC-011分別保留Lifecycle／Human Workflow、Shadow／Unclassified Store與Runtime Orchestration／downstream E2E責任。

PRD-003 metadata仍引用PRD-002 v1.4；current upstream Event authority為PRD-002 v1.5。此差異是既有non-blocking reference reconciliation，不改變Event schema或本SPEC authority，也不授權修改PRD-003。

若後續發現本文件與active upstream contract無法同時滿足，必須停止受影響範圍並回報PM，不得自行改寫upstream authority。

## 0.2 Purpose

SPEC-008定義authoritative Incident Store與Incident Manager Core的工程契約，使SPEC-006產生的terminal Incident Decision可在SPEC-007 durable Intent保護下，安全地建立或更新Incident，並提供durable idempotency、Event ownership、audit、read與recovery result。

核心責任句：

> **Engine decides. State remembers. Runtime orchestrates. Domain stores own the side effects.**

SPEC-008是Incident domain side-effect authority；它不進行candidate matching、不保存correlation execution state，也不排程工作。

## 0.3 D1～D12 Engineering Decisions

| ID | 正式決策 |
|---|---|
| D1 | SPEC-008擁有Incident persistence與correlation-driven mutation；SPEC-009擁有post-OPEN lifecycle／human workflow。Correlation mutation只接受`OPEN`、`ASSIGNED`、`IN_PROGRESS`，拒絕`AWAITING_REVIEW`、`CLOSED`。 |
| D2 | `IncidentRecord`保存ordered unique Event references、anchor、status、severity、timestamps、correlation context、append-style audit及RCA／external references；不duplicate full Event或RCA artifact。 |
| D3 | `CREATE_NEW`只消費real SPEC-007 Intent、authoritative Event與authoritative now；durable建立`OPEN` Incident，Strong／Weak anchor semantics依Intent，且不得自動assignment。 |
| D4 | `ATTACH_EXISTING`必須使用Intent指定的target，mutation前重新驗證target、status、ownership、family、time與promotion preconditions；成功後append Event、單調severity、更新timestamps並audit。 |
| D5 | `event_id`是business ownership identity，`operation_id`是mutation replay identity；SPEC-008 durable enforce one Event→one Incident及same-operation semantic replay，不依賴SPEC-007 Processed作唯一防線。 |
| D6 | Severity closed order為`LOW < MEDIUM < HIGH < CRITICAL`；Incident severity等於所有correlated Events最高severity，correlation mutation只能escalate。 |
| D7 | Mutation使用caller-supplied timezone-aware authoritative `now`；durable時間具canonical UTC semantics，replay保留原timestamps，hidden wall clock不得成為authority。 |
| D8 | `correlation_context`保存family與current anchor identity；family immutable、ordinary attach不覆寫anchor，並直接reuse SPEC-006 `NormalizedFingerprint`。 |
| D9 | Late Strong-Anchor Promotion只能由`ATTACH_EXISTING + WEAK_TO_STRONG`驅動；保留同一Incident與既有Events，atomic更新Strong anchor並產生auditable effect。 |
| D10 | 每次首次successful authoritative mutation append exactly one primary business audit；same-operation replay不重複audit，failed mutation不產生success audit，receipt不能取代audit。 |
| D11 | Recovery existence與`IncidentCorrelationView`是不同read capability；View只暴露SPEC-006所需current fields，Store不做candidate selection。 |
| D12 | PoC使用independent Python stdlib `sqlite3` database，並以local atomicity、own concurrency protection、durable operation result與Fail-Closed integrity支援replay／recovery；不與SPEC-007共用DB、table或transaction authority。 |

SPEC-008 v1.1在不改寫D1～D12既有behavior的前提下，additive補充一項Event ownership read capability。此capability只公開既有D5 Event→Incident ownership authority的semantic read，不建立新的ownership authority或mutation path。

## 0.4 008-R1 — Physical Persistence Decision

PoC正式選擇：

```text
technology = Python Standard Library sqlite3
default independent database path = incident_store.db
```

`incident_store.db`是SPEC-008的default independent path，可由Runtime／bootstrap configuration注入其他路徑。SPEC-008不得與SPEC-007 Correlation State Store共用同一physical SQLite database file，不得直接讀寫其tables、建立cross-store foreign key或假設shared／distributed transaction。

此決策是SPEC-008 PoC implementation decision，不是PRD-003 product-level永久database requirement，也不表示future production persistence必須使用SQLite。Exact tables、indexes、serialization與lock technique是Implementation Choice，不能取代D1～D12 observable invariants。

## 0.5 008-R2 — Actual Handoff & Recovery Boundary

1. Correlation-driven mutation直接reuse real SPEC-007 `CorrelationMutationIntent`，不建立平行Decision／Intent authority。
2. SPEC-011 Runtime另提供authoritative immutable Runtime Event及timezone-aware authoritative mutation `now`。
3. SPEC-008 durable提供same-`operation_id` result lookup／replay capability。
4. SPEC-008自行enforce `event_id → one Incident`，不以SPEC-007 Processed作唯一防線。
5. SPEC-007 `ProcessingClaim`只保護per-Event correlation execution，不是SPEC-008 per-Incident concurrency authority。
6. SPEC-008提供read-only Incident existence capability供recovery reference validation。
7. Recovery existence與`IncidentCorrelationView` API保持semantic separation。
8. Incident mutation、Event ownership、operation receipt與business audit在同一SPEC-008 local crash-consistent transaction內all commit或all rollback。
9. 不做cross-store 2PC，不直接存取SPEC-007 SQLite tables。

---

# 1. Responsibility 與 Cross-SPEC Boundary

## 1.1 MUST

SPEC-008必須：

- durable保存authoritative Incident current state；
- 由valid `CREATE_NEW` Intent建立`OPEN` Incident；
- 由valid `ATTACH_EXISTING` Intent向指定Incident附加Event reference；
- 保存ordered unique `event_ids`並enforce Event business ownership；
- 執行severity escalation、correlation timestamps與`correlation_context` mutation；
- 執行合法Late Strong-Anchor Promotion；
- append correlation-driven business audit；
- durable保存same-operation result／receipt並支援replay／recovery lookup；
- 提供Incident read、existence及`IncidentCorrelationView` read capability；
- 提供public、read-only Event→Incident ownership existence semantic capability；
- 提供自身local concurrency、transaction、integrity及readiness protection。

## 1.2 MUST NOT

SPEC-008不得：

- 執行SPEC-006 correlation decision、candidate selection、fingerprint computation或Correlation Window evaluation；
- 建立、修改或讀寫SPEC-007 Pending、Blocked、Processed、Intent或Claim authority；
- poll EventStore、schedule、sleep、retry或決定工作執行時機；
- 執行Assignment Policy或任何post-OPEN lifecycle transition；
- 保存Resolution Evidence或執行Human Review；
- 建立／修改Shadow；
- 生成完整RCA artifact或執行Jira、Discord、Dashboard、Email adapter logic；
- 使用Scenario ID、Generator state、Validator expected answer或hardcoded S1～S6 answer；
- 修改Runtime Event或以`Event.status`表示Incident／correlation state；
- automatic destructive repair、force overwrite、ownership reassignment、reset或normal-runtime delete。

## 1.3 Cross-SPEC ownership

| Contract | Authority／責任 | SPEC-008 boundary |
|---|---|---|
| PRD-002／EventStore | Immutable normalized Event evidence | 只讀取mutation所需evidence；以`event_id`保存reference。 |
| SPEC-006 | Correlation semantics與Decision fields | 不重新decision；只驗證Intent與Incident mutation是否一致。 |
| SPEC-007 | Execution state、Intent、Claim、Processed與recovery bookkeeping | 接收real Intent並回authoritative domain result；不存取其physical DB。 |
| SPEC-008 | Incident current state、Event ownership、operation result、correlation audit | 本文件範圍。 |
| SPEC-009 | Assignment、Resolution Evidence、Review與post-OPEN lifecycle | SPEC-008保留欄位及correlation-open defense-in-depth，不執行workflow。 |
| SPEC-010 | Shadow／Unclassified authority | `ROUTE_SHADOW`不由SPEC-008處理。 |
| SPEC-011 | Runtime orchestration、authoritative now、Event acquisition與cross-store coordination | 呼叫SPEC-008 public semantic APIs；不得繞過domain validation。 |

---

# 2. Domain Model

## 2.1 Closed sets

```text
IncidentStatus =
  OPEN | ASSIGNED | IN_PROGRESS | AWAITING_REVIEW | CLOSED

IncidentSeverity =
  LOW | MEDIUM | HIGH | CRITICAL

Correlation-open status =
  OPEN | ASSIGNED | IN_PROGRESS
```

SPEC-008只建立`OPEN`。其他status供read、persistence與future SPEC-009 mutation extension使用；SPEC-008不提供其transition workflow。

## 2.2 `IncidentRecord`

Minimum logical fields：

```text
incident_id
event_ids                  # ordered unique
anchor_event_id            # nullable
status
severity
created_at
updated_at
last_correlated_at
closed_at                  # nullable
assignee                    # nullable
reviewer                    # nullable
correlation_context
audit_trail
rca_status
rca_ref                     # nullable
external_refs
```

Rules：

- `event_ids`只保存Event references，不保存full Event；
- `anchor_event_id`若present必須是`event_ids`成員；
- `audit_trail`為append-style，既有entry不可由correlation mutation覆寫；
- `assignee`、`reviewer`與post-OPEN status可由future SPEC-009 authoritative mutation；
- `rca_ref`只指向future RCA authority，不保存full RCA artifact；
- `external_refs`只保存reference，不保存Jira／Discord／Dashboard完整payload；
- malformed或cross-field inconsistent record不得被當成Not Found或silent skip。

新Incident必須初始化`rca_status=PENDING`與`rca_ref=null`。`PENDING`表示Incident已存在，但RCA generation尚未由後續RCA／Runtime workflow完成；不表示SPEC-008呼叫LLM、建立RCA artifact、啟動RCA workflow或已有RCA artifact。除上述initial value外，`rca_status`的完整closed set與state transition不由本SPEC定義；SPEC-008只要求relationship fields可durable保存／讀取，correlation-driven mutation不得silent overwrite其既有值。

## 2.3 `IncidentCorrelationContext`

Minimum conceptual fields：

```text
correlation_family
anchor_strength
anchor_event_id
anchor_event_type
normalized_fingerprint
anchor_policy_id
anchor_policy_version
promoted_from_weak
```

`normalized_fingerprint`必須直接使用SPEC-006 `NormalizedFingerprint` semantic value，不建立另一套delimiter string或008-specific fingerprint model。

## 2.4 `IncidentAuditEntry`

Minimum conceptual fields：

```text
operation_id
event_id
incident_id
policy_id
policy_version
reason_code
action
effects
occurred_at
```

Primary action vocabulary：

```text
INCIDENT_CREATED
CORRELATION_ATTACHED
```

Effect vocabulary至少支援：

```text
EVENT_ATTACHED
SEVERITY_ESCALATED
ANCHOR_PROMOTED
```

Audit可保存必要safe old／new scalar state，但不得duplicate full Event、full Intent、raw telemetry或sensitive external payload。

## 2.5 Durable mutation result／receipt

Minimum conceptual fields：

```text
operation_id
event_id
mutation_kind
incident_id
completion_result
completed_at
immutable_mutation_identity
```

Receipt保存足以驗證same-operation request semantic equivalence的immutable identity；不能只因`operation_id`存在就回成功。Receipt是replay／recovery evidence，不取代business audit。

## 2.6 Thin mutation request boundary

Implementation可使用`IncidentMutationRequest`或等價wrapper，但它只能組合：

```text
real SPEC-007 CorrelationMutationIntent
authoritative PRD-002 Runtime Event
authoritative timezone-aware mutation now
```

Wrapper不得copy Intent fields成第二份authority，也不得定義008-specific Decision、Reason、Family、Anchor或Fingerprint enum。

---

# 3. Correlation Mutation Input Contract

## 3.1 Accepted Decisions

SPEC-008只接受下列Intent mapping：

| `decision_type` | `intended_terminal_outcome` | `target_incident_id` | Operation |
|---|---|---|---|
| `CREATE_NEW` | `CREATED_INCIDENT` | null | Create Incident |
| `ATTACH_EXISTING` | `ATTACHED_TO_INCIDENT` | required | Attach to exact target |

SPEC-008只接受`CREATE_NEW`與`ATTACH_EXISTING`。Wrong-domain Decision必須依下列closed mapping拒絕，不得轉換成Incident mutation：

```text
ENTER_PENDING
→ INVALID_INCIDENT_MUTATION
→ NON_RETRYABLE

ROUTE_SHADOW
→ INVALID_INCIDENT_MUTATION
→ NON_RETRYABLE
```

`ENTER_PENDING`不建立MutationIntent且屬SPEC-007 execution state；`ROUTE_SHADOW`屬SPEC-010。

## 3.2 Event boundary validation

Runtime必須提供PRD-002 authoritative immutable Event。SPEC-008只消費mutation所需欄位，例如：

```text
event_id
detected_at
event_type
severity
```

最低驗證：

- `event.event_id == intent.event_id`；
- required Event fields符合PRD-002 type與value contract；
- `detected_at`可作absolute、timezone-unambiguous comparison；
- `severity`屬正式closed set；
- Intent使用real SPEC-006／007 types且其Decision／outcome／target shape合法。

SPEC-008不得修改Event、rerun detector、猜測identity或讀Scenario／Generator／Validator answer。

## 3.3 Authoritative time

Mutation `now`由SPEC-011 Runtime提供，必須timezone-aware且可轉為canonical UTC semantics。SPEC-008不得用hidden wall-clock call替代。Invalid `now`必須在任何durable mutation前Fail Closed。

---

# 4. Incident Creation

## 4.1 CREATE_NEW flow

```text
SPEC-006 CREATE_NEW
→ SPEC-007 durable CorrelationMutationIntent
→ SPEC-011 supplies Intent + Event + authoritative now
→ SPEC-008 validates and atomically creates Incident domain state
→ SPEC-008 returns authoritative durable mutation result
→ SPEC-007 finalizes Processed
```

SPEC-008必須：

1. 驗證Intent為`CREATE_NEW`／`CREATED_INCIDENT`且`target_incident_id=null`；
2. 驗證Event與Intent identity一致；
3. 在同一local transaction檢查existing operation receipt及Event ownership；
4. 生成unique、non-empty Incident ID；exact format為Implementation Choice；
5. durable建立`status=OPEN`、`rca_status=PENDING`且`rca_ref=null`的Incident；
6. 建立Event ownership、primary business audit及operation receipt；
7. commit後回傳同一authoritative Incident result。

不得自動assign。Future assignment failure不得rollback已成功建立的`OPEN` Incident。

## 4.2 Strong CREATE

Strong create invariants：

- `anchor_strength=STRONG`；
- Intent `normalized_fingerprint` present；
- `anchor_event_id=event.event_id`；
- `anchor_event_type=event.event_type`；
- top-level與context anchor ID一致；
- context exact policy來自Intent；
- `promoted_from_weak=false`。

## 4.3 Weak standalone CREATE

Weak standalone invariants：

- `anchor_strength=WEAK`；
- `anchor_event_id=null`；
- `anchor_event_type=null`；
- `normalized_fingerprint=null`；
- `correlation_family`保留Intent family；
- exact policy／reason保留於context、audit及receipt所需trace；
- `promoted_from_weak=false`。

Strong／Weak create均將incoming `event_id`作為`event_ids`第一筆ordered evidence，severity取Event severity，`created_at=updated_at=now`，`last_correlated_at=event.detected_at`，`closed_at=null`，`rca_status=PENDING`，`rca_ref=null`，且不得設定assignee／reviewer或啟動RCA generation。

---

# 5. Event Attach

## 5.1 ATTACH_EXISTING preconditions

Intent必須是`ATTACH_EXISTING`／`ATTACHED_TO_INCIDENT`且`target_incident_id` present。SPEC-008只能mutate該target，不得選另一Incident、rerun SPEC-006或重新計算fingerprint。

Mutation前必須revalidate：

- target Incident存在且record完整；
- status為`OPEN`、`ASSIGNED`或`IN_PROGRESS`；
- Event尚未屬於其他Incident；
- Incident `correlation_family`等於Intent family；
- `event.detected_at >= incident.last_correlated_at`；
- Intent anchor／transition與current context一致；
- Late Promotion時滿足第9章全部preconditions。

## 5.2 Successful attach effects

首次successful attach必須在單一local transaction：

- 將incoming `event_id`append至ordered unique `event_ids`；
- 建立同一Event→target Incident ownership；
- `severity=max(current severity, event severity)`；
- `last_correlated_at=event.detected_at`；
- `updated_at=authoritative now`；
- 保留`incident_id`、lifecycle status、assignee、reviewer、closed_at與RCA relationship；
- ordinary attach保留anchor identity與`promoted_from_weak`狀態；
- append exactly one `CORRELATION_ATTACHED` primary audit；
- 建立operation receipt。

Event已在同一Incident的same-operation equivalent replay走第6章replay path，不得再次append。Same Event由不同operation提出，即使target相同，也必須Fail Closed。

## 5.3 Out-of-order protection

若`event.detected_at < incident.last_correlated_at`：

```text
→ STALE_CORRELATION_EVENT_TIME
→ Fail Closed
```

不得用`max(current, incoming)`、processing time或silent skip掩蓋out-of-order mutation。相等timestamp不違反forward-only rule。

---

# 6. Event Ownership 與 Idempotency

## 6.1 Dual identity

```text
event_id     = business Event→Incident ownership identity
operation_id = mutation replay identity
```

SPEC-008必須以durable constraint或等價atomic mechanism enforce：

```text
one event_id → at most one Incident
one operation_id → one semantically immutable mutation result
```

SPEC-007 Processed是correlation execution terminal bookkeeping，但不能取代SPEC-008 ownership。Crash C期間即使Processed尚未建立，SPEC-008仍必須拒絕第二Incident取得同一Event。

## 6.2 Replay matrix

| Existing state | Request | Required result |
|---|---|---|
| Same operation receipt | Semantically equivalent | 回原authoritative result；不mutate Incident、不append audit、不改timestamp |
| Same operation receipt | Contradictory immutable identity | `MUTATION_RECEIPT_CONFLICT`／Fail Closed |
| Existing Event ownership | Different operation | `EVENT_OWNERSHIP_CONFLICT`，即使destination相同亦拒絕 |
| No receipt／ownership | Valid first request | 依create／attach流程atomic執行 |

Semantic equivalence至少涵蓋actual Intent identity、Event identity及所有會影響mutation outcome的Event evidence；不得只比較operation ID或destination。

Same-operation replay仍必須先驗證semantic equivalence。只有完全一致的successful replay可直接回原durable result；此exemption只排除retry-time mutation clock validation，不得讓contradictory projection繞過`MUTATION_RECEIPT_CONFLICT`。

## 6.3 Incident ID

Incident ID由SPEC-008生成，必須unique、durable、non-empty且在replay時保持相同。Exact string format、sequence／UUID策略與display format屬Implementation Choice；`INC-001`只能作example，不是runtime requirement。

---

# 7. Severity 與 Timestamp Semantics

## 7.1 Severity

```text
LOW < MEDIUM < HIGH < CRITICAL
```

Create severity等於incoming Event severity。Attach severity為current與incoming最高值；低或相同severity不得downgrade Incident，也不得加入arbitrary bonus。Invalid Event severity在mutation前回`INVALID_INCIDENT_MUTATION`。

只有實際由較低升至較高時audit effects才包含`SEVERITY_ESCALATED`。

## 7.2 Time invariants

| Field | Semantic authority |
|---|---|
| `created_at` | Incident首次successful creation的authoritative mutation `now`；不等於first Event `detected_at`。 |
| `updated_at` | 首次successful authoritative mutation的`now`；same-operation replay不得改寫。 |
| `last_correlated_at` | 最近一次successful attached Event的`detected_at`。 |
| `closed_at` | 只由future SPEC-009 formal `CLOSED` transition設定。 |
| Audit `occurred_at` | 對應首次successful mutation的authoritative `now`。 |
| Receipt `completed_at` | 首次successful local completion的authoritative `now`。 |

所有durable time必須具有absolute、timezone-unambiguous、canonical UTC semantics及safe comparison capability。Physical serialization屬Implementation Choice。

首次authoritative new mutation不得使`updated_at`倒退，必須滿足`authoritative now >= current Incident.updated_at`。若`authoritative now < current Incident.updated_at`，必須回`STALE_CORRELATION_EVENT_TIME`／`REPAIR_REQUIRED`並Fail Closed，不得使用hidden current time修正。

若same-operation receipt已存在且request semantic identity完全一致，successful replay不得重新執行business mutation，不得以retry call傳入的`now`覆寫timestamp，也不得以該`now`重新判定原successful mutation是否合法。Replay必須回原durable operation result，並保留原`created_at`、`updated_at`、`last_correlated_at`、audit `occurred_at`及receipt `completed_at`。

---

# 8. Correlation Context

## 8.1 General invariants

- `correlation_family`建立後immutable；
- Strong Incident需要complete Strong anchor ID、type、fingerprint與exact policy；
- Weak standalone需要null anchor ID／type／fingerprint及`anchor_strength=WEAK`；
- top-level `anchor_event_id`與context `anchor_event_id`必須一致；
- `anchor_event_id`若present必須由同Incident ownership持有；
- ordinary supporting attach不得覆寫anchor identity、anchor policy或family；
- Intent／current context矛盾時回`CORRELATION_CONTEXT_CONFLICT`；
- 不以full Event或diagnostics作context authority。

## 8.2 View compatibility

Context必須足以由authoritative current Incident deterministic產生第11章`IncidentCorrelationView`。Persistence representation可不同，但不得lossy轉換fingerprint或anchor semantics。

---

# 9. Late Strong-Anchor Promotion

## 9.1 Sole trigger

Promotion只能由下列combination觸發：

```text
decision_type = ATTACH_EXISTING
anchor_transition = WEAK_TO_STRONG
```

不得新增`LATE_PROMOTE` Decision或008-specific correlation decision type。

## 9.2 Preconditions

- target Incident存在且correlation-open；
- target是complete Weak standalone Incident；
- current `anchor_strength=WEAK`；
- current anchor ID／type／fingerprint均為null；
- Intent family與Incident family相同；
- Intent resulting `anchor_strength=STRONG`；
- incoming Event提供Strong anchor evidence；
- Intent normalized fingerprint present；
- exact strong `policy_id + policy_version` present；
- Event與Intent identity及timestamp checks通過。

Error selection必須反映真正失敗的specific invariant：target不存在回`INCIDENT_NOT_FOUND`；lifecycle不再correlation-open回`INCIDENT_NOT_CORRELATION_OPEN`；Event已有其他Incident ownership回`EVENT_OWNERSHIP_CONFLICT`；operation receipt矛盾回`MUTATION_RECEIPT_CONFLICT`；Event time倒退回`STALE_CORRELATION_EVENT_TIME`；family／anchor context矛盾回`CORRELATION_CONTEXT_CONFLICT`。只有general mutation prerequisites均合法，且失敗真正屬於`WEAK_TO_STRONG` promotion-specific invariant時，才回`INVALID_ANCHOR_PROMOTION`。任何失敗均不得partial mutate。

## 9.3 Successful promotion

在同一local transaction：

- 保留same `incident_id`及既有ordered `event_ids`；
- append incoming Strong Event；
- 更新top-level／context `anchor_event_id`為incoming Event；
- `anchor_strength=STRONG`；
- `anchor_event_type=event.event_type`；
- 保存Intent normalized fingerprint與exact anchor policy；
- `promoted_from_weak=true`；
- 依第7章更新severity與timestamps；
- audit action為`CORRELATION_ATTACHED`，effects包含`EVENT_ATTACHED`與`ANCHOR_PROMOTED`，必要時包含`SEVERITY_ESCALATED`；
- 建立matching operation receipt。

不得建立replacement Incident、change family或進行第二次contradictory promotion。Same-operation replay回同一結果；concurrent／stale conflict Fail Closed。

---

# 10. Audit 與 Operation Receipt

## 10.1 Business audit invariants

每次首次successful authoritative create／attach append exactly one primary business audit entry：

- create：`INCIDENT_CREATED`，effects至少含`EVENT_ATTACHED`；
- ordinary attach：`CORRELATION_ATTACHED`，effects至少含`EVENT_ATTACHED`；
- promotion：仍為`CORRELATION_ATTACHED`，effects另含`ANCHOR_PROMOTED`。

Failed／rolled-back mutation不得留下success audit。Same-operation replay不得append第二entry。Audit、Incident、ownership及receipt必須同一local commit。

## 10.2 Receipt equivalence

Receipt必須保存或可deterministically reconstruct下列immutable identity：

- operation／event／mutation kind；
- Intent decision、outcome、target、family、policy、reason、fingerprint與anchor semantics；
- Event type、detected time及severity等實際影響mutation的evidence；
- resulting Incident ID及first completion time。

Implementation可保存canonical projection或digest加必要typed fields；exact representation是Implementation Choice，但collision／lossy comparison不得造成contradictory replay被接受。

---

# 11. Read APIs 與 `IncidentCorrelationView`

## 11.1 Recovery existence capability

SPEC-008必須提供read-only semantic capability：

```text
incident_exists(incident_id) -> bool
```

或完全等價接口，供SPEC-007／SPEC-011 recovery reference validation。Malformed／unreadable matching record不得回`false`偽裝Not Found；應回typed integrity failure。SPEC-007不得直接讀SPEC-008 SQLite tables。

## 11.2 Event → Incident ownership read capability

SPEC-008必須提供public、read-only semantic capability，概念接口為：

```text
event_has_incident_owner(event_id) -> bool
```

Exact implementation method name可依repository style決定，但等價semantic capability必須存在於public supported surface。Caller不得直接query SQLite或依賴`incident_events`等physical table／column名稱。

Normative result semantics：

| Authoritative state | Required result |
|---|---|
| `event_id`具有一個coherent、valid Incident owner | `True` |
| authoritative state確認`event_id`沒有Incident ownership | `False` |
| malformed ownership、dangling reference、contradictory ownership／receipt／Incident relationship、unsupported或corrupt persisted state | Fail Closed；使用既有SPEC-008 typed integrity／error contract |

Corruption或無法可靠分類的state不得回`False`或Not Found以偽裝clean absence。除非既有error taxonomy確實無法表達，implementation不得為此capability新增平行error authority。

此capability嚴格為read-only，且不得：

- create Incident或attach Event；
- 建立、修改、reassign或repair ownership；
- 改變Incident lifecycle；
- mutate operation receipt或business audit；
- force promotion；
- 暴露raw writable Store primitive。

`IncidentManager`仍是sole authoritative correlation mutation boundary。Implementation只能讀取既有authoritative local ownership state，不得建立second ownership table、duplicate ownership ledger、shared SPEC-010 database、cross-store foreign key、distributed transaction或2PC。

此capability允許SPEC-010在建立Shadow前判斷Event是否已有authoritative Incident ownership，但SPEC-008不得import SPEC-010、query Shadow Store、擁有global Incident／Shadow orchestration或實作SPEC-011 sequencing。Global single-terminal ownership仍由SPEC-007＋SPEC-008＋SPEC-010＋SPEC-011共同形成。

此refinement不是competition instrumentation。不得因此新增`count_incidents()`、`scenario_id`、`evaluation_run_id`、expected answer、ground truth、competition-only timestamp或report table。Competition Evaluation仍遵循：existing evidence > offline derivation > evaluation-only instrumentation > production instrumentation > domain contract change。

Documentation impact：

```text
PRD patch required          NO
SPEC-006 patch required     NO
SPEC-007 patch required     NO
SPEC-010 patch required     NO
SPEC-011                    future integration consumer
README patch required       NO
DDS patch required          NO
Architecture change         NO
```

## 11.3 Incident reads

至少提供：

- 依`incident_id`讀authoritative Incident；
- 依`operation_id`讀durable mutation result；
- 讀取／enumerate current Incident Correlation Views；
- readiness／integrity validation。

Read結果必須point-in-time coherent，不得組合不同transaction snapshot的fields。

## 11.4 Minimal `IncidentCorrelationView`

必須reuse SPEC-006 logical view semantics，只暴露：

```text
incident_id
status
last_correlated_at
correlation_family
anchor_strength
normalized_fingerprint
anchor_event_type
```

不得暴露assignee、reviewer、full audit、full RCA、external refs、full `event_ids`或Resolution Evidence。Malformed authoritative Incident不得silent skip以製造false candidate uniqueness；Store只產生／讀取Views，不做lifecycle、window、family或evidence candidate selection。

---

# 12. PoC Persistence Decision 與 Local Transactions

## 12.1 Current PoC choice

```text
Current SPEC-008 PoC implementation choice:
Python Standard Library sqlite3

Default independent DB path:
incident_store.db
```

- 不新增third-party DB dependency；
- default path可由Runtime／configuration注入；
- 必須與SPEC-007 physical DB file獨立；
- 不允許direct cross-table access、shared transaction authority、cross-store foreign key或cross-store ACID assumption；
- 不要求distributed transaction／2PC；
- SQLite不是PRD-003 product-level永久requirement或future production DB requirement。

不得宣稱SPEC-007 hardcode `correlation_state.db`；其actual constructor接受configurable `database_path`。

## 12.2 Local atomic boundary

首次successful create／attach／promotion必須在同一SPEC-008 local crash-consistent transaction中：

```text
Incident state mutation
+ Event ownership
+ operation result / receipt
+ business audit
→ all commit or all rollback
```

若任何validation、constraint、serialization或write失敗，不得留下partial Incident、orphan ownership、success receipt或success audit。

## 12.3 Physical schema boundary

Physical table names、column layout、indexes、JSON／relational representation、transaction helper與locking detail是Implementation Choice。本Draft不把recommended storage layout升格為business contract；implementation只需證明D1～D12 observable invariants與integrity gates。

---

# 13. Concurrency

## 13.1 Same Event concurrency

兩個operation競爭同一`event_id`時，最多一個可建立authoritative ownership。Equivalent same-operation replay可回既有結果；different operation必須Fail Closed。Unique constraint、transaction serialization、CAS或等價mechanism屬Implementation Choice。

## 13.2 Different Events → same Incident

```text
EVT-A → INC-001
EVT-B → INC-001
```

SPEC-007 per-Event Claim無法防止此情境的per-Incident lost update。SPEC-008必須自行提供local concurrency protection，確保：

- 兩個Event references均不遺失且維持authoritative serialization order；
- severity保留兩者最大值；
- 每次successful mutation各有exactly one audit與receipt；
- correlation context不被stale write覆寫；
- lifecycle／assignee／reviewer／RCA relationship不被reset。

Current SQLite PoC可使用transaction serialization或其他local technique；exact lock technology是Implementation Choice。

## 13.3 Concurrent promotion

同一Weak Incident只能產生一個consistent Strong anchor promotion authority。競爭中的same-operation replay回相同result；另一operation若與已建立anchor衝突，必須Fail Closed，不得last-write-wins或建立replacement Incident。

---

# 14. Error 與 Integrity Contract

## 14.1 Incident-domain error taxonomy

SPEC-008使用獨立typed errors，不修改SPEC-006／007 enum：

| Error code | Meaning |
|---|---|
| `INVALID_INCIDENT_MUTATION` | Intent／Event／now／severity／decision shape不合法或不屬SPEC-008。 |
| `INCIDENT_NOT_FOUND` | 指定target Incident不存在。 |
| `INCIDENT_NOT_CORRELATION_OPEN` | Target status為`AWAITING_REVIEW`／`CLOSED`或其他不可correlate狀態。 |
| `EVENT_OWNERSHIP_CONFLICT` | Event已由不同operation或不同Incident取得ownership。 |
| `MUTATION_RECEIPT_CONFLICT` | Same operation與既有immutable mutation identity矛盾。 |
| `STALE_CORRELATION_EVENT_TIME` | Incoming Event時間早於current `last_correlated_at`，或首次authoritative new mutation的`now`早於current `updated_at`。 |
| `CORRELATION_CONTEXT_CONFLICT` | Intent、family、anchor、fingerprint或Incident context矛盾。 |
| `INVALID_ANCHOR_PROMOTION` | `WEAK_TO_STRONG` preconditions不成立或發生contradictory promotion。 |
| `MALFORMED_INCIDENT_RECORD` | Persisted Incident／ownership／receipt／audit record結構或cross-field invariant無效。 |
| `UNSUPPORTED_INCIDENT_STATE_VERSION` | Stored state使用不支援的schema／semantic version。 |
| `INCIDENT_STORE_INTEGRITY_FAILURE` | Store無法安全初始化、enumerate、read或判斷authority。 |
| `TRANSIENT_INCIDENT_STORE_FAILURE` | 已明確分類且safe-to-retry的暫時store failure。 |

Failure disposition closed set：

```text
RETRYABLE | REPAIR_REQUIRED | NON_RETRYABLE
```

PoC v0.1 closed mapping：

| Error | Disposition |
|---|---|
| `INVALID_INCIDENT_MUTATION` | `NON_RETRYABLE` |
| `INCIDENT_NOT_FOUND` | `REPAIR_REQUIRED` |
| `INCIDENT_NOT_CORRELATION_OPEN` | `REPAIR_REQUIRED` |
| `EVENT_OWNERSHIP_CONFLICT` | `REPAIR_REQUIRED` |
| `MUTATION_RECEIPT_CONFLICT` | `REPAIR_REQUIRED` |
| `STALE_CORRELATION_EVENT_TIME` | `REPAIR_REQUIRED` |
| `CORRELATION_CONTEXT_CONFLICT` | `REPAIR_REQUIRED` |
| `INVALID_ANCHOR_PROMOTION` | `REPAIR_REQUIRED` |
| `MALFORMED_INCIDENT_RECORD` | `REPAIR_REQUIRED` |
| `UNSUPPORTED_INCIDENT_STATE_VERSION` | `REPAIR_REQUIRED` |
| `INCIDENT_STORE_INTEGRITY_FAILURE` | `REPAIR_REQUIRED` |
| `TRANSIENT_INCIDENT_STORE_FAILURE` | `RETRYABLE` |

## 14.2 Safe failure payload

Logical failure至少提供：

```text
error_code
retry_disposition
message
operation_id? event_id? incident_id?
field_path?
```

Failure不得duplicate full Event、Incident、Intent、raw logs、traceback或sensitive payload。SPEC-011未來負責將SPEC-008 failure協調為operator-visible狀態，並依正式handoff映射至SPEC-007 Block；SPEC-008不得修改SPEC-007 error enum或直接寫其tables。

## 14.3 Persistence integrity

必須驗證：

- malformed Incident／audit／ownership／receipt；
- unsupported persisted schema／state version；
- Event ownership contradiction；
- duplicate／conflicting operation receipt；
- missing referenced Incident；
- inconsistent anchor／correlation context；
- impossible lifecycle／timestamp combination；
- store能否安全initialize、enumerate及讀取authority。

禁止silent skip、last-write-wins、automatic replacement Incident、guess-and-rewrite或automatic destructive repair。Per-record問題若可安全隔離，回`REPAIR_REQUIRED`且不得當成Not Found；若store不能可靠判斷authority，startup／readiness必須Fail Fast。

---

# 15. Restart 與 Crash Recovery Handoff

## 15.1 Same-operation recovery

SPEC-008不管理SPEC-007 recovery loop。收到unresolved Intent recovery時，只能lookup／replay同一`operation_id`：

```text
SPEC-007 Intent durable
→ SPEC-008 mutation succeeded
→ crash before SPEC-007 Processed
→ restart
→ SPEC-011 asks SPEC-008 for same operation result/replay
→ SPEC-008 returns same durable Incident result
→ SPEC-007 finalizes Processed
```

不得rerun SPEC-006、換target、生成第二Incident或接受contradictory request。

## 15.2 Crash matrix

| Crash point | SPEC-008 durable state | Recovery |
|---|---|---|
| Before SPEC-008 local transaction | 無新Incident domain effect | Same Intent可首次執行；仍須完整validate。 |
| During local transaction | All rollback | Same operation安全重試，不得觀察partial ownership／audit。 |
| After local commit, before response | Incident＋ownership＋receipt＋audit均存在 | Lookup/replay same operation並回原result。 |
| After response, before SPEC-007 Processed | SPEC-008 result durable；SPEC-007 Intent unresolved | Same-operation reconciliation；不重新correlate。 |

Repeated restart／recovery不得改變Incident ID、timestamps、event order、severity、anchor、receipt或audit count。

## 15.3 Cross-store boundary

SPEC-008與SPEC-007各自保證local atomicity，不提供cross-store ACID／2PC。Runtime只透過semantic APIs協調Intent、domain mutation result與Processed finalization；任何cross-store contradictionFail Closed，不得由SPEC-008自動repair Correlation State。

---

# 16. Retention、RCA、External References 與 Lifecycle Governance

## 16.1 Retention／cleanup

- Incident不因`CLOSED`或age自動TTL delete；
- Active／History是query／lifecycle view，不等於physical move或delete；
- SPEC-008不提供normal-runtime destructive cleanup、reset或force repair；
- reset／cleanup只適用controlled dev／test／migration且須PM explicit authorization與strong guard；
- validator與AI coding agent不得清除state以使tests通過；
- future retention／archive engine必須preserveownership、audit與referential integrity，且不在本SPEC實作。

## 16.2 RCA／external reference boundary

SPEC-008只初始化新Incident為`rca_status=PENDING`與`rca_ref=null`，並保存／讀取`rca_status`、nullable `rca_ref`及`external_refs`。`PENDING`只表示RCA generation尚未完成；SPEC-008不得實作`PENDING → GENERATING → COMPLETED / FAILED`或其他RCA workflow。Correlation-driven mutation不得silent overwrite既有RCA relationship。RCA generation、artifact body、refresh／supersede／versioning、Jira、Discord、Dashboard與Email均由future contract處理；`rca_ref`維持null，直到future authoritative RCA persistence建立reference。

## 16.3 Lifecycle boundary

正式closed set為：

```text
OPEN → ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED
```

SPEC-008只create `OPEN`、read全部states、保留future SPEC-009 fields，並以defense-in-depth限制correlation attach／promotion只可作用於前三個correlation-open states。SPEC-008不得實作任何箭頭所代表的transition。

Assignment發生於Incident建立後；future assignment failure不得rollback已durable成功的`OPEN` Incident。

---

# 17. Acceptance Criteria

## AC-008-A — Record／Schema

- `IncidentRecord`具第2.2節minimum fields及closed status／severity values。
- `event_ids`ordered unique，anchor ID與Event ownership／context一致。
- 不duplicate full Event、full RCA或external payload。
- New Incident為`rca_status=PENDING`、`rca_ref=null`，且未duplicate RCA artifact。
- Strong／Weak context及top-level／nested anchor invariants可驗證。
- Malformed record不能被當成Not Found或從View enumeration silent skip。

## AC-008-B — CREATE_NEW

- Valid CREATE建立exactly one durable `OPEN` Incident、ownership、audit及receipt。
- Strong create保存Event anchor、type、fingerprint與exact policy。
- Weak standalone保存null anchor ID／type／fingerprint、WEAK strength及family。
- Strong與Weak create均為`status=OPEN`、`rca_status=PENDING`、`rca_ref=null`，且不啟動Assignment或RCA generation。
- Assignment failure boundary不rollback Incident。
- Incident ID unique且same-operation replay保持相同。

## AC-008-C — ATTACH

- Valid attach只更新Intent指定target並append unique Event。
- `AWAITING_REVIEW`／`CLOSED`拒絕；OPEN／ASSIGNED／IN_PROGRESS可依contract接受。
- Severity只取max，timestamps符合第7章。
- Attach保留lifecycle、assignee、reviewer、RCA relationship及ordinary anchor。
- Earlier Event time回`STALE_CORRELATION_EVENT_TIME`，不得max／skip；equal boundary合法。
- 首次authoritative new mutation的`now < updated_at`時回`STALE_CORRELATION_EVENT_TIME`／`REPAIR_REQUIRED`並Fail Closed。

## AC-008-D — Event Ownership／Idempotency

- Same Event最多一個Incident ownership。
- Same operation equivalent replay回原result並保留原timestamps；不得只因retry-call `now`不同而失敗，且不產生second audit／timestamp rewrite。
- Same operation contradictory semantic projection回`MUTATION_RECEIPT_CONFLICT`並Fail Closed。
- Same Event different operation即使same target亦回ownership conflict。
- Crash C後可由receipt回原result並供SPEC-007 finalize。

## AC-008-E — Late Promotion

- Valid Weak→Strong promotion保留same Incident ID及既有Event order。
- 成功後anchor、fingerprint、policy、severity／time與promotion audit正確。
- 不建立replacement Incident或改family。
- Second／concurrent contradictory promotion Fail Closed；same operation replay不重複effect。

## AC-008-F — Audit／Receipt

- 每次首次successful authoritative mutationexactly one primary business audit。
- Effects正確反映Event attach、severity escalation與anchor promotion。
- Replay不duplicate audit，failed／rolled-back mutation無success audit。
- Receipt與audit均durable但語意分離，且與Incident／ownership同一local commit。

## AC-008-G — Read Boundaries

- `incident_exists`或等價capability可供recovery reference validation。
- `get_operation_result`或等價capability回durable same-operation result。
- `IncidentCorrelationView`只包含SPEC-006 minimum fields且point-in-time coherent。
- Malformed authoritative stateFail Closed，不silent skip；Store不做candidate matching。

## AC-008-H — Persistence／Restart

- Close／reopen independent SQLite store後Incident、ownership、receipt與audit均durable。
- Unsupported version、malformed state與unreadable store符合第14章。
- Local transaction failureall rollback；post-commit crash可same-operation recovery。
- Incident不因age、CLOSED或restart自動刪除。
- Default path可配置且不得與SPEC-007 DB共用。

## AC-008-I — Concurrency

- Same Event race只有一個authoritative ownership；loser Fail Closed。
- Different Events concurrent attach同一Incident時無lost Event、severity、audit或context update。
- Concurrent promotion最多一個consistent anchor result。
- Concurrency correctness不依賴SPEC-007 per-Event Claim。

## AC-008-J — Cross-SPEC Boundary

- 使用real SPEC-007 `CorrelationMutationIntent`及real SPEC-006 `NormalizedFingerprint`／enums。
- Thin request wrapper不copy Intent fields成第二authority，無fake correlation enum。
- 不直接存取SPEC-007 DB／tables且不假設cross-store transaction。
- Event保持PRD-002 immutable，不使用answer leakage。
- `ENTER_PENDING`與`ROUTE_SHADOW`傳入SPEC-008時均回`INVALID_INCIDENT_MUTATION`／`NON_RETRYABLE`。
- 第14.1節十二項Incident-domain Error Code均依closed disposition mapping驗證，不以default／catch-all取代。
- 未實作SPEC-009 lifecycle、SPEC-010 Shadow或SPEC-011 orchestration。
- Full repository regression通過；full downstream Docker Correlation E2E仍defer至SPEC-011 integration。

## AC-008-K — Event Ownership Read Capability

- Existing coherent Event→Incident ownership回`True`。
- Clean authoritative no-owner state回`False`。
- Corrupt、malformed、dangling或contradictory ownership state必須Fail Closed，不得回`False`偽裝clean absence。
- Close／reopen後，同一durable ownership或clean absence維持相同semantic result。
- Capability strictly read-only，不得create／attach／promote、修改ownership、receipt、audit或lifecycle。
- 不建立duplicate ownership authority、second ownership ledger或shared／cross-store persistence authority。
- Public caller不接觸raw SQLite、`incident_events` table或writable Store primitive。

---

# 18. Required Test Layers

Implementation acceptance至少需要：

1. Targeted domain／unit tests：records、validation、create／attach、severity、time、context、promotion、errors。
2. SQLite persistence contract tests：independent DB、durability、schema compatibility、local atomicity與readiness。
3. Idempotency／receipt tests：equivalent replay、contradictory replay、Event ownership conflict與audit exactly-once。
4. Restart／reopen tests：durable Incident、ownership、receipt、audit與same-result lookup。
5. Crash-injection tests：transaction前／中／commit後／SPEC-007 Processed前。
6. Concurrency tests：same Event race、different Events same Incident、concurrent promotion與lost-update protection。
7. Real SPEC-006／SPEC-007 contract integration tests：actual Intent、Decision enums、Fingerprint與Processed handoff doubles。
8. `IncidentCorrelationView` boundary tests：minimum fields、coherent snapshot、malformed-state failure與no candidate selection。
9. Integrity／Fail-Closed tests：malformed record、unsupported version、dangling reference、ownership／receipt／context conflicts及unreadable store。
10. Full repository regression：`python -m pytest -q`或Repository正式equivalent command。
11. Event ownership read tests：existing owner、clean absence、corrupt／dangling／contradictory state、restart durability、read-only surface及no raw persistence leakage。

建議使用parameterized及controlled concurrency／crash tests。不得以固定test count取代contract coverage；coverage優先於數量。

完整downstream Docker Correlation E2E不屬SPEC-008 implementation acceptance，defer至SPEC-011 integration。

---

# 19. Out of Scope／Future Work

本SPEC不實作：

- SPEC-006 correlation decision、candidate selection、fingerprint computation或window evaluation；
- SPEC-007 Pending／Blocked／Processed／Intent／Claim／recovery implementation；
- SPEC-009 lifecycle、Assignment Policy、Resolution Evidence、Human Review與closure workflow；
- SPEC-010 Shadow／Unclassified persistence；
- SPEC-011 Runtime orchestration、polling、scheduling、retry與full downstream E2E；
- RCA generation、full artifact persistence與RAG／SOP learning；
- Jira、Discord、Dashboard、Email adapters；
- distributed DB、Kafka、2PC、cross-store ACID、HA或distributed workers；
- production DB selection；
- automatic authority repair或full Admin Repair Tool；
- production retention／archive engine；
- destructive cleanup／reset；
- full Docker downstream Correlation E2E。

Future work可包含production persistence adapter、governed schema migration、administrative repair tooling、retention／archive、RCA relationship evolution及SPEC-009～011 integration，但不得削弱本文件的ownership、idempotency、audit、atomicity與Fail-Closed invariants。

---

# 20. Implementation Handoff／Contract Freeze Governance

Implementation Owner為 **富裕**。SPEC-008 v1.1已完成PM-authorized narrow contract refinement並維持`Approved — Implementation Pending`；既有v1.0 implementation evidence已完成Phase 1～6，但v1.1新增的Event ownership read capability尚待implementation、verification與PM closure review。

Contract freeze治理：

> SPEC-008 v1.1已完成PM-authorized narrow refinement並frozen for implementation。任何後續semantic requirement／Engineering Contract change，必須先停止受影響implementation、保存evidence並交由PM審核；必要時更新本SPEC或upstream authority後才可繼續。Implementation不得靜默反向改寫SPEC；implementation reality可提出文件修訂，wording／metadata／implementation note可依治理作最小patch或defer，requirement-level change才考慮PRD revision。

Future implementation agent必須：

- 未經PM明確授權不得執行Git mutation；
- 不修改PRD-002、PRD-003、SPEC-006或SPEC-007 authority；
- 不執行destructive reset／cleanup；
- 發現active contract conflict時停止受影響範圍並回報PM；
- 不實作SPEC-009／010／011或其他downstream scope；
- 不以Scenario／Generator／Validator answer通過測試；
- 不把SQLite PoC choice宣稱為PRD或future production requirement。

更新為`Implemented`前至少必須完成targeted tests、SQLite persistence、idempotency／receipt、restart、crash、concurrency、real cross-SPEC contracts、View boundary、integrity tests、full repository regression與PM Final Review。

---

# 21. PM Review Checklist

- [x] Metadata為SPEC-008 v1.1／`Approved — Implementation Pending`／2026-09-09，Owner為富裕。
- [x] Authority正確引用PRD-003 v1.0 Final、PRD-002 v1.5、SPEC-006 v1.0 Implemented、SPEC-007 v1.0 Implemented。
- [x] D1～D12完整且與008-R1／008-R2一致。
- [x] Event保持15-field immutable contract，未使用`Event.status`或answer leakage作Incident state。
- [x] Correlation mutationreuse real SPEC-007 `CorrelationMutationIntent`，沒有duplicate Decision／Intent model。
- [x] `IncidentRecord` minimum fields、ordered unique Events、`rca_status=PENDING`／`rca_ref=null`及RCA／external reference boundary完整。
- [x] CREATE_NEW只建立OPEN，Strong／Weak standalone與RCA initialization semantics正確，且不auto assign或啟動RCA generation。
- [x] ATTACH使用exact target並具status、ownership、family、time及promotion guards。
- [x] SPEC-008自行enforce one Event→one Incident，不只依賴SPEC-007 Processed。
- [x] Same-operation equivalent replay回same result，contradictory replay Fail Closed。
- [x] Severity closed order與monotonic max semantics完整。
- [x] created／updated／last-correlated／closed time authority與replay invariants完整。
- [x] Late Strong-Anchor Promotion保留Incident identity且concurrent conflict Fail Closed。
- [x] Business audit exactly-once與durable operation receipt語意分離。
- [x] Recovery existence與minimal `IncidentCorrelationView` APIs分離。
- [x] Public Event→Incident ownership read capability定義existing owner／clean absence／integrity Fail-Closed semantics，且維持read-only與persistence abstraction。
- [x] SQLite使用independent configurable `incident_store.db`，未宣稱SPEC-007 hardcode DB path。
- [x] Incident、ownership、receipt、audit具有SPEC-008 local all-or-nothing transaction。
- [x] 無shared DB、direct SPEC-007 table access、cross-store ACID或2PC。
- [x] Same Event、different Events same Incident及concurrent promotion protection完整。
- [x] Incident-domaintyped errors、closed dispositions、integrity／readiness Fail Closed完整。
- [x] Incident不因CLOSED／age自動刪除，無normal-runtime destructive cleanup。
- [x] SPEC-009 lifecycle／human workflow未被提前實作。
- [x] SPEC-010 Shadow與SPEC-011 Runtime／full downstream E2E未被提前實作。
- [x] AC-008-A～K可直接轉換為targeted tests。
- [x] Required test layers完整且未以固定數量取代coverage。
- [x] 文件維持`Approved ≠ Implemented`，未宣稱implementation、完整Runtime或full Docker E2E完成。
- [x] Incident-domain failure disposition closed mapping已由PM Review確認。
