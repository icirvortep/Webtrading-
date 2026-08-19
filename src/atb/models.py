"""Doménové modely sdílené napříč celým botem."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> Side:
        return Side.SHORT if self is Side.LONG else Side.LONG


class Action(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    REVERSE = "reverse"
    CLOSE_ALL = "close_all"


class Regime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    VOLATILE = "volatile"
    QUIET = "quiet"

    @property
    def is_trend(self) -> bool:
        return self in (Regime.TREND_UP, Regime.TREND_DOWN)


@dataclass(slots=True)
class Signal:
    """Normalizovaný signál z TradingView webhooku."""

    symbol: str
    action: Action
    side: Side | None = None
    strategy: str = "tv"
    timeframe: str = "15m"
    price: float | None = None
    confidence: float = 0.5          # 0..1, volitelně posílá Pine skript
    sl_hint: float | None = None     # explicitní SL z TV (má přednost)
    tp_hint: float | None = None
    risk_multiplier: float = 1.0
    venue: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass(slots=True)
class MarketSnapshot:
    """Odvozené metriky trhu v okamžiku signálu."""

    symbol: str
    timeframe: str
    price: float
    atr: float
    atr_pct: float
    adx: float
    ema_fast: float
    ema_slow: float
    rsi: float
    bb_width: float
    volume_z: float
    realized_vol: float
    htf_trend: int                   # -1 / 0 / +1
    regime: Regime
    trend_strength: float            # 0..1
    funding_rate: float = 0.0
    spread_bps: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe, "price": self.price,
            "atr": self.atr, "atr_pct": self.atr_pct, "adx": self.adx,
            "rsi": self.rsi, "bb_width": self.bb_width, "volume_z": self.volume_z,
            "realized_vol": self.realized_vol, "htf_trend": self.htf_trend,
            "regime": self.regime.value, "trend_strength": self.trend_strength,
            "funding_rate": self.funding_rate, "spread_bps": self.spread_bps,
        }


@dataclass(slots=True)
class TakeProfit:
    price: float
    fraction: float                  # podíl pozice, 0..1
    r_multiple: float


@dataclass(slots=True)
class TradePlan:
    """Kompletní plán obchodu — vstup, SL, TP ladder, velikost, páka."""

    signal_id: str
    symbol: str
    side: Side
    entry: float
    stop_loss: float
    take_profits: list[TakeProfit]
    quantity: float
    notional: float
    leverage: int
    risk_amount: float
    risk_pct: float
    regime: Regime
    score: float
    reasons: list[str] = field(default_factory=list)
    breakeven_after_tp: int = 1
    trail_after_tp: int = 2
    trail_atr_mult: float = 2.5
    max_hold_minutes: int = 0
    atr: float = 0.0

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id, "symbol": self.symbol, "side": self.side.value,
            "entry": self.entry, "stop_loss": self.stop_loss, "quantity": self.quantity,
            "notional": self.notional, "leverage": self.leverage,
            "risk_amount": self.risk_amount, "risk_pct": self.risk_pct,
            "regime": self.regime.value, "score": self.score, "reasons": self.reasons,
            "take_profits": [
                {"price": t.price, "fraction": t.fraction, "r": t.r_multiple}
                for t in self.take_profits
            ],
        }


@dataclass(slots=True)
class Position:
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    leverage: int
    stop_loss: float | None = None
    take_profits: list[TakeProfit] = field(default_factory=list)
    unrealized_pnl: float = 0.0
    liquidation_price: float | None = None
    opened_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Balance:
    equity: float
    free: float
    currency: str = "USDT"


@dataclass(slots=True)
class OrderResult:
    ok: bool
    order_id: str | None = None
    filled_price: float | None = None
    filled_qty: float = 0.0
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class RejectReason(StrEnum):
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MAX_POSITIONS = "max_positions"
    DUPLICATE = "duplicate_position"
    LOW_SCORE = "low_score"
    SYMBOL_BLOCKED = "symbol_not_allowed"
    NO_EQUITY = "insufficient_equity"
    SIZE_TOO_SMALL = "size_below_minimum"
    SPREAD = "spread_too_wide"
    COOLDOWN = "cooldown_active"
    STALE = "signal_stale"


@dataclass(slots=True)
class Decision:
    accepted: bool
    plan: TradePlan | None = None
    reason: RejectReason | None = None
    detail: str = ""
