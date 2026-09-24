"""Export Tracker SQLite into docs/data/latest.json for GitHub Pages viewer.

Frozen schema — keep keys stable; see docs/SCHEMA.md.
Never write Alpaca secrets into the payload.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    "decisions_today",
    "auditor_flags_today",
    "mission_log",
    "watchlist",
    "sectors",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


DASHBOARD_TZ = ZoneInfo("America/New_York")


def _as_utc(ts: datetime | None = None) -> datetime:
    """Normalize *ts* (or now) to aware UTC."""
    t = ts or datetime.now(timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def local_day_bounds(ts: datetime | None = None, tz: ZoneInfo | None = None) -> tuple[datetime, datetime, str]:
    """Return (day_start_utc, day_end_utc, local_date_YYYY-MM-DD) for *tz* calendar day.

    Boundaries are timezone-aware UTC datetimes corresponding to local midnight
    at the start of the local calendar day containing *ts*.
    """
    zone = tz or DASHBOARD_TZ
    local = _as_utc(ts).astimezone(zone)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta

    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return start_utc, end_utc, start_local.strftime("%Y-%m-%d")


def _local_day_start_utc_iso(ts: datetime | None = None) -> str:
    """ET local midnight for *ts*, expressed as a UTC ISO string for DB cutoffs."""
    start_utc, _, _ = local_day_bounds(ts)
    return start_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utc_day_start(ts: datetime | None = None) -> str:
    """Deprecated alias: now returns America/New_York local-day start in UTC ISO."""
    return _local_day_start_utc_iso(ts)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _summarize_supporting(data: Any, max_keys: int = 10) -> dict[str, Any] | str | None:
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
        "fundamentals_source",
        "coverage",
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


def _build_closed(tracker: Tracker, sector_of: dict[str, str]) -> list[dict[str, Any]]:
    """Pair sells with running avg entry from chronological lot ladder.

    Walks all fills (including ``alpaca_backfill`` buys) by created_at/id so
    historical backfills inserted later still attribute correctly. Entry/PnL
    stay null when no prior buy qty exists for that ticker.
    """
    fills = tracker.conn.execute(
        """
        SELECT * FROM fills
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    if not fills:
        return []

    # Per-ticker running position for cost basis (qty, avg_cost)
    books: dict[str, dict[str, float]] = {}
    closed: list[dict[str, Any]] = []
    for f in fills:
        ticker = str(f["ticker"]).upper()
        side = str(f["side"]).lower()
        qty = float(f["qty"])
        price = float(f["price"])
        book = books.setdefault(ticker, {"qty": 0.0, "avg_cost": 0.0})
        if side == "buy":
            old_qty = book["qty"]
            new_qty = old_qty + qty
            if new_qty > 1e-12:
                book["avg_cost"] = (
                    ((old_qty * book["avg_cost"]) + (qty * price)) / new_qty
                    if old_qty > 1e-12
                    else price
                )
            book["qty"] = new_qty
            continue
        if side != "sell":
            continue
        entry = book["avg_cost"] if book["qty"] > 1e-12 else None
        exit_px = price
        pnl = None if entry is None else (exit_px - entry) * qty
        closed.append(
            {
                "ticker": ticker,
                "qty": qty,
                "entry": entry,
                "exit": exit_px,
                "pnl": pnl,
                "sector": sector_of.get(ticker),
                "closed_at": f["created_at"],
                "mode": f["mode"],
            }
        )
        # Reduce open qty after realizing; keep avg_cost for residual
        new_qty = book["qty"] - qty
        if new_qty <= 1e-12:
            books[ticker] = {"qty": 0.0, "avg_cost": 0.0}
        else:
            book["qty"] = new_qty
    return closed


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



# ---------------------------------------------------------------------------
# Buy / sell zone aggregation (Orbit Zones)
# ---------------------------------------------------------------------------
# Per ticker, take the latest signal that day for each research agent.
# buy  = mean over agents of (confidence/100 if direction == long  else 0)
# sell = mean over agents of (confidence/100 if direction == short else 0)
# Both axes are independent and in [0, 1]. Neutral / missing contribute 0.
# Zone labels (thresholds tuned so a typical book is mixed buy/quiet):
#   buy_zone   if buy  >= BUY_ZONE_MIN  and sell < SELL_QUIET_MAX
#   sell_zone  if sell >= SELL_ZONE_MIN and buy  < BUY_QUIET_MAX
#   conflicted if buy  >= BUY_ZONE_MIN  and sell >= SELL_ZONE_MIN
#   quiet      otherwise

BUY_ZONE_MIN = 0.35
SELL_ZONE_MIN = 0.35
BUY_QUIET_MAX = 0.25
SELL_QUIET_MAX = 0.25
ZONE_SERIES_IN_LATEST = 20  # compact day count embedded in latest.json


def latest_signals_by_agent_ticker(agents_today: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Return {ticker: {agent_id: signal}} using the newest signal per (agent, ticker).

    `agents_today` signals are assumed newest-first (export query is ORDER BY id DESC).
    """
    by_ticker: dict[str, dict[str, dict[str, Any]]] = {}
    for aid, block in (agents_today or {}).items():
        agent_id = (block.get("agent_id") if isinstance(block, dict) else None) or aid
        signals = (block.get("signals") if isinstance(block, dict) else None) or []
        for s in signals:
            if not isinstance(s, dict):
                continue
            ticker = str(s.get("ticker") or "").upper()
            if not ticker:
                continue
            slot = by_ticker.setdefault(ticker, {})
            if agent_id in slot:
                continue  # already have newer
            slot[agent_id] = s
    return by_ticker


def compute_zone_pressures(agent_signals: dict[str, dict[str, Any]]) -> tuple[float, float, float]:
    """Compute (buy, sell, net) from a map of agent_id → signal dict.

    Each agent contributes confidence/100 when direction matches; otherwise 0.
    Denominator is the number of agents that fired for this ticker (len of map).
    Empty map → (0.0, 0.0, 0.0).
    """
    if not agent_signals:
        return 0.0, 0.0, 0.0
    n = len(agent_signals)
    buy_sum = 0.0
    sell_sum = 0.0
    for s in agent_signals.values():
        conf = float(s.get("confidence") or 0.0) / 100.0
        if conf < 0:
            conf = 0.0
        direction = str(s.get("direction") or "").lower()
        if direction == "long":
            buy_sum += conf
        elif direction == "short":
            sell_sum += conf
    buy = buy_sum / n
    sell = sell_sum / n
    return buy, sell, buy - sell


def zone_label(buy: float, sell: float) -> str:
    """Map buy/sell pressures to a zone label."""
    if buy >= BUY_ZONE_MIN and sell < SELL_QUIET_MAX:
        return "buy_zone"
    if sell >= SELL_ZONE_MIN and buy < BUY_QUIET_MAX:
        return "sell_zone"
    if buy >= BUY_ZONE_MIN and sell >= SELL_ZONE_MIN:
        return "conflicted"
    return "quiet"


def _agent_breakdown(agent_signals: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for aid in sorted(agent_signals.keys()):
        s = agent_signals[aid]
        rows.append(
            {
                "agent_id": aid,
                "direction": str(s.get("direction") or "neutral"),
                "confidence": float(s.get("confidence") or 0.0),
            }
        )
    return rows


def build_zone_points(
    agents_today: dict[str, Any],
    *,
    held: set[str] | None = None,
    sector_of: dict[str, str] | None = None,
    holdings: list[dict[str, Any]] | None = None,
    compact: bool = False,
) -> list[dict[str, Any]]:
    """Build zone points for one day's agents_today.

    When *compact* is True, omit agents breakdown and weight (for zones_history).
    """
    held = {str(t).upper() for t in (held or set())}
    sector_of = sector_of or {}
    weight_of: dict[str, float] = {}
    if holdings:
        # Approximate weight by market value share when last is known; else by cost basis.
        vals: dict[str, float] = {}
        for h in holdings:
            t = str(h.get("ticker") or "").upper()
            qty = float(h.get("qty") or 0)
            last = h.get("last")
            avg = h.get("avg_cost")
            if last is not None:
                vals[t] = abs(qty * float(last))
            elif avg is not None:
                vals[t] = abs(qty * float(avg))
        total = sum(vals.values()) or 0.0
        if total > 0:
            weight_of = {t: v / total for t, v in vals.items()}

    points: list[dict[str, Any]] = []
    for ticker, agent_map in sorted(latest_signals_by_agent_ticker(agents_today).items()):
        buy, sell, net = compute_zone_pressures(agent_map)
        z = zone_label(buy, sell)
        is_held = ticker in held
        if compact:
            points.append(
                {
                    "ticker": ticker,
                    "buy": round(buy, 6),
                    "sell": round(sell, 6),
                    "zone": z,
                    "held": is_held,
                }
            )
        else:
            pt: dict[str, Any] = {
                "ticker": ticker,
                "sector": sector_of.get(ticker),
                "held": is_held,
                "buy": round(buy, 6),
                "sell": round(sell, 6),
                "net": round(net, 6),
                "zone": z,
                "agents": _agent_breakdown(agent_map),
            }
            if is_held and ticker in weight_of:
                pt["weight"] = round(weight_of[ticker], 6)
            points.append(pt)
    return points


def build_zones_block(
    agents_today: dict[str, Any],
    *,
    as_of: str,
    day_local: str,
    held: set[str] | None = None,
    sector_of: dict[str, str] | None = None,
    holdings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "as_of": as_of,
        "day_local": day_local,
        "thresholds": {
            "buy_zone_min": BUY_ZONE_MIN,
            "sell_zone_min": SELL_ZONE_MIN,
            "buy_quiet_max": BUY_QUIET_MAX,
            "sell_quiet_max": SELL_QUIET_MAX,
        },
        "points": build_zone_points(
            agents_today,
            held=held,
            sector_of=sector_of,
            holdings=holdings,
            compact=False,
        ),
    }


def _held_from_payload(payload: dict[str, Any]) -> set[str]:
    return {str(h.get("ticker") or "").upper() for h in (payload.get("holdings") or []) if h.get("ticker")}


def _sector_of_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    sectors = payload.get("sectors") or {}
    out: dict[str, str] = {}
    for theme, tickers in sectors.items():
        for t in tickers or []:
            out[str(t).upper()] = str(theme)
    return out


def rebuild_zones_history(
    hist_dir: Path,
    *,
    today_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scan history/*.json (+ optional today payload) and rebuild zones_history.

    Returns `{ days: [{day, points: [compact...]}] }` sorted ascending by day.
    """
    by_day: dict[str, list[dict[str, Any]]] = {}

    if hist_dir.is_dir():
        for path in sorted(hist_dir.glob("*.json")):
            try:
                day = path.stem
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            agents = payload.get("agents_today") or {}
            if not agents:
                continue
            held = _held_from_payload(payload)
            points = build_zone_points(agents, held=held, compact=True)
            by_day[day] = points

    if today_payload:
        day = (today_payload.get("meta") or {}).get("day_local")
        agents = today_payload.get("agents_today") or {}
        if day and agents:
            held = _held_from_payload(today_payload)
            by_day[str(day)] = build_zone_points(agents, held=held, compact=True)

    days = [{"day": d, "points": by_day[d]} for d in sorted(by_day.keys())]
    return {"days": days}


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
                    "current_price": p.get("current_price"),
                    # Dollar P/L only — do not fall back to unrealized_plpc (percent).
                    "unrealized_pl": p.get("unrealized_pl"),
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
    themes = {
        k: [t.upper() for t in (v.active if hasattr(v, "active") else v)]
        for k, v in cfg.universe.themes.items()
    }
    sector_of = _sector_map(themes)
    day_start_utc_dt, _, local_day = local_day_bounds()
    day_start = day_start_utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

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
            # Prefer current_price for last; else derive from market_value/qty.
            if a.get("current_price") is not None:
                try:
                    h["last"] = float(a["current_price"])
                except (TypeError, ValueError):
                    pass
            elif a.get("market_value") is not None and h["qty"]:
                try:
                    h["last"] = float(a["market_value"]) / float(h["qty"])
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

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
            # day_start_utc is ET local midnight converted to UTC (compat key name).
            "day_start_utc": day_start,
            "day_timezone": "America/New_York",
            "day_local": local_day,
            "day_label": "ET day",
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
    # Additive cold/active rotation (optional for older consumers)
    try:
        from quant_system.research.rotation import rotation_digest_section

        rot = rotation_digest_section(tracker, cfg)
        payload["rotation"] = rot
        payload["cold"] = rot.get("cold", [])
    except Exception:  # noqa: BLE001
        payload["rotation"] = {"active": {}, "cold": [], "settings": {}}
        payload["cold"] = []

    # Additive Orbit Zones (buy/sell pressure map)
    held_tickers = {str(h["ticker"]).upper() for h in holdings}
    payload["zones"] = build_zones_block(
        payload["agents_today"],
        as_of=payload["meta"]["exported_at"],
        day_local=local_day,
        held=held_tickers,
        sector_of=sector_of,
        holdings=holdings,
    )
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
            _, _, day = local_day_bounds()
            history_path = hist_dir / f"{day}.json"
            history_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")

        # Rebuild zones_history from history/*.json + today's payload
        zones_hist = rebuild_zones_history(hist_dir, today_payload=payload)
        zones_hist_path = data_dir / "zones_history.json"
        zones_hist_path.write_text(json.dumps(zones_hist, indent=2, default=str) + "\n")

        # Embed a compact recent series in latest for chart depth without a second fetch
        recent_days = zones_hist.get("days") or []
        if len(recent_days) > ZONE_SERIES_IN_LATEST:
            recent_days = recent_days[-ZONE_SERIES_IN_LATEST:]
        payload["zones_series"] = {"days": recent_days}
        # Rewrite latest with zones_series included
        latest_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        if history_path is not None:
            history_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")

        return {
            "latest": str(latest_path),
            "history": str(history_path) if history_path else None,
            "zones_history": str(zones_hist_path),
            "exported_at": payload["meta"]["exported_at"],
            "agents_today_counts": {
                k: v.get("count", 0) for k, v in payload["agents_today"].items()
            },
            "holdings": len(payload["holdings"]),
            "equity": payload["snapshot"]["equity"],
            "zones_points": len((payload.get("zones") or {}).get("points") or []),
            "zones_history_days": len(zones_hist.get("days") or []),
        }
    finally:
        if own or close_tracker:
            tr.close()
