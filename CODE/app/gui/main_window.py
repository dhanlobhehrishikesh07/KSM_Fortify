"""main_window.py — boots the pywebview desktop window that hosts the whole
app (Matrix, Chat, Strategy, Mobile Sync). Real GUI now lives in
app/gui/webui/ (HTML/CSS/JS) rendered by your OS's native web engine
(WebView2 on Windows, WebKit on macOS, WebKitGTK on Linux); this module just
wires that page to the existing backend via WebAPI and starts the optional
Firebase bridge thread, exactly as the old tkinter version did.
"""

from __future__ import annotations

import threading
from pathlib import Path

import webview

from app import cloud_sync, config
from app.ai_assistant import AIAssistant
from app.chat_sync import start_chat_sync
from app.config_sync import start_config_sync
from app.record_sync import start_record_sync
from app.data_store import TaskStore
from app.firebase_bridge import FirebaseBridge, run_bridge_safely
from app.gui.web_api import WebAPI
from app.logger import get_logger, setup_logging
from app.screen_analyzer import ScreenAnalyzer
from app.strategy_officer import StrategyOfficer
from app.task_sync import start_task_sync

log = get_logger(__name__)

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
INDEX_HTML = WEBUI_DIR / "index.html"


def _start_firebase_bridge(api: WebAPI) -> None:
    if not config.ENABLE_FIREBASE:
        api.bridge_log("Firebase sync is disabled. Turn it on in the SETTINGS tab to sync a mobile app.")
        return

    def on_mobile_message(text: str):
        api.bridge_log(f"[MOBILE] {text}")
        api.receive_external_message(text)
        return None  # WebAPI sends its own replies through the reply sink

    bridge = FirebaseBridge(on_message=on_mobile_message)
    api.set_reply_sink(bridge.push_reply)
    api.bridge_log("Starting Firebase listener...")
    # start() runs on a background thread; run_bridge_safely turns any failure
    # into a visible "[ERROR] ..." line on the Mobile Sync tab.
    threading.Thread(target=run_bridge_safely, args=(bridge, api.bridge_log), daemon=True).start()


def start_online_copy(store: TaskStore):
    """Starts the online copy of tasks, chat, audits and scans on ONE shared engine (one outbox file). Never raises.
    Returns (task_sync, chat_sync, record_sync); any may be None."""
    try:
        engine = cloud_sync.engine_from_config()
    except Exception:  # noqa: BLE001
        log.exception("Could not prepare the online copy")
        return None, None, None
    return start_task_sync(store, engine), start_chat_sync(engine), start_record_sync(engine)


def run():
    setup_logging()
    log.info("Starting %s", config.ASSISTANT_NAME)
    store = TaskStore()
    online = start_online_copy(store)    # online copy of tasks, chat, audits and scans; each does nothing unless its switch is on
    live_config = start_config_sync()    # prompts and shared settings from Firebase: returns at once, works in the background
    assistant = AIAssistant()
    officer = StrategyOfficer()
    screen = ScreenAnalyzer()
    api = WebAPI(store, assistant, officer, screen, task_sync=online[0], chat_sync=online[1], record_sync=online[2])

    window = webview.create_window(
        f"{config.ASSISTANT_NAME} — Central Command",
        str(INDEX_HTML),
        js_api=api,
        width=1500,
        height=940,
        min_size=(1100, 700),
        background_color="#050d1a",
    )

    api.set_window(window)
    try:
        webview.start(_start_firebase_bridge, api, debug=config.DEBUG_UI)
    except Exception:
        log.exception("webview crashed")
        raise
    finally:
        if live_config is not None:
            live_config.stop()
        log.info("Application closed")


if __name__ == "__main__":
    run()
