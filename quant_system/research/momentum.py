"""MomentumAgent — MA 20/50/200, RSI, MACD, breakouts, ATR, RS, volume / demand / squeeze, VbP, Weinstein, aVWAP, Minervini template."""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent

# Lookbacks / thresholds for volume & volatility-coil overlays
_RVOL_WINDOW = 20
_RVOL_HIGH = 1.5
_RVOL_LOW = 0.7
_DEMAND_WINDOW = 20
_SQUEEZE_BB_PERIOD = 20
_SQUEEZE_BB_STD = 2.0
_SQUEEZE_KC_PERIOD = 20
_SQUEEZE_KC_ATR_MULT = 1.5

# Structure overlays: Volume-by-Price, Weinstein weekly stage, Anchored VWAP
_VBP_LOOKBACK = 80
_VBP_BINS = 24
_VBP_VALUE_AREA = 0.70
_WEINSTEIN_SMA_WEEKS = 30
_WEINSTEIN_SLOPE_LOOKBACK = 5
_WEINSTEIN_SLOPE_FLAT = 0.005  # ~0.5% SMA change over slope lookback = "flat"
_AVWAP_LOOKBACK = 90
_AVWAP_MIN_BARS_AFTER = 5


def _sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = _ema(close, 12) - _ema(close, 26)
    signal = _ema(macd_line, 9)
    hist = macd_line - signal
    return macd_line, signal, hist


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or math.isinf(f):  # NaN / inf
        return None
    return f


def relative_volume(df: pd.DataFrame, window: int = _RVOL_WINDOW) -> float | None:
    """Today's volume vs prior N-day average (excludes today from the average).

    Returns None if Volume is missing / empty / zero average.
    """
    if df is None or getattr(df, "empty", True) or "Volume" not in df.columns:
        return None
    vol = pd.to_numeric(df["Volume"], errors="coerce")
    if len(vol) < 2:
        return None
    today = _safe_float(vol.iloc[-1])
    if today is None or today < 0:
        return None
    # Prefer full window of prior bars; fall back to whatever prior history exists
    prior = vol.iloc[-(window + 1) : -1].dropna()
    if prior.empty:
        prior = vol.iloc[:-1].dropna()
    if prior.empty:
        return None
    avg = float(prior.mean())
    if avg <= 0:
        return None
    return today / avg


def demand_imbalance(df: pd.DataFrame, window: int = _DEMAND_WINDOW) -> dict[str, Any]:
    """Up-day vs down-day volume proxy over the last `window` bars.

    A day is "up" when Close > prior Close (first bar uses Close >= Open).
    imbalance = (up_vol - down_vol) / (up_vol + down_vol) ∈ [-1, 1].
    """
    out: dict[str, Any] = {
        "demand_imbalance": None,
        "up_volume": None,
        "down_volume": None,
        "demand_window": window,
    }
    if df is None or getattr(df, "empty", True):
        return out
    needed = {"Close", "Volume"}
    if not needed.issubset(df.columns):
        return out
    close = pd.to_numeric(df["Close"], errors="coerce")
    vol = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0).clip(lower=0.0)
    if len(close) < 2:
        return out

    prev = close.shift(1)
    up = close > prev
    # First bar: compare close vs open when available
    if "Open" in df.columns:
        open_ = pd.to_numeric(df["Open"], errors="coerce")
        up = up.where(prev.notna(), close >= open_)
    else:
        up = up.fillna(False)

    tail = slice(-window, None) if len(df) >= window else slice(None)
    up_t = up.iloc[tail]
    vol_t = vol.iloc[tail]
    # Drop bars where close is NaN
    valid = close.iloc[tail].notna()
    up_t = up_t[valid]
    vol_t = vol_t[valid]
    if up_t.empty:
        return out

    up_vol = float(vol_t[up_t.fillna(False)].sum())
    down_vol = float(vol_t[~up_t.fillna(False)].sum())
    total = up_vol + down_vol
    out["up_volume"] = up_vol
    out["down_volume"] = down_vol
    if total <= 0:
        return out
    out["demand_imbalance"] = (up_vol - down_vol) / total
    return out


def bollinger_keltner_squeeze(
    df: pd.DataFrame,
    *,
    bb_period: int = _SQUEEZE_BB_PERIOD,
    bb_std: float = _SQUEEZE_BB_STD,
    kc_period: int = _SQUEEZE_KC_PERIOD,
    kc_atr_mult: float = _SQUEEZE_KC_ATR_MULT,
) -> dict[str, Any]:
    """TTM-style squeeze: Bollinger Band inside Keltner Channel = volatility coil.

    Also flags squeeze_release (prior bar on, current off) and bb_width for expansion.
    """
    out: dict[str, Any] = {
        "squeeze_on": None,
        "squeeze_release": None,
        "bb_width": None,
        "kc_width": None,
        "bb_inside_kc": None,
    }
    if df is None or getattr(df, "empty", True):
        return out
    for col in ("High", "Low", "Close"):
        if col not in df.columns:
            return out

    close = pd.to_numeric(df["Close"], errors="coerce")
    n = len(close)
    min_bars = max(bb_period, kc_period) + 2
    if n < min_bars:
        return out

    mid_bb = _sma(close, bb_period)
    std = close.rolling(bb_period, min_periods=bb_period).std()
    bb_upper = mid_bb + bb_std * std
    bb_lower = mid_bb - bb_std * std

    atr = _atr(df, kc_period)
    mid_kc = _ema(close, kc_period)
    kc_upper = mid_kc + kc_atr_mult * atr
    kc_lower = mid_kc - kc_atr_mult * atr

    inside = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    # Need at least two valid squeeze flags
    if bool(pd.isna(inside.iloc[-1])):
        return out

    squeeze_on = bool(inside.iloc[-1])
    prior_on = bool(inside.iloc[-2]) if n >= 2 and not bool(pd.isna(inside.iloc[-2])) else False
    squeeze_release = prior_on and not squeeze_on

    bb_w = _safe_float(bb_upper.iloc[-1] - bb_lower.iloc[-1])
    kc_w = _safe_float(kc_upper.iloc[-1] - kc_lower.iloc[-1])
    last = _safe_float(close.iloc[-1])
    bb_width_pct = (bb_w / last) if bb_w is not None and last and last > 0 else None
    kc_width_pct = (kc_w / last) if kc_w is not None and last and last > 0 else None

    out.update(
        {
            "squeeze_on": squeeze_on,
            "squeeze_release": squeeze_release,
            "bb_width": bb_w,
            "kc_width": kc_w,
            "bb_width_pct": bb_width_pct,
            "kc_width_pct": kc_width_pct,
            "bb_inside_kc": squeeze_on,
        }
    )
    return out


def volume_by_price(
    df: pd.DataFrame,
    *,
    lookback: int = _VBP_LOOKBACK,
    n_bins: int = _VBP_BINS,
    value_area_pct: float = _VBP_VALUE_AREA,
) -> dict[str, Any]:
    """Visible-range Volume-by-Price: PoC and ~70% Value Area from recent daily bars.

    Bins typical price ((H+L+C)/3) over the last `lookback` sessions and accumulates
    volume per bin. PoC = midpoint of the highest-volume bin. Value Area expands
    symmetrically from PoC until `value_area_pct` of total volume is covered.
    """
    out: dict[str, Any] = {
        "vbp_poc": None,
        "vbp_vah": None,
        "vbp_val": None,
        "price_vs_poc": None,
        "vbp_lookback": lookback,
        "vbp_bins": n_bins,
    }
    if df is None or getattr(df, "empty", True):
        return out
    for col in ("High", "Low", "Close", "Volume"):
        if col not in df.columns:
            return out

    tail = df.iloc[-lookback:] if len(df) >= lookback else df
    if len(tail) < 5:
        return out

    high = pd.to_numeric(tail["High"], errors="coerce")
    low = pd.to_numeric(tail["Low"], errors="coerce")
    close = pd.to_numeric(tail["Close"], errors="coerce")
    vol = pd.to_numeric(tail["Volume"], errors="coerce").fillna(0.0).clip(lower=0.0)
    typical = (high + low + close) / 3.0
    valid = typical.notna() & vol.notna()
    typical = typical[valid]
    vol = vol[valid]
    if len(typical) < 5 or float(vol.sum()) <= 0:
        return out

    p_min = float(typical.min())
    p_max = float(typical.max())
    if not (p_max > p_min):
        # Degenerate range — single price level
        poc = p_min
        last = _safe_float(close.iloc[-1])
        out.update(
            {
                "vbp_poc": poc,
                "vbp_vah": poc,
                "vbp_val": poc,
                "price_vs_poc": ((last - poc) / poc) if last and poc else None,
            }
        )
        return out

    edges = np.linspace(p_min, p_max, n_bins + 1)
    # bin index 0..n_bins-1; clip right edge into last bin
    idxs = np.clip(np.digitize(typical.to_numpy(), edges) - 1, 0, n_bins - 1)
    vol_bins = np.zeros(n_bins, dtype=float)
    for i, v in zip(idxs, vol.to_numpy()):
        vol_bins[int(i)] += float(v)
    total = float(vol_bins.sum())
    if total <= 0:
        return out

    poc_i = int(np.argmax(vol_bins))
    mids = (edges[:-1] + edges[1:]) / 2.0
    poc = float(mids[poc_i])

    # Expand value area from PoC until ~70% volume
    lo_i = hi_i = poc_i
    covered = float(vol_bins[poc_i])
    target = value_area_pct * total
    while covered < target and (lo_i > 0 or hi_i < n_bins - 1):
        left = vol_bins[lo_i - 1] if lo_i > 0 else -1.0
        right = vol_bins[hi_i + 1] if hi_i < n_bins - 1 else -1.0
        if right >= left and hi_i < n_bins - 1:
            hi_i += 1
            covered += float(vol_bins[hi_i])
        elif lo_i > 0:
            lo_i -= 1
            covered += float(vol_bins[lo_i])
        else:
            break

    vah = float(edges[hi_i + 1])  # top of highest VA bin
    val = float(edges[lo_i])  # bottom of lowest VA bin
    last = _safe_float(close.iloc[-1])
    price_vs_poc = ((last - poc) / poc) if last is not None and poc else None

    out.update(
        {
            "vbp_poc": poc,
            "vbp_vah": vah,
            "vbp_val": val,
            "price_vs_poc": price_vs_poc,
        }
    )
    return out


def _resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Daily OHLCV → weekly (W-FRI) bars. Requires a DatetimeIndex."""
    if not isinstance(df.index, pd.DatetimeIndex):
        # Best-effort: try to parse index
        idx = pd.to_datetime(df.index, errors="coerce")
        if idx.isna().all():
            return pd.DataFrame()
        df = df.copy()
        df.index = idx
    df = df.sort_index()
    # Drop tz for clean weekly buckets
    if getattr(df.index, "tz", None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    agg = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    cols = [c for c in agg if c in df.columns]
    if "Close" not in cols:
        return pd.DataFrame()
    weekly = df[cols].resample("W-FRI").agg({c: agg[c] for c in cols}).dropna(subset=["Close"])
    return weekly


def weinstein_stage(
    df: pd.DataFrame,
    *,
    sma_weeks: int = _WEINSTEIN_SMA_WEEKS,
    slope_lookback: int = _WEINSTEIN_SLOPE_LOOKBACK,
    slope_flat: float = _WEINSTEIN_SLOPE_FLAT,
) -> dict[str, Any]:
    """Stan Weinstein-style stage from weekly close vs 30-week SMA.

    Deterministic rule set (documented for paper learning — soft bias only):
      Stage 2: weekly close ABOVE rising 30w SMA (SMA slope > +flat threshold).
      Stage 4: weekly close BELOW declining 30w SMA (SMA slope < -flat threshold).
      Stage 3: after advance — close at/above SMA but SMA flattening/falling,
               or oscillating near a flat SMA that previously advanced (~20w).
      Stage 1: basing — close at/below SMA with flat/rising SMA after decline,
               or sideways near a flat SMA that previously declined (~20w).

    Slope = (SMA_now - SMA_{n weeks ago}) / SMA_{n weeks ago}, n=`slope_lookback`.
    Flat band = ±`slope_flat` (default 0.5%). Young names with < sma_weeks weekly
    bars return stage=None but may still expose partial sma30w when computable.
    """
    out: dict[str, Any] = {
        "weinstein_stage": None,
        "sma30w": None,
        "sma30w_slope": None,
        "weekly_bars": 0,
        "weekly_close": None,
    }
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return out

    weekly = _resample_weekly(df)
    out["weekly_bars"] = int(len(weekly))
    if weekly.empty:
        return out

    wclose = pd.to_numeric(weekly["Close"], errors="coerce")
    last_w = _safe_float(wclose.iloc[-1])
    out["weekly_close"] = last_w

    if len(wclose) < sma_weeks:
        # Not enough history for a full 30w SMA — leave stage unset
        if len(wclose) >= 5:
            partial = _sma(wclose, min(sma_weeks, len(wclose)))
            out["sma30w"] = _safe_float(partial.iloc[-1])
        return out

    sma = _sma(wclose, sma_weeks)
    sma_now = _safe_float(sma.iloc[-1])
    out["sma30w"] = sma_now
    if sma_now is None or last_w is None or sma_now <= 0:
        return out

    # Slope over lookback weeks
    lb = min(slope_lookback, len(sma) - 1)
    sma_prev = _safe_float(sma.iloc[-1 - lb]) if lb >= 1 else None
    if sma_prev is None or sma_prev <= 0:
        slope = 0.0
    else:
        slope = (sma_now - sma_prev) / sma_prev
    out["sma30w_slope"] = slope

    above = last_w > sma_now
    below = last_w < sma_now
    rising = slope > slope_flat
    declining = slope < -slope_flat
    flat = not rising and not declining

    # Longer context: did the 30w SMA advance or decline over ~20 weeks?
    # Distinguishes Stage 1 (basing after decline) from Stage 3 (topping after advance)
    # when price is near a flattening SMA.
    ctx_lb = min(20, len(sma) - 1)
    sma_ctx = _safe_float(sma.iloc[-1 - ctx_lb]) if ctx_lb >= 5 else None
    prior_decline = bool(
        sma_ctx is not None and sma_ctx > 0 and (sma_now - sma_ctx) / sma_ctx < -slope_flat
    )
    prior_advance = bool(
        sma_ctx is not None and sma_ctx > 0 and (sma_now - sma_ctx) / sma_ctx > slope_flat
    )
    near_sma = abs(last_w - sma_now) / sma_now <= 0.03

    # Primary classification
    if above and rising:
        stage = 2
    elif below and declining:
        stage = 4
    elif near_sma and flat:
        # Sideways at flat SMA: Stage 1 after decline, Stage 3 after advance
        if prior_decline and not prior_advance:
            stage = 1
        elif prior_advance and not prior_decline:
            stage = 3
        else:
            stage = 1 if below or not above else 3
    elif above and (flat or declining):
        # Still elevated vs SMA after advance / rolling over — Stage 3
        stage = 3 if (prior_advance or declining or flat) else 2
    elif below and (flat or rising):
        # Holding below / early reclaim — Stage 1
        stage = 1
    else:
        # Exactly on SMA
        if rising:
            stage = 2
        elif declining:
            stage = 4
        else:
            stage = 1 if prior_decline else 3
    out["weinstein_stage"] = stage
    return out


def anchored_vwap(
    df: pd.DataFrame,
    *,
    lookback: int = _AVWAP_LOOKBACK,
    min_bars_after: int = _AVWAP_MIN_BARS_AFTER,
) -> dict[str, Any]:
    """Anchored VWAP from a deterministic price-volume pivot (no earnings API).

    Anchor rule (documented):
      Within the last `lookback` sessions (default 90), choose the day with the
      highest volume that still leaves at least `min_bars_after` bars afterward
      so VWAP has room to form. That high-volume bar is treated as the practical
      catalyst / breakout-climax pivot for this universe (earnings dates are
      unreliable via free yfinance without extra APIs, so we do not use them).

    VWAP uses typical price (H+L+C)/3 * volume from the anchor bar through today.
    """
    out: dict[str, Any] = {
        "avwap": None,
        "anchor_date": None,
        "anchor_reason": None,
        "price_vs_avwap": None,
        "anchor_price": None,
    }
    if df is None or getattr(df, "empty", True):
        return out
    for col in ("High", "Low", "Close", "Volume"):
        if col not in df.columns:
            return out
    if len(df) < min_bars_after + 2:
        return out

    # Search window: exclude the final min_bars_after bars from being the anchor
    search_end = len(df) - min_bars_after
    search_start = max(0, search_end - lookback)
    if search_end <= search_start:
        return out

    window = df.iloc[search_start:search_end]
    vol = pd.to_numeric(window["Volume"], errors="coerce").fillna(0.0)
    if vol.empty or float(vol.max()) <= 0:
        return out

    # Index of max volume within window (last occurrence if ties — more recent)
    rel_i = int(vol.to_numpy()[::-1].argmax())
    anchor_i_in_window = len(vol) - 1 - rel_i
    anchor_pos = search_start + anchor_i_in_window

    seg = df.iloc[anchor_pos:]
    high = pd.to_numeric(seg["High"], errors="coerce")
    low = pd.to_numeric(seg["Low"], errors="coerce")
    close = pd.to_numeric(seg["Close"], errors="coerce")
    svol = pd.to_numeric(seg["Volume"], errors="coerce").fillna(0.0).clip(lower=0.0)
    typical = (high + low + close) / 3.0
    valid = typical.notna() & svol.notna() & (svol > 0)
    if valid.sum() < 2:
        return out
    tp = typical[valid]
    vv = svol[valid]
    cum_pv = float((tp * vv).sum())
    cum_v = float(vv.sum())
    if cum_v <= 0:
        return out
    avwap = cum_pv / cum_v
    last = _safe_float(close.iloc[-1])
    anchor_ts = seg.index[0]
    try:
        anchor_date = pd.Timestamp(anchor_ts).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        anchor_date = str(anchor_ts)
    anchor_px = _safe_float(pd.to_numeric(seg["Close"], errors="coerce").iloc[0])

    out.update(
        {
            "avwap": float(avwap),
            "anchor_date": anchor_date,
            "anchor_reason": (
                f"highest-volume day in prior {lookback}d lookback "
                f"(excl. last {min_bars_after}d); volume climax / breakout pivot"
            ),
            "price_vs_avwap": ((last - avwap) / avwap) if last is not None and avwap else None,
            "anchor_price": anchor_px,
        }
    )
    return out


def _apply_structure_overlays(
    *,
    score: float,
    reasons: list[str],
    vbp: dict[str, Any],
    weinstein: dict[str, Any],
    avwap: dict[str, Any],
    uptrend_short: bool,
) -> float:
    """Soft score tweaks from VbP / Weinstein / aVWAP — magnitudes stay small."""

    last_rel = None
    poc = vbp.get("vbp_poc")
    vah = vbp.get("vbp_vah")
    val = vbp.get("vbp_val")
    pvp = vbp.get("price_vs_poc")
    # Reconstruct last vs levels using price_vs_poc when available
    if poc is not None and pvp is not None:
        last_est = poc * (1.0 + pvp)
        # Near VAL / PoC support in uptrend
        if uptrend_short and val is not None and poc is not None:
            near_val = abs(last_est - val) / val <= 0.015 or (
                val <= last_est <= poc * 1.01
            )
            near_poc = abs(pvp) <= 0.01
            if near_val or near_poc:
                score += 4
                where = "VAL/PoC" if near_val else "PoC"
                reasons.append(f"VbP: price near {where} support in uptrend")
            elif vah is not None and last_est > vah * 1.02 and pvp > 0.04:
                score -= 4
                reasons.append(
                    f"VbP: extended above VAH/PoC (price_vs_poc={pvp:+.1%}) — chase caution"
                )
            else:
                reasons.append(
                    f"VbP PoC={poc:.2f} VA=[{val:.2f},{vah:.2f}] "
                    f"(price_vs_poc={pvp:+.1%})"
                )
        elif pvp is not None and vah is not None and last_est > vah * 1.02 and pvp > 0.05:
            score -= 3
            reasons.append(f"VbP: stretched above VAH (price_vs_poc={pvp:+.1%})")
        elif poc is not None and val is not None and vah is not None:
            reasons.append(
                f"VbP PoC={poc:.2f} VA=[{val:.2f},{vah:.2f}]"
                + (f" (price_vs_poc={pvp:+.1%})" if pvp is not None else "")
            )
    elif poc is not None:
        reasons.append(f"VbP PoC={poc:.2f}")

    stage = weinstein.get("weinstein_stage")
    sma30w = weinstein.get("sma30w")
    if stage is not None:
        if stage == 2:
            score += 5
            reasons.append(
                f"Weinstein Stage 2 (markup; SMA30w={sma30w:.2f})"
                if sma30w
                else "Weinstein Stage 2 (markup)"
            )
        elif stage == 3:
            score -= 4
            reasons.append(
                f"Weinstein Stage 3 (distribution/topping; SMA30w={sma30w:.2f}) — "
                "caution on new aggressive longs"
                if sma30w
                else "Weinstein Stage 3 — caution on new aggressive longs"
            )
        elif stage == 4:
            score -= 6
            reasons.append(
                f"Weinstein Stage 4 (markdown; SMA30w={sma30w:.2f}) — "
                "discourage new longs"
                if sma30w
                else "Weinstein Stage 4 — discourage new longs"
            )
        elif stage == 1:
            score += 1
            reasons.append(
                f"Weinstein Stage 1 (basing; SMA30w={sma30w:.2f})"
                if sma30w
                else "Weinstein Stage 1 (basing)"
            )
    elif sma30w is not None:
        reasons.append(
            f"Weinstein SMA30w={sma30w:.2f} (insufficient weekly bars for stage)"
        )

    av = avwap.get("avwap")
    pva = avwap.get("price_vs_avwap")
    anchor_date = avwap.get("anchor_date")
    if av is not None and pva is not None:
        above = pva >= 0
        weak_tape = stage in (3, 4) or not uptrend_short
        if above and stage == 2:
            score += 4
            reasons.append(
                f"Above aVWAP ({av:.2f}, anchor {anchor_date}) in Stage 2"
            )
        elif above and uptrend_short:
            score += 2
            reasons.append(f"Above aVWAP ({av:.2f}, anchor {anchor_date})")
        elif (not above) and weak_tape:
            score -= 4
            reasons.append(
                f"Below aVWAP ({av:.2f}, anchor {anchor_date}) on weak tape"
            )
        elif not above:
            score -= 2
            reasons.append(f"Below aVWAP ({av:.2f}, anchor {anchor_date})")
        else:
            reasons.append(
                f"aVWAP={av:.2f} (anchor {anchor_date}, price_vs_avwap={pva:+.1%})"
            )

    return score


def _apply_volume_overlays(
    *,
    score: float,
    reasons: list[str],
    rvol: float | None,
    demand: dict[str, Any],
    squeeze: dict[str, Any],
    breakout_up: bool,
    breakout_dn: bool,
    macd_hist: float,
) -> float:
    """Adjust raw momentum score from RVOL / demand / squeeze; mutate reasons."""

    # --- Relative volume: confirm breakouts, fade empty moves ---
    if rvol is not None:
        if breakout_up:
            if rvol >= _RVOL_HIGH:
                score += 8
                reasons.append(f"Breakout confirmed by elevated RVOL ({rvol:.2f}x)")
            elif rvol < _RVOL_LOW:
                score -= 6
                reasons.append(f"Empty breakout — low RVOL ({rvol:.2f}x), fade")
            else:
                reasons.append(f"RVOL {rvol:.2f}x on breakout (neutral volume)")
        elif breakout_dn:
            if rvol >= _RVOL_HIGH:
                score -= 8
                reasons.append(f"Breakdown confirmed by elevated RVOL ({rvol:.2f}x)")
            elif rvol < _RVOL_LOW:
                score += 6
                reasons.append(f"Empty breakdown — low RVOL ({rvol:.2f}x), fade")
            else:
                reasons.append(f"RVOL {rvol:.2f}x on breakdown (neutral volume)")
        elif rvol >= _RVOL_HIGH:
            # Elevated volume without a range break: slight push with MACD bias
            bump = 4 if macd_hist > 0 else -4
            score += bump
            reasons.append(
                f"Elevated RVOL ({rvol:.2f}x) with {'bullish' if bump > 0 else 'bearish'} bias"
            )
        else:
            reasons.append(f"RVOL {rvol:.2f}x vs {_RVOL_WINDOW}d avg")

    # --- Demand imbalance: buyers vs sellers via up/down volume ---
    imb = demand.get("demand_imbalance")
    if imb is not None:
        if imb >= 0.25:
            score += 8
            reasons.append(f"Demand imbalance strong (+{imb:.2f}; buyers stepping in)")
        elif imb >= 0.10:
            score += 4
            reasons.append(f"Demand imbalance positive (+{imb:.2f})")
        elif imb <= -0.25:
            score -= 8
            reasons.append(f"Demand imbalance strong ({imb:.2f}; sellers in control)")
        elif imb <= -0.10:
            score -= 4
            reasons.append(f"Demand imbalance negative ({imb:.2f})")
        else:
            reasons.append(f"Demand imbalance balanced ({imb:+.2f})")

    # --- Squeeze: BB inside KC (coil); release expands with momentum ---
    if squeeze.get("squeeze_on") is True:
        # Coil: do not force direction; slight confidence dampen via score pull-to-zero
        if score > 5:
            score -= 3
        elif score < -5:
            score += 3
        reasons.append("BB inside KC — volatility squeeze (coil)")
    elif squeeze.get("squeeze_release") is True:
        if macd_hist > 0 or breakout_up:
            score += 10
            reasons.append("Squeeze release with bullish expansion")
        elif macd_hist < 0 or breakout_dn:
            score -= 10
            reasons.append("Squeeze release with bearish expansion")
        else:
            reasons.append("Squeeze release — watch for directional expansion")
    elif squeeze.get("squeeze_on") is False:
        # Not coiled; optional note only when width is available
        pass

    return score


# Mark Minervini trend template — soft features only (never a hard veto).
# Classic 8 rules are split into the 10 boolean checks below.
_MINERVINI_RS_MIN = 70.0
_MINERVINI_ABOVE_LOW = 0.30  # price >= 30% above the 52-week low
_MINERVINI_WITHIN_HIGH = 0.25  # price within 25% of the 52-week high
_MINERVINI_WEEK52 = 252
_MINERVINI_SMA200_LAG = 21  # ~1 month of trading sessions
_RS_SHORT_BARS = 126  # ~6 months
_RS_LONG_BARS = 252  # ~12 months
_RS_HISTORY_PERIOD = "2y"
# Pass-count vs a 5/10 midpoint: 10/10 → +5, 9/10 → +4, 0/10 → -5.
# Separate RS tilt: (rs_proxy - 50) * 0.04, clipped to ±2.
# A 9/10 with rs_proxy 80 is about +5.2 — helpful, smaller than one MA cross (±15).
_MINERVINI_PASS_WEIGHT = 1.0
_MINERVINI_PASS_CENTER = 5.0
_MINERVINI_RS_WEIGHT = 0.04
_MINERVINI_RS_SCORE_CAP = 2.0

# Transparent stand-in for IBD relative strength (not available from Yahoo bars).
RS_PROXY_DEFINITION = (
    "Not IBD RS. rs_proxy is the cross-sectional percentile, scaled 1-99, of "
    "6-12 month excess return versus the benchmark (SPY) across the research "
    "universe. Excess return is the equal-weight average of (stock return - "
    "benchmark return) over 126 and 252 trading days when both windows exist; "
    "otherwise the single available window (6-month requires 126 sessions). "
    "Ties use average ranks: rs_proxy = 1 + 98 * (avg_rank - 1) / (n - 1), "
    "where avg_rank 1 is the weakest excess return and 99 is the strongest. "
    "n < 2 leaves rs_proxy null and that check does not pass. Soft input only."
)

_MINERVINI_CHECK_KEYS: tuple[str, ...] = (
    "rs_proxy_gt_70",
    "price_gt_sma50",
    "price_gt_sma150",
    "price_gt_sma200",
    "sma50_gt_sma150",
    "sma50_gt_sma200",
    "sma150_gt_sma200",
    "price_30pct_above_52w_low",
    "price_within_25pct_52w_high",
    "sma200_rising",
)


def _naive_daily_close(df: pd.DataFrame) -> pd.Series | None:
    """Close series indexed by tz-naive normalized dates (as-of safe; no future rows)."""
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return None
    close = pd.to_numeric(df["Close"], errors="coerce")
    idx = pd.to_datetime(close.index, errors="coerce")
    close = close.copy()
    close.index = idx
    close = close[close.index.notna()]
    if close.empty:
        return None
    if getattr(close.index, "tz", None) is not None:
        close.index = close.index.tz_localize(None)
    close.index = close.index.normalize()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    return close.astype(float)


def blended_excess_return(
    stock_df: pd.DataFrame,
    bench_df: pd.DataFrame,
    *,
    short_bars: int = _RS_SHORT_BARS,
    long_bars: int = _RS_LONG_BARS,
) -> dict[str, Any]:
    """6-month and/or 12-month excess return vs benchmark using only rows in the frames.

    Endpoint returns (close_t / close_{t-n} - 1). A truncated as-of frame cannot
    see later sessions, so this is safe on the backtest path.
    """
    out: dict[str, Any] = {"rs_excess": None, "rs_windows": []}
    stock = _naive_daily_close(stock_df)
    bench = _naive_daily_close(bench_df)
    if stock is None or bench is None:
        return out
    aligned = pd.concat(
        [stock.rename("s"), bench.rename("b")], axis=1, join="inner"
    ).dropna()
    if aligned.empty:
        return out
    parts: list[float] = []
    windows: list[str] = []
    for bars, label in ((short_bars, "6m"), (long_bars, "12m")):
        # bars sessions back from the last aligned row → iloc[-(bars+1)]
        need = bars + 1
        if len(aligned) < need:
            continue
        s0 = float(aligned["s"].iloc[-need])
        b0 = float(aligned["b"].iloc[-need])
        s1 = float(aligned["s"].iloc[-1])
        b1 = float(aligned["b"].iloc[-1])
        if s0 <= 0 or b0 <= 0:
            continue
        parts.append((s1 / s0 - 1.0) - (b1 / b0 - 1.0))
        windows.append(label)
    if not parts:
        return out
    out["rs_excess"] = float(sum(parts) / len(parts))
    out["rs_windows"] = windows
    return out


def rs_proxy_from_excess(excess_by_ticker: dict[str, float]) -> dict[str, float]:
    """Map excess returns to rs_proxy in [1, 99]. Empty when fewer than 2 names."""
    clean: dict[str, float] = {}
    for ticker, value in excess_by_ticker.items():
        f = _safe_float(value)
        if f is None:
            continue
        clean[str(ticker).upper()] = f
    n = len(clean)
    if n < 2:
        return {}
    values = list(clean.values())
    out: dict[str, float] = {}
    denom = float(n - 1)
    for ticker, value in clean.items():
        below = sum(1 for other in values if other < value)
        equal = sum(1 for other in values if other == value)
        avg_rank = below + (equal + 1) / 2.0  # 1 = weakest
        out[ticker] = 1.0 + 98.0 * (avg_rank - 1.0) / denom
    return out


def minervini_trend_template(
    df: pd.DataFrame,
    *,
    rs_proxy: float | None = None,
    week52: int = _MINERVINI_WEEK52,
    sma200_lag: int = _MINERVINI_SMA200_LAG,
) -> dict[str, Any]:
    """Classic Minervini trend-template checks from a daily OHLCV frame.

    Uses only rows present in ``df`` (the last bar is "today"). Callers on the
    backtest path must pass an as-of-truncated frame; this function does not
    look past ``df.iloc[-1]``.
    """
    checks: dict[str, bool] = {k: False for k in _MINERVINI_CHECK_KEYS}
    rs_val = _safe_float(rs_proxy)
    result: dict[str, Any] = {
        "minervini_passes": 0,
        "minervini_checks": checks,
        "rs_proxy": rs_val,
        "rs_proxy_label": "rs_proxy",
        "rs_proxy_definition": RS_PROXY_DEFINITION,
        "pct_vs_52w_high": None,
        "pct_vs_52w_low": None,
        "sma50": None,
        "sma150": None,
        "sma200": None,
        "sma200_month_ago": None,
        "week52_high": None,
        "week52_low": None,
        "week52_bars": 0,
    }
    if df is None or getattr(df, "empty", True) or "Close" not in getattr(df, "columns", []):
        result["minervini_passes"] = 0
        return result

    close = pd.to_numeric(df["Close"], errors="coerce")
    price = _safe_float(close.iloc[-1]) if len(close) else None
    if price is None or price <= 0:
        return result

    sma50 = _safe_float(_sma(close, 50).iloc[-1]) if len(close) >= 50 else None
    sma150 = _safe_float(_sma(close, 150).iloc[-1]) if len(close) >= 150 else None
    sma200_series = _sma(close, 200) if len(close) >= 200 else None
    sma200 = _safe_float(sma200_series.iloc[-1]) if sma200_series is not None else None
    sma200_prev = None
    if sma200_series is not None and len(sma200_series) > sma200_lag:
        sma200_prev = _safe_float(sma200_series.iloc[-1 - sma200_lag])

    high_src = (
        pd.to_numeric(df["High"], errors="coerce") if "High" in df.columns else close
    )
    low_src = pd.to_numeric(df["Low"], errors="coerce") if "Low" in df.columns else close
    window_n = int(min(week52, len(close)))
    week_high = _safe_float(high_src.iloc[-window_n:].max()) if window_n else None
    week_low = _safe_float(low_src.iloc[-window_n:].min()) if window_n else None
    pct_high = (
        (price / week_high - 1.0) if week_high is not None and week_high > 0 else None
    )
    pct_low = (
        (price / week_low - 1.0) if week_low is not None and week_low > 0 else None
    )

    checks["rs_proxy_gt_70"] = bool(rs_val is not None and rs_val > _MINERVINI_RS_MIN)
    checks["price_gt_sma50"] = bool(sma50 is not None and price > sma50)
    checks["price_gt_sma150"] = bool(sma150 is not None and price > sma150)
    checks["price_gt_sma200"] = bool(sma200 is not None and price > sma200)
    checks["sma50_gt_sma150"] = bool(sma50 is not None and sma150 is not None and sma50 > sma150)
    checks["sma50_gt_sma200"] = bool(sma50 is not None and sma200 is not None and sma50 > sma200)
    checks["sma150_gt_sma200"] = bool(
        sma150 is not None and sma200 is not None and sma150 > sma200
    )
    checks["price_30pct_above_52w_low"] = bool(
        pct_low is not None and pct_low >= _MINERVINI_ABOVE_LOW
    )
    checks["price_within_25pct_52w_high"] = bool(
        pct_high is not None and pct_high >= -_MINERVINI_WITHIN_HIGH
    )
    checks["sma200_rising"] = bool(
        sma200 is not None and sma200_prev is not None and sma200 > sma200_prev
    )

    result.update(
        {
            "minervini_passes": int(sum(1 for v in checks.values() if v)),
            "minervini_checks": checks,
            "rs_proxy": rs_val,
            "pct_vs_52w_high": pct_high,
            "pct_vs_52w_low": pct_low,
            "sma50": sma50,
            "sma150": sma150,
            "sma200": sma200,
            "sma200_month_ago": sma200_prev,
            "week52_high": week_high,
            "week52_low": week_low,
            "week52_bars": window_n,
        }
    )
    return result


def _apply_minervini_overlay(
    *,
    score: float,
    reasons: list[str],
    template: dict[str, Any],
) -> tuple[float, float]:
    """Modest pass-count + RS tilt. Never blocks a trade by itself."""
    passes = int(template.get("minervini_passes") or 0)
    delta = (passes - _MINERVINI_PASS_CENTER) * _MINERVINI_PASS_WEIGHT
    rs = _safe_float(template.get("rs_proxy"))
    if rs is not None:
        rs_tilt = (rs - 50.0) * _MINERVINI_RS_WEIGHT
        delta += float(np.clip(rs_tilt, -_MINERVINI_RS_SCORE_CAP, _MINERVINI_RS_SCORE_CAP))
    score += delta
    if rs is None:
        rs_bit = "rs_proxy unavailable"
    else:
        rs_bit = f"rs_proxy={rs:.0f}"
    reasons.append(
        f"Minervini template {passes}/10 (soft, not a veto; {rs_bit}; score {delta:+.1f})"
    )
    return score, float(delta)


class MomentumAgent(ResearchAgent):
    agent_id = "momentum"

    def __init__(
        self,
        market_data: MarketData,
        universe: Iterable[str] | None = None,
    ):
        super().__init__(market_data)
        if universe is None:
            self._rs_universe: tuple[str, ...] | None = None
        else:
            seen: list[str] = []
            for raw in universe:
                ticker = str(raw).upper().strip()
                if ticker and ticker not in seen:
                    seen.append(ticker)
            self._rs_universe = tuple(seen)
        # Cache key includes the benchmark as-of date so replay days do not reuse ranks.
        self._rs_cache: dict[tuple, dict[str, Any]] = {}

    def _universe_for_rs(self, ticker: str, benchmark: str) -> list[str]:
        bench = benchmark.upper().strip()
        names: list[str] = []
        if self._rs_universe:
            names.extend(self._rs_universe)
        elif hasattr(self.market_data, "tickers"):
            try:
                names.extend(self.market_data.tickers())
            except Exception:  # noqa: BLE001 — listing peers is optional
                names = []
        out: list[str] = []
        for raw in names:
            t = str(raw).upper().strip()
            if not t or t == bench or t in out:
                continue
            out.append(t)
        current = ticker.upper().strip()
        if current and current != bench and current not in out:
            out.append(current)
        return out

    def _history_quiet(self, ticker: str, period: str) -> pd.DataFrame | None:
        try:
            df = self.market_data.history(ticker, period=period, interval="1d")
        except Exception:  # noqa: BLE001 — missing peer bars
            return None
        if df is None or getattr(df, "empty", True):
            return None
        return df

    def _rs_snapshot(self, ticker: str, benchmark: str) -> dict[str, Any]:
        """Universe rs_proxy for ``ticker``. Cached per benchmark as-of date."""
        empty: dict[str, Any] = {
            "rs_proxy": None,
            "rs_excess": None,
            "rs_windows": [],
            "rs_universe_n": 0,
        }
        names = self._universe_for_rs(ticker, benchmark)
        bench_df = self._history_quiet(benchmark, _RS_HISTORY_PERIOD)
        if bench_df is None:
            # Fall back to the same 1y window the rest of the agent uses.
            bench_df = self._history_quiet(benchmark, "1y")
        if bench_df is None:
            return empty
        bench_close = _naive_daily_close(bench_df)
        asof = None
        if bench_close is not None and len(bench_close):
            asof = str(pd.Timestamp(bench_close.index[-1]).date())
        cache_key = (benchmark.upper(), asof, tuple(names))
        cached = self._rs_cache.get(cache_key)
        if cached is None:
            excess: dict[str, float] = {}
            windows: dict[str, list[str]] = {}
            for name in names:
                frame = self._history_quiet(name, _RS_HISTORY_PERIOD)
                if frame is None:
                    frame = self._history_quiet(name, "1y")
                if frame is None:
                    continue
                info = blended_excess_return(frame, bench_df)
                if info.get("rs_excess") is None:
                    continue
                excess[name] = float(info["rs_excess"])
                windows[name] = list(info.get("rs_windows") or [])
            proxies = rs_proxy_from_excess(excess)
            cached = {
                "excess": excess,
                "windows": windows,
                "proxies": proxies,
                "n": len(proxies),
            }
            self._rs_cache[cache_key] = cached
        key = ticker.upper()
        return {
            "rs_proxy": cached["proxies"].get(key),
            "rs_excess": cached["excess"].get(key),
            "rs_windows": list(cached["windows"].get(key) or []),
            "rs_universe_n": int(cached["n"]),
        }

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        try:
            df = self.market_data.history(ticker, period="1y", interval="1d")
        except Exception as exc:  # noqa: BLE001 — crumb/401 / empty yfinance
            return Signal(
                agent_id=self.agent_id,
                ticker=ticker,
                direction=Direction.NEUTRAL,
                confidence=10.0,
                reasoning=f"Momentum data unavailable: {exc}",
                supporting_data={"error": str(exc), "raw_score": 0.0},
            )

        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            return Signal(
                agent_id=self.agent_id,
                ticker=ticker,
                direction=Direction.NEUTRAL,
                confidence=10.0,
                reasoning="Insufficient momentum evidence (empty OHLCV)",
                supporting_data={"raw_score": 0.0, "bars": 0},
            )

        if len(df) < 200:
            # Fall back to available bars; still compute what we can
            pass
        close = pd.to_numeric(df["Close"], errors="coerce")
        ma20 = _sma(close, 20)
        ma50 = _sma(close, 50)
        ma200 = _sma(close, min(200, max(50, len(close) // 2)))
        rsi = _rsi(close, 14)
        macd_line, macd_sig, macd_hist = _macd(close)
        atr = _atr(df, 14)

        last = _safe_float(close.iloc[-1])
        if last is None:
            return Signal(
                agent_id=self.agent_id,
                ticker=ticker,
                direction=Direction.NEUTRAL,
                confidence=10.0,
                reasoning="Insufficient momentum evidence (no valid close)",
                supporting_data={"raw_score": 0.0, "bars": len(df)},
            )

        high20 = float(df["High"].rolling(20, min_periods=5).max().iloc[-1])
        low20 = float(df["Low"].rolling(20, min_periods=5).min().iloc[-1])
        breakout_up = bool(last >= high20 * 0.995)
        breakout_dn = bool(last <= low20 * 1.005)

        # Relative strength vs benchmark
        rs_ratio = None
        rs_score = 0.0
        try:
            bdf = self.market_data.history(benchmark, period="1y", interval="1d")
            aligned = pd.concat(
                [close.rename("t"), bdf["Close"].rename("b")], axis=1, join="inner"
            ).dropna()
            if len(aligned) >= 21:
                t_ret = float(aligned["t"].iloc[-1] / aligned["t"].iloc[-21] - 1)
                b_ret = float(aligned["b"].iloc[-1] / aligned["b"].iloc[-21] - 1)
                rs_ratio = t_ret - b_ret
                rs_score = float(np.clip(rs_ratio * 100, -20, 20))
        except Exception as exc:  # noqa: BLE001 — log in supporting_data
            rs_ratio = None
            rs_score = 0.0
            rs_err = str(exc)
        else:
            rs_err = None

        score = 0.0
        reasons: list[str] = []

        m20 = _safe_float(ma20.iloc[-1])
        m50 = _safe_float(ma50.iloc[-1])
        m200 = _safe_float(ma200.iloc[-1])
        if m20 is not None and m50 is not None:
            if m20 > m50:
                score += 15
                reasons.append("MA20 > MA50 (short-term uptrend)")
            else:
                score -= 15
                reasons.append("MA20 < MA50 (short-term downtrend)")
        if m50 is not None and m200 is not None:
            if m50 > m200:
                score += 15
                reasons.append("MA50 > MA200 (intermediate uptrend)")
            else:
                score -= 15
                reasons.append("MA50 < MA200 (intermediate downtrend)")

        rsi_raw = _safe_float(rsi.iloc[-1])
        rsi_v = rsi_raw if rsi_raw is not None else 50.0
        if rsi_v >= 70:
            score -= 10
            reasons.append(f"RSI overbought ({rsi_v:.1f})")
        elif rsi_v <= 30:
            score += 10
            reasons.append(f"RSI oversold ({rsi_v:.1f})")
        elif rsi_v >= 55:
            score += 5
            reasons.append(f"RSI constructive ({rsi_v:.1f})")
        elif rsi_v <= 45:
            score -= 5
            reasons.append(f"RSI soft ({rsi_v:.1f})")

        mh_raw = _safe_float(macd_hist.iloc[-1])
        mh = mh_raw if mh_raw is not None else 0.0
        if mh > 0:
            score += 10
            reasons.append("MACD histogram positive")
        else:
            score -= 10
            reasons.append("MACD histogram negative")

        if breakout_up:
            score += 10
            reasons.append("Near/above 20d high breakout")
        if breakout_dn:
            score -= 10
            reasons.append("Near/below 20d low breakdown")

        score += rs_score
        if rs_ratio is not None:
            reasons.append(f"20d RS vs {benchmark}: {rs_ratio:+.2%}")

        atr_v = _safe_float(atr.iloc[-1])
        atr_pct = (atr_v / float(last)) if atr_v and last else None

        # Volume / demand / squeeze overlays (same OHLCV stack)
        rvol: float | None = None
        demand: dict[str, Any] = {
            "demand_imbalance": None,
            "up_volume": None,
            "down_volume": None,
            "demand_window": _DEMAND_WINDOW,
        }
        squeeze: dict[str, Any] = {
            "squeeze_on": None,
            "squeeze_release": None,
            "bb_width": None,
            "kc_width": None,
            "bb_inside_kc": None,
        }
        try:
            rvol = relative_volume(df, _RVOL_WINDOW)
            demand = demand_imbalance(df, _DEMAND_WINDOW)
            squeeze = bollinger_keltner_squeeze(df)
            score = _apply_volume_overlays(
                score=score,
                reasons=reasons,
                rvol=rvol,
                demand=demand,
                squeeze=squeeze,
                breakout_up=breakout_up,
                breakout_dn=breakout_dn,
                macd_hist=mh,
            )
        except Exception as vol_exc:  # noqa: BLE001 — never fail signal on overlay math
            reasons.append(f"Volume overlays skipped: {vol_exc}")

        # Structure overlays: VbP / Weinstein weekly stage / anchored VWAP
        vbp: dict[str, Any] = {
            "vbp_poc": None,
            "vbp_vah": None,
            "vbp_val": None,
            "price_vs_poc": None,
            "vbp_lookback": _VBP_LOOKBACK,
            "vbp_bins": _VBP_BINS,
        }
        weinstein: dict[str, Any] = {
            "weinstein_stage": None,
            "sma30w": None,
            "sma30w_slope": None,
            "weekly_bars": 0,
            "weekly_close": None,
        }
        avwap_data: dict[str, Any] = {
            "avwap": None,
            "anchor_date": None,
            "anchor_reason": None,
            "price_vs_avwap": None,
            "anchor_price": None,
        }
        try:
            vbp = volume_by_price(df)
            weinstein = weinstein_stage(df)
            avwap_data = anchored_vwap(df)
            uptrend_short = bool(m20 is not None and m50 is not None and m20 > m50)
            score = _apply_structure_overlays(
                score=score,
                reasons=reasons,
                vbp=vbp,
                weinstein=weinstein,
                avwap=avwap_data,
                uptrend_short=uptrend_short,
            )
        except Exception as struct_exc:  # noqa: BLE001 — never fail signal on overlay math
            reasons.append(f"Structure overlays skipped: {struct_exc}")

        # Minervini trend template (soft). Same daily frame as the other overlays;
        # RS rank uses as-of history only (cached bars / Yahoo), never a hard block.
        minervini: dict[str, Any] = minervini_trend_template(df, rs_proxy=None)
        minervini_delta = 0.0
        rs_excess = None
        rs_windows: list[str] = []
        rs_universe_n = 0
        try:
            rs_snap = self._rs_snapshot(ticker, benchmark)
            rs_excess = rs_snap.get("rs_excess")
            rs_windows = list(rs_snap.get("rs_windows") or [])
            rs_universe_n = int(rs_snap.get("rs_universe_n") or 0)
            minervini = minervini_trend_template(df, rs_proxy=rs_snap.get("rs_proxy"))
            score, minervini_delta = _apply_minervini_overlay(
                score=score,
                reasons=reasons,
                template=minervini,
            )
        except Exception as min_exc:  # noqa: BLE001 — never fail signal on template math
            reasons.append(f"Minervini template skipped: {min_exc}")
            minervini = minervini_trend_template(df, rs_proxy=None)

        # Map score (-80..+80ish, plus a small Minervini tilt) -> direction + confidence
        if score >= 15:
            direction = Direction.LONG
        elif score <= -15:
            direction = Direction.SHORT
        else:
            direction = Direction.NEUTRAL

        confidence = float(np.clip(50 + score * 0.6, 5, 95))

        supporting: dict[str, Any] = {
            "last_close": float(last),
            "ma20": m20,
            "ma50": m50,
            "ma200": m200,
            "rsi14": rsi_v,
            "macd_hist": mh,
            "atr14": atr_v,
            "atr_pct": atr_pct,
            "breakout_up": breakout_up,
            "breakout_down": breakout_dn,
            "rs_vs_benchmark_20d": rs_ratio,
            "rs_error": rs_err,
            "relative_volume": rvol,
            "rvol_window": _RVOL_WINDOW,
            "demand_imbalance": demand.get("demand_imbalance"),
            "up_volume": demand.get("up_volume"),
            "down_volume": demand.get("down_volume"),
            "demand_window": demand.get("demand_window"),
            "squeeze_on": squeeze.get("squeeze_on"),
            "squeeze_release": squeeze.get("squeeze_release"),
            "bb_width": squeeze.get("bb_width"),
            "kc_width": squeeze.get("kc_width"),
            "bb_width_pct": squeeze.get("bb_width_pct"),
            "kc_width_pct": squeeze.get("kc_width_pct"),
            "bb_inside_kc": squeeze.get("bb_inside_kc"),
            "vbp_poc": vbp.get("vbp_poc"),
            "vbp_vah": vbp.get("vbp_vah"),
            "vbp_val": vbp.get("vbp_val"),
            "price_vs_poc": vbp.get("price_vs_poc"),
            "vbp_lookback": vbp.get("vbp_lookback"),
            "weinstein_stage": weinstein.get("weinstein_stage"),
            "sma30w": weinstein.get("sma30w"),
            "sma30w_slope": weinstein.get("sma30w_slope"),
            "weekly_bars": weinstein.get("weekly_bars"),
            "avwap": avwap_data.get("avwap"),
            "anchor_date": avwap_data.get("anchor_date"),
            "anchor_reason": avwap_data.get("anchor_reason"),
            "price_vs_avwap": avwap_data.get("price_vs_avwap"),
            "anchor_price": avwap_data.get("anchor_price"),
            "minervini_passes": minervini.get("minervini_passes"),
            "minervini_checks": minervini.get("minervini_checks"),
            "minervini_score_delta": minervini_delta,
            "rs_proxy": minervini.get("rs_proxy"),
            "rs_excess": rs_excess,
            "rs_windows": rs_windows,
            "rs_universe_n": rs_universe_n,
            "rs_proxy_definition": RS_PROXY_DEFINITION,
            "pct_vs_52w_high": minervini.get("pct_vs_52w_high"),
            "pct_vs_52w_low": minervini.get("pct_vs_52w_low"),
            "minervini_sma50": minervini.get("sma50"),
            "minervini_sma150": minervini.get("sma150"),
            "minervini_sma200": minervini.get("sma200"),
            "sma200_month_ago": minervini.get("sma200_month_ago"),
            "week52_high": minervini.get("week52_high"),
            "week52_low": minervini.get("week52_low"),
            "raw_score": score,
            "bars": len(df),
        }

        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=direction,
            confidence=confidence,
            reasoning="; ".join(reasons) if reasons else "Insufficient momentum evidence",
            supporting_data=supporting,
        )
