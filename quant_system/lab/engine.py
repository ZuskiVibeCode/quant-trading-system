"""Spec engine: run a StrategySpec over one window with the lab fill and cost model.

Each session t of the window:

1. Open: fill yesterday's orders at today's open, sells first, then buys in rank
   order. Slippage is the name's bucket as of the signal day. A name with no
   bar today doesn't fill (an exit is re-signalled tonight if still true).
2. Close: mark positions (a name with no bar today keeps its last close).
3. Signals at the close, for tomorrow's open (none on the window's last day):
   - exit: held names whose exit rule is true;
   - entry: names not held, entry rule true, with a bar today and a liquidity
     bucket (not tradable = never entered), best ``rank`` first, while slots
     remain under ``max_positions``.

Sizing: each entry buys ``min(1 / max_positions, max_weight)`` of equity at
the signal close, capped by cash. Long only. Like the live PortfolioManager, the
20% cap applies when buying; a position that grows past it is not trimmed.
Exits pay slippage too; a held name that has fallen below the lowest liquidity
bucket exits at the costliest bucket's rate. Positions still open at the end
are marked at the last close with no exit cost, the same as the benchmarks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from quant_system.lab.costs import median_dollar_volume, slippage_bps
from quant_system.lab.data import bars_panel
from quant_system.lab.indicators import get_indicator
from quant_system.lab.rules import Evaluator
from quant_system.lab.settings import LabCostSettings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.windows import ResolvedWindow

_FIELDS = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}


@dataclass
class EngineResult:
    returns: pd.Series  # daily, window sessions
    weights: pd.DataFrame  # end-of-day weight per name
    traded: pd.Series  # daily |notional traded| / prior equity
    trades: pd.DataFrame  # one row per fill
    contributions: pd.Series  # P&L per name, as a fraction of starting equity
    symbols: list[str]
    missing: list[str]  # universe names with no bars
    first_trade_date: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def trade_count(self) -> int:
        return int(len(self.trades))


def _through(bars: dict[str, pd.DataFrame], end) -> dict[str, pd.DataFrame]:
    cut = pd.Timestamp(end)
    out = {s: df.loc[df.index <= cut] for s, df in bars.items()}
    return {s: df for s, df in out.items() if not df.empty}


def indicator_panels(
    spec: StrategySpec,
    bars: dict[str, pd.DataFrame],
    bench_close: pd.Series,
    calendar: pd.DatetimeIndex,
) -> dict[str, np.ndarray]:
    """Each spec indicator, computed per name on its own sessions, as sessions x names."""
    out: dict[str, np.ndarray] = {}
    frames = {
        s: df.assign(BenchClose=bench_close.reindex(df.index)) for s, df in bars.items()
    }
    for name in spec.indicators:
        ind = get_indicator(str(spec.indicators[name]["fn"]))
        args = spec.indicator_args(name)
        panel = pd.DataFrame({s: ind(f, **args) for s, f in frames.items()}).reindex(calendar)
        out[name] = panel.to_numpy(dtype=float)
    return out


def run_spec(
    spec: StrategySpec,
    bars: dict[str, pd.DataFrame],
    universe: list[str],
    window: ResolvedWindow,
    costs: LabCostSettings,
    *,
    bench: str = "SPY",
) -> EngineResult:
    wanted = [s.upper() for s in dict.fromkeys(universe)]
    clipped = _through(bars, window.end)
    if bench.upper() not in clipped:
        raise ValueError(f"benchmark {bench} bars missing; run `lab data sync`")
    held_bars = {s: clipped[s] for s in wanted if s in clipped}
    missing = [s for s in wanted if s not in clipped]
    if not held_bars:
        raise ValueError("no bars for any name in the universe")
    symbols = sorted(held_bars)
    calendar = bars_panel(clipped, "Close").index
    sessions = window.sessions(calendar)
    if sessions.empty:
        raise ValueError(f"no sessions in {window.name} {window.start}..{window.end}")
    pos = calendar.get_indexer(sessions)

    px = {k: bars_panel(held_bars, col).reindex(index=calendar, columns=symbols) for k, col in _FIELDS.items()}
    env: dict[str, Any] = {k: v.to_numpy(dtype=float) for k, v in px.items()}
    env.update(indicator_panels(spec, {s: held_bars[s] for s in symbols}, clipped[bench.upper()]["Close"], calendar))
    env.update(spec.param_values())

    mdv = median_dollar_volume(px["close"], px["volume"], costs.lookback_days)
    bps = slippage_bps(mdv, costs).to_numpy()
    worst_bps = max(b.bps for b in costs.buckets)
    has_bar = ~np.isnan(env["close"])
    mark = px["close"].ffill().to_numpy()
    open_ = env["open"]

    rules = spec.rules
    entry = Evaluator(rules["entry"], env).full() & has_bar & ~np.isnan(bps)  # broadcasts constants
    shape = env["close"].shape
    if "rank" in rules:
        rank = np.broadcast_to(Evaluator(rules["rank"], env).values(), shape)
    else:
        rank = mdv.to_numpy()  # default: most liquid first
    exit_eval = Evaluator(rules["exit"], env)
    exit_full = None if rules["exit"].uses_state else np.broadcast_to(exit_eval.full(), shape)

    n = len(symbols)
    shares = np.zeros(n)
    cost_basis = np.zeros(n)  # cash paid for the open position
    entered_at = np.full(n, -1)
    pnl = np.zeros(n)
    cash = 1.0
    prev_equity = 1.0
    target_w = spec.sizing.target_weight
    max_pos = spec.sizing.max_positions

    sells: list[int] = []
    buys: list[int] = []
    signal_row = -1
    equity_out = np.empty(len(pos))
    weights_out = np.zeros((len(pos), n))
    traded_out = np.zeros(len(pos))
    trades: list[dict[str, Any]] = []

    for k, i in enumerate(pos):
        day = calendar[i]
        traded = 0.0
        # 1. Open: fill orders from the previous close.
        for j in sells:
            px_open = open_[i, j]
            if np.isnan(px_open) or shares[j] <= 0:
                continue
            b = bps[signal_row, j]
            b = worst_bps if np.isnan(b) else b
            proceeds = shares[j] * px_open * (1.0 - b / 10_000.0)
            cash += proceeds
            pnl[j] += proceeds - cost_basis[j]
            traded += proceeds
            trades.append({"date": day, "symbol": symbols[j], "side": "sell", "shares": shares[j],
                           "price": px_open, "bps": b, "notional": proceeds})
            shares[j], cost_basis[j], entered_at[j] = 0.0, 0.0, -1
        for j in buys:
            px_open = open_[i, j]
            budget = min(target_w * prev_equity, cash)
            if np.isnan(px_open) or budget <= 1e-9:
                continue
            b = bps[signal_row, j]
            shares[j] = budget / (px_open * (1.0 + b / 10_000.0))
            cost_basis[j] = budget
            entered_at[j] = i
            cash -= budget
            traded += budget
            trades.append({"date": day, "symbol": symbols[j], "side": "buy", "shares": shares[j],
                           "price": px_open, "bps": b, "notional": budget})
        traded_out[k] = traded / prev_equity
        sells, buys = [], []

        # 2. Close: mark to market.
        values = shares * np.nan_to_num(mark[i])
        equity = cash + values.sum()
        equity_out[k] = equity
        weights_out[k] = values / equity if equity > 0 else 0.0
        prev_equity = equity
        if k == len(pos) - 1:
            break

        # 3. Signals for tomorrow's open.
        held = shares > 0
        if held.any():
            if exit_full is not None:
                ex = exit_full[i]
            else:
                bars_held = np.where(held, i - entered_at + 1, np.nan).astype(float)
                ex = exit_eval.at(i, {"bars_held": bars_held})
            sells = list(np.flatnonzero(held & ex))
        slots = max_pos - (int(held.sum()) - len(sells))
        if slots > 0:
            cand = np.flatnonzero(entry[i] & ~held)
            if len(cand):
                r = np.nan_to_num(rank[i, cand], nan=-np.inf)
                order = np.lexsort((cand, -r))  # best rank first, then symbol order
                buys = list(cand[order][:slots])
        signal_row = i

    for j in np.flatnonzero(shares > 0):
        pnl[j] += shares[j] * mark[pos[-1], j] - cost_basis[j]

    equity_s = pd.Series(equity_out, index=sessions)
    returns = equity_s / equity_s.shift(1).fillna(1.0) - 1.0
    trades_df = pd.DataFrame(trades, columns=["date", "symbol", "side", "shares", "price", "bps", "notional"])
    first = str(trades_df["date"].iloc[0].date()) if len(trades_df) else None
    notes = []
    if first is None:
        notes.append("no trades: entry rule never fired on a tradable name")
    return EngineResult(
        returns=returns,
        weights=pd.DataFrame(weights_out, index=sessions, columns=symbols),
        traded=pd.Series(traded_out, index=sessions),
        trades=trades_df,
        contributions=pd.Series(pnl, index=symbols).sort_values(ascending=False),
        symbols=symbols,
        missing=missing,
        first_trade_date=first,
        notes=notes,
    )
