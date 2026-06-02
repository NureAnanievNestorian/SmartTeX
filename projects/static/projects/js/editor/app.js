import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as cm from "./cm.js";
import * as pdfviewer from "./pdfviewer.js";
import * as ui from "./ui.js";
import * as files from "./files.js";
import * as versions from "./versions.js";
import * as compile from "./compile.js";
import * as longdoc from "./longdoc.js";
import * as search from "./search.js";
import * as tinymist from "./tinymist.js";

const { s, cfg } = state;
const { api } = apiMod;
const {
  initCodeMirror, switchLanguage,
  focusEditor, jumpToLine, getSelectionSnapshot, setSelectionSnapshot, setEditorDiagnostics,
  setLineWrapping, isLineWrappingEnabled, replaceContentPreservingViewport,
  saveTabState, hasTabState, activateTab, dropTabState, runUndo, runRedo, setEditorContextMenuProvider, insertAtCursor,
} = cm;
const { loadPdfViewer, pdfEmpty } = pdfviewer;
const { initPreviewPanel, getPreviewMode, refreshTypstPreview, revealPreviewSelection, setPreviewCodeNavigationCallback, syncPreviewMemoryFile } = pdfviewer;
const {
  setSaveHint, setCompileState, updateEditorTab, openLog,
  switchBottomTab, initDialogs, initResizeHandles, updateLineCol, updateWrapToggle, showAnnotationPopover, showAnnotationInfoPopover,
  logToggleBtn, tabProblemsBtn, bottomCloseBtn, bottomPanel, editorWrapEl, assetView,
} = ui;
const {
  renderFileList, renderOutline, showEditorForText, showAssetViewer, showEmptyEditor,
  setSelectFileRef, setFileContextMenuRef, uploadFile, uploadImageWithRename, uploadZip, normalizeClipboardFile,
  createFolder, createEmptyTextFile, moveFileToFolder, deleteFile,
  isUploadableProjectFile, isImageFile, utf8ByteSize, pathBaseName, getFileTypeClass,
  setMainFile, getSelectedFolderPath, clearSelectedFolderPath, restoreFileTreeState,
  normalizeFileTreeState, remapFolderTreeState, removeFolderTreeState,
} = files;
const { renderVersions, initVersionsPanel, closeDiffModal } = versions;
const {
  saveCurrentFile, compileProject, updateCompileArtifacts,
  pollCompileStatus, connectProjectUpdatesSse, deleteCurrentProject,
  renameCurrentProject, setOutlineLocationRef, openCreateTemplateDialog,
} = compile;

// ── LSP cross-file navigation callback ───────────────────────────────────────

async function lspNavigateTo(filename, lineNum, charNum) {
  try {
    const target = String(filename || "");
    if (!target) return;
    if (s.selectedFile?.name !== target) {
      const fileObj = target === s.mainFileName
        ? { name: s.mainFileName, type: "main", is_text: true }
        : s.projectFiles.find(f => f.name === target);
      if (!fileObj) return;
      await selectFile(fileObj);
      await new Promise(r => requestAnimationFrame(r));
    }
    jumpToLine(lineNum, charNum);
  } catch (err) {
    console.error("LSP navigation error:", err);
  }
}

function primeTinymistMainContext(activeFileName = "") {
  if (s.projectMeta?.markup_type !== "typst") return;
  const mainFileName = String(s.mainFileName || "");
  if (!mainFileName || mainFileName === activeFileName) return;
  if (!String(s.mainFileContent || "")) return;
  tinymist.didOpen(mainFileName, s.mainFileContent);
}

// ── Bootstrap config (set by inline script in template) ──────────────────────

const editorConfig = window.EDITOR_CONFIG || {};
cfg.projectId  = editorConfig.projectId  || 0;
cfg.csrfToken  = editorConfig.csrfToken  || "";
cfg.sessionReview = Boolean(editorConfig.sessionReview);
cfg.sessionReviewUrl = editorConfig.sessionReviewUrl || "";

const AUTOSAVE_DEBOUNCE_MS = 2500;
const TYPST_AUTOSAVE_DEBOUNCE_MS = 900;
const TYPST_REALTIME_COMPILE_DEBOUNCE_MS = 2500;

// ── Tab bar ───────────────────────────────────────────────────────────────────

const editorTabbarEl = document.getElementById("editor-tabbar");
const editorShellEl = document.querySelector(".editor-shell");
const centerPanelEl = document.getElementById("drop-zone");
const waToggleBtnEl = document.getElementById("wa-tab-btn");
const mobileWorkspaceBtns = [...document.querySelectorAll("[data-mobile-panel]")];
const mobileWorkspaceMq = window.matchMedia("(max-width: 860px)");

function isMobileWorkspace() {
  return mobileWorkspaceMq.matches;
}

function setMobileWorkspacePanel(panel) {
  const nextPanel = String(panel || "editor");
  if (editorShellEl) editorShellEl.dataset.mobilePanel = nextPanel;
  mobileWorkspaceBtns.forEach(btn => {
    btn.classList.toggle("active", btn.dataset.mobilePanel === nextPanel);
  });
}

function syncMobileWorkspacePanel() {
  if (!isMobileWorkspace()) {
    if (editorShellEl) delete editorShellEl.dataset.mobilePanel;
    mobileWorkspaceBtns.forEach(btn => btn.classList.remove("active"));
    return;
  }
  const current = editorShellEl?.dataset.mobilePanel;
  if (centerPanelEl?.classList.contains("wa-active")) {
    setMobileWorkspacePanel("assistant");
    return;
  }
  setMobileWorkspacePanel(current || "files");
}

function closeWritingAssistantTab() {
  centerPanelEl?.classList.remove("wa-active");
  waToggleBtnEl?.classList.remove("active");
  if (isMobileWorkspace()) setMobileWorkspacePanel("editor");
}

function closeAiLogOverlay() {
  document.getElementById("ai-log-overlay")?.classList.remove("open");
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
    div.addEventListener("contextmenu", e => openTabContextMenu(tab.name, e));
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
  schedulePersistTabs();
}

function closeTab(name) {
  const idx = s.openTabs.findIndex(t => t.name === name);
  if (idx === -1) return;
  if (name === s.activeTabName) {
    captureActiveScroll();
    captureActiveSelection();
  }
  tinymist.didClose(name);
  s.openTabs.splice(idx, 1);
  dropTabState(name);
  _tabScrolls.delete(name);
  _tabSelections.delete(name);

  if (s.activeTabName === name) {
    const next = s.openTabs[Math.min(idx, s.openTabs.length - 1)];
    if (next) {
      selectFile(next);
    } else {
      s.activeTabName = "";
      s.selectedFile = { name: "", type: "", is_text: false };
      showEmptyEditor();
      renderEditorTabs();
      renderFileList();
      schedulePersistTabs();
    }
  } else {
    renderEditorTabs();
    renderFileList();
    schedulePersistTabs();
  }
}

// ── Tab persistence (localStorage) ────────────────────────────────────────────

const _tabScrolls = new Map();
const _tabSelections = new Map();
let _suppressScrollCapture = false;
let _persistTabsTimer = null;

function tabsStorageKey() {
  return `smarttex.editor.tabs.${cfg.projectId}`;
}

function captureActiveScroll() {
  if (!s.activeTabName) return;
  const top = cm.view?.scrollDOM?.scrollTop;
  if (typeof top === "number") _tabScrolls.set(s.activeTabName, top);
}

function captureActiveSelection() {
  if (!s.activeTabName) return;
  const selection = getSelectionSnapshot();
  if (selection) _tabSelections.set(s.activeTabName, selection);
}

function schedulePersistTabs() {
  clearTimeout(_persistTabsTimer);
  _persistTabsTimer = setTimeout(() => {
    _persistTabsTimer = null;
    persistTabs();
  }, 120);
}

function applyTabScroll(name) {
  const top = _tabScrolls.get(name);
  if (typeof top !== "number") return;
  _suppressScrollCapture = true;
  requestAnimationFrame(() => {
    if (cm.view?.scrollDOM) cm.view.scrollDOM.scrollTop = top;
    requestAnimationFrame(() => { _suppressScrollCapture = false; });
  });
}

function attachScrollListener() {
  const el = cm.view?.scrollDOM;
  if (!el) return;
  el.addEventListener("scroll", () => {
    if (_suppressScrollCapture) return;
    if (!s.activeTabName) return;
    _tabScrolls.set(s.activeTabName, el.scrollTop);
    schedulePersistTabs();
  }, { passive: true });
}

function applyTabSelection(name) {
  const selection = _tabSelections.get(name);
  if (!selection) return;
  setSelectionSnapshot(selection);
}

function persistTabs() {
  if (!cfg.projectId) return;
  captureActiveScroll();
  captureActiveSelection();
  try {
    const payload = {
      openTabs: s.openTabs.map(t => ({
        name: t.name,
        type: t.type || "asset",
        scrollTop: _tabScrolls.get(t.name) || 0,
        selection: _tabSelections.get(t.name) || null,
      })),
      activeTabName: s.activeTabName || "",
    };
    localStorage.setItem(tabsStorageKey(), JSON.stringify(payload));
  } catch (_) {}
}

function readStoredTabs() {
  if (!cfg.projectId) return null;
  try {
    const raw = localStorage.getItem(tabsStorageKey());
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.openTabs)) return null;
    return parsed;
  } catch (_) { return null; }
}

function resolveStoredTab(stored) {
  const name = String(stored?.name || "");
  if (!name) return null;
  if (typeof stored.scrollTop === "number") _tabScrolls.set(name, stored.scrollTop);
  if (stored?.selection?.ranges?.length) _tabSelections.set(name, stored.selection);
  if (name === s.mainFileName) {
    return { name, type: "main", is_text: true };
  }
  const f = s.projectFiles.find(x => x.name === name);
  if (!f) return null;
  return { ...f };
}

async function restoreTabsFromStorage() {
  const stored = readStoredTabs();
  if (!stored) return false;
  const tabs = stored.openTabs
    .map(resolveStoredTab)
    .filter(Boolean);
  if (!tabs.length) return false;

  s.openTabs = tabs;
  const activeName = tabs.find(t => t.name === stored.activeTabName)?.name || tabs[0].name;
  s.activeTabName = activeName;
  renderEditorTabs();

  const activeFile = tabs.find(t => t.name === activeName);
  await selectFile(activeFile);
  return true;
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
const fileListEl       = document.getElementById("file-list");
const compileBtn       = document.getElementById("compile-btn");
const renameProjBtn      = document.getElementById("rename-project-btn");
const deleteProjBtn      = document.getElementById("delete-project-btn");
const createTemplateBtnEl = document.getElementById("create-template-btn");
const projectMenuBtn     = document.getElementById("project-menu-btn");
const projectMenuEl      = document.getElementById("project-menu");
const fileMenuBtn        = document.getElementById("file-menu-btn");
const fileMenuEl         = document.getElementById("file-menu");
const editMenuBtn        = document.getElementById("edit-menu-btn");
const editMenuEl         = document.getElementById("edit-menu");
const typstMenuBtn       = document.getElementById("typst-menu-btn");
const typstMenuEl        = document.getElementById("typst-menu");
const newFolderBtn     = document.getElementById("new-folder-btn");
const newTextFileBtn   = document.getElementById("new-text-file-btn");
const dropZone         = document.getElementById("drop-zone");
const cmParent         = document.getElementById("cm-editor");
const editorContextMenuEl = document.getElementById("editor-context-menu");
const commandPaletteOverlayEl = document.getElementById("command-palette-overlay");
const commandPaletteInputEl = document.getElementById("command-palette-input");
const commandPaletteListEl = document.getElementById("command-palette-list");
const smallModelWarningEl = document.getElementById("small-model-warning");
const smallModelWarningTextEl = document.getElementById("small-model-warning-text");
const signatureHelpEl = document.getElementById("signature-help");
const WRAP_PREF_KEY = "smarttex.editor.lineWrap";
let _paletteMode = "commands";
let _workspaceSymbolTimer = null;
let _workspaceSymbolRequestId = 0;
let _signatureHelpTimer = null;

function getTokenAroundCursor() {
  if (!cm.view) return "";
  const pos = cm.view.state.selection.main.head;
  const text = cm.view.state.doc.toString();
  const left = text.slice(0, pos).match(/[A-Za-z_][A-Za-z0-9_-]*$/);
  const right = text.slice(pos).match(/^[A-Za-z0-9_-]*/);
  return `${left?.[0] || ""}${right?.[0] || ""}`.trim();
}
const MENU_ICONS_PDF_EMBED = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h7l3 3v5H3z"/><path d="M10 4v3h3"/><path d="M6 8.5h4"/><path d="M8 7v3"/></svg>`;
const MENU_ICONS_PDF_EMBED_OFF = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h7l3 3v5H3z"/><path d="M10 4v3h3"/><path d="M5 5l6 6"/></svg>`;
const MENU_ICONS_INSERT_SNIPPET = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h8"/><path d="M4 7h6"/><path d="M4 10h4"/><path d="M11 10l2 2-2 2"/></svg>`;

const MENU_ICONS = {
  newFile: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V6z"/><path d="M9.5 2v4h4"/><path d="M8 8.5v3"/><path d="M6.5 10h3"/></svg>`,
  newFolder: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 5a1 1 0 0 1 1-1h3.4l1.3 1.5h6.3a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z"/><path d="M7.5 7.5v3"/><path d="M6 9h3"/></svg>`,
  main: `<svg viewBox="0 0 16 16" fill="currentColor"><path d="m8 1.8 1.9 3.86 4.26.62-3.08 3 .73 4.24L8 11.5l-3.81 2 .73-4.24-3.08-3 4.26-.62z"/></svg>`,
  rename: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m11.5 2.5 2 2"/><path d="m3 13 2.7-.5 6.6-6.6-2.1-2.1-6.6 6.6z"/><path d="M3 13h10"/></svg>`,
  delete: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11"/><path d="M6 4V2.8h4V4"/><path d="M4.5 4l.6 8.5h5.8l.6-8.5"/><path d="M6.5 6.5v4.5"/><path d="M9.5 6.5v4.5"/></svg>`,
  close: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 4l8 8"/><path d="M12 4 4 12"/></svg>`,
  closeOthers: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="3" width="11" height="10" rx="1.5"/><path d="M6 6.2 10 10.2"/><path d="M10 6.2 6 10.2"/></svg>`,
  closeRight: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4.5h5"/><path d="M2.5 8h5"/><path d="M2.5 11.5h5"/><path d="m9 4 4 4-4 4"/></svg>`,
  closeAll: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3.5h9v9h-9z"/><path d="M5.5 5.5 10.5 10.5"/><path d="M10.5 5.5 5.5 10.5"/></svg>`,
  annotate: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3.5h10v7H7l-3 2z"/><path d="M5.5 6.2h5"/><path d="M5.5 8.4h3.5"/></svg>`,
};

function humanQuotaReason(reason) {
  const labels = {
    credits_limit_exceeded: "AI credits limit reached.",
  };
  return labels[reason] || "Small model requests are temporarily unavailable for this project.";
}

function renderSmallModelWarning() {
  const quota = s.projectMeta?.small_model || {};
  const show = Boolean(quota.enabled && quota.quota_warning_visible);
  if (smallModelWarningEl) smallModelWarningEl.classList.toggle("visible", show);
  if (smallModelWarningTextEl && show) {
    smallModelWarningTextEl.textContent = humanQuotaReason(quota.quota_reason);
  }
  const aiLogBtn = document.getElementById("open-ai-log-btn");
  if (aiLogBtn) aiLogBtn.style.display = quota.enabled ? "" : "none";
}

function readWrapPreference() {
  try {
    const raw = localStorage.getItem(WRAP_PREF_KEY);
    return raw == null ? true : raw === "1";
  } catch (_) {
    return true;
  }
}

function persistWrapPreference(enabled) {
  try {
    localStorage.setItem(WRAP_PREF_KEY, enabled ? "1" : "0");
  } catch (_) {}
}

function applyWrapPreference(enabled) {
  setLineWrapping(enabled);
  updateWrapToggle(enabled);
}

function toggleLineWrap() {
  const next = !isLineWrappingEnabled();
  applyWrapPreference(next);
  persistWrapPreference(next);
}

function closeTopMenus() {
  fileMenuEl?.classList.remove("open");
  editMenuEl?.classList.remove("open");
  typstMenuEl?.classList.remove("open");
  projectMenuEl?.classList.remove("open");
}

function toggleTopMenu(menuEl) {
  if (!menuEl) return;
  const willOpen = !menuEl.classList.contains("open");
  closeTopMenus();
  if (willOpen) menuEl.classList.add("open");
}

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

function isMenuOpen(menuEl) {
  return Boolean(menuEl?.classList.contains("open"));
}

function closeEditorContextMenu() {
  editorContextMenuEl?.classList.remove("open");
  if (editorContextMenuEl) {
    editorContextMenuEl.innerHTML = "";
    editorContextMenuEl.style.left = "";
    editorContextMenuEl.style.top = "";
  }
}

function openEditorContextMenu(items, event) {
  if (!editorContextMenuEl || !items?.length) return;
  event?.preventDefault?.();
  event?.stopPropagation?.();
  editorContextMenuEl.innerHTML = "";

  items.forEach(item => {
    if (item.type === "separator") {
      const sep = document.createElement("div");
      sep.className = "e-menu-separator";
      editorContextMenuEl.appendChild(sep);
      return;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `e-menu-item${item.danger ? " danger" : ""}`;
    btn.disabled = Boolean(item.disabled);
    btn.innerHTML = `
      <span class="e-menu-item-icon" aria-hidden="true">${item.icon || ""}</span>
      <span class="e-menu-item-label">${item.label}</span>
      <span class="e-menu-item-shortcut">${item.shortcut || ""}</span>
    `;
    btn.addEventListener("click", () => {
      closeEditorContextMenu();
      if (!item.disabled) item.onSelect?.();
    });
    editorContextMenuEl.appendChild(btn);
  });

  editorContextMenuEl.classList.add("open", "e-context-menu");
  const margin = 8;
  const viewW = window.innerWidth;
  const viewH = window.innerHeight;
  const menuRect = editorContextMenuEl.getBoundingClientRect();
  const x = Math.min(event.clientX, viewW - menuRect.width - margin);
  const y = Math.min(event.clientY, viewH - menuRect.height - margin);
  editorContextMenuEl.style.left = `${Math.max(margin, x)}px`;
  editorContextMenuEl.style.top = `${Math.max(margin, y)}px`;
}

async function reloadProjectTree({ selectPath = "", preferDir = false } = {}) {
  await Promise.all([loadProjectMeta(), loadFiles(), loadVersions(true)]);
  if (!selectPath) return;
  const target = preferDir
    ? s.projectFiles.find(x => x.name === selectPath.replace(/[\\/]+$/, ""))
    : s.projectFiles.find(x => x.name === selectPath);
  if (target) await selectFile(target);
}

async function handleCreateFolder(parentPath = "") {
  const created = await createFolder(parentPath || getSelectedFolderPath());
  if (created) await reloadProjectTree({ selectPath: created, preferDir: true });
}

async function handleCreateTextFile(parentPath = "") {
  const created = await createEmptyTextFile(parentPath || getSelectedFolderPath());
  if (created) await reloadProjectTree({ selectPath: created });
}

async function handleRenameFile(file) {
  const currentName = String(file?.name || "");
  if (!currentName) return;
  const nextName = await files.renameFile(file);
  if (!nextName || nextName === currentName) return;

  if (file?.is_dir) {
    remapFolderTreeState(currentName, nextName);
    s.openTabs = s.openTabs.map(tab => (
      tab.name === currentName || tab.name.startsWith(`${currentName}/`)
        ? { ...tab, name: `${nextName}${tab.name.slice(currentName.length)}` }
        : tab
    ));
    if (s.activeTabName === currentName || s.activeTabName.startsWith(`${currentName}/`)) {
      s.activeTabName = `${nextName}${s.activeTabName.slice(currentName.length)}`;
    }
  } else {
    s.openTabs = s.openTabs.map(tab => tab.name === currentName ? { ...tab, name: nextName } : tab);
    if (s.activeTabName === currentName) s.activeTabName = nextName;
  }
  await reloadProjectTree({ selectPath: nextName, preferDir: Boolean(file?.is_dir) });
}

async function handleDeleteFile(file) {
  const currentName = String(file?.name || "");
  const deletedSelected = await deleteFile(file);
  if (!currentName) return;
  const removedNames = file?.is_dir
    ? s.openTabs.filter(tab => tab.name === currentName || tab.name.startsWith(`${currentName}/`)).map(tab => tab.name)
    : [currentName];
  if (file?.is_dir) removeFolderTreeState(currentName);
  removedNames.forEach(name => {
    dropTabState(name);
    _tabScrolls.delete(name);
    _tabSelections.delete(name);
  });
  s.openTabs = s.openTabs.filter(tab => !removedNames.includes(tab.name));
  if (removedNames.includes(s.activeTabName)) s.activeTabName = "";
  await Promise.all([loadProjectMeta(), loadFiles(), loadVersions(true)]);
  if (deletedSelected) {
    s.selectedFile = { name: "", type: "", is_text: false };
    showEmptyEditor();
  } else if (!s.openTabs.length) {
    s.selectedFile = { name: "", type: "", is_text: false };
    showEmptyEditor();
  } else if (!s.activeTabName) {
    await selectFile(s.openTabs[0]);
    return;
  }
  renderEditorTabs();
  renderFileList();
  schedulePersistTabs();
}

function buildFileContextMenuItems(file) {
  const isDir = Boolean(file?.is_dir);
  const isMain = file?.name === s.mainFileName;
  const parentPath = isDir ? file.name : (file?.name?.includes("/") ? file.name.slice(0, file.name.lastIndexOf("/")) : "");
  const items = [];

  if (!cfg.sessionReview) {
    items.push(
      { label: isDir ? "Новий файл тут" : "Новий файл поруч", icon: MENU_ICONS.newFile, shortcut: "N", onSelect: () => handleCreateTextFile(parentPath).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
      { label: isDir ? "Нова папка тут" : "Нова папка поруч", icon: MENU_ICONS.newFolder, shortcut: "Shift+N", onSelect: () => handleCreateFolder(parentPath).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
    );

    if (!isDir && !isMain) {
      items.push({ type: "separator" });
      items.push({ label: "Зробити main файлом", icon: MENU_ICONS.main, onSelect: () => setMainFile(file.name).then(() => reloadProjectTree({ selectPath: file.name })).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) });
    }

    if (!isDir && s.projectMeta?.markup_type === "typst" && String(file.name).toLowerCase().endsWith(".pdf")) {
      const embed = s.pdfEmbeds[file.name];
      const isEmbedEnabled = Boolean(embed?.enabled);
      items.push({ type: "separator" });
      if (isEmbedEnabled) {
        items.push({
          label: "Вставити у документ",
          icon: MENU_ICONS_INSERT_SNIPPET,
          onSelect: () => insertPdfEmbedSnippet(file.name),
        });
        items.push({
          label: "Відключити PDF embed",
          icon: MENU_ICONS_PDF_EMBED_OFF,
          onSelect: () => togglePdfEmbed(file.name, false),
        });
      } else {
        items.push({
          label: "Включити PDF embed",
          icon: MENU_ICONS_PDF_EMBED,
          onSelect: () => togglePdfEmbed(file.name, true),
        });
      }
    }

    items.push({ type: "separator" });
    items.push({ label: "Перейменувати", icon: MENU_ICONS.rename, shortcut: "F2", onSelect: () => handleRenameFile(file).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) });
    if (!isMain) {
      items.push({ label: "Видалити", icon: MENU_ICONS.delete, shortcut: "Del", danger: true, onSelect: () => handleDeleteFile(file).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) });
    }
  }

  return items;
}

function openFileContextMenu(file, event) {
  const items = buildFileContextMenuItems(file);
  if (items.length) openEditorContextMenu(items, event);
}

function openRootFilesContextMenu(event) {
  if (cfg.sessionReview) return;
  openEditorContextMenu([
    { label: "Новий файл", icon: MENU_ICONS.newFile, onSelect: () => handleCreateTextFile().catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
    { label: "Нова папка", icon: MENU_ICONS.newFolder, onSelect: () => handleCreateFolder().catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
  ], event);
}

function closeTabsByNames(names) {
  const targets = new Set(names.filter(Boolean));
  if (!targets.size) return;
  captureActiveScroll();
  captureActiveSelection();
  s.openTabs = s.openTabs.filter(tab => !targets.has(tab.name));
  targets.forEach(name => {
    dropTabState(name);
    _tabScrolls.delete(name);
    _tabSelections.delete(name);
  });

  if (!s.openTabs.length) {
    s.activeTabName = "";
    s.selectedFile = { name: "", type: "", is_text: false };
    showEmptyEditor();
    renderEditorTabs();
    renderFileList();
    schedulePersistTabs();
    return;
  }

  if (targets.has(s.activeTabName)) {
    const next = s.openTabs[Math.max(0, Math.min(s.openTabs.length - 1, 0))];
    selectFile(next);
    return;
  }

  renderEditorTabs();
  renderFileList();
  schedulePersistTabs();
}

function buildTabContextMenuItems(tabName) {
  const activeIdx = s.openTabs.findIndex(tab => tab.name === tabName);
  const rightTabs = activeIdx >= 0 ? s.openTabs.slice(activeIdx + 1).map(tab => tab.name) : [];
  const otherTabs = s.openTabs.filter(tab => tab.name !== tabName).map(tab => tab.name);
  return [
    { label: "Закрити", icon: MENU_ICONS.close, shortcut: "Mod+W", onSelect: () => closeTab(tabName) },
    { label: "Закрити інші", icon: MENU_ICONS.closeOthers, onSelect: () => closeTabsByNames(otherTabs) , disabled: otherTabs.length === 0 },
    { label: "Закрити праворуч", icon: MENU_ICONS.closeRight, onSelect: () => closeTabsByNames(rightTabs), disabled: rightTabs.length === 0 },
    { type: "separator" },
    { label: "Закрити всі", icon: MENU_ICONS.closeAll, onSelect: () => closeTabsByNames(s.openTabs.map(tab => tab.name)), disabled: s.openTabs.length === 0 },
  ];
}

function openTabContextMenu(tabName, event) {
  openEditorContextMenu(buildTabContextMenuItems(tabName), event);
}

function openTabbarContextMenu(event) {
  openEditorContextMenu([
    { label: "Закрити всі", icon: MENU_ICONS.closeAll, onSelect: () => closeTabsByNames(s.openTabs.map(tab => tab.name)), disabled: s.openTabs.length === 0 },
  ], event);
}

function getSelectedProjectFile() {
  const name = String(s.selectedFile?.name || "");
  if (!name || name === s.mainFileName) return s.selectedFile?.name ? s.selectedFile : { name: s.mainFileName, type: "main", is_text: true, is_dir: false };
  return s.projectFiles.find(file => file.name === name) || s.selectedFile;
}

function buildEditorSelectionContextMenuItems(event = null) {
  const isTextFile = Boolean(s.selectedFile?.is_text && !s.selectedFile?.is_dir);
  const annotationsEnabled = Boolean(s.projectMeta?.longdoc?.enabled && s.projectMeta?.longdoc?.annotations_enabled);
  const selection = cm.getActiveSelectionDetails?.();
  const selectionRect = cm.getSelectionScreenRect?.();
  const hasSelection = Boolean(selection && !selection.empty && String(selection.selectedText || "").trim());
  return [
    {
      label: hasSelection ? "Додати помітку до виділення" : "Додати помітку тут",
      icon: MENU_ICONS.annotate,
      disabled: !isTextFile || !annotationsEnabled,
      onSelect: async () => {
        const fileName = String(s.activeTabName || s.selectedFile?.name || "");
        const lineStart = selection?.lineStart || 1;
        const lineEnd = selection?.lineEnd || lineStart;
        const instruction = await showAnnotationPopover({
          title: hasSelection ? "Помітка до виділення" : "Помітка до рядка",
          hint: hasSelection ? "Опишіть, що треба змінити в обраному фрагменті." : "Опишіть, що треба змінити в цьому місці.",
          target: `${fileName}:${lineStart}${lineEnd !== lineStart ? `-${lineEnd}` : ""}`,
          selectedText: hasSelection ? selection.selectedText : "Текст не виділено. Помітка буде прив’язана до поточного рядка.",
          rect: selectionRect,
          x: event?.clientX,
          y: event?.clientY,
        });
        if (!instruction || !instruction.trim()) return;
        try {
          await longdoc.createAnnotationFromEditorSelection?.(instruction.trim());
          setSaveHint("Помітку додано", "saved");
        } catch (err) {
          setSaveHint(`Помилка: ${err.message}`, "error");
        }
      },
    },
  ];
}

function openSelectionContextMenu(event) {
  const hadSelection = Boolean(cm.getActiveSelectionDetails?.() && !cm.getActiveSelectionDetails?.().empty);
  if (!hadSelection && event) {
    cm.setCursorFromClientPoint?.(event.clientX, event.clientY);
  }
  const items = buildEditorSelectionContextMenuItems(event).filter(Boolean);
  if (!items.length) return false;
  openEditorContextMenu(items, event);
  return true;
}

function renderAnnotationMarkers() {
  const currentFile = String(s.activeTabName || s.selectedFile?.name || "");
  const activeFile =
    currentFile === s.mainFileName
      ? { name: s.mainFileName, is_text: true, is_dir: false }
      : s.projectFiles.find(file => file.name === currentFile) || s.selectedFile;
  const isTextFile = Boolean(activeFile?.is_text && !activeFile?.is_dir);
  if (!currentFile || !isTextFile) {
    cm.setAnnotationMarkers?.([]);
    return;
  }
  const activeStatuses = new Set(["open", "in_progress"]);
  const groups = new Map();
  for (const item of s.longdoc.annotations || []) {
    if (!item || item.file_name !== currentFile || !activeStatuses.has(String(item.status || ""))) continue;
    const line = Math.max(1, Number(item.line_start) || 1);
    const existing = groups.get(line) || { line, count: 0, ids: [], status: "open", titles: [] };
    existing.count += 1;
    existing.ids.push(item.id);
    existing.status = existing.status === "in_progress" || item.status === "in_progress" ? "in_progress" : "open";
    existing.titles.push(String(item.instruction || "").trim());
    groups.set(line, existing);
  }
  cm.setAnnotationMarkers?.([...groups.values()].map(item => ({
    line: item.line,
    count: item.count,
    ids: item.ids,
    status: item.status,
    title: item.titles.filter(Boolean).slice(0, 3).join("\n"),
  })));
}

async function openAnnotationMarkerPopover(info, event) {
  const ids = Array.isArray(info?.ids) ? info.ids.map(Number).filter(Boolean) : [];
  if (!ids.length) return;
  const items = ids
    .map(id => (s.longdoc.annotations || []).find(item => Number(item?.id) === id))
    .filter(Boolean);
  if (!items.length) return;
  const currentFile = String(s.activeTabName || s.selectedFile?.name || "");
  const line = Math.max(1, Number(info?.line) || Number(items[0]?.line_start) || 1);
  const result = await showAnnotationInfoPopover({
    title: items.length > 1 ? `Помітки: ${items.length}` : "Помітка",
    target: `${currentFile}:${line}`,
    items,
    x: event?.clientX,
    y: event?.clientY,
  });
  if (!result?.action || !result?.id) return;
  if (result.action === "open") {
    longdoc.openAnnotationsPanel?.(result.id);
    return;
  }
  if (result.action === "save_edit") {
    const instruction = String(result.instruction || "").trim();
    if (!instruction) {
      setSaveHint("Текст помітки не може бути порожнім", "error");
      return;
    }
    try {
      await api(`/api/projects/${cfg.projectId}/annotations/${result.id}/`, {
        method: "PATCH",
        body: JSON.stringify({ instruction }),
      });
      await longdoc.loadLongdocData?.();
      setSaveHint("Помітку оновлено", "saved");
    } catch (err) {
      setSaveHint(`Помилка: ${err.message}`, "error");
    }
    return;
  }
  const nextStatus = result.action === "done" ? "done" : result.action === "dismiss" ? "dismissed" : "";
  if (!nextStatus) return;
  try {
    await api(`/api/projects/${cfg.projectId}/annotations/${result.id}/`, {
      method: "PATCH",
      body: JSON.stringify({ status: nextStatus }),
    });
    await longdoc.loadLongdocData?.();
    setSaveHint(nextStatus === "done" ? "Помітку завершено" : "Помітку відхилено", "saved");
  } catch (err) {
    setSaveHint(`Помилка: ${err.message}`, "error");
  }
}

function buildCommandPaletteItems() {
  const selected = getSelectedProjectFile();
  const canRename = Boolean(selected?.name);
  const canDelete = Boolean(selected?.name && selected.name !== s.mainFileName);
  const canSetMain = Boolean(selected?.name && !selected.is_dir && selected.name !== s.mainFileName);
  const isTypstProject = s.projectMeta?.markup_type === "typst";
  const isTypstFile = isTypstProject && String(s.activeTabName || "").endsWith(".typ");
  const lspEnabled = tinymist.isEnabled();
  const lspReady = lspEnabled && tinymist.getStatus() === "connected";
  return [
    { id: "compile", label: "Recompile project", hint: "Build", shortcut: "Mod+Enter", run: () => compileProject().catch(() => {}) },
    { id: "wrap", label: isLineWrappingEnabled() ? "Disable line wrap" : "Enable line wrap", hint: "Editor", shortcut: "Wrap", run: () => toggleLineWrap() },
    ...(isTypstFile && lspEnabled ? [
      { id: "format-doc", label: "Format document", hint: "Typst", shortcut: "Mod+Alt+L", disabled: !lspReady, run: () => formatCurrentDocument().catch(() => {}) },
      { id: "find-refs", label: "Find references", hint: "Typst", shortcut: "Mod+Click", disabled: !lspReady, run: () => findReferences().catch(() => {}) },
      { id: "document-symbols", label: "Document symbols", hint: "Typst", shortcut: "Mod+Shift+O", disabled: !lspReady, run: () => openDocumentSymbolsPalette() },
      { id: "workspace-symbols", label: "Workspace symbols", hint: "Typst", shortcut: "Mod+T", disabled: !lspReady, run: () => openWorkspaceSymbolsPalette() },
      { id: "rename-symbol", label: "Rename symbol", hint: "Typst", shortcut: "F2", disabled: !lspReady, run: () => renameCurrentSymbol().catch(() => {}) },
      { id: "restart-tinymist", label: "Restart Tinymist", hint: "Typst", run: () => tinymist.restart() },
    ] : []),
    { id: "new-file", label: "New file", hint: "Files", shortcut: "N", run: () => handleCreateTextFile().catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
    { id: "new-folder", label: "New folder", hint: "Files", shortcut: "Shift+N", run: () => handleCreateFolder().catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
    { id: "rename-file", label: `Rename: ${selected?.name || "current item"}`, hint: "Files", shortcut: "F2", disabled: !canRename, run: () => handleRenameFile(selected).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
    { id: "delete-file", label: `Delete: ${selected?.name || "current item"}`, hint: "Files", shortcut: "Del", danger: true, disabled: !canDelete, run: () => handleDeleteFile(selected).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
    { id: "set-main", label: `Set as main: ${selected?.name || "current item"}`, hint: "Files", disabled: !canSetMain, run: () => setMainFile(selected.name).then(() => reloadProjectTree({ selectPath: selected.name })).catch(err => setSaveHint(`Помилка: ${err.message}`, "error")) },
    { id: "close-tab", label: "Close active tab", hint: "Tabs", shortcut: "Mod+W", disabled: !s.activeTabName, run: () => closeActiveTab() },
    { id: "close-other-tabs", label: "Close other tabs", hint: "Tabs", disabled: s.openTabs.length <= 1, run: () => closeTabsByNames(s.openTabs.filter(tab => tab.name !== s.activeTabName).map(tab => tab.name)) },
    { id: "close-all-tabs", label: "Close all tabs", hint: "Tabs", disabled: !s.openTabs.length, run: () => closeTabsByNames(s.openTabs.map(tab => tab.name)) },
  ];
}

function closeCommandPalette() {
  _paletteMode = "commands";
  clearTimeout(_workspaceSymbolTimer);
  commandPaletteOverlayEl?.classList.remove("open");
  if (commandPaletteInputEl) commandPaletteInputEl.value = "";
  if (commandPaletteInputEl) commandPaletteInputEl.placeholder = "Введіть команду…";
  if (commandPaletteListEl) commandPaletteListEl.innerHTML = "";
}

function renderCommandPalette(query = "") {
  if (!commandPaletteListEl) return;
  const q = String(query || "").trim().toLowerCase();
  const items = buildCommandPaletteItems().filter(item => {
    if (!q) return true;
    return `${item.label} ${item.hint || ""} ${item.shortcut || ""}`.toLowerCase().includes(q);
  });
  commandPaletteListEl.innerHTML = "";
  items.forEach((item, idx) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `cp-item${item.danger ? " danger" : ""}`;
    btn.disabled = Boolean(item.disabled);
    btn.dataset.index = String(idx);
    btn.innerHTML = `
      <span class="cp-main">
        <span class="cp-label">${item.label}</span>
        <span class="cp-hint">${item.hint || ""}</span>
      </span>
      <span class="cp-shortcut">${item.shortcut || ""}</span>
    `;
    btn.addEventListener("click", () => {
      closeCommandPalette();
      if (!item.disabled) item.run?.();
    });
    commandPaletteListEl.appendChild(btn);
  });
  commandPaletteListEl.querySelector(".cp-item:not(:disabled)")?.classList.add("active");
}

function renderPaletteItems(items = []) {
  if (!commandPaletteListEl) return;
  commandPaletteListEl.innerHTML = "";
  items.forEach((item, idx) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `cp-item${item.danger ? " danger" : ""}`;
    btn.disabled = Boolean(item.disabled);
    btn.dataset.index = String(idx);
    btn.innerHTML = `
      <span class="cp-main">
        <span class="cp-label">${item.label}</span>
        <span class="cp-hint">${item.hint || ""}</span>
      </span>
      <span class="cp-shortcut">${item.shortcut || ""}</span>
    `;
    btn.addEventListener("click", () => {
      closeCommandPalette();
      if (!item.disabled) item.run?.();
    });
    commandPaletteListEl.appendChild(btn);
  });
  commandPaletteListEl.querySelector(".cp-item:not(:disabled)")?.classList.add("active");
}

function openDocumentSymbolsPalette() {
  _paletteMode = "document-symbols";
  commandPaletteOverlayEl?.classList.add("open");
  if (commandPaletteInputEl) {
    commandPaletteInputEl.value = "";
    commandPaletteInputEl.placeholder = "Символи поточного документа…";
    commandPaletteInputEl.focus();
  }
  renderDocumentSymbolsPalette("");
}

function renderDocumentSymbolsPalette(query = "") {
  const q = String(query || "").trim().toLowerCase();
  const items = (s.lspOutlineItems || [])
    .filter(item => !q || `${item.title} ${item.detail || ""}`.toLowerCase().includes(q))
    .map(item => ({
      label: item.title,
      hint: item.detail || `Line ${item.start_line}`,
      run: () => openOutlineLocation(item.file_name || s.activeTabName, item.start_line, 1),
    }));
  renderPaletteItems(items);
}

function openWorkspaceSymbolsPalette() {
  _paletteMode = "workspace-symbols";
  commandPaletteOverlayEl?.classList.add("open");
  const initialQuery = getTokenAroundCursor();
  if (commandPaletteInputEl) {
    commandPaletteInputEl.value = initialQuery;
    commandPaletteInputEl.placeholder = "Символи проєкту…";
    commandPaletteInputEl.focus();
  }
  renderWorkspaceSymbolsPalette(initialQuery);
}

async function renderWorkspaceSymbolsPalette(query = "") {
  const trimmedQuery = String(query || "").trim();
  if (!trimmedQuery) {
    renderPaletteItems([
      {
        label: "Введіть назву символу",
        hint: "Tinymist шукає символи проєкту за запитом. Постав курсор на слово або почни друкувати.",
        disabled: true,
        shortcut: "",
      },
    ]);
    return;
  }
  const requestId = ++_workspaceSymbolRequestId;
  clearTimeout(_workspaceSymbolTimer);
  _workspaceSymbolTimer = setTimeout(async () => {
    try {
      const result = await tinymist.requestWorkspaceSymbols(trimmedQuery);
      if (requestId !== _workspaceSymbolRequestId || _paletteMode !== "workspace-symbols") return;
      const root = (tinymist.getRootUri?.() || "");
      const rootSlash = root.endsWith("/") ? root : root + "/";
      const items = (Array.isArray(result) ? result : [])
        .map(item => {
          const loc = Array.isArray(item.locations) ? item.locations[0] : item.location;
          const uri = String(loc?.uri || "");
          const fileName = uri.startsWith(rootSlash) ? uri.slice(rootSlash.length) : uri;
          const line = (loc?.range?.start?.line ?? 0) + 1;
          return {
            label: item.name || fileName,
            hint: `${fileName}:${line}${item.containerName ? ` · ${item.containerName}` : ""}`,
            run: () => lspNavigateTo(fileName, line, (loc?.range?.start?.character ?? 0) + 1),
          };
        })
        .filter(item => item.label && item.hint);
      renderPaletteItems(items.length ? items : [
        {
          label: `Нічого не знайдено для “${trimmedQuery}”`,
          hint: "Спробуйте коротший або точніший запит.",
          disabled: true,
          shortcut: "",
        },
      ]);
    } catch (_) {
      if (requestId !== _workspaceSymbolRequestId || _paletteMode !== "workspace-symbols") return;
      renderPaletteItems([
        {
          label: "Не вдалося завантажити символи проєкту",
          hint: "Перезапустіть Tinymist або змініть запит.",
          disabled: true,
          shortcut: "",
        },
      ]);
    }
  }, 120);
}

function moveCommandPaletteSelection(delta) {
  if (!commandPaletteListEl) return;
  const enabled = [...commandPaletteListEl.querySelectorAll(".cp-item:not(:disabled)")];
  if (!enabled.length) return;
  let current = enabled.findIndex(el => el.classList.contains("active"));
  if (current === -1) current = 0;
  enabled.forEach(el => el.classList.remove("active"));
  const next = (current + delta + enabled.length) % enabled.length;
  enabled[next].classList.add("active");
  enabled[next].scrollIntoView({ block: "nearest" });
}

function executeActivePaletteItem() {
  const active = commandPaletteListEl?.querySelector(".cp-item.active:not(:disabled)");
  active?.click();
}

function openCommandPalette() {
  if (!commandPaletteOverlayEl || !commandPaletteInputEl) return;
  _paletteMode = "commands";
  commandPaletteOverlayEl.classList.add("open");
  commandPaletteInputEl.placeholder = "Введіть команду…";
  renderCommandPalette("");
  requestAnimationFrame(() => {
    commandPaletteInputEl.focus();
    commandPaletteInputEl.select();
  });
}

// ── Loaders ───────────────────────────────────────────────────────────────────

export async function loadProjectMeta() {
  const data = await api(`/api/projects/${cfg.projectId}/`, { method: "GET" });
  s.projectMeta = data || {};
  const prevMain = s.mainFileName;
  const nextMain = String(s.projectMeta.main_file_name || s.projectMeta.file_name || "main.tex");
  s.mainFileName      = nextMain;
  s.supportsSynctex   = Boolean(s.projectMeta.supports_synctex);
  initPreviewPanel();
  if (currentFileLbl) currentFileLbl.textContent = s.mainFileName;
  updateEditorTab(s.mainFileName);
  const selName = String(s.selectedFile?.name || "");
  if (!selName || s.selectedFile?.type === "main" || selName === prevMain || selName === "main.tex" || selName === "main.typ") {
    s.selectedFile = { name: s.mainFileName, type: "main", is_text: true };
  }
  renderSmallModelWarning();
  search.syncSearchTabVisibility?.();
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
  s.mainFileContent = data.content || "";
  // Re-check: user may have typed during the async fetch — don't overwrite their edits.
  if (!s.hasUnsavedChanges) {
    syncTabContent(s.mainFileName, s.mainFileContent, s.mainFileName);
    s.hasUnsavedChanges = false;
    setSaveHint("Завантажено", "saved");
  }
}

export async function loadFiles() {
  const p = await api(`/api/projects/${cfg.projectId}/files/`, { method: "GET" });
  s.projectFiles = p.files || [];
  normalizeFileTreeState();
  renderFileList();
}

export async function loadPdfEmbeds() {
  if (s.projectMeta?.markup_type !== "typst") return;
  const p = await api(`/api/projects/${cfg.projectId}/pdf-embed/`, { method: "GET" });
  s.pdfEmbeds = p.embeds || {};
  renderFileList();
}

async function togglePdfEmbed(filePath, enabled) {
  try {
    const result = await api(`/api/projects/${cfg.projectId}/pdf-embed/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: filePath, enabled }),
    });
    s.pdfEmbeds = { ...s.pdfEmbeds, [filePath]: result.entry };
    renderFileList();
    setSaveHint(enabled ? `PDF embed увімкнено. Скомпілюйте для генерації сторінок.` : `PDF embed вимкнено.`, "saved");
  } catch (err) {
    setSaveHint(`Помилка: ${err.message}`, "error");
  }
}

function insertPdfEmbedSnippet(filePath) {
  const snippet = `#smarttex-include-pdf("${filePath}")`;
  insertAtCursor(snippet);
  setSaveHint("Сніпет вставлено", "saved");
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
  if (isMobileWorkspace()) setMobileWorkspacePanel("editor");
  const prevFile = s.selectedFile;

  // Flush unsaved changes before switching
  if (s.hasUnsavedChanges && prevFile.is_text && !prevFile.is_dir) {
    clearTimeout(s.saveTimer);
    await saveCurrentFile();
  }

  // Snapshot current tab's editor state so its undo history is preserved.
  // Only save if the tab was already initialised — if it was still loading (no cached
  // state yet) the editor is showing a different file's content, so saving now would
  // corrupt the cache with stale content.
  if (prevFile.name && prevFile.is_text && !prevFile.is_dir && hasTabState(prevFile.name)) {
    saveTabState(prevFile.name);
  }
  captureActiveScroll();
  captureActiveSelection();

  s.selectedFile = { name: file.name, type: file.type || "asset", ...file };
  if (currentFileLbl) currentFileLbl.textContent = file.name;
  updateEditorTab(file.name);
  addTab(s.selectedFile);
  renderEditorTabs();
  renderFileList();

  if (file.name === s.mainFileName) {
    showEditorForText();
    if (hasTabState(s.mainFileName)) {
      activateTab(s.mainFileName, s.mainFileContent, s.mainFileName);
      applyTabSelection(file.name);
      setEditorDiagnostics(file.name, s.diagnostics);
      s.hasUnsavedChanges = false;
      setSaveHint("", "");
      applyTabScroll(file.name);
      focusEditor();
      primeTinymistMainContext(file.name);
      tinymist.didOpen(file.name, cm.getContent());
      tinymist.refreshActiveDocument(file.name);
      updateSignatureHelp();
      renderAnnotationMarkers();
      return;
    }
    setSaveHint("Завантаження…", "saving");
    try {
      await loadMainFile();
      if (s.activeTabName !== file.name) return;
      applyTabSelection(file.name);
      setEditorDiagnostics(file.name, s.diagnostics);
      s.hasUnsavedChanges = false;
      setSaveHint("Завантажено", "saved");
      applyTabScroll(file.name);
      focusEditor();
      primeTinymistMainContext(file.name);
      tinymist.didOpen(file.name, cm.getContent());
      tinymist.refreshActiveDocument(file.name);
      updateSignatureHelp();
      renderAnnotationMarkers();
    } catch (err) {
      if (s.activeTabName !== file.name) return;
      setSaveHint(`Помилка: ${err.message}`, "error");
    }
    return;
  }

  if (file.is_text && !file.is_dir) {
    showEditorForText();

    // If the tab was already loaded, restore its saved state (history intact)
    if (hasTabState(file.name)) {
      activateTab(file.name, "", file.name);
      applyTabSelection(file.name);
      setEditorDiagnostics(file.name, s.diagnostics);
      s.hasUnsavedChanges = false;
      setSaveHint("", "");
      applyTabScroll(file.name);
      focusEditor();
      primeTinymistMainContext(file.name);
      tinymist.didOpen(file.name, cm.getContent());
      tinymist.refreshActiveDocument(file.name);
      updateSignatureHelp();
      renderAnnotationMarkers();
      return;
    }

    // First visit — fetch from server and create fresh state
    setSaveHint("Завантаження…", "saving");
    try {
      const params = new URLSearchParams({ include_text: "1" });
      const data = await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(file.name)}/content/?${params}`);
      // User may have switched to another tab while the fetch was in flight.
      // Don't clobber the currently-visible editor; just warm the cache instead.
      if (s.activeTabName !== file.name) {
        activateTab(file.name, data.text_content || "", file.name, true, true);
        return;
      }
      activateTab(file.name, data.text_content || "", file.name);
      applyTabSelection(file.name);
      setEditorDiagnostics(file.name, s.diagnostics);
      s.hasUnsavedChanges = false;
      setSaveHint("Завантажено", "saved");
      applyTabScroll(file.name);
      focusEditor();
      primeTinymistMainContext(file.name);
      tinymist.didOpen(file.name, data.text_content || "");
      tinymist.refreshActiveDocument(file.name);
      updateSignatureHelp();
      renderAnnotationMarkers();
    } catch (err) {
      if (s.activeTabName !== file.name) return;
      setSaveHint(`Помилка: ${err.message}`, "error");
      s.selectedFile = { name: s.mainFileName, type: "main", is_text: true };
      if (currentFileLbl) currentFileLbl.textContent = s.mainFileName;
      activateTab(s.mainFileName, s.mainFileContent, s.mainFileName);
      s.hasUnsavedChanges = false;
      renderFileList();
    }
    return;
  }

  setEditorDiagnostics("", []);
  renderAnnotationMarkers();
  tinymist.refreshActiveDocument("");
  hideSignatureHelp();
  showAssetViewer(file);
}

// ── LSP actions ──────────────────────────────────────────────────────────────

async function formatCurrentDocument() {
  const filename = s.activeTabName || s.selectedFile?.name || "";
  if (!filename) return;
  const ok = await tinymist.formatDocument(filename).catch(() => false);
  if (ok) setSaveHint("Відформатовано", "saved");
}

async function renameCurrentSymbol() {
  const filename = s.activeTabName || s.selectedFile?.name || "";
  if (!filename || !cm.view) return;
  const { showRenameDialog } = await import("./ui.js");
  const currentName = getTokenAroundCursor();
  const newName = await showRenameDialog(currentName);
  if (!newName) return;
  if (newName === currentName) return;
  const pos = cm.view.state.selection.main.head;
  const doc = cm.view.state.doc;
  const line = doc.lineAt(pos);
  const ok = await tinymist.renameSymbol(filename, line.number, pos - line.from + 1, newName).catch(() => false);
  if (ok) {
    setSaveHint("Символ перейменовано", "saved");
    if (filename === s.mainFileName) loadMainFile().catch(() => {});
    else selectFile(s.selectedFile).catch(() => {});
  } else {
    setSaveHint("Tinymist не зміг перейменувати цей символ", "error");
  }
}

function hideSignatureHelp() {
  signatureHelpEl?.classList.remove("visible");
  if (signatureHelpEl) signatureHelpEl.innerHTML = "";
}

async function updateSignatureHelp() {
  clearTimeout(_signatureHelpTimer);
  _signatureHelpTimer = setTimeout(async () => {
    const filename = s.activeTabName || s.selectedFile?.name || "";
    if (!filename || !cm.view || !String(filename).endsWith(".typ") || tinymist.getStatus() !== "connected") {
      hideSignatureHelp();
      return;
    }
    const pos = cm.view.state.selection.main.head;
    const doc = cm.view.state.doc;
    const line = doc.lineAt(pos);
    try {
      const result = await tinymist.requestSignatureHelp(filename, line.number, pos - line.from + 1);
      const signatures = Array.isArray(result?.signatures) ? result.signatures : [];
      const activeSignature = signatures[result?.activeSignature || 0];
      if (!activeSignature?.label) {
        hideSignatureHelp();
        return;
      }
      const activeParam = Array.isArray(activeSignature.parameters)
        ? activeSignature.parameters[result?.activeParameter || 0]
        : null;
      signatureHelpEl.innerHTML = `<code>${activeSignature.label}</code>${activeParam?.label ? `<span>· ${activeParam.label}</span>` : ""}`;
      signatureHelpEl.classList.add("visible");
    } catch (_) {
      hideSignatureHelp();
    }
  }, 120);
}

async function findReferencesAt(filename, lineNum, charNum) {
  const refsList = document.getElementById("refs-list");
  if (refsList) refsList.innerHTML = "<div class='e-empty-card'>Пошук посилань…</div>";
  switchBottomTab("refs");
  try {
    const result = await tinymist.requestReferences(filename, lineNum, charNum);
    const locs = Array.isArray(result) ? result : (result ? [result] : []);
    if (!refsList) return;
    if (!locs.length) {
      refsList.innerHTML = "<div class='e-empty-card'>Посилання не знайдено.</div>";
      return;
    }
    const root = (tinymist.getRootUri?.() || "");
    const rootSlash = root.endsWith("/") ? root : root + "/";
    refsList.innerHTML = "";
    locs.forEach(loc => {
      const uri = String(loc.uri || "");
      const fname = uri.startsWith(rootSlash) ? uri.slice(rootSlash.length) : uri;
      const ln = (loc.range?.start?.line ?? 0) + 1;
      const ch = (loc.range?.start?.character ?? 0) + 1;
      const item = document.createElement("div");
      item.className = "e-diag-item e-ref-item";
      item.innerHTML = `<span class="e-diag-file">${fname}</span><span class="e-diag-loc">Ln ${ln}, Col ${ch}</span>`;
      item.style.cursor = "pointer";
      item.addEventListener("click", () => lspNavigateTo(fname, ln, ch));
      refsList.appendChild(item);
    });
  } catch (e) {
    if (refsList) refsList.innerHTML = `<div class='e-empty-card'>Помилка: ${e.message}</div>`;
  }
}

async function findReferences() {
  const filename = s.activeTabName || s.selectedFile?.name || "";
  if (!filename || !cm.view) return;
  const pos = cm.view.state.selection.main.head;
  const doc = cm.view.state.doc;
  const line = doc.lineAt(pos);
  await findReferencesAt(filename, line.number, pos - line.from + 1);
}

async function openDocumentLink(fileName) {
  const target = String(fileName || "");
  if (!target) return;
  const fileObj = target === s.mainFileName
    ? { name: s.mainFileName, type: "main", is_text: true }
    : s.projectFiles.find(f => f.name === target);
  if (fileObj) {
    await selectFile(fileObj);
  }
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
  s.editGeneration += 1;
  s.hasUnsavedChanges = true;
  setSaveHint("Є незбережені зміни…", "saving");
  renderEditorTabs();
  setCompileState("out_of_date", "pending");
  clearTimeout(s.saveTimer);
  clearTimeout(s.typstCompileTimer);

  const savedName = s.activeTabName || s.selectedFile.name;
  const isTypstTextFile = s.projectMeta?.markup_type === "typst" && String(savedName).toLowerCase().endsWith(".typ");
  const contentSnapshot = cm.getContent();
  const generationSnapshot = s.editGeneration;
  if (isTypstTextFile) {
    tinymist.didChange(savedName, contentSnapshot);
    syncPreviewMemoryFile(savedName, contentSnapshot);
  }
  s.saveTimer = setTimeout(() => {
    if (savedName === s.mainFileName) s.pendingSectionsRefresh = true;
    saveCurrentFile({ targetName: savedName, contentSnapshot, generation: generationSnapshot })
      .then(() => {})
      .catch(() => {});
  }, isTypstTextFile ? TYPST_AUTOSAVE_DEBOUNCE_MS : AUTOSAVE_DEBOUNCE_MS);
  if (isTypstTextFile) {
    s.typstCompileTimer = setTimeout(() => {
      s.pendingRealtimeCompile = true;
      saveCurrentFile({ targetName: savedName, contentSnapshot, generation: generationSnapshot })
        .then(() => {})
        .catch(() => {});
    }, TYPST_REALTIME_COMPILE_DEBOUNCE_MS);
  }
}

function selectOpenTabByOffset(offset) {
  if (!s.openTabs.length) return;
  const currentIdx = s.openTabs.findIndex(tab => tab.name === s.activeTabName);
  const baseIdx = currentIdx >= 0 ? currentIdx : 0;
  const nextIdx = (baseIdx + offset + s.openTabs.length) % s.openTabs.length;
  const next = s.openTabs[nextIdx];
  if (next && next.name !== s.activeTabName) selectFile(next);
}

function closeActiveTab() {
  if (!s.activeTabName) return;
  closeTab(s.activeTabName);
}

function isTextInputTarget(target) {
  if (!(target instanceof Element)) return false;
  if (target.closest("#cm-editor")) return false;
  return Boolean(target.closest("input, textarea, select, [contenteditable='true']"));
}

function handleGlobalEditorShortcuts(event) {
  if (event.defaultPrevented || isTextInputTarget(event.target)) return;
  const isMod = event.metaKey || event.ctrlKey;

  if (event.key === "F2") {
    const isTypstFile = String(s.activeTabName || "").endsWith(".typ");
    if (isTypstFile && tinymist.getStatus() === "connected") {
      event.preventDefault();
      renameCurrentSymbol().catch(() => {});
      return;
    }
  }

  if (isMod && event.altKey && (event.key === "l" || event.key === "L")) {
    const isTypstFile = String(s.activeTabName || "").endsWith(".typ");
    if (isTypstFile) {
      event.preventDefault();
      formatCurrentDocument().catch(() => {});
      return;
    }
  }

  if (isMod && event.shiftKey && (event.key === "O" || event.key === "o")) {
    const isTypstFile = String(s.activeTabName || "").endsWith(".typ");
    if (isTypstFile && tinymist.getStatus() === "connected") {
      event.preventDefault();
      openDocumentSymbolsPalette();
      return;
    }
  }

  if (isMod && !event.shiftKey && !event.altKey && (event.key === "t" || event.key === "T")) {
    if (s.projectMeta?.markup_type === "typst" && tinymist.getStatus() === "connected") {
      event.preventDefault();
      openWorkspaceSymbolsPalette();
      return;
    }
  }

  if (isMod && event.shiftKey && (event.key === "P" || event.key === "p")) {
    event.preventDefault();
    openCommandPalette();
    return;
  }
  if (!isMod) return;

  if (event.key === "w" || event.key === "W") {
    if (!s.activeTabName) return;
    event.preventDefault();
    closeActiveTab();
    return;
  }

  if (event.key === "PageUp") {
    event.preventDefault();
    selectOpenTabByOffset(-1);
    return;
  }

  if (event.key === "PageDown") {
    event.preventDefault();
    selectOpenTabByOffset(1);
  }
}

// ── Project menu ──────────────────────────────────────────────────────────────

function closeProjectMenu() { closeTopMenus(); }

// ── Init ──────────────────────────────────────────────────────────────────────

let _initPromise = null;

export function initEditorApp() {
  if (_initPromise) return _initPromise;
  _initPromise = (async () => {
  // Inject shared references to break circular deps
  setSelectFileRef(selectFile);
  setFileContextMenuRef(openFileContextMenu);
  setOutlineLocationRef(openOutlineLocation);
  longdoc.setLongdocProjectMetaRef?.(loadProjectMeta);
  longdoc.setLongdocAnnotationMarkersRef?.(renderAnnotationMarkers);
  longdoc.initSessionUI?.();
  search.setSearchSelectFileRef?.((file, line) => selectFile(file).then(() => {
    if (line && line > 1) jumpToLine(line);
  }));
  search.initSearchPanel?.();

  // Initialize CodeMirror
  initCodeMirror(
    cmParent,
    onEditorInput,
    () => {
      captureActiveSelection();
      schedulePersistTabs();
      if (cm.view) updateLineCol(cm.view);
      updateSignatureHelp();
      revealPreviewSelection(false);
    }
  );
  setEditorContextMenuProvider(openSelectionContextMenu);
  cm.setAnnotationMarkerClickProvider?.((info, event) => {
    openAnnotationMarkerPopover(info, event).catch(err => {
      setSaveHint(`Помилка: ${err.message}`, "error");
    });
  });
  applyWrapPreference(readWrapPreference());
  attachScrollListener();

  // Initialize UI subsystems
  initDialogs();
  initResizeHandles();
  initVersionsPanel();
  syncMobileWorkspacePanel();
  mobileWorkspaceMq.addEventListener("change", syncMobileWorkspacePanel);
  if (centerPanelEl && window.MutationObserver) {
    new MutationObserver(() => {
      if (!isMobileWorkspace()) return;
      if (centerPanelEl.classList.contains("wa-active")) setMobileWorkspacePanel("assistant");
      else if (editorShellEl?.dataset.mobilePanel === "assistant") setMobileWorkspacePanel("editor");
    }).observe(centerPanelEl, { attributes: true, attributeFilter: ["class"] });
  }
  mobileWorkspaceBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      const panel = btn.dataset.mobilePanel || "editor";
      if (panel === "assistant") {
        if (!centerPanelEl?.classList.contains("wa-active")) waToggleBtnEl?.click();
        else setMobileWorkspacePanel("assistant");
        return;
      }
      if (centerPanelEl?.classList.contains("wa-active")) closeWritingAssistantTab();
      setMobileWorkspacePanel(panel);
    });
  });

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
  document.getElementById("sb-wrap-toggle")?.addEventListener("click", toggleLineWrap);
  editorTabbarEl?.addEventListener("contextmenu", e => {
    if (e.target instanceof Element && e.target.closest(".e-edtab")) return;
    openTabbarContextMenu(e);
  });
  fileListEl?.addEventListener("contextmenu", e => {
    if (e.target instanceof Element && e.target.closest(".e-file-btn")) return;
    openRootFilesContextMenu(e);
  });
  fileListEl?.addEventListener("click", e => {
    if (e.target instanceof Element && e.target.closest(".e-file-btn")) return;
    clearSelectedFolderPath();
    renderFileList();
  });

  // Project menu
  fileMenuBtn?.addEventListener("click", () => toggleTopMenu(fileMenuEl));
  editMenuBtn?.addEventListener("click", () => toggleTopMenu(editMenuEl));
  typstMenuBtn?.addEventListener("click", () => toggleTopMenu(typstMenuEl));
  projectMenuBtn?.addEventListener("click", () => toggleTopMenu(projectMenuEl));
  document.addEventListener("click", e => {
    if (!editorContextMenuEl?.contains(e.target)) closeEditorContextMenu();
    const insideTopMenu = [fileMenuBtn, fileMenuEl, editMenuBtn, editMenuEl, typstMenuBtn, typstMenuEl, projectMenuBtn, projectMenuEl]
      .some(el => el?.contains?.(e.target));
    if (!insideTopMenu) closeProjectMenu();
  });
  commandPaletteInputEl?.addEventListener("input", e => {
    const value = e.target.value;
    if (_paletteMode === "document-symbols") {
      renderDocumentSymbolsPalette(value);
      return;
    }
    if (_paletteMode === "workspace-symbols") {
      renderWorkspaceSymbolsPalette(value);
      return;
    }
    renderCommandPalette(value);
  });
  commandPaletteInputEl?.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      moveCommandPaletteSelection(1);
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      moveCommandPaletteSelection(-1);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      executeActivePaletteItem();
    }
  });
  commandPaletteOverlayEl?.addEventListener("click", e => {
    if (e.target === commandPaletteOverlayEl) closeCommandPalette();
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
  createTemplateBtnEl?.addEventListener("click", () => { closeProjectMenu(); openCreateTemplateDialog(); });
  document.getElementById("menu-file-new-file")?.addEventListener("click", () => { closeTopMenus(); handleCreateTextFile().catch(err => setSaveHint(`Помилка: ${err.message}`, "error")); });
  document.getElementById("menu-file-new-folder")?.addEventListener("click", () => { closeTopMenus(); handleCreateFolder().catch(err => setSaveHint(`Помилка: ${err.message}`, "error")); });
  document.getElementById("menu-file-save")?.addEventListener("click", () => { closeTopMenus(); saveCurrentFile().catch(() => {}); });
  document.getElementById("menu-file-close-tab")?.addEventListener("click", () => { closeTopMenus(); closeActiveTab(); });
  document.getElementById("menu-edit-undo")?.addEventListener("click", () => { closeTopMenus(); runUndo(); });
  document.getElementById("menu-edit-redo")?.addEventListener("click", () => { closeTopMenus(); runRedo(); });
  document.getElementById("menu-edit-command-palette")?.addEventListener("click", () => { closeTopMenus(); openCommandPalette(); });
  document.getElementById("menu-edit-wrap")?.addEventListener("click", () => { closeTopMenus(); toggleLineWrap(); });
  document.getElementById("menu-typst-document-symbols")?.addEventListener("click", () => { closeTopMenus(); openDocumentSymbolsPalette(); });
  document.getElementById("menu-typst-workspace-symbols")?.addEventListener("click", () => { closeTopMenus(); openWorkspaceSymbolsPalette(); });
  document.getElementById("menu-typst-rename")?.addEventListener("click", () => { closeTopMenus(); renameCurrentSymbol().catch(() => {}); });
  document.getElementById("menu-typst-format")?.addEventListener("click", () => { closeTopMenus(); formatCurrentDocument().catch(() => {}); });
  document.getElementById("menu-typst-find-refs")?.addEventListener("click", () => { closeTopMenus(); findReferences().catch(() => {}); });
  document.getElementById("menu-typst-compile")?.addEventListener("click", () => { closeTopMenus(); compileProject().catch(() => {}); });
  document.getElementById("menu-typst-restart")?.addEventListener("click", () => {
    closeTopMenus();
    if (tinymist.isEnabled()) tinymist.restart();
  });

  // PDF actions
  refreshPdfBtn?.addEventListener("click", () => {
    if (s.projectMeta?.markup_type === "typst" && getPreviewMode() === "web") {
      refreshTypstPreview(true).catch(() => {});
      return;
    }
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
    const targetFolderPath = getSelectedFolderPath();
    for (const f of [...(e.target.files || [])]) {
      try {
        if (isImageFile(f)) { await uploadImageWithRename(f, targetFolderPath); }
        else { await uploadFile(f, targetFolderPath); }
      } catch (err) { setSaveHint(`Помилка: ${err.message}`, "error"); }
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
  newFolderBtn?.addEventListener("click",    () => { if (!cfg.sessionReview) handleCreateFolder().catch(err => setSaveHint(`Помилка: ${err.message}`, "error")); });
  newTextFileBtn?.addEventListener("click",  () => { if (!cfg.sessionReview) handleCreateTextFile().catch(err => setSaveHint(`Помилка: ${err.message}`, "error")); });

  // Drag & drop on drop zone
  let dragCounter = 0;
  dropZone?.addEventListener("dragenter", e => { if (cfg.sessionReview) return; e.preventDefault(); dragCounter++; dropZone.classList.add("drag-active"); });
  dropZone?.addEventListener("dragleave", () => { if (cfg.sessionReview) return; if (--dragCounter <= 0) { dragCounter = 0; dropZone.classList.remove("drag-active"); } });
  dropZone?.addEventListener("dragover",  e => { if (!cfg.sessionReview) e.preventDefault(); });
  dropZone?.addEventListener("drop", async e => {
    if (cfg.sessionReview) return;
    e.preventDefault(); dragCounter = 0; dropZone.classList.remove("drag-active");
    const targetFolderPath = getSelectedFolderPath();
    for (const f of [...(e.dataTransfer.files || [])]) {
      if (f.name.toLowerCase().endsWith(".zip")) {
        try { await uploadZip(f); } catch (err) { setSaveHint(`Помилка ZIP: ${err.message}`, "error"); }
      } else if (isImageFile(f)) {
        try { await uploadImageWithRename(f, targetFolderPath); } catch (err) { setSaveHint(`Помилка: ${err.message}`, "error"); }
      } else {
        try { await uploadFile(f, targetFolderPath); } catch (err) { setSaveHint(`Помилка: ${err.message}`, "error"); }
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
    const targetFolderPath = getSelectedFolderPath();
    for (let i = 0; i < files.length; i++) {
      const f = normalizeClipboardFile(files[i], i);
      try {
        if (isImageFile(f)) { await uploadImageWithRename(f, targetFolderPath); }
        else { await uploadFile(f, targetFolderPath); }
      } catch (err) { setSaveHint(`Помилка upload: ${err.message}`, "error"); }
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
      closeAiLogOverlay();
      return;
    }
    if (e.key === "Escape" && commandPaletteOverlayEl?.classList.contains("open")) {
      closeCommandPalette();
      return;
    }
    if (e.key === "Escape" && isMenuOpen(editorContextMenuEl)) closeEditorContextMenu();
  });
  document.addEventListener("keydown", handleGlobalEditorShortcuts);

  // Beforeunload cleanup
  window.addEventListener("pagehide", () => {
    captureActiveScroll();
    captureActiveSelection();
    persistTabs();
  });
  window.addEventListener("beforeunload", () => {
    captureActiveScroll();
    captureActiveSelection();
    persistTabs();
    clearTimeout(_persistTabsTimer);
    if (s.statusPollTimer)         clearInterval(s.statusPollTimer);
    if (s.typstCompileTimer)       clearTimeout(s.typstCompileTimer);
    if (s.projectSse) { try { s.projectSse.close(); } catch (_) {} s.projectSse = null; }
    tinymist.disconnect();
  });

  // ── Tinymist setup ──
  tinymist.initStatusEl(document.getElementById("sb-tinymist"));
  tinymist.setNavigationCallback(lspNavigateTo);
  tinymist.setReferencesCallback(findReferencesAt);
  setPreviewCodeNavigationCallback(lspNavigateTo);
  tinymist.setDocumentSymbolsCallback(items => {
    s.lspOutlineItems = Array.isArray(items) ? items : [];
    renderOutline(openOutlineLocation);
  });
  tinymist.setDocumentLinksCallback(openDocumentLink);

  // References tab button
  document.getElementById("tab-refs-btn")?.addEventListener("click", () => switchBottomTab("refs"));

  // ── Load initial data ──
  restoreFileTreeState();
  await loadProjectMeta();
  if (s.projectMeta?.markup_type === "typst") {
    await loadMainFile();
  }
  await Promise.all([loadFiles(), loadSections(), loadVersions(true), loadPdfEmbeds(), longdoc.loadLongdocData?.()]);
  setCompileState("out_of_date");
  if (tinymist.shouldAutostart()) tinymist.connect();

  const restored = await restoreTabsFromStorage();
  if (!restored) {
    s.openTabs = [];
    s.activeTabName = "";
    s.selectedFile = { name: "", type: "", is_text: false };
    if (currentFileLbl) currentFileLbl.textContent = "";
    updateEditorTab("");
    showEmptyEditor();
    renderEditorTabs();
  }

  const cd = await api(`/api/projects/${cfg.projectId}/compile/`, { method: "GET" });
  if (cd.log) { logEl.textContent = cd.log; openLog(); }
  await refreshLivePdfPreview(cd.pdf_url || null, cd.pdf_version ?? null);

  renderFileList();
  connectProjectUpdatesSse();
  })().catch(err => {
    _initPromise = null;
    setSaveHint(`Помилка ініціалізації: ${err.message}`, "error");
    throw err;
  });
  return _initPromise;
}
