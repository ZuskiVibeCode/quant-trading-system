/* Quant Mission Ledger — vanilla viewer for docs/data/latest.json */
(() => {
  const DATA_URL = "./data/latest.json";
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
    $("exportedAt").textContent = `Exported ${fmtTime(meta.exported_at)} · account ${meta.account || "paper"}`;
    const cards = [
      { label: "Equity", value: fmtUsd(s.equity) },
      { label: "Cash", value: fmtUsd(s.cash) },
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
              (h) => `<tr>
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

  function renderWatch() {
    const rows = (DATA.watchlist || []).filter((w) => matchesSector(w.sector || sectorOf(w.ticker)));
    if (!rows.length) {
      $("watchTable").innerHTML = `<div class="empty">Watchlist empty for filter</div>`;
      return;
    }
    $("watchTable").innerHTML = `
      <table>
        <thead><tr><th>Ticker</th><th>Sector</th><th>Top agent</th><th>Dir</th><th>Conf</th><th>Note</th></tr></thead>
        <tbody>
          ${rows
            .map((w) => {
              const s = w.signal;
              return `<tr>
                <td class="mono">${w.ticker}</td>
                <td class="muted">${w.sector || "—"}</td>
                <td class="mono muted">${s?.agent_id || "—"}</td>
                <td>${s ? `<span class="dir ${s.direction}">${s.direction}</span>` : "—"}</td>
                <td class="mono">${s ? Number(s.confidence).toFixed(0) : "—"}</td>
                <td class="muted">${trunc(s?.reasoning || "", 90)}</td>
              </tr>`;
            })
            .join("")}
        </tbody>
      </table>`;
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
    renderSnapshot();
    renderHoldings();
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
