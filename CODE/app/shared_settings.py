"""
shared_settings.py
------------------
Settings in Firebase (Phase 5, step 5C). No UI, no AI calls.

It keeps the COMMON and PER-DEVICE settings of this installation in step with the owner's own Firebase, under
`ksm/config`, and registers this installation as a device.

  COMMON      -> ksm/config/settings/<KEY>                               one value for every client
  PER-DEVICE  -> ksm/config/devices/<deviceId>/settings/<KEY>            one value for this device
  LOCAL ONLY and SECRET settings (the two API keys, the four settings needed to reach Firebase, the device id)
  are NEVER written to Firebase and NEVER read from it, even if someone put them there.

Each setting is stored as {value, updated_at, updated_by}. `updated_at` is the server clock.

What it does
* `seed()` copies this device's values up ONLY for keys that are missing in Firebase. It never overwrites.
* `refresh()` / `apply_snapshot()` bring Firebase values into the saved local value (which is also the offline copy) and
  apply them live through the settings store (its listeners run, `config.reload()` follows). When Firebase and this
  device differ, Firebase wins; the settings store keeps a backup of the old file and a history line.
* A local edit is saved locally at once (the settings store does that) and queued here. `flush()` sends it in a
  transaction that checks `updated_at` is still the one this device last saw. If another client changed the setting
  meanwhile, the other value wins, it is applied here, and a NOTICE is kept for the Settings tab (`notices()`).
* An environment variable still wins on its device, is never uploaded (not on edit, not in the seed), and a
  value that Firebase sends is still kept as the saved value underneath it.
* Values that contain a saved API key (or the shape of one) are never uploaded and never accepted.

Rules this module lives by (same as cloud_sync and prompt_store)
* Offline first. Every public method catches cloud failures: nothing raises into the UI and a local change is never
  lost. A change made while offline waits in `shared_settings_state.json` (the config folder) and goes out later.
* Switch off ("Keep settings online", `SYNC_SETTINGS`, local only) or no Firebase configured: no cloud call at all,
  nothing queued, nothing read.
* A local edit never waits for the network: the store's listener only queues, and sending runs in a background thread.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app import cloud_sync, config, settings_store
from app.cloud_sync import SERVER_TIME, TransactionAbort
from app.logger import get_logger
from app.settings_store import (BY_KEY, SCOPE_COMMON, SCOPE_PER_DEVICE, keys_with_scope, validate_value)

log = get_logger(__name__)

CONFIG_ROOT = f"{cloud_sync.ROOT}/config"
COMMON_ROOT = f"{CONFIG_ROOT}/settings"
DEVICES_ROOT = f"{CONFIG_ROOT}/devices"

STATE_FILE = "shared_settings_state.json"
STATE_VERSION = 1
MAX_VALUE_CHARS = 2000      # the rules file allows this much
MAX_NOTICES = 50
GATE_KEY = "SYNC_SETTINGS"  # local only: the switch for this whole layer


def _now_ms() -> int:
    return int(time.time() * 1000)


def shareable_keys() -> list:
    """The only settings that may ever be in Firebase: COMMON first, then PER-DEVICE, in schema order.
    Built from the scopes, and a secret kind is excluded again here as a second line of defence."""
    keys = keys_with_scope(SCOPE_COMMON) + keys_with_scope(SCOPE_PER_DEVICE)
    return [k for k in keys if BY_KEY[k].kind != "secret"]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _default_configured() -> bool:
    return bool(config.ENABLE_FIREBASE and config.FIREBASE_DATABASE_URL)


class SharedSettings:
    def __init__(self, store=None, cloud=None, state_path: Optional[Path] = None,
                 switch: Optional[Callable[[], bool]] = None,
                 configured: Optional[Callable[[], bool]] = None,
                 identity: Optional[Callable[[], dict]] = None,
                 clock: Optional[Callable[[], int]] = None,
                 background: bool = True,
                 retry_s: Optional[float] = 30.0, max_retry_s: float = 300.0):
        self._retry_s = retry_s              # first pause before re-sending after a failure; None = never retry (tests)
        self._max_retry_s = max_retry_s
        self._retry_delay = 0.0
        self._retry_timer: Optional[threading.Timer] = None
        self._store = store
        self._cloud = cloud
        self._state_path = Path(state_path) if state_path else None
        self._switch = switch
        self._configured = configured or _default_configured
        self._identity_fn = identity
        self._clock = clock or _now_ms
        self._background = background
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._kick_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._again = ""                     # "" | "flush" | "sync": work asked for while a background run was busy
        self._tl = threading.local()         # .applying is True while WE write a value into the settings store
        self._attached = False
        self._seq = 0
        self._seen: dict[str, int] = {}      # key -> updated_at of the Firebase value this device last saw
        self._seen_value: dict[str, str] = {}
        self._pending: dict[str, dict] = {}  # key -> {"value", "base", "seq"}: saved locally, not in Firebase yet
        self._notices: list[dict] = []
        self._warnings: list[str] = []
        self._last_sync_ms = 0
        self._error = ""
        self._from_cloud = False
        self._load_state()

    # ---- plumbing --------------------------------------------------------------------------------
    @property
    def store(self):
        if self._store is None:
            self._store = settings_store.get_store()
        return self._store

    @property
    def cloud(self):
        if self._cloud is None:
            self._cloud = cloud_sync.FirebaseCloudStore()
        return self._cloud

    def _path(self) -> Path:
        return self._state_path or (Path(self.store.dir) / STATE_FILE)

    def device(self) -> dict:
        """{"device_id", "name"} of this installation (made once, kept in device.json)."""
        if self._identity_fn is not None:
            return self._identity_fn()
        return settings_store.device_identity(Path(self.store.dir))

    def _who(self) -> str:
        try:
            return ("desktop:" + self.device()["device_id"])[:60]
        except Exception:  # noqa: BLE001
            return "desktop"

    def _switch_on(self) -> bool:
        if self._switch is not None:
            return bool(self._switch())
        return str(self.store.get(GATE_KEY)).strip().lower() == "true"

    def enabled(self) -> bool:
        """True when this layer may talk to Firebase: the switch is on and Firebase is configured."""
        try:
            return bool(self._switch_on()) and bool(self._configured())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _safe(fn) -> bool:
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False

    def _record_error(self, e: Exception) -> str:
        msg = settings_store.redact_text(str(e) or type(e).__name__, self._store)
        with self._lock:
            self._error = msg
        log.info("Shared settings: %s", msg)
        return msg

    def _where(self, key: str) -> Optional[str]:
        """The Firebase path for a setting, or None when that setting may never be in Firebase."""
        defn = BY_KEY.get(key)
        if defn is None or defn.kind == "secret":
            return None
        if defn.scope == SCOPE_COMMON:
            return f"{COMMON_ROOT}/{key}"
        if defn.scope == SCOPE_PER_DEVICE:
            return f"{DEVICES_ROOT}/{self.device()['device_id']}/settings/{key}"
        return None

    # ---- state file ------------------------------------------------------------------------------
    def _load_state(self) -> None:
        try:
            path = self._path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("Shared settings state could not be read (%s); starting clean", type(e).__name__)
            return
        if not isinstance(data, dict):
            return
        allowed = set(shareable_keys())
        seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
        seen_value = data.get("seen_value") if isinstance(data.get("seen_value"), dict) else {}
        self._seen = {k: int(v) for k, v in seen.items() if k in allowed and _is_number(v)}
        self._seen_value = {k: v for k, v in seen_value.items() if k in allowed and isinstance(v, str)}
        pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
        for k, e in pending.items():
            if k in allowed and isinstance(e, dict) and isinstance(e.get("value"), str) and _is_number(e.get("base")):
                self._seq += 1
                self._pending[k] = {"value": e["value"], "base": int(e["base"]), "seq": self._seq}
        notices = data.get("notices") if isinstance(data.get("notices"), list) else []
        self._notices = [n for n in notices if isinstance(n, dict) and n.get("key") in allowed][-MAX_NOTICES:]
        if _is_number(data.get("last_sync_ms")):
            self._last_sync_ms = int(data["last_sync_ms"])

    def _persist(self) -> None:
        with self._lock:
            payload = json.dumps({
                "version": STATE_VERSION, "seen": self._seen, "seen_value": self._seen_value,
                "pending": {k: {"value": e["value"], "base": e["base"]} for k, e in self._pending.items()},
                "notices": self._notices, "last_sync_ms": self._last_sync_ms,
            }, ensure_ascii=False)
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".shared-settings-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            log.warning("Could not save the shared settings state (%s)", type(e).__name__)

    # ---- is a value allowed to travel? -------------------------------------------------------------------
    def _check_value(self, key: str, value: Any) -> tuple:
        """(ok, tidy value or reason). Used before anything is sent and before anything from Firebase is accepted."""
        if not isinstance(value, str):
            return False, "the value is not text"
        if len(value) > MAX_VALUE_CHARS:
            return False, "the value is too long to share"
        from app import prompt_store      # the same key-text test the prompts use (saved keys and key shapes)
        if prompt_store._secrets_in(value):
            return False, "the value looks like an API key"
        result = validate_value(BY_KEY[key], value)
        if not result.ok:
            return False, result.error or "the value is not valid"
        return True, result.value

    # ---- local edits -> queue -------------------------------------------------------------------------
    def attach(self) -> None:
        """Starts watching the settings store for edits made in this app. Safe to call twice."""
        if not self._attached:
            self._attached = True
            self.store.add_listener(self._on_change)

    def _on_change(self, keys: list) -> None:
        if getattr(self._tl, "applying", False):
            return                                   # our own write of a value that came from Firebase
        try:
            if GATE_KEY in keys and self.enabled():
                self._kick("sync")                   # the owner just switched it on: catch up now
                return
            if not self.enabled():
                return                               # off: nothing queued, nothing sent
            allowed = set(shareable_keys())
            queued = [k for k in keys if k in allowed and self._queue(k)]
            if queued:
                self._persist()
                self._kick("flush")
        except Exception:  # noqa: BLE001  (a settings listener must never break the edit)
            log.exception("Shared settings listener failed")

    def _queue(self, key: str) -> bool:
        entry = self.store.resolve(key)
        if entry.source == "env":
            return False                             # an environment variable is never uploaded
        value = entry.value
        with self._lock:
            pend = self._pending.get(key)
            self._seq += 1
            if pend is not None:
                pend["value"], pend["seq"] = value, self._seq   # several offline edits: the last value, the first base
                return True
            if self._seen_value.get(key) == value:
                return False                         # same as Firebase already: nothing to send
            self._pending[key] = {"value": value, "base": self._seen.get(key, 0), "seq": self._seq}
            return True

    def _kick(self, work: str) -> None:
        """Runs `flush` or `sync` in a background thread so an edit never waits for the network."""
        if not self._background:
            return
        with self._kick_lock:
            if self._thread is not None and self._thread.is_alive():
                if self._again != "sync":
                    self._again = work
                return
            self._thread = threading.Thread(target=self._run, args=(work,), daemon=True, name="shared-settings")
            self._thread.start()

    def _run(self, work: str) -> None:
        while work:
            try:
                self.sync() if work == "sync" else self.flush()
            except Exception:  # noqa: BLE001
                log.exception("Shared settings background run failed")
            with self._kick_lock:
                work, self._again = self._again, ""

    # ---- Firebase -> local --------------------------------------------------------------------------
    def apply_snapshot(self, settings: Any, devices: Any) -> dict:
        """Takes what Firebase holds: `settings` = the node `ksm/config/settings`, `devices` = `ksm/config/devices` (or only
        this device's node inside a dict keyed by its id). Applies every valid COMMON value and every valid PER-DEVICE value
        of THIS device. Any other key is ignored with a warning, whatever it holds. Never raises.
        Returns {"applied": [keys], "restart": [keys needing a restart], "conflicts": [keys], "warnings": [text]}."""
        out = {"applied": [], "restart": [], "conflicts": [], "warnings": []}
        try:
            my_id = self.device()["device_id"]
            common_ok = set(keys_with_scope(SCOPE_COMMON))
            device_ok = set(keys_with_scope(SCOPE_PER_DEVICE))
            mine = devices.get(my_id) if isinstance(devices, dict) else None
            mine_settings = mine.get("settings") if isinstance(mine, dict) else None
            for source, allowed, label in ((settings, common_ok, "common"), (mine_settings, device_ok, "this device")):
                if not isinstance(source, dict):
                    continue
                for key, node in source.items():
                    if key not in allowed:
                        out["warnings"].append(f"{key}: ignored, it is not a {label} setting")
                        continue
                    self._apply_one(key, node, out)
        except Exception as e:  # noqa: BLE001
            out["warnings"].append("settings from Firebase could not be read: " + settings_store.redact_text(str(e), self._store))
        with self._lock:
            self._warnings = list(out["warnings"])
        self._persist()
        return out

    def _apply_one(self, key: str, node: Any, out: dict) -> None:
        if not isinstance(node, dict) or not _is_number(node.get("updated_at")):
            out["warnings"].append(f"{key}: ignored, the stored entry is not in the expected shape")
            return
        ok, value = self._check_value(key, node.get("value"))
        if not ok:
            out["warnings"].append(f"{key}: ignored, {value}")
            return
        at = int(node["updated_at"])
        with self._lock:
            pend = self._pending.get(key)
            if pend is not None:
                if at == pend["base"]:
                    return                           # nothing new in Firebase: my edit still waits to go up
                self._pending.pop(key, None)
                if value != pend["value"]:           # it changed elsewhere meanwhile: theirs wins, I am told
                    self._add_notice(key, pend["value"], value)
                    out["conflicts"].append(key)
                # same value: this is the echo of my own write arriving before the send finished; nothing was lost
            self._seen[key], self._seen_value[key] = at, value
        local, _src = self.store.value_ignoring_env(key)
        if local == value:
            return
        self._tl.applying = True
        try:
            result = self.store.set(key, value)
        finally:
            self._tl.applying = False
        if result.ok:
            out["applied"].append(key)
            if BY_KEY[key].restart:
                out["restart"].append(key)
        else:
            out["warnings"].append(f"{key}: could not be saved here ({result.error})")

    def _add_notice(self, key: str, mine: str, theirs: str) -> None:
        red = lambda t: settings_store.redact_text(t, self._store)  # noqa: E731
        with self._lock:
            self._notices.append({"key": key, "mine": red(mine)[:300], "theirs": red(theirs)[:300], "at_ms": self._clock()})
            self._notices = self._notices[-MAX_NOTICES:]

    def refresh(self) -> dict:
        """Reads this layer from Firebase and applies it. Returns the status."""
        if not self.enabled():
            return self.status()
        try:
            my_id = self.device()["device_id"]
            common = self.cloud.get(COMMON_ROOT)
            mine = self.cloud.get(f"{DEVICES_ROOT}/{my_id}")
        except Exception as e:  # noqa: BLE001
            self._record_error(e)
            return self.status()
        with self._lock:
            self._error, self._from_cloud, self._last_sync_ms = "", True, self._clock()
        self.apply_snapshot(common, {my_id: mine} if mine else {})
        return self.status()

    # ---- local -> Firebase ----------------------------------------------------------------------------
    def register(self) -> dict:
        """Writes this device's entry: name, kind and last_seen (server clock)."""
        if not self.enabled():
            return {"error": ""}
        try:
            ident = self.device()
            base = f"{DEVICES_ROOT}/{ident['device_id']}"
            self.cloud.set(f"{base}/name", ident["name"])
            self.cloud.set(f"{base}/kind", "desktop")
            self.cloud.set(f"{base}/last_seen", SERVER_TIME)
        except Exception as e:  # noqa: BLE001
            return {"error": self._record_error(e)}
        return {"error": ""}

    def seed(self) -> dict:
        """Writes this device's value for every shareable setting that does NOT exist in Firebase yet. Never overwrites.
        A setting set by an environment variable is skipped. Returns {"written": n, "error": ""}."""
        if not self.enabled():
            return {"written": 0, "error": ""}
        written = 0
        try:
            for key in shareable_keys():
                path = self._where(key)
                with self._lock:
                    waiting = key in self._pending
                if path is None or waiting or self.store.resolve(key).source == "env":
                    continue
                value, _src = self.store.value_ignoring_env(key)
                ok, value = self._check_value(key, value)
                if not ok:
                    continue
                who = self._who()

                def fn(current, value=value, who=who):
                    if current is not None:
                        raise TransactionAbort()     # it exists: never overwrite
                    return {"value": value, "updated_at": SERVER_TIME, "updated_by": who}
                committed, _ = self.cloud.transaction(path, fn)
                written += 1 if committed else 0
        except Exception as e:  # noqa: BLE001
            return {"written": written, "error": self._record_error(e)}
        return {"written": written, "error": ""}

    def flush(self) -> dict:
        """Sends every edit that waits. A setting that another client changed meanwhile is not sent: their value wins
        and a notice is kept. Stops at the first cloud failure (the rest stays queued). Returns {"sent", "conflicts", "error"}."""
        result = {"sent": 0, "conflicts": 0, "error": ""}
        if not self.enabled():
            return result
        with self._sync_lock:
            with self._lock:
                todo = sorted(self._pending.items())
            for key, entry in todo:
                try:
                    outcome = self._send_one(key, dict(entry))
                except Exception as e:  # noqa: BLE001
                    result["error"] = self._record_error(e)
                    break
                if outcome == "sent":
                    result["sent"] += 1
                elif outcome == "conflict":
                    result["conflicts"] += 1
            if not result["error"]:
                with self._lock:
                    self._error = ""
                    self._last_sync_ms = self._clock()
        self._persist()
        if result["error"]:
            self._schedule_retry()
        else:
            self._cancel_retry()
        return result

    def _schedule_retry(self) -> None:
        """After a failed send, tries again by itself with a growing pause (30 s, 60 s ... 300 s) until it works."""
        if self._retry_s is None or not self._background:
            return
        with self._lock:
            self._retry_delay = min(self._max_retry_s, self._retry_s if self._retry_delay <= 0 else self._retry_delay * 2)
            if self._retry_timer is not None:
                self._retry_timer.cancel()
            self._retry_timer = threading.Timer(self._retry_delay, self._kick, args=("flush",))
            self._retry_timer.daemon = True
            self._retry_timer.start()

    def _cancel_retry(self) -> None:
        with self._lock:
            timer, self._retry_timer = self._retry_timer, None
            self._retry_delay = 0.0
        if timer is not None:
            timer.cancel()

    def _drop_if_same(self, key: str, seq: int) -> None:
        with self._lock:
            cur = self._pending.get(key)
            if cur is not None and cur["seq"] == seq:
                self._pending.pop(key, None)

    def _send_one(self, key: str, entry: dict) -> str:
        path = self._where(key)
        ok, value = self._check_value(key, entry["value"])
        if path is None or not ok:
            self._drop_if_same(key, entry["seq"])    # never send it; the local value stays as it is
            with self._lock:
                self._warnings.append(f"{key}: not shared, {value if not ok else 'it may not be stored online'}")
            return "skipped"
        who, base = self._who(), entry["base"]

        def fn(current):
            if isinstance(current, dict) and _is_number(current.get("updated_at")) and int(current["updated_at"]) != base:
                raise TransactionAbort()             # changed elsewhere since this device last saw it
            return {"value": value, "updated_at": SERVER_TIME, "updated_by": who}
        committed, now = self.cloud.transaction(path, fn)
        if not committed:
            theirs = now if isinstance(now, dict) else self.cloud.get(path)
            return self._lost(key, entry, theirs)
        at = now.get("updated_at") if isinstance(now, dict) else None
        if not _is_number(at):                       # the SDK may hand back the server-clock marker, not the time
            fresh = self.cloud.get(path)
            at = fresh.get("updated_at") if isinstance(fresh, dict) else None
        with self._lock:
            if _is_number(at):
                self._seen[key] = int(at)
            else:
                self._seen[key] = 0
            self._seen_value[key] = value
            later = self._pending.get(key)
            if later is not None and later["seq"] != entry["seq"] and _is_number(at):
                later["base"] = int(at)              # edited again while this send ran: it builds on what was just written
        self._drop_if_same(key, entry["seq"])
        return "sent"

    def _lost(self, key: str, entry: dict, theirs: Any) -> str:
        """Another client's value is in Firebase and this device's edit was based on an older one."""
        if not isinstance(theirs, dict) or not _is_number(theirs.get("updated_at")):
            with self._lock:                         # damaged node: overwrite it on the next send
                cur = self._pending.get(key)
                if cur is not None:
                    cur["base"] = int(theirs.get("updated_at")) if isinstance(theirs, dict) and _is_number(theirs.get("updated_at")) else 0
            return "retry"
        ok, _v = self._check_value(key, theirs.get("value"))
        if not ok:                                   # their value is not usable: keep mine, send it over theirs next time
            with self._lock:
                cur = self._pending.get(key)
                if cur is not None:
                    cur["base"] = int(theirs["updated_at"])
            return "retry"
        out = {"applied": [], "restart": [], "conflicts": [], "warnings": []}
        self._apply_one(key, theirs, out)            # sees the pending edit with an older base: theirs wins, notice added
        return "conflict"

    def sync(self) -> dict:
        """The whole background routine: read Firebase, register this device, send what waits, seed what is missing, read
        again if anything was seeded (to learn the server times). Never raises. Returns the status."""
        if not self.enabled():
            return self.status()
        with self._sync_lock:
            if self.refresh()["last_error"]:
                return self.status()
        self.register()
        self.flush()
        with self._sync_lock:
            seeded = self.seed()
        if seeded["written"]:
            self.refresh()
        return self.status()

    # ---- what the UI will show ---------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            if not self._safe(self._switch_on):
                state = "off"
            elif not self._safe(self._configured):
                state = "no_firebase"
            elif self._pending:
                state = "offline" if self._error else "pending"
            elif self._error:
                state = "offline"
            else:
                state = "synced" if self._from_cloud else "starting"
            return {"state": state, "pending": len(self._pending), "pending_keys": sorted(self._pending),
                    "notices": len(self._notices), "last_sync_ms": self._last_sync_ms, "last_error": self._error,
                    "from_cloud": self._from_cloud, "warnings": list(self._warnings)}

    def notices(self) -> list:
        """Edits that lost to another client's value: [{key, mine, theirs, at_ms}], oldest first."""
        with self._lock:
            return [dict(n) for n in self._notices]

    def dismiss_notices(self) -> int:
        with self._lock:
            n = len(self._notices)
            self._notices = []
        self._persist()
        return n

    def warnings(self) -> list:
        with self._lock:
            return list(self._warnings)

    def overridden(self) -> list:
        """Shareable settings that a real environment variable overrides on this device (never uploaded)."""
        return [k for k in shareable_keys() if self.store.resolve(k).source == "env"]


# ----------------------------------------------------------------------------
# Shared instance (5D wires it)
# ----------------------------------------------------------------------------

_shared: Optional[SharedSettings] = None
_shared_lock = threading.Lock()


def get_shared() -> SharedSettings:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = SharedSettings()
        return _shared


def reset_shared_for_tests() -> None:
    global _shared
    with _shared_lock:
        _shared = None
