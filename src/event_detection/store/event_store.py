"""Append and read PRD-002 events in JSONL format."""

import json
from pathlib import Path


class EventStoreReadIntegrityError(RuntimeError):
    """The authoritative Event representation cannot be read completely."""

    def __init__(self, message: str, *, line_number: int | None = None):
        super().__init__(message)
        self.line_number = line_number


class EventStore:
    def __init__(self, store_path: str = "events/event_store.jsonl"):
        self.path = Path(store_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict) -> None:
        with self.path.open("a", encoding="utf-8") as store:
            store.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def read_all_authoritative(self) -> list[dict]:
        """Read every authoritative Event or fail without a partial result.

        Blank lines retain the existing JSONL representation semantics.  Every
        non-blank line must decode as a JSON object; malformed JSON, non-object
        JSON, and failures to read the representation are integrity failures.
        """
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise EventStoreReadIntegrityError(
                "authoritative EventStore representation cannot be read"
            ) from exc

        events: list[dict] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EventStoreReadIntegrityError(
                    f"authoritative EventStore record at line {line_number} is malformed JSON",
                    line_number=line_number,
                ) from exc
            if not isinstance(value, dict):
                raise EventStoreReadIntegrityError(
                    f"authoritative EventStore record at line {line_number} is not a JSON object",
                    line_number=line_number,
                )
            events.append(value)
        return events
