(function () {
  "use strict";

  var root = document.getElementById("ksm-root");
  var api = null; // set once pywebview is ready

  // ---- theme picker --------------------------------------------------------
  var THEMES = [["scifi", "Sci-fi blue"], ["saffron", "Sacred saffron"], ["slate", "Minimal slate"], ["forest", "Forest emerald"]];
  var themerow = document.getElementById("ksm-themerow");
  function applyTheme(t) {
    root.setAttribute("data-theme", t);
    Array.prototype.forEach.call(themerow.children, function (b) {
      b.classList.toggle("active", b.dataset.theme === t);
    });
    try { localStorage.setItem("ksm-theme", t); } catch (e) {}
  }
  THEMES.forEach(function (t) {
    var b = document.createElement("button");
    b.className = "ksm-themebtn";
    b.textContent = t[1];
    b.dataset.theme = t[0];
    b.addEventListener("click", function () {
      applyTheme(t[0]);
      callApi("settings_set", ["THEME", t[0]]);   // a per-device setting: kept in Firebase with this device's values
    });
    themerow.appendChild(b);
  });
  applyTheme((function () { try { return localStorage.getItem("ksm-theme") || "scifi"; } catch (e) { return "scifi"; } })());

  // ---- tab nav --------------------------------------------------------------
  var tabs = document.querySelectorAll("#ksm-tabnav .ksm-tbtn");
  var panes = { matrix: "ksm-pane-matrix", chat: "ksm-pane-chat", strategy: "ksm-pane-strategy", screen: "ksm-pane-screen", mobile: "ksm-pane-mobile", prompts: "ksm-pane-prompts", settings: "ksm-pane-settings", debug: "ksm-pane-debug" };
  var activeTab = "matrix";
  tabs.forEach(function (btn) {
    btn.addEventListener("click", function () {
      activeTab = btn.dataset.tab;
      tabs.forEach(function (b) { b.classList.toggle("active", b === btn); });
      Object.keys(panes).forEach(function (k) {
        document.getElementById(panes[k]).classList.toggle("visible", k === activeTab);
      });
      if (activeTab === "chat") scrollChatToEnd();
      if (activeTab === "settings") loadSettings();
      if (activeTab === "prompts") loadPrompts();
    });
  });

  // ---- helpers ---------------------------------------------------------------
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function parseServerTime(s) {
    // "YYYY-MM-DD HH:MM:SS" -> Date (treated as local time, matching the backend)
    var m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(s);
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  }

  // ---- matrix state -----------------------------------------------------------
  var CELLS = [3, 2, 1]; // importance columns
  var ROWS = [3, 2, 1]; // urgency rows
  var cellEls = {}; // "imp,urg" -> div
  var grid = document.getElementById("ksm-grid");
  var rowLabels = ["Very Urgent (R1)", "Moderate (R2)", "Less Urgent (R3)"];
  ROWS.forEach(function (u, ri) {
    var label = el("div", "ksm-row-label", rowLabels[ri]);
    grid.appendChild(label);
    CELLS.forEach(function (i) {
      var cell = el("div", "ksm-cell");
      cell.dataset.imp = i;
      cell.dataset.urg = u;
      cell.addEventListener("dragover", function (ev) {
        ev.preventDefault();
        cell.classList.add("ksm-dragover");
      });
      cell.addEventListener("dragleave", function () { cell.classList.remove("ksm-dragover"); });
      cell.addEventListener("drop", function (ev) {
        ev.preventDefault();
        cell.classList.remove("ksm-dragover");
        var taskId = ev.dataTransfer.getData("text/plain");
        if (!taskId) return;
        api.migrate_task(taskId, i, u).then(function (res) {
          if (!res.ok && res.error) showToast(res.error);
          refreshSnapshot();
        });
      });
      grid.appendChild(cell);
      cellEls[i + "," + u] = cell;
    });
  });

  var selectedTaskId = null;
  var lastTasks = [];
  var dragging = false;       // true while a card is being dragged
  var lastTasksSig = null;    // JSON of the tasks drawn last time
  var lastMinuteKey = null;   // "YYYY-MM-DD HH:MM" drawn last time (countdown text changes per minute)

  function showToast(msg) {
    var d = el("div", "ksm-empty-hint", msg);
    d.style.color = "var(--danger-bright)";
    var host = document.getElementById("ksm-detail-body");
    host.insertBefore(d, host.firstChild);
    setTimeout(function () { if (d.parentNode) d.parentNode.removeChild(d); }, 4000);
  }

  var lastDetailSig = null; // JSON of the task currently shown in the detail panel

  function renderDetail(task) {
    var host = document.getElementById("ksm-detail-body");
    lastDetailSig = task ? JSON.stringify(task) : "";
    host.innerHTML = "";
    if (!task) {
      host.appendChild(el("p", "ksm-empty-hint", "Click a task to see details."));
      return;
    }
    host.appendChild(el("div", "ksm-title", task.title));
    host.appendChild(el("div", "ksm-meta", "Importance " + task.imp + " | Urgency " + task.urg));
    host.appendChild(el("div", "ksm-meta", "Due: " + task.expected_at));
    var hist = el("div", null);
    hist.id = "ksm-history";
    (task.history || []).forEach(function (h) { hist.appendChild(el("div", null, h)); });
    host.appendChild(hist);
    var del = el("button", "ksm-btn danger block", "Delete Task");
    del.addEventListener("click", function () {
      api.delete_task(task.id).then(function () {
        selectedTaskId = null;
        renderDetail(null); // clear the panel so it does not keep showing the deleted task
        refreshSnapshot(true);
      });
    });
    host.appendChild(del);
  }

  function renderMatrix(data) {
    lastTasks = data.tasks || [];
    var now = parseServerTime(data.now) || new Date();

    Object.keys(cellEls).forEach(function (k) { cellEls[k].innerHTML = ""; });
    var urgentBox = document.getElementById("ksm-urgent-list");
    var todoBox = document.getElementById("ksm-todo-list");
    urgentBox.innerHTML = "";
    todoBox.innerHTML = "";
    var urgentCount = 0, todoCount = 0;

    lastTasks.forEach(function (task) {
      var key = task.imp + "," + task.urg;
      var cell = cellEls[key];
      if (!cell) return;

      var expDt = parseServerTime(task.expected_at);
      var overdue = false, remText = "";
      if (expDt) {
        var totalSec = Math.floor((expDt - now) / 1000);
        if (totalSec > 0) {
          var d = Math.floor(totalSec / 86400);
          var h = Math.floor((totalSec % 86400) / 3600);
          var m = Math.floor((totalSec % 3600) / 60);
          remText = d > 0 ? (d + "d " + h + "h " + m + "m") : (h + "h " + m + "m");
        } else {
          overdue = true;
          remText = "OVERDUE";
        }
      }
      var isAuto = task.history && task.history.length && /\[AUTO\]/.test(task.history[task.history.length - 1]);

      var chip = el("div", "ksm-chip" + (overdue ? " overdue" : "") + (isAuto && !overdue ? " auto" : "") + (task.id === selectedTaskId ? " selected" : ""));
      chip.draggable = true;
      chip.dataset.id = task.id;
      chip.appendChild(el("div", "ksm-chip-title", task.title));
      chip.appendChild(el("div", "ksm-chip-time", remText));
      chip.addEventListener("click", function () {
        selectedTaskId = task.id;
        renderDetail(task);
        highlightSelected();
      });
      chip.addEventListener("dragstart", function (ev) {
        dragging = true; // pause re-rendering so the dragged card is not destroyed mid-drag
        ev.dataTransfer.setData("text/plain", String(task.id));
      });
      chip.addEventListener("dragend", function () {
        dragging = false;
        refreshSnapshot(true); // catch up with anything that changed during the drag
      });
      cell.appendChild(chip);

      var isUrgentNow = (task.urg === 3 && task.imp >= 2) || (task.urg === 2 && task.imp === 3);
      var li = el("div", "ksm-list-item", task.title);
      li.addEventListener("click", function () { selectedTaskId = task.id; renderDetail(task); highlightSelected(); });
      if (isUrgentNow) { urgentBox.appendChild(li); urgentCount++; }
      else { todoBox.appendChild(li); todoCount++; }
    });

    if (urgentCount === 0) urgentBox.appendChild(el("div", "ksm-list-empty", "Nothing urgent."));
    if (todoCount === 0) todoBox.appendChild(el("div", "ksm-list-empty", "Nothing pending."));

    if (selectedTaskId != null) {
      var stillThere = lastTasks.find(function (t) { return t.id === selectedTaskId; });
      if (!stillThere) {
        selectedTaskId = null;
        renderDetail(null);
      } else if (JSON.stringify(stillThere) !== lastDetailSig) {
        renderDetail(stillThere); // only when its data changed, so scroll position and buttons survive
      }
    }
  }

  function highlightSelected() {
    document.querySelectorAll(".ksm-chip").forEach(function (c) {
      c.classList.toggle("selected", Number(c.dataset.id) === selectedTaskId);
    });
  }

  document.getElementById("ksm-add-task").addEventListener("click", function () {
    var title = document.getElementById("ksm-f-title").value.trim();
    var imp = document.getElementById("ksm-f-imp").value;
    var urg = document.getElementById("ksm-f-urg").value;
    var date = document.getElementById("ksm-f-date").value;
    var time = document.getElementById("ksm-f-time").value || "23:59:59";
    if (time.length === 5) time += ":00"; // HH:MM -> HH:MM:SS
    if (!title || !date) { showToast("Title and deadline date are required."); return; }
    api.add_task(title, imp, urg, date, time).then(function (res) {
      if (!res.ok) { showToast(res.error || "Could not add task."); return; }
      document.getElementById("ksm-f-title").value = "";
      refreshSnapshot();
    });
  });

  document.getElementById("ksm-apply-warp").addEventListener("click", function () {
    var d = document.getElementById("ksm-warp-d").value || "0";
    var h = document.getElementById("ksm-warp-h").value || "0";
    api.apply_warp(d, h).then(function (res) {
      if (!res.ok) showToast(res.error || "Invalid warp values.");
      refreshSnapshot();
    });
  });

  // default the date field to today (LOCAL date; toISOString() is UTC and can be yesterday in India)
  function localDateStr(d) {
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
  }
  document.getElementById("ksm-f-date").value = localDateStr(new Date());

  // ---- sync status chip (Phase 4F-3): Synced / Pending N / Offline / Off, from get_sync_status ------
  var SYNC_STATES = { synced: "Synced", pending: "Pending", offline: "Offline", off: "Off" };
  var syncBusy = false;
  function showSyncChip(state, label, tip) {
    var chip = document.getElementById("ksm-syncchip");
    if (!chip) return;
    if (!SYNC_STATES.hasOwnProperty(state)) { state = "off"; }
    chip.className = "ksm-syncchip " + state;
    chip.setAttribute("data-state", state);
    chip.textContent = label || SYNC_STATES[state];
    chip.title = tip || "";
  }
  function refreshSyncChip() {
    if (!api || syncBusy || typeof api.get_sync_status !== "function") return;
    syncBusy = true;
    api.get_sync_status().then(function (st) {
      syncBusy = false;
      if (!st || typeof st !== "object") { showSyncChip("offline", "Unknown", "The sync status could not be read."); return; }
      var tip = st.state === "off" ? "Nothing is sent online: every online-copy switch is off."
        : (st.last_sync ? "Last sync: " + st.last_sync : "Not synced yet in this run.");
      if (st.last_error) tip += "\n" + st.last_error;
      showSyncChip(String(st.state || "off"), String(st.label || ""), tip);
      showSyncLast(st);
    }, function () {
      syncBusy = false;
      showSyncChip("offline", "Unknown", "The sync status could not be read.");
      showSyncLast(null);
    });
  }

  // ---- mobile sync tab: last sync time, Sync now, Export cloud data (Phase 4F-4) -------------------
  var syncLastEl = document.getElementById("ksm-sync-last");
  var syncNowBtn = document.getElementById("ksm-sync-now");
  var syncExportBtn = document.getElementById("ksm-sync-export");
  var syncMsgEl = document.getElementById("ksm-sync-msg");
  function showSyncLast(st) {
    if (!syncLastEl) return;
    var t;
    if (!st || typeof st !== "object") t = "Last sync: unknown (the status could not be read)";
    else if (st.state === "off") t = "Last sync: nothing is sent online (every online-copy switch is off)";
    else t = st.last_sync ? "Last sync: " + st.last_sync : "Last sync: not yet in this run";
    syncLastEl.textContent = t;
  }
  function setSyncMsg(text, cls) {
    if (!syncMsgEl) return;
    syncMsgEl.textContent = text || "";
    syncMsgEl.className = cls || "";
  }
  function callSafely(name) {
    // a missing method, a rejected call or an odd answer all become one error object; never throws
    try {
      if (!api || typeof api[name] !== "function") return Promise.resolve({ ok: false, error: "The app did not answer." });
      return Promise.resolve(api[name]()).then(function (r) {
        return (r && typeof r === "object") ? r : { ok: false, error: "The app gave an unexpected answer." };
      }, function () { return { ok: false, error: "The app did not answer." }; });
    } catch (e) {
      return Promise.resolve({ ok: false, error: "The app did not answer." });
    }
  }
  if (syncNowBtn) syncNowBtn.addEventListener("click", function () {
    if (syncNowBtn.disabled) return;
    syncNowBtn.disabled = true;
    setSyncMsg("Starting sync...", "busy");
    callSafely("sync_now").then(function (res) {
      syncNowBtn.disabled = false;
      if (res.message) { setSyncMsg(String(res.message), "err"); }
      else if (res.ok === false) { setSyncMsg("Sync could not start. " + String(res.error || res.last_error || ""), "err"); }
      else {
        var kinds = Array.isArray(res.started) ? res.started.join(", ") : "";
        setSyncMsg("Sending started" + (kinds ? " (" + kinds + ")" : "") + ". The chip in the header shows the result.", "ok");
      }
      showSyncLast(res.state ? res : null);
      refreshSyncChip();
    });
  });
  if (syncExportBtn) syncExportBtn.addEventListener("click", function () {
    if (syncExportBtn.disabled) return;
    syncExportBtn.disabled = true;
    setSyncMsg("Exporting...", "busy");
    callSafely("export_cloud_data").then(function (res) {
      syncExportBtn.disabled = false;
      if (res.ok && res.path) {
        var c = res.counts && typeof res.counts === "object" ? res.counts : {};
        var parts = Object.keys(c).map(function (k) { return k + ": " + c[k]; });
        setSyncMsg("Saved to: " + String(res.path) + (parts.length ? "  (" + parts.join(", ") + ")" : ""), "ok");
      } else {
        setSyncMsg("Export failed. " + String(res.error || ""), "err");
      }
    });
  });

  // ---- clock + snapshot polling -----------------------------------------------
  function refreshSnapshot(force) {
    if (!api) return;
    api.get_snapshot().then(function (raw) {
      var data = JSON.parse(raw);
      document.getElementById("ksm-clock").textContent = data.now;
      document.getElementById("ksm-title").textContent = data.assistant_name.toUpperCase() + " — CENTRAL COMMAND";

      var banner = document.getElementById("ksm-banner");
      var warning = data.data_warning || "";
      if (banner.textContent !== warning) banner.textContent = warning;
      banner.hidden = !warning;
      var setTab = document.querySelector('#ksm-tabnav [data-tab="settings"]');
      if (setTab) setTab.classList.toggle("attention", !!data.legacy_offer);

      if (dragging && force !== true) return; // never rebuild the cards under the user's mouse
      var sig = JSON.stringify(data.tasks || []);
      var minuteKey = String(data.now || "").slice(0, 16);
      if (force === true || sig !== lastTasksSig || minuteKey !== lastMinuteKey) {
        lastTasksSig = sig;
        lastMinuteKey = minuteKey;
        renderMatrix(data);
      }
    });
  }

  // ---- chat tab ------------------------------------------------------------------
  var chatDisplay = document.getElementById("ksm-chat-display");
  var chatCount = 0;
  var chatSending = false;

  function scrollChatToEnd() { chatDisplay.scrollTop = chatDisplay.scrollHeight; }

  function appendChatMessage(msg) {
    var p = el("p", "ksm-msg " + msg.role, msg.text);
    chatDisplay.appendChild(p);
    scrollChatToEnd();
  }

  function pollChat() {
    if (!api) return;
    api.get_chat_updates(chatCount).then(function (raw) {
      var data = JSON.parse(raw);
      data.messages.forEach(appendChatMessage);
      chatCount = data.count;
      chatSending = data.busy;
      document.getElementById("ksm-chat-send").disabled = chatSending;
      document.getElementById("ksm-chat-entry").disabled = chatSending;
    });
  }

  function sendChat() {
    var input = document.getElementById("ksm-chat-entry");
    var text = input.value.trim();
    if (!text || chatSending) return;
    input.value = "";
    api.send_chat(text);
  }
  document.getElementById("ksm-chat-send").addEventListener("click", sendChat);
  document.getElementById("ksm-chat-entry").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") sendChat();
  });
  document.getElementById("ksm-clear-chat").addEventListener("click", function () {
    api.clear_chat();
  });

  // ---- strategy tab ----------------------------------------------------------------
  var auditDisplay = document.getElementById("ksm-audit-display");
  var runAuditBtn = document.getElementById("ksm-run-audit");
  var lastAuditText = "";
  runAuditBtn.addEventListener("click", function () {
    if (!api) return;
    api.start_audit().then(function (res) {
      if (!res.ok) {
        api.get_audit_status().then(function (raw) {
          var data = JSON.parse(raw);
          if (!data.available) {
            auditDisplay.textContent = "GEMINI_API_KEY is not configured — set it in your .env file to enable audits.";
          } else if (data.running) {
            auditDisplay.textContent = "An audit is already running, please wait...";
          }
        });
        return;
      }
      runAuditBtn.disabled = true;
      runAuditBtn.textContent = "ANALYZING...";
      auditDisplay.textContent = "[SYSTEM]: Reading current matrix data...";
    });
  });
  function pollAudit() {
    if (!api) return;
    api.get_audit_status().then(function (raw) {
      var data = JSON.parse(raw);
      if (!data.available && !auditDisplay.textContent) {
        auditDisplay.textContent = "GEMINI_API_KEY is not configured — set it in your .env file to enable audits.";
      }
      if (data.text && data.text !== lastAuditText) {
        lastAuditText = data.text;
        auditDisplay.textContent = "--- AUDIT RESULT ---\n\n" + data.text;
      }
      runAuditBtn.disabled = data.running || !data.available;
      runAuditBtn.textContent = data.running ? "ANALYZING..." : "RUN FULL MATRIX AUDIT";
    });
  }

  // ---- mobile sync tab ---------------------------------------------------------------
  var bridgeLogEl = document.getElementById("ksm-bridge-log");
  var bridgeCount = 0;
  function pollBridge() {
    if (!api) return;
    api.get_bridge_log(bridgeCount).then(function (raw) {
      var data = JSON.parse(raw);
      var dot = document.getElementById("ksm-bridge-dot");
      var text = document.getElementById("ksm-bridge-text");
      dot.className = "ksm-dot " + (data.enabled ? "ok" : "bad");
      text.textContent = data.enabled
        ? "Firebase sync enabled"
        : "Firebase sync disabled (turn it on in the SETTINGS tab)";
      data.entries.forEach(function (line) { bridgeLogEl.appendChild(el("div", null, line)); });
      if (data.entries.length) bridgeLogEl.scrollTop = bridgeLogEl.scrollHeight;
      bridgeCount = data.count;
    });
  }

  // ---- screen scan tab ---------------------------------------------------------------
  var scanBtn = document.getElementById("ksm-scan-btn");
  var scanStatus = document.getElementById("ksm-scan-status");
  var scanResult = document.getElementById("ksm-scan-result");
  var scanPreview = document.getElementById("ksm-scan-preview");
  var scanMeta = document.getElementById("ksm-scan-meta");
  var seenScanId = 0;
  var scanWasRunning = false;

  function setScanStatus(text, cls) {
    scanStatus.textContent = text;
    scanStatus.className = cls || "";
  }

  scanBtn.addEventListener("click", function () {
    if (!api) return;
    var hide = document.getElementById("ksm-scan-hide").checked;
    var q = document.getElementById("ksm-scan-question").value;
    var mon = document.getElementById("ksm-scan-monitor").value;
    scanBtn.disabled = true;
    setScanStatus("Starting scan...", "busy");
    api.start_screen_scan(hide, q, mon).then(function (res) {
      if (!res.ok) {
        setScanStatus(res.error || "Could not start scan.", "err");
        scanBtn.disabled = false;
      }
    });
  });

  function pollScreen() {
    if (!api) return;
    api.get_screen_status(seenScanId).then(function (raw) {
      var d = JSON.parse(raw);
      if (d.running) {
        scanWasRunning = true;
        setScanStatus("Working: " + d.stage + "...", "busy");
      } else if (!d.available && !scanWasRunning) {
        setScanStatus("Screen Scan unavailable: " + (d.problem || "unknown reason"), "err");
      }
      if (d.fresh) {
        seenScanId = d.scan_id;
        if (d.error) {
          setScanStatus("Scan failed: " + d.error, "err");
        } else {
          scanResult.textContent = d.text;
          if (d.preview) {
            scanPreview.src = "data:image/jpeg;base64," + d.preview;
            scanPreview.style.display = "block";
          }
          var m = d.meta || {};
          scanMeta.textContent = "Model: " + m.model + "\nScreen: " + m.screen + "  ->  sent " + m.sent +
            " (" + m.kb + " KB)\nCapture: " + m.capture_s + "s | AI: " + m.api_s + "s | Total: " + m.total_s + "s";
          setScanStatus("Done in " + m.total_s + "s.", "");
        }
      } else if (!d.running && scanWasRunning) {
        scanWasRunning = false;
      }
      scanBtn.disabled = d.running || !d.available;
    });
  }

  // ---- debug log tab -------------------------------------------------------------------
  var debugEl = document.getElementById("ksm-debug-log");
  var debugCount = 0;
  var debugLines = [];
  var LEVEL_RANK = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

  function debugVisible(line) {
    var min = LEVEL_RANK[document.getElementById("ksm-debug-level").value] || 0;
    return (LEVEL_RANK[line.level] || 0) >= min;
  }
  function debugAppend(line) {
    var row = el("div", "lv-" + line.level, line.text);
    debugEl.appendChild(row);
  }
  function debugRerender() {
    debugEl.innerHTML = "";
    debugLines.filter(debugVisible).forEach(debugAppend);
    debugEl.scrollTop = debugEl.scrollHeight;
  }
  document.getElementById("ksm-debug-level").addEventListener("change", debugRerender);
  document.getElementById("ksm-debug-clear").addEventListener("click", function () {
    debugLines = [];
    debugEl.innerHTML = "";
    api && api.clear_debug_log();
  });
  function pollDebug() {
    if (!api) return;
    api.get_debug_log(debugCount).then(function (raw) {
      var d = JSON.parse(raw);
      document.getElementById("ksm-debug-file").textContent = "Log file: " + d.file;
      if (d.entries.length) {
        d.entries.forEach(function (line) {
          debugLines.push(line);
          if (debugVisible(line)) debugAppend(line);
        });
        if (debugLines.length > 3000) { debugLines = debugLines.slice(-2000); debugRerender(); }
        if (document.getElementById("ksm-debug-follow").checked) debugEl.scrollTop = debugEl.scrollHeight;
      }
      debugCount = d.count;
    });
  }

  // ---- settings tab --------------------------------------------------------------------
  var setEls = {
    groups: document.getElementById("ksm-set-groups"),
    storage: document.getElementById("ksm-set-storage"),
    status: document.getElementById("ksm-set-status"),
    legacy: document.getElementById("ksm-set-legacy"),
    restart: document.getElementById("ksm-set-restart"),
    search: document.getElementById("ksm-set-search"),
    importCard: document.getElementById("ksm-set-import"),
    importText: document.getElementById("ksm-set-import-text"),
    importOver: document.getElementById("ksm-set-import-over"),
    importResult: document.getElementById("ksm-set-import-result"),
    history: document.getElementById("ksm-set-history"),
    shared: document.getElementById("ksm-set-shared")
  };
  var settingsData = null;     // last answer of settings_get
  var drafts = {};             // key -> text typed in an open editor (survives re-draw)
  var editing = {};            // key -> true while its editor is open
  var fieldErrors = {};        // key -> last validation error
  var testResults = {};        // service -> {running, result}
  var testTimer = null;
  var fbPasteOpen = false;
  var confirmKey = null;       // key waiting for its second "Confirm?" click
  var confirmTimer = null;

  function setStatus(text, cls) {
    setEls.status.textContent = text || "";
    setEls.status.className = cls || "";
  }

  var SCOPE_LABEL = {
    common: ["COMMON", "One value for every computer and app. Kept in your Firebase when 'Keep settings online' is on."],
    per_device: ["THIS DEVICE", "A value for this computer only. Kept in your Firebase under this device, so you can see it elsewhere."],
    local_only: ["LOCAL ONLY", "Stays on this computer. It is needed before the app can reach Firebase, so it is never stored there."],
    secret: ["SECRET", "An API key. Never stored in Firebase."]
  };
  var SOURCE_LABEL = { store: "SAVED", env: "ENVIRONMENT", dotenv: ".ENV FILE", "default": "DEFAULT" };

  function loadSettings() {
    if (!api) return Promise.resolve();
    return api.settings_get().then(function (raw) {
      settingsRaw = raw;
      settingsData = JSON.parse(raw);
      renderSettings();
    });
  }

  // Values can change while the page is open (another computer, or Firebase catching up after being offline).
  // Re-read every few seconds, but only while the SETTINGS tab shows, no field is open for editing, and something changed.
  var settingsRaw = "";
  function refreshSettingsLive() {
    if (!api || activeTab !== "settings" || settingsBusy()) return;
    api.settings_get().then(function (raw) {
      if (raw === settingsRaw || activeTab !== "settings" || settingsBusy()) return;
      settingsRaw = raw;
      settingsData = JSON.parse(raw);
      renderSettings();
    }).catch(function () {});
  }
  function settingsBusy() {
    return Object.keys(editing).length > 0 || Object.keys(drafts).length > 0;
  }

  function renderStorage(d) {
    setEls.storage.innerHTML = "";
    var k = el("span", "ksm-chip2 " + (d.backend_secure ? "ok" : "warn"));
    k.appendChild(el("span", null, d.backend_secure ? "Keys stored in: " + d.backend : "Keys stored in a protected file"));
    k.title = d.backend_secure
      ? "Your API keys are kept by the operating system, not in a file."
      : "No OS keychain was found, so keys are kept in a file only you can read. Install the 'keyring' package for better protection.";
    setEls.storage.appendChild(k);
    var f = el("span", "ksm-chip2");
    f.appendChild(el("span", null, "Folder:"));
    f.appendChild(el("span", "path", d.folder));
    f.title = d.folder;
    setEls.storage.appendChild(f);
    if (d.load_problem) {
      setEls.storage.appendChild(el("span", "ksm-chip2 warn", d.load_problem));
    }
  }

  function renderNotices(d) {
    var lg = d.legacy || {};
    var offer = !lg.dismissed && ((lg.dotenv_keys || []).length || lg.firebase_file);
    setEls.legacy.hidden = !offer;
    setEls.legacy.innerHTML = "";
    if (offer) {
      var parts = [];
      if ((lg.dotenv_keys || []).length) parts.push(lg.dotenv_keys.length + " value(s) from your old .env file");
      if (lg.firebase_file) parts.push("your serviceAccountKey.json");
      setEls.legacy.appendChild(el("span", "grow", "Found " + parts.join(" and ") +
        " in the project folder. Import them once into the common settings folder, and you will never need to copy them again."));
      var go = el("button", "ksm-btn", "Import now");
      go.addEventListener("click", function () {
        api.settings_import_legacy().then(function (r) {
          setStatus("Imported " + r.imported.length + " item(s)" + (r.problems.length ? "; problems: " + r.problems.join(" | ") : "") + ".", r.problems.length ? "warn" : "ok");
          loadSettings();
        });
      });
      var no = el("button", "ksm-btn ghost", "Not now");
      no.addEventListener("click", function () { api.settings_dismiss_legacy().then(loadSettings); });
      setEls.legacy.appendChild(go);
      setEls.legacy.appendChild(no);
    }
    var rp = d.restart_pending || [];
    setEls.restart.hidden = !rp.length;
    setEls.restart.textContent = rp.length ? "Restart the app to apply: " + rp.join(", ") + "." : "";
  }

  function entryDisplay(e) {
    if (e.kind === "secret") return e.is_set ? e.shown : "";
    if (e.kind === "bool") return e.value === "true" ? "On" : "Off";
    return e.value;
  }

  function makeBadge(e) {
    var cls, txt;
    if (!e.is_set && e.kind === "secret") { cls = "unset"; txt = "NOT SET"; }
    else { cls = e.source; txt = SOURCE_LABEL[e.source] || e.source.toUpperCase(); }
    var b = el("span", "ksm-badge " + cls, txt);
    if (e.source === "env") b.title = "Set by a real environment variable. It wins over anything saved here.";
    if (e.source === "dotenv") b.title = "Read from the old .env file. Save it here to move it to the common folder.";
    return b;
  }

  function saveSetting(e, value, done) {
    api.settings_set(e.key, value).then(function (res) {
      if (!res.ok) {
        fieldErrors[e.key] = res.error || "Could not save.";
        setStatus(e.label + ": " + fieldErrors[e.key], "err");
        renderSettings();
        return;
      }
      delete fieldErrors[e.key]; delete drafts[e.key]; delete editing[e.key];
      var msg = e.label + " saved" + (res.restart ? " (restart needed to apply)." : " and applied.");
      if (res.conflict) msg += " " + res.conflict;
      setStatus(res.warning ? msg + " " + res.warning : msg, res.warning || res.conflict ? "warn" : "ok");
      if (done) done();
      loadSettings();
    });
  }

  function askDelete(e, btn) {
    if (confirmKey !== e.key) {
      confirmKey = e.key;
      btn.textContent = "Confirm?";
      btn.classList.add("danger");
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(function () { confirmKey = null; renderSettings(); }, 3500);
      return;
    }
    confirmKey = null;
    clearTimeout(confirmTimer);
    api.settings_delete(e.key).then(function (res) {
      if (!res.ok) { setStatus(res.error || "Could not delete.", "err"); return; }
      delete drafts[e.key]; delete editing[e.key]; delete fieldErrors[e.key];
      setStatus(e.label + " removed from saved settings" + (e.kind === "secret" ? "." : "; the default applies."), "ok");
      loadSettings();
    });
  }

  function buildValueCell(e) {
    var cell = el("div", "ksm-set-value");
    var btns = el("div", "ksm-set-btns");

    if (e.kind === "bool") {
      var sw = el("label", "ksm-switch");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = e.value === "true";
      cb.setAttribute("aria-label", e.label);
      var track = el("span", "track");
      sw.appendChild(cb); sw.appendChild(track);
      sw.appendChild(el("span", null, cb.checked ? "On" : "Off"));
      cb.addEventListener("change", function () { saveSetting(e, cb.checked ? "true" : "false"); });
      cell.appendChild(sw);
      cell.appendChild(makeBadge(e));
    } else if (e.kind === "choice") {
      var sel = document.createElement("select");
      sel.setAttribute("aria-label", e.label);
      e.choices.forEach(function (c) {
        var o = document.createElement("option");
        o.value = c; o.textContent = c; o.selected = c === e.value;
        sel.appendChild(o);
      });
      sel.addEventListener("change", function () { saveSetting(e, sel.value); });
      cell.appendChild(sel);
      cell.appendChild(makeBadge(e));
    } else if (e.key === "FIREBASE_SERVICE_ACCOUNT_PATH") {
      var fb = settingsData.firebase_file;
      cell.appendChild(el("span", fb ? "val" : "val empty", fb ? (fb.in_store ? "Imported file in the config folder" : fb.path) : "No file imported"));
      cell.appendChild(makeBadge(e));
    } else if (editing[e.key]) {
      var inp = document.createElement("input");
      inp.type = e.kind === "secret" ? "password" : "text";
      inp.value = drafts[e.key] !== undefined ? drafts[e.key] : (e.kind === "secret" ? "" : e.value);
      inp.autocomplete = "off"; inp.spellcheck = false;
      inp.placeholder = e.kind === "secret" ? (e.placeholder || "Paste new key") : (e.placeholder || e.default || "");
      inp.setAttribute("aria-label", e.label);
      if (fieldErrors[e.key]) inp.classList.add("bad");
      inp.addEventListener("input", function () { drafts[e.key] = inp.value; });
      inp.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { ev.preventDefault(); saveSetting(e, inp.value); }
        if (ev.key === "Escape") { delete editing[e.key]; delete drafts[e.key]; delete fieldErrors[e.key]; renderSettings(); }
      });
      cell.appendChild(inp);
      if (e.kind === "secret") {
        var eye = el("button", "ksm-btn ghost", "Show");
        eye.type = "button";
        eye.addEventListener("click", function () {
          inp.type = inp.type === "password" ? "text" : "password";
          eye.textContent = inp.type === "password" ? "Show" : "Hide";
        });
        btns.appendChild(eye);
        if (e.stored) {
          var rev = el("button", "ksm-btn ghost", "Reveal current");
          rev.type = "button";
          rev.title = "Fills the box with the saved key. This is written to the settings history.";
          rev.addEventListener("click", function () {
            api.settings_reveal(e.key).then(function (r) {
              if (r.ok) { inp.value = r.value; drafts[e.key] = r.value; inp.type = "text"; eye.textContent = "Hide"; }
              else setStatus(r.error || "Could not reveal.", "err");
            });
          });
          btns.appendChild(rev);
        }
      }
      var save = el("button", "ksm-btn", "Save");
      save.type = "button";
      save.addEventListener("click", function () { saveSetting(e, inp.value); });
      var cancel = el("button", "ksm-btn ghost", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", function () { delete editing[e.key]; delete drafts[e.key]; delete fieldErrors[e.key]; renderSettings(); });
      btns.appendChild(save); btns.appendChild(cancel);
      if (fieldErrors[e.key]) cell.appendChild(el("span", "ksm-set-err", fieldErrors[e.key]));
      setTimeout(function () { try { inp.focus(); } catch (x) {} }, 0);
    } else {
      var shown = entryDisplay(e);
      cell.appendChild(el("span", shown ? "val" : "val empty", shown || "Not set"));
      cell.appendChild(makeBadge(e));
      if (e.restart) cell.appendChild(el("span", "ksm-badge restart", "RESTART"));
      if (e.conflict) {
        var cf = el("span", "ksm-set-err", "Overridden by an environment variable");
        cf.title = e.conflict;
        cell.appendChild(cf);
      }
    }

    // buttons that are not part of an open editor
    if (!editing[e.key] && e.kind !== "bool" && e.kind !== "choice" && e.key !== "FIREBASE_SERVICE_ACCOUNT_PATH") {
      var edit = el("button", "ksm-btn ghost", e.kind === "secret" && !e.is_set ? "Add" : "Edit");
      edit.type = "button";
      edit.addEventListener("click", function () { editing[e.key] = true; renderSettings(); });
      btns.appendChild(edit);
    }
    if (!editing[e.key] && e.stored && e.key !== "FIREBASE_SERVICE_ACCOUNT_PATH") {
      var del = el("button", "ksm-btn ghost" + (confirmKey === e.key ? " danger" : ""), confirmKey === e.key ? "Confirm?" : (e.kind === "secret" ? "Delete" : "Reset"));
      del.type = "button";
      del.title = e.kind === "secret" ? "Remove the saved key" : "Go back to the default";
      del.addEventListener("click", function () { askDelete(e, del); });
      btns.appendChild(del);
    }
    return { cell: cell, btns: btns };
  }

  var SH_STATE = { off: "Settings are not kept online.", no_firebase: "No Firebase set up: settings stay on this computer.",
    starting: "Connecting...", synced: "Settings are in step with Firebase.", pending: "Waiting to send changes.",
    offline: "Offline: changes wait on this computer." };
  function renderShared(sh) {
    var box = setEls.shared;
    box.innerHTML = "";
    if (!sh || !sh.ok) { box.hidden = true; return; }
    box.hidden = false;
    box.appendChild(el("div", "ksm-panel-title", "SHARED SETTINGS"));
    var st = (sh.settings && sh.settings.state) || "";
    box.appendChild(el("div", "line", (SH_STATE[st] || "") + (sh.settings && sh.settings.pending ? " (" + sh.settings.pending + " waiting)" : "")));
    if (sh.device && (sh.device.name || sh.device.device_id)) {
      box.appendChild(el("div", "line", "This device: " + (sh.device.name || "unnamed") + " (" + sh.device.device_id + "). Settings marked THIS DEVICE belong to it."));
    }
    (sh.notices || []).forEach(function (n) {
      var d = settingsData && settingsData.entries.filter(function (x) { return x.key === n.key; })[0];
      box.appendChild(el("div", "notice", "Your change to " + (d ? d.label : n.key) + " (\"" + n.mine + "\") lost to a newer value from another computer (\"" + n.theirs + "\"). The newer value is now used."));
    });
    if ((sh.notices || []).length) {
      var dis = el("button", "ksm-btn ghost", "Dismiss");
      dis.type = "button";
      dis.addEventListener("click", function () { callApi("config_dismiss_notices").then(loadSettings); });
      box.appendChild(dis);
    }
  }

  function buildRow(e) {
    var row = el("div", "ksm-set-row");
    row.dataset.key = e.key;
    row.dataset.search = (e.label + " " + e.key + " " + (e.help || "")).toLowerCase();
    var lab = el("div", "ksm-set-label");
    lab.appendChild(el("div", "n", e.label));
    lab.appendChild(el("div", "k", e.key));
    if (SCOPE_LABEL[e.scope]) {
      var sc = el("div", "scope");
      var sb = el("span", "ksm-badge " + e.scope, SCOPE_LABEL[e.scope][0]);
      sb.title = SCOPE_LABEL[e.scope][1];
      sc.appendChild(sb);
      if (settingsData && settingsData.shared && (settingsData.shared.overridden || []).indexOf(e.key) >= 0) {
        var ov = el("span", "ksm-badge env", "SET BY ENVIRONMENT");
        ov.title = "A real environment variable wins on this computer. This value is not sent to Firebase.";
        sc.appendChild(ov);
      }
      lab.appendChild(sc);
    }
    if (e.help) lab.appendChild(el("div", "h", e.help));
    if (e.link) {
      var h = el("div", "h");
      h.appendChild(document.createTextNode("Get a key: "));
      h.appendChild(el("span", null, e.link));
      lab.appendChild(h);
    }
    row.appendChild(lab);
    var parts = buildValueCell(e);
    row.appendChild(parts.cell);
    row.appendChild(parts.btns);
    return row;
  }

  function collectOverrides(service) {
    var o = {};
    (settingsData.entries || []).forEach(function (e) {
      if (e.service === service && drafts[e.key] && String(drafts[e.key]).trim()) o[e.key] = drafts[e.key];
    });
    return o;
  }

  function testLine(service) {
    var t = testResults[service];
    var line = el("div", "ksm-set-testline");
    line.dataset.service = service;
    if (!t) { line.hidden = true; return line; }
    if (t.running) {
      line.className += " busy";
      line.textContent = "Testing...";
      return line;
    }
    var r = t.result || {};
    line.className += " " + (r.status || "fail");
    var icon = { pass: "\u2714 ", fail: "\u2716 ", warn: "\u26A0 ", skip: "\u2022 " }[r.status] || "";
    line.textContent = icon + (r.message || "") + (r.ms ? "  (" + r.ms + " ms)" : "");
    if (r.detail) line.appendChild(el("span", "detail", r.detail));
    return line;
  }

  function pollTests() {
    api.settings_test_status().then(function (raw) {
      var all = JSON.parse(raw), anyRunning = false;
      Object.keys(all).forEach(function (svc) {
        testResults[svc] = all[svc];
        if (all[svc].running) anyRunning = true;
        var line = setEls.groups.querySelector('.ksm-set-testline[data-service="' + svc + '"]');
        if (line) {
          var fresh = testLine(svc);
          line.className = fresh.className; line.hidden = fresh.hidden;
          line.textContent = fresh.textContent;
          if (fresh.firstElementChild) line.appendChild(fresh.firstElementChild);
        }
      });
      if (anyRunning) testTimer = setTimeout(pollTests, 500);
      else {
        testTimer = null;
        document.querySelectorAll(".ksm-test-btn").forEach(function (b) { b.disabled = false; b.textContent = "Test connection"; });
      }
    });
  }

  function startTest(service, btn) {
    btn.disabled = true; btn.textContent = "Testing...";
    testResults[service] = { running: true };
    var line = setEls.groups.querySelector('.ksm-set-testline[data-service="' + service + '"]');
    if (line) { line.hidden = false; line.className = "ksm-set-testline busy"; line.textContent = "Testing..."; }
    api.settings_test(service, JSON.stringify(collectOverrides(service))).then(function (res) {
      if (!res.ok) {
        testResults[service] = { running: false, result: { status: "fail", message: res.error || "Could not start the test." } };
        renderSettings();
        return;
      }
      if (!testTimer) testTimer = setTimeout(pollTests, 300);
    });
  }

  function buildFirebaseBlock() {
    var fb = settingsData.firebase_file;
    var box = el("div", "ksm-set-fb");
    var meta = el("div", "meta");
    if (fb && !fb.unreadable) {
      meta.appendChild(document.createTextNode("Project: "));
      meta.appendChild(el("b", null, fb.project_id || "?"));
      meta.appendChild(document.createTextNode("   Account: "));
      meta.appendChild(el("b", null, fb.client_email || "?"));
      meta.appendChild(el("div", null, "File: " + fb.path + (fb.in_store ? "" : "   (still in the project folder: import it to move it)")));
    } else {
      meta.textContent = fb && fb.unreadable ? "The service account file could not be read." : "No service account file yet. Download one from Firebase console > Project settings > Service accounts.";
    }
    box.appendChild(meta);
    var acts = el("div", "ksm-set-row-actions");
    var imp = el("button", "ksm-btn", "Import file...");
    imp.addEventListener("click", function () {
      api.settings_pick_firebase_file().then(function (r) {
        if (r.cancelled) return;
        if (!r.ok) { setStatus(r.error || "Import failed.", "err"); return; }
        setStatus("Service account for project '" + r.project_id + "' imported and copied into the config folder.", "ok");
        loadSettings();
      });
    });
    var paste = el("button", "ksm-btn ghost", fbPasteOpen ? "Hide paste box" : "Paste JSON...");
    paste.addEventListener("click", function () { fbPasteOpen = !fbPasteOpen; renderSettings(); });
    acts.appendChild(imp); acts.appendChild(paste);
    if (fb && fb.in_store) {
      var rm = el("button", "ksm-btn ghost" + (confirmKey === "FB_FILE" ? " danger" : ""), confirmKey === "FB_FILE" ? "Confirm?" : "Remove file");
      rm.addEventListener("click", function () {
        if (confirmKey !== "FB_FILE") {
          confirmKey = "FB_FILE"; clearTimeout(confirmTimer);
          confirmTimer = setTimeout(function () { confirmKey = null; renderSettings(); }, 3500);
          renderSettings(); return;
        }
        confirmKey = null;
        api.settings_remove_firebase_file().then(function (r) {
          setStatus(r.ok ? "Service account file removed." : (r.error || "Could not remove it."), r.ok ? "ok" : "err");
          loadSettings();
        });
      });
      acts.appendChild(rm);
    }
    box.appendChild(acts);
    if (fbPasteOpen) {
      var pb = el("div", null); pb.id = "ksm-set-fb-paste";
      var ta = document.createElement("textarea");
      ta.rows = 6; ta.spellcheck = false; ta.id = "ksm-set-fb-text";
      ta.placeholder = "Paste the whole contents of serviceAccountKey.json here";
      pb.appendChild(ta);
      var sv = el("button", "ksm-btn", "Save key");
      sv.style.marginTop = "8px";
      sv.addEventListener("click", function () {
        api.settings_import_firebase_text(ta.value).then(function (r) {
          if (!r.ok) { setStatus(r.error || "That is not a valid key file.", "err"); return; }
          fbPasteOpen = false;
          setStatus("Service account for project '" + r.project_id + "' saved.", "ok");
          loadSettings();
        });
      });
      pb.appendChild(sv);
      box.appendChild(pb);
    }
    return box;
  }

  function renderSettings() {
    if (!settingsData) return;
    var d = settingsData;
    renderStorage(d);
    renderNotices(d);
    renderShared(d.shared);
    var y = setEls.groups.parentElement.scrollTop;
    setEls.groups.innerHTML = "";
    d.groups.forEach(function (g) {
      var entries = d.entries.filter(function (e) { return e.group === g.id; });
      if (!entries.length) return;
      var box = el("div", "ksm-set-group");
      box.dataset.group = g.id;
      var head = el("div", "ksm-set-ghead");
      head.appendChild(el("span", "t", g.title.toUpperCase()));
      head.appendChild(el("span", "b", g.blurb));
      var svc = entries[0].service || "";
      if (svc) {
        var tb = el("button", "ksm-btn ksm-test-btn", "Test connection");
        tb.type = "button";
        tb.title = "Checks the saved values, or the unsaved ones you just typed. Sends no prompt and uses no tokens.";
        if (testResults[svc] && testResults[svc].running) { tb.disabled = true; tb.textContent = "Testing..."; }
        tb.addEventListener("click", function () { startTest(svc, tb); });
        head.appendChild(tb);
      }
      box.appendChild(head);
      if (svc) box.appendChild(testLine(svc));
      entries.forEach(function (e) { box.appendChild(buildRow(e)); });
      if (g.id === "firebase") box.appendChild(buildFirebaseBlock());
      setEls.groups.appendChild(box);
    });
    applySettingsFilter();
    setEls.groups.parentElement.scrollTop = y;
  }

  function applySettingsFilter() {
    var q = (setEls.search.value || "").trim().toLowerCase();
    Array.prototype.forEach.call(setEls.groups.querySelectorAll(".ksm-set-group"), function (g) {
      var any = false;
      Array.prototype.forEach.call(g.querySelectorAll(".ksm-set-row"), function (r) {
        var hit = !q || r.dataset.search.indexOf(q) !== -1;
        r.hidden = !hit;
        if (hit) any = true;
      });
      g.hidden = !any;
    });
  }
  setEls.search.addEventListener("input", applySettingsFilter);

  // toolbar buttons
  document.getElementById("ksm-set-import-btn").addEventListener("click", function () {
    setEls.importCard.hidden = !setEls.importCard.hidden;
    setEls.importResult.textContent = "";
  });
  document.getElementById("ksm-set-import-close").addEventListener("click", function () { setEls.importCard.hidden = true; });
  function showImportResult(r) {
    if (!r.ok) { setEls.importResult.textContent = r.error || "Import failed."; setStatus(r.error || "Import failed.", "err"); return; }
    var t = "Imported: " + (r.imported.join(", ") || "nothing") + "\nSkipped (already saved): " + (r.skipped.join(", ") || "none") +
      (r.problems.length ? "\nProblems:\n  " + r.problems.join("\n  ") : "");
    setEls.importResult.textContent = t;
    setStatus("Import finished: " + r.imported.length + " imported.", r.problems.length ? "warn" : "ok");
    setEls.importText.value = "";
    loadSettings();
  }
  document.getElementById("ksm-set-import-go").addEventListener("click", function () {
    api.settings_import_env_text(setEls.importText.value, setEls.importOver.checked).then(showImportResult);
  });
  document.getElementById("ksm-set-import-file").addEventListener("click", function () {
    api.settings_pick_env_file(setEls.importOver.checked).then(function (r) { if (!r.cancelled) showImportResult(r); });
  });
  document.getElementById("ksm-set-backup-btn").addEventListener("click", function () {
    api.settings_save_backup().then(function (r) {
      if (r.cancelled) return;
      setStatus(r.ok ? "Backup saved to " + r.path + ". API keys are not included." : (r.error || "Backup failed."), r.ok ? "ok" : "err");
    });
  });
  document.getElementById("ksm-set-restore-btn").addEventListener("click", function () {
    api.settings_pick_restore().then(function (r) {
      if (r.cancelled) return;
      setStatus(r.ok ? "Restored " + r.imported.length + " setting(s) from the backup." : (r.error || "Restore failed."), r.ok ? "ok" : "err");
      loadSettings();
    });
  });
  document.getElementById("ksm-set-folder-btn").addEventListener("click", function () {
    api.settings_open_folder().then(function (r) { setStatus(r.ok ? "Opened " + r.path : (r.error || "Could not open the folder."), r.ok ? "ok" : "err"); });
  });
  document.getElementById("ksm-set-history-btn").addEventListener("click", function () {
    if (!setEls.history.hidden) { setEls.history.hidden = true; return; }
    api.settings_history().then(function (raw) {
      var lines = JSON.parse(raw).lines;
      setEls.history.textContent = lines.length ? lines.join("\n") : "No changes recorded yet. Values are never written here.";
      setEls.history.hidden = false;
    });
  });

  // ---- PROMPTS tab (Phase 5E) -------------------------------------------------------------------------------------
  // Every call goes through callApi: it never throws and never rejects, so a failed or missing call is a message, not a crash.
  function callApi(name, args) {
    try {
      if (!api || typeof api[name] !== "function") return Promise.resolve({ ok: false, message: "The app did not answer." });
      return Promise.resolve(api[name].apply(api, args || [])).then(function (r) {
        return (r && typeof r === "object") ? r : { ok: false, message: "The app gave an odd answer." };
      }, function () { return { ok: false, message: "The app did not answer." }; });
    } catch (e) { return Promise.resolve({ ok: false, message: "The app did not answer." }); }
  }

  var prEls = {
    roles: document.getElementById("ksm-pr-roles"), status: document.getElementById("ksm-pr-status"),
    notes: document.getElementById("ksm-pr-notes"), msg: document.getElementById("ksm-pr-msg"),
    list: document.getElementById("ksm-pr-list"), editor: document.getElementById("ksm-pr-editor"),
    head: document.getElementById("ksm-pr-head"), conflict: document.getElementById("ksm-pr-conflict"),
    blocks: document.getElementById("ksm-pr-blocks"), problems: document.getElementById("ksm-pr-problems"),
    save: document.getElementById("ksm-pr-save"), saveAs: document.getElementById("ksm-pr-saveas"),
    newName: document.getElementById("ksm-pr-newname"), addBlock: document.getElementById("ksm-pr-addblock"),
    previewBtn: document.getElementById("ksm-pr-previewbtn"), historyBtn: document.getElementById("ksm-pr-historybtn"),
    active: document.getElementById("ksm-pr-active"), reset: document.getElementById("ksm-pr-reset"),
    del: document.getElementById("ksm-pr-delete"), preview: document.getElementById("ksm-pr-preview"),
    history: document.getElementById("ksm-pr-history")
  };
  var P = { data: null, role: "chat", id: "", detail: null, edits: {}, added: [], confirm: "", confirmTimer: null,
            vTimer: null, vSeq: 0, errors: [], warnings: [], busy: false };
  var PR_STATE = { off: ["Prompts are not kept online.", ""], no_firebase: ["No Firebase set up: this computer's copy is used.", ""],
    starting: ["Connecting...", ""], synced: ["Synced with Firebase.", "ok"], pending: ["Waiting to send changes.", "warn"],
    offline: ["Offline: changes wait on this computer.", "warn"], conflict: ["A prompt was changed on another computer.", "warn"] };

  function prMsg(text, cls) { prEls.msg.textContent = text || ""; prEls.msg.className = cls || ""; }
  function prLimits() { return (P.data && P.data.limits) || { block: 4000, total: 20000, soft: 6000 }; }
  function prDirty() { return Object.keys(P.edits).length > 0; }

  function prChanges() {
    var out = {};
    Object.keys(P.edits).forEach(function (id) {
      out[id] = P.edits[id] === null ? null : Object.assign({}, P.edits[id]);
    });
    return out;
  }

  function prSetEdit(id, patch) {
    P.edits[id] = Object.assign({}, P.edits[id] || {}, patch);
    prUpdateButtons();
    prScheduleCheck();
  }

  function prScheduleCheck() {
    if (P.vTimer) clearTimeout(P.vTimer);
    var seq = ++P.vSeq;
    P.vTimer = setTimeout(function () {
      if (!P.id || !prDirty()) { P.errors = []; P.warnings = []; prRenderProblems(); prUpdateButtons(); return; }
      callApi("prompt_validate", [P.id, JSON.stringify(prChanges())]).then(function (r) {
        if (seq !== P.vSeq) return;
        if (Array.isArray(r.errors)) { P.errors = r.errors; P.warnings = r.warnings || []; }
        else { P.errors = []; P.warnings = [r.message || "Could not check the text just now."]; }
        prRenderProblems(); prUpdateButtons();
      });
    }, 300);
  }

  function prRenderProblems() {
    prEls.problems.innerHTML = "";
    P.errors.forEach(function (t) { prEls.problems.appendChild(el("div", "e", "Not allowed: " + t)); });
    P.warnings.forEach(function (t) { prEls.problems.appendChild(el("div", "w", "Note: " + t)); });
  }

  function prConfirmStep(key, btn, label, run) {
    if (P.confirm === key) { P.confirm = ""; clearTimeout(P.confirmTimer); prUpdateButtons(); run(); return; }
    P.confirm = key;
    clearTimeout(P.confirmTimer);
    P.confirmTimer = setTimeout(function () { P.confirm = ""; prUpdateButtons(); prRenderHistoryKeep(); }, 3500);
    prUpdateButtons();
    prRenderHistoryKeep();
  }

  function prUpdateButtons() {
    var d = P.detail, has = !!(d && d.ok);
    var dirty = prDirty();
    prEls.save.disabled = !has || !dirty || P.errors.length > 0 || P.busy;
    prEls.saveAs.disabled = !has || P.busy;
    prEls.addBlock.disabled = !has || P.busy;
    prEls.previewBtn.disabled = !has || P.busy;
    prEls.historyBtn.disabled = !has;
    prEls.active.disabled = !has || d.active || P.busy;
    prEls.active.textContent = has && d.active ? "In use" : "Set in use";
    prEls.reset.disabled = !has || P.busy;
    prEls.reset.textContent = P.confirm === "reset" ? "Confirm reset?" : "Reset prompt";
    prEls.reset.className = "ksm-btn ghost" + (P.confirm === "reset" ? " danger" : "");
    var canDelete = has && !d.builtin && !d.active;
    prEls.del.disabled = !canDelete || P.busy;
    prEls.del.title = has && d.builtin ? "The four built-in prompts cannot be deleted." : (has && d.active ? "Choose another prompt for this role first." : "");
    prEls.del.textContent = P.confirm === "delete" ? "Confirm delete?" : "Delete";
    prEls.del.className = "ksm-btn ghost" + (P.confirm === "delete" ? " danger" : "");
    prEls.save.textContent = dirty ? "Save changes" : "Save";
  }

  function prStatusText(d) {
    var st = (d && d.status) || {};
    var s = PR_STATE[st.state] || ["", ""];
    var t = s[0];
    if (st.pending) t += " (" + st.pending + " waiting)";
    prEls.status.textContent = t;
    prEls.status.className = s[1];
  }

  function prRenderTop() {
    var d = P.data;
    if (!d) return;
    prEls.roles.innerHTML = "";
    (d.roles || []).forEach(function (r) {
      var b = el("button", "ksm-pr-role" + (r.role === P.role ? " active" : ""), r.label);
      b.type = "button";
      b.dataset.role = r.role;
      b.addEventListener("click", function () {
        if (r.role === P.role) return;
        P.role = r.role; P.id = ""; prRenderTop(); prOpenPrompt(activeIdOf(r.role));
      });
      prEls.roles.appendChild(b);
    });
    prStatusText(d);
    var notes = [];
    (d.roles || []).forEach(function (r) { (r.notes || []).forEach(function (n) { notes.push(r.label + ": " + n); }); });
    ((d.status && d.status.warnings) || []).forEach(function (n) { notes.push(n); });
    prEls.notes.hidden = !notes.length;
    prEls.notes.innerHTML = "";
    notes.forEach(function (n) { prEls.notes.appendChild(el("div", "grow", n)); });
    prRenderList();
  }

  function activeIdOf(role) {
    var r = ((P.data && P.data.roles) || []).filter(function (x) { return x.role === role; })[0];
    return r ? r.active_id : "";
  }

  function prRenderList() {
    prEls.list.innerHTML = "";
    ((P.data && P.data.prompts) || []).filter(function (p) { return p.role === P.role; }).forEach(function (p) {
      var b = el("button", "ksm-pr-item" + (p.id === P.id ? " selected" : ""));
      b.type = "button";
      b.dataset.id = p.id;
      b.appendChild(el("span", null, p.name));
      if (p.active) b.appendChild(el("span", "ksm-badge inuse", "IN USE"));
      if (p.builtin) b.appendChild(el("span", "ksm-badge locked", "BUILT-IN"));
      if (p.modified) b.appendChild(el("span", "ksm-badge edited", "EDITED"));
      if (p.pending) b.appendChild(el("span", "ksm-badge wait", "WAITING"));
      if (p.conflict) b.appendChild(el("span", "ksm-badge wait", "CHANGED ELSEWHERE"));
      b.addEventListener("click", function () { if (p.id !== P.id) prOpenPrompt(p.id); });
      prEls.list.appendChild(b);
    });
  }

  function loadPrompts() {
    if (!api) return Promise.resolve();
    return callApi("prompts_get").then(function (r) {
      if (!r.ok) { prMsg(r.message || "Could not load the prompts.", "err"); return; }
      P.data = r;
      var mine = (r.prompts || []).filter(function (p) { return p.id === P.id && p.role === P.role; })[0];
      if (!mine) { P.id = ""; }
      prRenderTop();
      if (!P.id) prOpenPrompt(activeIdOf(P.role));
      else if (!P.detail) prOpenPrompt(P.id);
    });
  }

  function refreshPromptStatus() {
    if (!api || P.busy) return;
    callApi("prompts_get").then(function (r) {
      if (!r.ok || !P.data) return;
      P.data = r; prRenderTop();
    });
  }

  function prOpenPrompt(id) {
    if (!id) { prEls.editor.hidden = true; return Promise.resolve(); }
    var dropped = prDirty();
    P.id = id; P.edits = {}; P.added = []; P.errors = []; P.warnings = []; P.detail = null; P.confirm = "";
    prEls.preview.hidden = true; prEls.history.hidden = true;
    prRenderList();
    return callApi("prompt_get", [id]).then(function (r) {
      if (P.id !== id) return;
      if (!r.ok) { P.detail = null; prEls.editor.hidden = true; prMsg(r.message || "Could not open the prompt.", "err"); prUpdateButtons(); return; }
      P.detail = r;
      prEls.editor.hidden = false;
      if (dropped) prMsg("Unsaved changes in the previous prompt were dropped.", "warn");
      prRenderEditor();
    });
  }

  function prBlockList() {
    var d = P.detail, out = [];
    (d.blocks || []).forEach(function (b) {
      if (P.edits[b.id] === null) return;               // a custom block marked for removal
      out.push(b);
    });
    P.added.forEach(function (b) { out.push(b); });
    return out;
  }

  function prShown(b) {
    var e = P.edits[b.id] || {};
    return { text: e.text !== undefined ? e.text : b.text, enabled: e.enabled !== undefined ? e.enabled : b.enabled,
             title: e.title !== undefined ? e.title : b.title };
  }

  function prRenderEditor() {
    var d = P.detail;
    if (!d) return;
    prEls.head.innerHTML = "";
    prEls.head.appendChild(el("span", "n", d.name));
    prEls.head.appendChild(el("span", "m", "version " + d.version));
    if (d.active) prEls.head.appendChild(el("span", "ksm-badge inuse", "IN USE"));
    if (d.builtin) prEls.head.appendChild(el("span", "ksm-badge locked", "BUILT-IN"));
    if (d.pending) prEls.head.appendChild(el("span", "ksm-badge wait", "WAITING TO BE SENT"));
    prRenderConflict();
    prEls.blocks.innerHTML = "";
    prBlockList().forEach(function (b) { prEls.blocks.appendChild(prBuildBlock(b)); });
    prRenderProblems();
    prUpdateButtons();
  }

  function prRenderConflict() {
    var d = P.detail;
    prEls.conflict.hidden = !(d && d.conflict);
    prEls.conflict.innerHTML = "";
    if (!(d && d.conflict)) return;
    prEls.conflict.appendChild(el("span", "grow", "This prompt was changed on another computer while your save was waiting. Your text was not sent. Which one do you want?"));
    [["mine", "Keep mine"], ["theirs", "Take theirs"]].forEach(function (c) {
      var b = el("button", "ksm-btn" + (c[0] === "theirs" ? " ghost" : ""), c[1]);
      b.type = "button";
      b.addEventListener("click", function () {
        callApi("prompt_resolve_conflict", [P.id, c[0]]).then(function (r) {
          prMsg(r.ok ? (c[0] === "mine" ? "Kept your text." : "Took the other computer's text.") : (r.message || "Could not resolve."), r.ok ? "ok" : "err");
          reloadAll();
        });
      });
      prEls.conflict.appendChild(b);
    });
  }

  function prBuildBlock(b) {
    var box = el("div", "ksm-pb" + (b.locked ? " locked" : ""));
    box.dataset.block = b.id;
    var head = el("div", "ksm-pb-head");
    if (b.locked) {
      head.appendChild(el("span", "t", "🔒 " + b.title));
      head.appendChild(el("span", "ksm-badge locked", "LOCKED"));
      head.appendChild(el("span", "grow"));
      head.appendChild(el("span", "count", "Read-only. Changes only with a program upgrade."));
      box.appendChild(head);
      var pre = el("pre");
      pre.textContent = b.text;                 // text, never HTML
      box.appendChild(pre);
      return box;
    }
    var shown = prShown(b);
    var custom = b.kind === "custom";
    if (custom) {
      var ti = document.createElement("input");
      ti.type = "text"; ti.className = "title"; ti.value = shown.title; ti.maxLength = 100; ti.placeholder = "Block title";
      ti.setAttribute("aria-label", "Block title");
      ti.addEventListener("input", function () { prSetEdit(b.id, { title: ti.value }); });
      head.appendChild(ti);
    } else {
      head.appendChild(el("span", "t", b.title));
    }
    if (b.modified || P.edits[b.id]) head.appendChild(el("span", "ksm-badge edited", P.edits[b.id] ? "UNSAVED" : "EDITED"));
    head.appendChild(el("span", "grow"));
    var sw = el("label", "ksm-switch");
    var cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!shown.enabled; cb.setAttribute("aria-label", "Use " + b.title);
    sw.appendChild(cb); sw.appendChild(el("span", "track")); sw.appendChild(el("span", null, cb.checked ? "On" : "Off"));
    cb.addEventListener("change", function () { sw.lastChild.textContent = cb.checked ? "On" : "Off"; prSetEdit(b.id, { enabled: cb.checked }); });
    head.appendChild(sw);
    var count = el("span", "count");
    var rb = el("button", "ksm-btn ghost", custom ? "Remove block" : "Reset block");
    rb.type = "button";
    rb.title = custom ? "Delete this block" : "Go back to the shipped text of this block";
    rb.addEventListener("click", function () {
      var added = P.added.filter(function (x) { return x.id === b.id; })[0];
      if (added) { P.added = P.added.filter(function (x) { return x.id !== b.id; }); delete P.edits[b.id]; prScheduleCheck(); prRenderEditor(); return; }
      if (custom) { P.edits[b.id] = null; prScheduleCheck(); prRenderEditor(); return; }
      callApi("prompt_reset_block", [P.id, b.id]).then(function (r) {
        prMsg(r.ok ? "Block reset to the shipped text." : (r.message || "Could not reset the block."), r.ok ? "ok" : "err");
        delete P.edits[b.id];
        reloadAll();
      });
    });
    head.appendChild(count);
    head.appendChild(rb);
    box.appendChild(head);
    var ta = document.createElement("textarea");
    ta.value = shown.text; ta.spellcheck = false; ta.setAttribute("aria-label", b.title);
    function paintCount() {
      var n = ta.value.length, lim = prLimits().block;
      count.textContent = n + " / " + lim;
      count.className = "count" + (n > lim ? " over" : "");
      ta.classList.toggle("bad", n > lim);
    }
    ta.addEventListener("input", function () { paintCount(); prSetEdit(b.id, { text: ta.value }); });
    paintCount();
    box.appendChild(ta);
    return box;
  }

  function reloadAll() {
    var keep = P.id;
    P.detail = null; P.edits = {}; P.added = []; P.errors = []; P.warnings = [];
    return callApi("prompts_get").then(function (r) {
      if (r.ok) { P.data = r; prRenderTop(); }
      return prOpenPrompt(keep);
    });
  }

  function prBusy(on) { P.busy = on; prUpdateButtons(); }

  function prDoSave() {
    if (!P.detail || !prDirty()) return;
    prBusy(true);
    callApi("prompt_save", [P.id, JSON.stringify(prChanges()), P.detail.version, ""]).then(function (r) {
      prBusy(false);
      if (r.ok) {
        var t = "Saved as version " + r.version + ".";
        if (r.queued) t += " It is saved here and will be sent when Firebase can be reached.";
        if ((r.warnings || []).length) t += "\n" + r.warnings.join("\n");
        prMsg(t, (r.warnings || []).length || r.queued ? "warn" : "ok");
        reloadAll();
      } else {
        prMsg(r.message || "The prompt was not saved.", "err");
        if (r.conflict) reloadAll();
      }
    });
  }

  function prDoSaveAs() {
    var name = prEls.newName.value.trim();
    if (!name) { prMsg("Type a name for the new prompt first.", "warn"); try { prEls.newName.focus(); } catch (e) {} return; }
    prBusy(true);
    callApi("prompt_save_as_new", [P.role, name, JSON.stringify(prChanges()), "", P.id]).then(function (r) {
      prBusy(false);
      if (!r.ok) { prMsg(r.message || "The prompt was not saved.", "err"); return; }
      prMsg("Saved \"" + name + "\" as a new prompt. Press \"Set in use\" to use it.", "ok");
      prEls.newName.value = "";
      P.edits = {}; P.added = [];
      callApi("prompts_get").then(function (g) {
        if (g.ok) { P.data = g; prRenderTop(); }
        prOpenPrompt(r.prompt_id);
      });
    });
  }

  function prDoPreview() {
    prBusy(true);
    callApi("prompt_preview", [P.id, JSON.stringify(prChanges())]).then(function (r) {
      prBusy(false);
      prEls.preview.innerHTML = "";
      prEls.preview.hidden = false;
      if (!r.ok) {
        prEls.preview.appendChild(el("div", "ksm-panel-title", "PREVIEW"));
        prEls.preview.appendChild(el("div", "ksm-set-err", r.message || "Could not build the preview."));
        return;
      }
      var lim = prLimits();
      prEls.preview.appendChild(el("div", "ksm-panel-title", "PREVIEW: THE TEXT THE AI WOULD GET"));
      prEls.preview.appendChild(el("div", "ksm-pr-histrow", r.chars + " characters, about " + r.tokens + " tokens. Uses no AI tokens."));
      if (r.chars > lim.soft) prEls.preview.appendChild(el("div", "ksm-set-err", "This prompt is long (over " + lim.soft + " characters). The chat prompt is sent with every message."));
      (r.warnings || []).forEach(function (w) { prEls.preview.appendChild(el("div", "ksm-set-err", w)); });
      var pre = el("pre");
      pre.textContent = r.text;
      prEls.preview.appendChild(pre);
    });
  }

  var lastHistory = null;
  function prRenderHistoryKeep() { if (lastHistory && !prEls.history.hidden) prRenderHistory(lastHistory); }
  function prRenderHistory(r) {
    lastHistory = r;
    prEls.history.innerHTML = "";
    prEls.history.hidden = false;
    prEls.history.appendChild(el("div", "ksm-panel-title", "VERSION HISTORY"));
    if (!r.ok) { prEls.history.appendChild(el("div", "ksm-set-err", r.message || "History is not available now.")); return; }
    if (!(r.versions || []).length) { prEls.history.appendChild(el("div", "ksm-pr-histrow", "No saved versions yet.")); return; }
    r.versions.forEach(function (v) {
      var row = el("div", "ksm-pr-histrow");
      row.appendChild(el("span", null, "Version " + v.n));
      row.appendChild(el("span", "grow", (v.saved_at ? new Date(v.saved_at).toLocaleString() : "") + (v.saved_by ? " | " + v.saved_by : "") + (v.note ? " | " + v.note : "") + " | " + v.chars + " characters"));
      var key = "restore:" + v.n;
      var b = el("button", "ksm-btn ghost" + (P.confirm === key ? " danger" : ""), P.confirm === key ? "Confirm restore?" : "Restore");
      b.type = "button";
      b.addEventListener("click", function () {
        prConfirmStep(key, b, "Restore", function () {
          callApi("prompt_restore", [P.id, v.n]).then(function (res) {
            prMsg(res.ok ? "Restored version " + v.n + " as new version " + res.version + "." : (res.message || "Could not restore."), res.ok ? "ok" : "err");
            reloadAll();
          });
        });
      });
      row.appendChild(b);
      prEls.history.appendChild(row);
    });
  }

  prEls.save.addEventListener("click", prDoSave);
  prEls.saveAs.addEventListener("click", prDoSaveAs);
  prEls.previewBtn.addEventListener("click", prDoPreview);
  prEls.historyBtn.addEventListener("click", function () {
    if (!prEls.history.hidden) { prEls.history.hidden = true; return; }
    callApi("prompt_history", [P.id]).then(prRenderHistory);
  });
  prEls.active.addEventListener("click", function () {
    callApi("prompt_set_active", [P.role, P.id]).then(function (r) {
      prMsg(r.ok ? "This prompt is now used for " + P.role.replace("_", " ") + "." : (r.message || "Could not change the prompt in use."), r.ok ? "ok" : "err");
      reloadAll();
    });
  });
  prEls.reset.addEventListener("click", function () {
    prConfirmStep("reset", prEls.reset, "Reset prompt", function () {
      callApi("prompt_reset", [P.id]).then(function (r) {
        prMsg(r.ok ? "Prompt reset to the shipped text." : (r.message || "Could not reset the prompt."), r.ok ? "ok" : "err");
        reloadAll();
      });
    });
  });
  prEls.del.addEventListener("click", function () {
    prConfirmStep("delete", prEls.del, "Delete", function () {
      var gone = P.id;
      callApi("prompt_delete", [gone]).then(function (r) {
        prMsg(r.ok ? "Prompt deleted." : (r.message || "Could not delete the prompt."), r.ok ? "ok" : "err");
        P.id = ""; P.detail = null; prEls.editor.hidden = true;
        loadPrompts();
      });
    });
  });
  prEls.addBlock.addEventListener("click", function () {
    callApi("prompt_new_block_id").then(function (r) {
      if (!r.ok || !r.id) { prMsg(r.message || "Could not add a block.", "err"); return; }
      var order = 1;
      prBlockList().forEach(function (b) { if (typeof b.order === "number" && b.order >= order) order = b.order + 1; });
      var nb = { id: r.id, title: "My block", kind: "custom", locked: false, text: "", enabled: true, modified: true, order: order };
      P.added.push(nb);
      P.edits[r.id] = { title: nb.title, text: "", enabled: true, order: order };
      prRenderEditor();
      prScheduleCheck();
    });
  });

  // forward uncaught JS errors to the Python log
  window.addEventListener("error", function (ev) {
    try { api && api.log_client("error", ev.message + " @ " + ev.filename + ":" + ev.lineno); } catch (e) {}
  });
  window.addEventListener("unhandledrejection", function (ev) {
    try { api && api.log_client("error", "Unhandled promise rejection: " + ev.reason); } catch (e) {}
  });

  // ---- boot ------------------------------------------------------------------------
  var booted = false;
  function boot() {
    if (booted) return;
    booted = true;
    api = window.pywebview.api;
    callApi("get_theme").then(function (r) {            // this device's saved theme wins over the browser's memory
      if (r.ok && THEMES.some(function (t) { return t[0] === r.theme; })) applyTheme(r.theme);
    });
    refreshSnapshot();
    refreshSyncChip();
    pollChat();
    pollAudit();
    pollBridge();
    pollScreen();
    pollDebug();
    api.log_client("info", "UI booted");
    setInterval(pollScreen, 800);
    setInterval(pollDebug, 1000);
    setInterval(function () { refreshSnapshot(); }, 1000);
    setInterval(pollChat, 700);
    setInterval(pollAudit, 900);
    setInterval(pollBridge, 1500);
    setInterval(refreshSyncChip, 2000);
    setInterval(function () { if (activeTab === "prompts") refreshPromptStatus(); }, 3000);
    setInterval(refreshSettingsLive, 2000);
  }

  function tryBoot(attempt) {
    attempt = attempt || 0;
    if (window.pywebview && window.pywebview.api) { boot(); return; }
    if (attempt > 200) { // ~10s of polling at 50ms — something is genuinely wrong
      var banner = el(
        "div",
        null,
        "Could not connect to the Python backend (window.pywebview.api never appeared). " +
          "Close this window and restart the app; if it keeps happening, run it with the " +
          "console visible (see README) and check for errors there."
      );
      banner.style.cssText = "position:fixed;top:0;left:0;right:0;background:#7a1f1f;color:#fff;" +
        "font-size:12px;padding:10px 16px;z-index:9999;font-family:Consolas,monospace";
      document.body.appendChild(banner);
      return;
    }
    setTimeout(function () { tryBoot(attempt + 1); }, 50);
  }

  window.addEventListener("pywebviewready", boot);
  tryBoot();
})();
