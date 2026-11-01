# SPEC-014 Candidate-C Knowledge Index Setup

## Demo Gate Reset Candidate — 2026-11-01

### Full54 Option B Candidate — 2026-11-02

SPEC Lead於2026-11-02依PM `PM-AIOPS-COMP-P0-FULL54-BOUNDED-RISK-20261101`正式核准SPEC-014 §24.10.1～§24.10.4精確Contract、固定契約參數及已審離線實作（含F-01修正）；正式Bounded-risk Exception尚未啟用。既有Runner以新的protected `full54_bounded_risk`與Plan `admission_basis=FULL54_DEMO_RISK_CAP_V1`區分Strict Evidence；四個Reference為本PM方向、真實精確契約Approval、Human Project Exclusive-use確認、Human Quota Evidence，完整進原Digest／E4／Safety。Project仍為gen-lang-client-0149178738／Free Tier／RPM100／TPM30000／RPD1000，profile／rev-1不變。

固定54／6×9、最多6 Physical Attempts、每次30秒／No Retry／Parallel／Fallback／Resume；候選Reservation為8192/item、73728/batch、442368/campaign risk units及6 micro-USD內部Campaign Hold。這不是Accepted Token Evidence或Google費用保證；Provider Limits／Request Units明示UNKNOWN，不填假數字。保留原序列化Commitments與Payload Bounds；Parent全Corpus E4=52485 bytes，不能沿用Single Probe的4096-byte Grant。

每批前從Run開始或上批可信Usage持久化後進行65秒有界Quiet，Wall／Monotonic Clock一致性與原Authority逐次檢查；每批可信正Usage須≤30000，本地尚適用Receipt亦須納入檢查。超限保留Observed Usage並形成新UNKNOWN；任何Timeout／429／缺Usage／Partial／Persistence異常即停止且不交付Build。不宣稱已證Google Window／共享流量／剩餘RPD或完整輸入不截斷。Human須確認停止其他可控Project流量；未知外部流量仍為Final Grant需接受的風險。只有全部54成功才能Stage／Validate；不自動Activate。

最後文件Review後Human Clean Checkpoint→將本次真實Approval記錄由Human Publication Owner綁入受控Evidence／Human Project Exclusive-use確認／獨立PM Final Live Grant及執行Budget／Validity→重建Root／Child Authorization／E4／Safety→Human Publication／Preflight／Opt-in。不得任填Approval Reference或把契約批准當Final Grant。此處沒有新增可直接執行Live的CLI，不能用舊Probe Handoff。§24.9 Approved、原Strict／Production／Query／Formal義務、兩筆歷史UNKNOWN與全部Hold不變；Scope／Profile／Query／LLM仍各自待批准。下方Option B前的Pending描述保留歷史，本段不是Live操作許可。

Authority：`PM-AIOPS-COMP-P0-DEMO-GATE-RESET-20261101`；Parent：`PM-AIOPS-COMP-P0-KNOWLEDGE-DELIVERY-20261101`。SPEC Lead於2026-11-01正式核准SPEC-014 §24.9.1～§24.9.5精確Competition Demo-only Narrow Amendment。本節優先說明已核准契約及尚待正式Binding的離線實作；下方Current／2026-10-30原Calibration前置與Scope限制保留歷史。此批准不包含四Scope候選值、Demo Profile／Threshold、Risk Exception、Live或Activation；正式例外未啟用。

### 一次真實54→54的Demo路徑

正式Full54 Grant／Token／Provider／Quota／Budget／Source／E4／Safety通過後，同一次完整Provider Result經原`GoogleEmbeddingAdapter`→`embed_documents_for_build()`→`KnowledgeBuildService.stage()`→Validate。全部Mapping／Order／768D／finite／nonzero／norm0.01及逐批可信Usage持久化成功才有完整Build。沒有Calibration Vector Import、第二次Embedding、另一套Store或Activation Authority。Stage／Validate不Activate，Human仍需核對Demo範圍、Profile批准、immutable artifact、Validation與Generation/CAS後才可Explicit Activate；不得覆寫Formal／Production的Active／LKG權威。

Source沒有Calibration boolean gate；原前置在治理與Profile批准。PM只對Demo延後該前置，不更改Build Identity算法或原Activation Safety。Formal／Production保持Independent Calibration→Policy Freeze／Regression→Separate Build→Formal Acceptance；Calibration尚未完成、AC-014-X NOT EXECUTED、SPEC-017 Formal NOT EXECUTED。Demo命名與Operation Key只作可追溯標記，不能作Caller自我批准。

### Full54 Historical Scope Candidate

未來受控`campaign.json`可同時提供原兩份`p0_two_unknown_reviews`與新`p0_full54_history_scope`。新Scope五欄：`authority_reference`（本PM Gate Reset）、`delivery_scope=COMPETITION_DEMO_ONLY`、`global_input_commitment`、正式`batch_plan_commitment`、全部有序`batch_input_commitments`。Root與每個Child Projection均受Canonical Authorization Digest／E4／Safety綁定；只有原兩列的Blocking效果可被排除，Raw Fence／Hold不變。不能拿Proposal SHA當Runtime Plan SHA，不能使用原三次Probe Grant或PM工程Authority作Final Live Grant。缺Scope／Reviews、新Query或其他Ambiguity皆拒絕。

本輪沒有發布Authority或啟用正式例外。完成SPEC Lead Review／Human Git Checkpoint後，新Grant須綁新的Clean Source；不要用舊Third／Follow-up Handoff執行Full Campaign。最終唯讀`inspect_local_counting_store(..., fresh=False, for_authorized_campaign=True)`須等正式新Authority存在；它不能取代Typed Request Preflight、Atomic Reservation或Final Guard，也不執行fresh bootstrap。

### Pre-result Demo Profile與Scope — PROPOSAL ONLY

候選Demo Retrieval／Applicability Profile為`spec014-gemini2-demo-retrieval-v1`／`spec014-gemini2-demo-applicability-v1`，Version1.0；同一Gemini2 embedding profile、768D、v1 document/query envelopes，L2 LOWER_IS_BETTER。Threshold候選`0.2/0.5/0.8`只源自historical policy，NOT CALIBRATED／NOT APPROVED；保留現有top-k／candidate bounds作事前候選，不以S5結果調整。必須在任何真實Query／Ranking判讀前，由SPEC Lead正式接受完整Profile／Scope／threshold／typed-failure／Demo-boundary及canonical commitment；Tracked runtime config仍為historical model，不自動改用這份候選。

S5 Shared Dependency正例僅在Public Snapshot及經批准的Operational Context支持四Scope時才可能MATCH。S2負例僅在真實Capture缺必要Scope時形成合法Knowledge Gap，不刪已取得證據、不把Provider／Authorization失敗當NO_MATCH。現有`incident-evidence-query-v1`不變，四Filters仍省略；Event名稱不是根因，service_name不是Component Scope，SOP Metadata／Scenario／Oracle不作Production Scope來源。Query Live及LLM Live均獨立授權；Full Document Grant不代替它們。

### Full54 Admission仍有的實際缺口

6×9只是既有Candidate；actual bodies=9407/8795/8437/8697/8597/8622 bytes，最大保守per-batch檢查值10279。Parent Authorization亦綁52485-byte全Corpus E4（不是實際批次呼叫）；最大item envelope1302。新PM limits至少須涵蓋這些本地證據，但它們不是Google Token／Payload Limits。模型8192、Probe Usage100及Synthetic Bounds都不能代替54筆受接受Token證據。Project RPM／TPM／RPD／batch accounting、有限Usage／Cost／Validity仍待真實Evidence／PM Grant。

Bounded-risk Proposal只能提交PM：例如將8192/item、73728/batch、442368/campaign明確標為未觀測的Admission Risk Caps，固定6次attempt／30秒／No Retry，並保留UNKNOWN Quota及實際Usage／Billing未知。現有Full Runner不支援這個exception，不能把cap塞入`accepted_input_token_bounds`或虛構Quota過Validator；須先有精確Contract Decision及另授最小Source修訂，本輪不實作。若Free Tier仍適用，6 micro-USD只是6次內部Hold的候選限額，不是Google實際／最壞成本保證。原24576 Usage／3 Cost Hold與兩筆UNKNOWN保持不變。

11/03 12:00（UTC+08:00）仍未取得合法Full54路徑時交PMStop-loss決策；不自動多Probe、不延Validity、不建Scheduler／Resume／Counting，不啟動BM25。此頁不是可直接執行Live或Activation的批准。

## Current Knowledge Delivery — 2026-11-01

Authority：`PM-AIOPS-COMP-P0-KNOWLEDGE-DELIVERY-20261101`。本節是當前狀態；下方 2026-10-30 的 Draft／UNKNOWN／NOT EXECUTED 是當時歷史，不回溯改寫。Single Shared Demo、Two-UNKNOWN 與 Transport Fix 已經 Human Checkpoint；本輪新接線仍待 Source Review／Human Checkpoint，不能沿用舊 Live grant。

Human 已接受第三次 Probe（`6289a1653fecf0352135f4ebe736a46f88046986`）SUCCESS：campaign `spec014-direct-embed52-third-20261031-01`、attempt `48e21df9ea19485aa0e0746781be9f6c`、physical attempts=1、vector_count=1、observed usage=100。Human Post PASS：總 attempts=3、歷史 UNKNOWN raw fences=2、reserved usage=24576、reserved cost=3、historical rows unchanged=true。此處引用 Human Evidence；未重新讀寫正式 Store。Observed 100 不是整份 Corpus Token 上界，8192 不是 Observed Usage，internal cost hold 不是 Google Billing。

### 最短共享 Demo 路徑與必要界線

1. 使用同一 manifest／deterministic chunker 取得六份 SOP／RUNBOOK 的 54 個唯一有序 Chunk。正式 Global Ordinal 為 **0..53**；Probe 是 `chunks[52]`。六批各九筆（0–8、9–17、18–26、27–35、36–44、45–53）僅為可重現 serialization proposal，不是已通過 Provider／Quota Admission 的執行 Plan。完整 Runtime Plan 尚缺受接受的逐筆 Token Bounds、Provider Item／Aggregate／Payload Limits、Project RPM／TPM／RPD 與 Usage／Cost Budget；不得乘用 Probe 100 或8192補值。現行安全評估要求所有批次符合既有保守單一 quota window，沒有 scheduler／resume。
2. Full Document 和 Query 都須獨立 PM Live grant。現行 `p0_two_unknown_reviews` 僅接受 Single Document Probe；兩筆歷史 UNKNOWN 對 Full／Query 仍阻擋，第三次 grant 不可延伸。需先由 PM／SPEC Lead 決定精確 Campaign Scope Amendment；本輪只提出 Draft，不修改 Admission／Global Fence SQL、不啟用例外。
3. 完整真實 54→54／mapping／768D／finite／nonzero／norm 0.01／可信 Usage 持久化後，同一 `embed_documents_for_build()`→`stage()`→Chroma→`validate()`。完成新 Gemini2 retrieval calibration／applicability policy freeze，才可在隔離 Demo namespace explicit activate；保留 LKG。Tracked historical config 不自動轉為 Gemini2 或授予 Activation。
4. Query v1.0：先在呼叫邊界 `strip()` canonical query；`CanonicalKnowledgeQuery` 拒絕未 canonical／錯誤版本／敏感形狀。Envelope 固定 `task: search result | query: {canonical_query}`。同一 Gemini2 capability／768D／profile，query identity 包含 frozen build＋query commitment；SDK 不傳 `task_type`／`auto_truncate`。`embed_query_for_retrieval()` 使用原 guarded execution core，只有成功且可信 Usage 已持久化才交付一筆向量；缺 Usage／Provider Failure 不作 NO_MATCH。
5. CLI `retrieve` 對 Gemini2 新增 `--live-opt-in`；singular Runtime 新增 `--knowledge-live-opt-in`，僅允許呼叫既有 guarded query adapter，**不是授權或可直接執行的 Live Runbook**。兩者仍須完整 Query Authority／E4／Safety、凍結 Build、有效 budget、Atomic Reservation、Final Guard 與無 blocking fence。目前 Full／Query Live 均 NOT AUTHORIZED；不得在準備階段執行以上 opt-in。Runtime 的 flag 不授予 LLM Live。
6. Candidate-C 保留 MATCH／NO_MATCH／RETRIEVAL_UNAVAILABLE／INVALID 或 REPAIR_REQUIRED；Operation Replay 使用 immutable Snapshot，矛盾操作拒絕。Snapshot／Source／Version／Section／Chunk／Score／Applicability Provenance 交付 SPEC-015，Incident Facts 只取 SPEC-013。既有 SPEC-016 Durable Publication 與 Public Read 接線不變；LLM 另需自己的授權／budget。SOP context 不建立 Incident Cause Authority。

### 固定候選 Query（未驗證真實 Ranking／Applicability）

**Finding 1 保守接線（2026-11-01，依 SPEC Lead 本輪確認）。** `runtime_orchestration.knowledge_query` 的 `incident-evidence-query-v1` 僅將 SPEC-013 immutable Snapshot `event_projections` 中已支援的 event_source／event_type、severity 與唯一合法 service_name selector 投影成 bounded canonical Query；沒有 Snapshot／Incident 一致性時停止。Initial 在既有 Capture／Materiality 之後、Candidate-C 之前衍生，Follow-up 共用同一 helper。Retrieval 的 external references 保存 Mapping Version 與 Evidence Snapshot Reference；既有已 frozen（包括歷史 Incident-ID Query）原樣重播，不改 Build／Operation／Snapshot／Recovery Authority。

Public Facts 尚未提供四個 Applicability Scope，亦無已核准 Event→Scope 對照；SPEC Lead 已確認先完成保守 Mapping、另審 Scope 對照。因此此版本 **所有 Filters 省略**：service_name 不是 component_service_scope，detector event_type 不是已證實 failure cause；不得從目標 SOP Metadata 補值。即使相似度高，仍依原 Policy 判定不適用／NO_MATCH，不是可展示 MATCH 的完整 Mapping。既有隔離 MATCH Fixture 只驗證 Retrieval／RCA 介面，不能作 Incident-derived MATCH Evidence。下列 Query A／B 仍是候選，A 的文件 Scope Metadata 不能直接當成 Production Incident Filters。後續只缺由治理權責人核准、具 Public Fact Provenance 的四 Scope 對照；本輪不自行批准或降低門檻。

- A：`Multiple services report database connection timeouts and dependency connection failures.` 使用實際事件支持的 shared database/network dependency operational context，四個 exact filters 從 manifest 的 `ops.shared-database-network-dependency-interruption` metadata 導出；候選適用 RUNBOOK，仍須真正 Provider／Chroma／既定 Policy 驗證。
- B：`Filesystem reports disk read errors and a storage volume is read-only.` 若 Incident Context 確實是 storage volume／filesystem disk I/O，使用該真實 context（不是為製造 NO_MATCH 填假 filter）；目前 Corpus 沒有專屬磁碟修復文件，候選 Knowledge Gap，仍以正式 Policy 結果為準。

上述候選不是 Oracle／Ground Truth，不注入 Scenario ID、不寫死結果、不調閾值。本輪測試以合成 unit vectors 驗證介面與 MATCH／NO_MATCH 分流，不是候選 Query 的真實相似度證據。既有 SPEC-015 `safe_text` 仍拒絕包含禁止字樣的 Knowledge Projection（包括部分 SOP 治理文字）；不刪改 Corpus 或放寬安全規則。成功 E2E 測試使用原始、可合法投影的 operational section；任意 SOP Section 全面可用尚未宣稱。

### Offline verification / Human handoff

`tests/test_knowledge_gemini_query_integration.py` 使用 Synthetic Credentials／MockTransport／Temporary Diagnostic SQLite／隔離 Chroma與A/B/C/D stores，驗證 guarded query→snapshot→validated evidence-grounded RCA→durable publication→public current read；未修改正式 Active／LKG。連同既有 Build、batch、admission、retrieval、snapshot、RCA 回歸交付 Review。**OFFLINE INTEGRATION PASS ≠ REAL EMBEDDING／REAL-RAG／RCA E2E PASS**。

Human Review Diff→限定離線複核→Human Git Checkpoint後，所有新 Campaign／Authorization Digest／E4／Safety 均須重綁 Exact Clean Source、final plan／input／payload／profile／有效期與真實 publication time。先補 Token／Quota／Budget 與精確 Historical Review Scope 決策，再由 PM 核准 Full／Query Live。Generated Chroma／Knowledge SQLite 不推 Git；Diagnostic attempts.sqlite 不是 Knowledge Index；API Key 不分享。

## Competition P0 Shared Demo — 2026-10-30

Authority：`PM-AIOPS-COMPETITION-SINGLE-DEMO-20261030`。PM已核准共享展示方向與必要離線修訂；相關PRD／SPEC精確新增條文仍待Human正式Approval。此區分適用於本頁下列historical setup／commands；**不要直接以歷史rebuild／real-integration test命令觸發新的Live呼叫**。

- 一套由富裕操作的受控Demo Host；其他成員可共用或錄製展示，不重跑四份Embedding、不共享Key。正式Four-member AC-014-X、Production及Formal驗收保留。
- 目前single probe為UNKNOWN／transport，零有效vector；舊Campaign已消耗一次Attempt，Hold8192 Usage／1 micro-USD及Fence保留。沒有新的Live授權，不重送、不Reset／Refund／清理，不以換Store／Host恢復呼叫資格。
- 本次最小Source修訂僅讓未來Observation保存非敏感Exception Type、最深可用Cause Type、實際HTTP Status及分類；不保存Exception Message／Traceback／Headers／Key，不反推歷史根因，不更改既有Outcome、Reservation或Retry規則。Proxy／CA根因尚UNKNOWN；不關閉TLS驗證，不推測後更改trust_env。

### 最短真實交付路徑與目前缺口

1. 完成已知Transport證據檢查；Host的真實Google連線能力尚未由成功Probe證明。使用既有網路設定及新的安全分類縮小問題，不拿Embedding API做未授權測試。
2. 舊正式Store的UNKNOWN Fence仍保留。依`PM-AIOPS-COMP-P0-UNBLOCK-20261030-01`及SPEC Lead對`cf02cd6e4ee94bd68f02dfbf99a75151`的Human Review確認，新增窄幅離線Candidate：未來獨立Live grant可在同一Authorization Record附加精確`p0_unknown_review`，僅此舊列對指定新Campaign可不作Blocking；不改原UNKNOWN／Hold／Raw Fence，不豁免其他列。正式例外尚未啟用，新Campaign本身仍需PM最終Live授權。新欄位受原Digest／E4／Safety綁定，沒有Review仍維持原Global Fence；不建立新Store／Adjudication Framework。
3. 本輪Source若獲接受，Human完成新clean Git Checkpoint；後續PM新Campaign／Authority／E4／Safety全部綁新Revision、原Ordinal52／Profile／Input／Payload、明確有效期與有限正值Budget。舊30b9e9e授權不能用於修改後程式。Human私下配置既有Credential並explicit opt-in，才可進行下一次授權的一次Probe。
4. 重用已核准Controlled Sequential Batching，另核准完整54-input固定Plan、逐batch真實Token／Payload／Quota證據及累積Usage／Cost。Probe的8192例外不能套用54。任一batch無可信Usage時仍STOP；沒有自動Adjudication、Refund、Retry或Resume。
5. **Provider／Build離線接線已完成，真實成果仍待Live。** 既有GoogleEmbeddingAdapter以明確`controlled_live_opt_in`轉接同一Diagnostic execution core，只有固定Plan完整54→54、事前Tolerance=0.01、逐批可信Usage持久化及全域驗證完成，才將向量交付ProviderEmbeddingResult與既有stage()／Chroma／validate。實際physical invocation_count如實回傳，Gemini2完整54／六Source允許在logical limits內多次；舊模型仍限一次，Partial／UNKNOWN／Persistence Failure不交付向量或建立索引。沒有vector import、第二次Embedding、跨次cache或新Store。Mock接線PASS不是Real Embedding／Knowledge Build PASS。
6. 隔離calibration／policy freeze與validation通過後，在獨立Demo namespace／stores作explicit Demo-scoped Activation，保留LKG及歷史refs；不用Production／Formal Authority或資料庫作Demo。
7. 綁定相同Gemini2 embedding space，為真實Query Embedding另備操作授權，或採經核准且有完整來源的固定Query vector流程。任意文字查詢需要實際Query Embedding，不因Document已建索引就離線可用。至少展示一組Approved SOP MATCH及一組NO_MATCH／Knowledge Gap，保存Snapshot／Source／Version／Chunk Provenance。
8. 重用SPEC-013 Evidence Snapshot、Candidate-C public Knowledge Snapshot、SPEC-015 Generation／Validation、SPEC-016 orchestration／publication及SPEC-011 singular Runtime，完成一條S2故事線；不能直接操控Domain DB或另建第二條RCA pipeline。LLM Generation同樣須有效Provider操作授權，範圍不從本PM scope決策自動取得。

### Team operation與Evidence

富裕負責共享Host的受控操作、Credential、Provider及Knowledge authority；其他三人共用UI／public reads／已錄製素材，協助場景講解、結果核對與錄製，不取得Key、不接管SQLite或Activation authority。共享展示不要求遠端部署平台、新帳號系統或四人獨立DB副本。PRD-005正式文件本Repository未找到，UI／Dashboard／Integration ownership仍由PM指定；最小部署條款僅為上述共享操作／分工／錄製，不更改其Domain。

Demo evidence至少包含exact Git／Host／Profile／Manifest、六份Source／Version、54 ordered chunk／vector mapping、768D／finite／nonzero／固定norm tolerance、Plan／Payload／Usage、Build／Validation／explicit Activation／LKG、Query／Snapshot／SOP Provenance及Incident／RCA public IDs／結果／時間／限制。Generated `var/knowledge_index/`／Chroma／Knowledge SQLite與正式Diagnostic Store皆不推Git；API Key不寫Evidence。Competition只記DEMO READY / BLOCKED，Formal另記FORMAL PASS / PENDING。目前Demo BLOCKED，Formal PENDING；不以historical skip／Mock PASS改寫。

### Gemini2 Build操作介面（僅離線驗證完成，尚無新Live授權）

沿用同一`manage_knowledge_index.py --config <reviewed-config>`，不修改tracked historical config的模型。明確選用Gemini2的非敏感配置須指定`model=gemini-embedding-2`、`capability_identity=spec014-google-gemini-embedding-2-capability-v1`、`embedding_profile_identity=spec014-gemini-embedding-2-embedding-v1`、768維、`normalization_semantics=provider-normalized-conditional-v1`、核准Profile Reference與有限limits。Loader會同步建立相同embedding space的RetrievalProfile，但這不表示已calibrate或已授權Query。

Gemini2的`maximum_invocations`是logical campaign physical上限（正值且≤54），每個實際SDK Request仍投影為`maximum_invocations=1`及30秒。`maximum_request_bytes`逐physical payload核對，Build另作累積容量必要檢查；這些配置不取代固定Plan／Source／Input／Model／Profile／Budget／SQLite／PM Authorization gates。Provider Port既有cost/rate/quota/resource units仍為交付item數量（成功54）；不是Google Quota、Observed Tokens或USD，與既有Attempt Usage／micro-USD Ledger分開，沒有重新定義或退款。

D-1未來目標Campaign的唯讀Preflight可使用既有`inspect_local_counting_store(..., fresh=False, for_authorized_campaign=True)`，同時揭露Raw `fences`與目標的`blocking_fences`，不能將前者當成已清除。新Authority尚未合法發布時不要開啟此模式來假定例外生效；舊一次性Handoff只接受零Raw Fence，不能直接用於新Campaign。下一次須以實際新Source／Live grant／Profile Revision／有效期完成Binding後由Human操作，不覆寫舊History或重用原已耗盡Campaign。

普通`run_live()`仍只交付Diagnostic Observation，不自動Build。受控向量接線只供另有合法Knowledge Build操作scope的明確rebuild使用；Diagnostic授權、Caller opt-in或本次離線PASS不能取代既有Compatibility Acceptance、隔離Calibration／Policy Freeze、Build／Activation各自的治理要求。

未來取得新clean Source Checkpoint、明確Full Campaign授權並合法處理UNKNOWN Fence後，Gemini2 `rebuild`須另有`--live-opt-in`才會進入既有受控Provider；未opt-in時在開啟Knowledge Store前拒絕。`validate`／`inspect`／explicit `activate`仍使用原有API；本輪只在隔離測試Store驗證，沒有正式Activation。`retrieve`對Gemini2目前明確拒絕未接通的Live Query流程，不能落回舊模型或借Document授權呼叫Query；本輪MATCH／NO_MATCH只用Mock Query Provider驗證真實Snapshot／Provenance介面。SPEC-015／016及Runtime Domain未重寫。

本文件記錄current implemented Candidate-C setup：approved six-document corpus、governed manifest、Google embedding adapter、Chroma index adapter、build／validate／activate／retrieve lifecycle、Knowledge Snapshot及public provenance。這不宣告live-provider PASS、real-RAG PASS、final RCA E2E或Production Ready。

## Closure verification disclosure

**AC-014-X: NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE。** AC-014-X未執行，這不是PASS，也沒有four-member evidence；Approved AC-014-X持續為normative requirement。PM僅授權current repository在此explicit verification exception下closure。

## Setup and credential boundary

以下為historical Candidate-C setup，不是新Gemini2 Live操作指令；Competition P0路徑與限制以上節為準。

安裝`requirements.txt`後使用tracked non-secret config `configs/knowledge_index.yaml`。Provider固定為Google `text-embedding-004`，index固定為ChromaDB。Credential不寫入config、manifest、logs或durable identity。

Live commands使用Google Vertex AI／ADC environment boundary：

```text
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=<non-secret project id>
GOOGLE_CLOUD_LOCATION=<non-secret location>
GOOGLE_APPLICATION_CREDENTIALS=<untracked local credential path, when ADC needs it>
```

Candidate C不建立credential registry、不選key、不做credential failover。SDK retry固定為一次execution。

## Local workflow

```powershell
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml admit
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml rebuild
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml validate --build-id <kbld-id>
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml inspect --build-id <kbld-id>
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml activate --build-id <kbld-id> --operation-key <operation-id> --expected-generation <n>
python scripts/manage_knowledge_index.py --config configs/knowledge_index.yaml retrieve --operation-key <operation-id> --query <text> --filter <key=value>
```

`admit`驗證schema 1.1 release、exact-six membership、Git source revision、raw bytes、metadata、contentfulness及security。`rebuild`建立immutable staged collection但不activate；`validate`成功後仍需explicit `activate`。`inspect`顯示SQLite semantic authority、Chroma artifact及local readiness。`retrieve`只查詢一次freeze的exact build。

## Authority and generated state

SQLite `var/knowledge_index/knowledge.sqlite3`是Candidate-C durable semantic authority。Chroma只提供index capability，位於`var/knowledge_index/chroma/`；collection listing、mtime或metadata不決定Active/LKG。整個`var/knowledge_index/`皆為generated local state且不得commit、分享或作team baseline。

不得修改或normalize `configs/knowledge_manifest.json`及`docs/knowledge/*.md`以規避hash mismatch。`.gitattributes`固定這些authority artifacts為LF checkout。

## Tests

Credential-free default regression：

```powershell
python -m pytest -q
```

Real Google／Chroma integration只有明確opt-in才執行：

```powershell
$env:RUN_SPEC014_REAL_INTEGRATION="1"
python -m pytest -q tests/test_knowledge_real_integration.py
```

只有actual live execution evidence才能宣稱live Google／real-provider validation PASS。Google adapter存在、Chroma adapter存在、credential-free default regression PASS或該test被skip，都不能冒充live-provider evidence，也不能證明real-RAG或final RCA E2E。
