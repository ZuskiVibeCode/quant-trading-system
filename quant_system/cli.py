"""CLI: python -m quant_system run-once | halt | resume | status | digest"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from quant_system.config.loader import load_config
from quant_system.pipeline import run_once
from quant_system.tracker.api import Tracker


def cmd_run_once(args: argparse.Namespace) -> int:
    cfg = load_config()
    tracker = Tracker(args.db)
    # Seed starting equity into state if fresh
    if tracker.latest_snapshot() is None:
        eq = cfg.risk.starting_equity
        tracker.set_state("cash", f"{eq:.6f}")
        tracker.set_state("equity", f"{eq:.6f}")
        tracker.set_state("peak_equity", f"{eq:.6f}")
        tracker.set_state("day_start_equity", f"{eq:.6f}")
        tracker.snapshot_portfolio(equity=eq, cash=eq, positions=[], daily_pnl=0.0, drawdown=0.0, peak_equity=eq)

    tickers = None
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    summary = run_once(
        tracker=tracker,
        config=cfg,
        tickers=tickers,
        mock=args.mock,
    )
    print(json.dumps(_jsonable(summary), indent=2, default=str))
    tracker.close()
    return 0


def cmd_halt(args: argparse.Namespace) -> int:
    tracker = Tracker(args.db)
    reason = args.reason or "manual halt via CLI"
    tracker.halt(reason, day_only=args.day)
    print(json.dumps({"halted": True, "reason": reason, "day_only": args.day}))
    tracker.close()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    tracker = Tracker(args.db)
    reason = args.reason or "manual resume via CLI"
    tracker.resume(reason)
    print(json.dumps({"halted": False, "reason": reason}))
    tracker.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    tracker = Tracker(args.db)
    out = {
        "halted": tracker.is_halted(),
        "day_halted": tracker.is_day_halted(),
        "halt_reason": tracker.get_state("halt_reason", ""),
        "cash": float(tracker.get_state("cash", "100")),
        "equity": float(tracker.get_state("equity", "100")),
        "peak_equity": float(tracker.get_state("peak_equity", "100")),
        "positions": tracker.list_positions(),
        "weights": tracker.get_weights(),
        "db_path": tracker.db_path,
    }
    print(json.dumps(out, indent=2, default=str))
    tracker.close()
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    tracker = Tracker(args.db)
    dig = tracker.digest(limit=args.limit)
    print(json.dumps(_jsonable(dig), indent=2, default=str))
    tracker.close()
    return 0


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quant_system", description="Multi-agent paper quant trading CLI")
    p.add_argument("--db", default=None, help="SQLite Tracker path (default: data/tracker.db)")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run-once", help="One research+execution cycle")
    r.add_argument("--mock", action="store_true", help="Use MockMarketData (offline)")
    r.add_argument("--tickers", default=None, help="Comma-separated ticker subset")
    r.set_defaults(func=cmd_run_once)

    h = sub.add_parser("halt", help="Kill switch: block new orders")
    h.add_argument("--reason", default=None)
    h.add_argument("--day", action="store_true", help="Mark as day halt")
    h.set_defaults(func=cmd_halt)

    rs = sub.add_parser("resume", help="Clear halt; allow orders")
    rs.add_argument("--reason", default=None)
    rs.set_defaults(func=cmd_resume)

    st = sub.add_parser("status", help="Account/halt/positions summary")
    st.set_defaults(func=cmd_status)

    dg = sub.add_parser("digest", help="Digest from Tracker tables")
    dg.add_argument("--limit", type=int, default=20)
    dg.set_defaults(func=cmd_digest)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
