const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const http = require("http");
const https = require("https");
const os = require("os");
const path = require("path");

let output;
let compileOutput;
let compileDiagnostics;
let statusBar;
let stateWatcher;
let extensionPath = "";
let syncTimer = null;
let syncInFlight = false;
let annotationsProvider;
let autoSaveTimer = null;
let autoSaveInProgress = false;
let annotationDecorationTypes = {};
let annotationRefreshTimer = null;
let annotationRefreshInFlight = false;
let annotationCodeLensProvider;
let dashboardProvider;
let aiChangesProvider;
let fileAnnotationsProvider;
let historyProvider;
let historyPanel = null;
let settingsProvider;
let previewProvider;
let problemsProvider;
let pdfEmbedsProvider;
const previewPanels = new Map();
let previewRevealTimer = null;
// When we move the editor in response to a preview-originated jump, suppress the
// cursor-follow reveal for a short window so we do not bounce a scroll command
// back into the preview and create a scroll feedback loop.
let suppressPreviewRevealUntil = 0;
let lastTextEditorViewColumn = vscode.ViewColumn.One;
let authLoginPromise = null;
let lastCompileState = null;
let lastCompileDiagnostics = [];
let lastCompileLog = "";
let lastPdfEmbeds = {};
let pdfEmbedsLoading = false;
let pdfEmbedsError = "";
let lastWorkspaceStatus = null;
let workspaceStatusInFlight = false;
let lastInactiveWorkspacePromptAt = 0;
let realtimeRequest = null;
let realtimeReconnectTimer = null;
let realtimeProjectId = 0;
let realtimeReconnectDelayMs = 1500;

function config() {
  return vscode.workspace.getConfiguration("smarttex");
}

function extensionWorkspaceFolder() {
  if (extensionPath) return extensionPath;
  const folders = vscode.workspace.workspaceFolders || [];
  const extensionFolder = folders.find(folder => fs.existsSync(path.join(folder.uri.fsPath, "package.json"))
    && fs.existsSync(path.join(folder.uri.fsPath, "extension.js")));
  return extensionFolder?.uri?.fsPath || folders[0]?.uri?.fsPath || "";
}

function devAgentPath() {
  if (!extensionPath) return "";
  const candidate = path.resolve(extensionPath, "..", "local_agent", "go", "scripts", "smarttex-local-dev.sh");
  return fs.existsSync(candidate) ? candidate : "";
}

function hasExplicitSetting(name) {
  const inspected = config().inspect(name);
  return Boolean(inspected?.globalValue !== undefined
    || inspected?.workspaceValue !== undefined
    || inspected?.workspaceFolderValue !== undefined);
}

function configuredSetting(name) {
  if (!hasExplicitSetting(name)) return undefined;
  return config().get(name);
}

function expandHome(value) {
  const text = String(value || "");
  if (text === "~") return os.homedir();
  if (text.startsWith("~/") || text.startsWith("~\\")) return path.join(os.homedir(), text.slice(2));
  return text;
}

function expandConfigPath(value) {
  let text = expandHome(value);
  const workspaceFolder = extensionWorkspaceFolder();
  if (workspaceFolder) {
    text = text.replace(/\$\{workspaceFolder\}/g, workspaceFolder);
  }
  return text;
}

function serverUrl() {
  const fallback = devAgentPath() && !hasExplicitSetting("serverUrl") ? "http://localhost:8000" : "https://smart-tex.pp.ua";
  return String(configuredSetting("serverUrl") || fallback).replace(/\/+$/, "");
}

function agentPath() {
  const fallback = devAgentPath() && !hasExplicitSetting("localAgentPath") ? devAgentPath() : "smarttex-local";
  return expandConfigPath(configuredSetting("localAgentPath") || fallback);
}

function workspaceRoot() {
  const fallback = devAgentPath() && !hasExplicitSetting("workspaceRoot") ? "~/.smarttex-local-dev" : "~/.smarttex-local";
  return expandConfigPath(configuredSetting("workspaceRoot") || fallback);
}

function autoWatchAfterOpen() {
  if (!hasExplicitSetting("autoWatchAfterOpen") && devAgentPath()) return false;
  return Boolean(config().get("autoWatchAfterOpen"));
}

function syncOnSave() {
  if (!hasExplicitSetting("syncOnSave") && devAgentPath()) return true;
  return Boolean(config().get("syncOnSave"));
}

function autoSaveAndSync() {
  if (!hasExplicitSetting("autoSaveAndSync") && devAgentPath()) return true;
  return Boolean(config().get("autoSaveAndSync"));
}

function autoSaveDebounceMs() {
  const raw = Number(config().get("autoSaveDebounceMs") || 1200);
  return Math.max(250, Math.min(10000, raw || 1200));
}

function annotationRefreshIntervalMs() {
  const raw = Number(config().get("annotationRefreshIntervalMs") || 45000);
  return Math.max(5000, Math.min(60000, raw || 15000));
}

function annotationCodeLensEnabled() {
  return config().get("annotationCodeLens") !== false;
}

function quickAnnotationTemplates() {
  const templates = config().get("quickAnnotationTemplates");
  if (!Array.isArray(templates)) return [];
  return templates.map(item => String(item || "").trim()).filter(Boolean);
}

function showResolvedAnnotations() {
  return config().get("showResolvedAnnotations") === true;
}

function activeAnnotationItems(items) {
  if (showResolvedAnnotations()) return items;
  return items.filter(item => !["done", "dismissed"].includes(String(item.status || "")));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function localBridgeUrl() {
  return String(config().get("localBridgeUrl") || "http://127.0.0.1:8765").replace(/\/+$/, "");
}

function previewTheme() {
  const value = String(config().get("previewTheme") || "auto");
  return ["auto", "light", "dark"].includes(value) ? value : "auto";
}

function previewThemeParam(theme = previewTheme()) {
  if (theme === "dark") return "always";
  if (theme === "light") return "never";
  return "auto";
}

function previewFollowCursor() {
  return config().get("previewFollowCursor") !== false;
}

function previewClickToCode() {
  return config().get("previewClickToCode") !== false;
}

function localConfigPath() {
  if (process.env.SMARTTEX_LOCAL_CONFIG) return expandHome(process.env.SMARTTEX_LOCAL_CONFIG);
  return path.join(os.homedir(), ".smarttex-local", "config.json");
}

function localBridgeSecret() {
  if (process.env.SMARTTEX_LOCAL_SECRET) return String(process.env.SMARTTEX_LOCAL_SECRET);
  try {
    const raw = fs.readFileSync(localConfigPath(), "utf8");
    const parsed = JSON.parse(raw);
    return String(parsed.bridge_secret || "");
  } catch (_) {
    return "";
  }
}

function localAccessToken() {
  if (process.env.SMARTTEX_TOKEN) return String(process.env.SMARTTEX_TOKEN);
  try {
    const raw = fs.readFileSync(localConfigPath(), "utf8");
    const parsed = JSON.parse(raw);
    return String(parsed.access_token || "");
  } catch (_) {
    return "";
  }
}

function localAuthConfigured() {
  if (process.env.SMARTTEX_TOKEN) return true;
  try {
    const raw = fs.readFileSync(localConfigPath(), "utf8");
    const parsed = JSON.parse(raw);
    return Boolean(String(parsed.access_token || "").trim() || String(parsed.refresh_token || "").trim());
  } catch (_) {
    return false;
  }
}

function activePreviewPayload() {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.uri.scheme !== "file") return null;
  const filename = workspaceRelativePath(editor.document.uri.fsPath);
  if (!filename || filename.startsWith(".smarttex/")) return null;
  const lineNumber = editor.selection.active.line + 1;
  const columnNumber = editor.selection.active.character + 1;
  const lineText = editor.document.lineAt(editor.selection.active.line).text;
  let heading = "";
  for (let i = editor.selection.active.line; i >= 0; i--) {
    const text = editor.document.lineAt(i).text.trim();
    if (/^=+\s+/.test(text)) {
      heading = text.replace(/^=+\s+/, "");
      break;
    }
  }
  return {
    filename,
    lineNumber,
    columnNumber,
    lineText,
    excerpt: lineText,
    heading,
  };
}

function revealPreviewSelection(force = false) {
  if (!force && Date.now() < suppressPreviewRevealUntil) return;
  const projectId = projectIdFromWorkspace();
  if (!projectId) return;
  const payload = activePreviewPayload();
  if (!payload) return;
  const type = force ? "force-reveal" : "reveal";
  for (const session of previewPanels.values()) {
    if (session.projectId === projectId && (force || session.state?.follow !== false)) {
      session.webview.postMessage({ type, payload });
    }
  }
}

function normalizePreviewControlLocation(payload = {}) {
  const start = payload.start;
  const zeroBasedLine = Array.isArray(start)
    ? Number(start[0] ?? 0)
    : Number(start?.row ?? start?.line ?? payload.line ?? 0);
  const zeroBasedColumn = Array.isArray(start)
    ? Number(start[1] ?? 0)
    : Number(start?.column ?? start?.character ?? payload.character ?? 0);
  return {
    filename: payload.filepath || payload.path || payload.filename || "",
    line: zeroBasedLine + 1,
    column: zeroBasedColumn + 1,
  };
}

async function handlePreviewControlMessage(session, data) {
  if (String(data?.event || "") !== "editorScrollTo") return;
  if (session?.state?.click === false) return;
  const location = normalizePreviewControlLocation(data);
  await openPreviewLocation(location);
}

function schedulePreviewReveal() {
  if (!previewFollowCursor()) return;
  if (Date.now() < suppressPreviewRevealUntil) return;
  clearTimeout(previewRevealTimer);
  previewRevealTimer = setTimeout(() => revealPreviewSelection(false), 220);
}

function reloadPreviewSessions() {
  for (const session of previewPanels.values()) {
    try {
      session.webview?.postMessage({ type: "smarttex-preview-refresh" });
    } catch (_) {
      // Preview refresh is best-effort; Tinymist usually watches files itself.
    }
  }
}

function normalizePreviewText(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .replace(/[“”«»"]/g, '"')
    .replace(/[’']/g, "'")
    .trim()
    .toLowerCase();
}

function resolvePreviewFilename(value) {
  const raw = String(value || "").replace(/^file:\/\//, "");
  if (!raw) return "";
  const root = activeWorkspaceFolder();
  if (path.isAbsolute(raw) && root) {
    const rel = path.relative(root, raw).replace(/\\/g, "/");
    if (rel && !rel.startsWith("../") && !path.isAbsolute(rel)) return rel;
  }
  const normalized = raw.replace(/\\/g, "/");
  if (root) {
    const files = listTypstWorkspaceFiles(root);
    const hit = files.find(file => normalized.endsWith(file));
    if (hit) return hit;
  }
  return normalized;
}

function listTypstWorkspaceFiles(root) {
  const out = [];
  function walk(dir) {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
      if (entry.name === ".smarttex" || entry.name === ".git") continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
      } else if (entry.isFile() && entry.name.toLowerCase().endsWith(".typ")) {
        out.push(path.relative(root, full).replace(/\\/g, "/"));
      }
    }
  }
  if (root) walk(root);
  return out;
}

function locatePreviewTextInWorkspace(rawText) {
  const root = activeWorkspaceFolder();
  const target = normalizePreviewText(rawText);
  if (!root || target.length < 3) return null;
  const files = listTypstWorkspaceFiles(root);
  const activeRel = vscode.window.activeTextEditor ? workspaceRelativePath(vscode.window.activeTextEditor.document.uri.fsPath) : "";
  files.sort((a, b) => (a === activeRel ? -1 : b === activeRel ? 1 : 0));
  for (const file of files) {
    let text = "";
    try {
      text = fs.readFileSync(path.join(root, filepathFromProjectPath(file)), "utf8");
    } catch (_) {
      continue;
    }
    const lines = text.split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const hay = normalizePreviewText(lines[i]);
      if (hay === target || hay.includes(target) || (target.includes(hay) && hay.length >= 8)) {
        return { filename: file, line: i + 1, column: 1 };
      }
    }
  }
  return null;
}

async function openPreviewLocation(location) {
  const filename = resolvePreviewFilename(location?.filename || "");
  const line = Math.max(1, Number(location?.line || 1));
  const column = Math.max(1, Number(location?.column || 1));
  if (!filename) return false;
  const fullPath = path.join(activeWorkspaceFolder(), filepathFromProjectPath(filename));
  if (!fs.existsSync(fullPath)) return false;
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(fullPath));
  const editor = await vscode.window.showTextDocument(document, {
    preview: false,
    viewColumn: lastTextEditorViewColumn || vscode.ViewColumn.One,
  });
  const safeLine = Math.min(document.lineCount - 1, line - 1);
  const safeColumn = Math.min(document.lineAt(safeLine).text.length, column - 1);
  const position = new vscode.Position(safeLine, safeColumn);
  // This selection change comes from the preview, so silence the cursor-follow
  // reveal it would otherwise trigger and avoid a scroll feedback loop.
  suppressPreviewRevealUntil = Date.now() + 700;
  editor.selection = new vscode.Selection(position, position);
  editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenterIfOutsideViewport);
  return true;
}

async function handlePreviewClick(payload) {
  const location = payload?.location;
  if (location?.filename && Number(location?.line || 0) > 0) {
    if (await openPreviewLocation(location)) return;
  }
  const fallback = locatePreviewTextInWorkspace(payload?.text || "");
  if (fallback) await openPreviewLocation(fallback);
}

function quoteShell(value) {
  const text = String(value || "");
  if (process.platform === "win32") return `"${text.replace(/"/g, '\\"')}"`;
  return `'${text.replace(/'/g, "'\\''")}'`;
}

function terminalCommand(args) {
  return [agentPath(), ...args].map(quoteShell).join(" ");
}

function projectIdFromWorkspace() {
  const state = readWorkspaceState();
  if (state && Number(state.project_id)) return Number(state.project_id);
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath || "";
  const match = folder.match(/project-(\d+)-workspace$/);
  return match ? Number(match[1]) : 0;
}

function activeWorkspaceFolder() {
  const folders = vscode.workspace.workspaceFolders || [];
  return folders[0]?.uri?.fsPath || "";
}

function workspaceRelativePath(filePath) {
  const root = activeWorkspaceFolder();
  if (!root || !filePath) return "";
  const rel = path.relative(root, filePath).replace(/\\/g, "/");
  return rel.startsWith("../") || path.isAbsolute(rel) ? "" : rel;
}

function isSmartTeXDocument(doc) {
  if (!doc || doc.uri?.scheme !== "file") return false;
  const rel = workspaceRelativePath(doc.uri.fsPath);
  return Boolean(rel && !rel.startsWith(".smarttex/"));
}

function isIgnoredSmartTeXPath(rel) {
  const normalized = String(rel || "").replace(/\\/g, "/");
  return normalized.startsWith(".smarttex/cache/")
    || normalized.startsWith(".smarttex/sessions/")
    || normalized.startsWith(".smarttex/auto_generated/");
}

function projectFileFromUri(uri) {
  const fsPath = uri?.fsPath || "";
  const rel = workspaceRelativePath(fsPath);
  return rel && !rel.startsWith("../") && !path.isAbsolute(rel) ? rel : "";
}

function activePdfPath(uri) {
  const rel = projectFileFromUri(uri || vscode.window.activeTextEditor?.document?.uri);
  return rel && rel.toLowerCase().endsWith(".pdf") ? rel : "";
}

function scanLocalPdfFiles() {
  const root = activeWorkspaceFolder();
  const out = [];
  if (!root || !fs.existsSync(root)) return out;
  const visit = dir => {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      const rel = path.relative(root, fullPath).replace(/\\/g, "/");
      if (!rel || isIgnoredSmartTeXPath(rel)) continue;
      if (entry.isDirectory()) {
        visit(fullPath);
      } else if (entry.isFile() && rel.toLowerCase().endsWith(".pdf")) {
        out.push(rel);
      }
    }
  };
  visit(root);
  return out.sort((a, b) => a.localeCompare(b));
}

function workspaceStatePath(folder = activeWorkspaceFolder()) {
  return folder ? path.join(folder, ".smarttex", "local_workspace_state.json") : "";
}

function readWorkspaceState() {
  const statePath = workspaceStatePath();
  if (!statePath || !fs.existsSync(statePath)) return null;
  try {
    return JSON.parse(fs.readFileSync(statePath, "utf8"));
  } catch (err) {
    output?.appendLine(`Failed to read ${statePath}: ${err.message}`);
    return null;
  }
}

function commonArgs(projectId) {
  return ["--project", String(projectId), "--server", serverUrl(), "--workspace", workspaceRoot()];
}

function apiArgs(projectId) {
  return ["--project", String(projectId), "--server", serverUrl()];
}

function runAgent(args, options = {}) {
  if (options.reveal !== false) output.show(true);
  output.appendLine(`$ ${agentPath()} ${shortCommand(args)}`);
  return new Promise((resolve, reject) => {
    cp.execFile(agentPath(), args, {
      cwd: options.cwd || activeWorkspaceFolder() || undefined,
      maxBuffer: 1024 * 1024 * 32,
    }, (err, stdout, stderr) => {
      if (stdout) output.append(stdout);
      if (stderr) output.append(stderr);
      if (err) {
        reject(new Error(String(stderr || stdout || err.message).trim()));
        return;
      }
      resolve(stdout || "");
    });
  });
}

function stopRealtime() {
  clearTimeout(realtimeReconnectTimer);
  realtimeReconnectTimer = null;
  realtimeProjectId = 0;
  if (realtimeRequest) {
    try { realtimeRequest.destroy(); } catch (_) {}
    realtimeRequest = null;
  }
}

function scheduleRealtimeReconnect(projectId) {
  clearTimeout(realtimeReconnectTimer);
  if (!projectId) return;
  const delay = realtimeReconnectDelayMs;
  realtimeReconnectDelayMs = Math.min(30000, Math.round(realtimeReconnectDelayMs * 1.7));
  realtimeReconnectTimer = setTimeout(() => connectRealtime(projectId), delay);
}

function connectRealtime(projectId = projectIdFromWorkspace()) {
  const id = Number(projectId || 0);
  if (!id) {
    stopRealtime();
    return;
  }
  const token = localAccessToken();
  if (!token) {
    output?.appendLine("SmartTeX realtime: no OAuth token yet; polling fallback remains active.");
    return;
  }
  if (realtimeProjectId === id && realtimeRequest) return;
  stopRealtime();
  realtimeProjectId = id;
  const target = new URL(`/sse/projects/${id}/updates/`, `${serverUrl()}/`);
  const client = target.protocol === "https:" ? https : http;
  const request = client.request(target, {
    method: "GET",
    headers: {
      Accept: "text/event-stream",
      Authorization: `Bearer ${token}`,
    },
  }, response => {
    if (response.statusCode === 401 || response.statusCode === 403) {
      output?.appendLine(`SmartTeX realtime auth failed: HTTP ${response.statusCode}`);
      response.resume();
      return;
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      output?.appendLine(`SmartTeX realtime failed: HTTP ${response.statusCode}`);
      response.resume();
      scheduleRealtimeReconnect(id);
      return;
    }
    realtimeReconnectDelayMs = 1500;
    output?.appendLine(`SmartTeX realtime connected for project #${id}`);
    let buffer = "";
    response.setEncoding("utf8");
    response.on("data", chunk => {
      buffer += chunk;
      let idx = buffer.indexOf("\n\n");
      while (idx >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        handleRealtimeFrame(frame);
        idx = buffer.indexOf("\n\n");
      }
    });
    response.on("end", () => {
      if (realtimeProjectId === id) scheduleRealtimeReconnect(id);
    });
  });
  realtimeRequest = request;
  request.on("error", err => {
    output?.appendLine(`SmartTeX realtime error: ${err.message}`);
    if (realtimeProjectId === id) scheduleRealtimeReconnect(id);
  });
  request.end();
}

function handleRealtimeFrame(frame) {
  const dataLines = String(frame || "").split(/\r?\n/).filter(line => line.startsWith("data:"));
  if (!dataLines.length) return;
  const raw = dataLines.map(line => line.slice(5).trimStart()).join("\n");
  if (!raw) return;
  let payload = null;
  try { payload = JSON.parse(raw); } catch (_) { return; }
  handleRealtimeEvent(payload).catch(err => output?.appendLine(`SmartTeX realtime handler failed: ${err.message}`));
}

async function handleRealtimeEvent(payload) {
  const type = String(payload?.type || "");
  if (type === "connected") {
    if (payload.local_workspace) {
      lastWorkspaceStatus = {
        ...(lastWorkspaceStatus || {}),
        project_id: Number(payload.project_id || projectIdFromWorkspace()),
        workspace_id: payload.local_workspace.workspace_id || lastWorkspaceStatus?.workspace_id || "",
        server_workspace_id: payload.local_workspace.workspace_id || "",
        server_workspace_agent: payload.local_workspace.agent_id || "",
        server_workspace_expires: payload.local_workspace.expires_at || "",
        server_lease_active: Boolean(payload.local_workspace.active),
      };
      updateStatus();
    }
    return;
  }
  if (type === "local_workspace_updated") {
    await loadWorkspaceStatus(Number(payload.project_id || projectIdFromWorkspace()), { quiet: true });
    return;
  }
  if (type === "compile_updated") {
    showCompileLog(payload);
    lastCompileDiagnostics = applyCompileDiagnostics(payload);
    lastCompileState = {
      projectId: Number(payload.project_id || projectIdFromWorkspace()),
      failed: String(payload.compile_state || payload.status || "").includes("fail") || payload.status === "error",
      diagnosticCount: lastCompileDiagnostics.length,
      compileState: payload.compile_state || payload.status || "",
      runtime: "server/local",
      pdfUrl: payload.pdf_url || "",
      updatedAt: new Date().toLocaleTimeString(),
    };
    problemsProvider?.refresh();
    dashboardProvider?.refresh();
    reloadPreviewSessions();
    return;
  }
  if (type === "longdoc_updated") {
    await refreshAnnotationsQuietly();
    return;
  }
  if (type === "proposal_updated") {
    await aiChangesProvider?.load().catch(err => output?.appendLine(`AI changes refresh failed: ${err.message}`));
    dashboardProvider?.refresh();
    return;
  }
  if (type === "project_updated") {
    await Promise.all([
      historyProvider?.load().catch(err => output?.appendLine(`History refresh failed: ${err.message}`)),
      loadPdfEmbeds({ quiet: true }).catch(err => output?.appendLine(`PDF embeds refresh failed: ${err.message}`)),
      loadWorkspaceStatus(Number(payload.project_id || projectIdFromWorkspace()), { quiet: true }).catch(err => output?.appendLine(`Workspace refresh failed: ${err.message}`)),
    ]);
    reloadPreviewSessions();
  }
}

function shortCommand(args) {
  return args.map(arg => {
    const text = String(arg || "");
    return text.length > 120 ? `${text.slice(0, 117)}...` : text;
  }).join(" ");
}

function parseAgentJSON(stdout) {
  const text = String(stdout || "").trim();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    const start = text.indexOf("{");
    const end = text.lastIndexOf("}");
    if (start >= 0 && end > start) {
      return JSON.parse(text.slice(start, end + 1));
    }
    throw err;
  }
}

function isAuthError(err) {
  const message = String(err?.message || err || "").toLowerCase();
  return message.includes("no oauth login")
    || message.includes("oauth token expired")
    || message.includes("refresh failed")
    || message.includes("run `smarttex-local")
    || message.includes("http 401")
    || message.includes("http 403")
    || message.includes("unauthorized")
    || message.includes("forbidden")
    || message.includes("authentication credentials")
    || message.includes("invalid token");
}

function authErrorMessage(err) {
  const message = String(err?.message || err || "").trim();
  if (!message) return "SmartTeX authorization is required.";
  if (message.length > 260) return `${message.slice(0, 257)}...`;
  return message;
}

async function runLoginFlow() {
  if (!authLoginPromise) {
    authLoginPromise = vscode.window.withProgress({
      location: vscode.ProgressLocation.Notification,
      title: "Signing in to SmartTeX",
      cancellable: false,
    }, () => runAgent(["login", "--server", serverUrl()], { reveal: true }))
      .finally(() => {
        authLoginPromise = null;
      });
  }
  await authLoginPromise;
  connectRealtime(projectIdFromWorkspace());
  settingsProvider?.refreshRuntime().catch(() => {});
}

async function recoverAuthAndRetry(err, retry) {
  const choice = await vscode.window.showWarningMessage(
    `SmartTeX authorization needs attention: ${authErrorMessage(err)}`,
    "Login and retry",
    "Login",
    "Show Output",
  );
  if (choice === "Show Output") {
    output?.show(true);
    return undefined;
  }
  if (choice !== "Login" && choice !== "Login and retry") return undefined;
  try {
    await runLoginFlow();
    if (choice === "Login and retry" && typeof retry === "function") {
      return await retry();
    }
  } catch (retryErr) {
    const message = String(retryErr?.message || retryErr || "Unknown SmartTeX error");
    output?.appendLine(`SmartTeX auth recovery failed: ${message}`);
    const next = await vscode.window.showWarningMessage(`SmartTeX login/retry failed: ${authErrorMessage(retryErr)}`, "Show Output");
    if (next === "Show Output") output?.show(true);
  }
  return undefined;
}

function isSmartTeXWorkspace() {
  return Boolean(readWorkspaceState()?.project_id);
}

async function askProjectId() {
  const existing = projectIdFromWorkspace();
  const value = await vscode.window.showInputBox({
    title: "SmartTeX project id",
    prompt: "Enter the project id to open or control locally.",
    value: existing ? String(existing) : "",
    validateInput: text => Number(text) > 0 ? null : "Project id must be a positive number.",
  });
  if (!value) return 0;
  return Number(value);
}

function parseProjectList(stdout) {
  return String(stdout || "")
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => /^\d+\t/.test(line))
    .map(line => {
      const [id, markupType, mainFile, ...titleParts] = line.split("\t");
      return {
        id: Number(id),
        markupType: markupType || "",
        mainFile: mainFile || "",
        title: titleParts.join("\t") || `Project #${id}`,
      };
    })
    .filter(project => project.id > 0);
}

async function loadProjectsForPick() {
  const stdout = await runAgent(["projects", "--server", serverUrl()], { reveal: false });
  return parseProjectList(stdout);
}

async function pickProjectId() {
  let projects = [];
  try {
    projects = await vscode.window.withProgress({
      location: vscode.ProgressLocation.Window,
      title: "Loading SmartTeX projects...",
    }, loadProjectsForPick);
  } catch (err) {
    output?.appendLine(`SmartTeX projects list failed: ${err.message}`);
    if (isAuthError(err)) {
      const recovered = await recoverAuthAndRetry(err, async () => vscode.window.withProgress({
        location: vscode.ProgressLocation.Window,
        title: "Loading SmartTeX projects...",
      }, loadProjectsForPick));
      if (Array.isArray(recovered)) {
        projects = recovered;
      } else if (recovered === undefined) {
        return 0;
      }
    }
  }
  if (!projects.length) return askProjectId();
  const manualItem = {
    label: "$(edit) Open by project ID...",
    description: "Fallback",
    projectId: 0,
  };
  const picked = await vscode.window.showQuickPick([
    ...projects.map(project => ({
      label: `$(repo) ${project.title}`,
      description: `#${project.id}${project.markupType ? ` · ${project.markupType}` : ""}`,
      detail: project.mainFile || "No main file configured",
      projectId: project.id,
    })),
    manualItem,
  ], {
    title: "Open SmartTeX project locally",
    placeHolder: "Choose a project from your SmartTeX account",
    matchOnDescription: true,
    matchOnDetail: true,
  });
  if (!picked) return 0;
  return picked.projectId || askProjectId();
}

function showTerminal(name, args) {
  const terminal = vscode.window.createTerminal({ name });
  terminal.show();
  terminal.sendText(terminalCommand(args));
  return terminal;
}

function parseWorkspaceReady(stdout) {
  const match = String(stdout || "").match(/SmartTeX workspace ready:\s*(.+)$/m);
  return match ? match[1].trim() : "";
}

function isWorkspaceNotActiveError(err) {
  return String(err?.message || err || "").includes("LOCAL_WORKSPACE_NOT_ACTIVE");
}

function isUnsyncedWorkspaceError(err) {
  return String(err?.message || err || "").includes("unsynced change");
}

function isLocalWorkspaceLockedError(err) {
  const message = String(err?.message || err || "");
  return message.includes("PROJECT_LOCKED") && message.includes("local_workspace");
}

async function openWorkspaceWithProgress(projectId, args, title) {
  return vscode.window.withProgress({
    location: vscode.ProgressLocation.Notification,
    title,
  }, () => runAgent(args));
}

async function openProject() {
  const projectId = await pickProjectId();
  if (!projectId) return;
  let args = ["workspace", "open", ...commonArgs(projectId)];
  let stdout = "";
  try {
    stdout = await openWorkspaceWithProgress(projectId, args, `Opening SmartTeX project #${projectId}`);
  } catch (err) {
    if (isLocalWorkspaceLockedError(err)) {
      const choice = await vscode.window.showWarningMessage(
        "This project already has an active local workspace lease, probably from a previous VS Code window.",
        "Take over",
        "Cancel",
      );
      if (choice !== "Take over") return;
      await runAgent(["workspace", "release", ...commonArgs(projectId)]);
      args = [...args, "--force"];
      stdout = await openWorkspaceWithProgress(projectId, args, `Taking over SmartTeX project #${projectId}`);
    } else
    if (!isUnsyncedWorkspaceError(err)) throw err;
    else {
      const choice = await vscode.window.showWarningMessage(
        "This SmartTeX workspace has unsynced local changes.",
        "Sync first",
        "Force open",
        "Cancel",
      );
      if (choice === "Sync first") {
        try {
          await runAgent(["workspace", "sync", ...commonArgs(projectId)]);
          stdout = await openWorkspaceWithProgress(projectId, args, `Opening SmartTeX project #${projectId}`);
        } catch (syncErr) {
          if (!isWorkspaceNotActiveError(syncErr)) throw syncErr;
          const expiredChoice = await vscode.window.showWarningMessage(
            "The previous local workspace lease is no longer active, so those local changes cannot be synced as-is.",
            "Discard local copy and reopen",
            "Cancel",
          );
          if (expiredChoice !== "Discard local copy and reopen") return;
          args = [...args, "--force"];
          stdout = await openWorkspaceWithProgress(projectId, args, `Reopening SmartTeX project #${projectId}`);
        }
      } else if (choice === "Force open") {
        args = [...args, "--force"];
        stdout = await openWorkspaceWithProgress(projectId, args, `Force opening SmartTeX project #${projectId}`);
      } else {
        return;
      }
    }
  }
  const folder = parseWorkspaceReady(stdout) || path.join(workspaceRoot(), `project-${projectId}-workspace`);
  await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(folder), false);
  if (autoWatchAfterOpen()) {
    showTerminal(`SmartTeX watch #${projectId}`, ["workspace", "watch", ...commonArgs(projectId)]);
  }
}

async function runWorkspaceCommand(name, subcommand, opts = {}) {
  const projectId = await askProjectId();
  if (!projectId) return;
  const args = ["workspace", subcommand, ...commonArgs(projectId)];
  if (opts.force) args.push("--force");
  try {
    await vscode.window.withProgress({
      location: vscode.ProgressLocation.Notification,
      title: name,
    }, () => runAgent(args));
  } catch (err) {
    const message = String(err?.message || err || "");
    if (!message.includes("unsynced change") || subcommand !== "pull") throw err;
    const choice = await vscode.window.showWarningMessage(
      "This SmartTeX workspace has unsynced local changes.",
      "Sync first",
      "Force pull",
      "Cancel",
    );
    if (choice === "Sync first") {
      try {
        await runAgent(["workspace", "sync", ...commonArgs(projectId)]);
      } catch (syncErr) {
        if (!isWorkspaceNotActiveError(syncErr)) throw syncErr;
        const expiredChoice = await vscode.window.showWarningMessage(
          "The previous local workspace lease is no longer active, so those local changes cannot be synced as-is.",
          "Force pull",
          "Cancel",
        );
        if (expiredChoice !== "Force pull") return;
        await runAgent([...args, "--force"]);
        vscode.window.showInformationMessage(`${name}: done`);
        await loadWorkspaceStatus(projectId, { quiet: true });
        updateStatus();
        await historyProvider?.load().catch(() => {});
        return;
      }
      await runAgent(args);
    } else if (choice === "Force pull") {
      await runAgent([...args, "--force"]);
    } else {
      return;
    }
  }
  vscode.window.showInformationMessage(`${name}: done`);
  await loadWorkspaceStatus(projectId, { quiet: true });
  updateStatus();
  await historyProvider?.load().catch(() => {});
}

async function syncCurrentWorkspaceQuietly(reason = "save") {
  if (!syncOnSave()) return;
  const state = readWorkspaceState();
  const projectId = Number(state?.project_id || 0);
  if (!projectId || syncInFlight) return;
  syncInFlight = true;
  updateStatus("syncing");
  try {
	    await runAgent(["workspace", "sync", ...commonArgs(projectId)], { reveal: false });
	    output.appendLine(`SmartTeX sync after ${reason}: done`);
	    await loadWorkspaceStatus(projectId, { quiet: true });
	    await loadPdfEmbeds({ quiet: true });
	    await annotationsProvider?.load();
	    await historyProvider?.load().catch(() => {});
	    // Do not force a tinymist restart / iframe reload here: the file is
	    // already on disk and tinymist's own file watcher pushes a live,
	    // incremental preview update over the data WebSocket. Reloading would
	    // only add latency and reset the preview scroll position.
  } catch (err) {
    await loadWorkspaceStatus(projectId, { quiet: true });
    if (isWorkspaceNotActiveError(err)) {
      const now = Date.now();
      if (now - lastInactiveWorkspacePromptAt > 15000) {
        lastInactiveWorkspacePromptAt = now;
        const choice = await vscode.window.showWarningMessage(
          "SmartTeX local workspace is no longer active on the server. Reconnect it before syncing changes.",
          "Reconnect workspace",
          "Show Output",
        );
        if (choice === "Reconnect workspace") {
          await runWorkspaceCommand("SmartTeX reconnect", "pull");
        } else if (choice === "Show Output") {
          output?.show(true);
        }
      }
    } else {
      vscode.window.showWarningMessage(`SmartTeX sync failed: ${err.message}`);
    }
  } finally {
    syncInFlight = false;
    updateStatus();
  }
}

function queueSyncCurrentWorkspace(reason = "save") {
  if (!isSmartTeXWorkspace()) return;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(() => {
    syncCurrentWorkspaceQuietly(reason).catch(err => {
      output?.appendLine(`SmartTeX sync failed: ${err.message}`);
    });
  }, 350);
}

function queueAutoSaveAndSync(doc) {
  if (!autoSaveAndSync() || !isSmartTeXWorkspace() || !isSmartTeXDocument(doc)) return;
  clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(async () => {
    if (!doc.isDirty) return;
    autoSaveInProgress = true;
    updateStatus("syncing");
    try {
      const saved = await doc.save();
      if (saved) await syncCurrentWorkspaceQuietly("auto-save");
    } catch (err) {
      vscode.window.showWarningMessage(`SmartTeX auto-save failed: ${err.message}`);
    } finally {
      autoSaveInProgress = false;
      updateStatus();
    }
  }, autoSaveDebounceMs());
}

async function workspaceStatus() {
  const projectId = projectIdFromWorkspace() || await askProjectId();
  if (!projectId) return;
  const status = await loadWorkspaceStatus(projectId, { quiet: false });
  const label = workspaceStatusLabel(status);
  const details = [
    status?.local_unsynced_changes ? `${status.local_unsynced_changes} unsynced` : "synced",
    status?.server_latest_version && status?.local_base_version && Number(status.server_latest_version) !== Number(status.local_base_version)
      ? `server v${status.server_latest_version}`
      : "",
  ].filter(Boolean).join(" · ");
  vscode.window.showInformationMessage(`SmartTeX workspace: ${label}${details ? ` (${details})` : ""}`);
  updateStatus();
}

async function loadWorkspaceStatus(projectId = projectIdFromWorkspace(), options = {}) {
  const id = Number(projectId || 0);
  if (!id || workspaceStatusInFlight) return lastWorkspaceStatus;
  workspaceStatusInFlight = true;
  try {
    const stdout = await runAgent(["workspace", "status", ...commonArgs(id), "--json"], { reveal: options.quiet !== false ? false : true });
    const status = parseAgentJSON(stdout) || {};
    lastWorkspaceStatus = {
      project_id: id,
      ...status,
      local_unsynced_changes: Number(status.local_unsynced_changes || 0),
      local_base_version: Number(status.local_base_version || 0),
      server_latest_version: Number(status.server_latest_version || 0),
      server_lease_active: Boolean(status.server_lease_active),
    };
    dashboardProvider?.refresh();
    updateStatus();
    connectRealtime(id);
    return lastWorkspaceStatus;
  } catch (err) {
    if (options.quiet === false) throw err;
    output?.appendLine(`SmartTeX workspace status failed: ${err.message}`);
    return lastWorkspaceStatus;
  } finally {
    workspaceStatusInFlight = false;
  }
}

function workspaceStatusLabel(status = lastWorkspaceStatus) {
  const state = readWorkspaceState();
  if (!state?.project_id) return "No workspace";
  if (!status) return "Checking";
  if (!status.workspace_id) return "Not opened";
  if (!status.server_lease_active) return "Lease expired";
  if (status.local_unsynced_changes > 0) return "Unsynced";
  if (status.server_latest_version && status.local_base_version && status.server_latest_version > status.local_base_version) return "Server ahead";
  return "Connected";
}

function workspaceStatusClass(status = lastWorkspaceStatus) {
  const label = workspaceStatusLabel(status);
  if (label === "Connected") return "ok";
  if (label === "Unsynced" || label === "Server ahead" || label === "Checking") return "warn";
  if (label === "No workspace") return "idle";
  return "bad";
}

function workspaceStatusDetail(status = lastWorkspaceStatus) {
  const state = readWorkspaceState();
  if (!state?.project_id) return "Open a project to start local editing.";
  if (!status) return "Status will appear after refresh.";
  if (!status.workspace_id) return "This folder has no active SmartTeX local workspace id.";
  if (!status.server_lease_active) return "The server no longer recognizes this local editor session. Reconnect before syncing.";
  if (status.local_unsynced_changes > 0) return `${status.local_unsynced_changes} local change(s) are waiting to sync.`;
  if (status.server_latest_version && status.local_base_version && status.server_latest_version > status.local_base_version) {
    return `Server is at v${status.server_latest_version}, local base is v${status.local_base_version}.`;
  }
  return "Local workspace is connected and ready.";
}

async function loadPdfEmbeds(options = {}) {
  const projectId = projectIdFromWorkspace();
  if (!projectId || pdfEmbedsLoading) return lastPdfEmbeds;
  pdfEmbedsLoading = true;
  pdfEmbedsError = "";
  pdfEmbedsProvider?.refresh();
  try {
    const stdout = await runAgent(["pdf-embed", "list", ...apiArgs(projectId)], { reveal: options.quiet !== false ? false : true });
    const payload = parseAgentJSON(stdout) || {};
    lastPdfEmbeds = payload.embeds || {};
    pdfEmbedsError = "";
    pdfEmbedsProvider?.refresh();
    return lastPdfEmbeds;
  } catch (err) {
    pdfEmbedsError = err.message;
    pdfEmbedsProvider?.refresh();
    if (options.quiet === false) throw err;
    output?.appendLine(`PDF embeds refresh failed: ${err.message}`);
    return lastPdfEmbeds;
  } finally {
    pdfEmbedsLoading = false;
    pdfEmbedsProvider?.refresh();
  }
}

async function refreshPdfEmbeds() {
  await loadPdfEmbeds({ quiet: false });
  vscode.window.showInformationMessage("SmartTeX PDF embeds refreshed");
}

async function setPdfEmbedForPath(filePath, enabled) {
  const projectId = projectIdFromWorkspace() || await askProjectId();
  const pdfPath = String(filePath || "").trim();
  if (!projectId || !pdfPath) return;
  await runAgent(["pdf-embed", "set", ...apiArgs(projectId), "--file", pdfPath, "--enabled", enabled ? "true" : "false"], { reveal: false });
  await loadPdfEmbeds({ quiet: true });
  await loadWorkspaceStatus(projectId, { quiet: true });
  historyProvider?.load().catch(() => {});
  const action = enabled ? "enabled" : "disabled";
  const choices = enabled ? ["Insert snippet", "Recompile"] : ["Recompile"];
  const choice = await vscode.window.showInformationMessage(`PDF embed ${action}: ${pdfPath}`, ...choices);
  if (choice === "Insert snippet") await insertPdfEmbedSnippetForPath(pdfPath);
  if (choice === "Recompile") await compileProject();
}

async function enablePdfEmbed(uriOrPath) {
  const pdfPath = typeof uriOrPath === "string" ? uriOrPath : activePdfPath(uriOrPath);
  if (!pdfPath) {
    vscode.window.showWarningMessage("Choose a PDF file inside the SmartTeX workspace.");
    return;
  }
  await setPdfEmbedForPath(pdfPath, true);
}

async function disablePdfEmbed(uriOrPath) {
  const pdfPath = typeof uriOrPath === "string" ? uriOrPath : activePdfPath(uriOrPath);
  if (!pdfPath) {
    vscode.window.showWarningMessage("Choose a PDF file inside the SmartTeX workspace.");
    return;
  }
  await setPdfEmbedForPath(pdfPath, false);
}

async function insertPdfEmbedSnippet(uriOrPath) {
  const pdfPath = typeof uriOrPath === "string" ? uriOrPath : activePdfPath(uriOrPath);
  if (!pdfPath) {
    vscode.window.showWarningMessage("Choose a PDF file inside the SmartTeX workspace.");
    return;
  }
  await insertPdfEmbedSnippetForPath(pdfPath);
}

async function insertPdfEmbedSnippetForPath(pdfPath) {
  let editor = vscode.window.activeTextEditor;
  if (!editor || !isSmartTeXDocument(editor.document) || editor.document.uri.fsPath.toLowerCase().endsWith(".pdf")) {
    const root = activeWorkspaceFolder();
    const state = readWorkspaceState();
    const mainFile = String(state?.main_file || state?.project_main_file_field || "");
    const candidates = [
      mainFile ? path.join(root, filepathFromProjectPath(mainFile)) : "",
      ...vscode.workspace.textDocuments.map(doc => doc.uri.fsPath).filter(file => isSmartTeXDocument({ uri: vscode.Uri.file(file) }) && /\.(typ|typst|md|txt)$/i.test(file)),
    ].filter(Boolean);
    const target = candidates.find(file => fs.existsSync(file)) || "";
    if (target) {
      const document = await vscode.workspace.openTextDocument(vscode.Uri.file(target));
      editor = await vscode.window.showTextDocument(document, { preview: false, viewColumn: lastTextEditorViewColumn || vscode.ViewColumn.One });
    }
  }
  if (!editor) {
    vscode.window.showWarningMessage("Open a text/Typst file first, then insert the PDF embed snippet.");
    return;
  }
  const snippet = `#smarttex-include-pdf("${pdfPath}")`;
  await editor.insertSnippet(new vscode.SnippetString(snippet));
}

function diagnosticSeverity(value) {
  const normalized = String(value || "").toLowerCase();
  if (normalized === "warning" || normalized === "warn") return vscode.DiagnosticSeverity.Warning;
  if (normalized === "info" || normalized === "information") return vscode.DiagnosticSeverity.Information;
  if (normalized === "hint") return vscode.DiagnosticSeverity.Hint;
  return vscode.DiagnosticSeverity.Error;
}

function normalizeCompileDiagnostic(item) {
  if (!item || typeof item !== "object") return null;
  const file = String(item.file || item.filename || item.path || "").replace(/^file:\/\//, "");
  const line = Math.max(1, Number(item.line || item.line_start || item.row || 1));
  const column = Math.max(1, Number(item.column || item.character || item.col || 1));
  const message = String(item.message || item.detail || item.text || "SmartTeX diagnostic").trim();
  if (!file || !message) return null;
  return {
    file,
    line,
    column,
    severity: item.severity || "error",
    message,
  };
}

function parseTypstDiagnosticsFromLog(logText) {
  const diagnostics = [];
  const lines = String(logText || "").split(/\r?\n/);
  let pendingMessage = "";
  for (const line of lines) {
    const messageMatch = line.match(/^(error|warning):\s*(.+)$/i);
    if (messageMatch) {
      pendingMessage = messageMatch[2].trim();
      continue;
    }
    const locationMatch = line.match(/[┌╭]\S*\s*[─-]\s+(.+?):(\d+):(\d+)/);
    if (!locationMatch) continue;
    diagnostics.push({
      file: locationMatch[1].trim(),
      line: Number(locationMatch[2] || 1),
      column: Number(locationMatch[3] || 1),
      severity: pendingMessage.toLowerCase().startsWith("unknown font") ? "warning" : "error",
      message: pendingMessage || "Typst compile diagnostic",
    });
    pendingMessage = "";
  }
  return diagnostics;
}

function applyCompileDiagnostics(payload) {
  if (!compileDiagnostics) return [];
  const structured = Array.isArray(payload?.diagnostics) ? payload.diagnostics.map(normalizeCompileDiagnostic).filter(Boolean) : [];
  const diagnostics = structured.length ? structured : parseTypstDiagnosticsFromLog(payload?.log || "");
  const grouped = new Map();
  const resolvedDiagnostics = [];
  const root = activeWorkspaceFolder();
  for (const item of diagnostics) {
    const rel = resolvePreviewFilename(item.file);
    const fullPath = path.isAbsolute(rel) ? rel : path.join(root, filepathFromProjectPath(rel));
    if (!root || !fs.existsSync(fullPath)) continue;
    const uri = vscode.Uri.file(fullPath);
    const range = new vscode.Range(Math.max(0, item.line - 1), Math.max(0, item.column - 1), Math.max(0, item.line - 1), Math.max(0, item.column));
    const diag = new vscode.Diagnostic(range, item.message, diagnosticSeverity(item.severity));
    diag.source = "SmartTeX";
    resolvedDiagnostics.push({
      file: rel,
      fullPath,
      line: item.line,
      column: item.column,
      severity: String(item.severity || "error"),
      message: item.message,
    });
    const key = uri.toString();
    const bucket = grouped.get(key) || { uri, diagnostics: [] };
    bucket.diagnostics.push(diag);
    grouped.set(key, bucket);
  }
  compileDiagnostics.clear();
  for (const bucket of grouped.values()) {
    compileDiagnostics.set(bucket.uri, bucket.diagnostics);
  }
  return resolvedDiagnostics;
}

function showCompileLog(payload) {
  compileOutput?.clear();
  if (payload && Object.prototype.hasOwnProperty.call(payload, "log")) {
    lastCompileLog = payload.log || "";
  }
  compileOutput?.appendLine(`SmartTeX compile: ${payload?.compile_state || payload?.status || "unknown"}`);
  if (payload?.runtime) compileOutput?.appendLine(`runtime=${payload.runtime}`);
  if (payload?.pdf_url) compileOutput?.appendLine(`pdf=${payload.pdf_url}`);
  compileOutput?.appendLine("");
  compileOutput?.appendLine(payload?.log || "No compile log.");
}

function compileStateLabel(state = lastCompileState) {
  if (!state) return "Not compiled yet";
  if (state.failed) return "Failed";
  if (state.diagnosticCount > 0) return `${state.diagnosticCount} problem${state.diagnosticCount === 1 ? "" : "s"}`;
  return "Ready";
}

function compileStateClass(state = lastCompileState) {
  if (!state) return "idle";
  if (state.failed) return "bad";
  if (state.diagnosticCount > 0) return "warn";
  return "ok";
}

function compileStateDetail(state = lastCompileState) {
  if (!state) return "Run compile to populate VS Code Problems and the SmartTeX compile log.";
  const parts = [
    state.runtime ? `runtime ${state.runtime}` : "",
    state.compileState ? `state ${state.compileState}` : "",
    state.updatedAt ? `at ${state.updatedAt}` : "",
  ].filter(Boolean);
  return parts.join(" · ") || "Last compile result is available.";
}

async function showProblems() {
  await vscode.commands.executeCommand("workbench.actions.view.problems");
}

async function compileProject() {
  const projectId = await askProjectId();
  if (!projectId) return;
  const stdout = await vscode.window.withProgress({
    location: vscode.ProgressLocation.Notification,
    title: `Compiling SmartTeX project #${projectId}`,
  }, () => runAgent(["compile", ...commonArgs(projectId)], { reveal: false }));
  const payload = parseAgentJSON(stdout) || {};
  showCompileLog(payload);
  const diagnostics = applyCompileDiagnostics(payload);
  lastCompileDiagnostics = diagnostics;
  const failed = String(payload.compile_state || payload.status || "").includes("fail") || payload.status === "error";
  lastCompileState = {
    projectId,
    failed,
    diagnosticCount: diagnostics.length,
    compileState: payload.compile_state || payload.status || "",
    runtime: payload.runtime || "",
    pdfUrl: payload.pdf_url || "",
    updatedAt: new Date().toLocaleTimeString(),
  };
  if (failed || diagnostics.length) {
    compileOutput?.show(true);
    vscode.window.showWarningMessage(`SmartTeX compile finished with ${diagnostics.length || 1} problem(s).`);
  } else {
    vscode.window.showInformationMessage(`SmartTeX project #${projectId} compiled`);
  }
  await loadPdfEmbeds({ quiet: true });
  dashboardProvider?.refresh();
  problemsProvider?.refresh();
  updateStatus();
}

async function openWebEditor() {
  const projectId = await askProjectId();
  if (!projectId) return;
  vscode.env.openExternal(vscode.Uri.parse(`${serverUrl()}/projects/${projectId}/`));
}

async function startLocalAgent() {
  // serve must watch the SAME workspace root the extension opens projects into.
  // Otherwise (notably in dev mode, where the root is ~/.smarttex-local-dev) the
  // preview watches a different copy of the files than the one being edited, so
  // saves never reach tinymist and the preview appears frozen/stale.
  showTerminal("SmartTeX local agent", ["serve", "--server", serverUrl(), "--workspace", workspaceRoot()]);
}

async function checkLocalBridge(secret) {
  if (typeof fetch !== "function") return false;
  try {
    const response = await fetch(`${localBridgeUrl()}/v1/health`, {
      headers: { "X-SmartTeX-Local-Secret": secret },
    });
    return response.ok;
  } catch (_) {
    return false;
  }
}

function previewHtml(projectId, secret, initialState = {}) {
  const nonce = String(Date.now());
  const bridge = localBridgeUrl();
  const initialTheme = initialState.theme || previewTheme();
  const initialFollow = initialState.follow !== false;
  const initialClick = initialState.click !== false;
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${bridge}; connect-src ${bridge} ws://127.0.0.1:* ws://localhost:*; img-src ${bridge} data: blob:; style-src 'nonce-${nonce}' 'unsafe-inline'; script-src 'nonce-${nonce}' 'unsafe-eval' ${bridge};">
  <style nonce="${nonce}">
    html, body { width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden; background: #111827; color: #e5e7eb; }
    .bar { height: 38px; display: flex; align-items: center; gap: 10px; padding: 0 12px; border-bottom: 1px solid rgba(255,255,255,.12); font: 12px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .dot { width: 8px; height: 8px; border-radius: 999px; background: #22c55e; box-shadow: 0 0 16px rgba(34,197,94,.8); }
    .spacer { flex: 1; }
    button { border: 1px solid rgba(255,255,255,.18); color: #e5e7eb; background: rgba(255,255,255,.08); border-radius: 7px; padding: 4px 9px; cursor: pointer; }
    button:hover { background: rgba(255,255,255,.14); }
    button.active { border-color: rgba(34,197,94,.55); background: rgba(34,197,94,.18); color: #bbf7d0; }
    .group { display: inline-flex; gap: 4px; padding-left: 8px; border-left: 1px solid rgba(255,255,255,.12); }
    iframe { width: 100%; height: calc(100% - 38px); border: 0; background: white; }
    a { color: #93c5fd; }
  </style>
</head>
<body>
  <div class="bar">
    <span class="dot"></span><span>Project #${projectId}</span><span style="opacity:.65">${bridge}</span>
    <span class="spacer"></span>
    <button id="followBtn" title="Follow VS Code cursor">Follow</button>
    <button id="clickBtn" title="Click preview text to open source">Click→Code</button>
    <button id="revealBtn" title="Reveal current cursor now">Reveal</button>
    <span class="group">
      <button data-theme="auto">Auto</button>
      <button data-theme="light">Light</button>
      <button data-theme="dark">Dark</button>
    </span>
    <button id="reloadBtn">Reload</button>
  </div>
  <iframe id="previewFrame" allow="clipboard-read; clipboard-write" title="SmartTeX Local Preview"></iframe>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const bridge = ${JSON.stringify(bridge)};
    const projectId = ${JSON.stringify(projectId)};
    const secret = ${JSON.stringify(secret)};
    let state = { theme: ${JSON.stringify(initialTheme)}, follow: ${JSON.stringify(initialFollow)}, click: ${JSON.stringify(initialClick)} };
    const frame = document.getElementById("previewFrame");
    function themeParam(theme) {
      if (theme === "dark") return "always";
      if (theme === "light") return "never";
      return "auto";
    }
    function previewUrl() {
      const params = new URLSearchParams({ project_id: String(projectId), secret, theme: themeParam(state.theme), t: String(Date.now()) });
      return bridge + "/v1/preview/?" + params.toString();
    }
    function previewRefreshUrl() {
      const params = new URLSearchParams({ project_id: String(projectId), secret, theme: themeParam(state.theme), t: String(Date.now()) });
      return bridge + "/v1/preview/refresh?" + params.toString();
    }
    function previewControlUrl() {
      const url = new URL(bridge);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      url.pathname = "/ws/typst-preview/control/";
      url.search = "";
      url.searchParams.set("project_id", String(projectId));
      url.searchParams.set("secret", secret);
      url.searchParams.set("theme", themeParam(state.theme));
      return url.toString();
    }
    let previewRootUri = "";
    let controlWs = null;
    let controlReconnectTimer = null;
    let lastOfficialJumpAt = 0;
    let pendingScrollCapture = null;
    let pendingRestoreScroll = null;
    let refreshInFlight = false;
    let refreshQueued = false;
    function renderButtons() {
      document.getElementById("followBtn").classList.toggle("active", Boolean(state.follow));
      document.getElementById("clickBtn").classList.toggle("active", Boolean(state.click));
      document.querySelectorAll("[data-theme]").forEach(btn => btn.classList.toggle("active", btn.dataset.theme === state.theme));
    }
    function reloadPreview(options = {}) {
      if (options.restoreScroll) pendingRestoreScroll = options.restoreScroll;
      frame.src = previewUrl();
      renderButtons();
    }
    function requestScrollState() {
      return new Promise(resolve => {
        const key = String(Date.now()) + Math.random().toString(36).slice(2);
        const timer = setTimeout(() => {
          if (pendingScrollCapture?.key === key) pendingScrollCapture = null;
          resolve(null);
        }, 180);
        pendingScrollCapture = {
          key,
          resolve: state => {
            clearTimeout(timer);
            pendingScrollCapture = null;
            resolve(state);
          },
        };
        try {
          frame.contentWindow?.postMessage({ type: "smarttex-preview-capture-scroll", key }, bridge);
        } catch (_) {
          clearTimeout(timer);
          pendingScrollCapture = null;
          resolve(null);
        }
      });
    }
    async function refreshPreview() {
      if (refreshInFlight) {
        refreshQueued = true;
        return;
      }
      refreshInFlight = true;
      try {
        let restarted = true;
        try {
          const resp = await fetch(previewRefreshUrl(), {
            method: "POST",
            headers: { "X-SmartTeX-Local-Secret": secret },
          });
          if (resp && resp.ok) {
            const data = await resp.json().catch(() => null);
            if (data && typeof data.restarted === "boolean") restarted = data.restarted;
          }
        } catch (_) {}
        // Only reload the iframe when the preview process was actually rebuilt
        // (a server-snapshot re-pull). For a live local workspace tinymist has
        // already streamed the update over its data WebSocket, so reloading
        // would needlessly blank the view and reset the scroll position.
        if (restarted) {
          const scroll = await requestScrollState();
          reloadPreview({ restoreScroll: scroll });
        }
      } finally {
        refreshInFlight = false;
        if (refreshQueued) {
          refreshQueued = false;
          setTimeout(refreshPreview, 350);
        }
      }
    }
    function postReveal(payload) {
      try { frame.contentWindow?.postMessage({ type: "smarttex-preview-reveal", payload }, bridge); } catch (_) {}
    }
    function controlConnected() {
      return controlWs && controlWs.readyState === WebSocket.OPEN;
    }
    function sendControl(payload) {
      if (!controlConnected()) return false;
      try {
        controlWs.send(JSON.stringify(payload));
        return true;
      } catch (_) {
        return false;
      }
    }
    function absolutePreviewFilepath(filename) {
      const relative = String(filename || "");
      if (!previewRootUri || !relative) return "";
      if (relative.startsWith("file://")) {
        try { return decodeURIComponent(new URL(relative).pathname); } catch (_) { return relative.replace(/^file:\\/\\//, ""); }
      }
      const base = previewRootUri.endsWith("/") ? previewRootUri : previewRootUri + "/";
      try { return decodeURIComponent(new URL(relative, base).pathname); } catch (_) { return ""; }
    }
    function revealOfficial(payload, force) {
      const filepath = absolutePreviewFilepath(payload?.filename || "");
      if (!filepath) return false;
      const line = Math.max(0, Number(payload.lineNumber || 1) - 1);
      const character = Math.max(0, Number(payload.columnNumber || 1) - 1);
      const cursorSent = sendControl({ event: "changeCursorPosition", filepath, line, character });
      const scrollSent = (force || state.follow) ? sendControl({ event: "panelScrollTo", filepath, line, character }) : false;
      return cursorSent || scrollSent;
    }
    function connectControl() {
      clearTimeout(controlReconnectTimer);
      if (controlWs && (controlWs.readyState === WebSocket.CONNECTING || controlWs.readyState === WebSocket.OPEN)) return;
      try {
        const ws = new WebSocket(previewControlUrl());
        controlWs = ws;
        ws.onopen = () => vscode.postMessage({ type: "control-ready" });
        ws.onmessage = event => {
          let data = null;
          try { data = JSON.parse(event.data); } catch (_) { return; }
          if (data?.event === "editorScrollTo") lastOfficialJumpAt = Date.now();
          vscode.postMessage({ type: "control-message", payload: data });
        };
        ws.onclose = () => {
          if (controlWs === ws) controlWs = null;
          controlReconnectTimer = setTimeout(connectControl, 1200);
        };
        ws.onerror = () => {};
      } catch (_) {
        controlReconnectTimer = setTimeout(connectControl, 1200);
      }
    }
    document.getElementById("followBtn").addEventListener("click", () => {
      state.follow = !state.follow;
      renderButtons();
      vscode.postMessage({ type: "state", state });
    });
    document.getElementById("clickBtn").addEventListener("click", () => {
      state.click = !state.click;
      renderButtons();
      vscode.postMessage({ type: "state", state });
    });
    document.getElementById("revealBtn").addEventListener("click", () => vscode.postMessage({ type: "reveal-request" }));
    document.getElementById("reloadBtn").addEventListener("click", reloadPreview);
    document.querySelectorAll("[data-theme]").forEach(btn => btn.addEventListener("click", () => {
      state.theme = btn.dataset.theme;
      vscode.postMessage({ type: "state", state });
      reloadPreview();
    }));
    window.addEventListener("message", event => {
      if (event.source === frame.contentWindow) {
        const data = event.data || {};
        if (data.type === "smarttex-preview-click" && state.click) {
          const startedAt = Date.now();
          setTimeout(() => {
            if (lastOfficialJumpAt > startedAt) return;
            vscode.postMessage({ type: "preview-click", payload: data.payload || {} });
          }, controlConnected() ? 180 : 0);
        }
        if (data.type === "smarttex-preview-ready") {
          previewRootUri = String(data.rootUri || "");
          vscode.postMessage({ type: "preview-ready", rootUri: previewRootUri });
          if (pendingRestoreScroll) {
            const scroll = pendingRestoreScroll;
            pendingRestoreScroll = null;
            setTimeout(() => postPreviewScrollRestore(scroll), 40);
            setTimeout(() => postPreviewScrollRestore(scroll), 180);
            setTimeout(() => postPreviewScrollRestore(scroll), 420);
          }
        }
        if (data.type === "smarttex-preview-scroll-state" && pendingScrollCapture && data.key === pendingScrollCapture.key) {
          pendingScrollCapture.resolve({ x: Number(data.x || 0), y: Number(data.y || 0) });
        }
        return;
      }
      const data = event.data || {};
      if (data.type === "reveal" && state.follow) {
        if (!revealOfficial(data.payload || {}, false)) postReveal(data.payload || {});
      }
      if (data.type === "force-reveal") {
        if (!revealOfficial(data.payload || {}, true)) postReveal(data.payload || {});
      }
      if (data.type === "smarttex-preview-reload") {
        reloadPreview();
      }
      if (data.type === "smarttex-preview-refresh") {
        refreshPreview();
      }
    });
    function postPreviewScrollRestore(scroll) {
      if (!scroll) return;
      try { frame.contentWindow?.postMessage({ type: "smarttex-preview-restore-scroll", x: scroll.x || 0, y: scroll.y || 0 }, bridge); } catch (_) {}
    }
    reloadPreview();
    connectControl();
  </script>
</body>
</html>`;
}

class SmartTeXPreviewProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
    this.pendingProjectId = null;
    this.session = null;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.onDidDispose(() => {
      previewPanels.delete("sidebar");
      this.view = null;
      this.session = null;
    });
    view.webview.onDidReceiveMessage(command(async message => {
      const session = this.session;
      if (!session || !message || typeof message !== "object") return;
      if (message.type === "state") {
        session.state = { ...session.state, ...(message.state || {}) };
        return;
      }
      if (message.type === "reveal-request") {
        revealPreviewSelection(true);
        return;
      }
      if (message.type === "preview-ready") {
        setTimeout(() => revealPreviewSelection(true), 180);
        return;
      }
      if (message.type === "control-ready") {
        setTimeout(() => revealPreviewSelection(true), 80);
        return;
      }
      if (message.type === "control-message") {
        await handlePreviewControlMessage(session, message.payload || {});
        return;
      }
      if (message.type === "preview-click") {
        await handlePreviewClick(message.payload || {});
      }
    }));
    if (this.pendingProjectId) {
      this.render(this.pendingProjectId);
    } else {
      this.renderEmpty();
    }
  }

  async show(projectId) {
    this.pendingProjectId = projectId;
    await vscode.commands.executeCommand("smarttex.preview.focus");
    if (this.view) this.render(projectId);
  }

  renderEmpty() {
    if (!this.view) return;
    const nonce = String(Date.now());
    this.view.webview.html = `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}';">
  <style nonce="${nonce}">
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; color: var(--vscode-descriptionForeground); background: var(--vscode-editor-background); font: 13px/1.5 var(--vscode-font-family); }
    .empty { max-width: 260px; padding: 20px; text-align: center; border: 1px dashed var(--vscode-panel-border); border-radius: 14px; }
    strong { display: block; margin-bottom: 5px; color: var(--vscode-foreground); }
  </style>
</head>
<body><div class="empty"><strong>SmartTeX Preview</strong>Open a local project preview to render it here, without occupying an editor tab.</div></body>
</html>`;
  }

  render(projectId) {
    if (!this.view) return;
    const secret = localBridgeSecret();
    const previousState = this.session?.state || {};
    const state = {
      theme: previousState.theme || previewTheme(),
      follow: previousState.follow ?? previewFollowCursor(),
      click: previousState.click ?? previewClickToCode(),
    };
    this.session = { projectId, webview: this.view.webview, state };
    previewPanels.set("sidebar", this.session);
    try {
      this.view.title = `Preview #${projectId}`;
    } catch (_) {
      // Some VS Code builds expose WebviewView.title as read-only.
    }
    this.view.webview.html = previewHtml(projectId, secret, state);
    setTimeout(() => revealPreviewSelection(true), 450);
  }
}

async function openPreview() {
  const projectId = projectIdFromWorkspace() || await askProjectId();
  if (!projectId) return;
  const secret = localBridgeSecret();
  if (!secret) {
    const choice = await vscode.window.showWarningMessage(
      "SmartTeX local bridge secret is not configured. Run login or start the local agent first.",
      "Start Local Agent",
      "Cancel",
    );
    if (choice === "Start Local Agent") await startLocalAgent();
    return;
  }
  const healthy = await checkLocalBridge(secret);
  if (!healthy) {
    const choice = await vscode.window.showWarningMessage(
      "SmartTeX local agent is not reachable. Start it to use local preview.",
      "Start Local Agent",
      "Open Anyway",
      "Cancel",
    );
    if (choice === "Start Local Agent") {
      await startLocalAgent();
      return;
    }
    if (choice !== "Open Anyway") return;
  }
  await previewProvider?.show(projectId);
}

function annotationStatusLabel(status) {
  switch (String(status || "")) {
    case "ai_draft": return "AI";
    case "open": return "Open";
    case "in_progress": return "In progress";
    case "done": return "Done";
    case "dismissed": return "Dismissed";
    default: return String(status || "Annotation");
  }
}

function annotationIcon(status) {
  switch (String(status || "")) {
    case "ai_draft": return new vscode.ThemeIcon("sparkle", new vscode.ThemeColor("charts.purple"));
    case "done": return new vscode.ThemeIcon("check", new vscode.ThemeColor("testing.iconPassed"));
    case "dismissed": return new vscode.ThemeIcon("circle-slash", new vscode.ThemeColor("testing.iconSkipped"));
    case "in_progress": return new vscode.ThemeIcon("comment-discussion", new vscode.ThemeColor("charts.yellow"));
    default: return new vscode.ThemeIcon("comment", new vscode.ThemeColor("charts.blue"));
  }
}

function createAnnotationDecorationTypes(context) {
  const iconPath = vscode.Uri.file(path.join(context.extensionPath, "assets", "annotation-open.svg"));
  annotationDecorationTypes = {
    ai_draft: vscode.window.createTextEditorDecorationType({
      isWholeLine: false,
      backgroundColor: "rgba(168, 85, 247, 0.08)",
      overviewRulerColor: "rgba(168, 85, 247, 0.85)",
      overviewRulerLane: vscode.OverviewRulerLane.Right,
      gutterIconPath: iconPath,
      gutterIconSize: "contain",
      border: "0 0 0 1px rgba(168, 85, 247, 0.55)",
    }),
    open: vscode.window.createTextEditorDecorationType({
      isWholeLine: false,
      backgroundColor: "rgba(34, 197, 94, 0.07)",
      overviewRulerColor: "rgba(34, 197, 94, 0.85)",
      overviewRulerLane: vscode.OverviewRulerLane.Right,
      gutterIconPath: iconPath,
      gutterIconSize: "contain",
      border: "0 0 0 1px rgba(34, 197, 94, 0.55)",
    }),
    in_progress: vscode.window.createTextEditorDecorationType({
      isWholeLine: false,
      backgroundColor: "rgba(245, 158, 11, 0.07)",
      overviewRulerColor: "rgba(245, 158, 11, 0.85)",
      overviewRulerLane: vscode.OverviewRulerLane.Right,
      gutterIconPath: iconPath,
      gutterIconSize: "contain",
      border: "0 0 0 1px rgba(245, 158, 11, 0.55)",
    }),
    done: vscode.window.createTextEditorDecorationType({
      isWholeLine: false,
      backgroundColor: "rgba(148, 163, 184, 0.04)",
      overviewRulerColor: "rgba(148, 163, 184, 0.45)",
      overviewRulerLane: vscode.OverviewRulerLane.Right,
      gutterIconPath: iconPath,
      gutterIconSize: "contain",
    }),
    dismissed: vscode.window.createTextEditorDecorationType({
      isWholeLine: false,
      backgroundColor: "rgba(148, 163, 184, 0.03)",
      overviewRulerColor: "rgba(148, 163, 184, 0.3)",
      overviewRulerLane: vscode.OverviewRulerLane.Right,
    }),
  };
  Object.values(annotationDecorationTypes).forEach(type => context.subscriptions.push(type));
}

function annotationHover(annotation) {
  const status = annotationStatusLabel(annotation.status);
  const md = new vscode.MarkdownString(undefined, true);
  md.supportHtml = false;
  md.appendMarkdown(`**SmartTeX annotation #${annotation.id || ""}** · ${status}\n\n`);
  md.appendMarkdown(String(annotation.instruction || "").replace(/\n/g, "  \n"));
  if (annotation.selected_text) {
    md.appendMarkdown("\n\n---\n");
    md.appendCodeblock(String(annotation.selected_text), "text");
  }
  return md;
}

function annotationDecorationForDocument(document, annotation) {
  const rel = workspaceRelativePath(document.uri.fsPath);
  if (!rel || rel !== String(annotation.file_name || "")) return null;
  const startLine = Math.max(0, Number(annotation.line_start || 1) - 1);
  if (startLine >= document.lineCount) return null;
  const range = new vscode.Range(startLine, 0, startLine, Math.max(1, document.lineAt(startLine).text.length));
  return { range, hoverMessage: annotationHover(annotation) };
}

function updateEditorAnnotationDecorations(editor = vscode.window.activeTextEditor) {
  if (!editor || editor.document.uri.scheme !== "file") return;
  const items = annotationsProvider?.items || [];
  const grouped = {
    ai_draft: [],
    open: [],
    in_progress: [],
    done: [],
    dismissed: [],
  };
  for (const annotation of items) {
    const status = String(annotation.status || "open");
    const target = grouped[status] ? status : "open";
    const decoration = annotationDecorationForDocument(editor.document, annotation);
    if (decoration) grouped[target].push(decoration);
  }
  for (const [status, type] of Object.entries(annotationDecorationTypes)) {
    editor.setDecorations(type, grouped[status] || []);
  }
}

function updateVisibleAnnotationDecorations() {
  for (const editor of vscode.window.visibleTextEditors) {
    updateEditorAnnotationDecorations(editor);
  }
}

function annotationsForDocument(document) {
  const rel = workspaceRelativePath(document.uri.fsPath);
  if (!rel) return [];
  return (annotationsProvider?.items || []).filter(item => String(item.file_name || "") === rel);
}

function annotationRangeLabel(annotation) {
  const start = Number(annotation.line_start || 0);
  const end = Number(annotation.line_end || 0);
  if (!start) return "";
  return end && end !== start ? `${start}-${end}` : String(start);
}

function activeEditorFileLabel() {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.uri.scheme !== "file") return "";
  return workspaceRelativePath(editor.document.uri.fsPath);
}

function activeEditorLineNumber() {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.uri.scheme !== "file") return 0;
  return editor.selection.active.line + 1;
}

function annotationTouchesLine(annotation, line) {
  if (!line) return false;
  const start = Number(annotation.line_start || 0);
  const end = Number(annotation.line_end || start || 0);
  return start > 0 && line >= start && line <= Math.max(start, end);
}

class AnnotationCodeLensProvider {
  constructor() {
    this._onDidChangeCodeLenses = new vscode.EventEmitter();
    this.onDidChangeCodeLenses = this._onDidChangeCodeLenses.event;
  }

  refresh() {
    this._onDidChangeCodeLenses.fire();
  }

  provideCodeLenses(document) {
    if (!annotationCodeLensEnabled()) return [];
    const lenses = [];
    for (const annotation of annotationsForDocument(document)) {
      const line = Math.max(0, Number(annotation.line_start || 1) - 1);
      if (line >= document.lineCount) continue;
      const range = new vscode.Range(line, 0, line, 0);
      const title = `$(comment) SmartTeX #${annotation.id} ${annotationStatusLabel(annotation.status)}${annotationRangeLabel(annotation) ? ` · ${annotationRangeLabel(annotation)}` : ""}`;
      lenses.push(new vscode.CodeLens(range, {
        title,
        command: "smarttex.openAnnotation",
        arguments: [annotation],
      }));
      if (annotation.status === "ai_draft") {
        lenses.push(new vscode.CodeLens(range, {
          title: "$(check) Keep",
          command: "smarttex.keepAiAnnotation",
          arguments: [annotation],
        }));
      }
      if (annotation.status !== "done") {
        lenses.push(new vscode.CodeLens(range, {
          title: "$(check) Done",
          command: "smarttex.markAnnotationDone",
          arguments: [annotation],
        }));
      }
      if (annotation.status !== "dismissed") {
        lenses.push(new vscode.CodeLens(range, {
          title: "$(x) Dismiss",
          command: "smarttex.dismissAnnotation",
          arguments: [annotation],
        }));
      }
    }
    return lenses;
  }
}

class AnnotationItem extends vscode.TreeItem {
  constructor(annotation) {
    const lineStart = Number(annotation.line_start || 0);
    const lineEnd = Number(annotation.line_end || 0);
    const range = lineStart && lineEnd && lineEnd !== lineStart ? `${lineStart}-${lineEnd}` : (lineStart ? String(lineStart) : "");
    const label = String(annotation.instruction || "").replace(/\s+/g, " ").trim() || `Annotation #${annotation.id}`;
    super(label, vscode.TreeItemCollapsibleState.None);
    this.annotation = annotation;
    this.id = `annotation-${annotation.id}`;
    this.description = [annotationStatusLabel(annotation.status), range].filter(Boolean).join(" ");
    this.tooltip = [
      `#${annotation.id} ${annotationStatusLabel(annotation.status)}`,
      annotation.file_name ? `${annotation.file_name}${range ? `:${range}` : ""}` : "",
      "",
      annotation.instruction || "",
      annotation.selected_text ? `\nFragment: ${annotation.selected_text}` : "",
    ].filter(Boolean).join("\n");
    this.iconPath = annotationIcon(annotation.status);
    this.contextValue = annotation.status === "ai_draft" ? "annotationAiDraft" : "annotation";
    this.command = {
      command: "smarttex.openAnnotation",
      title: "Open Annotation",
      arguments: [this],
    };
  }
}

function historyOperationLabel(operation) {
  const labels = {
    update_project_file: "Edited",
    update_project_asset: "Edited",
    write_project_window: "MCP edit",
    insert_project_section: "MCP insert",
    update_project_section: "MCP section",
    create_project: "Created",
    create_project_file: "File created",
    upload_project_file: "Uploaded",
    upload_project_asset: "Uploaded",
    create_project_folder: "Folder created",
    delete_project_file: "Deleted",
    delete_project_asset: "Deleted",
    rename_project_file: "Renamed",
    rename_project_asset: "Renamed",
    rollback: "Rollback",
    compile_project: "Compiled",
    compile: "Compiled",
  };
  if (labels[operation]) return labels[operation];
  return String(operation || "Version").replace(/_/g, " ").replace(/^\w/, c => c.toUpperCase());
}

function shortDateLabel(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso || "";
  return date.toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

class VersionItem extends vscode.TreeItem {
  constructor(version) {
    const number = version.number ?? version.id;
    const op = historyOperationLabel(version.operation);
    super(`#${number} ${op}`, vscode.TreeItemCollapsibleState.None);
    this.version = version;
    this.id = `version-${version.id}`;
    this.description = [version.target_file || version.target || "", shortDateLabel(version.created_at)].filter(Boolean).join(" · ");
    this.tooltip = [
      `#${number} ${op}`,
      version.target_file || version.target || "",
      version.summary || "",
      version.actor ? `Actor: ${version.actor}` : "",
      version.created_at || "",
    ].filter(Boolean).join("\n");
    this.iconPath = new vscode.ThemeIcon(version.is_revertible ? "history" : "git-commit");
    this.contextValue = version.is_revertible ? "versionRevertible" : "version";
    this.command = {
      command: "smarttex.openHistoryVersion",
      title: "Open Version Diff",
      arguments: [this],
    };
  }
}

class HistoryProvider {
  constructor() {
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.items = [];
    this.loading = false;
    this.error = "";
  }

  refresh() {
    this._onDidChangeTreeData.fire();
  }

  async load() {
    const projectId = projectIdFromWorkspace();
    if (!projectId) {
      this.items = [];
      this.error = "";
      this.refresh();
      return;
    }
    this.loading = true;
    this.refresh();
    try {
      const stdout = await runAgent(["versions", "list", ...apiArgs(projectId), "--limit", "80"], { reveal: false });
      const payload = parseAgentJSON(stdout) || {};
      this.items = Array.isArray(payload.versions) ? payload.versions : [];
      this.error = "";
    } catch (err) {
      this.error = err.message;
      throw err;
    } finally {
      this.loading = false;
      this.refresh();
    }
  }

  getTreeItem(element) {
    return element;
  }

  getChildren() {
    if (this.loading) {
      const item = new vscode.TreeItem("Loading history...", vscode.TreeItemCollapsibleState.None);
      item.iconPath = new vscode.ThemeIcon("loading~spin");
      return [item];
    }
    if (this.error) {
      const item = new vscode.TreeItem("History failed to load.", vscode.TreeItemCollapsibleState.None);
      item.description = this.error;
      item.iconPath = new vscode.ThemeIcon("warning");
      return [item];
    }
    if (!projectIdFromWorkspace()) {
      const item = new vscode.TreeItem("Open a SmartTeX workspace to see history.", vscode.TreeItemCollapsibleState.None);
      item.iconPath = new vscode.ThemeIcon("folder-opened");
      return [item];
    }
    if (!this.items.length) {
      const item = new vscode.TreeItem("No versions yet.", vscode.TreeItemCollapsibleState.None);
      item.iconPath = new vscode.ThemeIcon("history");
      return [item];
    }
    return this.items.map(item => new VersionItem(item));
  }
}

class AnnotationsProvider {
  constructor() {
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.items = [];
    this.loading = false;
    this.lastFingerprint = "";
  }

  refresh() {
    this._onDidChangeTreeData.fire();
  }

  fingerprint(items) {
    return JSON.stringify(items.map(item => ({
      id: item.id,
      status: item.status,
      file_name: item.file_name,
      line_start: item.line_start,
      line_end: item.line_end,
      instruction: item.instruction,
      selected_text: item.selected_text,
    })).sort((a, b) => Number(a.id || 0) - Number(b.id || 0)));
  }

  async load(options = {}) {
    const quiet = options.quiet === true;
    const projectId = projectIdFromWorkspace();
    if (!projectId) {
      const hadItems = this.items.length > 0 || this.lastFingerprint;
      this.items = [];
      this.lastFingerprint = "";
      if (hadItems || !quiet) this.refresh();
      return;
    }
    if (!quiet || !this.items.length) {
      this.loading = true;
      this.refresh();
    }
    let changed = false;
    try {
      const stdout = await runAgent(["annotations", "list", ...apiArgs(projectId)], { reveal: false });
      const payload = parseAgentJSON(stdout) || {};
      const nextItems = activeAnnotationItems(Array.isArray(payload.annotations) ? payload.annotations : []);
      const nextFingerprint = this.fingerprint(nextItems);
      changed = nextFingerprint !== this.lastFingerprint;
      if (changed) {
        this.items = nextItems;
        this.lastFingerprint = nextFingerprint;
      }
    } finally {
      this.loading = false;
      if (changed || !quiet) {
        this.refresh();
        dashboardProvider?.refresh();
        fileAnnotationsProvider?.refresh();
        updateVisibleAnnotationDecorations();
        annotationCodeLensProvider?.refresh();
      }
    }
  }

  getTreeItem(element) {
    return element;
  }

  getChildren() {
    if (this.loading) {
      const item = new vscode.TreeItem("Loading annotations...", vscode.TreeItemCollapsibleState.None);
      item.iconPath = new vscode.ThemeIcon("loading~spin");
      return [item];
    }
    if (!projectIdFromWorkspace()) {
      const item = new vscode.TreeItem("Open a SmartTeX workspace to see annotations.", vscode.TreeItemCollapsibleState.None);
      item.iconPath = new vscode.ThemeIcon("folder-opened");
      return [item];
    }
    if (!this.items.length) {
      const item = new vscode.TreeItem("No annotations.", vscode.TreeItemCollapsibleState.None);
      item.iconPath = new vscode.ThemeIcon("pass");
      return [item];
    }
    return this.items.map(item => new AnnotationItem(item));
  }
}

class FileAnnotationsProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage(command(async message => {
      const type = String(message?.type || "");
      const id = Number(message?.id || 0);
      const annotation = (annotationsProvider?.items || []).find(item => Number(item.id) === id);
      if (type === "open" && annotation) {
        await openAnnotation(annotation);
      } else if (type === "status" && annotation) {
        await updateAnnotationStatus(annotation, String(message.status || ""));
      } else if (type === "add") {
        await addQuickAnnotation();
      } else if (type === "refresh") {
        await refreshAnnotations();
      }
    }));
    this.refresh();
    loadWorkspaceStatus(projectIdFromWorkspace(), { quiet: true }).catch(() => {});
  }

  refresh() {
    if (!this.view) return;
    this.view.webview.html = this.renderHtml();
  }

  renderHtml() {
    const nonce = String(Date.now());
    const file = activeEditorFileLabel();
    const activeLine = activeEditorLineNumber();
    const items = file
      ? (annotationsProvider?.items || []).filter(item => String(item.file_name || "") === file)
      : [];
    const activeItems = items.filter(item => annotationTouchesLine(item, activeLine));
    const sorted = [...items].sort((a, b) => Number(a.line_start || 0) - Number(b.line_start || 0));
    const card = annotation => {
      const status = String(annotation.status || "open");
      const isActive = annotationTouchesLine(annotation, activeLine);
      const range = annotationRangeLabel(annotation);
      const badge = annotationStatusLabel(status);
      return `<article class="note ${escapeHtml(status)} ${isActive ? "active" : ""}">
        <header>
          <span class="badge ${escapeHtml(status)}">${escapeHtml(badge)}</span>
          <strong>${escapeHtml(range || "?")}</strong>
        </header>
        <p>${escapeHtml(annotation.instruction || `Annotation #${annotation.id}`)}</p>
        <div class="actions">
          <button data-action="open" data-id="${escapeHtml(annotation.id)}">Open</button>
          ${status === "ai_draft" ? `<button data-action="status" data-status="open" data-id="${escapeHtml(annotation.id)}">Keep</button>` : ""}
          ${status !== "done" ? `<button data-action="status" data-status="done" data-id="${escapeHtml(annotation.id)}">Done</button>` : ""}
          ${status !== "dismissed" ? `<button data-action="status" data-status="dismissed" data-id="${escapeHtml(annotation.id)}">Dismiss</button>` : ""}
        </div>
      </article>`;
    };
    const body = !projectIdFromWorkspace() ? `
      <section class="empty"><h2>No workspace</h2><p>Open a SmartTeX workspace to review file notes.</p></section>
    ` : !file ? `
      <section class="empty"><h2>No active file</h2><p>Open a project text file to see its annotations.</p></section>
    ` : `
      <section class="head">
        <div>
          <span>Current file</span>
          <h2>${escapeHtml(path.basename(file))}</h2>
          <p>${escapeHtml(file)}${activeLine ? ` · line ${escapeHtml(activeLine)}` : ""}</p>
        </div>
        <button data-action="add">+</button>
      </section>
      ${activeItems.length ? `<section class="focus"><span>At cursor</span>${activeItems.map(card).join("")}</section>` : ""}
      <section class="list">
        ${sorted.length ? sorted.map(card).join("") : `<div class="empty slim"><h2>No notes in this file</h2><p>Use + or Cmd/Ctrl+Alt+A to add one.</p></div>`}
      </section>
    `;
    return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <style nonce="${nonce}">
    body { margin: 0; padding: 12px; color: var(--vscode-foreground); font: 12px/1.45 var(--vscode-font-family); }
    .head, .empty, .note { border: 1px solid var(--vscode-panel-border); border-radius: 12px; background: var(--vscode-sideBar-background); }
    .head { padding: 12px; display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; background: linear-gradient(135deg, rgba(34,197,94,.12), rgba(59,130,246,.08)); }
    .head span, .focus > span { color: var(--vscode-descriptionForeground); text-transform: uppercase; letter-spacing: .08em; font-size: 10px; font-weight: 800; }
    h2 { margin: 3px 0 0; font-size: 17px; }
    p { margin: 6px 0 0; color: var(--vscode-descriptionForeground); }
    .head p { overflow-wrap: anywhere; }
    .head button { width: 34px; height: 34px; border-radius: 10px; font-size: 20px; }
    .focus { margin-top: 10px; }
    .list { margin-top: 10px; display: grid; gap: 8px; }
    .note { padding: 10px; }
    .note.active { border-color: rgba(34,197,94,.7); box-shadow: inset 3px 0 0 rgba(34,197,94,.85); }
    .note.ai_draft { border-color: rgba(168,85,247,.45); }
    .note.done, .note.dismissed { opacity: .72; }
    .note header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .badge { border: 1px solid currentColor; border-radius: 999px; padding: 2px 7px; font-size: 10px; font-weight: 800; color: #60a5fa; }
    .badge.ai_draft { color: #c084fc; }
    .badge.done { color: #22c55e; }
    .badge.dismissed { color: #94a3b8; }
    .actions { margin-top: 9px; display: flex; flex-wrap: wrap; gap: 6px; }
    button { border: 1px solid var(--vscode-button-border, transparent); border-radius: 8px; padding: 6px 8px; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); cursor: pointer; font: inherit; font-weight: 700; }
    button:hover { background: var(--vscode-button-secondaryHoverBackground); }
    .empty { padding: 14px; }
    .empty.slim { padding: 12px; }
  </style>
</head>
<body>
  ${body}
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.addEventListener("click", event => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      vscode.postMessage({ type: button.dataset.action, id: button.dataset.id || 0, status: button.dataset.status || "" });
    });
  </script>
</body>
</html>`;
  }
}

class SmartTeXDashboardProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage(command(async message => {
      const commandId = String(message?.command || "");
      const allowed = new Set([
        "smarttex.login",
        "smarttex.openProject",
        "smarttex.startLocalAgent",
        "smarttex.openPreview",
        "smarttex.compile",
        "smarttex.showCompileLog",
        "smarttex.showProblems",
        "smarttex.refreshAll",
        "smarttex.syncWorkspace",
        "smarttex.pullWorkspace",
        "smarttex.refreshAnnotations",
        "smarttex.addAnnotation",
        "smarttex.openWebEditor",
        "smarttex.refreshAiChanges",
        "smarttex.openAiChanges",
        "smarttex.refreshHistory",
        "smarttex.openHistoryVersion",
        "smarttex.releaseWorkspace",
        "smarttex.workspaceStatus",
      ]);
      if (allowed.has(commandId)) await vscode.commands.executeCommand(commandId);
    }));
    this.refresh();
    loadWorkspaceStatus(projectIdFromWorkspace(), { quiet: true }).catch(() => {});
  }

  refresh() {
    if (!this.view) return;
    this.view.webview.html = this.renderHtml();
  }

  renderHtml() {
    const nonce = String(Date.now());
    const state = readWorkspaceState();
    const projectId = Number(state?.project_id || 0);
  const annotationItems = annotationsProvider?.items || [];
  const aiCount = annotationItems.filter(item => item.status === "ai_draft").length;
  const openCount = annotationItems.filter(item => !["done", "dismissed"].includes(String(item.status || ""))).length;
  const pdfEmbedCount = Object.values(lastPdfEmbeds || {}).filter(entry => entry?.enabled).length;
  const bridgeConfigured = Boolean(localBridgeSecret());
    const authConfigured = localAuthConfigured();
    const compileClass = compileStateClass();
    const workspaceStatus = lastWorkspaceStatus?.project_id === projectId ? lastWorkspaceStatus : null;
    const workspaceClass = workspaceStatusClass(workspaceStatus);
    const workspaceLabel = workspaceStatusLabel(workspaceStatus);
    const workspaceDetail = workspaceStatusDetail(workspaceStatus);
    const button = (commandId, label, kind = "") => `<button class="${kind}" data-command="${escapeHtml(commandId)}">${escapeHtml(label)}</button>`;
    const kv = (label, value) => `<div class="kv"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "—")}</strong></div>`;
    const projectBody = projectId ? `
      <section class="hero ${workspaceClass}">
        <div>
          <div class="eyebrow">Local workspace</div>
          <h2>Project #${escapeHtml(projectId)}</h2>
          <p>${escapeHtml(workspaceDetail)}</p>
        </div>
        <span class="pill ${workspaceClass}">${escapeHtml(workspaceLabel)}</span>
      </section>
      <section class="grid">
        ${kv("Server", state.server || serverUrl())}
        ${kv("Versions", workspaceStatus?.server_latest_version
          ? `local v${workspaceStatus.local_base_version || "?"} · server v${workspaceStatus.server_latest_version}`
          : state.base_version_number || "")}
        ${kv("Unsynced", workspaceStatus ? String(workspaceStatus.local_unsynced_changes || 0) : "Checking")}
        ${kv("Annotations", openCount ? `${openCount} active${aiCount ? ` · ${aiCount} AI` : ""}` : "No active")}
        ${kv("PDF embeds", pdfEmbedCount ? `${pdfEmbedCount} enabled` : "None enabled")}
        ${kv("Auth", authConfigured ? "Signed in" : "Needs login")}
        ${kv("Agent", bridgeConfigured ? "Configured" : "Needs login")}
      </section>
      <section class="actions primary-actions">
        ${button("smarttex.openPreview", "Open preview", "primary")}
        ${workspaceStatus && !workspaceStatus.server_lease_active
          ? button("smarttex.pullWorkspace", "Reconnect workspace", "primary")
          : button("smarttex.syncWorkspace", "Sync now")}
        ${button("smarttex.compile", "Compile")}
      </section>
      <section class="compile-card ${compileClass}">
        <div>
          <span>Compile</span>
          <strong>${escapeHtml(compileStateLabel())}</strong>
          <em>${escapeHtml(compileStateDetail())}</em>
        </div>
        <div class="compile-actions">
          ${button("smarttex.compile", "Run")}
          ${button("smarttex.showProblems", "Problems")}
          ${button("smarttex.showCompileLog", "Log")}
        </div>
      </section>
      <section class="actions">
        ${button("smarttex.addAnnotation", "Add annotation")}
        ${button("smarttex.refreshAll", "Refresh all")}
        ${button("smarttex.openAiChanges", "AI changes")}
        ${button("smarttex.refreshHistory", "History")}
        ${button("smarttex.refreshAnnotations", "Refresh annotations")}
        ${button("smarttex.pullWorkspace", "Pull server")}
        ${button("smarttex.openWebEditor", "Open web")}
        ${button("smarttex.showCompileLog", "Compile log")}
        ${button("smarttex.workspaceStatus", "Status")}
        ${button("smarttex.releaseWorkspace", "Release", "danger")}
      </section>
    ` : `
      <section class="hero empty">
        <div>
          <div class="eyebrow">SmartTeX</div>
          <h2>No local project open</h2>
          <p>Open a project locally to edit files, sync changes, review annotations, and use local Typst preview from VS Code.</p>
        </div>
      </section>
      <section class="actions primary-actions">
        ${button("smarttex.openProject", "Choose project", "primary")}
        ${button("smarttex.login", "Login")}
        ${button("smarttex.startLocalAgent", "Start agent")}
      </section>
    `;
    return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <style nonce="${nonce}">
    :root { color-scheme: light dark; }
    body { margin: 0; padding: 14px; color: var(--vscode-foreground); font: 12px/1.45 var(--vscode-font-family); }
    .hero { border: 1px solid var(--vscode-panel-border); border-radius: 12px; padding: 14px; background: linear-gradient(135deg, rgba(34,197,94,.14), rgba(59,130,246,.08)); display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
    .hero.warn { border-color: rgba(245,158,11,.52); background: linear-gradient(135deg, rgba(245,158,11,.15), rgba(59,130,246,.06)); }
    .hero.bad { border-color: rgba(239,68,68,.58); background: linear-gradient(135deg, rgba(239,68,68,.15), rgba(59,130,246,.05)); }
    .hero.idle { background: linear-gradient(135deg, rgba(148,163,184,.12), rgba(59,130,246,.06)); }
    .hero.empty { display: block; }
    .eyebrow { color: var(--vscode-descriptionForeground); text-transform: uppercase; letter-spacing: .08em; font-size: 10px; font-weight: 700; }
    h2 { margin: 3px 0 0; font-size: 18px; line-height: 1.15; }
    p { margin: 8px 0 0; color: var(--vscode-descriptionForeground); }
    .pill { border-radius: 999px; padding: 3px 8px; font-weight: 700; font-size: 10px; border: 1px solid currentColor; }
    .pill.ok { color: #22c55e; background: rgba(34,197,94,.12); }
    .pill.warn { color: #f59e0b; background: rgba(245,158,11,.12); }
    .pill.bad { color: #f87171; background: rgba(239,68,68,.12); }
    .pill.idle { color: #94a3b8; background: rgba(148,163,184,.10); }
    .grid { display: grid; gap: 8px; margin-top: 12px; }
    .kv { border: 1px solid var(--vscode-panel-border); border-radius: 10px; padding: 9px 10px; background: var(--vscode-sideBar-background); }
    .kv span { display: block; color: var(--vscode-descriptionForeground); font-size: 11px; }
    .kv strong { display: block; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    .primary-actions { grid-template-columns: 1fr; margin-top: 12px; }
    .compile-card { margin-top: 10px; border: 1px solid var(--vscode-panel-border); border-radius: 12px; padding: 11px; background: var(--vscode-sideBar-background); }
    .compile-card.ok { border-color: rgba(34,197,94,.45); background: linear-gradient(135deg, rgba(34,197,94,.12), transparent); }
    .compile-card.warn { border-color: rgba(245,158,11,.5); background: linear-gradient(135deg, rgba(245,158,11,.13), transparent); }
    .compile-card.bad { border-color: rgba(239,68,68,.55); background: linear-gradient(135deg, rgba(239,68,68,.13), transparent); }
    .compile-card span { display: block; color: var(--vscode-descriptionForeground); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; font-weight: 800; }
    .compile-card strong { display: block; margin-top: 2px; font-size: 15px; }
    .compile-card em { display: block; margin-top: 3px; color: var(--vscode-descriptionForeground); font-style: normal; }
    .compile-actions { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 9px; }
    button { width: 100%; border: 1px solid var(--vscode-button-border, transparent); border-radius: 9px; padding: 8px 10px; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); cursor: pointer; font: inherit; font-weight: 650; }
    button:hover { background: var(--vscode-button-secondaryHoverBackground); }
    button.primary { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
    button.primary:hover { background: var(--vscode-button-hoverBackground); }
    button.danger { color: var(--vscode-errorForeground); }
    .foot { margin-top: 12px; color: var(--vscode-descriptionForeground); font-size: 11px; }
  </style>
</head>
<body>
  ${projectBody}
  <div class="foot">Workspace root: ${escapeHtml(workspaceRoot())}</div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.addEventListener("click", event => {
      const button = event.target.closest("button[data-command]");
      if (!button) return;
      vscode.postMessage({ command: button.dataset.command });
    });
  </script>
</body>
</html>`;
  }
}

function compileProblemSeverityClass(value) {
  const normalized = String(value || "").toLowerCase();
  if (normalized.includes("warn")) return "warn";
  if (normalized.includes("info") || normalized.includes("hint")) return "info";
  return "error";
}

function hasEmbeddedPdfProblem() {
  const haystack = [
    lastCompileLog,
    ...lastCompileDiagnostics.map(item => `${item.file} ${item.message}`),
  ].join("\n").toLowerCase();
  return haystack.includes("pdf_includes") || haystack.includes("smarttex-include-pdf") || haystack.includes("embed pdf");
}

async function openCompileDiagnostic(index) {
  const item = lastCompileDiagnostics[Number(index || 0)];
  if (!item?.fullPath) return;
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(item.fullPath));
  const editor = await vscode.window.showTextDocument(document, { preview: false, viewColumn: lastTextEditorViewColumn || vscode.ViewColumn.One });
  const position = new vscode.Position(Math.max(0, Number(item.line || 1) - 1), Math.max(0, Number(item.column || 1) - 1));
  editor.selection = new vscode.Selection(position, position);
  editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenterIfOutsideViewport);
}

async function openPdfIncludesFile() {
  const root = activeWorkspaceFolder();
  if (!root) return;
  const candidates = [
    path.join(root, ".smarttex", "auto_generated", "pdf_includes.typ"),
    path.join(root, ".smarttex", "auto_generated", "pdf_includes.typst"),
  ];
  const existing = candidates.find(candidate => fs.existsSync(candidate));
  if (!existing) {
    vscode.window.showWarningMessage("SmartTeX embedded PDF include file is not present in this local workspace yet. Pull/recompile the project to regenerate it.");
    return;
  }
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(existing));
  await vscode.window.showTextDocument(document, { preview: false, viewColumn: lastTextEditorViewColumn || vscode.ViewColumn.One });
}

class SmartTeXProblemsProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage(command(async message => {
      const type = String(message?.type || "");
      if (type === "command") {
        const commandId = String(message?.command || "");
        const allowed = new Set([
          "smarttex.compile",
          "smarttex.showCompileLog",
          "smarttex.showProblems",
          "smarttex.refreshAll",
        ]);
        if (allowed.has(commandId)) await vscode.commands.executeCommand(commandId);
      } else if (type === "openDiagnostic") {
        await openCompileDiagnostic(message.index);
      } else if (type === "openPdfIncludes") {
        await openPdfIncludesFile();
      }
    }));
    this.refresh();
  }

  refresh() {
    if (!this.view) return;
    this.view.webview.html = this.renderHtml();
  }

  renderHtml() {
    const nonce = String(Date.now());
    const compileClass = compileStateClass();
    const diagnostics = lastCompileDiagnostics || [];
    const pdfHint = hasEmbeddedPdfProblem();
    const button = (commandId, label, kind = "") => `<button class="${kind}" data-command="${escapeHtml(commandId)}">${escapeHtml(label)}</button>`;
    const diagnosticCard = (item, index) => {
      const cls = compileProblemSeverityClass(item.severity);
      return `<button class="problem ${cls}" data-index="${escapeHtml(index)}">
        <span class="sev">${escapeHtml(cls)}</span>
        <strong>${escapeHtml(item.message)}</strong>
        <em>${escapeHtml(item.file)}:${escapeHtml(item.line)}${item.column ? `:${escapeHtml(item.column)}` : ""}</em>
      </button>`;
    };
    const body = !lastCompileState ? `
      <section class="empty">
        <h2>No compile result yet</h2>
        <p>Run SmartTeX compile to populate diagnostics, embedded PDF checks, and the compile log.</p>
      </section>
    ` : `
      <section class="hero ${compileClass}">
        <div>
          <span>Compile</span>
          <h2>${escapeHtml(compileStateLabel())}</h2>
          <p>${escapeHtml(compileStateDetail())}</p>
        </div>
      </section>
      ${pdfHint ? `<section class="embed-pdf">
        <span>Embedded PDF</span>
        <strong>PDF include generation needs attention</strong>
        <p>Compilation mentioned <code>pdf_includes</code> or <code>smarttex-include-pdf</code>. This usually means embedded PDF metadata/files are missing from the local workspace or were not regenerated.</p>
        <div class="actions two">
          <button data-action="openPdfIncludes">Open includes</button>
          ${button("smarttex.compile", "Recompile")}
        </div>
      </section>` : ""}
      <section class="actions">
        ${button("smarttex.compile", "Run compile", "primary")}
        ${button("smarttex.showCompileLog", "Log")}
        ${button("smarttex.showProblems", "VS Code Problems")}
        ${button("smarttex.refreshAll", "Refresh")}
      </section>
      <section class="list">
        ${diagnostics.length ? diagnostics.map(diagnosticCard).join("") : `<div class="empty slim"><h2>No SmartTeX diagnostics</h2><p>Compiler diagnostics will appear here after a failed compile or warning.</p></div>`}
      </section>
    `;
    return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <style nonce="${nonce}">
    body { margin: 0; padding: 12px; color: var(--vscode-foreground); font: 12px/1.45 var(--vscode-font-family); }
    .hero, .empty, .embed-pdf, .problem { border: 1px solid var(--vscode-panel-border); border-radius: 12px; background: var(--vscode-sideBar-background); }
    .hero { padding: 13px; background: linear-gradient(135deg, rgba(34,197,94,.14), rgba(59,130,246,.08)); }
    .hero.warn { border-color: rgba(245,158,11,.5); background: linear-gradient(135deg, rgba(245,158,11,.14), rgba(59,130,246,.07)); }
    .hero.bad { border-color: rgba(239,68,68,.55); background: linear-gradient(135deg, rgba(239,68,68,.14), rgba(59,130,246,.06)); }
    .hero.idle { background: linear-gradient(135deg, rgba(148,163,184,.12), rgba(59,130,246,.06)); }
    .hero span, .embed-pdf span, .sev { display: inline-block; color: var(--vscode-descriptionForeground); text-transform: uppercase; letter-spacing: .08em; font-size: 10px; font-weight: 800; }
    h2 { margin: 3px 0 0; font-size: 17px; line-height: 1.15; }
    p { margin: 7px 0 0; color: var(--vscode-descriptionForeground); }
    code { color: var(--vscode-textPreformat-foreground); }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    .actions.two { grid-template-columns: 1fr 1fr; }
    button { border: 1px solid var(--vscode-button-border, transparent); border-radius: 9px; padding: 8px 9px; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); cursor: pointer; font: inherit; font-weight: 700; text-align: center; }
    button:hover { background: var(--vscode-button-secondaryHoverBackground); }
    button.primary { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
    .embed-pdf { margin-top: 10px; padding: 12px; border-color: rgba(245,158,11,.55); background: linear-gradient(135deg, rgba(245,158,11,.12), transparent); }
    .embed-pdf strong { display: block; margin-top: 4px; }
    .list { display: grid; gap: 8px; margin-top: 10px; }
    .problem { display: block; width: 100%; padding: 10px; text-align: left; background: var(--vscode-sideBar-background); }
    .problem.error { border-color: rgba(239,68,68,.45); box-shadow: inset 3px 0 0 rgba(239,68,68,.75); }
    .problem.warn { border-color: rgba(245,158,11,.45); box-shadow: inset 3px 0 0 rgba(245,158,11,.8); }
    .problem.info { border-color: rgba(59,130,246,.45); box-shadow: inset 3px 0 0 rgba(59,130,246,.75); }
    .problem strong { display: block; margin-top: 4px; color: var(--vscode-foreground); }
    .problem em { display: block; margin-top: 5px; color: var(--vscode-descriptionForeground); font-style: normal; overflow-wrap: anywhere; }
    .empty { padding: 13px; }
    .empty.slim { padding: 12px; }
  </style>
</head>
<body>
  ${body}
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.addEventListener("click", event => {
      const diagnostic = event.target.closest("button[data-index]");
      if (diagnostic) {
        vscode.postMessage({ type: "openDiagnostic", index: diagnostic.dataset.index });
        return;
      }
      const pdf = event.target.closest("button[data-action='openPdfIncludes']");
      if (pdf) {
        vscode.postMessage({ type: "openPdfIncludes" });
        return;
      }
      const button = event.target.closest("button[data-command]");
      if (!button) return;
      vscode.postMessage({ type: "command", command: button.dataset.command });
    });
  </script>
</body>
</html>`;
  }
}

async function openWorkspaceFile(filePath) {
  const root = activeWorkspaceFolder();
  const rel = String(filePath || "");
  if (!root || !rel) return;
  const fullPath = path.join(root, filepathFromProjectPath(rel));
  if (!fs.existsSync(fullPath)) {
    vscode.window.showWarningMessage(`File not found in local workspace: ${rel}`);
    return;
  }
  await vscode.commands.executeCommand("vscode.open", vscode.Uri.file(fullPath));
}

class SmartTeXPdfEmbedsProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage(command(async message => {
      const type = String(message?.type || "");
      const file = String(message?.file || "");
      if (type === "refresh") await refreshPdfEmbeds();
      if (type === "enable") await enablePdfEmbed(file);
      if (type === "disable") await disablePdfEmbed(file);
      if (type === "insert") await insertPdfEmbedSnippet(file);
      if (type === "open") await openWorkspaceFile(file);
      if (type === "compile") await compileProject();
    }));
    this.refresh();
    loadPdfEmbeds({ quiet: true }).catch(() => {});
  }

  refresh() {
    if (!this.view) return;
    this.view.webview.html = this.renderHtml();
  }

  renderHtml() {
    const nonce = String(Date.now());
    const localPdfs = scanLocalPdfFiles();
    const manifest = lastPdfEmbeds || {};
    const knownFiles = Array.from(new Set([...localPdfs, ...Object.keys(manifest)])).sort((a, b) => a.localeCompare(b));
    const enabled = knownFiles.filter(file => manifest[file]?.enabled);
    const disabled = knownFiles.filter(file => !manifest[file]?.enabled);
    const card = (file) => {
      const entry = manifest[file] || {};
      const isEnabled = Boolean(entry.enabled);
      const local = localPdfs.includes(file);
      const pageCount = entry.page_count ? `${entry.page_count} page${Number(entry.page_count) === 1 ? "" : "s"}` : "";
      const hash = entry.last_hash ? `hash ${String(entry.last_hash).slice(0, 10)}` : "";
      const meta = [local ? "local file" : "not in local files", pageCount, hash].filter(Boolean).join(" · ");
      return `<article class="pdf ${isEnabled ? "enabled" : "disabled"} ${local ? "" : "missing"}">
        <header>
          <span class="badge ${isEnabled ? "on" : "off"}">${isEnabled ? "Embed on" : "Embed off"}</span>
          <strong>${escapeHtml(path.basename(file))}</strong>
        </header>
        <p>${escapeHtml(file)}</p>
        <em>${escapeHtml(meta || "No embed metadata yet")}</em>
        <div class="actions">
          <button data-action="open" data-file="${escapeHtml(file)}">Open</button>
          ${isEnabled ? `<button data-action="insert" data-file="${escapeHtml(file)}">Insert</button>` : `<button data-action="enable" data-file="${escapeHtml(file)}">Enable</button>`}
          ${isEnabled ? `<button data-action="disable" data-file="${escapeHtml(file)}">Disable</button>` : ""}
        </div>
      </article>`;
    };
    const body = !projectIdFromWorkspace() ? `
      <section class="empty"><h2>No workspace</h2><p>Open a SmartTeX local workspace to manage embedded PDFs.</p></section>
    ` : `
      <section class="hero">
        <div>
          <span>Embedded PDF</span>
          <h2>${escapeHtml(enabled.length)} enabled</h2>
          <p>${escapeHtml(localPdfs.length)} local PDF file${localPdfs.length === 1 ? "" : "s"} · generated via SmartTeX pre-compile jobs</p>
        </div>
      </section>
      ${pdfEmbedsError ? `<section class="error"><strong>Could not load PDF embeds</strong><p>${escapeHtml(pdfEmbedsError)}</p></section>` : ""}
      <section class="actions top">
        <button data-action="refresh">Refresh</button>
        <button data-action="compile">Recompile</button>
      </section>
      <section class="list">
        ${pdfEmbedsLoading ? `<div class="empty slim"><h2>Loading...</h2></div>` : ""}
        ${enabled.length ? `<h3>Enabled</h3>${enabled.map(card).join("")}` : ""}
        ${disabled.length ? `<h3>Available / disabled</h3>${disabled.map(card).join("")}` : ""}
        ${!knownFiles.length && !pdfEmbedsLoading ? `<div class="empty slim"><h2>No PDFs found</h2><p>Add a PDF file to the project workspace, then refresh this panel.</p></div>` : ""}
      </section>
    `;
    return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <style nonce="${nonce}">
    body { margin: 0; padding: 12px; color: var(--vscode-foreground); font: 12px/1.45 var(--vscode-font-family); }
    .hero, .empty, .pdf, .error { border: 1px solid var(--vscode-panel-border); border-radius: 12px; background: var(--vscode-sideBar-background); }
    .hero { padding: 13px; background: linear-gradient(135deg, rgba(34,197,94,.13), rgba(59,130,246,.08)); }
    .hero span, h3 { color: var(--vscode-descriptionForeground); text-transform: uppercase; letter-spacing: .08em; font-size: 10px; font-weight: 800; }
    h2 { margin: 3px 0 0; font-size: 18px; }
    h3 { margin: 12px 0 6px; }
    p { margin: 6px 0 0; color: var(--vscode-descriptionForeground); overflow-wrap: anywhere; }
    .actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
    .actions.top { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    button { border: 1px solid var(--vscode-button-border, transparent); border-radius: 8px; padding: 7px 8px; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); cursor: pointer; font: inherit; font-weight: 700; }
    button:hover { background: var(--vscode-button-secondaryHoverBackground); }
    .list { margin-top: 8px; }
    .pdf { padding: 10px; margin-top: 8px; }
    .pdf.enabled { border-color: rgba(34,197,94,.48); box-shadow: inset 3px 0 0 rgba(34,197,94,.75); }
    .pdf.missing { border-color: rgba(245,158,11,.5); }
    .pdf header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .pdf strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pdf em { display: block; margin-top: 5px; color: var(--vscode-descriptionForeground); font-style: normal; }
    .badge { flex: 0 0 auto; border-radius: 999px; border: 1px solid currentColor; padding: 2px 7px; font-size: 10px; font-weight: 800; }
    .badge.on { color: #22c55e; background: rgba(34,197,94,.10); }
    .badge.off { color: #94a3b8; background: rgba(148,163,184,.10); }
    .empty, .error { padding: 13px; }
    .empty.slim { padding: 12px; }
    .error { margin-top: 10px; border-color: rgba(239,68,68,.45); }
    .error strong { color: var(--vscode-errorForeground); }
  </style>
</head>
<body>
  ${body}
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.addEventListener("click", event => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      vscode.postMessage({ type: button.dataset.action, file: button.dataset.file || "" });
    });
  </script>
</body>
</html>`;
  }
}

function proposalStatusLabel(status) {
  switch (String(status || "")) {
    case "validating": return "Готується";
    case "failed_validation": return "Потребує уваги";
    case "failed_compile": return "Не компілюється";
    case "ready_for_review": return "Готово до перегляду";
    case "accepted": return "Прийнято";
    case "discarded": return "Відхилено";
    default: return String(status || "Немає");
  }
}

function proposalVisible(proposal) {
  return Boolean(proposal && ["failed_validation", "failed_compile", "ready_for_review", "validating"].includes(String(proposal.status || "")));
}

class AiChangesProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
    this.proposal = null;
    this.diffPayload = null;
    this.loading = false;
    this.error = "";
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage(command(async message => {
      const commandId = String(message?.command || "");
      const allowed = new Set([
        "smarttex.refreshAiChanges",
        "smarttex.openAiChanges",
        "smarttex.acceptAiChanges",
        "smarttex.discardAiChanges",
        "smarttex.openWebEditor",
      ]);
      if (allowed.has(commandId)) await vscode.commands.executeCommand(commandId);
      if (message?.type === "edit-line") {
        await manualEditAiProposalLine(message);
      }
    }));
    this.refresh();
    this.load().catch(err => {
      this.error = err.message;
      this.refresh();
    });
  }

  async load() {
    const projectId = projectIdFromWorkspace();
    if (!projectId) {
      this.proposal = null;
      this.diffPayload = null;
      this.error = "";
      this.refresh();
      return null;
    }
    this.loading = true;
    this.refresh();
    try {
      const stdout = await runAgent(["proposals", "status", ...apiArgs(projectId)], { reveal: false });
      const payload = parseAgentJSON(stdout) || {};
      this.proposal = payload.proposal || null;
      if (!proposalVisible(this.proposal)) this.diffPayload = null;
      this.error = "";
      return this.proposal;
    } catch (err) {
      this.error = err.message;
      throw err;
    } finally {
      this.loading = false;
      this.refresh();
      dashboardProvider?.refresh();
    }
  }

  refresh() {
    if (!this.view) return;
    this.view.webview.html = this.renderHtml();
  }

  async openDiff() {
    const projectId = projectIdFromWorkspace() || await askProjectId();
    if (!projectId) return;
    const proposal = await (this.load().catch(() => null) || Promise.resolve(null));
    if (!proposalVisible(proposal)) {
      vscode.window.showInformationMessage("SmartTeX: no reviewable AI changes.");
      return;
    }
    this.loading = true;
    this.refresh();
    try {
      this.diffPayload = await loadProposalDiff(projectId);
      await vscode.commands.executeCommand("smarttex.aiChanges.focus");
    } finally {
      this.loading = false;
      this.refresh();
    }
  }

  renderHtml() {
    const nonce = String(Date.now());
    const proposal = this.proposal;
    const projectId = projectIdFromWorkspace();
    const diffPayload = this.diffPayload;
    const hasProposal = proposalVisible(proposal);
    const risk = proposal?.smcl_risk_level || "low";
    const changedFiles = Array.isArray(proposal?.changed_files) ? proposal.changed_files : [];
    const warnings = Array.isArray(proposal?.smcl_warnings) ? proposal.smcl_warnings : [];
    const summary = proposal?.semantic_diff_summary || {};
    const button = (commandId, label, kind = "") => `<button class="${kind}" data-command="${escapeHtml(commandId)}">${escapeHtml(label)}</button>`;
    const body = !projectId ? `
      <section class="empty">
        <h2>AI changes</h2>
        <p>Open a SmartTeX local workspace to review proposed AI changes here.</p>
      </section>
    ` : this.loading ? `
      <section class="empty"><h2>Loading...</h2><p>Checking active proposal.</p></section>
    ` : this.error ? `
      <section class="empty error"><h2>Could not load AI changes</h2><p>${escapeHtml(this.error)}</p></section>
      <div class="actions">${button("smarttex.refreshAiChanges", "Retry", "primary")}</div>
    ` : hasProposal && diffPayload ? `
      <section class="diff-review">
        ${aiChangesDiffBody(projectId, proposal, diffPayload)}
      </section>
    ` : hasProposal ? `
      <section class="card ${escapeHtml(risk)}">
        <div class="head">
          <div>
            <div class="eyebrow">Proposal #${escapeHtml(proposal.id || "")}</div>
            <h2>${escapeHtml(proposalStatusLabel(proposal.status))}</h2>
          </div>
          <span class="pill ${escapeHtml(risk)}">${escapeHtml(risk)}</span>
        </div>
        <p>${escapeHtml(proposal.goal || proposal.user_visible_message || "AI-сесія запропонувала зміни.")}</p>
      </section>
      ${summary?.title ? `
        <section class="mini">
          <strong>${escapeHtml(summary.title)}</strong>
          ${summary.impact ? `<span>${escapeHtml(summary.impact)}</span>` : ""}
        </section>
      ` : ""}
      ${warnings.length ? `
        <section class="mini warn">
          <strong>${warnings.length} warning${warnings.length === 1 ? "" : "s"}</strong>
          <span>${escapeHtml(warnings[0]?.human_title || warnings[0]?.message || warnings[0]?.code || "Review before accepting")}</span>
        </section>
      ` : ""}
      ${changedFiles.length ? `
        <section class="files">
          ${changedFiles.slice(0, 6).map(file => `<div><span>${escapeHtml(file.filename || "")}</span><b>+${escapeHtml(file.lines_added || 0)} -${escapeHtml(file.lines_removed || 0)}</b></div>`).join("")}
        </section>
      ` : ""}
      <div class="actions primary-actions">${button("smarttex.openAiChanges", "Review diff", "primary")}</div>
      <div class="actions">
        ${button("smarttex.acceptAiChanges", "Accept", "accept")}
        ${button("smarttex.discardAiChanges", "Discard", "danger")}
        ${button("smarttex.refreshAiChanges", "Refresh")}
        ${button("smarttex.openWebEditor", "Open web")}
      </div>
    ` : `
      <section class="empty ok">
        <h2>No active AI changes</h2>
        <p>When an AI proposal is ready or needs attention, it will appear here.</p>
      </section>
      <div class="actions">${button("smarttex.refreshAiChanges", "Refresh", "primary")}</div>
    `;
    return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <style nonce="${nonce}">
    body { margin: 0; padding: 14px; color: var(--vscode-foreground); font: 12px/1.45 var(--vscode-font-family); }
    .card, .empty, .mini, .files { border: 1px solid var(--vscode-panel-border); border-radius: 12px; padding: 13px; background: var(--vscode-sideBar-background); }
    .card { background: linear-gradient(135deg, rgba(245,158,11,.15), rgba(59,130,246,.08)); }
    .card.high { background: linear-gradient(135deg, rgba(239,68,68,.16), rgba(245,158,11,.08)); }
    .head { display: flex; justify-content: space-between; gap: 12px; }
    .eyebrow { color: var(--vscode-descriptionForeground); text-transform: uppercase; letter-spacing: .08em; font-size: 10px; font-weight: 700; }
    h2 { margin: 3px 0 0; font-size: 17px; }
    p { margin: 8px 0 0; color: var(--vscode-descriptionForeground); }
    .pill { height: fit-content; border: 1px solid currentColor; border-radius: 999px; padding: 3px 8px; font-weight: 800; font-size: 10px; }
    .pill.low { color: #22c55e; }
    .pill.medium { color: #f59e0b; }
    .pill.high { color: #ef4444; }
    .mini, .files { margin-top: 10px; display: grid; gap: 4px; }
    .mini span { color: var(--vscode-descriptionForeground); }
    .mini.warn { border-color: rgba(245,158,11,.45); }
    .files div { display: flex; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--vscode-panel-border); padding: 4px 0; }
    .files div:last-child { border-bottom: 0; }
    .files span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .diff-review { display: grid; gap: 10px; }
    .diff-header { position: sticky; top: 0; z-index: 1; margin: -14px -14px 0; padding: 12px 14px; border-bottom: 1px solid var(--vscode-panel-border); background: var(--vscode-sideBar-background); }
    .diff-header p { font-size: 11px; }
    .diff-actions { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 7px; margin-top: 10px; }
    .review { display: grid; gap: 8px; }
    .card.warn { border-color: rgba(245,158,11,.5); }
    .card.error { border-color: rgba(239,68,68,.55); white-space: pre-wrap; }
    .kicker { color: var(--vscode-descriptionForeground); text-transform: uppercase; letter-spacing: .08em; font-size: 10px; font-weight: 800; }
    .title { margin-top: 3px; font-weight: 800; }
    .meta { color: var(--vscode-descriptionForeground); }
    .diff { border: 1px solid var(--vscode-panel-border); border-radius: 12px; overflow: hidden; font-family: var(--vscode-editor-font-family); font-size: 11px; }
    .line { white-space: pre-wrap; padding: 2px 8px; min-height: 1.45em; display: flex; gap: 8px; align-items: flex-start; }
    .line > span { flex: 1 1 auto; min-width: 0; }
    .line.add { background: rgba(34,197,94,.14); color: #86efac; }
    .line.del { background: rgba(239,68,68,.14); color: #fca5a5; }
    .line.hunk { background: rgba(59,130,246,.16); color: #93c5fd; font-weight: 700; }
    .line.meta { background: rgba(148,163,184,.10); color: var(--vscode-descriptionForeground); }
    .line.ctx { color: var(--vscode-editor-foreground); }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    .primary-actions { grid-template-columns: 1fr; }
    button { width: 100%; border: 1px solid var(--vscode-button-border, transparent); border-radius: 9px; padding: 8px 10px; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); cursor: pointer; font: inherit; font-weight: 650; }
    button.primary, button.accept { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
    button.danger { color: var(--vscode-errorForeground); }
    button:hover { filter: brightness(1.08); }
    .line .line-edit { width: auto; flex: 0 0 auto; margin-top: 1px; padding: 1px 7px; border-radius: 999px; visibility: hidden; opacity: 0; font-size: 10px; line-height: 1.35; transition: opacity .12s ease; }
    .line:not(.add):not(.ctx) .line-edit { display: none; }
    .line:hover .line-edit, .line .line-edit:focus { visibility: visible; opacity: 1; }
    .error { border-color: rgba(239,68,68,.45); }
    .ok { border-color: rgba(34,197,94,.35); }
  </style>
</head>
<body>
  ${body}
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.addEventListener("click", event => {
      const editButton = event.target.closest("button[data-edit-line]");
      if (editButton) {
        vscode.postMessage({
          type: "edit-line",
          file: editButton.dataset.file || "",
          line: Number(editButton.dataset.line || 0),
          expectedText: editButton.dataset.expected || "",
        });
        return;
      }
      const button = event.target.closest("button[data-command]");
      if (!button) return;
      vscode.postMessage({ command: button.dataset.command });
    });
  </script>
</body>
</html>`;
  }
}

async function updateSmartTeXSetting(key, value) {
  const allowed = new Set([
    "autoSaveAndSync",
    "syncOnSave",
    "autoSaveDebounceMs",
    "previewFollowCursor",
    "previewClickToCode",
    "previewTheme",
    "annotationCodeLens",
    "showResolvedAnnotations",
  ]);
  if (!allowed.has(key)) return;
  await config().update(key, value, vscode.ConfigurationTarget.Workspace);
  if (key === "annotationCodeLens") annotationCodeLensProvider?.refresh();
}

class SmartTeXSettingsProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
    this.runtime = { checked: false, ok: false, message: "Not checked" };
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage(command(async message => {
      const type = String(message?.type || "");
      if (type === "set") {
        await updateSmartTeXSetting(String(message.key || ""), message.value);
        this.refresh();
        dashboardProvider?.refresh();
        if (String(message.key || "") === "showResolvedAnnotations") await refreshAnnotationsQuietly();
        return;
      }
      if (type === "refresh-runtime") {
        await this.refreshRuntime();
        return;
      }
      if (type === "command") {
        const commandId = String(message.command || "");
        if (commandId === "workbench.action.openSettings") {
          await vscode.commands.executeCommand(commandId, message.query || "@ext:smarttex.smarttex");
        } else if (["smarttex.startLocalAgent", "smarttex.login", "smarttex.addQuickAnnotation"].includes(commandId)) {
          await vscode.commands.executeCommand(commandId);
        }
      }
    }));
    this.refreshRuntime().catch(() => this.refresh());
  }

  async refreshRuntime() {
    const secret = localBridgeSecret();
    if (!secret) {
      this.runtime = { checked: true, ok: false, message: "Bridge secret is missing. Run SmartTeX login or start the agent." };
      this.refresh();
      return;
    }
    try {
      const ok = await checkLocalBridge(secret);
      this.runtime = { checked: true, ok, message: ok ? "Local agent is reachable" : "Local agent is not reachable" };
    } catch (err) {
      this.runtime = { checked: true, ok: false, message: err.message };
    }
    this.refresh();
  }

  refresh() {
    if (!this.view) return;
    this.view.webview.html = this.renderHtml();
  }

  renderHtml() {
    const nonce = String(Date.now());
    const checked = key => {
      if (key === "autoSaveAndSync") return autoSaveAndSync();
      if (key === "syncOnSave") return syncOnSave();
      if (key === "previewFollowCursor") return previewFollowCursor();
      if (key === "previewClickToCode") return previewClickToCode();
      if (key === "annotationCodeLens") return annotationCodeLensEnabled();
      if (key === "showResolvedAnnotations") return showResolvedAnnotations();
      return Boolean(config().get(key));
    };
    const boolRow = (key, label, hint) => `
      <label class="row">
        <span><strong>${escapeHtml(label)}</strong><em>${escapeHtml(hint)}</em></span>
        <input type="checkbox" data-setting="${escapeHtml(key)}" ${checked(key) ? "checked" : ""}>
      </label>`;
    const templates = quickAnnotationTemplates();
    const runtimeClass = this.runtime.ok ? "ok" : "warn";
    const theme = previewTheme();
    return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <style nonce="${nonce}">
    body { margin: 0; padding: 14px; color: var(--vscode-foreground); font: 12px/1.45 var(--vscode-font-family); }
    .hero, .section { border: 1px solid var(--vscode-panel-border); border-radius: 12px; padding: 13px; background: var(--vscode-sideBar-background); }
    .hero { background: linear-gradient(135deg, rgba(59,130,246,.13), rgba(34,197,94,.08)); }
    h2 { margin: 0; font-size: 17px; }
    h3 { margin: 0 0 9px; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--vscode-descriptionForeground); }
    p { margin: 6px 0 0; color: var(--vscode-descriptionForeground); }
    .pill { display: inline-block; margin-top: 10px; border: 1px solid currentColor; border-radius: 999px; padding: 3px 8px; font-weight: 800; font-size: 10px; }
    .pill.ok { color: #22c55e; background: rgba(34,197,94,.10); }
    .pill.warn { color: #f59e0b; background: rgba(245,158,11,.10); }
    .section { margin-top: 10px; display: grid; gap: 8px; }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--vscode-panel-border); }
    .row:last-child { border-bottom: 0; }
    .row span { min-width: 0; }
    .row strong { display: block; }
    .row em { display: block; color: var(--vscode-descriptionForeground); font-style: normal; font-size: 11px; }
    input[type="checkbox"] { width: 18px; height: 18px; flex: 0 0 auto; }
    input[type="number"], select { width: 108px; color: var(--vscode-input-foreground); background: var(--vscode-input-background); border: 1px solid var(--vscode-input-border, var(--vscode-panel-border)); border-radius: 7px; padding: 5px 7px; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    button { width: 100%; border: 1px solid var(--vscode-button-border, transparent); border-radius: 9px; padding: 8px 10px; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); cursor: pointer; font: inherit; font-weight: 650; }
    button.primary { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
    code { color: var(--vscode-textLink-foreground); }
  </style>
</head>
<body>
  <section class="hero">
    <h2>SmartTeX Controls</h2>
    <p>${escapeHtml(this.runtime.message)}</p>
    <span class="pill ${runtimeClass}">${this.runtime.ok ? "Agent online" : "Needs attention"}</span>
    <div class="actions">
      <button class="primary" data-command="smarttex.startLocalAgent">Start agent</button>
      <button data-action="refresh-runtime">Check</button>
    </div>
  </section>
  <section class="section">
    <h3>Sync</h3>
    ${boolRow("autoSaveAndSync", "Auto-save and sync", "Save changed SmartTeX files after a short debounce and sync to server.")}
    ${boolRow("syncOnSave", "Sync on save", "Run workspace sync when files are saved.")}
    <label class="row"><span><strong>Auto-save debounce</strong><em>Delay before saving and syncing changed files.</em></span><input type="number" min="250" max="10000" step="250" data-setting="autoSaveDebounceMs" value="${escapeHtml(autoSaveDebounceMs())}"></label>
  </section>
  <section class="section">
    <h3>Preview</h3>
    ${boolRow("previewFollowCursor", "Follow cursor", "Keep local preview aligned with the active editor line.")}
    ${boolRow("previewClickToCode", "Click to code", "Click preview text to navigate back to source.")}
    <label class="row"><span><strong>Theme</strong><em>Local Typst preview color mode.</em></span><select data-setting="previewTheme">${["auto", "light", "dark"].map(item => `<option value="${item}" ${item === theme ? "selected" : ""}>${item}</option>`).join("")}</select></label>
  </section>
  <section class="section">
    <h3>Annotations</h3>
    ${boolRow("annotationCodeLens", "Inline actions", "Show annotation actions above annotated lines.")}
    ${boolRow("showResolvedAnnotations", "Show resolved", "Include done and dismissed annotations in views and decorations.")}
    <p>Quick templates: <code>${escapeHtml(templates.length ? `${templates.length} configured` : "none")}</code></p>
    <div class="actions">
      <button data-command="workbench.action.openSettings" data-query="smarttex.quickAnnotationTemplates">Edit templates</button>
      <button data-command="smarttex.addQuickAnnotation">Quick note</button>
    </div>
  </section>
  <section class="section">
    <h3>Paths</h3>
    <p>Server: <code>${escapeHtml(serverUrl())}</code></p>
    <p>Agent: <code>${escapeHtml(agentPath())}</code></p>
    <p>Workspace: <code>${escapeHtml(workspaceRoot())}</code></p>
    <p>Bridge: <code>${escapeHtml(localBridgeUrl())}</code></p>
    <div class="actions">
      <button data-command="workbench.action.openSettings" data-query="@ext:smarttex.smarttex">VS Code settings</button>
      <button data-command="smarttex.login">Login</button>
    </div>
  </section>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.addEventListener("change", event => {
      const target = event.target.closest("[data-setting]");
      if (!target) return;
      let value = target.type === "checkbox" ? target.checked : target.value;
      if (target.type === "number") value = Number(value);
      vscode.postMessage({ type: "set", key: target.dataset.setting, value });
    });
    document.addEventListener("click", event => {
      const refresh = event.target.closest("[data-action='refresh-runtime']");
      if (refresh) { vscode.postMessage({ type: "refresh-runtime" }); return; }
      const button = event.target.closest("button[data-command]");
      if (!button) return;
      vscode.postMessage({ type: "command", command: button.dataset.command, query: button.dataset.query || "" });
    });
  </script>
</body>
</html>`;
  }
}

async function refreshAnnotations() {
  await annotationsProvider?.load();
}

async function refreshAiChanges() {
  await aiChangesProvider?.load();
}

async function refreshAll() {
  updateStatus();
  await vscode.window.withProgress({
    location: vscode.ProgressLocation.Window,
    title: "Refreshing SmartTeX...",
  }, async () => {
    await Promise.all([
      loadWorkspaceStatus(projectIdFromWorkspace(), { quiet: true }).catch(err => output?.appendLine(`Workspace status refresh failed: ${err.message}`)),
      loadPdfEmbeds({ quiet: true }).catch(err => output?.appendLine(`PDF embeds refresh failed: ${err.message}`)),
      refreshAnnotationsQuietly().catch(err => output?.appendLine(`Annotations refresh failed: ${err.message}`)),
      aiChangesProvider?.load().catch(err => output?.appendLine(`AI changes refresh failed: ${err.message}`)),
      historyProvider?.load().catch(err => output?.appendLine(`History refresh failed: ${err.message}`)),
      settingsProvider?.refreshRuntime().catch(err => output?.appendLine(`Runtime refresh failed: ${err.message}`)),
    ]);
  });
  dashboardProvider?.refresh();
  problemsProvider?.refresh();
  vscode.window.showInformationMessage("SmartTeX refreshed");
}

async function loadProposalDiff(projectId) {
  const stdout = await runAgent(["proposals", "diff", ...apiArgs(projectId)], { reveal: false });
  return parseAgentJSON(stdout) || {};
}

function renderDiffText(diffText) {
  const lines = String(diffText || "").split(/\r?\n/);
  if (!lines.length || (lines.length === 1 && !lines[0])) return `<div class="empty">No diff text.</div>`;
  let currentFile = "";
  let oldLine = 0;
  let newLine = 0;
  return lines.map(line => {
    if (line.startsWith("+++ ")) {
      currentFile = line.replace(/^\+\+\+\s+b\//, "").replace(/^\+\+\+\s+/, "").trim();
    }
    const hunkMatch = line.match(/^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
    if (hunkMatch) {
      oldLine = Number(hunkMatch[1] || 0);
      newLine = Number(hunkMatch[2] || 0);
    }
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "add"
      : line.startsWith("-") && !line.startsWith("---") ? "del"
        : line.startsWith("@@") ? "hunk"
          : line.startsWith("diff ") || line.startsWith("+++") || line.startsWith("---") || line.startsWith("index ") ? "meta"
            : "ctx";
    let editableLine = 0;
    let expectedText = "";
    if (cls === "add") {
      editableLine = newLine;
      expectedText = line.slice(1);
      newLine += 1;
    } else if (cls === "ctx") {
      editableLine = newLine;
      expectedText = line.startsWith(" ") ? line.slice(1) : line;
      oldLine += 1;
      newLine += 1;
    } else if (cls === "del") {
      oldLine += 1;
    }
    const canEdit = currentFile && editableLine > 0 && (cls === "add" || cls === "ctx") && !currentFile.startsWith("/dev/null");
    const editButton = canEdit
      ? `<button class="line-edit" data-edit-line="1" data-file="${escapeHtml(currentFile)}" data-line="${escapeHtml(editableLine)}" data-expected="${escapeHtml(expectedText)}">Edit</button>`
      : "";
    return `<div class="line ${cls}"><span>${escapeHtml(line || " ")}</span>${editButton}</div>`;
  }).join("");
}

function aiChangesDiffBody(projectId, proposal, diffPayload) {
  const summary = diffPayload.semantic_diff_summary || proposal?.semantic_diff_summary || {};
  const warnings = diffPayload.smcl_warnings || proposal?.smcl_warnings || [];
  const risk = diffPayload.smcl_risk_level || proposal?.smcl_risk_level || "low";
  const compileError = diffPayload.compile_error_summary || "";
  return `
    <section class="diff-header">
      <div>
        <h2>AI Changes #${escapeHtml(diffPayload.proposal_id || proposal?.id || "")}</h2>
        <p>Project #${escapeHtml(projectId)} · ${escapeHtml(proposalStatusLabel(diffPayload.status || proposal?.status))} · risk ${escapeHtml(risk)}</p>
      </div>
      <div class="diff-actions">
        <button data-command="smarttex.refreshAiChanges">Refresh</button>
        <button class="primary" data-command="smarttex.acceptAiChanges">Accept</button>
        <button class="danger" data-command="smarttex.discardAiChanges">Discard</button>
      </div>
    </section>
    <section class="review">
      ${summary?.title ? `<article class="card"><div class="kicker">Semantic diff summary</div><div class="title">${escapeHtml(summary.title)}</div><div class="meta">${escapeHtml(summary.impact || "")}</div></article>` : ""}
      ${warnings.length ? `<article class="card warn"><div class="kicker">Safety review</div><div class="title">${escapeHtml(warnings[0]?.human_title || warnings[0]?.message || warnings[0]?.code || `${warnings.length} warnings`)}</div><div class="meta">${escapeHtml(warnings[0]?.human_detail || warnings[0]?.message || "")}</div></article>` : ""}
      ${compileError ? `<article class="card error"><div class="kicker">Compile output</div>${escapeHtml(compileError)}</article>` : ""}
    </section>
    <section class="diff">${renderDiffText(diffPayload.diff_text || "")}</section>`;
}

async function openAiChanges() {
  await aiChangesProvider?.openDiff();
}

async function manualEditAiProposalLine(message) {
  const projectId = projectIdFromWorkspace() || await askProjectId();
  const fileName = String(message?.file || "").trim();
  const lineNumber = Number(message?.line || 0);
  const expectedText = String(message?.expectedText ?? "");
  if (!projectId || !fileName || !lineNumber) {
    vscode.window.showWarningMessage("SmartTeX: could not identify the proposal line to edit.");
    return;
  }
  const newText = await vscode.window.showInputBox({
    title: "Edit AI proposal line",
    prompt: `${fileName}:${lineNumber}`,
    value: expectedText,
    validateInput: value => String(value ?? "").includes("\n") ? "Only one line can be edited here. Use the web diff for larger changes." : null,
  });
  if (newText === undefined) return;
  await vscode.window.withProgress({
    location: vscode.ProgressLocation.Notification,
    title: `Updating AI proposal line ${lineNumber}`,
  }, async () => {
    await runAgent([
      "proposals", "edit-line",
      ...apiArgs(projectId),
      "--file", fileName,
      "--line", String(lineNumber),
      "--expected-text", expectedText,
      "--new-text", newText,
    ], { reveal: false });
  });
  vscode.window.showInformationMessage(`SmartTeX: proposal line updated in ${path.basename(fileName)}:${lineNumber}`);
  if (aiChangesProvider) {
    aiChangesProvider.loading = true;
    aiChangesProvider.refresh();
    try {
      await aiChangesProvider.load().catch(() => {});
      aiChangesProvider.diffPayload = await loadProposalDiff(projectId);
    } finally {
      aiChangesProvider.loading = false;
      aiChangesProvider.refresh();
      dashboardProvider?.refresh();
    }
  }
}

async function acceptAiChanges() {
  const projectId = projectIdFromWorkspace() || await askProjectId();
  if (!projectId) return;
  const proposal = aiChangesProvider?.proposal || await aiChangesProvider?.load().catch(() => null);
  if (!proposalVisible(proposal)) {
    vscode.window.showInformationMessage("SmartTeX: no AI proposal to accept.");
    return;
  }
  const label = proposal.status === "failed_compile" ? "Accept with compile errors" : "Accept";
  const choice = await vscode.window.showWarningMessage("Accept proposed AI changes and sync the project?", { modal: true }, label);
  if (choice !== label) return;
  const args = ["proposals", "accept", ...apiArgs(projectId)];
  if (proposal.status === "failed_compile") args.push("--accept-compile-errors");
  await runAgent(args, { reveal: false });
  vscode.window.showInformationMessage("SmartTeX AI changes accepted");
  if (aiChangesProvider) aiChangesProvider.diffPayload = null;
  await Promise.all([
    aiChangesProvider?.load().catch(() => {}),
    refreshAnnotationsQuietly().catch(() => {}),
  ]);
  await vscode.window.withProgress({
    location: vscode.ProgressLocation.Notification,
    title: "Pulling accepted SmartTeX changes",
  }, () => runAgent(["workspace", "pull", ...commonArgs(projectId), "--force"], { reveal: false }));
  updateStatus();
}

async function discardAiChanges() {
  const projectId = projectIdFromWorkspace() || await askProjectId();
  if (!projectId) return;
  const proposal = aiChangesProvider?.proposal || await aiChangesProvider?.load().catch(() => null);
  if (!proposalVisible(proposal)) {
    vscode.window.showInformationMessage("SmartTeX: no AI proposal to discard.");
    return;
  }
  const choice = await vscode.window.showWarningMessage("Discard proposed AI changes?", { modal: true }, "Discard");
  if (choice !== "Discard") return;
  await runAgent(["proposals", "discard", ...apiArgs(projectId)], { reveal: false });
  vscode.window.showInformationMessage("SmartTeX AI changes discarded");
  if (aiChangesProvider) aiChangesProvider.diffPayload = null;
  await aiChangesProvider?.load().catch(() => {});
}

async function refreshHistory() {
  await historyProvider?.load();
}

async function loadVersionDetail(projectId, versionId) {
  const stdout = await runAgent(["versions", "detail", ...apiArgs(projectId), "--id", String(versionId)], { reveal: false });
  return parseAgentJSON(stdout) || {};
}

function versionDiffHtml(projectId, version) {
  const nonce = String(Date.now());
  const number = version.number ?? version.id;
  const op = historyOperationLabel(version.operation);
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <style nonce="${nonce}">
    body { margin: 0; color: var(--vscode-foreground); background: var(--vscode-editor-background); font: 13px/1.5 var(--vscode-font-family); }
    header { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--vscode-panel-border); background: var(--vscode-editor-background); }
    h1 { margin: 0; font-size: 17px; }
    .subtitle { color: var(--vscode-descriptionForeground); font-size: 12px; }
    .actions { display: flex; gap: 8px; }
    button { border: 1px solid var(--vscode-button-border, transparent); border-radius: 8px; padding: 7px 11px; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); cursor: pointer; font: inherit; font-weight: 700; }
    button.primary { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
    main { padding: 16px 18px 24px; }
    .meta { border: 1px solid var(--vscode-panel-border); border-radius: 12px; padding: 12px 14px; margin-bottom: 12px; background: var(--vscode-sideBar-background); color: var(--vscode-descriptionForeground); }
    .diff { border: 1px solid var(--vscode-panel-border); border-radius: 12px; overflow: hidden; font-family: var(--vscode-editor-font-family); font-size: var(--vscode-editor-font-size); }
    .line { white-space: pre-wrap; padding: 1px 12px; min-height: 1.45em; }
    .line.add { background: rgba(34,197,94,.14); color: #86efac; }
    .line.del { background: rgba(239,68,68,.14); color: #fca5a5; }
    .line.hunk { background: rgba(59,130,246,.16); color: #93c5fd; font-weight: 700; }
    .line.meta { background: rgba(148,163,184,.10); color: var(--vscode-descriptionForeground); }
    .line.ctx { color: var(--vscode-editor-foreground); }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Version #${escapeHtml(number)} · ${escapeHtml(op)}</h1>
      <div class="subtitle">Project #${escapeHtml(projectId)}${version.target_file ? ` · ${escapeHtml(version.target_file)}` : ""}${version.created_at ? ` · ${escapeHtml(shortDateLabel(version.created_at))}` : ""}</div>
    </div>
    <div class="actions">
      <button data-command="smarttex.refreshHistory">Refresh</button>
      ${version.is_revertible ? `<button class="primary" data-command="smarttex.rollbackHistoryVersion">Rollback</button>` : ""}
    </div>
  </header>
  <main>
    <section class="meta">${escapeHtml(version.summary || version.actor || "Project version snapshot")}</section>
    <section class="diff">${renderDiffText(version.diff || "")}</section>
  </main>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.addEventListener("click", event => {
      const button = event.target.closest("button[data-command]");
      if (!button) return;
      vscode.postMessage({ command: button.dataset.command, versionId: ${JSON.stringify(version.id)} });
    });
  </script>
</body>
</html>`;
}

async function openHistoryVersion(item) {
  const version = item?.version || item;
  const versionId = Number(version?.id || 0);
  const projectId = projectIdFromWorkspace() || await askProjectId();
  if (!projectId || !versionId) return;
  const detail = await loadVersionDetail(projectId, versionId);
  if (!historyPanel) {
    historyPanel = vscode.window.createWebviewPanel("smarttex.versionDiff", `SmartTeX Version #${detail.number ?? detail.id}`, vscode.ViewColumn.Beside, {
      enableScripts: true,
      retainContextWhenHidden: true,
    });
    historyPanel.onDidDispose(() => { historyPanel = null; });
    historyPanel.webview.onDidReceiveMessage(command(async message => {
      const commandId = String(message?.command || "");
      if (commandId === "smarttex.refreshHistory") {
        await refreshHistory();
      } else if (commandId === "smarttex.rollbackHistoryVersion") {
        const target = (historyProvider?.items || []).find(item => Number(item.id) === Number(message.versionId)) || { id: message.versionId, is_revertible: true };
        await rollbackHistoryVersion(target);
      }
    }));
  }
  historyPanel.title = `SmartTeX Version #${detail.number ?? detail.id}`;
  historyPanel.webview.html = versionDiffHtml(projectId, detail);
  historyPanel.reveal(vscode.ViewColumn.Beside);
}

async function rollbackHistoryVersion(item) {
  const version = item?.version || item;
  const versionId = Number(version?.id || 0);
  const projectId = projectIdFromWorkspace() || await askProjectId();
  if (!projectId || !versionId) return;
  if (version.is_revertible === false) {
    vscode.window.showWarningMessage("SmartTeX: this version cannot be rolled back automatically.");
    return;
  }
  const number = version.number ?? version.id ?? versionId;
  const target = version.target_file || version.target || "project";
  const choice = await vscode.window.showWarningMessage(
    `Roll back ${target} to version #${number}? Current server content will be replaced.`,
    { modal: true },
    "Rollback",
  );
  if (choice !== "Rollback") return;
  await runAgent([
    "versions", "rollback",
    ...apiArgs(projectId),
    "--id", String(versionId),
    "--summary", `Rollback to version ${number}`,
  ], { reveal: false });
  await vscode.window.withProgress({
    location: vscode.ProgressLocation.Notification,
    title: "Pulling rolled back SmartTeX files",
  }, () => runAgent(["workspace", "pull", ...commonArgs(projectId), "--force"], { reveal: false }));
  updateStatus();
  await Promise.all([
    historyProvider?.load().catch(() => {}),
    aiChangesProvider?.load().catch(() => {}),
    refreshAnnotationsQuietly().catch(() => {}),
  ]);
  vscode.window.showInformationMessage(`SmartTeX rolled back to version #${number}`);
}

async function refreshAnnotationsQuietly() {
  if (!annotationsProvider || annotationRefreshInFlight || !projectIdFromWorkspace()) return;
  annotationRefreshInFlight = true;
  try {
    await annotationsProvider.load({ quiet: true });
  } catch (err) {
    output?.appendLine(`Annotations refresh failed: ${err.message}`);
  } finally {
    annotationRefreshInFlight = false;
  }
}

function restartAnnotationRefreshTimer() {
  clearInterval(annotationRefreshTimer);
  annotationRefreshTimer = null;
  if (!projectIdFromWorkspace()) return;
  annotationRefreshTimer = setInterval(() => {
    refreshAnnotationsQuietly().catch(err => output?.appendLine(`Annotations refresh failed: ${err.message}`));
  }, annotationRefreshIntervalMs());
}

async function openAnnotation(item) {
  const annotation = item?.annotation || item;
  const fileName = String(annotation?.file_name || "");
  if (!fileName) return;
  const fullPath = path.join(activeWorkspaceFolder(), filepathFromProjectPath(fileName));
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(fullPath));
  const editor = await vscode.window.showTextDocument(document, {
    preview: false,
    viewColumn: lastTextEditorViewColumn || vscode.ViewColumn.One,
  });
  const line = Math.max(0, Number(annotation.line_start || 1) - 1);
  const endLine = Math.max(line, Number(annotation.line_end || annotation.line_start || 1) - 1);
  const range = new vscode.Range(line, 0, endLine, document.lineAt(Math.min(endLine, document.lineCount - 1)).text.length);
  editor.selection = new vscode.Selection(range.start, range.end);
  editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
}

function filepathFromProjectPath(value) {
  return String(value || "").split("/").filter(Boolean).join(path.sep);
}

function currentAnnotationTarget(projectId) {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.uri.scheme !== "file") {
    vscode.window.showWarningMessage("Open a project file before adding an annotation.");
    return null;
  }
  const rel = workspaceRelativePath(editor.document.uri.fsPath);
  if (!rel || rel.startsWith(".smarttex/")) {
    vscode.window.showWarningMessage("This file is outside the SmartTeX workspace.");
    return null;
  }
  const selection = editor.selection;
  const lineStart = selection.isEmpty ? selection.active.line + 1 : selection.start.line + 1;
  const lineEnd = selection.isEmpty ? lineStart : selection.end.line + 1;
  const selectedText = selection.isEmpty ? editor.document.lineAt(selection.active.line).text : editor.document.getText(selection);
  return { projectId, rel, lineStart, lineEnd, selectedText };
}

async function createAnnotationFromTarget(target, instruction) {
  const text = String(instruction || "").trim();
  if (!target || !text) return false;
  await runAgent([
    "annotations", "add",
    ...apiArgs(target.projectId),
    "--file", target.rel,
    "--line", String(target.lineStart),
    "--line-end", String(target.lineEnd),
    "--selected-text", target.selectedText,
    "--text", text,
  ], { reveal: false });
  vscode.window.showInformationMessage("SmartTeX annotation added");
  await refreshAnnotations();
  fileAnnotationsProvider?.refresh();
  return true;
}

async function addAnnotation() {
  const projectId = projectIdFromWorkspace() || await askProjectId();
  if (!projectId) return;
  const target = currentAnnotationTarget(projectId);
  if (!target) return;
  const instruction = await vscode.window.showInputBox({
    title: "Add SmartTeX annotation",
    prompt: `${target.rel}:${target.lineStart}${target.lineEnd !== target.lineStart ? `-${target.lineEnd}` : ""}`,
    validateInput: value => String(value || "").trim() ? null : "Annotation text is required.",
  });
  if (!instruction) return;
  await createAnnotationFromTarget(target, instruction);
}

async function addQuickAnnotation() {
  const projectId = projectIdFromWorkspace() || await askProjectId();
  if (!projectId) return;
  const target = currentAnnotationTarget(projectId);
  if (!target) return;
  const templates = quickAnnotationTemplates();
  const items = [
    { label: "$(edit) Custom annotation...", description: "Write a one-off note", value: "" },
    ...templates.map(text => ({ label: text, description: "Quick annotation template", value: text })),
  ];
  const picked = await vscode.window.showQuickPick(items, {
    title: "Add SmartTeX quick annotation",
    placeHolder: `${target.rel}:${target.lineStart}${target.lineEnd !== target.lineStart ? `-${target.lineEnd}` : ""}`,
    matchOnDescription: true,
  });
  if (!picked) return;
  let instruction = picked.value;
  if (!instruction) {
    instruction = await vscode.window.showInputBox({
      title: "Add SmartTeX annotation",
      prompt: `${target.rel}:${target.lineStart}${target.lineEnd !== target.lineStart ? `-${target.lineEnd}` : ""}`,
      validateInput: value => String(value || "").trim() ? null : "Annotation text is required.",
    });
  }
  await createAnnotationFromTarget(target, instruction);
}

async function updateAnnotationStatus(item, status) {
  const annotation = item?.annotation || item;
  const projectId = projectIdFromWorkspace() || await askProjectId();
  const id = Number(annotation?.id || 0);
  if (!projectId || !id) return;
  await runAgent(["annotations", "update", ...apiArgs(projectId), "--id", String(id), "--status", status], { reveal: false });
  await refreshAnnotations();
  fileAnnotationsProvider?.refresh();
}

function updateStatus(mode = "") {
  if (!statusBar) return;
  const state = readWorkspaceState();
  if (!state?.project_id) {
    statusBar.text = "$(file-code) SmartTeX";
    statusBar.tooltip = "Open or manage a SmartTeX local workspace";
    lastWorkspaceStatus = null;
    dashboardProvider?.refresh();
    return;
  }
  const workspaceClass = workspaceStatusClass(lastWorkspaceStatus?.project_id === Number(state.project_id) ? lastWorkspaceStatus : null);
  const icon = mode === "syncing" ? "sync~spin" : workspaceClass === "bad" ? "error" : workspaceClass === "warn" ? "warning" : "sync";
  statusBar.text = `$(${icon}) SmartTeX #${state.project_id}`;
  statusBar.tooltip = [
    `Project #${state.project_id}`,
    `Status: ${workspaceStatusLabel(lastWorkspaceStatus?.project_id === Number(state.project_id) ? lastWorkspaceStatus : null)}`,
    `Server: ${state.server || serverUrl()}`,
    state.workspace_id ? `Workspace: ${state.workspace_id}` : "",
    state.base_version_number ? `Base version: ${state.base_version_number}` : "",
    lastWorkspaceStatus?.local_unsynced_changes ? `Unsynced changes: ${lastWorkspaceStatus.local_unsynced_changes}` : "",
    lastWorkspaceStatus?.server_latest_version ? `Server version: ${lastWorkspaceStatus.server_latest_version}` : "",
  ].filter(Boolean).join("\n");
  dashboardProvider?.refresh();
}

function watchStateFile(context) {
  stateWatcher?.dispose();
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    stateWatcher = null;
    return;
  }
  const pattern = new vscode.RelativePattern(folder, ".smarttex/local_workspace_state.json");
  stateWatcher = vscode.workspace.createFileSystemWatcher(pattern);
  context.subscriptions.push(stateWatcher);
  const refreshWorkspaceState = () => {
    updateStatus();
    loadWorkspaceStatus(projectIdFromWorkspace(), { quiet: true }).catch(() => {});
  };
  stateWatcher.onDidChange(refreshWorkspaceState);
  stateWatcher.onDidCreate(refreshWorkspaceState);
  stateWatcher.onDidDelete(refreshWorkspaceState);
}

function command(handler, options = {}) {
  return async (...args) => {
    try {
      return await handler(...args);
    } catch (err) {
      const message = String(err?.message || err || "Unknown SmartTeX error");
      output?.appendLine(`SmartTeX command failed: ${message}`);
      if (options.authRecovery !== false && isAuthError(err)) {
        return recoverAuthAndRetry(err, () => handler(...args));
      }
      const choice = await vscode.window.showWarningMessage(`SmartTeX: ${message}`, "Show Output");
      if (choice === "Show Output") output?.show(true);
      return undefined;
    }
  };
}

function activate(context) {
  extensionPath = context.extensionPath;
  output = vscode.window.createOutputChannel("SmartTeX");
  compileOutput = vscode.window.createOutputChannel("SmartTeX Compile");
  compileDiagnostics = vscode.languages.createDiagnosticCollection("SmartTeX");
  createAnnotationDecorationTypes(context);
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = "smarttex.workspaceStatus";
  statusBar.show();
  context.subscriptions.push(output, compileOutput, compileDiagnostics, statusBar);

  context.subscriptions.push(
    vscode.commands.registerCommand("smarttex.login", command(runLoginFlow, { authRecovery: false })),
    vscode.commands.registerCommand("smarttex.openProject", command(openProject)),
    vscode.commands.registerCommand("smarttex.watchWorkspace", command(async () => {
      const projectId = await askProjectId();
      if (projectId) showTerminal(`SmartTeX watch #${projectId}`, ["workspace", "watch", ...commonArgs(projectId)]);
    })),
    vscode.commands.registerCommand("smarttex.syncWorkspace", command(() => runWorkspaceCommand("SmartTeX sync", "sync"))),
    vscode.commands.registerCommand("smarttex.pullWorkspace", command(() => runWorkspaceCommand("SmartTeX pull", "pull"))),
    vscode.commands.registerCommand("smarttex.releaseWorkspace", command(() => runWorkspaceCommand("SmartTeX release", "release"))),
    vscode.commands.registerCommand("smarttex.workspaceStatus", command(workspaceStatus)),
    vscode.commands.registerCommand("smarttex.refreshAll", command(refreshAll)),
    vscode.commands.registerCommand("smarttex.compile", command(compileProject)),
    vscode.commands.registerCommand("smarttex.showCompileLog", command(() => compileOutput?.show(true))),
    vscode.commands.registerCommand("smarttex.showProblems", command(showProblems, { authRecovery: false })),
    vscode.commands.registerCommand("smarttex.refreshPdfEmbeds", command(refreshPdfEmbeds)),
    vscode.commands.registerCommand("smarttex.enablePdfEmbed", command(enablePdfEmbed)),
    vscode.commands.registerCommand("smarttex.disablePdfEmbed", command(disablePdfEmbed)),
    vscode.commands.registerCommand("smarttex.insertPdfEmbedSnippet", command(insertPdfEmbedSnippet)),
    vscode.commands.registerCommand("smarttex.openWebEditor", command(openWebEditor)),
    vscode.commands.registerCommand("smarttex.openPreview", command(openPreview)),
    vscode.commands.registerCommand("smarttex.startLocalAgent", command(startLocalAgent)),
    vscode.commands.registerCommand("smarttex.refreshAnnotations", command(refreshAnnotations)),
    vscode.commands.registerCommand("smarttex.addAnnotation", command(addAnnotation)),
    vscode.commands.registerCommand("smarttex.addQuickAnnotation", command(addQuickAnnotation)),
    vscode.commands.registerCommand("smarttex.openAnnotation", command(openAnnotation)),
    vscode.commands.registerCommand("smarttex.markAnnotationDone", command(item => updateAnnotationStatus(item, "done"))),
    vscode.commands.registerCommand("smarttex.dismissAnnotation", command(item => updateAnnotationStatus(item, "dismissed"))),
    vscode.commands.registerCommand("smarttex.keepAiAnnotation", command(item => updateAnnotationStatus(item, "open"))),
    vscode.commands.registerCommand("smarttex.refreshAiChanges", command(refreshAiChanges)),
    vscode.commands.registerCommand("smarttex.openAiChanges", command(openAiChanges)),
    vscode.commands.registerCommand("smarttex.acceptAiChanges", command(acceptAiChanges)),
    vscode.commands.registerCommand("smarttex.discardAiChanges", command(discardAiChanges)),
    vscode.commands.registerCommand("smarttex.refreshHistory", command(refreshHistory)),
    vscode.commands.registerCommand("smarttex.openHistoryVersion", command(openHistoryVersion)),
    vscode.commands.registerCommand("smarttex.rollbackHistoryVersion", command(rollbackHistoryVersion)),
  );

  annotationsProvider = new AnnotationsProvider();
  dashboardProvider = new SmartTeXDashboardProvider(context);
  previewProvider = new SmartTeXPreviewProvider(context);
  problemsProvider = new SmartTeXProblemsProvider(context);
  pdfEmbedsProvider = new SmartTeXPdfEmbedsProvider(context);
  aiChangesProvider = new AiChangesProvider(context);
  fileAnnotationsProvider = new FileAnnotationsProvider(context);
  historyProvider = new HistoryProvider();
  settingsProvider = new SmartTeXSettingsProvider(context);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("smarttex.dashboard", dashboardProvider));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("smarttex.preview", previewProvider, {
    // Keep the preview's iframe, its tinymist data WebSocket, and the rendered
    // document alive when the view is hidden. Without this the webview is
    // destroyed whenever the user switches away from the SmartTeX sidebar (or
    // collapses the view), dropping the live connection so saves no longer
    // appear to update the preview until it is fully reloaded.
    webviewOptions: { retainContextWhenHidden: true },
  }));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("smarttex.problems", problemsProvider));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("smarttex.pdfEmbeds", pdfEmbedsProvider));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("smarttex.aiChanges", aiChangesProvider));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("smarttex.fileAnnotations", fileAnnotationsProvider));
  context.subscriptions.push(vscode.window.registerWebviewViewProvider("smarttex.settings", settingsProvider));
  context.subscriptions.push(vscode.window.registerTreeDataProvider("smarttex.history", historyProvider));
  context.subscriptions.push(vscode.window.registerTreeDataProvider("smarttex.annotations", annotationsProvider));
  annotationCodeLensProvider = new AnnotationCodeLensProvider();
  context.subscriptions.push(vscode.languages.registerCodeLensProvider({ scheme: "file" }, annotationCodeLensProvider));

  vscode.workspace.onDidChangeConfiguration(event => {
    if (!event.affectsConfiguration("smarttex")) return;
    settingsProvider?.refresh();
    dashboardProvider?.refresh();
    annotationCodeLensProvider?.refresh();
    refreshAnnotationsQuietly().catch(err => output?.appendLine(`Annotations refresh failed: ${err.message}`));
  }, null, context.subscriptions);

  vscode.workspace.onDidChangeWorkspaceFolders(() => {
    watchStateFile(context);
    updateStatus();
    connectRealtime(projectIdFromWorkspace());
    restartAnnotationRefreshTimer();
    refreshAnnotationsQuietly();
    aiChangesProvider?.load().catch(err => output?.appendLine(`AI changes refresh failed: ${err.message}`));
    historyProvider?.load().catch(err => output?.appendLine(`History refresh failed: ${err.message}`));
  }, null, context.subscriptions);
  vscode.window.onDidChangeActiveTextEditor(editor => {
    if (editor?.document?.uri?.scheme === "file" && editor.viewColumn) {
      lastTextEditorViewColumn = editor.viewColumn;
    }
    if (editor) updateEditorAnnotationDecorations(editor);
    fileAnnotationsProvider?.refresh();
    if (editor) schedulePreviewReveal();
  }, null, context.subscriptions);
  vscode.window.onDidChangeTextEditorSelection(event => {
    fileAnnotationsProvider?.refresh();
    if (event.textEditor === vscode.window.activeTextEditor) schedulePreviewReveal();
  }, null, context.subscriptions);
  vscode.window.onDidChangeVisibleTextEditors(updateVisibleAnnotationDecorations, null, context.subscriptions);
  vscode.workspace.onDidSaveTextDocument(doc => {
    if (autoSaveInProgress) return;
    if (doc.uri.scheme === "file") queueSyncCurrentWorkspace("save");
  }, null, context.subscriptions);
  vscode.workspace.onDidChangeTextDocument(event => {
    queueAutoSaveAndSync(event.document);
  }, null, context.subscriptions);
  watchStateFile(context);
  updateStatus();
  connectRealtime(projectIdFromWorkspace());
  restartAnnotationRefreshTimer();
  aiChangesProvider?.load().catch(err => output.appendLine(`AI changes refresh failed: ${err.message}`));
  historyProvider?.load().catch(err => output.appendLine(`History refresh failed: ${err.message}`));
  refreshAnnotations().catch(err => output.appendLine(`Annotations refresh failed: ${err.message}`));
}

function deactivate() {
  stopRealtime();
  clearTimeout(syncTimer);
  clearTimeout(autoSaveTimer);
  clearInterval(annotationRefreshTimer);
  compileDiagnostics?.clear();
  stateWatcher?.dispose();
}

module.exports = { activate, deactivate };
