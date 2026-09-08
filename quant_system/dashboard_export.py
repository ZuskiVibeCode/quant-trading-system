"""Export Tracker SQLite into docs/data/latest.json for the static Orbit viewer (Vercel).

Frozen schema — keep keys stable; see docs/SCHEMA.md.
Never write Alpaca secrets into the payload.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from quant_system.config.loader import load_config
from quant_system.tracker.api import Tracker
from quant_system.tracker.schema import DEFAULT_AGENT_IDS

REQUIRED_TOP_KEYS = (
    "meta",
    "snapshot",
    "holdings",
    "closed",
    "equity_curve",
    "agents_today",
    "agents_history",
    "decisions_today",
    "auditor_flags_today",
    "mission_log",
    "watchlist",
    "sectors",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utc_day_start(ts: datetime | None = None) -> str:
    t = ts or datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT00:00:00.000000Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _summarize_supporting(data: Any, max_keys: int = 8) -> dict[str, Any] | str | None:
    if data is None:
        return None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return data[:240] if len(data) > 240 else data
    if not isinstance(data, dict):
        return str(data)[:240]
    preferred = (
        "regime",
        "macro_risk_level",
        "sector_bias",
        "theme",
        "quality_score",
        "valuation_note",
        "statistical_edge_score",
        "manipulation_risk",
        "credibility",
        "rsi",
        "macd_signal",
        "relative_strength",
        "edge_type",
        "vol_regime",
    )
    out: dict[str, Any] = {}
    for k in preferred:
        if k in data and len(out) < max_keys:
            v = data[k]
            if isinstance(v, (dict, list)):
                continue
            out[k] = v
    if len(out) < max_keys:
        for k, v in data.items():
            if k in out or isinstance(v, (dict, list)):
                continue
            out[k] = v
            if len(out) >= max_keys:
                break
    return out


def _sector_map(themes: dict[str, list[str]]) -> dict[str, str]:
    m: dict[str, str] = {}
    for theme, tickers in themes.items():
        for t in tickers:
            m[t.upper()] = theme
    return m


def _query_since(tracker: Tracker, table: str, since: str, limit: int = 2000) -> list[dict[str, Any]]:
    json_fields = {
        "signals": ("supporting_data",),
        "decisions": ("payload", "data_snapshot"),
    }.get(table, ())
    rows = tracker.conn.execute(
        f"SELECT * FROM {table} WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    return [tracker._row_to_dict(r, json_fields=json_fields) for r in rows]


def _parse_signal_ids(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, list):
        out: list[int] = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return _parse_signal_ids(data)
    return []


def _fill_brief(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "ticket_id": int(row["ticket_id"]),
        "ticker": str(row["ticker"]).upper(),
        "side": row["side"],
        "qty": float(row["qty"]),
        "price": float(row["price"]),
        "mode": row["mode"],
        "created_at": row["created_at"],
    }


def _ticket_brief(row: Any) -> dict[str, Any]:
    signal_ids = _parse_signal_ids(row["signal_ids"])
    return {
        "id": int(row["id"]),
        "ticker": str(row["ticker"]).upper(),
        "side": row["side"],
        "qty": float(row["qty"]),
        "order_type": row["order_type"],
        "rationale": row["rationale"] or "",
        "signal_ids": signal_ids,
        "status": row["status"],
        "limit_price": float(row["limit_price"]) if row["limit_price"] is not None else None,
        "created_at": row["created_at"],
    }


def _signals_by_ids(tracker: Tracker, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    # Preserve ticket order; skip missing ids (never invent).
    placeholders = ",".join("?" for _ in ids)
    rows = tracker.conn.execute(
        f"SELECT * FROM signals WHERE id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    by_id = {
        int(r["id"]): tracker._row_to_dict(r, json_fields=("supporting_data",)) for r in rows
    }
    out: list[dict[str, Any]] = []
    for sid in ids:
        s = by_id.get(int(sid))
        if not s:
            continue
        out.append(
            {
                "signal_id": s.get("id"),
                "agent_id": s.get("agent_id"),
                "ticker": s.get("ticker"),
                "direction": s.get("direction"),
                "confidence": float(s.get("confidence") or 0),
                "reasoning": s.get("reasoning") or "",
                "supporting_data": _summarize_supporting(s.get("supporting_data")),
                "created_at": s.get("created_at"),
            }
        )
    return out


def _ticket_row(tracker: Tracker, ticket_id: int | None) -> Any | None:
    if ticket_id is None:
        return None
    return tracker.conn.execute(
        "SELECT * FROM trade_tickets WHERE id = ?",
        (int(ticket_id),),
    ).fetchone()


def _reasoning_bundle(tracker: Tracker, ticket_row: Any | None, fill_row: Any | None) -> dict[str, Any]:
    ticket = _ticket_brief(ticket_row) if ticket_row is not None else None
    fill = _fill_brief(fill_row) if fill_row is not None else None
    signal_ids = (ticket or {}).get("signal_ids") or []
    return {
        "ticket": ticket,
        "fill": fill,
        "signals": _signals_by_ids(tracker, list(signal_ids)),
    }


def _best_entry_buy_fill(tracker: Tracker, ticker: str, before_fill_id: int | None = None) -> Any | None:
    if before_fill_id is None:
        return tracker.conn.execute(
            """
            SELECT * FROM fills
            WHERE ticker = ? AND side = 'buy'
            ORDER BY id DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
    return tracker.conn.execute(
        """
        SELECT * FROM fills
        WHERE ticker = ? AND side = 'buy' AND id < ?
        ORDER BY id DESC LIMIT 1
        """,
        (ticker, before_fill_id),
    ).fetchone()


def _best_entry_buy_ticket(tracker: Tracker, ticker: str, *, prefer_qty: float | None = None) -> Any | None:
    """Prefer filled buy ticket; else submitted. Prefer qty match when provided."""
    rows = tracker.conn.execute(
        """
        SELECT * FROM trade_tickets
        WHERE ticker = ? AND side = 'buy' AND status IN ('filled', 'submitted')
        ORDER BY
          CASE status WHEN 'filled' THEN 0 WHEN 'submitted' THEN 1 ELSE 2 END,
          id ASC
        """,
        (ticker,),
    ).fetchall()
    if not rows:
        return None
    if prefer_qty is not None:
        for r in rows:
            if abs(float(r["qty"]) - float(prefer_qty)) < 1e-6:
                return r
    # latest filled, else earliest submitted
    filled = [r for r in rows if r["status"] == "filled"]
    if filled:
        return filled[-1]
    return rows[0]


def _build_closed(tracker: Tracker, sector_of: dict[str, str]) -> list[dict[str, Any]]:
    """Pair sell fills with prior entry context + stored ticket/signal reasoning."""
    sells = tracker.conn.execute(
        "SELECT * FROM fills WHERE side = 'sell' ORDER BY id ASC"
    ).fetchall()
    if not sells:
        return []
    closed: list[dict[str, Any]] = []
    for s in sells:
        ticker = str(s["ticker"]).upper()
        buy_avg = tracker.conn.execute(
            """
            SELECT AVG(price) AS avg_price FROM fills
            WHERE ticker = ? AND side = 'buy' AND id < ?
            """,
            (ticker, s["id"]),
        ).fetchone()
        entry = float(buy_avg["avg_price"]) if buy_avg and buy_avg["avg_price"] is not None else None
        exit_px = float(s["price"])
        qty = float(s["qty"])
        pnl = None if entry is None else (exit_px - entry) * qty

        entry_fill = _best_entry_buy_fill(tracker, ticker, before_fill_id=int(s["id"]))
        entry_ticket_row = None
        if entry_fill is not None:
            entry_ticket_row = _ticket_row(tracker, int(entry_fill["ticket_id"]))
        if entry_ticket_row is None:
            entry_ticket_row = _best_entry_buy_ticket(tracker, ticker)

        exit_ticket_row = _ticket_row(tracker, int(s["ticket_id"]))
        buy = _reasoning_bundle(tracker, entry_ticket_row, entry_fill)
        sell = _reasoning_bundle(tracker, exit_ticket_row, s)

        closed.append(
            {
                "ticker": ticker,
                "qty": qty,
                "entry": entry,
                "exit": exit_px,
                "pnl": pnl,
                "sector": sector_of.get(ticker),
                "closed_at": s["created_at"],
                "opened_at": (buy.get("fill") or {}).get("created_at")
                or (buy.get("ticket") or {}).get("created_at"),
                "mode": s["mode"],
                "fill_id": int(s["id"]),
                "ticket_id": int(s["ticket_id"]),
                "buy": buy,
                "sell": sell,
            }
        )
    return closed


def _enrich_holdings(
    tracker: Tracker,
    holdings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach buy ticket / signals / fill reasoning to open positions (stored data only)."""
    enriched: list[dict[str, Any]] = []
    for h in holdings:
        ticker = str(h["ticker"]).upper()
        qty = float(h.get("qty") or 0)
        entry_fill = _best_entry_buy_fill(tracker, ticker)
        entry_ticket_row = None
        if entry_fill is not None:
            entry_ticket_row = _ticket_row(tracker, int(entry_fill["ticket_id"]))
        if entry_ticket_row is None:
            entry_ticket_row = _best_entry_buy_ticket(tracker, ticker, prefer_qty=qty)
        buy = _reasoning_bundle(tracker, entry_ticket_row, entry_fill)
        fills = [
            _fill_brief(r)
            for r in tracker.conn.execute(
                "SELECT * FROM fills WHERE ticker = ? ORDER BY id ASC",
                (ticker,),
            ).fetchall()
        ]
        item = dict(h)
        item["buy"] = buy
        item["fills"] = fills
        item["opened_at"] = (buy.get("fill") or {}).get("created_at") or (
            buy.get("ticket") or {}
        ).get("created_at")
        enriched.append(item)
    return enriched



def _agents_today(signals: list[dict[str, Any]]) -> dict[str, Any]:
    by_agent: dict[str, list[dict[str, Any]]] = {a: [] for a in DEFAULT_AGENT_IDS}
    for s in signals:
        aid = s.get("agent_id") or "unknown"
        by_agent.setdefault(aid, [])
        by_agent[aid].append(
            {
                "ticker": s["ticker"],
                "direction": s["direction"],
                "confidence": float(s["confidence"]),
                "reasoning": s.get("reasoning") or "",
                "supporting_data": _summarize_supporting(s.get("supporting_data")),
                "created_at": s.get("created_at"),
                "signal_id": s.get("id"),
            }
        )
    out: dict[str, Any] = {}
    for aid, items in by_agent.items():
        # keep chronological within agent (newest first already)
        out[aid] = {
            "agent_id": aid,
            "count": len(items),
            "long": sum(1 for x in items if x["direction"] == "long"),
            "short": sum(1 for x in items if x["direction"] == "short"),
            "neutral": sum(1 for x in items if x["direction"] == "neutral"),
            "signals": items,
        }
    return out


def _agents_history(tracker: Tracker, *, days: int = 30) -> dict[str, list[dict[str, Any]]]:
    """Daily signal buckets per agent for the last `days` UTC days (inclusive of today)."""
    start = (datetime.now(timezone.utc) - timedelta(days=max(days - 1, 0))).strftime("%Y-%m-%d")
    rows = tracker.conn.execute(
        """
        SELECT agent_id,
               substr(created_at, 1, 10) AS day,
               COUNT(*) AS signal_count,
               AVG(confidence) AS avg_confidence,
               SUM(CASE WHEN direction = 'long' THEN 1 ELSE 0 END) AS long_n,
               SUM(CASE WHEN direction = 'short' THEN 1 ELSE 0 END) AS short_n,
               SUM(CASE WHEN direction = 'neutral' THEN 1 ELSE 0 END) AS neutral_n
        FROM signals
        WHERE substr(created_at, 1, 10) >= ?
        GROUP BY agent_id, day
        ORDER BY day ASC, agent_id ASC
        """,
        (start,),
    ).fetchall()

    out: dict[str, list[dict[str, Any]]] = {a: [] for a in DEFAULT_AGENT_IDS}
    for r in rows:
        aid = r["agent_id"] or "unknown"
        out.setdefault(aid, [])
        out[aid].append(
            {
                "date": r["day"],
                "signal_count": int(r["signal_count"] or 0),
                "avg_confidence": float(r["avg_confidence"] or 0.0),
                "long": int(r["long_n"] or 0),
                "short": int(r["short_n"] or 0),
                "neutral": int(r["neutral_n"] or 0),
            }
        )
    return out


def _decisions_today(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = {
        "tickets": [],
        "holds": [],
        "fills": [],
        "order_submitted": [],
        "failed": [],
        "other": [],
    }
    for d in decisions:
        dt = (d.get("decision_type") or "").lower()
        entry = {
            "id": d.get("id"),
            "decision_type": d.get("decision_type"),
            "actor": d.get("actor"),
            "ticker": d.get("ticker"),
            "reasoning": d.get("reasoning"),
            "payload": d.get("payload") if isinstance(d.get("payload"), dict) else {},
            "created_at": d.get("created_at"),
        }
        if dt in ("ticket",):
            buckets["tickets"].append(entry)
        elif dt in ("hold",):
            buckets["holds"].append(entry)
        elif dt in ("fill",):
            buckets["fills"].append(entry)
        elif dt in ("alpaca_order", "order_submitted", "order"):
            buckets["order_submitted"].append(entry)
        elif dt in ("failed", "order_failed", "error") or "fail" in dt:
            buckets["failed"].append(entry)
        else:
            # also catch failed via payload status
            payload = entry["payload"] or {}
            status = str(payload.get("status") or "").lower()
            if "fail" in status or status == "rejected":
                buckets["failed"].append(entry)
            else:
                buckets["other"].append(entry)
    return {
        "tickets": buckets["tickets"],
        "holds": buckets["holds"],
        "fills": buckets["fills"],
        "order_submitted": buckets["order_submitted"],
        "failed": buckets["failed"],
        "counts": {
            "tickets": len(buckets["tickets"]),
            "holds": len(buckets["holds"]),
            "fills": len(buckets["fills"]),
            "order_submitted": len(buckets["order_submitted"]),
            "failed": len(buckets["failed"]),
            "other": len(buckets["other"]),
        },
    }


def _mission_log(decisions: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    for d in decisions[:limit]:
        log.append(
            {
                "id": d.get("id"),
                "at": d.get("created_at"),
                "type": d.get("decision_type"),
                "actor": d.get("actor"),
                "ticker": d.get("ticker"),
                "summary": (d.get("reasoning") or "")[:280],
            }
        )
    return log


def _watchlist(
    universe_tickers: list[str],
    held: set[str],
    sector_of: dict[str, str],
    signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # latest signal per ticker across agents (highest confidence non-neutral preferred)
    latest: dict[str, dict[str, Any]] = {}
    for s in signals:  # already newest-first
        t = s["ticker"].upper()
        if t in latest:
            continue
        latest[t] = s
    # also track top agent by confidence for each ticker
    top: dict[str, dict[str, Any]] = {}
    for s in signals:
        t = s["ticker"].upper()
        cur = top.get(t)
        if cur is None or float(s["confidence"]) > float(cur["confidence"]):
            top[t] = s

    out: list[dict[str, Any]] = []
    for t in universe_tickers:
        if t in held:
            continue
        sig = top.get(t) or latest.get(t)
        item: dict[str, Any] = {
            "ticker": t,
            "sector": sector_of.get(t),
            "signal": None,
        }
        if sig:
            item["signal"] = {
                "agent_id": sig.get("agent_id"),
                "direction": sig.get("direction"),
                "confidence": float(sig.get("confidence") or 0),
                "reasoning": (sig.get("reasoning") or "")[:200],
                "created_at": sig.get("created_at"),
            }
        out.append(item)
    return out


def _equity_curve(tracker: Tracker, limit: int = 500) -> list[dict[str, Any]]:
    rows = tracker.conn.execute(
        """
        SELECT equity, cash, daily_pnl, drawdown, peak_equity, created_at
        FROM portfolio_snapshots
        ORDER BY id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "t": r["created_at"],
            "equity": float(r["equity"]),
            "cash": float(r["cash"]),
            "daily_pnl": float(r["daily_pnl"]) if r["daily_pnl"] is not None else None,
            "drawdown": float(r["drawdown"]) if r["drawdown"] is not None else None,
            "peak_equity": float(r["peak_equity"]) if r["peak_equity"] is not None else None,
        }
        for r in rows
    ]


def _optional_alpaca_snapshot() -> dict[str, Any] | None:
    """Best-effort public account numbers only — never keys."""
    try:
        from quant_system.config.loader import alpaca_credentials
        from quant_system.execution.broker import fetch_account_snapshot

        creds = alpaca_credentials()
        if not creds.get("configured"):
            return None
        snap = fetch_account_snapshot()
        return {
            "cash": snap.get("cash"),
            "equity": snap.get("equity"),
            "buying_power": snap.get("buying_power"),
            "open_orders_count": snap.get("open_orders_count"),
            "positions": [
                {
                    "ticker": p.get("ticker") or p.get("symbol"),
                    "qty": p.get("qty"),
                    "avg_cost": p.get("avg_cost") or p.get("avg_entry_price"),
                    "market_value": p.get("market_value"),
                    "unrealized_pl": p.get("unrealized_pl") or p.get("unrealized_plpc"),
                }
                for p in (snap.get("positions") or [])
            ],
            "paper": True,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "paper": True}


def build_dashboard_payload(
    tracker: Tracker,
    *,
    include_alpaca: bool = True,
    account: str = "paper",
) -> dict[str, Any]:
    cfg = load_config()
    themes = {k: [t.upper() for t in v] for k, v in cfg.universe.themes.items()}
    sector_of = _sector_map(themes)
    day_start = _utc_day_start()

    equity = float(tracker.get_state("equity", str(cfg.risk.starting_equity)))
    cash = float(tracker.get_state("cash", str(cfg.risk.starting_equity)))
    peak = float(tracker.get_state("peak_equity", str(cfg.risk.starting_equity)))
    day_start_eq = float(tracker.get_state("day_start_equity", str(equity)))
    snap = tracker.latest_snapshot()
    daily_pnl = float(snap["daily_pnl"]) if snap and snap.get("daily_pnl") is not None else equity - day_start_eq
    drawdown = float(snap["drawdown"]) if snap and snap.get("drawdown") is not None else (
        (peak - equity) / peak if peak else 0.0
    )

    positions = tracker.list_positions()
    held = {str(p["ticker"]).upper() for p in positions}
    holdings: list[dict[str, Any]] = []
    for p in positions:
        t = str(p["ticker"]).upper()
        qty = float(p["qty"])
        avg = float(p["avg_cost"])
        holdings.append(
            {
                "ticker": t,
                "qty": qty,
                "avg_cost": avg,
                "last": None,
                "sector": sector_of.get(t),
                "unrealized": None,
                "updated_at": p.get("updated_at"),
            }
        )

    alpaca = _optional_alpaca_snapshot() if include_alpaca else None
    # enrich holdings from alpaca if present
    if alpaca and isinstance(alpaca.get("positions"), list):
        by_t = {str(x.get("ticker") or "").upper(): x for x in alpaca["positions"]}
        for h in holdings:
            a = by_t.get(h["ticker"])
            if not a:
                continue
            if a.get("avg_cost") is not None:
                h["avg_cost"] = float(a["avg_cost"])
            if a.get("unrealized_pl") is not None:
                try:
                    h["unrealized"] = float(a["unrealized_pl"])
                except (TypeError, ValueError):
                    pass
            if a.get("market_value") is not None and h["qty"]:
                try:
                    h["last"] = float(a["market_value"]) / float(h["qty"])
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

    holdings = _enrich_holdings(tracker, holdings)

    signals = _query_since(tracker, "signals", day_start)
    decisions = _query_since(tracker, "decisions", day_start)
    flags = [
        {
            "id": f["id"],
            "flag_type": f["flag_type"],
            "severity": f["severity"],
            "message": f["message"],
            "related_table": f.get("related_table"),
            "related_id": f.get("related_id"),
            "created_at": f["created_at"],
        }
        for f in tracker.list_audit_flags(limit=200)
        if str(f.get("created_at") or "") >= day_start
    ]

    payload: dict[str, Any] = {
        "meta": {
            "exported_at": _now(),
            "account": account,
            "starting_equity": float(cfg.risk.starting_equity),
            "paper": True,
            "day_start_utc": day_start,
        },
        "snapshot": {
            "equity": equity,
            "cash": cash,
            "peak_equity": peak,
            "daily_pnl": daily_pnl,
            "drawdown": drawdown,
            "halted": tracker.is_halted(),
            "day_halted": tracker.is_day_halted(),
            "halt_reason": tracker.get_state("halt_reason", "") or None,
        },
        "holdings": holdings,
        "closed": _build_closed(tracker, sector_of),
        "equity_curve": _equity_curve(tracker),
        "agents_today": _agents_today(signals),
        "agents_history": _agents_history(tracker, days=30),
        "decisions_today": _decisions_today(decisions),
        "auditor_flags_today": flags,
        "mission_log": _mission_log(decisions),
        "watchlist": _watchlist(cfg.universe.tickers, held, sector_of, signals),
        "sectors": themes,
    }
    if alpaca is not None:
        # strip any accidental secret-looking keys
        safe = {k: v for k, v in alpaca.items() if "key" not in k.lower() and "secret" not in k.lower()}
        payload["alpaca_snapshot"] = safe
    return payload


def validate_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for k in REQUIRED_TOP_KEYS:
        if k not in payload:
            errors.append(f"missing top-level key: {k}")
    meta = payload.get("meta") or {}
    for k in ("exported_at", "account", "starting_equity", "paper"):
        if k not in meta:
            errors.append(f"meta missing: {k}")
    if meta.get("paper") is not True:
        errors.append("meta.paper must be true")
    snap = payload.get("snapshot") or {}
    for k in ("equity", "cash", "peak_equity", "daily_pnl", "drawdown", "halted", "day_halted"):
        if k not in snap:
            errors.append(f"snapshot missing: {k}")
    agents = payload.get("agents_today") or {}
    for aid in DEFAULT_AGENT_IDS:
        if aid not in agents:
            errors.append(f"agents_today missing agent: {aid}")
        else:
            block = agents[aid]
            if "signals" not in block or "count" not in block:
                errors.append(f"agents_today.{aid} incomplete")
    hist = payload.get("agents_history") or {}
    if not isinstance(hist, dict):
        errors.append("agents_history must be an object")
    else:
        for aid in DEFAULT_AGENT_IDS:
            if aid not in hist:
                errors.append(f"agents_history missing agent: {aid}")
            elif not isinstance(hist[aid], list):
                errors.append(f"agents_history.{aid} must be a list")
    closed = payload.get("closed")
    if not isinstance(closed, list):
        errors.append("closed must be a list")
    else:
        for i, c in enumerate(closed[:50]):
            if not isinstance(c, dict):
                errors.append(f"closed[{i}] must be an object")
                continue
            for req in ("ticker", "qty", "exit", "closed_at"):
                if req not in c:
                    errors.append(f"closed[{i}] missing {req}")
            # reasoning bundles are optional for empty books, but when present must be objects
            for side in ("buy", "sell"):
                if side in c and c[side] is not None and not isinstance(c[side], dict):
                    errors.append(f"closed[{i}].{side} must be an object")
    holdings = payload.get("holdings")
    if not isinstance(holdings, list):
        errors.append("holdings must be a list")
    else:
        for i, h in enumerate(holdings[:50]):
            if not isinstance(h, dict):
                errors.append(f"holdings[{i}] must be an object")
                continue
            if "buy" in h and h["buy"] is not None and not isinstance(h["buy"], dict):
                errors.append(f"holdings[{i}].buy must be an object")
    # secret scan (structured + string markers; never ship credentials)
    def _walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                if lk in {"api_key", "secret_key", "alpaca_api_key", "alpaca_secret_key", "apca_api_key_id", "apca_api_secret_key"}:
                    errors.append(f"forbidden key at {path}.{k}")
                _walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj[:50]):
                _walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            for bad in ("ALPACA_API_KEY=", "ALPACA_SECRET_KEY=", "APCA_API_SECRET_KEY="):
                if bad in obj:
                    errors.append(f"secret marker in string at {path}")
    _walk(payload)
    return errors


def export_dashboard(
    tracker: Tracker | None = None,
    *,
    docs_dir: Path | str | None = None,
    include_alpaca: bool = True,
    append_history: bool = True,
    close_tracker: bool = False,
) -> dict[str, Any]:
    """Write docs/data/latest.json (+ optional history/YYYY-MM-DD.json)."""
    own = tracker is None
    tr = tracker or Tracker()
    try:
        root = _repo_root()
        out_dir = Path(docs_dir) if docs_dir else root / "docs"
        data_dir = out_dir / "data"
        hist_dir = data_dir / "history"
        data_dir.mkdir(parents=True, exist_ok=True)
        hist_dir.mkdir(parents=True, exist_ok=True)

        payload = build_dashboard_payload(tr, include_alpaca=include_alpaca)
        errors = validate_payload(payload)
        if errors:
            raise ValueError("dashboard payload invalid: " + "; ".join(errors))

        latest_path = data_dir / "latest.json"
        latest_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")

        history_path = None
        if append_history:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            history_path = hist_dir / f"{day}.json"
            history_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")

        return {
            "latest": str(latest_path),
            "history": str(history_path) if history_path else None,
            "exported_at": payload["meta"]["exported_at"],
            "agents_today_counts": {
                k: v.get("count", 0) for k, v in payload["agents_today"].items()
            },
            "agents_history_days": {
                k: len(v) for k, v in (payload.get("agents_history") or {}).items()
            },
            "holdings": len(payload["holdings"]),
            "equity": payload["snapshot"]["equity"],
        }
    finally:
        if own or close_tracker:
            tr.close()
