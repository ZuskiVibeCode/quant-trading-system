/* Quant Mission Ledger — vanilla viewer for docs/data/latest.json */
(() => {
  const DATA_URL = "./data/latest.json";
  const ZONES_HISTORY_URL = "./data/zones_history.json";
  const SECTORS = ["all", "quantum", "robotics", "space"];
  const AGENT_ORDER = [
    "momentum",
    "fundamental",
    "statistical_ml",
    "sentiment",
    "macro",
  ];
  const ART = {
    all: "./art/observatory.jpg",
    quantum: "./art/quantum.jpg",
    robotics: "./art/robotics.jpg",
    space: "./art/space.jpg",
  };

  let DATA = null;
  let zonesHistory = null;
  let filter = "all";

  const $ = (id) => document.getElementById(id);

  function fmtUsd(n) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    }).format(Number(n));
  }

  function fmtPct(n) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    return `${(Number(n) * 100).toFixed(2)}%`;
  }

  function fmtSignedPct(n) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    const v = Number(n) * 100;
    const sign = v > 0 ? "+" : "";
    return `${sign}${v.toFixed(2)}%`;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return d.toLocaleString("en-US", {
        timeZone: "America/New_York",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }) + " ET";
    } catch {
      return iso;
    }
  }


  /** America/New_York calendar day as YYYY-MM-DD (en-CA). */
  function etCalendarDay(isoOrDate) {
    if (isoOrDate == null || isoOrDate === "") return null;
    try {
      const d = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
      if (Number.isNaN(d.getTime())) return null;
      return d.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
    } catch {
      return null;
    }
  }

  /** Today and prior ET calendar day (DST-safe via Y-M-D arithmetic). */
  function etTodayYesterday(now = new Date()) {
    const today = now.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
    const [Y, M, D] = today.split("-").map(Number);
    const prev = new Date(Date.UTC(Y, M - 1, D));
    prev.setUTCDate(prev.getUTCDate() - 1);
    const yesterday = prev.toISOString().slice(0, 10);
    return { today, yesterday };
  }

  /** Closed rows for Closed book UI: sector filter + today/yesterday ET only. */
  function closedRowsForRecentWindow(closed, matchesSectorFn, now = new Date()) {
    const { today, yesterday } = etTodayYesterday(now);
    return (closed || [])
      .filter((c) => matchesSectorFn(c.sector || sectorOf(c.ticker)))
      .map((c) => {
        const day = etCalendarDay(c.closed_at);
        let bucket = null;
        if (day === today) bucket = "today";
        else if (day === yesterday) bucket = "yesterday";
        return { row: c, day, bucket };
      })
      .filter((x) => x.bucket)
      .sort((a, b) => String(b.row.closed_at || "").localeCompare(String(a.row.closed_at || "")));
  }

  function trunc(s, n = 140) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function pnlClass(n) {
    if (n == null) return "";
    return Number(n) >= 0 ? "gain" : "loss";
  }

  function safeNum(n) {
    if (n == null || n === "") return null;
    const v = Number(n);
    return Number.isFinite(v) ? v : null;
  }

  function marketValue(qty, last) {
    const q = safeNum(qty);
    const l = safeNum(last);
    if (q == null || l == null) return null;
    return q * l;
  }

  function costBasis(qty, avgCost) {
    const q = safeNum(qty);
    const a = safeNum(avgCost);
    if (q == null || a == null) return null;
    return q * a;
  }

  function unrealizedPct(unrealized, qty, avgCost) {
    const u = safeNum(unrealized);
    const basis = costBasis(qty, avgCost);
    if (u == null || basis == null || basis === 0) return null;
    return u / basis;
  }

  function tickerKey(t) {
    return String(t || "").toUpperCase();
  }

  function signalsForTicker(ticker) {
    const want = tickerKey(ticker);
    const out = [];
    const agents = DATA?.agents_today || {};
    Object.values(agents).forEach((block) => {
      (block.signals || []).forEach((s) => {
        if (tickerKey(s.ticker) === want) {
          out.push({ ...s, agent_id: s.agent_id || block.agent_id });
        }
      });
    });
    out.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    return out;
  }

  function decisionsForTicker(ticker) {
    const want = tickerKey(ticker);
    const d = DATA?.decisions_today || {};
    const buckets = ["tickets", "holds", "fills", "order_submitted", "failed", "other"];
    const out = [];
    buckets.forEach((key) => {
      (d[key] || []).forEach((row) => {
        if (tickerKey(row.ticker) === want) out.push(row);
      });
    });
    out.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    return out;
  }

  function missionForTicker(ticker) {
    const want = tickerKey(ticker);
    return (DATA?.mission_log || [])
      .filter((e) => tickerKey(e.ticker) === want)
      .slice(0, 8);
  }

  function closedFillsForTicker(ticker) {
    const want = tickerKey(ticker);
    return (DATA?.closed || [])
      .filter((c) => tickerKey(c.ticker) === want)
      .slice()
      .sort((a, b) => String(b.closed_at || "").localeCompare(String(a.closed_at || "")));
  }


  function reviewsForTicker(ticker) {
    const want = tickerKey(ticker);
    return (DATA?.trade_reviews || [])
      .filter((r) => tickerKey(r.ticker) === want)
      .slice()
      .sort((a, b) => String(b.closed_at || "").localeCompare(String(a.closed_at || "")));
  }

  function reviewTagsHtml(tags) {
    const list = Array.isArray(tags) ? tags : [];
    if (!list.length) return "";
    return `<div class="badge-row" style="margin-top:0.5rem">${list.map((t) =>
      `<span class="chip review-tag ${String(t)}">${t}</span>`
    ).join("")}</div>`;
  }

  function reviewEvidenceHtml(evidence) {
    const list = Array.isArray(evidence) ? evidence : [];
    if (!list.length) {
      return `<p class="muted" style="margin-top:0.5rem">No evidence recorded.</p>`;
    }
    return `<ul class="review-evidence" style="margin:0.35rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:0.8rem">
      ${list.map((e) => {
        const idBit = e && e.id != null ? `<span class="mono">#${e.id}</span> · ` : `<span class="mono">—</span> · `;
        const kind = e && e.kind ? e.kind : "?";
        const label = e && e.label ? e.label : "";
        const detail = e && e.detail ? trunc(String(e.detail), 160) : "";
        return `<li><span class="mono subtle">${kind}</span> ${idBit}<strong>${label}</strong> — ${detail}</li>`;
      }).join("")}
    </ul>`;
  }

  function reviewMixHtml(mix, agents) {
    const m = mix || {};
    const rows = Array.isArray(agents) ? agents : [];
    let table = "";
    if (rows.length) {
      table = `<table style="margin-top:0.5rem;font-size:0.85rem">
        <thead><tr><th>Agent</th><th>Dir</th><th>Conf</th><th>Signal</th></tr></thead>
        <tbody>
          ${rows.map((a) => `<tr>
            <td class="mono">${a.agent_id || "—"}</td>
            <td><span class="dir ${a.direction || ""}">${a.direction || "—"}</span></td>
            <td class="mono">${a.confidence != null ? Number(a.confidence).toFixed(1) : "—"}</td>
            <td class="mono subtle">${a.signal_id != null ? "#" + a.signal_id : "—"}</td>
          </tr>`).join("")}
        </tbody>
      </table>`;
    }
    const lead = m.lead_agent ? `lead=${m.lead_agent}` : "lead=—";
    const split = m.disagreement ? "split_book" : "aligned";
    return `<p class="muted" style="margin-top:0.35rem">${lead} · ${split} · L[${(m.long || []).join(",") || "—"}] S[${(m.short || []).join(",") || "—"}] N[${(m.neutral || []).join(",") || "—"}]</p>${table}`;
  }

  function reviewBlockHtml(review, { heading } = {}) {
    if (!review || typeof review !== "object") {
      return `<p class="muted" style="margin-top:1rem">No trade review for this close.</p>`;
    }
    const path = review.path || {};
    const title = heading || "Review";
    const completeness = review.completeness || "—";
    return `
      <p class="kicker" style="margin-top:1rem">${title}
        <span class="chip review-completeness ${completeness}" style="margin-left:0.5rem">${completeness}</span>
      </p>
      <p style="margin-top:0.4rem">${review.verdict || "—"}</p>
      ${reviewTagsHtml(review.verdict_tags)}
      <div class="grid-stats" style="margin-top:0.75rem">
        <div class="stat"><p class="label">Exit reason</p><p class="value mono" style="font-size:0.85rem">${review.exit_reason || "—"}</p></div>
        <div class="stat"><p class="label">Hold h</p><p class="value mono">${review.hold_hours != null ? Number(review.hold_hours).toFixed(1) : "—"}</p></div>
        <div class="stat"><p class="label">Ret %</p><p class="value mono ${pnlClass(path.ret_pct)}">${path.ret_pct != null ? Number(path.ret_pct).toFixed(2) + "%" : "—"}</p></div>
        <div class="stat"><p class="label">MFE %</p><p class="value mono">${path.mfe_pct != null ? Number(path.mfe_pct).toFixed(2) + "%" : "—"}</p></div>
        <div class="stat"><p class="label">MAE %</p><p class="value mono">${path.mae_pct != null ? Number(path.mae_pct).toFixed(2) + "%" : "—"}</p></div>
        <div class="stat"><p class="label">PnL</p><p class="value mono ${pnlClass(review.pnl)}">${fmtUsd(review.pnl)}</p></div>
      </div>
      <p class="kicker" style="margin-top:0.85rem">Agent mix at entry</p>
      ${reviewMixHtml(review.mix, review.agents_at_entry)}
      <p class="kicker" style="margin-top:0.85rem">Evidence</p>
      ${reviewEvidenceHtml(review.evidence)}`;
  }



  function zonePointForTicker(ticker) {
    const want = tickerKey(ticker);
    const pts = DATA?.zones?.points || [];
    return pts.find((p) => tickerKey(p.ticker) === want) || null;
  }

  function zoneSeriesForTicker(ticker) {
    const want = tickerKey(ticker);
    const days = (zonesHistory?.days || DATA?.zones_series?.days || []);
    const out = [];
    days.forEach((d) => {
      const pt = (d.points || []).find((p) => tickerKey(p.ticker) === want);
      if (pt) out.push({ day: d.day, buy: Number(pt.buy) || 0, sell: Number(pt.sell) || 0, zone: pt.zone });
    });
    return out;
  }

  function zoneBadgeHtml(zone) {
    const z = zone || "quiet";
    const label = String(z).replace(/_/g, " ");
    return `<span class="zone-badge ${z}">${label}</span>`;
  }

  function zoneNiceMax(vals, padRatio = 1.2, floor = 0.1) {
    const nums = (vals || []).map((v) => Number(v) || 0);
    const m = nums.length ? Math.max(0, ...nums) : 0;
    if (m <= 0) return floor;
    const raw = Math.max(m * padRatio, m + 0.04);
    const step = raw <= 0.35 ? 0.05 : 0.1;
    return Math.ceil(raw / step) * step;
  }

  function drawZoneHistoryChart(series) {
    if (!series.length) {
      return `<div class="empty" style="padding:1rem">No zone history for this ticker yet</div>`;
    }
    const w = 520;
    const h = 160;
    const pad = { t: 16, r: 12, b: 28, l: 40 };
    const yMax = zoneNiceMax(series.flatMap((p) => [p.buy, p.sell]));
    const x = (i) => pad.l + (i / Math.max(series.length - 1, 1)) * (w - pad.l - pad.r);
    const y = (v) => pad.t + (1 - Math.min(Math.max(Number(v) || 0, 0), yMax) / yMax) * (h - pad.t - pad.b);
    const buyPts = series.map((p, i) => `${x(i).toFixed(1)},${y(p.buy).toFixed(1)}`).join(" ");
    const sellPts = series.map((p, i) => `${x(i).toFixed(1)},${y(p.sell).toFixed(1)}`).join(" ");
    const first = series[0].day.slice(5);
    const last = series[series.length - 1].day.slice(5);
    const thresh =
      0.35 <= yMax
        ? `<line x1="${pad.l}" x2="${w - pad.r}" y1="${y(0.35)}" y2="${y(0.35)}" stroke="#232a32" stroke-dasharray="3 4"/>`
        : "";
    return `<div class="zone-chart"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Buy and sell pressure history">
      ${thresh}
      <text x="4" y="${y(yMax) + 3}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono">${yMax.toFixed(2)}</text>
      <text x="4" y="${y(0) + 3}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono">0</text>
      <polyline points="${buyPts}" fill="none" stroke="#8aa58f" stroke-width="1.6"/>
      <polyline points="${sellPts}" fill="none" stroke="#c48980" stroke-width="1.6"/>
      <text x="${pad.l}" y="${h - 8}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono">${first}</text>
      <text x="${w - pad.r}" y="${h - 8}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono" text-anchor="end">${last}</text>
      <text x="${pad.l + 4}" y="12" fill="#8aa58f" font-size="10" font-family="IBM Plex Mono">buy</text>
      <text x="${pad.l + 40}" y="12" fill="#c48980" font-size="10" font-family="IBM Plex Mono">sell</text>
    </svg></div>`;
  }

  function agentMixTableHtml(agents) {
    const rows = agents || [];
    if (!rows.length) return `<p class="muted">No agent mix for this ticker today.</p>`;
    return `<table class="agent-mix-table">
      <thead><tr><th>Agent</th><th>Dir</th><th>Conf</th></tr></thead>
      <tbody>
        ${rows
          .map(
            (a) => `<tr>
              <td class="mono muted">${a.agent_id}</td>
              <td><span class="dir ${a.direction}">${a.direction}</span></td>
              <td class="mono">${Number(a.confidence).toFixed(1)}</td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
  }

  function zoneDetailBlock(ticker) {
    const pt = zonePointForTicker(ticker);
    const series = zoneSeriesForTicker(ticker);
    if (!pt && !series.length) {
      return `<p class="kicker" style="margin-top:1rem">Zones</p>
        <p class="muted">No zone score for this ticker yet.</p>`;
    }
    const badge = pt ? zoneBadgeHtml(pt.zone) : "";
    const nums = pt
      ? `<div class="grid-stats" style="margin-top:0.75rem">
          <div class="stat"><p class="label">Buy</p><p class="value mono gain">${Number(pt.buy).toFixed(3)}</p></div>
          <div class="stat"><p class="label">Sell</p><p class="value mono loss">${Number(pt.sell).toFixed(3)}</p></div>
          <div class="stat"><p class="label">Net</p><p class="value mono">${Number(pt.net).toFixed(3)}</p></div>
          <div class="stat"><p class="label">Zone</p><p class="value">${badge}</p></div>
        </div>`
      : "";
    return `<p class="kicker" style="margin-top:1rem">Zones</p>
      ${nums}
      ${drawZoneHistoryChart(series)}
      <p class="kicker" style="margin-top:0.75rem">Agent mix (today)</p>
      ${agentMixTableHtml(pt?.agents || [])}`;
  }

  function openZoneDetail(pt) {
    const series = zoneSeriesForTicker(pt.ticker);
    openDialogHtml(`
      <p class="kicker">Zones</p>
      <h3>${pt.ticker} · <span class="muted">${pt.sector || "—"}</span> ${zoneBadgeHtml(pt.zone)}</h3>
      <p class="muted">${pt.held ? "Held in open book" : "Watchlist only"}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Buy pressure</p><p class="value mono gain">${Number(pt.buy).toFixed(3)}</p></div>
        <div class="stat"><p class="label">Sell pressure</p><p class="value mono loss">${Number(pt.sell).toFixed(3)}</p></div>
        <div class="stat"><p class="label">Net</p><p class="value mono">${Number(pt.net != null ? pt.net : pt.buy - pt.sell).toFixed(3)}</p></div>
        ${pt.weight != null ? `<div class="stat"><p class="label">Weight</p><p class="value mono">${(Number(pt.weight) * 100).toFixed(1)}%</p></div>` : ""}
      </div>
      <p class="kicker" style="margin-top:1rem">History</p>
      ${drawZoneHistoryChart(series)}
      <p class="kicker" style="margin-top:0.75rem">Agent mix (today)</p>
      ${agentMixTableHtml(pt.agents || [])}`);
  }

  function openDialogHtml(html) {
    const dlg = $("dialog");
    const body = $("dialogBody");
    body.innerHTML = html;
    dlg.hidden = false;
    dlg.classList.add("open");
  }

  function bindRowActivation(tr, onActivate) {
    tr.classList.add("clickable");
    tr.tabIndex = 0;
    tr.setAttribute("role", "button");
    tr.addEventListener("click", onActivate);
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onActivate();
      }
    });
  }

  function sectorOf(ticker) {
    const sectors = DATA?.sectors || {};
    for (const [sec, list] of Object.entries(sectors)) {
      if ((list || []).map((x) => String(x).toUpperCase()).includes(String(ticker).toUpperCase())) {
        return sec;
      }
    }
    return null;
  }

  function matchesSector(sec) {
    return filter === "all" || sec === filter;
  }

  function setSectorFilter(key) {
    filter = SECTORS.includes(key) ? key : "all";
    setHeroArt();
    renderAll();
  }

  function setHeroArt() {
    const img = $("heroArt");
    if (!img) return;
    const src = ART[filter] || ART.all;
    img.onerror = () => { if (img.src.indexOf("observatory") === -1) img.src = ART.all; };
    img.src = src;
  }

  function renderFilters() {
    const el = $("filters");
    el.innerHTML = "";
    SECTORS.forEach((key) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip sector";
      btn.textContent = key === "all" ? "All" : key;
      btn.setAttribute("aria-pressed", filter === key ? "true" : "false");
      btn.addEventListener("click", () => {
        setSectorFilter(key);
      });
      el.appendChild(btn);
    });
  }

  function renderHeroBadges() {
    const snap = DATA.snapshot || {};
    const meta = DATA.meta || {};
    const el = $("heroBadges");
    const bits = [
      `<span class="chip paper">PAPER</span>`,
      `<span class="chip">START ${fmtUsd(meta.starting_equity)}</span>`,
      snap.halted || snap.day_halted
        ? `<span class="chip halt">HALTED</span>`
        : `<span class="chip active">LIVE BOOK</span>`,
    ];
    el.innerHTML = bits.join("");
  }

  function renderSnapshot() {
    const s = DATA.snapshot || {};
    const meta = DATA.meta || {};
    const dayBit = meta.day_local
      ? ` · ${meta.day_label || "ET day"} ${meta.day_local}`
      : "";
    $("exportedAt").textContent = `Exported ${fmtTime(meta.exported_at)} · account ${meta.account || "paper"}${dayBit}`;
    const cards = [
      { label: "Equity", value: fmtUsd(s.equity) },
      { label: "Cash", value: fmtUsd(s.cash) },
      { label: "Unrealized", value: fmtUsd(s.unrealized), cls: pnlClass(s.unrealized) },
      { label: "Daily PnL", value: fmtUsd(s.daily_pnl), cls: pnlClass(s.daily_pnl) },
      { label: "Drawdown", value: fmtPct(s.drawdown), cls: Number(s.drawdown) > 0 ? "loss" : "" },
    ];
    $("statGrid").innerHTML = cards
      .map(
        (c) => `<div class="stat"><p class="label">${c.label}</p><p class="value ${c.cls || ""}">${c.value}</p></div>`
      )
      .join("");
    drawEquityChart(DATA.equity_curve || []);
  }

  function drawEquityChart(curve) {
    const host = $("equityChart");
    if (!curve.length) {
      host.innerHTML = `<div class="empty">No portfolio snapshots yet</div>`;
      return;
    }
    const w = 720;
    const h = 220;
    const pad = { t: 16, r: 12, b: 28, l: 48 };
    const vals = curve.map((p) => Number(p.equity));
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = max - min || 1;
    const x = (i) => pad.l + (i / Math.max(curve.length - 1, 1)) * (w - pad.l - pad.r);
    const y = (v) => pad.t + (1 - (v - min) / span) * (h - pad.t - pad.b);
    const pts = curve.map((p, i) => `${x(i).toFixed(1)},${y(Number(p.equity)).toFixed(1)}`);
    const line = pts.join(" ");
    const area = `${pad.l},${h - pad.b} ${line} ${w - pad.r},${h - pad.b}`;
    const yTicks = [min, min + span / 2, max];
    host.innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Equity curve">
        <defs>
          <linearGradient id="navFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#8fb4c4" stop-opacity="0.28"/>
            <stop offset="100%" stop-color="#8fb4c4" stop-opacity="0"/>
          </linearGradient>
        </defs>
        ${yTicks
          .map(
            (t) =>
              `<text x="8" y="${y(t) + 3}" fill="#5c656e" font-size="11" font-family="IBM Plex Mono">${fmtUsd(t)}</text>
               <line x1="${pad.l}" x2="${w - pad.r}" y1="${y(t)}" y2="${y(t)}" stroke="#232a32" stroke-dasharray="3 4"/>`
          )
          .join("")}
        <polygon points="${area}" fill="url(#navFill)"/>
        <polyline points="${line}" fill="none" stroke="#8fb4c4" stroke-width="1.5"/>
        <text x="${pad.l}" y="${h - 8}" fill="#5c656e" font-size="11" font-family="IBM Plex Mono">${fmtTime(curve[0].t)}</text>
        <text x="${w - pad.r}" y="${h - 8}" fill="#5c656e" font-size="11" font-family="IBM Plex Mono" text-anchor="end">${fmtTime(curve[curve.length - 1].t)}</text>
      </svg>`;
  }

  function renderHoldings() {
    const rows = (DATA.holdings || []).filter((h) => matchesSector(h.sector || sectorOf(h.ticker)));
    if (!rows.length) {
      $("holdingsTable").innerHTML = `<div class="empty">No open holdings${filter !== "all" ? " in this sector" : ""}</div>`;
      return;
    }
    $("holdingsTable").innerHTML = `
      <table>
        <thead><tr><th>Ticker</th><th>Sector</th><th>Qty</th><th>Avg cost</th><th>Last</th><th>Unrealized</th></tr></thead>
        <tbody>
          ${rows
            .map(
              (h, i) => `<tr data-hold-idx="${i}" aria-label="Open details for ${h.ticker}">
                <td class="mono">${h.ticker}</td>
                <td class="muted">${h.sector || "—"}</td>
                <td class="mono">${Number(h.qty).toFixed(4)}</td>
                <td class="mono">${fmtUsd(h.avg_cost)}</td>
                <td class="mono">${fmtUsd(h.last)}</td>
                <td class="mono ${pnlClass(h.unrealized)}">${fmtUsd(h.unrealized)}</td>
              </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
    $("holdingsTable").querySelectorAll("tr[data-hold-idx]").forEach((tr) => {
      const idx = Number(tr.getAttribute("data-hold-idx"));
      bindRowActivation(tr, () => openHoldingDetail(rows[idx]));
    });
  }

  function renderClosed() {
    const host = $("closedTable");
    if (!host) return;
    // UI-only window: full DATA.closed stays intact for Lifetime / detail helpers.
    const recent = closedRowsForRecentWindow(DATA.closed || [], matchesSector);
    if (!recent.length) {
      const sectorBit = filter !== "all" ? " in this sector" : "";
      host.innerHTML = `<div class="empty">No closes today or yesterday (ET)${sectorBit}. Full history is in Lifetime.</div>`;
      return;
    }
    const { today, yesterday } = etTodayYesterday();
    const groups = [
      { key: "today", label: `Today · ${today} ET`, items: recent.filter((x) => x.bucket === "today") },
      { key: "yesterday", label: `Yesterday · ${yesterday} ET`, items: recent.filter((x) => x.bucket === "yesterday") },
    ].filter((g) => g.items.length);

    const ordered = [];
    const bodyHtml = groups
      .map((g) => {
        const head = `<tr class="day-group" aria-hidden="true"><td colspan="8">${g.label}</td></tr>`;
        const rows = g.items
          .map((x) => {
            const i = ordered.length;
            const c = x.row;
            ordered.push(c);
            return `<tr data-closed-idx="${i}" aria-label="Open closed trade details for ${c.ticker}">
                <td class="mono">${c.ticker}</td>
                <td class="muted">${c.sector || "—"}</td>
                <td class="mono">${c.qty != null ? Number(c.qty).toFixed(4) : "—"}</td>
                <td class="mono">${fmtUsd(c.entry)}</td>
                <td class="mono">${fmtUsd(c.exit)}</td>
                <td class="mono ${pnlClass(c.pnl)}">${fmtUsd(c.pnl)}</td>
                <td class="mono subtle">${fmtTime(c.closed_at)}</td>
                <td class="mono muted">${c.mode || "—"}</td>
              </tr>`;
          })
          .join("");
        return head + rows;
      })
      .join("");

    host.innerHTML = `
      <table>
        <thead><tr><th>Ticker</th><th>Sector</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Realized</th><th>Closed</th><th>Mode</th></tr></thead>
        <tbody>
          ${bodyHtml}
        </tbody>
      </table>`;
    host.querySelectorAll("tr[data-closed-idx]").forEach((tr) => {
      const idx = Number(tr.getAttribute("data-closed-idx"));
      bindRowActivation(tr, () => openClosedDetail(ordered[idx]));
    });
  }

  function openHoldingDetail(h) {
    const mv = marketValue(h.qty, h.last);
    const pct = unrealizedPct(h.unrealized, h.qty, h.avg_cost);
    const sigs = signalsForTicker(h.ticker);
    const decisions = decisionsForTicker(h.ticker);
    const mission = missionForTicker(h.ticker);
    const closed = closedFillsForTicker(h.ticker);
    const fills = (Array.isArray(h.fills) ? h.fills : []).slice(0, 8);
    const latestSig = sigs[0];
    const latestDec = decisions[0];

    let researchHtml = `<p class="muted">No research signals for this ticker in today's feed.</p>`;
    if (latestSig) {
      researchHtml = `
        <p><span class="mono muted">${latestSig.agent_id || "agent"}</span>
          · <span class="dir ${latestSig.direction}">${latestSig.direction}</span>
          · <span class="mono">${Number(latestSig.confidence).toFixed(1)}</span>
          · <span class="subtle">${fmtTime(latestSig.created_at)}</span></p>
        <p style="margin-top:0.5rem">${latestSig.reasoning || "—"}</p>
        ${latestSig.supporting_data ? `<pre>${JSON.stringify(latestSig.supporting_data, null, 2)}</pre>` : ""}`;
    }

    let decisionHtml = `<p class="muted">No decisions for this ticker in today's feed.</p>`;
    if (latestDec) {
      decisionHtml = `
        <p><span class="mono">${latestDec.decision_type || "decision"}</span>
          · <span class="muted">${latestDec.actor || "—"}</span>
          · <span class="subtle">${fmtTime(latestDec.created_at)}</span></p>
        <p style="margin-top:0.5rem">${latestDec.reasoning || "—"}</p>`;
    }

    let fillsHtml = "";
    if (fills.length) {
      fillsHtml = `<p class="kicker" style="margin-top:1rem">Position fills</p>
        <pre>${JSON.stringify(fills, null, 2)}</pre>`;
    } else if (closed.length) {
      fillsHtml = `<p class="kicker" style="margin-top:1rem">Related closed sells</p>
        <ul style="margin:0.35rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:0.85rem">
          ${closed.slice(0, 5).map((c) =>
            `<li class="mono">${fmtTime(c.closed_at)} · qty ${c.qty != null ? Number(c.qty).toFixed(4) : "—"} · exit ${fmtUsd(c.exit)} · pnl ${fmtUsd(c.pnl)}</li>`
          ).join("")}
        </ul>`;
    }

    let missionHtml = "";
    if (mission.length) {
      missionHtml = `<p class="kicker" style="margin-top:1rem">Mission notes</p>
        <ul style="margin:0.35rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:0.85rem">
          ${mission.map((e) =>
            `<li><span class="mono">${e.type || "event"}</span> · ${fmtTime(e.at)} — ${trunc(e.summary || "", 160)}</li>`
          ).join("")}
        </ul>`;
    }

    openDialogHtml(`
      <p class="kicker">Open book</p>
      <h3>${h.ticker} · <span class="muted">${h.sector || "—"}</span></h3>
      <p class="muted">Updated ${fmtTime(h.updated_at)}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Qty</p><p class="value mono">${h.qty != null ? Number(h.qty).toFixed(4) : "—"}</p></div>
        <div class="stat"><p class="label">Avg cost</p><p class="value mono">${fmtUsd(h.avg_cost)}</p></div>
        <div class="stat"><p class="label">Last</p><p class="value mono">${fmtUsd(h.last)}</p></div>
        <div class="stat"><p class="label">Market value</p><p class="value mono">${fmtUsd(mv)}</p></div>
        <div class="stat"><p class="label">Unrealized $</p><p class="value mono ${pnlClass(h.unrealized)}">${fmtUsd(h.unrealized)}</p></div>
        <div class="stat"><p class="label">Unrealized %</p><p class="value mono ${pnlClass(h.unrealized)}">${pct == null ? "—" : (pct * 100).toFixed(2) + "%"}</p></div>
      </div>
      <p class="kicker" style="margin-top:1rem">Latest research</p>
      ${researchHtml}
      <p class="kicker" style="margin-top:1rem">Latest decision</p>
      ${decisionHtml}
      ${fillsHtml}
      ${missionHtml}
      ${zoneDetailBlock(h.ticker)}`);
  }

  function openClosedDetail(c) {
    const sigs = signalsForTicker(c.ticker);
    const decisions = decisionsForTicker(c.ticker);
    const latestSig = sigs[0];
    const related = decisions.filter((d) => {
      const t = String(d.decision_type || "").toLowerCase();
      return t.includes("fill") || t.includes("order") || t === "ticket";
    }).slice(0, 5);

    let extra = "";
    if (latestSig) {
      extra += `<p class="kicker" style="margin-top:1rem">Latest research (same ticker)</p>
        <p><span class="mono muted">${latestSig.agent_id || "agent"}</span>
          · <span class="dir ${latestSig.direction}">${latestSig.direction}</span>
          · <span class="subtle">${fmtTime(latestSig.created_at)}</span></p>
        <p style="margin-top:0.35rem">${latestSig.reasoning || "—"}</p>`;
    }
    if (related.length) {
      extra += `<p class="kicker" style="margin-top:1rem">Related decisions</p>
        <ul style="margin:0.35rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:0.85rem">
          ${related.map((d) =>
            `<li><span class="mono">${d.decision_type}</span> · ${fmtTime(d.created_at)} — ${trunc(d.reasoning || "", 140)}</li>`
          ).join("")}
        </ul>`;
    }
    if (c.buy || c.sell) {
      extra += `<p class="kicker" style="margin-top:1rem">Linked legs</p>
        <pre>${JSON.stringify({ buy: c.buy || null, sell: c.sell || null }, null, 2)}</pre>`;
    }

    const review = c.review || null;
    openDialogHtml(`
      <p class="kicker">Closed book</p>
      <h3>${c.ticker} · <span class="muted">${c.sector || "—"}</span></h3>
      <p class="muted">Closed ${fmtTime(c.closed_at)} · mode ${c.mode || "—"}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Qty</p><p class="value mono">${c.qty != null ? Number(c.qty).toFixed(4) : "—"}</p></div>
        <div class="stat"><p class="label">Entry</p><p class="value mono">${fmtUsd(c.entry)}</p></div>
        <div class="stat"><p class="label">Exit</p><p class="value mono">${fmtUsd(c.exit)}</p></div>
        <div class="stat"><p class="label">Realized P/L</p><p class="value mono ${pnlClass(c.pnl)}">${fmtUsd(c.pnl)}</p></div>
      </div>
      ${reviewBlockHtml(review)}
      ${extra || `<p class="muted" style="margin-top:1rem">No additional reasoning or linked fill payloads in this feed.</p>`}`);
  }

  function drawLifetimeCumChart(series) {
    if (!series || series.length < 2) {
      return `<p class="muted" style="margin-top:0.75rem">Chart needs 2+ realized exits</p>`;
    }
    const w = 520;
    const h = 160;
    const pad = { t: 16, r: 12, b: 28, l: 48 };
    const vals = series.map((p) => Number(p.cum) || 0);
    const lo = Math.min(0, ...vals);
    const hi = Math.max(0, ...vals);
    const span = hi - lo || 1;
    const x = (i) => pad.l + (i / Math.max(series.length - 1, 1)) * (w - pad.l - pad.r);
    const y = (v) => pad.t + (1 - (v - lo) / span) * (h - pad.t - pad.b);
    const pts = series.map((p, i) => `${x(i).toFixed(1)},${y(Number(p.cum) || 0).toFixed(1)}`).join(" ");
    const end = vals[vals.length - 1];
    const stroke = end > 0 ? "#8aa58f" : end < 0 ? "#c48980" : "#8b949e";
    const zeroY = y(0);
    const first = String(series[0].at || "").slice(0, 10);
    const last = String(series[series.length - 1].at || "").slice(0, 10);
    return `<div class="zone-chart"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Cumulative realized PnL">
      <line x1="${pad.l}" x2="${w - pad.r}" y1="${zeroY}" y2="${zeroY}" stroke="#232a32" stroke-dasharray="3 4"/>
      <text x="4" y="${y(hi) + 3}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono">${hi.toFixed(2)}</text>
      <text x="4" y="${y(lo) + 3}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono">${lo.toFixed(2)}</text>
      <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="1.8"/>
      <text x="${pad.l}" y="${h - 8}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono">${first}</text>
      <text x="${w - pad.r}" y="${h - 8}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono" text-anchor="end">${last}</text>
      <text x="${pad.l + 4}" y="12" fill="${stroke}" font-size="10" font-family="IBM Plex Mono">cum realized</text>
    </svg></div>`;
  }

  function renderLifetime() {
    const host = $("lifetimeTable");
    if (!host) return;
    const rows = (DATA.ticker_pnl || []).filter((r) =>
      matchesSector(r.sector || sectorOf(r.ticker))
    );
    if (!rows.length) {
      host.innerHTML = `<div class="empty">No lifetime ticker PnL${filter !== "all" ? " in this sector" : ""}</div>`;
      return;
    }
    host.innerHTML = `
      <table>
        <thead><tr><th>Ticker</th><th>Sector</th><th>Realized</th><th>Unrealized</th><th>Total</th><th>Closes</th><th>Last activity</th></tr></thead>
        <tbody>
          ${rows
            .map(
              (r, i) => `<tr data-life-idx="${i}" aria-label="Open lifetime PnL for ${r.ticker}">
                <td class="mono">${r.ticker}</td>
                <td class="muted">${r.sector || "—"}</td>
                <td class="mono ${pnlClass(r.realized)}">${fmtUsd(r.realized)}</td>
                <td class="mono ${pnlClass(r.unrealized)}">${fmtUsd(r.unrealized)}</td>
                <td class="mono ${pnlClass(r.total)}">${fmtUsd(r.total)}</td>
                <td class="mono">${r.close_events != null ? r.close_events : "—"}</td>
                <td class="mono subtle">${fmtTime(r.last_activity_at)}</td>
              </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
    host.querySelectorAll("tr[data-life-idx]").forEach((tr) => {
      const idx = Number(tr.getAttribute("data-life-idx"));
      bindRowActivation(tr, () => openLifetimeDetail(rows[idx]));
    });
  }

  function openLifetimeDetail(r) {
    const series = Array.isArray(r.series) ? r.series : [];
    const reviews = reviewsForTicker(r.ticker);
    const lastReview = reviews[0] || null;
    const reviewSummary = r.last_verdict
      ? `<p class="muted" style="margin-top:0.75rem">Reviews: ${r.review_count != null ? r.review_count : reviews.length} · last completeness ${r.last_completeness || (lastReview && lastReview.completeness) || "—"}</p>`
      : (reviews.length ? `<p class="muted" style="margin-top:0.75rem">Reviews: ${reviews.length}</p>` : "");
    openDialogHtml(`
      <p class="kicker">Lifetime book</p>
      <h3>${r.ticker} · <span class="muted">${r.sector || "—"}</span></h3>
      <p class="muted">First buy ${fmtTime(r.first_buy_at)} · last activity ${fmtTime(r.last_activity_at)}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Realized</p><p class="value mono ${pnlClass(r.realized)}">${fmtUsd(r.realized)}</p></div>
        <div class="stat"><p class="label">Unrealized</p><p class="value mono ${pnlClass(r.unrealized)}">${fmtUsd(r.unrealized)}</p></div>
        <div class="stat"><p class="label">Total</p><p class="value mono ${pnlClass(r.total)}">${fmtUsd(r.total)}</p></div>
        <div class="stat"><p class="label">Closes</p><p class="value mono">${r.close_events != null ? r.close_events : "—"}</p></div>
        <div class="stat"><p class="label">Buys</p><p class="value mono">${r.buy_events != null ? r.buy_events : "—"}</p></div>
        <div class="stat"><p class="label">Qty open</p><p class="value mono">${r.qty_open != null ? Number(r.qty_open).toFixed(4) : "—"}</p></div>
        <div class="stat"><p class="label">Avg cost open</p><p class="value mono">${fmtUsd(r.avg_cost_open)}</p></div>
      </div>
      <p class="kicker" style="margin-top:1rem">Cumulative realized</p>
      ${drawLifetimeCumChart(series)}
      ${reviewSummary}
      ${reviewBlockHtml(lastReview, { heading: "Latest close review" })}`);
  }

  function renderAgents() {
    const agents = DATA.agents_today || {};
    const host = $("agentGrid");
    host.innerHTML = "";
    AGENT_ORDER.forEach((aid) => {
      const block = agents[aid] || { agent_id: aid, count: 0, signals: [], long: 0, short: 0, neutral: 0 };
      let signals = block.signals || [];
      if (filter !== "all") {
        signals = signals.filter((s) => matchesSector(sectorOf(s.ticker)));
      }
      const card = document.createElement("article");
      card.className = "agent-card";
      card.innerHTML = `
        <header>
          <div>
            <h3>${aid.replace(/_/g, " ")}</h3>
            <div class="counts">${signals.length} shown · ${block.count || 0} today · L${block.long || 0}/S${block.short || 0}/N${block.neutral || 0}</div>
          </div>
        </header>`;
      const list = document.createElement("ul");
      list.className = "signal-list";
      if (!signals.length) {
        list.innerHTML = `<li class="empty" style="padding:1rem">No signals</li>`;
      } else {
        signals.slice(0, 12).forEach((s) => {
          const li = document.createElement("li");
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "signal-item";
          btn.innerHTML = `
            <div class="signal-top">
              <span class="mono">${s.ticker}</span>
              <span>
                <span class="dir ${s.direction}">${s.direction}</span>
                <span class="mono subtle"> ${Number(s.confidence).toFixed(0)}</span>
              </span>
            </div>
            <p class="signal-reason">${trunc(s.reasoning, 120)}</p>`;
          btn.addEventListener("click", () => openDetail(s, aid));
          li.appendChild(btn);
          list.appendChild(li);
        });
      }
      card.appendChild(list);
      host.appendChild(card);
    });
  }

  function flattenSignals() {
    const agents = DATA.agents_today || {};
    const out = [];
    Object.values(agents).forEach((block) => {
      (block.signals || []).forEach((s) => {
        out.push({ ...s, agent_id: block.agent_id || s.agent_id });
      });
    });
    return out;
  }

  function renderResearch() {
    const d = DATA.decisions_today || {};
    const c = d.counts || {};
    $("decisionStats").innerHTML = [
      ["Tickets", c.tickets || 0],
      ["Holds", c.holds || 0],
      ["Fills", c.fills || 0],
      ["Failed", c.failed || 0],
    ]
      .map(
        ([label, v]) =>
          `<div class="stat"><p class="label">${label}</p><p class="value">${v}</p></div>`
      )
      .join("");

    let signals = flattenSignals().filter((s) => matchesSector(sectorOf(s.ticker)));
    signals = signals.slice(0, 40);
    if (!signals.length) {
      $("researchTable").innerHTML = `<div class="empty">No research signals for filter</div>`;
    } else {
      $("researchTable").innerHTML = `
        <table>
          <thead><tr><th>Agent</th><th>Ticker</th><th>Dir</th><th>Conf</th><th>Reasoning</th></tr></thead>
          <tbody>
            ${signals
              .map(
                (s) => `<tr class="clickable" data-sid="${s.signal_id || ""}">
                  <td class="mono muted">${s.agent_id}</td>
                  <td class="mono">${s.ticker}</td>
                  <td><span class="dir ${s.direction}">${s.direction}</span></td>
                  <td class="mono">${Number(s.confidence).toFixed(0)}</td>
                  <td class="muted">${trunc(s.reasoning, 100)}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
      $("researchTable").querySelectorAll("tr.clickable").forEach((tr, i) => {
        tr.addEventListener("click", () => openDetail(signals[i], signals[i].agent_id));
      });
    }

    const flags = (DATA.auditor_flags_today || []).filter((f) => {
      if (filter === "all") return true;
      const m = String(f.message || "");
      return (DATA.sectors?.[filter] || []).some((t) => m.includes(t));
    });
    if (!flags.length) {
      $("flagsTable").innerHTML = `<div class="empty">No auditor flags today</div>`;
    } else {
      $("flagsTable").innerHTML = `
        <table>
          <thead><tr><th>Severity</th><th>Type</th><th>Message</th><th>When</th></tr></thead>
          <tbody>
            ${flags
              .slice(0, 30)
              .map(
                (f) => `<tr>
                  <td class="mono ${f.severity === "warning" || f.severity === "critical" ? "loss" : ""}">${f.severity}</td>
                  <td class="mono muted">${f.flag_type}</td>
                  <td>${trunc(f.message, 120)}</td>
                  <td class="mono subtle">${fmtTime(f.created_at)}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
    }
  }

  function renderMission() {
    const log = (DATA.mission_log || []).filter((e) => {
      if (filter === "all") return true;
      if (!e.ticker) return true;
      return matchesSector(sectorOf(e.ticker));
    });
    if (!log.length) {
      $("missionLog").innerHTML = `<li class="empty">No mission events</li>`;
      return;
    }
    $("missionLog").innerHTML = log
      .slice(0, 40)
      .map(
        (e) => `<li>
          <div class="mono subtle">${fmtTime(e.at)}</div>
          <div>
            <div><span class="mono">${e.type}</span> · <span class="muted">${e.actor}</span>
              ${e.ticker ? ` · <span class="mono">${e.ticker}</span>` : ""}</div>
            <div class="muted" style="margin-top:0.25rem">${trunc(e.summary, 180)}</div>
          </div>
        </li>`
      )
      .join("");
  }

  function openWatchDetail(w) {
    const s = w.signal;
    let sigHtml = `<p class="muted">No top agent signal today.</p>`;
    if (s) {
      sigHtml = `<p><span class="mono muted">${s.agent_id || "agent"}</span>
        · <span class="dir ${s.direction}">${s.direction}</span>
        · <span class="mono">${Number(s.confidence).toFixed(1)}</span>
        · <span class="subtle">${fmtTime(s.created_at)}</span></p>
        <p style="margin-top:0.5rem">${s.reasoning || "—"}</p>`;
    }
    openDialogHtml(`
      <p class="kicker">Watchlist</p>
      <h3>${w.ticker} · <span class="muted">${w.sector || "—"}</span></h3>
      <p class="kicker" style="margin-top:1rem">Top signal</p>
      ${sigHtml}
      ${zoneDetailBlock(w.ticker)}`);
  }


  function renderZones() {
    const host = $("zonesScatter");
    const legend = $("zonesLegend");
    if (!host) return;
    const all = DATA?.zones?.points || [];
    const points = all.filter((p) => matchesSector(p.sector || sectorOf(p.ticker)));
    const counts = { buy_zone: 0, sell_zone: 0, conflicted: 0, quiet: 0 };
    all.forEach((p) => { counts[p.zone] = (counts[p.zone] || 0) + 1; });
    if (legend) {
      legend.innerHTML = [
        ["buy_zone", "Buy"],
        ["sell_zone", "Sell"],
        ["conflicted", "Conflicted"],
        ["quiet", "Quiet"],
      ]
        .map(([z, lab]) => `<span class="chip zone-badge ${z}">${lab} ${counts[z] || 0}</span>`)
        .join("") +
        `<span class="chip">held ● · watch ○</span>` +
        (filter !== "all" ? `<span class="chip">filter ${filter}: ${points.length}</span>` : "");
    }
    if (!points.length) {
      host.innerHTML = `<div class="empty">No zone points${filter !== "all" ? " for this sector" : ""}</div>`;
      return;
    }
    const w = 720;
    const h = 480;
    const pad = { t: 36, r: 28, b: 48, l: 56 };
    const xMax = zoneNiceMax(points.map((p) => p.sell));
    const yMax = zoneNiceMax(points.map((p) => p.buy));
    const xOf = (sell) => pad.l + (Math.min(Math.max(Number(sell) || 0, 0), xMax) / xMax) * (w - pad.l - pad.r);
    const yOf = (buy) => pad.t + (1 - Math.min(Math.max(Number(buy) || 0, 0), yMax) / yMax) * (h - pad.t - pad.b);
    // Zone threshold guides (0.35) only when they fall inside the fitted range
    const guides = [];
    if (0.35 <= xMax) {
      const xT = xOf(0.35);
      guides.push(`<line x1="${xT}" x2="${xT}" y1="${pad.t}" y2="${h - pad.b}" stroke="#232a32" stroke-dasharray="4 5"/>`);
    }
    if (0.35 <= yMax) {
      const yT = yOf(0.35);
      guides.push(`<line x1="${pad.l}" x2="${w - pad.r}" y1="${yT}" y2="${yT}" stroke="#232a32" stroke-dasharray="4 5"/>`);
    }
    const dots = points
      .map((p, i) => {
        const cx = xOf(p.sell).toFixed(1);
        const cy = yOf(p.buy).toFixed(1);
        const r = p.held ? 6.5 : 5;
        const fill = p.held ? "#8fb4c4" : "transparent";
        const stroke = p.zone === "buy_zone" ? "#8aa58f" : p.zone === "sell_zone" ? "#c48980" : p.zone === "conflicted" ? "#c4b08f" : "#5c656e";
        return `<g class="zones-scatter-point" data-zone-idx="${i}" tabindex="0" role="button" aria-label="${p.ticker} buy ${Number(p.buy).toFixed(2)} sell ${Number(p.sell).toFixed(2)}">
          <circle cx="${cx}" cy="${cy}" r="${r}" fill="${fill}" stroke="${stroke}" stroke-width="1.6"/>
          <title>${p.ticker} · buy ${Number(p.buy).toFixed(3)} · sell ${Number(p.sell).toFixed(3)} · ${p.zone}</title>
        </g>`;
      })
      .join("");
    host.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Buy vs sell pressure scatter">
      <rect x="${pad.l}" y="${pad.t}" width="${w - pad.l - pad.r}" height="${h - pad.t - pad.b}" fill="#0c1014" stroke="#232a32"/>
      ${guides.join("")}
      <text class="zones-quad-label" x="${pad.l + 8}" y="${pad.t + 16}">Buy</text>
      <text class="zones-quad-label" x="${w - pad.r - 8}" y="${pad.t + 16}" text-anchor="end">Conflicted</text>
      <text class="zones-quad-label" x="${pad.l + 8}" y="${h - pad.b - 8}">Quiet</text>
      <text class="zones-quad-label" x="${w - pad.r - 8}" y="${h - pad.b - 8}" text-anchor="end">Sell</text>
      <text x="${pad.l}" y="${h - 14}" fill="#5c656e" font-size="11" font-family="IBM Plex Mono">Sell → (fit 0–${xMax.toFixed(2)})</text>
      <text x="16" y="${pad.t + 4}" fill="#5c656e" font-size="11" font-family="IBM Plex Mono" transform="rotate(-90 16,${pad.t + 4})">Buy ↑ (0–${yMax.toFixed(2)})</text>
      <text x="${pad.l - 8}" y="${yOf(0) + 3}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono" text-anchor="end">0</text>
      <text x="${pad.l - 8}" y="${yOf(yMax) + 3}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono" text-anchor="end">${yMax.toFixed(2)}</text>
      <text x="${xOf(0)}" y="${h - pad.b + 18}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono" text-anchor="middle">0</text>
      <text x="${xOf(xMax)}" y="${h - pad.b + 18}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono" text-anchor="middle">${xMax.toFixed(2)}</text>
      ${dots}
    </svg>`;
    host.querySelectorAll("[data-zone-idx]").forEach((g) => {
      const idx = Number(g.getAttribute("data-zone-idx"));
      const activate = () => openZoneDetail(points[idx]);
      g.addEventListener("click", activate);
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate();
        }
      });
    });
  }

  function renderWatch() {
    const rows = (DATA.watchlist || []).filter((w) => matchesSector(w.sector || sectorOf(w.ticker)));
    if (!rows.length) {
      $("watchTable").innerHTML = `<div class="empty">Watchlist empty for filter</div>`;
      return;
    }
    $("watchTable").innerHTML = `
      <table>
        <thead><tr><th>Ticker</th><th>Sector</th><th>Top agent</th><th>Dir</th><th>Conf</th><th>Zone</th><th>Note</th></tr></thead>
        <tbody>
          ${rows
            .map((w, i) => {
              const s = w.signal;
              const zp = zonePointForTicker(w.ticker);
              return `<tr data-watch-idx="${i}" aria-label="Open details for ${w.ticker}">
                <td class="mono">${w.ticker}</td>
                <td class="muted">${w.sector || "—"}</td>
                <td class="mono muted">${s?.agent_id || "—"}</td>
                <td>${s ? `<span class="dir ${s.direction}">${s.direction}</span>` : "—"}</td>
                <td class="mono">${s ? Number(s.confidence).toFixed(0) : "—"}</td>
                <td>${zp ? zoneBadgeHtml(zp.zone) : `<span class="muted">—</span>`}</td>
                <td class="muted">${trunc(s?.reasoning || "", 90)}</td>
              </tr>`;
            })
            .join("")}
        </tbody>
      </table>`;
    $("watchTable").querySelectorAll("tr[data-watch-idx]").forEach((tr) => {
      const idx = Number(tr.getAttribute("data-watch-idx"));
      bindRowActivation(tr, () => openWatchDetail(rows[idx]));
    });
  }

  function openDetail(signal, agentId) {
    openDialogHtml(`
      <p class="kicker">${agentId || signal.agent_id || "agent"}</p>
      <h3>${signal.ticker} · <span class="dir ${signal.direction}">${signal.direction}</span>
        <span class="mono"> ${Number(signal.confidence).toFixed(1)}</span></h3>
      <p class="muted">${fmtTime(signal.created_at)}</p>
      <p style="margin-top:1rem">${signal.reasoning || ""}</p>
      <p class="kicker" style="margin-top:1rem">Supporting data</p>
      <pre>${JSON.stringify(signal.supporting_data || {}, null, 2)}</pre>`);
  }

  function closeDetail() {
    const dlg = $("dialog");
    dlg.classList.remove("open");
    dlg.hidden = true;
  }


  function renderSectors() {
    const host = $("sectorBoard");
    if (!host) return;
    const rows = DATA?.sector_board || [];
    if (!rows.length) {
      // Fallback: empty themes from sectors map so UI still shows structure
      const themes = Object.keys(DATA?.sectors || {});
      if (!themes.length) {
        host.innerHTML = `<div class="empty">No sector board in latest.json — re-export dashboard</div>`;
        return;
      }
    }
    const ordered = ["quantum", "space", "robotics"];
    const byTheme = Object.fromEntries(rows.map((r) => [r.theme, r]));
    const themes = ordered.filter((t) => byTheme[t] || (DATA?.sectors || {})[t]);
    // include any extra themes not in ordered
    for (const t of Object.keys(DATA?.sectors || {})) {
      if (!themes.includes(t)) themes.push(t);
    }
    const fmtPressure = (n) => (n == null || Number.isNaN(Number(n)) ? "—" : Number(n).toFixed(2));
    const barPct = (n) => Math.max(0, Math.min(100, Math.round(Math.abs(Number(n) || 0) * 100)));

    host.innerHTML = themes
      .map((theme) => {
        const r = byTheme[theme] || {
          theme,
          tickers_n: (DATA?.sectors?.[theme] || []).length,
          held_n: 0,
          weight: 0,
          unrealized: null,
          realized: null,
          buy: 0,
          sell: 0,
          net: 0,
          long: 0,
          short: 0,
          neutral: 0,
          zones: { buy_zone: 0, sell_zone: 0, conflicted: 0, quiet: 0 },
          returns: { "1d": null, "5d": null, "20d": null },
          returns_n: { "1d": 0, "5d": 0, "20d": 0 },
        };
        const pressed = filter === theme;
        const netCls = Number(r.net) > 0 ? "gain" : Number(r.net) < 0 ? "loss" : "flat";
        const z = r.zones || {};
        return `<button type="button" class="sector-card" data-theme="${theme}" aria-pressed="${pressed ? "true" : "false"}">
  <header>
    <h3>${theme}</h3>
    <span class="sector-meta">${r.held_n}/${r.tickers_n} held</span>
  </header>
  <div>
    <p class="sector-subhead">Pressure</p>
    <div class="pressure-bars">
      <div class="pressure-row"><span>Buy</span><div class="bar-track"><div class="bar-fill buy" style="width:${barPct(r.buy)}%"></div></div><span class="gain">${fmtPressure(r.buy)}</span></div>
      <div class="pressure-row"><span>Sell</span><div class="bar-track"><div class="bar-fill sell" style="width:${barPct(r.sell)}%"></div></div><span class="loss">${fmtPressure(r.sell)}</span></div>
      <div class="pressure-row"><span>Net</span><div class="bar-track"><div class="bar-fill net ${netCls}" style="width:${barPct(r.net)}%"></div></div><span class="${pnlClass(r.net)}">${fmtPressure(r.net)}</span></div>
    </div>
  </div>
  <div>
    <p class="sector-subhead">Tape (eq-wt)</p>
    <div class="sector-stats">
      <div class="stat"><p class="label">1d</p><p class="value mono ${pnlClass((r.returns || {})["1d"])}">${fmtSignedPct((r.returns || {})["1d"])}</p></div>
      <div class="stat"><p class="label">5d</p><p class="value mono ${pnlClass((r.returns || {})["5d"])}">${fmtSignedPct((r.returns || {})["5d"])}</p></div>
      <div class="stat"><p class="label">20d</p><p class="value mono ${pnlClass((r.returns || {})["20d"])}">${fmtSignedPct((r.returns || {})["20d"])}</p></div>
    </div>
  </div>
  <div>
    <p class="sector-subhead">Breadth</p>
    <div class="sector-stats">
      <div class="stat"><p class="label">Long</p><p class="value mono gain">${r.long ?? 0}</p></div>
      <div class="stat"><p class="label">Short</p><p class="value mono loss">${r.short ?? 0}</p></div>
      <div class="stat"><p class="label">Neutral</p><p class="value mono">${r.neutral ?? 0}</p></div>
    </div>
    <div class="sector-zones" style="margin-top:0.5rem">
      <span class="chip">buy ${z.buy_zone ?? 0}</span>
      <span class="chip">sell ${z.sell_zone ?? 0}</span>
      <span class="chip">conflict ${z.conflicted ?? 0}</span>
      <span class="chip">quiet ${z.quiet ?? 0}</span>
    </div>
  </div>
  <div>
    <p class="sector-subhead">Book</p>
    <div class="sector-stats">
      <div class="stat"><p class="label">Weight</p><p class="value mono">${fmtPct(r.weight)}</p></div>
      <div class="stat"><p class="label">Realized</p><p class="value mono ${pnlClass(r.realized)}">${fmtUsd(r.realized)}</p></div>
      <div class="stat"><p class="label">Unrealized</p><p class="value mono ${pnlClass(r.unrealized)}">${fmtUsd(r.unrealized)}</p></div>
      <div class="stat"><p class="label">Held</p><p class="value mono">${r.held_n}/${r.tickers_n}</p></div>
    </div>
  </div>
</button>`;
      })
      .join("");

    host.querySelectorAll(".sector-card").forEach((btn) => {
      btn.addEventListener("click", () => {
        const theme = btn.getAttribute("data-theme");
        setSectorFilter(filter === theme ? "all" : theme);
      });
    });
  }

  function renderAll() {
    renderFilters();
    renderHeroBadges();
    renderSnapshot();
    renderHoldings();
    renderClosed();
    renderLifetime();
    renderAgents();
    renderResearch();
    renderMission();
    renderSectors();
    renderZones();
    renderWatch();
  }

  function wireNav() {
    const links = document.querySelectorAll("#nav a");
    const sections = [...document.querySelectorAll("section[id]")];
    const onScroll = () => {
      let current = sections[0]?.id;
      for (const s of sections) {
        if (s.getBoundingClientRect().top <= 120) current = s.id;
      }
      links.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${current}`));
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  async function boot() {
    $("dialogClose").addEventListener("click", closeDetail);
    $("dialog").addEventListener("click", (e) => {
      if (e.target.id === "dialog") closeDetail();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDetail();
    });
    wireNav();
    try {
      const res = await fetch(DATA_URL, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status} loading ${DATA_URL}`);
      DATA = await res.json();
      try {
        const zh = await fetch(ZONES_HISTORY_URL, { cache: "no-store" });
        if (zh.ok) zonesHistory = await zh.json();
      } catch (_) {
        zonesHistory = DATA.zones_series || null;
      }
      if (!zonesHistory) zonesHistory = DATA.zones_series || null;
      setHeroArt();
      renderAll();
    } catch (err) {
      $("loadError").hidden = false;
      $("loadError").textContent =
        `Failed to load ./data/latest.json — run: python -m quant_system export-dashboard. (${err.message})`;
      $("exportedAt").textContent = "No data";
    }
  }

  boot();
})();
