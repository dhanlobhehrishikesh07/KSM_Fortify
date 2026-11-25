# Firebase data layout and rules (Phase 4)

This folder holds files for the owner's own Firebase project. Nothing here is a secret. The service account
key is never stored in the project; it lives in the config folder (SETTINGS tab, Mobile sync).

## Where each kind of data goes
All objects are keyed by id (Realtime Database handles arrays badly). Times are the app's local
`YYYY-MM-DD HH:MM:SS` text, plus `updated_at_ms` (milliseconds since 1970) where order matters.

| Path | What | Written by | Switch |
|---|---|---|---|
| `ksm/tasks/<id>` | one task: `title, imp, urg, created_at, expected_at, history, updated_at_ms, deleted` | desktop only | Keep tasks online |
| `ksm/chats/<session_id>/<msg_id>` | `role` (`user`, `assistant`, or `marker` = "context cleared"), `text`, `time` (keys removed from the text first; one session per app run or per Clear context; message ids `m000000`, `m000001`, ...) | desktop | Keep chat history online |
| `ksm/audits/<time_key>` | `question, answer, time` (the audit text is in `answer`) | desktop | Keep audits and screen scans online |
| `ksm/scans/<time_key>` | `question, answer, monitor, model, time`; never the image | desktop | Keep audits and screen scans online |
| `ksm/meta` | schema version, last upload time | desktop | any switch |
| `incoming_tasks/<push_id>` | text from the phone (already in use today) | phone | Enable mobile sync |
| `ai_responses` | latest AI reply `{text, timestamp}` (already in use today) | desktop | Enable mobile sync |

A deleted task stays as `deleted: true` with a new `updated_at_ms`, so the delete reaches every copy.

Never uploaded: API keys, the service account file, the debug log, screenshots, the time-warp offset.

## The rules file
`database.rules.json` denies everyone except one Firebase Authentication user (the owner). The desktop app
uses the service account, which is not limited by rules, so these rules only matter for the phone or web
app (Phase 5).

To use it, later:
1. Phase 5A adds Google sign-in. After you sign in once, open Firebase console, Authentication, Users, and
   copy your **User UID**.
2. In `database.rules.json` replace every `REPLACE_WITH_OWNER_UID` with it (the file keeps the placeholder
   so no real id is ever shipped).
3. Firebase console, Realtime Database, Rules tab: paste the file, then Publish.

Careful: publishing rules blocks any client that is not signed in as the owner. If your current phone app
reads or writes without signing in, it will stop working until it signs in (Phase 5 builds that).

## Do this now (takes one minute)
In Firebase console, Realtime Database, Rules tab, check what you have. If it says `".read": true` or
`".write": true` (test mode), your data is open to anyone who finds the URL. Replace it with the locked
rules below and publish. The desktop app keeps working, because it signs in with the service account.

    { "rules": { ".read": false, ".write": false } }


---

# Shared configuration layout (Phase 5) - PARTLY BUILT (5A, 5B done; nothing reads it from Firebase yet)
Target for Phase 5 (ROADMAP "Phase 5"). The code today reads prompts only from `prompts/*.txt` (and two hard-coded
texts) and keeps all settings local. Code wins over this section. Built so far: the block templates and the scopes (5A), the rules for `ksm/config` and `ksm/config_versions` (5A, not yet run against the emulator), the prompt library `app/prompt_store.py` (5B). Shared settings (5C), the live listener and the consumers (5D) and the screens (5E) are not built. Tasks, chats,
audits and scans keep the layout above (schema 1). Owner decision: all settings are in Firebase, except the two API keys and
four settings needed to reach Firebase (below). Switches "Keep prompts online" (`SYNC_PROMPTS`, common) and "Keep
settings online" (`SYNC_SETTINGS`, local only, the gate) are ON by default. With no Firebase configured they do nothing.

Everything configurable sits under ONE parent, `ksm/config`, so one live listener carries all of it. Old prompt versions sit
under a separate parent, `ksm/config_versions`, which that listener does not download.

## Paths (under `ksm/`)
| Path | Content | Written by |
|---|---|---|
| `config/settings/<KEY>` | COMMON settings: `value, updated_at, updated_by`; only the COMMON keys listed below | any client (owner) |
| `config/devices/<deviceId>` | `name, kind, last_seen`: one entry per installation | that device |
| `config/devices/<deviceId>/settings/<KEY>` | PER-DEVICE settings of that device: `value, updated_at, updated_by`; only the PER-DEVICE keys below | that device, or any client (owner) |
| `config/prompts/<promptId>` | `name, description, role, builtin, template_version, version, updated_at, updated_by` | any client (owner) |
| `config/prompts/<promptId>/blocks/<blockId>` | `text, enabled`; custom blocks also `title, order` | any client (owner) |
| `config/prompt_active/<role>` | the `promptId` that role uses | any client (owner) |
| `config_versions/prompts/<promptId>/<n>` | `blocks` (copy of the editable blocks), `saved_at, saved_by, note`; newest `PROMPT_VERSIONS_KEEP` (30) kept | any client (owner) |

`deviceId` is a random id made the first time an installation starts and kept in `device.json` in the config folder, with a
friendly `name` (default: the computer's name; it can be changed). `role` is `chat`, `task_extraction`, `audit` or
`screen_scan`. Built-in prompt ids: `chat-default`, `task-extraction-default`, `audit-default`, `screen-scan-default`
(`builtin: true`). A custom prompt id and a custom block id are Firebase push keys (custom block ids start with `x-`).
`updated_at` is the server clock (`{".sv": "timestamp"}`); `version` rises by one per save. Example:

    ksm/config/settings/USER_NAME: { value: "User", updated_at: 1793180400000, updated_by: "desktop" }
    ksm/config/devices/d-4f9a1c: { name: "This PC", kind: "desktop", last_seen: 1793180400000,
        settings: { SCREEN_MAX_WIDTH: { value: "1920", updated_at: 1793180400000, updated_by: "desktop" } } }
    ksm/config/prompts/chat-default: { name: "Chat (default)", role: "chat", builtin: true, template_version: 1, version: 3,
        updated_at: 1793180400000, updated_by: "desktop",
        blocks: { identity:  { text: "[IDENTITY]\nRole: {ASSISTANT_NAME}, a friendly and grounded AI assistant.\n...", enabled: true },
                  behaviour: { text: "[BEHAVIOR]\n1. Act as a normal, conversational chatbot. ...", enabled: true },
                  extra:     { text: "Answer in short messages. Use simple English.", enabled: true } } }
    ksm/config/prompt_active: { chat: "chat-default", task_extraction: "task-extraction-default",
        audit: "audit-default", screen_scan: "screen-scan-default" }

## Settings: scope of every setting (the field `scope` on `SettingDef`)
| Setting | Scope | Why |
|---|---|---|
| `ASSISTANT_NAME`, `USER_NAME`, `USER_PROFILE` | COMMON | same persona on every client |
| `GROQ_MODEL_ID`, `GEMINI_MODEL_ID`, `SCREEN_MODEL_ID` | COMMON | same models on every client |
| `SYNC_TASKS`, `SYNC_CHAT`, `SYNC_AUDITS_SCANS`, `SYNC_PROMPTS` (new) | COMMON | one behaviour everywhere (turning one off turns it off for all) |
| `PROMPT_VERSIONS_KEEP` (new, int, 30, 5 to 200) | COMMON | one pruning rule for all clients |
| `SCREEN_MAX_WIDTH`, `SCREEN_JPEG_QUALITY`, `SCREEN_HIDE_DELAY` | PER-DEVICE | depend on that PC's screen |
| `DATA_FILE_NAME`, `LOG_LEVEL`, `DEBUG_UI`, `THEME` (new) | PER-DEVICE | local file, log detail and display of that device |
| `ENABLE_FIREBASE`, `FIREBASE_DATABASE_URL`, `FIREBASE_SERVICE_ACCOUNT_PATH` | LOCAL ONLY | needed before Firebase can be reached |
| `SYNC_SETTINGS` (new, default on) | LOCAL ONLY | the gate for this layer; it cannot be stored inside what it controls |
| the device id (`device.json`) | LOCAL ONLY | identifies this installation |
| `GROQ_API_KEY`, `GEMINI_API_KEY` | SECRET | never in Firebase (ROADMAP Phase 5, open question 2) |
The rules file lists the COMMON keys under `config/settings` and the PER-DEVICE keys under `config/devices/$deviceId/settings`
by name, so no other key (a LOCAL ONLY setting or an API key) can be stored there.

## Placeholders
`{USER_NAME}`, `{ASSISTANT_NAME}`, `{USER_PROFILE}` (from the settings) and `{TRIGGER_PHRASE}` (the code constant
`TASK_TRIGGER_PHRASE`, today `task addtion initiated`, spelled that way on purpose) are filled in by the program.
`{FULL_SESSION_CONTEXT}` is left as written. Any other `{TOKEN}` is a warning.

## Default blocks: lock the wire, edit the rest
LOCKED = the program searches for it, the parser reads it, or it describes the data the program sends. Everything else is
EDITABLE. The order below is the order in the prompt; "insert point" = the `extra` block and custom blocks, sorted by
`order`. The shipped wording is today's wording; blocks only cut the existing text, and editable shipped text writes the
trigger phrase as `{TRIGGER_PHRASE}`, so the assembled default equals today's prompt.

**chat** (`general_helper.txt`; about 70% editable)
| Block | Locked? | Holds |
|---|---|---|
| `identity` | editable | `[IDENTITY]`: role, name, user and profile lines |
| `behaviour` | editable | `[BEHAVIOR]` item 1: act as a normal conversational chatbot |
| `duplicate_check` | editable | item 2: review the matrix and history first, do not trigger for a task that already exists, how to acknowledge it |
| `trigger_rule` | LOCKED | item 3: use the exact phrase only for a brand-new goal, once per task (the program looks for the phrase in the reply) |
| `scale` | editable | item 4: the 1-to-3 wording used when suggesting (only framing; the parser clamps values itself) |
| insert point | editable | `extra` and custom blocks |
| `confirmation` | LOCKED | item 5: a friendly confirmation first, then the phrase |

**task_extraction** (`task_translation_prompt.txt`; about 54% editable, most of the rest is the output format)
| Block | Locked? | Holds |
|---|---|---|
| `identity` | editable | `[IDENTITY]`: user, profile, "Strategic Chief of Staff" |
| `input_context` | LOCKED | `[INPUT CONTEXT]`: names the blocks the code sends (`[EXISTING MATRIX TASKS]`, `{FULL_SESSION_CONTEXT}`) |
| `dedup_rules` | editable | `[MISSION]` item 1 up to "you MUST ABORT": how similar counts as a duplicate |
| `dedup_reply` | LOCKED | "If aborting, reply ONLY with: DUPLICATE_DETECTED: ..." (`parse_tasks` drops any text that contains it) |
| `translation` | editable | item 2: translate to plain, modern English |
| `analysis` | editable | item 3: judge over-optimism from past `[AUTO]` migrations |
| `categorization` | LOCKED | item 4: importance and urgency as whole numbers only |
| `importance_rules` | editable | item 5: what counts as 3, 2, 1 for importance (the owner's own criteria) |
| `urgency_rules` | editable | item 6: what counts as 3, 2, 1 for urgency (time windows) |
| insert point | editable | `extra` and custom blocks |
| `output_format` | LOCKED | `[STRICT OUTPUT FORMAT]`: the exact `task :- ...` lines the parser reads |

**audit** (new file `strategy_audit.txt`, today `ANALYSIS_PROMPT` in `strategy_officer.py`; about 85% editable)
| Block | Locked? | Holds |
|---|---|---|
| `identity` | editable | "You are the 'Chief Strategy Officer AI'." |
| `data_description` | LOCKED | the JSON is the 3x3 matrix, importance and urgency 1 to 3 |
| `mission` | editable | danger zones, growth tasks, daily battle plan (how many tasks), procrastination call-out |
| `tone` | editable | "Be blunt, professional, and highly motivational." |
| insert point | editable | `extra` and custom blocks |
The code appends `HERE IS MY CURRENT MATRIX DATA:` and the tasks after the prompt; that is not part of the prompt.

**screen_scan** (`screen_analyzer.txt`; 100% editable, the answer is shown as plain text and not parsed)
| Block | Locked? | Holds |
|---|---|---|
| `role` | editable | "You are a precise screen-reading assistant..." |
| `job` | editable | "YOUR JOB" items 1 to 4 |
| `rules` | editable | "RULES": only report what is visible, hide secrets, layout, plain text only |
| insert point | editable | `extra` and custom blocks |
Recommended (warning, not a block): keep the line that hides passwords and keys, and the "plain text only" rule.

## Rules every client must follow
1. **Never store or send locked blocks, LOCAL ONLY settings or secrets.** Only editable blocks and the COMMON and
   PER-DEVICE settings are in Firebase. A block with the id of a locked block, and a setting key that is not on the list
   of its scope, are ignored on read.
2. **Guard rules for editable text:** no literal `task addtion initiated` (use `{TRIGGER_PHRASE}`); for `task_extraction`
   also no `DUPLICATE_DETECTED` and no output label lines; at most 4000 characters per block and 20000 in total; no key
   text. Unknown `{TOKEN}`s are a warning. A prompt over about 6000 characters gets a soft warning.
3. **Assembly** (done by the desktop; another client that builds a prompt must do the same): walk the template in order;
   locked block = shipped text; editable block = Firebase text if it is on and valid, else the last good local copy, else
   the shipped text, skipped if switched off; at the insert point add the enabled `extra` and custom blocks sorted by
   `order`; join with a blank line; fill the placeholders. Then run the self-check: `chat` must contain the literal
   trigger phrase; `task_extraction` must contain `DUPLICATE_DETECTED` and the five labels the parser reads
   (`task :-`, `importance :-`, `urgency :-`, `expected completion date :-`, `expected completion time :-`). If it fails,
   use the shipped default for that role and warn.
4. **Save a prompt = new version.** Read `version`; write the changed blocks and `version + 1` in one transaction that
   checks `version` is still the one you read (the history entry `config_versions/prompts/<promptId>/<n>` is written just after it; a failed
   history write stays pending and is retried), otherwise stop and reload (offline: keep the copy, queue it, and on a
   conflict offer "keep mine" or "take theirs"). Then delete versions beyond the newest `PROMPT_VERSIONS_KEEP`. Several offline saves of one prompt upload as one version.
5. **Save a setting.** Write `value` and `updated_at` in a transaction that checks `updated_at` is the one you last saw;
   on a conflict the other value wins and the user is told. An environment variable is never uploaded.
6. **Delete and reset.** A built-in prompt is never removed: "reset block" and "reset prompt" write the shipped text back (each is a version), so the console still shows it; the shipped text
   takes over at use. A custom prompt may be deleted only if no `prompt_active` entry
   points to it.
7. **Seed.** When a switch is on and Firebase is reachable, write each shipped editable block, each setting of this device (COMMON and its own PER-DEVICE ones) and
   each `prompt_active` entry only where it does not exist. Never overwrite. When Firebase already has a different value
   for a setting, Firebase wins.
8. **After a program upgrade** (`template_version` changes): new blocks use their shipped text, removed blocks are ignored.
9. **Speed.** Keep an in-memory copy and the local cache files; never wait for the network in an AI call or at start-up;
   use one listener on `ksm/config` (first the whole snapshot, then changes).
10. **Another client or an API** uses the same paths with the owner's sign-in; the rules allow only the owner. The desktop
    checks the guard rules and the self-check again when it uses a prompt.

# Phase 6 layout (v2) - PLANNED, DEFERRED, NOT IMPLEMENTED
Everything above describes what the code does today (schema 1). This section is the target for Phase 6 (see ROADMAP
"Phase 6"). Code wins over this section until sub-session 6A/6B marks a part as built. Owner decision 2026-10-28:
Firebase is the only source of truth; the desktop, the website and the Android app all read and write directly.

## Paths (all under `ksm/`, all keyed by id, no arrays)
| Path | Content | Written by |
|---|---|---|
| `tasks/<taskId>` | `title, imp, urg, created_at, expected_at, deleted?, updated_at` | every client |
| `tasks/<taskId>/history/<entryKey>` | `{at_ms, line}`; `line` is the text used today, e.g. `[2026-03-16 17:32:12] [AUTO] Created.` | every client |
| `chats/<session>/<msg>` | `role, text, time, device`; session id is `<time>-<device>` | every client |
| `audits/<time_key>` | `question, answer, time, device` | every client |
| `scans/<time_key>` | `question, answer, monitor, model, time, device` (desktop only, never the image) | desktop |
| `config/...` | prompts and settings: defined in "Shared configuration layout" (Phase 5); unchanged by Phase 6 | - |
| `meta` | `schema` (2 after migration), `migrated_at`, `migrated_by`, `last_upload_ms` | desktop (migration) |

Never in Firebase: API keys, the service account file, the debug log, screenshots, the time-warp offset, and the LOCAL ONLY
settings (Firebase on/off, database URL, service account path, `SYNC_SETTINGS`, the device id). All other settings follow the
Shared configuration layout (Phase 5).

## Rules every client must follow
1. **Ids.** New task: `push()` key. Existing tasks keep "101" to "114" as text keys. Never compute `max(id)+1`.
2. **Write only what changed.** Use a multi-path `update` for the fields touched; never `set` a whole existing task.
3. **imp and urg only go up.** Move = transaction storing `max(current, wanted)`. The app rule "one importance step at a
   time" stays in the client; the rules file only enforces "never lower".
4. **History.** Add one entry per event under `history/<entryKey>`. Normal key: `h<13-digit ms>-<4 random chars>`.
   Automatic escalation: `auto-24h` or `auto-1w` (fixed, so two writers leave one entry). Migrated old lines:
   `L000`, `L001`, ... in their old order. Display sorted by `at_ms`.
5. **Delete** = `deleted: true` plus `updated_at`. Never remove the node. Clients hide deleted tasks.
6. **Time.** `updated_at` is `{".sv": "timestamp"}`. Other stored date-times are the owner's wall-clock time (India
   Standard Time, to be confirmed) in `YYYY-MM-DD HH:MM:SS`; read them as that zone on every device.
7. **Auto-escalation** is computed from `expected_at` and real time: 24 hours or less left means urgency 3; 7 days or
   less means urgency at least 2. Clients show the computed value and save it by rule 3 plus the fixed history key.
   Nothing is saved while the desktop time-warp is not zero.
8. **Local copy = cache.** After schema 2, a client never uploads its whole cached list. It only sends operations.
   A task missing from Firebase stays missing (a stale cache must not bring back a deleted task).
9. **Offline queue.** Operations that could not be sent wait on the device and replay on reconnect.

## Task as stored (example)
    ksm/tasks/-OabcXYZ123: { title: "Submit lab report", imp: 3, urg: 2,
        created_at: "2026-10-28 11:20:00", expected_at: "2026-11-02 23:59:59", updated_at: 1793180400000,
        history: { "h1793180400000-k3f9": { at_ms: 1793180400000, line: "[2026-10-28 11:20:00] [WEB] Created." } } }

## Where the keys live (not in Firebase)
Desktop: OS keychain. Android: device secure storage. Website: browser storage on that device. Entered once per device.

## Migration (6B-3, desktop only, once)
Backup `matrix_data_pro.json` first; convert local tasks and Phase 4 cloud records (`updated_at_ms`, history array) to
this layout; set `meta.schema = 2` last. Running it again must change nothing.
