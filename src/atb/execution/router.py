"""Provedení plánu na burze: páka → vstupní příkaz → SL → TP ladder → zápis do DB.

Router je záměrně "hloupý" — rozhoduje risk manager a engine. Router jen
spolehlivě provede plán a uklidí po sobě, když něco selže.
"""
from __future__ import annotations

import logging
import time

from ..config import AppConfig
from ..exchanges.base import Exchange
from ..models import OrderResult, Side, TradePlan
from ..notify import Notifier
from ..state.store import Store

log = logging.getLogger(__name__)


class ExecutionRouter:
    def __init__(self, cfg: AppConfig, exchange: Exchange, store: Store, notifier: Notifier) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.store = store
        self.notifier = notifier

    # ---------- vstup ----------

    def execute(self, plan: TradePlan) -> OrderResult:
        symbol = plan.symbol
        quantity = self.exchange.amount_to_precision(symbol, plan.quantity)
        if quantity <= 0:
            return OrderResult(ok=False, error="množství po zaokrouhlení je nulové")

        self.exchange.set_leverage(symbol, plan.leverage)

        entry = self.exchange.create_market_order(
            symbol, plan.side, quantity, params={"leverage": plan.leverage}
        )
        if not entry.ok:
            self.notifier.send(f"❌ Vstup {symbol} selhal: {entry.error}", "error")
            return entry

        fill_price = entry.filled_price or plan.entry
        filled_qty = entry.filled_qty or quantity
        trade_id = self.store.open_trade(plan.as_dict(), fill_price, filled_qty)

        protection_ok = True
        if self.cfg.exits.use_exchange_stops:
            protection_ok = self._place_protection(plan, filled_qty)
            if not protection_ok:
                log.error("SL/TP se nepodařilo umístit — zavírám pozici %s", symbol)
                self.exchange.close_position(symbol)
                self.store.close_trade(trade_id, fill_price, 0.0, exit_reason="sl_placement_failed")
                self.notifier.send(
                    f"⚠️ {symbol}: nepodařilo se umístit SL, pozice okamžitě uzavřena", "error"
                )
                return OrderResult(ok=False, error="SL se nepodařilo umístit")

        self.store.set(f"trade_meta:{trade_id}", str(int(time.time())))
        self._notify_entry(plan, fill_price, filled_qty, trade_id)
        return OrderResult(
            ok=True, order_id=entry.order_id, filled_price=fill_price,
            filled_qty=filled_qty, raw={"trade_id": trade_id},
        )

    def _place_protection(self, plan: TradePlan, quantity: float) -> bool:
        """SL je povinný, TP jsou best-effort (částečné TP burza nemusí umět)."""
        sl = self.exchange.create_stop_loss(plan.symbol, plan.side, quantity, plan.stop_loss)
        if not sl.ok:
            log.error("SL pro %s odmítnut: %s", plan.symbol, sl.error)
            return False

        remaining = quantity
        for index, tp in enumerate(plan.take_profits, start=1):
            qty = self.exchange.amount_to_precision(plan.symbol, quantity * tp.fraction)
            if qty <= 0 or qty > remaining:
                qty = min(remaining, qty)
            if qty <= 0:
                continue
            result = self.exchange.create_take_profit(plan.symbol, plan.side, qty, tp.price)
            if result.ok:
                remaining -= qty
            else:
                log.warning("TP%d pro %s odmítnut: %s", index, plan.symbol, result.error)
        return True

    # ---------- výstup ----------

    def close(self, symbol: str, reason: str = "manual") -> OrderResult:
        trades = self.store.open_trades(symbol)
        positions = [p for p in self.exchange.fetch_positions([symbol]) if p.symbol == symbol]

        if self.exchange.tracks_positions:
            result = self.exchange.close_position(symbol)
        else:
            # Spot: burza pozice nezná, prodáváme podle vlastní evidence.
            result = self._close_from_records(symbol, trades)
        if not result.ok:
            self.notifier.send(f"❌ Uzavření {symbol} selhalo: {result.error}", "error")
            return result

        exit_price = result.filled_price or (positions[0].entry_price if positions else 0.0)
        for trade in trades:
            pnl = self._realized_pnl(trade, exit_price)
            self.store.close_trade(trade["id"], exit_price, pnl, exit_reason=reason)
            self.notifier.send(
                f"{'✅' if pnl >= 0 else '🔻'} <b>{symbol}</b> uzavřeno ({reason})\n"
                f"PnL: <b>{pnl:+.2f} {self.cfg.exchange.quote}</b> @ {exit_price:.6f}",
                "exit",
            )
        return result

    def _close_from_records(self, symbol: str, trades: list[dict]) -> OrderResult:
        """Uzavření podle evidence bota — pro trhy bez pozičního API (spot)."""
        quantity = sum(float(t["qty_open"] or 0.0) for t in trades)
        if quantity <= 0:
            return OrderResult(ok=True, error="žádné evidované množství")
        side = Side(trades[0]["side"])
        self.exchange.cancel_all_orders(symbol)
        return self.exchange.create_market_order(
            symbol, side.opposite, self.exchange.amount_to_precision(symbol, quantity),
            reduce_only=self.exchange.tracks_positions,
        )

    def take_partial_profit(self, trade: dict, fraction: float, reason: str) -> OrderResult:
        """Odprodá část pozice při zásahu TP a zapíše realizovaný zisk."""
        symbol = trade["symbol"]
        side = Side(trade["side"])
        quantity = self.exchange.amount_to_precision(
            symbol, float(trade["quantity"]) * fraction
        )
        remaining = float(trade["qty_open"] or 0.0)
        quantity = min(quantity, remaining)
        if quantity <= 0:
            return OrderResult(ok=False, error="nulové množství k odprodeji")

        result = self.exchange.create_market_order(
            symbol, side.opposite, quantity,
            reduce_only=self.exchange.tracks_positions,
        )
        if not result.ok:
            log.error("Částečný výstup %s selhal: %s", symbol, result.error)
            return result

        fill = result.filled_price or float(trade["entry"])
        pnl = (fill - float(trade["entry"])) * quantity * side.sign
        left = self.store.record_partial_exit(trade["id"], quantity, pnl)
        self.notifier.send(
            f"🎯 <b>{symbol}</b> {reason}: odprodáno {fraction * 100:.0f} % @ {fill:.6f}\n"
            f"Realizováno: <b>{pnl:+.2f} {self.cfg.exchange.quote}</b>, zbývá {left:.8f}",
            "exit",
        )
        if left <= 0:
            self.store.close_trade(trade["id"], fill, 0.0, exit_reason=reason)
        return result

    def close_all(self, reason: str = "close_all") -> list[OrderResult]:
        results = []
        for pos in self.exchange.fetch_positions():
            results.append(self.close(pos.symbol, reason))
        return results

    def _realized_pnl(self, trade: dict, exit_price: float) -> float:
        """Zisk poslední nohy — počítá se ze zbývajícího, ne z původního množství."""
        side = Side(trade["side"])
        quantity = float(trade["qty_open"] if trade["qty_open"] is not None else trade["quantity"])
        return (exit_price - trade["entry"]) * quantity * side.sign

    def _notify_entry(self, plan: TradePlan, fill: float, qty: float, trade_id: int) -> None:
        tps = " / ".join(f"{tp.price:.6f} ({tp.fraction * 100:.0f}%)" for tp in plan.take_profits)
        risk_pct_move = plan.stop_distance / fill * 100.0 if fill else 0.0
        self.notifier.send(
            f"🚀 <b>{plan.symbol} {plan.side.value.upper()}</b> #{trade_id}\n"
            f"Vstup: {fill:.6f} | množství: {qty:.8f} | páka: {plan.leverage}x\n"
            f"SL: {plan.stop_loss:.6f} ({risk_pct_move:.2f}%)\n"
            f"TP: {tps or '—'}\n"
            f"Riziko: {plan.risk_amount:.2f} {self.cfg.exchange.quote} ({plan.risk_pct:.2f}% equity)\n"
            f"Režim: {plan.regime.value} | skóre: {plan.score:.3f}",
            "entry",
        )
