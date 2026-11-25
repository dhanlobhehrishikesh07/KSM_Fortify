"""Phase 4E: audit and screen-scan online copy, tested against an in-memory cloud. No network, no real Firebase."""
import json
import time
from pathlib import Path

import pytest

from app import cloud_sync as cs
from app import config
from app import record_sync as rs
from app.chat_sync import ChatSync
from app.data_store import TaskStore
from app.gui.web_api import WebAPI
from app.record_sync import RecordSync
from app.screen_analyzer import ScanResult


def wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Clock:
    def __init__(self):
        self.t = 1_000_000

    def __call__(self):
        self.t += 10
        return self.t


class Env:
    def __init__(self, tmp_path, cloud=None, on=True, name="m", background=False):
        self.root = tmp_path / name
        self.root.mkdir(exist_ok=True)
        self.cloud = cloud or cs.MemoryCloud()
        self.on = on
        self.engine = cs.SyncEngine(self.cloud, state_path=self.root / "sync_state.json", clock=Clock())
        self.rec = RecordSync(self.engine, switch=lambda: self.on, background=background)

    def paths(self, root):
        return sorted(p for p in self.cloud.data if p.startswith(root + "/"))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---- switch off ----------------------------------------------------------------------------------------
def test_switch_off_queues_and_sends_nothing(tmp_path):
    e = Env(tmp_path, on=False)
    assert e.rec.record_audit("answer") is False
    assert e.rec.record_scan("q", "answer") is False
    e.rec.start()
    assert e.cloud.calls == [] and e.engine.pending_count() == 0
    assert not (e.root / "sync_state.json").exists()
    assert e.rec.status()["state"] == "off"


# ---- saving ------------------------------------------------------------------------------------------------
def test_an_audit_is_saved_with_question_answer_and_time(env):
    assert env.rec.record_audit("Do these three things today.")
    (path,) = env.paths("ksm/audits")
    rec = env.cloud.data[path]
    assert set(rec) == {"question", "answer", "time"}
    assert rec["answer"] == "Do these three things today." and rec["question"] == rec["question"].strip() != ""
    assert env.paths("ksm/scans") == []


def test_a_scan_is_saved_with_question_answer_monitor_model_and_time(env):
    assert env.rec.record_scan("What error is this?", "It is a 404.", monitor=2, model="gemini-x")
    (path,) = env.paths("ksm/scans")
    assert env.cloud.data[path] == {**env.cloud.data[path], "question": "What error is this?", "answer": "It is a 404.",
                                    "monitor": 2, "model": "gemini-x"}
    assert set(env.cloud.data[path]) == {"question", "answer", "monitor", "model", "time"}
    assert env.paths("ksm/audits") == []


def test_empty_answers_are_ignored_and_long_text_is_cut(env):
    assert env.rec.record_audit("   ") is False and env.rec.record_scan("q", "") is False
    assert env.rec.record_scan("q" * 5000, "a" * 100000, monitor="bad")
    rec = env.cloud.data[env.paths("ksm/scans")[0]]
    assert len(rec["answer"]) == rs.MAX_TEXT and len(rec["question"]) == rs.MAX_QUESTION and rec["monitor"] == 1


def test_two_records_in_the_same_second_do_not_overwrite_each_other(env, monkeypatch):
    monkeypatch.setattr(cs, "time_key", lambda when=None: "2099-01-01_00:00:00")
    for i in range(3):
        env.rec.record_scan("q", f"a{i}")
    paths = env.paths("ksm/scans")
    assert len(paths) == 3
    assert [env.cloud.data[p]["answer"] for p in paths] == ["a0", "a1", "a2"]


def test_saved_keys_never_leave_the_machine_not_even_in_the_outbox_file(tmp_path):
    secret = "gsk_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789secret"
    config.SETTINGS.set("GROQ_API_KEY", secret)
    try:
        e = Env(tmp_path)
        e.cloud.online = False                            # keep it in the outbox so the file is written
        e.rec.record_scan("my key is " + secret, "the key " + secret + " is on screen")
        e.rec.record_audit("audit mentions " + secret)
        assert e.engine.pending_count() == 2
        assert secret not in (e.root / "sync_state.json").read_text(encoding="utf-8")
        e.cloud.online = True
        e.rec.start()
        assert secret not in json.dumps(e.cloud.data)
    finally:
        config.SETTINGS.delete("GROQ_API_KEY")


# ---- offline / separation --------------------------------------------------------------------------------------------
def test_offline_records_wait_and_are_sent_later(env):
    env.cloud.online = False
    env.rec.record_audit("one")
    env.rec.record_scan("q", "two")
    assert env.rec.status()["state"] in ("pending", "offline") and env.rec.status()["pending"] == 2
    assert env.paths("ksm/audits") == [] and env.paths("ksm/scans") == []
    env.cloud.online = True
    env.rec.start()
    assert len(env.paths("ksm/audits")) == 1 and len(env.paths("ksm/scans")) == 1
    assert env.rec.status()["state"] == "synced"


def test_outbox_survives_a_restart(tmp_path):
    e = Env(tmp_path)
    e.cloud.online = False
    e.rec.record_scan("q", "kept")
    e2 = Env(tmp_path, cloud=e.cloud, name="m")           # same folder = same outbox file
    e.cloud.online = True
    e2.rec.start()
    assert [e.cloud.data[p]["answer"] for p in e.paths("ksm/scans")] == ["kept"]


def test_this_sync_flushes_only_audits_and_scans(tmp_path):
    e = Env(tmp_path)
    e.engine.queue_record("ksm/chats/s1/m000000", {"role": "user", "text": "private", "time": "t"})
    e.engine.queue_record("ksm/tasks/t1", {"title": "x", "imp": 1, "urg": 1, "updated_at_ms": 1})
    e.rec.record_audit("a")
    assert e.paths("ksm/chats") == [] and e.paths("ksm/tasks") == []
    assert len(e.paths("ksm/audits")) == 1
    assert e.engine.pending_count("ksm/chats/") == 1 and e.engine.pending_count("ksm/tasks/") == 1


def test_chat_and_task_flush_leave_queued_audits_and_scans_alone(tmp_path):
    e = Env(tmp_path, on=True)
    e.cloud.online = False
    e.rec.record_audit("waiting")
    e.rec.record_scan("q", "waiting too")
    e.cloud.online = True
    chat = ChatSync(e.engine, switch=lambda: True, background=False)
    chat.record("user", "hello")
    assert len(e.paths("ksm/chats")) == 1
    assert e.paths("ksm/audits") == [] and e.paths("ksm/scans") == []
    assert e.engine.pending_count("ksm/audits/") == 1 and e.engine.pending_count("ksm/scans/") == 1


def test_a_cloud_failure_never_raises(tmp_path):
    e = Env(tmp_path)
    e.cloud.online = False
    assert e.rec.record_audit("a") is True                # queued locally, not an error
    assert "internet" in e.rec.status()["last_error"].lower() or e.rec.status()["pending"] == 1


# ---- the WebAPI hooks --------------------------------------------------------------------------------------------------
class FakeAssistant:
    available = False


class FakeOfficer:
    available = True

    def __init__(self, text="Battle plan", fail=False):
        self.text, self.fail, self.matrix = text, fail, None

    def run_audit(self, matrix_json):
        self.matrix = matrix_json
        if self.fail:
            raise RuntimeError("Gemini is down")
        return self.text


class FakeScreen:
    available = True

    def __init__(self, fail=False):
        self.fail = fail

    def problem(self):
        return ""

    def capture_screen(self, monitor):
        return object()

    def analyze(self, img, question, capture_s=0.0):
        if self.fail:
            raise RuntimeError("vision failed")
        return ScanResult(text="I see a login page", preview_b64="PREVIEWBYTES", orig_size=(1920, 1080),
                          sent_size=(960, 540), sent_bytes=2048, model="gemini-vision")


def make_api(tmp_path, rec, officer=None, screen=None):
    return WebAPI(TaskStore(data_file=tmp_path / "t.json"), FakeAssistant(), officer or FakeOfficer(),
                  screen or FakeScreen(), record_sync=rec)


def test_a_finished_audit_is_saved_but_the_task_matrix_is_not(tmp_path):
    e = Env(tmp_path)
    api = make_api(tmp_path, e.rec)
    api.add_task("Secret task title", 3, 3, "2026-10-28", "10:00:00")
    assert "Secret task title" in api._store.as_json()      # the matrix the audit reads really holds it
    api.start_audit()
    assert wait(lambda: len(e.paths("ksm/audits")) == 1)
    blob = json.dumps(e.cloud.data)
    assert "Battle plan" in blob and "Secret task title" not in blob


def test_a_failed_audit_is_not_saved(tmp_path):
    e = Env(tmp_path)
    api = make_api(tmp_path, e.rec, officer=FakeOfficer(fail=True))
    api.start_audit()
    assert wait(lambda: not api._audit_running)
    assert e.cloud.calls == [] and "[ERROR]" in json.loads(api.get_audit_status())["text"]


def test_a_finished_scan_is_saved_without_any_image(tmp_path):
    e = Env(tmp_path)
    api = make_api(tmp_path, e.rec)
    assert api.start_screen_scan(False, "What is on screen?", 1)["ok"]
    assert wait(lambda: len(e.paths("ksm/scans")) == 1)
    rec = e.cloud.data[e.paths("ksm/scans")[0]]
    assert rec["question"] == "What is on screen?" and rec["answer"] == "I see a login page"
    assert rec["model"] == "gemini-vision" and rec["monitor"] == 1
    blob = json.dumps(e.cloud.data) + (e.root / "sync_state.json").read_text(encoding="utf-8")
    assert "PREVIEWBYTES" not in blob and "preview" not in blob.lower() and "image" not in blob.lower()
    assert "2048" not in blob and "1920" not in blob            # no size/byte details of the picture either
    # the screen status page still shows the preview locally, as before
    assert json.loads(api.get_screen_status(0))["preview"] == "PREVIEWBYTES"


def test_a_failed_scan_is_not_saved(tmp_path):
    e = Env(tmp_path)
    api = make_api(tmp_path, e.rec, screen=FakeScreen(fail=True))
    api.start_screen_scan(False, "q", 1)
    assert wait(lambda: not api._scan["running"] and api._scan["scan_id"] == 1)
    assert e.cloud.calls == []


def test_switch_off_audit_and_scan_work_as_before_and_send_nothing(tmp_path):
    e = Env(tmp_path, on=False)
    api = make_api(tmp_path, e.rec)
    api.start_audit()
    api.start_screen_scan(False, "q", 1)
    assert wait(lambda: not api._audit_running and api._scan["scan_id"] == 1 and not api._scan["running"])
    assert json.loads(api.get_audit_status())["text"] == "Battle plan"
    assert json.loads(api.get_screen_status(0))["text"] == "I see a login page"
    assert e.cloud.calls == []


def test_no_record_sync_object_is_fine(tmp_path):
    api = make_api(tmp_path, None)
    api.start_audit()
    assert wait(lambda: not api._audit_running)
    assert json.loads(api.get_audit_status())["text"] == "Battle plan"


def test_a_crash_inside_the_record_sync_never_reaches_audit_or_scan(tmp_path):
    class Boom:
        def record_audit(self, *a, **k):
            raise RuntimeError("boom")

        record_scan = record_audit

    api = make_api(tmp_path, Boom())
    api.start_audit()
    api.start_screen_scan(False, "q", 1)
    assert wait(lambda: not api._audit_running and api._scan["scan_id"] == 1 and not api._scan["running"])
    assert json.loads(api.get_audit_status())["text"] == "Battle plan"
    assert json.loads(api.get_screen_status(0))["error"] == ""


def test_webapi_still_exposes_only_methods(tmp_path):
    api = make_api(tmp_path, Env(tmp_path).rec)
    public = [n for n in dir(api) if not n.startswith("_")]
    assert all(callable(getattr(api, n)) for n in public)


def test_start_record_sync_never_raises(monkeypatch):
    monkeypatch.setattr(rs, "RecordSync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert rs.start_record_sync(None) is None


def test_rules_file_covers_audits_and_scans_and_stays_valid_json():
    rules = json.loads((Path(config.BASE_DIR) / "firebase" / "database.rules.json").read_text(encoding="utf-8"))
    for name in ("audits", "scans"):
        node = rules["rules"]["ksm"][name]["$id"]
        assert set(node) >= {".validate", "question", "answer", "time"}
    assert "image" not in json.dumps(rules["rules"]["ksm"]["scans"]).lower()
