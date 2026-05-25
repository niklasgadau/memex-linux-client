from __future__ import annotations

import httpx


class AuthError(Exception):
    """Raised when CF Access rejects the configured credentials.

    `mode` is one of: 'no-credentials', 'invalid', 'forbidden', 'unknown'.
    The wizard uses this to print actionable guidance.
    """

    def __init__(self, mode: str, message: str) -> None:
        super().__init__(message)
        self.mode = mode


class MemexAPI:
    def __init__(
        self,
        base_url: str,
        cf_client_id: str = "",
        cf_client_secret: str = "",
        legacy_token: str = "",
        timeout: float = 30.0,
    ) -> None:
        headers = {}
        if cf_client_id and cf_client_secret:
            headers["CF-Access-Client-Id"] = cf_client_id
            headers["CF-Access-Client-Secret"] = cf_client_secret
        elif legacy_token:
            headers["cf-access-token"] = legacy_token
        self.client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
            follow_redirects=False,
        )

    @classmethod
    def from_config(cls, cfg: dict, **kwargs) -> "MemexAPI":
        return cls(
            base_url=cfg["api_url"],
            cf_client_id=cfg.get("cf_client_id", ""),
            cf_client_secret=cfg.get("cf_client_secret", ""),
            legacy_token=cfg.get("api_token", ""),
            **kwargs,
        )

    def post_shell(self, items: list[dict]) -> int:
        return self._post("/api/ingest/shell", items)

    def post_clipboard(self, items: list[dict]) -> int:
        return self._post("/api/ingest/clipboard", items)

    def post_claude(self, batch: dict, chunk_size: int = 200, timeout: float = 120.0) -> int:
        """Send a multi-type Claude batch, splitting into smaller POSTs so a
        large initial backfill doesn't exceed httpx/Cloudflare timeouts.

        `batch` is a dict with keys: sessions, turns, recaps, plans, memory,
        inputs — each mapping to a list of items. Empty lists are fine.
        """
        keys = ("sessions", "turns", "recaps", "plans", "memory", "inputs")
        # Build chunks containing up to `chunk_size` items total across types,
        # preserving type assignment so the server still knows what's what.
        chunks: list[dict] = []
        current: dict = {k: [] for k in keys}
        current_count = 0
        for k in keys:
            for item in batch.get(k, []):
                current[k].append(item)
                current_count += 1
                if current_count >= chunk_size:
                    chunks.append(current)
                    current = {kk: [] for kk in keys}
                    current_count = 0
        if current_count > 0:
            chunks.append(current)

        total = 0
        for chunk in chunks:
            resp = self.client.post("/api/ingest/claude", json=chunk, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            total += int(data.get("count", 0))
        return total

    def health_check(self) -> bool:
        """Best-effort reachability check that swallows all errors.

        Use `probe()` instead when you need to know *why* a request fails.
        """
        try:
            self.probe()
            return True
        except (httpx.HTTPError, AuthError):
            return False

    def probe(self) -> None:
        """Hit /api/stats and raise AuthError with a categorized mode on failure."""
        try:
            resp = self.client.get("/api/stats")
        except httpx.ConnectError as e:
            raise AuthError("unknown", f"connection failed: {e}") from e

        if resp.status_code == 200:
            return
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if "cloudflareaccess.com" in location:
                raise AuthError(
                    "invalid",
                    "Cloudflare Access rejected the request — credentials missing or invalid.",
                )
            raise AuthError("unknown", f"unexpected redirect to {location}")
        if resp.status_code == 403:
            raise AuthError(
                "forbidden",
                "Cloudflare Access reached but policy forbids this token — "
                "attach a Service-Auth policy to the memex application.",
            )
        if resp.status_code in (502, 503, 504):
            raise AuthError(
                "unreachable",
                f"backend unreachable (HTTP {resp.status_code} from Cloudflare — "
                "server likely down or restarting)",
            )
        snippet = " ".join(resp.text.split())[:160]
        raise AuthError("unknown", f"HTTP {resp.status_code}: {snippet}")

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
