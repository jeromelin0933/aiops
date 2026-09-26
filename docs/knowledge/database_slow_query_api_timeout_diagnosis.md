# Database Slow-query and API-timeout Diagnosis

## Governance / Approved Revision

- Logical Document Identity: `ops.database-slow-query-api-timeout-diagnosis`
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

Provide an evidence-driven method to diagnose API timeouts that may involve database latency, application saturation, or another dependency in the request chain. The scope includes the database, application/API layer, gateway, and relevant downstream calls.

This approved Version 1.0 revision does not assume that a slow query is the root cause of every timeout. It does not prescribe a database engine, query, schema change, fixed latency threshold, or customer-specific topology. Destructive data operations and unreviewed production tuning are excluded.

## Observable Symptoms and Evidence

Potential signals include:

- increased request duration or timeout responses at the gateway or API;
- database query-duration, wait, lock, connection-pool, or resource-pressure signals outside an approved baseline;
- application workers or request queues remaining busy longer than expected;
- a timing correlation between a database operation and affected request paths;
- downstream latency that resembles database delay but originates elsewhere in the request chain.

Collect timestamps, request classes, aggregate traces, query fingerprints, pool metrics, database health signals, and deployment/configuration events through approved tools. Redact parameter values and payloads that could contain credentials or customer-sensitive data.

Correlation is evidence, not proof. A slow request can be caused by gateway limits, application contention, network delay, downstream services, or retry amplification.

## Prerequisites and Safety Checks

- Confirm read-only diagnostic access and ownership for each layer being inspected.
- Use approved query fingerprints or sanitized samples; do not copy sensitive parameter values.
- Establish the timeout policy and operational baselines from governed configuration or monitoring.
- Identify whether a failover, restart, query cancellation, or traffic shift requires separate authorization.
- Capture pre-change evidence and rollback criteria before applying containment.
- Avoid unrestricted diagnostic queries or profiling that could add material database load.

## Diagnosis and Verification

1. Establish the affected request class, observation window, user-visible symptom, and first layer reporting a timeout.
2. Trace the request path from gateway to application, database, and other dependencies using approved correlation identifiers or aggregate timing.
3. Compare time spent at each layer with its governed baseline and timeout budget. Do not infer causality from the slowest aggregate metric alone.
4. Inspect database evidence for slow query fingerprints, blocking or lock waits, connection-pool exhaustion, resource pressure, plan changes, or unavailable replicas.
5. Check application evidence for worker saturation, queue growth, connection leakage, retry loops, or a recent deployment/configuration change.
6. Check gateway, network, and non-database dependencies for competing explanations.
7. Reproduce only with approved synthetic or non-sensitive requests when safe; do not replay customer payloads.
8. State the leading hypothesis, supporting evidence, contradictions, and unresolved alternatives before containment.

If evidence cannot isolate the responsible layer, retain an unresolved diagnosis and escalate rather than labeling the database as factual root cause.

## Containment and Remediation Guidance

Choose a reversible action that matches verified evidence:

- reduce or shape affected traffic through an approved control when load is amplifying timeouts;
- stop an approved, clearly harmful workload or cancel work only when ownership and data-safety conditions are satisfied;
- shift to a healthy approved dependency path or replica when consistency and failover requirements permit;
- disable a confirmed retry amplifier or apply approved backoff at the owning client;
- roll back a correlated application or database change through its governed deployment process;
- optimize a verified query or resource configuration only through normal review, testing, and change control.

Do not perform schema changes, data deletion, unrestricted query cancellation, failover, or resource-limit changes solely because API timeouts and slow-query signals coincide.

## Recovery Verification

- Confirm request success, latency, and timeout rates against the approved baseline for the affected request class.
- Verify database waits, query duration, connection availability, and resource pressure have recovered without shifting failure elsewhere.
- Check gateway and application queues for drainage and absence of retry amplification.
- Validate representative synthetic transactions and required consistency behavior.
- Observe through the policy-defined recovery window and verify that containment rollback is safe.

## Escalation and Limitations

Escalate to database, application, gateway, or dependency owners when privileged diagnostics are required, data integrity may be at risk, failover is considered, or the responsible layer remains ambiguous. Escalate immediately when recovery actions could cause data loss or consistency violation.

This runbook cannot prove a query-level root cause without supporting evidence. It does not authorize production data access, schema changes, capacity procurement, or permanent timeout-policy changes.

## Post-recovery Follow-up

- Preserve sanitized evidence linking symptoms, hypotheses, decisions, and recovery checks.
- Review query plans, connection use, timeout budgets, retry behavior, and observability gaps through normal owner review.
- Propose tested performance or capacity changes through governed change control.
- Do not automatically alter this runbook, a detector, or correlation policy.
- Generalize reusable learning separately; do not promote incident-specific conclusions into Knowledge.
