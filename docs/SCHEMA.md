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
| `snapshot` | object | `equity`, `cash`, `peak_equity`, `daily_pnl`, `drawdown`, `halted`, `day_halted`, `halt_reason` |
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
| `cold` | array? | **Additive.** Compact `[{ticker, checkup_date, reason, theme}]` (same as `rotation.cold`) |

### `agents_today[].signals[]`

`{ticker, direction, confidence, reasoning, supporting_data, created_at, signal_id}`

`supporting_data` is a **summary** (selected scalar keys), not the full research blob.

## History

When history is enabled (default), the same payload is also written to:

`docs/data/history/YYYY-MM-DD.json` (America/New_York local calendar date).

## Security

- `meta.paper` must always be `true`
- Never embed Alpaca API keys / secrets in JSON under `docs/`
