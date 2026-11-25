"""Phase 2E: regression and contract checks across the whole backend.
Real classes, fake network clients, no API keys, no display needed."""
import importlib
import json
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import config
from app.data_store import TaskStore
from app.gui.web_api import WebAPI


@pytest.fixture(autouse=True)
def no_live_keys(monkeypatch):
    """Whatever is in the owner's .env, tests must never call a real API."""
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "ENABLE_FIREBASE", False)


def _real_api(tmp_path):
    from app.ai_assistant import AIAssistant
    from app.screen_analyzer import ScreenAnalyzer
    from app.strategy_officer import StrategyOfficer

    return WebAPI(TaskStore(data_file=tmp_path / "m.json"), AIAssistant(), StrategyOfficer(), ScreenAnalyzer())


# ---------------------------------------------------------------- imports
@pytest.mark.parametrize(
    "module",
    ["app.config", "app.logger", "app.data_store", "app.ai_assistant", "app.strategy_officer",
     "app.screen_analyzer", "app.firebase_bridge", "app.gui.web_api"],
)
def test_backend_modules_import_without_keys(module):
    importlib.import_module(module)


def test_main_window_imports():
    pytest.importorskip("webview")
    mod = importlib.import_module("app.gui.main_window")
    assert callable(mod.run)


# ---------------------------------------------------------------- the JS-facing API contract
def test_snapshot_shape_and_task_lifecycle(tmp_path):
    api = _real_api(tmp_path)
    snap = json.loads(api.get_snapshot())
    for key in ("now", "tasks", "groq_available", "gemini_available", "screen_available",
                "firebase_enabled", "assistant_name", "user_name", "data_warning"):
        assert key in snap
    assert snap["tasks"] == [] and snap["groq_available"] is False

    assert api.add_task("Write report", 2, 1, "2030-05-01", "10:00:00") == {"ok": True}
    [task] = json.loads(api.get_snapshot())["tasks"]
    assert (task["title"], task["imp"], task["urg"]) == ("Write report", 2, 1)

    assert api.migrate_task(task["id"], 3, 1) == {"ok": True}
    assert api.delete_task(task["id"]) == {"ok": True}
    assert json.loads(api.get_snapshot())["tasks"] == []


def test_add_task_rejects_bad_input_without_raising(tmp_path):
    api = _real_api(tmp_path)
    assert api.add_task("x", 1, 1, "not-a-date", "10:00:00")["ok"] is False
    assert api.add_task("x", "abc", 1, "2030-05-01", "10:00:00")["ok"] is False
    assert api.apply_warp("a", "b")["ok"] is False


def test_other_polling_endpoints_return_valid_json(tmp_path):
    api = _real_api(tmp_path)
    assert set(json.loads(api.get_chat_updates(0))) == {"messages", "count", "busy"}
    assert set(json.loads(api.get_audit_status())) == {"running", "text", "available"}
    assert "entries" in json.loads(api.get_bridge_log(0))
    assert "entries" in json.loads(api.get_debug_log(0))
    scr = json.loads(api.get_screen_status(0))
    assert scr["available"] is False and scr["problem"]


def test_unavailable_services_refuse_politely(tmp_path):
    api = _real_api(tmp_path)
    assert api.send_chat("hello") == {"ok": False}
    assert api.start_audit() == {"ok": False}
    r = api.start_screen_scan(False, "", 1)
    assert r["ok"] is False and r["error"]


# ---------------------------------------------------------------- task rules
def test_migration_rules(tmp_path):
    s = TaskStore(data_file=tmp_path / "m.json")
    t = s.add_task("a", 2, 2, "2030-01-01 00:00:00")
    assert s.migrate_task(t["id"], 1, 2) is None        # cannot go down in importance
    assert s.migrate_task(t["id"], 2, 1) is None        # cannot go down in urgency
    s2 = s.add_task("b", 1, 1, "2030-01-01 00:00:00")
    assert s.migrate_task(s2["id"], 3, 1) is None       # cannot skip an importance step
    assert s.migrate_task(s2["id"], 2, 3)["urg"] == 3   # one importance step, urgency up is fine
    assert s.migrate_task(999, 3, 3) is None


def test_auto_escalation_levels_and_history(tmp_path):
    s = TaskStore(data_file=tmp_path / "m.json")
    now = s.now()
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")
    soon = s.add_task("soon", 2, 1, fmt(now + timedelta(hours=3)))
    week = s.add_task("week", 2, 1, fmt(now + timedelta(days=5)))
    far = s.add_task("far", 2, 1, fmt(now + timedelta(days=30)))
    assert s.run_auto_escalation() is True
    by = {t["title"]: t for t in s.tasks}
    assert by["soon"]["urg"] == 3 and by["week"]["urg"] == 2 and by["far"]["urg"] == 1
    assert "[AUTO]" in by["soon"]["history"][-1]
    assert s.run_auto_escalation() is False             # nothing more to do the second time


def test_time_warp_triggers_escalation(tmp_path):
    s = TaskStore(data_file=tmp_path / "m.json")
    s.add_task("later", 3, 1, (s.now() + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"))
    assert s.run_auto_escalation() is False
    s.set_time_warp(days=9.5)
    assert s.run_auto_escalation() is True
    assert s.tasks[0]["urg"] == 3


# ---------------------------------------------------------------- strategy officer
def test_strategy_officer_sends_matrix_and_returns_text():
    from app.strategy_officer import StrategyOfficer, StrategyOfficerError

    o = StrategyOfficer()
    with pytest.raises(StrategyOfficerError):
        o.run_audit("[]")
    seen = {}

    def fake_generate(model, contents, config):
        seen["contents"] = contents
        return SimpleNamespace(text="AUDIT OK")

    o._client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
    pytest.importorskip("google.genai")
    assert o.run_audit('[{"id": 101}]') == "AUDIT OK"
    assert '"id": 101' in seen["contents"] and "Danger Zones" in seen["contents"]


# ---------------------------------------------------------------- screen analyzer
def _screen_analyzer(reply=None, errors=()):
    pytest.importorskip("google.genai")
    from app.screen_analyzer import ScreenAnalyzer

    a = ScreenAnalyzer()
    calls = {"n": 0}
    queue = list(errors)

    def fake_generate(model, contents, config):
        calls["n"] += 1
        if queue:
            raise queue.pop(0)
        return SimpleNamespace(text=reply)

    a._client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
    return a, calls


def test_prepare_image_shrinks_and_encodes(monkeypatch):
    from PIL import Image

    monkeypatch.setattr(config, "SCREEN_MAX_WIDTH", 800)
    a, _ = _screen_analyzer("x")
    data, preview_b64, size = a.prepare_image(Image.new("RGB", (2400, 1200), "white"))
    assert size == (800, 400)
    assert data[:2] == b"\xff\xd8" and preview_b64


def test_analyze_returns_result_and_appends_user_question():
    from PIL import Image

    a, _ = _screen_analyzer("[1] Terminal ...")
    r = a.analyze(Image.new("RGB", (100, 50), "black"), "what is open?")
    assert r.text.startswith("[1] Terminal") and r.sent_size == (100, 50)


def test_analyze_retries_once_on_transient_error(monkeypatch):
    from PIL import Image
    from app import screen_analyzer

    monkeypatch.setattr(screen_analyzer.time, "sleep", lambda s: None)
    a, calls = _screen_analyzer("fine", errors=[RuntimeError("503 UNAVAILABLE")])
    assert a.analyze(Image.new("RGB", (10, 10))).text == "fine"
    assert calls["n"] == 2


def test_analyze_empty_answer_and_error_messages():
    from PIL import Image
    from app.screen_analyzer import ScanResult, ScreenScanError

    a, _ = _screen_analyzer("   ")
    with pytest.raises(ScreenScanError, match="empty answer"):
        a.analyze(Image.new("RGB", (10, 10)))
    f = type(a)._friendly_error
    assert "rate limit" in f(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert "not found" in f(RuntimeError("404 NOT_FOUND"))
    assert "rejected the API key" in f(RuntimeError("403 PERMISSION_DENIED"))


def test_capture_failure_is_a_clear_error_not_a_crash(monkeypatch):
    from app import screen_analyzer
    from app.screen_analyzer import ScreenScanError

    a, _ = _screen_analyzer("x")
    monkeypatch.setattr(screen_analyzer, "mss", None)
    monkeypatch.setattr(screen_analyzer, "ImageGrab", SimpleNamespace(grab=lambda **k: (_ for _ in ()).throw(OSError("no display"))))
    with pytest.raises(ScreenScanError, match="Could not capture"):
        a.capture_screen()


# ---------------------------------------------------------------- logger
def test_log_ring_buffer_cursor_and_clear():
    from app.logger import get_buffer, get_logger, setup_logging

    setup_logging()
    log = get_logger("tests")
    _, start = get_buffer().since(0)
    log.warning("marker-one")
    log.error("marker-two")
    entries, total = get_buffer().since(start)
    assert [e["level"] for e in entries][-2:] == ["WARNING", "ERROR"]
    assert total >= start + 2
    get_buffer().clear()
    entries, total2 = get_buffer().since(total)
    assert entries == [] and total2 == total
