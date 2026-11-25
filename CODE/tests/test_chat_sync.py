"""Phase 4D: chat history online copy, tested against an in-memory cloud. No network, no real Firebase."""
import json
import threading
import time

import pytest

from app import chat_sync as chs
from app import cloud_sync as cs
from app import config
from app.chat_sync import ChatSync
from app.data_store import TaskStore
from app.gui.web_api import WebAPI
from app.task_sync import TaskSync


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
    def __init__(self, tmp_path, cloud=None, on=True, name="m", background=False, load_last=20):
        self.root = tmp_path / name
        self.root.mkdir(exist_ok=True)
        self.cloud = cloud or cs.MemoryCloud()
        self.on = on
        self.engine = cs.SyncEngine(self.cloud, state_path=self.root / "sync_state.json", clock=Clock())
        self.chat = ChatSync(self.engine, switch=lambda: self.on, background=background, load_last=load_last)

    def chat_paths(self):
        return sorted(p for p in self.cloud.data if p.startswith("ksm/chats/"))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def seed(cloud, session, msgs, start=0):
    """Puts messages straight into a fake cloud: msgs = [(role, text)]."""
    for i, (role, text) in enumerate(msgs, start):
        cloud.data[f"ksm/chats/{session}/m{i:06d}"] = {"role": role, "text": text, "time": "2026-10-20 10:00:00"}


# ---- switch off -------------------------------------------------------------------------------------------

def test_switch_off_queues_sends_and_loads_nothing(tmp_path):
    e = Env(tmp_path, on=False)
    assert e.chat.record("user", "hello") is False
    assert e.chat.record_clear() is False
    assert e.chat.load_recent() == ([], "")
    e.chat.start()
    assert e.cloud.calls == [] and e.engine.pending_count() == 0
    assert not (e.root / "sync_state.json").exists()
    assert e.chat.status()["state"] == "off"


# ---- saving ---------------------------------------------------------------------------------------------------

def test_messages_go_to_one_session_in_order(env):
    assert env.chat.record("user", "first") and env.chat.record("assistant", "second") and env.chat.record("user", "third")
    paths = env.chat_paths()
    assert len(paths) == 3 and len({p.rsplit("/", 1)[0] for p in paths}) == 1
    assert [env.cloud.data[p]["text"] for p in paths] == ["first", "second", "third"]
    assert [env.cloud.data[p]["role"] for p in paths] == ["user", "assistant", "user"]
    assert all(set(env.cloud.data[p]) == {"role", "text", "time"} for p in paths)
    assert all(cs.valid_path(p) for p in paths)
    assert env.chat.status()["state"] == "synced"


def test_saved_keys_never_leave_the_machine_not_even_in_the_outbox_file(env):
    secret = "gsk_" + "Qw1Er2Ty3Ui4Op5As6Df7"
    config.SETTINGS.set("GROQ_API_KEY", secret)
    try:
        env.cloud.online = False
        assert env.chat.record("user", f"my key is {secret}")
        assert secret not in (env.root / "sync_state.json").read_text(encoding="utf-8")
        env.cloud.online = True
        env.chat.start()
    finally:
        config.SETTINGS.delete("GROQ_API_KEY")
    assert secret not in json.dumps(env.cloud.data) and "***" in env.cloud.data[env.chat_paths()[0]]["text"]


def test_bad_roles_and_empty_text_are_ignored_and_long_text_is_cut(env):
    assert env.chat.record("system", "x") is False
    assert env.chat.record("user", "   ") is False
    assert env.chat.record("user", "a" * (chs.MAX_TEXT + 500))
    assert len(env.cloud.data[env.chat_paths()[0]]["text"]) == chs.MAX_TEXT


def test_offline_messages_wait_and_are_sent_later_in_order(env):
    env.cloud.online = False
    env.chat.record("user", "one")
    env.chat.record("assistant", "two")
    assert env.chat.status()["state"] == "offline" and env.chat.status()["pending"] == 2
    assert env.chat_paths() == []
    env.cloud.online = True
    env.chat.start()
    assert [env.cloud.data[p]["text"] for p in env.chat_paths()] == ["one", "two"]
    assert env.chat.status()["state"] == "synced"


def test_outbox_survives_a_restart(tmp_path):
    e = Env(tmp_path)
    e.cloud.online = False
    e.chat.record("user", "kept across restart")
    e2 = Env(tmp_path, cloud=e.cloud)           # same state file, new objects
    e.cloud.online = True
    e2.chat.start()
    assert [e.cloud.data[p]["text"] for p in e.chat_paths()] == ["kept across restart"]


def test_two_sessions_in_the_same_second_stay_distinct_and_ordered(env):
    env.chat.record("user", "a")
    env.chat.record_clear()
    env.chat.record("user", "b")
    env.chat.record_clear()
    env.chat.record("user", "c")
    sessions = sorted({p.split("/")[2] for p in env.chat_paths()})
    assert len(sessions) == 3 and sessions == sorted(sessions)


# ---- each switch controls only its own data --------------------------------------------------------------------

def test_chat_flush_does_not_send_tasks_and_task_flush_does_not_send_chat(tmp_path):
    e = Env(tmp_path)
    tasks = [{"id": 1, "title": "T", "imp": 2, "urg": 2, "created_at": "", "expected_at": "", "history": []}]
    e.engine.enabled = True
    e.engine.record_tasks(tasks)
    e.cloud.online = False
    e.chat.record("user", "waiting chat")
    e.cloud.online = True
    e.engine.flush("ksm/tasks/")
    assert "ksm/tasks/1" in e.cloud.data and e.chat_paths() == []
    e.engine.record_tasks([{**tasks[0], "title": "T2"}])
    e.chat.start()
    assert len(e.chat_paths()) == 1 and e.cloud.data["ksm/tasks/1"]["title"] == "T"   # task change still waiting
    assert e.engine.pending_count("ksm/tasks/") == 1 and e.engine.pending_count("ksm/chats/") == 0


def test_task_sync_leaves_queued_chat_alone_when_chat_is_switched_off(tmp_path):
    e = Env(tmp_path)
    e.cloud.online = False
    e.chat.record("user", "queued while on")
    e.cloud.online = True
    e.on = False                                  # chat switch turned off: nothing more may be sent
    store = TaskStore(data_file=e.root / "m.json")
    ts = TaskSync(store, e.engine, switch=lambda: True, backup_dir=e.root / "b", marker_path=e.root / "t.json",
                  background=False, retry_seconds=None)
    ts.start()
    store.add_task("a task", 2, 2, "2030-01-01 10:00:00")
    assert any(p.startswith("ksm/tasks/") for p in e.cloud.data) and e.chat_paths() == []


# ---- loading ------------------------------------------------------------------------------------------------------------

def test_load_returns_the_last_n_messages_across_sessions_oldest_first(tmp_path):
    e = Env(tmp_path, load_last=5)
    seed(e.cloud, "2026-10-20_10:00:00", [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")])
    seed(e.cloud, "2026-10-21_10:00:00", [("user", "q3"), ("assistant", "a3"), ("user", "q4")])
    lines, err = e.chat.load_recent()
    assert err == ""
    assert lines == ["User: q2", "AI: a2", "User: q3", "AI: a3", "User: q4"]


def test_load_reads_only_the_newest_sessions(tmp_path):
    e = Env(tmp_path, load_last=2)
    for d in range(1, 6):
        seed(e.cloud, f"2026-10-0{d}_10:00:00", [("user", f"q{d}"), ("assistant", f"a{d}")])
    e.cloud.calls.clear()
    lines, _ = e.chat.load_recent()
    assert lines == ["User: q5", "AI: a5"]
    gets = [c for c in e.cloud.calls if c[0] == "get"]
    assert len(gets) == 1                                   # older sessions were never downloaded


def test_a_cleared_chat_is_not_loaded_back(tmp_path):
    e = Env(tmp_path)
    seed(e.cloud, "2026-10-20_10:00:00", [("user", "old secret plan"), ("assistant", "old answer")])
    seed(e.cloud, "2026-10-21_10:00:00", [("marker", "context cleared"), ("user", "after clear"), ("assistant", "ok")])
    lines, _ = e.chat.load_recent()
    assert lines == ["User: after clear", "AI: ok"]
    seed(e.cloud, "2026-10-22_10:00:00", [("marker", "context cleared")])
    assert e.chat.load_recent() == ([], "")


def test_the_running_session_is_not_loaded_back(env):
    env.chat.record("user", "this run")
    seed(env.cloud, "2026-10-01_10:00:00", [("user", "earlier")])
    assert env.chat.load_recent()[0] == ["User: earlier"]


def test_load_ignores_malformed_records_and_removes_keys(tmp_path):
    e = Env(tmp_path)
    secret = "gsk_" + "Lm1Kn2Jo3Ip4Hq5Gr6"
    config.SETTINGS.set("GROQ_API_KEY", secret)
    try:
        e.cloud.data["ksm/chats/2026-10-01_10:00:00/m000000"] = {"role": "user", "text": f"has {secret}", "time": "t"}
        e.cloud.data["ksm/chats/2026-10-01_10:00:00/m000001"] = {"role": "alien", "text": "x"}
        e.cloud.data["ksm/chats/2026-10-01_10:00:00/m000002"] = {"role": "assistant", "text": 42}
        e.cloud.data["ksm/chats/2026-10-01_10:00:00/m000003"] = "junk"
        lines, err = e.chat.load_recent()
    finally:
        config.SETTINGS.delete("GROQ_API_KEY")
    assert err == "" and lines == ["User: has ***"]


def test_load_when_the_cloud_is_down_returns_nothing_and_does_not_raise(tmp_path):
    e = Env(tmp_path)
    e.cloud.online = False
    lines, err = e.chat.load_recent()
    assert lines == [] and "internet" in err.lower()


# ---- the chat window (WebAPI) -------------------------------------------------------------------------------------------------

class FakeAssistant:
    available = True

    def __init__(self):
        self.seen = []

    def chat(self, history, matrix_json):
        self.seen.append(list(history))
        return "reply to " + history[-1].split(": ", 1)[1]

    @staticmethod
    def reply_contains_trigger(reply):
        return False


class FakeScreen:
    available = False

    def problem(self):
        return "n/a"


class FakeOfficer:
    available = False


def make_api(tmp_path, chat=None, assistant=None):
    a = assistant or FakeAssistant()
    return WebAPI(TaskStore(data_file=tmp_path / "t.json"), a, FakeOfficer(), FakeScreen(), chat_sync=chat), a


def idle(api):
    return wait(lambda: not api._chat_busy)


def test_chat_messages_and_replies_are_saved_when_on(tmp_path):
    e = Env(tmp_path)
    api, _ = make_api(tmp_path, e.chat)
    api.send_chat("hello there")
    assert idle(api) and wait(lambda: len(e.chat_paths()) == 2)
    got = [(e.cloud.data[p]["role"], e.cloud.data[p]["text"]) for p in e.chat_paths()]
    assert got == [("user", "hello there"), ("assistant", "reply to hello there")]


def test_mobile_messages_are_saved_too(tmp_path):
    e = Env(tmp_path)
    api, _ = make_api(tmp_path, e.chat)
    api.receive_external_message("from the phone")
    assert idle(api) and wait(lambda: len(e.chat_paths()) == 2)
    assert e.cloud.data[e.chat_paths()[0]]["text"] == "from the phone"


def test_switch_off_chat_works_as_before_and_sends_nothing(tmp_path):
    e = Env(tmp_path, on=False)
    api, a = make_api(tmp_path, e.chat)
    api.send_chat("private")
    assert idle(api)
    assert e.cloud.calls == [] and a.seen[-1] == ["User: private"]
    assert any(m["text"].startswith("Hi") for m in api._chat_messages)


def test_no_chat_sync_object_is_fine(tmp_path):
    api, a = make_api(tmp_path, None)
    api.send_chat("hi")
    assert idle(api) and a.seen[-1] == ["User: hi"]
    api.clear_chat()


def test_history_from_earlier_sessions_reaches_the_ai(tmp_path):
    e = Env(tmp_path)
    seed(e.cloud, "2026-10-01_10:00:00", [("user", "my exam is on Friday"), ("assistant", "noted")])
    api, a = make_api(tmp_path, e.chat)
    assert wait(lambda: any("Loaded 2 earlier" in m["text"] for m in api._chat_messages))
    api.send_chat("when is it?")
    assert idle(api)
    assert a.seen[-1] == ["User: my exam is on Friday", "AI: noted", "User: when is it?"]


def test_an_earlier_session_is_not_duplicated_after_a_restart(tmp_path, monkeypatch):
    e = Env(tmp_path)
    api, _ = make_api(tmp_path, e.chat)
    api.send_chat("one")
    assert idle(api) and wait(lambda: len(e.chat_paths()) == 2)
    monkeypatch.setattr(cs, "time_key", lambda when=None: "2099-01-01_00:00:00")   # a real restart is later
    e2 = Env(tmp_path, cloud=e.cloud, name="again")            # app restarted: new ChatSync, new session
    api2, a2 = make_api(tmp_path, e2.chat)
    assert wait(lambda: any("Loaded 2 earlier" in m["text"] for m in api2._chat_messages))
    api2.send_chat("two")
    assert idle(api2)
    assert a2.seen[-1] == ["User: one", "AI: reply to one", "User: two"]


def test_clear_context_starts_a_new_session_and_the_next_start_forgets_the_old_one(tmp_path, monkeypatch):
    e = Env(tmp_path)
    api, _ = make_api(tmp_path, e.chat)
    api.send_chat("before clear")
    assert idle(api)
    api.clear_chat()
    api.send_chat("after clear")
    assert idle(api) and wait(lambda: len(e.chat_paths()) == 5)           # 2 + marker + 2
    sessions = sorted({p.split("/")[2] for p in e.chat_paths()})
    assert len(sessions) == 2
    marker = e.cloud.data[f"ksm/chats/{sessions[1]}/m000000"]
    assert marker["role"] == "marker"
    monkeypatch.setattr(cs, "time_key", lambda when=None: "2099-01-01_00:00:00")   # a real restart is later
    e2 = Env(tmp_path, cloud=e.cloud, name="again")
    api2, a2 = make_api(tmp_path, e2.chat)
    assert wait(lambda: any("Loaded 2 earlier" in m["text"] for m in api2._chat_messages))
    api2.send_chat("next")
    assert idle(api2)
    assert a2.seen[-1] == ["User: after clear", "AI: reply to after clear", "User: next"]


def test_clearing_while_the_history_is_loading_discards_the_load(tmp_path):
    e = Env(tmp_path)
    seed(e.cloud, "2026-10-01_10:00:00", [("user", "old"), ("assistant", "older")])
    gate = threading.Event()
    real = e.chat.load_recent

    def slow():
        gate.wait(5)
        return real()
    e.chat.load_recent = slow
    api, a = make_api(tmp_path, e.chat)
    api.clear_chat()                                  # before the load finishes
    gate.set()
    time.sleep(0.2)
    api.send_chat("fresh")
    assert idle(api)
    assert a.seen[-1] == ["User: fresh"]


def test_history_load_failure_never_breaks_the_chat(tmp_path):
    e = Env(tmp_path)
    e.cloud.online = False
    api, a = make_api(tmp_path, e.chat)
    api.send_chat("works offline")
    assert idle(api) and a.seen[-1] == ["User: works offline"]


def test_a_crash_inside_the_chat_sync_never_reaches_the_chat(tmp_path):
    class Broken:
        def record(self, *a):
            raise RuntimeError("boom")

        def record_clear(self):
            raise RuntimeError("boom")

        def load_recent(self):
            raise RuntimeError("boom")
    api, a = make_api(tmp_path, Broken())
    api.send_chat("still fine")
    assert idle(api) and a.seen[-1] == ["User: still fine"]
    assert api.clear_chat() == {"ok": True}


# ---- the cap -------------------------------------------------------------------------------------------------------------------

def test_session_history_is_capped_and_the_newest_lines_stay(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHAT_HISTORY_LIMIT", 6)
    api, a = make_api(tmp_path, None)
    for i in range(10):
        api.send_chat(f"msg {i}")
        assert idle(api)
    assert len(api._session_history) == 6
    assert api._session_history[-2:] == ["User: msg 9", "AI: reply to msg 9"]
    assert max(len(h) for h in a.seen) <= 6


def test_loaded_history_is_capped_too(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHAT_HISTORY_LIMIT", 4)
    e = Env(tmp_path)
    seed(e.cloud, "2026-10-01_10:00:00", [("user", f"q{i}") for i in range(10)])
    api, _ = make_api(tmp_path, e.chat)
    assert wait(lambda: any("Loaded" in m["text"] for m in api._chat_messages))
    assert api._session_history == ["User: q6", "User: q7", "User: q8", "User: q9"]


def test_webapi_still_exposes_only_methods(tmp_path):
    e = Env(tmp_path)
    api, _ = make_api(tmp_path, e.chat)
    public = [n for n in dir(api) if not n.startswith("_") and not callable(getattr(api, n))]
    assert public == []


# ---- start-up helpers -------------------------------------------------------------------------------------------------------------

def test_start_chat_sync_never_raises(monkeypatch):
    monkeypatch.setattr(chs, "ChatSync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert chs.start_chat_sync(None) is None


def test_start_online_copy_shares_one_engine_and_never_raises(tmp_path, monkeypatch):
    from app.gui import main_window
    store = TaskStore(data_file=tmp_path / "m.json")
    ts, chat, rec = main_window.start_online_copy(store)
    assert ts is not None and chat is not None and rec is not None and ts.engine is chat.engine is rec.engine
    monkeypatch.setattr(cs, "engine_from_config", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert main_window.start_online_copy(store) == (None, None, None)


def test_rules_file_covers_chats_and_stays_valid_json():
    from pathlib import Path
    rules = json.loads((Path(config.BASE_DIR) / "firebase" / "database.rules.json").read_text(encoding="utf-8"))
    msg = rules["rules"]["ksm"]["chats"]["$session"]["$msg"]
    assert set(msg) >= {".validate", "role", "text", "time"}
    assert "marker" in msg["role"][".validate"]
