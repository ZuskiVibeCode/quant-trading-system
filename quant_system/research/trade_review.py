"""High-signal trade reviews for closed lots — fact-tied, evidence-backed.

Built at export time from fills / tickets / signals. No schema migration.
Never invent agent motives or market stories; missing facts → null/unknown/thin.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from quant_system.tracker.api import Tracker

REASONING_TRUNC = 160
RATIONALE_TRUNC = 180

_WS_RE = re.compile(
    r"weighted_score\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
)
_BC_RE = re.compile(
    r"blended_conf(?:idence)?\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _trunc(text: str | None, n: int) -> str:
    if not text:
        return ""
    t = str(text).strip()
    if len(t) <= n:
        return t
    return t[: n - 1].rstrip() + "…"


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


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
            return _parse_signal_ids(json.loads(raw))
        except json.JSONDecodeError:
            return []
    return []


def classify_exit_reason(ticket_rationale: str | None) -> str:
    """Bucket exit rationale into a stable class string."""
    if not ticket_rationale or not str(ticket_rationale).strip():
        return "unknown"
    r = str(ticket_rationale).lower()
    if "cash buffer" in r or "cash_buffer" in r:
        return "cash_buffer_trim"
    if "opportunity rotate" in r or "opportunity_rotate" in r:
        return "opportunity_rotate"
    if "reduce risk" in r or "weighted_score=" in r:
        # Signal-driven sells use weighted_score + reduce risk (or short open).
        if r.strip().startswith("short "):
            return "other"
        return "signal_sell"
    if r.strip().startswith("sell ") and "weighted_score" in r:
        return "signal_sell"
    if "alpaca_backfill" in r:
        return "other"
    return "other"


def signals_near_entry(
    tracker: Tracker,
    ticker: str,
    entry_at: str | None,
    window_hours: float = 6.0,
    signal_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Latest signal per agent_id at/before entry (prefer explicit ticket signal_ids).

    Returns list of ``{agent_id, direction, confidence, reasoning_trunc, signal_id, created_at}``.
    """
    t = (ticker or "").upper()
    if not t:
        return []

    # Prefer exact signal ids from the entry ticket when present.
    if signal_ids:
        rows: list[dict[str, Any]] = []
        for sid in signal_ids:
            row = tracker.conn.execute(
                "SELECT * FROM signals WHERE id = ?", (int(sid),)
            ).fetchone()
            if not row:
                continue
            d = tracker._row_to_dict(row, json_fields=("supporting_data",))
            rows.append(d)
        # Dedupe by agent_id keeping first (ticket order)
        by_agent: dict[str, dict[str, Any]] = {}
        for d in rows:
            aid = str(d.get("agent_id") or "")
            if aid and aid not in by_agent:
                by_agent[aid] = d
        return [_signal_brief(d) for d in by_agent.values()]

    entry_dt = _parse_ts(entry_at)
    if entry_dt is None:
        return []

    # Window: look back window_hours before entry; take latest per agent at/before entry.
    since = (entry_dt - timedelta(hours=float(window_hours))).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    until = entry_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = tracker.conn.execute(
        """
        SELECT * FROM signals
        WHERE ticker = ? AND created_at >= ? AND created_at <= ?
        ORDER BY created_at DESC, id DESC
        """,
        (t, since, until),
    ).fetchall()
    by_agent: dict[str, dict[str, Any]] = {}
    for row in rows:
        d = tracker._row_to_dict(row, json_fields=("supporting_data",))
        aid = str(d.get("agent_id") or "")
        if aid and aid not in by_agent:
            by_agent[aid] = d
    return [_signal_brief(d) for d in by_agent.values()]


def _signal_brief(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_id": d.get("agent_id"),
        "direction": d.get("direction"),
        "confidence": _safe_float(d.get("confidence")),
        "reasoning_trunc": _trunc(d.get("reasoning"), REASONING_TRUNC),
        "signal_id": d.get("id"),
        "created_at": d.get("created_at"),
    }


def agent_mix_summary(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Leaders, direction buckets, and disagreement flag from entry signals."""
    long_a: list[str] = []
    short_a: list[str] = []
    neutral_a: list[str] = []
    lead_agent: str | None = None
    lead_conf = -1.0
    for s in signals or []:
        aid = str(s.get("agent_id") or "")
        direction = str(s.get("direction") or "neutral").lower()
        conf = _safe_float(s.get("confidence")) or 0.0
        if direction == "long":
            long_a.append(aid)
        elif direction == "short":
            short_a.append(aid)
        else:
            neutral_a.append(aid)
        if conf > lead_conf and direction in ("long", "short"):
            lead_conf = conf
            lead_agent = aid
    disagreement = bool(long_a) and bool(short_a)
    return {
        "long": long_a,
        "short": short_a,
        "neutral": neutral_a,
        "disagreement": disagreement,
        "lead_agent": lead_agent,
        "lead_confidence": None if lead_agent is None else lead_conf,
    }


def parse_score_from_rationale(rationale: str | None) -> dict[str, float] | None:
    """Pull weighted_score / blended_conf from ticket rationale text when present."""
    if not rationale:
        return None
    ws = _WS_RE.search(rationale)
    bc = _BC_RE.search(rationale)
    if not ws and not bc:
        return None
    out: dict[str, float] = {}
    if ws:
        out["weighted_score"] = float(ws.group(1))
    if bc:
        out["blended_conf"] = float(bc.group(1))
    return out or None


def recompute_entry_score(
    signals: list[dict[str, Any]],
    weights: dict[str, float] | None,
    *,
    momentum_max_abs_share: float = 0.40,
) -> dict[str, float] | None:
    """Recompute weighted_score / blended_conf from signals + agent weights.

    Applies the same Momentum absolute-share cap as PortfolioManager
    (``momentum_max_abs_share``, default 0.40).
    """
    if not signals:
        return None
    from quant_system.execution.blend_score import apply_momentum_abs_share_cap

    wmap = weights or {}
    conf_acc = 0.0
    weight_sum = 0.0
    detail: list[dict[str, Any]] = []
    for s in signals:
        aid = str(s.get("agent_id") or "")
        w = float(wmap.get(aid, 0.0) or 0.0)
        if w <= 0:
            continue
        direction = str(s.get("direction") or "neutral").lower()
        direction_val = {"long": 1.0, "short": -1.0, "neutral": 0.0}.get(direction, 0.0)
        conf = _safe_float(s.get("confidence")) or 0.0
        contrib = direction_val * (conf / 100.0) * w
        conf_acc += conf * w
        weight_sum += w
        detail.append(
            {
                "agent_id": aid,
                "direction": direction,
                "confidence": conf,
                "weight": w,
                "contrib": contrib,
            }
        )
    if weight_sum <= 0:
        return None
    score, _detail, _meta = apply_momentum_abs_share_cap(
        detail, max_share=float(momentum_max_abs_share)
    )
    return {
        "weighted_score": round(score, 6),
        "blended_conf": round(conf_acc / weight_sum, 3),
    }


def price_path_stats(
    market_data: Any | None,
    ticker: str,
    entry_at: str | None,
    exit_at: str | None,
    entry_px: float | None,
    exit_px: float | None,
) -> dict[str, Any]:
    """Hold hours, return %, optional MFE/MAE from daily bars between dates.

    Network optional — returns nulls for mfe/mae when offline or bars unavailable.
    """
    entry_dt = _parse_ts(entry_at)
    exit_dt = _parse_ts(exit_at)
    hold_hours: float | None = None
    if entry_dt and exit_dt:
        hold_hours = round((exit_dt - entry_dt).total_seconds() / 3600.0, 3)

    ret_pct: float | None = None
    ep = _safe_float(entry_px)
    xp = _safe_float(exit_px)
    if ep is not None and xp is not None and ep != 0:
        ret_pct = round(((xp - ep) / ep) * 100.0, 4)

    mfe_pct: float | None = None
    mae_pct: float | None = None
    path_source: str | None = None

    if market_data is not None and ep is not None and ep > 0 and entry_dt and exit_dt:
        try:
            df = market_data.history(ticker.upper(), period="3mo", interval="1d")
            if df is not None and not getattr(df, "empty", True):
                # Normalize index to dates for window slice
                idx = df.index
                highs = df["High"] if "High" in df.columns else None
                lows = df["Low"] if "Low" in df.columns else None
                if highs is not None and lows is not None:
                    entry_d = entry_dt.date()
                    exit_d = exit_dt.date()
                    mask = []
                    for i in idx:
                        try:
                            d = i.date() if hasattr(i, "date") else i
                        except Exception:  # noqa: BLE001
                            d = None
                        mask.append(d is not None and entry_d <= d <= exit_d)
                    if any(mask):
                        hi = float(highs[mask].max())
                        lo = float(lows[mask].min())
                        mfe_pct = round(((hi - ep) / ep) * 100.0, 4)
                        mae_pct = round(((lo - ep) / ep) * 100.0, 4)
                        path_source = "daily_bars"
        except Exception:  # noqa: BLE001 — offline-tolerant
            mfe_pct = None
            mae_pct = None
            path_source = None

    return {
        "hold_hours": hold_hours,
        "ret_pct": ret_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "path_source": path_source,
    }


def _evidence_item(
    kind: str,
    *,
    id: int | None = None,
    label: str,
    detail: str,
) -> dict[str, Any]:
    return {"kind": kind, "id": id, "label": label, "detail": detail}


def _build_evidence(
    *,
    entry_fill_id: int | None,
    exit_fill_id: int | None,
    entry_ticket: dict[str, Any] | None,
    exit_ticket: dict[str, Any] | None,
    agents: list[dict[str, Any]],
    entry_px: float | None,
    exit_px: float | None,
    qty: float | None,
    path: dict[str, Any],
    entry_score: dict[str, float] | None,
    completeness: str,
) -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = []

    if entry_fill_id is not None:
        ev.append(
            _evidence_item(
                "fill",
                id=int(entry_fill_id),
                label="entry fill",
                detail=(
                    f"buy qty={qty if qty is not None else '?'} "
                    f"@ {entry_px if entry_px is not None else '?'}"
                ),
            )
        )
    else:
        ev.append(
            _evidence_item(
                "fill",
                id=None,
                label="entry fill",
                detail="missing (no attributable buy fill for this lot)",
            )
        )

    if exit_fill_id is not None:
        ev.append(
            _evidence_item(
                "fill",
                id=int(exit_fill_id),
                label="exit fill",
                detail=f"sell @ {exit_px if exit_px is not None else '?'}",
            )
        )
    else:
        ev.append(
            _evidence_item(
                "fill",
                id=None,
                label="exit fill",
                detail="missing",
            )
        )

    if entry_ticket and entry_ticket.get("id") is not None:
        ev.append(
            _evidence_item(
                "ticket",
                id=int(entry_ticket["id"]),
                label="entry ticket",
                detail=_trunc(entry_ticket.get("rationale"), RATIONALE_TRUNC)
                or "no rationale",
            )
        )
    else:
        ev.append(
            _evidence_item(
                "ticket",
                id=None,
                label="entry ticket",
                detail="missing (backfill or unlinked buy)",
            )
        )

    if exit_ticket and exit_ticket.get("id") is not None:
        ev.append(
            _evidence_item(
                "ticket",
                id=int(exit_ticket["id"]),
                label="exit ticket",
                detail=_trunc(exit_ticket.get("rationale"), RATIONALE_TRUNC)
                or "no rationale",
            )
        )
    else:
        ev.append(
            _evidence_item(
                "ticket",
                id=None,
                label="exit ticket",
                detail="missing",
            )
        )

    if agents:
        for s in agents:
            sid = s.get("signal_id")
            aid = s.get("agent_id") or "?"
            direction = s.get("direction") or "?"
            conf = s.get("confidence")
            conf_s = f"{conf:.1f}" if isinstance(conf, (int, float)) else "?"
            ev.append(
                _evidence_item(
                    "signal",
                    id=int(sid) if sid is not None else None,
                    label=f"{aid} @ entry",
                    detail=f"{direction} @ conf={conf_s}",
                )
            )
    else:
        ev.append(
            _evidence_item(
                "signal",
                id=None,
                label="entry signals",
                detail="absent — completeness thin/backfill; mix claims unavailable",
            )
        )

    if entry_score:
        parts = []
        if "weighted_score" in entry_score:
            parts.append(f"weighted_score={entry_score['weighted_score']}")
        if "blended_conf" in entry_score:
            parts.append(f"blended_conf={entry_score['blended_conf']}")
        ev.append(
            _evidence_item(
                "score",
                id=None,
                label="entry score",
                detail="; ".join(parts) if parts else "present but empty",
            )
        )

    if path.get("path_source") and (
        path.get("mfe_pct") is not None or path.get("mae_pct") is not None
    ):
        ev.append(
            _evidence_item(
                "path",
                id=None,
                label="path stats",
                detail=(
                    f"source={path.get('path_source')}; "
                    f"mfe_pct={path.get('mfe_pct')}; mae_pct={path.get('mae_pct')}; "
                    f"ret_pct={path.get('ret_pct')}"
                ),
            )
        )
    elif completeness != "thin":
        # Explicit null path note when we tried but have no bars
        if path.get("ret_pct") is not None:
            ev.append(
                _evidence_item(
                    "path",
                    id=None,
                    label="path stats",
                    detail=(
                        f"ret_pct={path.get('ret_pct')}; "
                        f"mfe/mae unknown (no daily bars)"
                    ),
                )
            )

    return ev


def _verdict_and_tags(
    *,
    pnl: float | None,
    exit_reason: str,
    mix: dict[str, Any],
    agents: list[dict[str, Any]],
    entry_score: dict[str, float] | None,
    path: dict[str, Any],
    completeness: str,
    evidence: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    tags: list[str] = []

    if pnl is None:
        outcome = "unknown_pnl"
    elif abs(pnl) < 1e-6:
        outcome = "scratch"
        tags.append("scratch")
    elif pnl > 0:
        outcome = "winner"
        tags.append("winner")
    else:
        outcome = "loser"
        tags.append("loser")

    if completeness == "thin":
        tags.append("thin_data")
        parts = ["Thin review: missing entry signals (backfill fill); PnL and exit only."]
        if pnl is not None:
            parts.append(f"Realized PnL={pnl:.4f}.")
        if exit_reason and exit_reason != "unknown":
            parts.append(f"Exit class={exit_reason}.")
        return " ".join(parts), tags

    if mix.get("disagreement"):
        tags.append("split_book")
    else:
        tags.append("aligned")

    if exit_reason == "cash_buffer_trim":
        tags.append("forced_exit")
    elif exit_reason == "opportunity_rotate":
        tags.append("forced_exit")
        tags.append("rotate_exit")
    elif exit_reason == "signal_sell":
        tags.append("signal_exit")
    else:
        tags.append("other_exit")

    # Cite concrete evidence ids when present
    lead = mix.get("lead_agent")
    lead_sig = next(
        (e for e in evidence if e.get("kind") == "signal" and lead and lead in str(e.get("label"))),
        None,
    )
    lead_cite = ""
    if lead_sig and lead_sig.get("id") is not None:
        lead_cite = f" (see {lead} signal #{lead_sig['id']} {lead_sig.get('detail', '')})"
    elif lead:
        lead_cite = f" (lead={lead})"

    long_s = ", ".join(mix.get("long") or []) or "none"
    short_s = ", ".join(mix.get("short") or []) or "none"

    score_bit = ""
    if entry_score and "weighted_score" in entry_score:
        score_bit = f" Entry weighted_score={entry_score['weighted_score']}."

    hold = path.get("hold_hours")
    hold_bit = f" Held {hold:.1f}h." if hold is not None else ""

    pnl_bit = "PnL unknown."
    if pnl is not None:
        pnl_bit = f"Realized PnL={pnl:.4f}."

    exit_bit = {
        "cash_buffer_trim": "Exit was a cash-buffer trim (forced, not a conviction exit).",
        "opportunity_rotate": "Exit was an opportunity-rotate trim to fund another name.",
        "signal_sell": "Exit was a signal-driven sell (reduce risk).",
        "other": "Exit rationale classified as other.",
        "unknown": "Exit rationale unknown.",
    }.get(exit_reason, f"Exit class={exit_reason}.")

    if mix.get("disagreement"):
        split_bit = (
            f"Entered with a split book: long=[{long_s}] vs short=[{short_s}]"
            f"{lead_cite}."
        )
    elif lead:
        dirs = []
        for s in agents:
            if s.get("agent_id") == lead:
                dirs.append(
                    f"{s.get('direction')}@{s.get('confidence')}"
                )
        dir_s = dirs[0] if dirs else "unknown"
        split_bit = f"Entered aligned; lead agent {lead} was {dir_s}{lead_cite}."
    else:
        split_bit = "Entered with no directional agent leadership recorded."

    if outcome == "winner":
        head = f"Made money: {pnl_bit}"
    elif outcome == "loser":
        head = f"Lost money: {pnl_bit}"
    elif outcome == "scratch":
        head = f"Scratch: {pnl_bit}"
    else:
        head = f"Outcome unclear: {pnl_bit}"

    verdict = f"{head} {split_bit}{score_bit} {exit_bit}{hold_bit}".strip()
    # Collapse whitespace
    verdict = re.sub(r"\s+", " ", verdict)
    return verdict, tags


def build_round_review(
    *,
    ticker: str,
    qty: float,
    entry: float | None,
    exit_px: float | None,
    pnl: float | None,
    opened_at: str | None,
    closed_at: str | None,
    entry_fill_id: int | None = None,
    exit_fill_id: int | None = None,
    entry_ticket: dict[str, Any] | None = None,
    exit_ticket: dict[str, Any] | None = None,
    agents: list[dict[str, Any]] | None = None,
    weights: dict[str, float] | None = None,
    market_data: Any | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Assemble one closed-lot review dict with mandatory evidence list."""
    agents = agents or []
    exit_rationale = (exit_ticket or {}).get("rationale") if exit_ticket else None
    exit_reason = classify_exit_reason(exit_rationale)

    entry_rationale = (entry_ticket or {}).get("rationale") if entry_ticket else None
    entry_score = parse_score_from_rationale(entry_rationale)
    if entry_score is None and agents:
        entry_score = recompute_entry_score(agents, weights)

    mix = agent_mix_summary(agents)
    path = price_path_stats(
        market_data, ticker, opened_at, closed_at, entry, exit_px
    )

    # Completeness
    # full = entry ticket carried signal_ids we resolved; partial = window-only
    # signals or missing ticket link; thin = no signals (typical backfill-only).
    entry_signal_ids = _parse_signal_ids(
        (entry_ticket or {}).get("signal_ids") if entry_ticket else None
    )
    backfillish = bool(
        (mode and "backfill" in str(mode).lower())
        or (entry_rationale and "alpaca_backfill" in str(entry_rationale).lower())
        or (entry_ticket is not None and not entry_signal_ids)
    )
    if not agents:
        completeness = "thin"
    elif backfillish or entry_ticket is None or exit_ticket is None or not entry_signal_ids:
        completeness = "partial"
    else:
        completeness = "full"

    pnl_pct: float | None = None
    if entry is not None and entry != 0 and pnl is not None and qty:
        # Approximate pct on the closed lot notional
        pnl_pct = round((pnl / (abs(entry) * abs(qty))) * 100.0, 4) if qty else None

    evidence = _build_evidence(
        entry_fill_id=entry_fill_id,
        exit_fill_id=exit_fill_id,
        entry_ticket=entry_ticket,
        exit_ticket=exit_ticket,
        agents=agents,
        entry_px=entry,
        exit_px=exit_px,
        qty=qty,
        path=path,
        entry_score=entry_score,
        completeness=completeness,
    )

    verdict, tags = _verdict_and_tags(
        pnl=pnl,
        exit_reason=exit_reason,
        mix=mix,
        agents=agents,
        entry_score=entry_score,
        path=path,
        completeness=completeness,
        evidence=evidence,
    )

    return {
        "ticker": ticker.upper(),
        "qty": qty,
        "entry": entry,
        "exit": exit_px,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "hold_hours": path.get("hold_hours"),
        "exit_reason": exit_reason,
        "exit_rationale": _trunc(exit_rationale, RATIONALE_TRUNC) if exit_rationale else None,
        "entry_ticket_id": (entry_ticket or {}).get("id"),
        "exit_ticket_id": (exit_ticket or {}).get("id"),
        "entry_fill_id": entry_fill_id,
        "exit_fill_id": exit_fill_id,
        "entry_score": entry_score,
        "agents_at_entry": agents,
        "mix": mix,
        "path": {
            "mfe_pct": path.get("mfe_pct"),
            "mae_pct": path.get("mae_pct"),
            "ret_pct": path.get("ret_pct"),
        },
        "verdict": verdict,
        "verdict_tags": tags,
        "completeness": completeness,
        "evidence": evidence,
        "mode": mode,
    }


def _ticket_for_fill(tracker: Tracker, fill_row: Any) -> dict[str, Any] | None:
    tid = fill_row["ticket_id"] if fill_row is not None else None
    if tid is None:
        return None
    return tracker.get_ticket(int(tid))


def build_trade_reviews(
    tracker: Tracker,
    market_data: Any | None = None,
    sector_of: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Walk fills like ``_build_closed``; emit a review for each sell with known entry.

    Prefer linking the most recent buy ticket before that sell (or the buy that
    opened/refreshed the residual lot). Exit ticket rationale drives exit_reason.
    """
    sector_of = sector_of or {}
    try:
        weights = tracker.get_weights()
    except Exception:  # noqa: BLE001
        weights = {}

    fills = tracker.conn.execute(
        """
        SELECT * FROM fills
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    if not fills:
        return []

    # Per-ticker: qty, avg_cost, last_buy_fill, last_buy_ticket, lot_opened_at
    books: dict[str, dict[str, Any]] = {}
    reviews: list[dict[str, Any]] = []

    for f in fills:
        ticker = str(f["ticker"]).upper()
        side = str(f["side"]).lower()
        qty = float(f["qty"])
        price = float(f["price"])
        at = f["created_at"]
        mode = f["mode"]
        book = books.setdefault(
            ticker,
            {
                "qty": 0.0,
                "avg_cost": 0.0,
                "last_buy_fill_id": None,
                "last_buy_ticket": None,
                "lot_opened_at": None,
                "entry_mode": None,
            },
        )

        if side == "buy":
            old_qty = book["qty"]
            new_qty = old_qty + qty
            if new_qty > 1e-12:
                book["avg_cost"] = (
                    ((old_qty * book["avg_cost"]) + (qty * price)) / new_qty
                    if old_qty > 1e-12
                    else price
                )
            if old_qty <= 1e-12:
                book["lot_opened_at"] = at
            book["qty"] = new_qty
            book["last_buy_fill_id"] = int(f["id"])
            book["last_buy_ticket"] = _ticket_for_fill(tracker, f)
            book["entry_mode"] = mode
            continue

        if side != "sell":
            continue

        if book["qty"] <= 1e-12:
            # Orphan sell — skip review (matches ticker_pnl: no known entry)
            continue

        entry = book["avg_cost"]
        exit_px = price
        pnl = (exit_px - entry) * qty
        entry_ticket = book["last_buy_ticket"]
        exit_ticket = _ticket_for_fill(tracker, f)
        opened_at = book["lot_opened_at"]
        entry_fill_id = book["last_buy_fill_id"]
        exit_fill_id = int(f["id"])

        signal_ids = _parse_signal_ids(
            (entry_ticket or {}).get("signal_ids") if entry_ticket else None
        )
        agents = signals_near_entry(
            tracker,
            ticker,
            opened_at or at,
            window_hours=6.0,
            signal_ids=signal_ids or None,
        )

        review = build_round_review(
            ticker=ticker,
            qty=qty,
            entry=entry,
            exit_px=exit_px,
            pnl=pnl,
            opened_at=opened_at,
            closed_at=at,
            entry_fill_id=entry_fill_id,
            exit_fill_id=exit_fill_id,
            entry_ticket=entry_ticket,
            exit_ticket=exit_ticket,
            agents=agents,
            weights=weights,
            market_data=market_data,
            mode=mode,
        )
        review["sector"] = sector_of.get(ticker)
        reviews.append(review)

        new_qty = book["qty"] - qty
        if new_qty <= 1e-12:
            books[ticker] = {
                "qty": 0.0,
                "avg_cost": 0.0,
                "last_buy_fill_id": None,
                "last_buy_ticket": None,
                "lot_opened_at": None,
                "entry_mode": None,
            }
        else:
            book["qty"] = new_qty

    return reviews


def attach_reviews_to_closed(
    closed: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach matching review onto closed[] rows by ticker+closed_at+qty."""
    # Index reviews for greedy one-to-one match
    unused = list(reviews)
    out: list[dict[str, Any]] = []
    for c in closed:
        match = None
        for i, r in enumerate(unused):
            if str(r.get("ticker") or "").upper() != str(c.get("ticker") or "").upper():
                continue
            if str(r.get("closed_at") or "") != str(c.get("closed_at") or ""):
                continue
            cq = _safe_float(c.get("qty"))
            rq = _safe_float(r.get("qty"))
            if cq is not None and rq is not None and abs(cq - rq) > 1e-9:
                continue
            match = unused.pop(i)
            break
        row = dict(c)
        if match is not None:
            row["review"] = match
        out.append(row)
    return out


def summarize_reviews_for_ticker_pnl(
    ticker_pnl: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach review_count / last_verdict / last_tags onto ticker_pnl rows."""
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in reviews:
        t = str(r.get("ticker") or "").upper()
        by_ticker.setdefault(t, []).append(r)

    out: list[dict[str, Any]] = []
    for row in ticker_pnl:
        t = str(row.get("ticker") or "").upper()
        rs = by_ticker.get(t) or []
        # Sort by closed_at desc for "last"
        rs_sorted = sorted(
            rs, key=lambda x: str(x.get("closed_at") or ""), reverse=True
        )
        nr = dict(row)
        nr["review_count"] = len(rs)
        if rs_sorted:
            last = rs_sorted[0]
            nr["last_verdict"] = last.get("verdict")
            nr["last_verdict_tags"] = last.get("verdict_tags")
            nr["last_completeness"] = last.get("completeness")
        else:
            nr["last_verdict"] = None
            nr["last_verdict_tags"] = None
            nr["last_completeness"] = None
        out.append(nr)
    return out
