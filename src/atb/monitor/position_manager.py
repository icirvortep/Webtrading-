"""Správa otevřených pozic v čase: breakeven, trailing stop, časový stop, rekonciliace.

Běží ve vlastním vlákně a v pravidelném intervalu:
  1. zjistí aktuální ceny a pozice na burze,
  2. posune SL na breakeven po dosažení TP1,
  3. táhne trailing stop podle ATR po dosažení TP2,
  4. zavře pozici při překročení maximální doby držení,
  5. rekonciluje DB se skutečností (pozice zavřená burzou přes SL/TP).
"""
from __future__ import annotations

import json
import logging
import threading
import time

from ..config import AppConfig
from ..exchanges.base import Exchange
from ..execution.router import ExecutionRouter
from ..models import Position, Side
from ..notify import Notifier
from ..state.store import Store
from ..strategy import exits

log = logging.getLogger(__name__)


class PositionManager:
    def __init__(
        self, cfg: AppConfig, exchange: Exchange, store: Store,
        router: ExecutionRouter, notifier: Notifier,
    ) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.store = store
        self.router = router
        self.notifier = notifier
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_reconcile = 0.0

    # ---------- životní cyklus ----------

    def start(self) -> None:
        if not self.cfg.monitor.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="position-manager", daemon=True)
        self._thread.start()
        log.info("Position manager spuštěn (interval %.1fs)", self.cfg.monitor.poll_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.cfg.monitor.poll_seconds):
            try:
                self.tick()
            except Exception as exc:  # smyčka musí přežít jakoukoli chybu
                log.exception("Chyba v position manageru: %s", exc)

    # ---------- jedna iterace ----------

    def tick(self) -> None:
        open_trades = self.store.open_trades()
        if not open_trades and not self._reconcile_due():
            return

        if self.exchange.tracks_positions:
            positions = {p.symbol: p for p in self.exchange.fetch_positions()}
        else:
            # Spot: burza pozice nezná, jedinou evidencí je databáze bota.
            positions = {t["symbol"]: _position_from_trade(t) for t in open_trades}

        for trade in open_trades:
            symbol = trade["symbol"]
            position = positions.get(symbol)
            if position is None:
                self._settle_missing(trade)
                continue
            self._manage(trade, position)

        self._maybe_reconcile(list(positions.values()))

    def _manage(self, trade: dict, position: Position) -> None:
        plan = _plan_of(trade)
        if not plan:
            return
        side = Side(trade["side"])
        price = float(self.exchange.fetch_ticker(position.symbol).get("last") or 0.0)
        if price <= 0:
            return

        entry = float(trade["entry"])
        current_stop = float(trade["stop_loss"] or plan.get("stop_loss") or 0.0)
        # R se vždy měří k PŮVODNÍMU stopu z plánu — jinak by posun na
        # breakeven zmenšil R k nule a trailing by se spustil předčasně.
        original_stop = float(plan.get("stop_loss") or current_stop)
        r = abs(entry - original_stop)
        if r <= 0 or current_stop <= 0:
            return
        progress_r = (price - entry) * side.sign / r

        new_stop = current_stop
        reason = ""

        # 1) breakeven po dosažení úrovně prvního TP
        be_after = int(plan.get("breakeven_after_tp", self.cfg.exits.breakeven_after_tp))
        tps = plan.get("take_profits") or []
        if be_after and len(tps) >= be_after:
            trigger_r = float(tps[be_after - 1].get("r", 1.0))
            if progress_r >= trigger_r:
                be = exits.breakeven_price(side, entry, self.cfg.exits)
                if (side is Side.LONG and be > new_stop) or (side is Side.SHORT and be < new_stop):
                    new_stop, reason = be, "breakeven"

        # 2) ATR trailing po dosažení druhého TP
        trail_after = int(plan.get("trail_after_tp", self.cfg.exits.trail_after_tp))
        if trail_after and len(tps) >= trail_after:
            trigger_r = float(tps[trail_after - 1].get("r", 2.0))
            if progress_r >= trigger_r:
                distance = float(plan.get("atr", 0.0)) * float(
                    plan.get("trail_atr_mult", self.cfg.exits.trail_atr_mult.get("trend_up", 2.5))
                )
                if distance <= 0:
                    distance = r * 1.5
                trailed = exits.next_trailing_stop(side, new_stop, price, distance)
                if trailed and (
                    (side is Side.LONG and trailed > new_stop)
                    or (side is Side.SHORT and trailed < new_stop)
                ):
                    new_stop, reason = trailed, "trailing"

        if reason and abs(new_stop - current_stop) > entry * 1e-6:
            self._move_stop(trade, position, side, new_stop, reason, progress_r)
            current_stop = new_stop

        # Když stopy neleží na burze, musí je vyhodnotit bot sám.
        if not self.cfg.exits.use_exchange_stops and self._run_local_exits(
            trade, side, price, current_stop, plan
        ):
            return

        # 3) časový stop
        max_hold = int(plan.get("max_hold_minutes", self.cfg.exits.max_hold_minutes) or 0)
        if max_hold and time.time() - float(trade["opened_at"]) > max_hold * 60:
            log.info("Časový stop na %s po %d min", position.symbol, max_hold)
            self.router.close(position.symbol, reason="time_stop")

    def _run_local_exits(
        self, trade: dict, side: Side, price: float, stop: float, plan: dict,
    ) -> bool:
        """Vyhodnotí SL a TP lokálně. Vrací True, pokud pozice skončila.

        Používá se tam, kde burza reduce-only stop příkazy neumí (spot).
        Stop se testuje jako první — když by jeden pohyb ceny protnul SL i TP,
        počítá se ta horší varianta.
        """
        hit_stop = price <= stop if side is Side.LONG else price >= stop
        if hit_stop:
            log.info("%s: lokální SL na %.6f (cena %.6f)", trade["symbol"], stop, price)
            self.router.close(trade["symbol"], reason="local_stop_loss")
            return True

        targets = plan.get("take_profits") or []
        filled = int(trade["tp_filled"] or 0)
        for index in range(filled, len(targets)):
            target = targets[index]
            reached = (price >= target["price"] if side is Side.LONG
                       else price <= target["price"])
            if not reached:
                break
            fresh = self.store.open_trades(trade["symbol"])
            current = next((t for t in fresh if t["id"] == trade["id"]), None)
            if current is None:
                return True
            self.router.take_partial_profit(current, float(target["fraction"]), f"TP{index + 1}")
            if not self.store.open_trades(trade["symbol"]):
                return True
        return False

    def _move_stop(
        self, trade: dict, position: Position, side: Side,
        new_stop: float, reason: str, progress_r: float,
    ) -> None:
        price = self.exchange.price_to_precision(position.symbol, new_stop)
        if self.cfg.exits.use_exchange_stops:
            self.exchange.cancel_all_orders(position.symbol)
            result = self.exchange.create_stop_loss(position.symbol, side, position.quantity, price)
            if not result.ok:
                log.error("Posun SL na %s selhal: %s", position.symbol, result.error)
                return
        self.store.update_trade_stop(trade["id"], price)
        log.info("%s: SL → %.6f (%s, %.2fR)", position.symbol, price, reason, progress_r)
        self.notifier.send(
            f"🛡️ <b>{position.symbol}</b>: SL posunut na {price:.6f} ({reason}, {progress_r:+.2f}R)",
            "risk",
        )

    def _settle_missing(self, trade: dict) -> None:
        """Pozice zmizela z burzy → zavřel ji SL/TP. Dopočítáme PnL z ceny."""
        symbol = trade["symbol"]
        price = float(self.exchange.fetch_ticker(symbol).get("last") or 0.0)
        side = Side(trade["side"])
        exit_price = price or float(trade["stop_loss"] or trade["entry"])
        pnl = (exit_price - float(trade["entry"])) * float(trade["quantity"]) * side.sign
        self.store.close_trade(trade["id"], exit_price, pnl, exit_reason="exchange_stop")
        self.exchange.cancel_all_orders(symbol)
        log.info("Pozice %s uzavřena burzou, PnL %.4f", symbol, pnl)
        self.notifier.send(
            f"{'✅' if pnl >= 0 else '🔻'} <b>{symbol}</b> uzavřeno burzou (SL/TP)\n"
            f"PnL: <b>{pnl:+.2f} {self.cfg.exchange.quote}</b>",
            "exit",
        )

    def _reconcile_due(self) -> bool:
        return time.time() - self._last_reconcile >= self.cfg.monitor.reconcile_seconds

    def _maybe_reconcile(self, positions: list[Position]) -> None:
        if not self._reconcile_due():
            return
        self._last_reconcile = time.time()
        try:
            balance = self.exchange.fetch_balance()
            self.store.record_equity(balance.equity)
        except Exception as exc:
            log.warning("Nepodařilo se načíst zůstatek: %s", exc)
            return

        tracked = {t["symbol"] for t in self.store.open_trades()}
        orphans = [p for p in positions if p.symbol not in tracked]
        for orphan in orphans:
            log.warning(
                "Neevidovaná pozice na burze: %s %s %.8f — bot ji neřídí",
                orphan.symbol, orphan.side.value, orphan.quantity,
            )


def _position_from_trade(trade: dict) -> Position:
    """Pozice odvozená z evidence bota — pro trhy bez pozičního API."""
    return Position(
        symbol=trade["symbol"], side=Side(trade["side"]),
        quantity=float(trade["qty_open"] if trade["qty_open"] is not None else trade["quantity"]),
        entry_price=float(trade["entry"]), leverage=int(trade["leverage"] or 1),
        opened_at=float(trade["opened_at"]),
    )


def _plan_of(trade: dict) -> dict:
    raw = trade.get("plan")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
