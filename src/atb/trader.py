"""Trader — jediný vstupní bod pro zpracování signálu.

Drží dohromady burzu, engine, risk manager, router, DB a position manager.
Zpracování signálu je serializované zámkem, aby dva souběžné webhooky
nemohly otevřít dvě pozice nad rámec limitů.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .config import AppConfig
from .exchanges.base import Exchange
from .exchanges.factory import build_exchange
from .execution.router import ExecutionRouter
from .models import Action, RejectReason, Signal
from .monitor.position_manager import PositionManager
from .notify import Notifier
from .risk.manager import RiskManager
from .state.store import Store
from .strategy.engine import StrategyEngine

log = logging.getLogger(__name__)


class Trader:
    def __init__(self, cfg: AppConfig, exchange: Exchange | None = None, store: Store | None = None) -> None:
        self.cfg = cfg
        self.store = store or Store(cfg.database)
        self.exchange = exchange or build_exchange(cfg)
        self.notifier = Notifier(cfg.notify)
        self.risk = RiskManager(cfg, self.store)
        self.engine = StrategyEngine(cfg, self.exchange, self.risk)
        self.router = ExecutionRouter(cfg, self.exchange, self.store, self.notifier)
        self.positions = PositionManager(cfg, self.exchange, self.store, self.router, self.notifier)
        self._lock = threading.Lock()

    # ---------- životní cyklus ----------

    def start(self) -> None:
        balance = self.exchange.fetch_balance()
        self.store.start_of_day_equity(balance.equity)
        self.store.record_equity(balance.equity)
        self.positions.start()
        log.info(
            "Trader připraven | režim=%s burza=%s equity=%.2f %s riziko/obchod=%.2f%%",
            self.cfg.mode, self.exchange.id, balance.equity, balance.currency,
            self.cfg.risk.risk_per_trade_pct,
        )
        self.notifier.send(
            f"🤖 Bot spuštěn — {self.cfg.mode.upper()} na {self.exchange.id}\n"
            f"Equity: {balance.equity:.2f} {balance.currency} | "
            f"riziko: {self.cfg.risk.risk_per_trade_pct}% / obchod",
            "entry",
        )

    def shutdown(self, close_positions: bool = False) -> None:
        self.positions.stop()
        if close_positions:
            self.router.close_all("shutdown")
        self.store.close()

    # ---------- zpracování signálu ----------

    def handle_signal(self, signal: Signal) -> dict[str, Any]:
        with self._lock:
            return self._handle_signal(signal)

    def _handle_signal(self, signal: Signal) -> dict[str, Any]:
        if self.store.seen_signal(signal.id):
            log.info("Duplicitní signál %s ignorován", signal.id)
            return {"status": "duplicate", "signal_id": signal.id}

        if signal.action is Action.CLOSE_ALL:
            self.router.close_all("signal_close_all")
            self.store.record_signal(signal.id, signal.symbol, signal.action.value, None, True,
                                     "close_all", payload=signal.raw)
            return {"status": "closed_all"}

        if signal.action is Action.EXIT:
            result = self.router.close(signal.symbol, "signal_exit")
            self.store.record_signal(signal.id, signal.symbol, signal.action.value, None,
                                     result.ok, result.error or "exit", payload=signal.raw)
            return {"status": "closed" if result.ok else "error", "error": result.error}

        if signal.action is Action.REVERSE:
            self.router.close(signal.symbol, "signal_reverse")

        balance = self.exchange.fetch_balance()
        open_symbols = [p.symbol for p in self.exchange.fetch_positions()]
        decision = self.engine.decide(signal, balance, open_symbols)

        if not decision.accepted or decision.plan is None:
            reason = decision.reason.value if decision.reason else "unknown"
            self.store.record_signal(
                signal.id, signal.symbol, signal.action.value,
                signal.side.value if signal.side else None,
                False, f"{reason}: {decision.detail}", payload=signal.raw,
            )
            if decision.reason in (RejectReason.KILL_SWITCH, RejectReason.DAILY_LOSS_LIMIT):
                self.notifier.send(f"⛔ {signal.symbol}: {decision.detail}", "risk")
            return {"status": "rejected", "reason": reason, "detail": decision.detail}

        plan = decision.plan
        self.store.record_signal(
            signal.id, signal.symbol, signal.action.value, plan.side.value, True,
            "accepted", score=plan.score, regime=plan.regime.value, payload=signal.raw,
        )

        if self.cfg.dry_run and self.cfg.mode == "live":
            log.warning("DRY RUN: plán vypočten, ale neodesílá se na burzu")
            return {"status": "dry_run", "plan": plan.as_dict()}

        result = self.router.execute(plan)
        if not result.ok:
            return {"status": "error", "error": result.error, "plan": plan.as_dict()}
        return {
            "status": "executed",
            "trade_id": result.raw.get("trade_id"),
            "fill_price": result.filled_price,
            "plan": plan.as_dict(),
        }

    # ---------- introspekce ----------

    def status(self) -> dict[str, Any]:
        balance = self.exchange.fetch_balance()
        positions = self.exchange.fetch_positions()
        return {
            "mode": self.cfg.mode,
            "dry_run": self.cfg.dry_run,
            "exchange": self.exchange.id,
            "equity": round(balance.equity, 4),
            "free": round(balance.free, 4),
            "currency": balance.currency,
            "kill_switch": self.cfg.risk.kill_switch,
            "risk_per_trade_pct": self.cfg.risk.risk_per_trade_pct,
            "open_positions": [
                {
                    "symbol": p.symbol, "side": p.side.value, "qty": p.quantity,
                    "entry": p.entry_price, "leverage": p.leverage,
                    "unrealized_pnl": round(p.unrealized_pnl, 4),
                }
                for p in positions
            ],
            "stats": self.store.stats(),
        }
