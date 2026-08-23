"""Paper broker — plnohodnotná simulace obchodování bez reálných peněz.

Bere reálná tržní data (přes CCXT bez klíčů, tedy jen veřejné endpointy),
ale objednávky plní lokálně s modelem slippage a poplatků. Slouží pro
vývoj, testy a jako výchozí režim bota, aby nikdy nešlo omylem obchodovat živě.
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from ..config import ExchangeConfig
from ..models import Balance, OrderResult, Position, Side, TakeProfit
from .base import Exchange

log = logging.getLogger(__name__)


class PaperExchange(Exchange):
    def __init__(
        self,
        cfg: ExchangeConfig,
        starting_equity: float = 10_000.0,
        taker_fee_bps: float = 5.5,
        slippage_bps: float = 2.0,
        public_client: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.id = f"paper:{cfg.id}"
        self.equity = starting_equity
        self.taker_fee_bps = taker_fee_bps
        self.slippage_bps = slippage_bps
        self.positions: dict[str, Position] = {}
        self.orders: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self._public = public_client if public_client is not None else _public_ccxt(cfg)
        self._price_cache: dict[str, tuple[float, float]] = {}

    # ---------- market data (reálná, veřejná) ----------

    def load_markets(self) -> None:
        if self._public is not None:
            self._public.load_markets()

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[list[float]]:
        if self._public is None:
            raise RuntimeError("Paper broker nemá zdroj dat (chybí ccxt)")
        return self._public.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        if self._public is None:
            raise RuntimeError("Paper broker nemá zdroj dat (chybí ccxt)")
        cached = self._price_cache.get(symbol)
        now = time.time()
        if cached and now - cached[0] < 1.0:
            price = cached[1]
            return {"symbol": symbol, "last": price, "bid": price, "ask": price}
        ticker = self._public.fetch_ticker(symbol)
        self._price_cache[symbol] = (now, float(ticker.get("last") or 0.0))
        return ticker

    def list_symbols(self, quote: str = "USDT") -> list[str]:
        if self._public is None:
            return []
        if not self._public.markets:
            self._public.load_markets()
        return [
            symbol for symbol, market in self._public.markets.items()
            if market.get("active", True) and market.get("swap")
            and market.get("quote") == quote and market.get("settle") == quote
            and not market.get("inverse")
        ]

    def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        if self._public is None:
            return {}
        return self._public.fetch_tickers(symbols)

    def mark_price(self, symbol: str) -> float:
        return float(self.fetch_ticker(symbol).get("last") or 0.0)

    # ---------- účet ----------

    def fetch_balance(self) -> Balance:
        unrealized = sum(self._pnl(p) for p in self.positions.values())
        equity = self.equity + unrealized
        used = sum(p.quantity * p.entry_price / max(p.leverage, 1) for p in self.positions.values())
        return Balance(equity=equity, free=max(equity - used, 0.0), currency=self.cfg.quote)

    def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        out = []
        for pos in self.positions.values():
            if symbols and pos.symbol not in symbols:
                continue
            pos.unrealized_pnl = self._pnl(pos)
            out.append(pos)
        return out

    def set_leverage(self, symbol: str, leverage: int) -> None:
        pos = self.positions.get(symbol)
        if pos:
            pos.leverage = leverage

    # ---------- objednávky ----------

    def create_market_order(
        self, symbol: str, side: Side, quantity: float, reduce_only: bool = False,
        params: dict[str, Any] | None = None,
    ) -> OrderResult:
        if quantity <= 0:
            return OrderResult(ok=False, error="nulové množství")
        price = self.mark_price(symbol)
        if price <= 0:
            return OrderResult(ok=False, error="neznámá cena")
        fill = price * (1 + side.sign * self.slippage_bps / 10_000.0)
        fee = fill * quantity * self.taker_fee_bps / 10_000.0
        self.equity -= fee

        existing = self.positions.get(symbol)
        if reduce_only or (existing and existing.side is not side):
            realized = self._reduce(symbol, quantity, fill)
            self.trades.append({
                "ts": time.time(), "symbol": symbol, "action": "reduce",
                "qty": quantity, "price": fill, "fee": fee, "pnl": realized,
            })
        else:
            if existing:
                total = existing.quantity + quantity
                existing.entry_price = (
                    existing.entry_price * existing.quantity + fill * quantity
                ) / total
                existing.quantity = total
            else:
                self.positions[symbol] = Position(
                    symbol=symbol, side=side, quantity=quantity, entry_price=fill,
                    leverage=int((params or {}).get("leverage", 1)),
                )
            self.trades.append({
                "ts": time.time(), "symbol": symbol, "action": "open",
                "qty": quantity, "price": fill, "fee": fee, "pnl": 0.0,
            })

        order_id = f"paper-{len(self.trades)}"
        log.info("[PAPER] %s %s %.8f @ %.6f (fee %.4f)", symbol, side.value, quantity, fill, fee)
        return OrderResult(ok=True, order_id=order_id, filled_price=fill, filled_qty=quantity)

    def create_stop_loss(self, symbol: str, side: Side, quantity: float, stop_price: float) -> OrderResult:
        pos = self.positions.get(symbol)
        if pos:
            pos.stop_loss = stop_price
        self.orders.append({"symbol": symbol, "type": "sl", "price": stop_price, "qty": quantity})
        return OrderResult(ok=True, order_id=f"paper-sl-{len(self.orders)}")

    def create_take_profit(self, symbol: str, side: Side, quantity: float, price: float) -> OrderResult:
        pos = self.positions.get(symbol)
        if pos:
            pos.take_profits.append(TakeProfit(price=price, fraction=0.0, r_multiple=0.0))
        self.orders.append({"symbol": symbol, "type": "tp", "price": price, "qty": quantity})
        return OrderResult(ok=True, order_id=f"paper-tp-{len(self.orders)}")

    def cancel_all_orders(self, symbol: str) -> None:
        self.orders = [o for o in self.orders if o["symbol"] != symbol]

    def close_position(self, symbol: str) -> OrderResult:
        pos = self.positions.get(symbol)
        if not pos:
            return OrderResult(ok=True, error="žádná otevřená pozice")
        self.cancel_all_orders(symbol)
        return self.create_market_order(symbol, pos.side.opposite, pos.quantity, reduce_only=True)

    def market_limits(self, symbol: str) -> dict[str, float]:
        if self._public is None:
            return super().market_limits(symbol)
        try:
            market = self._public.market(symbol)
            limits = market.get("limits", {})
            return {
                "min_amount": float((limits.get("amount", {}) or {}).get("min") or 0.0),
                "min_cost": float((limits.get("cost", {}) or {}).get("min") or 0.0),
                "max_leverage": float((limits.get("leverage", {}) or {}).get("max") or 100.0),
                "contract_size": float(market.get("contractSize") or 1.0),
            }
        except Exception:
            return super().market_limits(symbol)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        if self._public is None:
            return amount
        try:
            return float(self._public.amount_to_precision(symbol, amount))
        except Exception:
            return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        if self._public is None:
            return price
        try:
            return float(self._public.price_to_precision(symbol, price))
        except Exception:
            return price

    # ---------- interní ----------

    def _pnl(self, pos: Position) -> float:
        price = self.mark_price(pos.symbol)
        return (price - pos.entry_price) * pos.quantity * pos.side.sign

    def _reduce(self, symbol: str, quantity: float, price: float) -> float:
        pos = self.positions.get(symbol)
        if not pos:
            return 0.0
        qty = min(quantity, pos.quantity)
        realized = (price - pos.entry_price) * qty * pos.side.sign
        self.equity += realized
        pos.quantity -= qty
        if pos.quantity <= 1e-12:
            del self.positions[symbol]
        return realized


def _public_ccxt(cfg: ExchangeConfig):
    """Veřejný (nepřihlášený) CCXT klient pro tržní data."""
    try:
        import ccxt
    except ImportError:  # pragma: no cover
        log.warning("ccxt není nainstalováno — paper broker běží bez tržních dat")
        return None
    if not hasattr(ccxt, cfg.id):
        log.warning("CCXT nezná burzu %s — paper broker bez dat", cfg.id)
        return None
    client = getattr(ccxt, cfg.id)({
        "enableRateLimit": True,
        "options": {"defaultType": cfg.account_type},
    })
    if cfg.testnet:
        with contextlib.suppress(Exception):
            client.set_sandbox_mode(True)
    return client
