import * as state from "./state.js";

const { s } = state;

// ── DOM refs ─────────────────────────────────────────────────────────────────

export const statusEl        = document.getElementById("status");
export const projectTitleEl  = document.getElementById("project-title");
export const saveHintEl      = document.getElementById("save-hint");
export const logEl           = document.getElementById("log");
export const diagListEl      = document.getElementById("diag-list");
export const diagCountEl     = document.getElementById("diag-count");
export const diagCountClosedEl = document.getElementById("diag-count-closed");
export const sbCompileEl     = document.getElementById("sb-compile");
export const sbLangEl        = document.getElementById("sb-lang");
export const sbLineColEl     = document.getElementById("sb-linecol");
export const sbWrapBtnEl     = document.getElementById("sb-wrap-toggle");
export const statusbarEl     = document.getElementById("statusbar");
export const editorTabNameEl = document.getElementById("editor-tab-name");
export const editorTabDirtyEl= document.getElementById("editor-tab-dirty");
export const bottomPanel     = document.getElementById("bottom-panel");
export const logPanelBody    = document.getElementById("log-panel-body");
export const tabProblemsBtn  = document.getElementById("tab-problems-btn");
export const logToggleBtn    = document.getElementById("log-toggle");
export const bottomCloseBtn  = document.getElementById("bottom-close-btn");
export const bottomCollapseBtn = document.getElementById("bottom-collapse-btn");
export const bottomOpener    = document.getElementById("bottom-opener");
export const bottomResizeHandle = document.getElementById("bottom-resize");
export const editorWrapEl    = document.getElementById("editor-wrap");
export const assetView       = document.getElementById("asset-view");
export const assetBox        = document.getElementById("asset-box");

// ── Primitives ────────────────────────────────────────────────────────────────

export function escHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function fmtDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso || "";
  return d.toLocaleString("uk-UA", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

// ── Status bar ────────────────────────────────────────────────────────────────

const LANG_MAP = {
  typ: "Typst", tex: "LaTeX", md: "Markdown", bib: "BibTeX",
  yaml: "YAML", yml: "YAML", json: "JSON", txt: "Text", csl: "CSL",
};

export function updateStatusBarLang(filename) {
  const ext = String(filename || "").split(".").pop().toLowerCase();
  if (sbLangEl) sbLangEl.textContent = LANG_MAP[ext] || ext.toUpperCase() || "Plain Text";
}

export function updateEditorTab(filename) {
  if (editorTabNameEl) editorTabNameEl.textContent = filename || "untitled";
  updateStatusBarLang(filename);
}

export function updateWrapToggle(enabled) {
  if (!sbWrapBtnEl) return;
  sbWrapBtnEl.classList.toggle("active", Boolean(enabled));
  sbWrapBtnEl.setAttribute("aria-pressed", enabled ? "true" : "false");
  sbWrapBtnEl.title = enabled ? "Вимкнути перенос рядків" : "Увімкнути перенос рядків";
}

const COMPILE_ICONS = {
  synced:      `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><polyline points="6 8 7.5 9.5 10.5 6.5"/></svg>`,
  compiling:   `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a6 6 0 1 0 6 6"/><path d="M14 2v4h-4"/></svg>`,
  failed:      `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><line x1="6" y1="6" x2="10" y2="10"/><line x1="10" y1="6" x2="6" y2="10"/></svg>`,
  out_of_date: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><line x1="8" y1="5" x2="8" y2="8.5"/><line x1="8" y1="11" x2="8" y2="11.5"/></svg>`,
};
const COMPILE_LABELS = {
  synced: "Synced", compiling: "Compiling…", failed: "Build failed", out_of_date: "Out of date",
};

export function humanCompileState(state) {
  return COMPILE_LABELS[state] || String(state || "").replace(/_/g, " ");
}

export function updateStatusBarCompile(state) {
  const icon  = COMPILE_ICONS[state] || COMPILE_ICONS.out_of_date;
  const label = COMPILE_LABELS[state] || humanCompileState(state);
  if (sbCompileEl) sbCompileEl.innerHTML = `${icon} ${label}`;
  if (statusbarEl) statusbarEl.classList.toggle("error-state", state === "failed");
}

export function setCompileState(state, rawStatus = state) {
  s.compileState = state || "out_of_date";
  if (statusEl) {
    statusEl.textContent = humanCompileState(s.compileState);
    statusEl.className = `status-chip ${String(s.compileState).replace(/_/g, "-")}`;
    statusEl.dataset.rawStatus = rawStatus || "";
  }
  updateStatusBarCompile(s.compileState);
}

export function setSaveHint(text, type = "") {
  if (saveHintEl) {
    saveHintEl.textContent = text;
    saveHintEl.className = `e-save-hint${type ? " " + type : ""}`;
  }
  if (editorTabDirtyEl) editorTabDirtyEl.classList.toggle("show", type === "saving");
}

// ── Bottom panel ──────────────────────────────────────────────────────────────

export function switchBottomTab(tab) {
  if (!bottomPanel) return;
  bottomPanel.classList.add("open");
  bottomPanel.classList.remove("collapsed");
  const diagPanel    = document.getElementById("diag-panel");
  const refsPanel    = document.getElementById("refs-panel");
  const refsTabBtn   = document.getElementById("tab-refs-btn");
  const allBodies    = [logPanelBody, diagPanel, refsPanel];
  const allTabs      = [logToggleBtn, tabProblemsBtn, refsTabBtn];
  allBodies.forEach(el => el?.classList.remove("active"));
  allTabs.forEach(el => el?.classList.remove("active"));
  if (tab === "log") {
    logToggleBtn?.classList.add("active");
    logPanelBody?.classList.add("active");
  } else if (tab === "refs") {
    refsTabBtn?.classList.add("active");
    refsPanel?.classList.add("active");
  } else {
    tabProblemsBtn?.classList.add("active");
    diagPanel?.classList.add("active");
  }
}

export function openLog() { switchBottomTab("log"); }

// ── Diagnostics ───────────────────────────────────────────────────────────────

export function parseDiagnostics(logText) {
  const text  = String(logText || "");
  const lines = text.split("\n");
  const items = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const arrowMatch = line.match(/(?:-->|┌─)\s+([A-Za-z0-9_./-]+\.(?:typ|tex|bib|sty|cls)):(\d+):(\d+)/);
    if (arrowMatch) {
      const prev = lines[Math.max(0, i - 1)].trim();
      items.push({ file: arrowMatch[1], line: Number(arrowMatch[2]), column: Number(arrowMatch[3]), message: prev || line.trim() });
      continue;
    }
    const pathMatch = line.match(/([A-Za-z0-9_./-]+\.(?:typ|tex|bib|sty|cls)):(\d+)(?::(\d+))?/);
    const msgMatch  = line.match(/(?:error|warning)[^:]*:\s*(.+)/i);
    if (pathMatch) {
      items.push({ file: pathMatch[1], line: Number(pathMatch[2]), column: Number(pathMatch[3] || 1), message: (msgMatch && msgMatch[1]) || line.trim() });
    }
  }
  return items;
}

const DIAG_ICONS = {
  error:   `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><line x1="8" y1="5.5" x2="8" y2="8.5"/><circle cx="8" cy="11" r=".5" fill="currentColor" stroke="none"/></svg>`,
  warning: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3.5 13.5 13H2.5L8 3.5z"/><line x1="8" y1="8" x2="8" y2="10"/><circle cx="8" cy="12" r=".5" fill="currentColor" stroke="none"/></svg>`,
};

export function renderDiagnostics(diagnostics, openOutlineLocation) {
  if (!diagListEl || !diagCountEl) return;
  diagCountEl.textContent = String(diagnostics.length);
  diagCountEl.classList.toggle("empty", diagnostics.length === 0);
  if (diagCountClosedEl) {
    diagCountClosedEl.textContent = String(diagnostics.length);
    diagCountClosedEl.classList.toggle("empty", diagnostics.length === 0);
  }
  if (!diagnostics.length) {
    diagListEl.innerHTML = `<div class="e-empty-card"><strong>No problems</strong>Compile the project to check for errors.</div>`;
    return;
  }
  diagListEl.innerHTML = diagnostics.map(d => {
    const isWarn = d.severity === "warning";
    const icon = DIAG_ICONS[isWarn ? "warning" : "error"];
    const loc = `${escHtml(d.file)}:${d.line}${d.column ? ':' + d.column : ''}`;
    return `<button class="e-diag-item${isWarn ? " warn" : ""}" data-file="${escHtml(d.file)}" data-line="${d.line}" data-col="${d.column || 1}">` +
      `<span class="e-diag-icon">${icon}</span>` +
      `<span class="e-diag-msg">${escHtml(d.message)}</span>` +
      `<span class="e-diag-loc">${loc}</span>` +
      `</button>`;
  }).join("");
  diagListEl.querySelectorAll(".e-diag-item").forEach(el => {
    el.addEventListener("click", () => {
      openOutlineLocation(el.dataset.file, Number(el.dataset.line), Number(el.dataset.col || 1));
    });
  });
}

// ── Dialogs ───────────────────────────────────────────────────────────────────

export function showConfirm(msg) {
  return new Promise(resolve => {
    s._confirmResolve = resolve;
    const overlay = document.getElementById("confirm-overlay");
    const msgEl   = document.getElementById("confirm-msg");
    if (msgEl) msgEl.textContent = msg;
    overlay?.classList.add("open");
  });
}

export function showRenameDialog(fileName) {
  return new Promise(resolve => {
    s._renameResolve = resolve;
    const overlay = document.getElementById("rename-overlay");
    const input   = document.getElementById("rename-input");
    if (input) { input.value = fileName; }
    overlay?.classList.add("open");
    setTimeout(() => { input?.select(); }, 40);
  });
}

export function showProjectRenameDialog(currentTitle) {
  return new Promise(resolve => {
    s._renameProjectResolve = resolve;
    const overlay = document.getElementById("rename-project-overlay");
    const input   = document.getElementById("rename-project-input");
    if (input) { input.value = currentTitle; }
    overlay?.classList.add("open");
    setTimeout(() => { input?.select(); }, 40);
  });
}

export function showCreateEntryDialog(kind, opts = {}) {
  return new Promise(resolve => {
    s._createEntryResolve = resolve;
    const overlay    = document.getElementById("create-entry-overlay");
    const input      = document.getElementById("create-entry-input");
    const titleEl    = document.getElementById("create-entry-title");
    const hintEl     = document.getElementById("create-entry-hint");
    if (titleEl) titleEl.textContent = kind === "directory" ? "Нова папка" : "Новий файл";
    if (hintEl)  hintEl.textContent  = opts.hint || (kind === "directory" ? "Назва папки" : "Назва файлу (напр. chapter1.tex)");
    if (input)   input.value = opts.default || "";
    overlay?.classList.add("open");
    setTimeout(() => { input?.focus(); }, 40);
  });
}

// Wire up dialog buttons (called from main.js after DOM ready)
export function initDialogs() {
  // Confirm dialog
  document.getElementById("confirm-ok")?.addEventListener("click", () => {
    document.getElementById("confirm-overlay")?.classList.remove("open");
    s._confirmResolve?.(true);
    s._confirmResolve = null;
  });
  document.getElementById("confirm-cancel")?.addEventListener("click", () => {
    document.getElementById("confirm-overlay")?.classList.remove("open");
    s._confirmResolve?.(false);
    s._confirmResolve = null;
  });

  // Rename dialog
  const renameOverlay = document.getElementById("rename-overlay");
  document.getElementById("rename-ok")?.addEventListener("click", () => {
    const v = document.getElementById("rename-input")?.value?.trim() || "";
    renameOverlay?.classList.remove("open");
    s._renameResolve?.(v || null);
    s._renameResolve = null;
  });
  document.getElementById("rename-cancel")?.addEventListener("click", () => {
    renameOverlay?.classList.remove("open");
    s._renameResolve?.(null);
    s._renameResolve = null;
  });
  document.getElementById("rename-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("rename-ok")?.click();
    if (e.key === "Escape") document.getElementById("rename-cancel")?.click();
  });

  // Rename project dialog
  const renameProjectOverlay = document.getElementById("rename-project-overlay");
  document.getElementById("rename-project-ok")?.addEventListener("click", () => {
    const v = document.getElementById("rename-project-input")?.value?.trim() || "";
    renameProjectOverlay?.classList.remove("open");
    s._renameProjectResolve?.(v || null);
    s._renameProjectResolve = null;
  });
  document.getElementById("rename-project-cancel")?.addEventListener("click", () => {
    renameProjectOverlay?.classList.remove("open");
    s._renameProjectResolve?.(null);
    s._renameProjectResolve = null;
  });
  document.getElementById("rename-project-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("rename-project-ok")?.click();
    if (e.key === "Escape") document.getElementById("rename-project-cancel")?.click();
  });

  // Create entry dialog
  const createOverlay = document.getElementById("create-entry-overlay");
  document.getElementById("create-entry-ok")?.addEventListener("click", () => {
    const v = document.getElementById("create-entry-input")?.value?.trim() || "";
    createOverlay?.classList.remove("open");
    s._createEntryResolve?.(v || null);
    s._createEntryResolve = null;
  });
  document.getElementById("create-entry-cancel")?.addEventListener("click", () => {
    createOverlay?.classList.remove("open");
    s._createEntryResolve?.(null);
    s._createEntryResolve = null;
  });
  document.getElementById("create-entry-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("create-entry-ok")?.click();
    if (e.key === "Escape") document.getElementById("create-entry-cancel")?.click();
  });
}

// ── Resize handles ────────────────────────────────────────────────────────────

export function initResizeHandles() {
  const body = document.getElementById("e-body");
  if (!body) return;

  const LS_KEY = "editor-panel-widths";
  const MIN_W = 160;

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function applyWidths(lw, rw) {
    body.style.setProperty("--left-w", lw + "px");
    body.style.setProperty("--right-w", rw + "px");
  }

  function loadSaved() {
    try {
      const d = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
      if (d.left && d.right) applyWidths(d.left, d.right);
    } catch (_) {}
  }

  function savePanelWidths(lw, rw) {
    try { localStorage.setItem(LS_KEY, JSON.stringify({ left: lw, right: rw })); } catch (_) {}
  }

  loadSaved();

  function makeDragger(handleId, side) {
    const handle = document.getElementById(handleId);
    if (!handle) return;
    handle.addEventListener("mousedown", () => {
      handle.classList.add("dragging");
      const totalW = body.clientWidth;
      const computedLeft  = parseInt(getComputedStyle(body).getPropertyValue("--left-w"))  || 260;
      const computedRight = parseInt(getComputedStyle(body).getPropertyValue("--right-w")) || 420;

      function onMove(me) {
        const rect = body.getBoundingClientRect();
        const x = me.clientX - rect.left;
        if (side === "left") {
          const lw = clamp(x, MIN_W, totalW - MIN_W - computedRight - 2);
          applyWidths(lw, computedRight);
          savePanelWidths(lw, computedRight);
        } else {
          const rw = clamp(totalW - x, MIN_W, totalW - MIN_W - computedLeft - 2);
          if (s.pdfDoc) { import("./pdfviewer.js").then(m => m.renderPdfPages(true)); }
          applyWidths(computedLeft, rw);
          savePanelWidths(computedLeft, rw);
        }
      }
      function onUp() {
        handle.classList.remove("dragging");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  makeDragger("resize-left", "left");
  makeDragger("resize-right", "right");

  // Bottom panel controls: close, collapse, reopen, vertical resize
  const BOTTOM_LS_KEY = "editor-bottom-panel-height";
  const BOTTOM_MIN_H = 92;
  const BOTTOM_DEFAULT_H = 220;

  function getBottomHeight() {
    const raw = parseInt(getComputedStyle(bottomPanel || document.documentElement).getPropertyValue("--bottom-panel-h"), 10);
    return Number.isFinite(raw) ? raw : BOTTOM_DEFAULT_H;
  }

  function setBottomHeight(h) {
    if (!bottomPanel) return;
    const center = document.querySelector(".e-center");
    const maxH = Math.max(BOTTOM_MIN_H, Math.min(window.innerHeight * 0.6, (center?.clientHeight || window.innerHeight) - 150));
    const next = clamp(h, BOTTOM_MIN_H, maxH);
    bottomPanel.style.setProperty("--bottom-panel-h", next + "px");
    try { localStorage.setItem(BOTTOM_LS_KEY, String(next)); } catch (_) {}
  }

  try {
    const savedBottomH = parseInt(localStorage.getItem(BOTTOM_LS_KEY) || "", 10);
    if (Number.isFinite(savedBottomH)) setBottomHeight(savedBottomH);
  } catch (_) {}

  function setBottomOpen(open, tab = null) {
    if (!bottomPanel) return;
    bottomPanel.classList.toggle("open", !!open);
    if (!open) {
      bottomPanel.classList.remove("collapsed");
      bottomCollapseBtn?.setAttribute("aria-expanded", "false");
      return;
    }
    bottomPanel.classList.remove("collapsed");
    bottomCollapseBtn?.setAttribute("aria-expanded", "true");
    if (tab) switchBottomTab(tab);
  }

  bottomCollapseBtn?.addEventListener("click", e => {
    e.preventDefault();
    if (!bottomPanel) return;
    if (!bottomPanel.classList.contains("open")) {
      setBottomOpen(true);
      return;
    }
    const willCollapse = !bottomPanel.classList.contains("collapsed");
    bottomPanel.classList.toggle("collapsed", willCollapse);
    bottomCollapseBtn.setAttribute("aria-expanded", willCollapse ? "false" : "true");
  });

  bottomCloseBtn?.addEventListener("click", e => {
    e.preventDefault();
    setBottomOpen(false);
  });

  bottomOpener?.querySelectorAll("[data-open-bottom]").forEach(btn => {
    btn.addEventListener("click", e => {
      e.preventDefault();
      setBottomOpen(true, btn.dataset.openBottom || "problems");
    });
  });

  bottomResizeHandle?.addEventListener("mousedown", e => {
    if (!bottomPanel || bottomPanel.classList.contains("collapsed")) return;
    e.preventDefault();
    bottomPanel.classList.add("open");
    bottomResizeHandle.classList.add("dragging");
    const startY = e.clientY;
    const startH = bottomPanel.getBoundingClientRect().height || getBottomHeight();

    function onMove(me) {
      setBottomHeight(startH + (startY - me.clientY));
    }
    function onUp() {
      bottomResizeHandle.classList.remove("dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  // Outline vertical resize
  const outlineHandle = document.getElementById("resize-outline");
  const leftEl = document.querySelector(".e-left");
  if (outlineHandle && leftEl) {
    outlineHandle.addEventListener("mousedown", () => {
      outlineHandle.classList.add("dragging");
      const onMove = me => {
        const rect = leftEl.getBoundingClientRect();
        const h = Math.max(60, Math.min(rect.bottom - me.clientY, leftEl.clientHeight - 100));
        leftEl.style.setProperty("--outline-h", h + "px");
      };
      const onUp = () => {
        outlineHandle.classList.remove("dragging");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }
}

// ── Update line/col status from CodeMirror ────────────────────────────────────

export function updateLineCol(view) {
  if (!sbLineColEl || !view) return;
  const pos  = view.state.selection.main.head;
  const line = view.state.doc.lineAt(pos);
  const col  = pos - line.from + 1;
  sbLineColEl.textContent = `Ln ${line.number}, Col ${col}`;
}

// ── Diff renderer ─────────────────────────────────────────────────────────────

export function parseHunkStart(line) {
  const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
  if (!m) return null;
  return { oldLine: Number(m[1]), newLine: Number(m[2]) };
}

export function renderDiff(raw, opts = {}) {
  if (!raw || raw === "(no changes)") return { html: `<span class="diff-empty">Без змін</span>`, truncated: false };
  const maxRows = Number.isFinite(opts.maxRows) ? opts.maxRows : Infinity;
  const lines = raw.split("\n");
  let oldLine = 0, newLine = 0, addCount = 0, delCount = 0;
  const chunks = [];
  let pendingEmptyCtx = 0;
  const COLLAPSE_THRESH = 4;
  let renderedRows = 0, truncated = false;

  function canAppend(rows = 1) {
    if (renderedRows + rows <= maxRows) return true;
    truncated = true; return false;
  }
  function appendCtxRow(text, oldNo, newNo) {
    if (!canAppend(1)) return;
    chunks.push(
      `<div class="diff-row ctx"><span class="diff-sign"> </span>` +
      `<span class="diff-ln">${oldNo}</span><span class="diff-ln">${newNo}</span>` +
      `<span class="diff-code">${escHtml(text)}</span></div>`
    );
    renderedRows++;
  }
  function flushEmpty() {
    if (!pendingEmptyCtx) return;
    if (pendingEmptyCtx < COLLAPSE_THRESH) {
      const fO = oldLine - pendingEmptyCtx, fN = newLine - pendingEmptyCtx;
      for (let i = 0; i < pendingEmptyCtx; i++) { appendCtxRow("", fO + i, fN + i); if (truncated) break; }
      pendingEmptyCtx = 0; return;
    }
    if (!canAppend(1)) { pendingEmptyCtx = 0; return; }
    chunks.push(`<div class="diff-row ctx-empty"><span class="diff-sign">…</span><span class="diff-ln"></span><span class="diff-ln"></span><span class="diff-code">пропущено порожніх рядків: ${pendingEmptyCtx}</span></div>`);
    renderedRows++; pendingEmptyCtx = 0;
  }

  for (let li = 0; li < lines.length; li++) {
    if (truncated) break;
    const line = lines[li];
    const e = escHtml(line);

    if (line.startsWith("---") || line.startsWith("+++")) {
      flushEmpty(); if (truncated) break;
      if (!canAppend(1)) break;
      chunks.push(`<span class="diff-meta">${e}</span>`); renderedRows++; continue;
    }
    if (line.startsWith("@@")) {
      flushEmpty(); if (truncated) break;
      const hs = parseHunkStart(line);
      if (hs) { oldLine = hs.oldLine; newLine = hs.newLine; }
      if (!canAppend(1)) break;
      chunks.push(`<span class="diff-hunk">${e}</span>`); renderedRows++; continue;
    }
    if (line.startsWith("+")) {
      flushEmpty(); if (truncated) break;
      if (!canAppend(1)) break;
      chunks.push(`<div class="diff-row add"><span class="diff-sign">+</span><span class="diff-ln"></span><span class="diff-ln">${newLine++}</span><span class="diff-code">${escHtml(line.slice(1))}</span></div>`);
      renderedRows++; addCount++; continue;
    }
    if (line.startsWith("-")) {
      flushEmpty(); if (truncated) break;
      if (!canAppend(1)) break;
      chunks.push(`<div class="diff-row del"><span class="diff-sign">−</span><span class="diff-ln">${oldLine++}</span><span class="diff-ln"></span><span class="diff-code">${escHtml(line.slice(1))}</span></div>`);
      renderedRows++; delCount++; continue;
    }
    // context line
    if (!line.trim()) { pendingEmptyCtx++; oldLine++; newLine++; continue; }
    flushEmpty(); if (truncated) break;
    appendCtxRow(line.startsWith(" ") ? line.slice(1) : line, oldLine++, newLine++);
  }
  flushEmpty();

  const summary = `<div class="diff-summary"><span class="diff-add-count">+${addCount}</span><span class="diff-del-count">-${delCount}</span></div>`;
  const expandBtn = truncated
    ? `<div class="diff-row ctx" style="cursor:pointer;color:#7ab4f0" data-expand-diff="1">… показати всі рядки …</div>`
    : "";
  return { html: summary + chunks.join("") + expandBtn, truncated };
}
