import os
from pathlib import Path

import pytest

from atb.config import AppConfig, load_config
from atb.exchanges import registry


def test_all_venues_have_sane_leverage():
    assert registry.VENUES
    for venue in registry.VENUES.values():
        assert 1 <= venue.max_leverage <= 500
        assert venue.kind in {"cex", "dex"}


def test_recommend_prefers_testnet_capable_venue():
    venue = registry.recommend(require_testnet=True, min_leverage=50)
    assert venue.testnet
    assert venue.max_leverage >= 50


def test_recommend_can_ignore_testnet_requirement():
    venue = registry.recommend(require_testnet=False, min_leverage=150)
    assert venue.max_leverage >= 150


def test_max_leverage_lookup_falls_back():
    assert registry.max_leverage("bybit") == 100
    assert registry.max_leverage("neexistuje", fallback=7) == 7


def test_table_lists_every_venue():
    table = registry.table()
    for venue in registry.VENUES.values():
        assert venue.id in table


def test_default_config_is_safe():
    """Výchozí konfigurace nesmí nikdy obchodovat naostro."""
    cfg = AppConfig()
    assert cfg.mode == "paper"
    assert cfg.dry_run is True
    assert cfg.live is False
    assert cfg.exchange.testnet is True
    assert cfg.risk.risk_per_trade_pct == 2.0


def test_shipped_config_matches_requested_risk():
    cfg = load_config("config/config.yaml")
    assert cfg.risk.risk_per_trade_pct == 2.0
    assert cfg.mode == "paper"
    assert cfg.exchange.testnet is True


def test_env_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("ATB_RISK_PCT", "1.5")
    monkeypatch.setenv("ATB_MAX_LEVERAGE", "8")
    monkeypatch.setenv("ATB_KILL_SWITCH", "true")
    monkeypatch.setenv("ATB_EXCHANGE", "okx")
    cfg = load_config("config/config.yaml")
    assert cfg.risk.risk_per_trade_pct == 1.5
    assert cfg.risk.max_leverage == 8
    assert cfg.risk.kill_switch is True
    assert cfg.exchange.id == "okx"


def test_insane_risk_is_rejected():
    with pytest.raises(ValueError, match="riziko na obchod"):
        AppConfig.model_validate({"risk": {"risk_per_trade_pct": 50.0}})


def test_credentials_read_from_environment(monkeypatch):
    monkeypatch.setenv("EXCHANGE_API_KEY", "key-123")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "secret-456")
    creds = AppConfig().exchange.credentials()
    assert creds == {"apiKey": "key-123", "secret": "secret-456"}


def test_config_file_contains_no_secrets():
    """Klíče se čtou z prostředí — v gitu nesmí být žádná tajemství."""
    text = Path("config/config.yaml").read_text(encoding="utf-8").lower()
    for needle in ("api_key:", "api_secret:", "password:"):
        assert needle not in text
    assert os.path.exists(".env.example")
