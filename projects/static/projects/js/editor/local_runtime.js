import { api } from "./api.js";
import { cfg, s } from "./state.js";

const DEFAULT_AGENT_URL = "http://127.0.0.1:8765";
export const LOCAL_RUNTIME_CHANGED_EVENT = "smarttex-local-runtime-changed";
const DEFAULT_CAPABILITIES = ["compile", "typst-preview", "tinymist-lsp"];

let statusEl = null;
let popoverEl = null;
let lastRuntime = null;
let lastHealth = null;
let lastStatus = "disabled";
let saveHintFn = null;

function notifyLocalRuntimeChanged() {
  try {
    window.dispatchEvent(new CustomEvent(LOCAL_RUNTIME_CHANGED_EVENT, {
      detail: {
        enabled: isLocalRuntimeEnabled(),
        active: isLocalRuntimeActive(),
        config: localRuntimeConfig(),
        runtime: getProjectLocalRuntime(),
      },
    }));
  } catch (_) {}
}

function readSetting(key, fallback = "") {
  try {
    return localStorage.getItem(key) || fallback;
  } catch (_) {
    return fallback;
  }
}

export function isLocalRuntimeEnabled() {
  return readSetting("smarttex.localRuntime.enabled") === "1";
}

export function getProjectLocalRuntime() {
  return lastRuntime || s.projectMeta?.local_runtime || null;
}

export function hasLocalRuntimeBridgeCredentials() {
  const local = localRuntimeConfig();
  return Boolean(local.url && local.secret);
}

export function isLocalRuntimeConfiguredForBrowser() {
  return isLocalRuntimeEnabled() && hasLocalRuntimeBridgeCredentials();
}

export function isLocalRuntimeActive() {
  const runtime = getProjectLocalRuntime();
  return runtime?.available !== false && Boolean(runtime?.enabled) && isLocalRuntimeConfiguredForBrowser();
}

export function localRuntimeCapabilities() {
  const caps = getProjectLocalRuntime()?.capabilities;
  return Array.isArray(caps) ? caps : [];
}

export function hasLocalRuntimeCapability(capability) {
  return isLocalRuntimeActive() && localRuntimeCapabilities().includes(capability);
}

export function localRuntimeConfig() {
  return {
    url: readSetting("smarttex.localRuntime.url", DEFAULT_AGENT_URL).replace(/\/+$/, ""),
    secret: readSetting("smarttex.localRuntime.secret", ""),
  };
}

export function setLocalRuntimeEnabled(enabled) {
  try {
    if (enabled) localStorage.setItem("smarttex.localRuntime.enabled", "1");
    else localStorage.removeItem("smarttex.localRuntime.enabled");
  } catch (_) {}
}

export function setLocalRuntimeSecret(secret) {
  try {
    if (secret) localStorage.setItem("smarttex.localRuntime.secret", secret);
    else localStorage.removeItem("smarttex.localRuntime.secret");
  } catch (_) {}
}

export function setLocalRuntimeUrl(url) {
  try {
    if (url) localStorage.setItem("smarttex.localRuntime.url", url);
    else localStorage.removeItem("smarttex.localRuntime.url");
  } catch (_) {}
}

function normalizeAgentUrl(url) {
  return String(url || DEFAULT_AGENT_URL).trim().replace(/\/+$/, "") || DEFAULT_AGENT_URL;
}

function shellQuote(value) {
  return `'${String(value || "").replace(/'/g, "'\\''")}'`;
}

function powershellQuote(value) {
  return `'${String(value || "").replace(/'/g, "''")}'`;
}

function unixInstallCommand() {
  const origin = window.location.origin || "https://smart-tex.pp.ua";
  return `curl -fsSL ${origin}/static/local-agent/stable/install.sh | SMARTTEX_SERVER=${shellQuote(origin)} bash`;
}

function windowsInstallCommand() {
  const origin = window.location.origin || "https://smart-tex.pp.ua";
  const scriptUrl = `${origin}/static/local-agent/stable/install.ps1`;
  return `powershell -ExecutionPolicy Bypass -NoProfile -Command "$env:SMARTTEX_SERVER=${powershellQuote(origin)}; iwr -useb ${powershellQuote(scriptUrl)} | iex"`;
}

export async function checkLocalRuntime(overrides = null) {
  const localCfg = overrides || localRuntimeConfig();
  const agentUrl = normalizeAgentUrl(localCfg.url);
  const secret = String(localCfg.secret || "").trim();
  if (!secret) return { ok: false, detail: "Вкажіть bridge secret з `smarttex-local serve`." };
  const response = await fetch(`${agentUrl}/v1/health`, {
    method: "GET",
    headers: { "X-SmartTeX-Local-Secret": secret },
  });
  if (!response.ok) {
    return { ok: false, detail: await response.text() };
  }
  return response.json();
}

export async function fetchProjectLocalRuntime() {
  if (!cfg.projectId) return null;
  const payload = await api(`/api/projects/${cfg.projectId}/local-runtime/`, { method: "GET" });
  if (s.projectMeta) s.projectMeta.local_runtime = payload;
  return payload;
}

export async function setProjectLocalRuntimeEnabled(enabled, capabilities = []) {
  if (!cfg.projectId) return null;
  const payload = await api(`/api/projects/${cfg.projectId}/local-runtime/`, {
    method: "PUT",
    body: JSON.stringify({ enabled: Boolean(enabled), capabilities }),
  });
  if (s.projectMeta) s.projectMeta.local_runtime = payload;
  return payload;
}

function shortTime(value) {
  if (!value) return "";
  try {
    return new Date(value).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (_) {
    return "";
  }
}

function capabilityLabel(name) {
  const labels = {
    compile: "compile",
    "typst-preview": "preview",
    "tinymist-lsp": "LSP",
  };
  return labels[name] || name;
}

function formatRuntimeAge(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 60) return `${Math.round(value)}s`;
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  if (minutes < 60) return rest ? `${minutes}m ${Math.round(rest)}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m` : `${hours}h`;
}

function runtimeStatusTone(runtime, healthOk) {
  if (runtime?.available === false) return "disabled";
  if (!runtime?.enabled) return "disabled";
  if (runtime.connected && healthOk) return "connected";
  if (runtime.connection_state === "error" || runtime.connection_state === "offline") return "error";
  return "checking";
}

function runtimeStatusLabel(runtime, healthOk) {
  if (runtime?.available === false) return "Локальний runtime вимкнено на сервері";
  if (!runtime?.enabled) return "Runtime вимкнений для цього проєкту";
  if (runtime.connected && healthOk) return "Локальний runtime активний";
  if (runtime.connection_state === "error") return "Потрібна увага";
  if (runtime.connection_state === "offline") return "Agent офлайн";
  if (runtime.connection_state === "waiting") return "Очікуємо heartbeat";
  return "Runtime увімкнений, очікуємо локальний агент";
}

function runtimeDetailText(runtime) {
  if (runtime?.available === false) return runtime.connection_detail || "Local runtime is disabled on this server.";
  if (!runtime?.enabled) return "";
  if (runtime.last_error) return runtime.last_error;
  if (runtime.connected) return "";
  if (runtime.connection_state === "waiting") return "Проєкт уже переведено на local runtime, але сервер ще не бачив heartbeat від агента.";
  if (runtime.connection_state === "offline") {
    const age = formatRuntimeAge(runtime.last_seen_age_seconds);
    return age
      ? `Останній heartbeat був ${age} тому. Можливо, агент зупинився або втратив доступ до сервера.`
      : "Heartbeat агента застарів. Можливо, локальний агент зупинився.";
  }
  return runtime.connection_detail || "";
}

function defaultPopoverMessage(runtime) {
  if (runtime?.enabled && !hasLocalRuntimeBridgeCredentials()) {
    return {
      tone: "error",
      text: "Цей браузер ще не має bridge secret. Вставте Agent URL і secret з локального smarttex-local agent.",
    };
  }
  const detail = runtimeDetailText(runtime);
  if (detail) {
    return {
      tone: runtime.connection_state === "error" || runtime.connection_state === "offline" ? "error" : "muted",
      text: detail,
    };
  }
  if (runtime?.enabled && lastHealth?.ok && !runtime.connected) {
    return {
      tone: "muted",
      text: "Agent відповідає локально, але сервер ще не отримав heartbeat для цього проєкту.",
    };
  }
  return { tone: "muted", text: "" };
}

function renderButton(status = null, title = "") {
  if (!statusEl) return;
  const serverRuntime = lastRuntime || s.projectMeta?.local_runtime || {};
  const browserReady = isLocalRuntimeConfiguredForBrowser();
  const serverEnabled = Boolean(serverRuntime.enabled);
  const runtimeTone = runtimeStatusTone(serverRuntime, Boolean(lastHealth?.ok));
  const nextStatus = status || (!browserReady && serverEnabled ? "error" : runtimeTone);
  lastStatus = nextStatus;
  statusEl.dataset.status = nextStatus;
  statusEl.textContent = nextStatus === "connected" ? "Local" : nextStatus === "checking" ? "Local..." : "Local";
  const runtimeDetail = runtimeDetailText(serverRuntime);
  statusEl.title = title || (!browserReady && serverEnabled
    ? "Локальний runtime увімкнений на сервері, але цей браузер не має bridge secret."
    : runtimeDetail
    ? runtimeDetail
    : browserReady
    ? "Локальний runtime налаштований. Натисніть, щоб відкрити стан."
    : "Локальний runtime вимкнений. Натисніть, щоб налаштувати.");
  statusEl.setAttribute("aria-pressed", serverEnabled ? "true" : "false");
}

function setPopoverBusy(busy) {
  if (!popoverEl) return;
  popoverEl.dataset.busy = busy ? "1" : "0";
  popoverEl.querySelectorAll("button, input").forEach(el => {
    if (el.dataset.action === "close") return;
    el.disabled = Boolean(busy);
  });
}

function setPopoverMessage(text, tone = "muted") {
  if (!popoverEl) return;
  const msg = popoverEl.querySelector("[data-role='message']");
  if (!msg) return;
  msg.textContent = text || "";
  msg.dataset.tone = tone;
}

function updatePopoverFromState() {
  if (!popoverEl) return;
  const local = localRuntimeConfig();
  const runtime = lastRuntime || s.projectMeta?.local_runtime || {};
  const urlInput = popoverEl.querySelector("[data-field='url']");
  const secretInput = popoverEl.querySelector("[data-field='secret']");
  const installNode = popoverEl.querySelector("[data-role='install-command']");
  const windowsInstallNode = popoverEl.querySelector("[data-role='install-command-windows']");
  if (installNode) installNode.textContent = unixInstallCommand();
  if (windowsInstallNode) windowsInstallNode.textContent = windowsInstallCommand();
  if (urlInput && document.activeElement !== urlInput) urlInput.value = local.url || DEFAULT_AGENT_URL;
  if (secretInput && document.activeElement !== secretInput) secretInput.value = local.secret || "";

  const serverEnabled = Boolean(runtime.enabled);
  const available = runtime.available !== false;
  const browserReady = isLocalRuntimeConfiguredForBrowser();
  const connected = Boolean(runtime.connected);
  const healthOk = Boolean(lastHealth?.ok);
  const statusNode = popoverEl.querySelector("[data-role='runtime-status']");
  const agentNode = popoverEl.querySelector("[data-role='agent']");
  const capsNode = popoverEl.querySelector("[data-role='capabilities']");
  const enableBtn = popoverEl.querySelector("[data-action='enable']");
  const disableBtn = popoverEl.querySelector("[data-action='disable']");

  if (statusNode) {
    statusNode.dataset.status = runtimeStatusTone(runtime, healthOk);
    statusNode.textContent = runtimeStatusLabel(runtime, healthOk);
  }
  if (agentNode) {
    const version = runtime.agent_version || lastHealth?.tool_version || lastHealth?.tool || "";
    const seen = shortTime(runtime.last_seen_at);
    const toolchain = [
      lastHealth?.typst_version ? `Typst ${lastHealth.typst_version}` : "",
      lastHealth?.tinymist_version ? `Tinymist ${lastHealth.tinymist_version}` : "",
    ].filter(Boolean).join(" · ");
    const base = connected
      ? `Agent ${runtime.agent_id || "local"}${version ? ` · ${version}` : ""}${seen ? ` · ${seen}` : ""}`
      : healthOk
      ? "Agent доступний у браузері, heartbeat ще не прийшов на сервер"
      : "Agent не перевірено";
    const runtimeDetail = runtimeDetailText(runtime);
    const details = [toolchain, runtimeDetail].filter(Boolean).join(" · ");
    agentNode.textContent = details ? `${base} · ${details}` : base;
  }
  if (capsNode) {
    const caps = Array.isArray(runtime.capabilities) && runtime.capabilities.length
      ? runtime.capabilities
      : Array.isArray(lastHealth?.capabilities)
      ? lastHealth.capabilities
      : DEFAULT_CAPABILITIES;
    capsNode.innerHTML = "";
    caps.forEach(cap => {
      const chip = document.createElement("span");
      chip.className = "e-local-runtime-cap";
      chip.textContent = capabilityLabel(cap);
      capsNode.appendChild(chip);
    });
  }
  if (enableBtn) {
    enableBtn.textContent = serverEnabled ? "Оновити" : "Увімкнути";
    enableBtn.disabled = !available;
  }
  if (disableBtn) {
    disableBtn.hidden = !serverEnabled && !browserReady;
  }
}

function positionPopover() {
  if (!popoverEl || !statusEl || popoverEl.hidden) return;
  const rect = statusEl.getBoundingClientRect();
  const width = Math.min(430, Math.max(320, window.innerWidth - 24));
  const left = Math.min(Math.max(12, rect.right - width), window.innerWidth - width - 12);
  popoverEl.style.width = `${width}px`;
  popoverEl.style.left = `${left}px`;
  popoverEl.style.bottom = `${Math.max(34, window.innerHeight - rect.top + 10)}px`;
}

function ensurePopover() {
  if (popoverEl) return popoverEl;
  popoverEl = document.createElement("div");
  popoverEl.className = "e-local-runtime-popover";
  popoverEl.hidden = true;
  popoverEl.innerHTML = `
    <div class="e-local-runtime-popover__head">
      <div>
        <div class="e-local-runtime-popover__title">Локальний runtime</div>
        <div class="e-local-runtime-popover__subtitle">Компіляція, preview і Tinymist LSP на вашому пристрої.</div>
      </div>
      <button class="e-local-runtime-icon" type="button" data-action="close" aria-label="Закрити">×</button>
    </div>
    <div class="e-local-runtime-card">
      <div class="e-local-runtime-status" data-role="runtime-status" data-status="disabled">Runtime вимкнений</div>
      <div class="e-local-runtime-agent" data-role="agent">Agent не перевірено</div>
      <div class="e-local-runtime-caps" data-role="capabilities"></div>
    </div>
    <div class="e-local-runtime-install">
      <div class="e-local-runtime-install__copy">
        <div>
          <div class="e-local-runtime-install__title">Потрібен локальний agent?</div>
          <div class="e-local-runtime-install__text">Інсталер поставить binary, додасть його в user PATH і після цього можна запускати <code>smarttex-local login --serve</code>.</div>
        </div>
      </div>
      <div class="e-local-runtime-install__row">
        <span class="e-local-runtime-install__os">macOS / Linux</span>
        <button class="e-local-runtime-copy" type="button" data-action="copy-install-unix">Копіювати</button>
      </div>
      <code data-role="install-command"></code>
      <div class="e-local-runtime-install__row">
        <span class="e-local-runtime-install__os">Windows PowerShell</span>
        <button class="e-local-runtime-copy" type="button" data-action="copy-install-windows">Копіювати</button>
      </div>
      <code data-role="install-command-windows"></code>
    </div>
    <label class="e-local-runtime-field">
      <span>Agent URL</span>
      <input data-field="url" autocomplete="off" spellcheck="false" />
    </label>
    <label class="e-local-runtime-field">
      <span>Bridge secret</span>
      <input data-field="secret" type="password" autocomplete="off" spellcheck="false" />
    </label>
    <div class="e-local-runtime-message" data-role="message" data-tone="muted"></div>
    <div class="e-local-runtime-actions">
      <button class="btn secondary" type="button" data-action="test">Перевірити</button>
      <button class="btn primary" type="button" data-action="enable">Увімкнути</button>
      <button class="btn danger" type="button" data-action="disable">Вимкнути</button>
    </div>
  `;
  document.body.appendChild(popoverEl);
  popoverEl.addEventListener("click", event => event.stopPropagation());
  popoverEl.querySelector("[data-action='close']")?.addEventListener("click", closePopover);
  popoverEl.querySelector("[data-action='test']")?.addEventListener("click", () => testFromPopover());
  popoverEl.querySelector("[data-action='enable']")?.addEventListener("click", () => enableFromPopover());
  popoverEl.querySelector("[data-action='disable']")?.addEventListener("click", () => disableFromPopover());
  popoverEl.querySelector("[data-action='copy-install-unix']")?.addEventListener("click", () => copyInstallCommand("unix"));
  popoverEl.querySelector("[data-action='copy-install-windows']")?.addEventListener("click", () => copyInstallCommand("windows"));
  popoverEl.querySelectorAll("input").forEach(input => {
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") enableFromPopover();
      if (event.key === "Escape") closePopover();
    });
  });
  window.addEventListener("resize", positionPopover);
  window.addEventListener("scroll", positionPopover, true);
  document.addEventListener("click", event => {
    if (!popoverEl || popoverEl.hidden) return;
    if (statusEl?.contains(event.target)) return;
    if (popoverEl.contains(event.target)) return;
    closePopover();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closePopover();
  });
  return popoverEl;
}

function openPopover() {
  ensurePopover();
  popoverEl.hidden = false;
  updatePopoverFromState();
  const runtime = lastRuntime || s.projectMeta?.local_runtime || {};
  const msg = defaultPopoverMessage(runtime);
  setPopoverMessage(msg.text, msg.tone);
  positionPopover();
  popoverEl.querySelector("[data-field='secret']")?.focus();
}

function closePopover() {
  if (!popoverEl) return;
  popoverEl.hidden = true;
}

function readPopoverConfig() {
  const agentUrl = normalizeAgentUrl(popoverEl?.querySelector("[data-field='url']")?.value);
  const secret = String(popoverEl?.querySelector("[data-field='secret']")?.value || "").trim();
  return { url: agentUrl, secret };
}

function persistBrowserConfig(localCfg, enabled) {
  setLocalRuntimeUrl(localCfg.url);
  setLocalRuntimeSecret(localCfg.secret);
  setLocalRuntimeEnabled(enabled);
}

async function copyInstallCommand(platform = "unix") {
  const command = platform === "windows" ? windowsInstallCommand() : unixInstallCommand();
  try {
    await navigator.clipboard.writeText(command);
    setPopoverMessage(platform === "windows" ? "Windows-команду встановлення скопійовано." : "Команду встановлення скопійовано.", "success");
  } catch (_) {
    setPopoverMessage(command, "muted");
  }
}

async function testFromPopover() {
  const localCfg = readPopoverConfig();
  setPopoverBusy(true);
  setPopoverMessage("Перевіряю локальний агент...", "muted");
  try {
    const health = await checkLocalRuntime(localCfg);
    lastHealth = health;
    if (!health?.ok) {
      setPopoverMessage(health?.detail || "Локальний агент не відповідає.", "error");
      renderButton("error", health?.detail || "Локальний агент не відповідає.");
      return health;
    }
    persistBrowserConfig(localCfg, isLocalRuntimeEnabled());
    setPopoverMessage("Agent відповів. Можна вмикати runtime для проєкту.", "success");
    renderButton(lastRuntime?.enabled ? (lastRuntime?.connected ? "connected" : "checking") : "checking");
    return health;
  } catch (err) {
    lastHealth = { ok: false, detail: err.message };
    setPopoverMessage(`Agent недоступний: ${err.message}`, "error");
    renderButton("error", `Agent недоступний: ${err.message}`);
    return lastHealth;
  } finally {
    setPopoverBusy(false);
    updatePopoverFromState();
  }
}

async function enableFromPopover() {
  const localCfg = readPopoverConfig();
  setPopoverBusy(true);
  setPopoverMessage("Перевіряю agent і вмикаю runtime...", "muted");
  try {
    const health = await checkLocalRuntime(localCfg);
    lastHealth = health;
    if (!health?.ok) throw new Error(health?.detail || "Локальний агент не відповідає.");
    persistBrowserConfig(localCfg, true);
    const capabilities = Array.isArray(health.capabilities) && health.capabilities.length ? health.capabilities : DEFAULT_CAPABILITIES;
    lastRuntime = await setProjectLocalRuntimeEnabled(true, capabilities);
    notifyLocalRuntimeChanged();
    renderButton(lastRuntime?.connected ? "connected" : "checking");
    setPopoverMessage(lastRuntime?.connected
      ? "Готово. MCP і кнопка компіляції тепер підуть через локальний agent."
      : "Runtime увімкнено. Зачекайте кілька секунд, поки agent надішле heartbeat.", "success");
    saveHintFn?.("Локальну компіляцію увімкнено", "saved");
  } catch (err) {
    setLocalRuntimeEnabled(false);
    renderButton("error", err.message);
    setPopoverMessage(err.message, "error");
  } finally {
    setPopoverBusy(false);
    updatePopoverFromState();
  }
}

async function disableFromPopover() {
  setPopoverBusy(true);
  setPopoverMessage("Вимикаю runtime для проєкту...", "muted");
  try {
    setLocalRuntimeEnabled(false);
    lastRuntime = await setProjectLocalRuntimeEnabled(false).catch(() => null);
    notifyLocalRuntimeChanged();
    lastHealth = null;
    renderButton("disabled");
    setPopoverMessage("Локальний runtime вимкнено. Компіляція знову піде на сервер.", "success");
    saveHintFn?.("Локальну компіляцію вимкнено", "saved");
  } catch (err) {
    renderButton("error", err.message);
    setPopoverMessage(err.message, "error");
  } finally {
    setPopoverBusy(false);
    updatePopoverFromState();
  }
}

export function initStatusToggle(el, { setSaveHint } = {}) {
  if (!el) return;
  statusEl = el;
  saveHintFn = setSaveHint || null;

  const refresh = async () => {
    const runtime = await fetchProjectLocalRuntime().catch(() => s.projectMeta?.local_runtime || null);
    lastRuntime = runtime;
    if (!runtime?.enabled) {
      renderButton("disabled");
      updatePopoverFromState();
      return;
    }
    if (!isLocalRuntimeEnabled() || !hasLocalRuntimeBridgeCredentials()) {
      renderButton("error", "Локальний runtime увімкнений на сервері, але цей браузер не має bridge secret.");
      updatePopoverFromState();
      return;
    }
    renderButton("checking", "Перевірка локального агента...");
    try {
      const health = await checkLocalRuntime();
      lastHealth = health;
      if (health?.ok) {
        renderButton(runtime.connected ? "connected" : "checking", runtime.connected
          ? `Локальний агент готовий: ${health.tool || "smarttex-local"}`
          : "Локальний агент доступний. Очікуємо heartbeat на сервері...");
      } else {
        renderButton("error", health?.detail || "Локальний агент недоступний");
      }
    } catch (err) {
      lastHealth = { ok: false, detail: err.message };
      renderButton("error", `Локальний агент недоступний: ${err.message}`);
    }
    updatePopoverFromState();
  };

  el.addEventListener("click", event => {
    event.stopPropagation();
    if (popoverEl && !popoverEl.hidden) closePopover();
    else openPopover();
  });

  fetchProjectLocalRuntime()
    .catch(() => null)
    .finally(() => refresh().catch(() => renderButton("error")));

  window.addEventListener(LOCAL_RUNTIME_CHANGED_EVENT, () => refresh().catch(() => null));
  window.setInterval(() => {
    if (lastStatus === "checking" || lastRuntime?.enabled) refresh().catch(() => null);
  }, 10000);
}
