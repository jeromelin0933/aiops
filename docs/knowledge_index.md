# SPEC-014 Candidate-C Knowledge Index Setup

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
