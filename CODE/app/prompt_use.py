"""
prompt_use.py
--------------
The one door the AI modules use to get their prompt text (Phase 5D).

`built(role)` returns the finished prompt of a role from the shared `PromptStore`: the active prompt from Firebase if it
is valid, otherwise the last good copy in the config folder, otherwise the shipped text (the store decides). It uses memory
only: no network call and no file read after the first load, so an AI call never waits for Firebase.

When Firebase prompts are switched on but the text in use is NOT the online one (the online copy has not arrived yet,
the app is offline, a block was refused, or the self-check fell back to the shipped prompt), a plain warning is logged
once per change and kept for the screens (`notes()`), so the owner can see which text the AI really got.

`PromptTemplateError` is NOT swallowed here: it means the shipped prompt file itself is unusable, and each caller keeps
its own old behaviour (chat: empty persona and a log line; audit: a clear error; screen scan: the fallback prompt).
"""

from __future__ import annotations

import dataclasses
import threading

from app import prompt_store, prompt_templates
from app.logger import get_logger
from app.prompt_store import Built
from app.prompt_templates import PromptTemplateError  # noqa: F401  (re-exported for the callers)

log = get_logger(__name__)

_lock = threading.Lock()
_logged: dict[str, tuple] = {}       # role -> the warnings last logged for it (so the log is not flooded)
_notes: dict[str, list] = {}         # role -> warnings of the last use


def _online_note(store, built: Built) -> list:
    """Why the text in use is not the online one, in plain words; empty when it is (or nothing online is expected)."""
    try:
        if not store.enabled() or built.source == "firebase":
            return []
    except Exception:  # noqa: BLE001
        return []
    label = built.role.replace("_", " ")
    if built.fell_back:
        return [f"The {label} prompt from Firebase could not be used; the shipped text was used instead."]
    if built.source == "cache":
        return [f"The {label} prompt is the last copy saved on this PC; the online version has not been loaded (offline or still starting)."]
    return [f"The {label} prompt is the shipped text; the online version has not been loaded (offline or still starting)."]


def _remember(role: str, built: Built, extra: list) -> Built:
    warnings = list(built.warnings) + [w for w in extra if w not in built.warnings]
    with _lock:
        _notes[role] = warnings
        key = tuple(warnings)
        if _logged.get(role) != key:
            _logged[role] = key
            for w in warnings:
                log.warning("Prompt %s: %s", role, w)
    return dataclasses.replace(built, warnings=warnings) if warnings != built.warnings else built


def built(role: str, store=None) -> Built:
    """The finished prompt for `role`. Raises PromptTemplateError only when the shipped file itself is unusable.
    A bug in the store never reaches the AI call: the shipped text is used and the problem is logged."""
    store = store or prompt_store.get_store()
    try:
        result = store.build(role)
    except PromptTemplateError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("The prompt library failed for %s; using the shipped text", role)
        text = prompt_templates.fill(prompt_templates.shipped_text(role))     # may raise PromptTemplateError: callers know it
        result = Built(role, text, source="shipped", warnings=["The prompt library failed; the shipped text was used."], fell_back=True)
        return _remember(role, result, [])
    return _remember(role, result, _online_note(store, result))


def text(role: str, store=None) -> str:
    return built(role, store).text


def notes(role: str = "") -> list:
    """Warnings of the last use of a role (or of every role when no role is given)."""
    with _lock:
        if role:
            return list(_notes.get(role, []))
        return [w for r in sorted(_notes) for w in _notes[r]]


def reset_for_tests() -> None:
    with _lock:
        _logged.clear()
        _notes.clear()
