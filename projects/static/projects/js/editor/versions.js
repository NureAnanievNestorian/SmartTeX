import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as ui from "./ui.js";

const { s, cfg } = state;
const { api } = apiMod;
const { escHtml, fmtDate, setSaveHint, setCompileState, renderDiff, showConfirm } = ui;

const versionsListEl       = document.getElementById("versions-list");
const diffModalOverlay     = document.getElementById("diff-modal-overlay");
const diffModalTitle       = document.getElementById("diff-modal-title");
const diffModalMeta        = document.getElementById("diff-modal-meta");
const diffModalContent     = document.getElementById("diff-modal-content");
const diffModalRollbackBtn = document.getElementById("diff-modal-rollback");
const diffModalCloseBtn    = document.getElementById("diff-modal-close");
const historyFilterBtn     = document.getElementById("history-file-filter-btn");

// ── Operation label map ───────────────────────────────────────────────────────

const OP_LABELS = {
  update_project_file:    "Edited",
  update_project_asset:   "Edited",
  write_project_window:   "MCP edit",
  insert_project_section: "MCP insert",
  update_project_section: "MCP section",
  create_project:         "Created",
  create_project_file:    "File created",
  upload_project_file:    "Uploaded",
  upload_project_asset:   "Uploaded",
  create_project_folder:  "Folder created",
  delete_project_file:    "Deleted",
  delete_project_asset:   "Deleted",
  rename_project_file:    "Renamed",
  rename_project_asset:   "Renamed",
  rollback:               "Rollback",
  compile_project:        "Compiled",
  compile:                "Compiled",
};

function humanOp(op) {
  if (OP_LABELS[op]) return OP_LABELS[op];
  // Strip common prefixes and make readable
  return String(op || "")
    .replace(/^(create|update|delete|rename|upload)_project_/, "$1 ")
    .replace(/_/g, " ")
    .replace(/^\w/, c => c.toUpperCase());
}

function fmtShort(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso || "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleString("uk-UA", { hour: "2-digit", minute: "2-digit" });
  }
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleString("uk-UA", { day: "2-digit", month: "short" });
  }
  return d.toLocaleString("uk-UA", { day: "2-digit", month: "short", year: "2-digit" });
}

// ── Source badge ──────────────────────────────────────────────────────────────

const SOURCE_LABELS = { mcp: "MCP", web: "Web", api: "API" };

// ── Filter state ──────────────────────────────────────────────────────────────

function updateFilterBtn() {
  if (!historyFilterBtn) return;
  const active = Boolean(s.versionsFileFilter);
  historyFilterBtn.classList.toggle("active", active);
  historyFilterBtn.title = active
    ? `Showing history for: ${s.versionsFileFilter} — click to show all`
    : "Filter to current file only";
}

export function closeDiffModal() {
  diffModalOverlay?.classList.remove("open");
  s.activeDiffVersionId = null;
}

// ── List rendering ────────────────────────────────────────────────────────────

export function renderVersions() {
  if (!versionsListEl) return;
  versionsListEl.innerHTML = "";

  s.versions.forEach(v => {
    const wrap = document.createElement("div");
    wrap.className = `e-ver-item${v.id === s.activeDiffVersionId ? " active" : ""}${v._loading ? " is-loading" : ""}`;

    const fileLabel = v.target_file ? escHtml(v.target_file) : "";
    const srcLabel  = (SOURCE_LABELS[v.source] && v.source !== "web") ? SOURCE_LABELS[v.source] : "";
    const tipParts  = [humanOp(v.operation), fileLabel, fmtDate(v.created_at)];
    if (v.summary) tipParts.push(v.summary);

    const head = document.createElement("div");
    head.className = "e-ver-head";
    head.title = tipParts.filter(Boolean).join(" · ");
    head.innerHTML =
      `<span class="e-ver-num">#${v.number ?? v.id}</span>` +
      `<span class="e-ver-op">${escHtml(humanOp(v.operation))}</span>` +
      (fileLabel ? `<span class="e-ver-file">${fileLabel}</span>` : `<span class="e-ver-file" style="color:var(--e-muted)">—</span>`) +
      (srcLabel ? `<span class="e-ver-src">${escHtml(srcLabel)}</span>` : "") +
      `<span class="e-ver-date">${escHtml(fmtShort(v.created_at))}</span>`;
    head.addEventListener("click", () => openDiffModal(v.id));
    wrap.appendChild(head);
    versionsListEl.appendChild(wrap);
  });

  if (s.versionsLoading || s.versionsHasMore) {
    const d = document.createElement("div");
    d.className   = "e-empty-msg";
    d.style.cssText = "padding:10px;font-size:11px;color:var(--e-muted);text-align:center";
    d.textContent = s.versionsLoading ? "Завантаження…" : "Прокрутіть нижче для підвантаження";
    versionsListEl.appendChild(d);
  }

  if (!s.versionsLoading && s.versions.length === 0) {
    const d = document.createElement("div");
    d.className = "e-empty-msg";
    d.style.cssText = "padding:16px 12px;font-size:12px;color:var(--e-muted);text-align:center";
    d.textContent = s.versionsFileFilter ? "Немає версій для цього файлу" : "Немає версій";
    versionsListEl.appendChild(d);
  }
}

// ── Diff modal ────────────────────────────────────────────────────────────────

export async function openDiffModal(vid) {
  s.activeDiffVersionId    = vid;
  s.activeDiffRenderFull   = false;
  const v = s.versions.find(x => x.id === vid);
  if (!v) return;

  const opLabel   = humanOp(v.operation);
  const fileLabel = v.target_file || "";

  if (diffModalTitle) diffModalTitle.textContent = `${opLabel}${fileLabel ? " — " + fileLabel : ""}`;
  if (diffModalMeta)  diffModalMeta.textContent  = `#${v.number ?? v.id} · ${fmtDate(v.created_at)}${v.summary ? " · " + v.summary : ""}`;
  if (diffModalContent) diffModalContent.innerHTML = `<span class="diff-empty">Завантаження…</span>`;
  if (diffModalRollbackBtn) {
    diffModalRollbackBtn.disabled = !v.is_revertible;
    diffModalRollbackBtn.title    = v.is_revertible ? "" : "Цю подію не можна відкотити автоматично";
    diffModalRollbackBtn.onclick  = v.is_revertible ? () => rollbackVersion(v.id) : null;
    diffModalRollbackBtn.textContent = fileLabel
      ? `Відновити ${fileLabel} до цієї версії`
      : "Відкотити до цієї версії";
  }
  diffModalOverlay?.classList.add("open");

  if (v._diff === undefined) {
    v._loading = true; renderVersions();
    try {
      const d = await api(`/api/projects/${cfg.projectId}/versions/${vid}/`, { method: "GET" });
      v._diff = d.diff || "(no changes)";
    } catch (err) { v._diff = `Помилка: ${err.message}`; }
    v._loading = false; renderVersions();
  }

  if (s.activeDiffVersionId !== vid) return;
  const render = renderDiff(v._diff || "", { maxRows: s.activeDiffRenderFull ? Infinity : 900 });
  if (diffModalContent) {
    diffModalContent.innerHTML = render.html;
    const expandBtn = diffModalContent.querySelector("[data-expand-diff='1']");
    if (expandBtn) {
      expandBtn.addEventListener("click", () => {
        s.activeDiffRenderFull = true;
        if (diffModalContent) diffModalContent.innerHTML = renderDiff(v._diff || "", { maxRows: Infinity }).html;
      });
    }
  }
}

// ── Rollback ──────────────────────────────────────────────────────────────────

export async function rollbackVersion(vid) {
  const v    = s.versions.find(x => x.id === vid);
  const vnum = v?.number ?? vid;
  const file = v?.target_file || "";
  const label = file ? `файл "${file}"` : "проєкт";
  const ok = await showConfirm(`Відновити ${label} до стану версії #${vnum}? Поточні зміни будуть замінені.`);
  if (!ok) return;
  try {
    await api(`/api/projects/${cfg.projectId}/versions/${vid}/rollback/`, {
      method: "POST",
      body: JSON.stringify({ summary: `Rollback to version ${vnum}` }),
    });
    closeDiffModal();
    const { loadMainFile, loadSections, loadVersions } = await import("./app.js");
    await loadMainFile();
    await Promise.all([loadSections(), loadVersions(true)]);
    setSaveHint(`Відновлено до версії #${vnum}`, "saved");
    setCompileState("out_of_date", "pending");
  } catch (err) { setSaveHint(`Помилка: ${err.message}`, "error"); }
}

// ── Panel init ────────────────────────────────────────────────────────────────

export function initVersionsPanel() {
  diffModalCloseBtn?.addEventListener("click", closeDiffModal);
  diffModalOverlay?.addEventListener("click", e => {
    if (e.target === diffModalOverlay) closeDiffModal();
  });
  versionsListEl?.addEventListener("scroll", () => {
    const nearBottom = versionsListEl.scrollTop + versionsListEl.clientHeight >= versionsListEl.scrollHeight - 120;
    if (nearBottom) import("./app.js").then(m => m.loadVersions(false)).catch(() => {});
  });

  historyFilterBtn?.addEventListener("click", async () => {
    const { loadVersions } = await import("./app.js");
    if (s.versionsFileFilter) {
      s.versionsFileFilter = null;
    } else {
      s.versionsFileFilter = s.selectedFile?.name || null;
    }
    updateFilterBtn();
    await loadVersions(true);
  });
}
