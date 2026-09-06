"""MacroAgent — risk-on / risk-off / neutral regime."""

from __future__ import annotations

import math

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent


class MacroAgent(ResearchAgent):
    """Regime from SPY trend + realized vol. Applies same regime tilt to each ticker."""

    agent_id = "macro"

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        bdf = self.market_data.history(benchmark, period="1y", interval="1d")
        close = bdf["Close"]
        ma50 = close.rolling(50, min_periods=20).mean().iloc[-1]
        ma200 = close.rolling(200, min_periods=50).mean().iloc[-1]
        last = float(close.iloc[-1])
        rets = close.pct_change().dropna()
        vol20 = float(rets.iloc[-20:].std(ddof=1) * (252**0.5)) if len(rets) >= 20 else None

        regime = "neutral"
        direction = Direction.NEUTRAL
        confidence = 40.0
        reasons = []

        if not math.isnan(float(ma50)) and not math.isnan(float(ma200)):
            if last > float(ma50) > float(ma200) and (vol20 is None or vol20 < 0.25):
                regime = "risk_on"
                direction = Direction.LONG
                confidence = 60.0
                reasons.append(f"{benchmark} above MA50>MA200; vol controlled")
            elif last < float(ma50) < float(ma200) or (vol20 is not None and vol20 > 0.35):
                regime = "risk_off"
                direction = Direction.SHORT
                confidence = 60.0
                reasons.append(f"{benchmark} below MAs and/or elevated vol")
            else:
                reasons.append(f"{benchmark} mixed trend/vol -> neutral regime")
        else:
            reasons.append("Insufficient benchmark history for regime")

        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=direction,
            confidence=confidence,
            reasoning=f"Macro regime={regime}. " + "; ".join(reasons),
            supporting_data={
                "regime": regime,
                "benchmark": benchmark,
                "benchmark_last": last,
                "benchmark_ma50": float(ma50) if not math.isnan(float(ma50)) else None,
                "benchmark_ma200": float(ma200) if not math.isnan(float(ma200)) else None,
                "realized_vol_20d_ann": vol20,
                "todos": [
                    "Add rates/credit/VIX proxies when data available",
                    "Theme-specific beta overlays",
                ],
            },
        )
