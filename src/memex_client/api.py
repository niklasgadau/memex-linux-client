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
        raise AuthError("unknown", f"HTTP {resp.status_code}: {resp.text[:200]}")

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
