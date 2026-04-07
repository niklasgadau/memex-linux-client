from __future__ import annotations

import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "memex"
STATE_PATH = DATA_DIR / "last_sync.json"


class SyncState:
    def __init__(self) -> None:
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if STATE_PATH.exists():
            self._data = json.loads(STATE_PATH.read_text())

    def get(self, source: str) -> dict:
        return self._data.get(source, {})

    def update(self, source: str, state: dict) -> None:
        self._data[source] = state
        self._save()

    def _save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, STATE_PATH)

    def reset(self, source: str) -> None:
        self._data.pop(source, None)
        self._save()

    def all(self) -> dict:
        return dict(self._data)
