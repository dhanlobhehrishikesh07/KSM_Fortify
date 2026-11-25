# Changelog

**Last updated:** 2026-10-31 10:40

Only the current phase and the previous phase are kept in full. Older phases move to
`PASSIVE_DOCS/retired_changelog_phases/` with a one-line pointer left here.

## Planning notes (no code changed)
2026-10-29 07:07, documentation session (Type 2). Phase 5 (shared configuration in Firebase: prompts with locked and editable blocks, and
ALL settings except the two API keys and four settings needed to reach Firebase, desktop only) and Phase 6 (several devices
write tasks directly; web and Android app; AI keys held on each device) were scoped in ROADMAP.md with owner decisions;
Phase 6 is deferred. Owner decisions recorded: both new sync switches are on by default, 30 prompt versions kept (a setting),
all settings stored in Firebase (common ones once, per-device ones under the device id), upload switches become common, the
locked part of the prompts reduced to the wire the program reads. README.md gained a "Planned architecture" section;
`firebase/DATA_LAYOUT.md` gained the planned shared configuration layout and the planned Phase 6 layout. **Correction:**
`firebase/DATA_LAYOUT.md` said audits and scans store a field `text`; the code and the rules file use `answer`. The doc was
wrong and was fixed; the code was right.

## Phase 1: Baseline, project as received (COMPLETE)
Retired to `PASSIVE_DOCS/retired_changelog_phases/PHASE_1_baseline.md`. One line: the project arrived as one unified six-tab app with no tests; secrets had been moved to `.env`.


## Phase 2: Reliability and hygiene upgrade (COMPLETE: automated checks done, manual checks outstanding)
Retired to `PASSIVE_DOCS/retired_changelog_phases/PHASE_2_reliability_hygiene.md`. One line: data-file protection, visible Firebase errors with a message queue, clean prompts, a forgiving parser, a calmer UI and the first test suite (72 Python, 10 UI tests).


## Phase 3: Settings tab and common key storage (COMPLETE: automated checks done, manual checks outstanding)
Retired to `PASSIVE_DOCS/retired_changelog_phases/PHASE_3_settings_tab.md`. One line: the SETTINGS tab and one upgrade-proof place for keys and settings (settings store, live config, `WebAPI.settings_*`, test buttons); a real-window failure found later (public `WebAPI` attributes exposed 744 functions) was fixed in 3G and is pinned by a test (145 Python, 19 UI tests).


## Phase 4: Online sync with Firebase (COMPLETE: automated checks and the owner's real-window check done; some manual checks outstanding)
Goal: keep the owner's data (tasks, chat history, audits and scans) also in the owner's own Firebase
Realtime Database, without ever making the app depend on the internet. The local JSON file stays the working copy.
Each kind of data has its own switch in SETTINGS (group "Online copy"), all off by default. Result so far: 289 Python
tests and 32 UI tests pass (Phase 3 ended at 145 and 19): 289 on Linux, and on the owner's Windows machine 286 passed and
3 skipped on purpose. Sub-sessions 4A to 4F were tested against a fake cloud; 4G was the owner's check on the real machine.
- **4A: Switches, rules, layout.** Settings `SYNC_TASKS`, `SYNC_CHAT`, `SYNC_AUDITS_SCANS` (bool, default off,
  marked "restart needed" at the time, changed in 4F-2; the engine is built at start only if a switch is on, and 4C/4D also check each switch live). `firebase/database.rules.json` denies everything by
  default and opens `ksm`, `incoming_tasks` and `ai_responses` to one user id only (placeholder
  `REPLACE_WITH_OWNER_UID`), with light shape checks. `firebase/DATA_LAYOUT.md` documents paths, who writes what, what
  is never uploaded and a one-minute check that the console is not in open "test mode". (The ROADMAP asked for the
  layout in README; a coding session may not edit README, so it lives in `DATA_LAYOUT.md` and README points to it.)
- **4B: Sync engine.** `app/firebase_app.py` is the single place that creates the Firebase app (the bridge and the
  online copy share it). `app/cloud_sync.py` holds the `CloudStore` shape with `MemoryCloud` (tests) and
  `FirebaseCloudStore` (every failure becomes a plain-words `CloudError`), and `SyncEngine`: `record_tasks`, `flush`,
  `pull_tasks`, `queue_record` (redacts keys, validates paths), `status`. Offline changes wait in an outbox kept with the
  sync state in `sync_state.json` in the config folder (atomic writes; a damaged file is set aside as `.corrupt-<time>`;
  outbox capped at 5000, oldest chat/audit/scan records dropped first, never tasks). Conflicts are last-write-wins per
  task; a deletion travels as a delete marker. With no sync history for a task that differs from the cloud, the local
  copy wins. The time of a change is the time the engine first sees it, kept in the state file, so the local task format
  is unchanged.
- **4C: Task sync and first upload.** `app/task_sync.py` subscribes to `TaskStore`; each change goes into the outbox
  on the calling thread and a background thread sends it (retry after 60 s, doubling to 10 min). At start it pulls,
  and only if the pull worked takes a backup, applies the pull and flushes. The task file is copied to
  `data/backups/matrix_data_pro.json.pre-sync-<first-upload|restore>-<time>` and compared byte for byte before the
  first upload and before any pull that changes the local list; if the backup fails or differs, nothing is sent or
  changed. `TaskStore` gained `save_blocked` and `apply_cloud_changes` (applies the pull as a change on the current
  list, skips an added task whose id is already used, restores memory if the save fails). Keys inside task text are
  removed before upload. Switch off means no network call, no backup, no file written, nothing queued.
- **4D: Chat history.** `app/chat_sync.py` saves each user message and AI reply (also from the phone) under
  `ksm/chats/<session>/m000000, ...` (role, text, time; text cut at 20000 characters; keys removed also in the outbox
  file). A session is one app run, and a new one starts after "Clear context", which writes a `marker` message. On
  start the last 20 messages are loaded (newest sessions first, stopping at a clear marker, skipping the running
  session) so the AI remembers earlier chats; a load that finishes after "Clear context" is discarded. Chat is not saved
  on this machine: with the switch off it stays in memory only. `session_history` is capped at 60 lines
  (`config.CHAT_HISTORY_LIMIT`), which closes the old idea about the Groq payload growing. `SyncEngine.flush(prefix)` and
  `pending_count(prefix)` make tasks and chat each send only their own paths, so a switch that is off never sends its data
  because another is on (a follow-up flagged in 4C). `main_window.start_online_copy(store)` builds one shared engine and
  starts both. `firebase/database.rules.json` validates chat rows.
- **4E: Audit and scan history.** `app/record_sync.py` (`RecordSync`) saves each successful strategy audit under
  `ksm/audits/<time_key>` (question, answer, time) and each screen scan under `ksm/scans/<time_key>` (question, answer,
  monitor, model, time), when "Keep audits and screen scans online" is on. Never the image, preview, sizes or the task
  matrix (the audit sends a fixed question label); failed runs are not saved. Answers are cut at 40000 characters,
  questions at 2000; same-second records get a `-NN` suffix; keys are removed by the engine. `WebAPI` takes an optional
  `record_sync` argument and calls two private hooks after a successful result; a hook error is logged and never
  reaches the UI. `start_online_copy` now returns `(task_sync, chat_sync, record_sync)`. `cloud_sync.py` gained only the
  `AUDIT_ROOT` and `SCAN_ROOT` constants; `database.rules.json` validates both paths. 21 new tests (264 Python, 19 UI).
- **4F: Sync status and controls** (five sub-sessions, 289 Python and 32 UI tests at the end):
  - **4F-1, backend.** Three new `WebAPI` methods (methods only, nothing public added). `get_sync_status()` returns
    `{ok, state, label, pending, last_sync, last_error, kinds}`; state is Off (no switch on), Offline, Pending N or Synced,
    each kind counted only if its switch is on, error text redacted, never raises. `sync_now()` asks each switched-on sync
    to send in the background and returns at once (with all switches off it sends nothing and says so).
    `export_cloud_data()` reads tasks, chats, audits and scans from the cloud (all-or-nothing, `SyncEngine.export_all`),
    removes keys and writes `exports/ksm-cloud-export-<time>.json` in the config folder (atomic, owner-only mode, never
    overwrites); it is download only and works with all switches off. 19 new tests. (In that session's offline sandbox 16
    older settings tests failed because packages such as keyring, groq and google-genai could not be installed; the same 16
    failed on the untouched zip, so they were an environment gap, not a regression.)
  - **4F-2, switches live.** The workers already read their switch on every call, so the `restart=True` flag on
    `SYNC_TASKS`, `SYNC_CHAT` and `SYNC_AUDITS_SCANS` was wrong and was removed (help text now "Applies at once"; the
    RESTART badge is gone). One real gap was fixed: turning tasks on while the app runs did nothing until the next task
    change; a private `_apply_sync_switches` now starts the first upload (and the pre-sync backup) at once. Proof: 3 of the
    new tests fail on the old code. Known limit: turning chat on live saves new messages at once but does not load earlier
    history until the next start.
  - **4F-3, header chip.** A small chip next to the clock shows the backend label (Off, Synced, Offline, Pending N),
    refreshed every 2 s without overlapping calls, with a tooltip (last sync time, redacted error, or "Nothing is sent
    online"). A failed call shows "Unknown"; text is set as text, never HTML; theme variables only. 5 new UI tests.
  - **4F-4, MOBILE SYNC panel.** An ONLINE COPY panel with the last sync time, a Sync now button, an Export cloud data
    button (shows "Saved to: <path>" with counts) and the note that nothing is uploaded unless a switch is on. Buttons are
    disabled while a call runs; a missing method, rejected call or odd answer becomes a message. 7 new UI tests.
  - **4F-5, themes.** Chip and panel rendered in all four themes (and a 700 px window) in headless Chromium with a fake
    `pywebview.api`. One bug: in the slate (light) theme the status colours were too pale for 10 px text (Synced 3.02:1,
    Pending 3.82:1, success message 3.45:1). Fixed by darkening `--good` to #157a44 and `--warn-bright` to #8f5a0a (now
    4.72, 5.06 and 5.39); the other themes were already 6.4:1 or better. One new UI test checks all three status colours
    reach 4.5:1 in every theme; it fails on the old CSS. Left as is: slate `--text2` is 4.23:1 on the second panel colour.
- **4G: Owner's real-world check (2026-10-28).** The owner ran both suites on Windows. UI: 32 of 32 passed. Python: 285 passed,
  3 skipped on purpose, 1 failed. In the real window: the header chip changed correctly (Off, Synced, Off); the MOBILE SYNC
  panel was correct and readable in the slate theme; the three Online copy switches applied at once with no restart note; the
  Firebase console rules were already locked (not open test mode); tasks were uploaded and are visible in the console; Sync now
  and Export cloud data both worked and the export file is on disk and opens; the terminal showed no red errors.
  - **4G-1, test fix (no app change).** The one failure was `test_second_start_with_lost_sync_state_is_still_idempotent`: it
    compared the task file's bytes with text using `\n`, but Windows text-mode writes give `\r\n`. The assertion in
    `tests/test_task_sync.py` (line 130) now maps `\r\n` to `\n` first, so it still checks content and `indent=4` formatting but
    not the operating system's line ending. No file under `app/` changed. Proof: with Windows newlines simulated on Linux the
    old assertion failed and the new one passed. The owner's rerun on Windows: 286 passed, 3 skipped, 0 failed.
**Correction (if any):** the ROADMAP planned 4C's check as "all 145 earlier tests still pass"; the real count at that
point was 184 (145 passed + 1 skipped at 3G, then +3 in 4A and +35 in 4B). Nothing was wrong in the code; the ROADMAP line was reworded.
**Checked on the owner's machine in 4G:** the list in the 4G entry above (real window, real Firebase project, real Windows
test run). `data/matrix_data_pro.json` stayed byte-identical through all automated testing.
**Still not verified (not reported by the owner in 4G; the list is in STATE.md):** the shipped rules file itself (the owner
confirmed the console is not in open test mode; whether it is `database.rules.json` with the owner's user id was not
recorded, and no emulator test exists, planned for 5A); a real Groq chat with loaded history and Clear context; a real
Gemini audit or screen scan appearing under `ksm/audits` and `ksm/scans`; the backup file in `data/backups/`; adding and
deleting a task and an offline add reaching the cloud; a real phone app. The Phase 2 and Phase 3 manual items are also still
open. The old exposed keys from before Phase 1 still need to be rotated; Phase 4 does not do that.
**Net result:** with the switches on, tasks, chat history, audits and screen scan answers reach the owner's own database
(tasks and chat also come back after a restart); with them off nothing leaves the machine. The header chip shows the state,
the MOBILE SYNC panel offers Sync now and Export cloud data, and the switches act at once. Task upload, Sync now, Export and
the status display are proven on the owner's real machine; chat, audit and scan upload are proven against a fake cloud only.
**Folded from (retired to PASSIVE_DOCS/retired_session_notes/):** SESSION_2026-10-27_0613_4A.md,
SESSION_2026-10-27_0629_4B.md, SESSION_2026-10-27_0945_4C.md, SESSION_2026-10-27_0950_4D.md
**Folded from (retired to PASSIVE_DOCS/retired_session_notes/), 2026-10-28:** SESSION_2026-10-27_1410_4E.md,
SESSION_2026-10-27_2015_4F1.md, SESSION_2026-10-27_2230_4F2.md, SESSION_2026-10-27_2330_4F3.md,
SESSION_2026-10-27_2345_4F4.md, SESSION_2026-10-28_0010_4F5.md
**Folded from (retired to PASSIVE_DOCS/retired_session_notes/), 2026-10-28 01:00:** SESSION_2026-10-27_2110_4G1.md


## Phase 5: Shared configuration in Firebase (IN PROGRESS: 5A to 5E done, 5F next)
Goal: prompts and all settings except the two API keys and four settings needed to reach Firebase live in the owner's Firebase, so
every client sees the same environment; the parts the program reads stay locked and ship with the program. Scope and decisions: ROADMAP.md.

- **5A, design lock (2026-10-29).** The three prompt files and the audit prompt (moved out of `strategy_officer.py` into
  `prompts/strategy_audit.txt`) were split into locked and editable blocks without changing the assembled words; the templates, the
  block-file parser, assembly, placeholder filling and the wire checks are in the new `app/prompt_templates.py`. `ai_assistant`,
  `strategy_officer` and `screen_analyzer` read their default prompt through it (no Firebase, no overrides yet). Every setting got a
  scope, four hidden settings and the device id file were added, and the Firebase rules gained `ksm/config` and `ksm/config_versions`
  (key lists and locked block ids generated from the code; old rules unchanged). Result: 346 Python and 32 UI tests passed (289 before;
  2 come from the fourth prompt file in existing tests, 55 are new). Seven deliberate breakages were each caught by the new tests.
  One known difference from Phase 4: the audit text has two line breaks, not three, before the matrix data. Old plain-text prompt files
  are refused. Not run: the Firebase emulator (no Java or blocked hosts), any live call.
- **5B, prompt library (2026-10-30).** New `app/prompt_store.py` (`PromptStore`, no UI, not yet called by anything): read, save with
  version and concurrency check, guard rules (literal phrase, parser words, 4000 and 20000 characters, key text), restore, reset,
  delete, active choice, seed, assemble, self-check, cache `prompts_cache.json`, pruning to `PROMPT_VERSIONS_KEEP`; building a prompt
  uses memory only. `cloud_sync.py` gained `transaction()` on both clouds and the fake `MemoryTreeCloud`. 84 new tests; about 430
  expected on the owner's PC. Thirteen deliberate breakages of the store: 11 caught at once, 2 first missed (both redundant defences),
  and a test was added for each. Two bugs found and fixed while testing (a stale error blocking a later send; reset holding a lock
  during a network call).
**Correction (documentation, 2026-10-30):** `firebase/DATA_LAYOUT.md` rule 4 said a save is "one transaction" and rule 6 said reset
deletes the changed blocks. The code (5B) makes the version check and block write one transaction and writes the history entry just
after it (a failed history write stays pending and is retried), and reset writes the shipped text. The doc was changed to match;
the code was as designed. `README.md` also said the audit prompt is inline in `strategy_officer.py`; fixed.
**Not verified yet:** real Firebase behaviour of `transaction` and the server clock inside it; the rules file against the emulator;
the owner's clean test run after 5B; nothing in Phase 5 is visible in the app yet (that starts with 5D and 5E).
  (Superseded in part by the 5C and 5D entries below: the consumers now read the library; there is still no screen.)
**Folded from (retired to PASSIVE_DOCS/retired_session_notes/), 2026-10-30:** SESSION_2026-10-29_1843_5A.md, SESSION_2026-10-30_1041_5B.md
- **5C, settings in Firebase (2026-10-30).** New `app/shared_settings.py` (`SharedSettings`, no UI). COMMON settings live at
  `ksm/config/settings/<KEY>` and PER-DEVICE settings at `ksm/config/devices/<deviceId>/settings/<KEY>`, each node
  `{value, updated_at (server clock), updated_by}`; 18 keys are shareable, SECRET and LOCAL ONLY keys have no path at all. Seed
  writes a missing node only (never overwrites; first computer fills the console with all 18 keys, a second computer's differing
  saved values then lose to Firebase, its store backup keeps them). `apply_snapshot` applies valid COMMON values and this device's own
  values through `store.set`, so `config.reload()` runs; other devices' nodes, wrong places, bad shapes, key-like or invalid values are
  ignored with a warning. Local edits are queued in `shared_settings_state.json` and sent by a background thread; a per-key
  transaction aborts when `updated_at` is not the base, then the other value wins and a notice is kept. A setting an environment
  variable overrides is never queued, seeded or uploaded. `register()` writes name, kind and `last_seen` per device. `status()`:
  off, no_firebase, starting, synced, pending, offline. `SettingsStore.value_ignoring_env(key)` was added (no other change). 66 new
  tests (496 Python, 32 UI); a new test found one bug (an edit made while the first send of the same key finished kept the old base
  and lost to the owner's own first write; fixed). Of 17 deliberate breakages 16 were caught at once; the echo-guard one was first
  missed and got a test. Not run: real Firebase, the emulator, the window.
- **5D, consumers and live listener (2026-10-30).** `cloud_sync.py` gained `TreeMirror`, `ListenHandle` and `listen(path, on_tree)` on
  both clouds (one SDK listener; the consumer gets the whole subtree after each event; a consumer that raises never kills the
  connection). New `app/prompt_use.py`: the one door through which the AI modules get prompt text (`built`, `text`, `notes`); a
  broken library never reaches the AI call (the shipped text is used and the fault is logged), and a warning says so when online
  prompts are on but the text in use is the cache or the shipped file. New `app/config_sync.py` (`ConfigSync`): `start()` returns at
  once, one listener on `ksm/config` (never two, closed when both layers go off), runs `PromptStore.sync()` and
  `SharedSettings.sync()`, merges event bursts into the newest tree, skips unchanged parts, retries a failed connection (30 s
  doubling to 300 s), and watches the switches live. `ai_assistant`, `strategy_officer` and `screen_analyzer` read through
  `prompt_use`; `main_window` starts and stops the hub; no file under `webui` or `web_api.py` changed. 39 new tests (535 Python,
  32 UI). Of 23 deliberate breakages 22 were caught at once; the one missed (applying prompts while their switch is off) got a test.
  One existing test (`test_screen_scan_prompt_comes_from_the_blocks_and_falls_back`) now resets the prompt library after patching a
  prompt file; no assertion changed.
- **5E, prompts and settings screens (2026-10-31).** `WebAPI` gained methods only (no public attribute): `prompts_get`, `prompt_get`,
  `prompt_validate`, `prompt_preview`, `prompt_save`, `prompt_save_as_new`, `prompt_restore`, `prompt_history`, `prompt_reset_block`,
  `prompt_reset`, `prompt_delete`, `prompt_set_active`, `prompt_resolve_conflict`, `prompt_new_block_id`, `config_status`,
  `config_dismiss_notices`, `get_theme`; each answers with a plain dict, never raises, and passes message text through the key filter;
  `settings_get` also returns the shared status. New PROMPTS tab: a role picker, the prompts of the role (badges IN USE, BUILT-IN, EDITED,
  WAITING, CHANGED ELSEWHERE), the blocks in order (locked ones read-only with a lock, editable ones with text box, switch, character
  counter and Reset block, own blocks with a title and Remove block), a live guard check that disables Save on an error, Preview
  (final text, characters, estimated tokens, no AI tokens used), History with a two-step Restore, two-step Reset prompt and Delete
  (built-in and in-use prompts cannot be deleted), Set in use, Save as new, and Keep mine / Take theirs after a conflict. SETTINGS: a scope
  badge on every row (Common, This device, Local only, Secret) and "set by environment", a SHARED SETTINGS box (state, this device's name and id, edits that lost with Dismiss),
  the two sync switches and "versions kept" now visible (only `THEME` stays hidden), and the theme saved as the per-device setting `THEME`
  (applied at start without being written back). The page gets everything through one wrapper that never throws, and backend text is
  always set as text. One existing test was changed on purpose (the hidden-settings test now expects `THEME` only); the slate
  theme's `--text2` was darkened (4.23:1 to 4.84:1 on the second panel colour; the item left open in 4F-5). 25 new Python tests and 23
  new UI tests (560 Python, 55 UI); the four themes were drawn at 1280 and 700 px in headless Chromium with a fake backend (no overflow,
  no page errors). Of 17 deliberate breakages 15 were caught at once; two were first missed (redundant redaction, wrong active id) and
  got a test each.
**Not verified yet (5C to 5E):** real Firebase for `listen` (first event, reconnect after a network drop, one connection on the free
plan) and for `transaction` with the server clock; the rules against the emulator; `REPLACE_WITH_OWNER_UID` is still a placeholder; the
desktop window (the new PROMPTS and SETTINGS screens were driven only by jsdom and headless Chromium with fake APIs, and the tests call `WebAPI` directly, so the pywebview exposure problem of 3G cannot show there); the owner's run of both suites on the PC after 5B to 5E.
**Folded from (retired to PASSIVE_DOCS/retired_session_notes/), 2026-10-30 14:10:** SESSION_2026-10-30_1310_5C.md, SESSION_2026-10-30_1354_5D.md
**Folded from (retired to PASSIVE_DOCS/retired_session_notes/), 2026-10-31 10:40:** SESSION_2026-10-31_1030_5E.md
