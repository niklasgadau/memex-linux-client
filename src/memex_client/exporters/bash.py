from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from memex_client.api import MemexAPI
from memex_client.config import resolve_client_name
from memex_client.exporters.base import BaseExporter
from memex_client.state import SyncState

BASH_HISTORY = Path.home() / ".bash_history"


class BashExporter(BaseExporter):
    name = "bash"

    def __init__(self, config: dict, state: SyncState, api: MemexAPI) -> None:
        super().__init__(config, state, api)
        self.client_name = resolve_client_name(config)
        self._new_offset: int = 0

    def collect_new_entries(self) -> list[dict]:
        if not BASH_HISTORY.exists():
            return []

        file_size = BASH_HISTORY.stat().st_size
        saved = self.state.get(self.name)
        offset = saved.get("byte_offset", 0)

        if file_size < offset:
            offset = 0

        if file_size == offset:
            return []

        with open(BASH_HISTORY, "r", errors="replace") as f:
            f.seek(offset)
            raw = f.read()

        self._new_offset = offset + len(raw.encode())
        return self._parse(raw)

    def _parse(self, raw: str) -> list[dict]:
        entries = []
        lines = raw.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#") and line[1:].strip().isdigit():
                # Timestamp line followed by command
                timestamp = int(line[1:].strip())
                if i + 1 < len(lines):
                    i += 1
                    command = lines[i]
                    entries.append(self._make_entry(command, timestamp))
            elif line.strip():
                # Plain command without timestamp
                entries.append(self._make_entry(line, None))
            i += 1
        return entries

    def _make_entry(self, cmd: str, when: int | None) -> dict:
        return {
            "command": cmd,
            "timestamp": when,
            "shell": "bash",
            "paths": [],
            "client": self.client_name,
        }

    def _post(self, entries: list[dict]) -> int:
        return self.api.post_shell(entries)

    def _save_state(self) -> None:
        self.state.update(self.name, {
            "byte_offset": self._new_offset,
            "last_sync_time": datetime.now(timezone.utc).isoformat(),
        })
