"""Technické indikátory postavené jen na numpy — bez TA-Lib závislosti.

Vstupem jsou vždy 1D numpy pole (nebo seznamy) v chronologickém pořadí.
Návratem je pole stejné délky s NaN na místech, kde indikátor ještě není
definovaný, takže výsledky jde bezpečně indexovat od konce ([-1]).
"""
from __future__ import annotations

import numpy as np

Array = np.ndarray


def _as_array(values) -> Array:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        raise ValueError("očekáváno 1D pole")
    return arr


def ema(values, period: int) -> Array:
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if arr.size < period or period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = arr[:period].mean()
    for i in range(period, arr.size):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def sma(values, period: int) -> Array:
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if arr.size < period or period <= 0:
        return out
    cumsum = np.cumsum(np.insert(arr, 0, 0.0))
    out[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return out


def rma(values, period: int) -> Array:
    """Wilderovo vyhlazení (používá RSI, ATR, ADX)."""
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if arr.size < period or period <= 0:
        return out
    out[period - 1] = arr[:period].mean()
    for i in range(period, arr.size):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return out


def true_range(high, low, close) -> Array:
    h, lo, c = _as_array(high), _as_array(low), _as_array(close)
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    return np.maximum(h - lo, np.maximum(np.abs(h - prev_close), np.abs(lo - prev_close)))


def atr(high, low, close, period: int = 14) -> Array:
    return rma(true_range(high, low, close), period)


def rsi(values, period: int = 14) -> Array:
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    if arr.size <= period:
        return out
    delta = np.diff(arr, prepend=arr[0])
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = rma(gains, period)
    avg_loss = rma(losses, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        out = 100.0 - (100.0 / (1.0 + rs))
    out[np.isnan(avg_gain)] = np.nan
    return out


def adx(high, low, close, period: int = 14) -> tuple[Array, Array, Array]:
    """Vrací (adx, +di, -di)."""
    h, lo = _as_array(high), _as_array(low)
    size = h.size
    nan = np.full(size, np.nan)
    if size <= period * 2:
        return nan, nan.copy(), nan.copy()

    up_move = np.diff(h, prepend=h[0])
    down_move = -np.diff(lo, prepend=lo[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = rma(true_range(high, low, close), period)
    plus_smooth = rma(plus_dm, period)
    minus_smooth = rma(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_smooth / tr_smooth
        minus_di = 100.0 * minus_smooth / tr_smooth
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    dx = np.nan_to_num(dx, nan=np.nan, posinf=np.nan, neginf=np.nan)

    adx_vals = np.full(size, np.nan)
    valid = np.where(~np.isnan(dx))[0]
    if valid.size >= period:
        start = valid[period - 1]
        adx_vals[start] = np.nanmean(dx[valid[0]:start + 1])
        for i in range(start + 1, size):
            if np.isnan(dx[i]):
                continue
            adx_vals[i] = (adx_vals[i - 1] * (period - 1) + dx[i]) / period
    return adx_vals, plus_di, minus_di


def bollinger(values, period: int = 20, mult: float = 2.0) -> tuple[Array, Array, Array]:
    arr = _as_array(values)
    mid = sma(arr, period)
    std = np.full(arr.shape, np.nan)
    for i in range(period - 1, arr.size):
        std[i] = arr[i - period + 1:i + 1].std(ddof=0)
    return mid + mult * std, mid, mid - mult * std


def bb_width(values, period: int = 20, mult: float = 2.0) -> Array:
    upper, mid, lower = bollinger(values, period, mult)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (upper - lower) / mid * 100.0


def zscore(values, period: int = 20) -> Array:
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan)
    for i in range(period - 1, arr.size):
        window = arr[i - period + 1:i + 1]
        std = window.std(ddof=0)
        out[i] = 0.0 if std == 0 else (arr[i] - window.mean()) / std
    return out


def realized_vol(close, period: int = 30, annualize: bool = False) -> float:
    arr = _as_array(close)
    if arr.size < period + 1:
        return float("nan")
    rets = np.diff(np.log(arr[-(period + 1):]))
    vol = float(rets.std(ddof=1))
    return vol * np.sqrt(365 * 24) if annualize else vol


def slope_pct(values, period: int = 10) -> float:
    """Sklon lineární regrese normalizovaný na % za bar."""
    arr = _as_array(values)
    if arr.size < period:
        return 0.0
    window = arr[-period:]
    x = np.arange(period, dtype=float)
    slope = np.polyfit(x, window, 1)[0]
    base = window.mean()
    return 0.0 if base == 0 else float(slope / base * 100.0)


def last_valid(arr: Array, default: float = float("nan")) -> float:
    """Poslední hodnota, která není NaN."""
    valid = arr[~np.isnan(arr)]
    return float(valid[-1]) if valid.size else default
