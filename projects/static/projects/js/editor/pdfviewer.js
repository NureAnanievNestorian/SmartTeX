import * as pdfjsLib from "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs";
import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as cm from "./cm.js";
import * as tinymist from "./tinymist.js";
import * as localRuntime from "./local_runtime.js";
import * as ui from "./ui.js";
import * as longdoc from "./longdoc.js";

const { s, cfg } = state;
const { api } = apiMod;
const { jumpToLine } = cm;
const { escHtml, showAnnotationPopover } = ui;

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
let _previewAnnotationResolverEl = null;
let _localPreviewRootUri = "";
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
  if (!isTypstProject()) return false;
  return s.projectMeta?.tinymist?.preview_enabled !== false || localRuntime.hasLocalRuntimeCapability("typst-preview");
}

function currentPreviewThemeParam() {
  return s.typstPreviewTheme === "light"
    ? "never"
    : s.typstPreviewTheme === "dark"
      ? "always"
      : "auto";
}

function previewBaseUrl() {
  if (localRuntime.isLocalRuntimeActive()) {
    const local = localRuntime.localRuntimeConfig();
    const params = new URLSearchParams({
      project_id: String(cfg.projectId || ""),
      secret: local.secret || "",
      theme: currentPreviewThemeParam(),
    });
    return `${local.url}/v1/preview/?${params.toString()}`;
  }
  return `/api/projects/${cfg.projectId}/typst-preview/?theme=${encodeURIComponent(currentPreviewThemeParam())}`;
}

function previewControlWsUrl() {
  if (localRuntime.isLocalRuntimeActive()) {
    const local = localRuntime.localRuntimeConfig();
    const base = new URL(local.url || "http://127.0.0.1:8765");
    base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    base.pathname = "/ws/typst-preview/control/";
    base.search = "";
    base.searchParams.set("project_id", String(cfg.projectId || ""));
    base.searchParams.set("secret", local.secret || "");
    base.searchParams.set("theme", currentPreviewThemeParam());
    return base.toString();
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws/projects/${cfg.projectId}/typst-preview/control/?preview_project=${encodeURIComponent(cfg.projectId)}&preview_theme=${encodeURIComponent(currentPreviewThemeParam())}`;
}

function absolutePreviewFilepath(filename) {
  const rootUri = localRuntime.isLocalRuntimeActive()
    ? _localPreviewRootUri
    : (tinymist.getRootUri?.() || "");
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
  const previewEnabled = typstPreviewEnabled();
  const webModeActive = previewEnabled && mode === "web";
  previewKindbarEl?.classList.toggle("visible", previewEnabled);
  previewKindWebBtn?.classList.toggle("active", previewEnabled && mode === "web");
  previewKindPdfBtn?.classList.toggle("active", false);
  if (previewKindWebBtn) previewKindWebBtn.style.display = previewEnabled ? "" : "none";
  if (previewKindPdfBtn) previewKindPdfBtn.style.display = "none";
  if (previewSyncFollowBtn?.parentElement) previewSyncFollowBtn.parentElement.style.display = webModeActive ? "" : "none";
  if (previewThemeAutoBtn?.parentElement) previewThemeAutoBtn.parentElement.style.display = webModeActive ? "" : "none";
  typstPreviewWrapEl?.classList.toggle("visible", webModeActive);
  if (pdfCanvasContainer) pdfCanvasContainer.style.display = webModeActive ? "none" : "flex";
  if (pdfEmpty) pdfEmpty.style.display = webModeActive ? "none" : pdfEmpty.style.display;
  if (pdfLoadingEl) pdfLoadingEl.style.display = webModeActive ? "none" : pdfLoadingEl.style.display;
  if (pdfPageInfo) pdfPageInfo.textContent = webModeActive ? "Web preview" : pdfPageInfo.textContent;
  if (openPdfLink) {
    openPdfLink.href = webModeActive
      ? previewBaseUrl()
      : (s.pdfCurrentUrl ? s.pdfCurrentUrl.split("?")[0] : openPdfLink.href);
    openPdfLink.title = webModeActive ? "Відкрити Web Preview" : "Відкрити PDF";
  }
  if (refreshPdfBtn) {
    refreshPdfBtn.title = webModeActive ? "Оновити Web Preview" : "Оновити PDF";
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
    if (s.typstPreviewFollowCursor && (!localRuntime.isLocalRuntimeActive() || _localPreviewRootUri)) {
      revealPreviewSelection(true);
    }
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
  if (!previewControlConnected()) {
    previewDebug("control send skipped: socket not connected", payload);
    return false;
  }
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
  s.previewMode = typstPreviewEnabled() ? "web" : "pdf";
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
      connectPreviewControl();
      setTimeout(() => revealPreviewSelection(true), 220);
    });
    window.addEventListener(localRuntime.LOCAL_RUNTIME_CHANGED_EVENT, () => {
      disconnectPreviewControl();
      applyPreviewModeUi();
      if (typstPreviewEnabled() && s.previewMode === "web") {
        refreshTypstPreview(true).catch(() => {});
      }
    });
    window.addEventListener("message", onPreviewFrameMessage);
    _previewUiInitialized = true;
  }
  applyPreviewModeUi();
  if (typstPreviewEnabled() && s.previewMode === "web") {
    if (typstPreviewFrameEl?.src) connectPreviewControl();
    else refreshTypstPreview(true);
  } else {
    disconnectPreviewControl();
  }
}

export function getPreviewMode() {
  return s.previewMode || "pdf";
}

export function setPreviewMode(mode) {
  s.previewMode = typstPreviewEnabled() ? "web" : "pdf";
  _previewLastRevealKey = "";
  applyPreviewModeUi();
  if (s.previewMode === "web") {
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
    disconnectPreviewControl();
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
  _localPreviewRootUri = "";
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

async function restartTypstPreviewSessionForProjectUpdate() {
  if (!typstPreviewEnabled() || s.previewMode !== "web") return false;
  if (localRuntime.isLocalRuntimeActive()) {
    const local = localRuntime.localRuntimeConfig();
    const params = new URLSearchParams({
      project_id: String(cfg.projectId || ""),
      theme: currentPreviewThemeParam(),
    });
    const response = await fetch(`${local.url}/v1/preview/refresh?${params.toString()}`, {
      method: "POST",
      headers: { "X-SmartTeX-Local-Secret": local.secret || "" },
    });
    if (!response.ok) throw new Error(`Local preview refresh failed: HTTP ${response.status}`);
    const payload = await response.json().catch(() => ({}));
    if (payload.root_uri) _localPreviewRootUri = String(payload.root_uri || "");
    return Boolean(payload.restarted);
  }
  await api(`/api/projects/${cfg.projectId}/typst-preview/restart/`, {
    method: "POST",
    body: JSON.stringify({ theme: currentPreviewThemeParam() }),
  });
  return true;
}

export async function refreshTypstPreviewFromProjectUpdate() {
  if (!typstPreviewEnabled() || s.previewMode !== "web") return;
  try {
    await restartTypstPreviewSessionForProjectUpdate();
  } catch (err) {
    previewDebug("preview restart after project update failed; falling back to iframe refresh", err);
  }
  await refreshTypstPreview(true);
  setTimeout(() => resyncTypstPreview({ reveal: false }), 220);
  setTimeout(() => resyncTypstPreview({ reveal: false }), 900);
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
    previewDebug("reveal payload", payload);
    const absolute = absolutePreviewFilepath(payload.filename);
    const localRootPending = localRuntime.isLocalRuntimeActive() && !_localPreviewRootUri;
    if (localRootPending) {
      previewDebug("reveal deferred: local preview root is not ready yet");
      return;
    }
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
    const didPostFallback = shouldScroll
      ? postToPreview({ type: "smarttex-preview-reveal", payload })
      : false;
    if (didSendCursor || didSendOfficialScroll || didPostFallback) {
      _previewLastRevealKey = revealKey;
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

function scorePreviewWindow(windowText, target) {
  const hay = normalizePreviewText(windowText).toLowerCase();
  if (!hay || !target) return 0;
  if (hay === target) return 220;
  if (hay.includes(target)) return 180 - Math.min(60, hay.length - target.length);
  if (target.includes(hay) && hay.length >= 16) return 115;
  const targetWords = target.split(" ").filter(word => word.length >= 4);
  if (targetWords.length >= 4) {
    const hits = targetWords.filter(word => hay.includes(word)).length;
    const ratio = hits / targetWords.length;
    if (ratio >= 0.72) return 80 + Math.round(ratio * 40);
  }
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

async function locateTextCandidatesInProject(rawText, headingText = "") {
  const target = normalizePreviewText(rawText).toLowerCase();
  const heading = normalizePreviewText(headingText).toLowerCase();
  if (!target) return [];
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
    const maxWindow = Math.min(8, Math.max(1, Math.ceil(target.length / 120) + 1));
    for (let i = 0; i < lines.length; i++) {
      let score = scoreTextMatch(lines[i], target);
      let lineEnd = i + 1;
      for (let span = 2; span <= maxWindow && i + span <= lines.length; span++) {
        const windowScore = scorePreviewWindow(lines.slice(i, i + span).join("\n"), target);
        if (windowScore > score) {
          score = windowScore;
          lineEnd = i + span;
        }
      }
      if (!score) continue;
      candidates.push({
        filename,
        line: i + 1,
        lineEnd,
        score: score + headingBoost + (filename === s.activeTabName ? 20 : 0) + (filename === s.mainFileName ? 8 : 0),
      });
    }
  }
  candidates.sort((a, b) => b.score - a.score || a.line - b.line);
  return candidates.slice(0, 8);
}

async function locateTextInProject(rawText, headingText = "") {
  const candidates = await locateTextCandidatesInProject(rawText, headingText);
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
  if (event.origin !== previewFrameOrigin()) return;
  const data = event.data || {};
  if (data.type === "smarttex-preview-ready") {
    _previewBridgeReady = true;
    if (data.rootUri) _localPreviewRootUri = String(data.rootUri || "");
    previewDebug("bridge ready");
    revealPreviewSelection(true);
    setTimeout(() => revealPreviewSelection(true), 280);
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
    return;
  }
  if (data.type === "smarttex-preview-annotation-request") {
    const requestId = data.requestId || data.payload?.requestId || "";
    const reply = (status, message = "") => {
      try {
        event.source?.postMessage({
          type: "smarttex-preview-annotation-response",
          requestId,
          status,
          message,
        }, event.origin);
      } catch (_) {}
    };
    reply("received");
    handlePreviewAnnotationRequest(data.payload || {}, event).then(created => {
      if (created === false) reply("cancelled");
      else reply("done");
    }).catch(err => {
      const message = err.message || String(err);
      reply("failed", message);
      window.alert(message);
    });
  }
}

function previewPayloadRect(payload = {}) {
  const rect = payload.rect || {};
  const frameRect = typstPreviewFrameEl?.getBoundingClientRect?.();
  if (!frameRect) return null;
  const left = frameRect.left + Number(rect.left || 0);
  const top = frameRect.top + Number(rect.top || 0);
  const right = frameRect.left + Number(rect.right || rect.left || 0);
  const bottom = frameRect.top + Number(rect.bottom || rect.top || 0);
  return {
    left,
    top,
    right,
    bottom,
    width: Math.max(1, right - left),
    height: Math.max(1, bottom - top),
  };
}

function closePreviewAnnotationResolver(result = null) {
  const resolver = _previewAnnotationResolverEl;
  if (!resolver) return;
  resolver.remove();
  _previewAnnotationResolverEl = null;
  const resolve = resolver._resolve;
  if (resolve) resolve(result);
}

function placePreviewResolver(popover, rect) {
  if (!popover) return;
  const margin = 12;
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
  const safeRect = rect || {
    left: viewportWidth / 2,
    right: viewportWidth / 2,
    top: viewportHeight / 2,
    bottom: viewportHeight / 2,
  };
  const anchorLeft = (safeRect.left + safeRect.right) / 2;
  const prefersBottom = safeRect.bottom + 16 + popover.offsetHeight <= viewportHeight - margin;
  const top = prefersBottom
    ? Math.min(safeRect.bottom + 14, viewportHeight - popover.offsetHeight - margin)
    : Math.max(safeRect.top - popover.offsetHeight - 14, margin);
  const left = Math.max(margin, Math.min(anchorLeft - popover.offsetWidth / 2, viewportWidth - popover.offsetWidth - margin));
  popover.dataset.placement = prefersBottom ? "bottom" : "top";
  popover.style.left = `${Math.round(left)}px`;
  popover.style.top = `${Math.round(top)}px`;
  popover.style.setProperty("--preview-annotation-arrow-left", `${Math.round(anchorLeft - left)}px`);
}

function choosePreviewAnnotationTarget(candidates, rect) {
  return new Promise(resolve => {
    closePreviewAnnotationResolver(null);
    const popover = document.createElement("div");
    popover.className = "preview-annotation-resolver";
    popover.innerHTML = `
      <div class="preview-annotation-resolver-card">
        <div class="preview-annotation-resolver-head">
          <div>
            <div class="preview-annotation-resolver-title">Куди привʼязати помітку?</div>
            <div class="preview-annotation-resolver-hint">У превʼю знайшлося кілька схожих місць.</div>
          </div>
          <button class="preview-annotation-resolver-close" type="button" aria-label="Закрити">×</button>
        </div>
        <div class="preview-annotation-resolver-list">
          ${candidates.map((item, index) => `
            <button class="preview-annotation-resolver-item" type="button" data-index="${index}">
              <span class="preview-annotation-resolver-path">${escHtml(item.filename)}</span>
              <span class="preview-annotation-resolver-line">${escHtml(String(item.line))}${item.lineEnd && item.lineEnd !== item.line ? `-${escHtml(String(item.lineEnd))}` : ""}</span>
            </button>
          `).join("")}
        </div>
      </div>
    `;
    popover._resolve = resolve;
    document.body.appendChild(popover);
    _previewAnnotationResolverEl = popover;
    requestAnimationFrame(() => placePreviewResolver(popover, rect));
    popover.querySelector(".preview-annotation-resolver-close")?.addEventListener("click", () => closePreviewAnnotationResolver(null));
    popover.querySelectorAll("[data-index]").forEach(button => {
      button.addEventListener("click", () => {
        const index = Number(button.getAttribute("data-index") || 0);
        closePreviewAnnotationResolver(candidates[index] || null);
      });
    });
  });
}

async function handlePreviewAnnotationRequest(payload = {}) {
  if (s.previewMode !== "web") throw new Error("Web Preview зараз не активний.");
  const selectedText = String(payload.text || "").trim();
  if (selectedText.length < 3) throw new Error("Не вдалося прочитати текст із превʼю.");
  const rect = previewPayloadRect(payload);
  const candidates = await locateTextCandidatesInProject(selectedText, payload.heading || "");
  if (!candidates.length) {
    window.alert("Не вдалося знайти цей фрагмент у файлах проєкту. Спробуйте виділити трохи більше тексту або перейти з превʼю в код кліком.");
    return false;
  }
  const topScore = Number(candidates[0]?.score || 0);
  const ambiguous = candidates
    .filter(item => Number(item.score || 0) >= topScore - 18)
    .slice(0, 5);
  const chosen = ambiguous.length > 1
    ? await choosePreviewAnnotationTarget(ambiguous, rect)
    : candidates[0];
  if (!chosen) return false;
  const target = {
    fileName: chosen.filename,
    lineStart: chosen.line,
    lineEnd: chosen.lineEnd || chosen.line,
    selectedText,
  };
  const instruction = await showAnnotationPopover({
    title: "Помітка з превʼю",
    hint: "Фрагмент знайдено у вихідному файлі. Напишіть, що треба змінити.",
    target: `${target.fileName}:${target.lineStart}${target.lineEnd !== target.lineStart ? `-${target.lineEnd}` : ""}`,
    selectedText,
    rect,
  });
  if (!instruction) return false;
  await longdoc.createAnnotationFromTarget(target, instruction, { openPanel: false });
  return true;
}

function previewFrameOrigin() {
  try {
    return new URL(typstPreviewFrameEl?.src || window.location.href, window.location.href).origin;
  } catch (_) {
    return window.location.origin;
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
