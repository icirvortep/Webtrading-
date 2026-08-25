"""Adaptér nad knihovnou CCXT — jednotný přístup k desítkám burz.

Rozdíly mezi burzami (jména parametrů pro SL/TP, hedge mód, testnet) řeší
tenhle modul, takže zbytek bota o konkrétní burze nic neví.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import ExchangeConfig
from ..models import Balance, OrderResult, Position, Side
from . import registry
from .base import Exchange

log = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    pass


class CCXTExchange(Exchange):
    def __init__(self, cfg: ExchangeConfig) -> None:
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover
            raise ExchangeError("Chybí knihovna ccxt: pip install ccxt") from exc

        self.cfg = cfg
        self.id = cfg.id
        if not hasattr(ccxt, cfg.id):
            raise ExchangeError(f"CCXT nezná burzu '{cfg.id}'")

        creds = cfg.credentials()
        if not creds.get("apiKey") or not creds.get("secret"):
            raise ExchangeError(
                f"Chybí API klíče — nastav {cfg.api_key_env} a {cfg.api_secret_env}"
            )

        self.client = getattr(ccxt, cfg.id)({
            **creds,
            "enableRateLimit": True,
            "options": {
                "defaultType": cfg.account_type,
                "recvWindow": cfg.recv_window_ms,
                "adjustForTimeDifference": True,
            },
        })
        self._check_market_type_supported()

        if cfg.testnet:
            if not self.client.has.get("sandbox", True):
                log.warning("Burza %s testnet oficiálně nepodporuje", cfg.id)
            self.client.set_sandbox_mode(True)
        self._markets: dict[str, Any] = {}
        self.tracks_positions = not cfg.is_spot
        self.can_short = cfg.can_short

    def _check_market_type_supported(self) -> None:
        """Ověří, že burza vůbec nabízí požadovaný typ trhu.

        Bez téhle kontroly by se nesoulad projevil až nesrozumitelnou chybou
        při prvním obchodu — třeba Bybit EU nabízí jen spot a margin, žádné
        perpetual kontrakty, na kterých je bot postavený.
        """
        wanted = self.cfg.account_type
        if self.client.has.get(wanted):
            return
        available = [name for name in ("swap", "future", "spot", "margin")
                     if self.client.has.get(name)]
        venue = registry.get(self.cfg.id)
        detail = f" {venue.notes}" if venue and venue.notes else ""
        raise ExchangeError(
            f"Burza {self.cfg.id} nenabízí typ trhu '{wanted}'."
            f" Dostupné: {', '.join(available) or 'žádné'}.{detail}"
        )

    # ---------- market data ----------

    def load_markets(self) -> None:
        self._markets = self.client.load_markets()
        log.info("Načteno %d trhů z %s", len(self._markets), self.id)

    def _market(self, symbol: str) -> dict[str, Any]:
        if not self._markets:
            self.load_markets()
        market = self._markets.get(symbol)
        if market is None:
            raise ExchangeError(f"Trh {symbol} na burze {self.id} neexistuje")
        return market

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[list[float]]:
        return self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return self.client.fetch_ticker(symbol)

    def list_symbols(self, quote: str = "USDT") -> list[str]:
        """Aktivní trhy odpovídající nastavenému typu účtu."""
        if not self._markets:
            self.load_markets()
        spot = self.cfg.is_spot
        out = []
        for symbol, market in self._markets.items():
            if not market.get("active", True) or market.get("option"):
                continue
            if market.get("quote") != quote:
                continue
            if spot:
                if not market.get("spot"):
                    continue
            else:
                # perpetual kontrakty vypořádané v kotační měně
                if not market.get("swap") or market.get("inverse"):
                    continue
                if market.get("settle") != quote:
                    continue
            out.append(symbol)
        return out

    def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Jeden dotaz na celý trh — Bybit vrací všechny tickery najednou."""
        try:
            return self.client.fetch_tickers(symbols)
        except Exception as exc:
            log.warning("fetch_tickers selhalo: %s", exc)
            raise

    def fetch_funding_rate(self, symbol: str) -> float:
        if not self.client.has.get("fetchFundingRate"):
            return 0.0
        try:
            return float(self.client.fetch_funding_rate(symbol).get("fundingRate") or 0.0)
        except Exception as exc:
            log.debug("fetch_funding_rate(%s) selhalo: %s", symbol, exc)
            return 0.0

    # ---------- účet ----------

    def fetch_balance(self) -> Balance:
        raw = self.client.fetch_balance()
        quote = self.cfg.quote
        total = raw.get("total", {}).get(quote)
        free = raw.get("free", {}).get(quote)
        if total is None:
            info = raw.get("info", {})
            total = _first_number(info, ("totalEquity", "equity", "totalWalletBalance", "usdtEquity"))
            free = _first_number(info, ("availableBalance", "totalAvailableBalance", "free")) or total
        if total is None:
            raise ExchangeError(f"Nepodařilo se zjistit equity v {quote}")
        return Balance(equity=float(total), free=float(free or total), currency=quote)

    def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        if not self.tracks_positions:
            # Spot pozice nemá — otevřené obchody si vede bot v databázi.
            return []
        raw = self.client.fetch_positions(symbols)
        out: list[Position] = []
        for item in raw:
            contracts = float(item.get("contracts") or item.get("contractSize") or 0.0)
            if contracts <= 0:
                continue
            side = Side.LONG if str(item.get("side", "")).lower() == "long" else Side.SHORT
            out.append(Position(
                symbol=item["symbol"], side=side, quantity=contracts,
                entry_price=float(item.get("entryPrice") or 0.0),
                leverage=int(float(item.get("leverage") or 1)),
                unrealized_pnl=float(item.get("unrealizedPnl") or 0.0),
                liquidation_price=_opt_float(item.get("liquidationPrice")),
                meta={"raw": item.get("info", {})},
            ))
        return out

    def set_leverage(self, symbol: str, leverage: int) -> None:
        if self.cfg.is_spot:
            return                      # spotový účet páku nezná
        market = self._market(symbol)
        try:
            if self.client.has.get("setMarginMode"):
                self.client.set_margin_mode(self.cfg.margin_mode, symbol, {"leverage": leverage})
        except Exception as exc:
            log.debug("set_margin_mode(%s): %s", symbol, exc)
        try:
            self.client.set_leverage(leverage, market["symbol"])
        except Exception as exc:
            log.warning("Nastavení páky %sx pro %s selhalo: %s", leverage, symbol, exc)

    # ---------- objednávky ----------

    def create_market_order(
        self, symbol: str, side: Side, quantity: float, reduce_only: bool = False,
        params: dict[str, Any] | None = None,
    ) -> OrderResult:
        order_side = "buy" if side is Side.LONG else "sell"
        req: dict[str, Any] = dict(params or {})
        if reduce_only:
            req["reduceOnly"] = True
        if self.cfg.hedge_mode:
            req.setdefault("positionIdx", 1 if side is Side.LONG else 2)

        # U spotového tržního NÁKUPU očekává burza množství v kotační měně
        # (kolik utratit), ne v mincích. Bez ceny by se naše základní množství
        # tiše vzalo jako částka v USDT — "kup 250 DOGE" by znamenalo
        # "utrať 250 USDT". Cenu proto předáváme vždy, ať je převod jednoznačný.
        price = self._market_buy_price(symbol) if self._needs_buy_price(side, reduce_only) else None

        if self._needs_buy_price(side, reduce_only) and price is None:
            return OrderResult(ok=False, error=f"neznámá cena pro {symbol}, nákup neodeslán")

        try:
            order = self.client.create_order(symbol, "market", order_side, quantity, price, req)
        except Exception as exc:  # chybu neřešíme tady — předáváme ji routeru
            log.error("Market order %s %s %s selhal: %s", symbol, order_side, quantity, exc)
            return OrderResult(ok=False, error=str(exc))
        return OrderResult(
            ok=True, order_id=str(order.get("id")),
            filled_price=_opt_float(order.get("average") or order.get("price")),
            filled_qty=float(order.get("filled") or quantity), raw=order,
        )

    def _needs_buy_price(self, side: Side, reduce_only: bool) -> bool:
        return self.cfg.is_spot and side is Side.LONG and not reduce_only

    def _market_buy_price(self, symbol: str) -> float | None:
        """Aktuální cena pro přepočet množství na částku k utracení."""
        try:
            last = _opt_float(self.fetch_ticker(symbol).get("last"))
        except Exception as exc:
            log.error("Cena pro %s není dostupná, nákup neodesílám: %s", symbol, exc)
            return None
        if not last or last <= 0:
            log.error("Neplatná cena pro %s, nákup neodesílám", symbol)
            return None
        return last

    def create_stop_loss(self, symbol: str, side: Side, quantity: float, stop_price: float) -> OrderResult:
        """SL je vždy reduce-only opačným směrem než pozice."""
        if self.cfg.is_spot:
            return OrderResult(
                ok=False,
                error="spotový účet nepodporuje reduce-only stop příkazy; "
                      "stopy hlídá bot lokálně (exits.use_exchange_stops=false)",
            )
        exit_side = "sell" if side is Side.LONG else "buy"
        price = self.price_to_precision(symbol, stop_price)
        params: dict[str, Any] = {
            "reduceOnly": True,
            "stopPrice": price,
            "triggerPrice": price,
            "triggerDirection": 2 if side is Side.LONG else 1,
        }
        if self.id.startswith("bybit"):
            params["stopLoss"] = price
        if self.cfg.hedge_mode:
            params.setdefault("positionIdx", 1 if side is Side.LONG else 2)
        last = "neznámá chyba"
        for order_type in ("stop_market", "market", "stop"):
            try:
                order = self.client.create_order(symbol, order_type, exit_side, quantity, None, params)
                return OrderResult(ok=True, order_id=str(order.get("id")), raw=order)
            except Exception as exc:
                last = str(exc)
                log.debug("SL typ %s odmítnut (%s): %s", order_type, symbol, exc)
        return OrderResult(ok=False, error=last)

    def create_take_profit(self, symbol: str, side: Side, quantity: float, price: float) -> OrderResult:
        if self.cfg.is_spot:
            return OrderResult(ok=False, error="spotový účet: TP hlídá bot lokálně")
        exit_side = "sell" if side is Side.LONG else "buy"
        tp = self.price_to_precision(symbol, price)
        params: dict[str, Any] = {
            "reduceOnly": True,
            "stopPrice": tp,
            "triggerPrice": tp,
            "triggerDirection": 1 if side is Side.LONG else 2,
        }
        if self.cfg.hedge_mode:
            params.setdefault("positionIdx", 1 if side is Side.LONG else 2)
        last = "neznámá chyba"
        for order_type in ("take_profit_market", "limit", "market"):
            try:
                limit_price = tp if order_type == "limit" else None
                order = self.client.create_order(symbol, order_type, exit_side, quantity, limit_price, params)
                return OrderResult(ok=True, order_id=str(order.get("id")), raw=order)
            except Exception as exc:
                last = str(exc)
                log.debug("TP typ %s odmítnut (%s): %s", order_type, symbol, exc)
        return OrderResult(ok=False, error=last)

    def cancel_all_orders(self, symbol: str) -> None:
        try:
            self.client.cancel_all_orders(symbol)
        except Exception as exc:
            log.warning("cancel_all_orders(%s) selhalo: %s", symbol, exc)

    def close_position(self, symbol: str) -> OrderResult:
        if not self.tracks_positions:
            return OrderResult(
                ok=False, error="spot: uzavření řídí router podle evidence obchodů"
            )
        positions = [p for p in self.fetch_positions([symbol]) if p.symbol == symbol]
        if not positions:
            return OrderResult(ok=True, error="žádná otevřená pozice")
        pos = positions[0]
        self.cancel_all_orders(symbol)
        return self.create_market_order(symbol, pos.side.opposite, pos.quantity, reduce_only=True)

    # ---------- přesnost a limity ----------

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        try:
            return float(self.client.amount_to_precision(symbol, amount))
        except Exception:
            return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        try:
            return float(self.client.price_to_precision(symbol, price))
        except Exception:
            return price

    def market_limits(self, symbol: str) -> dict[str, float]:
        market = self._market(symbol)
        limits = market.get("limits", {})
        lev = limits.get("leverage", {}) or {}
        return {
            "min_amount": float((limits.get("amount", {}) or {}).get("min") or 0.0),
            "min_cost": float((limits.get("cost", {}) or {}).get("min") or 0.0),
            "max_leverage": float(lev.get("max") or registry.max_leverage(self.id)),
            "contract_size": float(market.get("contractSize") or 1.0),
        }


def _opt_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_number(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Projde i vnořené struktury typu {"result": {"list": [{...}]}}."""
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in keys:
                if key in node:
                    value = _opt_float(node[key])
                    if value is not None:
                        return value
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None
