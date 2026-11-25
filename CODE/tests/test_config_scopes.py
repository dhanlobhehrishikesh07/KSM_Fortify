"""Phase 5A: every setting has one scope, the new settings exist, the device id works, and the
Firebase rules for ksm/config agree with the code (they list the same keys, locked blocks and roles)."""
import json
import re
from pathlib import Path

import pytest

from app import prompt_templates as pt
from app import settings_store as ss
from app.settings_store import (
    BY_KEY, SCHEMA, SCOPE_COMMON, SCOPE_LOCAL, SCOPE_PER_DEVICE, SCOPE_SECRET, SCOPES, FileBackend, SettingsStore,
    device_identity, keys_with_scope, validate_value,
)

RULES_PATH = Path(__file__).resolve().parent.parent / "firebase" / "database.rules.json"

# The table from ROADMAP Phase 5 / firebase/DATA_LAYOUT.md, written out. If a setting moves, change this on purpose.
EXPECTED = {
    SCOPE_COMMON: {"ASSISTANT_NAME", "USER_NAME", "USER_PROFILE", "GROQ_MODEL_ID", "GEMINI_MODEL_ID", "SCREEN_MODEL_ID",
                   "SYNC_TASKS", "SYNC_CHAT", "SYNC_AUDITS_SCANS", "SYNC_PROMPTS", "PROMPT_VERSIONS_KEEP"},
    SCOPE_PER_DEVICE: {"SCREEN_MAX_WIDTH", "SCREEN_JPEG_QUALITY", "SCREEN_HIDE_DELAY", "DATA_FILE_NAME", "LOG_LEVEL",
                       "DEBUG_UI", "THEME"},
    SCOPE_LOCAL: {"ENABLE_FIREBASE", "FIREBASE_DATABASE_URL", "FIREBASE_SERVICE_ACCOUNT_PATH", "SYNC_SETTINGS"},
    SCOPE_SECRET: {"GROQ_API_KEY", "GEMINI_API_KEY"},
}


def make(tmp_path):
    return SettingsStore(directory=tmp_path / "cfg", dotenv_path=None, environ={},
                         secret_backend=FileBackend(tmp_path / "cfg" / "secrets.json"))


# ------------------------------------------------------------ scopes
def test_every_setting_has_exactly_one_valid_scope():
    assert [d.key for d in SCHEMA if d.scope not in SCOPES] == []
    assert len({d.key for d in SCHEMA}) == len(SCHEMA)                  # no duplicate keys
    assert sum(len(keys_with_scope(s)) for s in SCOPES) == len(SCHEMA)


def test_scopes_match_the_plan():
    for scope, keys in EXPECTED.items():
        assert set(keys_with_scope(scope)) == keys, scope


def test_a_secret_is_always_scope_secret_and_nothing_else_is():
    assert {d.key for d in SCHEMA if d.kind == "secret"} == set(keys_with_scope(SCOPE_SECRET))


# ------------------------------------------------------------ the four new settings
def test_new_settings_have_the_agreed_defaults(tmp_path):
    st = make(tmp_path)
    assert st.resolve("SYNC_PROMPTS").value == "true"            # owner decision: on by default
    assert st.resolve("SYNC_SETTINGS").value == "true"
    assert st.resolve("PROMPT_VERSIONS_KEEP").value == "30"
    assert st.resolve("THEME").value == "scifi"


def test_new_settings_are_shown_since_5E_except_the_theme(tmp_path):
    """5E shows the two sync switches and the versions-kept number; THEME stays hidden because the theme buttons set it."""
    shown = {e["key"] for e in make(tmp_path).describe()["entries"]}
    hidden = {d.key for d in SCHEMA if d.hidden}
    assert hidden == {"THEME"}
    assert {"SYNC_PROMPTS", "SYNC_SETTINGS", "PROMPT_VERSIONS_KEEP"} <= shown
    assert not (shown & hidden)
    assert shown == {d.key for d in SCHEMA} - hidden               # every older setting is still shown
    entry = next(e for e in make(tmp_path).describe()["entries"] if e["key"] == "USER_NAME")
    assert entry["scope"] == SCOPE_COMMON


def test_versions_kept_is_a_whole_number_between_5_and_200():
    d = BY_KEY["PROMPT_VERSIONS_KEEP"]
    assert validate_value(d, "30").ok and validate_value(d, "5").ok and validate_value(d, "200").ok
    assert not validate_value(d, "4").ok and not validate_value(d, "201").ok and not validate_value(d, "many").ok


def test_theme_accepts_the_four_themes_in_any_case_and_log_level_still_works():
    d = BY_KEY["THEME"]
    for name in ("scifi", "saffron", "slate", "forest"):
        assert validate_value(d, name).value == name
    assert validate_value(d, "FOREST").value == "forest"           # canonical spelling is returned
    assert not validate_value(d, "neon").ok
    assert validate_value(BY_KEY["LOG_LEVEL"], "debug").value == "DEBUG"
    assert not validate_value(BY_KEY["LOG_LEVEL"], "loud").ok


def test_the_new_settings_save_and_read_back(tmp_path):
    st = make(tmp_path)
    st.set("PROMPT_VERSIONS_KEEP", "50")
    st.set("THEME", "slate")
    st.set("SYNC_PROMPTS", "false")
    assert st.resolve("PROMPT_VERSIONS_KEEP").value == "50"
    assert st.resolve("THEME").value == "slate"
    assert st.resolve("SYNC_PROMPTS").value == "false"


# ------------------------------------------------------------ device identity
def test_device_identity_is_made_once_and_stays_the_same(tmp_path):
    folder = tmp_path / "cfg"
    first = device_identity(folder)
    assert re.fullmatch(r"d-[0-9a-f]{8}", first["device_id"])
    assert first["name"] and len(first["name"]) <= 80
    assert (folder / ss.DEVICE_FILE).exists()
    assert device_identity(folder) == first
    assert json.loads((folder / ss.DEVICE_FILE).read_text(encoding="utf-8")) == first


def test_two_installations_get_different_ids(tmp_path):
    assert device_identity(tmp_path / "a")["device_id"] != device_identity(tmp_path / "b")["device_id"]


def test_device_identity_survives_a_damaged_file_and_keeps_a_good_id(tmp_path):
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / ss.DEVICE_FILE).write_text("{not json", encoding="utf-8")
    fresh = device_identity(folder)
    assert re.fullmatch(r"d-[0-9a-f]{8}", fresh["device_id"])
    (folder / ss.DEVICE_FILE).write_text(json.dumps({"device_id": fresh["device_id"], "name": "  "}), encoding="utf-8")
    again = device_identity(folder)
    assert again["device_id"] == fresh["device_id"] and again["name"].strip()
    (folder / ss.DEVICE_FILE).write_text(json.dumps({"device_id": "bad id", "name": "My PC"}), encoding="utf-8")
    third = device_identity(folder)
    assert re.fullmatch(r"d-[0-9a-f]{8}", third["device_id"]) and third["name"] == "My PC"


def test_device_identity_trims_a_long_name_and_is_not_created_by_importing_the_app(tmp_path):
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / ss.DEVICE_FILE).write_text(json.dumps({"device_id": "d-0a0b0c0d", "name": "x" * 200}), encoding="utf-8")
    assert len(device_identity(folder)["name"]) == 80
    assert not (tmp_path / "other" / ss.DEVICE_FILE).exists()


# ------------------------------------------------------------ the Firebase rules agree with the code
@pytest.fixture(scope="module")
def rules():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))["rules"]


@pytest.fixture(scope="module")
def rules_text():
    return RULES_PATH.read_text(encoding="utf-8")


def _alternatives(validate: str) -> set:
    """The names inside the first matches(/^(a|b|c)$/) of a rule."""
    m = re.search(r"matches\(/\^\(([^)]*)\)\$/\)", validate)
    assert m, validate
    return set(m.group(1).split("|"))


def test_rules_keep_the_owner_check_and_the_old_rules(rules):
    assert rules[".read"] is False and rules[".write"] is False
    ksm = rules["ksm"]
    assert "REPLACE_WITH_OWNER_UID" in ksm[".read"] and "REPLACE_WITH_OWNER_UID" in ksm[".write"]
    for old in ("tasks", "audits", "scans", "chats"):
        assert old in ksm
    assert "updated_at_ms" in ksm["tasks"]["$id"][".validate"]
    for old in ("incoming_tasks", "ai_responses"):
        assert "REPLACE_WITH_OWNER_UID" in rules[old][".write"]


def test_rules_allow_exactly_the_common_and_per_device_setting_names(rules):
    cfg = rules["ksm"]["config"]
    assert _alternatives(cfg["settings"]["$key"][".validate"]) == EXPECTED[SCOPE_COMMON]
    device_rules = cfg["devices"]["$deviceId"]["settings"]["$key"]
    assert _alternatives(device_rules[".validate"]) == EXPECTED[SCOPE_PER_DEVICE]


def test_no_secret_or_local_only_setting_appears_anywhere_in_the_rules(rules_text):
    for key in EXPECTED[SCOPE_SECRET] | EXPECTED[SCOPE_LOCAL]:
        assert key not in rules_text, key


def test_rules_refuse_unknown_children_in_config(rules):
    cfg = rules["ksm"]["config"]
    assert cfg["$other"][".validate"] is False
    assert cfg["settings"]["$key"]["$other"][".validate"] is False
    assert cfg["devices"]["$deviceId"]["$other"][".validate"] is False
    assert cfg["prompts"]["$promptId"]["$other"][".validate"] is False
    assert cfg["prompts"]["$promptId"]["blocks"]["$blockId"]["$other"][".validate"] is False


def test_rules_refuse_locked_block_ids_and_list_the_same_roles_as_the_code(rules):
    cfg = rules["ksm"]["config"]
    live = cfg["prompts"]["$promptId"]["blocks"]["$blockId"][".validate"]
    stored = rules["ksm"]["config_versions"]["prompts"]["$promptId"]["$n"]["blocks"]["$blockId"][".validate"]
    for text in (live, stored):
        m = re.search(r"!\$blockId\.matches\(/\^\(([^)]*)\)\$/\)", text)
        assert m and set(m.group(1).split("|")) == set(pt.LOCKED_BLOCK_IDS)
    assert _alternatives(cfg["prompt_active"]["$role"][".validate"]) == set(pt.ROLES)
    assert _alternatives(cfg["prompts"]["$promptId"]["role"][".validate"]) == set(pt.ROLES)


def _pattern(validate: str, variable: str) -> "re.Pattern":
    m = re.search(r"\$" + variable + r"\.matches\(/(\^.*?\$)/\)", validate)
    assert m, (variable, validate)
    return re.compile(m.group(1))


def test_the_id_patterns_in_the_rules_accept_and_refuse_the_right_names(rules):
    cfg = rules["ksm"]["config"]
    device = _pattern(cfg["devices"]["$deviceId"][".validate"], "deviceId")
    assert device.match("d-1a2b3c4d") and not device.match("d-1A2B3C4D") and not device.match("d-xyz") and not device.match("../x")
    block = cfg["prompts"]["$promptId"]["blocks"]["$blockId"][".validate"]
    assert _pattern(block, "blockId").match("x-AbC_1") and not _pattern(block, "blockId").match("a b")
    locked = re.compile(r"^(" + "|".join(pt.LOCKED_BLOCK_IDS) + r")$")
    assert all(locked.match(b) for b in ("trigger_rule", "output_format")) and not locked.match("extra")
    version = _pattern(rules["ksm"]["config_versions"]["prompts"]["$promptId"]["$n"][".validate"], "n")
    assert version.match("12") and not version.match("v1") and not version.match("1234567")
    setting = re.compile(r"^(" + "|".join(EXPECTED[SCOPE_COMMON]) + r")$")
    assert setting.match("USER_NAME") and not setting.match("GROQ_API_KEY") and not setting.match("THEME")


def test_rules_size_limits_exist_on_every_text_field(rules):
    cfg = rules["ksm"]["config"]
    assert "4000" in cfg["prompts"]["$promptId"]["blocks"]["$blockId"]["text"][".validate"]
    assert "2000" in cfg["settings"]["$key"]["value"][".validate"]
    assert "4000" in rules["ksm"]["config_versions"]["prompts"]["$promptId"]["$n"]["blocks"]["$blockId"]["text"][".validate"]
