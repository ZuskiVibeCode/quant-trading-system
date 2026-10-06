"""`lab nominate`, `lab validate`, `lab holdout` on synthetic bars (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

import quant_system.lab.final as final
import quant_system.lab.nominate as nominate_mod
import quant_system.lab.robust as robust_mod
import quant_system.lab.trials as trials_mod
from quant_system.backtest.cache import save_bars
from quant_system.cli import main
from quant_system.config.loader import load_config
from quant_system.lab.data import bars_dir
from quant_system.lab.final import check_trial_count_order, run_holdout, run_validation
from quant_system.lab.nominate import NominationError, gate_failures, load_nomination, nominate
from quant_system.lab.robust import run_robust
from quant_system.lab.settings import CEILING_ENV, DATA_DIR_ENV, LabRobustSettings, load_lab_settings
from quant_system.lab.spec import StrategySpec
from quant_system.lab.trials import load_trials, record_trial, trial_stats
from quant_system.lab.universe import universe_symbols
from quant_system.lab.windows import UNLOCK_ENV, WindowLocked

TODAY = date(2026, 10, 5)
NAMES = {  # symbol: (sector, cap bucket)
    "AAA": ("Tech", "large"), "BBB": ("Tech", "large"), "CCC": ("Tech", "mid"), "DDD": ("Tech", "mid"),
    "EEE": ("Energy", "large"), "FFF": ("Energy", "large"), "GGG": ("Energy", "mid"), "HHH": ("Energy", "mid"),
}
SPEC = {
    "id": "rsi-dip",
    "indicators": {"rsi": {"fn": "rsi", "length": "$n"}},
    "params": {"n": {"value": 14, "min": 7, "max": 28}},
    "entry": "rsi < 40",
    "exit": "rsi > 60 or bars_held >= 10",
    "sizing": {"max_positions": 4},
}
NEVER = {"id": "never", "entry": "close < 0", "exit": "False"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in (UNLOCK_ENV, "QUANT_LAB_BOT"):
        monkeypatch.delenv(key, raising=False)
    # Locked windows need bars past 2021 on the "validation machine".
    monkeypatch.setenv(CEILING_ENV, "today")
    # Tests run in whatever state the developer's checkout is in: pin a clean one.
    clean = lambda: ("abc123", False)  # noqa: E731
    for mod, name in ((nominate_mod, "fresh_git_state"), (trials_mod, "git_state"), (robust_mod, "git_state")):
        monkeypatch.setattr(mod, name, clean)


@pytest.fixture(scope="module")
def bars() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(11)
    idx = pd.bdate_range("2016-01-04", "2026-10-02")
    mkt = np.cumsum(rng.normal(0.0004, 0.01, len(idx)))
    out = {}
    for sym in ["SPY", *NAMES, *universe_symbols("themes")]:
        c = 40 * np.exp(mkt + (0 if sym == "SPY" else np.cumsum(rng.normal(0, 0.015, len(idx)))))
        o = c * np.exp(rng.normal(0, 0.004, len(idx)))
        out[sym] = pd.DataFrame(
            {"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c), "Close": c, "Volume": 1e8 / c},
            index=idx,
        )
    return out


@pytest.fixture
def settings(tmp_path):
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    (cdir / "lab.yaml").write_text("universe_file: u.yaml\n", encoding="utf-8")
    rows = [{"symbol": s, "sector": sec, "cap_bucket": cap} for s, (sec, cap) in NAMES.items()]
    (cdir / "u.yaml").write_text(yaml.safe_dump({"tickers": rows}), encoding="utf-8")
    s = load_lab_settings(cdir)
    return s.model_copy(update={"robust": LabRobustSettings(min_subset_names=2)})


@pytest.fixture
def passing(monkeypatch):
    """The real robustness run (trials logged as usual), with every check forced to pass.

    The gate reads the spec's own verdict from the trial log, so the logged base
    run is marked as beating the benchmarks too.
    """

    def forced(*args, **kwargs):
        rep = run_robust(*args, **kwargs)
        rep.update(passed=True, complete=True)
        rep["base"]["passed"] = True
        for check in rep["checks"].values():
            check["status"] = "pass"
        path = Path(rep["base"]["trial_file"])
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["metrics"]["beats_benchmarks"] = True
        path.write_text(json.dumps(rec), encoding="utf-8")
        return rep

    monkeypatch.setattr(nominate_mod, "run_robust", forced)
    monkeypatch.setattr(final, "run_robust", forced)


def _packet(spec: StrategySpec, n_trials: int = 3, at: str = "2026-10-01T00:00:00+00:00") -> dict:
    return {
        "schema": 1, "kind": "lab_nomination", "spec_id": spec.id, "spec_hash": spec.hash,
        "author_bot": "alpha", "nominated_at": at, "git_commit": "abc123",
        "frozen_trials": {"window": "discovery", "n_trials": n_trials, "n_records": n_trials,
                          "sharpe_variance": 0.04},
        "spec": spec.canonical(),
    }


def _log_validation(data_dir, spec: StrategySpec, *, passed: bool, n_trials: int, at: str):
    record_trial(
        spec=spec.canonical(), window="validation", author_bot="alpha",
        metrics={"sharpe": 1.0, "passed": passed}, returns=pd.Series([0.01], index=[pd.Timestamp("2022-01-03")]),
        extra={"frozen_trials": {"window": "discovery", "n_trials": n_trials, "n_records": n_trials,
                                 "sharpe_variance": 0.09},
               "nomination": {"spec_hash": spec.hash, "nominated_at": at}},
        data_dir=data_dir,
    )


def _base_record(data_dir, *, beats: bool, dirty: bool | None = False) -> dict:
    """A logged discovery run for SPEC and a matching robust report that passes everything."""
    spec = StrategySpec.model_validate(SPEC)
    vs = {"SPY": {"sharpe_diff": 0.2, "excess_cagr": 0.01}, "equal_weight": {"sharpe_diff": 0.1, "excess_cagr": 0.01}}
    path = record_trial(spec=spec.canonical(), window="discovery", author_bot="alpha", data_dir=data_dir,
                        metrics={"sharpe": 1.0, "beats_benchmarks": beats, "vs_benchmarks": vs},
                        returns=pd.Series([0.01], index=[pd.Timestamp("2017-01-03")]))
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["git_dirty"] = dirty
    path.write_text(json.dumps(rec), encoding="utf-8")
    return {"spec_hash": spec.hash, "passed": True, "complete": True, "git_dirty": False,
            "base": {"passed": True, "trial_file": str(path)},
            "checks": {"nudges": {"status": "pass"}, "sectors": {"status": "pass"}}}


def test_gate_recomputes_the_verdict_from_the_trial_log(tmp_path):
    rep = _base_record(tmp_path / "a", beats=True)
    assert gate_failures(rep, data_dir=tmp_path / "a") == []

    # The report says passed, but the logged run didn't beat the benchmarks.
    rep = _base_record(tmp_path / "b", beats=False)
    assert gate_failures(rep, data_dir=tmp_path / "b") == [
        "spec does not beat SPY and equal-weight after costs on discovery"
    ]
    # A report whose base run isn't in this trial log at all.
    assert "not in the trial log" in gate_failures(rep, data_dir=tmp_path / "a")[0]

    rep = _base_record(tmp_path / "c", beats=True, dirty=True)
    assert gate_failures(rep, data_dir=tmp_path / "c") == ["discovery record git_dirty=True; needs a clean checkout"]

    rep = _base_record(tmp_path / "d", beats=True)
    rep.update(git_dirty=None, complete=False)
    rep["checks"].update(sectors={"status": "fail"}, single_stock={"status": "skipped", "reason": "none"})
    reasons = gate_failures(rep, data_dir=tmp_path / "d")
    assert reasons[0].startswith("robust report git_dirty=None")
    assert reasons[1].startswith("robust report complete=false")
    assert reasons[2] == "sectors: fail" and "complete report" in reasons[3]


def test_nominate_refuses_a_dirty_checkout(tmp_path, monkeypatch, settings, bars):
    monkeypatch.setattr(nominate_mod, "fresh_git_state", lambda: ("abc123", True))
    with pytest.raises(NominationError, match="local edits"):
        nominate(StrategySpec.model_validate(SPEC), settings=settings, bars=bars, data_dir=tmp_path,
                 out_dir=tmp_path / "noms", today=TODAY)
    assert load_trials(data_dir=tmp_path) == []  # refused before any run


def test_nominate_a_failing_spec_writes_no_packet(tmp_path, settings, bars):
    res = nominate(StrategySpec.model_validate(NEVER), settings=settings, bars=bars, data_dir=tmp_path,
                   out_dir=tmp_path / "noms", today=TODAY)
    assert res["nominated"] is False and not (tmp_path / "noms").exists()
    assert any("does not beat" in r for r in res["reasons"])
    assert any("skipped" in r for r in res["reasons"])  # no settings to nudge: incomplete


def test_nominate_freezes_the_trial_count(tmp_path, settings, bars, passing):
    for i in range(2):  # earlier discovery trials by other bots
        other = StrategySpec.model_validate({**NEVER, "entry": f"close < {-i - 1}"})
        record_trial(spec=other.canonical(), window="discovery", author_bot="b", data_dir=tmp_path,
                     metrics={"sharpe": 0.1 * i}, returns=pd.Series([0.0], index=[pd.Timestamp("2017-01-03")]))
    spec = StrategySpec.model_validate(SPEC)
    res = nominate(spec, author_bot="alpha", settings=settings, bars=bars, data_dir=tmp_path,
                   out_dir=tmp_path / "noms", today=TODAY)
    assert res["nominated"] is True
    path = Path(res["packet"])
    assert path.name == f"rsi-dip-{spec.hash}.json"
    packet, loaded = load_nomination(path)
    assert loaded.hash == spec.hash and packet["author_bot"] == "alpha" and packet["git_commit"] == "abc123"
    assert packet["frozen_trials"]["n_trials"] == 3 == trial_stats("discovery", data_dir=tmp_path)["n_trials"]
    assert set(packet["discovery"]["checks"]) == {"nudges", "sectors", "cap_buckets", "selloffs", "single_stock"}

    # A packet edited after nomination no longer matches its hash.
    tampered = {**packet, "spec": {**packet["spec"], "entry": "rsi < 45"}}
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(NominationError, match="hashes to"):
        load_nomination(path)
    path.write_text(json.dumps({**packet, "frozen_trials": {"window": "discovery", "n_trials": 0}}), encoding="utf-8")
    with pytest.raises(NominationError, match="frozen_trials"):
        load_nomination(path)


def test_validate_and_holdout_refuse_off_the_validation_machine(tmp_path, monkeypatch, settings, bars):
    def boom(*a, **k):
        raise AssertionError("recheck ran on a locked window")

    monkeypatch.setattr(final, "run_robust", boom)
    spec = StrategySpec.model_validate(SPEC)
    kw = dict(settings=settings, bars=bars, data_dir=tmp_path, today=TODAY)
    with pytest.raises(WindowLocked, match="validation is locked"):
        run_validation(_packet(spec), spec, **kw)
    with pytest.raises(WindowLocked, match="holdout is locked"):
        run_holdout(_packet(spec), spec, **kw)
    monkeypatch.setenv(UNLOCK_ENV, "validation,holdout")
    monkeypatch.delenv(CEILING_ENV)  # Grok's machine: ceiling at the end of discovery
    for runner in (run_validation, run_holdout):
        with pytest.raises(WindowLocked, match="never on Grok's"):
            runner(_packet(spec), spec, **kw)
    monkeypatch.setenv(CEILING_ENV, "2021-12-31")
    with pytest.raises(WindowLocked, match="never on Grok's"):
        run_validation(_packet(spec), spec, **kw)
    monkeypatch.setenv(CEILING_ENV, "today")
    with pytest.raises(NominationError, match="packet is for"):
        run_validation(_packet(StrategySpec.model_validate(NEVER)), spec, settings=settings, bars=bars,
                       data_dir=tmp_path, today=TODAY)


def test_validate_rechecks_discovery_then_runs_once(tmp_path, monkeypatch, settings, bars, passing):
    monkeypatch.setenv(UNLOCK_ENV, "validation")
    spec = StrategySpec.model_validate(SPEC)
    packet = _packet(spec, n_trials=12)
    card = run_validation(packet, spec, settings=settings, bars=bars, data_dir=tmp_path, today=TODAY)

    assert card["ran"] is True and card["window"]["name"] == "validation"
    assert card["window"]["start"] == "2022-01-01" and card["window"]["end"] == "2024-12-31"
    assert card["trials"]["n_trials"] == 12  # the frozen count, not this machine's log
    assert set(card["verdict"]["checks"]) == {"beats_benchmarks", "deflated_sharpe"}
    assert card["recheck"]["passed"] is True

    (rec,) = load_trials(window="validation", data_dir=tmp_path)
    assert Path(card["trial_file"]).name == Path(rec["path"]).name  # recorded before it was returned
    assert rec["frozen_trials"]["n_trials"] == 12 and rec["nomination"]["nominated_at"] == packet["nominated_at"]
    assert rec["metrics"]["passed"] == card["verdict"]["passed"]
    assert rec["metrics"]["deflated_sharpe"] == card["deflated_sharpe"]
    b = rec["breakouts"]
    assert [x["name"] for x in b["selloffs"]] == ["bear_2022"]
    assert set(b["themes"]) == {"themes", *load_config().universe.themes}
    assert all(t["names"] > 0 for t in b["themes"].values())
    # The discovery recheck ran here and was logged in this machine's log.
    assert trial_stats("discovery", data_dir=tmp_path)["n_trials"] == 1

    with pytest.raises(WindowLocked, match="one run per spec"):
        run_validation(packet, spec, settings=settings, bars=bars, data_dir=tmp_path, today=TODAY)


def test_validate_stops_when_the_discovery_recheck_fails(tmp_path, monkeypatch, settings, bars):
    monkeypatch.setenv(UNLOCK_ENV, "validation")
    spec = StrategySpec.model_validate(NEVER)
    res = run_validation(_packet(spec), spec, settings=settings, bars=bars, data_dir=tmp_path, today=TODAY)
    assert res["ran"] is False and all(r.startswith("discovery recheck:") for r in res["reasons"])
    assert "verdict" not in res and "strategy" not in res
    assert load_trials(window="validation", data_dir=tmp_path) == []


def test_the_frozen_trial_count_may_not_go_backwards(tmp_path):
    spec = StrategySpec.model_validate(SPEC)
    other = StrategySpec.model_validate(NEVER)
    _log_validation(tmp_path, other, passed=False, n_trials=10, at="2026-10-01T00:00:00+00:00")
    check_trial_count_order(_packet(spec, 12, "2026-10-02T00:00:00+00:00"), data_dir=tmp_path)
    check_trial_count_order(_packet(spec, 10, "2026-10-02T00:00:00+00:00"), data_dir=tmp_path)
    check_trial_count_order(_packet(spec, 8, "2026-09-30T00:00:00+00:00"), data_dir=tmp_path)
    with pytest.raises(NominationError, match="went backwards"):
        check_trial_count_order(_packet(spec, 9, "2026-10-02T00:00:00+00:00"), data_dir=tmp_path)
    with pytest.raises(NominationError, match="went backwards"):
        check_trial_count_order(_packet(spec, 11, "2026-09-30T00:00:00+00:00"), data_dir=tmp_path)


def test_holdout_is_for_validation_survivors_only(tmp_path, monkeypatch, settings, bars):
    monkeypatch.setenv(UNLOCK_ENV, "holdout")
    spec = StrategySpec.model_validate(SPEC)
    packet = _packet(spec, n_trials=3)
    kw = dict(settings=settings, bars=bars, data_dir=tmp_path, today=TODAY)
    with pytest.raises(NominationError, match="no validation run"):
        run_holdout(packet, spec, **kw)
    _log_validation(tmp_path, spec, passed=False, n_trials=7, at=packet["nominated_at"])
    with pytest.raises(NominationError, match="did not pass"):
        run_holdout(packet, spec, **kw)

    _log_validation(tmp_path, spec, passed=True, n_trials=7, at=packet["nominated_at"])
    card = run_holdout(packet, spec, **kw)
    assert card["ran"] is True and card["window"]["name"] == "holdout"
    assert card["window"]["end"] == "2026-10-04"  # through the data ceiling
    assert card["trials"]["n_trials"] == 7  # frozen in the validation record, not the packet
    assert "recheck" not in card and card["breakouts"]["selloffs"] == []
    (rec,) = load_trials(window="holdout", data_dir=tmp_path)
    assert rec["frozen_trials"]["n_trials"] == 7 and "breakouts" in rec
    with pytest.raises(WindowLocked, match="one run per spec"):
        run_holdout(packet, spec, **kw)


def test_cli_nominate_validate_holdout(tmp_path, monkeypatch, capsys, bars):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    names = ["SPY", *universe_symbols("quantum")]
    for sym in names:
        save_bars(sym, bars[sym], bars_dir(tmp_path))
    spec_path = tmp_path / "s.yaml"
    spec_path.write_text(yaml.safe_dump({**NEVER, "universe": "quantum"}), encoding="utf-8")

    assert main(["lab", "nominate", str(spec_path), "--bot", "clibot", "--out", str(tmp_path / "noms")]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["nominated"] is False and out["reasons"]

    spec = StrategySpec.model_validate({**NEVER, "universe": "quantum"})
    packet_path = tmp_path / "p.json"
    packet_path.write_text(json.dumps(_packet(spec)), encoding="utf-8")
    for cmd in ("validate", "holdout"):
        assert main(["lab", cmd, str(packet_path)]) == 1
        assert "locked" in json.loads(capsys.readouterr().out)["error"]
    packet_path.write_text(json.dumps({**_packet(spec), "spec_hash": "0" * 16}), encoding="utf-8")
    assert main(["lab", "validate", str(packet_path)]) == 1
    assert "hashes to" in json.loads(capsys.readouterr().out)["error"]
