"""Výběr obchodovatelných trhů z celé nabídky burzy.

Bybit nabízí stovky perpetual kontraktů. Projít je všechny v plné hloubce
by trvalo minuty a narazilo by na limity API, takže výběr má dvě fáze:

1. **Hrubé síto** — jeden hromadný dotaz na tickery. Odfiltruje nelikvidní
   trhy a zbytek seřadí podle likvidity, volatility a denního pohybu.
2. **Hloubková analýza** — jen na špičce žebříčku, po dávkách, aby se
   zátěž rozložila v čase (řeší `MarketScanner`).

Pořadí se přepočítává jen jednou za `refresh_minutes`; mezi tím se pracuje
s uloženým seznamem.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from .config import UniverseConfig
from .exchanges.base import Exchange

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Candidate:
    """Jeden trh v žebříčku i s tím, proč se tam dostal."""

    symbol: str
    price: float
    volume_24h: float
    change_24h_pct: float
    range_24h_pct: float
    spread_bps: float
    liquidity_score: float
    volatility_score: float
    momentum_score: float
    rank_score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "price": self.price,
            "volume_24h": round(self.volume_24h, 2),
            "change_24h_pct": round(self.change_24h_pct, 3),
            "range_24h_pct": round(self.range_24h_pct, 3),
            "spread_bps": round(self.spread_bps, 2),
            "rank_score": round(self.rank_score, 4),
            "parts": {
                "liquidity": round(self.liquidity_score, 3),
                "volatility": round(self.volatility_score, 3),
                "momentum": round(self.momentum_score, 3),
            },
        }


class UniverseSelector:
    def __init__(self, cfg: UniverseConfig, exchange: Exchange) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.candidates: list[Candidate] = []
        self.total_markets = 0
        self.filtered_out = 0
        self.last_refresh = 0.0
        self.last_error: str | None = None

    # ---------- veřejné ----------

    def symbols(self, limit: int | None = None) -> list[str]:
        """Aktuální špička žebříčku; případně ho nejdřív obnoví."""
        if self.needs_refresh():
            self.refresh()
        count = limit if limit is not None else self.cfg.deep_scan_count
        return [c.symbol for c in self.candidates[:count]]

    def needs_refresh(self) -> bool:
        if not self.candidates:
            return True
        return time.time() - self.last_refresh > self.cfg.refresh_minutes * 60

    def refresh(self) -> list[Candidate]:
        """Projde celou burzu a sestaví nový žebříček."""
        try:
            tradable = self._tradable_symbols()
            tickers = self.exchange.fetch_tickers(tradable or None)
        except Exception as exc:  # bez žebříčku se pracuje se starým seznamem
            self.last_error = str(exc)
            log.warning("Obnova seznamu trhů selhala: %s", exc)
            return self.candidates

        allowed = set(tradable)
        raw = [
            (symbol, ticker) for symbol, ticker in tickers.items()
            if not allowed or symbol in allowed
        ]
        self.total_markets = len(raw)

        measured = []
        for symbol, ticker in raw:
            row = self._metrics(symbol, ticker)
            if row is not None:
                measured.append(row)

        eligible = self._apply_filters(measured)
        self.filtered_out = self.total_markets - len(eligible)
        self.candidates = self._rank(eligible)
        self.last_refresh = time.time()
        self.last_error = None
        log.info(
            "Žebříček trhů: %d z %d prošlo filtrem, top 5: %s",
            len(self.candidates), self.total_markets,
            ", ".join(c.symbol for c in self.candidates[:5]),
        )
        return self.candidates

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "total_markets": self.total_markets,
            "filtered_out": self.filtered_out,
            "eligible": len(self.candidates),
            "deep_scan_count": self.cfg.deep_scan_count,
            "min_volume_24h": self.cfg.min_volume_24h,
            "last_refresh": self.last_refresh,
            "age_minutes": round((time.time() - self.last_refresh) / 60, 1)
            if self.last_refresh else None,
            "last_error": self.last_error,
            "ranking": [c.as_dict() for c in self.candidates[:60]],
        }

    # ---------- interní ----------

    def _tradable_symbols(self) -> list[str]:
        """Aktivní perpetual kontrakty v kotační měně, bez vyloučených vzorů."""
        symbols = self.exchange.list_symbols(quote=self.cfg.quote)
        return [s for s in symbols if not self._excluded(s)]

    def _excluded(self, symbol: str) -> bool:
        return any(pattern in symbol for pattern in self.cfg.exclude_patterns)

    def _metrics(self, symbol: str, ticker: dict[str, Any]) -> Candidate | None:
        price = _num(ticker.get("last")) or _num(ticker.get("close"))
        volume = _num(ticker.get("quoteVolume"))
        if volume is None:
            base_volume = _num(ticker.get("baseVolume"))
            volume = base_volume * price if base_volume and price else None
        if not price or price <= 0 or volume is None:
            return None

        bid, ask = _num(ticker.get("bid")), _num(ticker.get("ask"))
        spread_bps = 0.0
        if bid and ask and bid > 0:
            spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000

        high, low = _num(ticker.get("high")), _num(ticker.get("low"))
        range_pct = ((high - low) / price * 100) if high and low and price else 0.0
        change_pct = _num(ticker.get("percentage")) or 0.0

        return Candidate(
            symbol=symbol, price=price, volume_24h=volume, change_24h_pct=change_pct,
            range_24h_pct=range_pct, spread_bps=spread_bps,
            liquidity_score=0.0, volatility_score=0.0, momentum_score=0.0, rank_score=0.0,
        )

    def _apply_filters(self, rows: list[Candidate]) -> list[Candidate]:
        """Filtry na likviditu a spread, s ústupem když by nezbylo nic.

        Absolutní práh objemu je vždycky odhad kalibrovaný na jednu burzu.
        Když na jiné vyřadí celý trh, není správná odpověď „nic neobchoduj",
        ale „vezmi to nejlikvidnější, co tahle burza má".
        """
        needed = max(self.cfg.deep_scan_count, 1)
        passed = [
            row for row in rows
            if row.volume_24h >= self.cfg.min_volume_24h
            and row.spread_bps <= self.cfg.max_spread_bps
        ]
        if len(passed) >= needed or not self.cfg.adaptive_filters or not rows:
            return passed

        by_volume = sorted(rows, key=lambda r: r.volume_24h, reverse=True)
        relaxed = [r for r in by_volume if r.spread_bps <= self.cfg.max_spread_bps]
        if len(relaxed) >= needed:
            log.warning(
                "Práh objemu %.0f propustil jen %d trhů z %d — beru místo něj "
                "%d nejlikvidnějších (od %.0f výš). Trvalá změna: uprav "
                "universe.min_volume_24h v Nastavení.",
                self.cfg.min_volume_24h, len(passed), len(rows), needed,
                relaxed[needed - 1].volume_24h,
            )
            return relaxed[: needed * 2]

        log.warning(
            "Filtry objemu i spreadu propustily jen %d trhů z %d — beru "
            "%d nejlikvidnějších bez ohledu na spread. Zkontroluj "
            "universe.min_volume_24h a universe.max_spread_bps v Nastavení.",
            len(passed), len(rows), needed,
        )
        return by_volume[: needed * 2]

    def _rank(self, rows: list[Candidate]) -> list[Candidate]:
        """Složené skóre z likvidity, volatility a síly pohybu (vše 0..1)."""
        if not rows:
            return []
        # objem má obrovský rozptyl, proto logaritmicky
        log_volumes = [math.log10(max(r.volume_24h, 1.0)) for r in rows]
        ranges = [r.range_24h_pct for r in rows]
        moves = [abs(r.change_24h_pct) for r in rows]

        for row, log_volume, range_pct, move in zip(rows, log_volumes, ranges, moves, strict=True):
            row.liquidity_score = _normalize(log_volume, log_volumes)
            # ideál je střední volatilita: dost pohybu, ale ne chaos
            row.volatility_score = _bell(range_pct, ideal=4.0, width=4.0)
            row.momentum_score = _normalize(move, moves)
            row.rank_score = (
                self.cfg.weight_liquidity * row.liquidity_score
                + self.cfg.weight_volatility * row.volatility_score
                + self.cfg.weight_momentum * row.momentum_score
            )
        return sorted(rows, key=lambda r: r.rank_score, reverse=True)


def _normalize(value: float, population: list[float]) -> float:
    low, high = min(population), max(population)
    if high - low < 1e-9:
        return 0.5
    return (value - low) / (high - low)


def _bell(value: float, ideal: float, width: float) -> float:
    """1.0 v ideálním bodě, plynule klesá oběma směry."""
    return math.exp(-((value - ideal) ** 2) / (2 * width**2))


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
