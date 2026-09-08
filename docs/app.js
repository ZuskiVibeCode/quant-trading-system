/* APHELION Orbit viewer — loads ./data/latest.json (Vercel static) */
(() => {
  const DATA_URL = "./data/latest.json";
  // Compact single-file deploys may define INLINE_DATA before this script.
  const HAS_INLINE = typeof INLINE_DATA !== "undefined" && INLINE_DATA;
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
  const SECTOR_LABEL = {
    quantum: "Quantum",
    robotics: "Robotics",
    space: "Space",
  };

  let DATA = null;
  let filter = "all";
  let query = "";
  let selectedAgent = null;

  const $ = (id) => document.getElementById(id);

  function fmtUsd(n) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    }).format(Number(n));
  }

  function fmtPct(n, alreadyPct = false) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    const v = alreadyPct ? Number(n) : Number(n) * 100;
    return `${v.toFixed(2)}%`;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return (
        d.toLocaleString("en-US", {
          timeZone: "America/New_York",
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        }) + " ET"
      );
    } catch {
      return iso;
    }
  }

  function fmtDateShort(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso.length <= 10 ? iso + "T12:00:00Z" : iso);
      return d.toLocaleDateString("en-US", {
        timeZone: "America/New_York",
        month: "short",
        day: "numeric",
        year: "numeric",
      });
    } catch {
      return iso;
    }
  }

  function trunc(s, n = 140) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function pnlClass(n) {
    if (n == null) return "";
    return Number(n) >= 0 ? "gain" : "loss";
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

  function matchesQuery(...parts) {
    if (!query) return true;
    const n = query.toLowerCase();
    return parts.some((p) => String(p || "").toLowerCase().includes(n));
  }

  function setHeroArt() {
    const img = $("heroArt");
    if (!img) return;
    const src = ART[filter] || ART.all;
    img.onerror = () => {
      if (img.src.indexOf("observatory") === -1) img.src = ART.all;
    };
    img.src = src;
  }

  function eyeIcon() {
    return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>`;
  }
  function scopeIcon() {
    return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/><circle cx="12" cy="12" r="8"/></svg>`;
  }
  function forkIcon() {
    return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M6 8v2a4 4 0 0 0 4 4h0a4 4 0 0 0 4-4V8M12 14v2"/></svg>`;
  }

  function renderFilters() {
    const el = $("filters");
    el.innerHTML = "";
    SECTORS.forEach((key) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip sector";
      btn.textContent = key === "all" ? "All" : SECTOR_LABEL[key] || key;
      btn.setAttribute("aria-pressed", filter === key ? "true" : "false");
      btn.addEventListener("click", () => {
        filter = key;
        setHeroArt();
        renderAll();
      });
      el.appendChild(btn);
    });
  }

  function renderHeroBadges() {
    const snap = DATA.snapshot || {};
    const meta = DATA.meta || {};
    const asOf = fmtDateShort(meta.exported_at || meta.day_start_utc);
    const bits = [
      `<span class="chip paper">Paper only</span>`,
      `<span class="chip vercel">Deployed on Vercel</span>`,
      `<span class="chip">As of ${asOf}</span>`,
      snap.halted || snap.day_halted
        ? `<span class="chip halt">Halted</span>`
        : `<span class="chip active">Book live</span>`,
    ];
    $("heroBadges").innerHTML = bits.join("");
  }

  function renderFrostBar() {
    const meta = DATA.meta || {};
    const watchN = (DATA.watchlist || []).length;
    const posN = (DATA.holdings || []).length;
    const closedN = (DATA.closed || []).length;
    $("paperStatus").textContent = meta.paper
      ? `Paper · start ${fmtUsd(meta.starting_equity)} · Vercel`
      : "Vercel";
    $("countPills").innerHTML = `
      <span class="count-pill">${eyeIcon()} Watch ${watchN}</span>
      <span class="count-pill">${scopeIcon()} Positions ${posN}</span>
      <span class="count-pill">${forkIcon()} Closed ${closedN}</span>`;
  }

  function renderMissionBrief() {
    const s = DATA.snapshot || {};
    const meta = DATA.meta || {};
    const agents = DATA.agents_today || {};
    const sigTotal = Object.values(agents).reduce((a, b) => a + (b.count || 0), 0);
    $("missionBriefText").textContent =
      `Paper $100 multi-agent orbit book. Equity ${fmtUsd(s.equity)}, cash ${fmtUsd(s.cash)}, ` +
      `${(DATA.holdings || []).length} open · ${(DATA.closed || []).length} closed · ` +
      `${sigTotal} agent signals today · exported ${fmtTime(meta.exported_at)}.`;
  }

  function sleeveValues() {
    const holdings = DATA.holdings || [];
    const out = { quantum: 0, robotics: 0, space: 0 };
    let total = 0;
    holdings.forEach((h) => {
      const sec = h.sector || sectorOf(h.ticker);
      const mv =
        h.last != null && h.qty != null
          ? Number(h.last) * Number(h.qty)
          : h.unrealized != null && h.avg_cost != null
            ? Number(h.avg_cost) * Number(h.qty) + Number(h.unrealized)
            : Number(h.avg_cost || 0) * Number(h.qty || 0);
      if (sec && out[sec] != null) out[sec] += mv;
      total += mv;
    });
    return { out, total };
  }

  function renderSnapshot() {
    const s = DATA.snapshot || {};
    const meta = DATA.meta || {};
    const cards = [
      { label: "Equity", value: fmtUsd(s.equity) },
      { label: "Cash", value: fmtUsd(s.cash) },
      { label: "Daily PnL", value: fmtUsd(s.daily_pnl), cls: pnlClass(s.daily_pnl) },
      {
        label: "Drawdown",
        value: fmtPct(s.drawdown),
        cls: Number(s.drawdown) > 0 ? "loss" : "",
      },
    ];
    $("statGrid").innerHTML = cards
      .map(
        (c) =>
          `<div class="stat"><p class="label">${c.label}</p><p class="value ${c.cls || ""}">${c.value}</p></div>`
      )
      .join("");

    const curve = DATA.equity_curve || [];
    if (curve.length) {
      $("curveMeta").textContent = `${fmtDateShort(curve[0].t)} – ${fmtDateShort(curve[curve.length - 1].t)}`;
    } else {
      $("curveMeta").textContent = `Start ${fmtUsd(meta.starting_equity)}`;
    }
    drawEquityChart(curve);
    renderSleeve();
  }

  function renderSleeve() {
    const { out, total } = sleeveValues();
    const denom = total > 0 ? total : 1;
    const keys = ["quantum", "robotics", "space"];
    const bar = keys
      .map((k) => {
        const pct = total > 0 ? (out[k] / denom) * 100 : 0;
        return `<div class="sleeve-seg ${k}" style="width:${pct}%"></div>`;
      })
      .join("");
    const list = keys
      .map((k) => {
        const pct = total > 0 ? (out[k] / denom) * 100 : 0;
        return `<li>
          <img class="sleeve-thumb" src="${ART[k]}" alt="" />
          <div class="sleeve-meta">
            <div class="row"><p class="name">${SECTOR_LABEL[k]}</p><p class="pct">${pct.toFixed(1)}%</p></div>
            <p class="usd">${fmtUsd(out[k])}</p>
          </div>
        </li>`;
      })
      .join("");
    $("sleeveMix").innerHTML = `<div class="sleeve-bar">${bar}</div><ul class="sleeve-list">${list}</ul>`;
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

  function sparkline(history) {
    if (!history || !history.length) {
      return `<div class="empty" style="padding:1rem">No history days yet</div>`;
    }
    const w = 480;
    const h = 88;
    const pad = { t: 8, r: 8, b: 18, l: 8 };
    const vals = history.map((d) => Number(d.signal_count));
    const max = Math.max(...vals, 1);
    const x = (i) => pad.l + (i / Math.max(history.length - 1, 1)) * (w - pad.l - pad.r);
    const y = (v) => pad.t + (1 - v / max) * (h - pad.t - pad.b);
    const pts = history.map((d, i) => `${x(i).toFixed(1)},${y(Number(d.signal_count)).toFixed(1)}`);
    const conf = history.map((d) => Number(d.avg_confidence));
    const cmax = Math.max(...conf, 1);
    const cpts = history.map((d, i) => `${x(i).toFixed(1)},${y((Number(d.avg_confidence) / cmax) * max).toFixed(1)}`);
    return `
      <div class="spark-wrap">
        <svg viewBox="0 0 ${w} ${h}" aria-label="Agent history">
          <polyline points="${pts.join(" ")}" fill="none" stroke="#5eead4" stroke-width="1.75"/>
          <polyline points="${cpts.join(" ")}" fill="none" stroke="#8fb4c4" stroke-width="1" stroke-dasharray="3 3" opacity="0.7"/>
          <text x="${pad.l}" y="${h - 4}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono">${history[0].date}</text>
          <text x="${w - pad.r}" y="${h - 4}" fill="#5c656e" font-size="10" font-family="IBM Plex Mono" text-anchor="end">${history[history.length - 1].date}</text>
        </svg>
        <p class="meta-line" style="margin-top:0.35rem">Solid = signal count · dashed = avg confidence (scaled)</p>
      </div>`;
  }


  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function signalListHtml(signals) {
    if (!signals || !signals.length) {
      return `<p class="empty tight">No linked agent signals in Tracker</p>`;
    }
    return `<ul class="reason-signals">${signals
      .map(
        (s) => `<li>
          <div class="signal-top">
            <span class="mono">${escapeHtml(s.agent_id || "agent")}</span>
            <span>
              <span class="dir ${escapeHtml(s.direction || "")}">${escapeHtml(s.direction || "—")}</span>
              <span class="mono subtle"> ${Number(s.confidence || 0).toFixed(0)}</span>
            </span>
          </div>
          <p class="signal-reason">${escapeHtml(s.reasoning || "")}</p>
          <p class="meta-line">${fmtTime(s.created_at)}</p>
        </li>`
      )
      .join("")}</ul>`;
  }

  function ticketBlockHtml(label, bundle) {
    const ticket = bundle?.ticket;
    const fill = bundle?.fill;
    const signals = bundle?.signals || [];
    if (!ticket && !fill && !signals.length) {
      return `<div class="reason-block"><p class="kicker">${escapeHtml(label)}</p><p class="empty tight">No stored ticket / fill / signals</p></div>`;
    }
    return `<div class="reason-block">
      <p class="kicker">${escapeHtml(label)}</p>
      ${
        ticket
          ? `<p class="thesis">${escapeHtml(ticket.rationale || "")}</p>
             <div class="meta-row">
               <span>ticket #${ticket.id}</span>
               <span>${escapeHtml(ticket.side || "")}</span>
               <span>${Number(ticket.qty || 0).toFixed(4)} sh</span>
               <span>${escapeHtml(ticket.status || "")}</span>
               <span>${fmtTime(ticket.created_at)}</span>
             </div>`
          : `<p class="empty tight">No PM ticket linked</p>`
      }
      ${
        fill
          ? `<div class="meta-row" style="margin-top:0.45rem">
               <span>fill #${fill.id}</span>
               <span>${escapeHtml(fill.side)}</span>
               <span>${Number(fill.qty).toFixed(4)} @ ${fmtUsd(fill.price)}</span>
               <span>${escapeHtml(fill.mode || "")}</span>
               <span>${fmtTime(fill.created_at)}</span>
             </div>`
          : `<p class="muted" style="margin-top:0.35rem;font-size:0.85rem">No fill row (may be reconcile-only)</p>`
      }
      <p class="kicker" style="margin-top:0.85rem">Agent signals</p>
      ${signalListHtml(signals)}
    </div>`;
  }

  function openPositionDetail(item, kind) {
    const dlg = $("dialog");
    const body = $("dialogBody");
    const sec = item.sector || sectorOf(item.ticker);
    const isClosed = kind === "closed";
    const titleBits = isClosed
      ? `Closed · ${fmtUsd(item.pnl)} PnL`
      : `Open · avg ${fmtUsd(item.avg_cost)}`;
    body.innerHTML = `
      <p class="kicker">${isClosed ? "Closed position" : "Open position"}${sec ? ` · ${SECTOR_LABEL[sec] || sec}` : ""}</p>
      <h3>${escapeHtml(item.ticker)} <span class="mono subtle" style="font-size:0.85rem">${escapeHtml(titleBits)}</span></h3>
      <div class="meta-row" style="margin-top:0.5rem">
        <span>${Number(item.qty || 0).toFixed(4)} sh</span>
        ${isClosed ? `<span>in ${fmtUsd(item.entry)}</span><span>out ${fmtUsd(item.exit)}</span>` : `<span>last ${fmtUsd(item.last)}</span><span class="${pnlClass(item.unrealized)}">${fmtUsd(item.unrealized)}</span>`}
        <span>${fmtTime(isClosed ? item.closed_at : item.updated_at || item.opened_at)}</span>
      </div>
      ${ticketBlockHtml(isClosed ? "Why bought" : "Buy reasoning", item.buy)}
      ${isClosed ? ticketBlockHtml("Why sold", item.sell) : ""}
      ${
        !isClosed && Array.isArray(item.fills) && item.fills.length
          ? `<div class="reason-block"><p class="kicker">Fills</p><ul class="reason-signals">${item.fills
              .map(
                (f) =>
                  `<li><div class="meta-row"><span>#${f.id}</span><span>${escapeHtml(f.side)}</span><span>${Number(f.qty).toFixed(4)} @ ${fmtUsd(f.price)}</span><span>${fmtTime(f.created_at)}</span></div></li>`
              )
              .join("")}</ul></div>`
          : ""
      }`;
    dlg.hidden = false;
    dlg.classList.add("open");
  }

  function renderHoldings() {
    const rows = (DATA.holdings || []).filter((h) => {
      const thesis = h.buy?.ticket?.rationale || "";
      const agents = (h.buy?.signals || []).map((s) => s.reasoning || "").join(" ");
      return (
        matchesSector(h.sector || sectorOf(h.ticker)) &&
        matchesQuery(h.ticker, h.sector, thesis, agents)
      );
    });
    const host = $("holdingsCards");
    if (!rows.length) {
      host.innerHTML = `<p class="empty">No open holdings${filter !== "all" ? " in this sector" : ""}${query ? " matching search" : ""}</p>`;
      return;
    }
    host.innerHTML = rows
      .map((h, idx) => {
        const sec = h.sector || sectorOf(h.ticker);
        const mv =
          h.last != null ? Number(h.last) * Number(h.qty) : Number(h.avg_cost) * Number(h.qty);
        const thesis = h.buy?.ticket?.rationale
          ? trunc(h.buy.ticket.rationale, 140)
          : `Open position · avg ${fmtUsd(h.avg_cost)}`;
        return `<button type="button" class="data-card" data-kind="holding" data-idx="${idx}">
          <div class="top">
            <div>
              <span class="ticker">${h.ticker}</span>
              ${sec ? `<span class="sector-tag"> · ${SECTOR_LABEL[sec] || sec}</span>` : ""}
              <p class="thesis">${escapeHtml(thesis)}</p>
            </div>
            <div class="side-stats">
              <span>${fmtUsd(mv)}</span>
              <span class="${pnlClass(h.unrealized)}">${fmtUsd(h.unrealized)}</span>
            </div>
          </div>
          <div class="meta-row">
            <span>${Number(h.qty).toFixed(4)} sh</span>
            <span>avg ${fmtUsd(h.avg_cost)}</span>
            <span>last ${fmtUsd(h.last)}</span>
            <span>${fmtTime(h.updated_at || h.opened_at)}</span>
            <span class="subtle">tap for reasoning</span>
          </div>
        </button>`;
      })
      .join("");
    host.querySelectorAll(".data-card").forEach((el) => {
      el.addEventListener("click", () => {
        const i = Number(el.getAttribute("data-idx"));
        openPositionDetail(rows[i], "holding");
      });
    });
  }

  function renderClosed() {
    const rows = (DATA.closed || []).filter((h) => {
      const buyR = h.buy?.ticket?.rationale || "";
      const sellR = h.sell?.ticket?.rationale || "";
      const agents = [...(h.buy?.signals || []), ...(h.sell?.signals || [])]
        .map((s) => s.reasoning || "")
        .join(" ");
      return (
        matchesSector(h.sector || sectorOf(h.ticker)) &&
        matchesQuery(h.ticker, h.sector, buyR, sellR, agents)
      );
    });
    const host = $("closedCards");
    if (!rows.length) {
      host.innerHTML = `<p class="empty">No closed trades${filter !== "all" ? " in this sector" : ""}${query ? " matching search" : ""}</p>`;
      return;
    }
    host.innerHTML = rows
      .map((h, idx) => {
        const sec = h.sector || sectorOf(h.ticker);
        const thesis = h.sell?.ticket?.rationale
          ? trunc(h.sell.ticket.rationale, 140)
          : `${h.mode || "fill"} · exit ${fmtUsd(h.exit)}`;
        return `<button type="button" class="data-card" data-kind="closed" data-idx="${idx}">
          <div class="top">
            <div>
              <span class="ticker">${h.ticker}</span>
              ${sec ? `<span class="sector-tag"> · ${SECTOR_LABEL[sec] || sec}</span>` : ""}
              <p class="thesis">${escapeHtml(thesis)}</p>
            </div>
            <div class="side-stats">
              <span class="${pnlClass(h.pnl)}">${fmtUsd(h.pnl)}</span>
            </div>
          </div>
          <div class="meta-row">
            <span>${Number(h.qty).toFixed(4)} sh</span>
            <span>in ${fmtUsd(h.entry)}</span>
            <span>out ${fmtUsd(h.exit)}</span>
            <span>${fmtTime(h.closed_at)}</span>
            <span class="subtle">tap for buy/sell why</span>
          </div>
        </button>`;
      })
      .join("");
    host.querySelectorAll(".data-card").forEach((el) => {
      el.addEventListener("click", () => {
        const i = Number(el.getAttribute("data-idx"));
        openPositionDetail(rows[i], "closed");
      });
    });
  }

  function filteredSignals(block) {
    let signals = block.signals || [];
    if (filter !== "all") {
      signals = signals.filter((s) => matchesSector(sectorOf(s.ticker)));
    }
    if (query) {
      signals = signals.filter((s) =>
        matchesQuery(s.ticker, s.direction, s.reasoning, block.agent_id)
      );
    }
    return signals;
  }

  function renderAgents() {
    const agents = DATA.agents_today || {};
    const host = $("agentGrid");
    host.innerHTML = "";
    AGENT_ORDER.forEach((aid) => {
      const block = agents[aid] || {
        agent_id: aid,
        count: 0,
        signals: [],
        long: 0,
        short: 0,
        neutral: 0,
      };
      const signals = filteredSignals(block);
      const card = document.createElement("button");
      card.type = "button";
      card.className = "agent-card" + (selectedAgent === aid ? " selected" : "");
      card.innerHTML = `
        <header>
          <div>
            <h3>${aid.replace(/_/g, " ")}</h3>
            <div class="counts">${signals.length} shown · ${block.count || 0} today · L${block.long || 0}/S${block.short || 0}/N${block.neutral || 0}</div>
          </div>
          <span class="status-dot" title="online"></span>
        </header>`;
      const list = document.createElement("ul");
      list.className = "signal-list";
      if (!signals.length) {
        list.innerHTML = `<li class="empty" style="padding:1rem">No signals</li>`;
      } else {
        signals.slice(0, 8).forEach((s) => {
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
          btn.addEventListener("click", (e) => {
            e.stopPropagation();
            openDetail(s, aid);
          });
          li.appendChild(btn);
          list.appendChild(li);
        });
      }
      card.appendChild(list);
      card.addEventListener("click", () => {
        selectedAgent = selectedAgent === aid ? null : aid;
        renderAgents();
        renderAgentDetail();
      });
      host.appendChild(card);
    });
    renderAgentDetail();
  }

  function renderAgentDetail() {
    const panel = $("agentDetail");
    if (!selectedAgent) {
      panel.hidden = true;
      panel.innerHTML = "";
      return;
    }
    const block = (DATA.agents_today || {})[selectedAgent] || { signals: [], count: 0 };
    const history = ((DATA.agents_history || {})[selectedAgent] || []).slice(-30);
    const signals = filteredSignals(block);
    panel.hidden = false;
    panel.innerHTML = `
      <div class="detail-head">
        <div>
          <p class="kicker">Agent telemetry</p>
          <h3>${selectedAgent.replace(/_/g, " ")}</h3>
        </div>
        <button type="button" class="chip" id="agentDetailClose">Close</button>
      </div>
      ${sparkline(history)}
      <div class="history-stats">
        ${history
          .slice(-5)
          .map(
            (d) =>
              `<span class="chip">${d.date}: ${d.signal_count} · μ${Number(d.avg_confidence).toFixed(0)} · L${d.long}/S${d.short}/N${d.neutral}</span>`
          )
          .join("")}
      </div>
      <p class="kicker" style="margin-bottom:0.5rem">Today's list</p>
      <ul class="signal-list" style="max-height:22rem" id="agentDetailList"></ul>`;
    $("agentDetailClose").addEventListener("click", () => {
      selectedAgent = null;
      renderAgents();
    });
    const list = $("agentDetailList");
    if (!signals.length) {
      list.innerHTML = `<li class="empty">No signals for filter</li>`;
    } else {
      signals.forEach((s) => {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "signal-item";
        btn.innerHTML = `
          <div class="signal-top">
            <span class="mono">${s.ticker}</span>
            <span><span class="dir ${s.direction}">${s.direction}</span>
            <span class="mono subtle"> ${Number(s.confidence).toFixed(1)}</span></span>
          </div>
          <p class="signal-reason">${trunc(s.reasoning, 200)}</p>`;
        btn.addEventListener("click", () => openDetail(s, selectedAgent));
        li.appendChild(btn);
        list.appendChild(li);
      });
    }
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

    let signals = flattenSignals().filter(
      (s) =>
        matchesSector(sectorOf(s.ticker)) &&
        matchesQuery(s.ticker, s.agent_id, s.direction, s.reasoning)
    );
    signals = signals.slice(0, 40);
    const host = $("researchCards");
    if (!signals.length) {
      host.innerHTML = `<p class="empty">No research signals for filter</p>`;
    } else {
      host.innerHTML = signals
        .map(
          (s) => `<button type="button" class="data-card" data-sid="${s.signal_id || ""}">
            <div class="top">
              <div>
                <span class="ticker">${s.ticker}</span>
                <span class="sector-tag"> · ${s.agent_id}</span>
              </div>
              <span class="dir ${s.direction}">${s.direction}</span>
            </div>
            <p class="thesis">${trunc(s.reasoning, 160)}</p>
            <div class="meta-row">
              <span>conf ${Number(s.confidence).toFixed(0)}</span>
              <span>${fmtTime(s.created_at)}</span>
            </div>
          </button>`
        )
        .join("");
      host.querySelectorAll(".data-card").forEach((el, i) => {
        el.addEventListener("click", () => openDetail(signals[i], signals[i].agent_id));
      });
    }

    const flags = (DATA.auditor_flags_today || []).filter((f) => {
      if (!matchesQuery(f.message, f.flag_type, f.severity)) return false;
      if (filter === "all") return true;
      const m = String(f.message || "");
      return (DATA.sectors?.[filter] || []).some((t) => m.includes(t));
    });
    const fh = $("flagsCards");
    if (!flags.length) {
      fh.innerHTML = `<p class="empty">No auditor flags today</p>`;
    } else {
      fh.innerHTML = flags
        .slice(0, 20)
        .map(
          (f) => `<div class="data-card" style="cursor:default">
            <div class="top">
              <span class="ticker ${f.severity === "warning" || f.severity === "critical" ? "loss" : ""}">${f.severity}</span>
              <span class="sector-tag">${f.flag_type}</span>
            </div>
            <p class="thesis">${trunc(f.message, 160)}</p>
            <div class="meta-row"><span>${fmtTime(f.created_at)}</span></div>
          </div>`
        )
        .join("");
    }
  }

  function renderMission() {
    const log = (DATA.mission_log || []).filter((e) => {
      if (!matchesQuery(e.type, e.actor, e.ticker, e.summary)) return false;
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
            <div class="kind">${e.type}${e.ticker ? ` · ${e.ticker}` : ""}</div>
            <div class="muted" style="margin-top:0.2rem;font-size:0.88rem">${trunc(e.summary, 180)}</div>
            <div class="subtle mono" style="margin-top:0.2rem;font-size:0.65rem">${e.actor || ""}</div>
          </div>
        </li>`
      )
      .join("");
  }

  function renderWatch() {
    const rows = (DATA.watchlist || []).filter(
      (w) =>
        matchesSector(w.sector || sectorOf(w.ticker)) &&
        matchesQuery(w.ticker, w.sector, w.signal?.reasoning, w.signal?.agent_id)
    );
    const host = $("watchCards");
    if (!rows.length) {
      host.innerHTML = `<p class="empty">Watchlist empty for filter</p>`;
      return;
    }
    host.innerHTML = rows
      .map((w) => {
        const s = w.signal;
        const sec = w.sector || sectorOf(w.ticker) || "space";
        return `<button type="button" class="data-card watch-card">
          <img class="thumb" src="${ART[sec] || ART.all}" alt="" />
          <div style="flex:1;min-width:0">
            <div class="top" style="justify-content:flex-start;gap:0.5rem">
              <span class="ticker">${w.ticker}</span>
              <span class="sector-tag">${SECTOR_LABEL[sec] || sec || "—"}</span>
              ${s ? `<span class="dir ${s.direction}">${s.direction}</span>` : ""}
            </div>
            <p class="thesis">${s ? trunc(s.reasoning, 140) : "No signal today"}</p>
            <div class="meta-row">
              <span>${s?.agent_id || "—"}</span>
              <span>${s ? `conf ${Number(s.confidence).toFixed(0)}` : ""}</span>
              <span>${s ? fmtTime(s.created_at) : ""}</span>
            </div>
          </div>
        </button>`;
      })
      .join("");
    host.querySelectorAll(".data-card").forEach((el, i) => {
      el.addEventListener("click", () => {
        const w = rows[i];
        if (w.signal) openDetail({ ...w.signal, ticker: w.ticker }, w.signal.agent_id);
      });
    });
  }

  function openDetail(signal, agentId) {
    const dlg = $("dialog");
    const body = $("dialogBody");
    body.innerHTML = `
      <p class="kicker">${agentId || signal.agent_id || "agent"}</p>
      <h3>${signal.ticker} · <span class="dir ${signal.direction}">${signal.direction}</span>
        <span class="mono"> ${Number(signal.confidence).toFixed(1)}</span></h3>
      <p class="muted">${fmtTime(signal.created_at)}</p>
      <p style="margin-top:1rem">${signal.reasoning || ""}</p>
      <p class="kicker" style="margin-top:1rem">Supporting data</p>
      <pre>${JSON.stringify(signal.supporting_data || {}, null, 2)}</pre>`;
    dlg.hidden = false;
    dlg.classList.add("open");
  }

  function closeDetail() {
    const dlg = $("dialog");
    dlg.classList.remove("open");
    dlg.hidden = true;
  }

  function renderAll() {
    renderFilters();
    renderHeroBadges();
    renderFrostBar();
    renderMissionBrief();
    renderSnapshot();
    renderHoldings();
    renderClosed();
    renderAgents();
    renderResearch();
    renderMission();
    renderWatch();
  }

  function wireNav() {
    const links = document.querySelectorAll("#nav a");
    const sections = [...document.querySelectorAll("section[id]")];
    const onScroll = () => {
      let current = sections[0]?.id;
      for (const s of sections) {
        if (s.getBoundingClientRect().top <= 140) current = s.id;
      }
      links.forEach((a) =>
        a.classList.toggle("active", a.getAttribute("href") === `#${current}`)
      );
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  function wireSearch() {
    const input = $("searchInput");
    let t = null;
    input.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        query = input.value.trim();
        renderAll();
      }, 120);
    });
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
    wireSearch();
    try {
      if (HAS_INLINE) {
        DATA = INLINE_DATA;
      } else {
        const res = await fetch(DATA_URL, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status} loading ${DATA_URL}`);
        DATA = await res.json();
      }
      setHeroArt();
      renderAll();
    } catch (err) {
      $("loadError").hidden = false;
      $("loadError").textContent =
        `Failed to load ./data/latest.json — run: python -m quant_system export-dashboard. (${err.message})`;
      $("paperStatus").textContent = "No data";
    }
  }

  boot();
})();
