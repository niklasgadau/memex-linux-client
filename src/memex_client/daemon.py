from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from memex_client.api import MemexAPI
from memex_client.config import get_client_id, resolve_client_name
from memex_client.state import SyncState

logger = logging.getLogger("memex.daemon")

GPASTE_BUS_NAME = "org.gnome.GPaste"
GPASTE_OBJECT_PATH = "/org/gnome/GPaste"
GPASTE_INTERFACE = "org.gnome.GPaste2"

HEARTBEAT_SECONDS = 300
RECONNECT_BACKOFF_SECONDS = 30


class ClipboardDaemon:
    """Live-pushes GPaste clipboard entries to memex-server.

    Listens for the GPaste `Update` DBus signal and pushes new history entries
    immediately. Also reconciles every 5 minutes (heartbeat) to recover from any
    missed signals or server-side outages, and reconnects when the GPaste daemon
    restarts (NameOwnerChanged).
    """

    state_key = "gpaste"  # share state with the periodic gpaste exporter

    def __init__(self, config: dict, state: SyncState, api: MemexAPI) -> None:
        self.config = config
        self.state = state
        self.api = api
        self.client_name = resolve_client_name(config)
        self.client_id = get_client_id()

        saved = self.state.get(self.state_key)
        self._known_uuids: set[str] = set(saved.get("synced_uuids", []))

        self.bus: dbus.SessionBus | None = None
        self.iface = None
        self.signal_match = None
        self.loop: GLib.MainLoop | None = None

    # --- Lifecycle ---------------------------------------------------------

    def run(self) -> None:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus()

        # Track GPaste daemon coming and going
        self.bus.watch_name_owner(GPASTE_BUS_NAME, self._on_owner_changed)

        # Initial connect attempt (no error if GPaste not running yet)
        self._try_connect()

        # Heartbeat: catches missed events and recovers from transient failures
        GLib.timeout_add_seconds(HEARTBEAT_SECONDS, self._heartbeat)

        # Clean shutdown on SIGTERM/SIGINT
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._stop)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._stop)

        self.loop = GLib.MainLoop()
        logger.info("memex clipboard daemon started")
        try:
            self.loop.run()
        finally:
            logger.info("memex clipboard daemon stopped")
            self._teardown_signal_match()

    def _stop(self) -> bool:
        logger.info("received termination signal")
        if self.loop:
            self.loop.quit()
        return False  # don't reschedule

    # --- DBus connection management ----------------------------------------

    def _try_connect(self) -> None:
        try:
            obj = self.bus.get_object(GPASTE_BUS_NAME, GPASTE_OBJECT_PATH)
            self.iface = dbus.Interface(obj, GPASTE_INTERFACE)
            self.signal_match = obj.connect_to_signal(
                "Update", self._on_update, dbus_interface=GPASTE_INTERFACE
            )
            logger.info("connected to GPaste, performing initial reconcile")
        except dbus.exceptions.DBusException as e:
            logger.warning("GPaste not reachable: %s", e)
            self.iface = None
            self.signal_match = None
            return

        # Catch up after connect — captures anything posted while we were offline
        self._reconcile_and_push()

    def _on_owner_changed(self, new_owner: str) -> None:
        if new_owner == "":
            logger.info("GPaste daemon went away")
            self._teardown_signal_match()
            self.iface = None
        else:
            logger.info("GPaste daemon appeared (owner=%s)", new_owner)
            self._try_connect()

    def _teardown_signal_match(self) -> None:
        if self.signal_match is not None:
            try:
                self.signal_match.remove()
            except Exception:
                pass
            self.signal_match = None

    # --- Sync logic --------------------------------------------------------

    def _on_update(self, action, target, index) -> None:
        # Action is "REPLACE" / "REMOVE" / etc. Regardless of the kind, we
        # diff the current history against our known set — that's robust to
        # missed signals.
        try:
            self._reconcile_and_push()
        except Exception:
            logger.exception("reconcile_and_push raised")

    def _heartbeat(self) -> bool:
        if self.iface is None:
            self._try_connect()
        else:
            try:
                self._reconcile_and_push()
            except Exception:
                logger.exception("heartbeat reconcile failed")
        return True  # keep firing

    def _reconcile_and_push(self) -> None:
        if self.iface is None:
            return

        try:
            history = self.iface.GetHistory()  # dbus.Array of struct(s, s)
        except dbus.exceptions.DBusException as e:
            logger.warning("GetHistory failed: %s", e)
            return

        current_uuids: set[str] = set()
        new_text_pairs: list[tuple[str, str]] = []  # (uuid, content)
        skipped_non_text: set[str] = set()

        for pair in history:
            uuid = str(pair[0])
            current_uuids.add(uuid)
            if uuid in self._known_uuids:
                continue

            try:
                kind = str(self.iface.GetElementKind(uuid))
            except dbus.exceptions.DBusException as e:
                logger.warning("GetElementKind(%s) failed: %s", uuid, e)
                continue

            if kind != "Text":
                skipped_non_text.add(uuid)
                continue

            try:
                content = str(self.iface.GetRawElement(uuid))
            except dbus.exceptions.DBusException as e:
                logger.warning("GetRawElement(%s) failed: %s", uuid, e)
                continue

            new_text_pairs.append((uuid, content))

        if not new_text_pairs and not skipped_non_text and current_uuids == self._known_uuids:
            return  # nothing to do — no churn

        if new_text_pairs:
            timestamp = int(time.time())
            entries = [
                {
                    "content": content,
                    "kind": "Text",
                    "client": self.client_name,
                    "client_id": self.client_id,
                    "timestamp": timestamp,
                }
                for _, content in new_text_pairs
            ]
            try:
                self.api.post_clipboard(entries)
                logger.info("pushed %d new clipboard entries", len(entries))
            except Exception as e:
                logger.warning("post_clipboard failed (will retry on next event): %s", e)
                return  # state NOT advanced — retry on next signal/heartbeat

        # State advancement: track only currently-present UUIDs (bounds growth
        # at the GPaste history-size limit, ~205 entries by default).
        self._known_uuids = current_uuids
        self._save_state()

    def _save_state(self) -> None:
        self.state.update(self.state_key, {
            "synced_uuids": sorted(self._known_uuids),
            "last_sync_time": datetime.now(timezone.utc).isoformat(),
        })


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
