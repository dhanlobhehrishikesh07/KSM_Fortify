"""Phase 4B: the online-copy engine, tested against an in-memory cloud. No network, no real Firebase."""
import json
import threading

import pytest

from app import cloud_sync as cs
from app import config, firebase_app


def task(i, title="Task", imp=2, urg=2, history=None):
    return {"id": i, "title": f"{title} {i}", "imp": imp, "urg": urg, "created_at": "2026-10-27 10:00:00",
            "expected_at": "2026-11-01 10:00:00", "history": history or [f"[2026-10-27 10:00:00] [MANUAL] Created."]}


class Clock:
    def __init__(self, start=1_000_000):
        self.t = start

    def __call__(self):
        self.t += 10
        return self.t


@pytest.fixture
def parts(tmp_path):
    cloud = cs.MemoryCloud()
    clock = Clock()
    state = tmp_path / "sync_state.json"
    eng = cs.SyncEngine(cloud, state_path=state, clock=clock)
    return eng, cloud, state, clock


def cloud_ids(cloud):
    return sorted(p.rsplit("/", 1)[1] for p in cloud.data if p.startswith("ksm/tasks/"))


# ---- upload ------------------------------------------------------------------------------

def test_first_upload_sends_every_task_and_a_meta_marker(parts):
    eng, cloud, _, _ = parts
    assert eng.record_tasks([task(1), task(2)]) == 2
    st = eng.flush()
    assert st["state"] == "synced" and st["pending"] == 0
    assert cloud_ids(cloud) == ["1", "2"]
    rec = cloud.data["ksm/tasks/1"]
    assert rec["title"] == "Task 1" and rec["deleted"] is False and rec["updated_at_ms"] > 0
    assert cloud.data["ksm/meta"]["schema"] == cs.SCHEMA_VERSION


def test_unchanged_tasks_queue_nothing_and_edits_send_only_that_task(parts):
    eng, cloud, _, _ = parts
    tasks = [task(1), task(2)]
    eng.record_tasks(tasks)
    eng.flush()
    cloud.calls.clear()
    assert eng.record_tasks(tasks) == 0
    eng.flush()
    assert cloud.calls == []
    tasks[1] = task(2, imp=3)
    assert eng.record_tasks(tasks) == 1
    eng.flush()
    assert [c for c in cloud.calls if c[0] == "set" and c[1].startswith("ksm/tasks/")] == [("set", "ksm/tasks/2")]


def test_a_malformed_local_task_is_skipped_not_fatal(parts):
    eng, cloud, _, _ = parts
    assert eng.record_tasks([{"id": 1}, task(2)]) == 1
    eng.flush()
    assert cloud_ids(cloud) == ["2"]


# ---- offline -------------------------------------------------------------------------------

def test_offline_changes_wait_then_arrive_and_nothing_raises(parts):
    eng, cloud, _, _ = parts
    cloud.online = False
    eng.record_tasks([task(1)])
    st = eng.flush()                                   # must not raise
    assert st["state"] == "offline" and st["pending"] == 1 and st["last_error"]
    cloud.online = True
    st = eng.flush()
    assert st["state"] == "synced" and st["pending"] == 0 and st["last_error"] == ""
    assert cloud_ids(cloud) == ["1"]


def test_outbox_survives_a_restart(parts, tmp_path):
    eng, cloud, state, clock = parts
    cloud.online = False
    eng.record_tasks([task(1), task(2)])
    eng.flush()
    cloud.online = True
    again = cs.SyncEngine(cloud, state_path=state, clock=clock)   # "app restarted"
    assert again.pending_count() == 2
    assert again.flush()["state"] == "synced"
    assert cloud_ids(cloud) == ["1", "2"]


def test_a_failure_halfway_keeps_only_the_unsent_changes(parts):
    eng, cloud, _, _ = parts
    eng.record_tasks([task(1), task(2), task(3)])
    real_set = cloud.set
    count = {"n": 0}

    def flaky(path, value):
        count["n"] += 1
        if count["n"] == 2:
            cloud.online = False
        real_set(path, value)

    cloud.set = flaky
    eng.flush()
    assert cloud_ids(cloud) == ["1"] and eng.pending_count() == 2
    cloud.set = real_set
    cloud.online = True
    eng.flush()
    assert cloud_ids(cloud) == ["1", "2", "3"]


def test_damaged_state_file_is_set_aside_and_the_engine_starts_fresh(tmp_path):
    state = tmp_path / "sync_state.json"
    state.write_text("{ not json", encoding="utf-8")
    eng = cs.SyncEngine(cs.MemoryCloud(), state_path=state)
    assert eng.pending_count() == 0
    assert list(tmp_path.glob("sync_state.json.corrupt-*"))


def test_disabled_engine_does_nothing(tmp_path):
    cloud = cs.MemoryCloud()
    eng = cs.SyncEngine(cloud, state_path=tmp_path / "s.json", enabled=False)
    assert eng.record_tasks([task(1)]) == 0 and eng.queue_record("ksm/chats/a/b", {"text": "x"}) is False
    assert eng.flush()["state"] == "off" and cloud.calls == []
    r = eng.pull_tasks([task(1)])
    assert [t["id"] for t in r.tasks] == [1] and cloud.calls == []


# ---- delete ---------------------------------------------------------------------------------

def test_delete_leaves_a_marker_in_the_cloud(parts):
    eng, cloud, _, _ = parts
    eng.record_tasks([task(1), task(2)])
    eng.flush()
    assert eng.record_tasks([task(2)]) == 1
    eng.flush()
    assert cloud.data["ksm/tasks/1"]["deleted"] is True and cloud.data["ksm/tasks/1"]["updated_at_ms"] > 0
    assert cloud.data["ksm/tasks/2"]["deleted"] is False


def test_a_marker_removes_the_task_on_another_machine(tmp_path):
    cloud = cs.MemoryCloud()
    a = cs.SyncEngine(cloud, state_path=tmp_path / "a.json", clock=Clock(1_000_000))
    b = cs.SyncEngine(cloud, state_path=tmp_path / "b.json", clock=Clock(1_000_000))
    a.record_tasks([task(1), task(2)]); a.flush()
    first = b.pull_tasks([task(1), task(2)])              # b already has both, same content
    assert first.removed == [] and first.added == []
    a._clock = Clock(5_000_000)                           # a later time
    a.record_tasks([task(2)]); a.flush()                  # a deletes task 1
    res = b.pull_tasks([task(1), task(2)])
    assert res.removed == [1] and [t["id"] for t in res.tasks] == [2]


def test_deleted_then_recreated_id_comes_back_alive(parts):
    eng, cloud, _, _ = parts
    eng.record_tasks([task(1)]); eng.flush()
    eng.record_tasks([]); eng.flush()
    assert cloud.data["ksm/tasks/1"]["deleted"] is True
    eng.record_tasks([task(1, title="Again")]); eng.flush()
    assert cloud.data["ksm/tasks/1"]["deleted"] is False and cloud.data["ksm/tasks/1"]["title"] == "Again 1"


# ---- pull / conflicts / restore -------------------------------------------------------------------

def test_restore_on_an_empty_machine_brings_tasks_back_without_uploading(tmp_path):
    cloud = cs.MemoryCloud()
    old = cs.SyncEngine(cloud, state_path=tmp_path / "old.json", clock=Clock())
    old.record_tasks([task(1), task(2, history=["a", "b"])]); old.flush()
    fresh = cs.SyncEngine(cloud, state_path=tmp_path / "fresh.json", clock=Clock())
    res = fresh.pull_tasks([])
    assert sorted(res.added) == [1, 2] and [t["id"] for t in res.tasks] == [1, 2]
    assert res.tasks[1]["history"] == ["a", "b"]
    assert fresh.pending_count() == 0


def test_first_upload_by_pull_is_idempotent(parts):
    eng, cloud, _, _ = parts
    local = [task(1), task(2)]
    r1 = eng.pull_tasks(local)                            # cloud empty: everything queued
    assert eng.pending_count() == 2 and [t["id"] for t in r1.tasks] == [1, 2]
    eng.flush()
    cloud.calls.clear()
    r2 = eng.pull_tasks(local)
    eng.flush()
    assert eng.pending_count() == 0 and r2.added == r2.updated == r2.removed == []
    assert [c for c in cloud.calls if c[0] in ("set", "delete")] == []


def test_newer_cloud_copy_wins(tmp_path):
    cloud = cs.MemoryCloud()
    a = cs.SyncEngine(cloud, state_path=tmp_path / "a.json", clock=Clock(1_000_000))
    b = cs.SyncEngine(cloud, state_path=tmp_path / "b.json", clock=Clock(9_000_000))
    a.record_tasks([task(1)]); a.flush()
    b.pull_tasks([task(1)])
    b.record_tasks([task(1, title="Edited on b", imp=3)]); b.flush()      # newer
    res = a.pull_tasks([task(1)])
    assert res.updated == [1] and res.tasks[0]["title"] == "Edited on b 1" and res.tasks[0]["imp"] == 3
    assert a.pending_count() == 0


def test_newer_local_copy_wins_and_is_uploaded(tmp_path):
    cloud = cs.MemoryCloud()
    a = cs.SyncEngine(cloud, state_path=tmp_path / "a.json", clock=Clock(1_000_000))
    b = cs.SyncEngine(cloud, state_path=tmp_path / "b.json", clock=Clock(9_000_000))
    a.record_tasks([task(1)]); a.flush()
    b.pull_tasks([task(1)])
    edited = task(1, imp=1)
    b.record_tasks([edited]); b.flush()
    older = cs.SyncEngine(cloud, state_path=tmp_path / "c.json", clock=Clock(20_000_000))
    older.pull_tasks([task(1)])
    older.record_tasks([task(1, title="Latest", imp=3)])                  # newest of all
    res = older.pull_tasks([task(1, title="Latest", imp=3)])
    assert res.updated == [] and res.tasks[0]["title"] == "Latest 1"
    older.flush()
    assert cloud.data["ksm/tasks/1"]["title"] == "Latest 1"


def test_cloud_that_lost_its_data_is_refilled_from_local(parts):
    eng, cloud, _, _ = parts
    local = [task(1), task(2)]
    eng.record_tasks(local); eng.flush()
    cloud.data.clear()                                    # someone wiped the database
    res = eng.pull_tasks(local)
    assert [t["id"] for t in res.tasks] == [1, 2] and eng.pending_count() == 2
    eng.flush()
    assert cloud_ids(cloud) == ["1", "2"]


def test_pull_when_the_cloud_is_down_returns_local_unchanged_and_does_not_raise(parts):
    eng, cloud, _, _ = parts
    cloud.online = False
    local = [task(1)]
    res = eng.pull_tasks(local)
    assert res.error and [t["id"] for t in res.tasks] == [1]
    assert eng.status()["last_error"]


def test_a_malformed_cloud_record_is_ignored(parts):
    eng, cloud, _, _ = parts
    cloud.data["ksm/tasks/9"] = {"title": "no ids or numbers"}
    cloud.data["ksm/tasks/8"] = "junk"
    res = eng.pull_tasks([task(1)])
    assert [t["id"] for t in res.tasks] == [1] and res.error == ""


# ---- records for chat / audits / scans -----------------------------------------------------------------

def test_queue_record_uploads_and_redacts_saved_keys(parts):
    eng, cloud, state, _ = parts
    secret = "gsk_" + "a1B2c3D4e5F6g7H8i9J0"
    config.SETTINGS.set("GROQ_API_KEY", secret)
    try:
        assert eng.queue_record("ksm/chats/s1/m1", {"role": "user", "text": f"my key is {secret} ok"})
        assert secret not in state.read_text(encoding="utf-8")            # not even in the outbox file
        eng.flush()
    finally:
        config.SETTINGS.delete("GROQ_API_KEY")
    assert secret not in json.dumps(cloud.data) and "***" in cloud.data["ksm/chats/s1/m1"]["text"]


@pytest.mark.parametrize("bad", ["tasks/1", "ksm", "ksm/a.b", "ksm/a/$x", "ksm//x", "ksm/a b", "../etc"])
def test_queue_record_rejects_bad_paths(parts, bad):
    eng, _, _, _ = parts
    assert eng.queue_record(bad, {"x": 1}) is False and eng.pending_count() == 0


def test_key_helpers():
    assert cs.safe_key("2026-10-27 06:13:00") == "2026-10-27_06:13:00"
    assert cs.safe_key("a.b#c$d[e]") == "a_b_c_d_e_"
    assert cs.valid_path(f"ksm/scans/{cs.time_key()}")


def test_outbox_cap_drops_old_records_but_never_tasks(parts, monkeypatch):
    eng, cloud, _, _ = parts
    monkeypatch.setattr(cs, "MAX_OUTBOX", 5)
    eng.record_tasks([task(1), task(2)])
    for i in range(10):
        eng.queue_record(f"ksm/chats/s/m{i}", {"text": str(i)})
    assert eng.pending_count() == 5
    paths = list(eng._outbox)
    assert "ksm/tasks/1" in paths and "ksm/tasks/2" in paths and "ksm/chats/s/m9" in paths
    assert eng.status()["dropped"] == 7


def test_concurrent_flushes_send_each_change_once(parts):
    eng, cloud, _, _ = parts
    eng.record_tasks([task(i) for i in range(1, 21)])
    threads = [threading.Thread(target=eng.flush) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    eng.flush()
    sets = [c for c in cloud.calls if c[0] == "set" and c[1].startswith("ksm/tasks/")]
    assert len(sets) == 20 and eng.pending_count() == 0


def test_engine_from_config_is_off_by_default():
    eng = cs.engine_from_config(cloud=cs.MemoryCloud())
    assert eng.status()["state"] == "off"


# ---- the real Firebase adapter and the shared app -------------------------------------------------------

class FakeRef:
    def __init__(self, path, store, fail=None):
        self.path, self.store, self.fail = path, store, fail

    def get(self):
        if self.fail:
            raise self.fail
        return self.store.get(self.path)

    def set(self, v):
        if self.fail:
            raise self.fail
        self.store[self.path] = v

    def delete(self):
        if self.fail:
            raise self.fail
        self.store.pop(self.path, None)


def test_firebase_store_round_trip_through_a_fake_reference():
    store = {}
    fb = cs.FirebaseCloudStore(ref_factory=lambda p: FakeRef(p, store))
    fb.set("ksm/x", {"a": 1})
    assert fb.get("ksm/x") == {"a": 1}
    fb.delete("ksm/x")
    assert fb.get("ksm/x") is None


def test_firebase_store_turns_any_failure_into_cloud_error_without_leaking_text():
    fb = cs.FirebaseCloudStore(ref_factory=lambda p: FakeRef(p, {}, fail=RuntimeError("401 Unauthorized")))
    for call in (lambda: fb.get("ksm/x"), lambda: fb.set("ksm/x", {}), lambda: fb.delete("ksm/x")):
        with pytest.raises(cs.CloudError) as e:
            call()
        assert "rejected the key" in str(e.value)


def test_engine_survives_the_real_adapter_failing(tmp_path):
    fb = cs.FirebaseCloudStore(ref_factory=lambda p: FakeRef(p, {}, fail=RuntimeError("timed out")))
    eng = cs.SyncEngine(fb, state_path=tmp_path / "s.json")
    eng.record_tasks([task(1)])
    st = eng.flush()
    assert st["state"] == "offline" and "internet" in st["last_error"]


def test_shared_firebase_app_is_created_only_once(monkeypatch, tmp_path):
    made = []

    class FakeAdmin:
        def __init__(self):
            self.app = None

        def get_app(self):
            if self.app is None:
                raise ValueError("no app")
            return self.app

        def initialize_app(self, cred, options):
            made.append(options)
            self.app = object()
            return self.app

    class FakeCreds:
        @staticmethod
        def Certificate(path):
            return ("cert", path)

    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(firebase_app, "firebase_admin", FakeAdmin())
    monkeypatch.setattr(firebase_app, "credentials", FakeCreds)
    monkeypatch.setattr(config, "FIREBASE_SERVICE_ACCOUNT_PATH", key)
    monkeypatch.setattr(config, "FIREBASE_DATABASE_URL", "https://x-default-rtdb.firebaseio.com/")
    a = firebase_app.get_app()
    b = firebase_app.get_app()
    assert a is b and len(made) == 1                     # bridge + sync would share this one app


def test_shared_app_reports_a_missing_file_and_url_in_plain_words(monkeypatch, tmp_path):
    class FakeAdmin:
        def get_app(self):
            raise ValueError

    monkeypatch.setattr(firebase_app, "firebase_admin", FakeAdmin())
    monkeypatch.setattr(config, "FIREBASE_SERVICE_ACCOUNT_PATH", tmp_path / "missing.json")
    with pytest.raises(firebase_app.FirebaseAppError, match="not found"):
        firebase_app.get_app()
    (tmp_path / "k.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "FIREBASE_SERVICE_ACCOUNT_PATH", tmp_path / "k.json")
    monkeypatch.setattr(config, "FIREBASE_DATABASE_URL", "")
    with pytest.raises(firebase_app.FirebaseAppError, match="DATABASE_URL"):
        firebase_app.get_app()
