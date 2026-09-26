# SPEC-014 — RAG Knowledge Index & Retrieval

## Software Design Specification v1.2

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-014 |
| Document Name | RAG Knowledge Index & Retrieval |
| Version | 1.2 |
| Status | Implemented |
| Approval Date | 2026-09-26 |
| Requirement Authority | PRD-004 v1.0 Approved |
| Runtime Authority | SPEC-011 v1.1 |
| Related Incident Contract | SPEC-008 v1.2；RCA integration implementation pending |
| Implementation Owner | 富裕 |

### Change History

| Version | Date | Status | Change |
|---|---|---|---|
| 0.1 | 2026-09-25 | Draft | 將Phase 0 Repository Reality Audit及Frozen D1～D4 formalize為Candidate C Engineering Contract；未進行implementation。 |
| 1.0 | 2026-09-25 | Approved — Implementation Pending | Phase 3 Re-review PASS；D1～D4 Engineering Contract frozen；F-014-01～F-014-06全部closed；正式核准進入implementation planning，implementation尚未開始。 |
| 1.1 | 2026-09-26 | Draft — Governance Amendment Review Pending | Additive formalize Production Knowledge Governance G1～G4；保留v1.0 Approved engineering baseline及Frozen D1～D4。Slices 1～5已完成並通過各slice audit；Slice 6因production corpus governance gap暫停。本amendment尚待read-only semantic review。 |
| 1.1 | 2026-09-26 | Approved — Implementation In Progress | Production Knowledge Governance Amendment核准；G1-A～G4-A正式納入SPEC-014 contract，Semantic Review／Re-review PASS。v1.0 historical approval及Slices 1～5 implementation evidence維持有效；Slice 6仍pending Scope Freeze re-entry／implementation。PRD semantics及Frozen D1～D4均未改變。 |
| 1.2 | 2026-09-26 | Implemented — Documentation-only Closure | Post-integration reconciliation：記錄approved six-document corpus、governed manifest、Google embedding／Chroma adapters、local lifecycle CLI、Knowledge Snapshot及public provenance已實作。AC-014-X未執行，依PM指示以explicit verification exception完成current repository closure；不修改Approved AC或Frozen D1～D4／G1～G4。 |

### Status Honesty

> **Draft ≠ Approved；Approved ≠ Implemented；Slice implementation ≠ RCA E2E complete。**

**SPEC-014 v1.0 Engineering Contract與v1.1 Production Knowledge Governance amendment維持historical authority；v1.2為documentation-only closure，Current Status為`Implemented`。** Repository現已包含approved six-document corpus、governed manifest、Google `text-embedding-004` adapter、Chroma index adapter、build／validate／activate／retrieve CLI、Knowledge Snapshot與public provenance。AC-014-X未執行，依PM指示作current closure的explicit verification exception；這不是PASS，也沒有four-member evidence。Default regression亦不構成live Google provider或real-RAG PASS。此closure不表示Candidate D／E／F、LLM generation、final RCA E2E或Production Ready。

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

## 0.6 Production Knowledge Governance Amendment — G1～G4

本v1.1 amendment只對v1.0 Approved engineering baseline作additive precision，不reopen或改寫D1-1～D4-3。G1-A～G4-A已完成Semantic Review／Re-review並隨v1.1 approval正式納入本SPEC contract；此approval本身不表示Slice 6 governance／implementation gate已通過。

| Decision | Additive normative precision |
|---|---|
| G1 — v1 Corpus Membership and Coverage | v1 Production Corpus須涵蓋current repository scenario scope所代表的六類operational problem；每類至少一份non-empty、meaningful、human-reviewed、approved、versioned且production-eligible的SOP／runbook revision。六類是operational coverage categories，不是Scenario ID或answer mapping。Coverage完整不保證每個query皆`MATCH`，runtime仍可合法產生`NO_MATCH`。 |
| G2 — Human Approval／Version／Lifecycle | Production Knowledge採human-governed immutable revision。Human governance核准actual content revision及production eligibility，並提供可稽核governance reference；Candidate C重新計算actual content commitment並Fail Closed驗證。只有approved且active的revision可admit，content change必須產生new Document Version，不得in-place overwrite historical revision。 |
| G3 — Classification／Outbound Eligibility | 每份送往Approved Google embedding provider的production source，須有explicit human governance decision確認為approved operational knowledge、non-secret、synthetic／mock operational content且outbound eligible。Manifest保存治理結果但不是該結果的semantic source；filename、author、manifest boolean或runtime guess均不得自行授權outbound。 |
| G4 — Knowledge Type／Applicability Governance | v1使用governed Knowledge Type及minimum applicability taxonomy。只有governance-authorized type與guidance-authority facts通過versioned applicability policy後，才可提供downstream `SOP_BACKED` support；RAG retrieval或similarity `MATCH`本身不授予`SOP_BACKED` authority。 |

G1六類operational coverage categories為：

1. credential abuse／brute-force response；
2. database slow-query／API-timeout diagnosis；
3. high-memory／OOM／service-crash handling；
4. external／third-party API timeout or outage handling；
5. shared database／network dependency interruption handling；
6. HTTP 429／rate-limit／QPS-spike handling。

上述categories不得編碼為`scenario_id`、S1～S6 mapping、expected root cause、validator answer或Ground Truth。Exact approver DTO、workflow UI、field names、serialization、numeric thresholds及physical paths均不由本amendment凍結。

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
| Governed Knowledge Type | Human governance核准的`SOP`、`RUNBOOK`或`OTHER_APPROVED_OPERATIONAL_REFERENCE` semantic classification；不等同media／MIME content type |
| Guidance Authority Fact | 可稽核表達該approved revision能否及以何種governed type支撐downstream guidance authority的manifest／Snapshot fact |
| Governed Source Root | Production Knowledge唯一logical source-root boundary；只界定受治理source集合，不凍結exact repository directory name |
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

v1 corpus release本身須取得explicit human governance approval，不能因所有entries各自可讀或通過mechanical validation而自動成為approved release。Human governance是actual content revision、Knowledge Type、classification、active／retired／revoked state、outbound eligibility及release membership的semantic authority；Manifest是Candidate C保存並執行這些治理結果的canonical admission authority，兩者不得混為一談。

v1 approved release須完整涵蓋第0.6節G1六類operational coverage categories，每類至少一份符合本SPEC admission的SOP／runbook revision。同一revision可否覆蓋多個category須由其approved governance facts及metadata明確表達，不得由filename、similarity或Scenario mapping猜測。Coverage gate只證明v1 corpus membership完整；對個別canonical query仍須執行retrieval及applicability，並允許合法`NO_MATCH`。

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

Production admission另須驗證：

11. source不是empty、whitespace-only或structurally contentless，且至少包含一個可canonical識別的有效Knowledge section；
12. required governance metadata完整，包含governed Knowledge Type、minimum applicability taxonomy、Guidance Authority Fact及metadata vocabulary／schema identity與version；
13. governance reference可識別該human-reviewed actual content revision及其approved／active／production-eligible decision；
14. external embedding適用時，human governance已明確核准該revision為non-secret、synthetic／mock operational content及outbound eligible；
15. 整個proposed v1 release通過第0.6節G1 six-category coverage gate並具有explicit release approval。

任何required entry失敗，整個build admission Fail Closed。禁止silent skip、best effort corpus、partial success後activate或以舊vector掩蓋missing source。

Empty、whitespace-only、沒有任何有效canonical Knowledge section或metadata incomplete的production source，必須在任何embedding invocation前Fail Closed。Exact byte threshold、section parser、loader method及physical file format留Implementation Phase；implementation仍須提供deterministic且可測的contentfulness／section validation，不得以檔案存在或hash一致取代。

Incident-specific ground truth無論是unreviewed、human-reviewed、confirmed或historically correct，都不得因review或事實正確而直接取得production Knowledge authority。它不得被production manifest admit、成為retrieval／applicability metadata或canonical filter truth、被embed、送往external embedding provider或支撐`SOP_BACKED`。Human review本身不會把Incident-specific ground truth轉換成approved operational knowledge。

若Incident經驗需要形成Knowledge，必須先經獨立knowledge authoring及governance：將Incident-specific material generalize為operational knowledge、移除incident-specific truth／Ground Truth encoding、建立新的SOP／RUNBOOK Document Revision，再完成human governance review、classification／outbound approval及manifest admission。只有該independently authored and approved SOP／RUNBOOK revision可成為production Knowledge；此boundary不授權automatic ingestion、Knowledge Improvement workflow、conversion service或具體transformation algorithm。

## 3.3 Corpus Mutation Boundary

Runtime對Active Corpus及Active Index read-only。Candidate C build流程不得自動將下列內容寫入Corpus：

- Scenario／Ground Truth／validator output；
- Closed Incident或Resolution submission；
- Shadow／Blocked record；
- RCA conclusion、MODEL_SUGGESTED guidance或provider response；
- Knowledge Gap或Knowledge Improvement Candidate；
- arbitrary filesystem／prompt／log content。

Future human governance可產生新的approved manifest／document version；該行為形成new build input，不得in-place改寫historical build或Snapshot。

## 3.4 Human Approval、Revision與Lifecycle

Production Knowledge採human-governed immutable revision：

- stable logical Document Identity在approved revisions間維持continuity；
- 每個Document Version只對應一份human-reviewed且approved的actual content revision；
- Human governance負責核准content／version、Knowledge Type、Guidance Authority Fact、classification、outbound eligibility、state及release membership；
- Candidate C必須重新讀取source、計算actual content commitment並與manifest hash Fail Closed比對，不得信任人工提供的hash結果或以approval取代mechanical integrity validation；
- content改變必須建立new Document Version及new manifest commitment；
- approved historical revision不得in-place overwrite；
- retirement／revocation只影響future admission／activation eligibility，不得retroactively rewrite historical Build、Retrieval Operation或Snapshot；
- Git tracked、filename含`sop`、Chroma已有vector或舊Build曾引用，都不能取代human governance approval及active state。

Governance reference必須stable、non-secret且可稽核地指向該revision的human decision；exact approver DTO、reference encoding、file format及workflow UI留Engineering／Implementation Phase。

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
- 每份source通過non-empty、meaningful、canonical Knowledge section及minimum governance／applicability metadata completeness validation；
- proposed v1 release具有explicit governance approval，且G1六類operational coverage gate完整；
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

### 9.2.1 Production Retrieval／Applicability Metadata

Production Retrieval Profile及Applicability Policy都必須有explicit identity及version，並只使用approved metadata與canonical query facts。v1 metadata semantics至少能表達：

- Governed Knowledge Type：`SOP`、`RUNBOOK`或`OTHER_APPROVED_OPERATIONAL_REFERENCE`；
- operational domain；
- applicable component／service scope；
- dependency role／scope；
- problem／failure class；
- observable signal／symptom class；
- applicability constraints／required scope predicates；
- Guidance Authority Fact；
- canonical section identity；
- metadata vocabulary／schema identity及version。

Required dimension缺失、unsupported或與query facts矛盾時，candidate不得取得applicable result或`SOP_BACKED` support；exact handling須由versioned policy deterministic定義。Exact key names、enum spelling、DTO、serialization、top-k、score threshold及numeric bounds留Engineering／Implementation Phase。

`scenario_id`、S1～S6、expected answer、Ground Truth、credential及unreviewed LLM guidance不得成為retrieval／applicability metadata。允許的query facts必須來自approved bounded upstream context，不得用Scenario metadata補齊缺失scope。

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

### 10.1.1 Governed Knowledge Type與`SOP_BACKED`

Governed Knowledge Type closed semantic set為：

```text
SOP
RUNBOOK
OTHER_APPROVED_OPERATIONAL_REFERENCE
```

Exact enum spelling可由Engineering／Implementation選擇，但不得合併其semantic distinction。`SOP`及`RUNBOOK`只有在human governance明確授予相應Guidance Authority Fact、revision維持approved／active／production-eligible，且candidate通過versioned applicability policy時，才能支撐downstream `SOP_BACKED` semantic。`OTHER_APPROVED_OPERATIONAL_REFERENCE`只提供approved context，不得被重新標示為`SOP_BACKED`；若future governance需要新的guidance authority semantic，須以new governed type／versioned contract明確處理，不得silent reinterpret本類型。

Similarity、vector rank、`MATCH`、文件名稱、作者或曾被歷史RCA引用，均不得單獨授予`SOP_BACKED` authority。Candidate C須在Snapshot／public provenance中保存governed type、Guidance Authority Fact及applicability result，使downstream可依authoritative facts判斷；Candidate C不擁有下游guidance presentation或RCA conclusion。

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

四名成員在相同repository revision、approved source revisions、approved governed manifest、required non-secret semantic configuration、rebuild tooling、compatibility profiles及合法credentials下，必須能：

1. 驗證manifest admission；
2. 建立相同semantic Build Identity及ordered chunk identities；
3. 產生local immutable staged index；
4. 執行required validation及drift check；
5. 明確查看Active／LKG及local readiness；
6. explicit activate指定validated build；
7. 執行bounded retrieval並取得可比較的provenance。

Git authority至少包含approved source revisions、approved governed manifest、required non-secret semantic configuration及rebuild tooling。Local vector files及generated index不是Git authority，不得要求或允許以複製另一成員Chroma DB作為team baseline或規避rebuild／validation。Repository implementation必須提供dependency／config expectations、non-secret examples、rebuild／validate／activate能力及`.gitignore` hygiene；exact command spelling、source directory、generated directory及collection name不由本SPEC v1.1凍結。

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

Historical-phase qualifier：上句保留本section核准時的baseline事實。Current repository已有Implemented的SPEC-012／013；Candidate C仍只保存opaque cross-domain references，ownership boundary不變。

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
- Incident-specific ground truth即使已human-reviewed、confirmed或historically correct，仍不得送往external embedding provider；只有依第3.2節獨立author並核准的新SOP／RUNBOOK revision可依其classification及outbound approval送出。
- 無法確認content或metadata安全時Fail Closed，不得送provider後再補稽核。
- Raw provider request／response不得成為primary Knowledge authority；debug保存須另有least-privilege、redaction及retention boundary。

## 17.2 Ground Truth Isolation

Production admission必須明確拒絕test／evaluation-only roots、Scenario config、validator expected answer、expected causal class、fixture label及hardcoded S1～S6 mapping。僅移除`scenario_id`欄位但保留其答案內容仍屬違規。

Production admission、retrieval／applicability metadata及canonical filters亦必須拒絕Incident-specific ground truth，不因其經過human review、已confirmed或historically correct而例外。Review Incident material不會賦予Knowledge authority；只有依第3.2節完成generalization、移除incident-specific／Ground Truth encoding並形成獨立approved SOP／RUNBOOK revision後，該new revision才可依正常governance進入Corpus。

Tests可使用isolated synthetic corpus／manifest，但其namespace、store、build及activation不得與production capability authority共用。Demo ground truth只能在結果產生後由evaluation layer比較，不得進query、filter、applicability或retrieved answer。

## 17.3 Governed Source-root Boundary

Production sources必須全部位於單一governed logical source-root boundary。Admission必須以canonical path resolution證明每個source位於該boundary內，並拒絕path traversal、symlink escape或其他跨boundary alias。該logical root必須與下列內容隔離：

- Scenario、Ground Truth及validator／evaluation data；
- test fixtures；
- generated indexes／Chroma state；
- local authority stores、runtime state及caches；
- credentials、secret files及environment material。

Source-root authority來自governance-approved boundary及manifest reference，不來自caller任意傳入的filesystem path。Exact repository directory name、loader、mount及path configuration留Slice 6 Scope Freeze／Implementation；不得因此允許多個未治理roots或runtime filesystem discovery成為admission authority。

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

只有canonical manifest中human-reviewed、active、approved、hash一致、metadata complete且security-eligible的documents可進build；unlisted、missing、changed、duplicate、retired、empty、whitespace-only、structurally contentless或沒有canonical Knowledge section的source使admission Fail Closed。v1 release須有explicit governance approval，且六類operational coverage category各至少有一份non-empty、meaningful、versioned、production-eligible SOP／runbook revision；coverage完整仍不保證個別query `MATCH`。

## AC-014-C — Ground Truth Isolation

Scenario、fixture、validator expected answer、expected root cause、S1～S6 mapping及evaluation label即使可讀或與SOP文字相似，也不能被production manifest admit、embed、retrieve、用作metadata／query filter或用於applicability。

## AC-014-D — Identity Determinism

相同canonical inputs在獨立process／member environment產生相同document／version、ordered chunk及Build Identity；content、chunking、embedding或schema compatibility改變產生不同identity。

同一Document Identity的approved content發生任何改變時，必須使用new Document Version及new manifest commitment；歷史approved revision、Build及Snapshot保持不變，禁止in-place overwrite。

## AC-014-E — Fail-Closed Pre-Build Security

含secret、禁止classification、unsupported content、hash mismatch或缺少human governance outbound approval的required entry在任何embedding invocation前被拒絕，且failure output不洩漏secret。Manifest boolean、filename、author或runtime guess不能取代approved operational knowledge、non-secret、synthetic／mock及explicit outbound eligibility的governance decision。Incident-specific ground truth在unreviewed、human-reviewed、confirmed及historically correct各情況下都不能被admit、作為retrieval／applicability metadata或canonical filter、embed、outbound或支撐`SOP_BACKED`；測試須證明human review不會直接轉換其authority，只有獨立author且完成正常governance的新SOP／RUNBOOK revision才可能admit。

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

相同Snapshot inputs與policy version產生相同`DIRECT / PARTIAL / CONTEXTUAL / NONE`；改變單一similarity score不能繞過required metadata／scope rules。Production profile／policy具有explicit identity／version，且required metadata完整涵蓋governed Knowledge Type、operational domain、component／service scope、dependency role／scope、problem／failure class、observable signal／symptom、constraints／scope predicates、Guidance Authority Fact、canonical section及metadata schema identity／version。

## AC-014-P — MATCH Semantics

只有至少一個approved candidate通過applicability policy才可`MATCH`；Snapshot包含ordered applicable refs及完整corpus／build／policy provenance。`MATCH`或similarity本身不授予`SOP_BACKED`。只有governance-authorized `SOP`或`RUNBOOK` revision同時維持approved、active、production-eligible、具有required Guidance Authority Fact，並通過versioned applicability policy時，才可支撐`SOP_BACKED`。v1的`OTHER_APPROVED_OPERATIONAL_REFERENCE`只能提供approved contextual knowledge；即使retrieval為`MATCH`、similarity高或具有其他authority metadata，也不得支撐`SOP_BACKED`，除非future governance contract將內容正式重新分類並建立為SOP／RUNBOOK revision。

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

全部四名team members在符合approved setup／config contract的各自環境中，皆可由同一repository authority所含的approved source revisions、approved governed manifest、required non-secret semantic configuration及rebuild tooling，重建compatible Knowledge index、detect drift／mismatch、validate staged build並執行explicit governed activation path，不複製任何成員的generated Chroma DB。此AC只驗證Candidate C Knowledge capability reproducibility；完整real-provider／real-RAG RCA E2E由Candidate F／final integration驗證，不降低本four-member rebuild gate。

### AC-014-X Current Closure Disposition（Non-normative）

- Status: **NOT EXECUTED**
- Disposition: **PM-DIRECTED SKIP FOR CURRENT CLOSURE**
- AC-014-X was not executed；this is **NOT PASS**。
- No four-member evidence exists。
- Approved AC-014-X remains normative and is neither modified nor deleted by this closure note。
- PM authorized current repository closure under this explicit verification exception。

## AC-014-Y — Public Semantic Reads

Activation、operation、Snapshot及provenance reads可deterministic resolve；not-found、unavailable、invalid及repair-required有不同typed result，private vector-store reads不構成public contract。

## AC-014-Z — Cross-domain Boundary

Integration tests使用opaque fake upstream references證明Candidate C不解析Evidence／Attempt內部內容、不mutationIncident、不排程retry；downstream只以exact Snapshot reference及public read消費Knowledge。

## AC-014-AA — Security and Secret Non-disclosure

Credential、authorization header及secret-shaped corpus metadata不會進Git、identity、Snapshot、telemetry或failure summary；unsafe outbound content在provider invocation前Fail Closed。

Production admission亦驗證source位於single governed logical source-root，且該root與Scenario／Ground Truth、fixtures、generated indexes、local stores、caches及credentials隔離；path traversal、symlink escape或caller-selected ungoverned root在provider invocation前Fail Closed。

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

以下不由本SPEC v1.1凍結：

- exact module、class、method、DTO或API route名稱；
- exact durable database及table／column名稱；
- UUID／hash algorithm及text encoding的具體選型，但必須符合canonical identity contract；
- exact filesystem、staging directory或Chroma collection naming；
- exact Chroma client／persistence API；
- exact chunk size、overlap及embedding batch size；
- exact top-k、score threshold及numeric tolerance values；
- exact timeout、request／batch、rate／quota及cost／resource numeric values；
- exact deterministic applicability rules及approved metadata vocabulary；
- exact metadata key names、enum spelling、serialization及Governed Knowledge Type／Guidance Authority Fact的physical representation；
- exact approver DTO、governance-reference encoding、workflow UI及manifest file format；
- exact governed source-root repository directory name、loader及mount；
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

Repository reality目前：

- approved six-document production corpus及governed manifest已存在；
- production Knowledge config與team-local CLI已支援admit、build／rebuild、validate、inspect、activate及retrieve；
- Google `text-embedding-004` adapter與Chroma index adapter已接線；adapter存在不等於live-provider PASS；
- governed manifest admission、deterministic chunking、immutable staged build、validation、single activation／LKG、retrieval resolution、Knowledge Snapshot、public provenance／semantic reads、integrity及recovery已實作；
- SPEC-012／013皆為active implemented downstream contracts；
- integrated develop full regression為`1669 passed, 3 skipped, 0 failed`（63.11s），develop integration baseline為`b034921eab7d0fa17754eea902c9e12bc2932052`。

因此SPEC-014 current Status為`Implemented`。此status只涵蓋Candidate C核准範圍；AC-014-X為`NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE`且不是PASS。3個skipped tests不提供four-member、live Google provider、real-RAG或final RCA E2E evidence。

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
- [x] Slices 1～5 implementation完成並通過各slice audit。
- [x] v1.1 Production Knowledge Governance amendment之Semantic Review／Re-review PASS，G1-A～G4-A已Frozen並完成approval。
- [x] Actual v1 source revisions、six-category coverage及governed manifest已建立並納入current implemented setup。
- [x] Slice 6 production corpus、adapter wiring及local lifecycle workflow已實作。
- [ ] AC-014-X未執行；`NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE`，不是PASS，且沒有four-member evidence。
- [ ] Live Google provider／real-provider validation未由本closure宣稱PASS；只有actual live execution evidence可支持該聲明。
