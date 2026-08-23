"""Skener trhu — srdce živého přehledu.

Na pozadí prochází sledované symboly, u každého spočítá režim, skóre pro long
i short, navrhne SL/TP a najde vstupní spouštěče. Výsledek drží v paměti, aby
ho rozhraní mohlo kdykoli zobrazit bez čekání na burzu.

V režimu *autopilot* navíc sám posílá signály traderovi — bot pak obchoduje
z vlastní analýzy a TradingView vůbec nepotřebuje.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import AppConfig
from .models import Action, Side, Signal
from .strategy import exits, scoring
from .strategy import signals as signal_mod

log = logging.getLogger(__name__)


class MarketScanner:
    def __init__(self, cfg: AppConfig, trader: Any) -> None:
        self.cfg = cfg
        self.trader = trader
        self.results: dict[str, dict[str, Any]] = {}
        self.last_scan: float = 0.0
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    # ---------- životní cyklus ----------

    def start(self) -> None:
        if not self.cfg.scanner.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="market-scanner", daemon=True)
        self._thread.start()
        log.info(
            "Skener spuštěn: %s à %.0fs%s",
            ", ".join(self.cfg.scanner.watchlist), self.cfg.scanner.interval_seconds,
            " [AUTOPILOT]" if self.cfg.scanner.autopilot else "",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        self.scan_once()
        while not self._stop.wait(self.cfg.scanner.interval_seconds):
            try:
                self.scan_once()
            except Exception as exc:  # smyčka skeneru musí přežít cokoli
                log.exception("Chyba skeneru: %s", exc)
                self.last_error = str(exc)

    # ---------- jedno kolo ----------

    def scan_once(self) -> dict[str, dict[str, Any]]:
        for symbol in list(self.cfg.scanner.watchlist):
            try:
                result = self.scan_symbol(symbol)
            except Exception as exc:  # jeden vadný symbol nesmí zastavit ostatní
                log.warning("Sken %s selhal: %s", symbol, exc)
                result = {"symbol": symbol, "error": str(exc), "ts": time.time()}
            with self._lock:
                self.results[symbol] = result
        self.last_scan = time.time()
        return self.snapshot()

    def scan_symbol(self, symbol: str) -> dict[str, Any]:
        cfg = self.cfg
        timeframe = cfg.scanner.timeframe
        snap = self.trader.engine.analyze(symbol, timeframe)
        ohlcv = self.trader.exchange.fetch_ohlcv(symbol, timeframe, limit=cfg.strategy.ohlcv_limit)

        sides: dict[str, Any] = {}
        for side in (Side.LONG, Side.SHORT):
            score = scoring.evaluate(side, snap, cfg.strategy)
            stop = exits.build_stop(side, snap.price, snap, cfg.exits)
            take_profits = exits.build_take_profits(side, snap.price, stop, snap, cfg.exits)
            sides[side.value] = {
                "score": score.value,
                "veto": score.veto,
                "tradeable": score.ok and score.value >= cfg.strategy.min_score,
                "reasons": score.reasons,
                "stop_loss": round(stop, 8),
                "stop_pct": round(abs(snap.price - stop) / snap.price * 100, 3),
                "take_profits": [
                    {"price": round(tp.price, 8), "r": tp.r_multiple,
                     "fraction": round(tp.fraction, 4)}
                    for tp in take_profits
                ],
            }

        triggers = signal_mod.detect(ohlcv, snap, cfg.strategy)
        result = {
            "symbol": symbol,
            "ts": time.time(),
            "market": snap.as_dict(),
            "sides": sides,
            "triggers": [t.as_dict() for t in triggers],
            "sparkline": [round(row[4], 8) for row in ohlcv[-60:]],
        }

        if cfg.scanner.autopilot:
            self._maybe_autotrade(symbol, triggers, sides, result)
        return result

    def _maybe_autotrade(
        self, symbol: str, triggers: list[signal_mod.Trigger],
        sides: dict[str, Any], result: dict[str, Any],
    ) -> None:
        """V autopilotu pošle spouštěč traderovi jako běžný signál."""
        for trigger in triggers:
            if trigger.strength < self.cfg.scanner.min_trigger_strength:
                continue
            side_info = sides[trigger.side.value]
            if not side_info["tradeable"]:
                continue
            bar_ts = int(result["ts"] // max(self.cfg.scanner.interval_seconds, 1))
            signal = Signal(
                symbol=symbol, action=Action.ENTRY, side=trigger.side,
                timeframe=self.cfg.scanner.timeframe, strategy=f"autopilot:{trigger.kind}",
                confidence=trigger.strength, price=result["market"]["price"],
                id=f"auto-{symbol}-{trigger.side.value}-{bar_ts}",
                raw={"trigger": trigger.as_dict()},
            )
            outcome = self.trader.handle_signal(signal)
            result.setdefault("autopilot", []).append(
                {"trigger": trigger.kind, "side": trigger.side.value, **outcome}
            )
            log.info(
                "AUTOPILOT %s %s (%s) → %s",
                symbol, trigger.side.value, trigger.kind, outcome.get("status"),
            )
            return          # na symbol nejvýš jeden pokus za kolo

    # ---------- čtení ----------

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self.results)

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.scanner.enabled,
            "autopilot": self.cfg.scanner.autopilot,
            "timeframe": self.cfg.scanner.timeframe,
            "interval_seconds": self.cfg.scanner.interval_seconds,
            "watchlist": list(self.cfg.scanner.watchlist),
            "last_scan": self.last_scan,
            "age_seconds": round(time.time() - self.last_scan, 1) if self.last_scan else None,
            "last_error": self.last_error,
            "markets": list(self.snapshot().values()),
        }
