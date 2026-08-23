"""Vlastní generátor vstupních signálů — Python protějšek Pine skriptu.

Díky němu bot nepotřebuje TradingView: stejné spouštěče (pullback v trendu,
mean-reversion v rangi, průraz po kompresi volatility) se počítají lokálně
z dat burzy. Skener je volá na každém sledovaném symbolu.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import StrategyConfig
from ..models import MarketSnapshot, Regime, Side
from . import indicators as ta


@dataclass(slots=True)
class Trigger:
    """Jeden spouštěč vstupu — co se stalo a jakým směrem."""

    side: Side
    kind: str                 # pullback | mean_reversion | breakout
    description: str
    strength: float           # 0..1, hrubý odhad kvality spouštěče

    def as_dict(self) -> dict:
        return {"side": self.side.value, "kind": self.kind,
                "description": self.description, "strength": round(self.strength, 3)}


def detect(ohlcv: list[list[float]], snap: MarketSnapshot, cfg: StrategyConfig) -> list[Trigger]:
    """Najde všechny spouštěče na poslední uzavřené svíčce."""
    arr = np.asarray(ohlcv, dtype=float)
    if arr.shape[0] < max(cfg.ema_slow, 60) + 5:
        return []

    high, low, close, volume = arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
    ema_fast = ta.ema(close, cfg.ema_fast)
    ema_trend = ta.ema(close, 200) if close.size > 200 else ta.ema(close, cfg.ema_slow)
    upper, _, lower = ta.bollinger(close, cfg.bb_period)
    width = ta.bb_width(close, cfg.bb_period)
    width_avg = ta.sma(width[~np.isnan(width)], 50) if np.sum(~np.isnan(width)) > 50 else None
    vol_z = ta.last_valid(ta.zscore(volume, 20), default=0.0)

    triggers: list[Trigger] = []
    price = float(close[-1])
    fast = ta.last_valid(ema_fast, default=price)
    trend_line = ta.last_valid(ema_trend, default=price)

    # --- 1) Pullback k rychlé EMA ve směru trendu -------------------------
    if snap.regime is Regime.TREND_UP and low[-1] <= fast < price and price > trend_line:
        triggers.append(Trigger(
            Side.LONG, "pullback",
            f"korekce na EMA{cfg.ema_fast} ({fast:.4f}) a odraz v uptrendu",
            strength=min(0.6 + snap.trend_strength * 0.4, 1.0),
        ))
    if snap.regime is Regime.TREND_DOWN and high[-1] >= fast > price and price < trend_line:
        triggers.append(Trigger(
            Side.SHORT, "pullback",
            f"korekce na EMA{cfg.ema_fast} ({fast:.4f}) a odraz v downtrendu",
            strength=min(0.6 + snap.trend_strength * 0.4, 1.0),
        ))

    # --- 2) Mean reversion od krajů Bollingera v rangi --------------------
    if snap.regime is Regime.RANGE:
        band_low = ta.last_valid(lower, default=price)
        band_high = ta.last_valid(upper, default=price)
        if price <= band_low and snap.rsi < 38 and close[-1] > close[-2]:
            triggers.append(Trigger(
                Side.LONG, "mean_reversion",
                f"dotek spodního Bollingera ({band_low:.4f}) při RSI {snap.rsi:.0f}",
                strength=0.55 + (38 - snap.rsi) / 100,
            ))
        if price >= band_high and snap.rsi > 62 and close[-1] < close[-2]:
            triggers.append(Trigger(
                Side.SHORT, "mean_reversion",
                f"dotek horního Bollingera ({band_high:.4f}) při RSI {snap.rsi:.0f}",
                strength=0.55 + (snap.rsi - 62) / 100,
            ))

    # --- 3) Průraz po kompresi volatility --------------------------------
    if width_avg is not None:
        recent_width = ta.last_valid(width, default=0.0)
        avg_width = ta.last_valid(width_avg, default=recent_width)
        squeezed = avg_width > 0 and recent_width < avg_width * 0.7
        if squeezed and vol_z > 0.5:
            prior_high = float(high[-21:-1].max())
            prior_low = float(low[-21:-1].min())
            if price > prior_high:
                triggers.append(Trigger(
                    Side.LONG, "breakout",
                    f"průraz 20-barového maxima ({prior_high:.4f}) po kompresi",
                    strength=min(0.6 + vol_z / 5, 1.0),
                ))
            elif price < prior_low:
                triggers.append(Trigger(
                    Side.SHORT, "breakout",
                    f"průraz 20-barového minima ({prior_low:.4f}) po kompresi",
                    strength=min(0.6 + vol_z / 5, 1.0),
                ))

    return triggers
