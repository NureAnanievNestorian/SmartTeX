import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as ui from "./ui.js";
import * as cm from "./cm.js";

const { s, cfg } = state;
const { api } = apiMod;
const {
  escHtml, fmtBytes, editorWrapEl, assetView, assetBox,
  setSaveHint, updateEditorTab,
} = ui;
const { setContent, switchLanguage, focusEditor, refreshLayout } = cm;

// selectFile is set from main.js to avoid circular dep at init time
let _selectFile = null;
export function setSelectFileRef(fn) { _selectFile = fn; }
export function selectFile(file) { return _selectFile?.(file); }

let _openFileContextMenu = null;
export function setFileContextMenuRef(fn) { _openFileContextMenu = fn; }

// ── File type utils ──────────────────────────────────────────────────────────

const FTYPE_MAP = {
  typ: "ft-typ", tex: "ft-tex", sty: "ft-tex", cls: "ft-tex",
  md: "ft-md", pdf: "ft-pdf", yaml: "ft-yaml", yml: "ft-yaml",
  json: "ft-json", bib: "ft-bib", csl: "ft-csl",
  png: "ft-img", jpg: "ft-img", jpeg: "ft-img", gif: "ft-img",
  svg: "ft-img", webp: "ft-img", csv: "ft-data", txt: "ft-txt",
};

export function getFileTypeClass(name) {
  const ext = String(name || "").split(".").pop().toLowerCase();
  return FTYPE_MAP[ext] || "ft-other";
}

export function isImageFile(file) {
  const name = String(file?.name || "").toLowerCase();
  const mime = String(file?.type || "").toLowerCase();
  return /\.(png|jpe?g|gif|webp|svg|bmp)$/.test(name) || mime.startsWith("image/");
}

export function isTextFile(file) {
  const name = String(file?.name || "").toLowerCase();
  const textExts = /\.(tex|typ|bib|md|txt|yaml|yml|json|sty|cls|csl|csv|tsv|html|htm|xml|toml|ini|cfg|conf|sh|py|js|ts)$/;
  return textExts.test(name) || (String(file?.type || "").startsWith("text/"));
}

export function isPdfFile(file) {
  return String(file?.name || "").toLowerCase().endsWith(".pdf") ||
    String(file?.type || "").toLowerCase() === "application/pdf";
}

export function isUploadableProjectFile(file) {
  return isImageFile(file) || isTextFile(file) || isPdfFile(file);
}

export function utf8ByteSize(text) {
  return new TextEncoder().encode(text).length;
}

export function displaySizeForEntry(file) {
  if (file.is_dir) return null;
  return file.size ?? null;
}

// ── Path utils ───────────────────────────────────────────────────────────────

export function splitPath(path) {
  const i = String(path || "").lastIndexOf("/");
  return i === -1 ? ["", path] : [path.slice(0, i), path.slice(i + 1)];
}

export function pathBaseName(path) { return splitPath(path)[1]; }
export function pathDirName(path)  { return splitPath(path)[0]; }

function joinProjectPath(parentPath, childName) {
  const parent = String(parentPath || "").replace(/\/+$/, "");
  const child = String(childName || "").replace(/^\/+/, "");
  if (!parent) return child;
  if (!child) return parent;
  return `${parent}/${child}`;
}

function fileTreeStorageKey() {
  return cfg.projectId ? `smarttex.editor.fileTree.${cfg.projectId}` : "";
}

function projectDirectorySet() {
  return new Set(
    s.projectFiles
      .filter(file => file?.is_dir)
      .map(file => String(file.name || "").replace(/\/+$/, ""))
      .filter(Boolean)
  );
}

function updateSelectedFolderPath(nextPath) {
  s.selectedFolderPath = String(nextPath || "").replace(/\/+$/, "");
}

export function getSelectedFolderPath() {
  return s.selectedFolderPath || "";
}

export function setSelectedFolderPath(nextPath) {
  updateSelectedFolderPath(nextPath);
  persistFileTreeState();
}

export function clearSelectedFolderPath() {
  if (!s.selectedFolderPath) return;
  s.selectedFolderPath = "";
  persistFileTreeState();
}

export function persistFileTreeState() {
  const key = fileTreeStorageKey();
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify({
      collapsedFolders: [...s.collapsedFolders],
      selectedFolderPath: s.selectedFolderPath || "",
    }));
  } catch (_) {}
}

export function restoreFileTreeState() {
  const key = fileTreeStorageKey();
  if (!key) return;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    s.collapsedFolders = new Set(
      Array.isArray(parsed?.collapsedFolders)
        ? parsed.collapsedFolders.map(item => String(item || "").replace(/\/+$/, "")).filter(Boolean)
        : []
    );
    updateSelectedFolderPath(parsed?.selectedFolderPath || "");
  } catch (_) {}
}

export function normalizeFileTreeState() {
  const dirs = projectDirectorySet();
  const nextCollapsed = [...s.collapsedFolders].filter(path => dirs.has(path));
  const changedCollapsed = nextCollapsed.length !== s.collapsedFolders.size;
  if (changedCollapsed) s.collapsedFolders = new Set(nextCollapsed);
  const currentSelected = String(s.selectedFolderPath || "");
  const selectedExists = !currentSelected || dirs.has(currentSelected);
  if (!selectedExists) s.selectedFolderPath = "";
  if (changedCollapsed || !selectedExists) persistFileTreeState();
}

export function remapFolderTreeState(oldPrefix, newPrefix) {
  const from = String(oldPrefix || "").replace(/\/+$/, "");
  const to = String(newPrefix || "").replace(/\/+$/, "");
  if (!from || !to || from === to) return;

  const remapped = new Set();
  s.collapsedFolders.forEach(path => {
    if (path === from || path.startsWith(`${from}/`)) remapped.add(`${to}${path.slice(from.length)}`);
    else remapped.add(path);
  });
  s.collapsedFolders = remapped;

  if (s.selectedFolderPath === from || s.selectedFolderPath.startsWith(`${from}/`)) {
    s.selectedFolderPath = `${to}${s.selectedFolderPath.slice(from.length)}`;
  }
  persistFileTreeState();
}

export function removeFolderTreeState(prefix) {
  const target = String(prefix || "").replace(/\/+$/, "");
  if (!target) return;
  s.collapsedFolders = new Set(
    [...s.collapsedFolders].filter(path => path !== target && !path.startsWith(`${target}/`))
  );
  if (s.selectedFolderPath === target || s.selectedFolderPath.startsWith(`${target}/`)) {
    s.selectedFolderPath = "";
  }
  persistFileTreeState();
}

export function compareTreeNodes(a, b) {
  if (a.file?.is_dir && !b.file?.is_dir) return -1;
  if (!a.file?.is_dir && b.file?.is_dir) return 1;
  return String(a.name || a.key || "").localeCompare(String(b.name || b.key || ""), undefined, { sensitivity: "base" });
}

export function buildProjectTree(entries) {
  const root = { key: null, name: "", depth: 0, children: [], file: null };
  const dirMap = { "": root };

  for (const entry of entries) {
    if (entry.is_dir) {
      const dir = entry.name;
      const parentDir = pathDirName(dir);
      const parent = dirMap[parentDir] || root;
      if (!dirMap[dir]) {
        const node = {
          key: dir,
          name: pathBaseName(dir) || dir,
          depth: parent.depth + 1,
          children: [],
          file: entry,
        };
        dirMap[dir] = node;
        parent.children.push(node);
      } else {
        dirMap[dir].file = entry;
      }
      continue;
    }
    const dir = pathDirName(entry.name);
    if (dir && !dirMap[dir]) {
      const parentDir = pathDirName(dir);
      const parent = dirMap[parentDir] || root;
      const node = {
        key: dir,
        name: pathBaseName(dir) || dir,
        depth: parent.depth + 1,
        children: [],
        file: { name: dir, is_dir: true, is_text: false, is_image: false, type: "asset" },
      };
      dirMap[dir] = node;
      parent.children.push(node);
    }
    const parent = dirMap[pathDirName(entry.name)] || root;
    parent.children.push({
      key: entry.name,
      name: pathBaseName(entry.name) || entry.name,
      depth: parent.depth + 1,
      children: [],
      file: entry,
    });
  }
  return root;
}

// ── File list rendering ───────────────────────────────────────────────────────

const fileListEl = document.getElementById("file-list");

export function renderFileList() {
  if (!fileListEl) return;
  fileListEl.innerHTML = "";
  const mainEntry = { name: s.mainFileName, is_text: true, type: "main", is_dir: false };
  const treeRoot  = buildProjectTree(s.projectFiles.filter(f => f.name !== s.mainFileName));

  function renderEntry(file, depth = 0, labelOverride = null) {
    const li  = document.createElement("li");
    const row = document.createElement("div");
    row.className = "e-file-row";

    const btn = document.createElement("button");
    btn.className = `e-file-btn${s.selectedFile.name === file.name ? " active" : ""}`;
    if (file.is_dir && s.selectedFolderPath === file.name) {
      btn.classList.add("folder-selected");
    }
    if (file.name === ".smarttex" || file.name.startsWith(".smarttex/")) {
      btn.classList.add("smarttex-folder");
    }
    btn.style.paddingLeft = `${8 + depth * 14}px`;
    btn.dataset.path  = file.name;
    btn.dataset.isDir = file.is_dir ? "1" : "0";
    if (file.is_dir) btn.dataset.folderPath = file.name;

    const collapsed = file.is_dir && s.collapsedFolders.has(file.name);
    const folderToggle = file.is_dir
      ? `<span class="e-folder-toggle"><svg viewBox="0 0 10 10" fill="currentColor" style="transform:rotate(${collapsed ? "-90deg" : "0deg"});transition:transform .1s"><polygon points="1,3 9,3 5,8"/></svg></span>`
      : `<span style="display:inline-block;width:16px;flex-shrink:0"></span>`;
    let icon;
    if (file.is_dir) {
      icon = collapsed
        ? `<svg viewBox="0 0 16 16" fill="currentColor" style="width:14px;height:14px;flex-shrink:0;color:#dcb67a"><path d="M1 5.5a1 1 0 011-1h3.9l1.1 1.5H14.5a.5.5 0 01.5.5v6a.5.5 0 01-.5.5H2a1 1 0 01-1-1V5.5z"/></svg>`
        : `<svg viewBox="0 0 16 16" fill="none" style="width:14px;height:14px;flex-shrink:0"><path fill="#dcb67a" d="M1 5.5a1 1 0 011-1h3.9l1.1 1.5H15v1.5H1V5.5z"/><path fill="#dcb67a" fill-opacity=".85" d="M1 7.5h14l-1.2 4.5a1 1 0 01-.96.72H3.16a1 1 0 01-.96-.72L1 7.5z"/></svg>`;
    } else {
      icon = `<span class="e-ftype-dot ${getFileTypeClass(file.name)}"></span>`;
    }
    const size     = displaySizeForEntry(file);
    const label    = labelOverride || pathBaseName(file.name) || file.name;
    const mainStar = file.name === s.mainFileName ? `<span class="e-main-star" title="Main file">●</span>` : "";
    const pdfEmbed = (!file.is_dir && String(file.name).toLowerCase().endsWith(".pdf") && s.pdfEmbeds?.[file.name]?.enabled)
      ? `<span class="e-pdf-embed-badge" title="PDF embed увімкнено${s.pdfEmbeds[file.name].page_count ? ` · ${s.pdfEmbeds[file.name].page_count} стор.` : ""}">↪</span>`
      : "";
    btn.innerHTML  = `<span class="e-file-name">${folderToggle}${icon}${escHtml(label)}${mainStar}${pdfEmbed}</span><span class="e-file-sz">${size == null ? "" : fmtBytes(size)}</span>`;
    if (file.is_dir) {
      const toggleFolder = () => {
        if (s.collapsedFolders.has(file.name)) s.collapsedFolders.delete(file.name);
        else s.collapsedFolders.add(file.name);
        persistFileTreeState();
        renderFileList();
      };
      btn.addEventListener("click", e => {
        if (e.target instanceof Element && e.target.closest(".e-folder-toggle")) {
          toggleFolder();
          return;
        }
        const isSameFolder = s.selectedFolderPath === file.name;
        updateSelectedFolderPath(isSameFolder ? "" : file.name);
        persistFileTreeState();
        setSaveHint(isSameFolder ? "Цільову папку знято" : `Цільова папка: ${file.name}`, "saved");
        renderFileList();
      });
      btn.style.cursor = "pointer";
    } else {
      btn.addEventListener("click", () => _selectFile?.(file));
    }
    btn.addEventListener("contextmenu", e => {
      if (!_openFileContextMenu) return;
      e.preventDefault();
      e.stopPropagation();
      _openFileContextMenu(file, e);
    });

    if (!cfg.sessionReview && !file.is_dir && file.name !== s.mainFileName) {
      btn.draggable = true;
      btn.addEventListener("dragstart", e => {
        s.draggedFilePath = file.name;
        try { e.dataTransfer?.setData("text/plain", file.name); if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; } catch (_) {}
      });
      btn.addEventListener("dragend", () => {
        s.draggedFilePath = null;
        fileListEl.querySelectorAll(".e-file-btn.drag-target").forEach(n => n.classList.remove("drag-target"));
      });
    }

    if (!cfg.sessionReview && file.is_dir) {
      btn.addEventListener("dragover", e => {
        if (!s.draggedFilePath || s.draggedFilePath.startsWith(`${file.name}/`)) return;
        e.preventDefault(); btn.classList.add("drag-target");
      });
      btn.addEventListener("dragleave", () => btn.classList.remove("drag-target"));
      btn.addEventListener("drop", e => {
        e.preventDefault(); e.stopPropagation(); btn.classList.remove("drag-target");
        const src = s.draggedFilePath || e.dataTransfer?.getData("text/plain") || "";
        if (src) moveFileToFolder(src, file.name).catch(err => setSaveHint(`Помилка: ${err.message}`, "error"));
      });
    }

    row.appendChild(btn);

    // File action buttons
    const actions = document.createElement("div");
    actions.className = "e-file-actions";
    if (!cfg.sessionReview && !file.is_dir && file.is_text && file.name !== s.mainFileName) {
      const setMainBtn = document.createElement("button");
      setMainBtn.type = "button"; setMainBtn.className = "e-file-act"; setMainBtn.textContent = "★";
      setMainBtn.title = "Set as main file";
      setMainBtn.addEventListener("click", e => { e.preventDefault(); e.stopPropagation(); setMainFile(file.name).catch(() => {}); });
      actions.appendChild(setMainBtn);
    }
    const renameBtn = document.createElement("button");
    renameBtn.type = "button"; renameBtn.className = "e-file-act"; renameBtn.textContent = "✎";
    renameBtn.title = "Rename";
    renameBtn.addEventListener("click", e => { e.preventDefault(); e.stopPropagation(); renameFile(file).catch(() => {}); });

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button"; deleteBtn.className = "e-file-act danger"; deleteBtn.textContent = "✕";
    deleteBtn.title = "Delete";
    deleteBtn.addEventListener("click", e => { e.preventDefault(); e.stopPropagation(); deleteFile(file).catch(() => {}); });

    if (!cfg.sessionReview) {
      actions.appendChild(renameBtn);
      actions.appendChild(deleteBtn);
    }
    row.appendChild(actions);
    li.appendChild(row);
    fileListEl.appendChild(li);
  }

  function renderNode(node) {
    if (node.key) {
      const file = node.file || { name: node.key, is_dir: true, is_text: false, is_image: false, type: "asset" };
      renderEntry(file, node.depth, node.name);
      if (s.collapsedFolders.has(node.key)) return;
    }
    [...node.children].sort(compareTreeNodes).forEach(renderNode);
  }

  renderEntry(mainEntry, 0, s.mainFileName);
  [...treeRoot.children].sort(compareTreeNodes).forEach(renderNode);
}

// ── Outline rendering ─────────────────────────────────────────────────────────

const outlineEl = document.getElementById("outline-list");
const OL_CLASS  = { 0: "e-ol-0", 1: "e-ol-2", 2: "e-ol-2", 3: "e-ol-3", 4: "e-ol-4", 5: "e-ol-5", 6: "e-ol-6" };

export function renderOutline(openOutlineLocation) {
  if (!outlineEl) return;
  outlineEl.innerHTML = "";
  const sourceItems = Array.isArray(s.lspOutlineItems) && s.lspOutlineItems.length
    ? s.lspOutlineItems.map(item => ({
        title: item.title,
        level: item.level,
        file_name: item.file_name,
        start_line: item.start_line,
        end_line: item.end_line,
        detail: item.detail,
      }))
    : s.sections;
  sourceItems.forEach(sec => {
    const lvl = Math.min(Number(sec.level || 1), 6);
    const li  = document.createElement("li");
    const btn = document.createElement("button");
    btn.className   = `e-outline-btn ${OL_CLASS[lvl] || "e-ol-3"}`;
    btn.textContent = sec.title;
    const detail = sec.detail ? ` · ${sec.detail}` : "";
    btn.title       = `${sec.file_name || s.mainFileName}${detail} · Рядки ${sec.start_line}–${sec.end_line || sec.start_line}`;
    btn.addEventListener("click", () => openOutlineLocation(sec.file_name || s.mainFileName, sec.start_line));
    li.appendChild(btn); outlineEl.appendChild(li);
  });
}

// ── File operations ──────────────────────────────────────────────────────────

async function promptAndImport(key, factoryFn) {
  const ui = await import("./ui.js");
  return factoryFn(ui);
}

export async function moveFileToFolder(sourcePath, targetFolderPath) {
  const newName = targetFolderPath
    ? `${targetFolderPath.replace(/\/+$/, "")}/${pathBaseName(sourcePath)}`
    : pathBaseName(sourcePath);
  if (newName === sourcePath) return;
  await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(sourcePath)}/rename/`, {
    method: "POST",
    body: JSON.stringify({ new_filename: newName }),
  });
}

export async function setMainFile(fileName) {
  const name = String(fileName || "").trim();
  if (!name || name === s.mainFileName) return;
  const prevMain = s.mainFileName;
  await api(`/api/projects/${cfg.projectId}/`, {
    method: "PATCH",
    body: JSON.stringify({ main_file: name }),
  });
  s.mainFileName = name;
  if (s.projectMeta) s.projectMeta.main_file_name = name;
  if (s.selectedFile.name === prevMain) {
    s.selectedFile = { ...s.selectedFile, type: "asset", is_text: true };
  }
  if (s.selectedFile.name === name) {
    s.selectedFile = { ...s.selectedFile, type: "main", is_text: true };
  }
  const labelEl = document.getElementById("current-file-label");
  if (labelEl) labelEl.textContent = s.selectedFile.name || s.mainFileName;
  setSaveHint(`Main file: ${name}`, "saved");
}

export async function renameFile(file) {
  const { showRenameDialog } = await import("./ui.js");
  const currentName = String(file?.name || "");
  const newName = await showRenameDialog(currentName);
  if (!newName) return;
  const trimmed = newName.trim();
  if (!trimmed || trimmed === currentName) return;
  await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(currentName)}/rename/`, {
    method: "POST",
    body: JSON.stringify({ new_filename: trimmed }),
  });
  if (s.selectedFile.name === currentName) {
    s.selectedFile = { ...file, name: trimmed, type: file.type || "asset" };
    const labelEl = document.getElementById("current-file-label");
    if (labelEl) labelEl.textContent = trimmed;
  }
  setSaveHint(`${file?.is_dir ? "Папку" : "Файл"} перейменовано: ${currentName} → ${trimmed}`, "saved");
  return trimmed;
}

export async function deleteFile(file) {
  const { showConfirm } = await import("./ui.js");
  const currentName = String(file?.name || "");
  if (!currentName) return;
  const ok = await showConfirm(`Видалити ${file?.is_dir ? "папку" : "файл"} "${currentName}"?`);
  if (!ok) return;
  await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(currentName)}/`, { method: "DELETE" });
  const wasSelected = s.selectedFile.name === currentName;
  setSaveHint(`${file?.is_dir ? "Папку" : "Файл"} видалено: ${currentName}`, "saved");
  return wasSelected;
}

export async function createFolder(parentPath = "") {
  const { showCreateEntryDialog } = await import("./ui.js");
  const basePath = String(parentPath || "").replace(/\/+$/, "");
  const folderName = await showCreateEntryDialog("directory", {
    hint: basePath ? `Нова папка в ${basePath}` : "Назва папки",
  });
  if (!folderName) return null;
  const targetPath = joinProjectPath(basePath, folderName);
  await api(`/api/projects/${cfg.projectId}/files/`, {
    method: "POST",
    body: JSON.stringify({ filename: targetPath, entry_kind: "directory" }),
  });
  setSaveHint(`Папку створено: ${targetPath}`, "saved");
  return targetPath;
}

export async function createEmptyTextFile(parentPath = "") {
  const { showCreateEntryDialog } = await import("./ui.js");
  const basePath = String(parentPath || "").replace(/\/+$/, "");
  const fileName = await showCreateEntryDialog("file", {
    hint: basePath ? `Новий файл у ${basePath} (напр. note.typ)` : "Назва файлу (напр. chapter1.typ)",
  });
  if (!fileName) return null;
  const targetPath = joinProjectPath(basePath, fileName);
  await api(`/api/projects/${cfg.projectId}/files/`, {
    method: "POST",
    body: JSON.stringify({ filename: targetPath, text_content: "" }),
  });
  setSaveHint(`Файл створено: ${targetPath}`, "saved");
  return targetPath;
}

export async function uploadFile(file, targetFolderPath = "") {
  if (!isUploadableProjectFile(file)) {
    throw new Error("Allowed uploads: images, PDFs, and supported text files");
  }
  const uploadName = joinProjectPath(String(targetFolderPath || "").replace(/\/+$/, ""), pathBaseName(file.name));
  let uploadFileObj = file;
  if (uploadName && uploadName !== file.name) {
    try {
      uploadFileObj = new File([file], uploadName, {
        type: file.type || "application/octet-stream",
        lastModified: file.lastModified || Date.now(),
      });
    } catch (_) {}
  }
  const fd = new FormData(); fd.append("file", uploadFileObj);
  await api(`/api/projects/${cfg.projectId}/files/`, { method: "POST", body: fd });
  setSaveHint(`Завантажено: ${uploadName || file.name}`, "saved");
}

export async function uploadImageWithRename(file, targetFolderPath = "") {
  const basePath = String(targetFolderPath || "").replace(/\/+$/, "");
  await uploadFile(file, basePath);
  const { showRenameDialog } = await import("./ui.js");
  const uploadedName = joinProjectPath(basePath, pathBaseName(file.name));
  const newName = await showRenameDialog(pathBaseName(uploadedName));
  if (!newName) return uploadedName;
  const trimmed = newName.trim();
  if (!trimmed || trimmed === pathBaseName(uploadedName)) return uploadedName;
  const targetName = joinProjectPath(basePath, trimmed);
  await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(uploadedName)}/rename/`, {
    method: "POST",
    body: JSON.stringify({ new_filename: targetName }),
  });
  setSaveHint(`Перейменовано: ${uploadedName} → ${targetName}`, "saved");
  return targetName;
}

export async function uploadZip(file) {
  const fd = new FormData(); fd.append("file", file);
  const result = await api(`/api/projects/${cfg.projectId}/files/`, { method: "POST", body: fd });
  const n = (result.files || []).length;
  const label = n === 1 ? "файл" : n < 5 ? "файли" : "файлів";
  setSaveHint(`ZIP: розпаковано ${n} ${label}`, "saved");
}

export function normalizeClipboardFile(file, idx = 0) {
  if (file.name && file.name.trim()) return file;
  const mime = String(file.type || "").toLowerCase();
  const extMap = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "image/svg+xml": "svg" };
  const ext  = extMap[mime] || "bin";
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
  const name  = `clipboard-${stamp}-${idx + 1}.${ext}`;
  try { return new File([file], name, { type: file.type || "application/octet-stream", lastModified: Date.now() }); }
  catch (_) { return file; }
}

// ── Asset viewer helper ──────────────────────────────────────────────────────

export function showEditorForText() {
  editorWrapEl.style.display = "";
  assetView.style.display    = "none";
  refreshLayout();
}

export function showEmptyEditor() {
  editorWrapEl.style.display = "none";
  assetView.style.display    = "flex";
  assetBox.innerHTML = `<div class="e-empty-card"><strong>No file open</strong>Open a file from the panel on the left.</div>`;
}

export function showAssetViewer(file) {
  editorWrapEl.style.display = "none";
  assetView.style.display    = "flex";
  if (file.is_dir) {
    assetBox.innerHTML = `<div class="e-empty-card"><strong>${escHtml(file.name)}</strong>Folder — place chapters, images, or support files here.</div>`;
    return;
  }
  const url = `/api/projects/${cfg.projectId}/files/${encodeURIComponent(file.name)}/`;
  if (file.is_image) {
    assetBox.innerHTML = `<img class="e-asset-img" src="${url}?t=${Date.now()}" alt="${escHtml(file.name)}">`;
    return;
  }
  assetBox.innerHTML = `<div class="e-empty-card"><strong>${escHtml(file.name)}</strong>Binary file. <a class="e-btn" href="${url}" target="_blank" style="margin-top:10px;">Open or download</a></div>`;
}

export function refreshOpenAsset() {
  const f = s.selectedFile;
  if (!f || f.is_text || f.is_dir || !f.name) return;
  if (assetView.style.display === "none") return;
  showAssetViewer(f);
}
