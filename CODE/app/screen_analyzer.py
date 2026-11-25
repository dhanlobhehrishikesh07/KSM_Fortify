"""
screen_analyzer.py
------------------
The "Screen Scout" ministry (micro-service #1).

Flow:  capture_screen()  ->  prepare_image()  ->  analyze()  ->  text answer

* capture_screen   grabs the PC screen (mss, falls back to Pillow ImageGrab)
* prepare_image    shrinks it (SCREEN_MAX_WIDTH) and JPEG-encodes it so the
                   upload is small and fast; also builds a tiny preview for the GUI
* analyze          sends image + prompts/screen_analyzer.txt to Gemini (vision)

Uses the same Gemini key as the Strategy Officer. The model is set separately
(SCREEN_MODEL_ID in the SETTINGS tab) so you can pick a fast/cheap vision model.
No GUI code in here — web_api.py wires it to the page.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from typing import Optional

from app import config, prompt_templates, prompt_use
from app.logger import get_logger

log = get_logger(__name__)

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - optional dependency
    genai = None
    types = None

try:
    from PIL import Image, ImageGrab
except ImportError:  # pragma: no cover - optional dependency
    Image = None
    ImageGrab = None

try:
    import mss
except ImportError:  # pragma: no cover - optional dependency
    mss = None


FALLBACK_PROMPT = (
    "Look at this PC screenshot. List every visible app/window: its name, what it "
    "does, and its key visible text. Only report what you can actually see."
)


class ScreenScanError(RuntimeError):
    """Any user-presentable failure in the screen-scan pipeline."""


@dataclass
class ScanResult:
    text: str
    preview_b64: str            # small JPEG, base64 (no data: prefix)
    orig_size: tuple[int, int]
    sent_size: tuple[int, int]
    sent_bytes: int
    capture_s: float = 0.0
    api_s: float = 0.0
    model: str = ""
    extra: dict = field(default_factory=dict)


class ScreenAnalyzer:
    def __init__(self):
        self._client = None
        self.refresh()

    def refresh(self) -> None:
        """Rebuilds the Gemini client from the current settings (called when the key changes)."""
        self._client = None
        if config.is_gemini_configured() and genai is not None:
            self._client = genai.Client(api_key=config.GEMINI_API_KEY)
        log.info(
            "ScreenAnalyzer init: gemini_client=%s pillow=%s mss=%s model=%s",
            self._client is not None, Image is not None, mss is not None, config.SCREEN_MODEL_ID,
        )

    # -- status ---------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._client is not None and Image is not None

    def problem(self) -> str:
        """Human-readable reason the service can't run, or ''."""
        if genai is None:
            return "Package 'google-genai' is not installed (pip install -r requirements.txt)."
        if not config.is_gemini_configured():
            return "GEMINI_API_KEY is not set. Add it in the SETTINGS tab."
        if Image is None:
            return "Package 'Pillow' is not installed (pip install -r requirements.txt)."
        return ""

    # -- 1. capture -------------------------------------------------------------
    def capture_screen(self, monitor: int = 1) -> "Image.Image":
        """monitor: 1 = primary, 2+ = other displays, 0 = all displays stitched."""
        if Image is None:
            raise ScreenScanError("Pillow is not installed.")
        t0 = time.time()
        img = None
        if mss is not None:
            try:
                with mss.mss() as sct:
                    mons = sct.monitors
                    if monitor < 0 or monitor >= len(mons):
                        log.warning("Monitor %s not found (have %s) — using primary", monitor, len(mons) - 1)
                        monitor = 1
                    shot = sct.grab(mons[monitor])
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    log.debug("mss captured monitor=%s size=%s", monitor, img.size)
            except Exception as e:  # noqa: BLE001
                log.warning("mss capture failed (%s) — trying Pillow ImageGrab", e)
        if img is None and ImageGrab is not None:
            try:
                img = ImageGrab.grab(all_screens=(monitor == 0)).convert("RGB")
                log.debug("ImageGrab captured size=%s", img.size)
            except Exception as e:  # noqa: BLE001
                log.error("ImageGrab failed: %s", e)
                raise ScreenScanError(
                    f"Could not capture the screen: {e}. (On Linux you need a running desktop "
                    "session; on macOS grant Screen Recording permission to your terminal/Python.)"
                ) from e
        if img is None:
            raise ScreenScanError("Could not capture the screen: install 'mss' (pip install mss).")
        log.info("Captured screen %sx%s in %.2fs", img.width, img.height, time.time() - t0)
        return img

    # -- 2. prepare ---------------------------------------------------------------
    def prepare_image(self, img: "Image.Image") -> tuple[bytes, str, tuple[int, int]]:
        """Returns (jpeg_bytes_for_api, preview_b64_for_gui, sent_size)."""
        work = img
        if work.width > config.SCREEN_MAX_WIDTH:
            ratio = config.SCREEN_MAX_WIDTH / work.width
            work = work.resize((config.SCREEN_MAX_WIDTH, int(work.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        work.save(buf, format="JPEG", quality=config.SCREEN_JPEG_QUALITY, optimize=True)
        data = buf.getvalue()

        prev = work.copy()
        prev.thumbnail((640, 400))
        pbuf = io.BytesIO()
        prev.save(pbuf, format="JPEG", quality=70)
        preview_b64 = base64.b64encode(pbuf.getvalue()).decode("ascii")
        log.debug("Prepared image: %sx%s, %.1f KB", work.width, work.height, len(data) / 1024)
        return data, preview_b64, work.size

    # -- 3. analyze -----------------------------------------------------------------
    def load_prompt(self) -> str:
        try:
            text = prompt_use.text("screen_scan").strip()
            if text:
                return text
            log.warning("Prompt file %s is empty — using fallback prompt", config.SCREEN_PROMPT_FILE)
        except prompt_templates.PromptTemplateError as e:
            log.warning("Could not use %s (%s) — using fallback prompt", config.SCREEN_PROMPT_FILE, e)
        return FALLBACK_PROMPT

    def analyze(self, img: "Image.Image", question: str = "", capture_s: float = 0.0) -> ScanResult:
        if not self._client:
            raise ScreenScanError(self.problem() or "Screen analyzer is not available.")
        data, preview_b64, sent_size = self.prepare_image(img)
        prompt = self.load_prompt()
        question = (question or "").strip()
        if question:
            prompt += f"\n\nEXTRA QUESTION FROM THE USER (answer it after the main report):\n{question}"

        log.info(
            "Sending to Gemini model=%s image=%.1f KB prompt_chars=%d extra_question=%s",
            config.SCREEN_MODEL_ID, len(data) / 1024, len(prompt), bool(question),
        )
        t0 = time.time()
        response = None
        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                response = self._client.models.generate_content(
                    model=config.SCREEN_MODEL_ID,
                    contents=[types.Part.from_bytes(data=data, mime_type="image/jpeg"), prompt],
                    config=types.GenerateContentConfig(temperature=0.2),
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e)
                transient = any(s in msg for s in ("503", "UNAVAILABLE", "500", "timed out", "timeout"))
                log.warning("Gemini call failed (attempt %d): %s", attempt, msg[:300])
                if attempt == 1 and transient:
                    time.sleep(1.5)
                    continue
                break
        api_s = time.time() - t0

        if response is None:
            raise ScreenScanError(self._friendly_error(last_err))
        text = (getattr(response, "text", None) or "").strip()
        if not text:
            reason = ""
            try:
                reason = str(response.candidates[0].finish_reason)
            except Exception:  # noqa: BLE001
                pass
            log.error("Gemini returned no text (finish_reason=%s)", reason)
            raise ScreenScanError(f"The AI returned an empty answer ({reason or 'no reason given'}). Try again.")
        log.info("Gemini answered in %.2fs (%d chars)", api_s, len(text))
        return ScanResult(
            text=text, preview_b64=preview_b64, orig_size=img.size, sent_size=sent_size,
            sent_bytes=len(data), capture_s=capture_s, api_s=api_s, model=config.SCREEN_MODEL_ID,
        )

    @staticmethod
    def _friendly_error(e: Optional[Exception]) -> str:
        msg = str(e) if e else "unknown error"
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            return ("Free-tier rate limit hit (too many requests per minute/day). "
                    "Wait a minute and try again, or change the screen scan model in the SETTINGS tab.")
        if "404" in msg or "NOT_FOUND" in msg:
            return f"Model '{config.SCREEN_MODEL_ID}' was not found. Check the screen scan model in the SETTINGS tab. ({msg[:160]})"
        if "API_KEY" in msg.upper() or "401" in msg or "403" in msg or "PERMISSION" in msg:
            return "Gemini rejected the API key. Check the Gemini key in the SETTINGS tab."
        return f"AI request failed: {msg[:300]}"

    # -- convenience: whole pipeline ------------------------------------------------
    def scan(self, monitor: int = 1, question: str = "") -> ScanResult:
        t0 = time.time()
        img = self.capture_screen(monitor)
        return self.analyze(img, question, capture_s=time.time() - t0)
