# S5/S2 controlled capture preparation — DRAFT

Authority: PM-AIOPS-COMP-P0-FINAL-DEMO-GATE-20261103.
Baseline: 8e3faca4b27a81400c26e4964436bde13aa3c643.
No runtime, observability deployment or capture is authorized here. PM's
2026-11-03 additional decision separately permitted one bounded build per model
using existing scripts/config/fixtures, without contract or threshold changes.
Both builds and existing loader/fixture validation succeeded; exact hashes and
limitations are preserved in the external controlled Handoff, never in Git.

`python -B scripts/prepare_demo_capture.py` only reads approved scenario input,
checks model file existence and prints a deterministic preparation proposal.
Exit 2 means BLOCKED. There is no execute, opt-in, store initialization or
network mode. Its commitment is not SPEC-013 effective configuration approval.

Implemented offline: exact parent/child/external-root binding, disjoint stores,
existing RuntimeConfig with rca=None, and existing SourceRequestPolicy extension
point selecting api_p95_latency_ms (milliseconds, 15s step, existing 2s request
timeout). S5 latency is context, never proof of a shared database interruption.
Default up policy, production capture selectors and all completeness rules are
unchanged. No new service/dependency labels or topology facts are manufactured.

## Required decisions / artifacts

1. Bind the controlled one-time model build evidence (PM conditional training
   approval, actual artifact hashes, scripts/config/fixture hashes and loader
   results). This is not approval to retrain or load arbitrary pickle files.
   SPEC-005 prerequisite handling explicitly does not auto-train or disable a
   detector. SPEC-004 enabled flags alone do not waive this scenario contract.
2. OBSERVABILITY_CONFIG_AMENDMENT_REQUIRED: shared
   docker/promtail/promtail-config.yml currently lacks JSON label extraction.
   SPEC-005 section 18.3 requires exact PM file approval. Proposed v1 demo
   deployment must bind the selected log file and real service/dependency
   fields. This document does not apply a shared configuration change.
3. SPEC-013 selector decision: service_name=multiple is aggregate Event metadata,
   not a real log service. Approve a narrowly bounded, provenance-bearing
   aggregate selector rule before changing capture_plan or allowlists. Do not
   drop all selectors. Metric gauges have no service/dependency labels; approve
   their global contextual meaning / bounded query policy before execution.
4. Review a small capture-only composition of existing Generator, Detection,
   rca-disabled Runtime and EvidenceCaptureService. Do not use the initial RCA
   coordinator. Existing capture/public-read APIs remain authoritative.
   Runtime retry configuration is retained, not silently bypassed: the later
   bounded controller must stop on first failure before retry work can run.
   The current preparation module does not provide that execution controller.
   Characterization tests now reproduce a typed retryable intake exception
   being caught inside run_cycle, followed by eligible retry execution before
   the outer caller regains control. Config retry.limit=0 is rejected. Therefore
   an outside STOP wrapper or logger callback is not a complete guarantee.
   Required narrow decision: approve a capture-only first-failure notification
   and stop check BEFORE later intake/retry/pending work in the existing core,
   preserving default Runtime recovery/retry semantics. No scheduler rewrite.
   This worker/factory contract change has NOT been applied or approved here.
   Per the explicit C3 STOP boundary, execution-controller implementation is
   stopped at this contract gap. The two new characterization tests pass by
   demonstrating the gap, not by demonstrating successful capture or STOP.

No new Scenario/Event/Detector semantics, Knowledge store, diagnostic ledger,
retry scheduler or snapshot format is introduced. Narrow decisions must precede
any production selector or infrastructure edits. The current proposal is NOT
READY — DO NOT EXECUTE CAPTURE.

## Later execution boundary (not implemented / not authorized)

Parent competition-capture-20261104-01; children -s5 and -s2, each under the
fixed external Handoffs root. One trigger and one selected Incident capture per
child; proposed cap 600s/child and 1200s total requires Human approval. Exact
model/config/endpoint commitments and a clean source checkpoint remain pending.
No provider or activation objects may be constructed. Actual CaptureCommand
must bind effective_capture_config_identity and versions, actual Incident ID
and snapshot time. Require successful public resolve_snapshot/resolve_revision.
Any missing input, ambiguity, timeout, invalid/partial source or persistence
failure stops; no automatic retry or invented source status.

Record actual child-local log append boundaries, unique Events, Incident
membership/terminal ownership, operation receipts, timestamps and public IDs.
Scenario expected counts are never observations. Raw metric sample count and
combined raw-log/metric reduction denominator remain NOT AVAILABLE.

## 2026-11-04 implementation addendum (current status)

The earlier prerequisite/characterization paragraphs above preserve the pre-patch
history. PM approved exact patch 82ce42c02107e48fec1dfaa2353d2efb2417c6c046d9d4374d7efc1a6fd74773.
The four Runtime source files now implement that exact stop hook; SPEC-011 has
the matching candidate addendum, still subject to SPEC Lead status review.
Human applied the two protected files; Codex only verified their approved hashes.
Deployment, child-local read-only mounts and positions compatibility are not yet
verified. No Docker process was started or reconfigured by this implementation.

`capture_controller.py` implements a bounded approval-envelope verifier using an
independently supplied Human publication hash/issuer/revision pin, independent
policy-record pins, current byte binding, validity and exact opt-in phrase.
The pin must be supplied through Human-controlled publication, never extracted
from the untrusted record. It is not a new registry or cryptographic proof of a
Human identity. Record publication, clean-source attestation and deployment
evidence remain pending. No record/reference in unit tests is real authorization.

An internal safe-boundary composition uses existing scenario/detection/runtime
handles and EvidenceCaptureService plus resolve_snapshot/resolve_revision. Its
isolated fixtures prove interface wiring only. It preserves a one-shot intent,
rejects repeated children, and records partial outcomes without deleting work.
The public execute_capture entry always refuses: MetricsGenerator.start_exporter
discards the server/thread handles; local file and SQLite I/O have no established
end-to-end hard stop bound. A loop deadline only checks returned operations and
cannot cancel a blocked call. EventDetectionRunner also returns pipeline failure
facts at cycle boundaries; no stronger intra-detector stop guarantee is claimed.
No production component factory or capture execute CLI is exposed while blocked.

`python -B scripts/prepare_demo_capture.py --controller-readiness` is read-only;
exit 2 still means BLOCKED. Structured trace, membership-service OR selectors
and global latency context remain SPEC Lead approval pending. No SPEC-013 adapter
or selector semantics have been changed. Source/config/input/model/policy/store
bindings, final cycle/time/I/O/resource limits and opt-in require final review
and Human Git checkpoint before any separate Capture Execution Authorization.

## D3 safety implementation - 2026-11-04

MetricsGenerator now retains its existing server/thread pair and exposes explicit
owned_exporter/stop_exporter/close lifecycle. Default metric names, values,
labels, threshold inputs and bind behavior are unchanged. Cleanup timeout bounds
the caller's wait, not cancellation: incomplete shutdown retains ownership and
blocks restart. Isolated loopback port-release tests are not Docker/Capture PASS.

The previous internal composition driver has been moved to the tests harness;
the production internal entry now denies execution, just like execute_capture.
Local intent/report helpers require a binding minted by the approval verifier.
Neither a binding dataclass nor that verifier's token grants execution permission.

evidence_compatibility.py performs read-only inspection via existing trusted-core
admission, not a new authoritative membership store. It records canonical public
Incident/Event commitments, concrete services (deduplicated/sorted, maximum 8)
and an unambiguous dependency. Aggregate/missing/conflicting membership is a
typed failure. It does not encode OR into v1's exact scalar SelectorFact.

Structured Trace and GLOBAL metric outputs are readiness views only. The current
wire/projection cannot represent parser coverage, bounded OR or explicit GLOBAL
scope with original selectors preserved. Exact SPEC-013 amendment is pending;
legacy adapters, DTOs and snapshot identity algorithms have not been changed.

Controller safety remains BLOCKED: local synchronous reads/writes/SQLite commit
have no proven whole-operation deadline. A busy timeout only addresses lock
waiting; HTTP timeout is not a total capture/process deadline. No force-kill,
new scheduler or silent softening of this requirement has been introduced.
Factory/cadence wiring remains withheld under the explicit prerequisite gate.


## SPEC-013 D3 source implementation - 2026-11-05

This dated entry supersedes the earlier D3 contract-pending readiness notes only
for the exact amendment approved on 2026-11-05 (SHA-256
40932e399b4bc934d14596f25bac150a669ac1fb10a4b5d6f5bf613cd2be70a8).
It does not unblock Controller execution or authorize Capture.

`incident_evidence.demo_v2` supplies parallel typed requests, bounded membership
OR compilation, a single N+1 raw Loki fetch followed by completeness admission,
client JSON classification/exact trace filtering, and explicit GLOBAL latency
context. Legacy adapters and the default capture configuration stay v1.
`EvidenceCaptureService(..., demo_v2=binding)` explicitly requires both v2 adapters;
its effective config includes the pinned policy record, publication hash and
versioned canonical/source semantics. Public membership comes from existing
trusted-core Incident/Event admission, never scenario labels or SOP metadata.
Missing independent trace, membership, dependency, policy or coverage proof fails
closed; the OR capability does not manufacture a trace for S5.

The Human publication owner must independently provide the policy record bytes,
expected publication SHA-256 and clean source revision to
`DemoPolicyBinding.from_published_record`. The expected hash must come from the
controlled publication channel, not the supplied untrusted record itself.
This is an integrity pin at that trust boundary, not a signature, a registry,
proof of Human identity, Controller approval or Capture permission. The record
binds exact version/decision/issuer role/reference/scope/source/SPEC patch,
validity, endpoint SHA-256 values, and coverage evidence. Endpoint identity is
SHA-256 of the exact configured endpoint string, without credentials.

Loki COMPLETE additionally requires real, applicable enumeration-contract and
deployment-config commitments/reference, an effective backend limit >= N+1,
indexed-label capability, exhaustive/all-stream enumeration, no silent caps,
observable warnings and compatible actual response facts. Those attestations
must be verified by the publication owner; test records are synthetic and prove
no deployment fact. A low row count alone cannot establish completeness.
UNKNOWN/INCOMPLETE coverage admits no records or legal EMPTY. Real deployment
and enumeration evidence remain PENDING; no extra fetch, paging or retry exists.

GLOBAL api_p95_latency_ms retains original predicates separately from applied
empty predicates, ms/15s/GLOBAL/CONTEXTUAL_ONLY/strong_evidence_eligible=false,
original labels/samples and omission/source/query facts. It proves no core-db
fault. A canonical reader must explicitly accept `spec013-demo-evidence-v2`:
`resolve_snapshot(id, accepted_demo_version=VERSION)` and likewise Revision.
The configured v2 capture service supplies this acceptance. Default readers and
unadapted materiality/LLM consumers reject v2 rather than drop scope/strength.
Read acceptance does not grant execution permission. Recovery and identity
algorithms are retained; there is no store migration.

Implementation is pending independent audit. Mock/offline regression is not real
Loki/Prometheus deployment integration. Controller remains BLOCKED under Option A;
no execute CLI/factory bypass, actual Capture, Query, LLM or Activation is enabled.
