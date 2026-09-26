# HTTP 429, Rate-limit, and Request-spike Handling

## Governance / Approved Revision

- Logical Document Identity: `ops.http-429-rate-limit-qps-spike-handling`
- Document Version: `1.0`
- Knowledge Type: `RUNBOOK`
- Status: `APPROVED`
- Lifecycle: `ACTIVE`
- Production Eligible: `YES`
- Approval Date: `2026-09-26`
- Approver Role: `SPEC Lead`
- Governance Reference: `SPEC014-KNOWLEDGE-V1-APPROVAL-20260926`
- Classification: `Approved Operational Knowledge`
- Security Classification: `Non-secret synthetic/mock operational knowledge`
- Outbound Eligibility: `APPROVED`
- Outbound Scope: Only the embedding/provider capability approved by SPEC-014; this is not permission for general Internet or public disclosure.
- Guidance Authority: `ELIGIBLE FOR SOP_BACKED`

Revision history: `0.1-draft` authoring candidate → content review PASS → SPEC Lead approval → Version `1.0`.

Approval does not make every retrieval `SOP_BACKED`. That semantic requires this approved, active, production-eligible RUNBOOK revision, its granted Guidance Authority, and a versioned applicability policy that evaluates it as applicable. Similarity, rank, or `MATCH` alone cannot grant `SOP_BACKED` authority.

## Purpose / Operational Scope / Exclusions

Provide an evidence-driven response for elevated HTTP 429 responses, rate-limit pressure, request/QPS spikes, and retry amplification. The scope includes the target or downstream API, rate-limiter or quota boundary, client traffic, and approved traffic controls.

This approved Version 1.0 revision does not define production quotas, fixed request thresholds, commercial limits, or customer-specific traffic policy. It does not assume that every 429 is caused by abusive traffic or insufficient capacity.

## Observable Symptoms and Evidence

Potential signals include:

- HTTP 429 responses increasing for a request class or client category;
- request rate, concurrency, or burstiness moving outside an approved baseline;
- quota or rate-limit counters reaching a governed policy boundary;
- retries increasing total traffic after throttling begins;
- latency, queueing, or downstream saturation coinciding with throttling;
- uneven impact across clients, operations, or dependency paths.

Collect aggregate request rates, response classes, limiter decisions, retry counts, concurrency, queue state, and dependency health from approved telemetry. Use sanitized client categories rather than real customer identifiers, authorization data, or request payloads.

A 429 can be an intentional healthy protection response. It does not by itself establish overload, abuse, misconfiguration, or insufficient capacity.

## Prerequisites and Safety Checks

- Identify the authority that owns quota, limiter, client, and traffic-management policy.
- Read current limits and thresholds from approved configuration or policy; do not invent values.
- Determine whether the affected operations are safe to retry and whether clients honor governed backoff behavior.
- Confirm rollback criteria before altering traffic shaping or temporary quota controls.
- Protect critical workflows according to approved prioritization; do not infer priority from traffic volume.
- Avoid collecting credentials, authorization headers, customer identifiers, or sensitive payloads.

## Diagnosis and Verification

1. Bound the observation window, responding component, affected operation, and sanitized client category.
2. Confirm where the 429 originates: edge, service, downstream provider, or another quota boundary.
3. Compare request rate, concurrency, burst pattern, and limiter decisions with governed policy and the approved baseline.
4. Separate organic demand, client retry amplification, batch or scheduled work, deployment effects, malformed client behavior, and dependency backpressure.
5. Inspect retry timing and fan-out to determine whether throttling is causing more requests.
6. Check service and downstream saturation signals to distinguish a protective limiter from a wrongly scoped or stale policy.
7. Compare affected and unaffected request classes without exposing client-sensitive data.
8. Record a provisional hypothesis and evidence gaps; do not treat high QPS or 429 count as factual root cause on its own.

Stop tests that add material traffic or could amplify throttling. Use approved synthetic traffic only within its governed bounds.

## Containment and Remediation Guidance

Apply the least disruptive approved control supported by evidence:

- reduce or stop a confirmed retry amplifier and use governed backoff and jitter behavior;
- shape, queue, or shed non-critical traffic through existing approved controls;
- pause or reschedule a verified batch source using its owning workflow;
- preserve priority traffic only according to approved policy;
- correct a confirmed limiter-scope or client-configuration change through governed rollback;
- scale within approved bounds when real capacity pressure is demonstrated and dependencies can support the additional load;
- coordinate with a downstream quota owner when throttling originates outside the service.

Do not disable protection globally, raise quotas without owner approval, encourage immediate retries, or transfer uncontrolled load to a dependency.

## Recovery Verification

- Confirm 429 responses, request rate, concurrency, retries, and queue state move toward approved baselines.
- Verify successful responses and latency for representative synthetic operations.
- Ensure recovery is not caused by silently dropping required work or starving an authorized traffic class.
- Check downstream capacity and quota state for transferred pressure.
- Remove temporary controls gradually while observing for rebound traffic.
- Verify clients continue to respect approved backoff after normal service resumes.

## Escalation and Limitations

Escalate to quota/limiter owners when policy interpretation or quota changes are required, to client owners when retry behavior is defective, and to dependency owners when external throttling persists. Escalate when priority or customer-impact decisions require authority not present in this runbook.

This runbook cannot determine traffic legitimacy, commercial entitlement, or capacity strategy from 429 and QPS signals alone. It does not authorize permanent quota changes, global limiter disablement, or customer-specific action.

## Post-recovery Follow-up

- Preserve sanitized traffic, limiter, retry, and action timelines.
- Review quota design, burst handling, retry bounds, fairness, capacity assumptions, and observability.
- Propose client, limiter, capacity, or traffic-policy changes through governed review and testing.
- Do not automatically modify this runbook, quota policy, detector, or correlation rules.
- Convert reusable learning into independently governed generalized Knowledge without incident-specific or customer-specific truth.
