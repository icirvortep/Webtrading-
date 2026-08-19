"""Detekce tržního režimu a výpočet snapshotu trhu.

Režim je jádro adaptace: podle něj se mění SL/TP profil, agresivita
vstupů i násobek rizika. Klasifikace kombinuje sílu trendu (ADX + shoda
EMA + sklon), volatilitu (ATR v % ceny) a kontext vyššího timeframu.
"""
from __future__ import annotations

import numpy as np

from ..config import StrategyConfig
from ..models import MarketSnapshot, Regime
from . import indicators as ta

OHLCV = list[list[float]]   # [ts, open, high, low, close, volume]


def _columns(ohlcv: OHLCV) -> dict[str, np.ndarray]:
    arr = np.asarray(ohlcv, dtype=float)
    if arr.ndim != 2 or arr.shape[1] < 6:
        raise ValueError("OHLCV musí být pole [ts, o, h, l, c, v]")
    return {
        "ts": arr[:, 0], "open": arr[:, 1], "high": arr[:, 2],
        "low": arr[:, 3], "close": arr[:, 4], "volume": arr[:, 5],
    }


def htf_bias(ohlcv: OHLCV, cfg: StrategyConfig) -> int:
    """Směr vyššího timeframu: +1 long bias, -1 short bias, 0 neutrál."""
    if not ohlcv or len(ohlcv) < cfg.ema_slow + 2:
        return 0
    cols = _columns(ohlcv)
    close = cols["close"]
    fast = ta.last_valid(ta.ema(close, cfg.ema_fast))
    slow = ta.last_valid(ta.ema(close, cfg.ema_slow))
    if np.isnan(fast) or np.isnan(slow):
        return 0
    slope = ta.slope_pct(close, min(10, close.size))
    if fast > slow and slope > 0:
        return 1
    if fast < slow and slope < 0:
        return -1
    return 0


def classify(
    ohlcv: OHLCV,
    cfg: StrategyConfig,
    htf_ohlcv: OHLCV | None = None,
    symbol: str = "",
    timeframe: str = "",
    funding_rate: float = 0.0,
    spread_bps: float = 0.0,
) -> MarketSnapshot:
    """Spočítá metriky a určí režim trhu."""
    cols = _columns(ohlcv)
    close, high, low, volume = cols["close"], cols["high"], cols["low"], cols["volume"]
    price = float(close[-1])

    atr_val = ta.last_valid(ta.atr(high, low, close, cfg.atr_period), default=0.0)
    atr_pct = (atr_val / price * 100.0) if price else 0.0
    adx_arr, plus_di, minus_di = ta.adx(high, low, close, cfg.adx_period)
    adx_val = ta.last_valid(adx_arr, default=0.0)
    di_plus = ta.last_valid(plus_di, default=0.0)
    di_minus = ta.last_valid(minus_di, default=0.0)
    ema_f = ta.last_valid(ta.ema(close, cfg.ema_fast), default=price)
    ema_s = ta.last_valid(ta.ema(close, cfg.ema_slow), default=price)
    rsi_val = ta.last_valid(ta.rsi(close, cfg.rsi_period), default=50.0)
    width = ta.last_valid(ta.bb_width(close, cfg.bb_period), default=0.0)
    vol_z = ta.last_valid(ta.zscore(volume, 20), default=0.0)
    rvol = ta.realized_vol(close, min(30, close.size - 1))
    if np.isnan(rvol):
        rvol = 0.0
    htf = htf_bias(htf_ohlcv, cfg) if htf_ohlcv else 0

    ema_up = ema_f > ema_s
    slope = ta.slope_pct(close, 10)
    # síla trendu 0..1: ADX škálovaný do [0,1] + bonus za shodu EMA a HTF
    adx_component = min(max((adx_val - 10.0) / 30.0, 0.0), 1.0)
    align = 0.0
    if (ema_up and slope > 0) or (not ema_up and slope < 0):
        align += 0.5
    if htf != 0 and ((htf > 0 and ema_up) or (htf < 0 and not ema_up)):
        align += 0.5
    trend_strength = round(min(0.6 * adx_component + 0.4 * align, 1.0), 4)

    regime = _pick_regime(cfg, adx_val, atr_pct, ema_up, di_plus, di_minus, trend_strength)

    return MarketSnapshot(
        symbol=symbol, timeframe=timeframe, price=price, atr=atr_val, atr_pct=atr_pct,
        adx=adx_val, ema_fast=ema_f, ema_slow=ema_s, rsi=rsi_val, bb_width=width,
        volume_z=vol_z, realized_vol=rvol, htf_trend=htf, regime=regime,
        trend_strength=trend_strength, funding_rate=funding_rate, spread_bps=spread_bps,
    )


def _pick_regime(
    cfg: StrategyConfig,
    adx_val: float,
    atr_pct: float,
    ema_up: bool,
    di_plus: float,
    di_minus: float,
    trend_strength: float,
) -> Regime:
    # Extrémní volatilita přebíjí vše — jiné SL i menší size.
    if atr_pct >= cfg.volatile_atr_pct:
        return Regime.VOLATILE
    if adx_val >= cfg.adx_trend_threshold and trend_strength >= 0.5:
        if ema_up and di_plus >= di_minus:
            return Regime.TREND_UP
        if not ema_up and di_minus >= di_plus:
            return Regime.TREND_DOWN
        return Regime.RANGE
    if atr_pct <= cfg.quiet_atr_pct:
        return Regime.QUIET
    if adx_val <= cfg.adx_range_threshold:
        return Regime.RANGE
    return Regime.RANGE
