# Harness upgrade: design

Draft by Claude Code, Oct 5, 2026. Follows `docs/claude-handoff.md`. PR 1
(agent replay fixes) is built; the rest is design. Decisions and remaining open
questions for Sabastian are at the end.

## The main design choice: two engines, one scoreboard

The current harness (`quant_system/backtest/harness.py`) replays the real agents
day by day, through the PortfolioManager and a SQLite Tracker for each sleeve.
That is the right tool for "do our existing agents work", but it is far too slow
for bot experiments: 10 years x a few hundred stocks x 5 agents, rerun for every
idea and every robustness nudge.

So the upgrade has two engines that share everything except the strategy logic:

| | Agent replay (`backtest-themes`, exists) | Strategy lab (`lab`, new) |
|---|---|---|
| Strategy | The live agents + PortfolioManager | A bot's YAML spec |
| Speed | Slow (per-day, per-agent) | Fast (vectorized pandas) |
| Used for | Agent scorecard, theme sleeves | Bot invention and the gauntlet |

Shared pieces, written once: bar data + cache, time windows, fill and cost
model, benchmarks, metrics, trial log.

## Code layout

```
quant_system/lab/
  data.py          # Alpaca bars provider, shared cache, data ceiling
  windows.py       # discovery / validation / holdout, unlock rules
  costs.py         # next-open fills, slippage by liquidity bucket
  benchmarks.py    # SPY + equal-weight buy-and-hold
  metrics.py       # CAGR, Sharpe, max DD, turnover, trades, vs-benchmark, deflated Sharpe
  trials.py        # trial log
  indicators/      # pure indicator functions + registry
  spec.py          # spec schema (pydantic) + safe rule expressions
  engine.py        # vectorized spec runner
  robust.py        # gauntlet step 2
config/lab.yaml    # windows, cost buckets, data ceiling, universe file path
config/lab_universe.yaml  # broad universe: ticker, sector, cap bucket
```

CLI, all under `python -m quant_system lab ...`:

- `lab data sync`: download/refresh the cache (one bot or routine does this, not every bot)
- `lab run <spec.yaml>`: score a spec on discovery, log the trial, print a scorecard
- `lab robust <spec.yaml>`: gauntlet step 2
- `lab trials`: trial count and recent results
- `lab indicators`: list registered indicators and their settings

## 1. Data

- **Provider:** `alpaca-py` `StockHistoricalDataClient`, daily bars,
  `adjustment=all` (splits + dividends). The paper keys already on Grok's
  machine should work for the data API.
- **Feed:** must be SIP, not IEX. IEX volume is a small slice of the market and
  would put every stock in the wrong liquidity bucket. The free plan is meant to
  allow SIP for history older than 15 minutes. **To verify on first run.**
- **Not on Alpaca:** `^VIX` and `^TNX` (indexes). MacroAgent needs them, so
  agent replay keeps using yfinance for those two. The lab does not need them
  at first.
- **Shared cache:** one directory outside every checkout, set with
  `QUANT_LAB_DATA_DIR` (for example `/workspace/lab-data/bars/`). CSV per
  ticker, like the current cache. Bots read it and never call the API per test.
- **Data ceiling:** `config/lab.yaml` sets `data_ceiling: 2021-12-31` on Grok's
  machine. `lab data sync` never downloads past it, and the loader drops any row
  past it even if one shows up in the cache. Validation/holdout machines raise
  the ceiling by environment variable.
- **Warm-up:** Alpaca data starts in 2016, so a 200-day indicator has no values
  until about October 2016. The engine reports the first date it actually
  traded. Validation and holdout use late-2021 / 2024 bars for warm-up only.

## 2. Fills and costs

- Signal at day *t*'s close, fill at day *t+1*'s open. Default for both engines.
- **Bug in the current harness:** with `fill_on="next_open"`, when no next bar
  exists it falls back to the *same* day's open (`harness.py:69-70`), which is
  a price from before the signal. Fix: no next bar, no fill.
- **Slippage by liquidity**, from the 20-day median dollar volume as of the
  signal day (no lookahead). Starting numbers, to be confirmed:

  | 20d median $ volume | Slippage per side |
  |---|---|
  | > $50M | 5 bps |
  | $10M-50M | 15 bps |
  | $2M-10M | 30 bps |
  | < $2M | not tradable |

- Alpaca charges no commission, so cost = slippage only.

## 3. Benchmarks

Every result includes, for the same window and with the same costs:
- SPY buy-and-hold
- Equal-weight buy-and-hold of the strategy's universe (equal weight at entry,
  no rebalancing; names with no bar on the entry day are excluded and listed)

Agent replay gets the same columns per theme sleeve, so the theme leaderboard
can finally be judged.

## 4. Windows

| Window | Dates | Who runs it | Rule |
|---|---|---|---|
| discovery | 2016-01-01 to 2021-12-31 | Bots, on Grok's machine | Unlimited runs |
| validation | 2022-01-01 to 2024-12-31 | Outside Grok | One run per spec, no retuning |
| holdout | 2025-01-01 to present | Outside Grok | One run, at the very end |

Enforced in code in three layers:
1. The data isn't there (data ceiling).
2. `lab run --window validation|holdout` refuses unless `QUANT_LAB_UNLOCK` names
   that window.
3. Each spec has a content hash. Validation and holdout record the hash and
   refuse a second run of the same spec, so "one run" is real.

**The honest limit:** Grok's machine runs live trading, so yfinance and Alpaca
both hand out 2022+ data there. A bot that writes its own script can see recent
prices. What the design guarantees is that no bot ever sees a validation or
holdout *score*.

## 5. Trial log

- Every `lab run` writes one JSON file to a shared trials directory
  (`$QUANT_LAB_DATA_DIR/trials/<timestamp>-<bot>-<hash>.json`). One file per
  trial means several bots can write at once with no locking.
- Contents: spec, spec hash, author bot, git commit, window, metrics, and the
  daily return series (needed for deflated Sharpe).
- Robustness runs are logged as `kind: robustness` under their parent spec.
  They don't count as new trials, since they test one idea, not new ones.
- Deflated Sharpe (Bailey and Lopez de Prado) uses the number of discovery
  trials and the spread of their Sharpes. The trial count is frozen into each
  candidate when it is nominated for validation.

## 6. Strategy spec

Bots write YAML, not Python. Example:

```yaml
id: rsi-pullback-in-uptrend
author_bot: <botname>
universe: broad            # broad | themes | quantum | space | robotics
indicators:
  rsi:   {fn: rsi, length: $rsi_len}
  trend: {fn: sma, length: 200}
params:                    # at most 4 tunable settings
  rsi_len:     {value: 14, min: 7,  max: 28}
  entry_level: {value: 30, min: 20, max: 40}
entry: "rsi < entry_level and close > trend"
exit:  "rsi > 55 or bars_held >= 20"
rank:  "-rsi"              # optional: when more entries than slots
sizing: {method: equal_weight, max_positions: 10, max_weight: 0.20}
```

- Rules are a small expression language (comparisons, `and`/`or`/`not`,
  arithmetic, indicator names, `close`/`open`/`high`/`low`/`volume`,
  `bars_held`). Parsed with Python's `ast` and an allowlist, never `eval`.
- The schema rejects more than 4 tunable settings.
- `max_weight` is capped at 0.20, the live per-position limit.

## 7. Indicators

- Pure functions in `quant_system/lab/indicators/`, registered with a decorator:
  bars in, series out, no I/O, no global state.
- **One generic lookahead test covers every registered indicator**, including
  new ones, so a bot can't forget to write it. For random cut-off dates it
  checks that values up to the cut-off are identical whether computed on full
  history or history truncated at the cut-off.
- New-indicator PRs also need their own unit test of the math.
- Starter set: SMA, EMA, RSI, MACD, ATR, Bollinger %B, rate of change, z-score,
  relative strength vs SPY, relative volume. Later these can be shared with
  MomentumAgent.

## 8. Metrics

CAGR, annualized Sharpe (daily returns, risk-free = 0 for now), max drawdown,
turnover, trade count, exposure (average % invested), and for each benchmark:
excess CAGR, Sharpe difference, beta, information ratio. Discovery adds
deflated Sharpe once there are enough trials.

## 9. Robustness (`lab robust`)

- Each setting nudged -20%, -10%, +10%, +20%, one at a time. Fail if the
  strategy only works at the exact setting.
- Split by sector and by cap bucket (from `config/lab_universe.yaml`).
- Selloffs inside discovery: Q4 2018 and Feb-Apr 2020.
- Single-stock dependence: rerun with the top contributing stock removed.

## 10. Agent replay changes (`backtest-themes`)

- `next_open` becomes the default; fix the same-bar fallback.
- Add the benchmarks per sleeve.
- Accept `--window`, with the same data-ceiling and unlock checks.
- The confidence-floor overrides are already in the JSON `meta`. Also print
  them under the CLI table so they're never missed.
- Orders are queued at the close and filled at the next session's open, so
  daily returns line up with when trades actually happen.

## Order of PRs

1. Agent replay fixes: next-open default, same-bar fix, benchmarks, overrides
   printed. Small, useful on its own.
2. Data layer: Alpaca provider, shared cache, data ceiling, broad universe file.
3. Windows, cost model, metrics (with deflated Sharpe), trial log.
4. Indicators + generic lookahead test, spec schema, engine, `lab run`.
5. `lab robust`.
6. Validation/holdout runner outside Grok (depends on open question 2).

Each PR: own branch, `pytest` passing, tests with synthetic bars (no network).

## Things the handoff plan needs to account for

- **The theme names barely exist in discovery.** IONQ, RGTI, QBTS, QSI, RKLB,
  ASTS, LUNR, RDW, BKSY, PATH and SYM listed in 2021-2023. The 2016-2021
  theme set is mostly big companies (IBM, MSFT, NVDA, BA, LMT...). So the theme
  check (gauntlet step 5) can only really happen in validation and holdout.
- **The 2022 selloff is in the validation window.** Gauntlet step 2 lists it as
  a robustness check, but bots can't touch 2022. Proposal: robustness uses 2018
  and 2020 only, and the validation report breaks out 2022 on its own.
- **Choosing today's liquid stocks for the broad universe favors survivors.**
  Stocks that are big today are the ones that did well. This is the same bias
  as the missing delisted stocks, and the Norgate/Sharadar recheck covers both.

## Decided (Sabastian, Oct 5, 2026)

- **PR workflow:** Claude Code pushes `claude/<task>` branches and opens PRs for
  Chief of Staff to review and merge. It never pushes to main.
- **Validation and holdout** run in Claude Code on Sabastian's machine, not on
  Grok's machine or in GitHub Actions (the bot account can read Action logs).
  Python 3.12 is now installed there; Alpaca paper keys in a local `.env` are
  still needed.
- **Pass bar:** Sharpe after costs above both SPY's and equal-weight
  buy-and-hold's, and higher CAGR than both. Validation also needs deflated
  Sharpe above 0.95 probability. PR 1 reports this as `beats_benchmarks`.

## Open questions for Sabastian

1. **Broad universe list.** Which few hundred names? Suggestion: S&P 500 plus
   S&P 400 midcaps that have Alpaca data back to 2016, trimmed to ~300 across
   sectors.
2. **Slippage numbers** in section 2: OK as a start?
3. **Live risk limits in the lab.** Recommendation: enforce the 20% position
   cap, but don't halt at 15% drawdown (over 10 years almost everything would
   halt in March 2020). Report drawdown instead.
4. **2022 selloff and theme check:** OK to handle them in validation as above?
