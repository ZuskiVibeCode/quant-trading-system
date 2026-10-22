# Harness upgrade: design

Draft by Claude Code, Oct 5, 2026. Follows `docs/claude-handoff.md`. PRs 1-6
(agent replay fixes; data layer; windows, costs, metrics, trial log; indicators,
spec, engine, `lab run`; `lab robust`; nomination, validation and holdout) are
built. What's left is setting up the validation machine. Decisions and remaining open
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
- **Discovery warm-up (Oct 15, 2026):** `lab data warmup` caches yfinance bars
  for 2014-01-01 to 2016-01-31 under `$QUANT_LAB_DATA_DIR/bars_warmup/`, once
  per machine (the validation machine needs them too, to recompute the
  discovery gate). On load, each name's rows before 2016-01-01 go in front of
  its Alpaca bars, scaled by the median Alpaca / yfinance close ratio over the
  January 2016 days both have. They feed indicators and the cost model only;
  nothing before 2016-01-01 is scored, and Alpaca is the only source from then
  on. A name gets no warm-up (and starts late, as before) when the sources
  disagree on those days: fewer than 10 shared days, any day's close ratio more
  than 1% off the median, or median volume more than 1.5x apart. Settings in
  `config/lab.yaml` `data.warmup`; `lab data status` lists the rejected names.
  Each trial records `universe.warmup_names`.

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

As built (PR 3, `lab/benchmarks.py`): both benchmarks enter on the same day,
the first fill any strategy could get. The signal day is the first window
session on which the cost model can price a trade (20 bars of history: session
1 of discovery with the pre-2016 warm-up bars, about session 20 without them;
session 1 of validation thanks to late-2021 bars), and the fill is the next
session's open with each name's own slippage. With warm-up, discovery's
benchmarks enter 2016-01-05; a name with no warm-up isn't tradable that day,
so it is left out of equal-weight (listed under `excluded`).
No exit cost at the window end, for strategies or benchmarks.

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

As built (PR 3, `lab/windows.py`): window dates are code constants, not
`config/lab.yaml` entries, so moving a window is a reviewed code change rather
than a config edit. Unlocking one window does not unlock the other. Locked
windows also refuse robustness runs. The validation/holdout runner (PR 6) must
write the trial record before it prints the score, so a crash can't allow a
second run.

## 5. Trial log

- Every `lab run` writes one JSON file to a shared trials directory
  (`$QUANT_LAB_DATA_DIR/trials/<timestamp>-<bot>-<hash>.json`). One file per
  trial means several bots can write at once with no locking.
- Contents: spec, spec hash, author bot, git commit, window, metrics, and the
  daily return series (needed for deflated Sharpe).
- Robustness runs are logged as `kind: robustness` under their parent spec.
  They don't count as new trials, since they test one idea, not new ones.
- As built (PR 3, `lab/trials.py`): a trial is a distinct spec hash. The hash
  ignores labels (`id`, `author_bot`, `description`, `notes`), so renaming a
  spec neither adds a trial nor earns a second validation run. Rerunning the
  same spec replaces its score (latest record wins) instead of adding a trial.
  `lab trials [--window]` prints the count and recent runs.
- `lab rescore` (after a scoring change, from a clean checkout): reruns the
  latest record of every logged discovery spec, so each spec's score moves to
  the new method and the trial count stays the same. Reruns keep the spec's
  author and carry `rescore: true`; the lab journal leaves them out of the
  day's work. A spec that no longer loads or hashes differently is skipped and
  listed, since rerunning it would add a trial. It refuses to run until
  `lab data warmup` has cached the warm-up bars.
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

As built (PR 4, `lab/spec.py`, `lab/rules.py`, `docs/lab-spec-format.md`):
- Rule functions: `abs`, `min`, `max`, `prev(x, n)` (n 1-252),
  `crosses_above`, `crosses_below`. `prev`/`crosses_*` can't look at
  `bars_held`; `bars_held` works in `exit` only.
- Every param must be used (no padding the four slots). Indicator settings
  have hard ranges in the registry; a spec's own `min`/`max` bound robustness
  nudges.
- `rank` is optional; the default is most liquid first (20-day median dollar
  volume), not alphabetical.
- `lab run` scores discovery only and always logs. There is no flag to skip
  logging and still get a score. `lab run --smoke` tests the pipeline: it runs
  the spec end to end but returns only whether it ran, its trade count and
  its runtime, with no returns, Sharpe, benchmarks or verdict. Since nothing
  about performance comes back, it is not a trial. It is recorded under
  `$QUANT_LAB_DATA_DIR/smoke/`, outside the trial log. `lab check` validates
  without running. The validation/holdout
  runner comes in PR 6 (`score_spec` already takes frozen trial stats and
  writes the record before returning).
- Engine semantics (`lab/engine.py` docstring): fills at the next open, sells
  first, then buys in rank order sized `min(1/max_positions, max_weight)` of
  the signal-close equity. The 20% cap applies when buying, like the live
  PortfolioManager, and drift isn't trimmed. A held name that drops below the
  $2M bucket exits at 30 bps. No orders on the window's last day.
- Discovery deflated Sharpe is shown, not stored: it changes as trials accrue.

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

As built (PR 4): settings are declared `(default, min, max)` in the decorator.
The generic test runs `lookahead_violations` on every registered indicator at
default and raised settings, and its own tests show it catches a peeking
indicator, a centered window and an input-mutating function. The engine also
has an end-to-end lookahead test: rewriting every bar after day X leaves
returns through X unchanged. Added beyond the starter list: `macd_hist`,
`atr_pct`, `donchian_high`/`donchian_low` (prior n bars, for breakouts).

Volume studies (`lab/indicators/volume.py`), so bots start with roughly what a
charting tool like TrendSpider offers: `obv_trend` (OBV over n bars as a share
of volume, -1 to +1), `cmf` (Chaikin money flow), `chaikin_osc` (in units of
average daily volume), `mfi` (money flow index), `vwap_dist` (close vs rolling
VWAP), `avwap_high_dist` / `avwap_low_dist` (close vs the VWAP anchored at the
highest high or lowest low of the last n bars), and `volume_osc`, alongside
`rel_volume` and the raw `volume` field. Running totals (OBV, the A/D line)
are windowed or normalized, so values compare across stocks and don't depend
on where the history starts. Not included: volume profile (point of control,
value area). It needs intraday volume to mean much, and a daily approximation
is slow. VWAPs are over whole days for the same reason.

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

As built (PR 5, `lab/robust.py`; thresholds in `config/lab.yaml` `robust:` and
copied into every report):

| Check | How | Passes when |
|---|---|---|
| nudges | each setting x0.8 / 0.9 / 1.1 / 1.2, one at a time, clipped to the spec's own min/max (duplicates dropped) | at least 75% of variants still beat SPY and equal-weight, and their median Sharpe is at least 50% of the spec's (no sharp peak) |
| sectors | rerun inside each sector of the broad universe with 10+ names (same sizing) | at least 60% of sectors (7 of 11) pass: 30+ entries, and an average daily return above that sector's equal-weight held at the strategy's own invested fraction each day |
| cap buckets | rerun inside large / mid / small | same rule, at least 60% (2 of 3) |
| selloffs | slice the spec's own run: Q4 2018, Feb-Apr 2020 | worst drawdown in each is at most 1.25x the worse of SPY's and equal-weight's |
| single stock | rerun without the top contributing name | still beats SPY and equal-weight |

- Sector and cap subsets are judged against their own equal-weight, not SPY:
  the question is whether the rule adds value inside the group.
- Exposure-matched (Sabastian, Oct 15, 2026; until then the subset's Sharpe had
  to beat the always-invested equal-weight's). Inside one sector a sparse
  strategy sits in cash most days, and with no cash yield that alone shrinks
  its Sharpe by about the square root of its invested fraction, whatever its
  picks do. Sitting in cash also earned a trend strategy a "win" in a sector
  that slumped. The matched benchmark holds the sector's equal-weight at the
  strategy's invested fraction: close to open at the previous close's
  fraction, open to close at the fraction right after the open's fills. So it
  asks whether the picks beat the group while invested. A sector with fewer
  than 30 entries (`min_subset_entries`) counts as a miss and stays in the
  denominator; reports list them under `thin`. Sizing doesn't change. Each
  group row keeps the old verdict as `beats_equal_weight_sharpe` for
  comparison. Orbit Control reruns `lab robust` on every spec that passes the
  discovery benchmark; if most specs that failed before now pass, treat it as
  a bug. The results stand, with no second round of rule changes.
- `passed` needs the spec itself to pass the discovery bar and no check to
  fail. A check with nothing to test (no settings, a theme universe with no
  sector data, no profitable name) is `skipped`. That doesn't fail the spec,
  but the report says `complete: false`, and nomination (PR 6) should require
  a complete report.
- The spec is scored and logged as a normal trial; variants are logged as
  `kind: robustness` with `parent_hash` and don't count as trials. The report
  is saved to `$QUANT_LAB_DATA_DIR/robust/`.
- Every trial and robust report now records `git_dirty`: whether the checkout
  had local edits to `quant_system/` or `config/`. Bots run in their own
  checkouts, so a locally loosened threshold would show up there. Nomination
  should reject dirty runs.
- Specs must now keep each `$setting`'s min/max inside the indicator's own
  range, so every nudge is a valid spec.
- Runtime: about 25 runs per spec (base, up to 16 nudges, 11 sectors, 3 caps,
  1 without the top name), about 26 s on a synthetic 300-name universe.

## 10. Agent replay changes (`backtest-themes`)

- `next_open` becomes the default; fix the same-bar fallback.
- Add the benchmarks per sleeve.
- Accept `--window`, with the same data-ceiling and unlock checks.
- The confidence-floor overrides are already in the JSON `meta`. Also print
  them under the CLI table so they're never missed.
- Orders are queued at the close and filled at the next session's open, so
  daily returns line up with when trades actually happen.

## 11. Nomination, validation and holdout (PR 6)

As built (`lab/nominate.py`, `lab/final.py`). Three commands carry a spec from
discovery to its two single runs:

| Command | Where | What it does |
|---|---|---|
| `lab nominate <spec>` | Grok's machine, clean checkout | Reruns `lab robust` itself (an earlier report is not trusted). Only `passed` and `complete` nominates. Writes `lab_nominations/<id>-<hash>.json`: the spec, its hash, and the discovery trial count and Sharpe variance, **frozen** right after the robust run (so the spec itself is counted) |
| `lab validate <packet>` | Validation machine, `QUANT_LAB_UNLOCK=validation` | The single 2022-2024 run, after the checks below |
| `lab holdout <packet>` | Validation machine, `QUANT_LAB_UNLOCK=holdout` | The single 2025-to-ceiling run, for specs whose validation record passed |

- **Packets travel by PR.** The nominating bot opens a PR adding its packet to
  `lab_nominations/`, and Orbit Control reviews it. The frozen count is then
  reviewed and kept in git history, and the validation machine gets packets
  with `git pull`. A packet holds discovery numbers only, which bots can see anyway.
- **The validation machine trusts only the frozen trial stats.** Everything
  else on Grok's machine is a file any bot could write, including trial records
  and robust reports. So `lab validate` reruns the full robustness gauntlet on
  discovery with its own data and its own reviewed code, and stops if the spec
  doesn't pass with a complete report. A spec that fails there never gets a
  validation score. This answers PR 5's review note that `evaluate()` can score
  without logging: no score a bot produced reaches the gate.
- **The gate reads the trial log, not the report.** For both `nominate` and
  `validate`, the spec's own pass/fail is recomputed from the discovery record
  that run wrote to the trial log, and that record must have `git_dirty: false`.
  The robust report must have `complete: true`, `git_dirty: false`, and no failed
  check. A report whose base run isn't in the log is rejected.
- **Machine guard.** `validate` and `holdout` refuse before anything else unless
  the data ceiling is past 2021-12-31 **and** `QUANT_LAB_UNLOCK` names the
  window. Grok's machine keeps the 2021-12-31 ceiling and never sets the
  unlock, so they can't run there by accident. Confirmed by Sabastian and Orbit
  Control, Oct 6, 2026.
- **Order of checks in `lab validate`:** machine guard, then clean checkout
  (`quant_system/` and `config/` match the commit), then the window rules (unlock, one run per hash),
  then trial-count order, then the discovery recheck, then the run. Everything
  that can refuse comes before any scoring.
- **The trial count can't go backwards.** If a packet nominated later carries
  fewer discovery trials than one already validated (or an earlier one carries
  more), `lab validate` refuses. That means the shared trial log lost records,
  or someone removed some to make the deflated Sharpe easier to pass. This is
  a tamper signal, not proof: a bot that deletes records before the *first*
  nomination isn't caught. The PR history of `lab_nominations/` is the other
  check.
- **Record before print.** The verdict, the frozen stats, the nomination
  details, the recheck summary and the breakouts all go into the one trial
  record, which is written before anything is returned. A crash before the write
  leaves no record and no score has been seen, so a rerun is fine. A crash after
  the write blocks a second run.
- **Breakouts, reported but not in the pass bar:** the 2022 bear market
  (`config/lab.yaml` `locked.selloffs`, Jan 3 to Oct 31, 2022, flagged with
  robust's `selloff_dd_ratio`), and the spec rerun on each theme's names and on
  all of them together. This is the theme check from gauntlet step 5. The pass
  bar stays as decided: beats SPY and equal-weight on Sharpe and CAGR, plus
  deflated Sharpe > 0.95.
- **Holdout** uses the frozen stats stored in the passed validation record, not
  the packet, so a re-issued packet can't change the count. It skips the
  discovery recheck because validation already did one.
- **Clean checkout, checked fresh.** `nominate`, `validate` and `holdout` re-read
  git status at the start rather than using the per-process cache (PR 5 review
  note). They refuse when `quant_system/` or `config/` has local edits, or when
  git can't tell.
- `n_records` is informational. Each robust run, including the reruns at
  nomination and validation, logs the base spec again, so records grow while
  `n_trials` (distinct hashes) doesn't. Nothing gates on `n_records`.
- Validation and holdout records live only in the validation machine's trial
  log (`QUANT_LAB_DATA_DIR` there). Never copy them to Grok's machine. Whether
  bots learn pass/fail for a spec is Sabastian's call; the CLI prints the full
  scorecard only to whoever runs it.
- **Still to do on the validation machine (held):** an Alpaca `.env`, a
  `lab data sync --universe all` with `QUANT_LAB_DATA_CEILING=today`, and a
  `QUANT_LAB_DATA_DIR` outside the checkout.

## 12. Intraday bars (Stage 1)

A spec may declare `timeframe: 15m` or `1h` (no timeframe = daily, and daily
spec hashes are unchanged). Code: `quant_system/lab/intraday.py` (data, bar
sizes, panels, coverage) and `engine.run_spec_intraday`.

- **Data.** Only Alpaca 15m bars are downloaded (`lab data intraday`), SIP
  feed, split + dividend adjusted, `data.start` to the machine's ceiling, so
  Grok's machine holds 2016-2021 and the validation machine holds 2016 on.
  If Alpaca refuses the feed, the sync stops; it never switches to IEX. Bars
  are kept only inside regular hours from Alpaca's market calendar (cached as
  `_calendar.csv`; 12 early closes in 2016-2021). Alpaca's own 1h bars start
  on the clock hour, so the 09:00 bar mixes in pre-market trades; 30m / 1h /
  2h bars are built here from 15m bars, starting at each session's open
  (1h: 09:30 ... 14:30, then 15:30-16:00). The closing auction prints at 16:00
  and lands outside the session, so the last 15m close is the last trade
  before the close. Storage: `bars_15m/<SYM>.csv.gz`, about 39k rows per name
  for 2016-2021. Every Alpaca request (daily and 15m bars, the calendar) has a
  network timeout (10 s to connect, 60 s between bytes) and two retries, 5 s
  and 15 s apart. After that the sync stops, and a rerun resumes at that name.
- **Coverage.** `lab data intraday-status` reports per name and per bar size
  (15m, 1h): missing bars against the calendar, sessions with no bars (on days
  the daily cache has a bar), the longest gap, and sessions whose first open or
  last close is off the daily bar by over 1% (after scaling by the median
  ratio, since the two caches are adjusted at different times), and sessions
  with under half their bars. Names past the `data.intraday.coverage` limits
  are flagged, not dropped. The report also lists dates where many names are
  short at once. Alpaca's 2016-2021 15m history has four:
  2018-05-02 and 2018-05-03 (about a quarter of the broad universe) and
  2021-04-19 and 2021-10-25 (about two thirds), where most of those names have
  only their opening bar. Renamed tickers (BBWI, VTRS, LIN, DXC...) start late
  in both the daily and the intraday cache.
- **Skipped sessions.** Intraday runs leave out each name's suspect sessions:
  the short ones above, and those whose first open or last close is off the
  daily bar by more than `price_tolerance` (`intraday.session_problems`, the
  same test the coverage report uses; it lists them per name as
  `skipped_sessions`). On a 2016-2021 sample (PYPL, NXPI, PCG, ZION) that is
  4-8 of 1,511 sessions per name. Most are a first trade away from the opening
  auction on a gap day (ZION 2016-04-20: 19.70 vs 19.20). Some are bad prints:
  PCG's last 15m close on 2019-01-24 is 41% above the close. A skipped
  session is blank for that name, so the engine treats it like any missing
  bar: no signal or fill there, indicators skip it, and positions keep their
  last price. The benchmark is never trimmed, because its close feeds every
  name's indicators. Scorecards report `skipped_sessions`. Intraday trials
  logged before this change need `lab rescore`.
- **Fills and costs.** Same rule as daily: signal at a bar's close, fill at the
  next bar's open, which after the session's last bar is the next session's
  open (overnight gaps are part of the trade). Intraday fills pay the spread,
  so `costs.intraday` charges 8 / 20 / 40 bps per side in the same liquidity
  buckets, from the daily 20-day median dollar volume as of the previous
  session's close. A fill in the first 30 minutes after the open, other than
  the opening print, pays 1.5x. `bar_of_day` and `bars_left_today` let a spec
  be flat overnight or skip late entries.
- **Scoring.** The engine runs bar by bar and reports equity at each session's
  close, so metrics, the pass bar, deflated Sharpe and the trial log all use
  daily returns. Benchmarks are the same daily buy-and-holds. Each timeframe
  is its own spec hash, so each counts as a trial in the same discovery pool.
  The scorecard splits the return into overnight (previous close to the first
  bar's open) and intraday.
- **Robustness.** Sector and cap-bucket subsets compare against equal-weight
  held at the strategy's own exposure bar by bar, so trading in and out during
  a session earns no credit for timing the group. Intraday specs add a
  `timeframes` check: the same spec on nearby bar sizes (1h: 30m and 2h; 15m:
  30m) must each beat SPY and equal-weight, with a median Sharpe at least
  `peak_ratio` x the spec's. These are robustness runs, not trials, and 30m /
  2h can't be submitted as specs.
- **Memory.** Grok's machine has about 3 GB free and no swap. Panels are
  float32 bars x names arrays filled one name at a time; one bar size is cached
  at a time; robustness subsets mask names instead of copying; every rerun runs
  in sequence, never in parallel. Robust reports and intraday `lab run` cards
  record `peak_memory_mb`.

## Order of PRs

1. Agent replay fixes: next-open default, same-bar fix, benchmarks, overrides
   printed. Small, useful on its own.
2. Data layer: Alpaca provider, shared cache, data ceiling, broad universe file.
3. Windows, cost model, metrics (with deflated Sharpe), trial log.
4. Indicators + generic lookahead test, spec schema, engine, `lab run`.
5. `lab robust`.
6. `lab nominate` / `lab validate` / `lab holdout` (section 11).

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
  Orbit Control to review and merge. It never pushes to main.
- **Validation and holdout** run in Claude Code on Sabastian's machine, not on
  Grok's machine or in GitHub Actions (the bot account can read Action logs).
  Python 3.12 is now installed there; Alpaca paper keys in a local `.env` are
  still needed.
- **Pass bar:** Sharpe after costs above both SPY's and equal-weight
  buy-and-hold's, and higher CAGR than both. Validation also needs deflated
  Sharpe above 0.95 probability. PR 1 reports this as `beats_benchmarks`.
  The lab's `metrics.pass_bar` applies the deflated-Sharpe check to holdout as
  well as validation (the handoff's gauntlet step 4), and fails any result
  missing either benchmark.

- **Delegated to Claude Code** (Sabastian: "I defer to you on all these"):
  - **Broad universe:** about 300 current S&P 1500 members, roughly 150 from the
    S&P 500 (large), 100 from the S&P 400 (mid) and 50 from the S&P 600
    (small), spread across GICS sectors in proportion to each index. Each name
    must have traded since the first trading day of 2016. Within a sector, names
    are picked by 2016 median dollar volume, which was known when discovery
    starts, rather than by how liquid they are today. Cap bucket = which index
    the name is in today. Built by `scripts/build_lab_universe.py` into
    `config/lab_universe.yaml`, and rebuilt only on purpose. Theme names
    (`config/universe.yaml`, active + bench) are a separate `themes` universe.
  - **Slippage:** the bucket numbers in section 2, as a starting point.
  - **Live risk limits in the lab:** enforce the 20% position cap. No drawdown
    or daily-loss halts; report max drawdown instead.
  - **2022 selloff and theme check:** robustness uses Q4 2018 and Feb-Apr 2020.
    The validation report breaks out 2022 and the theme names on their own.

## Decided (Sabastian, Oct 15, 2026)

- **Discovery warm-up:** load 2014-2015 daily bars from yfinance as warm-up
  only, lined up with Alpaca on the January 2016 days both cover; a name whose
  sources disagree gets no warm-up. Discovery's dates don't change. Keep the
  trial count and rescore every discovery spec once (`lab rescore`). Section 1
  has the details.
- **Process:** decided before any rerun; Orbit Control merges, rescores, then
  reruns the robustness checks on every spec that passes the discovery
  benchmark. If most specs that failed before suddenly pass, treat it as a
  bug. The results stand, with no second round of rule changes.

## Decided (Sabastian, Oct 17, 2026)

- **Intraday Stage 1** (section 12) approved, including the `quant_system/lab/`
  and `config/lab*.yaml` changes. Daily rules and thresholds are unchanged.
  Every timeframe tried is a trial. Stage 2 (a slower bar size for direction,
  a faster one for entries, with a test that a 15m signal can't see the
  current day's daily bar) and Stage 3 (a multi-timeframe inventor bot) come
  later, one PR each.
- `lab robust` on intraday specs runs its reruns one at a time and stays well
  inside the free memory on Grok's machine.

No open questions right now.
