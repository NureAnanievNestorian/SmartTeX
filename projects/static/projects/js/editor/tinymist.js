/**
 * Tinymist LSP client.
 *
 * Manages a WebSocket to the backend tinymist bridge and exposes:
 *  - document synchronisation (didOpen / didChange / didSave / didClose)
 *  - LSP request helpers (completion, hover, definition, formatting)
 *  - auto-registration of LSP completion + hover providers into cm.js
 */
import * as state from "./state.js";
import * as cm from "./cm.js";
import * as apiMod from "./api.js";

const { s, cfg } = state;
const { api } = apiMod;

let _ws = null;
let _nextId = 1;
const _pending = new Map(); // id -> { resolve, reject, timer }
let _reconnectTimer = null;
let _status = "disconnected";
let _rootUri = "";
const _openDocs = new Map(); // uri -> version number
let _statusEl = null;
let _legend = { tokenTypes: [], tokenModifiers: [] };
let _navCallback = null; // (filename, line, char) => Promise<void> — set by app.js
let _referencesCallback = null; // (filename, line, char) => Promise<void> — set by app.js
let _documentSymbolsCallback = null; // (items) => void
let _documentLinksCallback = null; // (filename) => Promise<void>
let _stTokensTimer = null;
const _semanticTokensByFile = new Map();
const _documentSymbolsByFile = new Map();
const _documentLinksByFile = new Map();
const _foldRangesByFile = new Map();
let _citationIndexCache = null;
let _citationIndexPromise = null;

export function setNavigationCallback(fn) { _navCallback = fn; }
export function setReferencesCallback(fn) { _referencesCallback = fn; }
export function setDocumentSymbolsCallback(fn) { _documentSymbolsCallback = fn; }
export function setDocumentLinksCallback(fn) { _documentLinksCallback = fn; }

function _debugEnabled() {
  try {
    return localStorage.getItem("smarttex.tinymist.debug") === "1";
  } catch (_) {
    return false;
  }
}

function _debug(...args) {
  if (_debugEnabled()) console.log("[tinymist]", ...args);
}

function _escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function _formatInlineMarkdown(text) {
  return _escapeHtml(text)
    .replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)/g, (_m, label, target) => `<a href="#" class="cm-lsp-link" data-target="${_escapeHtml(target)}">${label}</a>`)
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function _renderHoverMarkdown(raw) {
  const source = String(raw || "").replace(/\r\n/g, "\n").trim();
  if (!source) return "";

  const blocks = source.split(/\n{2,}/).map(part => part.trim()).filter(Boolean);
  return blocks.map(block => {
    const fence = block.match(/^```([^\n]*)\n([\s\S]*?)\n```$/);
    if (fence) {
      const lang = fence[1].trim();
      const code = _escapeHtml(fence[2]);
      return `<pre class="cm-lsp-hover-code"${lang ? ` data-lang="${_escapeHtml(lang)}"` : ""}><code>${code}</code></pre>`;
    }
    if (/^-{3,}$/.test(block)) {
      return `<hr>`;
    }
    const heading = block.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(6, heading[1].length);
      return `<h${level}>${_formatInlineMarkdown(heading[2])}</h${level}>`;
    }
    const lines = block.split("\n").map(line => _formatInlineMarkdown(line));
    return `<p>${lines.join("<br>")}</p>`;
  }).join("");
}

function _resetSessionState() {
  clearTimeout(_stTokensTimer);
  _stTokensTimer = null;
  _openDocs.clear();
  _rootUri = "";
  _legend = { tokenTypes: [], tokenModifiers: [] };
  _semanticTokensByFile.clear();
  _documentSymbolsByFile.clear();
  _documentLinksByFile.clear();
  _foldRangesByFile.clear();
  _citationIndexCache = null;
  _citationIndexPromise = null;
  cm.clearSemanticTokens();
}

function _rejectPending(reason = "tinymist session reset") {
  for (const [id, entry] of _pending.entries()) {
    clearTimeout(entry.timer);
    entry.reject(new Error(reason));
    _pending.delete(id);
  }
}

// ── Status ────────────────────────────────────────────────────────────────────

function _setStatus(status, label) {
  _status = status;
  if (_statusEl) {
    _statusEl.textContent = `Tinymist: ${label}`;
    _statusEl.dataset.status = status;
    _statusEl.title = `Tinymist LSP (${status}) — натисніть для перезапуску`;
  }
}

export function getStatus()  { return _status; }
export function getRootUri() { return _rootUri; }

export function initStatusEl(el) {
  _statusEl = el;
  if (el) {
    el.addEventListener("click", () => {
      if (s.projectMeta?.markup_type === "typst") restart();
    });
  }
}

// ── Connection ────────────────────────────────────────────────────────────────

export function connect() {
  if (!cfg.projectId) return;
  if (_ws && (_ws.readyState === WebSocket.CONNECTING || _ws.readyState === WebSocket.OPEN)) return;
  _setStatus("connecting", "підключення…");

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  _ws = new WebSocket(`${proto}//${location.host}/ws/projects/${cfg.projectId}/tinymist/`);
  _debug("connect", { projectId: cfg.projectId });

  _ws.onopen = () => _debug("socket open");

  _ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    _debug("socket message", msg);
    _handleMessage(msg);
  };

  _ws.onerror = (event) => {
    _debug("socket error", event);
    _setStatus("error", "помилка з'єднання");
  };

  _ws.onclose = () => {
    _debug("socket close");
    _ws = null;
    _setStatus("disconnected", "відключено");
    _rejectPending("tinymist disconnected");
    _resetSessionState();
    cm.clearLspProviders();
    clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(() => {
      if (s.projectMeta?.markup_type === "typst") connect();
    }, 5000);
  };
}

export function disconnect() {
  clearTimeout(_reconnectTimer);
  _reconnectTimer = null;
  _rejectPending("tinymist disconnected");
  _resetSessionState();
  cm.clearLspProviders();
  if (_ws) {
    _debug("disconnect");
    _ws.onclose = null;
    _ws.close();
    _ws = null;
  }
  _setStatus("disconnected", "відключено");
}

// ── Incoming message dispatch ─────────────────────────────────────────────────

function _handleMessage(msg) {
  // Envelope messages from the bridge
  if (msg.type === "tinymist_connected") {
    _rootUri = msg.root_uri || "";
    if (msg.semantic_tokens_legend?.tokenTypes?.length) {
      _legend = msg.semantic_tokens_legend;
    }
    _setStatus("connected", "готово");
    _registerLspProviders();
    // Re-open the currently active file
    const name = s.activeTabName || s.selectedFile?.name || "";
    _openMainDocumentContext(name);
    if (name && cm.view) {
      const content = cm.getContent();
      _sendDidOpen(name, content);
    }
    // Open all other open Typst tabs so the server sees the whole workspace
    _openAllProjectFiles(name);
    return;
  }

  if (msg.type === "tinymist_error") {
    _setStatus("error", msg.message || "помилка сервера");
    return;
  }

  // JSON-RPC response to a browser request
  if ("id" in msg && !("method" in msg)) {
    const entry = _pending.get(msg.id);
    if (entry) {
      _pending.delete(msg.id);
      clearTimeout(entry.timer);
      if (msg.error) {
        entry.reject(new Error(msg.error.message || "LSP error"));
      } else {
        entry.resolve(msg.result ?? null);
      }
    }
    return;
  }

  // Server push notifications
  if (msg.method === "textDocument/publishDiagnostics") {
    _onPublishDiagnostics(msg.params);
  }
  // Other notifications (window/logMessage, etc.) are ignored for now
}

function _onPublishDiagnostics(params) {
  if (!params?.uri || !_rootUri) return;
  const fileUri = String(params.uri);
  // Strip root URI prefix to get a relative filename
  const root = _rootUri.endsWith("/") ? _rootUri : _rootUri + "/";
  const filename = fileUri.startsWith(root) ? fileUri.slice(root.length) : null;
  if (!filename) return;

  const lspDiags = (params.diagnostics || []).map(d => ({
    file: filename,
    line: (d.range?.start?.line ?? 0) + 1,
    column: (d.range?.start?.character ?? 0) + 1,
    severity: d.severity === 2 ? "warning" : "error",
    message: String(d.message || ""),
    source: "lsp",
  }));
  cm.setLspDiagnostics(filename, lspDiags);
}

function _activeFilename() {
  return String(s.activeTabName || s.selectedFile?.name || "");
}

function _fileNameFromUri(uri) {
  const root = _rootUri.endsWith("/") ? _rootUri : _rootUri + "/";
  const value = String(uri || "");
  return value.startsWith(root) ? value.slice(root.length) : value;
}

function _citationKeyAt(docText, pos) {
  const before = String(docText || "").slice(0, pos);
  const match = before.match(/@([A-Za-z0-9:_-]+)$/);
  return match ? match[1] : "";
}

function _invalidateCitationIndex(filename = "") {
  const ext = String(filename || "").split(".").pop().toLowerCase();
  if (["typ", "bib", "yaml", "yml"].includes(ext)) {
    _citationIndexCache = null;
    _citationIndexPromise = null;
  }
}

export function invalidateCitationIndex(filename = "") {
  _invalidateCitationIndex(filename);
}

async function _loadCitationIndex() {
  if (_citationIndexCache) return _citationIndexCache;
  if (_citationIndexPromise) return _citationIndexPromise;
  _citationIndexPromise = api(`/api/projects/${cfg.projectId}/typst/citations/`, { method: "GET" })
    .then(payload => {
      _citationIndexCache = {
        entries: Array.isArray(payload?.entries) ? payload.entries : [],
        sourceFiles: Array.isArray(payload?.source_files) ? payload.source_files : [],
      };
      return _citationIndexCache;
    })
    .catch(() => ({ entries: [], sourceFiles: [] }))
    .finally(() => {
      _citationIndexPromise = null;
    });
  return _citationIndexPromise;
}

async function _findCitationEntry(key) {
  const needle = String(key || "").trim().toLowerCase();
  if (!needle) return null;
  const index = await _loadCitationIndex();
  return (index.entries || []).find(item => String(item?.key || "").toLowerCase() === needle) || null;
}

function _decodeTinymistCommandTarget(target) {
  const value = String(target || "");
  if (value.startsWith("file://")) return _fileNameFromUri(value);
  if (!value.startsWith("command:tinymist.openInternal?") && !value.startsWith("command:tinymist.openExternal?")) {
    return "";
  }
  try {
    const query = value.split("?")[1] || "";
    const decoded = decodeURIComponent(query);
    const parsed = JSON.parse(decoded);
    const first = Array.isArray(parsed) ? parsed[0] : parsed;
    if (typeof first === "string") return _fileNameFromUri(first);
  } catch (_) {}
  return "";
}

async function _openTargetLink(target) {
  const targetFile = _decodeTinymistCommandTarget(target) || (String(target || "").startsWith("file://") ? _fileNameFromUri(target) : "");
  if (targetFile && _documentLinksCallback) {
    await _documentLinksCallback(targetFile);
    return true;
  }
  return false;
}

function _applyTextEditsToText(text, edits = []) {
  const lines = String(text || "").split("\n");
  const lineOffset = (line, char) => {
    let offset = 0;
    for (let i = 0; i < line; i++) offset += (lines[i] || "").length + 1;
    return offset + char;
  };
  const source = String(text || "");
  const sorted = [...edits].sort((a, b) => {
    if (a.range.start.line !== b.range.start.line) return b.range.start.line - a.range.start.line;
    return b.range.start.character - a.range.start.character;
  });
  let result = source;
  for (const edit of sorted) {
    const from = lineOffset(edit.range.start.line, edit.range.start.character);
    const to = lineOffset(edit.range.end.line, edit.range.end.character);
    result = result.slice(0, from) + String(edit.newText || "") + result.slice(to);
  }
  return result;
}

function _applySemanticTokensForFilename(filename) {
  const target = String(filename || "");
  if (!target || _activeFilename() !== target) return;
  const tokens = _semanticTokensByFile.get(target);
  if (!Array.isArray(tokens) || !_legend?.tokenTypes?.length) {
    cm.clearSemanticTokens();
    return;
  }
  cm.applySemanticTokens(tokens, _legend);
}

function _flattenDocumentSymbols(items, filename, level = 1, into = []) {
  for (const item of Array.isArray(items) ? items : []) {
    const range = item.selectionRange || item.location?.range || item.range || {};
    const start = range.start || {};
    const end = range.end || {};
    const title = String(item.name || item.label || "").trim();
    if (title) {
      into.push({
        title,
        detail: String(item.detail || ""),
        level,
        file_name: filename,
        start_line: (start.line ?? 0) + 1,
        end_line: (end.line ?? start.line ?? 0) + 1,
        kind: Number(item.kind || 0),
      });
    }
    if (Array.isArray(item.children) && item.children.length) {
      _flattenDocumentSymbols(item.children, filename, level + 1, into);
    }
  }
  return into;
}

function _updateActiveOutline(filename) {
  const target = String(filename || "");
  if (_activeFilename() !== target) return;
  const items = _documentSymbolsByFile.get(target) || [];
  _documentSymbolsCallback?.(items);
}

// ── LSP transport ─────────────────────────────────────────────────────────────

function _send(msg) {
  if (!_ws || _ws.readyState !== WebSocket.OPEN) return false;
  _debug("send", msg);
  _ws.send(JSON.stringify(msg));
  return true;
}

function _request(method, params) {
  return new Promise((resolve, reject) => {
    if (!_ws || _ws.readyState !== WebSocket.OPEN) {
      reject(new Error("tinymist not connected"));
      return;
    }
    const id = _nextId++;
    const timer = setTimeout(() => {
      if (_pending.has(id)) {
        _pending.delete(id);
        reject(new Error(`LSP timeout: ${method}`));
      }
    }, 10000);
    _pending.set(id, { resolve, reject, timer });
    _send({ jsonrpc: "2.0", id, method, params });
  });
}

function _notify(method, params) {
  _send({ jsonrpc: "2.0", method, params });
}

// ── URI helpers ───────────────────────────────────────────────────────────────

function _fileUri(filename) {
  const root = _rootUri.endsWith("/") ? _rootUri : _rootUri + "/";
  return root + filename;
}

function _isTyp(name) {
  return String(name || "").toLowerCase().endsWith(".typ");
}

function _openMainDocumentContext(activeFilename = "") {
  const mainFilename = String(s.mainFileName || "");
  if (!mainFilename || !_isTyp(mainFilename) || _status !== "connected") return;
  if (mainFilename === activeFilename && cm.view && _activeFilename() === mainFilename) return;
  const content = String(s.mainFileContent || "");
  if (!content) return;
  didOpen(mainFilename, content);
}

// ── Document synchronisation ──────────────────────────────────────────────────

function _sendDidOpen(filename, content) {
  if (!_isTyp(filename) || _status !== "connected") return;
  const uri = _fileUri(filename);
  const version = 1;
  _openDocs.set(uri, version);
  _notify("textDocument/didOpen", {
    textDocument: { uri, languageId: "typst", version, text: content },
  });
  _scheduleSemanticTokens(filename);
}

export function didOpen(filename, content) {
  if (!_isTyp(filename) || _status !== "connected") return;
  _invalidateCitationIndex(filename);
  const uri = _fileUri(filename);
  if (_openDocs.has(uri)) {
    // Already open: update content
    didChange(filename, content);
    return;
  }
  _sendDidOpen(filename, content);
}

export function didChange(filename, content) {
  if (!_isTyp(filename) || _status !== "connected") return;
  _invalidateCitationIndex(filename);
  const uri = _fileUri(filename);
  const version = (_openDocs.get(uri) ?? 0) + 1;
  _openDocs.set(uri, version);
  _notify("textDocument/didChange", {
    textDocument: { uri, version },
    contentChanges: [{ text: content }],
  });
  _scheduleSemanticTokens(filename);
}

export function didSave(filename) {
  if (!_isTyp(filename) || _status !== "connected") return;
  const uri = _fileUri(filename);
  if (!_openDocs.has(uri)) return;
  _notify("textDocument/didSave", { textDocument: { uri } });
}

export function didClose(filename) {
  if (!_isTyp(filename) || _status !== "connected") return;
  _invalidateCitationIndex(filename);
  const uri = _fileUri(filename);
  if (!_openDocs.has(uri)) return;
  _openDocs.delete(uri);
  _notify("textDocument/didClose", { textDocument: { uri } });
}

// ── LSP feature requests ──────────────────────────────────────────────────────

function _docPos(filename, lineNum, char) {
  return { textDocument: { uri: _fileUri(filename) }, position: { line: lineNum - 1, character: char - 1 } };
}

async function _requestCompletion(filename, lineNum, char, trigger = "") {
  _debug("request completion", { filename, lineNum, char, trigger });
  return _request("textDocument/completion", {
    ..._docPos(filename, lineNum, char),
    context: { triggerKind: 1 },
  });
}

async function _requestHover(filename, lineNum, char) {
  return _request("textDocument/hover", _docPos(filename, lineNum, char));
}

export async function requestDefinition(filename, lineNum, char) {
  return _request("textDocument/definition", _docPos(filename, lineNum, char));
}

export async function requestFormatting(filename) {
  return _request("textDocument/formatting", {
    textDocument: { uri: _fileUri(filename) },
    options: { tabSize: 2, insertSpaces: true },
  });
}

export async function requestReferences(filename, lineNum, char) {
  return _request("textDocument/references", {
    ..._docPos(filename, lineNum, char),
    context: { includeDeclaration: true },
  });
}

export async function requestDocumentSymbols(filename) {
  return _request("textDocument/documentSymbol", {
    textDocument: { uri: _fileUri(filename) },
  });
}

export async function requestWorkspaceSymbols(query = "") {
  return _request("workspace/symbol", { query: String(query || "") });
}

export async function requestDocumentLinks(filename) {
  return _request("textDocument/documentLink", {
    textDocument: { uri: _fileUri(filename) },
  });
}

export async function requestFoldingRanges(filename) {
  return _request("textDocument/foldingRange", {
    textDocument: { uri: _fileUri(filename) },
  });
}

export async function requestRename(filename, lineNum, char, newName) {
  return _request("textDocument/rename", {
    ..._docPos(filename, lineNum, char),
    newName: String(newName || ""),
  });
}

export async function requestSignatureHelp(filename, lineNum, char) {
  return _request("textDocument/signatureHelp", _docPos(filename, lineNum, char));
}

async function _requestSemanticTokensFull(filename) {
  return _request("textDocument/semanticTokens/full", {
    textDocument: { uri: _fileUri(filename) },
  });
}

function _scheduleSemanticTokens(filename) {
  clearTimeout(_stTokensTimer);
  _stTokensTimer = setTimeout(async () => {
    if (_status !== "connected") return;
    try {
      const result = await _requestSemanticTokensFull(filename);
      const tokenData = Array.isArray(result?.data) ? result.data : [];
      _semanticTokensByFile.set(String(filename || ""), tokenData);
      _applySemanticTokensForFilename(filename);
    } catch (_) {}
  }, 1500);
}

async function _refreshDocumentSymbols(filename) {
  if (!_isTyp(filename) || _status !== "connected") return [];
  try {
    const result = await requestDocumentSymbols(filename);
    const flat = Array.isArray(result)
      ? _flattenDocumentSymbols(result, filename)
      : _flattenDocumentSymbols([result], filename);
    _documentSymbolsByFile.set(String(filename || ""), flat);
    _updateActiveOutline(filename);
    return flat;
  } catch (_) {
    return [];
  }
}

async function _refreshDocumentLinks(filename) {
  if (!_isTyp(filename) || _status !== "connected") return [];
  try {
    const result = await requestDocumentLinks(filename);
    const links = Array.isArray(result) ? result : [];
    _documentLinksByFile.set(String(filename || ""), links);
    return links;
  } catch (_) {
    return [];
  }
}

async function _refreshFoldingRanges(filename) {
  if (!_isTyp(filename) || _status !== "connected") return [];
  try {
    const result = await requestFoldingRanges(filename);
    const ranges = Array.isArray(result) ? result : [];
    _foldRangesByFile.set(String(filename || ""), ranges);
    return ranges;
  } catch (_) {
    return [];
  }
}

export async function formatDocument(filename) {
  if (!_isTyp(filename) || _status !== "connected") return false;
  try {
    const edits = await requestFormatting(filename);
    if (!edits?.length) return false;
    cm.applyTextEdits(edits);
    return true;
  } catch (e) {
    _debug("formatDocument error", e);
    return false;
  }
}

export async function renameSymbol(filename, lineNum, char, newName) {
  if (!_isTyp(filename) || _status !== "connected") return false;
  try {
    const edit = await requestRename(filename, lineNum, char, newName);
    const changes = edit?.changes || {};
    const entries = Object.entries(changes);
    if (!entries.length) return false;
    for (const [uri, edits] of entries) {
      const targetName = _fileNameFromUri(uri);
      if (!targetName || !Array.isArray(edits)) continue;
      let currentText = "";
      if (targetName === filename && cm.view && _activeFilename() === targetName) {
        currentText = cm.getContent();
      } else if (targetName === s.mainFileName) {
        const payload = await api(`/api/projects/${cfg.projectId}/file/`, { method: "GET" });
        currentText = String(payload.content || "");
      } else {
        const payload = await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(targetName)}/content/?include_text=1`);
        currentText = String(payload.text_content || "");
      }
      const nextText = _applyTextEditsToText(currentText, edits);
      if (targetName === s.mainFileName) {
        await api(`/api/projects/${cfg.projectId}/file/`, {
          method: "PUT",
          body: JSON.stringify({ content: nextText }),
        });
      } else {
        await api(`/api/projects/${cfg.projectId}/files/${encodeURIComponent(targetName)}/content/`, {
          method: "PUT",
          body: JSON.stringify({ content: nextText }),
        });
      }
    }
    return true;
  } catch (e) {
    _debug("renameSymbol error", e);
    return false;
  }
}

function _openAllProjectFiles(skipFilename) {
  // Tinymist already has the project rootUri so it can resolve cross-file imports
  // from the filesystem. We additionally open tabs whose EditorState is cached so
  // the server gets live (possibly unsaved) content for background files.
  // Files without a cached state will be opened via didOpen when the user visits them.
  try {
    const tabs = s.openTabs || [];
    tabs.forEach(tab => {
      if (!tab?.name || tab.name === skipFilename) return;
      if (!_isTyp(tab.name)) return;
      const uri = _fileUri(tab.name);
      if (_openDocs.has(uri)) return;
      if (!cm.hasTabState(tab.name)) return;
      // Read cached content via a temporary state access
      const savedState = cm.getTabStateContent?.(tab.name);
      if (savedState == null) return;
      _openDocs.set(uri, 1);
      _notify("textDocument/didOpen", {
        textDocument: { uri, languageId: "typst", version: 1, text: savedState },
      });
    });
  } catch (_) {}
}

export function restart() {
  disconnect();
  clearTimeout(_reconnectTimer);
  setTimeout(() => {
    if (s.projectMeta?.markup_type === "typst") connect();
  }, 300);
}

export function refreshActiveDocument(filename) {
  const target = String(filename || _activeFilename());
  if (!_isTyp(target) || _status !== "connected") {
    _documentSymbolsCallback?.([]);
    cm.clearSemanticTokens();
    return;
  }
  _updateActiveOutline(target);
  if (_semanticTokensByFile.has(target)) {
    _applySemanticTokensForFilename(target);
  } else {
    _scheduleSemanticTokens(target);
  }
  if (!_documentSymbolsByFile.has(target)) {
    _refreshDocumentSymbols(target).catch(() => {});
  } else {
    _updateActiveOutline(target);
  }
  if (!_documentLinksByFile.has(target)) _refreshDocumentLinks(target).catch(() => {});
  if (!_foldRangesByFile.has(target)) _refreshFoldingRanges(target).catch(() => {});
}

// ── LSP provider registration into cm.js ─────────────────────────────────────

const _LSP_KIND_MAP = {
  1: "text", 2: "method", 3: "function", 4: "constructor", 5: "field",
  6: "variable", 7: "class", 8: "interface", 9: "module", 10: "property",
  11: "unit", 12: "value", 13: "enum", 14: "keyword", 15: "snippet",
  16: "color", 17: "file", 18: "reference", 19: "folder", 20: "enum",
  21: "constant", 22: "struct", 23: "event", 24: "operator", 25: "type",
};

function _normalizeCompletionNeedle(typed = "", trigger = "") {
  const raw = String(typed || "");
  if (trigger && raw.startsWith(trigger)) return raw.slice(trigger.length).toLowerCase();
  return raw.toLowerCase();
}

function _normalizeCompletionLabel(label = "", trigger = "") {
  let normalized = String(label || "").trim().toLowerCase();
  if (trigger && normalized.startsWith(trigger)) normalized = normalized.slice(trigger.length);
  return normalized;
}

function _prefixScore(label, needle) {
  if (!needle) return 0;
  if (label === needle) return 120;
  if (label.startsWith(needle)) return 90 - Math.min(40, label.length - needle.length);
  if (label.includes(needle)) return 30;
  return -1;
}

function _allowCompletionItemForTrigger(item, trigger = "") {
  if (!trigger) return true;
  const label = String(item?.label || "");
  const kind = Number(item?.kind || 0);

  if (trigger === "@") {
    return label.startsWith("@") || kind === 18 || kind === 6 || kind === 5;
  }

  if (trigger === "<") {
    return label.startsWith("<") || kind === 18 || kind === 6 || kind === 5;
  }

  if (trigger === "#") {
    if (label.startsWith("@") || label.startsWith("<")) return false;
    return [
      2,  // method
      3,  // function
      4,  // constructor
      6,  // variable
      9,  // module
      10, // property
      14, // keyword
      15, // snippet
      17, // file
      18, // reference
      21, // constant
      22, // struct
      25, // type
    ].includes(kind);
  }

  return true;
}

function _registerLspProviders() {
  cm.setLspFoldProvider((state, lineStart, lineEnd) => {
    const filename = _activeFilename();
    const ranges = _foldRangesByFile.get(filename) || [];
    const line = state.doc.lineAt(lineStart).number - 1;
    const hit = ranges.find(item => (item.startLine ?? -1) === line);
    if (!hit) return null;
    const startDocLine = state.doc.line(Math.min(state.doc.lines, (hit.startLine ?? 0) + 1));
    const endDocLine = state.doc.line(Math.min(state.doc.lines, (hit.endLine ?? hit.startLine ?? 0) + 1));
    const from = Math.min(startDocLine.to, startDocLine.from + Math.max(0, Number(hit.startCharacter ?? 0)));
    const toBase = Math.min(endDocLine.to, endDocLine.from + Math.max(0, Number(hit.endCharacter ?? 0)));
    const to = toBase > from ? toBase : endDocLine.to;
    return to > from ? { from, to } : null;
  });

  cm.setLspDefinitionProvider(async (view, pos) => {
    const filename = s.activeTabName || s.selectedFile?.name || "";
    if (!_isTyp(filename) || _status !== "connected") return false;
    const doc = view.state.doc;
    const line = doc.lineAt(pos);
    const citationKey = _citationKeyAt(doc.toString(), pos);
    try {
      const result = await requestDefinition(filename, line.number, pos - line.from + 1);
      if (result) {
        const locs = Array.isArray(result) ? result : [result];
        if (locs.length) {
          const loc = locs[0];
          const locUri = String(loc.uri || "");
          const locLine = (loc.range?.start?.line ?? 0) + 1;
          const locChar = (loc.range?.start?.character ?? 0) + 1;
          const currentUri = _fileUri(filename);
          if (locUri === currentUri) {
            cm.jumpToLine(locLine, locChar);
            return true;
          }
          if (_navCallback) {
            const root = _rootUri.endsWith("/") ? _rootUri : _rootUri + "/";
            const targetFilename = locUri.startsWith(root) ? locUri.slice(root.length) : null;
            if (targetFilename) {
              await _navCallback(targetFilename, locLine, locChar);
              return true;
            }
          }
        }
      }
    } catch (_) {
      // fall back to SmartTeX citation index below
    }
    if (citationKey && _navCallback) {
      const entry = await _findCitationEntry(citationKey);
      if (entry?.file) {
        await _navCallback(entry.file, Number(entry.line || 1), Number(entry.column || 1));
        return true;
      }
    }
    return false;
  });

  cm.setLspCompletionProvider(async (context, query) => {
    const filename = s.activeTabName || s.selectedFile?.name || "";
    if (!_isTyp(filename)) return [];
    const doc = context.state.doc;
    const line = doc.lineAt(context.pos);
    const replaceFrom = query?.trigger ? query.from + 1 : query.from;
    const needle = _normalizeCompletionNeedle(query?.typed, query?.trigger);
    _debug("completion provider", {
      filename,
      pos: context.pos,
      line: line.number,
      from: query?.from,
      replaceFrom,
      typed: query?.typed,
      trigger: query?.trigger,
      needle,
    });
    try {
      const result = await _requestCompletion(
        filename,
        line.number,
        context.pos - line.from + 1,
        query?.trigger || "",
      );
      _debug("completion response", result);
      if (!result) return null;
      const items = Array.isArray(result) ? result : (result.items || []);
      const options = items
        .map(item => {
          if (!_allowCompletionItemForTrigger(item, query?.trigger)) return null;
          const textEdit = item.textEdit?.newText || item.textEdit?.text || "";
          const insertText = textEdit || item.insertText || item.label || "";
          const documentation = typeof item.documentation === "string"
            ? item.documentation
            : item.documentation?.value || "";
          const commit = String(insertText || "");
          if (!item.label || !commit) return null;
          const normalizedLabel = _normalizeCompletionLabel(item.label, query?.trigger);
          const score = _prefixScore(normalizedLabel, needle);
          if (needle && score < 0) return null;
          return {
            label: item.label,
            type: _LSP_KIND_MAP[item.kind] || "text",
            detail: item.detail || item.labelDetails?.detail || "",
            info: documentation,
            apply: commit,
            boost: 95 + Math.max(0, score),
          };
        })
        .filter(Boolean)
        .sort((a, b) => (b.boost || 0) - (a.boost || 0));
      _debug("completion normalized", {
        items: items.length,
        options: options.length,
      });
      if (!options.length) return null;
      return {
        from: replaceFrom,
        options,
        validFor: query?.trigger ? /^[A-Za-z0-9:_./-]*$/ : /^[#@<]?[A-Za-z0-9:_./-]*$/,
      };
    } catch (e) {
      _debug("completion error", e);
    }
    if (query?.trigger === "@") {
      try {
        const index = await _loadCitationIndex();
        const needle = _normalizeCompletionNeedle(query?.typed, query?.trigger);
        const options = (index.entries || [])
          .filter(item => {
            const key = String(item?.key || "");
            return !needle || key.toLowerCase().includes(needle);
          })
          .map(item => ({
            label: `@${item.key}`,
            type: "variable",
            detail: item.file || "citation",
            info: item.file ? `${item.file}:${item.line || 1}` : "citation",
            boost: 92,
          }));
        if (options.length) {
          return {
            from: replaceFrom,
            options,
            validFor: /^[A-Za-z0-9:_./-]*$/,
          };
        }
      } catch (_) {}
    }
    return null;
  });

  cm.setLspMetaClickProvider(async (view, pos) => {
    const filename = s.activeTabName || s.selectedFile?.name || "";
    if (!_isTyp(filename) || _status !== "connected") return false;
    const doc = view.state.doc;
    const line = doc.lineAt(pos);
    let links = _documentLinksByFile.get(filename) || [];
    if (!links.length) {
      links = await _refreshDocumentLinks(filename);
    }
    const currentLine = line.number - 1;
    const currentChar = pos - line.from;
    const link = links.find(item => {
      const start = item.range?.start;
      const end = item.range?.end;
      if (!start || !end) return false;
      if (currentLine < start.line || currentLine > end.line) return false;
      if (currentLine === start.line && currentChar < start.character) return false;
      if (currentLine === end.line && currentChar > end.character) return false;
      return true;
    });
    if (link?.target && await _openTargetLink(link.target)) {
      return true;
    }
    try {
      const result = await requestDefinition(filename, line.number, pos - line.from + 1);
      const locs = Array.isArray(result) ? result : (result ? [result] : []);
      const loc = locs[0];
      if (loc) {
        const locUri = String(loc.uri || "");
        const locLine = (loc.range?.start?.line ?? 0) + 1;
        const locChar = (loc.range?.start?.character ?? 0) + 1;
        const currentUri = _fileUri(filename);
        if (locUri === currentUri) {
          cm.jumpToLine(locLine, locChar);
          return true;
        }
        if (_navCallback) {
          const root = _rootUri.endsWith("/") ? _rootUri : _rootUri + "/";
          const targetFilename = locUri.startsWith(root) ? locUri.slice(root.length) : null;
          if (targetFilename) {
            await _navCallback(targetFilename, locLine, locChar);
            return true;
          }
        }
      }
    } catch (_) {}
    if (!_referencesCallback) return false;
    await _referencesCallback(filename, line.number, pos - line.from + 1);
    return true;
  });

  cm.setLspHoverProvider(async (view, pos) => {
    const filename = s.activeTabName || s.selectedFile?.name || "";
    if (!_isTyp(filename)) return null;
    const doc = view.state.doc;
    const line = doc.lineAt(pos);
    try {
      const result = await _requestHover(filename, line.number, pos - line.from + 1);
      if (!result) return null;
      const c = result.contents;
      const text = typeof c === "string" ? c
        : c?.value ? c.value
        : Array.isArray(c) ? c.map(x => (typeof x === "string" ? x : x.value || "")).join("\n\n")
        : null;
      if (!text) return null;
      const dom = document.createElement("div");
      dom.className = "cm-lsp-hover";
      dom.innerHTML = _renderHoverMarkdown(text);
      dom.addEventListener("click", event => {
        const linkEl = event.target instanceof Element ? event.target.closest(".cm-lsp-link") : null;
        if (!linkEl) return;
        event.preventDefault();
        const target = linkEl.getAttribute("data-target") || "";
        _openTargetLink(target).catch(() => {});
      });
      return { pos, above: true, create() { return { dom }; } };
    } catch (e) {
      return null;
    }
  });
}
