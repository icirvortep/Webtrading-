"""Confluence scoring — filtr kvality signálu z TradingView.

TradingView říká *kdy*; tenhle modul rozhoduje, jestli to stojí za riziko
a jak velké. Skóre 0..1 vzniká z vážených faktorů; pod prahem se signál
zahodí, nad prahem se lineárně promítne do násobku rizika.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import StrategyConfig
from ..models import MarketSnapshot, Regime, Side


@dataclass(slots=True)
class Score:
    value: float
    reasons: list[str]
    veto: str | None = None

    @property
    def ok(self) -> bool:
        return self.veto is None


# váha musí dávat v součtu 1.0
_WEIGHTS = {
    "signal_confidence": 0.20,
    "regime_fit": 0.25,
    "htf_alignment": 0.20,
    "momentum": 0.15,
    "volatility_fit": 0.10,
    "participation": 0.10,
}


def evaluate(
    side: Side,
    snap: MarketSnapshot,
    cfg: StrategyConfig,
    signal_confidence: float = 0.5,
    regime_bias: float = 1.0,
) -> Score:
    """Vrátí skóre 0..1 a seznam důvodů; `veto` znamená tvrdé zamítnutí."""
    reasons: list[str] = []
    parts: dict[str, float] = {}
    long = side is Side.LONG

    parts["signal_confidence"] = _clamp(signal_confidence)

    # 1) Sedí směr obchodu k režimu?
    if snap.regime is Regime.TREND_UP:
        fit = 1.0 if long else 0.15
    elif snap.regime is Regime.TREND_DOWN:
        fit = 0.15 if long else 1.0
    elif snap.regime is Regime.RANGE:
        # v rangi je lepší nakupovat u spodní hrany a prodávat u horní
        fit = 0.85 if (long and snap.rsi < 45) or (not long and snap.rsi > 55) else 0.45
    elif snap.regime is Regime.VOLATILE:
        fit = 0.5
    else:                                    # QUIET
        fit = 0.4
    parts["regime_fit"] = fit
    reasons.append(f"režim={snap.regime.value} fit={fit:.2f}")

    # 2) Shoda s vyšším timeframem
    if snap.htf_trend == 0:
        parts["htf_alignment"] = 0.5
    elif (snap.htf_trend > 0) == long:
        parts["htf_alignment"] = 1.0
        reasons.append("HTF potvrzuje směr")
    else:
        parts["htf_alignment"] = 0.1
        reasons.append("HTF jde proti signálu")

    # 3) Momentum: EMA struktura + RSI bez extrému
    ema_ok = (snap.ema_fast > snap.ema_slow) == long
    rsi_ok = (35 <= snap.rsi <= 72) if long else (28 <= snap.rsi <= 65)
    parts["momentum"] = 0.5 * float(ema_ok) + 0.5 * float(rsi_ok)

    # 4) Volatilita: příliš mrtvo i příliš divoko snižuje skóre
    if snap.atr_pct <= cfg.quiet_atr_pct:
        parts["volatility_fit"] = 0.25
        reasons.append("nízká volatilita")
    elif snap.atr_pct >= cfg.volatile_atr_pct * 1.5:
        parts["volatility_fit"] = 0.3
        reasons.append("extrémní volatilita")
    else:
        parts["volatility_fit"] = 1.0

    # 5) Účast trhu (objem)
    parts["participation"] = _clamp(0.5 + snap.volume_z / 4.0)

    value = sum(_WEIGHTS[k] * v for k, v in parts.items())
    value = _clamp(value * regime_bias)

    veto = _hard_vetoes(side, snap, cfg)
    reasons.append(f"skóre={value:.3f}")
    return Score(value=round(value, 4), reasons=reasons, veto=veto)


def _hard_vetoes(side: Side, snap: MarketSnapshot, cfg: StrategyConfig) -> str | None:
    long = side is Side.LONG
    if cfg.veto_counter_trend and snap.regime.is_trend and snap.trend_strength >= 0.7:
        against = (snap.regime is Regime.TREND_UP and not long) or (
            snap.regime is Regime.TREND_DOWN and long
        )
        if against:
            return "protitrendový vstup v silném trendu"
    if long and snap.rsi >= 85:
        return "RSI přehřáté (>=85) pro long"
    if not long and snap.rsi <= 15:
        return "RSI přeprodané (<=15) pro short"
    if snap.atr <= 0:
        return "ATR není k dispozici"
    return None


def risk_multiplier_from_score(score: float, cfg: StrategyConfig) -> float:
    """Skóre → násobek rizika. Na prahu 0.6x, při skóre 1.0 plných 1.0x."""
    if score <= cfg.min_score:
        return 0.0
    span = max(1.0 - cfg.min_score, 1e-6)
    return round(0.6 + 0.4 * (score - cfg.min_score) / span, 4)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
