"""Lab journal export (docs/data/lab/) from real lab output on synthetic bars (no network)."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from quant_system.cli import main
from quant_system.lab.robust import run_robust
from quant_system.lab.runner import score_spec
from quant_system.lab.spec import StrategySpec
from quant_system.lab_journal import describe_rule, describe_spec, export_lab_journal
from scripts.build_orbit_slim_deploy import copy_lab_journal
from tests.test_lab_robust import NAMES, SPEC, _bars, _settings

DAY = "2026-10-13"
PREV = "2026-10-12"
STAMP = "2026-10-13T15:00:00+00:00"  # 11 AM ET
TODAY = date(2026, 10, 13)
BREAKOUT = {
    "id": "breakout-vol",
    "description": "Buy 55-day highs on heavy volume.",
    "indicators": {"hi": {"fn": "donchian_high", "length": "$n"}, "rv": {"fn": "rel_volume", "length": 20}},
    "params": {"n": {"value": 55, "min": 20, "max": 100}},
    "entry": "close > hi and rv > 1.5",
    "exit": "crosses_below(close, prev(hi, 5)) or bars_held >= 40",
    "rank": "rv",
    "sizing": {"max_positions": 8},
}


def _restamp(folder: Path, iso: str) -> None:
    for path in folder.glob("*.json"):
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["recorded_at"] = iso
        path.write_text(json.dumps(rec), encoding="utf-8")


@pytest.fixture(scope="module")
def lab_data(tmp_path_factory) -> Path:
    """Trial log + robust report from one `lab robust` (snapback) and one `lab run` (momo)."""
    root = tmp_path_factory.mktemp("lab")
    data = root / "data"
    settings = _settings(root / "cfg")
    bars = _bars(NAMES)
    dip = StrategySpec.model_validate({**SPEC, "description": "Buy oversold dips.", "rank": "-rsi"})
    run_robust(dip, author_bot="snapback", settings=settings, bars=bars, data_dir=data, today=date(2026, 10, 5))
    score_spec(StrategySpec.model_validate(BREAKOUT), author_bot="momo", settings=settings, bars=bars,
               data_dir=data, today=date(2026, 10, 5))
    _restamp(data / "trials", STAMP)
    _restamp(data / "robust", STAMP)
    return data


def _prs() -> list[dict]:
    return [
        {
            "number": 21, "title": "feat(lab): cup_handle breakout indicator", "state": "MERGED",
            "body": "Author bot: smith\n\nAdds cup_handle for momo.", "headRefName": "smith/indicator-cup-handle",
            "createdAt": "2026-10-13T14:25:21Z", "mergedAt": "2026-10-13T15:15:16Z", "closedAt": None,
            "url": "https://example.test/pull/21",
            "files": [{"path": "quant_system/lab/indicators/patterns.py"}, {"path": "tests/test_lab_cup_handle.py"}],
            "comments": [{"body": "Skeptic: no objection", "createdAt": "2026-10-13T15:11:11Z", "url": "u21"}],
        },
        {
            "number": 30, "title": "nominate rsi-dip", "state": "OPEN",
            "body": "Author bot: snapback\n\nDip buying.", "headRefName": "snapback/nominate-rsi-dip",
            "createdAt": "2026-10-13T18:00:00Z", "mergedAt": None, "closedAt": None, "url": "https://example.test/pull/30",
            "files": [{"path": "lab_nominations/rsi-dip-abc.json"}],
            "comments": [{
                "body": "Skeptic: objection\nTop contributor carries 61% of profit.\nSee the holdout result 9.87 for this.",
                "createdAt": "2026-10-13T19:00:00Z", "url": "u30",
            }],
        },
        {  # A Claude PR on another day: not lab activity.
            "number": 20, "title": "fix(orbit): something", "state": "MERGED", "body": "", "headRefName": "claude/x",
            "createdAt": "2026-10-10T19:41:34Z", "mergedAt": "2026-10-10T19:53:56Z", "url": "u20",
            "files": [{"path": "quant_system/dashboard_export.py"}], "comments": [],
        },
    ]


def _lab_work(root: Path) -> Path:
    work = root / "lab-work"
    (work / "snapback").mkdir(parents=True, exist_ok=True)
    (work / "momo").mkdir(parents=True, exist_ok=True)
    (work / "snapback" / "notes.md").write_text(  # the format Orbit Control reported on Oct 13
        "# snapback notes\n"
        "## 2026-10-12 (Mon) afternoon session\n"
        "- rsi-dip | abcd1234abcd1234 | 0.80 (CAGR 7%, DD 22%) | fail | Older note from yesterday.\n"
        "## 2026-10-13 (Tue) afternoon session: 10 specs\n"
        "- rsi-dip | abcd1234abcd1234 | 0.92 (CAGR 8.7%, DD 20%) | fail | Nudges fragile; try a trend filter.\n"
        "- rs-pullback-quiet | 0000000000000000 | 0.50 (CAGR 4%, DD 13%) | fail | Refinement of rsi-dip: cut DD.\n"
        "- rsi-dip: validation Sharpe 9.87 (must never show)\n"
        "- rsi-dip-wide: a different spec\n"
        "Lessons: the quiet filter cuts drawdown\n"
        "but costs exposure.\n"
        "\n"
        "Next session: use the quiet filter as a rank input. No nominations today.\n",
        encoding="utf-8",
    )
    (work / "momo" / "notes.md").write_text("| id | lesson |\n|---|---|\n| breakout-vol | volume filter too loose |\n",
                                            encoding="utf-8")
    return work


def _export(lab_data: Path, tmp_path: Path, prs=None, **kw) -> tuple[dict, Path]:
    out = tmp_path / "out"
    kw.setdefault("today", TODAY)
    res = export_lab_journal(out, data_dir=lab_data, lab_work=_lab_work(tmp_path), prs=_prs() if prs is None else prs, **kw)
    return res, out


def _day(out: Path, day: str = DAY) -> dict:
    return json.loads((out / f"{day}.json").read_text(encoding="utf-8"))


def test_rules_read_as_plain_english():
    names = {"rsi": "rsi(14)", "lvl": "30", "trend": "sma(200)", "bars_held": "days held"}
    assert describe_rule("rsi < lvl and close > trend", names) == "rsi(14) is below 30 and close is above sma(200)"
    assert describe_rule("20 < rsi < 40", names) == "rsi(14) is between 20 and 40"
    assert describe_rule("30 > rsi", names) == "rsi(14) is below 30"
    assert describe_rule("rsi > 55 or bars_held >= 20", names) == "rsi(14) is above 55 or held 20 days or more"
    assert describe_rule("crosses_above(close, prev(trend))", names) == "close crosses above yesterday's sma(200)"
    assert describe_rule("(a or b) and c", {}) == "(a or b) and c"
    assert describe_rule("not valid ((", {}) == "not valid (("

    w = describe_spec({**BREAKOUT, "params": {"n": {"value": 55, "min": 20, "max": 100}}})
    assert w["buy"] == "close is above donchian_high(55) and rel_volume(20) is above 1.5"
    assert w["sell"] == "close crosses below donchian_high(55) from 5 days earlier or held 40 days or more"
    assert w["order"] == "buys the highest rel_volume(20) first"
    assert w["size"] == "up to 8 stocks at 12.5% of the account each"
    assert w["universe"] == "broad"
    assert w["indicators"][0]["about"].startswith("Highest high")
    assert w["settings"] == [{"name": "n", "value": 55, "min": 20, "max": 100}]
    assert describe_spec({**SPEC, "rank": "-rsi"})["order"] == "buys the lowest rsi(14) first"


def test_export_writes_one_small_file_for_the_day(lab_data, tmp_path):
    res, out = _export(lab_data, tmp_path)
    assert res["written"] == [PREV, DAY]  # Oct 12 has session notes only
    assert sorted(p.name for p in out.iterdir()) == [f"{PREV}.json", f"{DAY}.json", "index.json"]
    d = _day(out)
    assert (out / f"{DAY}.json").stat().st_size < 20_000

    ideas = {i["spec_id"]: i for i in d["ideas"]}
    assert [i["bot"] for i in d["ideas"]] == ["momo", "snapback"]
    dip, brk = ideas["rsi-dip"], ideas["breakout-vol"]
    for idea in (dip, brk):
        assert idea["category"] is None
        assert idea["new_today"] is True
        assert idea["run"]["start"] >= "2016-01-01" and idea["run"]["end"] <= "2021-12-31"
    assert brk["description"] == "Buy 55-day highs on heavy volume."
    assert brk["robust"] is None and brk["stage"] == "missed"
    assert set(brk["run"]["missed"]) <= {"SPY Sharpe", "SPY CAGR", "equal-weight Sharpe", "equal-weight CAGR"}
    assert brk["notes"] == ["volume filter too loose"]

    rob = dip["robust"]
    assert [c["name"] for c in rob["checks"]] == ["nudges", "sectors", "cap_buckets", "selloffs", "single_stock"]
    assert rob["failed"] == [c["name"] for c in rob["checks"] if c["status"] == "fail"]
    assert all(c["detail"] for c in rob["checks"])
    assert dip["robust_variants_today"] > 0
    assert dip["notes"] == ["Nudges fragile; try a trend filter."]  # not yesterday's, not rs-pullback-quiet's
    assert d["session_notes"] == {"snapback": [{"session": "afternoon session: 10 specs", "notes": [
        "rs-pullback-quiet: Refinement of rsi-dip: cut DD.",
        "rsi-dip-wide: a different spec",
        "Lessons: the quiet filter cuts drawdown but costs exposure.",
        "Next session: use the quiet filter as a rank input. No nominations today.",
    ]}]}
    prev = _day(out, PREV)
    assert prev["ideas"] == []
    assert prev["session_notes"]["snapback"][0]["notes"] == ["rsi-dip: Older note from yesterday."]
    assert dip["nomination"] == {"pr": 30, "url": "https://example.test/pull/30", "state": "open",
                                 "opened_day": DAY, "skeptic": {"verdict": "objection", "at": "2026-10-13T19:00:00Z"}}
    assert dip["stage"] == "nominated"

    assert [x["pr"] for x in d["indicators"]] == [21]
    assert d["indicators"][0]["indicators"][0]["name"] == "cup_handle"
    assert d["indicators"][0]["bot"] == "smith"
    assert d["indicators"][0]["summary"] == "Adds cup_handle for momo."
    assert [(v["pr"], v["verdict"]) for v in d["skeptic"]] == [(21, "no objection"), (30, "objection")]
    assert d["skeptic"][1]["evidence"] == "Top contributor carries 61% of profit."
    assert d["summary"]["ideas"] == 2 and d["summary"]["indicators_merged"] == 1
    assert d["summary"]["robust_runs"] == 1 and d["bots"]["snapback"]["robust"] == 1

    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert [r["date"] for r in index["days"]] == [PREV, DAY]
    assert index["days"][1]["ideas"] == 2


def test_validation_and_holdout_never_reach_the_journal(lab_data, tmp_path):
    data = tmp_path / "data"
    shutil.copytree(lab_data, data)
    src = next((data / "trials").glob("*momo*.json"))
    for window in ("validation", "holdout"):
        rec = json.loads(src.read_text(encoding="utf-8"))
        rec.update(window=window, spec_hash=f"locked{window}", spec_id=f"locked-{window}")
        rec["metrics"]["sharpe"] = 9.87
        (data / "trials" / f"20261013T160000000000Z-momo-{window}.json").write_text(json.dumps(rec), encoding="utf-8")
    rep = json.loads(next((data / "robust").glob("*.json")).read_text(encoding="utf-8"))
    rep["window"] = {"name": "validation", "start": "2022-01-01", "end": "2024-12-31"}
    rep["spec_hash"] = "lockedrobust"
    (data / "robust" / "locked.json").write_text(json.dumps(rep), encoding="utf-8")
    saved = tmp_path / "lab-work" / "snapback"
    saved.mkdir(parents=True)
    (saved / "holdout-run.json").write_text(json.dumps({**rep, "window": {"name": "holdout"}}), encoding="utf-8")

    res, out = _export(data, tmp_path)
    assert res["written"] == [PREV, DAY] and res["refused"] == []
    text = "".join(p.read_text(encoding="utf-8") for p in out.glob("*.json"))
    assert "9.87" not in text
    assert "locked" not in text
    assert "validation" not in text.lower() and "holdout" not in text.lower()


def test_rerun_rewrites_nothing_and_github_outage_keeps_pr_lists(lab_data, tmp_path):
    _export(lab_data, tmp_path)
    res, out = _export(lab_data, tmp_path)
    assert res["written"] == []

    res, out = _export(lab_data, tmp_path, prs=[])  # GitHub read fine, no PRs
    assert _day(out)["indicators"] == []
    _export(lab_data, tmp_path)
    res = export_lab_journal(out, data_dir=lab_data, lab_work=_lab_work(tmp_path), prs=None, today=TODAY)
    d = _day(out)
    assert res["github"] is False
    assert [x["pr"] for x in d["indicators"]] == [21] and len(d["skeptic"]) == 2


def test_days_without_activity_get_no_file(lab_data, tmp_path):
    res, out = _export(lab_data, tmp_path, prs=[], days=1, today=date(2026, 10, 14))
    assert res["written"] == [] and not list(out.glob("20*.json"))
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert index["days"] == [] and index["first_day"] is None

    res, out = _export(lab_data, tmp_path, days=None, today=date(2026, 10, 20))
    assert res["written"] == [PREV, DAY]  # --all
    assert not (out / "2026-10-10.json").exists()  # a non-lab PR doesn't make a day


def test_cli_export_lab_journal(lab_data, tmp_path, capsys):
    prs = tmp_path / "prs.json"
    prs.write_text(json.dumps(_prs()), encoding="utf-8")
    out = tmp_path / "cli-out"
    rc = main(["export-lab-journal", "--out", str(out), "--lab-data", str(lab_data),
               "--lab-work", str(_lab_work(tmp_path)), "--prs-json", str(prs), "--all"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["written"] == [PREV, DAY]
    assert (out / f"{DAY}.json").is_file()


def test_slim_deploy_ships_newest_lab_days_within_budget(tmp_path):
    lab = tmp_path / "docs" / "data" / "lab"
    lab.mkdir(parents=True)
    days = ["2026-10-12", "2026-10-13", "2026-10-14"]
    for d in days:
        (lab / f"{d}.json").write_text(json.dumps({"date": d, "pad": "x" * 900}), encoding="utf-8")
    (lab / "index.json").write_text(json.dumps({"schema": 1, "days": [{"date": d} for d in days]}), encoding="utf-8")
    out = tmp_path / "deploy"
    (out / "data" / "lab").mkdir(parents=True)
    (out / "data" / "lab" / "2026-01-01.json").write_text("{}", encoding="utf-8")  # stale from an earlier deploy

    res = copy_lab_journal(tmp_path / "docs", out, budget=2_000)
    assert res == {"days": 2, "bytes": res["bytes"], "trimmed": 1}
    shipped = sorted(p.name for p in (out / "data" / "lab").iterdir())
    assert shipped == ["2026-10-13.json", "2026-10-14.json", "index.json"]
    index = json.loads((out / "data" / "lab" / "index.json").read_text(encoding="utf-8"))
    assert [r["date"] for r in index["days"]] == ["2026-10-13", "2026-10-14"]
    assert index["first_day"] == "2026-10-13" and index["trimmed"] == 1

    assert copy_lab_journal(tmp_path / "nothing", out) == {"days": 0, "bytes": 0}
    assert not (out / "data" / "lab").exists()


def test_index_generated_at_alone_does_not_rewrite(lab_data, tmp_path):
    _, out = _export(lab_data, tmp_path)
    before = (out / "index.json").read_text(encoding="utf-8")
    export_lab_journal(out, data_dir=lab_data, lab_work=_lab_work(tmp_path), prs=_prs(), today=TODAY,
                       now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert (out / "index.json").read_text(encoding="utf-8") == before


def test_a_day_that_still_mentions_a_locked_window_is_refused(lab_data, tmp_path):
    prs = _prs()
    prs[0]["title"] = "feat(lab): cup_handle, safe before holdout"  # titles are hidden, so this one passes
    prs[0]["files"].append({"path": "lab_nominations/validation-notes.json"})  # a file path is not redacted
    res, out = _export(lab_data, tmp_path, prs=prs)
    assert res["refused"] == [DAY] and res["written"] == [PREV]
    assert not (out / f"{DAY}.json").exists()

    prs = _prs()
    prs[0]["title"] = "feat(lab): cup_handle, safe before holdout"
    res, out = _export(lab_data, tmp_path, prs=prs)
    assert res["written"] == [DAY]
    assert _day(out)["indicators"][0]["title"].startswith("(title hidden")


def test_notes_parse_spec_rows_paragraphs_and_sessions():
    from quant_system.lab_journal import parse_notes

    items = parse_notes(
        "## 2026-10-13 (Tue) morning session: venv rebuilt\n"
        "- cup-stage-handle | 0123456789abcdef | 0.1 (CAGR 1%, DD 9%) | fail | Many handles never pay.\n"
        "1. trend-hold-regime | 89abcdef | 1.09 | fail | First to pass nudges;\n"
        "   failed sectors.\n"
        "Lessons: anticipating\nthe breakout did not help.\n"
        "Note on 2018-10-01 selloff stays on Oct 13.\n"
    )
    assert [(i["ref"], i["text"]) for i in items] == [
        ("cup-stage-handle", "Many handles never pay."),
        ("trend-hold-regime", "First to pass nudges; failed sectors."),
        (None, "Lessons: anticipating the breakout did not help. Note on 2018-10-01 selloff stays on Oct 13."),
    ]
    assert {i["day"] for i in items} == {"2026-10-13"}
    assert {i["session"] for i in items} == {"morning session: venv rebuilt"}
    assert items[1]["ref_hash"] == "89abcdef" and items[2]["kind"] == "para"
