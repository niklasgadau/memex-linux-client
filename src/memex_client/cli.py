from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import click

from memex_client.api import MemexAPI
from memex_client.config import (
    CONFIG_PATH,
    get_client_id,
    load_config,
    resolve_client_name,
    write_config,
)
from memex_client.state import SyncState


EXPORTERS = {
    "fish": "memex_client.exporters.fish:FishExporter",
    "bash": "memex_client.exporters.bash:BashExporter",
    "gpaste": "memex_client.exporters.gpaste:GPasteExporter",
}


def _load_exporter(name: str):
    module_path, class_name = EXPORTERS[name].rsplit(":", 1)
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _is_source_enabled(sources_cfg: dict, name: str) -> bool:
    val = sources_cfg.get(name, False)
    if isinstance(val, dict):
        return val.get("enabled", False)
    return bool(val)


@click.group()
def main() -> None:
    """Memex Linux Client — sync shell history & clipboard to memex-server."""


@main.command()
@click.argument("source", required=False, default=None)
def sync(source: str | None) -> None:
    """Run exporters to sync data to memex-server.

    Optionally specify a single source: fish, bash, gpaste.
    """
    cfg = load_config()
    state = SyncState()
    api = MemexAPI(cfg["api_url"], cfg.get("api_token", ""))

    sources_cfg = cfg.get("sources", {})

    if source:
        if source not in EXPORTERS:
            click.echo(f"Unknown source: {source}. Choose from: {', '.join(EXPORTERS)}", err=True)
            sys.exit(1)
        sources_to_sync = [source]
    else:
        sources_to_sync = [s for s in EXPORTERS if _is_source_enabled(sources_cfg, s)]

    if not sources_to_sync:
        click.echo("No sources enabled. Run 'memex-client setup' first.", err=True)
        sys.exit(1)

    errors = False
    for name in sources_to_sync:
        try:
            exporter_cls = _load_exporter(name)
            exporter = exporter_cls(cfg, state, api)
            count = exporter.sync()
            click.echo(f"  {name}: {count} entries synced")
        except Exception as e:
            click.echo(f"  {name}: error — {e}", err=True)
            errors = True

    api.close()

    if errors:
        _notify_on_failure(cfg)
        sys.exit(1)


@main.command()
def setup() -> None:
    """Interactive first-time configuration."""
    click.echo("Memex Client Setup\n")

    # Server URL
    api_url = click.prompt("Server URL", default="http://localhost:8080")

    # API Token
    api_token = click.prompt("API Token", default="", show_default=False)

    # Client name
    client_name = click.prompt("Client name", default=socket.gethostname())

    # Auto-detect sources
    sources: dict = {}
    click.echo("\nDetected sources:")

    fish_history = Path.home() / ".local" / "share" / "fish" / "fish_history"
    if fish_history.exists():
        sources["fish"] = click.confirm("  Fish history found — enable?", default=True)
    else:
        click.echo("  Fish history — not found")
        sources["fish"] = False

    bash_history = Path.home() / ".bash_history"
    if bash_history.exists():
        sources["bash"] = click.confirm("  Bash history found — enable?", default=True)
    else:
        click.echo("  Bash history — not found")
        sources["bash"] = False

    gpaste_dir = Path.home() / ".local" / "share" / "gpaste"
    if gpaste_dir.exists():
        sources["gpaste"] = click.confirm("  GPaste found — enable?", default=True)
        if sources["gpaste"]:
            custom_dir = click.prompt(
                "  GPaste data directory",
                default=str(gpaste_dir),
            )
            sources["gpaste_config"] = {"data_dir": custom_dir}
    else:
        click.echo("  GPaste — not found")
        sources["gpaste"] = False

    # Test connectivity
    click.echo(f"\nTesting connection to {api_url}...")
    api = MemexAPI(api_url, api_token)
    if api.health_check():
        click.echo("  Server reachable!")
    else:
        click.echo("  Server not reachable — config will be saved anyway.")
    api.close()

    # Notifications
    notifications = {
        "enabled": click.confirm("\nEnable desktop notifications on sync failure?", default=True)
    }

    # Write config
    write_config(api_url, api_token, client_name, sources, notifications)
    click.echo(f"\nConfig written to {CONFIG_PATH}")

    # Offer to install timer
    if click.confirm("Install systemd timer for background sync?", default=True):
        _install_timer()


@main.command()
def status() -> None:
    """Show configuration, sync state, and timer status."""
    cfg = load_config()
    state = SyncState()

    click.echo("Configuration:")
    click.echo(f"  Config file:  {CONFIG_PATH}")
    click.echo(f"  Server:       {cfg['api_url']}")
    click.echo(f"  Client:       {resolve_client_name(cfg)}")
    click.echo(f"  Client ID:    {get_client_id()}")
    click.echo(f"  Token:        {'set' if cfg.get('api_token') else 'not set'}")

    sources = cfg.get("sources", {})
    click.echo(f"\nSources:")
    for name in EXPORTERS:
        enabled = _is_source_enabled(sources, name)
        mark = "enabled" if enabled else "disabled"
        sync_info = state.get(name)
        last = sync_info.get("last_sync_time", "never") if sync_info else "never"
        click.echo(f"  {name:8s}  {mark:10s}  last sync: {last}")

    # Timer status
    click.echo(f"\nSystemd timer:")
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "memex-sync.timer"],
            capture_output=True, text=True,
        )
        timer_status = result.stdout.strip() or "not installed"
    except FileNotFoundError:
        timer_status = "systemctl not found"
    click.echo(f"  {timer_status}")

    # Server connectivity
    click.echo(f"\nServer:")
    api = MemexAPI(cfg["api_url"], cfg.get("api_token", ""))
    reachable = api.health_check()
    api.close()
    click.echo(f"  {'reachable' if reachable else 'not reachable'}")


SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"

# Resolve source dir for systemd units: installed .deb or local dev
_PACKAGE_SYSTEMD = Path("/usr/lib/systemd/user")
_LOCAL_SYSTEMD = Path(__file__).resolve().parent.parent.parent / "systemd"


def _systemd_source_dir() -> Path:
    if (_PACKAGE_SYSTEMD / "memex-sync.service").exists():
        return _PACKAGE_SYSTEMD
    return _LOCAL_SYSTEMD


def _install_timer() -> None:
    source = _systemd_source_dir()
    service_src = source / "memex-sync.service"
    timer_src = source / "memex-sync.timer"

    if not service_src.exists():
        click.echo(f"Systemd units not found at {source}", err=True)
        return

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    import shutil
    shutil.copy2(service_src, SYSTEMD_USER_DIR / "memex-sync.service")
    shutil.copy2(timer_src, SYSTEMD_USER_DIR / "memex-sync.timer")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "memex-sync.timer"], check=True)
    click.echo("Systemd timer installed and started.")


@main.command()
def install() -> None:
    """Install and enable the systemd user timer for background sync."""
    _install_timer()


@main.command()
def uninstall() -> None:
    """Disable and remove the systemd user timer."""
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", "memex-sync.timer"],
        check=False,
    )
    for name in ("memex-sync.service", "memex-sync.timer"):
        unit = SYSTEMD_USER_DIR / name
        if unit.exists():
            unit.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    click.echo("Systemd timer removed.")


def _notify_on_failure(cfg: dict) -> None:
    notifications = cfg.get("notifications", {})
    if not notifications.get("enabled", True):
        return
    try:
        from memex_client.notify import notify
        notify("Memex Sync", "Sync failed — check 'memex-client status'", urgency="low")
    except Exception:
        pass
