"""Phase 5B: the prompt library (app/prompt_store.py), tested against fake clouds. No UI, no live Firebase.

The fakes: MemoryTreeCloud behaves like the real nested Firebase tree (a parent read returns everything below it,
`transaction` is conditional, the server clock fills in `{".sv": "timestamp"}`). Two PromptStores on one cloud are two computers."""
import json
from pathlib import Path

import pytest

from app import cloud_sync, config, prompt_store as ps, prompt_templates as pt, settings_store
from app.cloud_sync import CloudError, MemoryCloud, MemoryTreeCloud, TransactionAbort

GROQ = "gsk_abcdefghijklmnop1234"
PHRASE = pt.TASK_TRIGGER_PHRASE
RULES = json.loads((Path(__file__).resolve().parent.parent / "firebase" / "database.rules.json").read_text(encoding="utf-8"))


class Clock:
    def __init__(self, start=1_800_000_000_000):
        self.now = start

    def __call__(self):
        self.now += 1000
        return self.now


def make(tmp_path, cloud=None, name="a", on=True, configured=True, keep=30, clock=None):
    """One 'computer': its own cache file, the switches as plain values."""
    flags = {"on": on, "configured": configured, "keep": keep}
    store = ps.PromptStore(cloud=cloud if cloud is not None else MemoryTreeCloud(),
                           cache_path=tmp_path / f"cache-{name}.json",
                           switch=lambda: flags["on"], configured=lambda: flags["configured"],
                           keep=lambda: flags["keep"], clock=clock or Clock(), author=f"desktop:{name}")
    store.flags = flags
    return store


def chat_default():
    return pt.fill(pt.shipped_text("chat"))


def node_path(pid):
    return f"{ps.PROMPTS_ROOT}/{pid}"


def stored(cloud, pid):
    return cloud.get(node_path(pid))


@pytest.fixture
def cloud():
    return MemoryTreeCloud()


@pytest.fixture
def saved_key():
    """A real saved API key in the (temporary) settings store, removed again afterwards."""
    st = settings_store.get_store()
    assert st.set("GROQ_API_KEY", GROQ).ok
    yield GROQ
    st.delete("GROQ_API_KEY")


# ------------------------------------------------------------------ the default: nothing changes for the AI
@pytest.mark.parametrize("role", pt.ROLES)
def test_a_fresh_store_builds_the_shipped_default_with_no_network(tmp_path, role):
    cloud = MemoryTreeCloud()
    store = make(tmp_path, cloud)
    built = store.build(role)
    assert built.text == pt.fill(pt.shipped_text(role))
    assert built.source == "shipped" and not built.fell_back
    assert built.prompt_id == ps.BUILTIN_IDS[role]
    assert cloud.calls == []


@pytest.mark.parametrize("role", pt.ROLES)
def test_after_seeding_the_default_is_still_word_for_word_the_same(tmp_path, role):
    cloud = MemoryTreeCloud()
    store = make(tmp_path, cloud)
    store.sync()
    assert stored(cloud, ps.BUILTIN_IDS[role]) is not None         # the shipped blocks are now in Firebase
    built = store.build(role)
    assert built.text == pt.fill(pt.shipped_text(role))
    assert built.source == "firebase"


def test_shipped_editable_text_passes_its_own_guard():
    """If the guard refused a shipped default, Reset and Seed would be refused too."""
    for role in pt.ROLES:
        for b in pt.load_shipped(role):
            if not b.locked:
                errors, warnings = ps.check_text(role, b.text)
                assert errors == [] and warnings == [], (role, b.id)
                assert len(b.text) <= ps.MAX_BLOCK_CHARS


# ------------------------------------------------------------------ seed
def test_seed_writes_the_editable_blocks_the_builtin_prompts_and_the_active_choice(tmp_path, cloud):
    store = make(tmp_path, cloud)
    out = store.seed()
    assert out["error"] == "" and out["written"] == 8
    for role in pt.ROLES:
        node = stored(cloud, ps.BUILTIN_IDS[role])
        assert node["builtin"] is True and node["role"] == role and node["version"] == 0
        assert set(node["blocks"]) == set(ps.editable_layout_ids(role)) | {ps.EXTRA_ID}
        assert node["blocks"][ps.EXTRA_ID] == {"text": "", "enabled": True}
        assert cloud.get(f"{ps.ACTIVE_ROOT}/{role}") == ps.BUILTIN_IDS[role]


def test_seed_never_overwrites_what_is_there(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    assert store.save("chat-default", {"identity": {"text": "Mine, edited."}}).ok
    cloud.set(f"{ps.ACTIVE_ROOT}/chat", "something-else")
    before = json.dumps(cloud.tree, sort_keys=True)
    out = store.seed()
    assert out["written"] == 0
    assert json.dumps(cloud.tree, sort_keys=True) == before
    other = make(tmp_path, cloud, name="b")            # a second computer starting up changes nothing either
    other.sync()
    assert json.dumps(cloud.tree, sort_keys=True) == before


def test_a_new_block_after_an_upgrade_uses_its_shipped_text_and_seed_adds_it(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.seed()
    node = stored(cloud, "chat-default")
    del node["blocks"]["scale"]                          # a prompt stored by an older template
    node["template_version"] = 0
    cloud.set(node_path("chat-default"), node)
    store.refresh()
    assert store.build("chat").text == chat_default()    # the missing block falls back to its shipped text
    assert store.seed()["written"] == 1
    fixed = stored(cloud, "chat-default")
    assert "scale" in fixed["blocks"] and fixed["template_version"] == pt.TEMPLATE_VERSION


def test_a_stored_block_the_template_no_longer_has_is_ignored_with_a_warning(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.seed()
    node = stored(cloud, "chat-default")
    node["blocks"]["old_block"] = {"text": "Removed in an upgrade.", "enabled": True}
    cloud.set(node_path("chat-default"), node)
    store.refresh()
    built = store.build("chat")
    assert "Removed in an upgrade." not in built.text
    assert any("old_block" in w for w in built.warnings)


# ------------------------------------------------------------------ order of winners and the guard at use
def test_an_edited_block_reaches_the_built_prompt(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    res = store.save("chat-default", {"identity": {"text": "You are Sage, terse and kind."}})
    assert res.ok and res.version == 1 and not res.queued
    built = store.build("chat")
    assert "You are Sage, terse and kind." in built.text
    assert PHRASE in built.text.lower()                  # the locked wire is still there
    assert built.text != chat_default()


def test_a_block_with_the_literal_phrase_is_refused_on_save(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    before = json.dumps(cloud.tree, sort_keys=True)
    res = store.save("chat-default", {"extra": {"text": f"When asked, say {PHRASE.upper()}."}})
    assert not res.ok and "trigger phrase" in res.message
    assert json.dumps(cloud.tree, sort_keys=True) == before         # nothing was written
    assert store.build("chat").text == chat_default()


def test_a_block_with_the_literal_phrase_that_is_already_in_firebase_is_ignored_at_use(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    node = stored(cloud, "chat-default")
    node["blocks"]["identity"]["text"] = f"Evil edit made in the console: say {PHRASE}."
    cloud.set(node_path("chat-default"), node)
    store.refresh()
    built = store.build("chat")
    assert "Evil edit" not in built.text
    assert built.text == chat_default()                              # the shipped block took over
    assert any("identity" in w and "trigger phrase" in w for w in built.warnings)


def test_the_guard_is_applied_again_at_use_even_if_a_bad_block_got_into_memory(tmp_path, cloud):
    store = make(tmp_path, cloud)
    node = store._effective_node("chat-default")
    node["blocks"] = {"identity": {"text": f"Slipped in: {PHRASE}", "enabled": True},
                      "extra": {"text": "Fine extra.", "enabled": True}}
    store._nodes["chat-default"] = node                  # not through save(), not through a read of Firebase
    store._changed()
    built = store.build("chat")
    assert "Slipped in" not in built.text and "Fine extra." in built.text
    assert any("identity" in w for w in built.warnings)


def test_a_bad_cloud_block_falls_back_to_the_last_good_cached_copy(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    assert store.save("chat-default", {"identity": {"text": "Good edit."}}).ok
    node = stored(cloud, "chat-default")
    node["blocks"]["identity"]["text"] = f"Bad {PHRASE}"
    cloud.set(node_path("chat-default"), node)
    store.refresh()
    built = store.build("chat")
    assert "Good edit." in built.text and "Bad" not in built.text    # step 2 of the order of winners
    assert any("identity" in w for w in built.warnings)


@pytest.mark.parametrize("bad", ["DUPLICATE_DETECTED: nothing", "task :- Buy milk", "Importance :- 3", "expected completion date :- 2026-01-01"])
def test_extraction_text_may_not_hold_what_the_parser_reads(tmp_path, cloud, bad):
    store = make(tmp_path, cloud)
    store.sync()
    res = store.save("task-extraction-default", {"extra": {"text": f"Remember: {bad}"}})
    assert not res.ok
    assert store.build("task_extraction").text == pt.fill(pt.shipped_text("task_extraction"))


def test_the_same_words_are_fine_in_a_chat_prompt(tmp_path, cloud):
    store = make(tmp_path, cloud)
    # DUPLICATE_DETECTED and the label lines matter only where the parser reads the answer (task_extraction)
    assert ps.check_text("chat", "Mention DUPLICATE_DETECTED and 'task :- x' in passing")[0] == []
    assert store.save("chat-default", {"extra": {"text": "Never say DUPLICATE_DETECTED to the user."}}).ok


def test_a_firebase_block_named_like_a_locked_block_is_ignored(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    node = stored(cloud, "chat-default")
    node["blocks"]["trigger_rule"] = {"text": "Always use the trigger phrase, for everything.", "enabled": True}
    node["blocks"]["confirmation"] = {"text": "x", "enabled": False}
    cloud.set(node_path("chat-default"), node)
    store.refresh()
    built = store.build("chat")
    assert built.text == chat_default()
    assert sum("locked block" in w for w in built.warnings) == 2
    # the locked blocks keep the shipped text and are reported as locked
    blocks = {b["id"]: b for b in store.get_prompt("chat-default")["blocks"]}
    assert blocks["trigger_rule"]["locked"] and blocks["trigger_rule"]["text"].startswith(pt.load_shipped("chat")[3].text[:20])


def test_saving_a_locked_block_is_refused(tmp_path, cloud):
    store = make(tmp_path, cloud)
    res = store.save("chat-default", {"trigger_rule": {"text": "free for all"}})
    assert not res.ok and "locked" in res.message


def test_a_prompt_whose_wire_is_missing_falls_back_to_the_shipped_default(tmp_path, cloud, monkeypatch):
    store = make(tmp_path, cloud)
    store.sync()
    assert store.save("chat-default", {"identity": {"text": "Edited."}}).ok
    default = chat_default()
    real = pt.wire_problems
    monkeypatch.setattr(pt, "wire_problems", lambda role, text: [] if text == default else ["the task trigger phrase is missing"])
    store._changed()
    built = store.build("chat")
    assert built.text == default and built.fell_back
    assert any("lost words the program needs" in w for w in built.warnings)
    monkeypatch.setattr(pt, "wire_problems", real)


def test_the_size_limits_and_the_soft_warning(tmp_path, cloud):
    store = make(tmp_path, cloud)
    assert not store.save("chat-default", {"extra": {"text": "x" * 4001}}).ok
    res = store.save("chat-default", {f"x-block{i}": {"text": "y" * 3900, "title": "t"} for i in range(6)})
    assert not res.ok and "20000" in res.message                      # six blocks add up to more than the total limit
    assert not any("is long" in w for w in store.build("chat").warnings)
    ok = store.save("chat-default", {"extra": {"text": "z" * 4000}, "x-second": {"text": "w" * 1000, "title": "t"}})
    assert ok.ok
    built = store.build("chat")
    assert len(built.text) > ps.SOFT_WARN_CHARS
    assert any("is long" in w for w in built.warnings)               # over about 6000 characters: soft warning only
    assert "z" * 4000 in built.text


def test_unknown_placeholders_are_a_warning_not_a_refusal(tmp_path, cloud):
    store = make(tmp_path, cloud)
    res = store.save("chat-default", {"extra": {"text": "Greet {USER_NAME} on {WEEKDAY}. Keep {USER_PROFILE} in mind."}})
    assert res.ok and any("WEEKDAY" in w for w in res.warnings)
    assert "{USER_NAME}" not in store.build("chat").text


def test_placeholders_are_filled_and_follow_a_change_of_the_settings(tmp_path, cloud, monkeypatch):
    store = make(tmp_path, cloud)
    store.save("chat-default", {"extra": {"text": "Address the user as {USER_NAME}."}})
    monkeypatch.setattr(config, "USER_NAME", "Maharaj")
    assert "Address the user as Maharaj." in store.build("chat").text
    monkeypatch.setattr(config, "USER_NAME", "Raja")
    assert "Address the user as Raja." in store.build("chat").text    # the memo notices the changed placeholder


# ------------------------------------------------------------------ keys never leave
def test_a_saved_api_key_in_the_text_is_refused(tmp_path, cloud, saved_key):
    store = make(tmp_path, cloud)
    store.sync()
    res = store.save("chat-default", {"extra": {"text": f"Use this key: {saved_key}"}})
    assert not res.ok and "API key" in res.message
    assert saved_key not in json.dumps(cloud.tree)
    assert saved_key not in res.message


def test_a_key_shaped_text_from_another_device_is_refused_too(tmp_path, cloud):
    store = make(tmp_path, cloud)
    res = store.save("chat-default", {"extra": {"text": "key AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}})
    assert not res.ok


def test_a_key_in_the_text_is_not_uploaded_even_if_the_key_was_saved_after_queueing(tmp_path, cloud):
    store = make(tmp_path, cloud, on=False)             # switch off: saves wait
    assert store.save("chat-default", {"extra": {"text": "The code word is " + GROQ[:4] + "-" + GROQ[4:]}}).queued
    st = settings_store.get_store()
    assert st.set("GROQ_API_KEY", GROQ[:4] + "-" + GROQ[4:]).ok   # now that very text is a saved secret
    try:
        store.flags["on"] = True
        store.flush()
        assert stored(cloud, "chat-default") is None or "code word" not in json.dumps(cloud.tree)
        assert store.status()["pending"] == 1             # kept, not sent
        assert "was not sent" in store.status()["last_error"]
    finally:
        st.delete("GROQ_API_KEY")


def test_a_cloud_block_holding_a_key_is_ignored(tmp_path, cloud, saved_key):
    store = make(tmp_path, cloud)
    store.sync()
    node = stored(cloud, "chat-default")
    node["blocks"]["extra"]["text"] = f"my key {saved_key}"
    cloud.set(node_path("chat-default"), node)
    store.refresh()
    assert saved_key not in store.build("chat").text
    assert saved_key not in (store._path().read_text(encoding="utf-8") if store._path().exists() else "")


# ------------------------------------------------------------------ switch off / no Firebase / offline
def test_switch_off_sends_and_reads_nothing(tmp_path, cloud):
    store = make(tmp_path, cloud, on=False)
    assert store.refresh()["state"] == "off"
    assert store.sync()["state"] == "off"
    assert store.seed() == {"written": 0, "error": ""}
    assert store.history("chat-default")["ok"] is False
    res = store.save("chat-default", {"identity": {"text": "Local only."}})
    assert res.ok and res.queued and "off" in res.message
    assert store.flush()["state"] == "off"
    assert cloud.calls == [] and cloud.tree == {}
    assert "Local only." in store.build("chat").text                   # still works on this computer


def test_no_firebase_configured_does_nothing_and_raises_nothing(tmp_path):
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError("the cloud must not be touched")
    store = make(tmp_path, Exploding(), configured=False)
    assert store.sync()["state"] == "no_firebase"
    res = store.save("chat-default", {"identity": {"text": "Edit."}})
    assert res.ok and res.queued and "no Firebase" in res.message
    assert store.set_active("chat", "chat-default").ok


def test_offline_start_uses_the_cache_then_the_shipped_text(tmp_path, cloud):
    first = make(tmp_path, cloud)
    first.sync()
    assert first.save("chat-default", {"identity": {"text": "Cached persona."}}).ok
    cloud.online = False
    again = make(tmp_path, cloud)                      # same cache file, no network
    assert "Cached persona." in again.build("chat").text and again.build("chat").source == "cache"
    assert again.refresh()["last_error"]               # reported, not raised
    assert "Cached persona." in again.build("chat").text
    fresh = make(tmp_path, cloud, name="z")            # never synced: shipped text
    assert fresh.build("chat").text == chat_default()


def test_a_damaged_cache_file_is_set_aside_and_the_shipped_text_is_used(tmp_path):
    (tmp_path / "cache-a.json").write_text("{not json", encoding="utf-8")
    store = make(tmp_path)
    assert store.build("chat").text == chat_default()
    assert list(tmp_path.glob("cache-a.json.corrupt-*"))


def test_the_prompt_is_built_without_any_network_call_after_the_first_load(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    store.save("chat-default", {"extra": {"text": "Be brief."}})
    calls = len(cloud.calls)
    cloud.online = False
    for _ in range(5):
        for role in pt.ROLES:
            store.build(role)
            store.text(role)
    store.list_prompts()
    store.get_prompt("chat-default")
    store.preview("chat-default", {"extra": {"text": "Another try."}})
    assert len(cloud.calls) == calls


def test_building_is_memoised_until_something_changes(tmp_path, cloud):
    store = make(tmp_path, cloud)
    a = store.build("chat")
    assert store.build("chat") is a
    store.save("chat-default", {"extra": {"text": "Hello."}})
    assert store.build("chat") is not a


# ------------------------------------------------------------------ concurrent saves
def test_a_concurrent_save_is_refused_and_the_other_value_survives(tmp_path, cloud):
    a, b = make(tmp_path, cloud, name="a"), make(tmp_path, cloud, name="b")
    a.sync()
    b.sync()
    assert a.save("chat-default", {"identity": {"text": "From computer A."}}).ok
    res = b.save("chat-default", {"identity": {"text": "From computer B."}})   # B still has version 0
    assert not res.ok and res.conflict
    assert stored(cloud, "chat-default")["blocks"]["identity"]["text"] == "From computer A."
    assert stored(cloud, "chat-default")["version"] == 1
    assert "From computer A." in b.build("chat").text                 # B reloaded what is there now
    assert b.save("chat-default", {"identity": {"text": "From computer B."}}).ok   # and may now try again
    assert stored(cloud, "chat-default")["version"] == 2


def test_a_save_that_names_a_stale_version_is_refused_without_touching_the_cloud(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    store.save("chat-default", {"extra": {"text": "one"}})
    calls = len(cloud.calls)
    res = store.save("chat-default", {"extra": {"text": "two"}}, base_version=0)
    assert not res.ok and res.conflict and len(cloud.calls) == calls


def test_every_save_is_a_version_and_the_rules_shape_is_followed(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    store.save("chat-default", {"identity": {"text": "v1"}}, note="first")
    store.save("chat-default", {"identity": {"text": "v2"}})
    hist = store.history("chat-default")
    assert hist["ok"] and [v["n"] for v in hist["versions"]] == [2, 1]
    assert hist["versions"][1]["note"] == "first"
    rec = cloud.get(f"{ps.VERSIONS_ROOT}/chat-default/1")
    assert rec["blocks"]["identity"]["text"] == "v1" and isinstance(rec["saved_at"], int)


def test_saving_the_same_thing_twice_makes_no_new_version(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    store.save("chat-default", {"extra": {"text": "same"}})
    again = store.save("chat-default", {"extra": {"text": "same"}})
    assert again.ok and again.message == "Nothing changed." and again.version == 1


# ------------------------------------------------------------------ pruning
@pytest.mark.parametrize("keep", [5, 8, 30])
def test_pruning_keeps_exactly_the_set_number_of_versions(tmp_path, cloud, keep):
    store = make(tmp_path, cloud, keep=keep)
    store.sync()
    for i in range(keep + 7):
        assert store.save("chat-default", {"extra": {"text": f"edit {i}"}}).ok
    nums = sorted(int(k) for k in cloud.keys(f"{ps.VERSIONS_ROOT}/chat-default"))
    assert len(nums) == keep
    assert nums[-1] == keep + 7 and nums[0] == 8                       # the newest ones survive


def test_lowering_the_setting_prunes_on_the_next_save(tmp_path, cloud):
    store = make(tmp_path, cloud, keep=30)
    store.sync()
    for i in range(10):
        store.save("chat-default", {"extra": {"text": f"edit {i}"}})
    store.flags["keep"] = 5
    store.save("chat-default", {"extra": {"text": "last"}})
    assert len(cloud.keys(f"{ps.VERSIONS_ROOT}/chat-default")) == 5


def test_the_default_keep_comes_from_the_setting(tmp_path):
    assert ps._default_keep() == 30
    st = settings_store.get_store()
    assert st.set("PROMPT_VERSIONS_KEEP", "7").ok
    try:
        assert ps._default_keep() == 7
    finally:
        st.delete("PROMPT_VERSIONS_KEEP")


# ------------------------------------------------------------------ offline saves: keep mine / take theirs
def test_an_offline_save_is_the_working_copy_at_once_and_uploads_later(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    cloud.online = False
    res = store.save("chat-default", {"identity": {"text": "Written on the train."}})
    assert res.ok and res.queued and "will be sent" in res.message
    assert "Written on the train." in store.build("chat").text
    assert store.status()["pending"] == 1 and store.status()["state"] == "offline"
    cloud.online = True
    assert store.flush()["pending"] == 0
    node = stored(cloud, "chat-default")
    assert node["blocks"]["identity"]["text"] == "Written on the train." and node["version"] == 1
    assert cloud.get(f"{ps.VERSIONS_ROOT}/chat-default/1") is not None


def test_an_offline_save_survives_a_restart(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    cloud.online = False
    store.save("chat-default", {"identity": {"text": "Not lost."}})
    again = make(tmp_path, cloud)                                       # the app was closed and opened again
    assert "Not lost." in again.build("chat").text and again.status()["pending"] == 1
    cloud.online = True
    again.sync()
    assert stored(cloud, "chat-default")["blocks"]["identity"]["text"] == "Not lost."


def test_several_offline_saves_of_one_prompt_upload_as_one_version(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    cloud.online = False
    store.save("chat-default", {"identity": {"text": "one"}})
    store.save("chat-default", {"behaviour": {"text": "two"}})
    cloud.online = True
    store.flush()
    node = stored(cloud, "chat-default")
    assert node["version"] == 1 and node["blocks"]["identity"]["text"] == "one" and node["blocks"]["behaviour"]["text"] == "two"


def _conflicted(tmp_path, cloud):
    mine = make(tmp_path, cloud, name="a")
    theirs = make(tmp_path, cloud, name="b")
    mine.sync()
    theirs.sync()
    cloud.online = False
    mine.save("chat-default", {"identity": {"text": "MINE (offline)."}})
    cloud.online = True
    assert theirs.save("chat-default", {"identity": {"text": "THEIRS (online)."}}).ok
    return mine, theirs


def test_an_offline_save_that_conflicts_is_not_uploaded_and_is_offered_keep_mine_or_take_theirs(tmp_path, cloud):
    mine, _ = _conflicted(tmp_path, cloud)
    st = mine.sync()
    assert st["state"] == "conflict" and st["conflicts"] == ["chat-default"]
    assert stored(cloud, "chat-default")["blocks"]["identity"]["text"] == "THEIRS (online)."   # not overwritten
    assert "MINE (offline)." in mine.build("chat").text                  # my copy is still what I use
    assert mine.get_prompt("chat-default")["conflict"] is True
    blocked = mine.save("chat-default", {"extra": {"text": "more"}})
    assert not blocked.ok and blocked.conflict                           # settle it first


def test_reading_alone_is_enough_to_see_the_conflict(tmp_path, cloud):
    mine, _ = _conflicted(tmp_path, cloud)
    st = mine.refresh()                                  # no upload attempt: only a read
    assert st["state"] == "conflict" and st["conflicts"] == ["chat-default"]
    assert not any(c[0] == "transaction" for c in cloud.calls[-3:])


def test_take_theirs_drops_my_copy(tmp_path, cloud):
    mine, _ = _conflicted(tmp_path, cloud)
    mine.sync()
    res = mine.resolve_conflict("chat-default", "theirs")
    assert res.ok
    assert "THEIRS (online)." in mine.build("chat").text and "MINE" not in mine.build("chat").text
    assert mine.status()["state"] == "synced" and mine.status()["pending"] == 0
    assert stored(cloud, "chat-default")["version"] == 1


def test_keep_mine_uploads_my_copy_as_the_next_version(tmp_path, cloud):
    mine, _ = _conflicted(tmp_path, cloud)
    mine.sync()
    res = mine.resolve_conflict("chat-default", "mine")
    assert res.ok and not res.queued
    node = stored(cloud, "chat-default")
    assert node["blocks"]["identity"]["text"] == "MINE (offline)." and node["version"] == 2
    assert mine.status()["state"] == "synced"
    assert not mine.resolve_conflict("chat-default", "mine").ok          # nothing left to settle


def test_a_conflict_is_remembered_across_a_restart(tmp_path, cloud):
    mine, _ = _conflicted(tmp_path, cloud)
    mine.sync()
    again = make(tmp_path, cloud, name="a")
    assert again.status()["state"] == "conflict"
    assert again.resolve_conflict("chat-default", "theirs").ok


# ------------------------------------------------------------------ delete, active, custom prompts
def test_delete_is_refused_for_a_builtin_prompt(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    res = store.delete("chat-default")
    assert not res.ok and "built-in" in res.message
    assert stored(cloud, "chat-default") is not None


def test_a_custom_prompt_can_be_made_used_and_deleted_but_not_while_in_use(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    made = store.save_as_new("chat", "Casual chat", {"identity": {"text": "You are relaxed."}}, "A looser tone")
    assert made.ok and made.version == 1
    pid = made.prompt_id
    assert ps._ID_RE.match(pid) and pid not in ps.BUILTIN_IDS.values()
    assert store.active_id("chat") == "chat-default"                     # making one does not switch to it
    assert "You are relaxed." not in store.build("chat").text
    assert store.set_active("chat", pid).ok
    assert "You are relaxed." in store.build("chat").text and store.build("chat").prompt_id == pid
    assert cloud.get(f"{ps.ACTIVE_ROOT}/chat") == pid
    refused = store.delete(pid)
    assert not refused.ok and "in use" in refused.message
    assert stored(cloud, pid) is not None
    assert store.set_active("chat", "chat-default").ok
    assert store.delete(pid).ok
    assert stored(cloud, pid) is None and cloud.get(f"{ps.VERSIONS_ROOT}/{pid}") is None
    assert [p["id"] for p in store.list_prompts() if not p["builtin"]] == []


def test_the_active_prompt_must_belong_to_the_role(tmp_path, cloud):
    store = make(tmp_path, cloud)
    assert not store.set_active("chat", "audit-default").ok
    assert not store.set_active("chat", "nope").ok
    assert not store.set_active("nonsense", "chat-default").ok


def test_the_active_choice_made_offline_is_sent_later(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    made = store.save_as_new("chat", "Second", {"extra": {"text": "Be funny."}})
    cloud.online = False
    res = store.set_active("chat", made.prompt_id)
    assert res.ok and res.queued
    assert "Be funny." in store.build("chat").text
    cloud.online = True
    store.flush()
    assert cloud.get(f"{ps.ACTIVE_ROOT}/chat") == made.prompt_id


def test_a_missing_active_prompt_falls_back_to_the_builtin(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    cloud.set(f"{ps.ACTIVE_ROOT}/chat", "deleted-long-ago")
    store.refresh()
    assert store.active_id("chat") == "chat-default"
    assert store.build("chat").text == chat_default()


def test_a_custom_prompt_made_offline_is_created_later(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    cloud.online = False
    made = store.save_as_new("audit", "Gentle audit", {"tone": {"text": "Be gentle."}})
    assert made.ok and made.queued
    assert any(p["id"] == made.prompt_id for p in store.list_prompts())
    cloud.online = True
    store.sync()
    assert stored(cloud, made.prompt_id)["name"] == "Gentle audit"
    assert store.status()["pending"] == 0


def test_save_as_new_checks_its_inputs(tmp_path, cloud):
    store = make(tmp_path, cloud)
    assert not store.save_as_new("chat", "   ").ok
    assert not store.save_as_new("nonsense", "x").ok
    assert not store.save_as_new("chat", "x", {"extra": {"text": PHRASE}}).ok
    assert not store.save_as_new("chat", "x", from_prompt="audit-default").ok
    assert [p for p in store.list_prompts() if not p["builtin"]] == []   # nothing half-made is left behind


# ------------------------------------------------------------------ blocks: extras, switching, order
def test_extra_and_custom_blocks_go_at_the_insert_point_in_order(tmp_path, cloud):
    store = make(tmp_path, cloud)
    bid = store.new_block_id()
    assert bid.startswith("x-")
    res = store.save("chat-default", {
        "extra": {"text": "EXTRA-LAST", "order": 5},
        bid: {"text": "CUSTOM-FIRST", "title": "Mine", "order": 1},
    })
    assert res.ok
    text = store.build("chat").text
    assert text.index("CUSTOM-FIRST") < text.index("EXTRA-LAST")
    confirm = pt.load_shipped("chat")[-1].text
    assert text.index("EXTRA-LAST") < text.index(confirm[:30])           # the locked wire text is read last
    assert text.endswith(confirm.strip("\n"))
    rows = [b for b in store.get_prompt("chat-default")["blocks"] if b["kind"] in ("extra", "custom")]
    assert [b["id"] for b in rows] == [bid, "extra"] and rows[0]["title"] == "Mine"


def test_a_block_can_be_switched_off_and_on_without_losing_its_text(tmp_path, cloud):
    store = make(tmp_path, cloud)
    assert store.save("chat-default", {"identity": {"text": "Persona X."}}).ok
    assert store.save("chat-default", {"identity": {"enabled": False}}).ok
    off = store.build("chat").text
    assert "Persona X." not in off and PHRASE in off.lower()
    assert store.get_prompt("chat-default")["blocks"][0]["text"] == "Persona X."
    assert store.save("chat-default", {"identity": {"enabled": True}}).ok
    assert "Persona X." in store.build("chat").text


def test_a_shipped_block_can_be_switched_off_before_it_was_ever_edited(tmp_path, cloud):
    store = make(tmp_path, cloud)
    assert store.save("chat-default", {"scale": {"enabled": False}}).ok
    shipped_scale = pt.load_shipped("chat")[4].text
    assert shipped_scale not in store.build("chat").text
    assert store.get_prompt("chat-default")["blocks"][4]["text"] == shipped_scale


def test_locked_blocks_cannot_be_switched_off_or_removed(tmp_path, cloud):
    store = make(tmp_path, cloud)
    assert not store.save("chat-default", {"confirmation": {"enabled": False}}).ok
    assert not store.save("chat-default", {"identity": None}).ok            # a shipped block cannot be removed, only reset
    assert not store.save("chat-default", {"made_up": {"text": "x"}}).ok


def test_get_prompt_lists_blocks_in_template_order_with_locks(tmp_path, cloud):
    store = make(tmp_path, cloud)
    data = store.get_prompt("task-extraction-default")
    ids = [b["id"] for b in data["blocks"]]
    assert ids == ["identity", "input_context", "dedup_rules", "dedup_reply", "translation", "analysis", "categorization",
                   "importance_rules", "urgency_rules", "extra", "output_format"]
    locked = {b["id"] for b in data["blocks"] if b["locked"]}
    assert locked == {"input_context", "dedup_reply", "categorization", "output_format"}
    assert all(b["default_text"] for b in data["blocks"] if b["id"] != "extra")
    assert store.get_prompt("nope")["ok"] is False


# ------------------------------------------------------------------ reset and restore
def test_reset_block_returns_the_shipped_text_and_is_itself_a_version(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    store.save("chat-default", {"identity": {"text": "Changed."}})
    res = store.reset_block("chat-default", "identity")
    assert res.ok and res.version == 2
    assert store.build("chat").text == chat_default()
    assert store.get_prompt("chat-default")["blocks"][0]["modified"] is False


def test_reset_prompt_returns_everything_to_the_shipped_text(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    bid = store.new_block_id()
    store.save("chat-default", {"identity": {"text": "A"}, "scale": {"enabled": False}, "extra": {"text": "B"}, bid: {"text": "C"}})
    assert store.list_prompts()[0]["modified"] is True
    res = store.reset_prompt("chat-default")
    assert res.ok
    assert store.build("chat").text == chat_default()
    node = stored(cloud, "chat-default")
    assert bid not in node["blocks"] and node["blocks"]["extra"]["text"] == ""
    assert store.list_prompts()[0]["modified"] is False
    assert not store.reset_block("chat-default", "trigger_rule").ok


def test_restore_makes_an_old_version_the_text_again_as_a_new_version(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    store.save("chat-default", {"identity": {"text": "First."}})
    store.save("chat-default", {"identity": {"text": "Second."}, "extra": {"text": "added later"}})
    res = store.restore("chat-default", 1)
    assert res.ok and res.version == 3
    built = store.build("chat").text
    assert "First." in built and "Second." not in built and "added later" not in built
    assert len(store.history("chat-default")["versions"]) == 3            # the history was kept
    assert not store.restore("chat-default", 99).ok
    store.flags["on"] = False
    assert not store.restore("chat-default", 1).ok


def test_restore_ignores_a_bad_block_in_an_old_version(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    store.save("chat-default", {"identity": {"text": "Fine."}})
    rec = cloud.get(f"{ps.VERSIONS_ROOT}/chat-default/1")
    rec["blocks"]["identity"]["text"] = f"Poisoned {PHRASE}"
    cloud.set(f"{ps.VERSIONS_ROOT}/chat-default/1", rec)
    res = store.restore("chat-default", 1)
    assert res.ok and "Poisoned" not in store.build("chat").text


# ------------------------------------------------------------------ preview and validate (for the editor)
def test_preview_shows_unsaved_changes_and_saves_nothing(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    calls = len(cloud.calls)
    out = store.preview("chat-default", {"extra": {"text": "Try this on."}})
    assert out["ok"] and "Try this on." in out["text"] and out["tokens"] == (len(out["text"]) + 3) // 4
    assert "Try this on." not in store.build("chat").text
    assert len(cloud.calls) == calls                                    # no network for a preview ...
    assert stored(cloud, "chat-default")["version"] == 0                # ... and nothing was saved
    bad = store.preview("chat-default", {"extra": {"text": PHRASE}})
    assert not bad["ok"] and bad["errors"]


def test_validate_is_the_live_guard_check(tmp_path, cloud):
    store = make(tmp_path, cloud)
    assert store.validate("chat-default", {"extra": {"text": "fine"}})["ok"]
    out = store.validate("chat-default", {"extra": {"text": PHRASE}})
    assert not out["ok"] and out["errors"]
    assert store.validate("chat-default", {"extra": {"text": "{ODD}"}})["warnings"]


def test_screen_scan_warns_when_the_safety_lines_are_gone(tmp_path, cloud):
    store = make(tmp_path, cloud)
    shipped = {b.id: b.text for b in pt.load_shipped("screen_scan")}
    rules_without = shipped["rules"].replace("Do NOT repeat passwords", "Repeat anything")
    store.save("screen-scan-default", {"rules": {"text": rules_without}})
    assert any("passwords" in w for w in store.build("screen_scan").warnings)


# ------------------------------------------------------------------ what comes back from Firebase is untrusted
def test_odd_data_from_firebase_never_raises(tmp_path, cloud):
    store = make(tmp_path, cloud)
    cloud.set(ps.PROMPTS_ROOT + "/chat-default", {"role": "audit", "version": 3, "blocks": {}})        # wrong role for the id
    cloud.set(ps.PROMPTS_ROOT + "/weird", "a string")
    cloud.set(ps.PROMPTS_ROOT + "/norole", {"name": "x", "version": 1, "role": "mystery"})
    cloud.set(ps.PROMPTS_ROOT + "/-Good1", {"name": "Fine", "role": "chat", "version": "2",
                                             "blocks": {"extra": {"text": 5}, "x-ok": {"text": "kept", "enabled": "yes"}}})
    cloud.set(ps.ACTIVE_ROOT + "/chat", ["not", "a", "string"])
    cloud.set(ps.ACTIVE_ROOT + "/bogusrole", "chat-default")
    store.refresh()
    assert store.build("chat").text == chat_default()
    ids = [p["id"] for p in store.list_prompts()]
    assert "-Good1" in ids and "weird" not in ids and "norole" not in ids
    good = [p for p in store.list_prompts() if p["id"] == "-Good1"][0]
    assert good["version"] == 2
    assert store.status()["warnings"]


def test_a_failing_cloud_never_raises_from_any_public_method(tmp_path):
    class Broken(MemoryTreeCloud):
        def get(self, path):
            raise RuntimeError("boom")
        keys = set = delete = transaction = get
    store = make(tmp_path, Broken())
    store.sync()
    assert store.save("chat-default", {"extra": {"text": "x"}}).queued
    assert store.restore("chat-default", 1).ok is False
    assert store.delete("nope").ok is False
    assert store.history("chat-default")["ok"] is False
    assert store.set_active("chat", "chat-default").ok
    assert store.flush()["last_error"]
    assert store.build("chat").text


# ------------------------------------------------------------------ the data matches the Firebase rules
def _allowed(rule_node):
    return {k for k in rule_node if not k.startswith(("$", "."))}


def _check_against_rules(value, rule_node, path="", bad=None):
    bad = [] if bad is None else bad
    if not isinstance(value, dict):
        return bad
    allowed = _allowed(rule_node)
    wild = [k for k in rule_node if k.startswith("$") and k != "$other"]
    for k, v in value.items():
        if k in allowed:
            _check_against_rules(v, rule_node[k], f"{path}/{k}", bad)
        elif wild:
            _check_against_rules(v, rule_node[wild[0]], f"{path}/{k}", bad)
        else:
            bad.append(f"{path}/{k}")
    return bad


def test_everything_written_fits_the_firebase_rules(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    bid = store.new_block_id()
    made = store.save_as_new("chat", "Another", {bid: {"text": "hi", "title": "T", "order": 2}})
    store.save("chat-default", {"identity": {"text": "x"}, "extra": {"text": "y", "order": 3}}, note="n")
    store.set_active("chat", made.prompt_id)
    cfg = cloud.get(ps.CONFIG_ROOT)
    assert _check_against_rules(cfg, RULES["rules"]["ksm"]["config"]) == []
    versions = cloud.get(ps.VERSIONS_ROOT)
    assert _check_against_rules(versions, RULES["rules"]["ksm"]["config_versions"]["prompts"]) == []
    for pid, node in cfg["prompts"].items():
        assert isinstance(node["updated_at"], int)                      # the server clock was filled in
        for bid_, blk in node["blocks"].items():
            assert bid_ not in pt.LOCKED_BLOCK_IDS and len(blk["text"]) <= 4000
        assert PHRASE not in json.dumps(node).lower()


def test_no_locked_text_is_ever_stored(tmp_path, cloud):
    store = make(tmp_path, cloud)
    store.sync()
    store.save("task-extraction-default", {"extra": {"text": "x"}})
    blob = json.dumps(cloud.tree)
    for role in pt.ROLES:
        for b in pt.load_shipped(role):
            if b.locked:
                assert b.text not in blob
    cache = store._path().read_text(encoding="utf-8")
    for role in pt.ROLES:
        for b in pt.load_shipped(role):
            if b.locked:
                assert b.text not in cache


# ------------------------------------------------------------------ the cloud interface itself
@pytest.mark.parametrize("make_cloud", [MemoryCloud, MemoryTreeCloud])
def test_transaction_writes_only_when_the_function_agrees(make_cloud):
    c = make_cloud()
    committed, value = c.transaction("ksm/config/x", lambda cur: {"n": 1} if cur is None else (_ for _ in ()).throw(TransactionAbort()))
    assert committed and value == {"n": 1}
    committed, value = c.transaction("ksm/config/x", lambda cur: (_ for _ in ()).throw(TransactionAbort()))
    assert committed is False and value == {"n": 1}
    assert c.get("ksm/config/x") == {"n": 1}


def test_the_tree_fake_behaves_like_a_nested_database():
    c = MemoryTreeCloud(clock=lambda: 42)
    c.set("ksm/config/a/b", {"v": 1, "t": cloud_sync.SERVER_TIME})
    assert c.get("ksm/config/a") == {"b": {"v": 1, "t": 42}}
    assert c.get("ksm/config")["a"]["b"]["v"] == 1
    assert c.keys("ksm/config/a") == ["b"]
    c.set("ksm/config/a", {"c": 2})                                     # a parent write replaces everything below
    assert c.get("ksm/config/a/b") is None and c.get("ksm/config/a") == {"c": 2}
    c.delete("ksm/config/a/c")
    assert c.get("ksm/config") is None                                  # empty parents vanish, as in Firebase
    c.online = False
    with pytest.raises(CloudError):
        c.get("ksm/config")


def test_firebase_store_transaction_goes_through_the_sdk_and_maps_errors():
    class Ref:
        def __init__(self, behave):
            self.behave = behave

        def transaction(self, fn):
            return self.behave(fn)
    store = cloud_sync.FirebaseCloudStore(ref_factory=lambda path: Ref(lambda fn: fn({"v": 1})))
    assert store.transaction("ksm/x", lambda cur: {"v": cur["v"] + 1}) == (True, {"v": 2})
    abort = cloud_sync.FirebaseCloudStore(ref_factory=lambda path: Ref(lambda fn: fn({"v": 1})))
    assert abort.transaction("ksm/x", lambda cur: (_ for _ in ()).throw(TransactionAbort())) == (False, None)
    broken = cloud_sync.FirebaseCloudStore(ref_factory=lambda path: Ref(lambda fn: (_ for _ in ()).throw(OSError("net down"))))
    with pytest.raises(CloudError):
        broken.transaction("ksm/x", lambda cur: cur)


def test_the_shared_instance_is_built_lazily_and_can_be_reset():
    ps.reset_store_for_tests()
    first = ps.get_store()
    assert ps.get_store() is first
    ps.reset_store_for_tests()
    assert ps.get_store() is not first
    ps.reset_store_for_tests()
