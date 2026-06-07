import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as cm from "./cm.js";
import * as ui from "./ui.js";

const { cfg, s } = state;
const { api } = apiMod;
const { escHtml, showAnnotationPopover, showConfirm } = ui;
const { getActiveSelectionDetails } = cm;

const contextPanelEl = document.getElementById("longdoc-context-panel");
const outlinePanelEl = document.getElementById("longdoc-outline-panel");
const tasksPanelEl = document.getElementById("longdoc-tasks-panel");
const annotationsPanelEl = document.getElementById("longdoc-annotations-panel");
const notesPanelEl = document.getElementById("longdoc-notes-panel");
const requirementsPanelEl = document.getElementById("longdoc-requirements-panel");
const settingsPanelEl = document.getElementById("longdoc-settings-panel");
const overviewPanelEl = document.getElementById("longdoc-overview-panel");
const overviewBadgeEl = document.getElementById("writing-assistant-badge");
const centerEl = document.getElementById("drop-zone");
const waTabBtnEl = document.getElementById("wa-tab-btn");
const readonlyOverlayEl = document.getElementById("readonly-overlay");
const readonlyDiscardSessionBtn = document.getElementById("readonly-discard-session-btn");
const readonlyUseWebBtn = document.getElementById("readonly-use-web-btn");
const annotationRailToggleBtn = document.getElementById("annotation-rail-toggle");
const annotationRailEl = document.getElementById("annotation-rail");

let _reloadProjectMeta = null;
let _refreshAnnotationMarkers = null;
let _annotationRailScrollBound = false;
let _annotationRailScrollEl = null;
let _annotationRailFrame = 0;
const uiState = {
  creating: new Set(),
  editing: new Set(),
};

export function setLongdocProjectMetaRef(fn) {
  _reloadProjectMeta = fn;
}

export function setLongdocAnnotationMarkersRef(fn) {
  _refreshAnnotationMarkers = fn;
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
    dismissed: "Відхилено",
    ai_draft: "AI на перевірці",
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

function isAiDraftAnnotation(item) {
  return String(item?.status || "") === "ai_draft";
}

async function updateAnnotationStatus(annotationId, status) {
  if (!annotationId) return;
  await api(`/api/projects/${cfg.projectId}/annotations/${annotationId}/`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
  await loadLongdocData();
  renderAnnotationRail();
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

function currentEditorFileName() {
  return String(s.activeTabName || s.selectedFile?.name || "").trim();
}

function activeAnnotationStatuses() {
  return new Set(["ai_draft", "open", "in_progress"]);
}

function quickAnnotationTemplates() {
  return Array.isArray(s.longdoc.settings?.quick_annotation_templates)
    ? s.longdoc.settings.quick_annotation_templates.filter(item => item && item.enabled !== false)
    : [];
}

export function getQuickAnnotationTemplates() {
  return quickAnnotationTemplates();
}

export function matchingQuickAnnotationTemplate(event) {
  return quickAnnotationTemplates().find(template => shortcutMatches(event, template.shortcut)) || null;
}

function shortcutMatches(event, shortcut) {
  const parts = String(shortcut || "").split("+").map(part => part.trim().toLowerCase()).filter(Boolean);
  if (!parts.length) return false;
  const key = parts[parts.length - 1];
  const wantsMod = parts.includes("mod");
  const wantsAlt = parts.includes("alt");
  const wantsShift = parts.includes("shift");
  if (wantsMod !== Boolean(event.metaKey || event.ctrlKey)) return false;
  if (wantsAlt !== Boolean(event.altKey)) return false;
  if (wantsShift !== Boolean(event.shiftKey)) return false;
  const eventKey = String(event.key || "").toLowerCase();
  const eventCode = String(event.code || "").toLowerCase();
  const normalizedKey = key.toLowerCase();
  return eventKey === normalizedKey || eventCode === `digit${normalizedKey}` || eventCode === `key${normalizedKey}`;
}

function visibleAnnotationsForCurrentFile() {
  const fileName = currentEditorFileName();
  const activeStatuses = activeAnnotationStatuses();
  return (s.longdoc.annotations || [])
    .filter(item => item?.file_name === fileName && activeStatuses.has(String(item.status || "")))
    .sort((a, b) => (Number(a.line_start || 0) - Number(b.line_start || 0)) || Number(a.id || 0) - Number(b.id || 0));
}

function scheduleAnnotationRailLayout() {
  if (_annotationRailFrame) return;
  _annotationRailFrame = requestAnimationFrame(() => {
    _annotationRailFrame = 0;
    positionAnnotationRailCards();
  });
}

function ensureAnnotationRailScrollBinding() {
  const scroller = cm.getScrollContainer?.();
  if (!scroller) return;
  if (scroller === _annotationRailScrollEl) return;
  _annotationRailScrollEl?.removeEventListener?.("scroll", scheduleAnnotationRailLayout);
  scroller.addEventListener("scroll", scheduleAnnotationRailLayout, { passive: true });
  _annotationRailScrollEl = scroller;
  if (!_annotationRailScrollBound) window.addEventListener("resize", scheduleAnnotationRailLayout);
  _annotationRailScrollBound = true;
}

function positionAnnotationRailCards() {
  if (!annotationRailEl || !s.longdoc.annotationRailOpen) return;
  const list = annotationRailEl.querySelector(".annotation-rail-list");
  const head = annotationRailEl.querySelector(".annotation-rail-head");
  if (!list) return;
  const headHeight = head?.getBoundingClientRect?.().height || 0;
  const listHeight = list.clientHeight || 0;
  const safeVisibleTop = 24;
  const cards = [];
  for (const card of list.querySelectorAll(".annotation-rail-card[data-line-start]")) {
    const line = Number(card.getAttribute("data-line-start") || 1);
    const anchorTop = cm.getLineTop?.(line);
    if (anchorTop === null || anchorTop === undefined) {
      card.style.display = "none";
      continue;
    }
    card.style.display = "";
    card.style.visibility = "hidden";
    card.style.top = "0px";
    const height = card.offsetHeight || 0;
    let desiredTop = Math.round(anchorTop - headHeight + 8);
    if (anchorTop >= 0 && desiredTop < safeVisibleTop) {
      desiredTop = safeVisibleTop;
    }
    if (listHeight && (desiredTop + height < -24 || desiredTop > listHeight + 24)) {
      card.style.display = "none";
      continue;
    }
    cards.push({ card, height, desiredTop });
  }
  cards.sort((a, b) => a.desiredTop - b.desiredTop);
  let nextTop = Number.NEGATIVE_INFINITY;
  for (const item of cards) {
    const top = Math.max(item.desiredTop, nextTop);
    if (listHeight && (top + item.height < -24 || top > listHeight + 24)) {
      item.card.style.display = "none";
      continue;
    }
    item.card.style.top = `${top}px`;
    item.card.style.visibility = "";
    nextTop = top + item.height + 10;
  }
}

export function toggleAnnotationRail(force = null) {
  const next = force === null ? !s.longdoc.annotationRailOpen : Boolean(force);
  s.longdoc.annotationRailOpen = next;
  renderAnnotationRail();
  _refreshAnnotationMarkers?.();
}

export function renderAnnotationRail() {
  const open = Boolean(s.longdoc.annotationRailOpen);
  editorWrapEl?.classList.toggle("annotation-rail-open", open);
  annotationRailToggleBtn?.classList.toggle("active", open);
  if (!annotationRailEl) return;
  if (!open) {
    annotationRailEl.innerHTML = "";
    return;
  }
  const fileName = currentEditorFileName();
  const items = visibleAnnotationsForCurrentFile();
  annotationRailEl.innerHTML = `
    <div class="annotation-rail-head">
      <div>
        <strong>Помітки в тексті</strong>
        <span>${escHtml(fileName || "Файл не відкрито")} · Ctrl/Cmd+Shift+A</span>
      </div>
      <span>${items.length}</span>
    </div>
    <div class="annotation-rail-list">
      ${items.length ? items.map(item => `
        <article class="annotation-rail-card ${isAiDraftAnnotation(item) ? "ai-draft" : ""}" data-annotation-rail-id="${escHtml(String(item.id))}" data-annotation-id="${escHtml(String(item.id))}" data-line-start="${escHtml(String(item.line_start || 1))}">
          <div class="annotation-rail-meta">
            ${chip(item.status || "open")}
            ${isAiDraftAnnotation(item) ? `
              <span class="annotation-rail-actions" aria-label="Дії з AI-поміткою">
                <span class="annotation-rail-line">${escHtml(String(item.line_start || 1))}${item.line_end && item.line_end !== item.line_start ? `-${escHtml(String(item.line_end))}` : ""}</span>
                <button class="annotation-rail-action keep" type="button" data-action="keep-ai-annotation" title="Залишити як звичайну помітку">
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8.4 6.4 12 13 4"/></svg>
                </button>
                <button class="annotation-rail-action dismiss" type="button" data-action="dismiss-annotation" title="Відхилити AI-помітку">
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" aria-hidden="true"><path d="M4.5 4.5 11.5 11.5M11.5 4.5 4.5 11.5"/></svg>
                </button>
              </span>
            ` : `<span>${escHtml(String(item.line_start || 1))}${item.line_end && item.line_end !== item.line_start ? `-${escHtml(String(item.line_end))}` : ""}</span>`}
          </div>
          <div class="annotation-rail-text">${escHtml(item.instruction || "")}</div>
        </article>
      `).join("") : `<div class="annotation-rail-empty">У цьому файлі немає відкритих поміток. Виділіть текст або натисніть ПКМ, щоб додати.</div>`}
    </div>
  `;
  ensureAnnotationRailScrollBinding();
  scheduleAnnotationRailLayout();
  annotationRailEl.querySelectorAll("[data-annotation-rail-id]").forEach(card => {
    card.addEventListener("click", () => {
      const id = Number(card.getAttribute("data-annotation-rail-id") || 0);
      const item = (s.longdoc.annotations || []).find(row => Number(row.id) === id);
      if (item?.line_start) cm.jumpToLine?.(Number(item.line_start) || 1);
      annotationRailEl.querySelectorAll(".annotation-rail-card").forEach(el => el.classList.remove("active"));
      card.classList.add("active");
    });
  });
  annotationRailEl.querySelectorAll("[data-action]").forEach(button => {
    button.addEventListener("click", async event => {
      event.preventDefault();
      event.stopPropagation();
      const action = button.getAttribute("data-action");
      const actions = button.closest(".annotation-rail-actions");
      const annotationId = button.closest("[data-annotation-id]")?.getAttribute("data-annotation-id");
      if (!annotationId) return;
      actions?.classList.add("loading");
      actions?.querySelectorAll("button").forEach(actionButton => { actionButton.disabled = true; });
      button.classList.add("loading");
      try {
        await updateAnnotationStatus(annotationId, action === "keep-ai-annotation" ? "open" : "dismissed");
      } catch (err) {
        window.alert(err.message || String(err));
        button.classList.remove("loading");
        actions?.classList.remove("loading");
        actions?.querySelectorAll("button").forEach(actionButton => { actionButton.disabled = false; });
      }
    });
  });
}

export function openAssistantSettings() {
  switchAssistantTab("settings");
}

export function openAnnotationsPanel(annotationId = null) {
  switchAssistantTab("annotations");
  if (!annotationId) return;
  requestAnimationFrame(() => {
    const card = document.querySelector(`[data-annotation-id="${String(annotationId)}"]`);
    card?.scrollIntoView({ block: "center", behavior: "smooth" });
  });
}

export async function createAnnotationFromEditorSelection(instruction, { taskId = null, openPanel = false } = {}) {
  const draft = activeAnnotationDraft();
  if (!draft) throw new Error("Відкрийте текстовий файл, щоб створити помітку.");
  await createAnnotationFromTarget(draft, instruction, { taskId, openPanel });
}

export async function createAnnotationFromTarget(target, instruction, { taskId = null, openPanel = false } = {}) {
  const draft = target || null;
  if (!draft) throw new Error("Не вдалося визначити місце для помітки.");
  if (!longdocEnabled() || !featureEnabled("annotations_enabled")) {
    throw new Error("Помітки вимкнені для цього проєкту.");
  }
  const normalizedInstruction = String(instruction || "").trim();
  if (!normalizedInstruction) {
    throw new Error("Інструкція для помітки порожня.");
  }
  await api(`/api/projects/${cfg.projectId}/annotations/`, {
    method: "POST",
    body: JSON.stringify({
      file_name: draft.fileName,
      line_start: draft.lineStart,
      line_end: draft.lineEnd,
      selected_text: draft.selectedText,
      instruction: normalizedInstruction,
      task_id: taskId,
    }),
  });
  await loadLongdocData();
  if (openPanel) switchAssistantTab("annotations");
  renderAnnotationRail();
}

export async function createQuickAnnotation(template, { openRail = true } = {}) {
  if (!template?.instruction) return false;
  await createAnnotationFromEditorSelection(template.instruction, { openPanel: false });
  if (openRail) toggleAnnotationRail(true);
  return true;
}

export async function handleQuickAnnotationShortcut(event) {
  if (!longdocEnabled() || !featureEnabled("annotations_enabled")) return false;
  const template = matchingQuickAnnotationTemplate(event);
  if (!template) return false;
  event.preventDefault();
  await createQuickAnnotation(template);
  return true;
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

async function openSourceFile(filename, line = null) {
  const name = String(filename || "").trim();
  if (!name) return;
  const files = await import("./files.js");
  const existing = s.projectFiles.find(file => file.name === name) || { name, is_text: true, is_dir: false, type: "asset" };
  await files.selectFile(existing);
  if (line) requestAnimationFrame(() => cm.jumpToLine?.(Number(line) || 1));
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
  applyProjectEditLock();
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

async function loadAnnotations() {
  if (!featureEnabled("annotations_enabled")) {
    s.longdoc.annotations = [];
    return;
  }
  const payload = await api(`/api/projects/${cfg.projectId}/annotations/`, { method: "GET" });
  s.longdoc.annotations = payload.annotations || [];
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
  const previousSession = s.longdoc.activeSession;
  try {
    const payload = await api(`/api/projects/${cfg.projectId}/change-proposals/status/`, { method: "GET" });
    s.longdoc.activeSession = payload.proposal || null;
  } catch {
    s.longdoc.activeSession = null;
  }
  renderSessionBanner();
  syncSessionModalState(previousSession, s.longdoc.activeSession);
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
  _showPanelLoading(annotationsPanelEl);
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
    loadAnnotations(),
    loadNotes(),
    loadRequirements(),
  ]);
  s.longdoc.overview = overview;
  renderLongdocPanels();
  _refreshAnnotationMarkers?.();
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

function readQuickAnnotationTemplateSettings() {
  return [...(settingsPanelEl?.querySelectorAll("[data-quick-template-row]") || [])].map((row, index) => ({
    id: `quick-${index + 1}`,
    label: row.querySelector('[data-field="label"]')?.value || "",
    shortcut: row.querySelector('[data-field="shortcut"]')?.value || "",
    instruction: row.querySelector('[data-field="instruction"]')?.value || "",
    enabled: Boolean(row.querySelector('[data-field="enabled"]')?.checked),
  })).filter(item => item.label.trim() && item.instruction.trim());
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
      ["annotations_enabled", "Помітки", "Прив’язані до місця інструкції для точкових правок."],
      ["notes_enabled", "Нотатки", "Структурований блокнот проєкту."],
    ]],
    ["Автоматизація", [
      ["ai_sessions_enabled", "Запропоновані зміни", "Підготовка змін у контрольованому перегляді."],
      ["mcp_controlled_access", "Контрольований MCP-доступ", "Обмежити MCP доступом через контрольовані інструменти."],
      ["mcp_write_context", "MCP може писати контекст", "Дозволити MCP створювати та оновлювати контекст."],
    ]],
    ["Навігаційний індекс", [
      ["nav_index_enrich_enabled", "Збагачення індексу", "Мала модель додає короткі описи, стани та пошукові тригери для файлів і секцій."],
      ["nav_rerank_enabled", "Rerank цілей", "Мала модель переупорядковує кандидати read/edit targets для запиту."],
      ["nav_repair_enabled", "Repair guidance", "Мала модель пояснює помилки валідації та підказує точні виправлення."],
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
  const ghAppConnected = Boolean(gh.app_connected);
  const ghIntervalMinutes = Number(gh.sync_interval_minutes) || 30;
  const quickTemplates = Array.isArray(settings.quick_annotation_templates) ? settings.quick_annotation_templates : [];

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
        <h3>Швидкі помітки</h3>
        <div class="e-longdoc-meta" style="margin-bottom:8px">Режим карток поряд з текстом: <kbd>Ctrl/Cmd+Shift+A</kbd>. Шаблони нижче працюють у ПКМ меню та через задані shortcuts.</div>
        <div class="e-settings-list quick-annotation-settings">
          ${quickTemplates.map((item, index) => `
            <div class="quick-annotation-row" data-quick-template-row>
              <label class="e-setting-row compact">
                <span>
                  <strong>Увімкнено</strong>
                  <small>${escHtml(item.shortcut || "Без shortcut")}</small>
                </span>
                <input type="checkbox" data-field="enabled" ${item.enabled !== false ? "checked" : ""}>
              </label>
              <input class="e-longdoc-input" data-field="label" value="${escHtml(item.label || "")}" placeholder="Назва">
              <input class="e-longdoc-input" data-field="shortcut" value="${escHtml(item.shortcut || "")}" placeholder="Alt+1">
              <textarea class="e-longdoc-textarea small" data-field="instruction" placeholder="Текст помітки">${escHtml(item.instruction || "")}</textarea>
              <button class="e-sec-btn danger" type="button" data-action="remove-quick-template" data-index="${index}">Видалити</button>
            </div>
          `).join("")}
        </div>
        <div class="e-longdoc-actions" style="margin-top:8px">${button("Додати шаблон", "add-quick-template")}</div>
      </section>

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
          <div class="e-form-field" style="margin-bottom:8px">
            <span><strong>GitHub App: </strong>${ghAppConnected ? '<span style="color:var(--green,#22c55e)">підключено</span>' : '<span style="color:var(--muted,#888)">не підключено</span>'}</span>
          </div>
          ${ghAppConnected
            ? `<div class="e-longdoc-actions" style="margin-bottom:10px; gap:8px">${button("Відключити GitHub App", "disconnect-github", "")}</div>`
            : `<div class="e-longdoc-actions" style="margin-bottom:10px; gap:8px">${button("Підключити GitHub App", "connect-github", "primary")}</div>`
          }
          <div class="e-form-field">
            <label for="gh-repo-url">Repository URL</label>
            <input type="text" id="gh-repo-url" placeholder="https://github.com/user/repo" value="${escHtml(ghRepoUrl)}">
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
      body.quick_annotation_templates = readQuickAnnotationTemplateSettings();
      await api(`/api/projects/${cfg.projectId}/longdoc/settings/`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      await refreshProjectMeta();
      await loadLongdocData();
    },
    "add-quick-template": async () => {
      const next = readQuickAnnotationTemplateSettings();
      next.push({
        id: `custom-${Date.now()}`,
        label: "Нова помітка",
        instruction: "Опиши, що треба виправити в цьому фрагменті.",
        shortcut: "",
        enabled: true,
      });
      s.longdoc.settings.quick_annotation_templates = next;
      renderSettingsPanel();
    },
    "remove-quick-template": async (buttonEl) => {
      const index = Number(buttonEl?.dataset?.index || -1);
      const next = readQuickAnnotationTemplateSettings().filter((_, idx) => idx !== index);
      s.longdoc.settings.quick_annotation_templates = next;
      renderSettingsPanel();
    },
    "connect-github": async () => {
      const statusEl = settingsPanelEl.querySelector("#gh-status");
      try {
        const data = await api("/api/github/install-url/");
        window.open(data.url, "_blank", "noopener");
        if (statusEl) statusEl.textContent = "Відкрито GitHub App — після встановлення оновіть сторінку.";
      } catch (err) {
        if (statusEl) statusEl.textContent = `Помилка: ${err.message}`;
      }
    },
    "disconnect-github": async () => {
      const statusEl = settingsPanelEl.querySelector("#gh-status");
      if (!confirm("Відключити GitHub App? Синхронізація перестане працювати.")) return;
      try {
        await api("/api/github/disconnect/", { method: "DELETE" });
        await refreshProjectMeta();
        renderSettingsPanel();
      } catch (err) {
        if (statusEl) statusEl.textContent = `Помилка: ${err.message}`;
      }
    },
    "save-github": async () => {
      const statusEl = settingsPanelEl.querySelector("#gh-status");
      const repoUrl = settingsPanelEl.querySelector("#gh-repo-url")?.value.trim() || "";
      const syncEnabled = Boolean(settingsPanelEl.querySelector("#gh-sync-enabled")?.checked);
      const intervalMinutes = parseInt(settingsPanelEl.querySelector("#gh-interval")?.value || "30", 10);
      const body = {
        github_repo_url: repoUrl,
        github_sync_enabled: syncEnabled,
        github_sync_interval_minutes: Number.isFinite(intervalMinutes) ? intervalMinutes : 30,
      };
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
  const annotationCounts = overview.annotation_counts || {};
  const coverageCounts = overview.requirement_coverage_counts || {};
  const openTasks = Number(taskCounts.open || 0) + Number(taskCounts.in_progress || 0);
  const aiDraftAnnotations = Number(annotationCounts.ai_draft || 0);
  const openAnnotations = aiDraftAnnotations + Number(annotationCounts.open || 0) + Number(annotationCounts.in_progress || 0);
  const issueReqs = Number(coverageCounts.unchecked || 0) + Number(coverageCounts.partial || 0) + Number(coverageCounts.missing || 0);
  const session = isSessionVisibleInUi(overview.active_proposal)
    ? overview.active_proposal
    : isSessionVisibleInUi(s.longdoc.activeSession)
      ? s.longdoc.activeSession
      : null;

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
        <button class="e-overview-tile ${openAnnotations ? "attention" : ""}" type="button" data-action="go-annotations">
          <strong>${openAnnotations}</strong>
          <span>${aiDraftAnnotations ? `${aiDraftAnnotations} AI на ревʼю` : "активних поміток"}</span>
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
          ${button("Помітки", "go-annotations")}
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
    "go-annotations": async () => switchAssistantTab("annotations"),
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

function annotationTaskOptions(selectedTaskId = null) {
  const tasks = s.longdoc.tasks || [];
  const normalized = selectedTaskId == null ? "" : String(selectedTaskId);
  return `
    <option value="">Без завдання</option>
    ${tasks.map(task => `<option value="${task.id}" ${String(task.id) === normalized ? "selected" : ""}>${escHtml(task.description || `Task #${task.id}`)}</option>`).join("")}
  `;
}

function activeAnnotationDraft() {
  const fileName = String(s.activeTabName || s.selectedFile?.name || "");
  if (!fileName || !s.selectedFile?.is_text || s.selectedFile?.is_dir) return null;
  const selection = getActiveSelectionDetails?.();
  return {
    fileName,
    lineStart: selection?.lineStart || 1,
    lineEnd: selection?.lineEnd || selection?.lineStart || 1,
    selectedText: selection?.selectedText || "",
  };
}

function renderAnnotationsPanel() {
  if (!annotationsPanelEl) return;
  if (!longdocEnabled()) return renderDisabledPanel(annotationsPanelEl, "помітки");
  if (!featureEnabled("annotations_enabled")) return renderFeatureOffPanel(annotationsPanelEl, "Помітки");

  const items = s.longdoc.annotations || [];
  const draft = activeAnnotationDraft();
  const aiDraftItems = items.filter(isAiDraftAnnotation);
  const columns = [
    ["open", "Відкриті"],
    ["in_progress", "У роботі"],
    ["done", "Готові"],
    ["dismissed", "Відхилені"],
  ];
  const aiReviewCard = item => `
    <article class="e-task-card ai-review-card" data-annotation-id="${item.id}" tabindex="0">
      <div class="ai-review-card-top">
        <span class="e-longdoc-chip ai_draft">AI на перевірці</span>
        <span class="e-longdoc-meta">#${escHtml(String(item.id))}</span>
      </div>
      <div class="e-task-line ai-review-line">
        <span class="e-check-dot ai_draft"></span>
        <span>${escHtml(item.instruction || "")}</span>
      </div>
      <div class="e-longdoc-meta">${escHtml(item.file_name || "")}${item.line_start ? `:${escHtml(String(item.line_start))}${item.line_end && item.line_end !== item.line_start ? `-${escHtml(String(item.line_end))}` : ""}` : ""}${item.task_id ? ` · task #${escHtml(String(item.task_id))}` : ""}</div>
      ${item.selected_text ? renderTextBlock(item.selected_text, "") : ""}
      <div class="ai-review-actions">
        <button class="e-sec-btn primary" type="button" data-action="keep-ai-annotation" title="K">
          <span>Залишити</span>
          <kbd>K</kbd>
        </button>
        <button class="e-sec-btn" type="button" data-action="dismiss-annotation" title="D">
          <span>Відхилити</span>
          <kbd>D</kbd>
        </button>
        ${button("Відкрити", "open-annotation-file")}
        ${button("Редагувати", "edit-annotation")}
      </div>
    </article>
  `;
  const annotationCard = item => `
    <article class="e-task-card" data-annotation-id="${item.id}">
      ${isEditing("annotation", item.id) ? `
      <input class="e-longdoc-input" data-field="file_name" value="${escHtml(item.file_name || "")}" placeholder="main.typ">
      <div class="e-longdoc-grid">
        <input class="e-longdoc-input" data-field="line_start" type="number" min="1" value="${item.line_start || ""}" placeholder="Від рядка">
        <input class="e-longdoc-input" data-field="line_end" type="number" min="1" value="${item.line_end || ""}" placeholder="До рядка">
      </div>
      <select class="e-longdoc-input" data-field="task_id">${annotationTaskOptions(item.task_id)}</select>
      <select class="e-longdoc-input" data-field="status">
        <option value="ai_draft" ${item.status === "ai_draft" ? "selected" : ""}>AI на перевірці</option>
        <option value="open" ${item.status === "open" ? "selected" : ""}>Відкрита</option>
        <option value="in_progress" ${item.status === "in_progress" ? "selected" : ""}>У роботі</option>
        <option value="done" ${item.status === "done" ? "selected" : ""}>Готово</option>
        <option value="dismissed" ${item.status === "dismissed" ? "selected" : ""}>Відхилено</option>
      </select>
      <textarea class="e-longdoc-textarea small" data-field="instruction" placeholder="Що треба змінити">${escHtml(item.instruction || "")}</textarea>
      <textarea class="e-longdoc-textarea small" data-field="selected_text" placeholder="Фрагмент тексту">${escHtml(item.selected_text || "")}</textarea>
      <div class="e-longdoc-actions">
        ${button("Зберегти", "save-annotation", "primary")}
        ${button("Скасувати", "cancel-annotation-edit")}
      </div>
      ` : `
        <div class="e-task-line">
          <span class="e-check-dot ${item.status === "done" ? "done" : ""}"></span>
          <span>${escHtml(item.instruction || "")}</span>
        </div>
        <div class="e-longdoc-meta">${escHtml(item.file_name || "")}${item.line_start ? `:${escHtml(String(item.line_start))}${item.line_end && item.line_end !== item.line_start ? `-${escHtml(String(item.line_end))}` : ""}` : ""}${item.task_id ? ` · task #${escHtml(String(item.task_id))}` : ""}</div>
        ${item.selected_text ? renderTextBlock(item.selected_text, "") : ""}
        <div class="e-longdoc-actions">
          ${item.status !== "done" ? button("Готово", "complete-annotation") : ""}
          ${item.status !== "dismissed" ? button("Відхилити", "dismiss-annotation") : ""}
          ${button("Редагувати", "edit-annotation")}
          ${button("Видалити", "delete-annotation", "danger")}
        </div>
      `}
    </article>
  `;
  const createBody = draft
    ? `
      <div class="e-longdoc-meta">Файл: ${escHtml(draft.fileName)} · Рядки ${escHtml(String(draft.lineStart))}${draft.lineEnd && draft.lineEnd !== draft.lineStart ? `-${escHtml(String(draft.lineEnd))}` : ""}</div>
      ${draft.selectedText ? renderTextBlock(draft.selectedText, "") : `<div class="e-longdoc-muted">Текст не виділено. Помітка буде прив’язана до поточного рядка.</div>`}
      <textarea id="new-annotation-instruction" class="e-longdoc-textarea small" placeholder="Що треба змінити в цьому місці"></textarea>
      <select id="new-annotation-task-id" class="e-longdoc-input">${annotationTaskOptions()}</select>
      <div class="e-longdoc-actions">${button("Додати", "create-annotation", "primary")}</div>
    `
    : `<div class="e-longdoc-muted">Відкрийте текстовий файл у редакторі, щоб створити помітку з поточного місця.</div>`;
  annotationsPanelEl.innerHTML = `
    <div class="e-workspace-head">
      <div>
        <h2>Помітки</h2>
        <p>Локальні інструкції, прив’язані до файла та рядків. AI-кандидати спершу проходять коротке ревʼю.</p>
      </div>
      <span class="e-longdoc-meta">${items.length} поміток · ${aiDraftItems.length} на ревʼю</span>
    </div>
    <div class="e-longdoc-scroll">
      ${aiDraftItems.length ? `
        <section class="ai-review-queue" aria-label="AI анотації на перевірці">
          <div class="ai-review-head">
            <div>
              <strong>AI на перевірці</strong>
              <span>Натисніть <kbd>K</kbd>, щоб залишити як звичайну помітку, або <kbd>D</kbd>, щоб відхилити.</span>
            </div>
            <span class="e-longdoc-meta">${aiDraftItems.length}</span>
          </div>
          <div class="ai-review-list">${aiDraftItems.map(aiReviewCard).join("")}</div>
        </section>
      ` : ""}
      ${renderCreatePanel("annotation", "помітку", createBody)}
      <div class="e-task-board">
        ${columns.map(([status, label]) => {
          const columnItems = items.filter(item => (item.status || "open") === status);
          return `
            <section class="e-task-column">
              <div class="e-task-column-head">${escHtml(label)} · ${columnItems.length}</div>
              ${columnItems.map(annotationCard).join("") || emptyCard("Порожньо", "У цій колонці немає поміток.")}
            </section>
          `;
        }).join("")}
      </div>
    </div>
  `;

  bindActions(annotationsPanelEl, {
    "show-create-annotation": async () => { setCreating("annotation", true); renderAnnotationsPanel(); },
    "hide-create-annotation": async () => { setCreating("annotation", false); renderAnnotationsPanel(); },
    "create-annotation": async () => {
      const currentDraft = activeAnnotationDraft();
      if (!currentDraft) throw new Error("Відкрийте текстовий файл, щоб створити помітку.");
      await api(`/api/projects/${cfg.projectId}/annotations/`, {
        method: "POST",
        body: JSON.stringify({
          file_name: currentDraft.fileName,
          line_start: currentDraft.lineStart,
          line_end: currentDraft.lineEnd,
          selected_text: currentDraft.selectedText,
          instruction: document.getElementById("new-annotation-instruction")?.value || "",
          task_id: document.getElementById("new-annotation-task-id")?.value || null,
        }),
      });
      setCreating("annotation", false);
      await loadLongdocData();
    },
    "edit-annotation": async (buttonEl) => {
      const annotationId = buttonEl.closest("[data-annotation-id]")?.dataset.annotationId;
      if (annotationId) setEditing("annotation", annotationId, true);
      renderAnnotationsPanel();
    },
    "cancel-annotation-edit": async (buttonEl) => {
      const annotationId = buttonEl.closest("[data-annotation-id]")?.dataset.annotationId;
      if (annotationId) setEditing("annotation", annotationId, false);
      renderAnnotationsPanel();
    },
    "save-annotation": async (buttonEl) => {
      const card = buttonEl.closest("[data-annotation-id]");
      const annotationId = card?.dataset.annotationId;
      if (!annotationId) return;
      await api(`/api/projects/${cfg.projectId}/annotations/${annotationId}/`, {
        method: "PATCH",
        body: JSON.stringify({
          file_name: card.querySelector('[data-field="file_name"]')?.value || "",
          line_start: card.querySelector('[data-field="line_start"]')?.value || null,
          line_end: card.querySelector('[data-field="line_end"]')?.value || null,
          selected_text: card.querySelector('[data-field="selected_text"]')?.value || "",
          instruction: card.querySelector('[data-field="instruction"]')?.value || "",
          status: card.querySelector('[data-field="status"]')?.value || "open",
          task_id: card.querySelector('[data-field="task_id"]')?.value || null,
        }),
      });
      setEditing("annotation", annotationId, false);
      await loadLongdocData();
    },
    "keep-ai-annotation": async (buttonEl) => {
      const card = buttonEl.closest("[data-annotation-id]");
      const annotationId = card?.dataset.annotationId;
      await updateAnnotationStatus(annotationId, "open");
    },
    "complete-annotation": async (buttonEl) => {
      const card = buttonEl.closest("[data-annotation-id]");
      const annotationId = card?.dataset.annotationId;
      await updateAnnotationStatus(annotationId, "done");
    },
    "dismiss-annotation": async (buttonEl) => {
      const card = buttonEl.closest("[data-annotation-id]");
      const annotationId = card?.dataset.annotationId;
      await updateAnnotationStatus(annotationId, "dismissed");
    },
    "open-annotation-file": async (buttonEl) => {
      const card = buttonEl.closest("[data-annotation-id]");
      const annotationId = Number(card?.dataset.annotationId || 0);
      const item = (s.longdoc.annotations || []).find(row => Number(row.id) === annotationId);
      await openSourceFile(item?.file_name || "", item?.line_start || 1);
    },
    "delete-annotation": async (buttonEl) => {
      const card = buttonEl.closest("[data-annotation-id]");
      const annotationId = card?.dataset.annotationId;
      if (!annotationId || !(await showConfirm("Видалити помітку?"))) return;
      await api(`/api/projects/${cfg.projectId}/annotations/${annotationId}/`, {
        method: "DELETE",
        body: JSON.stringify({}),
      });
      await loadLongdocData();
    },
  });
  annotationsPanelEl.onkeydown = event => {
    if (event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;
    const key = String(event.key || "").toLowerCase();
    if (key !== "k" && key !== "d") return;
    const card = event.target instanceof Element ? event.target.closest(".ai-review-card[data-annotation-id]") : null;
    const annotationId = card?.dataset.annotationId;
    if (!annotationId) return;
    event.preventDefault();
    updateAnnotationStatus(annotationId, key === "k" ? "open" : "dismissed").catch(err => {
      window.alert(err.message || String(err));
    });
  };
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
  renderAnnotationsPanel();
  renderNotesPanel();
  renderRequirementsPanel();
}

// ── Suggested change UI ───────────────────────────────────────────────────────

const sessionBannerEl = document.getElementById("session-banner");
const sessionBannerGoalEl = document.getElementById("session-banner-goal");
const editorWrapEl = document.getElementById("editor-wrap");
const pdfTabbarEl = document.getElementById("pdf-tabbar");
const sessionDiffOverlayEl = document.getElementById("session-diff-overlay");
const sessionDiffContentEl = document.getElementById("session-diff-content");
const sessionDiffSubtitleEl = document.getElementById("session-diff-subtitle");
const sessionDiffFooterTitleEl = document.getElementById("session-diff-footer-title");
const sessionDiffFooterMetaEl = document.getElementById("session-diff-footer-meta");
const sessionDiffHeaderAcceptBtn = document.getElementById("session-diff-accept-btn");
const sessionDiffHeaderDiscardBtn = document.getElementById("session-diff-discard-btn");
const sessionDiffFooterAcceptBtn = document.getElementById("session-diff-footer-accept-btn");
const sessionDiffFooterDiscardBtn = document.getElementById("session-diff-footer-discard-btn");
const sessionDiffDetailsBtn = document.getElementById("session-diff-details-btn");
const sessionDiffFooterDetailsBtn = document.getElementById("session-diff-footer-details-btn");
let diffContextMenuEl = null;

function activeSessionSignature(session) {
  if (!session || !session.id) return "";
  return `${session.id}:${session.updated_at || ""}:${session.status || ""}`;
}

function sessionReviewUrl() {
  return cfg.sessionReviewUrl || `/projects/${cfg.projectId}/session/`;
}

function sessionStatusLabel(status) {
  return ({
    validating: "Готується",
    failed_validation: "Потребує уваги",
    failed_compile: "Не компілюється",
    ready_for_review: "Готово до перегляду",
  })[status] || status || "";
}

function isSessionVisibleInUi(session) {
  return Boolean(session && ["failed_validation", "failed_compile", "ready_for_review"].includes(session.status));
}

function isProjectLockedForEditing() {
  return Boolean(cfg.sessionReview || s.longdoc.settings?.locked || s.projectMeta?.longdoc?.locked || s.projectMeta?.local_workspace?.active);
}

function hasActiveLocalWorkspace() {
  return Boolean(s.projectMeta?.local_workspace?.active);
}

function hasHiddenLockingSession() {
  const sessionId = s.projectMeta?.longdoc?.locking_session_id;
  const proposalId = s.projectMeta?.longdoc?.locking_proposal_id;
  return Boolean(sessionId && !proposalId && !isSessionVisibleInUi(s.longdoc.activeSession));
}

function readonlyReasonText() {
  if (cfg.sessionReview) return "Перегляд зміни доступний тільки для читання";
  if (hasActiveLocalWorkspace()) {
    const agent = s.projectMeta?.local_workspace?.agent_id || "локальному редакторі";
    return `Проєкт зараз редагується у ${agent}. Натисніть “Редагувати у вебі”, щоб зупинити локальний lock і продовжити тут`;
  }
  const session = s.longdoc.activeSession;
  if (session && !isSessionVisibleInUi(session)) {
    return "AI готує запропоновану зміну. Редагування тимчасово заблоковано";
  }
  if (hasHiddenLockingSession()) {
    return "AI-сесія зависла або ще готується. Можна скасувати її, щоб розблокувати редактор";
  }
  if (session) {
    return "Запропонована зміна активна. Прийміть або відхиліть її, щоб редагувати";
  }
  return "Редактор доступний тільки для читання, доки запропонована зміна активна";
}

export function applyProjectEditLock() {
  const locked = isProjectLockedForEditing();
  if (editorWrapEl) editorWrapEl.classList.toggle("project-locked", locked);
  if (readonlyOverlayEl) {
    readonlyOverlayEl.setAttribute("aria-hidden", locked ? "false" : "true");
    readonlyOverlayEl.classList.toggle("can-discard-lock", Boolean(locked && hasHiddenLockingSession()));
    readonlyOverlayEl.classList.toggle("can-use-web", Boolean(locked && hasActiveLocalWorkspace()));
    const label = readonlyOverlayEl.querySelector("[data-readonly-label]");
    if (label) label.textContent = readonlyReasonText();
  }
  cm.setReadOnly?.(locked);
}

async function releaseLocalWorkspaceForWeb() {
  if (!hasActiveLocalWorkspace()) return;
  readonlyUseWebBtn?.setAttribute("disabled", "disabled");
  try {
    const workspaceId = s.projectMeta?.local_workspace?.workspace_id || "";
    await api(`/api/projects/${cfg.projectId}/local-workspace/`, {
      method: "DELETE",
      body: JSON.stringify({ workspace_id: workspaceId }),
    });
    await refreshProjectMeta();
    applyProjectEditLock();
  } finally {
    readonlyUseWebBtn?.removeAttribute("disabled");
  }
}

function isSessionAcceptable(session) {
  return Boolean(session && session.status === "ready_for_review");
}

function updateSessionModalActions(session) {
  const canAccept = isSessionAcceptable(session);
  [sessionDiffHeaderAcceptBtn, sessionDiffFooterAcceptBtn].forEach(btn => {
    if (!btn) return;
    btn.disabled = !canAccept;
    btn.title = canAccept ? "" : "Зміну можна прийняти після підготовки diff і успішної перевірки.";
    btn.style.display = cfg.sessionReview || session ? "" : "none";
  });
  [sessionDiffHeaderDiscardBtn, sessionDiffFooterDiscardBtn].forEach(btn => {
    if (!btn) return;
    btn.disabled = !session;
    btn.style.display = session ? "" : "none";
  });
  [sessionDiffDetailsBtn, sessionDiffFooterDetailsBtn].forEach(link => {
    if (!link) return;
    link.href = sessionReviewUrl();
    link.style.display = cfg.sessionReview ? "none" : "";
  });
}

function syncSessionModalState(previousSession, nextSession) {
  const previousSig = activeSessionSignature(previousSession);
  const nextSig = activeSessionSignature(nextSession);
  updateSessionModalActions(nextSession);

  if (!nextSession || !isSessionVisibleInUi(nextSession)) {
    closeSessionDiffModal();
    s.longdoc.proposalModalDiffSignature = "";
    return;
  }

  const isNewSignature = Boolean(nextSig && nextSig !== previousSig);
  const isUnseen = Boolean(nextSig && s.longdoc.proposalModalSeenSignature !== nextSig);
  if (!cfg.sessionReview && isUnseen && isNewSignature) {
    s.longdoc.proposalModalSeenSignature = nextSig;
    openSessionDiffModal({ forceReload: true, autoOpened: true }).catch(() => {});
    return;
  }

  if (sessionDiffOverlayEl?.classList.contains("open") && nextSig && nextSig !== s.longdoc.proposalModalDiffSignature) {
    openSessionDiffModal({ forceReload: true }).catch(() => {});
  }
}

export function renderSessionBanner() {
  const session = s.longdoc.activeSession;
  const isActive = isSessionVisibleInUi(session);

  if (sessionBannerEl) sessionBannerEl.classList.toggle("visible", Boolean(isActive));
  applyProjectEditLock();
  if (pdfTabbarEl) pdfTabbarEl.classList.toggle("visible", Boolean(isActive && cfg.sessionReview));

  if (!isActive) {
    if (cfg.sessionReview) {
      window.location.href = `/projects/${cfg.projectId}/`;
    }
    return;
  }

  if (sessionBannerGoalEl) sessionBannerGoalEl.textContent = session.goal || "";
  const statusEl = document.getElementById("session-banner-status");
  if (statusEl) statusEl.textContent = sessionStatusLabel(session.status);
}

function diffAnnotationKey(fileName, side, lineNumber) {
  return `${String(fileName || "")}::${String(side || "new")}::${Number(lineNumber || 0)}`;
}

function renderDiffAnnotationCards(items = []) {
  const visible = items.filter(item => String(item.status || "open") === "open");
  if (!visible.length) return "";
  return `
    <div class="diff-row-annotations">
      ${visible.map(item => `
        <article class="diff-row-annotation" data-diff-annotation-id="${escHtml(String(item.id))}">
          <div class="diff-row-annotation-head">
            <span>${escHtml(item.created_by === "mcp" ? "AI" : "Ви")}</span>
            <span>${escHtml(item.side || "new")}:${escHtml(String(item.line_number || ""))}</span>
          </div>
          <div class="diff-row-annotation-text">${escHtml(item.instruction || "")}</div>
          ${item.selected_text ? `<div class="diff-row-annotation-fragment">${escHtml(truncateDiffFragment(item.selected_text, 160))}</div>` : ""}
          <div class="diff-row-annotation-actions">
            <button type="button" data-action="resolve-diff-annotation" data-id="${escHtml(String(item.id))}">Закрити</button>
            <button type="button" data-action="dismiss-diff-annotation" data-id="${escHtml(String(item.id))}">Відхилити</button>
          </div>
        </article>
      `).join("")}
    </div>
  `;
}

function resolvedAnnotationKey(fileName, lineNumber) {
  return `${String(fileName || "")}::${Number(lineNumber || 0)}`;
}

function renderResolvedAnnotationChips(items = []) {
  if (!items.length) return "";
  return `
    <div class="diff-row-resolved-annotations">
      ${items.map(item => `
        <article class="diff-row-resolved-annotation" data-resolved-annotation-id="${escHtml(String(item.id))}">
          <div class="diff-row-resolved-annotation-head">
            <span class="diff-row-resolved-badge">Закриває помітку</span>
            <span>#${escHtml(String(item.id))}</span>
          </div>
          <div class="diff-row-resolved-annotation-text">${escHtml(item.instruction || "")}</div>
          ${item.selected_text ? `<div class="diff-row-resolved-fragment">${escHtml(truncateDiffFragment(item.selected_text, 170))}</div>` : ""}
        </article>
      `).join("")}
    </div>
  `;
}

function renderDetachedResolvedAnnotations(items = []) {
  if (!items.length) return "";
  return `
    <div class="diff-detached-resolved">
      <div class="diff-detached-resolved-head">
        <strong>Закриті помітки без точного рядка в цьому diff</strong>
        <span>${items.length}</span>
      </div>
      ${items.map(item => `
        <div class="diff-detached-resolved-item">
          <span>${escHtml(item.file_name || "")}${item.line_start ? `:${escHtml(String(item.line_start))}` : ""}</span>
          <strong>${escHtml(item.instruction || "")}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function ensureDiffContextMenu() {
  if (diffContextMenuEl) return diffContextMenuEl;
  diffContextMenuEl = document.createElement("div");
  diffContextMenuEl.id = "session-diff-context-menu";
  diffContextMenuEl.className = "e-menu e-context-menu session-diff-context-menu";
  diffContextMenuEl.setAttribute("role", "menu");
  document.body.appendChild(diffContextMenuEl);
  return diffContextMenuEl;
}

function closeDiffContextMenu() {
  if (!diffContextMenuEl) return;
  diffContextMenuEl.classList.remove("open");
  diffContextMenuEl.innerHTML = "";
  diffContextMenuEl.style.left = "";
  diffContextMenuEl.style.top = "";
}

function closeManualDiffEditors() {
  sessionDiffContentEl?.querySelectorAll(".diff-manual-edit").forEach(el => el.remove());
}

function getDiffSelectionForRow(rowEl) {
  const selection = window.getSelection?.();
  const selectedText = String(selection?.toString?.() || "").trim();
  if (!selection || selection.rangeCount === 0 || !selectedText) return null;
  const range = selection.getRangeAt(0);
  const codeEl = rowEl.querySelector(".diff-code");
  if (!codeEl) return null;
  const startEl = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer : range.startContainer.parentElement;
  const endEl = range.endContainer.nodeType === Node.ELEMENT_NODE ? range.endContainer : range.endContainer.parentElement;
  const startRow = startEl?.closest?.(".diff-row[data-file][data-line]");
  const endRow = endEl?.closest?.(".diff-row[data-file][data-line]");
  const startCode = startEl?.closest?.(".diff-code");
  const endCode = endEl?.closest?.(".diff-code");
  if (startRow !== rowEl || endRow !== rowEl || startCode !== codeEl || endCode !== codeEl) return null;
  if (selectedText.includes("\n")) return null;
  return {
    selectedText,
    rect: range.getBoundingClientRect(),
  };
}

function getDiffRowAnnotationTarget(rowEl) {
  const codeEl = rowEl?.querySelector?.(".diff-code");
  const selected = rowEl ? getDiffSelectionForRow(rowEl) : null;
  const rowText = decodeURIComponent(String(codeEl?.dataset?.rawTextEncoded || ""));
  const selectedText = selected?.selectedText || rowText;
  return {
    fileName: String(rowEl?.dataset?.file || "").trim(),
    side: String(rowEl?.dataset?.side || "new").trim(),
    lineNumber: Number(rowEl?.dataset?.line || 0),
    selectedText,
    hasSelection: Boolean(selected?.selectedText),
    rect: selected?.rect || rowEl?.getBoundingClientRect?.() || null,
  };
}

function openManualDiffEditor(rowEl, target) {
  if (!rowEl || !target?.fileName || !target?.lineNumber || target.side !== "new") return;
  closeManualDiffEditors();
  const wrap = rowEl.closest(".diff-row-wrap");
  const currentText = decodeURIComponent(String(rowEl.querySelector(".diff-code")?.dataset?.rawTextEncoded || ""));
  const editor = document.createElement("div");
  editor.className = "diff-manual-edit";
  editor.innerHTML = `
    <div class="diff-manual-edit-card">
      <div class="diff-manual-edit-head">
        <span>Ручна правка пропозала</span>
        <span class="diff-manual-edit-target">${escHtml(target.fileName)}:${escHtml(String(target.lineNumber))}</span>
      </div>
      ${target.hasSelection ? `<div class="diff-manual-edit-selection">Виділено: ${escHtml(truncateDiffFragment(target.selectedText, 180))}</div>` : ""}
      <textarea spellcheck="false" data-manual-diff-editor>${escHtml(currentText)}</textarea>
      <div class="diff-manual-edit-actions">
        <button type="button" data-action="cancel-manual-diff-edit">Скасувати</button>
        <button type="button" data-action="save-manual-diff-edit">Зберегти рядок</button>
      </div>
    </div>
  `;
  wrap?.insertBefore(editor, wrap.querySelector(".diff-row-annotations") || wrap.querySelector(".diff-row-resolved-annotations") || null);
  const textarea = editor.querySelector("textarea");
  textarea?.focus();
  textarea?.setSelectionRange?.(0, textarea.value.length);
  editor.querySelector('[data-action="cancel-manual-diff-edit"]')?.addEventListener("click", () => editor.remove());
  editor.querySelector('[data-action="save-manual-diff-edit"]')?.addEventListener("click", async e => {
    const btn = e.currentTarget;
    const newText = String(textarea?.value || "");
    if (newText.includes("\n")) {
      window.alert("Швидке ручне редагування підтримує один рядок. Для багаторядкової зміни краще оновити proposal.");
      return;
    }
    btn.disabled = true;
    btn.textContent = "Зберігаю...";
    try {
      await api(`/api/projects/${cfg.projectId}/change-proposals/manual-edit/`, {
        method: "POST",
        body: JSON.stringify({
          file_name: target.fileName,
          line_number: target.lineNumber,
          expected_text: currentText,
          new_text: newText,
        }),
      });
      s.longdoc.proposalModalDiffSignature = "";
      await loadLongdocData();
      await openSessionDiffModal({ forceReload: true });
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Зберегти рядок";
      window.alert(`Не вдалося зберегти ручну правку: ${err.message}`);
    }
  });
}

function openDiffContextMenu(rowEl, event) {
  const target = getDiffRowAnnotationTarget(rowEl);
  if (!target.fileName || !target.lineNumber) return;
  event.preventDefault();
  event.stopPropagation();
  const menu = ensureDiffContextMenu();
  menu.innerHTML = "";
  const annotationBtn = document.createElement("button");
  annotationBtn.type = "button";
  annotationBtn.className = "e-menu-item";
  annotationBtn.innerHTML = `
    <span class="e-menu-item-icon" aria-hidden="true">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3.5h10v7H7l-3 2z"/><path d="M5.5 6.2h5"/><path d="M5.5 8.4h3.5"/></svg>
    </span>
    <span class="e-menu-item-label">${target.hasSelection ? "Додати помітку до виділення" : "Додати помітку до рядка"}</span>
    <span class="e-menu-item-shortcut"></span>
  `;
  annotationBtn.addEventListener("click", async () => {
    closeDiffContextMenu();
    await createDiffAnnotationFromTarget(target, event);
  });
  menu.appendChild(annotationBtn);
  if (target.side === "new") {
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "e-menu-item";
    editBtn.innerHTML = `
      <span class="e-menu-item-icon" aria-hidden="true">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9.8 3.2l3 3L6 13H3v-3z"/><path d="M8.5 4.5l3 3"/></svg>
      </span>
      <span class="e-menu-item-label">${target.hasSelection ? "Редагувати рядок з виділенням" : "Редагувати рядок"}</span>
      <span class="e-menu-item-shortcut"></span>
    `;
    editBtn.addEventListener("click", () => {
      closeDiffContextMenu();
      openManualDiffEditor(rowEl, target);
    });
    menu.appendChild(editBtn);
  }
  menu.classList.add("open");
  const margin = 8;
  const rect = menu.getBoundingClientRect();
  const x = Math.min(event.clientX, window.innerWidth - rect.width - margin);
  const y = Math.min(event.clientY, window.innerHeight - rect.height - margin);
  menu.style.left = `${Math.max(margin, x)}px`;
  menu.style.top = `${Math.max(margin, y)}px`;
}

function renderDiffContent(diffText, diffAnnotations = [], resolvedAnnotations = []) {
  if (!diffText) return `<span class="diff-empty">Змін не знайдено.</span>`;

  const lines = diffText.split("\n");
  let totalAdded = 0, totalRemoved = 0;
  const rows = [];
  let leftLine = 0, rightLine = 0;
  let currentOldFile = "";
  let currentNewFile = "";
  const pendingRemoved = [];
  const pendingAdded = [];
  const annotationsByRow = new Map();
  const resolvedByRow = new Map();
  const attachedResolvedIds = new Set();
  for (const item of diffAnnotations || []) {
    if (!item || String(item.status || "open") !== "open") continue;
    const key = diffAnnotationKey(item.file_name, item.side, item.line_number);
    const bucket = annotationsByRow.get(key) || [];
    bucket.push(item);
    annotationsByRow.set(key, bucket);
  }
  for (const item of resolvedAnnotations || []) {
    const lineStart = Number(item?.line_start || 0);
    if (!item?.file_name || !lineStart) continue;
    const key = resolvedAnnotationKey(item.file_name, lineStart);
    const bucket = resolvedByRow.get(key) || [];
    bucket.push(item);
    resolvedByRow.set(key, bucket);
  }

  function buildInlineDiffPair(beforeText, afterText) {
    const before = String(beforeText || "");
    const after = String(afterText || "");
    const maxPrefix = Math.min(before.length, after.length);
    let prefixLen = 0;
    while (prefixLen < maxPrefix && before[prefixLen] === after[prefixLen]) prefixLen += 1;

    let beforeEnd = before.length - 1;
    let afterEnd = after.length - 1;
    while (beforeEnd >= prefixLen && afterEnd >= prefixLen && before[beforeEnd] === after[afterEnd]) {
      beforeEnd -= 1;
      afterEnd -= 1;
    }

    const sharedPrefix = before.slice(0, prefixLen);
    const sharedSuffix = before.slice(beforeEnd + 1);
    return {
      beforeHtml:
        `${escHtml(sharedPrefix)}<span class="diff-inline-del">${escHtml(before.slice(prefixLen, beforeEnd + 1)) || " "}</span>${escHtml(sharedSuffix)}`,
      afterHtml:
        `${escHtml(sharedPrefix)}<span class="diff-inline-add">${escHtml(after.slice(prefixLen, afterEnd + 1)) || " "}</span>${escHtml(sharedSuffix)}`,
    };
  }

  function renderDiffRow(kind, left, right, codeHtml, sign, opts = {}) {
    const side = opts.side || (kind === "del" ? "old" : kind === "add" ? "new" : "context");
    const fileName = opts.fileName || (side === "old" ? currentOldFile : currentNewFile);
    const lineNumber = Number(opts.lineNumber || (side === "old" ? left : right) || 0);
    const rowAnnotations = annotationsByRow.get(diffAnnotationKey(fileName, side, lineNumber)) || [];
    const resolvedItems = (resolvedByRow.get(resolvedAnnotationKey(fileName, lineNumber)) || [])
      .filter(item => !attachedResolvedIds.has(Number(item.id)));
    for (const item of resolvedItems) attachedResolvedIds.add(Number(item.id));
    return `
      <div class="diff-row-wrap">
        <div class="diff-row ${kind}" data-file="${escHtml(fileName)}" data-side="${escHtml(side)}" data-line="${escHtml(String(lineNumber || ""))}">
          <span class="diff-sign">${sign}</span>
          <span class="diff-ln">${left || ""}</span>
          <span class="diff-ln">${right || ""}</span>
          <span class="diff-code" data-raw-text-encoded="${encodeURIComponent(opts.rawText || "")}">${codeHtml}</span>
        </div>
        ${renderResolvedAnnotationChips(resolvedItems)}
        ${renderDiffAnnotationCards(rowAnnotations)}
      </div>
    `;
  }

  function flushPendingChanges() {
    if (!pendingRemoved.length && !pendingAdded.length) return;
    const pairs = Math.max(pendingRemoved.length, pendingAdded.length);
    for (let i = 0; i < pairs; i += 1) {
      const removed = pendingRemoved[i] || null;
      const added = pendingAdded[i] || null;
      if (removed && added) {
        const inline = buildInlineDiffPair(removed.text, added.text);
        rows.push(renderDiffRow("del", removed.line, "", inline.beforeHtml, "-", { side: "old", fileName: currentOldFile, lineNumber: removed.line, rawText: removed.text }));
        rows.push(renderDiffRow("add", "", added.line, inline.afterHtml, "+", { side: "new", fileName: currentNewFile, lineNumber: added.line, rawText: added.text }));
      } else if (removed) {
        rows.push(renderDiffRow("del", removed.line, "", escHtml(removed.text), "-", { side: "old", fileName: currentOldFile, lineNumber: removed.line, rawText: removed.text }));
      } else if (added) {
        rows.push(renderDiffRow("add", "", added.line, escHtml(added.text), "+", { side: "new", fileName: currentNewFile, lineNumber: added.line, rawText: added.text }));
      }
    }
    pendingRemoved.length = 0;
    pendingAdded.length = 0;
  }

  for (const raw of lines) {
    if (raw.startsWith("---") || raw.startsWith("+++")) {
      flushPendingChanges();
      if (raw.startsWith("---")) currentOldFile = raw.replace(/^---\s+[ab]\//, "").trim();
      if (raw.startsWith("+++")) currentNewFile = raw.replace(/^\+\+\+\s+[ab]\//, "").trim();
      rows.push(`<div class="diff-meta">${escHtml(raw)}</div>`);
    } else if (raw.startsWith("@@")) {
      flushPendingChanges();
      const m = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) { leftLine = parseInt(m[1], 10) - 1; rightLine = parseInt(m[2], 10) - 1; }
      rows.push(`<div class="diff-hunk">${escHtml(raw)}</div>`);
    } else if (raw.startsWith("+")) {
      rightLine++;
      totalAdded++;
      pendingAdded.push({ line: rightLine, text: raw.slice(1) });
    } else if (raw.startsWith("-")) {
      leftLine++;
      totalRemoved++;
      pendingRemoved.push({ line: leftLine, text: raw.slice(1) });
    } else {
      flushPendingChanges();
      leftLine++;
      rightLine++;
      rows.push(renderDiffRow("ctx-empty", leftLine, rightLine, escHtml(raw.slice(1)), " ", { side: "context", fileName: currentNewFile || currentOldFile, lineNumber: rightLine, rawText: raw.slice(1) }));
    }
  }
  flushPendingChanges();
  const detachedResolved = (resolvedAnnotations || []).filter(item => !attachedResolvedIds.has(Number(item.id)));

  return `
    <div class="diff-summary">
      <span class="diff-chip add">+${totalAdded}</span>
      <span class="diff-chip del">-${totalRemoved}</span>
      <span class="session-diff-summary-text">Темнішим підсвічено точкові зміни всередині рядків. Правий клік додає помітку або редагує зелений рядок.</span>
    </div>
    <div class="diff-table">${rows.join("")}</div>
    ${renderDetachedResolvedAnnotations(detachedResolved)}`;
}

function truncateDiffFragment(text, maxLen = 260) {
  const value = String(text || "").trim();
  if (!value) return "";
  return value.length > maxLen ? `${value.slice(0, maxLen - 1)}…` : value;
}

function renderSemanticDiffSummary(summary) {
  if (!summary || typeof summary !== "object" || !summary.title) return "";
  const impact = String(summary.impact || "low");
  const items = Array.isArray(summary.items) ? summary.items : [];
  const impactLabel = impact === "high" ? "Високий вплив" : impact === "medium" ? "Середній вплив" : "Локальна зміна";
  return `
    <section class="smcl-review-card semantic ${escHtml(impact)}">
      <div class="smcl-review-head">
        <span class="smcl-review-kicker">Semantic diff summary</span>
        <span class="smcl-review-impact">${escHtml(impactLabel)}</span>
      </div>
      <div class="smcl-review-title">${escHtml(summary.title)}</div>
      ${items.length ? `
        <div class="smcl-summary-grid">
          ${items.map(item => `
            <div class="smcl-summary-item ${escHtml(item.kind || "item")}">
              <strong>${escHtml(item.label || "")}</strong>
              <span>${escHtml(item.detail || "")}</span>
            </div>
          `).join("")}
        </div>
      ` : ""}
    </section>
  `;
}

function renderSmclWarnings(warnings, riskLevel) {
  const items = Array.isArray(warnings) ? warnings : [];
  if (!items.length) return "";
  return `
    <section class="smcl-review-card warnings ${escHtml(riskLevel || "medium")}">
      <div class="smcl-review-head">
        <span class="smcl-review-kicker">Safety review</span>
        <span class="diff-chip del">${escHtml(riskLevel || "medium")}</span>
      </div>
      <div class="smcl-warning-list">
        ${items.map(w => {
          const title = w?.human_title || w?.message || w?.code || "Попередження";
          const detail = w?.human_detail || w?.message || "";
          const samples = Array.isArray(w?.samples) ? w.samples : [];
          const locations = Array.isArray(w?.locations) ? w.locations : [];
          const severity = String(w?.severity || "medium");
          return `
            <article class="smcl-warning-item ${escHtml(severity)}">
              <div class="smcl-warning-icon">${severity === "high" ? "!" : "i"}</div>
              <div class="smcl-warning-body">
                <div class="smcl-warning-title">
                  <strong>${escHtml(title)}</strong>
                  <span>${escHtml(w?.code || "")}</span>
                </div>
                ${detail ? `<p>${escHtml(detail)}</p>` : ""}
                ${locations.length ? `<div class="smcl-warning-tags">${locations.map(item => `<span>${escHtml(item)}</span>`).join("")}</div>` : ""}
                ${samples.length ? `<div class="smcl-warning-samples">${samples.slice(0, 2).map(item => `<code>${escHtml(truncateDiffFragment(item, 180))}</code>`).join("")}</div>` : ""}
              </div>
            </article>
          `;
        }).join("")}
      </div>
    </section>
  `;
}

function renderSmclReviewPanel(data, session) {
  const warnings = data.smcl_warnings || session?.smcl_warnings || [];
  const summary = data.semantic_diff_summary || session?.semantic_diff_summary || data.smcl_metadata?.semantic_diff_summary || {};
  const riskLevel = data.smcl_risk_level || session?.smcl_risk_level || "medium";
  const summaryHtml = renderSemanticDiffSummary(summary);
  const warningsHtml = renderSmclWarnings(warnings, riskLevel);
  if (!summaryHtml && !warningsHtml) return "";
  return `<div class="smcl-review-panel">${summaryHtml}${warningsHtml}</div>`;
}

async function openSessionDiffModal({ forceReload = false } = {}) {
  const overlay = sessionDiffOverlayEl;
  const content = sessionDiffContentEl;
  const subtitle = sessionDiffSubtitleEl;
  const session = s.longdoc.activeSession;
  if (!overlay || !session || !isSessionVisibleInUi(session)) return;

  const signature = activeSessionSignature(session);
  overlay.classList.add("open");
  updateSessionModalActions(session);

  if (subtitle) subtitle.textContent = `Запропонована зміна #${session.id} · ${sessionStatusLabel(session.status)}`;
  if (sessionDiffFooterTitleEl) sessionDiffFooterTitleEl.textContent = session.goal || "Зміни AI-сесії";
  if (sessionDiffFooterMetaEl) {
    sessionDiffFooterMetaEl.textContent = `${sessionStatusLabel(session.status)}${session.updated_at ? ` · ${session.updated_at}` : ""}`;
  }
  if (!forceReload && s.longdoc.proposalModalDiffSignature === signature && content?.innerHTML) return;
  if (content) content.innerHTML = `<span class="diff-empty">Завантаження diff...</span>`;

  try {
    const data = await api(`/api/projects/${cfg.projectId}/change-proposals/diff/`, { method: "GET" });
    const currentSession = s.longdoc.activeSession;
    if (!currentSession || activeSessionSignature(currentSession) !== signature) return;
    s.longdoc.proposalModalDiffSignature = signature;
    const reviewHtml = renderSmclReviewPanel(data, session);
    let diffHtml;
    if (!data.diff_text && data.compile_error_summary) {
      diffHtml = `<pre class="diff-empty diff-compile-error">${escHtml(data.compile_error_summary)}</pre>`;
    } else {
      diffHtml = renderDiffContent(data.diff_text || "", data.diff_annotations || [], data.resolved_annotations || []);
    }
    if (content) content.innerHTML = reviewHtml + diffHtml;

  } catch (err) {
    if (content) content.innerHTML = `<span class="diff-empty">Помилка завантаження diff: ${escHtml(err.message)}</span>`;
  }
}

async function createDiffAnnotationFromTarget(target, event = null) {
  const fileName = String(target?.fileName || "").trim();
  const lineNumber = Number(target?.lineNumber || 0);
  const side = String(target?.side || "new").trim();
  const selectedText = String(target?.selectedText || "");
  if (!fileName || !lineNumber) return;
  const instruction = await showAnnotationPopover({
    title: target?.hasSelection ? "Помітка до виділення в diff" : "Помітка до рядка diff",
    hint: target?.hasSelection
      ? "Опишіть, що треба змінити саме в обраному фрагменті diff."
      : "Опишіть, що треба змінити в цьому рядку запропонованої зміни.",
    target: `${fileName}:${lineNumber} · ${side}`,
    selectedText: selectedText || "Текст рядка порожній.",
    rect: target?.rect,
    x: event?.clientX,
    y: event?.clientY,
  });
  if (!instruction || !instruction.trim()) return;
  await api(`/api/projects/${cfg.projectId}/change-proposals/diff-annotations/`, {
    method: "POST",
    body: JSON.stringify({
      file_name: fileName,
      line_number: lineNumber,
      side,
      selected_text: selectedText,
      instruction: instruction.trim(),
    }),
  });
  s.longdoc.proposalModalDiffSignature = "";
  await openSessionDiffModal({ forceReload: true });
}

async function updateDiffAnnotationStatus(annotationId, status) {
  if (!annotationId) return;
  await api(`/api/projects/${cfg.projectId}/change-proposals/diff-annotations/${annotationId}/`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
  s.longdoc.proposalModalDiffSignature = "";
  await openSessionDiffModal({ forceReload: true });
}

function closeSessionDiffModal() {
  const overlay = sessionDiffOverlayEl;
  if (overlay) overlay.classList.remove("open");
  closeManualDiffEditors();
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
  closeSessionDiffModal()
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
  const hiddenLock = hasHiddenLockingSession();
  const message = hiddenLock
    ? "Скасувати активну AI-сесію і розблокувати редактор? Незбережений proposal від цієї сесії буде відхилено."
    : "Відхилити запропоновану зміну? Дію не можна скасувати.";
  if (!(await showConfirm(message))) return;
  try {
    await api(`/api/projects/${cfg.projectId}/change-proposals/discard/`, { method: "POST" });
    if (cfg.sessionReview) {
      window.location.href = `/projects/${cfg.projectId}/`;
      return;
    }
    s.longdoc.activeSession = null;
    renderSessionBanner();
    closeSessionDiffModal();
    const main = await import("./app.js");
    await main.loadProjectMeta?.();
    await loadLongdocData();
  } catch (err) {
    alert(`Не вдалося відхилити зміни: ${err.message}`);
  }
}

export function initSessionUI() {
  if (cfg.sessionReview) {
    editorWrapEl?.classList.add("project-locked");
  }
  applyProjectEditLock();
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
  document.getElementById("session-diff-footer-accept-btn")?.addEventListener("click", acceptSession);
  document.getElementById("session-diff-footer-discard-btn")?.addEventListener("click", discardSession);
  readonlyDiscardSessionBtn?.addEventListener("click", discardSession);
  readonlyUseWebBtn?.addEventListener("click", releaseLocalWorkspaceForWeb);
  annotationRailToggleBtn?.addEventListener("click", () => toggleAnnotationRail());
  document.getElementById("session-diff-overlay")?.addEventListener("click", e => {
    if (e.target === e.currentTarget) closeSessionDiffModal();
  });
  sessionDiffOverlayEl?.addEventListener("contextmenu", e => {
    const row = e.target instanceof Element ? e.target.closest(".diff-row[data-file][data-line]") : null;
    if (!row || !sessionDiffOverlayEl.contains(row)) return;
    openDiffContextMenu(row, e);
  });
  sessionDiffContentEl?.addEventListener("click", e => {
    const target = e.target instanceof Element ? e.target.closest("[data-action]") : null;
    const action = target?.getAttribute("data-action") || "";
    if (action === "resolve-diff-annotation" || action === "dismiss-diff-annotation") {
      const id = target?.getAttribute("data-id");
      updateDiffAnnotationStatus(id, action === "resolve-diff-annotation" ? "done" : "dismissed")
        .catch(err => window.alert(err.message || String(err)));
    }
  });
  document.addEventListener("mousedown", e => {
    if (!diffContextMenuEl?.classList.contains("open")) return;
    if (diffContextMenuEl.contains(e.target)) return;
    closeDiffContextMenu();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") closeDiffContextMenu();
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
