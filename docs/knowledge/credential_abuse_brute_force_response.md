# Credential Abuse and Brute-force Response

## Governance / Approved Revision

- Logical Document Identity: `ops.credential-abuse-brute-force-response`
- Document Version: `1.0`
- Knowledge Type: `SOP`
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

Approval does not make every retrieval `SOP_BACKED`. That semantic requires this approved, active, production-eligible SOP revision, its granted Guidance Authority, and a versioned applicability policy that evaluates it as applicable. Similarity, rank, or `MATCH` alone cannot grant `SOP_BACKED` authority.

## Purpose / Operational Scope / Exclusions

Provide a bounded response procedure for suspected credential abuse or repeated authentication attempts affecting authentication endpoints and account-protection controls. The procedure covers evidence collection, containment, recovery verification, and escalation while limiting unnecessary account or service disruption.

This approved Version 1.0 revision applies only when approved telemetry indicates an authentication-abuse pattern within the responder's authorized scope. It does not establish that credentials were compromised, identify an attacker, prescribe organization-specific legal action, or replace a separately governed security-incident process.

It excludes credential contents, real account identifiers, customer data, fixed production thresholds, scenario mappings, and incident-specific conclusions.

## Observable Symptoms and Evidence

Potential signals include:

- repeated authentication failures or unauthorized responses;
- an unusual concentration of attempts against one account class or authentication endpoint;
- attempts distributed across multiple accounts or client sources;
- account-protection, challenge, or lockout controls activating more often than their approved baseline;
- authentication latency or availability degradation coinciding with elevated attempt volume.

Before acting, confirm the pattern with approved aggregate metrics and security/audit records. Record the observation window, affected endpoint or account class, signal source, and comparison baseline. Do not copy credentials, authorization headers, session tokens, or unnecessary personal data into operational notes.

No individual signal proves brute force or compromise. Planned tests, client defects, expired credentials, user error, or upstream retry behavior can produce similar symptoms.

## Prerequisites and Safety Checks

- Confirm authorization to inspect authentication telemetry and operate the relevant protection controls.
- Use only approved monitoring and security interfaces; do not test with real credentials.
- Identify service owners and the escalation path before applying controls that could deny legitimate access.
- Determine whether emergency-access or safety-critical accounts require special handling under an approved policy.
- Preserve existing audit evidence and timestamps without exporting secrets or sensitive payloads.
- Establish rollback criteria for any temporary restriction.

If the available evidence contains secrets or data outside the responder's authorized scope, stop and use the approved security-handling path.

## Diagnosis and Verification

1. Validate that the signal is current and reproducible across more than one approved evidence source when possible.
2. Bound the affected surface by endpoint, account class, client/source category, time window, and protection-control state. Avoid identifying a real person unless the approved security process requires it.
3. Compare attempt volume, failure distribution, and response codes with the approved operational baseline. Use thresholds from governed monitoring or policy; do not invent a cutoff.
4. Check for benign explanations such as a deployment, authentication-provider degradation, credential rotation, client retry defect, or authorized test.
5. Determine whether the observed pressure is isolated, distributed, or causing service-wide resource exhaustion.
6. Form a provisional hypothesis and list contradicting evidence. Treat it as a working diagnosis, not incident fact.
7. If compromise indicators or material customer impact are suspected, hand off to the authorized security-incident process while continuing only approved availability protections.

Stop diagnosis when evidence is insufficient, access is unauthorized, or further probing could expose credentials or worsen availability.

## Containment and Remediation Guidance

Apply the least disruptive approved control that addresses the verified scope:

- tune or activate an already approved rate-limit, challenge, or abuse-protection policy for the affected scope;
- temporarily restrict a verified abusive source category when the control supports safe rollback and legitimate traffic has been assessed;
- protect service capacity through approved admission-control or traffic-management mechanisms;
- require account-specific protection only through the governed identity/security process;
- correct a confirmed client retry or credential-rotation defect through its owning team.

Do not broadly block traffic, force credential resets, disable authentication controls, or alter lockout policy without the required human authority. Never place credentials into tickets, chat, logs, or this document.

## Recovery Verification

- Confirm that abusive-attempt signals and unauthorized-response pressure return toward the approved baseline.
- Verify authentication success and latency for representative legitimate synthetic checks.
- Confirm that temporary controls are operating only within the intended scope and are not causing unacceptable lockouts or denial of service.
- Check that audit evidence remains available and that no secret material was introduced into response artifacts.
- Observe for recurrence over the monitoring window defined by approved policy before removing temporary controls.
- Roll back containment if its measured harm exceeds its benefit, then escalate.

## Escalation and Limitations

Escalate to the authorized security and identity owners when compromise is suspected, protected accounts may be affected, evidence requires privileged access, or containment would materially affect legitimate users. Escalate to service or traffic owners when authentication availability is degrading.

This SOP cannot confirm attribution, credential compromise, legal impact, or incident root cause. It does not authorize access to raw secrets, account takeover testing, permanent policy changes, or outbound sharing of telemetry.

## Post-recovery Follow-up

- Preserve a non-secret timeline of signals, decisions, controls, and verification evidence.
- Review whether monitoring, client behavior, and protection policies behaved as intended.
- Propose policy or detector changes through their normal governed review; do not modify them automatically.
- If reusable learning is identified, author a separate generalized revision and submit it for human governance rather than copying incident-specific truth.
- Record deviations from the approved procedure for later human review.
