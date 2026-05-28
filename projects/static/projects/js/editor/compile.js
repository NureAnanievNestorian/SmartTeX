import { s, cfg } from "./state.js";
import { api } from "./api.js";
import { getContent, saveTabState, replaceTabContent } from "./cm.js";
import { utf8ByteSize } from "./files.js";
import {
  setSaveHint, setCompileState, openLog, parseDiagnostics, renderDiagnostics,
} from "./ui.js";
import { loadPdfViewer, pdfEmpty } from "./pdfviewer.js";

const openPdfLink = document.getElementById("open-pdf");
const logEl       = document.getElementById("log");

// openOutlineLocation is injected from main.js
let _openOutlineLocation = () => {};
export function setOutlineLocationRef(fn) { _openOutlineLocation = fn; }

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
  renderDiagnostics(s.diagnostics, _openOutlineLocation);

  if (!logText.trim() && s.diagnostics.length === 0) {
    pdfEmpty.innerHTML = `
      <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      <div><strong style="display:block;color:var(--e-text);margin-bottom:6px;">Preview not generated yet</strong><span class="e-state-copy">Compile the project to generate the first PDF.</span></div>`;
  } else if (s.diagnostics.length) {
    pdfEmpty.innerHTML = `
      <svg viewBox="0 0 24 24"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>
      <div><strong style="display:block;color:var(--e-text);margin-bottom:6px;">Preview blocked by diagnostics</strong><span class="e-state-copy">Review the structured diagnostics or raw log, then compile again.</span></div>`;
  } else if (s.compileState === "out_of_date") {
    pdfEmpty.innerHTML = `
      <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      <div><strong style="display:block;color:var(--e-text);margin-bottom:6px;">Preview is out of date</strong><span class="e-state-copy">There are unsynced changes waiting for the next compile.</span></div>`;
  }
}

// ── Save ──────────────────────────────────────────────────────────────────────

export async function saveCurrentFile() {
  if (cfg.sessionReview) {
    setSaveHint("Session review is read-only", "error");
    return;
  }
  if (s.longdoc.activeSession) {
    setSaveHint("Read-only: AI session active", "error");
    return;
  }
  if (s.saving || !s.selectedFile.is_text || s.selectedFile.is_dir) return;
  s.saving = true;
  setSaveHint("Збереження…", "saving");
  try {
    const content = getContent();
    if (s.selectedFile.name === s.mainFileName) {
      await api(`/api/projects/${cfg.projectId}/file/`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      s.mainFileContent = content;
    } else {
      await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(s.selectedFile.name)}/content/`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      const savedSize = utf8ByteSize(content);
      s.selectedFile   = { ...s.selectedFile, size: savedSize };
      s.projectFiles   = s.projectFiles.map(item =>
        item.name === s.selectedFile.name ? { ...item, size: savedSize } : item
      );
    }
    saveTabState(s.selectedFile.name);
    s.hasUnsavedChanges = false;
    setSaveHint("Збережено", "saved");
    setCompileState("out_of_date", "pending");
    import("./main.js").then(m => m.renderEditorTabs?.()).catch(() => {});
    const { renderFileList } = await import("./files.js");
    renderFileList();
    import("./main.js").then(m => m.loadVersions(true)).catch(() => {});
  } catch (err) {
    setSaveHint(`Помилка: ${err.message}`, "error");
  } finally {
    s.saving = false;
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
    const data = await api(`/api/projects/${cfg.projectId}/compile/`, { method: "POST" });
    setCompileState(data.compile_state || (data.status === "success" ? "synced" : "failed"), data.status);
    if (logEl) logEl.textContent = data.log || "";
    updateCompileArtifacts(data.log || "", data);
    if (data.pdf_url) {
      s.lastPdfVersion = data.pdf_version ?? Date.now();
      const url = `${data.pdf_url}?t=${s.lastPdfVersion}`;
      if (openPdfLink) openPdfLink.href = data.pdf_url;
      await loadPdfViewer(url);
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
  try {
    const { loadMainFile, loadFiles, loadVersions } = await import("./main.js");
    await loadMainFile();
    // Refresh non-main text file if currently open
    if (s.selectedFile?.is_text && !s.selectedFile?.is_dir && s.selectedFile?.name !== s.mainFileName) {
      try {
        const params = new URLSearchParams({ include_text: "1" });
        const fd = await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(s.selectedFile.name)}/content/?${params}`);
        replaceTabContent(s.selectedFile.name, fd.text_content || "", s.selectedFile.name);
        s.hasUnsavedChanges = false;
      } catch (_) {}
    }
    await Promise.all([loadFiles(), loadVersions(true)]);
    await pollUntilCompileDone();
  } catch (err) {
    setSaveHint(`MCP: помилка оновлення: ${err.message}`, "error");
  }
}

async function handleProjectUpdate(source = "web") {
  const label = source === "mcp" ? "MCP" : "Проєкт";
  setSaveHint(`${label}: оновлюємо стан…`, "saving");
  try {
    const main = await import("./main.js?v=20260529-ui6");
    await main.loadProjectMeta();
    await main.loadMainFile();
    if (s.selectedFile?.is_text && !s.selectedFile?.is_dir && s.selectedFile?.name !== s.mainFileName) {
      try {
        const params = new URLSearchParams({ include_text: "1" });
        const fd = await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(s.selectedFile.name)}/content/?${params}`);
        replaceTabContent(s.selectedFile.name, fd.text_content || "", s.selectedFile.name);
        s.hasUnsavedChanges = false;
      } catch (_) {}
    }
    await Promise.all([
      main.loadFiles(),
      main.loadSections(),
      main.loadVersions(true),
      import("./longdoc.js?v=20260529-ui6").then(m => m.loadLongdocData()),
    ]);
    if (source === "mcp") {
      await pollUntilCompileDone();
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
      return;
    }
    if (data.type === "proposal_updated") {
      import("./longdoc.js?v=20260529-ui6").then(m => m.loadLongdocData?.()).catch(() => {});
      import("./main.js?v=20260529-ui6").then(m => m.loadProjectMeta?.()).catch(() => {});
      return;
    }
    if (data.type !== "project_updated") return;
    const incoming = Number(data.version_id || 0);
    if (!incoming || incoming <= s.lastSeenMcpVersionId) return;
    s.lastSeenMcpVersionId = incoming;
    if (s.hasUnsavedChanges && data.source === "mcp") {
      setSaveHint("Проєкт змінено через MCP. Спершу збережіть локальні правки або перезавантажте сторінку.", "error");
      return;
    }
    if (data.source === "mcp") {
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
