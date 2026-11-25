"""
task_sync.py
-------------
Phase 4C: connects the task list (TaskStore) to the online-copy engine (cloud_sync).

What it does, in plain words:
* Every time a task is added, changed or deleted, the change is written into the engine's outbox (local, fast)
  and a background thread sends it. A Firebase problem never reaches the screen and never stops a local save.
* On start (and the first time the switch is turned on) it settles the local list with the cloud:
  tasks missing locally are restored, a newer cloud copy wins, and every local task the cloud has not seen is
  uploaded once. Running it again changes nothing.
* Before anything is sent or changed because of the cloud, the task file is copied to `data/backups/` (a
  one-time backup before the first upload, and another before a pull changes the local file). If that copy
  cannot be made, nothing is sent.
* All of it is controlled by the "Keep tasks online" switch (`SYNC_TASKS`, off by default). With the switch off
  this module does nothing at all and the app behaves exactly as before.

The engine and the cloud are passed in, so tests run against `MemoryCloud` with no network.
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from app import cloud_sync, config, settings_store
from app.data_store import TaskStore
from app.logger import get_logger

log = get_logger(__name__)

BACKUP_FOLDER = "backups"
RETRY_START_SECONDS = 60
RETRY_MAX_SECONDS = 600


class TaskSync:
    def __init__(
        self,
        store: TaskStore,
        engine: "cloud_sync.SyncEngine",
        switch: Optional[Callable[[], bool]] = None,
        backup_dir: Optional[Path] = None,
        marker_path: Optional[Path] = None,
        background: bool = True,
        retry_seconds: Optional[float] = RETRY_START_SECONDS,
    ):
        self.store = store
        self.engine = engine
        self._switch = switch or (lambda: bool(config.SYNC_TASKS))
        self._backup_dir = Path(backup_dir) if backup_dir else store.data_file.parent / BACKUP_FOLDER
        self._marker = Path(marker_path) if marker_path else settings_store.config_dir() / "task_sync.json"
        self._background = background
        self._retry_base = retry_seconds
        self._retry_delay = retry_seconds
        self._retry_timer: Optional[threading.Timer] = None

        self._run_lock = threading.Lock()
        self._running = False
        self._again = False
        self._initialized = False      # the settle-with-cloud step finished once in this run
        self._error = ""
        self.last_backup = ""          # file name of the newest backup this run made
        self._started = False

    # -- public ------------------------------------------------------------------------
    def start(self) -> None:
        """Subscribes to task changes and, if the switch is on, begins the first settle in the background."""
        if not self._started:
            self._started = True
            self.store.subscribe(self._on_change)
        if self._switch():
            self._kick()

    def sync_now(self) -> None:
        """Asks for a settle + send right away (for a future 'Sync now' button). Returns immediately."""
        if self._switch():
            self._kick()

    def status(self) -> dict:
        st = dict(self.engine.status())
        on = bool(self._switch())
        if not on:
            st["state"] = "off"
        elif not self._initialized and self._error:
            st["state"] = "offline"
        elif not self._initialized:
            st["state"] = "pending"
        st.update({"switch": on, "initialized": self._initialized,
                   "last_error": self._error or st.get("last_error", ""), "last_backup": self.last_backup})
        return st

    # -- reacting to task changes ---------------------------------------------------------
    def _on_change(self) -> None:
        """Called by TaskStore after every change, on the thread that made it. Must be quick and must not raise."""
        try:
            if not self._switch():
                return
            self.engine.enabled = True
            if self._initialized:
                self.engine.record_tasks(self.store.all_tasks())   # local file + outbox only, no network
            self._kick()
        except Exception:  # noqa: BLE001 - sync trouble never reaches the task list
            log.exception("Task sync could not react to a change")

    # -- running the work ---------------------------------------------------------------------
    def _kick(self) -> None:
        """Runs the work once more, on a background thread; calls made meanwhile are merged into one extra run."""
        with self._run_lock:
            if self._running:
                self._again = True
                return
            self._running = True
        if self._background:
            threading.Thread(target=self._loop, name="ksm-task-sync", daemon=True).start()
        else:
            self._loop()

    def _loop(self) -> None:
        try:
            while True:
                try:
                    self._work()
                except Exception:  # noqa: BLE001
                    log.exception("Task sync step failed")
                    self._error = "Task sync hit an unexpected problem (see the debug log)."
                with self._run_lock:
                    if not self._again:
                        self._running = False
                        break
                    self._again = False
        finally:
            with self._run_lock:
                self._running = False
        self._plan_retry()

    def _work(self) -> None:
        if not self._switch():
            return
        self.engine.enabled = True
        if self.store.save_blocked:
            self._error = "The task file is damaged and could not be backed up, so the online copy is paused."
            return
        if not self._initialized:
            self._settle()
            if not self._initialized:
                return
        else:
            self.engine.record_tasks(self.store.all_tasks())
        st = self.engine.flush(cloud_sync.TASK_ROOT + "/")      # tasks only: chat has its own switch
        pending = self.engine.pending_count(cloud_sync.TASK_ROOT + "/")
        self._error = st.get("last_error", "") if pending else ""

    # -- settle with the cloud (start / first time on) -------------------------------------------
    def _settle(self) -> None:
        result = self.engine.pull_tasks(self.store.all_tasks())
        if result.error:
            self._error = result.error                       # nothing was backed up, sent or changed
            return
        changes = bool(result.added or result.updated or result.removed)
        if changes or not self._marker_exists():
            problem = self._backup_task_file("restore" if changes else "first-upload")
            if problem:
                self._error = problem                          # no backup, no upload, no change
                return
        if changes:
            local = {t["id"]: t for t in self.store.all_tasks()}
            self.store.apply_cloud_changes(
                added=[t for t in result.tasks if t["id"] in set(result.added)],
                updated=[t for t in result.tasks if t["id"] in set(result.updated)],
                removed_ids=[i for i in result.removed if i in local],
            )
            self.engine.record_tasks(self.store.all_tasks())   # matches the cloud now, so queues nothing new
        self._write_marker()
        self._initialized = True
        self._error = ""

    # -- backup ------------------------------------------------------------------------------------
    def _marker_exists(self) -> bool:
        return self._marker.exists()

    def _write_marker(self) -> None:
        try:
            self._marker.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._marker.parent), prefix=".tasksync-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"first_settle_done": datetime.now().isoformat(timespec="seconds"),
                               "last_backup": self.last_backup}, f)
                os.replace(tmp, self._marker)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            log.warning("Could not save the task sync marker (%s)", type(e).__name__)

    def _backup_task_file(self, why: str) -> str:
        """Copies the task file to data/backups/ and checks the copy is identical. Returns "" on success (also when
        there is no task file yet), or a plain-words problem."""
        src = self.store.data_file
        if not src.exists():
            return ""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            self._backup_dir.mkdir(parents=True, exist_ok=True)
            dest = self._backup_dir / f"{src.name}.pre-sync-{why}-{stamp}"
            n = 1
            while dest.exists():
                dest = self._backup_dir / f"{src.name}.pre-sync-{why}-{stamp}-{n}"
                n += 1
            shutil.copy2(src, dest)
            if not filecmp.cmp(src, dest, shallow=False):
                dest.unlink()
                raise OSError("the copy did not match the original")
        except OSError as e:
            msg = f"Could not back up the task file ({e}). Nothing was sent online."
            log.error(msg)
            return msg
        self.last_backup = dest.name
        log.info("Task file backed up before online sync: %s", dest.name)
        return ""

    # -- retry when the cloud is unreachable ------------------------------------------------------------
    def _plan_retry(self) -> None:
        if self._retry_base is None or not self._switch():
            return
        failing = bool(self._error)
        if not failing:
            self._retry_delay = self._retry_base
            return
        if self._retry_timer is not None and self._retry_timer.is_alive():
            return
        self._retry_timer = threading.Timer(self._retry_delay, self._kick)
        self._retry_timer.daemon = True
        self._retry_timer.start()
        self._retry_delay = min((self._retry_delay or RETRY_START_SECONDS) * 2, RETRY_MAX_SECONDS)


def start_task_sync(store: TaskStore, engine: Optional["cloud_sync.SyncEngine"] = None) -> Optional[TaskSync]:
    """Used by the app at start-up. Never raises: if anything goes wrong the app simply runs without the online copy.
    Pass the shared engine so tasks and chat use one outbox file."""
    try:
        sync = TaskSync(store, engine or cloud_sync.engine_from_config())
        sync.start()
        return sync
    except Exception:  # noqa: BLE001
        log.exception("Could not start the online copy of tasks")
        return None
