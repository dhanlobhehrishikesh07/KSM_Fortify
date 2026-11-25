"""Phase 5C: settings in Firebase (app/shared_settings.py), tested with a fake cloud and two or more "computers"."""
import json
import re
import threading
import time
from pathlib import Path

import pytest

from app import settings_store as ss
from app import shared_settings as sh
from app.cloud_sync import CloudError, MemoryTreeCloud, SERVER_TIME, TransactionAbort
from app.settings_store import BY_KEY, FileBackend, SettingsStore, keys_with_scope

GROQ = "gsk_abcdefghijklmnop1234"
GEMINI = "AIzaSyABCDEFGHIJKLMNOP9876"
RULES = json.loads((Path(__file__).resolve().parent.parent / "firebase" / "database.rules.json").read_text(encoding="utf-8"))


class Clock:
    def __init__(self, start=1_800_000_000_000):
        self.now = start

    def __call__(self):
        self.now += 1000
        return self.now


class View:
    """One computer's connection to the shared fake cloud: it can lose its network on its own."""

    def __init__(self, cloud):
        self.cloud = cloud
        self.online = True
        self.abort_returns_none = False      # the real SDK says (False, None) when a transaction is aborted
        self.keep_server_marker = False      # the real SDK may hand back the {".sv"} marker instead of the time
        self.calls = []

    def _check(self, what, path):
        self.calls.append((what, path))
        if not self.online:
            raise CloudError("Could not reach Firebase. Check your internet connection (or proxy).")

    def get(self, path):
        self._check("get", path)
        return self.cloud.get(path)

    def keys(self, path):
        self._check("keys", path)
        return self.cloud.keys(path)

    def set(self, path, value):
        self._check("set", path)
        self.cloud.set(path, value)

    def delete(self, path):
        self._check("delete", path)
        self.cloud.delete(path)

    def transaction(self, path, fn):
        self._check("transaction", path)
        committed, value = self.cloud.transaction(path, fn)
        if not committed and self.abort_returns_none:
            return False, None
        if committed and self.keep_server_marker and isinstance(value, dict):
            value = dict(value, updated_at=SERVER_TIME)
        return committed, value


def make(tmp_path, cloud=None, name="a", on=True, configured=True, environ=None, dotenv=None, attach=True, background=False,
         dev_id=None):
    """One 'computer': its own config folder, settings store, state file and view of the cloud."""
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    dotenv_path = None
    if dotenv:
        dotenv_path = folder / ".env"
        dotenv_path.write_text("".join(f"{k}={v}\n" for k, v in dotenv.items()), encoding="utf-8")
    store = SettingsStore(directory=folder, dotenv_path=dotenv_path, environ=environ or {},
                          secret_backend=FileBackend(folder / "secrets.json"))
    flags = {"on": on, "configured": configured}
    view = View(cloud if cloud is not None else MemoryTreeCloud(clock=Clock()))
    ident = {"device_id": dev_id or "d-" + (name.encode().hex() + "00000000")[:8], "name": f"PC {name}"}
    shared = sh.SharedSettings(store=store, cloud=view, state_path=folder / sh.STATE_FILE,
                               switch=lambda: flags["on"], configured=lambda: flags["configured"],
                               identity=lambda: ident, clock=Clock(), background=background)
    if attach:
        shared.attach()
    shared.flags, shared.view, shared.ident, shared.folder = flags, view, ident, folder
    return shared


@pytest.fixture
def cloud():
    return MemoryTreeCloud(clock=Clock())


def common_node(cloud, key):
    return cloud.get(f"{sh.COMMON_ROOT}/{key}")


def device_node(cloud, dev, key):
    return cloud.get(f"{sh.DEVICES_ROOT}/{dev}/settings/{key}")


def leaves(tree, path=""):
    if isinstance(tree, dict):
        for k, v in tree.items():
            yield from leaves(v, f"{path}/{k}")
    else:
        yield path, tree


def stored_values(cloud):
    return {p: v for p, v in leaves(cloud.get("ksm") or {})}


# ----------------------------------------------------------------------------------------------------
# What may be in Firebase
# ----------------------------------------------------------------------------------------------------

def test_the_shareable_keys_are_exactly_the_common_and_per_device_ones_and_no_secret():
    keys = sh.shareable_keys()
    assert set(keys) == set(keys_with_scope("common")) | set(keys_with_scope("per_device"))
    assert len(keys) == 18
    for k in keys:
        assert BY_KEY[k].kind != "secret"
    for k in keys_with_scope("local_only") + keys_with_scope("secret"):
        assert k not in keys


def test_a_local_only_or_secret_key_has_no_firebase_path(tmp_path):
    a = make(tmp_path)
    for k in keys_with_scope("local_only") + keys_with_scope("secret"):
        assert a._where(k) is None
    assert a._where("USER_NAME") == "ksm/config/settings/USER_NAME"
    assert a._where("THEME") == f"ksm/config/devices/{a.ident['device_id']}/settings/THEME"


def test_seed_writes_common_keys_in_common_and_per_device_keys_under_the_device(tmp_path, cloud):
    a = make(tmp_path, cloud)
    r = a.seed()
    assert r == {"written": 18, "error": ""}
    for k in keys_with_scope("common"):
        node = common_node(cloud, k)
        assert set(node) == {"value", "updated_at", "updated_by"}
        assert node["value"] == BY_KEY[k].default and isinstance(node["updated_at"], int)
        assert node["updated_by"] == "desktop:" + a.ident["device_id"]
    for k in keys_with_scope("per_device"):
        assert device_node(cloud, a.ident["device_id"], k)["value"] == BY_KEY[k].default
    assert sorted(cloud.keys(sh.COMMON_ROOT)) == sorted(keys_with_scope("common"))


def test_no_api_key_and_no_local_only_setting_is_ever_written_even_when_they_are_set(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.store.set("GROQ_API_KEY", GROQ)
    a.store.set("GEMINI_API_KEY", GEMINI)
    a.store.set("ENABLE_FIREBASE", "true")
    a.store.set("FIREBASE_DATABASE_URL", "https://x-default-rtdb.firebaseio.com/")
    a.store.set("FIREBASE_SERVICE_ACCOUNT_PATH", "mykey.json")
    a.store.set("SYNC_SETTINGS", "true")
    assert a.status()["pending"] == 0
    a.sync()
    blob = json.dumps(cloud.get("ksm"))
    for needle in (GROQ, GEMINI, "mykey.json", "firebaseio.com", "GROQ_API_KEY", "GEMINI_API_KEY", "ENABLE_FIREBASE",
                   "FIREBASE_DATABASE_URL", "FIREBASE_SERVICE_ACCOUNT_PATH", "SYNC_SETTINGS"):
        assert needle not in blob


def test_secret_and_local_only_keys_found_in_firebase_are_never_read(tmp_path, cloud):
    a = make(tmp_path, cloud)
    junk = {"value": "true", "updated_at": 5}
    for k in keys_with_scope("local_only") + keys_with_scope("secret"):
        cloud.set(f"{sh.COMMON_ROOT}/{k}", {"value": GROQ if "KEY" in k else "x", "updated_at": 5})
        cloud.set(f"{sh.DEVICES_ROOT}/{a.ident['device_id']}/settings/{k}", dict(junk, value="evil"))
    before = a.store.snapshot()
    a.refresh()
    assert a.store.snapshot() == before
    assert not a.store.has_stored("GROQ_API_KEY") and not a.store.has_stored("ENABLE_FIREBASE")
    assert len(a.warnings()) == 2 * len(keys_with_scope("local_only") + keys_with_scope("secret"))
    assert GROQ not in json.dumps(a.warnings())


def test_a_setting_in_the_wrong_place_is_ignored(tmp_path, cloud):
    a = make(tmp_path, cloud)
    cloud.set(f"{sh.COMMON_ROOT}/SCREEN_MAX_WIDTH", {"value": "800", "updated_at": 5})          # per-device key in common
    cloud.set(f"{sh.DEVICES_ROOT}/{a.ident['device_id']}/settings/USER_NAME", {"value": "Eve", "updated_at": 5})  # common key under a device
    a.refresh()
    assert a.store.get("SCREEN_MAX_WIDTH") == "1920" and a.store.get("USER_NAME") == "User"
    assert len(a.warnings()) == 2


def test_another_devices_per_device_settings_are_not_applied_here(tmp_path, cloud):
    a = make(tmp_path, cloud, "a")
    cloud.set(f"{sh.DEVICES_ROOT}/d-ffffffff/settings/THEME", {"value": "slate", "updated_at": 5})
    a.refresh()
    assert a.store.get("THEME") == "scifi" and a.warnings() == []


def test_everything_written_fits_the_rules_file(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.store.set("USER_PROFILE", "x" * 250)
    a.sync()
    rules = RULES["rules"]["ksm"]["config"]
    common_rx = re.search(r"matches\(/(.+?)/\)", rules["settings"]["$key"][".validate"]).group(1)
    device_rx = re.search(r"matches\(/(.+?)/\)", rules["devices"]["$deviceId"]["settings"]["$key"][".validate"]).group(1)
    dev_id_rx = re.search(r"matches\(/(.+?)/\)", rules["devices"]["$deviceId"][".validate"]).group(1)
    for key in cloud.keys(sh.COMMON_ROOT):
        assert re.match(common_rx, key)
        node = common_node(cloud, key)
        assert set(node) <= {"value", "updated_at", "updated_by"} and isinstance(node["value"], str)
        assert len(node["value"]) <= 2000 and len(node["updated_by"]) <= 60
    dev = cloud.get(f"{sh.DEVICES_ROOT}/{a.ident['device_id']}")
    assert re.match(dev_id_rx, a.ident["device_id"])
    assert set(dev) <= {"name", "kind", "last_seen", "settings"} and len(dev["name"]) <= 80 and len(dev["kind"]) <= 20
    for key, node in dev["settings"].items():
        assert re.match(device_rx, key) and set(node) <= {"value", "updated_at", "updated_by"}


# ----------------------------------------------------------------------------------------------------
# Seed and first sync
# ----------------------------------------------------------------------------------------------------

def test_seed_never_overwrites_what_firebase_has(tmp_path, cloud):
    a = make(tmp_path, cloud)
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "Owner", "updated_at": 7, "updated_by": "console"})
    a.store.set("USER_NAME", "Local")
    r = a.seed()
    assert r["written"] == 17
    assert common_node(cloud, "USER_NAME") == {"value": "Owner", "updated_at": 7, "updated_by": "console"}


def test_seed_uses_the_saved_value_of_this_device(tmp_path, cloud):
    a = make(tmp_path, cloud, attach=False)
    a.store.set("USER_NAME", "Asha")
    a.store.set("SCREEN_MAX_WIDTH", "1280")
    a.seed()
    assert common_node(cloud, "USER_NAME")["value"] == "Asha"
    assert device_node(cloud, a.ident["device_id"], "SCREEN_MAX_WIDTH")["value"] == "1280"


def test_seed_uses_an_old_dotenv_value_but_never_an_environment_variable(tmp_path, cloud):
    a = make(tmp_path, cloud, environ={"USER_NAME": "FromEnv"}, dotenv={"ASSISTANT_NAME": "FromDotenv", "USER_NAME": "DotenvName"})
    a.seed()
    assert common_node(cloud, "ASSISTANT_NAME")["value"] == "FromDotenv"
    assert common_node(cloud, "USER_NAME") is None            # an environment variable on this device: not uploaded, not seeded
    assert "FromEnv" not in json.dumps(cloud.get("ksm"))


def test_first_sync_firebase_wins_when_it_differs_and_the_old_value_is_backed_up(tmp_path, cloud):
    a = make(tmp_path, cloud, attach=False)
    a.store.set("USER_NAME", "Local")
    a.store.set("USER_PROFILE", "local profile")           # a second save, so a backup of the first exists
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "Cloud", "updated_at": 10, "updated_by": "console"})
    r = a.refresh()
    assert a.store.get("USER_NAME") == "Cloud"
    bak = json.loads((a.folder / "settings.json.bak").read_text(encoding="utf-8"))
    assert bak["values"]["USER_NAME"] == "Local"
    assert any("USER_NAME" in line for line in a.store.history())
    assert r["state"] == "synced"


def test_first_sync_does_not_overwrite_firebase_with_local_values(tmp_path, cloud):
    a = make(tmp_path, cloud)
    cloud.set(f"{sh.COMMON_ROOT}/GROQ_MODEL_ID", {"value": "model-x", "updated_at": 3, "updated_by": "console"})
    a.store.set("GROQ_MODEL_ID", "local-model")
    a.sync()
    assert common_node(cloud, "GROQ_MODEL_ID")["value"] == "model-x"
    assert a.store.get("GROQ_MODEL_ID") == "model-x"


def test_sync_registers_the_device(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    dev = cloud.get(f"{sh.DEVICES_ROOT}/{a.ident['device_id']}")
    assert dev["name"] == "PC a" and dev["kind"] == "desktop" and isinstance(dev["last_seen"], int)
    first = dev["last_seen"]
    a.sync()
    assert cloud.get(f"{sh.DEVICES_ROOT}/{a.ident['device_id']}")["last_seen"] > first


def test_a_setting_removed_in_the_console_comes_back_from_the_local_value_at_the_next_seed(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    cloud.delete(f"{sh.COMMON_ROOT}/USER_NAME")
    assert a.store.get("USER_NAME") == "User"
    a.sync()
    assert common_node(cloud, "USER_NAME")["value"] == "User"


def test_the_device_identity_is_the_real_one_by_default(tmp_path):
    folder = tmp_path / "real"
    store = SettingsStore(directory=folder, environ={}, secret_backend=FileBackend(folder / "s.json"))
    s = sh.SharedSettings(store=store, cloud=MemoryTreeCloud(), switch=lambda: True, configured=lambda: True, background=False)
    ident = s.device()
    assert re.match(r"^d-[0-9a-f]{8}$", ident["device_id"])
    assert s.device() == ident and (folder / "device.json").exists()
    assert s._path() == folder / sh.STATE_FILE


# ----------------------------------------------------------------------------------------------------
# Edits: local to Firebase and back
# ----------------------------------------------------------------------------------------------------

def test_an_edit_is_saved_locally_at_once_queued_and_sent_by_flush(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.store.set("USER_NAME", "Asha")
    assert a.store.get("USER_NAME") == "Asha" and a.status()["pending"] == 1 and a.status()["state"] == "pending"
    assert common_node(cloud, "USER_NAME")["value"] == "User"
    r = a.flush()
    assert r == {"sent": 1, "conflicts": 0, "error": ""}
    node = common_node(cloud, "USER_NAME")
    assert node["value"] == "Asha" and node["updated_by"] == "desktop:" + a.ident["device_id"]
    assert a.status()["pending"] == 0 and a.status()["state"] == "synced"


def test_a_per_device_edit_goes_under_this_device_only(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    a.store.set("SCREEN_MAX_WIDTH", "1280")
    a.flush()
    assert device_node(cloud, a.ident["device_id"], "SCREEN_MAX_WIDTH")["value"] == "1280"
    assert device_node(cloud, b.ident["device_id"], "SCREEN_MAX_WIDTH")["value"] == "1920"
    b.refresh()
    assert b.store.get("SCREEN_MAX_WIDTH") == "1920"


def test_a_common_edit_reaches_the_other_computer_live_through_its_store_listeners(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    heard = []
    b.store.add_listener(heard.append)
    a.store.set("GEMINI_MODEL_ID", "gemini-new")
    a.flush()
    b.refresh()
    assert b.store.get("GEMINI_MODEL_ID") == "gemini-new"
    assert ["GEMINI_MODEL_ID"] in heard


def test_a_value_that_came_from_firebase_is_not_sent_back(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    a.store.set("USER_NAME", "Asha")
    a.flush()
    b.view.calls.clear()
    b.refresh()
    assert b.store.get("USER_NAME") == "Asha" and b.status()["pending"] == 0
    assert not [c for c in b.view.calls if c[0] in ("set", "transaction", "delete")]


def test_writing_a_value_that_came_from_firebase_never_even_reaches_the_queue(tmp_path, cloud):
    a = make(tmp_path, cloud)
    asked = []
    real = a._queue
    a._queue = lambda key: asked.append(key) or real(key)
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "Asha", "updated_at": 5})
    a.refresh()
    assert a.store.get("USER_NAME") == "Asha" and asked == []
    a.store.set("USER_NAME", "typed here")
    assert asked == ["USER_NAME"]


def test_setting_the_same_value_again_sends_nothing(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.store.set("USER_NAME", "User")
    assert a.status()["pending"] == 0


def test_deleting_a_saved_value_sends_the_default(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.store.set("USER_NAME", "Asha"); a.flush()
    a.store.delete("USER_NAME")
    a.flush()
    assert common_node(cloud, "USER_NAME")["value"] == "User"


def test_remote_values_are_validated_and_a_bad_one_is_ignored(tmp_path, cloud):
    a = make(tmp_path, cloud)
    dev = a.ident["device_id"]
    cloud.set(f"{sh.DEVICES_ROOT}/{dev}/settings/SCREEN_JPEG_QUALITY", {"value": "999", "updated_at": 5})
    cloud.set(f"{sh.DEVICES_ROOT}/{dev}/settings/THEME", {"value": "neon", "updated_at": 5})
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "", "updated_at": 5})
    cloud.set(f"{sh.COMMON_ROOT}/PROMPT_VERSIONS_KEEP", {"value": "3", "updated_at": 5})
    cloud.set(f"{sh.COMMON_ROOT}/GROQ_MODEL_ID", {"value": "x" * 2001, "updated_at": 5})
    cloud.set(f"{sh.COMMON_ROOT}/ASSISTANT_NAME", "just text")
    cloud.set(f"{sh.COMMON_ROOT}/USER_PROFILE", {"value": 42, "updated_at": 5})
    cloud.set(f"{sh.COMMON_ROOT}/SYNC_TASKS", {"value": "true"})
    before = a.store.snapshot()
    a.refresh()
    assert a.store.snapshot() == before
    assert len(a.warnings()) == 8


def test_a_valid_remote_value_is_tidied_like_a_typed_one(tmp_path, cloud):
    a = make(tmp_path, cloud)
    cloud.set(f"{sh.DEVICES_ROOT}/{a.ident['device_id']}/settings/THEME", {"value": "SLATE", "updated_at": 5})
    cloud.set(f"{sh.COMMON_ROOT}/SYNC_CHAT", {"value": "yes", "updated_at": 5})
    a.refresh()
    assert a.store.get("THEME") == "slate" and a.store.get("SYNC_CHAT") == "true"


def test_odd_snapshots_never_raise(tmp_path):
    a = make(tmp_path)
    for settings, devices in ((None, None), ("x", 5), ([], []), ({"USER_NAME": None}, {"d-1": "x"}), (5, {a.ident["device_id"]: {"settings": []}})):
        r = a.apply_snapshot(settings, devices)
        assert set(r) == {"applied", "restart", "conflicts", "warnings"}


def test_a_value_needing_a_restart_is_reported(tmp_path, cloud):
    a = make(tmp_path, cloud)
    dev = a.ident["device_id"]
    cloud.set(f"{sh.DEVICES_ROOT}/{dev}/settings/DATA_FILE_NAME", {"value": "other.json", "updated_at": 5})
    cloud.set(f"{sh.DEVICES_ROOT}/{dev}/settings/DEBUG_UI", {"value": "true", "updated_at": 5})
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "Asha", "updated_at": 5})
    r = a.apply_snapshot(cloud.get(sh.COMMON_ROOT), {dev: cloud.get(f"{sh.DEVICES_ROOT}/{dev}")})
    assert sorted(r["restart"]) == ["DATA_FILE_NAME", "DEBUG_UI"]
    assert sorted(r["applied"]) == ["DATA_FILE_NAME", "DEBUG_UI", "USER_NAME"]


def test_the_theme_travels_as_a_per_device_setting(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    a.store.set("THEME", "forest"); a.flush()
    b.refresh()
    assert b.store.get("THEME") == "scifi"
    assert device_node(cloud, a.ident["device_id"], "THEME")["value"] == "forest"


# ----------------------------------------------------------------------------------------------------
# Environment variables
# ----------------------------------------------------------------------------------------------------

def test_an_environment_variable_is_never_uploaded_on_edit(tmp_path, cloud):
    a = make(tmp_path, cloud, environ={"GROQ_MODEL_ID": "env-model"})
    a.sync()
    a.store.set("GROQ_MODEL_ID", "typed-model")
    assert a.status()["pending"] == 0
    assert a.overridden() == ["GROQ_MODEL_ID"]
    assert common_node(cloud, "GROQ_MODEL_ID") is None
    assert "env-model" not in json.dumps(cloud.get("ksm")) and "typed-model" not in json.dumps(cloud.get("ksm"))


def test_firebase_still_updates_the_saved_value_under_an_environment_variable(tmp_path, cloud):
    a = make(tmp_path, cloud, environ={"GROQ_MODEL_ID": "env-model"})
    cloud.set(f"{sh.COMMON_ROOT}/GROQ_MODEL_ID", {"value": "cloud-model", "updated_at": 5})
    a.refresh()
    assert a.store.get("GROQ_MODEL_ID") == "env-model"          # the environment variable still wins here
    assert a.store.value_ignoring_env("GROQ_MODEL_ID") == ("cloud-model", "store")
    assert a.store.resolve("GROQ_MODEL_ID").source == "env"


def test_value_ignoring_env_follows_the_normal_order_without_the_environment(tmp_path):
    a = make(tmp_path, environ={"USER_NAME": "E"}, dotenv={"USER_NAME": "D", "ASSISTANT_NAME": "DN"})
    assert a.store.value_ignoring_env("USER_NAME") == ("D", "dotenv")
    a.store.set("USER_NAME", "S")
    assert a.store.value_ignoring_env("USER_NAME") == ("S", "store")
    assert a.store.value_ignoring_env("USER_PROFILE") == (BY_KEY["USER_PROFILE"].default, "default")
    assert a.store.value_ignoring_env("GROQ_API_KEY") == ("", "secret")
    with pytest.raises(ss.SettingsError):
        a.store.value_ignoring_env("NOPE")


# ----------------------------------------------------------------------------------------------------
# Two devices and conflicts
# ----------------------------------------------------------------------------------------------------

def test_two_devices_changing_the_same_setting_the_first_to_reach_firebase_wins_and_the_other_is_told(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    a.store.set("USER_NAME", "FromA")
    b.store.set("USER_NAME", "FromB")
    a.flush()
    r = b.flush()
    assert r["conflicts"] == 1 and r["sent"] == 0
    assert common_node(cloud, "USER_NAME")["value"] == "FromA"
    assert b.store.get("USER_NAME") == "FromA"
    notes = b.notices()
    assert len(notes) == 1 and notes[0]["key"] == "USER_NAME" and notes[0]["mine"] == "FromB" and notes[0]["theirs"] == "FromA"
    assert b.status()["pending"] == 0 and b.status()["notices"] == 1
    assert a.notices() == []


def test_changes_to_different_settings_both_survive(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    a.store.set("USER_NAME", "FromA")
    b.store.set("USER_PROFILE", "profile B")
    a.flush(); b.flush()
    assert common_node(cloud, "USER_NAME")["value"] == "FromA" and common_node(cloud, "USER_PROFILE")["value"] == "profile B"
    a.refresh(); b.refresh()
    assert a.store.get("USER_PROFILE") == b.store.get("USER_PROFILE") == "profile B"
    assert a.store.get("USER_NAME") == b.store.get("USER_NAME") == "FromA"


def test_an_offline_edit_that_conflicts_is_not_uploaded_and_loses_with_a_notice(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    b.view.online = False
    b.store.set("USER_NAME", "OfflineB")
    assert b.flush()["error"] and b.status()["state"] == "offline" and b.status()["pending"] == 1
    a.store.set("USER_NAME", "OnlineA"); a.flush()
    b.view.online = True
    b.sync()
    assert common_node(cloud, "USER_NAME")["value"] == "OnlineA"
    assert b.store.get("USER_NAME") == "OnlineA"
    assert [n["mine"] for n in b.notices()] == ["OfflineB"] and b.status()["pending"] == 0


def test_an_offline_edit_without_a_conflict_goes_up_after_reconnecting(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    a.store.set("USER_NAME", "Offline")
    assert a.status()["state"] == "pending"
    assert a.flush()["error"] and a.status()["state"] == "offline"
    a.view.online = True
    assert a.sync()["state"] == "synced"
    assert common_node(cloud, "USER_NAME")["value"] == "Offline" and a.notices() == []


def test_an_offline_edit_survives_a_restart(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    a.store.set("USER_PROFILE", "kept")
    again = make(tmp_path, cloud, "a")
    assert again.status()["pending"] == 1 and again.status()["pending_keys"] == ["USER_PROFILE"]
    again.sync()
    assert common_node(cloud, "USER_PROFILE")["value"] == "kept"


def test_a_conflict_after_a_restart_is_still_detected(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    b.view.online = False
    b.store.set("USER_NAME", "B")
    a.store.set("USER_NAME", "A"); a.flush()
    b2 = make(tmp_path, cloud, "b")
    b2.sync()
    assert common_node(cloud, "USER_NAME")["value"] == "A" and b2.store.get("USER_NAME") == "A"
    assert len(b2.notices()) == 1


def test_several_offline_edits_of_one_setting_go_up_as_one_write_with_the_last_value(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    for v in ("one", "two", "three"):
        a.store.set("USER_NAME", v)
    a.view.online = True
    a.view.calls.clear()
    a.flush()
    assert common_node(cloud, "USER_NAME")["value"] == "three"
    assert len([c for c in a.view.calls if c[0] == "transaction"]) == 1


def test_a_change_made_in_the_console_while_an_edit_waits_wins_when_the_listener_delivers_it(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    a.store.set("USER_NAME", "mine")
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "console", "updated_at": 99, "updated_by": "console"})
    r = a.apply_snapshot(cloud.get(sh.COMMON_ROOT), {})
    assert r["conflicts"] == ["USER_NAME"] and a.store.get("USER_NAME") == "console" and a.status()["pending"] == 0


def test_a_snapshot_with_nothing_new_leaves_a_waiting_edit_alone(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    a.store.set("USER_NAME", "mine")
    a.apply_snapshot(cloud.get(sh.COMMON_ROOT), {})
    assert a.store.get("USER_NAME") == "mine" and a.status()["pending"] == 1 and a.notices() == []


def test_notices_are_kept_across_a_restart_and_can_be_dismissed(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    a.store.set("USER_NAME", "A"); b.store.set("USER_NAME", "B")
    a.flush(); b.flush()
    assert len(make(tmp_path, cloud, "b").notices()) == 1
    assert b.dismiss_notices() == 1 and b.notices() == []
    assert make(tmp_path, cloud, "b").notices() == []


def test_notices_never_hold_a_saved_key(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.store.set("GROQ_API_KEY", GROQ)
    a._add_notice("USER_NAME", "my " + GROQ, "their " + GROQ)
    assert GROQ not in json.dumps(a.notices())


def test_the_sdk_answer_for_an_aborted_transaction_is_handled(tmp_path, cloud):
    a, b = make(tmp_path, cloud, "a"), make(tmp_path, cloud, "b")
    a.sync(); b.sync()
    b.view.abort_returns_none = True
    a.store.set("USER_NAME", "A"); b.store.set("USER_NAME", "B")
    a.flush()
    assert b.flush()["conflicts"] == 1 and b.store.get("USER_NAME") == "A"


def test_the_server_clock_marker_in_the_sdk_answer_is_resolved_by_reading_the_value(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.view.keep_server_marker = True
    a.store.set("USER_NAME", "One"); a.flush()
    a.store.set("USER_NAME", "Two")
    r = a.flush()
    assert r["sent"] == 1 and r["conflicts"] == 0 and common_node(cloud, "USER_NAME")["value"] == "Two"


def test_a_damaged_node_in_firebase_is_overwritten_by_a_waiting_edit(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", "garbage")
    a.store.set("USER_NAME", "Fixed")
    a.flush()
    assert common_node(cloud, "USER_NAME")["value"] == "Fixed"


def test_a_remote_value_that_cannot_be_used_is_overwritten_by_my_edit_on_the_next_flush(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    cloud.set(f"{sh.COMMON_ROOT}/SYNC_CHAT", {"value": "maybe", "updated_at": 50})
    a.store.set("SYNC_CHAT", "true")
    a.flush()
    assert a.status()["pending"] == 1 and a.notices() == []
    a.flush()
    assert common_node(cloud, "SYNC_CHAT")["value"] == "true" and a.status()["pending"] == 0


# ----------------------------------------------------------------------------------------------------
# Keys must never travel
# ----------------------------------------------------------------------------------------------------

def test_a_saved_key_typed_into_a_profile_is_never_uploaded(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.store.set("GROQ_API_KEY", GROQ)
    a.store.set("USER_PROFILE", "my key is " + GROQ)
    r = a.flush()
    assert GROQ not in json.dumps(cloud.get("ksm")) and a.status()["pending"] == 0 and r["sent"] == 0
    assert any("USER_PROFILE" in w for w in a.warnings())
    assert a.store.get("USER_PROFILE") == "my key is " + GROQ        # the local value is untouched


def test_the_shape_of_a_key_typed_on_another_device_is_not_uploaded_either(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.store.set("USER_PROFILE", "AIza" + "x" * 35)
    a.flush()
    assert common_node(cloud, "USER_PROFILE")["value"] != "AIza" + "x" * 35


def test_a_key_in_a_value_that_arrives_from_firebase_is_ignored(tmp_path, cloud):
    a = make(tmp_path, cloud)
    cloud.set(f"{sh.COMMON_ROOT}/USER_PROFILE", {"value": "gsk_" + "a" * 30, "updated_at": 9})
    a.refresh()
    assert a.store.get("USER_PROFILE") == BY_KEY["USER_PROFILE"].default
    assert "gsk_" not in json.dumps(a.warnings())


def test_a_too_long_value_is_not_uploaded(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a._pending["USER_PROFILE"] = {"value": "x" * 2001, "base": 0, "seq": 1}
    a.flush()
    assert a.status()["pending"] == 0 and common_node(cloud, "USER_PROFILE")["value"] != "x" * 2001


# ----------------------------------------------------------------------------------------------------
# Switches, no Firebase, a cloud that is down
# ----------------------------------------------------------------------------------------------------

def test_with_the_switch_off_nothing_is_sent_or_read_or_queued(tmp_path, cloud):
    a = make(tmp_path, cloud, on=False)
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "Cloud", "updated_at": 5})
    a.view.calls.clear()
    a.store.set("USER_PROFILE", "typed while off")
    for call in (a.seed, a.refresh, a.flush, a.sync, a.register):
        call()
    assert a.view.calls == [] and a.status()["state"] == "off" and a.status()["pending"] == 0
    assert a.store.get("USER_NAME") == "User"
    assert common_node(cloud, "USER_PROFILE") is None


def test_with_no_firebase_configured_nothing_is_sent_or_read_and_nothing_is_raised(tmp_path, cloud):
    a = make(tmp_path, cloud, configured=False)
    a.store.set("USER_NAME", "x")
    for call in (a.seed, a.refresh, a.flush, a.sync, a.register):
        call()
    assert a.view.calls == [] and a.status()["state"] == "no_firebase" and a.status()["pending"] == 0


def test_switching_on_later_catches_up_without_overwriting_anything(tmp_path, cloud):
    a = make(tmp_path, cloud, on=False)
    a.store.set("USER_NAME", "typed while off")
    a.flags["on"] = True
    a.sync()
    assert common_node(cloud, "USER_NAME")["value"] == "typed while off"          # missing in Firebase: seeded from the saved value


def test_switching_off_stops_queueing_but_keeps_what_already_waits(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    a.store.set("USER_NAME", "waits")
    a.flags["on"] = False
    a.store.set("USER_PROFILE", "not queued")
    assert a.status()["pending_keys"] == ["USER_NAME"] and a.status()["state"] == "off"
    a.view.online = True
    a.view.calls.clear()
    a.flush()
    assert a.view.calls == []


def test_the_owner_turning_the_gate_on_asks_for_a_sync(tmp_path, cloud, monkeypatch):
    a = make(tmp_path, cloud, on=False)
    asked = []
    monkeypatch.setattr(a, "_kick", lambda work: asked.append(work))
    a.flags["on"] = True
    a.store.set("SYNC_SETTINGS", "true")
    assert asked == ["sync"]
    a.store.set("USER_NAME", "x")
    assert asked == ["sync", "flush"]


def test_the_gate_and_the_other_local_keys_are_never_queued(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.store.set("SYNC_SETTINGS", "false")
    a.store.set("ENABLE_FIREBASE", "true")
    assert a.status()["pending"] == 0


def test_no_public_method_raises_when_the_cloud_is_down(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    a.store.set("USER_NAME", "x")
    for call in (a.seed, a.refresh, a.flush, a.sync, a.register, a.status, a.notices, a.warnings, a.overridden):
        call()
    assert a.store.get("USER_NAME") == "x"                                         # the local change is kept
    assert a.status()["last_error"] and a.status()["state"] == "offline"


def test_no_public_method_raises_on_a_cloud_that_breaks_in_odd_ways(tmp_path):
    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise ValueError("odd failure with " + GROQ)
            return boom
    a = make(tmp_path)
    a.view = Broken()
    a._cloud = a.view
    a.store.set("GROQ_API_KEY", GROQ)
    a.store.set("USER_NAME", "x")
    for call in (a.seed, a.refresh, a.flush, a.sync, a.register):
        call()
    assert GROQ not in a.status()["last_error"] and a.status()["state"] == "offline"


def test_a_failed_send_keeps_the_rest_queued_in_order(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.store.set("USER_NAME", "n"); a.store.set("USER_PROFILE", "p")
    a.view.online = False
    a.flush()
    assert a.status()["pending_keys"] == ["USER_NAME", "USER_PROFILE"]
    a.view.online = True
    a.flush()
    assert common_node(cloud, "USER_NAME")["value"] == "n" and common_node(cloud, "USER_PROFILE")["value"] == "p"


def test_seed_skips_a_setting_whose_edit_is_waiting(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.store.set("USER_NAME", "waiting")
    r = a.seed()
    assert r["written"] == 17 and common_node(cloud, "USER_NAME") is None


# ----------------------------------------------------------------------------------------------------
# Speed
# ----------------------------------------------------------------------------------------------------

def test_an_edit_does_not_wait_for_a_slow_network(tmp_path, cloud):
    a = make(tmp_path, cloud, background=True)
    a.sync()
    release = threading.Event()
    real = a.view.transaction

    def slow(path, fn):
        release.wait(5)
        return real(path, fn)
    a.view.transaction = slow
    t0 = time.time()
    a.store.set("USER_NAME", "quick")
    assert time.time() - t0 < 1.0 and a.store.get("USER_NAME") == "quick"
    assert a.status()["pending"] == 1
    release.set()
    a._thread.join(5)
    assert a.status()["pending"] == 0 and common_node(cloud, "USER_NAME")["value"] == "quick"


def test_edits_made_while_a_background_send_runs_are_sent_too(tmp_path, cloud):
    a = make(tmp_path, cloud, background=True)
    a.sync()
    gate = threading.Event()
    real = a.view.transaction
    first = {"n": 0}

    def slow(path, fn):
        first["n"] += 1
        if first["n"] == 1:
            gate.wait(5)
        return real(path, fn)
    a.view.transaction = slow
    a.store.set("USER_NAME", "one")
    time.sleep(0.1)
    a.store.set("USER_PROFILE", "two")
    gate.set()
    a._thread.join(5)
    assert common_node(cloud, "USER_NAME")["value"] == "one" and common_node(cloud, "USER_PROFILE")["value"] == "two"


def test_a_value_changed_during_a_send_is_not_lost(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.sync()
    a.store.set("USER_NAME", "first")
    real = a.view.transaction

    def edit_during(path, fn):
        out = real(path, fn)
        a.store.set("USER_NAME", "second")            # the owner edits again while the first send is finishing
        return out
    a.view.transaction = edit_during
    a.flush()
    assert a.status()["pending_keys"] == ["USER_NAME"]
    a.view.transaction = real
    a.flush()
    assert common_node(cloud, "USER_NAME")["value"] == "second"


# ----------------------------------------------------------------------------------------------------
# The state file
# ----------------------------------------------------------------------------------------------------

def test_a_damaged_state_file_starts_clean_without_raising(tmp_path, cloud):
    a = make(tmp_path, cloud)
    (a.folder / sh.STATE_FILE).write_text("{not json", encoding="utf-8")
    b = make(tmp_path, cloud, "a")
    assert b.status()["pending"] == 0
    (a.folder / sh.STATE_FILE).write_text(json.dumps({"pending": {"GROQ_API_KEY": {"value": "x", "base": 0}, "USER_NAME": 5,
                                                                   "THEME": {"value": "slate", "base": 0}},
                                                      "seen": {"ENABLE_FIREBASE": 3}}), encoding="utf-8")
    c = make(tmp_path, cloud, "a")
    assert c.status()["pending_keys"] == ["THEME"] and "ENABLE_FIREBASE" not in c._seen


def test_the_state_file_holds_no_secret_and_no_local_only_key(tmp_path, cloud):
    a = make(tmp_path, cloud)
    a.store.set("GROQ_API_KEY", GROQ)
    a.store.set("FIREBASE_DATABASE_URL", "https://x-default-rtdb.firebaseio.com/")
    a.view.online = False
    a.store.set("USER_NAME", "n")
    text = (a.folder / sh.STATE_FILE).read_text(encoding="utf-8")
    assert GROQ not in text and "firebaseio" not in text and "FIREBASE_DATABASE_URL" not in text and "GROQ_API_KEY" not in text


def test_the_shared_instance_can_be_made_and_reset(tmp_path, monkeypatch):
    sh.reset_shared_for_tests()
    first = sh.get_shared()
    assert sh.get_shared() is first
    sh.reset_shared_for_tests()
    assert sh.get_shared() is not first
    sh.reset_shared_for_tests()
