# Karauli Shankar Mahadev — Central Command

A personal Eisenhower-Matrix task manager with an AI assistant that turns
natural conversation into structured tasks, plus an AI "Chief Strategy
Officer" that audits your workload.

## What changed from the original version

- **One app, seven tabs** (Matrix / AI Chat / Strategy Audit / Screen Scan / Mobile Sync / Settings / Debug Log)
  instead of three separate programs talking over raw TCP sockets. Same
  capabilities, far less to keep running and debug.
- **GUI rebuilt on pywebview instead of tkinter.** Tkinter can't do the
  sci-fi HUD look (smooth gradients, live theme switching, scrollable
  cards) — pywebview opens a real window backed by your OS's own web
  engine (WebView2 on Windows, WebKit on macOS, WebKitGTK on Linux) and
  renders actual HTML/CSS/JS (`app/gui/webui/`), while every existing
  Python module (`data_store.py`, `ai_assistant.py`, `strategy_officer.py`,
  `firebase_bridge.py`) is untouched — the new `app/gui/web_api.py` just
  exposes them to the page. Four built-in themes (Sci-fi blue / Sacred
  saffron / Minimal slate / Forest emerald) are switchable live from the
  top bar and remembered between runs.
- **No secrets in code.** The original files had a live Groq API key, a
  live Gemini API key, and a Firebase service-account private key hardcoded
  in plaintext. All three are now read from a local `.env` file that is
  git-ignored and never shipped.
  **⚠️ If you used the old files: rotate/revoke those three credentials —
  Groq console, Google AI Studio, and your Firebase project's service
  accounts — since they were exposed in plaintext.**
- Each "ministry" is still its own module (`data_store.py`, `ai_assistant.py`,
  `strategy_officer.py`, `firebase_bridge.py`) so you can read, test, or
  extend one without touching the others.
- Your 14 existing tasks were carried over into `data/matrix_data_pro.json`.
- Firebase / mobile sync is now optional and off by default — the app runs
  fully offline (matrix + manual tasks) with zero setup. Turn it on when
  you're ready to wire up the phone app again.

## New: Screen Scan + Debug Log

- **Screen Scan tab** — press *SCAN MY SCREEN*: the app minimizes itself, takes a
  screenshot (`mss`), shrinks it to `SCREEN_MAX_WIDTH`, sends it with
  `prompts/screen_analyzer.txt` to Gemini (`SCREEN_MODEL_ID`, default
  `gemini-2.5-flash-lite`, free tier), and shows the answer (apps, what they do,
  key text) next to a preview of what was sent. You can type an optional extra
  question. Uses the same `GEMINI_API_KEY` as Strategy Audit.
  **Privacy:** the screenshot is uploaded to Google; free-tier data may be used
  to improve Google products. Close sensitive windows first.
  Edit the prompt freely — no code change needed.
  macOS: allow Screen Recording for Terminal/Python in System Settings.
- **Debug Log tab** — live log of everything (chat, audits, scans, bridge, JS
  errors) with a level filter. Same lines are saved to `logs/ksm.log`
  (rotating). Set `LOG_LEVEL` in the SETTINGS tab (or `DEBUG_UI` there, then restart) for more detail.
- Code: `app/screen_analyzer.py` (service), `app/logger.py` (logging), wired in
  `app/gui/web_api.py` + `main_window.py`. The old modules are unchanged.

## Reliability upgrade (Phase 2)

- **Your task file is protected.** If `data/matrix_data_pro.json` is ever damaged, the app copies it to
  `matrix_data_pro.json.corrupt-<timestamp>` before starting empty, shows a red banner at the top, and
  never overwrites the damaged file if it could not be backed up. Saves are atomic (temp file + replace),
  so a crash or power cut mid-save cannot leave a half-written file.
- **Mobile sync is honest.** Start-up problems (missing key file, bad URL) now appear as `[ERROR]` lines on
  the Mobile Sync tab. Phone messages that arrive while the chat is busy are queued and handled in order,
  and the AI's reply is sent back to the phone (`ai_responses`).
- **Prompts are clean and configurable.** The prompt files use `{USER_NAME}`, `{ASSISTANT_NAME}` and
  `{USER_PROFILE}`, filled in from the settings. Edit your profile in the SETTINGS tab (e.g. your year/course) instead of
  editing the prompts. The trigger phrase `task addtion initiated` is misspelled on purpose and must stay
  identical in code and prompts.
- **Task parsing is forgiving.** Times like `18:00`, values the model wraps in `[ ]`, and a bad block in a
  multi-task answer no longer break task creation.
- **Calmer UI.** The matrix redraws only when something changed, never while you drag a card; the default
  deadline date uses your local date; deleting a task clears the detail panel.

## Settings tab and key storage (Phase 3)

You no longer have to create `.env` files or copy key files after an upgrade. Open the **SETTINGS** tab.

- **Where things are stored** (outside the project, so replacing the project folder keeps them):
  Windows `%APPDATA%\KSM`, macOS `~/Library/Application Support/KSM`, Linux `~/.config/ksm`.
  Set the environment variable `KSM_CONFIG_DIR` to use another folder.
  - API keys: the **OS keychain** (Windows Credential Manager, macOS Keychain, Linux Secret Service).
    If there is no keychain, keys go to a file only you can read, and the tab shows a warning.
  - Other settings: `settings.json` (with a `.bak` backup). Firebase key file: `firebase/`.
    A history of changes (never the values): `settings_history.log`.
- **What you can do in the tab:** add, edit and delete keys and settings; Show or Reveal a key while
  editing; filter the list; press **Test connection** for Groq, Gemini or Firebase (it can test a key you
  typed but have not saved, and uses no tokens); import an `.env` (paste or file); import the Firebase
  JSON (file or paste); Backup and Restore settings (keys are never in a backup); see History; open the folder.
- **Changes apply at once**, no restart. Only items marked RESTART (mobile sync switch, Firebase URL and
  file, data file name, dev-tools) need a restart.
- **Which value wins:** a real environment variable, then what you saved in the tab, then an old `.env`,
  then the default. Each row shows a badge: SAVED, ENVIRONMENT, .ENV FILE, DEFAULT or NOT SET.
- **From the old setup:** if the project folder still has `.env` or `serviceAccountKey.json`, the tab offers
  "Import now" once. The originals are left as they are; you can delete them afterwards.
- **Safety:** the page only ever receives masked keys (`gsk_…1234`). Keys are removed from the debug log,
  error messages and test results. Anyone who can log in as you can still read your keychain, so keep the
  PC account protected, and rotate any key you think leaked.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # backend: 145 tests, no API keys or network needed
cd tests/ui && npm install && npm test    # front end (jsdom); needs Node.js
```

Tests never call a live API and never touch `data/matrix_data_pro.json`. They cannot check the real
window, a real mouse drag, a real Groq/Gemini answer, or a real Firebase project; try those by hand.

## Project structure

```
karauli_shankar_mahadev/
├── app/
│   ├── config.py            # all settings as attributes; reload() re-reads them live
│   ├── settings_store.py    # common config folder + OS keychain, order of winners, import/backup
│   ├── settings_tests.py    # Test buttons: Groq, Gemini, Firebase (read-only)
│   ├── data_store.py        # task CRUD, migration, auto-escalation ("Central_Government")
│   ├── ai_assistant.py      # chat + task extraction ("General_Helper")
│   ├── strategy_officer.py  # matrix audit ("Chief Strategy Officer")
│   ├── firebase_bridge.py   # optional mobile sync ("Mobile_Server")
│   ├── screen_analyzer.py   # screenshot -> Gemini vision ("Screen Scout")
│   ├── logger.py            # file + in-app debug log
│   └── gui/
│       ├── main_window.py   # boots the pywebview window, starts the Firebase thread
│       ├── web_api.py       # exposes the backend to the page as window.pywebview.api
│       └── webui/           # the actual GUI: HTML/CSS/JS, four built-in themes
│           ├── index.html
│           ├── style.css
│           └── app.js
├── tests/                    # pytest backend tests + tests/ui (jsdom front-end checks)
├── prompts/
│   ├── screen_analyzer.txt         # screen-scan prompt
│   ├── general_helper.txt          # assistant persona + rules
│   └── task_translation_prompt.txt # task-extraction rules
├── data/
│   └── matrix_data_pro.json  # your task data (auto-created if missing)
├── main.py                   # run this
├── requirements.txt          # runtime;  requirements-dev.txt adds pytest
├── .env.example               # optional: old-style .env (the SETTINGS tab replaces it)
└── .gitignore
```

## Setup

**1. Install Python 3.10+** if you don't already have it.

**2. Create a virtual environment and install dependencies:**

```bash
cd karauli_shankar_mahadev
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**3. Add your keys (no `.env` needed):** start the app (step 4), open the **SETTINGS** tab and add:
- `GROQ_API_KEY` — required for the AI Chat tab. Get one free at
  https://console.groq.com/keys
- `GEMINI_API_KEY` — required for the Strategy Audit and Screen Scan tabs. Get one free at
  https://aistudio.google.com/apikey

Press **Test connection** to check each one. Optionally set your name, the assistant name and a short
profile (used in the prompts). Mobile sync stays off unless you turn it on (step 5).
If you prefer a file, `cp .env.example .env` still works: the tab reads it as a fallback and can import it.

Both AI tabs tell you in-app if a key is missing — the rest of the app
(the Matrix tab) works with no keys at all.

**4. Run it:**

```bash
python main.py
```

This opens a native window rendering `app/gui/webui/index.html` via
pywebview. On Linux you may need the system WebKitGTK package if it isn't
already installed (`sudo apt install python3-gi gir1.2-webkit2-4.1` on
Debian/Ubuntu-family distros); Windows (WebView2, usually preinstalled with
Windows 10/11) and macOS (Cocoa WebKit) need nothing extra.

**5. (Optional) Mobile sync setup:**

If you want the phone app to sync again:
- Generate a **new** Firebase service-account key (Firebase console →
  Project Settings → Service Accounts → Generate New Private Key) — don't
  reuse the old one, treat it as already exposed.
- In the SETTINGS tab, under Mobile sync, press **Import file...** (or **Paste JSON...**). The file is
  copied into the config folder; you can delete your download afterwards.
- Set **Realtime Database URL** (e.g. `https://your-project-default-rtdb.firebaseio.com/`), switch on
  **Enable mobile sync**, and press **Test connection**.
- Restart the app — the Mobile Sync tab will show a green status dot and
  start listening.

## Using the app

- **Theme picker** (top-right) — switch between Sci-fi blue, Sacred saffron,
  Minimal slate, and Forest emerald at any time; your choice is remembered.
- **Matrix tab** — the 3x3 grid. Click-and-drag a task card to a
  higher-priority cell to migrate it manually (you can only move up, one
  importance step at a time — that's intentional, it stops accidental
  drags from scrambling your priorities). Double-click or select a task
  in the side lists to see its full history and delete it. "Warp" lets you
  fast-forward the clock to test how auto-escalation behaves.
- **AI Chat tab** — talk normally. When you describe a new goal, the
  assistant will (based on the rules in `prompts/general_helper.txt`)
  say `task addtion initiated`, which triggers a second, stricter pass
  (`prompts/task_translation_prompt.txt`) that scores Importance/Urgency
  and adds it to the matrix automatically. Duplicate goals are detected
  and skipped.
- **Strategy Audit tab** — click "Run Full Matrix Audit" any time for a
  blunt read on Danger Zones (urgent-but-low-value tasks), neglected
  Growth Tasks, a daily battle plan, and a call-out on tasks you keep
  auto-migrating instead of finishing.
- **Mobile Sync tab** — shows whether Firebase sync is on, and a live log
  of messages coming in from / replies going out to the phone app.
- **Settings tab** — keys and settings: add, edit, delete, test, import, back up (see above).

## Editing the AI's behavior

The assistant's personality and rules live in plain text files, not code:
- `prompts/general_helper.txt` — who it is, how it talks, when it decides
  something is a "new task."
- `prompts/task_translation_prompt.txt` — exactly how it scores
  Importance/Urgency and what output format it must use.

Edit these directly; no code changes needed.
