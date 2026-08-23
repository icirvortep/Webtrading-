"""Testy automatického výběru trhů z celé nabídky burzy."""
import pytest

from atb.config import UniverseConfig
from atb.state.store import Store
from atb.trader import Trader
from atb.universe import UniverseSelector
from tests.conftest import FakeExchange


def build(exchange, **overrides) -> UniverseSelector:
    return UniverseSelector(UniverseConfig(**overrides), exchange)


@pytest.fixture()
def exchange_with_universe() -> FakeExchange:
    exchange = FakeExchange()
    exchange.universe = [f"{base}/USDT:USDT" for base in
                         ("BTC", "ETH", "SOL", "DOGE", "SHIB", "DEAD")]
    exchange.volumes = {
        "BTC/USDT:USDT": 900_000_000.0,
        "ETH/USDT:USDT": 500_000_000.0,
        "SOL/USDT:USDT": 200_000_000.0,
        "DOGE/USDT:USDT": 80_000_000.0,
        "SHIB/USDT:USDT": 60_000_000.0,
        "DEAD/USDT:USDT": 120_000.0,          # pod limitem likvidity
    }
    return exchange


def test_illiquid_markets_are_filtered_out(exchange_with_universe):
    selector = build(exchange_with_universe, min_volume_24h=50_000_000.0)
    symbols = [c.symbol for c in selector.refresh()]
    assert "DEAD/USDT:USDT" not in symbols
    assert "BTC/USDT:USDT" in symbols
    assert selector.filtered_out == 1


def test_ranking_is_sorted_and_bounded(exchange_with_universe):
    selector = build(exchange_with_universe)
    candidates = selector.refresh()
    scores = [c.rank_score for c in candidates]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_liquidity_weight_promotes_the_biggest_market(exchange_with_universe):
    selector = build(exchange_with_universe, weight_liquidity=1.0,
                     weight_volatility=0.0, weight_momentum=0.0)
    assert selector.refresh()[0].symbol == "BTC/USDT:USDT"


def test_excluded_patterns_are_skipped():
    exchange = FakeExchange()
    exchange.universe = ["BTC/USDT:USDT", "1000000PEPE/USDT:USDT"]
    selector = build(exchange, exclude_patterns=["1000000"], min_volume_24h=0.0)
    assert [c.symbol for c in selector.refresh()] == ["BTC/USDT:USDT"]


def test_wide_spread_disqualifies_market(exchange_with_universe, monkeypatch):
    def wide(symbols=None):
        return {"BTC/USDT:USDT": {"last": 100.0, "bid": 99.0, "ask": 101.0,
                                  "quoteVolume": 999_000_000.0, "high": 105, "low": 95}}
    monkeypatch.setattr(exchange_with_universe, "fetch_tickers", wide)
    selector = build(exchange_with_universe, max_spread_bps=5.0)
    assert selector.refresh() == []


def test_symbols_respects_deep_scan_count(exchange_with_universe):
    selector = build(exchange_with_universe, deep_scan_count=3)
    assert len(selector.symbols()) == 3


def test_refresh_is_cached_until_it_expires(exchange_with_universe):
    selector = build(exchange_with_universe, refresh_minutes=60.0)
    selector.symbols()
    assert not selector.needs_refresh()
    selector.last_refresh -= 3601
    assert selector.needs_refresh()


def test_exchange_failure_keeps_previous_ranking(exchange_with_universe, monkeypatch):
    selector = build(exchange_with_universe)
    before = [c.symbol for c in selector.refresh()]

    def boom(symbols=None):
        raise RuntimeError("burza neodpovídá")

    monkeypatch.setattr(exchange_with_universe, "fetch_tickers", boom)
    assert [c.symbol for c in selector.refresh()] == before   # starý seznam přežil
    assert "neodpovídá" in selector.last_error


def test_state_is_json_friendly(exchange_with_universe):
    import json
    selector = build(exchange_with_universe)
    selector.refresh()
    json.dumps(selector.state())


# ---------- napojení na skener ----------

@pytest.fixture()
def trader(config, exchange_with_universe) -> Trader:
    config.strategy.adaptive_learning = False
    config.scanner.enabled = False
    config.scanner.auto_universe = True
    config.universe.deep_scan_count = 4
    config.universe.batch_size = 2
    trader = Trader(config, exchange=exchange_with_universe, store=Store(":memory:"))
    yield trader
    trader.shutdown()


def test_scanner_uses_universe_instead_of_static_watchlist(trader):
    watchlist = trader.scanner.watchlist()
    assert len(watchlist) == 4
    assert "DEAD/USDT:USDT" not in watchlist


def test_batching_spreads_symbols_across_cycles(trader):
    """Jedno kolo zpracuje jen dávku, ale za pár kol projdou všechny."""
    first = trader.scanner.scan_once()
    assert len(first) == 2
    for _ in range(3):
        trader.scanner.scan_once()
    assert len(trader.scanner.snapshot()) == 4


def test_falls_back_to_watchlist_when_universe_empty(trader, monkeypatch):
    monkeypatch.setattr(trader.scanner.universe, "symbols", lambda limit=None: [])
    trader.cfg.scanner.watchlist = ["BTC/USDT:USDT"]
    assert trader.scanner.watchlist() == ["BTC/USDT:USDT"]


def test_opportunities_rank_tradeable_setups_first(trader):
    for _ in range(3):
        trader.scanner.scan_once()
    rows = trader.scanner.opportunities()
    assert rows
    tradeable = [r["tradeable"] for r in rows]
    assert tradeable == sorted(tradeable, reverse=True)
    for row in rows:
        assert row["side"] in ("long", "short")
        assert 0.0 <= row["score"] <= 1.0


def test_dropped_symbols_disappear_from_view(trader, monkeypatch):
    trader.scanner.scan_once()
    assert trader.scanner.snapshot()
    monkeypatch.setattr(trader.scanner, "watchlist", lambda: ["ETH/USDT:USDT"])
    trader.scanner.scan_once()
    assert set(trader.scanner.snapshot()) == {"ETH/USDT:USDT"}
