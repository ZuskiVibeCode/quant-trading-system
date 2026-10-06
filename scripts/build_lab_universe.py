"""Build config/lab_universe.yaml: the strategy lab's broad test universe.

Picks ~300 current S&P 1500 members (150 S&P 500 / 100 S&P 400 / 50 S&P 600),
spread across GICS sectors in proportion to each index, keeping only names that
traded on the first sessions of 2016. Within a sector, names are ranked by
2016 median dollar volume (known when discovery starts), not today's liquidity.

Known bias: today's index members are survivors. The Norgate/Sharadar recheck
in the gauntlet covers that.

Run rarely and on purpose; the output is committed so every bot tests the same set.

    pip install lxml   # pandas.read_html backend
    python scripts/build_lab_universe.py [--out config/lab_universe.yaml]
"""

from __future__ import annotations

import argparse
import io
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

INDEXES = (
    # (cap bucket, Wikipedia page, target names)
    ("large", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 150),
    ("mid", "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", 100),
    ("small", "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", 50),
)
LIQUIDITY_YEAR = 2016
# Must have a bar by this date to count as trading since the start of 2016.
FIRST_BAR_BY = pd.Timestamp("2016-01-06")
USER_AGENT ="quant-trading-system lab universe builder (research; paper trading only)"


def fetch_members(url: str) -> pd.DataFrame:
    """Return Symbol + Sector columns from the first Wikipedia table that has both."""
    html = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30).text
    for table in pd.read_html(io.StringIO(html)):
        cols = {str(c).strip().lower(): c for c in table.columns}
        sym = next((cols[k] for k in ("symbol", "ticker symbol", "ticker") if k in cols), None)
        sec = next((cols[k] for k in ("gics sector", "sector") if k in cols), None)
        if sym is not None and sec is not None:
            out = table[[sym, sec]].copy()
            out.columns = ["symbol", "sector"]
            out["symbol"] = out["symbol"].astype(str).str.strip().str.upper()
            out["sector"] = out["sector"].astype(str).str.strip()
            return out.drop_duplicates("symbol")
    raise ValueError(f"No symbol/sector table found at {url}")


def _history_2016(yf_sym: str) -> pd.DataFrame | None:
    """2016 bars for one symbol; None if Yahoo has none (not listed / unknown).

    Rate limits and network errors raise, so the caller retries instead of
    mistaking them for "not listed in 2016".
    """
    import yfinance as yf
    from yfinance.exceptions import (
        YFPricesMissingError,
        YFTickerMissingError,
        YFTzMissingError,
    )

    yf.config.debug.hide_exceptions = False
    try:
        df = yf.Ticker(yf_sym).history(
            start=f"{LIQUIDITY_YEAR}-01-01",
            end=f"{LIQUIDITY_YEAR + 1}-01-01",
            interval="1d",
            auto_adjust=False,  # split-adjusted Close x split-adjusted Volume = raw $ volume
        )
    except (YFPricesMissingError, YFTickerMissingError, YFTzMissingError):
        return None
    return None if df is None or df.empty else df


def liquidity_2016(
    symbols: list[str], *, progress_path: Path, pause: float = 0.4, max_wait: float = 1800.0
) -> dict[str, float]:
    """Median 2016 dollar volume for names trading by FIRST_BAR_BY (yfinance).

    Results (including "not listed") are saved to ``progress_path`` as they come
    in, so a rate-limited run can be resumed by rerunning the script.
    """
    import json
    import time

    done: dict[str, float | None] = {}
    if progress_path.is_file():
        done = json.loads(progress_path.read_text(encoding="utf-8"))
    for i, sym in enumerate(symbols):
        if sym in done:
            continue
        wait = 30.0
        while True:
            try:
                df = _history_2016(sym.replace(".", "-"))
                break
            except Exception as exc:  # noqa: BLE001 — rate limit / network: back off, retry
                if wait > max_wait:
                    raise RuntimeError(f"giving up on {sym} after repeated errors: {exc}") from exc
                print(f"{sym}: {type(exc).__name__}; retrying in {wait:.0f}s")
                time.sleep(wait)
                wait *= 2
        value = None
        if df is not None:
            df = df.dropna(subset=["Close", "Volume"])
            first = df.index.min().tz_localize(None) if len(df) else None
            if first is not None and first <= FIRST_BAR_BY:
                value = float((df["Close"] * df["Volume"]).median())
        done[sym] = value
        if i % 25 == 0:
            progress_path.write_text(json.dumps(done), encoding="utf-8")
        time.sleep(pause)
    progress_path.write_text(json.dumps(done), encoding="utf-8")
    return {s: v for s, v in done.items() if s in symbols and v is not None}


def sector_quotas(counts: dict[str, int], target: int) -> dict[str, int]:
    """Proportional allocation (largest remainder), at least one per sector."""
    total = sum(counts.values())
    target = min(target, total)
    raw = {s: target * n / total for s, n in counts.items()}
    quota = {s: max(1, min(counts[s], math.floor(v))) for s, v in raw.items()}
    by_remainder = sorted(counts, key=lambda s: raw[s] - math.floor(raw[s]), reverse=True)
    while sum(quota.values()) < target:
        for s in by_remainder:
            if sum(quota.values()) >= target:
                break
            if quota[s] < counts[s]:
                quota[s] += 1
    while sum(quota.values()) > target:
        s = max(quota, key=lambda k: quota[k] - raw[k])
        quota[s] -= 1
    return quota


def build(progress_path: Path) -> dict:
    picked: list[dict] = []
    seen: set[str] = set()
    summary = {}
    for bucket, url, target in INDEXES:
        members = fetch_members(url)
        members = members[~members["symbol"].isin(seen)]
        liq = liquidity_2016(members["symbol"].tolist(), progress_path=progress_path)
        eligible = members[members["symbol"].isin(liq)].copy()
        eligible["dollar_volume"] = eligible["symbol"].map(liq)
        quotas = sector_quotas(eligible["sector"].value_counts().to_dict(), target)
        for sector, n in sorted(quotas.items()):
            top = eligible[eligible["sector"] == sector].nlargest(n, "dollar_volume")
            for row in top.itertuples():
                picked.append(
                    {
                        "symbol": row.symbol,
                        "sector": sector,
                        "cap_bucket": bucket,
                        "median_dollar_volume_2016": round(float(row.dollar_volume)),
                    }
                )
                seen.add(row.symbol)
        summary[bucket] = {
            "index_members": int(len(members)),
            "traded_since_2016": int(len(eligible)),
            "picked": int(sum(quotas.values())),
        }
    picked.sort(key=lambda r: r["symbol"])
    return {
        "meta": {
            "built_at": datetime.now(timezone.utc).date().isoformat(),
            "method": (
                "current S&P 500/400/600 members (Wikipedia), traded by "
                f"{FIRST_BAR_BY.date()}, sector-proportional quotas per index, top by "
                f"{LIQUIDITY_YEAR} median dollar volume within sector (yfinance)"
            ),
            "bias": "survivorship: today's index members only (no delisted names)",
            "counts": summary,
        },
        "tickers": picked,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "config" / "lab_universe.yaml"),
    )
    ap.add_argument(
        "--progress",
        default=str(Path(__file__).resolve().parents[1] / "data" / "lab" / "universe_build_progress.json"),
        help="Resume file for per-symbol 2016 liquidity (delete to start fresh)",
    )
    args = ap.parse_args()
    progress = Path(args.progress)
    progress.parent.mkdir(parents=True, exist_ok=True)
    data = build(progress)
    header = (
        "# Strategy lab broad universe. Generated by scripts/build_lab_universe.py;\n"
        "# do not hand-edit. Rebuild only on purpose: every bot tests this same set.\n"
    )
    Path(args.out).write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    print(f"wrote {args.out}: {len(data['tickers'])} tickers {data['meta']['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
