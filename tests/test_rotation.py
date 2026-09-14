"""Unit tests for cold/active research rotation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from quant_system.config.loader import load_config
from quant_system.models.signal import Direction, Signal
from quant_system.pipeline import run_once
from quant_system.research.rotation import (
    STATE_KEY,
    cold_eligible,
    default_rotation_state,
    load_rotation_state,
    save_rotation_state,
    select_research_universe,
    ticker_signal_stats,
    update_rotation_after_cycle,
)
from quant_system.tracker.api import Tracker


def _ts(day: date, hour: int = 12, minute: int = 0) -> str:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _seed_shared_cycles(
    tracker: Tracker,
    ticker_specs: dict[str, tuple[float, Direction]],
    *,
    n_cycles: int,
    start_day: date | None = None,
) -> None:
    """One run_start per cycle; signals for each ticker in ticker_specs."""
    day0 = start_day or date(2026, 1, 1)
    agents = ["momentum", "fundamental", "statistical_ml", "sentiment", "macro"]
    for i in range(n_cycles):
        d = day0 + timedelta(days=i)
        tracker.log_decision(
            decision_type="run_start",
            actor="pipeline",
            reasoning=f"cycle {i}",
            payload={"tickers": list(ticker_specs)},
            created_at=_ts(d, 9),
        )
        for t, (confidence, direction) in ticker_specs.items():
            for j, aid in enumerate(agents):
                tracker.insert_signal(
                    Signal(
                        agent_id=aid,
                        ticker=t,
                        direction=direction,
                        confidence=confidence,
                        reasoning="seed",
                        supporting_data={},
                        created_at=_ts(d, 10, j),
                    )
                )
        tracker.log_decision(
            decision_type="run_end",
            actor="pipeline",
            reasoning=f"cycle {i} end",
            payload={},
            created_at=_ts(d, 11),
        )


def _seed_cycles(
    tracker: Tracker,
    ticker: str,
    *,
    n_cycles: int,
    confidence: float,
    direction: Direction = Direction.NEUTRAL,
    start_day: date | None = None,
) -> None:
    """Insert n_cycles of run_start + 5 agent-like signals for ticker."""
    _seed_shared_cycles(
        tracker,
        {ticker: (confidence, direction)},
        n_cycles=n_cycles,
        start_day=start_day,
    )


def test_cold_eligible_gates():
    cfg = load_config()
    rot = cfg.universe.rotation
    stats = {"median_confidence": 20.0, "long_fraction": 0.1, "n_signals": 50, "n_cycles": 10}
    assert cold_eligible(
        "IONQ", active={"IONQ"}, holdings=set(), stats=stats, rot=rot
    )
    # held
    assert not cold_eligible(
        "IONQ", active={"IONQ"}, holdings={"IONQ"}, stats=stats, rot=rot
    )
    # not active
    assert not cold_eligible(
        "IONQ", active={"IBM"}, holdings=set(), stats=stats, rot=rot
    )
    # thin history
    assert not cold_eligible(
        "IONQ", active={"IONQ"}, holdings=set(), stats=None, rot=rot
    )
    # not awful (high conf)
    good = {"median_confidence": 60.0, "long_fraction": 0.1}
    assert not cold_eligible(
        "IONQ", active={"IONQ"}, holdings=set(), stats=good, rot=rot
    )
    # not awful (many longs)
    longs = {"median_confidence": 20.0, "long_fraction": 0.8}
    assert not cold_eligible(
        "IONQ", active={"IONQ"}, holdings=set(), stats=longs, rot=rot
    )


def test_never_cold_holdings(tmp_path):
    cfg = load_config()
    tr = Tracker(str(tmp_path / "rot.db"))
    specs = {t: (70.0, Direction.LONG) for t in cfg.universe.themes["quantum"].active}
    specs["QUBT"] = (10.0, Direction.SHORT)
    _seed_shared_cycles(tr, specs, n_cycles=10)

    tr._conn.execute(
        "INSERT INTO positions(ticker, qty, avg_cost, updated_at) VALUES (?,?,?,?)",
        ("QUBT", 1.0, 10.0, _ts(date(2026, 1, 15))),
    )
    tr._conn.commit()

    events = update_rotation_after_cycle(
        tr, cfg, holdings=tr.list_positions(), as_of_date=date(2026, 1, 20)
    )
    cold_tickers = {e["ticker"] for e in events["went_cold"]}
    assert "QUBT" not in cold_tickers
    state = load_rotation_state(tr, cfg.universe)
    assert "QUBT" not in state["cold"]
    assert "QUBT" in state["active"]["quantum"]
    tr.close()


def test_promote_from_bench_on_cold(tmp_path):
    cfg = load_config()
    tr = Tracker(str(tmp_path / "rot2.db"))
    specs = {t: (75.0, Direction.LONG) for t in cfg.universe.themes["quantum"].active}
    specs["QSI"] = (5.0, Direction.NEUTRAL)
    _seed_shared_cycles(tr, specs, n_cycles=10)

    events = update_rotation_after_cycle(
        tr, cfg, holdings=[], as_of_date=date(2026, 1, 20)
    )
    assert any(e["ticker"] == "QSI" for e in events["went_cold"])
    assert events["promoted"], "expected a bench promotion"
    promo = events["promoted"][0]
    assert promo["theme"] == "quantum"
    assert promo["ticker"] in cfg.universe.themes["quantum"].bench
    state = load_rotation_state(tr, cfg.universe)
    assert "QSI" in state["cold"]
    assert "QSI" not in state["active"]["quantum"]
    assert promo["ticker"] in state["active"]["quantum"]
    assert state["cold"]["QSI"]["checkup_date"] == "2026-02-04"  # +15 days
    tr.close()


def test_checkup_extend(tmp_path):
    cfg = load_config()
    tr = Tracker(str(tmp_path / "rot3a.db"))
    state = default_rotation_state(cfg.universe)
    state["active"]["quantum"] = [t for t in state["active"]["quantum"] if t != "RGTI"] + ["AMZN"]
    state["cold"]["RGTI"] = {
        "theme": "quantum",
        "checkup_date": "2026-02-01",
        "reason": "test_cold",
        "went_cold_at": "2026-01-17",
    }
    save_rotation_state(tr, state)

    _seed_cycles(
        tr,
        "RGTI",
        n_cycles=3,
        confidence=10.0,
        direction=Direction.SHORT,
        start_day=date(2026, 1, 30),
    )
    events = update_rotation_after_cycle(
        tr, cfg, holdings=[], as_of_date=date(2026, 2, 1)
    )
    assert any(e["ticker"] == "RGTI" for e in events["extended"])
    assert not any(e["ticker"] == "RGTI" for e in events["revived"])
    st = load_rotation_state(tr, cfg.universe)
    assert "RGTI" in st["cold"]
    assert st["cold"]["RGTI"]["checkup_date"] == "2026-02-16"
    tr.close()


def test_checkup_revive(tmp_path):
    cfg = load_config()
    tr = Tracker(str(tmp_path / "rot3b.db"))
    state = default_rotation_state(cfg.universe)
    # At target size with AMZN already in; RGTI cold and due
    state["active"]["quantum"] = [t for t in state["active"]["quantum"] if t != "RGTI"] + ["AMZN"]
    assert len(state["active"]["quantum"]) == 10
    state["cold"]["RGTI"] = {
        "theme": "quantum",
        "checkup_date": "2026-02-16",
        "reason": "test_cold",
        "went_cold_at": "2026-02-01",
    }
    save_rotation_state(tr, state)

    # Only strong checkup signals (no prior awful history in this DB)
    _seed_cycles(
        tr,
        "RGTI",
        n_cycles=3,
        confidence=80.0,
        direction=Direction.LONG,
        start_day=date(2026, 2, 14),
    )
    events = update_rotation_after_cycle(
        tr, cfg, holdings=[], as_of_date=date(2026, 2, 16)
    )
    assert any(e["ticker"] == "RGTI" for e in events["revived"])
    st = load_rotation_state(tr, cfg.universe)
    assert "RGTI" not in st["cold"]
    assert "RGTI" in st["active"]["quantum"]
    tr.close()


def test_select_includes_checkup_due(tmp_path):
    cfg = load_config()
    tr = Tracker(str(tmp_path / "rot4.db"))
    state = default_rotation_state(cfg.universe)
    state["active"]["space"] = [t for t in state["active"]["space"] if t != "LUNR"]
    state["cold"]["LUNR"] = {
        "theme": "space",
        "checkup_date": "2026-03-01",
        "reason": "awful",
    }
    save_rotation_state(tr, state)

    before = select_research_universe(tr, cfg, as_of_date=date(2026, 2, 20))
    assert "LUNR" not in before
    due = select_research_universe(tr, cfg, as_of_date=date(2026, 3, 1))
    assert "LUNR" in due
    # bench not researched until promoted
    assert "SPCE" not in due
    tr.close()


def test_thin_history_no_thrash(tmp_path):
    cfg = load_config()
    tr = Tracker(str(tmp_path / "rot5.db"))
    # only 2 cycles — should not cold anyone
    _seed_cycles(tr, "PATH", n_cycles=2, confidence=1.0, direction=Direction.SHORT)
    events = update_rotation_after_cycle(
        tr, cfg, holdings=[], as_of_date=date(2026, 1, 5)
    )
    assert events["went_cold"] == []
    stats = ticker_signal_stats(tr, "PATH", lookback_cycles=10, require_full_history=True)
    assert stats is None
    tr.close()


def test_pipeline_researches_active_not_full_bench(tmp_path):
    cfg = load_config()
    tr = Tracker(str(tmp_path / "pipe.db"))
    summary = run_once(tracker=tr, config=cfg, mock=True)
    researched = summary["research_tickers"]
    assert len(researched) == 30
    for bench in ["AMZN", "META", "SPCE", "JOBY", "ADSK", "DE"]:
        assert bench not in researched
    for active in ["IONQ", "RKLB", "NVDA"]:
        assert active in researched
    assert summary.get("rotation") is not None
    # state key written (may be empty cold)
    assert tr.get_state(STATE_KEY, "")
    tr.close()
