"""
firebase_app.py
----------------
The ONE place that creates the Firebase app. The mobile-sync listener
(firebase_bridge.py) and the online copy (cloud_sync.py) both need it, and
firebase_admin refuses a second initialize_app() on the same name, so both ask
here and share the same app.

The Settings tab's Test button deliberately does NOT use this: it makes a
throwaway named app and deletes it, so testing never disturbs a running app.
"""

from __future__ import annotations

import threading

from app import config
from app.logger import get_logger

log = get_logger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials
except ImportError:  # pragma: no cover - optional dependency
    firebase_admin = None
    credentials = None

_lock = threading.Lock()


class FirebaseAppError(RuntimeError):
    """A user-presentable reason the Firebase app could not be created."""


def available() -> bool:
    return firebase_admin is not None


def get_app():
    """Returns the shared default Firebase app, creating it on first use."""
    if firebase_admin is None:
        raise FirebaseAppError("firebase-admin package is not installed.")
    with _lock:
        try:
            return firebase_admin.get_app()
        except ValueError:
            pass  # not created yet
        path = config.FIREBASE_SERVICE_ACCOUNT_PATH
        if not path.exists():
            raise FirebaseAppError(f"Service account file not found at {path}")
        if not config.FIREBASE_DATABASE_URL:
            raise FirebaseAppError("FIREBASE_DATABASE_URL is not set. Add it in the SETTINGS tab.")
        cred = credentials.Certificate(str(path))
        app = firebase_admin.initialize_app(cred, {"databaseURL": config.FIREBASE_DATABASE_URL})
        log.info("Firebase app created")
        return app
