"""Testy API, ze kterého čerpá webové rozhraní."""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atb.config import load_config
from atb.state.store import Store
from atb.trader import Trader
from atb.webhook.server import create_app


@pytest.fixture()
def client(config, exchange, tmp_path):
    os.environ["WEBHOOK_SECRET"] = "ui-test-secret"
    config.strategy.adaptive_learning = False
    config.scanner.watchlist = ["BTC/USDT:USDT"]
    config.scanner.enabled = False               # skenujeme ručně, ať je test rychlý
    config.webhook.enforce_ip_allowlist = False
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    cfg_file = tmp_path / "config.yaml"
    with TestClient(create_app(config, trader, config_path=str(cfg_file))) as client:
        client.cfg_file = cfg_file
        client.trader = trader
        yield client
    trader.shutdown()


def test_state_endpoint_has_everything_ui_needs(client):
    body = client.get("/api/state").json()
    for key in ("account", "scanner", "signals", "trades", "settings"):
        assert key in body
    assert "equity" in body["account"]
    assert "values" in body["settings"]


def test_scan_returns_market_analysis(client):
    body = client.post("/api/scan").json()
    assert body["markets"], "skener nevrátil žádný trh"
    market = body["markets"][0]
    assert market["symbol"] == "BTC/USDT:USDT"
    assert market["market"]["regime"]
    for side in ("long", "short"):
        info = market["sides"][side]
        assert 0.0 <= info["score"] <= 1.0
        assert info["stop_loss"] > 0
        assert isinstance(info["take_profits"], list)
    assert isinstance(market["triggers"], list)
    assert len(market["sparkline"]) > 10


def test_stop_is_on_correct_side_for_each_direction(client):
    market = client.post("/api/scan").json()["markets"][0]
    price = market["market"]["price"]
    assert market["sides"]["long"]["stop_loss"] < price
    assert market["sides"]["short"]["stop_loss"] > price


def test_settings_update_applies_live_and_persists(client):
    response = client.put("/api/settings", json={"risk.risk_per_trade_pct": 1.25})
    assert response.status_code == 200
    assert client.trader.cfg.risk.risk_per_trade_pct == 1.25          # živý objekt
    saved = load_config(client.cfg_file)
    assert saved.risk.risk_per_trade_pct == 1.25                     # a soubor


def test_take_profit_count_is_editable(client):
    ladder = {r: [1.0, 2.0] for r in
              ("trend_up", "trend_down", "range", "volatile", "quiet")}
    fractions = {r: [0.5, 0.5] for r in ladder}
    ladder["trend_up"] = [0.8, 1.6, 2.5, 4.0]
    fractions["trend_up"] = [0.3, 0.3, 0.25, 0.15]

    response = client.put("/api/settings", json={
        "exits.tp_r_multiples": ladder, "exits.tp_fractions": fractions,
    })
    assert response.status_code == 200
    assert client.trader.cfg.exits.tp_r_multiples["trend_up"] == [0.8, 1.6, 2.5, 4.0]

    market = client.post("/api/scan").json()["markets"][0]
    regime = market["market"]["regime"]
    expected = 4 if regime == "trend_up" else 2
    assert len(market["sides"]["long"]["take_profits"]) == expected


def test_protected_fields_are_rejected(client):
    for payload in ({"mode": "live"}, {"dry_run": False},
                    {"exchange.api_key_env": "HACKED"}):
        response = client.put("/api/settings", json=payload)
        assert response.status_code == 400
        assert "nelze měnit" in response.json()["error"]
    assert client.trader.cfg.mode == "paper"


def test_invalid_value_is_rejected_and_nothing_changes(client):
    before = client.trader.cfg.risk.risk_per_trade_pct
    response = client.put("/api/settings", json={"risk.risk_per_trade_pct": 99.0})
    assert response.status_code == 400
    assert client.trader.cfg.risk.risk_per_trade_pct == before


def test_runtime_only_overrides_are_not_persisted(client):
    """--offline / --live se nesmí propsat do souboru s konfigurací."""
    client.trader.cfg.mode = "offline"
    client.trader.cfg.database = "/tmp/nekde-jinde.sqlite"
    client.put("/api/settings", json={"risk.max_leverage": 7})
    saved = json.loads(json.dumps(
        __import__("yaml").safe_load(Path(client.cfg_file).read_text(encoding="utf-8"))
    ))
    assert saved["risk"]["max_leverage"] == 7
    assert "mode" not in saved
    assert "database" not in saved


def test_ui_page_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Adaptive Trading Bot" in page.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/style.css").status_code == 200


def test_ui_asset_route_blocks_path_traversal(client):
    assert client.get("/ui/..%2F..%2Fconfig.py").status_code == 404


def test_close_endpoint_reports_result(client):
    body = client.post("/api/close/BTC%2FUSDT%3AUSDT").json()
    assert body["ok"] is True
