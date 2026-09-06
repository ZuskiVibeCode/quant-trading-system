"""SQLite connection helpers for Tracker."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from quant_system.tracker.schema import DDL, DEFAULT_AGENT_IDS, SCHEMA_VERSION


def default_db_path() -> Path:
    env = os.environ.get("QUANT_DB_PATH")
    if env:
        return Path(env)
    # Prefer repo-local data/ when cwd is the project root
    root = Path(__file__).resolve().parents[2]
    return root / "data" / "tracker.db"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply frozen schema and seed defaults if empty."""
    conn.executescript(DDL)
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    # Halt state defaults
    _ensure_state(conn, "halted", "false")
    _ensure_state(conn, "halt_reason", "")
    _ensure_state(conn, "day_halted", "false")
    _ensure_state(conn, "peak_equity", "100.0")
    _ensure_state(conn, "day_start_equity", "100.0")
    _ensure_state(conn, "cash", "100.0")
    _ensure_state(conn, "equity", "100.0")

    # Equal weights seed
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    n = len(DEFAULT_AGENT_IDS)
    equal = 1.0 / n
    for agent_id in DEFAULT_AGENT_IDS:
        existing = conn.execute(
            "SELECT 1 FROM agent_weights WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO agent_weights(agent_id, weight, updated_at, notes) VALUES (?,?,?,?)",
                (agent_id, equal, now, "initial equal weight"),
            )
    conn.commit()


def _ensure_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row = conn.execute("SELECT 1 FROM system_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO system_state(key, value, updated_at) VALUES (?,?,?)",
            (key, value, now),
        )
