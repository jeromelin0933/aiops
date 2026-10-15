# SPEC-017 — RCA Evaluation & E2E Validation

## Engineering Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-017 |
| Document Name | RCA Evaluation & E2E Validation |
| Version | 1.0 |
| Status | Approved |
| Date | 2026-10-02 |
| Approval Date | 2026-10-02 |
| Owner | SPEC Lead |
| Product Authority | PRD-004 v1.0 Approved |
| Oracle Approval / Amendment Authority | 林子豪（PM） |
| Candidate | Candidate F |
| Repository Baseline | `7362d458a3908fdce38174db5ddaa69ed002657a` |

### Change History

| Version | Date | Status | Change |
|---|---|---|---|
| 0.1 | 2026-10-02 | Draft | Phase 2 initial Candidate-F Engineering Contract；formalized Frozen F1～F8及PM Approved S1～S6 Oracle。 |
| 1.0 | 2026-10-02 | Approved — Implementation Pending | Phase 3 Review提出F-001～F-004；Phase 4 Narrow Patch完成；Phase 3 Re-review PASS且全部findings closed。F1～F8及PM Oracle semantics preserved；正式核准進入implementation handoff planning，implementation尚未開始。 |

### Status Honesty

Draft ≠ Approved；Approved ≠ Implemented；Implemented ≠ Evaluation Executed；Evaluation Executed ≠ Product Acceptance Complete；Product Acceptance Complete ≠ Production Ready。

SPEC-017 v1.0是Approved Candidate-F evaluation-only Engineering Contract。Approval只授權implementation handoff planning；它不表示Candidate F已實作、不表示任何Evaluation已執行、不表示real LLM／real RAG／Docker gate已通過，也不表示PRD-004 Product Acceptance完成或系統Production Ready。Candidate F仍只可`observe / map / grade / aggregate / report`。

---

# 0. Purpose、Authority與Frozen Decisions

## 0.1 Purpose

Candidate F只負責：

```text
observe → map → grade → aggregate → report
```

本SPEC formalize S1～S6 RCA Evaluation Oracle、evaluation identities、isolation、adjudication、repeated-run aggregation、latency、failure／degraded／refresh／restart evaluation、reproducibility及evidence package。Candidate F不得取得任何production truth、scheduling、retry、recovery、publication或Current RCA authority。

## 0.2 Authority Order

衝突時依下列順序處理：

1. PRD-004 v1.0 Approved；
2. PM Approved S1～S6 RCA Evaluation Oracle Handoff；
3. PM Final Competition Evaluation Scope；
4. implemented active SPECs；
5. Frozen Candidate-F F1～F8 Decisions；
6. repository reality；
7. supporting docs。

若formalization發現無法同時滿足上述authority，必須進入`BLOCKED_AUTHORITY`並`CONTACT PM`；implementation、fixture或evaluator不得自行改寫Oracle semantics。

## 0.3 Frozen F1～F8 Ledger

本SPEC逐字保留下列approved option tuple，不重新比較alternatives：

| Decision | Frozen tuple | Contract coverage |
|---|---|---|
| F1 | B / C / C / C | Oracle identity、revision、admission、pinning |
| F2 | C / B / C / C | Execution／run／judgment identity與external ledger |
| F3 | B / B / B / B | RCA、claim、calibration decomposition與adjudication |
| F4 | B / C / D / B | immutable original verdict、append-only authorized re-adjudication |
| F5 | B / C / C / B | repeated-run denominator、aggregation與invalid／failed disposition |
| F6 | C / C / B / B | E2E latency、publication endpoint與refresh measurement |
| F7 | B / B / B / C | isolation guards、real execution gates與reproducibility |
| F8 | B / B / C / C | closure states、evidence package與readiness reporting |

Consolidated reconciliation為：

```text
PASS — F1～F8 RECONCILED; READY FOR SPEC-017 DRAFT
Authority Gap = NONE
Production Contract Gap = NONE
Evaluation Contract Gap = NONE
Upstream SPEC Change Required = NO
CONTACT PM = NO
```

本表是traceability，不授權用tuple字母重新推導或改變frozen semantics。

## 0.4 Repository Reality

目前Candidate A～E由SPEC-012～016實作；SPEC-011仍是唯一Runtime authority。Candidate F必須透過既有public semantic capabilities讀取：

- Incident及Incident-side RCA relationship；
- RCA Aggregate、Attempt／Try lineage、Version／Artifact、Current、Freshness及publication result；
- Evidence Snapshot／Revision、source status、completeness、Materiality及post-context facts；
- Knowledge Snapshot、typed resolution、applicability及provenance；
- Validated Generation Result、typed claims、ranked hypotheses、conclusion及generation provenance；
- Runtime公開的execution／recovery observations。

Repository中的具體methods可作implementation adapter依據，例如`get_rca_relationship`、`get_current`、`get_version_history`、`get_artifact_provenance`、`resolve_snapshot`、`resolve_revision`、`get_snapshot`及Candidate-D result read；這不授權Candidate F直接查詢任何private SQLite table。若某required observation尚無public read，implementation phase只能新增evaluation-side adapter或提出upstream public-read gap，不得繞過authority boundary。本SPEC approval未要求任何production change。

---

# 1. Scope and Non-goals

## 1.1 In Scope

- immutable Oracle Store及revision governance；
- two-stage Oracle admission與run pinning；
- S1～S6 evaluation-only fixtures的future schema；
- run／execution／judgment／review／re-adjudication identities；
- static及runtime Ground Truth isolation guard；
- Root Cause Identification、Unsupported Claim、Conclusion Calibration及Knowledge Boundary評分；
- deterministic machine checks與controlled human semantic adjudication；
- repeated-run denominator、aggregation及distribution；
- initial E2E、refresh、failure、degraded、restart／recovery evaluation；
- real LLM、real RAG及Docker gates；
- reproducibility manifest、report及immutable evidence package。

## 1.2 Non-goals

Candidate F不得：

- 修改Event、Incident、Evidence、Knowledge、RCA或Runtime production semantics；
- 建立第二套Runtime、scheduler、retry budget、Runtime Clock或recovery framework；
- direct-read或write private SQLite／Chroma internals；
- 將`scenario_id`、`evaluation_run_id`或Oracle identity寫入production entities；
- 將Oracle、expected answer、Ground Truth或judgment送入prompt、selector、query、retrieval、generation或orchestration；
- 以evaluation verdict替換、刪除或重排Current RCA；
- reset／truncate shared或default stores；
- 以fake provider／fake RAG宣稱real gate PASS；
- 用LLM judge產生或修改Oracle；
- 為benchmark通過而修改production behavior。

---

# 2. Oracle Governance

## 2.1 Authority

Oracle Approval Authority與Oracle Amendment Authority皆為`林子豪（PM）`。SPEC Lead可audit repository、draft、formalize schema、進行semantic review並提出amendment proposal，但不得核准會改變verdict semantics的revision。

## 2.2 Oracle Identity and Revision

Logical model至少包含：

```text
OracleSet
  oracle_set_id
  scope = S1..S6

OracleRevision
  oracle_revision_id
  parent_revision_id?
  schema_version
  semantic_content_commitment
  created_at
  status
  approval_authority
  approval_reference
  approved_at?
```

`oracle_revision_id`必須由canonical semantic content及schema identity deterministically commitment，且與human-readable version分離。Approved revision immutable；修正只能建立new revision，不得in-place edit。

## 2.3 Two-stage Admission and Pinning

Stage 1 `ORACLE_REVIEW_ADMITTED`驗證：schema完整、S1～S6皆存在、O1～O10齊備、cross-scenario rules存在、content commitment一致、PM approval reference有效。

Stage 2 `EVALUATION_PLAN_ADMITTED`將exact Oracle Revision連同fixture revision、production build、model、prompt、schema、corpus／index、configuration及`N`凍結成Evaluation Plan。只有完成兩階段admission者可開始正式run。

Run開始後不得切換Oracle Revision。任何revision change必須建立new Evaluation Plan及new run；不得把舊run部分execution搬入新run。

## 2.4 Official Re-adjudication

Official re-adjudication為`YES — CONDITION`：

1. original judgment與original Oracle Revision永久保留；
2. re-adjudication只新增append-only judgment；
3. new record必須含run identity、original judgment reference、original與new Oracle Revision、reason、authorization reference及result；
4. historical report同時呈現original-time judgment與later authorized judgment；
5. re-adjudication不得retroactively改寫run aggregate；如需依新revision比較，須產生獨立derived view並明示其basis；
6. production永遠不得讀取任何judgment或re-adjudication result。

未有PM authorization reference時，review只能標記`REVIEW_NOTE`，不能形成official re-adjudication。

---

# 3. Evaluation Identity and External Ledger

## 3.1 Identity Domains

| Identity | Meaning | Forbidden equivalence |
|---|---|---|
| `evaluation_plan_id` | pre-run admitted immutable plan | 不等於Runtime work／Attempt |
| `evaluation_run_id` | 一次依plan執行的campaign | 不寫入production |
| `execution_id` | scenario × repetition的一次scheduled execution | 不等於Attempt／Try |
| `observation_id` | 一次public semantic observation bundle | 不成為domain receipt |
| `judgment_id` | 對一個execution的original verdict | 不等於RCA Version |
| `review_id` | reviewer application record | 不覆寫judgment |
| `re_adjudication_id` | authorized append-only later judgment | 不替代original |
| `report_id` | 一個immutable aggregate/report projection | 不成為production authority |

所有identity domains須有type discriminator。Stable identity不得依process PID、random restart value、filesystem order或telemetry sequence決定；允許在evaluation-only namespace使用collision-resistant nonce建立run，但其值不得跨入production call payload。

## 3.2 Evaluation Store

Oracle、fixtures、plans、observations、judgments及reports只存在於Candidate-F external evaluation store／artifact package。其path、schema、credential及lifecycle須與production Event／Incident／Evidence／Knowledge／RCA／Runtime stores物理或權限隔離。

Evaluation Store只能保存production public output的copy／commitment作audit，不得被production component掛載為read source。Production store identity不得混入evaluation answer fields；evaluation ledger不得取得production write capability。

## 3.3 Public Mapping

Mapping以opaque production identities建立evaluation-side關聯：

```text
execution_id
→ observed incident_id
→ aggregate_id / attempt_id / version_id
→ evidence_snapshot_id / revision_id
→ knowledge_snapshot_id / validated_result_id
→ public publication / Current facts
```

`scenario_id`只存在mapping左側。任何production identity不存在、ambiguous或public read不可靠時，execution不得猜測mapping，應分類為`INVALID_OBSERVATION`或`SYSTEM_FAILURE`並保留原因。

---

# 4. Ground Truth Leakage Prevention

## 4.1 Static Guard

Implementation phase必須建立allowlist-based scan，覆蓋production prompt templates、Evidence selector/query construction、Knowledge canonical query／filters、Candidate-D input projection、Candidate-E orchestration payload及production persistence schema。至少拒絕：

```text
scenario_id
evaluation_run_id
expected_causal_class
expected_root_cause
accepted_alternative
forbidden_overclaim
oracle_revision_id
ground_truth
re_adjudication_result
```

僅改名、hash或encode answer content仍屬leakage。Static guard發現疑似alias時須fail closed並人工確認。

## 4.2 Runtime Guard

每次formal execution須在provider invocation前取得sanitized outbound manifest／commitment，證明prompt只由approved prompt、exact Evidence Snapshot、exact Knowledge Snapshot及non-secret config形成；retrieval query／selector manifest亦須證明只來自approved production facts。

Runtime guard對任何evaluation-only identifier或answer commitment命中時：

- 阻止該execution；
- verdict為`INVALID_ISOLATION_BREACH`；
- 不重試為正常execution；
- 保存sanitized finding，不保存secret或完整provider payload；
- formal run不可宣稱valid PASS。

## 4.3 Negative Canaries

Evaluation package須包含不對應任何production field的canary commitments。它們只用來證明evaluation data沒有進入production outbound／persistence，不得改變scenario diagnosis或query。Canary出現在production observation即為isolation failure。

---

# 5. Judgment Model

## 5.1 Root Cause Identification

每個完成且可評分的execution至少產生：

```text
canonical_causal_class_match
approved_accepted_alternative_match
top_hypothesis_accepted
granularity_valid
required_evidence_satisfied
forbidden_overclaim_detected = NONE | NON_MATERIAL | MATERIAL
conclusion_calibration_valid
knowledge_boundary_valid
root_cause_identification_success
```

```text
top_hypothesis_accepted
= canonical_causal_class_match
  OR approved_accepted_alternative_match

root_cause_identification_success = PASS
IFF
  top_hypothesis_accepted
  AND granularity_valid
  AND required_evidence_satisfied
  AND forbidden_overclaim_detected != MATERIAL
  AND conclusion_calibration_valid
  AND knowledge_boundary_valid
```

`required_evidence_satisfied`在normal／full case依該Scenario O5判定；authorized degraded case依該Scenario O7及O10判定，不得要求不存在的full-case Evidence，也不得放寬其可接受Conclusion與causal granularity。`NOT_EVALUABLE`或任何invalid execution不得形成PASS。各predicate均為必要條件，不存在weighted score、加分抵銷、平均補償或其他compensation semantics。Exact prose或keyword hit皆非充分條件。

## 5.2 Atomic Claim Evaluation

Candidate-D typed claims是production-authoritative claim decomposition。任何production RCA中未被typed claims覆蓋的factual／causal assertion皆屬Candidate-D Claim Coverage contract violation；Candidate-F external ledger必須將該execution及其observed output標示為`INVALID_OUTPUT`或等價typed disposition，且Root Cause Identification不得PASS。此標示不得mutation production Artifact。Candidate F不得從prose補建production-authoritative claims。Evaluation-side deterministic parser可標記該assertion並進行補充audit／UCR analysis，但parser或human review不得修復、正常化或取代Candidate-D validation。

每個atomic factual／causal claim分類：

```text
GROUNDED
UNSUPPORTED
CONTRADICTED
NOT_APPLICABLE
```

- `GROUNDED`：exact public Evidence可解析且足以支撐該granularity；
- `UNSUPPORTED`：找不到足以支撐的Evidence，且未被明確反證；
- `CONTRADICTED`：authoritative Evidence與claim實質矛盾；
- `NOT_APPLICABLE`：uncertainty、limitation、純格式文字或明確`MODEL_SUGGESTED` guidance，並非Incident factual／causal assertion。

Knowledge reference不能單獨令Incident claim成為`GROUNDED`。

## 5.3 Unsupported Claim Rate

```text
execution_UCR
= count(UNSUPPORTED + CONTRADICTED factual/causal claims)
  / count(GROUNDED + UNSUPPORTED + CONTRADICTED factual/causal claims)
```

若denominator為0，結果為`NOT_EVALUABLE`，不得記為0%。Run同時報告：

- micro UCR：所有可評分atomic claims合併計算；
- macro UCR：各個denominator非零execution的UCR算術平均；
- `NOT_EVALUABLE` count及原因；
- `CONTRADICTED`獨立count，不得只藏在combined rate。

## 5.4 Conclusion Calibration

| Conclusion | PASS condition |
|---|---|
| `IDENTIFIED` | Evidence支持asserted operational causal granularity，且無major contradiction |
| `MOST_SUPPORTED` | 一個hypothesis領先，但完整causal chain或排除力不足 |
| `INCONCLUSIVE` | Evidence不足或合理competing hypotheses未排除 |

`DEGRADED`不自動FAIL，也不自動允許`IDENTIFIED`。Calibration必須同時看source status、omission／bounds、support／contradiction、Knowledge resolution及Oracle O7。

## 5.5 Knowledge Boundary

Nominal S1～S6 real-RAG皆預期`MATCH`，但`MATCH expected ≠ MATCH mandatory for RCA accuracy`。合法`NO_MATCH`：

- 不自動造成Root Cause FAIL；
- 必須形成Knowledge Gap／coverage fact；
- 禁止fabricated Knowledge reference或`SOP_BACKED`；
- guidance依production contract降為`MODEL_SUGGESTED`。

`RETRIEVAL_UNAVAILABLE`不得偽裝`NO_MATCH`。Knowledge只支援remediation、prevention、SOP-backed guidance或diagnosis context，不能單獨建立Incident truth。

---

# 6. Machine and Human Adjudication

## 6.1 Deterministic Machine Checks

Machine checks負責identity、schema、lineage、evidence reference resolution、required field、closed set、numeric aggregation、forbidden literal／structured overclaim、Knowledge authority及isolation guard。它們不得以Scenario ID直接選答案，也不得只做keyword matching。

## 6.2 Human Semantic Review

Human adjudication遵循rules-first + bounded human adjudication：先執行§6.1 deterministic machine checks，再只把無法由deterministic rules完成的bounded semantic questions送入下列兩階段。Reviewer不得新增accepted cause、修改Oracle或修復Candidate-D output。

### Stage 1 — Blind Review

Blind packet不得包含Scenario ID、scenario name、Oracle expected answer、accepted alternatives或其他answer-revealing metadata。Stage 1只處理不需知道expected causal answer的事項：claim decomposition、Evidence support、unsupported specificity／overclaim、claim-level contradiction／是否實質扭曲diagnosis，以及conclusion calibration；此處不推導或取代Candidate-B-owned Materiality。Stage 1不得形成official Root Cause verdict。

Stage 1需要human judgment的項目由至少兩位reviewer independently review；一致結果成為append-only Stage-1 review fact，分歧由第三位reviewer只裁決該Stage-1項目。Deterministic machine fact不因進入此流程而改由human重寫。

### Stage 2 — Oracle-applied Adjudication

Stage 2使用Evaluation Plan所pinned的exact Oracle Revision，處理causal-class match、approved accepted alternative、accepted granularity及Oracle-dependent final Root Cause judgment。Official judgment只能在Stage 2完整套用§5.1 final predicate後形成；Stage 1結果只是其不可覆寫的input facts。

Stage 2由至少兩位reviewer依同一pinned Oracle Revision independently adjudicate；一致時形成official judgment，分歧時由第三位reviewer依同一revision裁決。任何reviewer不得以自身偏好擴張Oracle。若任一required stage人力或facts不完整，run標記`REVIEW_INCOMPLETE`，不得宣稱formal quality PASS。

兩階段的reviewer identity、stage、timestamp、input commitment、Oracle Revision（Stage 2）、reason code、bounded rationale、agreement／disagreement及third-review結果都以append-only、auditable records保存；後續更正只能依§2.4新增authorized re-adjudication，不得覆寫原review facts或official judgment。

LLM judge可做非權威exploratory analysis，但不得產生official judgment、tie-break、Oracle amendment或PASS evidence。

---

# 7. PM-approved S1～S6 Oracle

本章是PM Handoff的schema formalization；不得由fixture或implementation重新推導。

## 7.1 S1 — Brute Force

- **O1 Expected Causal Class：** credential abuse／brute-force authentication attempt；同一來源對同一或可追蹤使用者，在bounded window持續大量未授權authentication attempts。
- **O2 Granularity：** 至少authentication／credential-abuse operation level。接受brute-force login、repeated unauthorized authentication、password guessing；`login anomaly`、`many 401s`、`authentication error`或`service error`過粗。不要求attacker identity、location、tool或infrastructure。
- **O3 Alternatives：** brute-force login attempts、repeated password guessing、common-source repeated unauthorized authentication。`credential stuffing`只有Evidence支持外洩credential set時才接受。
- **O4 Forbidden：** 無Evidence不得宣稱account compromised、password leaked、successful login、data breach、attacker identity/location、botnet/tool、credential stuffing、IdP或database failure。`Account Locked`是可能後果，不是root cause。
- **O5 Evidence：** normal/full至少有authoritative `brute_force_detected`、same `source_ip`、same／traceable `user_id`及bounded window repeated HTTP 401。Account Locked optional，Metrics不要求。只有label沒有pattern不足以支撐最強granularity。
- **O6 Knowledge：** nominal `MATCH expected`，category=`credential abuse / brute-force response`；合法`NO_MATCH`不直接使accuracy FAIL。
- **O7 Degraded：** raw logs部分缺失但remaining authoritative Evidence仍明確支持，可為`MOST_SUPPORTED`；更不足或competing explanation無法排除時`INCONCLUSIVE`合法。
- **O8 Unacceptable：** OOM、DB/network、external dependency、rate limiting、generic auth/service error、只靠Knowledge建立攻擊事實、無Evidence宣稱compromise／leak。
- **O9 Refresh：** 更多同類401、Account Locked或不改變diagnosis/ranking/conclusion的support不要求new version；outcome改變、meaningful contradiction或足以改變interpretation/ranking/conclusion者為Material Evidence。
- **O10 PASS：** Top hypothesis命中brute-force／credential-abuse，granularity與Evidence適當，alternative合法，無material overclaim，Conclusion calibration正確。

## 7.2 S2 — DB Slow Query / API Cascade

- **O1：** database slow-query／database-side latency造成upstream API timeout cascade；`core-db slowness → AP latency → Gateway timeout/504`。
- **O2：** 至少dependency + service-chain causal level；只說API latency、Gateway 504或DB problem過粗。SQL、table、index、plan或lock非必需。
- **O3：** slow database dependency、core-db latency causing cascade、DB delay propagating via AP to Gateway、DB performance degradation causing upstream failure；Evidence只支持dependency slowness時不強迫`slow query`字樣。
- **O4：** 無Evidence不得宣稱specific SQL/table/index、DB CPU、lock、pool、storage I/O或Gateway自身為root cause。
- **O5：** 至少authoritative `cross_service_failure`、shared trace/service chain、`downstream_service=core-db`、DB→AP→Gateway propagation及`high_latency_detected`／duration／compatible Metric。只有latency Metric無chain不足。
- **O6：** nominal `MATCH expected`，category=`database slow-query / API-timeout diagnosis`；`NO_MATCH`合法。
- **O7：** chain + core-db但部分latency缺失，可`MOST_SUPPORTED — database-side latency likely driving cascade`；不能判DB或AP先異常時可conservative shared-dependency diagnosis或`INCONCLUSIVE`。
- **O8：** Gateway primary、unsupported AP bug、external API、OOM、rate limit、只描述504、unsupported SQL/index/lock/pool皆FAIL。
- **O9：** 更多相同cascade/latency/504且semantics不變，不新建version；新slow-query evidence提升granularity、改變Top hypothesis或證明DB非主因則refresh。
- **O10：** Top hypothesis命中database-side slowness causing upstream API/service cascade，granularity適當、無unsupported deeper mechanism且calibration正確。

## 7.3 S3 — OOM Crash

- **O1：** memory exhaustion／Out-of-Memory causing application or service failure；不是單純high memory。
- **O2：** 至少affected service + memory exhaustion/OOM failure。不要求memory leak、object allocation、GC或container limit。
- **O3：** OutOfMemory failure、memory exhaustion causing crash、service exhausted memory and failed、OOM causing application failure；`memory leak`不預設等價。
- **O4：** 無Evidence不得宣稱memory/heap leak、GC thrashing、JVM heap config、Kubernetes limit、OOMKill、specific allocation或deployment defect；502不等於Gateway root cause。
- **O5：** normal/full至少authoritative `oom_crash_detected`、log `OutOfMemoryError`及`system_memory_usage_pct >= threshold`或`high_memory_detected`。宣稱OOM→crash/downstream failure另需failure Evidence。
- **O6：** nominal `MATCH expected`，category=`high-memory / OOM / service-crash handling`；`NO_MATCH`合法。
- **O7：** 明確OutOfMemoryError但Metric unavailable，可`MOST_SUPPORTED — OOM / memory exhaustion`；只有high memory則只可high-memory MOST_SUPPORTED或INCONCLUSIVE。
- **O8：** DB、external dependency、rate limit、auth attack、generic 5xx漏掉OOM、high memory直接等於leak、unsupported JVM/container mechanism皆FAIL。
- **O9：** 更多相同memory/OOM不產生version；新service crash、502、matching service或contradiction足以改變chain/conclusion則refresh。
- **O10：** Top hypothesis命中memory exhaustion/OOM causing service failure，granularity不超Evidence、無unsupported mechanism、calibration正確；full Evidence下只答high memory不足。

## 7.4 S4 — External API Failure

- **O1：** external／third-party dependency failure造成affected transaction或service request failure／timeout。
- **O2：** 至少external dependency + request/transaction failure；不要求provider internal server、region、DB或network component。
- **O3：** external API failure、third-party failure/outage、external timeout、provider server-side failure；只有5xx不強迫timeout，只有timeout不要求provider-wide outage。
- **O4：** 無Evidence不得宣稱region/global outage、DNS、TLS/cert、packet loss、rate limit、credential failure、specific crash或data breach。
- **O5：** 至少authoritative `external_dependency_failure`、clear `external_service`、`transaction_id`等lineage，以及status≥500、timeout/duration/log或compatible failure evidence。Metrics不要求；只有local 5xx不足。
- **O6：** nominal `MATCH expected`，category=`external / third-party API timeout or outage handling`；Knowledge不得建立outage fact。
- **O7：** identity + failure明確但mode不完整，可`MOST_SUPPORTED — external dependency failure`；internal fault未排除時可leading hypothesis或`INCONCLUSIVE`。
- **O8：** DB、OOM、rate limit、credential abuse、generic 500未辨識external dependency、unsupported DNS/TLS/global outage或以SOP common cause當truth皆FAIL。
- **O9：** 更多相同external 5xx/timeout不需new version；dependency恢復但local failure持續、internal contradiction或更強causal evidence改變interpretation時refresh。
- **O10：** Top hypothesis命中external/third-party dependency failure並達dependency + affected request/transaction level，不overclaim provider mechanism。

## 7.5 S5 — DB Network Cascade

- **O1：** shared database/network dependency connectivity failure causing multi-service downstream cascade；多個independent services/requests對同一`core-db`發生connectivity/ConnectionRefused。
- **O2：** 至少shared downstream dependency + connectivity failure + multi-service impact；不要求DB crash、firewall、route、NIC、switch或network segment。
- **O3：** shared DB connectivity failure、core-db connection refusal causing cross-service failures、shared DB/network dependency outage、common core-db unreachable。
- **O4：** 無Evidence不得宣稱DB crash/overload、pool exhaustion、firewall change、network partition、DNS、blocked port、storage、router/switch或individual deployment issue。
- **O5：** 至少authoritative `downstream_cascade_failure`、multiple `service_name`、same `downstream_service=core-db`、`error_type=ConnectionRefused`及multiple Trace IDs／independent requests。單一service不足。
- **O6：** nominal `MATCH expected`，category=`shared database / network dependency interruption handling`；Knowledge不得提升firewall/partition為Incident fact。
- **O7：** multi-service + core-db + ConnectionRefused明確但部分logs缺失，可`MOST_SUPPORTED`；common dependency未可靠確認時只可suspected shared dependency或`INCONCLUSIVE`。
- **O8：** 各service分開當root cause、S2 latency、OOM、rate limit、external API、generic multi-service error未辨識shared core-db、unsupported exact mechanism皆FAIL。
- **O9：** 更多相同service→core-db→ConnectionRefused且diagnosis不變通常不新建version；出現meaningful contradiction、找到更具體且受支持的connectivity mechanism，或改變remediation／hypothesis ranking／causal granularity時則refresh。
- **O10：** Top hypothesis必須保留shared core-db/database dependency、connectivity failure及multi-service cascade；`DB problem`不足。

## 7.6 S6 — Rate Limit Storm / QPS Spike

- **O1：** request/QPS surge exceeding rate-limit/quota capacity，造成target service repeated HTTP 429 throttling。
- **O2：** 至少target service + request-volume spike + rate-limit/quota throttling；只說many 429、service busy或traffic high過粗。不要求campaign、bug、bot或attack。
- **O3：** QPS spike triggered rate limiting、request surge exceeded quota、traffic burst caused repeated 429、rate-limit saturation；不要求`storm`字樣。
- **O4：** 無Evidence不得宣稱DDoS、bot/malicious traffic、credential abuse、client retry loop、app bug、quota misconfiguration、capacity/autoscaling failure或third-party policy defect。
- **O5：** normal/full至少authoritative `rate_limit_storm`、same `target_service`、repeated 429、valid quota context、`request_spike_detected`或equivalent QPS evidence，且current顯著超過established baseline。只有429或只有QPS spike都不足完整causal class。
- **O6：** nominal `MATCH expected`，category=`HTTP 429 / rate-limit / QPS-spike handling`；Knowledge不得建立DDoS/bot/client bug/config defect。
- **O7：** 429 + target明確但QPS unavailable，只可`MOST_SUPPORTED — throttling occurring; surge cause not fully established`；只有QPS spike則request surge MOST_SUPPORTED或INCONCLUSIVE。
- **O8：** OOM、DB、external outage、credential abuse、只描述429未辨識rate-limit/request relationship、unsupported DDoS/bot/retry/config defect皆FAIL。
- **O9：** 更多相同429/QPS且causal interpretation不變時不產生version；若後續Evidence顯示traffic恢復後429消失、QPS回正常但429仍持續、出現meaningful contradiction，或出現another better-supported mechanism，並改變causal interpretation，則屬Material Evidence並refresh。
- **O10：** Top hypothesis保留target service、request/QPS surge、quota/throttling及repeated 429 relationship；只命中429不足。

## 7.7 Cross-scenario Disambiguation

```text
S1 credential abuse / brute-force authentication
S2 database latency → same request/service-chain timeout cascade
S3 memory exhaustion / OOM → service failure
S4 external dependency failure → affected request/transaction
S5 shared core-db connectivity → multiple independent services/requests fail
S6 request/QPS spike → quota throttling → repeated 429
```

尤其S2要求same trace/request propagation與latency；S5要求multiple independent traces/services與ConnectionRefused。`database problem`不得讓兩者都PASS。

---

# 8. Fixture and Execution Contract

## 8.1 Evaluation-only Fixture

每個fixture至少包含：`scenario_id`、fixture revision、input seed／generator config、signal definition、expected production observations、pinned Oracle scenario reference及isolation canary。Answer fields必須存於evaluation-only sidecar，不得存在production-consumable event/log/metric payload。

Fixture schema不得把Event label當唯一truth；Oracle O5所列pattern／lineage必須能由production Evidence capability觀察。Nominal、degraded、contradiction、post-context material/non-material、failure及restart variants需用不同fixture identity。

## 8.2 Execution Admission

一次execution只有在下列條件皆成立才可啟動：

- plan及Oracle已admitted/pinned；
- production build與config commitment相符；
- isolated clean evaluation namespace可用，且不是shared/default store；
- required real/fake gate明確；
- provider、credential profile、corpus/index及Docker readiness符合該gate；
- Ground Truth guards PASS；
- signal clock與observation endpoints ready。

Formal runs禁止中途修改model、temperature/config、prompt、schema、corpus/index、fixture或Oracle。發生drift時剩餘executions停止，run為`ABORTED_DRIFT`；已完成資料保留但不可拼接成另一plan。

## 8.3 Repeated-run N

`N`是每個nominal scenario獨立execution數，必須是正整數並於plan admission前固定。正式Competition plan的default proposal為`N=5` per S1～S6；若PM Final Competition Scope另有核准值，以其為準並建立新plan。不得因早期結果好壞改N或early stop。

每次repetition使用獨立evaluation namespace及新production business identities，但完全相同的pinned semantic configuration。Execution order須預先commit或以記錄seed randomized，避免手動挑選。

## 8.4 Denominators and Disposition

```text
planned_nominal_executions = 6 × N
RCA success denominator = all admitted scheduled nominal executions
```

`PASS`進success numerator；`RCA_FAIL`、`SYSTEM_FAILURE`、`TIMEOUT`、`INVALID_OUTPUT`、`INVALID_OBSERVATION`及`ISOLATION_BREACH`都留在denominator且不進numerator。`NOT_STARTED`只在整個run已正式aborted且有原因時另報，不得把它包裝成較小N。

Infrastructure-caused failure與diagnostic failure須分欄報告，但不得silent exclude。只有在run開始前由admission拒絕的plan不形成execution denominator。

Aggregation至少提供scenario-level count/rate、overall micro count/rate、各metric distribution及95% bootstrap interval；sample小時interval只作描述，不得取代raw counts。不得以平均文句分數取代binary Root Cause metric。

---

# 9. Latency Contract

## 9.1 Primary E2E Latency

```text
evaluation signal_started_at
→ first successfully published usable Current RCA observed through public semantics
```

Start由evaluation harness在外部記錄scenario signal正式可被production intake觀察的absolute UTC instant；不得以worker開始或LLM invocation縮短範圍。End必須同時證明Artifact durable、Incident authority反映Current，且public read能取得coherent usable Current。

Duration使用同一host/process可用的monotonic clock；UTC timestamps用於audit/correlation。若跨process，須記錄clock source與synchronization uncertainty。Post-context不阻塞primary latency。

## 9.2 Reporting

Primary latency只對成功到達endpoint者計算Median、p95、min/max及sample count；timeout/failure不得被當成慢值或刪除，須另外報failure/censor count與timeout bound。p95採nearest-rank deterministic definition。N不足以穩健估計時仍可列observed p95，但必須標示small-sample limitation。

Refresh latency從Material Evidence/public STALE obligation可觀察時點至new Current/FRESH publication。Restart recovery latency與primary/refresh分開，不得混算。

---

# 10. Failure、Degraded、Refresh and Restart Evaluation

## 10.1 Required Cases

至少驗證：

- Loki unavailable；
- Prometheus unavailable；
- RAG `NO_MATCH`；
- RAG unavailable／index mismatch；
- LLM transient failure；
- invalid structured output；
- refresh failure preserves last-known-good Current；
- authoritative integrity contradiction fails closed；
- restart at Evidence、Knowledge、generation、Artifact commit及publication boundaries。

## 10.2 Evaluation Rules

Degraded success必須有authoritative degraded finalization、可信remaining Evidence、明確omission及不超Evidence的Conclusion。`DEGRADED`本身既非PASS亦非FAIL；仍依Oracle O7及calibration評分。

`NO_MATCH`是合法Knowledge Gap；`RETRIEVAL_UNAVAILABLE`是failure/degraded path；`INVALID/REPAIR_REQUIRED`必須Fail Closed。不得互相改名。

LLM retry／recovery只能觀察SPEC-011／016既有budget與identity；Candidate F不得觸發額外retry。Failed Attempt不消耗Version number，restart不應duplicate Attempt／Try／Version／publication semantic effect。

## 10.3 Version and Refresh Assertions

必須覆蓋：

```text
v1 → Material Evidence → v1 STALE → v2 CURRENT/FRESH
failed refresh → v1 remains readable Current/STale
post-context NON_MATERIAL → no new Attempt/Version
failed Attempt → no Version allocation
restart/replay → no duplicate Attempt/Version/publication
```

Oracle各scenario O9決定什麼語意變化應導致refresh；單純資料量增加不成立。Evaluation只能觀察B-owned Materiality、A-owned freshness/version及E/Runtime protocol outcome，不得自行set STALE或publish v2。

---

# 11. Real Execution Gates

## 11.1 Gate Classes

| Gate | Required truth | What it may claim |
|---|---|---|
| Deterministic contract | fake adapters/providers permitted | schema、identity、guard、aggregation及failure protocol |
| Real LLM | actual approved Gemini adapter/model/profile invocation | real generation behavior only |
| Real RAG | actual governed corpus、real embedding provider、validated Active/LKG Chroma build及retrieval | real retrieval behavior only |
| Docker E2E | production-like Docker services/volumes and Runtime path | Docker execution behavior only |
| Formal Competition | Real LLM + Real RAG + Docker + pinned Oracle/plan + repeated N + completed review | Candidate-F formal quality result |

一個gate的PASS不得代替另一個。Fake provider、mock embedding、synthetic in-memory retrieval或host-only run不得宣稱Real LLM／Real RAG／Docker／Formal Competition PASS。

## 11.2 Gate Evidence

每個real gate至少保存non-secret provider/model/profile identity、prompt/schema/config commitment、corpus manifest、build/index/embedding identity、Docker image/config digest、start/end、invocation count、typed failures及sanitized receipts。Secret、raw authorization、unbounded prompt/response不得進package。

AC-014-X目前`NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE`且不是PASS；正式Competition若依scope要求team rebuild，必須取得新實際evidence，不能引用skip disposition。

---

# 12. Reproducibility Manifest

每個Evaluation Plan與Run必須產生immutable canonical manifest，至少包含：

- repository commit、dirty-state flag、branch informational value；
- Oracle set/revision/content commitment及approval reference；
- fixture schema/revisions/content commitments；
- scenario execution order及random seed；
- `N`、timeouts、denominator與aggregation policy versions；
- production Event/Incident/Evidence/Knowledge/RCA/Runtime contract versions；
- model/provider/profile、prompt、result schema及generation config；
- Knowledge corpus manifest、document revisions、embedding model、build/index及retrieval/applicability profile；
- Docker compose/config/image digests及isolated volume namespace；
- non-secret environment/tool versions與clock facts；
- reviewer protocol、reviewer identities及adjudication policy；
- public observation adapter version及report generator version。

任何required commitment缺失使run為`NON_REPRODUCIBLE`，不得宣稱formal PASS。Credential values及secrets只記availability/profile identity，不記內容。

---

# 13. Reporting and Evidence Package

## 13.1 Required Report

Report至少包含：

- status與gate class；
- admitted plan/run/Oracle identities；
- S1～S6 × repetition execution matrix；
- Root Cause success raw counts/rates與component fields；
- Unsupported Claim micro/macro rates、claim counts及contradicted count；
- Conclusion Calibration matrix；
- Knowledge MATCH/NO_MATCH/unavailable distribution與Knowledge Gaps；
- FULL/DEGRADED/failure distribution；
- Median/p95 latency、sample及censor/failure count；
- refresh/version/restart case results；
- isolation guard results；
- deviations、invalids、timeouts、aborts及review incompleteness；
- original judgments及authorized re-adjudication view明確分離。

## 13.2 Evidence Package

Evidence package append-only保存manifest、Oracle commitment、fixture commitments、sanitized execution logs、public observation bundles／commitments、machine findings、review records、judgments、aggregation inputs及generated report commitment。

Package不得保存secret、raw authorization、production write capability、private DB dump或不受控provider payload。Production source facts以public immutable projection或bounded copy保存；copy必須保留source identity與commitment。

## 13.3 Status Vocabulary

```text
DRAFT
READY_FOR_READ_ONLY_REVIEW
APPROVED_FOR_IMPLEMENTATION
IMPLEMENTED_NOT_EXECUTED
EXECUTED_WITH_FINDINGS
FORMAL_EVALUATION_PASS
FORMAL_EVALUATION_FAIL
BLOCKED_AUTHORITY
BLOCKED_CONTRACT
```

本文件目前為`Approved — Implementation Pending`。沒有implementation evidence不得升為Implemented；沒有real gates、repeated N、complete review及report不得宣稱Formal Evaluation PASS；任何Candidate-F結果都不自動等於Production Ready。

---

# 14. Failure and Recovery Semantics

Evaluation runner crash後只可從external ledger及production public facts恢復。它必須：

1. 保留已建立的run/execution identity；
2. 先讀existing observation/judgment，再決定resume；
3. 不重新觸發已可能產生production side effect的signal；
4. 不把ambiguous state當clean absence；
5. 無法可靠判斷時將execution標為`RECOVERY_AMBIGUOUS`並納入failure disposition；
6. 不重置production或shared stores；
7. 不呼叫production repair、retry或publication mutation以「完成評估」。

Evaluation ledger的equivalent write replay回same record；same identity不同content為typed conflict並Fail Closed。Original judgment、manifest及raw aggregate inputs不得被report regeneration覆寫。

---

# 15. Acceptance Criteria

## AC-017-A — Authority and Scope

Candidate F只observe/map/grade/aggregate/report；沒有production truth或mutation authority。SPEC approval不表示Candidate F已Implemented或任何Evaluation已Executed。

## AC-017-B — Frozen Decision Traceability

F1～F8 tuple及reconciled baseline完整保存，沒有重新選擇alternatives。

## AC-017-C — Immutable Oracle Revision

Oracle revision有deterministic commitment、PM approval reference、immutable admission及run pinning；in-place modification被拒絕。

## AC-017-D — Append-only Re-adjudication

Original judgment/revision永久保留；只有具authorization reference的new revision可新增official re-adjudication，且report區分兩者。

## AC-017-E — Identity Isolation

Plan、run、execution、observation、judgment及report identities互不混用，且不寫入production entities或替代Attempt／Try／Version／publication identity。

## AC-017-F — No Private Store Access

所有production observations經public semantic capabilities；private SQLite/Chroma direct read/write及destructive reset被禁止。

## AC-017-G — Ground Truth Isolation

Static/runtime guards證明Oracle、fixture answer、Scenario mapping、run identity及re-adjudication未進prompt、Evidence selector、Knowledge query、generation、orchestration或production persistence。

## AC-017-H — Root Cause Semantics

Scoring不是exact prose/keyword matching，且依§5.1無weighted compensation的final predicate獨立記錄causal class／approved alternative、granularity、required Evidence、overclaim、calibration與Knowledge boundary；`NOT_EVALUABLE`、invalid execution及uncovered production factual／causal assertion不得形成PASS。

## AC-017-I — S1～S6 Fidelity

每個scenario O1～O10及cross-scenario disambiguation忠實反映PM Handoff，沒有以Event label或Knowledge filename取代causal judgment。

## AC-017-J — Unsupported Claim Rate

Atomic claims可重現地分類，公式、zero denominator、micro/macro及contradiction reporting符合本SPEC；uncertainty與MODEL_SUGGESTED不被誤算。

## AC-017-K — Calibration and Knowledge

IDENTIFIED/MOST_SUPPORTED/INCONCLUSIVE依Evidence strength、contradiction與completeness評分；合法NO_MATCH不自動使RCA FAIL，Knowledge不建立Incident truth。

## AC-017-L — Human Adjudication

Semantic judgment依§6.2拆成不得形成official Root Cause verdict的Stage-1 Blind Review，以及使用exact pinned Oracle Revision形成official judgment的Stage-2 Oracle-applied Adjudication。兩個stage各自採independent two-reviewer agreement及third-reviewer disagreement handling；所有facts append-only／auditable，LLM judge無official authority。

## AC-017-M — Repeated-run Integrity

N在run前固定、不得early stop；所有admitted scheduled executions留在denominator，failure/invalid/timeout不得silent exclude。

## AC-017-N — Latency

Latency從signal started到publicly readable usable Current，報N/Median/p95與censor/failure；post-context不阻塞primary latency。

## AC-017-O — Degraded and Failure Cases

Required Loki/Prometheus/RAG/LLM/integrity cases依typed production semantics評估；DEGRADED不是自動PASS/FAIL，invalid authority不能降級。

## AC-017-P — Refresh and Version Cases

Material Evidence形成STALE→new Current；non-material post-context不產生Version；failed refresh保留Current；failed Attempt與restart不造成version/identity duplication。

## AC-017-Q — Singular Runtime

Evaluation不建立scheduler/retry/recovery，且不改變SPEC-011/016 timing、budget、identity或winner semantics。

## AC-017-R — Real Gate Honesty

Fake與real gates清楚分離；Formal Competition PASS要求real LLM、real RAG、Docker、pinned repeated plan及complete human review都有evidence。

## AC-017-S — Reproducibility

Manifest固定repository、Oracle、fixtures、model/prompt/schema/config、corpus/index、Docker、N、order、clock及review protocol；drift造成abort而非silent continuation。

## AC-017-T — Reporting and Recovery

Report可追溯至append-only evidence package；runner restart不重送ambiguous production signal、不覆寫judgment、不reset stores。

---

# 16. Verification Strategy（Future Implementation Phase）

本SPEC approval不實作或執行以下驗證；後續phase至少需要：

1. **Schema/unit tests**：Oracle canonicalization、identity domains、two-stage admission、pinning、append-only conflict及aggregation公式。
2. **Isolation tests**：forbidden field/alias、encoded answer、prompt/query/selector/persistence canary與write-capability denial。
3. **Scoring golden tests**：每個S1～S6的canonical／accepted-alternative predicate、too-coarse answer、normal／degraded required Evidence、wrong scenario、overclaim、Knowledge boundary、NOT_EVALUABLE、invalid／uncovered-output、NO_MATCH及zero-claim cases，並驗證不存在weighted compensation。
4. **Human protocol tests**：Stage-1 blind packet不得含Oracle answer且不得形成official verdict、Stage-2 exact Oracle pinning、兩階段各自的two-reviewer agreement／third-reviewer conflict、append-only audit及missing reviewer disposition。
5. **Public-read contract tests**：not-found/unavailable/corrupt/contradictory分離；禁止private DB fallback。
6. **Runner replay/recovery tests**：crash before/after signal、observation、judgment、report；same identity contradiction Fail Closed。
7. **Repeated-run tests**：fixed N、precommitted order、failure denominator、no early stop、micro/macro及p95 determinism。
8. **Failure/degraded tests**：Loki、Prometheus、RAG、LLM、invalid output、refresh failure及integrity contradiction。
9. **Refresh/version tests**：Material/Non-material、STALE、v2 publication、last-known-good preservation、restart non-duplication。
10. **Opt-in real gates**：live Gemini、real embedding/Chroma retrieval、Docker Runtime E2E及formal repeated competition；各gate產生獨立evidence。

Deterministic tests不得被描述成real execution。任何future verification command、fixture、module、API或storage detail不得反向改變本SPEC及PM Oracle semantics。

---

# 17. Deferred Implementation Details

下列可在不改變frozen semantics下於implementation plan決定：evaluation store的physical engine/path、exact DTO/class/module名稱、canonical serialization library、report file format、bootstrap confidence interval implementation、review UI及artifact compression。

下列不是可自行決定的detail：Oracle verdict semantics、approval/amendment authority、official re-adjudication條件、denominator inclusion、Ground Truth isolation、production ownership、real gate honesty或S1～S6 O1～O10。需要改變其中任何一項時，必須`BLOCK — CONTACT PM`。

---

# 18. Approval Gate

SPEC-017 v1.0正式核准為Candidate-F Engineering Contract，並可進入implementation handoff planning。Approval不表示implementation已開始或完成，也不構成任何execution evidence；fixture implementation、verification及real LLM／real RAG／Docker execution仍須於後續phase依本SPEC完成。

```text
Version: 1.0
Status: Approved — Implementation Pending
Approval Date: 2026-10-02
Phase 3 Re-review: PASS
Open Findings: NONE
F1～F8: FROZEN / preserved
PM Oracle: preserved
Candidate F IMPLEMENTED: NO
Evaluation Executed: NO
Real LLM Gate: NOT EXECUTED
Real RAG Gate: NOT EXECUTED
Docker Candidate-F Gate: NOT EXECUTED
Product Acceptance Complete: NO
Production Ready: NO
```
