"""Phase 2A: TaskStore must never lose or corrupt the owner's task file."""
import json
import shutil

import pytest

from app import data_store
from app.data_store import TaskStore


def _store(tmp_path):
    return TaskStore(data_file=tmp_path / "matrix.json")


def _backups(tmp_path):
    return sorted(tmp_path.glob("matrix.json.corrupt-*"))


def test_valid_file_loads_without_warning(tmp_path):
    f = tmp_path / "matrix.json"
    f.write_text(json.dumps([{"id": 101, "title": "x", "imp": 1, "urg": 1, "history": []}]))
    s = TaskStore(data_file=f)
    assert len(s.tasks) == 1
    assert s.load_warning == ""
    assert _backups(tmp_path) == []


def test_missing_file_starts_empty_without_warning(tmp_path):
    s = _store(tmp_path)
    assert s.tasks == []
    assert s.load_warning == ""


def test_corrupt_json_is_backed_up_byte_for_byte(tmp_path):
    f = tmp_path / "matrix.json"
    original = b'[{"id": 101, "title": "half written'
    f.write_bytes(original)
    s = TaskStore(data_file=f)
    assert s.tasks == []
    assert s.load_warning
    backups = _backups(tmp_path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_corrupt_file_survives_first_save_via_backup(tmp_path):
    f = tmp_path / "matrix.json"
    original = b"{not json at all"
    f.write_bytes(original)
    s = TaskStore(data_file=f)
    s.add_task("new", 2, 2, "2030-01-01 10:00:00")
    assert _backups(tmp_path)[0].read_bytes() == original
    assert len(json.loads(f.read_text())) == 1


def test_non_list_json_is_treated_as_corrupt(tmp_path):
    f = tmp_path / "matrix.json"
    f.write_text('{"tasks": []}')
    s = TaskStore(data_file=f)
    assert s.tasks == []
    assert s.load_warning
    assert len(_backups(tmp_path)) == 1


def test_list_of_non_objects_is_treated_as_corrupt(tmp_path):
    f = tmp_path / "matrix.json"
    f.write_text("[1, 2, 3]")
    s = TaskStore(data_file=f)
    assert s.tasks == []
    assert len(_backups(tmp_path)) == 1


def test_two_corrupt_loads_do_not_overwrite_each_other_backups(tmp_path):
    f = tmp_path / "matrix.json"
    f.write_text("{bad one")
    TaskStore(data_file=f)
    f.write_text("{bad two")
    TaskStore(data_file=f)
    assert len(_backups(tmp_path)) == 2


def test_saves_are_blocked_if_backup_of_corrupt_file_fails(tmp_path, monkeypatch):
    f = tmp_path / "matrix.json"
    f.write_text("{bad")
    monkeypatch.setattr(data_store.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    s = TaskStore(data_file=f)
    assert "not be overwritten" in s.load_warning
    with pytest.raises(data_store.TaskStoreError):
        s.add_task("new", 1, 1, "2030-01-01 10:00:00")
    assert f.read_text() == "{bad"  # untouched


def test_roundtrip_add_delete_migrate(tmp_path):
    s = _store(tmp_path)
    t = s.add_task("a", 1, 1, "2030-01-01 10:00:00")
    assert s.migrate_task(t["id"], 2, 1) is not None
    reloaded = TaskStore(data_file=tmp_path / "matrix.json")
    assert reloaded.tasks[0]["imp"] == 2
    s.delete_task(t["id"])
    assert TaskStore(data_file=tmp_path / "matrix.json").tasks == []


def test_interrupted_write_leaves_previous_file_intact(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.add_task("keep me", 3, 3, "2030-01-01 10:00:00")
    f = tmp_path / "matrix.json"
    before = f.read_text()

    def exploding_dump(obj, fp, **kw):
        fp.write('[{"id": 1, "title": "part')  # partial write, then crash
        raise OSError("power cut")

    monkeypatch.setattr(data_store.json, "dump", exploding_dump)
    with pytest.raises(OSError):
        s.add_task("lost", 1, 1, "2030-01-02 10:00:00")
    assert f.read_text() == before
    assert json.loads(before)[0]["title"] == "keep me"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_failed_add_is_not_kept_in_memory(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.add_task("one", 1, 1, "2030-01-01 10:00:00")
    monkeypatch.setattr(data_store.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        s.add_task("two", 1, 1, "2030-01-01 10:00:00")
    assert [t["title"] for t in s.tasks] == ["one"]


def test_subscriber_errors_are_isolated(tmp_path):
    s = _store(tmp_path)
    calls = []
    s.subscribe(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    s.subscribe(lambda: calls.append(1))
    s.add_task("a", 1, 1, "2030-01-01 10:00:00")
    assert calls == [1]
