"""Risk manager — jediné místo, které rozhoduje o velikosti pozice a páce.

Klíčové pravidlo: na jeden obchod se riskuje pevné % equity (výchozí 2 %).
Velikost pozice se dopočítá ze vzdálenosti ke stop lossu:

    množství = (equity * riziko%) / |vstup - SL|

Páka pak *není* volba agresivity, ale důsledek: jen zajišťuje, aby na
pozici stačila marže. Vyšší páka tedy sama o sobě nezvyšuje ztrátu při
zásahu SL — zvyšuje jen kapitálovou efektivitu (a blízkost likvidace,
kterou hlídáme zvlášť).
"""
from __future__ import annotations

import logging
import math
import time

from ..config import AppConfig
from ..models import (
    Balance,
    Decision,
    MarketSnapshot,
    RejectReason,
    Side,
    Signal,
    TradePlan,
)
from ..state.store import Store
from ..strategy import exits, scoring

log = logging.getLogger(__name__)

#: minimální odstup likvidační ceny od stop lossu (v násobcích SL vzdálenosti)
LIQUIDATION_BUFFER = 1.6


class RiskManager:
    def __init__(self, cfg: AppConfig, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    # ---------- vstupní brány ----------

    def pretrade_checks(self, signal: Signal, balance: Balance, open_symbols: list[str]) -> Decision | None:
        """Tvrdé limity, které se vyhodnocují ještě před analýzou trhu."""
        risk = self.cfg.risk

        if risk.kill_switch:
            return _reject(RejectReason.KILL_SWITCH, "kill switch je aktivní")

        age = time.time() - signal.received_at
        if age > risk.signal_max_age_sec:
            return _reject(RejectReason.STALE, f"signál starý {age:.0f}s")

        if not self._symbol_allowed(signal.symbol):
            return _reject(RejectReason.SYMBOL_BLOCKED, f"{signal.symbol} není povolen")

        if balance.equity <= 0:
            return _reject(RejectReason.NO_EQUITY, "nulová equity")

        sod_equity = self.store.start_of_day_equity(balance.equity)
        drawdown_pct = (sod_equity - balance.equity) / sod_equity * 100.0 if sod_equity else 0.0
        if drawdown_pct >= risk.max_daily_loss_pct:
            return _reject(
                RejectReason.DAILY_LOSS_LIMIT,
                f"denní ztráta {drawdown_pct:.2f}% >= limit {risk.max_daily_loss_pct}%",
            )

        if self.store.daily_trade_count() >= risk.max_daily_trades:
            return _reject(RejectReason.DAILY_LOSS_LIMIT, "vyčerpán denní počet obchodů")

        if len(open_symbols) >= risk.max_open_positions:
            return _reject(RejectReason.MAX_POSITIONS, f"otevřeno už {len(open_symbols)} pozic")

        if open_symbols.count(signal.symbol) >= risk.max_positions_per_symbol:
            return _reject(RejectReason.DUPLICATE, f"pozice na {signal.symbol} už existuje")

        cooldown = self._cooldown_remaining(signal.symbol)
        if cooldown > 0:
            return _reject(RejectReason.COOLDOWN, f"cooldown ještě {cooldown / 60:.1f} min")

        return None

    def _symbol_allowed(self, symbol: str) -> bool:
        if symbol in self.cfg.symbols_blocklist:
            return False
        allow = self.cfg.symbols_allowlist
        return not allow or symbol in allow

    def _cooldown_remaining(self, symbol: str) -> float:
        risk = self.cfg.risk
        now = time.time()
        streak = self.store.loss_streak()
        if streak >= risk.cooldown_after_streak:
            last = self.store.last_loss_time()
            if last and now - last < risk.streak_cooldown_min * 60:
                return risk.streak_cooldown_min * 60 - (now - last)
        last_symbol_loss = self.store.last_loss_time(symbol)
        if last_symbol_loss and now - last_symbol_loss < risk.cooldown_after_loss_min * 60:
            return risk.cooldown_after_loss_min * 60 - (now - last_symbol_loss)
        return 0.0

    # ---------- adaptace rizika ----------

    def adaptive_risk_pct(self, snap: MarketSnapshot, score: float) -> tuple[float, list[str]]:
        """Základní riziko upravené o kvalitu signálu, režim a historickou úspěšnost."""
        risk = self.cfg.risk
        strat = self.cfg.strategy
        reasons: list[str] = []
        base = risk.risk_per_trade_pct

        score_mult = scoring.risk_multiplier_from_score(score, strat)
        reasons.append(f"skóre×{score_mult:.2f}")

        # volatilní trh => menší size, klidný trend => plná
        vol_mult = 1.0
        if snap.atr_pct >= strat.volatile_atr_pct:
            vol_mult = 0.6
            reasons.append("volatilita×0.60")
        elif snap.atr_pct <= strat.quiet_atr_pct:
            vol_mult = 0.75
            reasons.append("nízká volatilita×0.75")

        learn_mult = 1.0
        if strat.adaptive_learning:
            stats = self.store.regime_stats(snap.regime.value)
            if stats["trades"] >= strat.learning_min_trades:
                # expektance v R mapovaná na násobek: -0.3R → min, +0.5R → max
                expectancy = stats["expectancy_r"]
                span = strat.learning_max_multiplier - strat.learning_min_multiplier
                learn_mult = strat.learning_min_multiplier + span * _clamp((expectancy + 0.3) / 0.8)
                learn_mult = round(learn_mult, 3)
                reasons.append(
                    f"učení×{learn_mult:.2f} (n={stats['trades']}, E={expectancy:+.2f}R)"
                )

        # snížení rizika při sérii ztrát
        streak = self.store.loss_streak()
        streak_mult = 1.0 if streak < 2 else max(0.5, 1.0 - 0.25 * (streak - 1))
        if streak_mult < 1.0:
            reasons.append(f"série ztrát×{streak_mult:.2f}")

        final = base * score_mult * vol_mult * learn_mult * streak_mult
        final = min(final, risk.max_risk_per_trade_pct)
        reasons.append(f"riziko={final:.2f}% (základ {base:.2f}%)")
        return round(final, 4), reasons

    # ---------- sestavení plánu ----------

    def build_plan(
        self,
        signal: Signal,
        side: Side,
        snap: MarketSnapshot,
        balance: Balance,
        score: scoring.Score,
        limits: dict[str, float],
        highs: list[float] | None = None,
        lows: list[float] | None = None,
    ) -> Decision:
        """Z signálu + snapshotu trhu složí kompletní, proveditelný plán obchodu."""
        strat, risk_cfg, exit_cfg = self.cfg.strategy, self.cfg.risk, self.cfg.exits

        if not score.ok:
            return _reject(RejectReason.LOW_SCORE, score.veto or "veto")
        if score.value < strat.min_score:
            return _reject(
                RejectReason.LOW_SCORE,
                f"skóre {score.value:.3f} < práh {strat.min_score}",
            )
        if snap.spread_bps > risk_cfg.max_spread_bps:
            return _reject(
                RejectReason.SPREAD, f"spread {snap.spread_bps:.1f} bps > {risk_cfg.max_spread_bps}"
            )

        entry = signal.price or snap.price
        swing = exits.swing_extreme(highs or [], lows or [], side) if highs and lows else None
        stop = exits.build_stop(side, entry, snap, exit_cfg, sl_hint=signal.sl_hint, swing_level=swing)
        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            return _reject(RejectReason.SIZE_TOO_SMALL, "nulová vzdálenost k SL")

        risk_pct, risk_reasons = self.adaptive_risk_pct(snap, score.value)
        risk_pct *= max(signal.risk_multiplier, 0.0)
        risk_pct = min(risk_pct, risk_cfg.max_risk_per_trade_pct)
        if risk_pct <= 0:
            return _reject(RejectReason.LOW_SCORE, "adaptivní riziko vyšlo nulové")

        # zbývající prostor v portfoliovém riziku
        max_portfolio_risk = balance.equity * risk_cfg.max_portfolio_risk_pct / 100.0
        remaining = max_portfolio_risk - self.store.open_risk_total()
        risk_amount = balance.equity * risk_pct / 100.0
        if remaining <= 0:
            return _reject(RejectReason.MAX_POSITIONS, "vyčerpáno portfoliové riziko")
        if risk_amount > remaining:
            risk_amount = remaining
            risk_reasons.append(f"oříznuto portfoliovým limitem na {risk_amount:.2f}")

        quantity = risk_amount / stop_distance
        contract_size = limits.get("contract_size", 1.0) or 1.0
        if contract_size != 1.0:
            quantity /= contract_size

        notional = quantity * entry * contract_size
        max_notional = balance.equity * risk_cfg.max_notional_pct_of_equity / 100.0
        if notional > max_notional:
            scale = max_notional / notional
            quantity *= scale
            notional *= scale
            risk_amount *= scale
            risk_reasons.append(f"oříznuto stropem notionalu na {notional:.2f}")

        leverage = self._required_leverage(notional, balance, limits)
        liq_ok, liq_note = self._liquidation_check(side, entry, stop, leverage)
        if not liq_ok:
            leverage = max(risk_cfg.min_leverage, self._safe_leverage(side, entry, stop))
            risk_reasons.append(f"páka snížena kvůli likvidaci na {leverage}x")
        elif liq_note:
            risk_reasons.append(liq_note)

        min_amount = limits.get("min_amount", 0.0)
        min_cost = max(limits.get("min_cost", 0.0), risk_cfg.min_notional_usd)
        if quantity < min_amount or notional < min_cost:
            return _reject(
                RejectReason.SIZE_TOO_SMALL,
                f"množství {quantity:.8f} / notional {notional:.2f} pod minimem burzy",
            )

        take_profits = exits.build_take_profits(side, entry, stop, snap, exit_cfg)

        plan = TradePlan(
            signal_id=signal.id, symbol=signal.symbol, side=side, entry=entry,
            stop_loss=stop, take_profits=take_profits, quantity=quantity,
            notional=notional, leverage=leverage, risk_amount=risk_amount,
            risk_pct=risk_pct, regime=snap.regime, score=score.value,
            reasons=[*score.reasons, *risk_reasons],
            breakeven_after_tp=exit_cfg.breakeven_after_tp,
            trail_after_tp=exit_cfg.trail_after_tp,
            trail_atr_mult=exit_cfg.trail_atr_mult.get(snap.regime.value, 2.5),
            max_hold_minutes=exit_cfg.max_hold_minutes,
            atr=snap.atr,
        )
        return Decision(accepted=True, plan=plan)

    # ---------- páka ----------

    def _required_leverage(self, notional: float, balance: Balance, limits: dict[str, float]) -> int:
        """Nejnižší páka, se kterou se pozice vejde do volné marže."""
        risk_cfg = self.cfg.risk
        usable = max(balance.free, 1e-9) * 0.9        # 10% rezerva na poplatky a výkyvy
        needed = math.ceil(notional / usable) if usable > 0 else risk_cfg.max_leverage
        venue_max = int(limits.get("max_leverage", risk_cfg.max_leverage))
        leverage = max(risk_cfg.min_leverage, needed)
        return int(min(leverage, risk_cfg.max_leverage, venue_max))

    def _safe_leverage(self, side: Side, entry: float, stop: float) -> int:
        """Nejvyšší páka, u níž je likvidace bezpečně za stop lossem."""
        stop_pct = abs(entry - stop) / entry
        if stop_pct <= 0:
            return self.cfg.risk.min_leverage
        safe = 1.0 / (stop_pct * LIQUIDATION_BUFFER)
        return max(self.cfg.risk.min_leverage, min(int(safe), self.cfg.risk.max_leverage))

    def _liquidation_check(self, side: Side, entry: float, stop: float, leverage: int) -> tuple[bool, str]:
        """Odhad: při izolované marži je likvidace zhruba 1/páka od vstupu."""
        if leverage <= 1:
            return True, ""
        liq_distance_pct = 1.0 / leverage
        stop_distance_pct = abs(entry - stop) / entry
        if stop_distance_pct * LIQUIDATION_BUFFER >= liq_distance_pct:
            return False, ""
        return True, f"likvidace ~{liq_distance_pct * 100:.2f}% vs SL {stop_distance_pct * 100:.2f}%"


def _reject(reason: RejectReason, detail: str) -> Decision:
    log.info("Signál zamítnut (%s): %s", reason.value, detail)
    return Decision(accepted=False, reason=reason, detail=detail)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
