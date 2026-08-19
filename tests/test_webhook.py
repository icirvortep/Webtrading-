"""Testy HTTP vrstvy — ověřování podpisu, IP allowlist, chybové stavy."""
import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient

from atb.state.store import Store
from atb.trader import Trader
from atb.webhook.server import create_app

SECRET = "test-secret-abc"


@pytest.fixture()
def client(config, exchange):
    os.environ["WEBHOOK_SECRET"] = SECRET
    config.strategy.adaptive_learning = False
    config.webhook.require_hmac = True
    config.webhook.enforce_ip_allowlist = False
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    with TestClient(create_app(config, trader)) as client:
        yield client
    trader.shutdown()


def signed(data: dict) -> tuple[bytes, dict]:
    raw = json.dumps(data).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Signature": signature, "Content-Type": "application/json"}


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_status_endpoint(client):
    response = client.get("/status")
    assert response.status_code == 200
    assert "equity" in response.json()


def test_valid_signed_webhook_is_executed(client):
    raw, headers = signed({"action": "buy", "symbol": "BTCUSDT", "timeframe": "15m", "id": "sig-1"})
    response = client.post("/webhook/tradingview", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] in {"executed", "rejected"}


def test_invalid_signature_is_rejected(client):
    raw, headers = signed({"action": "buy", "symbol": "BTCUSDT", "id": "sig-2"})
    headers["X-Signature"] = "0" * 64
    response = client.post("/webhook/tradingview", content=raw, headers=headers)
    assert response.status_code == 401


def test_tampered_body_is_rejected(client):
    raw, headers = signed({"action": "buy", "symbol": "BTCUSDT", "id": "sig-3"})
    response = client.post("/webhook/tradingview", content=raw + b" ", headers=headers)
    assert response.status_code == 401


def test_unsigned_request_falls_back_to_body_secret(client):
    payload = {"action": "buy", "symbol": "BTCUSDT", "id": "sig-4"}
    response = client.post("/webhook/tradingview", json=payload)
    assert response.status_code == 400          # chybí secret v těle

    payload["secret"] = SECRET
    payload["id"] = "sig-5"
    response = client.post("/webhook/tradingview", json=payload)
    assert response.status_code == 200


def test_malformed_payload_returns_400(client):
    raw, headers = signed({"action": "nonsense", "symbol": "BTCUSDT"})
    response = client.post("/webhook/tradingview", content=raw, headers=headers)
    assert response.status_code == 400


def test_oversized_payload_rejected(client, config):
    config.webhook.max_body_bytes = 32
    raw, headers = signed({"action": "buy", "symbol": "BTCUSDT", "note": "x" * 200})
    response = client.post("/webhook/tradingview", content=raw, headers=headers)
    assert response.status_code == 413


def test_ip_allowlist_blocks_unknown_source(config, exchange):
    os.environ["WEBHOOK_SECRET"] = SECRET
    config.webhook.require_hmac = True
    config.webhook.enforce_ip_allowlist = True
    config.webhook.allowed_ips = ["52.89.214.238"]
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    with TestClient(create_app(config, trader)) as client:
        raw, headers = signed({"action": "buy", "symbol": "BTCUSDT", "id": "sig-6"})
        blocked = client.post("/webhook/tradingview", content=raw, headers=headers)
        assert blocked.status_code == 403

        headers["X-Forwarded-For"] = "52.89.214.238"
        allowed = client.post("/webhook/tradingview", content=raw, headers=headers)
        assert allowed.status_code == 200
    trader.shutdown()


def test_kill_switch_endpoint(client):
    assert client.post("/control/kill-switch?enable=true").json()["kill_switch"] is True
    raw, headers = signed({"action": "buy", "symbol": "BTCUSDT", "id": "sig-7"})
    response = client.post("/webhook/tradingview", content=raw, headers=headers)
    assert response.json()["reason"] == "kill_switch"


def test_live_mode_requires_secret(config, exchange, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    config.mode = "live"
    config.dry_run = False
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    with pytest.raises(RuntimeError, match="webhook secret"):
        create_app(config, trader)
    trader.shutdown()
