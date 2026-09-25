# Dashboard JSON schema (`docs/data/latest.json`)

Frozen contract for the GitHub Pages viewer. Produced by:

```bash
python -m quant_system export-dashboard
```

Optional: `--no-alpaca`, `--no-history`, `--docs PATH`.

## Top-level keys

| Key | Type | Notes |
|-----|------|-------|
| `meta` | object | `exported_at`, `account`, `starting_equity`, `paper` (always `true`), `day_start_utc` (ET local midnight as UTC ISO), `day_timezone` (`America/New_York`), `day_local` (YYYY-MM-DD), `day_label` (`ET day`) |
| `snapshot` | object | `equity`, `cash`, `peak_equity`, `daily_pnl`, `unrealized` (sum of available holding dollar marks, or `null`), `drawdown`, `halted`, `day_halted`, `halt_reason` |
| `holdings` | array | `{ticker, qty, avg_cost, last?, sector, unrealized?, updated_at?}` |
| `closed` | array | From sell fills when trackable; may be `[]` |
| `equity_curve` | array | `{t, equity, cash, daily_pnl, drawdown, peak_equity}` from `portfolio_snapshots` |
| `agents_today` | object | Per `agent_id`: `{agent_id, count, long, short, neutral, signals[]}` |
| `decisions_today` | object | `tickets`, `holds`, `fills`, `order_submitted`, `failed`, `counts` |
| `auditor_flags_today` | array | Flags with `created_at` on the current ET calendar day |
| `mission_log` | array | Timeline `{id, at, type, actor, ticker, summary}` |
| `watchlist` | array | Universe tickers not held + top agent signal |
| `sectors` | object | Theme → ticker list from `config/universe.yaml` |
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
`agents_today`, `sectors`, and `holdings`. Never contains secrets.

```
sector_board: [{
  theme, tickers_n, held_n, signaled_n,
  weight,                 # theme holdings market value / equity
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
`weight` = Σ|qty·last| (else qty·avg_cost) / equity; `unrealized` = Σ dollar marks.

## History

When history is enabled (default), the same payload is also written to:

`docs/data/history/YYYY-MM-DD.json` (America/New_York local calendar date).

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
