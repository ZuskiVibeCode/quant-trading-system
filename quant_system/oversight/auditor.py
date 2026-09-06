"""Auditor — flags only; cannot override PortfolioManager decisions."""

from __future__ import annotations

from typing import Any

from quant_system.config.loader import AppConfig
from quant_system.models.signal import Signal
from quant_system.models.ticket import TradeTicket
from quant_system.tracker.api import Tracker


class Auditor:
    """Read decisions/signals/tickets and raise audit flags. Never mutates orders."""

    def __init__(self, tracker: Tracker, config: AppConfig):
        self.tracker = tracker
        self.config = config

    def review_signals(self, signals: list[tuple[int, Signal]]) -> list[int]:
        flag_ids: list[int] = []
        for sid, sig in signals:
            # Sentiment manipulation risk
            if sig.agent_id == "sentiment":
                manip = bool(sig.supporting_data.get("manipulation_risk"))
                cred = float(sig.supporting_data.get("credibility", 0) or 0)
                if manip or cred < 0.3:
                    fid = self.tracker.insert_audit_flag(
                        flag_type="sentiment_risk",
                        severity="warning",
                        message=(
                            f"Sentiment signal {sid} for {sig.ticker}: "
                            f"manipulation_risk={manip}, credibility={cred}"
                        ),
                        related_table="signals",
                        related_id=sid,
                    )
                    flag_ids.append(fid)
            if sig.confidence > 95:
                fid = self.tracker.insert_audit_flag(
                    flag_type="overconfidence",
                    severity="info",
                    message=f"Signal {sid} confidence {sig.confidence} unusually high",
                    related_table="signals",
                    related_id=sid,
                )
                flag_ids.append(fid)
        return flag_ids

    def review_ticket(self, ticket: TradeTicket, equity: float) -> list[int]:
        flag_ids: list[int] = []
        if ticket.id is None:
            return flag_ids
        # Position size check (flag only — PM already enforces)
        try:
            # approximate notional from rationale is hard; flag large qty heuristically via state
            max_pct = self.config.risk.max_position_pct
            # Soft check: if any position already > cap, flag
            for p in self.tracker.list_positions():
                # Auditor cannot know live price always; flag if qty weird
                if float(p["qty"]) < 0 and not self.config.risk.allow_short:
                    fid = self.tracker.insert_audit_flag(
                        flag_type="unexpected_short",
                        severity="critical",
                        message=f"Short position detected in {p['ticker']} while allow_short=false",
                        related_table="positions",
                        related_id=None,
                    )
                    flag_ids.append(fid)
            _ = max_pct, equity  # documented for future price-marked checks
        except Exception as exc:  # noqa: BLE001
            fid = self.tracker.insert_audit_flag(
                flag_type="auditor_error",
                severity="warning",
                message=str(exc),
                related_table="trade_tickets",
                related_id=ticket.id,
            )
            flag_ids.append(fid)
        return flag_ids

    def review_run(self, summary: dict[str, Any]) -> list[int]:
        flag_ids: list[int] = []
        if summary.get("errors"):
            fid = self.tracker.insert_audit_flag(
                flag_type="run_errors",
                severity="warning",
                message=f"Run completed with errors: {summary['errors']}",
            )
            flag_ids.append(fid)
        return flag_ids
