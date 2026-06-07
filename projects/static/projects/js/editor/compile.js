import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as cm from "./cm.js";
import * as files from "./files.js";
import * as ui from "./ui.js";
import * as pdfviewer from "./pdfviewer.js";
import * as tinymist from "./tinymist.js";

const { s, cfg } = state;
const { api } = apiMod;
const { getContent, saveTabState, activateTab, hasTabState, getTabStateContent, setEditorDiagnostics, replaceContentPreservingViewport } = cm;
const { utf8ByteSize, refreshOpenAsset } = files;
const { setSaveHint, setCompileState, openLog, parseDiagnostics, renderDiagnostics } = ui;
const {
  loadPdfViewer,
  pdfEmpty,
  getPreviewMode,
  resyncTypstPreview,
  refreshTypstPreviewFromProjectUpdate,
  refreshTypstPreviewStatus,
  syncPreviewMemoryFile,
} = pdfviewer;

const openPdfLink = document.getElementById("open-pdf");
const logEl       = document.getElementById("log");

// openOutlineLocation is injected from main.js
let _openOutlineLocation = () => {};
export function setOutlineLocationRef(fn) { _openOutlineLocation = fn; }

function syncTabContent(name, text, filename) {
  if (!cm.view || !name) return;
  const targetFilename = filename || name;
  if (name === s.activeTabName) {
    if (!hasTabState(name)) {
      activateTab(name, text, targetFilename, true, false);
      return;
    }
    replaceContentPreservingViewport(text, name);
    return;
  }
  activateTab(name, text, targetFilename, true, !!s.activeTabName);
}

function syncTypstPreviewMemoryIfNeeded(filename, content) {
  const name = String(filename || "");
  if (s.projectMeta?.markup_type !== "typst" || !name.toLowerCase().endsWith(".typ")) return;
  if (getPreviewMode() !== "web") return;
  syncPreviewMemoryFile(name, String(content || ""));
}

function applyLocalWorkspaceUpdate(localWorkspace) {
  if (!s.projectMeta) s.projectMeta = {};
  s.projectMeta.local_workspace = localWorkspace || { active: false };
  import("./longdoc.js").then(m => m.applyProjectEditLock?.()).catch(() => {});
}

// ── Compile state helpers ─────────────────────────────────────────────────────

export function queueCompile(mode = "manual") {
  s.queuedCompileMode = mode;
}

export function shouldOpenCompileLog(mode = "manual") {
  return mode === "manual";
}

// ── Compile artifacts ─────────────────────────────────────────────────────────

export function updateCompileArtifacts(logText = "", compilePayload = null) {
  s.diagnostics = Array.isArray(compilePayload?.diagnostics) && compilePayload.diagnostics.length
    ? compilePayload.diagnostics
    : parseDiagnostics(logText);
  const blockingDiagnostics = s.diagnostics.filter(item => String(item?.severity || "error").toLowerCase() !== "warning");
  renderDiagnostics(s.diagnostics, _openOutlineLocation);
  setEditorDiagnostics(s.activeTabName || s.selectedFile?.name || "", s.diagnostics);

  if (!logText.trim() && s.diagnostics.length === 0) {
    pdfEmpty.innerHTML = `
      <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      <div><strong style="display:block;color:var(--e-text);margin-bottom:6px;">Preview not generated yet</strong><span class="e-state-copy">Compile the project to generate the first PDF.</span></div>`;
  } else if (s.compileState === "failed" && blockingDiagnostics.length) {
    pdfEmpty.innerHTML = `
      <svg viewBox="0 0 24 24"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>
      <div><strong style="display:block;color:var(--e-text);margin-bottom:6px;">Preview blocked by diagnostics</strong><span class="e-state-copy">Review the structured diagnostics or raw log, then compile again.</span></div>`;
  } else if (s.compileState === "out_of_date") {
    pdfEmpty.innerHTML = `
      <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      <div><strong style="display:block;color:var(--e-text);margin-bottom:6px;">Preview is out of date</strong><span class="e-state-copy">There are unsynced changes waiting for the next compile.</span></div>`;
  }
  refreshTypstPreviewStatus();
}

// ── Save ──────────────────────────────────────────────────────────────────────

export async function saveCurrentFile(opts = {}) {
  if (cfg.sessionReview) {
    setSaveHint("Session review is read-only", "error");
    return;
  }
  if (s.longdoc.activeSession) {
    setSaveHint("Read-only: AI session active", "error");
    return;
  }
  const targetName = String(opts.targetName || s.activeTabName || s.selectedFile?.name || "");
  if (!targetName) return;
  const targetFile = targetName === s.mainFileName
    ? { name: targetName, type: "main", is_text: true, is_dir: false }
    : (
      s.projectFiles.find(item => item.name === targetName)
      || (s.selectedFile?.name === targetName ? s.selectedFile : null)
      || { name: targetName, type: "asset", is_text: true, is_dir: false }
    );
  if (!targetFile.is_text || targetFile.is_dir) return;
  if (s.saving) {
    s.saveQueued = true;
    return;
  }
  s.saveQueued = false;
  s.saving = true;
  const saveGeneration = Number.isFinite(opts.generation) ? opts.generation : s.editGeneration;
  setSaveHint("Збереження…", "saving");
  try {
    const content = typeof opts.contentSnapshot === "string"
      ? opts.contentSnapshot
      : targetName === s.activeTabName
        ? getContent()
        : getTabStateContent(targetName);
    if (typeof content !== "string") return;
    if (targetName === s.mainFileName) {
      await api(`/api/projects/${cfg.projectId}/file/`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      s.mainFileContent = content;
    } else {
      await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(targetName)}/content/`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      const savedSize = utf8ByteSize(content);
      if (s.selectedFile?.name === targetName) {
        s.selectedFile = { ...s.selectedFile, size: savedSize };
      }
      s.projectFiles   = s.projectFiles.map(item =>
        item.name === targetName ? { ...item, size: savedSize } : item
      );
    }
    tinymist.invalidateCitationIndex(targetName);
    saveTabState(targetName);
    const settled = s.editGeneration === saveGeneration;
    s.hasUnsavedChanges = !settled;
    if (settled) {
      setSaveHint("Збережено", "saved");
      setCompileState("out_of_date", "pending");
    } else {
      setSaveHint("Є незбережені зміни…", "saving");
    }
    import("./app.js").then(m => m.renderEditorTabs?.()).catch(() => {});
    const { renderFileList } = await import("./files.js");
    renderFileList();
    import("./app.js").then(m => m.loadVersions(true)).catch(() => {});
    if (settled) {
      const shouldCompile = s.pendingRealtimeCompile;
      const shouldRefreshSections = s.pendingSectionsRefresh && targetName === s.mainFileName;
      s.pendingRealtimeCompile = false;
      s.pendingSectionsRefresh = false;
      if (s.projectMeta?.markup_type === "typst" && String(targetName).toLowerCase().endsWith(".typ")) {
        tinymist.didSave(targetName);
        if (getPreviewMode() === "web") resyncTypstPreview({ reveal: false });
      }
      if (shouldCompile) {
        runCompile("realtime").catch(() => {});
      }
      if (shouldRefreshSections) {
        import("./app.js").then(m => m.loadSections?.()).catch(() => {});
      }
    }
  } catch (err) {
    setSaveHint(`Помилка: ${err.message}`, "error");
  } finally {
    s.saving = false;
    if (s.saveQueued || s.hasUnsavedChanges) {
      s.saveQueued = false;
      clearTimeout(s.saveTimer);
      s.saveTimer = setTimeout(() => {
        saveCurrentFile().catch(() => {});
      }, 120);
    }
  }
}

export async function waitUntilSaveIdle(maxWaitMs = 4000) {
  const startedAt = Date.now();
  while (s.saving && Date.now() - startedAt < maxWaitMs) {
    await new Promise(res => setTimeout(res, 60));
  }
}

// ── Compile ───────────────────────────────────────────────────────────────────

export async function runCompile(mode = "manual") {
  if (cfg.sessionReview) {
    setSaveHint("Session review is read-only", "error");
    return;
  }
  if (s.compileInFlight) { queueCompile(mode); return; }
  s.compileInFlight = true;
  setCompileState("compiling", "pending");

  if (shouldOpenCompileLog(mode)) {
    if (logEl) logEl.textContent = "Компіляція…";
    openLog();
  } else {
    setSaveHint("Typst: автокомпіляція…", "saving");
  }

  try {
    await waitUntilSaveIdle();
    await saveCurrentFile();
    const useLocalRuntime = Boolean(s.projectMeta?.local_runtime?.enabled) && s.projectMeta?.markup_type === "typst";
    if (useLocalRuntime) {
      setSaveHint("Локальна компіляція…", "saving");
    }
    const data = await api(`/api/projects/${cfg.projectId}/compile/`, { method: "POST" });
    setCompileState(data.compile_state || (data.status === "success" ? "synced" : "failed"), data.status);
    if (logEl) logEl.textContent = data.log || "";
    updateCompileArtifacts(data.log || "", data);
    if (data.pdf_url) {
      s.lastPdfVersion = data.pdf_version ?? Date.now();
      const url = `${data.pdf_url}?t=${s.lastPdfVersion}`;
      if (openPdfLink) openPdfLink.href = data.pdf_url;
      await loadPdfViewer(url);
      if (s.projectMeta?.markup_type === "typst" && getPreviewMode() === "web") {
        resyncTypstPreview({ reveal: false });
      }
    } else {
      pdfEmpty.style.display = "flex";
    }
    if (!shouldOpenCompileLog(mode) && data.status === "success") {
      setSaveHint("Typst: PDF оновлено", "saved");
    }
  } catch (err) {
    setCompileState("failed", "error");
    if (logEl) logEl.textContent = `Помилка: ${err.message}`;
    updateCompileArtifacts(String(err.message || ""));
    if (!shouldOpenCompileLog(mode)) setSaveHint(`Typst: помилка компіляції`, "error");
  } finally {
    s.compileInFlight = false;
    if (s.queuedCompileMode) {
      const next = s.queuedCompileMode; s.queuedCompileMode = null;
      runCompile(next).catch(() => {});
    }
  }
}

export async function compileProject() { await runCompile("manual"); }

// ── Polling ───────────────────────────────────────────────────────────────────

export async function pollCompileStatus() {
  try {
    const d = await api(`/api/projects/${cfg.projectId}/compile/`, { method: "GET" });
    setCompileState(d.compile_state || "out_of_date", d.status);
    updateCompileArtifacts(d.log || "", d);
    if (d.pdf_url && d.pdf_version && d.pdf_version !== s.lastPdfVersion) {
      s.lastPdfVersion = d.pdf_version;
      if (openPdfLink) openPdfLink.href = d.pdf_url;
      await loadPdfViewer(`${d.pdf_url}?t=${s.lastPdfVersion}`);
      if (s.projectMeta?.markup_type === "typst" && getPreviewMode() === "web") {
        resyncTypstPreview({ reveal: false });
      }
    }
    if (!d.pdf_url) pdfEmpty.style.display = "flex";
  } catch (_) {}
}

export async function pollUntilCompileDone(maxMs = 45000, stepMs = 600) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    const d = await api(`/api/projects/${cfg.projectId}/compile/`, { method: "GET" });
    setCompileState(d.compile_state || "out_of_date", d.status);
    if (logEl && d.log) logEl.textContent = d.log;
    updateCompileArtifacts(d.log || "", d);
    if (d.pdf_url && d.pdf_version && d.pdf_version !== s.lastPdfVersion) {
      s.lastPdfVersion = d.pdf_version;
      if (openPdfLink) openPdfLink.href = d.pdf_url;
      await loadPdfViewer(`${d.pdf_url}?t=${s.lastPdfVersion}`);
      if (s.projectMeta?.markup_type === "typst" && getPreviewMode() === "web") {
        resyncTypstPreview({ reveal: false });
      }
    }
    if (!d.pdf_url) pdfEmpty.style.display = "flex";
    if (d.compile_state && d.compile_state !== "out_of_date") {
      setSaveHint(d.status === "success" ? "MCP: PDF оновлено" : "MCP: помилка компіляції",
        d.status === "success" ? "saved" : "error");
      return;
    }
    await new Promise(res => setTimeout(res, stepMs));
  }
  setSaveHint("MCP: компіляція не завершена (таймаут)", "error");
}

// ── SSE (MCP project updates) ─────────────────────────────────────────────────

async function handleMcpUpdate() {
  setSaveHint("Проєкт оновлено через MCP. Оновлюємо…", "saving");
  const { loadMainFile, loadFiles, loadVersions } = await import("./app.js");
  try {
    if (!s.hasUnsavedChanges && !s.saving) {
      await loadMainFile();
      syncTypstPreviewMemoryIfNeeded(s.mainFileName, s.mainFileContent);
      // Refresh non-main text file if currently open
      if (s.selectedFile?.is_text && !s.selectedFile?.is_dir && s.selectedFile?.name !== s.mainFileName) {
        try {
          const params = new URLSearchParams({ include_text: "1" });
          const fd = await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(s.selectedFile.name)}/content/?${params}`);
          syncTabContent(s.selectedFile.name, fd.text_content || "", s.selectedFile.name);
          syncTypstPreviewMemoryIfNeeded(s.selectedFile.name, fd.text_content || "");
          s.hasUnsavedChanges = false;
        } catch (_) {}
      }
    }
  } catch (_) {}
  try {
    await Promise.all([loadFiles(), loadVersions(true)]);
    refreshOpenAsset();
    await pollUntilCompileDone();
    await refreshTypstPreviewFromProjectUpdate();
  } catch (err) {
    setSaveHint(`MCP: помилка оновлення: ${err.message}`, "error");
  }
}

async function handleProjectUpdate(source = "web") {
  const label = source === "mcp" ? "MCP" : "Проєкт";
  setSaveHint(`${label}: оновлюємо стан…`, "saving");
  try {
    const main = await import("./app.js");
    await main.loadProjectMeta();
    // Only reload file content from server when there are no local edits in flight.
    // syncTabContent is a no-op when content is identical, so the cursor never
    // jumps in the normal post-compile case where content is already in sync.
    if (!s.hasUnsavedChanges && !s.saving) {
      await main.loadMainFile();
      syncTypstPreviewMemoryIfNeeded(s.mainFileName, s.mainFileContent);
      if (s.selectedFile?.is_text && !s.selectedFile?.is_dir && s.selectedFile?.name !== s.mainFileName) {
        try {
          const params = new URLSearchParams({ include_text: "1" });
          const fd = await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(s.selectedFile.name)}/content/?${params}`);
          syncTabContent(s.selectedFile.name, fd.text_content || "", s.selectedFile.name);
          syncTypstPreviewMemoryIfNeeded(s.selectedFile.name, fd.text_content || "");
          s.hasUnsavedChanges = false;
        } catch (_) {}
      }
    }
    await Promise.all([
      main.loadFiles(),
      main.loadSections(),
      main.loadVersions(true),
      import("./longdoc.js").then(m => m.loadLongdocData()),
    ]);
    refreshOpenAsset();
    if (source === "mcp") {
      await pollUntilCompileDone();
      await refreshTypstPreviewFromProjectUpdate();
      return;
    }
    await main.refreshLivePdfPreview();
    setSaveHint("Проєкт: стан оновлено", "saved");
  } catch (err) {
    setSaveHint(`${label}: помилка оновлення: ${err.message}`, "error");
  }
}

export function connectProjectUpdatesSse() {
  if (s.projectSse) {
    try { s.projectSse.close(); } catch (_) {}
    s.projectSse = null;
  }

  if (typeof EventSource !== "function") {
    setSaveHint("SSE недоступний у цьому браузері. Оновлення MCP перевірятимуться періодично.", "error");
    return;
  }

  const url = `/sse/projects/${cfg.projectId}/updates/`;
  const es = new EventSource(url);
  s.projectSse = es;

  es.addEventListener("message", ev => {
    let data = null;
    try { data = JSON.parse(ev.data || "{}"); } catch (_) { return; }
    if (!data || typeof data !== "object") return;
    if (data.type === "connected") {
      s.lastSeenMcpVersionId = Number(data.latest_project_version_id || 0);
      applyLocalWorkspaceUpdate(data.local_workspace);
      return;
    }
    if (data.type === "local_workspace_updated") {
      applyLocalWorkspaceUpdate(data.local_workspace);
      import("./app.js").then(m => m.loadProjectMeta?.()).catch(() => {});
      return;
    }
    if (data.type === "compile_updated") {
      setCompileState(data.compile_state || "out_of_date", data.status);
      updateCompileArtifacts(data.log || "", data);
      if (data.pdf_url && data.pdf_version && data.pdf_version !== s.lastPdfVersion) {
        s.lastPdfVersion = data.pdf_version;
        if (openPdfLink) openPdfLink.href = data.pdf_url;
        loadPdfViewer(`${data.pdf_url}?t=${s.lastPdfVersion}`).catch(() => {});
        if (s.projectMeta?.markup_type === "typst" && getPreviewMode() === "web") {
          resyncTypstPreview({ reveal: false });
        }
      }
      if (!data.pdf_url) pdfEmpty.style.display = "flex";
      return;
    }
    if (data.type === "proposal_updated") {
      import("./longdoc.js").then(m => m.loadLongdocData?.()).catch(() => {});
      import("./app.js").then(m => m.loadProjectMeta?.()).catch(() => {});
      return;
    }
    if (data.type === "longdoc_updated") {
      import("./longdoc.js").then(m => m.loadLongdocData?.()).catch(() => {});
      return;
    }
    if (data.type !== "project_updated") return;
    const incoming = Number(data.version_id || 0);
    if (!incoming || incoming <= s.lastSeenMcpVersionId) return;
    s.lastSeenMcpVersionId = incoming;
    if (data.source === "mcp") {
      if (s.hasUnsavedChanges || s.saving) {
        setSaveHint("Проєкт змінено через MCP. Список файлів оновлено; збережіть локальні правки.", "error");
        import("./app.js").then(({ loadFiles, loadVersions }) =>
          Promise.all([loadFiles(), loadVersions(true)])
        ).then(() => refreshOpenAsset()).catch(() => {});
        return;
      }
      handleMcpUpdate().catch(() => {});
      return;
    }
    handleProjectUpdate(data.source || "web").catch(() => {});
  });

  es.addEventListener("error", () => {
    if (es.readyState === EventSource.CLOSED) {
      s.projectSse = null;
      setSaveHint("SSE-оновлення проєкту закрито. Періодична перевірка лишається активною.", "error");
    }
  });
}

// ── Project-level operations ──────────────────────────────────────────────────

const projectTitleEl = document.getElementById("project-title");

export async function deleteCurrentProject() {
  const { showConfirm } = await import("./ui.js");
  const ok = await showConfirm("Видалити цей проєкт? Дію не можна скасувати.");
  if (!ok) return;
  try {
    const r = await fetch(`/api/projects/${cfg.projectId}/`, { method: "DELETE", credentials: "same-origin" });
    if (!r.ok) throw new Error("Не вдалося видалити проєкт");
    window.location.href = "/";
  } catch (err) {
    setSaveHint(`Помилка: ${err.message}`, "error");
  }
}

export async function renameCurrentProject() {
  const { showProjectRenameDialog } = await import("./ui.js");
  const currentTitle = String(projectTitleEl?.textContent || "").trim();
  const newTitle = await showProjectRenameDialog(currentTitle);
  if (!newTitle) return;
  const trimmed = newTitle.trim();
  if (!trimmed || trimmed === currentTitle) return;
  try {
    const payload = await api(`/api/projects/${cfg.projectId}/`, {
      method: "PATCH",
      body: JSON.stringify({ title: trimmed }),
    });
    const title = String(payload?.title || trimmed);
    if (projectTitleEl) projectTitleEl.textContent = title;
    document.title = `${title} — SmartTeX`;
    setSaveHint("Назву проєкту оновлено", "saved");
  } catch (err) {
    setSaveHint(`Помилка: ${err.message}`, "error");
  }
}

export function openCreateTemplateDialog() {
  const overlay  = document.getElementById("create-template-overlay");
  const input    = document.getElementById("ct-title-input");
  const msg      = document.getElementById("ct-dialog-msg");
  const submit   = document.getElementById("ct-dialog-submit");
  const cancel   = document.getElementById("ct-dialog-cancel");
  const closeBtn = document.getElementById("ct-dialog-close");
  if (!overlay) return;

  if (msg)    msg.textContent = "";
  if (input)  input.value = "";
  if (submit) submit.disabled = false;
  overlay.style.display = "flex";
  input?.focus();

  function close() {
    overlay.style.display = "none";
    submit?.removeEventListener("click", onSubmit);
    cancel?.removeEventListener("click", close);
    closeBtn?.removeEventListener("click", close);
    overlay.removeEventListener("click", onOverlayClick);
  }

  function onOverlayClick(e) { if (e.target === overlay) close(); }

  async function onSubmit() {
    const title    = (input?.value || "").trim() || String(projectTitleEl?.textContent || "").trim();
    const category = document.getElementById("ct-category-select")?.value || "other";
    if (submit) submit.disabled = true;
    if (msg)    msg.textContent = "Створення…";
    try {
      const result = await api(`/api/projects/${cfg.projectId}/create-template/`, {
        method: "POST",
        body: JSON.stringify({ title, category }),
      });
      if (msg) msg.textContent = `Шаблон «${result?.title || title}» створено успішно.`;
      if (submit) submit.textContent = "Готово";
      setTimeout(close, 1800);
    } catch (err) {
      if (msg) msg.textContent = `Помилка: ${err.message}`;
      if (submit) submit.disabled = false;
    }
  }

  submit?.addEventListener("click", onSubmit);
  cancel?.addEventListener("click", close);
  closeBtn?.addEventListener("click", close);
  overlay.addEventListener("click", onOverlayClick);
}
