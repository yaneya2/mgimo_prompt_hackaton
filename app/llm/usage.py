"""Append-only usage events; replace this sink with storage in A4."""
import json
from pathlib import Path
from threading import Lock
from typing import Protocol


class UsageSink(Protocol):
    def record(self, event: dict) -> None: ...


class JsonlUsageSink:
    """One local worker; deduplicate by event id when aggregating records."""
    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def record(self, event: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
