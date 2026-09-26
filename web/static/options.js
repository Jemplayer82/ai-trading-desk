// TradingAgents Web — "Options" tab (daily options paper trader).
//
// Long calls/puts on S&P 500 movers, 100% simulated. Each account's daily run
// is an allocation: it waits for the shared 00:00 ET research (quick scan +
// deep dives, run once for every account) -> market-open gate (09:35 ET) ->
// contract vetting -> LLM allocator with hard guardrails. Positions
// live in normalized tables with an append-only cash ledger — realized P&L is
// real accounting here, unlike the S&P tab's snapshot portfolio.
//
// Endpoints — /api/options* routes to the portfolio app via nginx's
// `location /api/options`; scan cancel/delete reuse the /api/spy-scans routes
// (options runs are spy_scans rows with kind='options'):
//   POST   /api/options-scan                     {account_id} start today's build
//   GET    /api/options-scans?account_id=        history (kind=options only)
//   GET    /api/options-scans/{id}               scan detail (fetched on load + on real change)
//   GET    /api/options-scans/{id}/status        cheap poll target (see loadOptionsScan)
//   GET    /api/options-positions?account_id=&status=   open / settled positions
//   POST   /api/options-positions/refresh        settle + re-mark all contracts
//   GET    /api/options-summary?account_id=      cash / value / realized P&L
//   POST   /api/spy-scans/{id}/cancel            cooperative cancel
//   DELETE /api/spy-scans/{id}                   delete one scan
//   DELETE /api/spy-scans?kind=options           clear options history
//
// Globals from utils.js: $, escapeHtml, fmtTs, renderMarkdown, apiFetch,
// progressBar. Classic script, shared global scope — everything here is
// opt-/options-prefixed to avoid collisions with spy.js.

let activeOptionsId = null;
let optionsPollTimer = null;
let optAccounts = [];
let activeOptAccountId = null;
let editingOptAccountId = null;

// ===== Options paper accounts =====

async function loadOptAccounts() {
  try {
    const data = await apiFetch("/api/paper-accounts?kind=options");
    optAccounts = data.accounts || [];
  } catch (e) {
    optAccounts = [];
  }
  renderOptAccountSelector();
  renderOptAccountsModal();
}

function renderOptAccountSelector() {
  const sel = $("opt-account-sel");
  if (!sel) return;
  sel.innerHTML = "";
  if (!optAccounts.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(no options accounts — create one)";
    sel.appendChild(opt);
    activeOptAccountId = null;
    updateOptAccountMeta();
    return;
  }
  optAccounts.forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = a.name;
    if (a.id === activeOptAccountId) opt.selected = true;
    sel.appendChild(opt);
  });
  if (!activeOptAccountId || !optAccounts.some((a) => a.id === activeOptAccountId)) {
    activeOptAccountId = optAccounts[0].id;
    sel.value = activeOptAccountId;
  }
  updateOptAccountMeta();
}

function updateOptAccountMeta() {
  const meta = $("opt-account-meta");
  if (!meta) return;
  const acct = optAccounts.find((a) => a.id === activeOptAccountId);
  if (!acct) { meta.textContent = ""; return; }
  const biasLabel = { bullish: "🟢 Bullish", neutral: "⬜ Neutral", bearish: "🔴 Bearish" }[acct.bias] || acct.bias;
  meta.textContent = `$${(acct.starting_capital || 100000).toLocaleString()} · Aggressiveness ${acct.aggressiveness}/10 · ${biasLabel}`;
}

function renderOptAccountsModal() {
  const list = $("opt-accounts-list");
  if (!list) return;
  if (!optAccounts.length) {
    list.innerHTML = "<p class=\"dim\" style=\"font-size:12px;\">No options accounts yet. Create one below.</p>";
    return;
  }
  list.innerHTML = optAccounts.map((a) => {
    const biasLabel = { bullish: "Bullish", neutral: "Neutral", bearish: "Bearish" }[a.bias] || a.bias;
    const schedule = a.schedule_time ? escapeHtml(a.schedule_time) : "manual";
    return (
      "<div style=\"display:flex;align-items:center;gap:10px;padding:8px;border:1px solid var(--panel-border);border-radius:3px;margin-bottom:6px;\">" +
        "<div style=\"flex:1;\">" +
          "<strong>" + escapeHtml(a.name) + "</strong>" +
          "<span class=\"dim\" style=\"font-size:11px;margin-left:8px;\">" +
            "$" + (a.starting_capital || 100000).toLocaleString() + " · " +
            "Agg " + a.aggressiveness + "/10 · " + biasLabel + " · " + schedule + " · " + stopSummary(a) +
          "</span>" +
        "</div>" +
        "<button type=\"button\" class=\"ghost\" style=\"font-size:11px;padding:3px 8px;\" " +
          "onclick=\"editOptAccount(" + a.id + ")\">Edit</button>" +
        "<button type=\"button\" class=\"ghost\" style=\"font-size:11px;padding:3px 8px;color:var(--accent-red);border-color:var(--accent-red);\" " +
          "onclick=\"deleteOptAccount(" + a.id + ")\">Delete</button>" +
      "</div>"
    );
  }).join("");
}

async function openManageOptAccounts() {
  await loadOptAccounts();
  resetOptAccountForm();
  const modal = $("opt-accounts-modal");
  if (modal) { modal.hidden = false; modal.style.display = "flex"; }
}

function closeManageOptAccounts() {
  const modal = $("opt-accounts-modal");
  if (modal) { modal.hidden = true; modal.style.display = "none"; }
}

function populateOptAccountForm(acct) {
  if ($("opt-new-name")) $("opt-new-name").value = acct.name || "";
  if ($("opt-new-capital")) $("opt-new-capital").value = acct.starting_capital || 100000;
  if ($("opt-new-agg")) $("opt-new-agg").value = acct.aggressiveness || 5;
  if ($("opt-new-agg-val")) $("opt-new-agg-val").textContent = acct.aggressiveness || 5;
  const bias = acct.bias || "neutral";
  document.querySelectorAll("#opt-new-bias .bias-btn").forEach((x) => {
    x.classList.toggle("active", x.dataset.val === bias);
  });
  if ($("opt-new-schedule")) $("opt-new-schedule").value = acct.schedule_time || "";
  if ($("opt-new-stop-type")) $("opt-new-stop-type").value = acct.stop_type || "none";
  if ($("opt-new-stop-value")) $("opt-new-stop-value").value = acct.stop_value == null ? "" : acct.stop_value;
  if ($("opt-new-stop-offset")) $("opt-new-stop-offset").value = acct.stop_limit_offset == null ? "" : acct.stop_limit_offset;
  stopFieldVisibility("opt-new");
}

function resetOptAccountForm() {
  editingOptAccountId = null;
  populateOptAccountForm({ name: "", starting_capital: 100000, aggressiveness: 5, bias: "neutral", schedule_time: "09:00", stop_type: "none", stop_value: null, stop_limit_offset: null });
  const btn = $("btn-create-opt-account");
  if (btn) btn.textContent = "Create Account";
}

function editOptAccount(id) {
  const acct = optAccounts.find((a) => a.id === id);
  if (!acct) return;
  editingOptAccountId = id;
  populateOptAccountForm(acct);
  const btn = $("btn-create-opt-account");
  if (btn) btn.textContent = "Save Changes";
  const name = $("opt-new-name");
  if (name) name.focus();
}

async function saveOptAccount() {
  const name = ($("opt-new-name") || {}).value?.trim();
  if (!name) { alert("Enter an account name."); return; }
  const capital = parseFloat(($("opt-new-capital") || {}).value || "100000");
  const agg = parseInt(($("opt-new-agg") || {}).value || "5", 10);
  const activeBiasBtn = document.querySelector("#opt-new-bias .bias-btn.active");
  const bias = activeBiasBtn?.dataset?.val || "neutral";

  const sched = ($("opt-new-schedule") || {}).value || "";
  const stopType = ($("opt-new-stop-type") || {}).value || "none";
  const stopValue = ($("opt-new-stop-value") || {}).value || "";
  const stopOffset = ($("opt-new-stop-offset") || {}).value || "";

  if (stopType !== "none") {
    const sv = parseFloat(stopValue);
    if (isNaN(sv) || sv <= 0) { alert("Enter a positive stop value."); return; }
  }
  if (stopType === "stop_limit") {
    const so = parseFloat(stopOffset);
    if (isNaN(so) || so <= 0) { alert("Enter a positive limit offset."); return; }
  }

  const body = JSON.stringify({
    name,
    starting_capital: capital,
    aggressiveness: agg,
    bias,
    kind: "options",
    schedule_time: sched,
    stop_type: stopType,
    stop_value: stopType === "none" ? null : parseFloat(stopValue),
    stop_limit_offset: stopType === "stop_limit" ? parseFloat(stopOffset) : null,
  });
  try {
    if (editingOptAccountId) {
      await apiFetch("/api/paper-accounts/" + editingOptAccountId, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body,
      });
    } else {
      await apiFetch("/api/paper-accounts", {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
      });
    }
    resetOptAccountForm();
    await loadOptAccounts();
    await loadOptionsHistory();
  } catch (e) {
    alert("Failed to save account: " + e);
  }
}

async function deleteOptAccount(id) {
  const acct = optAccounts.find((a) => a.id === id);
  if (!confirm("Delete options account \"" + (acct?.name || id) + "\"?\n\nScan history and position records are kept but orphaned.")) return;
  try {
    await apiFetch("/api/paper-accounts/" + id, { method: "DELETE" });
    if (activeOptAccountId === id) activeOptAccountId = null;
    await loadOptAccounts();
    await loadOptionsHistory();
  } catch (e) {
    alert("Failed to delete: " + e);
  }
}

// ===== Formatting =====

function optPctCell(pct) {
  if (pct == null || isNaN(pct)) return "<span class=\"dim\">—</span>";
  const up = pct >= 0;
  const color = up ? "var(--accent-green)" : "var(--accent-red)";
  return "<span style=\"color:" + color + ";font-weight:700;\">" + (up ? "+" : "") + pct.toFixed(1) + "%</span>";
}

function optMoneyCell(v, { signed = false } = {}) {
  if (v == null || isNaN(v)) return "<span class=\"dim\">—</span>";
  const up = v >= 0;
  const color = signed ? (up ? "var(--accent-green)" : "var(--accent-red)") : "var(--text)";
  const sign = signed && up ? "+" : "";
  return "<span style=\"color:" + color + ";font-weight:600;\">" + sign + "$" + Math.round(v).toLocaleString() + "</span>";
}

// Display format: ticker (company name) + strike/call/put + expiration date.
// Company names sourced from a static map of top S&P 500 tickers.
function optContractLabel(p) {
  const companies = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "GOOGL": "Alphabet", "AMZN": "Amazon",
    "TSLA": "Tesla", "META": "Meta", "BRK": "Berkshire", "JPMJ": "JPMorgan", "V": "Visa",
    "JNJ": "J&J", "WMT": "Walmart", "XOM": "ExxonMobil", "KO": "Coca-Cola", "PG": "Procter & Gamble",
    "MA": "Mastercard", "HD": "Home Depot", "MRK": "Merck", "LLY": "Eli Lilly", "PFE": "Pfizer",
    "ABBV": "AbbVie", "ORCL": "Oracle", "NVO": "Novo Nordisk", "MMM": "3M", "CRM": "Salesforce",
    "IBM": "IBM", "INTC": "Intel", "AVGO": "Broadcom", "CAT": "Caterpillar", "C": "Citi",
    "UNH": "UnitedHealth", "CVX": "Chevron", "GE": "General Electric", "CSCO": "Cisco", "APO": "Apollo",
  };
  const ticker = p.underlying || "?";
  const company = companies[ticker] || "";
  const label = company ? ticker + " (" + company + ")" : ticker;
  const cp = (p.put_call || "?")[0];
  const strike = p.strike != null ? Number(p.strike) : 0;
  const strikeTxt = Number.isInteger(strike) ? strike : strike.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  return "<strong style=\"color:var(--accent-cyan);\">" + escapeHtml(label) + " $" + strikeTxt + cp + "</strong>" +
    "<span class=\"dim\" style=\"font-size:10px;margin-left:6px;\">" + escapeHtml(p.expiration_date || "") + "</span>";
}

function optDte(p) {
  if (!p.expiration_date) return null;
  const exp = new Date(p.expiration_date + "T00:00:00");
  return Math.round((exp - Date.now()) / 86400000);
}

// ===== On-demand ticker recommendation =====

let optRecTimer = null;

function _premiumCell(label, v, contracts100) {
  if (v == null) return "";
  const per = Number(v);
  return (
    "<div style=\"min-width:110px;\"><span class=\"dim\" style=\"font-size:10px;text-transform:uppercase;letter-spacing:.5px;\">" + label + "</span><br>" +
    "<strong>$" + per.toFixed(2) + "</strong>" +
    "<span class=\"dim\" style=\"font-size:10px;\"> ($" + Math.round(per * 100).toLocaleString() + "/ct)</span></div>"
  );
}

function renderOptRecommendation(data) {
  const box = $("opt-rec-result");
  if (!box) return;
  const rec = data.recommendation || {};
  const act = (rec.action || "NO_TRADE").toUpperCase();
  const actColor = act === "CALL" ? "var(--accent-green)" : (act === "PUT" ? "var(--accent-red)" : "var(--dim)");
  const conf = rec.confidence != null ? rec.confidence : "?";
  const q = data.quick || {};
  const contractHtml = data.contract ? optContractLabel(data.contract) : "";
  const c = data.contract || {};
  const deltaTxt = c.delta != null ? "delta " + Math.abs(Number(c.delta)).toFixed(2) : "";
  const statsBits = [
    c.dte != null ? c.dte + "d to expiry" : "",
    deltaTxt,
    c.mid != null ? "mid $" + Number(c.mid).toFixed(2) : "",
    c.open_interest != null ? "OI " + c.open_interest : "",
  ].filter(Boolean).join(" · ");
  const risks = (rec.risks || []).map((r) =>
    "<li style=\"margin:2px 0;\">" + escapeHtml(String(r)) + "</li>").join("");
  const ctx = data.context_used || {};
  const ctxBits = [
    ctx.history ? "graded decision history" : null,
    ctx.lessons ? "its own options lessons" : null,
  ].filter(Boolean).join(" + ");

  box.innerHTML = (
    "<div style=\"border:1px solid var(--panel-border);border-radius:4px;padding:14px;\">" +
      "<div style=\"display:flex;align-items:center;gap:14px;flex-wrap:wrap;\">" +
        "<span style=\"font-size:18px;font-weight:800;color:" + actColor + ";\">" +
          (act === "NO_TRADE" ? "NO TRADE" : "BUY " + act) + "</span>" +
        (contractHtml ? "<span style=\"font-size:15px;\">" + contractHtml + "</span>" : "") +
        "<span style=\"margin-left:auto;font-size:13px;\">confidence <strong style=\"font-size:16px;\">" +
          escapeHtml(String(conf)) + "/10</strong></span>" +
      "</div>" +
      (statsBits ? "<div class=\"dim\" style=\"font-size:11px;margin-top:4px;\">" + escapeHtml(statsBits) + "</div>" : "") +
      (act !== "NO_TRADE" ? (
        "<div style=\"display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;\">" +
          _premiumCell("Entry", rec.entry_premium) +
          _premiumCell("Target", rec.target_premium) +
          _premiumCell("Stop", rec.stop_premium) +
          (rec.horizon_days != null ?
            "<div><span class=\"dim\" style=\"font-size:10px;text-transform:uppercase;letter-spacing:.5px;\">Horizon</span><br><strong>" +
            escapeHtml(String(rec.horizon_days)) + "d</strong></div>" : "") +
        "</div>") : "") +
      "<p style=\"margin:12px 0 0;font-size:13px;line-height:1.5;\">" + escapeHtml(rec.thesis || "") + "</p>" +
      (risks ? "<div style=\"margin-top:8px;\"><span class=\"dim\" style=\"font-size:10px;text-transform:uppercase;letter-spacing:.5px;\">Risks</span>" +
        "<ul style=\"margin:4px 0 0 18px;font-size:12px;color:var(--dim);\">" + risks + "</ul></div>" : "") +
      "<div class=\"dim\" style=\"font-size:11px;margin-top:10px;border-top:1px solid var(--panel-border);padding-top:8px;\">" +
        "Momentum quick-read: <strong>" + escapeHtml(q.signal || "?") + " " + escapeHtml(String(q.conviction ?? "?")) + "/10</strong>" +
        (q.reasoning ? " — <span class=\"clamp-cell\" title=\"Click to expand\" style=\"display:inline-block;max-width:420px;vertical-align:bottom;\">" + escapeHtml(String(q.reasoning)) + "</span>" : "") +
        (ctxBits ? "<br>Informed by " + ctxBits + "." : "") +
        " · spot $" + Number(data.spot || 0).toFixed(2) +
        " · advisory only, nothing was traded. This call is stored and graded by the nightly learning sweep." +
      "</div>" +
    "</div>"
  );
}

async function recommendTicker() {
  const input = $("opt-rec-ticker");
  const btn = $("btn-opt-recommend");
  const status = $("opt-rec-status");
  const box = $("opt-rec-result");
  const ticker = (input?.value || "").trim().toUpperCase();
  if (!ticker) { if (status) status.textContent = "Enter a ticker."; return; }
  if (btn) btn.disabled = true;
  if (box) box.innerHTML = "";
  const started = Date.now();
  if (optRecTimer) clearInterval(optRecTimer);
  optRecTimer = setInterval(() => {
    if (status) status.textContent =
      "Analyzing " + ticker + "… quick-read, chain vetting, advisor (" +
      Math.round((Date.now() - started) / 1000) + "s — typically 30-90s)";
  }, 1000);
  try {
    const r = await fetch("/api/options-recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || ("HTTP " + r.status));
    }
    renderOptRecommendation(await r.json());
    if (status) status.textContent = "";
  } catch (e) {
    if (status) status.textContent = "";
    if (box) box.innerHTML =
      "<p style=\"color:var(--accent-red);font-size:13px;\">" + escapeHtml(String(e.message || e)) + "</p>";
  } finally {
    clearInterval(optRecTimer);
    optRecTimer = null;
    if (btn) btn.disabled = false;
  }
}

// ===== Queue + History sidebar =====

function _setupOptionsStabs() {
  document.querySelectorAll("[data-stab-target^=\"options-\"]").forEach((btn) => {
    if (!btn.dataset.stabTarget) return;  // skip the clear button
    btn.addEventListener("click", () => {
      const target = btn.dataset.stabTarget;
      document.querySelectorAll(".stab[data-stab-target^=\"options-\"]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      [$("options-queue"), $("options-history")].forEach((el) => {
        if (el) el.hidden = el.id !== target;
      });
    });
  });
}

async function loadOptionsQueue() {
  const ul = $("options-queue");
  if (!ul) return;
  try {
    const data = await apiFetch("/api/portfolio/status");
    // Options runs are spy_scans rows with kind='options'; the shared daily
    // research row (kind='research') is shown too because every allocation
    // waits on it. only:[...] keeps S&P equity allocations out of this queue.
    renderScanQueue(ul, data, { only: ["options", "research"], onOpen: (item) => loadOptionsScan(item.id) });
  } catch (e) {
    ul.innerHTML = "<li class=\"empty\" style=\"color:var(--accent-red);\">" + escapeHtml(String(e)) + "</li>";
  }
}

async function loadOptionsHistory() {
  loadOptionsQueue();  // keep queue in sync whenever history refreshes
  if (typeof loadResearchStatus === "function") loadResearchStatus();
  const ul = $("options-history");
  if (!ul) return;
  ul.innerHTML = "<li class=\"dim empty\">loading…</li>";
  try {
    const url = activeOptAccountId
      ? "/api/options-scans?account_id=" + activeOptAccountId
      : "/api/options-scans";
    const data = await apiFetch(url);
    const scans = data.scans || [];
    ul.innerHTML = "";
    const clearBtn = $("options-history-clear-btn");
    if (clearBtn) clearBtn.disabled = !scans.length;
    if (!scans.length) {
      ul.innerHTML = "<li class=\"dim empty\">(no scans yet)</li>";
      return;
    }
    scans.forEach((s) => {
      const li = document.createElement("li");
      li.dataset.id = s.id;
      if (String(s.id) === String(activeOptionsId)) li.classList.add("active");
      const running = s.status && s.status.startsWith("running");
      const statusLabel = (s.status || "—").toUpperCase();
      const statusClass = s.status === "completed" ? "BUY"
        : (s.status === "queued" ? "QUEUED"
          : (running || s.status === "pending" ? "HOLD" : "SELL"));
      const basis = s.starting_value;
      let returnBadge = "";
      if (s.current_value != null && basis) {
        const ret = ((s.current_value - basis) / basis) * 100;
        const color = ret >= 0 ? "var(--accent-green)" : "var(--accent-red)";
        returnBadge = " · <span style=\"color:" + color + ";font-weight:700;\">" + (ret >= 0 ? "+" : "") + ret.toFixed(2) + "%</span>";
      }
      li.innerHTML = (
        "<span class=\"h-main\">" +
          "<span class=\"h-top\">" +
            "<span class=\"h-tk\">#" + s.id + " · " + escapeHtml(s.trade_date) + "</span>" +
            "<span class=\"h-sig " + statusClass + "\">" + statusLabel + "</span>" +
          "</span>" +
          "<span class=\"h-ts\">" + fmtTs(s.created_at) + returnBadge + "</span>" +
        "</span>" +
        "<button class=\"h-del\" title=\"Delete this scan\" aria-label=\"Delete\">×</button>"
      );
      li.querySelector(".h-main").addEventListener("click", () => loadOptionsScan(s.id));
      li.querySelector(".h-del").addEventListener("click", (ev) => {
        ev.stopPropagation();
        deleteOptionsScan(s.id);
      });
      ul.appendChild(li);
    });
  } catch (e) {
    ul.innerHTML = "<li class=\"empty\" style=\"color:var(--accent-red);\">" + escapeHtml(e) + "</li>";
  }
}

// ===== Scan detail + account state =====

async function loadOptionsScan(id) {
  activeOptionsId = id;
  document.querySelectorAll("#options-history li").forEach((li) =>
    li.classList.toggle("active", String(li.dataset.id) === String(id))
  );
  stopOptionsPoll();

  await renderOptionsView(id);

  const r = await fetch("/api/options-scans/" + id);
  if (r.ok) {
    const scan = await r.json();
    if (scan.status && scan.status.startsWith("running")) {
      // Poll the cheap /status row every 5s, not the full scan detail (which
      // carries positions + account summary and was a confirmed ~38 MiB/hr
      // leak on the server from being re-fetched every 5s for the whole run).
      // Only re-fetch + re-render the full payload when something in the
      // status row actually changed — see spy.js's loadSpyScan for the
      // identical pattern.
      let lastPollKey = optionsPollKey(scan);
      optionsPollTimer = setInterval(async () => {
        const pr = await fetch("/api/options-scans/" + id + "/status");
        if (!pr.ok) { stopOptionsPoll(); return; }
        const status = await pr.json();
        const key = optionsPollKey(status);
        if (key === lastPollKey) return;
        lastPollKey = key;

        const fr = await fetch("/api/options-scans/" + id);
        if (!fr.ok) { stopOptionsPoll(); return; }
        const updated = await fr.json();
        await renderOptionsView(id, updated);
        updateOptStopButton(updated);
        if (!updated.status || !updated.status.startsWith("running")) stopOptionsPoll();
      }, 5000);
    }
  }
}

function optionsPollKey(s) {
  return [s.status, s.quick_count, s.deep_count, s.cancel_requested].join("|");
}

function stopOptionsPoll() {
  if (optionsPollTimer) { clearInterval(optionsPollTimer); optionsPollTimer = null; }
}

function updateOptStopButton(scan) {
  const btn = $("btn-options-stop");
  if (!btn) return;
  const running = scan && scan.status && scan.status.startsWith("running");
  btn.hidden = !running;
  if (running && scan.cancel_requested) {
    btn.disabled = true;
    btn.textContent = "Stopping…";
  } else {
    btn.disabled = false;
    btn.textContent = "Stop Scan";
  }
}

// Fetch everything the tab shows and render in one pass.
async function renderOptionsView(scanId, preloadedScan) {
  const main = $("options-main");
  if (!main) return;
  let scan = preloadedScan || null;
  try {
    if (!scan && scanId) {
      const r = await fetch("/api/options-scans/" + scanId);
      if (r.ok) scan = await r.json();
    }
  } catch (e) { /* render what we can */ }

  const acctId = (scan && scan.paper_account_id) || activeOptAccountId;
  let summary = null, openPositions = [], settledPositions = [];
  if (acctId) {
    try {
      [summary, openPositions, settledPositions] = await Promise.all([
        apiFetch("/api/options-summary?account_id=" + acctId).catch(() => null),
        apiFetch("/api/options-positions?account_id=" + acctId + "&status=open").then((d) => d.positions || []).catch(() => []),
        apiFetch("/api/options-positions?account_id=" + acctId + "&status=settled").then((d) => d.positions || []).catch(() => []),
      ]);
    } catch (e) { /* partial render */ }
  }

  main.innerHTML =
    optBannerHtml(scan) +
    optProgressHtml(scan) +
    optSummaryHtml(summary) +
    optOpenPositionsHtml(openPositions) +
    optClosedPositionsHtml(settledPositions) +
    optDecisionsHtml(scan) +
    optReportHtml(scan);
  updateOptStopButton(scan);
}

function optBannerHtml(scan) {
  if (!scan) return "";
  if (scan.status === "cancelled") {
    return "<div class=\"panel\"><p style=\"color:var(--accent-yellow);\"><strong>Scan #" + scan.id + " was stopped.</strong> Partial results below.</p></div>";
  }
  if (scan.cancel_requested && scan.status && scan.status.startsWith("running")) {
    return "<div class=\"panel\"><p style=\"color:var(--accent-yellow);\"><strong>Stopping scan #" + scan.id + "…</strong> In-progress allocation work is finishing; remaining work is skipped.</p></div>";
  }
  return "";
}

function optProgressHtml(scan) {
  if (!scan || !scan.status || !scan.status.startsWith("running")) return "";
  const qt = scan.quick_total || 151;
  const qc = scan.quick_count || 0;
  const dt = scan.deep_total || 51;
  const dc = scan.deep_count || 0;
  const gateNote = scan.status === "running_wait_research"
    ? "<p class=\"dim\" style=\"font-size:11px;margin:8px 0 0;\">Waiting for today's shared research…</p>"
    : scan.status === "running_wait_market"
    ? "<p class=\"dim\" style=\"font-size:11px;margin:8px 0 0;\">Waiting for market open (09:35 ET) so entries fill at live quotes.</p>"
    : (scan.status === "running_wait_alloc"
        ? "<p class=\"dim\" style=\"font-size:11px;margin:8px 0 0;\">Waiting for the allocation slot — another account in this build is still allocating.</p>"
        : (scan.status === "running_alloc"
            ? "<p class=\"dim\" style=\"font-size:11px;margin:8px 0 0;\">Allocating — vetting contracts and sizing positions.</p>"
            : ""));
  return (
    "<div class=\"panel\">" +
      "<div class=\"panel-title\">[ Progress ]</div>" +
      "<div style=\"margin-bottom:8px;\">" +
        "<div style=\"margin-bottom:4px;\">Shared research — quick scan: " + qc + "/" + qt + "</div>" +
        progressBar(qc, qt) +
      "</div>" +
      "<div>" +
        "<div style=\"margin-bottom:4px;\">Shared research — deep dives: " + dc + "/" + dt + "</div>" +
        progressBar(dc, dt) +
      "</div>" +
      gateNote +
    "</div>"
  );
}

function optSummaryHtml(s) {
  if (!s) return "";
  const retColor = s.return_pct >= 0 ? "var(--accent-green)" : "var(--accent-red)";
  return (
    "<div class=\"panel\">" +
      "<div class=\"panel-title\">[ Account ]</div>" +
      "<div style=\"display:flex;gap:24px;align-items:baseline;flex-wrap:wrap;\">" +
        "<span>Equity: <strong style=\"color:" + retColor + ";\">$" + Math.round(s.equity).toLocaleString() + "</strong></span>" +
        "<span>Return: <strong style=\"color:" + retColor + ";\">" + (s.return_pct >= 0 ? "+" : "") + s.return_pct.toFixed(2) + "%</strong></span>" +
        "<span>Cash: <strong>$" + Math.round(s.cash).toLocaleString() + "</strong></span>" +
        "<span>Premium at risk: <strong>$" + Math.round(s.deployed).toLocaleString() + "</strong></span>" +
        "<span>Open value: <strong>$" + Math.round(s.open_value).toLocaleString() + "</strong></span>" +
        "<span>Realized P&amp;L: " + optMoneyCell(s.realized_pnl, { signed: true }) + "</span>" +
        "<span class=\"dim\">" + s.open_count + " open · " + s.closed_count + " closed</span>" +
      "</div>" +
    "</div>"
  );
}

function optOpenPositionsHtml(positions) {
  if (!positions.length) {
    return (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\">[ Open Positions ]</div>" +
        "<p class=\"dim\">No open contracts.</p>" +
      "</div>"
    );
  }
  const rows = positions.map((p) => {
    const sig = (p.signal || "—").toUpperCase();
    const dte = optDte(p);
    const entry = Number(p.entry_premium) || 0;
    const mark = p.current_premium != null ? Number(p.current_premium) : null;
    const pnlPct = mark != null && entry > 0 ? (mark / entry - 1) * 100 : null;
    const value = p.current_value != null ? Number(p.current_value) : null;
    const cost = Number(p.cost_basis) || 0;
    const dteFlag = dte != null && dte <= 5
      ? " <span style=\"color:var(--accent-yellow);font-size:10px;\" title=\"Near expiry — force-close at 3 DTE\">⏳</span>" : "";
    const stopFlag = pnlPct != null && pnlPct <= -50
      ? " <span style=\"color:var(--accent-red);font-size:10px;\" title=\"Near stop-loss (-60%)\">⚠</span>" : "";
    const stale = (p.price_source === "carried" || p.price_source === "intrinsic")
      ? " <span class=\"dim\" style=\"font-size:10px;\" title=\"Quote unavailable — carried mark\">(stale)</span>" : "";
    return (
      "<tr>" +
        "<td>" + optContractLabel(p) + "</td>" +
        "<td><span class=\"badge " + sig + "\">" + sig + "</span></td>" +
        "<td>" + (dte != null ? dte + "d" + dteFlag : "—") + "</td>" +
        "<td style=\"font-weight:600;\">" + p.contracts + "</td>" +
        "<td>$" + entry.toFixed(2) + "</td>" +
        "<td>" + (mark != null ? "$" + mark.toFixed(2) + stale : "<span class=\"dim\">—</span>") + "</td>" +
        "<td>" + optPctCell(pnlPct) + (stopFlag || "") + "</td>" +
        "<td>" + optMoneyCell(cost) + "</td>" +
        "<td>" + optMoneyCell(value != null ? value : cost) + "</td>" +
        "<td class=\"clamp-cell\" title=\"Click to expand\" style=\"color:var(--dim);font-size:11px;\">" + escapeHtml(p.rationale || "") + "</td>" +
      "</tr>"
    );
  }).join("");
  return (
    "<div class=\"panel\">" +
      "<div class=\"panel-title\">[ Open Positions — " + positions.length + " contracts ]</div>" +
      "<div style=\"overflow-x:auto;\">" +
        "<table class=\"spy-table\">" +
          "<thead><tr><th>Contract</th><th>Signal</th><th>DTE</th><th>Qty</th><th>Entry</th><th>Mark</th><th>P&amp;L</th><th title=\"Total cost at entry\">Start</th><th title=\"Current total value\">End</th><th>Rationale</th></tr></thead>" +
          "<tbody>" + rows + "</tbody>" +
        "</table>" +
      "</div>" +
    "</div>"
  );
}

function optClosedPositionsHtml(positions) {
  if (!positions.length) return "";
  const reasonLabel = {
    llm_close: "closed", stop_loss: "STOP", trail_stop: "TRAIL", dte_floor: "DTE", expiry: "expired",
  };
  const rows = positions.slice(0, 30).map((p) => {
    const exitReason = reasonLabel[p.exit_reason] || p.exit_reason || "—";
    const reasonColor = p.exit_reason === "stop_loss" ? "var(--accent-red)"
      : (p.status === "expired_worthless" ? "var(--accent-red)" : "var(--dim)");
    const pnl = p.realized_pnl != null ? Number(p.realized_pnl) : null;
    const cost = Number(p.cost_basis) || 0;
    const pnlPct = pnl != null && cost > 0 ? (pnl / cost) * 100 : null;
    // Total proceeds at exit: exit_value if recorded, else derived from cost + realized P&L.
    const endVal = p.exit_value != null ? Number(p.exit_value)
      : (pnl != null ? cost + pnl : null);
    return (
      "<tr style=\"opacity:0.85;\">" +
        "<td>" + optContractLabel(p) + "</td>" +
        "<td>" + p.contracts + "</td>" +
        "<td>$" + (Number(p.entry_premium) || 0).toFixed(2) + "</td>" +
        "<td>" + (p.exit_premium != null ? "$" + Number(p.exit_premium).toFixed(2) : "—") + "</td>" +
        "<td>" + optMoneyCell(cost) + "</td>" +
        "<td>" + (endVal != null ? optMoneyCell(endVal) : "<span class=\"dim\">—</span>") + "</td>" +
        "<td>" + optMoneyCell(pnl, { signed: true }) + "</td>" +
        "<td>" + optPctCell(pnlPct) + "</td>" +
        "<td><span style=\"font-size:10px;font-weight:700;text-transform:uppercase;color:" + reasonColor + ";\">" + escapeHtml(exitReason) + "</span></td>" +
        "<td class=\"dim\" style=\"font-size:11px;\">" + fmtTs(p.closed_at) + "</td>" +
      "</tr>"
    );
  }).join("");
  return (
    "<div class=\"panel\">" +
      "<div class=\"panel-title\">[ Closed / Expired — last " + Math.min(positions.length, 30) + " ]</div>" +
      "<div style=\"overflow-x:auto;\">" +
        "<table class=\"spy-table\">" +
          "<thead><tr><th>Contract</th><th>Qty</th><th>Entry</th><th>Exit</th><th title=\"Total cost at entry\">Start</th><th title=\"Total proceeds at exit\">End</th><th>P&amp;L</th><th>P&amp;L %</th><th>Reason</th><th>When</th></tr></thead>" +
          "<tbody>" + rows + "</tbody>" +
        "</table>" +
      "</div>" +
    "</div>"
  );
}

// OCC option symbol -> contract identity. Fallback for decision-log rows
// stored before the engine enriched them with explicit fields: "PYPL  260814P00055000"
// -> {underlying, expiration_date, put_call, strike}. Returns null on no-match.
function parseOccSymbol(occ) {
  const m = /^([A-Z][A-Z0-9.\-]{0,5})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec(String(occ || "").trim());
  if (!m) return null;
  return {
    underlying: m[1],
    expiration_date: "20" + m[2] + "-" + m[3] + "-" + m[4],
    put_call: m[5] === "C" ? "CALL" : "PUT",
    strike: parseInt(m[6], 10) / 1000,
  };
}

// Today's decision log (scan.portfolio_json holds NEW/CLOSE/HOLD entries).
function optDecisionsHtml(scan) {
  if (!scan || !scan.portfolio_json || !scan.portfolio_json.length) return "";
  const actionColor = { NEW: "var(--accent-cyan)", CLOSE: "var(--accent-red)", HOLD: "var(--dim)" };
  const companies = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "GOOGL": "Alphabet", "AMZN": "Amazon",
    "TSLA": "Tesla", "META": "Meta", "BRK": "Berkshire", "JPMJ": "JPMorgan", "V": "Visa",
    "JNJ": "J&J", "WMT": "Walmart", "XOM": "ExxonMobil", "KO": "Coca-Cola", "PG": "Procter & Gamble",
    "MA": "Mastercard", "HD": "Home Depot", "MRK": "Merck", "LLY": "Eli Lilly", "PFE": "Pfizer",
    "ABBV": "AbbVie", "ORCL": "Oracle", "NVO": "Novo Nordisk", "MMM": "3M", "CRM": "Salesforce",
    "IBM": "IBM", "INTC": "Intel", "AVGO": "Broadcom", "CAT": "Caterpillar", "C": "Citi",
    "UNH": "UnitedHealth", "CVX": "Chevron", "GE": "General Electric", "CSCO": "Cisco", "APO": "Apollo",
  };
  const rows = scan.portfolio_json.map((d) => {
    const act = (d.action || "—").toUpperCase();
    const cid = d.underlying ? d : (parseOccSymbol(d.occ_symbol) || {});
    const ticker = cid.underlying || "?";
    const company = companies[ticker] || "";
    const contractLabel = company ? ticker + " (" + company + ")" : ticker;
    const cp = (cid.put_call || "?")[0];
    const strike = cid.strike != null ? Number(cid.strike) : 0;
    const strikeTxt = Number.isInteger(strike) ? strike : strike.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
    const contractStr = escapeHtml(contractLabel) + " $" + strikeTxt + cp + " " + escapeHtml(cid.expiration_date || "");
    const detail = act === "NEW"
      ? d.contracts + "x @ $" + (Number(d.entry_premium) || 0).toFixed(2) + " ($" + Math.round(d.cost || 0).toLocaleString() + ")"
      : (act === "CLOSE" ? "exit @ $" + (Number(d.exit_premium) || 0).toFixed(2) + " (" + escapeHtml(d.exit_reason || "") + ")" : "—");
    return (
      "<tr>" +
        "<td style=\"font-size:11px;font-weight:600;color:var(--accent-cyan);\">" + contractStr + "</td>" +
        "<td><span style=\"font-size:10px;font-weight:700;color:" + (actionColor[act] || "var(--dim)") + ";\">" + act + "</span></td>" +
        "<td class=\"dim\" style=\"font-size:11px;\">" + detail + "</td>" +
        "<td class=\"clamp-cell dim\" title=\"Click to expand\" style=\"font-size:11px;\">" + escapeHtml(d.rationale || "") + "</td>" +
      "</tr>"
    );
  }).join("");
  return (
    "<div class=\"panel\">" +
      "<div class=\"panel-title\">[ Scan #" + scan.id + " Decisions — " + escapeHtml(scan.trade_date || "") + " ]</div>" +
      "<div style=\"overflow-x:auto;\">" +
        "<table class=\"spy-table\">" +
          "<thead><tr><th>Contract</th><th>Action</th><th>Detail</th><th>Rationale</th></tr></thead>" +
          "<tbody>" + rows + "</tbody>" +
        "</table>" +
      "</div>" +
    "</div>"
  );
}

function optReportHtml(scan) {
  if (!scan) return "";
  if (scan.allocator_report) {
    return (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\">[ Allocator Report ]</div>" +
        "<div class=\"report-body\">" + renderMarkdown(scan.allocator_report) + "</div>" +
      "</div>"
    );
  }
  if (scan.error) {
    return (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\" style=\"color:var(--accent-red);\">[ Scan Failed ]</div>" +
        "<p style=\"color:var(--accent-red);white-space:pre-wrap;\">" + escapeHtml(scan.error) + "</p>" +
        "<p class=\"dim\" style=\"font-size:12px;margin-top:8px;\">Failed scans are retryable — just run the scan again.</p>" +
      "</div>"
    );
  }
  return "";
}

// ===== Actions =====

async function triggerOptionsScan() {
  const btn = $("btn-options-scan");
  const status = $("options-scan-status");
  if (!activeOptAccountId) {
    if (status) status.textContent = "Create an options paper account first (⚙ Accounts).";
    return;
  }
  if (btn) btn.disabled = true;
  if (status) status.textContent = "Queuing allocation…";
  try {
    const r = await fetch("/api/options-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: activeOptAccountId }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      // 409 = not a trading day; the route's detail says so.
      if (status) status.textContent = data.detail || ("Error: " + r.status);
      return;
    }
    if (data.error) throw new Error(data.error);
    const msg = !data.new ? "Allocation #" + data.scan_id + " already exists today"
      : "Allocation #" + data.scan_id + " queued — waits for today's research and the 09:35 ET open";
    if (status) status.textContent = msg;
    await loadOptionsHistory();
    loadOptionsScan(data.scan_id);
  } catch (e) {
    if (status) status.textContent = "Error: " + e;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function triggerOptionsStop() {
  if (!activeOptionsId) return;
  if (!confirm("Stop options scan #" + activeOptionsId + "?\n\nIn-progress allocation work will finish first. Partial results are kept; no trades are made.")) return;
  const btn = $("btn-options-stop");
  const status = $("options-scan-status");
  if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
  try {
    const r = await fetch("/api/spy-scans/" + activeOptionsId + "/cancel", { method: "POST" });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    if (status) status.textContent = "Cancellation requested…";
  } catch (e) {
    if (status) status.textContent = "Cancel failed: " + e;
    if (btn) { btn.disabled = false; btn.textContent = "Stop Scan"; }
  }
}

async function deleteOptionsScan(id) {
  if (!confirm("Delete options scan #" + id + "?\n\nPosition and ledger records are kept (they're the account's books); only the scan row and its quick-scan results go.")) return;
  try {
    const r = await fetch("/api/spy-scans/" + id, { method: "DELETE" });
    if (!r.ok) { alert("Delete failed (HTTP " + r.status + ")."); return; }
    if (String(activeOptionsId) === String(id)) {
      activeOptionsId = null;
      stopOptionsPoll();
      renderOptionsView(null);
    }
    await loadOptionsHistory();
  } catch (e) {
    alert("Delete failed: " + e);
  }
}

async function clearOptionsHistory() {
  const data = await apiFetch("/api/options-scans");
  const scans = data.scans || [];
  if (!scans.length) return;
  if (!confirm("Clear all " + scans.length + " options scans?\n\nPosition and ledger records are kept; deep-dive analyses stay in Run Analysis history.")) return;
  try {
    const r = await fetch("/api/spy-scans?kind=options", { method: "DELETE" });
    if (!r.ok) { alert("Clear failed (HTTP " + r.status + ")."); return; }
    activeOptionsId = null;
    stopOptionsPoll();
    renderOptionsView(null);
    await loadOptionsHistory();
  } catch (e) {
    alert("Clear failed: " + e);
  }
}

async function refreshOptionMarks() {
  const btn = $("btn-options-refresh");
  const status = $("options-scan-status");
  if (btn) { btn.disabled = true; btn.textContent = "Refreshing…"; }
  if (status) status.textContent = "Settling expiries + refreshing marks…";
  try {
    await apiFetch("/api/options-positions/refresh", { method: "POST" });
    if (status) status.textContent = "Marks refreshed.";
    await loadOptionsHistory();
    await renderOptionsView(activeOptionsId);
  } catch (e) {
    if (status) status.textContent = "Refresh failed: " + e;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Refresh Marks"; }
  }
}

// ===== Tab lifecycle + wiring =====

document.addEventListener("tab-shown", (ev) => {
  if (ev.detail === "options") {
    loadOptAccounts().then(async () => {
      await loadOptionsHistory();
      // Show current account state even before any scan is selected.
      if (!activeOptionsId) renderOptionsView(null);
    });
  } else {
    stopOptionsPoll();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  const btn = $("btn-options-scan");
  if (btn) btn.addEventListener("click", triggerOptionsScan);
  const stopBtn = $("btn-options-stop");
  if (stopBtn) stopBtn.addEventListener("click", triggerOptionsStop);
  const refreshBtn = $("btn-options-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", refreshOptionMarks);
  const clearBtn = $("options-history-clear-btn");
  if (clearBtn) clearBtn.addEventListener("click", clearOptionsHistory);
  _setupOptionsStabs();

  const recBtn = $("btn-opt-recommend");
  if (recBtn) recBtn.addEventListener("click", recommendTicker);
  const recInput = $("opt-rec-ticker");
  if (recInput) recInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") recommendTicker();
  });

  // Click-to-expand for clamped rationale cells. Delegated on document because
  // every table here is re-rendered wholesale via innerHTML on each poll.
  document.addEventListener("click", (e) => {
    const cell = e.target.closest(".clamp-cell");
    if (cell) cell.classList.toggle("open");
  });

  const sel = $("opt-account-sel");
  if (sel) {
    sel.addEventListener("change", () => {
      activeOptAccountId = sel.value ? parseInt(sel.value, 10) : null;
      activeOptionsId = null;
      stopOptionsPoll();
      updateOptAccountMeta();
      loadOptionsHistory();
      renderOptionsView(null);
    });
  }

  const manageBtn = $("btn-manage-opt-accounts");
  if (manageBtn) manageBtn.addEventListener("click", openManageOptAccounts);
  const closeBtn = $("btn-close-opt-accounts");
  if (closeBtn) closeBtn.addEventListener("click", closeManageOptAccounts);
  const modal = $("opt-accounts-modal");
  if (modal) modal.addEventListener("click", (e) => { if (e.target === modal) closeManageOptAccounts(); });
  const createBtn = $("btn-create-opt-account");
  if (createBtn) createBtn.addEventListener("click", saveOptAccount);
  document.getElementById("opt-new-stop-type")?.addEventListener("change", () => stopFieldVisibility("opt-new"));

  const aggSlider = $("opt-new-agg");
  if (aggSlider) {
    aggSlider.addEventListener("input", () => {
      const v = $("opt-new-agg-val");
      if (v) v.textContent = aggSlider.value;
    });
  }
  document.querySelectorAll("#opt-new-bias .bias-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#opt-new-bias .bias-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    });
  });

  loadOptAccounts();
});

// Auto-refresh history every 15s while the tab is visible.
setInterval(() => {
  const pane = document.querySelector("[data-pane=\"options\"]");
  if (pane && !pane.hidden) loadOptionsHistory();
}, 15000);