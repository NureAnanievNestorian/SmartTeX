import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as ui from "./ui.js";

const { cfg, s } = state;
const { api } = apiMod;
const { escHtml, showConfirm } = ui;

const contextPanelEl = document.getElementById("longdoc-context-panel");
const outlinePanelEl = document.getElementById("longdoc-outline-panel");
const tasksPanelEl = document.getElementById("longdoc-tasks-panel");
const notesPanelEl = document.getElementById("longdoc-notes-panel");
const requirementsPanelEl = document.getElementById("longdoc-requirements-panel");
const settingsPanelEl = document.getElementById("longdoc-settings-panel");
const overviewPanelEl = document.getElementById("longdoc-overview-panel");
const overviewBadgeEl = document.getElementById("writing-assistant-badge");
const centerEl = document.getElementById("drop-zone");
const waTabBtnEl = document.getElementById("wa-tab-btn");

let _reloadProjectMeta = null;
const uiState = {
  creating: new Set(),
  editing: new Set(),
};

export function setLongdocProjectMetaRef(fn) {
  _reloadProjectMeta = fn;
}

function longdocEnabled() {
  return Boolean(s.longdoc.settings?.enabled || s.projectMeta?.longdoc?.enabled);
}

function featureEnabled(name) {
  if (!longdocEnabled()) return false;
  return Boolean(s.longdoc.settings?.[name]);
}

function button(label, action, extra = "") {
  const cls = `e-sec-btn${extra ? ` ${extra}` : ""}`;
  return `<button class="${cls}" type="button" data-action="${escHtml(action)}">${escHtml(label)}</button>`;
}

function setCreating(kind, active) {
  if (active) uiState.creating.add(kind);
  else uiState.creating.delete(kind);
}

function editKey(kind, id) {
  return `${kind}:${id}`;
}

function setEditing(kind, id, active) {
  const key = editKey(kind, id);
  if (active) uiState.editing.add(key);
  else uiState.editing.delete(key);
}

function isEditing(kind, id) {
  return uiState.editing.has(editKey(kind, id));
}

function statusLabel(value) {
  const labels = {
    missing: "Відсутній",
    stub: "Чернетка-скелет",
    draft: "Чернетка",
    done: "Готово",
    open: "Відкрита",
    in_progress: "У роботі",
    covered: "Покрито",
    partial: "Частково",
    unchecked: "Не перевірено",
  };
  return labels[value] || String(value || "unchecked").replace(/_/g, " ");
}

function chip(value, label = null) {
  const safe = String(value || "unchecked");
  return `<span class="e-longdoc-chip ${escHtml(safe)}">${escHtml(label || statusLabel(safe))}</span>`;
}

function renderSectionRefChips(refs) {
  const items = Array.isArray(refs) ? refs : [];
  if (!items.length) return `<span class="e-longdoc-muted">Посилань на секції немає</span>`;
  return `<div class="e-chip-row">${items.map(ref => `<span class="e-ref-chip">${escHtml(ref)}</span>`).join("")}</div>`;
}

function renderTextBlock(text, fallback = "Вмісту ще немає.") {
  const value = String(text || "").trim();
  if (!value) return `<div class="e-longdoc-muted">${escHtml(fallback)}</div>`;
  return `<div class="e-rendered-text">${escHtml(value)}</div>`;
}

function renderCreatePanel(kind, title, bodyHtml) {
  if (!uiState.creating.has(kind)) {
    return `<div class="e-toolbar-row">${button(`Додати ${title}`, `show-create-${kind}`, "primary")}</div>`;
  }
  return `
    <section class="e-longdoc-card e-edit-card">
      <div class="e-longdoc-card-head">
        <strong>Новий елемент: ${escHtml(title.toLowerCase())}</strong>
        ${button("Скасувати", `hide-create-${kind}`)}
      </div>
      ${bodyHtml}
    </section>
  `;
}

function switchSidebarTab(tabName) {
  document.querySelectorAll(".e-tab").forEach(tab => {
    tab.classList.toggle("active", tab.dataset.tab === tabName);
  });
  document.querySelectorAll(".e-tabpanel").forEach(panel => {
    panel.classList.toggle("active", panel.id === `tab-${tabName}`);
  });
}

function setAssistantOpen(open) {
  centerEl?.classList.toggle("wa-active", Boolean(open));
  waTabBtnEl?.classList.toggle("active", Boolean(open));
}

function switchAssistantTab(tabName) {
  document.querySelectorAll(".e-wa-tab").forEach(tab => {
    tab.classList.toggle("active", tab.dataset.waTab === tabName);
  });
  document.querySelectorAll(".e-wa-panel").forEach(panel => {
    panel.classList.toggle("active", panel.id === `wa-panel-${tabName}`);
  });
  setAssistantOpen(true);
}

export function openAssistantSettings() {
  switchAssistantTab("settings");
}

function aiTaskLabel(value) {
  const labels = {
    pre_proposal_analyze: "Pre proposal",
    context_compress: "Context",
    edit_intent_classify: "Intent",
    diff_safety_review: "Diff review",
    compile_log_triage: "Compile triage",
    circuit_breaker_evaluate: "Circuit breaker",
  };
  return labels[value] || String(value || "").replace(/_/g, " ");
}

function aiStatusLabel(value) {
  const labels = {
    success: "Success",
    quota_exceeded: "Quota",
    timeout: "Timeout",
    invalid_json: "Invalid JSON",
    provider_error: "Provider error",
  };
  return labels[value] || String(value || "").replace(/_/g, " ");
}

function fmtLogDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso || "";
  return d.toLocaleString("uk-UA", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderAiLogDetails(item) {
  const prompt = String(item.input_prompt || "").trim();
  const output = String(item.output_text || "").trim();
  return `
    <div class="ai-log-detail">
      <div class="ai-log-detail-block">
        <strong>Input prompt</strong>
        <div>${prompt ? `<pre class="ai-log-pre">${escHtml(prompt)}</pre>` : "Prompt logging disabled or empty."}</div>
      </div>
      <div class="ai-log-detail-block">
        <strong>Result</strong>
        <div>${output ? `<pre class="ai-log-pre">${escHtml(output)}</pre>` : "Result logging disabled or empty."}</div>
      </div>
      <div class="ai-log-detail-block">
        <strong>Provider</strong>
        <div>${escHtml(item.provider || "—")}</div>
        <div>${escHtml(item.model_name || "—")}</div>
      </div>
      <div class="ai-log-detail-block">
        <strong>Request metadata</strong>
        <div>Task: ${escHtml(aiTaskLabel(item.task_type))}</div>
        <div>Status: ${escHtml(aiStatusLabel(item.status))}</div>
        <div>Latency: ${escHtml(String(item.latency_ms || 0))} ms</div>
        <div>Error: ${escHtml(item.error_code || "None")}</div>
      </div>
    </div>
  `;
}

function renderAiLogModal(payload) {
  const body = document.getElementById("ai-log-body");
  if (!body) return;
  const summary = payload?.summary || {};
  const items = payload?.items || [];
  const totalTokens = Number(summary.total_input_tokens || 0) + Number(summary.total_output_tokens || 0);
  const renderPills = (rows, key, formatter) => {
    if (!rows?.length) return `<div class="ai-log-empty">Даних ще немає.</div>`;
    return `<div class="ai-log-pills">${rows.map(row => `
      <span class="ai-log-pill">
        ${escHtml(formatter(row[key] || ""))}
        <small>${escHtml(String(row.count || 0))}</small>
      </span>
    `).join("")}</div>`;
  };
  body.innerHTML = `
    <section class="ai-log-summary">
      <div class="ai-log-stat"><strong>${escHtml(String(summary.total_requests || 0))}</strong><span>Requests</span></div>
      <div class="ai-log-stat"><strong>${escHtml(String(summary.total_input_tokens || 0))}</strong><span>Input tokens</span></div>
      <div class="ai-log-stat"><strong>${escHtml(String(summary.total_output_tokens || 0))}</strong><span>Output tokens</span></div>
      <div class="ai-log-stat"><strong>${escHtml(String(totalTokens))}</strong><span>Total tokens</span></div>
    </section>
    <section class="ai-log-groups">
      <div class="ai-log-group">
        <h4>By task</h4>
        ${renderPills(summary.by_task, "task_type", aiTaskLabel)}
      </div>
      <div class="ai-log-group">
        <h4>By status</h4>
        ${renderPills(summary.by_status, "status", aiStatusLabel)}
      </div>
    </section>
    <section class="ai-log-table-wrap">
      ${items.length ? `
        <table class="ai-log-table">
          <thead>
            <tr>
              <th class="ai-log-expand-cell"></th>
              <th>Time</th>
              <th>Task</th>
              <th>Status</th>
              <th>Tokens</th>
              <th>Latency</th>
              <th>Provider</th>
            </tr>
          </thead>
          <tbody>
            ${items.map((item, index) => `
              <tr class="ai-log-row expandable" data-ai-log-row="${index}">
                <td class="ai-log-expand-cell">
                  <button class="ai-log-expand-btn" type="button" data-ai-log-toggle="${index}" aria-expanded="false" aria-label="Expand log details">
                    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                      <polyline points="4 2 8 6 4 10"></polyline>
                    </svg>
                  </button>
                </td>
                <td>${escHtml(fmtLogDate(item.created_at))}</td>
                <td><span class="ai-log-task">${escHtml(aiTaskLabel(item.task_type))}</span></td>
                <td><span class="ai-log-status ${escHtml(item.status)}">${escHtml(aiStatusLabel(item.status))}</span>${item.error_code ? `<div class="e-longdoc-muted">${escHtml(item.error_code)}</div>` : ""}</td>
                <td>${escHtml(String((item.input_tokens_estimate || 0) + (item.output_tokens_estimate || 0)))} <span class="e-longdoc-muted">(${escHtml(String(item.input_tokens_estimate || 0))} in / ${escHtml(String(item.output_tokens_estimate || 0))} out)</span></td>
                <td>${escHtml(String(item.latency_ms || 0))} ms</td>
                <td>${escHtml(item.provider || "")}${item.model_name ? `<div class="e-longdoc-muted">${escHtml(item.model_name)}</div>` : ""}</td>
              </tr>
              <tr class="ai-log-detail-row" data-ai-log-detail="${index}">
                <td class="ai-log-detail-cell" colspan="7">${renderAiLogDetails(item)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      ` : `<div class="ai-log-empty">Для цього проєкту ще немає AI request log.</div>`}
    </section>
  `;
  body.querySelectorAll("[data-ai-log-toggle]").forEach(btn => {
    btn.addEventListener("click", event => {
      event.stopPropagation();
      const idx = btn.dataset.aiLogToggle;
      const detail = body.querySelector(`[data-ai-log-detail="${idx}"]`);
      const expanded = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", expanded ? "false" : "true");
      detail?.classList.toggle("open", !expanded);
    });
  });
  body.querySelectorAll("[data-ai-log-row]").forEach(row => {
    row.addEventListener("click", event => {
      if (event.target.closest("button")) return;
      const idx = row.dataset.aiLogRow;
      body.querySelector(`[data-ai-log-toggle="${idx}"]`)?.click();
    });
  });
}

export async function openAiLogModal() {
  const overlay = document.getElementById("ai-log-overlay");
  const body = document.getElementById("ai-log-body");
  if (!overlay || !body) return;
  overlay.classList.add("open");
  body.innerHTML = `<div class="ai-log-empty">Завантаження...</div>`;
  try {
    const payload = await api(`/api/projects/${cfg.projectId}/ai-request-log/?limit=40`, { method: "GET" });
    renderAiLogModal(payload);
  } catch (err) {
    body.innerHTML = `<div class="ai-log-empty">Помилка завантаження: ${escHtml(err.message || String(err))}</div>`;
  }
}

export function closeAiLogModal() {
  document.getElementById("ai-log-overlay")?.classList.remove("open");
}

async function openSourceFile(filename) {
  const name = String(filename || "").trim();
  if (!name) return;
  const files = await import("./files.js");
  const existing = s.projectFiles.find(file => file.name === name) || { name, is_text: true, is_dir: false, type: "asset" };
  await files.selectFile(existing);
}

function emptyCard(title, body, action = "") {
  return `<div class="e-empty-card"><strong>${escHtml(title)}</strong>${escHtml(body)}${action ? `<div class="e-longdoc-actions">${action}</div>` : ""}</div>`;
}

function bindActions(root, handlers) {
  root.querySelectorAll("[data-action]").forEach(el => {
    el.addEventListener("click", async event => {
      const action = event.currentTarget.dataset.action;
      const handler = handlers[action];
      if (!handler) return;
      try {
        await handler(event.currentTarget);
      } catch (err) {
        window.alert(err.message || String(err));
      }
    });
  });
}

async function refreshProjectMeta() {
  if (_reloadProjectMeta) await _reloadProjectMeta();
}

export async function enableLongdoc() {
  await api(`/api/projects/${cfg.projectId}/longdoc/settings/`, {
    method: "PATCH",
    body: JSON.stringify({ enabled: true }),
  });
  await refreshProjectMeta();
  await loadLongdocData();
}

async function loadLongdocSettings() {
  s.longdoc.settings = await api(`/api/projects/${cfg.projectId}/longdoc/settings/`, { method: "GET" });
  if (overviewBadgeEl) {
    overviewBadgeEl.textContent = s.longdoc.settings.enabled ? "Письмовий асистент" : "Асистент вимкнено";
    overviewBadgeEl.classList.toggle("off", !s.longdoc.settings.enabled);
  }
}

async function loadContextFiles() {
  if (!featureEnabled("context_enabled")) {
    s.longdoc.contextFiles = [];
    return;
  }
  const payload = await api(`/api/projects/${cfg.projectId}/context-files/`, { method: "GET" });
  const rows = payload.context_files || [];
  s.longdoc.contextFiles = await Promise.all(rows.map(async row => (
    await api(`/api/projects/${cfg.projectId}/context-files/${encodeURIComponent(row.filename)}/`, { method: "GET" })
  )));
}

async function loadOutlineItems() {
  if (!featureEnabled("outline_enabled")) {
    s.longdoc.outlineItems = [];
    s.longdoc.sectionSummaries = [];
    return;
  }
  const payload = await api(`/api/projects/${cfg.projectId}/outline-items/`, { method: "GET" });
  s.longdoc.outlineItems = payload.outline_items || [];
  if (!featureEnabled("summaries_enabled")) {
    s.longdoc.sectionSummaries = [];
    return;
  }
  const summaryPayload = await api(`/api/projects/${cfg.projectId}/section-summaries/`, { method: "GET" });
  s.longdoc.sectionSummaries = summaryPayload.section_summaries || [];
}

async function loadTasks() {
  if (!featureEnabled("tasks_enabled")) {
    s.longdoc.tasks = [];
    return;
  }
  const payload = await api(`/api/projects/${cfg.projectId}/tasks/`, { method: "GET" });
  s.longdoc.tasks = payload.tasks || [];
}

async function loadNotes() {
  if (!featureEnabled("notes_enabled")) {
    s.longdoc.noteSections = [];
    return;
  }
  const payload = await api(`/api/projects/${cfg.projectId}/note-sections/`, { method: "GET" });
  s.longdoc.noteSections = payload.note_sections || [];
}

async function loadRequirements() {
  if (!featureEnabled("requirements_enabled")) {
    s.longdoc.requirements = [];
    return;
  }
  const payload = await api(`/api/projects/${cfg.projectId}/requirements/`, { method: "GET" });
  s.longdoc.requirements = payload.requirements || [];
}

async function loadActiveSession() {
  try {
    const payload = await api(`/api/projects/${cfg.projectId}/change-proposals/status/`, { method: "GET" });
    s.longdoc.activeSession = payload.proposal || null;
  } catch {
    s.longdoc.activeSession = null;
  }
  renderSessionBanner();
}

function _showPanelLoading(root) {
  if (!root) return;
  root.innerHTML = `<div class="e-longdoc-loading" aria-live="polite"><span class="e-longdoc-spinner"></span>Завантаження...</div>`;
}

function _showPanelsLoading() {
  _showPanelLoading(overviewPanelEl);
  _showPanelLoading(contextPanelEl);
  _showPanelLoading(outlinePanelEl);
  _showPanelLoading(tasksPanelEl);
  _showPanelLoading(notesPanelEl);
  _showPanelLoading(requirementsPanelEl);
}

export async function loadLongdocData() {
  await loadLongdocSettings();
  await loadActiveSession();
  if (!longdocEnabled()) {
    s.longdoc.overview = null;
    renderLongdocPanels();
    return;
  }
  _showPanelsLoading();
  const [overview] = await Promise.all([
    api(`/api/projects/${cfg.projectId}/longdoc/overview/`, { method: "GET" }),
    loadContextFiles(),
    loadOutlineItems(),
    loadTasks(),
    loadNotes(),
    loadRequirements(),
  ]);
  s.longdoc.overview = overview;
  renderLongdocPanels();
}

function renderDisabledPanel(root, featureLabel) {
  root.innerHTML = emptyCard(
    "Письмовий асистент вимкнено",
    `Увімкніть режим довгого документа, щоб використовувати ${featureLabel}.`,
    `${button("Увімкнути", "enable-longdoc")} ${button("Налаштування", "open-settings")}`
  );
  bindActions(root, {
    "enable-longdoc": enableLongdoc,
    "open-settings": async () => { switchAssistantTab("settings"); },
  });
}

function panelIntro(title, description, meta = "") {
  return `
    <div class="e-workspace-head">
      <div>
        <h2>${escHtml(title)}</h2>
        <p>${escHtml(description)}</p>
      </div>
      ${meta}
    </div>
  `;
}

function renderFeatureOffPanel(root, featureLabel) {
  root.innerHTML = emptyCard(
    `${featureLabel} вимкнено`,
    `Увімкніть цей модуль у налаштуваннях проєкту.`,
    button("Налаштування", "open-settings")
  );
  bindActions(root, {
    "open-settings": async () => { switchAssistantTab("settings"); },
  });
}

function renderSettingsPanel() {
  if (!settingsPanelEl) return;
  const settings = s.longdoc.settings || {};
  const smcl = settings.small_model || {};
  const locked = Boolean(settings.locked);
  const lockText = locked
    ? `Заблоковано запропонованою зміною #${settings.locking_proposal_id || "?"}. Запис заблоковано до завершення перегляду.`
    : "Активного блокування запропонованою зміною немає.";
  const userHasSmallModelAccess = Boolean(s.projectMeta?.small_model?.user_has_access);
  const groups = [
    ["Основне", [
      ["enabled", "Письмовий асистент", "Увімкнути робочий простір довгого документа."],
    ]],
    ["Робочі модулі", [
      ["context_enabled", "Контекст", "Структуровані довідкові файли."],
      ["outline_enabled", "План", "Планування структури та статусів."],
      ["summaries_enabled", "Підсумки секцій", "Стан підсумків і відстеження застарілості."],
      ["requirements_enabled", "Вимоги", "Покриття вимог документом."],
      ["tasks_enabled", "Завдання", "Короткі дії для написання."],
      ["notes_enabled", "Нотатки", "Структурований блокнот проєкту."],
    ]],
    ["Автоматизація", [
      ["ai_sessions_enabled", "Запропоновані зміни", "Підготовка змін у контрольованому перегляді."],
      ["mcp_controlled_access", "Контрольований MCP-доступ", "Обмежити MCP доступом через контрольовані інструменти."],
      ["mcp_write_context", "MCP може писати контекст", "Дозволити MCP створювати та оновлювати контекст."],
    ]],
    ...(userHasSmallModelAccess ? [["AI Safety Layer", [
      ["small_model_control_enabled", "Увімкнути safety layer", "Опційна перевірка читання й запропонованих змін."],
      ["context_compressor_enabled", "Context Compressor", "Стискає контекст перед роботою моделі."],
      ["edit_intent_classifier_enabled", "Edit Intent Classifier", "Обмежує розмір зміни за наміром."],
      ["diff_safety_reviewer_enabled", "Diff Safety Reviewer", "Перевіряє diff перед переглядом."],
      ["compile_log_triage_enabled", "Compile Log Triage", "Класифікує помилки компіляції."],
      ["circuit_breaker_enabled", "Circuit Breaker", "Зупиняє повторні невдалі спроби."],
    ]]] : []),
  ];

  const gh = s.projectMeta?.github || {};
  const ghSyncEnabled = Boolean(gh.sync_enabled);
  const ghRepoUrl = String(gh.repo_url || "");
  const ghPatSet = Boolean(gh.pat_set);
  const ghIntervalMinutes = Number(gh.sync_interval_minutes) || 30;

  settingsPanelEl.innerHTML = `
    <div class="e-workspace-head">
      <div>
        <h2>Налаштування</h2>
        <p>Компактне керування модулями письмового асистента.</p>
      </div>
      ${chip(locked ? "warn" : "covered", locked ? "Заблоковано" : "Вільно")}
    </div>
    <div class="e-longdoc-scroll">
      <section class="e-settings-strip">
        <div class="e-longdoc-meta">${escHtml(lockText)}</div>
      </section>
      ${groups.map(([group, rows]) => `
        <section class="e-settings-section">
          <h3>${escHtml(group)}</h3>
          <div class="e-settings-list">
            ${rows.map(([field, label, hint]) => `
              <label class="e-setting-row">
                <span>
                  <strong>${escHtml(label)}</strong>
                  <small>${escHtml(hint)}</small>
                </span>
                <input type="checkbox" data-setting="${escHtml(field)}" ${(field in smcl ? smcl[field] : settings[field]) ? "checked" : ""}>
              </label>
            `).join("")}
          </div>
        </section>
      `).join("")}
      <div class="e-longdoc-actions">${button("Зберегти налаштування", "save-settings", "primary")}</div>

      <section class="e-settings-section" style="margin-top:10px">
        <h3>GitHub Sync</h3>
        <div class="e-settings-list">
          <label class="e-setting-row">
            <span>
              <strong>Увімкнути синхронізацію</strong>
              <small>Автоматично пушити зміни до GitHub після кожного збереження.</small>
            </span>
            <input type="checkbox" id="gh-sync-enabled" ${ghSyncEnabled ? "checked" : ""}>
          </label>
        </div>
        <div class="e-modal-form" style="margin-top:10px">
          <div class="e-form-field">
            <label for="gh-repo-url">Repository URL</label>
            <input type="text" id="gh-repo-url" placeholder="https://github.com/user/repo" value="${escHtml(ghRepoUrl)}">
          </div>
          <div class="e-form-field">
            <label for="gh-pat">Personal Access Token${ghPatSet ? " (вже збережено — залиште порожнім щоб не змінювати)" : ""}</label>
            <input type="password" id="gh-pat" placeholder="${ghPatSet ? "••••••••" : "ghp_…"}">
          </div>
          <div class="e-form-field">
            <label for="gh-interval">Інтервал автосинхронізації (хвилини)</label>
            <input type="number" id="gh-interval" min="5" max="1440" value="${ghIntervalMinutes}">
          </div>
        </div>
        <div class="e-longdoc-actions" style="margin-top:10px; gap:8px">
          ${button("Зберегти GitHub", "save-github", "primary")}
          ${button("Push зараз", "push-github")}
        </div>
        <div id="gh-status" class="e-longdoc-meta" style="margin-top:6px"></div>
      </section>
    </div>
  `;
  bindActions(settingsPanelEl, {
    "save-settings": async () => {
      const body = {};
      settingsPanelEl.querySelectorAll("[data-setting]").forEach(input => {
        body[input.dataset.setting] = Boolean(input.checked);
      });
      await api(`/api/projects/${cfg.projectId}/longdoc/settings/`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      await refreshProjectMeta();
      await loadLongdocData();
    },
    "save-github": async () => {
      const statusEl = settingsPanelEl.querySelector("#gh-status");
      const repoUrl = settingsPanelEl.querySelector("#gh-repo-url")?.value.trim() || "";
      const pat = settingsPanelEl.querySelector("#gh-pat")?.value || "";
      const syncEnabled = Boolean(settingsPanelEl.querySelector("#gh-sync-enabled")?.checked);
      const intervalMinutes = parseInt(settingsPanelEl.querySelector("#gh-interval")?.value || "30", 10);
      const body = {
        github_repo_url: repoUrl,
        github_sync_enabled: syncEnabled,
        github_sync_interval_minutes: Number.isFinite(intervalMinutes) ? intervalMinutes : 30,
      };
      if (pat) body.github_pat = pat;
      if (statusEl) statusEl.textContent = "Зберігаємо…";
      try {
        await api(`/api/projects/${cfg.projectId}/`, { method: "PATCH", body: JSON.stringify(body) });
        await refreshProjectMeta();
        if (statusEl) statusEl.textContent = "Збережено.";
        renderSettingsPanel();
      } catch (err) {
        if (statusEl) statusEl.textContent = `Помилка: ${err.message}`;
      }
    },
    "push-github": async () => {
      const statusEl = settingsPanelEl.querySelector("#gh-status");
      if (statusEl) statusEl.textContent = "Пушимо…";
      try {
        await api(`/api/projects/${cfg.projectId}/github-sync/`, { method: "POST" });
        if (statusEl) statusEl.textContent = "Push успішно виконано.";
      } catch (err) {
        if (statusEl) statusEl.textContent = `Помилка: ${err.message}`;
      }
    },
  });
}

function renderOverviewPanel() {
  if (!overviewPanelEl) return;
  if (!longdocEnabled()) return renderDisabledPanel(overviewPanelEl, "робочий простір");

  const overview = s.longdoc.overview || {};
  const taskCounts = overview.task_counts || {};
  const coverageCounts = overview.requirement_coverage_counts || {};
  const openTasks = Number(taskCounts.open || 0) + Number(taskCounts.in_progress || 0);
  const issueReqs = Number(coverageCounts.unchecked || 0) + Number(coverageCounts.partial || 0) + Number(coverageCounts.missing || 0);
  const session = overview.active_proposal || s.longdoc.activeSession;

  overviewPanelEl.innerHTML = `
    <div class="e-workspace-head">
      <div>
        <h2>Письмовий асистент</h2>
        <p>Огляд структури, контексту, завдань і покриття для довгого документа.</p>
      </div>
      ${session ? chip("warn", `Зміна #${session.id}`) : chip("covered", "Готово до роботи")}
    </div>
    <div class="e-longdoc-scroll">
      <section class="e-overview-grid">
        <button class="e-overview-tile" type="button" data-action="go-outline">
          <strong>${overview.outline_item_count || 0}</strong>
          <span>пунктів плану</span>
        </button>
        <button class="e-overview-tile" type="button" data-action="go-context">
          <strong>${overview.context_file_count || 0}</strong>
          <span>файлів контексту</span>
        </button>
        <button class="e-overview-tile ${openTasks ? "attention" : ""}" type="button" data-action="go-tasks">
          <strong>${openTasks}</strong>
          <span>активних завдань</span>
        </button>
        <button class="e-overview-tile ${issueReqs ? "attention" : ""}" type="button" data-action="go-requirements">
          <strong>${issueReqs}</strong>
          <span>вимог потребують уваги</span>
        </button>
      </section>
      <section class="e-workspace-section">
        <div class="e-section-title">
          <h3>Стан документа</h3>
          <span>${overview.summary_count || 0} підсумків · ${overview.stale_summary_count || 0} застарілих</span>
        </div>
        <div class="e-status-row">
          ${chip("missing", `${coverageCounts.missing || 0} відсутні`)}
          ${chip("partial", `${coverageCounts.partial || 0} частково`)}
          ${chip("covered", `${coverageCounts.covered || 0} покрито`)}
          ${chip("unchecked", `${coverageCounts.unchecked || 0} не перевірено`)}
        </div>
      </section>
      <section class="e-workspace-section">
        <div class="e-section-title">
          <h3>Найближчі завдання</h3>
          ${button("Відкрити завдання", "go-tasks")}
        </div>
        ${(overview.tasks || []).slice(0, 5).map(item => `
          <article class="e-compact-row">
            ${chip(item.status || "open")}
            <span>${escHtml(item.description || "")}</span>
          </article>
        `).join("") || emptyCard("Завдань немає", "Додайте завдання, щоб тримати наступні кроки під рукою.")}
      </section>
      <section class="e-workspace-section">
        <div class="e-section-title">
          <h3>Швидкий доступ</h3>
        </div>
        <div class="e-quick-actions">
          ${button("План", "go-outline")}
          ${button("Контекст", "go-context")}
          ${button("Вимоги", "go-requirements")}
          ${button("Нотатки", "go-notes")}
          ${button("Налаштування", "go-settings")}
        </div>
      </section>
    </div>
  `;
  bindActions(overviewPanelEl, {
    "go-outline": async () => switchAssistantTab("outline"),
    "go-context": async () => switchAssistantTab("context"),
    "go-tasks": async () => switchAssistantTab("tasks"),
    "go-requirements": async () => switchAssistantTab("requirements"),
    "go-notes": async () => switchAssistantTab("notes"),
    "go-settings": async () => switchAssistantTab("settings"),
  });
}

function renderContextPanel() {
  if (!contextPanelEl) return;
  if (!longdocEnabled()) return renderDisabledPanel(contextPanelEl, "контекст");
  if (!featureEnabled("context_enabled")) return renderFeatureOffPanel(contextPanelEl, "Контекст");

  const items = s.longdoc.contextFiles || [];
  const rows = items.map(item => `
    <article class="e-library-card" data-context-file="${escHtml(item.filename)}">
      <div class="e-longdoc-card-head">
        <div>
          <strong>${escHtml(item.display_name || item.filename)}</strong>
          <div class="e-longdoc-meta">${escHtml(item.filename)} · ${item.size_bytes || 0} B</div>
        </div>
        <div class="e-longdoc-actions">
          ${button("Відкрити файл", "open-context-source")}
          ${button(isEditing("context", item.filename) ? "Скасувати" : "Редагувати", isEditing("context", item.filename) ? "cancel-context-edit" : "edit-context")}
        </div>
      </div>
      ${isEditing("context", item.filename) ? `
        <input class="e-longdoc-input" data-field="display_name" value="${escHtml(item.display_name || "")}" placeholder="Назва для показу">
        <textarea class="e-longdoc-textarea small" data-field="description" placeholder="Короткий опис">${escHtml(item.description || "")}</textarea>
        <textarea class="e-longdoc-textarea" data-field="content" placeholder="Вміст контекстного файлу">${escHtml(item.content || "")}</textarea>
        <div class="e-longdoc-actions">
          ${button("Зберегти", "save-context", "primary")}
          ${button("Видалити", "delete-context", "danger")}
        </div>
      ` : `
        <p>${escHtml(item.description || "Довідковий файл без опису.")}</p>
        <div class="e-preview-box">${escHtml((item.content || "").slice(0, 520))}${(item.content || "").length > 520 ? "\n..." : ""}</div>
      `}
    </article>
  `).join("");

  contextPanelEl.innerHTML = `
    <div class="e-workspace-head">
      <div>
        <h2>Бібліотека контексту</h2>
        <p>Джерела, вимоги та нотатки, які асистент має враховувати під час роботи.</p>
      </div>
      <span class="e-longdoc-meta">${items.length} файлів</span>
    </div>
    <div class="e-longdoc-scroll">
      ${renderCreatePanel("context", "контекст", `
        <input id="new-context-filename" class="e-longdoc-input" placeholder="project-notes.md">
        <input id="new-context-display-name" class="e-longdoc-input" placeholder="Назва для показу">
        <textarea id="new-context-description" class="e-longdoc-textarea small" placeholder="Опис"></textarea>
        <textarea id="new-context-content" class="e-longdoc-textarea" placeholder="Вміст контексту"></textarea>
        <div class="e-longdoc-actions">${button("Створити", "create-context", "primary")}</div>
      `)}
      <div class="e-card-grid">${rows || emptyCard("Контексту ще немає", "Створіть довідковий файл, щоб зафіксувати обмеження, джерела або стиль.")}</div>
    </div>
  `;

  bindActions(contextPanelEl, {
    "show-create-context": async () => { setCreating("context", true); renderContextPanel(); },
    "hide-create-context": async () => { setCreating("context", false); renderContextPanel(); },
    "create-context": async () => {
      await api(`/api/projects/${cfg.projectId}/context-files/`, {
        method: "POST",
        body: JSON.stringify({
          filename: document.getElementById("new-context-filename")?.value || "",
          display_name: document.getElementById("new-context-display-name")?.value || "",
          description: document.getElementById("new-context-description")?.value || "",
          content: document.getElementById("new-context-content")?.value || "",
        }),
      });
      setCreating("context", false);
      await loadLongdocData();
    },
    "edit-context": async (buttonEl) => {
      const filename = buttonEl.closest("[data-context-file]")?.dataset.contextFile;
      if (filename) setEditing("context", filename, true);
      renderContextPanel();
    },
    "cancel-context-edit": async (buttonEl) => {
      const filename = buttonEl.closest("[data-context-file]")?.dataset.contextFile;
      if (filename) setEditing("context", filename, false);
      renderContextPanel();
    },
    "save-context": async (buttonEl) => {
      const card = buttonEl.closest("[data-context-file]");
      const filename = card?.dataset.contextFile;
      if (!filename) return;
      await api(`/api/projects/${cfg.projectId}/context-files/${encodeURIComponent(filename)}/`, {
        method: "PATCH",
        body: JSON.stringify({
          display_name: card.querySelector('[data-field="display_name"]')?.value || "",
          description: card.querySelector('[data-field="description"]')?.value || "",
          content: card.querySelector('[data-field="content"]')?.value || "",
        }),
      });
      setEditing("context", filename, false);
      await loadLongdocData();
    },
    "open-context-source": async (buttonEl) => {
      const card = buttonEl.closest("[data-context-file]");
      const filename = card?.dataset.contextFile;
      await openSourceFile(`.smarttex/context/${filename}`);
    },
    "delete-context": async (buttonEl) => {
      const card = buttonEl.closest("[data-context-file]");
      const filename = card?.dataset.contextFile;
      if (!filename || !(await showConfirm(`Видалити ${filename}?`))) return;
      await api(`/api/projects/${cfg.projectId}/context-files/${encodeURIComponent(filename)}/`, {
        method: "DELETE",
        body: JSON.stringify({}),
      });
      await loadLongdocData();
    },
  });
}

function renderOutlinePanel() {
  if (!outlinePanelEl) return;
  if (!longdocEnabled()) return renderDisabledPanel(outlinePanelEl, "план");
  if (!featureEnabled("outline_enabled")) return renderFeatureOffPanel(outlinePanelEl, "План");

  const items = s.longdoc.outlineItems || [];
  const summaries = s.longdoc.sectionSummaries || [];
  const sections = s.sections || [];
  const summariesEnabled = featureEnabled("summaries_enabled");
  const summaryByTitle = new Map(summaries.map(item => [item.section_title, item]));
  const sectionByTitle = new Map(sections.map(item => [item.title, item]));
  const unmatchedSections = sections.filter(item => !items.some(outlineItem => outlineItem.title === item.title));
  outlinePanelEl.innerHTML = `
    <div class="e-workspace-head">
      <div>
        <h2>План документа</h2>
        <p>Список очікуваних секцій із відповідністю до живої структури документа.</p>
      </div>
      <span class="e-longdoc-meta">${items.length} пунктів${summariesEnabled ? ` · ${summaries.length} підсумків` : ""}</span>
    </div>
    <div class="e-longdoc-scroll">
      ${renderCreatePanel("outline", "пункт плану", `
        <input id="new-outline-title" class="e-longdoc-input" placeholder="Назва секції">
        <div class="e-longdoc-grid">
          <input id="new-outline-level" class="e-longdoc-input" type="number" min="1" max="6" value="1" placeholder="Рівень">
          <select id="new-outline-status" class="e-longdoc-input">
            <option value="missing">Відсутній</option>
            <option value="stub">Чернетка-скелет</option>
            <option value="draft">Чернетка</option>
            <option value="done">Готово</option>
          </select>
        </div>
        <textarea id="new-outline-notes" class="e-longdoc-textarea small" placeholder="Нотатки"></textarea>
        <div class="e-longdoc-actions">${button("Додати", "create-outline", "primary")}</div>
      `)}
      <div class="e-plan-list">
      ${items.map(item => `
        <article class="e-plan-row level-${Math.min(Number(item.level || 1), 6)}" data-outline-id="${item.id}">
          <div class="e-longdoc-card-head">
            <div>
              <strong>${escHtml(item.title)}</strong>
              <div class="e-longdoc-meta">#${item.order} · H${item.level}${item.expected_pages ? ` · ${item.expected_pages} стор.` : ""}</div>
            </div>
            <div class="e-status-row">
              ${chip(item.status)}
              ${sectionByTitle.get(item.title) ? chip("covered", "Знайдено в документі") : chip("missing", "Не знайдено")}
              ${summariesEnabled ? (summaryByTitle.get(item.title) ? (summaryByTitle.get(item.title).is_stale ? chip("partial", "Підсумок застарів") : chip("covered", "Підсумок актуальний")) : chip("unchecked", "Без підсумку")) : ""}
            </div>
          </div>
          ${isEditing("outline", item.id) ? `
          <input class="e-longdoc-input" data-field="title" value="${escHtml(item.title)}">
          <div class="e-longdoc-grid">
            <input class="e-longdoc-input" data-field="level" type="number" min="1" max="6" value="${item.level}">
            <input class="e-longdoc-input" data-field="order" type="number" min="1" value="${item.order}">
            <select class="e-longdoc-input" data-field="status">
              <option value="missing" ${item.status === "missing" ? "selected" : ""}>Відсутній</option>
              <option value="stub" ${item.status === "stub" ? "selected" : ""}>Чернетка-скелет</option>
              <option value="draft" ${item.status === "draft" ? "selected" : ""}>Чернетка</option>
              <option value="done" ${item.status === "done" ? "selected" : ""}>Готово</option>
            </select>
          </div>
          <textarea class="e-longdoc-textarea small" data-field="notes" placeholder="Нотатки">${escHtml(item.notes || "")}</textarea>
          ` : renderTextBlock(item.notes, "Нотаток до цього пункту немає.")}
          ${summariesEnabled ? `<section class="e-longdoc-subcard" data-summary-title="${escHtml(item.title)}" data-summary-section-index="${sectionByTitle.get(item.title)?.index ?? ""}" data-summary-source-file="${escHtml(sectionByTitle.get(item.title)?.file_name || summaryByTitle.get(item.title)?.source_file || "")}">
            <div class="e-longdoc-card-head">
              <strong>Підсумок секції</strong>
              ${summaryByTitle.get(item.title) ? (summaryByTitle.get(item.title).is_stale ? chip("partial", "Застарів") : chip("covered", "Актуальний")) : chip("unchecked", "Відсутній")}
            </div>
            ${sectionByTitle.get(item.title) || summaryByTitle.get(item.title) ? `
              ${isEditing("summary", item.title) ? `<textarea class="e-longdoc-textarea small" data-field="summary_text" placeholder="Що зараз каже ця секція">${escHtml(summaryByTitle.get(item.title)?.summary_text || "")}</textarea>` : renderTextBlock(summaryByTitle.get(item.title)?.summary_text, "Підсумок ще не заповнено.")}
              <div class="e-longdoc-meta">Джерело: ${escHtml((sectionByTitle.get(item.title)?.file_name || summaryByTitle.get(item.title)?.source_file || s.mainFileName) + (summaryByTitle.get(item.title)?.source_line_start ? `:${summaryByTitle.get(item.title).source_line_start}-${summaryByTitle.get(item.title).source_line_end}` : ""))}</div>
              <div class="e-longdoc-actions">${isEditing("summary", item.title) ? button("Зберегти підсумок", "save-summary", "primary") : button("Редагувати підсумок", "edit-summary")}</div>
            ` : `
              <div class="e-longdoc-meta">Живої секції з такою назвою ще не знайдено.</div>
            `}
          </section>` : ""}
          <div class="e-longdoc-actions">
            ${isEditing("outline", item.id) ? `${button("Зберегти", "save-outline", "primary")} ${button("Скасувати", "cancel-outline-edit")}` : button("Редагувати", "edit-outline")}
            ${button("Видалити", "delete-outline", "danger")}
          </div>
        </article>
      `).join("") || emptyCard("Плану ще немає", "Додайте очікувані секції документа.")}
      </div>
      ${summariesEnabled && unmatchedSections.length ? `
        <section class="e-workspace-section">
          <div class="e-section-title"><h3>Секції документа без пункту плану</h3></div>
          ${unmatchedSections.map(item => {
            const summary = summaryByTitle.get(item.title);
            return `
              <article class="e-longdoc-subcard" data-summary-title="${escHtml(item.title)}" data-summary-section-index="${item.index}" data-summary-source-file="${escHtml(item.file_name || s.mainFileName)}">
                <div class="e-longdoc-card-head">
                  <strong>${escHtml(item.title)}</strong>
                  ${summary ? (summary.is_stale ? chip("partial", "Застарів") : chip("covered", "Актуальний")) : chip("unchecked", "Без підсумку")}
                </div>
                ${isEditing("summary", item.title) ? `<textarea class="e-longdoc-textarea small" data-field="summary_text" placeholder="Що зараз каже ця секція">${escHtml(summary?.summary_text || "")}</textarea>` : renderTextBlock(summary?.summary_text, "Підсумок ще не заповнено.")}
                <div class="e-longdoc-meta">${escHtml((item.file_name || s.mainFileName) + `:${item.start_line}-${item.end_line}`)}</div>
                <div class="e-longdoc-actions">${isEditing("summary", item.title) ? button("Зберегти підсумок", "save-summary", "primary") : button("Редагувати підсумок", "edit-summary")}</div>
              </article>
            `;
          }).join("")}
        </section>
      ` : ""}
    </div>
  `;

  bindActions(outlinePanelEl, {
    "show-create-outline": async () => { setCreating("outline", true); renderOutlinePanel(); },
    "hide-create-outline": async () => { setCreating("outline", false); renderOutlinePanel(); },
    "create-outline": async () => {
      await api(`/api/projects/${cfg.projectId}/outline-items/`, {
        method: "POST",
        body: JSON.stringify({
          title: document.getElementById("new-outline-title")?.value || "",
          level: Number(document.getElementById("new-outline-level")?.value || 1),
          status: document.getElementById("new-outline-status")?.value || "missing",
          notes: document.getElementById("new-outline-notes")?.value || "",
        }),
      });
      setCreating("outline", false);
      await loadLongdocData();
    },
    "edit-outline": async (buttonEl) => {
      const itemId = buttonEl.closest("[data-outline-id]")?.dataset.outlineId;
      if (itemId) setEditing("outline", itemId, true);
      renderOutlinePanel();
    },
    "cancel-outline-edit": async (buttonEl) => {
      const itemId = buttonEl.closest("[data-outline-id]")?.dataset.outlineId;
      if (itemId) setEditing("outline", itemId, false);
      renderOutlinePanel();
    },
    "save-outline": async (buttonEl) => {
      const card = buttonEl.closest("[data-outline-id]");
      const itemId = card?.dataset.outlineId;
      if (!itemId) return;
      await api(`/api/projects/${cfg.projectId}/outline-items/${itemId}/`, {
        method: "PATCH",
        body: JSON.stringify({
          title: card.querySelector('[data-field="title"]')?.value || "",
          level: Number(card.querySelector('[data-field="level"]')?.value || 1),
          order: Number(card.querySelector('[data-field="order"]')?.value || 1),
          status: card.querySelector('[data-field="status"]')?.value || "missing",
          notes: card.querySelector('[data-field="notes"]')?.value || "",
        }),
      });
      setEditing("outline", itemId, false);
      await loadLongdocData();
    },
    "edit-summary": async (buttonEl) => {
      const title = buttonEl.closest("[data-summary-title]")?.dataset.summaryTitle;
      if (title) setEditing("summary", title, true);
      renderOutlinePanel();
    },
    "save-summary": async (buttonEl) => {
      const card = buttonEl.closest("[data-summary-title]");
      const sectionTitle = card?.dataset.summaryTitle;
      if (!sectionTitle) return;
      const rawIndex = card.dataset.summarySectionIndex;
      await api(`/api/projects/${cfg.projectId}/section-summaries/`, {
        method: "POST",
        body: JSON.stringify({
          section_title: sectionTitle,
          section_index: rawIndex ? Number(rawIndex) : null,
          source_file: card.dataset.summarySourceFile || s.mainFileName,
          summary_text: card.querySelector('[data-field="summary_text"]')?.value || "",
        }),
      });
      setEditing("summary", sectionTitle, false);
      await loadLongdocData();
    },
    "delete-outline": async (buttonEl) => {
      const card = buttonEl.closest("[data-outline-id]");
      const itemId = card?.dataset.outlineId;
      if (!itemId || !(await showConfirm("Видалити пункт плану?"))) return;
      await api(`/api/projects/${cfg.projectId}/outline-items/${itemId}/`, {
        method: "DELETE",
        body: JSON.stringify({}),
      });
      await loadLongdocData();
    },
  });
}

function renderTasksPanel() {
  if (!tasksPanelEl) return;
  if (!longdocEnabled()) return renderDisabledPanel(tasksPanelEl, "завдання");
  if (!featureEnabled("tasks_enabled")) return renderFeatureOffPanel(tasksPanelEl, "Завдання");

  const items = s.longdoc.tasks || [];
  const columns = [
    ["open", "Відкриті"],
    ["in_progress", "У роботі"],
    ["done", "Готові"],
  ];
  const taskCard = item => `
    <article class="e-task-card" data-task-id="${item.id}">
      ${isEditing("task", item.id) ? `
      <textarea class="e-longdoc-textarea small" data-field="description">${escHtml(item.description || "")}</textarea>
      <select class="e-longdoc-input" data-field="status">
        <option value="open" ${item.status === "open" ? "selected" : ""}>Відкрита</option>
        <option value="in_progress" ${item.status === "in_progress" ? "selected" : ""}>У роботі</option>
        <option value="done" ${item.status === "done" ? "selected" : ""}>Готово</option>
      </select>
      <div class="e-longdoc-actions">
        ${button("Зберегти", "save-task", "primary")}
        ${button("Скасувати", "cancel-task-edit")}
      </div>
      ` : `
        <div class="e-task-line">
          <span class="e-check-dot ${item.status === "done" ? "done" : ""}"></span>
          <span>${escHtml(item.description || "")}</span>
        </div>
        <div class="e-longdoc-meta">${item.completed_at ? "Завершено" : "Очікує"}</div>
        <div class="e-longdoc-actions">
          ${item.status !== "done" ? button("Готово", "complete-task") : ""}
          ${button("Редагувати", "edit-task")}
          ${button("Видалити", "delete-task", "danger")}
        </div>
      `}
    </article>
  `;
  tasksPanelEl.innerHTML = `
    <div class="e-workspace-head">
      <div>
        <h2>Завдання</h2>
        <p>Короткий список дій без постійно відкритих форм редагування.</p>
      </div>
      <span class="e-longdoc-meta">${items.length} завдань</span>
    </div>
    <div class="e-longdoc-scroll">
      ${renderCreatePanel("task", "завдання", `
        <textarea id="new-task-description" class="e-longdoc-textarea small" placeholder="Опис завдання"></textarea>
        <div class="e-longdoc-actions">${button("Додати", "create-task", "primary")}</div>
      `)}
      <div class="e-task-board">
        ${columns.map(([status, label]) => {
          const columnItems = items.filter(item => (item.status || "open") === status);
          return `
            <section class="e-task-column">
              <div class="e-task-column-head">${escHtml(label)} · ${columnItems.length}</div>
              ${columnItems.map(taskCard).join("") || emptyCard("Порожньо", "У цій колонці немає завдань.")}
            </section>
          `;
        }).join("")}
      </div>
    </div>
  `;

  bindActions(tasksPanelEl, {
    "show-create-task": async () => { setCreating("task", true); renderTasksPanel(); },
    "hide-create-task": async () => { setCreating("task", false); renderTasksPanel(); },
    "create-task": async () => {
      await api(`/api/projects/${cfg.projectId}/tasks/`, {
        method: "POST",
        body: JSON.stringify({ description: document.getElementById("new-task-description")?.value || "" }),
      });
      setCreating("task", false);
      await loadLongdocData();
    },
    "edit-task": async (buttonEl) => {
      const taskId = buttonEl.closest("[data-task-id]")?.dataset.taskId;
      if (taskId) setEditing("task", taskId, true);
      renderTasksPanel();
    },
    "cancel-task-edit": async (buttonEl) => {
      const taskId = buttonEl.closest("[data-task-id]")?.dataset.taskId;
      if (taskId) setEditing("task", taskId, false);
      renderTasksPanel();
    },
    "save-task": async (buttonEl) => {
      const card = buttonEl.closest("[data-task-id]");
      const taskId = card?.dataset.taskId;
      if (!taskId) return;
      await api(`/api/projects/${cfg.projectId}/tasks/${taskId}/`, {
        method: "PATCH",
        body: JSON.stringify({
          description: card.querySelector('[data-field="description"]')?.value || "",
          status: card.querySelector('[data-field="status"]')?.value || "open",
        }),
      });
      setEditing("task", taskId, false);
      await loadLongdocData();
    },
    "complete-task": async (buttonEl) => {
      const card = buttonEl.closest("[data-task-id]");
      const taskId = card?.dataset.taskId;
      if (!taskId) return;
      await api(`/api/projects/${cfg.projectId}/tasks/${taskId}/`, {
        method: "PATCH",
        body: JSON.stringify({ status: "done" }),
      });
      await loadLongdocData();
    },
    "delete-task": async (buttonEl) => {
      const card = buttonEl.closest("[data-task-id]");
      const taskId = card?.dataset.taskId;
      if (!taskId || !(await showConfirm("Видалити завдання?"))) return;
      await api(`/api/projects/${cfg.projectId}/tasks/${taskId}/`, {
        method: "DELETE",
        body: JSON.stringify({}),
      });
      await loadLongdocData();
    },
  });
}

function renderNotesPanel() {
  if (!notesPanelEl) return;
  if (!longdocEnabled()) return renderDisabledPanel(notesPanelEl, "нотатки");
  if (!featureEnabled("notes_enabled")) return renderFeatureOffPanel(notesPanelEl, "Нотатки");

  const items = s.longdoc.noteSections || [];
  notesPanelEl.innerHTML = `
    <div class="e-workspace-head">
      <div>
        <h2>Нотатник</h2>
        <p>Розділи нотаток відображаються як читабельні записи, редагування відкривається окремо.</p>
      </div>
      <span class="e-longdoc-meta">${items.length} розділів</span>
    </div>
    <div class="e-longdoc-scroll">
      ${renderCreatePanel("note", "нотатку", `
        <input id="new-note-heading" class="e-longdoc-input" placeholder="Заголовок">
        <textarea id="new-note-body" class="e-longdoc-textarea" placeholder="Текст нотатки"></textarea>
        <div class="e-longdoc-actions">${button("Додати", "create-note", "primary")}</div>
      `)}
      <div class="e-notebook">
      ${items.map(item => `
        <article class="e-note-page" data-note-id="${item.id}">
          ${isEditing("note", item.id) ? `
          <input class="e-longdoc-input" data-field="heading" value="${escHtml(item.heading || "")}">
          <textarea class="e-longdoc-textarea" data-field="body">${escHtml(item.body || "")}</textarea>
          <div class="e-longdoc-actions">
            ${button("Зберегти", "save-note", "primary")}
            ${button("Скасувати", "cancel-note-edit")}
            ${button("Видалити", "delete-note", "danger")}
          </div>
          ` : `
            <div class="e-longdoc-card-head">
              <h3>${escHtml(item.heading || "Без заголовка")}</h3>
              ${button("Редагувати", "edit-note")}
            </div>
            ${renderTextBlock(item.body, "Нотатка порожня.")}
            <div class="e-longdoc-meta">Оновлено: ${escHtml(item.updated_at || "")}</div>
          `}
        </article>
      `).join("") || emptyCard("Нотаток ще немає", "Створіть перший розділ нотатника.")}
      </div>
    </div>
  `;

  bindActions(notesPanelEl, {
    "show-create-note": async () => { setCreating("note", true); renderNotesPanel(); },
    "hide-create-note": async () => { setCreating("note", false); renderNotesPanel(); },
    "create-note": async () => {
      await api(`/api/projects/${cfg.projectId}/note-sections/`, {
        method: "POST",
        body: JSON.stringify({
          heading: document.getElementById("new-note-heading")?.value || "",
          body: document.getElementById("new-note-body")?.value || "",
        }),
      });
      setCreating("note", false);
      await loadLongdocData();
    },
    "edit-note": async (buttonEl) => {
      const noteId = buttonEl.closest("[data-note-id]")?.dataset.noteId;
      if (noteId) setEditing("note", noteId, true);
      renderNotesPanel();
    },
    "cancel-note-edit": async (buttonEl) => {
      const noteId = buttonEl.closest("[data-note-id]")?.dataset.noteId;
      if (noteId) setEditing("note", noteId, false);
      renderNotesPanel();
    },
    "save-note": async (buttonEl) => {
      const card = buttonEl.closest("[data-note-id]");
      const noteId = card?.dataset.noteId;
      if (!noteId) return;
      await api(`/api/projects/${cfg.projectId}/note-sections/${noteId}/`, {
        method: "PATCH",
        body: JSON.stringify({
          heading: card.querySelector('[data-field="heading"]')?.value || "",
          body: card.querySelector('[data-field="body"]')?.value || "",
        }),
      });
      setEditing("note", noteId, false);
      await loadLongdocData();
    },
    "delete-note": async (buttonEl) => {
      const card = buttonEl.closest("[data-note-id]");
      const noteId = card?.dataset.noteId;
      if (!noteId || !(await showConfirm("Видалити розділ нотаток?"))) return;
      await api(`/api/projects/${cfg.projectId}/note-sections/${noteId}/`, {
        method: "DELETE",
        body: JSON.stringify({}),
      });
      await loadLongdocData();
    },
  });
}

function renderRequirementsPanel() {
  if (!requirementsPanelEl) return;
  if (!longdocEnabled()) return renderDisabledPanel(requirementsPanelEl, "вимоги");
  if (!featureEnabled("requirements_enabled")) return renderFeatureOffPanel(requirementsPanelEl, "Вимоги");

  const items = s.longdoc.requirements || [];
  const counts = items.reduce((acc, item) => {
    acc[item.coverage || "unchecked"] = (acc[item.coverage || "unchecked"] || 0) + 1;
    return acc;
  }, {});
  requirementsPanelEl.innerHTML = `
    <div class="e-workspace-head">
      <div>
        <h2>Покриття вимог</h2>
        <p>Дашборд вимог зі статусами та посиланнями на секції без comma-separated полів у режимі читання.</p>
      </div>
      <span class="e-longdoc-meta">${items.length} вимог</span>
    </div>
    <div class="e-longdoc-scroll">
      <section class="e-overview-grid">
        <div class="e-overview-tile">${chip("covered", `${counts.covered || 0} покрито`)}</div>
        <div class="e-overview-tile">${chip("partial", `${counts.partial || 0} частково`)}</div>
        <div class="e-overview-tile">${chip("missing", `${counts.missing || 0} відсутні`)}</div>
        <div class="e-overview-tile">${chip("unchecked", `${counts.unchecked || 0} не перевірено`)}</div>
      </section>
      ${renderCreatePanel("requirement", "вимогу", `
        <div class="e-longdoc-grid">
          <input id="new-requirement-id" class="e-longdoc-input" placeholder="R-01">
          <select id="new-requirement-coverage" class="e-longdoc-input">
            <option value="unchecked">Не перевірено</option>
            <option value="covered">Покрито</option>
            <option value="partial">Частково</option>
            <option value="missing">Відсутня</option>
          </select>
        </div>
        <textarea id="new-requirement-description" class="e-longdoc-textarea small" placeholder="Опис вимоги"></textarea>
        <textarea id="new-requirement-notes" class="e-longdoc-textarea small" placeholder="Нотатки щодо покриття"></textarea>
        <input id="new-requirement-refs" class="e-longdoc-input" placeholder="Секції через кому">
        <div class="e-longdoc-actions">${button("Додати", "create-requirement", "primary")}</div>
      `)}
      <div class="e-card-grid">
      ${items.map(item => `
        <article class="e-requirement-card" data-requirement-id="${item.id}">
          <div class="e-longdoc-card-head">
            <div>
              <strong>${escHtml(item.req_id)}</strong>
              <div class="e-longdoc-meta">Оновлено: ${escHtml(item.updated_at || "")}</div>
            </div>
            ${chip(item.coverage)}
          </div>
          ${isEditing("requirement", item.id) ? `
          <textarea class="e-longdoc-textarea small" data-field="description">${escHtml(item.description || "")}</textarea>
          <div class="e-longdoc-grid">
            <select class="e-longdoc-input" data-field="coverage">
              <option value="unchecked" ${item.coverage === "unchecked" ? "selected" : ""}>Не перевірено</option>
              <option value="covered" ${item.coverage === "covered" ? "selected" : ""}>Покрито</option>
              <option value="partial" ${item.coverage === "partial" ? "selected" : ""}>Частково</option>
              <option value="missing" ${item.coverage === "missing" ? "selected" : ""}>Відсутня</option>
            </select>
            <input class="e-longdoc-input" data-field="req_id" value="${escHtml(item.req_id || "")}">
          </div>
          <textarea class="e-longdoc-textarea small" data-field="notes" placeholder="Нотатки щодо покриття">${escHtml(item.notes || "")}</textarea>
          <input class="e-longdoc-input" data-field="section_refs" value="${escHtml((item.section_refs || []).join(", "))}" placeholder="Секції через кому">
          <div class="e-longdoc-actions">${button("Зберегти", "save-requirement", "primary")} ${button("Скасувати", "cancel-requirement-edit")}</div>
          ` : `
            ${renderTextBlock(item.description, "Опис вимоги не заповнено.")}
            <div>
              <div class="e-longdoc-meta">Секції</div>
              ${renderSectionRefChips(item.section_refs || [])}
            </div>
            ${item.notes ? `<div><div class="e-longdoc-meta">Нотатки</div>${renderTextBlock(item.notes)}</div>` : ""}
            <div class="e-longdoc-actions">${button("Редагувати", "edit-requirement")}</div>
          `}
        </article>
      `).join("") || emptyCard("Вимог ще немає", "Додайте вимоги, щоб відстежувати покриття документа.")}
      </div>
    </div>
  `;

  bindActions(requirementsPanelEl, {
    "show-create-requirement": async () => { setCreating("requirement", true); renderRequirementsPanel(); },
    "hide-create-requirement": async () => { setCreating("requirement", false); renderRequirementsPanel(); },
    "create-requirement": async () => {
      await api(`/api/projects/${cfg.projectId}/requirements/`, {
        method: "POST",
        body: JSON.stringify({
          req_id: document.getElementById("new-requirement-id")?.value || "",
          coverage: document.getElementById("new-requirement-coverage")?.value || "unchecked",
          description: document.getElementById("new-requirement-description")?.value || "",
          notes: document.getElementById("new-requirement-notes")?.value || "",
          section_refs: (document.getElementById("new-requirement-refs")?.value || "")
            .split(",")
            .map(value => value.trim())
            .filter(Boolean),
        }),
      });
      setCreating("requirement", false);
      await loadLongdocData();
    },
    "edit-requirement": async (buttonEl) => {
      const requirementId = buttonEl.closest("[data-requirement-id]")?.dataset.requirementId;
      if (requirementId) setEditing("requirement", requirementId, true);
      renderRequirementsPanel();
    },
    "cancel-requirement-edit": async (buttonEl) => {
      const requirementId = buttonEl.closest("[data-requirement-id]")?.dataset.requirementId;
      if (requirementId) setEditing("requirement", requirementId, false);
      renderRequirementsPanel();
    },
    "save-requirement": async (buttonEl) => {
      const card = buttonEl.closest("[data-requirement-id]");
      const requirementId = card?.dataset.requirementId;
      if (!requirementId) return;
      await api(`/api/projects/${cfg.projectId}/requirements/${requirementId}/`, {
        method: "PATCH",
        body: JSON.stringify({
          req_id: card.querySelector('[data-field="req_id"]')?.value || "",
          description: card.querySelector('[data-field="description"]')?.value || "",
          coverage: card.querySelector('[data-field="coverage"]')?.value || "unchecked",
          notes: card.querySelector('[data-field="notes"]')?.value || "",
          section_refs: (card.querySelector('[data-field="section_refs"]')?.value || "")
            .split(",")
            .map(value => value.trim())
            .filter(Boolean),
        }),
      });
      setEditing("requirement", requirementId, false);
      await loadLongdocData();
    },
  });
}

export function renderLongdocPanels() {
  renderSettingsPanel();
  renderOverviewPanel();
  renderContextPanel();
  renderOutlinePanel();
  renderTasksPanel();
  renderNotesPanel();
  renderRequirementsPanel();
}

// ── Suggested change UI ───────────────────────────────────────────────────────

const sessionBannerEl = document.getElementById("session-banner");
const sessionBannerGoalEl = document.getElementById("session-banner-goal");
const editorWrapEl = document.getElementById("editor-wrap");
const pdfTabbarEl = document.getElementById("pdf-tabbar");

export function renderSessionBanner() {
  const session = s.longdoc.activeSession;
  const isActive = session && ["validating", "failed_validation", "failed_compile", "ready_for_review"].includes(session.status);

  if (sessionBannerEl) sessionBannerEl.classList.toggle("visible", Boolean(isActive));
  if (editorWrapEl) editorWrapEl.classList.toggle("project-locked", Boolean(isActive));
  if (pdfTabbarEl) pdfTabbarEl.classList.toggle("visible", Boolean(isActive && cfg.sessionReview));

  if (!isActive) {
    if (cfg.sessionReview) {
      window.location.href = `/projects/${cfg.projectId}/`;
    }
    return;
  }

  if (sessionBannerGoalEl) sessionBannerGoalEl.textContent = session.goal || "";

  const statusLabels = {
    validating: "Готується",
    failed_validation: "Потребує уваги",
    failed_compile: "Не компілюється",
    ready_for_review: "Готово до перегляду",
  };
  const statusEl = document.getElementById("session-banner-status");
  if (statusEl) statusEl.textContent = statusLabels[session.status] || session.status;
}

function renderDiffContent(diffText) {
  if (!diffText) return `<span class="diff-empty">Змін не знайдено.</span>`;

  const lines = diffText.split("\n");
  let totalAdded = 0, totalRemoved = 0;
  const rows = [];
  let leftLine = 0, rightLine = 0;

  for (const raw of lines) {
    if (raw.startsWith("---") || raw.startsWith("+++")) {
      rows.push(`<div class="diff-meta">${escHtml(raw)}</div>`);
    } else if (raw.startsWith("@@")) {
      const m = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) { leftLine = parseInt(m[1], 10) - 1; rightLine = parseInt(m[2], 10) - 1; }
      rows.push(`<div class="diff-hunk">${escHtml(raw)}</div>`);
    } else if (raw.startsWith("+")) {
      rightLine++;
      totalAdded++;
      rows.push(`<div class="diff-row add"><span class="diff-sign">+</span><span class="diff-ln"></span><span class="diff-ln">${rightLine}</span><span class="diff-code">${escHtml(raw.slice(1))}</span></div>`);
    } else if (raw.startsWith("-")) {
      leftLine++;
      totalRemoved++;
      rows.push(`<div class="diff-row del"><span class="diff-sign">-</span><span class="diff-ln">${leftLine}</span><span class="diff-ln"></span><span class="diff-code">${escHtml(raw.slice(1))}</span></div>`);
    } else {
      leftLine++;
      rightLine++;
      rows.push(`<div class="diff-row ctx-empty"><span class="diff-sign"> </span><span class="diff-ln">${leftLine}</span><span class="diff-ln">${rightLine}</span><span class="diff-code">${escHtml(raw.slice(1))}</span></div>`);
    }
  }

  return `
    <div class="diff-summary">
      <span class="diff-chip add">+${totalAdded}</span>
      <span class="diff-chip del">-${totalRemoved}</span>
    </div>
    <div class="diff-table">${rows.join("")}</div>`;
}

async function openSessionDiffModal() {
  const overlay = document.getElementById("session-diff-overlay");
  const content = document.getElementById("session-diff-content");
  const subtitle = document.getElementById("session-diff-subtitle");
  if (!overlay) return;

  overlay.classList.add("open");
    if (content) content.innerHTML = `<span class="diff-empty">Завантаження diff...</span>`;

  try {
    const data = await api(`/api/projects/${cfg.projectId}/change-proposals/diff/`, { method: "GET" });
    const session = s.longdoc.activeSession;
    if (subtitle) subtitle.textContent = session ? `Suggested change #${session.id} · ${session.status}` : "";
    const warnings = data.smcl_warnings || session?.smcl_warnings || [];
    const warningHtml = warnings.length ? `
      <div class="diff-summary">
        <span class="diff-chip del">SMCL ${escHtml(data.smcl_risk_level || session?.smcl_risk_level || "medium")}</span>
        <span>${warnings.map(w => escHtml(w.message || w.code || "")).join(" · ")}</span>
      </div>
    ` : "";
    if (content) content.innerHTML = warningHtml + renderDiffContent(data.diff_text || "");

    const footer = document.getElementById("session-diff-footer");
    if (footer && session?.goal) {
      footer.innerHTML = `<span class="e-session-goal-label">Підсумок:</span> ${escHtml(session.goal || "")}`;
    }
  } catch (err) {
    if (content) content.innerHTML = `<span class="diff-empty">Помилка завантаження diff: ${escHtml(err.message)}</span>`;
  }
}

function closeSessionDiffModal() {
  const overlay = document.getElementById("session-diff-overlay");
  if (overlay) overlay.classList.remove("open");
}

let _stagingPdfMode = false;

function switchPdfTab(tab) {
  if (!cfg.sessionReview && tab === "staging") return;
  _stagingPdfMode = tab === "staging";
  document.querySelectorAll(".e-pdf-tab").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.pdfTab === tab);
  });
  const stagingUrl = `/api/projects/${cfg.projectId}/change-proposals/preview-pdf/`;
  if (_stagingPdfMode) {
    import("./pdfviewer.js").then(m => m.loadPdfViewer(stagingUrl)).catch(() => {});
  } else {
    import("./app.js").then(m => m.refreshLivePdfPreview?.()).catch(() => {});
  }
}

async function acceptSession() {
  if (!(await showConfirm("Прийняти запропоновану зміну? Її буде об'єднано з проєктом."))) return;
  try {
    const payload = await api(`/api/projects/${cfg.projectId}/change-proposals/accept/`, { method: "POST" });
    if (cfg.sessionReview) {
      window.location.href = `/projects/${cfg.projectId}/`;
      return;
    }
    s.longdoc.activeSession = null;
    renderSessionBanner();
    closeSessionDiffModal();
    const main = await import("./app.js");
    await Promise.all([
      loadLongdocData(),
      main.loadProjectMeta?.(),
      main.loadMainFile?.(),
      main.loadFiles?.(),
      main.loadSections?.(),
      main.loadVersions?.(true),
    ]);
    await main.refreshLivePdfPreview?.(payload?.pdf_url, payload?.pdf_version);
  } catch (err) {
    alert(`Не вдалося прийняти зміни: ${err.message}`);
  }
}

async function discardSession() {
  if (!(await showConfirm("Відхилити запропоновану зміну? Дію не можна скасувати."))) return;
  try {
    await api(`/api/projects/${cfg.projectId}/change-proposals/discard/`, { method: "POST" });
    if (cfg.sessionReview) {
      window.location.href = `/projects/${cfg.projectId}/`;
      return;
    }
    s.longdoc.activeSession = null;
    renderSessionBanner();
    closeSessionDiffModal();
    await loadLongdocData();
  } catch (err) {
    alert(`Не вдалося відхилити зміни: ${err.message}`);
  }
}

export function initSessionUI() {
  if (cfg.sessionReview) {
    editorWrapEl?.classList.add("project-locked");
  }
  const _toggleWA = () => {
    if (centerEl?.classList.contains("wa-active")) {
      setAssistantOpen(false);
      return;
    }
    switchAssistantTab("overview");
  };
  waTabBtnEl?.addEventListener("click", _toggleWA);
  document.querySelectorAll(".e-wa-tab[data-wa-tab]").forEach(btn => {
    btn.addEventListener("click", () => switchAssistantTab(btn.dataset.waTab || "outline"));
  });
  document.getElementById("session-btn-diff")?.addEventListener("click", openSessionDiffModal);
  document.getElementById("session-btn-staging")?.addEventListener("click", () => switchPdfTab("staging"));
  document.getElementById("session-btn-accept")?.addEventListener("click", acceptSession);
  document.getElementById("session-btn-discard")?.addEventListener("click", discardSession);
  document.getElementById("session-diff-close-btn")?.addEventListener("click", closeSessionDiffModal);
  document.getElementById("session-diff-accept-btn")?.addEventListener("click", acceptSession);
  document.getElementById("session-diff-discard-btn")?.addEventListener("click", discardSession);
  document.getElementById("session-diff-overlay")?.addEventListener("click", e => {
    if (e.target === e.currentTarget) closeSessionDiffModal();
  });
  document.getElementById("open-project-settings-btn")?.addEventListener("click", openAssistantSettings);
  document.getElementById("open-ai-log-btn")?.addEventListener("click", openAiLogModal);
  document.getElementById("ai-log-close-btn")?.addEventListener("click", closeAiLogModal);
  document.getElementById("ai-log-overlay")?.addEventListener("click", e => {
    if (e.target === e.currentTarget) closeAiLogModal();
  });
  document.querySelectorAll(".e-pdf-tab[data-pdf-tab]").forEach(btn => {
    btn.addEventListener("click", () => switchPdfTab(btn.dataset.pdfTab));
  });
}
