/* Rozhraní bota: dotazuje se na /api/state a překresluje obrazovku.
   Bez frameworku a bez build kroku — stačí otevřít prohlížeč. */

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
const money = (v) => (v >= 0 ? "+" : "") + fmt(v, 2);
const pct = (v, d = 2) => fmt(v, d) + " %";
const timeOf = (ts) => new Date(ts * 1000).toLocaleTimeString("cs-CZ");

let state = null;          // poslední odpověď serveru
let pending = {};          // neuložené změny nastavení
let refreshMs = 3000;

/* ---------------- načítání ---------------- */

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return response.json();
}

async function refresh() {
  try {
    state = await api("/api/state");
    render();
    $("pulse").classList.add("on");
    setTimeout(() => $("pulse").classList.remove("on"), 400);
  } catch (err) {
    $("venue").textContent = "spojení s botem selhalo: " + err.message;
  }
}

/* ---------------- vykreslení ---------------- */

function render() {
  renderHeader();
  renderMarkets();
  renderOpportunities();
  renderPositions();
  renderSignals();
  renderHistory();
  if (!Object.keys(pending).length) renderSettings();
}

function renderHeader() {
  const account = state.account;
  const mode = account.dry_run && account.mode === "live" ? "DRY RUN" : account.mode.toUpperCase();
  const net = state.settings.readonly.testnet ? "testnet" : "ostrá burza";
  $("venue").textContent = `${mode} · ${account.exchange} · ${net}`;
  $("equity").textContent = fmt(account.equity) + " " + account.currency;
  const daily = account.stats.daily_pnl;
  $("dailyPnl").textContent = money(daily);
  $("dailyPnl").className = daily >= 0 ? "pos" : "neg";
  $("openCount").textContent = account.open_positions.length;
  $("winRate").textContent = account.stats.closed_trades
    ? fmt(account.stats.win_rate * 100, 0) + " %" : "—";
  $("btnKill").classList.toggle("active", account.kill_switch);
  $("btnKill").textContent = account.kill_switch ? "STOP JE ZAPNUT" : "STOP";

  const scanner = state.scanner;
  $("scanInfo").textContent = scanner.last_scan
    ? `poslední sken před ${fmt(scanner.age_seconds, 0)} s · ${scanner.timeframe}`
      + (scanner.autopilot ? " · AUTOPILOT" : "")
    : "čekám na první sken…";
}

function renderMarkets() {
  const markets = state.scanner.markets || [];
  if (!markets.length) {
    $("markets").innerHTML = '<div class="empty">Skener zatím nemá data. Zkontroluj watchlist v Nastavení.</div>';
    return;
  }
  $("markets").innerHTML = markets.map(marketCard).join("");
}

function marketCard(entry) {
  if (entry.error) {
    return `<div class="card"><div class="card-head"><span class="symbol">${entry.symbol}</span></div>
      <div class="veto">Chyba: ${escapeHtml(entry.error)}</div></div>`;
  }
  const m = entry.market;
  const htf = m.htf_trend > 0 ? "▲" : m.htf_trend < 0 ? "▼" : "–";
  return `<div class="card">
    <div class="card-head">
      <span class="symbol">${entry.symbol}</span>
      <span class="badge ${m.regime}">${regimeLabel(m.regime)}</span>
      <span class="price">${fmt(m.price, priceDigits(m.price))}</span>
    </div>
    <div class="stats">
      <span>ATR <b>${pct(m.atr_pct)}</b></span>
      <span>ADX <b>${fmt(m.adx, 1)}</b></span>
      <span>RSI <b>${fmt(m.rsi, 1)}</b></span>
      <span>HTF <b>${htf}</b></span>
      <span>spread <b>${fmt(m.spread_bps, 1)} bps</b></span>
    </div>
    ${sparkline(entry.sparkline)}
    <div class="sides">
      ${sideBox("long", entry.sides.long, m.price)}
      ${sideBox("short", entry.sides.short, m.price)}
    </div>
    ${triggersHtml(entry.triggers)}
  </div>`;
}

function sideBox(dir, info, price) {
  const ready = info.tradeable ? `ready-${dir}` : "";
  const color = dir === "long" ? "var(--long)" : "var(--short)";
  const tps = info.take_profits.map((tp, i) =>
    `<div class="tp"><span>TP${i + 1} · ${fmt(tp.r, 1)}R · ${fmt(tp.fraction * 100, 0)} %</span>
     <span>${fmt(tp.price, priceDigits(price))}</span></div>`).join("");
  return `<div class="side ${ready}">
    <div class="side-head">
      <span class="dir-${dir}">${dir === "long" ? "LONG" : "SHORT"}</span>
      <span style="margin-left:auto;font-family:var(--mono)">${fmt(info.score, 3)}</span>
    </div>
    <div class="score-bar"><div class="score-fill" style="width:${Math.round(info.score * 100)}%;background:${color}"></div></div>
    <div class="levels">
      <div class="sl"><span>SL · ${pct(info.stop_pct)}</span><span>${fmt(info.stop_loss, priceDigits(price))}</span></div>
      ${tps}
    </div>
    ${info.veto ? `<div class="veto">⛔ ${escapeHtml(info.veto)}</div>` : ""}
  </div>`;
}

function triggersHtml(triggers) {
  if (!triggers || !triggers.length) return "";
  return `<div class="triggers">${triggers.map((t) =>
    `<div class="trigger ${t.side}"><b>${t.side.toUpperCase()} · ${t.kind}</b> — ${escapeHtml(t.description)}
     <span class="muted">(síla ${fmt(t.strength, 2)})</span></div>`).join("")}</div>`;
}

function sparkline(points) {
  if (!points || points.length < 2) return "";
  const min = Math.min(...points), max = Math.max(...points), span = max - min || 1;
  const step = 100 / (points.length - 1);
  const path = points.map((p, i) => `${(i * step).toFixed(2)},${(30 - ((p - min) / span) * 28).toFixed(2)}`).join(" ");
  const rising = points[points.length - 1] >= points[0];
  return `<svg class="spark" viewBox="0 0 100 32" preserveAspectRatio="none">
    <polyline points="${path}" fill="none" stroke="${rising ? "var(--long)" : "var(--short)"}" stroke-width="1.2"/>
  </svg>`;
}

function renderOpportunities() {
  const scanner = state.scanner;
  const universe = scanner.universe || {};
  $("universeInfo").textContent = scanner.auto_universe
    ? `${universe.eligible ?? 0} trhů prošlo filtrem z ${universe.total_markets ?? 0} na burze`
      + ` · hloubkově se analyzuje ${universe.deep_scan_count} nejlepších`
      + (universe.age_minutes !== null && universe.age_minutes !== undefined
         ? ` · žebříček přepočten před ${fmt(universe.age_minutes, 0)} min` : "")
    : "automatický výběr je vypnutý — sleduje se pevný watchlist";

  const rows = scanner.opportunities || [];
  if (!rows.length) {
    $("opportunities").innerHTML = '<div class="empty">Skener ještě nemá dost dat. Chvíli to potrvá — trhy se analyzují po dávkách.</div>';
    return;
  }
  $("opportunities").innerHTML = `<table><thead><tr>
    <th>Symbol</th><th>Cena</th><th>Režim</th><th>ATR</th><th>Nejlepší směr</th>
    <th>Skóre</th><th>SL</th><th>TP</th><th>Stav</th><th>Spouštěč</th>
    </tr></thead><tbody>
    ${rows.map((r) => `<tr>
      <td>${r.symbol}</td>
      <td>${fmt(r.price, priceDigits(r.price))}</td>
      <td><span class="badge ${r.regime}">${regimeLabel(r.regime)}</span></td>
      <td>${pct(r.atr_pct)}</td>
      <td class="${r.side === "long" ? "pos" : "neg"}">${r.side.toUpperCase()}</td>
      <td>${scoreCell(r.score)}</td>
      <td>${pct(r.stop_pct)}</td>
      <td>${r.take_profits}×</td>
      <td>${r.tradeable
        ? '<span class="pill ok">připraveno</span>'
        : `<span class="pill no" title="${escapeHtml(r.veto || "skóre pod prahem")}">čeká</span>`}</td>
      <td style="font-family:inherit">${r.triggers.length
        ? r.triggers.map((t) => escapeHtml(t.kind)).join(", ") : "—"}</td>
    </tr>`).join("")}</tbody></table>`;
}

function scoreCell(score) {
  const width = Math.round(score * 100);
  const color = score >= 0.6 ? "var(--long)" : score >= 0.4 ? "var(--warn)" : "var(--muted)";
  return `<div style="display:flex;align-items:center;gap:6px">
    <div class="score-bar" style="width:52px;margin:0"><div class="score-fill"
      style="width:${width}%;background:${color}"></div></div>${fmt(score, 3)}</div>`;
}

function renderPositions() {
  const rows = state.account.open_positions;
  if (!rows.length) {
    $("positions").innerHTML = '<div class="empty">Žádné otevřené pozice.</div>';
    return;
  }
  const tracked = Object.fromEntries(state.trades.open.map((t) => [t.symbol, t]));
  $("positions").innerHTML = `<table><thead><tr>
    <th>Symbol</th><th>Směr</th><th>Množství</th><th>Vstup</th><th>SL</th>
    <th>Páka</th><th>Nerealizovaný PnL</th><th></th></tr></thead><tbody>
    ${rows.map((p) => {
      const t = tracked[p.symbol];
      return `<tr>
        <td>${p.symbol}</td>
        <td class="${p.side === "long" ? "pos" : "neg"}">${p.side.toUpperCase()}</td>
        <td>${fmt(p.qty, 6)}</td>
        <td>${fmt(p.entry, priceDigits(p.entry))}</td>
        <td>${t && t.stop_loss ? fmt(t.stop_loss, priceDigits(p.entry)) : "—"}</td>
        <td>${p.leverage}x</td>
        <td class="${p.unrealized_pnl >= 0 ? "pos" : "neg"}">${money(p.unrealized_pnl)}</td>
        <td><button class="btn small danger" onclick="closePosition('${p.symbol}')">Zavřít</button></td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

function renderSignals() {
  const rows = state.signals;
  if (!rows.length) {
    $("signals").innerHTML = '<div class="empty">Zatím žádná rozhodnutí. Robot čeká na vhodnou situaci.</div>';
    return;
  }
  $("signals").innerHTML = `<table><thead><tr>
    <th>Čas</th><th>Symbol</th><th>Směr</th><th>Výsledek</th><th>Skóre</th><th>Režim</th><th>Důvod</th>
    </tr></thead><tbody>
    ${rows.map((s) => `<tr>
      <td>${timeOf(s.ts)}</td>
      <td>${s.symbol}</td>
      <td class="${s.side === "long" ? "pos" : s.side === "short" ? "neg" : ""}">${(s.side || "—").toUpperCase()}</td>
      <td><span class="pill ${s.accepted ? "ok" : "no"}">${s.accepted ? "VSTOUPIL" : "VYNECHAL"}</span></td>
      <td>${s.score !== null ? fmt(s.score, 3) : "—"}</td>
      <td>${s.regime ? regimeLabel(s.regime) : "—"}</td>
      <td style="font-family:inherit">${escapeHtml(s.reason || "")}</td>
    </tr>`).join("")}</tbody></table>`;
}

function renderHistory() {
  const rows = state.trades.closed;
  if (!rows.length) {
    $("history").innerHTML = '<div class="empty">Zatím žádné uzavřené obchody.</div>';
    return;
  }
  $("history").innerHTML = `<table><thead><tr>
    <th>Zavřeno</th><th>Symbol</th><th>Směr</th><th>Vstup</th><th>Výstup</th>
    <th>PnL</th><th>R</th><th>Důvod</th></tr></thead><tbody>
    ${rows.map((t) => `<tr>
      <td>${t.closed_at ? timeOf(t.closed_at) : "—"}</td>
      <td>${t.symbol}</td>
      <td class="${t.side === "long" ? "pos" : "neg"}">${t.side.toUpperCase()}</td>
      <td>${fmt(t.entry, priceDigits(t.entry))}</td>
      <td>${t.exit ? fmt(t.exit, priceDigits(t.entry)) : "—"}</td>
      <td class="${t.pnl >= 0 ? "pos" : "neg"}">${money(t.pnl)}</td>
      <td class="${t.r_multiple >= 0 ? "pos" : "neg"}">${t.r_multiple !== null ? fmt(t.r_multiple, 2) + "R" : "—"}</td>
      <td style="font-family:inherit">${escapeHtml(t.exit_reason || "")}</td>
    </tr>`).join("")}</tbody></table>`;
}

/* ---------------- nastavení ---------------- */

const GROUPS = [
  {
    title: "Investice a riziko", note: "Kolik účtu robot vsadí na jeden obchod.",
    fields: [
      ["risk.risk_per_trade_pct", "Riziko na obchod (%)", "Zásah SL stojí přesně tolik % účtu", "number", 0.1],
      ["risk.max_risk_per_trade_pct", "Tvrdý strop rizika (%)", "Ani adaptace ho nepřekročí", "number", 0.1],
      ["risk.max_portfolio_risk_pct", "Riziko portfolia (%)", "Součet všech otevřených pozic", "number", 0.5],
      ["risk.max_daily_loss_pct", "Denní stop (%)", "Po překročení robot přestane vstupovat", "number", 0.5],
      ["risk.max_leverage", "Maximální páka", "Strop bez ohledu na nabídku burzy", "number", 1],
      ["risk.max_open_positions", "Max. pozic naráz", "", "number", 1],
      ["risk.max_daily_trades", "Max. obchodů za den", "", "number", 1],
    ],
  },
  {
    title: "Kdy vstoupit", note: "Přísnost filtru — vyšší práh = méně, ale kvalitnějších obchodů.",
    fields: [
      ["strategy.min_score", "Minimální skóre", "0 až 1, pod prahem robot nevstoupí", "number", 0.01],
      ["strategy.veto_counter_trend", "Zákaz protitrendu", "Nevstupovat proti silnému trendu", "bool"],
      ["strategy.adaptive_learning", "Učení z historie", "Upravovat riziko podle úspěšnosti v režimu", "bool"],
      ["strategy.adx_trend_threshold", "ADX práh trendu", "Nad touto hodnotou = trendový režim", "number", 0.5],
      ["strategy.adx_range_threshold", "ADX práh range", "Pod touto hodnotou = range režim", "number", 0.5],
      ["strategy.volatile_atr_pct", "ATR % pro volatilní režim", "", "number", 0.1],
      ["strategy.htf_multiplier", "Násobek vyššího TF", "4 = potvrzení na 4× vyšším timeframu", "number", 1],
    ],
  },
  {
    title: "Sledované trhy", note: "Co skener prochází a jak často.",
    fields: [
      ["exchange.quote", "Měna účtu", "Co na burze držíš — USDT, USDC…", "text"],
      ["scanner.auto_universe", "Vybírat z celé burzy", "Vypnuto = jede jen pevný seznam níže", "bool"],
      ["universe.min_volume_24h", "Minimální 24h objem", "Pod tím je kniha moc mělká", "number", 1000000],
      ["universe.deep_scan_count", "Kolik trhů analyzovat", "Špička žebříčku", "number", 1],
      ["universe.batch_size", "Trhů na jedno kolo", "Menší číslo = šetrnější k limitům API", "number", 1],
      ["universe.refresh_minutes", "Přepočet žebříčku (min)", "", "number", 5],
      ["universe.max_spread_bps", "Max. spread pro výběr (bps)", "", "number", 1],
      ["universe.weight_liquidity", "Váha likvidity", "Jak moc rozhoduje objem", "number", 0.05],
      ["universe.weight_volatility", "Váha volatility", "Ideál je střední pohyb", "number", 0.05],
      ["universe.weight_momentum", "Váha pohybu", "Jak moc rozhoduje denní změna", "number", 0.05],
      ["scanner.watchlist", "Pevný seznam", "Použije se, když je výběr z burzy vypnutý", "list"],
      ["scanner.timeframe", "Timeframe", "1m, 5m, 15m, 1h, 4h…", "text"],
      ["scanner.interval_seconds", "Interval skenu (s)", "", "number", 5],
      ["scanner.autopilot", "AUTOPILOT", "Obchodovat z vlastních signálů, bez TradingView", "bool"],
      ["scanner.min_trigger_strength", "Min. síla spouštěče", "0 až 1, jen pro autopilot", "number", 0.05],
    ],
  },
  {
    title: "Ochrany", note: "Co robot udělá, když se nedaří.",
    fields: [
      ["risk.cooldown_after_loss_min", "Pauza po ztrátě (min)", "Na daném symbolu", "number", 5],
      ["risk.streak_cooldown_min", "Pauza po sérii ztrát (min)", "", "number", 15],
      ["risk.max_spread_bps", "Max. spread (bps)", "Nad tím neobchoduje", "number", 1],
      ["exits.max_hold_minutes", "Časový stop (min)", "0 = vypnuto", "number", 15],
      ["risk.kill_switch", "STOP (kill switch)", "Okamžitě zastaví nové vstupy", "bool"],
    ],
  },
];

function renderSettings() {
  const values = state.settings.values;
  const regimes = state.settings.regimes;
  const groups = GROUPS.map((group) => `<div class="group">
    <h3>${group.title}</h3><span class="muted">${group.note}</span>
    ${group.fields.map((f) => fieldHtml(f, values)).join("")}
  </div>`).join("");

  const exitGroups = regimes.map((regime) => tpEditor(regime, values)).join("");
  $("settings").innerHTML =
    `<div class="warnbox">Změny se projeví hned po uložení a zapíšou se do <code>config/config.yaml</code>.
     Už otevřené pozice si nechávají SL/TP, se kterými byly otevřeny.</div>`
    + `<div class="settings" style="grid-column:1/-1;padding:0">${groups}${exitGroups}</div>`;
}

function fieldHtml([key, label, hint, type, step], values) {
  const value = values[key];
  const id = "f_" + key.replace(/\./g, "_");
  let input;
  if (type === "bool") {
    input = `<input class="switch" type="checkbox" id="${id}" ${value ? "checked" : ""}
      onchange="setPending('${key}', this.checked, this)">`;
  } else if (type === "list") {
    input = `<input class="wide" type="text" id="${id}" value="${(value || []).join(", ")}"
      oninput="setPending('${key}', this.value.split(',').map(s=>s.trim()).filter(Boolean), this)">`;
  } else if (type === "text") {
    input = `<input type="text" id="${id}" value="${value}" oninput="setPending('${key}', this.value, this)">`;
  } else {
    input = `<input type="number" step="${step || 1}" id="${id}" value="${value}"
      oninput="setPending('${key}', parseFloat(this.value), this)">`;
  }
  const wide = type === "list";
  return `<div class="field" ${wide ? 'style="flex-direction:column;align-items:stretch;gap:4px"' : ""}>
    <label for="${id}">${label}${hint ? `<small>${hint}</small>` : ""}</label>${input}</div>`;
}

/* Editor SL/TP pro jeden režim trhu — počet TP se dá měnit. */
function tpEditor(regime, values) {
  const multiples = pendingOr("exits.tp_r_multiples", values)[regime] || [];
  const fractions = pendingOr("exits.tp_fractions", values)[regime] || [];
  const sum = fractions.reduce((a, b) => a + b, 0);
  const rows = multiples.map((r, i) => `<div class="tp-row">
    <span class="idx">TP${i + 1}</span>
    <input type="number" step="0.1" value="${r}" title="násobek R"
      onchange="setTp('${regime}', ${i}, 'r', parseFloat(this.value))">
    <input type="number" step="0.05" value="${fractions[i] ?? 0}" title="podíl pozice (0–1)"
      onchange="setTp('${regime}', ${i}, 'f', parseFloat(this.value))">
    <button class="btn small" onclick="removeTp('${regime}', ${i})">×</button>
  </div>`).join("");
  const bad = Math.abs(sum - 1) > 0.005;
  return `<div class="group">
    <h3>SL / TP — ${regimeLabel(regime)}</h3>
    <span class="muted">Vlevo násobek R, vpravo podíl pozice. Součet podílů má být 1.0.</span>
    ${fieldHtml([`__sl_${regime}`, "SL = ATR ×", "Vzdálenost stopu v násobcích ATR", "number", 0.1],
      { [`__sl_${regime}`]: pendingOr("exits.sl_atr_mult", values)[regime] })
      .replace(`setPending('__sl_${regime}', parseFloat(this.value), this)`,
               `setRegimeValue('exits.sl_atr_mult','${regime}', parseFloat(this.value), this)`)}
    ${fieldHtml([`__tr_${regime}`, "Trailing = ATR ×", "Vzdálenost trailing stopu", "number", 0.1],
      { [`__tr_${regime}`]: pendingOr("exits.trail_atr_mult", values)[regime] })
      .replace(`setPending('__tr_${regime}', parseFloat(this.value), this)`,
               `setRegimeValue('exits.trail_atr_mult','${regime}', parseFloat(this.value), this)`)}
    <div style="margin-top:10px">${rows || '<span class="muted">Žádné TP — pozice se zavře jen na SL nebo trailingu.</span>'}</div>
    <div class="tp-actions"><button class="btn small" onclick="addTp('${regime}')">+ Přidat TP</button></div>
    <div class="sum-hint ${bad ? "bad" : ""}">Součet podílů: ${fmt(sum, 3)}${bad ? " — musí být 1.0" : " ✓"}</div>
  </div>`;
}

function pendingOr(key, values) {
  return JSON.parse(JSON.stringify(pending[key] ?? values[key]));
}

function setPending(key, value, element) {
  pending[key] = value;
  if (element) element.classList.add("dirty");
  markUnsaved();
}

function setRegimeValue(key, regime, value, element) {
  const map = pendingOr(key, state.settings.values);
  map[regime] = value;
  setPending(key, map, element);
}

function setTp(regime, index, which, value) {
  const key = which === "r" ? "exits.tp_r_multiples" : "exits.tp_fractions";
  const map = pendingOr(key, state.settings.values);
  map[regime][index] = value;
  setPending(key, map);
  renderSettings();
}

function addTp(regime) {
  const multiples = pendingOr("exits.tp_r_multiples", state.settings.values);
  const fractions = pendingOr("exits.tp_fractions", state.settings.values);
  const last = multiples[regime][multiples[regime].length - 1] || 0.5;
  multiples[regime].push(Number((last + 1).toFixed(2)));
  fractions[regime].push(0);
  redistribute(fractions[regime]);
  setPending("exits.tp_r_multiples", multiples);
  setPending("exits.tp_fractions", fractions);
  renderSettings();
}

function removeTp(regime, index) {
  const multiples = pendingOr("exits.tp_r_multiples", state.settings.values);
  const fractions = pendingOr("exits.tp_fractions", state.settings.values);
  multiples[regime].splice(index, 1);
  fractions[regime].splice(index, 1);
  redistribute(fractions[regime]);
  setPending("exits.tp_r_multiples", multiples);
  setPending("exits.tp_fractions", fractions);
  renderSettings();
}

/** Rozdělí podíly rovnoměrně, aby jejich součet dal 1.0. */
function redistribute(list) {
  if (!list.length) return;
  const share = Number((1 / list.length).toFixed(4));
  for (let i = 0; i < list.length; i++) list[i] = share;
  list[list.length - 1] = Number((1 - share * (list.length - 1)).toFixed(4));
}

function markUnsaved() {
  $("saveInfo").textContent = `${Object.keys(pending).length} neuložených změn`;
  $("saveInfo").style.color = "var(--warn)";
}

async function saveSettings() {
  if (!Object.keys(pending).length) { $("saveInfo").textContent = "Není co ukládat."; return; }
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(pending) });
    pending = {};
    $("saveInfo").textContent = "Uloženo ✓";
    $("saveInfo").style.color = "var(--long)";
    await refresh();
    renderSettings();
  } catch (err) {
    $("saveInfo").textContent = "Chyba: " + err.message;
    $("saveInfo").style.color = "var(--short)";
  }
}

/* ---------------- akce ---------------- */

async function closePosition(symbol) {
  if (!confirm(`Zavřít pozici ${symbol} za tržní cenu?`)) return;
  await api("/api/close/" + encodeURIComponent(symbol), { method: "POST" });
  refresh();
}

async function toggleKill() {
  const next = !state.account.kill_switch;
  await api(`/control/kill-switch?enable=${next}`, { method: "POST" });
  refresh();
}

async function closeAll() {
  if (!confirm("Opravdu zavřít všechny otevřené pozice?")) return;
  await api("/control/close-all", { method: "POST" });
  refresh();
}

/* ---------------- pomocné ---------------- */

function regimeLabel(regime) {
  return {
    trend_up: "trend nahoru", trend_down: "trend dolů", range: "range",
    volatile: "volatilní", quiet: "klidný trh",
  }[regime] || regime;
}

function priceDigits(price) {
  return price >= 1000 ? 2 : price >= 1 ? 4 : 6;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- start ---------------- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("tab-" + tab.dataset.tab).classList.add("active");
  };
});

$("btnSave").onclick = saveSettings;
$("btnKill").onclick = toggleKill;
$("btnCloseAll").onclick = closeAll;
$("btnScan").onclick = async () => {
  $("btnScan").disabled = true;
  try { await api("/api/scan", { method: "POST" }); await refresh(); }
  finally { $("btnScan").disabled = false; }
};

refresh();
setInterval(refresh, refreshMs);
