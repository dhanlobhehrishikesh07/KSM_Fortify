"""Phase 4F-1: WebAPI.get_sync_status, sync_now and export_cloud_data, tested against an in-memory cloud."""
import inspect
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cloud_sync as cs
from app import settings_store
from app.chat_sync import ChatSync
from app.data_store import TaskStore
from app.gui.web_api import WebAPI
from app.record_sync import RecordSync
from app.task_sync import TaskSync

KEY = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


class Clock:
    def __init__(self):
        self.t = 5_000_000

    def __call__(self):
        self.t += 10
        return self.t


class Env:
    """Three syncs on one engine and one fake cloud, each with its own switch."""

    def __init__(self, tmp_path, tasks=False, chat=False, records=False):
        self.cloud = cs.MemoryCloud()
        self.engine = cs.SyncEngine(self.cloud, state_path=tmp_path / "sync_state.json", clock=Clock())
        self.sw = {"tasks": tasks, "chat": chat, "records": records}
        self.store = TaskStore(data_file=tmp_path / "t.json")
        self.task = TaskSync(self.store, self.engine, switch=lambda: self.sw["tasks"], background=False,
                             backup_dir=tmp_path / "bk", marker_path=tmp_path / "task_sync.json", retry_seconds=None)
        self.chat = ChatSync(self.engine, switch=lambda: self.sw["chat"], background=False)
        self.rec = RecordSync(self.engine, switch=lambda: self.sw["records"], background=False)
        fake = SimpleNamespace(available=False)
        self.api = WebAPI(self.store, fake, fake, fake, task_sync=self.task, chat_sync=self.chat,
                          record_sync=self.rec)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_all_switches_off_gives_state_off(env):
    st = env.api.get_sync_status()
    assert st["ok"] and st["state"] == "off" and st["label"] == "Off" and st["pending"] == 0
    assert set(st["kinds"]) == {"tasks", "chat", "records"}


def test_no_sync_objects_at_all_is_off_and_never_raises(tmp_path):
    fake = SimpleNamespace(available=False)
    api = WebAPI(TaskStore(data_file=tmp_path / "t.json"), fake, fake, fake)
    assert api.get_sync_status()["state"] == "off"
    assert api.sync_now()["started"] == []
    assert api.export_cloud_data()["ok"] is False


def test_synced_when_on_and_nothing_waits(tmp_path):
    e = Env(tmp_path, chat=True)
    st = e.api.get_sync_status()
    assert st["state"] == "synced" and st["label"] == "Synced" and st["pending"] == 0


def test_offline_with_waiting_changes_gives_pending_n_then_offline(tmp_path):
    e = Env(tmp_path, chat=True, records=True)
    e.cloud.online = False
    e.chat.record("user", "hello")
    e.chat.record("assistant", "hi")
    e.rec.record_audit("answer")
    st = e.api.get_sync_status()
    assert st["pending"] == 3
    assert st["state"] in ("pending", "offline")
    assert st["kinds"]["chat"]["pending"] == 2 and st["kinds"]["records"]["pending"] == 1
    assert st["state"] == "offline" and st["last_error"]          # a failed send was seen
    assert st["label"] == "Offline"


def test_pending_label_counts_before_a_failure_is_seen(tmp_path):
    e = Env(tmp_path, chat=True)
    e.engine.enabled = True
    e.engine.queue_record("ksm/chats/s1/m000000", {"role": "user", "text": "x", "time": "t"})
    st = e.api.get_sync_status()
    assert st["state"] == "pending" and st["label"] == "Pending 1"


def test_a_switched_off_kind_is_not_counted(tmp_path):
    e = Env(tmp_path, chat=True)
    e.engine.enabled = True
    e.engine.queue_record("ksm/audits/a1", {"answer": "x"})       # records switch is off
    assert e.api.get_sync_status()["pending"] == 0


def test_sync_now_with_fake_cloud_empties_the_outbox(tmp_path):
    e = Env(tmp_path, chat=True, records=True)
    e.cloud.online = False
    e.chat.record("user", "hello")
    e.rec.record_audit("answer")
    assert e.engine.pending_count() == 2
    e.cloud.online = True
    res = e.api.sync_now()
    assert sorted(res["started"]) == ["chat", "records"]
    assert e.engine.pending_count() == 0
    assert res["state"] == "synced" and res["last_sync"]
    assert any(p.startswith("ksm/chats/") for p in e.cloud.data)


def test_sync_now_with_all_switches_off_sends_nothing(env):
    env.engine.enabled = True
    env.engine.queue_record("ksm/chats/s1/m000000", {"role": "user", "text": "x", "time": "t"})
    res = env.api.sync_now()
    assert res["started"] == [] and "off" in res["message"]
    assert env.cloud.calls == [] and env.engine.pending_count() == 1


def test_sync_now_only_sends_the_kinds_that_are_on(tmp_path):
    e = Env(tmp_path, chat=True)
    e.engine.enabled = True
    e.engine.queue_record("ksm/chats/s1/m000000", {"role": "user", "text": "x", "time": "t"})
    e.engine.queue_record("ksm/audits/a1", {"answer": "x"})
    e.api.sync_now()
    assert [p for p in e.cloud.data if p.startswith("ksm/audits/")] == []
    assert e.engine.pending_count("ksm/audits/") == 1


def test_sync_now_when_cloud_down_does_not_raise(tmp_path):
    e = Env(tmp_path, chat=True)
    e.cloud.online = False
    e.chat.record("user", "hello")
    res = e.api.sync_now()
    assert res["state"] == "offline" and res["pending"] == 1


def test_a_failing_status_never_raises(env):
    class Broken:
        engine = None
        def status(self):
            raise RuntimeError("boom " + KEY)
        def sync_now(self):
            raise RuntimeError("boom")
    env.api._chat_sync = Broken()
    st = env.api.get_sync_status()
    assert "ok" in st and KEY not in json.dumps(st)
    env.api.sync_now()


def test_status_never_shows_a_saved_key(tmp_path, monkeypatch):
    e = Env(tmp_path, chat=True)
    monkeypatch.setattr(settings_store, "redact_text", lambda t, store=None: str(t).replace(KEY, "***"))
    e.chat._error = "failed with " + KEY
    e.engine.enabled = True
    e.engine.queue_record("ksm/chats/s1/m000000", {"role": "user", "text": "x", "time": "t"})
    st = e.api.get_sync_status()
    assert st["ok"] and KEY not in json.dumps(st) and "***" in json.dumps(st)


# ---- export ------------------------------------------------------------------------------------------------
def seed(e):
    e.cloud.data.update({
        "ksm/tasks/1": {"id": 1, "title": "Buy milk"},
        "ksm/chats/2026-10-27_10:00:00/m000000": {"role": "user", "text": "hello", "time": "t"},
        "ksm/chats/2026-10-27_10:00:00/m000001": {"role": "assistant", "text": "hi", "time": "t"},
        "ksm/audits/a1": {"question": "q", "answer": "plan"},
        "ksm/scans/s1": {"question": "q", "answer": "screen", "monitor": 1},
    })


def test_export_writes_a_file_in_the_exports_folder(tmp_path):
    e = Env(tmp_path)          # all switches off: export is download only and still works
    seed(e)
    res = e.api.export_cloud_data()
    assert res["ok"], res
    path = Path(res["path"])
    assert path.exists() and path.parent == settings_store.config_dir() / "exports"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tasks"]["1"]["title"] == "Buy milk"
    assert len(data["chats"]["2026-10-27_10:00:00"]) == 2
    assert data["audits"]["a1"]["answer"] == "plan" and data["scans"]["s1"]["monitor"] == 1
    assert res["counts"] == {"tasks": 1, "audits": 1, "scans": 1, "chats": 1}
    assert not list(path.parent.glob("*.tmp"))


def test_export_has_no_key_text_even_if_one_is_in_the_cloud(tmp_path, monkeypatch):
    e = Env(tmp_path)
    seed(e)
    e.cloud.data["ksm/audits/a2"] = {"answer": "my key is " + KEY}
    monkeypatch.setattr(settings_store, "redact_text", lambda t, store=None: str(t).replace(KEY, "***"))
    monkeypatch.setattr(cs.settings_store, "redact_text", settings_store.redact_text)
    res = e.api.export_cloud_data()
    assert res["ok"] and KEY not in Path(res["path"]).read_text(encoding="utf-8")


def test_export_when_cloud_down_gives_a_message_and_no_file(tmp_path):
    e = Env(tmp_path)
    seed(e)
    exports = settings_store.config_dir() / "exports"
    before = set(exports.iterdir()) if exports.exists() else set()
    e.cloud.online = False
    res = e.api.export_cloud_data()
    assert res["ok"] is False and res["error"]
    after = set(exports.iterdir()) if exports.exists() else set()
    assert after == before


def test_two_exports_in_one_second_do_not_overwrite(tmp_path):
    e = Env(tmp_path)
    seed(e)
    a, b = e.api.export_cloud_data(), e.api.export_cloud_data()
    assert a["ok"] and b["ok"] and a["path"] != b["path"]


def test_export_does_not_touch_the_outbox_or_send(tmp_path):
    e = Env(tmp_path, chat=True)
    seed(e)
    before = dict(e.cloud.data)
    e.api.export_cloud_data()
    assert e.cloud.data == before
    assert not [c for c in e.cloud.calls if c[0] in ("set", "delete")]


@pytest.mark.skipif(os.name == "nt", reason="file modes differ on Windows")
def test_export_file_is_owner_only(tmp_path):
    e = Env(tmp_path)
    seed(e)
    assert (Path(e.api.export_cloud_data()["path"]).stat().st_mode & 0o077) == 0


def test_only_methods_are_public_on_webapi(env):
    public = [n for n in dir(env.api) if not n.startswith("_")]
    for n in public:
        assert inspect.ismethod(getattr(env.api, n)), n
    for n in ("get_sync_status", "sync_now", "export_cloud_data"):
        assert n in public
