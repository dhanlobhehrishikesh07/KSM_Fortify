"""
logger.py
---------
One place for all debug logging.

* Writes to  logs/ksm.log  (rotating, 3 x 1 MB) so you can read it after a crash.
* Keeps the last 2000 lines in memory so the "DEBUG LOG" tab in the GUI can
  show them live.
* Mirrors everything to the console.
* Never writes an API key: every line passes through a filter that replaces any
  stored secret with *** (see settings_store.redact_text).

Usage in any module:
    from app.logger import get_logger
    log = get_logger(__name__)
    log.info("hello")
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler

from app import config, settings_store

ROOT_NAME = "ksm"
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"


class RedactSecretsFilter(logging.Filter):
    """Removes API keys from every log line before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            clean = settings_store.redact_text(msg)
            if record.exc_info and record.exc_info[1] is not None:
                # exception text is formatted later; fold it into the message so it is cleaned too
                import traceback
                tb = "".join(traceback.format_exception(*record.exc_info))
                clean = clean + "\n" + settings_store.redact_text(tb).rstrip()
                record.exc_info = None
                record.exc_text = None
            if clean != msg or record.args:
                record.msg, record.args = clean, ()
        except Exception:  # noqa: BLE001 - logging must never fail because of redaction
            pass
        return True


class RingBufferHandler(logging.Handler):
    """Keeps recent log lines in memory, each with an increasing index."""

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self._lock_buf = threading.Lock()
        self._entries: deque = deque(maxlen=capacity)
        self._total = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            return
        with self._lock_buf:
            self._entries.append((self._total, record.levelname, line))
            self._total += 1

    def since(self, since: int = 0) -> tuple[list[dict], int]:
        with self._lock_buf:
            out = [
                {"level": lvl, "text": txt}
                for idx, lvl, txt in self._entries
                if idx >= since
            ]
            return out, self._total

    def clear(self) -> None:
        with self._lock_buf:
            self._entries.clear()
            # keep _total so pollers' cursors stay valid


_ring = RingBufferHandler()
_configured = False


def setup_logging() -> None:
    """Idempotent. Call once at startup."""
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, config.LOG_LEVEL.upper(), logging.DEBUG)
    root = logging.getLogger(ROOT_NAME)
    root.setLevel(level)
    root.propagate = False
    fmt = logging.Formatter(_FORMAT, _DATEFMT)

    redact = RedactSecretsFilter()

    _ring.setFormatter(fmt)
    _ring.addFilter(redact)
    root.addHandler(_ring)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redact)
    root.addHandler(console)

    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            config.LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(_FORMAT))
        fh.addFilter(redact)
        root.addHandler(fh)
    except OSError as e:  # read-only folder etc. — keep going without a file
        root.warning("Could not open log file %s: %s", config.LOG_FILE, e)

    root.info("Logging started (level=%s, file=%s)", config.LOG_LEVEL, config.LOG_FILE)


def apply_log_level() -> None:
    """Applies config.LOG_LEVEL to the running app (used when the setting changes)."""
    level = getattr(logging, str(config.LOG_LEVEL).upper(), logging.DEBUG)
    logging.getLogger(ROOT_NAME).setLevel(level)


def get_logger(name: str = "") -> logging.Logger:
    short = name.replace("app.", "").replace("gui.", "") if name else ""
    return logging.getLogger(f"{ROOT_NAME}.{short}" if short else ROOT_NAME)


def get_buffer() -> RingBufferHandler:
    return _ring
