import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as ui from "./ui.js";

const { cfg, s } = state;
const { api } = apiMod;
const { escHtml } = ui;

const searchTabBtn   = document.getElementById("left-tab-search");
const searchPanel    = document.getElementById("tab-search");
const searchInput    = document.getElementById("smart-search-input");
const searchBtn      = document.getElementById("smart-search-btn");
const scopeSelect    = document.getElementById("smart-search-scope");
const includeExtra   = document.getElementById("smart-search-include-extra");
const includeOrphans = document.getElementById("smart-search-include-orphans");
const resultsEl      = document.getElementById("smart-search-results");
const searchStatus   = document.getElementById("smart-search-status");

let _searchInFlight = false;
let _selectFile = null;

export function setSearchSelectFileRef(fn) {
  _selectFile = fn;
}

export function initSearchPanel() {
  syncSearchTabVisibility();
  searchBtn?.addEventListener("click", () => runSearch());
  searchInput?.addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
}

export function syncSearchTabVisibility() {
  const enabled = Boolean(s.projectMeta?.small_model?.enabled);
  if (searchTabBtn) searchTabBtn.style.display = enabled ? "" : "none";
  // If currently showing search panel but feature just got disabled, switch to files.
  if (!enabled && searchPanel?.classList.contains("active")) {
    document.querySelectorAll(".e-tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".e-tabpanel").forEach(p => p.classList.remove("active"));
    document.querySelector('.e-tab[data-tab="files"]')?.classList.add("active");
    document.getElementById("tab-files")?.classList.add("active");
  }
}

async function runSearch() {
  if (_searchInFlight) return;
  const query = (searchInput?.value || "").trim();
  if (!query) {
    if (resultsEl) resultsEl.innerHTML = "";
    if (searchStatus) searchStatus.textContent = "";
    return;
  }
  _searchInFlight = true;
  if (searchBtn) searchBtn.disabled = true;
  if (searchStatus) searchStatus.textContent = "Пошук…";
  if (resultsEl) resultsEl.innerHTML = "";
  try {
    const body = {
      query,
      scope: scopeSelect?.value || "reachable_document",
      include_extra: Boolean(includeExtra?.checked),
      include_orphans: Boolean(includeOrphans?.checked),
      use_small_model: true,
      max_results: 20,
    };
    const data = await api(`/api/projects/${cfg.projectId}/navigation/search/`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderResults(data);
  } catch (err) {
    if (searchStatus) searchStatus.textContent = `Помилка: ${err.message}`;
  } finally {
    _searchInFlight = false;
    if (searchBtn) searchBtn.disabled = false;
  }
}

const MATCH_KIND_LABELS = {
  exact_match:       "Точний збіг",
  semantic_match:    "Семантичний збіг",
  related_context:   "Суміжний контекст",
  possible_conflict: "Можливий конфлікт",
  placeholder_or_demo: "Заповнювач / демо",
  old_topic_residue: "Стара тема",
  citation_or_source: "Джерело / бібліографія",
  diagram_reference: "Діаграма",
  definition:        "Визначення",
};

const MATCH_KIND_CLASS = {
  exact_match:       "sk-exact",
  semantic_match:    "sk-semantic",
  related_context:   "sk-related",
  possible_conflict: "sk-conflict",
  placeholder_or_demo: "sk-demo",
  old_topic_residue: "sk-old",
  citation_or_source: "sk-cite",
  diagram_reference: "sk-diagram",
  definition:        "sk-def",
};

const CONF_CLASS = { high: "sc-high", medium: "sc-medium", low: "sc-low" };

function renderResults(data) {
  if (!resultsEl) return;
  const results = data?.results || [];
  const warnings = data?.warnings || [];
  const mode = data?.mode || "";

  let statusText = "";
  if (results.length === 0) {
    statusText = "Нічого не знайдено.";
  } else {
    const modeLabel = mode === "smart_reranked" ? " · AI rerank" : "";
    statusText = `${results.length} результат(ів)${modeLabel} · ${data.latency_ms ?? "?"}ms`;
  }
  if (warnings.includes("query_all_generic_terms")) {
    statusText += " · запит містить лише загальні слова";
  }
  if (searchStatus) searchStatus.textContent = statusText;

  if (results.length === 0) {
    resultsEl.innerHTML = "<div class='ss-empty'>Немає результатів для цього запиту.</div>";
    return;
  }

  resultsEl.innerHTML = results.map(r => {
    const mkClass = MATCH_KIND_CLASS[r.match_kind] || "";
    const mkLabel = MATCH_KIND_LABELS[r.match_kind] || r.match_kind || "";
    const confClass = CONF_CLASS[r.confidence] || "";
    const filename = escHtml(r.filename || "");
    const regionTitle = r.region_title ? ` › ${escHtml(r.region_title)}` : "";
    const lineRange = r.line_start ? `${r.line_start}–${r.line_end}` : "";
    const snippet = r.snippet ? `<pre class="ss-snippet">${escHtml(r.snippet)}</pre>` : "";
    const reason = r.reason ? `<div class="ss-reason">${escHtml(r.reason)}</div>` : "";
    return `<div class="ss-result" data-file="${filename}" data-line="${r.line_start || 1}">
  <div class="ss-result-head">
    <span class="ss-filename">${filename}${regionTitle}</span>
    ${lineRange ? `<span class="ss-line">:${lineRange}</span>` : ""}
  </div>
  <div class="ss-tags">
    <span class="ss-mk ${mkClass}">${escHtml(mkLabel)}</span>
    <span class="ss-conf ${confClass}">${escHtml(r.confidence || "")}</span>
  </div>
  ${reason}
  ${snippet}
</div>`;
  }).join("");

  resultsEl.querySelectorAll(".ss-result").forEach(el => {
    el.addEventListener("click", () => {
      const filename = el.dataset.file;
      const line = parseInt(el.dataset.line || "1", 10) || 1;
      if (!filename || !_selectFile) return;
      const fileObj = s.projectFiles.find(f => f.name === filename);
      if (fileObj) {
        _selectFile(fileObj, line);
      }
    });
  });
}
