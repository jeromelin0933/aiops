# SPEC-013 — Incident Evidence Collection & Snapshot

## Engineering Specification v1.0

---

## 文件資訊

| 欄位 | 內容 |
|---|---|
| Document ID | SPEC-013 |
| Document Name | Incident Evidence Collection & Snapshot |
| Version | 1.0 |
| Status | Approved — Implementation Pending |
| Date | 2026-09-21 |
| Requirement Authority | PRD-004 v1.0 Approved |
| Related Product Authorities | PRD-001 v3.5；PRD-003 v1.1 Final |
| Related Incident Contract | SPEC-008 v1.2 |
| Related Runtime Contract | SPEC-011 v1.1 |
| Related Candidate-A Contract | SPEC-012 v1.0 Approved |
| Implementation Owner | 富裕 |

## 0.1 Status Honesty

本文件是 Candidate B 的 v1.0 Approved Engineering Contract，狀態為 Implementation Pending；此核准不構成 Implemented 聲明。

Repository baseline 尚未包含 Candidate-B production module、Evidence Store、Loki evidence client、Prometheus evidence client、Materiality implementation、Candidate A/B integration 或 Candidate-B tests。本核准狀態亦不表示 SPEC-008 RCA integration、SPEC-011 RCA Runtime integration、RCA E2E 或 Production Ready 已完成。

## 0.2 Authority Order

衝突判斷順序：

1. Approved PRD；
2. approved active SPEC；
3. PRD-004 Frozen cross-domain decisions；
4. SPEC-013 D1～D5 Frozen Engineering Decisions；
5. current repository reality；
6. DDS、Runtime docs、README。

Repository reality只提供feasibility與compatibility evidence，不得回寫Approved authority。若本文件與active upstream authority不能同時滿足，受影響範圍必須停止並交PM處理，不得由implementation改寫contract。

## 0.3 Core Responsibility

> **Candidate B captures trustworthy, bounded and immutable Incident evidence, preserves its semantic Revision, and judges pairwise Materiality. It does not generate RCA or schedule work.**

---

# 1. Purpose and Scope

SPEC-013定義PRD-004 Candidate B的工程契約，使caller可使用explicit Incident、stable capture operation及authoritative absolute time，建立可重播、可恢復、具完整provenance的Evidence Snapshot與Incident-scoped semantic Evidence Revision。

本SPEC涵蓋：

- authoritative Incident/Event trusted-core stabilization；
- bounded Loki／Prometheus evidence collection；
- source status與successful Snapshot completeness；
- normalized evidence與omission provenance；
- immutable Snapshot與semantic Revision；
- pairwise Materiality judgement；
- Candidate-B local persistence、replay、integrity、readiness及recovery facts；
- Candidate A／E／SPEC-011 semantic handoff；
- Ground Truth、secret及unsafe evidence isolation。

本SPEC不建立RCA generation、RAG、publication或Runtime scheduler。

---

# 2. Authority and Ownership

## 2.1 Candidate B Owns

Candidate B是下列truth的唯一authority：

- capture terminal outcome；
- immutable Evidence Snapshot；
- Incident-scoped semantic Evidence Revision；
- Snapshot → Revision binding；
- per-source status及successful Snapshot completeness；
- query、selector及collection provenance；
- sampling、aggregation、deduplication、truncation及omission facts；
- pairwise Materiality judgement；
- episode、collection window及post-context collection-boundary facts；
- Candidate-B Store local integrity、readiness及recovery facts。

## 2.2 Candidate B Does Not Own

Candidate B不得擁有或duplicate：

- Candidate-A Logical RCA Aggregate、Attempt、Try、Version、Artifact、Current、freshness或publication truth；
- Candidate-C Knowledge Snapshot、retrieval、RAG applicability或Knowledge Gap truth；
- Candidate-D LLM generation、validation、grounding或provider retry-safety truth；
- SPEC-008 Incident lifecycle、Incident RCA relationship或Current projection；
- SPEC-011 Runtime scheduling、retry timing／budget、absolute clock、wake-up、startup recovery execution或whole-platform READY；
- Candidate-E cross-domain protocol composition；
- EventStore或Incident Store authority。

Candidate B不得保存Knowledge Snapshot、LLM request/result、RCA Artifact或Runtime work ledger作為Evidence authority。

## 2.3 Upstream Read Boundaries

- SPEC-008提供authoritative, point-in-time coherent Incident public read。Candidate B不得讀取Incident private tables。
- EventStore authoritative enumeration只使用`EventStore.read_all_authoritative()`或future fully equivalent approved public semantic capability。Candidate B不得自行parse EventStore physical JSONL。
- SPEC-011 caller提供timezone-aware authoritative absolute `snapshot_at`。Candidate B不得hidden wall-clock call替代。
- SPEC-012 Candidate A提供authoritative Current Material Evidence baseline lineage；Candidate B不得從自身history選baseline。

## 2.4 Cross-domain Allocation

```text
Candidate B → evidence IDs, status, completeness, provenance,
              boundary facts, Materiality and recovery facts
Candidate A → Current baseline lineage and consumption of B references
Candidate E → initial/refresh/post-context/publication protocol composition
SPEC-011   → WHEN, ORDER, retry timing, wake-up and startup recovery
SPEC-008   → Incident truth and Incident-side RCA relationship
```

---

# 3. D1～D5 Frozen Engineering Decisions

| Decision | Frozen Contract |
|---|---|
| D1 | 使用public-read stabilization protocol建立trusted Incident/Event capture boundary；任何trusted-core contradiction Fail Closed，不得轉成DEGRADED。 |
| D2 | One stable `capture_operation_id` → at most one durable terminal outcome；equivalent replay回same outcome，contradictory replay Fail Closed。 |
| D3 | Candidate B計算episode/windows並保存closed source status、completeness及bounded omission provenance；caller提供authoritative `snapshot_at`。 |
| D4 | Candidate A提供explicit baseline，Candidate E compose，Candidate B只做explicit pairwise、non-transitive Materiality judgement。 |
| D5 | 使用independent configurable-path Evidence Store保存B-owned local truth、atomic success、schema/integrity及recovery facts；不取得其他domain authority。 |

D1～D5是本Draft的semantic basis。本文件的DTO、failure vocabulary、canonicalization metadata與acceptance criteria只formalize這些決策，不修改其authority allocation。

---

# 4. Domain Model and Identity

## 4.1 Closed Sets

```text
CaptureTerminalKind = SUCCESS | FAILURE

EvidenceSource = LOKI | PROMETHEUS

SourceStatus =
  AVAILABLE | EMPTY | UNAVAILABLE | INVALID

EvidenceCompleteness =
  FULL | DEGRADED

MaterialityJudgement =
  SAME | NON_MATERIAL | MATERIAL | REPAIR_REQUIRED

MaterialityEvaluationKind =
  PAIRWISE | NO_BASELINE

RetryDisposition =
  RETRYABLE | NON_RETRYABLE | REPAIR_REQUIRED
```

Exact enum representation與type names可依implementation language調整，但closed semantic sets不可被catch-all或message parsing取代。

## 4.2 Identities

### `capture_operation_id`

Stable identity for one requested capture operation。它是terminal outcome replay identity，不是Snapshot、Revision、Runtime work或retry schedule identity。

### `snapshot_id`

Immutable operational capture identity。每個successful capture operation exactly one Snapshot；不同capture operations不得共用同一Snapshot identity。

### `revision_id`

Incident-scoped semantic evidence identity。它表示canonical semantic evidence，而不是collection invocation。不同Snapshots可合法resolve到同一Revision：

```text
Snapshot S1 → Revision R1
Snapshot S2 → Revision R1
```

`snapshot_id`與`revision_id`必須是不同identity domains，即使physical encoding偶然相似亦不得互換。

### Materiality identity

Materiality evaluation/result必須可由explicit baseline Revision、candidate Revision及`materiality_rule_version`唯一辨識其semantic request。Exact ID encoding deferred，但same identity不得resolve到contradictory pair或rule version。

## 4.3 Incident Scope

每個Evidence Snapshot及Evidence Revision exactly one `incident_id`。跨Incident Revision comparison非法；Revision不得因相同payload而跨Incident共用identity。

---

# 5. Public Semantic Capabilities

下列為logical ports；exact Python class、module、HTTP route、RPC transport或method spelling不在本SPEC凍結。

## 5.1 Capture Evidence

```text
capture_evidence(CaptureCommand)
  → CaptureTerminalOutcome
  | NonTerminalInvocationFailure
```

`CaptureCommand`至少包含：

```text
capture_operation_id
incident_id
snapshot_at
capture_contract_version
canonicalization_version
source_policy_version
bounds_policy_version / config_identity
```

任何會改變selector derivation、query semantics、normalization、bounds、admission、completeness或Revision semantics的configuration必須被stable version／identity涵蓋。Secret或credential value不得成為command identity；只可使用approved non-secret configuration identity。

## 5.2 Read Capture Outcome

```text
read_capture_outcome(capture_operation_id)
  → FOUND(CaptureTerminalOutcome)
  | NOT_FOUND
  | typed integrity failure
```

`NOT_FOUND`只表示Candidate-B authority可靠確認無committed terminal outcome。Malformed matching state不得偽裝成`NOT_FOUND`。

## 5.3 Resolve Snapshot

```text
resolve_snapshot(snapshot_id)
  → FOUND(immutable EvidenceSnapshot)
  | NOT_FOUND
  | typed integrity failure
```

## 5.4 Resolve Revision

```text
resolve_revision(revision_id)
  → FOUND(immutable EvidenceRevision)
  | NOT_FOUND
  | typed integrity failure
```

## 5.5 Compare Materiality

```text
compare_materiality(MaterialityRequest)
  → MaterialityResult
```

Normal pairwise request必須包含：

```text
baseline_revision_id
candidate_revision_id
materiality_rule_version
```

Initial path使用explicit `NO_BASELINE` input/result，不得用null baseline執行normal pairwise comparison。

## 5.6 Read Materiality Result

```text
read_materiality_result(materiality_result_id or semantic request identity)
  → FOUND(MaterialityResult)
  | NOT_FOUND
  | typed integrity failure
```

## 5.7 Local Readiness and Integrity

```text
validate_local_readiness()
  → READY | MIGRATION_REQUIRED | typed failure

validate_integrity(scope?)
  → VALID | typed findings/failure
```

Candidate-B local READY不表示Loki、Prometheus、Candidate A/C/D/E、SPEC-008、SPEC-011或whole platform READY。

## 5.8 Enumerate Recovery Facts

```text
enumerate_recovery_facts(filter/cursor?)
  → complete, integrity-aware B-owned recovery facts
```

Pagination/filter shape deferred，但不得造成silent omission、不一致page snapshot或把unreadable records當成absence。

---

# 6. Capture Command Identity and Replay Equivalence

## 6.1 Semantic Command Identity

Equivalent replay requires the same `capture_operation_id` and semantic equality of all fields that can change authoritative output：

- `incident_id`；
- `snapshot_at` as the same absolute instant after canonical UTC conversion；
- capture contract version；
- canonicalization version；
- source admission/policy version；
- evidence-affecting bounds/config identity；
- any future evidence-affecting command field。

Timezone offsets representing the same absolute instant are equivalent。Transport metadata、request arrival time、trace ID及process-local retry count不屬command semantic identity。

## 6.2 Contradictory Replay

Same `capture_operation_id` with any non-equivalent semantic field is `CONTRADICTORY_REPLAY`／capture operation conflict and must Fail Closed。It must not create, replace or relink Snapshot/Revision authority。

## 6.3 Terminal Outcome

```text
one capture_operation_id
→ zero or one durable terminal outcome
```

Successful terminal outcome contains the original `snapshot_id` and `revision_id`。Failure terminal outcome contains typed failure kind, disposition and safe provenance, but no successful Snapshot identity。

## 6.4 Invocation Failure and Terminal Finalization

An interruption before authoritative local commit is not a terminal outcome。A typed `RETRYABLE` invocation failure may remain non-terminal only while a SPEC-011-authorized retry remains possible；`read_capture_outcome` then remains reliable `NOT_FOUND` and no terminal capture outcome is committed。Candidate B does not determine retry timing、retry budget、next eligibility or exhaustion timing。

When Candidate B has a trustworthy determinate `NON_RETRYABLE` or `REPAIR_REQUIRED` failure and Candidate-B authority can be written safely, it must atomically commit a typed durable terminal `FAILURE`；it must not leave that operation non-terminal indefinitely。

SPEC-011 owns retry timing、budget、exhaustion and execution ordering，and Candidate E composes the cross-domain handoff。When retry is authoritatively exhausted, or an upper layer requests finalization through the formal public semantic handoff, Candidate B receives that authoritative exhaustion/finalization fact for the same `capture_operation_id`；Candidate B must not calculate or infer exhaustion itself。

After that handoff, the operation must commit exactly one terminal result。If trusted core remains valid and the source failure satisfies section 10.2 degradation admission, Candidate B commits terminal `SUCCESS` with a `DEGRADED` Evidence Snapshot；otherwise it commits a typed durable terminal `FAILURE`。The operation must not remain non-terminal indefinitely。

Once either terminal result is durable, equivalent replay returns that same result。Contradictory reuse or finalization of the same operation fails closed and must not create、replace or relink a second terminal outcome。A future business request requires a new authorized capture operation identity。

---

# 7. Trusted-core Capture Admission — D1

## 7.1 Normative Stabilization Sequence

Candidate B must perform this logical sequence for every first execution without an existing terminal outcome：

```text
1. Read authoritative Incident capture-relevant state → I1
2. Obtain ordered referenced event_ids from I1
3. Call EventStore.read_all_authoritative()
4. Resolve every referenced Event exactly once
5. Validate required Event identity and trusted-core integrity
6. Re-read authoritative Incident capture-relevant state → I2
7. Compare every Incident fact actually consumed by this capture
8. Admit collection only when I1 and I2 are semantically equivalent
```

An already committed equivalent replay returns the durable outcome without re-performing collection。

## 7.2 Capture-relevant Incident Projection

The projection contains every authoritative Incident fact used to derive or interpret the capture, at minimum：

```text
incident_id
ordered unique event_ids
status
severity
created_at
updated_at
last_correlated_at
correlation_context fields actually used by selector derivation
anchor identity/context actually used by capture
```

If implementation consumes additional Incident fields for selector derivation, source policy, normalization or provenance, those fields automatically become part of both I1/I2 projection and comparison。Implementation must not consume an Incident fact while excluding it from stabilization。

Fields not consumed by capture need not invalidate admission。I1/I2 equivalence compares normalized semantic values, not object identity、row ordering noise或physical serialization。

## 7.3 Incident Preconditions

- Explicit `incident_id` must resolve to one coherent authoritative Incident。
- Reliable absence returns typed `INCIDENT_NOT_FOUND`; it is not an empty successful capture。
- Empty referenced Event set is invalid trusted core because episode bounds cannot be derived and PRD-004 requires all referenced authoritative Events。
- Duplicate `event_ids` in Incident authority are malformed trusted core, not a dedup opportunity。
- Malformed or ambiguous Incident timestamps Fail Closed。

## 7.4 Required Event Contract

Every referenced Event must resolve exactly once from the one complete authoritative enumeration。Required validation includes：

```text
event_id                         non-empty canonical identity
detected_at                      timezone-unambiguous absolute time
event_type                       valid upstream value
event_source                     valid upstream value
severity                         valid upstream closed value
all selector-bearing fields      valid type/value when used
all normalized evidence fields   valid upstream shape when consumed
```

The complete upstream 15-field Event remains authoritative。Candidate B may project only needed fields into Snapshot, but validation cannot accept a malformed required Event merely because the malformed field was inconvenient。

## 7.5 Event Integrity Outcomes

- Referenced ID absent from complete enumeration → `REFERENCED_EVENT_NOT_FOUND`。
- Duplicate authoritative Event identity, whether content is identical or different → `DUPLICATE_EVENT_ID`。
- Event key differs from its `event_id`, identity-bearing content conflicts, or same identity has contradictory content → `CONTRADICTORY_EVENT_IDENTITY`。
- Required Event schema/content/timestamp invalid → `INVALID_REQUIRED_EVENT`。
- Enumeration incomplete, unreadable or raises integrity failure → `AUTHORITATIVE_EVENT_ENUMERATION_FAILURE`。

All above prohibit successful Snapshot admission。

## 7.6 Stabilization Conflict

If any consumed I1 fact differs semantically from I2, result is `INCIDENT_CHANGED_DURING_CAPTURE`。Candidate B must discard uncommitted collection output, preserve only safe failure evidence where terminal failure is committed, and must not classify the condition as `DEGRADED`。

## 7.7 Forbidden Coupling

D1 does not authorize private Incident DB access、shared DB、cross-store transaction、foreign key、2PC、new Incident authority or new Event authority。

---

# 8. Time, Episode and Collection Windows — D3

## 8.1 Authoritative `snapshot_at`

Caller must provide a timezone-aware absolute `snapshot_at`。Candidate B validates and losslessly canonicalizes it to UTC semantics。Naive datetime、non-finite epoch、unparseable timestamp或out-of-range representation is `INVALID_CAPTURE_COMMAND`。

Candidate B must not call a hidden wall clock to replace or modify `snapshot_at`。Process-local timing may measure latency but cannot become business time。

If a Capture/Materiality result persists another business timestamp, that timestamp must likewise be supplied as a timezone-aware authoritative absolute input by the composing caller。A local wall clock may be used only for explicitly non-authoritative operational telemetry and must not affect replay identity、Revision、Materiality or scheduling eligibility。

## 8.2 Episode

After trusted Events validate：

```text
episode_start = min(referenced Events.detected_at)
episode_end   = max(referenced Events.detected_at)
```

Comparisons use absolute instants。`episode_start <= episode_end` must hold。Incident `created_at` must not replace technical Event time。

## 8.3 Default Windows

```text
logs_start = episode_start - 120 seconds
logs_end   = min(snapshot_at, episode_end + 120 seconds)

metrics_start = episode_start - 300 seconds
metrics_end   = min(snapshot_at, episode_end + 120 seconds)
```

These PRD-004 values are config-driven PoC defaults, not permanent production constants。Any override must have a non-secret version/config identity included in capture command equivalence and Snapshot provenance。

## 8.4 Endpoint Semantics

Logical collection windows are closed at both ends：

```text
[start, end]
```

Adapters must request and normalize data so evidence at exact start/end is neither silently lost nor double-counted。If a source API uses different physical boundary semantics, adapter provenance must record the translation and normalization must enforce the logical closed interval。

`end < start` is an invalid collection boundary and prohibits successful Snapshot。`end == start` is a valid zero-duration instant window; it is not automatically empty and must still be queried according to source semantics。

## 8.5 Post-context Facts

Candidate B exposes at least：

```text
episode_end
default_post_context_boundary = episode_end + 120 seconds
effective source window ends
snapshot_at
whether each effective window reached its configured upper boundary
```

These are collection-boundary facts only。Candidate E/SPEC-011 determine whether and when follow-up executes, absolute wake-up, coalescing and restart scheduling。

---

# 9. Source Adapter Semantic Contracts

## 9.1 Common Request

Each source adapter receives a semantic request containing：

```text
source
incident_id
window_start
window_end
derived selectors
query semantic identity/version
bounds policy identity
request timeout/budget identity where applicable
```

Credential values、authorization headers及transport secrets are never semantic request provenance。

## 9.2 Common Result

Adapter result must distinguish：

```text
status
normalized records
query provenance
selector provenance
collection provenance
validation findings
bound/omission facts
safe source failure information
```

Returning only an untyped list is insufficient because `[]` cannot distinguish `EMPTY`, `UNAVAILABLE` and `INVALID`。

## 9.3 Loki Contract

Loki request semantics include bounded Log window and allowlisted selectors derived from stabilized Incident/Event evidence。A valid successful Loki response：

- is a recognized response shape/version；
- is complete for the adapter's declared pagination/bounds protocol；
- has parseable absolute timestamps；
- has valid stream/label/value shapes；
- contains no record outside the logical window after normalization；
- yields deterministic normalized ordering and dedup facts。

No matching streams/entries in a valid successful response is `EMPTY`。Transport/timeout/service inability is `UNAVAILABLE`。Malformed, contract-incompatible, unsafe or incompletely enumerable response is `INVALID`。

Exact HTTP library、LogQL syntax builder and pagination implementation are deferred。

## 9.4 Prometheus Contract

Prometheus request semantics include bounded Metric window, approved metric/query semantic identity, allowlisted labels/selectors and required step/range policy identity。A valid successful range response：

- is a recognized successful matrix/range shape；
- has valid series labels and sample tuples；
- uses parseable finite absolute timestamps and finite values；
- has deterministic series/sample ordering after normalization；
- reports any deduplication, aggregation or omitted samples；
- contains no accepted sample outside the logical window。

Valid successful response with no matching series/samples is `EMPTY`。Transport/timeout/service inability is `UNAVAILABLE`。Wrong result type, malformed series/sample, unsafe label/content or contract-incompatible response is `INVALID`。

Existing detector code that collapses these cases to `[]` is not the Candidate-B contract。Exact HTTP library、PromQL builder and pagination implementation are deferred。

## 9.5 Query and Selector Provenance

Provenance must be sufficient to reproduce semantic intent without storing secrets：

```text
source
query semantic/version identity
logical window
normalized allowlisted selectors
adapter contract version
bounds policy identity
page/continuation summary where applicable
response validation status
```

Raw authorization material、secret-bearing URLs或tokens must never be stored。

---

# 10. Source Status and Completeness

## 10.1 Source Status

| Status | Normative Meaning |
|---|---|
| `AVAILABLE` | Source request completed validly and produced one or more trusted normalized records. |
| `EMPTY` | Source request completed validly and produced no matching trusted records. This is legitimate absence, not failure. |
| `UNAVAILABLE` | Source could not be queried or completed because infrastructure/provider/transport was unavailable. No evidence from that source is admitted. |
| `INVALID` | Response, reference, version, content, pagination, safety or validation failed contract. Invalid payload is never admitted as evidence. |

Statuses describe source collection truth, not retry disposition。Same failure kind may receive different disposition only through the typed domain policy; Runtime must not infer from status text。

## 10.2 Admission Matrix

Trusted core must first pass D1 in every successful case。

For source unavailability subject to PRD-004 bounded retry, the first `UNAVAILABLE` result is a typed `RETRYABLE` non-terminal invocation failure, not immediate `DEGRADED` success。The `UNAVAILABLE` success rows below apply only after SPEC-011 has performed the bounded retry and Candidate B has received the authoritative exhaustion/finalization handoff described in section 6.4。

| Source outcome | Successful Snapshot allowed? | Required completeness |
|---|---|---|
| All configured sources `AVAILABLE` or `EMPTY`, no completeness-affecting omission | YES | `FULL` |
| After required bounded retry and authorized exhaustion/finalization, one or more sources remain `UNAVAILABLE`, policy explicitly permits that source deficiency, and remaining evidence remains trustworthy | YES | `DEGRADED` |
| After authorized exhaustion/finalization, `UNAVAILABLE` source is required by active policy or remaining evidence cannot be trusted/interpreted honestly | NO | typed durable terminal `FAILURE` |
| `INVALID` confined to one optional external source, entire invalid payload excluded, policy explicitly permits source absence, and remaining evidence remains trustworthy | YES | `DEGRADED` with `INVALID` status and explicit exclusion reason |
| `INVALID` affects trusted core, selector derivation, admitted records, pagination completeness, redaction safety or the ability to know what was included | NO | Fail Closed |
| Any D1 trusted-core contradiction | NO | Fail Closed; never `DEGRADED` |

`INVALID` never automatically degrades。The successful degraded exception requires all listed isolation and policy conditions; otherwise capture cannot commit success。

## 10.3 Completeness Derivation

`FULL` requires all configured source contracts to have valid `AVAILABLE`/`EMPTY` results and no known semantic evidence loss。Lossless canonical deduplication or aggregation may remain `FULL` only when policy/version and provenance prove preservation of all required semantics。

`DEGRADED` is required for any permitted missing source、excluded invalid optional source、sampling、truncation or lossy aggregation that may omit distinct evidence。Completeness is only attached to successful Snapshots；terminal failures have no successful completeness value。

---

# 11. Normalized Evidence and Provenance

## 11.1 Normalized Log Evidence

A logical Log evidence record contains at least：

```text
source = LOKI
record semantic identity
observed_at
normalized allowlisted labels
normalized bounded message/content
incident/event linkage when derivable from authoritative evidence
query/selector provenance reference
collection provenance reference
redaction status/version
```

Ordering is deterministic by semantic time and stable tie-break identity。Raw unbounded source payload is not required and must not bypass bounds/redaction。

## 11.2 Normalized Metric Evidence

A logical Metric evidence record contains at least：

```text
source = PROMETHEUS
series semantic identity
metric/query semantic identity
normalized allowlisted labels
sample timestamp or aggregate interval
finite normalized value/aggregate
unit/aggregation semantics where applicable
incident/event linkage when derivable
query/selector provenance reference
collection provenance reference
```

Ordering is deterministic by series identity, absolute time and stable tie-break。NaN、infinity、unparseable timestamp/value or unsafe labels are invalid unless a versioned normalization rule explicitly excludes the entire invalid source payload under section 10.2。

## 11.3 Bounds and Omission Facts

Every source collection records：

```text
bounds_policy_version / budget identity
observed candidate count
included count
omitted count, or explicit UNKNOWN only when status prevents reliable count
omission reason
sampling applied + policy identity
aggregation applied + semantic rule identity
dedup applied + input/output counts
truncation applied + boundary/reason
completeness impact
```

Unknown omission magnitude must be explicit and cannot support `FULL`。No adapter may silently drop malformed, excess, duplicate or out-of-window data。

## 11.4 Bounded Payload

Normalized content, labels, record count, per-record size and total Snapshot payload must be bounded by versioned/configured policy。Exact numeric values are configuration/implementation precision unless an upstream authority freezes them。

---

# 12. Evidence Snapshot and Revision

## 12.1 Evidence Snapshot

An immutable Snapshot contains at least：

```text
snapshot_id
capture_operation_id
incident_id
snapshot_at
capture contract/canonicalization/source/bounds versions
stabilized Incident capture projection
ordered referenced Event projections
episode_start / episode_end
Log and Metric logical windows
normalized Log and Metric evidence
per-source statuses
EvidenceCompleteness
query/selector/collection provenance
bounds, inclusion and omission facts
post-context collection-boundary facts
revision_id binding
```

Snapshot is operational history。It preserves what was requested, queried, observed, excluded and committed for that capture, even when another Snapshot has the same semantic Revision。

## 12.2 Evidence Revision

An immutable Revision contains：

```text
revision_id
incident_id
canonicalization_version
canonical semantic evidence representation/reference
semantic source status/completeness facts
semantic omission/normalization facts
integrity metadata
```

Revision identity is deterministically derived from collision-resistant canonical semantic identity properties。Exact physical hash algorithm、library and byte encoding are Implementation Choices, but implementation must prove deterministic identity, domain separation and collision handling that fails closed rather than aliases contradictory content。

## 12.3 Canonical Semantic Inputs

Revision canonicalization includes facts whose change alters evidence meaning：

- stabilized Incident evidence context；
- ordered authoritative Event evidence projections；
- normalized Log/Metric evidence；
- source status and completeness；
- semantic omission、sampling、aggregation、dedup/truncation facts；
- semantic collection-boundary facts where they affect interpretation；
- canonicalization/rule versions。

It excludes purely operational noise that does not change evidence meaning, including `snapshot_id`、`capture_operation_id`、request arrival time、process latency、physical row order and transport trace identifiers。

Different query windows with identical admitted semantic evidence may resolve to the same Revision only when canonical source status、completeness、omission facts and evidence interpretation are also equivalent。A changed boundary that changes completeness or interpretation must produce a different Revision。

## 12.4 Revision Reuse and Integrity

```text
same Incident + same canonical semantic evidence
→ same revision_id

different Snapshot
→ may bind same revision_id
```

Same `revision_id` resolving to different Incident, canonicalization version or semantic content is integrity corruption and must Fail Closed。Revision ID equality alone never bypasses record resolution/integrity validation。

---

# 13. Materiality — D4

## 13.1 Authority Protocol

```text
Candidate A Current
→ authoritative baseline_revision_id
Candidate E
→ composes explicit request
Candidate B
→ resolves both Revisions and judges pairwise Materiality
```

Candidate B must not select latest/highest/newest prior Revision, infer baseline from B history or read Candidate-A private persistence。

## 13.2 Pairwise Request

```text
baseline_revision_id
candidate_revision_id
materiality_rule_version
```

Both Revisions must resolve, pass integrity validation and belong to the same Incident。Comparison is direct baseline→candidate and non-transitive。

```text
A→B NON_MATERIAL
B→C NON_MATERIAL
does not imply A→C NON_MATERIAL
```

If Candidate A says baseline A, B must compare A→C directly。

## 13.3 Judgements

| Judgement | Meaning |
|---|---|
| `SAME` | Both resolved inputs represent the same canonical semantic evidence under compatible canonicalization/rule semantics. ID equality is evidence but not a substitute for integrity resolution. |
| `NON_MATERIAL` | Semantic evidence differs, but the requested rule version determines the change does not materially affect hypothesis ranking, support, conclusion, remediation safety or another PRD-004 Material Evidence criterion. |
| `MATERIAL` | The requested rule version determines the pair contains Material Evidence requiring the composing domain to consider refresh. Candidate B does not schedule that refresh. |
| `REPAIR_REQUIRED` | A trustworthy comparison cannot be made because authority, lineage, schema, identity or rule state is contradictory/corrupt. |

Revision ID difference alone must never return `MATERIAL`。

## 13.4 Initial / No Baseline

When Candidate A authoritative Current is absent, Candidate E invokes/records an explicit `NO_BASELINE` evaluation path。Result is `INITIAL / NO_BASELINE`, not `MATERIAL` and not comparison against null。No pairwise judgement is fabricated。

## 13.5 Invalid Materiality Inputs

| Condition | Required result |
|---|---|
| Reliable missing baseline/candidate Revision | typed dangling/missing Revision failure; no judgement |
| Cross-Incident Revision pair | `REPAIR_REQUIRED` / typed conflict |
| Malformed or integrity-invalid Revision | `REPAIR_REQUIRED` |
| Unsupported `materiality_rule_version` | `UNSUPPORTED_MATERIALITY_RULE`; no guessed fallback |
| Contradictory replay for same Materiality result identity | Fail Closed |

## 13.6 Materiality Result

Stored pairwise result includes：

```text
result identity
baseline_revision_id
candidate_revision_id
materiality_rule_version
judgement
bounded deterministic reason/fact summary
non-secret evaluation provenance
```

Equivalent replay returns the same result。Candidate B stores judgement truth but not Runtime refresh work or Candidate-A freshness mutation。

---

# 14. Evidence Store, Atomicity and Schema — D5

## 14.1 Topology

Candidate B uses an independent configurable-path Evidence Store。It must not share a physical database, writable table, transaction authority or foreign key with Incident、Runtime、RCA or Knowledge stores。Current PoC persistence technology may be SQLite, but this SPEC does not make SQLite a permanent product requirement。

## 14.2 Logical Records

The Store owns logical records for：

- Capture Operation Result；
- immutable Evidence Snapshot；
- immutable Evidence Revision；
- Snapshot→Revision binding；
- Materiality Result；
- schema/readiness metadata；
- Candidate-B recovery facts。

Physical tables/columns/indexes are deferred。

## 14.3 Successful Capture Commit

First successful capture must commit in one Candidate-B local crash-consistent semantic transaction：

```text
terminal SUCCESS
+ immutable Snapshot
+ Revision create-or-verify
+ Snapshot→Revision binding
+ source status/completeness
+ required provenance and bounds/omission facts
→ all commit or all rollback
```

No observable state may contain SUCCESS without its Snapshot, Snapshot without required binding, operation result pointing to a contradictory Snapshot, or Revision identity bound to contradictory content。

Reuse of an existing equivalent Revision is create-or-verify, not mutation。

## 14.4 Terminal Failure Commit

A terminal failure commit atomically stores：

```text
capture_operation_id
semantic command identity
terminal FAILURE
typed failure kind
RetryDisposition
safe bounded failure provenance
```

It must not create a successful Snapshot or Revision binding。Failure payload cannot contain unrestricted raw logs, secret material or partial invalid evidence represented as trusted truth。

A trustworthy determinate `NON_RETRYABLE` or `REPAIR_REQUIRED` failure must use this commit when Candidate-B authority can be written safely。A `RETRYABLE` failure may remain uncommitted only while SPEC-011-authorized retry remains possible；after authoritative exhaustion/finalization, Candidate B must use this commit unless section 10.2 admits terminal `DEGRADED` success。Atomic uniqueness enforces exactly one terminal result for the `capture_operation_id`；contradictory finalization fails closed without a second result。

## 14.5 Materiality Commit

First Materiality result atomically binds its semantic request identity to one result。Equivalent replay returns it；contradictory pair/rule/result under the same identity fails closed。Materiality commit does not mutate Snapshot/Revision or Candidate-A Current。

## 14.6 Schema Governance

```text
current recognized schema
→ validate integrity, then operate

recognized older schema
→ MIGRATION_REQUIRED

unknown / malformed / unsafe schema
→ Fail Closed
```

No silent automatic migration, guess-and-rewrite, destructive repair or store recreation is allowed during normal startup/runtime。Migration tooling and procedure require a separately governed implementation plan。

---

# 15. Readiness and Integrity

## 15.1 Local Readiness Scope

Candidate-B local readiness means the Evidence Store can reliably open, recognize schema, read/decode its authority, validate required invariants and completely enumerate recovery facts。It does not assert source availability or whole-platform readiness。

## 15.2 Mandatory Integrity Checks

At minimum validate：

- physical store integrity appropriate to chosen technology；
- recognized schema/version；
- unique stable identities；
- Capture Result ↔ semantic command identity consistency；
- SUCCESS ↔ exactly one Snapshot consistency；
- FAILURE ↔ no successful Snapshot authority；
- Snapshot immutability and required fields；
- Snapshot→Revision total and non-contradictory binding；
- Revision Incident scope and canonical identity consistency；
- source status/completeness/admission invariants；
- required provenance and bounds facts；
- Materiality pair/result/rule consistency；
- absence of dangling required references；
- complete recovery enumeration。

## 15.3 Store-wide Uncertainty

If Store cannot reliably enumerate, distinguish readable from missing records, determine schema safety or validate global identity/binding invariants, local readiness fails closed。It must not return partial recovery set or `NOT_FOUND` from uncertain authority。

## 15.4 Per-record Corruption

Per-record corruption may be isolated only when Store can prove all unaffected authority remains completely enumerable and the corrupt record cannot alias/conflict with it。The corrupt subject returns typed `REPAIR_REQUIRED` and remains preserved。If isolation cannot be proven, treat as store-wide uncertainty。

## 15.5 Dangling and Contradictory State

Missing required Snapshot/Revision/result、contradictory receipt/result、same identity with different semantics、invalid completeness/status combination or malformed Materiality lineage must never be silently skipped、last-write-wins repaired or represented as clean absence。

---

# 16. Restart and Recovery

## 16.1 Restart Is Recovery, Not Reset

Reopen/restart must preserve terminal outcomes、Snapshots、Revisions、bindings、Materiality results、provenance and recovery facts。Restart must not reset identities, reclassify terminal failure, delete contradiction or recapture solely because process memory was lost。

## 16.2 Recovery Enumeration

Candidate B authoritative recovery enumeration must expose, with integrity-aware classification：

- committed capture terminal outcomes；
- successful Snapshot/Revision identities and bindings；
- terminal capture failures and dispositions；
- committed Materiality results；
- relevant episode/window/post-context boundary facts；
- records requiring reconciliation or governed repair；
- local schema/readiness findings needed by caller。

## 16.3 Recovery Boundary

Candidate B only enumerates/reads B-owned truth。Candidate E/SPEC-011 decides discovery order、work reconstruction、retry eligibility、wake-up、startup barrier and execution。Enumeration must not sleep、schedule、consume retry budget、create RCA Attempt or promote Current。

## 16.4 Crash Matrix

| Crash point | Durable B truth | Required recovery behavior |
|---|---|---|
| Before local terminal transaction | No terminal outcome/Snapshot authority | Same command may execute after full fresh validation. |
| During local transaction | All rollback | No partial result, Snapshot or binding is visible. |
| After SUCCESS commit, before response | Result + Snapshot + binding + provenance | Equivalent replay returns same `snapshot_id`/`revision_id`. |
| After FAILURE commit, before response | Same terminal failure | Equivalent replay returns failure; no Snapshot created. |
| After Revision reuse commit response loss | New Snapshot bound to verified existing Revision | Replay returns same Snapshot and shared Revision. |
| After Materiality commit response loss | Pairwise result durable | Equivalent replay returns same judgement. |
| Repeated restart | All committed B facts | Idempotent reads/enumeration; no reset or duplicate authority. |

---

# 17. Cross-domain Interfaces and Required Flows

## 17.1 Candidate A Handoff

Candidate B provides：

```text
evidence_snapshot_id
evidence_revision_id
EvidenceCompleteness
resolvable source/provenance references
Materiality result/basis when requested
```

Candidate A stores exact lineage references and its own Current baseline basis。It does not duplicate Snapshot/Revision content authority；Candidate B does not mutate Aggregate/Attempt/Version/Current。

## 17.2 Candidate E Handoff

Candidate E provides explicit Incident、capture operation、`snapshot_at` and, for refresh, Candidate-A baseline。Candidate E consumes terminal capture, post-context boundary and Materiality facts to compose higher-level protocol。Candidate B does not decide RCA eligibility、Attempt creation or refresh scheduling。

## 17.3 SPEC-011 Handoff

SPEC-011 supplies authoritative absolute time and orchestrates work/retry/recovery through public semantic capabilities。Candidate-B failures expose typed `RetryDisposition`; SPEC-011 owns timing/budget and must not parse messages to infer safety。

## 17.4 Flow A — Initial Capture

```text
explicit incident_id + capture_operation_id + snapshot_at
→ D1 trusted-core stabilization
→ bounded source collection
→ source status/completeness
→ atomic SUCCESS + Snapshot + Revision binding
→ explicit NO_BASELINE / INITIAL path
```

## 17.5 Flow B — Degraded Source

```text
trusted core valid
+ source UNAVAILABLE with RETRYABLE disposition
→ no terminal capture outcome yet
→ SPEC-011 bounded retry under its timing/budget authority
→ Candidate E/SPEC-011 authoritative exhaustion/finalization handoff
→ Candidate B applies section 10.2 admission rules
+ permitted isolated source deficiency and trustworthy remaining evidence
→ explicit UNAVAILABLE/INVALID status and omission facts
→ exactly one terminal SUCCESS with DEGRADED Snapshot
```

An `INVALID` optional-source deficiency follows the same final admission rules but does not become retryable merely from its source status。

## 17.6 Flow C — Trusted-core Contradiction / Terminal Failure

```text
Incident changed or required Event missing/duplicate/invalid
→ trustworthy NON_RETRYABLE or REPAIR_REQUIRED
   → immediate typed durable terminal FAILURE when B authority is safely writable
→ RETRYABLE while SPEC-011-authorized retry remains possible
   → non-terminal invocation failure; no terminal capture outcome
   → authoritative exhaustion/finalization handoff
   → typed durable terminal FAILURE if trusted core is still invalid
→ no successful Snapshot
```

The same finalization rule applies when exhausted source failure does not satisfy section 10.2：the same `capture_operation_id` commits exactly one typed durable terminal `FAILURE`。Equivalent replay returns it；contradictory finalization fails closed without creating another terminal outcome。

## 17.7 Flow D — Response-loss Replay

```text
SUCCESS local commit
→ response lost
→ same equivalent capture_operation_id replay
→ same snapshot_id and revision_id
```

## 17.8 Flow E — Same Revision Across Captures

```text
Capture S1 → Revision R1
Later semantically equivalent capture S2 → Revision R1
```

S2 remains a distinct immutable operational Snapshot。Revision reuse does not itself create a Materiality judgement。

## 17.9 Flow F — Refresh Materiality

```text
Candidate A authoritative Current baseline R10
→ Candidate E requests B compare R10 → R12 with rule version
→ Candidate B resolves direct pair
→ SAME / NON_MATERIAL / MATERIAL / REPAIR_REQUIRED
```

## 17.10 Flow G — Post-context

Candidate B owns episode/post-context collection-boundary facts and later Snapshot/Revision/Materiality truth。Candidate E/SPEC-011 owns when follow-up runs and its durable wake-up continuity。

## 17.11 Flow H — Restart / Recovery

```text
Candidate B Store reopen
→ schema/integrity validation
→ recover and enumerate B-owned durable truth
→ Candidate E/SPEC-011 uses facts for orchestration
```

---

# 18. Security and Ground-truth Isolation

## 18.1 Forbidden Production Inputs and Persistence

Production selector、query decision、Snapshot、Revision、Materiality and downstream evidence handoff must not use or persist：

```text
scenario_id
generator internal/current state
validator expected answer
expected RCA cause or class
competition/evaluation Ground Truth
S1～S6 answer mapping
evaluation fixture answer
evaluation-only run identity as business identity
```

Evaluation may read approved production outputs through public semantics but cannot mutate Candidate-B authority or supply answer labels to production capture。

## 18.2 Selector Safety

Selectors may only derive from versioned allowlisted fields in stabilized authoritative Incident/Event evidence。Values must undergo type validation、canonicalization and source-appropriate escaping。Raw string concatenation that permits LogQL/PromQL/query injection is forbidden。

Selectors and query provenance must be bounded。Unknown fields、unapproved labels、scenario metadata and generator state are rejected or excluded according to typed source safety rules；they are never silently promoted to selectors。

## 18.3 Redaction

Redaction/allowlist occurs before：

1. normalized evidence persistence；
2. query/selector provenance persistence；
3. any Candidate-D LLM/prompt handoff；
4. logs、telemetry or failure summaries。

Redaction rule version is non-secret provenance。If Candidate B cannot prove unsafe material was removed, affected source is `INVALID` and its payload is excluded。Successful DEGRADED admission is allowed only under section 10.2；otherwise capture fails closed。

## 18.4 Secret Exclusion

Credentials、tokens、API keys、Authorization headers、cookies、secret-bearing URLs/query material and raw secret values must not be stored in Snapshot、Revision、Materiality、receipt、failure、logs or telemetry。Credential presence is not business evidence provenance。

## 18.5 Bounded Safety

Evidence and provenance fields require per-field and total payload bounds。Overflow must produce explicit truncation/omission facts or typed invalid/failure behavior；silent truncation is forbidden。

---

# 19. Failure Model

## 19.1 Failure Kind vs Retry Disposition

Failure kind states what happened。`RetryDisposition` states whether another invocation may be safe。Candidate B/domain policy decides disposition；SPEC-011 decides timing and budget。

```text
failure kind ≠ retry schedule
source status ≠ retry disposition
terminal failure ≠ pre-commit interruption
```

## 19.2 Typed Failure Families

Exact language enum spelling may vary only if semantic mapping remains one-to-one。

| Failure family | Meaning | Default disposition |
|---|---|---|
| `INVALID_CAPTURE_COMMAND` | Missing/invalid ID, time or version/config input. | `NON_RETRYABLE` |
| `INCIDENT_NOT_FOUND` | Explicit Incident reliably absent. | `REPAIR_REQUIRED` |
| `MALFORMED_INCIDENT_STATE` | Incident projection cannot be trusted. | `REPAIR_REQUIRED` |
| `INCIDENT_CHANGED_DURING_CAPTURE` | I1/I2 consumed facts differ. | `RETRYABLE` only when fresh recapture is domain-safe; otherwise `REPAIR_REQUIRED` |
| `REFERENCED_EVENT_NOT_FOUND` | Incident references absent authoritative Event. | `REPAIR_REQUIRED` |
| `DUPLICATE_EVENT_ID` | Duplicate authoritative identity. | `REPAIR_REQUIRED` |
| `CONTRADICTORY_EVENT_IDENTITY` | Same/mismatched identity has contradictory content. | `REPAIR_REQUIRED` |
| `INVALID_REQUIRED_EVENT` | Required Event schema/content/time invalid. | `REPAIR_REQUIRED` |
| `AUTHORITATIVE_EVENT_ENUMERATION_FAILURE` | Complete Event enumeration unavailable/unreliable. | typed `RETRYABLE` or `REPAIR_REQUIRED` from source condition |
| `SOURCE_UNAVAILABLE` | Loki/Prometheus unavailable；where PRD-004 requires bounded retry, degradation cannot be admitted on the first failure. | commonly `RETRYABLE`; policy remains explicit |
| `SOURCE_INVALID` | Source result invalid and cannot be safely isolated/degraded. | `NON_RETRYABLE` or `REPAIR_REQUIRED` by cause |
| `UNSAFE_EVIDENCE_CONTENT` | Allowlist/redaction safety cannot be proven. | `NON_RETRYABLE` or `REPAIR_REQUIRED` |
| `CAPTURE_OPERATION_CONFLICT` | Stable operation identity already has incompatible binding. | `REPAIR_REQUIRED` |
| `CONTRADICTORY_REPLAY` | Same operation/result identity used with different semantics. | `REPAIR_REQUIRED` |
| `MIGRATION_REQUIRED` | Recognized older Candidate-B schema. | `REPAIR_REQUIRED` |
| `UNSUPPORTED_EVIDENCE_SCHEMA` | Unknown/unsafe schema. | `REPAIR_REQUIRED` |
| `EVIDENCE_STORE_INTEGRITY_FAILURE` | Local authority cannot be trusted. | `REPAIR_REQUIRED` |
| `DANGLING_EVIDENCE_REFERENCE` | Required Snapshot/Revision/result reference missing. | `REPAIR_REQUIRED` |
| `UNSUPPORTED_MATERIALITY_RULE` | Requested rule version unavailable. | `NON_RETRYABLE` until compatible rule is supplied |
| `MATERIALITY_REPAIR_REQUIRED` | Pair/lineage cannot be trusted. | `REPAIR_REQUIRED` |
| `TRANSIENT_EVIDENCE_STORE_FAILURE` | Explicitly classified temporary local store failure. | `RETRYABLE` |

Disposition must be returned as a typed field, not inferred from message。A terminal record preserves its original disposition; Runtime exhaustion does not rewrite domain failure kind。

For capture failures, terminalization is closed：a trustworthy determinate `NON_RETRYABLE` or `REPAIR_REQUIRED` failure commits terminal `FAILURE` immediately when Candidate-B authority is safely writable；a `RETRYABLE` failure may remain non-terminal only while SPEC-011-authorized retry remains possible。SPEC-011 owns timing、budget、next eligibility、exhaustion and ordering；Candidate B receives, but never derives, the authoritative exhaustion/finalization fact through the public semantic handoff。After that handoff Candidate B must commit exactly one terminal result under sections 6.4 and 10.2：admitted `DEGRADED` `SUCCESS`, otherwise typed durable `FAILURE`。

## 19.3 Legitimate Absence

The following are not interchangeable failures：

- source `EMPTY` is valid collection absence；
- public `NOT_FOUND` is reliable absence of requested B-owned record；
- `NO_BASELINE` is valid initial Materiality state；
- missing required Incident/Event/Revision is a typed failure；
- unreadable/malformed authority is integrity failure, never absence。

---

# 20. Concurrency and Idempotency

## 20.1 Same Capture Operation

Concurrent equivalent requests with the same `capture_operation_id` must converge to at most one terminal outcome。If one commits SUCCESS, all equivalent callers resolve the same Snapshot/Revision；if one commits FAILURE, all equivalent callers resolve the same failure。No duplicate Snapshot/result authority is allowed。

Concurrent contradictory requests under the same operation identity must Fail Closed。At most a semantically matching contender may commit；the conflict cannot be resolved by last-write-wins or timestamp winner。

## 20.2 Concurrent Captures for Same Incident

Different operation IDs may capture the same Incident concurrently。Each must independently satisfy D1 stabilization。They create distinct Snapshots if successful, but deterministic create-or-verify ensures semantically equivalent evidence resolves to the same Incident-scoped Revision。

No capture may mutate another Snapshot or infer a Materiality baseline from commit order。

## 20.3 Revision Consistency

Concurrent creation of equivalent Revision semantics must converge to one Revision identity/content。Concurrent collision or same identity/different content must Fail Closed without aliasing or overwrite。

## 20.4 Materiality Replay

Concurrent equivalent pairwise Materiality requests converge to the same result。Same result identity with different baseline/candidate/rule or judgement is contradiction and must Fail Closed。

## 20.5 Implementation Boundary

Implementation must provide local write serialization、fresh-read guards、uniqueness protection and semantic conflict detection sufficient for these guarantees。Exact transaction statement、lock、mutex、CAS or SQLite pragma is deferred。

---

# 21. Acceptance Criteria

## AC-013-A — Authority and Scope

- Candidate B owns only capture/Snapshot/Revision/source/completeness/provenance/Materiality/Evidence-Store truth。
- Candidate A receives references only；Candidate E composes；SPEC-011 schedules/orchestrates；SPEC-008 remains Incident authority。
- No private cross-domain DB access、shared DB、2PC or duplicate Runtime authority exists。

## AC-013-B — Trusted-core Stabilization

- A first capture performs I1 → authoritative Event enumeration → exact Event resolution/validation → I2。
- Every consumed Incident fact is compared；semantically changed Incident fails closed。
- Missing, duplicate, contradictory or malformed required Event prevents successful Snapshot。
- Event enumeration integrity failure never yields partial/degraded trusted core。

## AC-013-C — Capture Identity and Replay

- Equivalent same-operation replay returns the same terminal outcome。
- Successful replay returns identical `snapshot_id` and `revision_id`。
- Contradictory replay or contradictory finalization fails closed and cannot create a second terminal outcome。
- Pre-commit crash leaves no terminal outcome/Snapshot authority；post-commit response loss does not duplicate outcome。
- Authoritative exhaustion/finalization resolves the same `capture_operation_id` to exactly one terminal result；it cannot remain non-terminal indefinitely。

## AC-013-D — Snapshot and Revision Identity

- Snapshot is immutable operational capture identity；Revision is immutable Incident-scoped semantic evidence identity。
- Every successful Snapshot has exactly one Revision binding。
- Different successful captures may produce distinct Snapshots bound to the same Revision。
- Same Revision identity with contradictory content or Incident scope fails closed。

## AC-013-E — Time and Windows

- `snapshot_at` must be caller-supplied timezone-aware absolute time and canonicalizes to UTC semantics。
- Episode uses min/max referenced Event `detected_at`。
- Default Log and Metric windows match PRD-004 and enforce declared endpoint semantics。
- Candidate B does not obtain wall-clock or wake-up scheduling authority。

## AC-013-F — Source Adapter Semantics

- Loki and Prometheus adapters return typed status/result/provenance rather than an ambiguous list。
- Tests distinguish valid non-empty、valid empty、unavailable and invalid responses。
- Malformed/out-of-window/unsafe data cannot silently enter normalized evidence。
- Existing Prometheus `[]` behavior is not treated as formal Candidate-B semantics。

## AC-013-G — Status and Completeness

- `AVAILABLE`, `EMPTY`, `UNAVAILABLE` and `INVALID` remain distinct。
- Valid `EMPTY` does not itself degrade completeness。
- Source `UNAVAILABLE` subject to PRD-004 bounded retry first returns a `RETRYABLE` non-terminal failure；it cannot produce `DEGRADED` success before SPEC-011 retry exhaustion and authoritative finalization handoff。
- After authorized exhaustion/finalization, a permitted isolated source deficiency with trustworthy remaining evidence produces terminal `SUCCESS` with explicit `DEGRADED` status and omissions；otherwise it produces typed terminal `FAILURE`。
- `INVALID` degrades only when fully isolated/excluded and policy permits；otherwise capture fails。
- Trusted-core contradiction never becomes `DEGRADED`。

## AC-013-H — Normalization, Bounds and Provenance

- Log/Metric records preserve source、time、normalized content/value and resolvable query/collection provenance。
- Every sampling/aggregation/dedup/truncation operation records policy/version、counts、reason and completeness impact。
- Unknown omissions are explicit；silent loss fails acceptance。
- Payload and provenance remain bounded。

## AC-013-I — Canonicalization and Revision

- Canonicalization version and semantic inputs are persisted/resolvable。
- Same Incident and same semantic evidence deterministically resolve to the same Revision identity。
- Pure operational noise does not force a new Revision；changed evidence meaning does。
- Exact physical hash algorithm remains replaceable without weakening identity/integrity guarantees。

## AC-013-J — Materiality

- Pairwise request explicitly supplies baseline、candidate and rule version。
- Candidate B never selects baseline or infers it from Revision order/time。
- `SAME`, `NON_MATERIAL`, `MATERIAL`, `REPAIR_REQUIRED` and `NO_BASELINE` have distinct observable outcomes。
- Revision ID difference alone never determines Materiality。
- A→B and B→C results cannot substitute for required A→C direct comparison。
- Missing/cross-Incident/malformed Revision and unsupported rule version return typed outcomes。

## AC-013-K — Persistence Atomicity

- SUCCESS、Snapshot、Revision binding、status/completeness and required provenance commit together or all rollback。
- Trustworthy determinate `NON_RETRYABLE` or `REPAIR_REQUIRED` failure commits typed terminal `FAILURE` without successful Snapshot authority when Candidate-B authority is safely writable。
- A `RETRYABLE` failure remains non-terminal only while authorized retry remains possible；authoritative exhaustion/finalization commits exactly one terminal `DEGRADED` `SUCCESS` or typed terminal `FAILURE`。
- No success receipt points to missing/contradictory Snapshot or Revision。
- Materiality result replay is locally atomic and contradiction-safe。

## AC-013-L — Schema, Readiness and Integrity

- Current recognized schema validates before operation。
- Recognized older schema returns `MIGRATION_REQUIRED` without silent migration。
- Unknown/malformed/unsafe schema and store-wide uncertainty fail closed。
- Dangling reference、contradictory result and identity collision never appear as `NOT_FOUND` or READY。
- Candidate-B local READY is not whole-platform READY。

## AC-013-M — Restart and Recovery

- Close/reopen preserves all committed B-owned truth and same-result replay。
- Recovery enumeration completely exposes terminal captures/failures、bindings、Materiality and boundary facts or fails closed。
- Restart does not reset identity、delete contradiction or grant retry budget。
- Candidate B enumeration does not schedule, sleep, create RCA Attempt or promote Current。

## AC-013-N — Concurrency

- Concurrent equivalent same-operation requests produce at most one terminal outcome。
- Concurrent contradictory same-operation requests fail closed without last-write-wins。
- Concurrent same-Incident captures retain distinct Snapshots and deterministic Revision consistency。
- Concurrent equivalent Materiality requests converge to one result。

## AC-013-O — Cross-domain Handoff

- Candidate A consumes exact Snapshot/Revision references without copying B authority。
- Candidate E supplies explicit baseline and composes initial/refresh/post-context flows。
- SPEC-011 supplies authoritative time and owns retry/wake-up/startup orchestration。
- B recovery/post-context facts never become Runtime work authority。

## AC-013-P — Security and Ground-truth Isolation

- Production capture/selector/Revision/Materiality does not use scenario、generator、validator、expected-answer or Ground Truth data。
- Selector allowlist/escaping blocks query injection。
- Redaction occurs before persistence and LLM handoff。
- Credentials、tokens、Authorization headers and secret-bearing query material are absent from B authority and telemetry。
- Uncertain redaction yields typed invalid/fail-closed behavior under the source admission matrix。

## AC-013-Q — Required End-to-End Semantic Flows

- Flows A～H in section 17 are each validated with observable identities、statuses、bindings and ownership boundaries。
- Degraded and trusted-core failure flows validate immediate terminal failure、non-terminal retry、SPEC-011-owned bounded retry/exhaustion、public finalization handoff and exactly-one terminal result for the same `capture_operation_id`。
- Initial、degraded、trusted-core failure、response-loss replay、Revision reuse、refresh Materiality、post-context and restart recovery do not depend on process memory or private cross-domain persistence。

---

# 22. Deferred Implementation Choices and Out of Scope

## 22.1 Deferred Implementation Choices

The following are intentionally not frozen：

- physical SQLite schema；
- table、column and index names；
- exact SQL and transaction statement；
- Python class、module and package names；
- exact SQLite library/driver；
- lock、mutex、CAS and connection strategy；
- exact hash algorithm and serialization library；
- identity prefix/UUID/encoding；
- HTTP library；
- LogQL builder implementation；
- PromQL builder implementation；
- pagination mechanism；
- cache and performance tuning；
- test file/fixture layout；
- exact configurable numeric evidence limits not frozen upstream；
- migration tool implementation；
- Docker volume/topology。

These choices must not weaken authority、identity、immutability、atomicity、replay、status、completeness、provenance、Materiality、integrity、recovery or security semantics。

## 22.2 Explicit Out of Scope

- production implementation and tests；
- RCA Aggregate/Attempt/Try/Version/Artifact/Current/publication；
- RAG、Knowledge Snapshot or Knowledge Index；
- LLM generation、prompting、validation or provider integration；
- Incident RCA relationship mutation；
- Runtime work records、scheduler、retry budget、wake-up and startup ordering；
- whole-platform readiness；
- production retention/archive deletion policy；
- automatic destructive repair or silent migration；
- UI、Dashboard、Jira、Discord、Email or PRD-005 presentation。

## 22.3 Traceability Summary

| Authority / Decision | SPEC-013 Sections |
|---|---|
| D1 Trusted Core | 5, 7, 19, 21-B |
| D2 Capture Identity / Terminal Outcome | 4, 5, 6, 14, 16, 20, 21-C/K/N |
| D3 Collection / Status / Completeness | 8, 9, 10, 11, 17, 21-E/F/G/H |
| D4 Pairwise Materiality | 5.5–5.6, 13, 17.9, 20.4, 21-J |
| D5 Persistence / Recovery / Handoff | 14, 15, 16, 17, 20, 21-K/L/M/O |
| PRD-004 Security / Ground Truth | 18, 19, 21-P |
| SPEC-008 Incident Authority | 2.3–2.4, 7, 17, 21-A/O |
| SPEC-011 Runtime Authority | 2.2–2.4, 8.5, 16.3, 17.3, 21-M/O |
| SPEC-012 Candidate-A Baseline | 2.3–2.4, 13, 17.1/17.9, 21-J/O |

---

# 23. Approval Gate

This v1.0 Engineering Contract is Approved — Implementation Pending as of 2026-09-21。Approval does not claim implementation、integration、verification or Production Ready；those require separately authorized implementation and verification phases。
