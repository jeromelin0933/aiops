from dataclasses import replace

import pytest

from knowledge_index import (
    BuildFailureCode,
    BuildOperationKey,
    KnowledgeBuildService,
    SqliteKnowledgeStore,
)
from _knowledge_build_testkit import (
    DeterministicIndex,
    DeterministicProvider,
    capability,
    limits,
    manifest_and_plan,
)


@pytest.mark.parametrize(
    "limit_changes",
    [
        {"maximum_request_bytes": 1},
        {"maximum_batch_items": 0},
        {"maximum_invocations": 0},
        {"maximum_cost_units": 0},
        {"maximum_rate_units": 0},
        {"maximum_quota_units": 0},
        {"maximum_resource_units": 0},
    ],
)
def test_invalid_or_exceeded_provider_bounds_fail_before_invocation(tmp_path, limit_changes) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    provider = DeterministicProvider()
    index = DeterministicIndex()
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        with pytest.raises(Exception) if 0 in limit_changes.values() else _does_not_raise():
            configured = limits(**limit_changes)
            result = KnowledgeBuildService(store, provider, index).stage(
                operation_key=BuildOperationKey("stage-bounds"), raw_manifest=raw,
                source_root=tmp_path, chunks=chunks, identity_input=identity_input,
                limits=configured, required_capability_identity="embedding-capability-v1",
            )
            assert result.failures[0].code is BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED
            assert provider.calls == 0 and index.stage_calls == 0
    finally:
        store.close()


class _does_not_raise:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


def test_timeout_and_all_limits_are_forwarded_to_single_provider_invocation(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    configured = limits(timeout_seconds=7.5, maximum_cost_units=9)
    provider = DeterministicProvider(cost_units=9)
    index = DeterministicIndex()
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        result = KnowledgeBuildService(store, provider, index).stage(
            operation_key=BuildOperationKey("stage-limits"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=configured, required_capability_identity="embedding-capability-v1",
        )
        assert result.record is not None
        assert provider.calls == 1
        assert provider.requests[0].limits == configured
    finally:
        store.close()


def test_hidden_retry_or_profile_switch_is_rejected_without_provider_call(tmp_path) -> None:
    selected = capability(hidden_retries_disabled=False)
    raw, chunks, identity_input = manifest_and_plan(tmp_path, profile=selected)
    provider = DeterministicProvider(selected)
    index = DeterministicIndex()
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        result = KnowledgeBuildService(store, provider, index).stage(
            operation_key=BuildOperationKey("stage-hidden-retry"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity=selected.capability_identity,
        )
        assert result.failures[0].code is BuildFailureCode.PROFILE_MISMATCH
        assert provider.calls == 0
    finally:
        store.close()


def test_provider_cannot_report_hidden_extra_invocations_or_cost(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    provider = DeterministicProvider(invocation_count=2, cost_units=101, resource_units=101)
    index = DeterministicIndex()
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        result = KnowledgeBuildService(store, provider, index).stage(
            operation_key=BuildOperationKey("stage-extra-call"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        assert result.failures[0].code is BuildFailureCode.PROVIDER_CONTRACT_INVALID
        assert provider.calls == 1 and index.stage_calls == 0
    finally:
        store.close()


def test_provider_exception_is_sanitized_and_not_retried(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    provider = DeterministicProvider(raises=RuntimeError("api_key=synthetic-secret"))
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        result = KnowledgeBuildService(store, provider, DeterministicIndex()).stage(
            operation_key=BuildOperationKey("stage-error"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        assert provider.calls == 1
        assert "synthetic-secret" not in result.failures[0].detail
    finally:
        store.close()


def test_provider_capability_failure_is_sanitized_before_embedding(tmp_path) -> None:
    raw, chunks, identity_input = manifest_and_plan(tmp_path)
    provider = DeterministicProvider(
        capability_error=RuntimeError("credential=synthetic-secret")
    )
    index = DeterministicIndex()
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        result = KnowledgeBuildService(store, provider, index).stage(
            operation_key=BuildOperationKey("stage-capability-error"), raw_manifest=raw,
            source_root=tmp_path, chunks=chunks, identity_input=identity_input,
            limits=limits(), required_capability_identity="embedding-capability-v1",
        )
        assert result.failures[0].code is BuildFailureCode.PROVIDER_UNAVAILABLE
        assert "synthetic-secret" not in result.failures[0].detail
        assert provider.calls == 0 and index.stage_calls == 0
    finally:
        store.close()
