// TradingAgents Web — "Portfolio Scan" tab + the dashboard's main tab strip.
//
// Two DIFFERENT data sources render into this tab — don't conflate them:
//   * Holding cards (renderHoldingCard) are LIVE brokerage positions from
//     GET /api/accounts (Schwab today, normalized by web/brokerages.py).
//   * Scan-result cards (loadPortfolioScan) are HISTORICAL — frozen at scan time.
// Both render into the same #portfolio-tickers grid, so loading a scan replaces
// the live holdings view and selecting an account tab replaces the scan cards.
//
// Endpoints — routed to the PORTFOLIO app (web/portfolio_main.py) by
// web/nginx.conf's /api/portfolio and /api/accounts prefix locations (note
// /api/portfolio-scan(s) is caught by the /api/portfolio string prefix):
//   GET    /api/portfolio-scans          history list
//   GET    /api/portfolio/status         single cheap row (queue + banner)
//   GET    /api/portfolio-scans/{id}     scan detail (polled at 5s while running)
//   DELETE /api/portfolio-scans/{id}     delete a scan
//   DELETE /api/portfolio-scans          delete ALL scans (Clear history)
//   POST   /api/portfolio-scan           start a scan (idempotent for today)
//   GET    /api/accounts                 live holdings, all brokerage accounts
// GET /api/auth/schwab/status goes to the API app via the generic /api/ block.
//
// Account tabs: /api/accounts returns a synthetic {id: "all"} aggregate first,
// then real accounts with ids namespaced "<provider>:<number>" (e.g.
// "schwab:12345678"). Selection matches by dataset.id — never by label text.
//
// This is the T2 (Schwab) owner of: applySchwabVisibility (called as
// window.applySchwabVisibility by credentials.js when the SCHWAB_ENABLED
// toggle flips — see app.js's header for why that call lives here and not
// there), the Run Analysis tab's cross-type scan queue (loadAnalyzeQueue,
// _openRunningScan) and its scan-activity banner (setupScanActivity /
// pollScanActivity / scanActivity*). These are called from THIS file's own
// DOMContentLoaded, not app.js's, so a T1 deployment (no portfolio.js) never
// references them.
//
// Globals consumed from utils.js (loaded first): $, escapeHtml, fmtTs,
// renderMarkdown, progressBar. Also DEFINES SCAN_TYPE_TAG / scanTypeKey /
// renderScanQueue — the shared scan-queue rendering every tab's sidebar uses
// (this file's own loadAnalyzeQueue, spy.js, options.js) — because the queue
// concept itself only exists once Schwab/portfolio scans do (T2+). All
// top-level names here share the classic-script global scope — don't
// redeclare utils.js names.

let activePortfolioId = null;
let _accountsData = null;
let _activeAccountId = "all";
let portfolioPollTimer = null;

/**
 * Shared scan-queue rendering for every tab's sidebar. The queue is one global
 * FIFO (only one scan runs at a time), served by /api/portfolio/status as
 * { running, queued: [] }. Each item carries scan_type ('portfolio'|'spy') and
 * kind ('equity'|'options'|'research'); scanTypeKey collapses those to a single
 * label key so a tab can filter to just its own runs — or, on Run Analysis, show
 * them all. 'research' is the shared daily research scan (no paper account); the
 * equity/options rows are per-account allocations over it.
 */
const SCAN_TYPE_TAG = { portfolio: "pf", spy: "spy", options: "opt", research: "rsch" };

function scanTypeKey(item) {
  if (!item) return "";
  if (item.scan_type === "portfolio") return "portfolio";
  if (item.scan_type === "spy") {
    if (item.kind === "options") return "options";
    if (item.kind === "research") return "research";
    return "spy";
  }
  return item.scan_type || "";
}

/**
 * Render a queue list into `ul` from a /api/portfolio/status payload.
 *   opts.only  — array of type keys to include (e.g. ["spy"]); omit for all.
 *   opts.onOpen(item) — click handler for the RUNNING item (optional).
 */
function renderScanQueue(ul, data, opts) {
  if (!ul) return;
  opts = opts || {};
  const only = opts.only || null;
  const running = data && data.running ? [data.running] : [];
  const queued = (data && data.queued) || [];
  const waiting = (data && data.waiting) || [];
  let items = [...running, ...waiting, ...queued];
  if (only) items = items.filter((it) => only.includes(scanTypeKey(it)));
  ul.innerHTML = "";
  if (!items.length) {
    ul.innerHTML = '<li class="dim empty">(queue empty)</li>';
    return;
  }
  const runningShown = data && data.running && (!only || only.includes(scanTypeKey(data.running))) ? 1 : 0;
  const waitingShown = waiting.filter((it) => !only || only.includes(scanTypeKey(it))).length;
  items.forEach((item, idx) => {
    const li = document.createElement("li");
    li.dataset.id = item.id;
    const isRunning = data && item === data.running;
    const isWaiting = waiting.includes(item);
    let label, badgeClass;
    if (isRunning) {
      label = "RUNNING";
      badgeClass = "HOLD";
    } else if (isWaiting) {
      label = "WAITING";
      badgeClass = "QUEUED";
    } else {
      label = "#" + (idx - runningShown - waitingShown + 1) + " IN QUEUE";
      badgeClass = "QUEUED";
    }
    const tag = SCAN_TYPE_TAG[scanTypeKey(item)] || "scan";
    li.innerHTML =
      '<span class="h-main">' +
        '<span class="h-top">' +
          '<span class="h-tk">' + tag + " #" + item.id + " · " + escapeHtml(item.trade_date || "") + "</span>" +
          '<span class="h-sig ' + badgeClass + '">' + label + "</span>" +
        "</span>" +
        '<span class="h-ts">' + fmtTs(item.created_at) + "</span>" +
      "</span>";
    if ((isRunning || isWaiting) && opts.onOpen) {
      li.querySelector(".h-main").addEventListener("click", () => opts.onOpen(item));
    }
    ul.appendChild(li);
  });
}

function stopPortfolioPoll() {
  if (portfolioPollTimer) { clearInterval(portfolioPollTimer); portfolioPollTimer = null; }
}

// ---- number formatters (Portfolio-tab specific: currency / shares / percent) ----
function fmt$(v) {
  return "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtAbs$(v) {
  return "$" + Math.abs(Number(v)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtShares(v) {
  return Number(v).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 4 });
}
function fmtPct(v) {
  return (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%";
}
function fmtExpDate(iso) {
  // "2026-01-17" -> "01/17/26" (string split — avoids Date() UTC off-by-one)
  const [y, m, d] = iso.split("-");
  return `${m}/${d}/${y.slice(2)}`;
}

// The "[ Progress ]" panel shown while a portfolio scan runs. Built in one place so
// the initial render and the 5s poll re-render can't drift apart. `progressBar` is
// the shared bar primitive from utils.js.
function portfolioProgressHtml(scan) {
  const sc = scan.scanned_count || 0;
  const st = scan.scan_total || 0;
  const tickerLine = scan.current_ticker
    ? `<div style="margin-bottom:4px;">Analyzing: <strong>${escapeHtml(scan.current_ticker)}</strong></div>`
    : "";
  return (
    '<div class="panel">' +
      '<div class="panel-title">[ Progress ]</div>' +
      `<div style="margin-bottom:4px;">${sc}/${st} tickers analyzed</div>` +
      tickerLine +
      progressBar(sc, st) +
    "</div>"
  );
}

// ---- Scan queue/history sidebar ----

function _setupPortfolioStabs() {
  document.querySelectorAll("[data-stab-target^=\"portfolio-\"]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.stabTarget;
      document.querySelectorAll("[data-stab-target^=\"portfolio-\"]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      [$("portfolio-queue"), $("portfolio-history")].forEach((el) => {
        if (el) el.hidden = el.id !== target;
      });
    });
  });
}

async function loadPortfolioQueue() {
  const ul = $("portfolio-queue");
  if (!ul) return;
  try {
    const r = await fetch("/api/portfolio/status");
    const data = r.ok ? await r.json() : { running: null, queued: [] };
    renderScanQueue(ul, data, { only: ["portfolio"], onOpen: (item) => loadPortfolioScan(item.id) });
  } catch (e) {
    ul.innerHTML = `<li class="empty" style="color:var(--accent-red);">${escapeHtml(String(e))}</li>`;
  }
}

async function loadPortfolioHistory() {
  loadPortfolioQueue();  // keep queue in sync whenever history refreshes
  const ul = $("portfolio-history");
  if (!ul) return;
  ul.innerHTML = '<li class="dim empty">loading…</li>';
  try {
    const r = await fetch("/api/portfolio-scans?status=completed&status=failed");
    const { scans } = await r.json();
    ul.innerHTML = "";
    $("portfolio-history-clear-btn").disabled = !scans.length;
    if (!scans.length) {
      ul.innerHTML = '<li class="dim empty">(no scans yet)</li>';
      return;
    }
    scans.forEach((s) => {
      const counts = s.signal_counts || {};
      const sigStr = `${counts.BUY || 0} · ${counts.HOLD || 0} · ${counts.SELL || 0}`;
      const li = document.createElement("li");
      li.dataset.id = s.id;
      if (String(s.id) === String(activePortfolioId)) li.classList.add("active");
      const statusBadge = s.status === "completed" ? "BUY" : (s.status === "running" ? "HOLD" : "SELL");
      li.innerHTML = `
        <span class="h-main">
          <span class="h-top">
            <span class="h-tk">#${s.id} · ${escapeHtml(s.trade_date)}</span>
            <span class="h-sig ${statusBadge}">${(s.status || "—").toUpperCase()}</span>
          </span>
          <span class="h-ts">${fmtTs(s.created_at)} · ${s.num_tickers || 0} tickers</span>
          <span class="h-ts" style="font-size:10px;">BUY · HOLD · SELL = ${sigStr}</span>
        </span>
        <button class="h-del" title="Delete" aria-label="Delete">×</button>
      `;
      li.querySelector(".h-main").addEventListener("click", () => loadPortfolioScan(s.id));
      li.querySelector(".h-del").addEventListener("click", (ev) => {
        ev.stopPropagation();
        deletePortfolioScan(s.id);
      });
      ul.appendChild(li);
    });
  } catch (e) {
    ul.innerHTML = `<li class="empty" style="color: var(--accent-red);">${escapeHtml(e)}</li>`;
  }
}

async function loadPortfolioScan(id) {
  stopPortfolioPoll();
  activePortfolioId = id;
  document.querySelectorAll("#portfolio-history li").forEach((li) =>
    li.classList.toggle("active", String(li.dataset.id) === String(id))
  );
  const meta = $("portfolio-meta");
  const brief = $("portfolio-briefing");
  const grid = $("portfolio-tickers");
  brief.innerHTML = '<p class="dim">loading…</p>';
  grid.innerHTML = "";
  try {
    const r = await fetch(`/api/portfolio-scans/${id}`);
    if (!r.ok) {
      brief.innerHTML = `<p style="color: var(--accent-red);">Not found (HTTP ${r.status})</p>`;
      return;
    }
    const scan = await r.json();
    const counts = scan.signal_counts || {};
    meta.innerHTML = `
      Scan <strong>#${scan.id}</strong> · ${escapeHtml(scan.trade_date)} · ${fmtTs(scan.created_at)}
      · status: <strong>${scan.status}</strong> · ${scan.num_tickers || 0} tickers
      · ${counts.BUY || 0} BUY · ${counts.HOLD || 0} HOLD · ${counts.SELL || 0} SELL
      ${scan.newsletter_sent_at ? `· newsletter sent ${fmtTs(scan.newsletter_sent_at)}` : ""}
    `;

    // Progress panel — shown only while scan is running; disappears once it stops.
    if (scan.status === "running") {
      brief.innerHTML = portfolioProgressHtml(scan);

      // Poll every 5s while the scan is running; stop when status leaves "running".
      portfolioPollTimer = setInterval(async () => {
        // Guard: if the user switched to a different scan, cancel this stale timer.
        if (String(activePortfolioId) !== String(id)) { stopPortfolioPoll(); return; }
        const pr = await fetch(`/api/portfolio-scans/${id}`);
        if (!pr.ok) { stopPortfolioPoll(); return; }
        const updated = await pr.json();
        if (updated.status !== "running") {
          stopPortfolioPoll();
          loadPortfolioScan(id);
          return;
        }
        // Re-render just the progress panel in-place (avoid full re-render cost).
        brief.innerHTML = portfolioProgressHtml(updated);
      }, 5000);
    } else if (scan.aggregator_report) {
      brief.innerHTML = renderMarkdown(scan.aggregator_report);
    } else if (scan.error) {
      brief.innerHTML = `<p style="color: var(--accent-red);">${escapeHtml(scan.error)}</p>`;
    } else {
      brief.innerHTML = '<p class="dim">No briefing yet.</p>';
    }
    // Scan-result cards: HISTORICAL per-ticker outcomes from this scan (shares /
    // value as of scan time) — not live data. Rendered into the same grid the
    // live holding cards use (see header).
    (scan.tickers || []).forEach((t) => {
      const card = document.createElement("div");
      card.className = "pcard";
      const sig = (t.signal || "UNKNOWN").toUpperCase();
      card.innerHTML = `
        <div class="pcard-row">
          <span class="pcard-tk">${escapeHtml(t.ticker)}</span>
          <span class="badge ${sig}">${sig}</span>
        </div>
        <div class="pcard-divider" style="margin-top:6px;"></div>
        <div class="pcard-metrics">
          <div>
            <div class="pcard-metric-label">Shares</div>
            <div class="pcard-metric-val">${(t.quantity || 0).toFixed(0)}</div>
          </div>
          <div class="pcard-metrics-right">
            <div class="pcard-metric-label">Current worth</div>
            <div class="pcard-metric-val">$${(t.market_value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
          </div>
        </div>
        ${t.error ? `<div class="pcard-err">scan failed: ${escapeHtml(t.error)}</div>` : ""}
        ${t.analysis_id ? `<div class="pcard-link"><a href="#" data-analysis="${t.analysis_id}">Open full analysis →</a></div>` : ""}
      `;
      const link = card.querySelector("[data-analysis]");
      if (link) {
        link.addEventListener("click", (ev) => {
          ev.preventDefault();
          // switch to analyze tab and load the analysis
          window.dispatchEvent(new CustomEvent("load-analysis", { detail: parseInt(link.dataset.analysis, 10) }));
          document.querySelector('.main-tab[data-tab="analyze"]')?.click();
        });
      }
      grid.appendChild(card);
    });
  } catch (e) {
    brief.innerHTML = `<p style="color: var(--accent-red);">${escapeHtml(e)}</p>`;
  }
}

async function deletePortfolioScan(id) {
  if (!confirm(`Delete portfolio scan #${id}?`)) return;
  const r = await fetch(`/api/portfolio-scans/${id}`, { method: "DELETE" });
  if (!r.ok) {
    alert("Delete failed.");
    return;
  }
  if (String(activePortfolioId) === String(id)) {
    stopPortfolioPoll();
    activePortfolioId = null;
    $("portfolio-briefing").innerHTML = '<p class="dim">Select a scan from the sidebar.</p>';
    $("portfolio-tickers").innerHTML = "";
    $("portfolio-meta").textContent = "";
  }
  loadPortfolioHistory();
}

async function clearPortfolioHistory() {
  const { scans } = await (await fetch("/api/portfolio-scans")).json();
  if (!scans.length) return;
  if (!confirm(`Clear all ${scans.length} portfolio scans? This cannot be undone.`)) return;
  const r = await fetch("/api/portfolio-scans", { method: "DELETE" });
  if (!r.ok) {
    alert("Clear failed.");
    return;
  }
  stopPortfolioPoll();
  activePortfolioId = null;
  $("portfolio-briefing").innerHTML = '<p class="dim">Select a scan from the sidebar.</p>';
  $("portfolio-tickers").innerHTML = "";
  $("portfolio-meta").textContent = "";
  loadPortfolioHistory();
}

// Run a Schwab portfolio scan now (moved from the old Schwab tab).
async function runScanNow() {
  const btn = $("btn-scan-now");
  const out = $("scan-now-result");
  if (btn) btn.disabled = true;
  if (out) out.textContent = "starting…";
  const aggressiveness = parseInt($("pf-aggressiveness")?.value || "5", 10);
  const bias = document.querySelector("#pf-bias .bias-btn.active")?.dataset?.val || "neutral";
  try {
    const r = await fetch("/api/portfolio-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ aggressiveness, bias }),
    });
    const data = await r.json();
    if (!r.ok) {
      out.innerHTML = `<span style="color: var(--accent-red);">${data.detail || JSON.stringify(data)}</span>`;
    } else {
      const status = data.status === "queued" ? "queued" : (data.new ? "started" : "already running (idempotent)");
      out.innerHTML = `Scan <strong>#${data.scan_id}</strong> ${status}.`;
      loadPortfolioHistory();
    }
  } catch (e) {
    out.innerHTML = `<span style="color: var(--accent-red);">${escapeHtml(e)}</span>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---- Live account holdings ----
// Everything below renders LIVE brokerage data from GET /api/accounts; hides the
// account tabs + totals panel when brokerages are disabled or disconnected.

async function loadAccountHoldings() {
  const tabsEl = $("account-tabs");
  if (!tabsEl) return;
  try {
    const r = await fetch("/api/accounts");
    const data = await r.json();
    if (!data.enabled || !data.connected || !data.accounts) {
      tabsEl.innerHTML = "";
      const panel = $("portfolio-totals-panel");
      if (panel) panel.hidden = true;
      return;
    }
    _accountsData = data.accounts;
    renderAccountTabs(_accountsData);
    selectAccount(_activeAccountId);
  } catch (e) {
    tabsEl.innerHTML = "";
  }
}

function renderAccountTabs(accounts) {
  const tabsEl = $("account-tabs");
  if (!tabsEl) return;
  tabsEl.innerHTML = "";
  accounts.forEach((acct) => {
    const btn = document.createElement("button");
    btn.className = "account-tab" + (acct.id === _activeAccountId ? " active" : "");
    btn.textContent = acct.label;
    btn.dataset.id = acct.id;
    btn.addEventListener("click", () => selectAccount(acct.id));
    tabsEl.appendChild(btn);
  });
}

function selectAccount(id) {
  _activeAccountId = id;
  if (!_accountsData) return;
  const acct = _accountsData.find((a) => a.id === id) || _accountsData[0];
  if (!acct) return;

  document.querySelectorAll(".account-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.id === acct.id);
  });

  renderTotals(acct);

  const grid = $("portfolio-tickers");
  if (!grid) return;
  grid.innerHTML = "";
  if (!acct.positions.length) {
    grid.innerHTML = '<p class="dim">No positions in this account.</p>';
    return;
  }
  acct.positions.forEach((pos) => grid.appendChild(renderHoldingCard(pos)));
}

function renderTotals(acct) {
  const panel = $("portfolio-totals-panel");
  const el = $("portfolio-totals");
  if (!panel || !el) return;

  const gainCls = acct.gain_dollars >= 0 ? "up" : "down";
  const gainSign = acct.gain_dollars >= 0 ? "+" : "-";

  el.innerHTML = `
    <span class="totals-item">
      <span class="dim">Total Value</span>
      <strong>${fmt$(acct.total_value)}</strong>
    </span>
    <span class="totals-item">
      <span class="dim">Gain / Loss</span>
      <strong class="${gainCls}">${gainSign}${fmtAbs$(acct.gain_dollars)}</strong>
    </span>
    <span class="totals-item">
      <strong class="${gainCls}">${fmtPct(acct.gain_percent)}</strong>
    </span>
    <span class="totals-item">
      <span class="dim">Cash</span>
      <strong>${fmt$(acct.cash)}</strong>
    </span>
  `;
  panel.hidden = false;
}

// One LIVE holding card. Option positions (asset_type "OPTION", decoded from the
// OCC symbol by web/brokerages.py) render differently from equities: the
// underlying ticker with the expiration date alongside, an OPTION chip instead
// of a signal badge, and a contracts/strike/put-call subtext line. `shares`
// means contracts for options. Scan signal + analysis link only apply to
// equities (signals are merged onto positions server-side by symbol).
function renderHoldingCard(pos) {
  const card = document.createElement("div");
  card.className = "pcard";
  const isOption = pos.asset_type === "OPTION";
  const sig = (pos.signal || "").toUpperCase();
  const isUp = pos.gain_dollars >= 0;
  const gainCls = isUp ? "up" : "down";
  const gainSign = isUp ? "+" : "−";
  const arrow = isUp ? "▲" : "▼";
  const pct = Math.abs(Number(pos.gain_percent)).toFixed(2) + "%";
  const ticker = isOption ? (pos.underlying || pos.symbol) : pos.symbol;
  const expSpan = isOption && pos.expiration_date
    ? `<span class="pcard-exp">exp ${fmtExpDate(pos.expiration_date)}</span>` : "";
  const chip = isOption
    ? '<span class="badge OPTION">OPTION</span>'
    : (sig ? `<span class="badge ${sig}">${sig}</span>` : "");
  const subtext = isOption
    ? `${fmtShares(pos.shares)} contracts${pos.strike != null && pos.put_call ? ` · $${escapeHtml(pos.strike)} ${escapeHtml(pos.put_call)}` : ""} · ${gainSign}${fmtAbs$(pos.gain_dollars)}`
    : `${fmtShares(pos.shares)} shares · ${gainSign}${fmtAbs$(pos.gain_dollars)}`;

  card.innerHTML = `
    <div class="pcard-row">
      <span><span class="pcard-tk">${escapeHtml(ticker)}</span>${expSpan}</span>
      ${chip}
    </div>
    <div class="pcard-price-row">
      <span class="pcard-price">${fmt$(pos.current_price)}</span>
      <span class="pcard-pct ${gainCls}">${arrow} ${gainSign}${pct}</span>
    </div>
    <div class="pcard-divider"></div>
    <div class="pcard-metrics">
      <div>
        <div class="pcard-metric-label">Purchase price</div>
        <div class="pcard-metric-val">${fmt$(pos.average_price)}</div>
      </div>
      <div class="pcard-metrics-right">
        <div class="pcard-metric-label">Current worth</div>
        <div class="pcard-metric-val">${fmt$(pos.market_value)}</div>
      </div>
    </div>
    <div class="pcard-subtext">${subtext}</div>
    ${!isOption && pos.analysis_id ? `<div class="pcard-link"><a href="#" data-analysis="${pos.analysis_id}">Open full analysis →</a></div>` : ""}
  `;

  const link = card.querySelector("[data-analysis]");
  if (link) {
    link.addEventListener("click", (ev) => {
      ev.preventDefault();
      window.dispatchEvent(new CustomEvent("load-analysis", { detail: parseInt(link.dataset.analysis, 10) }));
      document.querySelector('.main-tab[data-tab="analyze"]')?.click();
    });
  }
  return card;
}

// Schwab (MCP) connection status line at the top of the Portfolio tab.
async function loadSchwabStatusLine() {
  const el = $("schwab-mcp-status");
  if (!el) return;
  try {
    const s = await (await fetch("/api/auth/schwab/status")).json();
    if (s.enabled === false) {
      el.innerHTML = '<span class="badge SELL">SCHWAB OFF</span> Enable Schwab in Settings to run a portfolio scan.';
    } else if (s.connected) {
      el.innerHTML = `<span class="badge BUY">SCHWAB MCP</span> connected · ${s.num_accounts || 0} account(s).`;
    } else {
      el.innerHTML = '<span class="badge SELL">NOT CONNECTED</span> Re-authorize at <a href="https://schwab.txferguson.net/auth" target="_blank" rel="noopener">schwab.txferguson.net/auth</a>.';
    }
  } catch (e) {
    el.textContent = "Schwab status unavailable.";
  }
}

// ---- Master Schwab switch ----
// When off, hide every Schwab surface so users without a brokerage still get
// reports + the S&P 500 paper builder. Moved from app.js: at T1 (no
// portfolio.js) there is no Schwab tab to hide in the first place.
async function applySchwabVisibility(enabledOverride) {
  // Callers may pass the known master-switch value (e.g. right after saving the
  // toggle) to skip the /api/auth/schwab/status round-trip, which can block on a
  // 30s MCP call when Schwab is enabled.
  let enabled = true;
  if (typeof enabledOverride === "boolean") {
    enabled = enabledOverride;
  } else {
    try {
      const s = await (await fetch("/api/auth/schwab/status")).json();
      enabled = s.enabled !== false;
    } catch (e) { /* default to showing */ }
  }
  const tabBtn = $("tab-portfolio");
  const acctBtn = $("btn-spy-account");
  if (tabBtn) tabBtn.style.display = enabled ? "" : "none";
  if (acctBtn) acctBtn.style.display = enabled ? "" : "none";
  if (!enabled) {
    const portPane = document.querySelector('[data-pane="portfolio"]');
    if (portPane && !portPane.hidden) {
      document.querySelector('.main-tab[data-tab="analyze"]')?.click();
    }
  }
}

// ---- Run Analysis tab's cross-type scan queue ----
// The Run Analysis sidebar's "Queue" stab shows EVERY scan type (portfolio,
// spy, options), not just this tab's own. Moved from app.js because its data
// source, /api/portfolio/status, is a T2 (portfolio-container) endpoint.

// Jump to a running scan's own tab and open it (queue items are cross-type here).
function _openRunningScan(item) {
  const key = scanTypeKey(item);
  // Research lives on the S&P tab; the generic /api/spy-scans/{id} route serves it.
  const tab = { portfolio: "portfolio", spy: "spy", options: "options", research: "spy" }[key];
  if (!tab) return;
  const tabBtn = document.querySelector(`.main-tab[data-tab="${tab}"]`);
  if (tabBtn) tabBtn.click();
  if (key === "portfolio" && typeof loadPortfolioScan === "function") loadPortfolioScan(item.id);
  else if ((key === "spy" || key === "research") && typeof loadSpyScan === "function") loadSpyScan(item.id);
  else if (key === "options" && typeof loadOptionsScan === "function") loadOptionsScan(item.id);
}

async function loadAnalyzeQueue() {
  const ul = $("analyze-queue");
  if (!ul) return;
  try {
    const r = await fetch("/api/portfolio/status");
    const data = r.ok ? await r.json() : { running: null, queued: [] };
    renderScanQueue(ul, data, { onOpen: _openRunningScan });  // no `only` → all scan types
  } catch (e) {
    ul.innerHTML = `<li class="empty" style="color:var(--accent-red);">${escapeHtml(String(e))}</li>`;
  }
}

// ===== Running-scan activity banner =====
// Surfaces a live progress bar on the Run Analysis tab whenever a Portfolio or
// S&P 500 scan is running in the portfolio container. Now polls a single cheap
// GET /api/portfolio/status row — the same endpoint loadAnalyzeQueue already
// hits on this same 5s tick. (The old implementation fetched
// /api/portfolio-scans and /api/spy-scans and then array-tested the
// {scans: [...]} envelope, so the banner never actually rendered.) Moved from
// app.js for the same reason as applySchwabVisibility/loadAnalyzeQueue above
// — this banner is T2+.

function setupScanActivity() {
  const box = $("scan-activity");
  if (!box) return;
  // Delegated "view →" link: jump to the owning tab (box is rewritten each poll).
  box.addEventListener("click", (e) => {
    const link = e.target.closest(".scan-activity-link");
    if (!link) return;
    e.preventDefault();
    document.querySelector(`.main-tab[data-tab="${link.dataset.tab}"]`)?.click();
  });
  pollScanActivity();  // analyze is the default tab — poll right away
  document.addEventListener("tab-shown", (ev) => {
    if (ev.detail === "analyze") { pollScanActivity(); loadAnalyzeQueue(); }
  });
  setInterval(() => {
    const pane = document.querySelector('[data-pane="analyze"]');
    if (pane && !pane.hidden) { pollScanActivity(); loadAnalyzeQueue(); }
  }, 5000);
}

async function pollScanActivity() {
  const box = $("scan-activity");
  if (!box) return;
  const blocks = [];
  try {
    const data = await apiFetch("/api/portfolio/status");
    const run = (data && data.running) || null;
    const waiting = (data && data.waiting) || [];
    // `=== "running"` for portfolio: the portfolio arm of /api/portfolio/status
    // only returns rows whose status is exactly "running" (spy rows can be
    // "pending", but portfolio rows never are). The old list-based branch also
    // required exactly "running", so a non-running status is unreachable here
    // and must render nothing.
    if (run && run.scan_type === "portfolio" && run.status === "running") {
      blocks.push(scanActivityPortfolio(run));
    }
    // Shared daily research (spy row, kind "research", no paper account):
    // its own "Daily research" block while running or pending. Allocation rows
    // parked in `running_wait_research` are equity/options rows, not research,
    // so they never land here.
    if (run && run.scan_type === "spy" && run.kind === "research") {
      const st = String(run.status || "");
      if (st.startsWith("running") || st === "pending") blocks.push(scanActivityResearch(run));
    }
    // `startsWith("running")` for spy: reproduces the old list predicate
    // exactly, including the `running_wait_*` states (`running_wait_market`,
    // `running_wait_research`). `kind !== "options"` because the old spy
    // branch fetched GET /api/spy-scans with its default `kind="equity"`, so an
    // options build was never surfaced in this banner; `kind !== "research"`
    // because research rows get their own block above.
    // The `waiting` fallback is needed because the wait states
    // (`running_wait_market`, `running_wait_research`) are excluded from
    // `_is_any_scan_running`'s busy set by default, so a parked spy row lands
    // in `waiting`, not `running`. Without this fallback the wait-state banner
    // would silently disappear. The same kind guards are applied to the
    // fallback, since options wait-state rows must not be mislabeled as an
    // S&P 500 scan.
    const spy =
      (run && run.scan_type === "spy" && run.kind !== "options" && run.kind !== "research"
        && String(run.status || "").startsWith("running")) ? run
      : waiting.find((w) => w.scan_type === "spy" && w.kind !== "options" && w.kind !== "research"
        && String(w.status || "").startsWith("running"));
    if (spy) blocks.push(scanActivitySpy(spy));
  } catch (e) { /* portfolio app unreachable / not authed — skip */ }

  if (!blocks.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.innerHTML = '<div class="panel-title">[ Scan in progress ]</div>' + blocks.join("");
  box.hidden = false;
}

function scanActivityPortfolio(scan) {
  const sc = scan.scanned_count || 0;
  const st = scan.scan_total || 0;
  const ticker = scan.current_ticker ? ` · <strong>${escapeHtml(scan.current_ticker)}</strong>` : "";
  return (
    '<div class="scan-activity-row">' +
      '<div class="scan-activity-head">' +
        '<a href="#" class="scan-activity-link" data-tab="portfolio">Portfolio scan →</a> ' +
        `<span class="dim">${sc}/${st} analyzed${ticker}</span>` +
      "</div>" +
      progressBar(sc, st) +
    "</div>"
  );
}

function scanActivitySpy(scan) {
  const qt = scan.quick_total || 151;
  const qc = scan.quick_count || 0;
  const dt = scan.deep_total || 51;
  const dc = scan.deep_count || 0;
  return (
    '<div class="scan-activity-row">' +
      '<div class="scan-activity-head">' +
        '<a href="#" class="scan-activity-link" data-tab="spy">S&amp;P 500 scan →</a>' +
      "</div>" +
      `<div class="scan-activity-sub">Quick ${qc}/${qt}</div>` +
      progressBar(qc, qt) +
      `<div class="scan-activity-sub">Deep ${dc}/${dt}</div>` +
      progressBar(dc, dt) +
    "</div>"
  );
}

function scanActivityResearch(scan) {
  const qt = scan.quick_total || 151;
  const qc = scan.quick_count || 0;
  const dt = scan.deep_total || 51;
  const dc = scan.deep_count || 0;
  return (
    '<div class="scan-activity-row">' +
      '<div class="scan-activity-head">' +
        `<a href="#" class="scan-activity-link" data-tab="spy">Daily research #${escapeHtml(String(scan.id ?? ""))} →</a>` +
      "</div>" +
      `<div class="scan-activity-sub">Quick ${qc}/${qt}</div>` +
      progressBar(qc, qt) +
      `<div class="scan-activity-sub">Deep ${dc}/${dt}</div>` +
      progressBar(dc, dt) +
    "</div>"
  );
}

const NIGHTLY_TIME_KEY = "SCHEDULE_NIGHTLY_SCAN_TIME";
const NIGHTLY_TIME_DEFAULT = "22:00";

// Read the nightly-scan time from the settings API and reflect it in the box.
// Non-secret settings come back verbatim in `masked`, so no new route is needed.
async function loadNightlyScanTime() {
  try {
    const data = await apiFetch("/api/settings");
    const entry = data.registry.find((s) => s.key === NIGHTLY_TIME_KEY);
    if (!entry) return;
    const value = (entry.has_value && entry.masked) || NIGHTLY_TIME_DEFAULT;
    const timeEl = $("pf-nightly-time");
    const captionEl = $("pf-nightly-caption");
    if (timeEl) timeEl.value = value;
    if (captionEl) {
      captionEl.textContent = "Pulls your real Schwab holdings (via the Schwab MCP) and runs each through the agents. Idempotent for today; the nightly cron also fires at " + value + " ET (Mon-Fri).";
    }
  } catch (e) {
    console.error("loadNightlyScanTime failed:", e);
  }
}

// Persist a new nightly-scan time. The scheduler's reconciler picks it up
// within ~60s — no container restart.
async function saveNightlyScanTime() {
  const value = $("pf-nightly-time").value;
  const status = $("pf-nightly-status");
  if (!value || !/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) {
    if (status) status.textContent = "enter HH:MM";
    return;
  }
  try {
    await apiFetch("/api/settings/" + NIGHTLY_TIME_KEY, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value }) });
    if (status) status.textContent = "saved — takes effect within a minute";
    const captionEl = $("pf-nightly-caption");
    if (captionEl) {
      captionEl.textContent = "Pulls your real Schwab holdings (via the Schwab MCP) and runs each through the agents. Idempotent for today; the nightly cron also fires at " + value + " ET (Mon-Fri).";
    }
  } catch (e) {
    if (status) {
      status.textContent = "save failed: " + e;
      status.style.color = "var(--accent-red)";
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  _setupPortfolioStabs();
  $("btn-scan-now")?.addEventListener("click", runScanNow);
  $("portfolio-history-clear-btn")?.addEventListener("click", clearPortfolioHistory);

  // Aggressiveness slider live label
  const aggSlider = $("pf-aggressiveness");
  if (aggSlider) {
    aggSlider.addEventListener("input", () => {
      const v = $("pf-aggressiveness-val");
      if (v) v.textContent = aggSlider.value;
    });
  }
  // Bias toggle
  document.querySelectorAll("#pf-bias .bias-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#pf-bias .bias-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    });
  });

  // Nightly scan time
  loadNightlyScanTime();
  const nightlyTime = $("pf-nightly-time");
  if (nightlyTime) {
    nightlyTime.addEventListener("change", saveNightlyScanTime);
  }

  applySchwabVisibility();
  setupScanActivity();
  loadAnalyzeQueue();
});

document.addEventListener("tab-shown", (ev) => {
  if (ev.detail === "portfolio") {
    loadNightlyScanTime();
    loadPortfolioQueue();
    loadPortfolioHistory();
    loadSchwabStatusLine();
    loadAccountHoldings();
  }
});

// Auto-refresh portfolio queue + history every 10s while the tab is visible
setInterval(() => {
  const pane = document.querySelector('[data-pane="portfolio"]');
  if (pane && !pane.hidden) {
    loadPortfolioQueue();
    loadPortfolioHistory();
  }
}, 10000);
