"""Testy paper brokera proti stubu tržních dat (bez sítě)."""
import pytest

from atb.config import ExchangeConfig
from atb.exchanges.paper import PaperExchange
from atb.models import Side
from tests.conftest import synthetic_ohlcv


class StubPublicClient:
    def __init__(self, price: float = 100.0) -> None:
        self.price = price
        self._ohlcv = synthetic_ohlcv(200)

    def load_markets(self):
        return {}

    def fetch_ohlcv(self, symbol, timeframe, limit=300):
        return self._ohlcv[-limit:]

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": self.price,
                "bid": self.price * 0.9999, "ask": self.price * 1.0001}

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0},
                           "leverage": {"max": 50}}, "contractSize": 1.0}

    def amount_to_precision(self, symbol, amount):
        return round(amount, 6)

    def price_to_precision(self, symbol, price):
        return round(price, 4)


@pytest.fixture()
def broker() -> PaperExchange:
    return PaperExchange(ExchangeConfig(), starting_equity=10_000.0,
                         public_client=StubPublicClient())


def test_starting_equity(broker):
    assert broker.fetch_balance().equity == pytest.approx(10_000.0, rel=1e-3)


def test_market_order_opens_position_and_charges_fee(broker):
    result = broker.create_market_order("BTC/USDT:USDT", Side.LONG, 1.0)
    assert result.ok
    assert broker.fetch_positions()[0].quantity == 1.0
    assert broker.equity < 10_000.0                 # zaplacen poplatek


def test_slippage_worsens_fill_for_both_sides(broker):
    long_fill = broker.create_market_order("A/USDT:USDT", Side.LONG, 1.0).filled_price
    short_fill = broker.create_market_order("B/USDT:USDT", Side.SHORT, 1.0).filled_price
    assert long_fill > 100.0                        # long kupuje dráž
    assert short_fill < 100.0                       # short prodává levněji


def test_profitable_close_increases_equity(broker):
    broker.create_market_order("BTC/USDT:USDT", Side.LONG, 1.0)
    broker._public.price = 110.0
    broker._price_cache.clear()
    broker.close_position("BTC/USDT:USDT")
    assert broker.fetch_positions() == []
    assert broker.fetch_balance().equity > 10_000.0


def test_losing_close_decreases_equity(broker):
    broker.create_market_order("BTC/USDT:USDT", Side.LONG, 1.0)
    broker._public.price = 90.0
    broker._price_cache.clear()
    broker.close_position("BTC/USDT:USDT")
    assert broker.fetch_balance().equity < 10_000.0


def test_averaging_into_position_updates_entry(broker):
    broker.create_market_order("BTC/USDT:USDT", Side.LONG, 1.0)
    broker._public.price = 120.0
    broker._price_cache.clear()
    broker.create_market_order("BTC/USDT:USDT", Side.LONG, 1.0)
    position = broker.fetch_positions()[0]
    assert position.quantity == 2.0
    assert 100.0 < position.entry_price < 120.0


def test_unrealized_pnl_tracks_price(broker):
    broker.create_market_order("BTC/USDT:USDT", Side.LONG, 2.0)
    broker._public.price = 105.0
    broker._price_cache.clear()
    # vstup je o slippage horší než 100, takže zisk je mírně pod 10
    assert broker.fetch_positions()[0].unrealized_pnl == pytest.approx(10.0, rel=0.01)


def test_zero_quantity_is_rejected(broker):
    assert not broker.create_market_order("BTC/USDT:USDT", Side.LONG, 0.0).ok


def test_market_limits_come_from_public_client(broker):
    limits = broker.market_limits("BTC/USDT:USDT")
    assert limits["min_amount"] == 0.001
    assert limits["max_leverage"] == 50


def test_closing_without_position_is_noop(broker):
    result = broker.close_position("BTC/USDT:USDT")
    assert result.ok
