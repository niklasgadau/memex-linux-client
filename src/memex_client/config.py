from __future__ import annotations

import os
import socket
import tomllib
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memex"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULTS = {
    "api_url": "http://localhost:8080",
    "api_token": "",
    "client": socket.gethostname(),
    "sources": {"fish": False, "bash": False, "gpaste": False},
    "notifications": {"enabled": True},
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    # Merge with defaults
    merged = dict(DEFAULTS)
    merged.update(cfg)
    if "sources" in cfg:
        merged["sources"] = {**DEFAULTS["sources"], **cfg["sources"]}
    if "notifications" in cfg:
        merged["notifications"] = {**DEFAULTS["notifications"], **cfg["notifications"]}
    return merged


def resolve_client_name(cfg: dict) -> str:
    return os.environ.get("MEMEX_CLIENT") or cfg.get("client") or socket.gethostname()


def write_config(
    api_url: str,
    api_token: str,
    client: str,
    sources: dict,
    notifications: dict | None = None,
) -> None:
    if notifications is None:
        notifications = {"enabled": True}

    lines = [
        f'api_url = "{api_url}"',
        f'api_token = "{api_token}"',
        f'client = "{client}"',
        "",
        "[sources]",
        f"fish = {str(sources.get('fish', False)).lower()}",
        f"bash = {str(sources.get('bash', False)).lower()}",
        f"gpaste = {str(sources.get('gpaste', False)).lower()}",
    ]

    gpaste_cfg = sources.get("gpaste_config", {})
    if gpaste_cfg.get("data_dir"):
        lines.append("")
        lines.append("[sources.gpaste]")
        lines.append(f'data_dir = "{gpaste_cfg["data_dir"]}"')

    lines.append("")
    lines.append("[notifications]")
    lines.append(f"enabled = {str(notifications.get('enabled', True)).lower()}")
    lines.append("")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines))
