import hashlib
import hmac
import json

import pytest

from atb.models import Action, Side
from atb.webhook import payload as payload_mod


def body(data: dict) -> bytes:
    return json.dumps(data).encode()


def test_parse_full_json_signal():
    signal = payload_mod.parse(body({
        "action": "buy", "symbol": "BTCUSDT", "timeframe": "15",
        "price": 65000.0, "sl": 64000.0, "confidence": 0.82,
        "strategy": "ase", "id": "abc123",
    }))
    assert signal.action is Action.ENTRY
    assert signal.side is Side.LONG
    assert signal.symbol == "BTC/USDT:USDT"
    assert signal.timeframe == "15m"
    assert signal.sl_hint == 64000.0
    assert signal.confidence == pytest.approx(0.82)
    assert signal.id == "abc123"


def test_parse_sell_signal():
    signal = payload_mod.parse(body({"action": "sell", "symbol": "ETHUSDT"}))
    assert signal.side is Side.SHORT


def test_parse_exit_signal_has_no_side():
    signal = payload_mod.parse(body({"action": "close", "symbol": "ETHUSDT"}))
    assert signal.action is Action.EXIT
    assert signal.side is None


def test_parse_close_all():
    signal = payload_mod.parse(body({"action": "close_all", "symbol": "ETHUSDT"}))
    assert signal.action is Action.CLOSE_ALL


def test_parse_plain_text_fallback():
    signal = payload_mod.parse(b"BUY BTCUSDT 15m")
    assert signal.side is Side.LONG
    assert signal.symbol == "BTC/USDT:USDT"
    assert signal.timeframe == "15m"


def test_missing_symbol_raises():
    with pytest.raises(payload_mod.PayloadError):
        payload_mod.parse(body({"action": "buy"}))


def test_unknown_action_raises():
    with pytest.raises(payload_mod.PayloadError):
        payload_mod.parse(body({"action": "dance", "symbol": "BTCUSDT"}))


def test_empty_body_raises():
    with pytest.raises(payload_mod.PayloadError):
        payload_mod.parse(b"   ")


def test_confidence_is_clamped():
    signal = payload_mod.parse(body({"action": "buy", "symbol": "BTCUSDT", "confidence": 9.9}))
    assert signal.confidence == 1.0


def test_secret_in_body_is_required_when_configured():
    data = {"action": "buy", "symbol": "BTCUSDT"}
    with pytest.raises(payload_mod.PayloadError):
        payload_mod.parse(body(data), shared_secret="s3cret", require_secret_in_body=True)
    signal = payload_mod.parse(body({**data, "secret": "s3cret"}),
                               shared_secret="s3cret", require_secret_in_body=True)
    assert signal.side is Side.LONG


def test_hmac_signature_roundtrip():
    secret = "topsecret"
    raw = body({"action": "buy", "symbol": "BTCUSDT"})
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert payload_mod.verify_signature(raw, signature, secret)
    assert payload_mod.verify_signature(raw, f"sha256={signature}", secret)
    assert not payload_mod.verify_signature(raw + b"x", signature, secret)
    assert not payload_mod.verify_signature(raw, None, secret)
    assert not payload_mod.verify_signature(raw, signature, "")


@pytest.mark.parametrize(("raw", "expected"), [
    ("BTCUSDT", "BTC/USDT:USDT"),
    ("BYBIT:ETHUSDT.P", "ETH/USDT:USDT"),
    ("SOL/USDT:USDT", "SOL/USDT:USDT"),
    ("XRPUSDC", "XRP/USDC:USDC"),
])
def test_symbol_normalization(raw, expected):
    assert payload_mod._normalize_symbol(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("15", "15m"), ("60", "1h"), ("240", "4h"), ("D", "1d"), ("5m", "5m"), ("1440", "1d"),
])
def test_timeframe_normalization(raw, expected):
    assert payload_mod._normalize_timeframe(raw) == expected
