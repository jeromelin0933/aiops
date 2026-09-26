# High-memory, OOM, and Service-crash Handling

## Governance / Approved Revision

- Logical Document Identity: `ops.high-memory-oom-service-crash-handling`
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

Provide a bounded response for memory pressure, out-of-memory termination, and related process or service crashes. The scope includes service processes or containers, their configured memory boundary, restart behavior, workload pressure, and resulting service availability.

This approved Version 1.0 revision does not assume that every crash is caused by memory exhaustion. It excludes fixed production limits, vendor-specific commands, uncontrolled memory dumps, customer payload inspection, and permanent capacity or runtime changes without review.

## Observable Symptoms and Evidence

Potential signals include:

- working-set, resident-memory, heap, or container-memory growth outside an approved baseline;
- allocation failures or an operating environment reporting an out-of-memory termination;
- repeated process restarts, crash loops, or readiness loss;
- increased garbage-collection, allocation, swap, or memory-reclaim pressure where those signals apply;
- request failures, latency, or upstream errors coinciding with process instability.

Collect process lifecycle events, resource metrics, runtime diagnostics, deployment/configuration events, traffic shape, and dependency behavior through approved interfaces. Diagnostic artifacts can contain secrets or customer data; capture them only under an authorized handling process.

High memory alone is not proof of a leak. Expected caching, traffic growth, batch work, dependency stalls, allocation bursts, or an unrelated crash can present similarly.

## Prerequisites and Safety Checks

- Confirm which process or container owns the observed memory and which service instances are affected.
- Obtain the configured resource boundary and alert thresholds from approved configuration or monitoring.
- Verify that restart, scaling, traffic shifting, or diagnostic capture is authorized and reversible.
- Preserve enough healthy capacity to avoid turning a local failure into a wider outage.
- Do not collect heap, core, or memory dumps unless their sensitive-data controls and retention are approved.
- Identify stateful work, in-flight operations, and data-integrity risks before terminating a process.

## Diagnosis and Verification

1. Align memory, restart, availability, traffic, deployment, and dependency timelines.
2. Confirm whether the environment explicitly reports OOM termination, allocation failure, or another crash reason.
3. Determine whether memory rises steadily, spikes with a workload, stabilizes as cache, or remains normal before the crash.
4. Compare affected and healthy instances using approved aggregate metrics and equivalent workload conditions.
5. Check for retry storms, queue accumulation, oversized work units, dependency stalls, or recent code/configuration changes that could retain work in memory.
6. Inspect runtime diagnostics that are safe and already approved; avoid ad hoc dumps or invasive profiling on an unstable service.
7. Separate the trigger, contributing resource pressure, and user-visible effect. Record alternative explanations.
8. Treat a memory leak, capacity deficit, or defective release as a hypothesis until evidence distinguishes it.

Stop investigation and escalate if diagnostics risk exposing sensitive memory, destabilizing remaining capacity, or damaging stateful work.

## Containment and Remediation Guidance

Based on verified evidence and existing authorization:

- drain or shift traffic away from an unstable instance while maintaining healthy capacity;
- restart a stateless affected instance through the governed operational mechanism when restart safety and rollback are known;
- reduce or pause a verified high-memory workload using its approved control;
- scale within approved limits when capacity pressure is demonstrated and scaling does not mask a crash loop;
- roll back a correlated release or configuration through normal deployment control;
- suppress a confirmed retry or queue amplifier at its owning component.

Do not repeatedly restart without diagnosis, raise memory limits blindly, delete state, or capture unrestricted dumps. Permanent code, runtime, capacity, and resource-limit changes require normal review and validation.

## Recovery Verification

- Confirm affected instances remain running and ready through the approved observation window.
- Verify memory behavior is stable relative to workload and configured boundaries.
- Confirm restart frequency, allocation/OOM errors, request failures, and upstream error signals recover.
- Validate representative synthetic service checks and any state-integrity requirements.
- Ensure queues and retries are draining rather than transferring pressure to another component.
- Roll back containment if it causes capacity loss, state risk, or a wider failure.

## Escalation and Limitations

Escalate to service/runtime owners when crash reason is unclear, privileged diagnostics are needed, a leak or code defect is suspected, or approved capacity controls are insufficient. Escalate to data owners before actions that could interrupt stateful processing.

This runbook does not authorize sensitive-memory inspection, permanent resource changes, new infrastructure, or a declaration of root cause based only on high memory and a crash.

## Post-recovery Follow-up

- Preserve non-sensitive timelines, resource trends, process events, and action outcomes.
- Review allocation behavior, queue bounds, retry policy, capacity assumptions, and diagnostic coverage.
- Propose code, configuration, alert, or capacity changes through governed review and testing.
- Do not automatically update this runbook, monitoring detectors, or correlation policy.
- Convert reusable learning into independently authored generalized Knowledge; exclude incident-specific truth.
