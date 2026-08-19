"""Vytvoření správné implementace burzy podle konfigurace."""
from __future__ import annotations

import logging

from ..config import AppConfig
from .base import Exchange
from .paper import PaperExchange

log = logging.getLogger(__name__)


def build_exchange(cfg: AppConfig, starting_equity: float = 10_000.0) -> Exchange:
    """V paper/dry-run módu nikdy nevrátí klienta schopného odeslat reálný příkaz."""
    if cfg.live:
        from .ccxt_adapter import CCXTExchange

        log.warning(
            "ŽIVÝ REŽIM na %s (testnet=%s) — objednávky půjdou na reálný účet",
            cfg.exchange.id, cfg.exchange.testnet,
        )
        exchange = CCXTExchange(cfg.exchange)
    else:
        log.info("PAPER režim na datech z %s — žádné reálné objednávky", cfg.exchange.id)
        exchange = PaperExchange(cfg.exchange, starting_equity=starting_equity)
    exchange.load_markets()
    return exchange
