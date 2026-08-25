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


def test_preflight_stops_when_keys_are_missing(capsys, monkeypatch, clean_env):
    """Bez klíčů musí skončit hned a říct, který řádek v .env chybí."""
    monkeypatch.delenv("EXCHANGE_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_API_SECRET", raising=False)
    assert main(["preflight"]) == 1
    output = capsys.readouterr().out
    assert "EXCHANGE_API_KEY" in output
    assert "✗" in output


def test_preflight_reports_unsupported_market_type(capsys, monkeypatch, clean_env, tmp_path):
    """Bybit EU + perpetuály musí padnout na kontrole, ne až u obchodu."""
    monkeypatch.setenv("EXCHANGE_API_KEY", "k")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "exchange:\n  id: bybiteu\n  account_type: swap\n  testnet: false\n",
        encoding="utf-8",
    )
    assert main(["--config", str(cfg), "preflight"]) == 1
    assert "nenabízí typ trhu" in capsys.readouterr().out


def test_preflight_lists_market_counts_per_quote(monkeypatch, capsys, clean_env, tmp_path):
    """Kolik trhů má která měna — podklad pro rozhodnutí, v čem držet peníze."""
    from atb.exchanges import ccxt_adapter

    monkeypatch.setenv("EXCHANGE_API_KEY", "k")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s")

    class Stub:
        def __init__(self, cfg):
            self.cfg = cfg
            self.id = cfg.id
            self.tracks_positions = False

        def load_markets(self):
            return None

        def list_symbols(self, quote="USDT"):
            return {"USDT": [f"C{i}/USDT" for i in range(700)],
                    "USDC": [f"C{i}/USDC" for i in range(12)]}.get(quote, [])

        def fetch_balance(self):
            from atb.models import Balance
            return Balance(equity=114.0, free=114.0, currency="USDC")

        def market_limits(self, symbol):
            return {"min_cost": 1.0, "min_amount": 0.0, "max_leverage": 1.0,
                    "contract_size": 1.0}

        def fetch_ticker(self, symbol):
            return {"last": 100.0}

    monkeypatch.setattr(ccxt_adapter, "CCXTExchange", Stub)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "exchange:\n  id: bybiteu\n  account_type: spot\n  quote: USDC\n  testnet: false\n",
        encoding="utf-8",
    )
    main(["--config", str(cfg), "preflight"])
    output = capsys.readouterr().out
    assert "700 trhů" in output
    assert "12 trhů" in output
    assert "tvoje měna" in output
