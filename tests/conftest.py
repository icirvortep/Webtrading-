"""Sdílené fixtures — hlavně falešná burza se syntetickými daty."""
from __future__ import annotations

import time
from typing import Any

import numpy as np
import pytest

from atb.config import AppConfig
from atb.exchanges.base import Exchange
from atb.models import Balance, OrderResult, Position, Side
from atb.state.store import Store


def synthetic_ohlcv(
    bars: int = 300, start: float = 100.0, drift: float = 0.0004,
    noise: float = 0.004, seed: int = 7,
) -> list[list[float]]:
    """Deterministická cenová řada (geometrický random walk s driftem)."""
    rng = np.random.default_rng(seed)
    returns = drift + rng.normal(0.0, noise, bars)
    rows: list[list[float]] = []
    price = start
    ts = int(time.time() * 1000) - bars * 900_000
    for i, ret in enumerate(returns):
        open_ = price
        price = max(price * (1.0 + float(ret)), 0.01)
        wick = abs(float(rng.normal(0.0, noise / 2)))
        high = max(open_, price) * (1 + wick)
        low = min(open_, price) * (1 - wick)
        volume = 1000.0 + abs(float(ret)) * 50_000
        rows.append([ts + i * 900_000, open_, high, low, price, volume])
    return rows


class FakeExchange(Exchange):
    """Deterministická burza pro testy — bez sítě."""

    def __init__(self, equity: float = 10_000.0, ohlcv: list[list[float]] | None = None) -> None:
        self.id = "fake"
        self.equity = equity
        self._ohlcv = ohlcv or synthetic_ohlcv()
        self.positions: dict[str, Position] = {}
        self.orders: list[dict[str, Any]] = []
        self.leverages: dict[str, int] = {}
        self.fail_stop_loss = False

    def load_markets(self) -> None:
        return None

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[list[float]]:
        return self._ohlcv[-limit:]

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        last = self._ohlcv[-1][4]
        return {"symbol": symbol, "last": last, "bid": last * 0.9999, "ask": last * 1.0001}

    def fetch_balance(self) -> Balance:
        return Balance(equity=self.equity, free=self.equity, currency="USDT")

    def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        return [p for p in self.positions.values() if not symbols or p.symbol in symbols]

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverages[symbol] = leverage

    def create_market_order(
        self, symbol: str, side: Side, quantity: float, reduce_only: bool = False,
        params: dict[str, Any] | None = None,
    ) -> OrderResult:
        price = float(self.fetch_ticker(symbol)["last"])
        self.orders.append({"type": "market", "symbol": symbol, "side": side, "qty": quantity})
        if reduce_only:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol] = Position(
                symbol=symbol, side=side, quantity=quantity, entry_price=price,
                leverage=self.leverages.get(symbol, 1),
            )
        return OrderResult(ok=True, order_id=f"fake-{len(self.orders)}",
                           filled_price=price, filled_qty=quantity)

    def create_stop_loss(self, symbol: str, side: Side, quantity: float, stop_price: float) -> OrderResult:
        if self.fail_stop_loss:
            return OrderResult(ok=False, error="burza odmítla SL")
        self.orders.append({"type": "sl", "symbol": symbol, "price": stop_price, "qty": quantity})
        return OrderResult(ok=True, order_id=f"fake-sl-{len(self.orders)}")

    def create_take_profit(self, symbol: str, side: Side, quantity: float, price: float) -> OrderResult:
        self.orders.append({"type": "tp", "symbol": symbol, "price": price, "qty": quantity})
        return OrderResult(ok=True, order_id=f"fake-tp-{len(self.orders)}")

    def cancel_all_orders(self, symbol: str) -> None:
        self.orders = [o for o in self.orders if o["symbol"] != symbol]

    def close_position(self, symbol: str) -> OrderResult:
        position = self.positions.get(symbol)
        if not position:
            return OrderResult(ok=True, error="žádná pozice")
        return self.create_market_order(symbol, position.side.opposite, position.quantity, reduce_only=True)

    def market_limits(self, symbol: str) -> dict[str, float]:
        return {"min_amount": 0.001, "min_cost": 5.0, "max_leverage": 100.0, "contract_size": 1.0}


@pytest.fixture()
def config() -> AppConfig:
    return AppConfig.model_validate({
        "mode": "paper",
        "dry_run": False,
        "database": ":memory:",
        "monitor": {"enabled": False},
        "webhook": {"enforce_ip_allowlist": False, "require_hmac": False},
    })


@pytest.fixture()
def store() -> Store:
    store = Store(":memory:")
    yield store
    store.close()


@pytest.fixture()
def exchange() -> FakeExchange:
    return FakeExchange()
