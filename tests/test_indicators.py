import numpy as np
import pytest

from atb.strategy import indicators as ta
from tests.conftest import synthetic_ohlcv


@pytest.fixture()
def series():
    data = np.asarray(synthetic_ohlcv(200), dtype=float)
    return data[:, 2], data[:, 3], data[:, 4]   # high, low, close


def test_ema_tracks_constant_series():
    values = [10.0] * 50
    assert ta.last_valid(ta.ema(values, 10)) == pytest.approx(10.0)


def test_ema_reacts_faster_than_sma():
    values = [10.0] * 40 + [20.0] * 10
    assert ta.last_valid(ta.ema(values, 20)) > ta.last_valid(ta.sma(values, 20))


def test_atr_positive_and_scales_with_range(series):
    high, low, close = series
    atr = ta.last_valid(ta.atr(high, low, close, 14))
    assert atr > 0
    wider = ta.last_valid(ta.atr(high * 1.02, low * 0.98, close, 14))
    assert wider > atr


def test_rsi_bounds(series):
    _, _, close = series
    values = ta.rsi(close, 14)
    valid = values[~np.isnan(values)]
    assert valid.size > 0
    assert valid.min() >= 0.0 and valid.max() <= 100.0


def test_rsi_extremes():
    rising = list(np.linspace(100, 200, 60))
    assert ta.last_valid(ta.rsi(rising, 14)) > 90
    falling = list(np.linspace(200, 100, 60))
    assert ta.last_valid(ta.rsi(falling, 14)) < 10


def test_adx_higher_in_trend_than_in_chop():
    trend = np.asarray(synthetic_ohlcv(300, drift=0.004, noise=0.001), dtype=float)
    chop = np.asarray(synthetic_ohlcv(300, drift=0.0, noise=0.006), dtype=float)
    adx_trend = ta.last_valid(ta.adx(trend[:, 2], trend[:, 3], trend[:, 4], 14)[0])
    adx_chop = ta.last_valid(ta.adx(chop[:, 2], chop[:, 3], chop[:, 4], 14)[0])
    assert adx_trend > adx_chop


def test_bollinger_ordering(series):
    _, _, close = series
    upper, mid, lower = ta.bollinger(close, 20, 2.0)
    assert ta.last_valid(upper) > ta.last_valid(mid) > ta.last_valid(lower)


def test_zscore_of_constant_is_zero():
    assert ta.last_valid(ta.zscore([5.0] * 40, 20)) == pytest.approx(0.0)


def test_short_input_returns_nan_without_crash():
    assert np.isnan(ta.last_valid(ta.ema([1.0, 2.0], 20)))
