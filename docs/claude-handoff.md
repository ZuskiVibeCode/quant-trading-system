# Handoff: indicator-bot testing program

Written by Claude (claude.ai chat, Oct 5, 2026) for the next Claude Code session.
Start here, then read `README.md` and the Grok Bot guide (see "When to use the Grok Bot guide" below).

## Goal

Sabastian wants a team of Grok Bots that invent and test indicators, indicator
combinations and brand-new indicators, to improve this paper-trading system.
Core rule: **bots invent strategies, they never grade their own work.** One
shared harness scores everything the same way, and no bot ever sees the final
test data.

## When to use the Grok Bot guide

`docs/grok-bot-guide-for-claude.md` explains how Sabastian's Grok Bots work: one
shared Linux machine, Chief of Staff (owns this project, reviews and merges PRs),
dr eggbot (creates bots), and the PR workflow. Read it at these points:

1. **Now, before designing the harness.** Grok Bots will be the ones running it,
   so design for section 2 (shared machine): a CLI the bots can call, each bot
   working in its own checkout, data cached once, and no validation or holdout
   data stored on that machine.
2. **Before any commit or PR.** Code changes follow sections 9-10: branch per
   task, PR instead of pushing to main, Chief of Staff reviews and merges, tests
   pass first. Open question for Sabastian: you are working on his machine with
   his git login (ZuskiVibeCode, which is on the branch-protection bypass list),
   not as a bot. Ask him whether to open PRs from a branch for Chief of Staff to
   review, or something else, before pushing anything.
3. **Before writing `docs/bot-team-spec.md`.** Section 12 is the required format
   (one YAML block per bot) and section 13 is the end-to-end flow. Only start this
   when Sabastian says go.

Also: Chief of Staff's routines push `docs/data/**` to main hourly on weekdays
(9:45 AM-3:45 PM ET), so `git pull` before starting work and again before any PR.

## Decisions so far

- **Data:** Alpaca daily bars, **2016 to present** (Alpaca has nothing earlier).
  Request split/dividend-adjusted bars. Cache once; bots never hit the API per test.
  The current code uses yfinance; adding an Alpaca bars provider is part of the work.
- **Universe for testing:** a broad set of a few hundred liquid US stocks across
  sectors and market caps, plus the theme names in `config/universe.yaml`.
  Broad = robustness check; theme names = "does it help on what we trade".
- **Time windows:**
  - Discovery 2016-2021: bots experiment freely.
  - Validation 2022-2024: survivors run once, no retuning after.
  - Holdout 2025-present: locked, run once at the very end.
- **Holdout and validation data stay off Grok's shared machine.** All bots share
  one filesystem, so anything stored there is visible to every bot. Validation
  and holdout runs happen outside Grok (Claude Code on Sabastian's machine, or a
  GitHub Action). Exact mechanism is an open question.
- **Fills:** next-day open, never the same bar the signal was computed on.
  Slippage charged, higher for small caps.
- **Benchmarks:** every strategy must beat equal-weight buy-and-hold of its
  universe (and SPY) on a risk-adjusted basis, after costs.
- **Survivorship bias:** Alpaca and yfinance lack delisted stocks. A strategy that
  survives the full gauntlet gets re-tested on survivorship-free data (Norgate or
  Sharadar, a one-month subscription) before anyone trusts it.
- **Order of work:** harness upgrade first. The agent scorecard (Phase 0) and the
  bot-team spec come after.

## What already exists

`quant_system/backtest/harness.py` replays the real agents per theme sleeve with
simulated fills. Last run: Jun 23 - Sep 25, 2026 (67 trading days), output in
`artifacts/backtest/theme_leaderboard_latest.json`. Gaps:

- Only ~3 months of history.
- Default `fill_on="close"` (same bar as the signal). `next_open` exists; make it
  the default.
- No benchmark comparison, so sleeve returns can't be judged.
- No train/validation/holdout split, no trial log, no robustness tests.
- Backtest lowers confidence floors (`risk_overrides`); keep that visible in output.

## Harness upgrade (next task)

1. Alpaca daily-bar provider + cache for 2016-present, adjusted.
2. `next_open` as default fill; slippage by liquidity bucket.
3. Benchmarks in every result: SPY and equal-weight universe/theme buy-and-hold.
4. Window config (discovery / validation / holdout) enforced in code; holdout
   refuses to run unless explicitly unlocked.
5. **Trial log:** every strategy run is recorded (spec, window, metrics). The total
   count is used to adjust Sharpe for multiple testing (deflated Sharpe).
6. **Strategy spec runner:** bots submit a short spec (indicators, settings,
   entry/exit rules, sizing), not free-form code. Max ~4 tunable settings.
   New indicators are pure functions with tests that prove no lookahead.
7. Metrics: CAGR, Sharpe, max drawdown, turnover, trade count, vs-benchmark.
8. Keep everything paper-only; `pytest` must pass.

## The gauntlet (what the harness must support)

1. Screen: beats benchmark after costs in discovery.
2. Robustness: settings nudged 10-20% still work; works across sectors, cap
   buckets, and the 2018, 2020 and 2022 selloffs. One sharp peak = fail.
3. Validation: single run on 2022-2024.
4. Holdout: single run on 2025-present, deflated Sharpe.
5. Theme check: adds value on quantum/space/robotics names.
6. Paper trading as a new agent in the ledger for 4-8 weeks, judged on the same
   scorecard as existing agents.

A critic bot attacks survivors (lookahead bugs, one-stock dependence, crowding).
Expect over 95% of strategies to die. That is the system working.

## Later

- **Phase 0 agent scorecard:** do existing agents' high-confidence calls predict
  5- and 20-day forward returns? Repo history (`docs/data/history/`) starts
  Sep 6, 2026, so this is preliminary; the full tracker DB lives on Grok's machine.
- **Bot-team spec:** `docs/bot-team-spec.md`, in the format from section 12 of
  `docs/grok-bot-guide-for-claude.md`. Sabastian said to hold off on this for now.

## Guardrails

- Paper trading only. Never commit `.env` or secrets.
- Risk-policy changes need Sabastian's explicit approval.
- Code changes go through PRs per sections 9-10 of the Grok Bot guide.
- Mark anything uncertain as an open question instead of guessing.
