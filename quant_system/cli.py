"""CLI: python -m quant_system run-once | halt | resume | status | digest | cancel-open-orders | reconcile | backfill-fills | export-dashboard | backtest-themes"""

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



def cmd_backfill_fills(args: argparse.Namespace) -> int:
    """Backfill missing Alpaca paper buy fills for Closed Book entry/PnL (no position mutate)."""
    from quant_system.execution.broker import backfill_missing_buy_fills

    tracker = Tracker(args.db)
    symbols = None
    if args.tickers:
        symbols = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    try:
        result = backfill_missing_buy_fills(tracker, symbols=symbols)
        tracker.log_decision(
            decision_type="backfill_fills",
            actor="cli",
            reasoning=(
                f"Backfilled {len(result.get('inserted') or [])} buy fills; "
                f"skipped {len(result.get('skipped') or [])}; "
                f"unresolved {result.get('unresolved')}"
            ),
            payload={
                "missing_tickers": result.get("missing_tickers"),
                "inserted_count": len(result.get("inserted") or []),
                "skipped_count": len(result.get("skipped") or []),
                "unresolved": result.get("unresolved"),
                "inserted": result.get("inserted"),
                "skipped": result.get("skipped"),
            },
        )
        print(json.dumps(_jsonable(result), indent=2, default=str))
        tracker.close()
        return 0 if not result.get("unresolved") else 1
    except Exception as exc:  # noqa: BLE001
        tracker.log_decision(
            decision_type="backfill_fills",
            actor="cli",
            reasoning=f"backfill-fills failed: {exc}",
            payload={"error": str(exc)},
        )
        print(json.dumps({"error": str(exc)}, indent=2))
        tracker.close()
        return 1


def cmd_export_dashboard(args: argparse.Namespace) -> int:
    """Export Tracker (+ optional Alpaca snapshot) to docs/data/latest.json for GitHub Pages."""
    from pathlib import Path

    from quant_system.dashboard_export import export_dashboard

    tracker = Tracker(args.db)
    docs = Path(args.docs) if args.docs else None
    try:
        result = export_dashboard(
            tracker,
            docs_dir=docs,
            include_alpaca=not args.no_alpaca,
            append_history=not args.no_history,
            close_tracker=False,
        )
        print(json.dumps(_jsonable(result), indent=2, default=str))
        tracker.close()
        return 0
    except Exception as exc:  # noqa: BLE001
        tracker.log_decision(
            decision_type="export_dashboard",
            actor="cli",
            reasoning=f"export-dashboard failed: {exc}",
            payload={"error": str(exc)},
        )
        print(json.dumps({"error": str(exc)}, indent=2))
        tracker.close()
        return 1



def cmd_backtest_themes(args: argparse.Namespace) -> int:
    """Replay quantum/space/robotics sleeves on cached daily OHLCV; print leaderboard."""
    from datetime import date, timedelta
    from pathlib import Path

    from quant_system.backtest.harness import format_leaderboard_table, run_theme_backtest

    end = args.end
    start = args.start
    if not end:
        end = date.today().isoformat()
    if not start:
        start = (date.fromisoformat(end) - timedelta(days=90)).isoformat()

    out_dir = Path(args.output) if args.output else (
        Path(__file__).resolve().parents[1] / "artifacts" / "backtest"
    )
    result = run_theme_backtest(
        start=start,
        end=end,
        cash_per_sleeve=float(args.cash),
        slippage_bps=float(args.slippage_bps),
        fill_on=args.fill_on,
        force_download=bool(args.force_download),
        cache_dir=args.cache_dir,
        fundamentals_cache_dir=args.fundamentals_cache_dir,
        output_dir=out_dir,
        min_confidence_to_trade=float(args.min_confidence_to_trade),
        min_confidence_to_sell=float(args.min_confidence_to_sell),
        min_abs_score=float(args.min_abs_score),
    )
    print(format_leaderboard_table(result))
    print()
    print(json.dumps(_jsonable(result), indent=2, default=str))
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

    bf = sub.add_parser(
        "backfill-fills",
        help="Insert missing Alpaca paper buy fills for Closed Book (no position mutate)",
    )
    bf.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated tickers (default: auto-detect sells-without-buys)",
    )
    bf.set_defaults(func=cmd_backfill_fills)

    ed = sub.add_parser(
        "export-dashboard",
        help="Export Tracker snapshot to docs/data/latest.json (GitHub Pages viewer)",
    )
    ed.add_argument(
        "--docs",
        default=None,
        help="Docs output directory (default: <repo>/docs)",
    )
    ed.add_argument(
        "--no-alpaca",
        action="store_true",
        help="Skip optional Alpaca reconcile snapshot",
    )
    ed.add_argument(
        "--no-history",
        action="store_true",
        help="Do not append docs/data/history/YYYY-MM-DD.json",
    )
    ed.set_defaults(func=cmd_export_dashboard)

    bt = sub.add_parser(
        "backtest-themes",
        help="Theme competition backtest (quantum/space/robotics) on cached daily OHLCV",
    )
    bt.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: end-90d)")
    bt.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: today)")
    bt.add_argument(
        "--cash",
        type=float,
        default=100.0,
        help="Starting cash per sleeve (default: 100)",
    )
    bt.add_argument(
        "--slippage-bps",
        type=float,
        default=5.0,
        help="Constant fill slippage in bps (default: 5)",
    )
    bt.add_argument(
        "--fill-on",
        choices=("close", "next_open"),
        default="close",
        help="Fill model price: same-bar close (default) or next-bar open",
    )
    bt.add_argument(
        "--cache-dir",
        default=None,
        help="OHLCV cache directory (default: data/backtest/bars)",
    )
    bt.add_argument(
        "--output",
        default=None,
        help="Leaderboard JSON directory (default: artifacts/backtest)",
    )
    bt.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download Yahoo bars even if cache looks fresh",
    )
    bt.add_argument(
        "--fundamentals-cache-dir",
        default=None,
        help="As-of fundamentals cache (default: data/backtest/fundamentals)",
    )
    bt.add_argument(
        "--min-confidence-to-trade",
        type=float,
        default=35.0,
        help=(
            "Backtest-only buy confidence floor (default: 35; live run-once uses "
            "config/risk.yaml 55). Does not rewrite live defaults."
        ),
    )
    bt.add_argument(
        "--min-confidence-to-sell",
        type=float,
        default=25.0,
        help=(
            "Backtest-only sell confidence floor (default: 25; live run-once uses "
            "config/risk.yaml 40)."
        ),
    )
    bt.add_argument(
        "--min-abs-score",
        type=float,
        default=0.02,
        help=(
            "Backtest-only |blended score| floor before HOLD (default: 0.02; "
            "live run-once uses 0.05)."
        ),
    )
    bt.set_defaults(func=cmd_backtest_themes)

    return p


def main(argv: list[str] | None = None) -> int:
    from quant_system.config.loader import load_project_dotenv

    load_project_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
