"""Unit tests for trade review builder (no network)."""

from __future__ import annotations

import pytest

from quant_system.models.signal import Direction, Signal
from quant_system.models.ticket import TicketSide, TicketStatus, TradeTicket
from quant_system.research.trade_review import (
    agent_mix_summary,
    build_round_review,
    build_trade_reviews,
    classify_exit_reason,
)
from quant_system.tracker.api import Tracker


def _fill(
    tracker: Tracker,
    ticker: str,
    side: str,
    qty: float,
    price: float,
    at: str,
    *,
    rationale: str | None = None,
    signal_ids: list[int] | None = None,
    mode: str = "alpaca",
) -> int:
    t = TradeTicket(
        ticker=ticker,
        side=TicketSide.BUY if side == "buy" else TicketSide.SELL,
        qty=qty,
        rationale=rationale or f"test {side}",
        signal_ids=signal_ids or [],
        status=TicketStatus.FILLED,
        created_at=at,
    )
    tid = tracker.insert_ticket(t)
    tracker.insert_fill_historical(
        tid, ticker, side, qty, price, mode, at, apply_to_position=False
    )
    return tid


def _sig(
    tracker: Tracker,
    agent_id: str,
    ticker: str,
    direction: str,
    confidence: float,
    at: str,
) -> int:
    return tracker.insert_signal(
        Signal(
            agent_id=agent_id,
            ticker=ticker,
            direction=Direction(direction),
            confidence=confidence,
            reasoning=f"{agent_id} {direction}",
            supporting_data={},
            created_at=at,
        )
    )


def test_classify_exit_reason_buckets():
    assert (
        classify_exit_reason(
            "SELL 0.1 QUBT @~9.21; cash buffer trim; weighted_score=-0.019; cash/equity=0.09% < min=10%"
        )
        == "cash_buffer_trim"
    )
    assert (
        classify_exit_reason(
            "SELL 0.1 LUNR @~14.96; opportunity rotate to fund ISRG (buy_score=0.446 donor_score=-0.215)"
        )
        == "opportunity_rotate"
    )
    assert (
        classify_exit_reason(
            "SELL 0.0002 IONQ @~46.24; weighted_score=-0.312; reduce risk"
        )
        == "signal_sell"
    )
    assert classify_exit_reason("") == "unknown"
    assert classify_exit_reason(None) == "unknown"
    assert classify_exit_reason("alpaca_backfill order_id=abc") == "other"


def test_disagreement_mix_verdict_mentions_split(tmp_path):
    db = str(tmp_path / "split.db")
    tracker = Tracker(db)
    sid_m = _sig(tracker, "momentum", "IONQ", "long", 72.0, "2026-09-10T09:00:00.000000Z")
    sid_f = _sig(tracker, "fundamental", "IONQ", "short", 65.0, "2026-09-10T09:00:00.000000Z")
    _fill(
        tracker,
        "IONQ",
        "buy",
        1.0,
        40.0,
        "2026-09-10T10:00:00.000000Z",
        rationale="BUY 1 IONQ @~40.00; weighted_score=0.050; blended_conf=68.0; max_pos=20%",
        signal_ids=[sid_m, sid_f],
    )
    _fill(
        tracker,
        "IONQ",
        "sell",
        1.0,
        38.0,
        "2026-09-11T10:00:00.000000Z",
        rationale="SELL 1 IONQ @~38.00; weighted_score=-0.200; reduce risk",
    )

    reviews = build_trade_reviews(tracker, market_data=None, sector_of={"IONQ": "quantum"})
    tracker.close()

    assert len(reviews) == 1
    r = reviews[0]
    assert r["mix"]["disagreement"] is True
    assert "split" in r["verdict"].lower() or "split_book" in r["verdict_tags"]
    assert "split_book" in r["verdict_tags"]
    assert "loser" in r["verdict_tags"]
    # Evidence must cite both agent signals
    sig_ev = [e for e in r["evidence"] if e["kind"] == "signal" and e["id"] is not None]
    assert {e["id"] for e in sig_ev} >= {sid_m, sid_f}
    assert any("momentum" in (e["label"] or "") for e in sig_ev)
    assert any("fundamental" in (e["label"] or "") for e in sig_ev)


def test_cash_buffer_exit_forced_tag(tmp_path):
    db = str(tmp_path / "buffer.db")
    tracker = Tracker(db)
    sid = _sig(tracker, "momentum", "QUBT", "long", 55.0, "2026-09-10T09:00:00.000000Z")
    _fill(
        tracker,
        "QUBT",
        "buy",
        2.0,
        10.0,
        "2026-09-10T10:00:00.000000Z",
        rationale="BUY 2 QUBT @~10.00; weighted_score=0.200; blended_conf=55.0; max_pos=20%",
        signal_ids=[sid],
    )
    _fill(
        tracker,
        "QUBT",
        "sell",
        0.5,
        9.0,
        "2026-09-11T10:00:00.000000Z",
        rationale="SELL 0.5 QUBT @~9.00; cash buffer trim; weighted_score=-0.019; cash/equity=0.09% < min=10%",
    )

    reviews = build_trade_reviews(tracker)
    tracker.close()
    r = reviews[0]
    assert r["exit_reason"] == "cash_buffer_trim"
    assert "forced_exit" in r["verdict_tags"]
    assert "cash-buffer" in r["verdict"].lower() or "cash buffer" in r["verdict"].lower()
    ticket_ev = [e for e in r["evidence"] if e["kind"] == "ticket" and e["label"] == "exit ticket"]
    assert ticket_ev and ticket_ev[0]["id"] is not None
    assert "cash buffer" in (ticket_ev[0]["detail"] or "").lower()


def test_thin_backfill_honest_verdict(tmp_path):
    db = str(tmp_path / "thin.db")
    tracker = Tracker(db)
    _fill(
        tracker,
        "LUNR",
        "buy",
        1.0,
        14.0,
        "2026-09-08T13:34:13.000000Z",
        rationale="alpaca_backfill order_id=abc",
        signal_ids=[],
        mode="alpaca_backfill",
    )
    _fill(
        tracker,
        "LUNR",
        "sell",
        1.0,
        15.0,
        "2026-09-18T14:04:52.000000Z",
        rationale="SELL 1 LUNR @~15.00; weighted_score=-0.200; reduce risk",
    )

    reviews = build_trade_reviews(tracker)
    tracker.close()
    r = reviews[0]
    assert r["completeness"] == "thin"
    assert "thin" in r["verdict"].lower()
    assert "thin_data" in r["verdict_tags"]
    # Evidence must state signals absent — never invent citations
    sig_ev = [e for e in r["evidence"] if e["kind"] == "signal"]
    assert sig_ev
    assert sig_ev[0]["id"] is None
    assert "absent" in (sig_ev[0]["detail"] or "").lower()


def test_winner_aligned_tag(tmp_path):
    db = str(tmp_path / "win.db")
    tracker = Tracker(db)
    sid_m = _sig(tracker, "momentum", "NVDA", "long", 80.0, "2026-09-10T09:00:00.000000Z")
    sid_s = _sig(tracker, "sentiment", "NVDA", "long", 70.0, "2026-09-10T09:00:00.000000Z")
    _fill(
        tracker,
        "NVDA",
        "buy",
        1.0,
        100.0,
        "2026-09-10T10:00:00.000000Z",
        rationale="BUY 1 NVDA @~100.00; weighted_score=0.500; blended_conf=75.0; max_pos=20%",
        signal_ids=[sid_m, sid_s],
    )
    _fill(
        tracker,
        "NVDA",
        "sell",
        1.0,
        110.0,
        "2026-09-12T10:00:00.000000Z",
        rationale="SELL 1 NVDA @~110.00; weighted_score=-0.100; reduce risk",
    )

    reviews = build_trade_reviews(tracker)
    tracker.close()
    r = reviews[0]
    assert r["pnl"] == pytest.approx(10.0)
    assert "winner" in r["verdict_tags"]
    assert "aligned" in r["verdict_tags"]
    assert "signal_exit" in r["verdict_tags"]
    assert r["completeness"] == "full"
    assert r["mix"]["lead_agent"] == "momentum"
    # Verdict may cite signal id
    assert any(e["kind"] == "signal" and e["id"] == sid_m for e in r["evidence"])
    assert any(e["kind"] == "fill" and e["label"] == "entry fill" and e["id"] for e in r["evidence"])
    assert any(e["kind"] == "fill" and e["label"] == "exit fill" and e["id"] for e in r["evidence"])


def test_agent_mix_summary_helpers():
    mix = agent_mix_summary(
        [
            {"agent_id": "momentum", "direction": "long", "confidence": 70},
            {"agent_id": "fundamental", "direction": "short", "confidence": 60},
            {"agent_id": "macro", "direction": "neutral", "confidence": 50},
        ]
    )
    assert mix["disagreement"] is True
    assert mix["long"] == ["momentum"]
    assert mix["short"] == ["fundamental"]
    assert mix["lead_agent"] == "momentum"


def test_build_round_review_evidence_always_present():
    r = build_round_review(
        ticker="X",
        qty=1.0,
        entry=10.0,
        exit_px=9.0,
        pnl=-1.0,
        opened_at="2026-09-01T10:00:00.000000Z",
        closed_at="2026-09-02T10:00:00.000000Z",
        entry_fill_id=None,
        exit_fill_id=None,
        entry_ticket=None,
        exit_ticket=None,
        agents=[],
        mode="alpaca_backfill",
    )
    assert isinstance(r["evidence"], list) and len(r["evidence"]) >= 3
    assert r["completeness"] == "thin"
    kinds = {e["kind"] for e in r["evidence"]}
    assert "fill" in kinds
    assert "ticket" in kinds
    assert "signal" in kinds


def test_build_trade_reviews_in_memory_multi(tmp_path):
    db = str(tmp_path / "multi.db")
    tracker = Tracker(db)
    sid = _sig(tracker, "momentum", "AMD", "long", 66.0, "2026-09-01T09:00:00.000000Z")
    _fill(
        tracker,
        "AMD",
        "buy",
        2.0,
        50.0,
        "2026-09-01T10:00:00.000000Z",
        rationale="BUY 2 AMD @~50; weighted_score=0.4; blended_conf=66; max_pos=20%",
        signal_ids=[sid],
    )
    _fill(
        tracker,
        "AMD",
        "sell",
        1.0,
        55.0,
        "2026-09-02T10:00:00.000000Z",
        rationale="SELL 1 AMD @~55; weighted_score=-0.1; reduce risk",
    )
    _fill(
        tracker,
        "AMD",
        "sell",
        1.0,
        48.0,
        "2026-09-03T10:00:00.000000Z",
        rationale="SELL 1 AMD @~48; opportunity rotate to fund NVDA (buy_score=0.5 donor_score=-0.1)",
    )
    reviews = build_trade_reviews(tracker)
    tracker.close()
    assert len(reviews) == 2
    assert reviews[0]["pnl"] == pytest.approx(5.0)
    assert reviews[1]["exit_reason"] == "opportunity_rotate"
    assert "forced_exit" in reviews[1]["verdict_tags"]
