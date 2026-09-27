# SPEC-015 — LLM Generation & Validation

## Engineering Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-015 |
| Document Name | LLM Generation & Validation |
| Version | 1.0 |
| Status | Approved — Implementation Pending |
| Candidate | Candidate D |
| Requirement Authority | PRD-004 v1.0 Approved |
| Upstream Engineering Contracts | SPEC-012, SPEC-013, SPEC-014, SPEC-011, SPEC-008 current approved contracts |
| Date | 2026-09-27 |
| Approval Date | 2026-09-27 |

### Change History

| Version | Date | Status | Changes |
|---|---|---|---|
| 0.1 | 2026-09-27 | Draft | Phase 2 initial Engineering Contract draft；formalized Frozen D1–D8 plus D1-4／G1–G9；Phase 3 semantic review completed；F-001 identified |
| 1.0 | 2026-09-27 | Approved — Implementation Pending | Phase 4 narrow editorial patch completed；F-001 closed；Phase 3 re-review PASS；no blocker／major／minor findings remain；D1–D8 and G1–G9 preserved；approved as Candidate-D implementation baseline；implementation has not started |

### Status Honesty

本文件是SPEC-015 v1.0 Approved Engineering Contract，狀態為`Approved — Implementation Pending`。Approved不等於Implemented，也不是implementation completion evidence；Candidate-D production implementation尚未開始。Current repository已完成Candidate A／B／C，但尚未實作Candidate D LLM generation、Candidate E RCA orchestration或Candidate F evaluation。本文件不得被用來宣稱Gemini live validation、RCA Runtime complete、Candidate E或Candidate F complete、final RCA/RAG E2E或Production Ready。

---

# 0. Purpose, Authority and Decision Traceability

## 0.1 Purpose

本SPEC定義Candidate D如何消費immutable Evidence Snapshot與Knowledge Snapshot，執行bounded structured generation，驗證schema、reference、grounding及guidance authority，並建立immutable、可恢復、可由SPEC-012 Artifact handoff消費的`ValidatedGenerationResult`。

Candidate D的核心責任句為：

> **Generate from exact frozen inputs, validate every authoritative claim boundary, and publish only a typed immutable result.**

Candidate D不是RCA Runtime、Try ledger、Evidence authority、Knowledge authority、Incident authority或publication coordinator。

## 0.2 Authority Hierarchy

本SPEC依下列順序解讀：

1. PRD-004 v1.0 Approved；
2. SPEC-012、SPEC-013、SPEC-014、SPEC-011、SPEC-008 current approved contracts；
3. Frozen SPEC-015 Engineering Decisions D1／D7／D2／D3／D4／D5／D6／D8；
4. current repository implementation reality；
5. DDS、Runtime documentation及README。

Repository reality不得反向改寫Approved semantics。本SPEC不得修改upstream authority；若實作發現無法在public semantic ports下滿足本契約，必須停止並進行governed reconciliation，不得direct-read private persistence。

## 0.3 Frozen Decision Traceability

| Decision | Frozen allocation | Normative sections |
|---|---|---|
| D1 | Candidate D durable owns immutable validated-result content truth；A仍唯一擁有Try terminal outcome/history | 5, 6, 17, 21, 23 |
| D1-4 | Raw provider request/response不是primary authority；normal path不要求durable保存 | 6, 14, 15, 25 |
| D7 | Logical Try與physical invocation分離；same-Try eligibility不是第四種retry disposition | 18, 19, 22 |
| D2 | Versioned structured result、bounded claims及lossless Artifact projection | 5, 7, 8, 11 |
| D3 | Versioned Evidence reference、deterministic Observed Fact及grounding | 8, 9 |
| D4 | Exact Knowledge provenance、SOP authority及degraded behavior | 10 |
| D5 | Caller-selected exact Profile admission與continuity；no fallback | 12, 13, 14, 15 |
| D6 | One-invocation bounds、typed failure class及three-state retry safety | 16, 17 |
| D8 | Candidate-D-local readiness、integrity及D-owned recovery facts | 20, 21 |

上述決策均為Frozen。本Approved contract只formalize observable semantics，不重新提出alternatives。

---

# 1. Scope and Non-goals

## 1.1 In Scope

- Structured generation input admission。
- Versioned prompt、result schema、generation configuration及Profile continuity。
- Gemini provider adapter boundary，但不耦合RCA domain contract至Gemini response shape。
- One-invocation request、input、output、token、timeout及resource safety bounds。
- Structured parse、schema、reference、grounding、claim及guidance validation。
- Immutable `ValidatedGenerationResult` identity、durability、replay及recovery reads。
- Typed Candidate-D failure class及`RETRYABLE / NON_RETRYABLE / REPAIR_REQUIRED` retry safety。
- Same-Try physical reinvocation safety classification。
- Deterministic lossless projection至SPEC-012 `RcaArtifact` semantic content。
- Candidate-D-local readiness、integrity及recovery enumeration。
- Secret、outbound data及Ground Truth isolation。
- Bounded sanitized observability facts。

## 1.2 Non-goals

本SPEC不定義或實作：

- Aggregate、Attempt或Logical Try terminal outcome persistence；
- Runtime work、retry budget、backoff、wake time、exhaustion或scheduler；
- Candidate E cross-domain orchestration、publication或reconciliation；
- Candidate F evaluation、quality metrics或Ground Truth；
- Evidence collection、Revision或Materiality；
- Knowledge retrieval、applicability、Snapshot或Corpus governance；
- Incident mutation、Current relationship或lifecycle；
- automatic remediation；
- multiple-provider failover；
- raw provider response作primary authority；
- production credential registry、key selection或secret lifecycle。

---

# 2. Candidate-D Ownership Boundary

## 2.1 Candidate D Owns

Candidate D是下列facts的唯一domain authority：

- immutable ValidatedGenerationResult content；
- stable opaque `validated_result_id`及semantic commitment；
- result schema／contract version；
- exact generation input lineage retained by the result；
- validation facts and validation success；
- typed generation/provider/validation failure content；
- retry-safety classification；
- same-Try physical reinvocation safety classification；
- generation-specific Profile/capability admission；
- one-invocation safety bounds；
- deterministic Artifact semantic projection。

## 2.2 Referenced Authorities

| Domain truth | Sole authority | Candidate-D behavior |
|---|---|---|
| Aggregate／Attempt／Try outcome/history／Version／Artifact persistence | SPEC-012 | Read/reference approved lineage；never create competing truth |
| Evidence Snapshot／Revision／completeness／Materiality | SPEC-013 | Consume exact public reads；never mutate or recompute authority |
| Knowledge Snapshot／MATCH／NO_MATCH／applicability／guidance authority | SPEC-014 | Consume exact public reads；never upgrade or reinterpret authority |
| Retry timing／budget／next Try／clock／recovery execution | SPEC-011 | Return typed facts only；never schedule |
| Incident／RCA relationship | SPEC-008 | No direct mutation or private read |
| Cross-domain completion／publication | Candidate E | Expose D-owned facts for fresh-read reconciliation |

## 2.3 Explicit Authority Guardrails

Candidate D不得建立第二套Try outcome authority、Try ledger、Runtime retry database、Attempt allocator、Incident relationship、Evidence store或Knowledge store。D-side result或failure existence不宣告A-side Try已terminal，也不宣告Artifact或publication已完成。

---

# 3. Terminology and Closed Semantic Sets

| Term | Meaning |
|---|---|
| Structured Generation Input | Exact admitted references and bounded renderable content for one Attempt／Try execution |
| Physical Invocation | One bounded external provider call；not a Logical Try identity |
| ValidatedGenerationResult | Immutable Candidate-D-owned successful structured result after all validation gates |
| Validation Fact | Bounded non-secret evidence proving which versioned validations ran and passed |
| Evidence Reference | Versioned path and commitment into one exact Evidence Snapshot |
| Knowledge Reference | Exact typed identity and immutable provenance from one Knowledge Snapshot |
| Artifact Projection | Deterministic lossless mapping from one validated result to SPEC-012 Artifact semantics |
| Same-Try Reinvocation Eligibility | D-owned safety fact about possible physical reinvocation；not execution authorization |

Closed upstream retry disposition remains:

```text
RETRYABLE | NON_RETRYABLE | REPAIR_REQUIRED
```

No fourth upstream retry disposition is legal。

---

# 4. Structured Generation Input Contract

## 4.1 Required Semantic Inputs

One generation input must identify at least:

- `attempt_id` and `try_ordinal`；
- exact Evidence Snapshot and Evidence Revision identities；
- exact Knowledge Snapshot identity and typed resolution；
- provider, model and selected non-secret Profile identity；
- stable prompt identity/version；
- result schema identity/version；
- generation configuration identity/version；
- versioned one-invocation bounds；
- approved bounded Evidence and Knowledge projections；
- caller-provided opaque operation/reference facts required for replay，without scheduling authority。

## 4.2 Input Admission Invariants

Before provider invocation, Candidate D must:

1. resolve the exact SPEC-012 Attempt lineage through a public semantic read or caller-supplied authoritative projection；
2. resolve the exact SPEC-013 Evidence Snapshot and Revision through public reads；
3. resolve the exact SPEC-014 Knowledge Snapshot and required provenance through public reads；
4. prove all snapshot and generation identities match the pinned Attempt；
5. validate Profile/capability, prompt, schema, config and bounds；
6. validate outbound safety and Ground Truth isolation；
7. fail before provider invocation on any deterministic mismatch or authority contradiction。

Input admission must not infer Materiality, change Evidence completeness, reinterpret Knowledge applicability or authorize degraded continuation。

## 4.3 Bounded Rendering

Outbound content may be rendered only from:

- the approved versioned prompt template；
- the exact admitted Evidence Snapshot projection；
- the exact admitted Knowledge Snapshot projection；
- non-secret generation configuration required by the template。

No ambient filesystem discovery、private database content、Scenario metadata、validator answer、Ground Truth or unrelated Runtime state may enter the generation input。

---

# 5. ValidatedGenerationResult Domain Model

## 5.1 Required Result Semantics

A ValidatedGenerationResult must contain or losslessly determine:

- stable opaque `validated_result_id`；
- result schema and contract versions；
- immutable semantic commitment；
- `attempt_id` and `try_ordinal`；
- exact Evidence Snapshot／Revision and Knowledge Snapshot lineage；
- exact provider/model/profile/prompt/schema/config identities；
- bounded structured analysis；
- typed claim collection；
- bounded ranked hypotheses；
- remediation and prevention guidance；
- limitations and uncertainty；
- evidence completeness and knowledge gap projection；
- diagnostic conclusion；
- evidence and knowledge references；
- validation facts；
- bounded sanitized invocation metadata；
- deterministic Artifact semantic projection or all information required to derive it。

## 5.2 Immutability

After successful local commit, none of the result content, lineage, validation facts or commitment may be updated in place. Correction requires a governed new generation flow and must never rewrite the existing result。

## 5.3 Result Is Not a Published RCA Version

`validated_result_id` is not `version_id`、`publication_operation_id` or Current RCA identity. Result durability proves only Candidate-D validation success. It does not prove:

- A-side Try outcome recorded；
- Artifact committed；
- Version allocated；
- Incident relationship updated；
- publication complete。

---

# 6. Result Identity, Durability and Replay

## 6.1 Stable Identity and Commitment

Result identity must be stable, opaque and bound to one immutable semantic result. Its semantic commitment must cover all behaviorally authoritative result content and exact lineage. Exact encoding and hash algorithm are implementation-deferred；observable equivalence and contradiction behavior are not。

## 6.2 Commit Ordering

Validation success must be durably committed within Candidate D before downstream reports a successful result or requests A-side Artifact handoff：

```text
provider response
→ parse and all validation gates
→ atomic Candidate-D result commit
→ exact read-back / result reference
→ downstream A-side Try and Artifact handoff
```

Partial result, validation-in-progress state or raw provider response must not be exposed as a successful validated result。

## 6.3 Replay

- Same semantic operation and same result replay returns the existing result and identity。
- Same `validated_result_id` with contradictory content or lineage fails closed as identity/replay contradiction。
- Response loss after result commit must resolve the existing result；it must not blindly invoke the provider again。
- Repeated restart must not change result identity、content、commitment or lineage。

## 6.4 Raw Provider Response

Normal production behavior does not require durable raw provider request/response. Authoritative D persistence is limited to validated result、typed failure、validation/provenance facts and bounded sanitized metadata。

Any future raw debug capture must be explicit opt-in、redacted、bounded、non-authoritative and governed by separate access and retention policy. It must not affect result identity、validation、retry or recovery decisions。

---

# 7. Result Schema and SPEC-012 Artifact Projection

## 7.1 Versioned Schema

The result schema and validation contract must have explicit stable identity/version. Unknown、unsupported、malformed or incompatible schema fails before result admission. A behavior-affecting schema change must not masquerade as the same pinned Attempt configuration。

## 7.2 Lossless Projection

Each ValidatedGenerationResult must have exactly one deterministic lossless projection to the structured semantic content required by SPEC-012 `RcaArtifact`, including:

- summary and advisory severity assessment；
- diagnostic conclusion；
- ordered hypotheses and evidential support；
- supporting and contradicting evidence；
- remediation and prevention；
- `SOP_BACKED / MODEL_SUGGESTED` distinction；
- limitations；
- evidence completeness and knowledge gap；
- Evidence、Knowledge and generation provenance。

Equivalent projection must be deterministic across restart and replay. Projection must not query private upstream storage or introduce new claims。

## 7.3 A-side Metadata Boundary

SPEC-012 may add A-owned Aggregate、Version、role、publication and freshness facts. Those facts must not rewrite the validated analysis、claim text、ranking、support、guidance or conclusion. If the projection cannot be represented losslessly by the active SPEC-012 contract, handoff must fail closed and be reconciled through governance；D must not silently drop semantics。

## 7.4 Bounds

All collections and text fields must be finite and bounded. Exact hypothesis count、claim count、text length and payload/token limits belong to the versioned Generation Profile/configuration。

---

# 8. Claim Model

## 8.1 Required Claim Categories

The structured result must distinguish at least:

```text
OBSERVED_FACT
ANALYTICAL_INFERENCE
KNOWLEDGE_BACKED_GUIDANCE
```

Model-suggested guidance must remain distinguishable from knowledge-backed guidance. Guidance is not Incident factual evidence。

## 8.2 Claim Coverage

Every factual or causal assertion in the result must be represented by a typed claim and covered by valid references. This includes assertions appearing in summary、severity assessment、hypothesis statements、reasoning summaries and other presentation fields. Presentation prose cannot bypass the claim model。

Uncovered or unsupported factual/causal assertions fail validation. Uncertainty、limitations and clearly marked model suggestions are not factual claims, but must remain within their defined semantics。

## 8.3 Claim Integrity

Claims must use stable internal identities or an equivalent deterministic addressing rule. Claim order、reference set and semantic type must survive persistence and Artifact projection without silent coalescing or omission。

---

# 9. Evidence Reference and Grounding Validation

## 9.1 Evidence Reference Minimum Content

Each Evidence reference includes at minimum:

- `evidence_snapshot_id`；
- `reference_schema_version`；
- `canonical_path`；
- `canonical_fact_commitment`。

The reference identifies a fact inside the exact immutable Snapshot. It does not create a second Evidence Revision or Materiality authority。

## 9.2 Reference Resolution

Candidate D must resolve references through SPEC-013 public semantic content. Validation must reject:

- missing or wrong Snapshot；
- unsupported reference schema；
- invalid/non-canonical path；
- path outside the admitted Snapshot；
- missing fact；
- commitment mismatch；
- Snapshot/Revision contradiction；
- corrupt or unreadable Evidence authority。

`NOT_FOUND` is not a safe substitute for corruption or unreadability。

## 9.3 Observed Fact Determinism

Observed Fact authoritative payload is deterministic and derived from the referenced Snapshot fact. Presentation rendering may improve readability but must not change:

- entity；
- value；
- unit；
- timestamp or window；
- polarity or negation；
- source meaning。

The LLM must not invent facts absent from the Snapshot or treat omitted/truncated evidence as a complete world。

## 9.4 Analytical Inference

Every Analytical Inference requires:

- an explicit support set；
- an explicit contradiction set, which may be empty only when explicitly represented as empty；
- `HIGH / MEDIUM / LOW` evidential support；
- reasoning that does not claim stronger causal granularity than the available evidence。

Evidence references prove the source facts exist；they do not automatically prove the inference. Candidate D must validate claim structure and grounding boundaries without using the LLM as Ground Truth authority。

---

# 10. Knowledge Reference and Guidance Validation

## 10.1 Exact Provenance Binding

A Knowledge reference must bind the exact SPEC-014 typed Snapshot/chunk identity and immutable provenance required to resolve corpus、build/index、document/version、section、chunk、applicability and guidance-authority facts. D must consume public Snapshot/provenance reads and must not inspect private Chroma or SQLite state。

## 10.2 SOP_BACKED Admission

`SOP_BACKED` is legal only when the exact referenced chunk is:

- included in the exact Knowledge Snapshot；
- applicable according to Candidate-C authority；
- governed and production-eligible；
- granted valid guidance authority；
- explicitly `sop_backed_eligible=true`。

Similarity、rank、document name、`MATCH` alone or LLM self-assessment cannot grant `SOP_BACKED` authority。

## 10.3 NO_MATCH

For authoritative `NO_MATCH`:

- `knowledge_gap=true` is legal and required by the upstream projection；
- `MODEL_SUGGESTED` guidance may be present within bounds；
- no `SOP_BACKED` guidance is legal；
- model suggestions must not fabricate Knowledge references；
- model suggestions must not become Incident factual evidence；
- no automatic retry or relaxed applicability is inferred by Candidate D。

## 10.4 RETRIEVAL_UNAVAILABLE and Degraded Generation

`RETRIEVAL_UNAVAILABLE` must never be rewritten as `NO_MATCH`. It does not by itself authorize degraded continuation。

Evidence-only degraded generation may occur only when a governed upstream flow authorizes it and Trusted Evidence remains sufficient. Candidate D validates the supplied authorization/reference and resulting content boundary；it does not decide orchestration eligibility. In this path all guidance is `MODEL_SUGGESTED`; `SOP_BACKED` is forbidden。

Invalid trusted core、authority contradiction、`INVALID` or `REPAIR_REQUIRED` knowledge state fails closed and cannot be degraded。

## 10.5 Knowledge Cannot Establish Incident Fact

Knowledge may guide diagnosis or action but cannot alone establish an Incident factual or causal claim. Any Incident-specific factual/causal claim must have qualifying Evidence support independent of Knowledge references。

---

# 11. Diagnostic Conclusion and Evidential Support

## 11.1 Evidential Support

The closed semantic set is:

```text
HIGH | MEDIUM | LOW
```

It describes support from the referenced evidence, not numeric probability、statistical confidence or model self-assurance. Numeric model confidence must not be persisted or presented as this authority。

## 11.2 Diagnostic Conclusion

The result must use the PRD-004 conclusion semantics:

- `IDENTIFIED`：evidence supports the asserted operational causal granularity and no major contradiction remains；
- `MOST_SUPPORTED`：one hypothesis is best supported but the causal chain is insufficient for identified；
- `INCONCLUSIVE`：evidence is insufficient or competing hypotheses cannot reasonably be excluded。

`COMPLETED + INCONCLUSIVE` is legal. `DEGRADED` does not automatically forbid `IDENTIFIED`, but conclusion must not exceed the available evidence、omission facts or causal granularity。

## 11.3 Ranked Hypotheses

Hypotheses must be bounded、ordered、unique and continuously ranked. Each hypothesis carries statement、evidential support、support set、contradiction set、knowledge references where applicable and bounded reasoning summary。

---

# 12. Prompt, Schema and Generation Profile Identity

## 12.1 Attempt Pinning

One Attempt pins exactly one semantic combination of:

- provider；
- model；
- selected Profile；
- prompt identity/version；
- result schema identity/version；
- generation configuration identity/version。

Candidate D verifies these values against the exact admitted Attempt lineage and D-owned versioned resources. Any behavior-affecting mismatch rejects reuse of the current Attempt。

The pinned generation configuration identity must commit the exact result-schema identity/version and every other behavior-affecting generation setting not represented by a separate Attempt lineage field. If the authoritative Attempt projection cannot prove that commitment, Candidate D must reject invocation rather than create a parallel mutable Attempt binding。

Candidate D must not create a replacement Attempt. A required new Attempt belongs to the Candidate A／Candidate E governed flow。

## 12.2 Prompt Governance

Prompt templates must be versioned、resolvable、non-secret and Ground-Truth-free. Prompt changes that may affect behavior require a new identity/version. Runtime edits、personal files or untracked ambient prompt content cannot silently change an Attempt。

## 12.3 Generation Profile

The Generation Profile is a non-secret versioned capability/configuration identity. It defines or references provider/model capability、schema/prompt compatibility and invocation bounds. It does not contain credentials and is not a secret registry。

---

# 13. Provider Admission

## 13.1 Caller-selected Profile

The caller supplies the exact selected Profile identity. Candidate D validates that exact Profile and must not select another Profile、key、provider or model。

Admission requires at least:

- exact Attempt pin continuity；
- supported provider/model generation capability；
- supported prompt/schema/config compatibility；
- valid bounded invocation policy；
- secret boundary availability without disclosure；
- safe outbound payload；
- no hidden fallback or hidden retry；
- exact input lineage and public-authority reads。

## 13.2 Admission Failure

Missing Profile、capability insufficiency、pin mismatch、unsafe payload or unsupported compatibility must fail before provider invocation with typed failure class and retry safety. Candidate D must not silently switch to another Profile/model/provider or weaken bounds。

## 13.3 Dynamic Availability

Dynamic provider/model availability is an invocation admission fact, not Candidate-D local store readiness. A provider outage does not by itself make D local result authority `NOT READY`。

---

# 14. Credential and Secret Boundary

- Profile identity is non-secret and may appear in lineage and sanitized telemetry。
- Credential values remain in local environment、ADC or provider-adapter boundary。
- Candidate D does not own a credential registry、key selection、rotation or failover authority。
- Secret values must not enter prompt provenance、result、failure、Artifact、Snapshot、log、telemetry or semantic identity。
- Failure detail must be bounded and sanitized before crossing the adapter boundary。
- If payload safety cannot be proven before invocation, Candidate D fails closed without calling the provider。

---

# 15. Gemini Adapter Boundary

The approved PoC generation direction is Gemini 2.5 Flash. The adapter must implement a provider-neutral Candidate-D semantic port；Gemini SDK request/response types must not become the RCA result contract。

The adapter is responsible for one bounded physical invocation and sanitized provider facts. It must not:

- choose or switch Profile/provider/model；
- schedule retries；
- create Logical Try outcomes；
- validate against Ground Truth；
- promote raw response to success；
- hide additional SDK retry；
- return secret-bearing diagnostics。

A provider/model family change requires governance review according to PRD-004 and cannot be a silent configuration substitution。

---

# 16. Invocation Bounds

## 16.1 Required Bound Families

One physical invocation must be governed by versioned finite positive bounds for at least:

- request and rendered input size；
- output size；
- input/output/total tokens as supported by the provider contract；
- timeout；
- maximum provider invocations for the execution；
- rate、quota、cost and resource safety；
- bounded structured collection sizes。

Exact numeric values belong to versioned Generation Profile/configuration and are implementation-deferred。

## 16.2 Preflight and Accounting

Deterministically knowable bound violations must fail before provider invocation. Actual invocation count and bounded usage metadata must be observable without storing raw secret-bearing payloads。

Hidden SDK retry must be disabled or proven to be exactly accounted within the single Runtime-authorized execution contract. It must never create a second retry budget。

---

# 17. Typed Failure and Retry Safety

## 17.1 Separation

Every expected failure exposes:

```text
failure_class + retry_safety + bounded sanitized facts
```

Failure class describes what Candidate D understands. Retry safety remains exactly one of `RETRYABLE / NON_RETRYABLE / REPAIR_REQUIRED`. Runtime must not infer disposition from message text。

## 17.2 Minimum Failure Families

The typed model must distinguish at least:

- malformed provider output；
- structured parse failure；
- result schema failure；
- Evidence reference/schema/commitment failure；
- Knowledge authority/reference/guidance failure；
- grounding or unsupported-claim failure；
- invalid input or Ground Truth contamination；
- provider timeout or unavailability；
- provider quota/rate/resource exhaustion；
- capability/Profile/model mismatch；
- request/token/output/resource bound failure；
- identity or replay contradiction；
- local persistence/integrity/read failure。

Validation failure must not be collapsed into one generic bucket。

## 17.3 Disposition Rules

- Only explicitly regeneration-safe failure classes may be `RETRYABLE`。
- Deterministic rejection defaults to `NON_RETRYABLE`。
- Authority、identity or integrity contradiction is `REPAIR_REQUIRED`。
- Retry exhaustion is not a new Candidate-D failure class or disposition；it remains SPEC-011 operational state。
- A successful validated result is not a retry disposition。

Exact enum spelling may be implementation-deferred where the semantic distinctions above remain preserved。

## 17.4 Failure Persistence Boundary

Candidate D may durably preserve immutable typed failure、validation/provenance facts and bounded sanitized metadata for replay and recovery. A D-side failure record is authoritative only for Candidate-D failure content and retry-safety classification；it is not an Attempt lifecycle transition or Logical Try terminal outcome. Candidate A remains the sole durable authority that records whether a Try has terminally failed。

Equivalent failure replay must converge to the same D-owned facts. Same failure identity with contradictory class、disposition or lineage fails closed. Failure persistence must not allocate the next Try、consume Runtime budget or authorize execution。

---

# 18. Logical Try versus Physical Invocation

Logical Try identity is `(attempt_id, try_ordinal)` and remains SPEC-012 authority. A physical provider invocation is an external execution event and is not a new Try identity。

Candidate D may record bounded sanitized invocation metadata within its result/failure authority, but must not maintain Attempt lifecycle、ordered Try history or terminal Try status. Candidate A alone records authoritative Try outcome and history。

External exactly-once invocation is not guaranteed. Correctness requires no contradictory D result identity and no duplicate published RCA semantic effect。

---

# 19. Same-Try Physical Reinvocation Boundary

## 19.1 Eligibility Preconditions

The same `try_ordinal` may be considered for physical reinvocation only when all are true：

1. no Candidate-D durable ValidatedGenerationResult exists for the exact identity/lineage；
2. no Candidate-A authoritative Try outcome exists；
3. Candidate D explicitly classifies same-Try physical reinvocation as safe。

This eligibility is not a fourth retry disposition and is not execution authorization。

## 19.2 Expected-state Guard

Before any reinvocation, the governing flow must present fresh expected-state evidence. Candidate D must reject stale or contradictory expected state. Equivalent replay resolves existing D authority；contradiction fails closed。

## 19.3 Terminal Failure Boundary

Once Candidate A has durably recorded a Try failure, Candidate D must not silently reuse that Try. A next logical Try requires Candidate E／SPEC-011 authorization and the next valid ordinal under SPEC-012。

Whether or when any physical invocation occurs remains Candidate E／SPEC-011 authority。

---

# 20. Local Readiness and Integrity

## 20.1 Candidate-D-local Readiness

Local readiness covers only whether:

- D-owned result authority can be reliably opened and read；
- prompt/schema/config resources can be resolved and parsed；
- local schema and persistence integrity are valid；
- result identity/commitment/lineage invariants hold；
- recovery facts can be completely and reliably enumerated。

It is not whole-platform READY and does not include dynamic provider/model reachability。

## 20.2 Typed Read Semantics

Public reads distinguish conceptually:

```text
FOUND
NOT_FOUND
UNAVAILABLE
INVALID
REPAIR_REQUIRED
```

`NOT_FOUND` means reliable authoritative absence only. Corrupt、unreadable、partial、schema-incompatible or contradictory authority must never collapse into `NOT_FOUND` or empty enumeration。

## 20.3 Integrity

Integrity validation covers at least schema recognition、identity uniqueness、semantic commitments、exact lineage、immutable result content、failure records、replay evidence and complete recovery enumeration. Store-wide uncertainty fails closed. Normal runtime must not automatically choose a winner、rewrite、truncate or destructively repair authority。

---

# 21. Recovery and Public Semantic Reads

## 21.1 Required Public Capabilities

Candidate D must provide semantic capabilities equivalent to:

- resolve ValidatedGenerationResult by `validated_result_id`；
- resolve result by exact operation/Attempt/Try replay identity where applicable；
- read typed D-owned failure facts；
- validate deterministic Artifact projection；
- read local readiness and integrity；
- enumerate complete D-owned recovery facts。

Exact class/method/DTO names are not frozen。

## 21.2 Recovery Facts

D recovery facts may expose only D-owned facts such as:

- `validated_result_id`；
- `attempt_id`；
- `try_ordinal`；
- semantic commitment；
- exact Snapshot/Prompt/Profile/schema/config lineage；
- whether the local result is complete and resolvable；
- typed local failure/integrity facts where required。

They must not store or declare whether:

- A-side Try outcome is recorded；
- Artifact is committed；
- Version is allocated；
- publication is complete。

Candidate E fresh-reads each domain and determines cross-domain completion/reconciliation state。

## 21.3 Crash Cases

| Crash point | Candidate-D authority | Required behavior |
|---|---|---|
| Before provider invocation | No result | Safe admission may be retried only under governing flow |
| During/after provider call before D commit | No authoritative result | Same-Try eligibility requires all D7 guards；no claim of exactly-once call |
| After validation, during local commit | All-or-nothing local result | Partial success must not be visible |
| After D commit, before response/A handoff | Durable validated result | Resolve/replay same result；no blind regeneration |
| After A Try outcome, before Artifact commit | D result plus A authority | Candidate E reconciles through public reads；D does not declare completion |

---

# 22. Candidate-E and SPEC-011 Boundary

Candidate E owns cross-domain protocol composition and reconciliation. SPEC-011 owns when/order、retry budget/count、next logical Try authorization、backoff、absolute eligibility、exhaustion、scheduling、clock and restart continuation。

Candidate D only returns D-owned result、failure、retry safety、same-Try eligibility and readiness/recovery facts. It must not:

- create Runtime work；
- consume or reset retry budget；
- choose next Try ordinal；
- schedule wake-up；
- decide retry exhaustion；
- authorize degraded orchestration by itself；
- decide supersession、promotion or publication；
- reconcile A/B/C/E state into a new authority record。

Each retry or recovery execution must use fresh authoritative public reads. Candidate E determines whether a D result needs A-side Try/Artifact handoff or whether work is already complete、obsolete or repair-required。

---

# 23. SPEC-012 Handoff Boundary

## 23.1 Successful Handoff

Candidate D returns a stable `validated_result_id` and immutable validated result. Candidate A records the authoritative Logical Try outcome by reference and, under the governed flow, persists the lossless Artifact projection and Version semantics。

Candidate D does not call A private persistence and does not claim that returning a result completes the Try。

## 23.2 Failure Handoff

Candidate D returns typed failure class、retry safety and bounded safe facts. Candidate A remains sole authority for whether/how a terminal Logical Try outcome is durably recorded. Candidate D failure persistence must not masquerade as A-side Attempt lifecycle or Try history。

## 23.3 Lineage

The D result and Artifact projection must match the exact Attempt Evidence Snapshot／Revision、Knowledge Snapshot and non-secret generation provenance. Mismatch fails closed；D must not repair A lineage or create a replacement Attempt。

---

# 24. Security and Ground Truth Isolation

## 24.1 Forbidden Production Inputs

Production generation、prompt rendering、reference validation and grounding must reject:

- `scenario_id` or S1～S6 mapping；
- Generator state；
- validator expected answer/output；
- expected root cause or accepted answer；
- evaluation Ground Truth/labels/run identity；
- fixture-only causal metadata；
- unapproved files or ambient local content；
- credential values or authorization headers。

Removing a field name while retaining encoded answer content remains a violation。

## 24.2 Model Is Not Validation Authority

Gemini may generate candidate structured content. It must not determine Ground Truth、Evidence existence、Knowledge applicability、SOP authority、Materiality or whether its own unsupported claim is valid. Validation uses deterministic contract rules and upstream public authority。

## 24.3 Outbound Safety

Only approved Snapshot content and prompt/config facts may leave the process boundary. Allowlist、redaction and bounds run before invocation. Unsafe or uncertain content fails closed without sending the request。

---

# 25. Observability and Sanitized Metadata Boundary

Observability may include bounded non-secret facts such as:

- Attempt/Try/result opaque identities；
- provider/model/Profile/prompt/schema/config identities；
- validation stage and typed failure class；
- retry-safety disposition；
- same-Try eligibility；
- invocation count and bounded usage units；
- duration and timeout category；
- result commitment and local readiness status。

Observability must not become result、Try、Runtime or publication authority. It must not contain raw prompt/payload/response、credentials、authorization headers、Ground Truth or unbounded provider exceptions. Exact event/metric names are implementation-deferred。

---

# 26. Acceptance Criteria

## AC-015-A — Result Durability and Identity

After successful validation, exactly one immutable result is durably committed before success is returned. The same semantic operation resolves the same `validated_result_id` and content across response loss and restart。

## AC-015-B — Replay and Contradiction

Equivalent same-identity replay returns the existing result without duplicate authority. Same identity with different result、commitment or lineage fails closed as repair-required identity/replay contradiction。

## AC-015-C — Crash Before A Handoff

A crash after D validation commit but before A-side Try/Artifact handoff is recoverable by resolving the existing D result. Recovery does not blindly regenerate or claim A-side completion。

## AC-015-D — Raw Provider Non-authority

Normal production success、replay and recovery require no durable raw provider response. Raw request/response cannot become primary result、grounding or retry authority。

## AC-015-E — Structured Schema

Malformed provider output、parse failure、unknown/invalid schema and contract incompatibility cannot produce a validated result or successful Artifact handoff。

## AC-015-F — Lossless Artifact Projection

Every admitted result produces one deterministic lossless SPEC-012 Artifact semantic projection. Repeated projection is identical；A-side metadata cannot change validated analysis content。

## AC-015-G — Claim Coverage

Every factual/causal assertion in summary、severity、hypotheses and reasoning is represented by a typed claim with valid reference coverage. An uncovered assertion fails validation。

## AC-015-H — Observed Fact Grounding

Observed Fact resolution uses exact Snapshot/path/commitment. Rendering preserves entity、value、unit、time/window、polarity and source meaning. A fact absent from the Snapshot cannot pass。

## AC-015-I — Evidence Reference Versioning

Unsupported reference schema、non-canonical path、wrong Snapshot、missing fact or commitment mismatch is a typed validation failure and never resolves as a valid reference。

## AC-015-J — Analytical Inference

Each inference has explicit support and contradiction sets plus `HIGH / MEDIUM / LOW` evidential support. Numeric confidence or model self-assurance is not accepted as evidential support。

## AC-015-K — Knowledge Provenance

Every Knowledge reference resolves exact Candidate-C Snapshot/chunk and immutable governance provenance through public semantic reads. Dangling、wrong-build/version or contradictory references fail closed。

## AC-015-L — SOP_BACKED Eligibility

`SOP_BACKED` passes only for an exact included、applicable、governed、guidance-authorized and `sop_backed_eligible=true` chunk. Similarity/rank/model assertion alone fails。

## AC-015-M — NO_MATCH

`NO_MATCH` produces legal `knowledge_gap=true`; bounded `MODEL_SUGGESTED` guidance may pass without fabricated Knowledge references. `SOP_BACKED` and Knowledge-created Incident facts fail validation。

## AC-015-N — RETRIEVAL_UNAVAILABLE

`RETRIEVAL_UNAVAILABLE` remains distinct from `NO_MATCH` and does not automatically authorize generation or degradation。

## AC-015-O — Evidence-only Degraded Gate

Evidence-only degraded generation succeeds only with governed-flow authorization and sufficient Trusted Evidence. Guidance is only `MODEL_SUGGESTED`. Invalid trusted core or authority contradiction fails closed。

## AC-015-P — Knowledge Cannot Create Incident Fact

A factual or causal Incident claim supported only by Knowledge fails validation regardless of applicability or SOP authority。

## AC-015-Q — Profile Continuity

The exact caller-selected Profile/provider/model/prompt/schema/config matches the pinned Attempt. Any behavior-affecting mismatch rejects reuse；D does not create another Attempt。

## AC-015-R — No Silent Fallback

Unavailable or insufficient Profile/model/provider produces typed admission failure. No alternate Profile、key、model or provider is selected silently。

## AC-015-S — Secret Isolation

Credentials and secret-bearing provider errors do not enter result、failure、prompt provenance、Artifact projection、logs、telemetry or identities. Unsafe outbound content fails before provider invocation。

## AC-015-T — Provider Unavailable

Provider timeout/unavailability produces a distinct typed failure and retry-safety classification without a validated result. It does not by itself make the D local authority unreadable/not-ready。

## AC-015-U — Failure Taxonomy

Malformed output、parse/schema、Evidence reference、Knowledge authority、grounding、Ground Truth contamination、provider、Profile、bounds、replay and integrity failures remain semantically distinguishable。

## AC-015-V — Retry Safety

Only explicitly regeneration-safe failure classes are `RETRYABLE`; deterministic rejection defaults `NON_RETRYABLE`; authority/integrity contradiction is `REPAIR_REQUIRED`. No fourth disposition exists。

## AC-015-W — Hidden Retry Prevention

One authorized execution cannot cause an unaccounted SDK retry or second retry budget. Invocation count/bounds are testable and observable。

## AC-015-X — Same-Try Reinvocation

Same ordinal eligibility requires no D result、no A Try outcome and explicit D safety. Missing any condition rejects same-Try reuse. Eligibility itself does not execute or schedule work。

## AC-015-Y — Next Logical Try Authority

After durable A-side Try failure, Candidate D cannot reuse the same Try. Candidate E/SPEC-011 must authorize the next logical Try, preserving A-owned history and Runtime budget。

## AC-015-Z — Local Readiness

Local readiness validates D result authority、resources、schema/config、integrity and complete enumeration only. Dynamic provider outage remains invocation admission state。

## AC-015-AA — Typed Reads

Reliable absence returns `NOT_FOUND`; unreadable、corrupt、invalid or contradictory authority returns the corresponding non-absence semantic and never empty/Not Found。

## AC-015-AB — Recovery Enumeration

Recovery enumeration is complete and stable across restart and exposes only D-owned result/failure/lineage facts. It does not declare Try、Artifact or publication completion。

## AC-015-AC — Ground Truth Isolation

Scenario、Generator、validator expected answer and Ground Truth are rejected from production input、prompt、reference、identity and validation authority. Gemini cannot act as validator authority。

## AC-015-AD — Authority Preservation

Candidate D neither direct-reads private upstream stores nor mutates Incident、Evidence Snapshot、Knowledge Snapshot、Attempt、Try history or Runtime work. Candidate E and all upstream authorities remain intact。

---

# 27. Required Test Strategy

This Approved contract defines required verification only；approval does not implement or execute tests。

## 27.1 Pure Deterministic Unit Tests

- identity/commitment equivalence and contradiction；
- schema and contract version validation；
- claim coverage and conclusion rules；
- Evidence path/commitment resolution；
- Knowledge guidance-authority rules；
- deterministic Artifact projection；
- bounds preflight and sanitized metadata。

## 27.2 Fake-provider Contract Tests

- valid structured response；
- malformed/empty/incompatible response；
- timeout、unavailable、quota/rate/resource failures；
- one invocation only and hidden-retry prevention；
- secret-bearing exception sanitization；
- no silent model/Profile/provider fallback。

## 27.3 Persistence, Replay and Restart Tests

- atomic result commit；
- response loss after commit；
- equivalent replay；
- contradictory identity/content；
- restart result resolution；
- crash before A handoff；
- complete recovery enumeration；
- store-wide integrity uncertainty。

## 27.4 Validator Tests

- structured parse/schema；
- factual claim coverage；
- Observed Fact immutable semantics；
- inference support/contradiction sets；
- conclusion/evidential-support calibration boundaries；
- NO_MATCH、RETRIEVAL_UNAVAILABLE and degraded gate；
- Knowledge-only factual claim rejection。

## 27.5 Security and Ground Truth Tests

- Scenario/fixture/validator/Ground Truth rejection；
- prompt and outbound allowlist；
- credential and authorization-header exclusion；
- bounded failure/telemetry；
- raw provider response non-authority。

## 27.6 Public Port and Cross-SPEC Contract Tests

- only SPEC-013 public Snapshot/Revision reads；
- only SPEC-014 public Snapshot/provenance reads；
- no private SQLite/Chroma dependency；
- exact SPEC-012 Attempt lineage and Artifact projection handoff；
- D result/failure does not become A Try outcome；
- Candidate E/SPEC-011 remains next-Try and scheduling authority。

## 27.7 Failure Taxonomy and Recovery Tests

- every minimum failure family has deterministic classification；
- regeneration-safe versus deterministic rejection；
- repair-required contradiction；
- same-Try eligibility guards；
- typed absence versus corruption/unavailable；
- provider outage does not invalidate local-store readiness。

## 27.8 Live Gemini Validation

Live Gemini generation validation is explicit opt-in and credential-dependent. It must use the approved configured model/Profile and exercise the same adapter contract as fake tests. Default regression must not require live credentials or network access。

Fake-provider PASS、adapter existence、skipped live test or default regression PASS must not be reported as real Gemini、final RCA E2E or Production Ready evidence。

---

# 28. Implementation-deferred Choices

The following remain implementation/configuration choices unless later repository compatibility proves a normative need：

- exact module、class、method、port and DTO names；
- exact database/table/index/column schema；
- exact enum spelling where semantic sets are preserved；
- exact identity encoding and hash algorithm；
- exact numeric request/token/timeout/size/rate/quota/cost/resource bounds；
- exact Gemini SDK call/config shape；
- exact prompt/schema/config file paths and serialization；
- exact telemetry metric/event names；
- exact CLI name；
- exact migration mechanism；
- exact lock/CAS/SQLite library details；
- future governed raw debug capture implementation。

Implementation choices must not weaken authority、identity、immutability、grounding、secret isolation、retry boundaries、recovery or Fail-Closed requirements。

---

# 29. Repository Reality and Implementation Gate

Current repository reality at baseline `34338ab37f3aefcdeb77c6975250d99aafa82853`：

- SPEC-012 Candidate A is implemented；
- SPEC-013 Candidate B is implemented；
- SPEC-014 Candidate C is implemented, with AC-014-X explicitly not executed under its recorded closure；
- `google-genai==2.25.0` and a Candidate-C Google embedding adapter exist；
- no production Gemini generation adapter exists；
- no Candidate-D prompt/schema/result store/validator/failure/recovery module exists；
- Candidate E RCA Runtime orchestration and Candidate F evaluation remain pending。

Candidate-C embedding code may inform SDK、timeout、ADC、hidden-retry and sanitization patterns, but it is not Candidate-D generation authority and its fixed embedding model/failure semantics must not be reused as LLM generation truth。

This Approved contract is the Candidate-D implementation baseline. Approval does not establish that implementation or validation has started or completed；Candidate-D production implementation remains pending. Approval and implementation execution remain separate gates。

---

# 30. Approval Checklist

- [x] Frozen D1／D7／D2／D3／D4／D5／D6／D8 decisions are complete and preserved。
- [x] D1-4 raw provider response boundary is preserved。
- [x] G1～G9 final consistency guardrails remain satisfied；Final Frozen Decision Consistency Audit passed。
- [x] Phase 3 semantic review is complete；F-001 is closed；Phase 3 re-review passed。
- [x] No open BLOCKER、MAJOR or MINOR finding remains。
- [x] No upstream PRD/SPEC change is required。
- [x] D1 result authority and A-side Try authority remain separated。
- [x] Candidate E orchestration/reconciliation scope and SPEC-011 Runtime authority remain intact。
- [x] Candidate F evaluation and Ground Truth remain out of production scope。
- [x] Acceptance criteria remain testable semantic behavior。
- [x] Required test strategy keeps default regression independent of live provider；live Gemini remains explicit opt-in。
- [x] Approval status does not claim implementation or test completion；Candidate-D implementation remains pending。

---

# 31. Approval Status

```text
Version: 1.0
Status: Approved — Implementation Pending
Implementation: Not Started
Approval Date: 2026-09-27
Final Consistency Audit: PASS
Phase 3 Re-review: PASS
Open Findings: NONE
```
