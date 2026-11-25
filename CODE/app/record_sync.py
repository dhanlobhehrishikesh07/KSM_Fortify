"""
record_sync.py
---------------
Phase 4E: keeps the owner's strategy audits and screen-scan answers in their own Firebase database.

* Controlled live by the "Keep audits and screen scans online" switch (`SYNC_AUDITS_SCANS`, off by default). Off means
  nothing is queued or sent: audits and scans stay in memory only, exactly as before.
* Each finished audit goes to `ksm/audits/<time_key>` and each finished scan to `ksm/scans/<time_key>`, as text only:
  the question, the answer, the time (and, for a scan, the monitor and model name). NEVER the screenshot or its preview,
  and never the task matrix that was sent to the audit (tasks have their own switch). Failed runs are not saved.
* Saved keys are removed from the text by the engine (`queue_record`) before anything is queued or written to the outbox.
* Only the audit and scan paths are flushed here, and the task and chat syncs flush only their own paths, so each
  switch controls its own data. A Firebase problem never reaches the audit or scan panel.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from app import cloud_sync, config
from app.logger import get_logger

log = get_logger(__name__)

MAX_TEXT = 40000        # one answer is cut to this many characters before it goes online
MAX_QUESTION = 2000
AUDIT_QUESTION = "Strategy audit of the task matrix"


class RecordSync:
    def __init__(
        self,
        engine: "cloud_sync.SyncEngine",
        switch: Optional[Callable[[], bool]] = None,
        background: bool = True,
    ):
        self.engine = engine
        self._switch = switch or (lambda: bool(config.SYNC_AUDITS_SCANS))
        self._background = background
        self._lock = threading.Lock()
        self._last_base = ""
        self._dupe = 0
        self._run_lock = threading.Lock()
        self._running = False
        self._again = False
        self._error = ""

    # -- public ----------------------------------------------------------------------------
    def start(self) -> None:
        """Sends anything left in the outbox from last time (if the switch is on)."""
        if self._switch():
            self._kick()

    def record_audit(self, answer: str, question: str = AUDIT_QUESTION) -> bool:
        """Queues one finished audit. Returns True if it was queued. Never raises."""
        try:
            if not self._switch() or not str(answer or "").strip():
                return False
            return self._queue(cloud_sync.AUDIT_ROOT,
                               {"question": str(question or "")[:MAX_QUESTION], "answer": str(answer)[:MAX_TEXT]})
        except Exception:  # noqa: BLE001
            log.exception("Could not record an audit for the online copy")
            return False

    def record_scan(self, question: str, answer: str, monitor=1, model: str = "") -> bool:
        """Queues one finished screen scan (text only, never the image). Never raises."""
        try:
            if not self._switch() or not str(answer or "").strip():
                return False
            try:
                mon = int(monitor)
            except (TypeError, ValueError):
                mon = 1
            return self._queue(cloud_sync.SCAN_ROOT,
                               {"question": str(question or "")[:MAX_QUESTION], "answer": str(answer)[:MAX_TEXT],
                                "monitor": mon, "model": str(model or "")[:100]})
        except Exception:  # noqa: BLE001
            log.exception("Could not record a screen scan for the online copy")
            return False

    def sync_now(self) -> None:
        """Sends what is waiting right away (if the switch is on). Returns immediately; never raises."""
        try:
            if self._switch():
                self._kick()
        except Exception:  # noqa: BLE001
            log.exception("Could not start a send")

    def status(self) -> dict:
        on = bool(self._switch())
        pending = self.engine.pending_count(cloud_sync.AUDIT_ROOT + "/") + self.engine.pending_count(cloud_sync.SCAN_ROOT + "/")
        state = "off" if not on else ("pending" if pending else "synced")
        if on and pending and self._error:
            state = "offline"
        return {"state": state, "switch": on, "pending": pending, "last_error": self._error}

    # -- internals ---------------------------------------------------------------------------
    def _key(self) -> str:
        base = cloud_sync.time_key()
        with self._lock:
            if base == self._last_base:               # two records in the same second must not overwrite each other
                self._dupe += 1
            else:
                self._last_base, self._dupe = base, 0
            return base if self._dupe == 0 else f"{base}-{self._dupe:02d}"

    def _queue(self, root: str, fields: dict) -> bool:
        record = dict(fields)
        record["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.engine.enabled = True
        ok = self.engine.queue_record(f"{root}/{self._key()}", record)
        if ok:
            self._kick()
        return ok

    def _kick(self) -> None:
        with self._run_lock:
            if self._running:
                self._again = True
                return
            self._running = True
        if self._background:
            threading.Thread(target=self._loop, name="ksm-record-sync", daemon=True).start()
        else:
            self._loop()

    def _loop(self) -> None:
        try:
            while True:
                try:
                    if self._switch():
                        for root in (cloud_sync.AUDIT_ROOT, cloud_sync.SCAN_ROOT):
                            self.engine.flush(root + "/")
                        st = self.engine.status()
                        pending = (self.engine.pending_count(cloud_sync.AUDIT_ROOT + "/")
                                   + self.engine.pending_count(cloud_sync.SCAN_ROOT + "/"))
                        self._error = st.get("last_error", "") if pending else ""
                except Exception:  # noqa: BLE001
                    log.exception("Audit/scan sync step failed")
                with self._run_lock:
                    if not self._again:
                        self._running = False
                        return
                    self._again = False
        finally:
            with self._run_lock:
                self._running = False


def start_record_sync(engine: "cloud_sync.SyncEngine") -> Optional[RecordSync]:
    """Used by the app at start-up. Never raises."""
    try:
        sync = RecordSync(engine)
        sync.start()
        return sync
    except Exception:  # noqa: BLE001
        log.exception("Could not start the online copy of audits and scans")
        return None
