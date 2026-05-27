"""MCP Apps UI helpers for SmartTeX.

This module keeps the widget URI, MIME type and tool metadata in one place so the
stdio bridge and any future HTTP MCP server can expose the same interactive UI.

The metadata follows the MCP Apps standard first (`_meta.ui.resourceUri`) and keeps
OpenAI's compatibility alias (`_meta["openai/outputTemplate"]`) for ChatGPT Apps.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"
SMARTTEX_WIDGET_URI = "ui://widget/smarttex-project-overview-v1.html"


def _js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def smarttex_project_widget_html() -> str:
    """Return a self-contained MCP Apps widget.

    It uses only the standard MCP Apps bridge (`ui/*` JSON-RPC over postMessage)
    and feature-detects ChatGPT's optional `window.openai` compatibility layer.
    """
    title = html.escape("SmartTeX Project")
    return f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: dark light;
      --bg: #161719;
      --panel: #1f2023;
      --panel-2: #25272b;
      --border: #3a3d42;
      --text: #e8e8e8;
      --muted: #a1a1aa;
      --green: #22c55e;
      --red: #ef4444;
      --amber: #f59e0b;
      --blue: #60a5fa;
      --shadow: 0 18px 48px rgba(0,0,0,.32);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #f4f6f8;
        --panel: #ffffff;
        --panel-2: #f0f2f5;
        --border: #d8dde5;
        --text: #1f2328;
        --muted: #667085;
        --shadow: 0 18px 48px rgba(15,23,42,.10);
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 14px; background: var(--bg); color: var(--text); }}
    .shell {{ border: 1px solid var(--border); background: var(--panel); border-radius: 14px; overflow: hidden; box-shadow: var(--shadow); }}
    .top {{ display: flex; gap: 12px; align-items: center; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--border); background: linear-gradient(180deg, rgba(255,255,255,.04), transparent); }}
    .brand {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
    .logo {{ width: 26px; height: 26px; border-radius: 8px; display: grid; place-items: center; color: #07160b; background: var(--green); font-weight: 900; }}
    h1 {{ margin: 0; font-size: 15px; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .sub {{ margin-top: 2px; color: var(--muted); font-size: 12px; }}
    .badge {{ display: inline-flex; align-items: center; gap: 6px; padding: 5px 9px; border-radius: 999px; border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); font-size: 12px; font-weight: 700; white-space: nowrap; }}
    .badge.success {{ color: var(--green); border-color: rgba(34,197,94,.35); }}
    .badge.failed {{ color: var(--red); border-color: rgba(239,68,68,.35); }}
    .dot {{ width: 7px; height: 7px; border-radius: 99px; background: currentColor; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; padding: 12px; }}
    .card {{ border: 1px solid var(--border); background: var(--panel-2); border-radius: 12px; padding: 12px; min-height: 84px; }}
    .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; font-weight: 800; }}
    .value {{ margin-top: 8px; font-size: 21px; font-weight: 850; }}
    .value.small {{ font-size: 14px; word-break: break-word; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; padding: 0 12px 12px; }}
    button, a.button {{ border: 1px solid var(--border); color: var(--text); background: var(--panel-2); border-radius: 10px; padding: 9px 11px; font-size: 13px; font-weight: 800; cursor: pointer; text-decoration: none; }}
    button.primary {{ background: var(--green); color: #07160b; border-color: rgba(34,197,94,.55); }}
    button:disabled {{ opacity: .55; cursor: not-allowed; }}
    .split {{ display: grid; grid-template-columns: 1fr 1.1fr; border-top: 1px solid var(--border); }}
    .section {{ min-width: 0; padding: 12px; }}
    .section + .section {{ border-left: 1px solid var(--border); }}
    .list {{ display: flex; flex-direction: column; gap: 7px; margin-top: 10px; max-height: 190px; overflow: auto; }}
    .row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; border: 1px solid var(--border); background: rgba(0,0,0,.08); padding: 8px 9px; border-radius: 9px; }}
    .file {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }}
    .pill {{ font-size: 11px; color: var(--muted); }}
    pre {{ margin: 10px 0 0; white-space: pre-wrap; word-break: break-word; max-height: 190px; overflow: auto; border: 1px solid var(--border); border-radius: 10px; padding: 10px; background: rgba(0,0,0,.14); font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; color: var(--text); }}
    .empty {{ color: var(--muted); font-size: 13px; padding: 14px; }}
    @media (max-width: 720px) {{ .grid, .split {{ grid-template-columns: 1fr; }} .section + .section {{ border-left: 0; border-top: 1px solid var(--border); }} }}
  </style>
</head>
<body>
  <main class="shell">
    <div class="top">
      <div class="brand">
        <div class="logo">T</div>
        <div style="min-width:0">
          <h1 id="project-title">SmartTeX Project</h1>
          <div class="sub" id="project-sub">Очікую дані від MCP tool…</div>
        </div>
      </div>
      <span class="badge" id="status"><span class="dot"></span> idle</span>
    </div>
    <div class="grid">
      <div class="card"><div class="label">Markup</div><div class="value" id="markup">—</div></div>
      <div class="card"><div class="label">Файлів</div><div class="value" id="files-count">—</div></div>
      <div class="card"><div class="label">Main file</div><div class="value small" id="main-file">—</div></div>
    </div>
    <div class="actions">
      <button class="primary" id="compile">Recompile</button>
      <button id="refresh">Refresh</button>
      <a class="button" id="open-pdf" target="_blank" rel="noreferrer" hidden>Open PDF</a>
    </div>
    <div class="split">
      <section class="section">
        <div class="label">Файли</div>
        <div class="list" id="files"><div class="empty">Ще немає списку файлів.</div></div>
      </section>
      <section class="section">
        <div class="label">Compile log</div>
        <pre id="log">Лог зʼявиться після виклику tool.</pre>
      </section>
    </div>
  </main>
  <script>
    const WIDGET_URI = {_js_string(SMARTTEX_WIDGET_URI)};
    const state = {{ projectId: null, lastToolResult: null }};
    const $ = (id) => document.getElementById(id);

    function setStatus(status) {{
      const el = $('status');
      const normalized = (status || 'idle').toLowerCase();
      el.className = 'badge ' + (normalized === 'success' ? 'success' : normalized.includes('fail') || normalized.includes('error') ? 'failed' : '');
      el.innerHTML = '<span class="dot"></span>' + (status || 'idle');
    }}

    function render(payload) {{
      const sc = payload?.structuredContent || payload || {{}};
      const meta = payload?._meta || {{}};
      const files = meta.files || sc.files || [];
      state.projectId = sc.id || sc.project_id || state.projectId;
      state.lastToolResult = payload;
      $('project-title').textContent = sc.title || 'SmartTeX Project';
      $('project-sub').textContent = state.projectId ? `Project #${{state.projectId}} · ${{sc.updated_at || 'synced'}}` : 'Дані отримано';
      $('markup').textContent = (sc.markup_type || '—').toUpperCase();
      $('files-count').textContent = String(sc.files_count ?? files.length ?? '—');
      $('main-file').textContent = sc.main_file_name || sc.main_file || '—';
      setStatus(sc.last_status || sc.status || 'ready');
      const pdf = sc.pdf_url || meta.pdf_url;
      const pdfLink = $('open-pdf');
      if (pdf) {{ pdfLink.hidden = false; pdfLink.href = pdf; }} else {{ pdfLink.hidden = true; }}
      $('files').innerHTML = files.length ? files.slice(0, 30).map((f) => {{
        const name = f.name || f.file_name || String(f);
        const size = f.size ? `${{Math.round(Number(f.size) / 1024)}} KB` : '';
        return `<div class="row"><span class="file" title="${{escapeHtml(name)}}">${{escapeHtml(name)}}</span><span class="pill">${{escapeHtml(size)}}</span></div>`;
      }}).join('') : '<div class="empty">Файли не передані у _meta.</div>';
      $('log').textContent = meta.compile_log || sc.log_excerpt || 'Немає compile log.';
    }}

    function escapeHtml(value) {{
      return String(value).replace(/[&<>'"]/g, (ch) => ({{'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}}[ch]));
    }}

    function postRpc(method, params) {{
      const id = 'smarttex-' + Date.now() + '-' + Math.random().toString(16).slice(2);
      window.parent?.postMessage({{ jsonrpc: '2.0', id, method, params }}, '*');
      return id;
    }}

    async function callTool(name, args) {{
      const openai = window.openai;
      if (openai?.callTool) return openai.callTool(name, args);
      postRpc('tools/call', {{ name, arguments: args }});
    }}

    $('compile').addEventListener('click', async () => {{
      if (!state.projectId) return;
      setStatus('compiling');
      $('compile').disabled = true;
      try {{ await callTool('compile_project', {{ project_id: state.projectId }}); }} finally {{ $('compile').disabled = false; }}
    }});
    $('refresh').addEventListener('click', async () => {{
      if (!state.projectId) return;
      await callTool('show_project_overview', {{ project_id: state.projectId }});
    }});

    window.addEventListener('message', (event) => {{
      if (event.source !== window.parent) return;
      const message = event.data;
      if (!message || message.jsonrpc !== '2.0') return;
      if (message.method === 'ui/notifications/tool-result') render(message.params);
      if (message.method === 'ui/initialize' && message.params?.toolResult) render(message.params.toolResult);
    }}, {{ passive: true }});

    if (window.openai?.toolOutput) render(window.openai.toolOutput);
  </script>
</body>
</html>"""


def project_overview_tool_descriptor() -> dict[str, Any]:
    return {
        "name": "show_project_overview",
        "title": "Show SmartTeX project overview",
        "description": "Render an interactive SmartTeX project card with compile status, files and log.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "SmartTeX project ID"},
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "markup_type": {"type": "string"},
                "main_file_name": {"type": "string"},
                "last_status": {"type": "string"},
                "files_count": {"type": "integer"},
                "pdf_url": {"type": ["string", "null"]},
                "updated_at": {"type": "string"},
            },
            "required": ["id", "title", "markup_type", "main_file_name", "last_status", "files_count", "updated_at"],
            "additionalProperties": True,
        },
        "_meta": {
            "ui": {"resourceUri": SMARTTEX_WIDGET_URI},
            "openai/outputTemplate": SMARTTEX_WIDGET_URI,
            "openai/toolInvocation/invoking": "Loading SmartTeX project…",
            "openai/toolInvocation/invoked": "SmartTeX project loaded.",
        },
    }


def widget_resource_contents(domain: str | None = None) -> dict[str, Any]:
    csp: dict[str, list[str]] = {"connectDomains": [], "resourceDomains": []}
    if domain:
        csp["connectDomains"].append(domain.rstrip("/"))
    return {
        "contents": [
            {
                "uri": SMARTTEX_WIDGET_URI,
                "mimeType": RESOURCE_MIME_TYPE,
                "text": smarttex_project_widget_html(),
                "_meta": {
                    "ui": {
                        "prefersBorder": True,
                        "csp": csp,
                    }
                },
            }
        ]
    }


def build_project_overview_result(project: dict[str, Any], files_payload: dict[str, Any] | None, compile_payload: dict[str, Any] | None, *, base_url: str = "") -> dict[str, Any]:
    files_payload = files_payload or {}
    compile_payload = compile_payload or {}
    files = files_payload.get("files") or files_payload.get("items") or []
    pdf_url = compile_payload.get("pdf_url") or project.get("pdf_url")
    if base_url and isinstance(pdf_url, str) and pdf_url.startswith("/"):
        pdf_url = f"{base_url.rstrip('/')}{pdf_url}"
    structured = {
        "id": int(project.get("id")),
        "title": str(project.get("title") or "Untitled"),
        "markup_type": str(project.get("markup_type") or "latex"),
        "main_file_name": str(project.get("main_file_name") or project.get("main_file") or "main.tex"),
        "last_status": str(compile_payload.get("status") or project.get("last_status") or "unknown"),
        "files_count": len(files),
        "pdf_url": pdf_url,
        "updated_at": str(project.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    }
    log = str(compile_payload.get("log") or "")
    return {
        "structuredContent": structured,
        "content": [
            {
                "type": "text",
                "text": f"SmartTeX project '{structured['title']}' is ready for interactive viewing.",
            }
        ],
        "_meta": {
            "files": files,
            "diagnostics": compile_payload.get("diagnostics") or [],
            "compile_log": log[-12000:],
            "pdf_url": pdf_url,
            "widgetUri": SMARTTEX_WIDGET_URI,
        },
    }
