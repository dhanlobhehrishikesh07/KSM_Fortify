"""
web_api.py
----------
The bridge between the pywebview HTML/CSS/JS front end (app/gui/webui/) and
the existing, GUI-agnostic backend (TaskStore, AIAssistant, StrategyOfficer,
FirebaseBridge). Every public method here is callable from JavaScript as
`window.pywebview.api.<method_name>(...)`.

Design: long-running calls (LLM chat, strategy audit) never block the JS
call itself — they kick off a background thread and the front end polls a
small "get_*_updates" method a few times a second. This keeps pywebview's
single JS-API worker free and avoids any need to reach back into the page
from a background thread.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from app import cloud_sync, config, config_sync, prompt_store, prompt_use, settings_store, settings_tests, shared_settings
from app.ai_assistant import AIAssistant
from app.data_store import TaskStore, TaskStoreError
from app.logger import apply_log_level, get_buffer, get_logger
from app.screen_analyzer import ScreenAnalyzer, ScreenScanError
from app.strategy_officer import StrategyOfficer

log = get_logger(__name__)


class WebAPI:
    def __init__(
        self,
        store: TaskStore,
        assistant: AIAssistant,
        officer: StrategyOfficer,
        screen: ScreenAnalyzer | None = None,
        settings: settings_store.SettingsStore | None = None,
        chat_sync=None,
        record_sync=None,
        task_sync=None,
        prompts=None,
        shared=None,
        hub=None,
    ):
        self._settings = settings or config.SETTINGS
        self._prompts_obj = prompts             # Phase 5E: None = the shared process-wide instance, looked up at call time
        self._shared_obj = shared
        self._hub_obj = hub
        self._store = store
        self._assistant = assistant
        self._officer = officer
        self._screen = screen or ScreenAnalyzer()
        self._window = None  # set by main_window once the pywebview window exists

        self._scan_lock = threading.Lock()
        self._scan = {
            "running": False, "stage": "idle", "scan_id": 0, "text": "", "error": "",
            "preview": "", "meta": {},
        }

        self._session_history: list[str] = []
        self._chat_sync = chat_sync              # online copy of chat (Phase 4D); None or switched off = memory only
        self._task_sync = task_sync              # online copy of tasks (Phase 4C); used for status and Sync now only
        self._record_sync = record_sync          # online copy of audits and scans (Phase 4E); text only, never the image
        self._history_epoch = 0                  # bumped by "Clear context" so a late history load is not applied

        self._chat_lock = threading.Lock()
        self._chat_messages: list[dict] = []
        self._chat_busy = False
        self._pending_external: list[str] = []   # mobile messages waiting for the chat to be free
        self._reply_sink = None                  # callable(str) that sends a reply back to the phone

        self._audit_lock = threading.Lock()
        self._audit_text = ""
        self._audit_running = False

        self._bridge_lock = threading.Lock()
        self._bridge_log: list[str] = []

        # settings tab state
        self._settings_lock = threading.Lock()
        self._restart_keys: list[str] = []
        self._tests: dict[str, dict] = {}
        self._legacy_offer = False
        self._refresh_legacy_offer()
        self._settings.add_listener(self._on_settings_changed)

        if self._assistant.available:
            self._chat_messages.append(
                {"role": "assistant", "text": f"Hi {config.USER_NAME}, {config.ASSISTANT_NAME} here. What's up?"}
            )
        else:
            self._chat_messages.append(
                {"role": "system", "text": "No Groq key yet. Open the SETTINGS tab, paste your key and press Test to enable chat."}
            )

        if self._chat_sync is not None:
            threading.Thread(target=self._restore_chat_history, args=(self._history_epoch,), daemon=True).start()

    # -- app shell / identity -------------------------------------------------
    def get_snapshot(self) -> str:
        self._store.run_auto_escalation()
        return json.dumps(
            {
                "now": self._store.now_str(),
                "tasks": self._store.all_tasks(),
                "groq_available": self._assistant.available,
                "gemini_available": self._officer.available,
                "screen_available": self._screen.available,
                "firebase_enabled": config.ENABLE_FIREBASE,
                "assistant_name": config.ASSISTANT_NAME,
                "user_name": config.USER_NAME,
                "data_warning": self._store.load_warning,
                "legacy_offer": self._legacy_offer,
                "restart_pending": bool(self._restart_keys),
            }
        )

    # -- online copy: status and actions (Phase 4F-1; no UI yet) ----------------------------
    def _sync_parts(self) -> list:
        """(kind, sync object or None, outbox path prefix) for the three online copies."""
        return [("tasks", self._task_sync, cloud_sync.TASK_ROOT + "/"),
                ("chat", self._chat_sync, cloud_sync.CHAT_ROOT + "/"),
                ("records", self._record_sync, None)]

    def _sync_engine(self):
        for _, part, _ in self._sync_parts():
            engine = getattr(part, "engine", None)
            if engine is not None:
                return engine
        return None

    def get_sync_status(self) -> dict:
        """One answer for the page: state is 'synced', 'pending', 'offline' or 'off'. Never raises, never contains a key."""
        try:
            kinds: dict = {}
            engine = self._sync_engine()
            for kind, part, prefix in self._sync_parts():
                if part is None:
                    kinds[kind] = {"state": "off", "switch": False, "pending": 0, "last_error": ""}
                    continue
                try:
                    st = dict(part.status())
                except Exception:  # noqa: BLE001
                    log.exception("Could not read the %s sync status", kind)
                    st = {"state": "offline", "switch": True, "last_error": "Status unavailable."}
                if kind == "records":
                    pending = (part.engine.pending_count(cloud_sync.AUDIT_ROOT + "/")
                               + part.engine.pending_count(cloud_sync.SCAN_ROOT + "/"))
                else:
                    pending = part.engine.pending_count(prefix)   # only this kind's own waiting changes
                kinds[kind] = {"state": st.get("state", "off"), "switch": bool(st.get("switch")),
                               "pending": pending,
                               "last_error": settings_store.redact_text(str(st.get("last_error") or ""))}
            active = [k for k in kinds.values() if k["switch"]]
            pending = sum(k["pending"] for k in active)
            if not active:
                state = "off"
            elif any(k["state"] == "offline" for k in active):
                state = "offline"
            elif pending or any(k["state"] == "pending" for k in active):
                state = "pending"
            else:
                state = "synced"
            last_ms = 0
            if engine is not None:
                last_ms = int(engine.status().get("last_sync_ms") or 0)
            last = datetime.fromtimestamp(last_ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if last_ms else ""
            label = {"off": "Off", "synced": "Synced", "offline": "Offline",
                     "pending": f"Pending {pending}" if pending else "Pending"}[state]
            error = next((k["last_error"] for k in active if k["last_error"]), "") if state == "offline" else ""
            return {"ok": True, "state": state, "label": label, "pending": pending,
                    "last_sync": last, "last_error": error, "kinds": kinds}
        except Exception as e:  # noqa: BLE001
            log.exception("Sync status failed")
            return {"ok": False, "state": "offline", "label": "Offline", "pending": 0, "last_sync": "",
                    "last_error": settings_store.redact_text(str(e)), "kinds": {}}

    def sync_now(self) -> dict:
        """Asks every online copy that is switched on to send what waits. Returns at once with the current status (the
        sending runs in the background; the page's timer shows the result). Never raises."""
        started = []
        try:
            for kind, part, _ in self._sync_parts():
                if part is None:
                    continue
                try:
                    if part.status().get("switch"):
                        part.sync_now()
                        started.append(kind)
                except Exception:  # noqa: BLE001
                    log.exception("Could not start the %s sync", kind)
        except Exception:  # noqa: BLE001
            log.exception("Sync now failed")
        status = self.get_sync_status()
        status["started"] = started
        if not started:
            status["message"] = "Nothing to send: every online copy switch is off."
        return status

    def export_cloud_data(self) -> dict:
        """Saves everything kept online (tasks, chats, audits, scans) as one JSON file in the config folder `exports/`.
        Keys are removed from the text. Download only; sends nothing. Never raises."""
        try:
            engine = self._sync_engine()
            if engine is None:
                return {"ok": False, "error": "The online copy is not set up. Open SETTINGS and check the Firebase settings."}
            data, error = engine.export_all()
            if error:
                return {"ok": False, "error": error}
            payload = {"schema": cloud_sync.SCHEMA_VERSION, "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       **data}
            folder = settings_store.config_dir() / "exports"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"ksm-cloud-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            n = 1
            while path.exists():                       # two exports in one second must not overwrite each other
                path = folder / f"ksm-cloud-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{n}.json"
                n += 1
            tmp = path.with_suffix(".tmp")
            text = settings_store.redact_text(json.dumps(payload, ensure_ascii=False, indent=2))
            tmp.write_text(text, encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)                   # private data: owner only (best effort on Windows)
            except OSError:
                pass
            os.replace(tmp, path)
            counts = {k: len(v) for k, v in data.items()}
            log.info("Cloud data exported (%s)", ", ".join(f"{k}: {v}" for k, v in counts.items()))
            return {"ok": True, "path": str(path), "counts": counts}
        except OSError as e:
            log.error("Export failed: %s", type(e).__name__)
            return {"ok": False, "error": f"Could not write the export file ({type(e).__name__})."}
        except Exception as e:  # noqa: BLE001
            log.exception("Export failed")
            return {"ok": False, "error": settings_store.redact_text(str(e)) or "Export failed."}

    # -- matrix / tasks ---------------------------------------------------------
    def add_task(self, title: str, importance, urgency, date: str, time_: str) -> dict:
        try:
            self._store.add_task(
                title=(title or "").strip(),
                importance=int(importance),
                urgency=int(urgency),
                expected_at=f"{date} {time_}",
                origin="MANUAL",
            )
            return {"ok": True}
        except (ValueError, TypeError, OSError, TaskStoreError) as e:
            log.error("Add task failed: %s", e)
            return {"ok": False, "error": str(e)}

    def delete_task(self, task_id) -> dict:
        self._store.delete_task(int(task_id))
        log.info("Task %s deleted", task_id)
        return {"ok": True}

    def migrate_task(self, task_id, new_imp, new_urg) -> dict:
        result = self._store.migrate_task(int(task_id), int(new_imp), int(new_urg))
        if result is None:
            return {
                "ok": False,
                "error": "You can only move a task to an equal-or-higher priority cell, "
                "one importance step at a time.",
            }
        return {"ok": True}

    def apply_warp(self, days, hours) -> dict:
        try:
            self._store.set_time_warp(float(days), float(hours))
            return {"ok": True}
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": "Warp days/hours must be numbers."}

    # -- chat ---------------------------------------------------------------
    def send_chat(self, text: str) -> dict:
        text = (text or "").strip()
        if not text or not self._assistant.available or self._chat_busy:
            return {"ok": False}
        log.info("Chat message received (%d chars)", len(text))
        with self._chat_lock:
            self._chat_messages.append({"role": "user", "text": f"You: {text}"})
            self._chat_busy = True
        threading.Thread(target=self._run_chat_workflow, args=(text,), daemon=True).start()
        return {"ok": True}

    def set_reply_sink(self, sink) -> None:
        """Called from Python: sink(text) delivers an AI reply back to the phone."""
        self._reply_sink = sink

    def receive_external_message(self, text: str) -> None:
        """Entry point for messages coming from the Firebase bridge (mobile app).
        Never blocks the caller's thread. If the chat is busy the message is
        queued and runs, in order, when the current one finishes."""
        if not self._assistant.available:
            self.bridge_log("[ERROR] AI is not configured (GROQ_API_KEY); mobile message was not processed.")
            return
        with self._chat_lock:
            if self._chat_busy:
                self._pending_external.append(text)
                queued = len(self._pending_external)
            else:
                queued = 0
                self._chat_busy = True
                self._chat_messages.append({"role": "system", "text": "Incoming message from mobile app..."})
        if queued:
            self.bridge_log(f"Chat is busy; mobile message queued ({queued} waiting).")
            return
        threading.Thread(target=self._run_chat_workflow, args=(text, True), daemon=True).start()

    def _run_chat_workflow(self, user_text: str, external: bool = False) -> None:
        try:
            self._session_history.append(f"User: {user_text}")
            self._chat_record("user", user_text)
            self._trim_history()
            reply = self._assistant.chat(self._session_history, self._store.as_json())
            self._session_history.append(f"AI: {reply}")
            self._chat_record("assistant", reply)
            self._trim_history()
            with self._chat_lock:
                self._chat_messages.append({"role": "assistant", "text": f"{config.ASSISTANT_NAME}: {reply}"})

            if self._assistant.reply_contains_trigger(reply):
                with self._chat_lock:
                    self._chat_messages.append({"role": "system", "text": "Running strategic validation..."})
                self._run_task_validation()

            if external and self._reply_sink is not None:
                try:
                    self._reply_sink(reply)
                except Exception:  # noqa: BLE001
                    log.exception("Could not send reply to the phone")
        except Exception as e:  # noqa: BLE001 - surface any backend error to the chat panel
            log.exception("Chat workflow failed")
            with self._chat_lock:
                self._chat_messages.append({"role": "system", "text": f"AI error: {self._redact(e)}"})
        finally:
            next_text = None
            with self._chat_lock:
                if self._pending_external:
                    next_text = self._pending_external.pop(0)   # stay busy: hand over to the next one
                    self._chat_messages.append({"role": "system", "text": "Incoming message from mobile app..."})
                else:
                    self._chat_busy = False
            if next_text is not None:
                threading.Thread(target=self._run_chat_workflow, args=(next_text, True), daemon=True).start()

    def _trim_history(self) -> None:
        """Keeps the conversation sent to the AI to the newest CHAT_HISTORY_LIMIT lines, so a long chat does not grow forever."""
        extra = len(self._session_history) - config.CHAT_HISTORY_LIMIT
        if extra > 0:
            del self._session_history[:extra]

    def _chat_record(self, role: str, text: str) -> None:
        if self._chat_sync is None:
            return
        try:
            self._chat_sync.record(role, text)
        except Exception:  # noqa: BLE001 - the online copy must never break the chat
            log.exception("Chat online copy failed")

    def _restore_chat_history(self, epoch: int) -> None:
        """Background: brings back the last messages from the online copy so the AI remembers earlier sessions."""
        try:
            lines, err = self._chat_sync.load_recent()
        except Exception:  # noqa: BLE001
            log.exception("Could not restore the chat history")
            return
        if err:
            log.info("Earlier chat history was not loaded: %s", err)
            return
        if not lines:
            return
        with self._chat_lock:
            if epoch != self._history_epoch:        # the owner cleared the context while we were loading
                return
            self._session_history[:0] = lines
            self._trim_history()
            self._chat_messages.append(
                {"role": "system", "text": f"Loaded {len(lines)} earlier messages from your online chat history."})

    def _run_task_validation(self) -> None:
        try:
            full_context = "\n".join(self._session_history)
            structured = self._assistant.validate_and_extract(full_context, self._store.as_json())
            with self._chat_lock:
                self._chat_messages.append({"role": "helper", "text": structured})

            for pt in self._assistant.parse_tasks(structured):
                try:
                    self._store.add_task(
                        title=pt.title,
                        importance=pt.importance,
                        urgency=pt.urgency,
                        expected_at=pt.expected_at,
                        origin="AUTO",
                    )
                    msg = f"SUCCESS: '{pt.title}' added to matrix."
                except Exception as e:  # noqa: BLE001 - keep going with the remaining tasks
                    log.exception("Could not add parsed task %r", pt.title)
                    msg = f"Could not add '{pt.title}': {e}"
                with self._chat_lock:
                    self._chat_messages.append({"role": "system", "text": msg})
        except Exception as e:  # noqa: BLE001
            log.exception("Task validation failed")
            with self._chat_lock:
                self._chat_messages.append({"role": "system", "text": f"Validation error: {e}"})

    def get_chat_updates(self, since=0) -> str:
        with self._chat_lock:
            since = max(0, int(since))
            messages = self._chat_messages[since:]
            total = len(self._chat_messages)
            busy = self._chat_busy
        return json.dumps({"messages": messages, "count": total, "busy": busy})

    def clear_chat(self) -> dict:
        with self._chat_lock:
            self._session_history = []
            self._history_epoch += 1
            self._chat_messages.append({"role": "system", "text": "Context cleared: starting a fresh session."})
        if self._chat_sync is not None:
            try:
                self._chat_sync.record_clear()
            except Exception:  # noqa: BLE001
                log.exception("Chat online copy failed")
        return {"ok": True}

    # -- strategy audit -------------------------------------------------------
    def start_audit(self) -> dict:
        if not self._officer.available or self._audit_running:
            return {"ok": False}
        self._audit_running = True
        log.info("Strategy audit started")
        threading.Thread(target=self._perform_audit, daemon=True).start()
        return {"ok": True}

    def _perform_audit(self) -> None:
        try:
            result = self._officer.run_audit(self._store.as_json())
            with self._audit_lock:
                self._audit_text = result
            self._record_audit(result)
        except Exception as e:  # noqa: BLE001
            log.exception("Strategy audit failed")
            with self._audit_lock:
                self._audit_text = f"[ERROR]: {self._redact(e)}"
        finally:
            self._audit_running = False

    def _record_audit(self, text: str) -> None:
        if self._record_sync is None:
            return
        try:
            self._record_sync.record_audit(text)
        except Exception:  # noqa: BLE001
            log.exception("Audit online copy failed")

    def _record_scan(self, question: str, text: str, monitor, model: str) -> None:
        if self._record_sync is None:
            return
        try:
            self._record_sync.record_scan(question, text, monitor, model)   # text only: the image is never passed on
        except Exception:  # noqa: BLE001
            log.exception("Scan online copy failed")

    def get_audit_status(self) -> str:
        with self._audit_lock:
            return json.dumps(
                {"running": self._audit_running, "text": self._audit_text, "available": self._officer.available}
            )

    # -- screen scan (vision micro-service) -----------------------------------
    def set_window(self, window) -> None:
        """Called from Python (not JS) so the scan can minimize/restore the app window."""
        self._window = window

    def start_screen_scan(self, hide_window=True, question="", monitor=1) -> dict:
        if self._scan["running"]:
            return {"ok": False, "error": "A scan is already running."}
        problem = self._screen.problem()
        if problem:
            log.error("Screen scan refused: %s", problem)
            return {"ok": False, "error": problem}
        try:
            monitor = int(monitor)
        except (TypeError, ValueError):
            monitor = 1
        with self._scan_lock:
            self._scan.update(running=True, stage="starting", error="", text="")
        log.info("Screen scan requested (hide_window=%s, monitor=%s, question=%r)", hide_window, monitor, (question or "")[:80])
        threading.Thread(
            target=self._run_screen_scan, args=(bool(hide_window), str(question or ""), monitor), daemon=True
        ).start()
        return {"ok": True}

    def _set_scan_stage(self, stage: str) -> None:
        with self._scan_lock:
            self._scan["stage"] = stage

    def _run_screen_scan(self, hide_window: bool, question: str, monitor: int) -> None:
        minimized = False
        try:
            t0 = time.time()
            if hide_window and self._window is not None:
                self._set_scan_stage("hiding window")
                try:
                    self._window.minimize()
                    minimized = True
                    time.sleep(config.SCREEN_HIDE_DELAY)  # let the OS finish the minimize animation
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not minimize window: %s", e)

            self._set_scan_stage("capturing screen")
            try:
                img = self._screen.capture_screen(monitor)
            finally:
                if minimized:
                    try:
                        self._window.restore()
                    except Exception as e:  # noqa: BLE001
                        log.warning("Could not restore window: %s", e)
            capture_s = time.time() - t0

            self._set_scan_stage("asking AI")
            result = self._screen.analyze(img, question, capture_s=capture_s)
            with self._scan_lock:
                self._scan.update(
                    text=result.text, error="", preview=result.preview_b64,
                    scan_id=self._scan["scan_id"] + 1,
                    meta={
                        "model": result.model,
                        "screen": f"{result.orig_size[0]}x{result.orig_size[1]}",
                        "sent": f"{result.sent_size[0]}x{result.sent_size[1]}",
                        "kb": round(result.sent_bytes / 1024, 1),
                        "capture_s": round(result.capture_s, 2),
                        "api_s": round(result.api_s, 2),
                        "total_s": round(time.time() - t0, 2),
                    },
                )
            self._record_scan(question, result.text, monitor, result.model)
            log.info("Screen scan finished in %.2fs", time.time() - t0)
        except ScreenScanError as e:
            log.error("Screen scan failed: %s", e)
            with self._scan_lock:
                self._scan.update(error=self._redact(e), scan_id=self._scan["scan_id"] + 1)
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected screen scan error")
            with self._scan_lock:
                self._scan.update(error=f"Unexpected error: {self._redact(e)}", scan_id=self._scan["scan_id"] + 1)
        finally:
            with self._scan_lock:
                self._scan.update(running=False, stage="idle")

    def get_screen_status(self, seen_scan_id=0) -> str:
        """Preview image is only included when the page hasn't seen this scan yet."""
        with self._scan_lock:
            s = dict(self._scan)
        try:
            seen = int(seen_scan_id)
        except (TypeError, ValueError):
            seen = 0
        if s["scan_id"] == seen:
            s["preview"] = ""
            s["fresh"] = False
        else:
            s["fresh"] = True
        s["available"] = self._screen.available
        s["problem"] = self._screen.problem()
        s["model"] = config.SCREEN_MODEL_ID
        return json.dumps(s)

    # -- debug log -----------------------------------------------------------------
    def get_debug_log(self, since=0) -> str:
        try:
            since = max(0, int(since))
        except (TypeError, ValueError):
            since = 0
        entries, total = get_buffer().since(since)
        return json.dumps({"entries": entries, "count": total, "file": str(config.LOG_FILE)})

    def clear_debug_log(self) -> dict:
        get_buffer().clear()
        log.info("Debug log view cleared (file on disk is untouched)")
        return {"ok": True}

    def log_client(self, level, message) -> None:
        """JS errors / notes from the page land in the same log."""
        lvl = {"error": log.error, "warn": log.warning}.get(str(level), log.info)
        lvl("[JS] %s", str(message)[:500])

    # -- mobile / firebase bridge log -----------------------------------------
    def bridge_log(self, text: str) -> None:
        text = self._redact(text)
        log.info("[bridge] %s", text)
        with self._bridge_lock:
            self._bridge_log.append(text)

    def get_bridge_log(self, since=0) -> str:
        with self._bridge_lock:
            since = max(0, int(since))
            entries = self._bridge_log[since:]
            total = len(self._bridge_log)
        return json.dumps({"entries": entries, "count": total, "enabled": config.ENABLE_FIREBASE})

    # -- settings tab ---------------------------------------------------------------
    def _redact(self, text) -> str:
        """Hides every saved API key in text that may be shown to the user or logged."""
        return settings_store.redact_text(text, self._settings)

    def _refresh_legacy_offer(self) -> None:
        try:
            info = self._settings.legacy_summary()
            self._legacy_offer = bool(
                not info["dismissed"] and (info["dotenv_keys"] or info["firebase_file"])
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not check for a legacy .env")
            self._legacy_offer = False

    def _on_settings_changed(self, keys: list[str]) -> None:
        """Runs after any saved change: re-reads config and rebuilds the AI clients, no restart."""
        was_groq, was_gemini = self._assistant.available, self._officer.available
        config.reload(self._settings)
        keyset = set(keys)
        if keyset & {"GROQ_API_KEY"}:
            self._assistant.refresh()
        if keyset & {"GEMINI_API_KEY"}:
            self._officer.refresh()
            self._screen.refresh()
        if "LOG_LEVEL" in keyset:
            apply_log_level()
        for k in keys:
            d = settings_store.BY_KEY.get(k)
            if d is not None and d.restart:
                with self._settings_lock:
                    if d.label not in self._restart_keys:
                        self._restart_keys.append(d.label)
        self._refresh_legacy_offer()
        self._apply_sync_switches(keyset)
        note = None
        if self._assistant.available and not was_groq:
            note = "Groq key accepted. Chat is ready."
        elif was_groq and not self._assistant.available:
            note = "Groq key removed. Chat is off until you add one in SETTINGS."
        if note:
            with self._chat_lock:
                self._chat_messages.append({"role": "system", "text": note})
        if self._officer.available != was_gemini:
            log.info("Gemini availability is now %s", self._officer.available)

    def _apply_sync_switches(self, keyset: set) -> None:
        """The three online-copy switches act at once (no restart). The workers read the switch every time they run, so
        turning one off already stops sending; turning one on needs one nudge so waiting changes go out now (for tasks:
        the backup and first upload). Never raises."""
        for key, part in (("SYNC_TASKS", self._task_sync), ("SYNC_CHAT", self._chat_sync),
                          ("SYNC_AUDITS_SCANS", self._record_sync)):
            if key not in keyset or part is None:
                continue
            try:
                part.sync_now()                  # does nothing when the switch is off
            except Exception:  # noqa: BLE001
                log.exception("Could not start the online copy after a switch change")

    def settings_get(self) -> str:
        data = self._settings.describe()
        with self._settings_lock:
            data["restart_pending"] = list(self._restart_keys)
        data["shared"] = self.config_status()          # Phase 5E: badges, this device, edits that lost, status (never raises)
        return json.dumps(data)

    def settings_set(self, key, value) -> dict:
        key = str(key)
        defn = settings_store.BY_KEY.get(key)
        if defn is None:
            return {"ok": False, "error": f"Unknown setting: {key}"}
        res = self._settings.set(key, value)
        if not res.ok:
            return {"ok": False, "error": res.error}
        return {"ok": True, "warning": res.warning, "restart": defn.restart,
                "conflict": self._settings.stored_source_conflict(key)}

    def settings_delete(self, key) -> dict:
        res = self._settings.delete(str(key))
        return {"ok": res.ok, "error": res.error}

    def settings_reveal(self, key) -> dict:
        try:
            return {"ok": True, "value": self._settings.reveal(str(key))}
        except settings_store.SettingsError as e:
            return {"ok": False, "error": str(e)}

    def settings_history(self) -> str:
        return json.dumps({"lines": self._settings.history(60)})

    def settings_import_env_text(self, text, overwrite=False) -> dict:
        try:
            r = self._settings.import_env_text(str(text or ""), bool(overwrite))
        except settings_store.SettingsError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **r}

    def settings_import_legacy(self) -> dict:
        r = self._settings.import_legacy()
        self._settings.notify_all()
        return {"ok": True, **r}

    def settings_dismiss_legacy(self) -> dict:
        self._settings.dismiss_legacy_offer()
        self._refresh_legacy_offer()
        return {"ok": True}

    def settings_import_firebase_text(self, text) -> dict:
        try:
            info = self._settings.import_firebase_text(str(text or ""))
        except settings_store.SettingsError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "project_id": info["project_id"], "client_email": info["client_email"]}

    def settings_remove_firebase_file(self) -> dict:
        try:
            self._settings.remove_firebase_file()
        except settings_store.SettingsError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def settings_restore_text(self, text) -> dict:
        try:
            r = self._settings.import_json(str(text or ""))
        except settings_store.SettingsError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **r}

    def settings_backup_text(self) -> str:
        return self._settings.export_json()

    # file dialogs (need the real window, so they return an error in tests / headless runs)
    def _dialog(self, save=False, name="", types=()):
        if self._window is None:
            return None
        import webview
        fd = getattr(webview, "FileDialog", None)
        kind = (fd.SAVE if fd else webview.SAVE_DIALOG) if save else (fd.OPEN if fd else webview.OPEN_DIALOG)
        kwargs = {"file_types": tuple(types)}
        if save:
            kwargs["save_filename"] = name
        picked = self._window.create_file_dialog(kind, **kwargs)
        if not picked:
            return None
        return Path(picked if isinstance(picked, str) else picked[0])

    def settings_pick_firebase_file(self) -> dict:
        path = self._dialog(types=("JSON files (*.json)", "All files (*.*)"))
        if path is None:
            return {"ok": False, "cancelled": True}
        try:
            info = self._settings.import_firebase_file(path)
        except settings_store.SettingsError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "project_id": info["project_id"], "client_email": info["client_email"]}

    def settings_pick_env_file(self, overwrite=False) -> dict:
        path = self._dialog(types=("Env files (*.env;.env;*.txt)", "All files (*.*)"))
        if path is None:
            return {"ok": False, "cancelled": True}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": f"Could not read that file ({e})."}
        return self.settings_import_env_text(text, overwrite)

    def settings_save_backup(self) -> dict:
        path = self._dialog(save=True, name="ksm-settings-backup.json", types=("JSON files (*.json)",))
        if path is None:
            return {"ok": False, "cancelled": True}
        try:
            path.write_text(self._settings.export_json(), encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": f"Could not save the backup ({e})."}
        return {"ok": True, "path": str(path)}

    def settings_pick_restore(self) -> dict:
        path = self._dialog(types=("JSON files (*.json)", "All files (*.*)"))
        if path is None:
            return {"ok": False, "cancelled": True}
        try:
            return self.settings_restore_text(path.read_text(encoding="utf-8"))
        except OSError as e:
            return {"ok": False, "error": f"Could not read that file ({e})."}

    def settings_open_folder(self) -> dict:
        folder = self._settings.dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"Could not open the folder ({e}). It is: {folder}"}
        return {"ok": True, "path": str(folder)}

    # connection tests run in the background; the page polls settings_test_status
    def settings_test(self, service, overrides=None) -> dict:
        service = str(service)
        if service not in settings_tests.SERVICES:
            return {"ok": False, "error": f"Unknown service: {service}"}
        if isinstance(overrides, str):
            try:
                overrides = json.loads(overrides or "{}")
            except ValueError:
                overrides = {}
        overrides = {k: v for k, v in (overrides or {}).items() if k in settings_store.BY_KEY}
        with self._settings_lock:
            if self._tests.get(service, {}).get("running"):
                return {"ok": False, "error": "A test for this service is already running."}
            self._tests[service] = {"running": True, "result": None}
        threading.Thread(target=self._run_settings_test, args=(service, overrides), daemon=True).start()
        return {"ok": True}

    def _run_settings_test(self, service: str, overrides: dict) -> None:
        try:
            result = settings_tests.run_test(service, self._settings, overrides, **getattr(self, "_test_factories", {}))
        except Exception as e:  # noqa: BLE001
            log.exception("Settings test crashed")
            result = {"service": service, "status": "fail",
                      "message": self._redact(f"Test crashed: {e}"), "detail": "", "ms": 0}
        with self._settings_lock:
            self._tests[service] = {"running": False, "result": result}

    def settings_test_status(self) -> str:
        with self._settings_lock:
            return json.dumps(self._tests)

    # -- Phase 5E: prompts and shared configuration -------------------------------------------------------------------
    # Methods only (RULEBOOK section 7). Every method answers with a plain dict, never raises, and never carries a key:
    # error text goes through `_redact`. The page never reaches the stores themselves, only these answers.
    _ROLE_LABELS = {"chat": "AI chat", "task_extraction": "Task extraction", "audit": "Strategy audit",
                    "screen_scan": "Screen scan"}

    def _ps(self):
        return self._prompts_obj or prompt_store.get_store()

    def _sh(self):
        return self._shared_obj or shared_settings.get_shared()

    def _hb(self):
        return self._hub_obj or config_sync.get_hub()

    def _changes(self, raw):
        """The editor sends {block id: {text?, enabled?, title?, order?} | null} as a JSON string (or a dict)."""
        if raw is None or raw == "":
            return {}
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            raise ValueError("The changes are not valid.")
        return data

    def _prompt_call(self, what: str, fn) -> dict:
        try:
            out = fn()
            if hasattr(out, "as_dict"):
                out = out.as_dict()
            out = dict(out) if isinstance(out, dict) else {"ok": True, "value": out}
            out.setdefault("ok", True)
            for k in ("message", "error"):
                if isinstance(out.get(k), str):
                    out[k] = self._redact(out[k])
            return out
        except Exception as e:  # noqa: BLE001
            log.exception("Prompt call failed: %s", what)
            msg = self._redact(f"{what} did not work ({type(e).__name__}). Nothing was changed.")
            return {"ok": False, "message": msg, "error": msg, "errors": [msg], "warnings": []}

    def prompts_get(self) -> dict:
        """The list for the PROMPTS tab: every prompt, the active one per role, the sync status and the warnings."""
        def go():
            ps = self._ps()
            roles = [{"role": r, "label": self._ROLE_LABELS.get(r, r), "active_id": ps.active_id(r),
                      "notes": [self._redact(n) for n in prompt_use.notes(r)]} for r in prompt_store.ROLES]
            return {"ok": True, "roles": roles, "prompts": ps.list_prompts(), "status": ps.status(),
                    "online": ps.enabled(), "limits": {"block": prompt_store.MAX_BLOCK_CHARS,
                                                       "total": prompt_store.MAX_TOTAL_CHARS,
                                                       "soft": prompt_store.SOFT_WARN_CHARS}}
        return self._prompt_call("Loading the prompts", go)

    def prompt_get(self, prompt_id) -> dict:
        return self._prompt_call("Opening the prompt", lambda: self._ps().get_prompt(str(prompt_id)))

    def prompt_validate(self, prompt_id, changes=None) -> dict:
        """Live guard check while typing: what a save of `changes` would refuse or warn about. Saves nothing."""
        return self._prompt_call("Checking the prompt", lambda: self._ps().validate(str(prompt_id), self._changes(changes)))

    def prompt_preview(self, prompt_id, changes=None) -> dict:
        """The final text with the placeholders filled and the estimated tokens. Saves nothing, uses no AI tokens."""
        return self._prompt_call("Building the preview", lambda: self._ps().preview(str(prompt_id), self._changes(changes)))

    def prompt_save(self, prompt_id, changes=None, base_version=None, note="") -> dict:
        def go():
            base = None if base_version in (None, "") else int(base_version)
            return self._ps().save(str(prompt_id), self._changes(changes), base, str(note or ""))
        return self._prompt_call("Saving the prompt", go)

    def prompt_save_as_new(self, role, name, changes=None, description="", from_prompt=None) -> dict:
        return self._prompt_call("Saving the new prompt", lambda: self._ps().save_as_new(
            str(role), str(name or ""), self._changes(changes), str(description or ""), str(from_prompt) if from_prompt else None))

    def prompt_restore(self, prompt_id, version) -> dict:
        return self._prompt_call("Restoring the version", lambda: self._ps().restore(str(prompt_id), int(version)))

    def prompt_history(self, prompt_id) -> dict:
        return self._prompt_call("Loading the history", lambda: self._ps().history(str(prompt_id)))

    def prompt_reset_block(self, prompt_id, block_id) -> dict:
        return self._prompt_call("Resetting the block", lambda: self._ps().reset_block(str(prompt_id), str(block_id)))

    def prompt_reset(self, prompt_id) -> dict:
        return self._prompt_call("Resetting the prompt", lambda: self._ps().reset_prompt(str(prompt_id)))

    def prompt_delete(self, prompt_id) -> dict:
        return self._prompt_call("Deleting the prompt", lambda: self._ps().delete(str(prompt_id)))

    def prompt_set_active(self, role, prompt_id) -> dict:
        return self._prompt_call("Choosing the prompt", lambda: self._ps().set_active(str(role), str(prompt_id)))

    def prompt_resolve_conflict(self, prompt_id, keep) -> dict:
        """After a save made elsewhere: keep is "mine" or "theirs"."""
        return self._prompt_call("Resolving the conflict", lambda: self._ps().resolve_conflict(str(prompt_id), str(keep)))

    def prompt_new_block_id(self) -> dict:
        return self._prompt_call("Adding a block", lambda: {"ok": True, "id": self._ps().new_block_id()})

    def config_status(self) -> dict:
        """Status of the shared configuration (both layers, the listener, this device, edits that lost). Never raises."""
        def go():
            sh = self._sh()
            hub = self._hb()
            try:
                dev = sh.device()
            except Exception:  # noqa: BLE001
                dev = {}
            return {"ok": True, "hub": hub.status(), "settings": sh.status(), "prompts": self._ps().status(),
                    "notices": sh.notices(), "overridden": sh.overridden(), "device": {
                        "device_id": str(dev.get("device_id", "")), "name": str(dev.get("name", ""))}}
        return self._prompt_call("Reading the status", go)

    def config_dismiss_notices(self) -> dict:
        return self._prompt_call("Dismissing the notices", lambda: {"ok": True, "dismissed": self._sh().dismiss_notices()})

    def get_theme(self) -> dict:
        """The theme of THIS device (the per-device setting THEME)."""
        try:
            return {"ok": True, "theme": self._settings.resolve("THEME").value}
        except Exception:  # noqa: BLE001
            return {"ok": False, "theme": "scifi"}
