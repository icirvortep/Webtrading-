import time

from atb.state.store import Store


def open_and_close(store: Store, symbol: str, pnl: float, regime: str = "trend_up") -> int:
    trade_id = store.open_trade(
        {"symbol": symbol, "side": "long", "regime": regime, "signal_id": "s",
         "leverage": 5, "stop_loss": 95.0, "risk_amount": 200.0},
        100.0, 1.0,
    )
    store.close_trade(trade_id, 100.0 + pnl, pnl)
    return trade_id


def test_open_and_close_trade(store):
    trade_id = store.open_trade({"symbol": "BTC/USDT:USDT", "side": "long", "risk_amount": 50.0,
                                 "signal_id": "s", "leverage": 3, "stop_loss": 95.0}, 100.0, 2.0)
    assert len(store.open_trades()) == 1
    store.close_trade(trade_id, 110.0, 20.0, exit_reason="tp")
    assert store.open_trades() == []
    closed = store.recent_closed()[0]
    assert closed["pnl"] == 20.0
    assert closed["exit_reason"] == "tp"


def test_r_multiple_is_computed_on_close(store):
    trade_id = store.open_trade({"symbol": "X", "side": "long", "risk_amount": 10.0,
                                 "signal_id": "s", "leverage": 1, "stop_loss": 90.0}, 100.0, 1.0)
    store.close_trade(trade_id, 120.0, 20.0)       # riziko 10 na jednotku → +2R
    assert store.recent_closed()[0]["r_multiple"] == 2.0


def test_daily_pnl_and_trade_count(store):
    open_and_close(store, "A", 10.0)
    open_and_close(store, "B", -4.0)
    assert store.daily_pnl() == 6.0
    assert store.daily_trade_count() == 2


def test_loss_streak(store):
    open_and_close(store, "A", -1.0)
    open_and_close(store, "B", -2.0)
    assert store.loss_streak() == 2
    open_and_close(store, "C", 5.0)
    assert store.loss_streak() == 0


def test_open_risk_total(store):
    store.open_trade({"symbol": "A", "side": "long", "risk_amount": 100.0,
                      "signal_id": "s", "leverage": 1, "stop_loss": 1.0}, 10.0, 1.0)
    store.open_trade({"symbol": "B", "side": "long", "risk_amount": 50.0,
                      "signal_id": "s", "leverage": 1, "stop_loss": 1.0}, 10.0, 1.0)
    assert store.open_risk_total() == 150.0


def test_regime_stats(store):
    open_and_close(store, "A", 10.0, regime="range")
    open_and_close(store, "B", -5.0, regime="range")
    stats = store.regime_stats("range")
    assert stats["trades"] == 2
    assert stats["win_rate"] == 0.5
    assert store.regime_stats("volatile")["trades"] == 0


def test_start_of_day_equity_is_sticky(store):
    assert store.start_of_day_equity(10_000.0) == 10_000.0
    assert store.start_of_day_equity(8_000.0) == 10_000.0


def test_signal_deduplication(store):
    assert not store.seen_signal("abc")
    store.record_signal("abc", "BTC/USDT:USDT", "entry", "long", True)
    assert store.seen_signal("abc")


def test_equity_curve_records(store):
    store.record_equity(10_000.0)
    time.sleep(0.002)
    store.record_equity(10_050.0)
    assert store.stats()["closed_trades"] == 0
