from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest

from incident_management import (
    IncidentDomainError,
    IncidentErrorCode,
    IncidentManager,
    IncidentRcaPublicationDisposition,
    IncidentRcaPublicationRequest,
    IncidentRcaRelationship,
    SqliteIncidentStore,
)

from test_incident_manager import NOW, _create_request


def _seed(tmp_path):
    database = tmp_path / "incident.db"
    store = SqliteIncidentStore(str(database))
    manager = IncidentManager(store, incident_id_factory=lambda: "INC-1")
    manager.apply_correlation_mutation(_create_request())
    return database, store, manager


def _request(
    operation="PUB-1", target="VER-1", expected=None, *, minute=1
):
    return IncidentRcaPublicationRequest(
        operation,
        "INC-1",
        target,
        expected,
        NOW + timedelta(minutes=minute),
    )


def test_initial_publication_and_replacement_are_authoritative_and_replay_safe(
    tmp_path,
) -> None:
    _, store, manager = _seed(tmp_path)
    assert store.get_rca_relationship("INC-1") == IncidentRcaRelationship(
        "INC-1", "PENDING", None
    )

    first = manager.publish_rca_current(_request())
    assert first.disposition is IncidentRcaPublicationDisposition.APPLIED
    assert manager.publish_rca_current(_request(minute=99)) == first

    second = manager.publish_rca_current(
        _request("PUB-2", "VER-2", "VER-1", minute=2)
    )
    assert second.disposition is IncidentRcaPublicationDisposition.APPLIED
    assert store.get_rca_relationship("INC-1") == IncidentRcaRelationship(
        "INC-1", "COMPLETED", "VER-2"
    )
    assert store.get_rca_publication_result("PUB-1") == first
    assert store.get_rca_publication_result("PUB-2") == second
    store.validate_integrity()
    store.close()


def test_stale_and_already_current_requests_never_overwrite_current(tmp_path) -> None:
    _, store, manager = _seed(tmp_path)
    manager.publish_rca_current(_request())
    manager.publish_rca_current(_request("PUB-2", "VER-2", "VER-1", minute=2))

    stale = manager.publish_rca_current(
        _request("PUB-STALE", "VER-OLD", "VER-1", minute=3)
    )
    assert stale.disposition is IncidentRcaPublicationDisposition.PRECONDITION_SUPERSEDED
    assert stale.resulting_current_version_id == "VER-2"

    conflict = manager.publish_rca_current(
        _request("PUB-CONFLICT", "VER-2", "VER-1", minute=4)
    )
    assert (
        conflict.disposition
        is IncidentRcaPublicationDisposition.TARGET_ALREADY_CURRENT_CONFLICT
    )
    assert store.get_rca_relationship("INC-1").current_version_id == "VER-2"
    store.close()


def test_contradictory_same_operation_fails_closed(tmp_path) -> None:
    _, store, manager = _seed(tmp_path)
    manager.publish_rca_current(_request())
    with pytest.raises(IncidentDomainError) as raised:
        manager.publish_rca_current(
            replace(_request(), target_version_id="VER-CONTRADICTORY")
        )
    assert raised.value.code is IncidentErrorCode.MUTATION_RECEIPT_CONFLICT
    store.close()


def test_publication_receipt_survives_restart_and_corruption_fails_closed(
    tmp_path,
) -> None:
    database, store, manager = _seed(tmp_path)
    expected = manager.publish_rca_current(_request())
    store.close()

    with SqliteIncidentStore(str(database)) as reopened:
        assert reopened.get_rca_publication_result("PUB-1") == expected

    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE incident_rca_publication_receipts
                  SET resulting_current_version_id='VER-CORRUPT'
                WHERE publication_operation_id='PUB-1'"""
        )
    with pytest.raises(IncidentDomainError) as raised:
        SqliteIncidentStore(str(database)).validate_integrity()
    assert raised.value.code is IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE
