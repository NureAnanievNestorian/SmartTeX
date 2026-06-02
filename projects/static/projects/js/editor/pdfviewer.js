import * as pdfjsLib from "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs";
import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as cm from "./cm.js";
import * as tinymist from "./tinymist.js";

const { s, cfg } = state;
const { api } = apiMod;
const { jumpToLine } = cm;

pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.worker.min.mjs";

const pdfCanvasContainer = document.getElementById("pdf-canvas-container");
const pdfEmpty           = document.getElementById("pdf-empty");
const pdfLoadingEl       = document.getElementById("pdf-loading");
const pdfPageInfo        = document.getElementById("pdf-page-info");
const previewKindbarEl   = document.getElementById("preview-kindbar");
const previewKindWebBtn  = document.getElementById("preview-kind-web");
const previewKindPdfBtn  = document.getElementById("preview-kind-pdf");
const previewThemeAutoBtn = document.getElementById("preview-theme-auto");
const previewThemeLightBtn = document.getElementById("preview-theme-light");
const previewThemeDarkBtn = document.getElementById("preview-theme-dark");
const previewSyncFollowBtn = document.getElementById("preview-sync-follow");
const previewSyncClickBtn = document.getElementById("preview-sync-click");
const previewSyncRevealBtn = document.getElementById("preview-sync-reveal");
const typstPreviewWrapEl = document.getElementById("typst-preview-wrap");
const typstPreviewFrameEl= document.getElementById("typst-preview-frame");
const openPdfLink        = document.getElementById("open-pdf");
const refreshPdfBtn      = document.getElementById("refresh-pdf");
let _previewUiInitialized = false;
let _previewBridgeReady = false;
let _previewRevealTimer = null;
let _previewCodeNavigationCallback = null;
let _previewControlWs = null;
let _previewControlReconnectTimer = null;
let _previewLastOfficialJumpAt = 0;
let _previewLastRevealKey = "";
let _previewMemorySyncTimer = null;
let _previewUserInteractingUntil = 0;
let _previewStatusEl = null;
const PREVIEW_THEME_KEY = "smarttex.typst.previewTheme";
const PREVIEW_FOLLOW_KEY = "smarttex.typst.previewFollowCursor";
const PREVIEW_CLICK_KEY = "smarttex.typst.previewClickSync";
const PREVIEW_DEBUG_KEY = "smarttex.preview.debug";

export { pdfEmpty };
export function setPreviewCodeNavigationCallback(fn) { _previewCodeNavigationCallback = fn; }

function ensurePreviewStatusEl() {
  if (_previewStatusEl || !typstPreviewWrapEl) return _previewStatusEl;
  const el = document.createElement("div");
  el.id = "typst-preview-status";
  Object.assign(el.style, {
    position: "absolute",
    inset: "16px",
    display: "none",
    alignItems: "center",
    justifyContent: "center",
    textAlign: "center",
    padding: "18px 20px",
    borderRadius: "14px",
    background: "rgba(18,18,22,.92)",
    color: "#f3f4f6",
    zIndex: "5",
    pointerEvents: "none",
    boxShadow: "0 12px 30px rgba(0,0,0,.24)",
    backdropFilter: "blur(8px)",
    lineHeight: "1.45",
    fontSize: "14px",
    whiteSpace: "pre-wrap",
  });
  typstPreviewWrapEl.appendChild(el);
  _previewStatusEl = el;
  return el;
}

function setPreviewStatus(message = "") {
  const el = ensurePreviewStatusEl();
  if (!el) return;
  const text = String(message || "").trim();
  if (!text) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.textContent = text;
  el.style.display = "flex";
}

function blockingDiagnostics() {
  return Array.isArray(s.diagnostics)
    ? s.diagnostics.filter(item => String(item?.severity || "error").toLowerCase() !== "warning")
    : [];
}

export function refreshTypstPreviewStatus() {
  if (!typstPreviewEnabled() || s.previewMode !== "web") {
    setPreviewStatus("");
    return;
  }
  const blocking = blockingDiagnostics();
  if (s.compileState === "failed" && blocking.length) {
    const first = blocking[0];
    const where = first?.line ? `Рядок ${first.line}` : "";
    const message = [where, first?.message || "Помилка компіляції"].filter(Boolean).join(": ");
    setPreviewStatus(`Помилка компіляції\n${message}`);
    return;
  }
  setPreviewStatus("");
}

export function markTypstPreviewEditing() {
  if (!typstPreviewEnabled() || s.previewMode !== "web") return;
  setPreviewStatus("");
}

function previewControlConnected() {
  return _previewControlWs && _previewControlWs.readyState === WebSocket.OPEN;
}

function previewDebugEnabled() {
  try {
    return localStorage.getItem(PREVIEW_DEBUG_KEY) === "1";
  } catch (_) {
    return false;
  }
}

function previewDebug(...args) {
  if (previewDebugEnabled()) console.log("[smarttex-preview]", ...args);
}

function isTypstProject() {
  return s.projectMeta?.markup_type === "typst";
}

function typstPreviewEnabled() {
  return isTypstProject() && s.projectMeta?.tinymist?.preview_enabled !== false;
}

function typstPreviewAutostart() {
  return typstPreviewEnabled() && s.projectMeta?.tinymist?.preview_autostart !== false;
}

function previewBaseUrl() {
  const themeParam = s.typstPreviewTheme === "light"
    ? "never"
    : s.typstPreviewTheme === "dark"
      ? "always"
      : "auto";
  return `/api/projects/${cfg.projectId}/typst-preview/?theme=${encodeURIComponent(themeParam)}`;
}

function previewControlWsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const themeParam = s.typstPreviewTheme === "light"
    ? "never"
    : s.typstPreviewTheme === "dark"
      ? "always"
      : "auto";
  return `${proto}//${location.host}/ws/projects/${cfg.projectId}/typst-preview/control/?preview_project=${encodeURIComponent(cfg.projectId)}&preview_theme=${encodeURIComponent(themeParam)}`;
}

function absolutePreviewFilepath(filename) {
  const rootUri = tinymist.getRootUri?.() || "";
  const relative = String(filename || "");
  if (!rootUri || !relative) return "";
  if (relative.startsWith("file://")) return relative.replace(/^file:\/\//, "");
  const base = rootUri.endsWith("/") ? rootUri : `${rootUri}/`;
  try {
    return decodeURIComponent(new URL(relative, base).pathname);
  } catch (_) {
    return `${base}${relative}`.replace(/^file:\/\//, "");
  }
}

function loadPreviewThemePreference() {
  try {
    const raw = localStorage.getItem(PREVIEW_THEME_KEY);
    if (raw === "auto" || raw === "light" || raw === "dark") {
      s.typstPreviewTheme = raw;
      return;
    }
  } catch (_) {}
  s.typstPreviewTheme = "auto";
}

function persistPreviewThemePreference() {
  try {
    localStorage.setItem(PREVIEW_THEME_KEY, s.typstPreviewTheme || "auto");
  } catch (_) {}
}

function loadPreviewSyncPreferences() {
  try {
    const follow = localStorage.getItem(PREVIEW_FOLLOW_KEY);
    const click = localStorage.getItem(PREVIEW_CLICK_KEY);
    s.typstPreviewFollowCursor = follow == null ? true : follow === "1";
    s.typstPreviewClickSync = click == null ? true : click === "1";
  } catch (_) {
    s.typstPreviewFollowCursor = true;
    s.typstPreviewClickSync = true;
  }
}

function persistPreviewSyncPreferences() {
  try {
    localStorage.setItem(PREVIEW_FOLLOW_KEY, s.typstPreviewFollowCursor ? "1" : "0");
    localStorage.setItem(PREVIEW_CLICK_KEY, s.typstPreviewClickSync ? "1" : "0");
  } catch (_) {}
}

function applyPreviewThemeUi() {
  const theme = s.typstPreviewTheme || "auto";
  previewThemeAutoBtn?.classList.toggle("active", theme === "auto");
  previewThemeLightBtn?.classList.toggle("active", theme === "light");
  previewThemeDarkBtn?.classList.toggle("active", theme === "dark");
}

function applyPreviewSyncUi() {
  previewSyncFollowBtn?.classList.toggle("active", Boolean(s.typstPreviewFollowCursor));
  previewSyncClickBtn?.classList.toggle("active", Boolean(s.typstPreviewClickSync));
}

function applyPreviewModeUi() {
  const mode = s.previewMode || "pdf";
  const isTypst = isTypstProject();
  const previewEnabled = typstPreviewEnabled();
  previewKindbarEl?.classList.toggle("visible", isTypst);
  previewKindWebBtn?.classList.toggle("active", previewEnabled && mode === "web");
  previewKindPdfBtn?.classList.toggle("active", mode === "pdf");
  if (previewKindWebBtn) previewKindWebBtn.style.display = previewEnabled ? "" : "none";
  if (previewKindPdfBtn) previewKindPdfBtn.style.display = isTypst && previewEnabled ? "none" : "";
  typstPreviewWrapEl?.classList.toggle("visible", previewEnabled && mode === "web");
  if (pdfCanvasContainer) pdfCanvasContainer.style.display = previewEnabled && mode === "web" ? "none" : "flex";
  if (pdfEmpty) pdfEmpty.style.display = previewEnabled && mode === "web" ? "none" : pdfEmpty.style.display;
  if (pdfLoadingEl) pdfLoadingEl.style.display = previewEnabled && mode === "web" ? "none" : pdfLoadingEl.style.display;
  if (pdfPageInfo) pdfPageInfo.textContent = previewEnabled && mode === "web" ? "Web preview" : pdfPageInfo.textContent;
  if (openPdfLink) {
    openPdfLink.href = previewEnabled && mode === "web"
      ? previewBaseUrl()
      : (s.pdfCurrentUrl ? s.pdfCurrentUrl.split("?")[0] : openPdfLink.href);
    openPdfLink.title = previewEnabled && mode === "web" ? "Відкрити Web Preview" : "Відкрити PDF";
  }
  if (refreshPdfBtn) {
    refreshPdfBtn.title = previewEnabled && mode === "web" ? "Оновити Web Preview" : "Оновити PDF";
  }
  applyPreviewThemeUi();
  applyPreviewSyncUi();
  refreshTypstPreviewStatus();
}

function markPreviewUserInteraction(ms = 2500) {
  _previewUserInteractingUntil = Date.now() + ms;
}

function previewUserInteractionActive() {
  return Date.now() < _previewUserInteractingUntil;
}

function schedulePreviewControlReconnect() {
  clearTimeout(_previewControlReconnectTimer);
  _previewControlReconnectTimer = setTimeout(() => {
    if (typstPreviewEnabled() && s.previewMode === "web") connectPreviewControl();
  }, 1200);
}

function disconnectPreviewControl() {
  clearTimeout(_previewControlReconnectTimer);
  _previewControlReconnectTimer = null;
  if (_previewControlWs) {
    try {
      _previewControlWs.onopen = null;
      _previewControlWs.onclose = null;
      _previewControlWs.onmessage = null;
      _previewControlWs.onerror = null;
      _previewControlWs.close();
    } catch (_) {}
    _previewControlWs = null;
  }
}

function connectPreviewControl() {
  if (!typstPreviewEnabled() || s.previewMode !== "web" || !cfg.projectId) return;
  if (_previewControlWs && (_previewControlWs.readyState === WebSocket.CONNECTING || _previewControlWs.readyState === WebSocket.OPEN)) {
    return;
  }
  const ws = new WebSocket(previewControlWsUrl());
  _previewControlWs = ws;
  previewDebug("control connect", { url: ws.url });
  ws.onopen = () => {
    previewDebug("control open");
    if (s.typstPreviewFollowCursor) revealPreviewSelection(true);
  };
  ws.onmessage = ev => {
    let data = null;
    try {
      data = JSON.parse(ev.data);
    } catch (_) {
      previewDebug("control raw message", ev.data);
      return;
    }
    previewDebug("control message", data);
    handlePreviewControlMessage(data);
  };
  ws.onerror = event => {
    previewDebug("control error", event);
  };
  ws.onclose = () => {
    previewDebug("control close");
    if (_previewControlWs === ws) _previewControlWs = null;
    schedulePreviewControlReconnect();
  };
}

function sendPreviewControlEvent(payload) {
  if (!previewControlConnected()) return false;
  try {
    _previewControlWs.send(JSON.stringify(payload));
    previewDebug("control send", payload);
    return true;
  } catch (_) {
    return false;
  }
}

function sendPreviewMemoryEvent(eventName = "updateMemoryFiles", filename = "", content = "") {
  const absolute = absolutePreviewFilepath(filename);
  if (!absolute || !String(filename || "").toLowerCase().endsWith(".typ")) return false;
  return sendPreviewControlEvent({
    event: eventName,
    files: {
      [absolute]: String(content || ""),
    },
  });
}

function collectPreviewMemoryFiles() {
  const files = {};
  const seen = new Set();
  const addFile = (filename, content) => {
    const name = String(filename || "");
    if (!name || seen.has(name) || !name.toLowerCase().endsWith(".typ")) return;
    const absolute = absolutePreviewFilepath(name);
    if (!absolute) return;
    files[absolute] = String(content || "");
    seen.add(name);
  };

  if (s.activeTabName && cm.view) addFile(s.activeTabName, cm.getContent());
  for (const tab of s.openTabs || []) {
    const name = String(tab?.name || "");
    if (!name || seen.has(name) || !name.toLowerCase().endsWith(".typ")) continue;
    const cached = cm.getTabStateContent?.(name);
    if (typeof cached === "string") addFile(name, cached);
  }
  return files;
}

function schedulePreviewMemorySync() {
  clearTimeout(_previewMemorySyncTimer);
  _previewMemorySyncTimer = setTimeout(() => {
    const files = collectPreviewMemoryFiles();
    if (!Object.keys(files).length) return;
    sendPreviewControlEvent({
      event: "syncMemoryFiles",
      files,
    });
  }, 140);
}

function normalizePreviewControlLocation(payload = {}) {
  const filepath = normalizeSourceFilename(payload.filepath || payload.path || payload.filename || "");
  const start = payload.start;
  const line = Array.isArray(start)
    ? Number(start[0] ?? 0) + 1
    : Number(start?.row ?? start?.line ?? payload.line ?? 0) + 1;
  const column = Array.isArray(start)
    ? Number(start[1] ?? 0) + 1
    : Number(start?.column ?? start?.character ?? payload.character ?? 0) + 1;
  return {
    filename: filepath,
    line,
    column,
  };
}

function handlePreviewControlMessage(data) {
  const event = String(data?.event || "");
  if (event === "editorScrollTo") {
    if (!s.typstPreviewClickSync) return;
    const location = normalizePreviewControlLocation(data);
    previewDebug("control editorScrollTo", location);
    if (!location.filename || location.line <= 0) return;
    _previewLastOfficialJumpAt = Date.now();
    if (_previewCodeNavigationCallback) {
      _previewCodeNavigationCallback(location.filename, location.line, location.column).catch(() => {});
    } else if (location.filename === s.activeTabName) {
      jumpToLine(location.line, location.column);
    }
    return;
  }
  if (event === "syncEditorChanges") {
    const files = collectPreviewMemoryFiles();
    if (Object.keys(files).length) {
      sendPreviewControlEvent({
        event: "syncMemoryFiles",
        files,
      });
    }
    return;
  }
}

export function initPreviewPanel() {
  loadPreviewThemePreference();
  loadPreviewSyncPreferences();
  if (!isTypstProject()) {
    s.previewMode = "pdf";
  } else if (!typstPreviewAutostart()) {
    s.previewMode = "pdf";
  } else {
    s.previewMode = "web";
  }
  if (!_previewUiInitialized) {
    previewKindWebBtn?.addEventListener("click", () => setPreviewMode("web"));
    previewKindPdfBtn?.addEventListener("click", () => setPreviewMode("pdf"));
    previewThemeAutoBtn?.addEventListener("click", () => setPreviewTheme("auto"));
    previewThemeLightBtn?.addEventListener("click", () => setPreviewTheme("light"));
    previewThemeDarkBtn?.addEventListener("click", () => setPreviewTheme("dark"));
    previewSyncFollowBtn?.addEventListener("click", () => togglePreviewFollowCursor());
    previewSyncClickBtn?.addEventListener("click", () => togglePreviewClickSync());
    previewSyncRevealBtn?.addEventListener("click", () => revealPreviewSelection(true));
    typstPreviewWrapEl?.addEventListener("wheel", () => markPreviewUserInteraction(), { passive: true });
    typstPreviewWrapEl?.addEventListener("pointerdown", () => markPreviewUserInteraction(4000), { passive: true });
    typstPreviewFrameEl?.addEventListener("load", () => {
      _previewBridgeReady = false;
      setTimeout(() => revealPreviewSelection(true), 220);
    });
    window.addEventListener("message", onPreviewFrameMessage);
    _previewUiInitialized = true;
  }
  applyPreviewModeUi();
  if (typstPreviewEnabled() && s.previewMode === "web") {
    connectPreviewControl();
    if (!typstPreviewFrameEl?.src) refreshTypstPreview(true);
  } else {
    disconnectPreviewControl();
  }
}

export function getPreviewMode() {
  return s.previewMode || "pdf";
}

export function setPreviewMode(mode) {
  s.previewMode = typstPreviewEnabled() && mode === "web" ? "web" : "pdf";
  _previewLastRevealKey = "";
  applyPreviewModeUi();
  if (s.previewMode === "web") {
    connectPreviewControl();
    refreshTypstPreview(true);
  } else if (s.pdfCurrentUrl) {
    disconnectPreviewControl();
    loadPdfViewer(s.pdfCurrentUrl).catch(() => {});
  }
}

export function setPreviewTheme(theme) {
  const next = theme === "light" || theme === "dark" ? theme : "auto";
  if (s.typstPreviewTheme === next) return;
  s.typstPreviewTheme = next;
  persistPreviewThemePreference();
  applyPreviewModeUi();
  if (typstPreviewEnabled() && s.previewMode === "web") {
    refreshTypstPreview(true);
  }
}

export function togglePreviewFollowCursor() {
  s.typstPreviewFollowCursor = !s.typstPreviewFollowCursor;
  persistPreviewSyncPreferences();
  applyPreviewModeUi();
  if (s.typstPreviewFollowCursor) revealPreviewSelection(true);
}

export function togglePreviewClickSync() {
  s.typstPreviewClickSync = !s.typstPreviewClickSync;
  persistPreviewSyncPreferences();
  applyPreviewModeUi();
}

export async function refreshTypstPreview(force = false) {
  if (!typstPreviewEnabled()) return;
  _previewBridgeReady = false;
  if (force) _previewLastRevealKey = "";
  const base = previewBaseUrl();
  const nextUrl = `${base}${base.includes("?") ? "&" : "?"}t=${force ? Date.now() : (s.lastPdfVersion || Date.now())}`;
  s.typstPreviewUrl = nextUrl;
  if (typstPreviewFrameEl && typstPreviewFrameEl.src !== nextUrl) {
    typstPreviewFrameEl.src = nextUrl;
  } else if (typstPreviewFrameEl && force) {
    typstPreviewFrameEl.src = nextUrl;
  }
  applyPreviewModeUi();
}

function normalizePreviewText(text) {
  return String(text || "")
    .replace(/\s+/g, " ")
    .replace(/[“”«»"]/g, "\"")
    .replace(/[’']/g, "'")
    .trim();
}

function isLikelyTypstHeading(text) {
  return /^\s*=+\s+\S/.test(String(text || ""));
}

function isLikelyTypstLabel(text) {
  return /^\s*<[^>]+>\s*$/.test(String(text || ""));
}

function currentPreviewRevealPayload() {
  if (!cm.view || !String(s.activeTabName || s.selectedFile?.name || "").toLowerCase().endsWith(".typ")) return null;
  const doc = cm.view.state.doc;
  const pos = cm.view.state.selection.main.head;
  const line = doc.lineAt(pos);
  const lineNumber = line.number;
  const columnNumber = Math.max(1, pos - line.from + 1);
  const beforeText = doc.sliceString(0, line.to);
  const headings = [...beforeText.matchAll(/^\s*(=+)\s+(.+?)\s*$/gm)];
  const heading = normalizePreviewText(headings.at(-1)?.[2] || "");
  const excerptLines = [];
  for (let n = Math.max(1, lineNumber - 1); n <= Math.min(doc.lines, lineNumber + 1); n++) {
    excerptLines.push(doc.line(n).text);
  }
  const excerpt = normalizePreviewText(excerptLines.join(" "));
  return {
    filename: s.activeTabName || s.selectedFile?.name || "",
    lineNumber,
    columnNumber,
    cursorLineNumber: lineNumber,
    cursorColumnNumber: columnNumber,
    heading,
    lineText: normalizePreviewText(line.text),
    anchorText: normalizePreviewText(line.text),
    excerpt,
  };
}

function postToPreview(message) {
  if (!typstPreviewFrameEl?.contentWindow) return false;
  try {
    const targetOrigin = new URL(typstPreviewFrameEl.src || window.location.href, window.location.href).origin;
    typstPreviewFrameEl.contentWindow.postMessage(message, targetOrigin);
    return true;
  } catch (_) {
    return false;
  }
}

export function revealPreviewSelection(force = false) {
  if (!typstPreviewEnabled() || s.previewMode !== "web") return;
  if (!force && previewUserInteractionActive()) return;
  clearTimeout(_previewRevealTimer);
  _previewRevealTimer = setTimeout(() => {
    const payload = currentPreviewRevealPayload();
    if (!payload) return;
    const revealKey = `${payload.filename}:${payload.lineNumber}:${payload.columnNumber}`;
    if (!force && revealKey === _previewLastRevealKey) return;
    _previewLastRevealKey = revealKey;
    previewDebug("reveal payload", payload);
    const absolute = absolutePreviewFilepath(payload.filename);
    const didSendCursor = absolute
      ? sendPreviewControlEvent({
        event: "changeCursorPosition",
        filepath: absolute,
        line: Math.max(0, Number(payload.lineNumber || 1) - 1),
        character: Math.max(0, Number(payload.columnNumber || 1) - 1),
      })
      : false;
    const shouldScroll = force || s.typstPreviewFollowCursor;
    const didSendOfficialScroll = absolute && shouldScroll
      ? sendPreviewControlEvent({
        event: "panelScrollTo",
        filepath: absolute,
        line: Math.max(0, Number(payload.lineNumber || 1) - 1),
        character: Math.max(0, Number(payload.columnNumber || 1) - 1),
      })
      : false;
    if (shouldScroll) {
      postToPreview({ type: "smarttex-preview-reveal", payload });
    }
  }, force ? 40 : 260);
}

export function syncPreviewMemoryFile(filename, content) {
  if (!typstPreviewEnabled() || s.previewMode !== "web") return;
  markTypstPreviewEditing();
  if (!previewControlConnected()) return;
  sendPreviewMemoryEvent("updateMemoryFiles", filename, content);
  schedulePreviewMemorySync();
}

export function resyncTypstPreview({ reveal = false, revealDelay = 120 } = {}) {
  if (!typstPreviewEnabled() || s.previewMode !== "web") return;
  if (!typstPreviewFrameEl?.src) {
    refreshTypstPreview(true).catch(() => {});
    return;
  }
  schedulePreviewMemorySync();
  if (reveal) {
    clearTimeout(_previewRevealTimer);
    _previewRevealTimer = setTimeout(() => revealPreviewSelection(false), revealDelay);
  }
}

function scoreTextMatch(lineText, target) {
  const hay = normalizePreviewText(lineText).toLowerCase();
  if (!hay || !target) return 0;
  if (hay === target) return 180;
  if (hay.includes(target)) return 130 - Math.min(40, hay.length - target.length);
  if (target.includes(hay) && hay.length >= 8) return 90;
  return 0;
}

async function fetchTypstFileText(filename) {
  const target = String(filename || "");
  if (!target) return "";
  if (target === s.activeTabName && cm.view) return cm.getContent();
  const cached = cm.getTabStateContent?.(target);
  if (typeof cached === "string") return cached;
  if (target === s.mainFileName) {
    const payload = await api(`/api/projects/${cfg.projectId}/file/`, { method: "GET" });
    return String(payload.content || "");
  }
  const payload = await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(target)}/content/?include_text=1`);
  return String(payload.text_content || "");
}

async function locateTextInProject(rawText, headingText = "") {
  const target = normalizePreviewText(rawText).toLowerCase();
  const heading = normalizePreviewText(headingText).toLowerCase();
  if (!target) return null;
  const candidates = [];
  const typstFiles = [
    s.mainFileName,
    ...s.projectFiles
      .filter(file => file?.is_text && !file?.is_dir && String(file.name || "").toLowerCase().endsWith(".typ"))
      .map(file => file.name),
  ].filter((name, index, list) => name && list.indexOf(name) === index);

  for (const filename of typstFiles) {
    let text = "";
    try {
      text = await fetchTypstFileText(filename);
    } catch (_) {
      continue;
    }
    if (!text) continue;
    const lines = text.split("\n");
    let headingBoost = 0;
    if (heading) {
      for (let i = 0; i < lines.length; i++) {
        const headingLine = normalizePreviewText(lines[i]).toLowerCase();
        if (headingLine === heading || headingLine.includes(heading)) {
          headingBoost = 35;
          break;
        }
      }
    }
    for (let i = 0; i < lines.length; i++) {
      const score = scoreTextMatch(lines[i], target);
      if (!score) continue;
      candidates.push({
        filename,
        line: i + 1,
        score: score + headingBoost + (filename === s.activeTabName ? 20 : 0) + (filename === s.mainFileName ? 8 : 0),
      });
    }
  }
  candidates.sort((a, b) => b.score - a.score || a.line - b.line);
  return candidates[0] || null;
}

function normalizeSourceFilename(value) {
  const raw = String(value || "").replace(/^file:\/\//, "");
  if (!raw) return "";
  const names = [
    s.mainFileName,
    ...s.projectFiles.map(file => file.name),
  ].filter(Boolean);
  const hit = names.find(name => raw.endsWith(name));
  return hit || raw;
}

function onPreviewFrameMessage(event) {
  if (event.origin !== window.location.origin) return;
  const data = event.data || {};
  if (data.type === "smarttex-preview-ready") {
    _previewBridgeReady = true;
    previewDebug("bridge ready");
    revealPreviewSelection(true);
    return;
  }
  if (data.type === "smarttex-preview-click") {
    if (!s.typstPreviewClickSync || s.previewMode !== "web") return;
    previewDebug("click payload", data.payload);
    const hasDirectLocation = Boolean(data.payload?.location?.filename) && Number(data.payload?.location?.line || 0) > 0;
    const clickText = String(data.payload?.text || "").trim();
    if (!hasDirectLocation && clickText.length < 3) {
      previewDebug("click ignored: no usable text/location");
      return;
    }
    const startedAt = Date.now();
    const runFallback = () => {
      const location = data.payload?.location;
      if (location?.filename && Number(location?.line || 0) > 0) {
        const target = normalizeSourceFilename(location.filename);
        previewDebug("click direct location", { raw: location, normalized: target });
        if (_previewCodeNavigationCallback) {
          _previewCodeNavigationCallback(target, Number(location.line || 1), Number(location.column || 1)).catch(() => {});
          return;
        }
      }
      locateTextInProject(clickText, data.payload?.heading || "").then(hit => {
        previewDebug("click fallback match", hit);
        if (!hit) return;
        if (_previewCodeNavigationCallback) {
          _previewCodeNavigationCallback(hit.filename, hit.line, 1).catch(() => {});
          return;
        }
        if (hit.filename === s.activeTabName) jumpToLine(hit.line);
      }).catch(() => {});
    };
    if (previewControlConnected()) {
      setTimeout(() => {
        if (_previewLastOfficialJumpAt > startedAt) return;
        previewDebug("click fallback after no official jump");
        runFallback();
      }, 180);
      return;
    }
    runFallback();
  }
}

export async function renderPdfPages(sizeOnly = false) {
  if (!s.pdfDoc || s.pdfRendering) return;
  s.pdfRendering = true;
  const dpr = window.devicePixelRatio || 1;
  const containerW = pdfCanvasContainer.clientWidth - 16;
  const savedScroll = pdfCanvasContainer.scrollTop;
  s.pdfViewports = [];

  if (sizeOnly) {
    for (let i = 1; i <= s.pdfDoc.numPages; i++) {
      const page = await s.pdfDoc.getPage(i);
      const base = page.getViewport({ scale: 1 });
      const scale = Math.max(0.5, containerW / base.width);
      const vp   = page.getViewport({ scale });
      const vpHD = page.getViewport({ scale: scale * dpr });
      s.pdfViewports.push(vp);
      const wrap = pdfCanvasContainer.children[i - 1];
      if (!wrap) break;
      const canvas = wrap.querySelector("canvas");
      if (!canvas) break;
      wrap.dataset.scale = scale;
      wrap.dataset.baseH = base.height;
      canvas.width  = Math.round(vpHD.width);
      canvas.height = Math.round(vpHD.height);
      canvas.style.width  = Math.round(vp.width)  + "px";
      canvas.style.height = Math.round(vp.height) + "px";
      wrap.style.width  = Math.round(vp.width)  + "px";
      wrap.style.height = Math.round(vp.height) + "px";
      await page.render({ canvasContext: canvas.getContext("2d"), viewport: vpHD }).promise;
    }
    s.pdfRendering = false;
    return;
  }

  const fragment = document.createDocumentFragment();
  for (let i = 1; i <= s.pdfDoc.numPages; i++) {
    const page = await s.pdfDoc.getPage(i);
    const base = page.getViewport({ scale: 1 });
    const scale = Math.max(0.5, containerW / base.width);
    const vp   = page.getViewport({ scale });
    const vpHD = page.getViewport({ scale: scale * dpr });
    s.pdfViewports.push(vp);

    const wrap = document.createElement("div");
    wrap.className = "pdf-page-wrap";
    wrap.dataset.page = i;
    wrap.dataset.scale = scale;
    wrap.dataset.baseH = base.height;
    wrap.style.width  = Math.round(vp.width)  + "px";
    wrap.style.height = Math.round(vp.height) + "px";

    const canvas = document.createElement("canvas");
    canvas.width  = Math.round(vpHD.width);
    canvas.height = Math.round(vpHD.height);
    canvas.style.width  = Math.round(vp.width)  + "px";
    canvas.style.height = Math.round(vp.height) + "px";
    wrap.appendChild(canvas);

    canvas.addEventListener("click", async (e) => {
      if (!s.supportsSynctex) return;
      const curScale = parseFloat(wrap.dataset.scale);
      const curBaseH = parseFloat(wrap.dataset.baseH);
      const rect = canvas.getBoundingClientRect();
      const pdfX = (e.clientX - rect.left) / curScale;
      const pdfY = curBaseH - (e.clientY - rect.top) / curScale;
      try {
        const r = await api(`/api/projects/${cfg.projectId}/synctex/pdf/?page=${i}&x=${pdfX.toFixed(4)}&y=${pdfY.toFixed(4)}`);
        if (r.line) {
          jumpToLine(r.line);
          const marker = document.createElement("div");
          marker.className = "pdf-synctex-marker";
          marker.style.left = (e.clientX - rect.left) + "px";
          marker.style.top  = (e.clientY - rect.top) + "px";
          wrap.appendChild(marker);
          setTimeout(() => marker.remove(), 1500);
        }
      } catch (_) {}
    });

    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vpHD }).promise;
    fragment.appendChild(wrap);
  }

  pdfCanvasContainer.replaceChildren(fragment);
  pdfCanvasContainer.scrollTop = savedScroll;
  s.pdfRendering = false;
}

export async function loadPdfViewer(url) {
  const savedScroll = pdfCanvasContainer.scrollTop;
  const isFirstLoad = s.pdfDoc === null;
  s.pdfCurrentUrl = url;
  if (typstPreviewEnabled() && s.previewMode === "web") {
    applyPreviewModeUi();
    return;
  }
  pdfLoadingEl.style.display = "flex";
  pdfEmpty.style.display = "none";
  try {
    const loadingTask = pdfjsLib.getDocument(url);
    s.pdfDoc = await loadingTask.promise;
    pdfLoadingEl.style.display = "none";
    pdfPageInfo.textContent = `${s.pdfDoc.numPages} стор.`;
    s.pdfRendering = false;
    await renderPdfPages();
    if (!isFirstLoad) pdfCanvasContainer.scrollTop = savedScroll;
  } catch (_) {
    pdfLoadingEl.style.display = "none";
    pdfEmpty.style.display = "flex";
  }
}

// Update page indicator on scroll
pdfCanvasContainer.addEventListener("scroll", () => {
  if (!s.pdfDoc) return;
  const scrollY = pdfCanvasContainer.scrollTop + pdfCanvasContainer.clientHeight / 2;
  let cumH = 0;
  for (let i = 0; i < s.pdfViewports.length; i++) {
    cumH += (s.pdfViewports[i]?.height || 0) + 12;
    if (scrollY <= cumH) {
      pdfPageInfo.textContent = `${i + 1} / ${s.pdfDoc.numPages}`;
      break;
    }
  }
});

// Re-render on resize
const pdfResizeObserver = new ResizeObserver(() => {
  if (s.pdfDoc && !s.pdfRendering) renderPdfPages(true);
});
pdfResizeObserver.observe(pdfCanvasContainer);
