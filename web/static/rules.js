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
    h += "<p class=\"dim\" style=\"font-size:12px;\">None yet. Signals are found after each close (6:00 PM ET) and traded the next afternoon (3:45 PM ET).</p>";
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
  if (runs.length) {
    const r = runs[0];
    h += "<p class=\"dim\" style=\"font-size:11px;\">Last signal check: " + escapeHtml(r.status) + ", started " +
      escapeHtml(r.started_at || "") + "</p>";
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

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-rules-create")?.addEventListener("click", createRulesAccount);
  document.getElementById("btn-rules-reload")?.addEventListener("click", loadRulesTab);
});
document.addEventListener("tab-shown", (e) => { if (e.detail === "rules") loadRulesTab(); });
