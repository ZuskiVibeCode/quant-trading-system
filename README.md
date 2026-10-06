# Multi-Agent Quant Trading System

Paper-trade US stocks in **quantum computing**, **space**, and **robotics** themes.
Designed to run under **Grok Bot** (CLI + SQLite) — not LangGraph/CrewAI/Telegram/VM.

**Goal:** $100 paper account; beat S&P 500 on a risk-adjusted (Sharpe) basis. Crypto out of scope.

## Hard constraints (enforced in code)

- **Paper only:** Alpaca when `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` present and `ALPACA_PAPER=true`; otherwise dry-run simulation
- Max position **20%** equity; max daily loss **10%** (day halt); max drawdown **15%** (halt + audit alert)
- No withdraw / transfer / account-settings mutations
- Kill switch: `python -m quant_system halt` / `resume`
- Every decision logged with reasoning, timestamp, data snapshot (Tracker SQLite)

### Risk / exits (`config/risk.yaml`)

- **Buys** require `min_confidence_to_trade` (default **55**); **sells/reduces** use a lower floor `min_confidence_to_sell` (default **40**) so paper can exit easier.
- **Cash buffer trim:** if cash/equity < `cash_buffer_min_pct` (3%), the cycle produced no BUYs, and at least one held name has score ≤ 0, emit one SELL of `buffer_trim_frac` (20%) of the weakest score≤0 holding (rationale includes `cash buffer trim`). Target buffer `cash_buffer_target_pct` (5%) is documented; trim uses min + frac. At most `max_buffer_trims_per_cycle` (1) per cycle. Holdings with score > 0 are never trimmed this way.

## Architecture

```
Research (signals only)
  MomentumAgent | FundamentalAgent | StatisticalMLAgent
  SentimentAgent | MacroAgent
        │  Frozen Signal model
        ▼
Tracker (SQLite)  ← FIRST oversight component
        │
PortfolioManager (weighs only; never invents ideas; risk limits)
        │  TradeTicket or HOLD
        ▼
ExecutionAgent (Alpaca paper or dry-run)
        │
Auditor (flags only; cannot override PM)
```

### Weighting policy

Equal initially → 30d review only → 90d light Sharpe → 1y fuller Sharpe.  
Floor **5%**, cap **40%**, **OOS only**.

**Momentum absolute-share cap** (`momentum_max_abs_share`, default **0.40**): after
weight floor/cap, each desk contributes `direction × (confidence/100) × weight`.
If Momentum’s `|contrib|` would exceed 40% of `sum(|contrib|)` **and** at least one
other live desk (Fundamental / Macro / Stat / Sentiment) has non-zero absolute
contribution, Momentum’s contribution is scaled down so its share equals 40%.
When every other desk is flat (zero abs contrib), Momentum is left uncapped.
This does **not** change live confidence floors (55 / 40 / `|score|` 0.05).

### Universe (`config/universe.yaml`)

Active set (~10/theme) is researched each cycle. Bench names promote when an active name goes **cold**.

| Theme | Active | Bench |
|-------|--------|-------|
| Quantum | IONQ, RGTI, QUBT, IBM, GOOGL, QBTS, QSI, MSFT, AMD, HON | AMZN, META, ORCL, ACN, CRWD |
| Space | RKLB, ASTS, LUNR, BA, LHX, RDW, BKSY, IRDM, NOC, LMT | SPCE, PL, JOBY, KTOS, HEI |
| Robotics | ISRG, PATH, TER, NVDA, TSLA, CGNX, SYM, ROK, AVAV, ABB | ADSK, EMR, ZBRA, DE, ROP |
| Benchmark | SPY | — |

#### Cold / active researcher

Stocks that look awful for a stretch go **cold** for `cold_days` (default 15 calendar days) with a `checkup_date`, while a same-theme bench name is promoted into the active set. Active target stays ~10/theme. **Never colds a currently held position.** Paper research only.

Tunables under `rotation:` in `config/universe.yaml`:

| Key | Default | Meaning |
|-----|---------|---------|
| `lookback_cycles` | 10 | Need this many recent `run_start` cycles with signals before colding |
| `min_median_confidence` | 35 | Median confidence below this (and weak long fraction) → awful |
| `max_long_fraction` | 0.35 | Fraction of long directions below this → awful |
| `cold_days` | 15 | Calendar days until checkup |
| `max_new_cold_per_theme_per_cycle` | 1 | Cap new colds per theme per cycle |
| `active_target_per_theme` | 10 | Target active size when promoting |

State lives in Tracker `system_state` key `research_rotation`. Digest / dashboard export add optional `rotation` + `cold` fields (additive schema).

## Quick start

```bash
cd quant-trading-system
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

# Offline dry-run (MockMarketData)
python -m quant_system run-once --mock

# Live prices via yfinance (still paper/dry-run unless Alpaca keys set)
python -m quant_system run-once

# Kill switch
python -m quant_system halt --reason "manual"
python -m quant_system resume
python -m quant_system status
python -m quant_system digest

# Paper broker hygiene (Alpaca keys required)
python -m quant_system cancel-open-orders   # free BP reserved by open/accepted orders
python -m quant_system reconcile           # sync tracker cash/equity/positions from Alpaca
python -m quant_system status --broker     # tracker + Alpaca cash/equity/BP/open orders

# GitHub Pages payload
python -m quant_system export-dashboard
```

Optional Alpaca paper keys (never commit secrets). Prefer a gitignored project `.env`
(see `.env.example`); `python -m quant_system …` and `export_dashboard` auto-load it
via `load_project_dotenv()` (does not override vars already in the environment):

```bash
# .env (gitignored) or:
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
export ALPACA_PAPER=true
```

## Grok Bot scheduling

Suggested cadence (America/New_York):

1. **Pre-market / once daily:** `python -m quant_system run-once` (or `--mock` in CI)
2. After each run: `python -m quant_system digest --limit 30` → summarize for operator
3. On anomaly / drawdown alert in digest: `python -m quant_system halt`
4. DB path: `data/tracker.db` (override with `QUANT_DB_PATH` or `--db`)

Example Bot prompt:

> From `/workspace/quant-trading-system` with venv active, run `python -m quant_system run-once`, then `digest`, report equity, positions, new fills, and any audit flags. If halted or drawdown ≥ 15%, do not resume without human confirmation.


## GitHub Pages dashboard

Static Orbit-inspired viewer under `docs/` (site root = `/docs`).

**Enable Pages:** repo **Settings → Pages → Build and deployment → Deploy from a branch** → Branch `main` → folder `/docs` → Save.

**Public URL pattern:** `https://zuskiVibeCode.github.io/quant-trading-system/`

(Exact casing follows the GitHub user/org; this repo is `ZuskiVibeCode/quant-trading-system`.)

### Export data

```bash
source .venv/bin/activate
python -m quant_system export-dashboard
# writes docs/data/latest.json
# and docs/data/history/YYYY-MM-DD.json (ET local date)
# optional: --no-alpaca  --no-history  --docs PATH
```

The exporter reads Tracker SQLite and optionally a public Alpaca paper snapshot (cash/equity/positions only — **never** API secrets). `meta.paper` is always `true`. Schema: [`docs/SCHEMA.md`](docs/SCHEMA.md).

### Digest / automation note

Daily digest routines should run `export-dashboard` after `run-once` / `digest`, then **commit and push** `docs/data/` (and history) so Pages stays current. Parent routines own `automation.json` updates.

## Package layout

```
quant_system/
  models/          # Frozen Signal, TradeTicket, HoldDecision
  tracker/         # SQLite schema + Tracker API (FIRST)
  research/        # 5 research agents + cold/active rotation
  execution/       # PortfolioManager + ExecutionAgent
  oversight/       # Auditor (flags only)
  market_data/     # MarketData interface + yfinance + mock
  config/          # YAML loaders
  pipeline.py      # run-once orchestration
  cli.py           # run-once | halt | resume | status | digest | cancel-open-orders | reconcile | export-dashboard | backtest-themes
  backtest/        # theme-competition as-of replay + leaderboard
docs/              # GitHub Pages viewer (static) + data/latest.json
config/
  universe.yaml
  risk.yaml
data/
  tracker.db       # created at runtime (gitignored)
tests/
```

## Tests

```bash
source .venv/bin/activate
pytest -q
```

## Research agents (status)

| Agent | Status | Notes |
|-------|--------|-------|
| MomentumAgent | live | MA/RSI/MACD/ATR/RS vs benchmark; relative volume, demand imbalance, BB/KC squeeze; VbP (PoC/VA), Weinstein weekly stage (30w SMA), anchored VWAP (high-volume pivot) — soft score only. Minervini / OBV / Chandelier still export in supporting_data but `momentum_flow_overlays` defaults **false** (score delta 0) |
| FundamentalAgent | live | yfinance first, FMP (`FMP_API_KEY`) fallback when Yahoo thin/fails → quality + valuation + EV/capital structure, sales multiples, dilution soft flags; missing → neutral/low conf; soft score only |
| SentimentAgent | live | headline polarity + credibility; `manipulation_risk` only on coordinated hype |
| MacroAgent | live | VIX (^VIX), dollar (UUP / DX-Y.NYB), rates (TLT / ^TNX), SPY trend → risk-on/off/neutral; `supporting_data`: regime, macro_risk_level, sector_bias, key_levels |
| StatisticalMLAgent | live | z-score / residual-to-MA mean-reversion, momentum-stat, vol regime, optional XS mom rank; `statistical_edge_score` + confidence band; missing → neutral/low conf |


## Theme competition backtest (v2)

Speed-test the three theme desks (quantum / space / robotics) on historical daily OHLCV before a live 3-agent paper split. Each sleeve starts with **$100** cash and trades only its theme universe.

```bash
source .venv/bin/activate
# Optional: warm Yahoo bar cache + statement cache (also happens on first run)
python -m quant_system backtest-themes --start 2026-06-23 --end 2026-09-25
# Flags: --cash 100 --slippage-bps 5 --fill-on next_open|close --force-download
#        --cache-dir data/backtest/bars --fundamentals-cache-dir data/backtest/fundamentals
#        --output artifacts/backtest
# Backtest-only density knobs (do NOT change live run-once / risk.yaml):
#        --min-confidence-to-trade 35   # live default 55
#        --min-confidence-to-sell 25    # live default 40
# Minervini/OBV/Chandelier score tilts are off by default (momentum_flow_overlays: false).
# ORBIT_DISABLE_MOMENTUM_FLOW_OVERLAYS=1  # force those three score deltas to 0
#        --min-abs-score 0.02           # live default 0.05
```

Leaderboard JSON lands under `artifacts/backtest/` (`theme_leaderboard_latest.json` + dated file). Bars cache under `data/backtest/bars/*.csv`; statement snapshots under `data/backtest/fundamentals/*.json`.

| Piece | v2 behavior |
|-------|-------------|
| Market data | `AsOfMarketData` over cached Yahoo daily bars; `history()` / `last_price()` truncated to as-of (no lookahead) |
| Fill model | Default **next_open**: signal at day t close, order queued, filled at day t+1 **open** × (1 ± slippage_bps/1e4). Sells fill before buys; buys trimmed to cash; no bar at t+1 or still queued after the last day → cancelled (counted in `cancelled_orders`). `--fill-on close` = same-bar close, optimistic, for comparison only |
| Benchmarks | Every sleeve reports SPY and equal-weight theme buy-and-hold (same window, entry and slippage), `vs_benchmarks` (excess return / CAGR, Sharpe diff) and `beats_benchmarks` = Sharpe above both and higher CAGR than both |
| Agents live | Momentum, StatisticalML, Macro (macro proxies also cached) |
| Fundamental | **As-of statements** — FMP `filingDate` when the API returns history; else cached yfinance annual/quarterly frames gated by period_end + filing lag (45d Q / 60d A). Market cap from as-of close × shares. **No live Yahoo `.info`** (avoids crumb 401 loops). Approximation, not full SEC PIT. |
| Sentiment | **Proxy** — 5d return + volume shock vs ~20d avg (clearly labeled). Used when historical news cannot be replayed cleanly. |
| Risk knobs | Backtest CLI overrides confidence / `|score|` floors on a **config copy** only. Live `run-once` still reads `config/risk.yaml` (55 / 40 / 0.05). |
| Isolation | Separate in-memory/temp Tracker per sleeve — a quantum fill never touches space cash |

**Caveats:** Yahoo adjustments / survivorship; fundamentals filing-lag is conservative but not true PIT; sentiment proxy is not news; `--fill-on close` is optimistic vs the next_open default; paper only — never writes live Tracker / Alpaca.

## TODOs

- **StatisticalMLAgent (v2):** OOS supervised model on frozen feature store; walk-forward only (v1 is rule-based numpy/pandas features)
- **Weights:** implement 90d/1y OOS Sharpe reweight job (CLI hook)
- **Sharpe vs SPY:** performance report job from `portfolio_snapshots` + SPY history

## License

Private — ZuskiVibeCode.
