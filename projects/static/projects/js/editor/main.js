import { s, cfg } from "./state.js";
import { api } from "./api.js";
import {
  initCodeMirror, switchLanguage,
  focusEditor, jumpToLine, view,
  saveTabState, hasTabState, activateTab, dropTabState, replaceTabContent,
} from "./cm.js?v=20260529-ui7";
import { loadPdfViewer, pdfEmpty } from "./pdfviewer.js";
import {
  setSaveHint, setCompileState, updateEditorTab, openLog,
  switchBottomTab, initDialogs, initResizeHandles, updateLineCol,
  logToggleBtn, tabProblemsBtn, bottomCloseBtn, bottomPanel, editorWrapEl, assetView,
} from "./ui.js";
import {
  renderFileList, renderOutline, showEditorForText, showAssetViewer, showEmptyEditor,
  setSelectFileRef, uploadFile, uploadZip, normalizeClipboardFile,
  createFolder, createEmptyTextFile, moveFileToFolder, deleteFile,
  isUploadableProjectFile, utf8ByteSize, pathBaseName, getFileTypeClass,
} from "./files.js";
import { renderVersions, initVersionsPanel, closeDiffModal } from "./versions.js";
import {
  saveCurrentFile, compileProject, runCompile, updateCompileArtifacts,
  pollCompileStatus, connectProjectUpdatesSse, deleteCurrentProject,
  renameCurrentProject, setOutlineLocationRef,
} from "./compile.js?v=20260529-ui7";
import { loadLongdocData, setLongdocProjectMetaRef, initSessionUI, closeAiLogModal } from "./longdoc.js?v=20260529-gh1";

// ── Bootstrap config (set by inline script in template) ──────────────────────

const editorConfig = window.EDITOR_CONFIG || {};
cfg.projectId  = editorConfig.projectId  || 0;
cfg.csrfToken  = editorConfig.csrfToken  || "";
cfg.sessionReview = Boolean(editorConfig.sessionReview);

// ── Tab bar ───────────────────────────────────────────────────────────────────

const editorTabbarEl = document.getElementById("editor-tabbar");

function closeWritingAssistantTab() {
  document.getElementById("drop-zone")?.classList.remove("wa-active");
  document.getElementById("wa-tab-btn")?.classList.remove("active");
}

export function renderEditorTabs() {
  if (!editorTabbarEl) return;
  editorTabbarEl.innerHTML = "";
  s.openTabs.forEach(tab => {
    const div = document.createElement("div");
    div.className = `e-edtab${tab.name === s.activeTabName ? " active" : ""}`;
    div.title = tab.name;

    const dot = document.createElement("span");
    dot.className = `e-edtab-dot e-ftype-dot ${getFileTypeClass(tab.name)}`;

    const nameSpan = document.createElement("span");
    nameSpan.className = "e-edtab-name";
    nameSpan.textContent = pathBaseName(tab.name) || tab.name;

    const dirtyDot = document.createElement("span");
    dirtyDot.className = "e-edtab-dirty";
    if (s.hasUnsavedChanges && tab.name === s.activeTabName) dirtyDot.classList.add("show");

    const closeBtn = document.createElement("button");
    closeBtn.className = "e-edtab-close";
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Close tab");
    closeBtn.innerHTML = `<svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="2" y1="2" x2="8" y2="8"/><line x1="8" y1="2" x2="2" y2="8"/></svg>`;
    closeBtn.addEventListener("click", e => { e.stopPropagation(); closeTab(tab.name); });

    div.append(dot, nameSpan, dirtyDot, closeBtn);
    div.addEventListener("click", () => {
      closeWritingAssistantTab();
      const f = s.openTabs.find(t => t.name === tab.name);
      if (f && f.name !== s.activeTabName) selectFile(f);
    });
    editorTabbarEl.appendChild(div);
  });

  // Scroll active tab into view
  const activeEl = editorTabbarEl.querySelector(".e-edtab.active");
  activeEl?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function addTab(file) {
  if (!s.openTabs.find(t => t.name === file.name)) {
    s.openTabs.push({ ...file });
  }
  s.activeTabName = file.name;
}

function closeTab(name) {
  const idx = s.openTabs.findIndex(t => t.name === name);
  if (idx === -1) return;
  s.openTabs.splice(idx, 1);
  dropTabState(name);

  if (s.activeTabName === name) {
    const next = s.openTabs[Math.min(idx, s.openTabs.length - 1)];
    if (next) {
      selectFile(next);
    } else {
      // All tabs closed — show empty state
      s.activeTabName = "";
      s.selectedFile = { name: "", type: "", is_text: false };
      showEmptyEditor();
      renderEditorTabs();
      renderFileList();
    }
  } else {
    renderEditorTabs();
    renderFileList();
  }
}

// ── DOM refs used only in main.js ─────────────────────────────────────────────

const currentFileLbl   = document.getElementById("current-file-label");
const fileUploadInput  = document.getElementById("file-upload");
const zipUploadInput   = document.getElementById("zip-upload");
const openPdfLink      = document.getElementById("open-pdf");
const logEl            = document.getElementById("log");
const refreshPdfBtn    = document.getElementById("refresh-pdf");
const refreshOutlineBtn= document.getElementById("refresh-outline");
const refreshVersionsBtn=document.getElementById("refresh-versions");
const compileBtn       = document.getElementById("compile-btn");
const renameProjBtn    = document.getElementById("rename-project-btn");
const deleteProjBtn    = document.getElementById("delete-project-btn");
const projectMenuBtn   = document.getElementById("project-menu-btn");
const projectMenuEl    = document.getElementById("project-menu");
const newFolderBtn     = document.getElementById("new-folder-btn");
const newTextFileBtn   = document.getElementById("new-text-file-btn");
const dropZone         = document.getElementById("drop-zone");
const cmParent         = document.getElementById("cm-editor");
const smallModelWarningEl = document.getElementById("small-model-warning");
const smallModelWarningTextEl = document.getElementById("small-model-warning-text");

function humanQuotaReason(reason) {
  const labels = {
    daily_request_limit_exceeded: "Daily request limit reached.",
    monthly_request_limit_exceeded: "Monthly request limit reached.",
    daily_token_limit_exceeded: "Daily token limit reached.",
    monthly_token_limit_exceeded: "Monthly token limit reached.",
  };
  return labels[reason] || "Small model requests are temporarily unavailable for this project.";
}

function renderSmallModelWarning() {
  const quota = s.projectMeta?.small_model || {};
  const show = Boolean(quota.enabled && quota.quota_warning_visible);
  if (smallModelWarningEl) smallModelWarningEl.classList.toggle("visible", show);
  if (smallModelWarningTextEl && show) {
    const parts = [humanQuotaReason(quota.quota_reason)];
    if (typeof quota.requests_remaining_today === "number" && typeof quota.tokens_remaining_today === "number") {
      parts.push(`Remaining today: ${quota.requests_remaining_today} requests, ${quota.tokens_remaining_today} tokens.`);
    }
    smallModelWarningTextEl.textContent = parts.join(" ");
  }
}

// ── Loaders ───────────────────────────────────────────────────────────────────

export async function loadProjectMeta() {
  const data = await api(`/api/projects/${cfg.projectId}/`, { method: "GET" });
  s.projectMeta = data || {};
  const prevMain = s.mainFileName;
  const nextMain = String(s.projectMeta.main_file_name || s.projectMeta.file_name || "main.tex");
  s.mainFileName      = nextMain;
  s.supportsSynctex   = Boolean(s.projectMeta.supports_synctex);
  if (currentFileLbl) currentFileLbl.textContent = s.mainFileName;
  updateEditorTab(s.mainFileName);
  const selName = String(s.selectedFile?.name || "");
  if (!selName || s.selectedFile?.type === "main" || selName === prevMain || selName === "main.tex" || selName === "main.typ") {
    s.selectedFile = { name: s.mainFileName, type: "main", is_text: true };
  }
  renderSmallModelWarning();
}

export async function loadMainFile() {
  const data = await api(`/api/projects/${cfg.projectId}/file/`, { method: "GET" });
  const prevMain = s.mainFileName;
  if (data.file_name) s.mainFileName = String(data.file_name);
  if (
    s.selectedFile?.type === "main" ||
    s.selectedFile?.name === prevMain ||
    s.selectedFile?.name === "main.tex" ||
    s.selectedFile?.name === "main.typ"
  ) {
    s.selectedFile = { name: s.mainFileName, type: "main", is_text: true };
  }
  if (currentFileLbl) currentFileLbl.textContent = s.selectedFile.name;
  s.mainFileContent   = data.content || "";
  replaceTabContent(s.mainFileName, s.mainFileContent, s.mainFileName);
  s.hasUnsavedChanges = false;
  setSaveHint("Завантажено", "saved");
}

export async function loadFiles() {
  const p = await api(`/api/projects/${cfg.projectId}/files/`, { method: "GET" });
  s.projectFiles = p.files || [];
  renderFileList();
}

export async function loadSections() {
  const p = await api(`/api/projects/${cfg.projectId}/sections/`, { method: "GET" });
  s.sections = p.sections || [];
  renderOutline(openOutlineLocation);
}

export async function loadVersions(reset = false) {
  if (s.versionsLoading) return;
  if (reset) { s.versions = []; s.versionsHasMore = true; s.versionsCursor = null; }
  else if (!s.versionsHasMore) return;
  s.versionsLoading = true;
  renderVersions();
  try {
    const q = new URLSearchParams({ limit: "30" });
    if (s.versionsCursor) q.set("before_id", String(s.versionsCursor));
    if (s.versionsFileFilter) q.set("file", s.versionsFileFilter);
    const p = await api(`/api/projects/${cfg.projectId}/versions/?${q.toString()}`, { method: "GET" });
    const seen = new Set(s.versions.map(v => v.id));
    (p.versions || []).map(v => ({ ...v, _diff: undefined, _loading: false }))
      .forEach(v => { if (!seen.has(v.id)) s.versions.push(v); });
    s.versionsHasMore  = Boolean(p.has_more);
    s.versionsCursor   = p.next_before_id ?? null;
  } finally {
    s.versionsLoading = false;
    renderVersions();
  }
}

export async function refreshLivePdfPreview(pdfUrl = null, pdfVer = null) {
  const payload = pdfUrl ? {
    pdf_url: pdfUrl,
    pdf_version: pdfVer ?? Date.now(),
    log: "",
    diagnostics: [],
    compile_state: s.projectMeta?.last_status === "success" ? "synced" : "out_of_date",
    status: s.projectMeta?.last_status || "success",
  } : await api(`/api/projects/${cfg.projectId}/compile/`, { method: "GET" });

  setCompileState(payload.compile_state || "out_of_date", payload.status);
  updateCompileArtifacts(payload.log || "", payload);
  if (payload.pdf_url) {
    s.lastPdfVersion = payload.pdf_version ?? Date.now();
    if (openPdfLink) openPdfLink.href = payload.pdf_url;
    await loadPdfViewer(`${payload.pdf_url}?t=${s.lastPdfVersion}`);
  } else {
    pdfEmpty.style.display = "flex";
  }
}

// ── File selection (coordination layer) ───────────────────────────────────────

async function selectFile(file) {
  closeWritingAssistantTab();
  const prevFile = s.selectedFile;

  // Flush unsaved changes before switching
  if (s.hasUnsavedChanges && prevFile.is_text && !prevFile.is_dir) {
    clearTimeout(s.saveTimer);
    await saveCurrentFile();
  }

  // Snapshot current tab's editor state so its undo history is preserved
  if (prevFile.name && prevFile.is_text && !prevFile.is_dir) {
    saveTabState(prevFile.name);
  }

  s.selectedFile = { name: file.name, type: file.type || "asset", ...file };
  if (currentFileLbl) currentFileLbl.textContent = file.name;
  updateEditorTab(file.name);
  addTab(s.selectedFile);
  renderEditorTabs();
  renderFileList();

  if (file.name === s.mainFileName) {
    showEditorForText();
    activateTab(s.mainFileName, s.mainFileContent, s.mainFileName);
    s.hasUnsavedChanges = false;
    setSaveHint("", "");
    focusEditor();
    return;
  }

  if (file.is_text && !file.is_dir) {
    showEditorForText();

    // If the tab was already loaded, restore its saved state (history intact)
    if (hasTabState(file.name)) {
      activateTab(file.name, "", file.name);
      s.hasUnsavedChanges = false;
      setSaveHint("", "");
      focusEditor();
      return;
    }

    // First visit — fetch from server and create fresh state
    setSaveHint("Завантаження…", "saving");
    try {
      const params = new URLSearchParams({ include_text: "1" });
      const data = await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(file.name)}/content/?${params}`);
      activateTab(file.name, data.text_content || "", file.name);
      s.hasUnsavedChanges = false;
      setSaveHint("Завантажено", "saved");
      focusEditor();
    } catch (err) {
      setSaveHint(`Помилка: ${err.message}`, "error");
      s.selectedFile = { name: s.mainFileName, type: "main", is_text: true };
      if (currentFileLbl) currentFileLbl.textContent = s.mainFileName;
      activateTab(s.mainFileName, s.mainFileContent, s.mainFileName);
      s.hasUnsavedChanges = false;
      renderFileList();
    }
    return;
  }

  showAssetViewer(file);
}

// ── Outline navigation ────────────────────────────────────────────────────────

async function openOutlineLocation(fileName, lineNumber, column = 1) {
  try {
    const target = String(fileName || s.mainFileName);
    if (s.selectedFile.name !== target) {
      const fileObj = target === s.mainFileName
        ? { name: s.mainFileName, type: "main", is_text: true }
        : s.projectFiles.find(f => f.name === target);
      if (!fileObj) return;
      await selectFile(fileObj);
      // yield to let CodeMirror finish DOM update before scrolling
      await new Promise(r => requestAnimationFrame(r));
    }
    jumpToLine(lineNumber, column);
  } catch (err) {
    console.error("Outline navigation error:", err);
  }
}

// ── Editor input handler ──────────────────────────────────────────────────────

function onEditorInput(action) {
  if (action === "save") {
    saveCurrentFile().catch(() => {});
    return;
  }
  if (action === "compile") {
    compileProject().catch(() => {});
    return;
  }
  // "change"
  if (!s.selectedFile.is_text || s.selectedFile.is_dir) return;
  s.hasUnsavedChanges = true;
  setSaveHint("Є незбережені зміни…", "saving");
  renderEditorTabs();
  setCompileState("out_of_date", "pending");
  clearTimeout(s.saveTimer);
  clearTimeout(s.typstCompileTimer);

  const savedName = s.selectedFile.name;
  const isTypstTextFile = s.projectMeta?.markup_type === "typst" && String(savedName).toLowerCase().endsWith(".typ");
  s.saveTimer = setTimeout(() => {
    saveCurrentFile()
      .then(() => {
        if (isTypstTextFile) runCompile("realtime").catch(() => {});
        if (savedName !== s.mainFileName) return;
        loadSections().catch(() => {});
      })
      .catch(() => {});
  }, isTypstTextFile ? 200 : 1200);
}

// ── Project menu ──────────────────────────────────────────────────────────────

function closeProjectMenu() {
  projectMenuEl?.classList.remove("open");
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  // Inject shared references to break circular deps
  setSelectFileRef(selectFile);
  setOutlineLocationRef(openOutlineLocation);
  setLongdocProjectMetaRef(loadProjectMeta);
  initSessionUI();

  // Initialize CodeMirror
  initCodeMirror(
    cmParent,
    onEditorInput,
    () => { if (view) updateLineCol(view); }
  );

  // Initialize UI subsystems
  initDialogs();
  initResizeHandles();
  initVersionsPanel();

  // Tab switching
  document.querySelectorAll(".e-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".e-tab").forEach(t => t.classList.remove("active"));
      document.querySelectorAll(".e-tabpanel").forEach(p => p.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById(`tab-${tab.dataset.tab}`)?.classList.add("active");
    });
  });

  // Bottom panel tabs
  logToggleBtn?.addEventListener("click",   () => switchBottomTab("log"));
  tabProblemsBtn?.addEventListener("click", () => switchBottomTab("problems"));
  bottomCloseBtn?.addEventListener("click", () => bottomPanel?.classList.remove("open"));

  // Project menu
  projectMenuBtn?.addEventListener("click", () => projectMenuEl?.classList.toggle("open"));
  document.addEventListener("click", e => {
    if (!projectMenuBtn?.contains(e.target) && !projectMenuEl?.contains(e.target)) closeProjectMenu();
  });

  // Compile / project actions
  compileBtn?.addEventListener("click",    () => {
    if (cfg.sessionReview) {
      setSaveHint("Session review is read-only", "error");
      return;
    }
    compileProject().catch(() => {});
  });
  renameProjBtn?.addEventListener("click", () => { closeProjectMenu(); renameCurrentProject().catch(() => {}); });
  deleteProjBtn?.addEventListener("click", () => { closeProjectMenu(); deleteCurrentProject().catch(() => {}); });

  // PDF actions
  refreshPdfBtn?.addEventListener("click", () => {
    if (s.pdfCurrentUrl) {
      const base = s.pdfCurrentUrl.split("?")[0];
      loadPdfViewer(`${base}?t=${Date.now()}`);
    }
  });

  // Outline / versions
  refreshOutlineBtn?.addEventListener("click",  () => loadSections().catch(() => {}));
  refreshVersionsBtn?.addEventListener("click", () => loadVersions(true).catch(() => {}));

  // File uploads
  fileUploadInput?.addEventListener("change", async e => {
    if (cfg.sessionReview) return;
    for (const f of [...(e.target.files || [])]) {
      try { await uploadFile(f); } catch (err) { setSaveHint(`Помилка: ${err.message}`, "error"); }
    }
    fileUploadInput.value = "";
    await Promise.all([loadProjectMeta(), loadFiles(), loadVersions(true)]);
  });
  zipUploadInput?.addEventListener("change", async e => {
    if (cfg.sessionReview) return;
    const f = e.target.files?.[0]; if (!f) return;
    try { await uploadZip(f); } catch (err) { setSaveHint(`Помилка ZIP: ${err.message}`, "error"); }
    finally { e.target.value = ""; }
    await Promise.all([loadProjectMeta(), loadFiles(), loadVersions(true)]);
  });

  // New file / folder
  newFolderBtn?.addEventListener("click",    () => { if (!cfg.sessionReview) createFolder().then(name => { if (name) return Promise.all([loadProjectMeta(), loadFiles(), loadVersions(true)]).then(() => { const created = s.projectFiles.find(x => x.name === name.replace(/[\\/]+$/, "")); if (created) selectFile(created); }); }).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")); });
  newTextFileBtn?.addEventListener("click",  () => { if (!cfg.sessionReview) createEmptyTextFile().then(name => { if (name) return Promise.all([loadProjectMeta(), loadFiles(), loadVersions(true)]).then(() => { const created = s.projectFiles.find(x => x.name === name); if (created) selectFile(created); }); }).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")); });

  // Drag & drop on drop zone
  let dragCounter = 0;
  dropZone?.addEventListener("dragenter", e => { if (cfg.sessionReview) return; e.preventDefault(); dragCounter++; dropZone.classList.add("drag-active"); });
  dropZone?.addEventListener("dragleave", () => { if (cfg.sessionReview) return; if (--dragCounter <= 0) { dragCounter = 0; dropZone.classList.remove("drag-active"); } });
  dropZone?.addEventListener("dragover",  e => { if (!cfg.sessionReview) e.preventDefault(); });
  dropZone?.addEventListener("drop", async e => {
    if (cfg.sessionReview) return;
    e.preventDefault(); dragCounter = 0; dropZone.classList.remove("drag-active");
    for (const f of [...(e.dataTransfer.files || [])]) {
      if (f.name.toLowerCase().endsWith(".zip")) {
        try { await uploadZip(f); } catch (err) { setSaveHint(`Помилка ZIP: ${err.message}`, "error"); }
      } else {
        try { await uploadFile(f); } catch (err) { setSaveHint(`Помилка: ${err.message}`, "error"); }
      }
    }
    await Promise.all([loadProjectMeta(), loadFiles(), loadVersions(true)]);
  });

  // File list drag-to-root drop
  document.getElementById("file-list")?.addEventListener("dragover", e => {
    if (cfg.sessionReview) return;
    if (!s.draggedFilePath) return; e.preventDefault();
  });
  document.getElementById("file-list")?.addEventListener("drop", e => {
    if (cfg.sessionReview) return;
    if (!s.draggedFilePath) return;
    const folderBtn = e.target.closest(".e-file-btn[data-is-dir='1']");
    if (folderBtn) return;
    e.preventDefault();
    const src = s.draggedFilePath || e.dataTransfer?.getData("text/plain") || "";
    if (!src) return;
    moveFileToFolder(src, "")
      .then(() => Promise.all([loadProjectMeta(), loadFiles()]))
      .catch(err => setSaveHint(`Помилка: ${err.message}`, "error"));
  });

  // Clipboard paste upload
  document.addEventListener("paste", async e => {
    if (cfg.sessionReview) return;
    const files = [...(e.clipboardData?.files || [])].filter(f => f && f.size > 0);
    if (!files.length) return;
    e.preventDefault();
    for (let i = 0; i < files.length; i++) {
      const f = normalizeClipboardFile(files[i], i);
      try { await uploadFile(f); } catch (err) { setSaveHint(`Помилка upload: ${err.message}`, "error"); }
    }
    await Promise.all([loadProjectMeta(), loadFiles(), loadVersions(true)]);
  });

  // Escape closes diff modal
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && document.getElementById("diff-modal-overlay")?.classList.contains("open")) {
      closeDiffModal();
      return;
    }
    if (e.key === "Escape" && document.getElementById("ai-log-overlay")?.classList.contains("open")) {
      closeAiLogModal();
    }
  });

  // Beforeunload cleanup
  window.addEventListener("beforeunload", () => {
    if (s.statusPollTimer)         clearInterval(s.statusPollTimer);
    if (s.typstCompileTimer)       clearTimeout(s.typstCompileTimer);
    if (s.projectSse) { try { s.projectSse.close(); } catch (_) {} s.projectSse = null; }
  });

  // ── Load initial data ──
  await loadProjectMeta();
  await loadMainFile();
  // Seed initial tab
  const mainFileObj = { name: s.mainFileName, type: "main", is_text: true };
  s.openTabs = [mainFileObj];
  s.activeTabName = s.mainFileName;
  renderEditorTabs();
  updateEditorTab(s.selectedFile?.name || s.mainFileName);
  setCompileState("out_of_date");
  await Promise.all([loadFiles(), loadSections(), loadVersions(true), loadLongdocData()]);

  const cd = await api(`/api/projects/${cfg.projectId}/compile/`, { method: "GET" });
  if (cd.log) { logEl.textContent = cd.log; openLog(); }
  await refreshLivePdfPreview(cd.pdf_url || null, cd.pdf_version ?? null);

  renderFileList();
  connectProjectUpdatesSse();
  s.statusPollTimer = setInterval(pollCompileStatus, 5000);
}

init().catch(err => setSaveHint(`Помилка ініціалізації: ${err.message}`, "error"));
