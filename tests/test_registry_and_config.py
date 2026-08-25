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


def test_dotenv_strips_inline_comments(tmp_path, monkeypatch, clean_env):
    """.env.example má komentáře na konci řádků — nesmí se dostat do hodnot."""
    from atb.main import _load_dotenv

    env = tmp_path / ".env"
    env.write_text(
        "ATB_MODE=paper          # paper | live\n"
        "ATB_RISK_PCT=2.0\n"
        'EXCHANGE_API_SECRET="tajne#heslo"\n'
        "# celý řádek komentář\n"
        "PRAZDNE=\n",
        encoding="utf-8",
    )
    for key in ("ATB_MODE", "ATB_RISK_PCT", "EXCHANGE_API_SECRET", "PRAZDNE"):
        monkeypatch.delenv(key, raising=False)

    _load_dotenv(str(env))
    assert os.environ["ATB_MODE"] == "paper"
    assert os.environ["ATB_RISK_PCT"] == "2.0"
    assert os.environ["EXCHANGE_API_SECRET"] == "tajne#heslo"   # uvozovky = doslova
    assert os.environ["PRAZDNE"] == ""


def test_shipped_env_example_loads_into_valid_config(tmp_path, monkeypatch, clean_env):
    """Celý .env.example musí projít až do platné konfigurace."""
    from atb.main import _load_dotenv

    for key in ("ATB_MODE", "ATB_DRY_RUN", "ATB_EXCHANGE", "ATB_TESTNET",
                "ATB_RISK_PCT", "ATB_MAX_LEVERAGE", "ATB_KILL_SWITCH",
                "ATB_WEBHOOK_PORT", "ATB_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)
    _load_dotenv(".env.example")
    cfg = load_config("config/config.yaml")
    assert cfg.mode == "paper"
    assert cfg.risk.risk_per_trade_pct == 2.0
    assert cfg.webhook.port == 8080


def test_example_config_documents_every_section():
    """Ukázkový config musí popisovat všechny sekce, ne jen ty původní."""
    text = Path("config/config.example.yaml").read_text(encoding="utf-8")
    for section in ("exchange:", "risk:", "strategy:", "exits:", "universe:",
                    "scanner:", "ui:", "webhook:", "monitor:", "notify:"):
        assert section in text, f"v ukázkovém configu chybí sekce {section}"


def test_example_config_matches_defaults():
    """Hodnoty v ukázce musí odpovídat výchozím, jinak dokumentace lže."""
    shipped = load_config("config/config.example.yaml")
    defaults = AppConfig()
    assert shipped.universe.deep_scan_count == defaults.universe.deep_scan_count
    assert shipped.scanner.auto_universe == defaults.scanner.auto_universe
    assert shipped.scanner.autopilot is False
    assert shipped.risk.risk_per_trade_pct == defaults.risk.risk_per_trade_pct


def test_default_allowlist_does_not_block_auto_universe():
    """Vyplněný allowlist by tiše zabil automatický výběr trhů."""
    cfg = load_config("config/config.example.yaml")
    assert cfg.symbols_allowlist == []


def test_registry_flags_venues_without_perpetuals():
    """Bybit EU nabízí jen spot a margin — registr to musí říct nahlas."""
    from atb.exchanges import registry

    assert registry.supports_swap("bybit") is True
    assert registry.supports_swap("bybiteu") is False
    assert "perpetual" in registry.get("bybiteu").notes.lower()
    assert "POZOR" in registry.table()


def test_unsupported_market_type_fails_with_a_clear_message(monkeypatch):
    """Nesoulad typu trhu musí padnout hned a vysvětlit proč."""
    import ccxt

    from atb.config import ExchangeConfig
    from atb.exchanges.ccxt_adapter import CCXTExchange, ExchangeError

    monkeypatch.setenv("EXCHANGE_API_KEY", "k")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s")
    assert ccxt.bybiteu().has["swap"] is False, "předpoklad testu už neplatí"

    cfg = ExchangeConfig(id="bybiteu", account_type="swap", testnet=False)
    with pytest.raises(ExchangeError, match="nenabízí typ trhu"):
        CCXTExchange(cfg)


def test_universe_quote_defaults_to_the_account_currency():
    """Nesoulad měn by tiše hledal trhy, na které nemáš čím zaplatit."""
    cfg = AppConfig.model_validate({"exchange": {"quote": "USDC"}})
    assert cfg.universe.quotes == ["USDC"]


def test_universe_can_span_several_quote_currencies():
    cfg = AppConfig.model_validate({
        "exchange": {"quote": "USDC"}, "universe": {"quotes": ["USDC", "USDT"]},
    })
    assert cfg.universe.quotes == ["USDC", "USDT"]


def test_missing_config_is_created_from_the_example(tmp_path):
    """Vlastní nastavení není v gitu — musí vzniknout samo, jinak nic nenajede."""
    import shutil

    target = tmp_path / "config.yaml"
    shutil.copy("config/config.example.yaml", tmp_path / "config.example.yaml")
    assert not target.exists()

    cfg = load_config(target)
    assert target.exists()
    assert cfg.risk.risk_per_trade_pct == 2.0


def test_user_config_is_not_tracked_by_git():
    """Sledovaný config.yaml působí konflikt při každé aktualizaci."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "config/config.yaml"], capture_output=True, text=True,
    ).stdout.strip()
    assert tracked == "", "config/config.yaml nesmí být verzovaný"
    assert "config/config.yaml" in Path(".gitignore").read_text(encoding="utf-8")
