"""Testy offline burzy a CLI příkazů, které nepotřebují síť."""
import pytest

from atb.config import ExchangeConfig
from atb.exchanges.offline import OfflineExchange, generate_ohlcv
from atb.main import main
from atb.models import Side


def test_generated_series_has_valid_candles():
    rows = generate_ohlcv(bars=200, start_price=50_000.0)
    assert len(rows) == 200
    for ts, open_, high, low, close, volume in rows:
        assert high >= max(open_, close)
        assert low <= min(open_, close)
        assert low > 0 and volume > 0
        assert ts > 0


def test_generated_series_is_deterministic():
    """Stejný seed = stejné ceny; čas svíček připínáme, ať test není flaky."""
    first = generate_ohlcv(bars=50, seed=1, end_ts=1_700_000_000)
    second = generate_ohlcv(bars=50, seed=1, end_ts=1_700_000_000)
    other = generate_ohlcv(bars=50, seed=2, end_ts=1_700_000_000)
    assert first == second
    assert first != other


def test_offline_exchange_is_stable_across_instances():
    """Stejná konfigurace musí dát stejná data i v novém procesu (crc32 seed)."""
    first = OfflineExchange(ExchangeConfig()).fetch_ohlcv("BTC/USDT:USDT", "15m", 100)
    second = OfflineExchange(ExchangeConfig()).fetch_ohlcv("BTC/USDT:USDT", "15m", 100)
    assert [row[4] for row in first] == [row[4] for row in second]


def test_different_symbols_get_different_series():
    exchange = OfflineExchange(ExchangeConfig())
    btc = exchange.fetch_ohlcv("BTC/USDT:USDT", "15m", 50)
    eth = exchange.fetch_ohlcv("ETH/USDT:USDT", "15m", 50)
    assert [r[4] for r in btc] != [r[4] for r in eth]


def test_higher_timeframe_series_is_separate():
    exchange = OfflineExchange(ExchangeConfig())
    fast = exchange.fetch_ohlcv("BTC/USDT:USDT", "15m", 50)
    slow = exchange.fetch_ohlcv("BTC/USDT:USDT", "1h", 50)
    assert [r[4] for r in fast] != [r[4] for r in slow]


def test_order_lifecycle_updates_equity():
    exchange = OfflineExchange(ExchangeConfig(), equity=10_000.0)
    opened = exchange.create_market_order("BTC/USDT:USDT", Side.LONG, 0.01)
    assert opened.ok
    assert len(exchange.fetch_positions()) == 1

    exchange.create_stop_loss("BTC/USDT:USDT", Side.LONG, 0.01, opened.filled_price * 0.98)
    assert exchange.fetch_positions()[0].stop_loss is not None

    exchange.close_position("BTC/USDT:USDT")
    assert exchange.fetch_positions() == []
    assert exchange.equity < 10_000.0        # zaplaceny poplatky obou stran


def test_offline_rejects_zero_quantity():
    exchange = OfflineExchange(ExchangeConfig())
    assert not exchange.create_market_order("BTC/USDT:USDT", Side.LONG, 0.0).ok


@pytest.mark.parametrize("side", ["long", "short"])
def test_demo_command_runs_end_to_end(side, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("ATB_DATABASE", ":memory:")
    exit_code = main(["--log-level", "ERROR", "demo", "--side", side])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "UKÁZKA" in output
    assert "Rozbor trhu" in output


def test_venues_command_lists_exchanges(capsys):
    assert main(["venues"]) == 0
    output = capsys.readouterr().out
    assert "bybit" in output
    assert "Max páka" in output
