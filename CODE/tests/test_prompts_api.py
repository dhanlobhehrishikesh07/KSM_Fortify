"""Phase 5E: the WebAPI methods behind the PROMPTS tab and the settings badges (no live Firebase, no window).

Everything runs against fake clouds. The methods only wrap PromptStore / SharedSettings / ConfigSync (already tested in
5A to 5D), so these tests check what the page depends on: the shape of every answer, that a failure never raises and never
carries a key, that locked text cannot be changed through the API, and that only METHODS are public on WebAPI."""
import inspect
import json

import pytest

from app import config, config_sync, prompt_store as ps, prompt_templates as pt, settings_store, shared_settings
from app.ai_assistant import AIAssistant
from app.cloud_sync import MemoryTreeCloud
from app.data_store import TaskStore
from app.gui.web_api import WebAPI
from app.screen_analyzer import ScreenAnalyzer
from app.settings_store import FileBackend, SettingsStore
from app.strategy_officer import StrategyOfficer

GROQ = "gsk_abcdefghijklmnop1234"
PHRASE = pt.TASK_TRIGGER_PHRASE


class Clock:
    def __init__(self):
        self.now = 1_800_000_000_000

    def __call__(self):
        self.now += 1000
        return self.now


def build(tmp_path, environ=None, cloud=None, on=True):
    cloud = cloud if cloud is not None else MemoryTreeCloud()
    st = SettingsStore(directory=tmp_path / "cfg", dotenv_path=tmp_path / ".env", environ=dict(environ or {}),
                       secret_backend=FileBackend(tmp_path / "cfg" / "secrets.json"))
    config.reload(st)
    prompts = ps.PromptStore(cloud=cloud, cache_path=tmp_path / "pc.json", switch=lambda: on, configured=lambda: on,
                             keep=lambda: 30, clock=Clock(), author="desktop:a")
    shared = shared_settings.SharedSettings(store=st, cloud=cloud, state_path=tmp_path / "ss.json", switch=lambda: on,
                                            configured=lambda: on, identity=lambda: {"device_id": "d-0001abcd", "name": "TestPC"},
                                            clock=Clock(), background=False)
    hub = config_sync.ConfigSync(prompts=prompts, shared=shared, cloud=cloud, background=False)
    api = WebAPI(TaskStore(data_file=tmp_path / "m.json"), AIAssistant(), StrategyOfficer(), ScreenAnalyzer(),
                 settings=st, prompts=prompts, shared=shared, hub=hub)
    return api, st, prompts, shared, cloud


@pytest.fixture
def env(tmp_path):
    return build(tmp_path)


CHAT = ps.BUILTIN_IDS["chat"]


# ------------------------------------------------------------------ what the page may see
def test_the_new_methods_are_methods_and_nothing_public_was_added_besides_methods(env):
    api = env[0]
    for name in ("prompts_get", "prompt_get", "prompt_validate", "prompt_preview", "prompt_save", "prompt_save_as_new",
                 "prompt_restore", "prompt_history", "prompt_reset_block", "prompt_reset", "prompt_delete",
                 "prompt_set_active", "prompt_resolve_conflict", "prompt_new_block_id", "config_status",
                 "config_dismiss_notices", "get_theme"):
        assert inspect.ismethod(getattr(api, name)), name
    for n in (n for n in dir(api) if not n.startswith("_")):
        assert inspect.ismethod(getattr(api, n)), f"public non-method attribute on WebAPI: {n}"


def test_prompts_get_lists_the_four_built_ins_with_the_active_one_per_role(env):
    api = env[0]
    r = api.prompts_get()
    assert r["ok"] and [x["role"] for x in r["roles"]] == list(pt.ROLES)
    assert {p["id"] for p in r["prompts"]} == set(ps.BUILTIN_IDS.values())
    assert all(x["active_id"] == ps.BUILTIN_IDS[x["role"]] for x in r["roles"])
    assert r["limits"] == {"block": 4000, "total": 20000, "soft": 6000}
    json.dumps(r)                                    # the page gets JSON: every value must be serialisable


def test_prompt_get_marks_locked_blocks_read_only_and_editable_ones_editable(env):
    r = env[0].prompt_get(CHAT)
    assert r["ok"]
    locked = {b["id"] for b in r["blocks"] if b["locked"]}
    assert locked == {"trigger_rule", "confirmation"}
    assert all(b["kind"] == "locked" for b in r["blocks"] if b["locked"])
    assert any(not b["locked"] and b["text"] for b in r["blocks"])


def test_a_locked_block_cannot_be_changed_through_the_api(env):
    api, _, prompts, _, cloud = env
    r = api.prompt_save(CHAT, {"trigger_rule": {"text": "x"}})
    assert not r["ok"] and "locked" in r["message"].lower()
    assert prompts.text("chat") == pt.fill(pt.shipped_text("chat"))


def test_changes_may_arrive_as_a_json_string_like_pywebview_sends_them(env):
    api = env[0]
    r = api.prompt_save(CHAT, json.dumps({"extra": {"text": "Be brief.", "enabled": True}}), None, "note")
    assert r["ok"], r
    assert "Be brief." in api.prompt_preview(CHAT, "{}")["text"]
    assert not api.prompt_save(CHAT, "not json")["ok"]            # a bad payload is an answer, not a crash


# ------------------------------------------------------------------ the editor helpers save nothing
def test_validate_refuses_the_trigger_phrase_live_and_saves_nothing(env):
    api, _, prompts, _, cloud = env
    before = list(cloud.calls)
    r = api.prompt_validate(CHAT, {"extra": {"text": f"say {PHRASE} now"}})
    assert r["ok"] is False and r["errors"]
    assert api.prompt_validate(CHAT, {"extra": {"text": "fine"}})["ok"] is True
    assert cloud.calls == before and prompts.text("chat") == pt.fill(pt.shipped_text("chat"))


def test_preview_fills_placeholders_reports_tokens_and_uses_no_network(env):
    api, st, prompts, _, cloud = env
    st.set("USER_NAME", "Ravi")
    config.reload(st)
    before = list(cloud.calls)
    r = api.prompt_preview(CHAT, {"extra": {"text": "Call me {USER_NAME}.", "enabled": True}})
    assert r["ok"] and "Call me Ravi." in r["text"] and "{USER_NAME}" not in r["text"]
    assert r["tokens"] > 0 and r["chars"] == len(r["text"])
    assert cloud.calls == before
    assert "Call me" not in prompts.text("chat")                  # nothing was saved


def test_preview_of_a_refused_change_says_why(env):
    r = env[0].prompt_preview(CHAT, {"extra": {"text": PHRASE}})
    assert r["ok"] is False and r["message"]


# ------------------------------------------------------------------ save, history, restore, reset, active, delete
def test_save_then_history_then_restore_then_reset(env):
    api, _, prompts, _, _ = env
    r1 = api.prompt_save(CHAT, {"extra": {"text": "One.", "enabled": True}})
    r2 = api.prompt_save(CHAT, {"extra": {"text": "Two.", "enabled": True}})
    assert r1["ok"] and r2["ok"] and r2["version"] > r1["version"]
    hist = api.prompt_history(CHAT)
    assert hist["ok"] and [v["n"] for v in hist["versions"]][0] == r2["version"]
    back = api.prompt_restore(CHAT, r1["version"])
    assert back["ok"] and "One." in prompts.text("chat") and "Two." not in prompts.text("chat")
    assert api.prompt_reset(CHAT)["ok"]
    assert prompts.text("chat") == pt.fill(pt.shipped_text("chat"))


def test_reset_block_returns_one_block_to_the_shipped_text(env):
    api, _, prompts, _, _ = env
    first = next(b for b in api.prompt_get(CHAT)["blocks"] if not b["locked"] and b["kind"] == "layout")
    api.prompt_save(CHAT, {first["id"]: {"text": "Changed text."}})
    assert "Changed text." in prompts.text("chat")
    assert api.prompt_reset_block(CHAT, first["id"])["ok"]
    assert prompts.text("chat") == pt.fill(pt.shipped_text("chat"))


def test_save_as_new_set_active_and_delete(env):
    api, _, prompts, _, _ = env
    r = api.prompt_save_as_new("chat", "Casual", {"extra": {"text": "Be casual.", "enabled": True}}, "relaxed")
    assert r["ok"] and r["prompt_id"]
    new_id = r["prompt_id"]
    assert api.prompt_set_active("chat", new_id)["ok"]
    assert "Be casual." in prompts.text("chat")
    assert next(x for x in api.prompts_get()["roles"] if x["role"] == "chat")["active_id"] == new_id
    listed = {p["id"]: p for p in api.prompts_get()["prompts"]}
    assert listed[new_id]["active"] is True and listed[CHAT]["active"] is False
    assert not api.prompt_delete(new_id)["ok"]                    # in use: refused
    assert api.prompt_set_active("chat", CHAT)["ok"]
    assert api.prompt_delete(new_id)["ok"]
    assert not api.prompt_delete(CHAT)["ok"]                      # built-in prompts cannot be deleted


def test_a_save_over_a_prompt_that_changed_elsewhere_is_a_conflict_the_page_can_show(tmp_path):
    cloud = MemoryTreeCloud()
    api, _, prompts, _, _ = build(tmp_path, cloud=cloud)
    other = ps.PromptStore(cloud=cloud, cache_path=tmp_path / "other.json", switch=lambda: True, configured=lambda: True,
                           keep=lambda: 30, clock=Clock(), author="desktop:b")
    other.sync()
    prompts.sync()
    assert other.save(CHAT, {"extra": {"text": "From the other PC.", "enabled": True}}).ok
    r = api.prompt_save(CHAT, {"extra": {"text": "Mine.", "enabled": True}}, 0)
    assert r["ok"] is False and r["conflict"] is True


def test_new_block_id_is_a_fresh_valid_custom_block(env):
    api = env[0]
    bid = api.prompt_new_block_id()["id"]
    assert bid and api.prompt_save(CHAT, {bid: {"title": "Mine", "text": "Hello.", "enabled": True, "order": 1}})["ok"]
    assert any(b["id"] == bid for b in api.prompt_get(CHAT)["blocks"])


def test_history_and_restore_say_so_when_online_prompts_are_off(tmp_path):
    api, *_ = build(tmp_path, on=False)
    assert api.prompt_history(CHAT)["ok"] is False and api.prompt_history(CHAT)["message"]
    assert api.prompt_restore(CHAT, 1)["ok"] is False


# ------------------------------------------------------------------ a failure never reaches the page, a key never leaves
class Broken:
    """A prompt library whose every call raises, with an API key in the message."""
    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError(f"boom with {GROQ} inside")
        return boom


def test_a_broken_library_gives_an_answer_not_an_exception_and_no_key(tmp_path):
    api, st, *_ = build(tmp_path)
    st.set("GROQ_API_KEY", GROQ)
    api._prompts_obj = Broken()
    calls = [lambda: api.prompts_get(), lambda: api.prompt_get("x"), lambda: api.prompt_validate("x", {}),
             lambda: api.prompt_preview("x", {}), lambda: api.prompt_save("x", {}), lambda: api.prompt_save_as_new("chat", "n"),
             lambda: api.prompt_restore("x", 1), lambda: api.prompt_history("x"), lambda: api.prompt_reset_block("x", "y"),
             lambda: api.prompt_reset("x"), lambda: api.prompt_delete("x"), lambda: api.prompt_set_active("chat", "x"),
             lambda: api.prompt_resolve_conflict("x", "mine"), lambda: api.prompt_new_block_id(), lambda: api.config_status()]
    for call in calls:
        r = call()
        assert r["ok"] is False and r["message"]
        assert GROQ not in json.dumps(r)


def test_no_saved_key_appears_in_any_answer_even_if_typed_into_a_block(env):
    api, st, *_ = env
    st.set("GROQ_API_KEY", GROQ)
    r = api.prompt_save(CHAT, {"extra": {"text": f"my key is {GROQ}"}})
    assert r["ok"] is False and GROQ not in json.dumps(r)
    for r in (api.prompts_get(), api.prompt_get(CHAT), api.config_status(), api.prompt_validate(CHAT, {"extra": {"text": GROQ}}),
              api.prompt_preview(CHAT, {"extra": {"text": GROQ}})):
        assert GROQ not in json.dumps(r)


# ------------------------------------------------------------------ status, notices, badges, theme
def test_config_status_has_everything_the_screens_need(env):
    r = env[0].config_status()
    assert r["ok"]
    assert set(r) >= {"hub", "settings", "prompts", "notices", "overridden", "device"}
    assert r["device"] == {"device_id": "d-0001abcd", "name": "TestPC"}
    assert r["settings"]["state"] in ("starting", "synced", "pending", "offline", "off", "no_firebase")
    json.dumps(r)


def test_config_status_with_online_switches_off_says_off(tmp_path):
    api, *_ = build(tmp_path, on=False)
    r = api.config_status()
    assert r["settings"]["state"] == "off" or r["settings"]["state"] == "no_firebase"
    assert r["prompts"]["state"] in ("off", "no_firebase")


def test_a_lost_edit_shows_as_a_notice_and_can_be_dismissed(env):
    api, _, _, shared, _ = env
    shared._add_notice("USER_NAME", "Mine", "Theirs")
    n = api.config_status()["notices"]
    assert len(n) == 1 and n[0]["key"] == "USER_NAME" and n[0]["theirs"] == "Theirs"
    assert api.config_dismiss_notices()["dismissed"] == 1
    assert api.config_status()["notices"] == []


def test_a_setting_set_by_a_real_environment_variable_is_listed_as_overridden(tmp_path):
    api, *_ = build(tmp_path, environ={"USER_NAME": "FromEnv"})
    assert "USER_NAME" in api.config_status()["overridden"]


def test_settings_get_carries_scope_for_every_entry_and_the_shared_status(env):
    d = json.loads(env[0].settings_get())
    scopes = {e["key"]: e["scope"] for e in d["entries"]}
    assert scopes["USER_NAME"] == "common" and scopes["SCREEN_MAX_WIDTH"] == "per_device"
    assert scopes["ENABLE_FIREBASE"] == "local_only" and scopes["GROQ_API_KEY"] == "secret"
    assert all(scopes.values())
    assert d["shared"]["ok"] and d["shared"]["device"]["name"] == "TestPC"


def test_the_new_switches_and_versions_setting_are_shown_and_the_theme_is_not(env):
    keys = {e["key"] for e in json.loads(env[0].settings_get())["entries"]}
    assert {"SYNC_PROMPTS", "SYNC_SETTINGS", "PROMPT_VERSIONS_KEEP"} <= keys and "THEME" not in keys


def test_the_theme_is_a_per_device_setting_saved_through_settings_set(env):
    api, st, *_ = env
    assert api.get_theme() == {"ok": True, "theme": "scifi"}
    assert api.settings_set("THEME", "forest")["ok"]
    assert api.get_theme()["theme"] == "forest"
    assert not api.settings_set("THEME", "neon")["ok"]            # only the four themes are valid
    assert api.get_theme()["theme"] == "forest"


def test_saving_the_versions_setting_validates_the_range(env):
    api = env[0]
    assert api.settings_set("PROMPT_VERSIONS_KEEP", "50")["ok"]
    assert not api.settings_set("PROMPT_VERSIONS_KEEP", "2")["ok"]
    assert not api.settings_set("PROMPT_VERSIONS_KEEP", "500")["ok"]


def test_a_key_inside_a_message_from_the_library_is_hidden_before_it_reaches_the_page(tmp_path):
    api, st, *_ = build(tmp_path)
    st.set("GROQ_API_KEY", GROQ)

    class Chatty:
        def get_prompt(self, pid):
            return {"ok": False, "message": f"failed with {GROQ}"}

        def history(self, pid):
            return {"ok": False, "versions": [], "message": f"offline {GROQ}"}

    api._prompts_obj = Chatty()
    for r in (api.prompt_get("x"), api.prompt_history("x")):
        assert GROQ not in json.dumps(r) and "***" in r["message"]
