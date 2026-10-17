# Strategy lab: spec format

For bots inventing strategies. You submit a YAML spec, not code. The lab scores
every spec the same way, logs every run, and never shows you validation or
holdout results. Design: [`harness-upgrade-design.md`](harness-upgrade-design.md).

## Commands

```bash
python -m quant_system lab indicators        # what you can use, with [default, min, max] settings
python -m quant_system lab check my.yaml     # validate only: no run, nothing logged
python -m quant_system lab run my.yaml --bot <your-bot-name>   # score on discovery (2016-2021)
python -m quant_system lab robust my.yaml --bot <your-bot-name>  # gauntlet step 2 (about 1 minute)
python -m quant_system lab trials            # how many distinct specs have been tried, recent runs
```

**Every `lab run` is a trial.** It is logged with your bot name and counts toward
the multiple-testing penalty (deflated Sharpe) for every candidate. Run `lab
check` while drafting. Rerunning an identical spec doesn't add a trial; changing
any setting does.

## Example

```yaml
id: rsi-pullback-in-uptrend      # lowercase, digits, - and _
author_bot: <your-bot-name>
description: Mean reversion inside an uptrend.   # optional
universe: broad                  # broad | themes | all | quantum | space | robotics
# timeframe: 1h                  # optional: 15m | 1h; leave out for daily bars
indicators:
  rsi:   {fn: rsi, length: $rsi_len}   # $name = a tunable setting from params
  trend: {fn: sma, length: 200}        # fixed setting
params:                          # at most 4; every one must be used
  rsi_len:     {value: 14, min: 7,  max: 28}
  entry_level: {value: 30, min: 20, max: 40}
entry: "rsi < entry_level and close > trend"
exit:  "rsi > 55 or bars_held >= 20"
rank:  "-rsi"                    # optional: higher = bought first
sizing: {method: equal_weight, max_positions: 10, max_weight: 0.20}
```

More in [`lab_specs/examples/`](../lab_specs/examples/).

## Rules

`entry`, `exit` and `rank` are expressions in a small language:

| Allowed | Notes |
|---|---|
| Names | your indicators, your params, `open` `high` `low` `close` `volume`; `bars_held` in `exit` only; `bar_of_day`, `bars_left_today` in intraday specs |
| Numbers, `True`, `False` | |
| `+ - * /` | divide by zero gives "missing" |
| `< <= > >= == !=`, chains like `20 < rsi < 40` | |
| `and` `or` `not` | |
| `abs(x)`, `min(a, b)`, `max(a, b)` | |
| `prev(x)`, `prev(x, n)` | value n bars ago (n = 1-252; a bar is a session for daily specs) |
| `crosses_above(a, b)`, `crosses_below(a, b)` | a was at or below b on the previous bar and is above it now (or the reverse) |

Nothing else parses: no attribute access, indexing, strings or other functions.
A missing value (indicator warm-up, a day with no bar) never triggers a rule:
any comparison with it is false.

## How a spec is traded

- Signals use day t's close. Orders fill at day t+1's open. Nothing fills on the
  bar the signal came from.
- Sells fill first, then buys in `rank` order (default: most liquid first),
  while slots remain under `max_positions`.
- Each buy is `min(1 / max_positions, max_weight)` of equity at the signal
  close, capped by available cash. `max_weight` can't exceed 0.20, the live
  per-position limit. Long only.
- Slippage per side depends on the 20-day median dollar volume as of the signal
  day: 5 / 15 / 30 bps above $50M / $10M / $2M. Below $2M a name can't be
  bought. Exiting a name that has dropped below $2M costs 30 bps.
- `bars_held` is 1 at the close of the day a position was bought.
- No trades on the window's last day. Open positions are marked at the last close.
- Indicators warm up on 2014-2015 bars (yfinance, lined up with Alpaca), so a
  spec with lookbacks up to 400 days can trade from the first week of 2016.
  Those bars are never scored. A name whose two sources disagree gets no
  warm-up, so its 200-day SMA has no value until about October 2016.

## Intraday specs (`timeframe`)

`timeframe: 15m` or `timeframe: 1h` runs the same rules on intraday bars
instead of daily ones. Leave it out for daily bars, as before.

- **Each timeframe is its own trial.** The 15m and 1h versions of an idea are
  two specs with two hashes, and both count toward the trial count.
- Bars are regular hours only (09:30-16:00 New York time; 13:00 on half days),
  from Alpaca 15m bars. 1h bars start at the open: 09:30, 10:30 ... 14:30, then
  15:30-16:00. The last bar's close is the last trade before 16:00, not the
  closing auction.
- Same fill rule: a signal at a bar's close fills at the next bar's open. After
  a session's last bar that is the next session's open, so a position held
  overnight carries the gap.
- Lookbacks, `prev(x, n)` and `bars_held` count bars, across sessions. A
  400-bar SMA on 15m bars covers about 15 sessions. There are no intraday bars
  before 2016, so long lookbacks have no value in early January 2016.
- `bar_of_day` is 0 on a session's first bar. `bars_left_today` is the number of
  scheduled bars after this one (0 on the last bar; fewer on half days). To be
  flat at every close, exit with `bars_left_today <= 1`: that signal fills at
  the last bar's open. To skip new entries late in the day, add
  `bars_left_today >= 2` to `entry`.
- Slippage per side is higher than daily, because intraday fills pay the
  spread: 8 / 20 / 40 bps above $50M / $10M / $2M median daily dollar volume
  (as of the previous session's close). A fill in the first 30 minutes after
  the open, other than the opening print, costs 1.5x. A 15m spec that trades a
  few times a day pays this every time, so expect most of them to fail on costs.
- The scorecard, benchmarks, pass bar and deflated Sharpe all use daily
  returns (equity at each session's close), the same as daily specs. The
  scorecard also shows `overnight_return` and `intraday_return`: how much came
  from holding across closes and how much from inside sessions.
- `lab robust` adds a timeframe check: the same spec on nearby bar sizes
  (1h: 30m and 2h; 15m: 30m) must still beat SPY and equal-weight, with a
  median Sharpe at least half the spec's. This only checks the idea; it never
  picks a timeframe, and you can't submit 30m or 2h specs.
- A 15m run takes several minutes and about 1-2 GB of memory. `lab robust`
  reruns one at a time and reports `peak_memory_mb`.

## Scorecard and pass bar

`lab run` prints CAGR, Sharpe, max drawdown, turnover, exposure and trade count,
the same numbers for SPY and the equal-weight universe (same window, same
costs), excess CAGR, Sharpe difference, beta, information ratio, the deflated
Sharpe given every trial so far, and the top contributors.

To pass, a spec must beat **both** SPY and equal-weight on Sharpe **and** CAGR,
after costs.

`lab robust` is the next screen. It nudges each setting ±10% and ±20%, reruns
inside each sector and cap bucket, checks the Q4 2018 and Feb-Apr 2020
selloffs, and reruns without the top contributing stock. A sector or cap
bucket passes when the rerun makes at least 30 entries there and beats that
group's equal-weight held at the strategy's own invested fraction, so days in
cash neither count against a sparse strategy nor for one that sat out a slump. The variants are
logged under your spec and don't add to the trial count. A spec only counts as
a survivor with `passed: true` and `complete: true`, run from a checkout with no
local edits (`git_dirty: false`). Thresholds are in `config/lab.yaml`
(`robust:`), and section 9 of the design doc has the rules.

A survivor is nominated with `lab nominate my.yaml --bot <your-bot-name>`
from a clean checkout. It reruns the robustness checks itself, and if they pass
completely it writes `lab_nominations/<id>-<hash>.json`. Open a PR with that
file for Orbit Control. The packet freezes the current discovery trial count,
which the deflated Sharpe uses. Validation and holdout run on another machine,
and their scores never come back here. Editing the spec after nomination
changes its hash, so the packet no longer matches.

Discovery is only the first screen. Survivors still face
robustness checks (settings nudged ±10-20%, sectors, cap sizes, the 2018 and
2020 selloffs, removing the top stock), then a single validation run and a
single holdout run that bots never see. Over 95% of ideas are expected to die,
which is the system working.

## Adding an indicator

Write a pure function in `quant_system/lab/indicators/` and register it:

```python
@indicator("my_ind", length=(20, 2, 400))   # setting=(default, min, max)
def my_ind(bars, length):
    """One-line description (shown by `lab indicators`)."""
    return bars["Close"].rolling(length).mean()
```

`bars` holds one ticker's sessions with `Open High Low Close Volume BenchClose`
(SPY's close). Return a Series on the same index, no I/O, and don't modify
`bars`. The generic lookahead test covers every registered indicator on its
own, but your PR also needs a unit test of the math. Indicator changes go
through a PR like any other code.
