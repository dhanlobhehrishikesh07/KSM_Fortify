"""
prompt_templates.py
-------------------
What a prompt is made of (Phase 5, step 5A).

A prompt is a TEMPLATE owned by the code: an ordered list of blocks, each LOCKED or
EDITABLE.

    LOCKED    the program searches for it, the parser reads it, or it describes the
              data the program sends. Its text ships with the program (the files in
              prompts/), is shown read-only and changes only with a code upgrade.
              It is never stored in Firebase.
    EDITABLE  everything else (persona, behaviour, tone, scoring rules, ...). The
              shipped text is the default; later steps let the owner change it.

The prompt files in prompts/ hold the shipped text of every block:

    @@@ block <id> <editable|locked> [tight]     starts a block ("tight" = join it to
                                                 the block above with no blank line)
    @@@ insert                                   where the owner's extra blocks go
    @@@ # ...                                    a comment line

Which blocks exist, their order and which are locked is decided HERE (LAYOUTS), not by
the file: a file whose blocks do not match the layout is refused, so a damaged or
hand-edited file can never silently turn a locked block into an editable one.

The default prompt (every block at its shipped text, placeholders filled) equals the
Phase 4 prompt word for word; tests/fixtures/phase4_prompts/ pins that.

No GUI, network or Firebase code in here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from app import config

log = logging.getLogger("ksm.prompts")

# Kept verbatim (misspelled on purpose): the model is told to say exactly this when a
# new task is found, and the chat code looks for it in the reply. ONE source of truth;
# editable prompt text writes {TRIGGER_PHRASE} instead of the phrase.
TASK_TRIGGER_PHRASE = "task addtion initiated"

TEMPLATE_VERSION = 1

EDITABLE = "editable"
LOCKED = "locked"
INSERT = "insert"            # layout entry: where the extra blocks go

ROLES = ("chat", "task_extraction", "audit", "screen_scan")

# role -> ordered layout. ("insert",) marks the insert point; otherwise (block id, kind).
LAYOUTS: dict[str, tuple] = {
    "chat": (
        ("identity", EDITABLE),
        ("behaviour", EDITABLE),
        ("duplicate_check", EDITABLE),
        ("trigger_rule", LOCKED),
        ("scale", EDITABLE),
        (INSERT,),
        ("confirmation", LOCKED),
    ),
    "task_extraction": (
        ("identity", EDITABLE),
        ("input_context", LOCKED),
        ("dedup_rules", EDITABLE),
        ("dedup_reply", LOCKED),
        ("translation", EDITABLE),
        ("analysis", EDITABLE),
        ("categorization", LOCKED),
        ("importance_rules", EDITABLE),
        ("urgency_rules", EDITABLE),
        (INSERT,),
        ("output_format", LOCKED),
    ),
    "audit": (
        ("identity", EDITABLE),
        ("data_description", LOCKED),
        ("mission", EDITABLE),
        ("tone", EDITABLE),
        (INSERT,),
    ),
    "screen_scan": (
        ("role", EDITABLE),
        ("job", EDITABLE),
        ("rules", EDITABLE),
        (INSERT,),
    ),
}

# role -> name of the config attribute that holds the shipped file path (looked up at
# call time so tests can point it somewhere else)
ROLE_FILE_ATTR = {
    "chat": "GENERAL_HELPER_PROMPT_FILE",
    "task_extraction": "TASK_TRANSLATION_PROMPT_FILE",
    "audit": "STRATEGY_AUDIT_PROMPT_FILE",
    "screen_scan": "SCREEN_PROMPT_FILE",
}

# Block ids that are locked in at least one role. Firebase rules and the store must never accept these
# ids as editable text (checked by tests/test_config_scopes.py).
LOCKED_BLOCK_IDS = frozenset(
    entry[0] for layout in LAYOUTS.values() for entry in layout
    if len(entry) == 2 and entry[1] == LOCKED
)

# Placeholders the program fills in. {FULL_SESSION_CONTEXT} is deliberately NOT here: the
# extraction prompt mentions it by name and the code appends the real value after the prompt.
FILLED_TOKENS = ("USER_NAME", "ASSISTANT_NAME", "USER_PROFILE", "TRIGGER_PHRASE")
LEFT_AS_WRITTEN_TOKENS = ("FULL_SESSION_CONTEXT",)

# What the parser reads out of the model's answer (see AIAssistant.parse_tasks).
EXTRACTION_LABEL_PATTERNS = (
    r"task\s?:-",
    r"importance\s*:-",
    r"urgency\s*:-",
    r"expected completion date\s*:-",
    r"expected completion time\s*:-",
)
DUPLICATE_MARKER = "DUPLICATE_DETECTED"


class PromptTemplateError(ValueError):
    """A prompt file that does not match its template (user-presentable message)."""


@dataclass(frozen=True)
class Block:
    id: str
    locked: bool
    text: str
    tight: bool = False      # join to the block above with a single line break


# ----------------------------------------------------------------------------
# Reading the shipped files
# ----------------------------------------------------------------------------

_MARKER_RE = re.compile(r"^@@@\s+(block|insert|#)(?:\s+(.*))?$")


def shipped_path(role: str) -> Path:
    if role not in LAYOUTS:
        raise PromptTemplateError(f"Unknown prompt role: {role!r}")
    return Path(getattr(config, ROLE_FILE_ATTR[role]))


def parse_shipped(role: str, raw: str) -> list[Block]:
    """Turns the text of a prompt file into blocks and checks it against the layout.
    Returns the blocks in layout order (the insert point is not a block)."""
    if role not in LAYOUTS:
        raise PromptTemplateError(f"Unknown prompt role: {role!r}")
    found: list = []          # ("block", id, kind, tight, [lines]) or ("insert",)
    current = None
    for line in raw.splitlines():
        m = _MARKER_RE.match(line)
        if m:
            what, rest = m.group(1), (m.group(2) or "").split()
            if what == "#":
                continue
            if what == "insert":
                current = None
                found.append(("insert",))
                continue
            if len(rest) < 2 or rest[1] not in (EDITABLE, LOCKED) or any(w != "tight" for w in rest[2:]):
                raise PromptTemplateError(f"{role}: bad block line: {line!r}")
            current = ["block", rest[0], rest[1], "tight" in rest[2:], []]
            found.append(current)
        elif line.startswith("@@@"):
            raise PromptTemplateError(f"{role}: unknown marker line: {line[:60]!r}")
        elif current is not None:
            current[4].append(line)
        elif line.strip():
            raise PromptTemplateError(f"{role}: text outside any block: {line[:60]!r}")

    expected = LAYOUTS[role]
    if len(found) != len(expected):
        raise PromptTemplateError(
            f"{role}: the file has {len(found)} entries but the template has {len(expected)}")
    blocks: list[Block] = []
    for entry, want in zip(found, expected):
        if want[0] == INSERT:
            if entry[0] != "insert":
                raise PromptTemplateError(f"{role}: the insert point is missing or in the wrong place")
            continue
        if entry[0] != "block" or entry[1] != want[0]:
            got = entry[1] if entry[0] == "block" else "insert"
            raise PromptTemplateError(f"{role}: expected block {want[0]!r} here, found {got!r}")
        if entry[2] != want[1]:
            raise PromptTemplateError(f"{role}: block {want[0]!r} must be {want[1]} (the file says {entry[2]})")
        text = "\n".join(entry[4]).strip("\n")
        if not text:
            raise PromptTemplateError(f"{role}: block {want[0]!r} is empty")
        if want[1] == EDITABLE and TASK_TRIGGER_PHRASE in text.lower():
            raise PromptTemplateError(
                f"{role}: editable block {want[0]!r} contains the trigger phrase; write {{TRIGGER_PHRASE}}")
        blocks.append(Block(id=want[0], locked=want[1] == LOCKED, text=text, tight=entry[3]))
    return blocks


def load_shipped(role: str) -> list[Block]:
    path = shipped_path(role)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PromptTemplateError(f"Cannot read the prompt file {path.name}: {e}") from e
    return parse_shipped(role, raw)


# ----------------------------------------------------------------------------
# Building the text
# ----------------------------------------------------------------------------


def assemble(role: str, blocks: Iterable[Block], overrides: Optional[dict] = None,
             disabled: Iterable[str] = (), extras: Iterable[tuple] = ()) -> str:
    """Joins the blocks in layout order. Placeholders are NOT filled (see fill()).

    overrides  {block id: text} replaces the text of EDITABLE blocks only (locked ones are ignored)
    disabled   ids of EDITABLE blocks to leave out (locked ones cannot be switched off)
    extras     (order, text) pairs added at the insert point, sorted by order
    """
    by_id = {b.id: b for b in blocks}
    overrides = overrides or {}
    off = set(disabled)
    pieces: list[tuple[str, bool]] = []      # (text, join tightly to the previous piece)
    prev_was_block = False
    for entry in LAYOUTS[role]:
        if entry[0] == INSERT:
            for _order, text in sorted(extras, key=lambda e: e[0]):
                if text and text.strip():
                    pieces.append((text.strip("\n"), False))
            prev_was_block = False
            continue
        block = by_id.get(entry[0])
        if block is None:
            raise PromptTemplateError(f"{role}: block {entry[0]!r} is missing")
        if not block.locked and block.id in off:
            prev_was_block = False
            continue
        text = block.text
        if not block.locked and overrides.get(block.id) is not None:
            text = overrides[block.id]
        pieces.append((text.strip("\n"), block.tight and prev_was_block))
        prev_was_block = True
    out = ""
    for i, (text, tight) in enumerate(pieces):
        out += text if i == 0 else ("\n" if tight else "\n\n") + text
    return out


def shipped_text(role: str) -> str:
    """The default prompt for a role, placeholders NOT yet filled. Raises PromptTemplateError."""
    return assemble(role, load_shipped(role))


def fill(text: str) -> str:
    """Fills the placeholders from config. Only these exact tokens are replaced, so other
    braces in a prompt are safe."""
    return (
        text.replace("{USER_NAME}", config.USER_NAME)
        .replace("{ASSISTANT_NAME}", config.ASSISTANT_NAME)
        .replace("{USER_PROFILE}", config.USER_PROFILE)
        .replace("{TRIGGER_PHRASE}", TASK_TRIGGER_PHRASE)
    )


# ----------------------------------------------------------------------------
# The wire: what the program needs to find in a finished prompt
# ----------------------------------------------------------------------------


def wire_problems(role: str, text: str) -> list[str]:
    """Checks a FINISHED prompt (placeholders filled) for the words the program depends on.
    Returns a list of problems; empty means the connection to the code is intact."""
    low = text.lower()
    problems: list[str] = []
    if role == "chat":
        if TASK_TRIGGER_PHRASE not in low:
            problems.append("the task trigger phrase is missing")
    elif role == "task_extraction":
        if DUPLICATE_MARKER.lower() not in low:
            problems.append(f"{DUPLICATE_MARKER} is missing")
        for pattern in EXTRACTION_LABEL_PATTERNS:
            if not re.search(pattern, text, re.IGNORECASE):
                problems.append(f"the output label matching {pattern!r} is missing")
    elif role not in LAYOUTS:
        problems.append(f"unknown role {role!r}")
    return problems
