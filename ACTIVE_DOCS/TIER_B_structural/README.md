# KSM: Karauli Shankar Mahadev, Central Command

**Last updated:** 2026-10-31 10:40

## What this project does
A personal desktop task manager built on a 3x3 Eisenhower matrix (Importance 1-3 by Urgency 1-3).
An AI chat assistant (Groq) turns conversation into scored tasks, a strategy officer (Gemini) audits
the matrix, and a screen-scan tool (Gemini vision) describes what is on screen. Urgency rises
automatically as deadlines approach. Optional Firebase sync lets a phone app send tasks in and get
the AI's reply back. A second, optional Firebase feature (Phase 4) keeps a copy of the owner's
tasks, chat history, audits and screen scan answers online, switch by switch.

## Current status
Phases 1, 2, 3 and 4 complete. Phase 2 added data-file protection, visible mobile-sync errors, clean prompts, a hardened
parser, a calmer UI and tests. Phase 3 added the SETTINGS tab and one common, upgrade-proof place for keys and settings (a
later fix, 3G, repaired a real-window failure the tests could not see; see the CHANGELOG correction). Phase 4 added the
online copy: tasks, chat history, audits and screen scan answers can be copied to the owner's Firebase database (tasks and
chat also restored from it), all off by default, switches that act at once, a sync status chip in the header and a Sync now /
Export cloud data panel on the MOBILE SYNC tab. Tests: 560 Python in the last sandbox run (Phase 4 ended at 289: 286 passed and 3 skipped on purpose on Windows) and 55 UI
tests pass. The owner checked the chip, the MOBILE SYNC panel, the switches, the locked Firebase rules, task upload, Sync now
and Export in the real window (4G). Chat, audit and scan upload were tested with a fake cloud only; some manual checks
are still outstanding (see STATE.md). Phase 5 (shared configuration in Firebase: prompts and all settings except API keys, desktop first) is in progress: 5A (prompt blocks, setting scopes, rules), 5B (prompt library), 5C (settings in Firebase) and 5D (the AI reads the prompt library; one live listener feeds prompts and settings) and 5E (the PROMPTS tab, scope badges and shared status in SETTINGS, theme saved per device) are built and tested with fakes and a fake browser backend; the real window and real Firebase are not yet proven (5F is the owner's check); 5F is next; Phase 6 (several devices write
tasks, web and Android app) is scoped and deferred. Only the parts marked built in ROADMAP exist; see "Planned architecture" below.
Detail: CHANGELOG.md. Next work: ROADMAP.md.

## Current architecture (high level)
- `CODE/main.py`: entry point, calls `app.gui.main_window.run`
- `app/settings_store.py`: the common store: config folder, OS keychain (`keyring`) or owner-only file, order of
  winners (env var, saved, old `.env`, default), validation, imports, backup, history, `redact_text`
- `app/settings_tests.py`: read-only Test buttons for Groq, Gemini and Firebase (fake clients in tests)
- `app/config.py`: all settings and paths as plain attributes; `reload()` re-reads them from the store
- `app/data_store.py`: `TaskStore`; CRUD, manual migration (up only, one importance step), auto-escalation;
  atomic saves; damaged files are backed up as `*.corrupt-<timestamp>` (`load_warning`)
- `app/ai_assistant.py`: Groq chat, trigger phrase, `render_prompt`, strict extraction, `parse_tasks`
- `app/strategy_officer.py`: Gemini matrix audit (prompt in `prompts/strategy_audit.txt`)
- `app/prompt_templates.py` (Phase 5A): the four role templates (locked and editable blocks), the block-file parser, `assemble()`, `fill()`, `wire_problems()`; the default prompt callers read through it
- `app/prompt_store.py` (Phase 5B): the prompt library (save, versions, restore, reset, seed, guard rules, cache `prompts_cache.json`); the AI modules reach it only through `prompt_use`
- `app/prompt_use.py` (Phase 5D): the one door for prompt text (`built`, `text`, `notes`); falls back to the shipped text and warns when the online text is not in use
- `app/shared_settings.py` (Phase 5C): COMMON and PER-DEVICE settings in `ksm/config` (seed, live apply, per-key conflict check, outbox `shared_settings_state.json`, device registry)
- `app/config_sync.py` (Phase 5D): `ConfigSync`, the one live listener on `ksm/config` feeding the prompt library and the shared settings; started and stopped by `main_window`
- `app/screen_analyzer.py`: capture (mss/Pillow), resize, Gemini vision call, one retry on transient errors
- `app/firebase_bridge.py`: optional listener on `incoming_tasks`, `run_bridge_safely`, reply to `ai_responses`
- `app/firebase_app.py`: the one place that creates the Firebase app (shared by the bridge and the online copy)
- `app/cloud_sync.py`: the online-copy engine: `CloudStore` (fake `MemoryCloud` and real `FirebaseCloudStore`), offline
  outbox and state in `sync_state.json`, last-write-wins per task, delete markers, key removal, prefix flush
- `app/task_sync.py`: connects `TaskStore` to the engine; pull on start, backup before first upload and before a restore
- `app/chat_sync.py`: saves chat messages per session and loads the last 20 on start (respects "Clear context")
- `app/record_sync.py`: saves each successful audit and screen scan answer (question, answer, time; never the image or the task matrix)
- `app/logger.py`: rotating file log plus in-memory ring buffer for the Debug tab; removes saved keys from every line
- `app/gui/main_window.py`: builds the pywebview window, starts the Firebase thread, starts the online copy (`start_online_copy`), wires the reply sink
- `app/gui/web_api.py`: every public method is callable from JS (includes `get_sync_status`, `sync_now`, `export_cloud_data`, and since Phase 5E the `prompt_*`, `config_status` and `get_theme` methods behind the PROMPTS tab); long calls run in threads and the page
  polls; mobile messages are queued while the chat is busy
- `app/gui/webui/`: index.html, style.css, app.js (8 tabs including PROMPTS and SETTINGS, 4 themes, warning banner, sync status chip in the header, ONLINE COPY panel on MOBILE SYNC)
- `prompts/`: four prompt files in block format (`@@@ block <id> <editable|locked>` markers; a file not matching the code's layout is refused); placeholders `{USER_NAME}`, `{ASSISTANT_NAME}`, `{USER_PROFILE}`, `{TRIGGER_PHRASE}`
- `firebase/`: `database.rules.json` (deny by default, owner only; paste into the Firebase console) and `DATA_LAYOUT.md` (paths, who writes what, what is never uploaded)
- `data/matrix_data_pro.json`: the owner's task data (14 tasks at handoff); `data/backups/` holds the automatic pre-sync copies
- `tests/`: pytest backend suite (289 tests) and `tests/ui/` jsdom front-end checks (32 tests); tests use a temporary config folder and never touch the real keychain

## Planned architecture (Phases 5 and 6) - NOT BUILT YET
Everything in this section is a plan agreed with the owner on 2026-10-28. Code wins over this section until a ROADMAP
sub-session marks a part as built. Details: ROADMAP.md (Phase 5, Phase 6) and `CODE/firebase/DATA_LAYOUT.md`.

### Principles (owner decisions)
1. One person, one Firebase project, one owner sign-in. Rules allow only the owner's user id.
2. Offline first: every client must open and work with no internet; the cloud catches up later. An AI call and start-up never wait for the network.
3. Keys never go to Firebase and never into hosted web files. Each device holds its own keys.
4. Free Firebase plan only (no Cloud Functions, no server of our own).
5. Common environment: prompts and all settings live in Firebase (`ksm/config`), on by default. Only the API keys and the four settings needed to reach Firebase stay out.
6. Prompts: lock the wire, edit the rest. Only what the program searches for or reads, and the format the parser reads, is locked and ships with the program. Everything else is editable and lives in Firebase.
7. Every setting has one scope: COMMON (Firebase, one value for all clients), PER-DEVICE (Firebase, one value per device), LOCAL ONLY (needed to reach Firebase) or SECRET (API keys, never in Firebase).
8. Finish the desktop project first (Phase 5). The web and Android apps (Phase 6) come after.

### Order of work
| Phase | What | Status |
|---|---|---|
| 1 to 4 | Baseline, reliability, Settings tab and keys, online copy of data | Complete |
| 5 | Shared configuration in Firebase: prompts and settings, desktop first (5A to 5F) | 5A to 5E done, 5F next |
| 6 | Several devices write tasks directly; web app and Android app; AI on each device (6A to 6H) | Scoped, deferred |

### Where each kind of data lives
| Data | Today (Phases 1 to 4) | After Phase 5 | After Phase 6 |
|---|---|---|---|
| Tasks | Local file is the working copy; copy in `ksm/tasks` (switch) | same | Firebase is the truth; local file is a cache and backup |
| Chat history | Memory; copy in `ksm/chats` (switch) | same | `ksm/chats`, written by any device |
| Audits, scan answers | Memory; copy in `ksm/audits`, `ksm/scans` (switch) | same | same (scans desktop only) |
| Prompts, editable blocks | Files in `prompts/` | `ksm/config/prompts` + `ksm/config_versions` (switch, on by default) | same |
| Prompts, locked blocks | Inside the prompt files | Ship with the program, never in Firebase | same |
| Common settings (names, profile, model names, upload switches, versions kept) | Config folder `settings.json` | `ksm/config/settings` (switch, on by default); `settings.json` keeps the offline copy | same |
| Per-device settings (screen tuning, log level, data file name, theme) | Config folder (theme: browser storage) | `ksm/config/devices/<deviceId>/settings` | same |
| Local-only settings (Firebase on/off, database URL, service account path, "keep settings online", device id) | Config folder | same (cannot be in Firebase) | same |
| API keys | OS keychain | same | each device: keychain, Android secure storage, browser storage; never Firebase |
| Firebase service account | Config folder `firebase/` | same | desktop only |
| Phone messages | `incoming_tasks`, `ai_responses` | same | retired |
| Debug log, time-warp | Local only | same | same |

### The desktop after Phase 5 (components)
```
                 Firebase Realtime Database (owner only)
   ksm/tasks  ksm/chats  ksm/audits  ksm/scans  ksm/meta                      (Phase 4)
   ksm/config/{settings, devices, prompts, prompt_active}   one live listener         (Phase 5, new)
   ksm/config_versions/prompts/...                 old versions, not listened (Phase 5, new)
   incoming_tasks  ai_responses                                              (old phone path)
                              ^   firebase-admin, service account, one shared app
 -------------------------------------------------------------------------------
  Desktop app (Python + pywebview)
   UI (HTML/JS tabs incl. new PROMPTS; Settings with badges) <-> web_api.py (methods only)
        |- TaskStore ........ local file, auto-escalation
        |- AIAssistant ...... Groq chat + extraction     --\
        |- StrategyOfficer .. Gemini audit                 |-- ask prompt_store for their prompt (in-memory copy)
        |- ScreenAnalyzer ... Gemini vision              --/
        |- prompt_store.py (new) <- prompt_templates.py (locked blocks, templates, wire checks) + shipped files
        |- shared_settings.py (new) <-> settings_store.py (scope per setting; env var, saved, .env, default)
        |- cloud_sync / task_sync / chat_sync / record_sync (outbox, switches)
  Local: data/matrix_data_pro.json, data/backups/, config folder (settings.json, keychain,
         sync_state.json, prompts_cache.json, exports/), logs/ksm.log
```

### How a prompt is built (Phase 5)
1. The role (`chat`, `task_extraction`, `audit`, `screen_scan`) names a template in code: an ordered list of blocks, each LOCKED or EDITABLE.
2. `ksm/config/prompt_active/<role>` says which stored prompt is in use (more than one per role is allowed).
3. For each block in order: locked = shipped text; editable = the Firebase text if it is on and passes the guard rules, otherwise the last good local copy, otherwise the shipped text.
4. At the insert point the `extra` block and the owner's own blocks are added, sorted by their order, just before the last locked block.
5. The blocks are joined and the placeholders `{USER_NAME}`, `{ASSISTANT_NAME}`, `{USER_PROFILE}`, `{TRIGGER_PHRASE}` are filled.
6. Self-check: the chat prompt must contain the literal trigger phrase; the extraction prompt must contain `DUPLICATE_DETECTED` and the five labels the parser reads. If not, the shipped default for that role is used and a warning is shown.
7. The result is kept in memory and rebuilt only when a block, the active choice or a placeholder value changes, so an AI call never waits for the network.
8. Guard rules (on save and again at use): an editable block may not contain the literal trigger phrase (it uses `{TRIGGER_PHRASE}`), and for task extraction not `DUPLICATE_DETECTED` or the output labels; size limits; no key text.
9. With the switch off nothing is read from or sent to Firebase and the shipped prompts are used, as today.

### How settings in Firebase work (Phase 5)
Each installation has a random device id (`device.json`) and a registry entry `ksm/config/devices/<deviceId>`. A COMMON setting
is one value for all clients (`ksm/config/settings/<KEY>`); a PER-DEVICE setting is one value per device
(`ksm/config/devices/<deviceId>/settings/<KEY>`). A setting is saved locally at once and sent to Firebase (outbox when
offline). A change that arrives from Firebase updates the saved local value and applies live (only `DATA_FILE_NAME` and
`DEBUG_UI` need a restart). Two clients changing the same setting: the transaction checks `updated_at`; the later writer is
told and the first value wins. An environment variable still wins on its device and is never uploaded. A LOCAL ONLY setting or
an API key can never be written under `ksm/config`: the rules list the keys by name for each scope.

### The three apps after Phase 6 (components)
```
        Desktop (Python)        Website / PWA (Firebase Hosting)        Android app (same web code, Capacitor)
        firebase-admin          Firebase JS SDK, Google sign-in         Firebase JS SDK, native Google sign-in
              \                         |                                  /
               +----------- Firebase Realtime Database (rules: owner only) -----------+
                 ksm/tasks/<id>(+history map)  ksm/chats  ksm/audits  ksm/scans
                 ksm/config/...  ksm/config_versions/...  ksm/meta
   Each client: own offline queue, own AI keys on the device, AI chat and audit run on the device that asks.
   Screen Scan: desktop only.
```
How tasks stay correct with several writers (full list in ROADMAP Phase 6 and DATA_LAYOUT "Phase 6 layout"): unique ids from
Firebase push keys; only the changed fields are written; importance and urgency only go up (transaction plus a rule that
refuses a lower value); history is a map of entries; a delete is a marker; urgency escalation is computed the same way by
every client; time comes from Firebase's clock; the local copy is a cache and never uploaded as a whole.

### Security model
- Rules: deny by default; read and write only for the owner's Firebase user id (replace `REPLACE_WITH_OWNER_UID` in `database.rules.json`).
- `ksm/config/settings` and each device's `settings` accept only the named keys of their scope, so an API key or a LOCAL ONLY setting cannot be stored there. The API keys are never in Firebase; the owner may ask for an opt-in later (ROADMAP Phase 5, open question 2).
- The desktop uses the service account and bypasses rules, so client-side checks and tests protect the data from a desktop bug.
- Web and Android use the owner's sign-in, so the rules apply to them. The web API key is public by design and is restricted to the hosting domain.
- Redaction: saved keys are removed from every text before it is uploaded or written to the outbox.
- User name and profile text (common settings) go to the owner's own private Firebase by default; the local switch `SYNC_SETTINGS` turns settings sync off on a device.
- The Spark plan has no automatic database backup: keep the desktop's local backups and the Export button.
- Still to do by the owner: rotate the keys exposed before Phase 1; confirm the console rules match the shipped file.

### Where to read more
| Question | File |
|---|---|
| What is the state now and what is next | `ACTIVE_DOCS/STATE.md` |
| Phase scopes, sub-sessions, checks, open questions | `ACTIVE_DOCS/TIER_B_structural/ROADMAP.md` |
| Firebase paths, who writes what, setting scopes, prompt blocks, rules | `CODE/firebase/DATA_LAYOUT.md` |
| How to run a session, naming, project rules | `RULEBOOK_DEVELOPER_GUIDE.md` |

## Settings and keys
Settings and API keys live outside the project, so replacing the project folder never loses them. Config
folder: `%APPDATA%\KSM`, `~/Library/Application Support/KSM` or `~/.config/ksm` (override: `KSM_CONFIG_DIR`).
Keys are in the OS keychain; settings in `settings.json`; the Firebase file in `firebase/`. An old `.env` or
`serviceAccountKey.json` in the project is offered for one-click import and is never modified.

## Online copy (Phase 4, optional, off by default)
Three switches in SETTINGS, group "Online copy": keep tasks online, keep chat history online, keep audits and
screen scans online. Each acts at once, with no restart. Local files stay the working copy; the app runs fully offline.
Changes wait in an outbox and are sent when the network allows; a cloud failure never reaches the UI or blocks a
local change. Only the desktop writes tasks. Text sent online first has saved keys removed. Never uploaded: API keys,
the service account file, the debug log, screenshots (audits and scans save text only). Before the first task upload (and
before any restore that changes the local list) the task file is backed up to `data/backups/`.
Status and controls: a chip in the header shows Synced, Pending N, Offline or Off; the MOBILE SYNC tab shows the last sync
time, a Sync now button and an Export cloud data button, which writes a JSON file (keys removed) to the `exports/` folder
inside the config folder. Layout, rules and the check that the Firebase console is not in open test mode:
`CODE/firebase/DATA_LAYOUT.md`.

## Data flow (chat to task)
chat -> `AIAssistant.chat` -> reply contains "task addtion initiated" -> `validate_and_extract`
(low temperature, strict format) -> `parse_tasks` -> `TaskStore.add_task(origin="AUTO")`, one task at a
time so one failure does not drop the others. Mobile messages take the same path via the queue.

## Setup / running
```bash
cd CODE
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt                  # requirements.txt if you do not need tests
python main.py           # then open the SETTINGS tab and add your Groq and Gemini keys (no .env needed)
python -m pytest -q                       # backend tests
cd tests/ui && npm install && npm test    # front-end tests (needs Node.js)
```
Full original setup notes (Linux WebKitGTK, Firebase) are in `CODE/README.md`.

## Where things live
- `CODE/`: source of truth (code wins over docs)
- `ACTIVE_DOCS/STATE.md`: read first in any session
- `ACTIVE_DOCS/TIER_A_session_notes/`: raw notes not yet folded
- `ACTIVE_DOCS/TIER_B_structural/`: README, CHANGELOG, ROADMAP

## Contributing
See `RULEBOOK_DEVELOPER_GUIDE.md` before any change (including section 7, project-specific rules).
