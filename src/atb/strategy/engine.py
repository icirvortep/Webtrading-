"""Adaptivní strategický engine — spojuje data, režim, skóre a plán obchodu.

Tok: signál z TradingView → stažení OHLCV (signální + vyšší TF) → detekce
režimu → confluence skóre → risk manager sestaví plán s SL/TP a velikostí.
"""
from __future__ import annotations

import logging
import re

from ..config import AppConfig
from ..exchanges.base import Exchange
from ..models import Balance, Decision, MarketSnapshot, RejectReason, Side, Signal
from ..risk.manager import RiskManager
from . import regime as regime_mod
from . import scoring

log = logging.getLogger(__name__)

_TF_UNITS = {"m": 1, "h": 60, "d": 1440, "w": 10080}
_TF_LADDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "3d", "1w"]


def timeframe_minutes(timeframe: str) -> int:
    match = re.fullmatch(r"(\d+)\s*([mhdwMHDW])", timeframe.strip())
    if not match:
        return 15
    value, unit = int(match.group(1)), match.group(2).lower()
    return value * _TF_UNITS.get(unit, 1)


def higher_timeframe(timeframe: str, multiplier: int = 4) -> str:
    """Nejbližší standardní TF, který je alespoň `multiplier`× vyšší."""
    target = timeframe_minutes(timeframe) * max(multiplier, 2)
    for candidate in _TF_LADDER:
        if timeframe_minutes(candidate) >= target:
            return candidate
    return _TF_LADDER[-1]


class StrategyEngine:
    def __init__(self, cfg: AppConfig, exchange: Exchange, risk: RiskManager) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.risk = risk

    def analyze(self, symbol: str, timeframe: str) -> MarketSnapshot:
        """Stáhne data a vrátí kompletní snapshot trhu (bez ohledu na signál)."""
        strat = self.cfg.strategy
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=strat.ohlcv_limit)
        if not ohlcv or len(ohlcv) < 60:
            raise ValueError(f"Nedostatek dat pro {symbol} {timeframe} ({len(ohlcv or [])} barů)")
        htf = higher_timeframe(timeframe, strat.htf_multiplier)
        try:
            htf_ohlcv = self.exchange.fetch_ohlcv(symbol, htf, limit=strat.ohlcv_limit)
        except Exception as exc:
            log.warning("HTF data %s %s nedostupná: %s", symbol, htf, exc)
            htf_ohlcv = None

        funding = self.exchange.fetch_funding_rate(symbol)
        try:
            spread = self.exchange.spread_bps(symbol)
        except Exception:
            spread = 0.0

        return regime_mod.classify(
            ohlcv, strat, htf_ohlcv=htf_ohlcv, symbol=symbol, timeframe=timeframe,
            funding_rate=funding, spread_bps=spread,
        )

    def decide(self, signal: Signal, balance: Balance, open_symbols: list[str]) -> Decision:
        """Kompletní rozhodnutí o vstupu — vrací plán, nebo důvod zamítnutí."""
        gate = self.risk.pretrade_checks(signal, balance, open_symbols)
        if gate is not None:
            return gate

        side = signal.side
        if side is None:
            return Decision(
                accepted=False, reason=RejectReason.LOW_SCORE, detail="signál neurčuje směr"
            )
        if side is Side.SHORT and not self.exchange.can_short:
            return Decision(
                accepted=False, reason=RejectReason.SYMBOL_BLOCKED,
                detail="spotový účet neumí short — prodat jde jen to, co vlastníš",
            )

        try:
            snap = self.analyze(signal.symbol, signal.timeframe)
        except Exception as exc:  # bez dat se neobchoduje, ať je příčina jakákoli
            log.error("Analýza %s selhala: %s", signal.symbol, exc)
            return Decision(accepted=False, reason=RejectReason.LOW_SCORE, detail=str(exc))

        score = scoring.evaluate(side, snap, self.cfg.strategy, signal.confidence)
        log.info(
            "%s %s | režim=%s ATR%%=%.2f ADX=%.1f RSI=%.1f HTF=%+d → skóre %.3f%s",
            signal.symbol, side.value, snap.regime.value, snap.atr_pct, snap.adx,
            snap.rsi, snap.htf_trend, score.value,
            f" VETO: {score.veto}" if score.veto else "",
        )

        ohlcv = self.exchange.fetch_ohlcv(
            signal.symbol, signal.timeframe, limit=min(self.cfg.strategy.ohlcv_limit, 120)
        )
        highs = [row[2] for row in ohlcv]
        lows = [row[3] for row in ohlcv]
        limits = self.exchange.market_limits(signal.symbol)

        decision = self.risk.build_plan(
            signal, side, snap, balance, score, limits, highs=highs, lows=lows
        )
        if decision.accepted and decision.plan:
            plan = decision.plan
            log.info(
                "PLÁN %s %s: vstup %.6f SL %.6f (%.2f%%) qty %.8f páka %sx riziko %.2f USD (%.2f%%)",
                plan.symbol, plan.side.value, plan.entry, plan.stop_loss,
                plan.stop_distance / plan.entry * 100.0, plan.quantity, plan.leverage,
                plan.risk_amount, plan.risk_pct,
            )
        return decision

    def snapshot_for_side(self, symbol: str, timeframe: str, side: Side) -> tuple[MarketSnapshot, scoring.Score]:
        """Pomocník pro CLI příkaz `analyze` — snapshot + skóre bez obchodování."""
        snap = self.analyze(symbol, timeframe)
        return snap, scoring.evaluate(side, snap, self.cfg.strategy)
