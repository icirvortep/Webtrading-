"""Testy CCXT adaptéru proti falešnému klientovi — ověřují, co se posílá burze."""
import pytest

from atb.config import ExchangeConfig
from atb.models import Side


class FakeCCXTClient:
    """Zaznamená volání místo odeslání na burzu."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.has = {"sandbox": True, "spot": True, "swap": True, "margin": True,
                    "setMarginMode": False, "fetchFundingRate": False}
        self.markets = {}
        self.last_price = 65_000.0

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.calls.append({"symbol": symbol, "type": order_type, "side": side,
                           "amount": amount, "price": price, "params": params or {}})
        return {"id": "1", "average": price or self.last_price, "filled": amount}

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": self.last_price,
                "bid": self.last_price * 0.9999, "ask": self.last_price * 1.0001}

    def load_markets(self):
        return {}

    def set_sandbox_mode(self, enabled):
        pass


def build(monkeypatch, account_type: str):
    """Adaptér s podvrženým klientem — bez sítě a bez klíčů."""
    from atb.exchanges import ccxt_adapter

    monkeypatch.setenv("EXCHANGE_API_KEY", "k")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s")
    cfg = ExchangeConfig(id="bybit", account_type=account_type, testnet=False)
    exchange = ccxt_adapter.CCXTExchange.__new__(ccxt_adapter.CCXTExchange)
    exchange.cfg = cfg
    exchange.id = cfg.id
    exchange.client = FakeCCXTClient()
    exchange._markets = {}
    exchange.tracks_positions = not cfg.is_spot
    exchange.can_short = cfg.can_short
    return exchange


def test_spot_market_buy_sends_price_so_cost_is_unambiguous(monkeypatch):
    """Bez ceny by burza vzala množství mincí jako částku v USDT."""
    exchange = build(monkeypatch, "spot")
    result = exchange.create_market_order("BTC/USDT", Side.LONG, 0.01)
    assert result.ok
    call = exchange.client.calls[0]
    assert call["price"] == pytest.approx(65_000.0)
    assert call["amount"] == pytest.approx(0.01)


def test_spot_market_sell_needs_no_price(monkeypatch):
    """Prodej se zadává v mincích, cenu doplňovat netřeba."""
    exchange = build(monkeypatch, "spot")
    exchange.create_market_order("BTC/USDT", Side.SHORT, 0.01)
    assert exchange.client.calls[0]["price"] is None


def test_perpetual_orders_never_send_a_price(monkeypatch):
    """U perpetuálů je množství vždy v kontraktech — cena by mátla."""
    exchange = build(monkeypatch, "swap")
    exchange.create_market_order("BTC/USDT:USDT", Side.LONG, 0.01)
    assert exchange.client.calls[0]["price"] is None


def test_buy_is_not_sent_when_price_is_unavailable(monkeypatch):
    """Raději neobchodovat než poslat příkaz s nejednoznačným množstvím."""
    exchange = build(monkeypatch, "spot")
    exchange.client.last_price = 0.0
    result = exchange.create_market_order("BTC/USDT", Side.LONG, 0.01)
    assert not result.ok
    assert "cena" in result.error.lower()
    assert exchange.client.calls == []


def test_reduce_only_flag_is_passed_for_perpetuals(monkeypatch):
    exchange = build(monkeypatch, "swap")
    exchange.create_market_order("BTC/USDT:USDT", Side.SHORT, 0.01, reduce_only=True)
    assert exchange.client.calls[0]["params"]["reduceOnly"] is True


def test_spot_rejects_exchange_stop_orders(monkeypatch):
    exchange = build(monkeypatch, "spot")
    result = exchange.create_stop_loss("BTC/USDT", Side.LONG, 0.01, 60_000.0)
    assert not result.ok
    assert exchange.client.calls == []


def test_spot_set_leverage_is_a_noop(monkeypatch):
    exchange = build(monkeypatch, "spot")
    exchange.set_leverage("BTC/USDT", 10)          # nesmí spadnout ani nic poslat
    assert exchange.client.calls == []
