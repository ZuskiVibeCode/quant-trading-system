/* Quant Mission Ledger — vanilla viewer for docs/data/latest.json */
(() => {
  const DATA_URL = "./data/latest.json";
  const ZONES_HISTORY_URL = "./data/zones_history.json";
  const AGENT_ORDER = [
    "momentum",
    "fundamental",
    "statistical_ml",
    "sentiment",
    "macro",
  ];
  const AGENT_LABEL = {
    momentum: "Momentum",
    fundamental: "Fundamental",
    statistical_ml: "Stat ML",
    sentiment: "Sentiment",
    macro: "Macro",
  };
  const ART = {
    quantum: "./art/quantum.jpg",
    robotics: "./art/robotics.jpg",
    space: "./art/space.jpg",
  };
  const THEME_COLOR = { robotics: "#8fb4c4", quantum: "#5f8597", space: "#46606f" };
  const EXTRA_THEME_COLORS = ["#7d9aa8", "#3f5866", "#9fb8c2"];
  const C = { gain: "#86d0a0", loss: "#ef9a86", accent: "#8fb4c4", muted: "#a3acb5", subtle: "#7d8790", line: "#1c2229", warn: "#e3c79a" };
  // Positions worth less than this are folded behind "Show N positions under $1".
  const SMALL_POSITION_USD = 1;

  let DATA = null;
  let zonesHistory = null;
  let filter = "all";
  let showSmall = false;
  let lastFocus = null;

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

  function fmtClock(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
    } catch {
      return iso;
    }
  }

  /** "2026-10-09" -> "Oct 9" (no timezone shift: the string is already an ET day). */
  function fmtDay(ymd, { weekday = false } = {}) {
    if (!ymd) return "—";
    const [Y, M, D] = String(ymd).slice(0, 10).split("-").map(Number);
    if (!Y || !M || !D) return String(ymd);
    const d = new Date(Date.UTC(Y, M - 1, D, 12));
    return d.toLocaleDateString("en-US", { timeZone: "UTC", month: "short", day: "numeric", ...(weekday ? { weekday: "short" } : {}) });
  }

  /** Signed money that stays readable at $100 scale: "+$1.32", "−7¢", "<1¢". */
  function fmtMoneySigned(n) {
    const v = safeNum(n);
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a < 0.005) return "<1¢";
    const sign = v < 0 ? "−" : "+";
    if (a < 1) return `${sign}${Math.round(a * 100)}¢`;
    return `${sign}$${a.toFixed(2)}`;
  }

  /** Unsigned money for prose: "55¢", "$2.98". */
  function fmtMoneyPlain(n) {
    const a = Math.abs(Number(n) || 0);
    if (a < 0.005) return "<1¢";
    return a < 1 ? `${Math.round(a * 100)}¢` : `$${a.toFixed(2)}`;
  }

  function fmtArrowPct(frac, digits = 1) {
    const v = safeNum(frac);
    if (v == null) return "—";
    const p = v * 100;
    if (Math.abs(p) < 0.05) return "0.0%";
    return `${p > 0 ? "▲" : "▼"} ${Math.abs(p).toFixed(digits)}%`;
  }

  function capitalize(s) {
    const t = String(s || "");
    return t ? t[0].toUpperCase() + t.slice(1) : t;
  }

  function joinAnd(list) {
    if (list.length <= 1) return list.join("");
    return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
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

  /** Escape text from the feed (tickers, reasoning, headlines) before it goes into innerHTML. */
  function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function trunc(s, n = 140) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function pnlClass(n) {
    if (n == null) return "";
    return Number(n) >= 0 ? "gain" : "loss";
  }

  /** Like pnlClass, but amounts under a cent read as flat. */
  function pnlTone(n) {
    const v = safeNum(n);
    if (v == null || Math.abs(v) < 0.005) return "muted";
    return v > 0 ? "gain" : "loss";
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

  function lifetimeForTicker(ticker) {
    const want = tickerKey(ticker);
    return (DATA?.ticker_pnl || []).find((r) => tickerKey(r.ticker) === want) || null;
  }

  function reviewTagsHtml(tags) {
    const list = Array.isArray(tags) ? tags : [];
    if (!list.length) return "";
    return `<div class="badge-row" style="margin-top:0.5rem">${list.map((t) =>
      `<span class="chip review-tag ${escapeHtml(t)}">${escapeHtml(t)}</span>`
    ).join("")}</div>`;
  }

  function reviewEvidenceHtml(evidence) {
    const list = Array.isArray(evidence) ? evidence : [];
    if (!list.length) {
      return `<p class="muted" style="margin-top:0.5rem">No evidence recorded.</p>`;
    }
    return `<ul class="review-evidence" style="margin:0.35rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:0.8rem">
      ${list.map((e) => {
        const idBit = e && e.id != null ? `<span class="mono">#${escapeHtml(e.id)}</span> · ` : `<span class="mono">—</span> · `;
        const kind = e && e.kind ? escapeHtml(e.kind) : "?";
        const label = e && e.label ? escapeHtml(e.label) : "";
        const detail = e && e.detail ? escapeHtml(trunc(String(e.detail), 160)) : "";
        return `<li><span class="mono subtle">${kind}</span> ${idBit}<strong>${label}</strong> — ${detail}</li>`;
      }).join("")}
    </ul>`;
  }

  function reviewMixHtml(mix, agents) {
    const m = mix || {};
    const rows = Array.isArray(agents) ? agents : [];
    let table = "";
    if (rows.length) {
      table = `<table style="margin-top:0.5rem;font-size:0.85rem;min-width:0">
        <thead><tr><th>Agent</th><th>Dir</th><th>Conf</th><th>Signal</th></tr></thead>
        <tbody>
          ${rows.map((a) => `<tr>
            <td class="mono">${escapeHtml(a.agent_id || "—")}</td>
            <td><span class="dir ${escapeHtml(a.direction || "")}">${escapeHtml(a.direction || "—")}</span></td>
            <td class="mono">${a.confidence != null ? Number(a.confidence).toFixed(1) : "—"}</td>
            <td class="mono subtle">${a.signal_id != null ? "#" + a.signal_id : "—"}</td>
          </tr>`).join("")}
        </tbody>
      </table>`;
    }
    const lead = m.lead_agent ? `lead=${escapeHtml(m.lead_agent)}` : "lead=—";
    const split = m.disagreement ? "split_book" : "aligned";
    return `<p class="muted" style="margin-top:0.35rem">${lead} · ${split} · L[${escapeHtml((m.long || []).join(",") || "—")}] S[${escapeHtml((m.short || []).join(",") || "—")}] N[${escapeHtml((m.neutral || []).join(",") || "—")}]</p>${table}`;
  }

  function reviewBlockHtml(review, { heading } = {}) {
    if (!review || typeof review !== "object") {
      return `<p class="muted" style="margin-top:1rem">No trade review for this close.</p>`;
    }
    const path = review.path || {};
    const title = heading || "Review";
    const completeness = escapeHtml(review.completeness || "—");
    return `
      <p class="kicker" style="margin-top:1rem">${title}
        <span class="chip review-completeness ${completeness}" style="margin-left:0.5rem">${completeness}</span>
      </p>
      <p style="margin-top:0.4rem">${escapeHtml(review.verdict || "—")}</p>
      ${reviewTagsHtml(review.verdict_tags)}
      <div class="grid-stats" style="margin-top:0.75rem">
        <div class="stat"><p class="label">Exit reason</p><p class="value mono" style="font-size:0.85rem">${escapeHtml(review.exit_reason || "—")}</p></div>
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

  function zoneThresholds() {
    const t = DATA?.zones?.thresholds || {};
    return { buy: safeNum(t.buy_zone_min) ?? 0.35, sell: safeNum(t.sell_zone_min) ?? 0.35 };
  }

  function zoneBadgeHtml(zone) {
    const z = escapeHtml(zone || "quiet");
    const label = z.replace(/_/g, " ");
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
    const first = escapeHtml(series[0].day.slice(5));
    const last = escapeHtml(series[series.length - 1].day.slice(5));
    const thresh =
      0.35 <= yMax
        ? `<line x1="${pad.l}" x2="${w - pad.r}" y1="${y(0.35)}" y2="${y(0.35)}" stroke="#3a4652" stroke-dasharray="3 4"/>`
        : "";
    return `<div class="zone-chart"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Buy and sell pressure history">
      ${thresh}
      <text x="4" y="${y(yMax) + 3}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono">${yMax.toFixed(2)}</text>
      <text x="4" y="${y(0) + 3}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono">0</text>
      <polyline points="${buyPts}" fill="none" stroke="${C.gain}" stroke-width="1.6"/>
      <polyline points="${sellPts}" fill="none" stroke="${C.loss}" stroke-width="1.6"/>
      <text x="${pad.l}" y="${h - 8}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono">${first}</text>
      <text x="${w - pad.r}" y="${h - 8}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono" text-anchor="end">${last}</text>
      <text x="${pad.l + 4}" y="12" fill="${C.gain}" font-size="10" font-family="IBM Plex Mono">buy</text>
      <text x="${pad.l + 40}" y="12" fill="${C.loss}" font-size="10" font-family="IBM Plex Mono">sell</text>
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
              <td class="mono muted">${escapeHtml(a.agent_id)}</td>
              <td><span class="dir ${escapeHtml(a.direction)}">${escapeHtml(a.direction)}</span></td>
              <td class="mono">${Number(a.confidence).toFixed(1)}</td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
  }

  function pressureBarsHtml(pt) {
    if (!pt) return `<p class="muted">No zone score for this ticker yet.</p>`;
    const th = zoneThresholds();
    const scale = Math.max(0.6, th.buy * 1.7);
    const w = (v) => Math.min(100, ((Number(v) || 0) / scale) * 100).toFixed(1);
    return `<div class="pbars">
        <span>Buy</span><div class="track"><div class="fill" style="width:${w(pt.buy)}%;background:${C.gain}"></div></div><span class="num">${Number(pt.buy).toFixed(2)}</span>
        <span>Sell</span><div class="track"><div class="fill" style="width:${w(pt.sell)}%;background:${C.loss}"></div></div><span class="num">${Number(pt.sell).toFixed(2)}</span>
      </div>
      <p class="footnote" style="margin-top:0.4rem">Buy zone starts at ${th.buy.toFixed(2)} · ${zoneBadgeHtml(pt.zone)}</p>`;
  }

  function zoneDetailBlock(ticker) {
    const pt = zonePointForTicker(ticker);
    const series = zoneSeriesForTicker(ticker);
    if (!pt && !series.length) {
      return `<h4>Pressure history</h4>
        <p class="muted">No zone score for this ticker yet.</p>`;
    }
    return `<h4>Pressure history</h4>
      ${drawZoneHistoryChart(series)}`;
  }

  function openZoneDetail(pt) {
    openTickerDetail(pt.ticker);
  }

  function openDialogHtml(html) {
    const dlg = $("dialog");
    const body = $("dialogBody");
    if (dlg.hidden) lastFocus = document.activeElement;
    body.innerHTML = html;
    const title = body.querySelector("h3");
    if (title) title.id = "dialogTitle";
    dlg.hidden = false;
    dlg.classList.add("open");
    dlg.querySelector(".drawer").scrollTop = 0;
    $("dialogClose").focus();
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
    const pt = zonePointForTicker(ticker);
    return pt?.sector || null;
  }

  function matchesSector(sec) {
    return filter === "all" || sec === filter;
  }

  function themeKeys() {
    const keys = Object.keys(DATA?.sectors || {});
    (DATA?.sector_board || []).forEach((r) => { if (!keys.includes(r.theme)) keys.push(r.theme); });
    const weight = (t) => Number((DATA?.sector_board || []).find((r) => r.theme === t)?.weight) || 0;
    return keys.sort((a, b) => weight(b) - weight(a) || a.localeCompare(b));
  }

  function themeColor(theme) {
    if (THEME_COLOR[theme]) return THEME_COLOR[theme];
    const extras = themeKeys().filter((t) => !THEME_COLOR[t]);
    return EXTRA_THEME_COLORS[Math.max(0, extras.indexOf(theme)) % EXTRA_THEME_COLORS.length];
  }

  function setSectorFilter(key) {
    filter = key === "all" || themeKeys().includes(key) ? key : "all";
    renderFilteredSections();
  }

  function renderFilters() {
    const el = $("filters");
    el.innerHTML = "";
    ["all", ...themeKeys()].forEach((key) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "filter-chip";
      btn.textContent = key === "all" ? "All themes" : key;
      btn.setAttribute("aria-pressed", filter === key ? "true" : "false");
      btn.addEventListener("click", () => {
        setSectorFilter(key);
      });
      el.appendChild(btn);
    });
  }

  // ---- Today ----------------------------------------------------------------

  /** Last equity point of each ET day, oldest first. */
  function dailyCloses(curve) {
    const byDay = new Map();
    (curve || []).forEach((p) => {
      const day = etCalendarDay(p.t);
      if (day && safeNum(p.equity) != null) byDay.set(day, { day, t: p.t, equity: Number(p.equity) });
    });
    return [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day));
  }

  /** Last session vs the close before it (works on weekends and before the open). */
  function sessionChange() {
    const s = DATA.snapshot || {};
    const closes = dailyCloses(DATA.equity_curve);
    const equity = safeNum(s.equity) ?? (closes.length ? closes[closes.length - 1].equity : null);
    if (equity == null || closes.length < 2) {
      return { equity, change: safeNum(s.daily_pnl), prior: null, session: closes.length ? closes[closes.length - 1].day : null };
    }
    const last = closes[closes.length - 1];
    const prior = closes[closes.length - 2];
    return { equity, change: equity - prior.equity, prior, session: last.day };
  }

  function todayFills() {
    // The export's own day, not the viewer's clock: a Saturday visit still describes Friday.
    const day = researchFallbackDay() || DATA?.meta?.day_local || etTodayYesterday().today;
    return (DATA.closed || []).filter((c) => etCalendarDay(c.closed_at) === day);
  }

  function buildSummary(sc) {
    const parts = [];
    if (sc.change != null) {
      const word = Math.abs(sc.change) < 0.005 ? "Flat" : sc.change > 0 ? "Up" : "Down";
      parts.push(word === "Flat" ? "Flat on the day." : `${word} ${fmtMoneyPlain(sc.change)} on the day.`);
    }
    const board = (DATA.sector_board || []).slice().sort((a, b) => (Number(b.unrealized) || 0) - (Number(a.unrealized) || 0));
    const lead = board[0];
    if (lead && Number(lead.unrealized) > 0.005) {
      const best = holdingsView()
        .filter((r) => r.theme === lead.theme && r.pct != null)
        .sort((a, b) => (Number(b.h.unrealized) || 0) - (Number(a.h.unrealized) || 0))[0];
      const who = best && best.pct > 0 ? ` — ${escapeHtml(best.h.ticker)} is ${fmtArrowPct(best.pct).replace("▲ ", "+")} on cost` : "";
      parts.push(`<strong>${escapeHtml(capitalize(lead.theme))} carried the book</strong>${who}.`);
    }
    const byTicker = {};
    todayFills().forEach((c) => {
      const k = tickerKey(c.ticker);
      byTicker[k] = byTicker[k] || { n: 0, pnl: 0 };
      byTicker[k].n += 1;
      byTicker[k].pnl += Number(c.pnl) || 0;
    });
    const churn = Object.entries(byTicker).sort((a, b) => b[1].n - a[1].n)[0];
    if (churn && churn[1].n >= 4) {
      parts.push(`The bot sold <strong>${escapeHtml(churn[0])} ${churn[1].n} times for ${fmtMoneySigned(churn[1].pnl)}</strong>.`);
    }
    const c = DATA.decisions_today?.counts || {};
    const flags = (DATA.auditor_flags_today || []).length;
    parts.push(`${c.fills || 0} fill${c.fills === 1 ? "" : "s"}, ${c.holds || 0} hold${c.holds === 1 ? "" : "s"}, ${flags ? `${flags} auditor flag${flags === 1 ? "" : "s"}` : "no auditor flags"}.`);
    return parts.join(" ");
  }

  function renderFreshness() {
    const s = DATA.snapshot || {};
    const meta = DATA.meta || {};
    const exported = meta.exported_at ? new Date(meta.exported_at) : null;
    const ageH = exported ? (Date.now() - exported.getTime()) / 36e5 : null;
    const halted = s.halted || s.day_halted;
    const stale = ageH != null && ageH > 26;
    const cls = halted ? "halt" : stale ? "stale" : "";
    const when = exported
      ? `${exported.toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" })} ET · ${fmtDay(etCalendarDay(exported), { weekday: true })}`
      : "—";
    $("exportedAt").innerHTML = `<span class="dot ${cls}" aria-hidden="true"></span>${halted ? "Halted · " : ""}Updated ${escapeHtml(when)}${stale ? ` <span class="subtle">(${Math.round(ageH)}h old)</span>` : ""}`;
    $("modePill").textContent = meta.paper === false ? "Live" : "Paper";
  }

  function renderToday() {
    const s = DATA.snapshot || {};
    const meta = DATA.meta || {};
    const sc = sessionChange();
    const start = safeNum(meta.starting_equity);
    $("equityEyebrow").textContent = start != null ? `Equity · ${fmtUsd(start)} ${meta.paper === false ? "" : "paper "}account` : "Equity";
    $("equityBig").textContent = fmtUsd(sc.equity);

    const chips = [];
    if (sc.change != null) {
      const pct = sc.prior ? sc.change / sc.prior.equity : null;
      const tone = pnlTone(sc.change);
      const arrow = tone === "gain" ? "▲" : tone === "loss" ? "▼" : "";
      const label = sc.session && sc.session !== meta.day_local ? fmtDay(sc.session) : "today";
      chips.push(`<span class="delta ${tone}">${arrow} ${fmtMoneyPlain(sc.change)} ${escapeHtml(label)}${pct != null ? ` (${fmtSignedPct(pct)})` : ""}</span>`);
    }
    if (start && sc.equity != null) {
      const sinceStart = (sc.equity - start) / start;
      chips.push(`<span class="delta ${pnlTone(sc.equity - start)}">${fmtArrowPct(sinceStart, 2)} since start</span>`);
    }
    if (s.halted || s.day_halted) {
      chips.push(`<span class="delta halt">Halted: ${escapeHtml(trunc(s.halt_reason || (s.day_halted ? "day halt" : "halted"), 60))}</span>`);
    }
    $("equityChips").innerHTML = chips.join("");
    $("daySummary").innerHTML = buildSummary(sc);
    $("dayBasis").textContent = sc.prior
      ? `${sc.session && sc.session !== meta.day_local ? `Last session ${fmtDay(sc.session)}, ` : "Today is "}measured against the ${fmtDay(sc.prior.day)} close (${fmtUsd(sc.prior.equity)}).`
      : "";
    $("startLegend").textContent = start != null ? `${fmtUsd(start).replace(".00", "")} start` : "Start";
    drawEquityChart(dailyCloses(DATA.equity_curve), start);
  }

  function drawEquityChart(closes, start) {
    const host = $("equityChart");
    if (!closes.length) {
      host.innerHTML = `<div class="empty" style="flex:1">No portfolio snapshots yet</div>`;
      return;
    }
    $("curveTitle").textContent = `Equity since ${fmtDay(closes[0].day)}`;
    const W = 640;
    const H = 200;
    const vals = closes.map((p) => p.equity).concat(start != null ? [start] : []);
    let lo = Math.min(...vals);
    let hi = Math.max(...vals);
    const padV = Math.max((hi - lo) * 0.08, 0.5);
    lo -= padV;
    hi += padV;
    const x = (i) => (closes.length === 1 ? W / 2 : (i / (closes.length - 1)) * W);
    const y = (v) => H - ((v - lo) / (hi - lo)) * H;
    const line = closes.map((p, i) => `${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
    const area = `0,${H} ${line} ${W},${H}`;
    const mid = (lo + hi) / 2;
    const iLow = closes.reduce((m, p, i) => (p.equity < closes[m].equity ? i : m), 0);
    const iHigh = closes.reduce((m, p, i) => (p.equity > closes[m].equity ? i : m), 0);
    const xLabels = [0, iLow, iHigh, closes.length - 1]
      .filter((v, i, arr) => arr.indexOf(v) === i)
      .sort((a, b) => a - b)
      .map((i) => {
        const tag = i === iLow && i !== 0 && i !== closes.length - 1 ? " · low" : i === iHigh && i !== 0 && i !== closes.length - 1 ? " · high" : "";
        return `<span>${escapeHtml(fmtDay(closes[i].day))}${tag}</span>`;
      })
      .join("");
    const aria = `Equity from ${fmtUsd(closes[0].equity)} on ${fmtDay(closes[0].day)} to ${fmtUsd(closes[closes.length - 1].equity)} on ${fmtDay(closes[closes.length - 1].day)}; low ${fmtUsd(closes[iLow].equity)} on ${fmtDay(closes[iLow].day)}, high ${fmtUsd(closes[iHigh].equity)} on ${fmtDay(closes[iHigh].day)}.`;
    host.innerHTML = `
      <div class="curve-axis" aria-hidden="true"><span>${fmtUsd(hi).replace(/\.\d\d$/, "")}</span><span>${fmtUsd(mid).replace(/\.\d\d$/, "")}</span><span>${fmtUsd(lo).replace(/\.\d\d$/, "")}</span></div>
      <div class="curve-plot">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${escapeHtml(aria)}">
          <line x1="0" x2="${W}" y1="${y(mid)}" y2="${y(mid)}" stroke="${C.line}" vector-effect="non-scaling-stroke"/>
          ${start != null ? `<line x1="0" x2="${W}" y1="${y(start)}" y2="${y(start)}" stroke="${C.subtle}" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>` : ""}
          <polygon points="${area}" fill="rgb(143 180 196 / 0.10)"/>
          <polyline points="${line}" fill="none" stroke="${C.accent}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
          <line id="curveCursor" x1="0" x2="0" y1="0" y2="${H}" stroke="${C.accent}" opacity="0.4" vector-effect="non-scaling-stroke"/>
        </svg>
        <div class="curve-hit" id="curveHit"></div>
        <div class="curve-tip" id="curveTip" aria-live="polite"></div>
        <div class="curve-x" aria-hidden="true">${xLabels}</div>
      </div>`;
    const hit = $("curveHit");
    const show = (i) => {
      const p = closes[i];
      const prev = closes[i - 1];
      const chg = prev ? p.equity - prev.equity : null;
      const pctX = (x(i) / W) * 100;
      $("curveCursor").setAttribute("x1", x(i));
      $("curveCursor").setAttribute("x2", x(i));
      const tip = $("curveTip");
      tip.style.left = `${Math.min(86, Math.max(14, pctX))}%`;
      // Keep the tip off the line: below the point when it sits in the top half.
      const high = y(p.equity) < H / 2;
      tip.style.top = high ? "auto" : "0.25rem";
      tip.style.bottom = high ? "1.9rem" : "auto";
      tip.innerHTML = `<span class="muted">${escapeHtml(fmtDay(p.day, { weekday: true }))} close</span>
        <span class="v">${fmtUsd(p.equity)}</span>
        <span class="${pnlTone(chg)}">${chg == null ? "first day" : `${chg >= 0 ? "▲" : "▼"} ${fmtMoneyPlain(chg)} that day`}</span>
        ${start ? `<span class="subtle">${fmtSignedPct((p.equity - start) / start)} from start</span>` : ""}`;
    };
    closes.forEach((p, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("aria-label", `${fmtDay(p.day)} close ${fmtUsd(p.equity)}`);
      b.addEventListener("mouseenter", () => show(i));
      b.addEventListener("focus", () => show(i));
      b.addEventListener("click", () => show(i));
      hit.appendChild(b);
    });
    show(closes.length - 1);
  }

  function renderStats() {
    const s = DATA.snapshot || {};
    const view = holdingsView();
    const cost = view.reduce((a, r) => a + (costBasis(r.h.qty, r.h.avg_cost) || 0), 0);
    const unr = safeNum(s.unrealized);
    const equity = safeNum(s.equity);
    const cash = safeNum(s.cash);
    const idle = equity && cash != null ? cash / equity : null;
    const peak = safeNum(s.peak_equity);
    const c = DATA.decisions_today?.counts || {};
    const flags = (DATA.auditor_flags_today || []).length;
    const tiles = [
      { k: "Cash", v: fmtUsd(cash), s: idle == null ? "" : `${(idle * 100).toFixed(1)}% idle${idle < 0.03 ? " · fully invested" : ""}` },
      { k: "Unrealized", v: unr == null ? "—" : `${unr >= 0 ? "▲" : "▼"} ${fmtUsd(Math.abs(unr))}`, cls: pnlTone(unr), s: unr != null && cost ? `${fmtSignedPct(unr / cost)} on ${fmtUsd(cost)} cost` : "" },
      { k: "Drawdown", v: fmtPct(s.drawdown), cls: Number(s.drawdown) > 0.0005 ? "loss" : "", s: peak != null ? `from ${fmtUsd(peak)} peak` : "" },
      { k: researchFallbackDay() ? `Activity · ${fmtDay(researchFallbackDay())}` : "Activity today", v: `${c.fills || 0} fill${c.fills === 1 ? "" : "s"}`, s: `${c.tickets || 0} tickets · ${c.holds || 0} holds · ${flags} flag${flags === 1 ? "" : "s"}` },
    ];
    $("statStrip").innerHTML = tiles
      .map((t) => `<div class="tile"><span class="k">${escapeHtml(t.k)}</span><span class="v ${t.cls || ""}">${t.v}</span><span class="s">${escapeHtml(t.s)}</span></div>`)
      .join("");
  }

  // ---- Book -----------------------------------------------------------------

  /** Same rule as the export's is_dust: under $0.01 at cost is leftover rounding. */
  function isDust(h) {
    const q = safeNum(h.qty);
    const a = safeNum(h.avg_cost);
    if (q == null || a == null) return false;
    return q <= 0 || Math.abs(q * a) < 0.01;
  }

  /** When today has no research yet (weekend, holiday, pre-open), the export shows the last day that did. */
  function researchFallbackDay() {
    const meta = DATA?.meta || {};
    return meta.research_fallback && meta.research_day_local ? String(meta.research_day_local) : null;
  }

  function renderResearchDayLabels() {
    const day = researchFallbackDay();
    const title = $("agentsTitle");
    const hint = $("agentsHint");
    const zonesHint = $("zonesHint");
    if (title) title.textContent = day ? `Research · ${fmtDay(day, { weekday: true })}` : "Today's research";
    if (hint) {
      hint.textContent = day
        ? `No research yet today (ET), so this shows ${day}, the last day with signals.`
        : "Every agent's call on every active name today (ET).";
    }
    if (zonesHint) {
      zonesHint.textContent =
        (day ? `Pressure from ${day}, the last day with research. ` : "") +
        "Every active name by aggregated research pressure. Filled = held, open = watch. Click a point for detail.";
    }
  }

  /** Non-dust holdings with value, weight and % on cost, largest first. */
  function holdingsView() {
    const equity = safeNum(DATA?.snapshot?.equity);
    return (DATA?.holdings || [])
      .filter((h) => !isDust(h))
      .map((h) => {
        const mv = marketValue(h.qty, h.last);
        return {
          h,
          mv,
          weight: mv != null && equity ? mv / equity : null,
          pct: unrealizedPct(h.unrealized, h.qty, h.avg_cost),
          theme: h.sector || sectorOf(h.ticker),
        };
      })
      .sort((a, b) => (b.mv ?? -1) - (a.mv ?? -1));
  }

  function renderHoldings() {
    const all = holdingsView();
    const small = all.filter((r) => r.mv != null && r.mv < SMALL_POSITION_USD);
    const rows = showSmall ? all : all.filter((r) => !(r.mv != null && r.mv < SMALL_POSITION_USD));
    const toggle = $("smallToggle");
    toggle.hidden = !small.length;
    toggle.textContent = showSmall
      ? `Hide ${small.length} position${small.length === 1 ? "" : "s"} under $1`
      : `Show ${small.length} position${small.length === 1 ? "" : "s"} under $1`;
    if (!rows.length) {
      $("holdingsTable").innerHTML = `<div class="empty">No open holdings</div>`;
      return;
    }
    const maxW = Math.max(0.25, ...rows.map((r) => r.weight || 0));
    $("holdingsTable").innerHTML = `
      <table>
        <thead><tr><th>Ticker</th><th>Weight</th><th class="num">Value</th><th class="num">On cost</th><th class="num">P&amp;L</th></tr></thead>
        <tbody>
          ${rows
            .map(
              (r, i) => `<tr data-hold-idx="${i}" aria-label="Open details for ${escapeHtml(r.h.ticker)}">
                <td><span class="tick">${escapeHtml(r.h.ticker)}</span><span class="tick-sub">${escapeHtml(r.theme || "—")}</span><span class="chev" aria-hidden="true">›</span></td>
                <td><div class="wbar"><div class="track"><div class="fill" style="width:${r.weight == null ? 0 : ((r.weight / maxW) * 100).toFixed(1)}%"></div></div><span class="num">${r.weight == null ? "—" : (r.weight * 100).toFixed(1) + "%"}</span></div></td>
                <td class="num mono">${r.mv != null && r.mv < 0.005 ? "<1¢" : fmtUsd(r.mv)}</td>
                <td class="num mono ${pnlTone(r.h.unrealized)}">${fmtArrowPct(r.pct)}</td>
                <td class="num mono ${pnlTone(r.h.unrealized)}">${fmtMoneySigned(r.h.unrealized)}</td>
              </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
    $("holdingsTable").querySelectorAll("tr[data-hold-idx]").forEach((tr) => {
      const idx = Number(tr.getAttribute("data-hold-idx"));
      bindRowActivation(tr, () => openHoldingDetail(rows[idx].h));
    });
  }

  function whyBoughtBuyHtml(b) {
    const sigs = Array.isArray(b.signals) ? b.signals : [];
    const fills = Array.isArray(b.fills) ? b.fills : [];
    const backfill = fills.length > 0 && fills.every((f) => String(f.mode || "").includes("backfill"));
    const score = b.score || {};
    const scoreBits = [
      score.weighted_score != null ? `score ${Number(score.weighted_score).toFixed(3)}` : "",
      score.blended_conf != null ? `conf ${Number(score.blended_conf).toFixed(1)}` : "",
    ].filter(Boolean).join(" · ");
    const reason = backfill
      ? `<p class="muted" style="margin-top:0.35rem">Imported from Alpaca order history; the PM's reasoning wasn't recorded.</p>`
      : `<p style="margin-top:0.35rem">${escapeHtml(b.rationale || "—")}</p>`;
    const sigTable = sigs.length
      ? `<table class="agent-mix-table">
          <thead><tr><th>Desk</th><th>Dir</th><th>Conf</th><th>Reasoning</th></tr></thead>
          <tbody>
            ${sigs.map((s) => `<tr>
              <td class="mono muted">${escapeHtml(s.agent_id || "—")}</td>
              <td><span class="dir ${escapeHtml(s.direction || "")}">${escapeHtml(s.direction || "—")}</span></td>
              <td class="mono">${s.confidence != null ? Number(s.confidence).toFixed(1) : "—"}</td>
              <td class="muted">${escapeHtml(trunc(s.reasoning_trunc || "", 140))}</td>
            </tr>`).join("")}
          </tbody>
        </table>`
      : `<p class="muted" style="margin-top:0.35rem">No desk signals linked to this ticket.</p>`;
    const fillTable = fills.length
      ? `<table class="agent-mix-table">
          <thead><tr><th>Filled</th><th>Qty</th><th>Price</th><th>Mode</th></tr></thead>
          <tbody>
            ${fills.map((f) => `<tr>
              <td class="mono subtle">${fmtTime(f.at)}</td>
              <td class="mono">${f.qty != null ? Number(f.qty).toFixed(6) : "—"}</td>
              <td class="mono">${fmtUsd(f.price)}</td>
              <td class="mono muted">${escapeHtml(f.mode || "—")}</td>
            </tr>`).join("")}
          </tbody>
        </table>`
      : "";
    return `<div class="why-buy">
        <p><span class="mono">Ticket #${b.ticket_id != null ? b.ticket_id : "—"}</span>
          · <span class="subtle">${fmtTime(b.created_at)}</span>
          ${scoreBits ? `· <span class="mono muted">${scoreBits}</span>` : ""}</p>
        ${reason}
        ${sigTable}
        ${fillTable}
      </div>`;
  }

  function whyBoughtHtml(wb) {
    const head = `<h4>Why we bought it</h4>`;
    const buys = wb && Array.isArray(wb.buys) ? wb.buys : [];
    if (!buys.length) {
      return `${head}<p class="muted">No buy records for this position in the feed.</p>`;
    }
    const count = wb.buy_count != null ? wb.buy_count : buys.length;
    const omitted = Number(wb.omitted) || 0;
    const html = buys.map((b, i) => {
      const gap = omitted && i === 1
        ? `<p class="muted why-gap">${omitted} more buy${omitted === 1 ? "" : "s"} between these not shown</p>`
        : "";
      return gap + whyBoughtBuyHtml(b);
    }).join("");
    return `${head}
      <p class="muted">Position opened ${fmtTime(wb.opened_at)} · ${count} buy ticket${count === 1 ? "" : "s"}</p>
      ${html}`;
  }

  // ---- Agent votes -----------------------------------------------------------

  /** One entry per agent: today's direction/confidence and its latest signal for the ticker. */
  function votesForTicker(ticker) {
    const pt = zonePointForTicker(ticker);
    const sigs = signalsForTicker(ticker);
    return AGENT_ORDER.map((agent) => {
      const sig = sigs.find((s) => s.agent_id === agent) || null;
      const z = (pt?.agents || []).find((a) => a.agent_id === agent) || null;
      const src = sig || z;
      return {
        agent,
        direction: src ? String(src.direction || "neutral") : null,
        confidence: src ? safeNum(src.confidence) : null,
        signal: sig,
      };
    });
  }

  function voteFill(v) {
    if (!v || !v.direction) return { cls: "none", bg: "transparent" };
    const a = (0.10 + Math.min(100, Math.max(0, v.confidence || 0)) / 100 * 0.42).toFixed(2);
    if (v.direction === "long") return { cls: "long", bg: `rgb(134 208 160 / ${a})` };
    if (v.direction === "short") return { cls: "short", bg: `rgb(239 154 134 / ${a})` };
    return { cls: "neutral", bg: "" };
  }

  function dotsHtml(ticker) {
    const votes = votesForTicker(ticker);
    const label = votes.map((v) => `${AGENT_LABEL[v.agent]} ${v.direction || "no call"}${v.confidence != null ? " " + Math.round(v.confidence) : ""}`).join(", ");
    return `<span class="dots" role="img" aria-label="${escapeHtml(label)}">${votes.map((v) => {
      const f = voteFill(v);
      const style = f.cls === "long" || f.cls === "short" ? ` style="background:${f.bg};border-color:transparent"` : "";
      return `<span${style}></span>`;
    }).join("")}</span>`;
  }

  /** Each agent's newest signal per ticker (research runs several cycles a day). */
  function latestSignals(agent) {
    const byTicker = new Map();
    (DATA?.agents_today?.[agent]?.signals || []).forEach((s) => {
      const k = tickerKey(s.ticker);
      const prev = byTicker.get(k);
      if (!prev || String(s.created_at || "") > String(prev.created_at || "")) byTicker.set(k, { ...s, agent_id: s.agent_id || agent });
    });
    return [...byTicker.values()];
  }

  /** Agents whose latest call was the same direction and confidence on every ticker. */
  function flatAgents() {
    const out = [];
    AGENT_ORDER.forEach((agent) => {
      const sigs = latestSignals(agent);
      if (sigs.length < 5) return;
      const keys = new Set(sigs.map((s) => `${s.direction}|${Math.round(Number(s.confidence) || 0)}`));
      if (keys.size === 1) {
        const s = sigs[0];
        const noData = sigs.every((x) => /no recent headlines|insufficient data/i.test(String(x.reasoning || "")));
        out.push({ agent, direction: s.direction, confidence: Math.round(Number(s.confidence) || 0), noData });
      }
    });
    return out;
  }

  function agentReadsHtml(ticker, focusAgent) {
    const votes = votesForTicker(ticker);
    if (votes.every((v) => !v.direction)) {
      return `<h4>Agent reads</h4><p class="muted">No agent signals for this ticker in the feed.</p>`;
    }
    return `<h4>${researchFallbackDay() ? `Agent reads · ${escapeHtml(fmtDay(researchFallbackDay()))}` : "Today's agent reads"}</h4>
      ${votes.map((v) => {
        const reason = v.signal ? escapeHtml(trunc(v.signal.reasoning || "", 260)) : "No reasoning in the feed.";
        return `<div class="read${v.agent === focusAgent ? " focus" : ""}">
          <div class="top"><span class="who">${AGENT_LABEL[v.agent]}</span>
            <span class="dir ${escapeHtml(v.direction || "")}">${escapeHtml(v.direction || "no call")}${v.confidence != null ? " · " + Math.round(v.confidence) : ""}</span></div>
          <p class="why">${reason}</p>
        </div>`;
      }).join("")}`;
  }

  function lifetimeStatsHtml(ticker) {
    const r = lifetimeForTicker(ticker);
    if (!r) return "";
    return `<h4>Lifetime</h4>
      <div class="grid-stats">
        <div class="stat"><p class="label">First bought</p><p class="value">${r.first_buy_at ? escapeHtml(fmtDay(etCalendarDay(r.first_buy_at))) : "—"}</p></div>
        <div class="stat"><p class="label">Buys · closes</p><p class="value">${r.buy_events ?? "—"} · ${r.close_events ?? "—"}</p></div>
        <div class="stat"><p class="label">Realized</p><p class="value ${pnlTone(r.realized)}">${fmtMoneySigned(r.realized)}</p></div>
        <div class="stat"><p class="label">Total P&amp;L</p><p class="value ${pnlTone(r.total)}">${fmtMoneySigned(r.total)}</p></div>
      </div>`;
  }

  function openTickerDetail(ticker, focusAgent) {
    const h = (DATA.holdings || []).find((x) => tickerKey(x.ticker) === tickerKey(ticker) && !isDust(x));
    if (h) {
      openHoldingDetail(h, focusAgent);
      return;
    }
    const w = (DATA.watchlist || []).find((x) => tickerKey(x.ticker) === tickerKey(ticker)) || { ticker, sector: sectorOf(ticker) };
    openWatchDetail(w, focusAgent);
  }

  function openHoldingDetail(h, focusAgent) {
    const mv = marketValue(h.qty, h.last);
    const pct = unrealizedPct(h.unrealized, h.qty, h.avg_cost);
    const view = holdingsView().find((r) => tickerKey(r.h.ticker) === tickerKey(h.ticker));
    const decisions = decisionsForTicker(h.ticker);
    const mission = missionForTicker(h.ticker);
    const closed = closedFillsForTicker(h.ticker);
    const fills = (Array.isArray(h.fills) ? h.fills : []).slice(0, 8);
    const latestDec = decisions[0];
    const theme = h.sector || sectorOf(h.ticker);

    let decisionHtml = `<p class="muted">No decisions for this ticker in today's feed.</p>`;
    if (latestDec) {
      decisionHtml = `
        <p><span class="mono">${escapeHtml(latestDec.decision_type || "decision")}</span>
          · <span class="muted">${escapeHtml(latestDec.actor || "—")}</span>
          · <span class="subtle">${fmtTime(latestDec.created_at)}</span></p>
        <p style="margin-top:0.5rem">${escapeHtml(latestDec.reasoning || "—")}</p>`;
    }

    let fillsHtml = "";
    if (fills.length) {
      fillsHtml = `<h4>Position fills</h4>
        <pre>${escapeHtml(JSON.stringify(fills, null, 2))}</pre>`;
    } else if (closed.length) {
      fillsHtml = `<h4>Recent sells</h4>
        <ul style="margin:0.35rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:0.85rem">
          ${closed.slice(0, 5).map((c) =>
            `<li class="mono">${fmtTime(c.closed_at)} · qty ${c.qty != null ? Number(c.qty).toFixed(4) : "—"} · exit ${fmtUsd(c.exit)} · pnl ${fmtUsd(c.pnl)}</li>`
          ).join("")}
        </ul>`;
    }

    let missionHtml = "";
    if (mission.length) {
      missionHtml = `<h4>Mission notes</h4>
        <ul style="margin:0.35rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:0.85rem">
          ${mission.map((e) =>
            `<li><span class="mono">${escapeHtml(e.type || "event")}</span> · ${fmtTime(e.at)} — ${escapeHtml(trunc(e.summary || "", 160))}</li>`
          ).join("")}
        </ul>`;
    }

    openDialogHtml(`
      <p class="kicker">Open book</p>
      <h3>${escapeHtml(h.ticker)} <span class="muted">${escapeHtml(theme || "—")}${view && view.weight != null ? ` · ${(view.weight * 100).toFixed(1)}% of book` : ""}</span></h3>
      <p class="muted">Marked ${fmtTime(h.updated_at)}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Value</p><p class="value">${fmtUsd(mv)}</p></div>
        <div class="stat"><p class="label">P&amp;L on cost</p><p class="value ${pnlTone(h.unrealized)}">${fmtMoneySigned(h.unrealized)} (${fmtArrowPct(pct)})</p></div>
        <div class="stat"><p class="label">Avg cost</p><p class="value">${fmtUsd(h.avg_cost)}</p></div>
        <div class="stat"><p class="label">Last</p><p class="value">${fmtUsd(h.last)}</p></div>
        <div class="stat"><p class="label">Qty</p><p class="value">${h.qty != null ? Number(h.qty).toFixed(6) : "—"}</p></div>
        <div class="stat"><p class="label">Zone</p><p class="value">${zoneBadgeHtml(zonePointForTicker(h.ticker)?.zone)}</p></div>
      </div>
      ${agentReadsHtml(h.ticker, focusAgent)}
      ${whyBoughtHtml(h.why_bought)}
      <h4>Pressure zone</h4>
      ${pressureBarsHtml(zonePointForTicker(h.ticker))}
      ${lifetimeStatsHtml(h.ticker)}
      <h4>Latest decision</h4>
      ${decisionHtml}
      ${fillsHtml}
      ${missionHtml}
      ${zoneDetailBlock(h.ticker)}`);
    if (focusAgent) {
      const el = $("dialogBody").querySelector(".read.focus");
      if (el) el.scrollIntoView({ block: "center" });
    }
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
      extra += `<h4>Latest research (same ticker)</h4>
        <p><span class="mono muted">${escapeHtml(latestSig.agent_id || "agent")}</span>
          · <span class="dir ${escapeHtml(latestSig.direction)}">${escapeHtml(latestSig.direction)}</span>
          · <span class="subtle">${fmtTime(latestSig.created_at)}</span></p>
        <p style="margin-top:0.35rem">${escapeHtml(latestSig.reasoning || "—")}</p>`;
    }
    if (related.length) {
      extra += `<h4>Related decisions</h4>
        <ul style="margin:0.35rem 0 0;padding-left:1.1rem;color:var(--muted);font-size:0.85rem">
          ${related.map((d) =>
            `<li><span class="mono">${escapeHtml(d.decision_type)}</span> · ${fmtTime(d.created_at)} — ${escapeHtml(trunc(d.reasoning || "", 140))}</li>`
          ).join("")}
        </ul>`;
    }
    if (c.buy || c.sell) {
      extra += `<h4>Linked legs</h4>
        <pre>${escapeHtml(JSON.stringify({ buy: c.buy || null, sell: c.sell || null }, null, 2))}</pre>`;
    }

    const review = c.review || null;
    openDialogHtml(`
      <p class="kicker">Closed trade</p>
      <h3>${escapeHtml(c.ticker)} <span class="muted">${escapeHtml(c.sector || sectorOf(c.ticker) || "—")}</span></h3>
      <p class="muted">Closed ${fmtTime(c.closed_at)} · mode ${escapeHtml(c.mode || "—")}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Qty</p><p class="value">${c.qty != null ? Number(c.qty).toFixed(6) : "—"}</p></div>
        <div class="stat"><p class="label">Realized P&amp;L</p><p class="value ${pnlTone(c.pnl)}">${fmtMoneySigned(c.pnl)}</p></div>
        <div class="stat"><p class="label">Entry</p><p class="value">${fmtUsd(c.entry)}</p></div>
        <div class="stat"><p class="label">Exit</p><p class="value">${fmtUsd(c.exit)}</p></div>
      </div>
      ${reviewBlockHtml(review)}
      ${extra || `<p class="muted" style="margin-top:1rem">No additional reasoning or linked fill payloads in this feed.</p>`}`);
  }

  // ---- Themes ---------------------------------------------------------------

  function themeFacts(theme) {
    const board = (DATA.sector_board || []).find((r) => r.theme === theme) || {};
    const names = (DATA.sectors?.[theme] || []).map(tickerKey);
    const held = holdingsView().filter((r) => r.theme === theme);
    const heldSet = new Set(held.map((r) => tickerKey(r.h.ticker)));
    const watch = watchRows().filter((w) => w.theme === theme);
    const tally = { long: 0, short: 0, neutral: 0 };
    const flat = new Set(flatAgents().map((f) => f.agent));
    const tallyLive = { long: 0, short: 0 };
    const perAgent = {};
    let inBuy = 0;
    names.forEach((t) => {
      votesForTicker(t).forEach((v) => {
        if (!v.direction) return;
        tally[v.direction] = (tally[v.direction] || 0) + 1;
        if (!flat.has(v.agent) && (v.direction === "long" || v.direction === "short")) tallyLive[v.direction] += 1;
        perAgent[v.agent] = perAgent[v.agent] || { long: 0, short: 0, neutral: 0, n: 0 };
        perAgent[v.agent][v.direction] = (perAgent[v.agent][v.direction] || 0) + 1;
        perAgent[v.agent].n += 1;
      });
      if (zonePointForTicker(t)?.zone === "buy_zone") inBuy += 1;
    });
    const cold = (DATA.cold || []).filter((c) => c.theme === theme);
    return { board, names, held, heldSet, watch, tally, tallyLive, flat, perAgent, inBuy, cold };
  }

  function themeTake(theme, f) {
    const out = [];
    const w = safeNum(f.board.weight);
    if (w != null) {
      out.push(w < 0.02
        ? `${escapeHtml(capitalize(theme))} is effectively out of the book (${(w * 100).toFixed(1)}%).`
        : `${escapeHtml(capitalize(theme))} is ${(w * 100).toFixed(1)}% of the book across ${f.held.length} holding${f.held.length === 1 ? "" : "s"}.`);
    }
    const best = f.held.slice().sort((a, b) => (Number(b.h.unrealized) || 0) - (Number(a.h.unrealized) || 0))[0];
    if (best && Number(best.h.unrealized) > 0.005) out.push(`Top open winner: ${escapeHtml(best.h.ticker)} (${fmtMoneySigned(best.h.unrealized)}).`);
    const lifetime = (DATA.ticker_pnl || []).filter((r) => (r.sector || sectorOf(r.ticker)) === theme);
    const worst = lifetime.slice().sort((a, b) => (Number(a.realized) || 0) - (Number(b.realized) || 0))[0];
    const rea = safeNum(f.board.realized);
    if (worst && Number(worst.realized) < -0.5) {
      out.push(`Biggest lifetime realized loss: ${escapeHtml(worst.ticker)} (${fmtMoneySigned(worst.realized)})${rea != null && rea < 0 ? ` of the theme's ${fmtMoneySigned(rea)}` : ""}.`);
    }
    const r20 = safeNum(f.board.returns?.["20d"]);
    if (r20 != null) out.push(`20-day tape ${fmtSignedPct(r20)}.`);
    AGENT_ORDER.forEach((agent) => {
      const p = f.perAgent[agent];
      if (!p || f.flat.has(agent) || p.n < 5) return;
      ["long", "short"].forEach((dir) => {
        if (p[dir] >= Math.ceil(p.n * 0.7)) out.push(`${AGENT_LABEL[agent]} votes ${dir} on ${p[dir]} of ${p.n} names.`);
      });
    });
    return out.join(" ");
  }

  function renderSectors() {
    const host = $("sectorBoard");
    const split = $("themeSplit");
    if (!host) return;
    const themes = themeKeys();
    if (!themes.length) {
      host.innerHTML = `<div class="empty">No themes in latest.json — re-export dashboard</div>`;
      split.innerHTML = "";
      return;
    }
    const byTheme = Object.fromEntries((DATA.sector_board || []).map((r) => [r.theme, r]));
    split.innerHTML = themes
      .map((t) => `<span style="width:${((Number(byTheme[t]?.weight) || 0) * 100).toFixed(1)}%;background:${themeColor(t)}"></span>`)
      .join("");
    host.innerHTML = themes
      .map((theme) => {
        const r = byTheme[theme] || {};
        const f = themeFacts(theme);
        const ret = r.returns || {};
        const tape = ["1d", "5d", "20d"]
          .map((k) => `<span class="${pnlTone(ret[k])}">${k} ${fmtArrowPct(ret[k])}</span>`)
          .join("");
        const bits = [`${f.held.length} held`, f.inBuy ? `${f.inBuy} in buy zone` : "nothing in buy zone"];
        if (f.cold.length) bits.push(`${f.cold.length} on ice`);
        return `<button type="button" class="theme-row" data-theme="${escapeHtml(theme)}">
          <span class="top">
            <span class="name"><span class="sw" style="background:${themeColor(theme)}" aria-hidden="true"></span>${escapeHtml(theme)}</span>
            <span class="w">${r.weight != null ? (Number(r.weight) * 100).toFixed(1) + "%" : "—"} <span class="chev" aria-hidden="true">›</span></span>
          </span>
          <span class="tape">${tape}</span>
          <span class="note">${bits.join(" · ")}</span>
        </button>`;
      })
      .join("");
    host.querySelectorAll(".theme-row").forEach((btn) => {
      btn.addEventListener("click", () => openThemeDetail(btn.getAttribute("data-theme")));
    });
  }

  function openThemeDetail(theme) {
    const f = themeFacts(theme);
    const b = f.board;
    const ret = b.returns || {};
    const total = f.tally.long + f.tally.short + f.tally.neutral || 1;
    const liveN = f.names.length * (AGENT_ORDER.length - f.flat.size);
    const flatNames = [...f.flat].map((a) => AGENT_LABEL[a]);
    const rot = DATA.rotation?.settings || {};
    const art = ART[theme];
    openDialogHtml(`
      ${art ? `<img class="drawer-art" src="${art}" alt="" />` : ""}
      <p class="kicker">Theme</p>
      <h3 style="text-transform:capitalize">${escapeHtml(theme)}</h3>
      <p class="muted">${f.names.length} active names · ${f.held.length} held${b.weight != null ? ` · ${(Number(b.weight) * 100).toFixed(1)}% of book` : ""}</p>
      <p class="take">${themeTake(theme, f)}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Book weight</p><p class="value">${b.weight != null ? (Number(b.weight) * 100).toFixed(1) + "%" : "—"}</p></div>
        <div class="stat"><p class="label">In buy zone</p><p class="value ${f.inBuy ? "gain" : ""}">${f.inBuy} of ${f.names.length}</p></div>
        <div class="stat"><p class="label">Unrealized</p><p class="value ${pnlTone(b.unrealized)}">${fmtMoneySigned(b.unrealized)}</p></div>
        <div class="stat"><p class="label">Realized, lifetime</p><p class="value ${pnlTone(b.realized)}">${fmtMoneySigned(b.realized)}</p></div>
      </div>
      <h4>Tape (equal-weight)</h4>
      <div class="grid-stats" style="grid-template-columns:repeat(3,minmax(0,1fr))">
        ${["1d", "5d", "20d"].map((k) => `<div class="stat"><p class="label">${k}</p><p class="value ${pnlTone(ret[k])}">${fmtArrowPct(ret[k], 2)}</p></div>`).join("")}
      </div>
      <h4>How the agents lean</h4>
      <div class="lean" aria-hidden="true">
        <span style="width:${(f.tally.long / total) * 100}%;background:${C.gain}"></span>
        <span style="width:${(f.tally.neutral / total) * 100}%;background:#3a434d"></span>
        <span style="width:${(f.tally.short / total) * 100}%;background:${C.loss}"></span>
      </div>
      <p class="muted" style="margin:0;font-size:0.85rem">All ${total} votes: ${f.tally.long} long · ${f.tally.neutral} neutral · ${f.tally.short} short.${flatNames.length ? ` Leaving out ${joinAnd(flatNames)}, which gave every name the same call: ${f.tallyLive.long} long · ${f.tallyLive.short} short of ${liveN}.` : ""}</p>
      <h4>Held (${f.held.length})</h4>
      ${f.held.length ? f.held.map((r) => `<button type="button" class="list-btn" data-ticker="${escapeHtml(r.h.ticker)}">
          <span class="tick">${escapeHtml(r.h.ticker)}</span>
          <span class="muted">${r.weight != null ? (r.weight * 100).toFixed(1) + "% of book" : "—"}</span>
          <span class="mono ${pnlTone(r.h.unrealized)}">${fmtMoneySigned(r.h.unrealized)}</span>
          <span class="chev" aria-hidden="true">›</span>
        </button>`).join("") : `<p class="muted">Nothing held in this theme.</p>`}
      <h4>Watching (${f.watch.length})</h4>
      ${f.watch.length ? f.watch.map((w) => `<button type="button" class="list-btn" data-ticker="${escapeHtml(w.ticker)}">
          <span class="tick">${escapeHtml(w.ticker)}</span>
          ${dotsHtml(w.ticker)}
          <span class="muted" style="text-align:right">buy ${w.buy.toFixed(2)} · sell ${w.sell.toFixed(2)}</span>
          <span class="chev" aria-hidden="true">›</span>
        </button>`).join("") : `<p class="muted">No other active names.</p>`}
      ${f.cold.length ? `<h4>On ice</h4>${f.cold.map((c) => `<div class="read"><div class="top"><span class="who mono">${escapeHtml(c.ticker)}</span><span class="muted">until ${escapeHtml(fmtDay(c.checkup_date))}</span></div><p class="why">${escapeHtml(coldReason(c.reason))}</p></div>`).join("")}` : ""}
      ${rot.cold_days ? `<p class="footnote">A name goes cold for ${rot.cold_days} days when, over its last ${rot.lookback_cycles} cycles, median confidence is under ${rot.min_median_confidence} and under ${Math.round((rot.max_long_fraction || 0) * 100)}% of votes are long.</p>` : ""}`);
    $("dialogBody").querySelectorAll(".list-btn[data-ticker]").forEach((btn) => {
      btn.addEventListener("click", () => openTickerDetail(btn.getAttribute("data-ticker")));
    });
  }

  /** "awful:median_conf=15.0,long_frac=0.12" -> readable sentence. */
  function coldReason(reason) {
    const s = String(reason || "");
    const conf = /median_conf=([\d.]+)/.exec(s);
    const lf = /long_frac=([\d.]+)/.exec(s);
    if (conf || lf) {
      return `Benched after ${conf ? `median confidence of ${Math.round(Number(conf[1]))}` : "weak votes"}${lf ? ` and only ${Math.round(Number(lf[1]) * 100)}% long votes` : ""}.`;
    }
    return s || "Benched by rotation.";
  }

  // ---- Exits ----------------------------------------------------------------

  function exitReasonOf(c) {
    return c.review?.exit_reason || null;
  }

  function renderClosed() {
    const host = $("closedTable");
    if (!host) return;
    // UI-only window: full DATA.closed stays intact for Lifetime / detail helpers.
    const recent = closedRowsForRecentWindow(DATA.closed || [], () => true);
    if (!recent.length) {
      host.innerHTML = `<div class="empty">No closes today or yesterday (ET). Full history is in Lifetime.</div>`;
      return;
    }
    const { today, yesterday } = etTodayYesterday();
    const html = [
      { key: "today", label: `Today · ${fmtDay(today)}` },
      { key: "yesterday", label: `Yesterday · ${fmtDay(yesterday)}` },
    ]
      .map((g) => {
        const items = recent.filter((x) => x.bucket === g.key).map((x) => x.row);
        if (!items.length) return "";
        const groups = new Map();
        items.forEach((c) => {
          const k = tickerKey(c.ticker);
          if (!groups.has(k)) groups.set(k, []);
          groups.get(k).push(c);
        });
        const rows = [...groups.entries()]
          .sort((a, b) => b[1].length - a[1].length)
          .map(([ticker, fills]) => exitGroupHtml(ticker, fills, g.key))
          .join("");
        return `<p class="exit-day">${escapeHtml(g.label)}</p><div class="exit-list">${rows}</div>`;
      })
      .join("");
    host.innerHTML = html;
    host.querySelectorAll(".exit-row").forEach((btn) => {
      btn.addEventListener("click", () => {
        const fills = recent
          .filter((x) => x.bucket === btn.getAttribute("data-bucket") && tickerKey(x.row.ticker) === btn.getAttribute("data-ticker"))
          .map((x) => x.row);
        openExitGroup(btn.getAttribute("data-ticker"), fills);
      });
    });
  }

  function summarizeExits(fills) {
    const sorted = fills.slice().sort((a, b) => String(a.closed_at || "").localeCompare(String(b.closed_at || "")));
    const pnlKnown = sorted.filter((c) => safeNum(c.pnl) != null);
    const pnl = pnlKnown.length ? pnlKnown.reduce((a, c) => a + Number(c.pnl), 0) : null;
    const notional = sorted.reduce((a, c) => a + (marketValue(c.qty, c.exit) || 0), 0);
    const reasons = [...new Set(sorted.map(exitReasonOf).filter(Boolean))];
    const amounts = sorted.map((c) => marketValue(c.qty, c.exit)).filter((v) => v != null);
    const shrinking = amounts.length >= 4 && amounts.every((v, i) => i === 0 || v <= amounts[i - 1] + 1e-9);
    return { sorted, pnl, notional, reasons, amounts, shrinking };
  }

  function exitTag(fills, s) {
    const n = fills.length;
    const reason = s.reasons.length === 1 ? s.reasons[0] : null;
    if (n >= 4 && reason) return { cls: "warn", text: `${s.shrinking ? "Drip-selling" : "Repeat sells"} · ${n} sells` };
    if (reason === "opportunity_rotate") return { cls: "accent", text: `Rotation · ${n} sell${n === 1 ? "" : "s"}` };
    if (reason === "signal_sell") return { cls: "", text: `Signal exit · ${n} sell${n === 1 ? "" : "s"}` };
    if (reason) return { cls: "", text: `${reason.replace(/_/g, " ")} · ${n} sell${n === 1 ? "" : "s"}` };
    return { cls: "", text: `${n} sell${n === 1 ? "" : "s"}` };
  }

  function exitWhy(fills, s) {
    const first = s.sorted[0];
    const last = s.sorted[s.sorted.length - 1];
    const span = fills.length > 1 ? `from ${fmtClock(first.closed_at)} to ${fmtClock(last.closed_at)}` : `at ${fmtClock(first.closed_at)}`;
    if (s.shrinking && s.amounts.length >= 4) {
      return `A smaller slice each time, ${fmtMoneyPlain(s.amounts[0])} down to ${fmtMoneyPlain(s.amounts[s.amounts.length - 1])}, ${span}.`;
    }
    const entry = safeNum(first.entry);
    return `${capitalize(span)}${entry != null ? ` · bought at ${fmtUsd(entry)}` : ""}.`;
  }

  function exitGroupHtml(ticker, fills, bucket) {
    const s = summarizeExits(fills);
    const tag = exitTag(fills, s);
    return `<button type="button" class="exit-row" data-ticker="${escapeHtml(ticker)}" data-bucket="${escapeHtml(bucket)}">
      <span class="tick">${escapeHtml(ticker)}</span>
      <span class="pill ${tag.cls}">${escapeHtml(tag.text)}</span>
      <span class="why">${escapeHtml(exitWhy(fills, s))}</span>
      <span class="amt ${pnlTone(s.pnl)}">${s.pnl == null ? "P&amp;L n/a" : fmtMoneySigned(s.pnl)}</span>
      <span class="amt subtle">${fmtMoneyPlain(s.notional)} sold <span class="chev" aria-hidden="true">›</span></span>
    </button>`;
  }

  function openExitGroup(ticker, fills) {
    const s = summarizeExits(fills);
    const tag = exitTag(fills, s);
    const first = s.sorted[0] || {};
    const held = (DATA.holdings || []).find((h) => tickerKey(h.ticker) === tickerKey(ticker) && !isDust(h));
    const reasonText = s.reasons.length
      ? `Exit reason${s.reasons.length === 1 ? "" : "s"}: ${s.reasons.map((r) => r.replace(/_/g, " ")).join(", ")}.`
      : "No exit reason recorded.";
    openDialogHtml(`
      <p class="kicker">Exits</p>
      <h3>${escapeHtml(ticker)} <span class="muted">${escapeHtml(first.sector || sectorOf(ticker) || "—")} · ${escapeHtml(tag.text)}</span></h3>
      <p class="take">${escapeHtml(exitWhy(fills, s))} ${escapeHtml(reasonText)}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Realized</p><p class="value ${pnlTone(s.pnl)}">${s.pnl == null ? "—" : fmtMoneySigned(s.pnl)}</p></div>
        <div class="stat"><p class="label">Sold</p><p class="value">${fmtUsd(s.notional)}</p></div>
        <div class="stat"><p class="label">Bought at</p><p class="value">${fmtUsd(first.entry)}</p></div>
        <div class="stat"><p class="label">Still held</p><p class="value">${held ? fmtUsd(marketValue(held.qty, held.last)) : "none"}</p></div>
      </div>
      <h4>Fills · click one for its trade review</h4>
      ${s.sorted.map((c, i) => `<button type="button" class="list-btn" data-fill-idx="${i}">
          <span class="mono muted">${escapeHtml(fmtClock(c.closed_at))}</span>
          <span class="mono">${fmtUsd(marketValue(c.qty, c.exit))} @ ${fmtUsd(c.exit)}</span>
          <span class="mono ${pnlTone(c.pnl)}">${fmtMoneySigned(c.pnl)}</span>
          <span class="chev" aria-hidden="true">›</span>
        </button>`).join("")}`);
    $("dialogBody").querySelectorAll(".list-btn[data-fill-idx]").forEach((btn) => {
      btn.addEventListener("click", () => openClosedDetail(s.sorted[Number(btn.getAttribute("data-fill-idx"))]));
    });
  }

  // ---- Lifetime -------------------------------------------------------------

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
    const stroke = end > 0 ? C.gain : end < 0 ? C.loss : C.muted;
    const zeroY = y(0);
    const first = escapeHtml(String(series[0].at || "").slice(0, 10));
    const last = escapeHtml(String(series[series.length - 1].at || "").slice(0, 10));
    return `<div class="zone-chart"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Cumulative realized PnL">
      <line x1="${pad.l}" x2="${w - pad.r}" y1="${zeroY}" y2="${zeroY}" stroke="#3a4652" stroke-dasharray="3 4"/>
      <text x="4" y="${y(hi) + 3}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono">${hi.toFixed(2)}</text>
      <text x="4" y="${y(lo) + 3}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono">${lo.toFixed(2)}</text>
      <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="1.8"/>
      <text x="${pad.l}" y="${h - 8}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono">${first}</text>
      <text x="${w - pad.r}" y="${h - 8}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono" text-anchor="end">${last}</text>
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
      host.innerHTML = `<div class="empty">No lifetime ticker PnL${filter !== "all" ? " in this theme" : ""}</div>`;
      return;
    }
    host.innerHTML = `
      <table>
        <thead><tr><th>Ticker</th><th class="num">Realized</th><th class="num">Unrealized</th><th class="num">Total</th><th class="num">Closes</th><th>Last activity</th></tr></thead>
        <tbody>
          ${rows
            .map(
              (r, i) => `<tr data-life-idx="${i}" aria-label="Open lifetime PnL for ${escapeHtml(r.ticker)}">
                <td><span class="tick">${escapeHtml(r.ticker)}</span><span class="tick-sub">${escapeHtml(r.sector || sectorOf(r.ticker) || "—")}</span></td>
                <td class="num mono ${pnlTone(r.realized)}">${fmtMoneySigned(r.realized)}</td>
                <td class="num mono ${pnlTone(r.unrealized)}">${r.unrealized == null ? "—" : fmtMoneySigned(r.unrealized)}</td>
                <td class="num mono ${pnlTone(r.total)}">${fmtMoneySigned(r.total)}</td>
                <td class="num mono">${r.close_events != null ? r.close_events : "—"}</td>
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
      ? `<p class="muted" style="margin-top:0.75rem">Reviews: ${r.review_count != null ? r.review_count : reviews.length} · last completeness ${escapeHtml(r.last_completeness || (lastReview && lastReview.completeness) || "—")}</p>`
      : (reviews.length ? `<p class="muted" style="margin-top:0.75rem">Reviews: ${reviews.length}</p>` : "");
    openDialogHtml(`
      <p class="kicker">Lifetime</p>
      <h3>${escapeHtml(r.ticker)} <span class="muted">${escapeHtml(r.sector || sectorOf(r.ticker) || "—")}</span></h3>
      <p class="muted">First buy ${fmtTime(r.first_buy_at)} · last activity ${fmtTime(r.last_activity_at)}</p>
      <div class="grid-stats" style="margin-top:1rem">
        <div class="stat"><p class="label">Realized</p><p class="value ${pnlTone(r.realized)}">${fmtMoneySigned(r.realized)}</p></div>
        <div class="stat"><p class="label">Unrealized</p><p class="value ${pnlTone(r.unrealized)}">${r.unrealized == null ? "—" : fmtMoneySigned(r.unrealized)}</p></div>
        <div class="stat"><p class="label">Total</p><p class="value ${pnlTone(r.total)}">${fmtMoneySigned(r.total)}</p></div>
        <div class="stat"><p class="label">Buys · closes</p><p class="value">${r.buy_events ?? "—"} · ${r.close_events ?? "—"}</p></div>
        <div class="stat"><p class="label">Qty open</p><p class="value">${r.qty_open != null ? Number(r.qty_open).toFixed(6) : "—"}</p></div>
        <div class="stat"><p class="label">Avg cost open</p><p class="value">${fmtUsd(r.avg_cost_open)}</p></div>
      </div>
      <h4>Cumulative realized</h4>
      ${drawLifetimeCumChart(series)}
      ${reviewSummary}
      ${reviewBlockHtml(lastReview, { heading: "Latest close review" })}`);
  }

  // ---- Research -------------------------------------------------------------

  function renderConsensus() {
    const note = $("consensusNote");
    const host = $("consensusGrid");
    const flat = flatAgents();
    if (flat.length) {
      const names = joinAnd(flat.map((f) => AGENT_LABEL[f.agent]));
      const calls = [...new Set(flat.map((f) => f.direction))];
      const confs = flat.map((f) => f.confidence).join(" and ");
      const noData = flat.filter((f) => f.noData).map((f) => AGENT_LABEL[f.agent]);
      note.innerHTML = `<div class="note-warn" role="note">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5"/><path d="M12 18h.01"/></svg>
        <span>${escapeHtml(names)} gave <strong>${escapeHtml(calls.join("/"))} at the same confidence (${escapeHtml(confs)}) on every name</strong>, so ${flat.length === 1 ? "that vote isn't" : "those votes aren't"} telling tickers apart.${noData.length ? ` ${escapeHtml(joinAnd(noData))} found no headlines for any ticker.` : ""}</span>
      </div>`;
    } else {
      note.innerHTML = "";
    }
    const held = holdingsView()
      .filter((r) => !(r.mv != null && r.mv < SMALL_POSITION_USD) && matchesSector(r.theme))
      .map((r) => ({ ticker: r.h.ticker, held: true }));
    const watch = watchRows()
      .filter((w) => matchesSector(w.theme))
      .slice(0, filter === "all" ? 6 : 10)
      .map((w) => ({ ticker: w.ticker, held: false }));
    const rows = held.concat(watch);
    if (!rows.length) {
      host.innerHTML = `<div class="empty">No agent signals${filter !== "all" ? " in this theme" : ""}</div>`;
      return;
    }
    const head = `<span></span>${AGENT_ORDER.map((a) => `<span class="colhead">${AGENT_LABEL[a]}</span>`).join("")}<span class="colhead">Votes</span>`;
    let body = "";
    rows.forEach((row, i) => {
      if (i === held.length && held.length && watch.length) body += `<span class="gap" aria-hidden="true"></span>`;
      const votes = votesForTicker(row.ticker);
      const L = votes.filter((v) => v.direction === "long").length;
      const S = votes.filter((v) => v.direction === "short").length;
      body += `<button type="button" class="rowhead" data-ticker="${escapeHtml(row.ticker)}"><span class="tick">${escapeHtml(row.ticker)}</span><span class="tag${row.held ? " held" : ""}">${row.held ? "held" : "watch"}</span></button>`;
      votes.forEach((v) => {
        const f = voteFill(v);
        const label = v.direction ? `${v.direction === "long" ? "L" : v.direction === "short" ? "S" : "·"} ${v.confidence != null ? Math.round(v.confidence) : ""}` : "—";
        const style = f.cls === "long" || f.cls === "short" ? ` style="background:${f.bg}"` : "";
        const aria = `${row.ticker} ${AGENT_LABEL[v.agent]}: ${v.direction || "no call"}${v.confidence != null ? " " + Math.round(v.confidence) : ""}`;
        body += v.direction
          ? `<button type="button" class="cell ${f.cls}"${style} data-ticker="${escapeHtml(row.ticker)}" data-agent="${v.agent}" aria-label="${escapeHtml(aria)}">${label}</button>`
          : `<span class="cell none" aria-label="${escapeHtml(aria)}">—</span>`;
      });
      body += `<span class="votes ${L > S ? "gain" : S > L ? "loss" : "muted"}">${L} L · ${S} S</span>`;
    });
    host.innerHTML = `<div class="cgrid">${head}${body}</div>`;
    host.querySelectorAll(".rowhead").forEach((b) => b.addEventListener("click", () => openTickerDetail(b.getAttribute("data-ticker"))));
    host.querySelectorAll(".cell[data-agent]").forEach((b) =>
      b.addEventListener("click", () => openTickerDetail(b.getAttribute("data-ticker"), b.getAttribute("data-agent")))
    );
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

    let signals = AGENT_ORDER.flatMap((a) => latestSignals(a)).filter((s) => matchesSector(sectorOf(s.ticker)));
    // Directional calls first, strongest first: the 5-agent neutral wall is in the grid above.
    signals = signals
      .slice()
      .sort((a, b) => (a.direction === "neutral") - (b.direction === "neutral") || Number(b.confidence) - Number(a.confidence))
      .slice(0, 40);
    if (!signals.length) {
      $("researchTable").innerHTML = `<div class="empty">No research signals for this filter</div>`;
    } else {
      $("researchTable").innerHTML = `
        <table>
          <thead><tr><th>Agent</th><th>Ticker</th><th>Dir</th><th class="num">Conf</th><th>Reasoning</th></tr></thead>
          <tbody>
            ${signals
              .map(
                (s, i) => `<tr data-sig-idx="${i}" aria-label="Open ${escapeHtml(s.agent_id)} signal for ${escapeHtml(s.ticker)}">
                  <td class="muted">${escapeHtml(AGENT_LABEL[s.agent_id] || s.agent_id)}</td>
                  <td class="tick">${escapeHtml(s.ticker)}</td>
                  <td><span class="dir ${escapeHtml(s.direction)}">${escapeHtml(s.direction)}</span></td>
                  <td class="num mono">${Number(s.confidence).toFixed(0)}</td>
                  <td class="muted">${escapeHtml(trunc(s.reasoning, 100))}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
      $("researchTable").querySelectorAll("tr[data-sig-idx]").forEach((tr) => {
        const s = signals[Number(tr.getAttribute("data-sig-idx"))];
        bindRowActivation(tr, () => openDetail(s, s.agent_id));
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
                  <td class="mono ${f.severity === "warning" || f.severity === "critical" ? "loss" : ""}">${escapeHtml(f.severity)}</td>
                  <td class="mono muted">${escapeHtml(f.flag_type)}</td>
                  <td>${escapeHtml(trunc(f.message, 120))}</td>
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
            <div><span class="mono">${escapeHtml(e.type)}</span> · <span class="muted">${escapeHtml(e.actor)}</span>
              ${e.ticker ? ` · <span class="tick">${escapeHtml(e.ticker)}</span>` : ""}</div>
            <div class="muted" style="margin-top:0.25rem">${escapeHtml(trunc(e.summary, 180))}</div>
          </div>
        </li>`
      )
      .join("");
  }

  // ---- Watchlist ------------------------------------------------------------

  /** Active names not held, with pressure and their strongest directional vote; closest to a buy first. */
  function watchRows() {
    const held = new Set(holdingsView().map((r) => tickerKey(r.h.ticker)));
    const cold = new Set((DATA?.cold || []).map((c) => tickerKey(c.ticker)));
    return (DATA?.watchlist || [])
      .filter((w) => !held.has(tickerKey(w.ticker)) && !cold.has(tickerKey(w.ticker)))
      .map((w) => {
        const pt = zonePointForTicker(w.ticker);
        const votes = votesForTicker(w.ticker);
        const best = votes
          .filter((v) => v.direction === "long" || v.direction === "short")
          .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))[0] || null;
        return {
          w,
          ticker: w.ticker,
          theme: w.sector || sectorOf(w.ticker),
          buy: Number(pt?.buy) || 0,
          sell: Number(pt?.sell) || 0,
          zone: pt?.zone || null,
          best,
        };
      })
      .sort((a, b) => b.buy - a.buy || a.sell - b.sell || a.ticker.localeCompare(b.ticker));
  }

  function openWatchDetail(w, focusAgent) {
    const theme = w.sector || sectorOf(w.ticker);
    openDialogHtml(`
      <p class="kicker">Watchlist</p>
      <h3>${escapeHtml(w.ticker)} <span class="muted">${escapeHtml(theme || "—")} · not held</span></h3>
      <h4>Pressure zone</h4>
      ${pressureBarsHtml(zonePointForTicker(w.ticker))}
      ${agentReadsHtml(w.ticker, focusAgent)}
      ${lifetimeStatsHtml(w.ticker)}
      ${zoneDetailBlock(w.ticker)}`);
    if (focusAgent) {
      const el = $("dialogBody").querySelector(".read.focus");
      if (el) el.scrollIntoView({ block: "center" });
    }
  }

  function renderWatch() {
    const all = watchRows();
    const rows = all.filter((r) => matchesSector(r.theme));
    const th = zoneThresholds();
    const top = all[0];
    $("watchHint").textContent = top
      ? top.buy >= th.buy
        ? `Active names not held, closest to a buy first. ${top.ticker} is in the buy zone (${top.buy.toFixed(2)}).`
        : `Active names not held, closest to a buy first. Nothing is near one: the top buy pressure is ${top.buy.toFixed(2)} and a buy needs ${th.buy.toFixed(2)}.`
      : "Active names not held, closest to a buy first.";
    const cold = DATA.cold || [];
    $("coldNote").textContent = cold.length
      ? `On ice: ${cold.map((c) => `${c.ticker}${c.theme ? ` (${c.theme})` : ""} until ${fmtDay(c.checkup_date)}`).join(" · ")}.`
      : "";
    if (!rows.length) {
      $("watchTable").innerHTML = `<div class="empty">Watchlist empty${filter !== "all" ? " for this theme" : ""}</div>`;
      return;
    }
    const scale = Math.max(0.6, th.buy * 1.7);
    const mark = ((th.buy / scale) * 100).toFixed(1);
    $("watchTable").innerHTML = `
      <table>
        <thead><tr><th>Ticker</th><th>Agent votes</th><th>Buy pressure <span class="muted">(| = ${th.buy.toFixed(2)} buy line)</span></th><th class="num">Sell</th><th>Strongest directional vote</th></tr></thead>
        <tbody>
          ${rows
            .map((r, i) => `<tr data-watch-idx="${i}" aria-label="Open details for ${escapeHtml(r.ticker)}">
                <td><span class="tick">${escapeHtml(r.ticker)}</span><span class="tick-sub">${escapeHtml(r.theme || "—")}</span><span class="chev" aria-hidden="true">›</span></td>
                <td>${dotsHtml(r.ticker)}</td>
                <td><div class="wbar"><div class="track"><div class="fill buy" style="width:${Math.min(100, (r.buy / scale) * 100).toFixed(1)}%"></div><span class="mark" style="left:${mark}%" aria-hidden="true"></span></div><span class="num">${r.buy.toFixed(2)}</span></div></td>
                <td class="num mono ${r.sell >= th.sell * 0.7 ? "loss" : ""}">${r.sell.toFixed(2)}</td>
                <td class="${r.best ? (r.best.direction === "long" ? "gain" : "loss") : "subtle"}">${r.best ? `${AGENT_LABEL[r.best.agent]} · ${r.best.direction} ${Math.round(r.best.confidence || 0)}` : "no directional vote"}</td>
              </tr>`)
            .join("")}
        </tbody>
      </table>`;
    $("watchTable").querySelectorAll("tr[data-watch-idx]").forEach((tr) => {
      const idx = Number(tr.getAttribute("data-watch-idx"));
      bindRowActivation(tr, () => openWatchDetail(rows[idx].w));
    });
  }

  // ---- Zones ----------------------------------------------------------------

  function renderZones() {
    const host = $("zonesScatter");
    const legend = $("zonesLegend");
    if (!host) return;
    const all = DATA?.zones?.points || [];
    const points = all.filter((p) => matchesSector(p.sector || sectorOf(p.ticker)));
    const counts = { buy_zone: 0, sell_zone: 0, conflicted: 0, quiet: 0 };
    points.forEach((p) => { counts[p.zone] = (counts[p.zone] || 0) + 1; });
    if (legend) {
      legend.innerHTML = [
        ["buy_zone", "Buy"],
        ["sell_zone", "Sell"],
        ["conflicted", "Conflicted"],
        ["quiet", "Quiet"],
      ]
        .map(([z, lab]) => `<span class="chip zone-badge ${z}">${lab} ${counts[z] || 0}</span>`)
        .join("");
    }
    if (!points.length) {
      host.innerHTML = `<div class="empty">No zone points${filter !== "all" ? " for this theme" : ""}</div>`;
      return;
    }
    const w = 720;
    const h = 460;
    const pad = { t: 28, r: 28, b: 44, l: 52 };
    const th = zoneThresholds();
    const xMax = Math.max(zoneNiceMax(points.map((p) => p.sell)), th.sell * 1.15);
    const yMax = Math.max(zoneNiceMax(points.map((p) => p.buy)), th.buy * 1.15);
    // Square-root axes spread the crowd near zero without hiding the 0.35 lines.
    const sx = (v) => Math.sqrt(Math.min(Math.max(Number(v) || 0, 0), xMax) / xMax);
    const sy = (v) => Math.sqrt(Math.min(Math.max(Number(v) || 0, 0), yMax) / yMax);
    const xOf = (sell) => pad.l + sx(sell) * (w - pad.l - pad.r);
    const yOf = (buy) => pad.t + (1 - sy(buy)) * (h - pad.t - pad.b);
    const xT = xOf(th.sell);
    const yT = yOf(th.buy);
    const quads = `
      <rect x="${pad.l}" y="${pad.t}" width="${xT - pad.l}" height="${yT - pad.t}" fill="rgb(134 208 160 / 0.06)"/>
      <rect x="${xT}" y="${yT}" width="${w - pad.r - xT}" height="${h - pad.b - yT}" fill="rgb(239 154 134 / 0.06)"/>
      <rect x="${xT}" y="${pad.t}" width="${w - pad.r - xT}" height="${yT - pad.t}" fill="rgb(224 180 110 / 0.05)"/>
      <line x1="${xT}" x2="${xT}" y1="${pad.t}" y2="${h - pad.b}" stroke="#3a4652" stroke-dasharray="4 5"/>
      <line x1="${pad.l}" x2="${w - pad.r}" y1="${yT}" y2="${yT}" stroke="#3a4652" stroke-dasharray="4 5"/>`;
    const dots = points
      .map((p, i) => {
        const cx = xOf(p.sell).toFixed(1);
        const cy = yOf(p.buy).toFixed(1);
        const r = p.held ? 6.5 : 5;
        const stroke = p.zone === "buy_zone" ? C.gain : p.zone === "sell_zone" ? C.loss : p.zone === "conflicted" ? C.warn : C.subtle;
        const fill = p.held ? C.accent : "#07090c";
        const label = p.held ? `<text class="zones-label" x="${Number(cx) + 9}" y="${Number(cy) + 3}">${escapeHtml(p.ticker)}</text>` : "";
        return `<g class="zones-scatter-point" data-zone-idx="${i}" tabindex="0" role="button" aria-label="${escapeHtml(p.ticker)} buy ${Number(p.buy).toFixed(2)} sell ${Number(p.sell).toFixed(2)}${p.held ? " held" : ""}">
          <circle cx="${cx}" cy="${cy}" r="${r}" fill="${fill}" stroke="${stroke}" stroke-width="1.8"/>
          ${label}
          <title>${escapeHtml(p.ticker)} · buy ${Number(p.buy).toFixed(3)} · sell ${Number(p.sell).toFixed(3)} · ${escapeHtml(p.zone)}</title>
        </g>`;
      })
      .join("");
    const tick = (v, axis) => axis === "x"
      ? `<text x="${xOf(v)}" y="${h - pad.b + 16}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono" text-anchor="middle">${v.toFixed(2)}</text>`
      : `<text x="${pad.l - 8}" y="${yOf(v) + 3}" fill="${C.subtle}" font-size="10" font-family="IBM Plex Mono" text-anchor="end">${v.toFixed(2)}</text>`;
    host.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Buy vs sell pressure scatter">
      <rect x="${pad.l}" y="${pad.t}" width="${w - pad.l - pad.r}" height="${h - pad.t - pad.b}" fill="#0c1014" stroke="${C.line}"/>
      ${quads}
      <text class="zones-quad-label" x="${pad.l + 8}" y="${pad.t + 16}">Buy zone</text>
      <text class="zones-quad-label" x="${w - pad.r - 8}" y="${pad.t + 16}" text-anchor="end">Conflicted</text>
      <text class="zones-quad-label" x="${pad.l + 8}" y="${h - pad.b - 8}">Quiet</text>
      <text class="zones-quad-label" x="${w - pad.r - 8}" y="${h - pad.b - 8}" text-anchor="end">Sell zone</text>
      <text x="${(pad.l + w - pad.r) / 2}" y="${h - 8}" fill="${C.muted}" font-size="11" font-family="IBM Plex Mono" text-anchor="middle">Sell pressure →</text>
      <text x="14" y="${(pad.t + h - pad.b) / 2}" fill="${C.muted}" font-size="11" font-family="IBM Plex Mono" text-anchor="middle" transform="rotate(-90 14,${(pad.t + h - pad.b) / 2})">Buy pressure →</text>
      ${tick(0, "x")}${tick(th.sell, "x")}${tick(xMax, "x")}
      ${tick(0, "y")}${tick(th.buy, "y")}${tick(yMax, "y")}
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


  function minerviniLabel(sd) {
    if (!sd || sd.minervini_passes == null || sd.minervini_passes === "") return "";
    const n = Number(sd.minervini_passes);
    if (!Number.isFinite(n)) return "";
    const rs =
      sd.rs_proxy != null && sd.rs_proxy !== ""
        ? ` · rs_proxy ${Number(sd.rs_proxy).toFixed(0)}`
        : "";
    return `<p class="mono" style="margin-top:0.65rem">Minervini ${n}/10${rs} <span class="muted">(soft)</span></p>`;
  }

  function obvChandelierLabel(sd) {
    if (!sd) return "";
    const bits = [];
    if (sd.obv != null && sd.obv !== "") {
      const slope = Number(sd.obv_slope);
      const slopeBit = Number.isFinite(slope)
        ? slope > 0
          ? " rising"
          : slope < 0
            ? " falling"
            : " flat"
        : "";
      bits.push(`OBV${slopeBit}`);
    }
    if (sd.price_vs_chandelier != null && sd.price_vs_chandelier !== "") {
      const vs = Number(sd.price_vs_chandelier);
      if (Number.isFinite(vs)) {
        bits.push(vs >= 0 ? "Chandelier above" : "Chandelier through");
      }
    } else if (sd.chandelier_long != null && sd.chandelier_long !== "") {
      bits.push("Chandelier");
    }
    if (!bits.length) return "";
    return `<p class="mono" style="margin-top:0.35rem">${bits.join(" · ")} <span class="muted">(soft)</span></p>`;
  }

  function openDetail(signal, agentId) {
    openDialogHtml(`
      <p class="kicker">${escapeHtml(AGENT_LABEL[agentId || signal.agent_id] || agentId || signal.agent_id || "agent")}</p>
      <h3>${escapeHtml(signal.ticker)} <span class="dir ${escapeHtml(signal.direction)}">${escapeHtml(signal.direction)}</span>
        <span class="mono"> ${Number(signal.confidence).toFixed(1)}</span></h3>
      <p class="muted">${fmtTime(signal.created_at)}</p>
      <p style="margin-top:1rem">${escapeHtml(signal.reasoning || "")}</p>
      ${minerviniLabel(signal.supporting_data)}
      ${obvChandelierLabel(signal.supporting_data)}
      <p style="margin-top:1rem"><button type="button" class="btn" id="openTickerFromSignal">All agents on ${escapeHtml(signal.ticker)} ›</button></p>
      <h4>Supporting data</h4>
      <pre>${escapeHtml(JSON.stringify(signal.supporting_data || {}, null, 2))}</pre>`);
    $("openTickerFromSignal").addEventListener("click", () => openTickerDetail(signal.ticker, agentId || signal.agent_id));
  }

  function closeDetail() {
    const dlg = $("dialog");
    if (dlg.hidden) return;
    dlg.classList.remove("open");
    dlg.hidden = true;
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
    lastFocus = null;
  }

  function renderFilteredSections() {
    renderFilters();
    renderConsensus();
    renderWatch();
    renderZones();
    renderResearch();
    renderLifetime();
    renderMission();
  }

  function renderAll() {
    renderFreshness();
    renderToday();
    renderStats();
    renderHoldings();
    renderSectors();
    renderClosed();
    renderResearchDayLabels();
    renderFilteredSections();
  }

  function wireNav() {
    const links = [...document.querySelectorAll("#nav a")];
    const targets = links.map((a) => document.getElementById(a.getAttribute("href").slice(1))).filter(Boolean);
    const onScroll = () => {
      let current = targets[0]?.id;
      for (const t of targets) {
        if (t.getBoundingClientRect().top <= 140) current = t.id;
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
    $("smallToggle").addEventListener("click", () => {
      showSmall = !showSmall;
      renderHoldings();
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
