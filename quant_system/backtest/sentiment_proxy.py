"""OHLCV sentiment surrogate for backtests (no historical news replay).

Uses short-horizon return + volume shock vs a trailing baseline. Clearly labeled
as a proxy in Signal.supporting_data — better than a permanent neutral stub, but
not a substitute for headline polarity.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent

PROXY_KIND = "return_volume_shock"
PROXY_DOC = (
    "backtest sentiment proxy: 5d return + volume vs 20d avg "
    "(no historical news replay)"
)


def score_return_volume_proxy(history: pd.DataFrame) -> dict[str, Any]:
    """Map recent OHLCV into a sentiment-like direction / confidence."""
    if history is None or getattr(history, "empty", True) or len(history) < 6:
        return {
            "direction": Direction.NEUTRAL,
            "confidence": 15.0,
            "raw_score": 0.0,
            "ret_5d": None,
            "vol_shock": None,
            "reasoning": "Proxy sentiment: insufficient as-of history (neutral)",
        }

    df = history.copy()
    df = df.rename(columns=str.title)
    closes = df["Close"].astype(float)
    volumes = df["Volume"].astype(float)

    ret_5d = float(closes.iloc[-1] / closes.iloc[-6] - 1.0)
    lookback = min(20, len(volumes) - 1)
    baseline = float(volumes.iloc[-(lookback + 1) : -1].mean()) if lookback >= 3 else float(volumes.mean())
    last_vol = float(volumes.iloc[-1])
    vol_shock = (last_vol / baseline - 1.0) if baseline > 0 else 0.0

    # Soft score: return dominates; volume shock amplifies conviction
    score = ret_5d * 100.0  # e.g. +3% → +3
    if abs(ret_5d) >= 0.01:
        score *= 1.0 + max(-0.5, min(1.0, vol_shock * 0.5))

    if score >= 2.5:
        direction = Direction.LONG
    elif score <= -2.5:
        direction = Direction.SHORT
    else:
        direction = Direction.NEUTRAL

    conf = 20.0 + min(40.0, abs(score) * 4.0)
    if abs(vol_shock) >= 0.5 and abs(ret_5d) >= 0.015:
        conf += 8.0
    confidence = float(max(10.0, min(75.0, conf)))

    reasoning = (
        f"Proxy sentiment ({PROXY_KIND}): 5d return {ret_5d:+.2%}, "
        f"volume shock {vol_shock:+.0%} vs ~{lookback}d avg"
    )
    return {
        "direction": direction,
        "confidence": confidence,
        "raw_score": round(score, 3),
        "ret_5d": round(ret_5d, 6),
        "vol_shock": round(vol_shock, 4),
        "lookback_vol_days": lookback,
        "reasoning": reasoning,
    }


class ProxySentimentAgent(ResearchAgent):
    """Sentiment desk stand-in for backtests — OHLCV return/volume shock proxy."""

    agent_id = "sentiment"

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        fetch_error: str | None = None
        try:
            hist = self.market_data.history(ticker, period="3mo", interval="1d")
        except Exception as exc:  # noqa: BLE001
            hist = None
            fetch_error = str(exc)

        result = score_return_volume_proxy(hist if hist is not None else pd.DataFrame())
        supporting: dict[str, Any] = {
            "proxy": True,
            "proxy_kind": PROXY_KIND,
            "stubbed": False,
            "credibility": 0.35,  # lower than wire news; honest about proxy quality
            "manipulation_risk": False,
            "manipulation_evidence": [],
            "raw_score": result["raw_score"],
            "ret_5d": result.get("ret_5d"),
            "vol_shock": result.get("vol_shock"),
            "benchmark": benchmark,
            "fetch_error": fetch_error,
            "headline_count": 0,
            "note": PROXY_DOC,
        }
        return Signal(
            agent_id=self.agent_id,
            ticker=ticker.upper(),
            direction=result["direction"],
            confidence=float(result["confidence"]),
            reasoning=result["reasoning"],
            supporting_data=supporting,
        )
