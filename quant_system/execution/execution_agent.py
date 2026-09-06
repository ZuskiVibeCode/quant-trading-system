"""ExecutionAgent — Alpaca paper when keys present; otherwise dry-run sim fills.

Hard rules: paper only; no withdraw/transfer/account-settings mutations; respect halt.
Only record fills when the broker reports a real fill (filled_qty > 0).
"""

from __future__ import annotations

from typing import Any

from quant_system.config.loader import alpaca_credentials
from quant_system.market_data.interface import MarketData
from quant_system.models.ticket import TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker

# Broker statuses that mean shares actually traded (with filled_qty > 0).
_FILL_STATUSES = frozenset({"filled", "partially_filled"})
# Still open / not yet traded — do not invent a local fill.
_PENDING_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "held",
        "pending_review",
        "calculated",
        "stopped",
        "suspended",
        "pending_replace",
        "pending_cancel",
        "replaced",
    }
)


def _order_status_str(order: Any) -> str:
    status = getattr(order, "status", None)
    if status is None:
        return ""
    return str(getattr(status, "value", status)).lower()


def _filled_qty(order: Any) -> float:
    raw = getattr(order, "filled_qty", None)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _filled_avg_price(order: Any, fallback: float) -> float:
    raw = getattr(order, "filled_avg_price", None)
    if raw is None:
        return float(fallback)
    try:
        val = float(raw)
        return val if val > 0 else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)


def is_real_fill(order: Any) -> bool:
    """True only when broker reports shares filled."""
    qty = _filled_qty(order)
    if qty <= 0:
        return False
    status = _order_status_str(order)
    if status in _FILL_STATUSES:
        return True
    # Some SDKs may leave status empty but still report filled_qty.
    return qty > 0 and status not in _PENDING_STATUSES and status not in {
        "canceled",
        "cancelled",
        "expired",
        "rejected",
    }


class ExecutionAgent:
    def __init__(self, tracker: Tracker, market_data: MarketData):
        self.tracker = tracker
        self.market_data = market_data
        self.creds = alpaca_credentials()
        self._alpaca = None
        if self.creds["configured"]:
            self._alpaca = self._init_alpaca()

    def _init_alpaca(self) -> Any:
        try:
            from alpaca.trading.client import TradingClient

            return TradingClient(
                self.creds["api_key"],
                self.creds["secret_key"],
                paper=bool(self.creds["paper"]),
            )
        except Exception as exc:  # noqa: BLE001
            self.tracker.log_decision(
                decision_type="execution_init",
                actor="execution_agent",
                reasoning=f"Alpaca init failed, falling back to dry-run: {exc}",
                payload={"error": str(exc)},
            )
            return None

    @property
    def mode(self) -> str:
        return "alpaca" if self._alpaca is not None else "dry_run"

    def execute(self, ticket: TradeTicket) -> dict[str, Any] | None:
        blocked, reason = self.tracker.orders_blocked()
        if blocked:
            if ticket.id is not None:
                self.tracker.update_ticket_status(ticket.id, TicketStatus.BLOCKED)
            self.tracker.log_decision(
                decision_type="blocked",
                actor="execution_agent",
                ticker=ticket.ticker,
                reasoning=f"Order blocked by kill switch: {reason}",
                payload={"ticket": ticket.model_dump(), "reason": reason},
            )
            return None

        if ticket.id is None:
            raise ValueError("Ticket must be persisted (id) before execution")

        # Never mutate account settings / withdraw / transfer — marketable orders only.
        price = self.market_data.last_price(ticket.ticker)
        side = ticket.side.value if hasattr(ticket.side, "value") else str(ticket.side)

        if self._alpaca is not None:
            return self._execute_alpaca(ticket, price, side)
        return self._execute_dry_run(ticket, price, side)

    def _execute_dry_run(self, ticket: TradeTicket, price: float, side: str) -> dict[str, Any]:
        self.tracker.update_ticket_status(ticket.id, TicketStatus.SUBMITTED)
        fill_id = self.tracker.insert_fill(
            ticket_id=ticket.id,
            ticker=ticket.ticker,
            side=side,
            qty=float(ticket.qty),
            price=float(price),
            mode="dry_run",
        )
        # Refresh equity snapshot
        cash = float(self.tracker.get_state("cash", "100"))
        positions = self.tracker.list_positions()
        equity = cash
        for p in positions:
            try:
                px = self.market_data.last_price(p["ticker"])
            except Exception:  # noqa: BLE001
                px = float(p["avg_cost"])
            equity += float(p["qty"]) * px
        peak = max(float(self.tracker.get_state("peak_equity", "100")), equity)
        day_start = float(self.tracker.get_state("day_start_equity", "100"))
        self.tracker.snapshot_portfolio(
            equity=equity,
            cash=cash,
            positions=positions,
            daily_pnl=equity - day_start,
            drawdown=(peak - equity) / peak if peak else 0.0,
            peak_equity=peak,
        )
        return {
            "mode": "dry_run",
            "fill_id": fill_id,
            "ticket_id": ticket.id,
            "ticker": ticket.ticker,
            "side": side,
            "qty": ticket.qty,
            "price": price,
        }

    def _execute_alpaca(self, ticket: TradeTicket, price: float, side: str) -> dict[str, Any]:
        """Submit paper market order only. No account mutations.

        Records a Tracker fill only when Alpaca reports filled_qty > 0.
        Accepted/new/pending orders are logged as order_submitted — cash untouched.
        API errors (e.g. 403 insufficient buying power) are logged and returned;
        they must never crash run-once.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        assert self._alpaca is not None
        if not self.creds["paper"]:
            # Hard refuse live trading
            self.tracker.update_ticket_status(ticket.id, TicketStatus.BLOCKED)
            self.tracker.log_decision(
                decision_type="blocked",
                actor="execution_agent",
                ticker=ticket.ticker,
                reasoning="Refusing non-paper Alpaca credentials (paper only)",
                payload={"ticket_id": ticket.id},
            )
            return {"mode": "blocked", "reason": "paper_only"}

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=ticket.ticker,
            qty=float(ticket.qty),
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        self.tracker.update_ticket_status(ticket.id, TicketStatus.SUBMITTED)

        try:
            order = self._alpaca.submit_order(req)
        except Exception as exc:  # noqa: BLE001 — include APIError and network
            self.tracker.update_ticket_status(ticket.id, TicketStatus.FAILED)
            self.tracker.log_decision(
                decision_type="order_failed",
                actor="execution_agent",
                ticker=ticket.ticker,
                reasoning=f"Alpaca order rejected/failed: {exc}",
                payload={
                    "ticket_id": ticket.id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "side": side,
                    "qty": float(ticket.qty),
                },
            )
            return {
                "mode": "alpaca",
                "status": "failed",
                "ticket_id": ticket.id,
                "ticker": ticket.ticker,
                "error": str(exc),
            }

        status = _order_status_str(order)
        filled_qty = _filled_qty(order)
        order_id = str(getattr(order, "id", ""))

        if is_real_fill(order):
            fill_price = _filled_avg_price(order, price)
            fill_id = self.tracker.insert_fill(
                ticket_id=ticket.id,
                ticker=ticket.ticker,
                side=side,
                qty=filled_qty,
                price=fill_price,
                mode="alpaca",
            )
            self.tracker.log_decision(
                decision_type="alpaca_order",
                actor="execution_agent",
                ticker=ticket.ticker,
                reasoning=f"Alpaca paper order filled ({status})",
                payload={
                    "order_id": order_id,
                    "ticket_id": ticket.id,
                    "status": status,
                    "filled_qty": filled_qty,
                    "fill_id": fill_id,
                },
            )
            return {
                "mode": "alpaca",
                "status": "filled",
                "fill_id": fill_id,
                "ticket_id": ticket.id,
                "order_id": order_id,
                "qty": filled_qty,
                "price": fill_price,
            }

        # Accepted / new / pending / rejected without fill — do not move cash.
        decision_type = "order_submitted"
        reasoning = f"Alpaca paper order submitted; awaiting fill (status={status})"
        if status in {"rejected", "canceled", "cancelled", "expired"}:
            decision_type = "order_failed"
            reasoning = f"Alpaca order ended without fill (status={status})"
            self.tracker.update_ticket_status(ticket.id, TicketStatus.FAILED)
        else:
            # Keep SUBMITTED; optionally mark pending explicitly via decision log.
            decision_type = "order_submitted"

        self.tracker.log_decision(
            decision_type=decision_type,
            actor="execution_agent",
            ticker=ticket.ticker,
            reasoning=reasoning,
            payload={
                "order_id": order_id,
                "ticket_id": ticket.id,
                "status": status,
                "filled_qty": filled_qty,
                "requested_qty": float(ticket.qty),
            },
        )
        return {
            "mode": "alpaca",
            "status": status or "pending",
            "ticket_id": ticket.id,
            "order_id": order_id,
            "filled_qty": filled_qty,
            "price": price,
        }
