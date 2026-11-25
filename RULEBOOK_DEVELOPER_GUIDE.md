# KSM (Karauli Shankar Mahadev) Documentation & Upgrade Rulebook

**Entry point. Any developer or AI assistant reads this FIRST, before code or any other doc.**

Purpose: keep every session small, cheap, and context-efficient by strictly separating
"what to read" from "what to ignore", and by using a fixed procedure for new work.

## 1. Folder structure (do not deviate)
```
PROJECT/
├── RULEBOOK_DEVELOPER_GUIDE.md   <- this file
├── CODE/                         <- the codebase (code wins over any doc)
├── ACTIVE_DOCS/                  <- handed to every dev / AI session
│   ├── STATE.md                  <- current status, READ SECOND (max ~1 page)
│   ├── TIER_A_session_notes/     <- one short file per work session
│   └── TIER_B_structural/        <- README.md, CHANGELOG.md, ROADMAP.md
└── PASSIVE_DOCS/                 <- company only, NEVER shared or uploaded
    ├── ARCHIVE_OUTDATED_DO_NOT_USE/
    ├── retired_session_notes/    <- Tier A notes after being folded
    └── retired_changelog_phases/ <- old phase entries, superseded
```
**Hard rule:** PASSIVE_DOCS is never zipped, uploaded, or pasted into an AI session.
If it appears in your context, do not read it and tell the user.

## 2. Naming convention
Session notes: `SESSION_<YYYY-MM-DD>_<HHMM>_<PHASE>.md` (e.g. `SESSION_2026-09-03_1015_2A1.md`).
STATE.md and every Tier B doc start with `Last updated: YYYY-MM-DD HH:MM`.

## 3. Two session types: pick one before starting

### TYPE 1: Coding session
Reads ONLY: `STATE.md` + the Tier B file(s) STATE.md points to for this phase.
Must NOT read: old Tier A notes, full CHANGELOG history, anything in PASSIVE_DOCS.
Produces, in order:
1. The code change, tested
2. One new Tier A note (use TEMPLATE_SESSION_NOTE.md)
3. One new entry in STATE.md's "Tier A notes not yet folded" list
STOP. A coding session never edits README / CHANGELOG / ROADMAP.

### TYPE 2: Documentation session
Use when STATE.md lists un-folded Tier A notes.
Reads ONLY: `STATE.md`, the listed un-folded notes, the Tier B docs being updated.
Produces:
1. Updated README / CHANGELOG / ROADMAP in consolidated prose (not copy-paste)
2. Each folded note moved to `PASSIVE_DOCS/retired_session_notes/`
3. STATE.md updated: folded list cleared, timestamp bumped, phase/next-work updated
Never writes or edits code.

## 4. Upgrade procedure: starting a new phase or feature
1. Scope it in ROADMAP.md first (named phase entry before any code).
2. Break it into small sub-sessions up front (1A, 1B, 1C...), each fits one coding session.
   Write them as a checklist in ROADMAP.md. Each has an acceptance check.
3. Update STATE.md: name the new phase, list first sub-session as "next planned work".
4. Run Type 1 sessions in order. Do not start N+1 until N's acceptance check passes.
5. Run a Type 2 session periodically (in batches) to fold notes into CHANGELOG and tick ROADMAP.
6. On phase completion, closing Type 2 session writes one consolidated phase entry in CHANGELOG
   and moves the phase to "completed" in ROADMAP.
7. If an earlier "done" claim was wrong: never silently rewrite it. Add a labeled correction
   note stating what was wrong, what fixed it, and keep the original claim visible.

## 5. Maintenance checklist (run at the start of any Documentation session)
- [ ] STATE.md "not yet folded" list matches the real contents of TIER_A_session_notes/
- [ ] STATE.md still ~1 page; move detail into Tier B, leave a pointer
- [ ] CHANGELOG holds only current phase + previous phase in full; older -> retired_changelog_phases
- [ ] No Tier A notes older than the current phase left unfolded
- [ ] Nothing sensitive/company-only in ACTIVE_DOCS; no secrets (keys, tokens) in the zip
- [ ] Before handoff, zip only: RULEBOOK + CODE/ + ACTIVE_DOCS/ (never PASSIVE_DOCS)

## 6. What gets handed over
Always: RULEBOOK_DEVELOPER_GUIDE.md, CODE/, ACTIVE_DOCS/. Never: PASSIVE_DOCS/.
AI assistant's first three actions, in order: (1) read this rulebook, (2) read STATE.md,
(3) pick Type 1 or Type 2 from the human's request and follow only that section.
Do not read every Tier A note "just to be safe".

## 7. Project-specific rules (KSM)
- Run: `cd CODE && python main.py`. Tests (run both before finishing any coding session):
  `cd CODE && python -m pytest -q` and `cd CODE/tests/ui && npm install && npm test` (needs Node.js).
- The app is a pywebview desktop GUI plus live Groq/Gemini/Firebase calls. An AI session without a
  desktop or API keys can verify backend modules and parsers by tests only. Say so in the Tier A
  note instead of claiming GUI or live-API checks passed.
- Never create or commit `CODE/.env` or `serviceAccountKey.json`. Only `CODE/.env.example` is shipped.
- Keys and settings live in the per-user config folder and OS keychain (`app/settings_store.py`), not in the
  project. Tests must never touch them: `tests/conftest.py` sets `KSM_CONFIG_DIR` to a temp folder and
  `KSM_DISABLE_KEYRING=1` before `app` is imported. Keep it that way for every new test file.
- A key must never appear in a log line, error text, UI answer or Tier A note. Send secrets to the page only
  through the masked values from `SettingsStore.describe()`.
- `CODE/data/matrix_data_pro.json` is the owner's real task data. Back it up before any change that
  touches `data_store.py`, and never commit test data into it (tests use a temp file).
- `TASK_TRIGGER_PHRASE = "task addtion initiated"` is misspelled on purpose. It must match the
  prompt files exactly. Do not "fix" it in one place only.
- `WebAPI` (`app/gui/web_api.py`) must have only public METHODS. pywebview exposes every public attribute of
  the `js_api` object and walks into it, so a public `settings`, `store` and so on once exposed hundreds of
  functions (including keychain access) and left every tab empty. Keep helper objects private (leading
  underscore). `test_only_the_api_methods_are_exposed_to_the_page` enforces this. Tests that call `WebAPI`
  directly cannot see this class of problem, so the owner's real-window check still matters.
