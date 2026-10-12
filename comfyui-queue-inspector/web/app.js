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
    tr.append(
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
        el("button", { class: "btn", text: "Prompt", onclick: () => openDetails(item.promptId, "prompt") }),
        item.inputImages.length
          ? el("button", { class: "btn", text: "Preview", onclick: () => openPreview(item.promptId, 0) })
          : null),
    );
    return tr;
  }

  function updateRow(entry, item) {
    const { tr } = entry;
    tr.cells[0].textContent = String(item.order);
    tr.cells[0].title = item.number != null ? `queue number ${item.number}` : "";
    const key = item.status;
    if (entry.statusKey !== key) {
      const badge = el("span", { class: "badge " + key, text: key.toUpperCase() });
      tr.cells[1].replaceChildren(badge);
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

    if (snap.unchanged) return;
    items = snap.items;
    $("empty").hidden = !(snap.ok && items.length === 0);

    const alive = new Set();
    let prev = null;
    for (const item of items) {
      alive.add(item.promptId);
      let entry = rows.get(item.promptId);
      if (!entry) {
        entry = { tr: buildRow(item), item, statusKey: null };
        entry.tr.classList.add("entering");
        rows.set(item.promptId, entry);
        requestAnimationFrame(() => entry.tr.classList.remove("entering"));
      }
      updateRow(entry, item);
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
  }

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
