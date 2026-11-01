// ComfyUI Queue Inspector - web UI.
// Talks only to the local inspector server (/api/*). All text from the queue
// is inserted with textContent, never innerHTML, so prompt text cannot inject markup.
(() => {
  "use strict";

  const POLL_MS = 1500;
  const POLL_HIDDEN_MS = 6000;

  const $ = (id) => document.getElementById(id);
  const rowsEl = $("rows");
  const rows = new Map(); // promptId -> { tr, item, statusKey }
  let version = null;
  let comfyUrl = "";
  let reorderEnabled = false;
  let snapOk = false;
  let typeInfo = false;
  let pollTimer = null;
  let items = [];

  // ------------------------------------------------------------ utilities

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const c of children) {
      if (c == null || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  const shortId = (id) => (id.length > 8 ? id.slice(0, 8) + "…" : id);

  let toastTimer = null;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), 1400);
  }

  // navigator.clipboard needs a secure context (https or localhost). When the
  // page is opened via a LAN IP over http it is undefined, so fall back.
  async function copyText(text, label) {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (_) { /* fall through */ }
    if (!ok) {
      const ta = el("textarea", { style: "position:fixed;left:-9999px;top:0", readonly: true });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      ta.remove();
    }
    toast(ok ? `${label || "Text"} copied` : "Copy failed");
  }

  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  // ------------------------------------------------------------ theme

  const THEME_KEY = "qi-theme";
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
  }
  try { applyTheme(localStorage.getItem(THEME_KEY)); } catch (_) { /* storage blocked */ }
  $("theme-btn").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    const next = cur === "light" ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* ignore */ }
  });

  // ------------------------------------------------------------ table rows

  function modelCell(item) {
    const main = item.mainModels;
    const loras = item.models.filter((m) => m.kind === "lora").length;
    let label, extra = 0, fallback = false;
    if (main.length) {
      label = main[0].display;
      extra = main.length - 1;
    } else if (item.models.length) {
      label = item.models[0].display;
      fallback = true;
    } else {
      return el("span", { class: "dash", text: "—" });
    }
    const tip = item.models.map((m) =>
      `${m.kindLabel}: ${m.name || m.display}${m.strength ? " (" + m.strength + ")" : ""}`).join("\n");
    return el("button", { class: "link-cell", title: tip, onclick: () => openDetails(item.promptId, "models") },
      el("span", { class: "main" + (fallback ? " muted" : ""), text: label }),
      extra > 0 ? el("span", { class: "more", text: `+${extra}` }) : null,
      loras > 0 ? el("span", { class: "chip", text: `LoRA ${loras}` }) : null);
  }

  function imageCell(item) {
    const imgs = item.inputImages;
    if (!imgs.length) return el("span", { class: "dash", text: "—" });
    const tip = imgs.map((i) => (i.subfolder ? i.subfolder + "/" : "") + i.filename).join("\n");
    return el("button", { class: "link-cell", title: tip, onclick: () => openPreview(item.promptId, 0) },
      el("span", { class: "main", text: imgs[0].display }),
      imgs.length > 1 ? el("span", { class: "more", text: `+${imgs.length - 1}` }) : null);
  }

  function buildRow(item) {
    const tr = el("tr", { "data-id": item.promptId });
    const status = el("span");
    const id = item.promptId;
    tr.append(
      el("td", { class: "c-sel edit-only" },
        el("input", { type: "checkbox", class: "sel", "aria-label": "Select",
          onclick: (e) => toggleSelect(id, e.shiftKey) }),
        el("span", { class: "handle", title: "Drag to move", text: "⠿",
          onmousedown: () => { tr.draggable = true; } })),
      el("td", { class: "c-order" }),
      el("td", { class: "c-status" }, status),
      el("td", { class: "c-id" },
        el("button", {
          class: "id-chip", title: `${item.promptId}\nClick to copy`, text: shortId(item.promptId),
          onclick: () => copyText(item.promptId, "Prompt ID"),
        })),
      el("td", { class: "c-model" }, modelCell(item)),
      el("td", { class: "c-image" }, imageCell(item)),
      el("td", { class: "c-actions" },
        el("span", { class: "view-only" },
          el("button", { class: "btn", text: "Prompt", onclick: () => openDetails(item.promptId, "prompt") }),
          item.inputImages.length
            ? el("button", { class: "btn", text: "Preview", onclick: () => openPreview(item.promptId, 0) })
            : null),
        el("span", { class: "move-btns edit-only" },
          el("button", { class: "icon-btn", text: "⤒", title: "Move to front", onclick: () => moveOp("top", id) }),
          el("button", { class: "icon-btn", text: "▲", title: "Move up", onclick: () => moveOp("up", id) }),
          el("button", { class: "icon-btn", text: "▼", title: "Move down", onclick: () => moveOp("down", id) }),
          el("button", { class: "icon-btn", text: "⤓", title: "Move to back", onclick: () => moveOp("bottom", id) }))),
    );
    attachDrag(tr, id);
    return tr;
  }

  function updateRow(entry, item, position) {
    const { tr } = entry;
    const orderCell = tr.querySelector(".c-order");
    orderCell.textContent = String(position);
    orderCell.title = item.number != null ? `queue number ${item.number}` : "";
    const key = item.status;
    if (entry.statusKey !== key) {
      const badge = el("span", { class: "badge " + key, text: key.toUpperCase() });
      tr.querySelector(".c-status").replaceChildren(badge);
      tr.classList.toggle("is-running", key === "running");
      entry.statusKey = key;
    }
    entry.item = item;
  }

  function render(snap) {
    if (snap.comfyUrl) comfyUrl = snap.comfyUrl;
    const banner = $("banner");
    const conn = $("conn");
    if (!snap.ok) {
      banner.textContent = snap.error || "Unable to read queue.";
      banner.hidden = false;
      conn.className = "conn err";
      $("conn-text").textContent = "ComfyUI unreachable";
      conn.title = `Trying ComfyUI at ${snap.comfyUrl}`;
    } else {
      banner.hidden = true;
      conn.className = "conn ok";
      $("conn-text").textContent = "Connected";
      conn.title = `Connected to ComfyUI at ${snap.comfyUrl}. ` + (snap.typeInfo
        ? "Node type info loaded."
        : "Node type info not loaded yet; prompt roles use name-based fallback.");
    }
    $("counts").replaceChildren(
      "Running ", el("b", { text: String(snap.running) }), " · Pending ", el("b", { text: String(snap.pending) }));
    $("updated").textContent = snap.checkedAt ? "Updated " + fmtTime(snap.checkedAt) : "";
    document.title = snap.ok ? `(${snap.running + snap.pending}) Queue Inspector` : "Queue Inspector";

    reorderEnabled = !!snap.reorderEnabled;
    snapOk = !!snap.ok;
    typeInfo = !!snap.typeInfo;
    updateEditControls();
    if (snap.unchanged) return;
    items = snap.items;
    $("empty").hidden = !(snap.ok && items.length === 0);
    paint();
  }

  // Put rows in DOM order: execution order, or the draft order while editing.
  function paint() {
    const ordered = editing ? reconcileDraft() : items;
    const alive = new Set();
    let prev = null;
    let position = 0;
    for (const item of ordered) {
      position += 1;
      alive.add(item.promptId);
      let entry = rows.get(item.promptId);
      if (!entry) {
        entry = { tr: buildRow(item), item, statusKey: null };
        entry.tr.classList.add("entering");
        rows.set(item.promptId, entry);
        requestAnimationFrame(() => entry.tr.classList.remove("entering"));
      }
      updateRow(entry, item, position);
      // Keep DOM order equal to execution order; move only rows that are out of place.
      const want = prev ? prev.nextSibling : rowsEl.firstChild;
      if (want !== entry.tr) rowsEl.insertBefore(entry.tr, want);
      prev = entry.tr;
    }
    for (const [id, entry] of rows) {
      if (alive.has(id)) continue;
      rows.delete(id);
      entry.tr.classList.add("leaving");
      setTimeout(() => entry.tr.remove(), 260);
    }
    annotateRows(ordered);
  }

  // ------------------------------------------------------------ reorder (edit mode)
  // Nothing is sent to ComfyUI while editing. Confirm sends the final order;
  // the server re-queues only the jobs that must move (see qi/reorder.py).

  let editing = false;
  let draft = [];            // pending prompt ids in the edited order
  let baseline = [];         // pending ids when editing started
  let selected = new Set();
  let lastClicked = null;
  let newIds = new Set();    // queued after editing started
  let lastUndo = null;       // order before the last confirmed change
  let applying = false;

  const isPinned = (item) => item && item.apiNodes && item.apiNodes.length > 0;
  const byId = () => new Map(items.map((i) => [i.promptId, i]));

  // Same rule as the server: largest set kept in place (weighted LIS, pinned jobs must stay).
  function keptSet(current, desired, pinned) {
    const pos = new Map(current.map((id, i) => [id, i]));
    const seq = desired.map((id) => pos.get(id));
    const big = seq.length + 1;
    const w = desired.map((id) => (pinned.has(id) ? big : 1));
    const best = w.slice(), prev = new Array(seq.length).fill(-1);
    for (let i = 0; i < seq.length; i++) {
      for (let j = 0; j < i; j++) {
        if (seq[j] < seq[i] && best[j] + w[i] > best[i]) { best[i] = best[j] + w[i]; prev[i] = j; }
      }
    }
    const keep = new Set();
    if (!seq.length) return keep;
    let i = 0;
    for (let k = 1; k < seq.length; k++) if (best[k] > best[i]) i = k;
    for (; i !== -1; i = prev[i]) keep.add(desired[i]);
    return keep;
  }

  function reconcileDraft() {
    const map = byId();
    const running = items.filter((i) => i.status === "running");
    const pending = items.filter((i) => i.status === "pending").map((i) => i.promptId);
    const pendingSet = new Set(pending);
    const dropped = draft.filter((id) => !pendingSet.has(id));
    if (dropped.length) {
      const started = dropped.filter((id) => map.get(id) && map.get(id).status === "running");
      toast(started.length ? `${started.length} job(s) started running and are now locked`
                           : `${dropped.length} job(s) left the queue`);
      draft = draft.filter((id) => pendingSet.has(id));
      for (const id of dropped) selected.delete(id);
    }
    for (const id of pending) {
      if (!draft.includes(id)) { draft.push(id); newIds.add(id); }
    }
    return running.concat(draft.map((id) => map.get(id)));
  }

  function changedIds() {
    const current = items.filter((i) => i.status === "pending").map((i) => i.promptId);
    const map = byId();
    const pinned = new Set(draft.filter((id) => isPinned(map.get(id))));
    const keep = keptSet(current, draft, pinned);
    return new Set(draft.filter((id) => !keep.has(id)));
  }

  function annotateRows(ordered) {
    if (!editing) {
      for (const { tr } of rows.values()) {
        tr.classList.remove("is-moved", "is-selected", "is-locked", "is-new");
        tr.querySelectorAll(".was, .tag").forEach((n) => n.remove());
      }
      return;
    }
    const changed = changedIds();
    const runningCount = ordered.filter((i) => i.status === "running").length;
    const basePos = new Map(baseline.map((id, i) => [id, i + 1 + runningCount]));
    ordered.forEach((item, idx) => {
      const entry = rows.get(item.promptId);
      if (!entry) return;
      const tr = entry.tr;
      const locked = item.status === "running" || isPinned(item);
      tr.classList.toggle("is-locked", locked);
      tr.classList.toggle("is-moved", changed.has(item.promptId));
      tr.classList.toggle("is-selected", selected.has(item.promptId));
      tr.classList.toggle("is-new", newIds.has(item.promptId));
      const box = tr.querySelector("input.sel");
      box.checked = selected.has(item.promptId);
      box.disabled = locked;
      tr.querySelector(".handle").style.visibility = locked ? "hidden" : "";
      tr.querySelectorAll(".move-btns button").forEach((b) => { b.disabled = locked; });
      tr.querySelectorAll(".was, .tag").forEach((n) => n.remove());
      const orderCell = tr.querySelector(".c-order");
      const was = basePos.get(item.promptId);
      if (changed.has(item.promptId) && was && was !== idx + 1) {
        orderCell.append(el("span", { class: "was", text: `was #${was}` }));
      }
      const statusCell = tr.querySelector(".c-status");
      if (item.status === "running") {
        statusCell.append(el("span", { class: "tag lock", text: "LOCKED", title: "Running jobs cannot be moved" }));
      } else if (isPinned(item)) {
        statusCell.append(el("span", { class: "tag lock", text: "API",
          title: `Uses cloud API nodes (${item.apiNodes.join(", ")}); ComfyUI keeps their login token ` +
                 "out of the queue, so this job cannot be re-queued and stays in place." }));
      } else if (newIds.has(item.promptId)) {
        statusCell.append(el("span", { class: "tag new", text: "NEW", title: "Queued after you started editing" }));
      }
    });
    const n = changed.size;
    $("edit-confirm").textContent = n ? `Confirm (${n})` : "Confirm";
    $("edit-confirm").disabled = n === 0 || !snapOk || applying;
    $("edit-label").textContent = n ? `Editing order · ${n} job(s) will move` : "Editing order";
    const movable = draft.filter((id) => !isPinned(byId().get(id)));
    $("sel-all").checked = movable.length > 0 && movable.every((id) => selected.has(id));
  }

  function updateEditControls() {
    $("reorder-btn").hidden = !reorderEnabled;
    // Without node type info, jobs using cloud API nodes cannot be recognised
    // (and must never be re-queued), so editing waits for it.
    const ready = snapOk && typeInfo;
    $("reorder-btn").disabled = !ready;
    $("reorder-btn").title = ready ? "Change the order of pending jobs"
      : "Waiting for ComfyUI's node type info before jobs can be reordered…";
    $("undo-btn").hidden = !reorderEnabled || !lastUndo;
    if (!reorderEnabled && editing) stopEditing();
  }

  function startEditing() {
    editing = true;
    draft = items.filter((i) => i.status === "pending").map((i) => i.promptId);
    baseline = draft.slice();
    selected = new Set();
    newIds = new Set();
    lastClicked = null;
    document.body.classList.add("editing");
    paint();
  }

  function stopEditing() {
    editing = false;
    draft = []; baseline = []; selected = new Set(); newIds = new Set();
    document.body.classList.remove("editing");
    paint();
  }

  function movableSelection(anchorId) {
    const map = byId();
    const ids = anchorId && !selected.has(anchorId) ? [anchorId] : draft.filter((id) => selected.has(id));
    return new Set(ids.filter((id) => !isPinned(map.get(id))));
  }

  function moveOp(op, anchorId) {
    const set = movableSelection(anchorId);
    if (!set.size) return;
    if (op === "top" || op === "bottom") {
      const block = draft.filter((id) => set.has(id));
      const rest = draft.filter((id) => !set.has(id));
      draft = op === "top" ? block.concat(rest) : rest.concat(block);
    } else if (op === "up") {
      for (let i = 1; i < draft.length; i++) {
        if (set.has(draft[i]) && !set.has(draft[i - 1])) [draft[i - 1], draft[i]] = [draft[i], draft[i - 1]];
      }
    } else if (op === "down") {
      for (let i = draft.length - 2; i >= 0; i--) {
        if (set.has(draft[i]) && !set.has(draft[i + 1])) [draft[i], draft[i + 1]] = [draft[i + 1], draft[i]];
      }
    }
    paint();
  }

  // Move the block before the job `targetId` (or to the end when null).
  function moveBlockBefore(set, targetId) {
    const block = draft.filter((id) => set.has(id));
    const rest = draft.filter((id) => !set.has(id));
    let at = targetId === null ? rest.length : rest.indexOf(targetId);
    if (at < 0) at = rest.length;
    draft = rest.slice(0, at).concat(block, rest.slice(at));
    paint();
  }

  function toggleSelect(id, shift) {
    if (shift && lastClicked && draft.includes(lastClicked)) {
      const a = draft.indexOf(lastClicked), b = draft.indexOf(id);
      const [lo, hi] = a < b ? [a, b] : [b, a];
      const on = !selected.has(id) || selected.has(lastClicked);
      const map = byId();
      for (const x of draft.slice(lo, hi + 1)) {
        if (isPinned(map.get(x))) continue;
        if (on) selected.add(x); else selected.delete(x);
      }
    } else if (selected.has(id)) {
      selected.delete(id);
    } else {
      selected.add(id);
    }
    lastClicked = id;
    paint();
  }

  // ---- drag and drop (rows become draggable only while the handle is held)
  let dragSet = null;

  function clearDropMarks() {
    rowsEl.querySelectorAll(".drop-before, .drop-after").forEach((r) => r.classList.remove("drop-before", "drop-after"));
  }

  function dropTarget(e) {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return null;
    const item = byId().get(tr.dataset.id);
    if (!item) return null;
    if (item.status === "running") return { tr, before: draft[0] || null, mark: "drop-after" };
    const r = tr.getBoundingClientRect();
    const upper = e.clientY < r.top + r.height / 2;
    const idx = draft.indexOf(tr.dataset.id);
    // Never drop onto a position inside the dragged block itself.
    let before = upper ? draft[idx] : draft[idx + 1] || null;
    while (before !== null && dragSet.has(before)) {
      const k = draft.indexOf(before);
      before = draft[k + 1] || null;
    }
    return { tr, before, mark: upper ? "drop-before" : "drop-after" };
  }

  function attachDrag(tr, id) {
    tr.addEventListener("dragstart", (e) => {
      if (!editing || !tr.draggable) { e.preventDefault(); return; }
      dragSet = movableSelection(id);
      if (!dragSet.size) { e.preventDefault(); return; }
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", id);
      for (const x of dragSet) { const r = rows.get(x); if (r) r.tr.classList.add("dragging"); }
    });
    tr.addEventListener("dragend", () => {
      tr.draggable = false;
      clearDropMarks();
      rowsEl.querySelectorAll(".dragging").forEach((r) => r.classList.remove("dragging"));
      dragSet = null;
    });
  }
  document.addEventListener("mouseup", () => {
    if (!dragSet) rowsEl.querySelectorAll("tr[draggable=true]").forEach((r) => { r.draggable = false; });
  });
  rowsEl.addEventListener("dragover", (e) => {
    if (!dragSet) return;
    const t = dropTarget(e);
    if (!t) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    clearDropMarks();
    t.tr.classList.add(t.mark);
  });
  rowsEl.addEventListener("drop", (e) => {
    if (!dragSet) return;
    const t = dropTarget(e);
    e.preventDefault();
    clearDropMarks();
    if (t) moveBlockBefore(dragSet, t.before);
  });

  // ---- header buttons and applying
  $("reorder-btn").addEventListener("click", startEditing);
  $("edit-cancel").addEventListener("click", stopEditing);
  $("sel-all").addEventListener("click", (e) => {
    const map = byId();
    selected = e.target.checked ? new Set(draft.filter((id) => !isPinned(map.get(id)))) : new Set();
    paint();
  });
  window.addEventListener("beforeunload", (e) => {
    if (editing && changedIds().size) { e.preventDefault(); e.returnValue = ""; }
  });

  async function sendOrder(order) {
    applying = true;
    document.body.classList.add("applying");
    $("edit-confirm").disabled = true;
    try {
      const res = await fetch("/api/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Queue-Inspector": "1" },
        body: JSON.stringify({ order }),
      });
      return await res.json();
    } catch (_) {
      return { ok: false, error: "The inspector server did not answer." };
    } finally {
      applying = false;
      document.body.classList.remove("applying");
    }
  }

  function showResult(res, title) {
    const map = byId();
    const label = (id) => {
      const it = map.get(id);
      const m = it && it.mainModels && it.mainModels[0];
      return m ? m.display : "";
    };
    $("result-title").textContent = title;
    const body = [];
    if (!res.ok) {
      $("result-summary").textContent = "Nothing was changed.";
      body.push(el("p", { class: "preview-reason", text: res.error || "Unknown error." }));
    } else {
      const r = res.results || [];
      const count = (s) => r.filter((x) => x.status === s).length;
      $("result-summary").textContent =
        `${count("moved")} moved · ${count("skipped")} skipped · ${count("restored")} kept in place · ${count("failed")} failed`;
      const li = r.map((x) => {
        let icon = "✓", cls = "r-ok", text = "Moved; re-queued job verified identical to the original.";
        if (x.status === "moved" && x.diffs && x.diffs.length) { icon = "⚠"; cls = "r-warn"; text = "Moved, but " + x.reason; }
        else if (x.status === "moved" && x.reason) { icon = "✓"; cls = "r-ok"; text = "Moved; " + x.reason; }
        else if (x.status === "skipped") { icon = "↷"; cls = "r-warn"; text = "Not moved: " + x.reason; }
        else if (x.status === "restored") { icon = "↺"; cls = "r-warn"; text = "Not moved: " + x.reason; }
        else if (x.status === "failed") { icon = "✕"; cls = "r-bad"; text = "Failed: " + x.reason + (x.backup ? ` (backup: ${x.backup})` : ""); }
        return el("li", null,
          el("span", { class: "r-icon " + cls, text: icon }),
          el("span", { class: "r-id", text: shortId(x.promptId) }),
          el("span", { class: "r-text", text: (label(x.promptId) ? label(x.promptId) + " — " : "") + text }));
      });
      if (!li.length) body.push(el("p", { class: "none", text: "The queue already had this order; nothing to move." }));
      else body.push(el("ul", { class: "result-list" }, ...li));
      const extra = [];
      if (res.skippedRunning && res.skippedRunning.length) extra.push(`${res.skippedRunning.length} job(s) started running and were left alone.`);
      if (res.skippedGone && res.skippedGone.length) extra.push(`${res.skippedGone.length} job(s) had already left the queue.`);
      if (extra.length) body.push(el("p", { class: "note section", text: extra.join(" ") }));
    }
    $("result-body").replaceChildren(...body);
    showModal("result");
  }

  $("edit-confirm").addEventListener("click", async () => {
    if (!editing || applying) return;
    const res = await sendOrder(draft.slice());
    if (res.ok && (res.results || []).some((x) => x.status === "moved")) lastUndo = res.previousOrder;
    stopEditing();
    showResult(res, "Reorder result");
    updateEditControls();
    poll(true);
  });

  $("undo-btn").addEventListener("click", async () => {
    if (!lastUndo || applying) return;
    if (!confirm("Restore the order the queue had before your last confirmed change?")) return;
    const res = await sendOrder(lastUndo);
    lastUndo = null;
    showResult(res, "Undo result");
    updateEditControls();
    poll(true);
  });

  // ------------------------------------------------------------ polling

  async function poll(force) {
    clearTimeout(pollTimer);
    try {
      const q = force ? "force=1" : version != null ? `v=${version}` : "";
      const res = await fetch("/api/queue" + (q ? "?" + q : ""), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const snap = await res.json();
      version = snap.version;
      render(snap);
    } catch (_) {
      // The inspector server itself is gone (window closed / stopped).
      $("conn").className = "conn err";
      $("conn-text").textContent = "Inspector server stopped";
    }
    pollTimer = setTimeout(() => poll(false), document.hidden ? POLL_HIDDEN_MS : POLL_MS);
  }

  $("refresh-btn").addEventListener("click", (e) => {
    const b = e.currentTarget;
    b.classList.remove("spin");
    void b.offsetWidth; // restart animation
    b.classList.add("spin");
    poll(true);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(false); });

  // ------------------------------------------------------------ modals

  let openModal = null;
  function showModal(id) {
    if (openModal) openModal.hidden = true;
    openModal = $(id);
    openModal.hidden = false;
  }
  function closeModal() {
    if (!openModal) return;
    openModal.hidden = true;
    if (openModal.id === "preview") $("preview-stage").replaceChildren(); // release image
    openModal = null;
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  for (const bd of document.querySelectorAll(".modal-backdrop")) {
    bd.addEventListener("mousedown", (e) => { if (e.target === bd) closeModal(); });
    bd.querySelector("[data-close]").addEventListener("click", closeModal);
  }

  // Modals read from a snapshot of the item taken at open time, so they keep
  // working even if the job finishes while the modal is open.
  let detailsItem = null;
  let detailsTab = "prompt";

  function findItem(promptId) {
    const e = rows.get(promptId);
    return e ? e.item : items.find((i) => i.promptId === promptId) || null;
  }

  function openDetails(promptId, tab) {
    const item = findItem(promptId);
    if (!item) return;
    detailsItem = item;
    $("details-title").textContent = tab === "models" ? "Models" : "Prompt";
    const chip = $("details-id");
    chip.textContent = item.promptId;
    chip.onclick = () => copyText(item.promptId, "Prompt ID");
    switchTab(tab);
    showModal("details");
  }

  function switchTab(tab) {
    detailsTab = tab;
    for (const t of document.querySelectorAll("#details .tab")) {
      t.classList.toggle("active", t.dataset.tab === tab);
      t.setAttribute("aria-selected", t.dataset.tab === tab ? "true" : "false");
    }
    $("details-title").textContent = tab === "models" ? "Models" : "Prompt";
    $("copy-all").hidden = tab !== "prompt";
    $("details-body").replaceChildren(...(tab === "models" ? modelsView(detailsItem) : promptView(detailsItem)));
    $("details-body").scrollTop = 0;
  }
  for (const t of document.querySelectorAll("#details .tab")) {
    t.addEventListener("click", () => switchTab(t.dataset.tab));
  }

  function promptBlock(entry) {
    const empty = entry.text === "";
    return el("div", { class: "prompt-block" },
      el("div", { class: "prompt-meta" },
        el("span", { text: `${entry.title} · #${entry.nodeId}${entry.field ? " · " + entry.field : ""}` }),
        el("span", { class: "spacer" }),
        entry.note && !entry.field ? null :
          el("button", { class: "btn", text: "Copy", onclick: () => copyText(entry.text, "Prompt") })),
      entry.note ? el("div", { class: "note", text: entry.note }) : null,
      entry.field ? el("pre", { class: "prompt-text" + (empty ? " is-empty" : ""), text: empty ? "(empty)" : entry.text }) : null);
  }

  function section(title, list, emptyText) {
    return el("div", { class: "section" },
      el("div", { class: "section-head" },
        el("span", null, title, list.length > 1 ? el("span", { class: "count", text: `(${list.length})` }) : null)),
      ...(list.length ? list.map(promptBlock) : [el("div", { class: "none", text: emptyText })]));
  }

  function promptView(item) {
    const p = item.prompts;
    const nothing = !p.positive.length && !p.negative.length && !p.other.length;
    if (nothing) return [el("div", { class: "none section", text: "No prompt text detected" })];
    const out = [];
    if (p.positive.length || p.negative.length) {
      out.push(section("Positive", p.positive, "Not detected"));
      out.push(section("Negative", p.negative, "Not detected"));
    }
    if (p.other.length) out.push(section("Other prompt text", p.other, ""));
    if (item.warnings && item.warnings.length) {
      out.push(el("div", { class: "section note", text: item.warnings.join(" · ") }));
    }
    return out;
  }

  $("copy-all").addEventListener("click", () => {
    if (!detailsItem) return;
    const p = detailsItem.prompts;
    const parts = [];
    const add = (title, list) => {
      const texts = list.filter((e) => e.field).map((e) => e.text);
      if (texts.length) parts.push(`[${title}]\n` + texts.join("\n\n"));
    };
    add("Positive", p.positive);
    add("Negative", p.negative);
    add("Other", p.other);
    copyText(parts.join("\n\n"), "All prompts");
  });

  function modelsView(item) {
    if (!item.models.length) return [el("div", { class: "none section", text: "No model files detected" })];
    const list = el("ul", { class: "model-list" },
      ...item.models.map((m) => el("li", null,
        el("span", { class: "model-kind", text: m.kindLabel }),
        el("div", null,
          el("div", { class: "model-name", text: m.name || m.display }),
          el("div", { class: "model-sub",
            text: `${m.title} · #${m.nodeId}${m.strength ? " · strength " + m.strength : ""}` })),
        m.name ? el("button", { class: "btn", text: "Copy", onclick: () => copyText(m.name, "Model name") }) : el("span"))));
    const meta = [];
    if (item.workflowId) meta.push(`workflow id ${item.workflowId}`);
    meta.push(`${item.nodeCount} nodes`);
    if (item.unusedNodeCount) meta.push(`${item.unusedNodeCount} not connected to an output (ignored)`);
    return [el("div", { class: "section" }, list), el("div", { class: "note section", text: meta.join(" · ") })];
  }

  // ------------------------------------------------------------ image preview

  let previewItem = null;

  function imageUrl(ref) {
    return "/api/image?" + viewQuery(ref);
  }

  function openPreview(promptId, idx) {
    const item = findItem(promptId);
    if (!item || !item.inputImages.length) return;
    previewItem = item;
    showPreviewImage(idx);
    showModal("preview");
  }

  function viewQuery(ref) {
    const q = new URLSearchParams({ filename: ref.filename, type: ref.type });
    if (ref.subfolder) q.set("subfolder", ref.subfolder);
    return q.toString();
  }

  function missingReason(ref, err) {
    // comfyFolder: the server knows this node reads from the folder /view serves.
    const core = ref.comfyFolder;
    if (!err) return "The inspector server did not answer.";
    if (err.error === "comfy_unreachable") return err.message + ".";
    if (err.error === "not_image") return err.message + ".";
    if (err.comfyStatus === 404) {
      return core
        ? `ComfyUI has no file with this name in its ${ref.type} folder. ` +
          "ComfyUI checks that the file exists when a job is queued, so it was " +
          "probably moved, renamed or deleted after queuing."
        : `This node (${ref.classType}) is not a loader the inspector knows, so it may ` +
          `read the file from its own folder rather than ComfyUI's ${ref.type} folder.`;
    }
    if (err.comfyStatus === 400 || err.comfyStatus === 403) {
      return `ComfyUI rejected the file path (HTTP ${err.comfyStatus}).`;
    }
    return err.message || "Unknown error.";
  }

  async function showMissing(stage, ref) {
    let err = null;
    try {
      const res = await fetch(imageUrl(ref), { cache: "no-store" });
      err = await res.json();
    } catch (_) { /* server gone or not JSON */ }
    const direct = comfyUrl ? `${comfyUrl}/view?${viewQuery(ref)}` : null;
    stage.replaceChildren(el("div", { class: "preview-missing" },
      el("b", { text: "Image not found" }),
      el("span", { text: ref.raw }),
      el("p", { class: "preview-reason", text: missingReason(ref, err) }),
      direct ? el("a", { class: "btn", href: direct, target: "_blank", rel: "noopener",
        text: "Open in ComfyUI" }) : null));
  }

  function showPreviewImage(idx) {
    const ref = previewItem.inputImages[idx];
    $("preview-title").textContent = ref.isAsset ? ref.raw : ref.filename;
    const where = ref.external ? ref.raw
      : `${ref.type}/${ref.subfolder ? ref.subfolder + "/" : ""}${ref.isAsset ? "" : ref.filename}`;
    $("preview-path").textContent = `${where} · node #${ref.nodeId} ${ref.classType || ""} (${ref.title})`;
    const stage = $("preview-stage");
    if (ref.external) {
      // Absolute path / URL from a multi-source loader: /view cannot serve it.
      stage.replaceChildren(el("div", { class: "preview-missing" },
        el("b", { text: "No preview for this source" }),
        el("span", { text: ref.raw }),
        el("p", { class: "preview-reason",
          text: "This image is read from outside ComfyUI's input folder (a local path or URL), " +
                "which ComfyUI's image viewer cannot serve." })));
    } else {
      // Load the image only now - never while rendering the table.
      const img = el("img", { alt: ref.display, src: imageUrl(ref) });
      img.addEventListener("error", () => showMissing(stage, ref));
      stage.replaceChildren(img);
    }

    const strip = $("preview-strip");
    if (previewItem.inputImages.length > 1) {
      strip.replaceChildren(...previewItem.inputImages.map((r, i) =>
        el("button", { class: "btn" + (i === idx ? " active" : ""), text: r.display, title: r.raw,
          onclick: () => showPreviewImage(i) })));
    } else {
      strip.replaceChildren();
    }
  }

  poll(false);
})();
