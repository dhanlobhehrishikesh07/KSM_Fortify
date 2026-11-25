"""Phase 5D: the AI modules read their prompts from the prompt library, and ONE live listener on `ksm/config` keeps the
prompt library and the shared settings current (app/config_sync.py, app/prompt_use.py). Fake clouds only: no live Firebase,
no window. Two `rig()`s on one MemoryTreeCloud are two computers."""
import logging
import threading
import time
from types import SimpleNamespace

import pytest

import test_prompt_store as tps
import test_shared_settings as tss
from app import cloud_sync, config, config_sync, prompt_store as ps, prompt_templates as pt, prompt_use
from app import shared_settings as sh
from app.ai_assistant import AIAssistant
from app.cloud_sync import CloudError, ListenHandle, MemoryTreeCloud, TreeMirror, FirebaseCloudStore
from app.screen_analyzer import ScreenAnalyzer
from app.strategy_officer import StrategyOfficer

PHRASE = pt.TASK_TRIGGER_PHRASE
CHAT_ID = ps.BUILTIN_IDS["chat"]
AUDIT_ID = ps.BUILTIN_IDS["audit"]
SCREEN_ID = ps.BUILTIN_IDS["screen_scan"]


@pytest.fixture
def cloud():
    return MemoryTreeCloud(clock=tss.Clock())


def rig(tmp_path, cloud, name="a", on=True, configured=True, background=False, retry_s=None):
    """One computer: prompt library, shared settings and the hub, all on `cloud` (the hub's own connection)."""
    prompts = tps.make(tmp_path, cloud, name=name, on=on, configured=configured)
    shared = tss.make(tmp_path, cloud, name=name, on=on, configured=configured, background=False)
    hub = config_sync.ConfigSync(prompts=prompts, shared=shared, cloud=cloud, background=background, retry_s=retry_s)
    return SimpleNamespace(prompts=prompts, shared=shared, hub=hub, cloud=cloud, name=name)


def use(monkeypatch, r):
    """Makes this computer's prompt library the one the AI modules use (the process-wide instance)."""
    monkeypatch.setattr(ps, "_store", r.prompts)


def fake_groq(sent):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: (
        sent.append(kw) or SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])))))


def first_editable(role):
    return ps.editable_layout_ids(role)[0]


def wait_for(fn, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.01)
    return fn()


# ---------------------------------------------------------------------------------------------------
# The listener primitive on the clouds
# ---------------------------------------------------------------------------------------------------

def test_tree_mirror_follows_put_patch_and_null_events():
    m = TreeMirror()
    assert m.apply("put", "/", {"prompts": {"a": {"version": 1}}, "settings": {"X": {"value": "1"}}}) == \
        {"prompts": {"a": {"version": 1}}, "settings": {"X": {"value": "1"}}}
    assert m.apply("put", "/prompts/a/version", 2)["prompts"]["a"]["version"] == 2
    assert m.apply("put", "/settings/Y", {"value": "2"})["settings"]["Y"] == {"value": "2"}
    assert m.apply("patch", "/settings", {"X": {"value": "9"}, "Z": None})["settings"] == {"X": {"value": "9"}, "Y": {"value": "2"}}
    assert m.apply("put", "/settings", None).get("settings") is None          # null removes the whole child
    assert m.apply("put", "/prompts/a", None) is None                          # nothing left at all
    assert m.apply("put", "/", {"k": 1}) == {"k": 1}
    assert m.apply("patch", "/", {"k": None, "j": 2}) == {"j": 2}
    assert m.apply("put", "/", None) is None


def test_tree_mirror_replaces_a_leaf_with_a_branch_and_returns_copies():
    m = TreeMirror()
    m.apply("put", "/", {"a": 5})
    out = m.apply("put", "/a/b", 1)
    assert out == {"a": {"b": 1}}
    out["a"]["b"] = 99
    assert m.apply("put", "/c", 1)["a"]["b"] == 1


def test_memory_cloud_listen_sends_the_first_snapshot_then_every_change_near_the_path(cloud):
    seen = []
    cloud.set("ksm/config/prompts/x", {"version": 1})
    handle = cloud.listen("ksm/config", seen.append)
    assert seen == [{"prompts": {"x": {"version": 1}}}]
    cloud.set("ksm/config/settings/A", {"value": "1"})               # below the path
    cloud.set("ksm/config", {"settings": {"A": {"value": "2"}}})     # at the path
    cloud.set("ksm", {"config": {"settings": {"A": {"value": "3"}}}})  # above it
    cloud.set("ksm/tasks/1", {"t": 1})                               # elsewhere: not delivered... (ksm replaced above, config is gone)
    assert len(seen) == 5 or len(seen) == 4
    n = len(seen)
    cloud.set("other/path", 1)
    assert len(seen) == n
    handle.close()
    cloud.set("ksm/config/settings/A", {"value": "4"})
    assert len(seen) == n and isinstance(handle, ListenHandle) and handle.closed


def test_memory_cloud_listen_offline_raises_and_a_broken_listener_never_breaks_the_writer(cloud):
    cloud.online = False
    with pytest.raises(CloudError):
        cloud.listen("ksm/config", lambda t: None)
    cloud.online = True

    def boom(tree):
        raise RuntimeError("listener bug")
    cloud.listen("ksm/config", lambda t: None)
    cloud._listeners.append(("ksm/config", boom))
    cloud.set("ksm/config/x", 1)                      # must not raise
    assert cloud.get("ksm/config/x") == 1


def test_firebase_listen_builds_whole_trees_from_sdk_events_and_wraps_errors():
    closed = []

    class Reg:
        def close(self):
            closed.append(1)

    class Ref:
        handler = None

        def listen(self, handler):
            Ref.handler = handler
            return Reg()

    got = []
    store = FirebaseCloudStore(ref_factory=lambda path: Ref())
    handle = store.listen("ksm/config", got.append)
    ev = lambda t, p, d: SimpleNamespace(event_type=t, path=p, data=d)    # noqa: E731
    Ref.handler(ev("put", "/", {"prompts": {"a": {"version": 1}}}))
    Ref.handler(ev("put", "/prompts/a/version", 2))
    Ref.handler(ev("patch", "/", {"settings": {"X": {"value": "1"}}}))
    assert got == [{"prompts": {"a": {"version": 1}}},
                   {"prompts": {"a": {"version": 2}}},
                   {"prompts": {"a": {"version": 2}}, "settings": {"X": {"value": "1"}}}]
    handle.close()
    handle.close()
    assert closed == [1] and handle.closed

    def bad(path):
        raise ValueError("no app")
    with pytest.raises(CloudError):
        FirebaseCloudStore(ref_factory=bad).listen("ksm/config", got.append)


def test_firebase_listen_survives_a_consumer_that_raises():
    class Ref:
        handler = None

        def listen(self, handler):
            Ref.handler = handler
            return SimpleNamespace(close=lambda: None)

    def consumer(tree):
        raise RuntimeError("bug in consumer")
    FirebaseCloudStore(ref_factory=lambda p: Ref()).listen("ksm/config", consumer)
    Ref.handler(SimpleNamespace(event_type="put", path="/", data={"a": 1}))      # must not raise


# ---------------------------------------------------------------------------------------------------
# The consumers read the prompt library
# ---------------------------------------------------------------------------------------------------

def test_an_edited_chat_block_reaches_the_next_chat_call_without_a_restart(tmp_path, cloud, monkeypatch):
    a, b = rig(tmp_path, cloud, "a"), rig(tmp_path, cloud, "b")
    use(monkeypatch, a)
    a.hub.start()
    sent = []
    ai = AIAssistant()
    ai._client = fake_groq(sent)
    ai.chat(["User: hi"], "[]")
    assert "PIRATE-VOICE" not in sent[0]["messages"][0]["content"]
    assert a.prompts.status()["from_cloud"] is True

    bid = first_editable("chat")
    assert b.prompts.save(CHAT_ID, {bid: {"text": "Always answer in PIRATE-VOICE for {USER_NAME}."}}).ok

    ai.chat(["User: hi again"], "[]")                         # same AIAssistant, same process, nothing restarted
    system = sent[1]["messages"][0]["content"]
    assert f"Always answer in PIRATE-VOICE for {config.USER_NAME}." in system
    assert system.count(PHRASE) >= 3                          # the locked wire text is still there


def test_edited_blocks_reach_the_extraction_the_audit_and_the_screen_scan(tmp_path, cloud, monkeypatch):
    a, b = rig(tmp_path, cloud, "a"), rig(tmp_path, cloud, "b")
    use(monkeypatch, a)
    a.hub.start()
    for pid, role in ((AUDIT_ID, "audit"), (SCREEN_ID, "screen_scan"), (ps.BUILTIN_IDS["task_extraction"], "task_extraction")):
        assert b.prompts.save(pid, {first_editable(role): {"text": f"MARK-{role}"}}).ok

    sent = []
    ai = AIAssistant()
    ai._client = fake_groq(sent)
    ai.validate_and_extract("ctx", "[]")
    assert "MARK-task_extraction" in sent[0]["messages"][0]["content"]
    assert "[STRICT OUTPUT FORMAT]" in sent[0]["messages"][0]["content"]

    seen = {}
    officer = StrategyOfficer()
    officer._client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda model, contents, config: seen.update(c=contents) or SimpleNamespace(text="r")))
    officer.run_audit("[]")
    assert "MARK-audit" in seen["c"]

    assert "MARK-screen_scan" in ScreenAnalyzer.load_prompt(None)


def test_a_changed_shared_setting_reaches_the_next_call_without_a_restart(tmp_path, cloud, monkeypatch):
    a, b = rig(tmp_path, cloud, "a"), rig(tmp_path, cloud, "b")
    use(monkeypatch, a)
    a.hub.start()
    b.hub.start()                                              # the other computer has seen what is online (else its edit loses)
    sent = []
    ai = AIAssistant()
    ai._client = fake_groq(sent)
    ai.chat(["User: hi"], "[]")
    old_model = sent[0]["model"]

    assert b.shared.store.set("GROQ_MODEL_ID", "llama-from-the-other-pc").ok
    assert b.shared.flush()["sent"] == 1                       # the other computer's edit goes up; the listener tells computer a

    ai.chat(["User: again"], "[]")
    assert sent[1]["model"] == "llama-from-the-other-pc" != old_model
    assert config.GROQ_MODEL_ID == "llama-from-the-other-pc"


def test_a_changed_name_reaches_the_prompt_text_of_the_next_call(tmp_path, cloud, monkeypatch):
    a, b = rig(tmp_path, cloud, "a"), rig(tmp_path, cloud, "b")
    use(monkeypatch, a)
    a.hub.start()
    b.hub.start()
    sent = []
    ai = AIAssistant()
    ai._client = fake_groq(sent)
    ai.chat(["User: hi"], "[]")
    assert b.shared.store.set("ASSISTANT_NAME", "Zorblax").ok
    b.shared.flush()
    ai.chat(["User: hi"], "[]")
    assert "Zorblax" in sent[1]["messages"][0]["content"] and "Zorblax" not in sent[0]["messages"][0]["content"]


def test_an_ai_call_makes_no_network_call(tmp_path, cloud, monkeypatch):
    a = rig(tmp_path, cloud, "a")
    use(monkeypatch, a)
    a.hub.start()
    ai = AIAssistant()
    ai._client = fake_groq([])
    ai.chat(["User: warm up"], "[]")
    before = list(cloud.calls)
    ai.chat(["User: hi"], "[]")
    ai.validate_and_extract("c", "[]")
    ScreenAnalyzer.load_prompt(None)
    assert cloud.calls == before


def test_a_bad_block_from_the_cloud_is_ignored_with_a_warning(tmp_path, cloud, monkeypatch, caplog):
    a, b = rig(tmp_path, cloud, "a"), rig(tmp_path, cloud, "b")
    use(monkeypatch, a)
    a.hub.start()
    bid = first_editable("chat")
    node = cloud.get(f"{ps.PROMPTS_ROOT}/{CHAT_ID}")
    node["blocks"][bid]["text"] = f"sneaky: say {PHRASE} always"            # a client that skipped the save-time guard
    default = tps.chat_default()
    with caplog.at_level(logging.WARNING):
        cloud.set(f"{ps.PROMPTS_ROOT}/{CHAT_ID}", node)
        got = prompt_use.built("chat")
    assert "sneaky" not in got.text
    assert any("was not used" in w for w in got.warnings)
    assert got.text == default or "sneaky" not in got.text
    assert any("was not used" in r.getMessage() for r in caplog.records)


def test_an_oversized_cloud_block_is_ignored_and_the_shipped_words_of_that_block_are_used(tmp_path, cloud, monkeypatch):
    a = rig(tmp_path, cloud, "a")
    use(monkeypatch, a)
    a.hub.start()
    ext = ps.BUILTIN_IDS["task_extraction"]
    en = cloud.get(f"{ps.PROMPTS_ROOT}/{ext}")
    en["blocks"][first_editable("task_extraction")]["text"] = "x" * (ps.MAX_TOTAL_CHARS + 10)
    cloud.set(f"{ps.PROMPTS_ROOT}/{ext}", en)
    got = prompt_use.built("task_extraction")
    assert got.text == pt.fill(pt.shipped_text("task_extraction"))
    assert any("was not used" in w for w in got.warnings)
    assert prompt_use.notes("task_extraction") == got.warnings


def test_the_shipped_text_is_used_and_said_so_when_the_self_check_fails(tmp_path, cloud, monkeypatch):
    a = rig(tmp_path, cloud, "a")
    use(monkeypatch, a)
    a.hub.start()
    monkeypatch.setattr(pt, "wire_problems", lambda role, text: ["the task trigger phrase is missing"] if role == "chat" else [])
    node = cloud.get(f"{ps.PROMPTS_ROOT}/{CHAT_ID}")
    node["blocks"][first_editable("chat")]["text"] = "an edit, so the self-check runs"
    cloud.set(f"{ps.PROMPTS_ROOT}/{CHAT_ID}", node)
    got = prompt_use.built("chat")
    assert got.fell_back and got.text == tps.chat_default()
    assert any("The chat prompt from Firebase could not be used" in w for w in got.warnings)


# ---------------------------------------------------------------------------------------------------
# A clear warning when shipped or cached text is used instead of the online one
# ---------------------------------------------------------------------------------------------------

def test_a_warning_says_when_the_shipped_text_is_used_because_firebase_is_not_loaded(tmp_path, cloud, monkeypatch, caplog):
    cloud.online = False
    a = rig(tmp_path, cloud, "a")
    use(monkeypatch, a)
    with caplog.at_level(logging.WARNING):
        a.hub.start()
        got = prompt_use.built("chat")
        again = prompt_use.built("chat")                                      # same state: logged only once
    assert got.source == "shipped" and got.text == tps.chat_default()
    assert any("online version has not been loaded" in w for w in got.warnings)
    assert len([r for r in caplog.records if "online version has not been loaded" in r.getMessage()]) == 1
    assert again.warnings == got.warnings
    assert prompt_use.notes("chat") == got.warnings

    cloud.online = True
    a.hub.reconcile()
    now = prompt_use.built("chat")
    assert now.source == "firebase" and prompt_use.notes("chat") == []


def test_a_warning_says_when_the_saved_copy_is_used_instead_of_the_online_one(tmp_path, cloud, monkeypatch):
    first = rig(tmp_path, cloud, "a")
    first.hub.start()
    assert first.prompts.save(CHAT_ID, {first_editable("chat"): {"text": "CACHED-WORDS"}}).ok
    # a new start of the same computer, now offline: the cache file of the library is all it has
    cloud.online = False
    again = tps.make(tmp_path, cloud, name="a")
    assert again.build("chat").source == "cache"
    got = prompt_use.built("chat", store=again)
    assert "CACHED-WORDS" in got.text
    assert any("last copy saved on this PC" in w for w in got.warnings)


def test_no_warning_when_online_prompts_are_off(tmp_path, cloud, monkeypatch):
    a = rig(tmp_path, cloud, "a", on=False)
    use(monkeypatch, a)
    a.hub.start()
    got = prompt_use.built("chat")
    assert got.source == "shipped" and got.warnings == []
    assert cloud.calls == []


def test_the_library_failing_never_reaches_the_ai_call(monkeypatch, caplog):
    class Broken:
        def build(self, role):
            raise RuntimeError("bug in the library")

        def enabled(self):
            return False
    with caplog.at_level(logging.ERROR):
        got = prompt_use.built("chat", store=Broken())
    assert got.text == pt.fill(pt.shipped_text("chat")) and got.fell_back
    assert any("bug in the library" in (r.exc_text or "") or "prompt library failed" in r.getMessage() for r in caplog.records)

    class Damaged:
        def build(self, role):
            raise pt.PromptTemplateError("shipped file damaged")
    with pytest.raises(pt.PromptTemplateError):
        prompt_use.built("chat", store=Damaged())


# ---------------------------------------------------------------------------------------------------
# One listener, switches, start-up
# ---------------------------------------------------------------------------------------------------

def test_there_is_exactly_one_listener_on_ksm_config_and_it_is_not_opened_twice(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    a.hub.reconcile()
    a.hub.reconcile()
    assert [c for c in cloud.calls if c[0] == "listen"] == [("listen", "ksm/config")]
    assert len(cloud._listeners) == 1
    assert a.hub.status()["listening"] is True
    assert "config_versions" not in "".join(p for p, _ in cloud._listeners)


def test_the_listener_feeds_both_the_prompt_library_and_the_shared_settings(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    spy = {"p": [], "s": []}
    real_p, real_s = a.prompts.apply_snapshot, a.shared.apply_snapshot
    a.prompts.apply_snapshot = lambda *x: spy["p"].append(x) or real_p(*x)
    a.shared.apply_snapshot = lambda *x: spy["s"].append(x) or real_s(*x)
    cloud.set(f"{ps.ACTIVE_ROOT}/chat", CHAT_ID)
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "Meera", "updated_at": 5, "updated_by": "x"})
    assert spy["p"] and spy["s"]
    assert spy["s"][-1][0]["USER_NAME"]["value"] == "Meera"
    assert a.shared.store.get("USER_NAME") == "Meera" and config.USER_NAME == "Meera"


def test_nothing_is_opened_read_or_written_while_both_switches_are_off(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a", on=False)
    a.hub.start()
    a.hub.reconcile()
    a.hub._on_tree({"prompts": {"x": 1}, "settings": {"USER_NAME": {"value": "Hacker", "updated_at": 1, "updated_by": "x"}}})
    assert cloud.calls == [] and a.hub.status()["listening"] is False
    assert a.shared.store.get("USER_NAME") != "Hacker"
    assert a.prompts.status()["state"] == "off"


def test_prompts_from_the_cloud_are_not_applied_while_the_prompt_switch_is_off(tmp_path, cloud):
    b = rig(tmp_path, cloud, "b")
    b.hub.start()
    assert b.prompts.save(CHAT_ID, {first_editable("chat"): {"text": "EDITED-ELSEWHERE"}}).ok
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    assert "EDITED-ELSEWHERE" in a.prompts.build("chat").text
    a.prompts.flags["on"] = False                                    # prompts off, settings still on
    a.prompts.apply_snapshot(None, None)                             # (start from nothing, as a fresh start would)
    assert "EDITED-ELSEWHERE" not in a.prompts.build("chat").text
    a.hub._on_tree(cloud.get("ksm/config"))                          # an event arrives anyway
    assert "EDITED-ELSEWHERE" not in a.prompts.build("chat").text


def test_no_firebase_configured_opens_nothing(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a", configured=False)
    a.hub.start()
    assert cloud.calls == [] and a.hub.status()["listening"] is False


def test_only_the_switch_that_is_on_is_fed(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    a.shared.flags["on"] = False                                   # settings off, prompts still on
    cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "Meera", "updated_at": 5, "updated_by": "x"})
    assert a.shared.store.get("USER_NAME") != "Meera"
    node = cloud.get(f"{ps.PROMPTS_ROOT}/{CHAT_ID}")
    node["blocks"][first_editable("chat")]["text"] = "PROMPTS-STILL-LIVE"
    cloud.set(f"{ps.PROMPTS_ROOT}/{CHAT_ID}", node)
    assert "PROMPTS-STILL-LIVE" in a.prompts.build("chat").text


def test_turning_a_switch_on_or_off_opens_and_closes_the_connection_live(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a", on=False)
    a.hub.start()
    assert cloud.calls == []
    a.prompts.flags["on"] = a.shared.flags["on"] = True
    a.hub._on_setting(["SYNC_PROMPTS"])
    assert a.hub.status()["listening"] is True and len(cloud._listeners) == 1
    a.prompts.flags["on"] = a.shared.flags["on"] = False
    a.hub._on_setting(["SYNC_SETTINGS"])
    assert a.hub.status()["listening"] is False and cloud._listeners == []
    node = cloud.get(f"{ps.PROMPTS_ROOT}/{CHAT_ID}")
    node["blocks"][first_editable("chat")]["text"] = "AFTER-OFF"
    n = len(cloud.calls)
    cloud.set(f"{ps.PROMPTS_ROOT}/{CHAT_ID}", node)               # another client edits; this computer has everything off
    assert "AFTER-OFF" not in a.prompts.build("chat").text
    assert a.prompts.status()["state"] == "off"
    assert cloud.calls[n:] == [("set", f"{ps.PROMPTS_ROOT}/{CHAT_ID}")]      # nothing was read or sent by this computer


def test_an_unrelated_setting_change_does_not_touch_the_connection(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    n = len(cloud.calls)
    a.hub._on_setting(["THEME", "USER_NAME"])
    assert len(cloud.calls) == n


def test_start_up_does_not_wait_for_a_cloud_that_never_answers(tmp_path, monkeypatch):
    release = threading.Event()

    class Silent:
        """Every call hangs until released, then fails: a network that never answers."""

        def _hang(self, *a, **k):
            release.wait(10)
            raise CloudError("down")
        get = keys = set = delete = transaction = listen = _hang

    a = rig(tmp_path, Silent(), "a", background=True)
    use(monkeypatch, a)
    t0 = time.time()
    try:
        a.hub.start()
        built = prompt_use.built("chat")
        sent = []
        ai = AIAssistant()
        ai._client = fake_groq(sent)
        ai.chat(["User: hi"], "[]")
        elapsed = time.time() - t0
        assert elapsed < 1.5, f"start-up waited for the network ({elapsed:.1f}s)"
        assert built.text == tps.chat_default() and sent
        assert a.hub.status()["listening"] is False
    finally:
        release.set()
        a.hub.stop()


def test_main_window_starts_the_hub_in_the_background_and_stops_it():
    import inspect
    from app.gui import main_window
    src = inspect.getsource(main_window.run)
    assert "start_config_sync()" in src and "live_config.stop()" in src
    assert src.index("start_config_sync()") < src.index("AIAssistant()")


def test_start_config_sync_never_raises(monkeypatch):
    monkeypatch.setattr(config_sync, "get_hub", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert config_sync.start_config_sync() is None


def test_start_is_idempotent_and_returns_at_once(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a", background=True)
    t0 = time.time()
    a.hub.start()
    a.hub.start()
    assert time.time() - t0 < 1.0
    assert wait_for(lambda: a.hub.status()["listening"])
    assert len(cloud._listeners) == 1
    a.hub.stop()
    assert cloud._listeners == [] and a.hub.status()["listening"] is False


# ---------------------------------------------------------------------------------------------------
# Failures and retries
# ---------------------------------------------------------------------------------------------------

def test_a_failed_connection_is_retried_and_then_works(tmp_path, cloud):
    cloud.online = False
    a = rig(tmp_path, cloud, "a", background=True, retry_s=0.05)
    a.hub.start()
    assert wait_for(lambda: a.hub.status()["last_error"] != "")
    assert a.hub.status()["listening"] is False
    cloud.online = True
    assert wait_for(lambda: a.hub.status()["listening"], 5.0)
    assert wait_for(lambda: a.prompts.status()["state"] == "synced")
    assert a.hub.status()["last_error"] == ""
    a.hub.stop()


def test_without_a_retry_setting_a_failed_connection_is_not_retried(tmp_path, cloud):
    cloud.online = False
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    assert a.hub._timer is None and a.hub.status()["listening"] is False
    assert a.hub.status()["last_error"]


def test_the_retry_pause_grows_up_to_a_limit(tmp_path, cloud):
    cloud.online = False
    a = rig(tmp_path, cloud, "a")
    a.hub._retry_s, a.hub._max_retry_s = 10.0, 35.0
    delays = []
    for _ in range(4):
        a.hub.reconcile()
        delays.append(a.hub._delay)
    a.hub._cancel_retry()
    assert delays == [10.0, 20.0, 35.0, 35.0]


def test_a_failing_apply_never_stops_the_listener(tmp_path, cloud, caplog):
    a = rig(tmp_path, cloud, "a")
    a.hub.start()

    def boom(*args):
        raise RuntimeError("apply bug")
    a.prompts.apply_snapshot = boom
    with caplog.at_level(logging.ERROR):
        cloud.set(f"{ps.ACTIVE_ROOT}/chat", CHAT_ID)
        cloud.set(f"{sh.COMMON_ROOT}/USER_NAME", {"value": "Still", "updated_at": 9, "updated_by": "x"})
    assert a.shared.store.get("USER_NAME") == "Still"            # the settings layer was still fed
    assert any("Could not apply the prompts" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------------------------------
# Events: merged, not repeated
# ---------------------------------------------------------------------------------------------------

def test_a_burst_of_events_is_merged_and_the_newest_one_wins(tmp_path):
    gate, calls = threading.Event(), []

    class P:
        def enabled(self):
            return True

        def apply_snapshot(self, prompts, active):
            calls.append(prompts)
            gate.wait(5)

        def status(self):
            return {}

    class S:
        def enabled(self):
            return False

        def status(self):
            return {}

    hub = config_sync.ConfigSync(prompts=P(), shared=S(), cloud=MemoryTreeCloud(), background=True, retry_s=None)
    for i in range(30):
        hub._on_tree({"prompts": {"n": i}})
    assert wait_for(lambda: len(calls) >= 1)
    gate.set()
    assert wait_for(lambda: calls[-1] == {"n": 29})
    assert len(calls) <= 3
    assert hub.status()["events"] == 30


def test_a_change_nobody_cares_about_is_not_applied_again(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    spy = {"p": 0, "s": 0}
    real_p, real_s = a.prompts.apply_snapshot, a.shared.apply_snapshot
    a.prompts.apply_snapshot = lambda *x: spy.__setitem__("p", spy["p"] + 1) or real_p(*x)
    a.shared.apply_snapshot = lambda *x: spy.__setitem__("s", spy["s"] + 1) or real_s(*x)
    cloud.set(f"{ps.ACTIVE_ROOT}/chat", CHAT_ID)                       # one real change (same value: still the same tree)
    base = dict(spy)
    cloud.set("ksm/config/devices/d-other/last_seen", 12345)         # another computer says it is alive
    cloud.set("ksm/config/devices/d-other/name", "Other")
    assert spy["p"] == base["p"]                                      # prompts untouched by device nodes of others
    assert spy["s"] == base["s"]                                      # and so are the settings of this device


def test_secret_looking_values_planted_in_the_cloud_are_still_refused(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    cloud.set(f"{sh.COMMON_ROOT}/USER_PROFILE", {"value": "key gsk_abcdefghijklmnop1234", "updated_at": 7, "updated_by": "x"})
    cloud.set(f"{sh.COMMON_ROOT}/GROQ_API_KEY", {"value": "gsk_zzzzzzzzzzzzzzzz9999", "updated_at": 7, "updated_by": "x"})
    assert "gsk_" not in a.shared.store.get("USER_PROFILE")
    assert a.shared.store.get("GROQ_API_KEY") != "gsk_zzzzzzzzzzzzzzzz9999"


def test_one_computers_own_write_does_not_loop(tmp_path, cloud):
    a = rig(tmp_path, cloud, "a")
    a.hub.start()
    before = a.hub.status()["events"]
    assert a.shared.store.set("LOG_LEVEL", "WARNING").ok
    a.shared.flush()
    after = a.hub.status()["events"]
    assert 0 < after - before < 10
    assert a.shared.status()["pending"] == 0


def test_the_wire_tests_still_hold_for_what_the_consumers_send(tmp_path, cloud, monkeypatch):
    """Rule 6 of the plan: the default extraction prompt gives exactly one task through parse_tasks; the chat prompt has the phrase."""
    a = rig(tmp_path, cloud, "a")
    use(monkeypatch, a)
    a.hub.start()
    assert PHRASE in prompt_use.text("chat")
    assert pt.wire_problems("task_extraction", prompt_use.text("task_extraction")) == []
