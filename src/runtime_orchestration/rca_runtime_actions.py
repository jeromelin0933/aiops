"""Public-port RCA actions invoked by the existing Runtime worker.

The actions compose the already approved S2-S5 protocols.  They do not own a
clock, retry ledger, work store, or a second dispatch loop.
"""

from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping
import hashlib
import json

from incident_evidence import (
    CaptureCommand, CaptureTerminalKind, EvidencePolicy, EvidenceSnapshot,
    MaterialityEvaluationKind, MaterialityJudgement, MaterialityRequest,
)
from incident_evidence.identity import capture_command_semantic_identity
from knowledge_index import (
    CanonicalKnowledgeQuery, KnowledgeReadStatus, KnowledgeSnapshotKey,
    OpaqueExternalReference, OpaqueReferenceType, RetrievalOperationKey,
    RetrievalOperationRequest, RetrievalResolution,
)
from llm_generation.contracts import GenerationInput
from llm_generation.service import (
    AvailableResources, canonical_evidence_projection,
    canonical_knowledge_projection,
)
from rca_persistence import AttemptLineage, CurrentFreshness, GenerationProvenance, LogicalTryIdentity

from .contracts import RuntimeWorkKind
from .identity import runtime_operation_id, runtime_work_id
from .rca_continuation import (
    CaptureCommandBasis, FollowUpRequirement, RcaContinuationConcurrencyError,
    SqliteRcaContinuationStore, rca_child_operation_id,
    rca_root_id,
)
from .rca_execution import RcaExecutionDisposition, RcaExecutionRequest
from .rca_followup import FollowUpDisposition
from .rca_host import RcaRecoveryKind, RcaRecoverySubject
from .rca_initial import InitialRcaDisposition, InitialRcaRequest
from .rca_publication import PublicationRuntimeDisposition


class RcaRuntimeActions:
    """Concrete S6 bridge over public domain and Candidate-E semantic ports."""

    def __init__(self, *, incidents: object, candidate_a: object,
                 candidate_b: object, candidate_c: object, capture_service: object,
                 continuations: SqliteRcaContinuationStore,
                 initial: object, execution: object, follow_up: object,
                 publication: object, evidence_policy: EvidencePolicy,
                 evidence_config_identity: str, knowledge_config: object,
                 generation_config: object, retry_limit: int) -> None:
        self._incidents, self._a, self._b, self._c = (
            incidents, candidate_a, candidate_b, candidate_c)
        self._capture = capture_service
        self._d2 = continuations
        self._initial, self._execution = initial, execution
        self._follow_up, self._publication = follow_up, publication
        self._evidence_policy = evidence_policy
        self._evidence_config_identity = evidence_config_identity
        self._knowledge_config, self._generation_config = (
            knowledge_config, generation_config)
        self._retry_limit = retry_limit

    def reconcile_publication(self, operation_id: str, observed_at: datetime) -> object:
        receipt = self._a.get_publication_result(operation_id)
        if receipt is None or receipt.target.publication_operation_id != operation_id:
            raise ValueError("publication lacks exact public A target")
        inspected, _ = self._publication.inspect(receipt.target)
        if inspected.classification.value in ("TARGET_CONFLICT", "INCOHERENT_AUTHORITY"):
            raise ValueError("publication target is contradictory")
        if self._d2.get(rca_root_id(receipt.target.incident_id)) is not None:
            self._admit_initial_post_context(receipt.target)
        # With genuinely absent D2 continuity, S5 may still reconcile the
        # authoritative A/008 business receipt. Its typed bookkeeping-lost
        # result then keeps Runtime terminalization closed.
        result = self._publication.reconcile(receipt.target, observed_at)
        if result.disposition in (
            PublicationRuntimeDisposition.REPAIR_REQUIRED,
            PublicationRuntimeDisposition.RUNTIME_BOOKKEEPING_LOST,
        ):
            raise ValueError("publication reconciliation is not coherent")
        return result

    def _admit_initial_post_context(self, target: object) -> None:
        version = self._a.get_version(target.target_version_id)
        if (version is None or version.aggregate_id != target.aggregate_id
            or version.publication_operation_id != target.publication_operation_id):
            raise ValueError("publication Version cannot prove its Evidence basis")
        lineage_read = self._a.get_attempt_lineage(version.attempt_id)
        if lineage_read is None:
            raise ValueError("publication Attempt is unreadable")
        snapshot = self._b.resolve_snapshot(
            lineage_read.attempt.lineage.evidence_snapshot_id)
        incident = self._incidents.get_incident(target.incident_id)
        if not isinstance(snapshot, EvidenceSnapshot) or incident is None:
            raise ValueError("publication Evidence or Incident is unreadable")
        content = snapshot.snapshot_content
        post = content.get("post_context")
        events = content.get("event_projections")
        if not isinstance(post, Mapping) or not isinstance(events, list):
            raise ValueError("Candidate-B post-context facts are malformed")
        boundary = post.get("default_boundary")
        if not isinstance(boundary, str):
            raise ValueError("Candidate-B boundary is missing")
        try:
            boundary_at = datetime.fromisoformat(boundary.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Candidate-B boundary is invalid") from exc
        reached = post.get("reached_upper_boundary")
        if not isinstance(reached, Mapping) or set(reached) != {
            "LOKI", "PROMETHEUS"} or any(type(value) is not bool
                                          for value in reached.values()):
            raise ValueError("Candidate-B source boundary facts are malformed")
        event_ids = tuple(item.get("event_id") for item in events
                          if isinstance(item, Mapping))
        if len(event_ids) != len(events):
            raise ValueError("Candidate-B Event projections are malformed")
        if (snapshot.snapshot_at < boundary_at or
            not all(reached.values()) or
            set(event_ids) != set(incident.event_ids)):
            admitted = self._follow_up.admit(
                target.incident_id,
                FollowUpRequirement("POST_CONTEXT",
                                    "boundary:" + snapshot.snapshot_id))
            if admitted.disposition.value != "OUTSTANDING":
                raise ValueError("initial post-context obligation was not admitted")

    def advance(self, subject: RcaRecoverySubject, observed_at: datetime) -> bool:
        kind = subject.kind
        if kind is RcaRecoveryKind.INITIAL_INCIDENT:
            return self._advance_initial(subject.incident_id, observed_at)
        if kind in (
            RcaRecoveryKind.VALIDATED_D_RESULT,
            RcaRecoveryKind.RETRYABLE_TRY_FAILURE,
            RcaRecoveryKind.NON_RETRYABLE_TRY_FAILURE,
            RcaRecoveryKind.RETRY_PENDING,
            RcaRecoveryKind.RETRY_BUDGET_EXHAUSTED,
        ):
            return self._advance_execution(subject)
        if kind in (
            RcaRecoveryKind.FOLLOW_UP_PENDING,
            RcaRecoveryKind.STALE_CURRENT,
        ):
            return self._advance_follow_up(subject.incident_id, observed_at)
        return False

    @staticmethod
    def _snapshot_event_ids(snapshot: EvidenceSnapshot) -> tuple[str, ...]:
        events = snapshot.snapshot_content.get("event_projections")
        if not isinstance(events, list):
            raise ValueError("Candidate-B Event projections are unreadable")
        ids = tuple(item.get("event_id") for item in events
                    if isinstance(item, Mapping))
        if (len(ids) != len(events) or any(not isinstance(item, str) for item in ids)
            or len(set(ids)) != len(ids)):
            raise ValueError("Candidate-B Event projections are contradictory")
        return ids

    @staticmethod
    def _snapshot_boundary(snapshot: EvidenceSnapshot) -> datetime:
        post = snapshot.snapshot_content.get("post_context")
        if not isinstance(post, Mapping):
            raise ValueError("Candidate-B post-context authority is unreadable")
        raw = post.get("default_boundary")
        if not isinstance(raw, str):
            raise ValueError("Candidate-B post-context boundary is missing")
        try:
            boundary = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Candidate-B post-context boundary is malformed") from exc
        if boundary.tzinfo is None:
            raise ValueError("Candidate-B post-context boundary lacks timezone")
        return boundary

    def _snapshots(self, incident_id: str) -> tuple[EvidenceSnapshot, ...]:
        facts = self._b.enumerate_recovery_facts()
        if (facts.local_readiness.value != "READY" or
            facts.integrity_status.value != "VALID" or facts.repair_findings):
            raise ValueError("Candidate-B enumeration is incomplete")
        return tuple(item for item in facts.snapshots if item.incident_id == incident_id)

    def _capture_follow_up(self, incident: object, snapshot_at: datetime) -> EvidenceSnapshot | None:
        root = rca_root_id(incident.incident_id)
        while True:
            state = self._d2.get(root)
            if state is None:
                raise ValueError("follow-up capture lacks durable RCA continuity")
            if len(state.follow_up_capture_bases) > len(state.follow_up_capture_snapshots):
                break
            fresh = self._incidents.get_incident(incident.incident_id)
            if fresh is None or fresh != incident:
                raise ValueError("Incident changed before follow-up capture admission")
            policy = self._evidence_policy
            event_ids = tuple(incident.event_ids)
            if (not event_ids or len(set(event_ids)) != len(event_ids)
                or any(not isinstance(item, str) or not item for item in event_ids)):
                raise ValueError("follow-up capture Event authority is malformed")
            event_fingerprint = hashlib.sha256(
                json.dumps(sorted(event_ids), separators=(",", ":")).encode()).hexdigest()
            boundaries = tuple(sorted(member.reference for member in state.unresolved_frontier
                                      if member.requirement_type == "POST_CONTEXT"))
            references = ("collection:events-" + event_fingerprint,)
            if boundaries:
                references += ("collection:post-context-" + hashlib.sha256(
                    json.dumps(boundaries, separators=(",", ":")).encode()).hexdigest(),)
            proposed = CaptureCommandBasis(
                incident.incident_id, root, "FOLLOW_UP", snapshot_at,
                policy.capture_contract_version, policy.canonicalization_version,
                policy.source_policy_version, self._evidence_config_identity,
                references, policy.bounds_policy_version,
            )
            if proposed in state.follow_up_capture_bases:
                index = state.follow_up_capture_bases.index(proposed)
                snapshot = self._b.resolve_snapshot(state.follow_up_capture_snapshots[index])
                if (not isinstance(snapshot, EvidenceSnapshot) or
                    snapshot.capture_operation_id != proposed.capture_operation_id or
                    snapshot.snapshot_at != proposed.snapshot_at):
                    raise ValueError("completed follow-up capture is unreadable")
                return snapshot
            try:
                self._d2.freeze_capture_basis(root, proposed, follow_up=True,
                    observed_at=max(snapshot_at, state.updated_at, state.observed_at),
                    expected_revision=state.revision)
                break
            except RcaContinuationConcurrencyError:
                continue
        # Read back the complete frozen command; a concurrent admission never
        # permits substituting today's Incident, timestamp or configuration.
        basis = self._d2.require_capture_command_basis(root, follow_up=True)
        command = CaptureCommand(
            basis.capture_operation_id, incident.incident_id, basis.snapshot_at,
            basis.capture_contract_version, basis.canonicalization_version,
            basis.source_policy_version, basis.bounds_policy_version,
            basis.configuration_identity,
        )
        outcome = self._capture.read_capture_outcome(command.capture_operation_id)
        if outcome is None:
            policy = self._evidence_policy
            if (basis.capture_contract_version, basis.canonicalization_version,
                basis.source_policy_version, basis.bounds_policy_version,
                basis.configuration_identity) != (
                policy.capture_contract_version, policy.canonicalization_version,
                policy.source_policy_version, policy.bounds_policy_version,
                self._evidence_config_identity):
                raise ValueError("frozen follow-up capture configuration is unavailable")
            outcome = self._capture.capture_evidence(command)
        if getattr(outcome, "terminal_kind", None) is None:
            return None
        if (outcome.capture_operation_id != command.capture_operation_id or
            outcome.command_semantic_identity != capture_command_semantic_identity(command)):
            raise ValueError("Candidate-B follow-up outcome contradicts the frozen command")
        if outcome.terminal_kind is not CaptureTerminalKind.SUCCESS:
            raise ValueError("Candidate-B follow-up Capture failed terminally")
        snapshot = self._b.resolve_snapshot(outcome.snapshot_id)
        if (not isinstance(snapshot, EvidenceSnapshot) or
            snapshot.incident_id != incident.incident_id or
            snapshot.capture_operation_id != command.capture_operation_id or
            snapshot.snapshot_at != basis.snapshot_at or
            snapshot.revision_id != outcome.revision_id):
            raise ValueError("Candidate-B follow-up Capture is contradictory")
        self._snapshot_event_ids(snapshot)
        # Admit B-owned requirements before acknowledging this capture. A
        # crash in either step replays the same B receipt and E3 admissions.
        self._admit_snapshot_requirements(incident.incident_id, snapshot)
        while True:
            state = self._d2.get(root)
            index = state.follow_up_capture_bases.index(basis)
            if index < len(state.follow_up_capture_snapshots):
                if state.follow_up_capture_snapshots[index] != snapshot.snapshot_id:
                    raise ValueError("follow-up capture Snapshot continuity contradicts B")
                break
            try:
                self._d2.resolve_follow_up_capture(root, basis, snapshot.snapshot_id,
                    observed_at=max(basis.snapshot_at, state.updated_at, state.observed_at),
                    expected_revision=state.revision)
                break
            except RcaContinuationConcurrencyError:
                continue
        return snapshot

    def _admit_snapshot_requirements(
        self, incident_id: str, snapshot: EvidenceSnapshot,
    ) -> bool:
        changed = False
        boundary = self._snapshot_boundary(snapshot)
        post = snapshot.snapshot_content["post_context"]
        reached = post.get("reached_upper_boundary")
        if (not isinstance(reached, Mapping) or set(reached) != {"LOKI", "PROMETHEUS"}
            or any(type(value) is not bool for value in reached.values())):
            raise ValueError("Candidate-B source boundary facts are malformed")
        if snapshot.snapshot_at < boundary or not all(reached.values()):
            member = FollowUpRequirement("POST_CONTEXT",
                                         "boundary:" + snapshot.snapshot_id)
            before = self._d2.get(rca_root_id(incident_id))
            result = self._follow_up.admit(incident_id, member)
            if result.disposition is not FollowUpDisposition.OUTSTANDING:
                raise ValueError("post-context admission did not converge")
            changed |= result.continuation.revision != before.revision
        aggregate = self._a.get_aggregate_by_incident(incident_id)
        current = self._a.get_current(aggregate.aggregate_id) if aggregate else None
        if current is not None:
            version = current.version
            lineage = self._a.get_attempt_lineage(version.attempt_id)
            if lineage is None:
                raise ValueError("Current Attempt lineage is unreadable")
            if lineage.attempt.lineage.evidence_revision_id != snapshot.revision_id:
                member = FollowUpRequirement("MATERIAL_EVIDENCE",
                                             "revision:" + snapshot.revision_id)
                before = self._d2.get(rca_root_id(incident_id))
                result = self._follow_up.admit(incident_id, member)
                if result.disposition is not FollowUpDisposition.OUTSTANDING:
                    raise ValueError("Material Evidence admission did not converge")
                changed |= result.continuation.revision != before.revision
        return changed

    def _knowledge_for_snapshot(
        self, incident_id: str, snapshot: EvidenceSnapshot,
    ) -> object | None:
        root = rca_root_id(incident_id)
        request = RetrievalOperationRequest(
            RetrievalOperationKey(rca_child_operation_id(
                root, snapshot.capture_operation_id, "KNOWLEDGE_RETRIEVAL")),
            CanonicalKnowledgeQuery("1.0", "1.0", incident_id),
            self._knowledge_config.retrieval_profile,
            self._knowledge_config.applicability_policy,
            (OpaqueExternalReference(OpaqueReferenceType.CALLER, "spec016-runtime"),),
            self._knowledge_config.capability.profile_reference,
            self._knowledge_config.capability.capability_identity,
        )
        outcome = self._c.resolve(request, self._knowledge_config.limits)
        if outcome.resolution in (RetrievalResolution.INVALID,
                                  RetrievalResolution.REPAIR_REQUIRED):
            raise ValueError("Candidate-C follow-up retrieval is invalid")
        return outcome.snapshot

    def _advance_follow_up(self, incident_id: str, observed_at: datetime) -> bool:
        incident = self._incidents.get_incident(incident_id)
        if incident is None or incident.incident_id != incident_id:
            raise ValueError("follow-up Incident is unreadable")
        root = rca_root_id(incident_id)
        state = self._d2.get(root)
        if state is None:
            raise ValueError("follow-up root is absent")
        if len(state.follow_up_capture_bases) > len(state.follow_up_capture_snapshots):
            basis = self._d2.require_capture_command_basis(root, follow_up=True)
            return self._capture_follow_up(incident, basis.snapshot_at) is not None
        progress = False
        aggregate = self._a.get_aggregate_by_incident(incident_id)
        current = self._a.get_current(aggregate.aggregate_id) if aggregate else None
        if current is not None:
            baseline = self._a.get_attempt_lineage(current.version.attempt_id)
            if baseline is None:
                raise ValueError("Current Attempt lineage is unreadable")
            original = self._b.resolve_snapshot(
                baseline.attempt.lineage.evidence_snapshot_id)
            if not isinstance(original, EvidenceSnapshot):
                raise ValueError("Current Evidence Snapshot is unreadable")
            progress |= self._admit_snapshot_requirements(incident_id, original)
        snapshots = self._snapshots(incident_id)
        matching = tuple(item for item in snapshots
                         if set(self._snapshot_event_ids(item)) == set(incident.event_ids))
        if not matching:
            if incident.last_correlated_at > observed_at:
                raise ValueError("Incident correlation time is in the future")
            captured = self._capture_follow_up(incident, incident.last_correlated_at)
            if captured is None:
                return False
            progress |= self._admit_snapshot_requirements(incident_id, captured)
            snapshots = self._snapshots(incident_id)
            matching = tuple(item for item in snapshots
                             if set(self._snapshot_event_ids(item)) == set(incident.event_ids))
        state = self._d2.get(root)
        if state.next_eligibility_at is not None and observed_at >= state.next_eligibility_at:
            post_members = tuple(item for item in state.unresolved_frontier
                                 if item.requirement_type == "POST_CONTEXT")
            if post_members:
                applicable = max(self._snapshot_boundary(
                    self._b.resolve_snapshot(member.reference[len("boundary:"):]))
                    for member in post_members)
                if observed_at >= applicable and not any(
                    item.snapshot_at >= applicable for item in matching
                ):
                    captured = self._capture_follow_up(incident, applicable)
                    if captured is None:
                        return progress
                    progress |= self._admit_snapshot_requirements(incident_id, captured)
                    snapshots = self._snapshots(incident_id)
                    matching = tuple(item for item in snapshots
                                     if set(self._snapshot_event_ids(item)) ==
                                     set(incident.event_ids))
        state = self._d2.get(root)
        aggregate = self._a.get_aggregate_by_incident(incident_id)
        current = self._a.get_current(aggregate.aggregate_id) if aggregate else None
        if current is not None and current.current.freshness is CurrentFreshness.STALE:
            revision_id = current.current.material_evidence_revision_basis
            if revision_id is None:
                raise ValueError("STALE Current lacks B Revision basis")
            member = FollowUpRequirement("MATERIAL_EVIDENCE",
                                         "revision:" + revision_id)
            if member not in state.admitted_frontier:
                admitted = self._follow_up.admit(incident_id, member)
                if admitted.disposition is not FollowUpDisposition.OUTSTANDING:
                    raise ValueError("STALE basis could not be admitted")
                progress = True
        state = self._d2.get(root)
        for member in state.unresolved_frontier:
            if member.requirement_type != "MATERIAL_EVIDENCE":
                continue
            revision_id = member.reference[len("revision:"):]
            source = next((item for item in snapshots
                           if item.revision_id == revision_id), None)
            if source is None:
                raise ValueError("Material Evidence member lacks public B Snapshot")
            current = self._a.get_current(aggregate.aggregate_id)
            if current is None:
                raise ValueError("Material Evidence lacks Current baseline")
            baseline = self._a.get_attempt_lineage(current.version.attempt_id)
            if baseline is None:
                raise ValueError("Current Evidence baseline is unreadable")
            compared = self._capture.compare_materiality(MaterialityRequest(
                MaterialityEvaluationKind.PAIRWISE, revision_id,
                self._evidence_policy.materiality_rule_versions[0],
                baseline.attempt.lineage.evidence_revision_id,
            ))
            if compared.judgement in (MaterialityJudgement.SAME,
                                      MaterialityJudgement.NON_MATERIAL):
                checked = self._follow_up.recheck(incident_id)
                if checked.disposition is FollowUpDisposition.REPAIR_REQUIRED:
                    raise ValueError("non-material Evidence proof is contradictory")
                progress |= checked.continuation.revision != state.revision
                state = self._d2.get(root)
                continue
            if compared.judgement is not MaterialityJudgement.MATERIAL:
                raise ValueError("Candidate-B Materiality has no typed judgement")
            knowledge = self._knowledge_for_snapshot(incident_id, source)
            if knowledge is None:
                return progress
            pin = self._generation_config.pin
            lineage = AttemptLineage(
                rca_child_operation_id(root, revision_id, "ATTEMPT"),
                aggregate.aggregate_id, source.snapshot_id, source.revision_id,
                knowledge.snapshot_key.value,
                GenerationProvenance(
                    pin.provider, pin.model, pin.prompt.identity,
                    pin.configuration.identity, pin.profile.identity),
            )
            refreshed = self._follow_up.refresh(
                incident_id, member, evidence_snapshot_id=source.snapshot_id,
                knowledge_snapshot_id=knowledge.snapshot_key.value,
                lineage=lineage)
            if refreshed.disposition is FollowUpDisposition.REPAIR_REQUIRED:
                raise ValueError("Material Evidence refresh requires repair")
            if refreshed.disposition is FollowUpDisposition.ATTEMPT_ADMITTED:
                progress = True
                progress |= self._execute(incident_id, lineage.attempt_id)
            elif refreshed.disposition is FollowUpDisposition.SUPPRESSED:
                progress = True
        state = self._d2.get(root)
        coverage = max(matching, key=lambda item: item.snapshot_at) if matching else None
        mapping = ({
            member: coverage.snapshot_id
            for member in state.unresolved_frontier
            if member.requirement_type == "POST_CONTEXT"
        } if coverage is not None else {})
        checked = self._follow_up.recheck(
            incident_id, post_context_snapshots=mapping,
        )
        if checked.disposition is FollowUpDisposition.REPAIR_REQUIRED:
            raise ValueError("full follow-up frontier requires repair")
        return progress or checked.continuation.revision != state.revision

    def _advance_initial(self, incident_id: str, observed_at: datetime) -> bool:
        incident = self._incidents.get_incident(incident_id)
        if incident is None or incident.incident_id != incident_id:
            raise ValueError("initial RCA Incident is not readable")
        root = rca_root_id(incident_id)
        state = self._d2.get(root)
        basis = state.capture_basis if state is not None else None
        if basis is None:
            policy = self._evidence_policy
            basis = CaptureCommandBasis(
                incident_id, root, "INITIAL", observed_at,
                policy.capture_contract_version,
                policy.canonicalization_version,
                policy.source_policy_version,
                self._evidence_config_identity,
                ("collection:incident",),
                policy.bounds_policy_version,
            )
        command = CaptureCommand(
            basis.capture_operation_id, incident_id, basis.snapshot_at,
            basis.capture_contract_version, basis.canonicalization_version,
            basis.source_policy_version, basis.bounds_policy_version,
            basis.configuration_identity,
        )
        retrieval = RetrievalOperationRequest(
            RetrievalOperationKey(rca_child_operation_id(
                root, basis.capture_operation_id, "KNOWLEDGE_RETRIEVAL")),
            CanonicalKnowledgeQuery("1.0", "1.0", incident_id),
            self._knowledge_config.retrieval_profile,
            self._knowledge_config.applicability_policy,
            (OpaqueExternalReference(OpaqueReferenceType.CALLER, "spec016-runtime"),),
            self._knowledge_config.capability.profile_reference,
            self._knowledge_config.capability.capability_identity,
        )
        pin = self._generation_config.pin
        provenance = GenerationProvenance(
            pin.provider, pin.model, pin.prompt.identity,
            pin.configuration.identity, pin.profile.identity,
        )
        attempt_id = (state.attempt_id if state is not None and state.attempt_id
                      else rca_child_operation_id(root, basis.capture_operation_id,
                                                  "ATTEMPT"))
        result = self._initial.run(InitialRcaRequest(
            incident_id,
            runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root),
            self._retry_limit,
            basis,
            observed_at,
            command,
            retrieval,
            self._knowledge_config.limits,
            None,
            None,
            attempt_id=attempt_id,
            generation_provenance=provenance,
        ))
        if result.disposition is InitialRcaDisposition.READY_FOR_TRY:
            return self._execute(incident_id, attempt_id)
        if result.disposition is InitialRcaDisposition.REPAIR_REQUIRED:
            raise ValueError("initial RCA public protocol requires repair")
        return False

    def _advance_execution(self, subject: RcaRecoverySubject) -> bool:
        attempt_id = subject.attempt_id
        if attempt_id is None:
            state = self._d2.get(rca_root_id(subject.incident_id))
            attempt_id = state.attempt_id if state is not None else None
        if attempt_id is None:
            raise ValueError("RCA execution lacks Attempt identity")
        return self._execute(subject.incident_id, attempt_id)

    def _execute(self, incident_id: str, attempt_id: str) -> bool:
        view = self._a.get_attempt_lineage(attempt_id)
        if view is None:
            raise ValueError("RCA Attempt is not readable")
        lineage = view.attempt.lineage
        evidence = self._b.resolve_snapshot(lineage.evidence_snapshot_id)
        knowledge = self._c.read_snapshot(KnowledgeSnapshotKey(
            lineage.knowledge_snapshot_id))
        provenance = self._c.read_provenance(KnowledgeSnapshotKey(
            lineage.knowledge_snapshot_id))
        if (not isinstance(evidence, EvidenceSnapshot)
            or knowledge.status is not KnowledgeReadStatus.FOUND
            or provenance.status is not KnowledgeReadStatus.FOUND):
            raise ValueError("RCA execution basis is incomplete")
        source = GenerationInput(
            LogicalTryIdentity(attempt_id, 1),
            runtime_operation_id("RCA_D_EXECUTION", rca_root_id(incident_id),
                                 attempt_id, "1", "0"),
            lineage.evidence_snapshot_id,
            lineage.evidence_revision_id,
            lineage.knowledge_snapshot_id,
            knowledge.value.resolution,
            self._generation_config.pin,
            canonical_evidence_projection(evidence),
            canonical_knowledge_projection(knowledge.value, provenance.value),
        )
        bounds = self._generation_config.bounds
        resources = AvailableResources(
            bounds.maximum_rate_units, bounds.maximum_quota_units,
            bounds.maximum_cost_units, bounds.maximum_resource_units,
        )
        result = self._execution.step(RcaExecutionRequest(
            incident_id, attempt_id, source, self._generation_config,
            resources))
        if result.disposition is RcaExecutionDisposition.REPAIR_REQUIRED:
            raise ValueError("RCA Attempt execution requires repair")
        return result.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
