"""Orbit follow-ups: research-day fallback, dust holdings, HTML escaping in the viewer."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quant_system import dashboard_export as de
from quant_system.models.signal import Direction, Signal
from quant_system.models.ticket import TicketSide, TicketStatus, TradeTicket
from quant_system.tracker.api import Tracker
from test_why_bought import _js_function

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "docs" / "app.js"

FRI = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)  # Fri 11:00 ET
SAT = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)  # Sat 11:00 ET


@pytest.fixture
def clock(monkeypatch):
    """Settable "now" for the export module."""
    state = {"now": FRI}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return state["now"].replace(tzinfo=None)
            return state["now"].astimezone(tz)

    monkeypatch.setattr(de, "datetime", _Clock)
    return state


def _signal(tracker: Tracker, agent: str, ticker: str, direction: Direction, conf: float, at: str) -> int:
    return tracker.insert_signal(
        Signal(agent_id=agent, ticker=ticker, direction=direction, confidence=conf,
               reasoning=f"{agent} {ticker}", created_at=at)
    )


def _seed_research(tracker: Tracker) -> None:
    # Thursday and Friday research; nothing on Saturday
    _signal(tracker, "momentum", "TER", Direction.SHORT, 60.0, "2026-10-08T15:00:00.000000Z")
    _signal(tracker, "momentum", "NVDA", Direction.LONG, 80.0, "2026-10-09T14:00:00.000000Z")
    _signal(tracker, "fundamental", "NVDA", Direction.LONG, 70.0, "2026-10-09T14:00:01.000000Z")


# --- (2) research-day fallback ------------------------------------------------


def test_weekend_payload_falls_back_to_last_research_day(tmp_path, clock):
    tracker = Tracker(str(tmp_path / "wk.db"))
    _seed_research(tracker)
    clock["now"] = SAT
    payload = de.build_dashboard_payload(tracker, include_alpaca=False)
    tracker.close()

    meta = payload["meta"]
    assert meta["day_local"] == "2026-10-10"
    assert meta["research_day_local"] == "2026-10-09"
    assert meta["research_fallback"] is True
    # Only Friday's signals, not Thursday's
    assert payload["agents_today"]["momentum"]["count"] == 1
    assert payload["agents_today"]["fundamental"]["count"] == 1
    assert [p["ticker"] for p in payload["zones"]["points"]] == ["NVDA"]
    nvda = [w for w in payload["watchlist"] if w["ticker"] == "NVDA"]
    assert nvda and nvda[0]["signal"] is not None


def test_weekday_with_research_does_not_fall_back(tmp_path, clock):
    tracker = Tracker(str(tmp_path / "wd.db"))
    _seed_research(tracker)
    clock["now"] = FRI
    payload = de.build_dashboard_payload(tracker, include_alpaca=False)
    tracker.close()
    assert payload["meta"]["research_fallback"] is False
    assert payload["meta"]["research_day_local"] == payload["meta"]["day_local"] == "2026-10-09"


def test_no_research_ever_stays_on_today(tmp_path, clock):
    tracker = Tracker(str(tmp_path / "none.db"))
    clock["now"] = SAT
    payload = de.build_dashboard_payload(tracker, include_alpaca=False)
    tracker.close()
    assert payload["meta"]["research_fallback"] is False
    assert payload["meta"]["research_day_local"] == "2026-10-10"


def test_weekend_export_writes_no_history_and_keeps_zones_history(tmp_path, clock):
    db = str(tmp_path / "exp.db")
    docs = tmp_path / "docs"
    tracker = Tracker(db)
    _seed_research(tracker)

    clock["now"] = FRI
    fri = de.export_dashboard(tracker, docs_dir=docs, include_alpaca=False)
    assert fri["research_fallback"] is False
    assert Path(fri["history"]).name == "2026-10-09.json"

    clock["now"] = SAT
    sat = de.export_dashboard(tracker, docs_dir=docs, include_alpaca=False)
    tracker.close()

    assert sat["research_fallback"] is True
    assert sat["research_day"] == "2026-10-09"
    assert sat["history"] is None
    hist = sorted(p.name for p in (docs / "data" / "history").glob("*.json"))
    assert hist == ["2026-10-09.json"]
    zh = json.loads((docs / "data" / "zones_history.json").read_text())
    assert [d["day"] for d in zh["days"]] == ["2026-10-09"]
    latest = json.loads((docs / "data" / "latest.json").read_text())
    assert latest["meta"]["research_fallback"] is True
    assert latest["zones"]["points"]  # Zones not blank on the weekend


def test_rebuild_zones_history_skips_empty_and_fallback_days(tmp_path):
    hist = tmp_path / "history"
    hist.mkdir()
    sig = {"ticker": "NVDA", "direction": "long", "confidence": 80.0}
    agents = {"momentum": {"agent_id": "momentum", "signals": [sig]}}
    empty = {"momentum": {"agent_id": "momentum", "signals": []}}

    def write(day, payload):
        (hist / f"{day}.json").write_text(json.dumps(payload))

    write("2026-10-09", {"meta": {"day_local": "2026-10-09"}, "agents_today": agents})
    write("2026-10-10", {"meta": {"day_local": "2026-10-10"}, "agents_today": empty})  # blank weekend export
    write("2026-10-11", {"meta": {"day_local": "2026-10-11", "research_fallback": True}, "agents_today": agents})

    out = de.rebuild_zones_history(hist)
    assert [d["day"] for d in out["days"]] == ["2026-10-09"]


# --- (3) dust holdings ----------------------------------------------------------


def test_is_dust_threshold():
    assert de.is_dust(0.000001, 250.0)  # IBM leftover: $0.00025
    assert de.is_dust(0.0, 100.0)
    assert not de.is_dust(0.00201, 250.0)  # small but real: ~$0.50
    assert not de.is_dust(1.0, 0.02)


def test_dust_position_hidden_from_holdings(tmp_path):
    tracker = Tracker(str(tmp_path / "dust.db"))
    for ticker, qty, price in (("IBM", 0.000001, 250.0), ("NVDA", 0.06, 230.0)):
        tid = tracker.insert_ticket(
            TradeTicket(ticker=ticker, side=TicketSide.BUY, qty=qty, rationale=f"BUY {ticker}",
                        status=TicketStatus.FILLED, created_at="2026-10-09T14:00:00.000000Z")
        )
        tracker.insert_fill(tid, ticker, "buy", qty, price, "alpaca", created_at="2026-10-09T14:00:01.000000Z")
    payload = de.build_dashboard_payload(tracker, include_alpaca=False)
    # The position itself is untouched
    assert {p["ticker"] for p in tracker.list_positions()} == {"IBM", "NVDA"}
    tracker.close()

    assert [h["ticker"] for h in payload["holdings"]] == ["NVDA"]
    assert payload["holdings"][0]["why_bought"] is not None


# --- (1) escaping in the viewer ---------------------------------------------------


def test_app_js_feed_text_is_escaped():
    """Every interpolated reasoning / rationale / message / summary goes through escapeHtml."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "function escapeHtml(" in src
    bad = []
    for m in re.finditer(r"\$\{([^{}]*\.(?:reasoning|reasoning_trunc|rationale|message|summary|verdict)\b[^{}]*)\}", src):
        expr = m.group(1).lstrip()
        if expr.startswith("err."):
            continue  # load error goes to textContent, which never parses HTML
        if not expr.startswith("escapeHtml("):
            bad.append(m.group(0))
    assert bad == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_viewer_escapes_html_and_hides_dust():
    src = APP_JS.read_text(encoding="utf-8")
    funcs = "\n".join(
        _js_function(src, n)
        for n in ("escapeHtml", "safeNum", "isDust", "fmtUsd", "fmtTime", "trunc", "whyBoughtBuyHtml", "whyBoughtHtml")
    )
    headline = '<img src=x onerror="alert(1)"> & "quotes"'
    wb = {
        "opened_at": "2026-10-06T14:01:00Z",
        "buy_count": 1,
        "omitted": 0,
        "buys": [{
            "ticket_id": 1,
            "created_at": "2026-10-06T14:00:00Z",
            "rationale": headline,
            "signals": [{"agent_id": "sentiment", "direction": "long", "confidence": 60, "reasoning_trunc": headline}],
            "fills": [{"at": "2026-10-06T14:01:00Z", "qty": 0.1, "price": 10, "mode": "alpaca"}],
        }],
    }
    script = funcs + f"""
const wb = {json.dumps(wb)};
console.log(JSON.stringify({{
  esc: escapeHtml({json.dumps(headline)}),
  nul: escapeHtml(null),
  html: whyBoughtHtml(wb),
  dust: [isDust({{qty: 0.000001, avg_cost: 250}}), isDust({{qty: 0.00201, avg_cost: 250}}), isDust({{qty: null}})],
}}));
"""
    out = json.loads(subprocess.check_output(["node", "-e", script], text=True, encoding="utf-8"))
    assert out["esc"] == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &quot;quotes&quot;"
    assert out["nul"] == ""
    assert "<img" not in out["html"]
    assert "&lt;img" in out["html"]
    assert out["dust"] == [True, False, False]
