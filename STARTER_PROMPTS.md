# Starter prompts for KSM (paste at the top of a new AI session; attach only RULEBOOK + CODE + ACTIVE_DOCS)

## A. First-time orientation (any session)
Read RULEBOOK_DEVELOPER_GUIDE.md, then ACTIVE_DOCS/STATE.md. Do not read anything else yet.
Tell me in 5 lines: current phase, next planned work, and whether this is a Type 1 or Type 2 session.

## B. Type 1: Coding session
Follow RULEBOOK Section 3, Type 1 and Section 7. Task: [sub-session code from ROADMAP + one-line goal].
Read only STATE.md and the ROADMAP entry for that sub-session. Implement, test (show the new test
failing on old code if this is a bug fix; run both test suites from Section 7), then write one Tier A
note from TEMPLATE_SESSION_NOTE.md and add one line to STATE.md's un-folded list. Do not edit
README/CHANGELOG/ROADMAP. Do not claim GUI, live-API or phone checks you could not run.

## B2. Manual check session (owner on a desktop)
Run `python main.py` and report, one line each: drag a card to a higher cell; add and delete a task;
chat a new goal and see it added; run a strategy audit; scan the screen; (if Firebase is on) send a phone
message and see the reply. Anything that fails goes to the AI as a Type 1 task with the debug-log lines.

## C. Type 2: Documentation session
Follow RULEBOOK Section 3, Type 2. Fold the un-folded Tier A notes listed in STATE.md into
CHANGELOG/ROADMAP/README as consolidated prose, move the notes to PASSIVE_DOCS/retired_session_notes/
(give me the list of moves), tick finished sub-sessions in ROADMAP, and update STATE.md. Do not touch code.

## D. Scope a new phase
Follow RULEBOOK Section 4, steps 1-3. Goal: [goal]. Add a named phase to ROADMAP.md, split it into
small sub-sessions each with files touched, dependencies, and an acceptance check. Update STATE.md.
Do not write code.

## E. Audit before trusting "done"
Verify the claims in CHANGELOG for Phase [N] against the actual code (not comments). List any gap as a
correction note per RULEBOOK Section 4, step 7. Do not rewrite the original claim.
