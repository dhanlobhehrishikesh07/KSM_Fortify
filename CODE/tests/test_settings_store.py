"""Phase 3A: the common settings store (config folder, keychain, resolution order)."""
import json
import os
import stat
import sys

import pytest

from app import settings_store as ss
from app.settings_store import (
    BY_KEY, FileBackend, KeychainBackend, SettingsError, SettingsStore, mask_secret, validate_value,
)

GROQ = "gsk_abcdefghijklmnop1234"
GEMINI = "AIzaSyABCDEFGHIJKLMNOP9876"


class FakeKeyring:
    """Stands in for the `keyring` package: an in-memory keychain."""
    class FakeBackend:  # noqa: D106
        pass

    def __init__(self):
        self.items = {}

    def get_keyring(self):
        return self.FakeBackend()

    def get_password(self, service, user):
        return self.items.get((service, user))

    def set_password(self, service, user, value):
        self.items[(service, user)] = value

    def delete_password(self, service, user):
        if (service, user) not in self.items:
            raise RuntimeError("Password not found")
        del self.items[(service, user)]


def make(tmp_path, environ=None, dotenv=None, backend=None):
    dotenv_path = None
    if dotenv is not None:
        dotenv_path = tmp_path / "proj" / ".env"
        dotenv_path.parent.mkdir(exist_ok=True)
        dotenv_path.write_text(dotenv, encoding="utf-8")
    return SettingsStore(
        directory=tmp_path / "cfg", dotenv_path=dotenv_path, environ=environ or {},
        secret_backend=backend or FileBackend(tmp_path / "cfg" / "secrets.json"),
    )


# ------------------------------------------------------------------ where it lives
def test_config_dir_override_and_platform_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("KSM_CONFIG_DIR", str(tmp_path / "x"))
    assert ss.config_dir() == tmp_path / "x"
    monkeypatch.delenv("KSM_CONFIG_DIR")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert ss.config_dir() == tmp_path / "xdg" / "ksm"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "ad"))
    assert ss.config_dir() == tmp_path / "ad" / "KSM"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ss.config_dir().parts[-3:] == ("Library", "Application Support", "KSM")


# ------------------------------------------------------------------ resolution order
def test_resolution_order_env_store_dotenv_default(tmp_path):
    st = make(tmp_path, environ={"USER_NAME": "FromEnv"}, dotenv="USER_NAME=FromDotenv\nASSISTANT_NAME=DotAssistant\n")
    assert st.resolve("USER_NAME").source == "env" and st.get("USER_NAME") == "FromEnv"
    assert st.resolve("ASSISTANT_NAME").source == "dotenv" and st.get("ASSISTANT_NAME") == "DotAssistant"
    assert st.resolve("USER_PROFILE").source == "default"
    st.set("ASSISTANT_NAME", "Saved")
    assert st.resolve("ASSISTANT_NAME").source == "store" and st.get("ASSISTANT_NAME") == "Saved"
    st.delete("ASSISTANT_NAME")
    assert st.resolve("ASSISTANT_NAME").source == "dotenv"       # falls back, not to nothing


def test_saved_value_hidden_by_real_env_var_is_reported(tmp_path):
    st = make(tmp_path, environ={"USER_NAME": "E"})
    st.set("USER_NAME", "Saved")
    assert st.get("USER_NAME") == "E"
    assert "environment variable" in st.stored_source_conflict("USER_NAME")


def test_dotenv_placeholder_values_are_not_offered_for_import(tmp_path):
    st = make(tmp_path, dotenv="GROQ_API_KEY=your_groq_key_here\nGEMINI_API_KEY=" + GEMINI + "\n")
    assert st.legacy_summary()["dotenv_keys"] == ["GEMINI_API_KEY"]


# ------------------------------------------------------------------ saving
def test_normal_setting_roundtrip_survives_a_new_store_object(tmp_path):
    st = make(tmp_path)
    assert st.set("USER_NAME", "Ravi").ok
    again = make(tmp_path)
    assert again.get("USER_NAME") == "Ravi"
    data = json.loads((tmp_path / "cfg" / "settings.json").read_text())
    assert data["values"]["USER_NAME"] == "Ravi"


def test_secret_never_lands_in_settings_json_or_history(tmp_path):
    st = make(tmp_path)
    assert st.set("GROQ_API_KEY", GROQ).ok
    assert GROQ not in (tmp_path / "cfg" / "settings.json").read_text()
    st.reveal("GROQ_API_KEY")
    hist = "\n".join(st.history())
    assert "GROQ_API_KEY" in hist and "reveal" in hist and GROQ not in hist
    assert make(tmp_path).get("GROQ_API_KEY") == GROQ              # but it is really stored


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_fallback_secret_file_and_firebase_file_are_owner_only(tmp_path):
    st = make(tmp_path)
    st.set("GEMINI_API_KEY", GEMINI)
    mode = stat.S_IMODE(os.stat(tmp_path / "cfg" / "secrets.json").st_mode)
    assert mode == 0o600


def test_keychain_backend_stores_secret_outside_the_folder(tmp_path):
    kr = FakeKeyring()
    st = make(tmp_path, backend=KeychainBackend(kr))
    st.set("GROQ_API_KEY", GROQ)
    assert kr.items[("KSM", "GROQ_API_KEY")] == GROQ
    assert not (tmp_path / "cfg" / "secrets.json").exists()
    assert st.describe()["backend_secure"] is True
    st.delete("GROQ_API_KEY")
    assert st.get("GROQ_API_KEY") == "" and ("KSM", "GROQ_API_KEY") not in kr.items


def test_secrets_move_from_fallback_file_into_keychain_when_it_appears(tmp_path):
    old = make(tmp_path)                      # no keychain yet: file backend
    old.set("GROQ_API_KEY", GROQ)
    kr = FakeKeyring()
    new = make(tmp_path, backend=KeychainBackend(kr))
    assert kr.items[("KSM", "GROQ_API_KEY")] == GROQ
    assert new.get("GROQ_API_KEY") == GROQ
    assert not (tmp_path / "cfg" / "secrets.json").exists()


def test_keychain_unusable_when_disabled_or_failing(monkeypatch):
    class Boom(FakeKeyring):
        def get_password(self, *a):
            raise RuntimeError("locked")
    assert ss._keychain_usable(None) is False
    assert ss._keychain_usable(Boom()) is False
    monkeypatch.setenv("KSM_DISABLE_KEYRING", "1")
    assert ss._keychain_usable(FakeKeyring()) is False
    monkeypatch.delenv("KSM_DISABLE_KEYRING")
    assert ss._keychain_usable(FakeKeyring()) is True


def test_listener_is_told_which_keys_changed(tmp_path):
    st = make(tmp_path)
    seen = []
    st.add_listener(seen.append)
    st.set("USER_NAME", "A")
    st.delete("USER_NAME")
    assert seen == [["USER_NAME"], ["USER_NAME"]]


# ------------------------------------------------------------------ damaged file
def test_damaged_settings_file_is_restored_from_backup(tmp_path):
    st = make(tmp_path)
    st.set("USER_NAME", "First")
    st.set("USER_NAME", "Second")             # makes settings.json.bak hold "First"
    (tmp_path / "cfg" / "settings.json").write_text("{ not json", encoding="utf-8")
    again = make(tmp_path)
    assert again.get("USER_NAME") == "First"
    assert "backup" in again.load_problem


def test_unwritable_save_reports_an_error_not_a_crash(tmp_path, monkeypatch):
    st = make(tmp_path)
    monkeypatch.setattr(ss, "_atomic_write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = st.set("USER_NAME", "X")
    assert not res.ok and "disk full" in res.error


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("key,value,ok", [
    ("GROQ_API_KEY", GROQ, True),
    ("GROQ_API_KEY", "short", False),
    ("GROQ_API_KEY", "gsk_has a space_inside_it", False),
    ("GROQ_API_KEY", "", False),
    ("ENABLE_FIREBASE", "TRUE", True),
    ("ENABLE_FIREBASE", "maybe", False),
    ("SCREEN_MAX_WIDTH", "1280", True),
    ("SCREEN_MAX_WIDTH", "99999", False),
    ("SCREEN_MAX_WIDTH", "wide", False),
    ("SCREEN_HIDE_DELAY", "0.5", True),
    ("SCREEN_HIDE_DELAY", "-1", False),
    ("LOG_LEVEL", "info", True),
    ("LOG_LEVEL", "LOUD", False),
    ("FIREBASE_DATABASE_URL", "https://x-default-rtdb.firebaseio.com/", True),
    ("FIREBASE_DATABASE_URL", "ftp://nope", False),
    ("USER_NAME", "", False),
    ("DATA_FILE_NAME", "../evil.json", False),
])
def test_validation(key, value, ok):
    assert validate_value(BY_KEY[key], value).ok is ok


def test_validation_normalises_and_warns_without_blocking():
    assert validate_value(BY_KEY["ENABLE_FIREBASE"], "Yes").value == "true"
    assert validate_value(BY_KEY["LOG_LEVEL"], "warning").value == "WARNING"
    odd = validate_value(BY_KEY["GROQ_API_KEY"], "sk-" + "x" * 20)
    assert odd.ok and "gsk_" in odd.warning
    assert "Firebase" in validate_value(BY_KEY["FIREBASE_DATABASE_URL"], "https://example.com").warning


def test_unknown_key_is_rejected(tmp_path):
    st = make(tmp_path)
    assert not st.set("NOT_A_SETTING", "x").ok
    with pytest.raises(SettingsError):
        st.resolve("NOT_A_SETTING")


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("abc") == "•••"
    assert mask_secret(GROQ) == "gsk_…1234"
    assert GROQ not in mask_secret(GROQ)


# ------------------------------------------------------------------ what the UI may see
def test_describe_never_contains_a_secret(tmp_path):
    st = make(tmp_path)
    st.set("GROQ_API_KEY", GROQ)
    st.set("GEMINI_API_KEY", GEMINI)
    blob = json.dumps(st.describe())
    assert GROQ not in blob and GEMINI not in blob
    e = {x["key"]: x for x in st.describe()["entries"]}
    assert e["GROQ_API_KEY"]["shown"] == "gsk_…1234" and e["GROQ_API_KEY"]["value"] == ""
    assert e["GROQ_API_KEY"]["source"] == "store" and e["GEMINI_API_KEY"]["is_set"]
    assert e["ENABLE_FIREBASE"]["restart"] is True and e["USER_NAME"]["restart"] is False


def test_online_copy_switches_are_not_marked_restart_needed(tmp_path):
    """Phase 4F-2: the three switches are read live by the workers, so the Settings tab must not ask for a restart."""
    e = {x["key"]: x for x in make(tmp_path).describe()["entries"]}
    for key in ("SYNC_TASKS", "SYNC_CHAT", "SYNC_AUDITS_SCANS"):
        assert e[key]["restart"] is False, key


def test_only_secrets_can_be_revealed(tmp_path):
    st = make(tmp_path)
    st.set("GROQ_API_KEY", GROQ)
    assert st.reveal("GROQ_API_KEY") == GROQ
    with pytest.raises(SettingsError):
        st.reveal("USER_NAME")


# ------------------------------------------------------------------ import / export
def test_import_env_text_skips_saved_values_unless_overwrite(tmp_path):
    st = make(tmp_path)
    st.set("USER_NAME", "Kept")
    text = f"USER_NAME=FromFile\nGROQ_API_KEY={GROQ}\nUNKNOWN=1\nSCREEN_MAX_WIDTH=99999\n"
    r = st.import_env_text(text)
    assert r["imported"] == ["GROQ_API_KEY"] and r["skipped"] == ["USER_NAME"]
    assert any("SCREEN_MAX_WIDTH" in p for p in r["problems"])
    assert st.get("USER_NAME") == "Kept"
    r2 = st.import_env_text("USER_NAME=Replaced", overwrite=True)
    assert r2["imported"] == ["USER_NAME"] and st.get("USER_NAME") == "Replaced"


def test_import_env_text_with_nothing_useful_is_an_error(tmp_path):
    with pytest.raises(SettingsError):
        make(tmp_path).import_env_text("hello world")


def test_backup_has_no_secrets_and_restores_normal_settings(tmp_path):
    st = make(tmp_path)
    st.set("USER_NAME", "Ravi")
    st.set("LOG_LEVEL", "INFO")
    st.set("GROQ_API_KEY", GROQ)
    backup = st.export_json()
    assert GROQ not in backup and "GROQ_API_KEY" in json.loads(backup)["secrets_set"]
    other = make(tmp_path / "second")
    r = other.import_json(backup)
    assert sorted(r["imported"]) == ["LOG_LEVEL", "USER_NAME"]
    assert other.get("USER_NAME") == "Ravi" and other.get("GROQ_API_KEY") == ""
    with pytest.raises(SettingsError):
        other.import_json("[1,2]")
    with pytest.raises(SettingsError):
        other.import_json("not json")


# ------------------------------------------------------------------ legacy project files
FB = {"type": "service_account", "project_id": "demo-proj", "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
      "client_email": "bot@demo-proj.iam.gserviceaccount.com"}


def test_legacy_env_and_firebase_file_are_offered_then_imported_once(tmp_path):
    st = make(tmp_path, dotenv=f"GROQ_API_KEY={GROQ}\nUSER_NAME=Old\nFIREBASE_SERVICE_ACCOUNT_PATH=serviceAccountKey.json\n")
    (tmp_path / "proj" / "serviceAccountKey.json").write_text(json.dumps(FB))
    info = st.legacy_summary()
    assert set(info["dotenv_keys"]) == {"GROQ_API_KEY", "USER_NAME"} and info["firebase_file"] and not info["dismissed"]
    r = st.import_legacy()
    assert "GROQ_API_KEY" in r["imported"] and "FIREBASE_SERVICE_ACCOUNT_PATH" in r["imported"] and not r["problems"]
    assert st.resolve("GROQ_API_KEY").source == "store"
    assert (tmp_path / "cfg" / "firebase" / "serviceAccountKey.json").exists()
    assert st.firebase_current()["in_store"] is True and st.firebase_current()["project_id"] == "demo-proj"
    assert st.legacy_summary()["dismissed"] is True
    assert (tmp_path / "proj" / ".env").exists() and (tmp_path / "proj" / "serviceAccountKey.json").exists()  # originals untouched


def test_dismissing_the_offer_is_remembered(tmp_path):
    st = make(tmp_path, dotenv=f"GROQ_API_KEY={GROQ}\n")
    st.dismiss_legacy_offer()
    assert make(tmp_path, dotenv=f"GROQ_API_KEY={GROQ}\n").legacy_summary()["dismissed"] is True


@pytest.mark.parametrize("text,fragment", [
    ("nope", "valid JSON"),
    ("[]", "not a service account"),
    (json.dumps({"type": "service_account"}), "missing"),
    (json.dumps({**FB, "type": "authorized_user"}), "type"),
    (json.dumps({**FB, "private_key": "plain"}), "private key"),
])
def test_bad_firebase_files_are_refused_with_a_reason(tmp_path, text, fragment):
    with pytest.raises(SettingsError) as e:
        make(tmp_path).import_firebase_text(text)
    assert fragment in str(e.value)
    assert not (tmp_path / "cfg" / "firebase").exists() or not list((tmp_path / "cfg" / "firebase").glob("*.json"))


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_imported_firebase_file_is_owner_only_and_removable(tmp_path):
    st = make(tmp_path)
    st.import_firebase_text(json.dumps(FB))
    p = tmp_path / "cfg" / "firebase" / "serviceAccountKey.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert st.get("FIREBASE_SERVICE_ACCOUNT_PATH") == str(p)
    st.remove_firebase_file()
    assert not p.exists() and st.resolve("FIREBASE_SERVICE_ACCOUNT_PATH").source == "default"


# ------------------------------------------------------------------ redaction helper
def test_redact_text_removes_every_known_secret(tmp_path):
    st = make(tmp_path)
    st.set("GROQ_API_KEY", GROQ)
    st.set("GEMINI_API_KEY", GEMINI)
    out = ss.redact_text(f"bad key {GROQ} and {GEMINI}!", store=st)
    assert GROQ not in out and GEMINI not in out and out.count("***") == 2
    assert ss.redact_text(None, store=st) == ""


# ------------------------------------------------------------------ the real `keyring` package
def test_works_with_the_real_keyring_package(tmp_path, monkeypatch):
    keyring = pytest.importorskip("keyring")
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError

    class Memory(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self.d = {}

        def get_password(self, service, username):
            return self.d.get((service, username))

        def set_password(self, service, username, password):
            self.d[(service, username)] = password

        def delete_password(self, service, username):
            if (service, username) not in self.d:
                raise PasswordDeleteError("not found")
            del self.d[(service, username)]

    old = keyring.get_keyring()
    mem = Memory()
    keyring.set_keyring(mem)
    monkeypatch.delenv("KSM_DISABLE_KEYRING")
    try:
        assert ss._keychain_usable(keyring) is True
        st = make(tmp_path, backend=KeychainBackend(keyring))
        st.set("GROQ_API_KEY", GROQ)
        assert mem.d[("KSM", "GROQ_API_KEY")] == GROQ
        assert st.get("GROQ_API_KEY") == GROQ
        st.delete("GROQ_API_KEY")
        assert st.delete("GROQ_API_KEY").ok            # deleting something already gone is fine
        assert st.get("GROQ_API_KEY") == ""
    finally:
        keyring.set_keyring(old)


# ---- Phase 4A: online-copy switches and the shipped rules file --------------------------------

def test_online_copy_switches_exist_and_start_off():
    from app import settings_store as ss
    for key in ("SYNC_TASKS", "SYNC_CHAT", "SYNC_AUDITS_SCANS"):
        d = ss.BY_KEY[key]
        assert d.kind == "bool" and d.default == "false" and d.group == "cloud"
    assert any(g[0] == "cloud" for g in ss.GROUPS)


def test_online_copy_switches_reach_config():
    from app import config
    assert config.SYNC_TASKS is False and config.SYNC_CHAT is False and config.SYNC_AUDITS_SCANS is False


def test_rules_file_is_valid_locked_and_has_no_real_ids_or_secrets():
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "firebase" / "database.rules.json"
    text = path.read_text(encoding="utf-8")
    rules = json.loads(text)["rules"]
    assert rules[".read"] is False and rules[".write"] is False       # everything denied by default
    for branch in ("ksm", "incoming_tasks", "ai_responses"):
        assert "auth.uid ===" in rules[branch][".read"] and "auth.uid ===" in rules[branch][".write"]
    assert text.count("REPLACE_WITH_OWNER_UID") >= 6                   # placeholder only, never a real uid
    assert "private_key" not in text and "BEGIN" not in text
