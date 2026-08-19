"""HTTP server přijímající alerty z TradingView.

Bezpečnost webhooku (endpoint je veřejně dostupný z internetu):
  * IP allowlist oficiálních odesílacích adres TradingView,
  * HMAC-SHA256 podpis těla v hlavičce X-Signature, nebo sdílený secret
    přímo v JSON payloadu,
  * limit velikosti těla a deduplikace signálů podle ID.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from ..config import AppConfig
from ..trader import Trader
from . import payload as payload_mod

log = logging.getLogger(__name__)


def create_app(cfg: AppConfig, trader: Trader) -> FastAPI:
    app = FastAPI(
        title="Adaptive Trading Bot",
        description="TradingView webhook → adaptivní strategie → burza",
        version="1.0.0",
        docs_url="/docs" if not cfg.live else None,
    )
    router = APIRouter()
    secret = os.getenv(cfg.webhook.secret_env, "")

    if cfg.live and not secret:
        raise RuntimeError(
            f"V živém režimu je povinný webhook secret — nastav {cfg.webhook.secret_env}"
        )

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "mode": cfg.mode, "exchange": trader.exchange.id}

    @router.get("/status")
    def status() -> dict[str, Any]:
        return trader.status()

    @router.get("/trades")
    def trades(limit: int = 20) -> dict[str, Any]:
        return {
            "open": trader.store.open_trades(),
            "closed": trader.store.recent_closed(min(limit, 200)),
        }

    @router.post("/control/kill-switch")
    def kill_switch(enable: bool = True) -> dict[str, Any]:
        cfg.risk.kill_switch = enable
        log.warning("Kill switch %s", "AKTIVOVÁN" if enable else "vypnut")
        return {"kill_switch": enable}

    @router.post("/control/close-all")
    def close_all() -> dict[str, Any]:
        results = trader.router.close_all("api_close_all")
        return {"closed": len(results), "errors": [r.error for r in results if not r.ok]}

    @router.post(cfg.webhook.path)
    async def webhook(
        request: Request,
        x_signature: str | None = Header(default=None, alias="X-Signature"),
    ) -> JSONResponse:
        client_ip = _client_ip(request)
        blocked_ip = (
            cfg.webhook.enforce_ip_allowlist
            and cfg.webhook.allowed_ips
            and client_ip not in cfg.webhook.allowed_ips
        )
        if blocked_ip:
            log.warning("Webhook z nepovolené IP %s zamítnut", client_ip)
            return JSONResponse({"error": "forbidden"}, status_code=403)

        body = await request.body()
        if len(body) > cfg.webhook.max_body_bytes:
            return JSONResponse({"error": "payload too large"}, status_code=413)

        use_hmac = cfg.webhook.require_hmac and x_signature is not None
        if use_hmac and not payload_mod.verify_signature(body, x_signature, secret):
            log.warning("Neplatný HMAC podpis z %s", client_ip)
            return JSONResponse({"error": "invalid signature"}, status_code=401)

        try:
            signal = payload_mod.parse(
                body, shared_secret=secret, require_secret_in_body=not use_hmac and bool(secret)
            )
        except payload_mod.PayloadError as exc:
            log.warning("Neplatný payload z %s: %s", client_ip, exc)
            return JSONResponse({"error": str(exc)}, status_code=400)

        log.info(
            "Signál %s: %s %s %s (%s)",
            signal.id, signal.action.value, signal.symbol,
            signal.side.value if signal.side else "-", signal.timeframe,
        )
        try:
            result = trader.handle_signal(signal)
        except Exception as exc:  # server nesmí spadnout kvůli jednomu signálu
            log.exception("Zpracování signálu selhalo")
            return JSONResponse({"error": str(exc)}, status_code=500)

        code = 200 if result.get("status") not in ("error",) else 500
        return JSONResponse(result, status_code=code)

    app.include_router(router)
    return app


def _client_ip(request: Request) -> str:
    """Za reverzní proxy bere první IP z X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
