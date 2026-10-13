"""Chart-pattern detectors. Each is causal: day t uses bars up to and including t.

``cup_handle`` flags the breakout bar of a cup-and-handle (William O'Neil style):
a prior uptrend into a left rim, a rounded cup, a right rim near the left rim, a
short shallow handle in the upper half of the cup, then a close above the handle
high on higher volume. Swing highs are pivots confirmed only after ``pivot_bars``
later bars have passed, so no pattern ever uses a future bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_system.lab.indicators.registry import indicator


def _confirmed_swing_highs(high: np.ndarray, low: np.ndarray, width: int, prominence: float) -> np.ndarray:
    """Bar indices i that are the highest high of [i-width, i+width] and rise
    ``prominence`` above that window's lowest low. Pivot i is known only at bar i+width."""
    n = len(high)
    out = []
    for i in range(width, n - width):
        win_h = high[i - width : i + width + 1]
        if high[i] < win_h.max() or np.argmax(win_h) != width:  # first max wins ties
            continue
        if high[i] / low[i - width : i + width + 1].min() - 1.0 >= prominence:
            out.append(i)
    return np.asarray(out, dtype=int)


_CUP_PARAMS: dict[str, tuple[int | float, int | float, int | float]] = dict(
    pivot_bars=(5, 2, 20),
    pivot_prominence=(0.03, 0.0, 0.5),
    uptrend_bars=(120, 10, 400),
    min_uptrend=(0.30, 0.0, 3.0),
    min_cup_depth=(0.12, 0.01, 0.9),
    max_cup_depth=(0.33, 0.02, 0.95),
    min_cup_bars=(35, 10, 400),
    max_cup_bars=(325, 20, 600),
    bottom_band=(0.25, 0.05, 0.9),
    min_bottom_share=(0.35, 0.0, 1.0),
    rim_tolerance=(0.07, 0.0, 0.5),
    min_handle_bars=(5, 1, 60),
    max_handle_bars=(20, 2, 120),
    max_handle_depth=(0.12, 0.01, 0.5),
    max_handle_cup_frac=(0.333, 0.05, 1.0),
    handle_min_position=(0.5, 0.0, 1.0),
    require_falling_volume=(0, 0, 1),
    volume_bars=(50, 5, 400),
    breakout_volume_mult=(1.4, 1.0, 10.0),
)


@indicator("cup_handle", **_CUP_PARAMS)
def cup_handle(
    bars: pd.DataFrame,
    pivot_bars: int,
    pivot_prominence: float,
    uptrend_bars: int,
    min_uptrend: float,
    min_cup_depth: float,
    max_cup_depth: float,
    min_cup_bars: int,
    max_cup_bars: int,
    bottom_band: float,
    min_bottom_share: float,
    rim_tolerance: float,
    min_handle_bars: int,
    max_handle_bars: int,
    max_handle_depth: float,
    max_handle_cup_frac: float,
    handle_min_position: float,
    require_falling_volume: int,
    volume_bars: int,
    breakout_volume_mult: float,
) -> pd.Series:
    """Cup-and-handle breakout: 1 on the bar that closes above the handle high on higher volume, else 0.

    Rules (each a setting): left/right rims are confirmed swing highs; the left rim
    sits at least ``min_uptrend`` above the lowest low of the prior ``uptrend_bars``;
    cup depth (left rim - cup low) / left rim in [min_cup_depth, max_cup_depth];
    rim-to-rim length in [min_cup_bars, max_cup_bars]; the cup low lies in the middle
    third and at least ``min_bottom_share`` of cup bars have lows within
    ``bottom_band`` x depth of the low (rejects V shapes); rims within
    ``rim_tolerance``; the handle (bars after the right rim, before the breakout)
    lasts [min_handle_bars, max_handle_bars], its depth from the handle high is at
    most ``max_handle_depth`` and at most ``max_handle_cup_frac`` of the cup's price
    depth, and its low stays above ``handle_min_position`` of the cup (0.5 = upper
    half); with ``require_falling_volume`` = 1 the handle's average volume must be
    below the cup's. Breakout: close above the handle high with volume at least
    ``breakout_volume_mult`` x the average of the prior ``volume_bars`` bars.
    """
    high = bars["High"].to_numpy(dtype=float)
    low = bars["Low"].to_numpy(dtype=float)
    close = bars["Close"].to_numpy(dtype=float)
    vol = bars["Volume"].to_numpy(dtype=float)
    n = len(bars)
    out = np.zeros(n)
    pivots = _confirmed_swing_highs(high, low, pivot_bars, pivot_prominence)
    if len(pivots) < 2:
        return pd.Series(out, index=bars.index)
    vol_avg = pd.Series(vol, index=bars.index).rolling(volume_bars).mean().shift(1).to_numpy()
    cvol = np.concatenate([[0.0], np.nancumsum(vol)])

    def mean_vol(a: int, b: int) -> float:  # bars a..b-1
        return (cvol[b] - cvol[a]) / max(b - a, 1)

    for t in range(n):
        if not np.isfinite(vol_avg[t]) or vol[t] < breakout_volume_mult * vol_avg[t]:
            continue
        # Right-rim candidates: handle = bars r+1 .. t-1, rim confirmed by bar t.
        lo_r, hi_r = t - 1 - max_handle_bars, t - 1 - min_handle_bars
        rights = pivots[(pivots >= lo_r) & (pivots <= hi_r) & (pivots + pivot_bars <= t)]
        hit = False
        for r in rights[::-1]:
            handle_high = high[r:t].max()
            if not close[t] > handle_high:
                continue
            handle_low = low[r + 1 : t].min()
            if (handle_high - handle_low) / handle_high > max_handle_depth:
                continue
            lefts = pivots[(pivots <= r - min_cup_bars) & (pivots >= r - max_cup_bars)]
            for left in lefts[::-1]:
                rim = high[left]
                if abs(high[r] / rim - 1.0) > rim_tolerance:
                    continue
                start = max(0, left - uptrend_bars)
                if rim / low[start : left + 1].min() - 1.0 < min_uptrend:
                    continue
                cup = low[left + 1 : r]
                k = int(np.argmin(cup))
                cup_low = cup[k]
                depth = (rim - cup_low) / rim
                if not min_cup_depth <= depth <= max_cup_depth:
                    continue
                span = r - left
                pos = (k + 1) / span
                if not 1.0 / 3.0 <= pos <= 2.0 / 3.0:
                    continue
                drop = rim - cup_low
                if np.mean(cup <= cup_low + bottom_band * drop) < min_bottom_share:
                    continue
                if handle_high - handle_low > max_handle_cup_frac * drop:
                    continue
                if handle_low < cup_low + handle_min_position * drop:
                    continue
                if require_falling_volume and not mean_vol(r + 1, t) < mean_vol(left + 1, r):
                    continue
                hit = True
                break
            if hit:
                break
        if hit:
            out[t] = 1.0
    return pd.Series(out, index=bars.index)


@indicator("cup_handle_stage", **_CUP_PARAMS)
def cup_handle_stage(bars: pd.DataFrame, **p: float) -> pd.Series:
    """Cup-and-handle progress, 0-5: the most advanced live candidate's stage on each bar.

    0 none; 1 prior uptrend into a confirmed left-rim pivot; 2 cup drawn down
    [min_cup_depth, max_cup_depth] with a rounded (not V) bottom so far; 3 price back
    within rim_tolerance of the left rim with a valid cup (right rim not yet a
    confirmed pivot); 4 handle in progress after a confirmed right-rim pivot, within
    the handle depth / cup-fraction / upper-cup / length (and optional volume) limits;
    5 the breakout bar, exactly where ``cup_handle`` fires. Same settings as cup_handle.
    A candidate dies when its drawdown exceeds max_cup_depth, price runs above the
    left rim by more than rim_tolerance, it outlives max_cup_bars + max_handle_bars,
    or a breakout has already fired after its left rim.
    """
    pb = int(p["pivot_bars"])
    high = bars["High"].to_numpy(dtype=float)
    low = bars["Low"].to_numpy(dtype=float)
    close = bars["Close"].to_numpy(dtype=float)
    vol = np.nan_to_num(bars["Volume"].to_numpy(dtype=float))
    n = len(bars)
    out = np.zeros(n)
    pivots = _confirmed_swing_highs(high, low, pb, p["pivot_prominence"])
    cvol = np.concatenate([[0.0], np.cumsum(vol)])
    min_cup, max_cup = int(p["min_cup_bars"]), int(p["max_cup_bars"])
    max_h = int(p["max_handle_bars"])
    tol = p["rim_tolerance"]
    fired = cup_handle(bars, **p).to_numpy()
    fired_before = np.concatenate([[0.0], np.cumsum(fired)])  # fires on bars 0 .. i-1

    def mean_vol(a: int, b: int) -> float:  # bars a..b-1
        return (cvol[b] - cvol[a]) / max(b - a, 1)

    def cup_ok(left: int, r: int) -> tuple[bool, float]:
        """Cup checks on bars left+1 .. r-1 with right rim r; returns (ok, cup low)."""
        span = r - left
        if not min_cup <= span <= max_cup:
            return False, np.nan
        cup = low[left + 1 : r]
        k = int(np.argmin(cup))
        cup_low, rim = cup[k], high[left]
        depth = (rim - cup_low) / rim
        if not p["min_cup_depth"] <= depth <= p["max_cup_depth"]:
            return False, cup_low
        if not 1.0 / 3.0 <= (k + 1) / span <= 2.0 / 3.0:
            return False, cup_low
        if np.mean(cup <= cup_low + p["bottom_band"] * (rim - cup_low)) < p["min_bottom_share"]:
            return False, cup_low
        return abs(high[r] / rim - 1.0) <= tol, cup_low

    for t in range(n):
        best = 0
        lefts = pivots[(pivots + pb <= t) & (pivots >= t - max_cup - max_h)]
        for left in lefts[::-1]:
            rim = high[left]
            if fired_before[t] - fired_before[left + 1] > 0:
                continue  # this rim already produced a breakout: pattern complete
            start = max(0, left - int(p["uptrend_bars"]))
            if rim / low[start : left + 1].min() - 1.0 < p["min_uptrend"]:
                continue
            seg = low[left + 1 : t + 1]
            k = int(np.argmin(seg))
            cup_low = seg[k]
            depth = (rim - cup_low) / rim
            if depth > p["max_cup_depth"] or high[left + 1 : t + 1].max() > rim * (1.0 + tol):
                continue
            stage = 1
            drop = rim - cup_low
            rounded = np.mean(seg <= cup_low + p["bottom_band"] * drop) >= p["min_bottom_share"]
            if depth >= p["min_cup_depth"] and rounded and t - left <= max_cup:
                stage = 2
            j = left + 1 + k  # cup-low bar
            if stage == 2 and t > j:
                r_try = j + 1 + int(np.argmax(high[j + 1 : t + 1]))
                if r_try - left >= min_cup and cup_ok(left, r_try)[0]:
                    stage = 3
            # Handle in progress after a confirmed right-rim pivot.
            rights = pivots[(pivots > j) & (pivots + pb <= t) & (pivots >= t - max_h) & (pivots < t)]
            for r in rights[::-1]:
                ok, c_low = cup_ok(left, r)
                if not ok:
                    continue
                hh = high[r : t + 1].max()
                hl = low[r + 1 : t + 1].min()
                d = rim - c_low
                if (
                    close[r + 1 : t + 1].max() <= high[r:t].max()  # no breakout yet
                    and (hh - hl) / hh <= p["max_handle_depth"]
                    and hh - hl <= p["max_handle_cup_frac"] * d
                    and hl >= c_low + p["handle_min_position"] * d
                    and (not p["require_falling_volume"] or mean_vol(r + 1, t + 1) < mean_vol(left + 1, r))
                ):
                    stage = 4
                    break
            best = max(best, stage)
            if best == 4:
                break
        out[t] = best
    out[fired == 1.0] = 5.0
    return pd.Series(out, index=bars.index)
