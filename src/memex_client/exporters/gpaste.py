from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from memex_client.api import MemexAPI
from memex_client.config import get_client_id, resolve_client_name
from memex_client.exporters.base import BaseExporter
from memex_client.state import SyncState

DEFAULT_GPASTE_DIR = Path.home() / ".local" / "share" / "gpaste"


class GPasteExporter(BaseExporter):
    name = "gpaste"

    def __init__(self, config: dict, state: SyncState, api: MemexAPI) -> None:
        super().__init__(config, state, api)
        self.client_name = resolve_client_name(config)
        self.client_id = get_client_id()

        gpaste_cfg = config.get("sources", {}).get("gpaste", {})
        if isinstance(gpaste_cfg, dict):
            data_dir = gpaste_cfg.get("data_dir", "")
            self.data_dir = Path(data_dir).expanduser() if data_dir else DEFAULT_GPASTE_DIR
        else:
            self.data_dir = DEFAULT_GPASTE_DIR

        self._new_synced_uuids: set[str] = set()

    def collect_new_entries(self) -> list[dict]:
        history_file = self.data_dir / "history.xml"
        if not history_file.exists():
            return []

        saved = self.state.get(self.name)
        synced_uuids = set(saved.get("synced_uuids", []))

        try:
            tree = ET.parse(history_file)
        except ET.ParseError:
            return []

        root = tree.getroot()
        entries = []

        for item in root.iter("item"):
            uuid = item.get("uuid", "")
            if not uuid or uuid in synced_uuids:
                self._new_synced_uuids.add(uuid)
                continue

            kind = item.get("kind", "Text")

            # Content is in <value><![CDATA[...]]></value> child element
            value_elem = item.find("value")
            content = (value_elem.text if value_elem is not None else item.text) or ""

            # Skip image entries for now (v1)
            if kind != "Text":
                self._new_synced_uuids.add(uuid)
                continue

            # GPaste has no timestamps — use file mtime
            mtime = int(history_file.stat().st_mtime)

            entries.append({
                "content": content,
                "kind": "Text",
                "client": self.client_name,
            "client_id": self.client_id,
                "timestamp": mtime,
            })
            self._new_synced_uuids.add(uuid)

        return entries

    def _post(self, entries: list[dict]) -> int:
        return self.api.post_clipboard(entries)

    def _save_state(self) -> None:
        self.state.update(self.name, {
            "synced_uuids": sorted(self._new_synced_uuids),
            "last_sync_time": datetime.now(timezone.utc).isoformat(),
        })
