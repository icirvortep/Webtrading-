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
from .universe import UniverseSelector

log = logging.getLogger(__name__)


def _for_market_type(symbol: str, account_type: str) -> str:
    """Sladí zápis symbolu s typem účtu.

    Pevný watchlist je v konfiguraci zapsaný jednou, ale spot používá
    'BTC/USDT' a perpetuály 'BTC/USDT:USDT'. Bez tohohle by záložní seznam
    na spotovém účtu odkazoval na neexistující trhy.
    """
    base = symbol.split(":")[0]
    if account_type == "spot":
        return base
    if ":" in symbol:
        return symbol
    quote = base.split("/")[-1]
    return f"{base}:{quote}"


class MarketScanner:
    def __init__(self, cfg: AppConfig, trader: Any) -> None:
        self.cfg = cfg
        self.trader = trader
        self.universe = UniverseSelector(cfg.universe, trader.exchange)
        self.results: dict[str, dict[str, Any]] = {}
        self.last_scan: float = 0.0
        self._cursor = 0                 # kam jsme se dostali v dávkovém průchodu
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
        source = ("celá burza (automatický výběr)" if self.cfg.scanner.auto_universe
                  else ", ".join(self.cfg.scanner.watchlist))
        log.info(
            "Skener spuštěn: %s à %.0fs%s",
            source, self.cfg.scanner.interval_seconds,
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

    def watchlist(self) -> list[str]:
        """Co se má analyzovat — buď pevný seznam, nebo špička žebříčku burzy."""
        if self.cfg.scanner.auto_universe and self.cfg.universe.enabled:
            symbols = self.universe.symbols()
            if symbols:
                return symbols
            log.warning(
                "Automatický výběr trhů nic nevrátil — sáhnu po pevném seznamu. "
                "Nejčastější příčina: universe.min_volume_24h je pro tuhle burzu "
                "příliš vysoký (teď %.0f).", self.cfg.universe.min_volume_24h,
            )
        return [_for_market_type(s, self.cfg.exchange.account_type)
                for s in self.cfg.scanner.watchlist]

    def _batch(self, symbols: list[str]) -> list[str]:
        """Dávka pro tohle kolo — kandidáti se střídají, ať nepřetečou limity API."""
        size = max(1, self.cfg.universe.batch_size)
        if not self.cfg.scanner.auto_universe or len(symbols) <= size:
            return symbols
        start = self._cursor % len(symbols)
        batch = (symbols + symbols)[start:start + size]
        self._cursor = (start + size) % len(symbols)
        return batch

    def scan_once(self) -> dict[str, dict[str, Any]]:
        symbols = self.watchlist()
        with self._lock:
            # trhy, které vypadly ze žebříčku, zmizí i z přehledu
            stale = [s for s in self.results if s not in symbols]
            for symbol in stale:
                del self.results[symbol]
        for symbol in self._batch(symbols):
            try:
                result = self.scan_symbol(symbol)
            except Exception as exc:  # jeden vadný symbol nesmí zastavit ostatní
                log.warning("Sken %s selhal: %s", symbol, exc)
                result = {"symbol": symbol, "error": str(exc), "ts": time.time()}
            with self._lock:
                self.results[symbol] = result
        self.last_scan = time.time()
        return self.snapshot()

    def opportunities(self, limit: int = 40) -> list[dict[str, Any]]:
        """Analyzované trhy seřazené podle nejlepší obchodovatelné příležitosti."""
        rows = []
        for entry in self.snapshot().values():
            if "sides" not in entry:
                continue
            direction, best = max(
                entry["sides"].items(), key=lambda item: item[1]["score"]
            )
            rows.append({
                "symbol": entry["symbol"],
                "price": entry["market"]["price"],
                "regime": entry["market"]["regime"],
                "atr_pct": entry["market"]["atr_pct"],
                "side": direction,
                "score": best["score"],
                "tradeable": best["tradeable"],
                "veto": best["veto"],
                "stop_pct": best["stop_pct"],
                "take_profits": len(best["take_profits"]),
                "triggers": entry["triggers"],
                "age_seconds": round(time.time() - entry["ts"], 1),
            })
        rows.sort(key=lambda r: (r["tradeable"], r["score"]), reverse=True)
        return rows[:limit]

    def scan_symbol(self, symbol: str) -> dict[str, Any]:
        cfg = self.cfg
        timeframe = cfg.scanner.timeframe
        snap = self.trader.engine.analyze(symbol, timeframe)
        ohlcv = self.trader.exchange.fetch_ohlcv(symbol, timeframe, limit=cfg.strategy.ohlcv_limit)

        # Na spotu nemá smysl počítat shorty — prodat jde jen to, co vlastníš.
        directions = (Side.LONG, Side.SHORT) if self.trader.exchange.can_short else (Side.LONG,)
        sides: dict[str, Any] = {}
        for side in directions:
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

        triggers = [
            trigger for trigger in signal_mod.detect(ohlcv, snap, cfg.strategy)
            if trigger.side.value in sides
        ]
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
            "auto_universe": self.cfg.scanner.auto_universe,
            "watchlist": self.watchlist(),
            "universe": self.universe.state(),
            "opportunities": self.opportunities(),
            "last_scan": self.last_scan,
            "age_seconds": round(time.time() - self.last_scan, 1) if self.last_scan else None,
            "last_error": self.last_error,
            "markets": list(self.snapshot().values()),
        }
