"""Lab rule language (safe parsing + evaluation) and strategy spec schema."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from quant_system.config.loader import load_config
from quant_system.lab.rules import Evaluator, RuleError, parse_rule
from quant_system.lab.spec import POSITION_CAP, SpecError, StrategySpec, load_spec

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "lab_specs" / "examples").glob("*.yaml"))
BASE = {
    "id": "t",
    "indicators": {"rsi": {"fn": "rsi", "length": "$n"}},
    "params": {"n": {"value": 14, "min": 7, "max": 28}},
    "entry": "rsi < 30",
    "exit": "rsi > 55 or bars_held >= 20",
}


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "close.__class__",
        "close[0] > 1",
        "__import__('os')",
        "(lambda: 1)()",
        "'a' == 'a'",
        "close if open else high",
        "[close]",
        "eval('1')",
        "abs(x=close)",
        "prev(close, 0)",
        "prev(close, 2.5)",
        "prev(close, 999)",
        "prev(bars_held) > 1",
        "close ** 2",
        "close // 2",
        "x := 1",
        "None",
        "",
        "close > " + "1 + " * 200 + "1",
    ],
)
def test_rules_reject_anything_outside_the_language(src):
    with pytest.raises(RuleError):
        parse_rule(src)


def test_rule_names_exclude_functions():
    rule = parse_rule("crosses_above(fast, slow) and abs(prev(close, 3)) > min(a, 2) and not -b")
    assert rule.names == {"fast", "slow", "close", "a", "b"}
    assert not rule.uses_state and parse_rule("bars_held >= 5").uses_state


# --- evaluation ------------------------------------------------------------

NAN = np.nan
ENV = {
    "x": np.array([[1.0, NAN], [3.0, 2.0], [2.0, 5.0], [4.0, 1.0]]),
    "y": np.array([[2.0, 1.0], [2.0, 1.0], [2.0, 1.0], [2.0, 1.0]]),
    "level": 2.5,
}


def _full(src):
    return Evaluator(parse_rule(src), ENV).full()


def test_evaluation_semantics():
    assert _full("x > y").tolist() == [[False, False], [True, True], [False, True], [True, False]]
    assert _full("not x > y")[0].tolist() == [True, True]  # NaN compares False, so not -> True
    assert _full("x > level and y < level")[:, 0].tolist() == [False, True, False, True]
    assert _full("1 < x < 4")[:, 0].tolist() == [False, True, True, False]
    assert _full("x / (y - 2) > 0")[:, 0].tolist() == [False] * 4  # divide by zero -> NaN -> False
    assert _full("prev(x) > x")[:, 0].tolist() == [False, False, True, False]
    assert _full("prev(x, 2) < x")[:, 0].tolist() == [False, False, True, True]
    assert _full("crosses_above(x, y)")[:, 0].tolist() == [False, True, False, True]
    assert _full("crosses_below(x, y)")[:, 1].tolist() == [False, False, False, False]
    assert _full("min(x, y) >= 1")[:, 1].tolist() == [False, True, True, True]  # NaN stays NaN
    assert _full("True").shape == ()  # constants broadcast in the engine
    vals = Evaluator(parse_rule("-x"), ENV).values()
    assert vals[1].tolist() == [-3.0, -2.0]


def test_row_evaluation_with_bars_held_matches_full():
    rule = parse_rule("x > level or bars_held >= 2")
    ev = Evaluator(rule, ENV)
    with pytest.raises(RuleError):
        ev.full()
    held = np.array([1.0, 2.0])
    for i in range(4):
        expected = (np.nan_to_num(ENV["x"][i]) > 2.5) | (held >= 2)
        assert ev.at(i, {"bars_held": held}).tolist() == expected.tolist()


# --- spec schema -----------------------------------------------------------


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_specs_validate(path):
    spec = load_spec(path)
    assert len(spec.hash) == 16 and spec.sizing.max_weight <= POSITION_CAP


def _bad(match, **changes):
    with pytest.raises(ValidationError, match=match):
        StrategySpec.model_validate({**BASE, **changes})


def test_spec_rejections():
    five = {f"p{i}": {"value": 1, "min": 0, "max": 2} for i in range(5)}
    _bad("at most 4 tunable", params=five, entry=" and ".join(f"p{i} > 0" for i in range(5)))
    _bad("never used", params={**BASE["params"], "spare": {"value": 1, "min": 0, "max": 2}})
    _bad("unknown name", entry="rsi < magic")
    _bad("only works in exit", entry="bars_held > 1")
    _bad("only works in exit", rank="bars_held")
    _bad("less than or equal to 0.2", sizing={"max_weight": 0.25})
    _bad("reserved", indicators={"close": {"fn": "sma"}}, entry="close > 1", params={})
    _bad("not a \\$setting", indicators={"rsi": {"fn": "rsi", "length": "$m"}})
    _bad("outside", indicators={"rsi": {"fn": "rsi", "length": 500}}, params={}, entry="rsi < 30")
    _bad("unknown indicator", indicators={"rsi": {"fn": "magic"}}, params={}, entry="rsi < 30")
    _bad("outside", params={"n": {"value": 40, "min": 7, "max": 28}})
    _bad("Extra inputs", code="import os")
    _bad("rule", exit="rsi.real > 1")
    _bad("id", id="Has Spaces")


def test_spec_hash_and_param_nudges():
    spec = StrategySpec.model_validate(BASE)
    renamed = StrategySpec.model_validate({**BASE, "id": "other", "author_bot": "b", "description": "d"})
    assert spec.hash == renamed.hash
    nudged = spec.with_params({"n": 16.8})
    assert nudged.hash != spec.hash and nudged.indicator_args("rsi") == {"length": 16.8}
    with pytest.raises(ValidationError):
        spec.with_params({"n": 40})  # outside the spec's own range
    with pytest.raises(ValueError, match="unknown settings"):
        spec.with_params({"m": 1})


def test_load_spec_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(SpecError, match="mapping"):
        load_spec(bad)
    with pytest.raises(SpecError):
        load_spec(tmp_path / "missing.yaml")


def test_position_cap_matches_live_risk_policy():
    assert POSITION_CAP == load_config().risk.max_position_pct
