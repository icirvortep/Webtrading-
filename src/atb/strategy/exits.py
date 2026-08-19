"""Výpočet SL, TP ladderu a parametrů trailingu podle režimu trhu.

Princip: SL se odvozuje od ATR (ne od pevných procent), takže se sám
přizpůsobí volatilitě daného trhu. TP jsou násobky R (= vzdálenost k SL),
takže poměr risk/reward zůstává konzistentní napříč instrumenty.
"""
from __future__ import annotations

from ..config import ExitConfig
from ..models import MarketSnapshot, Side, TakeProfit


def build_stop(
    side: Side,
    entry: float,
    snap: MarketSnapshot,
    cfg: ExitConfig,
    sl_hint: float | None = None,
    swing_level: float | None = None,
) -> float:
    """SL: explicitní hint > swing level > ATR násobek, vždy s clampem."""
    if sl_hint is not None and _valid_stop(side, entry, sl_hint):
        return _clamp_stop(side, entry, sl_hint, cfg)

    mult = cfg.sl_atr_mult.get(snap.regime.value, 2.0)
    distance = max(snap.atr * mult, entry * cfg.min_sl_pct / 100.0)
    raw = entry - distance if side is Side.LONG else entry + distance

    if swing_level is not None and _valid_stop(side, entry, swing_level):
        # posuň SL těsně za swing, pokud je dál než ATR stop (bezpečnější)
        buffer = snap.atr * 0.25
        candidate = swing_level - buffer if side is Side.LONG else swing_level + buffer
        if (side is Side.LONG and candidate < raw) or (side is Side.SHORT and candidate > raw):
            raw = candidate
    return _clamp_stop(side, entry, raw, cfg)


def build_take_profits(
    side: Side,
    entry: float,
    stop: float,
    snap: MarketSnapshot,
    cfg: ExitConfig,
) -> list[TakeProfit]:
    """TP ladder jako násobky R s podíly pozice, normalizovaný na součet 1.0."""
    r = abs(entry - stop)
    if r <= 0:
        return []
    key = snap.regime.value
    multiples = cfg.tp_r_multiples.get(key) or [1.0, 2.0]
    fractions = cfg.tp_fractions.get(key) or [0.5, 0.5]
    count = min(len(multiples), len(fractions))
    multiples, fractions = multiples[:count], list(fractions[:count])

    total = sum(fractions)
    fractions = [1.0 / count] * count if total <= 0 else [f / total for f in fractions]

    out: list[TakeProfit] = []
    for mult, frac in zip(multiples, fractions, strict=True):
        price = entry + side.sign * r * mult
        if price <= 0:
            continue
        out.append(TakeProfit(price=round(price, 10), fraction=round(frac, 6), r_multiple=mult))
    return out


def trail_distance(snap: MarketSnapshot, cfg: ExitConfig) -> float:
    """Vzdálenost trailing stopu v absolutní ceně (chandelier styl)."""
    return snap.atr * cfg.trail_atr_mult.get(snap.regime.value, 2.5)


def breakeven_price(side: Side, entry: float, cfg: ExitConfig) -> float:
    """BE posunuté o offset, aby pokrylo poplatky obou stran."""
    offset = entry * cfg.breakeven_offset_pct / 100.0
    return entry + side.sign * offset


def next_trailing_stop(
    side: Side,
    current_stop: float | None,
    price: float,
    distance: float,
) -> float | None:
    """Trailing stop se posouvá jen ve prospěch obchodu, nikdy zpět."""
    if distance <= 0:
        return current_stop
    candidate = price - distance if side is Side.LONG else price + distance
    if current_stop is None:
        return candidate
    if side is Side.LONG:
        return max(current_stop, candidate)
    return min(current_stop, candidate)


def swing_extreme(highs: list[float], lows: list[float], side: Side, lookback: int = 20) -> float | None:
    """Nejbližší swing low (pro long) / high (pro short) v posledních N barech."""
    if side is Side.LONG:
        window = lows[-lookback:] if lows else []
        return float(min(window)) if window else None
    window = highs[-lookback:] if highs else []
    return float(max(window)) if window else None


def _valid_stop(side: Side, entry: float, stop: float) -> bool:
    if stop <= 0:
        return False
    return stop < entry if side is Side.LONG else stop > entry


def _clamp_stop(side: Side, entry: float, stop: float, cfg: ExitConfig) -> float:
    min_dist = entry * cfg.min_sl_pct / 100.0
    max_dist = entry * cfg.max_sl_pct / 100.0
    distance = abs(entry - stop)
    distance = max(min_dist, min(max_dist, distance))
    return round(entry - side.sign * distance, 10)
