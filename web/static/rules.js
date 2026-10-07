// Rules tab — the rules-only options paper account (web/rules_engine.py). No AI decides anything
// here: signals, contracts, sizes and exits are fixed rules. Read-only view plus "create account".
// Globals from utils.js: escapeHtml, apiFetch.

function rulesMoney(v, signed) {
  if (v == null || isNaN(v)) return "<span class=\"dim\">—</span>";
  const up = v >= 0;
  const color = signed ? (up ? "var(--accent-green)" : "var(--accent-red)") : "var(--text)";
  return "<span style=\"color:" + color + ";font-weight:600;\">" + (signed && up ? "+" : "") +
    (v < 0 ? "-" : "") + "$" + Math.abs(Math.round(v)).toLocaleString() + "</span>";
}

function rulesPct(v) {
  if (v == null || isNaN(v)) return "<span class=\"dim\">—</span>";
  const color = v >= 0 ? "var(--accent-green)" : "var(--accent-red)";
  return "<span style=\"color:" + color + ";font-weight:700;\">" + (v >= 0 ? "+" : "") + v.toFixed(1) + "%</span>";
}

// American dates: 2026-10-06 -> 10/06/2026
function rulesDate(iso) {
  if (!iso) return "—";
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[2] + "/" + m[3] + "/" + m[1] : escapeHtml(iso);
}

function rulesSystem(s) {
  return s === "pead" ? "Earnings beat" : s === "congress" ? "Congress buy" : escapeHtml(s);
}

function rulesAccountHtml(a) {
  const acct = a.account;
  const start = acct.starting_capital;
  const ret = 100 * (a.equity / start - 1);
  const spyRet = 100 * (a.spy_buy_hold / start - 1);
  const open = a.positions.filter((p) => p.status === "open");
  const closed = a.positions.filter((p) => p.status === "closed");
  const last = a.daily.length ? a.daily[a.daily.length - 1] : null;
  let h = "<div class=\"panel\"><div class=\"panel-title\">[ " + escapeHtml(acct.name) + " ]</div>";
  h += "<div style=\"display:flex;gap:28px;flex-wrap:wrap;margin-bottom:12px;\">" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">Account value</div>" + rulesMoney(a.equity) + " " + rulesPct(ret) + "</div>" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">SPY buy-and-hold, same dates</div>" + rulesMoney(a.spy_buy_hold) + " " + rulesPct(spyRet) + "</div>" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">Worst drop</div>" + (a.worst_drop_pct ? "-" + a.worst_drop_pct.toFixed(1) + "%" : "0%") + "</div>" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">Closed trades</div>" + a.closed_trades + " (" + a.wins + " winners) " + rulesMoney(a.realized_pnl, true) + "</div>" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">Idle money</div>" +
      (last ? (last.switch_on ? "in SPY (SPY above its 200-day)" : "in cash (SPY below its 200-day)") : "not run yet") + "</div>" +
    "</div>";
  h += "<div class=\"dim\" style=\"font-size:11px;margin-bottom:6px;\">Open trades</div>";
  if (!open.length) {
    h += "<p class=\"dim\" style=\"font-size:12px;\">None.</p>";
  } else {
    h += "<table class=\"spy-table\"><thead><tr><th>Stock</th><th>Signal</th><th>Contract</th><th>Bought</th>" +
      "<th>Contracts</th><th>Cost</th><th>Now (bid)</th><th>Return</th><th>Trail</th><th>Sell by</th></tr></thead><tbody>";
    for (const p of open) {
      const cost = p.cost_per * p.contracts;
      const val = (p.last_value != null ? p.last_value : p.cost_per) * p.contracts;
      h += "<tr><td>" + escapeHtml(p.ticker) + "</td><td>" + rulesSystem(p.system) + "</td><td>$" + p.strike +
        " call " + rulesDate(p.expiration_date) + "</td><td>" + rulesDate(p.entry_date) + " at ask $" + Number(p.entry_ask).toFixed(2) +
        "</td><td>" + p.contracts + "</td><td>" + rulesMoney(cost) + "</td><td>" + rulesMoney(val) + "</td><td>" +
        rulesPct(100 * (val / cost - 1)) + "</td><td>" + (p.armed ? "armed, best " + rulesMoney(p.peak_value * p.contracts) : "<span class=\"dim\">arms at +50%</span>") +
        "</td><td>" + rulesDate(p.planned_exit) + "</td></tr>";
    }
    h += "</tbody></table>";
  }
  if (closed.length) {
    h += "<div class=\"dim\" style=\"font-size:11px;margin:12px 0 6px;\">Closed trades</div>";
    h += "<table class=\"spy-table\"><thead><tr><th>Stock</th><th>Signal</th><th>Bought</th><th>Sold</th><th>Why</th>" +
      "<th>Cost</th><th>Proceeds</th><th>Profit</th></tr></thead><tbody>";
    for (const p of closed) {
      const cost = p.cost_per * p.contracts;
      h += "<tr><td>" + escapeHtml(p.ticker) + "</td><td>" + rulesSystem(p.system) + "</td><td>" + rulesDate(p.entry_date) +
        "</td><td>" + rulesDate(p.exit_date) + "</td><td>" + (p.exit_reason === "trailing_stop" ? "10% off its best" :
        p.exit_reason === "time_exit" ? "7 days before expiry" : escapeHtml(p.exit_reason)) + "</td><td>" + rulesMoney(cost) +
        "</td><td>" + rulesMoney(p.proceeds) + "</td><td>" + rulesMoney(p.pnl, true) + " " + rulesPct(100 * p.pnl / cost) + "</td></tr>";
    }
    h += "</tbody></table>";
  }
  h += "<div class=\"dim\" style=\"font-size:11px;margin:12px 0 6px;\">Signals (newest first, every one logged, taken or not)</div>";
  if (!a.signals.length) {
    h += "<p class=\"dim\" style=\"font-size:12px;\">None yet. Signals are checked each trading morning (6:30 AM ET) and traded that afternoon (3:45 PM ET).</p>";
  } else {
    h += "<table class=\"spy-table\"><thead><tr><th>Stock</th><th>Signal</th><th>Signal day</th><th>Trade day</th><th>Result</th></tr></thead><tbody>";
    for (const s of a.signals.slice(0, 60)) {
      h += "<tr><td>" + escapeHtml(s.ticker) + "</td><td>" + rulesSystem(s.system) + "</td><td>" + rulesDate(s.signal_date) +
        "</td><td>" + rulesDate(s.entry_date) + "</td><td>" + escapeHtml(s.status) + (s.reason ? " — " + escapeHtml(s.reason) : "") + "</td></tr>";
    }
    h += "</tbody></table>";
  }
  return h + "</div>";
}

async function loadRulesTab() {
  const box = document.getElementById("rules-main");
  if (!box) return;
  let data;
  try {
    data = await apiFetch("/api/options-rules");
  } catch (e) {
    box.innerHTML = "<div class=\"panel\"><p class=\"dim\">Could not load: " + escapeHtml(e.message || String(e)) + "</p></div>";
    return;
  }
  const accounts = data.accounts || [];
  let h = accounts.length ? accounts.map(rulesAccountHtml).join("") :
    "<div class=\"panel\"><p class=\"dim\">No rules account yet. Create one above.</p></div>";
  const runs = data.runs || [];
  for (const [kind, label] of [["prepare", "Last signal check"], ["run", "Last trading run"]]) {
    const r = runs.find((x) => x.kind === kind);
    if (r) {
      const color = r.status === "failed" ? "var(--accent-red)" : "var(--dim)";
      h += "<p style=\"font-size:11px;color:" + color + ";\">" + label + ": " + escapeHtml(r.status) + ", started " +
        escapeHtml(r.started_at || "") + " UTC</p>";
    }
  }
  box.innerHTML = h;
}

async function createRulesAccount() {
  const name = (document.getElementById("rules-new-name").value || "").trim();
  const capital = Number(document.getElementById("rules-new-capital").value || 50000);
  if (!name) { alert("Give the account a name."); return; }
  try {
    await apiFetch("/api/options-rules/accounts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, starting_capital: capital }),
    });
  } catch (e) {
    alert(e.message || String(e));
    return;
  }
  loadRulesTab();
}

// ── Spread accounts (web/spread_engine.py): condors / credit verticals filled from live quotes ──

function spreadLegs(legsJson) {
  let legs = [];
  try { legs = JSON.parse(legsJson); } catch (e) { return ""; }
  return legs.map((l) => (l.side === "short" ? "-" : "+") + l.strike + (l.put_call === "PUT" ? "P" : "C")).join(" ");
}

function spreadAccountHtml(a) {
  const acct = a.account;
  const ret = 100 * (a.equity / acct.starting_capital - 1);
  const open = a.positions.filter((p) => p.status === "open");
  const closed = a.positions.filter((p) => p.status === "closed");
  const working = a.orders.filter((o) => o.status === "working");
  let h = "<div class=\"panel\"><div class=\"panel-title\">[ " + escapeHtml(acct.name) + " — " +
    ({ condor: "iron condors", vertical: "credit verticals", rich_put: "rich-day put spreads" }[acct.structure] || escapeHtml(acct.structure)) + " ]</div>";
  h += "<div style=\"display:flex;gap:28px;flex-wrap:wrap;margin-bottom:12px;\">" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">Account value</div>" + rulesMoney(a.equity) + " " + rulesPct(ret) + "</div>" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">Fill rate (orders done)</div>" + (a.fill_rate == null ? "—" : (100 * a.fill_rate).toFixed(0) + "%") + "</div>" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">Avg fill vs the midpoint</div>" + (a.avg_fill_vs_mid == null ? "—" : (a.avg_fill_vs_mid >= 0 ? "+" : "") + "$" + (100 * a.avg_fill_vs_mid).toFixed(0) + " a spread") + "</div>" +
    "<div><div class=\"dim\" style=\"font-size:10px;text-transform:uppercase;\">Closed</div>" + a.closed + " (" + a.wins + " winners) " + rulesMoney(a.realized, true) + "</div>" +
    "</div>";
  const table = (title, rows, head, row) => {
    if (!rows.length) return "<div class=\"dim\" style=\"font-size:11px;margin:8px 0;\">" + title + ": none</div>";
    return "<div class=\"dim\" style=\"font-size:11px;margin:10px 0 6px;\">" + title + "</div><table class=\"spy-table\"><thead><tr>" +
      head.map((x) => "<th>" + x + "</th>").join("") + "</tr></thead><tbody>" + rows.map(row).join("") + "</tbody></table>";
  };
  const ivrv = (o) => { try { const s = JSON.parse(o.signal || "null"); return s ? s.ivrv.toFixed(2) : "—"; } catch (e) { return "—"; } };
  h += table("Working orders (limit waits for the market)", working, ["Stock", "Legs", "Expiry", "Qty", "Limit", "Started at mid", "Floor", "Days", "IV/RV"],
    (o) => "<tr><td>" + escapeHtml(o.ticker) + "</td><td>" + spreadLegs(o.legs) + "</td><td>" + rulesDate(o.expiration_date) + "</td><td>" + o.contracts +
      "</td><td>$" + Number(o.limit_price).toFixed(2) + "</td><td>$" + Number(o.initial_mid).toFixed(2) + "</td><td>$" + Number(o.floor_price).toFixed(2) + "</td><td>" + o.trading_days +
      "</td><td>" + ivrv(o) + "</td></tr>");
  h += table("Open spreads", open, ["Stock", "Legs", "Expiry", "Qty", "Credit", "Take profit at", "Cost to close now", "Max loss"],
    (p) => "<tr><td>" + escapeHtml(p.ticker) + "</td><td>" + spreadLegs(p.legs) + "</td><td>" + rulesDate(p.expiration_date) + "</td><td>" + p.contracts +
      "</td><td>$" + Number(p.credit).toFixed(2) + "</td><td>$" + Number(p.tp_price).toFixed(2) + "</td><td>" + (p.last_close_natural == null ? "—" : "$" + Number(p.last_close_natural).toFixed(2)) +
      "</td><td>" + rulesMoney(p.max_loss) + "</td></tr>");
  h += table("Closed spreads", closed.slice(0, 50), ["Stock", "Legs", "Opened", "Closed", "Why", "Credit", "Closed at", "Profit"],
    (p) => "<tr><td>" + escapeHtml(p.ticker) + "</td><td>" + spreadLegs(p.legs) + "</td><td>" + rulesDate(p.opened_at) + "</td><td>" + rulesDate(p.closed_at) +
      "</td><td>" + (p.close_reason === "profit_target" ? "profit target" : p.close_reason === "expiry" ? "expired" : escapeHtml(p.close_reason)) +
      "</td><td>$" + Number(p.credit).toFixed(2) + "</td><td>$" + Number(p.close_price).toFixed(2) + "</td><td>" + rulesMoney(p.pnl, true) + "</td></tr>");
  return h + "</div>";
}

async function loadSpreads() {
  const box = document.getElementById("spreads-main");
  if (!box) return;
  try {
    const data = await apiFetch("/api/options-spreads");
    const accts = data.accounts || [];
    box.innerHTML = accts.length ? accts.map(spreadAccountHtml).join("") : "<div class=\"panel\"><p class=\"dim\">No spread account yet.</p></div>";
  } catch (e) {
    box.innerHTML = "<div class=\"panel\"><p class=\"dim\">Could not load spread accounts: " + escapeHtml(e.message || String(e)) + "</p></div>";
  }
}

async function createSpreadAccount() {
  const name = (document.getElementById("spreads-new-name").value || "").trim();
  const structure = document.getElementById("spreads-new-structure").value;
  const capital = Number(document.getElementById("spreads-new-capital").value || 50000);
  if (!name) { alert("Give the account a name."); return; }
  try {
    await apiFetch("/api/options-spreads/accounts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, structure, starting_capital: capital }),
    });
  } catch (e) {
    alert(e.message || String(e));
    return;
  }
  loadSpreads();
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-spreads-create")?.addEventListener("click", createSpreadAccount);
  document.getElementById("btn-rules-create")?.addEventListener("click", createRulesAccount);
  document.getElementById("btn-rules-reload")?.addEventListener("click", () => { loadRulesTab(); loadSpreads(); });
});
document.addEventListener("tab-shown", (e) => { if (e.detail === "rules") { loadRulesTab(); loadSpreads(); } });
