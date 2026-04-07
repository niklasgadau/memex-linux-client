from __future__ import annotations

from abc import ABC, abstractmethod

from memex_client.api import MemexAPI
from memex_client.state import SyncState


class BaseExporter(ABC):
    name: str

    def __init__(self, config: dict, state: SyncState, api: MemexAPI) -> None:
        self.config = config
        self.state = state
        self.api = api

    @abstractmethod
    def collect_new_entries(self) -> list[dict]:
        ...

    @abstractmethod
    def _post(self, entries: list[dict]) -> int:
        ...

    def sync(self) -> int:
        entries = self.collect_new_entries()
        if not entries:
            return 0
        count = self._post(entries)
        self._save_state()
        return count

    @abstractmethod
    def _save_state(self) -> None:
        ...
