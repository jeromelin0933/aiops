# SPEC-011 — Runtime Orchestration / E2E

## Software Design Specification v1.1

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-011 |
| Document Name | Runtime Orchestration / E2E |
| Version | 1.1 |
| Status | Implemented Existing Scope — RCA Runtime Boundary Approved / Implementation Pending |
| Approval Date | 2026-09-18 |
| Requirement Authority | PRD-003 v1.1 Final；PRD-004 v1.0 Approved（RCA Runtime boundary） |
| Upstream Event Contract | PRD-002 v1.5 + SPEC-001 v2.4 EventStore authoritative enumeration |
| Upstream Correlation Contract | SPEC-006 v1.0 Implemented |
| Upstream Correlation State Contract | SPEC-007 v1.0 Implemented |
| Incident Contract | SPEC-008 v1.2（v1.1 existing scope Implemented；RCA integration pending） |
| Lifecycle / Human Workflow Contract | SPEC-009 v1.0 Implemented |
| Shadow Contract | SPEC-010 v1.0 Implemented |
| Implementation Owner | 富裕 |

### Change History

| Version | Date | Status | Change |
|---|---|---|---|
| 0.1 | 2026-09-13 | Draft | 建立SPEC-011 Engineering Contract及D1～D8 normative contract；納入PM semantic review corrections，包括AUTO_ASSIGN zero-record recovery、Runtime Work integrity、Runtime Clock、crash/restart matrix及cross-section wording reconciliation。 |
| 1.0 | 2026-09-13 | Approved — Implementation Pending | PM完成final semantic review並核准；D1～D8凍結為implementation baseline；不需upstream semantic revision，implementation尚未開始。 |
| 1.0 | 2026-09-16 | Implemented | SPEC-011 Phase 1～7與PM-authorized corrective fixes完成；Final Full Contract Audit及PM Final Review均PASS。Implementation commit：`cd481e8a1ed6b390d51bd81a519da43914b0b786`。D1～D8與architecture semantics未變更。 |
| 1.1 | 2026-09-18 | Approved Additive Boundary — Implementation Pending | Post-PRD-004 reconciliation：要求既有singular Runtime framework future-compatible with initial RCA、Material Evidence refresh、post-context、Attempt retry、STALE refresh及Publication Reconciliation obligations。D1～D8與v1.0 implemented behavior不變；未凍結exact RCA work enum／record／worker／metric，亦未實作RCA integration。 |

### Implementation Status Honesty

> **Draft ≠ Approved；Approved ≠ Implemented。** SPEC-011現已另以implementation及verification evidence滿足Implemented gate。

**SPEC-011 v1.0既有Runtime Orchestration / E2E scope已Implemented；v1.1 RCA additive Runtime boundary為Approved／Implementation Pending。** D1～D8仍是FROZEN IMPLEMENTATION BASELINE。

**Existing Scope Implementation Status: IMPLEMENTED。** Independent Runtime Worker、D2 SQLite Runtime Work Store、startup recovery barrier、Pending與AUTO_ASSIGN recovery、bounded retry、Runtime clock、structured telemetry、host CLI及Docker Runtime service已實作並驗證。RCA work categories、RCA startup recovery、post-context wake-up及Publication Reconciliation尚未實作；本文件亦不表示RCA／RAG、external adapters、完整AIOps closed loop或production readiness已完成。

Implementation closure evidence：

- Implementation commit：`cd481e8a1ed6b390d51bd81a519da43914b0b786`
- Final Full Contract Audit：PASS
- PM Final Review：PASS
- Runtime Phase 1～7 + cross-SPEC：123 passed
- Real SPEC-006／007／008／009／010 integrations：309 passed
- Full regression：1053 passed, 1 skipped
- Opt-in Docker E2E（獨立執行）：1 passed
- `git diff --check`：PASS

---

# 0. Authority、Purpose與核准決策

## 0.1 核心責任句

> **Engine decides. State remembers. Runtime orchestrates. Domain stores own the side effects.**

SPEC-011是WHEN、ORDER、RETRY、RECOVERY與CROSS-STORE COORDINATION authority。它只能讀取authoritative evidence、呼叫public semantic APIs並協調執行；不得成為第二份Event、Policy、State、Incident、Lifecycle或Shadow authority。

## 0.2 Authority Hierarchy

| 領域 | Authority | SPEC-011必須遵守的邊界 |
|---|---|---|
| Product correlation／Incident requirements | PRD-003 v1.1 Final | 不降低ownership、recovery、lifecycle或fail-closed要求 |
| RCA product requirements | PRD-004 v1.0 Approved | Runtime只協調RCA obligations；不擁有Material Evidence、artifact、version、freshness、grounding或diagnostic truth |
| Runtime Event | PRD-002 v1.5 | 15-field immutable Event；不得改寫`Event.status`表示correlation state |
| Event persistence／enumeration | SPEC-001 v2.4、SPEC-004 v1.1 | Detector寫入；Runtime只從authoritative public read surface取得durable Event |
| Correlation Policy | SPEC-006 v1.0 | Decision、fingerprint、candidate、window、phase、policy identity與evaluation failure authority |
| Correlation State | SPEC-007 v1.0 | Claim、Pending、Intent、Processed、Blocked與recovery precedence authority |
| Incident | SPEC-008 v1.2 | Event→Incident ownership、create／attach及Incident-owned RCA relationship mutation／read authority；Runtime只呼叫public semantics |
| Lifecycle | SPEC-009 v1.0 | Assignment、人工作業、workflow receipt與lifecycle authority |
| Shadow | SPEC-010 v1.0 | Event→Shadow ownership、Shadow mutation／receipt／reason authority |
| Runtime | SPEC-011 | intake ordering、scheduling、cross-store protocol、retry timing、startup與telemetry |

PRD-001、DDS-001、README與SPEC-005提供supporting context，不得覆蓋上述domain authority。

## 0.3 Purpose

本SPEC定義從durable Runtime Event到Incident或Shadow terminal ownership的獨立Runtime Worker contract，並涵蓋Pending reevaluation、CREATE_NEW後AUTO_ASSIGN、bounded retry、restart recovery及最小E2E evidence。依PRD-004 v1.0，本SPEC亦定義既有singular Runtime framework未來承接RCA orchestration obligations的capability boundary，但不設計RCA domain或worker implementation。

本SPEC不重新定義任何detector或domain business semantics。

## 0.4 D1～D8 Engineering Decisions

| Decision | Normative contract |
|---|---|
| D1 | Durable Event-first；使用`EventStore.read_all_authoritative()`全量enumeration與SPEC-007 per-event resolution，不依賴durable cursor |
| D2 | 建立Minimal Durable Runtime Execution Record，只保存無法由domain truth完整表達的orchestration continuity |
| D3 | CREATE_NEW correlation Processed-first，之後以可重建durable work及stable workflow operation ID完成AUTO_ASSIGN |
| D4 | Recovery-first Startup Readiness Barrier；recovery state已可靠分類才可進入READY |
| D5 | configurable periodic global Active Pending sweep；PoC default為1 second，phase仍由SPEC-007決定 |
| D6 | protocol-level global ownership guard、symmetric read-before-mutate與post-mutation reconciliation；無2PC |
| D7 | independent Runtime Worker、first-class CLI、continuous與controlled drain共用同一core semantics |
| D8 | deterministic bounded retry、injectable timezone-aware absolute Runtime Clock、canonical UTC durable Runtime timestamps與minimal structured telemetry |

## 0.5 PRD-004 Additive RCA Runtime Boundary

本節是v1.1 approved additive amendment。D1～D8、既有correlation／Pending／AUTO_ASSIGN behavior及v1.0 implementation evidence全部維持不變；不得另建第二套RCA scheduler、retry database、recovery framework或clock authority。

既有Runtime framework未來至少必須能承接下列orchestration obligations，但exact enum、record、queue、worker、operation identity encoding與physical store均留Candidate E：

- initial RCA generation obligation；
- Material Evidence refresh obligation；
- post-context follow-up obligation；
- Generation Attempt retry continuation；
- STALE Current refresh obligation；
- Publication Reconciliation obligation。

Boundary allocation：

1. **D2 extensibility**：durable Runtime Work framework保存跨restart orchestration continuity，但不保存RCA Artifact或成為RCA truth。RCA work／publication需要stable logical work與operation identity概念；exact representation未凍結。
2. **Startup recovery**：outstanding RCA obligations未來必須可被authoritative discovery、classification、reconciliation與resume。RCA Domain提供capability readiness；Knowledge Index unavailable本身不重新定義whole-platform READY gate。無法可靠讀取RCA authoritative state或分類必要obligation時仍須Fail Closed。
3. **Retry authority**：RCA Domain決定retry safety並提供typed `RETRYABLE`、`NON_RETRYABLE`或`REPAIR_REQUIRED` disposition；Runtime只依D8執行durable budget、authoritative clock與bounded scheduling。PRD-004 PoC的initial try加最多4次、`1s／2s／4s／8s`是RCA domain policy reference，不改成所有Runtime work的global policy。
4. **Runtime Clock／post-context**：Runtime負責absolute wake time、eligibility與restart continuity；RCA Domain負責`episode_end`、post-context boundary、material difference與obligation是否仍成立。Restart不得reset absolute wake-up。
5. **Publication Reconciliation**：Runtime可協調RCA persistence authority與SPEC-008 Incident Manager authority，只能使用雙方public semantic reads／mutations；Runtime不擁有RCA truth、不direct-write任何domain store且不建立2PC。
6. **Telemetry／restart**：既有telemetry與controlled drain／startup recovery principles可擴充generation attempt、retry、refresh、provider invocation、failure disposition及publication reconciliation signals。Exact metric／event name未凍結；restart不重設attempt、budget、wake time或logical operation identity。

SPEC-011不得定義Material Evidence rules、Evidence Snapshot、Knowledge applicability、RCA Artifact／Published Version、Fresh／Stale、Diagnostic Conclusion、grounding、prompt或Chroma index；上述語意屬PRD-004及Candidate A～D／E。

---

# 1. Responsibility / Boundary

## 1.1 Runtime Owns

- 從authoritative EventStore發現durable Events。
- 依SPEC-007 resolved state選擇fresh execution、Pending、recovery或no-op路徑。
- 取得與尊重Processing Claim／fencing authority。
- 取得最新`IncidentCorrelationView`並呼叫SPEC-006。
- 排序Intent、domain side effect、cross-store reconciliation與Processed finalization。
- 排程Active Pending sweep與expired Pending reevaluation。
- 建立及恢復必要的Runtime execution work，包括AUTO_ASSIGN與bounded retry continuity。
- future建立及恢復第0.5節RCA orchestration continuity，但不擁有RCA business truth。
- 建立Startup Recovery Barrier。
- 提供authoritative timezone-aware absolute Runtime now及process-local monotonic scheduling。
- 產生不取代domain audit的structured telemetry。

## 1.2 Runtime Does Not Own

- Event內容、Event schema或Event persistence writer。
- Correlation decision、fingerprint、candidate ranking或correlation window。
- Pending／Blocked／Intent／Processed truth或state precedence。
- Incident／Shadow terminal ownership record。
- Incident create／attach或workflow transaction。
- assignee、reviewer、Resolution Evidence或human lifecycle truth。
- business audit、domain receipt或error retry disposition。
- Material Evidence、Evidence／Knowledge snapshot、RCA Artifact／version／freshness／conclusion或publication truth。

## 1.3 Public Semantic API Rule

Runtime只能透過各domain公開的semantic capabilities讀寫。禁止直接查詢或修改SPEC-007、008、009、010 private tables，禁止直接parse EventStore JSONL，禁止以telemetry或Runtime execution record覆寫domain truth。

---

# 2. Runtime Architecture

## 2.1 Logical Topology

```text
Event Detection Runner
  → Detector-owned EventStore.write()
  → durable immutable Event
  → independent SPEC-011 Runtime Worker
      → SPEC-007 State
      → SPEC-006 Policy Engine
      → SPEC-008 Incident authority
      → SPEC-010 Shadow authority
      → SPEC-009 AUTO_ASSIGN
```

Event Detection與Runtime是independent process boundaries。Event Detection failure不得要求丟棄Runtime durable state；Runtime failure不得要求Event Detection停止durable append。

## 2.2 Domain Dependency Direction

Runtime依賴public Event、Policy、State、Incident、Workflow與Shadow ports。Domain components不得反向依賴Runtime implementation。SPEC-004 EventDetectionRunner不得匯入或承擔downstream orchestration。

## 2.3 Current Deployment Reality

Repository目前包含independent Runtime Worker、config-driven host CLI、SQLite D2 Runtime Work Store及Docker Compose `runtime` service。Host與Docker共用同一Runtime core；checked-in host config使用`run-until-idle`，Docker config使用`continuous`。Docker service透過named volumes保存authoritative EventStore與Runtime相關SQLite state，並已驗證controlled shutdown、restart continuity及opt-in Docker E2E。這是single-process／single-node PoC topology，不表示HA、distributed coordination或production readiness。

---

# 3. Runtime Logical Domain Model

## 3.1 Runtime Lifecycle

```text
STOPPED
  → STARTING
  → RECOVERING
  → READY
  → DRAINING
  → STOPPED
```

上述是Runtime process lifecycle概念，不是Incident lifecycle或必須採用的exact persisted enum。Store-wide readiness／integrity無法判斷時，Runtime不得進入READY。

## 3.2 Work Classes

Runtime至少要能協調：

- fresh durable Event processing
- unresolved MutationIntent reconciliation
- Active Pending reevaluation
- retry-eligible domain operation
- outstanding AUTO_ASSIGN
- operator-visible exhausted／repair-required work

## 3.3 Authoritative Precedence

每次執行或retry前，Runtime必須fresh-read authoritative state。優先順序由SPEC-007的resolved state contract決定；Runtime不得建立另一套precedence。概念處理如下：

1. Processed：correlation terminal no-op；不得重新evaluate或重做terminal correlation side effect。`Processed(CREATED_INCIDENT)`仍須獨立reconcile可能尚未完成的post-correlation AUTO_ASSIGN obligation。
2. Unresolved Intent：same-operation reconciliation；不得重新evaluate。
3. Active Pending／Pending+Blocked：進入Pending path。
4. Blocked：尊重既有disposition。
5. Claim-only／UNSEEN：依claim與recovery proof處理。

---

# 4. Durable Event Intake — D1

## 4.1 Formal Input Boundary

Runtime input authority是已由Detector成功寫入authoritative EventStore的完整Runtime Event：

```text
Detector → Event build → EventStore.write() → durable Event exists → Runtime intake
```

raw log、raw metric、detector window、detector return list、ScenarioRuntime state、scenario ID、validator expected answer與ground truth均不是Runtime production input。

## 4.2 Authoritative Enumeration

正式intake只能使用SPEC-001 v2.4的：

```python
EventStore.read_all_authoritative()
```

不得以legacy `EventStore.read_all()`作production Runtime authority，亦不得自行讀取`events/event_store.jsonl`。Authoritative read必須deterministic、all-or-failure；blank line可忽略，但malformed JSON、non-object JSON、UTF-8 decode failure或representation I/O failure必須以`EventStoreReadIntegrityError` Fail Closed。

Runtime不得把integrity failure解讀為empty store，不得使用已取得的partial prefix繼續terminal mutations，不得repair、truncate或重寫EventStore。

## 4.3 PoC Discovery Algorithm

每個intake cycle：

1. 呼叫authoritative enumeration取得完整成功結果。
2. 以deterministic order巡覽結果；implementation須定義穩定order，不得依hash iteration。
3. 對每個`event_id`呼叫SPEC-007 authoritative resolution。
4. 依Processed、Intent、Pending、Blocked、claim或UNSEEN分類工作。
5. 只對UNSEEN且取得claim的Event啟動INITIAL evaluation。

沒有Runtime work record的Event仍必須可由此流程發現。

## 4.4 Process-local Optimization

implementation可使用last observed position、length或已觀察ID set等process-local cache降低重複工作，但此cache：

- 不得持久化成D1 correctness cursor。
- 可在restart完全遺失。
- 不得跳過authoritative enumeration與State resolution所保證的rediscovery。
- 與authoritative evidence矛盾時必須丟棄。

## 4.5 Explicit Non-capabilities

D1不建立consumer cursor、ACK、inbox、retry queue、broker、Kafka、partition或exactly-once delivery infrastructure。Exactly-once business effect由State resolution、claim、Intent與domain receipts共同達成，而不是由Event transport宣稱。

---

# 5. Minimal Durable Runtime Execution State — D2

## 5.1 Logical Store Contract

> **Logical Store ≠ Business Authority.**
>
> **Runtime Work Ledger coordinates work; authoritative domain stores determine truth.**

Runtime Execution Store只保存跨restart仍未完成、且既有domain authority無法完整表示的orchestration obligation。

此logical framework必須可擴充至第0.5節RCA obligations；不得要求另一套RCA retry DB、scheduler authority或recovery framework。擴充不改變D2「Logical Store ≠ Business Authority」，exact RCA work record留Candidate E。

## 5.2 Minimum Conceptual Information

一筆必要work record須能表達：

- stable Runtime work identity
- work kind
- subject `event_id`及必要時`incident_id` reference
- Runtime stage／next action
- referenced domain `operation_id`，若適用
- referenced `workflow_operation_id`，若適用
- automatic retry attempt count
- next retry eligibility absolute UTC time，若適用
- last attempt／Runtime observation absolute UTC time
- source domain error code及其原始`RetryDisposition`，若適用
- AUTO_ASSIGN outstanding／completed orchestration progress，若適用

Exact record class、status enum、table、file、serialization與database technology是Implementation Choice。

## 5.3 Forbidden Copies

Runtime Execution Store不得保存為authoritative copy：

- full Event payload
- CorrelationDecision truth或candidate snapshot
- Pending、Blocked、Intent或Processed truth
- Event→Incident或Event→Shadow ownership
- Incident lifecycle、assignee或reviewer
- ShadowRecord、Resolution Evidence或RCA artifact

Reference可以保存；truth必須在每次execution/retry/recovery前由domain fresh-read。

## 5.4 Reconciliation Rule

若Runtime record與domain state不一致：

```text
fresh-read domain authority
→ verify stable operation identity／receipt／ownership
→ update or complete Runtime obligation only when safe
→ Fail Closed on contradiction
```

Runtime record不得成為durable Event intake cursor，不要求與State、Incident或Shadow共享physical DB，也不提供cross-store transaction。

## 5.5 Missing、Stale、Corrupt與Contradictory Work

Runtime Work不是判斷orchestration obligation是否存在的唯一authority。任何work處理均先fresh-read可用的authoritative domain evidence：

| Runtime Work condition | Required behavior |
|---|---|
| Missing | 不得直接解讀為no work；先判斷能否由Processed、Intent、domain ownership／receipt及current domain state deterministic reconstruction。可安全重建時才建立新的orchestration continuity record。 |
| Stale | 較新的authoritative domain state優先；只reconcile或完成Runtime work，不rollback、reassign、re-correlate、re-shadow或recreate domain state。 |
| Corrupt／malformed | 先隔離該item並以authoritative evidence判斷是否可完整重建stage、stable operation identity、attempt count與eligibility；可完整重建才受控恢復，否則Fail Closed。 |
| Contradictory | 保留Runtime與domain evidence，domain truth優先；不得以Runtime record修復domain或猜測winner，進入operator-visible governed handling。 |

若無法可靠恢復已使用的automatic retry次數，而domain receipt又不能證明operation已完成，禁止將attempt count設為0或重新給予retry budget；該work必須Fail Closed。若Runtime Work Store整體無法可靠enumerate、無法建立完整recovery set或無法區分readable與missing work，Startup Recovery Barrier不得READY，必須Fail Fast。可安全隔離的單筆corruption則可依上表per-item reconstruction或Fail Closed處理。

---

# 6. Bootstrap / Startup Recovery Barrier — D4

## 6.1 Deterministic Startup Order

Runtime啟動／重啟必須依序：

1. Bootstrap repo-relative configuration。
2. Initialize public EventStore、Policy Registry、State、Incident、Workflow、Shadow與Runtime Work components。
3. Validate config及required store readiness／integrity，包括Runtime Work Store能可靠enumerate完整recovery set。
4. 確認authoritative Event enumeration及Processed／ownership／receipt lookup可用。
5. 使用SPEC-007 public recovery capability enumerate與classify recovery-relevant states。
6. Reconcile unresolved MutationIntents。
7. 只在具有SPEC-007要求的abandonment proof時reclaim stale Claim。
8. Restore Active Pending schedule；startup時已expired者必須依latest views與exact historical policy完成final reevaluation，或依既有typed failure安全分類為Blocked，才可通過Barrier。
9. Classify Blocked records by existing disposition。
10. Reconcile D2 retry work，不重設attempt budget。
11. Reconcile／reconstruct outstanding AUTO_ASSIGN work；即使D2 record缺失，也須以`Processed(CREATED_INCIDENT)`、其destination `incident_id`、authoritative Incident current state及stable workflow operation receipt/result完成第12章reconstruction。
12. 執行correctness所需的Incident／Shadow public ownership reconciliation。
13. 確認pre-start durable Events可由D1 scan發現。
14. 完成Startup Recovery Barrier並進入READY。
15. 啟用normal Event intake。

Future RCA integration必須在相同Startup Recovery governance下，以RCA authoritative public evidence發現、分類、reconcile並resume outstanding obligations。RCA capability degraded可依PRD-004保持operator-visible而不自動否定整個平台READY；RCA authoritative state不可可靠判斷時，受影響RCA execution必須Fail Closed。Exact discovery API與barrier wiring留Candidate E。

SPEC-007的resolved state與recovery action precedence始終優先。

## 6.2 READY Semantics

Barrier保證known recovery state已被可靠讀取、分類並置於安全狀態，不保證backlog為空。Non-expired Pending可在正確重排程後進入READY；startup時已expired Pending不得只被延後排程至READY之後。Isolated per-event NON_RETRYABLE或REPAIR_REQUIRED可保持operator-visible而不阻擋所有其他安全工作。

若任何required store無法可靠enumerate、Processed／ownership lookup不可判斷、或store-wide integrity失敗，Runtime必須Fail Fast／Fail Closed，不得READY。

## 6.3 Upstream Availability During Recovery

Barrier只限制SPEC-011 normal correlation execution。Event Detection與EventStore append可以繼續；新增Event在barrier完成後由authoritative enumeration發現。

---

# 7. Normal Event Processing Flow

## 7.1 Intake State Dispatch

```text
EventStore.read_all_authoritative()
→ for each durable Event: State.resolve(event_id)
    Processed          → correlation terminal no-op；不得重做terminal correlation side effect；若為CREATED_INCIDENT，依D3／第12節reconcile post-correlation AUTO_ASSIGN obligation
    unresolved Intent  → same-operation recovery
    Active Pending     → Pending scheduler path
    Blocked            → obey existing disposition
    UNSEEN/claim-only  → claim-safe fresh processing
```

## 7.2 Fresh Processing Protocol

對UNSEEN Event：

1. acquire valid Processing Claim。
2. 立即re-resolve authoritative State，防止stale classification。
3. 確認沒有Processed、Intent、Pending或non-retryable／repair-required Block阻止fresh evaluation。
4. 從Incident authority取得latest `IncidentCorrelationView`集合。
5. 建立SPEC-006 `INITIAL` evaluation context。
6. 呼叫Policy Engine。
7. 依Decision或typed evaluation failure進入正式路徑。

所有mutation前仍須確認claim／fencing authority有效。Claim loss後executor不得繼續side effect或finalization。

---

# 8. Policy Invocation Contract

Runtime呼叫SPEC-006的正式input為：

- original authoritative Event mapping
- latest authoritative Incident correlation views
- INITIAL或由SPEC-007 Pending phase提供的evaluation context

Runtime不得：

- 改寫Event作為policy input。
- 自己計算或覆寫fingerprint、Evidence Class、Correlation Family或candidate ranking。
- 重新定義`0 <= event.detected_at - incident.last_correlated_at <= correlation_window`。
- 把evaluation failure轉成CREATE_NEW、ROUTE_SHADOW或其他business decision。
- 對unresolved Intent重新evaluate。

Policy Engine只決策；Runtime只執行其typed result。

---

# 9. Decision Execution Contract

## 9.1 Common Terminal Protocol

所有terminal decision遵守：

```text
valid claim
→ durable SPEC-007 MutationIntent
→ opposite-domain ownership precheck
→ authoritative domain mutation using Intent operation_id
→ confirm same-operation durable result／receipt
→ post-mutation Incident/Shadow ownership reconciliation
→ SPEC-007 atomic Processed finalization
→ release claim
```

若任一步無法可靠判斷，停止後續步驟並依typed failure處理。Runtime不得直接修改domain record。

## 9.2 ATTACH_EXISTING

```text
ATTACH_EXISTING
→ create SPEC-007 Intent(ATTACHED_TO_INCIDENT)
→ verify no Shadow owner through public Shadow read
→ call SPEC-008 authoritative attach mutation
→ confirm Incident operation result／receipt and Event ownership
→ verify no opposite Shadow ownership
→ finalize SPEC-007 Processed(ATTACHED_TO_INCIDENT)
→ release claim
```

Incident authority重新驗證target存在、correlation-open、event time、family、anchor與ownership。Runtime不得自己append Event ID或force attach stale/closed Incident。

## 9.3 CREATE_NEW

```text
CREATE_NEW
→ create SPEC-007 Intent(CREATED_INCIDENT)
→ verify no Shadow owner
→ call SPEC-008 create authoritative OPEN Incident
→ confirm Incident receipt／Event ownership
→ verify no opposite Shadow ownership
→ finalize SPEC-007 Processed(CREATED_INCIDENT)
→ release correlation claim
→ establish/preserve AUTO_ASSIGN obligation
→ execute SPEC-009 AUTO_ASSIGN path
```

Correlation terminal completion不依賴AUTO_ASSIGN成功。Incident ownership一旦durable，不得因assignment failure rollback。`Processed(CREATED_INCIDENT)`只禁止重做correlation；它不證明AUTO_ASSIGN已完成。D2 work缺失時仍須依第12.3節重建post-correlation obligation。

## 9.4 ENTER_PENDING

Formal enum固定使用SPEC-006 `ENTER_PENDING`，不得建立`HOLD_PENDING` enum。

```text
ENTER_PENDING
→ no Incident/Shadow side effect
→ no MutationIntent
→ SPEC-007 create/read authoritative Active Pending
→ preserve exact policy identity and absolute expiry
→ release claim
→ periodic scheduler later wakes Event
```

Runtime不得建立fake terminal Intent表示Pending。

## 9.5 ROUTE_SHADOW

```text
ROUTE_SHADOW
→ create SPEC-007 Intent(SHADOWED)
→ Runtime verifies no Incident owner through public read
→ call SPEC-010 authoritative Shadow mutation
→ SPEC-010 independently performs required Incident ownership validation
→ confirm Shadow receipt／Event ownership
→ verify no opposite Incident ownership
→ finalize SPEC-007 Processed(SHADOWED)
→ release claim
```

Runtime precheck不得bypass SPEC-010 validation，亦不得自行建立ShadowRecord。

---

# 10. Pending Scheduling / Reevaluation — D5

## 10.1 Scheduler Contract

PoC使用config-driven、positive finite periodic global Active Pending sweep；approved default為1 second。Cadence是Runtime scheduling default，不改變SPEC-007 Pending Grace或expiry semantics。

每個cycle可先處理bounded durable Events再執行Pending sweep，但implementation必須防止Pending starvation。

## 10.2 Wake-up Protocol

```text
enumerate/discover Active Pending through State public capability
→ acquire claim
→ fresh State.resolve(event_id)
→ reload original Event through EventStore authoritative enumeration result
→ obtain authoritative Runtime now
→ SPEC-007 resolve Pending phase
→ resolve exact historical policy ID/version
→ load latest IncidentCorrelationViews
→ SPEC-006 reevaluate with returned phase/context
```

Runtime不得用stale in-memory Pending snapshot判斷phase。

## 10.3 Phase and Time Boundary

SPEC-007決定：

```text
now < expires_at  → PENDING_RECHECK
now >= expires_at → PENDING_EXPIRED
```

Runtime只提供authoritative now。Restart、retry或recheck不得重設`entered_pending_at`、`expires_at`或30-second grace，不得切換到current policy；必須使用首次Pending的exact `policy_id`及`policy_version`。

## 10.4 Reevaluation Result

- 再次`ENTER_PENDING`：只進行SPEC-007允許的reason update；保持entry、expiry與policy identity。
- Terminal decision：進入第9章Intent→domain→Processed protocol。
- Typed failure：依SPEC-007記錄／更新Blocked；上游要求時Pending與Blocked可共存。
- Exact policy不可用或state矛盾：REPAIR_REQUIRED，禁止猜測或切換policy。

Expired Pending不是unconditional CREATE_NEW。它必須使用latest views與`PENDING_EXPIRED` context交由SPEC-006決策。

## 10.5 Explicit Non-design

不建立fingerprint→Pending index、Event→Pending subscription、broker或durable scheduler tick work。一般scheduler tick不建立D2 record；Pending truth與absolute expiry已由SPEC-007保存。

---

# 11. Cross-store Ownership Integrity — D6

## 11.1 Global Invariant

> **One Event, One Terminal Owner — runtime coordinates, domain stores remain authoritative.**
>
> 一個Event只能有一個終態歸屬；Runtime負責協調，但真正的權威仍留在各Domain Store。

合法terminal outcomes：

- `ATTACHED_TO_INCIDENT`
- `CREATED_INCIDENT`
- `SHADOWED`

Pending及Blocked／Failure不是terminal ownership；Processed是terminal correlation bookkeeping與dedup evidence，不是domain ownership替代品。

## 11.2 Symmetric Read-before-mutate

- Incident path在first Incident mutation前，使用public Shadow read檢查`event_id`無Shadow owner。
- Shadow path在first Shadow mutation前，使用public Incident ownership read檢查無Incident owner。
- SPEC-010仍須在自身local transaction protocol執行Incident ownership validation。
- 無法可靠取得opposite-domain evidence即Fail Closed。

## 11.3 Post-mutation Reconciliation

domain success後、Processed前，Runtime必須fresh-read：

- intended domain same-operation receipt/result
- intended Event ownership
- opposite domain Event ownership
- current State claim／Intent authority

只有intended result存在、opposite owner不存在且processing authority仍合法時，才可finalize matching Processed。

## 11.4 Contradictory Ownership

若同一`event_id`同時有Incident與Shadow authoritative ownership：

- Fail Closed。
- 保留兩邊record、receipts、Intent、State與Runtime evidence。
- 使用governed Blocked／repair-required boundary。
- 禁止delete、last-write-wins、timestamp winner、重跑Policy選winner或automatic repair。

## 11.5 Transaction Limitation

State、Incident與Shadow各自只有local transaction。SPEC-011不要求shared DB、cross-store FK、distributed ACID或2PC。Crash continuity來自Intent、stable operation identity、domain receipt／ownership與Processed reconciliation。

---

# 12. CREATE_NEW → AUTO_ASSIGN — D3

## 12.1 Required Ordering

```text
CREATE_NEW Intent
→ SPEC-008 create OPEN Incident
→ D6 reconciliation
→ SPEC-007 Processed(CREATED_INCIDENT)
→ durable Runtime AUTO_ASSIGN obligation
→ SPEC-009 AUTO_ASSIGN
```

AUTO_ASSIGN永遠是獨立SPEC-009 workflow mutation，不可合併到SPEC-008 correlation transaction。

## 12.2 Stable Workflow Identity

同一Incident的logical initial automatic assignment必須具有stable、restart-reconstructable `workflow_operation_id`。Identity須由穩定business references及AUTO_ASSIGN scope決定；exact string encoding是Implementation Choice。Runtime automation actor identity亦須stable，避免same operation在replay時形成contradictory workflow semantic identity。

每次retry/recovery必須先查詢workflow operation result及fresh Incident state，再以相同identity呼叫`auto_assign_incident`。

## 12.3 Outstanding Work

D2 record保存AUTO_ASSIGN next action、Incident reference、stable workflow operation ID及retry progress，但不是automatic-assignment obligation的唯一authority。若crash發生於Processed後、work record建立前，startup reconciliation必須執行：

```text
ProcessedCorrelationRecord(
    terminal_outcome = CREATED_INCIDENT,
    incident_id = X
)
→ read authoritative Incident X
→ verify Processed.incident_id matches authoritative Incident ownership
→ derive the same deterministic AUTO_ASSIGN workflow_operation_id
→ query authoritative SPEC-009 workflow operation result／receipt
    receipt exists
      → reconcile its result; do not issue a second assignment
    receipt absent
      → fresh-read authoritative Incident status and assignee
          OPEN + unassigned
            → reconstruct exactly one D2 outstanding AUTO_ASSIGN work
            → safely invoke SPEC-009 with the stable workflow_operation_id
          already legally assigned or lifecycle advanced
            → no AUTO_ASSIGN mutation; authoritative domain state wins
```

Incident不存在、Processed destination不等於authoritative Incident ownership、receipt矛盾或任一evidence無法可靠判斷時，必須Fail Closed；不得猜測、建立replacement Incident或重跑correlation。

## 12.4 Reconciliation Outcomes

- AUTO_ASSIGN已成功：完成work，不得second assignment、second audit或second cursor advance。
- Incident仍`OPEN + unassigned`：可依D8執行／retry AUTO_ASSIGN。
- Incident為`ASSIGNED`、`IN_PROGRESS`、`AWAITING_REVIEW`、`CLOSED`或已有合法assignee：即使D2 work缺失或stale，也不得重新AUTO_ASSIGN。
- 合法Manual Assignment或其他合法assignment已完成：取消／完成stale AUTO_ASSIGN obligation，不得覆寫人工結果或推進automatic cursor。
- Incident／receipt矛盾或authority不可讀：Fail Closed／REPAIR_REQUIRED。
- AUTO_ASSIGN transient failure：Incident保持OPEN；只retry workflow mutation。

---

# 13. Retry Contract — D8

## 13.1 Authority Rule

> **Domain decides retry safety; Runtime decides retry timing.**

Runtime必須使用source domain提供的typed error及`RetryDisposition`，不得用exception string、message文字或local guess分類。

此authority rule同樣適用future RCA work：RCA Domain提供typed retry／repair disposition，Runtime不得自行判斷grounding、provider response或publication conflict是否安全重試。

| Disposition | Runtime behavior |
|---|---|
| `RETRYABLE` | automatic bounded retry allowed |
| `NON_RETRYABLE` | 保存evidence；不得automatic retry |
| `REPAIR_REQUIRED` | Fail Closed；不得因restart automatic retry；等待governed repair/reconciliation |

EventStore `EventStoreReadIntegrityError`是authoritative read-integrity failure；Runtime不得在partial data上繼續。其operational recovery policy須保持Fail Closed且operator-visible，不得將其偽裝成empty input或business decision。

## 13.2 PoC Default Budget

既有D8 budget適用既有Runtime work。PRD-004所定initial try加最多4 retries、`1s／2s／4s／8s`可由Runtime按RCA domain policy排程，但不升級為所有work kind的全域固定policy，也不得與provider SDK隱藏retry疊加成第二budget。

第一次失敗後最多四次automatic retries：

```text
initial attempt
retry #1 after 1 second
retry #2 after 2 seconds
retry #3 after 4 seconds
retry #4 after 8 seconds
```

Default無jitter。Policy可config-driven，但不得改變domain retryability；retry delay與budget必須是positive、finite及可驗證。

## 13.3 Stable Identity and Fresh Reads

同一logical side effect所有retry必須重用原domain operation identity。每次retry前先fresh-readState、claim、Intent、receipt、ownership及適用的Incident/workflow state；若domain已完成，應reconcile而非再次產生business operation。

## 13.4 Durable Retry Continuity

D2至少保存attempt count、具有canonical UTC semantics的next eligibility absolute time、stable operation reference及next action。Crash/restart不得將attempt count歸零或重新發號operation ID；corrupt metadata無法由authoritative evidence完整重建時亦不得補發retry budget。

## 13.5 Exhaustion

budget exhausted是Runtime operational status，不是新domain error classification。Runtime須停止automatic retry、保存source error/disposition與attempt evidence、維持operator-visible；不得假裝成功、delete authority或重新分類failure。

---

# 14. Time / Clock Semantics — D8

## 14.1 Authoritative Runtime Clock

Runtime提供injectable、timezone-aware absolute wall-clock `now`。Caller不必先轉換為UTC；例如`2026-09-13T10:00:00+08:00`是合法absolute input。此`now`供：

- SPEC-007 Pending phase input
- Intent／Processed相關Runtime提供的timestamp
- SPEC-008 Incident mutation `now`
- SPEC-010 Shadow mutation `now`
- SPEC-009 workflow operation `now`
- retry eligibility及Runtime durable timestamps
- future RCA post-context absolute wake-up及RCA retry／publication reconciliation eligibility

Production orchestration不得讓domain adapter偷偷另取wall clock作本次operation的authoritative time。

Runtime Work的retry eligibility、attempt／observation time及recovery bookkeeping等durable Runtime timestamps必須以canonical UTC semantics持久化，或使用可無損等價轉換至UTC、無時區歧義且可安全比較的absolute representation。各Domain Store仍依自身authority canonicalize其durable timestamps。

## 14.2 Upstream Time Authority

Runtime Clock不得重定義或覆寫：

- RCA Domain的`episode_end`、post-context boundary、material difference或refresh obligation判斷；Runtime只計算／執行authoritative absolute wake-up。

- `Event.detected_at`
- `Incident.last_correlated_at`
- Correlation Window
- Pending stored `entered_pending_at`／`expires_at`
- domain receipt原始timestamp

Replay必須保留original domain result時間。

## 14.3 Monotonic Clock

Monotonic clock只用於process-local wait、elapsed measurement與loop scheduling；不得persist為Incident、Pending、Shadow、workflow或business timestamp。Wall clock、monotonic clock與sleeper均須injectable，必要tests不得等待真實1／2／4／8秒。不同timezone offsets若表示相同absolute instant，Runtime phase、retry eligibility與domain input semantics必須等價，correctness不得依賴system local timezone。

---

# 15. Failure Handling

## 15.1 Failure Sources

Runtime至少區分source authority：

- SPEC-006 correlation evaluation failure
- SPEC-007 state／recovery failure
- SPEC-008 Incident failure
- SPEC-009 workflow failure
- SPEC-010 Shadow failure
- EventStore read-integrity failure
- Runtime internal／orchestration failure

Runtime telemetry保留原source code與disposition，不建立競爭的business taxonomy。

## 15.2 Operational Categories

- Recoverable interruption：由Intent／receipt／work metadata恢復same operation。
- Retryable transient failure：依D8 bounded retry。
- Non-retryable failure：記錄並停止automatic retry。
- Repair-required／authoritative contradiction：Fail Closed並等待governed action。
- Retry exhaustion：停止automatic retry但不改domain classification。
- Runtime programming/internal failure：不得轉換成CorrelationDecision、CREATE_NEW或ROUTE_SHADOW。

## 15.3 Whole-store Failure

authoritative store無法可靠讀取、enumerate或判斷integrity時，Runtime不得READY。運行中發生同類failure時，受影響execution不得使用partial/stale evidence繼續terminalization，並須operator-visible。

---

# 16. Persistence、Concurrency與Crash Consistency

## 16.1 Concurrency Rules

| Race | Required behavior |
|---|---|
| Same Event duplicate intake | State resolve + claim + Processed + stable IDs；最多一個terminal effect |
| Same Event concurrent executors | 只有有效fencing claim可mutate；loser停止 |
| Same fingerprint multiple Events | 使用SPEC-006與各次latest views；Runtime不得猜排序結果 |
| Pending + new Strong Event | 不建立direct index；periodic recheck讀latest Incident state |
| ATTACH vs lifecycle transition | SPEC-008/009 fresh transaction及typed disposition決定；不得force attach |
| CREATE_NEW replay | same Intent／operation ID及Incident receipt |
| Shadow vs Incident | D6 precheck、domain validation、post-reconcile；矛盾Fail Closed |
| recovery vs live ingestion | Startup Barrier完成後才開normal intake |

## 16.2 Crash Matrix

| Crash point | Durable evidence | Recovery behavior |
|---|---|---|
| Before State Claim | Event only | D1 rediscover；重新resolve與claim後可fresh evaluate |
| After Claim, before Decision | Event + claim | 僅在abandonment proven後reclaim；fresh views/evaluation |
| After Active Pending durable transition | ActivePendingRecord + EventStore Event + exact policy reference + absolute expiry | 不得當INITIAL；依authoritative now恢復RECHECK／EXPIRED path，不reset grace |
| After Intent, before domain call | Intent | resume same Intent／operation ID；不得reevaluate |
| During domain local transaction | Intent；domain commit未知 | lookup same operation receipt／ownership；安全same-ID replay |
| Domain commit, response lost | Intent + domain receipt | retrieve/replay same result；post-reconcile |
| Domain success, before Processed | Intent + receipt／ownership | verify both stores與claim；finalize matching Processed |
| After non-CREATE_NEW Processed | Processed + domain ownership | correlation terminal no-op；不增加terminal side effect |
| Processed(CREATED_INCIDENT), before AUTO_ASSIGN work | Processed outcome/destination + authoritative OPEN Incident + workflow result absence | 依第12.3節完整chain重建same stable AUTO_ASSIGN obligation；不得重新correlate |
| AUTO_ASSIGN work durable, before workflow call | Runtime work + Incident/workflow state | fresh-read後以same workflow ID呼叫 |
| AUTO_ASSIGN commit, acknowledgement lost | workflow receipt | same-ID lookup/replay；完成work，不推進cursor第二次 |
| Retry scheduled, before retry | D2 attempt count + absolute eligibility + stable operation identity | 保留budget；到期前不執行，retry前fresh-read authority |
| Retry domain commit, before Runtime bookkeeping | same operation domain receipt/result + D2 work | lookup/confirm committed result，不重做business mutation；reconcile Runtime work及attempt completion |
| Controlled drain/shutdown during claim, Intent, retry or AUTO_ASSIGN | State／Intent／domain receipt／D2 work中已到達的durable safe evidence | 停止取得新work；目前work完成至safe boundary或留下足夠recovery evidence後exit，不為關機清除authority |
| Forced process interruption during drain | crash前最後durable authority | 下次startup走相同normal recovery，不依賴drain process memory |
| Startup Recovery interrupted by another restart | authoritative stores、receipts及已durable Runtime work | 下一次startup重新authoritative discovery、resolution、receipt reconciliation與work reconstruction，重新完成Barrier |
| Active Pending跨越expiry restart | Pending absolute state | latest views + PENDING_EXPIRED reevaluation |
| Repeated restart | domain receipts/state/work | 每次從authority重新reconcile且idempotent；不改terminal outcome、Pending expiry或retry budget |
| Incident + Shadow contradiction | both domain owners + state evidence | Fail Closed、preserve evidence、governed repair only |

表格列數不是correctness contract；正式要求是每個meaningful interruption window都有不依賴process memory的authoritative recovery source。Intent recovery只處理same operation。除fresh pre-Intent Event及SPEC-007明確要求的Pending reevaluation外，recovery不得重新執行SPEC-006。

---

# 17. Structured Observability

## 17.1 Boundary

Runtime telemetry是observation，不是authority。它不取代SPEC-007 Processed／Blocked、SPEC-008 audit／receipt、SPEC-009 workflow audit／receipt或SPEC-010 receipt。

Future RCA telemetry亦只觀察generation attempt、retry、refresh、provider invocation、failure disposition與publication reconciliation；不得取代RCA artifact／attempt／publication authority，exact signal names留Candidate E。

## 17.2 Minimum Structured Fields

適用時記錄：

- `event_id`
- Runtime stage
- `policy_id`／`policy_version`
- correlation decision
- domain `operation_id`
- `workflow_operation_id`
- claim acquisition／loss
- destination `incident_id`／`shadow_id`
- source domain與existing error code
- retry disposition、attempt與next eligibility
- reconciliation result
- Runtime observation timestamp

Exact logger event enum與field transport是Implementation Choice；敏感payload不得為debug方便被完整輸出。

## 17.3 Minimum Counters

須可觀測：

- Events observed及already-terminal skipped
- ATTACH_EXISTING／CREATE_NEW／ENTER_PENDING／ROUTE_SHADOW
- Processed completions
- Pending entered／active／reevaluated／expired
- Blocked
- automatic retries及retry exhausted
- reconciliation success／failure
- AUTO_ASSIGN pending／success／failure
- startup／recovery／READY／stop

## 17.4 Useful Gauges

至少應能提供或推導：Active Pending、Blocked、unresolved Intent與outstanding AUTO_ASSIGN backlog。Exact metric names不在v0.1 freeze。

---

# 18. Configuration / Bootstrap / Portability

## 18.1 Runtime-owned Configuration

可包含：

- Event intake poll cadence
- Pending scan cadence；PoC default 1 second
- retry budget及1／2／4／8-second defaults
- Runtime Execution Store adapter/path
- loop／drain behavior
- stable automation actor
- telemetry settings

所有path必須repo-relative或config-driven，不得含personal absolute path。

## 18.2 Forbidden Overrides

Runtime config不得覆寫Evidence Class、Correlation Family、Strong/Weak identity、fingerprint、candidate logic、Correlation Window semantics、Pending policy、Incident lifecycle、Shadow reason、Event schema、policy version或domain retry disposition。

## 18.3 Bootstrap Reproducibility

- 不依賴手動預建DB或hidden previous-run state。
- 不依賴OS task scheduler或PM-only launch sequence。
- local stores可由每台machine各自擁有，但bootstrap與semantics須可重現。
- 同一committed config須產生相同Runtime semantics。
- implementation closure至少需PM environment與一台non-PM clean environment正式E2E PASS。
- competition baseline freeze前，目標是四位成員均能依README/setup contract啟動Demo path；不要求每個commit四台full regression。

---

# 19. Runtime Entry Points / Process Boundary — D7

Runtime implementation必須提供first-class、config-driven Python execution surface，並以同一core orchestration semantics支援：

- one-shot／controlled drain或run-until-idle
- continuous long-running processing
- startup recovery invocation
- graceful stop

Mode只能改變loop/lifecycle，不得分叉policy、ownership、retry或recovery semantics。Exact module、class、CLI command及flags是Implementation Choice。

Graceful stop須停止取得新work，讓已取得work依安全boundary完成或留下durable recovery evidence；不得為關機清除claim、Pending、Intent、Processed或Runtime work。

Future RCA work整合後適用相同controlled drain與startup recovery principle：restart不等於reset，不得重給retry budget、重設post-context wake time或改變logical publication identity。

Docker不是唯一correctness environment；host與Docker使用相同core orchestration semantics。Implementation closure已建立Compose `runtime` service、Docker config與named-volume restart continuity，並完成獨立opt-in Docker E2E。Multi-node、HA及active-active仍不在PoC requirement。

---

# 20. Runtime E2E Definition

## 20.1 Path A — Attach

```text
durable Event
→ authoritative intake
→ State resolve／Claim
→ SPEC-006 ATTACH_EXISTING
→ Intent
→ D6 ownership guard
→ SPEC-008 attach／receipt
→ D6 reconciliation
→ Processed(ATTACHED_TO_INCIDENT)
```

## 20.2 Path B — Create + Assignment

```text
durable Event
→ State／Policy CREATE_NEW
→ Intent
→ D6 ownership guard
→ durable OPEN Incident／receipt
→ D6 reconciliation
→ Processed(CREATED_INCIDENT)
→ durable outstanding AUTO_ASSIGN
→ SPEC-009 AUTO_ASSIGN
→ durable ASSIGNED
```

## 20.3 Path C — Pending

```text
durable Event
→ ENTER_PENDING
→ durable Active Pending
→ periodic wake
→ authoritative phase + latest views + exact policy
→ RECHECK or EXPIRED evaluation
→ eventual terminal domain result
→ Processed
```

## 20.4 Path D — Shadow

```text
durable Event
→ ROUTE_SHADOW
→ Intent
→ D6 ownership checks
→ SPEC-010 Shadow ownership／receipt
→ D6 reconciliation
→ Processed(SHADOWED)
```

## 20.5 Human Workflow Boundary

SPEC-011只自動觸發CREATE_NEW後的AUTO_ASSIGN。Manual assignment、reassignment、start work、submit resolution、review、recovery verification與closure是human／controlled actions；Runtime不得自動推進`ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED`。

---

# 21. Competition Evaluation Boundary

Production authority禁止加入`scenario_id`、`evaluation_run_id`、ground truth、expected answer、Generator internal state或validator metadata。

Evaluation evidence取得優先順序：

```text
existing evidence
→ offline derivation
→ evaluation-only instrumentation
→ production instrumentation
→ domain contract change
```

Runtime及既有domain evidence須足以讓external evaluation layer取得：

- EventStore run boundary／new Event IDs
- Runtime intake observation
- Pending enter／recheck／resolution
- downstream mutation result及terminal ownership
- retry attempts
- startup／recovery／READY
- AUTO_ASSIGN result
- authoritative domain timestamps與Runtime observation timestamps

此evidence支援EVAL-07 replay ×1／×10／×100、EVAL-08 restart/crash、EVAL-09 single ownership及EVAL-12 Time to Actionable Incident分析。v0.1不freeze EVAL-12 endpoint；後續可依CREATE_NEW、ATTACH_EXISTING、Pending→Attach及Pending→Create的實際path定義measurement semantics，不得為KPI改domain schema。

---

# 22. Security / Data / Answer Leakage Boundary

- Runtime configuration不得包含committed secrets；secret sourcing沿用deployment governance。
- Telemetry只記錄必要references與operational metadata，不應完整記錄raw logs、full Event payload、Resolution Evidence或可能含個資／憑證的內容。
- Error message不得把secret、完整raw sample或private persistence內容當作structured identity。
- Runtime不得使用competition answer、scenario metadata或hidden state影響production decision。
- Runtime Work Store及telemetry access須遵守其所引用domain evidence的最小權限原則。

---

# 23. Retention / Reset / Repair Governance

本SPEC不定義production retention/archive engine。任何cleanup不得破壞Event immutable evidence、Processed dedup、Pending continuity、Intent recovery、domain ownership/receipt或outstanding work。

Startup recovery不是reset。禁止為demo或測試便利自動：

- truncate EventStore
- delete Pending、Blocked、Intent或Processed
- clear claims without abandonment proof
- delete Incident或Shadow
- recreate authority DB
- delete contradictory evidence

Repair-required與ownership contradiction只能進入future governed repair流程；v0.1不授權destructive或automatic repair tooling。

---

# 24. Acceptance Criteria

## AC-011-A — Authority / Boundary

- Runtime不重定義Event、Policy、State、Incident、Lifecycle或Shadow。
- 所有domain action只使用public semantic APIs。
- Runtime Execution Store與telemetry均不成為business authority。
- `Approved ≠ Implemented`治理區別仍維持；v1.0既有scope有closure evidence，v1.1 RCA additive boundary仍為Implementation Pending。

## AC-011-B — Authoritative Event Intake

- 只處理先由EventStore durable persist的Event。
- production intake使用`read_all_authoritative()`，不使用legacy `read_all()`或direct JSONL parse。
- malformed、non-object、decode及read failure Fail Closed且無partial-success processing。
- restart可重新發現durable但UNSEEN Event，correctness不依賴cursor或process cache。
- Processed Event不產生第二次terminal mutation。

## AC-011-C — Terminal Decision Paths

- ATTACH_EXISTING、CREATE_NEW、ROUTE_SHADOW均在side effect前durable Intent。
- domain success由authoritative receipt／ownership確認。
- D6 reconciliation成功後才finalize matching Processed。
- replay重用same operation ID且不增加domain mutation/audit。

## AC-011-D — Pending

- ENTER_PENDING建立authoritative Active Pending且不建立terminal Intent。
- exact policy、entry及absolute expiry在recheck/restart不變。
- SPEC-007依injected now決定RECHECK／EXPIRED。
- reevaluation使用latest Incident views，不使用candidate snapshot。
- expiry執行final Policy evaluation而非unconditional CREATE_NEW。
- Pending與Blocked coexistence依SPEC-007處理。

## AC-011-E — AUTO_ASSIGN

- CREATE_NEW先產生durable OPEN Incident及Processed。
- correlation terminal completion不依賴assignment成功。
- outstanding assignment具有durable、可重建work及stable workflow operation ID。
- 即使D2 work尚未建立或遺失，`Processed(CREATED_INCIDENT)`、其destination `incident_id`、authoritative Incident status／assignee、deterministic workflow operation ID及workflow result／receipt可重建exactly one obligation。
- Processed destination必須與authoritative Incident ownership一致；矛盾時Fail Closed。
- assignment failure不rollback OPEN Incident。
- restart及commit-response-loss可same-ID replay，assignment/audit/cursor只發生一次。
- 合法人工assignment使stale AUTO_ASSIGN不得覆寫或推進cursor。

## AC-011-F — Startup Recovery

- normal live intake前完成Recovery Barrier。
- unresolved Intent、Active/expired Pending、Blocked、claim、retry work及AUTO_ASSIGN均可靠分類。
- stale Claim只在abandonment proven後reclaim。
- pre-start Event可由D1發現。
- Runtime Work的missing、stale、corrupt與contradictory case均先對照authoritative domain evidence；無法安全重建時Fail Closed且不reset retry budget。
- Startup Recovery中再次crash時，下一次startup重新authoritative discovery與reconciliation；repeated restart idempotent且不reset authority。
- per-event isolated Block或可隔離的Runtime Work corruption可operator-visible；任一required store整體無法可靠enumerate完整recovery set時不得READY。

## AC-011-G — Global Ownership / Cross-store Reconciliation

- 一個Event最多一個Incident或Shadow terminal owner。
- Incident與Shadow path均有opposite-domain precheck及post-reconciliation。
- 使用public reads，不查private tables。
- 同時存在兩種owner時Fail Closed、保存evidence、不得automatic repair。
- tests不得假設shared transaction或2PC。

## AC-011-H — Retry / Clock

- 只有RETRYABLE自動retry；NON_RETRYABLE與REPAIR_REQUIRED不自動retry。
- default最多四次retries，schedule為1／2／4／8 seconds且無jitter。
- restart保留attempt count、next eligibility與stable operation identity。
- exhaustion不改domain classification。
- domain `now`來自injectable timezone-aware absolute Runtime Clock；caller不必預先轉成UTC。
- durable Runtime work、retry eligibility及recovery bookkeeping timestamps具有canonical UTC semantics。
- 不同timezone offsets表示相同instant時產生等價Runtime semantics；不得依賴system local timezone。
- monotonic timestamp不作durable business authority，tests不需real sleep。

## AC-011-I — Observability / Evaluation

- structured telemetry可追蹤startup、intake、Pending、domain attempt、retry、reconciliation、AUTO_ASSIGN及stop。
- failure保留source domain code/disposition與attempt evidence。
- telemetry不取代State、receipt或business audit。
- EVAL-07／08／09及EVAL-12所需evidence可由production evidence或external evaluation derivation取得。
- production Event/domain/Runtime authority不包含scenario answer metadata。

## AC-011-J — Concurrency / Replay

- same Event duplicate intake與concurrent processing最多一個terminal effect。
- CREATE_NEW、ATTACH與ROUTE_SHADOW equivalent replay exactly-once。
- Pending與concurrent Incident changes使用latest views。
- lifecycle race依SPEC-008/009 typed result處理。
- recovery與live ingestion由Barrier隔離。
- stale claim及Incident/Shadow race均Fail Closed。

## AC-011-K — Runtime E2E / Portability

- Attach、Create+AUTO_ASSIGN、Pending及Shadow四條正式path均有real-domain E2E evidence。
- continuous與drain mode使用相同core semantics。
- first-class config-driven CLI可在host環境執行。
- 無personal absolute path、manual pre-created DB或hidden previous-run依賴。
- PM environment及至少一台non-PM clean environment E2E PASS。
- Docker Runtime integration通過前不得宣稱complete Runtime/Docker Correlation E2E。
- correctness以contract coverage判斷，不以固定test count宣稱。

## AC-011-L — Additive RCA Runtime Boundary（Implementation Pending）

- D2 framework可承接initial、Material Evidence refresh、post-context、Attempt retry、STALE refresh及Publication Reconciliation obligations，而不建立第二套Runtime authority。
- Startup Recovery可透過RCA authoritative public evidence發現、分類、reconcile及resume outstanding obligations；RCA truth不可可靠判斷時受影響work Fail Closed。
- RCA Domain決定retry safety／failure disposition，Runtime只依domain policy與D8治理排程；RCA budget不改成所有Runtime work的global policy。
- Post-context使用authoritative absolute Runtime Clock並跨restart保留；RCA Domain仍擁有episode／materiality判斷。
- Publication Reconciliation只協調RCA authority與SPEC-008 public semantics，不direct-write domain stores、不建立2PC且不擁有RCA truth。
- RCA telemetry與controlled restart沿用既有framework；exact metric、work enum、record、worker及operation identity encoding均未由v1.1凍結。

---

# 25. Required Test Layers

Implementation至少建立：

1. Runtime orchestration unit／domain tests。
2. Runtime Execution Store persistence／restart tests。
3. Event intake authoritative integrity tests。
4. Real SPEC-006 Policy integration tests。
5. Real SPEC-007 claim／Pending／Intent／Processed／Blocked integration tests。
6. Real SPEC-008 create／attach／receipt integration tests。
7. Real SPEC-009 AUTO_ASSIGN／workflow replay integration tests。
8. Real SPEC-010 Shadow／ownership integration tests。
9. Retry、Fake Clock及deterministic scheduler tests。
10. Crash-window及repeated-restart tests。
11. Concurrency／fencing tests。
12. Cross-store precheck與post-reconciliation tests。
13. 四條Runtime E2E paths。
14. Full repository regression。
15. v1.1 RCA Runtime future tests：work continuity、startup discovery、typed retry scheduling、absolute post-context wake-up、publication reconciliation、telemetry與controlled restart；本次reconciliation不宣稱此layer已實作或通過。

必要unit/contract tests不得依賴real Docker、network、long real sleep、real wall clock或competition expected answer。Docker/local-infrastructure E2E須與fast deterministic regression分離。

## 25.1 Mandatory Semantic Recovery Scenarios

除上述test layers外，至少須明確驗證：

- **AUTO_ASSIGN zero-record recovery**：建立`Processed(CREATED_INCIDENT)`、matching `OPEN + unassigned` Incident、無D2 AUTO_ASSIGN work且無workflow receipt；restart後只重建一筆stable obligation。
- **Manual assignment wins**：相同zero-record前提下，若restart前已合法manual assign，Runtime不得AUTO_ASSIGN、覆寫assignee或推進automatic cursor。
- **Workflow committed／Runtime incomplete**：AUTO_ASSIGN已commit但D2 work未標完成；restart透過same-ID workflow receipt reconciliation，不產生duplicate assignment／audit／cursor advance。
- **Runtime Work corruption**：可由完整authoritative evidence重建者受控恢復；無法確定attempt count、stage或identity者Fail Closed且不得reset budget。
- **Equivalent absolute clock offsets**：例如`2026-09-13T10:00:00+08:00`與`2026-09-13T02:00:00+00:00`產生相同phase、retry eligibility與domain time semantics。
- **Repeated startup interruption**：Recovery Barrier途中多次crash後重新authoritative discovery；不得duplicate Incident／attach／Shadow／assignment，不reset Pending或retry budget。

---

# 26. PoC、Demo與Production Hardening

## 26.1 Correctness — 不可犧牲

durable Event-first、read integrity、deterministic Policy、single ownership、idempotency、Intent、Processed、absolute Pending expiry、exact policy、fail-closed、restart continuity、stable identity、bounded safe retry及authoritative Runtime now。

## 26.2 PoC Required

single-node Runtime、authoritative enumeration、四條decision paths、Startup Barrier、periodic Pending sweep、durable execution continuity、AUTO_ASSIGN recovery、structured telemetry及deterministic retry。

## 26.3 Demo Required

Event→Decision→destination trace、OPEN→AUTO_ASSIGN、Pending、Shadow、replay無duplicate及restart/reconciliation evidence。

## 26.4 Future Production Hardening

Kafka、HA、multi-node workers、message broker、distributed scheduler、production DB、distributed transaction evaluation、retention/archive、administrative repair tooling及external adapters。PoC不得以competition為理由降低第26.1節。

---

# 27. Explicit Out of Scope

- Event Detection algorithm、Event Builder、Event schema或Event status mutation
- Strong／Weak、fingerprint、candidate、window、reason code或policy learning redesign
- Pending／Processed／Blocked／Intent／Claim semantics redesign
- Incident mutation、Late Promotion、lifecycle或Resolution Evidence redesign
- Human Review、Shadow classification/reason/review/clustering/learning
- RCA／RAG business semantics、RCA worker／record implementation、Knowledge workflow、Jira、Discord／ChatOps、Email、Dashboard UI；第0.5節僅凍結Runtime accommodation boundary
- automatic remediation或detector retraining
- Kafka、distributed ingestion、distributed scheduler、HA／active-active
- cross-store 2PC、shared domain DB或distributed lock
- production retention/archive、DB migration、automatic/destructive repair

---

# 28. Documentation Reconciliation Impact

Post-Implementation Documentation Reconciliation已於2026-09-16執行，並同步：

- README：Runtime status、CLI及honesty boundary。
- DDS-001：independent worker、D2 persistence及cross-store protocol。
- Architecture diagram：Detector→EventStore→Runtime→domain stores。
- Runtime/process topology與setup documentation。
- Docker topology：依已實作的Compose `runtime` service、Docker config與named volumes更新。

D1～D8是既有approved product requirements的engineering orchestration contract；2026-09-18 Post-PRD-004 reconciliation只新增第0.5節future RCA accommodation boundary，不修改其semantics。SPEC-006、007、009、010 semantics未被改動；SPEC-008僅另行加入相配的RCA relationship integration boundary。

---

# 29. Implementation Consideration — Non-normative Suggested Sequence

本節只提供風險導向的建議實作順序，不是correctness、acceptance或architecture authority。實作者可在不違反本SPEC observable contract、acceptance criteria及dependency prerequisites的前提下調整內部實作順序：

1. 定義public Runtime ports、Clock、config validation與D2 logical adapter contract。
2. 實作Startup Barrier及D1 intake/state dispatch。
3. 實作terminal Intent/domain/Processed與D6 reconciliation。
4. 實作Pending scheduler與exact-policy reevaluation。
5. 實作AUTO_ASSIGN work/reconstruction。
6. 實作bounded retry及structured telemetry。
7. 建立crash/concurrency與四路E2E evidence。
8. 建立CLI、drain/continuous modes及Docker integration gate。
9. Full regression、cross-machine validation與documentation reconciliation。

無論採何種順序，均不得藉implementation convenience修改frozen upstream authority。若public capability不足，停止受影響工作並回報PM。

---

# 30. Historical v1.0 PM Review／v1.1 Reconciliation Checklist

- [x] v1.0 existing scope Status為Implemented，並記錄implementation commit、Final Full Contract Audit及PM Final Review evidence；v1.1 RCA additive boundary明示Implementation Pending。
- [x] D1只使用`read_all_authoritative()`且維持durable Event-first。
- [x] D2只保存orchestration continuity，未建立第二business truth。
- [x] D2 missing／stale／corrupt／contradictory work均依authoritative evidence處理，retry budget不reset。
- [x] D3為Processed-first及獨立AUTO_ASSIGN，zero-record crash可由Processed destination與domain/workflow evidence重建，未合併SPEC-008/009 transaction。
- [x] D4為recovery-first barrier，但不要求empty backlog。
- [x] D5 phase/expiry/exact policy仍由SPEC-007 authority決定。
- [x] D6具symmetric precheck/post-reconcile，沒有2PC、winner guessing或automatic repair。
- [x] D7維持independent worker及shared core semantics。
- [x] D8只對RETRYABLE執行1／2／4／8 bounded retry且保留domain taxonomy。
- [x] Runtime Clock接受timezone-aware absolute now；durable Runtime timestamps為canonical UTC，monotonic與upstream timestamps邊界清楚。
- [x] 四條E2E path及human workflow boundary清楚。
- [x] v1.0 implementation無scenario answer leakage、RCA或external adapter scope creep；v1.1只新增Runtime accommodation contract，未設計或實作RCA Domain。
- [x] Retention/reset章節禁止destructive recovery。
- [x] AC-011-A～K已有對應contract verification evidence。
- [x] Architecture／README／Docker documentation已按implementation reality reconciliation，未作超額宣稱。
- [x] AC-011-L只記錄future RCA Runtime boundary；未宣稱implementation／test evidence，未改D1～D8。

---

## Approval Freeze Gate

本文件目前為v1.1。v1.0既有scope的Implementation、Docker integration、Final Full Contract Audit及PM Final Review均已完成；D1～D8持續凍結且未被本次reconciliation改變。v1.1僅新增PM-approved RCA Runtime accommodation boundary，Implementation Status為Pending；exact RCA work record／worker／metric未凍結。RCA business semantics、external adapters、HA／distributed runtime、production hardening與完整AIOps closed loop仍不在既有implementation closure範圍內。
