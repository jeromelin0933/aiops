"""Provider-neutral domain flow with a deterministic fake provider."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from llm_generation.contracts import FailureClass, GenerationFailure, InvocationMetadata, LocalReadStatus
from llm_generation.contracts import VersionedIdentity
from llm_generation.identity import semantic_commitment
from llm_generation.service import (
    AvailableResources, ExecutionAuthorization, GenerationService,
    ProviderCapability, ProviderFailureKind, ProviderInvocationError, ProviderResponse,
    canonical_evidence_projection, canonical_knowledge_projection,
    same_try_reinvocation_eligible, subject_input_commitment,
)
from llm_generation.sqlite_store import CandidateDStore
from rca_persistence.contracts import AdmittedRetryDisposition
from rca_persistence.contracts import (
    AttemptLineageRead, GenerationAttempt, GenerationLifecycle,
    LogicalTryOutcome, LogicalTryResultKind,
)
from test_llm_generation_phase2 import case, _structured
from test_llm_generation_causal_refinement import _admission_case
from _incident_evidence_store_testkit import success as evidence_success


class FakeProvider:
    def __init__(self, outcome, config):
        self.outcome = outcome
        self.calls = []
        self.token_bound_override = None
        self.token_accounting_available = True
        pin = config.pin
        self.capability = ProviderCapability(
            pin.provider, pin.model, pin.profile.identity,
            pin.result_schema.identity, pin.result_schema.version, True,
        )

    def input_token_upper_bound(self, prompt, timeout_seconds):
        assert timeout_seconds == self.outcome_timeout_seconds
        if not self.token_accounting_available:
            return None
        if self.token_bound_override is not None:
            return self.token_bound_override
        # This fake contract counts one token per UTF-8 byte exactly.
        return len(prompt.encode("utf-8"))

    def invoke_once(self, request):
        self.calls.append(request)
        if isinstance(self.outcome, ProviderInvocationError):
            raise ProviderInvocationError(self.outcome.kind, request_sent=True)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def environment(case, tmp_path, outcome=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = case.config
    original = case.content.input
    snapshot = case.evidence.resolve_snapshot(original.evidence_snapshot_id)
    from knowledge_index.contracts import KnowledgeSnapshotKey
    key = KnowledgeSnapshotKey(original.knowledge_snapshot_id)
    knowledge_snapshot = case.knowledge.read_snapshot(key).value
    knowledge_provenance = case.knowledge.read_provenance(key).value
    admitted = replace(
        original,
        evidence_projection=canonical_evidence_projection(snapshot),
        knowledge_projection=canonical_knowledge_projection(
            knowledge_snapshot, knowledge_provenance,
        ),
    )
    case.content = replace(case.content, input=admitted)
    raw = {
        "version": config.version, "provider": config.pin.provider, "model": config.pin.model,
        "profile": asdict(config.pin.profile), "prompt": asdict(config.pin.prompt),
        "result_schema": asdict(config.pin.result_schema),
        "configuration": asdict(config.pin.configuration), "bounds": asdict(config.bounds),
        "hidden_retries_disabled": True, "prompt_template": config.prompt_template,
        "result_schema_commitment": config.result_schema_commitment,
        "configuration_commitment": config.configuration_commitment,
    }
    if config.version == "2.0":
        raw["causal_rule_policy_id"] = config.causal_rule_policy_id
        raw["causal_rule_policy_version"] = config.causal_rule_policy_version
    resource = tmp_path / "generation.json"
    resource.write_text(json.dumps(raw), encoding="utf-8")
    store = CandidateDStore(tmp_path / "candidate_d.sqlite", resource_config_path=resource)
    assert store.initialize() is LocalReadStatus.FOUND
    fake = FakeProvider(outcome if outcome is not None else ProviderResponse(
        _structured(case.content, config), InvocationMetadata(1, 10, 5, 15, 100),
    ), config)
    fake.outcome_timeout_seconds = config.bounds.timeout_seconds
    service = GenerationService(store, fake, case.attempts, case.evidence, case.knowledge)
    source = case.content.input
    grant = ExecutionAuthorization(source.operation_id, source.try_identity, 0, True)
    resources = AvailableResources(1, 1, 1, 1)
    return service, fake, grant, resources


def test_fake_success_commit_and_response_loss_replay(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    first = service.execute(case.content.input, case.config, grant, resources)
    assert first.status is LocalReadStatus.FOUND and first.result is not None
    assert first.physical_invocations == 1 and len(fake.calls) == 1
    request = fake.calls[0]
    rendered = json.loads(request.prompt)
    assert rendered == {
        "prompt_identity": case.config.pin.prompt.identity,
        "prompt_version": case.config.pin.prompt.version,
        "template": case.config.prompt_template,
        "evidence": case.content.input.evidence_projection,
        "knowledge": case.content.input.knowledge_projection,
    }
    assert len(request.prompt.encode("utf-8")) <= case.config.bounds.maximum_request_bytes
    assert service.store.result(first.result.validated_result_id).value == first.result
    reopened = GenerationService(CandidateDStore(
        service.store.path, resource_config_path=service.store.resource_config_path,
    ), fake, case.attempts, case.evidence, case.knowledge)
    replay = reopened.execute(case.content.input, case.config, grant, resources)
    assert replay.result == first.result
    assert replay.physical_invocations == 0 and len(fake.calls) == 1


def test_result_authority_replays_across_operation_and_restart(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    first = service.execute(case.content.input, case.config, grant, resources)
    assert first.result is not None and len(fake.calls) == 1

    second_source = replace(case.content.input, operation_id="operation-2")
    second_grant = replace(grant, operation_id="operation-2")
    reopened = GenerationService(
        CandidateDStore(service.store.path,
                        resource_config_path=service.store.resource_config_path),
        fake, case.attempts, case.evidence, case.knowledge,
    )
    replay = reopened.execute(second_source, case.config, second_grant, resources)
    assert replay.result == first.result
    assert replay.result.content.input.operation_id == case.content.input.operation_id
    assert replay.physical_invocations == 0
    assert len(fake.calls) == 1


def test_cross_operation_result_semantic_contradiction_fails_closed(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    first = service.execute(case.content.input, case.config, grant, resources)
    assert first.result is not None and len(fake.calls) == 1

    contradictory = replace(
        case.content.input,
        operation_id="operation-2",
        evidence_projection="different approved projection",
    )
    second_grant = replace(grant, operation_id="operation-2")
    rejected = service.execute(contradictory, case.config, second_grant, resources)
    assert rejected.status is LocalReadStatus.REPAIR_REQUIRED
    assert rejected.failure.failure_class is FailureClass.IDENTITY_CONTRADICTION
    assert rejected.failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED
    assert len(fake.calls) == 1


def test_concurrent_operations_leave_one_authoritative_d_result(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    first_source = case.content.input
    second_source = replace(first_source, operation_id="operation-2")
    second_grant = replace(grant, operation_id="operation-2")
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(pool.map(
            lambda request: service.execute(
                request[0], case.config, request[1], resources,
            ),
            ((first_source, grant), (second_source, second_grant)),
        ))
    assert all(receipt.status is LocalReadStatus.FOUND for receipt in receipts)
    assert all(receipt.result is not None for receipt in receipts)
    assert receipts[0].result == receipts[1].result
    assert len(service.store.recovery().value.results) == 1
    assert 1 <= len(fake.calls) <= 2


def test_authoritative_projections_reject_tampering_and_cross_snapshot(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    source = case.content.input
    assert json.loads(source.evidence_projection)["snapshot_id"] == source.evidence_snapshot_id
    assert json.loads(source.knowledge_projection)["snapshot_id"] == source.knowledge_snapshot_id
    other = evidence_success(evidence="different evidence")
    mutations = (
        replace(source, evidence_projection=source.evidence_projection.replace(
            '"same evidence"', '"fabricated evidence"',
        )),
        replace(source, evidence_projection=canonical_evidence_projection(other.snapshot)),
        replace(source, knowledge_projection=source.knowledge_projection.replace(
            '"knowledge_gap":true', '"knowledge_gap":false',
        )),
        replace(source, knowledge_projection=source.knowledge_projection[:-1] +
                ',"extra_guidance":"unapproved"}'),
    )
    for index, altered in enumerate(mutations):
        isolated, isolated_fake, isolated_grant, isolated_resources = environment(
            case, tmp_path / f"projection-{index}",
        )
        receipt = isolated.execute(altered, case.config, isolated_grant, isolated_resources)
        assert receipt.failure.failure_class is FailureClass.INVALID_INPUT
        assert isolated_fake.calls == []
    with pytest.raises(ValueError):
        replace(source, evidence_projection=source.evidence_projection[:-1] +
                ',"scenario_id":"S1"}')
    assert fake.calls == []


@pytest.mark.parametrize("mutate", [
    lambda source: replace(source, pin=replace(source.pin, model="other-model")),
    lambda source: replace(source, pin=replace(source.pin, profile=replace(source.pin.profile, identity="other-profile"))),
    lambda source: replace(source, pin=replace(source.pin, prompt=replace(source.pin.prompt, identity="other-prompt"))),
    lambda source: replace(source, pin=replace(source.pin, result_schema=replace(source.pin.result_schema, identity="other-schema"))),
    lambda source: replace(source, pin=replace(source.pin, configuration=replace(source.pin.configuration, identity="other-config"))),
    lambda source: replace(source, evidence_revision_id="other-revision"),
])
def test_exact_admission_rejects_mismatch_without_call(case, tmp_path, mutate):
    service, fake, grant, resources = environment(case, tmp_path)
    receipt = service.execute(mutate(case.content.input), case.config, grant, resources)
    assert receipt.failure is not None
    assert receipt.failure.retry_safety is not AdmittedRetryDisposition.RETRYABLE
    assert len(fake.calls) == 0


def test_preflight_bounds_and_ground_truth_zero_calls(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    bounded = service.execute(case.content.input, case.config, grant,
                              AvailableResources(0, 1, 1, 1))
    assert bounded.failure.failure_class is FailureClass.INVOCATION_BOUND
    assert len(fake.calls) == 0

    service2, fake2, grant2, resources2 = environment(case, tmp_path / "second")
    unsafe = replace(case.content.input, evidence_projection="S1 answer mapping")
    rejected = service2.execute(unsafe, case.config, grant2, resources2)
    assert rejected.failure.failure_class is FailureClass.INVALID_INPUT
    assert len(fake2.calls) == 0

    service3, fake3, grant3, resources3 = environment(case, tmp_path / "prompt-bound")
    oversized = replace(case.content.input, evidence_projection="e" * 32700)
    bounded_prompt = service3.execute(oversized, case.config, grant3, resources3)
    assert bounded_prompt.failure.failure_class is FailureClass.INVALID_INPUT
    assert len(fake3.calls) == 0


def test_provider_capability_and_stale_try_reject_without_call(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    fake.capability = replace(fake.capability, model="unapproved-model")
    rejected = service.execute(case.content.input, case.config, grant, resources)
    assert rejected.failure.failure_class is FailureClass.CAPABILITY_MISMATCH
    assert len(fake.calls) == 0

    service2, fake2, grant2, resources2 = environment(case, tmp_path / "stale")
    stale = replace(case.content.input, try_identity=replace(
        case.content.input.try_identity, try_ordinal=2,
    ))
    stale_grant = replace(grant2, try_identity=stale.try_identity)
    rejected2 = service2.execute(stale, case.config, stale_grant, resources2)
    assert rejected2.failure.failure_class is FailureClass.IDENTITY_CONTRADICTION
    assert rejected2.failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED
    assert len(fake2.calls) == 0


@pytest.mark.parametrize("outcome,expected", [
    (ProviderInvocationError(ProviderFailureKind.TIMEOUT), FailureClass.PROVIDER_TIMEOUT),
    (ProviderInvocationError(ProviderFailureKind.UNAVAILABLE), FailureClass.PROVIDER_UNAVAILABLE),
    (ProviderInvocationError(ProviderFailureKind.QUOTA), FailureClass.PROVIDER_QUOTA),
    (ProviderInvocationError(ProviderFailureKind.RATE), FailureClass.PROVIDER_QUOTA),
    (ProviderInvocationError(ProviderFailureKind.RESOURCE), FailureClass.PROVIDER_QUOTA),
    (ProviderResponse("", InvocationMetadata(1, 0, 0, 0, 0)), FailureClass.MALFORMED_PROVIDER_OUTPUT),
    (ProviderResponse("{", InvocationMetadata(1, 0, 0, 0, 0)), FailureClass.STRUCTURED_PARSE),
])
def test_fake_failure_taxonomy_and_no_hidden_retry(case, tmp_path, outcome, expected):
    service, fake, grant, resources = environment(case, tmp_path, outcome)
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure.failure_class is expected
    assert receipt.failure.retry_safety is AdmittedRetryDisposition.RETRYABLE
    assert receipt.physical_invocations == len(fake.calls) == 1
    assert service.store.failure(case.content.input.operation_id,
                                 case.content.input.try_identity).value == receipt.failure
    replay = service.execute(case.content.input, case.config, grant, resources)
    assert replay.failure == receipt.failure and len(fake.calls) == 1


def test_schema_and_grounding_failure_are_non_retryable(case, tmp_path):
    data = _structured(case.content, case.config)
    data["schema_version"] = "unsupported"
    service, fake, grant, resources = environment(
        case, tmp_path, ProviderResponse(data, InvocationMetadata(1, 10, 5, 15, 100)),
    )
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure.failure_class is FailureClass.RESULT_SCHEMA
    assert receipt.failure.retry_safety is AdmittedRetryDisposition.NON_RETRYABLE
    assert len(fake.calls) == 1

    invalid = _structured(case.content, case.config)
    invalid["summary"] = "Unsupported conclusion"
    service2, fake2, grant2, resources2 = environment(
        case, tmp_path / "ground", ProviderResponse(invalid, InvocationMetadata(1, 10, 5, 15, 100)),
    )
    receipt2 = service2.execute(case.content.input, case.config, grant2, resources2)
    assert receipt2.failure.failure_class is FailureClass.GROUNDING
    assert receipt2.failure.retry_safety is AdmittedRetryDisposition.NON_RETRYABLE
    assert len(fake2.calls) == 1


def test_same_try_eligibility_is_safety_only(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    source = case.content.input
    assert not same_try_reinvocation_eligible(source, service.store, case.attempts)
    failure = GenerationFailure(source.try_identity, source.operation_id,
                                FailureClass.PROVIDER_TIMEOUT,
                                AdmittedRetryDisposition.RETRYABLE, "timeout",
                                subject_input_commitment(source), True)
    assert service.store.commit_failure(failure).status is LocalReadStatus.FOUND
    assert same_try_reinvocation_eligible(source, service.store, case.attempts)
    assert len(fake.calls) == 0
    assert service.execute(source, case.config, grant, resources).failure == failure
    assert len(fake.calls) == 0
    view = case.attempts.get_attempt_lineage(source.try_identity.attempt_id)
    outcome = LogicalTryOutcome(
        source.try_identity, LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.RETRYABLE, datetime.now(timezone.utc),
        failure_code="PROVIDER_TIMEOUT",
    )
    a_with_outcome = SimpleNamespace(get_attempt_lineage=lambda _: AttemptLineageRead(
        GenerationAttempt(view.attempt.lineage, GenerationLifecycle.FAILED, 1),
        (outcome,),
    ))
    assert not same_try_reinvocation_eligible(source, service.store, a_with_outcome)


def test_prior_failure_new_operation_requires_explicit_same_try_fact(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    source = case.content.input
    failure = GenerationFailure(
        source.try_identity, source.operation_id,
        FailureClass.PROVIDER_TIMEOUT, AdmittedRetryDisposition.RETRYABLE,
        "timeout", subject_input_commitment(source), False,
    )
    assert service.store.commit_failure(failure).status is LocalReadStatus.FOUND
    second_source = replace(source, operation_id="operation-2")
    second_grant = replace(grant, operation_id="operation-2")
    rejected = service.execute(second_source, case.config, second_grant, resources)
    assert rejected.failure.failure_class is FailureClass.IDENTITY_CONTRADICTION
    assert rejected.failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED
    assert rejected.physical_invocations == 0
    assert fake.calls == []


def test_prior_failure_with_explicit_same_try_fact_may_be_locally_admitted(case, tmp_path):
    service, fake, grant, resources = environment(
        case, tmp_path, ProviderInvocationError(ProviderFailureKind.TIMEOUT),
    )
    first = service.execute(case.content.input, case.config, grant, resources)
    assert first.failure.same_try_eligible is True
    assert first.failure.retry_safety is AdmittedRetryDisposition.RETRYABLE
    assert len(fake.calls) == 1

    second_source = replace(case.content.input, operation_id="operation-2")
    second_grant = replace(grant, operation_id="operation-2")
    assert same_try_reinvocation_eligible(second_source, service.store, case.attempts)
    fake.outcome = ProviderResponse(
        _structured(case.content, case.config),
        InvocationMetadata(1, 10, 5, 15, 100),
    )
    admitted = service.execute(second_source, case.config, second_grant, resources)
    assert admitted.result is not None
    assert admitted.result.content.input.operation_id == "operation-2"
    assert admitted.physical_invocations == 1
    assert len(fake.calls) == 2
    assert len(service.store.recovery().value.results) == 1


def test_prior_failure_semantic_change_is_not_same_try_eligible(case, tmp_path):
    service, fake, grant, resources = environment(
        case, tmp_path, ProviderInvocationError(ProviderFailureKind.TIMEOUT),
    )
    first = service.execute(case.content.input, case.config, grant, resources)
    assert first.failure.same_try_eligible is True
    changed = replace(
        case.content.input, operation_id="operation-2",
        evidence_projection="different approved projection",
    )
    changed_grant = replace(grant, operation_id="operation-2")
    rejected = service.execute(changed, case.config, changed_grant, resources)
    assert rejected.failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED
    assert rejected.physical_invocations == 0
    assert len(fake.calls) == 1


def test_a_side_try_outcome_blocks_new_operation_same_ordinal(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    source = replace(case.content.input, operation_id="operation-2")
    view = case.attempts.get_attempt_lineage(source.try_identity.attempt_id)
    outcome = LogicalTryOutcome(
        source.try_identity, LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.RETRYABLE, datetime.now(timezone.utc),
        failure_code="PROVIDER_TIMEOUT",
    )
    service.attempts = SimpleNamespace(get_attempt_lineage=lambda _: AttemptLineageRead(
        GenerationAttempt(view.attempt.lineage, GenerationLifecycle.FAILED, 1),
        (outcome,),
    ))
    second_grant = ExecutionAuthorization(
        source.operation_id, source.try_identity, 1, True,
    )
    rejected = service.execute(source, case.config, second_grant, resources)
    assert rejected.failure.failure_class is FailureClass.IDENTITY_CONTRADICTION
    assert rejected.failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED
    assert rejected.physical_invocations == 0
    assert fake.calls == []


def test_d_result_blocks_same_try_and_bad_store_blocks_execution(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.result is not None
    assert not same_try_reinvocation_eligible(case.content.input, service.store, case.attempts)
    service.store.path.write_bytes(b"")
    again = service.execute(case.content.input, case.config, grant, resources)
    assert again.status is LocalReadStatus.REPAIR_REQUIRED
    assert again.failure.failure_class is FailureClass.IDENTITY_CONTRADICTION
    assert again.failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED
    assert len(fake.calls) == 1


def test_public_authority_failures_are_typed_before_invocation(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    service.evidence = SimpleNamespace(
        resolve_snapshot=lambda _: None,
        resolve_revision=lambda _: case.evidence.resolve_revision(case.content.input.evidence_revision_id),
    )
    rejected = service.execute(case.content.input, case.config, grant, resources)
    assert rejected.failure.failure_class is FailureClass.EVIDENCE_REFERENCE
    assert len(fake.calls) == 0

    service2, fake2, grant2, resources2 = environment(case, tmp_path / "knowledge")
    from knowledge_index.contracts import KnowledgeReadResult, KnowledgeReadStatus
    service2.knowledge = SimpleNamespace(
        read_snapshot=lambda _: KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND),
        read_provenance=lambda _: KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND),
    )
    rejected2 = service2.execute(case.content.input, case.config, grant2, resources2)
    assert rejected2.failure.failure_class is FailureClass.KNOWLEDGE_AUTHORITY
    assert len(fake2.calls) == 0


def test_secret_bearing_provider_exception_never_persists(case, tmp_path):
    service, fake, grant, resources = environment(
        case, tmp_path, RuntimeError("Authorization: Bearer secret-token"),
    )
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert receipt.failure.retry_safety is AdmittedRetryDisposition.RETRYABLE
    assert b"secret-token" not in service.store.path.read_bytes()
    assert len(fake.calls) == 1


def test_raw_rejected_provider_output_is_not_durable(case, tmp_path):
    raw = '{"raw":"UNSAFE_RAW_MARKER"}'
    service, fake, grant, resources = environment(
        case, tmp_path, ProviderResponse(raw, InvocationMetadata(1, 10, 5, 15, 100)),
    )
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure is not None
    assert b"UNSAFE_RAW_MARKER" not in service.store.path.read_bytes()
    assert len(fake.calls) == 1


def test_ground_truth_in_fake_response_is_typed_and_not_durable(case, tmp_path):
    data = _structured(case.content, case.config)
    data["summary"] = "expected_answer=S1"
    service, fake, grant, resources = environment(
        case, tmp_path, ProviderResponse(data, InvocationMetadata(1, 10, 5, 15, 100)),
    )
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure.failure_class is FailureClass.INVALID_INPUT
    assert receipt.failure.retry_safety is AdmittedRetryDisposition.NON_RETRYABLE
    assert b"expected_answer" not in service.store.path.read_bytes()
    assert len(fake.calls) == 1


def test_fake_output_bound_is_non_retryable(case, tmp_path):
    output = "x" * (case.config.bounds.maximum_output_bytes + 1)
    service, fake, grant, resources = environment(
        case, tmp_path, ProviderResponse(output, InvocationMetadata(1, 10, 5, 15, 100)),
    )
    receipt = service.execute(case.content.input, case.config, grant, resources)
    assert receipt.failure.failure_class is FailureClass.INVOCATION_BOUND
    assert receipt.failure.retry_safety is AdmittedRetryDisposition.NON_RETRYABLE
    assert len(fake.calls) == 1


def test_safe_token_bound_over_limit_and_unavailable_fail_before_call(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    source = case.content.input
    prompt = json.dumps({
        "prompt_identity": case.config.pin.prompt.identity,
        "prompt_version": case.config.pin.prompt.version,
        "template": case.config.prompt_template,
        "evidence": source.evidence_projection,
        "knowledge": source.knowledge_projection,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(prompt.encode("utf-8")) // 4 < case.config.bounds.maximum_input_tokens
    fake.token_bound_override = case.config.bounds.maximum_input_tokens + 1
    receipt = service.execute(source, case.config, grant, resources)
    assert receipt.failure.failure_class is FailureClass.INVOCATION_BOUND
    assert fake.calls == []

    service2, fake2, grant2, resources2 = environment(case, tmp_path / "unavailable")
    fake2.token_accounting_available = False
    unavailable = service2.execute(case.content.input, case.config, grant2, resources2)
    assert unavailable.failure.failure_class is FailureClass.CAPABILITY_MISMATCH
    assert fake2.calls == []


def test_rendered_byte_bound_is_independent_of_token_capability(case, tmp_path):
    old = case.config
    bounds = replace(old.bounds, maximum_input_bytes=100)
    parts = (
        old.version, old.pin.provider, old.pin.model, old.pin.profile,
        old.pin.prompt, old.pin.result_schema, old.pin.configuration.version,
        bounds, old.hidden_retries_disabled, old.prompt_template,
        old.result_schema_commitment,
    )
    commitment = semantic_commitment(parts)
    pin = replace(old.pin, configuration=VersionedIdentity(
        "cfg_" + commitment, old.pin.configuration.version,
    ))
    config = replace(old, pin=pin, bounds=bounds, configuration_commitment=commitment)
    source = replace(case.content.input, pin=pin)
    view = case.attempts.get_attempt_lineage(source.try_identity.attempt_id)
    provenance = replace(view.attempt.lineage.generation_provenance,
                         configuration_id=pin.configuration.identity)
    lineage = replace(view.attempt.lineage, generation_provenance=provenance)
    attempts = SimpleNamespace(get_attempt_lineage=lambda _: AttemptLineageRead(
        GenerationAttempt(lineage, GenerationLifecycle.PENDING), (),
    ))
    bounded_case = SimpleNamespace(
        content=replace(case.content, input=source), config=config,
        attempts=attempts, evidence=case.evidence, knowledge=case.knowledge,
    )
    service, fake, grant, resources = environment(bounded_case, tmp_path)
    receipt = service.execute(bounded_case.content.input, config, grant, resources)
    assert receipt.failure.failure_class is FailureClass.INVOCATION_BOUND
    assert fake.calls == []


def test_schema_two_identified_requires_real_causal_proof(case, tmp_path):
    content, config, attempts, evidence, knowledge = _admission_case(case)
    causal = SimpleNamespace(
        content=content, config=config, attempts=attempts,
        evidence=evidence, knowledge=knowledge,
    )
    service, fake, grant, resources = environment(causal, tmp_path)
    receipt = service.execute(causal.content.input, config, grant, resources)
    assert receipt.result is not None
    assert receipt.result.content.contract_version == "2"
    assert len(receipt.result.content.causal_proofs) == 1
    assert receipt.result.content.claims[-1].causal_assertion is not None
    assert len(fake.calls) == 1


def test_durable_replay_identity_contradiction_is_typed(case, tmp_path):
    service, fake, grant, resources = environment(case, tmp_path)
    first = service.execute(case.content.input, case.config, grant, resources)
    assert first.result is not None
    altered = replace(case.content.input, evidence_projection="different approved projection")
    replay = service.execute(altered, case.config, grant, resources)
    assert replay.status is LocalReadStatus.REPAIR_REQUIRED
    assert replay.failure.failure_class is FailureClass.IDENTITY_CONTRADICTION
    assert replay.failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED
    assert len(fake.calls) == 1
