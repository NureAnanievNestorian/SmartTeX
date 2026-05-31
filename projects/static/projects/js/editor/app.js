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

const { s, cfg } = state;
const { api } = apiMod;
const {
  initCodeMirror, switchLanguage,
  focusEditor, jumpToLine, getSelectionSnapshot, setSelectionSnapshot, setEditorDiagnostics,
  setLineWrapping, isLineWrappingEnabled,
  saveTabState, hasTabState, activateTab, dropTabState,
} = cm;
const { loadPdfViewer, pdfEmpty } = pdfviewer;
const {
  setSaveHint, setCompileState, updateEditorTab, openLog,
  switchBottomTab, initDialogs, initResizeHandles, updateLineCol, updateWrapToggle,
  logToggleBtn, tabProblemsBtn, bottomCloseBtn, bottomPanel, editorWrapEl, assetView,
} = ui;
const {
  renderFileList, renderOutline, showEditorForText, showAssetViewer, showEmptyEditor,
  setSelectFileRef, setFileContextMenuRef, uploadFile, uploadZip, normalizeClipboardFile,
  createFolder, createEmptyTextFile, moveFileToFolder, deleteFile,
  isUploadableProjectFile, utf8ByteSize, pathBaseName, getFileTypeClass,
  setMainFile,
} = files;
const { renderVersions, initVersionsPanel, closeDiffModal } = versions;
const {
  saveCurrentFile, compileProject, runCompile, updateCompileArtifacts,
  pollCompileStatus, connectProjectUpdatesSse, deleteCurrentProject,
  renameCurrentProject, setOutlineLocationRef, openCreateTemplateDialog,
} = compile;

// ── Bootstrap config (set by inline script in template) ──────────────────────

const editorConfig = window.EDITOR_CONFIG || {};
cfg.projectId  = editorConfig.projectId  || 0;
cfg.csrfToken  = editorConfig.csrfToken  || "";
cfg.sessionReview = Boolean(editorConfig.sessionReview);
cfg.sessionReviewUrl = editorConfig.sessionReviewUrl || "";

// ── Tab bar ───────────────────────────────────────────────────────────────────

const editorTabbarEl = document.getElementById("editor-tabbar");

function closeWritingAssistantTab() {
  document.getElementById("drop-zone")?.classList.remove("wa-active");
  document.getElementById("wa-tab-btn")?.classList.remove("active");
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
const WRAP_PREF_KEY = "smarttex.editor.lineWrap";
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

function syncTabContent(name, text, filename) {
  if (!cm.view || !name) return;
  if (name === s.activeTabName) {
    const current = cm.view.state.doc.toString();
    if (current === text) return;
    const prevSel = cm.view.state.selection;
    activateTab(name, text, filename || name, true);
    try {
      const docLen = cm.view.state.doc.length;
      const clampedRanges = prevSel.ranges.map(range => ({
        anchor: Math.min(range.anchor, docLen),
        head: Math.min(range.head, docLen),
      }));
      cm.view.dispatch({ selection: { ranges: clampedRanges, mainIndex: prevSel.mainIndex } });
    } catch (_) {}
    return;
  }
  activateTab(name, text, filename || name, true, !!s.activeTabName);
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
  const created = await createFolder(parentPath);
  if (created) await reloadProjectTree({ selectPath: created, preferDir: true });
}

async function handleCreateTextFile(parentPath = "") {
  const created = await createEmptyTextFile(parentPath);
  if (created) await reloadProjectTree({ selectPath: created });
}

async function handleRenameFile(file) {
  const currentName = String(file?.name || "");
  if (!currentName) return;
  const nextName = await files.renameFile(file);
  if (!nextName || nextName === currentName) return;

  if (file?.is_dir) {
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

function buildCommandPaletteItems() {
  const selected = getSelectedProjectFile();
  const canRename = Boolean(selected?.name);
  const canDelete = Boolean(selected?.name && selected.name !== s.mainFileName);
  const canSetMain = Boolean(selected?.name && !selected.is_dir && selected.name !== s.mainFileName);
  return [
    { id: "compile", label: "Recompile project", hint: "Build", shortcut: "Mod+Enter", run: () => compileProject().catch(() => {}) },
    { id: "wrap", label: isLineWrappingEnabled() ? "Disable line wrap" : "Enable line wrap", hint: "Editor", shortcut: "Wrap", run: () => toggleLineWrap() },
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
  commandPaletteOverlayEl?.classList.remove("open");
  if (commandPaletteInputEl) commandPaletteInputEl.value = "";
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
  commandPaletteOverlayEl.classList.add("open");
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
  }, isTypstTextFile ? 400 : 2500);
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

function closeProjectMenu() {
  projectMenuEl?.classList.remove("open");
}

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
    }
  );
  applyWrapPreference(readWrapPreference());
  attachScrollListener();

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
  document.getElementById("sb-wrap-toggle")?.addEventListener("click", toggleLineWrap);
  editorTabbarEl?.addEventListener("contextmenu", e => {
    if (e.target instanceof Element && e.target.closest(".e-edtab")) return;
    openTabbarContextMenu(e);
  });
  fileListEl?.addEventListener("contextmenu", e => {
    if (e.target instanceof Element && e.target.closest(".e-file-btn")) return;
    openRootFilesContextMenu(e);
  });

  // Project menu
  projectMenuBtn?.addEventListener("click", () => projectMenuEl?.classList.toggle("open"));
  document.addEventListener("click", e => {
    if (!editorContextMenuEl?.contains(e.target)) closeEditorContextMenu();
    if (!projectMenuBtn?.contains(e.target) && !projectMenuEl?.contains(e.target)) closeProjectMenu();
  });
  commandPaletteInputEl?.addEventListener("input", e => renderCommandPalette(e.target.value));
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
  });

  // ── Load initial data ──
  await loadProjectMeta();
  await Promise.all([loadFiles(), loadSections(), loadVersions(true), longdoc.loadLongdocData?.()]);
  setCompileState("out_of_date");

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
