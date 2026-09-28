"""Candidate-D public A/B/C flow and handoff without upstream mutation."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from llm_generation.contracts import FailureClass, GenerationFailure, LocalReadStatus
from llm_generation.facade import CandidateDHandoffFacade
from llm_generation.service import (
    GenerationService, same_try_reinvocation_eligible, subject_input_commitment,
)
from llm_generation.sqlite_store import CandidateDStore
from rca_persistence.contracts import (
    AdmitAttemptRequest, AdmittedRetryDisposition, AttemptLineageRead,
    CreateAggregateRequest, LogicalTryOutcome, LogicalTryResultKind,
)
from rca_persistence.sqlite_store import SqliteRcaStore
from test_llm_generation_phase2 import case
from test_llm_generation_service import environment


NOW = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)


@pytest.fixture
def integrated(case, tmp_path):
    """Synthetic authority setup; D receives only public semantic readers."""
    a = SqliteRcaStore(tmp_path / "candidate_a.sqlite")
    lineage = case.attempts.get_attempt_lineage("attempt-1").attempt.lineage
    a.create_or_discover_aggregate(CreateAggregateRequest(
        "aggregate-setup", lineage.aggregate_id, "incident-1", NOW,
    ))
    a.admit_attempt(AdmitAttemptRequest("attempt-setup", lineage, NOW))
    case.attempts = a
    service, fake, grant, resources = environment(case, tmp_path / "candidate_d")
    facade = CandidateDHandoffFacade(service.store, a, case.evidence, case.knowledge)
    try:
        yield SimpleNamespace(case=case, a=a, service=service, fake=fake,
                              grant=grant, resources=resources, facade=facade)
    finally:
        a.close()


def test_full_fake_flow_projects_without_a_handoff(integrated):
    flow = integrated
    source = flow.case.content.input
    receipt = flow.service.execute(source, flow.case.config, flow.grant, flow.resources)
    assert receipt.status is LocalReadStatus.FOUND
    assert receipt.result is not None and len(flow.fake.calls) == 1
    handoff = flow.facade.resolve(receipt.result.validated_result_id)
    assert handoff.status is LocalReadStatus.FOUND
    assert handoff.result == receipt.result
    assert handoff.artifact is not None
    assert handoff.artifact.provenance.evidence_snapshot_id == source.evidence_snapshot_id
    assert handoff.artifact.provenance.knowledge_snapshot_id == source.knowledge_snapshot_id
    assert flow.a.get_attempt_lineage(source.try_identity.attempt_id).try_outcomes == ()
    assert flow.service.store.recovery().value.results[0].validated_result_id == receipt.result.validated_result_id


def test_crash_before_handoff_replays_same_d_truth_and_projection(integrated):
    flow = integrated
    source = flow.case.content.input
    first = flow.service.execute(source, flow.case.config, flow.grant, flow.resources)
    before = flow.facade.resolve(first.result.validated_result_id)
    reopened_store = CandidateDStore(flow.service.store.path,
                                      resource_config_path=flow.service.store.resource_config_path)
    restarted = GenerationService(reopened_store, flow.fake, flow.a,
                                  flow.case.evidence, flow.case.knowledge)
    replay = restarted.execute(source, flow.case.config, flow.grant, flow.resources)
    after = CandidateDHandoffFacade(reopened_store, flow.a, flow.case.evidence,
                                    flow.case.knowledge).resolve(first.result.validated_result_id)
    assert replay.result == first.result
    assert replay.physical_invocations == replay.token_preflight_calls == 0
    assert len(flow.fake.calls) == 1
    assert after.status is LocalReadStatus.FOUND
    assert after.result == before.result and after.artifact == before.artifact
    assert flow.a.get_attempt_lineage(source.try_identity.attempt_id).try_outcomes == ()


def test_handoff_fails_closed_on_fresh_public_lineage_contradiction(integrated):
    flow = integrated
    source = flow.case.content.input
    result = flow.service.execute(source, flow.case.config, flow.grant, flow.resources).result
    view = flow.a.get_attempt_lineage(source.try_identity.attempt_id)
    bad_lineage = replace(view.attempt.lineage, evidence_revision_id="wrong-revision")
    bad_a = SimpleNamespace(get_attempt_lineage=lambda _: AttemptLineageRead(
        replace(view.attempt, lineage=bad_lineage), view.try_outcomes,
    ))
    assert CandidateDHandoffFacade(flow.service.store, bad_a, flow.case.evidence,
                                   flow.case.knowledge).resolve(result.validated_result_id).status is LocalReadStatus.REPAIR_REQUIRED
    bad_b = SimpleNamespace(
        resolve_snapshot=lambda _: None,
        resolve_revision=flow.case.evidence.resolve_revision,
    )
    assert CandidateDHandoffFacade(flow.service.store, flow.a, bad_b,
                                   flow.case.knowledge).resolve(result.validated_result_id).status is LocalReadStatus.REPAIR_REQUIRED
    from knowledge_index.contracts import KnowledgeReadResult, KnowledgeReadStatus
    bad_c = SimpleNamespace(
        read_snapshot=lambda _: KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND),
        read_provenance=flow.case.knowledge.read_provenance,
    )
    assert CandidateDHandoffFacade(flow.service.store, flow.a, flow.case.evidence,
                                   bad_c).resolve(result.validated_result_id).status is LocalReadStatus.REPAIR_REQUIRED
    assert len(flow.fake.calls) == 1


def test_same_try_fresh_a_read_and_d_only_readiness(integrated):
    flow = integrated
    source = flow.case.content.input
    assert not same_try_reinvocation_eligible(source, flow.service.store, flow.a)
    failure = GenerationFailure(source.try_identity, source.operation_id,
                                FailureClass.PROVIDER_TIMEOUT,
                                AdmittedRetryDisposition.RETRYABLE, "timeout",
                                subject_input_commitment(source), True)
    assert flow.service.store.commit_failure(failure).status is LocalReadStatus.FOUND
    assert same_try_reinvocation_eligible(source, flow.service.store, flow.a)
    assert len(flow.fake.calls) == 0
    outcome = LogicalTryOutcome(source.try_identity, LogicalTryResultKind.FAILURE,
                                AdmittedRetryDisposition.RETRYABLE, NOW,
                                failure_code="PROVIDER_TIMEOUT")
    flow.a.record_try_outcome("a-test-outcome", outcome)
    assert not same_try_reinvocation_eligible(source, flow.service.store, flow.a)
    assert flow.service.store.local_readiness() is LocalReadStatus.FOUND
    recovery = flow.service.store.recovery()
    assert recovery.status is LocalReadStatus.FOUND and recovery.value.failures == (failure,)
    assert not hasattr(recovery.value, "artifact_committed")
    assert not hasattr(recovery.value, "publication_complete")


def test_ground_truth_and_canonical_public_type_identity(integrated):
    import llm_generation.service as generation_service
    import rca_persistence.contracts as a_contracts
    from llm_generation.contracts import GenerationInput

    flow = integrated
    assert generation_service.LogicalTryIdentity is a_contracts.LogicalTryIdentity
    assert isinstance(flow.case.content.input, GenerationInput)
    with pytest.raises(ValueError):
        replace(flow.case.content.input, evidence_projection="expected_answer=S1")
    assert len(flow.fake.calls) == 0


def test_provider_outage_does_not_change_local_readiness(integrated):
    from llm_generation.service import ProviderInvocationError, ProviderFailureKind

    flow = integrated
    flow.fake.outcome = ProviderInvocationError(ProviderFailureKind.UNAVAILABLE)
    receipt = flow.service.execute(flow.case.content.input, flow.case.config,
                                   flow.grant, flow.resources)
    assert receipt.failure.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert flow.service.store.local_readiness() is LocalReadStatus.FOUND
    assert len(flow.fake.calls) == 1


def test_a_failure_after_d_commit_blocks_handoff_projection(integrated):
    flow = integrated
    source = flow.case.content.input
    result = flow.service.execute(source, flow.case.config,
                                  flow.grant, flow.resources).result
    flow.a.record_try_outcome("a-conflicting-outcome", LogicalTryOutcome(
        source.try_identity, LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.NON_RETRYABLE, NOW,
        failure_code="A_TERMINAL_FAILURE",
    ))
    handoff = flow.facade.resolve(result.validated_result_id)
    assert handoff.status is LocalReadStatus.REPAIR_REQUIRED
    assert handoff.artifact is None
    assert len(flow.fake.calls) == 1
