# SPEC-012 — RCA Artifact & Persistence

## Engineering Specification v1.0

---

## Document Information

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-012 |
| Document Name | RCA Artifact & Persistence |
| Version | 1.0 |
| Status | Approved — Implementation Pending |
| Approval Date | 2026-09-20 |
| Requirement Authority | PRD-004 v1.0 Approved |
| Related Product Authorities | PRD-001 v3.5；PRD-003 v1.1 Final |
| Related Incident Contract | SPEC-008 v1.2 |
| Related Runtime Contract | SPEC-011 v1.1 |
| Implementation Owner | 富裕 |

### Change History

| Version | Date | Status | Change |
|---|---|---|---|
| 0.1 | 2026-09-19 | Draft | Phase 2 initial Engineering Contract draft；formalize Candidate A authority、D1～D11 Frozen Decisions及repository compatibility constraints。 |
| 1.0 | 2026-09-20 | Approved — Implementation Pending | Phase 3 semantic review、Phase 4 authorized narrow revision及Phase 3 re-review completed；F-001／F-002 closed，D1～D11 preserved。Engineering Contract approved as implementation baseline；implementation has not started。 |

### Status Honesty

> **Draft ≠ Approved；Approved ≠ Implemented。**

本文件是 Candidate A v1.0 approved Engineering Contract及implementation baseline；production implementation仍為Pending且尚未開始。本approval不代表RCA Store、RCA production pipeline、SPEC-008 RCA relationship mutation、SPEC-011 RCA Runtime integration或RCA E2E已實作，也不代表整體平台Production Ready。

---

# 0. Purpose, Authority and Decision Traceability

## 0.1 Purpose

本 SPEC 定義 RCA 作為 durable domain object 的最小完整工程契約：Logical RCA Aggregate、Generation Attempt、Logical Try durable outcome、RCA Version、immutable Artifact、Current／Historical、Fresh／Stale、lineage references、publication target identity、Candidate-A-side publication evidence、local persistence、authoritative reads、recovery evidence與retention。

核心責任句：

> **Candidate A preserves RCA business truth. SPEC-008 preserves Incident relationship truth. SPEC-011 orchestrates when and how cross-domain work proceeds.**

## 0.2 Authority Order

衝突判斷順序為：Approved PRD → Approved active SPEC → D1～D11 Frozen Engineering Decisions → repository implementation reality → DDS/runtime docs → README。Repository reality只提供implementation evidence與相容性限制，不得回寫或取代approved semantics。D1～D11是經上游驗證的downstream engineering contract，也不得凌駕PRD／active SPEC。

## 0.3 D1～D11 Traceability

| Decision | Candidate A contract |
|---|---|
| D1 | 擁有Aggregate、Attempt／Try durable truth、Version／Artifact、Current／Historical、freshness lineage、A-side publication evidence與public semantic reads；不收回其他domain authority。 |
| D2 | 使用獨立opaque identities，固定Incident／snapshot／profile lineage，區分Logical Try與physical invocation，Version order連續且publication identity一對一。 |
| D3 | 使用獨立configurable-path SQLite、obligation-first Aggregate、immutable Attempt core、append-only Try outcomes、allocate-with-Artifact atomic commit、publication-gated Current、explicit freshness與full retention。 |
| D4 | SPEC-008唯一擁有Incident RCA relationship；Candidate A不direct-write Incident persistence，只提供publication target及A-side facts。 |
| D5 | 只保存Evidence Snapshot／Revision exact references；Snapshot、Revision與Materiality仍由Candidate B擁有。 |
| D6 | 只保存Knowledge Snapshot exact reference；retrieval、applicability、gap與snapshot truth仍由Candidate C擁有。 |
| D7 | Candidate D決定validation、failure class與retry safety；Candidate A是Logical Try outcome history唯一durable authority。 |
| D8 | Candidate A提供recovery discovery facts；Candidate E／SPEC-011擁有work composition、ordering、retry timing、clock與startup recovery。 |
| D9 | Candidate A提供Current、history、freshness、lineage、Artifact conclusion／completeness及Attempt outcome等projection inputs；不擁有presentation composer。 |
| D10 | Candidate F只可觀察／評分；production persistence不得包含Evaluation Ground Truth或evaluation-only identity。 |
| D11 | Candidate A只判斷本地RCA store readiness／integrity；Candidate G擁有shared capability facts，SPEC-011擁有whole-platform READY。 |

---

# 1. Scope and Non-goals

## 1.1 In Scope

- RCA Aggregate與immutable Incident binding。
- Attempt immutable lineage、mutable lifecycle summary及append-only Logical Try outcomes。
- Validated Artifact的atomic Version allocation與immutable history。
- Candidate-A canonical Current、Historical及explicit freshness truth。
- Evidence／Knowledge／generation provenance references。
- `publication_operation_id`、A-side receipt／result與reconciliation facts。
- Independent SQLite persistence、local transactions、concurrency、schema recognition、readiness及integrity。
- Purpose-specific semantic reads、recovery discovery與full PoC retention。

## 1.2 Non-goals

本 SPEC 不定義或實作：

- Evidence Snapshot內容、Evidence Revision計算、query windows、collector或Materiality algorithm；
- Knowledge corpus、chunking、embedding、index、retrieval或applicability；
- prompt、provider adapter、LLM generation、grounding或validation algorithm；
- Incident persistence、`rca_ref` physical mapping或SPEC-008 private receipt／CAS；
- Runtime scheduler、retry timing／budget ledger、Runtime Work schema、clock或Startup Recovery framework；
- publication／reconciliation的跨domain orchestration composition；
- PRD-005 DTO／presentation composition、Evaluation scoring／Ground Truth或Credential physical schema；
- Docker topology、production migration program、archive engine或automatic repair。

---

# 2. Authority and Cross-domain Ownership

## 2.1 Candidate A Owns

Candidate A是下列truth的唯一RCA-domain authority：Aggregate identity與Incident binding、Attempt及Try outcome history、committed Version order、Artifact bytes／structured semantics及lineage binding、canonical Current、Current freshness與material-evidence basis、publication target identity、A-side receipt／result、local integrity與semantic reads。

## 2.2 References and Dependencies

- Candidate B：authoritative `evidence_snapshot_id`、`evidence_revision_id`及Materiality judgement。
- Candidate C：authoritative `knowledge_snapshot_id`及Knowledge applicability／gap semantics。
- Candidate D：validated admitted generation result、typed generation failure及retry disposition。
- SPEC-008：Incident existence與Incident-side coarse RCA／Current relationship truth。
- Candidate E／SPEC-011：obligation composition、scheduling、next logical retry authorization、publication reconciliation及startup recovery。
- Candidate F：read-only evaluation／audit consumer。
- Candidate G：non-secret profile／shared capability facts。

References不得被Candidate A重新計算、重新解釋或複製成第二份authority。Cross-store references不是foreign keys。

## 2.3 Explicitly Not Owned

SPEC-008仍是唯一Incident authority。SPEC-011仍是WHEN、ORDER、RETRY timing／budget、RECOVERY、Runtime Clock、Startup Recovery與cross-store coordination authority。Candidate A不得建立scheduler、retry DB、Runtime clock、startup barrier、Evidence／Knowledge store、grounding engine、evaluation truth或global `RCA_READY`。

---

# 3. Domain Model

## 3.1 Logical RCA Aggregate

Aggregate以opaque `aggregate_id`識別，並immutable綁定一個`incident_id`。

```text
1 Incident  → 0..1 Aggregate
1 Aggregate → exactly 1 Incident
1 Aggregate → 0..N Attempts
1 Aggregate → 0..N committed Versions
1 Aggregate → 0..1 canonical Current Version
```

當initial RCA obligation已被authoritatively admitted，Aggregate可先durable存在而`Attempts=0`、`Versions=0`。Aggregate表示obligation subject，不表示RCA已完成，也不形成第二份Incident authority。

## 3.2 Generation Attempt

Attempt以stable `attempt_id`識別，是固定immutable lineage上的logical generation work unit。Immutable core至少包含：

```text
attempt_id
aggregate_id
evidence_snapshot_id
evidence_revision_id
knowledge_snapshot_id
credential_profile_id          # non-secret identity only
generation provenance          # provider/model/prompt/config identities required upstream
```

Attempt可另有mutable lifecycle summary，用來coherently摘要目前狀態與latest authoritative Try disposition；summary不得覆寫或取代append-only Try history。新的Material Evidence需要generation時建立new Attempt，不得修改既有Attempt core。Candidate A接受Candidate B／D／E提供的authorized facts，不自行判斷materiality、generation eligibility或retry timing。

Attempt Generation Lifecycle的conceptual closed set為：

```text
PENDING | GENERATING | COMPLETED | FAILED
```

`PARTIAL`不是合法Generation Lifecycle state。Generation Lifecycle與Evidence Completeness是orthogonal semantics；`FULL／DEGRADED`不得取代上述lifecycle values，`FAILED`也不是`DEGRADED`的別名。Exact field、enum及storage representation是Implementation Choice。

Mutable lifecycle summary必須與Candidate A已保存的authoritative Attempt core、Logical Try outcomes及validated／committed Artifact lineage保持一致：

- `PENDING`表示logical Attempt已存在，但generation obligation尚未形成terminal generation result；
- `GENERATING`表示generation execution已進入active semantic phase，但尚無terminal generation result；
- `COMPLETED`表示Attempt已有authoritative successful terminal generation／admission result；需要Artifact的successful path必須可一致resolve相應validated／committed Artifact lineage；
- `FAILED`表示Attempt已有authoritative terminal generation failure，不得偽裝為`COMPLETED`。

Candidate A擁有上述durable summary及其integrity，不因而取得generation start、provider invocation、retry timing／budget、wake scheduling或startup recovery ordering authority。Candidate D仍擁有validation、generation failure semantics與retry safety；SPEC-011仍擁有WHEN／ORDER／RETRY／RECOVERY。

Restart不得將lifecycle summary重設為`PENDING`，Runtime Work缺失本身也不得重寫Candidate A lifecycle truth。Lifecycle read必須integrity-aware，並以authoritative Attempt／Try／Artifact facts形成coherent view；persistent contradiction不得silent normalize、last-write-wins或guess，必須依既有integrity conflict／`REPAIR_REQUIRED`／Fail-Closed semantics處理。

## 3.3 Logical Try and Durable Outcome

Logical Try identity為`(attempt_id, try_ordinal)`。`try_ordinal`是同一Attempt內的logical execution order，不是physical provider invocation count。

每個admitted Try outcome須append-only、restart-durable，至少能表達：

- identity與所屬Attempt；
- typed terminal result或failure family；
- Candidate D所決定的retry disposition；
- admitted validated result reference／payload，或safe failure information；
- required non-secret generation provenance及outcome time evidence；
- ambiguous physical invocation情形所需的recovery evidence，若Frozen semantics要求。

同一Try的equivalent outcome replay回原authoritative result；contradictory outcome必須Fail Closed。只有current Try已有authoritative disposition，且SPEC-011已consume／authorize下一logical retry slot時，才可admit下一`try_ordinal`。Candidate A不承諾physical provider invocation exactly once。

## 3.4 RCA Version and Artifact

每個committed Version包含獨立immutable `version_id`與同Aggregate內continuous public `version_number`。Version綁定exactly one Attempt的admitted validated result、one immutable Artifact、exact Evidence／Knowledge lineage及exactly one`publication_operation_id`。

Artifact保存PRD-004要求的structured RCA semantics，包括其analysis、hypotheses、actions、uncertainty、Evidence／Knowledge provenance、generation provenance、evidence completeness與diagnostic conclusion。Candidate A確保已由Candidate D admitted的representation被完整、immutable保存與讀回；Candidate A不重新執行grounding或validation。

Failed Attempt不配置或消耗published Version number。Committed number immutable、不得重用；Artifact修正只能建立new Version。

## 3.5 Current, Historical and Freshness

Candidate A維護每Aggregate至多一個canonical `current_version_id`。Current只能指向同Aggregate的valid committed Version。

```text
Artifact durable ≠ Published ≠ Current
```

新Version local commit後只是stable publication target。只有full publication relationship成功、包含SPEC-008 Incident-side authority已確認該target後，Candidate A才可將它設為Current；old Current在此前保持Current且可讀。成功replacement後old Current成為Historical／Superseded，但不被刪除或改寫。

Current freshness必須explicit表示`FRESH`或`STALE`，並保存其Material Evidence revision basis。Candidate A只能消費Candidate B的authoritative materiality judgement／basis，不得因`evidence_revision_id`不同自行推論Materiality。Refresh failure保留last-known-good Current與其可讀性；如有authoritative newer Material Evidence，舊Current可保持Current且explicit STALE。

`CURRENT_STALE_REFRESHING`、`CURRENT_WITH_DEGRADED_EVIDENCE`等組合是downstream projection，不是Candidate A新增的獨立business authority。Evidence completeness與diagnostic conclusion來自Current Artifact本身。

---

# 4. Identity and Lineage Invariants

下列invariants均為normative：

1. One `incident_id` maps to at most one `aggregate_id`; one `aggregate_id` maps to exactly one immutable `incident_id`.
2. Attempt immutable core在admission後不可改變；same `attempt_id`不得resolve至不同Aggregate、Evidence Snapshot／Revision、Knowledge Snapshot、Credential Profile或required generation provenance。
3. 每個Evidence Snapshot reference對應exactly one Evidence Revision reference；Candidate A不驗證其內容或計算Revision。
4. 即使Candidate C結果是legal `NO_MATCH`，Attempt仍須綁定immutable `knowledge_snapshot_id`。
5. Logical Try identity是`(attempt_id, try_ordinal)`；Logical Try不等於physical provider invocation。
6. Same Logical Try最多一個authoritative durable outcome；equivalent replay不append duplicate，contradiction Fail Closed。
7. Same `version_id`不得resolve至不同Aggregate、version number、Artifact semantics、Attempt或lineage。
8. Same Aggregate不得有duplicate committed `version_number`；committed numbers continuous、immutable且never reused。
9. One Version ↔ exactly one `publication_operation_id`；same publication identity不得target contradictory Version／Aggregate／Incident semantics。
10. Durable Artifact與A-side receipt不各自證明full publication或Incident-side publication。
11. New committed Version不得在authorized full publication完成前取代Current。
12. Current必須reference同Aggregate valid committed Version；每Aggregate至多一個canonical Current。
13. Current freshness必須explicit且具Material Evidence revision basis；Candidate A不得由Revision ID difference推論Materiality。
14. Refresh failure保留last-known-good Current。
15. Dangling references、contradictory identities、multiple-current corruption及receipt conflicts不得偽裝成Not Found、last-write-wins或silent repair。

依賴B／C／SPEC-008的invariant只驗證Candidate A保存的reference與authorized cross-domain evidence是否自洽；不得藉驗證收回對方authority。

---

# 5. Persistence Model

## 5.1 Topology

PoC使用Candidate A自己的independent configurable-path SQLite persistence。它不得與SPEC-008共享physical database authority、不得direct-write Incident SQLite、不得建立cross-store foreign key，也不要求distributed transaction或2PC。

Exact database filename、tables、columns、indexes、foreign keys within the local store及serialization留Implementation Choice。

## 5.2 Aggregate and Attempt Persistence

Aggregate creation採obligation-first，create-or-discover capability必須對equivalent authoritative obligation回same Aggregate，對same Incident的contradictory Aggregate identity Fail Closed。

Attempt採：

```text
immutable Attempt core
+ mutable lifecycle summary
+ append-only Logical Try outcome history
```

Lifecycle summary mutation必須由fresh authoritative read及semantic guards保護；不得使history倒退、改寫terminal outcome或將new lineage塞入舊Attempt。

## 5.3 Allocate-with-Artifact Commit

Candidate D的validation／admission成功後，Candidate A在one command-scoped local semantic transaction內：

1. fresh-read並驗證Attempt、Try、Aggregate及lineage；
2. allocate immutable `version_id`；
3. allocate next continuous per-Aggregate `version_number`；
4. bind exactly one `publication_operation_id`；
5. persist immutable Artifact與all required lineage；
6. persist minimal A-side publication receipt／evidence；
7. atomically commit all effects。

Transaction rollback時allocation沒有authority、不消耗public version number。禁止durable allocated-only Version、Version-without-Artifact或Artifact-without-required-A-side-receipt publication target。

## 5.4 A-side Publication Receipt and Result

A-side receipt至少要足以證明publication target已local commit，並支援same-operation replay、cross-domain reconciliation、startup rediscovery及integrity validation。其semantic identity至少涵蓋：

```text
publication_operation_id
aggregate_id
incident_id
target_version_id
expected_current_version_id       # nullable where semantically valid
```

`authoritative now`可為execution input及durable result evidence，但不屬replay identity。Receipt existence只證明Candidate A local side已commit，不代表SPEC-008已applied或full Publication成功。

Candidate A可在收到Candidate E依SPEC-008 public authority取得的deterministic Incident-side result後，durably記錄publication disposition並在合法成功時atomic更新A-side Current。它不得自行查private Incident tables、猜winner或以version number、timestamp、largest ID解讀`PRECONDITION_SUPERSEDED`。

Cross-domain outcomes須能相容於`APPLIED`、`PRECONDITION_SUPERSEDED / OBSOLETE`、`TARGET_ALREADY_CURRENT_CONFLICT`與`REPAIR_REQUIRED` families；exact enum spelling由相應contract決定。只有證明target已成為authorized Incident Current的successful／equivalent result可完成該Version的publication並更新Candidate-A Current。其他結果保留evidence供Candidate E reconcile，不能silent promote。

## 5.5 Transactions and Concurrency

所有mutation均須具command-scoped local transaction、SQLite write serialization、transaction內fresh-read、semantic conflict checks與strict local integrity/readiness gate。Process mutex可作optimization，但不得是唯一correctness mechanism。

Concurrent equivalent commands收斂至same authoritative effect；contradictory contenders只有符合guard的一方可commit，其餘取得typed conflict。不得lost update、duplicate Version number、duplicate Try outcome或multiple Current。Exact SQL、lock或CAS technique deferred。

---

# 6. Public Semantic Capabilities

本節定義semantic capability，不凍結Python method、class、route或DTO spelling。

| Capability | Required semantic result |
|---|---|
| Create or discover Aggregate | 在approved obligation下建立或回傳same Incident-bound Aggregate；區分absence、equivalent replay與identity conflict。 |
| Read Aggregate by Incident／Aggregate | 回point-in-time coherent Aggregate identity、Incident binding及summary；malformed binding Fail Closed。 |
| Admit Attempt | 保存immutable lineage；equivalent replay回same Attempt，contradictory lineage回typed conflict。 |
| Read Attempt and Try lineage | 回immutable core、integrity-aware coherent lifecycle summary及ordered durable Try outcomes，不把physical invocation當Try；summary與authoritative Attempt／Try／Artifact facts矛盾時Fail Closed。 |
| Record／read Logical Try outcome | Append one authoritative typed outcome or replay it；contradiction Fail Closed。 |
| Commit validated Artifact | Atomic allocate Version／number／Artifact／publication identity／A-side receipt；equivalent command回same result。 |
| Read Version／history | 依identity或Aggregate回immutable Version、continuous order、role及lineage；支援完整history。 |
| Read Artifact provenance | 回Artifact、Evidence Snapshot／Revision、Knowledge Snapshot、Attempt與non-secret generation provenance。 |
| Read Current RCA | 回explicit absence或coherent Current Version＋Artifact；不得以latest number猜Current。 |
| Read Current freshness／lineage | 回FRESH／STALE、material-evidence revision basis、Current lineage及Candidate-A-authoritative refresh／Attempt outcome inputs。 |
| Apply authorized freshness fact | 依B-owned materiality／revision fact更新Candidate-A freshness；不得自行infer materiality。 |
| Read publication operation | 回target identity、A-side receipt、known disposition與reconciliation facts；malformed receipt不等於absence。 |
| Complete authorized publication | 消費E／SPEC-008 authoritative result，在合法precondition下更新Current或回typed conflict／repair requirement。 |
| Enumerate recovery candidates | 完整列出committed-but-unpublished Versions、unresolved publication operations、Attempt／Try reconciliation candidates及relevant stale Current facts。 |
| Validate local readiness／integrity | 驗證schema recognition、store integrity、identity、lineage、receipt、Current與enumeration可靠性。 |

這些reads須支援Candidate E／SPEC-011 authoritative discovery、D9 projection inputs及D10 evaluation／audit inputs。Public consumer不得依賴private SQLite layout或取得writable primitive。Large history的pagination／filter shape deferred，但不得造成silent omission或incoherent page semantics。

---

# 7. Replay, Idempotency and Crash Semantics

| Case | Authoritative evidence | Legal behavior | Forbidden effect / conflict behavior |
|---|---|---|---|
| Same Aggregate／Attempt command replay | immutable identity及durable record | 回same ID／result | 不建立duplicate；contradiction typed conflict |
| Same Try outcome replay | `(attempt_id, try_ordinal)`＋stored outcome | equivalent回原outcome | 不append第二outcome；different outcome Fail Closed |
| Version allocation crash before commit | no committed Version／receipt | same command可重新執行；未commit number無authority | 不得暴露gap或allocated-only Version |
| Artifact/A-side receipt commit後response lost | Version＋Artifact＋receipt | read／replay same command回original identities | 不配置replacement Version或publication ID |
| Crash before SPEC-008 publication completes | A-side receipt＋Incident public read/result | Candidate E以same publication identityreconcile | 不direct-write Incident、不建立replacement Artifact |
| Same publication operation replay | receipt identity＋known Incident-side result | equivalent lookup/replay收斂至same publication effect | contradictory target／precondition Fail Closed |
| Stale caller precondition | fresh A Current＋SPEC-008 result | 回superseded／obsolete或conflict facts供E處理 | Candidate A不猜new winner、不覆寫Current |
| Repeated restart | durable Aggregate／Try／Version／receipt／Current facts | repeated discovery與reconciliation idempotent | 不reset identity、freshness、history或retry slots |
| Ambiguous provider result | no authoritative terminal Try outcome | D7/D8允許same Logical Try的physical reinvocation | 不宣稱external exactly-once |
| Terminal Try outcome already durable | authoritative Try outcome | read/reconcile existing result | 不建立第二provider-result authority |

系統保證避免duplicate authoritative semantic effect與duplicate published semantic effect；不保證exactly-once external network invocation。

---

# 8. Startup and Recovery Support Boundary

Candidate A不執行Startup Recovery，但必須提供足夠authoritative facts，使Candidate E／SPEC-011能：

- 發現missing／stale Runtime work，而不把Runtime Work當RCA truth；
- discover committed-but-unpublished Version及其original `publication_operation_id`；
- reconcile Attempt／Try state與authoritative terminal outcome；
- 讀取Current、freshness及lineage；
- resume而不配置replacement Attempt、Version、Artifact或publication identity；
- 在處理同一subject的新refresh／generation前，先收斂可能已有committed publication side effect。

Ordering、work reconstruction、retry slot consumption、wake time與barrier governance屬Candidate E／SPEC-011。若Candidate A無法可靠enumerate或classify自己的authority，受影響RCA execution必須Fail Closed；Candidate A不得因此定義whole-platform READY。

---

# 9. Failure, Integrity and Fail-Closed Semantics

## 9.1 Semantic Failure Families

Exact enum spelling deferred，但public contract必須typed區分：

| Family | Meaning |
|---|---|
| Legitimate absence / NOT_FOUND | 經完整可靠read確認requested object不存在。 |
| Semantic conflict | Command與合法既有state／precondition不相容。 |
| Identity／lineage conflict | Stable identity解析至不同immutable binding。 |
| Receipt replay conflict | Same operation identity帶入contradictory target semantics。 |
| Invalid reference | Request reference不存在、wrong-domain或不符合admission precondition。 |
| Integrity corruption | Persisted structure或cross-record invariant矛盾。 |
| Schema incompatibility | Store schema malformed、unknown或不安全。 |
| MIGRATION_REQUIRED | Recognized older schema，需要explicit governed migration。 |
| REPAIR_REQUIRED | Authority contradiction無法由safe replay解決，需governed repair。 |
| Local readiness failure | RCA store無法可靠open、read、write或enumerate。 |
| Publication evidence inconsistency | A-side receipt、Version、Current或Incident public evidence不能一致解釋。 |

`Not Found ≠ corruption ≠ contradiction ≠ unavailable`。不得以generic exception text作semantic classification。

## 9.2 Mandatory Fail-Closed Conditions

至少以下狀態必須Fail Closed並保留evidence：duplicate Incident→Aggregate、changed Attempt core、Attempt lifecycle summary與authoritative Try／Artifact facts矛盾、duplicate／gapped committed version order、Version／Artifact mismatch、missing required lineage、duplicate／contradictory Try outcome、Version↔publication identity mismatch、dangling receipt、contradictory publication target、multiple Current、Current指向wrong Aggregate／noncommitted Version、freshness無basis、unknown／malformed schema、failed SQLite integrity check或incomplete recovery enumeration。

不得silent skip、last-write-wins、guess-and-rewrite、automatic replacement identity或destructive automatic repair。可安全隔離的per-record failure可回REPAIR_REQUIRED而不污染無關record；若store-wide authority不可可靠判斷，local readiness必須fail。

---

# 10. Schema Version and Local Readiness

Store須有明確schema-version recognition：

```text
current recognized schema → continue after integrity validation
recognized older schema   → MIGRATION_REQUIRED
unknown / malformed / integrity-unsafe schema → Fail Closed
```

禁止silent automatic migration。New empty path的explicit initialization與existing older schema migration是不同語意；exact bootstrap／migration mechanism deferred。

Local readiness至少驗證：database可可靠存取、schema shape／version可辨識、SQLite integrity、所有authoritative records可decode、uniqueness與referential invariants、receipt／target一致性、Current唯一性及recovery enumeration完整性。此結果只代表Candidate A RCA-store readiness，不代表provider、Knowledge Index、credentials或whole platform READY。

---

# 11. Retention and Security Boundaries

## 11.1 Retention

PoC採full RCA-domain retention：Aggregate、Attempt core／summary、all Try outcomes、all committed Versions／Artifacts、Current／Historical roles、freshness lineage、publication receipts／results及recovery evidence均不得因age、supersede、failure、restart或Incident closure自動刪除。Normal Runtime不提供cleanup／reset。Production archive／deletion policy需另行authority；physical compaction只有logically lossless且不破壞identity、history、receipt與recovery時才可作Implementation Choice。

## 11.2 Secrets and Ground-truth Isolation

Artifact、Attempt、failure、receipt與telemetry-facing reads只能保存non-secret Credential／Profile identity；不得保存credential value。

Candidate F可透過public read觀察與評分，但不得mutation Candidate A。Production persistence嚴禁：`scenario_id`、`evaluation_run_id`、expected root cause、accepted answer、validator expected answer、competition Ground Truth或等價answer leakage。Evaluation Run identity不得替代Attempt、Version或publication identity。

---

# 12. Cross-SPEC Contracts

## 12.1 SPEC-008 — Incident Authority

Candidate A擁有RCA Artifact／Version truth；SPEC-008擁有Incident RCA relationship、`rca_status`、`rca_ref`、Incident-side publication receipt／result與Current relationship integrity。Candidate A不得direct-write Incident persistence。Publication是以same logical operation identity進行的cross-domain reconciliation，不是shared transaction。

Publication intent須能表達`incident_id`、`target_version_id`、`publication_operation_id`、nullable `expected_current_version_id`及execution input `authoritative now`。Candidate A的replay identity不得把`now`納入。Exact SPEC-008 request/result shape不由本文件凍結。

## 12.2 SPEC-011 — Singular Runtime Authority

Candidate A exposes authoritative RCA state；SPEC-011 owns scheduling、retry timing／budget、recovery、clock、startup及cross-store coordination。Candidate A不得建立scheduler、retry DB、Runtime clock或startup recovery framework，也不得把local readiness命名為whole-platform READY。

## 12.3 Candidate B and C — Snapshot Authorities

Candidate A只保存exact lineage references。Candidate B擁有Evidence Snapshot、Evidence Revision、trusted-core與Materiality；Candidate C擁有Knowledge Snapshot、retrieval／applicability、Knowledge Gap及MATCH／NO_MATCH等semantic truth。

## 12.4 Candidate D — Generation and Validation

Candidate D validates generation並決定typed failure／retry safety。Candidate A durably records admitted result及Try outcome，不成為另一套schema／reference／grounding engine，也不保存未validated prose為successful Artifact。

## 12.5 Candidate E — Protocol Composition

Candidate E composes initial／refresh work與publication／reconciliation protocol。Candidate A只擁有自己的local authoritative side effects、receipts與reads，不決定cross-domain ordering或winner。

## 12.6 Candidate F and G

Candidate F可observe／grade但不可mutate Candidate A，Ground Truth不得進production store。Candidate G擁有shared capability／Credential Profile facts；Candidate A只保存non-secret identity並擁有RCA persistence integrity，G不決定Candidate A business state。

---

# 13. Acceptance Criteria

## AC-012-A — Aggregate Cardinality and Binding

- Approved obligation可在Attempts／Versions皆為zero時建立durable Aggregate。
- One Incident最多一個Aggregate；Aggregate immutable綁定exactly one Incident。
- Equivalent replay回same identity；contradictory binding Fail Closed。

## AC-012-B — Attempt and Lineage Immutability

- Attempt保存exact Evidence Snapshot／Revision、Knowledge Snapshot、non-secret Profile及required generation provenance。
- Admitted core不可改寫；new Material Evidence generation建立new Attempt。
- B／C-owned truth只被reference，不由Candidate A重算。
- Durable Generation Lifecycle只接受`PENDING／GENERATING／COMPLETED／FAILED`；`PARTIAL`不得作為lifecycle state，`FULL／DEGRADED` Evidence Completeness不得取代lifecycle。
- Lifecycle summary與authoritative Attempt／Try／validated Artifact lineage保持coherent；restart或Runtime Work缺失不得將它重設為`PENDING`。
- Persistent lifecycle／durable-fact contradiction必須Fail Closed／`REPAIR_REQUIRED`，不得silent normalize、last-write-wins或guess。

## AC-012-C — Logical Try Durable History

- `(attempt_id, try_ordinal)`在restart後仍能讀取one authoritative typed outcome。
- Equivalent replay不duplicate；contradictory outcome Fail Closed。
- Logical Try與physical invocation分離，Candidate A不宣稱external exactly-once。

## AC-012-D — Version Continuity and Atomic Artifact Commit

- Successful admission在one local commit配置Version ID、next number、Artifact、publication identity及A-side receipt。
- Rollback不消耗number；不存在allocated-only Version或Version-without-Artifact。
- Per-Aggregate numbers continuous、immutable、unique且never reused；failed Attempt不消耗number。

## AC-012-E — Artifact and Provenance Immutability

- Same Version不可解析為different Artifact、Aggregate、number、Attempt或lineage。
- Artifact read可完整resolve Evidence／Knowledge／generation provenance而不複製外部snapshot authority。
- Completed Artifact只能以new Version修正。

## AC-012-F — Publication Receipt and Replay

- One Version與one publication operation雙向唯一，A-side receipt足以same-operation replay與startup rediscovery。
- Equivalent replay回same target/result，不產生duplicate Version／receipt／publication effect。
- Contradictory target或receipt、dangling target及cross-domain evidence inconsistency Fail Closed。

## AC-012-G — Publication-gated Current

- Artifact local durability不自動使其Published／Current。
- New Version只有在SPEC-008 authorized relationship成功後才可替換Candidate-A Current。
- Stale precondition、superseded或conflicting result不猜winner；交由Candidate E reconciliation。

## AC-012-H — Current, Freshness and Last-known-good

- 每Aggregate最多一個canonical Current，且必須指向same Aggregate committed Version。
- Freshness explicit並保存Material Evidence revision basis；Revision ID差異不被Candidate A自行解讀為Materiality。
- Refresh pending／failure保留可讀last-known-good Current；authorized newer material evidence可使其explicit STALE。

## AC-012-I — Public Semantic Reads

- Aggregate、Attempt／Try、Version／history、Current、freshness／lineage、Artifact provenance及publication result均有purpose-specific coherent reads。
- Reads提供E／SPEC-011 recovery、D9 projection及D10 audit所需facts，不暴露private writable SQLite layout。
- Legitimate absence與corruption／unavailability／contradiction可typed區分。

## AC-012-J — Recovery and Repeated Restart

- 可discover committed-but-unpublished Version、original publication identity、Try outcome及stale Current facts。
- Commit-response-loss與publication crash以same identities收斂，不配置replacement business identities。
- Repeated restart不改Attempt、Try、Version、Artifact、Current、freshness或receipt truth。

## AC-012-K — SQLite Schema, Readiness and Integrity

- Store path可配置且與SPEC-008 physical DB分離；不使用cross-store FK／2PC。
- Recognized older schema回MIGRATION_REQUIRED；unknown／malformed／unsafe schema Fail Closed；不silent migrate。
- Dangling、duplicate、contradictory、multiple-current及store-wide enumeration failure不得偽裝Not Found或Ready。

## AC-012-L — Transactions and Concurrency

- Mutation使用command-scoped local transaction、SQLite write serialization、fresh-read與semantic guards。
- Concurrent mutation無lost Try outcome、duplicate number、conflicting receipt或multiple Current。
- Process mutex不是唯一correctness mechanism。

## AC-012-M — Cross-domain Authority Preservation

- Candidate A不direct-write SPEC-008、不建立second Runtime／retry／recovery／clock authority。
- Candidate D仍決定validation／retry safety；Candidate E仍compose protocol；B／C仍擁有snapshot truth。
- Candidate G facts不成為Candidate A business state。

## AC-012-N — Ground-truth and Secret Isolation

- Candidate F只有read access；evaluation-only identity／answer／Ground Truth不進production persistence。
- Credential secret不進Artifact、Attempt、receipt或failure；只保存approved non-secret Profile identity。

## AC-012-O — Full PoC Retention

- Current、Historical、failed Attempts、Try outcomes、receipts及recovery evidence不因age、supersede、restart或closure自動刪除。
- Normal Runtime沒有destructive cleanup、silent repair或reset path。

---

# 14. Deferred Implementation Choices

## 14.1 SPEC Precision Intentionally Deferred

- Exact request／result type names、enum spelling及pagination envelope。
- Candidate E與SPEC-008 final publication result DTO mapping。
- Artifact serialization envelope，只要能lossless保存Frozen structured semantics。
- Timestamp field naming與canonical representation，只要無時區歧義且符合authority。

## 14.2 Implementation and Configuration Choices

- Table／column／index names、exact SQL、local FK layout與CAS SQL。
- Exact SQLite library／driver。
- UUID／prefix／encoding、database filename、filesystem path及ID generator。
- Python modules／classes、repository package layout、HTTP routes或method names。
- Thread／process lock implementation、connection policy、cache與JSON implementation。
- Explicit migration tooling、Docker volumes與deployment topology。

上述選擇不得削弱observable identity、atomicity、replay、integrity、authority或read semantics。

## 14.3 Test-planning Details Deferred

Test filenames、fixtures、test class／function names、fault-injection framework、coverage organization與performance thresholds留Phase 3 review後的Implementation Planning；acceptance以第13章semantic behavior為準。

## 14.4 Documentation Debt Deferred

Approval後可能需要README、DDS、runtime docs、architecture diagram與implementation setup reconciliation；本Draft不修改它們，也不把pending documentation寫成implemented reality。

---

# 15. Unresolved Semantic Questions

NONE。

本文件未發現D1～D11與active approved upstream authority不能同時滿足的衝突。Exact implementation choices依第14章defer，不構成semantic question。

---

# 16. Implementation Status Honesty and Approval Gate

Repository baseline目前已有：SPEC-008 Incident Core與其既有SQLite store、SPEC-011既有Runtime framework、domain-local receipt／replay／integrity模式。SPEC-008 v1.2 RCA relationship capability與SPEC-011 v1.1 RCA Runtime accommodation均明示Implementation Pending。Repository目前沒有Candidate A RCA Store、Aggregate／Attempt／Try／Version／Artifact persistence或RCA publication pipeline。

Phase 3 semantic review與re-review均已完成，F-001／F-002已關閉，D1～D11持續Frozen。本文件governance state為：

```text
Version: 1.0
Status: Approved — Implementation Pending
```

SPEC-012 v1.0可作為後續Implementation Handoff／Planning的正式baseline；Candidate A production implementation尚未開始。在後續implementation與verification完成前，不得宣稱RCA capability存在、RCA E2E完成或Production Ready。
