"""Testy vlastního generátoru signálů a skeneru."""
import numpy as np
import pytest

from atb.config import AppConfig
from atb.models import Regime, Side
from atb.state.store import Store
from atb.strategy import regime as regime_mod
from atb.strategy import signals as signal_mod
from atb.trader import Trader
from tests.conftest import FakeExchange, synthetic_ohlcv


def snapshot_for(ohlcv, cfg: AppConfig):
    return regime_mod.classify(ohlcv, cfg.strategy, symbol="X/USDT:USDT", timeframe="15m")


def test_no_triggers_on_insufficient_data(config):
    short = synthetic_ohlcv(30)
    snap = snapshot_for(synthetic_ohlcv(300), config)
    assert signal_mod.detect(short, snap, config.strategy) == []


def test_triggers_carry_direction_and_description(config):
    ohlcv = synthetic_ohlcv(400, drift=0.002, noise=0.003, seed=5)
    snap = snapshot_for(ohlcv, config)
    for trigger in signal_mod.detect(ohlcv, snap, config.strategy):
        assert trigger.side in (Side.LONG, Side.SHORT)
        assert trigger.kind in {"pullback", "mean_reversion", "breakout"}
        assert trigger.description
        assert 0.0 <= trigger.strength <= 1.0


def test_pullback_only_fires_with_the_trend(config):
    """V uptrendu smí pullback dát jen long, nikdy short."""
    ohlcv = synthetic_ohlcv(400, drift=0.003, noise=0.002, seed=9)
    snap = snapshot_for(ohlcv, config)
    if snap.regime is not Regime.TREND_UP:
        pytest.skip("data nevytvořila uptrend")
    pullbacks = [t for t in signal_mod.detect(ohlcv, snap, config.strategy) if t.kind == "pullback"]
    assert all(t.side is Side.LONG for t in pullbacks)


def test_mean_reversion_needs_range_regime(config):
    ohlcv = synthetic_ohlcv(400, drift=0.004, noise=0.001, seed=3)
    snap = snapshot_for(ohlcv, config)
    if snap.regime is Regime.RANGE:
        pytest.skip("data vytvořila range")
    kinds = {t.kind for t in signal_mod.detect(ohlcv, snap, config.strategy)}
    assert "mean_reversion" not in kinds


@pytest.fixture()
def trader(config) -> Trader:
    config.strategy.adaptive_learning = False
    config.scanner.enabled = False
    config.scanner.watchlist = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    trader = Trader(config, exchange=FakeExchange(), store=Store(":memory:"))
    yield trader
    trader.shutdown()


def test_scan_covers_whole_watchlist(trader):
    results = trader.scanner.scan_once()
    assert set(results) == {"BTC/USDT:USDT", "ETH/USDT:USDT"}
    for entry in results.values():
        assert entry["market"]["price"] > 0
        assert set(entry["sides"]) == {"long", "short"}


def test_scan_survives_a_broken_symbol(trader, monkeypatch):
    original = trader.engine.analyze

    def flaky(symbol, timeframe):
        if symbol.startswith("ETH"):
            raise RuntimeError("burza neodpovídá")
        return original(symbol, timeframe)

    monkeypatch.setattr(trader.engine, "analyze", flaky)
    results = trader.scanner.scan_once()
    assert "error" in results["ETH/USDT:USDT"]
    assert "market" in results["BTC/USDT:USDT"]       # druhý symbol se dopočítal


def test_autopilot_off_by_default_never_trades(trader):
    trader.scanner.scan_once()
    assert trader.store.open_trades() == []


def test_autopilot_places_trade_when_trigger_fires(trader, monkeypatch):
    trader.cfg.scanner.autopilot = True
    monkeypatch.setattr(signal_mod, "detect", lambda *a, **k: [
        signal_mod.Trigger(Side.LONG, "pullback", "testovací spouštěč", 0.9),
    ])
    trader.scanner.scan_once()
    open_trades = trader.store.open_trades()
    # spouštěč padne na obou symbolech z watchlistu, na každém jednou
    assert {t["symbol"] for t in open_trades} == set(trader.cfg.scanner.watchlist)
    assert all(t["side"] == "long" for t in open_trades)


def test_autopilot_respects_min_trigger_strength(trader, monkeypatch):
    trader.cfg.scanner.autopilot = True
    trader.cfg.scanner.min_trigger_strength = 0.8
    monkeypatch.setattr(signal_mod, "detect", lambda *a, **k: [
        signal_mod.Trigger(Side.LONG, "pullback", "slabý spouštěč", 0.3),
    ])
    trader.scanner.scan_once()
    assert trader.store.open_trades() == []


def test_autopilot_respects_kill_switch(trader, monkeypatch):
    trader.cfg.scanner.autopilot = True
    trader.cfg.risk.kill_switch = True
    monkeypatch.setattr(signal_mod, "detect", lambda *a, **k: [
        signal_mod.Trigger(Side.LONG, "pullback", "spouštěč", 0.9),
    ])
    trader.scanner.scan_once()
    assert trader.store.open_trades() == []


def test_scanner_state_is_serializable(trader):
    trader.scanner.scan_once()
    state = trader.scanner.state()
    assert state["watchlist"]
    assert state["age_seconds"] is not None
    import json
    json.dumps(state)                                 # rozhraní to posílá jako JSON


def test_sparkline_matches_closing_prices(trader):
    entry = trader.scanner.scan_symbol("BTC/USDT:USDT")
    closes = [row[4] for row in trader.exchange.fetch_ohlcv("BTC/USDT:USDT", "15m", 60)]
    assert np.allclose(entry["sparkline"], [round(c, 8) for c in closes])
