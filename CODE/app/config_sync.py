"""
config_sync.py
---------------
Phase 5D: the one live connection that keeps the shared configuration current.

What it does
* Opens ONE live listener on `ksm/config` (one connection for the prompts, the active choice, the common settings and the
  device nodes together; old prompt versions live elsewhere and are not downloaded).
* Every change that arrives is handed to `PromptStore.apply_snapshot` and `SharedSettings.apply_snapshot`, so an edited
  prompt block or a changed shared setting reaches the NEXT AI call without a restart (`config.reload()` is run after a
  setting was applied).
* At start-up, in a background thread, runs the two `sync()` routines (read, send what waits, seed what is missing).
* Does nothing at all while both switches (`SYNC_PROMPTS`, `SYNC_SETTINGS`) are off or Firebase is not configured: no
  connection, no read, no write. Turning a switch on or off, or changing the Firebase settings, is picked up live.
* Never blocks the app: `start()` returns at once, the listener's events are applied on a worker thread (a burst of
  events is merged into the newest one), and a failed connection is retried with a growing pause.

It holds no data of its own. The consumers read prompts through `app/prompt_use.py`.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from app import cloud_sync, config, prompt_store, settings_store, shared_settings
from app.logger import get_logger

log = get_logger(__name__)

CONFIG_ROOT = f"{cloud_sync.ROOT}/config"
# Settings whose change can open or close the connection (the two switches and the three that reach Firebase).
WATCHED_KEYS = frozenset({"SYNC_PROMPTS", "SYNC_SETTINGS", "ENABLE_FIREBASE", "FIREBASE_DATABASE_URL",
                          "FIREBASE_SERVICE_ACCOUNT_PATH"})


def _same(a: Any, b: Any) -> bool:
    try:
        return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return False


class ConfigSync:
    def __init__(self, prompts=None, shared=None, cloud=None, background: bool = True,
                 retry_s: Optional[float] = 30.0, max_retry_s: float = 300.0):
        self._prompts = prompts
        self._shared = shared
        self._cloud = cloud
        self._background = background
        self._retry_s = retry_s                  # first pause before a retry; None = never retry (tests)
        self._max_retry_s = max_retry_s
        self._lock = threading.RLock()
        self._reconcile_lock = threading.Lock()
        self._kick_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._again = False
        self._apply_lock = threading.Lock()
        self._apply_thread: Optional[threading.Thread] = None
        self._waiting: Optional[tuple] = None    # (tree,) the newest snapshot not applied yet
        self._handle = None
        self._timer: Optional[threading.Timer] = None
        self._delay = 0.0
        self._started = False
        self._stopped = False
        self._error = ""
        self._events = 0
        self._last_event_ms = 0
        self._last_prompts: Any = None
        self._last_settings: Any = None

    # ---- the parts ---------------------------------------------------------------------------------
    @property
    def prompts(self):
        if self._prompts is None:
            self._prompts = prompt_store.get_store()
        return self._prompts

    @property
    def shared(self):
        if self._shared is None:
            self._shared = shared_settings.get_shared()
        return self._shared

    @property
    def cloud(self):
        if self._cloud is None:
            self._cloud = cloud_sync.FirebaseCloudStore()
        return self._cloud

    def _prompts_on(self) -> bool:
        try:
            return bool(self.prompts.enabled())
        except Exception:  # noqa: BLE001
            return False

    def _settings_on(self) -> bool:
        try:
            return bool(self.shared.enabled())
        except Exception:  # noqa: BLE001
            return False

    def _record_error(self, e: Exception) -> str:
        msg = settings_store.redact_text(str(e) or type(e).__name__)
        with self._lock:
            self._error = msg
        log.info("Live configuration: %s", msg)
        return msg

    # ---- start / stop ---------------------------------------------------------------------------------
    def start(self) -> None:
        """Watches the settings for the switches, and runs the first sync in the background. Returns at once. Never raises."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopped = False
        try:
            self.shared.attach()
            self.shared.store.add_listener(self._on_setting)
        except Exception:  # noqa: BLE001
            log.exception("Could not start watching the settings")
        self._kick()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        self._cancel_retry()
        self._close()

    def _on_setting(self, keys: list) -> None:
        if self._stopped or not (WATCHED_KEYS & set(keys or [])):
            return
        try:
            config.reload(self.shared.store)     # the stores read ENABLE_FIREBASE and the URL from config
        except Exception:  # noqa: BLE001
            log.exception("config.reload failed")
        self._kick()

    # ---- background runs --------------------------------------------------------------------------------
    def _kick(self) -> None:
        if self._stopped:
            return
        if not self._background:
            self._run_safely()
            return
        with self._kick_lock:
            if self._thread is not None and self._thread.is_alive():
                self._again = True               # a run is busy (maybe waiting on a slow network): run once more after it
                return
            self._thread = threading.Thread(target=self._loop, daemon=True, name="config-sync")
            self._thread.start()

    def _loop(self) -> None:
        while True:
            self._run_safely()
            with self._kick_lock:
                if not self._again:
                    return
                self._again = False

    def _run_safely(self) -> None:
        try:
            self.reconcile()
        except Exception:  # noqa: BLE001
            log.exception("Live configuration run failed")

    def reconcile(self) -> dict:
        """Brings the connection in line with the switches: closes it when nothing is switched on, otherwise opens it
        (once) and runs the two sync routines. Blocking; call it from a background thread. Never raises."""
        with self._reconcile_lock:
            if self._stopped:
                return self.status()
            p_on, s_on = self._prompts_on(), self._settings_on()
            if not (p_on or s_on):
                self._cancel_retry()
                self._close()
                with self._lock:
                    self._error = ""
                return self.status()
            problem = ""
            if self._handle is None:
                problem = self._open()
            errors = [problem] if problem else []
            if p_on:
                errors.append(self._safe_sync(self.prompts))
            if s_on:
                errors.append(self._safe_sync(self.shared))
            with self._lock:
                self._last_prompts = self._last_settings = None     # the next event is applied in full
            errors = [e for e in errors if e]
            if errors:
                self._schedule_retry()
            else:
                self._delay = 0.0
                self._cancel_retry()
                with self._lock:
                    self._error = ""
            return self.status()

    @staticmethod
    def _safe_sync(layer) -> str:
        try:
            return str(layer.sync().get("last_error") or "")
        except Exception as e:  # noqa: BLE001
            log.exception("sync failed")
            return str(e) or type(e).__name__

    # ---- the connection ----------------------------------------------------------------------------------
    def _open(self) -> str:
        try:
            handle = self.cloud.listen(CONFIG_ROOT, self._on_tree)
        except Exception as e:  # noqa: BLE001
            return self._record_error(e)
        with self._lock:
            self._handle = handle
            self._error = ""
        return ""

    def _close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            self._waiting = None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                log.exception("Could not close the live connection")

    def _schedule_retry(self) -> None:
        if self._retry_s is None or self._stopped:
            return
        with self._lock:
            self._delay = min(self._max_retry_s, self._retry_s if self._delay <= 0 else self._delay * 2)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._kick)
            self._timer.daemon = True
            self._timer.start()

    def _cancel_retry(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    # ---- events -> stores ------------------------------------------------------------------------------------
    def _on_tree(self, tree: Any) -> None:
        """Called by the listener (on the SDK's thread) with the whole `ksm/config` tree after each change. Only the
        newest tree is kept; it is applied on a worker thread so the connection is never held up."""
        if self._stopped:
            return
        with self._lock:
            self._events += 1
            self._last_event_ms = int(time.time() * 1000)
        if not self._background:
            self._apply(tree)
            return
        with self._apply_lock:
            self._waiting = (tree,)
            if self._apply_thread is not None and self._apply_thread.is_alive():
                return
            self._apply_thread = threading.Thread(target=self._apply_loop, daemon=True, name="config-apply")
            self._apply_thread.start()

    def _apply_loop(self) -> None:
        while True:
            with self._apply_lock:
                item, self._waiting = self._waiting, None
                if item is None:
                    return
            self._apply(item[0])

    def _apply(self, tree: Any) -> None:
        """Hands each layer its part of the tree. Each layer applies only when its own switch is on. Never raises."""
        try:
            tree = tree if isinstance(tree, dict) else {}
            if self._prompts_on():
                part = (tree.get("prompts"), tree.get("prompt_active"))
                if not _same(part, self._last_prompts):
                    try:
                        self.prompts.apply_snapshot(*part)
                        self._last_prompts = part
                    except Exception:  # noqa: BLE001
                        log.exception("Could not apply the prompts from Firebase")
            if self._settings_on():
                settings, devices = tree.get("settings"), tree.get("devices")
                mine = None
                try:
                    mine_node = (devices or {}).get(self.shared.device()["device_id"])
                    mine = mine_node.get("settings") if isinstance(mine_node, dict) else None
                except Exception:  # noqa: BLE001
                    pass
                part = (settings, mine)
                if not _same(part, self._last_settings):
                    try:
                        out = self.shared.apply_snapshot(settings, devices)
                        self._last_settings = part
                        if out.get("applied"):
                            config.reload(self.shared.store)   # the next AI call sees the new values
                    except Exception:  # noqa: BLE001
                        log.exception("Could not apply the settings from Firebase")
        except Exception:  # noqa: BLE001
            log.exception("Could not apply the configuration from Firebase")

    # ---- what the screens will show (5E) ----------------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            listening = self._handle is not None and not getattr(self._handle, "closed", False)
            out = {"listening": listening, "events": self._events, "last_event_ms": self._last_event_ms,
                   "last_error": self._error, "retry_in_s": self._delay if self._timer is not None else 0.0}
        try:
            out["prompts"] = self.prompts.status()
            out["settings"] = self.shared.status()
        except Exception:  # noqa: BLE001
            pass
        return out


# ----------------------------------------------------------------------------
# Shared instance
# ----------------------------------------------------------------------------

_hub: Optional[ConfigSync] = None
_hub_lock = threading.Lock()


def get_hub() -> ConfigSync:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = ConfigSync()
        return _hub


def start_config_sync() -> Optional[ConfigSync]:
    """Called once at start-up. Returns at once; everything slow runs in the background. Never raises."""
    try:
        hub = get_hub()
        hub.start()
        return hub
    except Exception:  # noqa: BLE001
        log.exception("Could not start the live configuration")
        return None


def reset_hub_for_tests() -> None:
    global _hub
    with _hub_lock:
        hub, _hub = _hub, None
    if hub is not None:
        hub.stop()
