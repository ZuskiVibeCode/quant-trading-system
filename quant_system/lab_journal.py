"""Lab journal export: what the strategy-lab bots did each day, for the Orbit dashboard.

Writes ``docs/data/lab/<YYYY-MM-DD>.json`` (one small file per ET day with
activity) and ``docs/data/lab/index.json``. Sources, all on the shared machine:

- the trial log and robust reports under ``QUANT_LAB_DATA_DIR``,
- each inventor's ``/workspace/lab-work/<bot>/`` (``notes.md`` and saved
  ``lab robust`` output),
- GitHub PRs: smith's merged indicators, skeptic's verdict comments and the
  nomination PRs.

Validation and holdout results never enter the journal. Only discovery-window
records are read, and any free-text line that mentions validation or holdout
is dropped. Those runs happen only on Sabastian's machine, and their scores
stay there.

Each idea carries ``category: null``, to be filled in once there is about a
week of history to sort ideas into families (breakout, pullback, pattern, ...).
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
JOURNAL_TZ = ZoneInfo("America/New_York")
INVENTORS = ("momo", "snapback")
DISCOVERY = "discovery"
LAB_WORK_ENV = "QUANT_LAB_WORK_DIR"
DEFAULT_LAB_WORK = Path("/workspace/lab-work")
DEFAULT_DAYS = 7
CHECK_ORDER = ("nudges", "sectors", "cap_buckets", "selloffs", "single_stock")
BENCH_LABEL = {"SPY": "SPY", "equal_weight": "equal-weight"}
INDICATOR_DIR = "quant_system/lab/indicators/"
NOMINATION_DIR = "lab_nominations/"
MAX_NOTES_PER_IDEA = 6
MAX_NOTE_CHARS = 300
MAX_SESSION_NOTES = 12
MAX_SESSION_NOTE_CHARS = 500
MAX_PR_TEXT = 600
MAX_GROUP_NAMES = 8
# Free text mentioning these never reaches the journal (bots never see those scores).
_LOCKED_TEXT = re.compile(r"validat|holdout", re.IGNORECASE)
_DAY_IN_TEXT = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
_DAY_AT_START = re.compile(r"^\s*(20\d\d-\d\d-\d\d)\b")
_WEEKDAY_TAG = re.compile(r"^\s*\((mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\)", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*•]|\d+[.)])\s+")
_SPEC_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HEX = re.compile(r"^[0-9a-f]{8,16}$")
_AUTHOR_BOT = re.compile(r"^\s*author bot:\s*([a-z0-9_-]+)", re.IGNORECASE | re.MULTILINE)
_PR_JSON_FIELDS = "number,title,body,headRefName,state,createdAt,mergedAt,closedAt,url,comments,files"


# ---------------------------------------------------------------- small helpers


def et_day(iso: str | None) -> str | None:
    """ISO timestamp -> America/New_York calendar day (YYYY-MM-DD)."""
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(JOURNAL_TZ).date().isoformat()


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _round(v: Any, digits: int) -> float | int | None:
    """Rounded number (whole numbers stay ints), None for anything else."""
    x = _num(v)
    if x is None:
        return None
    x = round(x, digits)
    return int(x) if x == int(x) and abs(x) < 1e12 else x


def _fmt_num(v: Any) -> str:
    x = _num(v)
    if x is None:
        return str(v)
    return str(int(x)) if x == int(x) and abs(x) < 1e12 else f"{x:.4g}"


def _pct(v: Any, digits: int = 0) -> str:
    x = _num(v)
    return "?" if x is None else f"{x * 100:.{digits}f}%"


def _trunc(text: str, n: int) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def clean_text(text: str | None, limit: int = MAX_PR_TEXT) -> str | None:
    """Drop lines that mention validation or holdout; collapse whitespace; truncate."""
    if not text:
        return None
    kept = [ln for ln in str(text).splitlines() if ln.strip() and not _LOCKED_TEXT.search(ln)]
    return _trunc(" ".join(ln.strip() for ln in kept), limit) if kept else None


def safe_title(title: str | None) -> str | None:
    """A PR title, hidden if it mentions validation or holdout."""
    if title and _LOCKED_TEXT.search(title):
        return "(title hidden: mentions a locked window)"
    return title


def _window_name(rec: Mapping[str, Any]) -> str | None:
    w = rec.get("window")
    if isinstance(w, Mapping):
        w = w.get("name")
    return str(w).lower() if w else None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- plain English


_CMP_WORDS = {
    ast.Lt: "is below",
    ast.LtE: "is at or below",
    ast.Gt: "is above",
    ast.GtE: "is at or above",
    ast.Eq: "equals",
    ast.NotEq: "is not",
}
_FLIP = {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE, ast.Eq: ast.Eq, ast.NotEq: ast.NotEq}
_BIN_SYMBOL = {ast.Add: "+", ast.Sub: "−", ast.Mult: "×", ast.Div: "÷"}


def indicator_label(fn: str, given: Mapping[str, Any]) -> str:
    """``sma(200)``: the indicator with the settings the spec set (just the name past 3)."""
    values = [_fmt_num(v) for v in given.values()]
    return f"{fn}({', '.join(values)})" if 0 < len(values) <= 3 else fn


class _Phrase:
    """Render one rule expression in words, settings replaced by their values."""

    def __init__(self, names: Mapping[str, str]):
        self.names = names

    def __call__(self, node: ast.AST) -> str:
        return self.node(node)

    def operand(self, node: ast.AST) -> str:
        text = self.node(node)
        return f"({text})" if isinstance(node, (ast.BinOp, ast.BoolOp, ast.Compare)) else text

    def node(self, node: ast.AST) -> str:
        if isinstance(node, ast.Expression):
            return self.node(node.body)
        if isinstance(node, ast.Constant):
            if node.value is True:
                return "always"
            if node.value is False:
                return "never"
            return _fmt_num(node.value)
        if isinstance(node, ast.Name):
            return self.names.get(node.id, node.id)
        if isinstance(node, ast.BoolOp):
            word = " and " if isinstance(node.op, ast.And) else " or "
            parts = []
            for v in node.values:
                text = self.node(v)
                if isinstance(v, ast.BoolOp) and type(v.op) is not type(node.op):
                    text = f"({text})"
                parts.append(text)
            return word.join(parts)
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return f"not ({self.node(node.operand)})"
            sign = "-" if isinstance(node.op, ast.USub) else "+"
            return f"{sign}{self.operand(node.operand)}"
        if isinstance(node, ast.BinOp):
            return f"{self.operand(node.left)} {_BIN_SYMBOL.get(type(node.op), '?')} {self.operand(node.right)}"
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return self.call(node.func.id, node.args)
        raise ValueError(f"cannot describe {type(node).__name__}")

    def compare(self, node: ast.Compare) -> str:
        items = [node.left, *node.comparators]
        ops = node.ops
        lows = (ast.Lt, ast.LtE)
        if len(ops) == 2 and all(isinstance(o, lows) for o in ops):
            return f"{self.node(items[1])} is between {self.node(items[0])} and {self.node(items[2])}"
        parts = []
        for left, op, right in zip(items, ops, items[1:]):
            if isinstance(left, ast.Constant) and not isinstance(right, ast.Constant):
                left, op, right = right, _FLIP[type(op)](), left
            if isinstance(left, ast.Name) and left.id == "bars_held" and isinstance(op, (ast.Gt, ast.GtE)):
                n = self.node(right)
                parts.append(f"held {n} days or more" if isinstance(op, ast.GtE) else f"held more than {n} days")
                continue
            parts.append(f"{self.node(left)} {_CMP_WORDS[type(op)]} {self.node(right)}")
        return " and ".join(parts)

    def call(self, fname: str, args: list[ast.AST]) -> str:
        a = [self.node(x) for x in args]
        if fname == "prev":
            if len(args) == 1:
                return f"yesterday's {a[0]}"
            return f"{a[0]} from {a[1]} days earlier"
        if fname == "crosses_above":
            return f"{a[0]} crosses above {a[1]}"
        if fname == "crosses_below":
            return f"{a[0]} crosses below {a[1]}"
        if fname == "abs":
            return f"the size of {self.operand(args[0])}"
        if fname == "min":
            return f"the lower of {a[0]} and {a[1]}"
        if fname == "max":
            return f"the higher of {a[0]} and {a[1]}"
        return f"{fname}({', '.join(a)})"


def describe_rule(source: str | None, names: Mapping[str, str]) -> str | None:
    """A spec rule in words, or the rule itself if it won't parse."""
    if not source:
        return None
    try:
        return _Phrase(names)(ast.parse(source.strip(), mode="eval"))
    except (SyntaxError, ValueError, KeyError):
        return source.strip()


def describe_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """What a spec does, in plain English, plus its indicators and tunable settings."""
    params = spec.get("params") or {}
    names: dict[str, str] = {k: _fmt_num((p or {}).get("value")) for k, p in params.items()}
    names["bars_held"] = "days held"
    indicators = []
    try:
        from quant_system.lab.indicators import REGISTRY
    except Exception:  # pragma: no cover - registry import is part of the repo
        REGISTRY = {}
    for alias, raw in (spec.get("indicators") or {}).items():
        raw = dict(raw or {})
        fn = str(raw.pop("fn", alias))
        timeframe = raw.pop("timeframe", None)  # a slower bar size (multi-timeframe specs)
        given = {k: (params[v[1:]].get("value") if isinstance(v, str) and v[1:] in params else v)
                 for k, v in raw.items()}
        label = indicator_label(fn, given) + (f" on {timeframe} bars" if timeframe else "")
        names[alias] = label
        reg = REGISTRY.get(fn)
        indicators.append({
            "alias": alias,
            "fn": fn,
            "timeframe": timeframe,
            "label": label,
            "settings": {k: _round(v, 6) if _num(v) is not None else v for k, v in given.items()},
            "about": reg.doc if reg else None,
        })
    rank = spec.get("rank")
    if rank:
        try:
            tree = ast.parse(str(rank).strip(), mode="eval").body
        except SyntaxError:
            tree = None
        if isinstance(tree, ast.UnaryOp) and isinstance(tree.op, ast.USub):
            order = f"buys the lowest {_Phrase(names)(tree.operand)} first"
        else:
            order = f"buys the highest {describe_rule(str(rank), names)} first"
    else:
        order = "buys the most heavily traded names first"
    sizing = spec.get("sizing") or {}
    max_pos = int(sizing.get("max_positions") or 10)
    weight = min(1.0 / max_pos, float(sizing.get("max_weight") or 0.20))
    universe = str(spec.get("universe") or "broad").lower()
    return {
        "buy": describe_rule(spec.get("entry"), names),
        "sell": describe_rule(spec.get("exit"), names),
        "order": order,
        "size": f"up to {max_pos} stocks at {_pct(weight, 1).replace('.0%', '%')} of the account each",
        "universe": universe,
        "indicators": indicators,
        "settings": [
            {"name": k, **{f: _round(p.get(f), 6) for f in ("value", "min", "max")}}
            for k, p in params.items()
        ],
    }


# ---------------------------------------------------------------- results


def _bench_rows(metrics: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for name, v in (metrics.get("vs_benchmarks") or {}).items():
        out[name] = {k: _round(v.get(k), 4) for k in ("sharpe", "cagr", "max_drawdown", "sharpe_diff", "excess_cagr")}
    return out


def run_result(rec: Mapping[str, Any]) -> dict[str, Any]:
    """Lab run on discovery: the numbers, and which benchmark tests it missed."""
    m = rec.get("metrics") or {}
    bench = _bench_rows(m)
    missed = []
    for name, v in bench.items():
        label = BENCH_LABEL.get(name, name)
        if (v["sharpe_diff"] or 0) <= 0:
            missed.append(f"{label} Sharpe")
        if (v["excess_cagr"] or 0) <= 0:
            missed.append(f"{label} CAGR")
    out = {
        "at": rec.get("recorded_at"),
        "start": m.get("start"),
        "end": m.get("end"),
        "sharpe": _round(m.get("sharpe"), 4),
        "cagr": _round(m.get("cagr"), 4),
        "max_drawdown": _round(m.get("max_drawdown"), 4),
        "trade_count": m.get("trade_count"),
        "turnover": _round(m.get("turnover"), 2),
        "benchmarks": {k: {kk: v[kk] for kk in ("sharpe", "cagr", "max_drawdown")} for k, v in bench.items()},
        "beats_benchmarks": bool(m.get("beats_benchmarks")),
        "missed": missed,
        "universe_names": (rec.get("universe") or {}).get("names"),
    }
    if rec.get("git_dirty"):
        out["dirty_checkout"] = True
    return out


def _check_detail(name: str, c: Mapping[str, Any], th: Mapping[str, Any]) -> str:
    if c.get("status") == "skipped":
        return f"Skipped: {c.get('reason') or 'nothing to test'}."
    if name == "nudges":
        variants = c.get("variants") or []
        passing = sum(bool(v.get("passes")) for v in variants)
        return (
            f"{passing} of {len(variants)} nudged versions still beat both benchmarks "
            f"(needs {_pct(th.get('nudge_pass_share', 0.75))}). Their median Sharpe "
            f"{_fmt2(c.get('median_sharpe'))} vs {_fmt2(c.get('base_sharpe'))} for the spec "
            f"(needs at least {_pct(th.get('peak_ratio', 0.5))} of it)."
        )
    if name in ("sectors", "cap_buckets"):
        key = "sector" if name == "sectors" else "cap_bucket"
        noun = "sectors" if name == "sectors" else "cap buckets"
        need = th.get("sector_pass_share" if name == "sectors" else "cap_pass_share", 0.6)
        groups = c.get("groups") or []
        matched = any("passes" in g for g in groups)  # reports from Oct 15, 2026 on
        won = (lambda g: g.get("passes")) if matched else (lambda g: g.get("beats_equal_weight_sharpe"))
        wins = [g for g in groups if won(g)]
        losers = [str(g.get(key)) for g in groups if not won(g)]
        if matched:
            text = (f"Beat that group's equal-weight, held at the strategy's own exposure, in {len(wins)} "
                    f"of {len(groups)} {noun} (needs {_pct(need)}; a group with fewer than "
                    f"{th.get('min_subset_entries', 30)} entries counts as a miss).")
        else:
            text = (f"Beat that group's equal-weight Sharpe in {len(wins)} of {len(groups)} {noun} "
                    f"(needs {_pct(need)}).")
        if losers:
            more = f" and {len(losers) - MAX_GROUP_NAMES} more" if len(losers) > MAX_GROUP_NAMES else ""
            text += f" Missed: {', '.join(losers[:MAX_GROUP_NAMES])}{more}."
        thin = c.get("thin") or []
        if thin:
            text += f" Too few entries: {', '.join(str(x) for x in thin[:MAX_GROUP_NAMES])}."
        return text
    if name == "selloffs":
        ratio = th.get("selloff_dd_ratio", 1.25)
        parts = []
        for p in c.get("periods") or []:
            worst = max((_num(v) or 0 for v in (p.get("benchmark_drawdowns") or {}).values()), default=0)
            mark = "ok" if p.get("ok") else "too deep"
            parts.append(f"{p.get('name')}: drawdown {_pct(p.get('max_drawdown'), 1)} vs "
                         f"{_pct(worst, 1)} for the worse benchmark ({mark})")
        return "; ".join(parts) + f". Limit: {_fmt_num(ratio)}× the worse benchmark's drawdown."
    if name == "single_stock":
        share = c.get("top_share")
        share_text = f" ({_pct(share)} of the profit)" if _num(share) is not None else ""
        verdict = "still beats both benchmarks" if c.get("passes") else "no longer beats both benchmarks"
        return f"Without {c.get('removed')}{share_text}: Sharpe {_fmt2(c.get('sharpe'))}, {verdict}."
    return str(c.get("status"))


def _fmt2(v: Any) -> str:
    x = _num(v)
    return "?" if x is None else f"{x:.2f}"


def robust_result(report: Mapping[str, Any]) -> dict[str, Any]:
    """Lab robust: pass/fail per check in words, and which ones failed."""
    checks = report.get("checks") or {}
    th = report.get("thresholds") or {}
    rows = []
    for name in [*CHECK_ORDER, *[k for k in checks if k not in CHECK_ORDER]]:
        c = checks.get(name)
        if not isinstance(c, Mapping):
            continue
        rows.append({
            "name": name,
            "status": c.get("status"),
            "detail": _check_detail(name, c, th),
        })
    return {
        "at": report.get("recorded_at"),
        "day": et_day(report.get("recorded_at")),
        "passed": bool(report.get("passed")),
        "complete": bool(report.get("complete")),
        "base_passed": bool((report.get("base") or {}).get("passed")),
        "failed": [r["name"] for r in rows if r["status"] == "fail"],
        "skipped": [r["name"] for r in rows if r["status"] == "skipped"],
        "checks": rows,
    }


# ---------------------------------------------------------------- sources


def load_lab_records(data_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(trial records, robust reports) from the lab data dir, discovery only.

    Return series are dropped as they're read; the journal never needs them.
    """
    trials = []
    for path in sorted((data_dir / "trials").glob("*.json")):
        rec = _read_json(path)
        if not rec or _window_name(rec) != DISCOVERY:
            continue
        rec.pop("returns", None)
        trials.append(rec)
    trials.sort(key=lambda r: r.get("recorded_at") or "")
    return trials, _robust_reports(sorted((data_dir / "robust").glob("*.json")))


def _robust_reports(paths: Iterable[Path]) -> list[dict[str, Any]]:
    out = []
    for path in paths:
        rec = _read_json(path)
        if not rec or _window_name(rec) != DISCOVERY:
            continue
        if not (rec.get("spec_hash") and isinstance(rec.get("checks"), dict) and rec.get("recorded_at")):
            continue
        out.append(rec)
    return out


def _session_label(heading: str) -> str | None:
    """"## 2026-10-13 (Tue) afternoon session: 10 specs" -> "afternoon session: 10 specs"."""
    text = _DAY_IN_TEXT.sub("", heading.lstrip("#").strip(), count=1)
    text = _WEEKDAY_TAG.sub("", text).strip(" -—:·,")
    return _trunc(text, 120) if text and not _LOCKED_TEXT.search(text) else None


def _note_item(text: str, day: str | None, session: str | None, kind: str) -> dict[str, Any]:
    """One note. A spec row ("id | hash | sharpe | verdict | lesson") shows just its lesson."""
    item = {"text": text, "raw": text, "day": day, "session": session, "kind": kind, "ref": None, "ref_hash": None}
    if "|" in text:
        fields = [f.strip() for f in text.strip().strip("|").split("|")]
        if len(fields) >= 2 and _SPEC_ID.match(fields[0]):
            item["ref"] = fields[0]
            if _HEX.match(fields[1]):
                item["ref_hash"] = fields[1]
            item["text"] = fields[-1] or text
    return item


def parse_notes(text: str) -> list[dict[str, Any]]:
    """A bot's notes.md as items, each with the day and session of the heading above it.

    Bullets and table rows are one item each (indented lines continue them);
    consecutive plain lines join into one paragraph item. Items that mention
    validation or holdout are dropped whole.
    """
    items: list[dict[str, Any]] = []
    day = session = None
    cur: list[str] = []
    kind = "para"

    def flush() -> None:
        nonlocal cur
        if cur:
            joined = " ".join(cur)
            first = joined.strip("| ").split("|")[0].strip().lower()
            if not _LOCKED_TEXT.search(joined) and not (kind == "item" and "|" in joined and first == "id"):
                found = None if day else _DAY_AT_START.match(joined)
                items.append(_note_item(joined, day or (found.group(1) if found else None), session, kind))
        cur = []

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("#"):
            flush()
            found = _DAY_IN_TEXT.search(stripped)
            if found:
                day, session = found.group(1), _session_label(stripped)
            continue
        if set(stripped) <= set("|-: "):
            flush()
            continue
        if _BULLET.match(stripped) or stripped.startswith("|"):
            flush()
            cur, kind = [_BULLET.sub("", stripped)], "item"
            continue
        if cur and (raw[:1].isspace() or kind == "para"):
            cur.append(stripped)
            continue
        flush()
        cur, kind = [stripped], "para"
    flush()
    return items


def load_lab_work(lab_work: Path, bots: Iterable[str] = INVENTORS) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """(notes items per bot, robust reports saved there) from ``/workspace/lab-work/<bot>/``."""
    notes: dict[str, list[dict[str, Any]]] = {}
    reports: list[dict[str, Any]] = []
    for bot in bots:
        folder = lab_work / bot
        if not folder.is_dir():
            continue
        path = folder / "notes.md"
        notes[bot] = parse_notes(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else []
        reports.extend(_robust_reports(sorted(folder.rglob("*.json"))))
    return notes, reports


def fetch_pull_requests(gh: str = "gh", limit: int = 200, repo: str | None = None) -> list[dict[str, Any]] | None:
    """Recent PRs (all states) with comments and files, or None if gh fails."""
    cmd = [gh, "pr", "list", "--state", "all", "--limit", str(limit), "--json", _PR_JSON_FIELDS]
    if repo:
        cmd += ["--repo", repo]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True, encoding="utf-8")
        data = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _names_spec(item: Mapping[str, Any], spec_id: str | None, spec_hash: str) -> bool:
    if item["ref"] is not None:  # a spec row names its spec in its own fields
        rh = item["ref_hash"]
        return item["ref"] == spec_id or bool(rh and spec_hash.startswith(rh))
    if item["kind"] != "item":  # paragraphs are session notes, never one idea's
        return False
    id_hit = bool(spec_id) and re.search(rf"(?<![\w-]){re.escape(spec_id)}(?![\w-])", item["raw"])
    return spec_hash[:8] in item["raw"] or bool(id_hit)


def match_notes(items: list[dict[str, Any]], spec_id: str | None, spec_hash: str, day: str) -> tuple[list[str], set[int]]:
    """(note texts, item positions) naming this spec; that day's section first, else earlier ones."""
    hits = [i for i, it in enumerate(items) if _names_spec(it, spec_id, spec_hash)]
    same_day = [i for i in hits if items[i]["day"] == day]
    chosen = (same_day or [i for i in hits if items[i]["day"] is None or items[i]["day"] <= day])[-MAX_NOTES_PER_IDEA:]
    return [_trunc(items[i]["text"], MAX_NOTE_CHARS) for i in chosen], set(chosen)


def session_notes(items: list[dict[str, Any]], day: str, used: set[int]) -> list[dict[str, Any]]:
    """That day's notes not attached to an idea, grouped by session heading."""
    groups: list[dict[str, Any]] = []
    count = 0
    for i, it in enumerate(items):
        if it["day"] != day or i in used or count >= MAX_SESSION_NOTES:
            continue
        if not groups or groups[-1]["session"] != it["session"]:
            groups.append({"session": it["session"], "notes": []})
        text = f"{it['ref']}: {it['text']}" if it["ref"] else it["text"]
        groups[-1]["notes"].append(_trunc(text, MAX_SESSION_NOTE_CHARS))
        count += 1
    return groups


# ---------------------------------------------------------------- GitHub


def _pr_author_bot(pr: Mapping[str, Any]) -> str | None:
    m = _AUTHOR_BOT.search(pr.get("body") or "")
    if m:
        return m.group(1).lower()
    head = str(pr.get("headRefName") or "")
    return head.split("/", 1)[0].lower() if "/" in head else None


def _pr_files(pr: Mapping[str, Any]) -> list[str]:
    return [str(f.get("path")) for f in pr.get("files") or [] if isinstance(f, Mapping)]


def pr_kind(pr: Mapping[str, Any]) -> str:
    files = _pr_files(pr)
    if any(p.startswith(NOMINATION_DIR) and not p.endswith("README.md") for p in files):
        return "nomination"
    if any(p.startswith(INDICATOR_DIR) for p in files):
        return "indicator"
    return "other"


def skeptic_verdicts(pr: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Comments starting "Skeptic:", oldest first, verdict split from the evidence."""
    out = []
    for c in pr.get("comments") or []:
        body = str((c or {}).get("body") or "").strip()
        if not body.lower().startswith("skeptic:"):
            continue
        rest = body[len("skeptic:"):].strip()
        low = rest.lower()
        if low.startswith("no objection"):
            verdict, evidence = "no objection", rest[len("no objection"):]
        elif low.startswith("objection"):
            verdict, evidence = "objection", rest[len("objection"):]
        else:
            verdict, evidence = "comment", rest
        out.append({
            "verdict": verdict,
            "evidence": clean_text(evidence.lstrip(" .:-—\n"), MAX_PR_TEXT),
            "at": c.get("createdAt"),
            "url": c.get("url"),
        })
    out.sort(key=lambda v: v.get("at") or "")
    return out


def _indicator_names(pr: Mapping[str, Any], registry: Mapping[str, Any]) -> list[str]:
    def found(text: str) -> list[str]:
        return [n for n in registry if re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", text)]

    names = found(pr.get("title") or "") or found(pr.get("body") or "")
    if not names:
        head = str(pr.get("headRefName") or "")
        guess = head.rsplit("indicator-", 1)[-1].replace("-", "_") if "indicator-" in head else ""
        names = [guess] if guess else []
    return names


def _pr_summary(pr: Mapping[str, Any]) -> str | None:
    body = _AUTHOR_BOT.sub("", pr.get("body") or "")
    return clean_text(body, MAX_PR_TEXT)


def _latest_verdict(pr: Mapping[str, Any]) -> dict[str, Any] | None:
    v = skeptic_verdicts(pr)
    return {"verdict": v[-1]["verdict"], "at": v[-1]["at"]} if v else None


def github_by_day(prs: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Per ET day: merged indicator PRs, skeptic verdicts and opened nomination PRs."""
    try:
        from quant_system.lab.indicators import REGISTRY
    except Exception:  # pragma: no cover
        REGISTRY = {}
    days: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"indicators": [], "skeptic": [], "nominations": []}
    )
    for pr in prs:
        kind = pr_kind(pr)
        base = {"pr": pr.get("number"), "title": safe_title(pr.get("title")), "url": pr.get("url")}
        if kind == "indicator" and pr.get("mergedAt"):
            items = []
            for name in _indicator_names(pr, REGISTRY):
                reg = REGISTRY.get(name)
                items.append({
                    "name": name,
                    "about": reg.doc if reg else None,
                    "settings": [
                        {"name": k, "default": p.default, "min": p.lo, "max": p.hi}
                        for k, p in (reg.params.items() if reg else [])
                    ],
                })
            days[et_day(pr["mergedAt"])]["indicators"].append({
                **base,
                "merged_at": pr.get("mergedAt"),
                "bot": _pr_author_bot(pr),
                "indicators": items,
                "summary": _pr_summary(pr),
                "skeptic": _latest_verdict(pr),
            })
        if kind == "nomination" and pr.get("createdAt"):
            days[et_day(pr["createdAt"])]["nominations"].append({
                **base,
                "bot": _pr_author_bot(pr),
                "state": str(pr.get("state") or "").lower(),
                "opened_at": pr.get("createdAt"),
                "files": [p for p in _pr_files(pr) if p.startswith(NOMINATION_DIR)],
                "skeptic": _latest_verdict(pr),
            })
        for v in skeptic_verdicts(pr):
            day = et_day(v.get("at"))
            if day:
                days[day]["skeptic"].append({**base, "kind": kind, **v})
    for block in days.values():
        block["indicators"].sort(key=lambda x: x.get("merged_at") or "")
        block["skeptic"].sort(key=lambda x: x.get("at") or "")
        block["nominations"].sort(key=lambda x: x.get("opened_at") or "")
    return dict(days)


def _nomination_for(prs: list[dict[str, Any]], bot: str, spec_id: str | None, spec_hash: str) -> dict[str, Any] | None:
    best = None
    for pr in prs:
        if pr_kind(pr) != "nomination":
            continue
        files = _pr_files(pr)
        by_file = any(spec_hash in p for p in files)
        by_branch = bool(spec_id) and str(pr.get("headRefName") or "") == f"{bot}/nominate-{spec_id}"
        if by_file or by_branch:
            if best is None or (pr.get("createdAt") or "") > (best.get("createdAt") or ""):
                best = pr
    if best is None:
        return None
    return {
        "pr": best.get("number"),
        "url": best.get("url"),
        "state": str(best.get("state") or "").lower(),
        "opened_day": et_day(best.get("createdAt")),
        "skeptic": _latest_verdict(best),
    }


# ---------------------------------------------------------------- build


def _stage(idea: Mapping[str, Any]) -> str:
    """One word for where the idea got to (the page's badge)."""
    if idea.get("nomination"):
        return "nominated"
    robust = idea.get("robust")
    if robust:
        return "passed_robust" if robust["passed"] and robust["complete"] else "failed_robust"
    return "beat_benchmarks" if (idea.get("run") or {}).get("beats_benchmarks") else "missed"


def build_days(
    trials: list[dict[str, Any]],
    robust: list[dict[str, Any]],
    notes: Mapping[str, list[dict[str, Any]]],
    prs: list[dict[str, Any]] | None,
    *,
    days: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Journal entries keyed by ET day. ``days`` limits which days are built."""
    wanted = set(days) if days is not None else None
    gh_days = github_by_day(prs) if prs is not None else {}

    first_seen: dict[str, str] = {}
    trial_by_day: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    variants_by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    hashes_until: list[tuple[str, str]] = []
    for rec in trials:
        day = et_day(rec.get("recorded_at"))
        h = rec.get("spec_hash")
        if not day or not h:
            continue
        if rec.get("kind") == "robustness":
            variants_by_day[day][rec.get("parent_hash") or h] += 1
            continue
        hashes_until.append((day, h))
        if rec.get("rescore"):
            continue  # `lab rescore` reruns old specs after a scoring change; not that bot's work today
        if rec.get("author_bot") in INVENTORS:
            first_seen.setdefault(h, day)
            trial_by_day[day][h].append(rec)

    reports_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_reports = set()
    for rep in sorted(robust, key=lambda r: r.get("recorded_at") or ""):
        key = (rep.get("spec_hash"), rep.get("recorded_at"))
        if key in seen_reports or rep.get("author_bot") not in INVENTORS:
            continue
        seen_reports.add(key)
        reports_by_hash[rep["spec_hash"]].append(rep)
    for h, reps in reports_by_hash.items():
        for rep in reps:
            day = et_day(rep.get("recorded_at"))
            if day not in trial_by_day or h not in trial_by_day[day]:
                # A robust run always logs its base trial; this covers a report whose trial file is gone.
                base = [r for r in trials if r.get("spec_hash") == h and r.get("kind") == "trial"
                        and (et_day(r.get("recorded_at")) or "") <= day]
                if base:
                    trial_by_day[day][h].append(base[-1])

    note_days = {it["day"] for items in notes.values() for it in items if it["day"]}
    all_days = set(trial_by_day) | set(gh_days) | note_days
    out: dict[str, dict[str, Any]] = {}
    for day in sorted(all_days):
        if wanted is not None and day not in wanted:
            continue
        ideas = []
        used: dict[str, set[int]] = defaultdict(set)
        for h, recs in trial_by_day.get(day, {}).items():
            rec = recs[-1]
            bot = str(rec.get("author_bot"))
            spec = rec.get("spec") or {}
            spec_id = rec.get("spec_id") or spec.get("id")
            reps = [r for r in reports_by_hash.get(h, []) if (et_day(r.get("recorded_at")) or "") <= day]
            idea_notes = []
            if spec.get("notes"):
                note = clean_text(spec.get("notes"), MAX_NOTE_CHARS)
                if note:
                    idea_notes.append(note)
            matched, positions = match_notes(notes.get(bot, []), spec_id, h, day)
            idea_notes += matched
            used[bot] |= positions
            idea = {
                "spec_id": spec_id,
                "spec_hash": h,
                "bot": bot,
                "category": None,
                "description": clean_text(spec.get("description"), MAX_NOTE_CHARS),
                "what_it_does": describe_spec(spec),
                "new_today": first_seen.get(h) == day,
                "first_seen": first_seen.get(h),
                "runs_today": sum(1 for r in recs if et_day(r.get("recorded_at")) == day),
                "robust_variants_today": variants_by_day.get(day, {}).get(h, 0),
                "run": run_result(rec),
                "robust": robust_result(reps[-1]) if reps else None,
                "nomination": _nomination_for(prs or [], bot, spec_id, h),
                "notes": idea_notes,
            }
            idea["stage"] = _stage(idea)
            ideas.append(idea)
        ideas.sort(key=lambda i: (INVENTORS.index(i["bot"]) if i["bot"] in INVENTORS else 99,
                                  (i["run"] or {}).get("at") or ""))
        gh = gh_days.get(day, {"indicators": [], "skeptic": [], "nominations": []})
        bots = {}
        for bot in INVENTORS:
            mine = [i for i in ideas if i["bot"] == bot]
            bots[bot] = {
                "ideas": len(mine),
                "new": sum(i["new_today"] for i in mine),
                "beat_benchmarks": sum(bool(i["run"]["beats_benchmarks"]) for i in mine),
                "robust": sum(1 for i in mine if i["robust"] and i["robust"]["day"] == day),
                "passed_robust": sum(1 for i in mine if i["stage"] in ("passed_robust", "nominated")),
            }
        stamps = [i["run"]["at"] for i in ideas if i["run"].get("at")]
        stamps += [x.get("merged_at") for x in gh["indicators"]] + [x.get("at") for x in gh["skeptic"]]
        stamps += [x.get("opened_at") for x in gh["nominations"]]
        out[day] = {
            "schema": SCHEMA_VERSION,
            "date": day,
            "window": {"name": DISCOVERY, "start": "2016-01-01", "end": "2021-12-31"},
            "summary": {
                "ideas": len(ideas),
                "new_ideas": sum(i["new_today"] for i in ideas),
                "beat_benchmarks": sum(bool(i["run"]["beats_benchmarks"]) for i in ideas),
                "robust_runs": sum(b["robust"] for b in bots.values()),
                "passed_robust": sum(b["passed_robust"] for b in bots.values()),
                "nominations": len(gh["nominations"]),
                "indicators_merged": sum(len(x["indicators"]) or 1 for x in gh["indicators"]),
                "skeptic_verdicts": len(gh["skeptic"]),
                "discovery_trials_to_date": len({h for d, h in hashes_until if d <= day}),
            },
            "bots": bots,
            "ideas": ideas,
            "indicators": gh["indicators"],
            "skeptic": gh["skeptic"],
            "nominations": gh["nominations"],
            "session_notes": {
                bot: groups for bot in INVENTORS if (groups := session_notes(notes.get(bot, []), day, used[bot]))
            },
            "last_activity_at": max((s for s in stamps if s), default=None),
            "github": prs is not None,
        }
    return out


# ---------------------------------------------------------------- write


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str) + "\n"


def _write_if_changed(path: Path, obj: Mapping[str, Any], ignore: tuple[str, ...] = ()) -> bool:
    old = _read_json(path) if path.is_file() else None
    if old is not None:
        strip = lambda d: {k: v for k, v in d.items() if k not in ignore}  # noqa: E731
        if strip(old) == strip(json.loads(_dump(obj))):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(_dump(obj), encoding="utf-8")
    os.replace(tmp, path)
    return True


def mentions_locked_window(entry: Mapping[str, Any]) -> bool:
    """Last guard before writing: an entry may not mention validation or holdout anywhere."""
    return bool(_LOCKED_TEXT.search(_dump(entry)))


def write_index(out_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    rows = []
    for path in sorted(out_dir.glob("20*.json")):
        d = _read_json(path)
        if not d or not d.get("date"):
            continue
        rows.append({"date": d["date"], **(d.get("summary") or {}), "bots": d.get("bots") or {},
                     "bytes": path.stat().st_size})
    index = {
        "schema": SCHEMA_VERSION,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "timezone": "America/New_York",
        "first_day": rows[0]["date"] if rows else None,
        "last_day": rows[-1]["date"] if rows else None,
        "total_bytes": sum(r["bytes"] for r in rows),
        "days": rows,
    }
    _write_if_changed(out_dir / "index.json", index, ignore=("generated_at",))
    return index


def export_lab_journal(
    out_dir: Path,
    *,
    data_dir: Path,
    lab_work: Path,
    prs: list[dict[str, Any]] | None,
    days: int | None = DEFAULT_DAYS,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rebuild the last ``days`` ET days (all days if None) and the index.

    Days without activity get no file (the page shows an empty state). Existing
    day files are never deleted. A day whose entry still mentions validation or
    holdout anywhere is not written and is listed under ``refused``. If GitHub couldn't be read (``prs`` None), a
    day keeps the indicator, skeptic and nomination lists it already had.
    """
    trials, robust = load_lab_records(data_dir)
    notes, saved_reports = load_lab_work(lab_work)
    end = today or datetime.now(JOURNAL_TZ).date()
    wanted = None if days is None else {(end - timedelta(days=i)).isoformat() for i in range(days)}
    entries = build_days(trials, robust + saved_reports, notes, prs, days=wanted)
    written: list[str] = []
    refused: list[str] = []
    for day, entry in entries.items():
        path = out_dir / f"{day}.json"
        if prs is None and path.is_file():
            old = _read_json(path) or {}
            for key in ("indicators", "skeptic", "nominations"):
                entry[key] = old.get(key) or []
            entry["summary"]["indicators_merged"] = (old.get("summary") or {}).get("indicators_merged", 0)
            entry["summary"]["skeptic_verdicts"] = len(entry["skeptic"])
            entry["summary"]["nominations"] = len(entry["nominations"])
            entry["github"] = bool(old.get("github"))
        if not (entry["ideas"] or entry["indicators"] or entry["skeptic"] or entry["nominations"]
                or entry["session_notes"]):
            continue
        if mentions_locked_window(entry):
            refused.append(day)
            continue
        if _write_if_changed(path, entry):
            written.append(day)
    index = write_index(out_dir, now=now)
    return {"written": written, "refused": refused, "days_in_index": len(index["days"]), "total_bytes": index["total_bytes"],
            "github": prs is not None, "trial_records": len(trials)}


def default_lab_work() -> Path:
    env = os.environ.get(LAB_WORK_ENV)
    return Path(env) if env else DEFAULT_LAB_WORK
