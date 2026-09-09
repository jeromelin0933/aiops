# SPEC-010 — Shadow / Unclassified Store

## Software Design Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-010 |
| Document Name | Shadow / Unclassified Store |
| Version | 1.0 |
| Status | Implemented |
| Date | 2026-09-09 |
| Requirement Authority | PRD-003 v1.0 Final |
| Upstream Event Contract | PRD-002 v1.5 Approved |
| Upstream Correlation Contract | SPEC-006 v1.0 Implemented |
| Upstream Correlation State Contract | SPEC-007 v1.0 Implemented |
| Related Incident Contract | SPEC-008 v1.1 Implemented |
| Implementation Owner | 夜羽 |

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-09-08 | Initial Draft；定義authoritative Shadow／Unclassified persistence、legal `ROUTE_SHADOW` side effect、Event ownership、operation replay、crash recovery、read／enumeration、integrity、retention及cross-domain ownership工程契約。 |
| 1.0 | 2026-09-08 | Draft Review #1、Revision Round #1 與 PM Review #2 完成；D1～D12、010-R1～R5、ShadowReason semantics、Shadow／Incident ownership boundary、operation replay／crash recovery、closed Shadow error contract、Acceptance Criteria 與 cross-SPEC boundaries 完成 final review。Status 更新為 Approved — Implementation Pending；Engineering Contract frozen for implementation。 |
| 1.0 | 2026-09-09 | Implementation completed and PM Final Review PASS；SPEC-010 Phase 1～6、Shadow persistence、ownership、operation replay／crash recovery、SPEC-008 public ownership integration及AC-010-A～J均已實作並完成驗證。Status更新為 Implemented；Engineering semantics與approved v1.0 contract一致，無implementation deviation。 |

> **Implementation Status Honesty：SPEC-010 v1.0已完成implementation，Status為`Implemented`。** 此狀態只表示Shadow／Unclassified Store及其與SPEC-008 public read-only Event→Incident ownership capability的integration已完成並通過PM Final Review；不代表SPEC-009 lifecycle／human workflow、SPEC-011 Runtime Orchestration、full downstream Docker E2E、RCA／RAG、ChatOps或完整AIOps closed loop已完成，亦不表示整體平台Production Ready。

---

# 0. Authority、Purpose 與 Status

## 0.1 Authority hierarchy

本SPEC依下列優先序制定：

1. `PRD-003 v1.0 Final`是Alert Correlation／Incident Management requirement authority。
2. `PRD-002 v1.5 Approved`是15-field immutable Runtime Event與EventStore authority。
3. `SPEC-006 v1.0 Implemented`是UNKNOWN classification、`ROUTE_SHADOW`、reason、policy identity與`NormalizedFingerprint` authority。
4. `SPEC-007 v1.0 Implemented`是`CorrelationMutationIntent`、Processed／Blocked／Claim與recovery execution state authority。
5. `SPEC-008 v1.0 Approved — Implementation Pending`是related Incident domain與Event→Incident ownership contract；不成為Shadow authority。
6. SPEC-010只在上述authority內定義Shadow domain；SPEC-011 future Runtime負責end-to-end orchestration。

PRD-003 metadata仍引用PRD-002 v1.4，而current Event authority為PRD-002 v1.5。此差異是既知non-blocking metadata drift，不改變Event schema或本SPEC authority，亦不授權修改PRD-003。

若本文件與active upstream authority無法同時滿足，必須停止受影響範圍並回報PM，不得自行patch upstream或讓implementation反向改寫contract。

## 0.2 Purpose

SPEC-010定義一個durable、idempotent、restart-safe的Shadow／Unclassified domain boundary，使SPEC-006已合法決定的`ROUTE_SHADOW`可在SPEC-007 durable Intent保護下建立唯一Shadow ownership及operation result。

核心原則：

> **Shadow保存Unknown；Shadow不決定Unknown。**

> **Shadow ≠ Pending。**

責任口訣：

> **Engine decides. State remembers. Runtime orchestrates. Domain stores own the side effects.**

## 0.3 Pending與Shadow的正式差異

| Concept | Meaning | Authority |
|---|---|---|
| Pending | 已有approved evidence policy，但目前無法安全取得唯一Incident outcome，例如zero或multiple compatible candidates | SPEC-007 Correlation State |
| Shadow／Unclassified | 目前缺乏足夠operational identity或approved safe correlation rule，不能安全建立或加入operational Incident | SPEC-010 Shadow domain |

SPEC-010不得把Pending、validation failure、unregistered Event、contract failure或任意異常輸入轉成Shadow。Blocked／Failure亦不等於Shadow，且不因無法處理就取得learning authority。

---

# 1. Responsibility Boundary

## 1.1 MUST

SPEC-010必須：

- durable保存authoritative `ShadowRecord`；
- 執行legal `ROUTE_SHADOW` downstream side effect；
- 建立並enforce Shadow-local Event ownership；
- durable保存operation result／receipt並支援same-operation replay；
- 支援restart recovery所需的same-result lookup；
- 提供Shadow read、`shadow_exists`及deterministic enumeration capabilities；
- 驗證自身persistence integrity；
- 保持no-TTL及受治理cleanup semantics。

## 1.2 MUST NOT

SPEC-010不得：

- 執行Event detection、UNKNOWN classification、correlation decision、candidate selection或Correlation Window evaluation；
- 建立或修改SPEC-007 Pending、Processed、Blocked、MutationIntent或ProcessingClaim；
- 建立／修改Incident或執行Incident lifecycle、Assignment、RCA或Human Review；
- 執行clustering、aggregation、learning、policy mutation、training dataset generation或model retraining；
- poll EventStore、schedule、sleep、retry或決定工作何時執行；
- 執行Jira、Discord、Email或Dashboard adapter workflow；
- 自動將Shadow reclassify、promote或attach至Incident；
- 使用Scenario ID、Generator state、Validator answer或generator-only metadata；
- 修改Event或以`Event.status`表示Shadow state；
- 提供force shadow、ownership overwrite、destructive delete、reset或automatic repair。

---

# 2. D1～D12 Engineering Decisions

| ID | 正式決策 |
|---|---|
| D1 | SPEC-010擁有ShadowRecord、Shadow-local Event ownership、operation result、reads、integrity及no-TTL retention；不擁有classification、Pending、Incident、RCA、review、learning或orchestration。 |
| D2 | v1 `ShadowRecord`只保存`shadow_id`、`event_id`、`entered_shadow_at`、`reason`、`review_status`、`policy_id`、`policy_version`；`review_status`唯一合法值為`UNREVIEWED`。 |
| D3 | Legal `ROUTE_SHADOW`由real SPEC-007 Intent、authoritative immutable Event及authoritative timezone-aware `now`驅動；SPEC-010生成Shadow ID並回durable result。 |
| D4 | `event_id`是Shadow ownership identity，`operation_id`是replay identity；one Event最多一筆Shadow，equivalent same-operation replay回原result，任何contradiction Fail Closed。 |
| D5 | v1只接受符合SPEC-006 UNKNOWN direct-Shadow semantics的`ROUTE_SHADOW` Intent；unregistered、Pending、Incident Decisions或malformed input不得被當成Shadow。 |
| D6 | 首次successful local mutation將ShadowRecord、Shadow ownership與operation receipt all commit或all rollback；v1不新增business audit。 |
| D7 | 提供authoritative read、existence、operation-result lookup與deterministic enumeration；malformed state不得silent skip或偽裝Not Found。 |
| D8 | ShadowRecord建立後是durable historical state；v1不自動review、reclassify、delete、promote、cluster或learn。 |
| D9 | Local transaction、Event ownership uniqueness與operation receipt uniqueness保護same-event／same-operation concurrency；SPEC-007 Claim不是Shadow persistence lock。 |
| D10 | 使用獨立typed Shadow-domain errors與Fail-Closed integrity；store-wide authority不可可靠判斷時Fail Fast，安全可隔離的per-record contradiction為repair-required。 |
| D11 | Implementation acceptance以contract coverage為準，涵蓋domain、handoff、persistence、ownership、replay、crash、concurrency、integrity、reads及real upstream integration；Docker E2E defer至SPEC-011。 |
| D12 | SPEC-010是獨立logical persistence authority；不鎖physical DB technology，不共享SPEC-007／008 private tables或transaction authority，不要求cross-store ACID／2PC。 |

## 2.1 010-R1～R5 reconciliation

| ID | Accepted reconciliation |
|---|---|
| 010-R1 | ShadowRecord v1 minimum fields與`UNREVIEWED`-only review semantics。 |
| 010-R2 | ShadowReason domain vocabulary與current legal PoC creation path分離；Store不從candidate state自行推導future reasons。 |
| 010-R3 | 建立Shadow ownership前依賴read-only Incident ownership evidence；exact protocol／adapter保持Implementation Choice，integration impact為`REFINE DOWNSTREAM`。 |
| 010-R4 | 首次mutation local atomic commit Shadow＋ownership＋receipt；v1明確不新增Shadow business audit。 |
| 010-R5 | Physical persistence technology不在SPEC-010 v1.0鎖定；future Phase 0可評估stdlib `sqlite3`與independent `shadow_store.db`。 |

---

# 3. Shadow Domain Model

## 3.1 `ShadowRecord`

Minimum logical fields：

```text
shadow_id
event_id
entered_shadow_at
reason
review_status
policy_id
policy_version
```

Invariants：

- `shadow_id`必須unique、durable、non-empty；exact format為Implementation Choice；
- `event_id`只reference PRD-002 authoritative immutable Event；
- `entered_shadow_at`使用Runtime提供的authoritative timezone-aware mutation `now`，並保存absolute、timezone-unambiguous semantics；
- `reason`必須屬第4章ShadowReason vocabulary，且creation path符合current／future approved policy；
- `review_status=UNREVIEWED`是v1唯一合法值；
- `policy_id + policy_version`保存exact SPEC-006 policy trace；
- record不得duplicate full Event、Incident snapshot、candidate snapshot、full Intent、RCA artifact、clustering result、training metadata或LLM analysis。

`ShadowRecord`不是Incident、Pending record、Blocked record、Processed record或learning artifact。

## 3.2 Shadow ID

SPEC-010擁有Shadow ID generation。ID必須unique、durable、non-empty，且same-operation replay保持same `shadow_id`。Exact prefix、UUID／sequence strategy及display format是Implementation Choice；`SHADOW-001`只能作example，不能成為runtime requirement。

## 3.3 Operation result／receipt

Durable receipt/result必須足以：

- 依`operation_id`回原authoritative result；
- 確認resulting `shadow_id`與原`entered_shadow_at`；
- 驗證same-operation semantic equivalence；
- 偵測contradictory replay；
- 供SPEC-007在crash recovery後finalize `ProcessedCorrelationRecord(terminal_outcome=SHADOWED)`。

Exact class、table及serialization不在本SPEC鎖定。

---

# 4. Shadow Reason Semantics

## 4.1 Domain vocabulary

Shadow persistence vocabulary必須能表示：

```text
INSUFFICIENT_OPERATIONAL_IDENTITY
NO_COMPATIBLE_INCIDENT
MULTIPLE_COMPATIBLE_INCIDENTS
```

此closed vocabulary是domain capability，不是SPEC-010自行判斷correlation outcome的權限。

## 4.2 Current legal PoC path

目前SPEC-006 legal `ROUTE_SHADOW`只會產生：

```text
INSUFFICIENT_OPERATIONAL_IDENTITY
```

`NO_COMPATIBLE_INCIDENT`與`MULTIPLE_COMPATIBLE_INCIDENTS`只保留為PRD-level reusable Shadow vocabulary。只有future approved evidence／correlation policy正式定義其operational semantics後，上游才能要求建立該reason的Shadow。

SPEC-010不得因沒有Incident自行推論`NO_COMPATIBLE_INCIDENT`，不得因觀察到多筆Incident自行推論`MULTIPLE_COMPATIBLE_INCIDENTS`，也不得讀candidate set來選reason。

## 4.3 Current UNKNOWN behavior

Current registered UNKNOWN Event Types：

```text
general_log_anomaly
general_metrics_anomaly
```

Current flow：

```text
UNKNOWN
→ ROUTE_SHADOW
→ no Pending
→ no Incident
→ ShadowRecord
```

它不是`UNKNOWN → wait 30s → maybe Incident`，也不是`UNKNOWN → attach nearest Incident`。Unregistered Event Type是SPEC-006 `POLICY_NOT_REGISTERED` failure，不是Shadow。

---

# 5. `ROUTE_SHADOW` Input Contract

## 5.1 Thin request boundary

Implementation可使用薄型request wrapper，但只能組合：

```text
real SPEC-007 CorrelationMutationIntent
authoritative PRD-002 Runtime Event
authoritative timezone-aware mutation now
read-only Incident ownership evidence port
```

Wrapper不得copy Intent fields成第二authority，不得建立010-specific Decision、Reason、Family、Anchor或Fingerprint model。

## 5.2 Legal v1 Intent invariants

SPEC-010只接受semantic-equivalent於下列actual upstream contract的Intent：

```text
decision_type = ROUTE_SHADOW
intended_terminal_outcome = SHADOWED
target_incident_id = null
correlation_family = UNKNOWN
reason_code = INSUFFICIENT_OPERATIONAL_IDENTITY
normalized_fingerprint = null
anchor_strength = null
anchor_transition = NONE
```

另外必須滿足：

- `event.event_id == intent.event_id`；
- Event type為exact `policy_id + policy_version`所註冊的UNKNOWN Event Type；
- policy lookup使用SPEC-006 exact policy semantics，不fallback latest或猜測policy；
- Event只提供receipt與validation所需的`event_id`及`event_type` projection；
- mutation `now`為timezone-aware authoritative time，但不屬replay equivalence identity。

SPEC-010不得重新執行SPEC-006、重新分類Event、建立MutationIntent或由Event內容猜測UNKNOWN。

## 5.3 Wrong-domain與invalid input

Wrong-domain Intent必須依下列closed mapping Fail Closed，不得建立Shadow或轉送其他domain：

```text
CREATE_NEW
→ INVALID_SHADOW_MUTATION
→ NON_RETRYABLE

ATTACH_EXISTING
→ INVALID_SHADOW_MUTATION
→ NON_RETRYABLE
```

Real SPEC-007 `CorrelationMutationIntent`不能合法表示`ENTER_PENDING`。Production不得建立fake Intent model；測試若需驗證malformed boundary，只能使用isolated invalid-object／validation-boundary方式，不得修改SPEC-007或建立平行production contract。

Unregistered Event、Event／Intent ID mismatch、invalid policy identity、錯誤Decision shape或其他malformed input均不得轉成Shadow；若其typed classification為`INVALID_SHADOW_MUTATION`，disposition必須為`NON_RETRYABLE`。

---

# 6. Shadow Creation

## 6.1 Legal flow

```text
SPEC-006 ROUTE_SHADOW
→ SPEC-007 durable CorrelationMutationIntent
→ SPEC-011 supplies Intent + Event + authoritative now
→ SPEC-010 validates legal Shadow mutation and Incident ownership evidence
→ atomic create ShadowRecord + Shadow ownership + operation receipt
→ return authoritative Shadow result
→ SPEC-007 finalizes Processed = SHADOWED
```

## 6.2 First successful mutation

首次legal create必須：

1. 驗證第5章Intent、Event、policy與time contract；
2. 驗證same operation receipt及Shadow-local Event ownership尚無conflict；
3. 透過第8章read-only dependency確認目前無Incident ownership；
4. 生成unique durable `shadow_id`；
5. 建立`ShadowRecord`，其中`entered_shadow_at=authoritative now`、`review_status=UNREVIEWED`；
6. 建立Event→Shadow ownership與matching operation receipt；
7. 在同一local transaction all commit後回authoritative result。

不得建立business audit、Incident、Pending、Processed或learning artifact。SPEC-007 Processed只能在SPEC-010 authoritative success後由其自身authority finalize。

---

# 7. Ownership 與 Idempotency

## 7.1 Dual identity

```text
event_id     = Shadow business ownership identity
operation_id = mutation replay identity
```

SPEC-010必須以durable uniqueness／atomic mechanism enforce：

```text
one event_id → at most one ShadowRecord
one operation_id → one semantically immutable Shadow result
```

SPEC-007 Processed不能取代SPEC-010 local ownership，因為Shadow commit成功後可能在Processed finalize前crash。

## 7.2 Replay／conflict matrix

| Existing state | Request | Required result |
|---|---|---|
| Same operation receipt | Semantically equivalent | 回原`shadow_id`與`entered_shadow_at`；不建立record／ownership／receipt |
| Same operation receipt | Contradictory semantic identity | `MUTATION_RECEIPT_CONFLICT`／Fail Closed |
| Existing Shadow ownership | Different operation | `SHADOW_EVENT_OWNERSHIP_CONFLICT`／Fail Closed，即使仍想route Shadow |
| No local receipt／ownership | Legal first request且無Incident owner | 依第6章atomic create |

Same destination或相同reason不能把different operation靜默當成replay。

---

# 8. Cross-domain Incident Ownership

## 8.1 Global terminal invariant

```text
one event_id must not become both:
Incident-owned
and
Shadow-owned
```

SPEC-010在首次Shadow ownership建立前，必須取得read-only Incident ownership evidence，概念例如：

```text
event_has_incident_owner(event_id) -> bool
```

Exact protocol name、method name、adapter location及implementation technique為Implementation Choice。此capability只提供ownership evidence，不授予SPEC-010 Incident read-all、mutation、repair或lifecycle authority。

禁止direct read SPEC-008 SQLite tables、import其persistence internals、shared physical DB、cross-store foreign key、shared transaction、cross-store ACID或distributed 2PC。

## 8.2 Governance impact

```text
CROSS-SPEC IMPACT:
REFINE DOWNSTREAM

Blocking current SPEC-008 implementation? NO
Blocking SPEC-010 drafting? NO
Blocking SPEC-010 domain implementation? NO
Must be resolved before SPEC-011 full runtime integration? YES
```

SPEC-008目前不得因此被reopen或停止。若future SPEC-008 implementation自然提供等價read-only capability，直接以adapter／public API reuse；若沒有，於SPEC-008 closure或SPEC-011 integration前以minimum-impact governance補足。

在full runtime integration前，SPEC-011必須協調single terminal ownership sequencing及必要read-only adapters。若屆時仍無法可靠排除Incident ownership，該Event的Shadow mutation必須Fail Closed，不得猜測。

---

# 9. Operation Receipt 與 Replay

## 9.1 Receipt semantic identity

Same-operation equivalence minimum包含：

```text
real CorrelationMutationIntent semantic identity
+ Event mutation projection:
  - event_id
  - event_type
```

`authoritative now`不屬replay equivalence。Receipt可以保存canonical encoding、digest或typed columns；exact representation是Implementation Choice，但不得lossy comparison或只檢查`operation_id`存在。

不得duplicate full Event、要求ShadowRecord保存serialized Intent，或把diagnostics升格為mutation identity。

## 9.2 Successful replay

Equivalent same-operation replay必須：

- 完整驗證semantic equivalence；
- 回original durable result；
- 保留original `shadow_id`及`entered_shadow_at`；
- 不使用retry-call `now`覆寫original time；
- 不建立第二record、ownership或receipt；
- 不重新跑SPEC-006或重新判斷Shadow reason。

Contradictory request必須回`MUTATION_RECEIPT_CONFLICT`／Fail Closed。

---

# 10. Restart 與 Crash Recovery

SPEC-010不管理SPEC-007 recovery loop。Unresolved Intent recovery只能lookup／replay同一operation：

```text
SPEC-007 Intent durable
→ SPEC-010 Shadow + ownership + receipt commit
→ crash before SPEC-007 Processed
→ restart
→ SPEC-011 requests same operation result/replay
→ SPEC-010 returns same shadow_id and entered_shadow_at
→ SPEC-007 finalizes SHADOWED
```

| Crash point | Local durable state | Allowed recovery |
|---|---|---|
| Before local transaction | 無新Shadow effect | Same Intent可首次執行並重新做legal validation |
| During local transaction | All rollback | Same operation安全重試，不得觀察partial record／ownership／receipt |
| After commit before response | Shadow＋ownership＋receipt均存在 | Lookup／replay same operation並回原result |
| After response before Processed | SPEC-010 result durable；SPEC-007 Intent unresolved | Same-operation reconciliation；不重新correlate |

Repeated crash／restart不得改變Shadow ID、entered time、reason、policy trace、review status或receipt count。

---

# 11. Read 與 Enumeration

## 11.1 Semantic capabilities

至少支援：

```text
get_shadow(shadow_id)
get_shadow_by_event_id(event_id)
shadow_exists(shadow_id)
get_operation_result(operation_id)
enumerate_shadows(...)
```

Exact Python names與return container是Implementation Choice。

## 11.2 Enumeration

v1 enumeration最低可依下列欄位filter：

```text
reason
review_status
```

對相同authoritative snapshot及相同parameters，結果順序必須deterministic。Exact sort key／pagination strategy是Implementation Choice且不得被消費者當成business ranking或ownership authority。

## 11.3 Read integrity

Reads必須回authoritative durable state。Malformed matching record不得silent disappear、轉成Not Found或被enumeration略過。Read path不得要求EventStore或Incident Store full scan，也不得執行clustering、analytics ranking或candidate matching。

---

# 12. Concurrency

## 12.1 Same Event race

Different operations競爭同一Event時，只能一個建立Shadow ownership；loser必須Fail Closed。不得以last-write-wins、same destination或相同reason接受loser。

## 12.2 Same operation race

同一operation最多一個durable result。Equivalent caller取得same result；contradictory caller回receipt conflict。不得產生duplicate Shadow或receipt。

## 12.3 Local authority

SPEC-007 ProcessingClaim不成為SPEC-010 persistence lock authority。SPEC-010 local correctness必須由local transaction、ownership uniqueness、operation uniqueness及persistence constraints保障。

v1 ShadowRecord建立後沒有mutable business workflow，因此不需要SPEC-008式different Events共同更新同一Incident的per-aggregate concurrency model。Exact lock、transaction或CAS technique為Implementation Choice。

---

# 13. Integrity 與 Error Contract

## 13.1 Typed error taxonomy

SPEC-010使用獨立Shadow-domain errors，不修改SPEC-006、007或008 enums：

| Error code | Meaning |
|---|---|
| `INVALID_SHADOW_MUTATION` | Wrong-domain Decision、invalid Event／Intent／policy／time shape或其他invalid input。 |
| `SHADOW_NOT_FOUND` | 指定Shadow reference不存在；corrupt matching record不得使用此code偽裝Not Found。 |
| `SHADOW_EVENT_OWNERSHIP_CONFLICT` | Event已由不同Shadow operation取得Shadow ownership。 |
| `INCIDENT_EVENT_OWNERSHIP_CONFLICT` | Read-only Incident evidence確認Event已有Incident owner。 |
| `MUTATION_RECEIPT_CONFLICT` | Same operation request與既有immutable receipt identity矛盾。 |
| `MALFORMED_SHADOW_RECORD` | Persisted Shadow／ownership／receipt structure或cross-field invariant無效。 |
| `UNSUPPORTED_SHADOW_STATE_VERSION` | Stored state使用不支援的schema／semantic version。 |
| `SHADOW_STORE_INTEGRITY_FAILURE` | Store無法安全initialize、enumerate、read或判斷authority。 |
| `TRANSIENT_SHADOW_STORE_FAILURE` | 已明確分類且safe-to-retry的temporary persistence failure。 |

Failure disposition vocabulary：

```text
RETRYABLE | REPAIR_REQUIRED | NON_RETRYABLE
```

## 13.2 DD-010-1 — PM-Adjudicated Closed Disposition Mapping

下列exact mapping已由PM adjudicate，為SPEC-010 v1 Shadow-domain closed error contract。不得以catch-all、all others、default或unspecified mapping取代。

| Error | Disposition |
|---|---|
| `INVALID_SHADOW_MUTATION` | `NON_RETRYABLE` |
| `SHADOW_NOT_FOUND` | `NON_RETRYABLE` |
| `SHADOW_EVENT_OWNERSHIP_CONFLICT` | `REPAIR_REQUIRED` |
| `INCIDENT_EVENT_OWNERSHIP_CONFLICT` | `REPAIR_REQUIRED` |
| `MUTATION_RECEIPT_CONFLICT` | `REPAIR_REQUIRED` |
| `MALFORMED_SHADOW_RECORD` | `REPAIR_REQUIRED` |
| `UNSUPPORTED_SHADOW_STATE_VERSION` | `REPAIR_REQUIRED` |
| `SHADOW_STORE_INTEGRITY_FAILURE` | `REPAIR_REQUIRED` |
| `TRANSIENT_SHADOW_STORE_FAILURE` | `RETRYABLE` |

`SHADOW_NOT_FOUND`只表示ordinary public read（例如`get_shadow(nonexistent_shadow_id)`）確認authoritative Shadow Store本來就沒有該record；其disposition為`NON_RETRYABLE`，本身不是store corruption。

若SPEC-007 Processed／recovery state已authoritative reference某`shadow_id`，但SPEC-010無法確認該Shadow存在，這是cross-store authoritative contradiction，不得降級為ordinary `SHADOW_NOT_FOUND`。Recovery／integrity authority必須依SPEC-007既有`DANGLING_SHADOW_REFERENCE` semantics升級處理；SPEC-010不得建立competing dangling-reference taxonomy、replacement Shadow或cross-store mutation。

SPEC-010只回domain failure；SPEC-011 future Runtime負責retry trigger、operator visibility及與SPEC-007 Block的正式handoff。SPEC-010不得直接寫Blocked state或busy-loop。

## 13.3 Persistence integrity

Implementation必須能可靠辨識stored state採用supported或unsupported schema semantics；version metadata placement可在record、table、envelope或store metadata，屬Implementation Choice。

至少驗證：

- malformed Shadow、ownership或receipt；
- unsupported state version；
- duplicate／contradictory Event ownership；
- duplicate／contradictory operation receipt；
- missing referenced Shadow；
- invalid reason／review status／policy trace；
- store能否安全initialize、enumerate、read及判斷authority。

禁止silent skip、last-write-wins、guess-and-rewrite、automatic replacement Shadow或destructive recovery。Per-record問題若能安全isolate，可回repair-required並阻止該record／Event mutation；若store-wide authority無法可靠載入、enumerate或分類，startup／readiness必須Fail Fast。

---

# 14. Retention 與 Cleanup Governance

- ShadowRecord不因age、restart、`review_status`、policy update或Incident lifecycle自動刪除；
- v1沒有TTL semantics；
- inactive、unreviewed或historical不等於可erase；
- normal Runtime不得提供public destructive delete、reset、force repair或ownership reassignment；
- reset／cleanup只限controlled dev／test／migration，且必須PM explicit authorization與strong guard；
- validator與AI coding agent不得清除Shadow state以讓tests通過；
- physical compaction只有在logically lossless且不破壞ownership／receipt／referential integrity時才可作Implementation Choice。

Future retention／archive或Admin Repair Tool必須另立governed contract。

---

# 15. Cross-SPEC Boundary

| Contract | Owns | SPEC-010 boundary |
|---|---|---|
| PRD-002／EventStore | Immutable Event evidence | 只消費`event_id`／`event_type` projection並保存reference；不mutate Event。 |
| SPEC-006 | UNKNOWN classification、`ROUTE_SHADOW`、reason、policy identity | 不重新分類、不新增reason semantics、不建立Decision。 |
| SPEC-007 | MutationIntent、Processed、Blocked、Claim與recovery execution state | 接收real Intent、回domain result；不存取其private persistence。 |
| SPEC-008 | Incident、Event→Incident ownership與operational Incident context | 只透過read-only ownership evidence port確認conflict；不修改Incident。 |
| SPEC-010 | ShadowRecord、Event→Shadow ownership、receipt與reads | 本文件authority。 |
| SPEC-011 | Orchestration、Event acquisition、authoritative now、cross-store sequencing與recovery coordination | 呼叫semantic APIs；不得繞過SPEC-010 validation。 |

任何domain不得direct read／write另一domain private tables、假設shared transaction或將reference lookup轉成mutation authority。

---

# 16. Public Semantic APIs

Formal contract鎖semantic capability，不鎖exact Python signatures：

| Capability | Preconditions | Success | Failure semantics |
|---|---|---|---|
| Create from Shadow Intent | Real legal Intent、matching Event、valid now、無ownership／receipt conflict | Atomic Shadow＋ownership＋receipt，回authoritative result | Invalid input non-retryable；contradiction／integrity Fail Closed |
| Get operation result | Valid operation reference | 回durable result或明確absence | Malformed receipt不得當absence |
| Get Shadow by ID／Event | Valid reference | 回authoritative record或明確Not Found | Corrupt matching record回typed integrity failure |
| Shadow exists | Valid Shadow reference | Reliable boolean | Corrupt／unreadable matching state不得回false |
| Enumerate Shadows | Valid reason／review filters | Deterministic authoritative projection | Malformed state不得silent skip |
| Readiness／integrity | Store可被可靠載入／分類 | Ready | Store-wide authority failure Fail Fast |
| Incident ownership lookup port | Valid Event reference | Read-only ownership evidence | Unavailable／contradictory evidence時禁止建立Shadow |

Public API不得提供force shadow、Intent validation bypass、ownership overwrite、arbitrary review-status update、reclassification、delete、reset、repair或direct Incident mutation。

---

# 17. Acceptance Criteria

## AC-010-A — ShadowRecord

- Required seven fields完整，`shadow_id`／`event_id`／exact policy trace有效。
- `review_status`在v1只接受`UNREVIEWED`。
- Reason vocabulary可表示三種正式值，但current creation gate另依AC-B限制。
- 不保存full Event、Incident／candidate snapshot、full Intent、RCA、clustering、training或LLM artifact。

## AC-010-B — Legal `ROUTE_SHADOW`

- Real Intent符合`ROUTE_SHADOW`／`SHADOWED`／`UNKNOWN`／`INSUFFICIENT_OPERATIONAL_IDENTITY`及null target／fingerprint／anchor、`NONE` transition。
- Current registered `general_log_anomaly`與`general_metrics_anomaly`可依exact policy建立exactly one ShadowRecord、ownership及receipt。
- Record為`UNREVIEWED`且`entered_shadow_at`來自authoritative now。
- Legal flow不建立Pending或Incident。

## AC-010-C — Wrong-domain／Invalid Input

- `CREATE_NEW`傳入SPEC-010時回`INVALID_SHADOW_MUTATION`／`NON_RETRYABLE`。
- `ATTACH_EXISTING`傳入SPEC-010時回`INVALID_SHADOW_MUTATION`／`NON_RETRYABLE`。
- Unregistered Event不轉Shadow；Event ID mismatch、invalid policy identity及malformed input Fail Closed。
- 不建立fake production `ENTER_PENDING` Intent model；isolated malformed-boundary test不得改SPEC-007。
- Failure不留下Shadow、ownership或success receipt。

## AC-010-D — Event Ownership

- Same Event最多一個Shadow owner。
- Same Event different operation被拒絕，即使destination／reason相同。
- Incident ownership evidence為true時回`INCIDENT_EVENT_OWNERSHIP_CONFLICT`，不建立Shadow。
- 不direct read／writeSPEC-008 tables或silent overwriteownership。

## AC-010-E — Idempotency／Replay

- Same operation equivalent replay回same `shadow_id`及`entered_shadow_at`。
- Replay不建立duplicate record、ownership或receipt。
- Receipt驗證real Intent identity及Event `event_id + event_type` projection。
- Mutation `now`排除於equivalence；contradictory projection仍Fail Closed。

## AC-010-F — Crash／Restart

- Local transaction failureall rollback，無partial Shadow／ownership／receipt。
- Commit後crash可於reopen後lookup／replaysame operation並回原result。
- Recovery不重新執行SPEC-006，結果可供SPEC-007 finalize `SHADOWED`。
- Repeated recovery不改ID、entered time或receipt count。

## AC-010-G — Read／Enumeration

- Get by Shadow ID、Event ID、existence及operation lookup符合第11章。
- Reason／review filters有效，same snapshot／parameters重複enumeration結果順序一致。
- Malformed matching record不能Not Found或silent skip。
- Reads不觸發EventStore／IncidentStore full scan、candidate matching、clustering或ranking。

## AC-010-H — Integrity／Retention

- Unsupported version、malformed record、ownership／receipt contradiction及missing reference均Fail Closed。
- 第13.2節九項Shadow-domain Error Code均依closed disposition mapping驗證，不以catch-all、default或unspecified mapping取代。
- Ordinary nonexistent Shadow read回`SHADOW_NOT_FOUND`／`NON_RETRYABLE`；authoritative Processed／recovery reference所指Shadow不存在時，不得當普通Not Found，必須交由recovery／integrity authority依SPEC-007 `DANGLING_SHADOW_REFERENCE` semantics升級。
- Store-wide unreadable／unclassifiable authority使startup／readiness Fail Fast；可安全isolate的per-record failure不阻塞無關records。
- Shadow不因age、restart、review、policy或Incident lifecycle自動刪除。
- 無normal-runtime destructive cleanup、repair或TTL。

## AC-010-I — Concurrency

- Same Event race只有一個authoritative Shadow ownership。
- Same operation race最多一個durable result，equivalent callers取得same result。
- Contradictory race Fail Closed且無duplicate／partial state。
- Correctness不依賴SPEC-007 ProcessingClaim作persistence lock。

## AC-010-J — Cross-SPEC Boundary

- 使用real SPEC-006 Decision／policy semantics及real SPEC-007 `CorrelationMutationIntent`／`TerminalOutcome.SHADOWED`。
- 無duplicate correlation enums、fake Intent或full Event model authority。
- 無direct SPEC-007／008 table access、shared DB／transaction或2PC assumption。
- SPEC-008不被mutate；read-only Incident ownership dependency依010-R3處理。
- 未實作SPEC-011 orchestration、Human Review、RCA、learning或full downstream E2E。
- Full repository regression通過；完整Docker downstream E2E defer至SPEC-011。

---

# 18. Required Test Layers

Implementation acceptance至少包含：

1. Shadow domain contract tests：record、reason、review、policy及invariants。
2. Input validation tests：legal／wrong-domain／malformed handoff。
3. Ownership tests：Shadow-local與Incident conflict evidence。
4. Persistence contract tests：durability、atomicity、schema compatibility與readiness。
5. Replay／receipt tests：equivalent、contradictory及mutation-now exclusion。
6. Restart tests：reopen、same-result lookup與SPEC-007 finalization handoff。
7. Same-event／same-operation concurrency tests。
8. Integrity／version tests：malformed、unsupported、contradiction及store-wide failure。
9. Read／enumeration tests：ID／Event lookup、filters、determinism及no silent skip。
10. Incident ownership dependency tests：true／false／unavailable／contradictory evidence。
11. Real SPEC-006／007 integration-lite tests：actual enums、policy、Intent及`SHADOWED` outcome。
12. Full repository regression：Repository正式equivalent command。

不得以固定test count取代contract coverage。Full Docker downstream Correlation E2E defer至SPEC-011 integration。

---

# 19. Future Work／Out of Scope

本SPEC v1不實作：

- Incident Manager、Incident mutation或Incident lifecycle；
- Pending、Processed、Blocked、Intent或Claim implementation；
- Runtime orchestration、polling、scheduling或retry loop；
- RCA generation或artifact persistence；
- Human Review及`REVIEWED`／`ACCEPTED`／`REJECTED` review states；
- Shadow→Incident promotion、Event reclassification或automatic recorrelation；
- clustering、aggregation、frequency analysis、pattern discovery或analytics ranking；
- policy learning、training dataset generation、model retraining或SOP／RAG mutation；
- Dashboard、Jira、Discord或Email workflow；
- automatic retention／archive engine；
- full Admin Repair Tool、automatic repair或destructive cleanup；
- production distributed DB、Redis、Kafka、shared DB、cross-store ACID或2PC；
- full Docker downstream Correlation E2E。

Future work需另立authority與acceptance contract，不得把刪除Shadow／Processed或改寫ownership當成reclassification workflow。

---

# 20. Implementation Closure

Implementation Owner為 **夜羽**。SPEC-010 v1.0 Phase 1～6 implementation及PM Final Review已完成，Status已更新為`Implemented`。

Approved implementation phases已完成：

```text
Phase 0 — Read-only Implementation Plan
Phase 1 — Domain Contracts
Phase 2 — Durable Shadow Store
Phase 3 — ROUTE_SHADOW Mutation / Ownership
Phase 4 — Replay / Crash / Recovery
Phase 5 — Integrity / Concurrency / Read APIs
Phase 6 — Cross-SPEC Integration / Regression
```

Phases及implementation choices不改變本SPEC對physical DB technology保持open的normative contract。後續maintenance未經PM授權不得執行Git mutation、改寫upstream contracts、做destructive cleanup、實作SPEC-011 scope或把current physical adapter升格為永久requirement。

## 20.1 Implementation Closure Evidence（Non-normative）

下列test counts屬Engineering Confidence Evidence，不代表system accuracy，亦不承諾future competition final repository的固定test count。

| Evidence | Result |
|---|---|
| D1～D12 | PASS |
| 010-R1～R5 | PASS |
| AC-010-A～J | PASS |
| SPEC-010 targeted tests | 55 passed |
| Relevant SPEC-006 tests | 172 passed |
| Relevant SPEC-007 tests | 78 passed |
| Relevant SPEC-008 tests | 131 passed |
| Full repository regression | 865 passed／0 failed |
| PM Final Review | PASS |
| Implementation deviations | NONE |

Warnings evidence：PM validation觀察到24,022筆known joblib／NumPy `DeprecationWarning`及1筆existing environment `PytestCacheWarning`；未發現new functional warning pattern。

## 20.2 SPEC-008 Public Ownership Integration（Non-normative）

Current implementation透過public read-only semantic capability：

```text
SqliteIncidentStore.event_has_incident_owner(event_id: str) -> bool
```

SPEC-010只消費Event→Incident ownership evidence：coherent Incident owner存在時拒絕Shadow creation；clean absence時legal Shadow mutation可繼續；ownership corruption或contradiction必須Fail Closed。SPEC-010不存取private Incident persistence、raw Incident SQLite／tables，亦不建立duplicate Incident ownership authority。

Phase 6 evidence已涵蓋Incident-owned拒絕Shadow、clean absence允許Shadow、corruption Fail Closed，以及replay、restart與concurrency。Global invariant仍為同一Event不得同時具有Incident與Shadow authoritative ownership。

SPEC-010不提供distributed cross-store atomicity。Concurrency guarantee限於Shadow local-store atomicity加上protocol-level Incident ownership semantic check；unsupported cross-store race與Runtime sequencing／reconciliation仍由SPEC-011承接。本implementation未使用或宣稱2PC、distributed ACID、shared database locking或private cross-store access。

## 20.3 Competition Evaluation Boundary（Non-normative）

```text
EVAL-09 evidence improved: YES
Production competition instrumentation added: NO
```

未新增`scenario_id`、`evaluation_run_id`、ground truth、evaluation-only timestamps或KPI-specific production fields。Final measurement與reporting仍由external Evaluation Harness／SPEC-011處理。

---

# 21. Documentation Governance

SPEC-010 v1.0已完成PM Review並frozen for implementation。Implementation不得靜默反向改寫frozen contract。

Implementation若發現文件問題，先分類：

```text
MUST PATCH
NOTE ONLY
DEFER
```

Cross-SPEC impact分類：

```text
NO IMPACT
REFINE DOWNSTREAM
BLOCKING CONFLICT
```

不得先改implementation semantics再回頭使SPEC配合code。Wording、metadata或non-normative implementation note可最小patch／defer；Engineering Contract change先更新受影響SPEC，只有requirement／product-level behavior改變才考慮PRD revision。

Architecture change時必須評估PRD、SPEC、DDS、architecture diagram及README consistency。任何authority contradiction或destructive repair需求都必須停止受影響範圍、保存evidence並回報PM。

後續implementation若發現semantic requirement或Engineering Contract無法安全成立，必須停止受影響範圍、保存implementation evidence，依上述文件與Cross-SPEC impact分類，由PM審核後再決定是否修改本SPEC或upstream authority。AI coding agent不得自行執行destructive reset或cleanup。

---

# 22. PM Review／Implementation Closure Checklist

- [x] Metadata為SPEC-010 v1.0 `Implemented`／2026-09-09，Owner為夜羽。
- [x] Authority正確引用PRD-003、PRD-002 v1.5、SPEC-006／007 Implemented及SPEC-008 v1.1 Implemented。
- [x] D1～D12與010-R1～R5完整且無active conflict。
- [x] `Shadow ≠ Pending`及Blocked／invalid input不route Shadow。
- [x] Event保持15-field immutable authority且未duplicate full Event。
- [x] Reuse real SPEC-007 `CorrelationMutationIntent`，無duplicate／fake production Intent。
- [x] Current legal UNKNOWN／`ROUTE_SHADOW` path與SPEC-006一致。
- [x] ShadowReason domain vocabulary與current creation path明確分離。
- [x] v1 `review_status`只允許`UNREVIEWED`且無review transition。
- [x] Shadow-local Event ownership與operation replay semantics完整。
- [x] Incident ownership conflict由read-only evidence port處理，不取得Incident mutation authority。
- [x] Local Shadow＋ownership＋receipt all-or-nothing且沒有business audit。
- [x] Crash／restart recovery只lookup／replaysame operation。
- [x] Read、existence、operation lookup及deterministic enumeration完整。
- [x] DD-010-1 closed error mapping已正確反映PM adjudication，且無catch-all／default disposition。
- [x] `SHADOW_NOT_FOUND`與SPEC-007 dangling-reference authority distinction已驗證。
- [x] No TTL、auto delete、auto repair或destructive normal-runtime cleanup。
- [x] 無automatic reclassification、clustering、learning或retraining。
- [x] Physical DB technology未frozen；`sqlite3`／`shadow_store.db`僅為Phase 0評估選項。
- [x] Cross-SPEC impact維持`REFINE DOWNSTREAM`；SPEC-008 public ownership capability已完成，unsupported cross-store race及Runtime sequencing仍由SPEC-011承接。
- [x] SPEC-011 orchestration及full downstream Docker E2E未提前實作。
- [x] AC-010-A～J均可轉為targeted tests且未以固定test count取代coverage。
- [x] Required test layers含real SPEC-006／007 integration與full regression。
- [x] SPEC-010 v1.0 implementation及PM Final Review已完成；未宣稱SPEC-009、SPEC-011、完整Runtime或full Docker E2E完成。
