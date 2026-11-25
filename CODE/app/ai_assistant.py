"""
ai_assistant.py
----------------
The "General_Helper" ministry: a conversational assistant (Groq / Llama)
that chats naturally, notices when the user describes a new goal, and
then runs a stricter second pass to translate that goal into a
structured task (title / importance / urgency / deadline).

This module has no GUI or socket code in it — it is a plain Python
class you can call from the chat tab, from the Firebase bridge, or
from tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app import config, prompt_templates, prompt_use
from app.logger import get_logger
from app.prompt_templates import TASK_TRIGGER_PHRASE  # noqa: F401  (re-exported: other modules and tests import it from here)

log = get_logger(__name__)

TIME_FMT = "%Y-%m-%d %H:%M:%S"

try:
    from groq import Groq
except ImportError:  # pragma: no cover - optional dependency
    Groq = None

# TASK_TRIGGER_PHRASE ("task addtion initiated", misspelled on purpose) is defined once in
# app/prompt_templates.py and re-exported here; the prompts write {TRIGGER_PHRASE}.


@dataclass
class ParsedTask:
    title: str
    importance: int
    urgency: int
    expected_at: str  # 'YYYY-MM-DD HH:MM:SS'
    reasoning: str = ""


class AIAssistantError(RuntimeError):
    pass


class AIAssistant:
    def __init__(self):
        self._client = None
        self.refresh()

    def refresh(self) -> None:
        """Rebuilds the Groq client from the current settings (called when the key changes)."""
        self._client = None
        if config.is_groq_configured() and Groq is not None:
            self._client = Groq(api_key=config.GROQ_API_KEY)

    @property
    def available(self) -> bool:
        return self._client is not None

    def _require_client(self):
        if not self._client:
            raise AIAssistantError(
                "GROQ_API_KEY is not set (or the 'groq' package isn't installed). "
                "Add it in the SETTINGS tab to enable the AI assistant."
            )
        return self._client

    @staticmethod
    def _shipped_prompt(role: str) -> str:
        """The default prompt for a role (blocks joined, placeholders not yet filled).
        A missing or damaged prompt file gives an empty prompt and a log line, as before."""
        try:
            return prompt_templates.shipped_text(role)
        except prompt_templates.PromptTemplateError as e:
            log.error("Prompt for %s unavailable: %s", role, e)
            return ""

    @staticmethod
    def _prompt(role: str) -> str:
        """The finished prompt of a role (placeholders filled) from the prompt library: the online prompt when Firebase
        prompts are on and loaded, else the last saved copy, else the shipped text. Memory only, never waits for the network.
        A damaged shipped file gives an empty prompt and a log line, as before."""
        try:
            return prompt_use.text(role)
        except prompt_templates.PromptTemplateError as e:
            log.error("Prompt for %s unavailable: %s", role, e)
            return ""

    @staticmethod
    def render_prompt(text: str) -> str:
        """Fills {USER_NAME}, {ASSISTANT_NAME}, {USER_PROFILE} and {TRIGGER_PHRASE}.
        Only these exact tokens are replaced, so other braces in a prompt are safe."""
        return prompt_templates.fill(text)

    # -- Phase 1: general conversation ---------------------------------------
    def chat(self, session_history: list[str], matrix_json: str) -> str:
        """Takes the running conversation (list of 'User: ...' / 'AI: ...'
        strings) plus the current matrix JSON, returns the assistant's reply."""
        client = self._require_client()
        persona = self._prompt("chat")
        messages = [
            {
                "role": "system",
                "content": f"{persona}\n\n[CURRENT_MATRIX_DATA]\n{matrix_json or '[]'}",
            },
            {
                "role": "user",
                "content": "Full Conversation History:\n" + "\n".join(session_history),
            },
        ]
        completion = client.chat.completions.create(model=config.GROQ_MODEL_ID, messages=messages)
        return completion.choices[0].message.content

    @staticmethod
    def reply_contains_trigger(reply: str) -> bool:
        return TASK_TRIGGER_PHRASE in reply.lower()

    # -- Phase 2: strict task extraction --------------------------------------
    def validate_and_extract(self, full_session_context: str, matrix_json: str) -> str:
        """Runs the low-temperature validation pass and returns the model's
        raw structured-text output (still needs parse_tasks() to turn it
        into task dicts)."""
        client = self._require_client()
        instructions = self._prompt("task_extraction")
        now = datetime.now()
        payload = f"""{instructions}

[LIVE SYSTEM INFO]
current date :- {now.strftime('%Y-%m-%d')}
current time :- {now.strftime('%H:%M:%S')}

[EXISTING MATRIX TASKS]
{matrix_json or "No tasks in matrix."}

{{FULL_SESSION_CONTEXT: "{full_session_context}"}}
"""
        response = client.chat.completions.create(
            model=config.GROQ_MODEL_ID,
            messages=[{"role": "user", "content": payload}],
            temperature=0.1,
        )
        return response.choices[0].message.content

    @staticmethod
    def _clean(value: str) -> str:
        """Strips whitespace and the [ ] / quotes a model sometimes echoes from the format template."""
        return value.strip().strip("[]\"'").strip()

    @staticmethod
    def _normalize_expected(date_str: str, time_str: str) -> Optional[str]:
        """Returns 'YYYY-MM-DD HH:MM:SS' or None if the date/time is not valid.
        Accepts HH:MM as well as HH:MM:SS."""
        date_str = AIAssistant._clean(date_str)
        time_str = AIAssistant._clean(time_str)
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            time_str += ":00"
        candidate = f"{date_str} {time_str}"
        try:
            datetime.strptime(candidate, TIME_FMT)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def parse_tasks(structured_text: str) -> list[ParsedTask]:
        """Parses one or more 'task :- ...' blocks out of the model's
        structured output. Skips/ignores malformed blocks instead of
        raising, so one bad block doesn't lose the others."""
        if "duplicate_detected" in structured_text.lower():
            return []

        blocks = re.split(r"(?=task\s?:-)", structured_text, flags=re.IGNORECASE)
        parsed: list[ParsedTask] = []

        for block in blocks:
            if not re.match(r"\s*task\s?:-", block, re.IGNORECASE):
                continue
            try:
                title = AIAssistant._clean(re.search(r"task\s?:-[ \t]*(.*)", block, re.IGNORECASE).group(1))
                imp = int(re.search(r"importance\s*:-\s*\[?\s*(\d+)", block, re.IGNORECASE).group(1))
                urg = int(re.search(r"urgency\s*:-\s*\[?\s*(\d+)", block, re.IGNORECASE).group(1))
                e_date = re.search(r"expected completion date\s*:-[ \t]*(.*)", block, re.IGNORECASE).group(1)
                e_time = re.search(r"expected completion time\s*:-[ \t]*(.*)", block, re.IGNORECASE).group(1)
                reasoning_match = re.search(r"reasoning\s*:-[ \t]*(.*)", block, re.IGNORECASE)
                reasoning = reasoning_match.group(1).strip() if reasoning_match else ""
            except (AttributeError, ValueError):
                log.warning("Skipping malformed task block: %r", block[:120])
                continue

            expected_at = AIAssistant._normalize_expected(e_date, e_time)
            if not title or expected_at is None:
                log.warning("Skipping task block with empty title or invalid date/time: %r", block[:120])
                continue

            # Clamp to the valid 1-3 scale defensively.
            parsed.append(
                ParsedTask(
                    title=title,
                    importance=min(3, max(1, imp)),
                    urgency=min(3, max(1, urg)),
                    expected_at=expected_at,
                    reasoning=reasoning,
                )
            )

        return parsed
