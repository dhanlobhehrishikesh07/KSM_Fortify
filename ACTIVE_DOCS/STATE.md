# Project state

**Last updated:** 2026-10-31 10:40
**Updated by:** Documentation session (Type 2): folded 5E

## Current phase
Phases 1 to 4 are complete. Phase 4 (Online sync with Firebase) passed the owner's real-world check (4G) on Windows.
Phase 5 (shared configuration in Firebase: prompts and all settings except the two API keys and four settings needed to reach
Firebase) is IN PROGRESS: 5A to 5E are built and tested with fakes. The AI reads its prompts through the prompt library, one live
listener feeds the prompt library and the shared settings, and the app now has a PROMPTS tab and, in SETTINGS, scope badges, a shared
status box and the new switches. The real window and real Firebase (`listen`, `transaction` with the server clock) and the rules
emulator are NOT yet proven. Next is 5F, the owner's real-world check. Phase 6 (web and Android app) is scoped and DEFERRED. Details: ROADMAP.md.
CHANGELOG keeps Phase 4 and Phase 5 in full; Phase 3 was retired on 2026-10-30 14:10.

## Last completed work
Documentation session (Type 2, 2026-10-31 10:40): 5E folded into CHANGELOG, ROADMAP and README. Last code: 5E (2026-10-31): `WebAPI.prompt_*`,
`config_status`, `get_theme`, PROMPTS tab, settings badges; 560 Python and 55 UI tests passed in a sandbox (5A: 346 and 32 on the owner's PC);
four themes checked in headless Chromium with a fake backend. Not run anywhere: live Firebase, the Firebase emulator, the real desktop window.

## Tier A notes not yet folded
None. (5E was folded on 2026-10-31 10:40; 5C and 5D on 2026-10-30 14:10; 5A and 5B on 2026-10-30 12:00; 4G-1 on 2026-10-28 01:00; 4E and 4F on
2026-10-28 00:30; 3G and 4A to 4D on 2026-10-27 19:00.)

## Next planned work
**5F** (the owner's real-world check, on the PC). Start `python main.py` and open the PROMPTS and SETTINGS tabs first (the tests cannot see a
pywebview exposure problem; RULEBOOK section 7). Then the ROADMAP 5F list: both switches on, see prompts and settings in the Firebase console
(this PC's values under `config/devices`); change the chat persona and ask a question; add an `extra` instruction and see it followed; try to
type the trigger phrase and see the save refused; change a model name in the console and see the app follow without a restart; change the
screen width and see it under your device; Wi-Fi off, edit, on, see it upload; confirm no API key appears anywhere in Firebase; adding a task
through chat still works; Reset to default. Anything that fails goes to the AI as a Type 1 task with the debug-log lines (starter prompt B2/B).
Before 5F: run `python -m pytest -q` and `cd tests/ui && npm install && npm test` on the PC (expected 560 and 55); replace
`REPLACE_WITH_OWNER_UID` in `database.rules.json` before publishing the rules; no phase has yet run the rules against the Firebase emulator.
After 5F a Type 2 session folds its result and closes Phase 5 (ROADMAP "completed"). Do not start Phase 6.
Open for the owner (ROADMAP Phase 5 "Open for the owner"): review of the locked/editable split; whether the API keys stay out of
Firebase (plan: yes); whether device names default to the computer's name (plan: yes).

## Known open issues
- NOT yet checked by hand (Phase 2 items): mouse drag in the real window, a real Groq chat that adds a task, a Gemini
  audit, a screen scan, Firebase with a real phone app
- NOT yet checked by hand (Phase 4C): the backup file in `data/backups/`; add and delete a task and see `ksm/tasks` follow; switch
  off Wi-Fi, add a task, switch on, see it arrive
- NOT yet checked by hand (Phase 4D): turn on "Keep chat history online", chat, see `ksm/chats`; restart and ask about something said
  before; press Clear context, restart, confirm it is forgotten; offline, chat, reconnect
- NOT yet checked by hand (Phase 4E): a real Gemini audit and a real screen scan appear under `ksm/audits` and `ksm/scans` (text only, no image)
- NOT recorded in 4G: whether the Firebase rules in the console are the shipped `database.rules.json` with the owner's user id
  (the owner saw they are locked, not open test mode); an emulator test is still not done (planned for 5A, not built; do it in 5F or earlier)
- NOT yet checked by hand (Phase 3 items): save a Groq key and Test; save a Gemini key and Test; import the old `.env`; import a Firebase
  file with the file picker; Open folder; the keychain on the owner's OS
- Phase 4 uploads personal data (chat, scans) to Google servers; the owner decides per switch, all off by default
- Old keys exposed before Phase 1 must be rotated at the providers (not done by any phase)
- Small items parked as ideas in ROADMAP

## Relevant Tier B docs for current phase
5F: ROADMAP.md "Phase 5" (5F entry, "What 5C and 5D built", "What 5E built", "How settings in Firebase work"), `CODE/firebase/DATA_LAYOUT.md`,
`CODE/firebase/database.rules.json`. Skip CHANGELOG unless auditing a Phase 3 to 5 claim.
