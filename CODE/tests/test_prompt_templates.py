"""Phase 5A: prompts are built from blocks (locked / editable) and the default prompt is unchanged.

tests/fixtures/phase4_prompts/ holds the four prompts exactly as they were in Phase 4. They pin that
splitting the prompts into blocks changed no word. If the owner later changes a shipped default on
purpose, update the matching fixture in the same commit (they may be retired after Phase 5F)."""
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, prompt_templates as pt
from app.ai_assistant import TASK_TRIGGER_PHRASE, AIAssistant
from app.screen_analyzer import FALLBACK_PROMPT, ScreenAnalyzer
from app.strategy_officer import StrategyOfficer, StrategyOfficerError

FIXTURES = Path(__file__).parent / "fixtures" / "phase4_prompts"
OLD_FILE = {
    "chat": "general_helper.txt",
    "task_extraction": "task_translation_prompt.txt",
    "audit": "strategy_audit.txt",
    "screen_scan": "screen_analyzer.txt",
}


def _good(role):
    """The shipped file for a role as text (to build slightly damaged copies from)."""
    return pt.shipped_path(role).read_text(encoding="utf-8")


def _use_file(monkeypatch, role, tmp_path, text):
    path = tmp_path / f"{role}.txt"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, pt.ROLE_FILE_ATTR[role], path)
    return path


# ------------------------------------------------------------ nothing changed for the AI
@pytest.mark.parametrize("role", pt.ROLES)
def test_default_prompt_equals_the_phase4_prompt_word_for_word(role):
    old = (FIXTURES / OLD_FILE[role]).read_text(encoding="utf-8")
    old_filled = (
        old.replace("{USER_NAME}", config.USER_NAME)
        .replace("{ASSISTANT_NAME}", config.ASSISTANT_NAME)
        .replace("{USER_PROFILE}", config.USER_PROFILE)
    )
    # only blank lines at the very start or end may differ
    assert pt.fill(pt.shipped_text(role)) == old_filled.strip("\n")


@pytest.mark.parametrize("role", pt.ROLES)
def test_default_prompt_keeps_the_wire(role):
    assert pt.wire_problems(role, pt.fill(pt.shipped_text(role))) == []


# ------------------------------------------------------------ layout and files
@pytest.mark.parametrize("role", pt.ROLES)
def test_blocks_follow_the_layout(role):
    blocks = pt.load_shipped(role)
    want = [e for e in pt.LAYOUTS[role] if e[0] != pt.INSERT]
    assert [(b.id, b.locked) for b in blocks] == [(i, k == pt.LOCKED) for i, k in want]
    assert all(b.text.strip() for b in blocks)


def test_locked_blocks_are_the_small_wire_only():
    chat = {b.id for b in pt.load_shipped("chat") if b.locked}
    ext = {b.id for b in pt.load_shipped("task_extraction") if b.locked}
    assert chat == {"trigger_rule", "confirmation"}
    assert ext == {"input_context", "dedup_reply", "categorization", "output_format"}
    assert {b.id for b in pt.load_shipped("audit") if b.locked} == {"data_description"}
    assert not any(b.locked for b in pt.load_shipped("screen_scan"))
    assert pt.LOCKED_BLOCK_IDS == chat | ext | {"data_description"}


def test_editable_shipped_text_never_holds_the_literal_phrase():
    for role in pt.ROLES:
        for b in pt.load_shipped(role):
            if not b.locked:
                assert TASK_TRIGGER_PHRASE not in b.text.lower(), (role, b.id)
    chat = {b.id: b for b in pt.load_shipped("chat")}
    assert "{TRIGGER_PHRASE}" in chat["duplicate_check"].text
    assert TASK_TRIGGER_PHRASE in chat["trigger_rule"].text      # locked blocks keep it as written
    assert TASK_TRIGGER_PHRASE in chat["confirmation"].text


def test_trigger_phrase_has_one_source_of_truth():
    assert pt.TASK_TRIGGER_PHRASE == "task addtion initiated"      # misspelled on purpose
    assert TASK_TRIGGER_PHRASE is pt.TASK_TRIGGER_PHRASE


def test_tight_blocks_join_without_a_blank_line():
    ext = pt.fill(pt.shipped_text("task_extraction"))
    assert "you MUST ABORT.\n   - If aborting, reply ONLY with:" in ext
    audit = pt.fill(pt.shipped_text("audit"))
    assert "Chief Strategy Officer AI'.\nI am going to provide" in audit


# ------------------------------------------------------------ parser refuses damaged files
def _swap(text, a, b):
    assert text.count(a) == 1, a
    return text.replace(a, b)


def test_a_locked_block_marked_editable_is_refused():
    raw = _swap(_good("chat"), "@@@ block trigger_rule locked", "@@@ block trigger_rule editable")
    with pytest.raises(pt.PromptTemplateError, match="must be locked"):
        pt.parse_shipped("chat", raw)


def test_blocks_in_the_wrong_order_or_missing_are_refused():
    raw = _good("chat")
    swapped = _swap(_swap(raw, "@@@ block behaviour editable", "@@@ block XX editable"),
                    "@@@ block duplicate_check editable", "@@@ block behaviour editable")
    swapped = swapped.replace("@@@ block XX editable", "@@@ block duplicate_check editable")
    with pytest.raises(pt.PromptTemplateError, match="expected block"):
        pt.parse_shipped("chat", swapped)
    with pytest.raises(pt.PromptTemplateError, match="entries"):
        pt.parse_shipped("chat", _swap(raw, "@@@ insert\n", ""))


def test_text_outside_a_block_and_unknown_markers_are_refused():
    with pytest.raises(pt.PromptTemplateError, match="outside any block"):
        pt.parse_shipped("chat", "stray words\n" + _good("chat"))
    with pytest.raises(pt.PromptTemplateError, match="unknown marker"):
        pt.parse_shipped("chat", _good("chat") + "\n@@@ blok oops editable\n")
    with pytest.raises(pt.PromptTemplateError, match="bad block line"):
        pt.parse_shipped("chat", _swap(_good("chat"), "@@@ block scale editable", "@@@ block scale sometimes"))


def test_empty_block_and_literal_phrase_in_an_editable_block_are_refused():
    raw = _good("chat")
    first = raw.index("@@@ block identity editable")
    second = raw.index("@@@ block behaviour editable")
    with pytest.raises(pt.PromptTemplateError, match="is empty"):
        pt.parse_shipped("chat", raw[:first] + "@@@ block identity editable\n\n" + raw[second:])
    with pytest.raises(pt.PromptTemplateError, match="trigger phrase"):
        pt.parse_shipped("chat", _swap(raw, "1. Act as a normal", "1. Say task addtion initiated. Act as a normal"))


def test_unknown_role_is_refused():
    with pytest.raises(pt.PromptTemplateError):
        pt.shipped_path("nope")
    with pytest.raises(pt.PromptTemplateError):
        pt.parse_shipped("nope", "")


# ------------------------------------------------------------ assembly
def test_override_replaces_editable_text_only():
    blocks = pt.load_shipped("chat")
    out = pt.assemble("chat", blocks, overrides={"behaviour": "BE SHORT", "trigger_rule": "HACKED"})
    assert "BE SHORT" in out and "[BEHAVIOR]" not in out
    assert "HACKED" not in out                                   # a locked block can never be overridden
    assert "3. TRIGGER LOGIC" in out


def test_disabling_removes_editable_blocks_but_never_locked_ones():
    blocks = pt.load_shipped("chat")
    out = pt.assemble("chat", blocks, disabled=["scale", "trigger_rule", "confirmation"])
    assert "4. NUMERICAL ALIGNMENT" not in out
    assert "3. TRIGGER LOGIC" in out and "5. NATURAL CONFIRMATION" in out


def test_extras_sit_at_the_insert_point_sorted_and_before_the_last_locked_block():
    blocks = pt.load_shipped("chat")
    out = pt.assemble("chat", blocks, extras=[(20, "SECOND EXTRA"), (10, "FIRST EXTRA"), (5, "   ")])
    assert out.index("FIRST EXTRA") < out.index("SECOND EXTRA")
    assert out.index("4. NUMERICAL ALIGNMENT") < out.index("FIRST EXTRA")
    assert out.index("SECOND EXTRA") < out.index("5. NATURAL CONFIRMATION")      # locked wire stays last
    assert out.endswith(pt.load_shipped("chat")[-1].text)
    ext = pt.assemble("task_extraction", pt.load_shipped("task_extraction"), extras=[(1, "ANSWER IN SHORT")])
    assert ext.index("ANSWER IN SHORT") < ext.index("[STRICT OUTPUT FORMAT]")
    assert ext.index("6. DECISION ON URGENCY") < ext.index("ANSWER IN SHORT")


def test_a_tight_block_is_not_glued_to_a_block_that_was_switched_off():
    blocks = pt.load_shipped("task_extraction")
    out = pt.assemble("task_extraction", blocks, disabled=["dedup_rules"])
    assert "\n\n   - If aborting, reply ONLY with:" in out


def test_assemble_needs_every_block():
    with pytest.raises(pt.PromptTemplateError, match="missing"):
        pt.assemble("chat", pt.load_shipped("chat")[:-1])


# ------------------------------------------------------------ placeholders
def test_fill_replaces_the_four_tokens_and_leaves_the_rest(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", "Asha")
    monkeypatch.setattr(config, "ASSISTANT_NAME", "Bot")
    monkeypatch.setattr(config, "USER_PROFILE", "a nurse")
    out = pt.fill("{ASSISTANT_NAME} helps {USER_NAME}, {USER_PROFILE}; say {TRIGGER_PHRASE}. {FULL_SESSION_CONTEXT} {OTHER}")
    assert out == "Bot helps Asha, a nurse; say task addtion initiated. {FULL_SESSION_CONTEXT} {OTHER}"
    assert AIAssistant.render_prompt("{TRIGGER_PHRASE}") == TASK_TRIGGER_PHRASE


# ------------------------------------------------------------ the wire (prompt <-> code)
def test_wire_problems_names_what_is_missing():
    assert pt.wire_problems("chat", "nothing here") == ["the task trigger phrase is missing"]
    assert pt.wire_problems("chat", "ok TASK ADDTION INITIATED ok") == []      # same case-blind test as the chat code
    problems = pt.wire_problems("task_extraction", "just words")
    assert any("DUPLICATE_DETECTED" in p for p in problems)
    assert len(problems) == 1 + len(pt.EXTRACTION_LABEL_PATTERNS)
    assert pt.wire_problems("audit", "") == [] and pt.wire_problems("screen_scan", "") == []
    assert pt.wire_problems("bogus", "x")


def _sample_model_answer():
    """Turns the output-format example inside the shipped extraction prompt into a real answer."""
    fmt = {b.id: b for b in pt.load_shipped("task_extraction")}["output_format"].text
    values = {"task": "Prepare the maths exam", "importance": "2", "urgency": "3",
              "expected completion date": "2026-11-02", "expected completion time": "09:30:00"}
    lines = []
    for line in fmt.splitlines():
        label = line.split(":-")[0].strip().lower() if ":-" in line else ""
        if label in values:
            lines.append(f"{label} :- {values[label]}")
        elif label:
            lines.append(line)               # labels the parser ignores (current data/time, reasoning)
    return "\n".join(lines)


def test_the_output_format_in_the_prompt_is_something_parse_tasks_reads():
    tasks = AIAssistant.parse_tasks(_sample_model_answer())
    assert len(tasks) == 1
    t = tasks[0]
    assert (t.title, t.importance, t.urgency, t.expected_at) == ("Prepare the maths exam", 2, 3, "2026-11-02 09:30:00")


def test_the_duplicate_reply_in_the_prompt_makes_parse_tasks_return_nothing():
    reply_block = {b.id: b for b in pt.load_shipped("task_extraction")}["dedup_reply"].text
    assert AIAssistant.parse_tasks(reply_block) == []
    assert AIAssistant.parse_tasks(_sample_model_answer() + "\n" + reply_block) == []


def test_the_confirmation_example_in_the_chat_prompt_triggers_the_chat_code():
    confirmation = {b.id: b for b in pt.load_shipped("chat")}["confirmation"].text
    assert AIAssistant.reply_contains_trigger(confirmation)


# ------------------------------------------------------------ the consumers
def test_chat_and_extraction_get_their_prompt_from_the_blocks(monkeypatch):
    sent = []
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: (
        sent.append(kw) or SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])))))
    a = AIAssistant()
    a._client = fake
    a.chat(["User: hi"], "[]")
    a.validate_and_extract("ctx", "[]")
    chat_system = sent[0]["messages"][0]["content"]
    assert "{TRIGGER_PHRASE}" not in chat_system and "{USER_NAME}" not in chat_system
    assert chat_system.count(TASK_TRIGGER_PHRASE) >= 3
    extraction = sent[1]["messages"][0]["content"]
    assert "[STRICT OUTPUT FORMAT]" in extraction and "[EXISTING MATRIX TASKS]" in extraction


def test_a_missing_or_damaged_prompt_file_gives_an_empty_prompt_and_a_log_line(monkeypatch, tmp_path, caplog):
    _use_file(monkeypatch, "chat", tmp_path, "not a block file")
    with caplog.at_level(logging.ERROR):
        assert AIAssistant._shipped_prompt("chat") == ""
    monkeypatch.setattr(config, "GENERAL_HELPER_PROMPT_FILE", tmp_path / "missing.txt")
    assert AIAssistant._shipped_prompt("chat") == ""


def test_strategy_audit_sends_the_phase4_text(monkeypatch):
    seen = {}

    class FakeModels:
        def generate_content(self, model, contents, config):
            seen["contents"] = contents
            return SimpleNamespace(text="report")

    officer = StrategyOfficer()
    officer._client = SimpleNamespace(models=FakeModels())
    assert officer.run_audit('[{"id": 1}]') == "report"
    old = (FIXTURES / "strategy_audit.txt").read_text(encoding="utf-8").strip("\n")
    assert seen["contents"] == old + '\n\nHERE IS MY CURRENT MATRIX DATA:\n[{"id": 1}]'


def test_strategy_audit_with_a_damaged_prompt_file_says_so(monkeypatch, tmp_path):
    _use_file(monkeypatch, "audit", tmp_path, "garbage")
    officer = StrategyOfficer()
    officer._client = object()
    with pytest.raises(StrategyOfficerError, match="prompt file is unusable"):
        officer.run_audit("[]")


def test_screen_scan_prompt_comes_from_the_blocks_and_falls_back(monkeypatch, tmp_path):
    old = (FIXTURES / "screen_analyzer.txt").read_text(encoding="utf-8").strip()
    assert ScreenAnalyzer.load_prompt(None) == old
    _use_file(monkeypatch, "screen_scan", tmp_path, "garbage")
    from app import prompt_store
    prompt_store.reset_store_for_tests()    # 5D: the prompt library keeps its built text; the shipped file never changes in a running app
    assert ScreenAnalyzer.load_prompt(None) == FALLBACK_PROMPT
    monkeypatch.setattr(config, "SCREEN_PROMPT_FILE", tmp_path / "missing.txt")
    prompt_store.reset_store_for_tests()
    assert ScreenAnalyzer.load_prompt(None) == FALLBACK_PROMPT
