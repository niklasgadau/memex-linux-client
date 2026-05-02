from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import click
import httpx

from memex_client.api import AuthError, MemexAPI
from memex_client.config import (
    CONFIG_PATH,
    auth_mode,
    get_client_id,
    load_config,
    resolve_client_name,
    update_auth,
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


def _warn_if_legacy(cfg: dict) -> None:
    if auth_mode(cfg) == "legacy-jwt":
        click.echo(
            "Warning: using legacy cf-access-token (24h expiry). "
            "Run 'memex-client auth' to migrate to a service token.",
            err=True,
        )


@click.group()
def main() -> None:
    """Memex Linux Client — sync shell history & clipboard to memex-server."""


@main.command()
@click.argument("source", required=False, default=None)
@click.option("--full", is_flag=True, help="Reset local state and re-sync everything.")
def sync(source: str | None, full: bool) -> None:
    """Run exporters to sync data to memex-server.

    Optionally specify a single source: fish, bash, gpaste.
    """
    cfg = load_config()
    _warn_if_legacy(cfg)
    state = SyncState()
    api = MemexAPI.from_config(cfg)

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

    if full:
        for name in sources_to_sync:
            state.reset(name)
        click.echo("State reset — running full sync.")

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


CF_API_BASE = "https://api.cloudflare.com/client/v4"


def _create_service_token_via_cf_api(
    account_id: str, api_token: str, name: str
) -> tuple[str, str]:
    """Call CF API to create a new service token. Returns (client_id, client_secret).

    The secret is returned by CF only on creation — never persisted by CF, never
    retrievable later. We hand it straight to the caller; storage is its problem.
    """
    url = f"{CF_API_BASE}/accounts/{account_id}/access/service_tokens"
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        json={"name": name},
        timeout=30.0,
    )
    if resp.status_code == 401:
        raise click.ClickException(
            "Cloudflare API rejected the API token (401). "
            "Check it has 'Account → Access: Service Tokens → Edit' permission."
        )
    if resp.status_code == 404:
        raise click.ClickException(
            f"Cloudflare API returned 404 — account ID '{account_id}' not found "
            "or token lacks access to it."
        )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Cloudflare API error {resp.status_code}: {resp.text[:300]}"
        )

    data = resp.json()
    if not data.get("success"):
        errors = data.get("errors", [])
        raise click.ClickException(f"Cloudflare API call failed: {errors}")

    result = data["result"]
    return result["client_id"], result["client_secret"]


def _auth_wizard() -> tuple[str, str]:
    """Interactive flow to obtain a service-token (client_id, client_secret).

    Two branches:
      [1] User already has a token → just collect it
      [2] User wants to create one → call CF API on their behalf

    Branch [2] requires a Cloudflare API token with the 'Access: Service Tokens'
    edit permission. We do NOT persist the API token; it's used once and dropped.
    """
    click.echo("\nCloudflare Service Token")
    click.echo("------------------------")
    click.echo("  [1] I already have a Client-ID + Client-Secret")
    click.echo("  [2] Create a new service token via the Cloudflare API")
    choice = click.prompt("Choose", type=click.Choice(["1", "2"]), default="1")

    if choice == "1":
        client_id = click.prompt("CF-Access-Client-Id (ends in '.access')")
        client_secret = click.prompt("CF-Access-Client-Secret", hide_input=True)
        return client_id.strip(), client_secret.strip()

    click.echo(
        "\nThis will call the Cloudflare API to create a new service token."
    )
    click.echo(
        "You need a Cloudflare API token with permission "
        "'Account → Access: Service Tokens → Edit'."
    )
    click.echo("Create one at: https://dash.cloudflare.com/profile/api-tokens\n")

    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if api_token:
        click.echo("Using CLOUDFLARE_API_TOKEN from environment.")
    else:
        api_token = click.prompt("Cloudflare API Token", hide_input=True)

    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if account_id:
        click.echo(f"Using CLOUDFLARE_ACCOUNT_ID from environment: {account_id}")
    else:
        account_id = click.prompt("Cloudflare Account ID")

    default_name = f"memex-linux-{socket.gethostname()}"
    token_name = click.prompt("Token name", default=default_name)

    click.echo("\nCreating service token...")
    client_id, client_secret = _create_service_token_via_cf_api(
        account_id.strip(), api_token.strip(), token_name.strip()
    )
    click.echo(f"  Created: {client_id}")
    click.echo("  (the secret is shown only once and stored in your config)")
    return client_id, client_secret


def _print_policy_hint() -> None:
    click.echo("")
    click.echo("If the test still fails with 403/forbidden:")
    click.echo("  1. Open https://one.dash.cloudflare.com/")
    click.echo("  2. Zero Trust → Access → Applications → memex → Edit")
    click.echo("  3. Policies → add or edit a policy")
    click.echo("  4. Include → 'Service Auth' → select your token name")
    click.echo("  5. Save and re-run 'memex-client status'")


def _test_auth(api_url: str, client_id: str, client_secret: str) -> None:
    api = MemexAPI(api_url, cf_client_id=client_id, cf_client_secret=client_secret)
    try:
        api.probe()
        click.echo("  Server reachable and authorized.")
    except AuthError as e:
        click.echo(f"  {e.mode}: {e}", err=True)
        if e.mode == "forbidden":
            _print_policy_hint()
        elif e.mode == "invalid":
            click.echo(
                "  → Credentials look syntactically present but CF Access rejected them. "
                "Double-check Client-ID/Secret.",
                err=True,
            )
    finally:
        api.close()


@main.command()
def auth() -> None:
    """Refresh Cloudflare Access credentials (service token).

    Use this to rotate the token or migrate from a legacy api_token.
    """
    cfg = load_config()
    click.echo(f"Server: {cfg['api_url']}")
    click.echo(f"Current auth mode: {auth_mode(cfg)}")

    client_id, client_secret = _auth_wizard()
    update_auth(client_id, client_secret)
    click.echo(f"\nCredentials written to {CONFIG_PATH}")

    click.echo("\nTesting connection...")
    _test_auth(cfg["api_url"], client_id, client_secret)


@main.command()
def setup() -> None:
    """Interactive first-time configuration."""
    click.echo("Memex Client Setup\n")

    api_url = click.prompt("Server URL", default="http://localhost:8080")

    client_id, client_secret = _auth_wizard()

    client_name = click.prompt("\nClient name", default=socket.gethostname())

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

    click.echo(f"\nTesting connection to {api_url}...")
    _test_auth(api_url, client_id, client_secret)

    notifications = {
        "enabled": click.confirm("\nEnable desktop notifications on sync failure?", default=True)
    }

    write_config(
        api_url=api_url,
        client=client_name,
        sources=sources,
        notifications=notifications,
        cf_client_id=client_id,
        cf_client_secret=client_secret,
    )
    click.echo(f"\nConfig written to {CONFIG_PATH}")

    if click.confirm("Install systemd timer for background sync?", default=True):
        _install_timer()


@main.command()
def status() -> None:
    """Show configuration, sync state, and timer status."""
    cfg = load_config()
    state = SyncState()
    mode = auth_mode(cfg)

    click.echo("Configuration:")
    click.echo(f"  Config file:  {CONFIG_PATH}")
    click.echo(f"  Server:       {cfg['api_url']}")
    click.echo(f"  Client:       {resolve_client_name(cfg)}")
    click.echo(f"  Client ID:    {get_client_id()}")
    click.echo(f"  Auth mode:    {mode}")
    if mode == "legacy-jwt":
        click.echo("                (deprecated — run 'memex-client auth' to migrate)")

    sources = cfg.get("sources", {})
    click.echo(f"\nSources:")
    for name in EXPORTERS:
        enabled = _is_source_enabled(sources, name)
        mark = "enabled" if enabled else "disabled"
        sync_info = state.get(name)
        last = sync_info.get("last_sync_time", "never") if sync_info else "never"
        click.echo(f"  {name:8s}  {mark:10s}  last sync: {last}")

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

    click.echo(f"\nServer:")
    api = MemexAPI.from_config(cfg)
    try:
        api.probe()
        click.echo("  reachable and authorized")
    except AuthError as e:
        click.echo(f"  {e.mode}: {e}")
    finally:
        api.close()


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
