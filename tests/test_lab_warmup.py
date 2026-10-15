"""Pre-2016 warm-up bars (yfinance, lined up with Alpaca) and `lab rescore`, on synthetic bars (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import quant_system.lab.nominate as nominate_mod
from quant_system.backtest.cache import save_bars
from quant_system.cli import main
from quant_system.lab.data import (
    bars_dir,
    load_lab_bars,
    read_warmup_manifest,
    splice_warmup,
    sync_warmup,
    warmup_dir,
    warmup_status,
)
from quant_system.lab.rescore import RescoreError, rescore_discovery
from quant_system.lab.runner import score_spec
from quant_system.lab.settings import CEILING_ENV, DATA_DIR_ENV, LabDataSettings, LabSettings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import load_trials, trial_stats
from quant_system.lab_journal import build_days
from tests.test_lab_robust import NAMES, SPEC, _settings

TODAY = date(2026, 10, 5)
SETTINGS = LabSettings()  # data.start 2016-01-01, warm-up 2014-01-01 .. 2016-01-31
SMA200 = {
    "id": "above-sma200",
    "indicators": {"s": {"fn": "sma", "length": 200}},
    "entry": "close > s",
    "exit": "close < s",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in (CEILING_ENV, "QUANT_LAB_BOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(nominate_mod, "fresh_git_state", lambda: ("abc123", False))


def _frame(idx: pd.DatetimeIndex, close: np.ndarray, volume: float = 1e6) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": volume}, index=idx
    )


def _pair(k: float = 0.97, seed: int = 1) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(Alpaca 2016-01-04..2016-06-30, yfinance 2014-01-02..2016-01-29), same prices up to factor k."""
    rng = np.random.default_rng(seed)
    full = pd.bdate_range("2014-01-02", "2016-06-30")
    close = 50 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(full))))
    px = pd.Series(close, index=full)
    alpaca = _frame(full[full >= "2016-01-04"], px[full >= "2016-01-04"].to_numpy())
    yf_idx = full[full <= "2016-01-29"]
    yf = _frame(yf_idx, px[yf_idx].to_numpy() / k)
    return alpaca, yf


def test_splice_scales_warmup_to_alpaca_and_keeps_alpaca_rows():
    alpaca, yf = _pair(k=0.97)
    out, reason = splice_warmup(alpaca, yf, SETTINGS)
    assert reason is None
    early = out.loc[out.index < "2016-01-01"]
    assert early.index.min() == pd.Timestamp("2014-01-02") and len(early) > 400
    # Warm-up prices land on Alpaca's scale; volume is not touched.
    assert np.allclose(early["Close"], yf.loc[early.index, "Close"] * 0.97)
    assert (early["Volume"] == yf.loc[early.index, "Volume"]).all()
    # From data.start on, only Alpaca rows, unchanged.
    pd.testing.assert_frame_equal(out.loc[out.index >= "2016-01-01"], alpaca, check_freq=False)
    assert out.index.is_monotonic_increasing and not out.index.duplicated().any()


@pytest.mark.parametrize(
    "tamper, why",
    [
        (lambda a, y: y.loc["2016-01-15", "Close"] * 1.03, "closes disagree"),
        (None, "volumes disagree"),
        ("short", "shared days"),
    ],
)
def test_splice_refuses_when_the_sources_disagree(tamper, why):
    alpaca, yf = _pair()
    if callable(tamper):
        yf.loc["2016-01-15", "Close"] = tamper(alpaca, yf)
    elif tamper == "short":
        yf = yf.loc[yf.index <= "2016-01-08"]  # 5 shared days
    else:
        yf["Volume"] = yf["Volume"] * 3
    out, reason = splice_warmup(alpaca, yf, SETTINGS)
    assert why in reason
    assert out is alpaca  # starts late, as before


def test_sync_warmup_clips_to_the_warmup_span_and_caches(tmp_path):
    alpaca, yf = _pair()

    class Fake:
        source = "fake"
        calls: list = []

        def fetch(self, symbols, start, end):
            self.calls.append((list(symbols), start, end))
            # Deliberately returns rows past overlap_end so the sync must clip.
            ext = pd.concat([yf, alpaca.loc[alpaca.index > "2016-01-29"]])
            return {s: ext for s in symbols if s != "GONE"}

    prov = Fake()
    rep = sync_warmup(["aaa", "GONE"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    assert prov.calls == [(["AAA", "GONE"], date(2014, 1, 1), date(2016, 1, 31))]
    assert rep["fetched"] == ["AAA"] and rep["missing"] == ["GONE"]
    on_disk = pd.read_csv(warmup_dir(tmp_path) / "AAA.csv", index_col=0, parse_dates=True)
    assert on_disk.index.max() <= pd.Timestamp("2016-01-31")
    assert read_warmup_manifest(tmp_path)["tickers"]["AAA"]["through"] == "2016-01-31"
    again = sync_warmup(["AAA", "GONE"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    assert again["already_current"] == 2 and len(prov.calls) == 1


def test_sync_warmup_refuses_an_overlap_past_the_ceiling(tmp_path):
    s = LabSettings(data=LabDataSettings(ceiling=date(2016, 1, 15)))
    with pytest.raises(ValueError, match="past this machine's data ceiling"):
        sync_warmup(["AAA"], provider=object(), settings=s, data_dir=tmp_path, today=TODAY)


def test_load_lab_bars_splices_and_status_lists_rejects(tmp_path):
    alpaca, yf = _pair()
    for sym in ("AAA", "BAD", "NOWARM"):
        save_bars(sym, alpaca, bars_dir(tmp_path))
    save_bars("AAA", yf, warmup_dir(tmp_path))
    save_bars("BAD", yf.assign(Volume=yf["Volume"] * 10), warmup_dir(tmp_path))
    (warmup_dir(tmp_path) / "_manifest.json").write_text(
        json.dumps({"tickers": {"AAA": {}, "BAD": {}}, "missing": {}}), encoding="utf-8"
    )
    bars = load_lab_bars(["AAA", "BAD", "NOWARM"], settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    assert bars["AAA"].index.min() == pd.Timestamp("2014-01-02")
    assert bars["BAD"].index.min() == bars["NOWARM"].index.min() == pd.Timestamp("2016-01-04")
    status = warmup_status(settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    assert status["tickers"] == 2 and list(status["rejected"]) == ["BAD"]


def _long_bars(symbols, start="2014-01-02", seed=3) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, "2021-12-31")
    idx = idx[idx != "2016-01-01"]  # New Year's Day: the window's first session is Jan 4, as in real data
    mkt = np.cumsum(rng.normal(0.0004, 0.01, len(idx)))
    out = {}
    for sym in ["SPY", *symbols]:
        c = 40 * np.exp(mkt + (0 if sym == "SPY" else np.cumsum(rng.normal(0, 0.015, len(idx)))))
        o = c * np.exp(rng.normal(0, 0.004, len(idx)))
        out[sym] = pd.DataFrame(
            {"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c), "Close": c, "Volume": 1e8 / c},
            index=idx,
        )
    return out


def test_warmup_lets_long_lookbacks_and_benchmarks_start_in_the_first_week(tmp_path):
    settings = _settings(tmp_path / "cfg")
    spec = StrategySpec.model_validate(SMA200)
    warm = score_spec(spec, author_bot="t", settings=settings, bars=_long_bars(NAMES), data_dir=tmp_path / "a",
                      today=TODAY)
    # Scored from the window's first session; warm-up rows never enter the returns.
    assert warm["strategy"]["start"] == "2016-01-04"
    assert warm["strategy"]["first_trade_date"] == "2016-01-05"
    assert {b["entry_date"] for b in warm["benchmarks"].values()} == {"2016-01-05"}
    assert warm["universe"]["warmup_names"] == len(NAMES)

    cold = score_spec(spec, author_bot="t", settings=settings, bars=_long_bars(NAMES, start="2016-01-04"),
                      data_dir=tmp_path / "b", today=TODAY)
    assert cold["universe"]["warmup_names"] == 0
    assert cold["strategy"]["first_trade_date"] > "2016-10-01"  # 200 bars of history first
    assert cold["benchmarks"]["SPY"]["entry_date"] > "2016-01-29"  # 20 bars for the cost model


def test_rescore_replaces_scores_without_adding_trials(tmp_path):
    settings = _settings(tmp_path / "cfg")
    cold = _long_bars(NAMES, start="2016-01-04")
    for bot, raw in (("momo", SMA200), ("snapback", SPEC)):
        score_spec(StrategySpec.model_validate(raw), author_bot=bot, settings=settings, bars=cold,
                   data_dir=tmp_path, today=TODAY)
    # An old record whose spec no longer hashes the same must be skipped, not rerun as a new trial.
    odd = load_trials(data_dir=tmp_path)[0]
    rec = json.loads(Path(odd["path"]).read_text(encoding="utf-8"))
    rec["spec_hash"] = "0000000000000000"
    Path(odd["path"]).with_name("odd.json").write_text(json.dumps(rec), encoding="utf-8")
    before = trial_stats("discovery", data_dir=tmp_path)
    assert before["n_trials"] == 3
    journal_before = build_days(load_trials(data_dir=tmp_path), [], {}, None)

    rep = rescore_discovery(settings=settings, bars=_long_bars(NAMES), data_dir=tmp_path, today=TODAY)
    assert rep["n_trials"] == 3 and rep["rescored"] == 2
    assert [x["spec_hash"] for x in rep["skipped"]] == ["0000000000000000"]
    assert {x["author_bot"] for x in rep["specs"]} == {"momo", "snapback"}
    assert all(x["warmup_names"] == len(NAMES) for x in rep["specs"])
    after = trial_stats("discovery", data_dir=tmp_path)
    assert after["n_trials"] == before["n_trials"] and after["n_records"] == before["n_records"] + 2
    new = [r for r in load_trials(data_dir=tmp_path) if r.get("rescore")]
    assert len(new) == 2 and {r["author_bot"] for r in new} == {"momo", "snapback"}

    # The journal doesn't count reruns as the bots' work for the day.
    journal_after = build_days(load_trials(data_dir=tmp_path), [], {}, None)
    assert [d["summary"]["ideas"] for d in journal_after.values()] == [3]
    assert journal_after == journal_before


def test_rescore_needs_a_clean_checkout_and_warmup_bars(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(nominate_mod, "fresh_git_state", lambda: ("abc123", True))
    with pytest.raises(RescoreError, match="local edits"):
        rescore_discovery(settings=SETTINGS, bars={}, data_dir=tmp_path, today=TODAY)

    monkeypatch.setattr(nominate_mod, "fresh_git_state", lambda: ("abc123", False))
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    assert main(["lab", "rescore"]) == 1
    assert "run `lab data warmup` first" in json.loads(capsys.readouterr().out)["error"]


def test_cli_lab_data_warmup_parses():
    from quant_system.cli import build_parser

    args = build_parser().parse_args(["lab", "data", "warmup", "--tickers", "AAA,BBB"])
    assert args.tickers == "AAA,BBB" and args.force is False and args.lab_data_command == "warmup"
    assert build_parser().parse_args(["lab", "rescore"]).lab_command == "rescore"
