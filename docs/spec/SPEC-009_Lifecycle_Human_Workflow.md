# SPEC-009 — Lifecycle / Human Workflow

## Software Design Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-009 |
| Document Name | Lifecycle / Human Workflow |
| Version | 1.0 |
| Status | Implemented |
| Date | 2026-09-10 |
| Requirement Authority | PRD-003 v1.0 Final |
| Overall Product Context | PRD-001 v3.4 |
| Upstream Event Contract | PRD-002 v1.5 Approved |
| Upstream Correlation Contract | SPEC-006 v1.0 Implemented |
| Upstream Correlation State Contract | SPEC-007 v1.0 Implemented |
| Incident Domain Authority | SPEC-008 v1.1 Implemented |
| Related Shadow Contract | SPEC-010 v1.0 Implemented |
| Downstream Runtime Boundary | SPEC-011 |
| Implementation Owner | 夜羽 |

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-09-10 | Initial Draft；定義既有 Incident authority內的strict lifecycle、Assignment、workflow idempotency、Resolution Evidence revisions、Human Review、Recovery Verification、closure、audit、concurrency及additive persistence upgrade工程契約。 |
| 0.1 | 2026-09-10 | Revision Round 2；凍結`REVIEW_ATTEMPT`／closure operation model、per-action replay identity、Unified Incident Timeline cross-source ordering、typed workflow time regression、`IncidentCorrelationView` lifecycle propagation及Acceptance Criteria coverage；Version與Draft status不變。 |
| 1.0 | 2026-09-10 | PM Final Approval completed；D1～D12 frozen for implementation，Revision Round 1 R1～R5與Revision Round 2 R2-009-01～06均已納入。Status更新為`Approved — Implementation Pending`；Implementation Owner為夜羽。 |
| 1.0 | 2026-09-10 | Implementation Status Reconciliation（historical）；PM implementation authorization與Implementation Work Instructions已於v1.0 approval後issued，Status更新為`Approved — Implementation In Progress`。Owner維持夜羽；當時首先授權Phase 1 — Workflow Domain Contracts；Engineering Contract semantics未變更。 |
| 1.0 | 2026-09-11 | Final Implementation Progress Reconciliation；Phase 1～6均已完成並PASS，Current Gate更新為`Final Full Contract Audit / PM Final Review`，Implementation Candidate State為`COMPLETE — PM FINAL REVIEW PENDING`。Status、Version、Owner及Engineering Contract semantics未變更。 |
| 1.0 | 2026-09-11 | Implementation Closure；SPEC-009 v1.0 implementation completed。Phase 1～6 implementation及audit gates、Final Full Contract Re-Audit與PM Final Review均PASS。Regression evidence：SPEC-009 targeted 56 passed；SPEC-006 relevant 172 passed；SPEC-007 relevant 78 passed；SPEC-008 relevant 131 passed；SPEC-010 relevant 55 passed；full repository 921 passed / 0 failed；Syntax / Import PASS。D1～D12與AC coverage為contract basis，test count不是correctness定義。Implementation deviation：NONE。Status由`Approved — Implementation In Progress`升級為`Implemented`；Version、Owner與Engineering Contract semantics未變更。 |

> **Implementation Status Honesty：SPEC-009 Implemented = YES。** 此狀態僅表示SPEC-009 v1.0定義並已核准的Lifecycle／Human Workflow engineering contract已實作並驗證。它不表示SPEC-011 Implemented、Full AIOps Runtime Complete、full platform E2E Complete、RCA workflow Implemented、Shadow Review Implemented、Knowledge workflow Implemented或Production Ready。

---

# 0. Authority、目的與核心定位

## 0.1 Authority hierarchy

1. `PRD-003 v1.0 Final`是Alert Correlation／Incident Management requirement authority。
2. `PRD-001 v3.4`提供Incident-driven、Human-in-the-loop整體產品方向。
3. `PRD-002 v1.5 Approved`是15-field immutable Runtime Event與EventStore authority。
4. `SPEC-006 v1.0 Implemented`是Correlation Decision、candidate、fingerprint與policy semantics authority。
5. `SPEC-007 v1.0 Implemented`是Correlation execution state、Pending、Processed、Blocked、Intent與Claim authority。
6. `SPEC-008 v1.1 Implemented`是authoritative Incident persistence、correlation-driven mutation、Event ownership、Incident current state與local transaction boundary authority。
7. `SPEC-010 v1.0 Implemented`是Shadow／Unclassified persistence與Shadow ownership authority。
8. `SPEC-011`是future Runtime Orchestration與downstream E2E boundary。

Production code只作implementation reality reconciliation，不構成requirement authority。若implementation與正式authority衝突，必須停止受影響範圍並回報PM，不得讓SPEC靜默遷就code。

## 0.2 Purpose

SPEC-009是既有SPEC-008 Incident domain authority的**additive post-OPEN workflow extension**：

```text
SPEC-008
Correlation Decision
→ durable Incident OPEN
→ authoritative Incident current state
→ correlation attach / promotion

SPEC-009
OPEN
→ Assignment
→ ASSIGNED
→ Engineer starts work
→ IN_PROGRESS
→ Resolution Evidence
→ AWAITING_REVIEW
→ Human Review + Recovery Verification
→ CLOSED
```

SPEC-009不得建立`LifecycleStore`、`lifecycle_store.db`、第二個Incident database或parallel authoritative lifecycle service。Authoritative Incident state持續屬於既有Incident Manager／Incident Store。

責任原則：

```text
Engine decides. State remembers. Runtime orchestrates.
Domain stores own the side effects.
```

---

# 1. Responsibility Boundary

## 1.1 MUST

- 在既有Incident authority內執行strict lifecycle transition；
- 執行automatic assignment、manual assignment與reassignment semantics；
- durable保存Round Robin state及exact Assignment Policy identity；
- 每個authoritative workflow mutation使用獨立durable `workflow_operation_id`；
- durable保存append-only Resolution Evidence revisions與Review Attempts；
- enforce latest revision、same-attempt Review與Recovery Verification gates；
- 產生typed workflow audit並提供Unified Incident Timeline；
- 與correlation mutation共用Incident local transaction／write serialization；
- 維持CLOSED terminal、retention、integrity及Fail Closed semantics。

## 1.2 MUST NOT

- 建立第二套Incident persistence或lifecycle authority；
- redefine或mutate PRD-002 Event；
- 執行SPEC-006 correlation decision或candidate matching；
- 建立或mutate SPEC-007 Pending／Processed／Blocked／Intent／Claim；
- 實作Shadow review或修改SPEC-010 `review_status`；
- 實作SPEC-011 orchestration或full downstream E2E；
- 實作RCA state machine、Knowledge workflow、automatic remediation或external adapters；
- 以fake correlation fields表達workflow audit；
- 提供raw public Store write、force transition、reopen、delete、reset或automatic repair。

## 1.3 Cross-SPEC boundaries

| Domain | Authority | SPEC-009 boundary |
|---|---|---|
| PRD-002／EventStore | Immutable Event evidence | 不修改Event，不用`Event.status`承載Incident lifecycle。 |
| SPEC-006 | Correlation Decision與candidate semantics | 不重新evaluate或選candidate。 |
| SPEC-007 | Correlation execution state | workflow operation不重用`CorrelationMutationIntent`。 |
| SPEC-008 | Incident current state、correlation mutation、Event ownership、transaction | SPEC-009在此authority內additive extension。 |
| SPEC-010 | ShadowRecord與Shadow ownership | Shadow不進Incident workflow。 |
| SPEC-011 | Runtime trigger、orchestration與full integration | 決定何時自動呼叫Assignment等capability。 |

---

# 2. D1～D12 Engineering Decisions

| ID | 正式決策 |
|---|---|
| D1 | Lifecycle、Assignment、Resolution Evidence及Human Workflow擴充既有Incident Manager／Store；不得建立第二套Incident authority。 |
| D2 | 唯一normal lifecycle為`OPEN → ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED`；禁止skip、automatic backward及reopen，CLOSED terminal。 |
| D3 | Incident先由SPEC-008 durable建立OPEN，再由caller觸發Automatic Assignment；成功才推進durable cursor，失敗保留OPEN、assignment fields及cursor。 |
| D4 | Manual assignment／reassignment必須auditable；manual operation不影響automatic cursor，reassignment只改ownership、不重置progress。 |
| D5 | 每次workflow mutation使用獨立durable `workflow_operation_id`；equivalent replay回原result，contradiction Fail Closed，不重用Correlation Intent。 |
| D6 | `IN_PROGRESS → AWAITING_REVIEW`必須在同一local transaction append合法Resolution Evidence revision；不得silent overwrite。 |
| D7 | SOP deviation必須structured、traceable、durable、auditable；Knowledge／KB／RAG／policy／model更新不屬本SPEC且knowledge gap不阻擋closure。 |
| D8 | `AWAITING_REVIEW → CLOSED`要求同一current reviewer在同一Review Attempt提供Approval與Recovery Verification；RCA completion不是hard gate。 |
| D9 | 任一review gate失敗時保持AWAITING_REVIEW且`closed_at=null`；review前或failed review後皆可append新revision，不backward。 |
| D10 | CLOSED terminal且只由formal closure設定`closed_at`；recurrence走normal Event／Correlation flow建立New Incident，不reopen。 |
| D11 | Correlation與workflow mutation共用Incident local write serialization、fresh-read及atomic transaction discipline，禁止stale whole-record overwrite。 |
| D12 | 不實作Correlation State、Shadow review、RCA state machine、Runtime orchestration、external adapters、Knowledge workflow或full platform E2E。 |

## 2.1 Revision Round 1 refinements

| ID | Refinement |
|---|---|
| R1 — Assignment orchestration boundary | SPEC-009擁有Assignment semantics；SPEC-011或controlled caller擁有future invocation timing。SPEC-008 CREATE_NEW不得內建assignment。 |
| R2 — General Resolution resubmission | AWAITING_REVIEW可在review前或failed review後append新revision；不要求failed review先存在。 |
| R3 — Same-attempt Review + Recovery | Closure的Approval與Recovery Verification必須來自same Review Attempt與same current reviewer，不得跨attempt組合。 |
| R4 — Review／Resubmission concurrency | Review revision N與submission N+1共用local serialization；stale review不得close，closure先完成則later submission拒絕。 |
| R5 — Assignment Policy identity | Automatic assignment保存exact `policy_id + policy_version`、selected assignee及default reviewer；replay不得用latest roster重新解讀。 |

---

# 3. Domain Model

## 3.1 Existing IncidentRecord authority

SPEC-009沿用SPEC-008 `IncidentRecord`：

```text
incident_id
event_ids
anchor_event_id
status
severity
created_at
updated_at
last_correlated_at
closed_at
assignee
reviewer
correlation_context
audit_trail
rca_status
rca_ref
external_refs
```

SPEC-009不得duplicate Incident、Event、correlation context或RCA artifact。Workflow mutation除明確擁有的`status`、`assignee`、`reviewer`、`updated_at`及`closed_at`外，必須保留其他authoritative current state。

## 3.2 Lifecycle closed set

```text
OPEN
ASSIGNED
IN_PROGRESS
AWAITING_REVIEW
CLOSED
```

沿用SPEC-008 `IncidentStatus`，不建立第二套status model。

## 3.3 AssignmentPolicyConfig

```text
policy_id
policy_version
engineers
default_reviewer
```

PoC baseline：

```text
policy_id = POC-ROUND-ROBIN
policy_version = 1.0
engineers = [Engineer A, Engineer B, Engineer C]
default_reviewer = Supervisor
```

Rules：

- policy ID／version non-empty；
- engineers non-empty、ordered、unique，每個reference non-empty；
- list order具有Round Robin semantic meaning；
- default reviewer non-empty；
- invalid bootstrap config Fail Fast；
- v1不支援hot reload；
- 此type屬Assignment domain，不是SPEC-006 `CorrelationPolicy`。

## 3.4 Actor rules

SPEC-009保存actor references，不實作Enterprise RBAC、SSO或authentication。Actor reference必須non-empty、traceable、auditable；exact identifier format為Implementation Choice。

```text
START_WORK actor        = current assignee
SUBMIT_RESOLUTION actor = current assignee
REVIEW actor            = current reviewer
```

Automatic／manual assignment caller視為trusted application boundary；完整authorization在本SPEC外。

## 3.5 SopFollowed

```text
YES
NO
NOT_APPLICABLE
```

不得以free-form string代替closed semantics。

---

# 4. Strict Lifecycle State Machine

## 4.1 Normal path

```text
OPEN → ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED
```

禁止arbitrary skip、automatic backward及CLOSED reopen。Reassignment不構成status transition。

## 4.2 Transition Matrix

| Current | Operation | Result | Allowed |
|---|---|---|---|
| OPEN | Auto Assign | ASSIGNED | YES |
| OPEN | Manual Assign | ASSIGNED | YES |
| OPEN | Start Work | — | NO |
| OPEN | Submit Resolution | — | NO |
| ASSIGNED | Reassign | ASSIGNED | YES |
| ASSIGNED | Start Work | IN_PROGRESS | YES |
| ASSIGNED | Submit Resolution | — | NO |
| IN_PROGRESS | Reassign | IN_PROGRESS | YES |
| IN_PROGRESS | Submit Resolution Revision 1 | AWAITING_REVIEW | YES |
| AWAITING_REVIEW | Submit New Resolution Revision | AWAITING_REVIEW | YES |
| AWAITING_REVIEW | Review latest revision, Gate fails | AWAITING_REVIEW | YES |
| AWAITING_REVIEW | Review stale revision | — | NO |
| AWAITING_REVIEW | Review latest revision, both Gates true | CLOSED | YES |
| AWAITING_REVIEW | Reassign Assignee | — | NO |
| CLOSED | Resolution Resubmission | — | NO |
| CLOSED | Review | — | NO |
| CLOSED | Any lifecycle mutation | — | NO |

Unlisted lifecycle transition一律Fail Closed，包括：

```text
OPEN → IN_PROGRESS
OPEN → CLOSED
ASSIGNED → AWAITING_REVIEW
IN_PROGRESS → CLOSED
AWAITING_REVIEW → IN_PROGRESS
CLOSED → any non-CLOSED
```

---

# 5. Assignment & Reviewer Binding

## 5.1 Automatic Assignment

```text
SPEC-008 CREATE_NEW
→ Incident durable OPEN
→ SPEC-011 Runtime or controlled caller invokes SPEC-009
→ automatic assignment
→ ASSIGNED
```

Round Robin observable order：

```text
Engineer A → Engineer B → Engineer C → Engineer A
```

同一local transaction必須fresh-read OPEN Incident與durable cursor、resolve exact policy、設定assignee、bind default reviewer、transition ASSIGNED、更新`updated_at`、推進cursor、保存receipt及audit。

只有successful commit推進cursor。任何failure all rollback：

```text
Incident remains OPEN
assignee unchanged
reviewer unchanged
cursor unchanged
```

## 5.2 Manual Assignment

```text
OPEN + manual assignment
→ ASSIGNED
→ assignee = requested engineer
→ reviewer = default Supervisor
```

Manual assignment不得advance、consume或reset automatic cursor，也不得fake automatic Assignment Policy reference。

## 5.3 Reassignment

```text
ASSIGNED + reassignment    → ASSIGNED
IN_PROGRESS + reassignment → IN_PROGRESS
```

Reassignment只改ownership，不重置progress、不推進cursor，且必須durable、idempotent、auditable。AWAITING_REVIEW與CLOSED皆拒絕assignee reassignment。

## 5.4 Reviewer binding

Successful initial automatic或manual assignment在same operation／transaction設定：

```text
reviewer = AssignmentPolicyConfig.default_reviewer
```

PoC v1 `Reviewer reassignment = NOT SUPPORTED`。不得增加`ASSIGN_REVIEWER` action；future change需contract revision。

---

# 6. Workflow Operation Identity & Receipt

## 6.1 Request identity

每個authoritative workflow request包含：

```text
workflow_operation_id
incident_id
actor
authoritative now
workflow_action
action-specific payload
```

Workflow與correlation operation是separate typed namespaces，不得以SPEC-007 `CorrelationMutationIntent`承載workflow。

`immutable_workflow_identity`只包含caller-supplied semantic request identity；authoritative `now`不參與replay equivalence。Generated IDs、allocated revision、selected assignee、resulting status與execution timestamps屬durable result／provenance，不是caller request identity。所有payload比較必須使用canonical normalized semantics，不得依object identity、map iteration order或非semantic serialization差異判斷conflict。

## 6.2 Replay semantics

```text
same workflow_operation_id
+ equivalent immutable workflow identity
→ return original durable result
```

Equivalent replay不得再執行mutation、推進cursor、新增Resolution／Review／audit或改寫timestamps。Automatic replay不得用latest roster/config重新解讀historical result。

Completed receipt lookup與semantic equivalence validation必須先於fresh-operation state／time validation。若receipt已存在且identity equivalent，直接回傳original durable result；retry-call `now`或current policy/config change不得使已完成operation失效。

```text
same workflow_operation_id
+ contradictory identity
→ WORKFLOW_RECEIPT_CONFLICT / REPAIR_REQUIRED
→ Fail Closed
```

不得只判operation ID存在就blind success。

## 6.3 Workflow receipt role

不得重用SPEC-008 correlation receipt contract。使用additive logical role equivalent to：

```text
incident_workflow_operation_receipts
```

Minimum conceptual fields：

```text
workflow_operation_id
incident_id
workflow_action
completed_at
immutable_workflow_identity
resulting_status
result_reference
```

Automatic assignment receipt額外保存：

```text
assignment_policy_id
assignment_policy_version
selected_assignee
default_reviewer
```

Manual assignment／reassignment不得偽造automatic policy reference。Exact physical encoding是Implementation Choice。

## 6.4 Per-action minimum semantic identity

所有action必須使用以下minimum canonical semantic projection，不得自行建立不相容的equivalence rule。

### `AUTO_ASSIGN`

Caller request identity：

```text
workflow_action = AUTO_ASSIGN
incident_id
actor
```

`assignment_policy_id`、`assignment_policy_version`、`selected_assignee`、`bound_reviewer`及cursor before／after（或semantic equivalent）是durable resolved result／provenance，不是caller request identity。Completed replay必須先回original result，不得先resolve current policy或以new roster重新assign。

### `MANUAL_ASSIGN`

Caller request identity：

```text
workflow_action = MANUAL_ASSIGN
incident_id
actor
target_assignee
```

`bound_reviewer`、resulting status與timestamps是generated result。若default reviewer由current `AssignmentPolicyConfig` resolve，receipt/result必須保存exact `assignment_policy_id`、`assignment_policy_version`及`bound_reviewer`作configuration provenance，並明示`selection_mode=MANUAL`；target assignee不是Round Robin選出，cursor不得變動。

### `REASSIGN`

Caller request identity：

```text
workflow_action = REASSIGN
incident_id
actor
target_assignee
```

Current assignee是transaction-time state precondition，不是caller-controlled identity；若request明確提供expected-owner guard，該guard必須納入identity。PoC v1 reassignment不rebind reviewer、不改cursor。

### `START_WORK`

Caller request identity：

```text
workflow_action = START_WORK
incident_id
actor
```

Expected current status與assignee是transaction-time preconditions。

### `SUBMIT_RESOLUTION`

Caller request identity：

```text
workflow_action = SUBMIT_RESOLUTION
incident_id
actor
actual_action
resolution_note
sop_followed
additional_note
deviation_reason
```

`resolution_submission_id`、allocated revision、`submitted_at`與resulting status是generated result，不參與request equivalence；equivalent replay不得allocate新revision。

### `REVIEW_ATTEMPT`

Caller request identity：

```text
workflow_action = REVIEW_ATTEMPT
incident_id
actor
target_resolution_submission_id or exact target resolution revision
review_approved
review_note
recovery_verified
recovery_note
```

Exact target Resolution revision是semantic identity。`review_attempt_id`、`reviewed_at`、resulting status及nullable `closed_at`是generated result。`CLOSE`不是獨立operation，因此不存在independent CLOSE identity或receipt。

---

# 7. Resolution Evidence

## 7.1 ResolutionSubmission

```text
resolution_submission_id
incident_id
revision
actual_action
resolution_note
sop_followed
additional_note
deviation_reason
submitted_by
submitted_at
```

Rules：

- `actual_action`、`resolution_note` required／non-empty；
- `sop_followed` required且屬closed set；
- `additional_note` nullable；
- `sop_followed=NO`時`deviation_reason` required／non-empty；其他值時nullable；
- `submitted_by=current assignee`；
- `submitted_at`使用authoritative time。

## 7.2 Append-only revisions

每個Incident的revision必須monotonic、unique、concurrency-safe、never reused：

```text
1, 2, 3, ...
```

不得update/delete舊submission模擬resubmission。`latest applicable Resolution revision`是最高valid committed revision；closure Review必須reference它。

## 7.3 Initial submission

```text
IN_PROGRESS
→ append revision 1
→ AWAITING_REVIEW
```

Evidence、status transition、`updated_at`、receipt及audit必須在同一local transaction all-or-nothing。

## 7.4 General resubmission

```text
AWAITING_REVIEW + Revision N+1
→ append new revision
→ remain AWAITING_REVIEW
```

Resubmission可發生於review前或failed review後，不要求failed review先存在。舊Resolution與Review Attempts保持historical，但不能authorize新revision closure。

## 7.5 SOP deviation handoff

SOP deviation是future Knowledge Improvement Candidate workflow的structured handoff evidence。SPEC-009不建立KnowledgeCandidateStore、KB／RAG ingestion、SOP update、Detector／Correlation Policy update或model retraining。Knowledge gap不得阻擋合法closure。

---

# 8. Review Attempt、Recovery Verification & Closure

## 8.1 ReviewAttempt

```text
review_attempt_id
incident_id
resolution_revision
reviewer
review_approved
review_note
recovery_verified
recovery_note
reviewed_at
```

若保存`recovery_verified_by`，PoC v1必須滿足：

```text
recovery_verified_by == current reviewer
```

Review Attempt是append-only historical evidence。

## 8.2 Same-attempt closure

一筆Review Attempt必須同時滿足：

```text
incident.status == AWAITING_REVIEW
resolution_revision == latest applicable Resolution revision
reviewer == current Incident reviewer
review_approved == true
recovery_verified == true
```

不得跨attempt組合gates：

```text
Attempt 1: approved=true, recovery=false
Attempt 2: approved=false, recovery=true
→ CLOSED forbidden
```

## 8.3 Failed review

任一gate為false：

```text
status remains AWAITING_REVIEW
closed_at remains null
```

這是合法且successful的authoritative workflow mutation，不是exception。Review Attempt、one workflow receipt及one primary `REVIEW_ATTEMPT` audit必須durable append，`updated_at=authoritative now`；不得backward至IN_PROGRESS。`review_approved=false`或`recovery_verified=false`本身不得映射為error。

## 8.4 Successful closure

`CLOSE`不是externally callable workflow operation。唯一review mutation是`REVIEW_ATTEMPT`；當same-attempt closure gates全部成立時，此operation在同一local transaction：

```text
append valid Review Attempt
status = CLOSED
closed_at = authoritative now
updated_at = authoritative now
persist one REVIEW_ATTEMPT workflow receipt
append one primary REVIEW_ATTEMPT workflow audit
commit
```

該primary audit的structured effects可包含`REVIEW_RECORDED`、`RECOVERY_VERIFIED`、`STATUS_CHANGED`及`INCIDENT_CLOSED`。不得append第二筆primary CLOSE audit或建立CLOSE receipt。Any failure必須rollback全部。

## 8.5 RCA is not closure gate

SPEC-009可preserve/read `rca_status`／`rca_ref`，但`rca_status=COMPLETED`不是hard gate。RCA failed但human remediation、Resolution、Review Approval與Recovery Verification皆合法時可CLOSED。不得生成RCA、修改RCA state machine或overwrite RCA refs。

---

# 9. Review vs Resolution Resubmission Concurrency

```text
Incident = AWAITING_REVIEW
latest Resolution = N
Reviewer reviews N
vs
Engineer submits N+1
```

兩者必須共用Incident local write serialization及transaction內fresh-read。

Case 1：N+1先commit：

```text
N+1 becomes latest
→ Review N stale
→ STALE_RESOLUTION_REVISION / NON_RETRYABLE
→ must not close
```

Caller refresh state後以新operation review。

Case 2：Review N合法closure先commit：

```text
Incident CLOSED
→ later submission fresh-reads CLOSED
→ reject
```

禁止Reviewer以transaction外stale N在N+1 commit後close。

---

# 10. Atomicity & Concurrency

## 10.1 Shared write discipline

```text
BEGIN IMMEDIATE
→ fresh-read current state
→ validate receipt and business preconditions
→ apply only owned mutation
→ persist evidence / receipt / audit
→ commit
```

`BEGIN IMMEDIATE`是current PoC reality；normative requirement是同一Incident authority內的serializable observable outcome、fresh-read validation與local atomicity。

## 10.2 Correlation vs lifecycle race

`ATTACH_EXISTING`與`SUBMIT_RESOLUTION`只允許：

```text
attach first → submission sees enrichment → AWAITING_REVIEW
```

或：

```text
submission first → AWAITING_REVIEW → later attach rejected
```

禁止AWAITING_REVIEW後由stale correlation write silent attach。

## 10.3 Field preservation

Workflow mutation不得以stale whole-record覆寫：

```text
event_ids
anchor_event_id
severity
last_correlated_at
correlation_context
rca_status
rca_ref
external_refs
```

每次mutation須在transaction內fresh-read並只改workflow-owned fields。Event ownership與correlation audit不得重建、刪除或重排。

## 10.4 Local concurrency

Concurrent automatic assignment使每個successful operation取得唯一cursor position；同一Incident最多一個initial assignment，loser不consume cursor。Concurrent Resolution submissions必須取得unique monotonic revisions或typed Fail Closed，不得lost update或reuse revision。

---

# 11. Workflow Audit & Unified Timeline

## 11.1 Workflow actions

Minimum closed set：

```text
AUTO_ASSIGN
MANUAL_ASSIGN
REASSIGN
START_WORK
SUBMIT_RESOLUTION
REVIEW_ATTEMPT
```

Revision >1仍是`SUBMIT_RESOLUTION`，不新增`RESUBMIT_RESOLUTION`。Reviewer binding是Assignment effect，不是`ASSIGN_REVIEWER` action。

Successful `REVIEW_ATTEMPT`可將closure作為同一operation的atomic effect；`CLOSE`不是normal workflow action、independent request、public command或receipt identity。Failed legal Review Attempt仍使用`REVIEW_ATTEMPT` action並保存negative gate facts。

## 11.2 Workflow audit role

Existing SPEC-008 correlation audit要求Event／Policy／Reason，不能承載workflow action。不得填fake correlation fields。

新增additive logical role，prefer physically separate equivalent：

```text
incident_workflow_audit
```

```text
workflow_audit_id
workflow_operation_id
incident_id
actor
action
occurred_at
old_status
new_status
effects
reference_id
```

Effects至少可typed表達：

```text
ASSIGNEE_SET
REVIEWER_BOUND
STATUS_CHANGED
RESOLUTION_SUBMITTED
REVIEW_RECORDED
RECOVERY_VERIFIED
INCIDENT_CLOSED
```

Successful first authoritative mutationappend exactly one primary workflow audit。Failure與replay不得產生success duplicate。Audit不duplicate完整Resolution payload。

`AUTO_ASSIGN` audit必須保留exact `assignment_policy_id + assignment_policy_version`，可使用typed fields或不可歧義的receipt reference；不得只記錄latest policy，也不得複製完整configuration。Manual assignment與reassignment不得填入fake automatic policy identity。

## 11.3 Unified Incident Timeline

```text
existing correlation audit + workflow audit
→ Unified Incident Timeline
```

Timeline必須typed、deterministic、source-domain-aware。Physical tables可分開，不得為單一physical table重建既有correlation audit。Malformed entry不得silent skip。

Logical Unified Incident Timeline使用以下cross-source total-order key：

```text
(
  occurred_at_utc,
  source_domain_rank,
  source_local_order
)

source_domain_rank:
CORRELATION = 0
WORKFLOW    = 1
```

`source_local_order`是該source內durable stable ordering key，例如persisted audit sequence／ID。相同canonical timestamp時，Correlation entry排在Workflow entry之前；同domain再依durable local key排序。Ordering必須跨restart、repeated reads保持一致，且不得依database query return order。

此tie-break只定義deterministic presentation／read ordering；當timestamps完全相同時，不宣稱physical commit causality。Exact pagination及key encoding仍為Implementation Choice。

---

# 12. Persistence & Schema Upgrade

## 12.1 Same Incident authority

SPEC-009使用既有SPEC-008 Incident Store及default `incident_store.db`；不得建立`lifecycle_store.db`或第二Incident DB。

Expected additive logical roles：

```text
incident_workflow_operation_receipts
incident_assignment_state
incident_resolution_submissions
incident_review_attempts
incident_workflow_audit
```

Exact tables／indexes可依repository style調整，但logical responsibility、uniqueness、integrity及atomicity不得弱化。

## 12.2 Controlled additive migration

Migration必須保留existing Incidents、Event ownership、correlation receipts/audit、timestamps、RCA state/refs及external refs。成功後才transactionally更新schema metadata。

Unsupported future version、partial schema、migration contradiction或failure皆Fail Closed。不得drop DB、delete `incident_store.db`、reset Incidents、validator cleanup或destructive rebuild。

## 12.3 Atomic units

| Operation | Same local transaction |
|---|---|
| Auto Assignment | Incident assignment/status + cursor + receipt + audit |
| Manual／Reassign | Incident assignment/status + receipt + audit |
| Start Work | status/time + receipt + audit |
| Initial Resolution | submission + transition + receipt + audit |
| Resubmission | revision + updated time + receipt + audit |
| Failed Review | Review Attempt + unchanged status + time + receipt + audit |
| Successful Review Attempt with closure effect | Review Attempt + CLOSED/closed_at + one REVIEW_ATTEMPT receipt + one primary REVIEW_ATTEMPT audit |

---

# 13. Time Semantics

所有workflow mutation由caller提供timezone-aware absolute `now`；hidden wall clock不是domain authority。

```text
successful first mutation → updated_at = authoritative now
formal closure            → closed_at = authoritative now
same-operation replay     → original timestamps unchanged
```

`submitted_at`、`reviewed_at`、receipt/audit times及`closed_at`須保留absolute、timezone-unambiguous、safe-comparison與SPEC-008 canonical UTC semantics。

Fresh mutation的`now`不得早於current `updated_at`。Replay驗證identity後回original result，不以retry-call `now`重判已成功operation或覆寫original time。

Evaluation order是observable contract：

1. 在local write transaction內先以`workflow_operation_id`查詢completed receipt。
2. Receipt存在時驗證semantic equivalence；equivalent即回original result，contradiction回`WORKFLOW_RECEIPT_CONFLICT`。
3. 只有沒有completed receipt的新operation才fresh-read Incident並驗證`now >= current updated_at`。
4. 若fresh `now < current updated_at`，回`WORKFLOW_TIME_REGRESSION / NON_RETRYABLE`，不得commit Incident、evidence、receipt、audit或cursor mutation。

Caller可refresh authoritative state／time並在適用時使用新的workflow operation重新提交。

---

# 14. CLOSED Integrity & Recurrence

```text
CLOSED     → closed_at != null
non-CLOSED → closed_at == null
closed_at >= created_at
updated_at >= closed_at
```

Closure same transaction設定`status=CLOSED`與`closed_at=updated_at=authoritative now`。CLOSED後assignment、reassignment、start work、Resolution submission、review mutation及reopen全部禁止。

Recurrence走normal upstream flow：

```text
old Incident CLOSED + new failure
→ Event / Correlation
→ New Incident if upstream decides
```

SPEC-009不決定recurrence correlation或建立recurrence Incident。

---

# 15. Read Capabilities

Semantic capabilities至少equivalent to：

```text
get current Incident
get current assignment/status
get latest Resolution Evidence
list Resolution Evidence revisions
list Review Attempts
get workflow operation result
get Unified Incident Timeline
query/enumerate Incidents by workflow state where needed
```

Exact signatures、DTO及pagination是Implementation Choice。Future adapters只透過public APIs，不直接讀private SQLite tables。Reads須point-in-time coherent；malformed state、unsupported version或contradiction不得成為Not Found、silent skip或automatic repair。不得定義Jira／Discord／Dashboard-specific DTO。

## 15.1 IncidentCorrelationView lifecycle propagation

Workflow status mutation更新的是Incident domain同一筆authoritative durable current state。Commit後，SPEC-008 `IncidentCorrelationView.status`必須從該相同status投影，不得建立separate synchronization、第二status field或duplicated lifecycle authority。

```text
OPEN / ASSIGNED / IN_PROGRESS
→ correlation-open

AWAITING_REVIEW / CLOSED
→ correlation-closed
```

`IN_PROGRESS → AWAITING_REVIEW` commit後，subsequent authoritative View必須顯示`status=AWAITING_REVIEW`且該Incident不再是normal correlation candidate。`AWAITING_REVIEW → CLOSED` commit後，View必須顯示`status=CLOSED`並維持correlation-ineligible。

Lifecycle status不得copy至SPEC-007或建立lifecycle synchronization state。SPEC-006／008持續消費及投影existing authoritative Incident state；此clarification不reopen SPEC-008。

---

# 16. Typed Error Contract

## 16.1 Vocabulary and closed mapping

| Error | Disposition |
|---|---|
| `INVALID_WORKFLOW_MUTATION` | `NON_RETRYABLE` |
| `INCIDENT_NOT_FOUND` | `NON_RETRYABLE` |
| `INVALID_LIFECYCLE_TRANSITION` | `NON_RETRYABLE` |
| `INVALID_ASSIGNMENT_TARGET` | `NON_RETRYABLE` |
| `ASSIGNMENT_NOT_ALLOWED` | `NON_RETRYABLE` |
| `WORKFLOW_ACTOR_MISMATCH` | `NON_RETRYABLE` |
| `INVALID_RESOLUTION_EVIDENCE` | `NON_RETRYABLE` |
| `INVALID_REVIEW_ATTEMPT` | `NON_RETRYABLE` |
| `STALE_RESOLUTION_REVISION` | `NON_RETRYABLE` |
| `CLOSED_INCIDENT_MUTATION_FORBIDDEN` | `NON_RETRYABLE` |
| `WORKFLOW_TIME_REGRESSION` | `NON_RETRYABLE` |
| `RESOLUTION_REVISION_CONFLICT` | `REPAIR_REQUIRED` |
| `WORKFLOW_RECEIPT_CONFLICT` | `REPAIR_REQUIRED` |
| `MALFORMED_WORKFLOW_STATE` | `REPAIR_REQUIRED` |
| `UNSUPPORTED_INCIDENT_STORE_VERSION` | `REPAIR_REQUIRED` |
| `INCIDENT_WORKFLOW_INTEGRITY_FAILURE` | `REPAIR_REQUIRED` |
| `TRANSIENT_INCIDENT_STORE_FAILURE` | `RETRYABLE` |

Exact Python hierarchy為Implementation Choice，但code/disposition須machine-readable且deterministic。Integrity contradiction不得降級為business rejection；僅narrow transient persistence failure可RETRYABLE。

`WORKFLOW_TIME_REGRESSION`表示fresh workflow mutation提供的authoritative `now`早於write transaction內fresh-read所得Incident `updated_at`。Valid negative Review／Recovery facts是成功保存的business decision，不是error；`INVALID_REVIEW_ATTEMPT`只用於真正的request／contract violation。

## 16.2 Integrity behavior

- malformed workflow state不得視為Not Found；
- duplicate/non-monotonic revisions、conflicting receipts及impossible lifecycle/evidence combinations Fail Closed；
- unreadable/unclassifiable Incident authority readiness Fail Fast；
- 禁止silent repair、last-write-wins、guess-and-rewrite或automatic destructive recovery。

---

# 17. Conceptual IncidentManager Capabilities

Exact method names為Implementation Choice，但可提供semantic equivalents：

```text
IncidentManager.assign_incident(...)
IncidentManager.reassign_incident(...)
IncidentManager.start_work(...)
IncidentManager.submit_resolution(...)
IncidentManager.review_incident(...)
```

每個request包含operation ID、Incident ID、actor、authoritative `now`及action payload。IncidentManager保持domain validation authority；Store write primitives保持private/internal。

Public API不得提供force assignment、force transition、raw evidence insert、review bypass、reopen、delete、reset、repair或stale-state overwrite。

不得提供`IncidentManager.close_incident(...)`或semantic-equivalent independent CLOSE command；closure只能是successful `REVIEW_ATTEMPT`的atomic effect。

---

# 18. Shadow、RCA & External Boundaries

## 18.1 Shadow

```text
ShadowRecord ≠ Incident
Event → ROUTE_SHADOW → ShadowRecord
```

Shadow不進Assignment、IN_PROGRESS、Resolution Evidence、repair workflow、AWAITING_REVIEW或CLOSED。SPEC-010 `review_status=UNREVIEWED`不授權SPEC-009實作`UNREVIEWED → REVIEWED`。

## 18.2 RCA

SPEC-009可preserve/read `rca_status`及`rca_ref`，但不生成RCA、不改RCA state machine、不要求RCA completion才close、不silent overwrite refs。

## 18.3 External interfaces

Future Jira、Discord、Dashboard、Email可作view、human interface或intent source，但不是Incident authority。Adapter success/failure不得fabricate Incident state；adapter implementation不屬本SPEC。

---

# 19. Retention & Cleanup

```text
CLOSED != deleted
```

Incident、Resolution revisions、Review Attempts、workflow receipts與audit不得因closure、review completion、age或restart自動刪除；PoC v1無automatic TTL。

Normal Runtime不得提供destructive cleanup。Reset／cleanup僅限PM明確授權的controlled dev/test/migration並具strong guard。AI coding agent或validator不得刪除authority state以使流程通過。

---

# 20. Acceptance Criteria

## AC-009-A — Authority / Schema Upgrade

- Same existing Incident authority；無第二Store/DB。
- Additive upgrade保留Incidents、Event ownership、correlation receipts/audit、timestamps、RCA及external refs。
- Migration metadata transactional；partial、unsupported或failed migration Fail Closed。

## AC-009-B — Strict Lifecycle

- 驗證完整normal path及所有skip、backward、reopen、unlisted transition。
- Reassignment保持ASSIGNED或IN_PROGRESS，不重置progress。

## AC-009-C — Automatic Assignment

- 驗證A→B→C→A、restart continuity及concurrency。
- Successful assignment才推進cursor；failure不改Incident/assignment/cursor。
- Manual assignment不影響cursor。
- 只在durable OPEN後assignment；SPEC-011擁有future invocation timing。
- 保存exact Assignment Policy identity、selected assignee及reviewer；replay不用latest config。

## AC-009-D — Manual Assignment / Reassignment

- OPEN manual assignment成ASSIGNED並bind default reviewer。
- ASSIGNED/IN_PROGRESS reassignment保持status。
- AWAITING_REVIEW/CLOSED拒絕；不消耗cursor；無fake policy reference。
- v1無reviewer reassignment。
- `START_WORK actor != current assignee`回`WORKFLOW_ACTOR_MISMATCH`且不commit mutation。

## AC-009-E — Workflow Idempotency

- Equivalent replay至少`×1`、`×10`、`×100`。
- 無duplicate assignment、cursor advance、Resolution revision、Review Attempt、audit或timestamp mutation。
- Contradictory replay Fail Closed。
- 對`AUTO_ASSIGN`、`MANUAL_ASSIGN`、`REASSIGN`、`START_WORK`、`SUBMIT_RESOLUTION`及`REVIEW_ATTEMPT`逐一驗證§6.4 minimum semantic projection；相同projection回original result，任一semantic field改變回`WORKFLOW_RECEIPT_CONFLICT / REPAIR_REQUIRED`。
- Completed equivalent replay必須在fresh time validation前回original result並保存timestamps，即使retry-call `now`或current Assignment Policy已改變。
- Fresh operation的`now < Incident.updated_at`回`WORKFLOW_TIME_REGRESSION / NON_RETRYABLE`且不commit任何Incident、cursor、evidence、receipt或audit mutation。
- 不存在separate CLOSE replay identity、receipt或operation。

## AC-009-F — Resolution Evidence

- 驗證required fields、SopFollowed及deviation reason。
- Revisions append-only、monotonic、unique、concurrency-safe、never reused。
- Initial submission/transition atomic。
- review前及failed review後resubmission皆合法；latest revision deterministic。
- `SUBMIT_RESOLUTION actor != current assignee`回`WORKFLOW_ACTOR_MISMATCH`且不commitrevision或status mutation。
- `sop_followed=NO`且具有valid non-empty `deviation_reason`的submission仍可通過normal Review／Recovery gates；Knowledge Candidate workflow不存在不得阻擋closure。
- 不產生automatic KB、RAG、SOP、Detector、Correlation Policy或model mutation。

## AC-009-G — Review / Recovery Gate

- Approval與Recovery是separate facts、same reviewer、same Attempt。
- 禁止cross-attempt combination；stale revision不能close。
- Both true才CLOSED；任一false保持AWAITING_REVIEW/closed_at null。
- RCA completion不是gate。
- `REVIEW_ATTEMPT actor != current reviewer`回`WORKFLOW_ACTOR_MISMATCH`且不commit Review Attempt或closure。
- Successful closure由one `REVIEW_ATTEMPT` request產生one receipt、one Review Attempt record、one primary `REVIEW_ATTEMPT` audit及optional atomic CLOSED effect；不得有CLOSE receipt或第二筆CLOSE primary audit。
- Valid negative Review／Recovery result同樣產生one receipt、one Review Attempt、one primary audit，更新`updated_at`並保持AWAITING_REVIEW／`closed_at=null`，不得當成error。

## AC-009-H — Resubmission / No Backward

- AWAITING_REVIEW append新revision後status不變。
- 不要求failed review；舊evidence/reviews保留但不能authorize new revision。
- 不發生AWAITING_REVIEW→IN_PROGRESS。

## AC-009-I — CLOSED / Recurrence

- Closure atomic設定status、closed_at、updated_at。
- CLOSED後所有workflow mutation/reopen拒絕；non-CLOSED closed_at null。
- Recurrence creation留在upstream flow。

## AC-009-J — Audit / Unified Timeline

- Successful first mutationexactly one workflow audit；failure/replay不duplicate。
- 無fake correlation fields或full evidence duplication。
- Timeline deterministic/source-aware；malformed entry不silent skip。
- 相同`occurred_at_utc`的cross-source entries依`source_domain_rank`排序，其中`CORRELATION=0`、`WORKFLOW=1`；同source再依durable `source_local_order`。結果須跨restart/repeated reads一致且不依DB return order。
- Timeline tie-break只表示presentation/read order，不宣稱equal timestamp時的physical commit causality。

## AC-009-K — Concurrency

- 驗證ATTACH_EXISTING vs SUBMIT_RESOLUTION兩種合法serialization。
- 無AWAITING_REVIEW後late attach。
- 驗證Review N vs Submit N+1，無stale closure。
- Cursor/revision allocation無lost update；workflow不覆蓋correlation-owned fields。
- `IN_PROGRESS → AWAITING_REVIEW` commit後，authoritative `IncidentCorrelationView.status=AWAITING_REVIEW`且立即correlation-closed；CLOSED View持續correlation-ineligible。
- Lifecycle state只存在Incident authority，不duplicate至SPEC-007或任何synchronization state。

## AC-009-L — Regression / Scope

- SPEC-009 targeted及SPEC-006/007/008/010 relevant regression PASS。
- Full repository regression `0 failed`。
- 不為滿足本SPEC實作SPEC-011、Shadow review、RCA、Knowledge workflow、automatic remediation或adapters。
- Coverage是contract；固定total test count不是contract。

---

# 21. Required Test Layers

1. Domain contracts／closed enum tests。
2. Lifecycle transition matrix tests。
3. Round Robin、cursor、restart、concurrency及policy identity tests。
4. Manual assignment／reassignment／reviewer binding tests。
5. Workflow receipt replay／contradiction tests。
6. Resolution validation／revision／resubmission／SOP deviation tests。
7. Same-attempt Review／Recovery及RCA-independent closure tests。
8. Review/resubmission與correlation/lifecycle concurrency tests。
9. SQLite additive migration／reopen／crash rollback tests。
10. Workflow audit／Unified Timeline tests。
11. Malformed/version/Fail Closed integrity tests。
12. Real SPEC-008 integration與SPEC-006/007/010 relevant regression。
13. Full repository regression。

Full downstream Docker Runtime E2E defer至SPEC-011，不是SPEC-009 blocking implementation gate。

---

# 22. Implementation Phases

| Phase | Scope |
|---|---|
| Phase 1 — Domain Contracts | Requests/results、Assignment Policy、Resolution、Review及errors。 |
| Phase 2 — Persistence / Additive Schema Upgrade | Controlled migration、receipts、assignment state、evidence、review及audit。 |
| Phase 3 — Assignment / Start Work | Automatic/manual assignment、reassignment、reviewer binding、cursor、START_WORK。 |
| Phase 4 — Resolution / Review / Closure | Revisions、resubmission、same-attempt gates、closure及replay。 |
| Phase 5 — Concurrency / Integrity / Timeline | Races、fresh-read protection、readiness、reads及timeline。 |
| Phase 6 — Integration / Regression | SPEC-008 integration、cross-SPEC relevant suites及full regression。 |

PM implementation authorization及Implementation Work Instructions已issued；Phase 1～6、Final Full Contract Audit與PM Final Review均已PASS。SPEC-009 v1.0 implementation state為`IMPLEMENTED`。這不表示Production Ready、SPEC-011或full downstream E2E完成。

---

# 23. Implementation Reality Reconciliation（Non-normative）

2026-09-10 read-only pre-draft audit：

```text
Baseline branch                    develop
Baseline HEAD                      cac13d2d5c6eb4e29ab12a6b8c0ff04e2b2e1e30
Accepted D1–D12 conflict           NONE
Destructive migration needed       NO
SPEC-008 reopen needed             NO
```

REUSE：

```text
Incident current record
complete IncidentStatus
assignee/reviewer/closed_at columns
UTC timestamp validation
CLOSED integrity
BEGIN IMMEDIATE transaction
fresh-read pattern
IncidentCorrelationView
restart/concurrency/corruption test patterns
```

ADDITIVE EXTENSION：

```text
lifecycle mutations
assignment/reassignment
durable Round Robin state
workflow receipts
Resolution Evidence revisions
Review Attempts / Recovery Verification
workflow reads
schema migration
```

REFINE EXISTING INCIDENT DOMAIN：

```text
workflow audit
Unified Incident Timeline
shared correlation/workflow transaction discipline
protection against stale whole-record overwrite
```

此節是implementation reconciliation evidence，不是requirement authority。

---

# 24. Future / Out of Scope

- Correlation Decision、candidate matching、fingerprint或policy mutation；
- Pending、Processed、Blocked、MutationIntent或ProcessingClaim；
- Shadow persistence/review/reclassification/Shadow→Incident promotion；
- Runtime polling、scheduling、trigger sequencing或full downstream E2E；
- RCA state machine、RAG、KnowledgeCandidateStore、KB mutation、retraining；
- automatic remediation；Jira／Discord／Dashboard／Email adapters；
- Enterprise RBAC／SSO；reviewer reassignment；CLOSED reopen；
- retention/archive engine、Admin Repair Tool、destructive cleanup；
- distributed DB、cross-store 2PC、Kafka／Redis、HA workers；
- full Docker correlation/lifecycle E2E。

---

# 25. Documentation Governance

```text
PRD-003 semantic patch        NOT REQUIRED
PRD-001 semantic patch        NOT REQUIRED
SPEC-006/007 patch            NOT REQUIRED
SPEC-008 reopen               NOT REQUIRED
SPEC-010 patch                NOT REQUIRED
Architecture redesign         NOT REQUIRED
```

Implementation發現問題須分類`MUST PATCH / NOTE ONLY / DEFER`及`NO IMPACT / REFINE DOWNSTREAM / BLOCKING CONFLICT`。不得先改code semantics再讓SPEC配合。Requirement change才考慮PRD；contract conflict先停止、保存evidence並交PM。

Backward Documentation Governance：`PENDING / NON-BLOCKING`。PRD-001、DDS-001、README及architecture/status diagrams的backward status consistency，以及其他文件可能存在的舊implementation-status wording，留待另行授權的documentation reconciliation處理；本次不修改其他文件。AI coding agent不得自行destructive reset／cleanup。

---

# 26. Implementation Handoff

Implementation Owner：**夜羽**。

```text
Version 1.0
Implemented
SPEC-009 implementation completed and verified
```

## 26.1 Approval closure record

| Gate | Result |
|---|---|
| PM Review Round 1 | PASS |
| Revision Round 1 | PASS |
| PM Review Round 2 | PASS WITH REQUIRED REVISIONS |
| Revision Round 2 | PASS |
| Final PM Approval | PASS |
| Implementation Authorization | ISSUED |
| Implementation Work Instructions | ISSUED |
| Phase 1 — Workflow Domain Contracts | PASS |
| Phase 2 — Persistence / Additive Schema Migration | PASS |
| Phase 3 — Assignment / Reassignment / Start Work | PASS |
| Phase 4 — Resolution Evidence / Review / Closure | PASS |
| Phase 5 — Replay / Concurrency / Integrity / Reads / Unified Timeline | PASS |
| Phase 6 — Acceptance / Integration / Regression | PASS |
| Final Full Contract Audit | PASS |
| PM Final Review | PASS |
| Implementation State | IMPLEMENTED |

SPEC-009 v1.0是Lifecycle／Human Workflow implementation的approved Engineering Contract。Implementation必須遵守D1～D12及本文件normative contracts；文件明示為Implementation Choice的技術細節不因此被凍結。若implementation發現與PRD-003、SPEC-008或其他active authority衝突，必須停止受影響範圍、保存evidence並交由PM裁定，不得靜默改寫semantics。

## 26.2 Implementation closure state

```text
Final PM Approval:
PASS

Implementation Authorization:
ISSUED

Implementation Owner:
夜羽

Implementation Branch:
feature/spec-009-lifecycle-human-workflow

Approved Implementation Baseline:
a68e717dbe4672c55004eabf9d94d985cbaf00ac

Implementation Work Instructions:
ISSUED

Implementation Progress:
Phase 1 — Workflow Domain Contracts: PASS
Phase 2 — Persistence / Additive Schema Migration: PASS
Phase 3 — Assignment / Reassignment / Start Work: PASS
Phase 4 — Resolution Evidence / Review / Closure: PASS
Phase 5 — Replay / Concurrency / Integrity / Reads / Unified Timeline: PASS
Phase 6 — Acceptance / Integration / Regression: PASS

Final Full Contract Audit:
PASS

PM Final Review:
PASS

Implementation State:
IMPLEMENTED
```

PM implementation authorization已issued，Implementation Work Instructions已交付Owner夜羽；Phase 1～6、Final Full Contract Audit與PM Final Review均已PASS。SPEC-009 v1.0的approved Lifecycle／Human Workflow engineering contract已實作並驗證。

Current governed handoff state：

```text
SPEC-009 v1.0 Implemented
→ Phase 1～6 implementation PASS
→ Final Full Contract Audit PASS
→ PM Final Review PASS
→ IMPLEMENTED
```

本文件記錄SPEC-009 v1.0的formal implementation closure；不授權Git mutation、upstream rewrite、SPEC-011或downstream scope expansion。

---

# 27. PM Review／Approval Checklist

- [x] Metadata為SPEC-009 v1.0 `Implemented`／2026-09-10，Owner夜羽。
- [x] Authority正確引用PRD-003、PRD-001、PRD-002及SPEC-006/007/008/010。
- [x] D1～D12對齊並保持single Incident authority。
- [x] Strict lifecycle、invalid skip、no backward及no reopen完整。
- [x] Assignment failure保留OPEN、assignment fields及cursor。
- [x] Round Robin restart/concurrency-safe且只有success推進cursor。
- [x] Manual assignment/reassignment不影響automatic cursor。
- [x] Assignment Policy identity versioned並保存於automatic receipt/audit。
- [x] Assignment invocation timing留給SPEC-011/controlled caller。
- [x] Workflow idempotency與correlation operation分離。
- [x] Resolution Evidence append-only、monotonic且initial transition atomic。
- [x] Resubmission可在review前/failed review後，status不backward。
- [x] Approval與Recovery為same reviewer、same Review Attempt的separate facts。
- [x] Closure要求latest revision且禁止cross-attempt gates。
- [x] Review/Resubmission race使用shared serialization/fresh-read。
- [x] RCA completion不是closure hard gate。
- [x] Correlation audit未被fake workflow fields污染。
- [x] Workflow audit及Unified Timeline完整。
- [x] Additive migration保留SPEC-008 authority且不destructive reset。
- [x] CLOSED integrity、terminal及recurrence boundary完整。
- [x] Shadow review與`UNREVIEWED → REVIEWED`未進scope。
- [x] SPEC-011 Runtime/Docker E2E保持out of scope。
- [x] Error mapping、integrity、retention及reads完整。
- [x] AC-009-A～L及test layers完整，coverage不依固定count。
- [x] Status honesty：SPEC-009已Implemented；Phase 1～6、Final Full Contract Audit及PM Final Review均標示PASS；未宣稱SPEC-011 Implemented、Production Ready或完整Runtime。
