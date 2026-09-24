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
        pos: dict[str, Any] = {
            "ticker": str(getattr(p, "symbol", "")).upper(),
            "qty": float(getattr(p, "qty", 0) or 0),
            "avg_cost": float(getattr(p, "avg_entry_price", 0) or 0),
        }
        for src, dest in (
            ("market_value", "market_value"),
            ("unrealized_pl", "unrealized_pl"),
            ("current_price", "current_price"),
        ):
            raw = getattr(p, src, None)
            if raw is None or raw == "":
                pos[dest] = None
            else:
                try:
                    pos[dest] = float(raw)
                except (TypeError, ValueError):
                    pos[dest] = None
        positions.append(pos)

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


def fetch_closed_filled_orders(
    client: Any | None = None,
    *,
    symbols: list[str] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return filled (closed) paper orders as plain dicts. Paper endpoint only."""
    client = client or build_trading_client()
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = list(
            client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit, nested=True)
            )
            or []
        )
    except Exception:
        orders = list(client.get_orders(status="closed", limit=limit) or [])

    want = {s.upper() for s in symbols} if symbols else None
    out: list[dict[str, Any]] = []
    for o in orders:
        sym = str(getattr(o, "symbol", "") or "").upper()
        if want is not None and sym not in want:
            continue
        status = str(getattr(getattr(o, "status", ""), "value", getattr(o, "status", ""))).lower()
        if status != "filled":
            continue
        side = str(getattr(getattr(o, "side", ""), "value", getattr(o, "side", ""))).lower()
        filled_qty = float(getattr(o, "filled_qty", 0) or 0)
        if filled_qty <= 0:
            continue
        px_raw = getattr(o, "filled_avg_price", None)
        if px_raw is None or px_raw == "":
            continue
        price = float(px_raw)
        filled_at = getattr(o, "filled_at", None) or getattr(o, "submitted_at", None)
        if filled_at is None:
            continue
        if hasattr(filled_at, "strftime"):
            # Alpaca returns aware datetime; normalize to UTC Z
            from datetime import timezone as _tz

            if getattr(filled_at, "tzinfo", None) is None:
                ts = filled_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            else:
                ts = filled_at.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        else:
            ts = str(filled_at).replace("+00:00", "Z")
        out.append(
            {
                "order_id": str(getattr(o, "id", "")),
                "ticker": sym,
                "side": side,
                "qty": filled_qty,
                "price": price,
                "filled_at": ts,
                "status": status,
            }
        )
    out.sort(key=lambda r: (r["filled_at"], r["order_id"]))
    return out


def tickers_with_sells_missing_buys(tracker: Tracker) -> list[str]:
    """Tickers that have sell fills but no buy fills at all in Tracker."""
    rows = tracker.conn.execute(
        """
        SELECT DISTINCT s.ticker AS ticker
        FROM fills s
        WHERE s.side = 'sell'
          AND NOT EXISTS (
            SELECT 1 FROM fills b
            WHERE b.ticker = s.ticker AND b.side = 'buy'
          )
        ORDER BY s.ticker
        """
    ).fetchall()
    return [str(r["ticker"]).upper() for r in rows]


def backfill_missing_buy_fills(
    tracker: Tracker,
    client: Any | None = None,
    *,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Insert missing Alpaca paper buy fills that Closed Book needs for entry/PnL.

    Does **not** apply fills to positions (avoids corrupting reconciled open qty).
    Dedupes by Alpaca order_id and by ticker+side+qty+time prefix.
    """
    from quant_system.models.ticket import TicketSide, TicketStatus, TradeTicket

    missing = symbols or tickers_with_sells_missing_buys(tracker)
    missing = [s.upper() for s in missing]
    if not missing:
        return {"inserted": [], "skipped": [], "missing_tickers": [], "unresolved": []}

    orders = fetch_closed_filled_orders(client, symbols=missing)
    buys = [o for o in orders if o["side"] == "buy"]
    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for o in buys:
        oid = o["order_id"]
        if tracker.find_fill_by_order_id(oid):
            skipped.append({**o, "reason": "order_id_exists"})
            continue
        if tracker.has_similar_fill(o["ticker"], "buy", o["qty"], o["filled_at"]):
            skipped.append({**o, "reason": "similar_fill_exists"})
            continue
        ticket = TradeTicket(
            ticker=o["ticker"],
            side=TicketSide.BUY,
            qty=float(o["qty"]),
            order_type="market",
            rationale=f"alpaca_backfill order_id={oid}",
            status=TicketStatus.FILLED,
            created_at=o["filled_at"],
        )
        tid = tracker.insert_ticket(ticket)
        fill_id = tracker.insert_fill_historical(
            tid,
            o["ticker"],
            "buy",
            float(o["qty"]),
            float(o["price"]),
            "alpaca_backfill",
            o["filled_at"],
            order_id=oid,
            apply_to_position=False,
        )
        inserted.append({**o, "ticket_id": tid, "fill_id": fill_id})

    still_missing = tickers_with_sells_missing_buys(tracker)
    unresolved = [t for t in missing if t in still_missing]
    return {
        "missing_tickers": missing,
        "inserted": inserted,
        "skipped": skipped,
        "unresolved": unresolved,
        "buy_orders_seen": len(buys),
    }
