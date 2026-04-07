from __future__ import annotations

import httpx


class MemexAPI:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0) -> None:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)

    def post_shell(self, items: list[dict]) -> int:
        return self._post("/api/ingest/shell", items)

    def post_clipboard(self, items: list[dict]) -> int:
        return self._post("/api/ingest/clipboard", items)

    def health_check(self) -> bool:
        try:
            r = self.client.get("/api/stats")
            return r.status_code == 200
        except httpx.ConnectError:
            return False

    def _post(self, endpoint: str, items: list[dict]) -> int:
        total = 0
        for i in range(0, len(items), 500):
            chunk = items[i : i + 500]
            resp = self.client.post(endpoint, json=chunk)
            resp.raise_for_status()
            total += len(chunk)
        return total

    def close(self) -> None:
        self.client.close()
