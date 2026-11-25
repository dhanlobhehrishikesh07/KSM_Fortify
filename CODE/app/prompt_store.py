"""
prompt_store.py
---------------
The prompt library (Phase 5, step 5B). No UI, no AI calls.

It keeps the owner's EDITABLE prompt text, in their own Firebase under `ksm/config`, and builds the final prompt for each
role from the TEMPLATE in `prompt_templates.py` (the locked words always come from the code, never from here).

What it does
* Lists prompts, reads one (blocks in order, locked ones read-only), saves (every save is a version), saves as new,
  restores a version, resets a block or a prompt, deletes a custom prompt, sets which prompt a role uses.
* `build(role)` returns the finished prompt text. It uses only memory: it never waits for the network.
* Order of winners for each editable block: (1) the active prompt's block from Firebase, if it passes the guard rules;
  (2) the last good copy kept in the config folder (`prompts_cache.json`); (3) the shipped text.
* Guard rules (checked on save and again at use): no literal trigger phrase; for `task_extraction` also no
  `DUPLICATE_DETECTED` and no output label lines; at most 4000 characters per block and 20000 in total; no API key text.
  A block that fails is ignored (the next source is used) and a warning is kept. A Firebase block that has the id of a
  locked block, or an id the template does not have, is ignored with a warning.
* Self-check: before a built prompt is used the program checks the words it depends on (`wire_problems`). If any is
  missing the shipped default for the role is used instead, with a warning.

Rules this module lives by (same as cloud_sync)
* Offline first. Every public method catches cloud failures: nothing here raises into the UI, and a local change is never
  lost. With "Keep prompts online" off, or no Firebase configured, it sends and reads nothing and raises no error.
* A save is a conditional write: it names the version it started from and is refused if the prompt changed meanwhile.
  A save made while offline (or with the switch off) becomes the working copy at once and waits in `prompts_cache.json`
  ("pending"); if the prompt changed elsewhere in the meantime it is NOT uploaded and the owner picks "keep mine" or
  "take theirs" (`resolve_conflict`). Several offline saves of one prompt upload as one version.
* Never stored in Firebase: locked block text, API keys, anything else outside the rules for `ksm/config`.
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from app import cloud_sync, config, prompt_templates, settings_store
from app.cloud_sync import CloudError, SERVER_TIME, TransactionAbort
from app.logger import get_logger
from app.prompt_templates import (EDITABLE, INSERT, LAYOUTS, LOCKED_BLOCK_IDS, ROLES, TASK_TRIGGER_PHRASE,
                                  PromptTemplateError)

log = get_logger(__name__)

CONFIG_ROOT = f"{cloud_sync.ROOT}/config"
PROMPTS_ROOT = f"{CONFIG_ROOT}/prompts"
ACTIVE_ROOT = f"{CONFIG_ROOT}/prompt_active"
VERSIONS_ROOT = f"{cloud_sync.ROOT}/config_versions/prompts"

CACHE_FILE = "prompts_cache.json"
CACHE_VERSION = 1

MAX_BLOCK_CHARS = 4000
MAX_TOTAL_CHARS = 20000
SOFT_WARN_CHARS = 6000          # about 1500 tokens: the chat prompt goes out with every message
MAX_TITLE_CHARS = 100
MAX_NAME_CHARS = 100
MAX_DESCRIPTION_CHARS = 500
MAX_NOTE_CHARS = 500
DEFAULT_KEEP = 30

EXTRA_ID = "extra"              # the owner's free block at the insert point (always exists, empty at first)
CUSTOM_PREFIX = "x-"            # custom blocks the owner adds

BUILTIN_IDS = {
    "chat": "chat-default",
    "task_extraction": "task-extraction-default",
    "audit": "audit-default",
    "screen_scan": "screen-scan-default",
}
BUILTIN_NAMES = {
    "chat": "Chat (default)",
    "task_extraction": "Task extraction (default)",
    "audit": "Strategy audit (default)",
    "screen_scan": "Screen scan (default)",
}
_BUILTIN_ROLE = {v: k for k, v in BUILTIN_IDS.items()}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
_CUSTOM_BLOCK_RE = re.compile(r"^x-[A-Za-z0-9_-]{1,57}$")
_TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Key shapes that must never leave the machine even if that key is not saved on THIS computer (typed on another device).
_KEY_SHAPES = (re.compile(r"gsk_[A-Za-z0-9]{20,}"), re.compile(r"AIza[0-9A-Za-z_\-]{30,}"))
_PUSH_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------


def push_id(now_ms: Optional[int] = None) -> str:
    """A Firebase-style push key: 8 characters of time (so keys sort by creation) then 12 random ones."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    stamp = ""
    for _ in range(8):
        stamp = _PUSH_CHARS[now % 64] + stamp
        now //= 64
    return stamp + "".join(random.choice(_PUSH_CHARS) for _ in range(12))


def new_block_id() -> str:
    """An id for a custom block the owner adds (starts with x-)."""
    return CUSTOM_PREFIX + push_id()


def estimate_tokens(text: str) -> int:
    """A rough count (about 4 characters per token) for the size warning and the preview. Uses no AI tokens."""
    return (len(text) + 3) // 4


def layout_ids(role: str) -> list:
    """Ids of the blocks the template has for a role, in order (locked and editable, not the insert point)."""
    return [e[0] for e in LAYOUTS[role] if e[0] != INSERT]


def editable_layout_ids(role: str) -> list:
    return [e[0] for e in LAYOUTS[role] if e[0] != INSERT and e[1] == EDITABLE]


def locked_layout_ids(role: str) -> set:
    return {e[0] for e in LAYOUTS[role] if e[0] != INSERT and e[1] != EDITABLE}


def block_kind(role: str, block_id: str) -> str:
    """'locked', 'layout' (an editable block of the template), 'extra', 'custom' or '' (not allowed)."""
    if block_id in locked_layout_ids(role) or block_id in LOCKED_BLOCK_IDS:
        return "locked"
    if block_id in editable_layout_ids(role):
        return "layout"
    if block_id == EXTRA_ID:
        return "extra"
    if _CUSTOM_BLOCK_RE.match(block_id):
        return "custom"
    return ""


def _title_for(block_id: str) -> str:
    return block_id.replace("_", " ").replace("-", " ").strip().capitalize() or block_id


def _now_ms() -> int:
    return int(time.time() * 1000)


def _int(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _secrets_in(text: str) -> bool:
    if settings_store.redact_text(text) != text:
        return True
    return any(rx.search(text) for rx in _KEY_SHAPES)


# ----------------------------------------------------------------------------
# The guard rules for editable text
# ----------------------------------------------------------------------------


def check_text(role: str, text: Any) -> tuple:
    """Guard rules for ONE editable block. Returns (errors, warnings); an empty errors list means it may be used."""
    errors: list = []
    warnings: list = []
    if not isinstance(text, str):
        return ["The text must be text."], warnings
    low = text.lower()
    if TASK_TRIGGER_PHRASE in low:
        errors.append("It contains the task trigger phrase. Write {TRIGGER_PHRASE} instead.")
    if role == "task_extraction":
        if prompt_templates.DUPLICATE_MARKER.lower() in low:
            errors.append(f"It contains {prompt_templates.DUPLICATE_MARKER}, a word the program reads in the answer.")
        for pattern in prompt_templates.EXTRACTION_LABEL_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                errors.append("It contains an output label line (for example 'task :-') that the program reads in the answer.")
                break
    if len(text) > MAX_BLOCK_CHARS:
        errors.append(f"It is {len(text)} characters; a block may have at most {MAX_BLOCK_CHARS}.")
    if _secrets_in(text):
        errors.append("It contains what looks like an API key. Keys are never stored in a prompt.")
    known = set(prompt_templates.FILLED_TOKENS) | set(prompt_templates.LEFT_AS_WRITTEN_TOKENS)
    unknown = sorted({t for t in _TOKEN_RE.findall(text) if t not in known and t.isupper()})
    if unknown:
        warnings.append("Unknown placeholder(s) will be sent as written: " + ", ".join("{" + t + "}" for t in unknown))
    return errors, warnings


# ----------------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------------


@dataclass
class Built:
    """A finished prompt for one role."""
    role: str
    text: str
    prompt_id: str = ""
    source: str = "shipped"          # "shipped" (nothing edited in use), "firebase" or "cache"
    warnings: list = field(default_factory=list)
    fell_back: bool = False          # True: the self-check failed and the shipped default was used instead

    def as_dict(self) -> dict:
        return {"role": self.role, "text": self.text, "prompt_id": self.prompt_id, "source": self.source,
                "warnings": list(self.warnings), "fell_back": self.fell_back,
                "chars": len(self.text), "tokens": estimate_tokens(self.text)}


@dataclass
class Result:
    """The outcome of a change. `ok` False means nothing was changed; `message` says why in plain words."""
    ok: bool = True
    message: str = ""
    prompt_id: str = ""
    version: int = 0
    queued: bool = False             # saved here, waiting to be sent
    conflict: bool = False           # the prompt changed elsewhere
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "prompt_id": self.prompt_id, "version": self.version,
                "queued": self.queued, "conflict": self.conflict, "errors": list(self.errors),
                "warnings": list(self.warnings)}


def _fail(message: str, prompt_id: str = "", conflict: bool = False, errors: Optional[list] = None) -> Result:
    return Result(ok=False, message=message, prompt_id=prompt_id, conflict=conflict, errors=list(errors or [message]))


# ----------------------------------------------------------------------------
# Default switches (read from the settings; tests pass their own)
# ----------------------------------------------------------------------------


def _default_switch() -> bool:
    return str(settings_store.get_store().get("SYNC_PROMPTS")).strip().lower() == "true"


def _default_configured() -> bool:
    return bool(config.ENABLE_FIREBASE and config.FIREBASE_DATABASE_URL)


def _default_keep() -> int:
    try:
        n = int(float(settings_store.get_store().get("PROMPT_VERSIONS_KEEP")))
    except (TypeError, ValueError):
        return DEFAULT_KEEP
    return min(200, max(5, n))


# ----------------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------------


class PromptStore:
    def __init__(self, cloud=None, cache_path: Optional[Path] = None,
                 switch: Optional[Callable[[], bool]] = None,
                 configured: Optional[Callable[[], bool]] = None,
                 keep: Optional[Callable[[], int]] = None,
                 clock: Optional[Callable[[], int]] = None,
                 author: Optional[str] = None):
        self._cloud = cloud
        self._cache_path = Path(cache_path) if cache_path else None
        self._switch = switch or _default_switch
        self._configured = configured or _default_configured
        self._keep = keep or _default_keep
        self._clock = clock or _now_ms
        self._author = author
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._nodes: dict[str, dict] = {}       # prompt id -> node: the working copy (cloud, cache and local saves)
        self._active: dict[str, str] = {}       # role -> prompt id
        self._pending: dict[str, dict] = {}     # prompt id -> {base_version, node, note, stage, create}
        self._pending_active: dict[str, str] = {}
        self._conflicts: dict[str, dict] = {}   # prompt id -> {"theirs": node | None}
        self._warnings: list[str] = []          # from the last time Firebase data was read
        self._from_cloud = False                # True once Firebase has been read successfully this session
        self._last_sync_ms = 0
        self._error = ""
        self._rev = 0                           # bumps on every change that can alter a built prompt
        self._built: dict[str, tuple] = {}      # role -> (key, Built)
        self._load_cache()

    # ---- plumbing ------------------------------------------------------------------------------
    @property
    def cloud(self):
        if self._cloud is None:
            self._cloud = cloud_sync.FirebaseCloudStore()
        return self._cloud

    def _path(self) -> Path:
        return self._cache_path or settings_store.config_dir() / CACHE_FILE

    def _who(self) -> str:
        if self._author is None:
            try:
                self._author = "desktop:" + settings_store.device_identity()["device_id"]
            except Exception:  # noqa: BLE001
                self._author = "desktop"
        return self._author[:60]

    def enabled(self) -> bool:
        """True when this store may talk to Firebase: the switch is on and Firebase is configured."""
        try:
            return bool(self._switch()) and bool(self._configured())
        except Exception:  # noqa: BLE001
            return False

    def _changed(self) -> None:
        self._rev += 1
        self._built.clear()

    def _record_error(self, e: Exception) -> str:
        msg = settings_store.redact_text(str(e) or type(e).__name__)
        with self._lock:
            self._error = msg
        log.info("Prompt library: %s", msg)
        return msg

    # ---- cache file ------------------------------------------------------------------------------
    def _load_cache(self) -> None:
        path = self._path()
        try:
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            warns: list = []
            for pid, raw in (data.get("prompts") or {}).items():
                node = self._clean_node(str(pid), raw, warns, None)
                if node is not None:
                    self._nodes[str(pid)] = node
            for role, pid in (data.get("active") or {}).items():
                if role in ROLES and isinstance(pid, str) and _ID_RE.match(pid):
                    self._active[role] = pid
            for pid, item in (data.get("pending") or {}).items():
                node = self._clean_node(str(pid), (item or {}).get("node"), warns, None)
                if node is not None:
                    self._pending[str(pid)] = {
                        "base_version": _int(item.get("base_version")), "node": node,
                        "note": str(item.get("note") or "")[:MAX_NOTE_CHARS],
                        "stage": "version" if item.get("stage") == "version" else "node",
                        "create": bool(item.get("create"))}
                    self._nodes[str(pid)] = node
            for role, pid in (data.get("pending_active") or {}).items():
                if role in ROLES and isinstance(pid, str) and _ID_RE.match(pid):
                    self._pending_active[role] = pid
            for pid, item in (data.get("conflicts") or {}).items():
                if str(pid) in self._pending:
                    theirs = self._clean_node(str(pid), (item or {}).get("theirs"), warns, None)
                    self._conflicts[str(pid)] = {"theirs": theirs}
            self._last_sync_ms = _int(data.get("last_sync_ms"))
            self._changed()
        except Exception as e:  # noqa: BLE001 - a damaged cache must never stop the app
            log.warning("Prompt cache unreadable (%s); starting from the shipped prompts and keeping a copy", type(e).__name__)
            self._nodes, self._active, self._pending, self._pending_active, self._conflicts = {}, {}, {}, {}, {}
            try:
                path.replace(path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"))
            except OSError:
                pass

    def _persist(self) -> None:
        with self._lock:
            payload = json.dumps({
                "version": CACHE_VERSION, "template_version": prompt_templates.TEMPLATE_VERSION,
                "prompts": self._nodes, "active": self._active,
                "pending": self._pending, "pending_active": self._pending_active,
                "conflicts": self._conflicts, "last_sync_ms": self._last_sync_ms,
            }, ensure_ascii=False)
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".prompts-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            log.warning("Could not save the prompt cache (%s)", type(e).__name__)

    # ---- reading a node (from Firebase, the cache or a save) -------------------------------------------
    def _clean_node(self, prompt_id: str, raw: Any, warnings: list, previous: Optional[dict]) -> Optional[dict]:
        """Turns untrusted data into a node we may keep, or None. Bad blocks are dropped with a warning; a block that
        fails the guard keeps its previous good text if there is one (order of winners, step 2)."""
        if not isinstance(raw, dict) or not _ID_RE.match(prompt_id):
            return None
        if prompt_id in _BUILTIN_ROLE:
            role = _BUILTIN_ROLE[prompt_id]
            if raw.get("role") not in (None, "", role):
                warnings.append(f"The built-in prompt {prompt_id} has the wrong role online; it was ignored.")
                return None
            builtin = True
        else:
            role = raw.get("role")
            if role not in ROLES:
                warnings.append(f"A prompt with an unknown role was ignored ({prompt_id}).")
                return None
            builtin = False
        node = {
            "name": str(raw.get("name") or BUILTIN_NAMES.get(role, prompt_id))[:MAX_NAME_CHARS],
            "description": str(raw.get("description") or "")[:MAX_DESCRIPTION_CHARS],
            "role": role, "builtin": builtin,
            "template_version": _int(raw.get("template_version"), 1),
            "version": max(0, _int(raw.get("version"))),
            "updated_at": _int(raw.get("updated_at")),
            "updated_by": str(raw.get("updated_by") or "")[:60],
            "blocks": {},
        }
        blocks = raw.get("blocks")
        if isinstance(blocks, dict):
            for bid, rb in blocks.items():
                bid = str(bid)
                kind = block_kind(role, bid)
                if kind == "locked":
                    warnings.append(f"Online block '{bid}' of {node['name']} has the name of a locked block; it was ignored.")
                    continue
                if not kind or not isinstance(rb, dict) or not isinstance(rb.get("text"), str):
                    warnings.append(f"Online block '{bid}' of {node['name']} is not part of this prompt; it was ignored.")
                    continue
                enabled = rb.get("enabled", True)
                if not isinstance(enabled, bool):
                    enabled = True
                errors, _ = check_text(role, rb["text"])
                if errors:
                    warnings.append(f"Online block '{bid}' of {node['name']} was not used: {errors[0]}")
                    old = ((previous or {}).get("blocks") or {}).get(bid)
                    if old:
                        node["blocks"][bid] = old
                    continue
                entry: dict = {"text": rb["text"], "enabled": enabled}
                if kind == "custom":
                    entry["title"] = str(rb.get("title") or "")[:MAX_TITLE_CHARS]
                if kind in ("custom", "extra") and isinstance(rb.get("order"), (int, float)) and not isinstance(rb.get("order"), bool):
                    entry["order"] = rb["order"]
                node["blocks"][bid] = entry
        return node

    # ---- reading Firebase -------------------------------------------------------------------------------
    def apply_snapshot(self, prompts: Any, active: Any) -> list:
        """Takes what Firebase holds at `ksm/config/prompts` and `ksm/config/prompt_active` (a dict or None each) and makes
        it the working copy. Also what the live listener will call later. Returns the warnings found."""
        warns: list = []
        with self._lock:
            new_nodes: dict = {}
            prompts = prompts if isinstance(prompts, dict) else {}
            for pid, raw in prompts.items():
                node = self._clean_node(str(pid), raw, warns, self._nodes.get(str(pid)))
                if node is not None:
                    new_nodes[str(pid)] = node
            for pid, item in list(self._pending.items()):
                theirs = new_nodes.get(pid)
                if item.get("create"):
                    clash = theirs is not None                      # an id that did not exist is now taken
                elif theirs is None:
                    clash = item["base_version"] != 0               # it was deleted elsewhere
                else:
                    clash = theirs["version"] != item["base_version"]   # it moved on while mine waited
                if clash:
                    self._conflicts[pid] = {"theirs": theirs}
                else:
                    self._conflicts.pop(pid, None)
                new_nodes[pid] = item["node"]          # my unsent copy stays the working copy until it is settled
            for pid, old in self._nodes.items():
                if pid not in new_nodes and old.get("version", 0) > 0 and pid not in self._pending:
                    warns.append(f"The online copy no longer has the prompt '{old['name']}'; the shipped text is used.")
            self._nodes = new_nodes
            active = active if isinstance(active, dict) else {}
            new_active = {}
            for role, pid in active.items():
                if role in ROLES and isinstance(pid, str) and _ID_RE.match(pid):
                    new_active[role] = pid
            for role, pid in self._pending_active.items():
                new_active[role] = pid
            self._active = new_active
            self._warnings = warns
            self._from_cloud = True
            self._last_sync_ms = self._clock()
            self._error = ""
            self._changed()
        self._persist()
        return warns

    def refresh(self) -> dict:
        """Reads the prompts from Firebase (one background call). Never raises; returns the status."""
        if not self.enabled():
            return self.status()
        try:
            prompts = self.cloud.get(PROMPTS_ROOT)
            active = self.cloud.get(ACTIVE_ROOT)
        except Exception as e:  # noqa: BLE001
            self._record_error(e)
            return self.status()
        try:
            self.apply_snapshot(prompts, active)
        except Exception as e:  # noqa: BLE001
            log.exception("Could not read the prompts from the online copy")
            self._record_error(e)
        return self.status()

    # ---- listing and reading ----------------------------------------------------------------------------
    def _effective_node(self, prompt_id: str) -> Optional[dict]:
        """The working node, or a virtual one for a built-in prompt that was never saved."""
        node = self._nodes.get(prompt_id)
        if node is not None:
            return node
        role = _BUILTIN_ROLE.get(prompt_id)
        if role:
            return {"name": BUILTIN_NAMES[role], "description": "", "role": role, "builtin": True,
                    "template_version": prompt_templates.TEMPLATE_VERSION, "version": 0,
                    "updated_at": 0, "updated_by": "", "blocks": {}}
        return None

    def active_id(self, role: str) -> str:
        """The prompt a role uses now (the built-in one if the choice is missing or points to something unusable)."""
        with self._lock:
            pid = self._active.get(role)
            node = self._effective_node(pid) if pid else None
            if node is not None and node["role"] == role:
                return pid
            return BUILTIN_IDS[role]

    def list_prompts(self) -> list:
        with self._lock:
            ids = list(BUILTIN_IDS.values()) + sorted(p for p, n in self._nodes.items() if p not in _BUILTIN_ROLE)
            out = []
            for pid in ids:
                node = self._effective_node(pid)
                if node is None:
                    continue
                role = node["role"]
                out.append({
                    "id": pid, "name": node["name"], "description": node["description"], "role": role,
                    "builtin": node["builtin"], "version": node["version"], "updated_at": node["updated_at"],
                    "updated_by": node["updated_by"], "active": self.active_id(role) == pid,
                    "modified": self._is_modified(node), "pending": pid in self._pending,
                    "conflict": pid in self._conflicts,
                })
            return out

    def _is_modified(self, node: dict) -> bool:
        try:
            shipped = {b.id: b for b in prompt_templates.load_shipped(node["role"])}
        except PromptTemplateError:
            return bool(node["blocks"])
        for bid, blk in node["blocks"].items():
            kind = block_kind(node["role"], bid)
            if kind == "custom":
                return True
            if kind == "extra":
                if blk["text"].strip() or not blk["enabled"]:
                    return True
            elif kind == "layout" and (not blk["enabled"] or blk["text"].strip("\n") != shipped[bid].text.strip("\n")):
                return True
        return False

    def get_prompt(self, prompt_id: str) -> dict:
        """One prompt for the screen: its blocks in order. Locked blocks carry the shipped text and `locked: True`."""
        with self._lock:
            node = self._effective_node(prompt_id)
            if node is None:
                return {"ok": False, "message": "No such prompt."}
            role = node["role"]
            try:
                shipped = {b.id: b for b in prompt_templates.load_shipped(role)}
            except PromptTemplateError as e:
                return {"ok": False, "message": str(e)}
            blocks = []
            for entry in LAYOUTS[role]:
                if entry[0] == INSERT:
                    blocks.extend(self._insert_blocks(node))
                    continue
                bid = entry[0]
                ship = shipped[bid]
                if ship.locked:
                    blocks.append({"id": bid, "title": _title_for(bid), "kind": "locked", "locked": True,
                                   "text": ship.text, "enabled": True, "default_text": ship.text, "modified": False})
                    continue
                mine = node["blocks"].get(bid)
                text = mine["text"] if mine else ship.text
                enabled = mine["enabled"] if mine else True
                blocks.append({"id": bid, "title": _title_for(bid), "kind": "layout", "locked": False, "text": text,
                               "enabled": enabled, "default_text": ship.text,
                               "modified": (text.strip("\n") != ship.text.strip("\n")) or not enabled})
            return {"ok": True, "id": prompt_id, "name": node["name"], "description": node["description"], "role": role,
                    "builtin": node["builtin"], "version": node["version"], "active": self.active_id(role) == prompt_id,
                    "pending": prompt_id in self._pending, "conflict": prompt_id in self._conflicts,
                    "blocks": blocks, "warnings": list(self._warnings)}

    def _insert_blocks(self, node: dict) -> list:
        """The `extra` block and the custom blocks, in the order they are used."""
        rows = []
        extra = node["blocks"].get(EXTRA_ID) or {"text": "", "enabled": True}
        rows.append((extra.get("order", 0), 0, {
            "id": EXTRA_ID, "title": "Extra instructions", "kind": "extra", "locked": False, "text": extra["text"],
            "enabled": extra["enabled"], "default_text": "", "modified": bool(extra["text"].strip()) or not extra["enabled"],
            "order": extra.get("order", 0)}))
        for bid, blk in node["blocks"].items():
            if block_kind(node["role"], bid) == "custom":
                rows.append((blk.get("order", 0), 1, {
                    "id": bid, "title": blk.get("title") or "Custom block", "kind": "custom", "locked": False,
                    "text": blk["text"], "enabled": blk["enabled"], "default_text": "", "modified": True,
                    "order": blk.get("order", 0)}))
        rows.sort(key=lambda r: (r[0], r[1], r[2]["id"]))
        return [r[2] for r in rows]

    # ---- validating a change ------------------------------------------------------------------------------
    def _apply_changes(self, node: dict, changes: dict) -> tuple:
        """Applies {block id: {"text"?, "enabled"?, "title"?, "order"?} | None} to a copy of node's blocks.
        None removes a custom block. Returns (blocks, errors, warnings)."""
        role = node["role"]
        blocks = json.loads(json.dumps(node["blocks"]))
        errors: list = []
        warnings: list = []
        for bid, spec in (changes or {}).items():
            bid = str(bid)
            kind = block_kind(role, bid)
            if kind == "locked":
                errors.append(f"'{bid}' is a locked block. Its text changes only with a program upgrade.")
                continue
            if not kind:
                errors.append(f"'{bid}' is not a block of this prompt.")
                continue
            if spec is None:
                if kind == "custom":
                    blocks.pop(bid, None)
                else:
                    errors.append(f"The block '{bid}' cannot be removed; reset it instead.")
                continue
            if not isinstance(spec, dict):
                errors.append(f"The change for '{bid}' is not valid.")
                continue
            cur = blocks.get(bid) or {"text": "", "enabled": True}
            entry = dict(cur)
            if "text" in spec:
                if not isinstance(spec["text"], str):
                    errors.append(f"The text of '{bid}' must be text.")
                    continue
                errs, warns = check_text(role, spec["text"])
                if errs:
                    errors.extend(f"{_title_for(bid)}: {e}" for e in errs)
                    continue
                warnings.extend(f"{_title_for(bid)}: {w}" for w in warns)
                entry["text"] = spec["text"]
            elif bid not in blocks:
                if kind == "layout":                      # switching a shipped block on or off: start from its shipped text
                    try:
                        entry["text"] = next(b.text for b in prompt_templates.load_shipped(role) if b.id == bid)
                    except (PromptTemplateError, StopIteration):
                        errors.append(f"The shipped text of '{bid}' is not available.")
                        continue
            if "enabled" in spec:
                if not isinstance(spec["enabled"], bool):
                    errors.append(f"'enabled' of '{bid}' must be true or false.")
                    continue
                entry["enabled"] = spec["enabled"]
            if kind == "custom":
                if "title" in spec:
                    if not isinstance(spec["title"], str) or len(spec["title"]) > MAX_TITLE_CHARS:
                        errors.append(f"The title of '{bid}' is too long (at most {MAX_TITLE_CHARS} characters).")
                        continue
                    entry["title"] = spec["title"]
                entry.setdefault("title", "")
            if kind in ("custom", "extra") and "order" in spec:
                if not isinstance(spec["order"], (int, float)) or isinstance(spec["order"], bool):
                    errors.append(f"The order of '{bid}' must be a number.")
                    continue
                entry["order"] = spec["order"]
            blocks[bid] = entry
        total = sum(len(b["text"]) for b in blocks.values())
        if total > MAX_TOTAL_CHARS:
            errors.append(f"The editable text adds up to {total} characters; the limit is {MAX_TOTAL_CHARS}.")
        return blocks, errors, warnings

    def validate(self, prompt_id: str, changes: dict) -> dict:
        """The live guard check for the editor: what would be refused or warned about if `changes` were saved."""
        with self._lock:
            node = self._effective_node(prompt_id)
            if node is None:
                return {"ok": False, "errors": ["No such prompt."], "warnings": []}
            _, errors, warnings = self._apply_changes(node, changes)
            return {"ok": not errors, "errors": errors, "warnings": warnings}

    # ---- building the final text --------------------------------------------------------------------------
    def build(self, role: str) -> Built:
        """The finished prompt for a role (placeholders filled). Memory only: no network, no disk read after the first call
        for the same state. Raises PromptTemplateError only if the SHIPPED file itself is unusable."""
        if role not in LAYOUTS:
            raise PromptTemplateError(f"Unknown prompt role: {role!r}")
        key = (self._rev, config.USER_NAME, config.ASSISTANT_NAME, config.USER_PROFILE)
        with self._lock:
            hit = self._built.get(role)
            if hit and hit[0] == key:
                return hit[1]
            result = self._build_now(role)
            self._built[role] = (key, result)
            return result

    def text(self, role: str) -> str:
        return self.build(role).text

    def _build_now(self, role: str, node: Optional[dict] = None, prompt_id: Optional[str] = None) -> Built:
        shipped = prompt_templates.load_shipped(role)
        default = prompt_templates.fill(prompt_templates.assemble(role, shipped))
        pid = prompt_id or self.active_id(role)
        node = node or self._effective_node(pid)
        warnings: list = list(self._warnings)
        if node is None or not node["blocks"]:
            return Built(role, default, pid, "shipped", warnings)
        overrides: dict = {}
        disabled: list = []
        extras: list = []
        total = 0
        for bid, blk in node["blocks"].items():
            kind = block_kind(role, bid)
            if kind in ("locked", ""):
                warnings.append(f"Block '{bid}' is not an editable block of this prompt and was ignored.")
                continue
            errors, _ = check_text(role, blk["text"])      # the guard is applied again at use
            if errors:
                warnings.append(f"Block '{bid}' was not used: {errors[0]}")
                continue
            if not blk["enabled"]:
                if kind == "layout":
                    disabled.append(bid)
                continue
            total += len(blk["text"])
            if kind == "layout":
                overrides[bid] = blk["text"]
            else:
                extras.append((blk.get("order", 0), blk["text"]))
        if total > MAX_TOTAL_CHARS:
            warnings.append(f"The editable text is over {MAX_TOTAL_CHARS} characters; the shipped prompt is used.")
            return Built(role, default, pid, "shipped", warnings, fell_back=True)
        text = prompt_templates.fill(prompt_templates.assemble(role, shipped, overrides, disabled, extras))
        problems = prompt_templates.wire_problems(role, text)
        if problems:
            warnings.append("The edited prompt lost words the program needs (" + "; ".join(problems) +
                            "); the shipped prompt is used.")
            return Built(role, default, pid, "shipped", warnings, fell_back=True)
        if len(text) > SOFT_WARN_CHARS:
            warnings.append(f"This prompt is long ({len(text)} characters, about {estimate_tokens(text)} tokens) and is "
                            "sent with every request.")
        if role == "screen_scan":
            low = text.lower()
            if "password" not in low or "plain text" not in low:
                warnings.append("Recommended: keep the lines that hide passwords and keys, and the 'plain text only' rule.")
        source = "firebase" if self._from_cloud else "cache"
        return Built(role, text, pid, source, warnings)

    def preview(self, prompt_id: str, changes: Optional[dict] = None) -> dict:
        """The final text with unsaved changes applied and the placeholders filled, for the editor. Saves nothing
        and uses no AI tokens."""
        with self._lock:
            node = self._effective_node(prompt_id)
            if node is None:
                return {"ok": False, "message": "No such prompt."}
            blocks, errors, warnings = self._apply_changes(node, changes or {})
            if errors:
                return {"ok": False, "errors": errors, "warnings": warnings, "message": errors[0]}
            trial = dict(node, blocks=blocks)
            try:
                built = self._build_now(node["role"], trial, prompt_id)
            except PromptTemplateError as e:
                return {"ok": False, "message": str(e), "errors": [str(e)], "warnings": warnings}
            out = built.as_dict()
            out.update({"ok": True, "errors": [], "warnings": warnings + [w for w in built.warnings if w not in warnings]})
            return out

    # ---- status -------------------------------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            if not bool(self._safe(self._switch)):
                state = "off"
            elif not bool(self._safe(self._configured)):
                state = "no_firebase"
            elif self._conflicts:
                state = "conflict"
            elif self._pending or self._pending_active:
                state = "offline" if self._error else "pending"
            elif self._error:
                state = "offline"
            else:
                state = "synced" if self._from_cloud else "starting"
            return {"state": state, "pending": len(self._pending) + len(self._pending_active),
                    "conflicts": sorted(self._conflicts), "last_sync_ms": self._last_sync_ms,
                    "last_error": self._error, "from_cloud": self._from_cloud, "warnings": list(self._warnings)}

    @staticmethod
    def _safe(fn) -> bool:
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False

    def warnings(self) -> list:
        with self._lock:
            return list(self._warnings)

    # ---- saving ---------------------------------------------------------------------------------------------
    def _node_for_write(self, node: dict, blocks: dict, version: int, name: Optional[str] = None,
                        description: Optional[str] = None) -> dict:
        out = {
            "name": (name if name is not None else node["name"])[:MAX_NAME_CHARS],
            "description": (description if description is not None else node["description"])[:MAX_DESCRIPTION_CHARS],
            "role": node["role"], "builtin": node["builtin"],
            "template_version": prompt_templates.TEMPLATE_VERSION, "version": version,
            "updated_at": self._clock(), "updated_by": self._who(), "blocks": blocks,
        }
        return out

    @staticmethod
    def _wire_node(node: dict) -> dict:
        """The node as it is written to Firebase: server clock for `updated_at`."""
        out = json.loads(json.dumps(node))
        out["updated_at"] = SERVER_TIME
        return out

    def _guard_node(self, node: dict) -> list:
        """Guard rules over a whole node (used again just before upload). Returns errors."""
        errors: list = []
        total = 0
        for bid, blk in node["blocks"].items():
            kind = block_kind(node["role"], bid)
            if kind in ("locked", ""):
                errors.append(f"'{bid}' is not an editable block.")
                continue
            errs, _ = check_text(node["role"], blk["text"])
            errors.extend(f"{_title_for(bid)}: {e}" for e in errs)
            total += len(blk["text"])
        if total > MAX_TOTAL_CHARS:
            errors.append(f"The editable text adds up to {total} characters; the limit is {MAX_TOTAL_CHARS}.")
        return errors

    def save(self, prompt_id: str, changes: dict, base_version: Optional[int] = None, note: str = "") -> Result:
        """Saves changes to a prompt as a new version. `changes` is {block id: {"text"?, "enabled"?, "title"?, "order"?}}; a
        value of None removes a custom block. `base_version` is the version the editor started from (default: the
        working one); if the prompt has moved on the save is refused. Never raises."""
        try:
            with self._lock:
                node = self._effective_node(prompt_id)
                if node is None:
                    return _fail("No such prompt.", prompt_id)
                blocks, errors, warnings = self._apply_changes(node, changes)
                if errors:
                    return _fail("The prompt was not saved: " + errors[0], prompt_id, errors=errors)
            return self._commit(prompt_id, blocks, base_version, note, warnings, create=False)
        except Exception as e:  # noqa: BLE001
            log.exception("Prompt save failed")
            return _fail(settings_store.redact_text(f"The prompt was not saved ({type(e).__name__})."), prompt_id)

    def save_as_new(self, role: str, name: str, changes: Optional[dict] = None, description: str = "",
                    from_prompt: Optional[str] = None, note: str = "") -> Result:
        """Makes a new custom prompt for a role, starting from `from_prompt` (default: the role's built-in prompt) with
        `changes` applied. The new prompt is not made active. Never raises."""
        try:
            if role not in ROLES:
                return _fail("Unknown prompt role.")
            name = (name or "").strip()
            if not name or len(name) > MAX_NAME_CHARS:
                return _fail(f"Give the prompt a name of 1 to {MAX_NAME_CHARS} characters.")
            if len(description or "") > MAX_DESCRIPTION_CHARS:
                return _fail(f"The description is too long (at most {MAX_DESCRIPTION_CHARS} characters).")
            with self._lock:
                src = self._effective_node(from_prompt or BUILTIN_IDS[role])
                if src is None or src["role"] != role:
                    return _fail("The prompt to copy from does not exist for this role.")
                new_id = push_id()
                node = {"name": name, "description": description or "", "role": role, "builtin": False,
                        "template_version": prompt_templates.TEMPLATE_VERSION, "version": 0, "updated_at": 0,
                        "updated_by": "", "blocks": json.loads(json.dumps(src["blocks"]))}
                self._nodes[new_id] = node          # lives only here until the commit below succeeds
                try:
                    blocks, errors, warnings = self._apply_changes(node, changes or {})
                    if errors:
                        self._nodes.pop(new_id, None)
                        return _fail("The prompt was not saved: " + errors[0], errors=errors)
                except Exception:
                    self._nodes.pop(new_id, None)
                    raise
            result = self._commit(new_id, blocks, 0, note or "Created", warnings, create=True,
                                  name=name, description=description or "")
            if not result.ok:
                with self._lock:
                    self._nodes.pop(new_id, None)
                    self._changed()
            return result
        except Exception as e:  # noqa: BLE001
            log.exception("Prompt save-as-new failed")
            return _fail(settings_store.redact_text(f"The prompt was not saved ({type(e).__name__})."))

    def restore(self, prompt_id: str, version: int, note: str = "") -> Result:
        """Makes an old version the new working text (a NEW version; history is kept). Needs the online copy."""
        try:
            if not self.enabled():
                return _fail("History is kept online. Turn on 'Keep prompts online' (and Firebase) to restore a version.", prompt_id)
            with self._lock:
                node = self._effective_node(prompt_id)
                if node is None:
                    return _fail("No such prompt.", prompt_id)
            try:
                rec = self.cloud.get(f"{VERSIONS_ROOT}/{prompt_id}/{int(version)}")
            except Exception as e:  # noqa: BLE001
                return _fail(self._record_error(e), prompt_id)
            if not isinstance(rec, dict) or not isinstance(rec.get("blocks"), dict):
                return _fail("That version was not found.", prompt_id)
            warns: list = []
            old = self._clean_node(prompt_id, {"role": node["role"], "blocks": rec["blocks"]}, warns, None)
            if old is None:
                return _fail("That version could not be read.", prompt_id)
            blocks = self._complete_blocks(node["role"], old["blocks"])
            return self._commit(prompt_id, blocks, None, note or f"Restored version {int(version)}", warns, create=False)
        except Exception as e:  # noqa: BLE001
            log.exception("Prompt restore failed")
            return _fail(settings_store.redact_text(f"The version was not restored ({type(e).__name__})."), prompt_id)

    def _complete_blocks(self, role: str, blocks: dict) -> dict:
        """A restored set of blocks with every shipped editable block present (a missing one means 'shipped text')."""
        out = json.loads(json.dumps(blocks))
        shipped = {b.id: b for b in prompt_templates.load_shipped(role)}
        for bid in editable_layout_ids(role):
            out.setdefault(bid, {"text": shipped[bid].text, "enabled": True})
        out.setdefault(EXTRA_ID, {"text": "", "enabled": True})
        return out

    def reset_block(self, prompt_id: str, block_id: str) -> Result:
        """Returns one block to its shipped text (the extra block is emptied, a custom block is removed). Saved as a version."""
        with self._lock:
            node = self._effective_node(prompt_id)
        if node is None:
            return _fail("No such prompt.", prompt_id)
        kind = block_kind(node["role"], block_id)
        if kind == "custom":
            return self.save(prompt_id, {block_id: None}, note=f"Removed block {block_id}")
        if kind == "extra":
            return self.save(prompt_id, {block_id: {"text": "", "enabled": True}}, note="Reset extra instructions")
        if kind == "layout":
            try:
                ship = next(b.text for b in prompt_templates.load_shipped(node["role"]) if b.id == block_id)
            except (PromptTemplateError, StopIteration):
                return _fail("The shipped text is not available.", prompt_id)
            return self.save(prompt_id, {block_id: {"text": ship, "enabled": True}}, note=f"Reset block {block_id}")
        return _fail(f"'{block_id}' is not an editable block.", prompt_id)

    def reset_prompt(self, prompt_id: str) -> Result:
        """Returns a whole prompt to the shipped text: every block shipped and on, the extra block empty, custom blocks removed."""
        with self._lock:
            node = self._effective_node(prompt_id)
            custom = [b for b in (node or {}).get("blocks", {}) if block_kind(node["role"], b) == "custom"]
        if node is None:
            return _fail("No such prompt.", prompt_id)
        try:
            shipped = {b.id: b for b in prompt_templates.load_shipped(node["role"])}
        except PromptTemplateError as e:
            return _fail(str(e), prompt_id)
        changes: dict = {bid: {"text": shipped[bid].text, "enabled": True} for bid in editable_layout_ids(node["role"])}
        changes[EXTRA_ID] = {"text": "", "enabled": True}
        for bid in custom:
            changes[bid] = None
        return self.save(prompt_id, changes, note="Reset to the shipped text")

    def new_block_id(self) -> str:
        return new_block_id()

    # ---- committing a save ------------------------------------------------------------------------------------
    def _commit(self, prompt_id: str, blocks: dict, base_version: Optional[int], note: str, warnings: list,
                create: bool, name: Optional[str] = None, description: Optional[str] = None) -> Result:
        note = (note or "")[:MAX_NOTE_CHARS]
        with self._lock:
            node = self._effective_node(prompt_id)
            working = node["version"]
            if base_version is not None and int(base_version) != working:
                return _fail("This prompt was changed since you opened it. Reload it and try again.",
                             prompt_id, conflict=True)
            if prompt_id in self._conflicts:
                return _fail("This prompt has an unresolved conflict. Choose 'keep mine' or 'take theirs' first.",
                             prompt_id, conflict=True)
            pend = self._pending.get(prompt_id)
            base = pend["base_version"] if pend else working
            if not pend and not create and blocks == node["blocks"] and name is None:
                return Result(ok=True, message="Nothing changed.", prompt_id=prompt_id, version=working, warnings=warnings)
            new_node = self._node_for_write(node, blocks, base + 1, name, description)
            guard = self._guard_node(new_node)
            if guard:
                return _fail("The prompt was not saved: " + guard[0], prompt_id, errors=guard)
            online = self.enabled()
        tried = False
        if online and not pend:
            tried = True
            try:
                committed, _current = self._write_node(prompt_id, new_node, base, create)
            except Exception as e:  # noqa: BLE001 - offline or refused: keep the work locally
                self._record_error(e)
                committed = None
            if committed is False:
                self.refresh()                    # somebody else saved first: bring in what is there now
                return _fail("This prompt was changed elsewhere in the meantime, so your save was refused. "
                             "Reload it and apply your change again.", prompt_id, conflict=True)
            if committed:
                with self._lock:
                    self._nodes[prompt_id] = new_node
                    self._error = ""
                    self._last_sync_ms = self._clock()
                    self._changed()
                self._persist()
                self._finish_versions(prompt_id, new_node, note)
                return Result(ok=True, message=f"Saved as version {new_node['version']}.", prompt_id=prompt_id,
                              version=new_node["version"], warnings=warnings)
        # Not sent: work locally and wait in the pending list.
        with self._lock:
            self._nodes[prompt_id] = new_node
            self._pending[prompt_id] = {"base_version": base, "node": new_node, "note": note, "stage": "node",
                                        "create": create or bool(pend and pend.get("create"))}
            self._changed()
        self._persist()
        if online and not tried:
            self.flush()                          # older work was already waiting: send it together, at once
            if prompt_id not in self._pending:
                return Result(ok=True, message=f"Saved as version {new_node['version']}.", prompt_id=prompt_id,
                              version=new_node["version"], warnings=warnings)
            if prompt_id in self._conflicts:
                return Result(ok=True, queued=True, conflict=True, prompt_id=prompt_id, version=new_node["version"],
                              warnings=warnings, message="Saved here, but the prompt was changed elsewhere. "
                              "Choose 'keep mine' or 'take theirs'.")
        why = "Saved on this computer; it will be sent when the online copy is reachable."
        if not self._safe(self._switch):
            why = "Saved on this computer only ('Keep prompts online' is off)."
        elif not self._safe(self._configured):
            why = "Saved on this computer only (no Firebase is set up)."
        return Result(ok=True, queued=True, message=why, prompt_id=prompt_id, version=new_node["version"], warnings=warnings)

    def _write_node(self, prompt_id: str, new_node: dict, base: int, create: bool) -> tuple:
        """The conditional write: only if the prompt still has the version we started from."""
        wire = self._wire_node(new_node)

        def fn(current):
            if current is None:
                if create or base == 0:
                    return wire
                raise TransactionAbort()          # it was deleted elsewhere
            if create or _int(current.get("version")) != base:
                raise TransactionAbort()
            return wire
        return self.cloud.transaction(f"{PROMPTS_ROOT}/{prompt_id}", fn)

    def _finish_versions(self, prompt_id: str, node: dict, note: str) -> None:
        """After the node is committed: write the history entry and prune. A failure here keeps the work pending at stage 'version'."""
        record = {"saved_at": SERVER_TIME, "saved_by": self._who(), "blocks": json.loads(json.dumps(node["blocks"]))}
        if note:
            record["note"] = note
        try:
            self.cloud.set(f"{VERSIONS_ROOT}/{prompt_id}/{node['version']}", record)
        except Exception as e:  # noqa: BLE001
            self._record_error(e)
            with self._lock:
                self._pending[prompt_id] = {"base_version": node["version"], "node": node, "note": note,
                                            "stage": "version", "create": False}
            self._persist()
            return
        with self._lock:
            self._pending.pop(prompt_id, None)
        self._persist()
        self._prune(prompt_id)

    def _prune(self, prompt_id: str) -> int:
        """Deletes the oldest history entries beyond PROMPT_VERSIONS_KEEP. Returns how many were deleted. Never raises."""
        try:
            keep = int(self._keep())
            numbers = sorted(int(k) for k in (self.cloud.keys(f"{VERSIONS_ROOT}/{prompt_id}") or []) if str(k).isdigit())
            old = numbers[:-keep] if keep > 0 else numbers
            for n in old:
                self.cloud.delete(f"{VERSIONS_ROOT}/{prompt_id}/{n}")
            return len(old)
        except Exception as e:  # noqa: BLE001
            self._record_error(e)
            return 0

    # ---- sending what waits -------------------------------------------------------------------------------------
    def flush(self) -> dict:
        """Sends saves and active choices that wait (never a prompt with an unresolved conflict). Never raises; returns the status."""
        if not self.enabled():
            return self.status()
        if not self._sync_lock.acquire(blocking=False):
            return self.status()
        try:
            broke = False
            for pid in list(self._pending):
                with self._lock:
                    item = self._pending.get(pid)
                    if item is None or pid in self._conflicts:
                        continue
                node = item["node"]
                if item["stage"] == "node":
                    errors = self._guard_node(node)
                    if errors:                    # for example a key saved after this was queued: never upload it
                        self._record_error(RuntimeError("A waiting prompt was not sent: " + errors[0]))
                        continue
                    try:
                        committed, current = self._write_node(pid, node, item["base_version"], item["create"])
                    except Exception as e:  # noqa: BLE001
                        self._record_error(e)
                        broke = True
                        break
                    if not committed:
                        try:                      # what is there now (an aborted Firebase transaction returns nothing)
                            current = self.cloud.get(f"{PROMPTS_ROOT}/{pid}")
                        except Exception as e:  # noqa: BLE001
                            self._record_error(e)
                            broke = True
                            break
                        theirs = self._clean_node(pid, current, [], None) if current else None
                        with self._lock:
                            self._conflicts[pid] = {"theirs": theirs}
                        self._persist()
                        continue
                    with self._lock:
                        self._last_sync_ms = self._clock()
                        self._error = ""
                self._finish_versions(pid, node, item.get("note", ""))
            if not broke:
                for role, pid in list(self._pending_active.items()):
                    try:
                        self.cloud.set(f"{ACTIVE_ROOT}/{role}", pid)
                    except Exception as e:  # noqa: BLE001
                        self._record_error(e)
                        break
                    with self._lock:
                        self._pending_active.pop(role, None)
                        self._error = ""
                self._persist()
        except Exception as e:  # noqa: BLE001
            log.exception("Could not send the waiting prompts")
            self._record_error(e)
        finally:
            self._sync_lock.release()
        return self.status()

    def resolve_conflict(self, prompt_id: str, keep: str) -> Result:
        """'mine': upload my copy on top of what is there now. 'theirs': drop my copy and take what is online."""
        try:
            with self._lock:
                conflict = self._conflicts.get(prompt_id)
                item = self._pending.get(prompt_id)
                if conflict is None or item is None:
                    return _fail("There is no conflict to settle for this prompt.", prompt_id)
                theirs = conflict["theirs"]
                if keep == "theirs":
                    self._pending.pop(prompt_id, None)
                    self._conflicts.pop(prompt_id, None)
                    if theirs is not None:
                        self._nodes[prompt_id] = theirs
                    elif prompt_id not in _BUILTIN_ROLE:
                        self._nodes.pop(prompt_id, None)
                    else:
                        self._nodes.pop(prompt_id, None)
                    self._changed()
                    self._persist()
                    return Result(ok=True, message="Took the online version.", prompt_id=prompt_id,
                                  version=(theirs or {}).get("version", 0))
                if keep != "mine":
                    return _fail("Choose 'mine' or 'theirs'.", prompt_id)
                base = theirs["version"] if theirs else 0
                item["base_version"] = base
                item["create"] = False if theirs else item.get("create", False)
                item["node"] = dict(item["node"], version=base + 1)
                item["stage"] = "node"
                self._nodes[prompt_id] = item["node"]
                self._conflicts.pop(prompt_id, None)
                self._changed()
            self._persist()
            self.flush()
            with self._lock:
                still = prompt_id in self._pending
                version = self._nodes.get(prompt_id, {}).get("version", 0)
            return Result(ok=True, queued=still, prompt_id=prompt_id, version=version,
                          conflict=prompt_id in self._conflicts,
                          message="Kept my copy." + (" It will be sent when the online copy is reachable." if still else ""))
        except Exception as e:  # noqa: BLE001
            log.exception("Could not settle the prompt conflict")
            return _fail(settings_store.redact_text(f"Could not settle the conflict ({type(e).__name__})."), prompt_id)

    # ---- deleting, choosing, history ---------------------------------------------------------------------------------
    def delete(self, prompt_id: str) -> Result:
        """Deletes a custom prompt (and its history). Built-in prompts and the one a role uses cannot be deleted.
        Needs the online copy."""
        try:
            with self._lock:
                node = self._effective_node(prompt_id)
                if node is None:
                    return _fail("No such prompt.", prompt_id)
                if node["builtin"] or prompt_id in _BUILTIN_ROLE:
                    return _fail("A built-in prompt cannot be deleted. Use 'Reset' to return it to the shipped text.", prompt_id)
                if self.active_id(node["role"]) == prompt_id or self._active.get(node["role"]) == prompt_id:
                    return _fail("This prompt is in use. Choose another prompt for this role first.", prompt_id)
                if not self.enabled():
                    return _fail("Deleting needs the online copy: turn on 'Keep prompts online' and check the connection.", prompt_id)
            try:
                self.cloud.delete(f"{PROMPTS_ROOT}/{prompt_id}")
            except Exception as e:  # noqa: BLE001
                return _fail(self._record_error(e), prompt_id)
            try:
                self.cloud.delete(f"{VERSIONS_ROOT}/{prompt_id}")
            except Exception as e:  # noqa: BLE001 - leftover history is harmless
                self._record_error(e)
            with self._lock:
                self._nodes.pop(prompt_id, None)
                self._pending.pop(prompt_id, None)
                self._conflicts.pop(prompt_id, None)
                self._changed()
            self._persist()
            return Result(ok=True, message="Deleted.", prompt_id=prompt_id)
        except Exception as e:  # noqa: BLE001
            log.exception("Prompt delete failed")
            return _fail(settings_store.redact_text(f"Could not delete ({type(e).__name__})."), prompt_id)

    def set_active(self, role: str, prompt_id: str) -> Result:
        """Chooses which prompt a role uses. Applies at once; the choice is sent when the online copy is reachable."""
        try:
            with self._lock:
                node = self._effective_node(prompt_id)
                if role not in ROLES or node is None or node["role"] != role:
                    return _fail("That prompt does not belong to this role.", prompt_id)
                self._active[role] = prompt_id
                self._pending_active[role] = prompt_id
                self._changed()
            self._persist()
            if self.enabled():
                self.flush()
            with self._lock:
                queued = role in self._pending_active
            return Result(ok=True, queued=queued, prompt_id=prompt_id,
                          message="Now in use." + (" The choice will be sent when the online copy is reachable." if queued else ""))
        except Exception as e:  # noqa: BLE001
            log.exception("set_active failed")
            return _fail(settings_store.redact_text(f"Could not change the prompt in use ({type(e).__name__})."), prompt_id)

    def history(self, prompt_id: str) -> dict:
        """The saved versions of a prompt, newest first: {ok, versions: [{n, saved_at, saved_by, note, chars}], message}."""
        if not self.enabled():
            return {"ok": False, "versions": [], "message": "History is kept online; it is not available now."}
        try:
            raw = self.cloud.get(f"{VERSIONS_ROOT}/{prompt_id}") or {}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "versions": [], "message": self._record_error(e)}
        if isinstance(raw, list):
            # Firebase hands back keys 1..N with no gaps as a list (index 0 empty), not as an object.
            raw = {str(i): rec for i, rec in enumerate(raw) if rec is not None}
        versions = []
        for k, rec in (raw.items() if isinstance(raw, dict) else []):
            if not str(k).isdigit() or not isinstance(rec, dict):
                continue
            blocks = rec.get("blocks") if isinstance(rec.get("blocks"), dict) else {}
            versions.append({"n": int(k), "saved_at": _int(rec.get("saved_at")), "saved_by": str(rec.get("saved_by") or ""),
                             "note": str(rec.get("note") or ""),
                             "chars": sum(len(str(b.get("text", ""))) for b in blocks.values() if isinstance(b, dict))})
        versions.sort(key=lambda v: -v["n"])
        return {"ok": True, "versions": versions, "message": ""}

    # ---- seeding and the whole sync ------------------------------------------------------------------------------------
    def seed(self) -> dict:
        """Writes the shipped editable blocks, the built-in prompts and the active choices into Firebase, ONLY where they do not
        exist yet (a missing block of an older template is added too). Never overwrites. Returns {"written": n, "error": ""}."""
        if not self.enabled():
            return {"written": 0, "error": ""}
        written = 0
        try:
            for role in ROLES:
                pid = BUILTIN_IDS[role]
                shipped = {b.id: b for b in prompt_templates.load_shipped(role)}
                want = {bid: {"text": shipped[bid].text, "enabled": True} for bid in editable_layout_ids(role)}
                want[EXTRA_ID] = {"text": "", "enabled": True}
                fresh = {"name": BUILTIN_NAMES[role], "description": "", "role": role, "builtin": True,
                         "template_version": prompt_templates.TEMPLATE_VERSION, "version": 0,
                         "updated_at": SERVER_TIME, "updated_by": self._who(), "blocks": want}

                def fn(current, fresh=fresh, want=want):
                    if current is None:
                        return fresh
                    blocks = dict(current.get("blocks") or {})
                    added = {b: v for b, v in want.items() if b not in blocks}
                    if not added and _int(current.get("template_version"), 1) >= prompt_templates.TEMPLATE_VERSION:
                        raise TransactionAbort()
                    merged = dict(current)
                    merged["blocks"] = dict(blocks, **added)
                    merged["template_version"] = prompt_templates.TEMPLATE_VERSION
                    return merged
                committed, _ = self.cloud.transaction(f"{PROMPTS_ROOT}/{pid}", fn)
                written += 1 if committed else 0
                if self.cloud.get(f"{ACTIVE_ROOT}/{role}") is None:
                    self.cloud.set(f"{ACTIVE_ROOT}/{role}", pid)
                    written += 1
        except Exception as e:  # noqa: BLE001
            return {"written": written, "error": self._record_error(e)}
        return {"written": written, "error": ""}

    def sync(self) -> dict:
        """The whole background routine: read Firebase, send what waits, seed what is missing, read again if anything was
        seeded. Never raises, never blocks anything but the thread that calls it. Returns the status."""
        if not self.enabled():
            return self.status()
        if self.refresh()["last_error"]:
            return self.status()
        self.flush()
        seeded = self.seed()
        if seeded["written"]:
            self.refresh()
        return self.status()


# ----------------------------------------------------------------------------
# Shared instance (5D wires it to the consumers)
# ----------------------------------------------------------------------------

_store: Optional[PromptStore] = None
_store_lock = threading.Lock()


def get_store() -> PromptStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = PromptStore()
        return _store


def reset_store_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None
