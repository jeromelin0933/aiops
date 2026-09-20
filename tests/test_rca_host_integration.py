from datetime import timedelta
import inspect
import sqlite3

import pytest

from incident_management import (
    IncidentDomainError,
    IncidentManager,
    IncidentRcaPublicationRequest,
    SqliteIncidentStore,
)
from rca_integration import RcaPublicationCoordinator
from rca_persistence import (
    PublicationDisposition,
    PublicationTargetIdentity,
    RcaDomainError,
    SqliteRcaStore,
)

from test_incident_manager import _create_request
from test_rca_persistence_version_artifact import NOW, artifact, commit, seed


def _host(tmp_path):
    incident_path = tmp_path / "incident.db"
    rca_path = tmp_path / "rca.db"
    incident_store = SqliteIncidentStore(str(incident_path))
    incident_manager = IncidentManager(
        incident_store, incident_id_factory=lambda: "INC-1"
    )
    incident_manager.apply_correlation_mutation(_create_request())
    rca_store = SqliteRcaStore(rca_path)
    seed(rca_store)
    commit(rca_store)
    coordinator = RcaPublicationCoordinator(
        rca_store, incident_manager, incident_store
    )
    return incident_path, rca_path, incident_store, rca_store, coordinator


def _commit_next(
    store, attempt_id, operation_id, version_id, publication_id, expected
):
    seed(store, attempt_id)
    target = PublicationTargetIdentity(
        publication_id, "AGG-1", "INC-1", version_id, expected
    )
    return store.commit_validated_artifact(
        operation_id, attempt_id, artifact(), target, NOW + timedelta(minutes=2)
    )


def test_version_durable_to_incident_publication_to_candidate_a_current(
    tmp_path,
) -> None:
    _, _, incident_store, rca_store, coordinator = _host(tmp_path)
    result = coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    assert result.disposition is PublicationDisposition.APPLIED
    assert incident_store.get_rca_relationship("INC-1").current_version_id == "VER-1"
    assert rca_store.get_current("AGG-1").version.version_id == "VER-1"
    incident_store.close()
    rca_store.close()


def test_response_loss_replays_same_identity_without_duplicate_effect(tmp_path) -> None:
    _, _, incident_store, rca_store, coordinator = _host(tmp_path)
    target = rca_store.get_publication_result("PUB-1").target
    incident_result = IncidentManager(
        incident_store, incident_id_factory=lambda: "unused"
    ).publish_rca_current(
        IncidentRcaPublicationRequest(
            target.publication_operation_id,
            target.incident_id,
            target.target_version_id,
            target.expected_current_version_id,
            NOW + timedelta(minutes=3),
        )
    )
    assert rca_store.get_current("AGG-1") is None

    completed = coordinator.reconcile("PUB-1", NOW + timedelta(minutes=4))
    assert completed.recorded_at == incident_result.completed_at
    assert coordinator.reconcile("PUB-1", NOW + timedelta(minutes=5)) == completed
    assert len(rca_store.get_version_history("AGG-1")) == 1
    incident_store.close()
    rca_store.close()


def test_a_side_committed_incident_side_missing_is_reconciled(tmp_path) -> None:
    _, _, incident_store, rca_store, coordinator = _host(tmp_path)
    assert incident_store.get_rca_publication_result("PUB-1") is None
    assert len(coordinator.reconcile_outstanding(NOW + timedelta(minutes=3))) == 1
    assert incident_store.get_rca_publication_result("PUB-1") is not None
    assert rca_store.get_current("AGG-1").version.version_id == "VER-1"
    incident_store.close()
    rca_store.close()


def test_replacement_wins_and_older_publication_cannot_overwrite_it(tmp_path) -> None:
    _, _, incident_store, rca_store, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    _commit_next(rca_store, "ATT-2", "OP-COMMIT-2", "VER-2", "PUB-2", "VER-1")
    coordinator.publish("PUB-2", NOW + timedelta(minutes=4))
    _commit_next(
        rca_store, "ATT-3", "OP-COMMIT-3", "VER-3", "PUB-STALE", "VER-1"
    )

    stale = coordinator.publish("PUB-STALE", NOW + timedelta(minutes=5))
    assert stale.disposition is PublicationDisposition.PRECONDITION_SUPERSEDED
    assert stale.resulting_current_version_id == "VER-2"
    assert incident_store.get_rca_relationship("INC-1").current_version_id == "VER-2"
    assert rca_store.get_current("AGG-1").version.version_id == "VER-2"
    incident_store.close()
    rca_store.close()


def test_restart_before_publication_completion_reconciles_original_identity(
    tmp_path,
) -> None:
    incident_path, rca_path, incident_store, rca_store, _ = _host(tmp_path)
    incident_store.close()
    rca_store.close()

    with SqliteIncidentStore(str(incident_path)) as reopened_incident:
        with SqliteRcaStore(rca_path) as reopened_rca:
            coordinator = RcaPublicationCoordinator(
                reopened_rca,
                IncidentManager(reopened_incident),
                reopened_incident,
            )
            result = coordinator.reconcile("PUB-1", NOW + timedelta(minutes=3))
            assert result.target.publication_operation_id == "PUB-1"
            assert reopened_rca.get_current("AGG-1").version.version_id == "VER-1"


def test_repeated_restart_does_not_duplicate_semantic_effect(tmp_path) -> None:
    incident_path, rca_path, incident_store, rca_store, _ = _host(tmp_path)
    incident_store.close()
    rca_store.close()
    results = []
    for minute in (3, 4, 5):
        with SqliteIncidentStore(str(incident_path)) as reopened_incident:
            with SqliteRcaStore(rca_path) as reopened_rca:
                coordinator = RcaPublicationCoordinator(
                    reopened_rca,
                    IncidentManager(reopened_incident),
                    reopened_incident,
                )
                results.append(
                    coordinator.reconcile("PUB-1", NOW + timedelta(minutes=minute))
                )
                assert len(reopened_rca.get_version_history("AGG-1")) == 1
    assert results[0] == results[1] == results[2]


def test_cross_domain_corruption_fails_closed_through_public_semantics(tmp_path) -> None:
    incident_path, _, incident_store, rca_store, coordinator = _host(tmp_path)
    coordinator.publish("PUB-1", NOW + timedelta(minutes=3))
    incident_store.close()
    with sqlite3.connect(incident_path) as connection:
        connection.execute(
            """UPDATE incident_rca_publication_receipts
                  SET target_version_id='VER-CONTRADICTORY'
                WHERE publication_operation_id='PUB-1'"""
        )
    corrupted = SqliteIncidentStore(str(incident_path))
    with pytest.raises((IncidentDomainError, RcaDomainError)):
        RcaPublicationCoordinator(
            rca_store, IncidentManager(corrupted), corrupted
        ).reconcile("PUB-1", NOW + timedelta(minutes=4))
    corrupted.close()
    rca_store.close()


def test_host_coordinator_has_no_private_store_or_runtime_authority() -> None:
    source = inspect.getsource(RcaPublicationCoordinator)
    assert "_transaction" not in source
    assert "sqlite" not in source.lower()
    assert not {
        "schedule",
        "retry",
        "recover_to_ready",
        "clock",
        "startup",
    } & set(RcaPublicationCoordinator.__dict__)
