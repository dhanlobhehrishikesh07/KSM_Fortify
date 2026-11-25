"""Phase 2C: prompt files are clean and configurable; parse_tasks survives realistic model output."""
import re
import time
from types import SimpleNamespace

import pytest

from app import config, prompt_templates
from app.ai_assistant import TASK_TRIGGER_PHRASE, AIAssistant
from app.data_store import TaskStore
from app.gui.web_api import WebAPI

PROMPTS = sorted(config.PROMPTS_DIR.glob("*.txt"))


# ---------------------------------------------------------------- prompt files
def test_prompt_files_exist():
    assert len(PROMPTS) == 4   # chat, task extraction, strategy audit, screen scan (audit moved out of the code in Phase 5A)


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.name)
def test_no_citation_artifacts(path):
    assert "[cite" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.name)
def test_no_hardcoded_person_or_semester(path):
    text = path.read_text(encoding="utf-8")
    assert "Hrishikesh" not in text
    assert "1st Year" not in text and "1st-year" not in text
    assert "Sem 2" not in text


def test_text_prompts_use_placeholders():
    helper = config.GENERAL_HELPER_PROMPT_FILE.read_text(encoding="utf-8")
    task = config.TASK_TRANSLATION_PROMPT_FILE.read_text(encoding="utf-8")
    assert "{USER_NAME}" in helper and "{ASSISTANT_NAME}" in helper
    assert "{USER_NAME}" in task


def test_trigger_phrase_matches_prompt_exactly():
    # The phrase is misspelled on purpose; code and prompt must agree character for character.
    # Checked on the FINISHED prompt (blocks joined, {TRIGGER_PHRASE} filled in), which is what the model sees.
    helper = prompt_templates.fill(prompt_templates.shipped_text("chat"))
    assert TASK_TRIGGER_PHRASE in helper
    assert helper.count(TASK_TRIGGER_PHRASE) >= 2


def test_render_prompt_fills_only_known_tokens(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", "Asha")
    monkeypatch.setattr(config, "ASSISTANT_NAME", "Bot")
    monkeypatch.setattr(config, "USER_PROFILE", "a nurse")
    out = AIAssistant.render_prompt("{ASSISTANT_NAME} helps {USER_NAME}, {USER_PROFILE}. {FULL_SESSION_CONTEXT} [x]")
    assert out == "Bot helps Asha, a nurse. {FULL_SESSION_CONTEXT} [x]"


class FakeGroq:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        text = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _assistant(replies, monkeypatch=None):
    a = AIAssistant()
    a._client = FakeGroq(replies)
    return a


def test_chat_system_prompt_is_rendered_with_configured_names(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", "Asha")
    monkeypatch.setattr(config, "ASSISTANT_NAME", "Bot")
    a = _assistant(["hi"])
    a.chat(["User: hello"], "[]")
    system = a._client.calls[0]["messages"][0]["content"]
    assert "Asha" in system and "Bot" in system
    assert "{USER_NAME}" not in system and "{ASSISTANT_NAME}" not in system
    assert "Hrishikesh" not in system and "[cite" not in system


def test_extraction_prompt_is_rendered_with_configured_names(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", "Asha")
    a = _assistant(["whatever"])
    a.validate_and_extract("User: x", "[]")
    payload = a._client.calls[0]["messages"][0]["content"]
    assert "Asha" in payload and "{USER_NAME}" not in payload
    assert "[cite" not in payload and "current date :-" in payload


# ---------------------------------------------------------------- parse_tasks
GOOD = """task :- Finish maths assignment
importance :- 3
urgency :- 2
current data :- 2026-10-26
current time :- 09:00:00
expected completion date :- 2026-10-30
expected completion time :- 18:00:00
reasoning :- Academic and due this week."""


def test_parse_single_valid_block():
    [t] = AIAssistant.parse_tasks(GOOD)
    assert (t.title, t.importance, t.urgency) == ("Finish maths assignment", 3, 2)
    assert t.expected_at == "2026-10-30 18:00:00"
    assert t.reasoning.startswith("Academic")


def test_parse_two_blocks():
    out = AIAssistant.parse_tasks(GOOD + "\n\n" + GOOD.replace("maths assignment", "physics lab"))
    assert [t.title for t in out] == ["Finish maths assignment", "Finish physics lab"]


def test_parse_duplicate_detected_returns_nothing():
    assert AIAssistant.parse_tasks("DUPLICATE_DETECTED: This goal is already in your matrix.") == []


def test_parse_empty_and_chatter_return_nothing():
    assert AIAssistant.parse_tasks("") == []
    assert AIAssistant.parse_tasks("Sure, happy to help with that!") == []


def test_parse_skips_malformed_block_but_keeps_good_one():
    bad = "task :- Broken\nimportance :- high\nurgency :- 2\nexpected completion date :- 2026-10-30\nexpected completion time :- 10:00:00"
    out = AIAssistant.parse_tasks(bad + "\n\n" + GOOD)
    assert [t.title for t in out] == ["Finish maths assignment"]


def test_parse_clamps_scores_to_1_to_3():
    out = AIAssistant.parse_tasks(GOOD.replace("importance :- 3", "importance :- 7").replace("urgency :- 2", "urgency :- 0"))
    assert (out[0].importance, out[0].urgency) == (3, 1)


def test_parse_accepts_time_without_seconds():
    out = AIAssistant.parse_tasks(GOOD.replace("18:00:00", "18:00"))
    assert out[0].expected_at == "2026-10-30 18:00:00"


def test_parse_accepts_echoed_brackets_from_the_format_template():
    text = GOOD.replace("importance :- 3", "importance :- [3]").replace("2026-10-30", "[2026-10-30]").replace(
        "task :- Finish maths assignment", "task :- [Finish maths assignment]"
    )
    [t] = AIAssistant.parse_tasks(text)
    assert (t.title, t.importance, t.expected_at) == ("Finish maths assignment", 3, "2026-10-30 18:00:00")


def test_parse_skips_block_with_impossible_date():
    assert AIAssistant.parse_tasks(GOOD.replace("2026-10-30", "2026-13-45")) == []


def test_parse_result_is_always_accepted_by_task_store(tmp_path):
    store = TaskStore(data_file=tmp_path / "m.json")
    for t in AIAssistant.parse_tasks(GOOD.replace("18:00:00", "18:00")):
        store.add_task(t.title, t.importance, t.urgency, t.expected_at, origin="AUTO")
    assert len(store.tasks) == 1


# ---------------------------------------------------------------- full chat -> task pipeline
class FakeScreen:
    available = False
    def problem(self): return ""


class FakeOfficer:
    available = False


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_pipeline_chat_trigger_extract_add(tmp_path):
    a = _assistant(["Sounds good! " + TASK_TRIGGER_PHRASE, GOOD + "\n\n" + GOOD.replace("maths assignment", "lab record")])
    api = WebAPI(TaskStore(data_file=tmp_path / "m.json"), a, FakeOfficer(), FakeScreen())
    assert api.send_chat("I need to finish my maths assignment and lab record") == {"ok": True}
    assert _wait(lambda: len(api._store.tasks) == 2 and not api._chat_busy)
    assert [t["title"] for t in api._store.tasks] == ["Finish maths assignment", "Finish lab record"]
    assert all("[AUTO]" in t["history"][0] for t in api._store.tasks)


def test_pipeline_one_bad_task_does_not_block_the_rest(tmp_path, monkeypatch):
    a = _assistant(["ok " + TASK_TRIGGER_PHRASE, GOOD + "\n\n" + GOOD.replace("maths assignment", "lab record")])
    api = WebAPI(TaskStore(data_file=tmp_path / "m.json"), a, FakeOfficer(), FakeScreen())
    real_add = api._store.add_task
    state = {"n": 0}

    def flaky_add(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("disk hiccup")
        return real_add(**kw)

    monkeypatch.setattr(api._store, "add_task", flaky_add)
    api.send_chat("two tasks please")
    assert _wait(lambda: not api._chat_busy and state["n"] == 2)
    assert [t["title"] for t in api._store.tasks] == ["Finish lab record"]
    texts = [m["text"] for m in api._chat_messages]
    assert any("Could not add" in t and "maths" in t for t in texts)
