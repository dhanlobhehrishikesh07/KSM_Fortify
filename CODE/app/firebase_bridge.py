"""
firebase_bridge.py
--------------------
Optional mobile-sync ministry. Listens on Firebase Realtime Database for
tasks/messages pushed from a phone, hands the raw text to a callback
(normally AIAssistant's pipeline), and can push the AI's reply back so
the mobile app can display it.

Disabled by default (ENABLE_FIREBASE=false in the SETTINGS tab) so the rest of the
app works fully offline without a Firebase project. When enabled, it
needs a real service account file; import it in the SETTINGS tab (it is
copied into the common config folder) — see README.md for setup.

Design note: the original version relayed everything over raw localhost
TCP sockets between separate processes. Since this now runs as a thread
inside the single unified app, it just calls a callback directly instead
— simpler, and nothing to deserialize/lose on the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from app import config, firebase_app
from app.logger import get_logger

log = get_logger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, db
except ImportError:  # pragma: no cover - optional dependency
    firebase_admin = None
    credentials = None
    db = None


class FirebaseBridgeError(RuntimeError):
    pass


class FirebaseBridge:
    """on_message(text) is called for every incoming mobile message, on a
    background thread. Its return value (a reply string, or None) is
    pushed back to Firebase automatically if provided."""

    def __init__(self, on_message: Callable[[str], Optional[str]]):
        self.on_message = on_message
        self._app = None

    @property
    def available(self) -> bool:
        return firebase_admin is not None

    def start(self) -> None:
        if not config.ENABLE_FIREBASE:
            return
        if not self.available:
            raise FirebaseBridgeError("firebase-admin package is not installed.")
        if not config.FIREBASE_SERVICE_ACCOUNT_PATH.exists():
            raise FirebaseBridgeError(
                f"Service account file not found at {config.FIREBASE_SERVICE_ACCOUNT_PATH}"
            )
        if not config.FIREBASE_DATABASE_URL:
            raise FirebaseBridgeError("FIREBASE_DATABASE_URL is not set. Add it in the SETTINGS tab.")

        try:
            self._app = firebase_app.get_app()   # shared with the online copy; created once
        except firebase_app.FirebaseAppError as e:
            raise FirebaseBridgeError(str(e)) from e
        db.reference("incoming_tasks", app=self._app).listen(self._handle_event)

    def _handle_event(self, event) -> None:
        """Called by firebase_admin on its own thread for every change under
        incoming_tasks. Each entry is handled on its own so one failure cannot
        stop the others or kill the listener."""
        data = getattr(event, "data", None)
        if not data:
            return  # deletions (our own cleanup) arrive here with data=None
        path = (getattr(event, "path", "/") or "/").strip("/")
        if isinstance(data, dict) and not path:
            entries = data                       # initial snapshot: {push_id: text, ...}
        elif path:
            entries = {path: data}               # a single new child
        else:
            entries = {"": data}                 # bare value at the root

        for entry_id, text in entries.items():
            ref_path = f"incoming_tasks/{entry_id}" if entry_id else "incoming_tasks"
            if not isinstance(text, str) or not text.strip():
                log.warning("Dropping non-text mobile entry %r", entry_id)
                self._delete(ref_path)
                continue
            try:
                reply = self.on_message(text)
                if reply:
                    self.push_reply(reply)
            except Exception:  # noqa: BLE001
                log.exception("Mobile message handler failed; leaving %r in Firebase for retry", entry_id)
                continue
            self._delete(ref_path)

    @staticmethod
    def _delete(ref_path: str) -> None:
        try:
            db.reference(ref_path).delete()
        except Exception:  # noqa: BLE001
            log.exception("Could not delete %s from Firebase", ref_path)

    def push_reply(self, reply_text: str) -> None:
        if not self._app:
            return
        db.reference("ai_responses").set(
            {"text": reply_text, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        )


def run_bridge_safely(bridge: "FirebaseBridge", report: Callable[[str], None]) -> bool:
    """Runs bridge.start() and reports the outcome through report(). Meant to be
    the target of a background thread: without this, an exception raised inside
    the thread (missing key file, bad URL, no network) would vanish and the
    Mobile Sync tab would look fine while nothing was listening."""
    try:
        bridge.start()
    except FirebaseBridgeError as e:
        report(f"[ERROR] {e}")
        return False
    except Exception as e:  # noqa: BLE001
        log.exception("Firebase bridge failed to start")
        report(f"[ERROR] Firebase failed to start: {e}")
        return False
    report("Firebase listener started. Waiting for messages from the phone.")
    return True
