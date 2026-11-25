"""Phase 4C: TaskStore wired to the online-copy engine, tested against an in-memory cloud.
No network, no real Firebase, and never the owner's real task file (every store uses a temp folder)."""
import hashlib
import json
import shutil
import threading
import time

import pytest

from app import cloud_sync as cs
from app import config, task_sync
from app.data_store import TaskStore
from app.task_sync import TaskSync

FUTURE = "2030-01-01 10:00:00"


def raw_task(i, title="Old task", imp=2, urg=2):
    return {"id": i, "title": f"{title} {i}", "imp": imp, "urg": urg, "created_at": "2026-10-20 09:00:00",
            "expected_at": FUTURE, "history": [f"[2026-10-20 09:00:00] [MANUAL] Created."]}


class Clock:
    def __init__(self, start=1_000_000):
        self.t = start

    def __call__(self):
        self.t += 10
        return self.t


class Env:
    """One 'machine': a task file, a sync state file, a marker, a backups folder, and a shared cloud."""

    def __init__(self, tmp_path, cloud=None, seed=None, switch=True, name="m"):
        self.root = tmp_path / name
        self.root.mkdir()
        self.data = self.root / "matrix.json"
        if seed is not None:
            self.data.write_text(json.dumps(seed, indent=4), encoding="utf-8")
        self.cloud = cloud or cs.MemoryCloud()
        self.on = switch
        self.store = TaskStore(data_file=self.data)
        self.engine = cs.SyncEngine(self.cloud, state_path=self.root / "sync_state.json", clock=Clock())
        self.sync = TaskSync(self.store, self.engine, switch=lambda: self.on, backup_dir=self.root / "backups",
                             marker_path=self.root / "task_sync.json", background=False, retry_seconds=None)

    @property
    def backups(self):
        return sorted((self.root / "backups").glob("*")) if (self.root / "backups").exists() else []

    def cloud_ids(self):
        return sorted(int(p.rsplit("/", 1)[1]) for p, v in self.cloud.data.items()
                      if p.startswith("ksm/tasks/") and not v.get("deleted"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, seed=[raw_task(101), raw_task(102)])


# ---- switch off: behaves exactly as before --------------------------------------------------------------

def test_switch_off_sends_nothing_writes_nothing(tmp_path):
    e = Env(tmp_path, seed=[raw_task(101)], switch=False)
    before = sha(e.data)
    e.sync.start()
    e.store.add_task("New", 2, 2, FUTURE)
    e.store.delete_task(101)
    assert e.cloud.calls == [] and e.cloud.data == {}
    assert e.backups == [] and not (e.root / "task_sync.json").exists()
    assert not (e.root / "sync_state.json").exists()
    assert e.sync.status()["state"] == "off"
    assert before != sha(e.data)           # the local file still changed normally (the add and the delete)


# ---- first upload --------------------------------------------------------------------------------------

def test_first_start_backs_up_then_uploads_and_leaves_the_file_untouched(env):
    original = env.data.read_bytes()
    env.sync.start()
    assert env.cloud_ids() == [101, 102]
    assert len(env.backups) == 1 and env.backups[0].read_bytes() == original   # byte-identical backup
    assert env.data.read_bytes() == original                                   # task file not rewritten
    assert (env.root / "task_sync.json").exists()
    st = env.sync.status()
    assert st["state"] == "synced" and st["pending"] == 0 and st["initialized"] is True


def test_backup_happens_before_the_first_send(env, monkeypatch):
    order = []
    real_set = env.cloud.set
    monkeypatch.setattr(env.cloud, "set", lambda p, v: (order.append(("set", bool(env.backups))), real_set(p, v))[1])
    env.sync.start()
    assert order and all(had_backup for _, had_backup in order)


def test_running_the_first_upload_twice_changes_nothing(tmp_path):
    e1 = Env(tmp_path, seed=[raw_task(101), raw_task(102)], name="a")
    e1.sync.start()
    cloud_after_first = json.dumps(e1.cloud.data, sort_keys=True)
    # the app is closed and started again on the same machine (same files, same cloud)
    again = TaskStore(data_file=e1.data)
    engine = cs.SyncEngine(e1.cloud, state_path=e1.root / "sync_state.json", clock=Clock(9_000_000))
    s2 = TaskSync(again, engine, switch=lambda: True, backup_dir=e1.root / "backups",
                  marker_path=e1.root / "task_sync.json", background=False, retry_seconds=None)
    e1.cloud.calls.clear()
    s2.start()
    assert [c for c in e1.cloud.calls if c[0] in ("set", "delete")] == []
    assert json.dumps(e1.cloud.data, sort_keys=True) == cloud_after_first
    assert len(e1.backups) == 1                      # no second backup


def test_second_start_with_lost_sync_state_is_still_idempotent(tmp_path):
    """Sync state file deleted: the engine re-registers, but content is identical so the cloud copy is unchanged."""
    e1 = Env(tmp_path, seed=[raw_task(101)], name="a")
    e1.sync.start()
    (e1.root / "sync_state.json").unlink()
    engine = cs.SyncEngine(e1.cloud, state_path=e1.root / "sync_state.json", clock=Clock(9_000_000))
    s2 = TaskSync(TaskStore(data_file=e1.data), engine, switch=lambda: True, backup_dir=e1.root / "backups",
                  marker_path=e1.root / "task_sync.json", background=False, retry_seconds=None)
    s2.start()
    assert e1.cloud_ids() == [101]
    # Windows writes text files with \r\n; normalise so only the content, not the OS line ending, is compared
    assert e1.data.read_bytes().replace(b"\r\n", b"\n") == json.dumps([raw_task(101)], indent=4).encode()


# ---- changes after start ----------------------------------------------------------------------------------

def test_add_migrate_delete_reach_the_cloud(env):
    env.sync.start()
    t = env.store.add_task("Brand new", 2, 2, FUTURE)
    assert t["id"] in env.cloud_ids()
    assert env.cloud.data[f"ksm/tasks/{t['id']}"]["title"] == "Brand new"
    env.store.migrate_task(101, 3, 2)
    assert env.cloud.data["ksm/tasks/101"]["imp"] == 3
    env.store.delete_task(102)
    assert env.cloud.data["ksm/tasks/102"]["deleted"] is True
    assert 102 not in env.cloud_ids()
    assert env.sync.status()["pending"] == 0


def test_auto_escalation_changes_are_sent(tmp_path):
    soon = "2026-10-27 12:00:00"
    e = Env(tmp_path, seed=[{**raw_task(101, urg=1), "expected_at": soon}])
    e.store.master_offset = __import__("datetime").timedelta(0)
    e.sync.start()
    assert e.cloud.data["ksm/tasks/101"]["urg"] == 1
    e.store.set_time_warp(days=-1)          # no change in tasks: nothing new may be sent
    e.cloud.calls.clear()
    e.store.set_time_warp(days=0)
    assert [c for c in e.cloud.calls if c[0] == "set"] == []


# ---- offline / failures never reach the owner ---------------------------------------------------------------

def test_offline_at_start_changes_nothing_then_catches_up(env):
    env.cloud.online = False
    original = env.data.read_bytes()
    env.sync.start()                                  # must not raise
    assert env.data.read_bytes() == original and env.backups == [] and env.cloud.data == {}
    assert env.sync.status()["state"] == "offline" and "internet" in env.sync.status()["last_error"].lower()
    env.store.add_task("While offline", 2, 2, FUTURE)  # local save still works
    assert len(json.loads(env.data.read_text())) == 3
    env.cloud.online = True
    env.sync.sync_now()
    assert env.cloud_ids() == [101, 102, 103]
    assert len(env.backups) == 1                       # backup was taken before the first upload, once
    assert env.sync.status()["state"] == "synced"


def test_cloud_down_after_start_queues_and_recovers(env):
    env.sync.start()
    env.cloud.online = False
    t = env.store.add_task("Offline add", 2, 2, FUTURE)      # does not raise, is saved locally
    assert t in env.store.tasks and env.sync.status()["pending"] == 1
    env.cloud.online = True
    env.sync.sync_now()
    assert t["id"] in env.cloud_ids() and env.sync.status()["pending"] == 0


def test_unexpected_cloud_exception_never_reaches_the_task_list(env, monkeypatch):
    env.sync.start()

    def boom(*a, **k):
        raise RuntimeError("something odd inside the cloud adapter")
    monkeypatch.setattr(env.cloud, "set", boom)
    t = env.store.add_task("Still works", 2, 2, FUTURE)
    assert t in env.store.tasks and json.loads(env.data.read_text())[-1]["title"] == "Still works"


def test_a_failing_subscriber_inside_sync_is_contained(env, monkeypatch):
    env.sync.start()
    monkeypatch.setattr(env.engine, "record_tasks", lambda tasks: (_ for _ in ()).throw(ValueError("bad")))
    env.store.add_task("x", 2, 2, FUTURE)     # no exception out of add_task
    assert len(env.store.tasks) == 3


# ---- backup failure blocks everything -------------------------------------------------------------------------

def test_if_the_backup_cannot_be_made_nothing_is_sent(env, monkeypatch):
    original = env.data.read_bytes()

    def no_copy(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(task_sync.shutil, "copy2", no_copy)
    env.sync.start()
    assert env.cloud.data == {} and env.data.read_bytes() == original
    st = env.sync.status()
    assert st["initialized"] is False and "back up" in st["last_error"]
    assert not (env.root / "task_sync.json").exists()


def test_a_backup_that_does_not_match_is_rejected(env, monkeypatch):
    real = shutil.copy2

    def bad_copy(src, dst, *a, **k):
        real(src, dst)
        with open(dst, "ab") as f:
            f.write(b"x")
    monkeypatch.setattr(task_sync.shutil, "copy2", bad_copy)
    env.sync.start()
    assert env.cloud.data == {} and env.backups == []


def test_a_damaged_unbacked_task_file_pauses_sync(tmp_path):
    e = Env(tmp_path)
    e.data.write_bytes(b"{not json")
    e.store._save_blocked = True       # what TaskStore sets when the damaged file could not be backed up
    e.store.load_warning = "damaged"
    e.sync.start()
    assert e.cloud.calls == [] and "paused" in e.sync.status()["last_error"]


# ---- restore and conflicts -------------------------------------------------------------------------------------------

def test_empty_machine_restores_tasks_from_the_cloud(tmp_path):
    a = Env(tmp_path, seed=[raw_task(101), raw_task(102)], name="old")
    a.sync.start()
    b = Env(tmp_path, cloud=a.cloud, seed=None, name="new")        # new machine, no task file at all
    b.sync.start()
    assert [t["id"] for t in b.store.tasks] == [101, 102]
    assert [t["id"] for t in json.loads(b.data.read_text())] == [101, 102]   # written to the local file
    assert b.backups == []                                                  # there was nothing to back up


def test_newer_cloud_copy_wins_and_a_backup_is_taken_first(tmp_path):
    a = Env(tmp_path, seed=[raw_task(101, imp=1)], name="old")
    a.sync.start()
    # the cloud copy was edited later (for example from another desktop): newer time, new content
    a.cloud.data["ksm/tasks/101"] = {**a.cloud.data["ksm/tasks/101"], "imp": 3, "updated_at_ms": 9_999_999_999}
    before = a.data.read_bytes()
    engine = cs.SyncEngine(a.cloud, state_path=a.root / "sync_state.json", clock=Clock(2_000_000))
    store = TaskStore(data_file=a.data)
    s2 = TaskSync(store, engine, switch=lambda: True, backup_dir=a.root / "backups",
                  marker_path=a.root / "task_sync.json", background=False, retry_seconds=None)
    s2.start()
    assert store.tasks[0]["imp"] == 3 and json.loads(a.data.read_text())[0]["imp"] == 3
    restore = [p for p in a.backups if "pre-sync-restore" in p.name]
    assert len(restore) == 1 and restore[0].read_bytes() == before            # backup holds the pre-change file


def test_cloud_delete_marker_newer_than_local_removes_the_task(tmp_path):
    a = Env(tmp_path, seed=[raw_task(101), raw_task(102)], name="old")
    a.sync.start()
    a.cloud.data["ksm/tasks/102"] = {"id": 102, "deleted": True, "updated_at_ms": 9_999_999_999}
    engine = cs.SyncEngine(a.cloud, state_path=a.root / "sync_state.json", clock=Clock(2_000_000))
    store = TaskStore(data_file=a.data)
    TaskSync(store, engine, switch=lambda: True, backup_dir=a.root / "backups", marker_path=a.root / "task_sync.json",
             background=False, retry_seconds=None).start()
    assert [t["id"] for t in store.tasks] == [101]


def test_first_run_with_a_different_cloud_copy_keeps_the_local_one(tmp_path):
    """Single writer: with no sync history the desktop file is the working copy and the cloud is overwritten."""
    cloud = cs.MemoryCloud()
    cloud.data["ksm/tasks/101"] = {**cs.task_to_cloud(raw_task(101, title="Cloud version"), 5), "title": "Cloud version"}
    e = Env(tmp_path, cloud=cloud, seed=[raw_task(101, title="Local version")])
    e.sync.start()
    assert e.store.tasks[0]["title"] == "Local version 101"
    assert cloud.data["ksm/tasks/101"]["title"] == "Local version 101"


def test_applying_cloud_changes_keeps_a_task_made_meanwhile(env):
    new_local = env.store.add_task("Made during the network call", 2, 2, FUTURE)
    n = env.store.apply_cloud_changes(added=[raw_task(150)], updated=[], removed_ids=[101])
    ids = [t["id"] for t in env.store.tasks]
    assert n == 2 and new_local["id"] in ids and 150 in ids and 101 not in ids


def test_added_task_with_a_used_id_is_skipped(env):
    n = env.store.apply_cloud_changes(added=[raw_task(101, title="Impostor")], updated=[], removed_ids=[])
    assert n == 0 and env.store.tasks[0]["title"] == "Old task 101"


def test_failed_save_during_apply_leaves_memory_as_it_was(env, monkeypatch):
    def boom():
        raise OSError("disk")
    monkeypatch.setattr(env.store, "_save", boom)
    with pytest.raises(OSError):
        env.store.apply_cloud_changes(added=[raw_task(150)], updated=[], removed_ids=[])
    assert [t["id"] for t in env.store.tasks] == [101, 102]


# ---- switch turned on later, in the same session ---------------------------------------------------------------------------

def test_turning_the_switch_on_later_starts_with_backup_and_upload(tmp_path):
    e = Env(tmp_path, seed=[raw_task(101)], switch=False)
    e.sync.start()
    e.store.add_task("While off", 2, 2, FUTURE)
    assert e.cloud.data == {}
    e.on = True
    e.store.add_task("After on", 2, 2, FUTURE)
    assert e.cloud_ids() == [101, 102, 103] and len(e.backups) == 1


def test_turning_the_switch_off_stops_sending(env):
    env.sync.start()
    env.on = False
    env.cloud.calls.clear()
    env.store.add_task("After off", 2, 2, FUTURE)
    assert env.cloud.calls == [] and env.sync.status()["state"] == "off"
    assert env.engine.pending_count() == 0          # switched off: not even queued for later


# ---- privacy -----------------------------------------------------------------------------------------------------------------

def test_a_saved_key_inside_a_task_never_goes_online_and_does_not_reupload(tmp_path):
    secret = "gsk_" + "Zq9Xw8Vu7Ts6Rp5On4Ml3"
    config.SETTINGS.set("GROQ_API_KEY", secret)
    try:
        e = Env(tmp_path, seed=[raw_task(101, title=f"call with {secret}")])
        e.sync.start()
        assert secret not in json.dumps(e.cloud.data) and "***" in e.cloud.data["ksm/tasks/101"]["title"]
        assert secret not in (e.root / "sync_state.json").read_text(encoding="utf-8")
        engine = cs.SyncEngine(e.cloud, state_path=e.root / "sync_state.json", clock=Clock(9_000_000))
        s2 = TaskSync(TaskStore(data_file=e.data), engine, switch=lambda: True, backup_dir=e.root / "backups",
                      marker_path=e.root / "task_sync.json", background=False, retry_seconds=None)
        e.cloud.calls.clear()
        s2.start()
        assert [c for c in e.cloud.calls if c[0] == "set"] == []        # no endless re-upload
        assert secret in e.data.read_text(encoding="utf-8")            # the local file is left alone
    finally:
        config.SETTINGS.delete("GROQ_API_KEY")


# ---- the real startup helper and background mode -------------------------------------------------------------------------------

def test_start_task_sync_never_raises(tmp_path, monkeypatch):
    store = TaskStore(data_file=tmp_path / "m.json")
    monkeypatch.setattr(cs, "engine_from_config", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert task_sync.start_task_sync(store) is None


def test_start_task_sync_with_switch_off_is_inert(tmp_path):
    store = TaskStore(data_file=tmp_path / "m.json")
    assert config.SYNC_TASKS is False
    sync = task_sync.start_task_sync(store)
    store.add_task("x", 2, 2, FUTURE)
    assert sync is not None and sync.status()["state"] == "off"


def test_background_mode_sends_without_blocking(tmp_path):
    e = Env(tmp_path, seed=[raw_task(101)])
    gate = threading.Event()
    real_set = e.cloud.set

    def slow_set(p, v):
        gate.wait(5)
        real_set(p, v)
    e.cloud.set = slow_set
    e.sync._background = True
    e.sync.start()                      # returns at once although the cloud is stuck
    t = e.store.add_task("Not blocked", 2, 2, FUTURE)
    assert t in e.store.tasks
    gate.set()
    for _ in range(100):
        if 102 in e.cloud_ids():
            break
        threading.Event().wait(0.05)
    assert e.cloud_ids() == [101, 102]


def test_retry_timer_is_planned_after_a_failure_and_backs_off(tmp_path, monkeypatch):
    e = Env(tmp_path, seed=[raw_task(101)])
    e.cloud.online = False
    e.sync._retry_base = 0.05
    e.sync._retry_delay = 0.05
    e.sync.start()
    assert e.sync._retry_timer is not None and e.sync._retry_delay == 0.1
    e.cloud.online = True
    for _ in range(100):
        if e.cloud_ids() == [101]:
            break
        time.sleep(0.05)
    assert e.cloud_ids() == [101]

