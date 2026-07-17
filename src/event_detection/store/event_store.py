"""Append and read PRD-002 events in JSONL format."""

import json
from pathlib import Path


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
