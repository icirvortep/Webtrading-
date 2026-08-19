"""Testy správy otevřených pozic — breakeven, trailing, rekonciliace."""
import json
import time

import pytest

from atb.execution.router import ExecutionRouter
from atb.models import Action, Position, Side, Signal
from atb.monitor.position_manager import PositionManager
from atb.notify import Notifier
from atb.state.store import Store
from atb.trader import Trader


@pytest.fixture()
def trader(config, exchange) -> Trader:
    config.strategy.adaptive_learning = False
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    yield trader
    trader.shutdown()


def open_trade(trader: Trader) -> dict:
    result = trader.handle_signal(Signal(
        symbol="BTC/USDT:USDT", action=Action.ENTRY, side=Side.LONG,
        timeframe="15m", confidence=0.9,
    ))
    assert result["status"] == "executed", result
    return trader.store.open_trades()[0]


def set_price(exchange, price: float) -> None:
    """Přepíše poslední svíčku, aby mark price odpovídala zadané hodnotě."""
    last = list(exchange._ohlcv[-1])
    last[4] = price
    last[2] = max(last[2], price)
    last[3] = min(last[3], price)
    exchange._ohlcv[-1] = last


def test_stop_moves_to_breakeven_after_first_target(trader, exchange):
    trade = open_trade(trader)
    entry, stop = trade["entry"], trade["stop_loss"]
    r = entry - stop

    set_price(exchange, entry + r * 1.2)                # +1.2R
    trader.positions.tick()

    updated = trader.store.open_trades()[0]
    assert updated["stop_loss"] > stop
    assert updated["stop_loss"] >= entry                # breakeven nebo výš


def test_trailing_stop_engages_after_second_target(trader, exchange):
    trade = open_trade(trader)
    entry, stop = trade["entry"], trade["stop_loss"]
    r = entry - stop

    set_price(exchange, entry + r * 8.0)                # +8R
    trader.positions.tick()

    updated = trader.store.open_trades()[0]
    assert updated["stop_loss"] > entry                 # už jen zisková zóna


def test_stop_never_moves_backwards(trader, exchange):
    trade = open_trade(trader)
    entry, stop = trade["entry"], trade["stop_loss"]
    r = entry - stop

    set_price(exchange, entry + r * 8.0)
    trader.positions.tick()
    best = trader.store.open_trades()[0]["stop_loss"]

    set_price(exchange, entry + r * 1.5)                # cena spadla zpět
    trader.positions.tick()
    assert trader.store.open_trades()[0]["stop_loss"] == best


def test_position_closed_by_exchange_is_settled(trader, exchange):
    trade = open_trade(trader)
    exchange.positions.clear()                          # jako by SL vystřelil
    trader.positions.tick()

    assert trader.store.open_trades() == []
    closed = trader.store.recent_closed()[0]
    assert closed["exit_reason"] == "exchange_stop"
    assert closed["id"] == trade["id"]


def test_time_stop_closes_stale_position(config, exchange):
    config.strategy.adaptive_learning = False
    config.exits.max_hold_minutes = 1
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    trade = open_trade(trader)

    plan = json.loads(trade["plan"])
    plan["max_hold_minutes"] = 1
    trader.store._conn.execute(
        "UPDATE trades SET opened_at=?, plan=? WHERE id=?",
        (time.time() - 3600, json.dumps(plan), trade["id"]),
    )
    trader.store._conn.commit()

    trader.positions.tick()
    assert exchange.positions == {}
    trader.shutdown()


def test_orphan_position_is_reported_not_touched(config, exchange, caplog):
    """Pozice otevřená ručně mimo bota se nesmí zavírat, jen zalogovat."""
    config.strategy.adaptive_learning = False
    store = Store(":memory:")
    router = ExecutionRouter(config, exchange, store, Notifier(config.notify))
    manager = PositionManager(config, exchange, store, router, Notifier(config.notify))
    exchange.positions["ETH/USDT:USDT"] = Position(
        symbol="ETH/USDT:USDT", side=Side.LONG, quantity=1.0, entry_price=2000.0, leverage=5,
    )
    with caplog.at_level("WARNING"):
        manager.tick()
    assert "Neevidovaná pozice" in caplog.text
    assert "ETH/USDT:USDT" in exchange.positions
    store.close()
