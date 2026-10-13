# Dashboard JSON schema (`docs/data/latest.json`)

Frozen contract for the GitHub Pages viewer. Produced by:

```bash
python -m quant_system export-dashboard
```

Optional: `--no-alpaca`, `--no-history`, `--docs PATH`.

## Top-level keys

| Key | Type | Notes |
|-----|------|-------|
| `meta` | object | `exported_at`, `account`, `starting_equity`, `paper` (always `true`), `day_start_utc` (ET local midnight as UTC ISO), `day_timezone` (`America/New_York`), `day_local` (YYYY-MM-DD), `day_label` (`ET day`), `research_day_local`, `research_fallback` — see Research day below |
| `snapshot` | object | `equity`, `cash`, `peak_equity`, `daily_pnl` (vs the prior ET-day close), `unrealized` (sum of available holding dollar marks, or `null`), `drawdown`, `halted`, `day_halted`, `halt_reason` |
| `holdings` | array | `{ticker, qty, avg_cost, last?, sector, unrealized?, updated_at?, why_bought?}` — `why_bought` is additive, see below. Dust positions (`qty × avg_cost` under $0.01) are left out |
| `closed` | array | From sell fills when trackable; may be `[]` |
| `equity_curve` | array | `{t, equity, cash, daily_pnl, drawdown, peak_equity}` from `portfolio_snapshots` |
| `agents_today` | object | Per `agent_id`: `{agent_id, count, long, short, neutral, signals[]}` |
| `decisions_today` | object | `tickets`, `holds`, `fills`, `order_submitted`, `failed`, `counts` |
| `auditor_flags_today` | array | Flags with `created_at` on the current ET calendar day |
| `mission_log` | array | Timeline `{id, at, type, actor, ticker, summary}` |
| `watchlist` | array | Live active tickers (after rotation) not held + top agent signal |
| `sectors` | object | Theme → live active ticker list (rotation state; seeded from `config/universe.yaml`) |
| `alpaca_snapshot` | object? | Optional public cash/equity/positions only — **never secrets** |
| `rotation` | object? | **Additive.** `{active: theme→tickers, cold: [...], settings}` from cold/active researcher |
| `zones` | object? | **Additive.** Today's buy/sell pressure map — see below |
| `zones_series` | object? | **Additive.** Compact recent history `{days:[{day, points}]}` (≤20 days) for charts |
| `sector_board` | array? | **Additive.** Per-theme pressure / breadth / book — see below |
| `cold` | array? | **Additive.** Compact `[{ticker, checkup_date, reason, theme}]` (same as `rotation.cold`) |
| `ticker_pnl` | array? | **Additive.** Lifetime per-ticker realized/unrealized + series |
| `trade_reviews` | array? | **Additive.** Closed-lot postmortems — see below |

### `agents_today[].signals[]`

`{ticker, direction, confidence, reasoning, supporting_data, created_at, signal_id}`

`supporting_data` is a **summary** (selected scalar keys), not the full research blob.

### `holdings[].why_bought` (additive)

The buy tickets behind the current open lot, or `null` when no buy fills are on
record for the position.

```
{opened_at, buy_count, omitted,
 buys: [{ticket_id, created_at, rationale, score?: {weighted_score?, blended_conf?},
         signals: [{agent_id, direction, confidence, reasoning_trunc, signal_id, created_at}],
         fills: [{fill_id, at, qty, price, mode}]}]}
```

- The lot opens on the first buy from flat and resets when a sell flattens it.
- `buys` are chronological and grouped by ticket. Over 10 tickets, the opening
  buy and the 9 most recent are kept, and `omitted` counts the rest.
- `signals` (one per desk) come only from the ticket's own `signal_ids`.
  Backfilled buys (`mode` `alpaca_backfill`) have none.
- `rationale` is the portfolio manager's ticket text. `score` is parsed from it
  when present.


### `zones` (additive)

Computed from `agents_today` on each export. Never contains secrets.

```
zones: {
  as_of: ISO-UTC,
  day_local: YYYY-MM-DD,
  thresholds: {buy_zone_min, sell_zone_min, buy_quiet_max, sell_quiet_max},
  points: [{
    ticker, sector?, held, weight?,
    buy, sell, net,          # floats in [0,1] (net = buy - sell)
    zone,                    # buy_zone | sell_zone | conflicted | quiet
    agents: [{agent_id, direction, confidence}]
  }]
}
```

**Pressure model** (per ticker, per ET day):

1. Take the **latest** signal for each research agent that fired on the ticker.
2. `buy = mean over agents of (confidence/100 if direction==long else 0)`
3. `sell = mean over agents of (confidence/100 if direction==short else 0)`
4. Neutral / missing agent contributions are `0`; both axes are independent and in `[0, 1]`.
5. Zone labels (defaults):
   - `buy_zone` if `buy >= 0.35` and `sell < 0.25`
   - `sell_zone` if `sell >= 0.35` and `buy < 0.25`
   - `conflicted` if `buy >= 0.35` and `sell >= 0.35`
   - `quiet` otherwise

### `zones_history.json` (sibling file)

Written next to `latest.json` on every export. Full backfill from `history/*.json`:

```
{ "days": [ { "day": "YYYY-MM-DD", "points": [ {ticker, buy, sell, zone, held} ] } ] }
```

Days sorted ascending. `zones_series` in `latest.json` is the trailing window of this file.


### `sector_board` (additive)

One row per universe theme (quantum / space / robotics). Built at export from
`agents_today`, `sectors`, `holdings`, and `closed` (realized). Never contains secrets.

```
sector_board: [{
  theme, tickers_n, held_n, signaled_n,
  weight,                 # theme holdings market value / equity
  realized,               # sum of closed-lot pnl for theme tickers (0 if none)
  unrealized,             # sum of dollar unrealized marks (or null)
  buy, sell, net,         # mean per-ticker zone pressures (signaled only)
  long, short, neutral,   # ticker counts by majority of latest agent directions
  zones: {buy_zone, sell_zone, conflicted, quiet}  # ticker counts by zone_label
  returns: { "1d", "5d", "20d" },   # eq-wt simple returns (float|null)
  returns_n: { "1d", "5d", "20d" }  # tickers that contributed per horizon
}]
```

**Pressure:** same per-ticker math as Zones (`compute_zone_pressures` on latest
signal per agent); theme buy/sell/net = mean across theme tickers that have
signals. Tickers with no signals that day are excluded from the mean.

**Tape (eq-wt):** for each theme ticker with usable daily closes, simple return
`(close_t / close_{t-N}) - 1` for N ∈ {1, 5, 20}; theme return = equal-weight
mean of those ticker returns (skip missing history / failed fetch). Offline
tolerant — nulls when market data is unavailable. `returns_n` is the count of
contributors per horizon.

**Breadth:** for each signaled ticker, majority of latest agent directions →
long / short / neutral (ties → neutral). Zone bucket counts use `zone_label`.

**Book:** `held_n` open holdings in theme vs `tickers_n` universe size;
`weight` = Σ|qty·last| (else qty·avg_cost) / equity; `realized` = Σ numeric
closed-lot `pnl` for theme tickers (0.0 when no closes; null pnls ignored);
`unrealized` = Σ dollar marks.

## History

When history is enabled (default), the same payload is also written to:

`docs/data/history/YYYY-MM-DD.json` (America/New_York local calendar date).

No history file is written for an export with `meta.research_fallback: true`.

## Research day

`agents_today`, `zones`, `sector_board` pressure and `watchlist` signals come from
`meta.research_day_local`. Normally that is `day_local`. When today (ET) has no
signals yet (weekend, holiday, or an export before the first research run), the
export uses the latest earlier ET day that has signals and sets
`research_fallback: true`. `decisions_today`, `mission_log` and auditor flags stay
on `day_local`.

`zones_history.json` is keyed by research day. Days with no zone points are left
out, and a fallback payload never replaces a day's own history file.

## Security

- `meta.paper` must always be `true`
- Never embed Alpaca API keys / secrets in JSON under `docs/`


### `trade_reviews` (additive)

Built at export from fills / tickets / signals (no schema migration). Also
written to `docs/data/trade_reviews.json`. Matching `review` objects are
attached onto `closed[]` rows; `ticker_pnl` gains `review_count` /
`last_verdict` / `last_verdict_tags` / `last_completeness`.

```
trade_reviews: [{
  ticker, qty, entry, exit, pnl, pnl_pct,
  opened_at, closed_at, hold_hours,
  exit_reason,   # signal_sell | cash_buffer_trim | opportunity_rotate | other | unknown
  exit_rationale,
  entry_ticket_id, exit_ticket_id, entry_fill_id, exit_fill_id,
  entry_score: {weighted_score, blended_conf} | null,
  agents_at_entry: [{agent_id, direction, confidence, reasoning_trunc, signal_id}],
  mix: {long, short, neutral, disagreement, lead_agent, lead_confidence},
  path: {mfe_pct, mae_pct, ret_pct},
  verdict,                 # 1–2 fact-tied sentences
  verdict_tags: [...],     # winner|loser|scratch, aligned|split_book, forced_exit|signal_exit, ...
  completeness: full|partial|thin,
  evidence: [{kind, id, label, detail}]  # mandatory citations (fill/ticket/signal/path/score)
}]
```

Never invent motives. Thin/backfill lots keep `completeness=thin` and evidence
lists what is absent.

## Lab journal (`docs/data/lab/`, page `lab.html`)

What the strategy-lab bots did each America/New_York day. Written by
`python -m quant_system export-lab-journal` on the shared machine (Orbit
Control's data routine). It rebuilds the last 7 days by default, or every day
with `--all`, and rewrites a file only when its content changed.

Sources:
- the trial log and robust reports under `QUANT_LAB_DATA_DIR`
- `/workspace/lab-work/<bot>/`: `notes.md`, plus any saved `lab robust` JSON
  (override the folder with `QUANT_LAB_WORK_DIR` or `--lab-work`)
- `gh pr list`: indicator PRs, nomination PRs, and comments starting `Skeptic:`

**Discovery only.** The exporter reads only records whose window is
`discovery`. It drops every free-text line (notes, descriptions, PR text) that
mentions validation or holdout, hides PR titles that do, and refuses to
write a day whose file would still mention either one (it's listed under
`refused` in the command's output). Validation and holdout results stay on
Sabastian's machine.

One file per day with activity, `<YYYY-MM-DD>.json` (compact JSON). A day with
no activity gets no file, and the page shows an empty state for it. If GitHub
can't be read, a day keeps the PR lists it already had and is marked
`github: false`.

```
{ schema: 1, date, window: {name: "discovery", start, end},
  summary: {ideas, new_ideas, beat_benchmarks, robust_runs, passed_robust,
            nominations, indicators_merged, skeptic_verdicts, discovery_trials_to_date},
  bots: {momo|snapback: {ideas, new, beat_benchmarks, robust, passed_robust}},
  ideas: [{
    spec_id, spec_hash, bot,
    category,              # null for now; idea family (breakout, pullback/mean-reversion, pattern, volume, ...)
    description,           # the spec's own description
    what_it_does: {buy, sell, order, size, universe, indicators: [{alias, fn, label, settings, about}],
                   settings: [{name, value, min, max}]},   # rules in plain English
    new_today, first_seen, runs_today, robust_variants_today,
    run: {at, start, end, sharpe, cagr, max_drawdown, trade_count, turnover,
          benchmarks: {SPY|equal_weight: {sharpe, cagr, max_drawdown}},
          beats_benchmarks, missed: ["SPY Sharpe", "equal-weight CAGR", ...],
          universe_names, dirty_checkout?},
    robust: null | {at, day, passed, complete, base_passed, failed: [...], skipped: [...],
                    checks: [{name, status, detail}]},   # nudges, sectors, cap_buckets, selloffs, single_stock
    nomination: null | {pr, url, state, opened_day, skeptic: {verdict, at}},
    notes: [...],          # spec notes + matching notes.md lines (same-day section first)
    stage                  # missed | beat_benchmarks | failed_robust | passed_robust | nominated
  }],
  indicators: [{pr, title, url, merged_at, bot, summary, skeptic,
                indicators: [{name, about, settings: [{name, default, min, max}]}]}],
  skeptic: [{pr, title, url, kind, verdict, evidence, at}],   # verdict: no objection | objection | comment
  nominations: [{pr, title, url, bot, state, opened_at, files, skeptic}],
  last_activity_at, github }
```

`index.json`: `{schema, generated_at, timezone, first_day, last_day,
total_bytes, days: [{date, ...summary, bots, bytes}]}`.

The folder is kept out of `docs/data/history/`. `scripts/build_orbit_slim_deploy.py`
copies the newest days into the Vercel deploy until they reach 2.5 MB
(`LAB_DEPLOY_BUDGET`) and rewrites the deployed index to match (`trimmed` =
days left out). Older days stay in the repo.
