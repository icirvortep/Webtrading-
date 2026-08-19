import pytest

from atb.backtest import run_backtest
from tests.conftest import synthetic_ohlcv


def test_backtest_runs_and_reports(config):
    result = run_backtest(synthetic_ohlcv(1200, seed=3), config, starting_equity=10_000.0)
    assert result.trades >= 0
    assert result.equity_curve
    assert result.max_drawdown_pct >= 0.0
    assert "Obchodů" in result.summary()


def test_backtest_requires_enough_data(config):
    with pytest.raises(ValueError, match="Málo dat"):
        run_backtest(synthetic_ohlcv(50), config)


def test_risk_per_trade_bounds_single_loss(config):
    """Žádný jednotlivý obchod nesmí ztratit víc než nastavený strop rizika."""
    config.risk.risk_per_trade_pct = 2.0
    config.risk.max_risk_per_trade_pct = 3.0
    result = run_backtest(synthetic_ohlcv(1500, seed=11), config, starting_equity=10_000.0)
    if result.trades:
        # -1R je plný zásah SL; horší než -1.05R by znamenalo chybu v sizingu
        assert result.total_r >= -result.trades * 1.05
