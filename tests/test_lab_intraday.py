"""Intraday lab (Stage 1): 15m bars, bar sizes built from them, engine, costs, trials. No network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml

from quant_system.lab.engine import run_spec_intraday
from quant_system.lab.intraday import (
    AlpacaIntradayProvider,
    IntradaySource,
    bar_slots,
    build_panels,
    calendar_frame,
    intraday_coverage,
    intraday_dir,
    load_calendar,
    load_intraday,
    raw_bars_frame,
    regular_hours,
    resample,
    save_intraday,
    sync_intraday,
    ticker_coverage,
)
from quant_system.lab.memory import peak_memory_mb
from quant_system.lab.robust import matched_returns, run_robust
from quant_system.lab.runner import evaluate, score_spec
from quant_system.lab.settings import (
    CEILING_ENV,
    LabCoverageSettings,
    LabCostSettings,
    LabRobustSettings,
    load_lab_settings,
)
from quant_system.lab.spec import SpecError, StrategySpec, load_spec
from quant_system.lab.trials import load_trials, trial_stats
from quant_system.lab.windows import UNLOCK_ENV, ResolvedWindow

TODAY = date(2026, 10, 5)
COSTS = LabCostSettings()
NAMES = {"AAA": ("Tech", "large"), "BBB": ("Tech", "mid"), "CCC": ("Energy", "large"), "DDD": ("Energy", "mid")}
HALF_DAY = pd.Timestamp("2021-11-26")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (CEILING_ENV, UNLOCK_ENV, "QUANT_LAB_BOT"):
        monkeypatch.delenv(key, raising=False)


def _calendar(days) -> pd.DataFrame:
    rows = []
    for d in pd.DatetimeIndex(days):
        close = pd.Timedelta(hours=13) if d == HALF_DAY else pd.Timedelta(hours=16)
        rows.append((d, d + pd.Timedelta(hours=9, minutes=30), d + close))
    return calendar_frame(rows)


def _intraday(cal: pd.DataFrame, symbols, *, seed=7, bar_dollars=4e6, flat=False) -> dict[str, pd.DataFrame]:
    """Random-walk 15m bars on every scheduled slot ($4M a bar: the 8 bps bucket)."""
    rng = np.random.default_rng(seed)
    idx = bar_slots(cal, "15m", cal.index[0].date(), cal.index[-1].date()).index
    out = {}
    for sym in symbols:
        if flat:
            c = np.full(len(idx), 50.0)
            o = c.copy()
        else:
            c = 50 * np.exp(np.cumsum(rng.normal(0.00002, 0.003, len(idx))))
            o = np.r_[50.0, c[:-1]] * np.exp(rng.normal(0, 0.001, len(idx)))
        out[sym] = pd.DataFrame(
            {"Open": o, "High": np.maximum(o, c) * 1.001, "Low": np.minimum(o, c) * 0.999, "Close": c,
             "Volume": bar_dollars / c},
            index=idx,
        )
    return out


def _daily(intra: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out = {}
    for sym, df in intra.items():
        g = df.groupby(df.index.normalize())
        out[sym] = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
                                 "Close": g["Close"].last(), "Volume": g["Volume"].sum()})
    return out


def _settings(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "lab.yaml").write_text("universe_file: u.yaml\n", encoding="utf-8")
    rows = [{"symbol": s, "sector": sec, "cap_bucket": cap} for s, (sec, cap) in NAMES.items()]
    (tmp_path / "u.yaml").write_text(yaml.safe_dump({"tickers": rows}), encoding="utf-8")
    s = load_lab_settings(tmp_path)
    return s.model_copy(update={"robust": LabRobustSettings(min_subset_names=2)})


def _world(n_sessions=40, **kw):
    days = pd.bdate_range("2021-10-01", periods=n_sessions)
    cal = _calendar(days)
    intra = _intraday(cal, ["SPY", *NAMES], **kw)
    return cal, intra, _daily(intra)


def _source(cal, intra, settings=None, calls=None):
    def loader(sym):
        if calls is not None:
            calls.append(sym)
        return intra.get(sym)

    return IntradaySource(settings=settings or load_lab_settings(), loader=loader, calendar=cal, today=TODAY)


def _spec(**changes) -> StrategySpec:
    base = {"id": "t", "timeframe": "15m", "entry": "close > 0", "exit": "bars_held >= 3", "rank": "close",
            "sizing": {"max_positions": 1, "max_weight": 0.2}}
    return StrategySpec.model_validate({**base, **changes})


def _window(cal, first, last) -> ResolvedWindow:
    return ResolvedWindow("discovery", cal.index[first].date(), cal.index[last].date())


def _panels(cal, intra, tf="15m"):
    return build_panels(list(NAMES), tf, bench="SPY", calendar=cal, loader=intra.get,
                        start=date(2016, 1, 1), ceiling=date(2021, 12, 31))


# -- spec ---------------------------------------------------------------------


def test_daily_spec_hashes_did_not_change():
    # Hashes on main before intraday existed: daily specs keep their trials.
    expected = {"breakout_trend": "b0cef5886da632db", "macd_cross": "95639219422a1f76",
                "rsi_pullback": "3f531b6be67dc201"}
    for name, h in expected.items():
        spec = load_spec(Path("lab_specs/examples") / f"{name}.yaml")
        assert spec.hash == h and "timeframe" not in spec.canonical()


def test_each_timeframe_is_its_own_spec_hash():
    daily = _spec(timeframe=None)
    assert len({daily.hash, _spec().hash, _spec(timeframe="1h").hash}) == 3
    assert _spec(timeframe="1h").canonical()["timeframe"] == "1h"


def test_specs_may_declare_15m_and_1h_only(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump({**_spec().canonical(), "timeframe": "30m"}), encoding="utf-8")
    with pytest.raises(SpecError, match="specs may declare 15m, 1h"):
        load_spec(path)
    path.write_text(yaml.safe_dump({**_spec().canonical(), "timeframe": "5m"}), encoding="utf-8")
    with pytest.raises(SpecError):
        load_spec(path)


def test_session_names_need_an_intraday_timeframe():
    _spec(entry="bar_of_day == 0", exit="bars_left_today <= 1")  # fine
    with pytest.raises(ValueError, match="need an intraday timeframe"):
        _spec(timeframe=None, entry="bar_of_day == 0")


# -- bars ---------------------------------------------------------------------


def test_regular_hours_drop_extended_bars_and_honor_half_days():
    cal = _calendar([pd.Timestamp("2021-11-24"), HALF_DAY])
    idx = pd.DatetimeIndex(["2021-11-24 04:00", "2021-11-24 09:15", "2021-11-24 09:30", "2021-11-24 15:45",
                            "2021-11-24 16:00", "2021-11-26 12:45", "2021-11-26 13:00", "2021-11-27 10:00"])
    df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx)
    kept = regular_hours(df, cal).index.strftime("%m-%d %H:%M").tolist()
    assert kept == ["11-24 09:30", "11-24 15:45", "11-26 12:45"]


def test_bar_slots_start_at_the_open_and_count_the_session():
    cal = _calendar([pd.Timestamp("2021-11-24"), HALF_DAY])
    one = bar_slots(cal, "1h", date(2021, 1, 1), date(2021, 12, 31))
    day1 = one.loc["2021-11-24"]
    assert day1.index.strftime("%H:%M").tolist() == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    assert day1["bars_left"].tolist() == [6, 5, 4, 3, 2, 1, 0]
    assert one.loc["2021-11-26"].index.strftime("%H:%M").tolist() == ["09:30", "10:30", "11:30", "12:30"]
    q = bar_slots(cal, "15m", date(2021, 1, 1), date(2021, 12, 31))
    assert len(q.loc["2021-11-24"]) == 26 and len(q.loc["2021-11-26"]) == 14
    assert q["minute"].iloc[:3].tolist() == [0, 15, 30]
    assert len(bar_slots(cal, "2h", date(2021, 1, 1), date(2021, 12, 31)).loc["2021-11-24"]) == 4


def test_resample_uses_only_the_15m_bars_inside_each_bar():
    cal = _calendar([pd.Timestamp("2021-11-24")])
    idx = pd.DatetimeIndex(["2021-11-24 09:30", "2021-11-24 09:45", "2021-11-24 10:15", "2021-11-24 10:30",
                            "2021-11-24 15:45"])
    df = pd.DataFrame({"Open": [10, 11, 12, 13, 14.0], "High": [10.5, 11.5, 12.5, 13.5, 14.5],
                       "Low": [9.5, 10.5, 11.5, 12.5, 13.5], "Close": [11, 12, 13, 14, 15.0],
                       "Volume": [1, 2, 3, 4, 5.0]}, index=idx)
    h = resample(df, cal, "1h")
    assert h.index.strftime("%H:%M").tolist() == ["09:30", "10:30", "15:30"]
    first = h.iloc[0]
    assert (first["Open"], first["High"], first["Low"], first["Close"], first["Volume"]) == (10, 12.5, 9.5, 13, 6)
    assert h.iloc[2]["Open"] == 14 and h.iloc[2]["Close"] == 15  # 15:30 bar: 30 minutes, one 15m bar here


def test_window_mask_keeps_intraday_bars_on_the_last_day():
    idx = pd.DatetimeIndex(["2021-12-30 15:45", "2021-12-31 09:30", "2021-12-31 15:45", "2022-01-03 09:30"])
    w = ResolvedWindow("discovery", date(2021, 12, 31), date(2021, 12, 31))
    assert w.mask(idx).tolist() == [False, True, True, False]


# -- engine -------------------------------------------------------------------


def test_signal_at_bar_close_fills_at_next_bar_open_with_intraday_costs():
    cal, intra, daily = _world(flat=True)
    panels = _panels(cal, intra)
    window = _window(cal, 25, 27)
    res = run_spec_intraday(_spec(entry="bar_of_day == 1", exit="bars_held >= 2"), panels, daily, list(NAMES),
                            window, COSTS)
    buys = res.trades[res.trades["side"] == "buy"]
    first = buys.iloc[0]
    assert first["date"] == cal.index[25] + pd.Timedelta(hours=10)  # 09:45 bar's close -> 10:00 open
    assert first["bps"] == pytest.approx(8.0)  # $104M/day: top intraday bucket; 10:00 is past the open window
    sells = res.trades[res.trades["side"] == "sell"]
    assert sells.iloc[0]["date"] == cal.index[25] + pd.Timedelta(hours=10, minutes=30)  # held 2 bars
    assert list(res.returns.index) == list(cal.index[25:28])  # one return per session


def test_open_window_surcharge_and_overnight_fill():
    cal, intra, daily = _world(flat=True)
    panels = _panels(cal, intra)
    window = _window(cal, 25, 27)
    early = run_spec_intraday(_spec(entry="bar_of_day == 0"), panels, daily, list(NAMES), window, COSTS)
    t = early.trades.iloc[0]
    assert t["date"] == cal.index[25] + pd.Timedelta(hours=9, minutes=45) and t["bps"] == pytest.approx(12.0)
    late = run_spec_intraday(_spec(entry="bars_left_today == 0"), panels, daily, list(NAMES), window, COSTS)
    t = late.trades.iloc[0]
    # The session's last bar fills at the next session's opening print: no surcharge.
    assert t["date"] == cal.index[26] + pd.Timedelta(hours=9, minutes=30) and t["bps"] == pytest.approx(8.0)


def test_flat_overnight_spec_holds_nothing_at_any_close():
    cal, intra, daily = _world()
    panels = _panels(cal, intra)
    spec = _spec(entry="bar_of_day == 0", exit="bars_left_today <= 1", sizing={"max_positions": 2})
    res = run_spec_intraday(spec, panels, daily, list(NAMES), _window(cal, 22, 39), COSTS)
    assert res.trade_count > 0
    assert (res.weights.sum(axis=1) == 0).all()
    assert (res.overnight == 0).all()  # nothing held across a close
    ret = (1 + res.overnight) * (1 + res.intraday) - 1
    assert np.allclose(ret, res.returns)


def test_engine_sees_no_bar_after_the_one_it_signals_on():
    cal, intra, daily = _world()
    spec = _spec(indicators={"r": {"fn": "rsi", "length": 6}}, entry="r < 45", exit="r > 55 or bars_held >= 8",
                 sizing={"max_positions": 2})
    window = _window(cal, 22, 39)
    base = run_spec_intraday(spec, _panels(cal, intra), daily, list(NAMES), window, COSTS)
    cut = pd.Timestamp(cal.index[31]) + pd.Timedelta(hours=12)
    noisy = {}
    rng = np.random.default_rng(1)
    for sym, df in intra.items():
        df = df.copy()
        late = df.index > cut
        df.loc[late, ["Open", "High", "Low", "Close"]] *= rng.uniform(0.5, 1.5, (late.sum(), 1))
        noisy[sym] = df
    other = run_spec_intraday(spec, _panels(cal, noisy), daily, list(NAMES), window, COSTS)
    # A fill at bar t uses bar t's open and signals from bars before it: all unchanged up to the cut.
    keep = lambda t: t[t["date"] <= cut].reset_index(drop=True)  # noqa: E731
    assert len(keep(base.trades)) > 0
    pd.testing.assert_frame_equal(keep(base.trades), keep(other.trades))


def test_panels_are_float32_and_subsets_reuse_them():
    cal, intra, _ = _world(n_sessions=5)
    calls: list[str] = []
    src = _source(cal, intra, calls=calls)
    p = src.panels("1h", list(NAMES), bench="SPY")
    assert p.close.dtype == np.float32 and p.close.shape == (5 * 7, 4)
    assert sorted(calls) == sorted([*NAMES, "SPY"])
    assert src.panels("1h", ["AAA", "CCC"], bench="SPY") is p and len(calls) == 5  # no reload
    p30 = src.panels("30m", list(NAMES), bench="SPY")
    assert p30.timeframe == "30m" and len(calls) == 10


# -- runner, trials, robust -----------------------------------------------------


def test_every_timeframe_counts_as_a_trial(tmp_path):
    settings = _settings(tmp_path / "cfg")
    cal, intra, daily = _world()
    kw = dict(settings=settings, bars=daily, data_dir=tmp_path, today=TODAY)
    src = _source(cal, intra, settings)
    spec = _spec(exit="bars_held >= 4", sizing={"max_positions": 2})
    c15 = score_spec(spec, author_bot="b", intraday=src, **kw)
    c1h = score_spec(spec.with_timeframe("1h"), author_bot="b", intraday=src, **kw)
    score_spec(spec.model_copy(update={"timeframe": None}), author_bot="b", **kw)
    assert trial_stats("discovery", data_dir=tmp_path)["n_trials"] == 3
    assert c15["strategy"]["timeframe"] == "15m" and c1h["strategy"]["timeframe"] == "1h"
    rec = load_trials(spec_hash=spec.hash, data_dir=tmp_path)[0]
    assert rec["spec"]["timeframe"] == "15m"
    assert rec["returns"]["dates"] == [str(d.date()) for d in cal.index]  # daily returns in the log
    with pytest.raises(ValueError, match="specs may declare"):
        score_spec(spec.with_timeframe("30m"), author_bot="b", intraday=src, **kw)


def test_benchmarks_and_matched_returns_for_intraday(tmp_path):
    settings = _settings(tmp_path / "cfg")
    cal, intra, daily = _world()
    src = _source(cal, intra, settings)
    window = _window(cal, 0, 39)
    ev = evaluate(_spec(exit="bars_held >= 4", sizing={"max_positions": 4}), daily, list(NAMES), window,
                  settings, intraday=src)
    ew = ev.benchmarks["equal_weight"]
    assert ev.result.returns.index.equals(ew["returns"].index)
    bars = ev.ew_bars
    entry = pd.Timestamp(ew["entry_date"])
    first = bars.index[bars.index.normalize() == entry][0]
    assert bars.loc[first, "open"] == 1.0 and (bars.loc[bars.index < entry] == 1.0).all().all()
    # The basket's value at a close, from its own 15m closes, matches the daily benchmark.
    day_ret = bars["close"].groupby(bars.index.normalize()).last()
    ew_curve = (1 + ew["returns"]).cumprod()
    assert np.allclose(day_ret.loc[entry:].to_numpy(), ew_curve.loc[entry:].to_numpy(), rtol=1e-5)
    # A strategy that is never invested earns nothing on the matched benchmark.
    idle = SimpleNamespace(
        result=SimpleNamespace(returns=ev.result.returns,
                               bar_exposure=ev.result.bar_exposure * 0.0),
        ew_bars=ev.ew_bars,
    )
    assert (matched_returns(idle) == 0).all()


def test_run_robust_adds_the_timeframe_check_for_intraday_specs(tmp_path):
    settings = _settings(tmp_path / "cfg")
    days = pd.bdate_range("2021-06-01", "2021-12-31")
    cal = _calendar(days)
    intra = _intraday(cal, ["SPY", *NAMES])
    daily = _daily(intra)
    spec = StrategySpec.model_validate({
        "id": "rsi-1h", "timeframe": "1h",
        "indicators": {"rsi": {"fn": "rsi", "length": "$n"}},
        "params": {"n": {"value": 14, "min": 7, "max": 28}},
        "entry": "rsi < 40", "exit": "rsi > 60 or bars_held >= 10", "sizing": {"max_positions": 2},
    })
    rep = run_robust(spec, author_bot="bot1", settings=settings, bars=daily, data_dir=tmp_path, today=TODAY,
                     intraday=_source(cal, intra, settings))
    tf = rep["checks"]["timeframes"]
    assert [v["timeframe"] for v in tf["variants"]] == ["30m", "2h"]
    assert tf["status"] in ("pass", "fail")
    assert trial_stats("discovery", data_dir=tmp_path)["n_trials"] == 1  # neighbours are not trials
    checks = {v["check"] for v in load_trials(kind="robustness", data_dir=tmp_path)}
    assert "timeframe" in checks
    assert rep["peak_memory_mb"] is None or rep["peak_memory_mb"] > 0


def test_peak_memory_reports_a_number():
    mb = peak_memory_mb()
    assert mb is None or mb > 1


# -- sync and coverage ------------------------------------------------------------


class _FakeProvider:
    source = "fake"

    def __init__(self, cal, bars):
        self.cal, self.bars, self.fetched = cal, bars, []

    def calendar(self, start, end):
        return self.cal

    def fetch(self, symbol, start, end):
        self.fetched.append(symbol)
        return self.bars.get(symbol, pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]))


def test_sync_keeps_regular_hours_up_to_the_ceiling(tmp_path):
    days = pd.bdate_range("2021-12-28", "2022-01-04")
    cal = _calendar(days)
    bars = _intraday(cal, ["SPY", "AAA"])
    extra = bars["AAA"].iloc[:1].copy()
    extra.index = [pd.Timestamp("2021-12-28 08:00")]  # pre-market: dropped
    bars["AAA"] = pd.concat([extra, bars["AAA"]]).sort_index()
    prov = _FakeProvider(cal, bars)
    rep = sync_intraday(["SPY", "AAA", "ZZZ"], provider=prov, data_dir=tmp_path, today=TODAY)
    assert rep["fetched"] == ["AAA", "SPY"] and rep["missing"] == ["ZZZ"]
    df = load_intraday("AAA", tmp_path)
    assert df.index.max() == pd.Timestamp("2021-12-31 15:45")  # discovery ceiling
    assert df.index.min() == pd.Timestamp("2021-12-28 09:30") and len(df) == 4 * 26
    assert load_calendar(tmp_path).index.equals(cal.index[cal.index <= "2021-12-31"])
    manifest = json.loads((intraday_dir(tmp_path) / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["tickers"]["AAA"]["outside_hours_dropped"] == 1 + 26 * 2  # pre-market + 2022 bars
    again = sync_intraday(["SPY", "AAA"], provider=prov, data_dir=tmp_path, today=TODAY)
    assert again["already_current"] == 2 and prov.fetched.count("AAA") == 1


def test_provider_never_switches_feeds_when_refused():
    from alpaca.common.exceptions import APIError

    class _Client:
        feeds: list[str] = []

        def get_stock_bars(self, req):
            self.feeds.append(req.feed.value)
            raise APIError("subscription does not permit querying recent SIP data")

    client = _Client()
    prov = AlpacaIntradayProvider(client=client, feed="sip")
    with pytest.raises(RuntimeError, match="Not switching feeds"):
        prov.fetch("AAA", date(2016, 1, 1), date(2016, 12, 31))
    assert client.feeds == ["sip"]


def test_raw_bars_frame_converts_utc_to_new_york():
    df = raw_bars_frame([{"t": "2016-01-04T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "n": 3}])
    assert df.index[0] == pd.Timestamp("2016-01-04 09:30") and df.iloc[0]["Close"] == 1.5


def test_coverage_flags_missing_bars_empty_sessions_and_mismatches(tmp_path):
    cal, intra, daily = _world(n_sessions=30)
    cov = LabCoverageSettings(max_missing_share=0.05, max_empty_sessions=1, max_mismatch_days=0)
    good = ticker_coverage(intra["AAA"], daily["AAA"], cal, date(2016, 1, 1), date(2021, 12, 31), cov)
    assert good["flags"] == [] and good["15m"]["missing_share"] == 0 and good["1h"]["bars"] == 30 * 7
    thin = intra["BBB"].iloc[::3]  # two of every three 15m bars missing
    gaps = thin.loc[~thin.index.normalize().isin(cal.index[5:8])]  # and three empty sessions
    bad_daily = daily["BBB"].copy()
    bad_daily.iloc[10, bad_daily.columns.get_loc("Open")] *= 1.2
    row = ticker_coverage(gaps, bad_daily, cal, date(2016, 1, 1), date(2021, 12, 31), cov)
    assert row["empty_sessions"] == 3 and row["longest_empty_run"] == 3
    assert any("15m" in f for f in row["flags"]) and any("sessions with no bars" in f for f in row["flags"])
    assert row["open_mismatch_days"] >= 1
    one_bar = intra["CCC"].loc[~((intra["CCC"].index.normalize() == cal.index[12])
                                 & (intra["CCC"].index.time > pd.Timestamp("09:30").time()))]
    short = ticker_coverage(one_bar, daily["CCC"], cal, date(2016, 1, 1), date(2021, 12, 31), cov)
    assert short["short_sessions"] == [str(cal.index[12].date())] and short["empty_sessions"] == 0


def test_intraday_coverage_report_from_the_cache(tmp_path):
    from quant_system.backtest.cache import save_bars
    from quant_system.lab.data import bars_dir
    from quant_system.lab.intraday import save_calendar

    cal, intra, daily = _world(n_sessions=25)
    for sym in ("SPY", "AAA"):
        save_intraday(sym, intra[sym], tmp_path)
        save_bars(sym, daily[sym], bars_dir(tmp_path))
    save_calendar(cal, tmp_path)
    rep = intraday_coverage(["SPY", "AAA", "QQQ"], data_dir=tmp_path, today=TODAY)
    assert rep["names"] == 3 and rep["no_bars"] == ["QQQ"]
    assert rep["early_closes"] == [] and rep["median_missing_share"]["15m"] == 0.0
    assert rep["short_session_dates"] == {}
    assert Path(rep["full_report"]).is_file()
    back = load_intraday("AAA", tmp_path)
    assert np.allclose(back["Close"], intra["AAA"]["Close"], atol=1e-4)


def test_intraday_runs_take_turns_on_one_machine(tmp_path):
    import subprocess
    import sys
    import textwrap

    from quant_system.lab.intraday import intraday_run_lock

    child = textwrap.dedent(f"""
        import sys
        from quant_system.lab.intraday import intraday_run_lock
        with intraday_run_lock({str(tmp_path)!r}, wait=lambda: print("waited", flush=True)):
            print("ran", flush=True)
    """)
    with intraday_run_lock(tmp_path):
        proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
        first = proc.stdout.readline().strip()  # blocks until the child reports waiting
    rest = proc.communicate(timeout=60)[0].split()
    assert first == "waited" and rest == ["ran"]
    with intraday_run_lock(tmp_path):  # free again, and reusable
        pass
