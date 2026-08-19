"""Abstraktní rozhraní burzy. Implementace: CCXT (live/testnet) a paper broker."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Balance, OrderResult, Position, Side


class Exchange(ABC):
    """Minimální kontrakt, který router potřebuje k obchodování."""

    id: str = "abstract"

    @abstractmethod
    def load_markets(self) -> None: ...

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[list[float]]: ...

    @abstractmethod
    def fetch_ticker(self, symbol: str) -> dict[str, Any]: ...

    @abstractmethod
    def fetch_balance(self) -> Balance: ...

    @abstractmethod
    def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]: ...

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> None: ...

    @abstractmethod
    def create_market_order(
        self, symbol: str, side: Side, quantity: float, reduce_only: bool = False,
        params: dict[str, Any] | None = None,
    ) -> OrderResult: ...

    @abstractmethod
    def create_stop_loss(self, symbol: str, side: Side, quantity: float, stop_price: float) -> OrderResult: ...

    @abstractmethod
    def create_take_profit(self, symbol: str, side: Side, quantity: float, price: float) -> OrderResult: ...

    @abstractmethod
    def cancel_all_orders(self, symbol: str) -> None: ...

    @abstractmethod
    def close_position(self, symbol: str) -> OrderResult: ...

    # --- pomocné, s rozumným výchozím chováním ---

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        return price

    def market_limits(self, symbol: str) -> dict[str, float]:
        return {"min_amount": 0.0, "min_cost": 0.0, "max_leverage": 100.0, "contract_size": 1.0}

    def fetch_funding_rate(self, symbol: str) -> float:
        return 0.0

    def spread_bps(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask or bid <= 0:
            return 0.0
        return (ask - bid) / ((ask + bid) / 2.0) * 10_000.0
