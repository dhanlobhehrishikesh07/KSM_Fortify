# Roadmap

**Last updated:** 2026-10-31 10:40

## Completed phases
### Phase 1: Baseline, project as received COMPLETE
Imported unchanged and documented. Detail in CHANGELOG.md.

### Phase 2: Reliability and hygiene upgrade COMPLETE (automated checks; manual checks outstanding)
Data-file protection, visible Firebase errors with a message queue, clean configurable prompts, a
forgiving task parser, a calmer UI, and the first test suite. Sub-sessions 2A to 2E all done. Detail in
CHANGELOG.md.

### Phase 3: Settings tab and common key storage COMPLETE (automated checks; manual checks outstanding)
Owner goal: one common place for keys and settings that survives upgrades, editable, deletable and
testable from the GUI. Decisions taken with the owner: config folder plus OS keychain; `.env` kept as
fallback and one-time import; Firebase file copied into the config folder; keys masked in the page;
Test buttons for Groq, Gemini and Firebase. All sub-sessions done (detail in CHANGELOG.md):
- [x] 3A `settings_store.py`: folder, keychain, order of winners, history. Check: store tests pass
- [x] 3B live `config.reload()`, client refresh, log redaction. Check: 72 earlier tests still pass
- [x] 3C `WebAPI.settings_*`. Check: API tests pass, no full key in any answer
- [x] 3D SETTINGS tab. Check: UI tests pass, screenshots in four themes
- [x] 3E Test buttons. Check: fake-client tests pass
- [x] 3F documentation fold (this entry, STATE, README, CHANGELOG)
- [x] 3G fix: `WebAPI` helper attributes made private after the real window showed empty tabs (see the CHANGELOG correction)

### Phase 4: Online sync with Firebase COMPLETE (automated checks and the owner's real-window check; some manual checks outstanding)
Owner goal: the app stays the owner's personal project but its data (tasks, chat history, audits, screen
scan answers) is also kept online in the owner's own Firebase Realtime Database, so it is not lost with
the machine and can be read from anywhere later. Today the machine keeps: tasks (`data/matrix_data_pro.json`),
settings and the change log (config folder), API keys (OS keychain or owner-only file), the Firebase key file
(config folder `firebase/`), the debug log (`logs/ksm.log`, 3 x 1 MB) and the theme (page `localStorage`).
Chat, audits, screen scans and the Mobile Sync log exist in memory only and vanish when the app closes.

**Decisions already taken (change only with the owner):**
- Offline first. The local JSON file stays the working copy and the app must run fully with no internet or
  with sync switched off. Firebase is a second copy that catches up when it can; a Firebase failure never
  blocks or loses a local change.
  **Changed from Phase 6 (owner decision 2026-10-28, deferred):** Firebase becomes the one source of truth and the local file only a
  cache and backup. The app must still open and show the last known tasks with no internet.
- Single writer for tasks: only the desktop app creates, changes and deletes tasks. The phone sends
  messages in through the existing `incoming_tasks` path and reads results. This avoids two devices
  creating the same task id while offline. Two-way phone editing is parked for a later phase.
  **Superseded from Phase 6 (owner decision 2026-10-28, deferred):** several clients may write tasks directly; see Phase 6. This rule applied to Phase 4, describes the code today and stays true until 6B changes it.
- Never uploaded: API keys, the service account file, the debug log, screenshots, the time-warp offset
  (`master_offset` is a local testing tool).
- Each kind of data has its own switch in SETTINGS, so the owner decides what goes online: tasks, chat
  history, audits and scans. Chat upload is a setting (owner decision, 2026-10-27); it starts OFF and the
  owner turns it on when ready. It can be turned off again at any time and then nothing more is sent.
- The existing `incoming_tasks` and `ai_responses` paths keep working exactly as today, so the current
  phone app is not broken.
- Text sent to the cloud passes through `redact_text` first, so a key pasted into a chat is never stored.

**Cloud layout (RTDB, under one root `ksm/`, objects keyed by id, no arrays):**
`ksm/tasks/<id>` (task fields plus `updated_at_ms` and `deleted` flag so a delete reaches the cloud),
`ksm/chats/<session_id>/<msg_id>` (role, text, time), `ksm/audits/<time_key>`, `ksm/scans/<time_key>`
(question, answer text, monitor, time; no image), `ksm/meta` (schema version, last upload time).

**Security:** security rules live in a shipped file `CODE/firebase/database.rules.json` (no secrets) that
allows read and write only to the owner's Firebase Authentication user id, and denies everything else. The
desktop uses the service account (full access), so rules matter for the phone or web viewer. The owner
pastes the file into the Firebase console once.

**Known limits to tell the owner:** the free Spark plan has no automatic RTDB backups, so the local JSON
and an "Export cloud data to a file" button are the backup; the desktop app must be running for phone
messages to be processed; chat and scan text is private data stored on Google's servers.

**Sub-sessions (each fits one coding session; do not start N+1 before N's check passes):**
- [x] 4A Design lock. Add `firebase/database.rules.json`, a short data-layout doc (done as `firebase/DATA_LAYOUT.md`, README points to it), and the new
  settings (`SYNC_TASKS`, `SYNC_CHAT`, `SYNC_AUDITS_SCANS`, all `bool`, default false) in
  `settings_store.py`. Check: settings tests pass; rules file is valid JSON; no existing test changes.
- [x] 4B Sync core, no UI. New `app/cloud_sync.py`: one shared Firebase app helper (the bridge and sync must
  not both call `initialize_app`), a `CloudStore` interface with a fake for tests, an outbox file in the
  config folder for changes made offline, last-write-wins per task, delete markers. Check: fake-cloud
  tests for offline queue, reconnect flush, conflict, delete, and "cloud down never raises into the UI".
- [x] 4C Task sync and first upload. Wire `TaskStore` changes to the sync engine; pull on start; one-time
  upload of the existing tasks, idempotent, preceded by an automatic backup of `matrix_data_pro.json`
  (RULEBOOK section 7). Check: all earlier tests still pass (184 at that point; the first draft said 145); the data file is byte-identical through
  the tests; running the migration twice changes nothing.
- [x] 4D Chat history (done: last 20 messages loaded, `session_history` capped at 60 lines). Save each chat message (redacted) per session; on start, load the last N messages so
  the AI remembers across sessions; cap `session_history` (also closes an old idea below). Check: tests
  for redaction, the cap, reload order, and sync switched off means nothing is sent.
- [x] 4E Audit and screen scan history (done: `app/record_sync.py`; each finished audit and scan answer is saved with its
  question and time under `ksm/audits/` and `ksm/scans/`; never the image or the task matrix; failed runs are not saved).
  Check passed: fake-cloud tests; no image bytes are ever sent.
- [x] 4F UI (done 2026-10-28: 4F-1 to 4F-5 all ticked). Sync status chip (Synced, Pending N, Offline, Off), the three
  switches in SETTINGS, on the Mobile Sync tab: last sync time, Sync now, Export cloud data. Check passed: new UI tests;
  four themes checked in headless Chromium; `WebAPI` gained only methods, never public attributes (RULEBOOK section 7).
  4F was split on 2026-10-27 (scoping session) because one session exceeded the AI usage limit. Sub-sessions:
  - [x] 4F-1 Backend status and actions, no UI. `WebAPI.get_sync_status`, `sync_now`, `export_cloud_data` (methods only);
    `SyncEngine.export_all`; `sync_now` on chat and record sync. 19 new tests in `tests/test_sync_api.py`.
  - [x] 4F-2 Switches live, not restart-needed. The workers already read the three switches live, so the `restart=True`
    flag was wrong and was removed; one real gap fixed (turning `SYNC_TASKS` on now starts the first upload at once).
    Proof: 3 new tests fail on the old code.
  - [x] 4F-3 Sync status chip in the header, refreshed every 2 s from `get_sync_status`; theme variables only.
  - [x] 4F-4 MOBILE SYNC tab: ONLINE COPY panel with last sync time, Sync now, Export cloud data (shows the saved path) and a
    note that nothing is uploaded unless a switch is on. A failed call shows a message, never a crash.
  - [x] 4F-5 Themes and full run. Chip and panel checked in all four themes in headless Chromium; slate status colours
    darkened for contrast (one UI test added). Both suites pass: 289 Python, 32 UI.
- [x] 4G Real-world check by the owner (done 2026-10-28) and documentation fold. Result: on the owner's Windows machine the UI
  suite passed 32 of 32; the Python suite had one failure, a test-only line-ending problem (a byte comparison against `\n`
  while Windows writes `\r\n`), fixed in 4G-1 (one assertion in `tests/test_task_sync.py`, no app file touched); rerun:
  286 passed, 3 skipped on purpose, 0 failed. In the real window the header chip changed Off, Synced, Off; the MOBILE SYNC
  panel was correct and readable in the slate theme; the three switches act at once with no restart note; the Firebase
  console rules are not open test mode; tasks uploaded and are visible in the console; Sync now and Export cloud data work
  and the export file opens; no red errors in the terminal. Still not checked by hand: see STATE.md.
  - [x] 4G-1 Test fix for Windows line endings (test only).

## In progress / up next
### Phase 5: Shared configuration in Firebase: prompts and settings, desktop first (SCOPED 2026-10-28, revised three times the same day; 5A to 5E done, 5F is next)
Owner goal: all configuration lives in the owner's own Firebase (every setting except the two API keys and the four settings needed to reach Firebase), so every client (the
desktop now; the web and Android apps and any API later) sees the same prompts and the same settings: one common
environment. The owner can view, edit, switch, reset or delete the prompt parts that do not affect the program, and edit
the settings, from the desktop first. The parts the program depends on stay fixed until the program is upgraded.
It must stay fast and must keep working with no internet. Today the prompts are three files in `prompts/`
(`general_helper.txt`, `task_translation_prompt.txt`, `screen_analyzer.txt`), the strategy audit prompt is hard-coded in
`strategy_officer.py` (`ANALYSIS_PROMPT`), `screen_analyzer.py` has a hard-coded `FALLBACK_PROMPT`, and all settings are
local (`settings.json` plus the keychain). The owner decided on 2026-10-28 to finish the desktop project first; the web and
Android apps (several devices writing tasks) are Phase 6, deferred.

**Owner decisions (2026-10-28, change only with the owner):**
- ALL settings go to Firebase. Two kinds cannot, and the plan keeps them out on purpose: the four settings the app needs
  before it can reach Firebase (`ENABLE_FIREBASE`, `FIREBASE_DATABASE_URL`, `FIREBASE_SERVICE_ACCOUNT_PATH`, and the new
  `SYNC_SETTINGS`, the gate for this layer, which cannot sit inside what it controls), and the two API keys (see "Open for
  the owner", item 2).
- "Keep prompts online" (`SYNC_PROMPTS`) and "Keep settings online" (`SYNC_SETTINGS`) are ON by default. With no Firebase
  configured they do nothing and raise no error (the status says "no Firebase").
- The upload switches (`SYNC_TASKS`, `SYNC_CHAT`, `SYNC_AUDITS_SCANS`, `SYNC_PROMPTS`) become COMMON: one value for every
  client, so the environment behaves the same everywhere (turning one off turns it off for all).
- Prompt versions kept per prompt: 30, as the setting `PROMPT_VERSIONS_KEEP` (whole number, 5 to 200), one value for all.
- User name and profile text go to the owner's own private Firebase (owner-only rules).

**Settings scopes (new field `scope` on every `SettingDef`; every setting has exactly one):**
| Scope | Where it is stored | Settings |
|---|---|---|
| COMMON | Firebase, one value for all clients: `ksm/config/settings/<KEY>` | `ASSISTANT_NAME`, `USER_NAME`, `USER_PROFILE`, `GROQ_MODEL_ID`, `GEMINI_MODEL_ID`, `SCREEN_MODEL_ID`, `SYNC_TASKS`, `SYNC_CHAT`, `SYNC_AUDITS_SCANS`, `SYNC_PROMPTS` (new), `PROMPT_VERSIONS_KEEP` (new) |
| PER-DEVICE | Firebase, one value per device: `ksm/config/devices/<deviceId>/settings/<KEY>` | `SCREEN_MAX_WIDTH`, `SCREEN_JPEG_QUALITY`, `SCREEN_HIDE_DELAY` (this PC's screen), `DATA_FILE_NAME`, `LOG_LEVEL`, `DEBUG_UI`, `THEME` (new; today it is only in the browser storage) |
| LOCAL ONLY | This device only; cannot be in Firebase | `ENABLE_FIREBASE`, `FIREBASE_DATABASE_URL`, `FIREBASE_SERVICE_ACCOUNT_PATH` (needed to reach Firebase), `SYNC_SETTINGS` (new, the gate), and the device id |
| SECRET | Never in Firebase (open question 2) | `GROQ_API_KEY`, `GEMINI_API_KEY` |
A secret kind is always scope SECRET. Some values only make sense for one machine (screen size, log detail); they are still
in Firebase under that device's id, so they can be seen and edited from any client, but they do not change other devices.
The Firebase rules list the keys by name for each scope, so a LOCAL ONLY or SECRET key can never be stored there, even by a
buggy client.

**How settings in Firebase work:**
- Each installation makes a random device id the first time (file `device.json` in the config folder, with a friendly name
  that defaults to the computer's name and can be changed) and registers at `ksm/config/devices/<deviceId>` with `name`,
  `kind` ("desktop") and `last_seen` (server clock, set at start-up).
- Every setting is stored as `{value, updated_at, updated_by}`.
- The order of winners stays (environment variable, saved value, old `.env`, default). The "saved value" is kept in step
  with Firebase: a change that arrives from Firebase updates it, so it is also the offline copy. An environment variable
  still wins on that device, is never uploaded, and the Settings tab says "set by environment".
- Editing a setting in the Settings tab saves it locally at once and sends it to Firebase (through the outbox when
  offline). The send is a transaction that checks the `updated_at` this device last saw; if another client changed it
  meanwhile, the other value wins and the Settings tab shows a notice.
- It applies live through `config.reload()`; only `DATA_FILE_NAME` and `DEBUG_UI` need a restart, and the app says so as today.
- First sync: values are copied up only for keys missing in Firebase (this device's own values go under its device id);
  existing Firebase values are never overwritten by a seed. When Firebase already has a value that differs from this
  device's saved one, Firebase wins; the settings backup the store already makes protects the old local value.
- The theme moves from the browser storage to the per-device setting `THEME`.
- The Settings tab shows a badge on every setting: Common, This device (in Firebase), Local only or Secret. The owner can
  also edit the values in the Firebase console.

**Prompts: lock the wire, edit the rest.** A prompt is built from a TEMPLATE in the code: an ordered list of blocks, each
LOCKED or EDITABLE.
- LOCKED (as little as possible): only words the program searches for or reads, the format the parser reads, and short
  descriptions of the data the program sends. The text ships with the program, is shown read-only, changes only with a
  program upgrade, and is NEVER stored in Firebase, so no client can edit or delete it.
- EDITABLE: everything else, such as persona, behaviour, tone, language, duplicate handling, the 1-to-3 wording in chat,
  how much to trust the owner, the importance and urgency rules, audit mission and tone. Stored in Firebase; the shipped
  text is the default.
- Placeholders filled by the program: `{USER_NAME}`, `{ASSISTANT_NAME}`, `{USER_PROFILE}` and the new `{TRIGGER_PHRASE}`
  (the code constant `TASK_TRIGGER_PHRASE`). Editable text talks about the trigger through the placeholder, so it never
  holds the literal phrase and a change of the phrase in the code reaches every prompt. `{FULL_SESSION_CONTEXT}` is left as
  written.
- The default prompt (every block at its shipped text, placeholders filled) equals today's prompt word for word, because
  the blocks keep today's order, so nothing changes for the AI until the owner edits something.
- Share of the default text that is editable (measured on today's files): chat about 70%, task extraction about 54% (the
  rest is mostly the output format), audit about 85%, screen scan 100%.
- What is locked, and why (the full block tables are in `firebase/DATA_LAYOUT.md`):
  - `chat`: the trigger rule (use the phrase only for a brand-new goal) and the confirmation line that ends with the
    phrase, because the program looks for that phrase in the reply;
  - `task_extraction`: the input-context labels the code sends, the duplicate reply (`parse_tasks` drops any text that
    contains `DUPLICATE_DETECTED`), the whole-number line, and the strict output format the parser reads;
  - `audit`: the description of the data sent;
  - `screen_scan`: nothing (its answer is shown as text and not parsed).
- Each role has an editable `extra` block (empty at first) just before the last locked block, so the locked wire text is
  read last. The owner may add own blocks there (title, text, order, on or off). A block can be switched off without
  deleting it.
- Guard rules for editable text, checked on save and again at use: it may not contain the literal trigger phrase nor, for
  `task_extraction`, `DUPLICATE_DETECTED` or the output label lines (the model could echo them: a false task, or a
  dropped one); at most 4000 characters per block and 20000 in total; no saved API key text; unknown `{TOKEN}`s are a
  warning. A block that fails is ignored and its shipped text is used, with a visible warning. A Firebase block that has
  the name of a locked block is ignored with a warning.
- Self-check of the assembled text: before use the program checks that the wire is present (`chat`: the literal trigger
  phrase; `task_extraction`: `DUPLICATE_DETECTED` and the five labels the parser reads). If not, the shipped default for
  that role is used and a warning is shown. This also protects against a template bug after a program upgrade.
- Offline order of winners per block: (1) the active prompt's block from Firebase, if valid; (2) the last good copy in the
  config folder (`prompts_cache.json`); (3) the shipped text.
- Several prompts per role are allowed (for example a strict and a casual chat prompt); `ksm/config/prompt_active/<role>`
  says which is used. The four built-in prompts cannot be deleted; "Reset block" and "Reset prompt" return the shipped
  text. A custom prompt can be deleted unless a role uses it.
- Every save makes a version (a copy of the editable blocks). The newest `PROMPT_VERSIONS_KEEP` (30) are kept; older ones
  are deleted by the client that saves. Restoring makes a new version. A save names the version it started from and is
  refused if the prompt changed meanwhile. A save made offline becomes the working copy at once and waits in the outbox;
  if the prompt changed elsewhere in the meantime it is not uploaded and the app offers "keep mine" or "take theirs".
- After a program upgrade the template may gain or change blocks. Each stored prompt records the `template_version` it was
  edited against; a new block uses its shipped text; a block that no longer exists is ignored with a warning.

**Speed and connectivity (design rules):**
1. An AI call never waits for the network. The final text per role is built once from an in-memory copy and rebuilt only
   when a block, the active choice or a placeholder value changes.
2. Start-up reads the local cache files first, so the app is ready at once; Firebase is read in a background thread and
   the UI is never blocked.
3. One live listener on `ksm/config` carries prompts, the active choice, the common settings and the device nodes together (small data). Old
   versions live in `ksm/config_versions`, which that listener does not download. One connection (the free plan allows 100).
4. Size: a prompt over about 6000 characters (about 1500 tokens) gets a soft warning, because the chat prompt is sent with
   every message; the preview shows the estimated tokens; the hard limit is 20000 characters.
5. A cloud failure never reaches the UI or blocks a local change; saves and settings changes reuse the Phase 4 outbox.
6. Automatic wire tests, run with every code change: the output-format example inside the default extraction prompt must
   give exactly one task when passed to `parse_tasks`; the default chat prompt must contain the trigger phrase. A later
   code change that alters the parser or the phrase without the template then fails a test.

**Sub-sessions (do not start N+1 before N's check passes; each fits one coding session):**
- [x] 5A Design lock (done 2026-10-29; 346 Python and 32 UI tests passed on the owner's PC). (1) Split the shipped prompts into blocks, locked or editable, WITHOUT changing the assembled
  default words (use `{TRIGGER_PHRASE}` in the shipped editable text); the split is in DATA_LAYOUT; the shipped file
  format and markers are the developer's choice. (2) Move the audit prompt out of `strategy_officer.py` into
  `prompts/strategy_audit.txt` (the old test that counts prompt files expects 3 and must become 4). (3) The templates and
  the wire checks as a small module (`app/prompt_templates.py`). (4) Add `scope` (COMMON, PER-DEVICE, LOCAL ONLY or SECRET) to every `SettingDef` as in the table, add `THEME`, the device id file and
  `SYNC_PROMPTS` (bool, default true), `SYNC_SETTINGS` (bool, default true) and `PROMPT_VERSIONS_KEEP` (int, 30, 5 to 200).
  (5) Extend `database.rules.json` for `ksm/config` and `ksm/config_versions` (shapes, size limits, the key lists for common and
  per-device settings, owner id check). Check: assembled default prompts equal the old files and the old audit constant apart from
  blank lines; the wire tests of rule 6 pass; a test that every setting has exactly one scope and that no SECRET or LOCAL ONLY
  key is in a rules list; rules file is valid JSON; existing settings and prompt tests pass.
- [x] 5B `app/prompt_store.py`, no UI (done 2026-10-30; 84 new tests; UI tests, live Firebase and the emulator not run in that session; see the notes under this list). List, read, save (version, concurrency check, guard rules), restore, reset block and
  prompt, delete, set active, seed, assemble, self-check, order of winners, config-folder cache, version pruning by
  `PROMPT_VERSIONS_KEEP`. One shared Firebase app (`firebase_app`), the `CloudStore` interface and its fake. Check:
  fake-cloud tests for: offline fallback; a block with the literal phrase is refused on save and ignored at use; a
  Firebase block named like a locked block is ignored; a prompt whose wire is missing falls back to the shipped default;
  seed never overwrites; delete refused for built-in and for an active prompt; pruning keeps exactly the set number;
  concurrent save refused; offline save kept and offered "keep mine / take theirs"; a key in the text is not uploaded;
  switch off sends and reads nothing; a new block after an upgrade uses its default; the prompt is built without any
  network call after the first load.
**What 5A and 5B built (folded 2026-10-30; the code wins over the plan text above where they differ):**
- 5A: `app/prompt_templates.py` holds the four role templates (ordered blocks, locked or editable, one insert point), the parser for
  the block files (markers `@@@ block <id> <editable|locked> [tight]` and `@@@ insert`), `assemble()`, `fill()` and `wire_problems()`.
  `TASK_TRIGGER_PHRASE` is defined there once. The audit prompt moved to `prompts/strategy_audit.txt`; the four prompt files use the
  block format and the assembled default equals the Phase 4 prompt word for word (pinned in `tests/fixtures/phase4_prompts/`), with one
  exception: the audit text sent to Gemini has two line breaks, not three, before `HERE IS MY CURRENT MATRIX DATA:`. Locked blocks:
  chat `trigger_rule`, `confirmation`; extraction `input_context`, `dedup_reply`, `categorization`, `output_format`; audit
  `data_description`; screen scan none. Plain-text prompt files are no longer accepted, so local edits to `prompts/*.txt` are replaced
  by the shipped files on upgrade. `SettingDef` has `scope` (COMMON 11, PER-DEVICE 7, LOCAL ONLY 4, SECRET 2) and `hidden`; four hidden
  settings were added (`SYNC_PROMPTS`, `SYNC_SETTINGS`, `PROMPT_VERSIONS_KEEP`, `THEME`); `device_identity()` makes `device.json`
  (id `d-` plus 8 hex digits, name defaults to the computer's name); `database.rules.json` has rules for `ksm/config` and
  `ksm/config_versions`. Nothing reads the new settings or the device id yet.
- 5B: `app/prompt_store.py` (`PromptStore`; list, read, save, save as new, restore, reset block and prompt, delete, set active,
  resolve conflict, seed, sync, build, preview, validate) and, in `cloud_sync.py`, `TransactionAbort`, `SERVER_TIME`, `transaction()` on
  both clouds and the fake `MemoryTreeCloud`. A save is a conditional write (version check inside a transaction), then a history entry,
  then pruning. Offline saves wait in the `pending` part of `prompts_cache.json`, not in the Phase 4 outbox (that outbox does plain
  writes only). Several offline saves of one prompt upload as one version. Reset writes the shipped text instead of deleting nodes.
  Seed creates a built-in prompt at version 0 and adds only missing blocks. `SYNC_PROMPTS` and `PROMPT_VERSIONS_KEEP` are read from the
  settings store directly. Nothing calls `PromptStore` yet; `apply_snapshot` is ready for the 5D listener.
- Still unproven for 5A and 5B: `FirebaseCloudStore.transaction` and the server clock inside a transaction (`{".sv": "timestamp"}`)
  were tested against fakes only (if refused, change `_wire_node` to a client time, one line); the rules file has not run against the
  emulator; `REPLACE_WITH_OWNER_UID` is still a placeholder; 7 tests failed in the 5B sandbox only because `pywebview`, `groq` and
  `google-genai` could not be installed there, so the owner should confirm a clean run on the PC (about 430 expected); the UI tests were
  not run after 5B (no file under `app/gui` changed).
**What 5C and 5D built (folded 2026-10-30; the code wins over the plan text above where they differ):**
- 5C: `app/shared_settings.py` (`SharedSettings`): paths `ksm/config/settings/<KEY>` and `ksm/config/devices/<id>/settings/<KEY>`, node
  `{value, updated_at, updated_by}`; 18 shareable keys, secret and local-only keys have no path; seed never overwrites and skips keys with a
  waiting edit or an environment override; `apply_snapshot(settings, devices)` applies valid values through `store.set` (Firebase wins when it
  differs; the store's `.bak` keeps the old value); edits queue in `shared_settings_state.json` and go up in a background thread with a
  per-key `updated_at` check, a losing edit gives a notice (`notices()`, `dismiss_notices()`); key-like values, values over 2000 characters and
  invalid values are never sent or accepted; `register()` writes name, kind, `last_seen`. `SettingsStore.value_ignoring_env()` added.
  Limits: a failed send is retried only at the next `sync()`, `flush()`, edit or start (5D's hub adds the timer); `register()` keeps the local
  device name; a setting deleted in the console is re-seeded from the local value.
- 5D: `listen` on both clouds, `TreeMirror`, `ListenHandle` in `cloud_sync.py`; `app/prompt_use.py` (`built`, `text`, `notes`) is the only way the AI
  modules reach the library, with a warning when cache or shipped text is used instead of the online one; `app/config_sync.py` (`ConfigSync`,
  `get_hub`, `start_config_sync`) owns the one listener on `ksm/config`, feeds `PromptStore.apply_snapshot` and `SharedSettings.apply_snapshot`
  only while their own switch is on, merges bursts, skips unchanged parts, retries with a growing pause, and calls `config.reload()` after a
  setting was applied. `main_window` starts it before the assistants and stops it in `finally`. `tests/conftest.py` resets the process-wide
  instances around every test. Ready for 5E and not yet shown anywhere: `ConfigSync.status()`, `prompt_use.notes()`, `SharedSettings.notices()`
  and `.overridden()`; `WebAPI` does not hold the hub, 5E decides how the page asks for it (methods only, RULEBOOK section 7).
- Still unproven for 5C and 5D: real Firebase `listen` (first event, reconnect, one connection on the free plan; if the SDK listener dies
  silently after a network loss, add a watchdog), real `transaction` with the server clock (if refused, use a client time for `updated_at` in
  `_send_one` and `seed`), the rules file against the emulator, `REPLACE_WITH_OWNER_UID`. Confirm these first in 5F or earlier.
- [x] 5C `app/shared_settings.py`, no UI (done 2026-10-30; 66 new tests, 496 Python and 32 UI passed; live Firebase and emulator not run; see the notes under this list). Connects `SettingsStore` to Firebase for COMMON and PER-DEVICE settings (device id, registry, `last_seen`): seed, live update of the
  saved value, per-key conflict check by `updated_at`, outbox, notice, environment override never uploaded. Check:
  fake-cloud tests with two devices; an offline edit that conflicts; SECRET and LOCAL ONLY keys never uploaded or read from
  Firebase even if present there; a first sync does not overwrite; switch off sends and reads nothing; settings
  tests of Phase 3 still pass (the keychain and `.env` behaviour must not change).
- [x] 5D (done 2026-10-30; 39 new tests, 535 Python and 32 UI passed; live Firebase listener and emulator not run; see the notes under this list) Wire the consumers and the live listener. `ai_assistant.chat`, `validate_and_extract`, `strategy_officer` and
  `screen_analyzer` get their text from `prompt_store`; the one listener on `ksm/config` feeds `prompt_store` and
  `shared_settings`; a change made elsewhere applies to the next AI call without a restart; a clear warning when a shipped
  or cached block is used instead of a Firebase one. Check: all earlier tests pass; an edited block and a changed shared
  setting reach the next call; a bad block from the cloud is ignored with a warning; start-up does not wait for the
  network (a test with a cloud that never answers).
- [x] 5E (done 2026-10-31; 25 new Python and 23 new UI tests, 560 and 55 passed; real window, live Firebase and emulator not run; see the notes under this list) `WebAPI.prompt_*` and settings methods (methods only, never public attributes: RULEBOOK section 7) and the screens.
  PROMPTS tab: for each prompt the blocks in order, locked ones grey with a lock and read-only, editable ones with a text
  box, an on/off switch and "reset block"; add your own block; live guard check; a preview of the final text with the
  placeholders filled in and the estimated tokens (uses no tokens from the AI); Save, Save as new, history with Restore,
  Reset or Delete, Set in use. SETTINGS tab: the badge (Common, This device, Local only, Secret) on every setting,
  the values of this device, the theme and the new switches. Check: API
  tests; jsdom UI tests; the four themes in headless Chromium; a failed call never shows a crash.
**What 5E built (folded 2026-10-31; the code wins over the plan text above where they differ):**
- `WebAPI` (methods only): `prompts_get`, `prompt_get`, `prompt_validate`, `prompt_preview`, `prompt_save(prompt_id, changes, base_version, note)`,
  `prompt_save_as_new`, `prompt_restore`, `prompt_history`, `prompt_reset_block`, `prompt_reset`, `prompt_delete`, `prompt_set_active`,
  `prompt_resolve_conflict(prompt_id, "mine"|"theirs")`, `prompt_new_block_id`, `config_status`, `config_dismiss_notices`, `get_theme`. The
  block changes travel as a JSON string `{block id: {text?, enabled?, title?, order?} | null}`. The prompt library, shared settings and hub are
  private optional constructor parts (default: the process-wide instances, looked up at call time). `settings_get` returns a `shared` block.
- Screens: PROMPTS tab and the SETTINGS additions as listed in CHANGELOG. `THEME` is the only setting still hidden; `SYNC_PROMPTS`,
  `SYNC_SETTINGS` and `PROMPT_VERSIONS_KEEP` are shown. "The values of this device" is the THIS DEVICE badge plus the device name and id line, not
  a separate list. No header chip for the shared configuration (status is on the PROMPTS tab and in the SHARED SETTINGS box). The PROMPTS status
  refreshes every 3 s only while the tab is open.
- Still open after 5E: the real window (pywebview, WebView2) has not shown these screens; real Firebase `listen` and `transaction`, the rules
  emulator and `REPLACE_WITH_OWNER_UID` as in the 5C and 5D note above. Left from 4F-5 and now fixed: slate `--text2` contrast.
- [ ] 5F Real-world check by the owner and documentation fold. Owner steps: start with both switches on and see the prompts
  and the settings appear in the Firebase console (this PC's own values under `config/devices`); change the chat persona in the app and ask a question; add an
  `extra` instruction and see it followed; try to type the trigger phrase into an editable block and see the save
  refused; change a model name in the Firebase console and see the app follow without a restart; change this PC's screen width and see it under your device in the console; switch Wi-Fi off, edit,
  switch it on and see it upload; confirm that no API key appears anywhere in Firebase; check that adding a task through
  chat still works; Reset to default.

**Open for the owner:**
1. Review the refined locked/editable split in `firebase/DATA_LAYOUT.md` (the plan now locks much less than before).
2. API keys. The plan does NOT put the two API keys in Firebase: a rules mistake, or access to the owner's Google account or
   the Firebase console, would expose them (the keys exposed before Phase 1 are the reason for caution). If the owner wants
   them there anyway, the plan would add a separate SECRET-in-Firebase option with its own switch, default off, and a
   warning. Today each device keeps its keys in its own keychain, typed once per device.
3. Device names default to the computer's name. Is that fine?

### Phase 6: Multi-writer Firebase, web app and Android app (SCOPED 2026-10-28, DEFERRED: the owner chose to finish the desktop project first, see Phase 5; not started)
**Owner decision (2026-10-28), replaces the earlier scope of this phase (apps sending commands to the desktop).** Firebase Realtime Database is the ONE source of
truth. The desktop app, the website and the Android app are equal clients: each reads and writes tasks directly. The
earlier plan (apps send commands to `ksm/commands`, the desktop applies them) is dropped, because with the PC off the
phone could only queue work, which is the access problem this phase exists to fix. The Phase 4 rule "single writer for
tasks" no longer holds from Phase 6 on (see the labeled note under Phase 4 decisions).

**Decisions (change only with the owner):**
- Sign-in with the owner's Google account (Firebase Authentication). Rules allow only the owner's user id. The Firebase
  web configuration (project id, web API key) is public by design; the rules are the protection. The web API key is
  restricted to the hosting domain in the Google Cloud console.
- Stay on the free Spark plan. No Cloud Functions, no server of our own.
- AI keys (Groq, Gemini) are held on each device and entered there once: OS keychain on the desktop, device storage in
  the Android app, browser storage on the website. They are NEVER stored in Firebase and NEVER shipped inside the
  hosted web files. AI chat and strategy audit run on the device that asks, so they work with the PC off. Their results
  are written to Firebase like any other record. Screen Scan stays desktop-only (it captures that PC's screen).
  Owner accepts that anyone with access to a device or browser profile could read the key stored there.
- One codebase for web and Android: an installable web app (PWA) on Firebase Hosting, wrapped as an APK (Capacitor)
  for personal use. Neither `CODE/web/` nor `CODE/android/` may contain keys or the service account file.
- The desktop keeps the service account (firebase-admin, bypasses rules). The web and Android clients use the
  Firebase client SDK signed in as the owner, so the rules apply to them.
- The local task file `data/matrix_data_pro.json` stays, but only as a cache and a backup. After migration it is never
  uploaded as a whole again (this prevents a stale desktop from bringing back a task deleted on the phone).
- Prompts and all settings except the API keys are already in Firebase by then (Phase 5, `ksm/config`), so every client behaves the same.

**How tasks stay correct with several writers (design rules, details in `firebase/DATA_LAYOUT.md`, "Phase 6 layout"):**
1. New task ids are Firebase push keys (unique even offline). The 14 existing tasks keep their numbers as text keys
   ("101" to "114"). The task `id` becomes text everywhere in the code, UI and tests.
2. A change writes only the fields it changes, never the whole task, so two devices editing different things both win.
3. `imp` and `urg` only go up (today's rule: a task can only move to a higher cell). A move is a transaction that
   stores the higher of the two values, and the rules file rejects any lower value. Concurrent moves therefore agree.
4. History is a map of entries, not an array. Each entry is `{at_ms, line}`; sort by `at_ms`. Automatic entries use a
   fixed key (`auto-24h`, `auto-1w`) so two devices writing it at once leave one line.
5. A delete is a marker (`deleted: true`), so it reaches every device and is never undone by an old copy.
6. Auto-escalation is computed by every client from `expected_at` and the real time, and saved with rule 3. While the
   desktop "Warp" (time-warp) tool is not zero, nothing is written to Firebase from the escalation.
7. `updated_at` comes from Firebase's server clock (`{".sv":"timestamp"}`), never from a device clock.
8. All stored date-times are the owner's wall-clock time (India Standard Time, Asia/Kolkata), read as such on every
   device whatever its own timezone. Open item: confirm with the owner.
9. Offline: every client keeps a small queue of changes in the same operation format (add, move, delete, history,
   chat, audit, scan) and replays it when online. Replay is safe because operations are field-level and only go up.

**Acceptance scenarios for the whole phase (an end-to-end script the owner runs on real devices in 6H):**
- S1 PC off: phone adds a task; when the PC is switched on it appears without any action.
- S2 Phone and website change different tasks at the same moment: both changes are kept.
- S3 Phone offline adds a task; after reconnecting it arrives exactly once.
- S4 Two devices move the same task: the higher cell wins, never a downgrade.
- S5 Task deleted on the phone while the PC was off: after the PC starts it stays deleted.
- S6 AI chat and audit from the phone with the PC off: the answer is saved and visible on the other devices.
- S7 A signed-in user who is not the owner is denied everything; an unsigned client is denied everything.

**Sub-sessions (do not start N+1 before N's check passes; each fits one coding session):**
- [ ] 6A Design lock, rules and emulator tests. Write the v2 layout into `firebase/DATA_LAYOUT.md` as built (the planned
  layout is already described there), extend `database.rules.json` (new task shape, only-up `imp`/`urg`, `device` field on chat, audit and scan records, owner id check), add a Firebase emulator rules test.
  Check: emulator tests show owner allowed, other user denied, unsigned denied, a lower `urg` rejected, a malformed
  task rejected. Also in 6A: owner supplies where the existing phone app's source is (see open questions).
- [ ] 6B Desktop core, split because it is the biggest part:
  - [ ] 6B-1 Operation layer `app/cloud_tasks.py` (add, move by transaction, delete, append history, field set) against
    the existing `CloudStore` interface, `MemoryCloud` fake extended with transactions, and the operation queue.
    Check: tests with two fake clients writing at once (S2, S4), queue replay (S3), cloud down never raises into the UI.
  - [ ] 6B-2 `TaskStore` becomes a cache fed by a live listener on `ksm/tasks`; text ids; history map; GUI, `web_api`
    and `app.js` updated; the old whole-record upload is removed. Check: earlier tests updated and passing, UI tests
    pass, S5 (stale cache never resurrects a deleted task) proven by a test.
  - [ ] 6B-3 One-time migration of the local tasks and of the Phase 4 cloud records (`updated_at_ms`, history array)
    to v2, after an automatic backup of `matrix_data_pro.json` (RULEBOOK section 7); `ksm/meta.schema` set to 2 last;
    legacy history keys fixed (`L000`, `L001`, ...) so running it twice changes nothing. Check: 14 tasks and every
    history line preserved; second run changes nothing.
  - [ ] 6B-4 Auto-escalation computed and saved by rule 6; Warp never writes. Check: tests incl. two clients bumping
    at once leave one history line.
- [ ] 6C Shared records (prompts and settings were done in Phase 5); chat sessions are per device (`<time>-<device>`); audit and scan records carry `device`. The
  "Keep tasks online" switch is removed (tasks are always online); the chat and audit/scan switches stay.
  Check: tests with the fake cloud.
- [ ] 6D Web app skeleton: `CODE/web/`, Google sign-in and sign-out, read-only views (3x3 matrix, chat history, audits,
  scans, sync status), phone and desktop layouts, the four themes. Includes a short spike that a browser can call Groq
  and Gemini directly (cross-origin access); the result is written in the session note and decides 6E. Check: jsdom
  UI tests; screenshots at phone and desktop widths.
- [ ] 6E Web writes and AI: add, move, delete tasks with the offline queue (IndexedDB) and a cached last view for
  offline reading; key entry screen (keys stay on the device); AI chat and audit in the browser using the shared
  prompts; AI-created tasks go through the same add operation. Check: tests for the queue and the AI task parser
  (shared fixtures with the Python parser so both give the same result); S2, S3, S4 run against the emulator.
- [ ] 6F Installable app and hosting: PWA manifest and offline shell, deploy to Firebase Hosting, authorized domains
  limited to the hosting address, API key restricted by referrer, strict content-security policy, no third-party
  scripts (browser storage holds the keys). Check: owner installs it from the Hosting address on the phone.
- [ ] 6G Android package: Capacitor wrap, secure key storage, native Google sign-in (sign-in inside a web view is
  often blocked, so this has real risk and gets its own check), private APK. Check: owner installs the APK, signs in
  and completes S1 and S6.
- [ ] 6H Owner check on real devices (S1 to S7), then retire `incoming_tasks`, `ai_responses` and the mobile bridge
  (after the old phone app is no longer used), update the rules file, and fold the documentation.

**Known limits to tell the owner:** the Spark plan has no automatic database backups, so the desktop's daily local
backup and the Export button stay important, and the web app gets an Export too; the website loses queued changes only
if its tab is closed while offline, the Android app keeps them; free-plan limits (1 GB stored, 10 GB a month download,
100 simultaneous connections) are far above one person's use; the desktop's firebase-admin bypasses the rules, so
client-side checks and tests must protect the data from a desktop bug; the old keys exposed before Phase 1 must still
be rotated.

**Open for the owner:**
1. Where is the source of the existing phone app (tech used, how it signs in, how it calls the AI)? Reuse its code or
   rebuild it as the PWA/Capacitor app? (Publishing the rules file will block it until it signs in as the owner.)
2. Today "finishing" a task means deleting it. Add a separate `done` state (kept, shown greyed, can be reopened)?
3. Should "Keep chat history online" and "Keep audits and screen scans online" default to on now that other devices read
   them?
4. Confirm the stored-time rule (India Standard Time).
5. Private APK or Google Play; `*.web.app` address or a custom domain (still open from before).

## Ideas / not yet scheduled
- Turning "Keep chat history online" on while the app runs does not load earlier chat history into the running session (only at start); new messages are saved at once
- An "Open folder" button for the `exports/` folder (Settings already has one for the config folder)
- Slate theme: `--text2` (#6b7280) on `--bg-panel2` is 4.23:1, slightly under 4.5:1 (grey "Off" chip, small grey notes); change only if the owner finds it hard to read
- `get_sync_status` shows one "last sync" time for all kinds (taken from the shared engine), not one per kind
- Chat records have no timed retry (tasks retry after 60 s, doubling to 10 min): chat waiting in the outbox goes out at the next start or next message
- Decide whether the Mobile Sync log should also be kept online (memory only today; not part of 4D or 4E)
- Pull on start runs in the background, so on an empty machine the first seconds show the local list before a restore from the cloud is applied
- (Now part of Phase 5, 5A) Move the strategy audit prompt out of `strategy_officer.py` into `prompts/`
- Remove unused UI colour constants from `config.py`
- Give the chat input focus again after a reply (it is disabled while the AI is busy)
- `send_chat` reads the busy flag outside the lock (small race with mobile messages)
- Delete, migrate and auto-escalation keep their in-memory change if a save fails (error is raised and logged)
- Run both test suites automatically (e.g. GitHub Actions) on every push
- Confirm with the real phone app that the `ai_responses` reply format is what it expects
- Settings: when no OS keychain exists, encrypt the fallback file with a key derived from a passphrase (today it is a plain file readable only by you)
- Settings: let mobile sync start or stop without restarting the app
- Settings: a "rotate this key" reminder (age of each saved key) and a one-click "open the provider's key page"
- Settings: per-profile settings (for example work and personal) selected at start-up
