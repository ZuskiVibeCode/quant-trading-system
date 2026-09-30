"""Closed book UI day-window contract (America/New_York today + yesterday).

Export keeps full ``closed`` history for Lifetime / sector realized; the Orbit
viewer filters only the Closed section display. These tests lock the calendar
rules that ``docs/app.js`` implements with ``etCalendarDay`` /
``etTodayYesterday`` / ``closedRowsForRecentWindow``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "docs" / "app.js"


def _et_ymd(dt: datetime) -> str:
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def test_app_js_defines_closed_day_window_helpers():
    src = APP_JS.read_text()
    assert "function etCalendarDay" in src
    assert "function etTodayYesterday" in src
    assert "function closedRowsForRecentWindow" in src
    assert "No closes today or yesterday (ET)" in src
    assert "UI-only window: full DATA.closed stays intact" in src
    # Lifetime path must still consume full ticker_pnl / not share this filter
    assert "function renderLifetime" in src
    assert "DATA.ticker_pnl" in src or "ticker_pnl" in src


def test_node_et_calendar_day_and_window_filter():
    """Run the same Intl/en-CA rules as app.js via node."""
    script = r"""
function etCalendarDay(isoOrDate) {
  if (isoOrDate == null || isoOrDate === "") return null;
  const d = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
}
function etTodayYesterday(now = new Date()) {
  const today = now.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const [Y, M, D] = today.split("-").map(Number);
  const prev = new Date(Date.UTC(Y, M - 1, D));
  prev.setUTCDate(prev.getUTCDate() - 1);
  const yesterday = prev.toISOString().slice(0, 10);
  return { today, yesterday };
}
function closedRowsForRecentWindow(closed, now) {
  const { today, yesterday } = etTodayYesterday(now);
  return (closed || [])
    .map((c) => {
      const day = etCalendarDay(c.closed_at);
      let bucket = null;
      if (day === today) bucket = "today";
      else if (day === yesterday) bucket = "yesterday";
      return { row: c, day, bucket };
    })
    .filter((x) => x.bucket);
}

const now = new Date("2026-09-30T17:00:00.000Z"); // afternoon ET on Sep 30
const { today, yesterday } = etTodayYesterday(now);
const closed = [
  { ticker: "AAA", closed_at: "2026-09-30T14:00:00.000Z" },
  { ticker: "BBB", closed_at: "2026-09-29T20:00:00.000Z" },
  { ticker: "CCC", closed_at: "2026-09-28T20:00:00.000Z" },
  { ticker: "DDD", closed_at: "2026-09-30T03:30:00.000Z" }, // still Sep 29 ET
];
const recent = closedRowsForRecentWindow(closed, now);
console.log(JSON.stringify({ today, yesterday, tickers: recent.map(x => [x.row.ticker, x.bucket]) }));
"""
    out = subprocess.check_output(["node", "-e", script], text=True).strip()
    payload = json.loads(out)
    assert payload["today"] == "2026-09-30"
    assert payload["yesterday"] == "2026-09-29"
    assert payload["tickers"] == [
        ["AAA", "today"],
        ["BBB", "yesterday"],
        ["DDD", "yesterday"],
    ]


def test_python_et_window_matches_meta_day_local():
    """Sanity: latest.json meta day_local is ET; older closes fall outside window."""
    latest = ROOT / "docs" / "data" / "latest.json"
    if not latest.exists():
        pytest.skip("no latest.json")
    data = json.loads(latest.read_text())
    meta = data.get("meta") or {}
    assert meta.get("day_timezone") == "America/New_York"
    day_local = meta.get("day_local")
    if not day_local:
        pytest.skip("no day_local")
    today = datetime.strptime(day_local, "%Y-%m-%d").replace(tzinfo=ET)
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    closed = data.get("closed") or []
    in_window = []
    for c in closed:
        day = _et_ymd(datetime.fromisoformat(str(c["closed_at"]).replace("Z", "+00:00")))
        if day in {day_local, yesterday}:
            in_window.append(c["ticker"])
    # Full history must be larger than the 2-day window when book has history
    if len(closed) > 5:
        assert len(in_window) < len(closed)
    assert all(isinstance(t, str) for t in in_window)
