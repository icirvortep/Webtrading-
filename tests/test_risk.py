import time

import pytest

from atb.models import Action, Balance, MarketSnapshot, Regime, RejectReason, Side, Signal
from atb.risk.manager import RiskManager
from atb.strategy import scoring


@pytest.fixture()
def snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT:USDT", timeframe="15m", price=100.0, atr=2.0, atr_pct=2.0,
        adx=30.0, ema_fast=101.0, ema_slow=99.0, rsi=55.0, bb_width=4.0,
        volume_z=0.5, realized_vol=0.01, htf_trend=1, regime=Regime.TREND_UP,
        trend_strength=0.85,
    )


@pytest.fixture()
def manager(config, store) -> RiskManager:
    return RiskManager(config, store)


LIMITS = {"min_amount": 0.0001, "min_cost": 5.0, "max_leverage": 100.0, "contract_size": 1.0}


def perfect_score(value: float = 1.0) -> scoring.Score:
    return scoring.Score(value=value, reasons=["test"])


def entry_signal(**kwargs) -> Signal:
    params = {"symbol": "BTC/USDT:USDT", "action": Action.ENTRY, "side": Side.LONG,
              "price": 100.0, "confidence": 0.9}
    params.update(kwargs)
    return Signal(**params)


def test_position_size_risks_exactly_configured_percent(manager, snapshot, config):
    """Jádro celého bota: zásah SL musí stát přesně nastavená % equity."""
    config.strategy.adaptive_learning = False
    balance = Balance(equity=10_000.0, free=10_000.0)
    decision = manager.build_plan(entry_signal(), Side.LONG, snapshot, balance, perfect_score(), LIMITS)

    assert decision.accepted
    plan = decision.plan
    loss_at_stop = plan.stop_distance * plan.quantity
    assert loss_at_stop == pytest.approx(plan.risk_amount, rel=1e-6)
    assert plan.risk_amount == pytest.approx(balance.equity * plan.risk_pct / 100.0, rel=1e-6)
    # při plném skóre a klidném trhu se drží kolem nastavených 2 %
    assert 1.0 <= plan.risk_pct <= config.risk.max_risk_per_trade_pct


def test_risk_scales_with_equity(manager, snapshot, config):
    config.strategy.adaptive_learning = False
    small = manager.build_plan(entry_signal(), Side.LONG, snapshot,
                               Balance(equity=1_000.0, free=1_000.0), perfect_score(), LIMITS)
    large = manager.build_plan(entry_signal(), Side.LONG, snapshot,
                               Balance(equity=10_000.0, free=10_000.0), perfect_score(), LIMITS)
    assert large.plan.quantity == pytest.approx(small.plan.quantity * 10, rel=1e-6)


def test_leverage_never_exceeds_configured_cap(manager, snapshot, config):
    config.risk.max_leverage = 10
    config.risk.max_notional_pct_of_equity = 10_000.0
    decision = manager.build_plan(entry_signal(), Side.LONG, snapshot,
                                  Balance(equity=100.0, free=100.0), perfect_score(), LIMITS)
    if decision.accepted:
        assert decision.plan.leverage <= 10


def test_leverage_never_exceeds_venue_cap(manager, snapshot, config):
    config.risk.max_leverage = 100
    limits = {**LIMITS, "max_leverage": 5.0}
    decision = manager.build_plan(entry_signal(), Side.LONG, snapshot,
                                  Balance(equity=200.0, free=200.0), perfect_score(), limits)
    if decision.accepted:
        assert decision.plan.leverage <= 5


def test_low_score_is_rejected(manager, snapshot, config):
    decision = manager.build_plan(entry_signal(), Side.LONG, snapshot,
                                  Balance(equity=10_000.0, free=10_000.0),
                                  perfect_score(0.10), LIMITS)
    assert not decision.accepted
    assert decision.reason is RejectReason.LOW_SCORE


def test_veto_blocks_trade(manager, snapshot):
    vetoed = scoring.Score(value=0.9, reasons=[], veto="protitrend")
    decision = manager.build_plan(entry_signal(), Side.LONG, snapshot,
                                  Balance(equity=10_000.0, free=10_000.0), vetoed, LIMITS)
    assert not decision.accepted


def test_kill_switch_blocks_everything(manager, config):
    config.risk.kill_switch = True
    decision = manager.pretrade_checks(entry_signal(), Balance(equity=1000.0, free=1000.0), [])
    assert decision is not None and decision.reason is RejectReason.KILL_SWITCH


def test_stale_signal_rejected(manager):
    old = entry_signal()
    old.received_at = time.time() - 600
    decision = manager.pretrade_checks(old, Balance(equity=1000.0, free=1000.0), [])
    assert decision is not None and decision.reason is RejectReason.STALE


def test_max_open_positions_enforced(manager, config):
    config.risk.max_open_positions = 2
    decision = manager.pretrade_checks(entry_signal(), Balance(equity=1000.0, free=1000.0),
                                       ["ETH/USDT:USDT", "SOL/USDT:USDT"])
    assert decision is not None and decision.reason is RejectReason.MAX_POSITIONS


def test_duplicate_position_rejected(manager):
    decision = manager.pretrade_checks(entry_signal(), Balance(equity=1000.0, free=1000.0),
                                       ["BTC/USDT:USDT"])
    assert decision is not None and decision.reason is RejectReason.DUPLICATE


def test_daily_loss_limit_stops_trading(manager, store, config):
    config.risk.max_daily_loss_pct = 5.0
    store.start_of_day_equity(10_000.0)
    decision = manager.pretrade_checks(entry_signal(), Balance(equity=9_400.0, free=9_400.0), [])
    assert decision is not None and decision.reason is RejectReason.DAILY_LOSS_LIMIT


def test_symbol_blocklist(manager, config):
    config.symbols_blocklist = ["BTC/USDT:USDT"]
    decision = manager.pretrade_checks(entry_signal(), Balance(equity=1000.0, free=1000.0), [])
    assert decision is not None and decision.reason is RejectReason.SYMBOL_BLOCKED


def test_portfolio_risk_cap_limits_new_trade(manager, store, config, snapshot):
    config.risk.max_portfolio_risk_pct = 2.0
    store.open_trade({"symbol": "ETH/USDT:USDT", "side": "long", "risk_amount": 200.0,
                      "signal_id": "x", "leverage": 5, "stop_loss": 1.0}, 100.0, 1.0)
    decision = manager.build_plan(entry_signal(), Side.LONG, snapshot,
                                  Balance(equity=10_000.0, free=10_000.0), perfect_score(), LIMITS)
    assert not decision.accepted and decision.reason is RejectReason.MAX_POSITIONS


def test_cooldown_after_loss(manager, store, config):
    config.risk.cooldown_after_loss_min = 30
    trade_id = store.open_trade({"symbol": "BTC/USDT:USDT", "side": "long", "risk_amount": 10.0,
                                 "signal_id": "x", "leverage": 5, "stop_loss": 95.0}, 100.0, 1.0)
    store.close_trade(trade_id, 95.0, -50.0, exit_reason="stop")
    decision = manager.pretrade_checks(entry_signal(), Balance(equity=1000.0, free=1000.0), [])
    assert decision is not None and decision.reason is RejectReason.COOLDOWN


def test_adaptive_risk_shrinks_after_losing_streak(manager, store, config, snapshot):
    config.strategy.adaptive_learning = False
    base, _ = manager.adaptive_risk_pct(snapshot, 1.0)
    for _ in range(3):
        trade_id = store.open_trade({"symbol": "X", "side": "long", "risk_amount": 10.0,
                                     "signal_id": "s", "leverage": 1, "stop_loss": 95.0}, 100.0, 1.0)
        store.close_trade(trade_id, 95.0, -20.0)
    reduced, reasons = manager.adaptive_risk_pct(snapshot, 1.0)
    assert reduced < base
    assert any("série ztrát" in r for r in reasons)


def test_adaptive_risk_never_exceeds_hard_cap(manager, config, snapshot):
    config.risk.risk_per_trade_pct = 9.0
    config.risk.max_risk_per_trade_pct = 3.0
    risk_pct, _ = manager.adaptive_risk_pct(snapshot, 1.0)
    assert risk_pct <= 3.0


def test_tiny_account_rejected_below_exchange_minimum(manager, snapshot):
    decision = manager.build_plan(entry_signal(), Side.LONG, snapshot,
                                  Balance(equity=1.0, free=1.0), perfect_score(),
                                  {**LIMITS, "min_cost": 100.0})
    assert not decision.accepted and decision.reason is RejectReason.SIZE_TOO_SMALL
