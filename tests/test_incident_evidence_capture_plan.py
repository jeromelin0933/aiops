from datetime import datetime, timezone

from incident_evidence import EvidenceSource

from test_incident_evidence_trusted_core import admit, event, incident


UTC = timezone.utc


def test_episode_windows_boundaries_and_selectors_are_deterministic():
    events = [
        event("EVT-2", "2026-09-22T01:03:00Z", service_name="worker"),
        event("EVT-1", "2026-09-22T01:00:00Z"),
    ]
    value = incident(("EVT-2", "EVT-1"))
    first, _, _ = admit((value, value), events)
    second, _, _ = admit((value, value), events)

    assert first == second
    assert first.episode.start == datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
    assert first.episode.end == datetime(2026, 9, 22, 1, 3, tzinfo=UTC)
    assert first.windows.logs.start == datetime(2026, 9, 22, 0, 58, tzinfo=UTC)
    assert first.windows.metrics.start == datetime(2026, 9, 22, 0, 55, tzinfo=UTC)
    assert first.windows.logs.end == datetime(2026, 9, 22, 1, 4, tzinfo=UTC)
    assert first.windows.default_post_context_boundary == datetime(2026, 9, 22, 1, 5, tzinfo=UTC)
    assert not first.logs_reached_post_context_boundary
    assert not first.metrics_reached_post_context_boundary

    assert {(item.selector.source, item.selector.field_name) for item in first.selectors} == {
        (EvidenceSource.LOKI, "service_name"),
        (EvidenceSource.LOKI, "source_ip"),
        (EvidenceSource.PROMETHEUS, "service_name"),
    }
    assert {item.source_event_id for item in first.selectors} == {"EVT-2"}


def test_created_at_never_replaces_event_episode_time():
    value = incident(created_at=datetime(2026, 9, 22, 0, 0, tzinfo=UTC))
    plan, _, _ = admit((value, value), [event()])
    assert plan.episode.start == datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
