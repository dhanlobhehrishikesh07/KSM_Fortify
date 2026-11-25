"""
settings_store.py
-----------------
One common place for every setting and secret, outside the project folder, so
upgrading or replacing the project never means re-creating .env files.

Where things live (the "config folder"):
    Windows  %APPDATA%\\KSM\\
    macOS    ~/Library/Application Support/KSM/
    Linux    $XDG_CONFIG_HOME/ksm/   (default ~/.config/ksm/)
    override with the environment variable KSM_CONFIG_DIR

Inside it:
    settings.json            normal settings (names, models, switches). Backup: settings.json.bak
    secrets.json             ONLY used when the OS keychain is not available (owner-only file)
    firebase/                imported Firebase service-account file (owner-only)
    settings_history.log     who changed what and when. Never contains values.

Secrets (API keys) go to the OS keychain through the optional `keyring`
package (Windows Credential Manager, macOS Keychain, Linux Secret Service).

Which value wins for a setting (first match):
    1. a real environment variable        (source "env")
    2. this store                         (source "store")
    3. the legacy project .env file       (source "dotenv")
    4. the built-in default               (source "default")

No GUI code in here. web_api.py exposes it to the Settings tab.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import logging

# not app.logger: logger.py imports config, and config imports this module
log = logging.getLogger("ksm.settings")

try:  # optional: without it, secrets fall back to an owner-only file
    import keyring
    from keyring.errors import KeyringError
except ImportError:  # pragma: no cover - optional dependency
    keyring = None
    KeyringError = Exception  # type: ignore[assignment,misc]

KEYRING_SERVICE = "KSM"
SETTINGS_VERSION = 1

# ----------------------------------------------------------------------------
# What settings exist
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    group: str
    kind: str = "text"            # text | secret | bool | int | float | choice | url | path
    default: str = ""
    help: str = ""
    restart: bool = False          # needs an app restart to take effect
    choices: tuple = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    link: str = ""                 # where to get the key
    service: str = ""              # groups settings for the "Test" buttons: groq | gemini | firebase
    placeholder: str = ""
    # Phase 5: where the setting lives (see ROADMAP Phase 5 and firebase/DATA_LAYOUT.md). Every setting has one.
    scope: str = ""
    # True = defined and validated, but not shown in the Settings tab yet (its feature is built in a later step).
    hidden: bool = False


# Settings scopes (Phase 5). Only COMMON and PER_DEVICE settings may ever be stored in Firebase.
SCOPE_COMMON = "common"          # Firebase, one value for all clients
SCOPE_PER_DEVICE = "per_device"  # Firebase, one value per device
SCOPE_LOCAL = "local_only"       # this device only (needed to reach Firebase)
SCOPE_SECRET = "secret"          # never in Firebase (API keys)
SCOPES = (SCOPE_COMMON, SCOPE_PER_DEVICE, SCOPE_LOCAL, SCOPE_SECRET)


GROUPS = [
    ("identity", "Identity", "Names the assistant uses in chat and prompts."),
    ("groq", "Groq (AI chat)", "Powers the AI Chat tab and task extraction."),
    ("gemini", "Gemini (strategy + screen scan)", "Powers Strategy Audit and Screen Scan."),
    ("firebase", "Mobile sync (Firebase)", "Optional phone sync. Off by default."),
    ("cloud", "Online copy (Firebase)", "Keep a copy of your data in your own Firebase database. Each kind is "
              "its own switch and all start off. Needs the Mobile sync settings above."),
    ("screen", "Screen scan tuning", "Screenshot size and timing."),
    ("app", "App, data and debugging", "Files and log detail."),
]

SCHEMA: list[SettingDef] = [
    SettingDef("ASSISTANT_NAME", "Assistant name", "identity", default="Karauli Shankar Mahadev", scope=SCOPE_COMMON),
    SettingDef("USER_NAME", "Your name", "identity", default="User", scope=SCOPE_COMMON),
    SettingDef("USER_PROFILE", "Short profile", "identity", default="a student at IIIT Raichur",
               help="One short phrase the AI uses to describe you. Goes into the prompts.", scope=SCOPE_COMMON),

    SettingDef("GROQ_API_KEY", "Groq API key", "groq", kind="secret", service="groq",
               link="https://console.groq.com/keys", placeholder="gsk_...", scope=SCOPE_SECRET),
    SettingDef("GROQ_MODEL_ID", "Groq model", "groq", default="llama-3.3-70b-versatile", service="groq", scope=SCOPE_COMMON),

    SettingDef("GEMINI_API_KEY", "Gemini API key", "gemini", kind="secret", service="gemini",
               link="https://aistudio.google.com/apikey", placeholder="AIza...", scope=SCOPE_SECRET),
    SettingDef("GEMINI_MODEL_ID", "Strategy model", "gemini", default="gemini-2.0-flash", service="gemini", scope=SCOPE_COMMON),
    SettingDef("SCREEN_MODEL_ID", "Screen scan model", "gemini", default="gemini-2.5-flash-lite", service="gemini",
               help="A fast vision model. Uses the same Gemini key.", scope=SCOPE_COMMON),

    SettingDef("ENABLE_FIREBASE", "Enable mobile sync", "firebase", kind="bool", default="false", restart=True,
               service="firebase", scope=SCOPE_LOCAL),
    SettingDef("FIREBASE_DATABASE_URL", "Realtime Database URL", "firebase", kind="url", restart=True,
               service="firebase", placeholder="https://your-project-default-rtdb.firebaseio.com/", scope=SCOPE_LOCAL),
    SettingDef("FIREBASE_SERVICE_ACCOUNT_PATH", "Service account file", "firebase", kind="path", restart=True,
               default="serviceAccountKey.json", service="firebase",
               help="Use 'Import file' below: the file is copied into the config folder.", scope=SCOPE_LOCAL),

    SettingDef("SYNC_TASKS", "Keep tasks online", "cloud", kind="bool", default="false",
               help="Copies your tasks to Firebase. The local file stays the working copy. Applies at once.", scope=SCOPE_COMMON),
    SettingDef("SYNC_CHAT", "Keep chat history online", "cloud", kind="bool", default="false",
               help="Saves your AI chat messages (with any key removed) so the AI can remember across sessions. Applies at once.", scope=SCOPE_COMMON),
    SettingDef("SYNC_AUDITS_SCANS", "Keep audits and screen scans online", "cloud", kind="bool", default="false",
               help="Saves the text of audits and scan answers, never the screenshot. Applies at once.", scope=SCOPE_COMMON),

    SettingDef("SCREEN_MAX_WIDTH", "Max screenshot width (px)", "screen", kind="int", default="1920",
               minimum=320, maximum=7680, scope=SCOPE_PER_DEVICE),
    SettingDef("SCREEN_JPEG_QUALITY", "JPEG quality", "screen", kind="int", default="85", minimum=10, maximum=100, scope=SCOPE_PER_DEVICE),
    SettingDef("SCREEN_HIDE_DELAY", "Hide delay (seconds)", "screen", kind="float", default="0.8",
               minimum=0, maximum=10, scope=SCOPE_PER_DEVICE),

    SettingDef("DATA_FILE_NAME", "Task data file name", "app", default="matrix_data_pro.json", restart=True,
               help="Lives in the project's data/ folder.", scope=SCOPE_PER_DEVICE),
    SettingDef("LOG_LEVEL", "Log level", "app", kind="choice", default="DEBUG",
               choices=("DEBUG", "INFO", "WARNING", "ERROR"), scope=SCOPE_PER_DEVICE),
    SettingDef("DEBUG_UI", "Open browser dev-tools", "app", kind="bool", default="false", restart=True, scope=SCOPE_PER_DEVICE),

    # --- Phase 5 (shown since 5E, except THEME, which the theme buttons set)
    SettingDef("SYNC_PROMPTS", "Keep prompts online", "cloud", kind="bool", default="true", scope=SCOPE_COMMON,
               help="Keeps your editable prompt parts in your own Firebase. Applies at once."),
    SettingDef("SYNC_SETTINGS", "Keep settings online", "cloud", kind="bool", default="true", scope=SCOPE_LOCAL,
               help="Keeps your settings in your own Firebase (never the API keys). Stays on this device because it controls the sync itself."),
    SettingDef("PROMPT_VERSIONS_KEEP", "Prompt versions kept", "app", kind="int", default="30", minimum=5, maximum=200,
               scope=SCOPE_COMMON, help="How many saved versions of each prompt are kept."),
    SettingDef("THEME", "Theme", "app", kind="choice", default="scifi", scope=SCOPE_PER_DEVICE, hidden=True,
               choices=("scifi", "saffron", "slate", "forest")),
]

BY_KEY: dict[str, SettingDef] = {d.key: d for d in SCHEMA}
SECRET_KEYS = [d.key for d in SCHEMA if d.kind == "secret"]


def keys_with_scope(scope: str) -> list[str]:
    """Setting keys with the given scope, in schema order."""
    return [d.key for d in SCHEMA if d.scope == scope]

TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off", ""}


class SettingsError(ValueError):
    """A user-presentable problem (bad value, unknown key, unreadable file)."""


@dataclass
class Validation:
    ok: bool
    value: str = ""
    error: str = ""
    warning: str = ""


def mask_secret(value: str) -> str:
    """Safe-to-show version of a secret: first 4 and last 4 characters only for long keys."""
    if not value:
        return ""
    if len(value) < 12:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def validate_value(defn: SettingDef, raw) -> Validation:
    """Checks (and tidies) one value. Never raises."""
    value = "" if raw is None else str(raw).strip()
    kind = defn.kind
    if kind == "secret":
        if not value:
            return Validation(False, error="Paste a key, or use Delete to remove the stored one.")
        if any(ch.isspace() for ch in value):
            return Validation(False, error="A key cannot contain spaces or line breaks. Check the paste.")
        if len(value) < 8:
            return Validation(False, error="That is too short to be an API key.")
        warning = ""
        if defn.key == "GROQ_API_KEY" and not value.startswith("gsk_"):
            warning = "Groq keys usually start with 'gsk_'. Saved anyway; use Test to check it."
        if defn.key == "GEMINI_API_KEY" and not value.startswith("AIza"):
            warning = "Gemini keys usually start with 'AIza'. Saved anyway; use Test to check it."
        return Validation(True, value, warning=warning)
    if kind == "bool":
        low = value.lower()
        if low in TRUE_WORDS:
            return Validation(True, "true")
        if low in FALSE_WORDS:
            return Validation(True, "false")
        return Validation(False, error="Use true or false.")
    if kind in ("int", "float"):
        try:
            num = int(value) if kind == "int" else float(value)
        except ValueError:
            return Validation(False, error="Must be a number.")
        if defn.minimum is not None and num < defn.minimum:
            return Validation(False, error=f"Must be at least {defn.minimum:g}.")
        if defn.maximum is not None and num > defn.maximum:
            return Validation(False, error=f"Must be at most {defn.maximum:g}.")
        return Validation(True, str(num))
    if kind == "choice":
        for choice in defn.choices:
            if choice.upper() == value.upper():
                return Validation(True, choice)
        return Validation(False, error="Pick one of: " + ", ".join(defn.choices) + ".")
    if kind == "url":
        if not value:
            return Validation(True, "")
        if not value.lower().startswith(("https://", "http://")):
            return Validation(False, error="Must start with https://")
        warning = ""
        if "firebaseio.com" not in value and "firebasedatabase.app" not in value:
            warning = "This does not look like a Firebase Realtime Database URL."
        return Validation(True, value, warning=warning)
    if kind == "path":
        return Validation(True, value)
    # plain text
    if defn.key in ("ASSISTANT_NAME", "USER_NAME", "SCREEN_MODEL_ID", "GROQ_MODEL_ID", "GEMINI_MODEL_ID",
                    "DATA_FILE_NAME") and not value:
        return Validation(False, error="This cannot be empty. Use Reset to go back to the default.")
    if defn.key == "DATA_FILE_NAME" and any(c in value for c in "/\\") :
        return Validation(False, error="Give a file name only, not a path.")
    if len(value) > 300:
        return Validation(False, error="Too long (300 characters at most).")
    return Validation(True, value)


# ----------------------------------------------------------------------------
# Config folder
# ----------------------------------------------------------------------------


def config_dir() -> Path:
    """The common settings folder. KSM_CONFIG_DIR wins; otherwise per-OS standard place."""
    override = os.environ.get("KSM_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "KSM"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "KSM"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ksm"


def _private_file(path: Path) -> None:
    """Best effort: owner-only permissions (no effect on Windows)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _atomic_write(path: Path, text: str, private: bool = False, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        try:
            data = path.read_bytes()
            bak = path.with_name(path.name + ".bak")
            bak.write_bytes(data)
            if private:
                _private_file(bak)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if private:
            _private_file(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------------
# Device identity (Phase 5)
# ----------------------------------------------------------------------------

DEVICE_FILE = "device.json"
_DEVICE_ID_RE = re.compile(r"^d-[0-9a-f]{8}$")


def device_identity(folder: Optional[Path] = None) -> dict:
    """The id and friendly name of THIS installation: {"device_id": "d-1a2b3c4d", "name": "..."}.

    Made the first time it is asked for and kept in device.json in the config folder; the id
    never changes after that. The name defaults to the computer's name. Later Phase 5 steps
    register it in Firebase (ksm/config/devices/<id>) so per-device settings have a home.
    Nothing here runs at import time and nothing is created until this is called."""
    path = (folder or config_dir()) / DEVICE_FILE
    data: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    device_id = data.get("device_id")
    name = data.get("name")
    if isinstance(device_id, str) and _DEVICE_ID_RE.match(device_id) and isinstance(name, str) and name.strip():
        return {"device_id": device_id, "name": name.strip()[:80]}
    if not (isinstance(device_id, str) and _DEVICE_ID_RE.match(device_id)):
        device_id = "d-" + secrets.token_hex(4)
    if not (isinstance(name, str) and name.strip()):
        try:
            name = socket.gethostname().strip()[:80]
        except OSError:
            name = ""
        name = name or "This PC"
    result = {"device_id": device_id, "name": name.strip()[:80]}
    try:
        _atomic_write(path, json.dumps(result, indent=2) + "\n")
    except OSError as e:
        log.warning("Could not save %s (%s); the device id will be new next time", path, e)
    return result


# ----------------------------------------------------------------------------
# Secret backends
# ----------------------------------------------------------------------------


class SecretBackend:
    name = "?"
    secure = True

    def get(self, key: str) -> Optional[str]: raise NotImplementedError
    def set(self, key: str, value: str) -> None: raise NotImplementedError
    def delete(self, key: str) -> None: raise NotImplementedError


class KeychainBackend(SecretBackend):
    secure = True

    def __init__(self, kr=None):
        self._kr = kr or keyring
        try:
            self.name = "OS keychain (" + type(self._kr.get_keyring()).__name__ + ")"
        except Exception:  # noqa: BLE001
            self.name = "OS keychain"

    def get(self, key):
        return self._kr.get_password(KEYRING_SERVICE, key)

    def set(self, key, value):
        self._kr.set_password(KEYRING_SERVICE, key, value)

    def delete(self, key):
        try:
            self._kr.delete_password(KEYRING_SERVICE, key)
        except Exception as e:  # noqa: BLE001  (already gone is fine)
            if "not found" not in str(e).lower() and type(e).__name__ != "PasswordDeleteError":
                raise


class FileBackend(SecretBackend):
    """Fallback when no keychain exists. A plain JSON file readable only by you."""
    name = "protected file (no OS keychain found)"
    secure = False

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        with self._lock:
            return self._load().get(key)

    def set(self, key, value):
        with self._lock:
            data = self._load()
            data[key] = value
            _atomic_write(self.path, json.dumps(data, indent=2), private=True)

    def delete(self, key):
        with self._lock:
            data = self._load()
            if key in data:
                del data[key]
                _atomic_write(self.path, json.dumps(data, indent=2), private=True)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._load().keys())


def _keychain_usable(kr) -> bool:
    """True only when a real, working keychain is present (not the 'fail' or 'null' backend)."""
    if kr is None or os.environ.get("KSM_DISABLE_KEYRING", "").strip() in TRUE_WORDS:
        return False
    try:
        backend = kr.get_keyring()
        name = type(backend).__module__ + "." + type(backend).__name__
        if "fail" in name.lower() or "null" in name.lower():
            return False
        kr.get_password(KEYRING_SERVICE, "__ksm_probe__")  # raises if the keychain is locked or broken
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("OS keychain is not usable (%s); falling back to a protected file", e)
        return False


# ----------------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------------


@dataclass
class Entry:
    key: str
    value: str          # full value; NEVER send to the UI for secrets (use .shown)
    source: str         # env | store | dotenv | default
    defn: SettingDef
    shown: str = ""     # what the UI may display


class SettingsStore:
    def __init__(
        self,
        directory: Optional[Path] = None,
        dotenv_path: Optional[Path] = None,
        environ: Optional[dict] = None,
        secret_backend: Optional[SecretBackend] = None,
    ):
        self.dir = Path(directory) if directory else config_dir()
        self.settings_path = self.dir / "settings.json"
        self.history_path = self.dir / "settings_history.log"
        self.firebase_dir = self.dir / "firebase"
        self.dotenv_path = Path(dotenv_path) if dotenv_path else None
        # real environment, captured now so a later .env load cannot be confused with it
        self._environ = dict(os.environ if environ is None else environ)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[list[str]], None]] = []
        self._values: dict[str, str] = {}
        self._secret_names: set[str] = set()
        self._meta: dict = {}
        self._dotenv: dict[str, str] = {}
        self.load_problem = ""

        if secret_backend is not None:
            self.backend = secret_backend
        elif _keychain_usable(keyring):
            self.backend = KeychainBackend()
        else:
            self.backend = FileBackend(self.dir / "secrets.json")

        self.reload_from_disk()
        self._migrate_file_secrets_to_keychain()

    # -- loading -------------------------------------------------------------
    def reload_from_disk(self) -> None:
        with self._lock:
            self.load_problem = ""
            self._values, self._secret_names, self._meta = {}, set(), {}
            for candidate in (self.settings_path, self.settings_path.with_name("settings.json.bak")):
                if not candidate.exists():
                    continue
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("not an object")
                    vals = data.get("values", {})
                    self._values = {k: str(v) for k, v in vals.items() if k in BY_KEY and BY_KEY[k].kind != "secret"}
                    self._secret_names = {k for k in data.get("secret_names", []) if k in SECRET_KEYS}
                    self._meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
                    if candidate is not self.settings_path:
                        self.load_problem = "settings.json was damaged; restored from the last backup."
                    break
                except (OSError, ValueError) as e:
                    log.error("Could not read %s: %s", candidate, e)
                    self.load_problem = f"{candidate.name} could not be read ({e})."
            self._dotenv = self._read_dotenv()

    def _read_dotenv(self) -> dict[str, str]:
        if not self.dotenv_path or not self.dotenv_path.exists():
            return {}
        try:
            from dotenv import dotenv_values
            return {k: ("" if v is None else v) for k, v in dotenv_values(self.dotenv_path).items() if k in BY_KEY}
        except Exception as e:  # noqa: BLE001
            log.warning("Could not read %s: %s", self.dotenv_path, e)
            return {}

    def _save(self) -> None:
        payload = {
            "version": SETTINGS_VERSION,
            "values": self._values,
            "secret_names": sorted(self._secret_names),
            "meta": self._meta,
        }
        _atomic_write(self.settings_path, json.dumps(payload, indent=2, sort_keys=True), backup=True)

    def _migrate_file_secrets_to_keychain(self) -> None:
        """If a keychain appeared after secrets were saved in the fallback file, move them."""
        if not self.backend.secure:
            return
        fb = FileBackend(self.dir / "secrets.json")
        if not fb.path.exists():
            return
        moved = 0
        for k in fb.keys():
            v = fb.get(k)
            if v and k in SECRET_KEYS:
                try:
                    self.backend.set(k, v)
                    fb.delete(k)
                    self._secret_names.add(k)
                    moved += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not move %s into the keychain: %s", k, e)
        if moved:
            with self._lock:
                self._save()
            self._audit("migrate", "*", f"{moved} secret(s) moved into the OS keychain")
        if not fb.keys():
            try:
                fb.path.unlink()
            except OSError:
                pass

    # -- history ---------------------------------------------------------------
    def _audit(self, action: str, key: str, note: str = "") -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {action:<7} | {key} | {note}".rstrip(" |")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _private_file(self.history_path)
        except OSError as e:
            log.warning("Could not write settings history: %s", e)

    def history(self, limit: int = 60) -> list[str]:
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return list(reversed(lines[-limit:]))

    # -- reading ------------------------------------------------------------------
    def _secret(self, key: str) -> str:
        try:
            return self.backend.get(key) or ""
        except Exception as e:  # noqa: BLE001
            log.warning("Could not read %s from the secret backend: %s", key, e)
            return ""

    def resolve(self, key: str) -> Entry:
        defn = BY_KEY.get(key)
        if defn is None:
            raise SettingsError(f"Unknown setting: {key}")
        with self._lock:
            env_val = self._environ.get(key)
            if env_val is not None and env_val.strip() != "":
                value, source = env_val.strip(), "env"
            elif defn.kind == "secret" and key in self._secret_names and (sv := self._secret(key)):
                value, source = sv, "store"
            elif defn.kind != "secret" and key in self._values:
                value, source = self._values[key], "store"
            elif self._dotenv.get(key, "").strip() != "":
                value, source = self._dotenv[key].strip(), "dotenv"
            else:
                value, source = defn.default, "default"
        shown = mask_secret(value) if defn.kind == "secret" else value
        return Entry(key, value, source, defn, shown)

    def get(self, key: str) -> str:
        return self.resolve(key).value

    def value_ignoring_env(self, key: str) -> tuple:
        """(value, source) as if no real environment variable existed: saved value, then old .env, then default.
        Used by the Firebase settings sync (Phase 5C): an environment variable is never uploaded, and the saved value
        it hides is what stays in step with Firebase. Never returns a secret's value. Does not change any other behaviour."""
        defn = BY_KEY.get(key)
        if defn is None:
            raise SettingsError(f"Unknown setting: {key}")
        if defn.kind == "secret":
            return "", "secret"
        with self._lock:
            if key in self._values:
                return self._values[key], "store"
            if self._dotenv.get(key, "").strip() != "":
                return self._dotenv[key].strip(), "dotenv"
        return defn.default, "default"

    def snapshot(self) -> dict[str, str]:
        return {d.key: self.resolve(d.key).value for d in SCHEMA}

    def secret_values(self) -> list[str]:
        """Every secret currently in use (for log redaction). Never shown anywhere."""
        out = []
        for k in SECRET_KEYS:
            v = self.resolve(k).value
            if v and len(v) >= 8:
                out.append(v)
        return out

    def stored_source_conflict(self, key: str) -> str:
        """Non-empty text when a saved value is hidden by a real environment variable."""
        env_val = self._environ.get(key)
        if env_val is not None and env_val.strip() != "":
            return "A real environment variable named " + key + " overrides the saved value."
        return ""

    # -- writing -------------------------------------------------------------------
    def set(self, key: str, raw) -> Validation:
        defn = BY_KEY.get(key)
        if defn is None:
            return Validation(False, error=f"Unknown setting: {key}")
        result = validate_value(defn, raw)
        if not result.ok:
            return result
        with self._lock:
            before = self.resolve(key)
            try:
                if defn.kind == "secret":
                    self.backend.set(key, result.value)
                    self._secret_names.add(key)
                else:
                    self._values[key] = result.value
                self._save()
            except Exception as e:  # noqa: BLE001
                log.error("Saving %s failed: %s", key, e)
                return Validation(False, error=f"Could not save: {e}")
        self._audit("set", key, "secret updated" if defn.kind == "secret" else f"source was {before.source}")
        self._notify([key])
        return result

    def delete(self, key: str) -> Validation:
        defn = BY_KEY.get(key)
        if defn is None:
            return Validation(False, error=f"Unknown setting: {key}")
        with self._lock:
            try:
                if defn.kind == "secret":
                    self.backend.delete(key)
                    self._secret_names.discard(key)
                else:
                    self._values.pop(key, None)
                self._save()
            except Exception as e:  # noqa: BLE001
                return Validation(False, error=f"Could not delete: {e}")
        self._audit("delete", key)
        self._notify([key])
        return Validation(True)

    def has_stored(self, key: str) -> bool:
        defn = BY_KEY[key]
        return key in self._secret_names if defn.kind == "secret" else key in self._values

    def reveal(self, key: str) -> str:
        """Full secret for the Edit box. Recorded in the history."""
        defn = BY_KEY.get(key)
        if defn is None or defn.kind != "secret":
            raise SettingsError("Only secrets can be revealed.")
        value = self.resolve(key).value
        self._audit("reveal", key)
        return value

    # -- import / export ----------------------------------------------------------------
    def import_pairs(self, pairs: dict[str, str], overwrite: bool = False, origin: str = "import") -> dict:
        """Used for .env text and for restoring a backup. Returns counts and per-key messages."""
        imported, skipped, problems = [], [], []
        changed = []
        for key, raw in pairs.items():
            if key not in BY_KEY:
                continue
            defn = BY_KEY[key]
            if raw is None or str(raw).strip() == "":
                continue
            if self.has_stored(key) and not overwrite:
                skipped.append(key)
                continue
            if defn.kind == "path" and key == "FIREBASE_SERVICE_ACCOUNT_PATH":
                # a path inside the old project is replaced by the copy in the config folder
                p = Path(str(raw)).expanduser()
                if not p.is_absolute() and self.dotenv_path:
                    p = self.dotenv_path.parent / p
                if p.exists():
                    try:
                        self.import_firebase_file(p)
                        imported.append(key)
                        continue
                    except SettingsError as e:
                        problems.append(f"{key}: {e}")
                        continue
                problems.append(f"{key}: file not found at {p}")
                continue
            result = self.set(key, raw)
            if result.ok:
                imported.append(key)
                changed.append(key)
            else:
                problems.append(f"{key}: {result.error}")
        self._audit(origin, "*", f"{len(imported)} imported, {len(skipped)} skipped, {len(problems)} problems")
        return {"imported": imported, "skipped": skipped, "problems": problems}

    @staticmethod
    def parse_env_text(text: str) -> dict[str, str]:
        from dotenv import dotenv_values
        from io import StringIO
        return {k: ("" if v is None else v) for k, v in dotenv_values(stream=StringIO(text)).items()}

    def import_env_text(self, text: str, overwrite: bool = False) -> dict:
        pairs = self.parse_env_text(text or "")
        if not any(k in BY_KEY for k in pairs):
            raise SettingsError("No KSM settings were found in that text. Expected lines like GROQ_API_KEY=...")
        return self.import_pairs(pairs, overwrite=overwrite, origin="import")

    def legacy_summary(self) -> dict:
        """What an old-style install still holds in the project folder (for the first-run offer)."""
        env_keys = [k for k, v in self._dotenv.items() if str(v).strip()
                    and not str(v).strip().lower().startswith(("your_", "your-"))]
        usable = [k for k in env_keys if not self.has_stored(k) and k != "FIREBASE_SERVICE_ACCOUNT_PATH"]
        fb_file = None
        if self.dotenv_path:
            cand = self.dotenv_path.parent / "serviceAccountKey.json"
            cur = self.firebase_current()
            if cand.exists() and not (cur and cur.get("in_store")):
                fb_file = str(cand)
        return {
            "dotenv_found": bool(self._dotenv),
            "dotenv_keys": usable,
            "firebase_file": fb_file,
            "dismissed": bool(self._meta.get("legacy_offer_dismissed")),
        }

    def dismiss_legacy_offer(self) -> None:
        with self._lock:
            self._meta["legacy_offer_dismissed"] = True
            self._save()

    def import_legacy(self) -> dict:
        """One click: bring in the project's .env and serviceAccountKey.json."""
        info = self.legacy_summary()
        pairs = {k: self._dotenv[k] for k in info["dotenv_keys"]}
        result = self.import_pairs(pairs, overwrite=False, origin="import")
        if info["firebase_file"]:
            try:
                self.import_firebase_file(Path(info["firebase_file"]))
                result["imported"].append("FIREBASE_SERVICE_ACCOUNT_PATH")
            except SettingsError as e:
                result["problems"].append(str(e))
        self.dismiss_legacy_offer()
        return result

    def export_json(self) -> str:
        """Backup: normal settings only. Secrets are never exported."""
        with self._lock:
            return json.dumps(
                {
                    "ksm_settings_backup": SETTINGS_VERSION,
                    "exported": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "values": {k: v for k, v in sorted(self._values.items())},
                    "secrets_set": sorted(self._secret_names),
                    "note": "API keys are not included. Add them again after restoring.",
                },
                indent=2,
            )

    def import_json(self, text: str, overwrite: bool = True) -> dict:
        try:
            data = json.loads(text)
        except ValueError as e:
            raise SettingsError(f"That is not valid JSON ({e}).") from e
        if not isinstance(data, dict) or "ksm_settings_backup" not in data:
            raise SettingsError("That is not a KSM settings backup.")
        vals = {k: v for k, v in (data.get("values") or {}).items() if k in BY_KEY and BY_KEY[k].kind != "secret"
                and k != "FIREBASE_SERVICE_ACCOUNT_PATH"}
        return self.import_pairs(vals, overwrite=overwrite, origin="restore")

    # -- firebase service account ----------------------------------------------------------
    REQUIRED_FIREBASE_FIELDS = ("type", "project_id", "private_key", "client_email")

    @classmethod
    def check_firebase_json(cls, text: str) -> dict:
        try:
            data = json.loads(text)
        except ValueError as e:
            raise SettingsError(f"That file is not valid JSON ({e}).") from e
        if not isinstance(data, dict):
            raise SettingsError("That file is not a service account key.")
        missing = [f for f in cls.REQUIRED_FIREBASE_FIELDS if not data.get(f)]
        if missing:
            raise SettingsError("Not a service account key: missing " + ", ".join(missing) + ".")
        if data.get("type") != "service_account":
            raise SettingsError("Not a service account key (type is not 'service_account').")
        if "BEGIN PRIVATE KEY" not in str(data.get("private_key")):
            raise SettingsError("The private_key field does not look like a private key.")
        return data

    def import_firebase_text(self, text: str) -> dict:
        data = self.check_firebase_json(text)
        self.firebase_dir.mkdir(parents=True, exist_ok=True)
        dest = self.firebase_dir / "serviceAccountKey.json"
        _atomic_write(dest, json.dumps(data, indent=2), private=True, backup=True)
        self.set("FIREBASE_SERVICE_ACCOUNT_PATH", str(dest))
        self._audit("import", "FIREBASE_SERVICE_ACCOUNT_PATH", f"project {data.get('project_id')}")
        return {"project_id": data.get("project_id"), "client_email": data.get("client_email"), "path": str(dest)}

    def import_firebase_file(self, path: Path) -> dict:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise SettingsError(f"Could not read that file ({e}).") from e
        return self.import_firebase_text(text)

    def firebase_current(self) -> Optional[dict]:
        """Non-secret facts about the imported service account, or None."""
        path = self.get("FIREBASE_SERVICE_ACCOUNT_PATH")
        p = Path(path).expanduser()
        if not p.is_absolute() and self.dotenv_path:
            p = self.dotenv_path.parent / p
        if not p.is_absolute() or not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {"project_id": data.get("project_id", ""), "client_email": data.get("client_email", ""),
                    "path": str(p), "in_store": str(p).startswith(str(self.firebase_dir))}
        except (OSError, ValueError):
            return {"project_id": "", "client_email": "", "path": str(p), "in_store": False, "unreadable": True}

    def remove_firebase_file(self) -> None:
        dest = self.firebase_dir / "serviceAccountKey.json"
        try:
            if dest.exists():
                dest.unlink()
        except OSError as e:
            raise SettingsError(f"Could not delete the file ({e}).") from e
        self.delete("FIREBASE_SERVICE_ACCOUNT_PATH")
        self._audit("delete", "FIREBASE_SERVICE_ACCOUNT_PATH", "file removed")

    # -- change notification ----------------------------------------------------------------
    def add_listener(self, fn: Callable[[list[str]], None]) -> None:
        self._listeners.append(fn)

    def _notify(self, keys: list[str]) -> None:
        for fn in list(self._listeners):
            try:
                fn(keys)
            except Exception:  # noqa: BLE001
                log.exception("Settings listener failed")

    def notify_all(self, keys: Optional[list[str]] = None) -> None:
        self._notify(keys or [d.key for d in SCHEMA])

    # -- for the UI ----------------------------------------------------------------------------
    def describe(self) -> dict:
        entries = []
        for d in SCHEMA:
            if d.hidden:
                continue
            e = self.resolve(d.key)
            entries.append({
                "key": d.key, "label": d.label, "group": d.group, "kind": d.kind,
                "value": "" if d.kind == "secret" else e.value,
                "shown": e.shown, "is_set": bool(e.value),
                "source": e.source, "stored": self.has_stored(d.key),
                "default": "" if d.kind == "secret" else d.default,
                "help": d.help, "restart": d.restart, "choices": list(d.choices),
                "link": d.link, "service": d.service, "placeholder": d.placeholder,
                "minimum": d.minimum, "maximum": d.maximum, "scope": d.scope,
                "conflict": self.stored_source_conflict(d.key) if self.has_stored(d.key) else "",
            })
        return {
            "groups": [{"id": g, "title": t, "blurb": b} for g, t, b in GROUPS],
            "entries": entries,
            "folder": str(self.dir),
            "backend": self.backend.name,
            "backend_secure": self.backend.secure,
            "load_problem": self.load_problem,
            "legacy": self.legacy_summary(),
            "firebase_file": self.firebase_current(),
        }


# ----------------------------------------------------------------------------
# Log / message redaction
# ----------------------------------------------------------------------------

def redact_text(text, store: Optional["SettingsStore"] = None) -> str:
    """Replaces any secret that appears inside `text` with ***. Used for logs and error messages."""
    text = "" if text is None else str(text)
    st = store or _store
    if st is None:
        return text
    try:
        for secret in st.secret_values():
            if secret in text:
                text = text.replace(secret, "***")
    except Exception:  # noqa: BLE001  (redaction must never break logging)
        pass
    return text


# ----------------------------------------------------------------------------
# Shared instance used by config.py
# ----------------------------------------------------------------------------

_store: Optional[SettingsStore] = None
_store_lock = threading.Lock()


def init_store(dotenv_path: Optional[Path] = None, directory: Optional[Path] = None) -> SettingsStore:
    """Creates the shared store (once). A second call returns the same store."""
    global _store
    with _store_lock:
        if _store is None:
            try:
                _store = SettingsStore(directory=directory, dotenv_path=dotenv_path)
            except OSError as e:  # unusable folder: run from .env / defaults only
                log.error("Settings folder unusable (%s); using a temporary one", e)
                _store = SettingsStore(directory=Path(tempfile.mkdtemp(prefix="ksm-settings-")), dotenv_path=dotenv_path)
        return _store


def get_store() -> SettingsStore:
    return init_store()


def reset_store_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None
