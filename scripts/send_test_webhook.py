#!/usr/bin/env python3
"""Pošle podepsaný testovací webhook na běžícího bota.

Použití:
    python scripts/send_test_webhook.py --symbol BTCUSDT --action buy
    WEBHOOK_SECRET=… python scripts/send_test_webhook.py --url http://localhost:8080/webhook/tradingview
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Testovací TradingView webhook")
    parser.add_argument("--url", default="http://localhost:8080/webhook/tradingview")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--action", default="buy", choices=["buy", "sell", "close", "close_all"])
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--confidence", type=float, default=0.8)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--sl", type=float, default=None)
    args = parser.parse_args()

    secret = os.getenv("WEBHOOK_SECRET", "")
    payload = {
        "action": args.action,
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "confidence": args.confidence,
        "strategy": "manual-test",
        "id": f"test-{int(time.time())}",
    }
    if args.price:
        payload["price"] = args.price
    if args.sl:
        payload["sl"] = args.sl

    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    else:
        print("Upozornění: WEBHOOK_SECRET není nastaven, request nebude podepsaný.")

    request = urllib.request.Request(args.url, data=body, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            print(response.status, response.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()}")
        return 1
    except OSError as exc:
        print(f"Nepodařilo se spojit s botem: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
