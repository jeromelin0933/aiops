"""S2 public-boundary and initial RCA composition evidence."""
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

from incident_evidence.contracts import (CaptureCommand, CaptureTerminalKind, CaptureTerminalOutcome,
    MaterialityEvaluationKind, MaterialityRequest, MaterialityResult)
from incident_evidence.identity import capture_command_semantic_identity
from knowledge_index.contracts import KnowledgeReadResult, KnowledgeReadStatus, KnowledgeResolutionOutcome, RetrievalResolution
from rca_persistence.contracts import (AttemptLineage, GenerationAttempt, GenerationLifecycle,
    GenerationProvenance, RcaAggregate, RecoveryCandidate, RecoveryCandidateKind)
from runtime_orchestration.rca_continuation import CaptureCommandBasis, SqliteRcaContinuationStore, rca_child_operation_id, rca_root_id
from runtime_orchestration.rca_initial import InitialRcaDisposition, InitialRcaOrchestrator, InitialRcaRequest
from runtime_orchestration.contracts import RuntimeWorkKind
from runtime_orchestration.identity import runtime_work_id


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


@dataclass
class _Incident:
    incident_id: str
    anchor_event_id: str = "EVT-1"
    event_ids: tuple[str, ...] = ("EVT-1",)


@dataclass
class _Relationship:
    incident_id: str
    current_version_id: str | None = None


class _Incidents:
    def __init__(self, *, listed=True, present=True, relationship=True):
        self.listed, self.present, self.relationship = listed, present, relationship
    def validate_readiness(self): pass
    def list_correlation_views(self): return (_Incident("INC-1"),) if self.listed else ()
    def get_incident(self, incident_id): return _Incident(incident_id) if self.present else None
    def get_rca_relationship(self, incident_id): return _Relationship(incident_id) if self.relationship else None


class _A:
    def __init__(self): self.aggregate = None; self.admissions = 0; self._attempts = {}; self._by_id = {}
    def get_aggregate_by_incident(self, incident_id): return self.aggregate
    def get_current(self, aggregate_id): return None
    def validate_local_readiness(self): pass
    def enumerate_recovery_candidates(self):
        return tuple(RecoveryCandidate(RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION,
            attempt.lineage.aggregate_id, attempt_id=attempt.lineage.attempt_id) for attempt in self._by_id.values())
    def get_attempt_lineage(self, attempt_id):
        attempt = self._by_id.get(attempt_id)
        return None if attempt is None else SimpleNamespace(attempt=attempt)
    def create_or_discover_aggregate(self, request):
        self.aggregate = RcaAggregate(request.aggregate_id, request.incident_id); return self.aggregate
    def admit_attempt(self, request):
        existing = self._attempts.get(request.operation_id)
        if existing is not None:
            assert existing.lineage == request.lineage
            return existing
        self.admissions += 1
        attempt = GenerationAttempt(request.lineage, GenerationLifecycle.PENDING)
        self._attempts[request.operation_id] = attempt
        self._by_id[request.lineage.attempt_id] = attempt
        return attempt


class _B:
    def __init__(self): self.outcome = None; self.captures = 0; self.materiality_calls = 0; self.materiality = None
    def read_capture_outcome(self, operation_id): return self.outcome
    def capture_evidence(self, command):
        self.captures += 1
        self.outcome = CaptureTerminalOutcome(command.capture_operation_id, capture_command_semantic_identity(command), CaptureTerminalKind.SUCCESS, "snapshot-1", "revision-1")
        return self.outcome
    def resolve_snapshot(self, snapshot_id):
        return SimpleNamespace(snapshot_id=snapshot_id, revision_id="revision-1", incident_id="INC-1")
    def resolve_revision(self, revision_id):
        return SimpleNamespace(revision_id=revision_id, incident_id="INC-1")
    def compare_materiality(self, request):
        self.materiality_calls += 1
        if self.materiality is None:
            self.materiality = MaterialityResult("materiality:1", request, None, ("explicit NO_BASELINE initial evaluation",))
        assert self.materiality.request == request
        return self.materiality


class _C:
    def read_operation(self, key): return KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
    def resolve(self, request, limits):
        snapshot = SimpleNamespace(resolution=RetrievalResolution.NO_MATCH, snapshot_key=SimpleNamespace(value="knowledge-1"))
        return KnowledgeResolutionOutcome(RetrievalResolution.NO_MATCH, snapshot)


class _UnavailableC:
    def __init__(self): self.finalized = 0
    def read_operation(self, key): return KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
    def resolve(self, request, limits): return KnowledgeResolutionOutcome(RetrievalResolution.RETRIEVAL_UNAVAILABLE)
    def finalize_unavailable(self, request):
        self.finalized += 1
        snapshot = SimpleNamespace(resolution=RetrievalResolution.RETRIEVAL_UNAVAILABLE, snapshot_key=SimpleNamespace(value="knowledge-1"))
        return KnowledgeResolutionOutcome(RetrievalResolution.RETRIEVAL_UNAVAILABLE, snapshot)


def _request():
    basis = CaptureCommandBasis("INC-1", rca_root_id("INC-1"), "INITIAL", NOW, "SPEC-013/v1", "canon/v1", "policy/v1", "bounds/v1", ("collection:logs",), "bounds/v1")
    command = CaptureCommand(basis.capture_operation_id, "INC-1", NOW, "SPEC-013/v1", "canon/v1", "policy/v1", "bounds/v1", "bounds/v1")
    # Aggregate identity is asserted only after A creates/discovers it, so this
    # helper lets the test bind the immutable A lineage to the resulting ID.
    return basis, command


def _run(tmp_path, incidents=None, a=None, b=None, c=None, unavailable_finalization_request=None):
    incidents, a, b = incidents or _Incidents(), a or _A(), b or _B()
    basis, command = _request()
    aggregate_id = rca_child_operation_id(rca_root_id("INC-1"), "INC-1", "AGGREGATE_IDENTITY")
    lineage = AttemptLineage("attempt:1", aggregate_id, "snapshot-1", "revision-1", "knowledge-1", GenerationProvenance("provider-1", "model-1", "prompt-1", "config-1", "profile-1"))
    store = SqliteRcaContinuationStore(tmp_path / "runtime.sqlite3")
    retrieval = SimpleNamespace(operation_key=SimpleNamespace(value=rca_child_operation_id(rca_root_id("INC-1"), basis.capture_operation_id, "KNOWLEDGE_RETRIEVAL")))
    materiality = MaterialityRequest(MaterialityEvaluationKind.NO_BASELINE, "revision-1", "evidence-materiality-v1")
    work_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, rca_root_id("INC-1"))
    result = InitialRcaOrchestrator(incidents=incidents, candidate_a=a, candidate_b=b, candidate_c=c or _C(), continuations=store).run(InitialRcaRequest("INC-1", work_id, 4, basis, NOW, command, retrieval, object(), lineage, materiality, unavailable_finalization_request))
    return result, a, b, store


def test_initial_s2_converges_to_admitted_attempt_without_try_execution(tmp_path):
    result, a, b, store = _run(tmp_path)
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert result.attempt is not None and result.attempt.lifecycle is GenerationLifecycle.PENDING
    assert a.admissions == 1 and b.captures == 1 and b.materiality_calls == 1
    assert result.materiality is not None and result.materiality.judgement is None
    assert store.get(rca_root_id("INC-1")).execution_operation_id is None


def test_replay_uses_candidate_b_same_operation_and_candidate_a_idempotency(tmp_path):
    result, a, b, store = _run(tmp_path)
    assert result.disposition is InitialRcaDisposition.READY_FOR_TRY
    path = store.database_path
    store.close()
    reopened = SqliteRcaContinuationStore(path)
    result2 = InitialRcaOrchestrator(incidents=_Incidents(), candidate_a=a, candidate_b=b, candidate_c=_C(), continuations=reopened).run(_request_for_existing())
    assert result2.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert b.captures == 1 and a.admissions == 1 and b.materiality_calls == 2  # B/A same operations converge
    assert reopened.get(rca_root_id("INC-1")).capture_basis.snapshot_at == NOW


def _request_for_existing():
    # Separate helper is intentionally invalid for a new Runtime work identity.
    basis, command = _request()
    aggregate_id = rca_child_operation_id(rca_root_id("INC-1"), "INC-1", "AGGREGATE_IDENTITY")
    lineage = AttemptLineage("attempt:1", aggregate_id, "snapshot-1", "revision-1", "knowledge-1", GenerationProvenance("provider-1", "model-1", "prompt-1", "config-1", "profile-1"))
    retrieval = SimpleNamespace(operation_key=SimpleNamespace(value=rca_child_operation_id(rca_root_id("INC-1"), basis.capture_operation_id, "KNOWLEDGE_RETRIEVAL")))
    materiality = MaterialityRequest(MaterialityEvaluationKind.NO_BASELINE, "revision-1", "evidence-materiality-v1")
    return InitialRcaRequest("INC-1", runtime_work_id(RuntimeWorkKind.RCA_INITIAL, rca_root_id("INC-1")), 4, basis, NOW, command, retrieval, object(), lineage, materiality)


def test_incident_enumeration_and_fresh_read_contradiction_fails_closed(tmp_path):
    result, *_ = _run(tmp_path, incidents=_Incidents(listed=True, present=False, relationship=False))
    assert result.disposition is InitialRcaDisposition.REPAIR_REQUIRED


def test_reliable_absence_is_not_conflated_with_integrity_contradiction(tmp_path):
    missing, *_ = _run(tmp_path / "missing", incidents=_Incidents(listed=False, present=False, relationship=False))
    assert missing.disposition is InitialRcaDisposition.NOT_FOUND
    contradictory, *_ = _run(tmp_path / "bad", incidents=_Incidents(listed=False, present=False, relationship=True))
    assert contradictory.disposition is InitialRcaDisposition.REPAIR_REQUIRED


def test_retrieval_unavailable_requires_existing_public_terminal_finalization(tmp_path):
    unavailable = _UnavailableC()
    pending, *_ = _run(tmp_path / "pending", c=unavailable)
    assert pending.disposition is InitialRcaDisposition.KNOWLEDGE_UNAVAILABLE
    assert unavailable.finalized == 0
    finalized, a, _, _ = _run(tmp_path / "finalized", c=unavailable, unavailable_finalization_request=object())
    assert finalized.disposition is InitialRcaDisposition.READY_FOR_TRY
    assert unavailable.finalized == 1 and a.admissions == 1


def test_s2_module_uses_only_consumer_owned_public_protocols():
    import inspect
    import runtime_orchestration.rca_initial as module
    source = inspect.getsource(module)
    assert "SqliteIncidentStore" not in source
    assert "SqliteEvidenceStore" not in source
    assert "_transaction" not in source
