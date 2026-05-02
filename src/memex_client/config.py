from __future__ import annotations

import os
import socket
import tomllib
import uuid
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memex"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "memex"
CLIENT_ID_PATH = DATA_DIR / "client_id"

DEFAULTS = {
    "api_url": "http://localhost:8080",
    "cf_client_id": "",
    "cf_client_secret": "",
    "api_token": "",
    "client": socket.gethostname(),
    "sources": {"fish": False, "bash": False, "gpaste": {"enabled": False}},
    "notifications": {"enabled": True},
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    merged = dict(DEFAULTS)
    merged.update(cfg)
    if "sources" in cfg:
        merged["sources"] = {**DEFAULTS["sources"], **cfg["sources"]}
    if "notifications" in cfg:
        merged["notifications"] = {**DEFAULTS["notifications"], **cfg["notifications"]}
    return merged


def auth_mode(cfg: dict) -> str:
    """Return 'service-token', 'legacy-jwt', or 'none' based on configured credentials."""
    if cfg.get("cf_client_id") and cfg.get("cf_client_secret"):
        return "service-token"
    if cfg.get("api_token"):
        return "legacy-jwt"
    return "none"


def get_client_id() -> str:
    """Return a persistent client UUID, creating one on first call.

    This ID is used in API payloads for server-side deduplication.
    It survives re-setup, package reinstall, and config changes.
    """
    if CLIENT_ID_PATH.exists():
        return CLIENT_ID_PATH.read_text().strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    client_id = str(uuid.uuid4())
    CLIENT_ID_PATH.write_text(client_id)
    return client_id


def resolve_client_name(cfg: dict) -> str:
    """Human-readable display name (for status output)."""
    return os.environ.get("MEMEX_CLIENT") or cfg.get("client") or socket.gethostname()


def write_config(
    api_url: str,
    client: str,
    sources: dict,
    notifications: dict | None = None,
    cf_client_id: str = "",
    cf_client_secret: str = "",
    api_token: str = "",
) -> None:
    if notifications is None:
        notifications = {"enabled": True}

    # gpaste is either a bool (from setup wizard) or a {enabled, data_dir} dict
    # (from re-loaded config). Normalize both shapes here.
    gpaste_raw = sources.get("gpaste", False)
    if isinstance(gpaste_raw, dict):
        gpaste_enabled = gpaste_raw.get("enabled", False)
        gpaste_cfg = {"data_dir": gpaste_raw["data_dir"]} if "data_dir" in gpaste_raw else {}
    else:
        gpaste_enabled = bool(gpaste_raw)
        gpaste_cfg = sources.get("gpaste_config", {})

    lines = [
        f'api_url = "{api_url}"',
        f'cf_client_id = "{cf_client_id}"',
        f'cf_client_secret = "{cf_client_secret}"',
    ]
    if api_token:
        lines.append(f'api_token = "{api_token}"')
    lines += [
        f'client = "{client}"',
        "",
        "[sources]",
        f"fish = {str(sources.get('fish', False)).lower()}",
        f"bash = {str(sources.get('bash', False)).lower()}",
        "",
        "[sources.gpaste]",
        f"enabled = {str(bool(gpaste_enabled)).lower()}",
    ]

    if gpaste_cfg.get("data_dir"):
        lines.append(f'data_dir = "{gpaste_cfg["data_dir"]}"')

    lines.append("")
    lines.append("[notifications]")
    lines.append(f"enabled = {str(notifications.get('enabled', True)).lower()}")
    lines.append("")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines))


def update_auth(cf_client_id: str, cf_client_secret: str) -> None:
    """Update only the auth credentials in config.toml, preserving everything else.

    Used by the auth-rotation wizard so users can refresh credentials without
    re-running the full setup.
    """
    cfg = load_config()
    write_config(
        api_url=cfg["api_url"],
        client=cfg.get("client", socket.gethostname()),
        sources=cfg.get("sources", {}),
        notifications=cfg.get("notifications", {}),
        cf_client_id=cf_client_id,
        cf_client_secret=cf_client_secret,
        api_token="",
    )
