# SPEC-014 — RAG Knowledge Index & Retrieval

## Software Design Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-014 |
| Document Name | RAG Knowledge Index & Retrieval |
| Version | 1.0 |
| Status | Approved — Implementation Pending |
| Approval Date | 2026-09-25 |
| Requirement Authority | PRD-004 v1.0 Approved |
| Runtime Authority | SPEC-011 v1.1 |
| Related Incident Contract | SPEC-008 v1.2；RCA integration implementation pending |
| Implementation Owner | 富裕 |

### Change History

| Version | Date | Status | Change |
|---|---|---|---|
| 0.1 | 2026-09-25 | Draft | 將Phase 0 Repository Reality Audit及Frozen D1～D4 formalize為Candidate C Engineering Contract；未進行implementation。 |
| 1.0 | 2026-09-25 | Approved — Implementation Pending | Phase 3 Re-review PASS；D1～D4 Engineering Contract frozen；F-014-01～F-014-06全部closed；正式核准進入implementation planning，implementation尚未開始。 |

### Status Honesty

> **Draft ≠ Approved；Approved ≠ Implemented。**

**SPEC-014 Engineering Contract已核准，可進入implementation planning；Candidate C production implementation尚未完成。** Repository仍無Candidate C production implementation；本文件不表示Corpus、Embedding、Chroma Index、Retrieval、Knowledge Snapshot、team rebuild或RCA integration已實作、驗證或可供production使用，亦不表示RCA E2E complete或Production Ready。

---

# 0. Authority、Purpose與Frozen Decisions

## 0.1 核心責任句

> **Candidate C governs approved Knowledge, freezes one retrieval boundary, and preserves immutable retrieval truth. Runtime decides when recovery executes; RCA domains decide how that truth is consumed.**

Candidate C擁有Knowledge Corpus、Index、Retrieval、Applicability、Knowledge Gap、Knowledge Snapshot及其local capability truth。Candidate C不得成為Evidence、RCA、Incident、Runtime或Evaluation authority。

## 0.2 Authority Hierarchy

```text
PRD-004 v1.0 Approved
>
Approved active upstream SPECs
>
Frozen SPEC-014 D1～D4
>
repository reality
>
DDS / README / runtime docs
```

| Domain | Authority | SPEC-014 Boundary |
|---|---|---|
| RCA product requirements | PRD-004 v1.0 Approved | Candidate C實作其Knowledge semantics，不改寫Evidence、RCA或Runtime authority |
| Incident relationship | SPEC-008 v1.2 | Candidate C不得直接寫Incident persistence或`rca_ref` |
| Runtime | SPEC-011 v1.1 | SPEC-011唯一擁有WHEN、ORDER、retry timing／budget、Runtime Clock、recovery execution與Startup Recovery |
| Scenario／validation | SPEC-005 | 只屬test／evaluation；不得成為production Knowledge authority |
| Candidate A／B | Current baseline無active SPEC-012／013 | 本SPEC不猜測其DTO、class、method或persistence |
| Candidate D／E | Future downstream | 只凍結Candidate-C-owned semantic ports及opaque references |

DDS、README及runtime docs只提供supporting context，不得覆蓋上述authority。

## 0.3 Purpose

本SPEC定義：

- approved Knowledge Corpus及canonical governed manifest；
- canonical document／version、deterministic chunk及compatibility-bound build identity；
- fail-closed pre-build admission；
- immutable staged build、validation、explicit activation及single durable Last-Known-Good authority；
- independent Retrieval Operation及one-time build pinning；
- versioned、bounded、deterministic retrieval及applicability；
- typed Knowledge Resolution及Knowledge Gap；
- exactly one immutable durable Knowledge Snapshot；
- local integrity、readiness、replay、concurrency及recovery facts；
- team-local reproducible rebuild及public semantic boundaries。

本SPEC不定義LLM prompt、RCA conclusion、Evidence materiality、RCA publication或Runtime scheduling。

## 0.4 Frozen D1～D4 Engineering Decisions

| Decision | Frozen normative contract |
|---|---|
| D1-1 | Canonical Governed Manifest Admission |
| D1-2 | Canonical Document／Version Identity + Deterministic Chunk Identity + Compatibility-bound Build Identity |
| D1-3 | Fail-Closed Pre-Build Knowledge Admission |
| D2-1 | Immutable Staged Build → Validate → Explicit Activate |
| D2-2 | Single Durable Activation Authority |
| D2-3 | Authoritative LKG Recovery + Fail-Closed Ambiguity |
| D3-1 | Independent Retrieval Operation + One-time Frozen Build Boundary |
| D3-2 | Versioned Bounded Deterministic Retrieval Profile |
| D3-3 | Typed Knowledge Resolution + Versioned Deterministic Applicability Policy |
| D4-1 | Exactly One Immutable Durable Knowledge Snapshot |
| D4-2 | Candidate-C Local Capability Truth + External Runtime Recovery Authority |
| D4-3 | Candidate-C-owned Public Semantic Ports + Opaque Cross-domain References |

這些決策已Frozen。本SPEC只formalize，不提供incompatible alternative。

## 0.5 Critical Invariants

1. Build existence不等於Active authority。
2. 只有validation成功且explicit activation成功的build可成為Active及LKG。
3. Failed／partial rebuild不得destroy、replace或silent deactivate current LKG。
4. Restart不得以timestamp、filesystem order、newest file或highest-looking ID猜Active winner。
5. 一個Retrieval Operation只freeze build boundary一次。
6. `Retrieval Operation ≠ Knowledge Snapshot ≠ RCA Attempt ≠ Runtime Work`。
7. 成功完成的logical retrieval exactly產生一份immutable durable Knowledge Snapshot。
8. `NO_MATCH`是合法Knowledge result，不是failure。
9. `RETRIEVAL_UNAVAILABLE`不是`NO_MATCH`。
10. Similarity score不是automatic applicability authority。
11. Knowledge applicability不是Incident factual root-cause evidence。
12. Candidate C不得實作scheduler、retry timing／budget、Runtime Clock或Startup Recovery authority。
13. Scenario、Ground Truth及validator expected answers不得成為production Corpus、query selector、applicability或retrieval answer authority。

---

# 1. Responsibility Boundary

## 1.1 Candidate C Owns

- approved Knowledge Corpus及manifest admission；
- document／version／chunk identity；
- content hash、chunking及embedding／index provenance；
- immutable staged build及validation truth；
- activation authority及single LKG；
- Retrieval Operation identity及frozen build binding；
- canonical bounded query validation；
- deterministic retrieval profile、ordering及filters；
- deterministic applicability policy；
- `MATCH`、`NO_MATCH`、`RETRIEVAL_UNAVAILABLE`及`INVALID / REPAIR_REQUIRED` resolution；
- Knowledge Gap；
- Knowledge Snapshot identity、content、lineage及durability；
- Candidate-C local integrity／readiness／recovery facts；
- team-local rebuild semantics及drift detection；
- Candidate-C public semantic reads。

## 1.2 Candidate C Does Not Own

- Evidence Snapshot、Evidence Revision或Materiality；
- RCA Aggregate、Generation Attempt、Published Version、Current、Fresh／Stale或publication；
- LLM generation、grounding conclusion或Diagnostic Conclusion；
- Incident persistence、`rca_status`或`rca_ref` mutation；
- Runtime work、scheduling、retry timing／budget、Runtime Clock或Startup Recovery；
- Scenario／Evaluation Ground Truth；
- shared credential registry、secret storage／resolution、key selection或Credential Profile lifecycle authority；
- automatic remediation或Knowledge Improvement Candidate approval workflow。

## 1.3 Runtime Separation

Candidate C可回報operation state、local capability truth及typed retry／repair disposition，但不得決定何時再執行。SPEC-011 Runtime依domain disposition執行durable scheduling及recovery。Candidate C不得建立第二套scheduler、retry ledger、wake-time authority或startup barrier。

---

# 2. Terminology與Identity Model

| Term | Normative meaning |
|---|---|
| Governed Manifest | production Corpus admission的唯一canonical declaration，列出approved sources、identity、version、hash、status及security metadata |
| Document Identity | 跨build穩定識別同一logical approved document的canonical identity |
| Document Version | 識別該document一份immutable approved content revision的canonical version |
| Chunk Identity | 由document identity／version、canonical chunk boundary及content commitment deterministic產生的identity |
| Compatibility Profile | 會影響index compatibility或retrieval interpretation的versioned configuration集合 |
| Build Identity | 對manifest content、chunking、embedding、index schema及compatibility profile作canonical commitment的immutable identity |
| Staged Build | 已完整產生但尚未取得Active authority的immutable build |
| Validated Build | 通過本SPEC全部required validation、仍未必Active的staged build |
| Active Build | single durable activation authority目前唯一指定的validated build |
| Last-Known-Good | 最近一次成功explicit activate且仍可由authority完整解析的build |
| Retrieval Operation | Candidate C獨立logical retrieval identity；一次freeze build及query boundary |
| Retrieval Profile | versioned bounded query、filter、top-k、score interpretation及ordering contract |
| Applicability Policy | versioned deterministic規則，將retrieved candidates及approved metadata解析為applicability |
| Knowledge Resolution | Retrieval Operation的typed semantic result |
| Knowledge Gap | `NO_MATCH`所表達的合法狀態：目前frozen boundary內沒有適用approved knowledge |
| Knowledge Snapshot | 一個Retrieval Operation的唯一immutable durable結果及完整lineage |
| Local Capability Truth | Candidate C對manifest、build、activation、index、snapshot及integrity的authoritative local facts |

所有durable identity必須有明確namespace／type discriminator及schema version，禁止靠字串外觀將不同identity互換。Exact UUID、hash encoding或storage key格式留Implementation Phase。

---

# 3. Corpus與Governed Manifest — D1-1、D1-3

## 3.1 Manifest Is Admission Authority

Runtime production Corpus只包含current governed manifest明確admit的approved SOP／operational knowledge。目錄存在、檔案存在、Git tracked、檔名含`sop`、index中已有vector或曾被舊build使用，均不構成admission authority。

Manifest至少須canonical表達：

- manifest identity及schema version；
- corpus identity及corpus version；
- document identity及document version；
- immutable source locator或repository-relative governed reference；
- expected content hash；
- approval／admission status及governance reference；
- effective／retired state；
- allowed knowledge classification及outbound eligibility；
- declared media／content type；
- required metadata及其canonical serialization version。

Manifest本身必須可canonical serialize及hash。相同semantic manifest在四名成員環境必須產生相同manifest commitment。

## 3.2 Admission Rules

Pre-build admission必須完整enumerate manifest entries並逐項驗證：

1. manifest schema及closed-set fields合法；
2. document identity／version唯一且未衝突；
3. source存在、可讀且位於approved source boundary；
4. actual content hash等於manifest commitment；
5. approval及active status合法；
6. content type受支援；
7. security classification允許進入該build／embedding profile；
8. source不是Scenario、Ground Truth、validator expected answer、RCA output、Shadow、unreviewed Incident或LLM suggestion；
9. metadata不含credential或禁止outbound內容；
10. canonical identity及version不與另一內容重複。

任何required entry失敗，整個build admission Fail Closed。禁止silent skip、best effort corpus、partial success後activate或以舊vector掩蓋missing source。

## 3.3 Corpus Mutation Boundary

Runtime對Active Corpus及Active Index read-only。Candidate C build流程不得自動將下列內容寫入Corpus：

- Scenario／Ground Truth／validator output；
- Closed Incident或Resolution submission；
- Shadow／Blocked record；
- RCA conclusion、MODEL_SUGGESTED guidance或provider response；
- Knowledge Gap或Knowledge Improvement Candidate；
- arbitrary filesystem／prompt／log content。

Future human governance可產生新的approved manifest／document version；該行為形成new build input，不得in-place改寫historical build或Snapshot。

---

# 4. Document、Chunk與Build Identity — D1-2

## 4.1 Document／Version Identity

Document Identity在content revision間保持logical continuity；Document Version唯一對應一份approved immutable content。相同Document Identity＋Version不得解析出不同bytes、不同canonical text或不同content hash；若發生即為authoritative contradiction及`REPAIR_REQUIRED`。

Rename、path relocation或checkout time不得自行改變Document Version。Content改變必須產生new version及new manifest commitment，不得覆寫既有version。

## 4.2 Deterministic Chunk Identity

Chunking必須由versioned deterministic chunking profile執行。每個chunk identity至少commit：

- document identity及version；
- canonical section／boundary provenance；
- deterministic ordinal或等價stable boundary identity；
- canonical chunk content hash；
- chunking profile identity／version。

同一manifest、source bytes及chunking profile在compatible環境須產生相同ordered chunk identities及content commitments。Chunk identity不得依賴process memory、filesystem enumeration order、random seed缺省值或build timestamp。

## 4.3 Compatibility-bound Build Identity

Build Identity至少commit：

- manifest identity、version及content commitment；
- ordered admitted document／version commitments；
- chunking profile及canonicalization version；
- embedding provider、model及versioned embedding profile；
- embedding dimension及必要normalization semantics；
- index engine／schema compatibility identity；
- retrieval-relevant metadata schema；
- build contract／serialization version。

任何compatibility input不同都不得冒充同一Build Identity。Build timestamp、builder hostname、credential value及personal path不得進入semantic identity。

---

# 5. Staged Build與Validation — D2-1

## 5.1 Build State Model

最低semantic state：

```text
ADMISSION_PENDING
→ BUILDING
→ STAGED
→ VALIDATED
→ eligible for explicit activation

any invalid path
→ FAILED or REPAIR_REQUIRED
```

State名稱及physical representation可調整，但不得省略「immutable staged」「validated」與「explicit activation」三個semantic gates。

## 5.2 Immutability

Build開始後，其Build Identity、manifest commitment、document／chunk set、embedding profile及index content不得in-place改寫。Retry若安全，只能完成同一declared immutable build或產生另一build；不得把partial output宣告為原build成功。

Build existence、STAGED或VALIDATED都不等於Active。

## 5.3 Required Validation

Activation前至少驗證：

- admission及manifest commitment完整；
- document／chunk identity deterministic且unique；
- expected與actual document／chunk count一致；
- embedding profile、dimension及model identity一致；
- every admitted chunk有且只有一個compatible index entry；
- index沒有unknown／orphan／duplicate chunk；
- metadata可完整resolve回manifest及source provenance；
- bounded probe retrieval可執行且返回contract-valid結果；
- index／metadata／build record integrity成功；
- security classification及outbound policy未被繞過；
- build可由其identity及metadata重新開啟，不依賴builder process memory。

Validation failure不得觸碰current activation authority或LKG。

---

# 6. Activation與Last-Known-Good — D2-2、D2-3

## 6.1 Single Durable Activation Authority

Candidate C必須有且只有一個durable activation authority，能authoritatively回答：

- current Active Build Identity；
- activation generation或等價monotonic conflict token；
- activation operation identity；
- activation所依據的validated build commitment；
- activation result及必要integrity metadata。

Directory symlink、Chroma collection listing、mtime、creation time、lexicographic filename或「唯一看起來完整的build」不得取代此authority。

## 6.2 Explicit Atomic Activation

Activation必須：

1. 接受明確指定的validated Build Identity及stable activation operation identity；
2. fresh-read current activation authority；
3. 驗證target仍完整且compatible；
4. 以atomic local semantic transaction切換authority；
5. commit後target同時成為Active及single LKG；
6. equivalent same-operation replay回傳相同result；
7. same identity不同target或stale conflicting request Fail Closed。

Activation不得修改build內容。沒有successful activation commit時，舊LKG持續authoritative。

## 6.3 LKG Recovery

Restart或component reopen必須先讀single durable activation authority，再解析exact referenced build並驗證integrity。合法結果只有：

- exact Active／LKG可完整解析：Candidate C可宣告retrieval-capable；
- 沒有任何曾activation的record：明確`NOT_INITIALIZED` local state；
- authority unreadable、multiple winner、target missing、identity mismatch或integrity failure：Fail Closed，`INVALID / REPAIR_REQUIRED`或capability unavailable。

禁止scan candidates並自動選最新／最大／唯一build。Operator-governed repair可恢復authority，但normal Runtime與Candidate C read path不得automatic destructive repair。

---

# 7. Embedding、Provider Capability與Index Provenance

## 7.1 Approved PoC Technology Baseline

PRD-004 v1.0的current Approved PoC technology direction為：

```text
Embedding baseline:   Google text-embedding-004
Vector store baseline: ChromaDB
```

Provider／model family substitution或vector-store technology substitution不得silent replacement，必須先經governance review。Adapter／class、SDK／client API、collection naming、filesystem layout、persistent directory naming及exact configuration key names不是本SPEC的Frozen implementation detail。

## 7.2 Shared Credential／Capability Boundary

Candidate C只消費approved shared non-secret Profile／capability reference。Candidate C不擁有credential registry、secret storage／resolution、key selection或Profile lifecycle authority，也不得猜測future shared-capability DTO、class或API。

每個需要external embedding／provider capability的logical build或Retrieval Operation必須：

- 驗證selected Profile具備required embedding／provider capability；
- 綁定selected non-secret Profile reference及必要capability identity；
- 對same logical operation及其retry／recovery維持Profile continuity；
- 禁止silent切換Profile、credential或key；
- 不保存、回傳或記錄credential secret。

Capability unavailable或Profile不符合要求時須產生typed failure facts，不得自行選另一Profile或key偽裝成功。

## 7.3 Provenance

每個build及Snapshot可解析的provenance至少包括：

- corpus／manifest identity、version及commitment；
- document／version／chunk identities及content hashes；
- chunking profile；
- embedding provider、model、profile及dimension；
- selected shared non-secret Credential Profile／capability reference（若適用）；
- index engine及schema compatibility identity；
- build identity及validation result reference；
- activation identity／generation；
- retrieval及applicability profile identities。

Credential secret、raw API key、authorization header及secret-bearing failure text不得進入build metadata、Snapshot、log或telemetry。Provider／model的silent substitution禁止；substitution是new compatibility input及new build，且須符合第7.1節governance review要求。

## 7.4 Provider Invocation Safety

每次external embedding／provider invocation都必須受bounded protection，至少涵蓋：

- configurable timeout；
- request size及batch bound；
- rate／quota protection；
- cost／resource bound；
- typed provider exhaustion／failure facts。

Exact numeric values留config及Implementation Phase。Candidate C只決定其domain failure facts及retry safety disposition，不擁有durable retry scheduling、retry timing或retry budget。Provider SDK hidden retry必須停用，或明確計入唯一Runtime-authorized retry／budget contract；不得形成第二套retry authority、暗中增加execution try或在recovery時重給budget。

---

# 8. Retrieval Operation與Build Pinning — D3-1

## 8.1 Independent Identity

每次logical retrieval須有Candidate-C-owned stable Retrieval Operation identity。它可保存opaque external references，但不得由RCA Attempt ID或Runtime Work ID冒充。

Retrieval Operation至少semantic綁定：

- operation identity及schema version；
- opaque caller／RCA Attempt reference（若提供）；
- opaque Evidence Snapshot／query-source reference（若提供）；
- canonical query commitment；
- retrieval profile及applicability policy identity；
- frozen Build Identity及activation observation；
- selected shared non-secret Profile／capability reference（若external provider capability適用）；
- resulting Knowledge Snapshot identity或terminal failure facts。

## 8.2 One-time Freeze

首次執行須fresh-read single activation authority，驗證Active build，並將exact Build Identity與operation atomically freeze。Freeze成功後：

- 同一operation所有execution tries、response-loss recovery及restart都使用same build；
- 後續activation不得改變已freeze operation；
- 若frozen build不可讀，不得切換new LKG，須回報`RETRIEVAL_UNAVAILABLE`或`INVALID / REPAIR_REQUIRED`；
- same operation提出不同query、profile、policy或external reference commitment屬contradictory replay，必須Fail Closed。

Freeze尚未commit前的crash可重新fresh-read Active並嘗試建立boundary；freeze commit後即不可改變。

---

# 9. Canonical Query與Deterministic Retrieval — D3-2

## 9.1 Query Contract

Candidate C public retrieval input必須是bounded、schema-versioned canonical query，不得接受任意filesystem path、raw Scenario answer或unbounded upstream payload。Query至少表達：

- normalized query content或content commitment；
- allowed structured context／filters；
- retrieval profile identity；
- applicability policy identity；
- opaque upstream references；
- explicit input bounds及canonicalization version。

Exact DTO與field names留Implementation Phase。

## 9.2 Versioned Retrieval Profile

Retrieval Profile至少凍結：

- query canonicalization及embedding profile；
- maximum input size；
- approved filter semantics；
- finite positive top-k／candidate bounds；
- score interpretation及precision handling；
- deterministic final ordering／tie-break；
- result content／metadata bounds；
- unsupported／empty query handling；
- profile version及compatibility rules。

Profile change是behavior change，必須有new identity／version，不得silent修改historical operation。

## 9.3 Deterministic Ordering

對固定Build、canonical query及profile，Candidate C必須產生canonical ordered candidates。Vector-store native return order不是final authority。Final ordering至少使用明確primary ranking及stable Chunk Identity tie-break；缺失、NaN、invalid或不可比較score不得被任意排序，必須按profile明確reject或classify。

Retrieved candidate數、rendered content、metadata及總payload均須bounded。Truncation只能依versioned profile deterministic執行並記錄provenance，不得silent截斷。

---

# 10. Applicability與Typed Knowledge Resolution — D3-3

## 10.1 Applicability

Applicability closed set：

```text
DIRECT
PARTIAL
CONTEXTUAL
NONE
```

Applicability須由versioned deterministic policy依approved metadata、scope constraints、query facts及retrieval signals計算。Similarity score可作candidate signal或policy input，但不得單獨自動取得applicability authority。LLM self-assessment不得成為v1 production applicability authority。

Applicability result必須保存policy identity、evaluated inputs、matched／rejected rules及必要score provenance。Applicability只表示approved knowledge對query context的適用程度，不證明此次Incident factual root cause。

## 10.2 Typed Resolution

Knowledge Resolution必須是下列互斥semantic variant：

| Resolution | Meaning | Required consequence |
|---|---|---|
| `MATCH` | 至少一個approved candidate經policy判定為`DIRECT`、`PARTIAL`或`CONTEXTUAL` | Snapshot保存bounded ordered applicable references及provenance |
| `NO_MATCH` | Retrieval正常完成但沒有applicable approved knowledge；applicability為`NONE` | 合法Knowledge Gap；不是retryable failure |
| `RETRIEVAL_UNAVAILABLE` | Frozen build、embedding／index capability或required retrieval dependency暫時不可用 | 不得偽裝`NO_MATCH`；transient failure提供typed facts，只有authoritative terminal finalization才建立unavailable Snapshot |
| `INVALID / REPAIR_REQUIRED` | manifest、identity、lineage、activation、index、Snapshot或replay contradiction | Fail Closed；不得automatic authority repair |

Expected domain failure必須structured，禁止以exception message文字猜resolution。Candidate C決定domain retry safety disposition；SPEC-011決定retry timing及budget。

## 10.3 Knowledge Gap

`NO_MATCH`本身就是完整、合法且可持久化的Knowledge result。其Snapshot必須保存frozen build、query／profile、searched bounds、filters、candidate／rejection provenance、applicability policy及`knowledge_gap=true`。不得為了避免Gap而放寬policy、切換build、加入unapproved content或呼叫LLM填補。

---

# 11. Knowledge Snapshot — D4-1

## 11.1 Cardinality與Identity

每個成功完成或經authoritative orchestration明確finalize為terminal degraded／unavailable的Retrieval Operation exactly對應一個immutable durable Knowledge Snapshot：

```text
one Retrieval Operation
→ zero Snapshot while incomplete or only experiencing transient retrieval/provider failure
→ exactly one Snapshot when logical retrieval completes as MATCH or NO_MATCH
→ exactly one Snapshot when authoritative orchestration explicitly finalizes RETRIEVAL_UNAVAILABLE
```

Transient retrieval／provider failure不得立即形成finalized unavailable Snapshot。Candidate C不得自行決定retry exhaustion、terminal degraded timing或Runtime scheduling。只有authoritative orchestration／Runtime boundary明確要求`finalize as terminal degraded / unavailable`時，Candidate C才可由既有frozen operation建立unavailable Snapshot。

`INVALID / REPAIR_REQUIRED`不得被降級或轉寫成unavailable Snapshot；它必須Fail Closed。若同一operation已有Snapshot，equivalent unavailable finalization replay回傳same Snapshot；與既有resolution、frozen build、Profile或provenance矛盾的finalization為`INVALID / REPAIR_REQUIRED`。

Snapshot identity與Retrieval Operation identity不同。Snapshot identity生成規則須stable、type-safe且可作opaque cross-domain reference；exact encoding不凍結。

## 11.2 Required Snapshot Content

Snapshot至少包含：

- Snapshot identity及schema version；
- Retrieval Operation identity；
- opaque RCA Attempt／Evidence／caller references（若提供）；
- frozen Build Identity及activation observation；
- manifest／corpus／embedding／index provenance；
- canonical query commitment；
- retrieval profile及applicability policy identities；
- typed `MATCH`、`NO_MATCH`或authoritatively finalized `RETRIEVAL_UNAVAILABLE` resolution；
- Knowledge Gap flag；
- ordered retrieved candidate及applicable chunk references；
- document／version／section／chunk identities及content commitments；
- bounded included content或可deterministic resolve的immutable content reference；
- scores、filters、ordering、truncation及rejection provenance；
- applicability result及rule provenance；
- source／integrity status；
- non-secret creation metadata。

Finalized unavailable Snapshot至少必須包含Snapshot identity、Retrieval Operation identity、frozen Build Identity、manifest／corpus lineage、selected non-secret Profile reference、`source_status=UNAVAILABLE`、`resolution=RETRIEVAL_UNAVAILABLE`、zero retrieved／applicable refs及required failure／finalization provenance。

Historical Snapshot不得因corpus更新、new build activation、document retirement或policy change而被retroactively rewrite。

## 11.3 Atomic Completion

Operation以`MATCH`、`NO_MATCH`或authoritatively finalized `RETRIEVAL_UNAVAILABLE`完成時，其terminal completion與unique Snapshot publication須是一個atomic local semantic transaction或具等價不可觀察partial success的protocol。不得出現：

- operation顯示successful但無法找到Snapshot；
- 同operation有兩份不同Snapshot；
- Snapshot存在但operation指向另一Snapshot；
- response loss後重建不同content的Snapshot。

Same-operation replay必須回傳既有Snapshot；不得重新retrieval取得新結果。

## 11.4 Referential-Integrity Retention Floor

Knowledge Snapshot及解析該Snapshot所必需的content／build lineage，在仍被下列authoritative reference使用期間必須保持readable及deterministically resolvable：

- published RCA；
- materially relevant failed Generation Attempt；
- outstanding Retrieval Operation；
- unfinished reconciliation／recovery obligation。

Build不再Active、new build activation、restart或age alone均不得破壞仍被引用的Snapshot、content、build或provenance lineage。Exact retention duration、archive mechanism、physical storage strategy及cleanup implementation留future configurable policy／Implementation Phase，但不得破壞此retention floor或referential integrity。

---

# 12. Persistence、Concurrency與Idempotency

## 12.1 Durable Store Requirements

Candidate C須使用獨立durable authority保存至少：manifest admission commitments、build／validation、activation／LKG、Retrieval Operation、Knowledge Snapshot及integrity metadata。Store須具有explicit schema version、authoritative reads、atomic local semantic transaction及fail-closed integrity checks。

Exact database、table、collection、filesystem layout及serialization library留Implementation Phase。Chroma或其他vector store只保存index capability；不得單獨取代activation、operation或Snapshot authority。

## 12.2 Idempotency

所有會建立durable authority的public mutation須使用stable operation identity。Equivalent replay回傳原result，不重複build、activation、operation或Snapshot。Same identity配不同semantic input為conflict，不得last-write-wins。

## 12.3 Concurrency

至少保證：

- concurrent builds互不修改彼此或Active build；
- concurrent activation對single authority序列化，最多一個requested transition成功；
- retrieval freeze與activation race以durable ordering決定，operation取得完整old或new build，不能混合；
- concurrent same-operation retrieval最多commit一份Snapshot；
- concurrent不同operations可安全共享同一immutable build；
- validation與activation race不得使unvalidated或post-validation mutated build成為Active。

禁止以process-local lock作唯一correctness guarantee。

---

# 13. Crash、Restart與Recovery — D2-3、D4-2

| Interruption point | Durable authority | Required recovery behavior |
|---|---|---|
| During admission／build | old activation／LKG + partial staged state | 隔離或標記failed；不影響LKG，不silent resume成validated |
| Build complete before validation | immutable staged build | 可按same build identity驗證；仍非Active |
| Validation success before activation | validated build | 保持eligible但inactive；不得restart自動activate |
| During activation before commit | old activation authority | old LKG仍authoritative |
| Activation commit, response lost | activation operation receipt／authority | same-operation replay回傳already activated result |
| Restart after activation | single activation authority | resolve exact target並驗證；不scan newest build |
| Before retrieval freeze commit | no frozen operation boundary | 可fresh-read current Active後建立boundary |
| Freeze commit, before index query | operation + frozen build | 使用same frozen build resume |
| Transient retrieval／provider failure | operation + frozen build + typed failure facts | 不建立finalized unavailable Snapshot；由SPEC-011依authoritative budget決定retry／recovery timing |
| Retrieval complete, before Snapshot commit | operation incomplete | 使用same frozen boundary安全re-execute；不得切換build |
| Authoritative terminal unavailable finalization | operation + frozen build + Runtime／orchestration finalization request | atomic建立exactly one unavailable Snapshot；不得re-query或切換build |
| Snapshot commit, response lost | operation + unique Snapshot | replay原Snapshot，不重新retrieval |
| Concurrent duplicate response | unique operation mapping | exactly one Snapshot；其餘讀原result |
| Frozen build missing／corrupt | operation + broken lineage | unavailable或repair-required；不得fallback new Active |
| Activation authority ambiguous | conflicting authority evidence | Fail Closed、operator-visible、governed repair only |

Candidate C提供上述local facts、same-operation reads及typed disposition。SPEC-011 Runtime負責何時執行recovery、retry scheduling、budget、clock及startup orchestration。

---

# 14. Integrity、Repair與Local Readiness

## 14.1 Integrity Reads

Authoritative read不得把corruption、partial enumeration或schema mismatch解讀為empty Corpus、no Active build或`NO_MATCH`。Integrity至少覆蓋：

- manifest commitment及source consistency；
- build metadata與index consistency；
- single activation authority及target resolution；
- operation frozen-boundary consistency；
- exactly-one Snapshot mapping；
- Snapshot references及content commitments；
- schema／compatibility version support。

## 14.2 Repair Boundary

Normal read、retrieval、restart及Runtime不得delete、truncate、choose-winner、re-embed或rewrite authority以「修好」矛盾。Repair必須是future governed operator capability，保存原evidence、明確授權、可稽核且不改寫historical Snapshot。

## 14.3 Candidate-C Local Readiness

Candidate C至少能authoritatively表達：

```text
READY
NOT_INITIALIZED
UNAVAILABLE
MISMATCH
REPAIR_REQUIRED
```

Exact enum naming可於Implementation Phase調整，但語意不可合併。`READY`要求manifest、activation target、build compatibility、index open及required integrity皆成功。Knowledge local readiness不是whole-platform Runtime READY；SPEC-011 Startup Recovery authority不變。

---

# 15. Team-local Rebuild與Generated Artifacts

四名成員在相同repository revision、approved sources、manifest、compatibility profiles及合法credentials下，必須能：

1. 驗證manifest admission；
2. 建立相同semantic Build Identity及ordered chunk identities；
3. 產生local immutable staged index；
4. 執行required validation及drift check；
5. 明確查看Active／LKG及local readiness；
6. explicit activate指定validated build；
7. 執行bounded retrieval並取得可比較的provenance。

Local vector files及generated index不是Git authority，不得要求複製某成員Chroma DB。Repository implementation必須提供dependency／config expectations、non-secret examples、rebuild／validate／activate能力及`.gitignore` hygiene；exact command spelling、directory及collection name不由本SPEC v1.0凍結。

Provider浮點或ANN engine若無法保證bitwise identical vectors／scores，implementation仍須保證identity inputs、candidate bounds、ordering rules、profile及Snapshot replay deterministic；允許的numeric tolerance必須versioned、明示且測試，不得以native nondeterminism取消semantic guarantee。

---

# 16. Public Semantic Ports — D4-3

## 16.1 Candidate-C-owned Capabilities

本SPEC凍結下列semantic capabilities，不凍結class、method、route或DTO名稱：

1. **Manifest Admission Read／Validate**：驗證governed manifest及source admission，回傳typed findings。
2. **Build Stage**：以明確compatibility inputs建立immutable staged build。
3. **Build Validate**：對指定build執行required validation並保存authoritative result。
4. **Build Activate**：以stable operation identity explicit activate指定validated build。
5. **Activation／LKG Read**：authoritatively讀取single Active／LKG及integrity state。
6. **Local Readiness Read**：讀取Candidate-C capability truth，不宣告whole-platform READY。
7. **Retrieval Resolve**：建立／resume Retrieval Operation、freeze build、取得typed Knowledge Resolution及Snapshot。
8. **Unavailable Finalization**：只接受authoritative orchestration對既有operation的terminal degraded／unavailable finalization，建立或replay immutable unavailable Snapshot；不擁有finalization timing。
9. **Retrieval Operation Read**：以operation identity讀取frozen boundary、state、failure或Snapshot reference。
10. **Knowledge Snapshot Read**：以Snapshot identity取得immutable validated Snapshot。
11. **Build／Snapshot Provenance Read**：deterministic resolve document／chunk及compatibility lineage。

Public reads必須區分not found、unavailable、invalid及repair-required；不得把read error回傳為empty或NO_MATCH。

## 16.2 Opaque Cross-domain References

Current baseline沒有active SPEC-012／013，故本SPEC不猜測其concrete type。Candidate C只接受及保存帶type discriminator的opaque references，不解析或擁有其domain truth。

Future expected semantics：

- Candidate A保存exact `knowledge_snapshot_id` reference，但RCA persistence仍由Candidate A擁有；
- Candidate D只透過Candidate C public Snapshot／resolution消費Knowledge，不直接查private Chroma或build metadata；
- Candidate E組合Evidence、Knowledge、Generation及Publication cross-domain protocol；
- SPEC-011排程retry／recovery並維持Runtime Clock／budget；
- Candidate C不得直接mutation Incident `rca_ref`。

Future active upstream contract若命名或representation不同，須做explicit reconciliation；不得silent reinterpret opaque reference。

---

# 17. Security與Ground Truth Isolation

## 17.1 Secret Boundary

- Credentials不得hardcode或commit。
- Secret不得進manifest、build identity、index metadata、chunk text、Snapshot、log、telemetry或failure summary。
- Credential Profile ID可保存，但必須non-secret。
- External embedding payload只可來自admitted approved content，並通過allowlist／redaction及size bounds。
- 無法確認content或metadata安全時Fail Closed，不得送provider後再補稽核。
- Raw provider request／response不得成為primary Knowledge authority；debug保存須另有least-privilege、redaction及retention boundary。

## 17.2 Ground Truth Isolation

Production admission必須明確拒絕test／evaluation-only roots、Scenario config、validator expected answer、expected causal class、fixture label及hardcoded S1～S6 mapping。僅移除`scenario_id`欄位但保留其答案內容仍屬違規。

Tests可使用isolated synthetic corpus／manifest，但其namespace、store、build及activation不得與production capability authority共用。Demo ground truth只能在結果產生後由evaluation layer比較，不得進query、filter、applicability或retrieved answer。

---

# 18. Observability

Candidate C應產生不取代authority的structured telemetry，至少可觀察：

- manifest admission及rejection category；
- build stage／validation／activation result；
- Active／LKG identity的non-secret reference；
- drift、mismatch、corruption及repair-required；
- Retrieval Operation stage、frozen build、profile及resolution；
- MATCH／NO_MATCH／unavailable／invalid distribution；
- bounded candidate／applicable count及truncation；
- Snapshot commit／replay／conflict；
- provider invocation timing及typed failure，不含payload secret；
- local readiness transition。

Telemetry、log、metric及health endpoint均不是manifest、activation、operation或Snapshot authority。Exact event／metric names留Implementation Phase。

---

# 19. Acceptance Criteria

## AC-014-A — Authority Isolation

Candidate C tests證明其不建立Evidence、RCA Artifact／Version、Incident mutation、Runtime scheduler／clock／budget或Ground Truth authority；production code不依賴validator expected answers。

## AC-014-B — Canonical Manifest Admission

只有canonical manifest中active、approved、hash一致且security-eligible的documents可進build；unlisted、missing、changed、duplicate或retired source使admission Fail Closed。

## AC-014-C — Ground Truth Isolation

Scenario、fixture、validator expected answer及evaluation label即使可讀或與SOP文字相似，也不能被production manifest admit、embed、retrieve或用於applicability。

## AC-014-D — Identity Determinism

相同canonical inputs在獨立process／member environment產生相同document／version、ordered chunk及Build Identity；content、chunking、embedding或schema compatibility改變產生不同identity。

## AC-014-E — Fail-Closed Pre-Build Security

含secret、禁止classification、unsupported content或hash mismatch的required entry在任何embedding invocation前被拒絕，且failure output不洩漏secret。

## AC-014-F — Immutable Staged Build

Build完成後內容不可in-place修改；STAGED或VALIDATED build存在不改變Active authority，partial／failed build不得被retrieval使用。

## AC-014-G — Required Validation

Missing／duplicate／orphan chunk、embedding mismatch、invalid metadata、failed probe或integrity failure阻止activation並保留typed findings。

## AC-014-H — Explicit Atomic Activation

只有指定validated build可經stable activation operation explicit activate；commit前舊build仍Active，commit後new build唯一Active／LKG，same-operation replay不重複transition。

## AC-014-I — Failed Rebuild Preservation

在admission、embedding、index write或validation任一點失敗，current LKG identity、content及retrieval能力保持不變。

## AC-014-J — Authoritative Restart Resolution

Restart只依single durable activation authority解析exact build；存在較新timestamp、較大ID或額外validated build也不得改變winner。Ambiguous／missing target Fail Closed。

## AC-014-K — Retrieval Operation Identity

Retrieval Operation與Snapshot、RCA Attempt、Runtime Work具有不同type／namespace，不能互換或以字串巧合通過validation。

## AC-014-L — One-time Build Pinning

Operation freeze後即使另一build activation，所有retry／resume仍使用原build；原build不可讀時回報unavailable／repair-required，不fallback新Active。

## AC-014-M — Versioned Bounded Retrieval

Invalid／unbounded query被拒絕；fixed build、query及profile只回傳有限candidate／content／metadata，並保存profile及truncation provenance。

## AC-014-N — Deterministic Ordering

Vector-store以不同native tie order回傳相同candidates時，Candidate C仍依profile及stable Chunk Identity產生相同final order；invalid score按contract fail，不任意排序。

## AC-014-O — Deterministic Applicability

相同Snapshot inputs與policy version產生相同`DIRECT / PARTIAL / CONTEXTUAL / NONE`；改變單一similarity score不能繞過required metadata／scope rules。

## AC-014-P — MATCH Semantics

只有至少一個approved candidate通過applicability policy才可`MATCH`；Snapshot包含ordered applicable refs及完整corpus／build／policy provenance。

## AC-014-Q — NO_MATCH / Knowledge Gap

正常retrieval但無applicable knowledge產生`NO_MATCH`、`knowledge_gap=true`及immutable Snapshot；不得automatic retry、切換build或填入model-suggested answer。

## AC-014-R — Failure Separation

Index unavailable回傳`RETRIEVAL_UNAVAILABLE`；manifest／activation／lineage contradiction回傳`INVALID / REPAIR_REQUIRED`；兩者都不能回傳`NO_MATCH`。

## AC-014-S — Exactly-one Knowledge Snapshot

成功logical operation在normal、concurrent及replay路徑均exactly one Snapshot；operation成功與Snapshot publication不可出現observable partial state。

## AC-014-T — Replay與Response Loss

Snapshot commit後response loss，再以same operation讀取時回傳相同Snapshot identity及content，不重新query；same identity配不同query／profile／build request Fail Closed。

## AC-014-U — Concurrency

Concurrent activation最多一個transition成功；freeze／activation race只取得完整old或new build；concurrent same-operation retrieval最多commit一份Snapshot。

## AC-014-V — Integrity與Repair

Corrupt／partial authority不得被解讀為empty、NOT_INITIALIZED或NO_MATCH；normal startup／read／Runtime不自動delete、choose winner或rewrite，並保留operator-visible repair-required evidence。

## AC-014-W — Local Readiness Isolation

Candidate C能區分READY、NOT_INITIALIZED、UNAVAILABLE、MISMATCH及REPAIR_REQUIRED；Knowledge unavailable不改寫SPEC-011 whole-platform READY semantics。

## AC-014-X — Team Rebuild

全部四名team members在符合approved setup／config contract的各自環境中，皆可由同一repository authority重建compatible Knowledge index、detect drift／mismatch、validate staged build並執行explicit governed activation path，不複製個人vector DB。此AC只驗證Candidate C Knowledge capability reproducibility；完整real-provider／real-RAG RCA E2E由Candidate F／final integration驗證，不降低本four-member rebuild gate。

## AC-014-Y — Public Semantic Reads

Activation、operation、Snapshot及provenance reads可deterministic resolve；not-found、unavailable、invalid及repair-required有不同typed result，private vector-store reads不構成public contract。

## AC-014-Z — Cross-domain Boundary

Integration tests使用opaque fake upstream references證明Candidate C不解析Evidence／Attempt內部內容、不mutationIncident、不排程retry；downstream只以exact Snapshot reference及public read消費Knowledge。

## AC-014-AA — Security and Secret Non-disclosure

Credential、authorization header及secret-shaped corpus metadata不會進Git、identity、Snapshot、telemetry或failure summary；unsafe outbound content在provider invocation前Fail Closed。

## AC-014-AB — Historical Immutability

New manifest、build activation、document retirement或policy change不改變既有operation frozen boundary、Snapshot content、resolution或provenance。

## AC-014-AC — Shared Credential／Capability Authority

Candidate C只接受approved shared non-secret Profile／capability reference；capability不足時typed fail，不建立credential registry、不解析或保存secret、不自行選key。Same logical build／retrieval及其retry／recovery維持selected Profile continuity，silent switching被拒絕。

## AC-014-AD — Provider Invocation Safety

Embedding／provider contract tests驗證timeout、request／batch bound、rate／quota及cost／resource protection與typed exhaustion facts。SDK hidden retry停用或可證明計入唯一Runtime-authorized budget；Candidate C沒有durable retry scheduler、clock或第二budget。

## AC-014-AE — Finalized Unavailable Knowledge Lineage

Transient provider failure不建立unavailable Snapshot；只有authoritative terminal degraded／unavailable finalization可為既有frozen operation建立exactly one Snapshot，且包含`source_status=UNAVAILABLE`、`resolution=RETRIEVAL_UNAVAILABLE`、selected non-secret Profile、zero retrieved refs及required lineage。Equivalent replay回傳same Snapshot；contradictory finalization及`INVALID / REPAIR_REQUIRED` Fail Closed。

## AC-014-AF — Referential-Integrity Retention Floor

Published RCA、materially relevant failed Attempt、outstanding Retrieval Operation或unfinished reconciliation／recovery仍引用Snapshot時，即使build不再Active、new activation、restart或age增加，Snapshot及required content／build lineage仍可read及deterministically resolve。

## AC-014-AG — Approved PoC Technology Baseline

PoC implementation及real integration verification使用Google `text-embedding-004`與ChromaDB。Provider／model family或vector-store technology substitution在沒有governance approval時被拒絕；adapter、SDK API、collection及path命名不作為此AC的固定值。

---

# 20. Verification Strategy

Future implementation至少須提供：

- pure deterministic identity／canonicalization tests；
- manifest admission及security rejection tests；
- fake embedding／index contract tests；
- shared Profile capability validation、continuity及no-silent-switch tests；
- provider invocation timeout／bounds／quota／cost及hidden-retry isolation tests；
- staged build、validation、activation及LKG store tests；
- crash／response-loss及same-operation replay tests；
- concurrency tests；
- corruption／partial-read／schema-mismatch tests；
- deterministic ordering及applicability policy tests；
- Knowledge Snapshot cardinality／immutability tests；
- authoritative unavailable finalization／replay及contradictory finalization tests；
- referenced Snapshot／content／historical-build retention-floor tests；
- scenario／ground-truth isolation tests；
- all-four-member rebuild reproducibility test；
- public port boundary及fake Candidate A／D／E／Runtime integration tests；
- explicit opt-in Google `text-embedding-004`／ChromaDB validation，不納入credential-free default regression。

Default regression不得依賴live provider。Fake adapter evidence不得冒充real-RAG Demo成功。

---

# 21. Implementation-deferred Choices

以下不由本SPEC v1.0凍結：

- exact module、class、method、DTO或API route名稱；
- exact durable database及table／column名稱；
- UUID／hash algorithm及text encoding的具體選型，但必須符合canonical identity contract；
- exact filesystem、staging directory或Chroma collection naming；
- exact Chroma client／persistence API；
- exact chunk size、overlap及embedding batch size；
- exact top-k、score threshold及numeric tolerance values；
- exact timeout、request／batch、rate／quota及cost／resource numeric values；
- exact deterministic applicability rules及approved metadata vocabulary；
- exact shared-capability DTO／class／API及configuration key names；
- exact local CLI command及flags；
- exact telemetry／metric名稱；
- exact lock、CAS或transaction implementation；
- production retention／archive及governed repair tooling；
- future Candidate A／B／D／E concrete DTO及operation encoding；
- Docker topology及volume naming。

上述選擇不得削弱authority、immutability、single activation、one-time pinning、typed resolution、exactly-one Snapshot、security或Runtime separation。

---

# 22. Repository Reality與Implementation Gate

Approval／design baseline目前：

- 沒有approved non-empty SOP corpus或manifest；
- 沒有ingestion／chunking／embedding／Chroma dependency；
- 沒有build、activation或LKG store；
- 沒有retrieval API、applicability或Knowledge Snapshot；
- 沒有Candidate C tests、config、rebuild command或Docker wiring；
- current tree沒有active SPEC-012／013；
- README仍正確標示RCA／RAG未實作。

因此本文件核准只代表Engineering Contract已Approved，不得宣告Implemented。Implementation尚未開始；後續須另依implementation plan新增code、tests、dependencies、config及documentation，且完成相應verification後才可聲稱implemented capability。

---

# 23. Approval Completion Checklist

- [x] D1-1 Governed Manifest Admission已formalize。
- [x] D1-2 Document／Chunk／Build identity已formalize。
- [x] D1-3 Fail-Closed Admission已formalize。
- [x] D2-1 Staged → Validate → Activate已formalize。
- [x] D2-2 Single Durable Activation Authority已formalize。
- [x] D2-3 LKG Recovery及ambiguity Fail Closed已formalize。
- [x] D3-1 Independent Retrieval Operation及one-time pinning已formalize。
- [x] D3-2 Bounded deterministic profile已formalize。
- [x] D3-3 Typed Resolution及deterministic applicability已formalize。
- [x] D4-1 Exactly-one immutable Snapshot已formalize。
- [x] D4-2 Local truth／external Runtime recovery已formalize。
- [x] D4-3 Public ports／opaque refs已formalize。
- [x] Security、Ground Truth isolation、concurrency、crash、repair及team rebuild已formalize。
- [x] AC-014-A～AG已建立。
- [x] Phase 3 Re-review PASS；Engineering Contract approval完成。
- [ ] Implementation尚未開始。
