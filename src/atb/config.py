"""Konfigurace: YAML soubor + přepis přes proměnné prostředí (.env)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(os.getenv("ATB_CONFIG", "config/config.yaml"))


class ExchangeConfig(BaseModel):
    id: str = "bybit"                      # ccxt id
    account_type: Literal["swap", "future", "spot", "margin"] = "swap"
    quote: str = "USDT"
    testnet: bool = True
    api_key_env: str = "EXCHANGE_API_KEY"
    api_secret_env: str = "EXCHANGE_API_SECRET"
    password_env: str | None = None        # OKX/Bitget passphrase
    hedge_mode: bool = False
    margin_mode: Literal["isolated", "cross"] = "isolated"
    recv_window_ms: int = 10_000

    @property
    def is_spot(self) -> bool:
        """Spot: žádné pozice, žádná páka, jen nákup a prodej drženého aktiva."""
        return self.account_type == "spot"

    @property
    def can_short(self) -> bool:
        """Na čistém spotu nejde prodat něco, co nevlastníš."""
        return self.account_type != "spot"

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


class UniverseConfig(BaseModel):
    """Automatický výběr trhů ze všech, co burza nabízí.

    Skenovat stovky symbolů v plné hloubce nejde — limity API to nedovolí.
    Proto dvě fáze: jeden hromadný dotaz na tickery seřadí celý trh podle
    likvidity, volatility a pohybu, a do hluboké analýzy jde jen špička.
    """

    enabled: bool = True
    quote: str = "USDT"
    #: minimální 24h objem v kotační měně — pod tím je kniha příliš mělká
    min_volume_24h: float = 50_000_000.0
    max_spread_bps: float = 8.0
    #: kolik nejlepších kandidátů projít plnou analýzou
    deep_scan_count: int = 24
    #: kolik z nich stihnout v jednom kole (zbytek přijde na řadu v dalším)
    batch_size: int = 8
    refresh_minutes: float = 15.0
    #: symboly, které nikdy nechceme (např. pákové tokeny)
    exclude_patterns: list[str] = Field(default_factory=lambda: ["1000000", "USDC/"])
    #: váhy pro pořadí kandidátů, součet nemusí být 1
    weight_liquidity: float = 0.4
    weight_volatility: float = 0.35
    weight_momentum: float = 0.25


class ScannerConfig(BaseModel):
    """Průběžné skenování trhu — zdroj živého přehledu i vlastních signálů."""

    enabled: bool = True
    watchlist: list[str] = Field(default_factory=lambda: [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    ])
    timeframe: str = "15m"
    interval_seconds: float = 20.0
    #: automaticky vybírat trhy z celé burzy místo pevného watchlistu
    auto_universe: bool = True
    #: autopilot = bot obchoduje z vlastních signálů, bez TradingView
    autopilot: bool = False
    min_trigger_strength: float = 0.5


class UIConfig(BaseModel):
    enabled: bool = True
    refresh_seconds: float = 3.0
    #: rozhraní je bez přihlášení — nechávej ho jen na localhost
    bind_localhost_only: bool = True


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
    mode: Literal["offline", "paper", "live"] = "paper"
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
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @model_validator(mode="after")
    def _align_with_market_type(self) -> AppConfig:
        """Na spotu nedávají páka ani burzovní SL/TP smysl — srovnáme to tady.

        Bez toho by risk manager počítal páku, kterou burza neumí nastavit,
        a router by posílal stop příkazy, které spotový účet odmítne.
        """
        if self.exchange.is_spot:
            self.risk.max_leverage = 1
            self.risk.min_leverage = 1
            # spotové SL/TP si hlídá bot sám (viz PositionManager)
            self.exits.use_exchange_stops = False
        return self

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
    "ATB_AUTOPILOT": ("scanner", "autopilot"),
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


#: pole, která smí měnit uživatelské rozhraní (zbytek vyžaduje restart a ruční zásah)
EDITABLE_PATHS: tuple[tuple[str, ...], ...] = (
    ("risk", "risk_per_trade_pct"),
    ("risk", "max_risk_per_trade_pct"),
    ("risk", "max_portfolio_risk_pct"),
    ("risk", "max_daily_loss_pct"),
    ("risk", "max_daily_trades"),
    ("risk", "max_open_positions"),
    ("risk", "max_leverage"),
    ("risk", "max_spread_bps"),
    ("risk", "cooldown_after_loss_min"),
    ("risk", "streak_cooldown_min"),
    ("risk", "kill_switch"),
    ("strategy", "min_score"),
    ("strategy", "veto_counter_trend"),
    ("strategy", "adaptive_learning"),
    ("strategy", "adx_trend_threshold"),
    ("strategy", "adx_range_threshold"),
    ("strategy", "volatile_atr_pct"),
    ("strategy", "htf_multiplier"),
    ("exits", "sl_atr_mult"),
    ("exits", "tp_r_multiples"),
    ("exits", "tp_fractions"),
    ("exits", "trail_atr_mult"),
    ("exits", "min_sl_pct"),
    ("exits", "max_sl_pct"),
    ("exits", "breakeven_after_tp"),
    ("exits", "trail_after_tp"),
    ("exits", "max_hold_minutes"),
    ("scanner", "watchlist"),
    ("scanner", "auto_universe"),
    ("universe", "enabled"),
    ("universe", "min_volume_24h"),
    ("universe", "max_spread_bps"),
    ("universe", "deep_scan_count"),
    ("universe", "batch_size"),
    ("universe", "refresh_minutes"),
    ("universe", "weight_liquidity"),
    ("universe", "weight_volatility"),
    ("universe", "weight_momentum"),
    ("scanner", "timeframe"),
    ("scanner", "interval_seconds"),
    ("scanner", "autopilot"),
    ("scanner", "min_trigger_strength"),
    ("symbols_allowlist"),
)


def _get_path(data: Any, path: tuple[str, ...]) -> Any:
    node = data
    for key in path:
        node = node[key] if isinstance(node, dict) else getattr(node, key)
    return node


def apply_updates(cfg: AppConfig, updates: dict[str, Any]) -> AppConfig:
    """Vrátí novou, zvalidovanou konfiguraci s aplikovanými změnami.

    `updates` je plochá mapa `"risk.risk_per_trade_pct" -> hodnota`. Měnit jde
    jen pole z `EDITABLE_PATHS`; cokoli jiného skončí chybou, aby rozhraní
    nemohlo přepsat například přihlašovací údaje nebo živý režim.
    """
    editable = {".".join(p) if isinstance(p, tuple) else p for p in EDITABLE_PATHS}
    data = cfg.model_dump()
    for dotted, value in updates.items():
        if dotted not in editable:
            raise ValueError(f"pole '{dotted}' nelze měnit z rozhraní")
        _set_path(data, tuple(dotted.split(".")), value)
    return AppConfig.model_validate(data)


def apply_updates_inplace(cfg: AppConfig, updates: dict[str, Any]) -> AppConfig:
    """Zvaliduje změny a promítne je do *živého* objektu konfigurace.

    Trader, risk manager i skener drží referenci na tentýž `AppConfig`, takže
    nová hodnota se projeví okamžitě bez restartu — proto se nevrací kopie,
    ale mění se existující instance.
    """
    validated = apply_updates(cfg, updates)
    for dotted in updates:
        path = tuple(dotted.split("."))
        target = cfg
        for key in path[:-1]:
            target = getattr(target, key)
        setattr(target, path[-1], _get_path(validated, path))
    return cfg


def persist_updates(updates: dict[str, Any], path: str | Path | None = None) -> Path:
    """Zapíše změny do YAML souboru — a nic jiného.

    Ukládá se *soubor na disku* s aplikovanými změnami, ne běžící konfigurace.
    Kdyby se dumpoval živý objekt, uložily by se do něj i dočasné přepínače
    z příkazové řádky a prostředí (``--offline``, ``--live``, ``ATB_DATABASE``)
    a uživatel by je měl natrvalo v souboru, aniž by o to požádal.
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    editable = {".".join(p) if isinstance(p, tuple) else p for p in EDITABLE_PATHS}
    for dotted, value in updates.items():
        if dotted not in editable:
            raise ValueError(f"pole '{dotted}' nelze měnit z rozhraní")
        _set_path(data, tuple(dotted.split(".")), value)

    # kontrola, že výsledek dává smysl, ať na disku nikdy neskončí rozbitý soubor
    AppConfig.model_validate(data)

    header = (
        "# Konfigurace Adaptive Trading Bota.\n"
        "# Tenhle soubor přepisuje i webové rozhraní, takže se z něj při uložení\n"
        "# ztratí komentáře. Okomentovaný vzor všech voleb je v config.example.yaml.\n"
    )
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    cfg_path.write_text(header + body, encoding="utf-8")
    return cfg_path
