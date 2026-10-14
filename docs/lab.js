/* Mission Ledger — lab journal: what the strategy-lab bots did each day (docs/data/lab/) */
(() => {
  const INDEX_URL = "./data/lab/index.json";
  const dayUrl = (day) => `./data/lab/${day}.json`;
  const INVENTORS = ["momo", "snapback"];
  const BOT_ROLE = {
    momo: "trend, momentum and breakout ideas",
    snapback: "mean-reversion, pullback and volatility ideas",
  };
  const STAGE = {
    nominated: { label: "Nominated", cls: "accent" },
    passed_robust: { label: "Passed robust", cls: "gain" },
    failed_robust: { label: "Failed robust", cls: "warn" },
    beat_benchmarks: { label: "Beat benchmarks", cls: "gain" },
    missed: { label: "Missed benchmarks", cls: "loss" },
  };
  const CHECK_LABEL = {
    nudges: "Settings nudged ±10% and ±20%",
    sectors: "Inside each sector",
    cap_buckets: "Inside each cap bucket",
    selloffs: "Q4 2018 and Feb–Apr 2020 selloffs",
    single_stock: "Without the top stock",
  };
  const UNIVERSE = {
    broad: "the broad test set (about 300 S&P 1500 stocks across every sector)",
    themes: "the quantum, space and robotics theme stocks",
    all: "the broad test set plus the theme stocks",
  };
  const BENCH_LABEL = { SPY: "SPY", equal_weight: "Equal-weight" };
  const RECENT_DAYS = 14;

  let INDEX = null;
  let botFilter = "all";
  let renderToken = 0;
  const cache = new Map();

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  const isDay = (s) => /^\d{4}-\d{2}-\d{2}$/.test(String(s || ""));

  function todayET() {
    return new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  }

  function dayDate(ymd) {
    const [Y, M, D] = String(ymd).split("-").map(Number);
    return new Date(Date.UTC(Y, M - 1, D, 12));
  }

  /** "2026-10-13" -> "Oct 13" (the string is already an ET day; no timezone shift). */
  function fmtDay(ymd, opts = {}) {
    if (!isDay(ymd)) return String(ymd || "—");
    return dayDate(ymd).toLocaleDateString("en-US", { timeZone: "UTC", month: "short", day: "numeric", ...opts });
  }

  function shiftDay(ymd, n) {
    const d = dayDate(ymd);
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }

  function isWeekend(ymd) {
    const wd = dayDate(ymd).getUTCDay();
    return wd === 0 || wd === 6;
  }

  function fmtClock(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }) + " ET";
  }

  function fmtStamp(iso) {
    const d = new Date(iso);
    if (!iso || Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " ET";
  }

  function num(v) {
    return typeof v === "number" && Number.isFinite(v) ? v : null;
  }

  function fmtPct(v, digits = 1) {
    const x = num(v);
    return x == null ? "—" : `${(x * 100).toFixed(digits)}%`;
  }

  function fmtSharpe(v) {
    const x = num(v);
    return x == null ? "—" : x.toFixed(2);
  }

  function fmtSetting(v) {
    const x = num(v);
    if (x == null) return esc(v);
    return Number.isInteger(x) ? String(x) : String(+x.toPrecision(4));
  }

  function plural(n, one, many = `${one}s`) {
    return `${n} ${n === 1 ? one : many}`;
  }

  function joinAnd(list) {
    if (list.length <= 1) return list.join("");
    return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
  }

  // ---------------------------------------------------------------- data

  async function loadIndex() {
    const res = await fetch(INDEX_URL, { cache: "no-store" });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`HTTP ${res.status} loading ${INDEX_URL}`);
    return res.json();
  }

  function activeDays() {
    return (INDEX?.days || []).map((d) => d.date).filter(isDay).sort();
  }

  async function loadDay(day) {
    if (cache.has(day)) return cache.get(day);
    let data = null;
    if (activeDays().includes(day)) {
      const res = await fetch(dayUrl(day), { cache: "no-store" });
      if (res.ok) data = await res.json();
    }
    cache.set(day, data);
    return data;
  }

  // ---------------------------------------------------------------- picker

  function selectedDay() {
    const h = decodeURIComponent(location.hash.slice(1));
    if (isDay(h)) return h;
    const days = activeDays();
    const today = todayET();
    if (days.includes(today) || !days.length) return today;
    return days[days.length - 1];
  }

  function go(day) {
    if (!isDay(day)) return;
    if (location.hash.slice(1) === day) render();
    else location.hash = day;
  }

  function renderPicker(day) {
    const days = activeDays();
    $("dayInput").value = day;
    $("dayInput").max = todayET();
    const before = days.filter((d) => d < day);
    const after = days.filter((d) => d > day);
    $("prevDay").disabled = !before.length;
    $("nextDay").disabled = !after.length;
    $("prevDay").dataset.day = before[before.length - 1] || "";
    $("nextDay").dataset.day = after[0] || "";
    $("todayBtn").disabled = day === todayET();

    const byDate = new Map((INDEX?.days || []).map((d) => [d.date, d]));
    const recent = days.slice(-RECENT_DAYS).reverse();
    $("dayList").innerHTML = recent.length
      ? recent
          .map((d) => {
            const row = byDate.get(d) || {};
            const bits = [];
            if (row.ideas) bits.push(plural(row.ideas, "idea"));
            if (row.indicators_merged) bits.push(plural(row.indicators_merged, "indicator"));
            if (!bits.length && row.skeptic_verdicts) bits.push(plural(row.skeptic_verdicts, "verdict"));
            return `<button type="button" class="day-chip" data-day="${esc(d)}" aria-pressed="${d === day}">
              <span class="d">${esc(fmtDay(d, { weekday: "short" }))}</span>
              <span class="n">${esc(bits.join(" · ") || "activity")}</span>
            </button>`;
          })
          .join("")
      : `<p class="hint">None yet.</p>`;
    $("dayList").querySelectorAll(".day-chip").forEach((b) => b.addEventListener("click", () => go(b.dataset.day)));
  }

  // ---------------------------------------------------------------- day

  async function render() {
    const token = ++renderToken;
    const day = selectedDay();
    renderPicker(day);
    $("dayTitle").textContent = dayDate(day).toLocaleDateString("en-US", {
      timeZone: "UTC", weekday: "long", month: "long", day: "numeric", year: "numeric",
    });
    let data = null;
    try {
      data = await loadDay(day);
    } catch (err) {
      showError(`Couldn't load ${dayUrl(day)} (${err.message}).`);
    }
    if (token !== renderToken) return;
    if (!data) renderEmpty(day);
    else renderDay(data);
  }

  function renderEmpty(day) {
    const days = activeDays();
    const latest = days[days.length - 1];
    let why;
    if (!INDEX) why = "The lab journal hasn't been exported yet. Orbit Control writes it with <code>python -m quant_system export-lab-journal</code>.";
    else if (day > todayET()) why = "That day hasn't happened yet.";
    else if (INDEX.first_day && day < INDEX.first_day) why = `The journal starts on ${esc(fmtDay(INDEX.first_day, { year: "numeric" }))}.`;
    else if (isWeekend(day)) why = "No lab activity that day. The bots run on weekdays.";
    else if (day === todayET()) why = "No lab activity yet today. momo and snapback run at about 10:30 AM and 2:30 PM ET.";
    else why = "No lab activity that day: no ideas tried, no indicators merged and no skeptic verdicts.";
    $("daySummary").innerHTML = "";
    $("labStats").hidden = true;
    const jump = latest && latest !== day
      ? `<button type="button" class="btn" id="jumpLatest">Go to the latest activity (${esc(fmtDay(latest, { weekday: "short" }))})</button>`
      : "";
    $("dayBody").innerHTML = `<section class="card"><div class="empty lab-empty"><p>${why}</p>${jump}</div></section>`;
    $("jumpLatest")?.addEventListener("click", () => go(latest));
  }

  function renderDay(d) {
    const s = d.summary || {};
    $("labStats").hidden = false;
    $("daySummary").innerHTML = daySentence(d);
    const tile = (k, v, sub) => `<div class="tile"><span class="k">${k}</span><span class="v">${v}</span><span class="s">${sub || "&nbsp;"}</span></div>`;
    $("labStats").innerHTML = [
      tile("Ideas tried", s.ideas ?? 0, `${s.new_ideas ?? 0} new`),
      tile("Beat benchmarks", s.beat_benchmarks ?? 0, s.ideas ? `of ${s.ideas}` : ""),
      tile("Lab robust", s.robust_runs ?? 0, `${s.passed_robust ?? 0} passed`),
      tile("Nominated", s.nominations ?? 0, "packet PRs opened"),
      tile("Indicators merged", s.indicators_merged ?? 0, "by smith"),
      tile("Skeptic verdicts", s.skeptic_verdicts ?? 0, s.discovery_trials_to_date != null ? `${s.discovery_trials_to_date} discovery trials to date` : ""),
    ].join("");
    $("dayBody").innerHTML = [ideasSection(d), indicatorsSection(d), nominationsSection(d), skepticSection(d)].join("");
    $("dayBody").querySelectorAll("[data-bot-filter]").forEach((b) =>
      b.addEventListener("click", () => {
        botFilter = b.dataset.botFilter;
        renderDay(d);
      })
    );
  }

  function daySentence(d) {
    const parts = [];
    const bots = d.bots || {};
    const tried = INVENTORS.filter((b) => bots[b]?.ideas).map((b) => `<strong>${b}</strong> tried ${plural(bots[b].ideas, "idea")}`);
    if (tried.length) parts.push(`${joinAnd(tried)}.`);
    const s = d.summary || {};
    if (s.ideas) {
      const beat = s.beat_benchmarks ? `${s.beat_benchmarks} beat both benchmarks` : "None beat both benchmarks";
      const robust = s.robust_runs ? `; ${plural(s.robust_runs, "went", "went")} through lab robust and ${s.passed_robust ? `${s.passed_robust} passed` : "none passed"}` : "";
      parts.push(`${beat}${robust}.`);
    }
    const ind = (d.indicators || []).flatMap((x) => (x.indicators || []).map((i) => `<code>${esc(i.name)}</code>`));
    if (ind.length) parts.push(`<strong>smith</strong> merged ${joinAnd(ind)}.`);
    if (s.skeptic_verdicts) parts.push(`<strong>skeptic</strong> posted ${plural(s.skeptic_verdicts, "verdict")}.`);
    return parts.join(" ") || "Nothing logged.";
  }

  // ---------------------------------------------------------------- ideas

  function ideasSection(d) {
    const ideas = d.ideas || [];
    const counts = Object.fromEntries(INVENTORS.map((b) => [b, ideas.filter((i) => i.bot === b).length]));
    const chips = ["all", ...INVENTORS]
      .map((b) => {
        const n = b === "all" ? ideas.length : counts[b];
        return `<button type="button" class="filter-chip" data-bot-filter="${b}" aria-pressed="${botFilter === b}">${b === "all" ? "All" : b} · ${n}</button>`;
      })
      .join("");
    const shown = INVENTORS.filter((b) => botFilter === "all" || botFilter === b);
    const groups = shown
      .map((bot) => {
        const mine = ideas.filter((i) => i.bot === bot);
        const body = mine.length ? mine.map((i) => ideaCard(i, d.date)).join("") : `<p class="empty">No ideas from ${esc(bot)} this day.</p>`;
        return `<div class="bot-group">
          <h3 class="bot-head"><span class="pill accent">${esc(bot)}</span><span class="subtle">${esc(BOT_ROLE[bot] || "")} · ${plural(mine.length, "idea")}</span></h3>
          ${sessionNotes(bot, (d.session_notes || {})[bot])}
          ${body}
        </div>`;
      })
      .join("");
    return `<section class="card" id="ideas">
      <div class="card-head">
        <div>
          <h2>Ideas tried</h2>
          <p class="hint">Every strategy spec momo or snapback ran this day, in the order they ran.</p>
        </div>
        <div class="chip-row" role="group" aria-label="Filter ideas by bot">${chips}</div>
      </div>
      <details class="how-tested">
        <summary>How each idea is tested</summary>
        <ol>
          <li><strong>Lab run.</strong> The spec trades the discovery window (2016 through 2021-12-31) after trading costs. It has to beat both SPY and an equal-weight basket of the same stocks on Sharpe and on CAGR.</li>
          <li><strong>Lab robust</strong>, only for ideas that pass the lab run. Each setting is nudged ±10% and ±20%. The idea is rerun inside each sector and each cap bucket, checked through the Q4 2018 and Feb–Apr 2020 selloffs, and rerun without its top stock. It survives only if every check passes.</li>
          <li><strong>Nomination.</strong> A survivor goes to a PR for skeptic and Orbit Control. Validation and holdout come after that, on Sabastian's laptop, and never show up here.</li>
        </ol>
      </details>
      ${groups}
    </section>`;
  }

  /** Notes from the bot's session that aren't about one idea ("Lessons:", "Next session: ..."). */
  function sessionNotes(bot, groups) {
    if (!groups || !groups.length) return "";
    const blocks = groups
      .map((g) => `<div class="session-block">
        ${g.session ? `<p class="kicker">${esc(g.session)}</p>` : ""}
        <ul>${(g.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
      </div>`)
      .join("");
    return `<div class="session-notes" aria-label="Session notes from ${esc(bot)}"><p class="session-title">Session notes from ${esc(bot)}</p>${blocks}</div>`;
  }

  function stagePill(stage) {
    const st = STAGE[stage] || { label: stage || "—", cls: "" };
    return `<span class="pill ${st.cls} stage">${esc(st.label)}</span>`;
  }

  function missedSentence(run) {
    if (run.beats_benchmarks) return "Beat SPY and equal-weight on both Sharpe and CAGR.";
    const by = {};
    for (const m of run.missed || []) {
      const i = m.lastIndexOf(" ");
      const metric = m.slice(i + 1);
      (by[metric] = by[metric] || []).push(m.slice(0, i));
    }
    const bits = Object.entries(by).map(([metric, who]) => `${metric} at or below ${joinAnd(who)}`);
    return bits.length ? `Missed: ${bits.join("; ")}.` : "Missed its benchmarks.";
  }

  function resultTable(run) {
    const b = run.benchmarks || {};
    const row = (label, x, cls = "") => `<tr class="${cls}"><td>${label}</td><td class="num">${fmtSharpe(x?.sharpe)}</td><td class="num">${fmtPct(x?.cagr)}</td><td class="num">${fmtPct(x?.max_drawdown)}</td></tr>`;
    const benches = Object.keys(b).map((k) => row(esc(BENCH_LABEL[k] || k), b[k], "bench")).join("");
    return `<div class="table-scroll"><table class="mini-table">
      <thead><tr><th>2016–2021</th><th class="num">Sharpe</th><th class="num">CAGR</th><th class="num" title="Max drawdown">Max DD</th></tr></thead>
      <tbody>${row("This idea", run, "self")}${benches}</tbody>
    </table></div>`;
  }

  function robustStep(idea, day) {
    const r = idea.robust;
    const run = idea.run || {};
    if (!r) {
      const why = run.beats_benchmarks ? "Not run yet." : "Not run. Only ideas that beat both benchmarks go on to lab robust.";
      return `<li class="step skip"><span class="step-k">Lab robust</span><span class="muted">${why}</span></li>`;
    }
    const status = r.passed && r.complete ? ["pass", "Passed"] : r.passed ? ["warn", "Incomplete"] : ["fail", "Failed"];
    const when = r.day && r.day !== day ? ` <span class="subtle">(ran ${esc(fmtDay(r.day))})</span>` : "";
    const failed = (r.failed || []).map((n) => CHECK_LABEL[n] || n);
    const skipped = (r.skipped || []).map((n) => CHECK_LABEL[n] || n);
    let head = r.passed && r.complete
      ? "Every check passed."
      : failed.length
        ? `Failed: ${esc(joinAnd(failed.map((x) => x.toLowerCase())))}.`
        : !r.base_passed
          ? "The base run didn't beat both benchmarks."
          : "";
    if (skipped.length) head += ` Skipped: ${esc(joinAnd(skipped.map((x) => x.toLowerCase())))} (so not complete).`;
    const variants = idea.robust_variants_today ? ` <span class="subtle">${plural(idea.robust_variants_today, "variant run")} today.</span>` : "";
    const checks = (r.checks || [])
      .map((c) => {
        const cls = c.status === "pass" ? "gain" : c.status === "fail" ? "loss" : "";
        return `<li><span class="pill ${cls}">${esc(c.status)}</span><span><strong>${esc(CHECK_LABEL[c.name] || c.name)}.</strong> ${esc(c.detail)}</span></li>`;
      })
      .join("");
    return `<li class="step ${status[0]}"><span class="step-k">Lab robust${when}</span>
      <span><span class="pill ${status[0] === "pass" ? "gain" : status[0] === "fail" ? "loss" : "warn"}">${status[1]}</span> ${head}${variants}</span>
      <ul class="checks">${checks}</ul></li>`;
  }

  function nominationStep(idea) {
    const n = idea.nomination;
    if (!n) {
      if (idea.stage === "passed_robust") return `<li class="step skip"><span class="step-k">Nomination</span><span class="muted">No packet PR yet.</span></li>`;
      return "";
    }
    const sk = n.skeptic ? ` · Skeptic: ${esc(n.skeptic.verdict)}` : " · waiting on skeptic";
    return `<li class="step pass"><span class="step-k">Nomination</span><span><a href="${esc(n.url)}" target="_blank" rel="noopener">PR #${esc(n.pr)}</a> (${esc(n.state)})${sk}</span></li>`;
  }

  function ideaCard(i, day) {
    const w = i.what_it_does || {};
    const run = i.run || {};
    const tags = [];
    tags.push(`<span class="chip category${i.category ? "" : " unset"}" title="Idea family. Categories get filled in after about a week of history.">Category: ${esc(i.category || "—")}</span>`);
    if (i.new_today) tags.push(`<span class="chip">New</span>`);
    else if (i.first_seen) tags.push(`<span class="chip">First tried ${esc(fmtDay(i.first_seen))}</span>`);
    if (run.at) tags.push(`<span class="chip">${esc(fmtClock(run.at))}</span>`);
    const rules = [
      ["Buys when", w.buy],
      ["Sells when", w.sell],
      ["Order", w.order],
      ["Size", w.size],
      ["Stocks", UNIVERSE[w.universe] || (w.universe ? `the ${w.universe} theme stocks` : null)],
    ]
      .filter(([, v]) => v)
      .map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`)
      .join("");
    const runs = i.runs_today > 1 ? ` <span class="subtle">Run ${i.runs_today} times today; the latest is shown.</span>` : "";
    const dirty = run.dirty_checkout ? ` <span class="pill warn">local edits</span> <span class="subtle">Run from a checkout with local edits, so it can't be nominated as is.</span>` : "";
    const names = run.universe_names ? `${run.universe_names} stocks` : "the same stocks";
    const runStep = `<li class="step ${run.beats_benchmarks ? "pass" : "fail"}"><span class="step-k">Lab run</span>
      <span>Discovery window, ${esc(fmtDay(run.start, { year: "numeric" }))} to ${esc(fmtDay(run.end, { year: "numeric" }))}, after trading costs, against SPY and an equal-weight basket of ${esc(names)}.
      <span class="pill ${run.beats_benchmarks ? "gain" : "loss"}">${run.beats_benchmarks ? "Beat" : "Missed"}</span> ${esc(missedSentence(run))}
      <span class="subtle">${run.trade_count != null ? `${esc(run.trade_count)} trades` : ""}${num(run.turnover) != null ? ` · turnover ${run.turnover.toFixed(1)}× a year` : ""}.</span>${runs}${dirty}</span></li>`;
    const notes = (i.notes || []).length
      ? `<div class="idea-notes"><p class="kicker">Notes from ${esc(i.bot)}</p><ul>${i.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>`
      : "";
    const inds = (w.indicators || [])
      .map((x) => `<li><code>${esc(x.label)}</code>${x.alias && x.alias !== x.fn ? ` <span class="subtle">(called ${esc(x.alias)})</span>` : ""}${x.about ? ` ${esc(x.about)}` : ""}</li>`)
      .join("");
    const settings = (w.settings || [])
      .map((p) => `<li><code>${esc(p.name)}</code> = ${fmtSetting(p.value)} <span class="subtle">(nudged within ${fmtSetting(p.min)}–${fmtSetting(p.max)})</span></li>`)
      .join("");
    const more = inds || settings
      ? `<details class="idea-more"><summary>Indicators and settings</summary>
          ${inds ? `<ul>${inds}</ul>` : ""}${settings ? `<p class="kicker">Tunable settings</p><ul>${settings}</ul>` : ""}
          <p class="subtle mono">spec ${esc(i.spec_hash)}</p>
        </details>`
      : "";
    return `<article class="idea">
      <header class="idea-head">
        <div class="idea-title">
          <h4 class="idea-name">${esc(i.spec_id || i.spec_hash)}</h4>
          <div class="badge-row">${tags.join("")}</div>
        </div>
        ${stagePill(i.stage)}
      </header>
      ${i.description ? `<p class="idea-desc">${esc(i.description)}</p>` : ""}
      <dl class="idea-rules">${rules}</dl>
      <div class="idea-tests">
        <p class="kicker">How it was tested</p>
        <ol class="steps">${runStep}${robustStep(i, day)}${nominationStep(i)}</ol>
      </div>
      ${resultTable(run)}
      ${notes}
      ${more}
    </article>`;
  }

  // ---------------------------------------------------------------- GitHub sections

  function verdictPill(v) {
    if (!v) return `<span class="pill">no verdict yet</span>`;
    const cls = v.verdict === "no objection" ? "gain" : v.verdict === "objection" ? "loss" : "";
    return `<span class="pill ${cls}">${esc(v.verdict)}</span>`;
  }

  function githubNote(d) {
    return d.github === false ? `<p class="footnote">GitHub couldn't be read on the last export; this list may be out of date.</p>` : "";
  }

  function indicatorsSection(d) {
    const rows = d.indicators || [];
    const body = rows.length
      ? rows
          .map((pr) => {
            const items = (pr.indicators || []).length ? pr.indicators : [{ name: pr.title }];
            const names = items
              .map((x) => {
                const settings = (x.settings || [])
                  .map((p) => `<li><code>${esc(p.name)}</code> ${fmtSetting(p.default)} <span class="subtle">(${fmtSetting(p.min)}–${fmtSetting(p.max)})</span></li>`)
                  .join("");
                return `<div class="ind">
                  <p class="ind-name"><code>${esc(x.name)}</code>${x.about ? ` <span>${esc(x.about)}</span>` : ""}</p>
                  ${settings ? `<details class="idea-more"><summary>${plural(x.settings.length, "setting")}: default (range)</summary><ul class="settings-list">${settings}</ul></details>` : ""}
                </div>`;
              })
              .join("");
            return `<li class="gh-row">
              <div class="gh-meta"><a href="${esc(pr.url)}" target="_blank" rel="noopener">#${esc(pr.pr)}</a> <span class="subtle">merged ${esc(fmtClock(pr.merged_at))}${pr.bot ? ` · by ${esc(pr.bot)}` : ""}</span></div>
              <div class="gh-body">
                ${names}
                ${pr.summary ? `<p class="muted gh-summary">${esc(pr.summary)}</p>` : ""}
                <p class="subtle">Skeptic: ${verdictPill(pr.skeptic)}</p>
              </div>
            </li>`;
          })
          .join("")
      : "";
    return `<section class="card" id="indicators">
      <div class="card-head"><div><h2>Indicators merged</h2><p class="hint">New indicators smith built that merged this day. Inventors can use them in specs from now on.</p></div></div>
      ${rows.length ? `<ul class="gh-list">${body}</ul>` : `<p class="empty">No indicators merged this day.</p>`}
      ${githubNote(d)}
    </section>`;
  }

  function nominationsSection(d) {
    const rows = d.nominations || [];
    if (!rows.length) return "";
    const body = rows
      .map((n) => `<li class="gh-row">
        <div class="gh-meta"><a href="${esc(n.url)}" target="_blank" rel="noopener">#${esc(n.pr)}</a> <span class="subtle">${esc(fmtClock(n.opened_at))}${n.bot ? ` · ${esc(n.bot)}` : ""}</span></div>
        <div class="gh-body"><p>${esc(n.title)} <span class="pill">${esc(n.state)}</span></p><p class="subtle">Skeptic: ${verdictPill(n.skeptic)}</p></div>
      </li>`)
      .join("");
    return `<section class="card" id="nominations">
      <div class="card-head"><div><h2>Nominations opened</h2><p class="hint">Packets for ideas that survived lab robust. They wait for skeptic, then Orbit Control.</p></div></div>
      <ul class="gh-list">${body}</ul>
    </section>`;
  }

  function skepticSection(d) {
    const rows = d.skeptic || [];
    const kind = { indicator: "indicator PR", nomination: "nomination PR", other: "PR" };
    const body = rows
      .map((v) => `<li class="gh-row">
        <div class="gh-meta">${esc(fmtClock(v.at))}</div>
        <div class="gh-body">
          <p>${verdictPill(v)} on <a href="${esc(v.url)}" target="_blank" rel="noopener">#${esc(v.pr)}</a> <span class="subtle">${esc(kind[v.kind] || "PR")}</span> ${esc(v.title)}</p>
          ${v.evidence ? `<p class="muted gh-summary">${esc(v.evidence)}</p>` : ""}
        </div>
      </li>`)
      .join("");
    return `<section class="card" id="skeptic">
      <div class="card-head"><div><h2>Skeptic's verdicts</h2><p class="hint">skeptic attacks every nomination and new-indicator PR before Orbit Control merges it.</p></div></div>
      ${rows.length ? `<ul class="gh-list">${body}</ul>` : `<p class="empty">No verdicts posted this day.</p>`}
      ${githubNote(d)}
    </section>`;
  }

  // ---------------------------------------------------------------- boot

  function showError(msg) {
    $("loadError").hidden = false;
    $("loadError").textContent = msg;
  }

  function renderFreshness() {
    const el = $("exportedAt");
    if (!INDEX) {
      el.textContent = "Not exported yet";
      return;
    }
    el.innerHTML = `<span class="dot" aria-hidden="true"></span>Updated ${esc(fmtStamp(INDEX.generated_at))}`;
  }

  async function boot() {
    $("prevDay").addEventListener("click", (e) => go(e.currentTarget.dataset.day));
    $("nextDay").addEventListener("click", (e) => go(e.currentTarget.dataset.day));
    $("todayBtn").addEventListener("click", () => go(todayET()));
    $("dayInput").addEventListener("change", (e) => go(e.target.value));
    window.addEventListener("hashchange", render);
    try {
      INDEX = await loadIndex();
    } catch (err) {
      showError(`Couldn't load ${INDEX_URL} (${err.message}).`);
    }
    renderFreshness();
    render();
  }

  boot();
})();
