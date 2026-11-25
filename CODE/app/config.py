"""
config.py
---------
Central configuration for Karauli Shankar Mahadev.

Every setting is resolved by app/settings_store.py in this order:
    real environment variable > saved in the common config folder / OS keychain
    > legacy project .env file > built-in default

Nothing sensitive is hardcoded. Edit settings in the app (SETTINGS tab); the
old .env file still works and can be imported with one click.

The values below are plain module attributes (config.GROQ_API_KEY and so on).
reload() re-reads them, so a change made in the Settings tab applies without
restarting the app (except the few settings marked "restart needed").
"""

from pathlib import Path

from app import settings_store

# --- Paths -------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROMPTS_DIR = BASE_DIR / "prompts"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "ksm.log"

# The common settings store (config folder + OS keychain). The project's .env,
# if present, is only read as a fallback and is never modified.
SETTINGS = settings_store.init_store(dotenv_path=BASE_DIR / ".env")

GENERAL_HELPER_PROMPT_FILE = PROMPTS_DIR / "general_helper.txt"
TASK_TRANSLATION_PROMPT_FILE = PROMPTS_DIR / "task_translation_prompt.txt"
SCREEN_PROMPT_FILE = PROMPTS_DIR / "screen_analyzer.txt"
STRATEGY_AUDIT_PROMPT_FILE = PROMPTS_DIR / "strategy_audit.txt"


def _as_bool(text: str) -> bool:
    return str(text).strip().lower() == "true"


def _as_int(text: str, fallback: int) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return fallback


def _as_float(text: str, fallback: float) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return fallback


def reload(store=None) -> None:
    """Re-read every setting from the store into this module's attributes.
    `store` is only passed by tests that use their own temporary store."""
    g = globals()
    v = (store or SETTINGS).snapshot()

    # --- Identity / persona
    g["ASSISTANT_NAME"] = v["ASSISTANT_NAME"]
    g["USER_NAME"] = v["USER_NAME"]
    g["USER_PROFILE"] = v["USER_PROFILE"]   # one short phrase; goes into the prompts

    # --- Data files
    g["DATA_FILE"] = DATA_DIR / v["DATA_FILE_NAME"]

    # --- AI providers
    g["GROQ_API_KEY"] = v["GROQ_API_KEY"]
    g["GROQ_MODEL_ID"] = v["GROQ_MODEL_ID"]
    g["GEMINI_API_KEY"] = v["GEMINI_API_KEY"]
    g["GEMINI_MODEL_ID"] = v["GEMINI_MODEL_ID"]

    # --- Screen Scan micro-service (vision)
    g["SCREEN_MODEL_ID"] = v["SCREEN_MODEL_ID"]
    g["SCREEN_MAX_WIDTH"] = _as_int(v["SCREEN_MAX_WIDTH"], 1920)       # px, screenshot is shrunk to this
    g["SCREEN_JPEG_QUALITY"] = _as_int(v["SCREEN_JPEG_QUALITY"], 85)
    g["SCREEN_HIDE_DELAY"] = _as_float(v["SCREEN_HIDE_DELAY"], 0.8)    # seconds to wait after minimizing

    # --- Debugging
    g["LOG_LEVEL"] = v["LOG_LEVEL"]
    g["DEBUG_UI"] = _as_bool(v["DEBUG_UI"])   # opens webview dev-tools

    # --- Firebase / mobile sync (optional). An absolute path stays as is.
    g["ENABLE_FIREBASE"] = _as_bool(v["ENABLE_FIREBASE"])
    g["FIREBASE_SERVICE_ACCOUNT_PATH"] = BASE_DIR / v["FIREBASE_SERVICE_ACCOUNT_PATH"]
    g["FIREBASE_DATABASE_URL"] = v["FIREBASE_DATABASE_URL"]

    # --- Online copy (Phase 4). Each kind of data is its own switch; all default to off.
    g["SYNC_TASKS"] = _as_bool(v["SYNC_TASKS"])
    g["SYNC_CHAT"] = _as_bool(v["SYNC_CHAT"])
    g["SYNC_AUDITS_SCANS"] = _as_bool(v["SYNC_AUDITS_SCANS"])


reload()

# --- Chat memory --------------------------------------------------------
# Longest the in-memory conversation sent to the AI may get (lines: one per user message or reply). Older lines drop off.
CHAT_HISTORY_LIMIT = 60

# --- Task-escalation thresholds (seconds) --------------------------------
URGENCY_ESCALATE_24H = 86400
URGENCY_ESCALATE_1WEEK = 604800

# --- UI colors -------------------------------------------------------------
CELL_COLORS = {3: "#ffcccc", 2: "#fff3cd", 1: "#d1e7dd"}
HIGHLIGHT_COLOR = "#00ffff"
OVERDUE_COLOR = "#ff4d4d"
AUTO_MIGRATE_COLOR = "#ffa500"

BG_DARK = "#0f172a"
PANEL_DARK = "#1e293b"
ACCENT_BLUE = "#38bdf8"
ACCENT_AMBER = "#fbbf24"
ACCENT_GREEN = "#4ade80"
TEXT_MUTED = "#94a3b8"


def is_groq_configured() -> bool:
    return bool(GROQ_API_KEY)


def is_gemini_configured() -> bool:
    return bool(GEMINI_API_KEY)
