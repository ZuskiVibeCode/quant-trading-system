import os
import pytest

@pytest.fixture(autouse=True)
def _isolate_alpaca_env(monkeypatch):
    """Unit tests expect dry-run unless explicitly testing Alpaca."""
    if os.environ.get("QUANT_ALLOW_ALPACA_IN_TESTS") == "1":
        return
    for k in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
