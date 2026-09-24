"""Frozen SQLite schema for Tracker. Migrations must be additive only."""

from __future__ import annotations

SCHEMA_VERSION = 2

# Frozen DDL — do not rename/drop columns without a versioned migration.
DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long', 'short', 'neutral')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 100),
    reasoning TEXT NOT NULL,
    supporting_data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_ticker_created ON signals(ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_signals_agent_created ON signals(agent_id, created_at);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    ticker TEXT,
    payload TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    data_snapshot TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_type_created ON decisions(decision_type, created_at);

CREATE TABLE IF NOT EXISTS trade_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    qty REAL NOT NULL CHECK(qty > 0),
    order_type TEXT NOT NULL DEFAULT 'market',
    rationale TEXT NOT NULL,
    signal_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    limit_price REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('alpaca', 'dry_run', 'alpaca_backfill')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES trade_tickets(id)
);

CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY,
    qty REAL NOT NULL,
    avg_cost REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions_json TEXT NOT NULL,
    daily_pnl REAL,
    drawdown REAL,
    peak_equity REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    related_table TEXT,
    related_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_weights (
    agent_id TEXT PRIMARY KEY,
    weight REAL NOT NULL CHECK(weight >= 0 AND weight <= 1),
    updated_at TEXT NOT NULL,
    notes TEXT
);
"""

DEFAULT_AGENT_IDS = (
    "momentum",
    "fundamental",
    "statistical_ml",
    "sentiment",
    "macro",
)
