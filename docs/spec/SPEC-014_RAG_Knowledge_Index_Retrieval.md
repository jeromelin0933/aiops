# SPEC-014 — RAG Knowledge Index & Retrieval

## Software Design Specification v1.4

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-014 |
| D-1 P0 UNKNOWN Exception Amendment | 2026-10-30 Approved — SPEC Lead富裕正式核准；Authority：PM-AIOPS-COMP-P0-UNBLOCK-20261030-01；D-1 Audit Finding CLOSED；正式例外未啟用，D-2 Live未授權 |
| Competition P0 Shared Demo Amendment | 2026-10-30 Approved — SPEC Lead富裕正式核准；Authority：PM-AIOPS-COMPETITION-SINGLE-DEMO-20261030；不取代既有Approved baseline或構成Demo／Formal PASS |
| Document Name | RAG Knowledge Index & Retrieval |
| Version | 1.4（Phase A Minimum Controls narrow-scope amendment） |
| Status | Approved — Minimum Controls Amendment；Slice 4 Phase B implementation pending；v1.3 Approved migration scope與historical v1.2 Implemented scope保留 |
| Approval Date | 2026-10-23（v1.4 SPEC Lead formal document approval；v1.3及更早approval dates保留） |
| Requirement Authority | PRD-004 v1.1 Approved；PRD-001 v3.6 |
| Migration Authority | PM-RAG-EMBEDDING-MIGRATION-20261022；SPEC Lead Slice 0 authorization |
| Diagnostic Security Authority | PM-DIAG-MINIMUM-CONTROLS-20261022（MODIFY — APPROVED WITH SCOPE SIMPLIFICATION；Parent：PM-RAG-EMBEDDING-MIGRATION-20261022）；SPEC Lead 2026-10-23 formal v1.4 document approval |
| Runtime Authority | SPEC-011 v1.2 |
| Related Incident Contract | SPEC-008 v1.3 Implemented |
| Implementation Owner | 富裕 |
| Approved Amendment | 2026-10-28 Minimum Necessary Competition Scope Reset Amendment on v1.4；Approved；baseline version不變 |
| Scope Reset Approval Record | SPEC Lead 2026-10-28：APPROVE — Minimum Necessary Competition Scope Reset Amendment；依PM-AIOPS-COMPETITION-SCOPE-RESET-20261028採納Consolidated Independent Semantic Audit：PASS／Findings NONE／READY FOR APPROVAL；僅核准本次新增條款，不授權implementation或Live execution |
| Controlled Batching Amendment | v1.4-based Approved — Controlled Batching Narrow Amendment；baseline version不變；不構成Live Authorization |
| Controlled Batching Approval Record | SPEC Lead 2026-10-28正式採納既有Combined Independent Document Audit — PASS／Findings NONE；依PM「APPROVE — SPEC-014 Controlled Batching Narrow Amendment Direction」及最新「APPROVE WITH MINIMUM NECESSARY CONTROLS — SPEC-014 Independent Counting Observation」對正式文件收尾的授權，核准EMD-06、§24.2／§24.3／§24.5／§24.6／§24.6.1與AC-014-AD的既有batching修訂；未新增PM Decision ID，未重新執行Audit，不授權Live execution。 |
| Counting Observation Amendment | v1.4-based Approved — COUNTING_OBSERVATION Minimum Narrow Amendment；核准範圍為§24.2／§24.6.1新增Counting條款及必要文件狀態；baseline version不變，不構成Implementation或PM Live Campaign Authorization。 |
| Counting Observation Approval Record | Approval Authority：AIOps SPEC Lead；Approval Date：2026-10-28；PM Authority：APPROVE WITH MINIMUM NECESSARY CONTROLS — SPEC-014 Independent Counting Observation；Decision：APPROVE — SPEC-014 COUNTING_OBSERVATION Minimum Narrow Amendment；正式採納Independent Document Audit：PASS — READY FOR SPEC LEAD FORMAL DOCUMENT APPROVAL；Audit Findings：NONE。僅核准§24.2／§24.6.1新增Counting契約及必要文件資訊／History／Status，不新增PM Decision ID，不授權Implementation或任何Live Provider Calls。 |
| Prototype Local Host Amendment | v1.4-based Approved — Prototype Local Host Simplification Narrow Amendment；Approval Date：2026-10-29；baseline version及既有Approved Amendments不變。 |
| Prototype Local Host Approval Record | Approval Authority：SPEC Lead；Decision：APPROVE — SPEC-014 Prototype Local Host Simplification Narrow Amendment；正式採納Independent Document Audit：PASS — SPEC-014 PROTOTYPE LOCAL HOST SIMPLIFICATION INDEPENDENT DOCUMENT AUDIT；Findings：NONE。PM Authority：APPROVE — Prototype Local Host Simplification。核准原49行新增範圍，Normative Contract不變；文件批准不等於Implementation Acceptance、Host初始化或Live授權。 |
| Prototype Local Host Authority／Scope | PM：APPROVE — Prototype Local Host Simplification；取代先前Option A中要求完整保留Windows Host Validator的部分。正式核准§24.2適用性、§24.6／§24.6.1 Host／SQLite部署及AC-014-AD直接相關補充；起草時僅授權文件的歷史紀錄保留。後續工程依SPEC Lead獨立Implementation Authorization執行，不由文件批准取得Implementation Acceptance、Host初始化或Live授權；不虛構PM Decision ID。 |
| Single Direct Document Embedding Probe Amendment | v1.4-based APPROVED — SPEC-014 Single Direct Embedding Probe Narrow Amendment；Approval Authority：SPEC Lead；Approval Date：2026-10-30；Implementation ACCEPTED；B-01 Independent Narrow Re-Audit PASS／Findings NONE；baseline version不變。 |
| Single Probe PM Authority | APPROVE WITH MODIFICATION — Direct Embedding Single-Input Probe；授權本機隔離Candidate的最小契約／離線工程，不構成Google Live Campaign Authorization。 |

### Change History

**Single Probe Formal Approval — 2026-10-30。** Approval Authority：AIOps SPEC Lead；PM Authority：APPROVE WITH MODIFICATION — Direct Embedding Single-Input Probe；Decision：APPROVE — SPEC-014 Single Direct Embedding Probe Narrow Amendment；ACCEPT — Single Direct Embedding Probe Implementation；CLOSE — B-01 Independent Narrow Re-Audit PASS／Findings NONE。本次正式採納既有獨立審查，未重新執行或虛構Audit；僅更新Approval Metadata／History／Status Labels，已審查技術契約不變。Git Checkpoint待Human執行；Google Live NOT AUTHORIZED。

**Historical Single Probe draft record（以下原文保留，不代表目前仍Pending）：**

**Single Direct Document Embedding Probe — Competition P0（2026-10-30）：DRAFT — SPEC Lead Approval／Affected-scope Independent Contract and Implementation Audit Pending。** PM Authority：APPROVE WITH MODIFICATION — Direct Embedding Single-Input Probe；SPEC Lead授權隔離Candidate中的最小文件／Source／Tests修訂，不構成正式Amendment Approval或Live Authorization。原Approved Baseline、Controlled Batching／Counting／Host Approval及歷史紀錄保留；F1 Document Audit BLOCK、Option B Scope Reduction、Integrated Audit X1 BLOCK與最新Option B／Counting／X1 Development STOP決策均保留為歷史，不帶入停止中的Cross-operation自動豁免，不新增PM Decision ID。

| Version | Date | Status | Change |
|---|---|---|---|
| v1.4-based P0 approval closure | 2026-10-30 | Approved — SPEC Lead | SPEC Lead富裕正式核准D-1與Single Shared Demo Narrow Amendments；依PM-AIOPS-COMP-P0-UNBLOCK-20261030-01及PM-AIOPS-COMPETITION-SINGLE-DEMO-20261030，Provider→Build Integrated Audit PASS，D-1 Audit Finding CLOSED。僅Approval／Status收尾，技術條款與下列Draft及UNKNOWN／NOT EXECUTED歷史保留；正式例外未啟用，新Live未授權，Human Git Checkpoint待執行。 |
| v1.4-based P0 D-1 draft | 2026-10-30 | Draft — SPEC Lead Formal Approval Pending | 依PM-AIOPS-COMP-P0-UNBLOCK-20261030-01及本次Human Review確認，僅新增精確歷史UNKNOWN對指定新授權Campaign的條件式Admission分類；舊Row／Hold／Raw Fence不改，Default Denied、獨立Live grant、Atomic Reservation及其他Fence保留，不建立新Store或啟用正式例外。 |
| v1.4-based single shared demo draft | 2026-10-30 | Draft — SPEC Lead Approval Pending | 依PM-AIOPS-COMPETITION-SINGLE-DEMO-20261030增補AC-014-X、§24.5～24.7的Competition適用性；保留正式Four-member／Production、Batching、UNKNOWN Fence與歷史結果；新增最小共享Demo Evidence Contract，不授權新Live或Fence解除。 |
| v1.4-based prototype local host amendment | 2026-10-29（approval date） | Approved — Prototype Local Host Simplification Narrow Amendment | SPEC Lead採納Independent Document Audit PASS／Findings NONE並依PM Authority正式核准原49行新增契約；僅完成Approval Metadata／Status收尾，不改Normative Contract。以下歷史Draft／PENDING／BLOCK及既有Approval保留，不宣稱Host Implementation PASS、SQLite已初始化或PM Live Authorization。 |
| v1.4-based prototype local host draft | 2026-10-29（draft date；非approval date） | Draft — Independent Narrow Document Audit／SPEC Lead Amendment Approval Pending | 依PM「APPROVE — Prototype Local Host Simplification」提出Non-Elevated vincent Runner、依Token屬性與實際能力拒絕、LocalApplicationData Known Folder單一SQLite authority及Fresh-only簡化初始化；明列PM接受的同帳號竄改風險與最小驗收。僅調整Host／Identity threat model，既有Counting／Batching／Provider／Formal契約及歷史紀錄保留；未授權程式修訂或Live。 |
| v1.4-based counting observation amendment | 2026-10-28（approval date） | Approved — COUNTING_OBSERVATION Minimum Narrow Amendment | SPEC Lead正式採納Independent Document Audit PASS／Findings NONE並依上列PM Authority核准§24.2／§24.6.1新增Counting契約；本輪僅作Approval Status Reconciliation，已審查技術正文不變，下列draft、Controlled Batching／Scope Reset／v1.4及歷史Approval／Audit／BLOCK／Remediation紀錄保留。Counting Runner未實作，Live仍NOT AUTHORIZED／NOT EXECUTED，不產生Provider、G8、Knowledge或Formal PASS。 |
| v1.4-based counting observation draft | 2026-10-28（draft date；非approval date） | Draft — Independent Document Audit／SPEC Lead Amendment Approval Pending | 依PM已核准的Independent Counting Observation方向與最低控制（D2／D3；D4授權的batching Approval收尾已保留），於§24.2／§24.6.1新增單筆COUNTING_OBSERVATION scope、固定input／payload commitments、operation-specific admission及result／usage邊界。完整54→54、既有安全／Knowledge／Runtime／Formal authority不變；本輪只修文件，不授權Implementation或Live，不宣稱Audit／Provider PASS。 |
| v1.4-based batching amendment | 2026-10-28（approval date） | Approved — Controlled Batching Narrow Amendment | SPEC Lead依本次PM授權採納既有Combined Independent Document Audit PASS／Findings NONE，僅完成既有batching修訂的Approval／Status收尾；技術條款不變，下列draft及歷史Approval／BLOCK／Audit／Skip紀錄保留。不新增Counting條款，不產生Provider／RAG／RCA或Formal PASS，不授權Live。 |
| v1.4-based batching draft | 2026-10-28 | Draft — Independent Document Audit／SPEC Lead Approval Pending | 依本次PM Controlled Batching方向核准及SPEC Lead文件修訂授權，窄幅修訂EMD-06、§24.2／§24.3／§24.5／§24.6／§24.6.1與AC-014-AD：由單次54-item request改為有限Sequential Synchronous Batching，維持完整54→54與安全／Free Tier控制；不新增Decision ID，不授權implementation或Live，不改歷史approval／audit／BLOCK／skip／NOT EXECUTED紀錄。 |
| v1.4-based amendment | 2026-10-28（approval date） | Approved — Minimum Necessary Competition Scope Reset Amendment | SPEC Lead正式採納Consolidated Independent Semantic Audit PASS／Findings NONE並核准本次新增條款；僅完成Approval／Status收尾，保留下列draft與歷史approval；不構成Provider／Implementation／four-member／Activation或Formal PASS，不授權Live Campaign。 |
| v1.4-based draft | 2026-10-28 | Draft — Scope Reset Approval Pending | 依PM-AIOPS-COMPETITION-SCOPE-RESET-20261028僅增補AC-014-X、§24.5、§24.6／§24.6.1的Milestone A適用方式；未取得Amendment Final Approval，不授權implementation或Live execution。 |
| 1.4 | 2026-10-23（approval date） | Approved — Minimum Controls Amendment；Phase B implementation pending | SPEC Lead採納`PASS — MAJOR-01 / MAJOR-02 CLOSED; SLICE 4 PHASE A INDEPENDENT RE-AUDIT PASS; READY FOR SPEC LEAD REVIEW`，正式`APPROVE — SPEC-014 v1.4 Minimum Controls Narrow-scope Amendment`。批准範圍僅為§24.6.1與必要文件治理；PM-DIAG security simplification與SPEC文件approval分屬不同authority。前次BLOCK／remediation及下列同版revision紀錄保留；不授權Live Google Provider invocation。 |
| 1.4 | 2026-10-23（revision date；非approval date） | Draft — Phase A Independent Re-audit Pending | 依PM-DIAG-MINIMUM-CONTROLS-20261022及SPEC Lead Phase A授權，對§24.6.1增補minimum controls；本次MAJOR-01／02 narrow remediation同步版本／authority／status與Authorization Record必填契約。前次Phase A document audit為BLOCK，尚無v1.4 approval或PASS；v1.3及更早歷史紀錄保留。 |
| 1.3 | 2026-10-22 | Approved — Migration Implementation Pending | PM-RAG-EMBEDDING-MIGRATION-20261022及SPEC Lead Slice 0授權；§24正式納入narrow-scope embedding compatibility contract及EMD-01～10 reconciliation。D1～D4、G1～G4及歷史v1.2 implementation／AC-014-X skip完整保留；新provider／calibration／four-member／activation evidence未取得。 |
| 0.1 | 2026-09-25 | Draft | 將Phase 0 Repository Reality Audit及Frozen D1～D4 formalize為Candidate C Engineering Contract；未進行implementation。 |
| 1.0 | 2026-09-25 | Approved — Implementation Pending | Phase 3 Re-review PASS；D1～D4 Engineering Contract frozen；F-014-01～F-014-06全部closed；正式核准進入implementation planning，implementation尚未開始。 |
| 1.1 | 2026-09-26 | Draft — Governance Amendment Review Pending | Additive formalize Production Knowledge Governance G1～G4；保留v1.0 Approved engineering baseline及Frozen D1～D4。Slices 1～5已完成並通過各slice audit；Slice 6因production corpus governance gap暫停。本amendment尚待read-only semantic review。 |
| 1.1 | 2026-09-26 | Approved — Implementation In Progress | Production Knowledge Governance Amendment核准；G1-A～G4-A正式納入SPEC-014 contract，Semantic Review／Re-review PASS。v1.0 historical approval及Slices 1～5 implementation evidence維持有效；Slice 6仍pending Scope Freeze re-entry／implementation。PRD semantics及Frozen D1～D4均未改變。 |
| 1.2 | 2026-09-26 | Implemented — Documentation-only Closure | Post-integration reconciliation：記錄approved six-document corpus、governed manifest、Google embedding／Chroma adapters、local lifecycle CLI、Knowledge Snapshot及public provenance已實作。AC-014-X未執行，依PM指示以explicit verification exception完成current repository closure；不修改Approved AC或Frozen D1～D4／G1～G4。 |

### Status Honesty

**Prototype Local Host current approval status — Approved，2026-10-29：** SPEC Lead已採納Independent Document Audit PASS／Findings NONE並正式核准Amendment。Implementation Acceptance尚未取得，正式SQLite NOT INITIALIZED，Host Readiness未驗證，PM Live Authorization NOT AUTHORIZED。以下Draft status及其原Authority／Scope保留為核准前快照，不代表現行文件仍Pending；本次文件收尾不代表工程或Host執行完成。

**Historical Prototype Local Host draft status（以下原文保留）：**

**Prototype Local Host current status — Draft，2026-10-29：** PM已核准Simplification Direction；本SPEC窄幅Amendment尚待Independent Document Audit及SPEC Lead正式Approval，不得當作Approved Contract或Implementation Authorization。以下新增的Host條款只定義待審目標，不宣稱現行程式已支援。依SPEC Lead本次交接，既有Counting Offline Implementation已ACCEPTED／Independent Implementation Audit PASS，Human Source Checkpoint為`aa89808153374b6873df33e6a45c501f6170e338`；這些既有成果不重判，也不代表本次Host改版已實作。現行Human Runner Precheck因Deny-only Administrators被BLOCKED；正式SQLite NOT INITIALIZED；Human已觀察`C:\ProgramData\AIOps`不存在，執行前仍須重查。Host Ready未取得，Google Live Calls = 0，PM Live Authorization = NOT AUTHORIZED；Counting／Embedding Provider Compatibility與G8均無新增PASS，Formal Evaluation仍NOT EXECUTED。下列Counting approval-time「NOT IMPLEMENTED」及其他歷史status原文保留為當時快照，不覆蓋本段最新交接狀態。

**COUNTING_OBSERVATION current status — Approved，2026-10-28：** COUNTING_OBSERVATION Contract：APPROVED；SPEC Lead已正式採納Independent Document Audit PASS／Findings NONE並完成本次文件核准，僅作狀態收尾，技術契約不變。Counting Runner Implementation：NOT IMPLEMENTED；Offline Implementation PASS：未取得；Counting Live：NOT AUTHORIZED／NOT EXECUTED；Provider Counting Compatibility：NOT VERIFIED；Embedding 54→54 Compatibility：NOT PASS；G8：NOT PASS；本次新模型Knowledge Build／Activation：NOT EXECUTED；Formal Evaluation：NOT EXECUTED。所有Live Google Provider Calls仍為NOT AUTHORIZED；文件Approval不代替後續Implementation授權或PM Live Campaign Authorization。Controlled Batching Approval及歷史紀錄不變；下段只保留當時Counting draft狀態。

**Historical COUNTING_OBSERVATION draft status（原文保留；已由上列正式Approval收尾）：**

**COUNTING_OBSERVATION current status — Draft／Audit及Amendment Approval Pending，2026-10-28：** PM最低控制方向已核准，本輪僅提出下列窄幅文件草案；Controlled Batching的正式Approval不因此退回Pending。PM方向、文件Amendment Approval、後續Implementation及首次PM Live Campaign Authorization分別記錄；既有offline candidate evidence不是Provider Acceptance或Live Token Evidence。歷史Approval／Draft／BLOCK／Skip／NOT EXECUTED紀錄保留，不要求交接報告必須實體存放Repository。

**Controlled Batching current status — Approved，2026-10-28：** 本次SPEC Lead依上列PM Authority正式採納既有Combined Independent Document Audit PASS／Findings NONE並核准既有batching修訂；SPEC-014、PRD-004 §13.5第3點與SPEC-017 §19.4已完成對應Approval Status Reconciliation。僅更新Approval／Version History／Status，baseline version及已審查技術語意不變。下段為當時draft狀態的歷史紀錄，不代表目前仍Pending。既有Implementation Audit及Human Source Checkpoint `7d0000a472e51572a522b6d7472241ecb24d5eef`依SPEC Lead交接紀錄保留，不由本次文件批准重新判定；Provider Compatibility仍為NOT EXECUTED，不形成Live Readiness或Production Ready，首次PM Live Campaign Authorization仍須另行取得。本次不新增COUNTING_OBSERVATION契約或實作。

**Historical Controlled Batching draft status（原文保留；已由上列正式Approval收尾）：**

**Controlled Batching draft — 2026-10-28：** 本次PM僅正式核准「APPROVE — SPEC-014 Controlled Batching Narrow Amendment Direction」；SPEC Lead授權本輪文件修訂，Independent Document Audit及Amendment Final Approval仍待完成。以下標示的batching修訂為待審草案，尚不取代已核准契約；原單次54-item／no split契約可由修訂前Human Checkpoint `39d199b92187f70da9f54e770af34df37ec13da1`核對。既有v1.4與Scope Reset approval完整保留，不挪用為batching approval。本checkpoint已包含Diagnostic Offline Implementation及`FREE_TIER_TEXT_V1`修復，依SPEC Lead提供的Acceptance／Independent Audit PASS／Human Commit紀錄確認；這不等於batching已實作或Provider Compatibility PASS。Provider／token安全缺口不得由方向核准補成PASS；不必以Google書面回覆作為離線工程前置條件，但Live前仍須具備必要安全上界及另行PM Campaign Authorization。依本輪跨文件最小修訂授權，PRD-004 §13.5第3點與SPEC-017 §19.4已在草案層級對齊本batching contract，三份文件待一次Combined Independent Document Audit及各自正式Approval；PRD-004仍須PM／文件權責人核准，不宣稱已完成正式跨文件reconciliation。

**2026-10-28 approved amendment：** SPEC Lead依PM-AIOPS-COMPETITION-SCOPE-RESET-20261028採納Consolidated Independent Semantic Audit PASS／Findings NONE，正式核准本次新增的Scope Reset適用性條款，在明列的Milestone A範圍優先適用；v1.4 baseline version及原核准範圍保留。歷史Approval／Audit／BLOCK／Skip／NOT EXECUTED紀錄保留；本次不產生Implementation、Provider Compatibility、four-member、Activation或Formal PASS，也不授權Live Provider。Milestone A／B不等同歷史Slice 4 Phase A／B。

**v1.4 current document status：Approved — Minimum Controls Narrow-scope Amendment（SPEC Lead formal approval：2026-10-23）。** 最新PM以`PM-DIAG-MINIMUM-CONTROLS-20261022`核准Diagnostic Security Governance Simplification；SPEC Lead另於本次決策採納Phase A Independent Re-audit `PASS — MAJOR-01 / MAJOR-02 CLOSED`後批准v1.4文件，兩種approval不可互代。前次Phase A document audit的`BLOCK — DOCUMENT CONTRACT / PM AUTHORITY FINDINGS REQUIRE REMEDIATION`及remediation evidence仍作歷史紀錄，不回溯刪改。v1.3 migration amendment及下列v1.2狀態段落、§22、§23保留historical scope/evidence，其Implemented與既有checkmarks不得套用到新模型。Current diagnostic security authority為最新PM決策；migration其餘未完成Gates仍依v1.3 §24與parent PM authority。v1.4文件批准不授權Slice 4 Phase B實作或Live Google Provider Execution；後者仍為`NOT AUTHORIZED`。Provider Compatibility、Calibration、new Build／Activation與SPEC-017 readiness均未PASS；Formal Evaluation為`NOT EXECUTED`且Slots Consumed = 0。

> **Draft ≠ Approved；Approved ≠ Implemented；Slice implementation ≠ RCA E2E complete。**

**SPEC-014 v1.0 Engineering Contract與v1.1 Production Knowledge Governance amendment維持historical authority；v1.2為documentation-only closure，Current Status為`Implemented`。** Repository現已包含approved six-document corpus、governed manifest、Google `text-embedding-004` adapter、Chroma index adapter、build／validate／activate／retrieve CLI、Knowledge Snapshot與public provenance。AC-014-X未執行，依PM指示作current closure的explicit verification exception；這不是PASS，也沒有four-member evidence。Default regression亦不構成live Google provider或real-RAG PASS。Candidate D／SPEC-015與Candidate E／SPEC-016現已實作；Candidate E僅透過Candidate-C public Knowledge semantics組合protocol，不擁有Knowledge authority。Candidate F與final real-LLM／RAG evaluation仍Pending，Production Ready為`NO`。

---

# 0. Authority、Purpose與Frozen Decisions

## 0.1 核心責任句

> **Candidate C governs approved Knowledge, freezes one retrieval boundary, and preserves immutable retrieval truth. Runtime decides when recovery executes; RCA domains decide how that truth is consumed.**

Candidate C擁有Knowledge Corpus、Index、Retrieval、Applicability、Knowledge Gap、Knowledge Snapshot及其local capability truth。Candidate C不得成為Evidence、RCA、Incident、Runtime或Evaluation authority。

## 0.2 Authority Hierarchy

```text
PRD-004 v1.1 Approved (v1.0 semantics preserved; migration amendment added)
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
| RCA product requirements | PRD-004 v1.1 Approved | Candidate C實作其Knowledge semantics，不改寫Evidence、RCA或Runtime authority |
| Incident relationship | SPEC-008 v1.3 | Candidate C不得直接寫Incident persistence或`rca_ref` |
| Runtime | SPEC-011 v1.2 | SPEC-011唯一擁有WHEN、ORDER、retry timing／budget、Runtime Clock、recovery execution與Startup Recovery |
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

PRD-004 v1.1依PM-RAG-EMBEDDING-MIGRATION-20261022修訂的current Approved PoC technology direction為：

```text
Embedding baseline:   Google gemini-embedding-2 (Stable Model)
Vector store baseline: ChromaDB
```

Provider／model family substitution或vector-store technology substitution不得silent replacement，必須先經governance review。Adapter／class、SDK／client API、collection naming、filesystem layout、persistent directory naming及exact configuration key names不是本SPEC的Frozen implementation detail。

## 7.2 Shared Credential／Capability Boundary

§7.1新baseline依`PM-RAG-EMBEDDING-MIGRATION-20261022`核准；歷史text-embedding-004 baseline及evidence不回溯改寫。Migration-specific API／SDK／envelope／bounds及evidence requirements見§24.2；baseline approval不代表compatibility PASS。

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

**Competition P0 applicability — Approved（2026-10-30）：** 僅Competition Milestone A依§24.7採一套共享Demo，不以前述四人獨立重建為展示前置。以下完整AC、Formal Portability／Production Acceptance與歷史NOT EXECUTED不變，不將單人Demo記作AC-014-X PASS。

**Scope Reset amendment — Approved 2026-10-28：** Milestone A仍必須完成§24.5所列四人獨立Build→Validate→Activate→Retrieve證據，不得以歷史skip或單人結果替代。A portability evidence不自動構成下列完整Formal AC或Production Acceptance PASS；原AC與historical closure disposition保留。

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

**D-1 P0 narrow acceptance — SPEC Lead Approved。** 指定歷史UNKNOWN經Human確認、精確Row／Evidence Commitment未變，且新獨立Competition Campaign有完整PM Live grant／Review Binding時，僅該舊列可不作目標Campaign的Blocking Fence；Raw Fence及原Usage／Cost Hold仍保留。其他UNKNOWN／TIMEOUT／IN_FLIGHT、錯誤歷史／目標Identity、錯誤Review／Live PM Reference、變更Row／Hold、Source／Credential／Budget不符均BLOCK。新Campaign再度不確定時不得沿用例外。Preflight Raw／Blocking Fence與Atomic reserve語意一致；無Review時舊Authorization Digest及原行為不變。缺可信Provider Usage仍停止完整Campaign，不建立STAGED Index。全部驗證使用隔離SQLite／Synthetic Credential／Mock Transport；不修改正式Row／Authority。

**Single Direct Document Embedding Probe — Approved acceptance。** 依§24.2及§24.6.1的受控Probe Binding：P1從正式Approved Corpus重建Ordinal 52完整Envelope；P2使用Pinned SDK實際Embedding Route／768設定／Payload Bytes及SHA-256；P3一筆Input以獨立Input與Payload Commitment綁定；P4有效PM Campaign／Bounds／Budget下允許Exactly 1 Document且不因Exact Token Count未知而拒絕；P5 Mock Response驗證1→1、768、Finite／Nonzero及事前Tolerance的Normalization／Mapping；P6保留8192 Admission Risk Cap預占、1 micro-USD Hold與原始Usage／Outcome。負向：未授權Scope、Wrong Source／Chunk／Payload／Model／SDK／Project／Profile、缺失授權或超出一筆／一次、無效Cost／Usage Budget均BLOCK；Duplicate／UNKNOWN／TIMEOUT／IN_FLIGHT與真正Embedding UNKNOWN Usage維持Fence。Reservation Persistence Failure在Credential／Transport前拒絕（Calls=0）；Transport後的Persistence Failure如實保存呼叫可能性並停止，不退款／重送。未核准Caller缺少原Token／Payload Evidence仍Fail Closed；Probe不自動觸發54→54或任何第二Request，不構成Full Compatibility／Knowledge／G8／Formal PASS。所有驗證使用隔離Store及Synthetic Credential／Mock Transport，正式Store不讀寫。

Embedding／provider contract tests驗證timeout、request／batch bound、rate／quota及cost／resource protection與typed exhaustion facts。SDK hidden retry停用或可證明計入唯一Runtime-authorized budget；Candidate C沒有durable retry scheduler、clock或第二budget。

**Controlled Batching amendment — Approved：** 新模型依§24.2驗證固定Sequential Synchronous Batching、每request最多一次physical attempt／30秒、campaign累積限額及全域54→54完整mapping／order；partial result、未授權split或UNKNOWN／TIMEOUT後自動繼續均不得通過。既有domain／retry authority不變。

**Prototype Local Host amendment — Draft：** Diagnostic Host／Local Identity／SQLite部署須依§24.6.1末段的Prototype契約及最小驗收核對；僅修訂直接受影響的Windows admission／部署要求，不降低本AC的Provider Safety，也不改Candidate C／SPEC-011 Runtime ownership。該段尚待Audit與正式Amendment Approval，不能以本次方向核准放行Live。

## AC-014-AE — Finalized Unavailable Knowledge Lineage

Transient provider failure不建立unavailable Snapshot；只有authoritative terminal degraded／unavailable finalization可為既有frozen operation建立exactly one Snapshot，且包含`source_status=UNAVAILABLE`、`resolution=RETRIEVAL_UNAVAILABLE`、selected non-secret Profile、zero retrieved refs及required lineage。Equivalent replay回傳same Snapshot；contradictory finalization及`INVALID / REPAIR_REQUIRED` Fail Closed。

## AC-014-AF — Referential-Integrity Retention Floor

Published RCA、materially relevant failed Attempt、outstanding Retrieval Operation或unfinished reconciliation／recovery仍引用Snapshot時，即使build不再Active、new activation、restart或age增加，Snapshot及required content／build lineage仍可read及deterministically resolve。

## AC-014-AG — Approved PoC Technology Baseline

Migration PoC implementation及real integration verification使用Stable Google `gemini-embedding-2`與ChromaDB，依`PM-RAG-EMBEDDING-MIGRATION-20261022`及§24取得新compatibility evidence。歷史text-embedding-004 implementation不構成本AC新模型PASS。Provider／model family或vector-store technology substitution在沒有governance approval時被拒絕；migration profile的API／SDK及bounds受§24約束，collection及path命名仍為implementation choice。

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
- explicit opt-in Google `gemini-embedding-2`／ChromaDB migration validation，依§24取得新evidence，不納入credential-free default regression；historical text-embedding-004 validation不轉作新模型PASS。

Default regression不得依賴live provider。Fake adapter evidence不得冒充real-RAG Demo成功。

---

# 21. Implementation-deferred Choices

v1.3 clarification：以下既有deferred choices仍受Frozen semantics約束；對本migration，§24明確固定的model／API target／SDK pin／dimension／envelopes／invocation／timeout優先適用，不得以本節自行放寬。未定threshold及numeric tolerance須versioned calibration／review；不構成預先PASS。

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

本節及§23保留v1.2歷史closure scope；v1.3新migration implementation與execution gates見§24，尚未完成。現有source/config仍為舊模型hard pins，屬已識別的後續implementation reconciliation gap，不得依新Contract宣稱現有adapter已相容。

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

---

# 24. PM-approved Embedding Compatibility Migration（v1.3 Additive Amendment）

## 24.1 Authority、Scope與Decision Traceability

Decision ID：`PM-RAG-EMBEDDING-MIGRATION-20261022`；Approval Date：`2026-10-22`；Approval Authority：AIOps Project PM；Decision：`APPROVED — Scoped Compatibility Migration`；Owner：SPEC Lead 富裕。Upstream為PRD-001 v3.6 §11.1、PRD-004 v1.1 §13.5；本工程條文依SPEC Lead Slice 0授權正式納入。

本節僅amend embedding compatibility／verification要求，補充§4.3、§5～§7、§9～§10、§15、§17及AC-014-AG；D1～D4、G1～G4、single activation／LKG、immutable Snapshot、typed resolution、Runtime與Candidate A～E ownership不變。六份Approved Knowledge Sources、Manifest、Corpus Release及Human Knowledge Governance不變。Envelope屬embedding input transformation，不是source revision。

以下EMD-01～10為本migration的決策主題對照；approval只涵蓋設計與必要Gate，並非execution結果：

| Decision | Approved direction／required gate | Contract coverage |
|---|---|---|
| EMD-01 | Replacement Model：Stable gemini-embedding-2取代text-embedding-004；既有RAG Scope不變 | §7.1、AC-014-AG、§24.1 |
| EMD-02 | API Version / SDK：Developer API stable v1及pinned SDK；HTTP compatibility仍待evidence | §24.2 |
| EMD-03 | Dimension / Normalization：768 dimensions、provider normalization；結果須驗證 | §24.2 |
| EMD-04 | Asymmetric Task Semantics / Envelopes：versioned asymmetric document/query envelopes；不傳task_type | §24.2 |
| EMD-05 | Approved Document Title：Document envelope的title使用approved document_id | §24.2 |
| EMD-06 | Multi-content Serialization / Cardinality — Controlled Batching approved amendment：54個獨立types.Content依固定順序作有限Sequential Synchronous Batching→完整54 embeddings；逐筆及全域identity/order驗證；每request一次physical attempt／30秒，無hidden retry／fallback／未授權split | §24.2 |
| EMD-07 | Truncation / Payload / Token Bounds：完整envelope／serialized payload／token checks；不傳auto_truncate；fail closed | §24.2 |
| EMD-08 | New Identities / Vector Isolation：新capability/profile/build identities、獨立collection，不混用新舊vectors | §24.3；downstream Build／Validate／Activation gates見§24.5 |
| EMD-09 | Chroma L2 / Retrieval Calibration：保留Chroma L2；Retrieval／Applicability calibration、policy freeze與資料隔離 | §24.3～§24.4；downstream AC-014-X／Activation gates見§24.5 |
| EMD-10 | Candidate-F Readiness / Fresh Lineage：fresh preregistration／source-bound preparation／Real-RAG readiness；Formal Scope不變 | §24.6、SPEC-017 §19；downstream readiness所需AC-014-X見§24.5 |

## 24.2 Request Contract與Compatibility Evidence

**Single Direct Document Embedding Probe — Competition P0（Approved exception）。** Independent Counting不是前置條件。既有`CONTROLLED_PROVIDER_DIAGNOSTIC`可在完整Compatibility之前，另行取得PM Live授權後執行固定單筆Document Probe；正式Document Diagnostic及固定Batch Plan仍須Six Approved Sources、完整54 Unique Ordered Inputs、逐筆及全域Mapping／Order、768、Finite／Nonzero／Normalization與完整Provenance／Campaign Bounds。單筆例外不是把54→54改成1→1，也不提供Knowledge Build／Activation Authority。

Probe固定Google Gemini Developer API、`gemini-embedding-2`、Stable `v1`、`google-genai==2.25.0`、Output Dimension 768；同步`POST /v1/models/gemini-embedding-2:batchEmbedContents`內恰有一個獨立`types.Content`。Input固定Approved Corpus的Global Ordinal 52、Source `ops.shared-database-network-dependency-interruption`、Version `1.0`、Chunk `kchk_5efd99ff50a380e0f983cc4193bd115f0b19072b7c03054af0165c561db4eb89`；由Manifest／Source重新推導`title: {approved document_id} | text: {chunk_content}`及完整Corpus Commitment `a104796f2e0982e0b591bfe0255a0338cd8468cc0f3d1a0e52b2045026dd7840`。Exact Envelope 528 UTF-8 bytes、Single-input Commitment `1f7c73a6895e0c0b9b089e6ab97bbf720cb3b8151615764d9eb96a63f93a2f12`須重驗。Pinned SDK實際Embedding Body為671 bytes、SHA-256 `b80a0c066b0bae79a77d5b9bcc6277ee47e0d196f6d3b8cbefe7017cd7f1c6ba`，亦須在受控E4及Final Guard重驗；Input Commitment不等於SDK Payload Commitment，不沿用Counting的591-byte Body或Hash。SDK／Content／Route／Bytes／Hash不符即停止，不偷偷更新Golden Evidence。

**PM接受的單筆有限資源風險例外。** 本固定Probe不要求事前Exact Token Count或證明所有未公開Provider System Quota。官方模型Input Limit 8192（https://ai.google.dev/gemini-api/docs/models/gemini-embedding-2）僅作PM接受的`PM_ACCEPTED_MODEL_LIMIT_RISK_CAP`，不是本Input已驗證Token Count、Aggregate Provider Guarantee或實際Usage。Exact Input Token Count=`NOT VERIFIED`；Provider Usage待可信Response；未驗證Item／Aggregate／Payload System Limits明記`UNKNOWN`，不得捏造數字。未知Exact Count或未公開System Limits不單獨否決具完整受控Probe Binding的本例外；完整54→54及其他Embedding仍適用原Token／Provider Bounds規則，不採Bytes→Tokens、Approximate Tokenizer、Multiplier、隱藏Counting或全面關閉Validator。

沿用既有Budget欄位：僅本Probe的`maximum_item_tokens`、`maximum_aggregate_tokens`、`maximum_usage_units`固定8192，單位語意是Admission Risk Cap；SQLite `usage_reserved=8192`，不是Observed Tokens。`usage_observed`僅由既有可信Provider Usage Receipt填入，缺失保持UNKNOWN；不得把Risk Cap填為已觀測Usage。最大Invocation／Item均1，`maximum_item_bytes=528`；PM Record另須指定有限正值Payload Bound（涵蓋實際SDK與既有保守Serialization檢查，且不超過現有本地1,000,000-byte cap），並由E4精確綁定671-byte Body。此本地上限不是Google Provider Limit。Free Tier `FREE_TIER_TEXT_V1`的Unit Price=0，既有每Attempt 1 micro-USD Internal Hold及有限正值Campaign Cost Limit均保留；Hold不是Google Billing或已證明的最壞成本。缺失／不一致的授權、Payload、有限預算、Source／Profile或Reservation仍Fail Closed。

**TRUNCATION VERIFICATION LIMITATION。** Pinned Developer API Converter不支援`auto_truncate`參數；保持原Envelope／Config，不傳送不存在的No-truncation控制，不改模型或Endpoint。實際Request必須是完整核准Content；可確認本機未截短，不能據此保證Provider未截斷。保存可取得的非敏感Response／Usage Provenance與限制標示，不保存Raw Response Body；結果僅`PROBE OBSERVATION ONLY`，不宣稱Full Input／54→54 Compatibility PASS。Provider拒絕、超限、429、不支援、Timeout、Response或Persistence異常即STOP／Typed Finding／No Retry／No Fallback，不縮短輸入、不換Project、不追加Request。

**Controlled Batching amendment — Approved。** 本節下列request/cardinality／bounds／verification修訂僅採本次PM核准方向，不宣告Google已保證任何batch size，也不構成Live Authorization。

**Operation applicability（COUNTING_OBSERVATION approved amendment）：** 本節既有document／query、54→54、vector／768／normalization、batch與Embedding token admission條款維持適用於Embedding Diagnostic及Knowledge相關流程。獨立`COUNTING_OBSERVATION`僅適用本節末段及§24.6.1明列的Counting scope／minimum admission，不以單筆Counting取代上述契約；Counting不是隱藏附加API呼叫，也不因本amendment而取得Live授權。

**Prototype Host applicability — Draft：** §24.6.1末段僅調整本機Host／Runner admission及Diagnostic SQLite部署；不改本節Counting／Embedding的operation、Golden input、commitment、SDK serialization、token／payload admission或完整54→54驗收。Counting仍為獨立Observation，不能以Host preflight或Counting結果完成Embedding Usage Receipt／Compatibility。

- Backend target為Gemini Developer API stable `v1`，model為`gemini-embedding-2`；使用pinned `google-genai==2.25.0`進行離線serialization verification。API target／SDK static support不等於實際HTTP相容；SDK升級不在本baseline內，須另行SPEC Lead engineering approval，不得silent upgrade。
- 明確設定`output_dimensionality=768`，採新模型provider-side normalization semantics。每個vector須驗證dimension、finite values、nonzero norm及依已聲明numeric tolerance檢查normalization；不得以client-side renormalization掩蓋provider mismatch。Provider normalization結果尚未驗證，tolerance須版本化並在結果判讀前固定。
- Document exact template：`title: {approved document_id} | text: {chunk_content}`。Query exact template：`task: search result | query: {canonical_query}`。先依§9.1 canonicalize query，再套固定envelope；兩者使用同一model/profile family。固定literal、空白、encoding及envelope revisions須commitment，不允許依scenario／Ground Truth改prefix。`task_type`與`auto_truncate`均不傳送。
- Document scope維持six approved sources、54 ordered且unique chunk identities及完整input/envelope commitment；每chunk使用獨立`types.Content`。採Sequential Synchronous Batching：執行前固定有限batch plan，包含依全域順序連續分段的batch index、ordered chunk identities／範圍、每batch input commitment及完整plan commitment。各段不得重疊、遺漏或改序，合併後須恰為原54個inputs；不得以`list[str]`可能聚合的serialization替代而未驗證。每batch的逐筆output一對一對應原chunk identity；按batch順序組合後必須完整驗證global 54→54、mapping、order及每vector的dimension／normalization，count相等或HTTP success不足以證明mapping正確。Query仍使用固定query envelope與核准query scope；每個query request為1個independent item，不因document分批擴大query數量或授權。
- 每batch為依固定順序執行的同步physical HTTP request，不平行發送；每request最多一次physical attempt、30秒timeout、no hidden retry／fallback／未授權split。Campaign事前固定maximum batch／physical invocation count，分別列明document batches與query requests並合計，所有已記錄及預占attempt均計入；不得因失敗重給budget。維持每request現有128 units本地上限與1,000,000-byte本地request上限，不得以本地cap或選定batch size作為Google已保證支持的item／payload limit。對timeout、rate/quota、request-too-large、unsupported contract、response shape/cardinality/order/dimension/normalization mismatch等產生typed findings；禁止silent retry、fallback、aggregation、truncation、omission或reordering。失敗或UNKNOWN／TIMEOUT即停止後續batch，不自動重送或繞過控制繼續。
- Bounds檢查須分別涵蓋完整envelope後的每item、每batch aggregate token／UTF-8 bytes、各batch實際serialized request payload（含JSON escaping與必要欄位），並驗證provider item／per-item token／aggregate token／payload限制的適用範圍；另驗證完整campaign的有限invocation／item／token／usage／cost budget及累積占用。Project quota與campaign budget分開記錄，quota不是授權budget；依核准rate／quota限制執行，不推定分批即可滿足TPM等限制。Bytes不等於tokens；須有model-compatible local counting或可證明的保守上界，不以54×8192或近似模型tokenizer冒充已證實上界。未有必要安全證據即fail closed，不得另加隱藏token API invocation；尚未證實的provider limits維持NOT VERIFIED，不捏造上限。
- Offline tests須覆蓋各batch的獨立Content serialization、exact route/config、absence of task_type/auto_truncate、固定plan／全域commitment、retry/timeout、逐batch與campaign bounds、malformed／partial responses及失敗停止；受控provider diagnostic再驗證stable v1、完整54→54及逐筆／跨batch mapping/order、768、normalization與實際request/response behavior。Order verification方法須事先定義，不能只檢查陣列長度；任一batch缺漏、失敗或無法驗證即不得將partial result宣稱為完整Provider Compatibility PASS，亦不得形成完整可驗收Knowledge Build。
- 若實際provider無法滿足本contract，輸出typed incompatibility並STOP；不得在固定且核准的plan外自行拆批、增加invocation、放寬timeout、改backend／API、改用asynchronous Batch API或降低測試要求。變更plan／input／budget須重新核對適用scope與授權，不能沿用stale commitment；需變更正式contract時交SPEC Lead／PM，不由runner自行決定。
- 第一版只要求有限同步分批、完整mapping／order、安全bounds、typed failure與54→54 complete validation。成功batch可在本次執行內暫存供完整結果驗證，但不授予partial publication／activation。Persistent Resume／跨次cache reuse不是必要交付，也未由本amendment授權實作；日後若token重複消耗確有需要，由SPEC Lead另行決定最小Resume scope。不得新增完整Checkpoint Framework、Quota Accounting Domain或Multi-key Pool。

**Independent COUNTING_OBSERVATION — Approved：** 依PM「APPROVE WITH MINIMUM NECESSARY CONTROLS — SPEC-014 Independent Counting Observation」，新增用途明確的獨立Counting Operation，目的僅為取得一筆Approved Exact Document Content的實際model-compatible token counting observation。它不是Embedding Diagnostic、Knowledge Build、Calibration、Formal Evaluation或Production Knowledge Authority，不提供G8 Compatibility PASS，也不改變本節原完整54→54驗收。

| Fixed first observation binding | Candidate／必要核對 |
|---|---|
| Provider／Model／API／SDK | Google Gemini Developer API；`gemini-embedding-2`；Stable `v1`；`google-genai==2.25.0`。不得換模型、改v1beta、silent upgrade或fallback。 |
| Operation／Method／Route | `COUNTING_OBSERVATION`／`models.countTokens`；同步`POST https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:countTokens`；一個獨立`types.Content`、一個text part，內容為原Approved Exact Document Envelope；不附帶Embedding request、output_dimensionality、task_type、auto_truncate、工具或未核准內容。 |
| Source／Version／Global Ordinal | `ops.shared-database-network-dependency-interruption`／`1.0`／`52`。原source／manifest／document version provenance須保留；不得注入Scenario Ground Truth、Oracle或Expected Answer。 |
| Chunk Identity | `kchk_5efd99ff50a380e0f983cc4193bd115f0b19072b7c03054af0165c561db4eb89` |
| Single-input Commitment | `1f7c73a6895e0c0b9b089e6ab97bbf720cb3b8151615764d9eb96a63f93a2f12`；使用既有canonical input commitment規則，須與實際單筆identity／envelope exact match。 |
| Complete Corpus Commitment | `a104796f2e0982e0b591bfe0255a0338cd8468cc0f3d1a0e52b2045026dd7840`；作原Approved Corpus來源綁定，不代表本次處理全部54筆。 |
| Offline candidate size／body commitment | Exact Envelope `528` UTF-8 bytes；Counting serialized body `591` bytes；body SHA-256 `e8b294325b71c9b6732ecd16a05b6e89d8631be2159dcfac5c6963bae4a701cc`。僅為既有Offline Candidate Evidence，不是Token Count、Provider Payload Limit或Provider Acceptance。 |

正式執行前MUST以實際Pinned SDK送出前的request重新核對上述route／method、單筆完整Content、input／body commitments及實際serialized bytes；本次固定input size及payload bound分別為528／591 bytes，須同時exact match，不能只檢查小於上限。任何內容、identity、序列化、API或SDK不一致即拒絕，不自行改寫候選hash或擴大budget。這些數值是PM指定候選的本地admission bounds，不宣稱為Google保證接受的上限；必要授權及其他minimum admission仍依§24.6.1。

`countTokens.totalTokens`只可作Counting Observation Result。須驗證為可信、與本次physical attempt／model／input及request body綁定的非負整數（不接受bool、字串、負數、缺失或malformed response）；保存非敏感result及必要provenance，不持久化完整raw response body或secret。成功Observation只證明該次取得的計數結果；不得擴張為其他53筆、Embedding Actual Usage、完整Embedding Admission或Compatibility PASS。後續採納其作Embedding token evidence時，仍須核對model／Exact Content／計量適用性與獨立授權，不能自動啟動Embedding。

## 24.3 Versioned Identity與Physical Isolation

新Provider Capability、Embedding Profile（含document/query envelope）、Retrieval Profile、Applicability Policy及Real-RAG Gate Profile必須建立新versioned identities，不得沿用舊名稱卻更換語意。Compatibility commitment至少涵蓋backend/API version、model、SDK compatibility baseline、dimension、normalization、envelope revisions及bounds/retry policy；Build Identity依§4.3 commitment實際build inputs，可透過profile commitment涵蓋。

**Controlled Batching approved amendment：** batching semantics／固定plan與其commitment納入適用的compatibility/profile及execution evidence；全54 inputs、source／manifest／model／profile／Build Identity continuity不得跨batch切換。依既有identity規則綁定實際inputs與profile，不另造Build authority、schema升級或虛構digest；舊單次request evidence不自動證明新plan相容。

Chroma L2 physical schema保持；新模型重新embedding並建立獨立collection，不混用舊vectors、不複製generated DB冒充新evidence。New Build Identity由實際canonical inputs衍生，不預填虛構digest。Artifact revision不等於schema version；若schema shape／semantics未變，不為模型遷移任意升schema或改hash algorithm。

`6589815`及`555ee83`相關historical preparation、claims、Chroma、Docker resources與evidence不重用、不修改、不清理、不rebind；原Build／Snapshot／RCA retention與lineage仍受§11.4及AC-014-AB／AF約束。新claim只能屬於明確新授權的authoritative workflow。

## 24.4 Calibration Artifact與Policy Freeze

Calibration Artifact為獨立namespace的vectors／可查詢L2 index及結果commitments，不是Candidate-C STAGED／VALIDATED record，沒有Active／LKG權威，亦不是Candidate-F preparation或Formal evidence。Calibration harness不得呼叫既有Candidate-C durable claim workflow，不掛載既有Candidate-C／Candidate-F stores，不建立production activation pointer；現有stage()會建立claim，不能直接當作此harness。

Artifact須記錄source revision、manifest、ordered chunk identities、embedding/profile/API/SDK、vector/index、dataset/protocol及結果commitments。先以此artifact完成calibration與policy freeze，再獨立建立authoritative Candidate-C build；不得將calibration collection改名、rebind或直接升格為正式build。本baseline不授權vector import捷徑。

Calibration protocol於結果判讀／政策選擇前固定development／held-out分割、positive/negative與knowledge-gap cases、metrics、selection及acceptance criteria。開發calibration資料與Formal Evaluation資料完全隔離；不得使用Formal results或為通過S1～S6調門檻，Ground Truth不得進production query／metadata／retrieval decision。可涵蓋approved operational scenario類別，但不以正式fixture／Oracle labels作為調參資料。

舊`0.2 / 0.5 / 0.8`僅為historical policy，不自動繼承新模型驗證資格。新Retrieval／Applicability Policy須經calibration review後freeze並版本化；確認MATCH、NO_MATCH、SOP_BACKED與Knowledge Gap語意不退化。合法NO_MATCH不得為符合預期答案而改成MATCH；threshold calibration不取代Candidate-F Conclusion Calibration。

## 24.5 Build、Four-member Gate與Activation

**2026-10-30 Competition P0 applicability — Approved：** §24.7限定一套共享Demo的Build／Validate／Demo-scoped Activation／Retrieve交付；下列四人獨立重建對本Competition展示不再是前置，但仍約束Formal Portability／Production Acceptance。既有compatibility、隔離calibration／policy freeze、STAGED→VALIDATED→Explicit Activation、Provenance與LKG要求不豁免。Demo activation不得寫入或冒充Production／Formal activation。

**Controlled Batching approved amendment：** 下列新build／validation與四人獨立rebuild須使用§24.2完整54→54及固定batch-plan-bound evidence。每人仍獨立完成全部54 embeddings，不複製他人generated DB；成功個別batch不是完整Build Validation或AC-014-X PASS。STAGED→VALIDATED→explicit activation、new collection isolation、provenance、LKG及A/B驗收邊界均不變，不新增resume或四人逐batch人工簽核要求。

**Scope Reset amendment — Approved 2026-10-28；Authority：PM-AIOPS-COMPETITION-SCOPE-RESET-20261028。** 本段限定下列原條款中Milestone A Competition readiness的證據範圍；完整Formal／Production Acceptance義務不取消。

Milestone A必須由四名成員使用相同approved Git revision／source／model／manifest／config，各自在本機以自己的合法API Key／profile完成一次獨立Build→Validate→Explicit Activate→bounded Retrieve，不得複製他人成品Chroma DB。每人最小紀錄包含：OS／主要工具版本與Git／非敏感model/config identity、credential profile identity（不含key）、document/chunk數與build ID、validation PASS/FAIL、activation ID，以及至少一組approved SOP query的retrieved chunk/source/version、MATCH/NO_MATCH或typed error。失敗／缺漏不得記為完成四人驗證。

此為A portability驗證，不要求四人LLM文字一致，也不混入固定環境的品質統計。首次未驗證新模型Diagnostic仍依§24.6.1取得PM Campaign Authorization；經驗證且獲准常態操作後，成員在核准操作範圍及安全限額內自行重建／Demo不需per-run PM approval。個人驗證activation不要求AC預先PASS，亦不等於shared Production Activation。A證據不自動授予完整Formal AC-014-X、Common Readiness或Production Acceptance PASS；較完整drift／mismatch及正式契約核對留B。§24.2 compatibility、§24.4 calibration／policy freeze、全新STAGED build／validation、explicit activation及LKG保護均維持。

Calibration freeze之後，Candidate C在新授權scope內建立新的immutable STAGED build，完成§5.3 validation，再依明確authority explicit activate；structural validation或bounded probe不取代retrieval quality calibration。Production activation前必須完成compatibility、calibration、regression／audit及新AC-014-X四人evidence，且fresh驗證target/profile相容性。

新AC-014-X必須取得四名成員在相互隔離個人環境、相同repository revision／approved sources／manifest／config下各自獨立rebuild、drift／mismatch detection、staged validation、explicit governed activation、retrieval／provenance及reproducibility evidence；不得複製另一成員generated Chroma DB。各自的驗證activation屬取得AC evidence的測試步驟，不要求該AC預先PASS，不等於shared Production Activation。如此不形成activation／AC-014-X依賴循環。

新AC-014-X為Migration Production Activation及正式Competition Readiness必要Gate。§19原AC-014-X與Current Closure Disposition逐字保留；historical `NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE`不是新模型例外。新的PASS必須是新的source/profile-bound四人evidence，不能改寫歷史。無法滿足時BLOCK並聯絡PM。

## 24.6 Gate Sequence與Slice 0 Boundary

**Competition P0 applicability — Approved：** 本節歷史Gate圖中的AC-014-X four-member verification對2026/11/07 Competition A依§24.7取代為單一共享環境證據；Formal／Production分支及其全部Gate維持。此Scope Reset不授權新的Google Invocation或解除既有UNKNOWN Fence。

**Competition P0 Single Probe — Approved。** §24.2單筆Probe是完整Provider Compatibility前的限定Observation，不依賴Independent Counting；不取代完整54→54、Knowledge或Formal Acceptance。準備及離線Implementation授權不等於Google Live Authorization，Default Live DENIED。本例外完成後停止，不自動啟動Batch Campaign／Build／Calibration／Activation。

**Prototype Host preparation — Draft：** 在另行取得本次Amendment正式Approval及實作／操作授權後，Host準備採§24.6.1末段五步驟，可由符合條件的Non-Elevated本機Runner完成Fresh初始化，不預設Administrator Bootstrap。此為既有Host readiness工作的精簡，不新增G／S／E Gate；Source checkpoint、首次PM Live Campaign Authorization及後續Provider Acceptance的獨立邊界不變。本輪僅文件草案，不能依本段執行初始化或Provider Call。

**Scope Reset amendment — Approved 2026-10-28：** 下列原順序的安全／資料依賴保留，但Milestone A不要求每個微步驟各自audit或checkpoint。同一整合風險可合併affected regression與一次independent audit；不強制在已涵蓋相同風險的audit外再做獨立Cumulative Audit。Human Git Checkpoint依實際source/config revision及完整change set決定，可覆蓋多項相關修改。首次Live仍須鎖定exact clean committed execution revision；後續policy/config若變更，須記錄實際新revision並重評受影響evidence，不能rebind舊checkpoint或沿用stale證據。

A在compatibility、隔離calibration／policy freeze、新build／validation、explicit activation與精簡四人驗證的必要依賴下完成PRD-004技術交付；Fresh Formal Preregistration、Final Admission及Formal execution屬SPEC-017 §19.6的B branch，不是A前置條件。以下Slice 0限制及原A/B checkpoint名稱保留其歷史scope；本文件amendment不授權任何implementation、Git mutation或Live execution。

```text
PM Approval → Approved Contract
→ Offline Implementation / Regression / Independent Audit / Human Checkpoint A
→ Controlled Provider Diagnostic
→ Isolated Calibration → Policy Freeze / Regression / Independent Audit / Human Checkpoint B
→ New Candidate-C Build / Validate / AC-014-X four-member verification
→ required final regression / audit / authorized remediation
→ Production branch: explicit governed Production Activation
→ Evaluation branch: Fresh Preregistration → fresh preparation-only build / validation / activation
   → fresh Real-RAG / required readiness verification → Final Admission → ADMITTED
```

最後兩行為分支而非先後依賴；Production Activation不是Formal Preparation前置條件。Fresh Preregistration在首次preparation mutation前固定methodology、profiles及operational resolution rules；實際build/activation commitments在受控preparation形成，再由Final Admission解析及fresh驗證。Formal Execution另需execution authority，ADMITTED不是本Slice的execution授權。

Checkpoint A/B由Human建立；source/config不變可指同一commit，policy/config變更則須重新audit並建立B，不rebind A。Controlled Batching approved amendment：Diagnostic有限batch campaign不授權不限次calibration或四人成員rebuild；各操作仍須其明確scope與budget。後續remediation若改source/profile，須重評受影響evidence並重新checkpoint，不沿用stale preparation。

Slice 0只授權四份PRD／SPEC文件修改與documentation semantic verification；不執行source/config變更、provider diagnostic、calibration、AC-014-X、Docker／Chroma／Candidate-C/F mutation、activation或Formal Evaluation。Git mutations由Human執行。新migration implementation及全部execution evidence均未由本amendment完成；Formal Evaluation為NOT EXECUTED，Slots Consumed = 0。Independent Slice 0 audit尚待執行。

### 24.6.1 Controlled Provider Diagnostic — Minimum Necessary Controls（Additive）

**D-1 minimum historical UNKNOWN exception — 2026-10-30 SPEC Lead Approved。** Authority：`PM-AIOPS-COMP-P0-UNBLOCK-20261030-01`，Parent：`PM-AIOPS-COMPETITION-SINGLE-DEMO-20261030`。PM已核准有限額外實體呼叫風險方向，不構成D-2新Live授權。SPEC Lead已明確確認原Attempt／Request關聯並授權離線實作，但不啟用正式例外；後續另獲PM Live Authorization的獨立Competition P0 Campaign，才可在同一既有Authorization／Safety／SQLite Admission中綁定此精確歷史個案；不能由Caller Flag啟用，不能改舊Authority或舊Row。

本次唯讀核對對象：Campaign `spec014-direct-embed52-20261030-01`、Attempt `cf02cd6e4ee94bd68f02dfbf99a75151`、Source `30b9e9e58edcf5f11722b25d4d160d26bb780320`、Outcome／Usage UNKNOWN、Items=1、Usage Hold=8192、Cost Hold=1 micro-USD；舊Input／SDK Body Commitment及Observation與既有Authority一致。Observation沒有Attempt ID且該Row的observation_json為NULL；SPEC Lead本次已確認是同一呼叫，但不宣稱舊資料本身已有直接Request級持久化證明。本段是新建Review摘要，原Row／Observation／建立時間及Hash不得補寫或回溯修改。

條件式例外只能改變這筆精確且未變更歷史Row對指定新Campaign的Blocking判斷；須綁定Human Review Reference／Reviewer、精確歷史Identity／原始Row占用與Evidence Commitment、新Campaign／新Live PM Reference及其Source／有效期。舊UNKNOWN／Raw Fence／8192 Usage Hold／1 micro-USD Hold完整保留並揭露，不退款、不轉移、不視為零；其他UNKNOWN、TIMEOUT、IN_FLIGHT、超額／不可靠Accounting、錯誤Identity／PM Reference、Duplicate、Persistence Ambiguity仍Fail Closed。新Campaign再度UNKNOWN／TIMEOUT不得沿用此例外。Preflight須區分原始Fence與指定新Campaign的實際Blocking判斷，並與Atomic reserve一致；Preflight不是Reservation或Live授權。不新增通用Adjudication Framework、Ledger或Schema Migration。

**最小Wire Binding（同一Authorization Record）。** 僅新獨立Live grant可選擇附加`p0_unknown_review`，精確欄位為`authority_reference`、`review_reference`、`reviewer_identity`、`recorded_at`、`historical_campaign_id`、`historical_request_id`、`historical_row_sha256`、`historical_evidence_sha256`、`target_campaign_id`、`target_pm_approval_reference`。Authority須為本D-1 PM reference，Reviewer與grant的SPEC Lead一致，記錄時間真實且不早於本決策日期、不在未來；Target與新grant一致且不得是舊Campaign／舊Live reference或Preparation Authority。Row Hash由精確八個既有欄位（含NULL observation_json）canonical JSON計算，Evidence Hash綁本次已核對的歷史Source／Input／Body／Authorization／Observation commitments；它們是Human Review範圍，不是重新證明歷史授權或建立Archive／Resolver。此欄位受既有Authorization Digest及E4／Safety共同綁定；缺省為None並從canonical digest省略，歷史Digest保持原值。不存在approved=true捷徑，不向舊Authority補欄位。 Runtime Atomic Admission MUST revalidate `review_reference` as exactly `SPECLEAD-SPEC014-D1-UNKNOWN-REVIEW-20261030-01`; empty or unrelated nonempty references and mismatched historical Attempt bindings MUST be rejected. Construction-only validation or caller-selected review identity is insufficient.

**Preflight／Reservation。** `inspect_local_counting_store(..., fresh=False, for_authorized_campaign=True)`只讀固定受控grant及其Safety／E4，附加`blocking_fences`／`reviewed_historical_fences`及目標Campaign；原`fences`始終是Raw Fence數，不改為零。Default inspection維持原樣。實際reserve在原BEGIN IMMEDIATE內使用同一分類，僅扣除符合精確快照的該一列Blocking效果；原Row／Hold沒有任何UPDATE。Preflight PASS不取代最終Atomic Eligibility，亦不預占Attempt或讀Credential。

**本次交付狀態：** Human Review已完成；SPEC Lead富裕已正式核准本Amendment，D-1 Audit Finding已CLOSED；離線實作已完成，正式例外未啟用，Human Git Checkpoint待執行；文件核准不構成新Live授權。D-2候選Campaign／PM Reference僅供Preparation，新Source、Live有效期、獨立有限正值Cost Limit及最終PM Live Authorization不得由本Authority代填。

**Competition P0 execution boundary — 2026-10-30 draft：** `PM-AIOPS-COMPETITION-SINGLE-DEMO-20261030`不是Live Campaign grant。歷史`spec014-direct-embed52-20261030-01`於source `30b9e9e58edcf5f11722b25d4d160d26bb780320`的Human execution為UNKNOWN／transport、一次本機Transport委派、零有效vector、usage UNKNOWN；Human Post報告attempts=1、fences=1、reserved_usage=8192、reserved_cost=1。這不證明Google收到或未收到；保留原Attempt、Hold、Authority與Fence，不Reset／Refund／覆寫。下一次須另有新Campaign授權及合法Eligibility；目前reserve()的Store-wide UNKNOWN Fence不可由換Campaign／Credential／Model／Host或新Store繞過。若無既有可用Adjudication API，先回報BLOCKED及精確PM決策需求，不自行造數據或建立Recovery Framework。新增非敏感Exception Type／Cause Type／實際HTTP Status／Failure Kind只改善下次可觀察性，不改Outcome、Budget或Retry權限。

**Single Direct Document Embedding Probe — Approved minimum admission。** 僅受控PM Campaign Record的`purpose=CONTROLLED_PROVIDER_DIAGNOSTIC`與`bounds_basis=SINGLE_DOCUMENT_PROBE_V1`、固定Approved Input及同一Safety／E4 Binding共同決定Probe適用性；名稱／Marker／Caller Flag本身不是授權。沿用同一固定Host Authority、Credential Resolver、SQLite及Final Guard，不新增Runner／Store／Framework。Safety Artifact的版本化`single_probe`區段須明列PM「APPROVE WITH MODIFICATION — Direct Embedding Single-Input Probe」、8192 Model Limit與Risk Basis、Exact Tokens NOT VERIFIED、固定Source／Corpus／Chunk／Input／Envelope／SDK Payload Bytes與Hash、Project及Truncation Limitation，並與同一Authorization Digest／Profile Revision一致。不得挾帶Counting、Batch Plan或偽造的Exact-input Token Evidence取得本例外。

PM Live Approval Reference、Campaign ID／有效時窗、Exact Clean Committed Source、Host／Runner、Google Project、非敏感Credential Profile Identity／Revision、Free Tier／Pricing與Synthetic Non-secret Outbound Evidence、Model／API／SDK、Input／Payload Commitment、最多一次Invocation／一筆Input、8192 Admission Risk Cap、有限正值Payload／Cost Bounds及Explicit Human Live Opt-in均必須有效。Project／Profile資料缺失不能以PM技術方向取代；本稿不產生有效Campaign Record或Final Cost Limit。Project／Revision使用既有Authorization欄位，僅限本Probe適用，其他Embedding既有規則不變。

先Atomic Eligibility／Durable Reservation，再Credential Resolution，再Final Guard及一次同步Transport；Single Runner、30秒、Duplicate Prevention、全域UNKNOWN／TIMEOUT／IN_FLIGHT與Budget Fence不變，不採停止中的Option B Exemption。完成後只驗證並記錄單筆768維、Finite、Nonzero Norm、事前Tolerance內的Provider Normalization與Positional Mapping Observation；不以Response Count單獨證明內容Identity。缺失Usage保持UNKNOWN，會依既有Fence阻擋後續受保護操作；Counting結果或本Probe成功不自動清Fence、Adjudicate、Refund、Reset、Retry或Resume。Formal SQLite已初始化，Source更新不授權重建／Migration／平行Store；後續須Human Checkpoint及獨立Live授權。本Amendment已於2026-10-30由SPEC Lead正式核准、Implementation ACCEPTED，並採納既有Independent Audit及B-01 Narrow Re-Audit PASS／Findings NONE；Human Git Checkpoint待建立，Google Live NOT AUTHORIZED。

**COUNTING_OBSERVATION applicability — Approved：** 下列原Controlled Provider Diagnostic purpose、完整54筆document scope、batch-plan、Embedding token／usage及vector驗收要求不改寫；只有本節末段明列的獨立Counting Operation適用差異，依Counting專用binding及minimum admission判讀。共用PM受控授權來源、exact clean source、host／runner、profile／account、atomic eligibility／reservation、secret safety及既有attempt authority不變；不得將Counting目的偽裝為`CONTROLLED_PROVIDER_DIAGNOSTIC`以繞過檢查。

**Controlled Batching amendment — Approved：** 以下更新request／plan及campaign binding；原PM-DIAG與Scope Reset歷史approval保留。本次PM Direction不等於Amendment Final Approval、Diagnostic Implementation Authorization或PM Live Campaign Authorization。只有事前核准固定plan的Sequential Synchronous Batching可適用，不允許runner自行split或動態擴張campaign。

**Scope Reset amendment — Approved 2026-10-28：** 本段在Milestone A範圍限定下列「每次Campaign／各操作授權」及「分段Gate」的解讀；原安全binding與§24.2不放寬。首次尚未驗證新Provider的Live Diagnostic Campaign須PM一次明確核准當次exact runner/source/model/config、input與budget範圍，並另作explicit Live opt-in，預設Live DENIED。同一已核准Campaign內的requests依同一有效scope／budget核對，不逐request再簽；超出scope／有效期／budget不得繼續。

Diagnostic核准不自動授權Build、Calibration、Production或Formal目的。新模型／腳本完成驗證且相關操作已獲准常態使用後，一般成員在該操作範圍與安全限額內自行rebuild／Demo不設per-run PM approval；未獲常態操作授權前不得自行推定已授權。此區分不要求新增credential、usage或security framework。

同一整合風險的regression／independent audit及Human checkpoint依§24.6合併，不另增加微步驟Gate。Controlled Batching approved amendment保留single-host／single-runner、每request transport前持久化attempt及budget記錄、bounded requests／payload／tokens與費用、每request 30秒／一次physical attempt／no hidden retry、secret-safe typed errors，以及TIMEOUT／UNKNOWN不自動重送、退還預算或繼續後續batch。下列既有輕量JSONL／SQLite選項已足夠，不新增完整Accounting／Reservation Ledger子系統。

最新PM Decision `PM-DIAG-MINIMUM-CONTROLS-20261022`（`MODIFY — APPROVED WITH SCOPE SIMPLIFICATION`）以`PM-RAG-EMBEDDING-MIGRATION-20261022`為Parent Authority，將本節Controlled Provider Diagnostic的安全設計限定為Minimum Necessary Controls。PM-DIAG-01：不採用Ed25519 cryptographic grant dependency；PM-DIAG-02：批准human-governed Live authorization；PM-DIAG-03：批准minimal API usage monitoring；PM-DIAG-04：批准offline／live separation。Ed25519、private／public Trust Root及獨立完整Atomic Accounting Framework不是本次必要交付。先前S4-D1／S4-D2工程授權方案已由最新PM決策取代；其歷史決策、audit與BLOCK evidence仍須保留，不回溯改寫。本addendum不變更§24.2、EMD-01～10、Frozen D1～D4／G1～G4或下游驗收條件。

- **Live authorization與source gate（Controlled Batching approved amendment）：** 每次Live Diagnostic Campaign須事先取得PM明確授權，預設`Live DENIED`且執行者須另作explicit Live mode opt-in。受控Authorization Record須綁定Human Git Checkpoint A後的exact committed clean source revision、執行者、model、credential profile identity、批准的完整input scope／commitment與固定batch-plan commitment、campaign最大batch／invocation／usage budget、有效期及每request 30-second timeout／one physical attempt／no-retry policy。Runner須核對實際HEAD、clean working tree、record與source／input／plan的一致性；一般caller不得任意取代授權來源或紀錄路徑，caller-supplied approved flag、verification物件或字串均不構成授權。任一前置條件不成立，須在credential discovery與provider transport前fail closed；包括累積budget、IN_FLIGHT／runner conflict及未經human adjudication解除的UNKNOWN／TIMEOUT等既有atomic eligibility拒絕條件。先完成既有atomic eligibility／attempt reservation，再進行credential resolution；credential失敗保留typed failure、已占用budget與UNKNOWN usage，不自動退款或重送。`39d199b92187f70da9f54e770af34df37ec13da1`是既有Offline／Free Tier source checkpoint，不冒充尚未實作batching後的execution revision。

Authorization Record為每次Live Campaign的必填、受控且可稽核契約；下列各欄均須存在、格式有效，並於每次attempt前與實際執行事實及同一PM核准scope逐一比對。Record自身或caller宣稱`approved`不取代受控來源的PM授權核對；Record、授權來源與usage／attempt紀錄路徑不得由一般caller任意指定或替換。任一欄缺失、不一致、超出核准範圍或無法確認上限，均須於credential discovery／transport前fail closed。

| Mandatory binding | 可驗證條件／拒絕準則 |
|---|---|
| PM Approval Reference | 非空且可在受控human-governed PM Live Campaign authorization來源解析；決策須明確涵蓋本次campaign，不得以parent migration approval代替Live execution approval。 |
| Authorized SPEC Lead Identity | 非空、明確身分；與PM核准紀錄指定的SPEC Lead／執行監督權責一致，不接受caller自行宣稱。 |
| Authorized Runner／Execution Identity | 非空、明確身分；與PM核准紀錄及執行時可驗證的runner／execution principal一致，未列入者拒絕。 |
| Exact Clean Committed Source Revision | 完整commit identity；須等於Human Checkpoint A後核准revision及執行時HEAD，且working tree（含untracked files）clean；不得使用本次Slice 1 baseline冒充完成後source。 |
| Provider Identity | 明確為本campaign核准的`google` Developer API provider，與實際client target一致；其他provider拒絕。 |
| Model Identity | 明確為本campaign核准的`gemini-embedding-2`，與實際request model一致；fallback或別名替換拒絕。 |
| Credential Profile Identity | 非空opaque profile identity，須與PM核准且執行時使用的profile一致；不得包含API key、token或其他secret material。 |
| Diagnostic Purpose | 明確限定`CONTROLLED_PROVIDER_DIAGNOSTIC`；Build、Calibration、Production或Formal Evaluation目的拒絕。 |
| Input Scope | Controlled Batching approved amendment：明列核准的document／query種類、來源及數量；document為six approved sources／54 ordered unique chunks，固定batch index／連續範圍及各batch數量，query另列且每request 1 item；每個實際輸入須在scope內，Scenario／Oracle／Ground Truth不得成為production retrieval輸入。 |
| Input Commitment | Controlled Batching approved amendment：對核准的完整canonical ordered inputs與document／query envelopes形成固定commitment，連同batch plan及各batch input commitments綁定；執行時重算後須exact match，不容許變更順序、內容、prefix、範圍或重複／遺漏。 |
| Maximum Invocation Count | Controlled Batching approved amendment：明列有限正整數maximum batches及maximum physical invocations，document batches與query requests分列並合計，不超PM核准campaign budget；包含同campaign已記錄及本次預占attempt，超額或記錄不可用即拒絕，不從retry分類推得額外呼叫。 |
| Usage／Input Bounds | Controlled Batching approved amendment：明列適用的每request item數、完整envelope每item／每batch aggregate token與UTF-8 byte bounds、各serialized payload上限、安全依據及完整campaign累積token／usage限額；執行時逐項驗證，不能證明必要安全上界時拒絕；Project quota不取代campaign budget。 |
| Cost Limit | 明列核准的幣別／計量單位及有限、可比較的正上限；本次呼叫前須能以核准的保守上界證明累積預算不超限，否則拒絕，不以估計值冒充Google Billing。 |
| Authorization Validity Start／Expiration | 兩個明確可比較且含時區的時間點，start早於expiration；每次attempt均須落在核准時窗內，過期或時鐘／時區無法判定即拒絕。 |
| Timeout／Attempt／Retry Policy | Controlled Batching approved amendment：固定Sequential Synchronous Batching；每request timeout = 30 seconds、`attempts=1`且最多一次physical attempt、no hidden retry／fallback／未授權split；record／plan與實際SDK options任一不符即拒絕。 |
| Single Authorized Host／Single Active Runner Scope | 明列唯一host identity及本campaign適用的runner exclusivity；實際host須一致且執行鎖確證無第二active runner，無法確認即拒絕。 |

Provider未回傳usage時，該usage事實記為`UNKNOWN`而非已知零值；若費用或使用量無法可靠確認，仍須依事前核准的保守上界占用budget並交human adjudication，不得自動繼續後續呼叫或宣稱正式Google Billing。這不放寬§24.2既有token／payload／provider item安全證據要求。

**Free Tier preservation（Controlled Batching approved amendment）：** 沿用已完成的`FREE_TIER_TEXT_V1`與受控pricing／quota／credential profile／account context／PM campaign及資料使用許可binding。Provider unit price可為0，但campaign cost limit仍須有限且為正值；每physical attempt保留既有1 micro-USD internal budget hold，計入campaign累積cost bound，不作Google單價或實際Billing。UNKNOWN token／usage不因零費率變成已知0；即使batch回應成功但usage仍UNKNOWN，也維持既有human adjudication fence，不自動繼續下一batch。固定batch plan不是該fence的豁免；本amendment不要求逐batch PM重新簽核，也不授權自動usage reconciliation、新accounting authority或Persistent Resume。

- **Single-host usage與crash safety（Controlled Batching approved amendment）：** Live Campaign限定single authorized host、single active Diagnostic Runner，使用必要的執行鎖及受控位置的輕量持久化Attempt／Usage／Outcome紀錄。每次provider attempt必須在transport前atomically檢查eligibility、記錄並占用核准budget，綁定campaign／batch identity與input commitment；duplicate、budget exceeded、未結清UNKNOWN／TIMEOUT／IN_FLIGHT、紀錄或鎖不可用時，均於credential discovery前fail closed。Crash、TIMEOUT或UNKNOWN outcome不自動重新送出、不自動退還已占用budget、不自動繼續後續batch，須保留可稽核狀態供human adjudication。可選適合prototype的JSONL、SQLite或等效簡單可靠方案，不強制建立完整Accounting Domain或multi-host coordination。保持每request 30-second timeout、one physical attempt、no hidden retry／fallback／未授權split；`SAME_OPERATION_ONLY`等retry-safety分類不授權自動重試。Provider usage未提供時明記`UNKNOWN`；cost estimate不得冒充正式Google Billing。
- **Failure與evidence safety：** Diagnostic須以可稽核且secret-safe的typed failure／CLI reporting區分rate limit、quota／billing、credential／authentication、network／timeout、provider unavailable、response contract invalid及ambiguous／unknown outcome，並記錄attempt數、source／input／budget commitment及非敏感結果。不將API key、Authorization header或敏感provider response複製到log、CLI或evidence。失敗或不明結果不形成Provider PASS，亦不觸發Candidate-C／Candidate-F durable mutation。
- **分段Gate與證據誠實性：** 原§24.6順序保持；於Offline Implementation／Regression／Independent Audit之後先完成Cumulative Audit，再由Human建立Git Checkpoint A並確定exact clean source revision。其後仍須另取得PM Live Campaign Authorization，方可執行Controlled Provider Diagnostic；只有實際provider observation依事前固定判準完成Provider Compatibility Acceptance，才可進入後續Calibration／new Build／Activation／SPEC-017 Evaluation各自的授權Gate。Offline MockTransport PASS不是Provider PASS。首次真實Diagnostic不要求事前已有該次呼叫才可能產生的Provider PASS，但input、item／token／payload安全bounds、budget、有效期與判讀準則必須事前核准；§24.2的54→54、768、document／query envelopes、逐筆identity／order、normalization及fail-closed驗收不放寬。本addendum只界定後續offline implementation的安全契約；本Phase A不授權Diagnostic Runner實作，Live Google invocation仍為`NOT AUTHORIZED`。

**Independent COUNTING_OBSERVATION minimum admission — Approved**

本段落實PM本次D2／D3的獨立Counting scope及minimum controls；D4相關Controlled Batching正式文件批准依上列Approval Record保留，不將其當作Counting Amendment或Live批准。只有明確以`COUNTING_OBSERVATION`為目的、指向§24.2固定候選的獨立Campaign可適用下列差異；原Embedding Campaign仍須滿足全部原有binding／admission，Counting成功不建立任何下游readiness豁免。

| Counting-specific binding／control | Mandatory admission／拒絕準則 |
|---|---|
| Purpose／Scope／Operation | PM受控Live Campaign record明列`COUNTING_OBSERVATION`、取得單筆token counting evidence的用途，以及§24.2的exact operation／model／API／SDK／source／version／ordinal／chunk／single-input及complete-corpus commitments；不使用54-item batch plan冒充單筆scope，不授權Embedding、Build、Calibration、Production或Formal目的。 |
| Authority／Source／Validity | Default `Live DENIED`；首次Counting仍須PM Explicit Live Campaign Authorization及runner explicit opt-in。沿用既有受控human-governed來源，不接受caller的approved flag／自製verification object。綁定Authorized SPEC Lead、唯一Runner／Host、Human checkpoint後的exact committed clean execution source（含untracked檢查）及明確有效起訖；本次方向核准或文件草案不能取代上述授權。 |
| Project／Credential／Outbound eligibility | 確認實際Google Project／account context及credential profile identity／revision與授權一致，綁定核准的synthetic／non-secret outbound eligibility及Free Tier資料使用許可；僅經既有resolver取得Credential，不切換profile／account或fallback，不記錄key。Project／profile或資料使用證據缺失、不一致即拒絕。 |
| Fixed Input／Payload | 一個Approved Exact Document Content；§24.2固定528-byte envelope／591-byte serialized body及其hash，MUST由實際Pinned SDK request於transport前重驗；單筆input／payload／method／route／commitment任一不符即拒絕。不得把bytes當tokens，也不以這些本地bounds宣稱Google Payload Admission或官方System Limit已驗證。 |
| Invocation／Timeout／Retry | Campaign最大physical invocation／attempt count = `1`，包括既有及本次預占attempt；同步執行，timeout = `30 seconds`，SDK `attempts=1`，no hidden retry／fallback／parallel／async batch／自動後續呼叫。一次attempt是本地Campaign budget，不是Google官方RPM／RPD unit保證。 |
| Quota／Token evidence treatment | 以本Operation自身適用的最低事前安全依據判讀；不要求先取得Counting要產生的Exact Token Count，不機械式套用Embedding per-item／batch token admission。官方Billing FAQ的GetTokens免費／不計Inference Quota說明可作相關證據，但須記錄來源與適用範圍，不擴張為本模型已Live驗證或無限請求。尚無公開證明適用本案的Counting System-level RPM，不填虛構數值、不套用其他產品3000 RPM，也不以Embedding的100 RPM／30000 TPM／1000 RPD當Counting budget。所有未驗證Provider Limits記`UNKNOWN`，不是0或Unlimited。 |
| Minimum safety／Unknown system limits | 依PM核准的本次minimum-controls原則，以明確單筆approved input、固定實際payload／hash、確認Project／profile／outbound eligibility、一次attempt、有限cost及受控authority／persistence作Counting admission；不要求事先證明所有未知Google System-level Limits，亦不將「尚未知系統RPM」本身轉成必須取得人工回覆的新Gate。此為獨立Counting的窄幅admission，不宣稱Provider保證接受；任一上述最低安全依據無法成立、已知適用限制被違反或無法確認Campaign不超限時，仍於credential discovery／transport前fail closed，不能靠PM一般性同意放行。 |
| Cost／Usage bounds | PM record仍MUST明列幣別／計量單位、有限正值Campaign Cost Limit、一次attempt的有限input／payload／invocation占用及費用安全依據。保留`FREE_TIER_TEXT_V1`、零Provider Unit Price與每physical attempt既有`1 micro-USD` Internal Budget Hold，hold計入Campaign正值cost bound，不冒充Google Billing。缺失的actual usage／費用事實保持`UNKNOWN`，不得因免費、計數成功或取得totalTokens而填零／退款；未知系統Quota不偽造成數字token budget。 |
| Reservation／Final Guard／Persistence | 重用既有single-host／single-runner、SQLite／Attempt authority及鎖。MUST先atomically檢查eligibility、累積budget／duplicate／IN_FLIGHT／UNKNOWN／TIMEOUT fence並持久化reservation，再credential resolution；送出前Final Guard重驗同一source／authority／profile／input／payload／有效期。Credential失敗保留typed outcome及占用，不重送或退款；紀錄／鎖／final guard不可用即拒絕。不得新增Store、Credential Registry、Quota Domain或Counting Scheduler。 |

**Result／Usage／Failure boundary。** Counting `totalTokens`與Embedding Actual Usage分離；不得完成Embedding Usage Receipt、寫入不相容的Embedding usage欄位，或以Counting結果清除本次／其他Campaign未結清的UNKNOWN／TIMEOUT fence。僅在可信Counting response驗證及既有authority中的durable recording均完成後，可記錄「Counting Observation result obtained」；不得將不明結果或持久化失敗標成成功。Evidence至少綁定campaign／attempt、exact source、host／runner、model／profile、input／body commitments、時間、結果或typed failure與未知limit／usage事實；不新增第二套Usage Authority，不持久化secret或完整raw response body。

Counting失敗、Malformed Response、Model Unsupported／Unsupported Operation、429／Quota、Timeout、Network／UNKNOWN或Persistence Ambiguity均MUST記錄適當typed outcome並STOP；未知結果、crash／restart或已占用budget不自動重送、退款、恢復額度、改模型／API、改input或啟動Embedding。無法確證durable write時保留既有IN_FLIGHT／UNKNOWN等保守fence供human adjudication，不假造記錄成功。一次Counting成功後亦無下一個自動request；後續Embedding Diagnostic須另行授權並通過其原Token／Payload／Quota／Batch／Usage admission。

**Acceptance／delivery preservation。** 本amendment不修改Controlled Sequential Batching、Trusted Usage Metadata、54→54／768／normalization、Knowledge Build／Validate／Activate、AC-014-X、Knowledge Governance、SPEC-011 singular Runtime或SPEC-017 Formal Acceptance。單筆Counting不是G7完整Embedding Diagnostic、G8 Compatibility PASS、Candidate-C Build、RAG／RCA PASS或Production Ready；不是Formal Evaluation evidence或slot consumption。本輪只完成已通過Audit的Counting Amendment正式文件Approval Status收尾，不授權Counting Runner Implementation或任何Provider Execution，不新增G／S／E Gate、Persistent Resume、Multi-key Pool或獨立Accounting Framework。

**Prototype Local Host Simplification — Narrow Amendment Approved（2026-10-29）**

**Approval applicability。** 上列SPEC Lead Approval Record已正式核准以下原Normative Contract，以及§24.2／§24.6與AC-014-AD的Prototype Host補充。上述及下文原「Draft／草案／待Audit／待Approval」與末段交付狀態保留起草時授權快照，不代表現行文件仍Pending；技術契約不變，文件批准本身不授予Implementation Acceptance、Host操作或Live授權。後續工程依SPEC Lead獨立Implementation Authorization執行。

**窄幅優先規則。** 僅在本Amendment正式核准及另行實作／操作授權後，Prototype Host部署依本段優先判讀；原文泛用JSONL／SQLite選項在本Prototype限用同一SQLite authority，不授權切換JSONL或第二Store。所有非Host範圍契約保持原有authority，歷史狀態與決策不回溯改寫。

**Authority／適用範圍。** PM「APPROVE — Prototype Local Host Simplification」取代先前「APPROVE — Option A：保留SQLite及現有Host Validator」中要求完整保留Windows Validator的部分；SQLite及必要Provider Safety維持。以下僅為待Audit／SPEC Lead Approval的Host修訂草案：適用單機Prototype Diagnostic的本機Identity、authority/evidence檔案與同一SQLite Attempt／Usage／Budget部署，不取得Knowledge／Incident／RCA authority，不改Counting與Embedding Operation scope。既有Accepted Source仍採舊Validator／ProgramData resolver；不得停用它們或偽造Token／Path facts取得PASS。後續程式變更須另行明確授權，完成受影響離線驗證及適用Human Source Checkpoint後，才可使用新契約執行。歷史Approval／Audit／BLOCK／Failure／NOT EXECUTED紀錄完整保留。

**Local Runner admission。** 允許既有`LAPTOP-C5CUESSG\vincent`帳號，實際執行時以Windows取得的primary process user SID綁定，不只比較名稱。不強制Dedicated Standard Account或跨帳號部署；每次preflight及送出前Final Guard仍核對唯一Host、相同Runner SID／execution identity、Source與PM Campaign。Token facts須由Windows真實程序Token取得，無法取得或存在thread impersonation／不明執行身分即拒絕。Process必須Non-Elevated；真正Elevated、Enabled Administrators群組（`S-1-5-32-544`具Enabled屬性）均拒絕。Deny-only Administrators／`S-1-5-114`只參與拒絕ACE，不能當作Allow權限；其存在不再單獨拒絕。不得藉由關閉UAC、修改User Rights Assignment或偽造facts符合本條款。

Privilege判讀MUST保留名稱與Enabled／Disabled屬性，不能沿用「名稱存在即全部視為Enabled」或「所有非Allowlist Disabled均拒絕」。最小高風險拒絕集合為`SeBackupPrivilege`、`SeRestorePrivilege`、`SeTakeOwnershipPrivilege`、`SeDebugPrivilege`、`SeSecurityPrivilege`、`SeManageVolumePrivilege`、`SeImpersonatePrivilege`、`SeTcbPrivilege`、`SeLoadDriverPrivilege`、`SeAssignPrimaryTokenPrivilege`、`SeCreateTokenPrivilege`、`SeTrustedCredManAccessPrivilege`：Enabled時拒絕；即使Disabled，只要仍存在且可由該程序啟用，也拒絕，因其可繞過檔案保護、取得其他身分／Credential或控制程序／系統，不能僅以當下Disabled視為安全。已移除的Privilege不當作仍持有。`SeChangeNotifyPrivilege`及常見`SeShutdownPrivilege`、`SeUndockPrivilege`、`SeIncreaseWorkingSetPrivilege`、`SeTimeZonePrivilege`的Disabled狀態不單獨拒絕；它們不等同上述檔案／身分安全界線的繞過能力，仍保留availability／本機設定風險。其他Privilege若具上述繞過能力或Token屬性無法可靠判讀，須typed fail closed，不能因不在列舉中自動允許；不建立通用Privilege Registry／全面Allowlist Framework。

**唯一正式Path Resolver／Store authority。** 正式Root為目前已綁定Runner使用者的Windows `LocalApplicationData` Known Folder（`FOLDERID_LocalAppData`）加`AIOps\Diagnostic`；唯一Store為`<LocalApplicationData Known Folder>\AIOps\Diagnostic\state\attempts.sqlite`，authority/evidence位置維持同Root下`authority`與既有相對檔名。從Windows Known Folder API解析，不信任`LOCALAPPDATA`字串覆寫、CLI任意path、目前目錄或TEMP；不得silently fallback到ProgramData或建立另一個authority。Resolver／PM binding／attempt evidence須一致記錄canonical Root、Runner SID及Store file identity；非預期reparse／symlink或路徑identity變動即拒絕。Google Project／Credential Profile及其他既有非敏感binding維持，不讀取或保存Secret於此Root。

**Existing state／Fresh-only。** Fresh Initialization與Normal Diagnostic Invocation必須為分離的明確操作。初始化前重新檢查新Root及舊Windows `ProgramData` Known Folder下`AIOps\Diagnostic`（含原`state\attempts.sqlite`／authority／sidecars），不能只相信先前目錄不存在的觀察。新Root已存在任何Store、空檔、殘留目錄或不明資料即STOP；舊Diagnostic位置若存在任何可能authority／Attempt／Budget／UNKNOWN狀態，或無法可靠判定其不存在，也STOP，待Human確認唯一Store Authority與相容性，不能另建第二個有效Store。此草案不授權Migration、複製、刪除、reset或自動搬移；不以改名／換路徑逃避fence。

在後續Human明確Fresh操作授權下，以CreateNew／exclusive no-overwrite建立全新空Store，並以SQLite排他初始化及既有exact `_COUNTING_ATTEMPTS_DDL`建立Schema；不改`observation_json`、constraints、`STRICT`、`WITHOUT ROWID`、`journal_mode=delete`或Accounting語意。使用既有exact DDL／Integrity／row consistency validation；初始化失敗保留狀態供Human判讀，不自動cleanup或重試。正常Diagnostic僅開啟已存在且相容的正式Store，缺失、不相容或sidecar／recovery ambiguity時fail closed，不暗中初始化、重建、修復或Migration。正常SQLite已核准transaction中的journal建立／刪除不等同允許刪除既有不明sidecar。

**基本本機檔案保護。** 允許該Runner使用者擁有及管理LocalAppData Root／Store，不要求Administrator ownership、獨立帳號或禁止owner一切刪除權限。檢查實際owner／ACL及有效存取：綁定Runner、SYSTEM與Administrators可有必要管理權，不允許Everyone／Users／Authenticated Users或其他未授權主體寫入、修改ACL或替換authority／Store；不得以Everyone Full Control或開放整個個人Profile解決。Runner須具Store read/write及SQLite journal在state內的必要create/write/delete權，驗證journal實際適用的繼承／權限，不能機械沿用原Administrator-owned state mask。Invocation持續以既有file-identity／不分享delete的DB handle guard防止操作期間pathname被普通流程替換，並維持SQLite atomic eligibility／reservation、duplicate prevention與single active runner；owner在操作外仍可竄改／替換的風險另如下文揭露，不能宣稱guard提供企業級不可竄改性。

**簡化Host Preparation。** 只需：(1) 核對exact Source Revision、腳本integrity、Host／Runner SID；(2) 封網確認必要Python／SQLite版本、Diagnostic／pinned SDK依賴與既有Exact Counting serialization；(3) 重新確認新舊Store位置不存在或已按唯一authority規則停止；(4) Human explicit Fresh SQLite initialization；(5) 一次combined Host／SQLite preflight。符合admission的Non-Elevated同帳號Session可完成；沒有預設必要的Administrator步驟。Dedicated account、跨帳號ACL、Administrator-owned directory bootstrap、多層Approval Manifest、完整OS Dependency Inventory及重複Script／Token／ACL Approval不作必要條件；保留簡單、可核對且對照Human已確認版本的script hash／Source檢查，不以自算hash冒充核准，不執行未核對的程式。這五步不建立新治理Gate或Live Authorization。

Combined preflight一次核對Source／script、Host／SID／真實Token、必要Runtime／Golden payload、唯一正式path／ACL／Store file identity／journal safety、exact DDL／Integrity及Attempt／Usage／Budget／UNKNOWN一致性；不得在正式Store建立測試Attempt。首次Fresh驗證可要求Attempt及reserved Usage／Cost皆為零；後續已有合法Attempt的Health檢查不得要求零筆，亦不清除fence。唯讀檢查不觸發recovery／repair、初始化或Provider；若還缺PM Campaign／Profile／其他Live evidence，標明缺項，不假造Host Live Ready。

**保留的Provider／Authorization控制。** 原§24.6.1受控human-governed PM record不是CLI caller的`approved=true`或自製verification object；本機檔案仍須驗證其正式授權來源、內容、Source／Host／Runner SID／Profile／Model／Operation／Input／Payload／scope／有效期與有限budget。PM Explicit Live Campaign Authorization、Default Live DENIED及explicit opt-in不變。改用使用者擁有檔案不使caller self-approval合法，也不宣稱可抵擋同帳號惡意偽造。保留同一SQLite durable Attempt／Usage／Budget、single active runner、credential discovery前atomic eligibility／reservation、duplicate／budget／IN_FLIGHT／UNKNOWN／TIMEOUT拒絕、Final Guard及必要typed outcome。Counting最多一次physical attempt；各Embedding request最多一次、受固定batch plan及campaign累積上限約束；30秒timeout、no hidden retry／fallback、crash／UNKNOWN／TIMEOUT不自動重送、退款、reset或恢復budget，既有`1 micro-USD` internal hold及有限正值cost limit不變。Counting `totalTokens`不是Embedding Actual Usage，不解除其他fence，不自動Embedding，也不形成54→54或G8 PASS。API Key不入Git／Logs／Reports，不新增Credential Framework或第二套Store。

**PM接受的Prototype風險。** 本契約提供Prototype Operational Safety：正常受控流程必須遵守授權、限額、持久化與防重送；違反仍fail closed。PM接受使用單一既有帳號及其LocalAppData、缺少企業級Windows Account Isolation與完整OS Dependency Attestation，亦接受對具有該本機使用者存取能力之惡意竄改者的防護降低；該使用者可在流程外修改自身檔案／DB／程式，單純hash、ACL或SQLite不能提供企業級不可竄改承諾。這項Protection Against Malicious Local User Tampering的取捨不豁免未授權重複呼叫、持久化缺失、Secret洩漏、自動清紀錄／退款／重送、超scope／budget或caller self-approval；不宣稱上述風險不存在，不擴張為Production Ready或Formal Acceptance豁免。

**最小Acceptance（AC-014-AD直接相關補充，Draft）。** 後續僅驗證受影響範圍，不新增完整Windows Security Test Framework：

| Area | 必要判準；尚未執行本次新Host實作／驗收 |
|---|---|
| Local identity／Token | 實際vincent Non-Elevated Session在其他條件成立時可admit；Deny-only Administrators及上述一般Disabled privileges不單獨拒絕；Elevated／Enabled Administrators、不可確認的身分／Token、高風險Enabled及仍可啟用的上述高風險Disabled均拒絕。不得用synthetic facts冒充Human PASS。 |
| Resolver／唯一authority | 真實Runner LocalApplicationData Known Folder及固定相對路徑；環境／caller覆寫、舊ProgramData state、雙Store或未知state均不會silent admission／fresh重建。 |
| Fresh／Schema／File safety | 新Root／Store或殘留已存在即拒絕且不overwrite／cleanup／Migration；exclusive Fresh建立exact DDL、STRICT／WITHOUT ROWID／observation_json／delete journal；驗證Integrity、基本ACL、journal必要權限及操作中的file identity穩定。 |
| Persistence／Concurrency | 同一SQLite保留Attempt／Budget／Usage／UNKNOWN；duplicate、超budget、IN_FLIGHT／UNKNOWN／TIMEOUT、lock／persistence ambiguity於Credential前拒絕；single active runner及crash／restart fence不退化。Fresh zero-state不套用日後合法非空Store。 |
| Authorization／Provider boundary | Source／Input／Operation／Model／Profile／Host／SID／有效期／PM Authority及Final Guard維持；未有PM explicit Live Authorization或opt-in時Provider Calls = 0；不接受caller self-approval。保持reservation-before-credential、一次attempt、30秒、no retry／fallback、internal hold及Secret Safety；Counting Result不記作Embedding Usage／54→54 PASS。 |

**本草案交付狀態。** PM方向已核准；Independent Narrow Document Audit PENDING、SPEC Lead Amendment Approval PENDING、Implementation NOT AUTHORIZED／NOT EXECUTED、Host Initialization NOT EXECUTED、PM Live Authorization NOT AUTHORIZED。本段不回溯變更既有Counting／Batching Approval與Offline Implementation Acceptance，不改PRD-004產品語意、AC-014-X／Knowledge Governance、SPEC-011 Runtime或SPEC-017 Oracle／N／reviewer／denominator／Formal Acceptance；後者仍須依真實證據完成。

## 24.7 Competition P0 — Single Shared Demo Environment（SPEC Lead Approved）

Authority：`PM-AIOPS-COMPETITION-SINGLE-DEMO-20261030`，2026-10-30，APPROVE — Competition P0 Single Shared Demo Environment；SPEC Lead富裕，目標2026/11/07決賽。PM已核准方向與本次必要離線修訂；SPEC Lead富裕已正式核准本Amendment，Provider→Build Integrated Audit PASS；不宣稱Real Provider或Demo PASS。本新增段只取代Competition Milestone A的四人獨立環境前置；§24.5／§24.6及AC-014-X的Formal Portability／Production義務和全部歷史Approval／BLOCK／NOT EXECUTED保留。

**Demo交付與Ownership。** 在一套受控共享Host，由SPEC Lead完成六份 Approved SOP／RUNBOOK 文件→54 unique ordered chunks→54筆真實768D向量→immutable Knowledge Build→Validate→explicit Demo-scoped Activation→bounded Retrieval→既有SPEC-013／015／016與SPEC-011 singular Runtime的Evidence-grounded RCA／Publication故事線。其餘三人共用操作環境或錄製素材；不要求各自API Key／Embedding／重建，不分享Key或將generated DB推入Git。Diagnostic attempts.sqlite只是Attempt／Usage／Budget權威，不是Knowledge index或供其他Host重新取得Eligibility的來源。

**最小Demo Evidence Contract（缺一不得記DEMO READY）。**

| Evidence | 必要內容 |
|---|---|
| Source／Provider scope | Exact clean committed revision、PM授權scope、Host／Runner、非敏感Project／Profile／Revision、Model／SDK／API、Manifest／Source／Version、Input／Payload及固定Batch Plan commitments；不保存Secret |
| 完整Embedding | 全54 unique chunk identity，無重複／遺漏／改序，一對一768D finite／nonzero vector，事前固定Normalization Tolerance，逐batch及全域mapping／order、真實Provider Observation與可信Usage或如實UNKNOWN；Partial／HTTP success／單筆Probe不能冒充完整PASS |
| Build／Activation | Model／Profile／Build identity、immutable staged artifact、validation result、explicit Demo authority／operation key／activation generation、獨立Demo namespace與stores；保留LKG、historical references及錯誤時不更動既有有效build |
| Retrieval／RCA | 至少一組Approved SOP MATCH及一組NO_MATCH或Knowledge Gap，實際Query embedding或核准且可驗證的固定Query流程，Snapshot／Source／Version／Chunk provenance；至少一條Incident→Evidence Snapshot→Knowledge Snapshot→Validated RCA→durable publication→public read故事線 |
| Limitations／Team handoff | 實際執行時間、結果／typed failure、未完成工作、必要failure／restart checks、操作與錄製分工；DEMO READY / BLOCKED與FORMAL PASS / PENDING分開，不填虛構統計或修改Formal denominator |

**不變的必要前提。** 保留§24.2完整Compatibility、§24.4隔離calibration／policy freeze、Known Folder／File Identity／Legacy Fence、Exact SQLite DDL、Atomic Reservation-before-Credential、有限Invocation／Usage／Cost、每request 30秒／一次physical attempt／無hidden retry或fallback、真正UNKNOWN／TIMEOUT／IN_FLIGHT Fence。未知Provider Limits不得虛構；Probe的8192風險例外不自動適用全54或Query。Demo activation不等於Production Activation或SPEC-017 Formal preparation；不用Oracle／Ground Truth調整Demo query或污染production RAG／prompt。

**離線接線狀態與下一步限制。** Controlled Batching已於2026-10-28正式核准，其Historical Draft原文不是當前Pending。依本次PM授權完成同一Diagnostic core→GoogleEmbeddingAdapter→Provider Result→Build／Validation接線；只有完整固定Plan、逐批持久化可信Usage與54→54／768／finite／nonzero／0.01 norm驗證成功才交付向量。Gemini2完整六Source／54個Chunk的Build允許受既有Plan及limits約束的多次physical invocation，回報真實count；舊模型仍限一次。明確Gemini2 config／CLI選擇不改historical tracked model，不構成PM Live grant，也沒有第二次Embedding、vector import、第二套Store或自動Activation。Mock Provider接真實Chroma／Build Validation及隔離Snapshot介面驗證不構成Real Embedding／Knowledge／RAG PASS；本Amendment已由SPEC Lead富裕正式核准，Provider→Build Integrated Audit PASS；Human Git Checkpoint尚待執行。Query仍須獨立授權與接線，不因Document完成就宣稱可作任意Live或離線查詢。

**目前狀態。** 已有Single Probe UNKNOWN及未解除的Store-wide Fence；第一筆有效768D、完整54→54、Demo新build／activation／retrieval／RCA故事線均未取得成功證據。Competition A = BLOCKED；Formal B = PENDING；AC-014-X歷史NOT EXECUTED不變。換Host不豁免舊Attempt／Budget／UNKNOWN事實；若需更換Host先由PM決定合法連續性，不建立空白Store繞過Fence。
