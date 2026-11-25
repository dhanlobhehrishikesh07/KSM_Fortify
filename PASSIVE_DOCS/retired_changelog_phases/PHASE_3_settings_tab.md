# Retired from CHANGELOG on 2026-10-30 14:10

## Phase 3: Settings tab and common key storage (COMPLETE: automated checks done, manual checks outstanding)
Goal: stop re-creating `.env` and key files after every upgrade, and manage keys from the GUI like a
professional product. Result: 145 Python tests and 19 UI tests pass (73 and 9 new).
- **3A: Common store.** `app/settings_store.py`. Settings live in a per-user config folder outside the
  project (`%APPDATA%\KSM`, `~/Library/Application Support/KSM`, `~/.config/ksm`, or `KSM_CONFIG_DIR`).
  API keys go to the OS keychain (`keyring`); without one they go to an owner-only file and the tab shows
  a warning. Order of winners: real environment variable, saved setting, old `.env`, default. Every
  value remembers its source. Damaged `settings.json` is restored from `settings.json.bak`. A history
  file records who changed what (never values).
- **3B: Live config.** `config.reload()` re-reads all values; the Groq and Gemini clients are rebuilt
  on a key change, `LOG_LEVEL` applies at once, so no restart is needed except for items marked RESTART
  (mobile sync switch, Firebase URL and file, data file name, dev-tools). Log lines, errors and test
  results pass through a filter that replaces any key with `***`.
- **3C: API.** `WebAPI.settings_*` methods. The page only ever receives masked keys (`gsk_…1234`); the
  full key is returned only by Reveal, which is written to the history. Imports: `.env` text or file,
  one-click import of the old `.env` and `serviceAccountKey.json` (originals are left untouched),
  Firebase JSON check and copy, backup and restore of settings (no keys inside).
- **3D: SETTINGS tab.** Seventh tab with grouped rows, source badges, inline edit with validation,
  Show/Reveal, two-step delete, switches, filter, import panel, backup/restore, history, Open folder and
  a first-run offer to import the old files. Checked in four themes with headless Chromium.
- **3E: Test buttons.** Read-only checks for Groq (list models), Gemini (look up both models) and
  Firebase (file, URL, then a throwaway connection). They can test a key you typed but did not save yet.
**CORRECTION (added 2026-10-27, after the fact; the "COMPLETE" claim above is kept as written):** Phase 3 was
reported complete, but on the owner's real machine every tab and the Debug Log showed nothing; only the key fields
and Test buttons in SETTINGS worked. Cause: pywebview exposes every public attribute of the `js_api` object and
walks into nested objects. Phase 3 added a public `WebAPI.settings`; together with `store`, `assistant`, `officer`
and `screen` this exposed 744 functions (including keychain access) and the page could not talk to the backend.
The tests missed it because they call `WebAPI` directly, not through pywebview.
- **3G: Fix.** The six helper attributes (`store`, `assistant`, `officer`, `screen`, `settings`, `session_history`) are
  now private (leading underscore); only methods are public. A regression test,
  `test_only_the_api_methods_are_exposed_to_the_page`, fails if `WebAPI` ever gets a public non-method attribute, and the
  rule is in RULEBOOK section 7. After the fix: 145 passed, 1 skipped (backend). The owner confirmed on the real
  machine that the app works fully again; that confirmation is the only live check.
**Not verified (needs the owner's machine):** the real Windows Credential Manager or macOS Keychain, real
Groq, Gemini and Firebase answers from the Test buttons, native file dialogs and Open folder inside the
real pywebview window, and the look in WebView2 (only jsdom and Chromium were used). The old exposed keys
from before Phase 1 still need to be rotated; Phase 3 does not do that.
**Net result:** one place for keys and settings that survives upgrades, editable and testable from the GUI.
**Folded from (retired to PASSIVE_DOCS/retired_session_notes/):** SESSION_2026-10-27_1000_3A.md,
SESSION_2026-10-27_1010_3B.md, SESSION_2026-10-27_1020_3C.md, SESSION_2026-10-27_1030_3D.md,
SESSION_2026-10-27_1040_3E.md, SESSION_2026-10-27_0600_3G.md
