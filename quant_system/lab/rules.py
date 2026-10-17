"""Rule expressions for strategy specs: a small, safe language parsed with ``ast``.

Allowed: numbers, ``True``/``False``, names (indicators, settings, ``open``,
``high``, ``low``, ``close``, ``volume``, ``bars_held`` in exit rules, and in
intraday specs ``bar_of_day`` / ``bars_left_today``),
``+ - * /``, comparisons (chains too), ``and`` / ``or`` / ``not``, and the
functions ``abs(x)``, ``min(a, b)``, ``max(a, b)``, ``prev(x)`` / ``prev(x, n)``
(value n bars ago, n a whole number 1-252), ``crosses_above(a, b)`` and
``crosses_below(a, b)``. Nothing else parses, and nothing is ever ``eval``-ed.

Evaluation is on numpy arrays shaped bars x tickers (sessions, for daily specs). Missing values never
trigger anything: a comparison with NaN is False.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

MAX_RULE_CHARS = 500
MAX_PREV = 252
PRICE_FIELDS = ("open", "high", "low", "close", "volume")
STATE_NAMES = ("bars_held",)
# Intraday specs only: the bar's place in its session (0 = the first bar) and the
# scheduled bars after it (0 = the session's last bar; half days have fewer).
SESSION_NAMES = ("bar_of_day", "bars_left_today")
FUNCTIONS = {
    "abs": (1, 1),
    "min": (2, 2),
    "max": (2, 2),
    "prev": (1, 2),
    "crosses_above": (2, 2),
    "crosses_below": (2, 2),
}
RESERVED = frozenset(PRICE_FIELDS) | frozenset(STATE_NAMES) | frozenset(SESSION_NAMES) | frozenset(FUNCTIONS)

_BOOL_OPS = {ast.And: np.logical_and, ast.Or: np.logical_or}
_BIN_OPS = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide}
_CMP_OPS = {
    ast.Lt: np.less,
    ast.LtE: np.less_equal,
    ast.Gt: np.greater,
    ast.GtE: np.greater_equal,
    ast.Eq: np.equal,
    ast.NotEq: np.not_equal,
}
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare, ast.Call, ast.Name,
    ast.Constant, ast.Load, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    *_BIN_OPS, *_CMP_OPS,
)  # fmt: skip


class RuleError(ValueError):
    """A rule outside the allowed language."""


@dataclass(frozen=True)
class Rule:
    source: str
    tree: ast.Expression = field(repr=False, compare=False)
    names: frozenset[str]

    @property
    def uses_state(self) -> bool:
        return bool(self.names & set(STATE_NAMES))


def _uses_state(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id in STATE_NAMES for n in ast.walk(node))


def parse_rule(source: str) -> Rule:
    text = (source or "").strip()
    if not text:
        raise RuleError("empty rule")
    if len(text) > MAX_RULE_CHARS:
        raise RuleError(f"rule longer than {MAX_RULE_CHARS} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise RuleError(f"cannot parse {text!r}: {exc.msg}") from None
    # Node types first, so e.g. a lambda is reported as a lambda, not as a call.
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleError(f"{type(node).__name__} is not allowed in rules: {text!r}")
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and (
            not isinstance(node.value, (int, float)) or node.value is None
        ):
            raise RuleError(f"only numbers and True/False are allowed, got {node.value!r}")
        if isinstance(node, ast.Call):
            fname = node.func.id if isinstance(node.func, ast.Name) else None
            if fname not in FUNCTIONS:
                raise RuleError(f"unknown function in {text!r}; allowed: {', '.join(FUNCTIONS)}")
            lo, hi = FUNCTIONS[fname]
            if node.keywords or not lo <= len(node.args) <= hi:
                raise RuleError(f"{fname}() takes {lo}-{hi} positional arguments")
            if fname == "prev":
                if len(node.args) == 2:
                    n = node.args[1]
                    if not (isinstance(n, ast.Constant) and type(n.value) is int and 1 <= n.value <= MAX_PREV):
                        raise RuleError(f"prev(x, n) needs a whole number n in 1-{MAX_PREV}")
            if fname in ("prev", "crosses_above", "crosses_below") and any(_uses_state(a) for a in node.args):
                raise RuleError(f"{fname}() cannot look at bars_held")
        if isinstance(node, ast.Name):
            names.add(node.id)
    # Function names appear as Name nodes too; drop them.
    names -= {
        n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    return Rule(text, tree, frozenset(names))


def _truthy(x: Any) -> Any:
    arr = np.asarray(x)
    if arr.dtype == bool:
        return arr
    with np.errstate(invalid="ignore"):
        return np.nan_to_num(arr.astype(float), nan=0.0) != 0


def _shift(x: Any, n: int) -> Any:
    arr = np.asarray(x, dtype=float)
    if arr.ndim == 0:
        return arr
    out = np.full(arr.shape, np.nan)
    if n < len(arr):
        out[n:] = arr[: len(arr) - n]
    return out


class Evaluator:
    """Evaluate one rule over a full history, or one session at a time.

    ``env`` maps names to 2-D arrays (sessions x tickers) or scalars. Rules
    without ``bars_held`` evaluate in one shot with ``full()``. Exit rules that
    use ``bars_held`` evaluate per session with ``at(i, state)``; every piece
    that doesn't touch ``bars_held`` is still computed once over the full
    history and cached.
    """

    def __init__(self, rule: Rule, env: Mapping[str, Any]):
        self.rule = rule
        self.env = env
        self._cache: dict[int, Any] = {}

    def full(self) -> np.ndarray:
        if self.rule.uses_state:
            raise RuleError("rule uses bars_held; evaluate it per session with at()")
        return _truthy(self._full(self.rule.tree.body))

    def values(self) -> np.ndarray:
        """Numeric value of the whole rule over the full history (for rank)."""
        if self.rule.uses_state:
            raise RuleError("rule uses bars_held; evaluate it per session with at()")
        return np.asarray(self._full(self.rule.tree.body), dtype=float)

    def at(self, i: int, state: Mapping[str, Any]) -> np.ndarray:
        return _truthy(self._row(self.rule.tree.body, i, state))

    # -- whole history ---------------------------------------------------
    def _full(self, node: ast.AST) -> Any:
        key = id(node)
        if key not in self._cache:
            self._cache[key] = self._eval(node, self._full, self._lookup_full, self._prev_full)
        return self._cache[key]

    def _lookup_full(self, name: str) -> Any:
        if name not in self.env:
            raise RuleError(f"unknown name {name!r}")
        return self.env[name]

    def _prev_full(self, node: ast.AST, n: int) -> Any:
        return _shift(self._full(node), n)

    # -- one session -----------------------------------------------------
    def _row(self, node: ast.AST, i: int, state: Mapping[str, Any]) -> Any:
        if not _uses_state(node):
            val = self._full(node)
            return val[i] if np.ndim(val) == 2 else val

        def lookup(name: str) -> Any:
            return state[name] if name in STATE_NAMES else self._lookup_full(name)

        return self._eval(node, lambda c: self._row(c, i, state), lookup, None)

    # -- shared ----------------------------------------------------------
    def _eval(self, node: ast.AST, sub, lookup, prev) -> Any:
        if isinstance(node, ast.Constant):
            return float(node.value) if not isinstance(node.value, bool) else node.value
        if isinstance(node, ast.Name):
            return lookup(node.id)
        if isinstance(node, ast.BoolOp):
            op = _BOOL_OPS[type(node.op)]
            vals = [_truthy(sub(v)) for v in node.values]
            out = vals[0]
            for v in vals[1:]:
                out = op(out, v)
            return out
        if isinstance(node, ast.UnaryOp):
            val = sub(node.operand)
            if isinstance(node.op, ast.Not):
                return np.logical_not(_truthy(val))
            arr = np.asarray(val, dtype=float)
            return -arr if isinstance(node.op, ast.USub) else arr
        if isinstance(node, ast.BinOp):
            with np.errstate(divide="ignore", invalid="ignore"):
                out = _BIN_OPS[type(node.op)](
                    np.asarray(sub(node.left), dtype=float), np.asarray(sub(node.right), dtype=float)
                )
            return np.where(np.isfinite(out), out, np.nan)
        if isinstance(node, ast.Compare):
            left = np.asarray(sub(node.left), dtype=float)
            out = None
            for op, comp in zip(node.ops, node.comparators):
                right = np.asarray(sub(comp), dtype=float)
                with np.errstate(invalid="ignore"):
                    res = _CMP_OPS[type(op)](left, right) & ~np.isnan(left) & ~np.isnan(right)
                out = res if out is None else np.logical_and(out, res)
                left = right
            return out
        if isinstance(node, ast.Call):
            fname = node.func.id
            args = node.args
            if fname == "prev":
                n = int(args[1].value) if len(args) == 2 else 1
                return prev(args[0], n)
            if fname in ("crosses_above", "crosses_below"):
                a, b = (np.asarray(sub(x), dtype=float) for x in args)
                pa, pb = prev(args[0], 1), prev(args[1], 1)
                with np.errstate(invalid="ignore"):
                    now = a > b if fname == "crosses_above" else a < b
                    before = pa <= pb if fname == "crosses_above" else pa >= pb
                return now & before
            vals = [np.asarray(sub(x), dtype=float) for x in args]
            if fname == "abs":
                return np.abs(vals[0])
            return (np.minimum if fname == "min" else np.maximum)(vals[0], vals[1])  # NaN stays NaN
        raise RuleError(f"unsupported node {type(node).__name__}")  # pragma: no cover
