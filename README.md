# Multi-Agent Quant Trading System

Paper-trade US stocks in **quantum computing**, **space**, and **robotics** themes.
Designed to run under **Grok Bot** (CLI + SQLite) — not LangGraph/CrewAI/Telegram/VM.

**Goal:** $100 paper account; beat S&P 500 on a risk-adjusted (Sharpe) basis. Crypto out of scope.

## Hard constraints (enforced in code)

- **Paper only:** Alpaca when `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` present and `ALPACA_PAPER=true`; otherwise dry-run simulation
- Max position **20%** equity; max daily loss **5%** (day halt); max drawdown **15%** (halt + audit alert)
- No withdraw / transfer / account-settings mutations
- Kill switch: `python -m quant_system halt` / `resume`
- Every decision logged with reasoning, timestamp, data snapshot (Tracker SQLite)

## Architecture

```
Research (signals only)
  MomentumAgent | FundamentalAgent (stub) | StatisticalMLAgent
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

### Universe (`config/universe.yaml`)

| Theme | Tickers |
|-------|---------|
| Quantum | IONQ, RGTI, QUBT, IBM, GOOGL |
| Space | RKLB, ASTS, LUNR, BA, LHX |
| Robotics | ISRG, PATH, TER, NVDA, TSLA |
| Benchmark | SPY |

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
```

Optional Alpaca paper keys (never commit secrets):

```bash
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

## Package layout

```
quant_system/
  models/          # Frozen Signal, TradeTicket, HoldDecision
  tracker/         # SQLite schema + Tracker API (FIRST)
  research/        # 5 research agents (signals only)
  execution/       # PortfolioManager + ExecutionAgent
  oversight/       # Auditor (flags only)
  market_data/     # MarketData interface + yfinance + mock
  config/          # YAML loaders
  pipeline.py      # run-once orchestration
  cli.py           # run-once | halt | resume | status | digest
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

## TODOs

- **FundamentalAgent:** wire fundamentals provider; valuation vs theme peers; dilution/cash-burn flags
- **SentimentAgent:** yfinance headlines; lexicon polarity; credibility by source; pump heuristics (`manipulation_risk` only on coordinated hype)
- **StatisticalMLAgent:** OOS supervised model on feature store; walk-forward only
- **MacroAgent:** rates/credit/VIX proxies; theme beta overlays
- **Weights:** implement 90d/1y OOS Sharpe reweight job (CLI hook)
- **Sharpe vs SPY:** performance report job from `portfolio_snapshots` + SPY history

## License

Private — ZuskiVibeCode.
