# SPEC-016 — RCA Orchestration / Publication / Recovery

## Engineering Specification v1.1

---

## Document Metadata

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-016 |
| Document Name | RCA Orchestration / Publication / Recovery |
| Version | 1.1 |
| Status | Implemented |
| Date | 2026-09-29 |
| Approval Date | 2026-09-29 |
| Requirement Authority | PRD-004 v1.0 Approved |
| Runtime Authority | SPEC-011 v1.2 |
| Upstream RCA Contracts | SPEC-012～015 |
| Incident Contract | SPEC-008 |
| Candidate | Candidate E |
| Implementation Owner | 夜羽 |

### Change History

| Version | State | Summary |
|---|---|---|
| v0.1 | Draft | Phase 2 initial Engineering Contract; formalized E1–E4. |
| v0.1 | Semantic Review / Narrow Patch | Phase 3 identified F-001～F-005; Phase 4 narrow patch closed all findings; E1-C1 / E2-C1 / E3-C1 frozen clarifications added; Phase 3 re-review PASS. |
| v1.0 | Approved — Implementation Pending | SPEC Lead Final Review PASS; BLOCKER / MAJOR / MINOR = NONE; E1–E4 and E1-C1 / E2-C1 / E3-C1 frozen as Candidate-E implementation baseline; implementation has not started. |
| v1.1 | Implemented — Documentation-only Closure | Candidate-E implementation commit `eff7eb067cc09974c7679050f5136a7cfdffadaf` accepted after Post-Handoff Final Full Contract Re-audit PASS and SPEC Lead Final Review PASS. E1～E4, E1-C1 / E2-C1 / E3-C1 and AC-016-A～Z were preserved. Full regression: `2064 passed, 4 skipped`. Docker and Live Gemini: `NOT EXECUTED`; Candidate F and final RCA／RAG evaluation: Pending; Production Ready: `NO`. Git sequence deviation: `PM-AUTHORIZED WIP HANDOFF CHECKPOINT`. |

### Status Honesty

Draft ≠ Approved. Approved ≠ Implemented. SPEC-016 v1.0 remains the Approved normative Candidate-E Engineering Contract; v1.1 records documentation-only implementation closure and does not add or alter normative requirements.

```text
SPEC-016 normative baseline = Approved v1.0
SPEC-016 current status = Implemented (v1.1 documentation-only closure)
Candidate-E Engineering Contract = Approved

Candidate A = Implemented
Candidate B = Implemented
Candidate C = Implemented
Candidate D = Implemented

Candidate E = Implemented
Candidate E implementation commit = eff7eb067cc09974c7679050f5136a7cfdffadaf
Candidate F = Pending

RCA Runtime integration within the singular SPEC-011 Runtime = Implemented
Final RCA/RAG E2E = Pending
Docker Candidate-E execution = NOT EXECUTED
Live Gemini = NOT EXECUTED
Production Ready = NO
```

Candidate-C AC-014-X remains `NOT EXECUTED — PM-DIRECTED SKIP FOR CURRENT CLOSURE`; this is not PASS. SPEC-011 remains the singular Runtime authority. Candidate E implements the approved RCA orchestration protocol inside that existing Runtime framework and does not create a second Runtime, scheduler, Runtime Clock, retry authority or recovery framework. Candidate F, final real-LLM／RAG quality evaluation and Production Ready remain outside this closure.

---

# 0. Purpose, Authority and Frozen Decisions

## 0.1 Purpose

SPEC-016 defines the RCA-specific orchestration protocol by which the existing SPEC-011 Runtime composes the public semantic capabilities of SPEC-008 and Candidates A～D. It governs stable RCA obligation identity, cross-domain ordering, Attempt／Try continuation, two-lane retry, coalesced follow-up, refresh admission, publication, publication reconciliation, startup classification and Runtime completion.

The core responsibility rule is:

> **Engine decides. State remembers. Runtime orchestrates. Domain stores own the side effects.**

Candidate E composes and reconciles. It does not acquire any upstream business truth or create another Runtime.

## 0.2 Authority Order

Conflicts are resolved in this order:

```text
Approved PRD
>
Approved active SPEC
>
Frozen PRD-004 Engineering Decisions
>
SPEC-016 Frozen E1～E4
>
Repository Reality
>
DDS / runtime docs
>
README
```

Repository reality is implementation compatibility evidence. It cannot rewrite approved authority or Frozen E1～E4. If these authorities cannot be satisfied together, affected work must stop for governed review; implementation convenience cannot alter this contract.

## 0.3 Frozen E1～E4 Traceability

| Decision | Frozen outcome | Normative sections |
|---|---|---|
| E1 | Incident-root hierarchical deterministic identity | 4, 5, 17 |
| E2 | One Generation-Attempt-scoped shared invocation budget with authority-driven Same-Try／Next-Try transition | 8, 12, 17 |
| E3 | Incident-scoped singleton follow-up root with typed unresolved requirement-frontier coalescing | 9, 12, 17 |
| E4 | Receipt-first layered authoritative publication recovery classification | 11, 12, 17 |

Exact string, UUID, digest, serialization and hash encoding remain implementation choices. Their semantic inputs, domain separation, determinism, replay and contradiction behavior are normative.

## 0.4 Repository Compatibility Evidence

Current production code exposes public semantic capabilities compatible with this approved Engineering Contract, including:

- Candidate A Aggregate／Attempt／Try／Version／Current／publication reads, mutations and recovery-candidate enumeration;
- Candidate B capture outcome, Snapshot／Revision, Materiality, finalization and recovery facts;
- Candidate C frozen retrieval operation, typed resolution, immutable Knowledge Snapshot, unavailable finalization and public provenance;
- Candidate D result／failure reads, Same-Try safety, recovery facts and lossless Candidate-A projection;
- SPEC-008 RCA relationship and same-operation publication receipt／result reads plus authorized publication mutation;
- the existing Candidate-A／SPEC-008 caller-driven publication coordinator;
- SPEC-011 D2 work continuity, durable retry, Runtime Clock and Startup Recovery Barrier foundations.

These findings prove compatibility surfaces, not Candidate-E implementation. Current SPEC-011 Runtime work types and worker integration do not yet implement this RCA protocol.

---

# 1. Scope and Non-scope

## 1.1 In Scope

Candidate E owns the protocol for:

- initial RCA obligation discovery and continuation;
- cross-domain sequencing through public semantic ports;
- stable orchestration identity composition;
- Evidence capture and Knowledge resolution ordering;
- Attempt admission, Try execution ordering and validated-result handoff;
- Same-Try and Next-Try continuation under one durable budget per Generation Attempt;
- singleton follow-up coalescing and typed unresolved requirement-frontier processing;
- Material Evidence, post-context and STALE refresh admission orchestration;
- publication intent, mutation, completion and reconciliation ordering;
- RCA-specific startup recovery classification;
- RCA Runtime completion criteria.

## 1.2 Non-scope

SPEC-016 does not define or implement:

- a new Runtime, scheduler, worker loop, retry database, Startup Recovery framework or clock;
- new Materiality semantics or an Evidence authority;
- new RAG, applicability, retrieval or Knowledge semantics;
- new LLM generation, grounding, validation or Same-Try safety semantics;
- a new Artifact, Version, Current, Freshness or Try-history authority;
- a new Incident relationship or publication-winner authority;
- Candidate F evaluation, final RCA／RAG E2E evaluation or full production readiness;
- shared cross-domain business storage, cross-store transactions, 2PC or private domain-table access;
- exact files, classes, tables, schemas, DTO spellings, operation-ID encoding or implementation commits.

---

# 2. Authority Map

| Contract | Authoritative truth / side effect | SPEC-016 boundary |
|---|---|---|
| SPEC-008 | Incident-side RCA relationship, publication mutation, publication receipt and Incident Current relationship | E invokes public reads／mutation only; never direct-writes Incident persistence or guesses a winner. |
| SPEC-011 | WHEN, ORDER, retry scheduling／timing, durable Runtime budget, Runtime Clock, Startup Recovery Barrier, worker lifecycle, controlled drain and cross-store coordination | E is an RCA-specific protocol integrated into this singular framework. |
| SPEC-012 / Candidate A | Aggregate, Attempt, authoritative Try outcome history, Artifact, Version, Current, Freshness／STALE and A-side publication record | E fresh-reads and composes public capabilities; A owns all local side effects and facts. |
| SPEC-013 / Candidate B | Evidence capture, Snapshot, Revision, Materiality and post-context collection facts | E determines when to request work; B alone judges Materiality. |
| SPEC-014 / Candidate C | Active corpus／build, retrieval, applicability, MATCH／NO_MATCH, Knowledge Snapshot and provenance | E consumes typed resolution; AC-014-X remains not executed. |
| SPEC-015 / Candidate D | Generation, validation, grounding, typed failure, retry safety, Same-Try local safety and durable validated result | E schedules and composes; D owns subject-level result and safety facts. |
| SPEC-016 / Candidate E | RCA obligation protocol, cross-domain sequence, stable orchestration identities, retry-lane transition, coalescing, publication reconciliation and RCA recovery classification | E never duplicates domain business truth or SPEC-011 Runtime authority. |

No E-owned record may override an authoritative fact in these contracts.

---

# 3. RCA Obligation Model

## 3.1 Obligation Subjects

One durable authoritative Incident creates at most one stable RCA obligation root. The root may carry sequential obligations for:

1. initial RCA;
2. Material Evidence refresh;
3. post-context follow-up;
4. STALE Current refresh;
5. Generation continuation;
6. Artifact publication;
7. Publication Reconciliation;
8. restart recovery.

These are stages or requirements beneath one Incident root, not independent competing RCA roots.

## 3.2 Discovery and Reconstruction

An obligation is discoverable or reconstructible only from authoritative public facts, including:

- durable Incident existence and Incident-side RCA relationship from SPEC-008;
- Aggregate／Attempt／Try／Version／Current／Freshness／publication facts from A;
- capture outcome, Snapshot／Revision, Materiality and post-context facts from B;
- Retrieval Operation／Knowledge Snapshot／typed resolution facts from C;
- validated result, typed failure and Same-Try safety facts from D;
- D2 orchestration continuity and durable retry／wake facts from SPEC-011.

D2 absence never proves that an RCA obligation is absent. When domain facts uniquely establish the semantic subject, stage and required stable identities, the same Runtime continuity may be reconstructed. If identity, used budget or stage cannot be recovered without guessing, the affected subject is `REPAIR_REQUIRED` and fails closed.

## 3.3 Lifecycle Admission

Initial RCA is assignment-independent after a durable Incident exists. Automatic refresh follows PRD-004 lifecycle constraints: OPEN／ASSIGNED／IN_PROGRESS may admit automatic refresh; AWAITING_REVIEW may finish already scheduled or known obligations; CLOSED admits no new automatic refresh and no silent Current replacement after closure. Candidate E does not move Incident lifecycle backward.

---

# 4. Identity Model — E1

## 4.1 Stable Incident Root

Each Incident has one deterministic, stable RCA work root derived from the authoritative `incident_id` and an RCA-root semantic purpose discriminator. The same Incident must resolve the same root across restart, retry, refresh, publication reconciliation and Runtime work reconstruction.

The root is not an Aggregate ID, Attempt ID, Runtime Work ID or domain operation ID. It is the parent orchestration identity from which distinct semantic operation identities are composed.

## 4.2 Hierarchical Deterministic Derivation

Candidate-E caller-generated identities must be deterministically composed from:

```text
stable Incident RCA root
+ semantic subject
+ operation purpose
+ versioned identity contract discriminator
```

Required semantic relationships include:

| Subject / operation | Required stable inputs |
|---|---|
| Aggregate discover/create | RCA root + Incident subject + aggregate-obligation purpose |
| Evidence capture | RCA root + durable frozen Capture Command Basis + capture purpose |
| Materiality evaluation | RCA root + explicit A baseline Revision + candidate Revision + rule identity |
| Knowledge retrieval | RCA root + admitted Attempt basis or equivalent fixed query subject + retrieval purpose |
| Attempt admission | RCA root + exact Evidence Snapshot／Revision + Knowledge Snapshot + pinned generation lineage |
| D execution | RCA root + Attempt + Try ordinal + execution purpose |
| Artifact commit | RCA root + authoritative validated result／Try subject + artifact-commit purpose |
| Publication | RCA root + committed target Version + expected Current precondition + publication purpose |
| Follow-up | RCA root + singleton follow-up purpose; changing requirement basis is coalesced state, not a new root |

Domain operation identities are independent. Capture, retrieval, D execution, Artifact commit and publication must not share one undifferentiated `operation_id`.

## 4.3 Capture Command Basis — E1-C1

Before the first possible Candidate-B effect for a capture, Candidate E／SPEC-011 must durably freeze a complete, restart-reconstructible Capture Command Basis. It contains at least:

```text
Incident / stable RCA root reference
capture purpose
authoritative snapshot_at
required Evidence / collection-boundary references
capture contract version
canonicalization version
source policy version
evidence-affecting configuration / bounds identity
```

The required order is:

```text
durably freeze the complete Capture Command Basis
→ derive the stable capture_operation_id from that basis and purpose
→ invoke Candidate B
```

The frozen basis must cover every SPEC-013 capture replay-equivalence field that can affect authoritative output. Restart reuses its original authoritative `snapshot_at` and semantic command; it must not obtain a new wall-clock value or reuse the old `capture_operation_id` with a different command. If the complete frozen basis cannot be authoritatively recovered, the capture is `REPAIR_REQUIRED` and fails closed. Exact encoding and physical persistence remain implementation-deferred.

## 4.4 Replay versus New Semantic Action

- Equivalent repetition of the same semantic action reuses its original domain operation identity.
- A new Evidence capture basis, new Attempt lineage, next logical Try, new Artifact target or new publication target is a new semantic action and receives its own deterministically derived identity.
- A physical retry of the same domain action reuses the domain identity unless the owning domain contract requires a new logical action, such as the next valid Try ordinal.
- A coalesced follow-up basis update does not create a parallel follow-up root.

## 4.5 Recovery Reconstruction

Every caller-generated identity required for correctness must either:

1. be deterministically reconstructible from authoritative facts; or
2. already be durably retained in an authoritative domain receipt／store.

If neither is true, execution must not invent a replacement identity. It fails closed as `REPAIR_REQUIRED`.

## 4.6 Forbidden Identity Inputs

Stable semantic identity must not depend on a newly observed wall-clock timestamp, random restart UUID, PID, worker instance, process generation, retry wake time, message text, filesystem order or telemetry sequence. The durable authoritative `snapshot_at` frozen in the Capture Command Basis is a required SPEC-013 replay-equivalence input and may participate in capture identity derivation; restart must reuse that exact absolute instant. Other authoritative time may be an execution input or result fact but is not replay identity unless an upstream contract explicitly makes it semantic.

---

# 5. Durable Runtime Continuation

## 5.1 D2 Content

Within the existing SPEC-011 D2 framework, RCA Runtime continuity may durably retain only what authoritative domain stores cannot completely express:

- stable RCA root and Runtime work identity;
- orchestration stage／next action;
- stable references to Incident, Aggregate, Attempt, Try and domain operation identities;
- automatic retry slots already consumed and remaining eligibility;
- absolute UTC next-eligibility／wake reference;
- singleton follow-up reference and typed unresolved requirement frontier;
- publication continuation reference;
- last observed typed source disposition and bounded non-secret observation metadata.

## 5.2 Forbidden Duplication

D2 must not duplicate or decide Evidence／Materiality, Knowledge applicability, D result／safety, Try outcome, Artifact／Version／Current／Freshness, Incident RCA relationship or publication winner truth. References are permitted; copied business state is not authority.

## 5.3 Reconciliation Rule

Every execution, retry and recovery step begins with fresh authoritative public reads. Domain authority overrides stale Runtime continuity. D2 may advance or complete only after the applicable domain effects are durably coherent. Contradiction is preserved and fails closed; it is never resolved by last-write-wins.

---

# 6. Initial RCA Protocol

## 6.1 Required Ordering

```text
durable Incident discovery
→ derive stable RCA root
→ discover/create Incident-bound Aggregate
→ durably freeze complete Capture Command Basis
→ derive stable capture_operation_id
→ execute/reconcile Evidence capture
→ explicit NO_BASELINE initial path
→ resolve/finalize Knowledge Snapshot
→ admit one Attempt on exact frozen lineage
→ execute/reconcile Try
→ durably record A-side Try outcome
→ commit validated Artifact / Version / A publication receipt
→ reconcile SPEC-008 publication
→ complete A-side publication / Current
→ complete Runtime work last
```

Knowledge `NO_MATCH` is a legal Snapshot and does not block Attempt admission. `RETRIEVAL_UNAVAILABLE` may continue only through the governed upstream degraded authorization and C finalization contract. Invalid／repair-required Evidence or Knowledge authority fails closed.

## 6.2 Crash and Replay Rule

Before invoking the next stage, Candidate E reads the intended domain operation receipt／result and relevant subject authority. Existing equivalent success is reconciled rather than repeated. Absence after a reliable read permits the same semantic operation. Evidence capture is permitted only after the complete Capture Command Basis is durable and its stable operation identity has been derived; recovery reuses that basis and its original authoritative `snapshot_at`. Unreadable, conflicting or ambiguous state never becomes clean absence, and an unrecoverable frozen capture basis is `REPAIR_REQUIRED`.

## 6.3 Validated Handoff

A durable D result is not an A Try outcome or Artifact. Candidate E uses the exact D result and lineage to record the A Try outcome, then invokes A's validated Artifact commit. A response-loss replay must recover the same Try outcome, Version, Artifact and publication identity; no replacement Attempt or Version is allocated.

---

# 7. Attempt Admission and Basis Freezing

An Attempt is admitted only after one successful Evidence Snapshot／Revision and one terminal Knowledge Snapshot have been fixed. Its immutable core uses the authoritative A／B／C／D lineage required by SPEC-012 and SPEC-015.

Candidate E must not:

- admit an Attempt merely because a new Event arrived;
- change Evidence or Knowledge inside an existing Attempt;
- use B history to guess the A Current baseline;
- treat a different Revision ID as Materiality;
- reuse an old Attempt for new Material Evidence;
- select a new Profile, model, prompt or schema inside an existing Attempt.

Initial work uses B's explicit `NO_BASELINE` path. Refresh uses direct pairwise comparison from A's authoritative Current material-evidence basis to the candidate B Revision.

---

# 8. Attempt / Try / Retry Protocol — E2

## 8.1 Two Lanes, One Attempt-scoped Budget

The retry-budget semantic subject is exactly one Generation Attempt's generation continuation. Within that Attempt it has two continuation lanes:

```text
Same-Try lane  = another physical provider invocation for the same logical Try
Next-Try lane  = record current Try outcome, then admit the next logical Try ordinal
```

Both lanes consume one shared durable automatic retry budget. Under the PRD-004 PoC policy, the initial provider invocation is not an automatic retry and at most four subsequent physical provider invocations may consume retry slots. The Runtime schedule remains `1s → 2s → 4s → 8s` by current approved default unless governed configuration changes it.

Each new physical provider invocation after the initial invocation consumes exactly one slot before invocation. A lane change, Try ordinal change, D operation-ID change, restart or Runtime work reconstruction does not reset or expand the budget. Hidden provider-SDK retry must be disabled or accounted as part of the same authorized invocation contract; no second budget is legal.

```text
Incident-level stable RCA root
≠ retry budget subject

one Generation Attempt generation-continuation
= one retry budget subject
```

All Same-Try physical reinvocations and all Next Logical Tries within that Attempt share this budget. An authoritative Material Evidence Revision change that requires generation creates a new Generation Attempt and therefore a new retry-budget subject. It does not create or change the Incident-level stable RCA root. A retry budget never crosses Attempt lineage and is never regenerated for an existing Attempt.

The budget subject is durably established and bound to the Generation Attempt before its initial provider invocation. That initial invocation consumes no automatic retry slot. The subject persists across all lane transitions and restarts until the Attempt reaches a terminal generation-continuation outcome or exhaustion. A terminal or exhausted Attempt's budget cannot be reopened, transferred or reused by another Attempt.

## 8.2 Fresh Authority Reads

Before every invocation Candidate E／SPEC-011 fresh-reads at least:

- A Attempt lineage and ordered Try outcomes;
- D result and typed failure facts for the exact subject;
- D Same-Try safety;
- the current Attempt's shared Runtime budget consumption and absolute eligibility;
- any newer authoritative basis that makes the Attempt obsolete;
- applicable lifecycle and fail-closed authority.

Runtime never infers disposition from exception messages or strings.

## 8.3 Same-Try Lane

Same-Try physical reinvocation is eligible only when SPEC-015 confirms all of the following:

1. no D durable validated result exists for the exact identity／lineage;
2. no A authoritative outcome exists for that Try;
3. D explicitly classifies Same-Try physical reinvocation as safe;
4. that Generation Attempt's shared budget has an eligible slot and the Runtime Clock has reached its absolute eligibility;
5. the Attempt remains authoritative and not obsolete.

Same-Try safety is a D-owned safety fact, not a fourth retry disposition and not execution authorization.

## 8.4 Next-Try Lane

If the failure is `RETRYABLE` but Same-Try is unavailable or unsafe:

```text
fresh-read D failure
→ durably record Try N outcome in A
→ verify A outcome by authoritative read
→ consume/verify one shared retry slot for the next physical invocation
→ authorize the next valid A Try ordinal
→ invoke D for Try N+1 when eligible
```

A having any authoritative outcome for Try N permanently prohibits returning to Same-Try for N. Candidate E must not skip the A failure-recording boundary, invent a Try ordinal or let D allocate the next Try.

## 8.5 Result and Failure Outcomes

- Durable D validated result: hand off to A; do not consume another retry slot.
- `RETRYABLE`: use Same-Try when all guards hold; otherwise record A outcome then transition to Next-Try if budget remains.
- `NON_RETRYABLE`: durably reconcile required A outcome, then stop automatic retry.
- `REPAIR_REQUIRED`: fail closed; do not invoke automatically or transition lanes.
- Attempt obsolete by authoritative newer basis: stop provider continuation and preserve facts; Candidate E follows refresh/coalescing protocol rather than treating obsolescence as provider failure.

## 8.6 Exhaustion and Restart

When no slot remains in the current Generation Attempt's shared budget, Runtime records operator-visible exhaustion without changing the domain failure class or disposition. No automatic physical provider invocation and no Next-Try transition follows. Restart restores that Attempt's consumed slots and absolute eligibility; if used count or its Attempt binding cannot be reliably recovered, the work fails closed rather than resetting to zero. A later Material-Evidence-authorized new Attempt receives its own new budget subject without changing the Incident RCA root.

---

# 9. Follow-up / Material Refresh / Post-context Protocol — E3

## 9.1 Singleton Follow-up Root

Per Incident there may be at most one outstanding follow-up root beneath the stable RCA root. Material Evidence signals, post-context requirements and STALE-related requirements merge into that root. They do not create parallel follow-up roots.

## 9.2 Typed Unresolved Requirement Frontier — E3-C1

The existing follow-up retains a durable typed unresolved requirement frontier sufficient to rediscover every successfully admitted requirement and its relevant authoritative Incident／B/A facts. The frontier may simultaneously contain:

```text
one or more Evidence Revision requirements
post-context boundary requirement
STALE / refresh requirement
```

D2 may store the typed frontier, references and wake continuity, but it does not decide Materiality, Snapshot, Current or STALE truth. A newly admitted requirement coalesces into the same root without overwriting unresolved requirements. Revision ID, timestamp, arrival order or a scalar "latest" value must not discard an existing requirement.

A requirement may be removed from the frontier only when authoritative A／B facts prove it is covered or resolved under its type-specific contract. Evidence Revision requirements still require Candidate B's explicit direct pairwise Materiality judgement against the A-authoritative baseline; no frontier ordering or transitive inference substitutes for that judgement. Requirements that are not authoritatively comparable remain together in the frontier or fail closed if their coexistence is contradictory.

## 9.3 Post-context Wake Continuity

B supplies `episode_end`, the post-context boundary and window-completion facts. SPEC-011 Runtime Clock owns the absolute wake and scheduling. Restart preserves the original authoritative boundary／wake reference; it does not delay initial RCA, reset the wake or silently discard a due follow-up.

## 9.4 Refresh Admission Ordering

```text
capture new Evidence
→ B commits Snapshot / Revision
→ read A authoritative Current baseline
→ B evaluates direct pairwise Materiality
→ SAME / NON_MATERIAL
   → no refreshed Attempt
   → record/reconcile requirement as processed

→ MATERIAL
   → apply authorized STALE fact to A Current
   → verify Current remains readable and STALE
   → freeze required Evidence and Knowledge basis
   → admit a new Attempt
```

New Event arrival alone never admits a new Attempt. A Current remains last-known-good while refresh is pending, executing or failed. Candidate E does not redefine B's Materiality or A's STALE semantics.

## 9.5 Follow-up Completion Recheck

Before completing the singleton follow-up, Candidate E fresh-reads the complete typed unresolved requirement frontier and all authoritative basis facts. Each successfully admitted requirement must be proven covered or resolved without loss. If any frontier member remains unresolved, the same follow-up root continues.

An Evidence Revision requirement is covered only by its required explicit A-baseline／B-candidate judgement and resulting terminal `SAME／NON_MATERIAL` path or admitted／completed `MATERIAL` refresh path. Post-context and STALE／refresh requirements use their own authoritative coverage facts. A lawful lifecycle suppression or operator-visible terminal fail-closed outcome may resolve only the specific requirements to which it applies. Completion followed by creation of a parallel root for an already-admitted requirement is forbidden.

---

# 10. Publication Protocol

## 10.1 Publication Ordering

```text
A durably records validated Try outcome
→ A atomically commits Artifact + Version + publication target/receipt
→ Candidate E reads the exact publication intent
→ SPEC-008 authorized publication mutation with same publication identity
→ Candidate E reads typed SPEC-008 receipt/result and Current relationship
→ A records authorized publication disposition and updates A Current when legal
→ Candidate E verifies A / SPEC-008 coherence
→ Runtime completion last
```

Artifact durability is not publication completion. Publication intent contains the authoritative Incident, target Version, publication operation and expected Current precondition defined by A／SPEC-008 contracts. Candidate E does not rewrite it.

## 10.2 No Cross-store Transaction

A and SPEC-008 each commit only their local authoritative effects. Candidate E coordinates through public receipts and reads. Shared database authority, cross-store foreign keys, cross-store ACID and 2PC are forbidden.

## 10.3 Publication Result

Only an authorized SPEC-008 result proving that the target is Incident Current, together with coherent A completion, permits publication success and A Current promotion. A stale, superseded or conflicting result must not silently promote the target.

---

# 11. Publication Reconciliation / Recovery — E4

## 11.1 Receipt-first Classification Order

For each RCA publication subject, startup and recovery classify in this exact authority order:

```text
1. SPEC-008 same-operation publication receipt
2. SPEC-008 current RCA relationship
3. Candidate-A publication receipt/result
4. Candidate-A Current / Version history
5. Runtime D2 continuation
```

Publication reconciliation precedes renewed generation or refresh execution for the same subject. A SPEC-008 `APPLIED` receipt prohibits repeating the business mutation.

## 11.2 Required Classifications

| Classification | Authoritative evidence | Safe action | Completion / fail-closed condition |
|---|---|---|---|
| `APPLIED` | Same-operation SPEC-008 receipt proves target applied; relationship agrees | Do not re-mutate; complete or replay A-side publication | Complete only after A Current／result and SPEC-008 relationship are coherent |
| `PRECONDITION_SUPERSEDED / OBSOLETE` | Typed SPEC-008 receipt or coherent Current/history proves expected Current is no longer valid and target did not win | Record/reconcile typed obsolete disposition in A; preserve Artifact/history | Complete as obsolete only from authoritative facts; never guess winner |
| Target already current, equivalent | Incident relationship already references exact target and identities／history prove equivalence | Reconcile receipts and A completion without second mutation | Coherent equivalent effect required |
| `TARGET conflict` | Same operation targets different semantics, or target/current facts contradict authorized history | Preserve evidence; `REPAIR_REQUIRED` | No automatic mutation, promotion or replacement Artifact |
| Response lost | A intent exists; SPEC-008 receipt or relationship may contain completed effect | Receipt-first lookup, then same-ID public replay only if absence is reliable | Same outcome recovered or fail closed |
| A committed, SPEC-008 incomplete | A Version／Artifact／receipt exists; no Incident receipt/effect after reliable reads | Invoke same publication identity through SPEC-008 | Receipt plus coherent relationship, then A completion |
| SPEC-008 applied, A incomplete | Incident receipt／relationship proves applied; A receipt/Current not completed | Do not repeat SPEC-008 mutation; pass authoritative result to A | A and Incident state coherent |
| Runtime bookkeeping lost | A and SPEC-008 already coherent; D2 incomplete／missing | Reconstruct same work root and mark Runtime complete | Domain coherence verified first |
| Incoherent authority | Facts cannot form one valid history | `REPAIR_REQUIRED` | Fail closed; governed repair only |

Candidate E must not select a publication winner by timestamp, lexical Version ID, operation ID, highest-looking number or last-write-wins. Supersession and conflict are classified only through SPEC-008 and A authoritative Current／history facts.

## 11.3 Runtime Completion Last

Runtime work becomes terminal only after the authoritative publication outcome is known, A and SPEC-008 are coherent for that outcome, any required A-side completion is durable, and the typed unresolved requirement frontier is empty by authoritative coverage／resolution proof. Runtime bookkeeping can never make an incomplete publication complete.

---

# 12. Startup Recovery within SPEC-011

## 12.1 Barrier Integration

RCA recovery is a classification phase inside the existing SPEC-011 Startup Recovery Barrier. It is not a separate startup loop. Normal RCA execution for an affected subject may begin only after its required authorities are readable and the subject is safely classified. Publication reconciliation is classified before generation, refresh or retry continuation.

## 12.2 Subject Classification Matrix

| Subject class | Authoritative evidence | Safe replay / reconcile action | Completion condition | Fail-closed condition |
|---|---|---|---|---|
| Publication reconciliation first | SPEC-008 receipt／relationship, then A receipt／Current／history | Apply section 11 before other work | Publication terminal classification coherent | Any contradictory target／winner evidence |
| Validated D result not yet in A | D exact result＋lineage; A Try outcome absent | Record same A Try outcome, then same Artifact handoff | A Try／Artifact facts resolve exact D result | D/A lineage mismatch or contradictory A outcome |
| Retryable Try failure not yet recorded | D typed `RETRYABLE` failure; A outcome absent; Attempt-scoped budget／lane facts | Same-Try only if all D guards hold; otherwise record A failure, then Next-Try only if that Attempt's budget has a slot | Safe invocation scheduled or A outcome durably reconciled | Missing safety, identity, Attempt binding or budget evidence |
| Non-retryable Try failure | D typed `NON_RETRYABLE` failure＋A Try facts | Reconcile the necessary A-side terminal Try outcome, then stop | A terminal outcome is durable and no automatic continuation remains | Any contradictory D/A lineage or attempted invocation／Next-Try |
| Repair-required Try failure | D typed `REPAIR_REQUIRED` failure or authority contradiction | Fail closed; preserve facts; no Same-Try or Next-Try | Operator-visible governed repair state | Any automatic invocation or lane transition |
| Retry budget exhausted | Attempt-bound consumed-slot record＋typed source failure＋A/D facts | Reconcile necessary A-side outcome; schedule no invocation and authorize no Next-Try | Exhaustion and terminal domain facts are durable／operator-visible | Missing or contradictory Attempt-budget binding |
| Artifact committed, publication incomplete | A Version／Artifact／publication receipt; SPEC-008 receipt／relationship | Same publication identity reconciliation | A／008 coherent | Target/precondition contradiction |
| Retry pending | Typed `RETRYABLE` disposition＋Attempt-bound D2 consumption＋absolute eligibility＋domain receipts | Restore the same Attempt budget, lane, identity and schedule | Existing effect reconciled or eligible retry safely scheduled | Used budget, Attempt binding or stage unrecoverable |
| Follow-up pending | Singleton root＋typed unresolved requirement frontier＋A/B facts | Restore same root and process every unresolved typed requirement | Entire frontier authoritatively covered／resolved and rechecked | Lost requirement, multiple incompatible roots or contradictory frontier |
| Post-context pending | B boundary facts＋frontier member＋D2 absolute wake continuity | Restore wake under Runtime Clock; capture at due time | Post-context requirement authoritatively covered／resolved | Boundary/wake identity cannot be reconstructed |
| STALE Current | A Current／Freshness basis＋B Materiality facts | Ensure one follow-up root; capture／compare before Attempt admission | NON_MATERIAL covered or MATERIAL refresh admitted／terminally classified | STALE without trustworthy basis or contradictory Current |
| Initial Incident with no RCA work | SPEC-008 durable Incident and relationship; A Aggregate absence from reliable read | Derive stable root and discover/create Aggregate | Initial protocol durably continued | Incident／Aggregate binding contradiction |
| Contradictory authority | Any required store cannot provide coherent complete facts | Preserve evidence and classify `REPAIR_REQUIRED` | Governed repair outside this SPEC | Automatic repair, guessing or partial authority use |

Recovery applies exactly the same retry disposition rules as normal execution. Only typed `RETRYABLE` may enter Same-Try or Next-Try consideration. `NON_RETRYABLE` reconciles the necessary A-side terminal outcome and stops. `REPAIR_REQUIRED` fails closed. Exhausted Attempt budget permits neither a new physical invocation nor a Next-Try transition.

## 12.3 Readiness Semantics

SPEC-016 does not redefine whole-platform READY. RCA capability supplies classification/readability conditions to SPEC-011:

- required A／B／C／D／SPEC-008 public reads and recovery enumerations must be reliable for each affected subject;
- an isolated RCA `NON_RETRYABLE` or `REPAIR_REQUIRED` subject may remain operator-visible without redefining platform-wide readiness, as SPEC-011 permits;
- store-wide inability to enumerate or distinguish absence from corruption prevents safe RCA classification and the affected capability fails closed;
- Knowledge or provider dynamic unavailability follows its typed degraded／invocation semantics and does not by itself redefine whole-platform READY.

---

# 13. Crash Consistency Matrix

| Crash window | Durable recovery evidence | Normative recovery |
|---|---|---|
| Incident obligation discovery → work durability | SPEC-008 Incident＋A Aggregate read | Re-derive same root and reconstruct the same initial continuity; never create another root |
| Work → Evidence | durable frozen Capture Command Basis＋derived capture identity＋B outcome／absence | Read B outcome; invoke/replay only with the same complete basis and original `snapshot_at`; unrecoverable basis fails closed |
| Evidence → Knowledge | B Snapshot／Revision＋C operation read | Resume/finalize same retrieval operation and frozen boundary |
| Knowledge → D execution | C Snapshot＋A Attempt lineage＋D subject read | Admit/reuse same Attempt and D execution identity; no new basis |
| Provider invocation → D durable result/failure | D subject/result/failure facts＋A outcome＋Attempt-bound budget | Only `RETRYABLE` may apply Same-Try／Next-Try guards; NON_RETRYABLE stops, REPAIR_REQUIRED fails closed, and exhaustion permits no invocation／Next-Try |
| D result → A Try outcome | D result＋A Try read | Record/replay the exact A outcome; contradiction fails closed |
| Try outcome → Artifact commit | A Try outcome＋D result＋A publication recovery facts | Commit/replay same Artifact command; recover same Version/publication identity |
| Artifact → SPEC-008 publication | A Version／Artifact／receipt＋SPEC-008 receipt／relationship | Receipt-first publication reconciliation |
| SPEC-008 publication → A completion | SPEC-008 receipt／relationship＋A publication record | Never repeat applied mutation; complete A with authoritative result |
| A／008 coherent → Runtime completion | Both domain authorities coherent＋D2 outstanding | Complete D2 last; missing bookkeeping may be reconstructed |
| Post-context scheduled → restart | B boundary facts＋D2 absolute wake reference | Restore same wake; do not reset or lose follow-up |
| Follow-up coalesced → restart | singleton root＋typed unresolved requirement frontier＋A/B facts | Resume the same root and every unresolved frontier member; no lost requirement or parallel root |

No crash window may be repaired by replacement Attempt／Version／Artifact, budget reset, private DB access or destructive cleanup.

---

# 14. Concurrency, Idempotency and Replay

## 14.1 Duplicate Initial Discovery

Concurrent discovery for the same Incident derives the same RCA root and Aggregate operation. A cardinality and replay guards converge to one Aggregate. A conflicting binding is `REPAIR_REQUIRED`, not a second Aggregate.

## 14.2 Concurrent Follow-up Coalescing

Concurrent Material Evidence, post-context or STALE requirements merge into one singleton follow-up's typed unresolved requirement frontier. The durable result must retain every successfully admitted unresolved requirement until authoritative A／B coverage facts resolve that specific member. No timestamp, Revision ID or arrival-order overwrite is permitted. Completion uses a final full-frontier fresh-read/recheck; parallel roots and lost requirements are forbidden.

## 14.3 Same Try Concurrent Execution

Only execution holding valid SPEC-011 work authority and satisfying fresh A/D expected-state guards may invoke. D and A subject-level uniqueness ensure at most one authoritative result／outcome. A loser reconciles the winner or fails closed; it does not consume an extra invocation silently.

## 14.4 Publication Replay and Race

Equivalent same-publication replay converges through receipts. Concurrent different publication targets are decided only by SPEC-008 mutation preconditions and A／SPEC-008 Current history. A typed superseded result is preserved. A target conflict or contradictory same-operation identity fails closed. Runtime never performs last-write-wins.

## 14.5 Restart versus Active Worker

The SPEC-011 Startup Recovery Barrier prevents normal RCA dispatch before recovery classification. Existing fencing／worker lifecycle mechanisms must prevent a stale executor from mutating after authority loss. Controlled drain leaves a durable safe boundary; it does not clear work or authority.

## 14.6 Runtime Completion Race

Concurrent completers may mark one Runtime work terminal only after identical fresh-read domain coherence. Any member remaining in the typed unresolved requirement frontier, unresolved publication or changed authoritative state prevents completion. Runtime terminalization is idempotent bookkeeping, never business truth.

---

# 15. Failure Model

SPEC-016 preserves source-domain failure semantics and adds no competing business taxonomy.

| Operational category | Meaning | Candidate-E behavior |
|---|---|---|
| Retryable execution failure | Owning domain returns typed `RETRYABLE` | Continue only under section 8's Generation-Attempt-bound shared budget, lane guards and SPEC-011 scheduling |
| Non-retryable domain failure | Owning domain returns `NON_RETRYABLE` | Reconcile required durable domain outcome; stop automatic retry |
| Repair-required contradiction | Authority, identity, receipt, lineage or integrity cannot be coherently classified | Fail closed; preserve evidence; no automatic retry or winner guess |
| Authority／readiness failure | Required public authority cannot reliably read／enumerate／differentiate absence | Stop affected execution; capability/barrier handling follows section 12.3 |
| Publication superseded／obsolete | Authoritative receipt／Current history proves the target did not win or precondition is obsolete | Record/reconcile typed terminal obsolete outcome; do not promote or retry as transient failure |
| Retry exhausted | Shared Runtime budget has no slot | Stop automatic invocation; preserve original source disposition and exhaustion evidence |

Candidate E must not convert provider, grounding, Materiality, retrieval, publication or integrity messages into a new disposition. Existing A／B／C／D／SPEC-008 typed outcomes remain authoritative.

---

# 16. Readiness, Recovery and Fail-Closed Rules

Candidate E requires public, integrity-aware reads for every authority it uses. `NOT_FOUND` is legal only when the owning domain reliably proves absence. Unreadable, partial, malformed, unsupported or contradictory state is never treated as absence.

Fail-closed conditions include at least:

- more than one stable RCA root or Aggregate binding for one Incident;
- irreconstructible correctness-critical operation identity;
- irrecoverable retry consumption or lane state;
- A Try outcome contradicting D result／failure lineage;
- parallel or contradictory singleton follow-up roots;
- MATERIAL refresh without authorized A STALE transition;
- publication receipt, target, relationship, A Current or history contradiction;
- Runtime completion while A／SPEC-008 publication is incoherent;
- incomplete recovery enumeration from a required authority.

Normal Runtime must not delete, reset, truncate, choose a winner or rewrite authority to recover. Governed repair is outside this specification.

---

# 17. Security and Credential Boundary

Credential/provider secrets, authorization headers and secret-bearing exception content must not enter:

- Runtime Work or follow-up basis metadata;
- telemetry, logs or metrics;
- Artifact or Candidate-E orchestration records;
- publication, capture, retrieval or operation receipts;
- failure payloads or stable identities.

Only approved non-secret Profile／capability identities and bounded sanitized facts may be referenced. Candidate E does not select fallback credentials, provider or model. Outbound safety, redaction and provider-secret handling remain SPEC-013～015 authority. Ground Truth, scenario identity, expected answers and Candidate-F evaluation identity must not influence production orchestration or identities.

---

# 18. Telemetry

Telemetry is observation only. It must not become Evidence, Materiality, Knowledge, Try, publication, Incident or Runtime-work authority.

Implementations must make the following semantic observations possible, using bounded non-secret references where applicable:

- RCA obligation root and obligation kind;
- orchestration stage and next action;
- Aggregate／Attempt／Try references;
- retry lane, slots consumed, slots remaining and absolute eligibility;
- follow-up coalescing and typed unresolved requirement-frontier membership／coverage;
- post-context wake restoration;
- publication reconciliation classification;
- startup recovery classification;
- source failure class and typed disposition;
- superseded／obsolete, conflict, exhaustion and repair-required outcomes;
- Runtime completion after domain coherence.

Exact metric, event and field names are implementation-deferred.

---

# 19. Acceptance Criteria

## AC-016-A — Stable Incident-root Identity

The same authoritative Incident resolves one stable RCA obligation root across initial work, retry, refresh, reconciliation and restart; a second root is rejected.

## AC-016-B — Deterministic Replay Identity

Every correctness-critical caller-generated domain operation identity is reconstructible from stable authoritative facts or retained by an authoritative receipt. Before first Candidate-B effect, the complete Capture Command Basis is durable, the capture identity is derived from it, and restart reuses its original authoritative `snapshot_at`. An unrecoverable basis fails closed; newly observed wall-clock time, PID, worker and restart randomness cannot alter or replace it.

## AC-016-C — No Second Runtime

RCA work executes through SPEC-011 D2, Runtime Clock, retry scheduling, Startup Recovery Barrier, worker lifecycle and drain semantics. No second scheduler, worker loop, retry database or recovery framework exists.

## AC-016-D — Initial RCA Restart Recovery

A durable Incident with incomplete initial RCA is rediscovered and resumes the same root, Aggregate and semantic operations without duplicate Attempt, Version or publication.

## AC-016-E — Same-Try Retry

A retryable ambiguous invocation may physically reinvoke the same Try only when no D result, no A outcome and explicit D Same-Try safety are freshly proven, and one shared slot is consumed.

## AC-016-F — Next-Try Retry

When Same-Try is unavailable but failure is retryable, Try N failure is first durably recorded in A, then only the next valid Try ordinal is authorized under the same Generation Attempt's shared budget.

## AC-016-G — Shared Budget Survives Restart

Within one Generation Attempt, Same-Try and all Next-Try invocations share one durable automatic retry budget. The Incident RCA root is not the budget subject. Restart, lane, ordinal or D operation-ID changes do not reset consumption or eligibility; a Material-Evidence-authorized new Attempt creates a new budget subject without changing the Incident root.

## AC-016-H — Retry Exhaustion

After the PRD-004-authorized retry limit is consumed, no automatic physical invocation occurs; source disposition and operator-visible exhaustion remain preserved.

## AC-016-I — Singleton Follow-up Root

Per Incident, concurrent Material Evidence, post-context and STALE requirements produce at most one outstanding follow-up root.

## AC-016-J — Typed Requirement-frontier Coalescing

Coalescing preserves a typed unresolved requirement frontier that may simultaneously hold Evidence Revision, post-context boundary and STALE／refresh requirements. No member is overwritten by Revision ID, timestamp or arrival order; each is removed only after authoritative A／B facts prove coverage／resolution, and completion proves no successfully admitted unresolved requirement was lost.

## AC-016-K — NON_MATERIAL Suppresses Attempt

B `SAME／NON_MATERIAL` for the explicit A baseline produces no refreshed Attempt or Version.

## AC-016-L — MATERIAL → STALE → Refreshed Attempt

B `MATERIAL` is followed by authorized A Current STALE persistence before freezing new Evidence／Knowledge lineage and admitting a refreshed Attempt.

## AC-016-M — Post-context Wake Continuity

The B-owned post-context boundary and SPEC-011-owned absolute wake survive restart without delaying initial RCA, resetting time or duplicating a follow-up root.

## AC-016-N — Artifact / Publication Ordering

A Try outcome and immutable Artifact／Version／publication receipt are durable before SPEC-008 mutation; Artifact durability alone never marks publication or Runtime complete.

## AC-016-O — Receipt-first Recovery

Recovery classifies each publication subject in the E4 order and never repeats a SPEC-008 business mutation after an `APPLIED` same-operation receipt.

## AC-016-P — Superseded Publication

Typed precondition-superseded／obsolete results are reconciled from authoritative receipt and Current／history facts, do not promote the target and do not trigger winner guessing.

## AC-016-Q — Target Conflict

Contradictory same-operation target or incoherent target/current authority produces `REPAIR_REQUIRED`; no timestamp, lexical ID or last-write-wins resolution is used.

## AC-016-R — Runtime Completion Last

Runtime work completes only after A and SPEC-008 publication facts are coherent and every member of the typed unresolved follow-up requirement frontier is authoritatively covered or resolved.

## AC-016-S — Publication Response Lost

If SPEC-008 commits but the response is lost, recovery uses the same-operation receipt／relationship, does not repeat an applied mutation and completes A with the authoritative result.

## AC-016-T — D Result → A Recovery

A durable D validated result with no A Try outcome resumes by recording the exact A outcome and committing the same Artifact lineage; it does not regenerate or allocate a replacement Attempt.

## AC-016-U — Crash / Restart Matrix

Every section 13 interruption window has tests proving recovery from durable authority without process memory, identity replacement, budget reset or duplicate business effect.

## AC-016-V — Concurrent Replay / Idempotency

Duplicate initial discovery, same-Try execution, follow-up updates, publication replay, publication races and Runtime completion races converge or fail closed without lost updates or duplicate semantic effects.

## AC-016-W — No Private DB Access

Candidate E uses only supported public semantic capabilities and never reads or writes private A／B／C／D／SPEC-008 tables or vector-store internals.

## AC-016-X — No 2PC

All domain effects retain domain-local atomicity; the protocol uses stable identities, receipts and reconciliation rather than shared business storage, cross-store transactions or 2PC.

## AC-016-Y — Fail Closed on Contradiction

Irreconstructible identity／budget, incomplete authority enumeration, conflicting lineage, parallel follow-up roots or incoherent publication facts remain operator-visible `REPAIR_REQUIRED` and are never guessed or destructively repaired.

## AC-016-Z — Status Honesty

Draft or future Candidate-E implementation evidence does not imply Candidate F, final RCA／RAG E2E, AC-014-X, Live Gemini or Production Ready. Live Gemini remains `NOT EXECUTED`; Production Ready remains `NO`.

---

# 20. Verification Strategy

Historical-phase qualifier: the following strategy and statement that no tests were executed describe the v1.0 approval patch. The v1.1 documentation-only closure evidence is recorded in section 23; the approved verification semantics below remain unchanged.

Future implementation must provide at least:

1. unit tests for identity composition, state classification, lane selection, coalescing and completion guards;
2. contract tests against public SPEC-008 and Candidate A～D semantic capabilities;
3. cross-SPEC integration tests for initial, refresh, generation, Artifact and publication ordering;
4. durable-store reopen／process-restart continuity tests for Runtime continuity and all domain receipts; current repository SQLite adapters may provide implementation evidence, but SQLite is not a SPEC-016 normative persistence requirement;
5. crash injection for every section 13 window and repeated startup interruption;
6. concurrency tests for duplicate discovery, Same-Try execution, follow-up coalescing, publication races and completion races;
7. replay tests for every caller-generated identity and response-loss path;
8. publication reconciliation tests for APPLIED, equivalent current, superseded／obsolete, target conflict, partial A／008 completion and Runtime-bookkeeping loss;
9. SPEC-011 Runtime integration tests for the existing D2, Clock, Startup Barrier, retry, drain and worker lifecycle;
10. full repository regression.

Tests must use deterministic fake adapters where appropriate. This approval patch executes no tests and claims no test exists or passes. Live provider verification, Candidate F evaluation and final RCA／RAG E2E remain separate gates.

---

# 21. Historical v1.0 Implementation Handoff Boundary

This section preserves the implementation handoff boundary as approved in v1.0. Its future-tense wording is historical and does not describe the current implementation status.

Future implementation planning may define semantic implementation areas for:

- integration with the existing SPEC-011 Runtime work abstraction and recovery barrier;
- Candidate-E orchestration service／ports using public A～D and SPEC-008 capabilities;
- deterministic RCA root and child-operation identity composition;
- retry-lane and shared-budget continuation;
- singleton follow-up coalescing and wake restoration;
- publication reconciliation classifier and Runtime completion guard;
- bounded semantic telemetry;
- verification fixtures and fault injection.

This approved Engineering Contract does not freeze exact files, package layout, classes, tables, columns, physical schema, lock implementation, DTO spelling, metric names, identity encoding, database filenames, migration tooling or implementation commits. Any handoff must first re-verify repository surfaces and preserve this contract.

---

# 22. Historical v1.0 Approval Gate

The following statement is the preserved v1.0 approval-phase truth and is not a current-state claim:

SPEC-016 v1.0 is an approved Candidate-E Engineering Contract with implementation pending. Candidate E remains `NOT Implemented`. No production code, tests, configuration or existing authority document is changed by this approval patch.

---

# 23. Post-Implementation Documentation-only Closure

SPEC-016 v1.0 remains the Approved normative baseline. This v1.1 closure records current implementation evidence only and does not modify Frozen E1～E4, E1-C1／E2-C1／E3-C1, Approved AC-016-A～Z or any upstream authority.

```text
Accepted implementation commit: eff7eb067cc09974c7679050f5136a7cfdffadaf
Git sequence deviation: PM-AUTHORIZED WIP HANDOFF CHECKPOINT
Post-Handoff Final Full Contract Re-audit: PASS
SPEC Lead Final Review: PASS
E1 / E1-C1: PASS / preserved
E2 / E2-C1: PASS / preserved
E3 / E3-C1: PASS / preserved
E4: PASS / preserved
AC-016-A～Z: PASS
Full regression: 2064 passed, 4 skipped
Docker Candidate-E execution: NOT EXECUTED
Live Gemini: NOT EXECUTED
Candidate F: Pending
Final real-LLM/RAG quality evaluation: Pending
Production Ready: NO
Remaining implementation blocker: NONE
```

The PM-authorized WIP handoff checkpoint was not an early Implemented declaration, Final Audit bypass or semantic waiver. The complete committed tree was subsequently re-audited and accepted. SPEC-011 remains the singular Runtime authority; Candidate E executes within its existing worker, D2, Runtime Clock, retry, Startup Recovery Barrier and controlled-drain framework. Candidates A～D and SPEC-008 retain their respective domain authority.
