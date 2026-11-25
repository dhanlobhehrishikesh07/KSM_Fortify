// Headless UI checks for app/gui/webui (jsdom + a fake pywebview API).
// Run:  cd tests/ui && npm install && npm test
// Optional: KSM_WEBUI=/path/to/webui node ui_smoke.test.js   (to test another copy of the UI)
"use strict";
process.env.TZ = "Asia/Kolkata"; // the owner's timezone: UTC+5:30, where a UTC date is often "yesterday"

const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const WEBUI = process.env.KSM_WEBUI || path.resolve(__dirname, "../../app/gui/webui");
const HTML = fs.readFileSync(path.join(WEBUI, "index.html"), "utf8").replace(/<script src="app.js"><\/script>/, "");
const JS = fs.readFileSync(path.join(WEBUI, "app.js"), "utf8");

// 2026-10-26 00:30 in India = 2026-10-25 19:00 UTC
const FIXED_NOW = Date.UTC(2026, 9, 25, 19, 0, 0);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pad = (n) => String(n).padStart(2, "0");
function fmt(d) { // local "YYYY-MM-DD HH:MM:SS" like the Python backend
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
    pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

function settingsFixture() {
  const ent = (key, label, group, kind, extra) => Object.assign({
    key, label, group, kind, value: "", shown: "", is_set: false, source: "default", stored: false, default: "",
    help: "", restart: false, choices: [], link: "", service: "", placeholder: "", conflict: "", scope: "common",
  }, extra || {});
  return {
    groups: [
      { id: "identity", title: "Identity", blurb: "Names." },
      { id: "groq", title: "Groq (AI chat)", blurb: "Chat." },
      { id: "firebase", title: "Mobile sync (Firebase)", blurb: "Phone." },
      { id: "screen", title: "Screen scan tuning", blurb: "Size." },
    ],
    entries: [
      ent("USER_NAME", "Your name", "identity", "text", { value: "T", is_set: true, source: "store", stored: true }),
      ent("GROQ_API_KEY", "Groq API key", "groq", "secret", { service: "groq", is_set: true, source: "store", stored: true, shown: "gsk_…1234", scope: "secret" }),
      ent("GROQ_MODEL_ID", "Groq model", "groq", "text", { service: "groq", value: "llama-3.3-70b-versatile", is_set: true }),
      ent("ENABLE_FIREBASE", "Enable mobile sync", "firebase", "bool", { value: "false", is_set: true, restart: true, service: "firebase", scope: "local_only" }),
      ent("FIREBASE_SERVICE_ACCOUNT_PATH", "Service account file", "firebase", "path", { service: "firebase", value: "serviceAccountKey.json", is_set: true, restart: true, scope: "local_only" }),
      ent("SCREEN_MAX_WIDTH", "Max screenshot width (px)", "screen", "int", { value: "1920", is_set: true, default: "1920", scope: "per_device" }),
    ],
    folder: "/home/t/.config/ksm", backend: "OS keychain (Fake)", backend_secure: true, load_problem: "",
    legacy: { dotenv_found: true, dotenv_keys: ["GEMINI_API_KEY"], firebase_file: "/p/serviceAccountKey.json", dismissed: false },
    firebase_file: null, restart_pending: [],
    shared: { ok: true, hub: {}, settings: { state: "synced", pending: 0 }, prompts: { state: "synced", pending: 0 }, notices: [],
      overridden: [], device: { device_id: "d-0001abcd", name: "TestPC" } },
  };
}

// ---- Phase 5E: fake prompt API
function promptFakes(state) {
  const ok = (v) => Promise.resolve(v);
  const pr = state.pr = {
    role_active: { chat: "p-chat", task_extraction: "p-ext", audit: "p-aud", screen_scan: "p-scr" },
    prompts: [
      { id: "p-chat", name: "Chat (built-in)", description: "", role: "chat", builtin: true, version: 3, active: true, modified: false, pending: false, conflict: false },
      { id: "p-casual", name: "Casual chat", description: "", role: "chat", builtin: false, version: 1, active: false, modified: true, pending: false, conflict: false },
      { id: "p-ext", name: "Extraction (built-in)", description: "", role: "task_extraction", builtin: true, version: 0, active: true, modified: false, pending: false, conflict: false },
      { id: "p-aud", name: "Audit (built-in)", description: "", role: "audit", builtin: true, version: 0, active: true, modified: false, pending: false, conflict: false },
      { id: "p-scr", name: "Screen scan (built-in)", description: "", role: "screen_scan", builtin: true, version: 0, active: true, modified: false, pending: false, conflict: false },
    ],
    status: { state: "synced", pending: 0, warnings: [] }, notes: [], conflict: false,
    calls: { save: [], saveAs: [], active: [], del: [], reset: [], resetBlock: [], restore: [], validate: [], preview: [], history: 0, resolve: [], getAll: 0 },
    previewAnswer: null, failAll: false, saveAnswer: null,
  };
  const guard = (fn) => (...a) => (pr.failAll ? Promise.reject(new Error("boom")) : fn(...a));
  const blocks = () => [
    { id: "identity", title: "Identity", kind: "layout", locked: false, text: "You are KSM.", enabled: true, default_text: "You are KSM.", modified: false },
    { id: "trigger_rule", title: "Trigger rule", kind: "locked", locked: true, text: "Use the phrase only for a NEW goal.", enabled: true, default_text: "", modified: false },
    { id: "extra", title: "Extra instructions", kind: "extra", locked: false, text: "", enabled: true, default_text: "", modified: false, order: 0 },
  ];
  return {
    prompts_get: guard(() => { pr.calls.getAll++; return ok({ ok: true, roles: [
      { role: "chat", label: "AI chat", active_id: pr.role_active.chat, notes: pr.notes },
      { role: "task_extraction", label: "Task extraction", active_id: pr.role_active.task_extraction, notes: [] },
      { role: "audit", label: "Strategy audit", active_id: pr.role_active.audit, notes: [] },
      { role: "screen_scan", label: "Screen scan", active_id: pr.role_active.screen_scan, notes: [] }],
      prompts: pr.prompts.map((p) => Object.assign({}, p, { active: pr.role_active[p.role] === p.id })), status: pr.status, online: true,
      limits: { block: 4000, total: 20000, soft: 6000 } }); }),
    prompt_get: guard((id) => { const p = pr.prompts.find((x) => x.id === id); if (!p) return ok({ ok: false, message: "No such prompt." });
      return ok({ ok: true, id, name: p.name, description: "", role: p.role, builtin: p.builtin, version: p.version, active: pr.role_active[p.role] === id,
        pending: false, conflict: pr.conflict, blocks: blocks(), warnings: [] }); }),
    prompt_validate: guard((id, ch) => { pr.calls.validate.push([id, JSON.parse(ch)]);
      const bad = JSON.stringify(JSON.parse(ch)).includes("task addtion initiated");
      return ok(bad ? { ok: false, errors: ["It may not contain the trigger phrase."], warnings: [] } : { ok: true, errors: [], warnings: JSON.stringify(ch).length > 5000 ? ["Long."] : [] }); }),
    prompt_preview: guard((id, ch) => { pr.calls.preview.push([id, JSON.parse(ch)]);
      return ok(pr.previewAnswer || { ok: true, text: "FINAL <b>TEXT</b> for T", chars: 23, tokens: 6, warnings: [], errors: [] }); }),
    prompt_save: guard((id, ch, base, note) => { pr.calls.save.push([id, JSON.parse(ch), base, note]);
      return ok(pr.saveAnswer || { ok: true, version: base + 1, queued: false, warnings: [] }); }),
    prompt_save_as_new: guard((role, name, ch, desc, from) => { pr.calls.saveAs.push([role, name, JSON.parse(ch), from]);
      pr.prompts.push({ id: "p-new", name, description: "", role, builtin: false, version: 1, active: false, modified: true, pending: false, conflict: false });
      return ok({ ok: true, prompt_id: "p-new", version: 1, warnings: [] }); }),
    prompt_set_active: guard((role, id) => { pr.calls.active.push([role, id]); pr.role_active[role] = id; return ok({ ok: true }); }),
    prompt_delete: guard((id) => { pr.calls.del.push(id); pr.prompts = pr.prompts.filter((p) => p.id !== id); return ok({ ok: true }); }),
    prompt_reset: guard((id) => { pr.calls.reset.push(id); return ok({ ok: true }); }),
    prompt_reset_block: guard((id, b) => { pr.calls.resetBlock.push([id, b]); return ok({ ok: true }); }),
    prompt_restore: guard((id, n) => { pr.calls.restore.push([id, n]); return ok({ ok: true, version: 9 }); }),
    prompt_history: guard(() => { pr.calls.history++; return ok({ ok: true, versions: [
      { n: 3, saved_at: 1800000000000, saved_by: "desktop:a", note: "tone", chars: 120 }, { n: 2, saved_at: 1799990000000, saved_by: "desktop:a", note: "", chars: 100 }] }); }),
    prompt_resolve_conflict: guard((id, keep) => { pr.calls.resolve.push([id, keep]); pr.conflict = false; return ok({ ok: true }); }),
    prompt_new_block_id: guard(() => ok({ ok: true, id: "c-new1" })),
    config_dismiss_notices: guard(() => { state.settings.shared.notices = []; return ok({ ok: true, dismissed: 1 }); }),
    get_theme: guard(() => ok({ ok: true, theme: state.theme || "scifi" })),
  };
}

function makeEnv(opts) {
  opts = opts || {};
  const state = {
    theme: opts.theme,
    now: new Date(FIXED_NOW),
    tasks: [
      { id: 101, title: "Maths study plan", imp: 3, urg: 3, expected_at: fmt(new Date(FIXED_NOW + 5 * 3600e3)), history: ["[x] [AUTO] Created."] },
      { id: 102, title: "Gym", imp: 2, urg: 1, expected_at: fmt(new Date(FIXED_NOW + 9 * 86400e3)), history: ["[x] [MANUAL] Created."] },
    ],
    warning: "",
    sync: { ok: true, state: "off", label: "Off", pending: 0, last_sync: "", last_error: "", kinds: {} },
    syncFails: false,
    syncNowAnswer: { ok: true, state: "pending", label: "Pending 2", pending: 2, last_sync: "", last_error: "", kinds: {}, started: ["tasks", "chats"] },
    syncNowFails: false,
    exportAnswer: { ok: true, path: "C:\\Users\\t\\ksm\\exports\\ksm-cloud-export-1.json", counts: { tasks: 2, chats: 5 } },
    exportFails: false,
    calls: { syncNow: 0, exportCloud: 0, syncStatus: 0, migrate: [], snapshot: 0, logClient: [], set: [], del: [], test: [], importLegacy: 0, dismiss: 0 },
    settings: settingsFixture(),
    testResult: { service: "groq", status: "pass", message: "Key works. Model 'm' is available.", detail: "", ms: 321 },
    testRunning: false,
  };
  const ok = (v) => Promise.resolve(JSON.stringify(v));
  const api = {
    get_snapshot() {
      state.calls.snapshot++;
      state.now = new Date(state.now.getTime() + 1000); // the clock moves every poll, like the real app
      return ok({ now: fmt(state.now), tasks: state.tasks, assistant_name: "KSM", user_name: "T", data_warning: state.warning });
    },
    get_sync_status() {
      state.calls.syncStatus++;
      if (state.syncFails) return Promise.reject(new Error("boom"));
      return Promise.resolve(state.sync);
    },
    sync_now() {
      state.calls.syncNow++;
      if (state.syncNowFails) return Promise.reject(new Error("boom"));
      return Promise.resolve(state.syncNowAnswer);
    },
    export_cloud_data() {
      state.calls.exportCloud++;
      if (state.exportFails) return Promise.reject(new Error("boom"));
      return Promise.resolve(state.exportAnswer);
    },
    get_chat_updates() { return ok({ messages: [], count: 0, busy: false }); },
    get_audit_status() { return ok({ running: false, text: "", available: true }); },
    get_bridge_log() { return ok({ entries: [], count: 0, enabled: false }); },
    get_screen_status() { return ok({ running: false, available: true, problem: "", fresh: false, scan_id: 0 }); },
    get_debug_log() { return ok({ entries: [], count: 0, file: "x" }); },
    log_client(level, msg) { state.calls.logClient.push([level, msg]); },
    migrate_task(id, imp, urg) { state.calls.migrate.push([String(id), imp, urg]); return Promise.resolve({ ok: true }); },
    add_task() { return Promise.resolve({ ok: true }); },
    delete_task(id) { state.tasks = state.tasks.filter((x) => x.id !== id); return Promise.resolve({ ok: true }); },
    apply_warp() { return Promise.resolve({ ok: true }); },
    start_audit() { return Promise.resolve({ ok: true }); },
    start_screen_scan() { return Promise.resolve({ ok: true }); },
    send_chat() { return Promise.resolve({ ok: true }); },
    clear_chat() { return Promise.resolve({ ok: true }); },
    clear_debug_log() { return Promise.resolve({ ok: true }); },
    // ---- settings tab fakes
    settings_get() { return ok(state.settings); },
    settings_set(key, value) {
      state.calls.set.push([key, value]);
      if (key === "SCREEN_MAX_WIDTH" && Number(value) > 7680) return Promise.resolve({ ok: false, error: "Must be at most 7680." });
      const en = state.settings.entries.find((x) => x.key === key);
      if (en) { en.is_set = true; en.stored = true; en.source = "store"; en.shown = en.kind === "secret" ? "gsk_…WXYZ" : value; if (en.kind !== "secret") en.value = value; }
      return Promise.resolve({ ok: true, warning: "", restart: false, conflict: "" });
    },
    settings_delete(key) { state.calls.del.push(key); return Promise.resolve({ ok: true }); },
    settings_reveal() { return Promise.resolve({ ok: true, value: "gsk_FULLSECRETVALUE1234" }); },
    settings_history() { return ok({ lines: ["2026-10-26 07:00:00 | set     | GROQ_API_KEY | secret updated"] }); },
    settings_test(service, overrides) { state.calls.test.push([service, JSON.parse(overrides || "{}")]); state.testRunning = true; setTimeout(() => { state.testRunning = false; }, 400); return Promise.resolve({ ok: true }); },
    settings_test_status() { return ok(state.testRunning ? { groq: { running: true, result: null } } : { groq: { running: false, result: state.testResult } }); },
    settings_import_legacy() { state.calls.importLegacy++; state.settings.legacy = { dotenv_found: true, dotenv_keys: [], firebase_file: null, dismissed: true }; return Promise.resolve({ ok: true, imported: ["GROQ_API_KEY"], skipped: [], problems: [] }); },
    settings_dismiss_legacy() { state.calls.dismiss++; state.settings.legacy.dismissed = true; return Promise.resolve({ ok: true }); },
    settings_import_env_text() { return Promise.resolve({ ok: true, imported: ["GEMINI_API_KEY"], skipped: [], problems: [] }); },
  };
  Object.assign(api, promptFakes(state));
  const dom = new JSDOM(HTML, { runScripts: "outside-only", pretendToBeVisual: true, url: "http://localhost/" });
  const w = dom.window;
  const RealDate = w.Date;
  w.Date = class extends RealDate { // freeze "new Date()" with no args; explicit dates behave normally
    constructor(...a) { if (a.length === 0) super(FIXED_NOW); else super(...a); }
    static now() { return FIXED_NOW; }
  };
  w.pywebview = { api };
  w.eval(JS);
  w.dispatchEvent(new w.Event("pywebviewready"));
  // let already-resolved fake-API promises finish before the window is torn down
  return { w, doc: w.document, state, close: async () => { await sleep(30); w.close(); } };
}

function dragEvent(w, type) {
  const ev = new w.Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(ev, "dataTransfer", { value: { setData() {}, getData: () => "101" } });
  return ev;
}
const chipFor = (doc, id) => doc.querySelector('.ksm-chip[data-id="' + id + '"]');

const makeEnvWithTheme = (t) => makeEnv({ theme: t });
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("default deadline date is the LOCAL date (not the UTC date)", async () => {
  const e = makeEnv();
  assert.strictEqual(e.doc.getElementById("ksm-f-date").value, "2026-10-26");
  await e.close();
});

test("tasks are drawn in the right matrix cells", async () => {
  const e = makeEnv(); await sleep(150);
  assert.ok(chipFor(e.doc, 101).parentElement.matches('[data-imp="3"][data-urg="3"]'));
  assert.ok(chipFor(e.doc, 102).parentElement.matches('[data-imp="2"][data-urg="1"]'));
  await e.close();
});

test("unchanged data does not rebuild the matrix on every poll", async () => {
  const e = makeEnv(); await sleep(150);
  const chip = chipFor(e.doc, 101);
  const before = e.state.calls.snapshot;
  await sleep(2300); // at least two more polls
  assert.ok(e.state.calls.snapshot >= before + 2, "polling should continue");
  assert.ok(chip.isConnected && chipFor(e.doc, 101) === chip, "chip element was replaced during polling");
  await e.close();
});

test("a card can be dragged: no re-render while dragging, catch-up after dragend", async () => {
  const e = makeEnv(); await sleep(150);
  const chip = chipFor(e.doc, 101);
  chip.dispatchEvent(dragEvent(e.w, "dragstart"));
  e.state.tasks = e.state.tasks.concat([{ id: 103, title: "New one", imp: 1, urg: 1, expected_at: fmt(new Date(FIXED_NOW + 20 * 86400e3)), history: [] }]);
  await sleep(1300);
  assert.ok(chip.isConnected, "dragged chip was removed mid-drag");
  assert.strictEqual(chipFor(e.doc, 103), null, "DOM was rebuilt during a drag");
  chip.dispatchEvent(dragEvent(e.w, "dragend"));
  await sleep(200);
  assert.ok(chipFor(e.doc, 103), "UI did not catch up after the drag ended");
  await e.close();
});

test("dropping a card on a cell calls migrate_task", async () => {
  const e = makeEnv(); await sleep(150);
  const cell = e.doc.querySelector('.ksm-cell[data-imp="3"][data-urg="3"]');
  cell.dispatchEvent(dragEvent(e.w, "drop"));
  await sleep(100);
  assert.deepStrictEqual(e.state.calls.migrate[0], ["101", 3, 3]);
  await e.close();
});

test("task detail panel is not rebuilt every second", async () => {
  const e = makeEnv(); await sleep(150);
  chipFor(e.doc, 101).click();
  const hist = e.doc.getElementById("ksm-history");
  assert.ok(hist, "detail should show after click");
  await sleep(2300);
  assert.ok(hist.isConnected && e.doc.getElementById("ksm-history") === hist, "detail panel was rebuilt while idle");
  await e.close();
});

test("deleting a task removes its card and clears the detail panel", async () => {
  const e = makeEnv(); await sleep(150);
  chipFor(e.doc, 101).click();
  [...e.doc.querySelectorAll("#ksm-detail-body button")].find((b) => /Delete/.test(b.textContent)).click();
  await sleep(200);
  assert.strictEqual(chipFor(e.doc, 101), null, "card still drawn");
  assert.ok(!e.doc.getElementById("ksm-history"), "detail panel still shows the deleted task");
  await e.close();
});

test("countdown text still updates when the minute changes", async () => {
  const e = makeEnv(); await sleep(150);
  const before = chipFor(e.doc, 101).querySelector(".ksm-chip-time").textContent;
  e.state.now = new Date(e.state.now.getTime() + 120 * 1000); // jump two minutes
  await sleep(1300);
  const after = chipFor(e.doc, 101).querySelector(".ksm-chip-time").textContent;
  assert.notStrictEqual(after, before);
  await e.close();
});

test("data-file warning banner shows and hides", async () => {
  const e = makeEnv(); await sleep(150);
  const banner = e.doc.getElementById("ksm-banner");
  assert.ok(banner, "banner element missing");
  assert.ok(banner.hidden, "banner should be hidden when there is no warning");
  e.state.warning = "Task file could not be read. A copy was saved.";
  await sleep(1300);
  assert.ok(!banner.hidden && banner.textContent.includes("copy was saved"));
  e.state.warning = "";
  await sleep(1300);
  assert.ok(banner.hidden);
  await e.close();
});

const openSettings = async (e) => {
  e.doc.querySelector('#ksm-tabnav [data-tab="settings"]').click();
  await sleep(120);
};
const rowFor = (doc, key) => doc.querySelector('.ksm-set-row[data-key="' + key + '"]');
const btnIn = (root, re) => [...root.querySelectorAll("button")].find((b) => re.test(b.textContent));

test("settings tab lists groups; secrets are masked and the full key is never in the page", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  assert.ok(e.doc.getElementById("ksm-pane-settings").classList.contains("visible"));
  assert.strictEqual(e.doc.querySelectorAll(".ksm-set-group").length, 4);
  const row = rowFor(e.doc, "GROQ_API_KEY");
  assert.ok(row.textContent.includes("gsk_…1234"), "masked key missing");
  assert.ok(!e.doc.body.textContent.includes("FULLSECRET"), "a full secret is visible");
  assert.ok(e.doc.getElementById("ksm-set-storage").textContent.includes("OS keychain"));
  await e.close();
});

test("editing a secret saves it through settings_set (value in a password box)", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  btnIn(rowFor(e.doc, "GROQ_API_KEY"), /^Edit$/).click();
  const input = rowFor(e.doc, "GROQ_API_KEY").querySelector("input");
  assert.strictEqual(input.type, "password");
  input.value = "gsk_NEWKEYNEWKEY9999"; input.dispatchEvent(new e.w.Event("input", { bubbles: true }));
  btnIn(rowFor(e.doc, "GROQ_API_KEY"), /^Save$/).click();
  await sleep(150);
  assert.deepStrictEqual(e.state.calls.set[0], ["GROQ_API_KEY", "gsk_NEWKEYNEWKEY9999"]);
  assert.ok(e.doc.getElementById("ksm-set-status").textContent.includes("saved"));
  await e.close();
});

test("a rejected value shows its error next to the field and keeps the editor open", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  btnIn(rowFor(e.doc, "SCREEN_MAX_WIDTH"), /^Edit$/).click();
  const input = rowFor(e.doc, "SCREEN_MAX_WIDTH").querySelector("input");
  input.value = "99999"; input.dispatchEvent(new e.w.Event("input", { bubbles: true }));
  btnIn(rowFor(e.doc, "SCREEN_MAX_WIDTH"), /^Save$/).click();
  await sleep(150);
  assert.ok(rowFor(e.doc, "SCREEN_MAX_WIDTH").textContent.includes("at most 7680"));
  assert.ok(rowFor(e.doc, "SCREEN_MAX_WIDTH").querySelector("input"), "editor closed after an error");
  await e.close();
});

test("delete needs a second click to confirm", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  btnIn(rowFor(e.doc, "GROQ_API_KEY"), /^Delete$/).click();
  await sleep(50);
  assert.strictEqual(e.state.calls.del.length, 0, "deleted on the first click");
  btnIn(rowFor(e.doc, "GROQ_API_KEY"), /^Confirm\?$/).click();
  await sleep(150);
  assert.deepStrictEqual(e.state.calls.del, ["GROQ_API_KEY"]);
  await e.close();
});

test("Test connection sends unsaved text as an override and shows the result", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  btnIn(rowFor(e.doc, "GROQ_API_KEY"), /^Edit$/).click();
  const input = rowFor(e.doc, "GROQ_API_KEY").querySelector("input");
  input.value = "gsk_TRYTHISBEFORESAVING"; input.dispatchEvent(new e.w.Event("input", { bubbles: true }));
  btnIn(e.doc.querySelector('.ksm-set-group[data-group="groq"]'), /Test connection/).click();
  await sleep(1500);
  assert.deepStrictEqual(e.state.calls.test[0], ["groq", { GROQ_API_KEY: "gsk_TRYTHISBEFORESAVING" }]);
  const line = e.doc.querySelector('.ksm-set-testline[data-service="groq"]');
  assert.ok(!line.hidden && line.classList.contains("pass") && line.textContent.includes("321 ms"), "result not shown: " + line.className);
  await e.close();
});

test("a switch saves true/false immediately", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  const cb = rowFor(e.doc, "ENABLE_FIREBASE").querySelector('input[type="checkbox"]');
  cb.checked = true; cb.dispatchEvent(new e.w.Event("change", { bubbles: true }));
  await sleep(150);
  assert.deepStrictEqual(e.state.calls.set[0], ["ENABLE_FIREBASE", "true"]);
  await e.close();
});

test("old .env found: offer appears, Import now calls settings_import_legacy and hides it", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  const box = e.doc.getElementById("ksm-set-legacy");
  assert.ok(!box.hidden && box.textContent.includes("serviceAccountKey.json"));
  btnIn(box, /Import now/).click();
  await sleep(200);
  assert.strictEqual(e.state.calls.importLegacy, 1);
  assert.ok(e.doc.getElementById("ksm-set-legacy").hidden, "offer still visible after import");
  await e.close();
});

test("the filter box hides rows and empty groups", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  const s = e.doc.getElementById("ksm-set-search");
  s.value = "groq"; s.dispatchEvent(new e.w.Event("input", { bubbles: true }));
  assert.ok(rowFor(e.doc, "GROQ_API_KEY") && !rowFor(e.doc, "GROQ_API_KEY").hidden);
  assert.ok(rowFor(e.doc, "USER_NAME").hidden);
  assert.ok(e.doc.querySelector('.ksm-set-group[data-group="screen"]').hidden);
  await e.close();
});

test("history panel shows the change log and never needs a value", async () => {
  const e = makeEnv(); await sleep(100); await openSettings(e);
  e.doc.getElementById("ksm-set-history-btn").click();
  await sleep(100);
  const h = e.doc.getElementById("ksm-set-history");
  assert.ok(!h.hidden && h.textContent.includes("GROQ_API_KEY"));
  await e.close();
});

// ---- Phase 4F-3: sync status chip
function chipState(doc) {
  const c = doc.getElementById("ksm-syncchip");
  return { text: c.textContent, state: c.getAttribute("data-state"), cls: c.className, title: c.title };
}
test("sync chip shows Off when every switch is off", async () => {
  const e = makeEnv(); await sleep(100);
  const c = chipState(e.doc);
  assert.strictEqual(c.text, "Off"); assert.strictEqual(c.state, "off"); assert.ok(c.cls.includes("off"));
  assert.ok(c.title.includes("Nothing is sent online"));
  await e.close();
});

test("sync chip shows Synced, Pending N and Offline from get_sync_status (refreshed on a timer)", async () => {
  const e = makeEnv(); await sleep(100);
  assert.strictEqual(chipState(e.doc).state, "off");
  e.state.sync = { ok: true, state: "synced", label: "Synced", pending: 0, last_sync: "2026-10-26 07:00:00", last_error: "", kinds: {} };
  await sleep(2300);
  let c = chipState(e.doc);
  assert.strictEqual(c.text, "Synced"); assert.ok(c.cls.includes("synced")); assert.ok(c.title.includes("2026-10-26 07:00:00"));
  e.state.sync = { ok: true, state: "pending", label: "Pending 3", pending: 3, last_sync: "", last_error: "", kinds: {} };
  await sleep(2300);
  c = chipState(e.doc);
  assert.strictEqual(c.text, "Pending 3"); assert.strictEqual(c.state, "pending");
  e.state.sync = { ok: true, state: "offline", label: "Offline", pending: 3, last_sync: "", last_error: "Could not reach Firebase.", kinds: {} };
  await sleep(2300);
  c = chipState(e.doc);
  assert.strictEqual(c.text, "Offline"); assert.strictEqual(c.state, "offline"); assert.ok(c.title.includes("Could not reach Firebase."));
  assert.ok(e.state.calls.syncStatus >= 4);
  await e.close();
});

test("sync chip: a failing or odd answer shows Unknown, never a crash, and an unknown state falls back to off", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.syncFails = true;
  await sleep(2300);
  assert.strictEqual(chipState(e.doc).text, "Unknown");
  e.state.syncFails = false;
  e.state.sync = { ok: true, state: "weird", label: "", pending: 0, last_sync: "", last_error: "", kinds: {} };
  await sleep(2300);
  const c = chipState(e.doc);
  assert.strictEqual(c.state, "off"); assert.strictEqual(c.text, "Off");
  const errors = e.state.calls.logClient.filter((x) => x[0] === "error");
  assert.deepStrictEqual(errors, []);
  await e.close();
});

test("sync chip text comes from the backend as text only (no HTML is built from it)", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.sync = { ok: true, state: "pending", label: "<b>Pending 1</b>", pending: 1, last_sync: "", last_error: "", kinds: {} };
  await sleep(2300);
  const chip = e.doc.getElementById("ksm-syncchip");
  assert.strictEqual(chip.children.length, 0);
  assert.strictEqual(chip.textContent, "<b>Pending 1</b>");
  await e.close();
});

test("sync chip uses theme variables only (no hard-coded colours in its CSS)", async () => {
  const css = fs.readFileSync(path.join(WEBUI, "style.css"), "utf8");
  const rules = css.split("}").filter((r) => r.includes(".ksm-syncchip"));
  assert.ok(rules.length >= 5);
  for (const r of rules) assert.ok(!/#[0-9a-fA-F]{3,8}\b|rgb\(|rgba\(|hsl\(/.test(r), "hard-coded colour in: " + r.trim());
});

// ---- Phase 4F-4: Mobile Sync tab additions
const byId = (doc, id) => doc.getElementById(id);
test("mobile tab shows the last sync time from get_sync_status and the 'nothing uploaded' note", async () => {
  const e = makeEnv(); await sleep(100);
  assert.ok(byId(e.doc, "ksm-sync-last").textContent.includes("nothing is sent online"));
  assert.ok(byId(e.doc, "ksm-sync-note").textContent.includes("Nothing is uploaded unless"));
  e.state.sync = { ok: true, state: "synced", label: "Synced", pending: 0, last_sync: "2026-10-27 22:15:00", last_error: "", kinds: {} };
  await sleep(2300);
  assert.ok(byId(e.doc, "ksm-sync-last").textContent.includes("2026-10-27 22:15:00"));
  await e.close();
});

test("Sync now calls sync_now, shows what started, and refreshes the chip", async () => {
  const e = makeEnv(); await sleep(100);
  const before = e.state.calls.syncStatus;
  byId(e.doc, "ksm-sync-now").click();
  await sleep(100);
  assert.strictEqual(e.state.calls.syncNow, 1);
  const m = byId(e.doc, "ksm-sync-msg");
  assert.ok(m.textContent.includes("Sending started") && m.textContent.includes("tasks, chats"));
  assert.ok(m.className.includes("ok"));
  assert.ok(e.state.calls.syncStatus > before);
  assert.strictEqual(byId(e.doc, "ksm-sync-now").disabled, false);
  await e.close();
});

test("Sync now with every switch off shows the backend message", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.syncNowAnswer = { ok: true, state: "off", label: "Off", pending: 0, last_sync: "", last_error: "", kinds: {}, started: [], message: "Nothing to send: every online copy switch is off." };
  byId(e.doc, "ksm-sync-now").click(); await sleep(100);
  assert.ok(byId(e.doc, "ksm-sync-msg").textContent.includes("Nothing to send"));
  await e.close();
});

test("a failing Sync now shows a message, re-enables the button and logs no JS error", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.syncNowFails = true;
  byId(e.doc, "ksm-sync-now").click(); await sleep(100);
  const m = byId(e.doc, "ksm-sync-msg");
  assert.ok(m.textContent.includes("Sync could not start")); assert.ok(m.className.includes("err"));
  assert.strictEqual(byId(e.doc, "ksm-sync-now").disabled, false);
  e.state.syncNowFails = false;
  e.state.syncNowAnswer = "not an object";
  byId(e.doc, "ksm-sync-now").click(); await sleep(100);
  assert.ok(m.textContent.includes("Sync could not start"));
  assert.deepStrictEqual(e.state.calls.logClient.filter((x) => x[0] === "error"), []);
  await e.close();
});

test("Export cloud data calls export_cloud_data and shows the saved path and counts", async () => {
  const e = makeEnv(); await sleep(100);
  byId(e.doc, "ksm-sync-export").click(); await sleep(100);
  assert.strictEqual(e.state.calls.exportCloud, 1);
  const m = byId(e.doc, "ksm-sync-msg");
  assert.ok(m.textContent.includes("Saved to: C:\\Users\\t\\ksm\\exports\\ksm-cloud-export-1.json"));
  assert.ok(m.textContent.includes("tasks: 2") && m.textContent.includes("chats: 5"));
  assert.ok(m.className.includes("ok"));
  assert.strictEqual(byId(e.doc, "ksm-sync-export").disabled, false);
  await e.close();
});

test("a failed or crashing export shows the error text, never a crash; the path is shown as text only", async () => {
  const e = makeEnv(); await sleep(100);
  const m = byId(e.doc, "ksm-sync-msg");
  e.state.exportAnswer = { ok: false, error: "The online copy is not set up." };
  byId(e.doc, "ksm-sync-export").click(); await sleep(100);
  assert.ok(m.textContent.includes("Export failed") && m.textContent.includes("not set up")); assert.ok(m.className.includes("err"));
  e.state.exportFails = true;
  byId(e.doc, "ksm-sync-export").click(); await sleep(100);
  assert.ok(m.textContent.includes("Export failed")); assert.strictEqual(byId(e.doc, "ksm-sync-export").disabled, false);
  e.state.exportFails = false;
  e.state.exportAnswer = { ok: true, path: "<img src=x onerror=alert(1)>", counts: {} };
  byId(e.doc, "ksm-sync-export").click(); await sleep(100);
  assert.strictEqual(m.children.length, 0); assert.ok(m.textContent.includes("<img src=x"));
  assert.deepStrictEqual(e.state.calls.logClient.filter((x) => x[0] === "error"), []);
  await e.close();
});

test("sync panel CSS uses theme variables only (no hard-coded colours)", async () => {
  const css = fs.readFileSync(path.join(WEBUI, "style.css"), "utf8");
  const rules = css.split("}").filter((r) => /#ksm-sync(panel|-last|-btns|-msg|-note)/.test(r));
  assert.ok(rules.length >= 6);
  for (const r of rules) assert.ok(!/#[0-9a-fA-F]{3,8}\b|rgb\(|rgba\(|hsl\(/.test(r.replace(/#ksm-sync[-a-z]*/g, "")), "hard-coded colour in: " + r.trim());
});

// ---- Phase 4F-5: status colours must stay readable in every theme (found in the slate theme)
test("chip and panel status colours reach 4.5:1 contrast on the panel backgrounds in all four themes", async () => {
  const css = fs.readFileSync(path.join(WEBUI, "style.css"), "utf8");
  const lum = (hex) => {
    const h = hex.replace("#", "");
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
      .map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  for (const theme of ["scifi", "saffron", "slate", "forest"]) {
    const block = css.match(new RegExp('data-theme="' + theme + '"\\] \\{([^}]*)\\}'));
    assert.ok(block, "theme block missing: " + theme);
    const v = {};
    block[1].replace(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})/g, (m, k, c) => { v[k] = c; return m; });
    for (const colour of ["good", "warn-bright", "danger-bright"]) {
      for (const bg of ["bg-panel", "bg-panel2"]) {
        const r = ratio(v[colour], v[bg]);
        assert.ok(r >= 4.5, theme + ": --" + colour + " on --" + bg + " is " + r.toFixed(2) + ":1 (needs 4.5)");
      }
    }
  }
});

// ---- Phase 5E: PROMPTS tab and settings badges
const PHRASE = "task addtion initiated";
async function openPrompts(e) {
  e.doc.querySelector('.ksm-tbtn[data-tab="prompts"]').click();
  await sleep(150);
}
const typeInto = (e, el, text) => { el.value = text; el.dispatchEvent(new e.w.Event("input", { bubbles: true })); };
const blockEl = (e, id) => e.doc.querySelector('.ksm-pb[data-block="' + id + '"]');
const btn = (e, id) => e.doc.getElementById(id);

test("PROMPTS tab lists the four roles, the prompts of the first role and marks the one in use", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  const roles = [...e.doc.querySelectorAll(".ksm-pr-role")].map((b) => b.textContent);
  assert.deepStrictEqual(roles, ["AI chat", "Task extraction", "Strategy audit", "Screen scan"]);
  const items = [...e.doc.querySelectorAll(".ksm-pr-item")];
  assert.strictEqual(items.length, 2);
  assert.ok(items[0].textContent.includes("IN USE") && items[0].textContent.includes("BUILT-IN"));
  assert.ok(!items[1].textContent.includes("IN USE") && items[1].textContent.includes("EDITED"));
  assert.ok(e.doc.getElementById("ksm-pr-status").textContent.includes("Synced"));
  await e.close();
});

test("locked blocks are read-only text with a lock; editable ones have a text box, a switch and Reset block", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  const locked = blockEl(e, "trigger_rule");
  assert.ok(locked.classList.contains("locked") && locked.textContent.includes("LOCKED") && locked.textContent.includes("🔒"));
  assert.ok(!locked.querySelector("textarea") && !locked.querySelector("input") && !locked.querySelector("button"));
  assert.strictEqual(locked.querySelector("pre").textContent, "Use the phrase only for a NEW goal.");
  const ed = blockEl(e, "identity");
  assert.strictEqual(ed.querySelector("textarea").value, "You are KSM.");
  assert.ok(ed.querySelector('input[type="checkbox"]') && /Reset block/.test(ed.textContent));
  assert.ok(blockEl(e, "extra"));
  await e.close();
});

test("typing the trigger phrase shows a live error and blocks Save; fixing the text allows it again", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  const ta = blockEl(e, "extra").querySelector("textarea");
  assert.strictEqual(btn(e, "ksm-pr-save").disabled, true, "nothing changed yet");
  typeInto(e, ta, "Say " + PHRASE + " always");
  await sleep(450);
  assert.ok(e.doc.getElementById("ksm-pr-problems").textContent.includes("trigger phrase"));
  assert.strictEqual(btn(e, "ksm-pr-save").disabled, true);
  typeInto(e, ta, "Be brief.");
  await sleep(450);
  assert.strictEqual(e.doc.getElementById("ksm-pr-problems").textContent, "");
  assert.strictEqual(btn(e, "ksm-pr-save").disabled, false);
  await e.close();
});

test("Save sends only the changed block with the version it started from, then shows the result", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  typeInto(e, blockEl(e, "extra").querySelector("textarea"), "Be brief.");
  await sleep(450);
  btn(e, "ksm-pr-save").click(); await sleep(250);
  assert.deepStrictEqual(e.state.pr.calls.save, [["p-chat", { extra: { text: "Be brief." } }, 3, ""]]);
  assert.ok(e.doc.getElementById("ksm-pr-msg").textContent.includes("Saved as version 4"));
  assert.strictEqual(btn(e, "ksm-pr-save").disabled, true, "nothing unsaved after a save");
  await e.close();
});

test("a switch is sent as enabled true/false and a refused or queued save is told in plain words", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  const cb = blockEl(e, "identity").querySelector('input[type="checkbox"]');
  cb.checked = false; cb.dispatchEvent(new e.w.Event("change", { bubbles: true }));
  await sleep(450);
  e.state.pr.saveAnswer = { ok: false, message: "The prompt was not saved: too long.", errors: ["too long"] };
  btn(e, "ksm-pr-save").click(); await sleep(250);
  assert.deepStrictEqual(e.state.pr.calls.save[0][1], { identity: { enabled: false } });
  const m = e.doc.getElementById("ksm-pr-msg");
  assert.ok(m.textContent.includes("too long") && m.className.includes("err"));
  e.state.pr.saveAnswer = { ok: true, version: 4, queued: true, warnings: [] };
  btn(e, "ksm-pr-save").click(); await sleep(250);
  assert.ok(m.textContent.includes("will be sent when Firebase can be reached") && m.className.includes("warn"));
  await e.close();
});

test("Preview shows the final text as text, the size and tokens, and saves nothing", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  typeInto(e, blockEl(e, "extra").querySelector("textarea"), "Hi");
  btn(e, "ksm-pr-previewbtn").click(); await sleep(200);
  const box = e.doc.getElementById("ksm-pr-preview");
  assert.ok(!box.hidden && box.textContent.includes("23 characters") && box.textContent.includes("6 tokens"));
  assert.ok(box.textContent.includes("FINAL <b>TEXT</b> for T") && !box.querySelector("b"), "preview must be text, never HTML");
  assert.deepStrictEqual(e.state.pr.calls.preview[0], ["p-chat", { extra: { text: "Hi" } }]);
  assert.deepStrictEqual(e.state.pr.calls.save, []);
  e.state.pr.previewAnswer = { ok: true, text: "x".repeat(10), chars: 7000, tokens: 1750, warnings: [], errors: [] };
  btn(e, "ksm-pr-previewbtn").click(); await sleep(200);
  assert.ok(box.textContent.includes("This prompt is long"));
  await e.close();
});

test("Add your own block adds an editable block with a title and sends title, text, switch and order", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  btn(e, "ksm-pr-addblock").click(); await sleep(200);
  const nb = blockEl(e, "c-new1");
  assert.ok(nb && nb.querySelector("textarea") && nb.querySelector("input.title") && /Remove block/.test(nb.textContent));
  typeInto(e, nb.querySelector("textarea"), "My rule.");
  await sleep(450);
  btn(e, "ksm-pr-save").click(); await sleep(250);
  const sent = e.state.pr.calls.save[0][1]["c-new1"];
  assert.strictEqual(sent.text, "My rule."); assert.strictEqual(sent.title, "My block"); assert.strictEqual(sent.enabled, true); assert.strictEqual(typeof sent.order, "number");
  await e.close();
});

test("Save as new needs a name, sends the role, name, changes and the prompt it started from", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  btn(e, "ksm-pr-saveas").click(); await sleep(100);
  assert.ok(e.doc.getElementById("ksm-pr-msg").textContent.includes("Type a name"));
  assert.deepStrictEqual(e.state.pr.calls.saveAs, []);
  typeInto(e, blockEl(e, "extra").querySelector("textarea"), "Strict.");
  btn(e, "ksm-pr-newname").value = "Strict chat";
  btn(e, "ksm-pr-saveas").click(); await sleep(300);
  assert.deepStrictEqual(e.state.pr.calls.saveAs, [["chat", "Strict chat", { extra: { text: "Strict." } }, "p-chat"]]);
  assert.ok([...e.doc.querySelectorAll(".ksm-pr-item")].some((b) => b.textContent.includes("Strict chat")));
  await e.close();
});

test("Set in use, two-step Reset prompt and two-step Delete (built-in prompts cannot be deleted)", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  assert.strictEqual(btn(e, "ksm-pr-delete").disabled, true, "built-in");
  assert.strictEqual(btn(e, "ksm-pr-active").disabled, true, "already in use");
  btn(e, "ksm-pr-reset").click(); await sleep(50);
  assert.deepStrictEqual(e.state.pr.calls.reset, []);
  assert.strictEqual(btn(e, "ksm-pr-reset").textContent, "Confirm reset?");
  btn(e, "ksm-pr-reset").click(); await sleep(200);
  assert.deepStrictEqual(e.state.pr.calls.reset, ["p-chat"]);
  e.doc.querySelector('.ksm-pr-item[data-id="p-casual"]').click(); await sleep(200);
  assert.strictEqual(btn(e, "ksm-pr-delete").disabled, false);
  btn(e, "ksm-pr-active").click(); await sleep(250);
  assert.deepStrictEqual(e.state.pr.calls.active, [["chat", "p-casual"]]);
  assert.strictEqual(btn(e, "ksm-pr-delete").disabled, true, "in use: choose another first");
  e.doc.querySelector('.ksm-pr-item[data-id="p-chat"]').click(); await sleep(200);
  btn(e, "ksm-pr-active").click(); await sleep(250);
  e.doc.querySelector('.ksm-pr-item[data-id="p-casual"]').click(); await sleep(200);
  btn(e, "ksm-pr-delete").click(); await sleep(50);
  assert.deepStrictEqual(e.state.pr.calls.del, []);
  btn(e, "ksm-pr-delete").click(); await sleep(250);
  assert.deepStrictEqual(e.state.pr.calls.del, ["p-casual"]);
  await e.close();
});

test("Reset block calls the backend for that block", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  [...blockEl(e, "identity").querySelectorAll("button")].find((b) => /Reset block/.test(b.textContent)).click(); await sleep(250);
  assert.deepStrictEqual(e.state.pr.calls.resetBlock, [["p-chat", "identity"]]);
  await e.close();
});

test("History lists the versions and Restore needs a second click", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  btn(e, "ksm-pr-historybtn").click(); await sleep(200);
  const box = e.doc.getElementById("ksm-pr-history");
  assert.ok(!box.hidden && box.textContent.includes("Version 3") && box.textContent.includes("Version 2"));
  const first = [...box.querySelectorAll("button")][0];
  first.click(); await sleep(50);
  assert.deepStrictEqual(e.state.pr.calls.restore, []);
  assert.ok(/Confirm restore/.test(box.textContent));
  [...box.querySelectorAll("button")][0].click(); await sleep(250);
  assert.deepStrictEqual(e.state.pr.calls.restore, [["p-chat", 3]]);
  await e.close();
});

test("a prompt changed on another computer offers Keep mine / Take theirs", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.pr.conflict = true;
  await openPrompts(e);
  const c = e.doc.getElementById("ksm-pr-conflict");
  assert.ok(!c.hidden && /Keep mine/.test(c.textContent) && /Take theirs/.test(c.textContent));
  [...c.querySelectorAll("button")].find((b) => /Take theirs/.test(b.textContent)).click(); await sleep(250);
  assert.deepStrictEqual(e.state.pr.calls.resolve, [["p-chat", "theirs"]]);
  assert.ok(e.doc.getElementById("ksm-pr-conflict").hidden);
  await e.close();
});

test("warnings about shipped or cached text in use are shown on the PROMPTS tab", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.pr.notes = ["The saved copy is in use because Firebase cannot be reached."];
  e.state.pr.status = { state: "offline", pending: 2, warnings: [] };
  await openPrompts(e);
  assert.ok(e.doc.getElementById("ksm-pr-notes").textContent.includes("saved copy is in use"));
  assert.ok(e.doc.getElementById("ksm-pr-status").textContent.includes("Offline") && e.doc.getElementById("ksm-pr-status").textContent.includes("2 waiting"));
  await e.close();
});

test("switching role shows that role's prompt; unsaved text in the old prompt is dropped with a note", async () => {
  const e = makeEnv(); await sleep(100); await openPrompts(e);
  typeInto(e, blockEl(e, "extra").querySelector("textarea"), "draft");
  [...e.doc.querySelectorAll(".ksm-pr-role")].find((b) => /Strategy audit/.test(b.textContent)).click(); await sleep(250);
  const items = [...e.doc.querySelectorAll(".ksm-pr-item")];
  assert.strictEqual(items.length, 1); assert.ok(items[0].textContent.includes("Audit"));
  assert.ok(e.doc.getElementById("ksm-pr-msg").textContent.includes("dropped"));
  assert.strictEqual(btn(e, "ksm-pr-save").disabled, true);
  await e.close();
});

test("PROMPTS tab: every call failing (rejected) shows messages, never a crash, and logs no JS error", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.pr.failAll = true;
  await openPrompts(e);
  assert.ok(e.doc.getElementById("ksm-pr-msg").textContent.length > 0);
  assert.ok(e.doc.getElementById("ksm-pr-editor").hidden);
  e.state.pr.failAll = false;
  await e.w.eval("void 0");
  e.doc.querySelector('.ksm-tbtn[data-tab="matrix"]').click();
  await openPrompts(e);
  assert.ok(!e.doc.getElementById("ksm-pr-editor").hidden);
  e.state.pr.failAll = true;
  typeInto(e, blockEl(e, "extra").querySelector("textarea"), "x"); await sleep(450);
  btn(e, "ksm-pr-save").click(); await sleep(250);
  assert.ok(/did not answer|not saved/i.test(e.doc.getElementById("ksm-pr-msg").textContent));
  assert.deepStrictEqual(e.state.calls.logClient.filter((x) => x[0] === "error"), []);
  await e.close();
});

test("PROMPTS tab: a missing API method or an odd answer is a message, not a crash", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.calls.logClient.length = 0;
  const api = e.w.pywebview.api;
  api.prompts_get = () => Promise.resolve("not an object");
  await openPrompts(e);
  assert.ok(e.doc.getElementById("ksm-pr-msg").textContent.includes("odd answer"));
  delete api.prompts_get;
  e.doc.querySelector('.ksm-tbtn[data-tab="matrix"]').click();
  await openPrompts(e);
  assert.ok(e.doc.getElementById("ksm-pr-msg").textContent.includes("did not answer"));
  assert.deepStrictEqual(e.state.calls.logClient.filter((x) => x[0] === "error"), []);
  await e.close();
});

test("prompt text from the backend is shown as text (a prompt containing HTML is inert)", async () => {
  const e = makeEnv(); await sleep(100);
  e.w.pywebview.api.prompt_get = () => Promise.resolve({ ok: true, id: "p-chat", name: "<i>Chat</i>", role: "chat", builtin: true, version: 1, active: true,
    pending: false, conflict: false, warnings: [], blocks: [{ id: "identity", title: "<b>Id</b>", kind: "layout", locked: false, text: "<img src=x onerror=alert(1)>", enabled: true, modified: false },
    { id: "trigger_rule", title: "T", kind: "locked", locked: true, text: "<script>1</script>", enabled: true }] });
  await openPrompts(e);
  const ed = e.doc.getElementById("ksm-pane-prompts");
  assert.ok(!ed.querySelector("img") && !ed.querySelector("script") && !ed.querySelector("#ksm-pr-head i") && !ed.querySelector(".ksm-pb b"));
  assert.strictEqual(ed.querySelector('.ksm-pb[data-block="identity"] textarea').value, "<img src=x onerror=alert(1)>");
  assert.ok(ed.textContent.includes("<script>1</script>") && ed.textContent.includes("<i>Chat</i>"));
  await e.close();
});

test("SETTINGS rows carry a scope badge: Common, This device, Local only, Secret", async () => {
  const e = makeEnv(); await sleep(100);
  e.doc.querySelector('.ksm-tbtn[data-tab="settings"]').click(); await sleep(200);
  const badge = (key) => e.doc.querySelector('.ksm-set-row[data-key="' + key + '"] .scope .ksm-badge').textContent;
  assert.strictEqual(badge("USER_NAME"), "COMMON");
  assert.strictEqual(badge("SCREEN_MAX_WIDTH"), "THIS DEVICE");
  assert.strictEqual(badge("ENABLE_FIREBASE"), "LOCAL ONLY");
  assert.strictEqual(badge("GROQ_API_KEY"), "SECRET");
  assert.ok(e.doc.getElementById("ksm-set-shared").textContent.includes("TestPC"));
  assert.ok(e.doc.getElementById("ksm-set-shared").textContent.includes("in step with Firebase"));
  await e.close();
});

test("SETTINGS: a setting set by an environment variable says so; an edit that lost shows a notice that can be dismissed", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.settings.shared.overridden = ["USER_NAME"];
  e.state.settings.shared.notices = [{ key: "USER_NAME", mine: "Ravi", theirs: "Tara", at_ms: 1 }];
  e.doc.querySelector('.ksm-tbtn[data-tab="settings"]').click(); await sleep(200);
  assert.ok(e.doc.querySelector('.ksm-set-row[data-key="USER_NAME"] .scope').textContent.includes("SET BY ENVIRONMENT"));
  const sh = e.doc.getElementById("ksm-set-shared");
  assert.ok(sh.textContent.includes("Ravi") && sh.textContent.includes("Tara") && sh.textContent.includes("Your name"));
  [...sh.querySelectorAll("button")].find((b) => /Dismiss/.test(b.textContent)).click(); await sleep(250);
  assert.ok(!e.doc.getElementById("ksm-set-shared").textContent.includes("Tara"));
  await e.close();
});

test("SETTINGS: a failed shared status hides the box instead of breaking the tab", async () => {
  const e = makeEnv(); await sleep(100);
  e.state.settings.shared = { ok: false, message: "x" };
  e.doc.querySelector('.ksm-tbtn[data-tab="settings"]').click(); await sleep(200);
  assert.ok(e.doc.getElementById("ksm-set-shared").hidden);
  assert.ok(e.doc.querySelector('.ksm-set-row[data-key="USER_NAME"]'));
  await e.close();
});

test("theme: clicking a theme applies it and saves it as the per-device setting THEME; the saved theme is applied at start", async () => {
  const e = makeEnv(); await sleep(100);
  e.doc.querySelector('.ksm-themebtn[data-theme="forest"]').click(); await sleep(100);
  assert.strictEqual(e.doc.getElementById("ksm-root").getAttribute("data-theme"), "forest");
  assert.deepStrictEqual(e.state.calls.set.filter((c) => c[0] === "THEME"), [["THEME", "forest"]]);
  await e.close();
  const f = makeEnvWithTheme("saffron"); await sleep(200);
  assert.strictEqual(f.doc.getElementById("ksm-root").getAttribute("data-theme"), "saffron");
  assert.deepStrictEqual(f.state.calls.set.filter((c) => c[0] === "THEME"), [], "applying the saved theme must not write it back");
  await f.close();
});

test("PROMPTS and settings-badge CSS use theme variables only (no hard-coded colours)", async () => {
  const css = fs.readFileSync(path.join(WEBUI, "style.css"), "utf8");
  const start = css.indexOf("PROMPTS tab (Phase 5E)");
  assert.ok(start > 0);
  const tail = css.slice(start).replace(/\/\*[\s\S]*?\*\//g, "");
  for (const r of tail.split("}")) assert.ok(!/#[0-9a-fA-F]{3,8}\b(?![-\w])|rgb\(|rgba\(|hsl\(/.test(r.replace(/#ksm-[-a-z]+/g, "")), "hard-coded colour in: " + r.trim());
});

test("PROMPTS tab text colours reach 4.5:1 on the panel backgrounds in all four themes", async () => {
  const css = fs.readFileSync(path.join(WEBUI, "style.css"), "utf8");
  const lum = (hex) => {
    const h = hex.replace("#", "");
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
      .map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  for (const theme of ["scifi", "saffron", "slate", "forest"]) {
    const block = css.match(new RegExp('data-theme="' + theme + '"\\] \\{([^}]*)\\}'));
    const v = {};
    block[1].replace(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})/g, (m, k, c) => { v[k] = c; return m; });
    // badges and counts sit on --bg-panel (list buttons, cards) and --bg-panel2 (block frames); read-only text on --bg-panel
    for (const colour of ["text1", "text2", "accent-bright", "good", "warn-bright", "danger-bright"]) {
      for (const bg of ["bg-panel", "bg-panel2", "bg-page"]) {
        const r = ratio(v[colour], v[bg]);
        assert.ok(r >= 4.5, theme + ": --" + colour + " on --" + bg + " is " + r.toFixed(2) + ":1 (needs 4.5)");
      }
    }
  }
});

test("no JavaScript errors were reported to the Python log", async () => {
  const e = makeEnv(); await sleep(1500);
  const errors = e.state.calls.logClient.filter((c) => c[0] === "error");
  assert.deepStrictEqual(errors, []);
  await e.close();
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log("PASS  " + name); }
    catch (err) { failed++; console.log("FAIL  " + name + "\n      " + String(err.message).split("\n")[0]); }
  }
  console.log("\n" + (tests.length - failed) + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})();
