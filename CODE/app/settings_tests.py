"""
settings_tests.py
-----------------
The "Test" buttons of the Settings tab. Each test checks one service with the
values you give it (the saved ones, or unsaved ones you just typed), and
returns a small result dict:

    {"service": "groq", "status": "pass" | "fail" | "warn" | "skip",
     "message": "...", "detail": "...", "ms": 412}

Rules:
* Tests only READ: they list models or read a shallow database key. They send
  no prompt text and use no tokens.
* The key never appears in a result: messages go through redact_text().
* Network clients are created by small factory functions, so tests can swap in
  fakes (see tests/test_settings_tests.py).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from app.logger import get_logger
from app.settings_store import SECRET_KEYS, SettingsStore, redact_text

log = get_logger(__name__)

try:
    from groq import Groq
except ImportError:  # pragma: no cover - optional dependency
    Groq = None

try:
    from google import genai
except ImportError:  # pragma: no cover - optional dependency
    genai = None

try:
    import firebase_admin
    from firebase_admin import credentials, db
except ImportError:  # pragma: no cover - optional dependency
    firebase_admin = None
    credentials = None
    db = None

SERVICES = ("groq", "gemini", "firebase")

Getter = Callable[[str], str]


def scrub(text, secrets=()) -> str:
    """Removes saved secrets AND any extra values (such as a key typed but not saved yet)."""
    text = redact_text(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def _result(service: str, status: str, message: str, detail: str = "", t0: float | None = None) -> dict:
    return {
        "service": service,
        "status": status,
        "message": message,
        "detail": detail,
        "ms": int((time.time() - t0) * 1000) if t0 else 0,
    }


def explain_error(service: str, err: Exception) -> str:
    """Turns an SDK exception into a one-line, human reason (no secrets)."""
    msg = str(err)
    low = msg.lower()
    name = {"groq": "Groq", "gemini": "Gemini", "firebase": "Firebase"}.get(service, service)
    if "401" in msg or "403" in msg or "invalid api key" in low or "api_key_invalid" in low or "permission" in low \
            or "unauthorized" in low or "unauthenticated" in low:
        return f"{name} rejected the key. Check it was copied completely and is still active."
    if "429" in msg or "rate limit" in low or "resource_exhausted" in low or "quota" in low:
        return f"{name} says you are over the rate limit or quota. The key itself may be fine; try again later."
    if "404" in msg or "not found" in low or "not_found" in low:
        return f"{name} could not find that model. Check the model name."
    if any(w in low for w in ("connection", "timed out", "timeout", "name or service", "network", "getaddrinfo", "ssl")):
        return f"Could not reach {name}. Check your internet connection (or proxy)."
    return f"{name} test failed: {msg[:200]}"


# ----------------------------------------------------------------------------
# Groq
# ----------------------------------------------------------------------------


def _make_groq(api_key: str):
    return Groq(api_key=api_key, timeout=15.0, max_retries=0)


def test_groq(get: Getter, client_factory: Callable = None) -> dict:
    t0 = time.time()
    key = get("GROQ_API_KEY")
    if not key:
        return _result("groq", "skip", "No Groq key yet. Paste one first.")
    factory = client_factory or (_make_groq if Groq is not None else None)
    if factory is None:
        return _result("groq", "fail", "The 'groq' package is not installed (pip install -r requirements.txt).")
    model = get("GROQ_MODEL_ID")
    try:
        client = factory(key)
        listing = client.models.list()
        ids = [getattr(m, "id", "") for m in getattr(listing, "data", listing) or []]
    except Exception as e:  # noqa: BLE001
        log.info("Groq test failed (%s)", type(e).__name__)
        return _result("groq", "fail", explain_error("groq", e), str(e)[:300], t0)
    if model and ids and model not in ids:
        return _result(
            "groq", "warn",
            f"Key works, but model '{model}' is not in your list. Chat will fail until you change the model.",
            "Available examples: " + ", ".join(sorted(ids)[:6]), t0,
        )
    return _result("groq", "pass", f"Key works. Model '{model}' is available." if ids else "Key works.", "", t0)


# ----------------------------------------------------------------------------
# Gemini
# ----------------------------------------------------------------------------


def _make_gemini(api_key: str):
    return genai.Client(api_key=api_key)


def _gemini_has(client, model_id: str) -> bool:
    try:
        client.models.get(model=model_id)
        return True
    except Exception as e:  # noqa: BLE001
        low = str(e).lower()
        if "404" in low or "not_found" in low or "not found" in low:
            return False
        raise


def test_gemini(get: Getter, client_factory: Callable = None) -> dict:
    t0 = time.time()
    key = get("GEMINI_API_KEY")
    if not key:
        return _result("gemini", "skip", "No Gemini key yet. Paste one first.")
    factory = client_factory or (_make_gemini if genai is not None else None)
    if factory is None:
        return _result("gemini", "fail", "The 'google-genai' package is not installed (pip install -r requirements.txt).")
    try:
        client = factory(key)
        missing = []
        for label, setting in (("Strategy model", "GEMINI_MODEL_ID"), ("Screen scan model", "SCREEN_MODEL_ID")):
            model_id = get(setting)
            if model_id and not _gemini_has(client, model_id):
                missing.append(f"{label} '{model_id}'")
    except Exception as e:  # noqa: BLE001
        log.info("Gemini test failed (%s)", type(e).__name__)
        return _result("gemini", "fail", explain_error("gemini", e), str(e)[:300], t0)
    if missing:
        return _result("gemini", "warn", "Key works, but not found: " + "; ".join(missing) + ". Fix the model name.", "", t0)
    return _result("gemini", "pass", "Key works. Both models are available.", "", t0)


# ----------------------------------------------------------------------------
# Firebase
# ----------------------------------------------------------------------------


def _firebase_online_check(path: Path, database_url: str) -> None:
    """Connects with a throwaway named app and reads one shallow key. Always cleans up."""
    app = None
    try:
        cred = credentials.Certificate(str(path))
        app = firebase_admin.initialize_app(cred, {"databaseURL": database_url}, name="ksm-settings-test")
        db.reference("/", app=app).get(shallow=True)
    finally:
        if app is not None:
            try:
                firebase_admin.delete_app(app)
            except Exception:  # noqa: BLE001
                pass


def test_firebase(get: Getter, online_check: Callable = None) -> dict:
    t0 = time.time()
    path_text = get("FIREBASE_SERVICE_ACCOUNT_PATH")
    url = get("FIREBASE_DATABASE_URL")
    path = Path(path_text).expanduser() if path_text else None
    if path is None or not path.is_absolute():
        # a relative path means the old project-folder layout
        from app import config
        path = config.BASE_DIR / (path_text or "serviceAccountKey.json")
    if not path.exists():
        return _result("firebase", "fail", "No service account file found. Use 'Import file' to add it.")
    try:
        info = SettingsStore.check_firebase_json(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return _result("firebase", "fail", str(e), "", t0)
    if not url:
        return _result("firebase", "warn", f"Service account is valid (project {info.get('project_id')}), "
                       "but the Database URL is empty.", "", t0)
    if not url.lower().startswith("https://"):
        return _result("firebase", "fail", "The Database URL must start with https://", "", t0)
    check = online_check or (_firebase_online_check if firebase_admin is not None else None)
    if check is None:
        return _result("firebase", "warn", "File and URL look right, but 'firebase-admin' is not installed, "
                       "so the connection was not tested.", "", t0)
    try:
        check(path, url)
    except Exception as e:  # noqa: BLE001
        log.info("Firebase test failed (%s)", type(e).__name__)
        return _result("firebase", "fail", explain_error("firebase", e), str(e)[:300], t0)
    return _result("firebase", "pass", f"Connected to project '{info.get('project_id')}'.", "", t0)


# ----------------------------------------------------------------------------


def run_test(service: str, store: SettingsStore, overrides: dict | None = None, **factories) -> dict:
    """Runs one test. `overrides` are unsaved values typed in the form; they win over saved ones."""
    overrides = {k: str(v).strip() for k, v in (overrides or {}).items() if str(v).strip()}

    def get(key: str) -> str:
        return overrides[key] if key in overrides else store.get(key)

    if service == "groq":
        result = test_groq(get, factories.get("groq_factory"))
    elif service == "gemini":
        result = test_gemini(get, factories.get("gemini_factory"))
    elif service == "firebase":
        result = test_firebase(get, factories.get("firebase_check"))
    else:
        return _result(service, "fail", f"Unknown service: {service}")
    # last line of defence: no key (saved or typed) may leave this module
    secrets = [get(k) for k in SECRET_KEYS]
    result["message"] = scrub(result["message"], secrets)
    result["detail"] = scrub(result["detail"], secrets)
    return result
