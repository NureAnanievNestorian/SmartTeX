import * as state from "./state.js";

const { cfg } = state;

export async function api(url, opts = {}) {
  const headers = {
    "X-CSRFToken": cfg.csrfToken,
    "X-Change-Source": "web",
  };
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const r = await fetch(url, {
    credentials: "same-origin",
    ...opts,
    headers: { ...headers, ...(opts.headers || {}) },
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { const d = await r.json(); msg = d.detail || d.error || msg; } catch (_) {}
    throw new Error(msg);
  }
  if (r.status === 204) return {};
  return r.json();
}
