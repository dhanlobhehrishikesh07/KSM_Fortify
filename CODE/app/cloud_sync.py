"""
cloud_sync.py
--------------
The "online copy" engine (Phase 4). It keeps a second copy of the owner's data
in their own Firebase Realtime Database, without ever being able to hurt the
local copy.

Rules this module lives by:
* Offline first. The local file is the working copy. Every public method here
  catches its own failures: a Firebase problem never raises into the UI and
  never blocks or loses a local change.
* Changes made while offline wait in an outbox (saved on disk) and are sent when
  the cloud is reachable again, so closing the app does not lose them.
* Single writer for tasks: only this desktop app changes tasks in the cloud.
  Conflicts (for example after restoring on a new machine) are settled per task
  by last-write-wins on `updated_at_ms`.
* A deleted task stays in the cloud as a small marker (`deleted: true`) so the
  delete reaches every copy.
* Text that goes online passes through `redact_text` first, so a key pasted
  into a chat is never uploaded (or kept in the outbox file).
* Never uploaded here: API keys, the service account file, screenshots.

Nothing in this module touches the GUI or TaskStore directly. 4C wires tasks to
it; 4D/4E use `queue_record` for chat, audits and scans. The Firebase part is
behind the small `CloudStore` interface, so tests use `MemoryCloud`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from app import config, firebase_app, settings_store
from app.logger import get_logger

log = get_logger(__name__)

ROOT = "ksm"
TASK_ROOT = f"{ROOT}/tasks"
CHAT_ROOT = f"{ROOT}/chats"
AUDIT_ROOT = f"{ROOT}/audits"
SCAN_ROOT = f"{ROOT}/scans"
META_PATH = f"{ROOT}/meta"
SCHEMA_VERSION = 1
STATE_VERSION = 1
MAX_OUTBOX = 5000   # offline for a very long time must not grow the file forever

_SEGMENT = re.compile(r"^[A-Za-z0-9_\-:@+~]+$")


class CloudError(RuntimeError):
    """The cloud could not be reached or refused the request. Message is user-presentable."""


class TransactionAbort(Exception):
    """Raised by the function given to `transaction()` to say "do not write: the data is not what I expected"."""


# Firebase fills this in with the server clock (ms) when it is written. MemoryCloud and MemoryTreeCloud do the same.
SERVER_TIME = {".sv": "timestamp"}


def _resolve_server_values(value: Any, now_ms: int) -> Any:
    """Replaces every {".sv": "timestamp"} inside a value with `now_ms` (what Firebase does on write)."""
    if isinstance(value, dict):
        if value == SERVER_TIME:
            return now_ms
        return {k: _resolve_server_values(v, now_ms) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_server_values(v, now_ms) for v in value]
    return value


# ----------------------------------------------------------------------------
# The cloud behind a small interface
# ----------------------------------------------------------------------------


class MemoryCloud:
    """An in-memory stand-in for Firebase (tests and dry runs). Set `online = False` to simulate no network."""

    def __init__(self):
        self.data: dict[str, Any] = {}      # flat: full path -> value
        self.online = True
        self.calls: list[tuple] = []

    def _check(self):
        if not self.online:
            raise CloudError("Could not reach Firebase. Check your internet connection (or proxy).")

    def get(self, path: str) -> Any:
        self._check()
        self.calls.append(("get", path))
        if path in self.data:
            return json.loads(json.dumps(self.data[path]))
        prefix = path.rstrip("/") + "/"
        kids = {p[len(prefix):]: v for p, v in self.data.items()
                if p.startswith(prefix) and "/" not in p[len(prefix):]}
        return json.loads(json.dumps(kids)) if kids else None

    def keys(self, path: str) -> list:
        """Names of the direct children of `path` (cheap listing; no values)."""
        self._check()
        self.calls.append(("keys", path))
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/", 1)[0] for p in self.data if p.startswith(prefix)})

    def set(self, path: str, value: Any) -> None:
        self._check()
        self.calls.append(("set", path))
        self.data[path] = json.loads(json.dumps(value))

    def delete(self, path: str) -> None:
        self._check()
        self.calls.append(("delete", path))
        self.data.pop(path, None)

    def transaction(self, path: str, update_fn: Callable[[Any], Any]) -> tuple:
        """Reads the value at `path`, gives it to `update_fn` and writes what it returns, as one step.
        `update_fn` raises TransactionAbort to leave the data alone. Returns (committed, value now at the path)."""
        self._check()
        self.calls.append(("transaction", path))
        current = json.loads(json.dumps(self.data[path])) if path in self.data else None
        try:
            new = update_fn(current)
        except TransactionAbort:
            return False, current
        self.data[path] = _resolve_server_values(json.loads(json.dumps(new)), int(time.time() * 1000))
        return True, json.loads(json.dumps(self.data[path]))


class TreeMirror:
    """Rebuilds a Firebase subtree from the live events of `Reference.listen` ("put" replaces what is at the path, "patch"
    merges the children it carries; a null removes). `apply` returns a fresh copy of the whole tree after the event
    (None when nothing is there), so a listener always sees one consistent picture."""

    def __init__(self):
        self.tree: Any = None

    @staticmethod
    def _parts(path: Any) -> list:
        return [p for p in str(path or "/").split("/") if p]

    @staticmethod
    def _set(root: dict, parts: list, value: Any) -> None:
        node = root
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                if value is None:
                    return
                nxt = node[part] = {}
            node = nxt
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value

    @staticmethod
    def _prune(node: dict) -> None:
        for k in list(node):
            if isinstance(node[k], dict):
                TreeMirror._prune(node[k])
                if not node[k]:
                    del node[k]

    def apply(self, event_type: Any, path: Any, data: Any) -> Any:
        parts = self._parts(path)
        merge = str(event_type).lower() == "patch" and isinstance(data, dict)
        if not parts:
            if merge and isinstance(self.tree, dict):
                for k, v in data.items():
                    self._set(self.tree, [k], v)
            else:
                self.tree = json.loads(json.dumps(data)) if data is not None else None
        else:
            if not isinstance(self.tree, dict):
                self.tree = {}
            if merge:
                for k, v in data.items():
                    self._set(self.tree, parts + [k], v)
            else:
                self._set(self.tree, parts, data)
        if isinstance(self.tree, dict):
            self._prune(self.tree)
            if not self.tree:
                self.tree = None
        return json.loads(json.dumps(self.tree)) if self.tree is not None else None


class ListenHandle:
    """What `listen` returns: `close()` stops the live listener. Safe to call twice."""

    def __init__(self, close: Callable[[], None]):
        self._close = close
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._close()
        except Exception:  # noqa: BLE001
            log.exception("Could not close the live listener")


class MemoryTreeCloud:
    """A faithful in-memory Firebase tree (nested, like the real database): writing a parent replaces everything
    below it, and reading a parent returns everything below it. Use it where code reads and writes at different
    levels (the shared configuration). Same methods as MemoryCloud, plus `transaction`. `online = False` simulates no network."""

    def __init__(self, clock: Optional[Callable[[], int]] = None):
        self.tree: dict = {}
        self.online = True
        self.calls: list[tuple] = []
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._listeners: list = []       # [(path, on_tree)]

    def _check(self):
        if not self.online:
            raise CloudError("Could not reach Firebase. Check your internet connection (or proxy).")

    @staticmethod
    def _parts(path: str) -> list:
        return [p for p in str(path).split("/") if p]

    @staticmethod
    def _clean(value: Any) -> Any:
        """Firebase keeps no nulls and no empty objects."""
        if isinstance(value, dict):
            out = {k: MemoryTreeCloud._clean(v) for k, v in value.items() if v is not None}
            out = {k: v for k, v in out.items() if v != {}}
            return out
        return value

    def _node(self, path: str):
        node: Any = self.tree
        for part in self._parts(path):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def get(self, path: str) -> Any:
        self._check()
        self.calls.append(("get", path))
        node = self._node(path)
        return json.loads(json.dumps(node)) if node is not None else None

    def keys(self, path: str) -> list:
        self._check()
        self.calls.append(("keys", path))
        node = self._node(path)
        return sorted(node) if isinstance(node, dict) else []

    def _put(self, path: str, value: Any) -> None:
        parts = self._parts(path)
        value = self._clean(_resolve_server_values(json.loads(json.dumps(value)), self._clock())) if value is not None else None
        node = self.tree
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                if value is None:
                    return
                nxt = node[part] = {}
            node = nxt
        if value is None or value == {}:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
        self._prune_empty(self.tree)

    def _prune_empty(self, node: dict) -> None:
        for k in list(node):
            if isinstance(node[k], dict):
                self._prune_empty(node[k])
                if not node[k]:
                    del node[k]

    def set(self, path: str, value: Any) -> None:
        self._check()
        self.calls.append(("set", path))
        self._put(path, value)
        self._notify(path)

    def delete(self, path: str) -> None:
        self._check()
        self.calls.append(("delete", path))
        self._put(path, None)
        self._notify(path)

    def transaction(self, path: str, update_fn: Callable[[Any], Any]) -> tuple:
        self._check()
        self.calls.append(("transaction", path))
        node = self._node(path)
        current = json.loads(json.dumps(node)) if node is not None else None
        try:
            new = update_fn(current)
        except TransactionAbort:
            return False, current
        self._put(path, new)
        node = self._node(path)
        result = json.loads(json.dumps(node)) if node is not None else None
        self._notify(path)
        return True, result

    # ---- live listener (what Reference.listen does on the real database) ----------------------------------------
    def listen(self, path: str, on_tree: Callable[[Any], None]) -> "ListenHandle":
        """Calls `on_tree(subtree)` now (the first snapshot) and after every later write at, above or below `path`.
        The calls run on the thread that wrote (the real database uses its own thread). Raises CloudError offline."""
        self._check()
        self.calls.append(("listen", path))
        entry = (path, on_tree)
        self._listeners.append(entry)

        def close():
            if entry in self._listeners:
                self._listeners.remove(entry)
        handle = ListenHandle(close)
        self._deliver(entry)
        return handle

    def _deliver(self, entry: tuple) -> None:
        path, fn = entry
        node = self._node(path)
        try:
            fn(json.loads(json.dumps(node)) if node is not None else None)
        except Exception:  # noqa: BLE001  (a listener must never break the writer)
            log.exception("A live listener failed")

    def _notify(self, changed: str) -> None:
        cp = self._parts(changed)
        for entry in list(self._listeners):
            lp = self._parts(entry[0])
            n = min(len(cp), len(lp))
            if cp[:n] == lp[:n]:
                self._deliver(entry)


class FirebaseCloudStore:
    """The real thing: Firebase Realtime Database through the shared app. All failures become CloudError."""

    def __init__(self, ref_factory: Optional[Callable[[str], Any]] = None):
        self._ref_factory = ref_factory

    def _ref(self, path: str):
        if self._ref_factory is not None:
            return self._ref_factory(path)
        from firebase_admin import db  # local import: the package is optional
        return db.reference(path, app=firebase_app.get_app())

    @staticmethod
    def _wrap(e: Exception) -> CloudError:
        if isinstance(e, CloudError):
            return e
        if isinstance(e, firebase_app.FirebaseAppError):
            return CloudError(str(e))
        from app.settings_tests import explain_error
        return CloudError(explain_error("firebase", e))

    def get(self, path: str) -> Any:
        try:
            return self._ref(path).get()
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e

    def keys(self, path: str) -> list:
        try:
            found = self._ref(path).get(shallow=True)     # keys only: does not download the values
            return sorted(found) if isinstance(found, dict) else []
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e

    def set(self, path: str, value: Any) -> None:
        try:
            self._ref(path).set(value)
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e

    def delete(self, path: str) -> None:
        try:
            self._ref(path).delete()
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e

    def listen(self, path: str, on_tree: Callable[[Any], None]) -> "ListenHandle":
        """Opens ONE live connection on `path`. `on_tree(subtree)` is called on the SDK's own thread with the whole subtree
        after every change (the first call is the current state). Returns a handle; `close()` ends the connection."""
        mirror = TreeMirror()
        lock = threading.Lock()

        def handler(event):
            with lock:
                tree = mirror.apply(getattr(event, "event_type", "put"), getattr(event, "path", "/"), getattr(event, "data", None))
            try:
                on_tree(tree)
            except Exception:  # noqa: BLE001  (a listener must never kill the connection)
                log.exception("A live listener failed")
        try:
            registration = self._ref(path).listen(handler)
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e
        return ListenHandle(registration.close)

    def transaction(self, path: str, update_fn: Callable[[Any], Any]) -> tuple:
        """Firebase's own transaction: `update_fn(current)` returns the new value, or raises TransactionAbort to leave
        the data alone. Returns (committed, value). Firebase may call `update_fn` more than once, so it must have no side effects."""
        try:
            def wrapper(current):
                return update_fn(current)          # TransactionAbort passes through the SDK untouched
            try:
                new = self._ref(path).transaction(wrapper)
            except TransactionAbort:
                return False, None
            return True, new
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def valid_path(path: str) -> bool:
    """Only paths under ksm/ with plain key characters (Firebase forbids . # $ [ ] in keys)."""
    parts = str(path).split("/")
    return len(parts) >= 2 and parts[0] == ROOT and all(_SEGMENT.match(p) for p in parts)


def safe_key(text: str) -> str:
    """Turns '2026-10-27 06:13:00' (or any text) into a legal Firebase key."""
    key = re.sub(r"[^A-Za-z0-9_\-:@+~]", "_", str(text).strip())
    return key or "_"


def time_key(when: Optional[datetime] = None) -> str:
    return safe_key((when or datetime.now()).strftime("%Y-%m-%d_%H:%M:%S"))


def redact_values(value: Any) -> Any:
    """Removes every saved key from all text inside a value that is about to leave the machine."""
    if isinstance(value, str):
        return settings_store.redact_text(value)
    if isinstance(value, dict):
        return {k: redact_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_values(v) for v in value]
    return value


def _history_list(raw: Any) -> list[str]:
    if isinstance(raw, dict):   # Firebase returns a dict if an array ever has gaps
        def order(kv):
            return int(kv[0]) if str(kv[0]).isdigit() else 0
        raw = [v for _, v in sorted(raw.items(), key=order)]
    return [str(x) for x in (raw or [])]


def normalize_task(task: dict) -> dict:
    """The fields that are synced, in one fixed shape. Raises KeyError/ValueError on a malformed task."""
    return {
        "id": int(task["id"]),
        "title": str(task["title"]),
        "imp": int(task["imp"]),
        "urg": int(task["urg"]),
        "created_at": str(task.get("created_at", "")),
        "expected_at": str(task.get("expected_at", "")),
        "history": _history_list(task.get("history")),
    }


def task_hash(task: dict) -> str:
    # Hash the text as it will exist online (saved keys removed), so a task whose text held a key does not
    # look "changed" against its own redacted cloud copy on every start.
    text = json.dumps(redact_values(normalize_task(task)), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def task_to_cloud(task: dict, updated_at_ms: int) -> dict:
    rec = redact_values(normalize_task(task))
    rec["updated_at_ms"] = int(updated_at_ms)
    rec["deleted"] = False
    return rec


def cloud_to_task(rec: dict) -> dict:
    return normalize_task(rec)


@dataclass
class PullResult:
    tasks: list = field(default_factory=list)     # the merged task list (the caller decides whether to apply it)
    added: list = field(default_factory=list)     # ids restored from the cloud
    updated: list = field(default_factory=list)   # ids where the cloud copy was newer
    removed: list = field(default_factory=list)   # ids deleted because the cloud says so
    error: str = ""


# ----------------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------------


class SyncEngine:
    def __init__(self, cloud, state_path: Optional[Path] = None, enabled: bool = True,
                 clock: Optional[Callable[[], int]] = None):
        self.cloud = cloud
        self.enabled = enabled
        self._path = Path(state_path) if state_path else settings_store.config_dir() / "sync_state.json"
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._tasks: dict[str, dict] = {}     # id -> {"hash", "ms", "deleted"}
        self._outbox: dict[str, dict] = {}    # path -> {"value": dict | None (delete), "ms"}
        self._last_sync_ms = 0
        self._dropped = 0
        self._error = ""
        self._load()

    # -- state file ------------------------------------------------------------
    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._tasks = {str(k): v for k, v in (data.get("tasks") or {}).items()}
            self._outbox = dict(data.get("outbox") or {})
            self._last_sync_ms = int(data.get("last_sync_ms") or 0)
            self._dropped = int(data.get("dropped") or 0)
        except Exception as e:  # noqa: BLE001 - a damaged state file must never stop the app
            log.warning("Sync state file unreadable (%s); starting fresh and keeping a copy", type(e).__name__)
            try:
                self._path.replace(self._path.with_name(
                    f"{self._path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"))
            except OSError:
                pass
            self._tasks, self._outbox = {}, {}

    def _persist(self) -> None:
        with self._lock:
            payload = json.dumps({
                "version": STATE_VERSION, "tasks": self._tasks, "outbox": self._outbox,
                "last_sync_ms": self._last_sync_ms, "dropped": self._dropped,
            }, ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".sync-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            log.warning("Could not save the sync state (%s)", type(e).__name__)

    # -- status ------------------------------------------------------------------
    def pending_count(self, prefix: Optional[str] = None) -> int:
        """How many changes wait to be sent (only those whose path starts with `prefix`, if given)."""
        with self._lock:
            if prefix is None:
                return len(self._outbox)
            return sum(1 for p in self._outbox if p.startswith(prefix))

    def status(self) -> dict:
        with self._lock:
            pending = len(self._outbox)
            if not self.enabled:
                state = "off"
            elif pending and self._error:
                state = "offline"
            elif pending:
                state = "pending"
            else:
                state = "synced"
            return {"state": state, "pending": pending, "last_sync_ms": self._last_sync_ms,
                    "last_error": self._error, "dropped": self._dropped}

    def _fail(self, e: Exception) -> str:
        msg = settings_store.redact_text(str(e) or type(e).__name__)
        with self._lock:
            self._error = msg
        log.info("Online copy problem: %s", msg)
        return msg

    # -- queueing ------------------------------------------------------------------
    def _enqueue(self, path: str, value: Optional[dict], ms: int) -> None:
        with self._lock:
            self._outbox.pop(path, None)       # re-insert so the newest change is last in order
            self._outbox[path] = {"value": value, "ms": int(ms)}
            self._cap_outbox()

    def _cap_outbox(self) -> None:
        over = len(self._outbox) - MAX_OUTBOX
        if over <= 0:
            return
        for path in [p for p in self._outbox if not p.startswith(TASK_ROOT + "/")][:over]:
            del self._outbox[path]             # drop the oldest chat/audit/scan records, never tasks
            self._dropped += 1
            over -= 1
        if over > 0:
            log.warning("Sync outbox is over its limit by %d (only task changes left)", over)

    def queue_record(self, path: str, value: Any) -> bool:
        """Queues any record (chat message, audit, scan answer) for upload. Text is redacted first."""
        if not self.enabled:
            return False
        if not valid_path(path):
            log.warning("Refusing to queue an invalid cloud path")
            return False
        try:
            self._enqueue(path, redact_values(value), self._clock())
            self._persist()
            return True
        except Exception:  # noqa: BLE001
            log.exception("Could not queue a record for the online copy")
            return False

    # -- tasks: local changes -> outbox ----------------------------------------------
    def record_tasks(self, tasks: list) -> int:
        """Compares the current local tasks with what is known to be synced and queues the differences
        (new or changed tasks, and delete markers for removed ones). Returns how many were queued.
        Cheap to call after every local change."""
        if not self.enabled:
            return 0
        queued = 0
        try:
            now = self._clock()
            seen = set()
            with self._lock:
                for t in tasks:
                    try:
                        h = task_hash(t)
                        key = str(int(t["id"]))
                    except (KeyError, TypeError, ValueError):
                        log.warning("Skipping a malformed task in the online copy")
                        continue
                    seen.add(key)
                    st = self._tasks.get(key)
                    if st and not st.get("deleted") and st.get("hash") == h:
                        continue
                    self._tasks[key] = {"hash": h, "ms": now, "deleted": False}
                    self._enqueue(f"{TASK_ROOT}/{key}", task_to_cloud(t, now), now)
                    queued += 1
                for key, st in list(self._tasks.items()):
                    if key in seen or st.get("deleted"):
                        continue
                    self._tasks[key] = {"hash": None, "ms": now, "deleted": True}
                    self._enqueue(f"{TASK_ROOT}/{key}",
                                  {"id": int(key), "deleted": True, "updated_at_ms": now}, now)
                    queued += 1
            if queued:
                self._persist()
        except Exception:  # noqa: BLE001
            log.exception("Could not record task changes for the online copy")
        return queued

    # -- outbox -> cloud ----------------------------------------------------------------
    def flush(self, prefix: Optional[str] = None) -> dict:
        """Sends what is waiting (only paths starting with `prefix`, if given, so one kind of data can be sent without
        sending another whose switch is off). Never raises; returns the status."""
        if not self.enabled:
            return self.status()
        if not self._flush_lock.acquire(blocking=False):
            return self.status()          # another thread is already sending
        try:
            with self._lock:
                paths = [p for p in self._outbox if prefix is None or p.startswith(prefix)]
            sent = 0
            for path in paths:
                with self._lock:
                    op = self._outbox.get(path)
                if op is None:
                    continue
                try:
                    if op["value"] is None:
                        self.cloud.delete(path)
                    else:
                        self.cloud.set(path, op["value"])
                except Exception as e:  # noqa: BLE001
                    self._fail(e)
                    break
                with self._lock:
                    if self._outbox.get(path) is op:   # not replaced by a newer change meanwhile
                        del self._outbox[path]
                    self._error = ""
                    self._last_sync_ms = self._clock()
                sent += 1
            if sent:
                try:
                    self.cloud.set(META_PATH, {"schema": SCHEMA_VERSION, "last_upload_ms": self._clock()})
                except Exception:  # noqa: BLE001 - the marker is best effort
                    pass
            if sent or paths:
                self._persist()
        except Exception as e:  # noqa: BLE001
            self._fail(e)
        finally:
            self._flush_lock.release()
        return self.status()

    # -- cloud -> local (tasks) -------------------------------------------------------------
    def pull_tasks(self, local_tasks: list) -> PullResult:
        """Reads the cloud and settles each task against the local list (last write wins). Does NOT write the
        local file: the caller applies `result.tasks` if it wants to. A local task the cloud does not have is
        queued for upload (first upload, or the cloud lost its data). Never raises."""
        result = PullResult(tasks=list(local_tasks))
        if not self.enabled:
            return result
        try:
            self.record_tasks(local_tasks)            # register local changes (with times) before comparing
            remote = self.cloud.get(TASK_ROOT) or {}
        except Exception as e:  # noqa: BLE001
            result.error = self._fail(e)
            return result
        try:
            by_id = {}
            for t in local_tasks:
                try:
                    by_id[str(int(t["id"]))] = t
                except (KeyError, TypeError, ValueError):
                    continue
            with self._lock:
                for key, rec in remote.items():
                    if not isinstance(rec, dict):
                        continue
                    path = f"{TASK_ROOT}/{key}"
                    ms = int(rec.get("updated_at_ms") or 0)
                    st = self._tasks.get(key)
                    if rec.get("deleted"):
                        if key in by_id and st and ms > st["ms"]:
                            del by_id[key]
                            self._outbox.pop(path, None)
                            result.removed.append(int(key))
                        if not st or ms > st["ms"]:
                            self._tasks[key] = {"hash": None, "ms": ms, "deleted": True}
                        continue
                    try:
                        task = cloud_to_task(rec)
                        h = task_hash(task)
                    except (KeyError, TypeError, ValueError):
                        log.warning("Ignoring a malformed task in the cloud")
                        continue
                    if key not in by_id:
                        if st and st.get("deleted") and st["ms"] >= ms:
                            continue                   # our own newer delete is (or will be) sent
                        by_id[key] = task
                        self._tasks[key] = {"hash": h, "ms": ms, "deleted": False}
                        self._outbox.pop(path, None)
                        result.added.append(task["id"])
                    elif st and st.get("hash") == h:
                        st["ms"] = ms                  # same content: nothing to do, adopt the cloud time
                        self._outbox.pop(path, None)
                    elif st and ms > st["ms"]:
                        by_id[key] = task              # the cloud copy is newer: it wins
                        self._tasks[key] = {"hash": h, "ms": ms, "deleted": False}
                        self._outbox.pop(path, None)
                        result.updated.append(task["id"])
                    elif path not in self._outbox:     # local is newer but nothing is waiting: queue it
                        self._enqueue(path, task_to_cloud(by_id[key], st["ms"] if st else self._clock()),
                                      st["ms"] if st else self._clock())
                for key, t in by_id.items():           # local tasks the cloud has never seen (or lost)
                    path = f"{TASK_ROOT}/{key}"
                    if key not in remote and path not in self._outbox:
                        ms = (self._tasks.get(key) or {}).get("ms") or self._clock()
                        self._enqueue(path, task_to_cloud(t, ms), ms)
            result.tasks = sorted(by_id.values(), key=lambda t: int(t["id"]))
            self._persist()
        except Exception as e:  # noqa: BLE001
            log.exception("Could not settle tasks with the online copy")
            result.error = self._fail(e)
            result.tasks = list(local_tasks)
        return result

    # -- cloud -> local (chat) -----------------------------------------------------------------------
    def fetch_recent_chat(self, limit: int, exclude: str = "") -> tuple:
        """The last `limit` chat messages from the cloud, oldest first, as ([{role, text, time}], error). Reads the newest
        sessions only and stops at a session that starts with a "context cleared" marker, so a chat the owner cleared is
        not brought back. The session named in `exclude` (the one running now) is skipped. Saved keys are removed from the text again on the way in. Never raises."""
        if not self.enabled or limit <= 0:
            return [], ""
        try:
            sessions = sorted(k for k in (self.cloud.keys(CHAT_ROOT) or []) if _SEGMENT.match(str(k)))
            collected: list = []
            for sid in reversed(sessions):
                if sid == exclude:
                    continue
                raw = self.cloud.get(f"{CHAT_ROOT}/{sid}") or {}
                if not isinstance(raw, dict):
                    continue
                part, cleared = [], False
                for _, rec in sorted(raw.items(), key=lambda kv: str(kv[0])):
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("role") == "marker":
                        cleared = True
                        part = []                       # anything stored before the marker belongs to the cleared chat
                        continue
                    if rec.get("role") in ("user", "assistant") and isinstance(rec.get("text"), str):
                        part.append({"role": rec["role"], "text": settings_store.redact_text(rec["text"]),
                                     "time": str(rec.get("time", ""))})
                collected = part + collected
                if cleared or len(collected) >= limit:
                    break
            return collected[-limit:], ""
        except Exception as e:  # noqa: BLE001
            return [], self._fail(e)

    # -- cloud -> file (export) ------------------------------------------------------------------------
    def export_all(self) -> tuple:
        """Reads everything the online copy keeps (tasks, chats, audits, scans) as ({...}, error). Download only: it works
        whatever the switches say and sends nothing. Saved keys are removed from all text. Nothing partial is returned:
        if any read fails the result is ({}, message). Never raises."""
        try:
            data: dict = {}
            for name, root in (("tasks", TASK_ROOT), ("audits", AUDIT_ROOT), ("scans", SCAN_ROOT)):
                raw = self.cloud.get(root)
                data[name] = raw if isinstance(raw, dict) else {}
            chats: dict = {}
            for sid in sorted(k for k in (self.cloud.keys(CHAT_ROOT) or []) if _SEGMENT.match(str(k))):
                raw = self.cloud.get(f"{CHAT_ROOT}/{sid}")
                if isinstance(raw, dict):
                    chats[str(sid)] = raw
            data["chats"] = chats
            return redact_values(data), ""
        except Exception as e:  # noqa: BLE001
            return {}, self._fail(e)


def engine_from_config(cloud=None) -> SyncEngine:
    """Builds the engine from settings: on only if at least one online-copy switch is on."""
    on = bool(config.SYNC_TASKS or config.SYNC_CHAT or config.SYNC_AUDITS_SCANS)
    return SyncEngine(cloud or FirebaseCloudStore(), enabled=on)
