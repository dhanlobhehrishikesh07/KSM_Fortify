"""Phase 3B-3D: the Settings methods of WebAPI and live apply without restart."""
import json
import logging
import time

import pytest

from app import config, settings_store
from app.ai_assistant import AIAssistant
from app.data_store import TaskStore
from app.gui.web_api import WebAPI
from app.logger import RedactSecretsFilter
from app.screen_analyzer import ScreenAnalyzer
from app.settings_store import FileBackend, SettingsStore
from app.strategy_officer import StrategyOfficer

GROQ = "gsk_abcdefghijklmnop1234"
GEMINI = "AIzaSyABCDEFGHIJKLMNOP9876"
FB = {"type": "service_account", "project_id": "demo", "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
      "client_email": "b@demo.iam.gserviceaccount.com"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    st = SettingsStore(directory=tmp_path / "cfg", dotenv_path=proj / ".env", environ={},
                       secret_backend=FileBackend(tmp_path / "cfg" / "secrets.json"))
    config.reload(st)
    api = WebAPI(TaskStore(data_file=tmp_path / "m.json"), AIAssistant(), StrategyOfficer(), ScreenAnalyzer(), settings=st)
    return api, st, proj


def last_chat(api):
    return json.loads(api.get_chat_updates(0))["messages"][-1]["text"]


def test_saving_a_groq_key_turns_chat_on_without_a_restart(env):
    api, st, _ = env
    assert json.loads(api.get_snapshot())["groq_available"] is False
    res = api.settings_set("GROQ_API_KEY", GROQ)
    assert res["ok"] and res["restart"] is False
    assert json.loads(api.get_snapshot())["groq_available"] is True
    assert config.GROQ_API_KEY == GROQ and api._assistant.available
    assert "Chat is ready" in last_chat(api)


def test_deleting_the_key_turns_chat_off_again(env):
    api, st, _ = env
    api.settings_set("GROQ_API_KEY", GROQ)
    assert api.settings_delete("GROQ_API_KEY")["ok"]
    assert api._assistant.available is False and config.GROQ_API_KEY == ""
    assert "Chat is off" in last_chat(api)


def test_gemini_key_enables_strategy_and_screen_scan(env):
    api, st, _ = env
    api.settings_set("GEMINI_API_KEY", GEMINI)
    assert api._officer.available and api._screen.problem() == ""  # needs google-genai + Pillow installed
    assert json.loads(api.get_snapshot())["gemini_available"] is True


def test_model_and_tuning_changes_apply_live(env):
    api, st, _ = env
    api.settings_set("GROQ_MODEL_ID", "my-model")
    api.settings_set("SCREEN_MAX_WIDTH", "1280")
    api.settings_set("USER_NAME", "Ravi")
    assert (config.GROQ_MODEL_ID, config.SCREEN_MAX_WIDTH, config.USER_NAME) == ("my-model", 1280, "Ravi")
    assert json.loads(api.get_snapshot())["user_name"] == "Ravi"


def test_bad_values_are_refused_and_nothing_changes(env):
    api, st, _ = env
    r = api.settings_set("SCREEN_MAX_WIDTH", "99999")
    assert r["ok"] is False and "at most" in r["error"] and config.SCREEN_MAX_WIDTH == 1920
    assert api.settings_set("NOPE", "x")["ok"] is False


def test_restart_needed_settings_are_listed_not_applied_silently(env):
    api, st, _ = env
    assert json.loads(api.get_snapshot())["restart_pending"] is False
    r = api.settings_set("ENABLE_FIREBASE", "true")
    assert r["restart"] is True
    assert json.loads(api.get_snapshot())["restart_pending"] is True
    assert "Enable mobile sync" in json.loads(api.settings_get())["restart_pending"]


def test_settings_get_masks_secrets(env):
    api, st, _ = env
    api.settings_set("GROQ_API_KEY", GROQ)
    raw = api.settings_get()
    assert GROQ not in raw
    groq = [e for e in json.loads(raw)["entries"] if e["key"] == "GROQ_API_KEY"][0]
    assert groq["shown"] == "gsk_…1234" and groq["value"] == "" and groq["is_set"]


def test_reveal_is_available_for_secrets_only_and_is_logged(env):
    api, st, _ = env
    api.settings_set("GROQ_API_KEY", GROQ)
    assert api.settings_reveal("GROQ_API_KEY") == {"ok": True, "value": GROQ}
    assert api.settings_reveal("USER_NAME")["ok"] is False
    assert any("reveal" in line for line in json.loads(api.settings_history())["lines"])


def test_legacy_offer_flag_import_and_dismiss(env):
    api, st, proj = env
    (proj / ".env").write_text(f"GROQ_API_KEY={GROQ}\n", encoding="utf-8")
    (proj / "serviceAccountKey.json").write_text(json.dumps(FB))
    st.reload_from_disk()
    api._refresh_legacy_offer()
    assert json.loads(api.get_snapshot())["legacy_offer"] is True
    r = api.settings_import_legacy()
    assert r["ok"] and "GROQ_API_KEY" in r["imported"]
    assert api._assistant.available                      # live, thanks to notify_all
    assert json.loads(api.get_snapshot())["legacy_offer"] is False


def test_dismiss_hides_the_offer(env):
    api, st, proj = env
    (proj / ".env").write_text(f"GROQ_API_KEY={GROQ}\n", encoding="utf-8")
    st.reload_from_disk()
    api._refresh_legacy_offer()
    assert api._legacy_offer is True
    api.settings_dismiss_legacy()
    assert api._legacy_offer is False


def test_import_env_text_and_firebase_text_through_the_api(env):
    api, st, _ = env
    r = api.settings_import_env_text(f"GEMINI_API_KEY={GEMINI}")
    assert r["ok"] and r["imported"] == ["GEMINI_API_KEY"] and api._officer.available
    assert api.settings_import_env_text("junk")["ok"] is False
    fb = api.settings_import_firebase_text(json.dumps(FB))
    assert fb["ok"] and fb["project_id"] == "demo"
    assert api.settings_import_firebase_text("{}")["ok"] is False
    assert json.loads(api.settings_get())["firebase_file"]["in_store"] is True
    assert api.settings_remove_firebase_file()["ok"]


def test_file_dialogs_fail_politely_without_a_window(env):
    api, st, _ = env
    for call in (api.settings_pick_firebase_file, api.settings_pick_env_file, api.settings_save_backup, api.settings_pick_restore):
        assert call() == {"ok": False, "cancelled": True}


def test_backup_text_roundtrip_through_the_api(env):
    api, st, _ = env
    api.settings_set("USER_NAME", "Ravi")
    api.settings_set("GROQ_API_KEY", GROQ)
    text = api.settings_backup_text()
    assert GROQ not in text
    api.settings_delete("USER_NAME")
    r = api.settings_restore_text(text)
    assert r["ok"] and config.USER_NAME == "Ravi"


def _wait_for_test(api, service, seconds=3):
    end = time.time() + seconds
    while time.time() < end:
        state = json.loads(api.settings_test_status()).get(service, {})
        if state and not state["running"]:
            return state["result"]
        time.sleep(0.02)
    raise AssertionError("test did not finish")


def test_connection_test_runs_in_background_and_reports(env):
    from types import SimpleNamespace
    api, st, _ = env
    api._test_factories = {"groq_factory": lambda k: SimpleNamespace(models=SimpleNamespace(
        list=lambda: SimpleNamespace(data=[SimpleNamespace(id=config.GROQ_MODEL_ID)])))}
    assert api.settings_test("groq", json.dumps({"GROQ_API_KEY": GROQ}))["ok"]
    result = _wait_for_test(api, "groq")
    assert result["status"] == "pass" and GROQ not in json.dumps(result)
    assert api.settings_test("bogus")["ok"] is False


def test_a_crashing_test_becomes_a_failed_result(env):
    api, st, _ = env
    api._test_factories = {"groq_factory": lambda k: (_ for _ in ()).throw(RuntimeError(f"boom {GROQ}"))}
    api.settings_set("GROQ_API_KEY", GROQ)
    api.settings_test("groq")
    result = _wait_for_test(api, "groq")
    assert result["status"] == "fail" and GROQ not in json.dumps(result)


# ------------------------------------------------------------------ keys never reach the log
def test_log_filter_removes_secrets_from_messages_and_tracebacks(tmp_path):
    st = SettingsStore(directory=tmp_path / "cfg", environ={}, secret_backend=FileBackend(tmp_path / "s.json"))
    st.set("GROQ_API_KEY", GROQ)
    settings_store._store = st
    try:
        f = RedactSecretsFilter()
        rec = logging.LogRecord("ksm.x", logging.ERROR, __file__, 1, "request failed with key %s", (GROQ,), None)
        assert f.filter(rec) and GROQ not in rec.getMessage() and "***" in rec.getMessage()
        try:
            raise RuntimeError("401 for " + GROQ)
        except RuntimeError:
            import sys
            rec2 = logging.LogRecord("ksm.x", logging.ERROR, __file__, 2, "boom", (), sys.exc_info())
        f.filter(rec2)
        assert GROQ not in rec2.getMessage() and rec2.exc_info is None
    finally:
        settings_store._store = config.SETTINGS


def test_chat_and_bridge_errors_are_redacted(env):
    api, st, _ = env
    api.settings_set("GROQ_API_KEY", GROQ)
    api.bridge_log(f"[ERROR] failed {GROQ}")
    assert GROQ not in json.loads(api.get_bridge_log(0))["entries"][-1]


def test_only_the_api_methods_are_exposed_to_the_page(env):
    """pywebview walks every PUBLIC attribute of js_api and exposes what it finds.
    A public `settings`/`store`/... attribute once exposed hundreds of functions
    (including keychain get/delete) and broke the whole UI. Keep them private."""
    import inspect
    api, _, _ = env
    public = [n for n in dir(api) if not n.startswith("_")]
    for n in public:
        assert inspect.ismethod(getattr(api, n)), f"public non-method attribute on WebAPI: {n}"


# ------------------------------------------------------------------ Phase 4F-2: online-copy switches act at once
@pytest.fixture
def live(tmp_path, env):
    """WebAPI wired to real TaskSync/ChatSync/RecordSync that use the REAL settings switches, on one fake cloud.
    The engine starts disabled, as at start-up when every switch is off."""
    from app import cloud_sync as cs
    from app.chat_sync import ChatSync
    from app.record_sync import RecordSync
    from app.task_sync import TaskSync

    api0, st, proj = env
    cloud = cs.MemoryCloud()
    engine = cs.SyncEngine(cloud, state_path=tmp_path / "sync_state.json", enabled=False)
    store = TaskStore(data_file=tmp_path / "live.json")
    task = TaskSync(store, engine, backup_dir=tmp_path / "bk", marker_path=tmp_path / "task_sync.json",
                    background=False, retry_seconds=None)
    task.start()                                   # all switches are off: nothing happens
    chat = ChatSync(engine, background=False)
    rec = RecordSync(engine, background=False)
    api = WebAPI(store, AIAssistant(), StrategyOfficer(), ScreenAnalyzer(), settings=st,
                 task_sync=task, chat_sync=chat, record_sync=rec)
    yield SimpleNs(api=api, cloud=cloud, store=store, rec=rec, chat=chat, task=task)
    for k in ("SYNC_TASKS", "SYNC_CHAT", "SYNC_AUDITS_SCANS"):
        st.set(k, "false")
    config.reload(st)


class SimpleNs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def paths(cloud, root):
    return [p for p in cloud.data if p.startswith(root + "/")]


def test_switch_settings_do_not_ask_for_a_restart(env):
    api, st, _ = env
    for key in ("SYNC_TASKS", "SYNC_CHAT", "SYNC_AUDITS_SCANS"):
        r = api.settings_set(key, "true")
        assert r["ok"] and r["restart"] is False, key
    assert json.loads(api.get_snapshot())["restart_pending"] is False
    assert json.loads(api.settings_get())["restart_pending"] == []


def test_audits_switch_off_sends_nothing_and_on_resumes_without_restart(live):
    from app import cloud_sync as cs
    live.rec.record_audit("first audit")
    assert paths(live.cloud, cs.AUDIT_ROOT) == []                       # switch off at start: nothing sent
    assert live.api.settings_set("SYNC_AUDITS_SCANS", "true")["ok"]
    live.rec.record_audit("second audit")                               # engine was disabled at start; switch works live
    assert len(paths(live.cloud, cs.AUDIT_ROOT)) == 1
    assert live.api.settings_set("SYNC_AUDITS_SCANS", "false")["ok"]
    live.rec.record_audit("third audit")
    assert len(paths(live.cloud, cs.AUDIT_ROOT)) == 1                   # off again: nothing more goes out
    live.api.settings_set("SYNC_AUDITS_SCANS", "true")
    live.rec.record_audit("fourth audit")
    assert len(paths(live.cloud, cs.AUDIT_ROOT)) == 2                   # resumes


def test_chat_switch_off_sends_nothing_and_on_resumes_without_restart(live):
    from app import cloud_sync as cs
    live.chat.record("user", "hello while off")
    assert paths(live.cloud, cs.CHAT_ROOT) == []
    live.api.settings_set("SYNC_CHAT", "true")
    live.chat.record("user", "hello while on")
    assert len(paths(live.cloud, cs.CHAT_ROOT)) == 1
    live.api.settings_set("SYNC_CHAT", "false")
    live.chat.record("user", "hello off again")
    assert len(paths(live.cloud, cs.CHAT_ROOT)) == 1


def test_turning_tasks_on_uploads_existing_tasks_at_once(live):
    """Without the nudge nothing would go up until the next task change (and the backup would wait too)."""
    from app import cloud_sync as cs
    live.store.add_task("Existing task", 2, 2, "2030-01-01 09:00:00")
    assert paths(live.cloud, cs.TASK_ROOT) == []
    live.api.settings_set("SYNC_TASKS", "true")
    assert len(live.store.all_tasks()) == 1
    assert len(paths(live.cloud, cs.TASK_ROOT)) == 1
    assert live.task.status()["initialized"] is True


def test_a_broken_sync_object_never_breaks_saving_a_switch(env):
    api, st, _ = env

    class Broken:
        def sync_now(self):
            raise RuntimeError("boom")

    api._task_sync = api._chat_sync = api._record_sync = Broken()
    for key in ("SYNC_TASKS", "SYNC_CHAT", "SYNC_AUDITS_SCANS"):
        assert api.settings_set(key, "true")["ok"] is True
    for key in ("SYNC_TASKS", "SYNC_CHAT", "SYNC_AUDITS_SCANS"):
        api.settings_set(key, "false")
