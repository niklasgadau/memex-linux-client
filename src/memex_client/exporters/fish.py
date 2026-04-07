from __future__ import annotations

import os
from pathlib import Path

from memex_client.api import MemexAPI
from memex_client.config import get_client_id
from memex_client.exporters.base import BaseExporter
from memex_client.state import SyncState

FISH_HISTORY = Path.home() / ".local" / "share" / "fish" / "fish_history"


class FishExporter(BaseExporter):
    name = "fish"

    def __init__(self, config: dict, state: SyncState, api: MemexAPI) -> None:
        super().__init__(config, state, api)
        self.client_id = get_client_id()
        self._new_offset: int = 0

    def collect_new_entries(self) -> list[dict]:
        if not FISH_HISTORY.exists():
            return []

        file_size = FISH_HISTORY.stat().st_size
        saved = self.state.get(self.name)
        offset = saved.get("byte_offset", 0)

        # File was truncated/rewritten — reset
        if file_size < offset:
            offset = 0

        if file_size == offset:
            return []

        with open(FISH_HISTORY, "r", errors="replace") as f:
            f.seek(offset)
            raw = f.read()

        self._new_offset = offset + len(raw.encode())
        return self._parse(raw)

    def _parse(self, raw: str) -> list[dict]:
        entries = []
        current_cmd = None
        current_when = None
        current_paths: list[str] = []

        for line in raw.splitlines():
            if line.startswith("- cmd:"):
                # Save previous entry
                if current_cmd is not None:
                    entries.append(self._make_entry(current_cmd, current_when, current_paths))
                current_cmd = line[len("- cmd:"):].strip()
                current_when = None
                current_paths = []
            elif line.strip().startswith("when:"):
                try:
                    current_when = int(line.strip()[len("when:"):].strip())
                except ValueError:
                    current_when = None
            elif line.strip().startswith("paths:"):
                pass  # paths follow on subsequent lines
            elif line.strip().startswith("- ") and current_cmd is not None and current_when is not None:
                # This is a path entry under paths:
                current_paths.append(line.strip()[2:])

        # Don't forget last entry
        if current_cmd is not None:
            entries.append(self._make_entry(current_cmd, current_when, current_paths))

        return entries

    def _make_entry(self, cmd: str, when: int | None, paths: list[str]) -> dict:
        return {
            "command": cmd,
            "timestamp": when,
            "shell": "fish",
            "paths": paths,
            "client": self.client_id,
        }

    def _post(self, entries: list[dict]) -> int:
        return self.api.post_shell(entries)

    def _save_state(self) -> None:
        from datetime import datetime, timezone

        self.state.update(self.name, {
            "byte_offset": self._new_offset,
            "last_sync_time": datetime.now(timezone.utc).isoformat(),
        })
