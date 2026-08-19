"""Jednoduchý event-driven backtest pro ověření nastavení strategie.

Přehrává historické OHLCV bar po baru, na každém baru vyhodnotí režim,
vygeneruje vlastní vstupní signál (EMA cross + potvrzení skóre), spočítá
SL/TP stejným kódem jako živý bot a simuluje průběh obchodu uvnitř baru.

Není to náhrada za důkladné testování — slouží k rychlé kontrole, že
parametry režimů a SL/TP profily dávají na daném trhu smysl.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .config import AppConfig
from .models import Side, TakeProfit
from .strategy import exits, scoring
from .strategy import indicators as ta
from .strategy import regime as regime_mod

log = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    side: Side
    entry_index: int
    entry: float
    stop: float
    take_profits: list[TakeProfit]
    quantity: float
    risk_amount: float
    regime: str
    remaining: float = 1.0
    realized: float = 0.0
    exit_index: int | None = None
    exit_reason: str = ""


@dataclass
class BacktestResult:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    equity_start: float = 0.0
    equity_end: float = 0.0
    max_drawdown_pct: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    by_regime: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def expectancy_r(self) -> float:
        return self.total_r / self.trades if self.trades else 0.0

    @property
    def return_pct(self) -> float:
        if not self.equity_start:
            return 0.0
        return (self.equity_end - self.equity_start) / self.equity_start * 100.0

    def summary(self) -> str:
        lines = [
            f"Obchodů:          {self.trades}",
            f"Win rate:         {self.win_rate * 100:.1f}%",
            f"Expektance:       {self.expectancy_r:+.3f}R / obchod",
            f"Součet R:         {self.total_r:+.2f}R",
            f"Equity:           {self.equity_start:.2f} → {self.equity_end:.2f} "
            f"({self.return_pct:+.2f}%)",
            f"Max drawdown:     {self.max_drawdown_pct:.2f}%",
        ]
        if self.by_regime:
            lines.append("Podle režimu:")
            for name, stats in sorted(self.by_regime.items()):
                lines.append(
                    f"  {name:<12} n={stats['trades']:>3.0f}  "
                    f"win={stats['win_rate'] * 100:>5.1f}%  E={stats['expectancy_r']:+.3f}R"
                )
        return "\n".join(lines)


def run_backtest(
    ohlcv: list[list[float]],
    cfg: AppConfig,
    starting_equity: float = 10_000.0,
    fee_bps: float = 5.5,
    warmup: int = 120,
) -> BacktestResult:
    arr = np.asarray(ohlcv, dtype=float)
    if arr.shape[0] <= warmup + 10:
        raise ValueError("Málo dat pro backtest")

    close, high, low = arr[:, 4], arr[:, 2], arr[:, 3]
    ema_fast = ta.ema(close, cfg.strategy.ema_fast)
    ema_slow = ta.ema(close, cfg.strategy.ema_slow)

    equity = starting_equity
    peak = equity
    result = BacktestResult(equity_start=starting_equity, equity_curve=[equity])
    open_trade: BacktestTrade | None = None
    regime_acc: dict[str, list[float]] = {}

    for i in range(warmup, arr.shape[0]):
        bar_high, bar_low = high[i], low[i]

        if open_trade is not None:
            equity += _process_bar(open_trade, bar_high, bar_low, fee_bps)
            if open_trade.remaining <= 1e-9:
                open_trade.exit_index = i
                r_value = open_trade.realized / open_trade.risk_amount if open_trade.risk_amount else 0.0
                result.trades += 1
                result.total_r += r_value
                if open_trade.realized > 0:
                    result.wins += 1
                else:
                    result.losses += 1
                regime_acc.setdefault(open_trade.regime, []).append(r_value)
                open_trade = None

        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100.0 if peak else 0.0
        result.max_drawdown_pct = max(result.max_drawdown_pct, drawdown)
        result.equity_curve.append(equity)

        if open_trade is not None or equity <= 0:
            continue

        side = _entry_signal(ema_fast, ema_slow, i)
        if side is None:
            continue

        window = arr[max(0, i - cfg.strategy.ohlcv_limit) : i + 1].tolist()
        htf = _resample(window, cfg.strategy.htf_multiplier)
        try:
            snap = regime_mod.classify(window, cfg.strategy, htf_ohlcv=htf, timeframe="bt")
        except ValueError:
            continue

        score = scoring.evaluate(side, snap, cfg.strategy)
        if not score.ok or score.value < cfg.strategy.min_score:
            continue

        entry = float(close[i])
        stop = exits.build_stop(side, entry, snap, cfg.exits)
        distance = abs(entry - stop)
        if distance <= 0:
            continue

        risk_pct = min(
            cfg.risk.risk_per_trade_pct * scoring.risk_multiplier_from_score(score.value, cfg.strategy),
            cfg.risk.max_risk_per_trade_pct,
        )
        risk_amount = equity * risk_pct / 100.0
        quantity = risk_amount / distance
        open_trade = BacktestTrade(
            side=side, entry_index=i, entry=entry, stop=stop,
            take_profits=exits.build_take_profits(side, entry, stop, snap, cfg.exits),
            quantity=quantity, risk_amount=risk_amount, regime=snap.regime.value,
        )
        equity -= entry * quantity * fee_bps / 10_000.0

    result.equity_end = equity
    result.by_regime = {
        name: {
            "trades": len(values),
            "win_rate": sum(1 for v in values if v > 0) / len(values),
            "expectancy_r": sum(values) / len(values),
        }
        for name, values in regime_acc.items()
    }
    return result


def _entry_signal(ema_fast: np.ndarray, ema_slow: np.ndarray, i: int) -> Side | None:
    """Vstupní trigger backtestu = kříž EMA (zástupce za signál z TradingView)."""
    if np.isnan(ema_fast[i]) or np.isnan(ema_slow[i]) or np.isnan(ema_fast[i - 1]):
        return None
    crossed_up = ema_fast[i - 1] <= ema_slow[i - 1] and ema_fast[i] > ema_slow[i]
    crossed_down = ema_fast[i - 1] >= ema_slow[i - 1] and ema_fast[i] < ema_slow[i]
    if crossed_up:
        return Side.LONG
    if crossed_down:
        return Side.SHORT
    return None


def _process_bar(trade: BacktestTrade, bar_high: float, bar_low: float, fee_bps: float) -> float:
    """Konzervativní pravidlo: pokud bar zasáhne SL i TP, počítá se SL jako první."""
    pnl = 0.0
    hit_stop = bar_low <= trade.stop if trade.side is Side.LONG else bar_high >= trade.stop
    if hit_stop:
        qty = trade.quantity * trade.remaining
        pnl = (trade.stop - trade.entry) * qty * trade.side.sign
        pnl -= trade.stop * qty * fee_bps / 10_000.0
        trade.realized += pnl
        trade.remaining = 0.0
        trade.exit_reason = "stop"
        return pnl

    for tp in list(trade.take_profits):
        reached = bar_high >= tp.price if trade.side is Side.LONG else bar_low <= tp.price
        if not reached:
            continue
        qty = trade.quantity * min(tp.fraction, trade.remaining)
        gained = (tp.price - trade.entry) * qty * trade.side.sign
        gained -= tp.price * qty * fee_bps / 10_000.0
        pnl += gained
        trade.realized += gained
        trade.remaining = max(0.0, trade.remaining - tp.fraction)
        trade.take_profits.remove(tp)
        # po prvním TP posuneme stop na breakeven
        trade.stop = trade.entry
        trade.exit_reason = "take_profit"
    return pnl


def _resample(ohlcv: list[list[float]], factor: int) -> list[list[float]]:
    """Agregace barů na vyšší timeframe (pro HTF kontext v backtestu)."""
    factor = max(factor, 2)
    out: list[list[float]] = []
    for start in range(0, len(ohlcv) - factor + 1, factor):
        chunk = ohlcv[start : start + factor]
        out.append([
            chunk[0][0], chunk[0][1],
            max(c[2] for c in chunk), min(c[3] for c in chunk),
            chunk[-1][4], sum(c[5] for c in chunk),
        ])
    return out
