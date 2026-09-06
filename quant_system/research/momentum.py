"""MomentumAgent — MA 20/50/200, RSI, MACD, breakouts, ATR, RS vs sector/benchmark."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent


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


class MomentumAgent(ResearchAgent):
    agent_id = "momentum"

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        df = self.market_data.history(ticker, period="1y", interval="1d")
        if len(df) < 200:
            # Fall back to available bars; still compute what we can
            pass
        close = df["Close"]
        ma20 = _sma(close, 20)
        ma50 = _sma(close, 50)
        ma200 = _sma(close, min(200, max(50, len(close) // 2)))
        rsi = _rsi(close, 14)
        macd_line, macd_sig, macd_hist = _macd(close)
        atr = _atr(df, 14)

        last = close.iloc[-1]
        high20 = df["High"].rolling(20, min_periods=5).max().iloc[-1]
        low20 = df["Low"].rolling(20, min_periods=5).min().iloc[-1]
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

        m20, m50, m200 = float(ma20.iloc[-1]), float(ma50.iloc[-1]), float(ma200.iloc[-1])
        if not math.isnan(m20) and not math.isnan(m50):
            if m20 > m50:
                score += 15
                reasons.append("MA20 > MA50 (short-term uptrend)")
            else:
                score -= 15
                reasons.append("MA20 < MA50 (short-term downtrend)")
        if not math.isnan(m50) and not math.isnan(m200):
            if m50 > m200:
                score += 15
                reasons.append("MA50 > MA200 (intermediate uptrend)")
            else:
                score -= 15
                reasons.append("MA50 < MA200 (intermediate downtrend)")

        rsi_v = float(rsi.iloc[-1]) if not math.isnan(float(rsi.iloc[-1])) else 50.0
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

        mh = float(macd_hist.iloc[-1]) if not math.isnan(float(macd_hist.iloc[-1])) else 0.0
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

        atr_v = float(atr.iloc[-1]) if not math.isnan(float(atr.iloc[-1])) else None
        atr_pct = (atr_v / float(last)) if atr_v and last else None

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
