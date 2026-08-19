"""Konfigurace: YAML soubor + přepis přes proměnné prostředí (.env)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path(os.getenv("ATB_CONFIG", "config/config.yaml"))


class ExchangeConfig(BaseModel):
    id: str = "bybit"                      # ccxt id
    account_type: Literal["swap", "future", "spot"] = "swap"
    quote: str = "USDT"
    testnet: bool = True
    api_key_env: str = "EXCHANGE_API_KEY"
    api_secret_env: str = "EXCHANGE_API_SECRET"
    password_env: str | None = None        # OKX/Bitget passphrase
    hedge_mode: bool = False
    margin_mode: Literal["isolated", "cross"] = "isolated"
    recv_window_ms: int = 10_000

    def credentials(self) -> dict[str, str]:
        creds = {
            "apiKey": os.getenv(self.api_key_env, ""),
            "secret": os.getenv(self.api_secret_env, ""),
        }
        if self.password_env:
            creds["password"] = os.getenv(self.password_env, "")
        return creds


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 2.0        # % z equity riskovaných na obchod
    max_risk_per_trade_pct: float = 3.0    # tvrdý strop i po adaptaci
    max_portfolio_risk_pct: float = 6.0    # součet otevřeného rizika
    max_daily_loss_pct: float = 6.0
    max_daily_trades: int = 20
    max_open_positions: int = 4
    max_positions_per_symbol: int = 1
    max_leverage: int = 20                 # strop nezávisle na burze
    min_leverage: int = 1
    max_notional_pct_of_equity: float = 500.0
    min_notional_usd: float = 5.0
    cooldown_after_loss_min: int = 15
    cooldown_after_streak: int = 3         # počet ztrát v řadě pro delší pauzu
    streak_cooldown_min: int = 120
    max_spread_bps: float = 12.0
    signal_max_age_sec: int = 90
    kill_switch: bool = False

    @field_validator("risk_per_trade_pct", "max_risk_per_trade_pct")
    @classmethod
    def _sane_risk(cls, v: float) -> float:
        if not 0 < v <= 10:
            raise ValueError("riziko na obchod musí být v intervalu (0, 10] %")
        return v


class StrategyConfig(BaseModel):
    enabled: bool = True
    htf_multiplier: int = 4                # vyšší timeframe = 4x signální
    ohlcv_limit: int = 300
    atr_period: int = 14
    adx_period: int = 14
    rsi_period: int = 14
    ema_fast: int = 21
    ema_slow: int = 55
    bb_period: int = 20
    adx_trend_threshold: float = 23.0
    adx_range_threshold: float = 18.0
    volatile_atr_pct: float = 3.0          # ATR/cena v % => volatilní režim
    quiet_atr_pct: float = 0.35
    min_score: float = 0.45                # pod tímto skóre se signál zahodí
    veto_counter_trend: bool = True        # zakázat protitrendové vstupy v trendu
    adaptive_learning: bool = True         # úprava rizika dle historie režimu
    learning_min_trades: int = 12
    learning_max_multiplier: float = 1.3
    learning_min_multiplier: float = 0.4


class ExitConfig(BaseModel):
    """Profily SL/TP pro jednotlivé režimy trhu (násobky ATR a R)."""

    sl_atr_mult: dict[str, float] = Field(default_factory=lambda: {
        "trend_up": 2.0, "trend_down": 2.0, "range": 1.2, "volatile": 3.0, "quiet": 1.5,
    })
    tp_r_multiples: dict[str, list[float]] = Field(default_factory=lambda: {
        "trend_up": [1.0, 2.0, 3.5], "trend_down": [1.0, 2.0, 3.5],
        "range": [0.8, 1.5], "volatile": [1.2, 2.5, 4.0], "quiet": [1.0, 1.8],
    })
    tp_fractions: dict[str, list[float]] = Field(default_factory=lambda: {
        "trend_up": [0.4, 0.35, 0.25], "trend_down": [0.4, 0.35, 0.25],
        "range": [0.6, 0.4], "volatile": [0.5, 0.3, 0.2], "quiet": [0.6, 0.4],
    })
    trail_atr_mult: dict[str, float] = Field(default_factory=lambda: {
        "trend_up": 2.5, "trend_down": 2.5, "range": 1.2, "volatile": 3.5, "quiet": 1.5,
    })
    min_sl_pct: float = 0.25               # SL nikdy blíž než 0.25 % (šum/poplatky)
    max_sl_pct: float = 8.0
    breakeven_after_tp: int = 1            # po kolikátém TP posunout SL na BE
    breakeven_offset_pct: float = 0.05     # +poplatky
    trail_after_tp: int = 2
    max_hold_minutes: int = 0              # 0 = bez časového stopu
    use_exchange_stops: bool = True        # posílat SL/TP přímo na burzu


class WebhookConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    path: str = "/webhook/tradingview"
    secret_env: str = "WEBHOOK_SECRET"
    require_hmac: bool = True              # X-Signature: hex(hmac_sha256(body))
    allowed_ips: list[str] = Field(default_factory=lambda: [
        # oficiální odesílací IP TradingView webhooků
        "52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7",
    ])
    enforce_ip_allowlist: bool = True
    max_body_bytes: int = 16_384


class MonitorConfig(BaseModel):
    enabled: bool = True
    poll_seconds: float = 5.0
    reconcile_seconds: float = 60.0


class NotifyConfig(BaseModel):
    telegram_enabled: bool = False
    telegram_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_env: str = "TELEGRAM_CHAT_ID"
    notify_levels: list[str] = Field(default_factory=lambda: ["entry", "exit", "error", "risk"])


class AppConfig(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    dry_run: bool = True
    log_level: str = "INFO"
    log_json: bool = False
    database: str = "data/atb.sqlite"
    symbols_allowlist: list[str] = Field(default_factory=list)   # prázdné = vše
    symbols_blocklist: list[str] = Field(default_factory=list)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @property
    def live(self) -> bool:
        return self.mode == "live" and not self.dry_run


_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "ATB_MODE": ("mode",),
    "ATB_DRY_RUN": ("dry_run",),
    "ATB_LOG_LEVEL": ("log_level",),
    "ATB_EXCHANGE": ("exchange", "id"),
    "ATB_TESTNET": ("exchange", "testnet"),
    "ATB_RISK_PCT": ("risk", "risk_per_trade_pct"),
    "ATB_MAX_LEVERAGE": ("risk", "max_leverage"),
    "ATB_KILL_SWITCH": ("risk", "kill_switch"),
    "ATB_WEBHOOK_PORT": ("webhook", "port"),
    "ATB_DATABASE": ("database",),
}

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in _TRUTHY:
        return True
    if low in _FALSY:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def load_config(path: str | Path | None = None) -> AppConfig:
    """Načte YAML konfiguraci a aplikuje přepisy z prostředí."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    for env_key, target in _ENV_OVERRIDES.items():
        raw = os.getenv(env_key)
        if raw is not None:
            _set_path(data, target, _coerce(raw))
    return AppConfig.model_validate(data)
