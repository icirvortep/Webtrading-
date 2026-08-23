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
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..config import EDITABLE_PATHS, AppConfig, apply_updates_inplace, persist_updates
from ..trader import Trader
from . import payload as payload_mod

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def create_app(cfg: AppConfig, trader: Trader, config_path: str | None = None) -> FastAPI:
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

    # ---------------- API pro webové rozhraní ----------------

    @router.get("/api/state")
    def api_state() -> dict[str, Any]:
        """Vše, co rozhraní potřebuje, v jednom dotazu."""
        return {
            "ts": time.time(),
            "account": trader.status(),
            "scanner": trader.scanner.state(),
            "signals": trader.store.recent_signals(30),
            "trades": {
                "open": trader.store.open_trades(),
                "closed": trader.store.recent_closed(30),
            },
            "settings": _settings_payload(cfg),
        }

    @router.get("/api/settings")
    def api_get_settings() -> dict[str, Any]:
        return _settings_payload(cfg)

    @router.put("/api/settings")
    def api_put_settings(updates: dict[str, Any]) -> dict[str, Any]:
        try:
            apply_updates_inplace(cfg, updates)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # pydantic ValidationError a spol.
            return JSONResponse({"error": str(exc)}, status_code=400)
        path = persist_updates(updates, config_path)
        log.info("Nastavení změněno z rozhraní: %s", ", ".join(sorted(updates)))
        return {"saved_to": str(path), "settings": _settings_payload(cfg)}

    @router.post("/api/scan")
    def api_scan() -> dict[str, Any]:
        trader.scanner.scan_once()
        return trader.scanner.state()

    @router.post("/api/close/{symbol:path}")
    def api_close(symbol: str) -> dict[str, Any]:
        result = trader.router.close(symbol, "ui")
        return {"ok": result.ok, "error": result.error}

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

    if cfg.ui.enabled:
        index = UI_DIR / "index.html"

        @router.get("/", response_class=HTMLResponse)
        def ui_index() -> HTMLResponse:
            return HTMLResponse(index.read_text(encoding="utf-8"))

        @router.get("/ui/{filename}")
        def ui_asset(filename: str) -> Any:
            """Statické soubory rozhraní — jen ze složky ui, bez procházení stromu."""
            target = (UI_DIR / filename).resolve()
            if target.parent != UI_DIR.resolve() or not target.is_file():
                return JSONResponse({"error": "not found"}, status_code=404)
            media = "text/javascript" if filename.endswith(".js") else "text/css"
            return Response(target.read_text(encoding="utf-8"), media_type=media)

    app.include_router(router)
    return app


def _settings_payload(cfg: AppConfig) -> dict[str, Any]:
    """Aktuální hodnoty editovatelných polí + kontext, který rozhraní zobrazuje."""
    values: dict[str, Any] = {}
    for path in EDITABLE_PATHS:
        dotted = ".".join(path) if isinstance(path, tuple) else path
        node: Any = cfg
        for key in dotted.split("."):
            node = getattr(node, key)
        values[dotted] = node
    return {
        "values": values,
        "readonly": {
            "mode": cfg.mode,
            "dry_run": cfg.dry_run,
            "exchange": cfg.exchange.id,
            "testnet": cfg.exchange.testnet,
            "quote": cfg.exchange.quote,
        },
        "regimes": ["trend_up", "trend_down", "range", "volatile", "quiet"],
    }


def _client_ip(request: Request) -> str:
    """Za reverzní proxy bere první IP z X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
