# PRD-003 — Alert Correlation & Incident Management

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | PRD-003 |
| Document Name | Alert Correlation & Incident Management |
| Version | 1.1 |
| Status | Final |
| Date | 2026-09-18 |
| Author | 林子豪（PM） |
| Upstream Authority | PRD-002 v1.5 Approved／EventStore normalized Runtime Event evidence |
| RCA Domain Authority | PRD-004 v1.0 Approved／Evidence-Grounded LLM + RAG Root Cause Analysis |
| Target | Product、Engineering SPEC、QA／Validation、Operations |

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-29 | Initial Final requirements for evidence-driven Alert Correlation, Incident lifecycle/state, Pending/Dedup recovery, Shadow routing, and persistence governance. |
| 1.1 | 2026-09-18 | Post-PRD-004 v1.0 backward reconciliation：clarify Incident coarse RCA availability／Current relationship projection、`rca_ref` meaning、Assignment-independent RCA eligibility及Material Evidence authority handoff。此為narrow boundary clarification，不變更Strong／Weak、Pending、Incident lifecycle、ownership、Shadow或assignment behavior。 |

> **Implementation Status Honesty：**本文件是 Final Requirements，不代表implementation complete。SPEC-006～011既有核准scope已各自Implemented；SPEC-008／011新增的RCA integration boundary、RCA／RAG workflow、PRD-005 interfaces及完整AIOps closed loop仍為Implementation Pending。不得將本文件解讀為RCA Implemented、Production Ready或Completed。

---

## 1. 文件目的與產品定位

本文件定義normalized Events如何依Runtime evidence收斂為Incident，以及Incident identity、ownership、lifecycle、persistence與human workflow的產品契約。Incident是Jira、Discord、Dashboard與RCA等後續operational workflow的authoritative context，但本文件不規定其adapter或UI實作。

產品目標：

1. 將具有operational relationship的多筆Events收斂為Incident。
2. 避免duplicate Incident與false correlation。
3. 依evidence strength採取不同correlation policy。
4. Strong evidence可建立或確認Incident identity。
5. Weak evidence只在安全且唯一context下attach。
6. Ambiguity不猜測，temporary over-segmentation優於false correlation／operational suppression。
7. Unknown anomaly不直接驅動不安全的operational workflow。
8. Incident lifecycle與correlation eligibility分離。
9. Event ownership、restart recovery與dedup皆可追蹤。
10. Incident提供後續operational authority context，但不duplicate完整Event或RCA payload。

---

## 2. 上游Authority Boundary與禁止Answer Leakage

PRD-002是Event Detection與15-field Event Schema的authority；PRD-003不得重新定義或要求修改該contract。EventStore是immutable normalized Event evidence authority，Correlation Engine只能使用正式Runtime Event evidence。

禁止以以下資料作為Runtime correlation decision source：

- `scenario_id`。
- Mock Generator current scenario或internal state。
- Validator expected answers或validator-only metadata。
- Hardcoded scenario mapping。

> **Policy selection is evidence-driven, not scenario-driven.**

> **Different telemetry carries different types and strengths of identity evidence.**

Upstream Event是immutable evidence。PRD-003不得要求改寫`Event.status`以表示`PENDING`、`CORRELATED`或`CONSUMED`；Pending、Processed與Dedup由Correlation State保存。

---

## 3. Core Concepts

### 3.1 Event

由PRD-002定義並持久化於EventStore的immutable detection evidence。Correlation只保存reference與processing state，不取得Event mutation ownership。

### 3.2 Incident

代表一次operational episode的工作單位。Incident保存ordered unique Event references與authoritative operational state，不duplicate完整Event payload。

### 3.3 Correlation Window

Correlation Window可設定，PoC default為`120 seconds`，採rolling episode continuity：

```text
event.detected_at - incident.last_correlated_at <= correlation_window
```

它是candidate eligibility constraint，不是wall-clock countdown timer。Compatible Event成功attach後更新`last_correlated_at`，使ongoing episode可持續延伸。

Window expiration只表示舊Incident不再對新Event具correlation eligibility；不自動改變lifecycle，也不自動CLOSED Incident。

> **Correlation Window determines whether an Incident can accept new correlated evidence; Incident Lifecycle determines the operational handling state of the existing Incident.**

### 3.4 Pending Grace

Pending Grace可設定，PoC default為`30 seconds`，是Pending Event的最大等待期限，不是Correlation Window。

> **Pending Grace protects responsiveness; Correlation Window protects episode continuity.**

Grace期間每次reevaluation都必須依最新Incident state重新計算candidate set，不得將進入Pending時的candidate snapshot作為authoritative decision source。Correlation eligibility改變不等同Grace expiration；Grace未到期且沒有唯一candidate時持續等待，直到出現唯一candidate或Grace expires。

---

## 4. Correlation Candidate Lifecycle

只有以下Incident status是correlation-open／eligible：

- `OPEN`
- `ASSIGNED`
- `IN_PROGRESS`

`OPEN`／`ASSIGNED`／`IN_PROGRESS`代表episode仍可能演進，可attach compatible evidence，但不得reset lifecycle、assignee、reviewer、incident_id、既有RCA或assignment workflow。這些updates可形成RCA-relevant technical evidence並必須audit；何者構成Material Evidence、是否使Current RCA成為STALE及是否建立refresh obligation，均由`PRD-004 v1.0 Approved`定義。

`AWAITING_REVIEW`表示remediation已宣告完成、Resolution Evidence已提交，正在等待review與recovery verification，因此不再correlation-open。新Event不得normal attach回該Incident；新Strong Anchor形成新episode時建立New Incident。Recurrence evidence可供review參考，但不得把舊Incident自動backward transition至`IN_PROGRESS`。

`CLOSED`是terminal，永不reopen且永不作為candidate。後續recurrence建立New Incident。

---

## 5. Known Strong Anchor Contract（S1～S6）

| Scenario | Strong Anchor Event | Normalized Fingerprint | 排除／Supporting Evidence |
|---|---|---|---|
| S1 | `brute_force_detected` | `event_type + source_ip` | `source_ip`只能來自Runtime Event；不得使用Scenario metadata。 |
| S2 | `cross_service_failure` | `event_type + trace_id` | `trace_id`是identity；`downstream_service`、`affected_services`是diagnostic／RCA evidence。Known Weak：`high_latency_detected`。 |
| S3 | `oom_crash_detected` | `event_type + service_name` | PRD-003依賴已Closure的upstream contract：`service_name`代表actual OOM-origin service。不得使用`unique_services[0]`或generator／scenario metadata。Known Weak：`high_memory_detected`；memory value／`max_memory_pct`只屬diagnostic／RCA。 |
| S4 | `external_dependency_failure` | `event_type + external_service` | 排除`service_name`（caller／affected service）與`transaction_id`（diagnostic／RCA）。 |
| S5 | `downstream_cascade_failure` | `event_type + downstream_service` | 排除`service_name`與`trace_id`。Affected services是blast-radius／RCA evidence；同downstream、rolling-window compatible且唯一candidate時，set可演進而不改identity。 |
| S6 | `rate_limit_storm` | `event_type + triggered_features.target_service` | 不假設top-level `target_service`。排除`service_name`與`rate_limit_quota`。Known Weak：`request_spike_detected`。 |

---

## 6. Strong Anchor Policy

對Strong Anchor Event重新計算compatible、correlation-open candidates：

- Exactly one：attach既有Incident。
- Zero：建立New Incident。
- More than one：不得猜測，進入Pending，`pending_reason=MULTIPLE_COMPATIBLE_CANDIDATES`。

若Pending Grace到期仍無唯一candidate，建立standalone Incident。設計偏好temporary over-segmentation，不接受false correlation或operational suppression。

---

## 7. Known Weak Supporting Policy

Known Weak Events：

- `high_latency_detected`
- `high_memory_detected`
- `request_spike_detected`

處理規則：

- Exactly one compatible candidate：立即attach。
- Zero：Pending，`NO_COMPATIBLE_CANDIDATE`。
- More than one：Pending，`MULTIPLE_COMPATIBLE_CANDIDATES`。

Grace期間每次重新計算candidate；任一時刻出現唯一candidate即立即attach，不必等滿30秒。Grace到期仍無唯一candidate，建立standalone Weak Incident，且不得把Grace自動延長成120秒。

若standalone Weak Incident在rolling Correlation Window內遇到唯一compatible Strong Anchor，允許**Late Strong-Anchor Promotion**：保留原`incident_id`與既有`event_ids`，更新`anchor_event_id`、anchor strength／Event Type、normalized fingerprint與`correlation_context`，並audit promotion；不得建立replacement Incident。

---

## 8. Unknown／Shadow Policy

Unknown anomaly：`general_log_anomaly`、`general_metrics_anomaly`。

PoC v1.0沒有approved safe support rule時，不自動attach、不進Known Weak Pending、不建立operational Incident，直接進Shadow／Unclassified。

> **Known anomaly drives operations; unknown anomaly drives learning.**

Shadow minimum state：

- `shadow_id`
- `event_id`
- `entered_shadow_at`
- `reason`
- `review_status`
- `policy_version`

Reason taxonomy至少支援`INSUFFICIENT_OPERATIONAL_IDENTITY`、`NO_COMPATIBLE_INCIDENT`、`MULTIPLE_COMPATIBLE_INCIDENTS`；v1.0一般`general_*`最常使用第一項，其他reason需未來approved evidence-based compatibility rule才具operational meaning。`review_status`至少支援`UNREVIEWED`。

Shadow v1.0不自動執行clustering、policy／detector／SOP／RAG KB mutation或model retraining。Future learning path可為Shadow aggregation／clustering→pattern mining→knowledge-gap analysis→human review→future Detector／Correlation Policy／SOP／RAG improvement，但不宣稱已實作。

---

## 9. Incident Schema Requirements

Incident至少保存：

- `incident_id`
- ordered unique `event_ids`
- nullable `anchor_event_id`
- lifecycle `status`
- current `severity`
- `created_at`、`updated_at`、`last_correlated_at`
- nullable `closed_at`、`assignee`、`reviewer`
- `correlation_context`
- append-style `audit_trail`
- coarse RCA availability／Current relationship projection，例如`rca_status`、`rca_ref`
- extensible `external_refs`

Incident不得保存完整Event duplicate或完整RCA Artifact。Logical RCA Aggregate、Published Versions、Generation Attempts、Execution retries、Fresh／Stale、refresh lifecycle、Evidence completeness、Diagnostic Conclusion、Evidence／Knowledge snapshots及完整history均由`PRD-004 v1.0 Approved`所治理的RCA Domain擁有。

### 9.1 Timestamp Semantics

- `created_at`：Incident被系統建立／operationalize的時間，不等同第一筆`Event.detected_at`。
- `last_correlated_at`：最近一次成功attach Event的`detected_at`，不是processing wall-clock。
- `updated_at`：任何authoritative mutation都更新，包括attach、severity escalation、promotion、assignment、lifecycle、review與RCA relationship/status mutation。
- `closed_at`：只在正式`CLOSED`時設定。

### 9.2 correlation_context

至少保存current anchor strength、anchor event id、anchor Event Type、normalized fingerprint、promotion／enrichment context及audit所需policy information。普通supporting attach不必改`anchor_event_id`；Late Promotion必須更新anchor id、strength、type與fingerprint。

---

## 10. Correlation State Store

本文件定義logical persistence role，不鎖死JSONL、SQLite、Redis或特定DB engine。

> **Logical Store ≠ Physical Database.**

同一physical transactional persistence可實作多個logical roles，但責任必須清楚分離。

### 10.1 Active Pending

Minimum fields：`event_id`、`entered_pending_at`、absolute `expires_at`、`correlation_policy`、`pending_reason`。Candidate list不得成為authoritative persisted snapshot；每次reevaluation依最新Incident state重算。

`correlation_policy`必須是reusable evidence policy，例如`STRONG_ANCHOR`、`WEAK_SUPPORTING_KNOWN`、`UNKNOWN_SUPPORT_ONLY`，不得使用Scenario ID。

### 10.2 Processed／Dedup Ledger

Ledger至少回答event是否完成processing、terminal outcome及destination。Outcome至少支援：

- `ATTACHED_TO_INCIDENT`
- `CREATED_INCIDENT`
- `SHADOWED`

欄位概念至少包含`event_id`、`outcome`、`resolved_at`及optional `incident_id`／`shadow_ref`，用於idempotency、replay safety、restart recovery與duplicate ownership prevention。

---

## 11. Event Ownership、Idempotency與Restart Recovery

同一`event_id`最多一次terminal correlation ownership resolution；一個Event最多屬於一個Incident。Replay不得造成duplicate Incident、duplicate attach或duplicate Shadow state。Atomicity／transaction technique由後續SPEC定義，但observable behavior必須idempotent與replay-safe。

Pending continuity必須跨restart保留，不能restart後重算完整30秒。Recovery須restorePending／Processed state並比較current time與absolute `expires_at`：未到期保留剩餘Grace；已到期則依最新Incident state重算candidate後terminal resolution。不得依賴crash前candidate snapshot。Startup不要求掃描全部歷史Event；active lookup、index、batch與priority recovery由SPEC設計。

---

## 12. Incident Creation、Assignment與Evidence Attach

Incident先建立並持久化為`OPEN`，之後才執行Assignment Policy：`OPEN → ASSIGNED`。Assignment failure不得rollback Incident；保留`OPEN`供retry、manual assignment與operational handling。

Incident一旦durable，initial RCA obligation即可依`PRD-004 v1.0 Approved`成立，不以`ASSIGNED`或`IN_PROGRESS`為eligibility gate，並可與AUTO_ASSIGN obligation並行。此規則不修改Assignment Policy、assignment order或Incident lifecycle。

對`OPEN`／`ASSIGNED`／`IN_PROGRESS` attach compatible Event時至少：append unique event id、更新`last_correlated_at`與`updated_at`、必要時severity escalation、更新correlation state並append audit trail。

Incident severity等於最高correlated Event severity；correlation-driven mutation只能escalation，不得自動降低。Attach不得reset lifecycle、assignee、reviewer、incident_id或restart Assignment Policy。

---

## 13. RCA Update Governance

`PRD-004 v1.0 Approved`是RCA Domain product requirement authority；PRD-003不定義完整RCA implementation或建立第二套RCA lifecycle。Incident只擁有coarse RCA availability／Current relationship projection：

- `rca_ref`在concept level指向latest successfully published Current RCA version；本PRD不凍結physical type、encoding或storage topology。
- 首次successful Current之前，`PENDING／GENERATING／FAILED`可描述coarse initial availability。
- successful Current存在後，refresh pending／generating／failed期間Incident coarse `rca_status`維持`COMPLETED`，`rca_ref`維持last-known-good Current；完整refresh state與failure outcome由RCA Domain提供。
- Incident不得保存Logical RCA Aggregate、Published Version history、Generation Attempts、execution retries、Fresh／Stale、Evidence completeness、Diagnostic Conclusion、Evidence／Knowledge snapshots或完整RCA Artifact。

Correlation／Incident update可能產生RCA-relevant technical evidence，但何者構成Material Evidence、何時使Current成為STALE以及是否建立refresh obligation，均由PRD-004定義。Compatible Event attach不得silent overwrite既有Current，也不得由PRD-003自行建立materiality或versioning規則。

---

## 14. Incident Lifecycle與Authority

唯一正式lifecycle：

```text
OPEN → ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED
```

必須strict guard：不任意跳階、不自動backward、reassignment不重置lifecycle、`CLOSED`不reopen。所有authoritative lifecycle mutation由Incident Manager驗證與執行。

Jira、Discord與Dashboard只能是human action interface、intent source或presentation layer，不得成為lifecycle authority。

RCA不是Incident lifecycle authority；RCA failure不得rollback Incident、阻止Assignment或成為Review／Closure的automatic hard gate。`CLOSED`維持terminal；lifecycle-aware RCA refresh boundary只reference PRD-004，不在本PRD重複定義。

---

## 15. Resolution Evidence、Review與Closure

`IN_PROGRESS → AWAITING_REVIEW`需要Resolution Evidence：Actual Action（required）、Resolution Note（required）、SOP Followed（required）、Additional Note（optional）。SOP Followed建議支援`YES`、`NO`、`NOT_APPLICABLE`；偏離SOP／RCA時須structurally identify deviation並保存traceable reason。

提交後進入`AWAITING_REVIEW`並關閉normal correlation eligibility。

`AWAITING_REVIEW → CLOSED`至少需要reviewer approval與recovery verification。RCA absence／failure本身不是額外closure hard gate。Knowledge Improvement Candidate與lifecycle分離；knowledge gap不得阻止合理closure，但須保留future workflow extensibility，且不得自動修改SOP、KB、RAG、Detector或Correlation policy。

---

## 16. Recurrence Policy

`CLOSED`永不reopen。之後相同／相似fingerprint形成新operational episode時建立New Incident，例如`INC-001 CLOSED`後再次出現`oom_crash_detected + payment-api`，建立`INC-002`而非重開`INC-001`。Future analytics／RCA可建立recurrence或similarity relationship，但relationship不等同lifecycle reopen；範例service只用於說明，不是固定identity值。

---

## 17. Persistence Roles

| Logical Role | Authority／Responsibility |
|---|---|
| EventStore | Immutable normalized Event evidence authority。 |
| Correlation State Store | Pending、Processed、Dedup與restart recovery。 |
| Incident Store | Incident current operational state、Event references、lifecycle與audit。 |
| Shadow／Unclassified State | Unknown anomaly learning／review state。 |
| RCA Persistence Authority | 保存Logical RCA Aggregate、immutable Published Versions、Current、attempt／snapshot／freshness／refresh outcome與完整RCA Artifact；詳細語意由PRD-004 v1.0治理。 |

PRD不鎖定physical technology；logical roles可位於同一physical transactional system，但authority boundary不可混淆。

---

## 18. Retention、History與Reset／Cleanup Governance

Lifecycle、Retention、Reset／Cleanup是三個不同概念。PoC default下，Event、Incident、Processed與Shadow不因時間經過或Incident `CLOSED`自動刪除。Closed Incident可留在同一Incident Store；Active／History是lifecycle／query view，不表示必須使用不同physical store。

Production retention／archive／cleanup應可設定並依compliance、storage cost、audit與learning requirements設計。

Reset／Cleanup僅適用major contract／schema change、controlled development／testing或maintenance；不得由normal runtime、validator、test failure處理或AI coding agent自動執行。事前須列出affected stores／data並取得PM／team authorization；scoped cleanup須維護referential integrity。本PRD不鎖定具體CLI／API。

---

## 19. Acceptance Criteria

### AC-A — Known Correlation Contract

- 驗證S1～S6 Strong Anchor fingerprints與Runtime Event evidence-only policy。
- 驗證無scenario／generator／validator answer leakage。
- 驗證S3使用repaired actual OOM-origin `service_name` contract。

### AC-B — Strong／Weak／Pending

- 驗證configurable 120s rolling Correlation Window與30s Pending Grace。
- Candidate lifecycle只允許`OPEN`／`ASSIGNED`／`IN_PROGRESS`。
- 驗證Strong zero／one／multiple candidates、Known Weak Pending、candidate recomputation、timeout與standalone Weak Incident。
- 驗證Late Strong-Anchor Promotion保留Incident identity。
- 明確驗證Correlation Window eligibility change不等同Pending Grace expiration。

### AC-C — Unknown／Shadow Safety

- `general_*`不unsafe auto attach、不進Known Weak Pending、不建立operational Incident。
- 驗證Shadow persistence、`policy_version`與minimum reason／review state。
- 驗證無automatic knowledge、policy、detector或model mutation。

### AC-D — Ownership／Dedup／Recovery

- 一個`event_id`最多一次terminal ownership resolution、一個Event最多一個Incident。
- 驗證replay safety、Processed ledger與restart Pending continuity。
- Candidate在recovery時重新計算，不依賴snapshot。

### AC-E — Incident Mutation／Persistence

- 驗證ordered unique `event_ids`、`anchor_event_id`、`correlation_context`與timestamp semantics。
- Severity monotonic escalation；attach不reset lifecycle／ownership。
- Late promotion保留Incident identity並更新anchor context。
- RCA relationship／reference與完整Artifact分離。
- `rca_ref`只投影latest successfully published Current；已有Current時，refresh pending／generating／failed不清除或降級coarse `COMPLETED` projection。
- Assignment不是initial RCA eligibility gate；RCA failure不成為Incident lifecycle或closure hard gate。

### AC-F — Lifecycle／Human Workflow

- 驗證`OPEN → ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED` strict guards。
- Assignment failure保留`OPEN`；Incident Manager是authority，Jira／Discord／Dashboard只提供interface／intent。
- 驗證Resolution Evidence guard、`AWAITING_REVIEW` correlation-closed、reviewer approval與recovery verification。
- `CLOSED`不reopen，recurrence建立New Incident。

### AC-G — Persistence／History／Reset Governance

- 驗證logical store responsibilities與Logical Store ≠ Physical Database。
- PoC不因`CLOSED`或時間自動刪除；Active／History是query／lifecycle concept。
- 驗證destructive reset／cleanup protection，以及runtime／validator不自動執行destructive operation。

---

## 20. Out of Scope／Future Work

以下不屬本文件implementation scope，但後續設計須遵守本文件authority boundary與`PRD-004 v1.0 Approved`：

- LLM RCA generation details與RAG architecture。
- SOP retrieval與RCA artifact version schema。
- Jira adapter、Discord ChatOps、Dashboard UI與Email fallback實作細節。
- Production retention engine。
- Automatic Shadow clustering／learning或knowledge mutation。
- Topology-aware／ML correlation expansion。
- Physical DB technology choice。

---

## 21. Known Limitations／Future Extensibility

### 21.1 S4 Identity Granularity

目前`external_service`是identity granularity。共用external service的不同endpoint／component未來可能需要更細evidence contract，須另行修訂上游與correlation policy。

### 21.2 S6 Global QPS Ambiguity

目前QPS是單一unlabeled global series；`request_spike_detected`在多個simultaneous target-service Incidents間可能無足夠identity。Ambiguity必須Pending／never guess。未來label-aware metric需另行更新DDS、Metrics Generator、Event Detection SPEC與Event evidence contract。

### 21.3 Multiple OOM Origins

S3正式PoC validation是單一OOM-origin service。同一Window若有多個不同OOM-origin services，上游現行implementation fail closed；PRD-003不自行定義multi-OOM correlation，須獨立contract decision。

---

## 22. Backward Documentation Governance Note（Non-blocking）

DC-2 backward documentation consistency reconciliation已於2026-09-11執行，並re-check README、PRD-001、PRD-002、DDS-001及相關SPEC current-state wording。此為documentation-only reconciliation，不修改本PRD requirement、Event authority或frozen Engineering Contract semantics。

本次reconciliation確認：

1. RCA persistence wording：Incident只保存coarse RCA availability／Current relationship projection；完整Logical RCA Aggregate、versions、attempts、freshness與artifact由PRD-004所治理的RCA persistence authority保存。
2. Jira CLOSED wording：Jira等interface只能送intent；Incident Manager是authoritative lifecycle mutator。
3. Lifecycle／retention／reset wording：三者不同；destructive reset／cleanup不得由normal runtime、validator或AI agent自動執行。
4. 舊correlation rules、Cooldown vs Correlation Window、Event→Incident relationship與legacy lifecycle wording已完成re-check；未改變既有requirement semantics。

2026-09-18 Post-PRD-004 reconciliation完成後，本PRD更新為v1.1 Final；此narrow clarification不改變Correlation、Incident lifecycle、ownership、Shadow或assignment behavior。SPEC-006～011既有scope Implemented不表示RCA additive integration、RCA／RAG、external operational integrations、Knowledge workflow或automatic remediation已完成。
