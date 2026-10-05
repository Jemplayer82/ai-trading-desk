// TradingAgents Web — shared frontend utilities.
//
// This file is loaded FIRST in index.html, before auth.js / app.js / portfolio.js
// / spy.js / credentials.js. Those are plain classic <script defer> tags, NOT ES
// modules, so every top-level `const`/`function` declared here lives in one shared
// global lexical scope that the other files can see directly (no import needed).
//
// Two consequences worth knowing before you edit:
//   1. Load order matters. `defer` scripts run in document order, so utils.js must
//      stay the first /static/*.js tag — otherwise these globals won't exist yet
//      when the other modules run their DOMContentLoaded handlers.
//   2. The other modules must NOT redeclare these names at top level. A second
//      top-level `const $` in the same shared scope throws
//      "Identifier '$' has already been declared". (That global-scope collision is
//      historically why the modules used divergent aliases like `$$p` / `$$spy`.)
//
// Shared globals defined here: $, escapeHtml, renderMarkdown, fmtTs, apiFetch,
// stopFieldVisibility, stopSummary, progressBar, setupTabs (the whole dashboard's tab strip — every tier needs it,
// so it lives here rather than in any one tab's file).
//
// Escape-at-render discipline (project-wide): every interpolation of server- or
// LLM-sourced data into an HTML string must pass through escapeHtml(); LLM-authored
// markdown is rendered ONLY via renderMarkdown() (marked + DOMPurify). Assigning
// unescaped data to innerHTML is the known-bad pattern these helpers exist to stop.

/** Shorthand for document.getElementById. */
const $ = (id) => document.getElementById(id);

/**
 * Escape a string for safe insertion into HTML text or attribute contexts.
 * Handles the five HTML-significant characters (& < > " '); escaping the quotes
 * as well as the angle brackets makes the result safe inside attribute values,
 * not just text nodes. Returns "" for null/undefined.
 */
function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Render markdown to sanitized HTML for assignment to innerHTML.
 *
 * Reports and Q&A answers are authored by the LLM, not the end user, but we still
 * run the marked.js output through DOMPurify so a model that emits raw HTML (e.g.
 * an <img onerror=...> tag) can't inject script into the page. Falls back to an
 * escaped <pre> block if marked.js failed to load from the CDN.
 */
function renderMarkdown(md) {
  if (window.marked) {
    const html = window.marked.parse(md);
    return window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
  }
  return `<pre>${escapeHtml(md)}</pre>`;
}

/**
 * Format an ISO timestamp as "YYYY-MM-DD HH:MM" in the browser's local time.
 * Returns "—" for empty input and echoes the raw value back if it can't be parsed.
 */
function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Fetch JSON from a same-origin API endpoint, throwing on a non-2xx status so the
 * caller's catch block can surface the error. Use this for the common
 * "GET → JSON, report failures via catch" pattern; call fetch() directly when an
 * endpoint needs bespoke handling (e.g. reading resp.text() on error, or treating
 * an application-level {error: ...} field in a 200 response).
 */
async function apiFetch(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    // Surface the server's reason (FastAPI puts it in `detail`) so a popup says
    // "An options account named 'Bull' already exists" rather than "HTTP 409".
    let detail = "";
    try {
      const body = await resp.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch (e) { /* non-JSON error body */ }
    throw new Error(detail ? detail + " (HTTP " + resp.status + ")" : "HTTP " + resp.status);
  }
  return resp.json();
}

/**
 * Show/hide the stop-policy inputs of an account form whose field ids share
 * `prefix` ("new-acct" or "opt-new"): the value input is needed by every
 * non-"none" type, the limit-offset input only by "stop_limit". Also relabels
 * the value input for percent vs dollar types.
 */
function stopFieldVisibility(prefix) {
  const type = (document.getElementById(prefix + "-stop-type") || {}).value || "none";
  const valueWrap = document.getElementById(prefix + "-stop-value-wrap");
  const offsetWrap = document.getElementById(prefix + "-stop-offset-wrap");
  const valueLabel = document.getElementById(prefix + "-stop-value-label");
  if (valueWrap) { valueWrap.hidden = (type === "none"); valueWrap.style.display = (type === "none") ? "none" : ""; }
  if (offsetWrap) { offsetWrap.hidden = (type !== "stop_limit"); offsetWrap.style.display = (type !== "stop_limit") ? "none" : ""; }
  for (const field of ["stage-trigger", "stage-trail"]) {
    const wrap = document.getElementById(prefix + "-" + field + "-wrap");
    if (wrap) { wrap.hidden = type !== "trailing_staged"; wrap.style.display = wrap.hidden ? "none" : ""; }
  }
  const stageHelp = document.getElementById(prefix + "-stage-help");
  if (stageHelp) { stageHelp.hidden = type !== "trailing_staged"; }
  if (type === "trailing_staged") {
    for (const [field, value] of [["stop-value", 20], ["stage-trigger", 20], ["stage-trail", 10]]) {
      const input = document.getElementById(prefix + "-" + field);
      if (input && input.value === "") input.value = value;
    }
  }
  updateStagedStopExplanation(prefix);
  if (valueLabel) {
    valueLabel.textContent = type === "trailing_dollar" ? "Trail amount ($)"
      : (type === "trailing_pct" || type === "trailing_staged") ? "Trail below peak (%)"
      : "Stop below entry (%)";
  }
}

/** Plain-text staged explanation shared by the form and dashboard. */
function stagedStopSentence(baseValue, triggerValue, trailValue) {
  const values = [baseValue, triggerValue, trailValue];
  const [base, trigger, trail] = values.map(Number);
  if (values.some(v => v == null || String(v).trim() === "") ||
      ![base, trigger, trail].every(Number.isFinite) ||
      base <= 0 || base >= 100 || trigger <= 0 || trail < 1 || trail > 99) {
    return "Enter the three numbers to see how this stop works.";
  }
  const sentence = `Sells if the price falls ${base}% from its highest point. Once the trade is up ${trigger}%, it sells if it falls ${trail}% from its highest point.`;
  return sentence + (trail > base ? " A looser setting never lowers a stop the trade has already reached." : "");
}

function updateStagedStopExplanation(prefix) {
  const explanation = document.getElementById(prefix + "-stage-explanation");
  if (!explanation) return;
  explanation.hidden = document.getElementById(prefix + "-stop-type")?.value !== "trailing_staged";
  explanation.textContent = stagedStopSentence(
    document.getElementById(prefix + "-stop-value")?.value,
    document.getElementById(prefix + "-stage-trigger")?.value,
    document.getElementById(prefix + "-stage-trail")?.value);
}

/**
 * Human-readable summary of an account's stop policy, rendered in both the
 * S&P 500 (equity) and Options paper-account lists. Returns strings that the
 * account-list UI joins with " · ".
 */
function stopSummary(a) {
  const t = a.stop_type || "none";
  if (t === "none") return "no stop";
  const v = a.stop_value;
  if (t === "stop") return `stop ${escapeHtml(v)}%`;
  if (t === "stop_limit") return `stop ${escapeHtml(v)}% / limit ${escapeHtml(a.stop_limit_offset ?? 0)}%`;
  if (t === "trailing_staged") return escapeHtml(stagedStopSentence(v, a.stage_trigger_pct, a.stage_trail_pct));
  if (t === "trailing_pct") return `trail ${escapeHtml(v)}%`;
  if (t === "trailing_dollar") return `trail $${escapeHtml(v)}`;
  return escapeHtml(t);
}

/**
 * Build the track+fill element for a "[ Progress ]" bar. The portfolio scan and the
 * S&P 500 scan both render this exact markup; callers own the surrounding panel and
 * the "N/total" label line (those differ per scan type). Width is the count/total
 * ratio as a whole percent, 0% when total is 0.
 */
function progressBar(count, total) {
  const pct = total > 0 ? Math.round((count / total) * 100) : 0;
  return `<div class="scan-progress"><div class="scan-progress-bar" style="width:${pct}%"></div></div>`;
}

/**
 * Wires the top-level Run Analysis / Portfolio / S&P 500 / Options / Settings
 * tabs and dispatches "tab-shown" (detail = tab name) on every switch. Every
 * module's per-tab refresh hangs off that event. Lives here (not in any one
 * tab's own file) because it's dashboard-wide plumbing every tier needs —
 * a T1-only deployment still has to switch between Run Analysis and Settings.
 */
function setupTabs() {
  document.querySelectorAll(".main-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.tab;
      document.querySelectorAll(".main-tab").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-pane").forEach((p) => {
        const show = p.dataset.pane === name;
        p.hidden = !show;
        p.classList.toggle("active", show);
      });
      document.dispatchEvent(new CustomEvent("tab-shown", { detail: name }));
    });
  });
}

document.addEventListener("DOMContentLoaded", setupTabs);
