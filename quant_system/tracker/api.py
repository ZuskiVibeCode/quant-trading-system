"""Tracker APIs — append-only decision log + portfolio state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from quant_system.models.signal import Signal
from quant_system.models.ticket import HoldDecision, TradeTicket, TicketStatus
from quant_system.tracker.db import connect, init_db, default_db_path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Tracker:
    """FIRST oversight component: persists every decision with reasoning."""

    def __init__(self, db_path: str | None = None):
        self.db_path = str(db_path) if db_path else str(default_db_path())
        self._conn = connect(self.db_path)
        init_db(self._conn)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # --- signals ---
    def insert_signal(self, signal: Signal) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO signals(agent_id, ticker, direction, confidence, reasoning, supporting_data, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                signal.agent_id,
                signal.ticker,
                signal.direction.value if hasattr(signal.direction, "value") else signal.direction,
                float(signal.confidence),
                signal.reasoning,
                json.dumps(signal.supporting_data, default=str),
                signal.created_at,
            ),
        )
        signal_id = int(cur.lastrowid)
        self.log_decision(
            decision_type="signal",
            actor=signal.agent_id,
            ticker=signal.ticker,
            payload={"signal_id": signal_id, **signal.model_dump()},
            reasoning=signal.reasoning,
            data_snapshot=signal.supporting_data,
            created_at=signal.created_at,
        )
        self._conn.commit()
        return signal_id

    def list_signals(
        self,
        ticker: str | None = None,
        agent_id: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM signals WHERE 1=1"
        params: list[Any] = []
        if ticker:
            q += " AND ticker = ?"
            params.append(ticker.upper())
        if agent_id:
            q += " AND agent_id = ?"
            params.append(agent_id)
        if since:
            q += " AND created_at >= ?"
            params.append(since)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_dict(r, json_fields=("supporting_data",)) for r in rows]

    # --- decisions ---
    def log_decision(
        self,
        decision_type: str,
        actor: str,
        reasoning: str,
        payload: dict[str, Any] | None = None,
        ticker: str | None = None,
        data_snapshot: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO decisions(decision_type, actor, ticker, payload, reasoning, data_snapshot, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                decision_type,
                actor,
                ticker,
                json.dumps(payload or {}, default=str),
                reasoning,
                json.dumps(data_snapshot, default=str) if data_snapshot is not None else None,
                created_at or _now(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_decisions(self, decision_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if decision_type:
            rows = self._conn.execute(
                "SELECT * FROM decisions WHERE decision_type = ? ORDER BY id DESC LIMIT ?",
                (decision_type, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(r, json_fields=("payload", "data_snapshot")) for r in rows]

    # --- tickets / holds ---
    def insert_ticket(self, ticket: TradeTicket) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO trade_tickets(ticker, side, qty, order_type, rationale, signal_ids, status, limit_price, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                ticket.ticker.upper(),
                ticket.side.value if hasattr(ticket.side, "value") else ticket.side,
                float(ticket.qty),
                ticket.order_type,
                ticket.rationale,
                json.dumps(ticket.signal_ids),
                ticket.status.value if hasattr(ticket.status, "value") else ticket.status,
                ticket.limit_price,
                ticket.created_at,
            ),
        )
        ticket_id = int(cur.lastrowid)
        self.log_decision(
            decision_type="ticket",
            actor="portfolio_manager",
            ticker=ticket.ticker,
            payload={"ticket_id": ticket_id, **ticket.model_dump()},
            reasoning=ticket.rationale,
            created_at=ticket.created_at,
        )
        self._conn.commit()
        return ticket_id

    def insert_hold(self, hold: HoldDecision) -> int:
        return self.log_decision(
            decision_type="hold",
            actor="portfolio_manager",
            ticker=hold.ticker,
            payload=hold.model_dump(),
            reasoning=hold.rationale,
            data_snapshot=hold.data_snapshot,
            created_at=hold.created_at,
        )

    def update_ticket_status(self, ticket_id: int, status: TicketStatus | str) -> None:
        st = status.value if hasattr(status, "value") else status
        self._conn.execute(
            "UPDATE trade_tickets SET status = ? WHERE id = ?", (st, ticket_id)
        )
        self._conn.commit()

    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM trade_tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_dict(row, json_fields=("signal_ids",))

    # --- fills / positions ---
    def insert_fill(
        self,
        ticket_id: int,
        ticker: str,
        side: str,
        qty: float,
        price: float,
        mode: str,
        created_at: str | None = None,
    ) -> int:
        ts = created_at or _now()
        cur = self._conn.execute(
            """
            INSERT INTO fills(ticket_id, ticker, side, qty, price, mode, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (ticket_id, ticker.upper(), side, qty, price, mode, ts),
        )
        fill_id = int(cur.lastrowid)
        self._apply_fill_to_position(ticker.upper(), side, qty, price, ts)
        self.update_ticket_status(ticket_id, TicketStatus.FILLED)
        self.log_decision(
            decision_type="fill",
            actor="execution_agent",
            ticker=ticker,
            payload={
                "fill_id": fill_id,
                "ticket_id": ticket_id,
                "side": side,
                "qty": qty,
                "price": price,
                "mode": mode,
            },
            reasoning=f"Filled {side} {qty} {ticker} @ {price} ({mode})",
            created_at=ts,
        )
        self._conn.commit()
        return fill_id

    def _apply_fill_to_position(
        self, ticker: str, side: str, qty: float, price: float, ts: str
    ) -> None:
        row = self._conn.execute(
            "SELECT qty, avg_cost FROM positions WHERE ticker = ?", (ticker,)
        ).fetchone()
        cash = float(self.get_state("cash", "100.0"))
        if side == "buy":
            cost = qty * price
            cash -= cost
            if row is None:
                self._conn.execute(
                    "INSERT INTO positions(ticker, qty, avg_cost, updated_at) VALUES (?,?,?,?)",
                    (ticker, qty, price, ts),
                )
            else:
                old_qty = float(row["qty"])
                old_cost = float(row["avg_cost"])
                new_qty = old_qty + qty
                new_avg = ((old_qty * old_cost) + cost) / new_qty if new_qty else 0.0
                self._conn.execute(
                    "UPDATE positions SET qty=?, avg_cost=?, updated_at=? WHERE ticker=?",
                    (new_qty, new_avg, ts, ticker),
                )
        else:  # sell
            proceeds = qty * price
            cash += proceeds
            if row is None:
                raise ValueError(f"Cannot sell {ticker}: no position")
            old_qty = float(row["qty"])
            new_qty = old_qty - qty
            if new_qty < -1e-9:
                raise ValueError(f"Cannot sell more than held for {ticker}")
            if new_qty <= 1e-9:
                self._conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
            else:
                self._conn.execute(
                    "UPDATE positions SET qty=?, updated_at=? WHERE ticker=?",
                    (new_qty, ts, ticker),
                )
        self.set_state("cash", f"{cash:.6f}")

    def clear_positions(self) -> int:
        """Delete all local position rows. Returns rows deleted."""
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM positions")
        n = int(cur.fetchone()["n"])
        self._conn.execute("DELETE FROM positions")
        self._conn.commit()
        return n

    def reconcile_portfolio(
        self,
        cash: float,
        equity: float,
        positions: list[dict[str, Any]],
        source: str = "alpaca",
    ) -> dict[str, Any]:
        """Reset local cash/equity/positions to match an external broker snapshot.

        Clears phantom positions and overwrites cash/equity state. Does not invent fills.
        """
        ts = _now()
        cleared = self.clear_positions()
        written = 0
        for p in positions:
            ticker = str(p.get("ticker") or p.get("symbol") or "").upper()
            if not ticker:
                continue
            qty = float(p.get("qty") or 0)
            if abs(qty) <= 1e-12:
                continue
            avg_cost = float(p.get("avg_cost") or p.get("avg_entry_price") or 0)
            self._conn.execute(
                "INSERT INTO positions(ticker, qty, avg_cost, updated_at) VALUES (?,?,?,?)",
                (ticker, qty, avg_cost, ts),
            )
            written += 1
        peak = max(float(self.get_state("peak_equity", "0") or 0), float(equity))
        day_start = float(self.get_state("day_start_equity", str(equity)) or equity)
        self.snapshot_portfolio(
            equity=float(equity),
            cash=float(cash),
            positions=self.list_positions(),
            daily_pnl=float(equity) - day_start,
            drawdown=(peak - float(equity)) / peak if peak else 0.0,
            peak_equity=peak,
        )
        self.log_decision(
            decision_type="reconcile",
            actor="tracker",
            reasoning=f"Reconciled portfolio state from {source}",
            payload={
                "source": source,
                "cash": float(cash),
                "equity": float(equity),
                "positions_cleared": cleared,
                "positions_written": written,
                "positions": self.list_positions(),
            },
        )
        return {
            "source": source,
            "cash": float(cash),
            "equity": float(equity),
            "positions_cleared": cleared,
            "positions_written": written,
            "positions": self.list_positions(),
        }

    def list_positions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM positions ORDER BY ticker").fetchall()
        return [dict(r) for r in rows]

    def list_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- portfolio snapshots ---
    def snapshot_portfolio(
        self,
        equity: float,
        cash: float,
        positions: list[dict[str, Any]],
        daily_pnl: float | None = None,
        drawdown: float | None = None,
        peak_equity: float | None = None,
    ) -> int:
        ts = _now()
        cur = self._conn.execute(
            """
            INSERT INTO portfolio_snapshots(equity, cash, positions_json, daily_pnl, drawdown, peak_equity, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                equity,
                cash,
                json.dumps(positions, default=str),
                daily_pnl,
                drawdown,
                peak_equity,
                ts,
            ),
        )
        self.set_state("equity", f"{equity:.6f}")
        self.set_state("cash", f"{cash:.6f}")
        if peak_equity is not None:
            self.set_state("peak_equity", f"{peak_equity:.6f}")
        self._conn.commit()
        return int(cur.lastrowid)

    def latest_snapshot(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return self._row_to_dict(row, json_fields=("positions_json",))

    # --- halt / resume ---
    def is_halted(self) -> bool:
        return self.get_state("halted", "false").lower() == "true"

    def is_day_halted(self) -> bool:
        return self.get_state("day_halted", "false").lower() == "true"

    def halt(self, reason: str, day_only: bool = False) -> None:
        if day_only:
            self.set_state("day_halted", "true")
        self.set_state("halted", "true")
        self.set_state("halt_reason", reason)
        self.log_decision(
            decision_type="halt",
            actor="kill_switch",
            reasoning=reason,
            payload={"day_only": day_only},
        )

    def resume(self, reason: str = "manual resume") -> None:
        self.set_state("halted", "false")
        self.set_state("day_halted", "false")
        self.set_state("halt_reason", "")
        self.log_decision(
            decision_type="resume",
            actor="kill_switch",
            reasoning=reason,
            payload={},
        )

    def orders_blocked(self) -> tuple[bool, str]:
        if self.is_halted():
            return True, self.get_state("halt_reason", "halted")
        if self.is_day_halted():
            return True, "day halt active"
        return False, ""

    # --- system state / weights / audit ---
    def get_state(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        ts = _now()
        self._conn.execute(
            """
            INSERT INTO system_state(key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, ts),
        )
        self._conn.commit()

    def get_weights(self) -> dict[str, float]:
        rows = self._conn.execute("SELECT agent_id, weight FROM agent_weights").fetchall()
        return {r["agent_id"]: float(r["weight"]) for r in rows}

    def set_weight(self, agent_id: str, weight: float, notes: str = "") -> None:
        ts = _now()
        self._conn.execute(
            """
            INSERT INTO agent_weights(agent_id, weight, updated_at, notes) VALUES (?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at, notes=excluded.notes
            """,
            (agent_id, weight, ts, notes),
        )
        self._conn.commit()

    def insert_audit_flag(
        self,
        flag_type: str,
        severity: str,
        message: str,
        related_table: str | None = None,
        related_id: int | None = None,
    ) -> int:
        ts = _now()
        cur = self._conn.execute(
            """
            INSERT INTO audit_flags(flag_type, severity, message, related_table, related_id, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (flag_type, severity, message, related_table, related_id, ts),
        )
        flag_id = int(cur.lastrowid)
        self.log_decision(
            decision_type="audit",
            actor="auditor",
            reasoning=message,
            payload={
                "flag_id": flag_id,
                "flag_type": flag_type,
                "severity": severity,
                "related_table": related_table,
                "related_id": related_id,
            },
            created_at=ts,
        )
        self._conn.commit()
        return flag_id

    def list_audit_flags(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM audit_flags ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def digest(self, limit: int = 20) -> dict[str, Any]:
        """Aggregate view for CLI digest command."""
        out: dict[str, Any] = {
            "halted": self.is_halted(),
            "halt_reason": self.get_state("halt_reason", ""),
            "day_halted": self.is_day_halted(),
            "cash": float(self.get_state("cash", "100")),
            "equity": float(self.get_state("equity", "100")),
            "peak_equity": float(self.get_state("peak_equity", "100")),
            "weights": self.get_weights(),
            "positions": self.list_positions(),
            "recent_signals": self.list_signals(limit=limit),
            "recent_decisions": self.list_decisions(limit=limit),
            "recent_fills": self.list_fills(limit=limit),
            "recent_audit_flags": self.list_audit_flags(limit=limit),
            "latest_snapshot": self.latest_snapshot(),
            "db_path": self.db_path,
        }
        try:
            from quant_system.research.rotation import rotation_digest_section

            out["rotation"] = rotation_digest_section(self)
            out["cold"] = out["rotation"].get("cold", [])
        except Exception:  # noqa: BLE001 — digest must not fail if rotation unavailable
            out["rotation"] = {"active": {}, "cold": [], "settings": {}}
            out["cold"] = []
        return out

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, json_fields: tuple[str, ...] = ()) -> dict[str, Any]:
        d = dict(row)
        for f in json_fields:
            if f in d and isinstance(d[f], str):
                try:
                    d[f] = json.loads(d[f])
                except json.JSONDecodeError:
                    pass
        return d
