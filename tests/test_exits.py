import pytest

from atb.config import ExitConfig
from atb.models import MarketSnapshot, Regime, Side
from atb.strategy import exits


def snapshot(regime=Regime.TREND_UP, price=100.0, atr=2.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT:USDT", timeframe="15m", price=price, atr=atr,
        atr_pct=atr / price * 100, adx=30.0, ema_fast=price, ema_slow=price * 0.99,
        rsi=55.0, bb_width=4.0, volume_z=0.3, realized_vol=0.01, htf_trend=1,
        regime=regime, trend_strength=0.8,
    )


def test_stop_is_below_entry_for_long():
    cfg = ExitConfig()
    stop = exits.build_stop(Side.LONG, 100.0, snapshot(), cfg)
    assert stop < 100.0
    assert stop == pytest.approx(96.0)          # 2× ATR pod vstupem v trendu


def test_stop_is_above_entry_for_short():
    stop = exits.build_stop(Side.SHORT, 100.0, snapshot(Regime.TREND_DOWN), ExitConfig())
    assert stop > 100.0


def test_range_regime_uses_tighter_stop():
    cfg = ExitConfig()
    trend_stop = exits.build_stop(Side.LONG, 100.0, snapshot(Regime.TREND_UP), cfg)
    range_stop = exits.build_stop(Side.LONG, 100.0, snapshot(Regime.RANGE), cfg)
    assert range_stop > trend_stop              # blíž vstupu = těsnější


def test_stop_respects_max_pct_clamp():
    cfg = ExitConfig(max_sl_pct=3.0)
    stop = exits.build_stop(Side.LONG, 100.0, snapshot(atr=20.0), cfg)
    assert stop == pytest.approx(97.0)


def test_stop_respects_min_pct_clamp():
    cfg = ExitConfig(min_sl_pct=1.0)
    stop = exits.build_stop(Side.LONG, 100.0, snapshot(atr=0.001), cfg)
    assert stop == pytest.approx(99.0)


def test_explicit_hint_wins_when_valid():
    stop = exits.build_stop(Side.LONG, 100.0, snapshot(), ExitConfig(), sl_hint=98.5)
    assert stop == pytest.approx(98.5)


def test_invalid_hint_is_ignored():
    """SL nad vstupem u longu je nesmysl — spadne se zpět na ATR."""
    stop = exits.build_stop(Side.LONG, 100.0, snapshot(), ExitConfig(), sl_hint=105.0)
    assert stop < 100.0


def test_take_profits_are_r_multiples_and_sum_to_one():
    cfg = ExitConfig()
    tps = exits.build_take_profits(Side.LONG, 100.0, 96.0, snapshot(), cfg)
    assert len(tps) == 3
    assert tps[0].price == pytest.approx(104.0)     # 1R = 4 body
    assert tps[2].price == pytest.approx(114.0)     # 3.5R
    assert sum(tp.fraction for tp in tps) == pytest.approx(1.0)


def test_take_profits_for_short_go_down():
    tps = exits.build_take_profits(Side.SHORT, 100.0, 104.0, snapshot(Regime.TREND_DOWN), ExitConfig())
    assert all(tp.price < 100.0 for tp in tps)


def test_trailing_stop_never_moves_backwards():
    stop = exits.next_trailing_stop(Side.LONG, None, 110.0, 5.0)
    assert stop == pytest.approx(105.0)
    assert exits.next_trailing_stop(Side.LONG, stop, 108.0, 5.0) == pytest.approx(105.0)
    assert exits.next_trailing_stop(Side.LONG, stop, 120.0, 5.0) == pytest.approx(115.0)


def test_trailing_stop_for_short_only_decreases():
    stop = exits.next_trailing_stop(Side.SHORT, None, 90.0, 5.0)
    assert stop == pytest.approx(95.0)
    assert exits.next_trailing_stop(Side.SHORT, stop, 92.0, 5.0) == pytest.approx(95.0)
    assert exits.next_trailing_stop(Side.SHORT, stop, 80.0, 5.0) == pytest.approx(85.0)


def test_breakeven_includes_fee_offset():
    cfg = ExitConfig(breakeven_offset_pct=0.1)
    assert exits.breakeven_price(Side.LONG, 100.0, cfg) == pytest.approx(100.1)
    assert exits.breakeven_price(Side.SHORT, 100.0, cfg) == pytest.approx(99.9)


def test_swing_extreme_picks_correct_side():
    highs = [10, 12, 15, 11]
    lows = [5, 4, 6, 7]
    assert exits.swing_extreme(highs, lows, Side.LONG) == 4
    assert exits.swing_extreme(highs, lows, Side.SHORT) == 15
