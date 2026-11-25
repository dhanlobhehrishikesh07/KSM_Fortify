"""
data_store.py
-------------
The "Central_Government" data layer: owns matrix_data_pro.json, task
CRUD, migration, and the auto-urgency-escalation engine.

Kept independent of any GUI or socket code so it can be unit tested and
reused (e.g. by the strategy officer or a future CLI) on its own.

Any UI that wants live updates can subscribe() a no-arg callback; it will
be called after every mutation (add / delete / migrate / auto-escalate).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from app import config
from app.logger import get_logger

log = get_logger(__name__)

TIME_FMT = "%Y-%m-%d %H:%M:%S"


class TaskStoreError(RuntimeError):
    """Raised when the store refuses to write (e.g. it could not back up a damaged data file)."""


class TaskStore:
    def __init__(self, data_file: Path = config.DATA_FILE):
        self.data_file = data_file
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[], None]] = []
        self.master_offset = timedelta(0)
        self.load_warning = ""          # non-empty if the data file was damaged on load
        self._save_blocked = False      # True if a damaged file could not be backed up
        self.tasks: list[dict] = self._load()

    # -- subscription -----------------------------------------------------
    def subscribe(self, callback: Callable[[], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self) -> None:
        for cb in list(self._subscribers):
            try:
                cb()
            except Exception:  # noqa: BLE001 - one bad subscriber must not break the others
                log.exception("TaskStore subscriber failed")

    # -- persistence --------------------------------------------------------
    def _load(self) -> list[dict]:
        """Load tasks. A missing file is normal (first run). A damaged file is
        copied to '<name>.corrupt-<timestamp>' first, so starting empty can never
        destroy the only copy of the owner's data."""
        if not self.data_file.exists():
            return []
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list) or not all(isinstance(t, dict) for t in data):
                raise ValueError("top-level JSON must be a list of task objects")
            return data
        except (OSError, ValueError) as e:  # ValueError covers JSONDecodeError and bad UTF-8
            self._handle_damaged_file(e)
            return []

    def _handle_damaged_file(self, error: Exception) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.data_file.with_name(f"{self.data_file.name}.corrupt-{stamp}")
        n = 1
        while backup.exists():
            backup = self.data_file.with_name(f"{self.data_file.name}.corrupt-{stamp}-{n}")
            n += 1
        try:
            shutil.copy2(self.data_file, backup)
            self.load_warning = (
                f"Task file could not be read ({error}). A copy was saved as '{backup.name}'; "
                "starting with an empty matrix."
            )
            log.error(self.load_warning)
        except OSError as copy_err:
            self._save_blocked = True
            self.load_warning = (
                f"Task file could not be read ({error}) and could not be backed up ({copy_err}). "
                "It will not be overwritten: fix or move the file, then restart."
            )
            log.error(self.load_warning)

    def _save(self) -> None:
        """Atomic save: write a temp file, flush to disk, then replace. An
        interrupted write can never leave a half-written task file behind."""
        if self._save_blocked:
            raise TaskStoreError(self.load_warning)
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.data_file.with_name(self.data_file.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.data_file)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    # -- time helpers ---------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now() + self.master_offset

    def now_str(self) -> str:
        return self.now().strftime(TIME_FMT)

    def set_time_warp(self, days: float = 0, hours: float = 0) -> None:
        with self._lock:
            self.master_offset = timedelta(days=days, hours=hours)
        self._notify()

    # -- CRUD ---------------------------------------------------------------
    def all_tasks(self) -> list[dict]:
        with self._lock:
            return list(self.tasks)

    def next_id(self) -> int:
        return max((t["id"] for t in self.tasks), default=100) + 1

    def add_task(
        self,
        title: str,
        importance: int,
        urgency: int,
        expected_at: str,
        origin: str = "MANUAL",
    ) -> dict:
        """expected_at must be 'YYYY-MM-DD HH:MM:SS'."""
        datetime.strptime(expected_at, TIME_FMT)  # raises if malformed
        with self._lock:
            task = {
                "id": self.next_id(),
                "title": title,
                "imp": int(importance),
                "urg": int(urgency),
                "created_at": self.now_str(),
                "expected_at": expected_at,
                "history": [f"[{self.now_str()}] [{origin}] Created."],
            }
            self.tasks.append(task)
            try:
                self._save()
            except BaseException:
                self.tasks.remove(task)  # do not keep a task that was never persisted
                raise
        self._notify()
        return task

    def delete_task(self, task_id: int) -> None:
        with self._lock:
            self.tasks = [t for t in self.tasks if t["id"] != task_id]
            self._save()
        self._notify()

    def migrate_task(self, task_id: int, new_imp: int, new_urg: int) -> Optional[dict]:
        """Manual drag-and-drop style migration; only allows moving to an
        equal-or-higher priority cell, one step at a time (mirrors the
        original UX rule so the grid stays intentional rather than random)."""
        with self._lock:
            task = next((t for t in self.tasks if t["id"] == task_id), None)
            if not task:
                return None
            if new_imp < task["imp"] or new_urg < task["urg"]:
                return None
            if abs(new_imp - task["imp"]) > 1:
                return None
            task["history"].append(
                f"[{self.now_str()}] [MANUAL] Migrated: "
                f"{task['imp']},{task['urg']} -> {new_imp},{new_urg}"
            )
            task["imp"], task["urg"] = new_imp, new_urg
            self._save()
        self._notify()
        return task

    @property
    def save_blocked(self) -> bool:
        """True when the task file was damaged and could not be backed up: the store refuses to write."""
        return self._save_blocked

    def apply_cloud_changes(self, added: list, updated: list, removed_ids: list) -> int:
        """Applies tasks settled by the online copy (Phase 4C): restores `added`, replaces `updated` (cloud fields win,
        local-only fields are kept) and drops `removed_ids`. Applied as a delta on the CURRENT list, so a task the
        owner created while the network call was running is never lost. An added task whose id is already used
        locally is skipped (the local one stays). Returns how many tasks changed; saves and notifies only if any."""
        changed = 0
        with self._lock:
            before = list(self.tasks)
            try:
                by_id = {t["id"]: i for i, t in enumerate(self.tasks)}
                for t in added:
                    if t["id"] in by_id:
                        log.warning("Online copy has task %s but a local task already uses that id; keeping local", t["id"])
                        continue
                    self.tasks.append(dict(t))
                    by_id[t["id"]] = len(self.tasks) - 1
                    changed += 1
                for t in updated:
                    i = by_id.get(t["id"])
                    if i is not None:
                        self.tasks[i] = {**self.tasks[i], **t}
                        changed += 1
                gone = set(removed_ids)
                if gone:
                    kept = [t for t in self.tasks if t["id"] not in gone]
                    changed += len(self.tasks) - len(kept)
                    self.tasks = kept
                if changed:
                    self.tasks.sort(key=lambda t: t["id"])
                    self._save()
            except BaseException:
                self.tasks = before     # a failed save must not leave memory and file different
                raise
        if changed:
            self._notify()
        return changed

    def as_json(self) -> str:
        with self._lock:
            return json.dumps(self.tasks, indent=2)

    # -- background engine ----------------------------------------------------
    def run_auto_escalation(self) -> bool:
        """Bumps urgency as deadlines approach. Returns True if anything changed.
        Safe to call from a timer loop on any thread; it only touches data,
        never the GUI."""
        now_dt = self.now()
        changed = False
        with self._lock:
            for task in self.tasks:
                if "expected_at" not in task:
                    continue
                try:
                    exp_dt = datetime.strptime(task["expected_at"], TIME_FMT)
                except ValueError:
                    continue
                diff_sec = (exp_dt - now_dt).total_seconds()
                old_urg = task["urg"]

                if diff_sec <= config.URGENCY_ESCALATE_24H and task["urg"] < 3:
                    task["urg"] = 3
                    task["history"].append(
                        f"[{self.now_str()}] [AUTO] <24h Remaining: Urgency {old_urg} -> 3"
                    )
                    changed = True
                elif diff_sec <= config.URGENCY_ESCALATE_1WEEK and task["urg"] < 2:
                    task["urg"] = 2
                    task["history"].append(
                        f"[{self.now_str()}] [AUTO] <1 Week Remaining: Urgency {old_urg} -> 2"
                    )
                    changed = True

            if changed:
                self._save()

        if changed:
            self._notify()
        return changed
