"""Parsování a ověření příchozích webhooků z TradingView.

TradingView umí poslat libovolný text; doporučený formát je JSON (viz
pine/ šablona). Podporujeme i jednoduchý textový formát typu
"BUY BTCUSDT" jako fallback.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any

from ..models import Action, Side, Signal

log = logging.getLogger(__name__)

_SIDE_WORDS = {
    "buy": Side.LONG, "long": Side.LONG, "bull": Side.LONG,
    "sell": Side.SHORT, "short": Side.SHORT, "bear": Side.SHORT,
}
_EXIT_WORDS = {"exit", "close", "flat", "tp", "sl"}


class PayloadError(ValueError):
    pass


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """HMAC-SHA256 podpis těla požadavku v hlavičce X-Signature (hex)."""
    if not secret:
        return False
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = signature.strip().lower().removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def parse(body: bytes, shared_secret: str = "", require_secret_in_body: bool = False) -> Signal:
    """Z těla requestu vyrobí normalizovaný Signal."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise PayloadError("prázdné tělo požadavku")

    data: dict[str, Any]
    try:
        parsed = json.loads(text)
        data = parsed if isinstance(parsed, dict) else {"text": text}
    except json.JSONDecodeError:
        data = _parse_text(text)

    if require_secret_in_body:
        provided = str(data.get("secret", ""))
        if not shared_secret or not hmac.compare_digest(provided, shared_secret):
            raise PayloadError("neplatný nebo chybějící secret v těle")

    symbol = _normalize_symbol(str(data.get("symbol") or data.get("ticker") or ""))
    if not symbol:
        raise PayloadError("chybí symbol")

    action = _parse_action(data)
    side = _parse_side(data, action)

    return Signal(
        symbol=symbol,
        action=action,
        side=side,
        strategy=str(data.get("strategy") or data.get("alert_name") or "tv"),
        timeframe=_normalize_timeframe(str(data.get("timeframe") or data.get("interval") or "15")),
        price=_opt_float(data.get("price") or data.get("close")),
        confidence=_clamp(_opt_float(data.get("confidence")) or 0.5),
        sl_hint=_opt_float(data.get("sl") or data.get("stop_loss")),
        tp_hint=_opt_float(data.get("tp") or data.get("take_profit")),
        risk_multiplier=max(_opt_float(data.get("risk_multiplier")) or 1.0, 0.0),
        venue=(str(data["exchange"]) if data.get("exchange") else None),
        raw=data,
        id=str(data.get("id") or "") or _fallback_id(text),
    )


def _parse_text(text: str) -> dict[str, Any]:
    """Fallback: 'BUY BTCUSDT 15m' nebo 'close ETHUSDT'."""
    tokens = re.split(r"[\s,;]+", text.lower())
    data: dict[str, Any] = {"text": text}
    for token in tokens:
        if token in _SIDE_WORDS and "action" not in data:
            data["action"] = token
        elif token in _EXIT_WORDS and "action" not in data:
            data["action"] = "exit"
        elif re.fullmatch(r"\d+[mhdw]", token):
            data["timeframe"] = token
        elif re.fullmatch(r"[a-z0-9]{4,20}([/:][a-z0-9:]{2,12})?", token) and "symbol" not in data:
            data["symbol"] = token.upper()
    return data


def _parse_action(data: dict[str, Any]) -> Action:
    raw = str(data.get("action") or data.get("side") or data.get("order") or "").lower().strip()
    if raw in {"close_all", "closeall", "flatten"}:
        return Action.CLOSE_ALL
    if raw in {"reverse", "flip"}:
        return Action.REVERSE
    if raw in _EXIT_WORDS or raw.startswith("exit"):
        return Action.EXIT
    if raw in _SIDE_WORDS or raw in {"entry", "open"}:
        return Action.ENTRY
    if data.get("side") or data.get("direction"):
        return Action.ENTRY
    raise PayloadError(f"neznámá akce: '{raw}'")


def _parse_side(data: dict[str, Any], action: Action) -> Side | None:
    if action in (Action.EXIT, Action.CLOSE_ALL):
        return None
    for key in ("side", "action", "direction", "order"):
        raw = str(data.get(key) or "").lower().strip()
        if raw in _SIDE_WORDS:
            return _SIDE_WORDS[raw]
    raise PayloadError("chybí směr obchodu (buy/sell)")


def _normalize_symbol(symbol: str) -> str:
    """'BTCUSDT' → 'BTC/USDT:USDT' (CCXT formát perpetual kontraktu)."""
    raw = symbol.strip().upper()
    if not raw:
        return ""
    if "/" in raw:
        return raw                       # už je v CCXT tvaru (BTC/USDT:USDT)
    if ":" in raw:
        raw = raw.split(":")[-1]         # 'BYBIT:BTCUSDT' → 'BTCUSDT'
    raw = raw.removesuffix(".P").replace("PERP", "")
    for quote in ("USDT", "USDC", "USD", "BUSD"):
        if raw.endswith(quote) and len(raw) > len(quote):
            base = raw[: -len(quote)]
            settle = "USDT" if quote in ("USDT", "BUSD") else quote
            return f"{base}/{quote}:{settle}"
    return raw


def _normalize_timeframe(value: str) -> str:
    """TradingView posílá '15', '60', '240', 'D' — převedeme na CCXT tvar."""
    raw = value.strip().lower()
    if re.fullmatch(r"\d+[mhdw]", raw):
        return raw
    if raw in {"d", "1d", "day"}:
        return "1d"
    if raw in {"w", "1w", "week"}:
        return "1w"
    if raw.isdigit():
        minutes = int(raw)
        if minutes >= 1440 and minutes % 1440 == 0:
            return f"{minutes // 1440}d"
        if minutes >= 60 and minutes % 60 == 0:
            return f"{minutes // 60}h"
        return f"{minutes}m"
    return "15m"


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _fallback_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]
