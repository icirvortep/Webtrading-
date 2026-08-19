"""Integrační testy — celý tok od signálu po objednávku na (falešné) burze."""
import pytest

from atb.models import Action, Side, Signal
from atb.state.store import Store
from atb.trader import Trader
from tests.conftest import FakeExchange, synthetic_ohlcv


@pytest.fixture()
def trader(config, exchange) -> Trader:
    config.strategy.adaptive_learning = False
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    yield trader
    trader.shutdown()


def make_signal(**kwargs) -> Signal:
    params = {"symbol": "BTC/USDT:USDT", "action": Action.ENTRY, "side": Side.LONG,
              "timeframe": "15m", "confidence": 0.9}
    params.update(kwargs)
    return Signal(**params)


def test_entry_signal_opens_position_with_stop(trader, exchange):
    result = trader.handle_signal(make_signal())
    assert result["status"] == "executed", result
    assert len(exchange.positions) == 1
    assert any(o["type"] == "sl" for o in exchange.orders)
    assert any(o["type"] == "tp" for o in exchange.orders)


def test_executed_trade_is_persisted(trader):
    trader.handle_signal(make_signal())
    open_trades = trader.store.open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["symbol"] == "BTC/USDT:USDT"
    assert open_trades[0]["stop_loss"] < open_trades[0]["entry"]


def test_duplicate_signal_id_is_ignored(trader):
    signal = make_signal()
    trader.handle_signal(signal)
    second = trader.handle_signal(make_signal(id=signal.id))
    assert second["status"] == "duplicate"


def test_second_position_on_same_symbol_rejected(trader):
    trader.handle_signal(make_signal())
    result = trader.handle_signal(make_signal())
    assert result["status"] == "rejected"
    assert result["reason"] in {"duplicate_position", "cooldown_active"}


def test_exit_signal_closes_position(trader, exchange):
    trader.handle_signal(make_signal())
    result = trader.handle_signal(make_signal(action=Action.EXIT, side=None))
    assert result["status"] == "closed"
    assert exchange.positions == {}
    assert trader.store.open_trades() == []


def test_kill_switch_rejects_entries(trader):
    trader.cfg.risk.kill_switch = True
    result = trader.handle_signal(make_signal())
    assert result["status"] == "rejected"
    assert result["reason"] == "kill_switch"


def test_position_closed_when_stop_cannot_be_placed(trader, exchange):
    exchange.fail_stop_loss = True
    result = trader.handle_signal(make_signal())
    assert result["status"] == "error"
    assert exchange.positions == {}          # bot nesmí nechat pozici bez SL


def test_dry_run_computes_plan_without_ordering(config, exchange):
    config.mode = "live"
    config.dry_run = True
    config.strategy.adaptive_learning = False
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    result = trader.handle_signal(make_signal())
    assert result["status"] == "dry_run"
    assert exchange.positions == {}
    assert result["plan"]["quantity"] > 0
    trader.shutdown()


def test_counter_trend_signal_is_vetoed_in_strong_trend(config):
    """Short do silného uptrendu musí engine odmítnout."""
    config.strategy.adaptive_learning = False
    exchange = FakeExchange(ohlcv=synthetic_ohlcv(400, drift=0.006, noise=0.0008))
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    result = trader.handle_signal(make_signal(side=Side.SHORT))
    assert result["status"] == "rejected"
    assert result["reason"] == "low_score"
    trader.shutdown()


def test_status_reports_equity_and_positions(trader):
    trader.handle_signal(make_signal())
    status = trader.status()
    assert status["equity"] > 0
    assert len(status["open_positions"]) == 1
    assert status["risk_per_trade_pct"] == pytest.approx(2.0)
