"""Offline burza pro ukázku a ladění — generuje vlastní tržní data.

Nepotřebuje API klíče ani připojení k internetu. Slouží k tomu, aby si šlo
celý řetězec (signál → režim → skóre → plán → objednávka → trailing) projít
a odladit dřív, než se bot vůbec připojí k reálné burze.
"""
from __future__ import annotations

import logging
import time
import zlib
from typing import Any, ClassVar

import numpy as np

from ..config import ExchangeConfig
from ..models import Balance, OrderResult, Position, Side
from .base import Exchange

log = logging.getLogger(__name__)


def generate_ohlcv(
    bars: int = 400,
    start_price: float = 65_000.0,
    drift: float = 0.0006,
    volatility: float = 0.004,
    timeframe_minutes: int = 15,
    seed: int = 42,
    end_ts: float | None = None,
) -> list[list[float]]:
    """Geometrický random walk s driftem — deterministický podle seedu.

    `end_ts` umožňuje připnout čas poslední svíčky; bez něj se bere aktuální
    čas, takže dvě volání se liší jen v časových razítkách, ne v cenách.
    """
    rng = np.random.default_rng(seed)
    returns = drift + rng.normal(0.0, volatility, bars)
    rows: list[list[float]] = []
    price = start_price
    step_ms = timeframe_minutes * 60_000
    ts = int((end_ts if end_ts is not None else time.time()) * 1000) - bars * step_ms
    for i, ret in enumerate(returns):
        open_ = price
        price = max(price * (1.0 + float(ret)), 0.01)
        wick = abs(float(rng.normal(0.0, volatility / 2)))
        rows.append([
            ts + i * step_ms, open_,
            max(open_, price) * (1 + wick), min(open_, price) * (1 - wick),
            price, 1000.0 + abs(float(ret)) * 50_000,
        ])
    return rows


class OfflineExchange(Exchange):
    """Simulovaná burza se syntetickými daty a lokálním plněním příkazů."""

    def __init__(
        self, cfg: ExchangeConfig, equity: float = 10_000.0,
        drift: float = 0.0006, volatility: float = 0.004, seed: int = 42,
        taker_fee_bps: float = 5.5, slippage_bps: float = 2.0,
    ) -> None:
        self.cfg = cfg
        self.id = "offline"
        self.equity = equity
        self.taker_fee_bps = taker_fee_bps
        self.slippage_bps = slippage_bps
        self.positions: dict[str, Position] = {}
        self.orders: list[dict[str, Any]] = []
        self._series: dict[str, list[list[float]]] = {}
        self._params = {"drift": drift, "volatility": volatility, "seed": seed}
        self.can_short = cfg.can_short

    def _ohlcv_for(self, symbol: str, timeframe: str) -> list[list[float]]:
        key = f"{symbol}|{timeframe}"
        if key not in self._series:
            from ..strategy.engine import timeframe_minutes

            minutes = timeframe_minutes(timeframe)
            # každý symbol i timeframe má vlastní, ale napříč běhy stabilní
            # realizaci — vestavěný hash() je per-proces náhodný, proto crc32
            seed = self._params["seed"] + zlib.crc32(key.encode()) % 1000
            self._series[key] = generate_ohlcv(
                bars=400, drift=self._params["drift"] * (minutes / 15),
                volatility=self._params["volatility"] * (minutes / 15) ** 0.5,
                timeframe_minutes=minutes, seed=seed,
            )
        return self._series[key]

    # ---------- data ----------

    def load_markets(self) -> None:
        log.info("OFFLINE režim — data se generují lokálně, žádné připojení k burze")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[list[float]]:
        return self._ohlcv_for(symbol, timeframe)[-limit:]

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        price = self._ohlcv_for(symbol, "15m")[-1][4]
        return {"symbol": symbol, "last": price, "bid": price * 0.99995, "ask": price * 1.00005}

    #: simulovaná nabídka burzy — ať jde vyzkoušet i automatický výběr trhů
    SIMULATED_UNIVERSE: ClassVar[list[str]] = [
        "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON", "DOT",
        "MATIC", "NEAR", "APT", "ARB", "OP", "SUI", "INJ", "SEI", "TIA", "PEPE",
        "WIF", "BONK", "LTC", "BCH", "ATOM", "FIL", "RNDR", "AAVE", "UNI", "ETC",
    ]

    def list_symbols(self, quote: str = "USDT") -> list[str]:
        if self.cfg.is_spot:
            return [f"{base}/{quote}" for base in self.SIMULATED_UNIVERSE]
        return [f"{base}/{quote}:{quote}" for base in self.SIMULATED_UNIVERSE]

    def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Hromadný přehled — deterministicky odvozený z vygenerovaných řad."""
        wanted = symbols or self.list_symbols(self.cfg.quote)
        out: dict[str, Any] = {}
        for symbol in wanted:
            series = self._ohlcv_for(symbol, "15m")
            day = series[-96:] if len(series) >= 96 else series
            price = day[-1][4]
            high = max(row[2] for row in day)
            low = min(row[3] for row in day)
            first = day[0][1]
            # objem odvozený od symbolu, aby žebříček nebyl u všech stejný
            volume = (zlib.crc32(symbol.encode()) % 900 + 100) * 1_000_000.0
            out[symbol] = {
                "symbol": symbol, "last": price, "close": price,
                "bid": price * 0.99995, "ask": price * 1.00005,
                "high": high, "low": low, "quoteVolume": volume,
                "percentage": (price - first) / first * 100 if first else 0.0,
            }
        return out

    def fetch_balance(self) -> Balance:
        unrealized = sum(
            (self.fetch_ticker(p.symbol)["last"] - p.entry_price) * p.quantity * p.side.sign
            for p in self.positions.values()
        )
        equity = self.equity + unrealized
        return Balance(equity=equity, free=equity, currency=self.cfg.quote)

    def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        return [p for p in self.positions.values() if not symbols or p.symbol in symbols]

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.orders.append({"type": "leverage", "symbol": symbol, "value": leverage})

    # ---------- objednávky ----------

    def create_market_order(
        self, symbol: str, side: Side, quantity: float, reduce_only: bool = False,
        params: dict[str, Any] | None = None,
    ) -> OrderResult:
        if quantity <= 0:
            return OrderResult(ok=False, error="nulové množství")
        price = float(self.fetch_ticker(symbol)["last"])
        fill = price * (1 + side.sign * self.slippage_bps / 10_000.0)
        self.equity -= fill * quantity * self.taker_fee_bps / 10_000.0
        self.orders.append({"type": "market", "symbol": symbol, "side": side.value,
                            "qty": quantity, "price": fill, "reduce_only": reduce_only})
        if reduce_only:
            position = self.positions.pop(symbol, None)
            if position:
                self.equity += (fill - position.entry_price) * position.quantity * position.side.sign
        else:
            self.positions[symbol] = Position(
                symbol=symbol, side=side, quantity=quantity, entry_price=fill,
                leverage=int((params or {}).get("leverage", 1)),
            )
        return OrderResult(ok=True, order_id=f"off-{len(self.orders)}",
                           filled_price=fill, filled_qty=quantity)

    def create_stop_loss(self, symbol: str, side: Side, quantity: float, stop_price: float) -> OrderResult:
        position = self.positions.get(symbol)
        if position:
            position.stop_loss = stop_price
        self.orders.append({"type": "sl", "symbol": symbol, "price": stop_price, "qty": quantity})
        return OrderResult(ok=True, order_id=f"off-sl-{len(self.orders)}")

    def create_take_profit(self, symbol: str, side: Side, quantity: float, price: float) -> OrderResult:
        self.orders.append({"type": "tp", "symbol": symbol, "price": price, "qty": quantity})
        return OrderResult(ok=True, order_id=f"off-tp-{len(self.orders)}")

    def cancel_all_orders(self, symbol: str) -> None:
        self.orders = [o for o in self.orders if o.get("symbol") != symbol]

    def close_position(self, symbol: str) -> OrderResult:
        position = self.positions.get(symbol)
        if not position:
            return OrderResult(ok=True, error="žádná otevřená pozice")
        self.cancel_all_orders(symbol)
        return self.create_market_order(symbol, position.side.opposite, position.quantity, reduce_only=True)

    def market_limits(self, symbol: str) -> dict[str, float]:
        return {"min_amount": 0.001, "min_cost": 5.0, "max_leverage": 100.0, "contract_size": 1.0}

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return round(amount, 6)

    def price_to_precision(self, symbol: str, price: float) -> float:
        return round(price, 2)
