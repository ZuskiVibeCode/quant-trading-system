"""CLI: python -m quant_system run-once | halt | resume | status | digest | cancel-open-orders | reconcile"""

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
    out: dict[str, Any] = {
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
    if args.broker:
        try:
            from quant_system.execution.broker import fetch_account_snapshot

            snap = fetch_account_snapshot()
            out["alpaca"] = {
                "cash": snap["cash"],
                "equity": snap["equity"],
                "buying_power": snap["buying_power"],
                "open_orders_count": snap["open_orders_count"],
                "positions": snap["positions"],
            }
        except Exception as exc:  # noqa: BLE001
            out["alpaca_error"] = str(exc)
    print(json.dumps(out, indent=2, default=str))
    tracker.close()
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    tracker = Tracker(args.db)
    dig = tracker.digest(limit=args.limit)
    print(json.dumps(_jsonable(dig), indent=2, default=str))
    tracker.close()
    return 0


def cmd_cancel_open_orders(args: argparse.Namespace) -> int:
    """Cancel all open Alpaca paper orders to free buying power."""
    from quant_system.execution.broker import cancel_all_open_orders

    tracker = Tracker(args.db)
    try:
        result = cancel_all_open_orders()
        tracker.log_decision(
            decision_type="cancel_open_orders",
            actor="cli",
            reasoning="Cancelled open Alpaca paper orders",
            payload={
                "cancelled_count": result.get("cancelled_count"),
                "open_orders_before": result.get("open_orders_before"),
                "open_orders_after": result.get("open_orders_after"),
                "buying_power_after": result.get("buying_power_after"),
                "errors": result.get("errors"),
            },
        )
        print(json.dumps(result, indent=2, default=str))
        tracker.close()
        return 0 if not result.get("errors") else 1
    except Exception as exc:  # noqa: BLE001
        tracker.log_decision(
            decision_type="cancel_open_orders",
            actor="cli",
            reasoning=f"cancel-open-orders failed: {exc}",
            payload={"error": str(exc)},
        )
        print(json.dumps({"error": str(exc)}, indent=2))
        tracker.close()
        return 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Reset tracker cash/equity/positions from Alpaca account+positions."""
    from quant_system.execution.broker import reconcile_tracker_to_alpaca

    tracker = Tracker(args.db)
    try:
        result = reconcile_tracker_to_alpaca(tracker)
        print(json.dumps(_jsonable(result), indent=2, default=str))
        tracker.close()
        return 0
    except Exception as exc:  # noqa: BLE001
        tracker.log_decision(
            decision_type="reconcile",
            actor="cli",
            reasoning=f"reconcile failed: {exc}",
            payload={"error": str(exc)},
        )
        print(json.dumps({"error": str(exc)}, indent=2))
        tracker.close()
        return 1


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
    st.add_argument("--broker", action="store_true", help="Include Alpaca cash/equity/BP/open orders")
    st.set_defaults(func=cmd_status)

    dg = sub.add_parser("digest", help="Digest from Tracker tables")
    dg.add_argument("--limit", type=int, default=20)
    dg.set_defaults(func=cmd_digest)

    co = sub.add_parser(
        "cancel-open-orders",
        help="Cancel all open Alpaca paper orders (frees reserved buying power)",
    )
    co.set_defaults(func=cmd_cancel_open_orders)

    rc = sub.add_parser(
        "reconcile",
        help="Set tracker cash/equity/positions from Alpaca account+positions",
    )
    rc.set_defaults(func=cmd_reconcile)

    return p


def _load_dotenv() -> None:
    """Load project .env into os.environ if present (does not override existing)."""
    import os
    from pathlib import Path as _Path

    candidates = [
        _Path.cwd() / ".env",
        _Path(__file__).resolve().parents[1] / ".env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)
        break


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
