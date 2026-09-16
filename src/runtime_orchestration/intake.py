"""Authoritative durable Event intake without a cursor or JSONL knowledge."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from event_detection.store.event_store import EventStoreReadIntegrityError


class AuthoritativeEventStore(Protocol):
    """The only EventStore surface permitted for production Runtime intake."""

    def read_all_authoritative(self) -> list[dict]: ...


class RuntimeEventIntakeIntegrityError(RuntimeError):
    """No complete, trustworthy Event snapshot could be constructed."""


@dataclass(frozen=True, slots=True)
class AuthoritativeEventSnapshot:
    events: tuple[Mapping[str, object], ...]
    event_by_id: Mapping[str, Mapping[str, object]]
    scan_generation: int


class DurableEventIntake:
    """All-or-failure scans with an explicitly non-authoritative local cache."""

    def __init__(self, event_store: AuthoritativeEventStore) -> None:
        if not callable(getattr(event_store, "read_all_authoritative", None)):
            raise TypeError("event_store must provide read_all_authoritative()")
        self._event_store = event_store
        self._snapshot: AuthoritativeEventSnapshot | None = None
        self._scan_generation = 0

    @property
    def process_local_snapshot(self) -> AuthoritativeEventSnapshot | None:
        return self._snapshot

    def clear_process_local_cache(self) -> None:
        """Simulate restart/cache loss; correctness never depends on this cache."""
        self._snapshot = None

    def scan_authoritative(self) -> AuthoritativeEventSnapshot:
        try:
            raw_events = self._event_store.read_all_authoritative()
        except EventStoreReadIntegrityError as exc:
            raise RuntimeEventIntakeIntegrityError(
                "authoritative EventStore scan failed integrity validation"
            ) from exc
        except Exception as exc:
            raise RuntimeEventIntakeIntegrityError(
                "authoritative EventStore cannot be read reliably"
            ) from exc
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise RuntimeEventIntakeIntegrityError(
                "read_all_authoritative() must return a complete Event sequence"
            )

        events: list[Mapping[str, object]] = []
        lookup: dict[str, Mapping[str, object]] = {}
        for index, raw_event in enumerate(raw_events):
            if not isinstance(raw_event, Mapping):
                raise RuntimeEventIntakeIntegrityError(
                    f"authoritative Event at index {index} is not a mapping"
                )
            event_id = raw_event.get("event_id")
            if (
                not isinstance(event_id, str)
                or not event_id
                or event_id != event_id.strip()
            ):
                raise RuntimeEventIntakeIntegrityError(
                    f"authoritative Event at index {index} has no valid event_id"
                )
            if event_id in lookup:
                raise RuntimeEventIntakeIntegrityError(
                    f"authoritative EventStore contains duplicate event_id {event_id}"
                )
            local_event = MappingProxyType(dict(raw_event))
            events.append(local_event)
            lookup[event_id] = local_event

        self._scan_generation += 1
        snapshot = AuthoritativeEventSnapshot(
            tuple(events), MappingProxyType(lookup), self._scan_generation
        )
        self._snapshot = snapshot
        return snapshot
