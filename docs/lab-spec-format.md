# Strategy lab: spec format

For bots inventing strategies. You submit a YAML spec, not code. The lab scores
every spec the same way, logs every run, and never shows you validation or
holdout results. Design: [`harness-upgrade-design.md`](harness-upgrade-design.md).

## Commands

```bash
python -m quant_system lab indicators        # what you can use, with [default, min, max] settings
python -m quant_system lab check my.yaml     # validate only: no run, nothing logged
python -m quant_system lab run my.yaml --bot <your-bot-name>   # score on discovery (2016-2021)
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
| Names | your indicators, your params, `open` `high` `low` `close` `volume`; `bars_held` in `exit` only |
| Numbers, `True`, `False` | |
| `+ - * /` | divide by zero gives "missing" |
| `< <= > >= == !=`, chains like `20 < rsi < 40` | |
| `and` `or` `not` | |
| `abs(x)`, `min(a, b)`, `max(a, b)` | |
| `prev(x)`, `prev(x, n)` | value n sessions ago (n = 1-252) |
| `crosses_above(a, b)`, `crosses_below(a, b)` | a was at or below b yesterday and is above it today (or the reverse) |

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
- Indicators warm up on their own: a 200-day SMA has no value until about
  October 2016, and nothing triggers before then.

## Scorecard and pass bar

`lab run` prints CAGR, Sharpe, max drawdown, turnover, exposure and trade count,
the same numbers for SPY and the equal-weight universe (same window, same
costs), excess CAGR, Sharpe difference, beta, information ratio, the deflated
Sharpe given every trial so far, and the top contributors.

To pass, a spec must beat **both** SPY and equal-weight on Sharpe **and** CAGR,
after costs. Discovery is only the first screen. Survivors still face
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
