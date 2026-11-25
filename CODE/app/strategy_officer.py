"""
strategy_officer.py
--------------------
The "Chief Strategy Officer" ministry: sends the full matrix to Gemini
and asks for a blunt audit — Danger Zones, Growth Tasks, a daily battle
plan, and a call-out on procrastination patterns found in task history.
"""

from __future__ import annotations

from app import config, prompt_templates, prompt_use

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - optional dependency
    genai = None
    types = None


class StrategyOfficerError(RuntimeError):
    pass


class StrategyOfficer:
    def __init__(self):
        self._client = None
        self.refresh()

    def refresh(self) -> None:
        """Rebuilds the Gemini client from the current settings (called when the key changes)."""
        self._client = None
        if config.is_gemini_configured() and genai is not None:
            self._client = genai.Client(api_key=config.GEMINI_API_KEY)

    @property
    def available(self) -> bool:
        return self._client is not None

    def run_audit(self, matrix_json: str) -> str:
        if not self._client:
            raise StrategyOfficerError(
                "GEMINI_API_KEY is not set (or the 'google-genai' package isn't "
                "installed). Add it in the SETTINGS tab to enable strategy audits."
            )
        try:
            prompt = prompt_use.text("audit")
        except prompt_templates.PromptTemplateError as e:
            raise StrategyOfficerError(f"The audit prompt file is unusable: {e}") from e
        full_query = f"{prompt}\n\nHERE IS MY CURRENT MATRIX DATA:\n{matrix_json}"
        response = self._client.models.generate_content(
            model=config.GEMINI_MODEL_ID,
            contents=full_query,
            config=types.GenerateContentConfig(temperature=0.7),
        )
        return response.text
