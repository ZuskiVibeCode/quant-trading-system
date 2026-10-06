"""Strategy lab data layer: Alpaca provider, shared cache, data ceiling, universes."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from quant_system.backtest.cache import save_bars
from quant_system.lab.data import (
    AlpacaBarsProvider,
    bars_dir,
    cache_status,
    load_lab_bars,
    read_manifest,
    sync_bars,
)
from quant_system.lab.settings import (
    CEILING_ENV,
    LabDataSettings,
    LabSettings,
    data_ceiling,
    load_lab_settings,
)
from quant_system.lab.universe import universe_symbols

TODAY = date(2026, 10, 5)
SETTINGS = LabSettings(data=LabDataSettings(start=date(2021, 12, 1), ceiling=date(2021, 12, 31)))


@pytest.fixture(autouse=True)
def _no_ceiling_override(monkeypatch):
    monkeypatch.delenv(CEILING_ENV, raising=False)


def _ohlcv(start: str, end: str) -> pd.DataFrame:
    idx = pd.bdate_range(start, end)
    px = [100.0 + i for i in range(len(idx))]
    return pd.DataFrame(
        {"Open": px, "High": px, "Low": px, "Close": px, "Volume": 1_000_000}, index=idx
    )


class FakeProvider:
    source = "fake"

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.calls: list[tuple[list[str], date, date]] = []

    def fetch(self, symbols, start, end):
        self.calls.append((list(symbols), start, end))
        # Deliberately ignores `end` so the cache layer must clip.
        return {s: self.frames[s] for s in symbols if s in self.frames}


def test_sync_clips_to_ceiling_and_writes_manifest(tmp_path):
    prov = FakeProvider({"AAA": _ohlcv("2021-12-01", "2022-01-14")})
    report = sync_bars(["aaa"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY)

    assert prov.calls == [(["AAA"], date(2021, 12, 1), date(2021, 12, 31))]
    assert report["fetched"] == ["AAA"] and report["ceiling"] == "2021-12-31"
    on_disk = pd.read_csv(bars_dir(tmp_path) / "AAA.csv", index_col=0, parse_dates=True)
    assert on_disk.index.max() <= pd.Timestamp("2021-12-31")
    entry = read_manifest(tmp_path)["tickers"]["AAA"]
    assert entry["last"] == "2021-12-31" and entry["fetched_through"] == "2021-12-31"


def test_sync_skips_current_records_missing_and_force_refetches(tmp_path):
    prov = FakeProvider({"AAA": _ohlcv("2021-12-01", "2021-12-31")})
    sync_bars(["AAA", "GONE"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    again = sync_bars(["AAA", "GONE"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    assert len(prov.calls) == 1  # both already current (GONE recorded as missing)
    assert again["already_current"] == 2
    assert read_manifest(tmp_path)["missing"].keys() == {"GONE"}

    sync_bars(["AAA"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY, force=True)
    assert len(prov.calls) == 2


def test_raising_the_ceiling_triggers_a_full_refetch(tmp_path, monkeypatch):
    prov = FakeProvider({"AAA": _ohlcv("2021-12-01", "2022-03-31")})
    sync_bars(["AAA"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    monkeypatch.setenv(CEILING_ENV, "2022-03-31")
    sync_bars(["AAA"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    assert prov.calls[-1][1:] == (date(2021, 12, 1), date(2022, 3, 31))
    assert read_manifest(tmp_path)["tickers"]["AAA"]["last"] == "2022-03-31"


def test_load_drops_rows_past_ceiling_even_if_cached(tmp_path):
    save_bars("LEAK", _ohlcv("2021-12-01", "2022-06-30"), bars_dir(tmp_path))
    bars = load_lab_bars(["leak", "nothing"], settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    assert list(bars) == ["LEAK"]
    assert bars["LEAK"].index.max() == pd.Timestamp("2021-12-31")


def test_status_flags_rows_past_ceiling(tmp_path, monkeypatch):
    prov = FakeProvider({"AAA": _ohlcv("2021-12-01", "2022-03-31")})
    monkeypatch.setenv(CEILING_ENV, "2022-03-31")
    sync_bars(["AAA"], provider=prov, settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    monkeypatch.delenv(CEILING_ENV)
    status = cache_status(settings=SETTINGS, data_dir=tmp_path, today=TODAY)
    assert status["rows_past_ceiling"] == ["AAA"]


def test_data_ceiling_overrides_and_never_reaches_today(monkeypatch):
    assert data_ceiling(SETTINGS, today=TODAY) == date(2021, 12, 31)
    monkeypatch.setenv(CEILING_ENV, "2024-12-31")
    assert data_ceiling(SETTINGS, today=TODAY) == date(2024, 12, 31)
    monkeypatch.setenv(CEILING_ENV, "today")
    assert data_ceiling(SETTINGS, today=TODAY) == date(2026, 10, 4)
    monkeypatch.setenv(CEILING_ENV, "2030-01-01")
    assert data_ceiling(SETTINGS, today=TODAY) == date(2026, 10, 4)


def test_repo_lab_settings_default_to_discovery_ceiling():
    s = load_lab_settings()
    assert s.data.ceiling == date(2021, 12, 31)
    assert s.data.start == date(2016, 1, 1)
    assert (s.data.feed, s.data.adjustment) == ("sip", "all")


def test_alpaca_provider_requests_adjusted_sip_and_maps_session_dates():
    from alpaca.data.enums import Adjustment, DataFeed

    # Alpaca stamps daily bars at midnight ET, i.e. 05:00 UTC in winter.
    ts = pd.to_datetime(["2021-12-30 05:00", "2021-12-31 05:00"], utc=True)
    idx = pd.MultiIndex.from_product([["BRK.B"], ts], names=["symbol", "timestamp"])
    frame = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10, 20],
            "trade_count": [1, 1],
            "vwap": [1.0, 2.0],
        },
        index=idx,
    )

    class FakeClient:
        requests: list = []

        def get_stock_bars(self, req):
            self.requests.append(req)
            return SimpleNamespace(df=frame)

    client = FakeClient()
    out = AlpacaBarsProvider(client, feed="sip", adjustment="all").fetch(
        ["BRK.B"], date(2021, 12, 1), date(2021, 12, 31)
    )
    req = client.requests[0]
    assert req.feed == DataFeed.SIP and req.adjustment == Adjustment.ALL
    # The SDK stores end in UTC; it must still be the ceiling day in New York.
    end = pd.Timestamp(req.end)
    end = end.tz_localize("UTC") if end.tzinfo is None else end
    assert end.tz_convert("America/New_York").date() == date(2021, 12, 31)
    df = out["BRK.B"]
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert [d.date() for d in df.index] == [date(2021, 12, 30), date(2021, 12, 31)]


def test_alpaca_provider_requires_keys(monkeypatch):
    for k in ("ALPACA_API_KEY", "APCA_API_KEY_ID", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("QUANT_SKIP_DOTENV", "1")
    with pytest.raises(RuntimeError, match="Alpaca keys not set"):
        AlpacaBarsProvider().fetch(["SPY"], date(2021, 1, 1), date(2021, 1, 5))


def test_universes(tmp_path):
    (tmp_path / "lab.yaml").write_text("universe_file: u.yaml\n", encoding="utf-8")
    (tmp_path / "u.yaml").write_text(
        yaml.safe_dump(
            {"tickers": [{"symbol": "aaa", "sector": "X", "cap_bucket": "large"}, {"symbol": "IBM"}]}
        ),
        encoding="utf-8",
    )
    s = load_lab_settings(tmp_path)
    assert universe_symbols("broad", settings=s) == ["AAA", "IBM"]
    themes = universe_symbols("themes", settings=s)
    assert "IONQ" in themes and "KTOS" in themes  # active + bench
    assert universe_symbols("quantum", settings=s)[0] == "IONQ"
    combined = universe_symbols("all", settings=s)
    assert combined.count("IBM") == 1 and combined[:2] == ["AAA", "IBM"]
    with pytest.raises(ValueError):
        universe_symbols("crypto", settings=s)


def test_repo_broad_universe_file_is_well_formed():
    rows = yaml.safe_load(load_lab_settings().universe_path.read_text(encoding="utf-8"))["tickers"]
    assert 250 <= len(rows) <= 320
    assert len({r["symbol"] for r in rows}) == len(rows)
    assert {r["cap_bucket"] for r in rows} == {"large", "mid", "small"}
    assert all(r["sector"] for r in rows)


def test_cli_lab_data_parses():
    from quant_system.cli import build_parser

    args = build_parser().parse_args(["lab", "data", "sync", "--universe", "themes"])
    assert args.universe == "themes" and args.force is False
    assert build_parser().parse_args(["lab", "data", "status"]).lab_data_command == "status"
