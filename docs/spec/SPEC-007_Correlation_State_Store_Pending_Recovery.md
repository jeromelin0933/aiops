# SPEC-007 — Correlation State Store & Pending Recovery

## Software Design Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-007 |
| Document Name | Correlation State Store & Pending Recovery |
| Version | 1.0 |
| Status | Approved — Implementation Pending |
| Date | 2026-09-04 |
| Requirement Authority | PRD-003 v1.0 Final |
| Upstream Event Contract | PRD-002 v1.5 |
| Upstream Correlation Contract | SPEC-006 v1.0 Implemented |
| Implementation Owner | Tako |

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-09-04 | Initial Draft；定義 Correlation State Store、Pending continuity、Processed／Dedup、Blocked／Failure、single-flight、durable Mutation Intent、crash consistency與restart recovery工程契約。 |
| 0.2 | 2026-09-04 | PM Review revision：修正 Active Pending policy vocabulary、Block resolution semantics、nullable failure phase、MutationIntent upstream field alignment、schema/version implementation neutrality、startup integrity scope、Shadow dangling-reference coverage與 Claim wording；D1～D12核心架構不變。 |
| 1.0 | 2026-09-04 | Second PM Review completed；D1～D12、D2 narrow clarification、D12 Time／Phase Ownership amendment、state／recovery／crash consistency contracts與 AC-007-A～I通過 PM Review。Status更新為 Approved — Implementation Pending；Engineering Contract frozen for implementation。尚未開始 implementation。 |

> **Implementation Status Honesty：Approved ≠ Implemented。** 本文件已完成 Second PM Review並進入 `Approved — Implementation Pending`；此狀態表示 Engineering Contract已核准，可進入後續受治理的 implementation，但不代表 Correlation State Store、Pending recovery、Processed／Dedup、Blocked／Failure persistence、MutationIntent、single-flight或 restart recovery已完成實作。只有在 production implementation、targeted tests、persistence／concurrency／crash recovery tests、cross-SPEC integration、full repository regression與 PM Final Review完成後，才可更新為 `Implemented`。

---

# 0. Authority、Purpose 與 D1～D12 Decisions

## 0.1 Authority hierarchy

本 SPEC 依下列 authority 制定：

1. `PRD-003 v1.0 Final` 是 Alert Correlation／Incident Management requirement authority。
2. `PRD-002 v1.5` 是 15-field immutable Runtime Event 與 EventStore authority；SPEC-007 只能保存 Event reference與 correlation bookkeeping，不得重新定義或修改 Event。
3. `SPEC-006 v1.0 Implemented` 是 correlation decision、logical type、enum、exact policy reference、Pending evaluation phase與 failure contract authority。
4. SPEC-008、SPEC-010、SPEC-011 目前是 downstream handoff boundary；本 SPEC 不預先實作其 domain side effects或 orchestration。

PRD-003 metadata仍引用 PRD-002 v1.4；PRD-002 v1.5只做 downstream-reference reconciliation，明確保留 Event schema與 detector semantics。本 SPEC 因此引用 current authority PRD-002 v1.5；此 metadata差異不是 semantic blocker，也不授權修改 PRD-003。

若 implementation 發現最新 authority、既有 production contract與本文件無法同時滿足的 active conflict，必須停止受影響範圍並回報 PM，不得自行修改 upstream authority或重設計。

## 0.2 Purpose 與 architectural boundary

SPEC-007 定義獨立 logical `Correlation State Store`，負責 correlation execution state與 bookkeeping 的 durable memory、state-transition validation、idempotency protection與 recovery continuity。

```text
SPEC-006 Engine
→ decides correlation semantics

SPEC-007 Correlation State Store
→ remembers and validates durable execution state

SPEC-011 Runtime
→ orchestrates when work occurs

SPEC-008 Incident Store / Manager
→ owns authoritative Incident mutation

SPEC-010 Shadow Store
→ owns authoritative Shadow mutation
```

工程責任句：

> **Engine decides. State remembers. Runtime orchestrates. Domain stores own the side effects.**

## 0.3 D1～D12 Engineering Decisions

| ID | 正式決策 |
|---|---|
| D1 | Correlation State Store 是獨立 logical persistence role；保存 execution state，不改 EventStore，也不把 bookkeeping藏入 Incident Store。Logical Store ≠ Physical Database。 |
| D2 | `ActivePendingRecord`只保存 durable reevaluation context與 exact policy identity，不保存 authoritative candidate snapshot；`correlation_policy`與 exact policy必須一致。 |
| D3 | Pending Grace可設定，PoC default 30秒且必須 positive／finite；保存 authoritative `entered_pending_at`與 absolute `expires_at`，restart不得重設。 |
| D4 | Pending recheck／expiry沿用首次 Pending 的 exact `policy_id + policy_version`，以 latest Incident Views重算；SPEC-007依 authoritative now決定 phase。 |
| D5 | 每個完成 correlation的 `event_id`只有一筆 terminal Processed record；只有 downstream authoritative side effect成功後才能建立。 |
| D6 | Evaluation Failure既非 Processed亦非 normal Pending；以 durable Blocked state保存 safe diagnostics與 retry disposition，Fail Closed但不丟棄。 |
| D7 | `event_id`是 idempotency key；Temporary Processing Claim保障同時間最多一條 authoritative mutation path，且 crash後可安全 reclaim。 |
| D8 | Terminal side effect前必須先持久化 durable `CorrelationMutationIntent`；以 same-operation idempotent replay與 reconciliation提供 crash consistency，不要求 distributed ACID／2PC。 |
| D9 | Restart是 recovery而非 reset；Startup Recovery Barrier與固定 per-event precedence必須防止既有 Event誤判為 `UNSEEN`。 |
| D10 | 必須區分 recoverable interruption與 authoritative contradiction；前者可 deterministic recovery，後者 Fail Closed且不得自動 authority repair。 |
| D11 | PoC不自動 retention deletion；Processed不得獨立 TTL prune，reset／destructive cleanup只能經受控授權，validator與 AI coding agent不得執行。 |
| D12 | Public contract定義 semantic capabilities與 illegal-transition rejection，不鎖定 method name、DB、file layout或 lock technology；Store validates transitions，Engine decides semantics。 |

## 0.4 D2 narrow clarification — UNKNOWN policy vocabulary

PoC v1.0 `ActivePendingRecord.correlation_policy`合法 persisted closed set只有：

```text
STRONG_ANCHOR
WEAK_SUPPORTING_KNOWN
```

`UNKNOWN_SUPPORT_ONLY`可視為 PRD-level reusable vocabulary／example，但在目前 PoC v1.0 direct-Shadow semantics下，不是 `ActivePendingRecord`的合法 persisted value。依 SPEC-006，registered `general_log_anomaly`與`general_metrics_anomaly`直接 `ROUTE_SHADOW`，不得建立 `ActivePendingRecord`；本 clarification不新增 UNKNOWN Pending flow。

## 0.5 D12 narrow amendment — Time／Phase ownership

```text
SPEC-011 Runtime:
- owns authoritative runtime now
- owns scheduling / wake-up / orchestration

SPEC-007:
- owns durable Pending state
- compares authoritative now against durable expires_at
- determines PENDING_RECHECK vs PENDING_EXPIRED

SPEC-006:
- only consumes resolved EvaluationPhase
- does not read wall clock
- does not own Pending timer
```

SPEC-011 不得 reset Pending timer、overwrite `expires_at`或 bypass SPEC-007 phase determination。

---

# 1. Module Responsibility 與 Boundary

## 1.1 MUST

SPEC-007 必須：

- durable保存 Active Pending、Processed／Dedup、Blocked／Failure、Mutation Intent與 Temporary Processing Claim所需狀態；
- 以 `event_id` resolve authoritative per-event execution state；
- 驗證 Correlation State record structure、state consistency invariants、per-event ownership bookkeeping與 legal transition；
- 保存 Pending absolute expiry、evidence semantics與 exact policy reference；
- 使用 SPEC-011提供的 authoritative now決定 Pending phase；
- enforce per-event single-flight與 unique terminal ownership；
- 在 downstream mutation前 durable建立 same-operation intent；
- 在 downstream success後 atomic finalize Processed bookkeeping與 matching local state；
- 支援 startup reconciliation、crash recovery與 replay-safe state access；跨 EventStore／Incident Store／Shadow Store的 read-only authoritative confirmation由 SPEC-011／recovery orchestration協調；
- 對 contradiction、malformed state或 unsupported state version Fail Closed。

## 1.2 MUST NOT

SPEC-007 不得：

- 修改 PRD-002 15-field Event或以 `Event.status`表示 Pending／Correlated／Consumed；
- duplicate full Event、raw Logs、raw Metrics或 EventStore authority；
- 執行 SPEC-006 candidate matching、fingerprint computation或 business decision；
- 保存 `candidate_incident_ids` authoritative snapshot；
- create／attach／mutate Incident或控制 Incident lifecycle；
- create／mutate完整 Shadow artifact；
- poll EventStore、排程、sleep或決定何時執行工作；
- 執行 RCA／RAG、SOP learning、Dashboard或 notification；
- 以 Scenario ID、Generator state、Validator expected answer或 hardcoded S1～S6 answer作為 authority；
- 自動 repair、last-write-wins、silent skip、destructive cleanup或 reset state；
- 鎖定 physical database、transaction engine、file layout、mutex／lease／CAS技術。

## 1.3 Cross-SPEC ownership

| Contract | Authority／責任 | SPEC-007 handoff |
|---|---|---|
| PRD-002／EventStore | Immutable normalized Event evidence | 只保存 `event_id`；需要 payload時由 Runtime重新取得 authoritative Event。 |
| SPEC-006 | Pure、deterministic、persistence-blind、wall-clock blind correlation semantics | 接收 Decision／Failure；Pending recovery提供 exact context與 resolved phase，不 duplicate matching。 |
| SPEC-007 | Correlation execution state、bookkeeping與 transition validation | 本文件範圍。 |
| SPEC-008 | Incident creation／attach、single Event→one Incident ownership、audit與 authoritative result | Intent後由 Runtime呼叫；mutation必須以 `event_id`／`operation_id` idempotent。 |
| SPEC-010 | Shadow authoritative creation與 conflict detection | Intent後由 Runtime呼叫；same operation replay回相同 Shadow。 |
| SPEC-011 | Event ingestion、authoritative now、scheduling、View acquisition、Engine invocation、claim coordination、retry trigger與 recovery orchestration | 決定何時做；不得取代 Store的 phase determination或 transition validation。 |

SPEC-007知道 **WHAT state exists**；SPEC-011決定 **WHEN to act on it**。Domain Stores擁有 side effects。

---

# 2. Domain Model／Logical Records

## 2.1 Common record rules

所有 durable records概念上必須：

- 使 implementation可可靠判斷 stored state使用的是 supported或 unsupported schema semantics；version metadata可位於 record、document envelope、table、store metadata、migration metadata或其他明確 persistence boundary，placement屬 Implementation Choice；
- 使 identifiers遵守其 authoritative source contract，並維持 deterministic reference semantics；SPEC-007不得重新定義 Event ID、Incident ID、Shadow ref或 Policy ID的 normalization規則；
- 使 durable time values保留 absolute、timezone-unambiguous semantics與 safe comparison capability；ISO-8601 UTC、epoch、database-native timestamp或其他等價 representation均屬 Implementation Choice，除非 upstream contract另有要求；
- 不保存 full Event、full Incident、full Shadow或 arbitrary traceback；
- 在讀取與寫入時驗證 immutable fields與 cross-record invariants。

Physical serialization、table／collection layout與 storage engine是 Implementation Choice，不屬本 Draft的固定契約。

## 2.2 `ActivePendingRecord`

Minimum logical fields：

```text
event_id
entered_pending_at
expires_at
correlation_policy
policy_id
policy_version
pending_reason
```

Closed sets：

```text
PendingReason =
  NO_COMPATIBLE_CANDIDATE
  MULTIPLE_COMPATIBLE_CANDIDATES

CorrelationPolicyKind =
  STRONG_ANCHOR
  WEAK_SUPPORTING_KNOWN
```

`correlation_policy`表達 reusable Pending evidence semantics；`policy_id + policy_version`表達首次 `ENTER_PENDING`使用的 exact SPEC-006 policy identity。Store必須透過受注入／受控的 exact policy resolver驗證兩者一致；例如 exact policy為 `KNOWN_WEAK`但 record聲稱 `STRONG_ANCHOR`時，必須 Fail Closed並標記 Repair Required。

`UNKNOWN_SUPPORT_ONLY`不是上述 PoC Active Pending closed set的成員；若 persisted Pending record包含此值，必須視為 invalid state並 Fail Closed。Registered UNKNOWN仍依 SPEC-006 direct `ROUTE_SHADOW`。

Immutable after creation：`event_id`、`entered_pending_at`、`expires_at`、`correlation_policy`、`policy_id`、`policy_version`。Pending期間只允許 `pending_reason`依最新 SPEC-006 `ENTER_PENDING` Decision更新。

不得保存 candidate IDs、candidate count作 authoritative decision input或任何 candidate snapshot。

## 2.3 `ProcessedCorrelationRecord`

Minimum logical fields：

```text
event_id
terminal_outcome
resolved_at
incident_id: nullable
shadow_ref: nullable
policy_id
policy_version
```

Terminal outcome closed set：

```text
ATTACHED_TO_INCIDENT
CREATED_INCIDENT
SHADOWED
```

Reference invariants：

- `ATTACHED_TO_INCIDENT`與`CREATED_INCIDENT`要求 `incident_id` present、`shadow_ref=null`。
- `SHADOWED`要求 `shadow_ref` present、`incident_id=null`。
- `event_id`全域唯一；record建立後 terminal identity不可改寫。

Processed不保存 full Event、candidate list、full fingerprint diagnostics、full Incident或 full Shadow payload。

## 2.4 `BlockedCorrelationRecord`

Recommended minimum logical fields：

```text
event_id
failure_kind
failure_code
evaluation_phase: nullable
first_failed_at
last_failed_at
attempt_count
retry_disposition
policy_id: nullable
policy_version: nullable
field_path: nullable
incident_id: nullable
```

Closed sets：

```text
FailureKind = CORRELATION_DOMAIN_FAILURE | STATE_DOMAIN_FAILURE | INTERNAL_FAILURE
RetryDisposition = RETRYABLE | REPAIR_REQUIRED | NON_RETRYABLE
```

Block只保存 safe、reference-oriented diagnostic metadata，不保存 full Event、raw Logs、full Incident、candidate snapshot、arbitrary traceback dump或 sensitive payload。相同 unresolved failure再次被可靠觀察時，保留 `first_failed_at`，更新 `last_failed_at`並單調增加 `attempt_count`；不得以 retry覆寫成較早時間或重建 Event authority。

若 Block來自具體 SPEC-006 evaluation attempt，必須保存當時真實 `EvaluationPhase`。若 failure來自 state-domain、startup、recovery、mutation reconciliation或 internal path且沒有合法 SPEC-006 evaluation phase，`evaluation_phase=null`；不得為填欄位而 invent phase。`policy_id`與`policy_version`同樣只在可安全適用時保存，否則為 null，不得猜測。

## 2.5 `CorrelationMutationIntent`

Terminal Decisions `ATTACH_EXISTING`、`CREATE_NEW`、`ROUTE_SHADOW`在 downstream authoritative side effect前，必須先建立 durable intent。

Minimum logical fields：

```text
operation_id
event_id
intended_terminal_outcome
decision_type
policy_id
policy_version
correlation_family
reason_code
target_incident_id: nullable
normalized_fingerprint: nullable
anchor_strength: nullable
anchor_transition
created_at
```

上述 decision context只引用 SPEC-006 actual `CorrelationDecision` fields，並保存 retry **同一 authoritative mutation**所需的最小 immutable subset；不建立另一套 correlation model。SPEC-006 `diagnostics`不是 terminal mutation identity，不得保存為 authoritative retry context。未來若證明額外 scalar field是必要 mutation precondition，必須另行 PM review，不得由 implementation自行加入 authority contract。

Mapping必須符合：`ATTACH_EXISTING`要求 `target_incident_id` present；`CREATE_NEW`與`ROUTE_SHADOW`要求 `target_incident_id=null`；`ENTER_PENDING`不建立 MutationIntent。`CREATE_NEW`與`ROUTE_SHADOW`在 mutation前不得 invent destination reference或 Shadow target／`shadow_ref`；downstream成功後產生的 `incident_id`／`shadow_ref`才寫入 final `ProcessedCorrelationRecord`。`operation_id + event_id`是 downstream重播同一 mutation並取得相同 authoritative result的依據。Intent不 duplicate Event payload；Runtime從 EventStore取得 immutable Event。

## 2.6 Temporary Processing Claim

Claim是 per-event temporary execution authority，不是 terminal ownership。Logical semantics至少包含：

- 唯一識別 `event_id`與本次 claim attempt；
- 同一 Event同時間只有一個 holder具 authoritative mutation資格；
- 非 holder不得建立或推進 Intent／Processed；
- 已失去 processing authority的 stale executor不得繼續 authoritative state mutation；
- crash後只能在 abandonment已可安全判定時 reclaim；
- reclaim前必須重新讀 Processed、Intent、Pending與Blocked；
- release／reclaim不得刪除 durable terminal或 recovery state。

Implementation必須防止已失去 processing authority的 stale executor繼續進行 authoritative state mutation。Attempt token、fencing token、lease、CAS、transaction或其他等價 mechanism皆屬 Implementation Choice，不強制其中任一技術。若使用 lease，其期限與 Pending Grace語意完全不同，不得改寫 `expires_at`。

## 2.7 Per-event authoritative state concept

一個 `event_id`可同時具有部分不同角色 records，例如 Pending + Blocked；caller看到的 authoritative execution state必須由第4章 precedence導出，而非以最新 timestamp猜測。`UNSEEN`是查無任何 recovery-relevant durable state的 derived absence state，不持久化 `UNSEEN` record。

---

# 3. Config 與 Time Semantics

## 3.1 Pending Grace config

Minimum config：

```text
pending_grace_seconds: finite positive number = 30
```

Invalid type、boolean、zero、negative、NaN或 infinity必須 bootstrap Fail Fast，不得 silent fallback。PoC config在 controlled startup load後於 process lifetime immutable；本 SPEC不要求 hot reload。

Config source、physical file與 parsing由 future bootstrap／SPEC-011 integration決定。State Store不直接依賴 Scenario config或 expected answers。

## 3.2 Authoritative time

SPEC-011提供 timezone-aware authoritative runtime `now`；SPEC-007驗證其型別與可比較性後使用。State Store不得自行以隱藏 wall-clock call取代注入的 authoritative now，否則 phase determination與測試不可重現。

首次 Pending：

```text
entered_pending_at = authoritative now
expires_at = entered_pending_at + pending_grace
```

兩者皆為 absolute timestamp；不得保存 remaining seconds作 authority。Restart、retry、reason update、Blocked coexistence與 claim reclaim均不得重設這兩個值。

## 3.3 Phase determination

```text
if now < expires_at:
    PENDING_RECHECK

if now >= expires_at:
    PENDING_EXPIRED
```

Boundary `now == expires_at`必須是 `PENDING_EXPIRED`。Phase resolution回傳 SPEC-006既有 `EvaluationPhase`，並組裝 exact `CorrelationEvaluationContext(policy_id, policy_version)`；不得 invent新 phase。

Pending Grace與 Correlation Window不同：

> **Pending Grace protects responsiveness; Correlation Window protects episode continuity.**

Correlation Window由 SPEC-006依 Event與 latest Incident Views評估；Pending Grace由 SPEC-007依 durable Pending與 authoritative now評估。

---

# 4. Authoritative State Resolution 與 Precedence

## 4.1 Fixed precedence

每次 Runtime準備處理 `event_id`，Store必須用一致 snapshot／等價隔離語意解析：

```text
1. TERMINAL_PROCESSED
2. UNRESOLVED_MUTATION_INTENT
3. ACTIVE_PENDING + BLOCKED
4. ACTIVE_PENDING
5. BLOCKED
6. UNSEEN
```

此 precedence不是資料刪除順序；它決定下一個合法 action與哪個狀態支配 execution。

| Resolved state | Required handling |
|---|---|
| `TERMINAL_PROCESSED` | idempotent no-op；不得 reevaluate。完全 matching stale Intent可做 bookkeeping completion，conflict則 Fail Closed。 |
| `UNRESOLVED_MUTATION_INTENT` | 不呼叫 SPEC-006；reconcile／retry same `operation_id`。 |
| `ACTIVE_PENDING + BLOCKED` | Block disposition先限制 retry eligibility；Pending absolute expiry與 exact policy保持。 |
| `ACTIVE_PENDING` | SPEC-007依 now決定 recheck／expired；Runtime取得 latest Views後呼叫 SPEC-006。 |
| `BLOCKED` | 依 disposition決定不可自動 retry、等待 repair或由 SPEC-011觸發受控 retry。 |
| `UNSEEN` | derived absence；只有此狀態可進 `INITIAL` evaluation。 |

## 4.2 Resolution safety

Malformed record不得因解析失敗被當成不存在，否則 Event可能誤判 `UNSEEN`並重複 mutation。若無法以 per-event粒度安全隔離，視為 store-level integrity failure並阻止 startup。

---

# 5. Active Pending State Machine

## 5.1 Initial enter

只有 SPEC-006成功回傳 `ENTER_PENDING`時可建立 Active Pending。建立前必須：

1. 持有該 `event_id`有效 claim；
2. 重新確認沒有 Processed或 unresolved Intent；
3. 驗證 Decision reason是 `NO_COMPATIBLE_CANDIDATE`或`MULTIPLE_COMPATIBLE_CANDIDATES`；
4. 由 resolved exact policy導出並驗證 `correlation_policy`；
5. 使用 authoritative now計算一次 absolute expiry。

Registered UNKNOWN直接 `ROUTE_SHADOW`，不得進 Pending。

## 5.2 Pending recheck

當 `now < expires_at`，Store產生：

```text
CorrelationEvaluationContext(
  evaluation_phase=PENDING_RECHECK,
  policy_id=pending.policy_id,
  policy_version=pending.policy_version
)
```

Runtime必須取得 latest authoritative Incident Views，再呼叫 SPEC-006。不得使用 pending entry或 crash前 candidate snapshot。

若結果仍為 `ENTER_PENDING`，只可更新 `pending_reason`；不可改 immutable Pending fields。若結果是 terminal Decision，進第9章 Mutation Intent protocol。

## 5.3 Pending expiry

當 `now >= expires_at`，Store產生 exact context但 phase為 `PENDING_EXPIRED`。Runtime仍須取得 latest Views並完成 final SPEC-006 reevaluation；expiry不代表 unconditional `CREATE_NEW`。

若 final result唯一匹配，仍可 `ATTACH_EXISTING`；若 SPEC-006回 `CREATE_NEW`，才進 terminal mutation protocol。

## 5.4 Pending + Block coexistence

Pending reevaluation若回 Failure或遭 internal／state failure，建立／更新 Block但保留原 Pending。不得 reset `entered_pending_at`、`expires_at`、`correlation_policy`或 exact policy reference。

Repair後重新處理時，以原 absolute expiry與當下 authoritative now決定 phase；若已過期，直接使用 `PENDING_EXPIRED`，不得重給30秒。

若 `ACTIVE_PENDING + BLOCKED`在正式允許的 repair／retry trigger後完成 safe reevaluation，且 SPEC-006回 `ENTER_PENDING`，必須保留原 `ActivePendingRecord`，只在需要時更新 `pending_reason`，並 resolve matching active Block，結果回到 `ACTIVE_PENDING`。原 `entered_pending_at`、`expires_at`、`correlation_policy`、`policy_id`與`policy_version`全部不變。

## 5.5 Blocked-only reevaluation進入 Pending

若 Event只有 `BLOCKED`，在正式允許的 repair／retry trigger後完成 safe reevaluation，且 SPEC-006回 `ENTER_PENDING`，則建立新的 `ActivePendingRecord`並 resolve matching active Block：

```text
BLOCKED
→ safe reevaluation returns ENTER_PENDING
→ create ActivePendingRecord
→ resolve matching Block
→ ACTIVE_PENDING
```

因原本沒有 Pending，新 record使用 `entered_pending_at=authoritative now`、`expires_at=authoritative now + Pending Grace`，並保存此次合法 evaluation對應的 `correlation_policy`、exact `policy_id + policy_version`與 `pending_reason`。

若 repaired reevaluation回 `ATTACH_EXISTING`、`CREATE_NEW`或 `ROUTE_SHADOW`，必須先建立 durable MutationIntent；Block可保持 active，直到 downstream authoritative mutation成功並完成 terminal finalization時，才由 local atomic transition resolve matching Block。

## 5.6 Pending state transitions

```text
UNSEEN + ENTER_PENDING
→ ACTIVE_PENDING

ACTIVE_PENDING + ENTER_PENDING
→ ACTIVE_PENDING (reason may change)

ACTIVE_PENDING + Failure
→ ACTIVE_PENDING + BLOCKED

BLOCKED + safe reevaluation ENTER_PENDING
→ new ACTIVE_PENDING + resolve matching Block

ACTIVE_PENDING + BLOCKED + safe reevaluation ENTER_PENDING
→ preserve ACTIVE_PENDING + optional reason update + resolve matching Block

ACTIVE_PENDING + terminal Decision
→ ACTIVE_PENDING + MUTATION_INTENT
→ downstream success
→ TERMINAL_PROCESSED
```

Pending deactivation只能作為 successful terminal finalization的一部分，或由未來明確受治理的行政流程執行；不能先移除 Pending再嘗試 downstream mutation。

---

# 6. Evaluation Failure／Blocked／Retryability

## 6.1 SPEC-006 domain failure mapping

| `CorrelationErrorCode` | Retry disposition |
|---|---|
| `INVALID_EVENT_ENVELOPE` | `NON_RETRYABLE` |
| `MISSING_REQUIRED_IDENTITY` | `NON_RETRYABLE` |
| `INVALID_IDENTITY_VALUE` | `NON_RETRYABLE` |
| `POLICY_NOT_REGISTERED` | `REPAIR_REQUIRED` |
| `POLICY_VERSION_UNAVAILABLE` | `REPAIR_REQUIRED` |
| `INVALID_INCIDENT_VIEW` | `REPAIR_REQUIRED` |
| `INCONSISTENT_CORRELATION_CONTEXT` | `REPAIR_REQUIRED` |

Mapping是 closed contract；不得將 immutable input error busy-loop retry。`POLICY_VERSION_UNAVAILABLE`不得 fallback current／latest。

## 6.2 Internal and state failures

Unexpected programming／internal exception使用 `failure_kind=INTERNAL_FAILURE`，PoC至少標為 `REPAIR_REQUIRED`，不得包裝成新的 SPEC-006 `CorrelationErrorCode`。

SPEC-007 persistence／integrity failure使用獨立 state-domain taxonomy（第15章）。`RETRYABLE`只適用已被 downstream contract或 implementation明確分類為 transient且 safe-to-repeat的 failure；retry cadence／backoff由 SPEC-011定義，State Store只保存 disposition與attempt metadata。

## 6.3 Block behavior

- Failure／Blocked不是 Processed，也不是 normal Pending。
- Block可與 Active Pending coexist。
- `NON_RETRYABLE`不得由 normal automatic loop永久 retry。
- `REPAIR_REQUIRED`不會因 restart自動變成 repaired。
- Retry前必須重新 resolve per-event state並取得 claim。
- Block只有在後續 safe reevaluation成功重新進入 Pending，或 terminal downstream outcome成功完成後，才能解除 active blocking semantics。
- `BLOCKED` safe reevaluation回 `ENTER_PENDING`時建立新 Pending並 resolve matching Block；`ACTIVE_PENDING + BLOCKED`則保留原 Pending、必要時更新 reason並 resolve matching Block。
- Repaired reevaluation回 terminal Decision時，Block保持 active直到 successful terminal finalization在同一 local atomic transition resolve matching Block。
- Restart、operator單純要求、retry開始，或 error表面消失但尚未成功 reevaluate，均不得直接刪除／resolve Block。
- Block不得 direct route Shadow，也不得直接形成 RCA Knowledge Improvement Candidate或 feeding RAG／SOP learning。

Blocked state可供 engineering reliability monitoring、data quality analysis、bug fix、test hardening與 deployment governance。若 repair後 Event成功進 Incident，其後正常 RCA／Resolution Evidence才可依正式 downstream workflow進 Knowledge Improvement。

> **Fail Closed doesn’t mean dropped, but also doesn’t grant the system authority to repair data automatically.**

---

# 7. Processed／Dedup／Terminal Ownership

## 7.1 Establishment rule

Processed只能在 SPEC-008／SPEC-010 authoritative downstream side effect已成功或 idempotently確認相同結果後建立。

下列皆不等於 Processed：Event被讀取、SPEC-006 evaluate成功、`ENTER_PENDING`、Failure／Blocked、Intent剛建立。

## 7.2 Replay semantics

```text
Processed event_id
→ idempotent no-op
→ no SPEC-006 reevaluation
→ no duplicate attach
→ no duplicate Incident
→ no duplicate Shadow
```

完全相同 terminal replay可回 idempotent confirmation／no-op。若 replay聲稱不同 terminal outcome、不同 Incident或不同 Shadow reference，必須 Fail Closed為 persistence／ownership conflict，不得建立另一 terminal record。

## 7.3 Ownership scope

Terminal ownership只限制 authoritative operational Incident／Shadow membership。它不限制 RCA後續讀取 Event、human analysis、Dashboard evidence view、Logs／Metrics contextual retrieval或 future analytics。

> **We limit operational ownership, not information reuse.**

SPEC-008必須承接 same `event_id`不能 authoritative屬於兩個 Incidents；SPEC-010必須承接 Shadow與 Incident ownership conflict detection。

---

# 8. Single-flight Processing 與 Idempotency

## 8.1 Execution invariant

所有 `INITIAL`、Pending recheck、Pending expiry、Blocked retry與 Intent recovery均以 `event_id`為 idempotency key。同一 `event_id`同時間最多一條 path具有 authoritative mutation資格。

Runtime在任何 evaluation或 recovery action前，必須先 resolve第4章狀態，再依 action取得 claim。Claim acquisition必須 atomic／具等價競爭保護；loser不得繼續 authoritative action。

## 8.2 Claim lifecycle

| Action | Required invariant |
|---|---|
| Acquire | 無其他有效 holder；取得本次 temporary processing authority；具體識別／保護方式屬 Implementation Choice。 |
| Use | 每個 authoritative state mutation都必須確認 executor仍持有 processing authority，避免 stale executor寫入。 |
| Release | 只解除 temporary authority，不刪除 Pending／Block／Intent／Processed。 |
| Reclaim | 已安全證明 abandonment，並重新讀所有 per-event durable state後才繼續。 |

Claim不得永久卡死 Event；也不得僅因 process restart就盲目奪取仍有效 authority。Abandonment判斷，以及 attempt token、fencing token、lease、CAS、transaction或其他保護技術均為 Implementation Choice；observable stale-executor protection semantics必須符合本節。

## 8.3 Concurrency conflicts

- Concurrent same Event只有一方可建立 first Pending或 Intent。
- Unique Processed constraint是最後防線，不可只依 in-memory mutex。
- Matching same-operation retry可 idempotent成功。
- 不同 operation或不同 terminal target競爭時 Fail Closed。

---

# 9. Durable Mutation Intent 與 Crash Consistency

## 9.1 Terminal transition protocol

```text
1. hold valid per-event claim
2. resolve state and validate terminal CorrelationDecision
3. persist durable CorrelationMutationIntent
4. invoke SPEC-008 or SPEC-010 idempotently by event_id / operation_id
5. confirm authoritative downstream result matches Intent
6. atomically establish ProcessedCorrelationRecord
   + resolve matching Intent
   + deactivate matching Pending
   + resolve matching Block
7. release claim
```

步驟3必須先於 downstream side effect。步驟6在 Correlation State Store內必須具有 atomic state-transition semantics，禁止先 delete Intent後才建立 Processed。

PoC不要求 cross-store distributed ACID或2PC。Crash consistency來自 durable intent、idempotent downstream mutation、reconciliation與 single terminal ownership。

## 9.2 Decision-to-outcome mapping

| SPEC-006 Decision | Intended terminal outcome | Domain owner |
|---|---|---|
| `ATTACH_EXISTING` | `ATTACHED_TO_INCIDENT` | SPEC-008 |
| `CREATE_NEW` | `CREATED_INCIDENT` | SPEC-008 |
| `ROUTE_SHADOW` | `SHADOWED` | SPEC-010 |
| `ENTER_PENDING` | 非 terminal，不建立 Intent | SPEC-007 Pending |

SPEC-007只驗證 mapping與保存 intent，不執行 Incident／Shadow mutation。

## 9.3 Crash-point matrix

| Crash point | Durable state | Allowed recovery | Forbidden behavior | Expected idempotency |
|---|---|---|---|---|
| A. Intent前 | 原 per-event state；無新 Intent、無 downstream side effect | 重新 resolve state；若仍合法可重新 evaluate或依原 Pending phase執行 | 假設 side effect成功、直接建 Processed、刪除 Pending | 尚無 operation；只有取得新 claim的合法 path可繼續 |
| B. Intent後、downstream mutation前 | Unresolved Intent保存完整 same-operation context | 不 reevaluate；以相同 `operation_id`呼叫相同 domain mutation | 建新 Decision、換 target、換 policy、建立第二 Intent | same operation首次執行或重播得到相同 authoritative result |
| C. Downstream success後、Processed前 | Unresolved Intent；domain store已有 authoritative result | 查詢／重播 same operation，確認同一結果，再 finalize Processed | 再做新 correlation、建立第二 Incident／Shadow、因不確定而刪 Intent | downstream以 `event_id`／`operation_id`回相同 Incident／Shadow／attach confirmation |
| D. Processed建立後、Intent resolution前 | 若 local atomicity正確，此狀態不應可見；若 legacy／stale matching bookkeeping可見，Processed支配 | 驗證 Intent與 Processed完全一致後 deterministic resolve stale Intent | 重做 downstream mutation；conflict時 last-write-wins | matching cleanup可重複；conflict Fail Closed |

Repeated recovery本身必須 idempotent；任何 crash點再次發生都不得改變 terminal outcome。

---

# 10. Restart Recovery 與 Startup Reconciliation

## 10.1 Startup Recovery Barrier

Runtime接受正常新 correlation work前，必須先協調完成：

- 完整 load／reconstruct並驗證 unresolved `CorrelationMutationIntent`、Active Pending、Blocked及 stale／reclaimable Processing Claim等 recovery-relevant active state；
- 對上述 active state執行 structural及必要 semantic／referential validation，使每個 recovery action可安全 resolve；
- 建立可靠的 Processed lookup／dedup／ownership detection by `event_id`，以及 Intent、Pending、Blocked與 Claim的 safe lookup／protection；
- 防止已有 state的 Event誤判 `UNSEEN`。

Startup不要求掃描完整 historical EventStore，也不要求 cross-check每一筆 historical Processed對每一筆 historical Incident／Shadow，更不要求所有 Blocked Event先 repair。Historical Processed的 cross-store reference validation可在該 `event_id` replay／lookup、recovery／reconciliation涉及該 Event，或 controlled integrity audit明確掃描歷史資料時執行。Store-level corruption若使 recovery-relevant state enumerate／classify或 Processed ownership lookup不可靠，必須 startup Fail Fast。

SPEC-007負責 state consistency invariants、state-domain validation semantics、Fail Closed、per-event ownership bookkeeping與 recovery state interpretation。SPEC-011／recovery orchestration可協調 read-only查詢 EventStore、Incident Store與 Shadow Store以確認 authoritative evidence／result；SPEC-007不因此取得 Incident／Shadow authority，也不成為 normal runtime poller或 duplicate downstream data authority。

## 10.2 Recovery actions by precedence

- `TERMINAL_PROCESSED`：no-op；matching stale Intent只做 deterministic local reconciliation。
- `UNRESOLVED_MUTATION_INTENT`：retry／reconcile same operation，不做 SPEC-006 reevaluation。
- `ACTIVE_PENDING + BLOCKED`：先依 disposition限制；repair後依原 expiry決定 phase。
- `ACTIVE_PENDING`：保留原 timestamps、policy semantics與 exact version；依 now決定 phase。
- `BLOCKED`：`NON_RETRYABLE`維持 blocked；`REPAIR_REQUIRED`等待 repair；`RETRYABLE`由 SPEC-011依 cadence觸發。
- `UNSEEN`：才可進 INITIAL。

## 10.3 Restart boundary cases

```text
t+0  enter Pending
t+25 crash
t+25 restart
→ expires_at不變，remaining grace約5秒

t+31 restart
→ PENDING_EXPIRED
→ final reevaluation
→ not unconditional CREATE_NEW
```

Restart不得 reset Grace、改 exact policy、清除 Block或把 unresolved Intent降級成 Pending／UNSEEN。Repeated crash／restart不得改變 terminal outcome。

> **Restart must preserve semantics, not merely restore availability.**

---

# 11. Persistence Integrity 與 Fail Closed

## 11.1 Validation layers

### Structural validation

至少驗證 required fields、closed enum values、timestamp／reference types、stored representation的 schema semantics可被可靠辨識且受支援，以及 record可解析性。這不要求每筆 record具有 literal `schema_version` field；version metadata placement屬 Implementation Choice。Malformed record不得 silent skip。

### Semantic／referential validation

至少驗證：

- Pending `correlation_policy`與 exact policy evidence class一致；
- Pending expiry公式與 immutable continuity一致；
- Processed outcome與 destination reference shape一致；
- Intent Decision／outcome mapping與 target shape一致；
- 對 recovery／replay／lookup所涉及 Event，驗證 Processed與 Intent／domain authoritative result一致；
- `event_id`沒有 conflicting terminal ownership；
- 對需要 cross-store reconciliation的 Event，Incident／Shadow references由相應 authority read-only確認，或明確判定 dangling。

Startup必須完整驗證 recovery-relevant active state並建立可靠 Processed by-event lookup，但本節不要求 startup exhaustive掃描所有 historical Processed或對所有歷史 Incident／Shadow作 full cross-store audit。

## 11.2 Failure granularity

| Granularity | Required behavior |
|---|---|
| Per-event integrity failure | 建立／呈現 `REPAIR_REQUIRED`，停止該 Event authoritative mutation；可安全隔離時 unrelated Events繼續。 |
| Store-level corruption | 若無法可靠 load、enumerate、classify或 identify ownership，Startup Fail Fast。 |

## 11.3 State-domain taxonomy

PoC conceptual taxonomy至少包含：

```text
MALFORMED_STATE_RECORD
DANGLING_EVENT_REFERENCE
DANGLING_INCIDENT_REFERENCE
DANGLING_SHADOW_REFERENCE
TERMINAL_OWNERSHIP_CONFLICT
MUTATION_INTENT_CONFLICT
UNSUPPORTED_STATE_VERSION
STORE_INTEGRITY_FAILURE
```

這些是 SPEC-007 persistence／state-domain failures，不得塞入 SPEC-006 `CorrelationErrorCode`，也不得送 Shadow或 learning flow。

若 Processed／recovery state引用 `shadow_ref`，但 authoritative Shadow Store無法確認該 Shadow，回 `DANGLING_SHADOW_REFERENCE`、Fail Closed並標為 `REPAIR_REQUIRED`。不得建立 replacement Shadow、改寫 terminal ownership、刪除 Processed後重新 route Shadow，也不得將此 integrity failure誤認為 UNKNOWN anomaly或送入 RCA／RAG／SOP learning。

## 11.4 Recovery versus repair

可自動 deterministic recovery：crash interruption、matching stale bookkeeping、same-operation retry。

不可猜測：malformed state、dangling authoritative reference、terminal conflict、cross-store contradiction、unsupported version。

禁止 last-write-wins、latest timestamp wins、silent skip、automatic overwrite、automatic data relocation或 automatic destructive repair。唯一允許的 deterministic cleanup是 fully matching Processed + stale Intent；若任一 identity／target／outcome不一致，Fail Closed。

> **Interruption is recoverable; contradiction is not guessable.**

> **We automate recovery, not authority repair.**

---

# 12. Retention／Reset／Cleanup Governance

## 12.1 PoC retention rule

PoC不因 time、Incident lifecycle、`CLOSED`、restart、Pending expiry、validator或 runtime error自動刪除 Correlation State。

`expires_at`是 Pending evaluation phase boundary，不是 retention TTL。Pending／Block／Intent解除 active semantics不代表 historical evidence必須 erase。

## 12.2 Processed durability

Processed Ledger不得獨立 TTL prune。只要 EventStore仍可能 replay Event，刪除 Processed就可能使 Event重新成為 `UNSEEN`，破壞 idempotency與 ownership。

任何 future retention／archive必須跨 EventStore、Correlation State與 domain ownership建立一致治理，不在本 SPEC實作。

## 12.3 Reset and destructive cleanup

只允許：controlled dev／test、major schema／contract migration、或 PM explicitly authorized maintenance。事前必須界定 affected state並維護 referential integrity。

Normal runtime、startup recovery、validator、test failure handler與 AI coding agent不得執行 destructive reset／cleanup。Validator只能 detect、report、fail，不得 clear state以讓測試通過。

Physical compaction若 logically lossless可作 Implementation Choice；不得使任何 Event失去 dedup／recovery memory。

Historical replay／reclassification不得以刪除 Processed／Pending／Block後假裝 `UNSEEN`實現；未來必須另立 governed workflow。

> **Cleanup must never erase idempotency. A reset is an administrative operation, not a recovery strategy.**

---

# 13. Public API Semantic Capabilities

本章定義 capability，不規定 Python method name、sync／async形式或 physical adapter。

| Capability | Preconditions | Postconditions | Failure semantics |
|---|---|---|---|
| Resolve authoritative per-event state | Valid `event_id`；store可讀 | 依固定 precedence回一個 resolved state與必要 references | Malformed／conflict不可回 `UNSEEN`；per-event Fail Closed或 store-level failure |
| Enumerate recovery-relevant states | Startup Barrier內；一致讀取能力 | 完整列出 unresolved Intent、Active Pending、Blocked及需處理 stale Claim；建立可靠 Processed by-event lookup | 無法完整可靠 enumerate active state或建立 ownership lookup時 Startup Fail Fast；不要求 exhaustive historical cross-store audit |
| Acquire processing authority | 已 resolve state；無有效 competing holder | caller取得 temporary per-event authority | 競爭失敗或已失去 authority者不得繼續 mutation；保護 mechanism屬 Implementation Choice |
| Release processing authority | Caller持有有效 claim | 解除 temporary authority，不改 durable domain state | Stale／foreign release拒絕 |
| Safe reclaim abandoned authority | Abandonment已可靠成立 | 取得新 authority並重新 resolve all state | 無法證明 abandonment則拒絕，不猜測 |
| Create／read Active Pending | SPEC-006 `ENTER_PENDING`；valid claim；無 terminal／Intent | 唯一 Pending建立，absolute expiry與 exact policy固定 | Duplicate matching可 idempotent read；conflict拒絕 |
| Update pending reason | Existing Pending；SPEC-006仍回 `ENTER_PENDING` | 只更新 reason，其餘 immutable | 修改 grace／policy嘗試拒絕 |
| Determine Pending phase | Existing valid Pending；authoritative now | `now < expiry`→RECHECK，否則 EXPIRED；產生 exact context | Invalid time／record Fail Closed |
| Record／read／resolve Block | Evaluation／state／internal failure已分類 | Safe metadata durable；`evaluation_phase`與 policy refs只在適用時保存；Pending可保留；safe `ENTER_PENDING`或 terminal success可 resolve | Restart／retry開始／operator要求不得直接 resolve；不得將 Block當 Processed／Shadow |
| Begin Mutation Intent | Terminal Decision；valid claim；無 Processed／conflicting Intent | Durable same-operation intent先於 side effect存在 | Matching retry idempotent；conflicting operation Fail Closed |
| Read／reconcile Intent | Unresolved Intent | 提供 same-operation context並確認 authoritative result | 禁止重新 correlation；contradiction Fail Closed |
| Finalize terminal outcome atomically | Downstream success已確認且符合 Intent | 建 Processed、resolve Intent、deactivate Pending、resolve matching Block | Any mismatch拒絕；不可產生 partial local terminal state |

Store不能完全相信 caller；每個 capability都必須重新驗證 legal transition與 cross-record invariant。Store也不得自行產生 business correlation Decision。

---

# 14. State Transition／Invariant Tables

## 14.1 Legal transitions

| From | Trigger | To | Required invariant |
|---|---|---|---|
| `UNSEEN` | SPEC-006 `ENTER_PENDING` | `ACTIVE_PENDING` | exact policy一致；absolute expiry只計算一次 |
| `UNSEEN` | Terminal Decision | `UNRESOLVED_MUTATION_INTENT` | Intent durable before side effect |
| `ACTIVE_PENDING` | Recheck仍 `ENTER_PENDING` | `ACTIVE_PENDING` | 只可更新 reason，不 reset grace |
| `ACTIVE_PENDING` | Evaluation Failure | `ACTIVE_PENDING + BLOCKED` | Pending immutable continuity |
| `BLOCKED` | Safe retry仍 failure | `BLOCKED` | attempts單調、disposition合法 |
| `BLOCKED` | Safe reevaluation回 `ENTER_PENDING` | `ACTIVE_PENDING` | 建新 Pending；以 authoritative now起算 Grace；resolve matching Block |
| `ACTIVE_PENDING + BLOCKED` | Safe reevaluation回 `ENTER_PENDING` | `ACTIVE_PENDING` | 保留原 Pending／Grace／exact policy；可更新 reason；resolve matching Block |
| `ACTIVE_PENDING`／`BLOCKED`／`ACTIVE_PENDING + BLOCKED` | Terminal Decision | `UNRESOLVED_MUTATION_INTENT`並保留 recovery context與 Block直到 finalize | same exact policy；valid claim；Block只在 terminal success後 resolve |
| `UNRESOLVED_MUTATION_INTENT` | Authoritative success confirmed | `TERMINAL_PROCESSED` | atomic local finalization |
| `TERMINAL_PROCESSED + matching stale Intent` | Reconciliation | `TERMINAL_PROCESSED` | 完全 matching，只清 bookkeeping |

## 14.2 Illegal transitions

| Illegal transition／attempt | Required response |
|---|---|
| `TERMINAL_PROCESSED → ACTIVE_PENDING` | Reject／Fail Closed |
| Pending policy v1.0 overwrite為 v1.1 | Reject／Fail Closed |
| Pending expiry／entry timestamp reset | Reject |
| Terminal Incident A改成 Incident B或 Shadow | `TERMINAL_OWNERSHIP_CONFLICT` |
| Intent OP-A改成 conflicting OP-B | `MUTATION_INTENT_CONFLICT` |
| Unresolved Intent後重新跑 SPEC-006 | Reject orchestration path |
| Malformed record被忽略並回 `UNSEEN` | Integrity failure |
| Blocked被直接轉 Shadow | Reject |

## 14.3 Record coexistence and precedence

| Records | May coexist? | Dominant interpretation |
|---|---:|---|
| Pending + Blocked | Yes | `ACTIVE_PENDING + BLOCKED`；Block先限制 retry，Pending continuity保留 |
| Pending + unresolved Intent | Temporarily yes during terminal transition | Intent支配；不 reevaluate |
| Blocked + unresolved Intent | Temporarily yes | Intent支配；Block可在 successful finalize時 resolve |
| Processed + matching stale Intent | Only as recoverable stale bookkeeping／legacy observation | Processed支配；matching cleanup only |
| Processed + conflicting Intent | Invalid | Fail Closed |
| Multiple Processed for same Event | Invalid | Terminal ownership conflict |
| Claim + any durable state | Yes | Claim只控制 temporary execution，不改 precedence |

## 14.4 Terminal finalization invariant

```text
confirmed downstream authoritative success
AND matching durable Intent
AND valid processing authority
→ atomically:
   establish unique Processed
   resolve matching Intent
   deactivate matching Pending
   resolve matching Block
```

若 Processed已完全 matching，finalization是 idempotent confirmation；任何 outcome／destination／policy／operation contradiction均 Fail Closed。

---

# 15. Failure／Error Taxonomy

## 15.1 Separation of domains

| Domain | Representation | Examples | Owner |
|---|---|---|---|
| SPEC-006 correlation-domain failure | `CorrelationEvaluationError`／`CorrelationErrorCode` | invalid Event、missing identity、policy unavailable、invalid View | SPEC-006定義；SPEC-007保存 Block mapping |
| SPEC-007 persistence／state-domain failure | 獨立 state error taxonomy | malformed record、dangling reference、ownership／Intent conflict | SPEC-007 |
| Internal failure | `failure_kind=INTERNAL_FAILURE` + safe metadata | unexpected programming／library error | Runtime捕捉邊界；SPEC-007 durable記錄 |

不得將三者混為單一 enum，也不得以 SPEC-006 `INVALID_EVENT_ENVELOPE`掩蓋 state corruption或 internal bug。

## 15.2 State error behavior

| Code | Default behavior |
|---|---|
| `MALFORMED_STATE_RECORD` | Per-event可隔離則 `REPAIR_REQUIRED`；否則 store-level Fail Fast |
| `DANGLING_EVENT_REFERENCE` | `REPAIR_REQUIRED`；停止該 Event mutation |
| `DANGLING_INCIDENT_REFERENCE` | `REPAIR_REQUIRED`；不得換 target猜測 |
| `DANGLING_SHADOW_REFERENCE` | Fail Closed／`REPAIR_REQUIRED`；不得建立 replacement Shadow、改寫 ownership或刪除 Processed重送 Shadow |
| `TERMINAL_OWNERSHIP_CONFLICT` | Fail Closed／`REPAIR_REQUIRED` |
| `MUTATION_INTENT_CONFLICT` | Fail Closed／`REPAIR_REQUIRED` |
| `UNSUPPORTED_STATE_VERSION` | 不讀寫未知 semantics；per-event或 store-level依可隔離性處理 |
| `STORE_INTEGRITY_FAILURE` | Startup Fail Fast |

Failure message不得成為 machine decision source；automation依 typed code、disposition與 references。

---

# 16. Acceptance Criteria

## AC-007-A — Active Pending

- `ENTER_PENDING`建立唯一 `ActivePendingRecord`，包含兩種 required timestamps、`correlation_policy`、exact policy reference與 valid reason。
- PoC合法 persisted `correlation_policy`只有 `STRONG_ANCHOR`與`WEAK_SUPPORTING_KNOWN`；`UNKNOWN_SUPPORT_ONLY`只為 PRD-level example，不是合法 Pending值。
- `entered_pending_at`與 `expires_at`在 restart／retry／reason change後不變。
- `NO_COMPATIBLE_CANDIDATE ↔ MULTIPLE_COMPATIBLE_CANDIDATES`可更新，不延長 Grace。
- 不持久化 candidate IDs／authoritative candidate snapshot。
- exact policy與 `correlation_policy` mismatch Fail Closed／Repair Required。
- registered UNKNOWN Decision不得建立 Pending。

## AC-007-B — Processed／Dedup

- Processed只在 downstream authoritative success確認後建立。
- 同 `event_id`只能有一個 terminal outcome與相符 destination。
- 完全相同 replay為 idempotent no-op，不 reevaluate、不 duplicate side effect。
- 不同 outcome／Incident／Shadow replay Fail Closed。
- Terminal ownership不妨礙 RCA／human／Dashboard／analytics讀 Event。

## AC-007-C — Blocked／Failure

- 所有 SPEC-006 error codes依第6章映射正確 disposition。
- Failure不建立 Processed、不當 normal Pending、不 route Shadow。
- Existing Pending failure後形成 Pending + Block，且 grace／policy continuity不變。
- Blocked-only Event safe reevaluation回 `ENTER_PENDING`時，以 authoritative now建立新 Pending並 resolve matching Block。
- Pending + Block safe reevaluation回 `ENTER_PENDING`時，保留原 timestamps／policy、必要時更新 reason並 resolve matching Block。
- Repaired reevaluation回 terminal Decision時，Block保持 active至 downstream success與 atomic terminal finalization；restart、retry開始或 operator要求不能直接 resolve。
- `NON_RETRYABLE`不 busy-loop；`REPAIR_REQUIRED`不因 restart自動 retry。
- Internal failure保存 `INTERNAL_FAILURE` safe metadata且至少 `REPAIR_REQUIRED`。
- Evaluation-origin Block保存真實 phase；無合法 SPEC-006 evaluation context的 state／startup／recovery／internal failure使用 `evaluation_phase=null`且不猜 policy refs。
- Block不直接進 RCA／RAG／SOP learning。

## AC-007-D — Single-flight

- Concurrent same `event_id`最多一個 holder取得 authoritative processing資格。
- Losing path不能建立／推進 Intent或 Processed。
- Abandoned claim可安全 reclaim且 Event不永久卡死。
- Reclaim前重新 resolve Processed、Intent、Pending與Blocked。
- Stale holder不能覆寫新 holder結果；mechanism可替換但 invariant不變。

## AC-007-E — Crash Consistency

- A～D四個 crash points均依第9.3矩陣 recovery。
- Downstream side effect前一定存在 durable Intent。
- Intent recovery只 retry／reconcile same operation，不重新 correlation。
- Downstream success後 crash仍能確認同一 result並 finalize Processed。
- Processed建立與 Intent／Pending／Block local resolution具 atomic semantics。
- Repeated recovery crash仍不改 terminal outcome或 duplicate side effect。

## AC-007-F — Restart Recovery

- Startup Recovery Barrier完成前不接受 normal new work。
- Startup完整載入／驗證 unresolved Intent、Active Pending、Blocked與 stale／reclaimable Claim，並建立可靠 Processed lookup／dedup by `event_id`。
- Restart前 expiry仍保留剩餘時間；restart後 `now < expires_at`為 `PENDING_RECHECK`。
- Restart後 `now >= expires_at`為 `PENDING_EXPIRED`；`now == expires_at`亦為 expired。
- Expired仍進 latest Views final reevaluation，不 unconditional create。
- Recovery沿用 exact historical policy；unavailable時 Block為 `REPAIR_REQUIRED`，不 fallback latest。
- 不要求完整 historical EventStore scan，也不要求 startup cross-check所有 historical Processed對所有 Incident／Shadow；按 replay／lookup／recovery或 controlled audit需要驗證。Repeated restart idempotent。

## AC-007-G — Integrity／Fail Closed

- Structural validation拒絕 malformed required field、enum、timestamp與 unsupported schema semantics；version placement與 time serialization保持 implementation-neutral。
- Semantic validation偵測 dangling Event／Incident／Shadow references、Intent conflict與 terminal ownership contradiction。
- `DANGLING_SHADOW_REFERENCE`必須 Fail Closed／`REPAIR_REQUIRED`，不得 replacement Shadow、改寫 ownership、刪除 Processed重送或進 learning flow。
- Malformed record不會被 silent skip成 `UNSEEN`。
- 可隔離 per-event failure不阻止 unrelated Events；whole-store unreadable／不可 enumerate時 Startup Fail Fast。
- Matching Processed + stale Intent可 deterministic cleanup；不一致時 Fail Closed。
- 無 last-write-wins、timestamp guessing或 automatic authority repair。

## AC-007-H — Retention／Cleanup

- PoC沒有 automatic retention deletion。
- Pending expiry不當 TTL；unresolved Pending／Block／Intent不因 age消失。
- Processed不能獨立 TTL prune，replay後仍保持 dedup。
- Validator只能 detect／report／fail；不能 clear state。
- Runtime／startup／AI coding agent不能 destructive reset。
- Lossless compaction不改 logical state；historical replay不靠刪 state偽裝 `UNSEEN`。

## AC-007-I — Boundary／Integration Contract

- SPEC-006保持 pure、deterministic、persistence-blind、wall-clock blind；SPEC-007不 duplicate matching。
- SPEC-007依 authoritative now決定 Pending phase；SPEC-011只提供 now與 scheduling，不 bypass phase determination。
- SPEC-008 attach／create與 SPEC-010 Shadow mutation均以 `event_id`／`operation_id` idempotent，same replay回同一結果。
- Store只 validate transition；Engine決定 semantics；Runtime orchestration；Domain Stores執行 side effects。
- EventStore保持 immutable，`Event.status`不表示 correlation state。
- State records不含 full Event／Incident／Shadow、candidate snapshot、scenario／generator／validator answers。

## 16.1 Required implementation test layers

Implementation acceptance至少需要：

1. Targeted unit tests：record validation、config、phase boundary、precedence、transition tables、error mapping與 cleanup guards。
2. Persistence contract tests：durability、unique constraints／等價保護、local atomic finalization與 reopen／restart reconstruction。
3. Cross-SPEC integration tests：使用真實 SPEC-006 Decision／Failure／Context types，並以 SPEC-008／010 contract doubles驗證 idempotent handoff；不得 duplicate fake correlation enums。
4. Concurrency tests：same-event claims、stale holder與 conflicting finalization。
5. Crash-injection tests：第9.3 A～D及 repeated recovery crash。
6. Full repository regression：`python -m pytest -q`或 Repository正式 equivalent command。

不得用固定 test數量取代 contract coverage。完整 downstream Docker Correlation E2E待 SPEC-008～011整合完成後執行。

---

# 17. Out of Scope／Future Work

本 SPEC不實作或鎖定：

- physical database／JSONL／SQLite／Redis選擇；
- distributed transaction、2PC或 cross-store distributed ACID；
- production retention engine與 archival policy；
- full Admin Repair Tool；
- automatic schema migration；
- historical replay／reclassification workflow；
- SPEC-008 Incident Store／Manager implementation；
- SPEC-010 Shadow Store implementation；
- SPEC-011 Runtime Orchestration implementation；
- RCA／RAG／SOP learning；
- Dashboard／notification；
- HA、distributed workers、Kafka或 production-scale coordination；
- Policy hot reload與 migration；
- physical lock／lease／CAS／transaction implementation；
- storage file／table／index layout。

Future decisions必須保持本文件的 idempotency、single ownership、exact policy continuity、crash consistency與 authority boundaries。

---

# 18. Implementation Handoff／Governance

Implementation Owner為 **Tako**。文件目前為 `Approved — Implementation Pending`；Engineering Contract已完成 Second PM Review，本次 approval closure不開始 implementation，也不宣稱 Implemented。

Implementation agent必須：

- 依已核准的 SPEC-007 v1.0 Engineering Contract進入受治理的 implementation；
- 不修改 PRD-002、PRD-003或 SPEC-006 authority；
- 不實作 speculative SPEC-008／SPEC-010／SPEC-011 domain behavior；
- 不執行 destructive reset／cleanup以通過測試；
- 不依賴個人 local workaround或 Scenario answer leakage；
- 發現 active conflict時停止受影響範圍並回報 PM。

AI coding agent不得自行執行 Git operation或 destructive state cleanup；版本控制與 destructive maintenance須由 PM明確授權的流程處理。

更新為 `Implemented`前至少必須具備：

1. 符合本 SPEC的 production implementation；
2. targeted unit與 persistence contract tests通過；
3. 使用真實 SPEC-006 contracts的 integration tests通過；
4. concurrency與 crash recovery tests通過；
5. full repository regression通過；
6. PM Final Review確認 implementation evidence與 scope compliance。

> **Approved ≠ Implemented。** Approval只表示 Engineering Contract可進入實作；不表示 Correlation State persistence或完整 Alert Correlation Runtime已完成。

---

# 19. PM Review Checklist

> **Second PM Review Result：PASS — 2026-09-04。** 本 checklist已完成 PM核准；後續若 implementation發現 active authority conflict，仍須停止受影響範圍並回報 PM。

- [x] Metadata為 SPEC-007 v1.0／`Approved — Implementation Pending`／2026-09-04，Owner為 Tako。
- [x] Authority正確引用 PRD-003 v1.0 Final、PRD-002 v1.5、SPEC-006 v1.0 Implemented。
- [x] D1～D12與 D2／D12 narrow clarifications完整且未重新設計。
- [x] EventStore immutable；未修改15-field schema或使用 `Event.status`表示 correlation state。
- [x] Active Pending同時保存 `correlation_policy`與 exact policy reference，且無 candidate snapshot。
- [x] PoC Active Pending policy只有 `STRONG_ANCHOR`／`WEAK_SUPPORTING_KNOWN`；`UNKNOWN_SUPPORT_ONLY`不是合法 persisted value。
- [x] Pending使用 absolute expiry，restart不 reset，`now == expires_at`為 expired。
- [x] Processed只在 downstream success後建立並 enforce single terminal ownership。
- [x] Pending、Blocked與 Shadow語意清楚分離。
- [x] Block只在 safe reevaluation重新進入 Pending或 terminal downstream成功後 resolve；Pending + Block路徑不 reset Grace。
- [x] Block `evaluation_phase`與 policy refs為 conditional／nullable，且不得 invent值。
- [x] Mutation Intent先於 side effect，recovery只 retry same operation。
- [x] MutationIntent使用 SPEC-006 `target_incident_id`，不保存 diagnostics或預先 invent destination reference。
- [x] Startup precedence與 crash-point matrix完整。
- [x] Startup完整處理 active recovery state與 Processed lookup，但不要求 exhaustive historical cross-store audit。
- [x] Integrity區分 interruption與 contradiction，無 auto repair／last-write-wins。
- [x] `DANGLING_SHADOW_REFERENCE`具 Fail Closed／Repair Required contract。
- [x] Processed不獨立 TTL prune；validator／AI agent不得 destructive cleanup。
- [x] Public API只鎖 semantic capabilities，不鎖 physical implementation。
- [x] AC-007-A～I可直接轉換為 targeted tests。
- [x] SPEC-008／010／011 boundary保持鬆耦合且未提前實作。
