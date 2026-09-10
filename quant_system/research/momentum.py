"""MomentumAgent — MA 20/50/200, RSI, MACD, breakouts, ATR, RS, volume / demand / squeeze."""

from __future__ import annotations

import math
from typing import Any

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


class MomentumAgent(ResearchAgent):
    agent_id = "momentum"

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

        # Map score (-80..+80ish) -> direction + confidence
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
