# PRD-004 — Evidence-Grounded LLM + RAG Root Cause Analysis

## 產品需求文件（PRD）v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | PRD-004 |
| Document Name | Evidence-Grounded LLM + RAG Root Cause Analysis |
| 中文名稱 | 證據驅動 LLM + RAG 根因分析與可稽核 RCA |
| Version | 1.0 |
| Status | Approved |
| Date | 2026-09-18 |
| Author | 林子豪（PM） |
| Upstream Requirement Authority | PRD-001、PRD-002、PRD-003 |
| Relevant Upstream Engineering Contracts | SPEC-001～004、SPEC-006～011 current approved contracts |
| Validation／Scenario Evidence | SPEC-005 current repository validation baseline |
| Downstream Planned Requirement | PRD-005 Operational Interfaces / Knowledge / Demo Integration |
| Target | Product、Engineering SPEC、QA／Evaluation、Demo／Competition |

### Version History

| Version | Date | Status | Change |
|---|---|---|---|
| 0.1 | 2026-09-18 | Working Draft | PM D1～D10 decision baseline；未進Repository。 |
| 0.2 | 2026-09-18 | Draft — PM Review Pending | 正式Repository Draft；整合current authority、D1～D10與PM-approved Revision Round 1。 |
| 0.3 | 2026-09-18 | Draft — PM Final Approval Candidate | Narrow PM Final Review patch：釐清authority hierarchy、加入`INITIAL_PENDING` downstream projection、封閉Incident `rca_status` refresh semantics。 |
| 1.0 | 2026-09-18 | Approved | PM Final Approval completed；D1～D10、Revision Round 1、Narrow Final Patch及AC-004-A～X通過Final Review；正式凍結為RCA Domain後續Engineering SPEC與cross-document reconciliation的產品需求權威。 |

> **Status Honesty：**`PRD-004 v1.0`已完成PM Final Review，Product Status維持`Approved`，正式成為RCA Domain後續Engineering SPEC與cross-document reconciliation的產品需求權威。Post-integration repository reality為Candidate A／B／C已依SPEC-012／013／014實作；Candidate D／E／F、LLM generation、RCA Runtime orchestration、final RCA／RAG E2E及Production Ready仍為Pending。產品語意版本不因本次implementation reconciliation升版。

---

# 1. 文件目的與產品定位

PRD-004定義平台如何以已建立的Incident為唯一RCA analysis unit，蒐集可追溯且有界的Incident／Event／Logs／Metrics evidence，從受治理的SOP Knowledge Corpus進行RAG retrieval，再由LLM產生structured、可版本化、可稽核且明確揭露不確定性的Root Cause Analysis。

本產品建立以下可信鏈結：

```text
Incident
→ bounded authoritative evidence
→ governed knowledge
→ structured reasoning
→ validated RCA
→ durable publication and history
→ evidence-grounded evaluation
```

核心產品目標：

1. RCA跟隨Incident完整事故脈絡，而不是跟隨單一Alert或Event。
2. 重要factual／causal claim可回溯至Evidence Snapshot中的正式evidence。
3. Incident Evidence與SOP Knowledge的角色、provenance及支持強度明確分離。
4. Evidence、Knowledge或外部服務不足時明確降級或安全失敗，不捏造缺失事實。
5. Material Evidence演進時建立可追蹤的新published version，不silent overwrite歷史。
6. 提供PRD-005穩定的RCA semantic truth，不讓presentation channels建立第二套RCA pipeline或authority。
7. 支援S1～S6 repeated evaluation及四名成員可重建的real-provider／real-RAG Demo path。

> **RCA follows the incident, not the alert.**
>
> **RCA跟著Incident的事故脈絡走，而不是跟著單一告警走。**

---

# 2. Authority Hierarchy與Frozen Boundaries

## 2.1 Authority Hierarchy

```text
Existing upstream PRD / SPEC authorities
→ remain authoritative within their existing domains

PRD-004, once approved
→ becomes the RCA-domain product requirement authority

Affected Engineering SPEC reconciliation
→ is required before implementation when PRD-004 adds capability
  to an existing upstream engineering contract
```

既有上游PRD／SPEC持續約束其各自的Event、Correlation、Incident、Lifecycle、Shadow與Runtime Domain；PRD-004核准後成為RCA Domain的產品需求權威。若PRD-004 requirement需要擴充既有Engineering Contract，必須先完成對應SPEC reconciliation，才能實作。

> **Existing upstream domain authorities remain binding for their respective Event, Correlation, Incident, Lifecycle, Shadow and Runtime domains. Once approved, PRD-004 becomes the product requirement authority for the RCA domain. Any PRD-004 requirement that requires additive changes to an existing engineering contract must be reconciled through the affected SPEC before implementation.**

此治理語意不授權PRD-004直接覆蓋SPEC-008、SPEC-011或其他既有domain contract，也不表示舊SPEC天然凌駕未來Approved PRD-004的RCA-domain product requirements。

| Domain | Current Authority | PRD-004 Boundary |
|---|---|---|
| Platform direction | PRD-001 | 細化RCA產品契約，不silent改寫Incident-driven platform方向。 |
| Event Detection／Schema | PRD-002、SPEC-001～004 | 只讀immutable Event，不修改Event或detector。 |
| Scenario／validation | SPEC-005 | 只供evaluation layer；不得進production RCA input。 |
| Correlation Policy | PRD-003、SPEC-006 | 不重跑、不重定義Strong／Weak、fingerprint、candidate或window。 |
| Correlation State | PRD-003、SPEC-007 | 不改Pending、Processed、Intent、Blocked、ownership或recovery precedence。 |
| Incident | PRD-003、SPEC-008 | Incident Manager／Store保持authority；不得直接寫physical persistence。 |
| Lifecycle | PRD-003、SPEC-009 | 不改assignment、review、closure或CLOSED terminal semantics。 |
| Shadow | PRD-003、SPEC-010 | 不改Shadow ownership，不自動送入RCA／RAG learning。 |
| Platform Runtime | SPEC-011 | 保持唯一WHEN／ORDER／RETRY scheduling／RECOVERY／cross-store coordination authority。 |
| RCA Domain | 本PRD經核准後 | 定義RCA obligation、artifact、evidence、knowledge、publication、version、failure及evaluation semantics。 |

## 2.2 Event與Observability Boundary

EventStore持續是immutable normalized Event evidence authority。PRD-004：

```text
READS Event
DOES NOT MUTATE Event
```

Evidence Snapshot不取代EventStore、Incident Store、Loki或Prometheus authority。LLM不得直接漫遊或任意操作上述系統，只能消費approved collector建立的frozen snapshots。

Production RCA不得使用`scenario_id`、Generator state、Validator expected answer、expected root cause、evaluation ground truth／labels或hardcoded S1～S6 answer mapping。

## 2.3 Correlation、Ownership與Shadow Boundary

PRD-004不得re-run／redefine correlation policy、改變Event terminal ownership、Strong／Weak／Pending／Shadow rules、fingerprint或candidate semantics，也不得將Blocked／Shadow直接轉成RCA Knowledge learning input。RCA只分析authoritative upstream已形成的Incident及其referenced Events。

## 2.4 Incident與Lifecycle Boundary

Incident Manager／Incident Record持續擁有Incident identity、Event relationship／ownership、lifecycle、assignment、review／closure及RCA relationship／current state。

```text
OPEN → ASSIGNED → IN_PROGRESS → AWAITING_REVIEW → CLOSED
```

RCA lifecycle與Incident lifecycle分離。RCA failure不得rollback Incident、阻塞human workflow或成為closure hard gate。`CLOSED`不reopen；RCA不得推動automatic backward lifecycle transition。

Incident只保存RCA relationship／current-state projection，不duplicate完整RCA Artifact。PRD-004不得直接修改Incident physical persistence。

## 2.5 Singular Platform Runtime Authority

> **SPEC-011 remains the platform Runtime orchestration authority; PRD-004 defines RCA domain semantics and RCA work requirements that execute under that Runtime governance.**
>
> **SPEC-011持續作為平台Runtime編排權威；PRD-004定義RCA Domain語意與RCA工作需求，而RCA工作必須納入該Runtime治理下執行。**

PRD-004定義何者構成RCA obligation／Material Evidence／STALE／failure disposition／refresh及publication success。平台何時schedule、如何排序、何時retry、如何startup recovery、如何恢復durable work、提供Runtime clock及cross-store sequencing，持續由SPEC-011治理。不得建立第二套competing Runtime authority、retry ledger或startup barrier。

---

# 3. Terminology與Conceptual Model

| Term | Product Meaning |
|---|---|
| Logical RCA Aggregate | 一個Incident至多一份的logical RCA，包含Current relationship、immutable version history及generation lineage。 |
| Published RCA Version | 通過validation及publication的immutable version；同一aggregate採連續public version order。 |
| Generation Attempt | 對固定Evidence Snapshot及Knowledge Snapshot產生candidate的logical工作單位。Material Evidence revision改變時建立新attempt。 |
| Execution Try／Retry | 同一Generation Attempt內的實際執行次數；不取得published version。 |
| Current RCA | 最新成功published且由Incident authority反映的version；同時最多一個。 |
| Historical／Superseded RCA | 不再Current但須保留的version；Superseded表示被較新Current取代，不代表刪除或必然錯誤。 |
| Fresh／Stale | Current是否已涵蓋latest known Material Evidence Revision。Stale仍可讀。 |
| Evidence Snapshot | 對單一Incident及Evidence Revision建立的immutable、bounded、可識別generation input。 |
| Incident Evidence Revision | 可重現識別technical evidence內容及collection boundary的semantic revision／fingerprint。 |
| Knowledge Snapshot | Attempt實際使用的retrieved knowledge、applicability及provenance之immutable snapshot。 |
| Knowledge Gap | 沒有適用approved knowledge的合法狀態，不等同RAG infrastructure failure。 |
| Material Evidence | 可能改變diagnosis、hypothesis ranking、support、conclusion或安全建議的technical evidence。 |
| Evidence Completeness | `FULL`或`DEGRADED`；與generation lifecycle分離。 |
| Diagnostic Conclusion | `IDENTIFIED`、`MOST_SUPPORTED`或`INCONCLUSIVE`。 |
| Publication | Artifact durable且Incident authority成功反映latest Current relationship。 |
| Publication Reconciliation | Artifact已durable但Incident relationship未完成時，以same-operation identity恢復publication。 |
| Credential Profile | 非secret local config identity，指向environment-provided credential及可用capability。 |
| Last-Known-Good Knowledge Index | 最近一次通過corpus／version／readiness validation並成功active的index。 |

## 3.1 RCA Cardinality

```text
1 Incident
→ 0..1 Logical RCA Aggregate
       ├─ Published RCA v1
       ├─ Published RCA v2
       └─ Published RCA vN
```

同時最多一個Current version。這是一份logical RCA的immutable version history，不是`1 Incident → N independent RCA objects`。

## 3.2 Orthogonal RCA Dimensions

```text
Generation Lifecycle: PENDING / GENERATING / COMPLETED / FAILED
Publication Role:      CURRENT / HISTORICAL
Freshness:             FRESH / STALE
Evidence Completeness: FULL / DEGRADED
Diagnostic Conclusion: IDENTIFIED / MOST_SUPPORTED / INCONCLUSIVE
```

Exact field及enum naming留SPEC。以下組合合法：

```text
generation = COMPLETED
publication = CURRENT
freshness = STALE
evidence completeness = DEGRADED
diagnostic conclusion = MOST_SUPPORTED
```

---

# 4. End-to-End Product Flow

```text
Durable authoritative Incident
      ├── AUTO_ASSIGN obligation
      └── initial RCA obligation
                    │
                    ▼
          Incident Evidence Collection
          ├── Incident public reads
          ├── EventStore public reads
          ├── Loki bounded collection
          └── Prometheus bounded collection
                    ▼
          Immutable Evidence Snapshot
                    ▼
          Governed Knowledge Retrieval
                    ▼
          Immutable Knowledge Snapshot
                    ▼
          Structured LLM Generation
                    ▼
          Parse / Schema / Reference /
          Grounding Validation
                    ▼
          Durable RCA Artifact
                    ▼
          Publication Reconciliation
                    ▼
          Incident Manager reflects Current RCA
                    ▼
          Public Semantic Reads for PRD-005
```

Assignment不是RCA eligibility gate。Assignment failure不rollback Incident、不取消RCA；RCA failure不rollback Incident、不取消Assignment。兩者在singular platform Runtime治理下按各自domain semantics執行。

---

# 5. Incident-Driven RCA Triggering

## 5.1 Initial RCA Obligation

RCA唯一analysis unit是Incident。Incident成功durable operationalized後即可形成initial RCA obligation，不要求先完成Assignment或進入`IN_PROGRESS`。合法狀態包括`Incident=OPEN`且`RCA generation=GENERATING`。

Initial RCA不等待完整post-context。目標是先提供可用、誠實且可追溯的analysis，再依Material Evidence產生後續version。

## 5.2 Material Evidence

Material Evidence至少包含new correlated Event、material Log／Metrics evidence、late Strong-Anchor promotion technical context、source恢復後取得的重要enrichment，或其他會改變hypothesis ranking、support、conclusion或remediation safety的evidence。

Assignee、reviewer、Jira／Discord／Email delivery、Dashboard view、單純workflow或presentation metadata不得單獨觸發refresh。

Materiality必須可重現、可audit，能回答：本RCA看到哪個revision、evidence是否materially changed、是否應refresh。Exact hash、serialization、diff及rule algorithm留SPEC。短時間多筆evidence可coalesce，但不得遺失latest required revision。

## 5.3 Lifecycle × Automatic Refresh

| Incident Status | Automatic RCA Behavior |
|---|---|
| `OPEN`／`ASSIGNED`／`IN_PROGRESS` | initial、material-evidence及post-context refresh可啟動。 |
| `AWAITING_REVIEW` | closure前已scheduled post-context或已知Material Evidence obligation可完成，使Reviewer取得更新RCA；不得使lifecycle backward。 |
| `CLOSED` | 不啟動new automatic refresh，不因RCA reopen Incident。 |

若closure前已有generation執行中，結果可保留作audit，但不得在closure後silent改寫Reviewer closure時確認的Current。Future post-closure re-analysis必須是explicit governed／manual capability，不屬v1 automatic path。

> **Publish early, revise with evidence, never rewrite history.**
>
> **先提供可用分析，再隨證據修正，但永不改寫歷史。**

---

# 6. Logical RCA Aggregate與Structured Artifact

## 6.1 Conceptual Artifact Semantics

Successful RCA必須是structured artifact，不得只保存free-text prose。

| Group | Required Semantics |
|---|---|
| Identity | Aggregate、Incident、attempt lineage、published version identity／order。 |
| Analysis | Summary、severity assessment、Diagnostic Conclusion。 |
| Hypotheses | bounded ranking、supporting／contradicting evidence及Evidential Support。 |
| Actions | remediation、prevention及SOP-backed／model-suggested distinction。 |
| Uncertainty | limitations、insufficient evidence及Knowledge Gap。 |
| Evidence Provenance | Snapshot identity、resolvable references、collection／truncation provenance。 |
| Knowledge Provenance | Knowledge Snapshot、document／section／chunk、corpus／index及applicability。 |
| Generation Provenance | provider、model、prompt、Credential Profile ID及configuration identity；不含secret。 |
| Lifecycle／Failure | timestamps、failure、retry lineage、publication／freshness／completeness。 |

Exact schema、field names、serialization、table及class留SPEC。

## 6.2 Evidence → Claim Traceability

每項重要factual／causal claim及hypothesis必須可對應supporting evidence，並在適用時保留contradicting evidence。Evidence description主要由authoritative source deterministic normalization產生。LLM可分析及重新敘述，但不得製造Snapshot中不存在的evidence facts。

RCA至少區分`Observed Fact`、`Analytical Inference`及`Knowledge-backed Guidance`。Remediation與Prevention必須分離。`severity_assessment`是advisory，不取得Incident severity mutation authority。

## 6.3 Read／Write Integrity

RCA persistence authority必須保證：同一Incident至多一個Aggregate；同時至多一個Current；published versions immutable且order唯一連續；failed attempt不消耗version；replay不得duplicate publication；malformed、dangling、contradictory或multiple-current state不得偽裝Not Found／silent skip；Current／history reads須coherent且可resolve lineage。Exact mechanism留SPEC。

---

# 7. Immutable Evidence Snapshot

## 7.1 Trusted Core與Coherent Revision

每次Generation Attempt使用一份immutable Evidence Snapshot，對應單一Incident Evidence Revision。

```text
valid authoritative Incident
+ all referenced authoritative Events
+ coherent Incident Evidence Revision
```

Referenced Event missing、authoritative contradiction、corrupt state或incoherent revision必須Fail Closed，不得在partial trusted core上宣稱success。

Evidence Revision／fingerprint須具deterministic semantic identity，以比較same evidence、post-context difference及refresh必要性。Exact algorithm留SPEC。

## 7.2 Episode與PoC Windows

```text
episode_start = min(correlated Events.detected_at)
episode_end   = max(correlated Events.detected_at)
```

不得以`Incident.created_at`取代technical time。

```text
Logs:    episode_start - 120s → min(snapshot_at, episode_end + 120s)
Metrics: episode_start - 300s → min(snapshot_at, episode_end + 120s)
```

以上是config-driven PoC defaults，不是production invariant。

## 7.3 Post-context Follow-up

Post-context是collection upper bound，不是initial generation delay。Boundary到達後即使沒有new Event，也要有follow-up obligation：no material difference則不建立unnecessary attempt／version；material difference則建立refresh attempt。新Event可推進boundary並提早觸發coalesced refresh。Follow-up受第5.3節及第12章治理。

## 7.4 Content、Query Provenance與Bounds

Evidence Snapshot至少表達Snapshot／Incident／revision identity、snapshot time、episode及query windows、Incident context、ordered Events、normalized Logs／Metrics、source status、selector／query boundary provenance，以及included、deduplicated、sampled、aggregated、truncated內容。

Selectors只能從Incident／Event evidence衍生，例如`service_name`、`trace_id`、`external_service`、`downstream_service`、正式S6 target-service evidence或`source_ip`；不得使用scenario metadata。

Logs、Metrics、Knowledge及LLM request／response均須bounded。若sampling、aggregation、dedup或truncation，Snapshot必須揭露included、omitted、原因、budget identity及completeness影響。LLM不得把truncated snapshot誤認為完整世界。Exact limits及algorithm留SPEC／config。

---

# 8. Governed RAG與Knowledge Snapshot

## 8.1 Corpus Authority

v1 Corpus只含approved SOP／operational knowledge。Runtime對active corpus read-only。Shadow、Closed Incident、Knowledge Improvement Candidate、LLM guidance或unreviewed remediation不得自動寫入Corpus／Index。Future ingestion必須human governance；本PRD不實作該UI。

## 8.2 Applicability、Gap與Guidance

```text
DIRECT / PARTIAL / CONTEXTUAL / NONE
```

```text
retrieval similarity
≠ knowledge applicability
≠ root-cause evidential support
```

Applicability須具provenance及validation，不得是無依據模型自評。SOP不得單獨把generic diagnosis提升為此次Incident factual root cause。

`NO_MATCH`是合法Knowledge Gap，可產生`COMPLETED + knowledge_gap=true` evidence-grounded RCA。Guidance至少區分`SOP_BACKED`及`MODEL_SUGGESTED`；後者須明示未由approved knowledge支持，且不取得automatic remediation authority。

## 8.3 Knowledge Snapshot

每個Attempt綁定immutable Knowledge Snapshot，至少保留corpus manifest、active index build、embedding、retrieved document／version／section／chunk、retrieval provenance、source status及applicability。後續Knowledge更新不得retroactively rewrite historical RCA。

---

# 9. Ranked Hypotheses與Diagnostic Semantics

## 9.1 Bounded Ranked Hypotheses

RCA不得只以單一free-text root cause作唯一診斷輸出。每項hypothesis至少具有：

```text
rank
statement
evidential support
supporting evidence
contradicting evidence
knowledge provenance
reasoning summary
```

Exact Top-K留SPEC／config，但不得無界輸出。

## 9.2 Evidential Support

PRD-004 v1不採未校準numeric LLM confidence，採`HIGH／MEDIUM／LOW`表示evidence對statement的支持強度，而不是statistical probability或model self-assurance。

> **Confidence must describe evidential support, not model self-assurance.**
>
> **信心程度要描述證據支持度，而不是模型自己的自信程度。**

## 9.3 Diagnostic Conclusion

| Conclusion | Product Meaning |
|---|---|
| `IDENTIFIED` | Evidence足以支持statement所聲稱的operational causal granularity，且無重大contradictory evidence。 |
| `MOST_SUPPORTED` | 一個hypothesis明顯較受支持，但causal chain不足以稱為identified。 |
| `INCONCLUSIVE` | Evidence不足或competing hypotheses無法合理排除。 |

Root cause identification以Evidence支持的operational causal granularity為準，不要求永遠找到最深code-level defect。`COMPLETED`不代表一定`IDENTIFIED`；`COMPLETED + INCONCLUSIVE`必須合法。

Evidence Completeness與Conclusion須合理校準。`DEGRADED`不自動禁止`IDENTIFIED`，但conclusion不得超出available evidence及其granularity。

---

# 10. RCA Lifecycle、Version、Freshness與Publication

## 10.1 Attempt、Retry與Version

```text
Logical Generation Attempt
  └─ Execution Try #1
  └─ Retry #1..#4, when eligible

Successful validated publication
  └─ Published Version vN
```

同一Attempt的bounded retries使用同一Evidence Snapshot、Knowledge Snapshot、attempt identity及Credential Profile；不取得新published version；不因restart建立新attempt。Retry前須確認attempt是否已完成或被newer Material Evidence淘汰。

Failed Attempt不消耗version。Material Evidence Revision改變時建立new Attempt，而不是將new evidence偷偷加入舊retry。

## 10.2 Current、Fresh與Stale

`Incident.rca_ref` conceptually指latest successfully published Current RCA version。Incident不得保存完整Artifact，也不得由RCA component直接修改physical persistence。

Material Evidence超前於Current Snapshot時：

```text
Current remains readable
publication = CURRENT
freshness = STALE
```

Refresh candidate成功前不得切換Current。已有Current時，refresh不得使availability退回「無可用RCA」、不得因failure把last-known-good改為整體FAILED、不得刪除／遮蔽Current，且須向downstream揭露STALE及refresh state。

Incident的`rca_status`表達coarse RCA availability／initial processing relationship state；background refresh的lifecycle、freshness與failure屬RCA Domain語意，不得覆寫既有last-known-good Current RCA的可用性。

在first successful RCA publication前，Incident relationship projection可概念反映initial processing availability，例如`rca_ref=null`且`rca_status=PENDING／GENERATING／FAILED`；exact physical mapping留後續reconciliation／SPEC。

一旦v1已成功published為Current，background v2處於`PENDING`、`GENERATING`或`FAILED`時，Incident coarse projection必須維持：

```text
rca_ref = v1
rca_status = COMPLETED
```

RCA Domain public semantics另行表達`Current v1 = STALE`及refresh為`PENDING`、`GENERATING`或latest refresh `FAILED`。只有v2完成validation及publication後，`Incident.rca_ref`才由v1切換為v2；`Incident.rca_status`維持`COMPLETED`，v1成為Historical／Superseded。Exact enum storage implementation不由本PRD freeze。

## 10.3 Promotion與Supersede

Candidate通過structured、schema、reference、grounding及publication checks後才promotion：

```text
new version → CURRENT + FRESH
old Current → HISTORICAL / SUPERSEDED
```

Superseded不代表舊version被刪除或必然錯誤。Completed Artifact immutable；修正只能建立new version。

Refresh期間若又有newer Material Evidence，不得無界平行generation；須保留newer refresh obligation。Candidate完成時若已被newer revision淘汰，可保存audit result但不得錯誤promote為FRESH。

## 10.4 Publication與Incident Relationship

Artifact durable不等於publication complete：

```text
RCA Artifact is durable
+ Incident authority successfully reflects Current RCA relationship
```

若Artifact durable後Incident relationship update失敗：

- 不刪除Artifact；
- 不bypass Incident Manager；
- 不直接寫Incident DB；
- 保留same-operation Publication Reconciliation obligation；
- restart後可deterministic reconcile；
- 不建立另一version掩蓋linkage failure。

Exact cross-store protocol留SPEC，並在SPEC-011 singular Runtime governance下執行。

現有SPEC-008只提供`rca_status`／`rca_ref` initial persistence及preservation contract，尚未提供完整RCA relationship mutation surface。後續需要narrow additive public semantic refinement及documentation／SPEC reconciliation；本PRD不假裝該API已存在，也不freeze physical type或method signature。

Post-integration qualifier：上段保留PRD-004 approval時的repository baseline事實。Current repository已由SPEC-008 v1.2 approved additive boundary及v1.3 documentation-only closure記錄RCA relationship mutation／read capability與caller-driven publication coordinator為Implemented；這不表示SPEC-011 RCA Runtime orchestration已完成。

---

# 11. Failure、Degradation與Bounded Retry

## 11.1 Lifecycle與Completeness分離

Generation lifecycle維持`PENDING／GENERATING／COMPLETED／FAILED`；不得新增`PARTIAL`。Evidence Completeness另以`FULL／DEGRADED`表達。

## 11.2 Source Status

| Status | Meaning |
|---|---|
| `AVAILABLE` | Source可用且collection成功。 |
| `EMPTY` | Query合法成功但無matching data；不等於failure。 |
| `UNAVAILABLE` | Infrastructure／provider不可用。 |
| `INVALID` | Response、reference、version或content不符合contract。 |

RAG須區分`NO_MATCH`（legal Knowledge Gap）與`RETRIEVAL_UNAVAILABLE`（system failure）。

Loki／Prometheus／RAG unavailable經bounded retry後，若Trusted Core足夠，可產生`COMPLETED + DEGRADED`，但所有gap、omission及diagnostic影響須明示。Invalid trusted core或authoritative contradiction不得safe degrade，必須Fail Closed。

## 11.3 Failure Disposition與Retry

```text
RETRYABLE
NON_RETRYABLE
REPAIR_REQUIRED
```

真正理解錯誤的component／domain決定retry safety；SPEC-011 Runtime決定retry scheduling。不得以message或local guess分類。

PoC default：

```text
initial execution try
+ max 4 automatic retries

1s → 2s → 4s → 8s
```

Policy可config-driven。Retry budget、attempt count及next eligibility跨restart保持，restart不得重給budget。

Structured parse、schema或reference failure可依typed disposition bounded retry；耗盡後不得保存unvalidated prose、mock、canned fallback或scenario answer作successful RCA。

> **Degrade explicitly, fail safely, and never fabricate missing evidence.**
>
> **可以明確降級，但必須安全失敗，而且永遠不能捏造缺失的證據。**

---

# 12. Runtime、Crash、Restart與Publication Recovery

## 12.1 Durable RCA Obligations

以下obligations不能因crash／restart遺失、重置或錯誤完成：

- initial RCA generation；
- pending Material Evidence refresh；
- scheduled post-context follow-up；
- retry progress及next eligibility；
- Publication Reconciliation；
- STALE Current requiring refresh；
- generation期間形成的newer revision follow-up。

上述work在SPEC-011 Runtime治理下執行。PRD-004定義domain meaning；exact durable representation、scheduler、locking及storage留SPEC。

## 12.2 Restart Is Recovery, Not Reset

Restart不得reset budget／attempt identity、無理由duplicate Attempt／version／promotion、遺失post-context、將STALE silent標FRESH、將failed refresh當成no RCA，或刪除Artifact／Snapshot／lineage／contradictory evidence。

Startup recovery須透過authoritative public reads分類及恢復obligations。Corrupt／contradictory state無法可靠分類時Fail Closed並operator-visible，不得guess、last-write-wins或automatic destructive repair。

## 12.3 Cross-Store Recovery

Artifact commit與Incident publication之間crash時：

1. 讀取RCA authority same-operation evidence；
2. 讀取Incident current relationship；
3. 若一致，完成orchestration obligation且不重做publication；
4. 若Artifact durable但relationship缺失，以same identity恢復Incident Manager mutation；
5. contradiction或multiple-current時Fail Closed／REPAIR_REQUIRED；
6. 不繞過API、不要求2PC、不建立replacement Artifact。

## 12.4 External API Non-Transactional Honesty

Provider已處理request但local尚未durable記錄時crash，restart可能再次呼叫provider。PRD-004不宣稱external API exactly-once。

```text
Correctness: no duplicate published RCA semantic effect
Not guaranteed: exactly one external network call
```

Provider invocation及可能duplicate cost須可觀察且受第15章bounds治理。

---

# 13. Model、Prompt、Credential與Deterministic Testing

## 13.1 Approved PoC Technology Direction

```text
LLM:          Gemini 2.5 Flash
Embedding:    Google text-embedding-004
Vector Store: ChromaDB
```

這是current approved PoC direction。Provider／model family變更須governance review，不得silent replacement。

## 13.2 Provider Abstraction與Prompt

RCA contract不得耦合Gemini-specific response shape。PoC不要求multiple providers或automatic failover；exact port／adapter／SDK留SPEC。

每個Attempt至少保存provider、model、stable prompt version、Knowledge／index provenance、non-secret Credential Profile ID、configuration identity及timestamps。Prompt change是behavior change，不得silent修改或包含ground truth。Output通過parse、schema、reference及grounding validation後才可publish。

> **The model is replaceable; the RCA contract is not.**
>
> **模型可以替換，但RCA契約不能跟著模型漂移。**

## 13.3 Credential Profiles

支援config-driven profiles，例如Profile 1～4。Profile ID非secret，credential由local environment提供。Demo可選`Scenario + available Credential Profile`。

- 未設定或capability不足的Profile不顯示；
- profile須驗證generation／embedding所需capability；
- Attempt綁定selected profile，retry沿用；
- 不silent automatic key switching；
- telemetry／audit可記Profile ID，不得記API key。

## 13.4 Deterministic Testing

Default regression不依賴live API。Domain／orchestration tests可用Fake adapters；provider contract tests用mocked／sanitized response；live integration explicit opt-in且需credential。正式Demo／Evaluation必須使用real configured LLM及real RAG，不得以Fake output冒充。Regression count是Engineering Confidence Evidence，不是accuracy。

---

# 14. ChromaDB與Multi-Member Portability

## 14.1 Repository與Local Authority

Git authority包括approved SOP sources、knowledge manifest、chunking／ingestion contract、rebuild tooling及corpus／index version。Local Chroma files是generated local state，不是Git authority，不得以個人local DB作team baseline。

## 14.2 Knowledge Index Lifecycle

Knowledge Index須有approved manifest、build identity／version、embedding／ingestion provenance、activation／readiness validation、Last-Known-Good active index及drift detection。Failed rebuild不得corrupt、replace或silent activate over Last-Known-Good。Mismatch、corrupt或unavailable不得偽裝RAG success。

## 14.3 Capability-Specific Readiness

```text
Index READY → normal RAG-backed RCA
Index UNAVAILABLE / MISMATCH → RAG unavailable or degraded
```

Knowledge unavailable不自動阻止whole-platform Runtime READY，也不重新定義SPEC-011 barrier。Trusted Core足夠時可走evidence-only `DEGRADED` path。正式RAG Demo／Evaluation不得在mismatch／unavailable時宣稱RAG成功。

## 14.4 Four-Member Setup Contract

四名成員任一台符合setup contract的電腦，必須能從同一Repository配置local credentials、重建compatible index、detect drift、選Scenario、選available Profile並執行RCA Demo。Setup須揭露Event Detection model artifacts、observability及其他prerequisites；不得依賴hidden PM-only sequence、personal path或copy他人Chroma files。

---

# 15. Security與External Provider Boundary

## 15.1 Secret與Outbound Data

Credentials不得hardcode／commit，也不得進Artifact、Snapshot、logs、telemetry、prompt provenance或failure summary。Repository可提供non-secret examples，真實secret由local environment提供。

v1 external LLM／embedding只處理synthetic／mock operational data。Outbound payload只能來自approved Evidence Snapshot、Knowledge Snapshot及prompt template，並須通過allowlist／redaction，排除secret、unapproved files及非Snapshot data。無法確認安全時Fail Closed。

## 15.2 Invocation Bounds

Provider invocation須具configurable timeout、request budget、retry budget、input／output／token bound及rate／quota／cost protection。Budget exhausted須operator-visible，不得以fallback偽裝成功。除approved retry default外，exact數值留SPEC／config。

Raw provider request／response若controlled debug保存，須另有least-privilege、redaction及retention boundary，且不屬primary Artifact。

---

# 16. Evaluation與Product Acceptance Baseline

本章正式收納PM-approved competition evaluation baseline，作為PRD-004產品驗收需求。Repository目前沒有獨立、已核准且命名為EVAL-13～15的authority document；本PRD不得假裝該artifact已存在。未來若建立，須documentation governance reconciliation。

## 16.1 Ground Truth Isolation

S1～S6 evaluation-only fixtures至少定義scenario identity、expected causal class、accepted operational granularity、required evidence classes、forbidden overclaims、acceptable alternatives及expected knowledge behavior。Ground Truth只在evaluation／test layer，不得成為production input、prompt、selector或Runtime decision source。

## 16.2 Root Cause Identification Success

成功至少要求Top hypothesis命中expected causal class、不超出accepted／evidence-supported granularity，且Conclusion calibration合理。不用exact prose matching。

## 16.3 Unsupported Claim Rate

Atomic factual／causal claims至少分類`GROUNDED／UNSUPPORTED／CONTRADICTED／NOT_APPLICABLE`。

```text
Unsupported Claim Rate
= (UNSUPPORTED + CONTRADICTED factual/causal claims)
  / all evaluated factual/causal claims
```

正確uncertainty、limitations及`MODEL_SUGGESTED` guidance不得誤算為unsupported factual claim。Claim decomposition及review protocol留Evaluation SPEC，但須可重現／audit。

## 16.4 Conclusion Calibration、Degraded與Version Cases

Evaluation須驗證`IDENTIFIED／MOST_SUPPORTED／INCONCLUSIVE`與evidence、contradiction及granularity一致；此為engineering／backup evidence，不取代兩項main quality metrics。

至少涵蓋Loki unavailable、Prometheus unavailable、RAG NO_MATCH、RAG unavailable／index mismatch、LLM transient failure、invalid structured output、refresh failure preserving Current及authoritative integrity Fail Closed。

Version cases至少涵蓋：

```text
v1 → evidence change → v1 STALE → v2 CURRENT/FRESH
failed refresh preserves v1
post-context no difference suppresses new version
failed Attempt does not consume version
restart does not duplicate attempt/version
```

## 16.5 Repeated Evaluation

正式Evaluation必須repeated-run。Exact `N`依quota、cost、runtime及variance於plan／SPEC提出並在run前固定。Report至少揭露N、invalid／failed disposition、model、prompt、corpus／index、configuration及success／failure／degraded distribution。

## 16.6 Full E2E Latency

```text
evaluation signal_started_at
→ first successfully published usable Current RCA
```

Published usable要求Artifact durable且Incident authority已反映Current，可由public semantic read取得。Post-context不阻塞主latency。主報告至少提供N／Median／p95；refresh latency可作backup evidence，regression count不得冒充accuracy。

> **Evaluate the diagnosis, not the eloquence.**
>
> **評估的是診斷是否可信，不是文字寫得漂不漂亮。**

---

# 17. PRD-005 Downstream Handoff

## 17.1 Stable Semantic Read Boundary

PRD-004須提供stable semantic reads，使PRD-005至少能取得Incident的Current RCA、Aggregate published history、指定version完整Artifact／lineage，以及current freshness、completeness、diagnostic conclusion、active／last refresh outcome。

Exact HTTP／REST／Python method、route、DTO及pagination留SPEC。PRD-005不得直接讀RCA physical DB。

## 17.2 Required Read Projection

Downstream至少能區分：

```text
NO_RCA_YET

INITIAL_PENDING
INITIAL_GENERATING
INITIAL_FAILED

CURRENT_FRESH

CURRENT_STALE_REFRESH_PENDING
CURRENT_STALE_REFRESHING
CURRENT_STALE_REFRESH_FAILED

CURRENT_WITH_DEGRADED_EVIDENCE
```

`NO_RCA_YET`表示downstream尚不存在可觀察的RCA obligation／relationship state。`INITIAL_PENDING`表示Incident已存在且initial RCA obligation已成立，但generation尚未開始。`INITIAL_GENERATING`表示initial RCA generation正在進行。

以上是product-observable semantics，不freeze exact enum或response layout。PRD-005不得只靠`rca_ref != null`猜測freshness、refresh或failure。

## 17.3 Presentation與Command Boundary

PRD-004提供semantic truth；PRD-005只負責presentation、delivery及authorized human interaction。Dashboard、Discord、ChatOps、Jira及Email不得直接寫RCA persistence、自行組裝第二套Evidence→RAG→LLM pipeline、自行宣告Current／Fresh／Superseded，或以delivery metadata改變technical materiality。

Future controlled manual refresh若需要，須透過RCA public semantic command及Runtime governance，不得由PRD-005直接呼叫provider。

## 17.4 Knowledge Workflow Handoff

PRD-004輸出`knowledge_gap`、Knowledge provenance及`MODEL_SUGGESTED` distinction。Human-facing Knowledge Improvement Candidate creation／review、Dashboard workflow及approved future ingestion由PRD-005或後續governance負責；PRD-004不自動修改Corpus。

---

# 18. Retention、Reset與Cleanup

Lifecycle、retention、reset／cleanup是不同概念，不得混用。

PoC default：

- published history不因失去Current、Incident status或age自動刪除；
- Superseded version保持可讀；
- failed Attempts保留必要audit、failure、retry及lineage metadata；
- Evidence／Knowledge Snapshots保留足以解釋published RCA及重要failed attempt的lineage；
- Publication Reconciliation及outstanding work完成前不得因restart／cleanup遺失；
- normal Runtime不得destructive cleanup；
- restart不是reset。

Production retention期限、archive及physical enforcement留future configurable policy，但不得破壞Current／history、Incident relationship、audit、dedup、version、snapshot或referential integrity。

Reset／cleanup只適用controlled development、testing、migration或maintenance，須事前列出affected stores／data並取得PM／team explicit authorization。Validator、test failure handler、normal Runtime及AI coding agent不得為使流程通過而自動清除authority state。

---

# 19. Product Scope

## 19.1 In Scope

- Incident-driven initial RCA及Assignment-independent eligibility；
- Material Evidence refresh、coalescing及lifecycle-aware post-context；
- immutable、bounded Evidence Snapshot；
- Event／Logs／Metrics reads、normalization、revision、query及truncation provenance；
- governed RAG、Knowledge applicability／gap／Snapshot；
- Last-Known-Good Knowledge Index及team-local rebuild；
- structured Aggregate、Artifact及immutable version history；
- ranked hypotheses、Evidential Support及Diagnostic Conclusion；
- Current／Fresh／Stale／Historical／Superseded semantics；
- typed failure、explicit degradation及bounded retry；
- crash／restart continuity及Publication Reconciliation requirements；
- Gemini boundary、prompt／model／secret governance；
- Credential Profiles及four-member reproducible Demo；
- deterministic tests、opt-in live integration及real Demo path；
- S1～S6 RCA Evaluation及technical evaluation runner；
- public semantic reads及PRD-005 projection。

## 19.2 Out of Scope

- Dashboard、Discord／War Room／ChatOps、Jira及Email implementation；
- human-facing Knowledge Candidate review UI；
- final full-platform presentation；
- automatic remediation；
- automatic SOP／KB mutation、candidate ingestion或Vector DB learning update；
- autonomous LLM exploration of Loki／Prometheus；
- production financial／personal data；
- enterprise secret manager／RBAC；
- multi-region／HA LLM infrastructure；
- multi-provider routing／automatic failover；
- production retention enforcement；
- post-closure automatic re-analysis。

---

# 20. Product Acceptance Criteria

PRD-004未達以下條件不得宣稱完成。

### AC-004-A — Initial RCA與Parallelism

Durable Incident可形成initial RCA obligation，Assignment不是gate。Assignment failure不取消RCA；RCA failure不rollback Incident或取消Assignment。

### AC-004-B — Evidence Snapshot Provenance

每個Attempt可追溯immutable Evidence Snapshot、deterministic Evidence Revision、authoritative Incident／Events及bounded Logs／Metrics query provenance。

### AC-004-C — Bounded Evidence Honesty

Evidence及LLM payload有界；sampling／aggregation／dedup／truncation時可觀察included、omitted及reason，不偽裝完整。

### AC-004-D — Structured RCA

Successful RCA符合structured semantics；raw prose、mock、canned fallback或scenario answer不得冒充success。

### AC-004-E — Grounding與Reference Integrity

重要claims可對應supporting evidence；Evidence／Knowledge refs可deterministic resolve。Dangling或contradictory reference validation fail／Fail Closed。

### AC-004-F — RAG Provenance與Knowledge Gap

RCA可追溯Knowledge Snapshot、corpus／index／document及引用位置。`NO_MATCH`與retrieval failure可區分；Knowledge Gap可合法完成evidence-grounded RCA。

### AC-004-G — Diagnostic Honesty

系統可輸出三種Conclusion，資料不足時不強迫確定root cause或超出operational causal granularity。

### AC-004-H — Aggregate與Version Governance

每Incident至多一Aggregate及一Current。Published versions immutable；failed attempt不消耗version；Material Evidence使Current STALE，新version成功publication後才promotion。

### AC-004-I — Refresh Failure Preservation

Refresh failure不得刪除／遮蔽last-known-good；已有Current時Incident coarse `rca_status`維持`COMPLETED`，downstream可讀Current STALE及refresh failure。

### AC-004-J — Explicit Degradation

Logs／Metrics／Knowledge unavailable且Trusted Core允許時，可`COMPLETED + DEGRADED`並揭露gap，不製造missing evidence。

### AC-004-K — Fail Closed Integrity

Incident／Event contradiction、incoherent revision、multiple-current、dangling critical reference或corrupt trusted state不得產生successful RCA。

### AC-004-L — Attempt／Retry Identity

Retry沿用Snapshot、attempt及Profile；restart不duplicate attempt、不reset budget。Material Evidence變更建立new attempt。

### AC-004-M — Crash／Restart Continuity

Initial、refresh、post-context、retry、STALE及publication work在restart後可安全恢復；restart是recovery，不是reset。

### AC-004-N — Publication Reconciliation

Artifact durable但Incident update失敗時，same-operation obligation跨restartreconcile；不bypass Incident Manager、不duplicate version、不直接寫DB。

### AC-004-O — Secret與Outbound Safety

Credentials不進Git、Artifact、Snapshot、logs、telemetry或failure。External payload只由approved Snapshot／prompt組成並通過allowlist／redaction。

### AC-004-P — Provider Bounds與Honesty

Invocation具timeout、request／retry／token及rate／quota／cost protection；不宣稱external call exactly-once，但不得duplicate published semantic effect。

### AC-004-Q — Credential Profiles

Demo可選Scenario及available Profile；未設定／capability不足不顯示；Attempt綁定profile且不silent switching。

### AC-004-R — Team Portability

四名成員任一台符合setup contract的電腦皆可由同一Repository配置credentials、重建compatible index、偵測drift並跑real-provider／real-RAG Demo。

### AC-004-S — Knowledge Index Last-Known-Good

Failed rebuild不取代／破壞Last-Known-Good；mismatch／unavailable可偵測並形成capability-specific degraded state，不偽裝RAG success。

### AC-004-T — Deterministic Regression與Real Demo

一般regression不依賴live Gemini／Embedding；正式Demo／Evaluation使用real LLM及real RAG，不以Fake冒充。

### AC-004-U — Evaluation

S1～S6具Root Cause Success、Unsupported Claim Rate、Conclusion Calibration、degraded／failure／version cases、repeated-run及Full E2E contract。

### AC-004-V — Downstream Projection

PRD-005可透過semantic interface區分`NO_RCA_YET`、`INITIAL_PENDING`及其他initial／Current／refresh states，並讀取History、freshness、completeness及diagnostic semantics；不得直接讀DB或只靠`rca_ref`猜測。

### AC-004-W — Closed-Incident Boundary

OPEN／ASSIGNED／IN_PROGRESS可automatic refresh；AWAITING_REVIEW只完成已scheduled／known obligation；CLOSED不啟動new automatic refresh、不因RCA reopen，in-flight result不得silent改寫Reviewer確認的Current。

### AC-004-X — Singular Runtime Authority

RCA work遵守SPEC-011 singular Runtime governance；不存在第二套competing Runtime、startup recovery、retry scheduling或cross-store coordinator。

---

# 21. Candidate Engineering Decomposition

本節是non-frozen planning，不指定正式SPEC number，不freezeclass、table、API或topology。

## Candidate A — RCA Artifact & Persistence

Logical Aggregate、published versions、attempt lineage、Current／history、integrity、public semantic reads及Incident integration boundary。

## Candidate B — Incident Evidence Collection & Snapshot

Incident／Event reads、Loki／Prometheus collection、Evidence Snapshot、revision／materiality、post-context及bounded／truncation provenance。

## Candidate C — RAG Knowledge Index & Retrieval

Corpus／manifest、embedding、index build、Last-Known-Good activation、retrieval、applicability、Knowledge Snapshot及team-local rebuild。

## Candidate D — LLM Generation & Validation

Gemini boundary、prompt／profile provenance、structured output、schema／reference／grounding validation及invocation bounds。

## Candidate E — RCA Orchestration, Publication & Recovery

Initial、refresh、post-context requirement、attempt／retry continuity、promotion、Publication Reconciliation、restart及SPEC-011 integration。

## Candidate F — RCA Evaluation & E2E Validation

S1～S6 fixtures、repeated-run、main metrics、Conclusion Calibration、degraded／refresh cases、Full E2E及real-provider／real-RAG team Demo。

建議順序：先完成upstream reconciliation與A；B／C可平行；D依賴A／B／C；E整合A～D與SPEC-008／011；F進行full validation。

## 21.1 Post-Integration Downstream Mapping／Reconciliation Ledger（2026-09-26）

本ledger是additive current mapping，不改寫上述historical planning／decomposition，也不表示PRD-004在當時已指定SPEC number。

| Candidate | Current downstream mapping | Current disposition |
|---|---|---|
| Candidate A — RCA Artifact & Persistence | SPEC-012 | Implemented |
| Candidate B — Incident Evidence Collection & Snapshot | SPEC-013 | Implemented |
| Candidate C — RAG Knowledge Index & Retrieval | SPEC-014 | Implemented；AC-014-X另依該SPEC記錄為`NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE`，不是PASS |
| Candidate D — LLM Generation & Validation | 尚無completed downstream implementation | Pending |
| Candidate E — RCA Orchestration, Publication & Recovery | 尚無completed downstream implementation；caller-driven publication coordinator不等於RCA Runtime scheduler／recovery authority | Pending |
| Candidate F — RCA Evaluation & E2E Validation | 尚無completed downstream implementation | Pending |

Integrated develop full regression evidence為`1669 passed, 3 skipped, 0 failed`。此evidence支持completed SPEC的current implementation reconciliation；3個skipped tests不構成AC-014-X、live Google provider或final RCA E2E的PASS evidence。

---

# 22. Cross-Document Governance Impact

本節只列PRD-004核准後的governance obligation；本文件不自行修改下列authority。

## 22.1 MUST RECONCILE AFTER PRD-004 APPROVAL

| Document／Artifact | Reason |
|---|---|
| PRD-001 | Aggregate／version cardinality、Assignment-independent eligibility、completion及evaluation wording。 |
| PRD-003 | RCA relationship／projection、Material Evidence refresh及lifecycle handoff。 |
| SPEC-008 | Narrow additive Incident Manager mutation／read capability、same-operation publication及integrity。 |
| SPEC-011 | RCA durable work、scheduling、retry／recovery、clock及Publication Reconciliation integration。 |
| DDS-001 | Evidence Collector、RCA Store、Knowledge Index、LLM adapter及logical architecture。 |
| README | Implementation status、setup、commands、profiles、index rebuild及Demo prerequisites。 |
| Runtime documentation | RCA obligations、startup recovery、capability readiness及restart continuity。 |
| Architecture diagram | RCA／Evidence／Knowledge components及singular Runtime coordination。 |
| Competition evaluation material | Quality metrics、isolation、repeated-run及latency endpoint。 |

## 22.2 LIKELY RECONCILE

- `docker-compose.yml`：若final implementation需要process、volume或dependency；本PRD不freeze topology。
- `.gitignore`：local Chroma／generated artifacts。
- `.env.example`：non-secret Profile及provider expectations。
- deployment／setup docs：four-member rebuild、model artifacts、observability及live Demo prerequisites。
- dependency manifests：Gemini、embedding、Chroma及approved libraries。
- SPEC-005／evaluation tooling docs：fixture isolation及full evaluation boundary。
- SPEC-009：只在需補RCA relationship concurrent preservation或projection時narrow reconcile；RCA仍非closure gate。

## 22.3 VERIFY ONLY

PRD-002、SPEC-001、SPEC-002、SPEC-003、SPEC-004、SPEC-006、SPEC-007、SPEC-010及existing detector／correlation configs／tests。若Event、Detector、Correlation、Pending、Shadow或ownership semantics未變，不應無理由修改。

---

# 23. Open Implementation Choices與Deferred Decisions

以下留Engineering SPEC／configuration：

- exact SQL／schema、database library及persistence technology；
- exact class、module、API route、REST method、DTO及pagination；
- exact lock、CAS、transaction及cross-store protocol；
- exact Evidence hash、serialization、diff及materiality engine；
- exact sample、token、payload limits及sampling／aggregation algorithm；
- exact chunk size、Top-K、embedding batch及Chroma internals；
- exact prompt file path、syntax及provider SDK；
- exact Runtime work record／scheduler；
- exact Docker topology／volume；
- repeated evaluation exact `N`，但run前須固定揭露；
- production retention及future governed manual／post-closure re-analysis。

Implementation choice不得削弱authority boundary、grounding、immutability、version、degradation、recovery、security或evaluation requirements。

---

# 24. Final PM Requirement Position

PRD-004建立的不是「會回答維運問題的Chatbot」，而是以Incident為單位、以authoritative Evidence為基礎、以approved Knowledge為輔助、可版本化且可稽核的RCA Domain。

系統允許Knowledge不完整、optional source unavailable、diagnosis INCONCLUSIVE、refresh失敗、provider暫時失敗及Current暫時STALE；但必須明確表示，並遵守：

1. 不用ground truth／scenario metadata作production answer。
2. 不製造Snapshot中不存在的事實。
3. 不silent overwritehistory。
4. 不改寫Incident lifecycle、Event、Correlation或Shadow authority。
5. 不建立第二套Runtime authority。
6. 不以restart重給budget或清除obligations。
7. 不以mock／fallback／unvalidated prose冒充success。
8. 不讓PRD-005 channels成為RCA semantic authority。

> **Trustworthy RCA requires evidence, provenance, uncertainty, recovery, and history — not just a plausible answer.**
>
> **可信的RCA需要證據、來源、不確定性、復原能力與歷史，而不只是一個看似合理的答案。**
