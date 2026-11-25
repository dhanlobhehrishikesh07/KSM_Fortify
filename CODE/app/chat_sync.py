"""
chat_sync.py
-------------
Phase 4D: keeps the owner's chat history in their own Firebase database, so the AI remembers earlier sessions.

* Controlled live by the "Keep chat history online" switch (`SYNC_CHAT`, off by default). Off means nothing is
  queued, sent or loaded: chat stays in memory only, exactly as before.
* Each user message and AI reply is queued (saved keys removed from the text first, by the engine) and sent by a
  background thread to `ksm/chats/<session>/<message>`. A Firebase problem never reaches the chat panel.
* A session is one run of the app, or the stretch after "Clear context". Clearing writes a small marker so a cleared
  chat is not loaded back on the next start. The running session is never loaded back (it is already in memory).
* `load_recent()` reads the last few messages (across sessions, newest session first, stopping at a marker) and
  returns them as the "User: ..." / "AI: ..." lines the assistant already uses.
* Only chat paths are flushed here, and only chat paths are flushed by the task sync, so each switch controls its own data.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from app import cloud_sync, config
from app.logger import get_logger

log = get_logger(__name__)

LOAD_LAST = 20          # messages brought back when the app starts
MAX_TEXT = 20000        # one message is cut to this many characters before it goes online
_ROLES = ("user", "assistant")


class ChatSync:
    def __init__(
        self,
        engine: "cloud_sync.SyncEngine",
        switch: Optional[Callable[[], bool]] = None,
        background: bool = True,
        load_last: int = LOAD_LAST,
    ):
        self.engine = engine
        self._switch = switch or (lambda: bool(config.SYNC_CHAT))
        self._background = background
        self.load_last = load_last
        self._lock = threading.Lock()
        self._last_base = ""
        self._dupe = 0
        self._session = self._new_session_id()      # fixed up front so a history load can skip the running session
        self._n = 0
        self._run_lock = threading.Lock()
        self._running = False
        self._again = False
        self._error = ""

    # -- public ----------------------------------------------------------------------------
    def start(self) -> None:
        """Sends anything left in the outbox from last time (if the switch is on)."""
        if self._switch():
            self._kick()

    def record(self, role: str, text: str) -> bool:
        """Queues one chat message for upload. Returns True if it was queued. Never raises."""
        try:
            if role not in _ROLES or not self._switch() or not str(text or "").strip():
                return False
            return self._queue(role, str(text)[:MAX_TEXT])
        except Exception:  # noqa: BLE001
            log.exception("Could not record a chat message for the online copy")
            return False

    def record_clear(self) -> bool:
        """Starts a new session that begins with a 'context cleared' marker. Never raises."""
        try:
            with self._lock:
                self._session, self._n = self._new_session_id(), 0
            if not self._switch():
                return False
            return self._queue("marker", "context cleared")
        except Exception:  # noqa: BLE001
            log.exception("Could not record a cleared chat for the online copy")
            return False

    def load_recent(self) -> tuple:
        """([ 'User: ...', 'AI: ...' ], error). Empty when the switch is off. Never raises."""
        try:
            if not self._switch():
                return [], ""
            self.engine.enabled = True
            msgs, err = self.engine.fetch_recent_chat(self.load_last, exclude=self._session)
            lines = [("User: " if m["role"] == "user" else "AI: ") + m["text"] for m in msgs]
            if err:
                self._error = err
            return lines, err
        except Exception:  # noqa: BLE001
            log.exception("Could not load the chat history from the online copy")
            return [], "Could not load the chat history."

    def sync_now(self) -> None:
        """Sends what is waiting right away (if the switch is on). Returns immediately; never raises."""
        try:
            if self._switch():
                self._kick()
        except Exception:  # noqa: BLE001
            log.exception("Could not start a send")

    def status(self) -> dict:
        on = bool(self._switch())
        pending = self.engine.pending_count(cloud_sync.CHAT_ROOT + "/")
        state = "off" if not on else ("pending" if pending else "synced")
        if on and pending and self._error:
            state = "offline"
        return {"state": state, "switch": on, "pending": pending, "last_error": self._error}

    # -- internals ---------------------------------------------------------------------------
    def _new_session_id(self) -> str:
        base = cloud_sync.time_key()
        if base == self._last_base:                       # two sessions in the same second must stay in order
            self._dupe += 1
        else:
            self._last_base, self._dupe = base, 0
        return base if self._dupe == 0 else f"{base}-{self._dupe:02d}"

    def _queue(self, role: str, text: str) -> bool:
        with self._lock:
            path = f"{cloud_sync.CHAT_ROOT}/{self._session}/m{self._n:06d}"
            self._n += 1
        self.engine.enabled = True
        ok = self.engine.queue_record(path, {"role": role, "text": text,
                                             "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
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
            threading.Thread(target=self._loop, name="ksm-chat-sync", daemon=True).start()
        else:
            self._loop()

    def _loop(self) -> None:
        try:
            while True:
                try:
                    if self._switch():
                        st = self.engine.flush(cloud_sync.CHAT_ROOT + "/")
                        pending = self.engine.pending_count(cloud_sync.CHAT_ROOT + "/")
                        self._error = st.get("last_error", "") if pending else ""
                except Exception:  # noqa: BLE001
                    log.exception("Chat sync step failed")
                with self._run_lock:
                    if not self._again:
                        self._running = False
                        return
                    self._again = False
        finally:
            with self._run_lock:
                self._running = False


def start_chat_sync(engine: "cloud_sync.SyncEngine") -> Optional[ChatSync]:
    """Used by the app at start-up. Never raises."""
    try:
        sync = ChatSync(engine)
        sync.start()
        return sync
    except Exception:  # noqa: BLE001
        log.exception("Could not start the online copy of chat")
        return None
