"""Alpaca paper broker helpers: cancel open orders, account snapshot, reconcile."""

from __future__ import annotations

from typing import Any

from quant_system.config.loader import alpaca_credentials
from quant_system.tracker.api import Tracker


def build_trading_client() -> Any:
    """Return an Alpaca TradingClient for paper trading, or raise if unavailable."""
    creds = alpaca_credentials()
    if not creds["configured"]:
        raise RuntimeError("Alpaca credentials not configured (ALPACA_API_KEY / ALPACA_SECRET_KEY)")
    if not creds["paper"]:
        raise RuntimeError("Refusing non-paper Alpaca credentials (paper only)")
    from alpaca.trading.client import TradingClient

    return TradingClient(creds["api_key"], creds["secret_key"], paper=True)


def fetch_account_snapshot(client: Any | None = None) -> dict[str, Any]:
    """Equity/cash/buying_power + positions + open order count (no secrets)."""
    client = client or build_trading_client()
    account = client.get_account()
    positions_raw = client.get_all_positions()
    open_orders = _list_open_orders(client)

    positions = []
    for p in positions_raw or []:
        positions.append(
            {
                "ticker": str(getattr(p, "symbol", "")).upper(),
                "qty": float(getattr(p, "qty", 0) or 0),
                "avg_cost": float(getattr(p, "avg_entry_price", 0) or 0),
            }
        )

    cash = float(getattr(account, "cash", 0) or 0)
    equity = float(getattr(account, "equity", 0) or 0)
    buying_power = float(getattr(account, "buying_power", 0) or 0)
    return {
        "cash": cash,
        "equity": equity,
        "buying_power": buying_power,
        "positions": positions,
        "open_orders_count": len(list(open_orders or [])),
        "open_orders": [
            {
                "id": str(getattr(o, "id", "")),
                "symbol": str(getattr(o, "symbol", "")),
                "status": str(getattr(getattr(o, "status", ""), "value", getattr(o, "status", ""))),
                "qty": str(getattr(o, "qty", "")),
                "filled_qty": str(getattr(o, "filled_qty", "0")),
                "side": str(getattr(getattr(o, "side", ""), "value", getattr(o, "side", ""))),
            }
            for o in (open_orders or [])
        ],
    }


def _list_open_orders(client: Any) -> list[Any]:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        return list(client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)) or [])
    except Exception:
        try:
            return list(client.get_orders(status="open") or [])
        except Exception:
            all_orders = list(client.get_orders() or [])
            open_statuses = {
                "new",
                "accepted",
                "pending_new",
                "partially_filled",
                "held",
                "accepted_for_bidding",
                "pending_review",
            }
            out = []
            for o in all_orders:
                st = str(getattr(getattr(o, "status", ""), "value", getattr(o, "status", ""))).lower()
                if st in open_statuses:
                    out.append(o)
            return out


def cancel_all_open_orders(client: Any | None = None) -> dict[str, Any]:
    """Cancel all open Alpaca paper orders to free buying power."""
    client = client or build_trading_client()
    before = fetch_account_snapshot(client)
    cancelled: list[str] = []
    errors: list[str] = []

    try:
        client.cancel_orders()
        cancelled = [o["id"] for o in before.get("open_orders", [])]
    except Exception as bulk_exc:
        for o in before.get("open_orders", []):
            oid = o.get("id")
            if not oid:
                continue
            try:
                client.cancel_order_by_id(oid)
                cancelled.append(oid)
            except Exception as exc:
                errors.append(f"{oid}: {exc}")
        if not cancelled and not before.get("open_orders"):
            errors.append(str(bulk_exc))

    after = fetch_account_snapshot(client)
    return {
        "cancelled_count": len(cancelled),
        "cancelled_ids": cancelled,
        "errors": errors,
        "open_orders_before": before["open_orders_count"],
        "open_orders_after": after["open_orders_count"],
        "buying_power_after": after["buying_power"],
        "cash": after["cash"],
        "equity": after["equity"],
    }


def reconcile_tracker_to_alpaca(
    tracker: Tracker,
    client: Any | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Set tracker cash/equity/positions from Alpaca account+positions snapshot."""
    snap = snapshot or fetch_account_snapshot(client)
    before = {
        "cash": float(tracker.get_state("cash", "0")),
        "equity": float(tracker.get_state("equity", "0")),
        "positions": tracker.list_positions(),
    }
    result = tracker.reconcile_portfolio(
        cash=float(snap["cash"]),
        equity=float(snap["equity"]),
        positions=list(snap["positions"]),
        source="alpaca",
    )
    return {
        "before": before,
        "after": {
            "cash": float(snap["cash"]),
            "equity": float(snap["equity"]),
            "positions": list(snap["positions"]),
        },
        "alpaca": {
            "buying_power": snap.get("buying_power"),
            "open_orders_count": snap.get("open_orders_count"),
        },
        "reconcile": result,
    }
