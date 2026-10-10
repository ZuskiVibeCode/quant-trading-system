"""'Why we bought it' click-through: export block on holdings + viewer render."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from quant_system.dashboard_export import (
    WHY_BOUGHT_MAX_BUYS,
    build_dashboard_payload,
    build_why_bought,
    validate_payload,
)
from quant_system.models.signal import Direction, Signal
from quant_system.models.ticket import TicketSide, TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "docs" / "app.js"


def _ts(day: int, hour: int = 14, minute: int = 0) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00.000000Z"


def _signal(tracker: Tracker, agent: str, ticker: str, direction: Direction, conf: float, at: str) -> int:
    return tracker.insert_signal(
        Signal(
            agent_id=agent,
            ticker=ticker,
            direction=direction,
            confidence=conf,
            reasoning=f"{agent} says {direction.value}",
            created_at=at,
        )
    )


def _ticket(tracker: Tracker, ticker: str, side: TicketSide, qty: float, rationale: str, at: str, signal_ids=None) -> int:
    return tracker.insert_ticket(
        TradeTicket(
            ticker=ticker,
            side=side,
            qty=qty,
            rationale=rationale,
            signal_ids=signal_ids or [],
            status=TicketStatus.FILLED,
            created_at=at,
        )
    )


def test_why_bought_links_ticket_signals_and_fills(tmp_path):
    tracker = Tracker(str(tmp_path / "why.db"))
    at = _ts(6)
    s1 = _signal(tracker, "momentum", "NVDA", Direction.LONG, 78.0, at)
    s2 = _signal(tracker, "fundamental", "NVDA", Direction.NEUTRAL, 40.0, at)
    tid = _ticket(
        tracker, "NVDA", TicketSide.BUY, 0.1,
        "BUY 0.1 NVDA @~230.36; weighted_score=0.650; blended_conf=75.6; max_pos=20%",
        at, signal_ids=[s1, s2],
    )
    # Two partial fills on the same ticket
    tracker.insert_fill(tid, "NVDA", "buy", 0.06, 230.0, "alpaca", created_at=_ts(6, 14, 1))
    tracker.insert_fill(tid, "NVDA", "buy", 0.04, 231.0, "alpaca", created_at=_ts(6, 14, 2))

    payload = build_dashboard_payload(tracker, include_alpaca=False)
    assert validate_payload(payload) == []
    (h,) = [h for h in payload["holdings"] if h["ticker"] == "NVDA"]
    wb = h["why_bought"]
    assert wb["opened_at"] == _ts(6, 14, 1)
    assert wb["buy_count"] == 1
    assert wb["omitted"] == 0
    (buy,) = wb["buys"]
    assert buy["ticket_id"] == tid
    assert buy["rationale"].startswith("BUY 0.1 NVDA")
    assert buy["score"] == {"weighted_score": pytest.approx(0.65), "blended_conf": pytest.approx(75.6)}
    desks = {s["agent_id"]: s for s in buy["signals"]}
    assert set(desks) == {"momentum", "fundamental"}
    assert desks["momentum"]["direction"] == "long"
    assert desks["momentum"]["confidence"] == pytest.approx(78.0)
    assert desks["momentum"]["signal_id"] == s1
    assert [(f["qty"], f["price"], f["mode"]) for f in buy["fills"]] == [
        (0.06, 230.0, "alpaca"),
        (0.04, 231.0, "alpaca"),
    ]
    assert buy["fills"][0]["at"] == _ts(6, 14, 1)
    tracker.close()


def test_why_bought_only_reports_current_lot(tmp_path):
    tracker = Tracker(str(tmp_path / "lot.db"))
    old = _ticket(tracker, "TER", TicketSide.BUY, 0.5, "BUY old lot", _ts(1))
    tracker.insert_fill(old, "TER", "buy", 0.5, 100.0, "alpaca", created_at=_ts(1))
    sell = _ticket(tracker, "TER", TicketSide.SELL, 0.5, "SELL all", _ts(2))
    tracker.insert_fill(sell, "TER", "sell", 0.5, 105.0, "alpaca", created_at=_ts(2))
    new = _ticket(tracker, "TER", TicketSide.BUY, 0.3, "BUY new lot", _ts(3))
    tracker.insert_fill(new, "TER", "buy", 0.3, 110.0, "alpaca", created_at=_ts(3))

    out = build_why_bought(tracker, {"TER"})
    assert out["TER"]["opened_at"] == _ts(3)
    assert [b["ticket_id"] for b in out["TER"]["buys"]] == [new]
    # Unlinked ticket: no guessed signals
    assert out["TER"]["buys"][0]["signals"] == []
    tracker.close()


def test_why_bought_caps_to_opening_and_recent_buys(tmp_path):
    tracker = Tracker(str(tmp_path / "cap.db"))
    n = WHY_BOUGHT_MAX_BUYS + 2
    ids = []
    for i in range(n):
        tid = _ticket(tracker, "PATH", TicketSide.BUY, 0.1, f"BUY add {i}", _ts(1 + i))
        tracker.insert_fill(tid, "PATH", "buy", 0.1, 15.0 + i, "alpaca", created_at=_ts(1 + i))
        ids.append(tid)

    wb = build_why_bought(tracker, {"PATH"})["PATH"]
    assert wb["buy_count"] == n
    assert wb["omitted"] == 2
    kept = [b["ticket_id"] for b in wb["buys"]]
    assert len(kept) == WHY_BOUGHT_MAX_BUYS
    assert kept[0] == ids[0]  # opening buy always kept
    assert kept[1:] == ids[-(WHY_BOUGHT_MAX_BUYS - 1):]
    tracker.close()


def test_why_bought_backfill_and_unheld(tmp_path):
    tracker = Tracker(str(tmp_path / "bf.db"))
    bid = _ticket(tracker, "IONQ", TicketSide.BUY, 0.5, "alpaca_backfill order_id=abc", _ts(8))
    tracker.insert_fill_historical(
        bid, "IONQ", "buy", 0.5, 42.29, "alpaca_backfill", _ts(8),
        order_id="abc", apply_to_position=True,
    )
    other = _ticket(tracker, "LUNR", TicketSide.BUY, 1.0, "BUY LUNR", _ts(8))
    tracker.insert_fill_historical(
        other, "LUNR", "buy", 1.0, 14.0, "alpaca", _ts(8), apply_to_position=False,
    )

    out = build_why_bought(tracker, {"IONQ"})
    assert set(out) == {"IONQ"}  # only held tickers
    (buy,) = out["IONQ"]["buys"]
    assert buy["signals"] == []
    assert buy["fills"][0]["mode"] == "alpaca_backfill"
    assert build_why_bought(tracker, set()) == {}
    tracker.close()


def test_holding_without_fills_gets_null(tmp_path):
    tracker = Tracker(str(tmp_path / "nofill.db"))
    tracker.conn.execute(
        "INSERT INTO positions(ticker, qty, avg_cost, updated_at) VALUES (?,?,?,?)",
        ("MSFT", 0.5, 400.0, _ts(9)),
    )
    tracker.conn.commit()
    payload = build_dashboard_payload(tracker, include_alpaca=False)
    (h,) = payload["holdings"]
    assert h["why_bought"] is None
    tracker.close()


# --- viewer -----------------------------------------------------------------


def _js_function(src: str, name: str) -> str:
    """Return the source of ``function name(...) {...}`` from app.js by brace matching."""
    m = re.search(rf"function {name}\(", src)
    assert m, f"{name} not found in app.js"
    i = src.index("{", m.end())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_viewer_renders_why_bought():
    src = APP_JS.read_text(encoding="utf-8")
    assert "${whyBoughtHtml(h.why_bought)}" in src  # wired into the holding detail view
    funcs = "\n".join(
        _js_function(src, n)
        for n in ("escapeHtml", "fmtUsd", "fmtTime", "trunc", "whyBoughtBuyHtml", "whyBoughtHtml")
    )
    wb = {
        "opened_at": "2026-10-06T14:01:00Z",
        "buy_count": 12,
        "omitted": 2,
        "buys": [
            {
                "ticket_id": 101,
                "created_at": "2026-10-06T14:00:00Z",
                "rationale": "BUY 0.1 NVDA @~230.36; weighted_score=0.650",
                "score": {"weighted_score": 0.65, "blended_conf": 75.6},
                "signals": [
                    {"agent_id": "momentum", "direction": "long", "confidence": 78,
                     "reasoning_trunc": "RS leader", "signal_id": 7},
                ],
                "fills": [{"fill_id": 1, "at": "2026-10-06T14:01:00Z", "qty": 0.06, "price": 230, "mode": "alpaca"}],
            },
            {
                "ticket_id": 102,
                "created_at": "2026-10-07T14:00:00Z",
                "rationale": "alpaca_backfill order_id=abc",
                "signals": [],
                "fills": [{"fill_id": 2, "at": "2026-10-07T14:01:00Z", "qty": 0.5, "price": 42.29, "mode": "alpaca_backfill"}],
            },
        ],
    }
    script = funcs + f"""
const wb = {json.dumps(wb)};
console.log(JSON.stringify({{full: whyBoughtHtml(wb), empty: whyBoughtHtml(null)}}));
"""
    out = json.loads(subprocess.check_output(["node", "-e", script], text=True, encoding="utf-8"))
    full = out["full"]
    assert "Why we bought it" in full
    assert "12 buy tickets" in full
    assert "2 more buys between these not shown" in full
    assert "Ticket #101" in full and "BUY 0.1 NVDA" in full
    assert "score 0.650" in full and "conf 75.6" in full
    assert "momentum" in full and 'class="dir long"' in full and "78.0" in full
    assert "$230.00" in full
    # Backfilled buy explains the missing reasoning instead of showing raw text
    assert "the PM's reasoning wasn't recorded" in full
    assert "No desk signals linked to this ticket." in full
    assert "No buy records for this position" in out["empty"]
